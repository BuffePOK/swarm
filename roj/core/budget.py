"""Hierarchical daily budgets in tokens and USD (ADR-0004).

Scopes: "" (project root) -> "team:<name>" -> "team:<name>/agent:<name>".
A reservation must fit the limit of the scope itself and of every ancestor,
so a team can never spend more than its project, and an agent more than its team.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from ..config import BudgetSettings
from ..util import day_key, utcnow, utcnow_iso
from .eventlog import EventLog
from .store import Store

_SEGMENT = re.compile(r"^(team|agent|role|cycle|skill):[A-Za-z0-9_\-.]+$")


class BudgetExceeded(Exception):
    def __init__(self, scope: str, kind: str, limit: float, used: float, requested: float):
        self.scope, self.kind, self.limit, self.used, self.requested = scope, kind, limit, used, requested
        where = scope or "project"
        super().__init__(f"budget exceeded for {where}: {kind} used {used:g} + requested {requested:g} > limit {limit:g}")


@dataclass(frozen=True)
class Reservation:
    id: str
    day: str
    scope: str
    tokens: int
    usd: float


def validate_scope(scope: str) -> str:
    scope = scope.strip("/")
    if scope and not all(_SEGMENT.match(s) for s in scope.split("/")):
        raise ValueError(f"invalid budget scope {scope!r}; use e.g. 'team:analysis/agent:writer'")
    return scope


def ancestors(scope: str) -> list[str]:
    """'' first, then each prefix, ending with the scope itself."""
    parts = scope.split("/") if scope else []
    return [""] + ["/".join(parts[: i + 1]) for i in range(len(parts))]


class BudgetLedger:
    def __init__(self, store: Store, events: EventLog, settings: BudgetSettings, tz: str):
        self.store = store
        self.events = events
        self.settings = settings
        self.tz = tz

    def today(self) -> str:
        return day_key(self.tz)

    # --- limits ------------------------------------------------------------
    def allocate(self, scope: str, tokens: int | None, usd: float | None, set_by: str = "owner") -> list[str]:
        """Set a daily limit for a child scope. Returns warnings (e.g. over-allocation)."""
        scope = validate_scope(scope)
        if not scope:
            raise ValueError("the project root budget lives in project.toml [budget]")
        self.store.run(
            "INSERT INTO budget_alloc(scope, tokens, usd, set_by, updated) VALUES(?,?,?,?,?)"
            " ON CONFLICT(scope) DO UPDATE SET tokens=excluded.tokens, usd=excluded.usd,"
            " set_by=excluded.set_by, updated=excluded.updated",
            (scope, tokens, usd, set_by, utcnow_iso()),
        )
        self.events.append("budget.allocated", {"scope": scope, "tokens": tokens, "usd": usd}, actor=set_by)
        return self._overallocation_warnings(scope)

    def limit_of(self, scope: str) -> tuple[int | None, float | None]:
        if scope == "":
            return self.settings.tokens_per_day, self.settings.usd_per_day
        row = self.store.one("SELECT tokens, usd FROM budget_alloc WHERE scope = ?", (scope,))
        return (row["tokens"], row["usd"]) if row else (None, None)

    def _overallocation_warnings(self, scope: str) -> list[str]:
        parent = "/".join(scope.split("/")[:-1])
        p_tokens, p_usd = self.limit_of(parent)
        n = len(parent) + 1 if parent else 0
        rows = self.store.all("SELECT scope, tokens, usd FROM budget_alloc")
        children = [r for r in rows if r["scope"][:n] == (parent + "/" if parent else "") and "/" not in r["scope"][n:]]
        warnings = []
        t_sum = sum(r["tokens"] or 0 for r in children)
        u_sum = sum(r["usd"] or 0 for r in children)
        if p_tokens is not None and t_sum > p_tokens:
            warnings.append(f"children of '{parent or 'project'}' are allocated {t_sum} tokens > parent {p_tokens}")
        if p_usd is not None and u_sum > p_usd:
            warnings.append(f"children of '{parent or 'project'}' are allocated {u_sum:.2f} USD > parent {p_usd:.2f}")
        return warnings

    # --- usage -------------------------------------------------------------
    def usage(self, scope: str = "", day: str | None = None, *, conn=None) -> tuple[int, float]:
        """Tokens and USD used by the scope's subtree, including active reservations."""
        day = day or self.today()
        q = "SELECT COALESCE(SUM(tokens),0) t, COALESCE(SUM(usd),0) u FROM budget_spend WHERE day = ?"
        params: tuple
        if scope:
            q += " AND (scope = ? OR substr(scope, 1, ?) = ?)"
            params = (day, scope, len(scope) + 1, scope + "/")
        else:
            params = (day,)
        row = (conn.execute(q, params).fetchone() if conn else self.store.one(q, params))
        return int(row["t"]), float(row["u"])

    def _expire_stale(self, conn) -> None:
        cutoff = (utcnow() - timedelta(seconds=self.settings.reservation_ttl_s)).isoformat(timespec="milliseconds")
        stale = conn.execute(
            "SELECT id, ref, scope FROM budget_spend WHERE kind = 'reserve' AND created < ?", (cutoff,)
        ).fetchall()
        if stale:
            conn.execute("DELETE FROM budget_spend WHERE kind = 'reserve' AND created < ?", (cutoff,))
            self.events.append("budget.reservations_expired", {"count": len(stale)})

    # --- reserve / commit --------------------------------------------------
    def reserve(self, scope: str, tokens: int, usd: float, ref: str | None = None) -> Reservation:
        scope = validate_scope(scope)
        day = self.today()
        rid = uuid.uuid4().hex
        with self.store.tx() as c:
            self._expire_stale(c)
            for anc in ancestors(scope):
                lim_t, lim_u = self.limit_of(anc)
                used_t, used_u = self.usage(anc, day, conn=c)
                if lim_t is not None and used_t + tokens > lim_t:
                    raise BudgetExceeded(anc, "tokens", lim_t, used_t, tokens)
                if lim_u is not None and used_u + usd > lim_u + 1e-12:
                    raise BudgetExceeded(anc, "usd", lim_u, round(used_u, 6), round(usd, 6))
            c.execute(
                "INSERT INTO budget_spend(id, day, scope, tokens, usd, kind, ref, created)"
                " VALUES(NULL,?,?,?,?, 'reserve', ?, ?)",
                (day, scope, tokens, usd, rid, utcnow_iso()),
            )
        return Reservation(rid, day, scope, tokens, usd)

    def commit(self, res: Reservation, tokens: int, usd: float, ref: str | None = None) -> None:
        """Replace the reservation with actual usage (may exceed the estimate; it is recorded as is)."""
        with self.store.tx() as c:
            c.execute("DELETE FROM budget_spend WHERE kind = 'reserve' AND ref = ?", (res.id,))
            c.execute(
                "INSERT INTO budget_spend(id, day, scope, tokens, usd, kind, ref, created)"
                " VALUES(NULL,?,?,?,?, 'commit', ?, ?)",
                (res.day, res.scope, tokens, usd, ref or res.id, utcnow_iso()),
            )

    def release(self, res: Reservation) -> None:
        self.store.run("DELETE FROM budget_spend WHERE kind = 'reserve' AND ref = ?", (res.id,))

    # --- reporting ---------------------------------------------------------
    def report(self, day: str | None = None) -> list[dict]:
        day = day or self.today()
        scopes = {""} | {r["scope"] for r in self.store.all("SELECT scope FROM budget_alloc")}
        scopes |= {r["scope"] for r in self.store.all("SELECT DISTINCT scope FROM budget_spend WHERE day = ?", (day,))}
        out = []
        for s in sorted(scopes, key=lambda x: (x.count("/"), x)):
            lt, lu = self.limit_of(s)
            ut, uu = self.usage(s, day)
            out.append({"scope": s or "(project)", "tokens_used": ut, "tokens_limit": lt,
                        "usd_used": round(uu, 6), "usd_limit": lu})
        return out


