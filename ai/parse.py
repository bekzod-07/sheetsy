import os
import re
import json
import datetime as dt
from typing import Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo
# ============== OpenAI klient ==============
try:
    from openai import AsyncOpenAI
    _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
except Exception:
    _client = None

OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-5-mini")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gpt-5-mini")

_CHAT_COMPAT_PREFIXES = ("gpt-5-mini")

def _require_openai():
    if _client is None:
        raise RuntimeError("OPENAI_API_KEY topilmadi yoki OpenAI klienti ishga tushmadi.")
    
def _supports_temperature(model_name: str) -> bool:
    model_name = (model_name or "").lower()
    return model_name.startswith(("gpt-5", "o5", "gpt-4o", "o4"))

def _is_chat_compatible(model: str) -> bool:
    return (model or "").lower().startswith(_CHAT_COMPAT_PREFIXES)

def _extract_text_from_response(resp) -> str:
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text
    try:
        chunks = []
        for item in getattr(resp, "output", []) or []:
            for piece in getattr(item, "content", []) or []:
                if getattr(piece, "type", "") == "output_text":
                    t = getattr(piece, "text", "")
                    if t:
                        chunks.append(t)
        if chunks:
            return "\n".join(chunks)
    except Exception:
        pass
    try:
        return resp.choices[0].message.content
    except Exception:
        return ""

async def _create_json_completion_safe(model: str, messages: List[Dict]) -> Dict:
    _require_openai()

    def _join_messages(msgs: List[Dict]) -> str:
        return "\n\n".join(f"{(m.get('role') or '').upper()}:\n{m.get('content') or ''}" for m in msgs)

    prompt_text = _join_messages(messages)

    # ---- Responses API kwargs: temperature YO'Q! ----
    def _mk_responses_kwargs(mdl: str) -> Dict:
        kwargs = {"model": mdl, "input": prompt_text}
        # Agar xohlasangiz, chiqishni cheklash:
        # kwargs["max_output_tokens"] = int(os.getenv("OPENAI_MAX_TOKENS", "800"))
        return kwargs

    # ---- Chat API kwargs: response_format bor, temperature ehtiyotkorlik bilan ----
    def _mk_chat_kwargs(mdl: str, *, with_temperature: bool = True) -> Dict:
        kwargs = {"model": mdl, "messages": messages, "response_format": {"type": "json_object"}}
        if with_temperature and _supports_temperature(mdl):
            try:
                kwargs["temperature"] = float(os.getenv("OPENAI_TEMPERATURE", "0.1"))
            except Exception:
                kwargs["temperature"] = 0.1
        return kwargs

    async def _call_responses_api(mdl: str) -> Dict:
        resp = await _client.responses.create(**_mk_responses_kwargs(mdl))
        text = _extract_text_from_response(resp)
        try:
            return json.loads(text or "{}")
        except Exception:
            return {}

    async def _call_chat_api(mdl: str) -> Dict:
        # 1-urinish: temperature bilan (agar qo‘llasa)
        try:
            resp = await _client.chat.completions.create(**_mk_chat_kwargs(mdl, with_temperature=True))
            text = (resp.choices[0].message.content or "")
            return json.loads(text or "{}")
        except Exception as e:
            s = str(e).lower()
            # Agar model temperature’ni qabul qilmasa, temperature-siz qayta urinib ko‘ramiz
            if "unsupported parameter" in s and "temperature" in s:
                resp = await _client.chat.completions.create(**_mk_chat_kwargs(mdl, with_temperature=False))
                text = (resp.choices[0].message.content or "")
                try:
                    return json.loads(text or "{}")
                except Exception:
                    return {}
            raise

    primary = (model or "").strip() or OPENAI_MODEL
    fallback = (FALLBACK_MODEL or "").strip()

    # 1) Avval har doim Responses API (GPT-5 / o5 / 4.1 / 4o uchun to‘g‘ri yo‘l)
    try:
        return await _call_responses_api(primary)
    except Exception as e_primary:
        # 2) Fallback ham Responses’da
        if fallback and fallback != primary:
            try:
                return await _call_responses_api(fallback)
            except Exception as e_fb:
                # 3) Fallback chat-compat bo‘lsa, chat/completions’ga o‘tamiz
                if _is_chat_compatible(fallback):
                    try:
                        return await _call_chat_api(fallback)
                    except Exception as e_chat:
                        raise RuntimeError(f"OpenAI error (chat fallback failed): {e_chat}") from e_fb
                raise RuntimeError(f"OpenAI error (fallback failed): {e_fb}") from e_fb

        # 4) Primary chat-compat bo‘lsa, oxirgi urinish sifatida chat/completions
        if _is_chat_compatible(primary):
            try:
                return await _call_chat_api(primary)
            except Exception as e_chat:
                raise RuntimeError(f"OpenAI error (chat fallback failed): {e_chat}") from e_primary

        # 5) Aks holda — aniq xato bilan chiqamiz
        raise RuntimeError(f"OpenAI error: {e_primary}")


