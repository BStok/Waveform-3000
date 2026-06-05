from dotenv import load_dotenv
print("TOP OF MAIN, THE ONLY PINNACLE WITHIN YOUR REACH")
load_dotenv()

import logging
import os
import yt_dlp

import shutil
import subprocess

from flask import Flask, request

try:
    from flask_cors import CORS
except ImportError:
    def CORS(*args, **kwargs):
        return None

from app.config import Config
from app.routes.auth import auth_bp
from app.routes.download import download_bp
from app.routes.library import library_bp
from app.routes.pages import pages_bp
from app.routes.playlists import playlists_bp
from app.services.downloader import get_ffmpeg_path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

#Diagnosis log for ytdlp
logger.info("Node executable: %s", shutil.which("node"))

try:
    result = subprocess.check_output(
        ["node", "--version"],
        text=True
    ).strip()
    logger.info("Node version: %s", result)
except Exception as e:
    logger.error("Node check failed: %s", e)
logger.info("yt-dlp version: %s", yt_dlp.version.__version__)

def create_app():
    Config.ensure_directories()

    flask_app = Flask(__name__, template_folder=str(Config.TEMPLATE_DIR))
    flask_app.config["SECRET_KEY"] = Config.SECRET_KEY

    CORS(
        flask_app,
        resources={r"/api/*": {"origins": "*"}},
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    )

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(download_bp)
    flask_app.register_blueprint(library_bp)
    flask_app.register_blueprint(playlists_bp)
    flask_app.register_blueprint(pages_bp)

    @flask_app.before_request
    def log_request():
        try:
            result = subprocess.check_output(
                ["node", "--version"],
                text=True
            ).strip()
            logger.error("NODE VERSION = %s", result)
        except Exception as e:
            logger.error("NODE ERROR = %s", e)

        logger.info("[%s] %s from %s",
                    request.method,
                    request.path,
                    request.remote_addr)
    
    @flask_app.after_request
    def log_response(response):
        logger.info("[%s] %s %s", response.status_code, request.method, request.path)
        return response

    return flask_app

print("MAIN.PY LOADED...")
print("YOU CAN DO IT DIVA, KEEP GOINGGGGGGG")
app = create_app()


if __name__ == "__main__":
    
    logger.info("Starting WAVEFORM-3000 PRO Server")
    
    get_ffmpeg_path()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
