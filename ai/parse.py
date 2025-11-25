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

OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-5.1")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gpt-5.1")

_CHAT_COMPAT_PREFIXES = ("gpt-5.1", "gpt-4", "o3", "o1")


def _require_openai():
    if _client is None:
        raise RuntimeError("OPENAI_API_KEY topilmadi yoki OpenAI klienti ishga tushmadi.")


def _supports_temperature(model_name: str) -> bool:
    model_name = (model_name or "").lower()
    return model_name.startswith(("gpt-5.1", "o5", "gpt-4o", "o4"))


def _is_chat_compatible(model: str) -> bool:
    return (model or "").lower().startswith(_CHAT_COMPAT_PREFIXES)


# ===== Narx sozlamalari (1M token uchun) =====
PRICE_PROMPT     = float(os.getenv("OPENAI_PRICE_PROMPT", "0"))       # $ per 1M input tokens
PRICE_COMPLETION = float(os.getenv("OPENAI_PRICE_COMPLETION", "0"))   # $ per 1M output tokens
PRICE_CURRENCY   = os.getenv("OPENAI_PRICE_CURRENCY", "USD")


def _estimate_cost_from_usage(usage: dict) -> float:
    """
    usage: {"input_tokens": int, "output_tokens": int, "total_tokens": int}
    natija: taxminiy cost (float, PRICE_CURRENCY bo'yicha)
    """
    if not usage:
        return 0.0

    in_toks  = int(usage.get("input_tokens", 0))
    out_toks = int(usage.get("output_tokens", 0))

    cost_prompt     = (in_toks  / 1_000_000.0) * PRICE_PROMPT
    cost_completion = (out_toks / 1_000_000.0) * PRICE_COMPLETION
    return cost_prompt + cost_completion