# ====================== LEXIKON / UTIL ======================

UZ_WORDS = {
    "today": ["bugun", "today", "bugungi"],
    "yesterday": ["kecha", "yesterday", "kechagi"],
    "tomorrow": ["ertaga", "tomorrow", "ertangi"],
    "som": ["so'm", "som", "uzs", "so’m", "s'om", "uzbek so'mi", "so'mi"],
    "usd": ["dollar", "usd", "$", "aqsh dollari", "amerika dollari", "dollor"],
    "eur": ["evro", "euro", "eur", "€"],
    "rub": ["rubl", "rub", "₽"],
}

UZ_NUM = {
    "bir":1, "ikki":2, "uch":3, "tort":4, "to'rt":4, "besh":5,
    "olti":6, "yetti":7, "sakkiz":8, "toqqiz":9, "to'qqiz":9,
    "on":10, "o'n":10, "yigirma":20, "ottiz":30, "o'ttiz":30,
    "qirq":40, "ellik":50, "oltmish":60, "yetmish":70, "sakson":80,
    "toqson":90, "to'qson":90,
    "yuz":100, "ming":1000, "million":1_000_000,
}

_CYR2LAT = {
    "ў":"o'", "қ":"q", "ғ":"g'", "ҳ":"h", "ё":"yo", "ю":"yu", "я":"ya", "ш":"sh", "ч":"ch",
    "Ў":"O'", "Қ":"Q", "Ғ":"G'", "Ҳ":"H", "Ё":"Yo", "Ю":"Yu", "Я":"Ya", "Ш":"Sh", "Ч":"Ch",
    "й":"y", "Й":"Y", "ы":"i", "Ы":"I", "э":"e", "Э":"E", "ц":"ts", "Ц":"Ts", "щ":"sh", "Щ":"Sh",
    "ъ":"", "Ъ":"", "ь":"", "Ь":""
}

_CURRENCY_ALIASES = {
    "USD": {"usd", "$", "dollar", "aqsh dollari", "amerika dollari", "amerika$","dollor"},
    "EUR": {"eur", "€", "euro", "evro"},
    "RUB": {"rub", "₽", "rubl"},
    "UZS": {"uzs", "so'm", "som", "so’m", "s'om", "uzbek so'mi", "somda", "so'mda"},
}

_SCALE_WORDS = {
    "ming": 1_000, "mln": 1_000_000, "million": 1_000_000,
    "mlrd": 1_000_000_000, "milliard": 1_000_000_000,
    "bin": 1_000, "тыс": 1_000, "тысяча": 1_000,
    "k": 1_000, "m": 1_000_000, "b": 1_000_000_000,
    "mln.": 1_000_000, "mlrd.": 1_000_000_000, "mn": 1_000_000, "тыс.": 1_000
}

UZ_NUM_EX = {**UZ_NUM, "yuz":100, "ming":1000, "mln":1_000_000, "mlrd":1_000_000_000, "million":1_000_000, "milliard":1_000_000_000}

_MONTHS = {
    "yanvar":1, "fevral":2, "mart":3, "aprel":4, "may":5, "iyun":6, "iyul":7,
    "avgust":8, "sentyabr":9, "sentabr":9, "oktyabr":10, "oktabr":10,
    "noyabr":11, "dekabr":12,
    "yanvarya":1, "fevralya":2, "marta":3, "aprelya":4, "maya":5, "iyunya":6, "iyulya":7,
    "avgusta":8, "sentyabrya":9, "oktyabrya":10, "noyabrya":11, "dekabrya":12,
    "yanvar'":1, "fevral'":2, "oktyabr'":10, "noyabr'":11, "dekabr'":12,
}

def _to_latin(s: str) -> str:
    if not s: return ""
    return "".join(_CYR2LAT.get(ch, ch) for ch in s)

# ====================== Raqam normalizatsiyasi ======================

_ARAB_PERS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
def _normalize_numerals(s: str) -> str:
    return (s or "").translate(_ARAB_PERS_MAP)

def _strip_currency_noise(s: str) -> str:
    s = _normalize_numerals(s or "")
    t = _to_latin(s.lower())
    for aliases in _CURRENCY_ALIASES.values():
        for a in aliases:
            t = re.sub(rf"(?i)\b{re.escape(a)}\b", " ", t)
    t = t.replace("$", " ").replace("€", " ").replace("₽", " ").replace("¥", " ")
    t = re.sub(r"[^\d,\.\-\s]", " ", t)
    t = re.sub(r"\s*-\s*", "-", t)
    return re.sub(r"\s+", " ", t).strip()

