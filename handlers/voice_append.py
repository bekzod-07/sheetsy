import asyncio
import re
import time
import tempfile
from typing import Dict, List, Tuple, Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from rapidfuzz import process, fuzz

from ai.parse import extract_with_ai
from ai.parse import _iso_to_ddmmyyyy as _ai_iso_to_ddmmyyyy
from ai.parse import _normalize_date as _ai_normalize_date
from config import BASE_URL
from db import (
    get_last_file_id_by_tg,
    get_last_sheet_title_for_file_by_tg,
    get_user_by_tg,
    set_last_sheet_title_for_file_by_tg, get_google_email_by_tg
)
from google_oauth import get_credentials_for_tg
from stt_client import stt_long, stt_short

router = Router(name="voice_append")


# =========================
# FSM
# =========================
class VAdd(StatesGroup):
    awaiting_decision = State()
    editing_text = State()


# =========================
# String-safety helperlar
# =========================
_ZW = "\u200b"
_NBSP = "\u00a0"



def _s(x) -> str:
    return "" if x is None else str(x)


def _sstrip(x) -> str:
    return _s(x).strip()


def _to_lat(s: str) -> str:
    s = (s or "")
    s = s.replace(_ZW, "").replace(_NBSP, " ")
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"\s+", " ", s)
    return s.lower().strip()


def _tokens(s: str) -> List[str]:
    return [t for t in re.split(r"[^\w']+", _to_lat(s)) if t]


def _smart_match_plus(value: str, options: List[str]) -> str:
    """
    Tartib:
      1) TOKEN-CONTAINMENT (v butun token sifatida ichida) → (token_count DESC, length DESC)
      2) SUBSTRING-CONTAINMENT (v ichida) → (length DESC, token_count DESC)
      3) EXACT EQUALITY (normalize qilingan)
      4) FUZZY (WRatio, cutoff=50)
      5) TOKEN-OVERLAP (+ uzunlikka kichik bonus)
      6) FALLBACK: birinchi opsiya
    """
    if not options:
        return value or ""

    v = _to_lat(value)
    opts = [o for o in options if o]
    opts_lat = [_to_lat(o) for o in opts]

    # 1) token-containment
    candidates = []
    for idx, (o, ol) in enumerate(zip(opts, opts_lat)):
        ot = _tokens(ol)
        if v and v in ot:
            candidates.append(((len(ot), len(ol), -idx), o))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # 2) substring-containment
    containers = []
    for idx, (o, ol) in enumerate(zip(opts, opts_lat)):
        if v and v in ol:
            containers.append(((len(ol), len(_tokens(ol)), -idx), o))
    if containers:
        containers.sort(reverse=True)
        return containers[0][1]

    # 3) exact equality
    for o, ol in zip(opts, opts_lat):
        if v and v == ol:
            return o

    # 4) fuzzy
    try:
        hit = process.extractOne(v, opts, scorer=fuzz.WRatio, score_cutoff=50)
        if hit:
            return hit[0]
    except Exception:
        pass

    # 5) token-overlap
    vt = set(_tokens(v))
    if vt:
        best_o, best_sc = None, -1.0
        for o in opts:
            ot = set(_tokens(o))
            if not ot:
                continue
            sc = (len(vt & ot) / max(1, len(vt))) + (len(o) / 1000.0)
            if sc > best_sc:
                best_sc, best_o = sc, o
        if best_o:
            return best_o

    # 6) fallback
    return opts[0]


def _to_iso_date(s: str) -> str:
    s = _sstrip(s)
    if not s:
        return s
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$", s)
    if m:
        d, mth, y = m.groups()
        return f"{y}-{mth.zfill(2)}-{d.zfill(2)}"
    n = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if n:
        y, mth, d = n.groups()
        return f"{y}-{mth.zfill(2)}-{d.zfill(2)}"
    return s


def _fmt_ddmmyyyy(iso: str) -> str:
    try:
        y, m, d = _s(iso).split("-")
        return f"{d.zfill(2)}.{m.zfill(2)}.{y}"
    except Exception:
        return _s(iso) or ""


def _smart_match(value: str, options: List[str]) -> str:
    if not value or not options:
        return value or ""
    v = _s(value).strip().lower()
    for o in options:
        if v in _s(o).lower():
            return o
    try:
        best = process.extractOne(value, options, score_cutoff=50)
        return best[0] if best else value
    except Exception:
        return value


def _coerce_by_tag(tag: str, value: str, dd_options: List[str]) -> str:
    t = (tag or "").upper()

    if t.startswith("#DD"):
        if not dd_options:
            return ""
        base = (value or "").strip()
        return _smart_match_plus(base, dd_options)

    if t == "#DT":
        return _fmt_ddmmyyyy(_to_iso_date(value or ""))

    if t == "#NB":
        try:
            return str(int(float((value or "").replace(" ", "").replace("\u00A0", "").replace(",", "."))))
        except Exception:
            return ""

    if t == "#FF":
        return ""

    return value or ""


