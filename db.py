import sqlite3
import asyncio
from pathlib import Path
from typing import Optional
import aiosqlite
from config import DATABASE_URL

# ---------------------------
#  SQLITE CONNECTION
# ---------------------------
DB_PATH = DATABASE_URL.split("///")[-1] if "///" in DATABASE_URL else "./bot.db"
_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    """Senkron ishlar uchun yagona global connection"""
    global _conn
    if _conn is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


# ---------------------------
#  INIT DB
# ---------------------------
async def init_db():
    """Barcha jadvalar mavjudligini tekshiradi va yaratadi."""
    def _init():
        c = _get_conn().cursor()

        # Users
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              tg_id INTEGER UNIQUE,
              full_name TEXT NOT NULL,
              phone TEXT NOT NULL,
              language TEXT NOT NULL DEFAULT 'uz',
              created_at TEXT DEFAULT (datetime('now'))
            );
        """)

        # Tokens — EMAIL qo‘shilgan
        c.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expiry_ts REAL,
                email TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)

        # Email ustun qo‘shish (agar yo‘q bo‘lsa)
        try:
            c.execute("ALTER TABLE tokens ADD COLUMN email TEXT;")
        except:
            pass

        # Last file
        c.execute("""
            CREATE TABLE IF NOT EXISTS prefs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER UNIQUE,
              last_file_id TEXT,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)

        # File → Sheet mapping
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_file_prefs (
              user_id INTEGER NOT NULL,
              file_id TEXT NOT NULL,
              sheet_title TEXT NOT NULL,
              PRIMARY KEY (user_id, file_id),
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)

        _get_conn().commit()

    await asyncio.to_thread(_init)


# ---------------------------
#  USER HELPERS
# ---------------------------
async def get_user_by_tg(tg_id: int) -> Optional[dict]:
    def _q():
        c = _get_conn().cursor()
        c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        r = c.fetchone()
        return dict(r) if r else None
    return await asyncio.to_thread(_q)


async def get_user_id_by_tg(tg_id: int) -> Optional[int]:
    row = await get_user_by_tg(tg_id)
    return int(row["id"]) if row else None


async def create_user(tg_id: int, full_name: str, phone: str, language: str) -> int:
    def _ins():
        conn = _get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO users(tg_id, full_name, phone, language) VALUES(?,?,?,?)",
            (tg_id, full_name, phone, language),
        )
        conn.commit()
        return c.lastrowid
    return await asyncio.to_thread(_ins)


# ---------------------------
#  GOOGLE TOKEN HELPERS (EMAIL bilan)
# ---------------------------
async def upsert_token_by_tg(
    tg_id: int,
    access_token: str,
    refresh_token: Optional[str],
    expiry_ts: Optional[float],
    email: Optional[str] = None
):
    def _op():
        conn = _get_conn()
        c = conn.cursor()

        # user_id ni olish
        c.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        u = c.fetchone()
        if not u:
            return
        user_id = u["id"]

        # tokens ichida bor-yo‘qligini tekshirish
        c.execute("SELECT id FROM tokens WHERE user_id=?", (user_id,))
        t = c.fetchone()

        if t:
            c.execute("""
                UPDATE tokens SET
                    access_token=?,
                    refresh_token=COALESCE(?, refresh_token),
                    expiry_ts=?,
                    email=COALESCE(?, email)
                WHERE user_id=?
            """, (access_token, refresh_token, expiry_ts, email, user_id))
        else:
            c.execute("""
                INSERT INTO tokens(user_id, access_token, refresh_token, expiry_ts, email)
                VALUES (?,?,?,?,?)
            """, (user_id, access_token, refresh_token, expiry_ts, email))

        conn.commit()

    await asyncio.to_thread(_op)



async def get_token_by_tg(tg_id: int) -> Optional[dict]:
    """email ham qaytaradi"""
    def _q():
        c = _get_conn().cursor()
        c.execute("""
            SELECT t.access_token, t.refresh_token, t.expiry_ts, t.email
            FROM tokens t
            JOIN users u ON u.id=t.user_id
            WHERE u.tg_id=?
        """, (tg_id,))
        r = c.fetchone()
        return dict(r) if r else None
    return await asyncio.to_thread(_q)


async def get_google_email_by_tg(tg_id: int) -> str:
    """
    Telegram foydalanuvchisi uchun emailni qaytaradi.
    """
    def _q():
        c = _get_conn().cursor()
        c.execute("""
            SELECT t.email
            FROM tokens t
            JOIN users u ON u.id = t.user_id
            WHERE u.tg_id = ?
        """, (tg_id,))
        r = c.fetchone()
        return r["email"] if r and r["email"] else ""

    return await asyncio.to_thread(_q)