def _guess_decimal_and_clean(num: str) -> Tuple[str, str]:
    num = num.strip()
    if "," in num and "." not in num:
        if re.search(r",[0-9]{1,6}$", num):
            i, f = num.rsplit(",", 1); i = re.sub(r"[^\d]", "", i); return i, f
        i = re.sub(r"[^\d]", "", num); return i, ""
    if "." in num and "," not in num:
        if re.search(r"\.[0-9]{1,6}$", num):
            i, f = num.rsplit(".", 1); i = re.sub(r"[^\d]", "", i); return i, f
        i = re.sub(r"[^\d]", "", num); return i, ""
    if "." in num and "," in num:
        if re.search(r",[0-9]{1,6}$", num):
            i = re.sub(r"[.,](?=\d{3}\b)", "", num.rsplit(",", 1)[0]); i = re.sub(r"[^\d]", "", i)
            f = num.rsplit(",", 1)[1]; return i, f
        if re.search(r"\.[0-9]{1,6}$", num):
            i = re.sub(r"[.,](?=\d{3}\b)", "", num.rsplit(".", 1)[0]); i = re.sub(r"[^\d]", "", i)
            f = num.rsplit(".", 1)[1]; return i, f
        i = re.sub(r"[^\d]", "", num); return i, ""
    i = re.sub(r"[^\d]", "", num); return i, ""

def _only_digits(s: str) -> str:
    s = _strip_currency_noise(s)
    i, f = _guess_decimal_and_clean(s)
    return i + (("." + f) if f else "")

def _parse_scaled_chunk(tok: str) -> Optional[float]:
    t = _to_latin(tok.lower()).replace("’","'")
    m = re.fullmatch(r"\s*([\-+]?\d[\d\s,\.]*)\s*([a-z\.]+)?\s*", t)
    if not m: return None
    n_raw, scale_word = m.groups()
    n_raw = _only_digits(n_raw)
    if not n_raw: return None
    val = float(n_raw)
    if scale_word:
        sw = scale_word.strip(".").strip()
        if sw in _SCALE_WORDS: val *= _SCALE_WORDS[sw]
    return val


def _compose_scaled_sequence(tokens: List[str]) -> Optional[int]:
    vals: List[float] = []; i = 0; hit = False
    while i < len(tokens):
        v = _parse_scaled_chunk(tokens[i])
        if v is not None: hit = True; vals.append(v); i += 1; continue
        cur = _to_latin(tokens[i].lower())
        nxt = _to_latin(tokens[i + 1].lower()) if i + 1 < len(tokens) else ""
        if re.fullmatch(r"[\d\.,]+", cur) and nxt in _SCALE_WORDS:
            base_v = float(_only_digits(cur) or "0")
            vals.append(base_v * _SCALE_WORDS[nxt]); i += 2; hit = True; continue
        i += 1
    if not hit: return None
    return int(round(sum(vals)))


def _to_number(s: str, *, prefer_int: bool = True) -> Optional[Union[int, float]]:
    if not s: return None
    raw = _strip_currency_noise(s)
    neg = "-" in raw and not re.search(r"-\s*-", raw)
    toks = [t for t in re.split(r"[^\w\.\,\'’\-]+", _to_latin(s).lower()) if t]
    comp = _compose_scaled_sequence(toks)
    if comp is not None: return -comp if neg else comp
    m = re.search(r"-?\s*\d[\d\s,.\u00A0]*\d|\b\d\b", raw)
    if not m: return None
    num = m.group(0)
    if num.strip()[0] != "-" and neg and m.start() == raw.find("-"):
        num = "-" + num
    i, f = _guess_decimal_and_clean(num)
    if not i and not f: return None
    val = float(f"{'-' if num.strip().startswith('-') else ''}{i}.{f or '0'}")
    if prefer_int:
        if (not f) or int(f) == 0: return int(val)
        return int(round(val))
    return val

def _words_to_number(text: str) -> Optional[int]:
    if not text: return None
    t = _to_latin(text.lower()).replace("’","'").replace("`","'")
    tokens = [w for w in re.split(r"[^\w']+", t) if w]
    if not tokens: return None
    total, current, hit = 0, 0, False
    i = 0
    while i < len(tokens):
        w = tokens[i]
        if re.fullmatch(r"\d+", w):
            current += int(w); hit = True; i += 1; continue
        if w in UZ_NUM_EX:
            val = UZ_NUM_EX[w]; hit = True
            if val == 100: current = (current or 1) * 100
            elif val >= 1000: total += (current or 1) * val; current = 0
            else: current += val
            i += 1; continue
        i += 1
    if not hit: return None
    return (total + current) or None


def parse_amount(text: str) -> Optional[int]:
    n = _to_number(text, prefer_int=True)
    if isinstance(n, int): return n if n >= 0 else None
    toks = [t for t in re.split(r"[^\w\.\,\'’\-]+", _to_latin((text or "")).lower()) if t]
    comp = _compose_scaled_sequence(toks)
    if comp is not None and comp >= 0: return comp
    w = _words_to_number(text)
    return w if (w is not None and w >= 0) else None

