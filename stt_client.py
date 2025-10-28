# stt_client.py
from __future__ import annotations

import os
import asyncio
import mimetypes
from pathlib import Path
from typing import Optional, Dict, Any, Mapping, Union

import aiohttp

# ----- Config -----
AISHA_STT_API_KEY = os.getenv("AISHA_STT_API_KEY") or ""
AISHA_STT_BASE = os.getenv("AISHA_STT_BASE", "https://back.aisha.group").rstrip("/")

# MIME fixes for common Telegram/audio formats
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/ogg", ".oga")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/mp4", ".m4a")

Json = Dict[str, Any]


def _filename_and_ct(audio_path: Union[str, Path], title: str) -> tuple[str, str]:
    """
    Keep original extension, guess content-type, and provide safe fallbacks.
    """
    p = Path(audio_path)
    ext = p.suffix or ".ogg"  # Telegram voice is typically .ogg/opus
    filename = f"{title}{ext}"

    ctype, _ = mimetypes.guess_type(filename)
    if not ctype:
        ctype = "audio/ogg" if ext.lower() in (".ogg", ".oga") else "application/octet-stream"

    return filename, ctype


async def _read_body_safely(resp: aiohttp.ClientResponse) -> str:
    try:
        return await resp.text()
    except Exception:
        return "<no body>"


async def _raise_with_body(resp: aiohttp.ClientResponse) -> None:
    body = await _read_body_safely(resp)
    raise aiohttp.ClientResponseError(
        resp.request_info,
        resp.history,
        status=resp.status,
        message=f"{resp.reason} | body: {body}",
        headers=resp.headers,
    )


class STTClient:
    """
    High-performance, reusable client for Aisha STT API.
    Reuses one aiohttp session, handles retries, and provides clean APIs.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        timeout_seconds: int = 600,
        default_language: str = "uz",
        max_retries: int = 3,
        retry_base_delay: float = 0.6,
    ) -> None:
        self.api_key = (api_key if api_key is not None else AISHA_STT_API_KEY).strip()
        self.base_url = (base_url if base_url is not None else AISHA_STT_BASE).rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.default_language = (default_language or "uz").lower()
        self.max_retries = max(1, max_retries)
        self.retry_base_delay = max(0.1, retry_base_delay)
        self._session: Optional[aiohttp.ClientSession] = None

        if not self.api_key:
            raise ValueError("AISHA_STT_API_KEY is missing. Set env var or pass api_key.")

    @property
    def headers(self) -> Mapping[str, str]:
        return {
            "x-api-key": self.api_key,
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> "STTClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def _post_multipart(
        self,
        url: str,
        fields: Mapping[str, Union[str, bytes]],
        file_field_name: str,
        file_path: Union[str, Path],
        file_filename: str,
        file_content_type: str,
    ) -> Json:
        """
        Robust POST with retries for transient errors.
        Retries on 5xx and common connection errors.
        """
        session = await self._ensure_session()

        attempt = 0
        while True:
            attempt += 1
            data = aiohttp.FormData()
            # regular fields
            for k, v in fields.items():
                # FormData requires str for fields; server expects strings anyway
                data.add_field(k, str(v))

            try:
                # Open file per-attempt to avoid using a closed stream after retry
                with open(file_path, "rb") as f:
                    data.add_field(
                        file_field_name,
                        f,
                        filename=file_filename,
                        content_type=file_content_type,
                    )
                    async with session.post(url, headers=self.headers, data=data) as resp:
                        if resp.status >= 400:
                            # retry only on 5xx
                            if 500 <= resp.status < 600 and attempt < self.max_retries:
                                await asyncio.sleep(self.retry_base_delay * (2 ** (attempt - 1)))
                                continue
                            await _raise_with_body(resp)

                        # Try JSON, fall back to text wrapper
                        try:
                            return await resp.json()
                        except Exception:
                            txt = await _read_body_safely(resp)
                            return {"ok": True, "raw": txt}

            except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError) as e:
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_base_delay * (2 ** (attempt - 1)))
                    continue
                raise e

    # ---------- Public APIs ----------

    async def stt_short(
        self,
        audio_path: Union[str, Path],
        *,
        title: str = "audio",
        language: Optional[str] = None,
        has_diarization: bool = False,
    ) -> Json:
        """
        v1: Qisqa audio uchun sinxron STT (javob darhol qaytadi).
        language: 'uz' | 'ru' | 'en'
        """
        url = f"{self.base_url}/api/v1/stt/post/"
        lang = (language or self.default_language).lower()
        filename, ctype = _filename_and_ct(audio_path, title)

        fields = {
            "title": title,
            "language": lang,
            "has_diarization": "true" if has_diarization else "false",
        }

        return await self._post_multipart(
            url=url,
            fields=fields,
            file_field_name="audio",
            file_path=audio_path,
            file_filename=filename,
            file_content_type=ctype,
        )

    async def stt_long(
        self,
        audio_path: Union[str, Path],
        *,
        webhook_url: str,
        title: str = "audio",
        has_diarization: bool = False,
    ) -> Json:
        """
        v2: Uzun audio uchun. Natija webhook'ga yuboriladi.
        """
        if not webhook_url:
            raise ValueError("webhook_url is required for stt_long().")

        url = f"{self.base_url}/api/v2/stt/post/"
        filename, ctype = _filename_and_ct(audio_path, title)

        fields = {
            "title": title,
            "has_diarization": "true" if has_diarization else "false",
            "webhook_notification_url": webhook_url,
        }

        return await self._post_multipart(
            url=url,
            fields=fields,
            file_field_name="audio",
            file_path=audio_path,
            file_filename=filename,
            file_content_type=ctype,
        )


# ---------- Convenience top-level functions (backward compatible) ----------

# Old-style helpers for quick usage without managing the client manually.
# They create a client, perform the call, then close the session.

async def stt_short(
    audio_path: Union[str, Path],
    *,
    title: str = "audio",
    language: str = "uz",
    has_diarization: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout_seconds: int = 600,
) -> Json:
    async with STTClient(api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds) as client:
        return await client.stt_short(
            audio_path=audio_path,
            title=title,
            language=language,
            has_diarization=has_diarization,
        )


async def stt_long(
    audio_path: Union[str, Path],
    *,
    webhook_url: str,
    title: str = "audio",
    has_diarization: bool = False,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout_seconds: int = 600,
) -> Json:
    async with STTClient(api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds) as client:
        return await client.stt_long(
            audio_path=audio_path,
            webhook_url=webhook_url,
            title=title,
            has_diarization=has_diarization,
        )
