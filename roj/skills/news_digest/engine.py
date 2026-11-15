"""Deterministic engine of the morning brief: zero tokens.

fetch -> extract -> window -> cluster -> rule classify -> rank -> gates -> render.
Everything a model is not needed for lives here.
"""

from __future__ import annotations

import asyncio
import html
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ...hands.feeds import FeedError, discover_feeds, looks_like_feed, parse_feed
from ...hands.http import Fetcher, FetchResult
from ...hands.market import InstrumentSpec, MarketError, Quote, fetch_quote
from ...skills.sandbox import SandboxError, run_adapter
from ...util import sha256_hex
from .spec import DigestSpec, RankingSpec, SourceSpec, TopicRule

STOPWORDS = set("""
a an the and or but of to in on at for from by with as is are was were be been being it its this that these those
after before over under into amid about against than then new says said say will would could may might can up down
out off more most less least very just also not no yes how why what when where who which while has have had do does
did his her their our your they them we you he she i report reports update live latest week day year today
""".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9'.\-]*[a-z0-9]")
_NUM = re.compile(r"(?<![A-Za-z0-9.])[-+]?\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")


class ExtractError(Exception):
    pass


@dataclass
class Item:
    id: str
    source_id: str
    source_name: str
    tier: str
    title: str
    link: str
    published: str | None
    summary: str
    independence_key: str

    def snippet(self) -> str:
        return f"{self.title}. {self.summary}".strip()


@dataclass
class Event:
    id: str
    items: list[Item]
    tokens: list[str]
    topics: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    continuing: bool = False
    relevance: int | None = None  # set by the classifier slot (0-3)

    @property
    def independent(self) -> int:
        return len({i.independence_key for i in self.items})

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "items": [asdict(i) for i in self.items], "tokens": self.tokens, "topics": self.topics,
                "score": round(self.score, 4), "continuing": self.continuing, "relevance": self.relevance}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Event":
        return Event(id=d["id"], items=[Item(**i) for i in d["items"]], tokens=d["tokens"], topics=d.get("topics", {}),
                     score=d.get("score", 0.0), continuing=d.get("continuing", False), relevance=d.get("relevance"))


# --- extraction --------------------------------------------------------------------