# ====================== Sana / Valyuta / Moslik ======================

def _normalize_date(text: str, tz: str = "Asia/Tashkent", *, default_to_today: bool = False) -> str:
    """
    Matndan sanani aniqlaydi.
    Yil aytilmasa — joriy yilni qo'yadi.
    default_to_today=False bo'lsa va topilmasa, "" qaytaradi.
    """
    t = _to_latin((text or "")).strip().lower()
    today = dt.datetime.now(ZoneInfo(tz)).date()

    # nisbiy kunlar
    rel_phrases = [
        (UZ_WORDS["today"], 0),
        (UZ_WORDS["yesterday"], -1),
        (UZ_WORDS["tomorrow"], 1),
        (["ertadan keyin", "indin", "ertangi kundan keyin"], 2),
        (["kechadan oldingi kun", "suyanchi kun", "avvalgi kun"], -2),
    ]
    for words, delta in rel_phrases:
        if any(w in t for w in words):
            return (today + dt.timedelta(days=delta)).isoformat()

    # YYYY-MM-DD yoki YYYY/MM/DD
    m = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", t)
    if m:
        y, mo, d_ = map(int, m.groups())
        return dt.date(y, mo, d_).isoformat()

    # DD.MM.YYYY yoki DD/MM/YYYY
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b", t)
    if m:
        d_, mo, y = map(int, m.groups())
        return dt.date(y, mo, d_).isoformat()

    # DD.MM yoki DD/MM — yil yo‘q → joriy yil
    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})(?![.\-/]\d{2,4})\b", t)
    if m:
        d_, mo = map(int, m.groups())
        return dt.date(today.year, mo, d_).isoformat()

    # "15 sentabr 2025"
    m = re.search(r"\b(\d{1,2})\s+([a-z'’]+)\s+(\d{4})\b", t)
    if m:
        d_, month_name, y = m.groups()
        mn = _MONTHS.get(month_name)
        if mn:
            return dt.date(int(y), mn, int(d_)).isoformat()

    # "15 sentabr" — yil yo‘q → joriy yil
    m = re.search(r"\b(\d{1,2})\s+([a-z'’]+)\b", t)
    if m:
        d_, month_name = m.groups()
        mn = _MONTHS.get(month_name)
        if mn:
            return dt.date(today.year, mn, int(d_)).isoformat()

    # "sentabr 2025"
    m = re.search(r"\b([a-z'’]+)\s+(\d{4})\b", t)
    if m:
        month_name, y = m.groups()
        mn = _MONTHS.get(month_name)
        if mn:
            return dt.date(int(y), mn, 1).isoformat()

    return today.isoformat() if default_to_today else ""

def _iso_to_ddmmyyyy(s: str) -> str:
    try:
        y, m, d = map(int, s.split("-"))
        return f"{d:02d}.{m:02d}.{y:04d}"
    except Exception:
        return ""

# ====================== To'lov turi (kuchaytirilgan) ======================

_WORD = r"(?<!\w){}(?!\w)"

_PAY_KWS = {
    "NAQD":  {"naqd", "naqd pul", "naqd pulda", "cash", "нал", "наличные"},
    "KARTA": {"karta", "kartadan", "kartaga", "kartadan to'lov", "plastik", "plastikdan",
              "terminal", "terminaldan", "terminal orqali",
              "pos", "pos-terminal", "posterminal", "posdan",
              "uzcard", "humo", "visa", "mastercard"},
    "ONLAYN":{"payme", "click", "uzum", "click up", "paynet", "payme.uz", "qiwi",
              "yandex", "yoomoney", "paypal", "stripe", "uzum bank", "uzumbank"},
    "BANK":  {"otkazma", "o'tkazma", "o‘tkazma", "bank", "tt", "hisob raqam",
              "hisobraqam", "hisobraqami", "bank o'tkazmasi", "bank orqali",
              "bankdan", "mfo", "inn", "swift", "iban"},
    "AVANS": {"avans", "oldindan", "depozit", "oldindan to'lov", "oldindan to‘lov", "prepayment"},
    "QARZ":  {"qarz", "nasiya", "bo'lib to'lash", "bo‘lib to‘lash", "muddatli to'lov", "muddatli tolov"},
}

_PAY_PRIORITY = ["BANK", "KARTA", "ONLAYN", "NAQD", "AVANS", "QARZ"]


