import logging
import threading
import uuid

from flask import Blueprint, jsonify, request
from psycopg2.extras import RealDictCursor

from app.db import get_db
from app.services.auth_service import require_auth
from app.services.downloader import DEFAULT_SONGS, create_download_job, run_download_job


download_bp = Blueprint("download", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


@download_bp.route("/songs", methods=["GET"])
def get_songs():
    try:
        return jsonify({"songs": DEFAULT_SONGS})
    except Exception as e:
        logger.error("[GET_SONGS] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@download_bp.route("/download", methods=["POST"])
@require_auth
def start_download():
    try:
        user_id = request.user_id
        data = request.get_json(silent=True) or {}
        songs = data.get("songs", [])

        if not isinstance(songs, list) or not songs:
            return jsonify({"error": "Invalid songs list"}), 400

        job_id = create_download_job(user_id, songs)
        thread = threading.Thread(
            target=run_download_job,
            args=(job_id, user_id, songs),
            daemon=True,
        )
        thread.start()
        return jsonify({"job_id": job_id, "jobId": job_id})
    except Exception as e:
        logger.error("[DOWNLOAD] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@download_bp.route("/status/<job_id>", methods=["GET"])
@require_auth
def job_status(job_id):
    try:
        try:
            uuid.UUID(str(job_id))
        except ValueError:
            return jsonify({"error": "Invalid job id"}), 400

        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM download_jobs WHERE job_id = %s", (job_id,))
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
        failed = [
            {"song": r["song_query"], "error": r["error_message"]}
            for r in results
            if r["status"] == "failed"
        ]

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
        logger.error("[STATUS] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
