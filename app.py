import os
import shutil
import zipfile
import threading
import uuid
import time
import json
from datetime import datetime
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

# Directory structure
DOWNLOADS_DIR = "downloads"
MUSIC_DIR = "music"
LIBRARY_FILE = "library.json"

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(MUSIC_DIR, exist_ok=True)

# Hardcoded user credentials
VALID_USERS = {
    "Sanya": "123456789",
    "Demo": "1234"
}

# In-memory job store for download progress
jobs = {}

# In-memory library & playlists (persisted to JSON)
library = {"songs": {}, "playlists": {}}

def load_library():
    """Load library from disk"""
    global library
    if os.path.exists(LIBRARY_FILE):
        try:
            with open(LIBRARY_FILE, 'r') as f:
                library = json.load(f)
        except:
            library = {"songs": {}, "playlists": {}}
    return library

def save_library():
    """Save library to disk"""
    with open(LIBRARY_FILE, 'w') as f:
        json.dump(library, f, indent=2)

DEFAULT_SONGS = [
    "Icarus Bastille", "Pompeii Bastille", "Achilles Come Down Gang of Youths",
    "Glory and Gore Lorde", "Touch the Sky Julie Fowlis", "Centuries Fall Out Boy",
    "I Am the Best 2NE1", "Touch-Tone Telephone Lemon Demon",
    "Cult of Dionysus The Orion Experience", "Abhi Kuch Dino Se Pritam",
    "Chandaniya 2 States", "Chand Si Mehbooba Ho Meri", "Chaudhary Mame Khan",
    "Sawan Mein Lag Gayi Aag Falguni Pathak", "Dhuro Nachyo Abhigya The Artist",
    "Huri Chalyo Prashant",
]

