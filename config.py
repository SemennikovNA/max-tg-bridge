import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

SESSION_DIR = Path(os.getenv("SESSION_DIR") or BASE_DIR / "session")
SESSION_FILE = SESSION_DIR / "session.json"
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")

WEB_TIMEZONE = os.getenv("TZ", "Europe/Moscow")
WEB_LOCALE = os.getenv("WEB_LOCALE", "ru")
WEB_APP_VERSION = os.getenv("WEB_APP_VERSION", "26.6.6")
WEB_USER_AGENT = os.getenv(
    "WEB_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
)
WEB_OS_VERSION = os.getenv("WEB_OS_VERSION", "macOS")
WEB_DEVICE_NAME = os.getenv("WEB_DEVICE_NAME", "Chrome")
WEB_SCREEN = os.getenv("WEB_SCREEN", "982x1512 2.0x")

WEB_FINGERPRINT = {
    "deviceType": "WEB",
    "pushDeviceType": "WEBPUSH",
    "locale": WEB_LOCALE,
    "deviceLocale": WEB_LOCALE,
    "osVersion": WEB_OS_VERSION,
    "deviceName": WEB_DEVICE_NAME,
    "headerUserAgent": WEB_USER_AGENT,
    "appVersion": WEB_APP_VERSION,
    "screen": WEB_SCREEN,
    "timezone": WEB_TIMEZONE,
}

MAX_PHONE = os.getenv("MAX_PHONE", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_GROUP_ID = int(os.getenv("TELEGRAM_GROUP_ID") or "0")

HUMAN_DELAY_MIN = float(os.getenv("HUMAN_DELAY_MIN") or "1.0")
HUMAN_DELAY_MAX = float(os.getenv("HUMAN_DELAY_MAX") or "5.0")
QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START") or "1")
QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END") or "8")

HISTORY_DEPTH = int(os.getenv("HISTORY_DEPTH") or "10")
TOPIC_THROTTLE = float(os.getenv("TOPIC_THROTTLE") or "2.0")

IGNORED_CHAT_IDS = {
    int(x) for x in (os.getenv("IGNORED_CHAT_IDS") or "0").split(",") if x.strip()
}

HEARTBEAT_FILE = DATA_DIR / "heartbeat"
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL") or "30")
