"""
Web-based Telegram Session generator.

Flow:
1. send_code  → creates TelegramClient, sends verification code, returns a token
2. verify_code → completes sign-in, returns encrypted Session String
3. resend_code → disconnects old client, creates fresh one, resends code
4. Pending clients auto-expire after 5 minutes
"""

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
    PhoneCodeEmptyError,
    FloodWaitError,
)
from telethon.sessions import StringSession

from app.security import encrypt_secret

log = logging.getLogger("session-web")

TG_API_ID = int(os.environ.get("TG_API_ID", "0"))
TG_API_HASH = os.environ.get("TG_API_HASH", "")

# Pending auth sessions: token → PendingSession
_sessions: dict[str, "_PendingSession"] = {}
SESSION_TTL = 300  # 5 minutes


@dataclass
class _PendingSession:
    client: TelegramClient
    phone: str
    phone_code_hash: str
    created_at: float


async def cleanup_expired() -> None:
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


def _is_api_configured() -> bool:
    return bool(
        TG_API_ID
        and TG_API_HASH
        and TG_API_HASH != "PLACEHOLDER_NEEDS_REAL_VALUE"
    )


async def _new_client_and_send(phone: str) -> tuple[TelegramClient, str]:
    """建立全新 TelegramClient 並發送驗證碼，回傳 (client, phone_code_hash)。"""
    client = TelegramClient(StringSession(), TG_API_ID, TG_API_HASH)
    await client.connect()
    try:
        result = await client.send_code_request(phone)
        log.info("驗證碼已發送 phone=%s hash=%s", phone, result.phone_code_hash[:8] + "...")
        return client, result.phone_code_hash
    except PhoneNumberInvalidError:
        await client.disconnect()
        raise RuntimeError("手機號碼格式不正確")
    except FloodWaitError as exc:
        await client.disconnect()
        raise RuntimeError(f"發送過於頻繁，請 {exc.seconds} 秒後再試")
    except Exception as exc:
        await client.disconnect()
        raise RuntimeError(f"發送驗證碼失敗：{exc}")


async def send_code(phone: str) -> dict[str, Any]:
    """Step 1: 發送驗證碼。"""
    if not _is_api_configured():
        raise RuntimeError("TG_API_ID / TG_API_HASH 尚未設定為真實值")

    phone = phone.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    await cleanup_expired()

    client, phone_code_hash = await _new_client_and_send(phone)

    token = secrets.token_urlsafe(16)
    _sessions[token] = _PendingSession(
        client=client,
        phone=phone,
        phone_code_hash=phone_code_hash,
        created_at=time.time(),
    )
    log.info("Pending session created token=%s phone=%s", token, phone)
    return {"token": token, "phone": phone}


async def resend_code(token: str) -> dict[str, Any]:
    """
    重新發送驗證碼。
    關鍵：斷開舊 client，建立全新 client 重新發送。
    避免舊 client 狀態殘留導致 sign_in 時 phone_code_hash 不匹配。
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

    phone = ps.phone

    # 斷開舊的連線
    try:
        await ps.client.disconnect()
    except Exception:
        pass

    # 建立全新 client 並發送
    new_client, new_hash = await _new_client_and_send(phone)

    # 用同一個 token 存新的 session
    _sessions[token] = _PendingSession(
        client=new_client,
        phone=phone,
        phone_code_hash=new_hash,
        created_at=time.time(),  # 重設計時器
    )
    log.info("Resend done token=%s phone=%s new_hash=%s", token, phone, new_hash[:8] + "...")
    return {"token": token, "phone": phone}


async def verify_code(
    token: str, code: str, password: str = ""
) -> dict[str, Any]:
    """Step 2: 用驗證碼完成登入。"""
    ps = _sessions.pop(token, None)
    if not ps:
        raise RuntimeError("請求已過期或不存在，請重新發送驗證碼")

    if time.time() - ps.created_at > SESSION_TTL:
        try:
            await ps.client.disconnect()
        except Exception:
            pass
        raise RuntimeError("請求已過期（超過 5 分鐘），請重新發送驗證碼")

    code = code.replace(" ", "").replace("-", "")

    log.info(
        "verify_code: token=%s code=%s hash=%s client_connected=%s",
        token, code, ps.phone_code_hash[:8] + "...",
        await ps.client.is_connected(),
    )

    try:
        # 確保 client 仍然連線
        if not await ps.client.is_connected():
            await ps.client.connect()

        try:
            await ps.client.sign_in(
                phone=ps.phone,
                code=code,
                phone_code_hash=ps.phone_code_hash,
            )
        except (PhoneCodeInvalidError, PhoneCodeEmptyError):
            _sessions[token] = ps
            raise RuntimeError("驗證碼不正確，請重新輸入。如多次失敗請點「重新發送驗證碼」")
        except PhoneCodeExpiredError:
            _sessions[token] = ps
            raise RuntimeError("EXPIRED")
        except SessionPasswordNeededError:
            if not password:
                _sessions[token] = ps
                raise RuntimeError("NEED_2FA")
            await ps.client.sign_in(password=password)
        except Exception as sign_exc:
            error_msg = str(sign_exc)
            log.warning("sign_in failed: %s (type=%s)", error_msg, type(sign_exc).__name__)
            # ResendCodeRequest 或 PHONE_CODE_EMPTY → 自動重新發送一次
            if "ResendCode" in error_msg or "PHONE_CODE_EMPTY" in error_msg:
                log.info("Auto-resending code after sign_in failure...")
                try:
                    await ps.client.disconnect()
                except Exception:
                    pass
                try:
                    new_client, new_hash = await _new_client_and_send(ps.phone)
                    _sessions[token] = _PendingSession(
                        client=new_client,
                        phone=ps.phone,
                        phone_code_hash=new_hash,
                        created_at=time.time(),
                    )
                except Exception:
                    pass
                raise RuntimeError("EXPIRED")
            # 其他錯誤，放回去讓用戶重試
            _sessions[token] = ps
            raise RuntimeError(f"登入失敗：{error_msg}")

        # 登入成功
        session_string = ps.client.session.save()
        encrypted = encrypt_secret(session_string)

        me = await ps.client.get_me()
        username = getattr(me, "username", "") or ""
        user_id = me.id if me else None

        await ps.client.disconnect()
        log.info("Session generated: @%s (%s)", username, user_id)

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
    ps = _sessions.get(token)
    if not ps:
        return False
    return time.time() - ps.created_at <= SESSION_TTL
