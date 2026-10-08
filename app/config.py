import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

SECRET_KEY = os.getenv("SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/main-project.db")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")
YANDEX_API_BASE = "https://api.iot.yandex.net/v1.0"
POLL_INTERVAL_SECONDS = max(10, int(os.getenv("POLL_INTERVAL_SECONDS", "60")))

ACTIVITY_DIR = os.getenv("ACTIVITY_DIR", "./data/activity")
