# handlers/favorites.py
from __future__ import annotations

import re
import asyncio
from typing import List, Dict, Optional

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from googleapiclient.discovery import build

from google_oauth import ensure_valid_credentials
from db import list_favorite_files, add_favorite_file, remove_favorite_file  # ✅ sevimlilar DB
from utils.keyboards import MAIN_MENU_KB

router = Router(name="favorites")


# ---------- STATES ----------
class FavStates(StatesGroup):
    waiting_info_link = State()   # 🔗 Linkdan ma’lumot uchun link


# ---------- YORDAMCHI FUNKSIYALAR ----------

async def _get_creds_or_reply(message: Message | CallbackQuery) -> Optional["Credentials"]:
    """
    Tokenni tekshiradi, kerak bo'lsa refresh qiladi.
    Agar umuman topilmasa, userga /connect ni eslatadi.
    """
    from_user = message.from_user if isinstance(message, Message) else message.from_user
    creds = await ensure_valid_credentials(from_user.id)
    msg = message if isinstance(message, Message) else message.message

    if not creds:
        await msg.answer("Avval Google hisobingizni ulang: /connect")
        return None
    return creds


async def _drive_get_file(creds, file_id: str, fields: str):
    def _work():
        service = build("drive", "v3", credentials=creds)
        return service.files().get(fileId=file_id, fields=fields).execute()

    return await asyncio.to_thread(_work)


async def _drive_list_recent_spreadsheets(creds, page_size: int = 10):
    """
    Google Drive’dagi eng so‘nggi N ta Google Sheets fayl (modifiedTime desc).
    """
    def _work():
        service = build("drive", "v3", credentials=creds)
        return service.files().list(
            q="mimeType='application/vnd.google-apps.spreadsheet'",
            orderBy="modifiedTime desc",
            pageSize=page_size,
            fields="files(id,name,webViewLink)"
        ).execute()

    return await asyncio.to_thread(_work)


async def _sheets_get_meta(creds, spreadsheet_id: str):
    def _work():
        service = build("sheets", "v4", credentials=creds)
        return service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

    return await asyncio.to_thread(_work)


async def _send_favorites_list(message: Message, user_id: int) -> None:
    """
    Bazadagi sevimli fayllar ro'yxatini chiqaradi.
    ⭐️ Sevimli Google Sheets fayllaringiz:
    """
    try:
        files = await list_favorite_files(user_id)
    except Exception as e:
        print("list_favorite_files error:", e)
        await message.answer("❌ Sevimli fayllar ro'yxatini olishda xatolik yuz berdi.")
        return

    rows = []

    if not files:
        # Sevimlilar bo'sh bo'lsa ham yangi qo‘shish menyusi bo‘lsin
        rows.append([
            InlineKeyboardButton(
                text="➕ Sevimlilarga yangi ish varaq qo‘shish",
                callback_data="fav:new_add_menu"
            ),
        ])
        rows.append([
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="fav:back_main"),
            InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
        ])

        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await message.answer(
            "📭 Hozircha sevimli Google Sheets fayllaringiz yo‘q.\n\n"
            "➕ Tugma orqali yangi ish varaqni sevimlilarga qo‘shishingiz mumkin.",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return

    # Sevimlilar ro‘yxati
    for row in files:
        fid = (
            row.get("file_id")
            or row.get("fileid")
            or row.get("google_id")
            or row.get("id")
        )
        name = (
            row.get("title")
            or row.get("name")
            or row.get("file_name")
            or fid
        )

        if not fid:
            continue

        link = (
            row.get("webViewLink")
            or row.get("webview_link")
            or f"https://docs.google.com/spreadsheets/d/{fid}/edit"
        )

        # ⭐️ sevimli fayl + 🗑 sevimlilardan o‘chirish (tezkor)
        rows.append([
            InlineKeyboardButton(
                text=f"⭐️ {name[:35]}",
                url=link
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Orqaga", callback_data="fav:back_main"),
        InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        "⭐️ <b>Sevimli Google Sheets fayllaringiz</b>:\n\n"
        "📂 <b>Mening fayllarim</b> bo‘limida ham ushbu ro‘yxat yuqorida ko‘rinadi.",
        reply_markup=kb,
        parse_mode="HTML"
    )


# ---------- Sevimlilar bosh menyusi ----------
async def _send_favorites_home(message: Message, state: FSMContext) -> None:
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📂 Sevimli fayllar ro‘yxati", callback_data="fav:list"),
        ],
        [
            InlineKeyboardButton(
                text="➕ Sevimlilarga yangi ish varaq qo‘shish",
                callback_data="fav:new_add_menu"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🗑 Sevimlilar ro‘yxatini o‘chirish",
                callback_data="fav:delete_menu"
            ),
        ],
        [
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="fav:back_main"),
            InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu"),
        ],
    ])

    text = (
        "⭐️ <b>Sevimli Google Sheets fayllari bo‘limi</b>\n\n"
        "Bu bo‘lim yordamida tez-tez foydalaniladigan jadvallarni qulay boshqarishingiz mumkin.\n\n"
        "Mavjud imkoniyatlar:\n"
        "• <b>📂 Sevimli fayllar ro‘yxati</b> – bazaga saqlangan sevimli Google Sheets fayllaringiz.\n"
        "• <b>➕ Sevimlilarga yangi ish varaq qo‘shish</b> – hozir ishlatilgan fayllar yoki link orqali sevimlilarga qo‘shish.\n"
        "• <b>🗑 Sevimlilar ro‘yxatini o‘chirish</b> – sevimli fayllardan birini tanlab o‘chirish.\n\n"
        "Kerakli amaliyotni pastdagi tugmalar orqali tanlang."
    )

    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------- KIRISH: “⭐️ Sevimlilar” ----------
