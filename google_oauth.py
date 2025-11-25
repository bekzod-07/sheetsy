# google_oauth.py
import base64
import json
import hmac
import hashlib
from typing import Optional, Tuple

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from config import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    OAUTH_REDIRECT_URI,
    SCOPES,
    SESSION_SECRET,
)

from db import get_token_by_tg   # faqat buni ishlatamiz


# ============================================================
#  JWT decode helperlar (EMAIL chiqarish uchun)
# ============================================================

def _b64_decode(part: str) -> str:
    part += "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part.encode()).decode()


def extract_email_from_id_token(id_token: str) -> str:
    """ID_TOKEN → JWT → EMAIL"""
    try:
        header, payload, sig = id_token.split(".")
        payload_json = json.loads(_b64_decode(payload))
        return payload_json.get("email", "")
    except:
        return ""


# ============================================================
#  STATE SIGN / VERIFY (HMAC)
# ============================================================

def sign_state(tg_id: int) -> str:
    payload = str(tg_id).encode()
    sig = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"." + sig).decode()


def verify_state(state: str) -> int:
    raw = base64.urlsafe_b64decode(state.encode())
    payload, sig = raw.split(b".", 1)

    expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Bad state signature")

    return int(payload.decode())


# ============================================================
#  BUILD OAUTH FLOW
# ============================================================

def build_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "project_id": "sheetsy-bot",
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": [OAUTH_REDIRECT_URI],
            "javascript_origins": []
        }
    }

    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI
    )


def build_auth_url(tg_id: int) -> Tuple[str, str]:
    flow = build_flow()
    state_token = sign_state(tg_id)

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes=True,
        prompt="consent",
        state=state_token
    )
    return auth_url, state_token


# ============================================================
#  DB ↔ CREDENTIALS
# ============================================================

async def _load_row(tg_id: int):
    return await get_token_by_tg(tg_id)


def _creds_from_row(row) -> Optional[Credentials]:
    if not row:
        return None

    return Credentials(
        token=row["access_token"],
        refresh_token=row.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES
    )


# ================================
# Yangi TOKENS saqlash
# ================================

async def _persist_creds(tg_id: int, creds):
    import sqlite3
    conn = sqlite3.connect("bot.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ===== EMAIL OLISh =====
    email = ""

    # 1) ID_TOKEN orqali (eng to‘g‘ri)
    if getattr(creds, "id_token", None):
        email = extract_email_from_id_token(creds.id_token)

    # 2) token_response (fallback)
    if not email:
        try:
            email = creds.token_response.get("email", "")
        except:
            pass

    if not email:
        email = ""

    # ===== EXPIRY =====
    expiry_ts = None
    try:
        if creds.expiry:
            expiry_ts = creds.expiry.timestamp()
    except:
        expiry_ts = None

    # ===== USER ID topamiz =====
    cur.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return

    user_id = row["id"]

    # ===== TOKENS NI UPSERT QILAMIZ =====
    cur.execute("""
        INSERT INTO tokens (user_id, access_token, refresh_token, expiry_ts, email)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET 
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expiry_ts=excluded.expiry_ts,
            email=excluded.email
    """, (
        user_id,
        creds.token,
        creds.refresh_token,
        expiry_ts,
        email
    ))

    conn.commit()
    conn.close()


# ============================================================
#  PUBLIC HELPERS
# ============================================================

async def get_credentials_for_tg(tg_id: int) -> Optional[Credentials]:
    row = await _load_row(tg_id)
    return _creds_from_row(row)


async def save_token_for_tg(tg_id: int, creds):
    await _persist_creds(tg_id, creds)


async def ensure_valid_credentials(tg_id: int) -> Optional[Credentials]:
    creds = await get_credentials_for_tg(tg_id)
    if not creds:
        return None

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            await _persist_creds(tg_id, creds)
            return creds
        except:
            return None

    return None


# ============================================================
#  CALLBACK PROCESSOR
# ============================================================

async def exchange_code_and_store(state: str, code: str) -> int:
    tg_id = verify_state(state)

    flow = build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials

    await _persist_creds(tg_id, creds)
    return tg_id
