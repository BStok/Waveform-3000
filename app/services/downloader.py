import base64
import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import yt_dlp

from app.config import Config
from app.db import get_db


logger = logging.getLogger(__name__)
FFMPEG_PATH = None
YOUTUBE_COOKIES_FILE = None

DEFAULT_SONGS = [
    "Icarus Bastille", "Pompeii Bastille", "Achilles Come Down Gang of Youths",
    "Glory and Gore Lorde", "Touch the Sky Julie Fowlis", "Centuries Fall Out Boy",
    "I Am the Best 2NE1", "Touch-Tone Telephone Lemon Demon",
    "Cult of Dionysus The Orion Experience", "Abhi Kuch Dino Se Pritam",
    "Chandaniya 2 States", "Chand Si Mehbooba Ho Meri", "Chaudhary Mame Khan",
    "Sawan Mein Lag Gayi Aag Falguni Pathak", "Dhuro Nachyo Abhigya The Artist",
    "Huri Chalyo Prashant",
]


def check_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        try:
            import imageio_ffmpeg
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:
            logger.warning("imageio-ffmpeg fallback unavailable: %s", e)

    if not ffmpeg_path:
        raise RuntimeError("ffmpeg is required but not installed")

    logger.info("ffmpeg found at: %s", ffmpeg_path)
    return ffmpeg_path


def get_ffmpeg_path():
    global FFMPEG_PATH
    if not FFMPEG_PATH:
        FFMPEG_PATH = check_ffmpeg()
    return FFMPEG_PATH


def get_youtube_cookies_file():
    global YOUTUBE_COOKIES_FILE

    if Config.YOUTUBE_COOKIES_PATH:
        if os.path.exists(Config.YOUTUBE_COOKIES_PATH):
            return Config.YOUTUBE_COOKIES_PATH
        logger.warning("YOUTUBE_COOKIES_PATH is set but the file does not exist")

    if not Config.YOUTUBE_COOKIES_B64 and not Config.YOUTUBE_COOKIES:
        return None

    if not YOUTUBE_COOKIES_FILE:
        Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        YOUTUBE_COOKIES_FILE = str(Config.TEMP_DIR / "youtube_cookies.txt")

    cookies_text = Config.YOUTUBE_COOKIES
    if Config.YOUTUBE_COOKIES_B64:
        cookies_text = base64.b64decode(Config.YOUTUBE_COOKIES_B64).decode("utf-8")
    else:
        cookies_text = cookies_text.replace("\\n", "\n")

    Path(YOUTUBE_COOKIES_FILE).write_text(cookies_text, encoding="utf-8")
    return YOUTUBE_COOKIES_FILE


def get_audio_duration(file_path):
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", file_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return int(float(result.stdout.strip()))
        return 0
    except Exception as e:
        logger.warning("Could not determine duration for %s: %s", file_path, e)
        return 0