@router.message(F.text == "⭐️ Sevimlilar")
async def favorites_entry(msg: Message, state: FSMContext):
    await _send_favorites_home(msg, state)


@router.callback_query(F.data == "fav:back_main")
async def favorites_back_main(cb: CallbackQuery, state: FSMContext):
    await cb.answer("⬅️ Orqaga")
    await _send_favorites_home(cb.message, state)


# ============================================================
# 1) 📂 Sevimli fayllar ro‘yxati — faqat BAZADAN
# ============================================================
@router.callback_query(F.data == "fav:list")
async def favorites_file_list(cb: CallbackQuery, state: FSMContext):
    await cb.answer("📂 Sevimli fayllar ro‘yxati")
    await _send_favorites_list(cb.message, cb.from_user.id)


# ============================================================
# 2) ➕ Sevimlilarga yangi ish varaq qo‘shish (meni)
# ============================================================
@router.callback_query(F.data == "fav:new_add_menu")
async def favorites_new_add_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer("➕ Sevimlilarga qo‘shish menyusi")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📂 Oxirgi marta ishlatilgan fayllardan tanlash",
                callback_data="fav:add_from_recent"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔗 Link yuborib qo‘shish",
                callback_data="fav:send_info"
            ),
        ],
        [
            InlineKeyboardButton(text="⬅️ Sevimlilar ro‘yxati", callback_data="fav:list"),
            InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
        ],
    ])

    await cb.message.answer(
        "➕ <b>Sevimlilarga yangi ish varaq qo‘shish</b>\n\n"
        "1️⃣ Oxirgi marta ishlatilgan Google Sheets fayllardan birini tanlab qo‘shishingiz mumkin.\n"
        "2️⃣ Yoki Google Sheets linkini yuborib, sevimlilarga qo‘shishingiz mumkin.",
        reply_markup=kb,
        parse_mode="HTML"
    )


