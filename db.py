import sqlite3
import asyncio
from pathlib import Path
from typing import Optional
from config import DATABASE_URL

DB_PATH = DATABASE_URL.split("///")[-1] if "///" in DATABASE_URL else "./bot.db"
_conn: Optional[sqlite3.Connection] = None

def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn

# --- init ---
# --- init ---
async def init_db():
    def _init():
        c = _get_conn().cursor()
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

        c.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER UNIQUE,
              access_token TEXT NOT NULL,
              refresh_token TEXT,
              expiry_ts REAL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS prefs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER UNIQUE,
              last_file_id TEXT,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)

        # 🔥 YANGI: varaq nomini faylga bog‘lab saqlash
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_file_prefs (
              user_id    INTEGER NOT NULL,
              file_id    TEXT    NOT NULL,
              sheet_title TEXT   NOT NULL,
              PRIMARY KEY (user_id, file_id),
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
        """)

        _get_conn().commit()
    await asyncio.to_thread(_init)


# --- CRUD helpers ---
async def get_user_by_tg(tg_id: int) -> Optional[dict]:
    def _q():
        c = _get_conn().cursor()
        c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        r = c.fetchone()
        return dict(r) if r else None
    return await asyncio.to_thread(_q)

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

async def get_user_id_by_tg(tg_id: int) -> Optional[int]:
    row = await get_user_by_tg(tg_id)
    return int(row["id"]) if row else None

async def upsert_token_by_tg(tg_id: int, access_token: str,
                             refresh_token: Optional[str],
                             expiry_ts: Optional[float]):
    def _op():
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        u = c.fetchone()
        if not u:
            return
        user_id = u["id"]
        c.execute("SELECT id FROM tokens WHERE user_id=?", (user_id,))
        t = c.fetchone()
        if t:
            c.execute(
                "UPDATE tokens SET access_token=?, "
                "refresh_token=COALESCE(?, refresh_token), "
                "expiry_ts=? WHERE user_id=?",
                (access_token, refresh_token, expiry_ts, user_id),
            )
        else:
            c.execute(
                "INSERT INTO tokens(user_id, access_token, refresh_token, expiry_ts) "
                "VALUES(?,?,?,?)",
                (user_id, access_token, refresh_token, expiry_ts),
            )
        conn.commit()
    await asyncio.to_thread(_op)

async def get_token_by_tg(tg_id: int) -> Optional[dict]:
    def _q():
        c = _get_conn().cursor()
        c.execute(
            """
            SELECT t.access_token, t.refresh_token, t.expiry_ts
            FROM tokens t JOIN users u ON u.id=t.user_id
            WHERE u.tg_id=?
            """,
            (tg_id,),
        )
        r = c.fetchone()
        return dict(r) if r else None
    return await asyncio.to_thread(_q)

async def set_last_file_id_by_tg(tg_id: int, file_id: str):
    def _op():
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        u = c.fetchone()
        if not u: return
        user_id = u["id"]
        c.execute("SELECT id FROM prefs WHERE user_id=?", (user_id,))
        p = c.fetchone()
        if p:
            c.execute("UPDATE prefs SET last_file_id=? WHERE user_id=?", (file_id, user_id))
        else:
            c.execute("INSERT INTO prefs(user_id, last_file_id) VALUES(?,?)", (user_id, file_id))
        conn.commit()
    await asyncio.to_thread(_op)

async def get_last_file_id_by_tg(tg_id: int) -> Optional[str]:
    def _q():
        conn = _get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT p.last_file_id
            FROM prefs p JOIN users u ON u.id=p.user_id
            WHERE u.tg_id=?
        """, (tg_id,))
        r = c.fetchone()
        return r["last_file_id"] if r else None
    return await asyncio.to_thread(_q)


async def set_last_sheet_title_by_tg(tg_id: int, sheet_title: str):
    file_id = await get_last_file_id_by_tg(tg_id)
    if not file_id:
        return
    await set_last_sheet_title_for_file_by_tg(tg_id, file_id, sheet_title)

async def get_last_sheet_title_by_tg(tg_id: int) -> Optional[str]:
    file_id = await get_last_file_id_by_tg(tg_id)
    if not file_id:
        return None
    return await get_last_sheet_title_for_file_by_tg(tg_id, file_id)


# db.py

# Varaqni saqlash (faylga bog'langan)
# Varaqni saqlash (faylga bog'langan)
async def set_last_sheet_title_for_file_by_tg(tg_id: int, file_id: str, sheet_title: str) -> None:
    def _op():
        conn = _get_conn()
        c = conn.cursor()
        c.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        u = c.fetchone()
        if not u:
            return
        user_id = u["id"]
        # UPSERT (SQLite 3.24+)
        c.execute("""
            INSERT INTO user_file_prefs(user_id, file_id, sheet_title)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, file_id)
            DO UPDATE SET sheet_title=excluded.sheet_title
        """, (user_id, file_id, sheet_title))
        conn.commit()
    await asyncio.to_thread(_op)

# Varaqni o‘qish (faylga bog'langan)
async def get_last_sheet_title_for_file_by_tg(tg_id: int, file_id: str) -> Optional[str]:
    def _q():
        c = _get_conn().cursor()
        c.execute("""
            SELECT ufp.sheet_title
            FROM user_file_prefs ufp
            JOIN users u ON u.id = ufp.user_id
            WHERE u.tg_id=? AND ufp.file_id=?
            LIMIT 1
        """, (tg_id, file_id))
        r = c.fetchone()
        return r["sheet_title"] if r else None
    return await asyncio.to_thread(_q)

