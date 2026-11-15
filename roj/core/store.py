"""SQLite store with reentrant, immediate transactions.

One file per project (`state.db`) and one per installation (`roj.db`).
WAL mode lets the CLI read while a worker writes.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, type TEXT NOT NULL, actor TEXT NOT NULL,
  trace_id TEXT, task_id TEXT, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS ev_type ON events(type);
CREATE INDEX IF NOT EXISTS ev_task ON events(task_id);

CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, workflow TEXT NOT NULL, inputs TEXT NOT NULL, status TEXT NOT NULL,
  error TEXT, owner_host TEXT, owner_pid INTEGER, created TEXT NOT NULL, updated TEXT NOT NULL,
  result_ref TEXT);
CREATE INDEX IF NOT EXISTS task_status ON tasks(status);

CREATE TABLE IF NOT EXISTS steps(
  task_id TEXT NOT NULL, idx INTEGER NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, result_ref TEXT, started TEXT, finished TEXT, error TEXT,
  PRIMARY KEY(task_id, idx));

CREATE TABLE IF NOT EXISTS llm_cache(
  key TEXT PRIMARY KEY, model TEXT NOT NULL, response TEXT NOT NULL, created TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS budget_alloc(
  scope TEXT PRIMARY KEY, tokens INTEGER, usd REAL, set_by TEXT NOT NULL, updated TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS budget_spend(
  id INTEGER PRIMARY KEY, day TEXT NOT NULL, scope TEXT NOT NULL, tokens INTEGER NOT NULL,
  usd REAL NOT NULL, kind TEXT NOT NULL, ref TEXT, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS spend_day ON budget_spend(day, scope);

CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, key TEXT NOT NULL, action TEXT NOT NULL, tier TEXT NOT NULL, task_id TEXT,
  details TEXT NOT NULL, amount REAL, status TEXT NOT NULL, created TEXT NOT NULL, expires TEXT,
  decided TEXT, decided_by TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS approval_key ON approvals(key);

CREATE TABLE IF NOT EXISTS http_cache(
  key TEXT PRIMARY KEY, url_masked TEXT NOT NULL, etag TEXT, last_modified TEXT, ref TEXT,
  status INTEGER, content_type TEXT, fetched_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS robots_cache(
  origin TEXT PRIMARY KEY, body TEXT, status INTEGER, fetched_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS skill_specs(
  skill TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL, spec_ref TEXT NOT NULL,
  created TEXT NOT NULL, created_by TEXT NOT NULL, task_id TEXT, notes TEXT,
  PRIMARY KEY(skill, version));

CREATE TABLE IF NOT EXISTS skill_state(
  skill TEXT PRIMARY KEY, active_version INTEGER, last_run_task TEXT, last_window_end TEXT, updated TEXT);

CREATE TABLE IF NOT EXISTS source_health(
  skill TEXT NOT NULL, source_id TEXT NOT NULL, consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_ok TEXT, last_error TEXT, updated TEXT, PRIMARY KEY(skill, source_id));

CREATE TABLE IF NOT EXISTS digest_history(
  day TEXT NOT NULL, fingerprint TEXT NOT NULL, headline TEXT NOT NULL, tokens TEXT NOT NULL,
  PRIMARY KEY(day, fingerprint));

CREATE TABLE IF NOT EXISTS schedule_runs(
  skill TEXT NOT NULL, day TEXT NOT NULL, task_id TEXT, status TEXT NOT NULL, started TEXT NOT NULL,
  PRIMARY KEY(skill, day));

CREATE TABLE IF NOT EXISTS ratings(
  task_id TEXT PRIMARY KEY, skill TEXT, score INTEGER NOT NULL, comment TEXT, source TEXT, created TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, sent TEXT, error TEXT);

CREATE TABLE IF NOT EXISTS eval_cases(
  suite TEXT NOT NULL, case_id TEXT NOT NULL, data TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(suite, case_id));

CREATE TABLE IF NOT EXISTS gates(
  suite TEXT NOT NULL, metric TEXT NOT NULL, threshold REAL NOT NULL, streak INTEGER NOT NULL DEFAULT 0,
  updated TEXT NOT NULL, PRIMARY KEY(suite, metric));

CREATE TABLE IF NOT EXISTS gate_history(
  id INTEGER PRIMARY KEY, suite TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, threshold REAL,
  passed INTEGER NOT NULL, ref TEXT, created TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS blueprints(
  version INTEGER PRIMARY KEY, ref TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL,
  created_by TEXT NOT NULL, approved_without_edits INTEGER, diff TEXT);

CREATE TABLE IF NOT EXISTS kb_packs(
  pack TEXT PRIMARY KEY, manifest TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_docs(
  pack TEXT NOT NULL, doc_id TEXT NOT NULL, title TEXT, source TEXT, sha256 TEXT NOT NULL, meta TEXT,
  created TEXT NOT NULL, PRIMARY KEY(pack, doc_id));
CREATE TABLE IF NOT EXISTS kb_chunks(
  id INTEGER PRIMARY KEY, pack TEXT NOT NULL, doc_id TEXT NOT NULL, ord INTEGER NOT NULL, section TEXT,
  text TEXT NOT NULL, context TEXT, sha256 TEXT NOT NULL, vector BLOB);
CREATE INDEX IF NOT EXISTS kb_chunk_pack ON kb_chunks(pack, doc_id);

CREATE TABLE IF NOT EXISTS task_memo(
  task_id TEXT NOT NULL, key TEXT NOT NULL, ref TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(task_id, key));

CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, recipient TEXT NOT NULL, envelope TEXT NOT NULL, status TEXT NOT NULL,
  created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS msg_inbox ON messages(recipient, status);
CREATE TABLE IF NOT EXISTS missions(
  id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL, control TEXT NOT NULL, state TEXT NOT NULL,
  created TEXT NOT NULL, updated TEXT NOT NULL);
"""

_EVENTS_DDL = PROJECT_SCHEMA.split("CREATE TABLE IF NOT EXISTS tasks")[0]

GLOBAL_SCHEMA = _EVENTS_DDL + """
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pool_items(
  kind TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
  sha256 TEXT NOT NULL, code TEXT NOT NULL, card TEXT NOT NULL, tests TEXT, uses INTEGER NOT NULL DEFAULT 0,
  projects TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL, created_by TEXT NOT NULL,
  PRIMARY KEY(kind, name, version));
CREATE TABLE IF NOT EXISTS models(
  id TEXT PRIMARY KEY, status TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  blocked_until TEXT, block_reason TEXT, meta TEXT);
"""


class Store:
    def __init__(self, path: Path, schema: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._depth = 0
        self.conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(schema)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Immediate transaction; nested calls join the outer one."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield self.conn
                finally:
                    self._depth -= 1
                return
            self.conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")
            finally:
                self._depth = 0

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def run(self, sql: str, params: tuple | dict = ()) -> Any:
        with self.tx() as c:
            return c.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self.conn.close()