# === AI chiqishini shablon ustunlariga map qilish ===
def _labels_from_ai(ai_out: dict, cols_meta: List[dict]) -> Dict[str, str]:
    lv_ai: Dict[str, str] = dict(ai_out.get("LABEL_VALUES") or {})

    sem_pay = (ai_out.get("TOLOV_TURI") or "").strip()
    sem_cur = (ai_out.get("VALYUTA") or "").strip()
    sem_cat = (ai_out.get("HARAJAT") or "").strip()

    def _is_pay_label(lbl: str) -> bool:
        L = (lbl or "").lower()
        return ("to'lov turi" in L) or ("tolov turi" in L) or (L.strip() == "turi")

    def _is_cur_label(lbl: str) -> bool:
        L = (lbl or "").lower()
        return ("valyuta" in L) or ("currency" in L)

    def _is_cat_label(lbl: str) -> bool:
        L = (lbl or "").lower()
        return ("harajat" in L) or ("xarajat" in L) or ("kategoriya" in L) or ("toifa" in L) or ("category" in L)

    dd_fixed: Dict[str, str] = {}
    for m in cols_meta:
        tag = (m.get("tag") or "").upper()
        if not tag.startswith("#DD"):
            continue

        lbl = m.get("label") or ""
        opts = m.get("dd_options") or []
        if not opts:
            continue

        base = (lv_ai.get(lbl) or "").strip()
        if not base:
            if _is_pay_label(lbl) and sem_pay:
                base = sem_pay
            elif _is_cur_label(lbl) and sem_cur:
                base = sem_cur
            elif _is_cat_label(lbl) and sem_cat:
                base = sem_cat

        snapped = _smart_match_plus(base, opts) if base else opts[0]
        print(f"[DD-MAP] lbl='{lbl}' base='{base}' -> snapped='{snapped}'")
        dd_fixed[lbl] = snapped

    out: Dict[str, str] = {}
    for m in cols_meta:
        lbl = m.get("label") or ""
        tag = (m.get("tag") or "")
        opts = m.get("dd_options") or []
        key = (m.get("field_key") or "")

        if tag.upper().startswith("#DD"):
            val_in = dd_fixed.get(lbl, lv_ai.get(lbl, ""))
        else:
            val_in = lv_ai.get(lbl, "")

        out[key] = _coerce_by_tag(tag, val_in, opts)

    return out


# =========================
# UTIL: A1, #S, label/DD, va boshqalar
# =========================
def _value_for_meta(meta: Dict, data: Dict[str, str]) -> str:
    key = (meta.get("field_key") or "")
    tag = (meta.get("tag") or "").upper()
    if tag == "#FF":
        return ""
    return data.get(key, "")


def _value_preview_for_meta(meta: Dict, data: Dict[str, str]) -> str:
    tag = (meta.get("tag") or "").upper()
    val_in = _value_for_meta(meta, data)
    v = _coerce_by_tag(tag, val_in, meta.get("dd_options", []))
    return "—" if (tag == "#FF" and v == "") else v


def _normalize_input_for_meta(meta: Dict, raw_val: str, dd_options: List[str]) -> str:
    tag = (meta.get("tag") or "").upper()
    raw_val = raw_val or ""
    if tag == "#DT":
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", raw_val.strip()):
            return raw_val.strip()
        iso = _ai_normalize_date(raw_val, tz="Asia/Tashkent", default_to_today=False)
        return _ai_iso_to_ddmmyyyy(iso) if iso else ""
    if tag.startswith("#DD"):
        return _smart_match_plus(raw_val, dd_options or [])
    if tag == "#NB":
        try:
            return str(int(float(_s(raw_val).replace(" ", "").replace("\u00A0", "").replace(",", "."))))
        except Exception:
            return ""
    if tag == "#FF":
        return ""
    return raw_val


def _looks_like_named_range(s: str) -> bool:
    if not s:
        return False
    s = _s(s).strip()
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s))


def _get_values_2d(svc, spreadsheet_id: str, rng: str) -> List[List[str]]:
    raw = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=rng)
        .execute()
        .get("values", [])
        or []
    )
    safe: List[List[str]] = []
    for row in raw:
        if not row:
            safe.append([])
            continue
        safe.append([_sstrip(c) for c in row])
    return safe


def _pick_label_cell_value(svc, spreadsheet_id: str, sheet_title: str, s_row_idx_0b: int, col_idx_0b: int) -> str:
    col_letter = _col_idx_to_letter(col_idx_0b)

    try:
        rng = f"{_a1_sheet(sheet_title)}!{col_letter}{s_row_idx_0b+2}"
        v2d = _get_values_2d(svc, spreadsheet_id, rng) or [[""]]
        lab = (v2d[0][0] if v2d and v2d[0] else "")
        if lab:
            return lab
    except Exception:
        pass

    if s_row_idx_0b >= 1:
        try:
            rng = f"{_a1_sheet(sheet_title)}!{col_letter}{s_row_idx_0b}"
            v2d = _get_values_2d(svc, spreadsheet_id, rng) or [[""]]
            lab = (v2d[0][0] if v2d and v2d[0] else "")
            if lab:
                return lab
        except Exception:
            pass

    for add in (3, 4, 5):
        try:
            rng = f"{_a1_sheet(sheet_title)}!{col_letter}{s_row_idx_0b+add}"
            v2d = _get_values_2d(svc, spreadsheet_id, rng) or [[""]]
            lab = (v2d[0][0] if v2d and v2d[0] else "")
            if lab:
                return lab
        except Exception:
            pass

    return ""


def _get_sheet_grid(svc, spreadsheet_id: str, sheet_title: str) -> dict:
    meta = (
        svc.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,gridProperties))",
        )
        .execute()
    )
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("title") == sheet_title:
            return props.get("gridProperties", {}) or {}
    return {}


def _col_idx_to_letter(idx: int) -> str:
    s, idx = "", idx + 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _a1_sheet(title: str) -> str:
    t = _s(title).replace("'", "''")
    return "'" + t + "'"


def _is_hash_s(val: str) -> bool:
    s = _s(val).replace(_ZW, "").strip()
    return s.upper() == "#S"


def _fix_a1_side_token(side: str) -> str:
    s = _s(side).strip().upper()
    if s == "0":
        return "O"
    m = re.fullmatch(r"0(\d+)", s)
    if m:
        return "O" + m.group(1)
    m = re.fullmatch(r"([A-Z]+)(\d+)?", s)
    if m:
        col, row = m.group(1), m.group(2) or ""
        return f"{col}{row}"
    return s