def create_download_job(user_id, songs):
    job_id = str(uuid.uuid4())
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO download_jobs (job_id, user_id, total_songs, status)
            VALUES (%s, %s, %s, %s)
        """, (job_id, user_id, len(songs), "queued"))
    return job_id


def run_download_job(job_id, user_id, songs):
    session_temp_dir = Config.TEMP_DIR / job_id

    try:
        session_temp_dir.mkdir(parents=True, exist_ok=True)
        mark_job_running(job_id)
        logger.info("[DOWNLOAD] Job %s started: %s songs", job_id, len(songs))

        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "default_search": "ytsearch1",
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_music", "web_creator"],
                }
            },
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "outtmpl": str(session_temp_dir / "%(id)s.%(ext)s"),
            "restrictfilenames": False,
            "quiet": False,
            "no_warnings": False,
            "ffmpeg_location": get_ffmpeg_path(),
            "prefer_ffmpeg": True,
            "socket_timeout": 30,
            "retries": 3,
            "sleep_interval": 2,
        }

        cookies_file = get_youtube_cookies_file()
        if cookies_file:
            ydl_opts["cookiefile"] = cookies_file
            logger.info("[DOWNLOAD] Using YouTube cookies file")
        else:
            logger.warning("[DOWNLOAD] No YouTube cookies configured; YouTube may block Render downloads")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for song_query in songs:
                if job_is_cancelled(job_id):
                    logger.info("[DOWNLOAD] Job %s cancelled", job_id)
                    break
                download_one_song(ydl, job_id, user_id, song_query, session_temp_dir)

        mark_job_done(job_id)
        logger.info("[DOWNLOAD] Job %s completed", job_id)
    except Exception as e:
        logger.error("[DOWNLOAD] Job %s fatal error: %s", job_id, e, exc_info=True)
        mark_job_error(job_id, str(e))
    finally:
        shutil.rmtree(session_temp_dir, ignore_errors=True)


def download_one_song(ydl, job_id, user_id, song_query, session_temp_dir):
    update_current_song(job_id, song_query)
    logger.info("[DOWNLOAD] Downloading: %s", song_query)

    temp_file = None
    try:
        info = ydl.extract_info(song_query, download=True)
        song_title = info.get("title", song_query)
        source_id = info.get("id")
        song_id = str(uuid.uuid4())
        temp_file = find_downloaded_mp3(session_temp_dir, source_id)

        duration = get_audio_duration(str(temp_file))
        final_path = Config.STORAGE_DIR / f"{song_id}.mp3"

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO songs (
                    song_id, user_id, source_id, title, artist, duration_seconds, storage_path, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (song_id, user_id, source_id, song_title, song_query, duration, str(final_path), "ready"))

        shutil.move(str(temp_file), str(final_path))
        temp_file = None
        mark_song_success(job_id, song_id, song_query)
        logger.info("[DOWNLOAD] success: %s", song_title)
    except Exception as e:
        logger.error("[DOWNLOAD] failed %s: %s", song_query, e)
        if temp_file and Path(temp_file).exists():
            Path(temp_file).unlink()
        mark_song_failed(job_id, song_query, str(e))


def find_downloaded_mp3(session_temp_dir, source_id):
    expected_file = session_temp_dir / f"{source_id}.mp3"
    if expected_file.exists():
        return expected_file

    mp3_files = list(session_temp_dir.glob("*.mp3"))
    if not mp3_files:
        raise RuntimeError("No MP3 files found")

    return max(mp3_files, key=lambda path: path.stat().st_ctime)


def mark_job_running(job_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE download_jobs SET status = %s WHERE job_id = %s", ("running", job_id))


def job_is_cancelled(job_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT cancelled FROM download_jobs WHERE job_id = %s", (job_id,))
        result = cur.fetchone()
    return bool(result and result[0])


def update_current_song(job_id, song_query):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE download_jobs
            SET current_song = %s
            WHERE job_id = %s
        """, (song_query, job_id))


def mark_song_success(job_id, song_id, song_query):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO job_results (job_id, song_id, song_query, status)
            VALUES (%s, %s, %s, %s)
        """, (job_id, song_id, song_query, "success"))
        cur.execute("""
            UPDATE download_jobs
            SET downloaded_count = downloaded_count + 1
            WHERE job_id = %s
        """, (job_id,))


def mark_song_failed(job_id, song_query, error_message):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO job_results (job_id, song_query, status, error_message)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (job_id, song_query) DO UPDATE
            SET status = 'failed', error_message = %s
        """, (job_id, song_query, "failed", error_message, error_message))
        cur.execute("""
            UPDATE download_jobs
            SET failed_count = failed_count + 1
            WHERE job_id = %s
        """, (job_id,))


def mark_job_done(job_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE download_jobs
            SET status = %s, completed_at = CURRENT_TIMESTAMP
            WHERE job_id = %s
        """, ("done", job_id))


def mark_job_error(job_id, error_message):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE download_jobs
            SET status = %s, error_message = %s, completed_at = CURRENT_TIMESTAMP
            WHERE job_id = %s
        """, ("error", error_message, job_id))
