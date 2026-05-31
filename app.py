from dotenv import load_dotenv

load_dotenv()
import os
import shutil
import threading
import uuid
import time
import json
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp
import psycopg2
from psycopg2.extras import RealDictCursor, DictCursor
from psycopg2.pool import SimpleConnectionPool
from contextlib import contextmanager
from flask import render_template



# Import auth module
from services.auth import  generate_token, verify_token, require_auth, handle_login, handle_signup,handle_verify_email,handle_set_password

# ============ SETUP & CONFIGURATION ============

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "super-secret-dev-key-change-this"
)

CORS(app, 
     resources={r"/api/*": {"origins": "*"}},
     allow_headers=['Content-Type', 'Authorization'],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.before_request
def log_request():
    logger.info(f"[{request.method}] {request.path} from {request.remote_addr}")

@app.after_request
def log_response(response):
    logger.info(f"[{response.status_code}] {request.method} {request.path}")
    return response

# Directory structure
DOWNLOADS_DIR = "downloads"
STORAGE_DIR = "storage"
TEMP_DIR = os.path.join(DOWNLOADS_DIR, "temp")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# ============ DATABASE CONFIGURATION ============

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "database": os.environ.get("DB_NAME", "music_library"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "Dsmushe22!"),
    "port": int(os.environ.get("DB_PORT", 5432))
}

db_pool = None

def init_db_pool():
    """Initialize PostgreSQL connection pool"""
    global db_pool
    try:
        db_pool = SimpleConnectionPool(
            minconn=2,
            maxconn=10,
            connect_timeout=5,
            **DB_CONFIG
        )
        logger.info(f"✓ Database pool initialized: {DB_CONFIG['database']}@{DB_CONFIG['host']}")
        return db_pool
    except Exception as e:
        logger.error(f"✗ Failed to initialize database pool: {e}", exc_info=True)
        raise

@contextmanager
def get_db():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        yield conn
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

def init_schema():
    """Create database schema at startup"""
    from schema import init_schema as schema_init
    
    try:
        with get_db() as conn:
            schema_init(conn)
        logger.info("✓ Schema initialized")
    except Exception as e:
        logger.error(f"✗ Schema initialization failed: {e}", exc_info=True)
        raise

# ============ FFMPEG DEPENDENCY CHECK ============

def check_ffmpeg():
    """Verify ffmpeg is available"""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        logger.error("✗ CRITICAL: ffmpeg not found in PATH")
        raise RuntimeError("ffmpeg is required but not installed")
    logger.info(f"✓ ffmpeg found at: {ffmpeg_path}")
    return ffmpeg_path

# ============ AUTHENTICATION ROUTES ============

@app.route("/api/auth/login", methods=["POST"])
def login():
    """Login with username + password"""
    try:
        data = request.get_json(silent=True) or {}
        token, email, error = handle_login(data, get_db)
        
        if error:
            logger.warning(f"[LOGIN] Failed: {error}")
            return jsonify({"error": error}), 401
        
        logger.info(f"[LOGIN] success: {email}")
        return jsonify({"token": token, "email": email})
    
    except Exception as e:
        logger.error(f"[LOGIN] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/auth/signup", methods=["POST"])
def signup():

    try:
        data = request.get_json(silent=True) or {}

        result, error = handle_signup(data, get_db)

        if error:
            return jsonify({"error": error}), 400

        return jsonify(result)

    except Exception as e:
        logger.error(e, exc_info=True)

        return jsonify({
            "error": "Internal server error"
        }), 500

@app.route("/api/auth/verify-email", methods=["POST"])
def verify_email():

    try:
        data = request.get_json(silent=True) or {}

        result, error = handle_verify_email(data, get_db)

        if error:
            return jsonify({"error": error}), 400

        return jsonify(result)

    except Exception as e:
        logger.error(e, exc_info=True)

        return jsonify({
            "error": "Internal server error"
        }), 500
    
@app.route("/api/auth/set-password", methods=["POST"])
def set_password():

    try:
        data = request.get_json(silent=True) or {}

        result, error = handle_set_password(data, get_db)

        if error:
            return jsonify({"error": error}), 400

        return jsonify(result)

    except Exception as e:
        logger.error(e, exc_info=True)

        return jsonify({
            "error": "Internal server error"
        }), 500

# ============ SONGS ============