def _sanitize_a1_ref(a1: str, default_sheet: str) -> str:
    if not a1:
        return a1
    s = _s(a1).strip().strip('"').strip("'")
    s = s.replace("\\'", "'").replace('\\"', '"')

    if _looks_like_named_range(s) and "!" not in s and ":" not in s:
        return s

    if "!" not in s:
        s = f"{_a1_sheet(default_sheet)}!{s}"
        _, cols = default_sheet, s.split("!", 1)[1]
        sheet = default_sheet
    else:
        sheet, cols = s.split("!", 1)

    sheet = _sstrip(sheet).strip("'").strip('"')
    cols = _sstrip(cols).upper()

    if ":" in cols:
        left, right = [c.strip() for c in cols.split(":", 1)]
        left = _fix_a1_side_token(left)
        right = _fix_a1_side_token(right)
        cols = f"{left}:{right}"
    else:
        cols = _fix_a1_side_token(cols)

    return f"{_a1_sheet(sheet)}!{cols}"


def _get_sheet_values(svc, spreadsheet_id: str, a1: str) -> List[str]:
    vr = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=a1)
        .execute()
    )
    vals = vr.get("values") or []
    return [_sstrip(r[0]) if r else "" for r in vals]


def _get_sheet_id_and_grid(svc, spreadsheet_id: str, sheet_title: str) -> Tuple[int, Dict]:
    meta = (
        svc.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(sheetId,title,gridProperties))",
        )
        .execute()
    )
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("title") == sheet_title:
            return props.get("sheetId"), props.get("gridProperties", {})
    raise RuntimeError(f"Sheet topilmadi: {sheet_title}")


def _get_column_values(svc, spreadsheet_id: str, a1: str) -> List[str]:
    vr = (
        svc.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=a1,
            majorDimension="COLUMNS",
        )
        .execute()
    )
    cols = vr.get("values") or []
    return [_sstrip(v) for v in (cols[0] if cols else [])]


def _get_sheet_titles(svc, spreadsheet_id: str) -> List[str]:
    meta = (
        svc.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(title))")
        .execute()
    )
    return [sh["properties"]["title"] for sh in (meta.get("sheets") or [])]


def _get_sheet_props(svc, spreadsheet_id: str, sheet_title: str) -> dict:
    meta = (
        svc.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,gridProperties))",
        )
        .execute()
    )
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("title") == sheet_title:
            return props.get("gridProperties", {}) or {}
    return {}


def _right_edge_letter(svc, spreadsheet_id: str, sheet_title: str) -> str:
    grid = _get_sheet_props(svc, spreadsheet_id, sheet_title)
    col_count = int(grid.get("columnCount") or 26)
    col_count = max(26, min(col_count, 5000))
    return _col_idx_to_letter(col_count - 1)


def _try_guess_dd_source_on_sheet(svc, spreadsheet_id: str, sheet_title: str, label: str) -> Optional[str]:
    if not label:
        return None

    label_norm = (label or "").strip().casefold()

    # ❗ Juda katta range olishning hojati yo‘q
    MAX_SCAN_ROWS = 60      # 500 emas, faqat 60 qator skaner qilamiz (10x tez)
    MAX_SCAN_COLS = 60      # 5000 emas, 40 ta ustun (odatda shablonlar kichik bo‘ladi)

    grid = (
        svc.spreadsheets()
        .get(
            spreadsheetId=spreadsheet_id,
            fields="sheets(properties(title,gridProperties))",
        )
        .execute()
    )

    # Real colCount ni olamiz, lekin limit qo‘yamiz
    col_count = 0
    for sh in grid.get("sheets", []):
        if sh["properties"]["title"] == sheet_title:
            g = sh.get("gridProperties", {}) or {}
            col_count = int(g.get("columnCount", 26))
            break

    # ustunni ham limitlaymiz
    col_count = max(5, min(col_count, MAX_SCAN_COLS))

    right = _col_idx_to_letter(col_count - 1)

    # ❗ Endi faqat A1:AO60 (yoki shunga yaqin) skaner
    rng = f"{_a1_sheet(sheet_title)}!A1:{right}{MAX_SCAN_ROWS}"

    vals = _get_values_2d(svc, spreadsheet_id, rng)

    for r, row in enumerate(vals):
        for c, cell in enumerate(row):
            if (cell or "").strip().casefold() == label_norm:
                col_letter = _col_idx_to_letter(c)
                start_1b = r + 2
                return f"{_a1_sheet(sheet_title)}!{col_letter}{start_1b}:{col_letter}"

    return None



# ====== Config: kesh ======
_TEMPLATE_TTL_SEC = 36000
_tpl_cache = {}  # {(file_id, sheet_title): {"ts": time.time(), "data": (markers, dd_opts, s_row_idx, s_col_idx, cols_meta)}}


def _cache_get_tpl(file_id: str, sheet_title: str):
    key = (file_id, sheet_title)
    item = _tpl_cache.get(key)
    if not item:
        return None
    if time.time() - item["ts"] > _TEMPLATE_TTL_SEC:
        _tpl_cache.pop(key, None)
        return None
    return item["data"]


def _cache_set_tpl(file_id: str, sheet_title: str, data_tuple):
    _tpl_cache[(file_id, sheet_title)] = {"ts": time.time(), "data": data_tuple}


