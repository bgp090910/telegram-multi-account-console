"""
Web-based Telegram Session generator.

Flow:
1. send_code  → creates TelegramClient, sends verification code, returns a token
2. verify_code → completes sign-in, returns encrypted Session String
3. Pending clients auto-expire after 5 minutes
"""

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    PhoneCodeExpiredError,
)
from telethon.sessions import StringSession

from app.security import encrypt_secret

log = logging.getLogger("session-web")

TG_API_ID = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")

# Pending auth sessions: token → PendingSession
_sessions: dict[str, "_PendingSession"] = {}
_cleanup_lock = asyncio.Lock()
SESSION_TTL = 300  # 5 minutes


@dataclass
class _PendingSession:
    client: TelegramClient
    phone: str
    phone_code_hash: str
    created_at: float


async def cleanup_expired() -> None:
    """移除過期的 pending session。"""
    now = time.time()
    expired_keys = [
        k for k, v in _sessions.items() if now - v.created_at > SESSION_TTL
    ]
    for k in expired_keys:
        ps = _sessions.pop(k, None)
        if ps:
            try:
                await ps.client.disconnect()
            except Exception:
                pass
    if expired_keys:
        log.info("已清除 %s 個過期 Session 生成請求", len(expired_keys))


async def send_code(phone: str) -> dict[str, Any]:
    """
    Step 1: 建立 TelegramClient，發送驗證碼。
    回傳 {"token": "...", "phone": "..."} 或拋出例外。
    """
    if not TG_API_ID or not TG_API_HASH or TG_API_HASH == "PLACEHOLDER_NEEDS_REAL_VALUE":
        raise RuntimeError("TG_API_ID / TG_API_HASH 尚未設定為真實值")

    phone = phone.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    await cleanup_expired()

    client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)
    await client.connect()

    try:
        result = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await client.disconnect()
        raise RuntimeError("手機號碼格式不正確")
    except Exception as exc:
        await client.disconnect()
        raise RuntimeError(f"發送驗證碼失敗：{exc}")

    token = secrets.token_urlsafe(16)
    _sessions[token] = _PendingSession(
        client=client,
        phone=phone,
        phone_code_hash=result.phone_code_hash,
        created_at=time.time(),
    )
    return {"token": token, "phone": phone}


async def verify_code(
    token: str, code: str, password: str = ""
) -> dict[str, Any]:
    """
    Step 2: 用驗證碼完成登入，回傳加密的 Session String。
    """
    ps = _sessions.pop(token, None)
    if not ps:
        raise RuntimeError("請求已過期或不存在，請重新發送驗證碼")

    if time.time() - ps.created_at > SESSION_TTL:
        try:
            await ps.client.disconnect()
        except Exception:
            pass
        raise RuntimeError("請求已過期（超過 5 分鐘），請重新發送驗證碼")

    # 清理 code 格式（去掉空格、橫線）
    code = code.replace(" ", "").replace("-", "")

    try:
        try:
            await ps.client.sign_in(
                phone=ps.phone,
                code=code,
                phone_code_hash=ps.phone_code_hash,
            )
        except PhoneCodeInvalidError:
            # 把 session 放回去讓用戶重試
            _sessions[token] = ps
            raise RuntimeError("驗證碼不正確")
        except PhoneCodeExpiredError:
            try:
                await ps.client.disconnect()
            except Exception:
                pass
            raise RuntimeError("驗證碼已過期，請重新發送")
        except SessionPasswordNeededError:
            if not password:
                # 把 session 放回去讓用戶輸入密碼
                _sessions[token] = ps
                raise RuntimeError("NEED_2FA")
            await ps.client.sign_in(password=password)

        # 登入成功，提取 Session String
        session_string = ps.client.session.save()
        encrypted = encrypt_secret(session_string)

        # 取得帳號資訊
        me = await ps.client.get_me()
        username = getattr(me, "username", "") or ""
        user_id = me.id if me else None

        # 斷開連線
        await ps.client.disconnect()

        return {
            "session_string": session_string,
            "encrypted_session": encrypted,
            "username": username,
            "user_id": user_id,
        }

    except RuntimeError:
        raise
    except Exception as exc:
        try:
            await ps.client.disconnect()
        except Exception:
            pass
        raise RuntimeError(f"登入失敗：{exc}")


def has_pending(token: str) -> bool:
    """檢查 token 是否仍有效。"""
    ps = _sessions.get(token)
    if not ps:
        return False
    if time.time() - ps.created_at > SESSION_TTL:
        return False
    return True