def _extract_text_from_response(resp) -> str:
    """
    Responses API va Chat API javoblaridan matnni xavfsiz olish.
    """
    text = getattr(resp, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    # responses API (output[].content[]...)
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

    # chat/completions
    try:
        return resp.choices[0].message.content
    except Exception:
        return ""


async def _create_json_completion_safe(model: str, messages: List[Dict]) -> Dict:
    """
    Birlamchi yo'l: Responses API (gpt-5-mini / 4.1 / 4o va h.k.)
    Fallback: Responses (fallback modeli), so'ng chat/completions (fallback yoki primary chat-compatible bo'lsa).
    Har bir chaqiriqda usage + taxminiy cost logga chiqadi.
    """
    _require_openai()

    # ---- Bitta string prompt (Responses API uchun) ----
    def _join_messages(msgs: List[Dict]) -> str:
        return "\n\n".join(f"{(m.get('role') or '').upper()}:\n{m.get('content') or ''}" for m in msgs)

    prompt_text = _join_messages(messages)

    # ---- Responses API kwargs: temperature YO'Q ----
    def _mk_responses_kwargs(mdl: str) -> Dict:
        kwargs = {
            "model": mdl,
            "input": prompt_text,
            "response_format": {"type": "text"},  # JSON qaytadi, lekin text sifatida olamiz
        }
        # Maksimal chiqish tokenlarini cheklash (tezroq ishlashi uchun)
        try:
            max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "400"))
        except Exception:
            max_tokens = 400
        kwargs["max_output_tokens"] = max_tokens
        return kwargs

    # ---- Chat API kwargs: response_format JSON, temperature ehtiyotkorlik bilan ----
    def _mk_chat_kwargs(mdl: str, *, with_temperature: bool = True) -> Dict:
        kwargs = {
            "model": mdl,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if with_temperature and _supports_temperature(mdl):
            try:
                kwargs["temperature"] = float(os.getenv("OPENAI_TEMPERATURE", "0.1"))
            except Exception:
                kwargs["temperature"] = 0.1
        return kwargs

    async def _call_responses_api(mdl: str) -> Dict:
        """
        Asosiy tez va arzon yo'l: Responses API.
        """
        resp = await _client.responses.create(**_mk_responses_kwargs(mdl))

        usage = getattr(resp, "usage", None)
        if usage:
            usage_dict = {
                "input_tokens":  getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "total_tokens":  getattr(usage, "total_tokens", 0),
            }
            cost = _estimate_cost_from_usage(usage_dict)
            print(f"[OpenAI responses] model={mdl} usage={usage_dict} cost≈{cost:.6f} {PRICE_CURRENCY}")

        text = _extract_text_from_response(resp)
        try:
            return json.loads(text or "{}")
        except Exception:
            return {}

    async def _call_chat_api(mdl: str) -> Dict:
        """
        Fallback: chat/completions (gpt-5.1, gpt-4o va hokazo).
        """
        async def _once(with_temperature: bool) -> Dict:
            resp = await _client.chat.completions.create(
                **_mk_chat_kwargs(mdl, with_temperature=with_temperature)
            )

            usage = getattr(resp, "usage", None)
            if usage:
                usage_dict = {
                    "input_tokens":  getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0),
                    "output_tokens": getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0),
                    "total_tokens":  getattr(usage, "total_tokens", 0),
                }
                cost = _estimate_cost_from_usage(usage_dict)
                print(f"[OpenAI chat] model={mdl} usage={usage_dict} cost≈{cost:.6f} {PRICE_CURRENCY}")

            text = (resp.choices[0].message.content or "")
            try:
                return json.loads(text or "{}")
            except Exception:
                return {}

        try:
            # 1-urinish: temperature bilan
            return await _once(with_temperature=True)
        except Exception as e:
            s = str(e).lower()
            if "temperature" in s and any(
                key in s
                for key in ["unsupported parameter", "unsupported value", "does not support"]
            ):
                # 2-urinish: temperature-siz
                return await _once(with_temperature=False)
            raise

    primary = (model or "").strip() or OPENAI_MODEL
    fallback = (FALLBACK_MODEL or "").strip()

    # 1) Avval har doim primary bilan Responses API
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
    if not s:
        return ""
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
            i, f = num.rsplit(",", 1)
            i = re.sub(r"[^\d]", "", i)
            return i, f
        i = re.sub(r"[^\d]", "", num)
        return i, ""
    if "." in num and "," not in num:
        if re.search(r"\.[0-9]{1,6}$", num):
            i, f = num.rsplit(".", 1)
            i = re.sub(r"[^\d]", "", i)
            return i, f
        i = re.sub(r"[^\d]", "", num)
        return i, ""
    if "." in num and "," in num:
        if re.search(r",[0-9]{1,6}$", num):
            i = re.sub(r"[.,](?=\d{3}\b)", "", num.rsplit(",", 1)[0])
            i = re.sub(r"[^\d]", "", i)
            f = num.rsplit(",", 1)[1]
            return i, f
        if re.search(r"\.[0-9]{1,6}$", num):
            i = re.sub(r"[.,](?=\d{3}\b)", "", num.rsplit(".", 1)[0])
            i = re.sub(r"[^\d]", "", i)
            f = num.rsplit(".", 1)[1]
            return i, f
        i = re.sub(r"[^\d]", "", num)
        return i, ""
    i = re.sub(r"[^\d]", "", num)
    return i, ""


def _only_digits(s: str) -> str:
    s = _strip_currency_noise(s)
    i, f = _guess_decimal_and_clean(s)
    return i + (("." + f) if f else "")


def _parse_scaled_chunk(tok: str) -> Optional[float]:
    t = _to_latin(tok.lower()).replace("’","'")
    m = re.fullmatch(r"\s*([\-+]?\d[\d\s,\.]*)\s*([a-z\.]+)?\s*", t)
    if not m:
        return None
    n_raw, scale_word = m.groups()
    n_raw = _only_digits(n_raw)
    if not n_raw:
        return None
    val = float(n_raw)
    if scale_word:
        sw = scale_word.strip(".").strip()
        if sw in _SCALE_WORDS:
            val *= _SCALE_WORDS[sw]
    return val