DEFAULT_SONGS = [
    "Icarus Bastille", "Pompeii Bastille", "Achilles Come Down Gang of Youths",
    "Glory and Gore Lorde", "Touch the Sky Julie Fowlis", "Centuries Fall Out Boy",
    "I Am the Best 2NE1", "Touch-Tone Telephone Lemon Demon",
    "Cult of Dionysus The Orion Experience", "Abhi Kuch Dino Se Pritam",
    "Chandaniya 2 States", "Chand Si Mehbooba Ho Meri", "Chaudhary Mame Khan",
    "Sawan Mein Lag Gayi Aag Falguni Pathak", "Dhuro Nachyo Abhigya The Artist",
    "Huri Chalyo Prashant",
]

@app.route("/api/songs", methods=["GET"])
def get_songs():
    """Get suggested songs for RIP"""
    try:
        logger.debug("Fetching suggested songs")
        return jsonify({"songs": DEFAULT_SONGS})
    except Exception as e:
        logger.error(f"[GET_SONGS] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ DOWNLOAD ============

def get_audio_duration(file_path):
    """Extract duration from MP3 file"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", 
             "-of", "default=noprint_wrappers=1:nokey=1:noprint_wrappers=1", file_path],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return int(float(result.stdout.strip()))
        return 0
    except Exception as e:
        logger.warning(f"Could not determine duration for {file_path}: {e}")
        return 0

def run_download_job(job_id, user_id, songs, ffmpeg_path):
    """Download songs in background"""
    session_temp_dir = os.path.join(TEMP_DIR, job_id)
    
    try:
        os.makedirs(session_temp_dir, exist_ok=True)
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE download_jobs 
                SET status = %s 
                WHERE job_id = %s
            """, ("running", job_id))
        
        logger.info(f"[DOWNLOAD] Job {job_id} started: {len(songs)} songs")
        
        ydl_opts = {
            "format": "bestaudio/best",
            "default_search": "ytsearch1",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "outtmpl": os.path.join(session_temp_dir, "%(id)s.%(ext)s"),
            "restrictfilenames": False,
            "quiet": False,
            "no_warnings": False,
            "ffmpeg_location": ffmpeg_path,
            "prefer_ffmpeg": True,
            "socket_timeout": 30,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for idx, song_query in enumerate(songs):
                # Check for cancellation
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT cancelled FROM download_jobs WHERE job_id = %s", (job_id,))
                    result = cur.fetchone()
                    if result and result[0]:
                        logger.info(f"[DOWNLOAD] Job {job_id} cancelled")
                        break
                
                # Update current song
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE download_jobs 
                        SET current_song = %s 
                        WHERE job_id = %s
                    """, (song_query, job_id))
                
                logger.info(f"[DOWNLOAD] Downloading: {song_query}")
                
                temp_file = None
                try:
                    info = ydl.extract_info(song_query, download=True)
                    song_title = info.get("title", song_query)
                    source_id = info.get("id")
                    song_id = str(uuid.uuid4())
                    
                    expected_file = os.path.join(session_temp_dir, f"{source_id}.mp3")
                    
                    if os.path.exists(expected_file):
                        temp_file = expected_file
                    else:
                        mp3_files = [f for f in os.listdir(session_temp_dir) if f.endswith('.mp3')]
                        if mp3_files:
                            temp_file = os.path.join(session_temp_dir, 
                                                    max(mp3_files, 
                                                        key=lambda f: os.path.getctime(
                                                            os.path.join(session_temp_dir, f))))
                        else:
                            raise RuntimeError(f"No MP3 files found")
                    
                    if not os.path.exists(temp_file):
                        raise RuntimeError(f"File verification failed")
                    
                    duration = get_audio_duration(temp_file)
                    final_path = os.path.join(STORAGE_DIR, f"{song_id}.mp3")
                    
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO songs 
                            (song_id, user_id, source_id, title, artist, duration_seconds, storage_path, status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """, (song_id, user_id, source_id, song_title, song_query, duration, final_path, "ready"))
                    
                    shutil.move(temp_file, final_path)
                    temp_file = None
                    
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO job_results 
                            (job_id, song_id, song_query, status)
                            VALUES (%s, %s, %s, %s)
                        """, (job_id, song_id, song_query, "success"))
                        
                        cur.execute("""
                            UPDATE download_jobs 
                            SET downloaded_count = downloaded_count + 1 
                            WHERE job_id = %s
                        """, (job_id,))
                    
                    logger.info(f"[DOWNLOAD] ✓ {song_title}")
                
                except Exception as e:
                    logger.error(f"[DOWNLOAD] ✗ {song_query}: {e}")
                    if temp_file and os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass
                    
                    with get_db() as conn:
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO job_results 
                            (job_id, song_query, status, error_message)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (job_id, song_query) DO UPDATE 
                            SET status = 'failed', error_message = %s
                        """, (job_id, song_query, "failed", str(e), str(e)))
                        
                        cur.execute("""
                            UPDATE download_jobs 
                            SET failed_count = failed_count + 1 
                            WHERE job_id = %s
                        """, (job_id,))
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE download_jobs 
                SET status = %s, completed_at = CURRENT_TIMESTAMP 
                WHERE job_id = %s
            """, ("done", job_id))
        
        logger.info(f"[DOWNLOAD] Job {job_id} completed")
    
    except Exception as e:
        logger.error(f"[DOWNLOAD] Job {job_id} fatal error: {e}", exc_info=True)
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE download_jobs 
                SET status = %s, error_message = %s, completed_at = CURRENT_TIMESTAMP 
                WHERE job_id = %s
            """, ("error", str(e), job_id))
    
    finally:
        try:
            shutil.rmtree(session_temp_dir, ignore_errors=True)
        except:
            pass

