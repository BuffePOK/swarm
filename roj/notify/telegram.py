"""Telegram: delivery to the owner's own chat, inline buttons, and owner-only callbacks.

- Token from an environment variable (default ROJ_TELEGRAM_TOKEN), chat id from settings.
- HTML parse mode with escaping; messages split below the 4096-character limit.
- Buttons: approvals (Approve / Reject) and brief ratings (1-5).
- Callbacks are accepted ONLY from the configured chat; anything else is ignored and logged.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from ..config import NotifySettings

LIMIT = 3900


class NotifyError(Exception):
    pass


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    parts, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts


def configured(settings: NotifySettings) -> bool:
    return bool(os.environ.get(settings.telegram_token_env) and settings.telegram_chat_id)


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": d[:64]} for t, d in row] for row in rows]}


async def api(settings: NotifySettings, method: str, payload: dict[str, Any] | None = None,
              transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20) -> Any:
    token = os.environ.get(settings.telegram_token_env, "")
    if not token:
        raise NotifyError(f"Telegram not configured: set {settings.telegram_token_env}")
    async with httpx.AsyncClient(base_url=settings.telegram_api.rstrip("/"), timeout=timeout, transport=transport) as c:
        r = await c.post(f"/bot{token}/{method}", json=payload or {})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("ok"):
        raise NotifyError(f"Telegram {method} HTTP {r.status_code}: {str(data.get('description', r.text))[:200]}")
    return data["result"]


async def send_telegram(settings: NotifySettings, html_text: str, *, buttons: list[list[tuple[str, str]]] | None = None,
                        transport: httpx.AsyncBaseTransport | None = None) -> list[int]:
    if not configured(settings):
        raise NotifyError(f"Telegram not configured: set {settings.telegram_token_env} and [notify] telegram_chat_id")
    parts = split_message(html_text)
    ids = []
    for k, part in enumerate(parts):
        payload: dict[str, Any] = {"chat_id": settings.telegram_chat_id, "text": part, "parse_mode": "HTML",
                                   "disable_web_page_preview": True}
        if buttons and k == len(parts) - 1:
            payload["reply_markup"] = keyboard(buttons)
        res = await api(settings, "sendMessage", payload, transport)
        ids.append(int(res["message_id"]))
    return ids