def _compose_scaled_sequence(tokens: List[str]) -> Optional[int]:
    vals: List[float] = []
    i = 0
    hit = False
    while i < len(tokens):
        v = _parse_scaled_chunk(tokens[i])
        if v is not None:
            hit = True
            vals.append(v)
            i += 1
            continue
        cur = _to_latin(tokens[i].lower())
        nxt = _to_latin(tokens[i + 1].lower()) if i + 1 < len(tokens) else ""
        if re.fullmatch(r"[\d\.,]+", cur) and nxt in _SCALE_WORDS:
            base_v = float(_only_digits(cur) or "0")
            vals.append(base_v * _SCALE_WORDS[nxt])
            i += 2
            hit = True
            continue
        i += 1
    if not hit:
        return None
    return int(round(sum(vals)))


def _to_number(s: str, *, prefer_int: bool = True) -> Optional[Union[int, float]]:
    if not s:
        return None
    raw = _strip_currency_noise(s)
    neg = "-" in raw and not re.search(r"-\s*-", raw)
    toks = [t for t in re.split(r"[^\w\.\,\'’\-]+", _to_latin(s).lower()) if t]
    comp = _compose_scaled_sequence(toks)
    if comp is not None:
        return -comp if neg else comp
    m = re.search(r"-?\s*\d[\d\s,.\u00A0]*\d|\b\d\b", raw)
    if not m:
        return None
    num = m.group(0)
    if num.strip()[0] != "-" and neg and m.start() == raw.find("-"):
        num = "-" + num
    i, f = _guess_decimal_and_clean(num)
    if not i and not f:
        return None
    val = float(f"{'-' if num.strip().startswith('-') else ''}{i}.{f or '0'}")
    if prefer_int:
        if (not f) or int(f) == 0:
            return int(val)
        return int(round(val))
    return val


def _words_to_number(text: str) -> Optional[int]:
    if not text:
        return None
    t = _to_latin(text.lower()).replace("’","'").replace("`","'")
    tokens = [w for w in re.split(r"[^\w']+", t) if w]
    if not tokens:
        return None
    total, current, hit = 0, 0, False
    i = 0
    while i < len(tokens):
        w = tokens[i]
        if re.fullmatch(r"\d+", w):
            current += int(w)
            hit = True
            i += 1
            continue
        if w in UZ_NUM_EX:
            val = UZ_NUM_EX[w]
            hit = True
            if val == 100:
                current = (current or 1) * 100
            elif val >= 1000:
                total += (current or 1) * val
                current = 0
            else:
                current += val
            i += 1
            continue
        i += 1
    if not hit:
        return None
    return (total + current) or None


def parse_amount(text: str) -> Optional[int]:
    n = _to_number(text, prefer_int=True)
    if isinstance(n, int):
        return n if n >= 0 else None
    toks = [t for t in re.split(r"[^\w\.\,\'’\-]+", _to_latin((text or "")).lower()) if t]
    comp = _compose_scaled_sequence(toks)
    if comp is not None and comp >= 0:
        return comp
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
            if re.search(p, t):
                scores[label] += 2
    for label, kws in _PAY_KWS.items():
        for k in kws:
            k_norm = _to_latin(k.lower())
            if re.search(_WORD.format(re.escape(k_norm)), t):
                scores[label] += 1

    best, best_score = None, 0
    for label in _PAY_PRIORITY:
        sc = scores.get(label, 0)
        if sc > best_score:
            best, best_score = label, sc

    # Title-case: Naqd, Karta, Onlayn, Bank, Avans, Qarz
    return (best.capitalize() if best else "")


# ====================== Valyuta normalizatsiyasi (kod) ======================

