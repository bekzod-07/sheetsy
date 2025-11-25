# handlers/voice.py
import tempfile
from aiogram import Router, F
from aiogram.types import Message
from db import get_user_by_tg
from stt_client import stt_short, stt_long
from config import BASE_URL

router = Router(name="voice")


def _lang_for_user(row) -> str:
    if not row:
        return "uz"
    lang = (row.get("language") or "uz").lower()
    if lang.startswith("uz"):
        return "uz"
    if lang.startswith("ru"):
        return "ru"
    return "en"


@router.message(F.voice | F.audio)
async def handle_audio(msg: Message):
    user = await get_user_by_tg(msg.from_user.id)
    lang = _lang_for_user(user)

    audio = msg.voice or msg.audio
    if not audio:
        return

    # 1) faylni bir marta olib, vaqtinchalik saqlaymiz
    file = await msg.bot.get_file(audio.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as f:
        await msg.bot.download(file, destination=f.name)
        audio_path = f.name

    duration = int(audio.duration or 0)
    title = f"tg_{msg.from_user.id}_{msg.message_id}"

    try:
        if duration <= 60:
            # 🔹 Qisqa audio – sinxron STT
            resp = await stt_short(
                audio_path,
                title=title,
                language=lang,
                has_diarization=False,
            )
            text = resp.get("text") or resp.get("result") or resp.get("transcript") or ""
            if not text:
                text = str(resp)
            await msg.answer(f"📝 Matn:\n{text}")
        else:
            # 🔹 Uzoq audio – webhook orqali
            webhook = f"{BASE_URL}/webhooks/aisha-stt?tg_id={msg.from_user.id}"
            await stt_long(
                audio_path,
                title=title,
                has_diarization=False,
                webhook_url=webhook,
            )
            await msg.answer("✅ Audio qabul qilindi. Tayyor bo‘lganda matnni yuboraman.")
    except Exception as e:
        await msg.answer(f"❌ STT xatolik: {e}")