@app.route("/api/download", methods=["POST"])
@require_auth
def start_download():
    """Start download job"""
    try:
        user_id = request.user_id
        data = request.get_json(silent=True) or {}
        songs = data.get("songs", [])
        
        if not isinstance(songs, list) or len(songs) == 0:
            return jsonify({"error": "Invalid songs list"}), 400
        
        job_id = str(uuid.uuid4())
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO download_jobs (job_id, user_id, total_songs, status)
                VALUES (%s, %s, %s, %s)
            """, (job_id, user_id, len(songs), "queued"))
        
        thread = threading.Thread(
            target=run_download_job,
            args=(job_id, user_id, songs, ffmpeg_path),
            daemon=True
        )
        thread.start()
        
        return jsonify({"job_id": job_id})
    
    except Exception as e:
        logger.error(f"[DOWNLOAD] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/status/<job_id>", methods=["GET"])
@require_auth
def job_status(job_id):
    """Get job status"""
    try:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            cur.execute("""
                SELECT * FROM download_jobs WHERE job_id = %s
            """, (job_id,))
            job = cur.fetchone()
            
            if not job:
                return jsonify({"error": "Job not found"}), 404
            
            cur.execute("""
                SELECT song_id, song_query, status, error_message 
                FROM job_results 
                WHERE job_id = %s
            """, (job_id,))
            results = cur.fetchall()
        
        downloaded = [r["song_query"] for r in results if r["status"] == "success"]
        failed = [{"song": r["song_query"], "error": r["error_message"]}
                  for r in results if r["status"] == "failed"]
        
        return jsonify({
            "status": job["status"],
            "progress": job["downloaded_count"],
            "total": job["total_songs"],
            "current": job["current_song"] or "",
            "failed_count": len(failed),
            "failed": failed,
            "downloaded": downloaded,
        })
    
    except Exception as e:
        logger.error(f"[STATUS] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ LIBRARY ============

@app.route("/api/library", methods=["GET"])
@require_auth
def get_library():
    """Get user's library"""
    try:
        user_id = request.user_id
        
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT song_id, title, artist, duration_seconds, status
                FROM songs 
                WHERE user_id = %s AND status = 'ready'
                ORDER BY created_at DESC
            """, (user_id,))
            songs = cur.fetchall()
        
        library_dict = {}
        for song in songs:
            song_dict = dict(song)
            song_dict["id"] = song_dict["song_id"]
            library_dict[song["song_id"]] = song_dict
        
        return jsonify(library_dict)
    
    except Exception as e:
        logger.error(f"[GET_LIBRARY] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/library/<song_id>", methods=["DELETE"])
@require_auth
def delete_song(song_id):
    """Delete song"""
    try:
        user_id = request.user_id
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE songs 
                SET status = 'deleted', updated_at = CURRENT_TIMESTAMP
                WHERE song_id = %s::UUID AND user_id = %s
            """, (song_id, user_id))
        
        logger.info(f"[DELETE_SONG] {song_id}")
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[DELETE_SONG] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ AUDIO STREAMING ============

