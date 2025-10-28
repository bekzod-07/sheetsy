from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import BOT_TOKEN
from handlers.voice_append import router as voice_append_router
from handlers.sheet_wizard import router as wizard_router

def create_bot_dp():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(wizard_router)
    dp.include_router(voice_append_router)    # <-- YANGI

    return bot, dp
    
async def setup_commands(bot: Bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Boshlash / r   o'yxatdan o'tish"),
        BotCommand(command="connect", description="Google bilan ulash"),
        BotCommand(command="mysheets", description="Sheets ro'yxati"),
    ])
