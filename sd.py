import asyncio
import base64
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import FSInputFile
from openai import OpenAI


# =============================
#  ENV VARIABLES
# =============================
BOT_TOKEN = "8267943463:AAGrk_3fO5F6yYsx2jhhTEaKRQV4FZF1SeQ"
OPENAI_API_KEY = "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"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

client = OpenAI(api_key=OPENAI_API_KEY)


# =============================
#  START COMMAND
# =============================
@dp.message(F.text == "/start")
async def start_cmd(message: types.Message):
    await message.answer(
        "Assalomu alaykum! 😊\n"
        "Menga *har qanday prompt* yozing — men sizga chiroyli, professional rasm yaratib beraman!\n\n"
        "Masalan:\n"
        "`premium dark blue gradient background`\n"
        "`3D robot logo`\n"
        "`data science futuristic banner`"
    )


# =============================
#  PROMPT → IMAGE GENERATION
# =============================
@dp.message()
async def generate_image(message: types.Message):
    prompt = message.text.strip()

    await message.answer("⏳ Rasm yaratilmoqda, biroz kuting...")

    try:
        # OpenAI API orqali rasm yaratish
        result = client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            n=1,
        )

        # Base64 decode
        image_base64 = result.data[0].b64_json
        image_bytes = base64.b64decode(image_base64)

        image_path = "result.png"
        with open(image_path, "wb") as f:
            f.write(image_bytes)

        # Rasmni foydalanuvchiga yuborish
        await message.reply_photo(
            photo=FSInputFile(image_path),
            caption="Mana siz so‘ragan rasm 😊"
        )

    except Exception as e:
        await message.answer(f"⚠️ Xatolik: {str(e)}")


# =============================
#  BOT POLLING
# =============================
async def main():
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