async def delete_token_by_tg(tg_id: int) -> None:
    """Token va emailni o‘chiradi"""
    def _op():
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        user = c.fetchone()
        if not user:
            return
        c.execute("DELETE FROM tokens WHERE user_id=?", (user["id"],))
        conn.commit()
    await asyncio.to_thread(_op)


# ---------------------------
#  LAST FILE HELPERS
# ---------------------------
async def set_last_file_id_by_tg(tg_id: int, file_id: str):
    def _op():
        conn = _get_conn()
        c = conn.cursor()

        c.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        user = c.fetchone()
        if not user:
            return
        user_id = user["id"]

        c.execute("SELECT id FROM prefs WHERE user_id=?", (user_id,))
        pref = c.fetchone()

        if pref:
            c.execute("UPDATE prefs SET last_file_id=? WHERE user_id=?", (file_id, user_id))
        else:
            c.execute("INSERT INTO prefs(user_id, last_file_id) VALUES(?,?)", (user_id, file_id))

        conn.commit()

    await asyncio.to_thread(_op)


async def get_last_file_id_by_tg(tg_id: int) -> Optional[str]:
    def _q():
        c = _get_conn().cursor()
        c.execute("""
            SELECT last_file_id
            FROM prefs p
            JOIN users u ON u.id=p.user_id
            WHERE u.tg_id=?
        """, (tg_id,))
        r = c.fetchone()
        return r["last_file_id"] if r else None
    return await asyncio.to_thread(_q)


# ---------------------------
#  FAVORITE FILES (o'zgarishsiz)
# ---------------------------
async def _ensure_favorites_table():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorite_files (
                user_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, file_id)
            )
        """)
        await db.commit()


async def add_favorite_file(user_id: int, file_id: str, name: str):
    await _ensure_favorites_table()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO favorite_files (user_id, file_id, name)
            VALUES (?, ?, ?)
        """, (user_id, file_id, name))
        await db.commit()


async def remove_favorite_file(user_id: int, file_id: str):
    await _ensure_favorites_table()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM favorite_files WHERE user_id=? AND file_id=?
        """, (user_id, file_id))
        await db.commit()


async def list_favorite_files(user_id: int):
    await _ensure_favorites_table()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT file_id, name, created_at
            FROM favorite_files
            WHERE user_id=?
            ORDER BY created_at DESC
        """, (user_id,))
        rows = await cur.fetchall()
    return [{"file_id": r[0], "name": r[1], "created_at": r[2]} for r in rows]


# ---------------------------
#  LANGUAGE SETTER
# ---------------------------
async def set_user_language_by_tg(tg_id: int, language: str):
    def _op():
        conn = _get_conn()
        c = conn.cursor()
        c.execute(
            "UPDATE users SET language=? WHERE tg_id=?",
            (language, tg_id),
        )
        conn.commit()
    await asyncio.to_thread(_op)

# ---------------------------
#  SHEET TITLE HELPERS (per file)
# ---------------------------

async def get_last_sheet_title_for_file_by_tg(tg_id: int, file_id: str) -> Optional[str]:
    """
    Foydalanuvchi + fayl bo‘yicha oxirgi ishlagan sheet nomini qaytaradi.
    """
    def _q():
        c = _get_conn().cursor()
        c.execute("""
            SELECT sheet_title
            FROM user_file_prefs
            JOIN users ON users.id = user_file_prefs.user_id
            WHERE users.tg_id=? AND user_file_prefs.file_id=?
            LIMIT 1
        """, (tg_id, file_id))
        r = c.fetchone()
        return r["sheet_title"] if r else None

    return await asyncio.to_thread(_q)


async def set_last_sheet_title_for_file_by_tg(tg_id: int, file_id: str, sheet_title: str):
    """
    Foydalanuvchi + fayl bo‘yicha oxirgi tanlangan sheetni saqlaydi.
    """
    def _op():
        conn = _get_conn()
        c = conn.cursor()

        c.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        u = c.fetchone()
        if not u:
            return
        user_id = u["id"]

        c.execute("""
            INSERT INTO user_file_prefs (user_id, file_id, sheet_title)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, file_id)
            DO UPDATE SET sheet_title=excluded.sheet_title
        """, (user_id, file_id, sheet_title))

        conn.commit()

    await asyncio.to_thread(_op)

async def set_last_sheet_title_by_tg(tg_id: int, sheet_title: str):
    """
    Foydalanuvchining oxirgi tanlangan fayliga sheet_title o‘rnatadi.
    Bu funksiya sheet_wizard.py tomonidan ishlatiladi.
    """
    file_id = await get_last_file_id_by_tg(tg_id)
    if not file_id:
        return None

    return await set_last_sheet_title_for_file_by_tg(
        tg_id=tg_id,
        file_id=file_id,
        sheet_title=sheet_title
    )