@app.route("/api/audio/<song_id>/stream", methods=["GET"])
@require_auth
def stream_audio(song_id):
    """Stream MP3 audio"""
    try:
        user_id = request.user_id
        
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT storage_path FROM songs 
                WHERE song_id = %s::UUID AND user_id = %s AND status = 'ready'
            """, (song_id, user_id))
            song = cur.fetchone()
        
        if not song:
            return jsonify({"error": "Not found"}), 404
        
        storage_path = song["storage_path"]
        if not os.path.exists(storage_path):
            logger.error(f"[STREAM] File missing: {storage_path}")
            return jsonify({"error": "File not found"}), 404
        
        response = send_file(storage_path, mimetype='audio/mpeg', conditional=True, as_attachment=False)
        response.headers['Accept-Ranges'] = 'bytes'
        return response
    
    except Exception as e:
        logger.error(f"[STREAM] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ PLAYLISTS ============

@app.route("/api/playlists", methods=["GET"])
@require_auth
def get_playlists():
    """Get user's playlists"""
    try:
        user_id = request.user_id
        
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT playlist_id, name, created_at, updated_at
                FROM playlists
                WHERE user_id = %s
                ORDER BY updated_at DESC
            """, (user_id,))
            playlists = cur.fetchall()
            
            playlists_dict = {}
            for p in playlists:
                cur.execute("""
                    SELECT song_id
                    FROM playlist_songs
                    WHERE playlist_id = %s
                    ORDER BY position ASC
                """, (p["playlist_id"],))
                songs = [row["song_id"] for row in cur.fetchall()]
                
                p_dict = dict(p)
                p_dict["id"] = p_dict["playlist_id"]
                p_dict["songs"] = songs
                playlists_dict[p["playlist_id"]] = p_dict
        
        return jsonify(playlists_dict)
    
    except Exception as e:
        logger.error(f"[GET_PLAYLISTS] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists", methods=["POST"])
@require_auth
def create_playlist():
    """Create playlist"""
    try:
        user_id = request.user_id
        data = request.get_json(silent=True) or {}
        name = data.get("name", "Untitled")
        playlist_id = str(uuid.uuid4())
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO playlists (playlist_id, user_id, name)
                VALUES (%s, %s, %s)
            """, (playlist_id, user_id, name))
        
        return jsonify({
            "id": playlist_id,
            "playlist_id": playlist_id,
            "name": name,
            "songs": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        })
    
    except Exception as e:
        logger.error(f"[CREATE_PLAYLIST] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>", methods=["DELETE"])
@require_auth
def delete_playlist(playlist_id):
    """Delete playlist"""
    try:
        user_id = request.user_id
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM playlists 
                WHERE playlist_id = %s::UUID AND user_id = %s
            """, (playlist_id, user_id))
        
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[DELETE_PLAYLIST] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>/songs/<song_id>", methods=["POST"])
@require_auth
def add_song_to_playlist(playlist_id, song_id):
    """Add song to playlist"""
    try:
        user_id = request.user_id
        
        with get_db() as conn:
            cur = conn.cursor()
            
            # Verify user owns playlist
            cur.execute("""
                SELECT playlist_id FROM playlists 
                WHERE playlist_id = %s::UUID AND user_id = %s
            """, (playlist_id, user_id))
            if not cur.fetchone():
                return jsonify({"error": "Not found"}), 404
            
            # Get max position
            cur.execute("""
                SELECT MAX(position) FROM playlist_songs WHERE playlist_id = %s::UUID
            """, (playlist_id,))
            result = cur.fetchone()
            next_position = (result[0] or -1) + 1
            
            cur.execute("""
                INSERT INTO playlist_songs (playlist_id, song_id, position)
                VALUES (%s::UUID, %s::UUID, %s)
                ON CONFLICT (playlist_id, song_id) DO NOTHING
            """, (playlist_id, song_id, next_position))
        
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[ADD_TO_PLAYLIST] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>/songs/<song_id>", methods=["DELETE"])
@require_auth
def remove_song_from_playlist(playlist_id, song_id):
    """Remove song from playlist"""
    try:
        user_id = request.user_id
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM playlist_songs 
                WHERE playlist_id = %s::UUID AND song_id = %s::UUID
            """, (playlist_id, song_id))
        
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[REMOVE_FROM_PLAYLIST] {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ STATIC FILES ============

@app.route("/")
def home():
    """Serve landing page"""
    return render_template("landing.html")

@app.route("/app")
def app_page():
    """Serve app page"""
    return render_template("index.html")
# ============ STARTUP ============

if __name__ == "__main__":
    try:
        logger.info("=" * 60)
        logger.info("Starting WAVEFORM-3000 PRO Server")
        logger.info("=" * 60)
        
        ffmpeg_path = check_ffmpeg()
        init_db_pool()
        # init_schema()
        
        logger.info("✓ Server initialization complete")
        logger.info("=" * 60)
        
        port = int(os.environ.get("PORT", 5000))
        logger.info(f"Listening on 0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, debug=False)
    
    except Exception as e:
        logger.error(f"✗ FATAL: {e}", exc_info=True)
        raise

if __name__ == "main":
    app.run(host="0.0.0.0", port=5000)
