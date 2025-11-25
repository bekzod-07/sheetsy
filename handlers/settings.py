# handlers/settings.py
from __future__ import annotations

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from googleapiclient.discovery import build

from google_oauth import (
    get_credentials_for_tg,
    ensure_valid_credentials,
    build_auth_url,
)
from db import (
    delete_token_by_tg,
    get_user_by_tg,
    set_user_language_by_tg,
)
from utils.keyboards import MAIN_MENU_KB

router = Router(name="settings")

# ==========================
#  Til helperlari
# ==========================

LANG_CHOICES = [
    ("uz", "🇺🇿 O‘zbekcha"),
    ("ru", "🇷🇺 Русский"),
    ("en", "🇺🇸 English"),
]


def _lang_codes_from_db(value: str) -> list[str]:
    """
    DB dagi language = 'uz,ru,en' bo‘lsa -> ['uz', 'ru', 'en']
    bo‘sh bo‘lsa -> []
    """
    if not value:
        return []
    return [part.strip().lower() for part in value.split(",") if part.strip()]


def _human_lang_list(db_value: str) -> str:
    """
    DB dagi 'uz,ru' ni:
    '🇺🇿 O‘zbekcha, 🇷🇺 Русский' ko‘rinishida qaytaradi.
    Hech narsa bo‘lmasa O‘zbekcha deb olamiz.
    """
    codes = _lang_codes_from_db(db_value)
    if not codes:
        return "🇺🇿 O‘zbekcha"

    names = []
    for code in codes:
        for c, label in LANG_CHOICES:
            if c == code:
                names.append(label)
                break
    if not names:
        return "🇺🇿 O‘zbekcha"
    return ", ".join(names)


def _build_lang_kb(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for code, label in LANG_CHOICES:
        mark = " ✅" if code in selected else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{label}{mark}",
                callback_data=f"settings:lang_toggle:{code}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Sozlamalarga qaytish", callback_data="settings:home"),
        InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_lang_text(selected: list[str]) -> str:
    db_value = ",".join(selected)
    human = _human_lang_list(db_value)
    return (
        "🌐 <b>Til sozlamalari</b>\n\n"
        f"Hozirgi tillar: <b>{human}</b>\n\n"
        "Bu bot bilan qaysi tillarda ishlashingizni bildiradi.\n"
        "Quyidagi tugmalar orqali bir nechta tilni tanlashingiz mumkin "
        "(kamida bitta til qolishi kerak)."
    )


# ==========================
# ⚙️ Sozlamalar bosh menyu
# ==========================
async def _send_settings_home(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📡 Holatni ko‘rish", callback_data="settings:status"),
        ],
        [
            InlineKeyboardButton(text="🌐 Til sozlamalari", callback_data="settings:lang"),
        ],
        [
            InlineKeyboardButton(text="🔄 Google hisobini almashtirish", callback_data="settings:change_acc"),
        ],
        [
            InlineKeyboardButton(text="🔄 Ma’lumotlarni yangilash", callback_data="settings:refresh"),
        ],
        [
            InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu"),
        ],
    ])


    text = (
        "⚙️ <b>Sozlamalar bo‘limi</b>\n\n"
        "Bu bo‘limda bot bilan ishlash uchun asosiy parametrlarni boshqarasiz:\n\n"
        "1️⃣ <b>📡 Holatni ko‘rish</b> – Google hisobga ulanish holati, aktiv token va joriy email.\n"
        "2️⃣ <b>🌐 Til sozlamalari</b> – bot bilan qaysi tillarda ishlayotganingizni ko‘rish va o‘zgartirish.\n"
        "3️⃣ <b>🔄 Google hisobini almashtirish</b> – boshqa Google akkauntga ulash.\n\n"
        "Quyidagi tugmalardan birini tanlang 👇"
    )

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# “⚙️ Sozlamalar” tugmasi va /settings komandasi
@router.message(F.text == "⚙️ Sozlamalar")
@router.message(F.text == "/settings")
async def settings_entry(msg: Message):
    await _send_settings_home(msg)


# Inline callback orqali ham qayta ochish
@router.callback_query(F.data == "settings:home")
async def settings_home_cb(cb: CallbackQuery):
    await cb.answer()
    await _send_settings_home(cb.message)


