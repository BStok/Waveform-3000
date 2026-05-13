import os
import shutil
import zipfile
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

# ============ SETUP & CONFIGURATION ============

app = Flask(__name__)

# BETTER CORS SETUP
CORS(app, 
     resources={r"/api/*": {"origins": "*"}},
     allow_headers=['Content-Type', 'Authorization'],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'])

# Setup structured logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# REQUEST LOGGING MIDDLEWARE
@app.before_request
def log_request():
    logger.info(f"[{request.method}] {request.path} from {request.remote_addr}")

@app.after_request
def log_response(response):
    logger.info(f"[{response.status_code}] {request.method} {request.path}")
    return response

# Directory structure
DOWNLOADS_DIR = "downloads"
STORAGE_DIR = "storage"  # Immutable, write-only storage for final files
TEMP_DIR = os.path.join(DOWNLOADS_DIR, "temp")  # Temporary scratch space

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Hardcoded user credentials
VALID_USERS = {
    "Sanya": "123456789",
    "Demo": "1234"
}

# ============ DATABASE CONFIGURATION ============

# PostgreSQL connection pool
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "database": os.environ.get("DB_NAME", "music_library"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "Dsmushe22!"),
    "port": int(os.environ.get("DB_PORT", 5432))
}

db_pool = None

def init_db_pool():
    """Initialize PostgreSQL connection pool with timeout"""
    global db_pool
    try:
        db_pool = SimpleConnectionPool(
            minconn=2,
            maxconn=10,
            connect_timeout=5,  # 5s connect timeout (FIX #3)
            **DB_CONFIG
        )
        logger.info(f"Database pool initialized: {DB_CONFIG['database']}@{DB_CONFIG['host']}")
        return db_pool
    except Exception as e:
        logger.error(f"Failed to initialize database pool: {e}", exc_info=True)
        raise

@contextmanager
def get_db():
    """Get database connection from pool (with timeout fallback) (FIX #3)"""
    conn = None
    try:
        conn = db_pool.getconn()
        yield conn
        conn.commit()
    except psycopg2.pool.PoolError as e:
        logger.error(f"Connection pool exhausted: {e}")
        if conn:
            conn.rollback()
        raise RuntimeError("Database connection pool exhausted. Request rejected.")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Database transaction error: {e}", exc_info=True)
        raise
    finally:
        if conn:
            db_pool.putconn(conn)

def init_schema():
    """Create database schema at startup"""
    with get_db() as conn:
        cur = conn.cursor()
        
        # Songs table: immutable record with deterministic storage_path
        cur.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                song_id UUID PRIMARY KEY,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                duration_seconds INT DEFAULT 0,
                storage_path TEXT,
                status TEXT NOT NULL DEFAULT 'downloading',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT
            );
        """)
        
        # Playlists table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                playlist_id UUID PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Playlist songs: maintains order
        cur.execute("""
            CREATE TABLE IF NOT EXISTS playlist_songs (
                playlist_id UUID NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
                song_id UUID NOT NULL REFERENCES songs(song_id) ON DELETE CASCADE,
                position INT NOT NULL,
                PRIMARY KEY (playlist_id, song_id)
            );
        """)
        
        # Download jobs table: tracks async download state
        cur.execute("""
            CREATE TABLE IF NOT EXISTS download_jobs (
                job_id UUID PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'queued',
                total_songs INT NOT NULL,
                downloaded_count INT DEFAULT 0,
                failed_count INT DEFAULT 0,
                current_song TEXT DEFAULT '',
                cancelled BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                error_message TEXT
            );
        """)
        
        # Job results: track success/failure per song in job
        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_results (
                job_id UUID NOT NULL REFERENCES download_jobs(job_id) ON DELETE CASCADE,
                song_id UUID,
                song_query TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                PRIMARY KEY (job_id, song_query)
            );
        """)
        
        conn.commit()
        logger.info("Database schema initialized")

# ============ CONCURRENCY & STATE MANAGEMENT ============

# Global lock for concurrent operations
state_lock = threading.Lock()

# ============ VALIDATION ============

