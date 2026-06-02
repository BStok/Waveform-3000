import os
from pathlib import Path


class Config:
    ROOT_DIR = Path(__file__).resolve().parent.parent
    TEMPLATE_DIR = ROOT_DIR / "templates"
    ASSETS_DIR = TEMPLATE_DIR / "assets"

    DOWNLOADS_DIR = ROOT_DIR / "downloads"
    STORAGE_DIR = ROOT_DIR / "storage"
    TEMP_DIR = DOWNLOADS_DIR / "temp"

    SECRET_KEY = os.environ.get("SECRET_KEY", "super-secret-dev-key-change-this")
    DATABASE_URL = os.environ.get("DATABASE_URL")

    DB_CONFIG = {
        "host": os.environ.get("DB_HOST", "localhost"),
        "database": os.environ.get("DB_NAME", "music_library"),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", "Dsmushe22!"),
        "port": int(os.environ.get("DB_PORT", 5432)),
    }

    YOUTUBE_COOKIES_PATH = os.environ.get("YOUTUBE_COOKIES_PATH", "").strip()
    YOUTUBE_COOKIES_B64 = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
    YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "").strip()

    @classmethod
    def ensure_directories(cls):
        cls.DOWNLOADS_DIR.mkdir(exist_ok=True)
        cls.STORAGE_DIR.mkdir(exist_ok=True)
        cls.TEMP_DIR.mkdir(parents=True, exist_ok=True)