def _normalize_currency(text: str) -> str:
    """
    Valyutani ehtiyotkorlik bilan aniqlash:
      - avval so'm (UZS) signaliga qaraymiz
      - keyin EUR, RUB
      - USD faqat 'usd' / 'dollar' so'zlari bo'lsa (yalang '$' emas!)
    """
    t = _to_latin((text or "").lower())

    # alohida flaglar
    has_uzs = any(a in t for a in UZ_WORDS["som"])
    # '$' ni hisobga olmaymiz, faqat 'usd', 'dollar', ...
    has_usd_word = any(a in t for a in UZ_WORDS["usd"] if a != "$")
    has_eur = any(a in t for a in UZ_WORDS["eur"])
    has_rub = any(a in t for a in UZ_WORDS["rub"])

    # 1) So'm bo'lsa – har doim UZS ustuvor
    if has_uzs:
        return "UZS"

    # 2) Keyin EUR / RUB
    if has_eur:
        return "EUR"
    if has_rub:
        return "RUB"

    # 3) Keyin USD (faqat so'z bo'lsa)
    if has_usd_word:
        return "USD"

    # 4) Aks holda – o'zini qaytaramiz (yo bo'sh qolishi mumkin)
    return (text or "").upper().strip()




# ====================== LABEL meta & DD match ======================

