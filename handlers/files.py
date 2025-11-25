# handlers/files.py
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from googleapiclient.discovery import build

from google_oauth import get_credentials_for_tg
from db import (
    set_last_file_id_by_tg,
    add_favorite_file,
    list_favorite_files,
)
from handlers.sheet_wizard import Wiz
from utils.keyboards import MAIN_MENU_KB  # bosh menyu uchun import

router = Router(name="files")


# --- Google Drive’dan oxirgi Google Sheets fayllarni olish ---
def _drive_list_recent_spreadsheets(creds, page_size: int = 10):
    service = build("drive", "v3", credentials=creds)
    return service.files().list(
        q="mimeType='application/vnd.google-apps.spreadsheet'",
        orderBy="modifiedTime desc",
        pageSize=page_size,
        fields="files(id,name,webViewLink)"
    ).execute()


# --- “📂 Mening fayllarim” ---
@router.message(F.text == "📂 Mening fayllarim")
async def my_files(msg: Message, state: FSMContext):
    """
    Tepada:
      ⭐️ Sevimli fayllar (DB dan)
    Pastda:
      Oxirgi marta ishlatilgan Google Sheets fayllar (Drive’dan)
    Umumiy ro‘yxat: 10 ta (sevimlilar + oxirgi ishlatilganlar)
    """
    creds = await get_credentials_for_tg(msg.from_user.id)
    if not creds:
        await msg.answer("Avval Google hisobingizni ulang: /connect")
        return

    # 1) Sevimlilarni DB dan olamiz
    try:
        fav_rows = await list_favorite_files(msg.from_user.id)
    except Exception as e:
        print("my_files list_favorite_files error:", e)
        fav_rows = []

    fav_map = {}
    for row in fav_rows:
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
        fav_map[fid] = {
            "id": fid,
            "name": name,
            "link": f"https://docs.google.com/spreadsheets/d/{fid}/edit",
            "is_fav": True,
        }

    # 2) Google Drive’dan oxirgi fayllarni olamiz
    try:
        res = _drive_list_recent_spreadsheets(creds, page_size=10)
        drive_files = res.get("files", []) or []
    except Exception as e:
        print("my_files drive_list error:", e)
        await msg.answer("❌ Google Drive’dan fayllarni olishda xatolik yuz berdi.")
        return

    recent_map = {}
    for f in drive_files:
        fid = f.get("id")
        if not fid:
            continue
        name = f.get("name") or fid
        link = f.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{fid}/edit"
        recent_map[fid] = {
            "id": fid,
            "name": name,
            "link": link,
            "is_fav": fid in fav_map,
        }

    # 3) Birlashtiramiz: sevimlilar tepada, keyin oxirgi ishlatilganlar
    merged: list[dict] = []
    seen = set()

    # Avval sevimlilar
    for fid, info in fav_map.items():
        merged.append(info)
        seen.add(fid)

    # Keyin oxirgi ishlatilganlar (sevimli bo‘lmaganlari)
    for fid, info in recent_map.items():
        if fid in seen:
            continue
        merged.append(info)
        seen.add(fid)

    # Umumiy ro‘yxatni 10 taga cheklaymiz
    merged = merged[:10]

    if not merged:
        await msg.answer("📭 Sizning Google Drive’ingizda Google Sheets fayli topilmadi.")
        return

    # 4) Keyboard yasaymiz
    rows = []
    for info in merged:
        fid = info["id"]
        name = info["name"]
        is_fav = info["is_fav"]

        title = f"{'⭐️ ' if is_fav else ''}{name[:30]}"

        # 1-tugma: faylni tanlash (wizard uchun)
        open_btn = InlineKeyboardButton(
            text=title,
            callback_data=f"open:{fid}"
        )
        rows.append([open_btn])

    rows.append([
        InlineKeyboardButton(text="🏠 Bosh menyuga qaytish", callback_data="go_main_menu"),
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    picker = await msg.answer(
        "📂 <b>Mening fayllarim</b>\n\n"
        "⭐️ Sevimli fayllar tepada, qolganlari esa oxirgi marta ishlatilgan Google Sheets fayllaridir.\n"
        "Kerakli faylni tanlang:",
        reply_markup=kb,
        parse_mode="HTML"
    )

    await state.set_state(Wiz.picking_file)
    await state.update_data(picker_msg_id=picker.message_id)


# --- Faylni sevimlilarga qo‘shish (📂 Mening fayllarim ro‘yxatidan) ---
@router.callback_query(F.data.startswith("fav:add:"))
async def fav_add_from_list(cb: CallbackQuery, state: FSMContext):
    await cb.answer("⭐️ Sevimlilarga qo‘shildi")
    file_id = cb.data.split(":", 2)[2]

    creds = await get_credentials_for_tg(cb.from_user.id)
    if not creds:
        await cb.message.answer("Avval Google hisobingizni ulang: /connect")
        return

    try:
        drive = build("drive", "v3", credentials=creds)
        info = drive.files().get(fileId=file_id, fields="id,name").execute()
        name = info.get("name") or file_id
    except Exception as e:
        print("fav_add_from_list error:", e)
        await cb.message.answer("❌ Fayl ma'lumotlarini olishda xatolik yuz berdi.")
        return

    await add_favorite_file(cb.from_user.id, file_id, name)
    # Xabar yozmasak ham bo‘ladi, callback answer yetarli; hohlasangiz qo‘shimcha yozish mumkin


# --- 🏠 Bosh menyuga qaytish ---
@router.callback_query(F.data == "go_main_menu")
async def go_main_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer(
        "🏠 Siz bosh menyuga qaytdingiz.\nQuyidagi menyudan kerakli bo‘limni tanlang:",
        reply_markup=MAIN_MENU_KB
    )
    await cb.answer()