# ---------- 2.1) Oxirgi marta ishlatilgan fayllardan tanlash ----------
@router.callback_query(F.data == "fav:add_from_recent")
async def favorites_add_from_recent(cb: CallbackQuery, state: FSMContext):
    await cb.answer("📂 Oxirgi fayllar")
    creds = await _get_creds_or_reply(cb)
    if not creds:
        return

    try:
        res = await _drive_list_recent_spreadsheets(creds, page_size=10)
        files = res.get("files", []) or []
    except Exception as e:
        print("favorites_add_from_recent error:", e)
        await cb.message.answer("❌ Google Drive’dan oxirgi fayllarni olishda xatolik yuz berdi.")
        return

    if not files:
        await cb.message.answer("📭 Google Drive’da Google Sheets formatidagi fayl topilmadi.")
        return

    rows = []
    for f in files:
        fid = f["id"]
        name = f.get("name") or fid
        rows.append([
            InlineKeyboardButton(
                text=f"{name[:40]}",
                callback_data=f"fav:add_recent_one:{fid}"
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Sevimlilar menyusi", callback_data="fav:new_add_menu"),
        InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.answer(
        "📂 <b>Oxirgi marta ishlatilgan Google Sheets fayllar</b>\n\n"
        "Sevimlilarga qo‘shmoqchi bo‘lgan faylni tanlang:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("fav:add_recent_one:"))
async def favorites_add_recent_one(cb: CallbackQuery, state: FSMContext):
    await cb.answer("⭐️ Sevimlilarga qo‘shildi")
    file_id = cb.data.split(":", 2)[2]

    creds = await _get_creds_or_reply(cb)
    if not creds:
        return

    try:
        info = await _drive_get_file(creds, file_id, fields="id,name")
        name = info.get("name") or file_id
    except Exception as e:
        print("favorites_add_recent_one error:", e)
        await cb.message.answer("❌ Fayl ma'lumotlarini olishda xatolik yuz berdi.")
        return

    try:
        await add_favorite_file(cb.from_user.id, file_id, name)
    except Exception as e:
        print("add_favorite_file error:", e)
        await cb.message.answer("❌ Faylni sevimlilarga qo‘shib bo‘lmadi.")
        return

    await cb.message.answer(
        f"✅ <b>{name}</b> sevimli fayllaringizga qo‘shildi.\n"
        f"Quyida yangilangan sevimli fayllar ro‘yxati:",
        parse_mode="HTML"
    )
    await _send_favorites_list(cb.message, cb.from_user.id)


# ============================================================
# 3) 🔗 Linkdan ma’lumot olish → bazaga saqlash + ro‘yxatni chiqarish
# ============================================================
@router.callback_query(F.data == "fav:send_info")
async def favorites_send_info_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer("🔗 Linkdan ma’lumot")
    await state.set_state(FavStates.waiting_info_link)
    await cb.message.answer(
        "🔗 Iltimos, Google Sheets faylning to‘liq linkini yuboring.\n\n"
        "Masalan:\n"
        "<code>https://docs.google.com/spreadsheets/d/XXXXXXXXXXXXXXX/edit</code>",
        parse_mode="HTML"
    )


@router.message(FavStates.waiting_info_link)
async def favorites_send_info_finish(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()
    await state.clear()

    # Linkdan fayl ID sini ajratib olamiz
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_\-]+)/", text)
    if not m:
        await msg.answer("❌ Linkni tushunmadim. Iltimos, to‘liq Google Sheets linkini yuboring.")
        return

    file_id = m.group(1)

    # Google bilan bog'lanish (fayl nomini olish uchun)
    creds = await _get_creds_or_reply(msg)
    if not creds:
        return

    try:
        meta = await _sheets_get_meta(creds, file_id)
    except Exception as e:
        print("favorites_send_info_finish meta error:", e)
        await msg.answer("❌ Fayl ma'lumotlarini olishda xatolik yuz berdi. Linkni tekshiring.")
        return

    file_title = meta.get("properties", {}).get("title", "Noma’lum fayl")

    # ✅ Faylni sevimlilarga bazaga saqlaymiz
    try:
        await add_favorite_file(msg.from_user.id, file_id, file_title)
    except Exception as e:
        print("add_favorite_file error:", e)
        await msg.answer("❌ Faylni bazaga sevimlilar qatoriga qo‘shib bo‘lmadi.")
        return

    # ✅ Endi sevimli fayllar ro'yxatini chiqaramiz
    await msg.answer(
        f"✅ <b>{file_title}</b> sevimli fayllaringizga qo‘shildi.\n"
        f"Quyida barcha sevimli fayllar ro‘yxati:",
        parse_mode="HTML"
    )
    await _send_favorites_list(msg, msg.from_user.id)


# ============================================================
# 4) 🗑 Sevimlilardan O‘CHIRISH (tezkor)
# ============================================================
@router.callback_query(F.data.startswith("fav:remove:"))
async def favorites_remove_one(cb: CallbackQuery, state: FSMContext):
    await cb.answer("🗑 Sevimlilardan o‘chirildi")
    file_id = cb.data.split(":", 2)[2]

    try:
        await remove_favorite_file(cb.from_user.id, file_id)
    except Exception as e:
        print("favorites_remove_one error:", e)
        await cb.message.answer("❌ Sevimlilardan o‘chirishda xatolik yuz berdi.")
        return

    await cb.message.answer(
        "✅ Fayl sevimlilar ro‘yxatidan o‘chirildi.\n"
        "Yangilangan sevimlilar ro‘yxati:",
        parse_mode="HTML"
    )
    await _send_favorites_list(cb.message, cb.from_user.id)


# ============================================================
# 5) 🗑 Sevimlilar ro‘yxatini O‘CHIRISH menyusi (alohida oqim)
# ============================================================
@router.callback_query(F.data == "fav:delete_menu")
async def favorites_delete_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer("🗑 Sevimlilardan o‘chirish")
    try:
        files = await list_favorite_files(cb.from_user.id)
    except Exception as e:
        print("favorites_delete_menu error:", e)
        await cb.message.answer("❌ Sevimlilar ro‘yxatini olishda xatolik yuz berdi.")
        return

    if not files:
        await cb.message.answer(
            "📭 Sevimli fayllaringiz yo‘q, o‘chirishga hech narsa yo‘q.",
        )
        return

    rows = []
    for row in files:
        fid = (
            row.get("file_id")
            or row.get("fileid")
            or row.get("google_id")
            or row.get("id")
        )
        name = (
            row.get("title")
            or row.get("name")
            or row.get("file_name")
            or fid
        )

        if not fid:
            continue

        rows.append([
            InlineKeyboardButton(
                text=f"⭐️ {name[:40]}",
                callback_data=f"fav:delete_one:{fid}"
            )
        ])

    rows.append([
        InlineKeyboardButton(text="⬅️ Sevimlilar menyusi", callback_data="fav:back_main"),
        InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await cb.message.answer(
        "🗑 <b>Sevimlilar ro‘yxatidan o‘chirish</b>\n\n"
        "Qaysi sevimli faylni o‘chirib yubormoqchisiz? Faylni tanlang:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("fav:delete_one:"))
async def favorites_delete_one(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    file_id = cb.data.split(":", 2)[2]

    # Fayl nomini DB dagi ro‘yxatdan topishga urinib ko‘ramiz
    name = file_id
    try:
        files = await list_favorite_files(cb.from_user.id)
        for row in files:
            fid = (
                row.get("file_id")
                or row.get("fileid")
                or row.get("google_id")
                or row.get("id")
            )
            if fid == file_id:
                name = (
                    row.get("title")
                    or row.get("name")
                    or row.get("file_name")
                    or file_id
                )
                break
    except Exception:
        pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Ha, sevimlilardan o‘chir",
                callback_data=f"fav:delete_confirm:{file_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Bekor qilish",
                callback_data="fav:delete_menu"
            )
        ],
    ])

    await cb.message.answer(
        "⚠️ <b>Diqqat!</b>\n\n"
        f"Quyidagi faylni <b>sevimlilar ro‘yxatidan</b> o‘chirayotgan bo‘lasiz:\n"
        f"📄 <b>{name}</b>\n\n"
        "Tasdiqlaysizmi?",
        reply_markup=kb,
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("fav:delete_confirm:"))
async def favorites_delete_confirm(cb: CallbackQuery, state: FSMContext):
    await cb.answer("🗑 Sevimlilardan o‘chirildi")
    file_id = cb.data.split(":", 2)[2]

    try:
        await remove_favorite_file(cb.from_user.id, file_id)
    except Exception as e:
        print("favorites_delete_confirm error:", e)
        await cb.message.answer("❌ Sevimlilardan o‘chirishda xatolik yuz berdi.")
        return

    await cb.message.answer(
        "✅ Tanlangan fayl sevimlilar ro‘yxatidan o‘chirildi.\n"
        "Yangilangan sevimlilar ro‘yxati:",
        parse_mode="HTML"
    )
    await _send_favorites_list(cb.message, cb.from_user.id)
