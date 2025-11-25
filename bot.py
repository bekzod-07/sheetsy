from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import BOT_TOKEN
from handlers.voice_append import router as voice_append_router
from handlers.sheet_wizard import router as wizard_router
from handlers.files import router as files_router
from handlers.favorites import router as favorites_router
from handlers.help import router as help_router
from handlers.settings import router as settings_router

def create_bot_dp():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Routerni ketma-ket ulaymiz
    dp.include_router(wizard_router)
    dp.include_router(files_router)          # ✅ "📂 Mening fayllarim" uchun yangi router
    dp.include_router(voice_append_router)
    dp.include_router(favorites_router)     # ✅ "⭐️ Sevimlilar" uchun yangi router
    dp.include_router(help_router)
    dp.include_router(settings_router)

    return bot, dp


async def setup_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash / Ro‘yxatdan o‘tish"),
        BotCommand(command="connect", description="Google bilan bog‘lash"),
        BotCommand(command="mysheets", description="Sheets fayllar ro‘yxati"),
    ])
