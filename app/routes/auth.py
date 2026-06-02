import logging

from flask import Blueprint, jsonify, request

from app.db import get_db
from app.services.auth_service import (
    handle_login,
    handle_set_password,
    handle_signup,
    handle_verify_email,
)


auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")
logger = logging.getLogger(__name__)


@auth_bp.route("/login", methods=["POST"])
def login():
    try:
        data = request.get_json(silent=True) or {}
        token, email, error = handle_login(data, get_db)

        if error:
            logger.warning("[LOGIN] Failed: %s", error)
            return jsonify({"error": error}), 401

        logger.info("[LOGIN] success: %s", email)
        return jsonify({"token": token, "email": email})
    except Exception as e:
        logger.error("[LOGIN] Exception: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@auth_bp.route("/signup", methods=["POST"])
def signup():
    try:
        result, error = handle_signup(request.get_json(silent=True) or {}, get_db)
        if error:
            return jsonify({"error": error}), 400
        return jsonify(result)
    except Exception as e:
        logger.error("[SIGNUP] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@auth_bp.route("/verify-email", methods=["POST"])
def verify_email():
    try:
        result, error = handle_verify_email(request.get_json(silent=True) or {}, get_db)
        if error:
            return jsonify({"error": error}), 400
        return jsonify(result)
    except Exception as e:
        logger.error("[VERIFY_EMAIL] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@auth_bp.route("/set-password", methods=["POST"])
def set_password():
    try:
        result, error = handle_set_password(request.get_json(silent=True) or {}, get_db)
        if error:
            return jsonify({"error": error}), 400
        return jsonify(result)
    except Exception as e:
        logger.error("[SET_PASSWORD] %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