class ValidationError(Exception):
    pass

def validate_songs_input(songs):
    """Validate song list input"""
    if not isinstance(songs, list):
        raise ValidationError("Songs must be a list")
    
    if len(songs) == 0:
        raise ValidationError("No songs provided")
    
    if len(songs) > 50:
        raise ValidationError("Maximum 50 songs per request")
    
    validated = []
    for song in songs:
        if not isinstance(song, str):
            raise ValidationError(f"Song must be string, got {type(song).__name__}")
        
        song_clean = song.strip()
        if not song_clean:
            continue  # Skip empty strings
        
        if len(song_clean) > 200:
            raise ValidationError(f"Song query too long (max 200 chars): {song_clean[:50]}...")
        
        validated.append(song_clean)
    
    if not validated:
        raise ValidationError("No valid songs after filtering")
    
    return validated

# ============ FFMPEG DEPENDENCY CHECK ============

def check_ffmpeg():
    """Verify ffmpeg is available at startup"""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        logger.error("CRITICAL: ffmpeg not found in PATH. Cannot start server.")
        raise RuntimeError(
            "ffmpeg is required but not installed. "
            "Install it and ensure it's in PATH, then restart."
        )
    logger.info(f"✓ ffmpeg found at: {ffmpeg_path}")
    return ffmpeg_path

# ============ DOWNLOAD LOGIC ============

DEFAULT_SONGS = [
    "Icarus Bastille", "Pompeii Bastille", "Achilles Come Down Gang of Youths",
    "Glory and Gore Lorde", "Touch the Sky Julie Fowlis", "Centuries Fall Out Boy",
    "I Am the Best 2NE1", "Touch-Tone Telephone Lemon Demon",
    "Cult of Dionysus The Orion Experience", "Abhi Kuch Dino Se Pritam",
    "Chandaniya 2 States", "Chand Si Mehbooba Ho Meri", "Chaudhary Mame Khan",
    "Sawan Mein Lag Gayi Aag Falguni Pathak", "Dhuro Nachyo Abhigya The Artist",
    "Huri Chalyo Prashant",
]

def get_audio_duration(file_path):
    """Extract duration from MP3 file using ffprobe"""
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

