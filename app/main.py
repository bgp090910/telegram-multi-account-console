import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.manager import AccountManager
from app import session_web
from app.security import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    COOKIE_SECURE,
    make_login_cookie,
    read_login_cookie,
    require_admin,
    verify_login,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

manager = AccountManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    await manager.startup()
    yield
    await manager.shutdown()


app = FastAPI(title="Telegram 多帳號控制台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _form_data(
    name: str,
    session_string: str,
    role_type: str,
    persona: str,
    ai_base_url: str,
    ai_api_key: str,
    ai_model: str,
    temperature: float,
    max_tokens: int,
    group_ids: str,
    reply_mode: str,
    reply_probability: float,
    reply_cooldown: float,
    min_delay: float,
    max_delay: float,
    user_blacklist: str,
    enabled: str | None,
) -> dict:
    if reply_mode not in {"all", "mention", "probability"}:
        raise HTTPException(400, "reply_mode 不正確")
    if min_delay < 0 or max_delay < min_delay:
        raise HTTPException(400, "回覆延遲設定不正確")
    return {
        "name": name.strip(),
        "session_string": session_string.strip(),
        "role_type": role_type.strip(),
        "persona": persona.strip(),
        "ai_base_url": ai_base_url.strip(),
        "ai_api_key": ai_api_key.strip(),
        "ai_model": ai_model.strip(),
        "temperature": max(0.0, min(2.0, temperature)),
        "max_tokens": max(50, min(4000, max_tokens)),
        "group_ids": group_ids.strip(),
        "reply_mode": reply_mode,
        "reply_probability": max(0.0, min(1.0, reply_probability)),
        "reply_cooldown": max(0.0, min(300.0, reply_cooldown)),
        "min_delay": min_delay,
        "max_delay": max_delay,
        "user_blacklist": user_blacklist.strip(),
        "enabled": enabled == "on",
    }


# ── Health ─────────────────────────────────────────────────

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, **db.account_counts()})


# ── Auth ──────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if read_login_cookie(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not verify_login(username, password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "帳號或密碼錯誤"}, status_code=401
        )
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        make_login_cookie(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    return response


@app.post("/logout")
async def logout() -> RedirectResponse:
    response = _redirect_login()
    response.delete_cookie(COOKIE_NAME)
    return response


# ── Dashboard ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        return _redirect_login()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "accounts": db.list_accounts(),
            "default_persona": db.DEFAULT_PERSONA,
            "memory_ttl": db.MEMORY_TTL_HOURS,
        },
    )


# ── Account CRUD ──────────────────────────────────────────

@app.post("/accounts")
async def add_account(
    _: dict = Depends(require_admin),
    name: str = Form(...),
    session_string: str = Form(...),
    role_type: str = Form(...),
    persona: str = Form(...),
    ai_base_url: str = Form(...),
    ai_api_key: str = Form(...),
    ai_model: str = Form(...),
    temperature: float = Form(0.8),
    max_tokens: int = Form(500),
    group_ids: str = Form(""),
    reply_mode: str = Form("all"),
    reply_probability: float = Form(1.0),
    reply_cooldown: float = Form(8.0),
    min_delay: float = Form(1.0),
    max_delay: float = Form(3.0),
    user_blacklist: str = Form(""),
    enabled: str | None = Form(None),
):
    data = _form_data(
        name, session_string, role_type, persona, ai_base_url, ai_api_key,
        ai_model, temperature, max_tokens, group_ids, reply_mode,
        reply_probability, reply_cooldown, min_delay, max_delay,
        user_blacklist, enabled,
    )
    if not all([data["name"], data["session_string"], data["ai_base_url"],
                data["ai_api_key"], data["ai_model"]]):
        raise HTTPException(400, "新增帳號時 Session、API Key 與模型均為必填")
    account_id = db.create_account(data)
    if data["enabled"]:
        await manager.start_account(account_id)
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/{account_id}/update")
async def edit_account(
    account_id: int,
    _: dict = Depends(require_admin),
    name: str = Form(...),
    session_string: str = Form(""),
    role_type: str = Form(...),
    persona: str = Form(...),
    ai_base_url: str = Form(...),
    ai_api_key: str = Form(""),
    ai_model: str = Form(...),
    temperature: float = Form(0.8),
    max_tokens: int = Form(500),
    group_ids: str = Form(""),
    reply_mode: str = Form("all"),
    reply_probability: float = Form(1.0),
    reply_cooldown: float = Form(8.0),
    min_delay: float = Form(1.0),
    max_delay: float = Form(3.0),
    user_blacklist: str = Form(""),
    enabled: str | None = Form(None),
):
    data = _form_data(
        name, session_string, role_type, persona, ai_base_url, ai_api_key,
        ai_model, temperature, max_tokens, group_ids, reply_mode,
        reply_probability, reply_cooldown, min_delay, max_delay,
        user_blacklist, enabled,
    )
    before = db.get_account(account_id, include_secrets=True)
    if not before:
        raise HTTPException(404, "帳號不存在")

    # 判斷 session / AI key 是否變更（在 update 前讀取）
    session_changed = bool(data["session_string"] and data["session_string"] != before["session_string"])
    ai_key_changed = bool(data["ai_api_key"] and data["ai_api_key"] != before["ai_api_key"])

    db.update_account(account_id, data)

    # 若 AI 設定變更，清除快取讓下次使用新 client
    if ai_key_changed or data["ai_base_url"] != before["ai_base_url"]:
        manager.invalidate_ai_client(account_id)

    running = manager.is_running(account_id)
    if data["enabled"]:
        if session_changed and running:
            await manager.restart_account(account_id)
        elif not running:
            await manager.start_account(account_id)
    elif running:
        await manager.stop_account(account_id)
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/{account_id}/start")
async def start_account(account_id: int, _: dict = Depends(require_admin)):
    await manager.start_account(account_id)
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/{account_id}/stop")
async def stop_account(account_id: int, _: dict = Depends(require_admin)):
    await manager.stop_account(account_id)
    return RedirectResponse("/", status_code=303)