def _normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _detect_payment_type(text: str) -> str:
    t = _to_latin(_normalize_space(text).lower())

    bonus_signals = {
        "BANK":  [r"\b(?:iban|swift|mfo|inn)\b", r"\bhisob\s*raqam[iy]?\b", r"\bbank(?:dan|ga| orqali)?\b"],
        "KARTA": [r"\bpos(?:-?terminal)?\b", r"\bterminaldan\b", r"\bterminal\s+orqali\b",
                  r"\bkartadan\b", r"\bkartaga\b", r"\b(plastik|uzcard|humo|visa|mastercard)\b"],
        "ONLAYN":[r"\b(payme|click|uzum(?:\sbank)?|paypal|stripe)\b"],
        "NAQD":  [r"\bnaqd(\s+pul(da)?)?\b", r"\bcash\b", r"\bнал\b", r"\bналичные\b"],
        "AVANS": [r"\b(avans|depozit|oldindan(?:\s(to['’]lov)?)?)\b", r"\bprepayment\b"],
        "QARZ":  [r"\b(qarz|nasiya|bo['’]lib\s+to['’]lash|muddatli\s+to['’]lov)\b"],
    }

    scores = {k: 0 for k in _PAY_KWS.keys()}
    for label, pats in bonus_signals.items():
        for p in pats:
            if re.search(p, t): scores[label] += 2
    for label, kws in _PAY_KWS.items():
        for k in kws:
            k_norm = _to_latin(k.lower())
            if re.search(_WORD.format(re.escape(k_norm)), t): scores[label] += 1

    best, best_score = None, 0
    for label in _PAY_PRIORITY:
        sc = scores.get(label, 0)
        if sc > best_score:
            best, best_score = label, sc

    # Title-case: Naqd, Karta, Onlayn, Bank, Avans, Qarz
    return (best.capitalize() if best else "")

# ====================== Valyuta normalizatsiyasi (kod) ======================


def _normalize_currency(text: str) -> str:
    t = _to_latin((text or "").lower())
    if any(a in t for a in UZ_WORDS["usd"]): return "USD"
    if any(a in t for a in UZ_WORDS["eur"]): return "EUR"
    if any(a in t for a in UZ_WORDS["rub"]): return "RUB"
    if any(a in t for a in UZ_WORDS["som"]): return "UZS"
    return (text or "").upper().strip()

# ====================== LABEL meta & DD match ======================


def _index_fields_meta(fields_meta: Optional[List[Dict]]) -> Dict[str, Dict]:
    idx = {}
    for m in (fields_meta or []):
        label = (m.get("label") or "").strip()
        if not label: continue
        idx[label.upper()] = {
            "tag": (m.get("tag") or "").upper(),
            "dd_options": list(m.get("dd_options") or [])
        }
    return idx


def _match_currency_option(value: str, options: List[str]) -> Optional[str]:
    if not value or not options: return None
    v = _to_latin((value or "").strip().lower())
    aliases = {
        "usd": {"usd", "$", "dollar", "aqsh dollari", "amerika dollari", "dollor", "dollarlar"},
        "eur": {"eur", "€", "euro", "evro"},
        "rub": {"rub", "₽", "rubl"},
        "uzs": {"uzs", "so'm", "som", "so’m", "s'om", "uzbek so'mi", "so'mi", "so'mda", "somda"},
    }
    key = None
    for k, al in aliases.items():
        if any(a in v for a in al): key = k; break
    if not key: return None
    for o in options:
        ol = _to_latin(o).strip().lower()
        if ol == key or any(a in ol for a in aliases[key]):
            return o
    return None


def _smart_match(value: str, options: List[str]) -> str:
    """#DD uchun yaqin mos: (0) valyuta-alias, (1) exact substring, (2) raqam yaqinligi, (3) token overlap."""
    if not options:
        return value or ""
    # 0) valyuta alias (USD↔Dollar, UZS↔So'm, …)
    cur_hit = _match_currency_option(value, options)
    if cur_hit: return cur_hit

    v = _to_latin(value or "").strip().lower()
    opts_lat = [_to_latin(o).strip() for o in options if o]

    # 1) exact substring
    for o, ol in zip(options, [x.lower() for x in opts_lat]):
        if v and v in ol:
            return o

    # 2) number proximity
    def first_int(s: str) -> Optional[int]:
        m = re.search(r"\d+", _to_latin(s)); return int(m.group(0)) if m else None
    vnum = first_int(v)
    if vnum is not None:
        best, best_diff = None, 10**12
        for o in options:
            onum = first_int(o)
            if onum is None: continue
            d = abs(onum - vnum)
            if d < best_diff: best, best_diff = o, d
        if best is not None: return best

    # 3) token overlap
    vtoks = set([t for t in re.split(r"\W+", v) if t])
    best, best_score = None, 0.0
    for o in options:
        otoks = set([t for t in re.split(r"\W+", _to_latin(o).lower()) if t])
        sc = len(vtoks & otoks) / max(1, len(vtoks)) if vtoks else 0
        if sc > best_score: best, best_score = o, sc
    return best or options[0]