def run_download_job(job_id, songs, ffmpeg_path):
    """
    Download songs with strict database-driven state machine:
    1. Get duration BEFORE any DB writes (can fail safely)
    2. Insert DB entry + storage path FIRST (atomic transaction)
    3. Move file from TEMP to STORAGE AFTER DB succeeds (FIX #5)
    4. All failures recorded in DB, no filesystem inference
    """
    session_temp_dir = os.path.join(TEMP_DIR, job_id)
    
    try:
        os.makedirs(session_temp_dir, exist_ok=True)
        
        # Update job to running in DB
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
            "restrictfilenames": False,  # Disable to avoid Windows path issues
            "quiet": False,  # Show output for debugging
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
                        logger.info(f"[DOWNLOAD] Job {job_id} cancelled by user")
                        break
                
                # Update current song in DB
                with get_db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE download_jobs 
                        SET current_song = %s 
                        WHERE job_id = %s
                    """, (song_query, job_id))
                
                logger.info(f"[DOWNLOAD] Downloading: {song_query}")
                
                temp_file = None  # Track for cleanup
                try:
                    # Extract info and download
                    info = ydl.extract_info(song_query, download=True)
                    song_title = info.get("title", song_query)
                    source_id = info.get("id")  # YouTube video ID
                    song_id = str(uuid.uuid4())
                    
                    # FIX #2: Find actual downloaded file (Windows path handling)
                    # yt-dlp saves as %(id)s.%(ext)s per outtmpl
                    expected_file = os.path.join(session_temp_dir, f"{source_id}.mp3")
                    
                    if os.path.exists(expected_file):
                        temp_file = expected_file
                    else:
                        # Fallback: search for any .mp3 file in session directory
                        # (Windows path/filename encoding can cause mismatches)
                        try:
                            mp3_files = [f for f in os.listdir(session_temp_dir) if f.endswith('.mp3')]
                            if mp3_files:
                                # Use the most recently modified file (safest bet)
                                temp_file = os.path.join(session_temp_dir, 
                                                        max(mp3_files, 
                                                            key=lambda f: os.path.getctime(
                                                                os.path.join(session_temp_dir, f))))
                                logger.warning(f"[DOWNLOAD] File mismatch - expected {source_id}.mp3, "
                                             f"found {os.path.basename(temp_file)}")
                            else:
                                raise RuntimeError(f"No MP3 files found in {session_temp_dir}")
                        except Exception as e:
                            logger.error(f"[DOWNLOAD] Failed to search directory: {e}")
                            raise RuntimeError(f"Could not find downloaded file for: {song_query}")
                    
                    if not os.path.exists(temp_file):
                        raise RuntimeError(f"File verification failed: {temp_file}")
                    
                    # FIX #5: Get duration BEFORE any DB writes (can fail safely)
                    duration = get_audio_duration(temp_file)
                    
                    # FIX #5: Deterministic storage path
                    final_path = os.path.join(STORAGE_DIR, f"{song_id}.mp3")
                    
                    # FIX #5: Create DB entry + insert in SAME atomic transaction
                    # Only commit if move succeeds
                    with get_db() as conn:
                        cur = conn.cursor()
                        
                        # Insert with ready status BEFORE moving file
                        cur.execute("""
                            INSERT INTO songs 
                            (song_id, source_id, title, artist, duration_seconds, storage_path, status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (song_id, source_id, song_title, song_query, duration, final_path, "ready"))
                    
                    # FIX #5: Only MOVE FILE after transaction succeeds
                    shutil.move(temp_file, final_path)
                    temp_file = None  # Mark as moved
                    
                    # Record success AFTER file is safely moved
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
                    
                    logger.info(f"[DOWNLOAD] ✓ {song_title} ({duration}s) -> {final_path}")
                
                except Exception as e:
                    logger.error(f"[DOWNLOAD] ✗ {song_query}: {type(e).__name__}: {str(e)}", exc_info=True)
                    
                    # Clean up temp file if not moved
                    if temp_file and os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass
                    
                    # Record failure ONLY (don't insert into songs)
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
        
        # Mark job as done (FIX #1: don't delete, just mark as done)
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE download_jobs 
                SET status = %s, completed_at = CURRENT_TIMESTAMP 
                WHERE job_id = %s
            """, ("done", job_id))
        
        logger.info(f"[DOWNLOAD] Job {job_id} completed")
    
    except Exception as e:
        logger.error(f"[DOWNLOAD] Job {job_id} fatal error: {type(e).__name__}: {str(e)}", exc_info=True)
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE download_jobs 
                SET status = %s, error_message = %s, completed_at = CURRENT_TIMESTAMP 
                WHERE job_id = %s
            """, ("error", str(e), job_id))
    
    finally:
        # Clean up session temp directory
        try:
            shutil.rmtree(session_temp_dir, ignore_errors=True)
            logger.debug(f"Cleaned up session directory: {session_temp_dir}")
        except Exception as e:
            logger.warning(f"Failed to cleanup {session_temp_dir}: {e}")

def cleanup_expired_jobs():
    """Periodically mark old jobs as expired (don't delete) (FIX #1)"""
    while True:
        try:
            time.sleep(30 * 60)  # Every 30 minutes
            
            with get_db() as conn:
                cur = conn.cursor()
                
                # Don't DELETE - just mark as expired
                cur.execute("""
                    UPDATE download_jobs 
                    SET status = 'expired'
                    WHERE status = 'done' 
                    AND completed_at < CURRENT_TIMESTAMP - INTERVAL '30 minutes'
                """)
                
                updated = cur.rowcount
                if updated > 0:
                    logger.info(f"Marked {updated} jobs as expired")
        
        except Exception as e:
            logger.error(f"Job cleanup error: {e}")