# ==========================
# 1) 📡 Holatni ko‘rish
# ==========================
@router.callback_query(F.data == "settings:status")
async def settings_status(cb: CallbackQuery):
    await cb.answer("📡 Holat")

    tg_id = cb.from_user.id

    # DB dagi user ma'lumotlari (til uchun)
    user_row = await get_user_by_tg(tg_id)
    raw_lang = user_row["language"] if user_row else "uz"
    cur_lang_text = _human_lang_list(raw_lang)

    # Tokenlar holati
    raw_creds = await get_credentials_for_tg(tg_id)
    valid_creds = await ensure_valid_credentials(tg_id)

    # Ulangan email ni olishga harakat qilamiz
    linked_email = None
    linked_name = None
    if valid_creds:
        try:
            drive = build("drive", "v3", credentials=valid_creds)
            about = drive.about().get(fields="user(emailAddress,displayName)").execute()
            user_info = about.get("user") or {}
            linked_email = user_info.get("emailAddress")
            linked_name = user_info.get("displayName")
        except Exception:
            linked_email = None
            linked_name = None

    # Matnni yig'ish
    lines = []
    lines.append("📡 <b>Joriy holat</b>\n")

    # Google ulanish holati
    if not raw_creds:
        lines.append("• <b>Google ulanishi:</b> ulanmagan ❌")
    else:
        if valid_creds:
            lines.append("• <b>Google ulanishi:</b> aktiv ✅")
        else:
            lines.append("• <b>Google ulanishi:</b> token muddati tugagan ❗️")

    # Email
    if linked_email:
        if linked_name:
            lines.append(f"• <b>Ulangan akkaunt:</b> {linked_name} <code>&lt;{linked_email}&gt;</code>")
        else:
            lines.append(f"• <b>Ulangan akkaunt:</b> <code>{linked_email}</code>")
    else:
        if raw_creds:
            lines.append("• <b>Ulangan akkaunt:</b> emailni aniqlab bo‘lmadi (ruxsatlar cheklangan bo‘lishi mumkin).")
        else:
            lines.append("• <b>Ulangan akkaunt:</b> yo‘q")

    lines.append("")
    # Til holati
    lines.append(f"🌐 <b>Bot bilan ishlayotgan tillar:</b> {cur_lang_text}")
    lines.append("   (O‘zgartirish uchun “🌐 Til sozlamalari” tugmasini bosing.)\n")

    # Qayta ulash bo‘yicha hint
    if not raw_creds:
        lines.append(
            "Google xizmatlaridan foydalanish uchun avval hisobni ulang:\n"
            "• <b>/connect</b> buyrug‘ini kiriting yoki\n"
            "• “🔄 Google hisobini almashtirish” tugmasini bosing."
        )
    elif not valid_creds:
        lines.append(
            "Token muddati tugagan bo‘lishi mumkin.\n"
            "Hisobni qayta faollashtirish uchun: <b>/connect</b> yoki "
            "“🔄 Google hisobini almashtirish”."
        )

    text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌐 Tilni ko‘rish / o‘zgartirish", callback_data="settings:lang"),
        ],
        [
            InlineKeyboardButton(text="🔄 Google hisobini almashtirish", callback_data="settings:change_acc"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Sozlamalarga qaytish", callback_data="settings:home"),
        ],
        [
            InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu"),
        ],
    ])

    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ==========================
# 2) 🌐 Til sozlamalari (multi-select, /start dagidek)
# ==========================
@router.callback_query(F.data == "settings:lang")
async def settings_lang(cb: CallbackQuery):
    await cb.answer("🌐 Til")

    tg_id = cb.from_user.id
    user_row = await get_user_by_tg(tg_id)
    raw_lang = user_row["language"] if user_row else "uz"

    selected = _lang_codes_from_db(raw_lang)
    if not selected:
        selected = ["uz"]

    text = _build_lang_text(selected)
    kb = _build_lang_kb(selected)

    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# /lang komandasi – xuddi shu menyuni ochadi (message orqali)
@router.message(F.text == "/lang")
async def lang_command_entry(msg: Message):
    tg_id = msg.from_user.id
    user_row = await get_user_by_tg(tg_id)
    raw_lang = user_row["language"] if user_row else "uz"

    selected = _lang_codes_from_db(raw_lang)
    if not selected:
        selected = ["uz"]

    text = _build_lang_text(selected)
    kb = _build_lang_kb(selected)

    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("settings:lang_toggle:"))
async def settings_lang_toggle(cb: CallbackQuery):
    tg_id = cb.from_user.id
    code = cb.data.split(":", 2)[2]  # uz / ru / en

    # mavjud ro'yxatni o‘qiymiz
    user_row = await get_user_by_tg(tg_id)
    raw_lang = user_row["language"] if user_row else "uz"
    selected = _lang_codes_from_db(raw_lang)
    if not selected:
        selected = ["uz"]

    # toggle
    if code in selected:
        if len(selected) == 1:
            await cb.answer("Kamida bitta til qolishi kerak.", show_alert=True)
            return
        selected.remove(code)
    else:
        # maksimal 3 ta tilni cheklash mumkin
        if len(selected) >= 3:
            await cb.answer("Maksimal 3 ta til tanlash mumkin.", show_alert=True)
            return
        selected.append(code)

    # yangilangan ro'yxatni bazaga yozamiz
    new_value = ",".join(selected)
    await set_user_language_by_tg(tg_id, new_value)

    # matn + klaviaturani yangilash
    text = _build_lang_text(selected)
    kb = _build_lang_kb(selected)

    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.edit_reply_markup(reply_markup=kb)

    await cb.answer("Til sozlamalari yangilandi.")


