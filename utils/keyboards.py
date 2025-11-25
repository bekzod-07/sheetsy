from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

LANG_KB = types.InlineKeyboardMarkup(inline_keyboard=[
    [types.InlineKeyboardButton(text="🇺🇿 O'zbek", callback_data="lang:uz")],
    [types.InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru")],
    [types.InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en")],
])

def connect_kb(url: str) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="Google bilan ulan", url=url)]]
    )

def sheets_kb(pairs: list[tuple[str, str]]) -> types.InlineKeyboardMarkup:
    rows = []
    for fid, name in pairs:
        rows.append([types.InlineKeyboardButton(text=name[:60], callback_data=f"open:{fid}")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)

REQUEST_CONTACT_KB = types.ReplyKeyboardMarkup(
    keyboard=[[types.KeyboardButton(text="📞 Raqamni yuborish", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

MAIN_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📂 Mening fayllarim"),
            KeyboardButton(text="⭐️ Sevimlilar"),
        ],
        [
            KeyboardButton(text="⚙️ Sozlamalar"),
            KeyboardButton(text="❓ Yordam"),
        ],
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)