def _find_anchor_S(svc, spreadsheet_id: str, sheet_title: str) -> Tuple[int, int]:
    right = _right_edge_letter(svc, spreadsheet_id, sheet_title)
    rng = f"{_a1_sheet(sheet_title)}!A1:{right}50"
    try:
        values = _get_values_2d(svc, spreadsheet_id, rng)
        for r, row in enumerate(values):
            for c, val in enumerate(row):
                if _is_hash_s(val):
                    return r, c
        raise RuntimeError("#S topilmadi. Marker birinchi 50 qatorda bo'lishi kerak.")
    except Exception as e:
        raise RuntimeError(f"#S markerini qidirishda xatolik: {e}")


def _list_sheet_titles(svc, spreadsheet_id: str) -> List[str]:
    meta = (
        svc.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(title))")
        .execute()
    )
    return [sh["properties"]["title"] for sh in (meta.get("sheets") or [])]


def _has_hash_s(svc, spreadsheet_id: str, sheet_title: str) -> bool:
    try:
        _find_anchor_S(svc, spreadsheet_id, sheet_title)
        return True
    except Exception:
        return False


async def _resolve_sheet_title_or_autodetect(tg_id: int, file_id: str):
    creds = await get_credentials_for_tg(tg_id)
    if not creds:
        raise RuntimeError("Google bilan /connect qiling")
    svc = build("sheets", "v4", credentials=creds)

    title = await get_last_sheet_title_for_file_by_tg(tg_id, file_id)
    if title:
        if _has_hash_s(svc, file_id, title):
            return title, svc
        raise RuntimeError(f"Tanlangan varaqda #S yo'q: {title}. /start orqali 'Apply template' bosing.")

    for t in _list_sheet_titles(svc, file_id):
        if _has_hash_s(svc, file_id, t):
            return t, svc

    raise RuntimeError("Fayldagi hech bir varoqda #S topilmadi. /start orqali shablonni qo‘llang.")


def _detect_template_and_sources(svc, spreadsheet_id: str, sheet_title: str):
    cached = _cache_get_tpl(spreadsheet_id, sheet_title)
    if cached:
        return cached

    s_row_idx, s_col_idx = _find_anchor_S(svc, spreadsheet_id, sheet_title)

    right = _right_edge_letter(svc, spreadsheet_id, sheet_title)
    left_letter = _col_idx_to_letter(s_col_idx)
    row_rng = f"{_a1_sheet(sheet_title)}!{left_letter}{s_row_idx+1}:{right}{s_row_idx+1}"

    row_vals_2d = _get_values_2d(svc, spreadsheet_id, row_rng)
    row_vals = row_vals_2d[0] if row_vals_2d else []

    cols_meta: List[Dict] = []
    for off, cell in enumerate(row_vals):
        raw = _sstrip(cell)
        if not raw or raw.upper() == "#S":
            continue
        col_idx = s_col_idx + off

        label = _pick_label_cell_value(svc, spreadsheet_id, sheet_title, s_row_idx, col_idx)
        if not label:
            label = raw.lstrip("#")

        tag = raw.upper()
        meta = {
            "col_idx": col_idx,
            "tag": tag,
            "label": label,
            "field_key": (label or "").upper(),
        }

        if tag.startswith("#DD"):
            dd_src = ""
            if "-" in raw:
                dd_src = raw.split("-", 1)[1].strip()

            if not dd_src:
                cand = _sstrip(label)
                if cand and (_looks_like_named_range(cand) or "!" in cand or re.search(r"\d", cand)):
                    dd_src = cand

            if dd_src:
                dd_src = _sanitize_a1_ref(dd_src, sheet_title)
                try:
                    opts = _get_sheet_values(svc, spreadsheet_id, dd_src)
                except Exception as e:
                    raise RuntimeError(f"#DD diapazoni noto‘g‘ri: {dd_src} ({e})")
                meta["dd_options"] = [o for o in opts if o][:50]
            else:
                guessed = _try_guess_dd_source_on_sheet(svc, spreadsheet_id, sheet_title, label)
                if guessed:
                    try:
                        opts = _get_sheet_values(svc, spreadsheet_id, guessed)
                        meta["dd_options"] = [o for o in opts if o][:50]
                    except Exception:
                        meta["dd_options"] = []

        print(
            "[TPL]",
            sheet_title,
            "col",
            col_idx,
            "tag",
            tag,
            "label",
            label,
            "dd_opts",
            len(meta.get("dd_options", [])) if tag.startswith("#DD") else "-",
        )

        cols_meta.append(meta)

    if not cols_meta:
        raise RuntimeError("Kerakli teglar yo‘q: #S topilmadi yoki markerlar aniqlanmadi.")

    markers = {m["field_key"]: m["col_idx"] for m in cols_meta}
    dd_opts = {m["field_key"]: m.get("dd_options", []) for m in cols_meta if m["tag"].startswith("#DD")}

    data_tuple = (markers, dd_opts, s_row_idx, s_col_idx, cols_meta)
    _cache_set_tpl(spreadsheet_id, sheet_title, data_tuple)
    return data_tuple


def _build_preview(cols_meta: List[Dict], data: Dict[str, str]) -> str:
    lines = []
    for i, m in enumerate(cols_meta, start=1):
        val_in = _value_for_meta(m, data)
        v = _coerce_by_tag(m["tag"], val_in, m.get("dd_options", []))
        v_show = "—" if (m["tag"] == "#FF" and v == "") else v
        lines.append(f"{str(i).zfill(2)}. {m['label']}: {v_show}")
    return "\n".join(lines)