@app.post("/accounts/{account_id}/delete")
async def remove_account(account_id: int, _: dict = Depends(require_admin)):
    await manager.stop_account(account_id)
    db.delete_account(account_id)
    return RedirectResponse("/", status_code=303)


# ── Message Log Viewer ────────────────────────────────────

@app.get("/accounts/{account_id}/messages", response_class=HTMLResponse)
async def message_log_page(
    request: Request,
    account_id: int,
    chat_id: int | None = Query(None),
    limit: int = Query(100, ge=10, le=500),
):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        return _redirect_login()
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "帳號不存在")
    messages = db.get_recent_messages(account_id, chat_id=chat_id, limit=limit)
    return templates.TemplateResponse(
        request,
        "messages.html",
        {
            "account": account,
            "messages": messages,
            "chat_id": chat_id,
            "limit": limit,
        },
    )


# ── JSON API ──────────────────────────────────────────────

@app.get("/api/accounts")
async def api_list_accounts(request: Request):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(401, "未登入")
    accounts = db.list_accounts()
    # 加入 runtime 狀態
    for acc in accounts:
        acc["runtime_online"] = manager.is_running(acc["id"])
    return JSONResponse(accounts)


@app.get("/api/accounts/{account_id}/messages")
async def api_get_messages(
    request: Request,
    account_id: int,
    chat_id: int | None = Query(None),
    limit: int = Query(100, ge=10, le=500),
):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(401, "未登入")
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "帳號不存在")
    messages = db.get_recent_messages(account_id, chat_id=chat_id, limit=limit)
    return JSONResponse(messages)


@app.get("/api/accounts/{account_id}/stats")
async def api_account_stats(request: Request, account_id: int):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(401, "未登入")
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "帳號不存在")
    return JSONResponse({
        "id": account["id"],
        "name": account["name"],
        "status": account["status"],
        "messages_sent": account.get("messages_sent", 0),
        "last_active_at": account.get("last_active_at", ""),
        "runtime_online": manager.is_running(account_id),
    })


# ── Session Generator (Web) ──────────────────────────────

@app.get("/session-generator", response_class=HTMLResponse)
async def session_generator_page(request: Request):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        return _redirect_login()
    return templates.TemplateResponse(request, "session.html", {
        "step": "phone",
        "error": "",
        "token": "",
        "phone": "",
        "result": None,
    })


@app.post("/session-generator/send-code")
async def session_send_code(
    request: Request,
    phone: str = Form(...),
):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        return _redirect_login()
    try:
        result = await session_web.send_code(phone)
        return templates.TemplateResponse(request, "session.html", {
            "step": "verify",
            "error": "",
            "token": result["token"],
            "phone": result["phone"],
            "result": None,
        })
    except RuntimeError as exc:
        return templates.TemplateResponse(request, "session.html", {
            "step": "phone",
            "error": str(exc),
            "token": "",
            "phone": phone,
            "result": None,
        })


@app.post("/session-generator/verify")
async def session_verify(
    request: Request,
    token: str = Form(...),
    code: str = Form(...),
    password: str = Form(""),
):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        return _redirect_login()
    try:
        result = await session_web.verify_code(token, code, password)
        return templates.TemplateResponse(request, "session.html", {
            "step": "done",
            "error": "",
            "token": "",
            "phone": "",
            "result": result,
        })
    except RuntimeError as exc:
        if str(exc) == "NEED_2FA":
            return templates.TemplateResponse(request, "session.html", {
                "step": "2fa",
                "error": "",
                "token": token,
                "phone": "",
                "result": None,
            })
        return templates.TemplateResponse(request, "session.html", {
            "step": "verify",
            "error": str(exc),
            "token": token,
            "phone": "",
            "result": None,
        })


@app.post("/session-generator/2fa")
async def session_2fa(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
):
    if not read_login_cookie(request.cookies.get(COOKIE_NAME)):
        return _redirect_login()
    try:
        result = await session_web.verify_code(token, "", password)
        return templates.TemplateResponse(request, "session.html", {
            "step": "done",
            "error": "",
            "token": "",
            "phone": "",
            "result": result,
        })
    except RuntimeError as exc:
        return templates.TemplateResponse(request, "session.html", {
            "step": "2fa",
            "error": str(exc),
            "token": token,
            "phone": "",
            "result": None,
        })
