import logging
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request
from psycopg2.extras import RealDictCursor

from app.db import get_db
from app.services.auth_service import require_auth


playlists_bp = Blueprint("playlists", __name__, url_prefix="/api")
logger = logging.getLogger(__name__)


@playlists_bp.route("/playlists", methods=["GET"])
@require_auth
def get_playlists():
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
            for playlist in playlists:
                cur.execute("""
                    SELECT song_id
                    FROM playlist_songs
                    WHERE playlist_id = %s
                    ORDER BY position ASC
                """, (playlist["playlist_id"],))
                songs = [row["song_id"] for row in cur.fetchall()]

                playlist_dict = dict(playlist)
                playlist_dict["id"] = playlist_dict["playlist_id"]
                playlist_dict["songs"] = songs
                playlists_dict[playlist["playlist_id"]] = playlist_dict

        return jsonify(playlists_dict)
    except Exception as e:
        logger.error("[GET_PLAYLISTS] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@playlists_bp.route("/playlists", methods=["POST"])
@require_auth
def create_playlist():
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

        now = datetime.now().isoformat()
        return jsonify({
            "id": playlist_id,
            "playlist_id": playlist_id,
            "name": name,
            "songs": [],
            "created_at": now,
            "updated_at": now,
        })
    except Exception as e:
        logger.error("[CREATE_PLAYLIST] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@playlists_bp.route("/playlists/<playlist_id>", methods=["DELETE"])
@require_auth
def delete_playlist(playlist_id):
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
        logger.error("[DELETE_PLAYLIST] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@playlists_bp.route("/playlists/<playlist_id>/songs/<song_id>", methods=["POST"])
@require_auth
def add_song_to_playlist(playlist_id, song_id):
    try:
        user_id = request.user_id
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT playlist_id
                FROM playlists
                WHERE playlist_id = %s::UUID AND user_id = %s
            """, (playlist_id, user_id))
            if not cur.fetchone():
                return jsonify({"error": "Not found"}), 404

            cur.execute("SELECT MAX(position) FROM playlist_songs WHERE playlist_id = %s::UUID", (playlist_id,))
            result = cur.fetchone()
            next_position = (result[0] or -1) + 1

            cur.execute("""
                INSERT INTO playlist_songs (playlist_id, song_id, position)
                VALUES (%s::UUID, %s::UUID, %s)
                ON CONFLICT (playlist_id, song_id) DO NOTHING
            """, (playlist_id, song_id, next_position))

        return jsonify({"ok": True})
    except Exception as e:
        logger.error("[ADD_TO_PLAYLIST] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@playlists_bp.route("/playlists/<playlist_id>/songs/<song_id>", methods=["DELETE"])
@require_auth
def remove_song_from_playlist(playlist_id, song_id):
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                DELETE FROM playlist_songs
                WHERE playlist_id = %s::UUID AND song_id = %s::UUID
            """, (playlist_id, song_id))

        return jsonify({"ok": True})
    except Exception as e:
        logger.error("[REMOVE_FROM_PLAYLIST] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
