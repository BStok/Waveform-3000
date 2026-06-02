from flask import Blueprint, render_template, send_from_directory

from app.config import Config


pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/assets/<path:filename>")
def template_asset(filename):
    return send_from_directory(Config.ASSETS_DIR, filename)


@pages_bp.route("/favicon.ico")
def favicon():
    return send_from_directory(Config.ASSETS_DIR, "favicon.ico", mimetype="image/x-icon")


@pages_bp.route("/")
def home():
    return render_template("landing.html")


@pages_bp.route("/app")
def app_page():
    return render_template("index.html")
