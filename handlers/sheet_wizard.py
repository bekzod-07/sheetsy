# handlers/sheet_wizard.py
"""
Yagona, to'liq va optimallashtirilgan handler fayli.
Asosiy maqsad: ro'yxatdan o'tish, Google bilan bog'lash, Google Sheets fayllarni tanlash
va interfeysni tozalash (eski yordamchi xabarlarni o'chirish) — hammasi optimal va ishonchli.
"""

import asyncio
from typing import List, Optional

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from googleapiclient.discovery import build

from db import (
    get_user_by_tg, create_user,
    set_last_file_id_by_tg, set_last_sheet_title_by_tg
)
from google_oauth import get_credentials_for_tg, build_flow, sign_state
from utils.keyboards import REQUEST_CONTACT_KB, sheets_kb, MAIN_MENU_KB

router = Router(name="sheet_wizard")

# ===== States =====
class Reg(StatesGroup):
    fio = State()
    languages = State()
    phone = State()


class Wiz(StatesGroup):
    picking_file = State()
    picking_sheet = State()
    ready_to_apply = State()


# ===== Config / Options =====
LANG_OPTIONS = [("uz", "O'zbekcha"), ("ru", "Русский"), ("en", "English")]
PAGE_SIZE = 10


# ===== Helpers =====
def _dbg(msg: str) -> None:
    print(f"[WIZ] {msg}")


def _lang_kb(selected: List[str]):
    rows = []
    for code, label in LANG_OPTIONS:
        mark = " ✅" if code in selected else ""
        rows.append([InlineKeyboardButton(text=f"{label}{mark}", callback_data=f"lang:toggle:{code}")])
    rows.append([InlineKeyboardButton(text="Tugatish", callback_data="lang:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _delete_old_messages(state: FSMContext, chat_id: int, bot):
    """Agar avvalgi header/picker xabar idlari state da saqlangan bo'lsa, o'chiradi."""
    data = await state.get_data()
    header_id = data.get("header_msg_id")
    picker_id = data.get("picker_msg_id")

    # Clear stored ids regardless of delete success (to avoid repeated attempts)
    await state.update_data(header_msg_id=None, picker_msg_id=None)

    async def _del(mid: Optional[int]):
        if not mid:
            return
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            # Silently ignore (message might be already deleted or not accessible)
            pass

    await asyncio.gather(_del(picker_id), _del(header_id))


# Blocking Drive calls -> run in thread to avoid blocking event loop
async def _drive_list_spreadsheets(creds, page_size=PAGE_SIZE):
    def _sync():
        drive = build("drive", "v3", credentials=creds)
        res = drive.files().list(
            q="mimeType='application/vnd.google-apps.spreadsheet'",
            orderBy="modifiedTime desc",
            pageSize=page_size,
            fields="files(id,name)"
        ).execute()
        return res.get("files", []) or []
    return await asyncio.to_thread(_sync)


async def _sheets_get_meta(creds, spreadsheet_id: str):
    def _sync():
        sheets = build("sheets", "v4", credentials=creds)
        meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        return meta
    return await asyncio.to_thread(_sync)


# ===== Start / Registration flow =====
@router.message(Command("start"))
async def entry_start(msg: Message, state: FSMContext):
    await state.clear()
    user = await get_user_by_tg(msg.from_user.id)

    if not user:
        await msg.answer("Salom! Iltimos, F.I.O ni kiriting:")
        await state.set_state(Reg.fio)
        return

    creds = await get_credentials_for_tg(msg.from_user.id)
    if not creds:
        await msg.answer("Google hisobingiz ulanmagan — iltimos /connect buyrug'ini bajaring.")
        return

    await msg.answer(
        "🎉 Xush kelibsiz!\nQuyidagi menyudan kerakli bo’limni tanlang:",
        reply_markup=MAIN_MENU_KB
    )


@router.message(Reg.fio)
async def reg_ask_languages(msg: Message, state: FSMContext):
    await state.update_data(fio=(msg.text or "").strip())
    await state.update_data(selected_langs=[])
    await msg.answer(
        "Ishlash tillarini tanlang (maksimum 3 ta). Tugallagach 'Tugatish' tugmasini bosing.",
        reply_markup=_lang_kb([])
    )
    await state.set_state(Reg.languages)


@router.callback_query(Reg.languages, F.data.startswith("lang:toggle:"))
async def reg_toggle_lang(cb: CallbackQuery, state: FSMContext):
    code = cb.data.split(":", 2)[2]
    data = await state.get_data()
    selected = data.get("selected_langs", []) or []
    if code in selected:
        selected.remove(code)
    else:
        if len(selected) >= 3:
            await cb.answer("Siz maksimal 3 ta til tanladingiz.", show_alert=True)
            return
        selected.append(code)
    await state.update_data(selected_langs=selected)
    await cb.message.edit_reply_markup(reply_markup=_lang_kb(selected))
    await cb.answer()


@router.callback_query(Reg.languages, F.data == "lang:done")
async def reg_finish_languages(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_langs", []) or []
    if not selected:
        await cb.answer("Kamida 1 ta til tanlang.", show_alert=True)
        return
    await cb.message.answer("Telefon raqamingizni yuboring (tugma orqali):", reply_markup=REQUEST_CONTACT_KB)
    await state.set_state(Reg.phone)
    await cb.answer()


@router.message(Reg.phone, F.contact)
async def reg_got_contact(msg: Message, state: FSMContext):
    data = await state.get_data()
    fio = data.get("fio")
    langs = data.get("selected_langs", []) or []
    phone = msg.contact.phone_number
    try:
        await create_user(
            tg_id=msg.from_user.id,
            full_name=fio,
            phone=phone,
            language=",".join(langs)
        )
    except Exception as e:
        _dbg(f"create_user err: {e}")
        await msg.answer("❌ Ma'lumotlarni saqlashda xatolik yuz berdi.")
        await state.clear()
        return

    await msg.answer("Ma’lumotlar saqlandi. Iltimos, Google hisobingizni ulang: /connect")
    await state.clear()


@router.message(Reg.phone)
async def reg_fallback_phone(msg: Message):
    await msg.answer("Iltimos, tugma orqali telefon raqam yuboring.")


# ===== Google connect (redirect flow) =====
@router.message(Command("connect"))
async def cmd_connect(msg: Message, state: FSMContext):
    await state.clear()
    creds = await get_credentials_for_tg(msg.from_user.id)
    if creds:
        return await _list_sheets(msg, state)

    try:
        flow = build_flow()
        state_token = sign_state(msg.from_user.id)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state_token
        )
    except Exception as e:
        _dbg(f"authorization_url error: {e}")
        return await msg.answer("❌ Google bilan bog‘lashda xatolik. Keyinroq urinib ko‘ring.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Google bilan bog‘lash", url=auth_url)]
    ])
    await msg.answer(
        "🔗 Google bilan bog‘lanish uchun pastdagi tugmani bosing.\n"
        "Ruxsat berganingizdan so‘ng bu yerga qayting va /start ni bosing.",
        reply_markup=kb
    )