# ==========================
# 3) 🔄 Google hisobini almashtirish
# ==========================
@router.message(F.text == "/change_acc")
async def change_acc_cmd(msg: Message):
    tg_id = msg.from_user.id
    auth_url, _ = build_auth_url(tg_id)

    text = (
        "🔄 <b>Google hisobini almashtirish</b>\n\n"
        "Quyidagilarni bajaring:\n"
        "1. <b>“🔗 Yangi hisobni ulash”</b> tugmasini bosing\n"
        "2. Brauzerda kerakli Google akkauntni tanlang\n"
        "3. Bot so‘rayotgan ruxsatlarni <b>Allow / Разрешить</b> orqali tasdiqlang\n\n"
        "Shundan so‘ng bot yangi akkaunt bilan ishlaydi."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Yangi hisobni ulash", url=auth_url)],
        [
            InlineKeyboardButton(text="⬅️ Sozlamalar", callback_data="settings:home"),
            InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
        ],
    ])

    await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "settings:change_acc")
async def settings_change_acc(cb: CallbackQuery):
    await cb.answer("🔄 Hisobni almashtirish")
    tg_id = cb.from_user.id
    auth_url, _ = build_auth_url(tg_id)

    text = (
        "🔄 <b>Google hisobini almashtirish</b>\n\n"
        "Agar hozir ulangan akkaunt o‘rniga boshqa Google akkauntni ulashni istasangiz:\n\n"
        "1. Pastdagi <b>“🔗 Yangi hisobni ulash”</b> tugmasini bosing\n"
        "2. Kerakli Google akkauntni tanlang\n"
        "3. Bot so‘rayotgan ruxsatlarni tasdiqlang\n\n"
        "Jarayon tugagach, bot yangi ulangan akkaunt bilan ishlaydi."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Yangi hisobni ulash", url=auth_url)],
        [
            InlineKeyboardButton(text="⬅️ Sozlamalarga qaytish", callback_data="settings:home"),
        ],
        [
            InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu"),
        ],
    ])

    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


# ==========================
# 4) ❌ Google hisobini uzish (MENYUDAN OLIB TASHLANGAN)
# ==========================
@router.callback_query(F.data == "settings:disconnect")
async def settings_disconnect(cb: CallbackQuery):
    """
    Tugma hozircha menyuda yo‘q, lekin kelajakda kerak bo‘lsa qo‘shib olish mumkin.
    """
    await cb.answer("❌ Uzish")

    tg_id = cb.from_user.id

    try:
        await delete_token_by_tg(tg_id)
        text = (
            "❌ <b>Google hisob bilan bog‘lanish uzildi.</b>\n\n"
            "• Saqlangan access token bazadan o‘chirildi.\n"
            "• Endi bot Google Sheets / Google Drive bilan ishlamaydi.\n\n"
            "Qaytadan ulash uchun istalgan vaqtda <b>/connect</b> yoki "
            "<b>“🔄 Google hisobini almashtirish”</b funksiyasidan foydalanishingiz mumkin."
        )
    except Exception:
        text = (
            "⚠️ <b>Google hisobini uzishda xatolik yuz berdi.</b>\n\n"
            "Keyinroq qayta urinib ko‘ring yoki texnik yordamga murojaat qiling."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Yangi hisobni ulash", callback_data="settings:change_acc"),
        ],
        [
            InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu"),
        ],
    ])

    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
from handlers.voice_append import refresh_local_cache
# ==========================
# 5) 🔄 Ma'lumotlarni yangilash
# ==========================
@router.callback_query(F.data == "settings:refresh")
async def settings_refresh(cb: CallbackQuery):
    await cb.answer("🔄 Yangilanmoqda...")

    tg_id = cb.from_user.id

    # Voice append modulidagi yangilash funksiyasini chaqiramiz
    sheet_title, status = await refresh_local_cache(tg_id)

    if sheet_title is None:
        await cb.message.answer(f"❌ {status}")
        return

    text = (
        f"🔄 <b>Ma'lumotlar yangilandi!</b>\n\n"
        f"🗂 Varaq: <b>{sheet_title}</b>\n"
        f"{status}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ Sozlamalarga qaytish", callback_data="settings:home"),
        ],
        [
            InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu"),
        ],
    ])

    await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")

