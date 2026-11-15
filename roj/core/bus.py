"""Message bus: typed envelopes between agents, persisted and logged.

No free-form chat between agents: every message has a type, a sender and a
recipient, and is visible in the event log.
"""

from __future__ import annotations

import json

from ..contracts import Envelope
from ..util import canonical_json, utcnow_iso
from .eventlog import EventLog
from .store import Store


class Bus:
    def __init__(self, store: Store, events: EventLog):
        self.store = store
        self.events = events

    def send(self, env: Envelope) -> str:
        data = env.model_dump(mode="json")
        with self.store.tx() as c:
            c.execute(
                "INSERT INTO messages(id, recipient, envelope, status, created) VALUES(?,?,?, 'pending', ?)",
                (env.id, env.recipient, canonical_json(data), utcnow_iso()),
            )
            self.events.append(
                f"msg.{env.type.value}",
                {"id": env.id, "from": env.sender, "to": env.recipient, "priority": int(env.priority),
                 "payload": env.payload, "refs": env.refs},
                actor=env.sender, trace_id=env.trace_id, task_id=env.task_id,
            )
        return env.id

    def inbox(self, recipient: str, limit: int = 50) -> list[Envelope]:
        rows = self.store.all(
            "SELECT envelope FROM messages WHERE recipient = ? AND status = 'pending' ORDER BY created LIMIT ?",
            (recipient, limit),
        )
        envs = [Envelope.model_validate(json.loads(r["envelope"])) for r in rows]
        return sorted(envs, key=lambda e: (int(e.priority), e.ts))

    def ack(self, message_id: str, by: str) -> None:
        self.store.run("UPDATE messages SET status = 'done' WHERE id = ?", (message_id,))
        self.events.append("msg.ack", {"id": message_id}, actor=by)