def tokens(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        w = w.strip("'-")
        if len(w) < 3 or w in STOPWORDS:
            continue
        out.add(w[:6])  # crude stem: "rates"/"rate", "falls"/"fall", "inflation"/"inflat"
    return out


def extract(source: SourceSpec, raw: bytes, *, adapter_code: str | None = None) -> list[dict[str, Any]]:
    if source.kind == "adapter":
        if not adapter_code:
            raise ExtractError(f"{source.id}: adapter {source.adapter_sha} not found in pool")
        try:
            return run_adapter(adapter_code, raw.decode("utf-8", "replace"), source.url)
        except SandboxError as e:
            raise ExtractError(f"{source.id}: adapter failed: {e}") from e
    try:
        _, items = parse_feed(raw, base_url=source.url)
    except FeedError as e:
        raise ExtractError(f"{source.id}: {e}") from e
    return [i.as_dict() for i in items]


def make_items(source: SourceSpec, raw_items: Iterable[dict[str, Any]], limit: int) -> list[Item]:
    out, seen = [], set()
    for r in raw_items:
        key = r.get("guid") or r.get("link") or r.get("title")
        iid = sha256_hex(f"{source.id}|{key}")[:12]
        if iid in seen:
            continue
        seen.add(iid)
        out.append(Item(id=iid, source_id=source.id, source_name=source.name, tier=source.tier,
                        title=(r.get("title") or "").strip(), link=(r.get("link") or "").strip(),
                        published=r.get("published"), summary=(r.get("summary") or "").strip(),
                        independence_key=source.independence_key or source.id))
        if len(out) >= limit:
            break
    return out


def in_window(items: list[Item], start: datetime, end: datetime) -> list[Item]:
    keep = []
    for it in items:
        if not it.published:
            keep.append(it)  # undated (adapter pages): repeats are handled by history
            continue
        try:
            t = datetime.fromisoformat(it.published)
        except ValueError:
            keep.append(it)
            continue
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        if start <= t <= end + timedelta(minutes=10):
            keep.append(it)
    return keep


async def fetch_sources(fetcher: Fetcher, sources: list[SourceSpec]) -> dict[str, FetchResult]:
    results = await asyncio.gather(*(fetcher.get(s.url, purpose=f"source:{s.id}") for s in sources))
    return {s.id: r for s, r in zip(sources, results)}


async def fetch_market(fetcher: Fetcher, instruments: list[InstrumentSpec]) -> tuple[list[Quote], dict[str, str]]:
    quotes, errors = [], {}
    results = await asyncio.gather(*(fetch_quote(fetcher, i) for i in instruments), return_exceptions=True)
    for spec, r in zip(instruments, results):
        if isinstance(r, Quote):
            quotes.append(r)
        elif isinstance(r, MarketError):
            errors[spec.id] = str(r)
        else:
            errors[spec.id] = f"{type(r).__name__}: {r}"
    return quotes, errors


# --- probing (bootstrap and repair) -----------------------------------------------

async def probe_source(fetcher: Fetcher, src: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Facts about one candidate source. No model involved."""
    rep: dict[str, Any] = {"source_id": src["id"], "name": src.get("name", src["id"]), "url": src["url"],
                           "tier": src.get("tier", "unknown"), "region": src.get("region", "global"),
                           "feed_url": None, "status": "error", "items": 0, "items_24h": 0, "newest_age_h": None,
                           "has_summaries": False, "sample_titles": [], "fixture_ref": None, "error": None,
                           "links_on_page": 0}
    r = await fetcher.get(src["url"], purpose=f"probe:{src['id']}")
    if r.error and "robots" in r.error:
        rep.update(status="robots_disallowed", error=r.error)
        return rep
    if not r.ok:
        rep.update(status="unreachable", error=r.error)
        return rep
    data, url = r.content, src["url"]
    if not looks_like_feed(data):
        feeds = discover_feeds(data, src["url"])
        if feeds:
            r2 = await fetcher.get(feeds[0], purpose=f"probe:{src['id']}:discovered")
            if r2.ok and looks_like_feed(r2.content):
                data, url, r = r2.content, feeds[0], r2
                rep["feed_url"] = feeds[0]
        if not looks_like_feed(data):
            links = len(re.findall(r"<a\s[^>]*href=", data.decode("utf-8", "replace"), re.I))
            rep.update(status="needs_adapter" if links >= 5 else "not_a_feed", fixture_ref=r.ref, links_on_page=links,
                       error=None if links >= 5 else "no feed and too few links")
            return rep
    try:
        _, items = parse_feed(data, base_url=url)
    except FeedError as e:
        rep.update(status="parse_error", error=str(e)[:200], fixture_ref=r.ref)
        return rep
    dates = [datetime.fromisoformat(i.published) for i in items if i.published]
    newest = max(dates) if dates else None
    rep.update(
        status="ok" if items else "no_items", items=len(items), fixture_ref=r.ref,
        items_24h=sum(1 for d in dates if now - d <= timedelta(hours=24)),
        newest_age_h=round((now - newest).total_seconds() / 3600, 1) if newest else None,
        has_summaries=sum(1 for i in items if len(i.summary) > 40) >= max(1, len(items) // 2),
        sample_titles=[i.title[:120] for i in items[:5]],
    )
    if newest and now - newest > timedelta(days=3):
        rep["status"] = "stale"
    if url != src["url"]:
        rep["feed_url"] = url
    return rep


# --- clustering, classification, ranking --------------------------------------------

def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def fingerprint(toks: Iterable[str]) -> str:
    return sha256_hex(" ".join(sorted(toks)[:12]))[:12]


def similarity(a: tuple[set[str], set[str]], b: tuple[set[str], set[str]]) -> float:
    """max(Jaccard of title+lead tokens, 0.6 x overlap coefficient of titles sharing >= 3 tokens).

    The overlap term catches the same story told with different verbs
    ("inflation slows to 2.9%" / "inflation cools to 2.9%") without merging
    stories that only share a country name.
    """
    j = jaccard(a[0], b[0])
    ta, tb = a[1], b[1]
    shared = len(ta & tb)
    ov = shared / min(len(ta), len(tb)) if shared >= 3 and ta and tb else 0.0
    return max(j, 0.6 * ov)


def cluster(items: list[Item], threshold: float) -> list[Event]:
    """Greedy single-link clustering; primary sources seed the clusters."""
    def key(it: Item) -> tuple[set[str], set[str]]:
        title = tokens(it.title)
        return title | tokens(" ".join(it.summary.split()[:12])), title

    ordered = sorted(items, key=lambda i: (i.tier != "primary", i.published or ""))
    groups: list[tuple[list[Item], list[tuple[set[str], set[str]]]]] = []
    for it in ordered:
        k = key(it)
        best, best_sim = None, 0.0
        for g in groups:
            sim = max(similarity(k, other) for other in g[1])
            if sim > best_sim:
                best, best_sim = g, sim
        if best is not None and best_sim >= threshold:
            best[0].append(it)
            best[1].append(k)
        else:
            groups.append(([it], [k]))
    events = []
    for members, keys in groups:
        titles = [k[1] for k in keys]
        common = set.intersection(*titles) if len(titles) > 1 else titles[0]
        seed = sorted(common or titles[0])
        events.append(Event(id=fingerprint(seed + [m.id for m in members][:1]), items=members, tokens=seed))
    return events


def classify_rules(event: Event, topics: list[TopicRule]) -> dict[str, float]:
    return topic_hits(" ".join(i.snippet() for i in event.items), topics)


def topic_hits(text: str, topics: list[TopicRule]) -> dict[str, float]:
    text = text.lower()
    hits: dict[str, float] = {}
    for t in topics:
        n = 0
        for kw in t.keywords:
            kw = kw.lower().strip()
            if kw and re.search(r"(?<![a-z0-9])" + re.escape(kw), text):
                n += 1
        if n:
            hits[t.name] = round(t.weight * (1 + math.log2(n)), 3)
    return hits


def mark_continuing(events: list[Event], history: list[set[str]], threshold: float = 0.6) -> None:
    for e in events:
        tk = set(e.tokens) or tokens(e.items[0].title)
        e.continuing = any(jaccard(tk, h) >= threshold for h in history)


def rank(events: list[Event], spec: DigestSpec, window_start: datetime, window_end: datetime) -> list[Event]:
    r: RankingSpec = spec.ranking
    span = max((window_end - window_start).total_seconds(), 3600.0)
    weights = {s.id: s.weight for s in spec.sources}
    for e in events:
        tier = max(r.tier_weights.get(i.tier, 0.3) * weights.get(i.source_id, 1.0) for i in e.items)
        coverage = math.log2(1 + e.independent)
        topic = max(e.topics.values()) if e.topics else 0.0
        if e.relevance is not None:
            topic = max(topic, e.relevance / 3 * 1.5) if e.relevance > 0 else 0.0
        dates = [datetime.fromisoformat(i.published) for i in e.items if i.published]
        recency = 0.0
        if dates:
            newest = max(d if d.tzinfo else d.replace(tzinfo=timezone.utc) for d in dates)
            recency = max(0.0, 1 - (window_end - newest).total_seconds() / span)
        score = r.coverage_weight * coverage + r.topic_weight * topic + tier + r.recency_weight * recency
        if not e.topics and (e.relevance is None or e.relevance == 0):
            score *= 0.3  # nothing economic detected
        if e.continuing:
            score *= r.continuing_penalty
        e.score = score
    return sorted(events, key=lambda e: e.score, reverse=True)


# --- gates ---------------------------------------------------------------------------

def numbers_in(text: str) -> list[str]:
    return _NUM.findall(text)


def _norm(n: str) -> float | None:
    s = n.replace(",", "").lstrip("+")
    try:
        return float(s)
    except ValueError:
        return None


def allowed_numbers(texts: Iterable[str], quotes: Iterable[Quote]) -> list[float]:
    vals: list[float] = []
    for t in texts:
        vals += [v for v in (_norm(n) for n in numbers_in(t)) if v is not None]
    for q in quotes:
        vals += [q.value, round(q.value, q.decimals)]
        if q.previous is not None:
            vals.append(round(q.previous, q.decimals))
        if q.change_pct is not None:
            vals += [round(q.change_pct, 1), round(q.change_pct, 2), round(abs(q.change_pct), 1), round(abs(q.change_pct), 2)]
    return vals


def unsupported_numbers(text: str, allowed: list[float]) -> list[str]:
    bad = []
    for n in numbers_in(text):
        v = _norm(n)
        if v is None:
            continue
        if float(v).is_integer() and 1990 <= v <= 2100:
            continue  # a year
        if float(v).is_integer() and 0 <= v <= 10 and "." not in n:
            continue  # small counts ("two", "3 banks") carry little risk
        if not any(abs(v - a) <= max(0.005, abs(a) * 0.001) for a in allowed):
            bad.append(n)
    return bad


def drop_unsupported_sentences(text: str, allowed: list[float]) -> tuple[str, list[str]]:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept, dropped = [], []
    for s in sentences:
        bad = unsupported_numbers(s, allowed)
        (dropped if bad else kept).append(s)
    return " ".join(kept).strip(), dropped


# --- rendering ---------------------------------------------------------------------

def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Morning brief — {report['date']}", "",
             f"_Window: {report['window_start']} → {report['window_end']} · {report['stats']['sources_ok']} sources · "
             f"{report['stats']['items']} items · {report['stats']['events']} events_", ""]
    if report.get("one_line"):
        lines += [f"**In one line:** {report['one_line']}", ""]
    lines.append("## What happened and why it matters")
    for i, e in enumerate(report["events"], 1):
        cont = " _(continuing story)_" if e.get("continuing") else ""
        lines += ["", f"### {i}. {e['headline']}{cont}", "", e["summary"]]
        if e.get("why"):
            lines += ["", f"**Why it matters:** {e['why']}"]
        if e.get("channels"):
            lines.append(f"**Channels:** {', '.join(e['channels'])} · **Confidence:** {e.get('confidence', 'n/a')}")
        lines.append("Sources: " + " · ".join(f"[{s['name']}]({s['link']})" for s in e["sources"]))
    if report.get("markets"):
        lines += ["", "## Markets (data, not model output)", "", "| Instrument | Last | Change | As of | Provider |", "|---|---|---|---|---|"]
        for q in report["markets"]:
            lines.append(f"| {q['label']} | {q['value']} | {q['change']} | {q['as_of'] or ''} | {q['provider']} |")
    if report.get("watch"):
        lines += ["", "## Watch today", ""] + [f"- {w}" for w in report["watch"]]
    if report.get("notes"):
        lines += ["", "## Quality notes", ""] + [f"- {n}" for n in report["notes"]]
    return "\n".join(lines) + "\n"


def render_telegram(report: dict[str, Any], max_events: int) -> str:
    esc = html.escape
    out = [f"<b>Morning brief {esc(report['date'])}</b>"]
    if report.get("one_line"):
        out.append(esc(report["one_line"]))
    for i, e in enumerate(report["events"][:max_events], 1):
        src = ", ".join(f'<a href="{esc(s["link"], quote=True)}">{esc(s["name"])}</a>' for s in e["sources"][:3])
        why = f"\n<i>{esc(e['why'])}</i>" if e.get("why") else ""
        out.append(f"<b>{i}. {esc(e['headline'])}</b>\n{esc(e['summary'])}{why}\n{src}")
    if report.get("markets"):
        out.append("\n".join(f"{esc(q['label'])}: {esc(q['value'])} ({esc(q['change'])})" for q in report["markets"]))
    if report.get("watch"):
        out.append("<b>Watch today</b>\n" + "\n".join(f"• {esc(w)}" for w in report["watch"]))
    return "\n\n".join(out)


# --- learning from teacher labels (bootstrap) -------------------------------------------

def rule_metrics(topics: list[TopicRule], cases: list[dict[str, Any]], relevant_at: int = 2) -> dict[str, Any]:
    """Score keyword rules against teacher labels. score = mean(macro F1 over topics, relevance F1)."""
    names = [t.name for t in topics]
    tp = {n: 0 for n in names}
    fp = dict(tp)
    fn = dict(tp)
    rel = {"tp": 0, "fp": 0, "fn": 0}
    errors = []
    for c in cases:
        pred = set(topic_hits(c["text"], topics))
        gold = set(c.get("topics", [])) & set(names)
        for n in names:
            if n in pred and n in gold:
                tp[n] += 1
            elif n in pred:
                fp[n] += 1
            elif n in gold:
                fn[n] += 1
        pr, gr = bool(pred), c.get("relevance", 0) >= relevant_at
        key = "tp" if pr and gr else "fp" if pr else "fn" if gr else "tn"
        rel[key] = rel.get(key, 0) + 1
        if pred != gold or pr != gr:
            errors.append({"case_id": c.get("case_id"), "text": c["text"][:300], "gold": sorted(gold), "predicted": sorted(pred),
                           "relevance": c.get("relevance", 0)})

    def f1(t: int, f_p: int, f_n: int) -> float:
        return 2 * t / (2 * t + f_p + f_n) if (2 * t + f_p + f_n) else 1.0

    per = {n: round(f1(tp[n], fp[n], fn[n]), 4) for n in names if tp[n] + fp[n] + fn[n] > 0}
    macro = sum(per.values()) / len(per) if per else 0.0
    rel_f1 = f1(rel["tp"], rel["fp"], rel["fn"])
    return {"score": round((macro + rel_f1) / 2, 4), "macro_f1": round(macro, 4), "relevance_f1": round(rel_f1, 4),
            "per_topic": per, "cases": len(cases), "errors": errors}


def ndcg(relevances: list[float], k: int = 6) -> float:
    def dcg(rs: list[float]) -> float:
        return sum((2 ** r - 1) / math.log2(i + 2) for i, r in enumerate(rs[:k]))

    ideal = dcg(sorted(relevances, reverse=True))
    return dcg(relevances) / ideal if ideal else 0.0


def calibrate_ranking(events: list[Event], teacher: dict[str, float], spec: DigestSpec,
                      window_start: datetime, window_end: datetime, k: int = 6) -> dict[str, Any]:
    """Grid-search ranking weights to agree with the teacher's event relevance (NDCG@k). Zero tokens."""
    def rel(e: Event) -> float:
        return max((teacher.get(i.id, 0.0) for i in e.items), default=0.0)

    def score(weights: dict[str, float]) -> float:
        s = spec.model_copy(update={"ranking": spec.ranking.model_copy(update=weights)})
        return ndcg([rel(e) for e in rank([Event.from_dict(e.as_dict()) for e in events], s, window_start, window_end)], k)

    base = {f: getattr(spec.ranking, f) for f in ("coverage_weight", "topic_weight", "recency_weight")}
    before = score(base)
    best, best_score = base, before
    for cw in (0.5, 1.0, 1.5, 2.0):
        for tw in (0.5, 1.0, 1.5, 2.0):
            for rw in (0.0, 0.25, 0.5, 1.0):
                w = {"coverage_weight": cw, "topic_weight": tw, "recency_weight": rw}
                sc = score(w)
                if sc > best_score + 1e-9:
                    best, best_score = w, sc
    return {"weights": best, "ndcg_before": round(before, 4), "ndcg_after": round(best_score, 4), "events": len(events), "k": k}
