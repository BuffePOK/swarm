"""Owner notifications through an outbox, and Telegram callbacks (buttons).

The kernel never talks to Telegram while deciding: policy writes an outbox row
(same transaction as the approval) and `flush()` delivers it later from the CLI,
the scheduler tick, the panel or `roj telegram poll`.

Callback data (<= 64 bytes):
  ap:<approval16>:y|n     approve / reject
  rt:<task16>:<1-5>       rate a brief
"""

from __future__ import annotations

import html
import json
import logging
from typing import TYPE_CHECKING, Any

from ..util import canonical_json, utcnow_iso
from .telegram import NotifyError, api, configured, send_telegram

if TYPE_CHECKING:
    from ..project import Installation, Project

log = logging.getLogger("roj.notify")


def enqueue(store, kind: str, payload: dict[str, Any]) -> None:
    store.run("INSERT INTO outbox(kind, payload, status, created) VALUES(?,?, 'pending', ?)",
              (kind, canonical_json(payload), utcnow_iso()))


def _approval_text(project: str, p: dict[str, Any]) -> str:
    details = json.dumps(p.get("details", {}), ensure_ascii=False, indent=1)
    if len(details) > 1800:
        details = details[:1800] + "\n…"
    return (f"<b>Approval needed · {html.escape(project)}</b>\nAction: <code>{html.escape(p['action'])}</code>\n"
            f"Reason: {html.escape(p.get('reason', ''))}\n<pre>{html.escape(details)}</pre>")


async def flush(project: "Project") -> dict[str, int]:
    rows = project.store.all("SELECT * FROM outbox WHERE status = 'pending' ORDER BY id LIMIT 20")
    counts = {"sent": 0, "no_channel": 0, "failed": 0}
    if not rows:
        return counts
    if not configured(project.settings.notify):
        project.store.run("UPDATE outbox SET status = 'no_channel' WHERE status = 'pending'")
        counts["no_channel"] = len(rows)
        return counts
    for r in rows:
        p = json.loads(r["payload"])
        try:
            if r["kind"] == "approval":
                aid = p["id"]
                await send_telegram(project.settings.notify, _approval_text(project.name, p),
                                    buttons=[[("Approve", f"ap:{aid[:16]}:y"), ("Reject", f"ap:{aid[:16]}:n")]])
            else:
                await send_telegram(project.settings.notify, p.get("html") or html.escape(p.get("text", "")))
            project.store.run("UPDATE outbox SET status = 'sent', sent = ?, attempts = attempts + 1 WHERE id = ?", (utcnow_iso(), r["id"]))
            counts["sent"] += 1
        except NotifyError as e:
            status = "failed" if r["attempts"] >= 4 else "pending"
            project.store.run("UPDATE outbox SET status = ?, attempts = attempts + 1, error = ? WHERE id = ?", (status, str(e)[:300], r["id"]))
            counts["failed"] += 1
            log.warning("outbox delivery failed: %s", e)
            break
    return counts


def _find(inst: "Installation", table: str, prefix: str) -> tuple[str, str] | None:
    for name in inst.project_names():
        p = inst.project(name)
        try:
            col = "id"
            row = p.store.one(f"SELECT {col} FROM {table} WHERE substr({col}, 1, ?) = ?", (len(prefix), prefix))
            if row:
                return name, row[col]
        finally:
            p.close()
    return None


