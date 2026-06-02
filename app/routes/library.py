import logging
import os

from flask import Blueprint, jsonify, request, send_file
from psycopg2.extras import RealDictCursor

from app.db import get_db
from app.services.auth_service import require_auth


library_bp = Blueprint("library", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


@library_bp.route("/library", methods=["GET"])
@require_auth
def get_library():
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
        logger.error("[GET_LIBRARY] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@library_bp.route("/library/<song_id>", methods=["DELETE"])
@require_auth
def delete_song(song_id):
    try:
        user_id = request.user_id
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE songs
                SET status = 'deleted', updated_at = CURRENT_TIMESTAMP
                WHERE song_id = %s::UUID AND user_id = %s
            """, (song_id, user_id))

        logger.info("[DELETE_SONG] %s", song_id)
        return jsonify({"ok": True})
    except Exception as e:
        logger.error("[DELETE_SONG] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@library_bp.route("/audio/<song_id>/stream", methods=["GET"])
@require_auth
def stream_audio(song_id):
    try:
        user_id = request.user_id
        with get_db() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT storage_path
                FROM songs
                WHERE song_id = %s::UUID AND user_id = %s AND status = 'ready'
            """, (song_id, user_id))
            song = cur.fetchone()

        if not song:
            return jsonify({"error": "Not found"}), 404

        storage_path = song["storage_path"]
        if not os.path.exists(storage_path):
            logger.error("[STREAM] File missing: %s", storage_path)
            return jsonify({"error": "File not found"}), 404

        response = send_file(storage_path, mimetype="audio/mpeg", conditional=True, as_attachment=False)
        response.headers["Accept-Ranges"] = "bytes"
        return response
    except Exception as e:
        logger.error("[STREAM] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
