from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class TelegramError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, retry_after: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def mask_token(token: str) -> str:
    return token[:6] + "..." if len(token) > 8 else "***"


class TelegramClient:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org", timeout: int = 15):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[int] = None) -> Any:
        url = f"{self.api_base}/bot{self.token}/{method}"
        data = json.dumps(params or {}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except ValueError:
                raise TelegramError(f"HTTP {exc.code} from Telegram", code=exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TelegramError(f"cannot reach Telegram: {reason}") from exc
        if not payload.get("ok"):
            code = payload.get("error_code")
            desc = payload.get("description", "unknown error")
            retry = (payload.get("parameters") or {}).get("retry_after")
            hint = ""
            if code == 401:
                hint = " - the bot token is wrong or was revoked"
            elif code == 400 and "chat not found" in desc.lower():
                hint = " - wrong chat_id, or the bot has not been started / added to that chat yet"
            elif code == 403:
                hint = " - the bot was blocked or removed from the chat"
            raise TelegramError(f"Telegram: {desc}{hint}", code=code, retry_after=retry)
        return payload.get("result")


    def get_me(self) -> Dict[str, Any]:
        return self.call("getMe")

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        thread_id: Optional[int] = None,
        silent: bool = False,
        html: bool = True,
    ) -> Any:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if html:
            params["parse_mode"] = "HTML"
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        return self.call("sendMessage", params)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "my_chat_member"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + self.timeout) or []

    def webhook_url(self) -> str:
        return (self.call("getWebhookInfo") or {}).get("url", "")


def chats_from_updates(updates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chats: Dict[int, Dict[str, Any]] = {}
    for upd in updates:
        msg = upd.get("message") or (upd.get("my_chat_member") or {})
        chat = msg.get("chat") if isinstance(msg, dict) else None
        if not chat or "id" not in chat:
            continue
        title = chat.get("title") or " ".join(
            filter(None, [chat.get("first_name"), chat.get("last_name")])
        ) or chat.get("username") or str(chat["id"])
        chats[chat["id"]] = {"id": chat["id"], "title": title, "type": chat.get("type", "private")}
    return list(reversed(list(chats.values())))
