import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from app import db

log = logging.getLogger("account-manager")
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "16"))
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "4000"))


@dataclass
class Runtime:
    account_id: int
    client: TelegramClient
    task: asyncio.Task[Any] | None = None
    self_id: int | None = None
    username: str = ""
    locks: dict[tuple[int, int], asyncio.Lock] = field(default_factory=dict)


class AccountManager:
    def __init__(self) -> None:
        self.runtimes: dict[int, Runtime] = {}
        self.cleanup_task: asyncio.Task[Any] | None = None
        self._guard = asyncio.Lock()
        # AI client 快取：避免每條訊息都重建 AsyncOpenAI
        self._ai_clients: dict[int, AsyncOpenAI] = {}
        # 群組冷卻：(account_id, chat_id) → 上次回覆時間戳
        self._cooldowns: dict[tuple[int, int], float] = {}

    @property
    def managed_user_ids(self) -> set[int]:
        # 包含所有已啟動的帳號（含尚未解析 self_id 的），避免啟動窗口期互觸
        result: set[int] = set()
        for runtime in self.runtimes.values():
            if runtime.self_id is not None:
                result.add(runtime.self_id)
            elif runtime.task and not runtime.task.done():
                # 尚未解析 self_id，但帳號已在運行，用 account_id 佔位避免漏判
                pass
        # 同時加入 DB 中已知的 telegram_user_id（已登入過至少一次的帳號）
        for account in db.list_accounts():
            uid = account.get("telegram_user_id")
            if uid:
                result.add(int(uid))
        return result

    def is_running(self, account_id: int) -> bool:
        runtime = self.runtimes.get(account_id)
        return bool(runtime and runtime.task and not runtime.task.done())

    def _get_ai_client(self, account_id: int, api_key: str, base_url: str) -> AsyncOpenAI:
        """取得或建立快取的 AsyncOpenAI client。"""
        cached = self._ai_clients.get(account_id)
        if cached is not None:
            return cached
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._ai_clients[account_id] = client
        return client

    def invalidate_ai_client(self, account_id: int) -> None:
        """帳號設定變更時清除 AI client 快取。"""
        self._ai_clients.pop(account_id, None)

    async def startup(self) -> None:
        for account in db.list_accounts():
            if account["enabled"]:
                await self.start_account(account["id"])
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def shutdown(self) -> None:
        if self.cleanup_task:
            self.cleanup_task.cancel()
        for account_id in list(self.runtimes):
            await self.stop_account(account_id)

    async def start_account(self, account_id: int) -> None:
        async with self._guard:
            existing = self.runtimes.get(account_id)
            if existing and existing.task and not existing.task.done():
                return
            account = db.get_account(account_id, include_secrets=True)
            if not account:
                raise KeyError(account_id)
            client = TelegramClient(
                StringSession(account["session_string"]), TG_API_ID, TG_API_HASH
            )
            runtime = Runtime(account_id=account_id, client=client)
            self.runtimes[account_id] = runtime

            async def handler(event: Any, aid: int = account_id) -> None:
                await self._handle_message(aid, event)

            client.add_event_handler(handler, events.NewMessage(incoming=True))
            runtime.task = asyncio.create_task(self._run_account(runtime))

    async def restart_account(self, account_id: int) -> None:
        await self.stop_account(account_id)
        account = db.get_account(account_id)
        if account and account["enabled"]:
            await self.start_account(account_id)

    async def stop_account(self, account_id: int) -> None:
        async with self._guard:
            runtime = self.runtimes.pop(account_id, None)
        if not runtime:
            db.set_status(account_id, "stopped")
            return
        try:
            await runtime.client.disconnect()
        except Exception as exc:
            log.warning("帳號 %s 斷線時出錯（已忽略）：%s", account_id, exc)
        finally:
            if runtime.task and not runtime.task.done():
                runtime.task.cancel()
                try:
                    await runtime.task
                except asyncio.CancelledError:
                    pass
            # 清理該帳號的冷卻記錄與 AI client
            keys_to_remove = [k for k in self._cooldowns if k[0] == account_id]
            for k in keys_to_remove:
                del self._cooldowns[k]
            self.invalidate_ai_client(account_id)
            db.set_status(account_id, "stopped")

    async def _run_account(self, runtime: Runtime) -> None:
        db.set_status(runtime.account_id, "connecting")
        try:
            await runtime.client.connect()
            if not await runtime.client.is_user_authorized():
                raise RuntimeError("Telegram Session 已失效或未授權")
            me = await runtime.client.get_me()
            runtime.self_id = int(me.id)
            runtime.username = getattr(me, "username", "") or ""
            db.set_status(
                runtime.account_id,
                "online",
                telegram_user_id=runtime.self_id,
                telegram_username=runtime.username,
            )
            log.info("帳號 %s 已上線：%s (%s)", runtime.account_id, runtime.username, runtime.self_id)
            await runtime.client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("帳號 %s 執行失敗", runtime.account_id)
            db.set_status(runtime.account_id, "error", str(exc))
        finally:
            if self.runtimes.get(runtime.account_id) is runtime:
                self.runtimes.pop(runtime.account_id, None)

    @staticmethod
    def _parse_ids(raw: str) -> set[int]:
        result: set[int] = set()
        for value in raw.replace("\n", ",").split(","):
            value = value.strip()
            if value:
                try:
                    result.add(int(value))
                except ValueError:
                    pass
        return result

    async def _direct_trigger(self, runtime: Runtime, event: Any, text: str) -> bool:
        """檢查是否 @機器人 或回覆機器人。"""
        # 回覆機器人的訊息
        if event.message.reply_to_msg_id:
            try:
                replied = await event.get_reply_message()
                if replied and int(replied.sender_id or 0) == runtime.self_id:
                    return True
            except Exception:
                pass

        # @username 文字提及
        if runtime.username and f"@{runtime.username.lower()}" in text.lower():
            return True

        # Telethon mentioned flag（最後檢查，某些消息類型可能不準確）
        if getattr(event.message, "mentioned", False):
            return True

        return False

    def _check_cooldown(self, account_id: int, chat_id: int, cooldown_sec: float) -> bool:
        """回傳 True 表示已冷卻（可以回覆），False 表示仍在冷卻中。"""
        key = (account_id, chat_id)
        last_time = self._cooldowns.get(key, 0.0)
        if time.monotonic() - last_time < cooldown_sec:
            return False
        self._cooldowns[key] = time.monotonic()
        return True

    async def _handle_message(self, account_id: int, event: Any) -> None:
        runtime = self.runtimes.get(account_id)
        if not runtime or not event.is_group or event.out:
            return

        sender_id = int(event.sender_id or 0)
        chat_id = int(event.chat_id or 0)
        text = (event.raw_text or "").strip()
        if not sender_id or not chat_id or not text or len(text) > MAX_INPUT_CHARS:
            return

        # 避免受管帳號互相觸發形成回覆迴圈
        if sender_id in self.managed_user_ids:
            return

        account = db.get_account(account_id, include_secrets=True)
        if not account:
            return

        # 群組白名單
        allowed_groups = self._parse_ids(account["group_ids"])
        if allowed_groups and chat_id not in allowed_groups:
            return

        # 用戶黑名單
        blacklisted_users = self._parse_ids(account.get("user_blacklist", ""))
        if sender_id in blacklisted_users:
            return

        # 觸發模式判斷
        direct = await self._direct_trigger(runtime, event, text)
        if account["reply_mode"] == "mention" and not direct:
            return
        if account["reply_mode"] == "probability" and not direct:
            if random.random() > account["reply_probability"]:
                return

        # 群組冷卻
        cooldown_sec = float(account.get("reply_cooldown", 8.0))
        if not self._check_cooldown(account_id, chat_id, cooldown_sec):
            return

        lock = runtime.locks.setdefault((chat_id, sender_id), asyncio.Lock())
        if lock.locked():
            return

        async with lock:
            try:
                sender = await event.get_sender()
                sender_name = " ".join(
                    value
                    for value in [
                        getattr(sender, "first_name", "") or "",
                        getattr(sender, "last_name", "") or "",
                    ]
                    if value
                ).strip() or "群成員"
                chat = await event.get_chat()
                chat_title = getattr(chat, "title", "") or "未命名群組"

                reply = await self._generate_reply(
                    account, chat_id, sender_id, chat_title, sender_name, text
                )
                delay_min = max(0.0, account["min_delay"])
                delay_max = max(delay_min, account["max_delay"])
                async with runtime.client.action(chat_id, "typing"):
                    await asyncio.sleep(random.uniform(delay_min, delay_max))
                await event.reply(reply)
                db.save_message(account_id, chat_id, sender_id, "user", text)
                db.save_message(account_id, chat_id, sender_id, "assistant", reply)
                db.set_last_error(account_id, "")
            except Exception as exc:
                log.exception("帳號 %s 處理群訊息失敗", account_id)
                db.set_last_error(account_id, str(exc))

    async def _generate_reply(
        self,
        account: dict[str, Any],
        chat_id: int,
        sender_id: int,
        chat_title: str,
        sender_name: str,
        text: str,
    ) -> str:
        history = db.get_history(
            account["id"], chat_id, sender_id, MAX_HISTORY_MESSAGES
        )
        system = (
            f"固定角色類型：{account['role_type']}。\n"
            f"目前群組：{chat_title}；目前發言者：{sender_name}。\n"
            f"{account['persona']}"
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        ai = self._get_ai_client(account["id"], account["ai_api_key"], account["ai_base_url"])
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await ai.chat.completions.create(
                    model=account["ai_model"],
                    messages=messages,
                    temperature=account["temperature"],
                    max_tokens=account["max_tokens"],
                )
                reply = (response.choices[0].message.content or "").strip()
                if not reply:
                    raise RuntimeError("AI 模型回傳空白內容")
                return reply
            except Exception as exc:
                last_error = exc
                log.warning("帳號 %s AI 呼叫失敗（第 %s 次）：%s", account["id"], attempt + 1, exc)
                await asyncio.sleep(2**attempt)
        raise RuntimeError(f"AI 呼叫連續失敗：{last_error}")

    async def _cleanup_loop(self) -> None:
        """每小時清理過期記憶。"""
        while True:
            try:
                removed = db.cleanup_memory()
                if removed:
                    log.info("已清除 %s 筆過期記憶", removed)
            except Exception:
                log.exception("記憶清理失敗")
            await asyncio.sleep(3600)