def parse_dd_tag(tag: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    #DD taglarni parslash:

      "#DD-HARAJAT!P5:P" -> ("#DD", "HARAJAT", "P5:P")
      "#DD-VALYUTA!O5:O" -> ("#DD", "VALYUTA", "O5:O")
      "#DD-HARAJAT"      -> ("#DD", "HARAJAT", None)
      "#DD"              -> ("#DD", None, None)
      boshqa taglar      -> (tag, None, None)
    """
    if not tag:
        return "", None, None

    t = tag.strip()
    up = t.upper()
    if not up.startswith("#DD"):
        return t, None, None

    # baza tag har doim #DD
    base_tag = "#DD"
    rest = t[3:]  # "#DD" dan keyingi qism

    list_key: Optional[str] = None
    a1_range: Optional[str] = None

    if rest.startswith("-"):
        rest = rest[1:]
        if "!" in rest:
            list_part, range_part = rest.split("!", 1)
            list_key = (list_part or "").strip() or None
            a1_range = (range_part or "").strip() or None
        else:
            list_key = rest.strip() or None

    return base_tag, list_key, a1_range


def _index_fields_meta(fields_meta: Optional[List[Dict]]) -> Dict[str, Dict]:
    idx = {}
    for m in (fields_meta or []):
        label = (m.get("label") or "").strip()
        if not label:
            continue
        idx[label.upper()] = {
            "tag": (m.get("tag") or "").upper(),
            "dd_options": list(m.get("dd_options") or [])
        }
    return idx


def _match_currency_option(value: str, options: List[str]) -> Optional[str]:
    if not value or not options:
        return None
    v = _to_latin((value or "").strip().lower())
    aliases = {
        "usd": {"usd", "$", "dollar", "aqsh dollari", "amerika dollari", "dollor", "dollarlar"},
        "eur": {"eur", "€", "euro", "evro"},
        "rub": {"rub", "₽", "rubl"},
        "uzs": {"uzs", "so'm", "som", "so’m", "s'om", "uzbek so'mi", "so'mi", "so'mda", "somda"},
    }
    key = None
    for k, al in aliases.items():
        if any(a in v for a in al):
            key = k
            break
    if not key:
        return None
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
    if cur_hit:
        return cur_hit

    v = _to_latin(value or "").strip().lower()
    opts_lat = [_to_latin(o).strip() for o in options if o]

    # 1) exact substring
    for o, ol in zip(options, [x.lower() for x in opts_lat]):
        if v and v in ol:
            return o

    # 2) number proximity
    def first_int(s: str) -> Optional[int]:
        m = re.search(r"\d+", _to_latin(s))
        return int(m.group(0)) if m else None

    vnum = first_int(v)
    if vnum is not None:
        best, best_diff = None, 10**12
        for o in options:
            onum = first_int(o)
            if onum is None:
                continue
            d = abs(onum - vnum)
            if d < best_diff:
                best, best_diff = o, d
        if best is not None:
            return best

    # 3) token overlap
    vtoks = set([t for t in re.split(r"\W+", v) if t])
    best, best_score = None, 0.0
    for o in options:
        otoks = set([t for t in re.split(r"\W+", _to_latin(o).lower()) if t])
        sc = len(vtoks & otoks) / max(1, len(vtoks)) if vtoks else 0
        if sc > best_score:
            best, best_score = o, sc
    return best or options[0]


def _apply_tag_rules_to_labels(label_values: Dict[str, str], fields_meta: Optional[List[Dict]], *, raw_text: str = "") -> Dict[str, str]:
    if not label_values and not fields_meta:
        return {}
    _ = _index_fields_meta(fields_meta)
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
    if not fields_meta:
        return lv

    def find_label(*cands: str) -> Optional[str]:
        cset = [c.casefold() for c in cands if c]
        for m in fields_meta:
            name = (m.get("label") or "").strip()
            if not name:
                continue
            nm = name.casefold()
            if any(c in nm for c in cset):
                return name
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
        if lbl and out.get(key):
            lv[lbl] = out[key]
    return lv


# ---------------------- DD -> Semantika qaytarish ----------------------

def _pull_semantics_from_dd(lv: Dict[str, str], fields_meta: Optional[List[Dict]]) -> Dict[str, str]:
    """#DD qiymatlari mavjud bo'lsa, semantik maydonlarni BEVOSITA shu variantlarga tenglaymiz."""
    res: Dict[str, str] = {}

    def pick(label_aliases: List[str]) -> Optional[str]:
        if not lv:
            return None
        for m in (fields_meta or []):
            lbl = (m.get("label") or "").strip()
            nm = lbl.casefold()
            if any(a in nm for a in label_aliases):
                v = lv.get(lbl, "")
                if v:
                    return v
        return None

    pay = pick(["to'lov turi", "tolov turi", "turi"])
    cur = pick(["valyuta", "currency"])
    har = pick(["harajat", "xarajat", "kategoriya", "toifa"])

    if pay:
        res["TOLOV_TURI"] = pay
    if cur:
        res["VALYUTA"]   = cur
    if har:
        res["HARAJAT"]   = har
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
      0) Valyuta bo'lsa: So'm / Dollar / Rubl kabi aliaslardan eng mosini olish
      1) TOKEN-CONTAINMENT (v butun token sifatida ichida) → (token_count DESC, length DESC)
      2) SUBSTRING-CONTAINMENT (v ichida) → (length DESC, token_count DESC)
      3) EXACT EQUALITY (normalize qilingan)
      4) FUZZY (WRatio, cutoff=50)
      5) TOKEN-OVERLAP (+ uzunlikka kichik bonus)
      6) FALLBACK: birinchi opsiya
    """
    if not options:
        return value or ""

    # 0) Agar bu valyuta bo'yicha bo'lsa (So'm / Dollar / Rubl va hok), aliaslardan eng mosini olamiz
    cur_hit = _match_currency_option(value, options)
    if cur_hit:
        return cur_hit

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



def _coerce_by_tag(tag: str, value: str, dd_options: List[str], *, fallback_text: str = "", google_email: str = "") -> str:
    t = (tag or "").upper().strip()

    # --- #DT-A ---
    if t == "#DT-A":
        now = dt.datetime.now(ZoneInfo("Asia/Tashkent"))
        return now.strftime("%d.%m.%Y")

    # --- #TX-A ---
    if t == "#TX-A":
        now = dt.datetime.now(ZoneInfo("Asia/Tashkent"))
        return now.strftime("%H:%M:%S")

    # --- #GA-A ---
    if t == "#GA-A":
        return google_email or ""

    # --- #DT ---
    if t == "#DT":
        v = (value or "").strip()

        # agar DD.MM.YYYY bo'lsa — shu formatni qaytaramiz
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", v):
            return v

        # ISO aniqlash yoki matndan chiqarish
        iso = _normalize_date(v, default_to_today=False)
        if not iso:
            iso = _normalize_date(fallback_text, default_to_today=False)

        # Agar ISO topilmasa → bo‘sh
        if not iso or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
            return ""

        # ISO → DD.MM.YYYY
        try:
            return _iso_to_ddmmyyyy(iso)
        except:
            return ""


    # --- #DD ---
    if t.startswith("#DD"):
        base = (value or "").strip()
        if not dd_options:
            return ""
        return _smart_match_plus(base, dd_options) if base else ""

    # -------- #NB (number-only) --------
    if t == "#NB":
        try:
            n = parse_amount(value or "")
            return str(n) if n is not None else ""
        except:
            return ""

    # --- #FF ---
    if t == "#FF":
        return ""

    # --- #TX ---
    return (value or "").strip()


async def extract_with_ai(
    text: str,
    dd_options: Dict[str, List[str]],
    tz: str = "Asia/Tashkent",
    fields_meta: Optional[List[Dict]] = None,
    google_email: str = ""
) -> Dict[str, str]:

    _require_openai()

    # ======================================================
    # 1) FIELD META tayyorlash (#DD, #DT, #NB, #TX ...)
    # ======================================================
    normalized_fm: List[Dict] = []
    dd_global = dd_options or {}

    for m in (fields_meta or []):
        raw_tag = (m.get("tag") or "")
        label = (m.get("label") or "")
        per_field_dd = list(m.get("dd_options") or [])

        base_tag = raw_tag
        up = raw_tag.upper()

        # #DD-HARAJAT!P5:P → dd_options globaldan olish
        if up.startswith("#DD"):
            base_tag, list_key, a1_range = parse_dd_tag(raw_tag)

            if not per_field_dd:  
                candidates = []
                if list_key:
                    candidates += [list_key, list_key.upper()]
                if a1_range:
                    candidates += [a1_range, a1_range.upper()]
                if raw_tag:
                    candidates += [raw_tag, raw_tag.upper()]
                if label:
                    candidates += [label, label.upper()]

                for key in candidates:
                    if key in dd_global:
                        per_field_dd = list(dd_global.get(key) or [])
                        break

        normalized_fm.append({
            "tag": base_tag,
            "label": label,
            "dd_options": per_field_dd,
        })

    fm = normalized_fm

    # ======================================================
    # 2) AIga prompt yuborish (faqat JSON)
    # ======================================================
    payload = {
        "timezone": tz,
        "text": text or "",
        "dropdowns_global": dd_global,
        "fields_meta": fm,
        "need": {
            "semantic_keys": ["SANA", "HARAJAT", "TOLOV_TURI",
                               "VALYUTA", "KURS", "SUMMA", "IZOH", "ISM"],
            "label_values": True
        }
    }

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_GUIDE},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    out = await _create_json_completion_safe(OPENAI_MODEL, messages)

    # ======================================================
    # 3) Semantik maydonlarni normallashtirish
    # ======================================================

    for k in ["SANA", "HARAJAT", "TOLOV_TURI",
              "VALYUTA", "KURS", "SUMMA", "IZOH", "ISM"]:
        out[k] = "" if out.get(k) is None else str(out.get(k))

    # Sana (#DT)
    out["SANA"] = _coerce_by_tag(
        "#DT", out["SANA"], [],
        fallback_text=text,
        google_email=google_email
    ) or out.get("SANA", "")

    # To'lov turi — bo‘sh bo‘lsa avtomatik aniqlaymiz
    if not out.get("TOLOV_TURI"):
        pay = _detect_payment_type(f"{text} {out.get('IZOH','')}")
        if pay:
            out["TOLOV_TURI"] = pay

    # Valyuta normalize
    if out.get("VALYUTA"):
        out["VALYUTA"] = _normalize_currency(out["VALYUTA"])
    else:
        guess = _normalize_currency(f"{text} {out.get('IZOH','')}")
        if guess in {"USD", "EUR", "RUB", "UZS"}:
            out["VALYUTA"] = guess

    # ======================================================
    # 4) LABEL_VALUES → (#DT, #DD, #NB, #TX, #GA-A)
    # ======================================================
    lv_raw = dict(out.get("LABEL_VALUES") or {})
    lv_norm: Dict[str, str] = {}

    for m in fm:
        lbl = m["label"]
        lv_norm[lbl] = _coerce_by_tag(
            m["tag"],
            lv_raw.get(lbl, ""),
            m["dd_options"],
            fallback_text=text,
            google_email=google_email
        )

    out["LABEL_VALUES"] = lv_norm

    # ======================================================
    # 5) DD → semantik ustuvor (Valyuta, To'lov turi, Harajat)
    # ======================================================
    dd_sem = _pull_semantics_from_dd(out["LABEL_VALUES"], fm)
    out.update(dd_sem)

    # ======================================================
    # 6) KURS / SUMMA UZS (FAQAT AYTILSA!)
    # ======================================================

    val = (out.get("VALYUTA") or "").upper().strip()
    summa = parse_amount(out.get("SUMMA", ""))

    # Matndan kurs olish
    def _extract_course_from_text(t: str) -> Optional[int]:
        t = _to_latin(t.lower())

        # "kurs 12800"
        m = re.search(r"kurs\s*([0-9\., ]+)", t)
        if m:
            return parse_amount(m.group(1))

        # "dollar 12800"
        m = re.search(r"(dollar|evro|rubl)\s*([0-9\., ]+)", t)
        if m:
            return parse_amount(m.group(2))

        return None

    kurs_ai = parse_amount(out.get("KURS", ""))
    kurs = kurs_ai if kurs_ai is not None else _extract_course_from_text(f"{text} {out.get('IZOH','')}")

    # Kurs aytilmagan → bo‘sh
    if kurs is None:
        out["KURS"] = ""
    else:
        out["KURS"] = str(kurs)

    # SUMMA UZS hisoblash
    out.pop("SUMMA UZS", None)

    if val == "UZS":
        out["SUMMA UZS"] = str(summa) if isinstance(summa, int) else ""

    elif val in {"USD", "EUR", "RUB"}:
        if isinstance(summa, int) and isinstance(kurs, int):
            out["SUMMA UZS"] = str(summa * kurs)
        else:
            out["SUMMA UZS"] = ""

    else:
        out["SUMMA UZS"] = ""

    # ======================================================
    # 7) KURS / SUMMA UZS -> LABEL_VALUES qayta sinxronlash
    # ======================================================
    lv2 = dict(out["LABEL_VALUES"])

    def find_label(*cands):
        cset = [c.casefold() for c in cands]
        for m in fm:
            if any(c in m["label"].casefold() for c in cset):
                return m["label"]
        return None

    lbl_kurs = find_label("kurs", "rate")
    if lbl_kurs:
        lv2[lbl_kurs] = out.get("KURS", "")

    lbl_sumuz = find_label("summa uzs", "uzs", "som", "so'm")
    if lbl_sumuz:
        lv2[lbl_sumuz] = out.get("SUMMA UZS", "")

    # Oxirgi tag qoidasiga moslab beramiz
    lv_final: Dict[str, str] = {}
    for m in fm:
        lbl = m["label"]
        lv_final[lbl] = _coerce_by_tag(
            m["tag"],
            lv2.get(lbl, ""),
            m["dd_options"],
            fallback_text=text,
            google_email=google_email
        )

    out["LABEL_VALUES"] = lv_final

    return out