def cleanup_deleted_files():
    """Periodically delete files marked as deleted (FIX #4)"""
    while True:
        try:
            time.sleep(60 * 60)  # Every hour
            
            with get_db() as conn:
                cur = conn.cursor(cursor_factory=RealDictCursor)
                
                # Get deleted songs with files that are old enough
                cur.execute("""
                    SELECT song_id, storage_path FROM songs 
                    WHERE status = 'deleted' 
                    AND updated_at < CURRENT_TIMESTAMP - INTERVAL '1 hour'
                """)
                deleted_songs = cur.fetchall()
                
                for song in deleted_songs:
                    # Delete file
                    if song["storage_path"] and os.path.exists(song["storage_path"]):
                        try:
                            os.remove(song["storage_path"])
                            logger.info(f"Cleaned up file: {song['storage_path']}")
                        except Exception as e:
                            logger.warning(f"Failed to delete file: {e}")
                    
                    # Remove DB entry
                    cur.execute("DELETE FROM songs WHERE song_id = %s::UUID", (song["song_id"],))
                
                conn.commit()
                if deleted_songs:
                    logger.info(f"Cleaned up {len(deleted_songs)} deleted songs")
        
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# Start cleanup threads (daemon)
cleanup_thread = threading.Thread(target=cleanup_expired_jobs, daemon=True)
cleanup_thread.start()

cleanup_thread_2 = threading.Thread(target=cleanup_deleted_files, daemon=True)
cleanup_thread_2.start()

# ============ AUTHENTICATION ============

