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
CORS(app, resources={r"/api/*": {"origins": "*", "methods": ["GET", "POST", "PUT", "DELETE"]}})

DOWNLOAD_DIR = "downloads"
ZIP_DIR = "zips"
LIBRARY_FILE = "library.json"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(ZIP_DIR, exist_ok=True)

# Hardcoded user credentials
VALID_USERS = {
    "Sanya": "123456789",
    "Demo": "1234"
}

# In-memory job store
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

def generate_song_id(title):
    """Generate unique song ID"""
    return str(uuid.uuid4())

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
    """Download songs and add to library"""
    job = jobs[job_id]
    job["total"] = len(songs)
    job["progress"] = 0
    job["status"] = "running"
    job["downloaded"] = []
    job["failed"] = []

    session_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(session_dir, exist_ok=True)

    def progress_hook(d):
        if d["status"] == "finished":
            job["downloaded"].append(d.get("filename", ""))

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
        "progress_hooks": [progress_hook],
        "ffmpeg_location": shutil.which("ffmpeg"),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for song in songs:
            if job.get("cancelled"):
                break
            try:
                info = ydl.extract_info(song, download=True)
                song_title = info.get("title", song)
                song_id = generate_song_id(song_title)
                
                # Find the actual MP3 file that was created
                mp3_path = None
                for fname in os.listdir(session_dir):
                    if fname.endswith(".mp3"):
                        full_path = os.path.join(session_dir, fname)
                        mp3_path = full_path
                        break
                
                if not mp3_path:
                    job["failed"].append({"song": song, "error": "MP3 not found after conversion"})
                    continue
                
                # Add to library with full path
                library["songs"][song_id] = {
                    "id": song_id,
                    "title": song_title,
                    "artist": song,
                    "duration": info.get("duration", 0),
                    "added": datetime.now().isoformat(),
                    "path": f"/api/audio/{song_id}/play"
                }
            except Exception as e:
                job["failed"].append({"song": song, "error": str(e)})
            job["progress"] += 1

    # Zip everything
    if not job.get("cancelled"):
        zip_path = os.path.join(ZIP_DIR, f"{job_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname in os.listdir(session_dir):
                if fname.endswith(".mp3"):
                    zf.write(os.path.join(session_dir, fname), fname)
        job["zip_path"] = zip_path
        job["status"] = "done"
        save_library()
    else:
        job["status"] = "cancelled"

    # Don't delete session dir - we need it for serving audio files
    # shutil.rmtree(session_dir, ignore_errors=True)

# ============ API ROUTES ============

@app.route("/api/auth/login", methods=["POST"])
def login():
    """Authenticate user with hardcoded credentials"""
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    
    if username not in VALID_USERS:
        return jsonify({"error": "Invalid credentials"}), 401
    
    if VALID_USERS[username] != password:
        return jsonify({"error": "Invalid credentials"}), 401
    
    return jsonify({"token": str(uuid.uuid4()), "username": username})

@app.route("/api/library", methods=["GET"])
def get_library():
    """Get all songs in library"""
    return jsonify(library["songs"])

@app.route("/api/library/<song_id>", methods=["GET"])
def get_song(song_id):
    """Get single song"""
    if song_id not in library["songs"]:
        return jsonify({"error": "Not found"}), 404
    return jsonify(library["songs"][song_id])

@app.route("/api/library/<song_id>", methods=["DELETE"])
def delete_song(song_id):
    """Delete song from library"""
    if song_id not in library["songs"]:
        return jsonify({"error": "Not found"}), 404
    del library["songs"][song_id]
    # Remove from all playlists
    for playlist in library["playlists"].values():
        playlist["songs"] = [s for s in playlist["songs"] if s != song_id]
    save_library()
    return jsonify({"ok": True})

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
    """Update playlist metadata"""
    if playlist_id not in library["playlists"]:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    if "name" in data:
        library["playlists"][playlist_id]["name"] = data["name"]
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
def reorder_playlist(playlist_id):
    """Reorder songs in playlist"""
    if playlist_id not in library["playlists"]:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    if "songs" in data:
        library["playlists"][playlist_id]["songs"] = data["songs"]
    save_library()
    return jsonify(library["playlists"][playlist_id])

@app.route("/api/songs", methods=["GET"])
def get_songs():
    """Get default song suggestions"""
    return jsonify({"songs": DEFAULT_SONGS})

@app.route("/api/download", methods=["POST"])
def start_download():
    """Start download job"""
    data = request.json or {}
    songs = data.get("songs", DEFAULT_SONGS)
    if not songs:
        return jsonify({"error": "No songs provided"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "total": len(songs),
        "downloaded": [],
        "failed": [],
        "zip_path": None,
        "cancelled": False,
    }

    thread = threading.Thread(target=run_download_job, args=(job_id, songs), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>", methods=["GET"])
def job_status(job_id):
    """Get job status"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
        "failed_count": len(job.get("failed", [])),
        "failed": job.get("failed", []),
    })

@app.route("/api/download/<job_id>/zip", methods=["GET"])
def download_zip(job_id):
    """Download ZIP file"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": "Not ready yet"}), 400
    zip_path = job.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({"error": "Zip file missing"}), 500
    return send_file(zip_path, as_attachment=True, download_name="MyMusic.zip")

@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    """Cancel download job"""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["cancelled"] = True
    return jsonify({"ok": True})

# ============ AUDIO STREAMING ============
# Build a reverse map of song_id -> file path
def get_song_file_path(song_id):
    """Find the actual MP3 file for a song"""
    # Search through all downloaded files
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        for file in files:
            if file.endswith(".mp3"):
                full_path = os.path.join(root, file)
                # Simple heuristic: check if this is likely our file
                return full_path
    return None

@app.route("/api/audio/<song_id>/play", methods=["GET"])
def stream_audio(song_id):
    """Stream audio file for a song"""
    if song_id not in library["songs"]:
        return jsonify({"error": "Song not found"}), 404
    
    song = library["songs"][song_id]
    
    # For now, find any MP3 in the downloads folder
    # In production, you'd want a better mapping system
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        if files:
            for file in files:
                if file.endswith(".mp3"):
                    full_path = os.path.join(root, file)
                    try:
                        return send_file(
                            full_path,
                            mimetype="audio/mpeg",
                            as_attachment=False,
                            download_name=f"{song['title']}.mp3"
                        )
                    except:
                        continue
    
    return jsonify({"error": "Audio file not found"}), 404

@app.route("/")
def home():
    return send_file("index.html")

if __name__ == "__main__":
    load_library()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)