def _prepare_row_values(cols_meta: List[Dict], data: Dict[str, str]) -> List[str]:
    max_col = max(m["col_idx"] for m in cols_meta)
    row = [""] * (max_col + 1)
    for m in cols_meta:
        val_in = _value_for_meta(m, data)
        v = _coerce_by_tag(m["tag"], val_in, m.get("dd_options", []))

        tag = (m.get("tag") or "").upper()
        label_up = (m.get("label") or "").strip().upper()

        if tag == "#FF" and label_up in {"SUMMA UZS", "SUM UZS", "SUMMA SO'M", "SUMMA SOM"}:
            row[m["col_idx"]] = v
            continue

        row[m["col_idx"]] = "" if tag == "#FF" else v
    return row


def _build_dd_options_from_cols(cols_meta: List[Dict]) -> Dict[str, List[str]]:
    dd_options: Dict[str, List[str]] = {}
    for m in cols_meta:
        tag = (m.get("tag") or "").upper()
        if not tag.startswith("#DD"):
            continue
        key = (m.get("field_key") or "").upper()
        opts = m.get("dd_options") or []
        dd_options.setdefault(key, [])
        for o in opts:
            if o and o not in dd_options[key]:
                dd_options[key].append(o)
    return dd_options


def _find_target_row_by_dt_col(svc, spreadsheet_id: str, sheet_title: str, s_row_idx_0b: int, cols_meta: List[Dict]) -> int:
    dt_meta = next((m for m in cols_meta if (m.get("tag") or "").upper() == "#DT"), None)
    if not dt_meta:
        raise RuntimeError("#DT ustuni topilmadi (SANA). Shablonda #DT bo‘lishi shart.")

    dt_col_idx_0b = int(dt_meta["col_idx"])
    dt_col_letter = _col_idx_to_letter(dt_col_idx_0b)

    start_1b = s_row_idx_0b + 2
    col_rng = f"{_a1_sheet(sheet_title)}!{dt_col_letter}{start_1b}:{dt_col_letter}"
    col_vals = _get_column_values(svc, spreadsheet_id, col_rng)

    for i, v in enumerate(col_vals):
        if _sstrip(v) == "":
            return start_1b + i

    return start_1b + len(col_vals)


def _find_target_row_by_sequence(
    svc,
    spreadsheet_id: str,
    sheet_title: str,
    s_row_idx_0b: int,
    s_col_idx_0b: int,
    cols_meta: List[Dict],
    max_n: int = 100_000,
) -> int:
    seq_col_letter = _col_idx_to_letter(s_col_idx_0b)
    start_1b = s_row_idx_0b + 2

    seq_rng = f"{_a1_sheet(sheet_title)}!{seq_col_letter}{start_1b}:{seq_col_letter}"
    col_vals = _get_column_values(svc, spreadsheet_id, seq_rng)

    try:
        base_idx = col_vals.index("1")
    except ValueError:
        raise RuntimeError("'1' raqami topilmadi. #S ostida 1,2,3... ketma-ketlik bo'lishi shart.")

    next_row_1b = start_1b + len(col_vals)

    for i in range(base_idx, len(col_vals)):
        if _sstrip(col_vals[i]) == "":
            next_row_1b = start_1b + i
            break

    return next_row_1b


def _ensure_capacity(
    svc,
    spreadsheet_id: str,
    sheet_title: str,
    *,
    target_row_1b: int,
    target_col_index_0b: int,
    row_buffer: int = 100,
    col_buffer: int = 10,
) -> None:
    sheet_id, grid = _get_sheet_id_and_grid(svc, spreadsheet_id, sheet_title)
    cur_rows = int(grid.get("rowCount", 1000) or 1000)
    cur_cols = int(grid.get("columnCount", 26) or 26)

    need_rows = max(cur_rows, target_row_1b + row_buffer)
    need_cols = max(cur_cols, (target_col_index_0b + 1) + col_buffer)

    reqs = []
    if need_rows > cur_rows or need_cols > cur_cols:
        new_props = {
            "sheetId": sheet_id,
            "gridProperties": {"rowCount": need_rows, "columnCount": need_cols},
        }
        reqs.append(
            {
                "updateSheetProperties": {
                    "properties": new_props,
                    "fields": "gridProperties(rowCount,columnCount)",
                }
            }
        )

    if not reqs:
        return

    svc.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": reqs}).execute()


async def _append_structured_row(
    tg_id: int,
    file_id: str,
    sheet_title: str,
    text_or_data,
    use_ai: bool = True,
    tpl=None,  # (markers, dd_opts, s_row_idx, s_col_idx, cols_meta) — agar bor bo‘lsa qayta detect qilmaymiz
):
    creds = await get_credentials_for_tg(tg_id)
    if not creds:
        raise RuntimeError("Google bilan /connect qiling")
    svc = build("sheets", "v4", credentials=creds)

    if tpl is not None:
        _, _, s_row_idx, s_col_idx, cols_meta = tpl
    else:
        try:
            _, _, s_row_idx, s_col_idx, cols_meta = _detect_template_and_sources(svc, file_id, sheet_title)
        except Exception as e:
            raise RuntimeError(f"Kerakli teglar yoki shablon muammosi: {e}")

    if use_ai:
        dyn_dd = _build_dd_options_from_cols(cols_meta)
        kwargs = dict(text=_s(text_or_data or ""), dd_options=dyn_dd, tz="Asia/Tashkent", fields_meta=cols_meta)
        try:
            ai_out = await extract_with_ai(**kwargs)
        except TypeError:
            kwargs.pop("fields_meta", None)
            ai_out = await extract_with_ai(**kwargs)
        row_data = _labels_from_ai(ai_out, cols_meta)
    else:
        row_data = dict(text_or_data or {})

    full_row_vals = _prepare_row_values(cols_meta, row_data)

    min_c = min(m["col_idx"] for m in cols_meta)
    max_c = max(m["col_idx"] for m in cols_meta)
    left_letter = _col_idx_to_letter(min_c)
    right_letter = _col_idx_to_letter(max_c)
    slice_vals = full_row_vals[min_c : max_c + 1]

    try:
        target_row = _find_target_row_by_dt_col(svc, file_id, sheet_title, s_row_idx, cols_meta)
    except Exception:
        target_row = _find_target_row_by_sequence(
            svc,
            file_id,
            sheet_title,
            s_row_idx,
            s_col_idx,
            cols_meta,
            max_n=100_000,
        )

    _ensure_capacity(
        svc,
        file_id,
        sheet_title,
        target_row_1b=target_row,
        target_col_index_0b=max_c,
    )

    write_rng = f"{_a1_sheet(sheet_title)}!{left_letter}{target_row}:{right_letter}{target_row}"

    def _do_write():
        body = {"values": [slice_vals]}
        return (
            svc.spreadsheets()
            .values()
            .update(
                spreadsheetId=file_id,
                range=write_rng,
                valueInputOption="USER_ENTERED",
                body=body,
            )
            .execute()
        )

    try:
        try:
            res = await asyncio.to_thread(_do_write)
        except HttpError:
            await asyncio.sleep(0.5)
            res = await asyncio.to_thread(_do_write)
    except Exception as e:
        raise RuntimeError(f"Yozishda xatolik: {e}")

    try:
        await set_last_sheet_title_for_file_by_tg(tg_id, file_id, sheet_title)
    except Exception:
        pass

    preview = _build_preview(cols_meta, row_data)
    return res.get("updatedRange", write_rng), row_data, preview