# ===== List sheets (reusable, removes previous helper messages) =====
async def _list_sheets(msg: Message, state: FSMContext):
    _dbg(f"list_sheets by tg:{msg.from_user.id}")
    creds = await get_credentials_for_tg(msg.from_user.id)
    if not creds:
        return await msg.answer("Avval /connect qiling (Google bilan bog‘lanish).")

    # eski xabarlarni tozalash
    await _delete_old_messages(state, msg.chat.id, msg.bot)

    try:
        files = await _drive_list_spreadsheets(creds, page_size=PAGE_SIZE)
    except Exception as e:
        _dbg(f"Drive list error: {e}")
        return await msg.answer("❌ Google Drive’dan fayllarni olishda xatolik.")

    if not files:
        return await msg.answer("Google Drive’da Spreadsheet topilmadi.")

    rows = [
        [InlineKeyboardButton(text=f["name"][:60], callback_data=f"open:{f['id']}")]
        for f in files
    ]
    rows.append([
        InlineKeyboardButton(text="🔙 Ortga qaytish", callback_data="wizard:back_to_files"),
        InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    picker = await msg.answer(
        "📂 Quyidagi ro‘yxatdan kerakli Google Sheets faylini tanlang:",
        reply_markup=kb
    )

    await state.set_state(Wiz.picking_file)
    await state.update_data(picker_msg_id=picker.message_id)


# ===== Pick file -> list tabs (cleans old messages first) =====
@router.callback_query(Wiz.picking_file, F.data.startswith("open:"))
async def pick_file(cb: CallbackQuery, state: FSMContext):
    _dbg(f"Button clicked: {cb.data}")
    file_id = cb.data.split(":", 1)[1]

    await set_last_file_id_by_tg(cb.from_user.id, file_id)

    creds = await get_credentials_for_tg(cb.from_user.id)
    if not creds:
        _dbg("No creds on pick_file")
        await cb.answer("Avval /connect qiling", show_alert=True)
        return

    # eski xabarlarni tozalash
    await _delete_old_messages(state, cb.message.chat.id, cb.message.bot)

    try:
        meta = await _sheets_get_meta(creds, spreadsheet_id=file_id)
        sheet_props = meta.get("sheets", [])
    except Exception as e:
        _dbg(f"sheets.get error: {e}")
        await cb.answer("Google Sheets dan varaqlarni olishda xato yuz berdi.", show_alert=True)
        return

    if not sheet_props:
        await cb.answer("Varaq topilmadi", show_alert=True)
        return

    titles = [sh["properties"]["title"] for sh in sheet_props]
    await state.update_data(file_id=file_id, titles=titles)

    rows = [
        [InlineKeyboardButton(text=title[:60], callback_data=f"picksh:{i}")]
        for i, title in enumerate(titles)
    ]

    rows.append([
        InlineKeyboardButton(text="🔙 Fayllar ro‘yxatiga qaytish", callback_data="wizard:back_to_files"),
        InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await state.set_state(Wiz.picking_sheet)
    try:
        await cb.message.edit_text("Varaqni tanlang:", reply_markup=kb)
    except Exception:
        await cb.message.answer("Varaqni tanlang:", reply_markup=kb)
    await cb.answer()
    
from .voice_append import _detect_template_and_sources


# ===== Pick sheet -> confirm / set default =====
@router.callback_query(Wiz.picking_sheet, F.data.startswith("picksh:"))
async def pick_sheet(cb: CallbackQuery, state: FSMContext):
    _dbg(f"Button clicked: {cb.data}")
    data = await state.get_data()
    file_id = data.get("file_id")
    titles = data.get("titles", [])

    if not file_id:
        _dbg("pick_sheet -> file_id missing")
        await cb.answer("Ichki xato: fayl aniqlanmadi. Iltimos, /start qayta boshlang.", show_alert=True)
        return

    try:
        idx = int(cb.data.split(":", 1)[1])
    except Exception:
        await cb.answer("Noto‘g‘ri tanlov (index).", show_alert=True)
        return

    if idx < 0 or idx >= len(titles):
        await cb.answer("Noto‘g‘ri tanlov — ro‘yxatda bunday varaq yo‘q.", show_alert=True)
        return

    sheet_title = titles[idx]
    _dbg(f"Sheet selected: {sheet_title}")

    await state.update_data(sheet_title=sheet_title)
    try:
        await set_last_sheet_title_by_tg(cb.from_user.id, sheet_title)
    except Exception as e:
        _dbg(f"DB save last_sheet err: {e}")

    # 🔍 Shablondan maydonlarni avtomatik olish (Google Sheets bo‘yicha)
    fields_block = ""
    try:
        creds = await get_credentials_for_tg(cb.from_user.id)
        if creds:
            svc = build("sheets", "v4", credentials=creds)

            # voice_append dagi helper orqali shablonni aniqlaymiz
            _, _, _, _, cols_meta = _detect_template_and_sources(svc, file_id, sheet_title)

            EXCLUDE_TAGS = {"#FF", "#DT-A", "#TX-A", "#GA-A"}
            lines = []
            for i, m in enumerate(cols_meta, start=1):
                tag = (m.get("tag") or "").upper()
                if tag in EXCLUDE_TAGS:
                    continue
                label = (m.get("label") or "").strip()
                if not label:
                    continue
                lines.append(f"{str(i).zfill(2)}. {label}")

            if lines:
                fields_block = (
                    "\n\n<b>Bu varaqqa quyidagi maydonlar yoziladi:</b>\n"
                    + "\n".join(lines)
                )
    except Exception as e:
        _dbg(f"detect_template err: {e}")
        # Xato bo‘lsa ham bot ishlashda davom etadi, faqat ro‘yxat chiqmaydi
        fields_block = ""

    # 🔘 Tugmalar
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Shu varaqqa yozishni tanla", callback_data="set_default_sheet")],
        [
            InlineKeyboardButton(
                text="🔙 Orqaga (varaqlar ro‘yxati)",
                callback_data="wizard:back_to_sheets"
            ),
            InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
        ]
    ])

    await state.set_state(Wiz.ready_to_apply)

    text = (
        f"✅ <b>Varaq tanlandi!</b>\n\n"
        f"📄 <b>Tanlangan varaq:</b> <code>{sheet_title}</code>\n\n"
        "Endi shu varaqqa ma’lumotlar avtomatik tarzda yoziladi."
    )
    if fields_block:
        text += fields_block

    text += "\n\n📌 Quyidagi tugmani bosib tasdiqlang."

    try:
        await cb.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            text,
            reply_markup=kb,
            parse_mode="HTML"
        )
    await cb.answer()


@router.callback_query(Wiz.ready_to_apply, F.data == "set_default_sheet")
async def set_default_sheet(cb: CallbackQuery, state: FSMContext):
    _dbg("Button clicked: set_default_sheet")
    data = await state.get_data()
    file_id = data.get("file_id")
    sheet_title = data.get("sheet_title")
    if not file_id or not sheet_title:
        await cb.answer("Ichki xatolik: fayl yoki varoq topilmadi.", show_alert=True)
        return
    try:
        await set_last_sheet_title_by_tg(cb.from_user.id, sheet_title)
        await cb.message.answer(
            f"📌 Standart varoq sifatida tanlandi: *{sheet_title}*",
            parse_mode="Markdown"
        )
        _dbg(f"Default sheet saved: {sheet_title}")
    except Exception as e:
        _dbg(f"set_default_sheet error: {e}")
        await cb.message.answer(f"❌ Xatolik: {e}")
    await cb.answer()


# ===== UNIVERSAL MESSAGE CLEANER =====
async def _cleanup_chat(state: FSMContext, chat_id: int, bot, current_msg_id: Optional[int] = None):
    """
    Ishonchli tozalash:
      - state'dan header_msg_id va picker_msg_id o'qib, o'chiradi (agar mavjud bo'lsa)
      - keyin callback message (current_msg_id) o'chirilsa ham urinadi
      - oxirida state dagi idlarni tozalaydi
    """
    data = await state.get_data()
    header_id = data.get("header_msg_id")
    picker_id = data.get("picker_msg_id")

    # Build unique ids list
    ids = []
    for mid in (header_id, picker_id, current_msg_id):
        if isinstance(mid, int) and mid not in ids:
            ids.append(mid)

    for mid in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            # agar o'chirmasa davom etamiz
            pass

    # tozalaymiz
    await state.update_data(header_msg_id=None, picker_msg_id=None)


# 🆕 YANGI HANDLER: varaqlar ro'yxatiga qaytish
@router.callback_query(F.data == "wizard:back_to_sheets")
async def wizard_back_to_sheets(cb: CallbackQuery, state: FSMContext):
    """
    «Varaq tanlandi» ekranidan orqaga bosilganda
    — aynan shu faylning varaqlar ro'yxatiga qaytaradi.
    """
    await cb.answer("🔙 Varaqlar ro‘yxatiga qaytish")

    data = await state.get_data()
    file_id = data.get("file_id")
    titles = data.get("titles", [])

    # Agar ma'lumot yo'q bo'lsa — fayllar ro'yxatiga qaytamiz (fallback)
    if not file_id or not titles:
        await wizard_back_to_files(cb, state)
        return

    rows = [
        [InlineKeyboardButton(text=title[:60], callback_data=f"picksh:{i}")]
        for i, title in enumerate(titles)
    ]
    rows.append([
        InlineKeyboardButton(text="🔙 Fayllar ro‘yxatiga qaytish", callback_data="wizard:back_to_files"),
        InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    try:
        await cb.message.edit_text("Varaqni tanlang:", reply_markup=kb)
    except Exception:
        await cb.message.answer("Varaqni tanlang:", reply_markup=kb)

    await state.set_state(Wiz.picking_sheet)


# ===== NAV handlers =====
@router.callback_query(F.data == "wizard:back_to_files")
async def wizard_back_to_files(cb: CallbackQuery, state: FSMContext):
    await cb.answer("🔄 Roʻyxat yangilanmoqda...")

    creds = await get_credentials_for_tg(cb.from_user.id)
    if not creds:
        await cb.message.answer("Avval /connect qiling (Google bilan bog‘lanish).")
        return

    # tozalash: o'z callback xabarini ham o'chirishni urinadi
    await _cleanup_chat(state, cb.message.chat.id, cb.message.bot, current_msg_id=cb.message.message_id)

    try:
        files = await _drive_list_spreadsheets(creds, page_size=PAGE_SIZE)
    except Exception as e:
        _dbg(f"Drive list error (back_to_files): {e}")
        await cb.message.answer("❌ Google Drive’dan fayllarni olishda xatolik yuz berdi.")
        return

    if not files:
        await cb.message.answer("Google Drive’da Spreadsheet topilmadi.")
        return

    header = await cb.message.answer("Oxirgi Google Sheets fayllari:")
    rows = [
        [InlineKeyboardButton(text=f["name"][:60], callback_data=f"open:{f['id']}")]
        for f in files
    ]
    rows.append([
        InlineKeyboardButton(text="🔙 Ortga qaytish", callback_data="wizard:back_to_files"),
        InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="go_main_menu"),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    picker = await cb.message.answer(
        "📂 Quyidagi ro‘yxatdan kerakli Google Sheets faylini tanlang:",
        reply_markup=kb
    )

    await state.set_state(Wiz.picking_file)
    await state.update_data(header_msg_id=header.message_id, picker_msg_id=picker.message_id)


@router.callback_query(F.data == "go_main_menu")
async def go_main_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer("🏠 Bosh menyuga qaytildi")

    # tozalash (o'z callback xabarini ham o'chirishga harakat qiladi)
    await _cleanup_chat(state, cb.message.chat.id, cb.message.bot, current_msg_id=cb.message.message_id)

    await state.clear()
    await cb.message.answer(
        "🏠 Siz bosh menyuga qaytdingiz.\nQuyidagi menyudan kerakli bo’limni tanlang:",
        reply_markup=MAIN_MENU_KB
    )
