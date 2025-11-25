# handlers/help.py
from __future__ import annotations

from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from utils.keyboards import MAIN_MENU_KB

router = Router(name="help")


@router.message(F.text.in_(["❓ Yordam", "/help"]))
async def help_handler(msg: Message):
    """
    Bot bo‘yicha qisqa qo‘llanma.
    """
    text = (
        "❓ <b>Sheetsy bot bo‘yicha yordam</b>\n\n"
        "Quyida botning asosiy imkoniyatlari va ulardan foydalanish tartibi keltirilgan.\n\n"
        "1️⃣ <b>Google hisobini ulash</b>\n"
        "   • Bot Google Sheets va Google Drive bilan ishlashi uchun avval hisobni bog‘lash kerak.\n"
        "   • Buning uchun: <b>/connect</b> buyrug‘ini bosing.\n"
        "   • Ochilgan Google oynasida ruxsat so‘rovlarini diqqat bilan o‘qib, <b>Allow / Разрешить</b> tugmasini bosing.\n\n"
        "2️⃣ <b>⭐️ Sevimlilar bo‘limi</b>\n"
        "   Ushbu bo‘lim orqali tez-tez ishlatiladigan jadvallarni boshqarasiz:\n"
        "   • <b>📂 Fayllar ro‘yxati</b> – Google Drive’dagi eng so‘nggi 10 ta Google Sheets hujjatini ko‘rish.\n"
        "   • <b>➕ Yangi fayl yaratish</b> – yangi jadval yaratib, uni avtomatik ravishda sevimlilarga qo‘shish.\n"
        "   • <b>🗑 Faylni o‘chirish</b> – Google Drive’dan kerakli jadvalni butunlay o‘chirish.\n"
        "   • <b>🔗 Linkdan ma’lumot olish</b> – tashqi Google Sheets havolasi bo‘yicha varaqlar soni va\n"
        "     A1:Z20 oralig‘ida ma’lumot bor-yo‘qligi bo‘yicha qisqa statistika.\n\n"
        "3️⃣ <b>Token va ruxsatlar bilan bog‘liq muammolar</b>\n"
        "   • Agar bot «ruxsat yetarli emas» yoki <code>insufficientPermissions</code> xatolarini qaytarsa —\n"
        "     bu odatda Google hisobini qayta ulash kerakligini anglatadi.\n"
        "   • Buning uchun: <b>/connect</b> buyrug‘ini qaytadan bosing va barcha ruxsatlarni tasdiqlang.\n\n"
        "4️⃣ <b>Xatolik yuzaga kelsa</b>\n"
        "   • Ekran tasviri (screenshot) va bot yuborgan xatolik matnini saqlab qo‘ying.\n"
        "   • Texnik yordamga yuborishda ushbu ma’lumotlar muammoni tezroq aniqlashga yordam beradi.\n\n"
        "Agar ma’lum bir bo‘lim bo‘yicha alohida savolingiz bo‘lsa, shu yerning o‘zida yozishingiz mumkin. ✅"
    )

    # Pastda kichik inline “Bosh menyu” tugmasi – ixtiyoriy
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu")],
    ])

    await msg.answer(text, reply_markup=inline_kb, parse_mode="HTML")
    # Agar reply-keyboard bosh menyuni ham qayta ko‘rsatmoqchi bo‘lsang:
    # await msg.answer("Bosh menyudan kerakli bo‘limni tanlang 👇", reply_markup=MAIN_MENU_KB)