# =========================
# Handler: voice/audio
# =========================
@router.message(F.voice | F.audio)
async def handle_voice_or_audio(msg: Message, state: FSMContext):
    t_all_start = time.perf_counter()

    tg_id = msg.from_user.id

    # 🟩 BU YERDA EMAIL OLINADI
    google_email = await get_google_email_by_tg(tg_id)

    file_id = await get_last_file_id_by_tg(tg_id)
    if not file_id:
        await msg.answer("Avval /mysheets orqali fayl tanlang (inline tugmadan bitta faylni bosing).")
        return

    try:
        sheet_title, svc = await _resolve_sheet_title_or_autodetect(tg_id, file_id)
    except Exception as e:
        await msg.answer(f"❌ Varaq aniqlanmadi: {e}")
        return

    user = await get_user_by_tg(tg_id)
    language = (user or {}).get("language", "uz")[:2]

    bot = msg.bot
    file_id_tg = msg.voice.file_id if msg.voice else msg.audio.file_id
    duration = (msg.voice.duration if msg.voice else (msg.audio.duration or 0)) or 0

    t_dl_start = time.perf_counter()
    file_obj = await bot.get_file(file_id_tg)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as f:
        await bot.download(file_obj, destination=f.name)
        audio_path = f.name
    _ = time.perf_counter() - t_dl_start

    try:
        if duration <= 60:
            t_stt_start = time.perf_counter()
            resp = await stt_short(
                audio_path,
                title=f"tg_{tg_id}_{msg.message_id}",
                language=language,
                has_diarization=False,
            )
            stt_sec = time.perf_counter() - t_stt_start

            text = resp.get("text") or resp.get("result") or resp.get("transcript") or ""
            if not text:
                await msg.answer("❌ STT natijasi bo‘sh chiqdi.")
                return

            # TEMPLATE DETECT
            try:
                tpl = _detect_template_and_sources(svc, file_id, sheet_title)
                _, _, s_row_idx, s_col_idx, cols_meta = tpl
            except Exception as e:
                await msg.answer(
                    "❌ Kerakli teglar yoki shablon muammosi.\n"
                    f"Fayl: `{file_id}`\nVaraq: `{sheet_title}`\nXato: `{e}`",
                    parse_mode="Markdown",
                )
                return

            dyn_dd = _build_dd_options_from_cols(cols_meta)

            t_ai_start = time.perf_counter()
            try:
                ai_out = await extract_with_ai(
                    text=text,
                    dd_options=dyn_dd,
                    tz="Asia/Tashkent",
                    fields_meta=cols_meta,
                    google_email=google_email   # 🟩 ENDILIKDA ISHLAYDI!
                )
            except TypeError:
                ai_out = await extract_with_ai(
                    text,
                    dd_options=dyn_dd,
                    tz="Asia/Tashkent",
                )
            ai_sec = time.perf_counter() - t_ai_start

            row_data = _labels_from_ai(ai_out, cols_meta)
            preview = _build_preview(cols_meta, row_data)

            all_sec = time.perf_counter() - t_all_start
            timing_line = f"⏱ STT: {stt_sec:.2f}s | AI: {ai_sec:.2f}s | Umumiy: {all_sec:.2f}s"

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✏️ Tahrirlash", callback_data="vadd:edit"),
                        InlineKeyboardButton(text="✅ Yuborish", callback_data="vadd:send"),
                    ]
                ]
            )

            await state.set_state(VAdd.awaiting_decision)
            await state.update_data(
                pending={"file_id": file_id, "sheet_title": sheet_title, "data": row_data},
                cols_meta=cols_meta,
                dd_options=dyn_dd,
                tpl=tpl,
            )

            await msg.answer(
                timing_line
                + "\n\n"
                + "Tekshirib ko‘ring. Tahrirlasangiz ham bo‘ladi.\n\n"
                + preview,
                reply_markup=kb,
            )

        else:
            webhook = f"{BASE_URL}/webhooks/aisha-stt?tg_id={tg_id}"
            await stt_long(
                audio_path,
                title=f"tg_{tg_id}_{msg.message_id}",
                has_diarization=False,
                webhook_url=webhook,
            )
            all_sec = time.perf_counter() - t_all_start
            await msg.answer(
                f"✅ Audio qabul qilindi ({all_sec:.2f}s). "
                "Uzoq STT ishga tushdi; tayyor bo‘lsa yuboraman."
            )
    except Exception as e:
        await msg.answer(f"❌ Xatolik 33: {e}")


