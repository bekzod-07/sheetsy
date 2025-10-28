# google_oauth.py
import base64
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
from db import get_token_by_tg, upsert_token_by_tg  # async DB helpers


# -----------------------
#  State sign/verify (HMAC)
# -----------------------
def sign_state(tg_id: int) -> str:
    payload = str(tg_id).encode()
    sig = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"." + sig).decode()


def verify_state(state: str) -> int:
    raw = base64.urlsafe_b64decode(state.encode())
    payload, sig = raw.split(b".", 1)
    expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad state signature")
    return int(payload.decode())


# -----------------------
#  OAuth Flow
# -----------------------
def build_flow() -> Flow:
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "project_id": "proj",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uris": [OAUTH_REDIRECT_URI],
            }
        },
        scopes=SCOPES,
    )
    flow.redirect_uri = OAUTH_REDIRECT_URI
    return flow


def build_auth_url(tg_id: int) -> Tuple[str, str]:
    """
    Aiogram handlerlarda qulay bo‘lishi uchun: (auth_url, state_token) qaytaradi.
    """
    flow = build_flow()
    state_token = sign_state(tg_id)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state_token,
    )
    return auth_url, state_token


# -----------------------
#  DB <-> Credentials
# -----------------------
async def _load_row(tg_id: int):
    """
    get_token_by_tg DB dan quyidagilarni qaytarishi kutiladi:
      { "access_token": str, "refresh_token": Optional[str], "expiry_ts": Optional[int] }
    bo‘lmasa, o‘zingiz moslashtiring.
    """
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
        scopes=SCOPES,
    )


async def _persist_creds(tg_id: int, creds: Credentials) -> None:
    """
    refresh_token yo‘q bo‘lsa, eskisini saqlab qolamiz (Google ba'zan qaytarmaydi).
    """
    existing = await _load_row(tg_id)
    refresh_token = creds.refresh_token or (existing.get("refresh_token") if existing else None)

    expiry_ts = int(creds.expiry.timestamp()) if getattr(creds, "expiry", None) else None

    await upsert_token_by_tg(
        tg_id=tg_id,
        access_token=creds.token,
        refresh_token=refresh_token,
        expiry_ts=expiry_ts,
    )


# -----------------------
#  Public helpers
# -----------------------
async def get_credentials_for_tg(tg_id: int) -> Optional[Credentials]:
    """
    DB dan o‘qiydi, Credentials obyektini yasaydi (valid/expired bo‘lishi mumkin).
    """
    row = await _load_row(tg_id)
    return _creds_from_row(row)


async def save_token_for_tg(tg_id: int, creds: Credentials) -> None:
    """
    Callback’da fetch_token’dan so‘ng saqlash uchun.
    """
    await _persist_creds(tg_id, creds)


async def ensure_valid_credentials(tg_id: int) -> Optional[Credentials]:
    """
    - DB dan o‘qiydi
    - Agar expired va refresh_token mavjud bo‘lsa, yangilaydi
    - Yangilangan tokenni DB ga qayta yozadi
    - Oxirida valid Credentials qaytaradi (yoki None)
    """
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
        except Exception:
            return None

    return None

# -----------------------
#  Web callback helper
# -----------------------
async def exchange_code_and_store(state: str, code: str) -> int:
    """
    OAuth callback routerida ishlatish uchun qulay funksiya.
    - state dan tg_id ni tekshiradi
    - code orqali tokenni olib, DB ga saqlaydi
    - tg_id ni qaytaradi
    """
    tg_id = verify_state(state)
    flow = build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    await _persist_creds(tg_id, creds)
    return tg_id
