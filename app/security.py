import hmac
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
APP_SECRET = os.getenv("APP_SECRET", "")
MASTER_KEY = os.getenv("MASTER_KEY", "")
COOKIE_NAME = "tg_console_session"
COOKIE_MAX_AGE = int(os.getenv("COOKIE_MAX_AGE", "43200"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"

if not ADMIN_PASSWORD:
    raise RuntimeError("缺少 ADMIN_PASSWORD")
if len(APP_SECRET) < 32:
    raise RuntimeError("APP_SECRET 至少需要 32 個字元")
if not MASTER_KEY:
    raise RuntimeError("缺少 MASTER_KEY")

try:
    _fernet = Fernet(MASTER_KEY.encode())
except ValueError as exc:
    raise RuntimeError("MASTER_KEY 必須是 Fernet 金鑰") from exc

_serializer = URLSafeTimedSerializer(APP_SECRET, salt="telegram-console")


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet.decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("無法解密敏感資料，MASTER_KEY 可能已變更") from exc


def verify_login(username: str, password: str) -> bool:
    return hmac.compare_digest(username, ADMIN_USERNAME) and hmac.compare_digest(
        password, ADMIN_PASSWORD
    )


def make_login_cookie() -> str:
    return _serializer.dumps({"username": ADMIN_USERNAME})


def read_login_cookie(cookie: str | None) -> dict[str, Any] | None:
    if not cookie:
        return None
    try:
        payload = _serializer.loads(cookie, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if payload.get("username") != ADMIN_USERNAME:
        return None
    return payload


def require_admin(request: Request) -> dict[str, Any]:
    payload = read_login_cookie(request.cookies.get(COOKIE_NAME))
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return payload