@app.route("/api/auth/login", methods=["POST", "OPTIONS"])
def login():
    """Authenticate user with hardcoded credentials"""
    if request.method == "OPTIONS":
        return jsonify({"ok": True})
    
    try:
        data = request.get_json(silent=True) or {}
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        
        logger.info(f"[LOGIN] Attempt: username='{username}'")
        
        if username not in VALID_USERS or VALID_USERS[username] != password:
            logger.warning(f"[LOGIN] Failed for '{username}'")
            return jsonify({"error": "Invalid credentials"}), 401
        
        logger.info(f"[LOGIN] Success for '{username}'")
        return jsonify({"token": str(uuid.uuid4()), "username": username})
    
    except Exception as e:
        logger.error(f"[LOGIN] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ DOWNLOAD ROUTES ============

@app.route("/api/songs", methods=["GET"])
def get_songs():
    """Get suggested songs for RIP"""
    try:
        logger.debug("Fetching suggested songs list")
        return jsonify({"songs": DEFAULT_SONGS})
    except Exception as e:
        logger.error(f"[GET_SONGS] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/download", methods=["POST"])
def start_download():
    """Start download job with database-driven state"""
    try:
        data = request.get_json(silent=True) or {}
        songs_input = data.get("songs", [])
        
        # Validate input
        try:
            songs = validate_songs_input(songs_input)
        except ValidationError as e:
            logger.warning(f"[DOWNLOAD] Validation failed: {e}")
            return jsonify({"error": str(e)}), 400
        
        job_id = str(uuid.uuid4())
        
        # Create job in database
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO download_jobs (job_id, total_songs, status)
                VALUES (%s, %s, %s)
            """, (job_id, len(songs), "queued"))
        
        logger.info(f"[DOWNLOAD] Created job {job_id}: {len(songs)} songs")
        
        # Start download in background thread
        thread = threading.Thread(
            target=run_download_job,
            args=(job_id, songs, ffmpeg_path),
            daemon=True,
            name=f"download-{job_id}"
        )
        thread.start()
        
        return jsonify({"job_id": job_id})
    
    except Exception as e:
        logger.error(f"[DOWNLOAD] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/status/<job_id>", methods=["GET"])
def job_status(job_id):
    """Get job status and results from database (FIX #1)"""
    try:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # Get job
            cur.execute("""
                SELECT * FROM download_jobs WHERE job_id = %s
            """, (job_id,))
            job = cur.fetchone()
            
            if not job:
                logger.warning(f"[STATUS] Job not found: {job_id}")
                return jsonify({"error": "Job not found or expired"}), 404
            
            # Get job results
            cur.execute("""
                SELECT song_id, song_query, status, error_message 
                FROM job_results 
                WHERE job_id = %s 
                ORDER BY song_query
            """, (job_id,))
            results = cur.fetchall()
        
        # Separate successes and failures
        downloaded = [r["song_query"] for r in results if r["status"] == "success"]
        failed = [
            {"song": r["song_query"], "error": r["error_message"]}
            for r in results if r["status"] == "failed"
        ]
        
        # FIX #1: Allow returning status for expired jobs too
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
        logger.error(f"[STATUS] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    """Cancel download job (sets DB flag)"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            # Check if job exists
            cur.execute("SELECT job_id FROM download_jobs WHERE job_id = %s", (job_id,))
            if not cur.fetchone():
                logger.warning(f"[CANCEL] Job not found: {job_id}")
                return jsonify({"error": "Job not found"}), 404
            
            # Set cancelled flag
            cur.execute("""
                UPDATE download_jobs SET cancelled = TRUE WHERE job_id = %s
            """, (job_id,))
        
        logger.info(f"[CANCEL] Cancelled job {job_id}")
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[CANCEL] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ LIBRARY ROUTES ============

@app.route("/api/library", methods=["GET"])
def get_library():
    """Get all songs in library from database"""
    try:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT song_id, title, artist, duration_seconds, status
                FROM songs 
                WHERE status = 'ready'
                ORDER BY created_at DESC
            """)
            songs = cur.fetchall()
        
        # Convert to dict keyed by song_id, add id field for frontend compatibility
        library_dict = {}
        for song in songs:
            song_dict = dict(song)
            song_dict["id"] = song_dict["song_id"]
            library_dict[song["song_id"]] = song_dict
        logger.debug(f"Retrieved library: {len(library_dict)} songs")
        return jsonify(library_dict)
    
    except Exception as e:
        logger.error(f"[GET_LIBRARY] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/library/<song_id>", methods=["GET"])
def get_song(song_id):
    """Get single song metadata from database (FIX #6: filter by status)"""
    try:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT song_id, title, artist, duration_seconds, status, source_id
                FROM songs 
                WHERE song_id = %s::UUID AND status = 'ready'
            """, (song_id,))
            song = cur.fetchone()
        
        if not song:
            logger.warning(f"[GET_SONG] Not found: {song_id}")
            return jsonify({"error": "Not found"}), 404
        
        song_dict = dict(song)
        song_dict["id"] = song_dict["song_id"]
        return jsonify(song_dict)
    
    except Exception as e:
        logger.error(f"[GET_SONG] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/library/<song_id>", methods=["PUT"])
def update_song(song_id):
    """Update song metadata in database"""
    try:
        data = request.get_json(silent=True) or {}
        
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # Check exists
            cur.execute("SELECT song_id FROM songs WHERE song_id = %s::UUID", (song_id,))
            if not cur.fetchone():
                logger.warning(f"[UPDATE_SONG] Not found: {song_id}")
                return jsonify({"error": "Not found"}), 404
            
            # Update only allowed fields
            updates = []
            params = []
            if "title" in data:
                updates.append("title = %s")
                params.append(data["title"])
            if "artist" in data:
                updates.append("artist = %s")
                params.append(data["artist"])
            
            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(song_id)
                
                cur.execute(f"""
                    UPDATE songs 
                    SET {", ".join(updates)}
                    WHERE song_id = %s::UUID
                """, params)
            
            # Fetch updated song
            cur.execute("""
                SELECT song_id, title, artist, duration_seconds, status
                FROM songs 
                WHERE song_id = %s::UUID
            """, (song_id,))
            song = cur.fetchone()
        
        logger.info(f"[UPDATE_SONG] Updated {song_id}")
        return jsonify(dict(song))
    
    except Exception as e:
        logger.error(f"[UPDATE_SONG] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/library/<song_id>", methods=["DELETE"])
def delete_song(song_id):
    """Soft-delete song from library (FIX #4: don't delete immediately)"""
    try:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # Check exists
            cur.execute("""
                SELECT song_id FROM songs WHERE song_id = %s::UUID
            """, (song_id,))
            
            if not cur.fetchone():
                logger.warning(f"[DELETE_SONG] Not found: {song_id}")
                return jsonify({"error": "Not found"}), 404
            
            # FIX #4: Mark as deleted instead of deleting immediately
            cur.execute("""
                UPDATE songs 
                SET status = 'deleted', updated_at = CURRENT_TIMESTAMP
                WHERE song_id = %s::UUID
            """, (song_id,))
        
        logger.info(f"[DELETE_SONG] Marked deleted: {song_id}")
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[DELETE_SONG] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ AUDIO STREAMING ============

@app.route("/api/audio/<song_id>/stream", methods=["GET"])
def stream_audio(song_id):
    """
    Stream MP3 file using database lookup (FIX #4: only stream ready songs).
    Query DB for song_id → get storage_path → stream.
    No filesystem guessing or inference.
    """
    try:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT storage_path FROM songs 
                WHERE song_id = %s::UUID AND status = 'ready'
            """, (song_id,))
            song = cur.fetchone()
        
        if not song:
            logger.warning(f"[STREAM] Song not found or not ready: {song_id}")
            return jsonify({"error": "Not found"}), 404
        
        storage_path = song["storage_path"]
        
        # Sanity check: file must exist
        if not os.path.exists(storage_path):
            logger.error(f"[STREAM] File missing at DB path: {storage_path}")
            return jsonify({"error": "File not found"}), 404
        
        logger.debug(f"[STREAM] Streaming {song_id} from {storage_path}")
        
        # Stream with range support
        response = send_file(
            storage_path,
            mimetype='audio/mpeg',
            conditional=True,
            as_attachment=False
        )
        response.headers['Accept-Ranges'] = 'bytes'
        return response
    
    except Exception as e:
        logger.error(f"[STREAM] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ PLAYLIST ROUTES ============

@app.route("/api/playlists", methods=["GET"])
def get_playlists():
    """Get all playlists from database"""
    try:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT playlist_id, name, created_at, updated_at 
                FROM playlists 
                ORDER BY updated_at DESC
            """)
            playlists = cur.fetchall()
        
        playlists_dict = {}
        for p in playlists:
            p_dict = dict(p)
            p_dict["id"] = p_dict["playlist_id"]
            p_dict["songs"] = state.playlists.get(p["playlist_id"], {}).get("songs", []) if "state" in globals() else []
            playlists_dict[p["playlist_id"]] = p_dict
        
        logger.debug(f"Retrieved playlists: {len(playlists_dict)}")
        return jsonify(playlists_dict)
    
    except Exception as e:
        logger.error(f"[GET_PLAYLISTS] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists", methods=["POST"])
def create_playlist():
    """Create new playlist in database"""
    try:
        data = request.get_json(silent=True) or {}
        name = data.get("name", "Untitled")
        playlist_id = str(uuid.uuid4())
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO playlists (playlist_id, name)
                VALUES (%s, %s)
            """, (playlist_id, name))
        
        logger.info(f"[CREATE_PLAYLIST] Created {playlist_id}: '{name}'")
        return jsonify({
            "id": playlist_id,
            "playlist_id": playlist_id,
            "name": name,
            "songs": [],
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        })
    
    except Exception as e:
        logger.error(f"[CREATE_PLAYLIST] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>", methods=["GET"])
def get_playlist(playlist_id):
    """Get playlist with songs from database (FIX #6: filter by status)"""
    try:
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # Get playlist
            cur.execute("""
                SELECT playlist_id, name, created_at, updated_at 
                FROM playlists 
                WHERE playlist_id = %s::UUID
            """, (playlist_id,))
            playlist = cur.fetchone()
            
            if not playlist:
                logger.warning(f"[GET_PLAYLIST] Not found: {playlist_id}")
                return jsonify({"error": "Not found"}), 404
            
            # FIX #6: Only include ready songs in playlist
            cur.execute("""
                SELECT s.song_id, s.title, s.artist, s.duration_seconds, s.status
                FROM playlist_songs ps
                JOIN songs s ON ps.song_id = s.song_id
                WHERE ps.playlist_id = %s::UUID AND s.status = 'ready'
                ORDER BY ps.position ASC
            """, (playlist_id,))
            songs = cur.fetchall()
        
        playlist_dict = dict(playlist)
        playlist_dict["id"] = playlist_dict["playlist_id"]
        playlist_dict["songs"] = []
        for s in songs:
            song_dict = dict(s)
            song_dict["id"] = song_dict["song_id"]
            playlist_dict["songs"].append(song_dict)
        
        return jsonify(playlist_dict)
    
    except Exception as e:
        logger.error(f"[GET_PLAYLIST] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>", methods=["PUT"])
def update_playlist(playlist_id):
    """Update playlist metadata or songs in database"""
    try:
        data = request.get_json(silent=True) or {}
        
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # Check exists
            cur.execute("SELECT playlist_id FROM playlists WHERE playlist_id = %s::UUID", (playlist_id,))
            if not cur.fetchone():
                logger.warning(f"[UPDATE_PLAYLIST] Not found: {playlist_id}")
                return jsonify({"error": "Not found"}), 404
            
            # Update name if provided
            if "name" in data:
                cur.execute("""
                    UPDATE playlists 
                    SET name = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE playlist_id = %s::UUID
                """, (data["name"], playlist_id))
            
            # Update songs if provided (reorder)
            if "songs" in data:
                song_ids = data["songs"]
                
                # Clear existing
                cur.execute("DELETE FROM playlist_songs WHERE playlist_id = %s::UUID", (playlist_id,))
                
                # Insert with positions
                for position, song_id in enumerate(song_ids):
                    cur.execute("""
                        INSERT INTO playlist_songs (playlist_id, song_id, position)
                        VALUES (%s::UUID, %s::UUID, %s)
                    """, (playlist_id, song_id, position))
                
                cur.execute("""
                    UPDATE playlists 
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE playlist_id = %s::UUID
                """, (playlist_id,))
            
            # Fetch updated
            cur.execute("""
                SELECT playlist_id, name, created_at, updated_at 
                FROM playlists 
                WHERE playlist_id = %s::UUID
            """, (playlist_id,))
            playlist = cur.fetchone()
        
        playlist_dict = dict(playlist)
        playlist_dict["id"] = playlist_dict["playlist_id"]
        logger.info(f"[UPDATE_PLAYLIST] Updated {playlist_id}")
        return jsonify(playlist_dict)
    
    except Exception as e:
        logger.error(f"[UPDATE_PLAYLIST] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>", methods=["DELETE"])
def delete_playlist(playlist_id):
    """Delete playlist from database"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            # Check exists
            cur.execute("SELECT playlist_id FROM playlists WHERE playlist_id = %s::UUID", (playlist_id,))
            if not cur.fetchone():
                logger.warning(f"[DELETE_PLAYLIST] Not found: {playlist_id}")
                return jsonify({"error": "Not found"}), 404
            
            # Delete cascades to playlist_songs via foreign key
            cur.execute("DELETE FROM playlists WHERE playlist_id = %s::UUID", (playlist_id,))
        
        logger.info(f"[DELETE_PLAYLIST] Deleted {playlist_id}")
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[DELETE_PLAYLIST] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>/songs/<song_id>", methods=["POST"])
def add_song_to_playlist(playlist_id, song_id):
    """Add song to playlist (append to end) (FIX #7: check exists first)"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            # Check both exist
            cur.execute("""
                SELECT 1 FROM playlists WHERE playlist_id = %s::UUID
            """, (playlist_id,))
            if not cur.fetchone():
                logger.warning(f"[ADD_TO_PLAYLIST] Playlist not found: {playlist_id}")
                return jsonify({"error": "Not found"}), 404
            
            cur.execute("""
                SELECT 1 FROM songs WHERE song_id = %s::UUID AND status = 'ready'
            """, (song_id,))
            if not cur.fetchone():
                logger.warning(f"[ADD_TO_PLAYLIST] Song not found: {song_id}")
                return jsonify({"error": "Not found"}), 404
            
            # FIX #7: Check if already exists (idempotent)
            cur.execute("""
                SELECT 1 FROM playlist_songs 
                WHERE playlist_id = %s::UUID AND song_id = %s::UUID
            """, (playlist_id, song_id))
            if cur.fetchone():
                logger.warning(f"[ADD_TO_PLAYLIST] Already exists: {song_id} in {playlist_id}")
                return jsonify({"ok": True})  # Idempotent
            
            # Get max position
            cur.execute("""
                SELECT MAX(position) FROM playlist_songs WHERE playlist_id = %s::UUID
            """, (playlist_id,))
            result = cur.fetchone()
            next_position = (result[0] or -1) + 1
            
            # FIX #7: Explicit constraint name
            cur.execute("""
                INSERT INTO playlist_songs (playlist_id, song_id, position)
                VALUES (%s::UUID, %s::UUID, %s)
                ON CONFLICT (playlist_id, song_id) DO NOTHING
            """, (playlist_id, song_id, next_position))
        
        logger.info(f"[ADD_TO_PLAYLIST] Added {song_id} to {playlist_id}")
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[ADD_TO_PLAYLIST] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>/songs/<song_id>", methods=["DELETE"])
def remove_song_from_playlist(playlist_id, song_id):
    """Remove song from playlist"""
    try:
        with get_db() as conn:
            cur = conn.cursor()
            
            cur.execute("""
                DELETE FROM playlist_songs 
                WHERE playlist_id = %s::UUID AND song_id = %s::UUID
            """, (playlist_id, song_id))
        
        logger.info(f"[REMOVE_FROM_PLAYLIST] Removed {song_id} from {playlist_id}")
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[REMOVE_FROM_PLAYLIST] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/api/playlists/<playlist_id>/songs", methods=["PUT"])
def reorder_playlist_songs(playlist_id):
    """Reorder songs in playlist"""
    try:
        data = request.get_json(silent=True) or {}
        song_ids = data.get("songs", [])
        
        with get_db() as conn:
            cur = conn.cursor()
            
            # Check exists
            cur.execute("SELECT playlist_id FROM playlists WHERE playlist_id = %s::UUID", (playlist_id,))
            if not cur.fetchone():
                logger.warning(f"[REORDER_PLAYLIST] Not found: {playlist_id}")
                return jsonify({"error": "Not found"}), 404
            
            # Delete all current positions
            cur.execute("DELETE FROM playlist_songs WHERE playlist_id = %s::UUID", (playlist_id,))
            
            # Re-insert with new positions
            for position, song_id in enumerate(song_ids):
                cur.execute("""
                    INSERT INTO playlist_songs (playlist_id, song_id, position)
                    VALUES (%s::UUID, %s::UUID, %s)
                """, (playlist_id, song_id, position))
            
            cur.execute("""
                UPDATE playlists 
                SET updated_at = CURRENT_TIMESTAMP
                WHERE playlist_id = %s::UUID
            """, (playlist_id,))
        
        logger.info(f"[REORDER_PLAYLIST] Reordered {playlist_id}")
        return jsonify({"ok": True})
    
    except Exception as e:
        logger.error(f"[REORDER_PLAYLIST] Exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500

# ============ STATIC ROUTES ============

@app.route("/")
def home():
    return send_file("index.html")

# ============ INITIALIZATION & STARTUP ============

if __name__ == "__main__":
    try:
        logger.info("=" * 60)
        logger.info("Starting Music Library Server (PostgreSQL Backend)")
        logger.info("=" * 60)
        
        # Check ffmpeg availability
        ffmpeg_path = check_ffmpeg()
        
        # Initialize database pool and schema
        init_db_pool()
        init_schema()
        
        logger.info("Server initialization complete")
        logger.info("=" * 60)
        
        # Run Flask app
        port = int(os.environ.get("PORT", 5000))
        logger.info(f"Listening on 0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, debug=False)
    
    except Exception as e:
        logger.error(f"FATAL: Server startup failed: {e}", exc_info=True)
        raise