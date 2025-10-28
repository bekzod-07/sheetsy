# handlers/voice.py
import tempfile
from aiogram import Router, F
from aiogram.types import Message, FSInputFile
from db import get_user_by_tg
from stt_client import stt_short, stt_long
from google_oauth import sign_state  # faqat BASE_URL uchun bizda bor
from config import BASE_URL

router = Router(name="voice")

def _lang_for_user(row) -> str:
    if not row:
        return "uz"
    lang = (row.get("language") or "uz").lower()
    return "uz" if lang.startswith("uz") else "ru" if lang.startswith("ru") else "en"

@router.message(F.voice | F.audio)
async def handle_audio(msg: Message):
    bot = msg.bot
    user = await get_user_by_tg(msg.from_user.id)
    lang = _lang_for_user(user)

    # Telegram faylini vaqtincha saqlab olish
    file_obj = await bot.get_file(msg.voice.file_id if msg.voice else msg.audio.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as f:
        await bot.download(file_obj, destination=f.name)
        audio_path = f.name

    duration = msg.voice.duration if msg.voice else (msg.audio.duration or 0)
    title = f"tg_{msg.from_user.id}_{msg.message_id}"

    try:
        if duration <= 60:
            # Qisqa audio: sinxron
            resp = await stt_short(audio_path, title=title, language=lang, has_diarization=False)
            # Xizmat javob JSON tuzilmasini sizdagi backdan kelishiga qarab moslang:
            text = resp.get("text") or resp.get("result") or str(resp)
            await msg.answer(f"📝 Matn:\n{text}")
        else:
            # Uzun audio: webhook
            webhook = f"{BASE_URL}/webhooks/aisha-stt?tg_id={msg.from_user.id}"
            resp = await stt_long(audio_path, title=title, has_diarization=False, webhook_url=webhook)
            await msg.answer("✅ Audio qabul qilindi. Tayyor bo‘lganda matnni shu yerga yuboraman.")
    except Exception as e:
        await msg.answer(f"❌ STT xatolik: {e}")