def _apply_tag_rules_to_labels(label_values: Dict[str, str], fields_meta: Optional[List[Dict]], *, raw_text: str = "") -> Dict[str, str]:
    if not label_values and not fields_meta: return {}
    idx = _index_fields_meta(fields_meta)
    out: Dict[str, str] = {}
    for m in (fields_meta or []):
        lbl = (m.get("label") or "").strip()
        tag = (m.get("tag") or "")
        dd_opts = list(m.get("dd_options") or [])
        cur_val = (label_values or {}).get(lbl, "")
        out[lbl] = _coerce_by_tag(tag, str(cur_val), dd_opts, fallback_text=raw_text)
    return out


def _push_into_label_values(out: Dict[str, str], fields_meta: Optional[List[Dict]]) -> Dict[str, str]:
    lv = dict(out.get("LABEL_VALUES") or {})
    if not fields_meta: return lv

    def find_label(*cands: str) -> Optional[str]:
        cset = [c.casefold() for c in cands if c]
        for m in fields_meta:
            name = (m.get("label") or "").strip()
            if not name: continue
            nm = name.casefold()
            if any(c in nm for c in cset): return name
        return None

    m = {
        "SANA": ("sana", "#dt"),
        "TOLOV_TURI": ("to'lov turi", "tolov turi", "turi"),
        "VALYUTA": ("valyuta", "currency"),
        "KURS": ("kurs", "rate"),
        "SUMMA": ("summa", "miqdor", "amount"),
        "SUMMA UZS": ("summa uzs", "uzs", "so'm", "som"),
        "IZOH": ("izoh", "comment", "description"),
        "ISM": ("ism", "mijoz", "client", "fio"),
        "HARAJAT": ("harajat", "xarajat", "kategoriya", "toifa"),
    }
    for key, cands in m.items():
        lbl = find_label(*cands)
        if lbl and out.get(key): lv[lbl] = out[key]
    return lv

# ---------------------- DD -> Semantika qaytarish ----------------------

def _pull_semantics_from_dd(lv: Dict[str, str], fields_meta: Optional[List[Dict]]) -> Dict[str, str]:
    """#DD qiymatlari mavjud bo'lsa, semantik maydonlarni BEVOSITA shu variantlarga tenglaymiz."""
    res: Dict[str, str] = {}

    def pick(label_aliases: List[str]) -> Optional[str]:
        if not lv: return None
        for m in (fields_meta or []):
            lbl = (m.get("label") or "").strip()
            nm = lbl.casefold()
            if any(a in nm for a in label_aliases):
                v = lv.get(lbl, "")
                if v: return v
        return None

    pay = pick(["to'lov turi", "tolov turi", "turi"])
    cur = pick(["valyuta", "currency"])
    har = pick(["harajat", "xarajat", "kategoriya", "toifa"])

    if pay: res["TOLOV_TURI"] = pay
    if cur: res["VALYUTA"]   = cur
    if har: res["HARAJAT"]    = har
    return res

# ====================== AI Promptlar ======================

SYSTEM = (
    "Siz o'zbek tilida ishlaydigan aniq va qat'iy parser yordamchisiz. "
    "Sizga foydalanuvchi gapirgan matn va elektron jadvallardagi shablon metama'lumotlari (teg/label/#DD variantlar) beriladi. "
    "Siz hech qanday izohsiz faqat JSON qaytarasiz. Hech qanday qo'shimcha matn yozmaysiz. "
    "Siz barcha ajratish, moslashtirish, formatlash ishlarini o'zingiz bajarasiz. "
    "Siz quyidagilarni qaytarishingiz shart: "
    "1) 'SANA', 'HARAJAT', 'TOLOV_TURI', 'VALYUTA', 'KURS', 'SUMMA', 'IZOH', 'ISM' – semantik qiymatlar; "
    "2) 'LABEL_VALUES' – label -> yoziladigan qiymatlar (tag qoidalariga muvofiq). "
    "Agar qiymat yo'q bo'lsa, bo'sh string qaytaring."
)

USER_GUIDE = (
    "Qoidalar:\n"
    "- #DT: qiymatni dd.mm.yyyy formatida qaytaring (masalan: 05.10.2025). "
    "Sana matndan kelsin (bugun/kecha/ertaga iboralari ham), yil aytilmasa joriy yil; natija har doim dd.mm.yyyy.\n"
    "- #DD: faqat berilgan variantlardan eng mosini tanlang. Variantlar labelga biriktiriladi.\n"
    "- #NB: faqat butun raqam qaytaring (kasr bo'lsa, yaxlitlang yoki mosini kiriting).\n"
    "- #FF: bo'sh string \"\" (bu formula katagi, yozilmaydi).\n"
    "- #TX: erkin matn.\n"
    "- Semantik (masalan, VALYUTA) aniqlansa, mos #DD labelni ham shu qiymatga keltiring.\n"
    "- JSON dan boshqa hech narsa qaytarmang."
)

