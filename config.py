# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# <-- YANGI: oauthlib scope farqiga jahl qilmasin
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET", "devsecret")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid"
]


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN yo'q")
if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI and BASE_URL):
    print("[WARN] Google OAuth sozlamalari to'liq emas – /connect ishlamasligi mumkin.")

