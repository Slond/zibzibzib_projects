import os

SECRET_KEY = os.getenv("SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

YANDEX_TOKEN = os.getenv("YANDEX_TOKEN")
YANDEX_API_BASE = "https://api.iot.yandex.net/v1.0"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS"))