# ====================== ASOSIY FUNKSIYA ======================

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

    from rapidfuzz import process, fuzz

    def _to_lat_local(s: str) -> str:
        s = (s or "")
        s = s.replace("’", "'").replace("`", "'")
        s = re.sub(r"\s+", " ", s)
        return s.lower().strip()

    def _tokens_local(s: str) -> List[str]:
        return [t for t in re.split(r"[^\w']+", _to_lat_local(s)) if t]

    v = _to_lat_local(value)
    opts = [o for o in options if o]
    opts_lat = [_to_lat_local(o) for o in opts]

    # 1) token-containment
    candidates = []
    for idx, (o, ol) in enumerate(zip(opts, opts_lat)):
        ot = _tokens_local(ol)
        if v and v in ot:
            candidates.append(((len(ot), len(ol), -idx), o))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # 2) substring-containment
    containers = []
    for idx, (o, ol) in enumerate(zip(opts, opts_lat)):
        if v and v in ol:
            containers.append(((len(ol), len(_tokens_local(ol)), -idx), o))
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
    vt = set(_tokens_local(v))
    if vt:
        best_o, best_sc = None, -1.0
        for o in opts:
            ot = set(_tokens_local(o))
            if not ot:
                continue
            sc = (len(vt & ot) / max(1, len(vt))) + (len(o) / 1000.0)
            if sc > best_sc:
                best_sc, best_o = sc, o
        if best_o:
            return best_o

    # 6) fallback
    return opts[0]


def _coerce_by_tag(tag: str, value: str, dd_options: List[str], *, fallback_text: str = "") -> str:
    """
    TAG qoidalari (FAOLLASHTIRILGAN FIX):
    - #DT: sana -> dd.mm.yyyy (agar matnda yil ko‘rsatilmagan bo‘lsa, AI bergan yilni joriy yilga tuzatish saqlanadi)
    - #DD: FAQAT value’dan tanlaydi (fallback_text ishlatilmaydi!). value bo‘sh bo‘lsa "".
    - #NB: FAQAT value’dan raqam oladi (fallback_text ishlatilmaydi!). value bo‘sh bo‘lsa "".
    - #FF: bo'sh
    - #TX: erkin matn (value)
    """
    t = (tag or "").upper().strip()

    def _text_has_year(txt: str) -> bool:
        return bool(re.search(r"\b(19|20)\d{2}\b", _to_latin((txt or "").lower())))

    if t == "#DT":
        v = (value or "").strip()
        # 1) Agar allaqachon dd.mm.yyyy bo'lsa — lekin matnda yil yo'q bo'lsa → joriy yilga majburlaymiz
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", v):
            if not _text_has_year(fallback_text):
                try:
                    d, m, y = map(int, v.split("."))
                    y_now = dt.datetime.now(ZoneInfo("Asia/Tashkent")).year
                    return f"{d:02d}.{m:02d}.{y_now:04d}"
                except Exception:
                    pass
            return v
        # 2) Aks holda, value va (oxirgi chora sifatida) fallback_textdan sanani urinamiz
        iso = _normalize_date(v, default_to_today=False) or _normalize_date(fallback_text, default_to_today=False)
        return _iso_to_ddmmyyyy(iso) if iso else ""

    if t.startswith("#DD"):
        # fallback_text ishlatilmaydi; har doim ro‘yxatdan snap
        if not dd_options:
            return ""
        base = (value or "").strip()
        return _smart_match_plus(base, dd_options) if base else ""

    if t == "#NB":
        # 🔒 MUHIM: endi fallback_text ishlatilmaydi — KURS bo‘sh qolsa, butun matndan raqam tortib kelmasin.
        try:
            n = parse_amount(value or "")
            return str(n) if n is not None else ""
        except Exception:
            return ""

    if t == "#FF":
        return ""

    return (value or "").strip()