async def handle_update(inst: "Installation", upd: dict[str, Any]) -> dict[str, Any] | None:
    s = inst.settings.notify
    cb = upd.get("callback_query")
    msg = upd.get("message")
    chat = str(((cb or {}).get("message") or msg or {}).get("chat", {}).get("id", ""))
    if chat != str(s.telegram_chat_id):
        log.warning("ignored Telegram update from chat %s (not the owner chat)", chat)
        inst.events.append("telegram.ignored", {"chat": chat})
        return None
    if msg and (msg.get("text") or "").strip().startswith("/status"):
        lines = []
        q = "SELECT COUNT(*) n FROM tasks WHERE status IN ('paused_api', 'paused_budget', 'failed')"
        for name in inst.project_names():
            p = inst.project(name)
            try:
                lines.append(f"{name}: {len(p.policy.pending())} approvals pending, {p.store.one(q)['n']} paused/failed")
            finally:
                p.close()
        await send_telegram(s, html.escape("\n".join(lines) or "no projects"))
        return {"command": "status"}
    if not cb:
        return None
    data = cb.get("data", "")
    answer = "?"
    result: dict[str, Any] = {"data": data}
    try:
        kind, ident, value = data.split(":", 2)
        if kind == "ap":
            found = _find(inst, "approvals", ident)
            if not found:
                answer = "approval not found"
            else:
                pname, aid = found
                p = inst.project(pname)
                try:
                    p.policy.decide(aid, value == "y", by="owner:telegram")
                    task = p.store.one("SELECT task_id FROM approvals WHERE id = ?", (aid,))["task_id"]
                    answer = "Approved" if value == "y" else "Rejected"
                    result.update(project=pname, approval=aid, decision=answer)
                    if task:
                        res = await p.kernel.resume(task)
                        result["task_status"] = res[0].status.value if res else "not resumed"
                finally:
                    p.close()
        elif kind == "rt":
            found = _find(inst, "tasks", ident)
            if found:
                pname, tid = found
                p = inst.project(pname)
                try:
                    rate(p, tid, int(value), source="telegram")
                finally:
                    p.close()
                answer = f"Thanks: {value}/5"
                result.update(project=pname, task=tid, rating=int(value))
            else:
                answer = "brief not found"
    except (ValueError, KeyError) as e:
        answer = f"error: {e}"[:180]
    try:
        await api(s, "answerCallbackQuery", {"callback_query_id": cb.get("id"), "text": answer})
        if cb.get("message"):
            await api(s, "editMessageReplyMarkup", {"chat_id": s.telegram_chat_id, "message_id": cb["message"]["message_id"],
                                                    "reply_markup": {"inline_keyboard": []}})
    except NotifyError as e:
        log.warning("callback answer failed: %s", e)
    inst.events.append("telegram.callback", result, actor="owner:telegram")
    return result


async def poll_once(inst: "Installation", timeout: int = 0) -> list[dict[str, Any]]:
    s = inst.settings.notify
    if not configured(s):
        return []
    row = inst.store.one("SELECT value FROM kv WHERE key = 'telegram_offset'")
    offset = int(row["value"]) if row else 0
    updates = await api(s, "getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
                        timeout=timeout + 15)
    out = []
    for u in updates:
        offset = max(offset, int(u["update_id"]) + 1)
        inst.store.run("INSERT OR REPLACE INTO kv(key, value, updated) VALUES('telegram_offset', ?, ?)", (str(offset), utcnow_iso()))
        r = await handle_update(inst, u)
        if r:
            out.append(r)
    return out


def rate(project: "Project", task_id: str, score: int, *, comment: str = "", source: str = "panel") -> None:
    if not 1 <= score <= 5:
        raise ValueError("score must be 1-5")
    project.store.run("INSERT OR REPLACE INTO ratings(task_id, skill, score, comment, source, created) VALUES(?,?,?,?,?,?)",
                      (task_id, "news_digest", score, comment[:500], source, utcnow_iso()))
    project.events.append("brief.rated", {"task_id": task_id, "score": score, "comment": comment[:200], "source": source}, actor="owner")


async def flush_all(inst: "Installation") -> dict[str, int]:
    total = {"sent": 0, "no_channel": 0, "failed": 0}
    for name in inst.project_names():
        p = inst.project(name)
        try:
            for k, v in (await flush(p)).items():
                total[k] += v
        finally:
            p.close()
    return total