# =========================
# Tahrirlash/Yuborish oqimi
# =========================
@router.callback_query(VAdd.awaiting_decision, F.data == "vadd:edit")
async def vadd_edit(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    p = d.get("pending", {}) or {}
    data = p.get("data", {}) or {}
    cols_meta = d.get("cols_meta", []) or []

    EXCLUDE_TAGS = {"#DT-A", "#TX-A", "#GA-A"}  # ❌ tahrirlashdan chiqariladigan ustunlar

    lines = []
    for i, m in enumerate(cols_meta, start=1):
        tag = (m.get("tag") or "").upper()

        # ❌ Bu TAGlar tahrirlash oynasida chiqmaydi
        if tag in EXCLUDE_TAGS:
            continue

        show_val = _value_preview_for_meta(m, data)
        lines.append(f"{str(i).zfill(2)}. {m['label']}: {show_val}")


    template_plain = "\n".join(lines)
    guide = (
        "Matnni shu formatda tahrirlab, shu xabarga javob qilib yuboring.\n"
        "Qoidalar:\n"
        "• #DT = sana (dd.mm.yyyy), #DD = ro‘yxatdan eng yaqin mos, #NB = faqat raqam, #FF = bo‘sh qoladi, #TX = matn.\n"
        "• CHIQARILGAN LABEL nomlarini o‘zgartirmang — faqat ikki nuqtadan keyingi qiymatni tahrirlang.\n\n"
    )
    await cb.message.answer(guide + "```\n" + template_plain + "\n```", parse_mode="Markdown")
    await state.set_state(VAdd.editing_text)
    await cb.answer()

@router.message(VAdd.editing_text, F.text)
async def vadd_receive_edited(msg: Message, state: FSMContext):
    text = _sstrip(msg.text)
    d = await state.get_data()
    cols_meta = d.get("cols_meta", []) or []

    # label → meta map
    label_map = {(_sstrip(m["label"]).lower()): m for m in cols_meta}
    new_data: Dict[str, str] = {}

    for line in text.splitlines():
        if ":" not in line:
            continue

        try:
            left, raw_val = line.split(":", 1)
        except ValueError:
            continue

        left = _sstrip(left)
        raw_val = _sstrip(raw_val)

        # “01. Sana:” kabi indeksni olib tashlaymiz
        left = re.sub(r"^\d{1,3}\.\s*", "", left)
        label_key = left.lower()

        # label → meta
        meta = label_map.get(label_key)
        if not meta:
            alt = (
                label_key.replace("’", "'")
                .replace("`", "'")
                .replace("  ", " ")
                .strip()
            )
            meta = label_map.get(alt)
        if not meta:
            continue

        tag = (meta["tag"] or "").upper()
        key = meta["field_key"]

        # ❌ Auto-fields — tahrirlash YOPIQ
        if tag in {"#DT-A", "#TX-A", "#GA-A"}:
            continue

        # Normalizatsiya
        norm_val = _normalize_input_for_meta(
            meta, raw_val, meta.get("dd_options", [])
        )

        # Qoida bo‘yicha yozamiz
        if tag == "#FF":
            continue
        elif tag == "#NB":
            new_data[key] = "" if norm_val in ("—", "") else norm_val
        else:
            new_data[key] = norm_val

    # Eski pending ma'lumotlar (ai_out dan kelgan to‘liq data)
    old_pending = d.get("pending") or {}
    old_data = dict(old_pending.get("data") or {})

    # 🔗 Yangi tahrirlarni eski data ustiga MERGE qilamiz
    merged_data = dict(old_data)
    merged_data.update(new_data)   # faqat tahrirlangan maydonlar yangilanadi,
                                   # auto-fields (#DT-A, #TX-A, #GA-A) o‘zgarmaydi

    # =========================
    # PREVIEW YARATAMIZ
    # =========================
    preview_lines = []
    for i, m in enumerate(cols_meta, start=1):
        show_val = _value_preview_for_meta(m, merged_data)
        preview_lines.append(f"{str(i).zfill(2)}. {m['label']}: {show_val}")

    preview = "\n".join(preview_lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yuborish", callback_data="vadd:send")]
        ]
    )

    await msg.answer("Yangilangan ko‘rinish:\n\n" + preview, reply_markup=kb)

    # pending → endi to‘liq MERGED data bilan saqlaymiz
    d_pending = old_pending.copy()
    d_pending["data"] = merged_data
    await state.update_data(pending=d_pending)

    # qayta Yuborish/Tahrirlash holatiga o‘tamiz
    await state.set_state(VAdd.awaiting_decision)




