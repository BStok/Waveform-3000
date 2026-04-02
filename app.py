import os
import shutil
import zipfile
import threading
import uuid
import time
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "downloads"
ZIP_DIR = "zips"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(ZIP_DIR, exist_ok=True)

# In-memory job store: { job_id: { status, progress, total, done, error, songs } }
jobs = {}

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
                ydl.download([song])
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
    else:
        job["status"] = "cancelled"

    # Cleanup raw files
    shutil.rmtree(session_dir, ignore_errors=True)


@app.route("/api/songs", methods=["GET"])
def get_songs():
    return jsonify({"songs": DEFAULT_SONGS})


@app.route("/api/download", methods=["POST"])
def start_download():
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
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["cancelled"] = True
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5000)