async def extract_with_ai(
    text: str,
    dd_options: Dict[str, List[str]],
    tz: str = "Asia/Tashkent",
    fields_meta: Optional[List[Dict]] = None,
) -> Dict[str, str]:
    """
    Asosiy oqim:
    1) AI'dan JSON olamiz
    2) Semantik maydonlarni tozalaymiz/formatlaymiz
    3) LABEL_VALUES ni tag qoidalari bilan hosil qilamiz (#DD har doim variant)
    4) KURS/SUMMA UZS qoidalari:
       - VALYUTA=UZS -> KURS=""
         SUMMA UZS = SUMMA (agar butun bo‘lsa)
       - VALYUTA in {USD,EUR,RUB} -> KURS faqat foydalanuvchi bergan bo‘lsa; bermasa ""
         SUMMA UZS faqat SUMMA va KURS ikkalasi ham bo‘lsa = SUMMA*KURS
       - Noma’lum valyuta -> KURS="", SUMMA UZS=""
    5) DD maydonlardan semantiklarni qayta gelishtirish (ustuvor)
    6) **KURS va SUMMA UZS ni LABEL_VALUES bilan ikkilamchi sinxronlash** (majburiy!)
    """
    _require_openai()

    fm = [{"tag": (m.get("tag") or ""),
           "label": (m.get("label") or ""),
           "dd_options": (m.get("dd_options") or [])} for m in (fields_meta or [])]

    payload = {
        "timezone": tz,
        "text": text or "",
        "dropdowns_global": dd_options or {},
        "fields_meta": fm,
        "need": {
                "semantic_keys": ["SANA","HARAJAT","TOLOV_TURI","VALYUTA","KURS","SUMMA","IZOH","ISM"],
                "label_values": True
        }
    }

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_GUIDE},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    out = await _create_json_completion_safe(OPENAI_MODEL, messages)

    # 2) Semantik maydonlarni normalize
    for k in ["SANA","HARAJAT","TOLOV_TURI","VALYUTA","KURS","SUMMA","IZOH","ISM"]:
        v = out.get(k, ""); out[k] = "" if v is None else str(v)

    # Sana: dd.mm.yyyy (AI 2015 deb yuborsa ham, matnda yil bo‘lmasa -> joriy yilga tuzatamiz)
    out["SANA"] = _coerce_by_tag("#DT", out.get("SANA",""), [], fallback_text=text) or out.get("SANA","")

    # To'lov turi — bo'sh qolsa heuristika
    if not out.get("TOLOV_TURI"):
        pay = _detect_payment_type(f"{text} {out.get('IZOH','')}")
        if pay: out["TOLOV_TURI"] = pay

    # Valyuta — kodga normalize yoki taxmin
    if out.get("VALYUTA"):
        out["VALYUTA"] = _normalize_currency(out["VALYUTA"])
    else:
        guess_val = _normalize_currency(f"{text} {out.get('IZOH','')}")
        if guess_val in {"USD","EUR","RUB","UZS"}:
            out["VALYUTA"] = guess_val

    # 3) LABEL_VALUES ni tag qoidalari bilan normallashtirish (#DD doim variantdan)
    lv_raw  = dict(out.get("LABEL_VALUES") or {})
    lv_norm = _apply_tag_rules_to_labels(lv_raw, fields_meta, raw_text=text)
    out["LABEL_VALUES"] = lv_norm

    # Semantik -> labelga yoyish
    out["LABEL_VALUES"] = _apply_tag_rules_to_labels(
        _push_into_label_values(out, fields_meta),
        fields_meta,
        raw_text=text
    )

    # 4) KURS / SUMMA / SUMMA UZS (avto-taxmin yo‘q)
    val = (out.get("VALYUTA") or "").upper().strip()
    kurs = parse_amount(str(out.get("KURS", "")))
    summa = parse_amount(str(out.get("SUMMA", "")))

    # qayta hisoblashdan oldin tozalash
    out.pop("SUMMA UZS", None)

    if val == "UZS":
        # UZS bo'lsa KURS doimo bo'sh
        out["KURS"] = ""
        # SUMMA UZS = SUMMA (agar SUMMA int bo'lsa)
        if isinstance(summa, int):
            out["SUMMA UZS"] = str(summa)

    elif val in {"USD", "EUR", "RUB"}:
        # Chet valyuta: foydalanuvchi kurs bermasa bo'sh
        if not isinstance(kurs, int):
            out["KURS"] = ""
        # SUMMA UZS faqat SUMMA va KURS ikkalasi bo'lsa
        if isinstance(summa, int) and isinstance(kurs, int):
            out["SUMMA UZS"] = str(int(summa * kurs))

    else:
        # Valyuta noaniq: KURS va SUMMA UZS bo'sh
        out["KURS"] = ""

    # 5) DD -> Semantik (ustuvor)
    dd_sem = _pull_semantics_from_dd(out.get("LABEL_VALUES", {}), fields_meta)
    out.update(dd_sem)

    # 6) KURS/SUMMA UZS ni LABEL_VALUES bilan ikkilamchi sinxronlash (majburiy!)
    if fields_meta:
        # label topuvchi kichik yordamchi
        def _find_label(*cands: str) -> Optional[str]:
            cset = [c.casefold() for c in cands if c]
            for m in fields_meta:
                name = (m.get("label") or "").strip()
                if not name: continue
                nm = name.casefold()
                if any(c in nm for c in cset): return name
            return None

        lv = dict(out.get("LABEL_VALUES") or {})

        lbl_kurs = _find_label("kurs", "rate")
        if lbl_kurs is not None:
            lv[lbl_kurs] = out.get("KURS","")

        lbl_sumuzs = _find_label("summa uzs", "uzs", "so'm", "som")
        if lbl_sumuzs is not None:
            lv[lbl_sumuzs] = out.get("SUMMA UZS","")

        # qayta tag qoidasidan o‘tkazamiz (#NB/#DD mos yozilsin)
        out["LABEL_VALUES"] = _apply_tag_rules_to_labels(lv, fields_meta, raw_text=text)

    return out