@router.callback_query(VAdd.awaiting_decision, F.data.startswith("vadd:sheet:"))
async def vadd_sheet_switch(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    pending = d.get("pending", {}) or {}
    file_id = pending.get("file_id")
    if not file_id:
        await cb.answer("Fayl aniqlanmadi. Iltimos, /start orqali fayl tanlang.", show_alert=True)
        return

    if cb.data.startswith("vadd:sheet:set:"):
        titles = d.get("all_titles", [])
        try:
            idx = int(cb.data.split(":", 2)[2])
        except Exception:
            await cb.answer("Noto‘g‘ri tanlov.", show_alert=True)
            return

        if not titles or idx < 0 or idx >= len(titles):
            await cb.answer("Bunday varaq yo‘q.", show_alert=True)
            return

        new_title = titles[idx]
        pending["sheet_title"] = new_title

        try:
            await set_last_sheet_title_for_file_by_tg(cb.from_user.id, file_id, new_title)
        except Exception:
            pass

        try:
            creds = await get_credentials_for_tg(cb.from_user.id)
            svc = build("sheets", "v4", credentials=creds)
            tpl_new = _detect_template_and_sources(svc, pending["file_id"], new_title)
            _, dd_opts_new, _, _, cols_meta = tpl_new
            await state.update_data(dd_options=dd_opts_new, cols_meta=cols_meta, tpl=tpl_new)

            data = pending.get("data", {}) or {}
            dd_keys = {
                m["field_key"]
                for m in cols_meta
                if (m.get("tag") or "").upper().startswith("#DD")
            }
            for key in list(data.keys()):
                up_key = (key or "").upper()
                if up_key in dd_keys and dd_opts_new.get(up_key):
                    data[key] = _smart_match_plus(str(data[key]), dd_opts_new[up_key])
            pending["data"] = data
        except Exception:
            pass

        await state.update_data(pending=pending)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✏️ Tahrirlash", callback_data="vadd:edit"),
                    InlineKeyboardButton(text="✅ Yuborish", callback_data="vadd:send"),
                ]
            ]
        )
        await cb.message.answer(
            f"🗂 Yozish joyi: *{new_title}* tanlandi.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        await cb.answer()
        return

    await cb.answer("Noto‘g‘ri buyruq.", show_alert=True)


@router.callback_query(VAdd.awaiting_decision, F.data == "vadd:send")
async def vadd_send(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    p = d.get("pending", {})
    file_id = p.get("file_id")
    sheet_title = p.get("sheet_title")
    data = p.get("data")
    tpl = d.get("tpl")  # 🔥 shu yerda shablon keshdan olinadi

    if not file_id or not sheet_title or not isinstance(data, dict):
        await cb.answer("Ichki xatolik: ma'lumot yetarli emas.", show_alert=True)
        return

    try:
        updated_range, _, preview = await _append_structured_row(
            tg_id=cb.from_user.id,
            file_id=file_id,
            sheet_title=sheet_title,
            text_or_data=data,
            use_ai=False,
            tpl=tpl,  # 🔥 qayta detect qilmaydi
        )
        await cb.message.answer(
            "✅ Qo‘shildi!\n"
            f"🗂 Varaq: *{sheet_title}*\n"
            f"📌 Joylashuvi: `{updated_range}`\n\n{preview}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await cb.message.answer(f"❌ Xatolik 4: {e}")
    finally:
        await state.clear()
        await cb.answer()

# === LOCAL DATA CACHE ===
_last10_cache = {}   # { (tg_id, file_id, sheet_title): [ {...}, {...}, ... ] }


async def refresh_local_cache(tg_id: int):
    file_id = await get_last_file_id_by_tg(tg_id)
    if not file_id:
        return None, "❌ Fayl tanlanmagan!"

    creds = await get_credentials_for_tg(tg_id)
    if not creds:
        return None, "❌ Google bilan bog‘laning: /connect"

    svc = build("sheets", "v4", credentials=creds)

    # tanlangan varaq
    try:
        sheet_title, _ = await _resolve_sheet_title_or_autodetect(tg_id, file_id)
    except Exception as e:
        return None, f"❌ {e}"

    # === SHABLON KESHINI TOZALAYMIZ ===
    key = (file_id, sheet_title)
    if key in _tpl_cache:
        _tpl_cache.pop(key, None)

    # === Shablonni qayta yaratamiz ===
    try:
        tpl = _detect_template_and_sources(svc, file_id, sheet_title)
        _, _, s_row_idx, s_col_idx, cols_meta = tpl
    except Exception as e:
        return None, f"❌ Shablonni aniqlab bo‘lmadi: {e}"

    # === Oxirgi 10 ta satrni yuklaymiz ===
    dt_meta = next((m for m in cols_meta if (m["tag"] or "").upper() == "#DT"), None)
    if not dt_meta:
        return None, "❌ #DT ustuni topilmadi!"

    dt_col = _col_idx_to_letter(dt_meta["col_idx"])
    start_1b = s_row_idx + 2

    rng = f"{_a1_sheet(sheet_title)}!{dt_col}{start_1b}:{dt_col}"
    values = _get_column_values(svc, file_id, rng)

    # bo‘sh bo‘lmagan joylarni topamiz
    non_empty = [(i, v) for i, v in enumerate(values) if v.strip()]
    if not non_empty:
        rows = []
    else:
        last_rows = non_empty[-10:]   # oxirgi 10 ta
        rows = [start_1b + idx for idx, _ in last_rows]

    # endi shu qatorlar bo‘yicha to‘liq ma’lumotlarni olamiz
    row_dicts = []
    for r in rows:
        left = _col_idx_to_letter(min(m["col_idx"] for m in cols_meta))
        right = _col_idx_to_letter(max(m["col_idx"] for m in cols_meta))
        rng = f"{_a1_sheet(sheet_title)}!{left}{r}:{right}{r}"
        row_vals = _get_values_2d(svc, file_id, rng)[0]

        data = {}
        for m in cols_meta:
            idx = m["col_idx"] - min(c["col_idx"] for c in cols_meta)
            data[m["field_key"]] = row_vals[idx] if idx < len(row_vals) else ""

        row_dicts.append(data)

    # Saqlaymiz
    _last10_cache[(tg_id, file_id, sheet_title)] = row_dicts

    return sheet_title, f"✅ Ma’lumotlar yangilandi ({len(row_dicts)} ta qator)."
