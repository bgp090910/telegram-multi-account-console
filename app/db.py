import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.security import decrypt_secret, encrypt_secret

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "console.sqlite3"
MEMORY_TTL_HOURS = int(os.getenv("MEMORY_TTL_HOURS", "24"))

DEFAULT_PERSONA = """你以群內一般成員角色參與互動，不使用助理、客服、管理員或官方代表的口吻。
使用台灣繁體中文，語氣自然、口語、生活化。可以主動接話、分享一般生活感受、詢問近況或開啟輕鬆話題。
可以談論交友、約會與感情相處，也可以自然回覆他人分享；沒有真實經歷時，只能分享想法、期待、顧慮或假設情境，不得捏造真實見證。
成人或私密話題必須建立在所有參與者皆已成年、明確自願、互相尊重及重視隱私安全的前提。對方拒絕、不舒服、沉默或要求停止時，立即停止。
不主動索取私密影像、地址、電話、身分資料或其他敏感資訊。若被直接詢問是否為自動化帳號，應如實說明。"""


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _ensure_columns(db: sqlite3.Connection) -> None:
    """Schema 遷移：檢查並補齊缺少的欄位，確保舊資料庫升級不崩潰。"""
    cols = {row[1] for row in db.execute("PRAGMA table_info(accounts)").fetchall()}
    migrations: list[tuple[str, str]] = [
        ("user_blacklist", "TEXT NOT NULL DEFAULT ''"),
        ("reply_cooldown", "REAL NOT NULL DEFAULT 8.0"),
        ("messages_sent", "INTEGER NOT NULL DEFAULT 0"),
        ("last_active_at", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col_name, col_def in migrations:
        if col_name not in cols:
            db.execute(f"ALTER TABLE accounts ADD COLUMN {col_name} {col_def}")


def init_db() -> None:
    with _connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                session_cipher TEXT NOT NULL,
                role_type TEXT NOT NULL DEFAULT '男性老成員',
                persona TEXT NOT NULL,
                ai_base_url TEXT NOT NULL,
                ai_api_key_cipher TEXT NOT NULL,
                ai_model TEXT NOT NULL,
                temperature REAL NOT NULL DEFAULT 0.8,
                max_tokens INTEGER NOT NULL DEFAULT 500,
                group_ids TEXT NOT NULL DEFAULT '',
                reply_mode TEXT NOT NULL DEFAULT 'all',
                reply_probability REAL NOT NULL DEFAULT 1.0,
                min_delay REAL NOT NULL DEFAULT 1.0,
                max_delay REAL NOT NULL DEFAULT 3.0,
                reply_cooldown REAL NOT NULL DEFAULT 8.0,
                user_blacklist TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'stopped',
                last_error TEXT NOT NULL DEFAULT '',
                telegram_user_id INTEGER,
                telegram_username TEXT NOT NULL DEFAULT '',
                messages_sent INTEGER NOT NULL DEFAULT 0,
                last_active_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_messages_scope
                ON messages(account_id, chat_id, user_id, created_at);
            """
        )
        _ensure_columns(db)


def _public_account(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item.pop("session_cipher", None)
    item.pop("ai_api_key_cipher", None)
    item["enabled"] = bool(item["enabled"])
    item["has_session"] = True
    item["has_ai_key"] = True
    return item


def list_accounts() -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    return [_public_account(row) for row in rows]


def get_account(account_id: int, include_secrets: bool = False) -> dict[str, Any] | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    if include_secrets:
        item["session_string"] = decrypt_secret(item.pop("session_cipher"))
        item["ai_api_key"] = decrypt_secret(item.pop("ai_api_key_cipher"))
    else:
        item.pop("session_cipher", None)
        item.pop("ai_api_key_cipher", None)
    return item


def create_account(data: dict[str, Any]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        cursor = db.execute(
            """
            INSERT INTO accounts (
                name, session_cipher, role_type, persona, ai_base_url,
                ai_api_key_cipher, ai_model, temperature, max_tokens,
                group_ids, reply_mode, reply_probability, min_delay,
                max_delay, reply_cooldown, user_blacklist, enabled,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["name"], encrypt_secret(data["session_string"]),
                data.get("role_type", "男性老成員"), data.get("persona") or DEFAULT_PERSONA,
                data["ai_base_url"].rstrip("/"), encrypt_secret(data["ai_api_key"]),
                data["ai_model"], float(data.get("temperature", 0.8)),
                int(data.get("max_tokens", 500)), data.get("group_ids", ""),
                data.get("reply_mode", "all"), float(data.get("reply_probability", 1.0)),
                float(data.get("min_delay", 1.0)), float(data.get("max_delay", 3.0)),
                float(data.get("reply_cooldown", 8.0)),
                data.get("user_blacklist", ""),
                1 if data.get("enabled", True) else 0, now, now,
            ),
        )
        return int(cursor.lastrowid)


def update_account(account_id: int, data: dict[str, Any]) -> None:
    current = get_account(account_id, include_secrets=True)
    if not current:
        raise KeyError(account_id)
    session_string = data.get("session_string", "").strip() or current["session_string"]
    ai_api_key = data.get("ai_api_key", "").strip() or current["ai_api_key"]
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        db.execute(
            """
            UPDATE accounts SET
                name=?, session_cipher=?, role_type=?, persona=?, ai_base_url=?,
                ai_api_key_cipher=?, ai_model=?, temperature=?, max_tokens=?,
                group_ids=?, reply_mode=?, reply_probability=?, min_delay=?,
                max_delay=?, reply_cooldown=?, user_blacklist=?, enabled=?, updated_at=?
            WHERE id=?
            """,
            (
                data["name"], encrypt_secret(session_string), data["role_type"],
                data["persona"], data["ai_base_url"].rstrip("/"),
                encrypt_secret(ai_api_key), data["ai_model"], float(data["temperature"]),
                int(data["max_tokens"]), data.get("group_ids", ""),
                data["reply_mode"], float(data["reply_probability"]),
                float(data["min_delay"]), float(data["max_delay"]),
                float(data.get("reply_cooldown", 8.0)),
                data.get("user_blacklist", ""),
                1 if data.get("enabled") else 0, now, account_id,
            ),
        )


def delete_account(account_id: int) -> None:
    with _connect() as db:
        db.execute("DELETE FROM messages WHERE account_id=?", (account_id,))
        db.execute("DELETE FROM accounts WHERE id=?", (account_id,))


def set_status(
    account_id: int,
    status: str,
    error: str = "",
    telegram_user_id: int | None = None,
    telegram_username: str | None = None,
) -> None:
    with _connect() as db:
        db.execute(
            """
            UPDATE accounts SET status=?, last_error=?,
                telegram_user_id=COALESCE(?, telegram_user_id),
                telegram_username=COALESCE(?, telegram_username), updated_at=?
            WHERE id=?
            """,
            (
                status, error[:2000], telegram_user_id, telegram_username,
                datetime.now(timezone.utc).isoformat(), account_id,
            ),
        )


def set_last_error(account_id: int, error: str) -> None:
    with _connect() as db:
        db.execute(
            "UPDATE accounts SET last_error=?, updated_at=? WHERE id=?",
            (error[:2000], datetime.now(timezone.utc).isoformat(), account_id),
        )


def increment_messages_sent(account_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        db.execute(
            "UPDATE accounts SET messages_sent=messages_sent+1, last_active_at=?, updated_at=? WHERE id=?",
            (now, now, account_id),
        )


def save_message(
    account_id: int, chat_id: int, user_id: int, role: str, content: str
) -> None:
    with _connect() as db:
        db.execute(
            """
            INSERT INTO messages(account_id, chat_id, user_id, role, content, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, chat_id, user_id, role, content,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    if role == "assistant":
        increment_messages_sent(account_id)


def get_history(
    account_id: int, chat_id: int, user_id: int, limit: int = 16
) -> list[dict[str, str]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MEMORY_TTL_HOURS)).isoformat()
    with _connect() as db:
        rows = db.execute(
            """
            SELECT role, content FROM messages
            WHERE account_id=? AND chat_id=? AND user_id=? AND created_at>=?
            ORDER BY id DESC LIMIT ?
            """,
            (account_id, chat_id, user_id, cutoff, limit),
        ).fetchall()
    rows.reverse()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def get_recent_messages(
    account_id: int, chat_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """取得帳號最近訊息（供日誌查看器使用）。"""
    with _connect() as db:
        if chat_id is not None:
            rows = db.execute(
                """
                SELECT id, chat_id, user_id, role, content, created_at
                FROM messages WHERE account_id=? AND chat_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (account_id, chat_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT id, chat_id, user_id, role, content, created_at
                FROM messages WHERE account_id=?
                ORDER BY id DESC LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
    rows = list(reversed(rows))
    return [dict(row) for row in rows]


def cleanup_memory() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MEMORY_TTL_HOURS)).isoformat()
    with _connect() as db:
        cursor = db.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        return cursor.rowcount


def account_counts() -> dict[str, int]:
    with _connect() as db:
        total = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        online = db.execute("SELECT COUNT(*) FROM accounts WHERE status='online'").fetchone()[0]
    return {"total": int(total), "online": int(online)}
