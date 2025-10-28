# handlers/sheet_wizard.py
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
from utils.keyboards import REQUEST_CONTACT_KB, sheets_kb

router = Router(name="sheet_wizard")

# ==========
#  Registration FSM
# ==========
LANG_OPTIONS = [("uz", "O'zbekcha"), ("ru", "Русский"), ("en", "English")]


class Reg(StatesGroup):
    fio = State()
    languages = State()
    phone = State()


def _lang_kb(selected: List[str]) -> InlineKeyboardMarkup:
    rows = []
    for code, label in LANG_OPTIONS:
        mark = " ✅" if code in selected else ""
        rows.append([InlineKeyboardButton(text=f"{label}{mark}", callback_data=f"lang:toggle:{code}")])
    rows.append([InlineKeyboardButton(text="Tugatish", callback_data="lang:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ==========
#  Wizard FSM (Sheets)
# ==========
class Wiz(StatesGroup):
    picking_file = State()
    picking_sheet = State()
    ready_to_apply = State()


# ==========
#  Helpers & Logging
# ==========
def _dbg(msg: str) -> None:
    print(f"[WIZ] {msg}")


# ==========
#  Start — registration check then wizard
# ==========
@router.message(Command("start"))
async def entry_start(msg: Message, state: FSMContext):
    await state.clear()
    user = await get_user_by_tg(msg.from_user.id)
    if not user:
        # yangi foydalanuvchi — ro'yxatga olish
        await msg.answer("Salom! Iltimos, F.I.O ni kiriting:")
        await state.set_state(Reg.fio)
        return

    # agar user bor — tekshir Google credential
    creds = await get_credentials_for_tg(msg.from_user.id)
    if not creds:
        await msg.answer("Google hisobingiz yo‘q — iltimos /connect bilan bog‘lang.")
        return

    # hammasi joyida — ko‘rsat fayllar ro'yxatini
    return await _list_sheets(msg, state)


# ==========
#  Registration handlers
# ==========
@router.message(Reg.fio)
async def reg_ask_languages(msg: Message, state: FSMContext):
    await state.update_data(fio=(msg.text or "").strip())
    await state.update_data(selected_langs=[])
    kb = _lang_kb([])
    await msg.answer("Ishlash tillarini tanlang (maksimum 3 ta). Tugallagach ‘Tugatish’ tugmasini bosing.", reply_markup=kb)
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

    await msg.answer("Ma'lumotlar saqlandi. Iltimos, Google hisobingizni ulang: /connect")
    await state.clear()


@router.message(Reg.phone)
async def reg_fallback_phone(msg: Message):
    await msg.answer("Iltimos, tugma orqali telefon raqam yuboring.")


# ==========
#  Google connect (redirect flow)
# ==========
@router.message(Command("connect"))
async def cmd_connect(msg: Message, state: FSMContext):
    await state.clear()
    creds = await get_credentials_for_tg(msg.from_user.id)
    if creds:
        # agar allaqachon bog'langan bo'lsa — ko'rsat fayllar
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


# ==========
#  List sheets (reusable)
# ==========
async def _list_sheets(msg: Message, state: FSMContext):
    _dbg(f"list_sheets by tg:{msg.from_user.id}")
    creds = await get_credentials_for_tg(msg.from_user.id)
    if not creds:
        return await msg.answer("Avval /connect qiling (Google bilan bog‘lanish).")

    try:
        drive = build("drive", "v3", credentials=creds)
        res = drive.files().list(
            q="mimeType='application/vnd.google-apps.spreadsheet'",
            orderBy="modifiedTime desc",
            pageSize=10,
            fields="files(id,name)"
        ).execute()
        files = res.get("files", []) or []
    except Exception as e:
        _dbg(f"Drive list error: {e}")
        return await msg.answer("❌ Google Drive’dan fayllarni olishda xatolik.")

    if not files:
        return await msg.answer("Google Drive’da Spreadsheet topilmadi.")

    header = await msg.answer("Oxirgi Google Sheets fayllari:")
    rows = [[InlineKeyboardButton(text=f["name"][:60], callback_data=f"open:{f['id']}")] for f in files]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    picker = await msg.answer("📂 Faylni tanlang:", reply_markup=kb)

    await state.set_state(Wiz.picking_file)
    await state.update_data(header_msg_id=header.message_id, picker_msg_id=picker.message_id)


# ==========
#  Pick file (shared by list and wizard)
# ==========
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

    sheets = build("sheets", "v4", credentials=creds)
    meta = sheets.spreadsheets().get(spreadsheetId=file_id).execute()
    sheet_props = meta.get("sheets", [])
    _dbg(f"pick_file -> {len(sheet_props)} tabs")
    if not sheet_props:
        await cb.answer("Varaq topilmadi", show_alert=True)
        return

    titles = [sh["properties"]["title"] for sh in sheet_props]
    await state.update_data(file_id=file_id, titles=titles)

    rows = [[InlineKeyboardButton(text=title[:60], callback_data=f"picksh:{i}")]
            for i, title in enumerate(titles)]

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await state.set_state(Wiz.picking_sheet)
    try:
        await cb.message.edit_text("Varaqni tanlang:", reply_markup=kb)
    except Exception:
        # fallback — send new message if edit fails
        await cb.message.answer("Varaqni tanlang:", reply_markup=kb)
    await cb.answer()


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
        await cb.answer("Eslatma: varaq tanlandi, ammo uni DB ga saqlashda muammo bo‘ldi.", show_alert=False)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Shu varaqqa yozishni tanla", callback_data="set_default_sheet")]
    ])

    await state.set_state(Wiz.ready_to_apply)

    try:
        await cb.message.edit_text(
            f"✅ <b>Varaq tanlandi!</b>\n\n"
            f"📄 <b>Tanlangan varaq:</b> <code>{sheet_title}</code>\n\n"
            "Endi shu varaqqa ma’lumotlar avtomatik tarzda yoziladi.\n\n"
            "📌 Quyidagi tugmani bosib tasdiqlang.",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            f"✅ Tanlandi: {sheet_title}\n\nIltimos, quyidagi tugmani bosing.",
            reply_markup=kb
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
        await cb.message.answer(f"📌 Standart varoq sifatida tanlandi: *{sheet_title}*", parse_mode="Markdown")
        _dbg(f"Default sheet saved: {sheet_title}")
    except Exception as e:
        _dbg(f"set_default_sheet error: {e}")
        await cb.message.answer(f"❌ Xatolik: {e}")
    await cb.answer()


@router.callback_query(Wiz.ready_to_apply, F.data == "wizard:back_to_files")
async def back_to_files(cb: CallbackQuery, state: FSMContext):
    _dbg("Button clicked: wizard:back_to_files")
    await state.clear()
    await cb.message.answer("Qaytish uchun /start ni qayta bosing.")
    await cb.answer()
