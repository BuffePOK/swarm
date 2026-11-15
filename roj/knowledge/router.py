"""Selective retrieval: decide whether to retrieve at all, from which packs, and how much.

Not every pack goes to every agent and not every hit goes into a prompt:
- a pack is searched only if the agent's role is in its audience (shared knowledge of a
  speciality, e.g. two DevOps agents share one pack) and the pack's vocabulary covers the query
- a chunk is injected only if it contains enough of the query's terms
- the total is capped in tokens
All of this is arithmetic on text: retrieval decisions cost no model calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..util import estimate_tokens
from .packs import Hit, KnowledgeBase

if TYPE_CHECKING:
    from ..project import Project

_W = re.compile(r"\w{3,}", re.UNICODE)
STOP = set("the and for are was were with that this from have has not but you your what which when where how why who can will "
           "jak jest oraz dla nie ale czy tak ten tego jego ich przez przy lub aby który która które gdzie kiedy co to na do się "
           "about into than then them they their there does did been being also only".split())


def stems(text: str) -> set[str]:
    """Lowercase terms cut to 5 letters: a crude stem that survives Polish and English inflection."""
    return {w[:5] for w in _W.findall(text.lower()) if w not in STOP}


def coverage(query_stems: set[str], text: str) -> float:
    return len(query_stems & stems(text)) / len(query_stems) if query_stems else 0.0


@dataclass
class Retrieval:
    hits: list[Hit] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0


def audience_ok(manifest: dict[str, Any], role: str) -> bool:
    aud = manifest.get("audience") or ["*"]
    return "*" in aud or role in aud


async def retrieve(project: "Project", query: str, *, role: str = "*", packs: list[str] | None = None, k: int = 6,
                   max_tokens: int | None = None) -> Retrieval:
    cfg = project.settings.knowledge
    kb = KnowledgeBase(project)
    q = stems(query)
    out = Retrieval()
    scored: list[tuple[float, float, Hit, str]] = []
    for info in kb.packs():
        name = info["pack"]
        if packs is not None and name not in packs:
            continue
        m = kb.manifest(name)
        if not audience_ok(m, role):
            out.decisions.append({"pack": name, "used": False, "why": f"role {role} is not in its audience"})
            continue
        cov = coverage(q, m["description"] + " " + " ".join(kb._vocabulary(name, 300)))
        if cov < cfg.min_pack_coverage:
            out.decisions.append({"pack": name, "used": False, "why": f"off-topic (covers {cov:.0%} of the query terms)"})
            continue
        hits = await kb.search(name, query, k=k)
        kept = 0
        for h in hits:
            c = coverage(q, f"{h.title} {h.context} {h.text}")
            if c >= cfg.min_hit_coverage or (len(h.via) >= 2 and c >= cfg.min_hit_coverage / 2):
                scored.append((c, h.score, h, name))
                kept += 1
        out.decisions.append({"pack": name, "used": kept > 0, "why": f"{kept} of {len(hits)} hits relevant", "coverage": round(cov, 2)})
    limit = max_tokens or cfg.max_tokens
    seen: set[str] = set()
    for c, _, h, name in sorted(scored, key=lambda x: (x[0], x[1]), reverse=True):
        t = estimate_tokens(h.text)
        if h.text in seen or out.tokens + t > limit:
            continue
        seen.add(h.text)
        out.hits.append(h)
        out.tokens += t
    project.events.append("kb.route", {"role": role, "query": query[:200], "hits": len(out.hits), "tokens": out.tokens,
                                       "decisions": out.decisions})
    return out