def run_download_job(job_id, songs):
    """Download songs to /downloads/{job_id}, then copy to /music"""
    job = jobs[job_id]
    job["total"] = len(songs)
    job["progress"] = 0
    job["status"] = "running"
    job["downloaded"] = []
    job["failed"] = []
    job["current"] = ""

    session_dir = os.path.join(DOWNLOADS_DIR, job_id)
    os.makedirs(session_dir, exist_ok=True)

    ydl_opts = {
        "format": "bestaudio/best",
        "default_search": "ytsearch1",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "outtmpl": os.path.join(session_dir, "%(title)s.%(ext)s"),
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": shutil.which("ffmpeg"),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for song in songs:
            if job.get("cancelled"):
                break
            
            job["current"] = song
            print(f"[DOWNLOAD] Downloading: {song}")
            
            try:
                info = ydl.extract_info(song, download=True)
                song_title = info.get("title", song)
                song_id = str(uuid.uuid4())
                
                # Find the MP3 file that was just created
                mp3_files = [f for f in os.listdir(session_dir) if f.endswith('.mp3')]
                if mp3_files:
                    temp_file = os.path.join(session_dir, mp3_files[-1])
                    # Copy to permanent music directory
                    final_path = os.path.join(MUSIC_DIR, f"{song_id}.mp3")
                    shutil.copy2(temp_file, final_path)
                    
                    # Get duration
                    duration = 0
                    try:
                        import wave
                        import contextlib
                        with contextlib.closing(wave.open(final_path, 'rb')) as f:
                            frames = f.getnframes()
                            rate = f.getframerate()
                            duration = frames // rate
                    except:
                        duration = 0
                    
                    # Add to library
                    library["songs"][song_id] = {
                        "id": song_id,
                        "title": song_title,
                        "artist": song,
                        "path": final_path,
                        "duration": duration,
                        "added": datetime.now().isoformat(),
                    }
                    job["downloaded"].append(song_title)
                    print(f"[DOWNLOAD] Added: {song_title} ({duration}s)")
                    
            except Exception as e:
                print(f"[DOWNLOAD] Failed: {song} - {str(e)}")
                job["failed"].append({"song": song, "error": str(e)})
            
            job["progress"] += 1

    # Cleanup temp directory
    try:
        shutil.rmtree(session_dir, ignore_errors=True)
    except:
        pass
    
    save_library()
    job["status"] = "done"
    print(f"[DOWNLOAD] Job {job_id} completed")

# ============ AUTHENTICATION ============

@app.route("/api/auth/login", methods=["POST"])
def login():
    """Authenticate user with hardcoded credentials"""
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    
    print(f"[LOGIN] Attempt: username='{username}'")
    
    if username not in VALID_USERS or VALID_USERS[username] != password:
        print(f"[LOGIN] Failed for '{username}'")
        return jsonify({"error": "Invalid credentials"}), 401
    
    print(f"[LOGIN] Success for '{username}'")
    return jsonify({"token": str(uuid.uuid4()), "username": username})

# ============ DOWNLOAD ============

@app.route("/api/songs", methods=["GET"])
def get_songs():
    """Get suggested songs for RIP"""
    return jsonify({"songs": DEFAULT_SONGS})

@app.route("/api/download", methods=["POST"])
def start_download():
    """Start download job - accepts both suggested and custom songs"""
    data = request.json or {}
    songs = data.get("songs", [])
    
    if not songs:
        return jsonify({"error": "No songs provided"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "total": len(songs),
        "downloaded": [],
        "failed": [],
        "current": "",
        "cancelled": False,
    }

    thread = threading.Thread(target=run_download_job, args=(job_id, songs), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>", methods=["GET"])
def job_status(job_id):
    """Get job status with progress"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
        "current": job.get("current", ""),
        "failed_count": len(job.get("failed", [])),
        "failed": job.get("failed", []),
        "downloaded": job.get("downloaded", []),
    })

@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    """Cancel download job"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["cancelled"] = True
    return jsonify({"ok": True})

# ============ LIBRARY ============

@app.route("/api/library", methods=["GET"])
def get_library():
    """Get all songs in library"""
    return jsonify(library["songs"])

@app.route("/api/library/<song_id>", methods=["GET"])
def get_song(song_id):
    """Get single song metadata"""
    if song_id not in library["songs"]:
        return jsonify({"error": "Not found"}), 404
    return jsonify(library["songs"][song_id])

@app.route("/api/library/<song_id>", methods=["PUT"])
def update_song(song_id):
    """Update song metadata"""
    if song_id not in library["songs"]:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    for key in ["title", "artist"]:
        if key in data:
            library["songs"][song_id][key] = data[key]
    save_library()
    return jsonify(library["songs"][song_id])

@app.route("/api/library/<song_id>", methods=["DELETE"])
def delete_song(song_id):
    """Delete song from library"""
    if song_id not in library["songs"]:
        return jsonify({"error": "Not found"}), 404
    
    song = library["songs"][song_id]
    # Delete file
    if os.path.exists(song["path"]):
        try:
            os.remove(song["path"])
        except:
            pass
    
    del library["songs"][song_id]
    # Remove from all playlists
    for playlist in library["playlists"].values():
        playlist["songs"] = [s for s in playlist["songs"] if s != song_id]
    
    save_library()
    return jsonify({"ok": True})

# ============ AUDIO STREAMING ============

@app.route("/api/audio/<song_id>/stream", methods=["GET"])
def stream_audio(song_id):
    """Stream MP3 file with range request support"""
    if song_id not in library["songs"]:
        return jsonify({"error": "Not found"}), 404
    
    song = library["songs"][song_id]
    file_path = song["path"]
    
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    
    # Support range requests for seeking
    range_header = request.headers.get('Range')
    file_size = os.path.getsize(file_path)
    
    if range_header:
        try:
            start, end = range_header.replace('bytes=', '').split('-')
            start = int(start) if start else 0
            end = int(end) if end else file_size - 1
            
            with open(file_path, 'rb') as f:
                f.seek(start)
                data = f.read(end - start + 1)
            
            response = app.response_class(data, 206, mimetype='audio/mpeg')
            response.headers['Accept-Ranges'] = 'bytes'
            response.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
            response.headers['Content-Length'] = len(data)
            return response
        except:
            pass
    
    # No range request
    response = send_file(file_path, mimetype='audio/mpeg')
    response.headers['Accept-Ranges'] = 'bytes'
    return response

# ============ PLAYLISTS ============

@app.route("/api/playlists", methods=["GET"])
def get_playlists():
    """Get all playlists"""
    return jsonify(library["playlists"])

@app.route("/api/playlists", methods=["POST"])
def create_playlist():
    """Create new playlist"""
    data = request.json or {}
    name = data.get("name", "Untitled")
    playlist_id = str(uuid.uuid4())
    library["playlists"][playlist_id] = {
        "id": playlist_id,
        "name": name,
        "songs": [],
        "created": datetime.now().isoformat()
    }
    save_library()
    return jsonify(library["playlists"][playlist_id])

@app.route("/api/playlists/<playlist_id>", methods=["GET"])
def get_playlist(playlist_id):
    """Get playlist with songs"""
    if playlist_id not in library["playlists"]:
        return jsonify({"error": "Not found"}), 404
    playlist = library["playlists"][playlist_id]
    playlist_copy = playlist.copy()
    playlist_copy["songs"] = [library["songs"][sid] for sid in playlist["songs"] if sid in library["songs"]]
    return jsonify(playlist_copy)

@app.route("/api/playlists/<playlist_id>", methods=["PUT"])
def update_playlist(playlist_id):
    """Update playlist metadata or reorder songs"""
    if playlist_id not in library["playlists"]:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    
    if "name" in data:
        library["playlists"][playlist_id]["name"] = data["name"]
    if "songs" in data:
        library["playlists"][playlist_id]["songs"] = data["songs"]
    
    save_library()
    return jsonify(library["playlists"][playlist_id])

@app.route("/api/playlists/<playlist_id>", methods=["DELETE"])
def delete_playlist(playlist_id):
    """Delete playlist"""
    if playlist_id not in library["playlists"]:
        return jsonify({"error": "Not found"}), 404
    del library["playlists"][playlist_id]
    save_library()
    return jsonify({"ok": True})

@app.route("/api/playlists/<playlist_id>/songs/<song_id>", methods=["POST"])
def add_song_to_playlist(playlist_id, song_id):
    """Add song to playlist"""
    if playlist_id not in library["playlists"] or song_id not in library["songs"]:
        return jsonify({"error": "Not found"}), 404
    if song_id not in library["playlists"][playlist_id]["songs"]:
        library["playlists"][playlist_id]["songs"].append(song_id)
    save_library()
    return jsonify({"ok": True})

@app.route("/api/playlists/<playlist_id>/songs/<song_id>", methods=["DELETE"])
def remove_song_from_playlist(playlist_id, song_id):
    """Remove song from playlist"""
    if playlist_id not in library["playlists"]:
        return jsonify({"error": "Not found"}), 404
    library["playlists"][playlist_id]["songs"] = [
        s for s in library["playlists"][playlist_id]["songs"] if s != song_id
    ]
    save_library()
    return jsonify({"ok": True})

@app.route("/api/playlists/<playlist_id>/songs", methods=["PUT"])
def reorder_playlist_songs(playlist_id):
    """Reorder songs in playlist"""
    if playlist_id not in library["playlists"]:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    if "songs" in data:
        library["playlists"][playlist_id]["songs"] = data["songs"]
    save_library()
    return jsonify(library["playlists"][playlist_id])

@app.route("/")
def home():
    return send_file("index.html")

if __name__ == "__main__":
    load_library()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)