"""Knowledge packs: chunked documents with hybrid retrieval (concept §8, owner's RAG notes).

- chunking by headings and paragraphs, ~12% overlap, a context line per chunk
  ("from <doc>, section <s>: <doc summary>"); the summary can be written by a cheap model
  once per document (contextual retrieval) instead of once per chunk
- BM25 through SQLite FTS5 (no extra dependency; pure-Python fallback), embeddings through
  the gateway's /embeddings, fused with reciprocal rank fusion
- optional query rewriting and reranking by models, original query always included
- test questions per pack, recall@k measured by code and guarded by the ratchet
"""

from __future__ import annotations

import json
import math
import re
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from ..config import ModelBinding
from ..contracts import BudgetSpec, TaskSpec
from ..prompting.compiler import Document, RoleSpec
from ..util import canonical_json, sha256_hex, utcnow_iso

if TYPE_CHECKING:
    from ..project import Project

_WORDS = re.compile(r"\w{2,}", re.UNICODE)
RRF_K = 60


# --- chunking ------------------------------------------------------------------------

@dataclass
class Chunk:
    ord: int
    section: str
    text: str


def chunk_text(text: str, *, max_chars: int = 1500, overlap: float = 0.12) -> list[Chunk]:
    sections: list[tuple[str, list[str]]] = [("", [])]
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        b = block.strip()
        if not b:
            continue
        m = re.match(r"^(#{1,6})\s+(.+)", b)
        if m:
            title = m.group(2).strip()
            rest = b[m.end():].strip()
            sections.append((title, [rest] if rest else []))
        else:
            sections[-1][1].append(b)
    chunks: list[Chunk] = []
    tail = ""
    for title, paras in sections:
        buf = ""
        for para in paras:
            while len(para) > max_chars:  # a paragraph longer than a chunk: cut at sentence ends
                cut = para.rfind(". ", 0, max_chars)
                cut = cut + 1 if cut > max_chars // 3 else max_chars
                paras_head, para = para[:cut], para[cut:].strip()
                if buf:
                    chunks.append(Chunk(len(chunks), title, (tail + buf).strip()))
                    tail, buf = _tail(buf, max_chars, overlap), ""
                chunks.append(Chunk(len(chunks), title, (tail + paras_head).strip()))
                tail = _tail(paras_head, max_chars, overlap)
            if buf and len(buf) + len(para) + 2 > max_chars:
                chunks.append(Chunk(len(chunks), title, (tail + buf).strip()))
                tail, buf = _tail(buf, max_chars, overlap), ""
            buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            chunks.append(Chunk(len(chunks), title, (tail + buf).strip()))
            tail = _tail(buf, max_chars, overlap)
    return chunks


def _tail(text: str, max_chars: int, overlap: float) -> str:
    n = int(max_chars * overlap)
    if n <= 0 or len(text) <= n:
        return ""
    t = text[-n:]
    dot = t.find(". ")
    return (t[dot + 2:] if 0 <= dot < len(t) - 10 else t) + " … "


# --- model slots -----------------------------------------------------------------------

class DocSummary(BaseModel):
    summary: str = Field(min_length=5, max_length=400)


class Rewrites(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=3)


class RerankScore(BaseModel):
    chunk_id: int
    score: int = Field(ge=0, le=3)


class Rerank(BaseModel):
    scores: list[RerankScore]


ROLES = {
    "librarian": "You write one or two sentences saying what a document is about and what it is useful for, so that each of its "
                 "chunks can be found by search. No opinions, no new facts.",
    "rewriter": "You turn a vague question into 1-3 precise search queries for the described corpus: use its vocabulary, names "
                "and codes. Keep the user's intent; do not answer the question.",
    "reranker": "You judge how well each chunk answers the query: 0 irrelevant, 1 related, 2 partly answers, 3 answers. "
                "Judge only from the chunk text.",
    "examiner": "You write short-answer exam questions about the specific facts of a document: figures, names, internal terms, "
                "decisions. Each question must make sense to someone who has never seen the document.",
    "closed-book": "You answer exam questions from your own knowledge only. If you do not know an answer you write unknown.",
}


def _role_binding(project: "Project", *roles: str) -> ModelBinding:
    for r in roles:
        try:
            return project.settings.binding_for(r)
        except KeyError:
            continue
    raise KeyError(f"bind one of these roles in roj.toml: {', '.join(roles)}")


async def _slot(project: "Project", role: str, output: type[BaseModel], task: TaskSpec, docs: list[Document],
                binding: ModelBinding) -> BaseModel:
    from ..core.kernel import StepContext
    from ..skills.slots import Slot, run_slot

    ctx = StepContext(project, None, None, f"kb:{role}", 0, 1, {}, {})  # type: ignore[arg-type]
    slot = Slot(role, RoleSpec(role, ROLES[role]), output, max_tokens=1200)
    res = await run_slot(ctx, slot, task, binding=binding, scope=f"role:{role}", documents=docs)
    return res.value


# --- the store -----------------------------------------------------------------------------

@dataclass
class Hit:
    chunk_id: int
    doc_id: str
    title: str
    section: str
    text: str
    context: str
    source: str
    score: float
    via: list[str] = field(default_factory=list)

    def as_document(self) -> Document:
        return Document(f"{self.doc_id}#{self.chunk_id}", self.text, self.source or self.title)


class KnowledgeBase:
    def __init__(self, project: "Project"):
        self.p = project
        self.store = project.store
        self.fts = self._ensure_fts()

    def _ensure_fts(self) -> bool:
        try:
            self.store.run("CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(text, context, pack UNINDEXED, "
                           "tokenize='unicode61 remove_diacritics 2')")
            return True
        except Exception:  # noqa: BLE001 - SQLite built without FTS5: pure-Python BM25
            return False

    # --- packs -----------------------------------------------------------------------
    def create(self, name: str, description: str, *, level: str = "project", embed_model: str | None = None,
               chunk_chars: int = 1500, overlap: float = 0.12, audience: list[str] | None = None) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_\-.]{0,60}", name):
            raise ValueError("pack name: lowercase letters, digits, '-', '_', '.'")
        manifest = {"name": name, "description": description, "level": level, "embed_model": embed_model,
                    "chunk_chars": chunk_chars, "overlap": overlap, "questions": [], "version": 1,
                    "audience": sorted(audience or ["*"])}  # roles that share this pack; "*" = everyone in the project
        self.store.run("INSERT INTO kb_packs(pack, manifest, created, updated) VALUES(?,?,?,?)",
                       (name, canonical_json(manifest), utcnow_iso(), utcnow_iso()))
        self.p.events.append("kb.created", {"pack": name, "level": level, "embed_model": embed_model})
        return manifest

    def manifest(self, name: str) -> dict[str, Any]:
        row = self.store.one("SELECT manifest FROM kb_packs WHERE pack = ?", (name,))
        if not row:
            raise KeyError(f"no knowledge pack {name!r}")
        return json.loads(row["manifest"])

    def _save_manifest(self, m: dict[str, Any]) -> None:
        m["version"] = m.get("version", 1) + 1
        self.store.run("UPDATE kb_packs SET manifest = ?, updated = ? WHERE pack = ?", (canonical_json(m), utcnow_iso(), m["name"]))

    def packs(self) -> list[dict[str, Any]]:
        out = []
        for r in self.store.all("SELECT pack, manifest FROM kb_packs ORDER BY pack"):
            m = json.loads(r["manifest"])
            n = self.store.one("SELECT COUNT(DISTINCT doc_id) d, COUNT(*) c FROM kb_chunks WHERE pack = ?", (r["pack"],))
            out.append({"pack": r["pack"], "level": m["level"], "docs": n["d"], "chunks": n["c"], "questions": len(m["questions"]),
                        "embed_model": m.get("embed_model"), "description": m["description"][:80]})
        return out

    # --- ingestion ---------------------------------------------------------------------
    async def add_text(self, pack: str, doc_id: str, title: str, text: str, *, source: str = "",
                       contextualize: bool = False, novelty: bool = False) -> int:
        m = self.manifest(pack)
        sha = sha256_hex(text)
        row = self.store.one("SELECT sha256 FROM kb_docs WHERE pack = ? AND doc_id = ?", (pack, doc_id))
        if row and row["sha256"] == sha:
            return 0  # unchanged
        nov = None
        if novelty:
            from .novelty import measure_novelty

            nov = await measure_novelty(self.p, title, text, audience=m.get("audience"))
            if nov["score"] < self.p.settings.knowledge.novelty_threshold:
                self.store.run("INSERT OR REPLACE INTO kb_docs(pack, doc_id, title, source, sha256, meta, created) VALUES(?,?,?,?,?,?,?)",
                               (pack, doc_id, title, source, sha, canonical_json({"skipped": "known", "novelty": nov["score"]}), utcnow_iso()))
                self.p.events.append("kb.skipped_known", {"pack": pack, "doc_id": doc_id, **nov})
                return 0  # the consuming model already knows this: storing it would only cost tokens at every retrieval
        summary = ""
        if contextualize:
            task = TaskSpec(goal=f"Summarise what the document '{title}' is about for search", deliverable="DocSummary JSON",
                            acceptance=["summary has at most 2 sentences", "summary contains no facts absent from the document"],
                            output_format="JSON only", budget=BudgetSpec(tokens=6000))
            v = await _slot(self.p, "librarian", DocSummary, task, [Document(doc_id, text[:6000], source)],
                            _role_binding(self.p, "librarian", "classifier", "analyst"))
            summary = v.summary
        chunks = chunk_text(text, max_chars=m["chunk_chars"], overlap=m["overlap"])
        contexts = [f"From '{title}'" + (f", section '{c.section}'" if c.section else "") + (f": {summary}" if summary else ".")
                    for c in chunks]
        vectors: list[list[float]] | None = None
        if m.get("embed_model") and chunks:
            from ..models.caller import embed

            vectors, _ = await embed(self.p.caller, binding=ModelBinding(model=m["embed_model"]),
                                     inputs=[f"{ctx} {c.text}" for ctx, c in zip(contexts, chunks)], agent="librarian",
                                     scope="role:librarian")
        with self.store.tx() as c:
            old = [r["id"] for r in c.execute("SELECT id FROM kb_chunks WHERE pack = ? AND doc_id = ?", (pack, doc_id)).fetchall()]
            if old and self.fts:
                c.executemany("DELETE FROM kb_fts WHERE rowid = ?", [(i,) for i in old])
            c.execute("DELETE FROM kb_chunks WHERE pack = ? AND doc_id = ?", (pack, doc_id))
            c.execute("INSERT OR REPLACE INTO kb_docs(pack, doc_id, title, source, sha256, meta, created) VALUES(?,?,?,?,?,?,?)",
                      (pack, doc_id, title, source, sha, canonical_json({"summary": summary, **({"novelty": nov["score"]} if nov else {})}), utcnow_iso()))
            for k, ch in enumerate(chunks):
                blob = array("f", vectors[k]).tobytes() if vectors else None
                cur = c.execute("INSERT INTO kb_chunks(pack, doc_id, ord, section, text, context, sha256, vector) VALUES(?,?,?,?,?,?,?,?)",
                                (pack, doc_id, ch.ord, ch.section, ch.text, contexts[k], sha256_hex(ch.text), blob))
                if self.fts:
                    c.execute("INSERT INTO kb_fts(rowid, text, context, pack) VALUES(?,?,?,?)", (cur.lastrowid, ch.text, contexts[k], pack))
            self.p.events.append("kb.added", {"pack": pack, "doc_id": doc_id, "chunks": len(chunks), "contextualized": bool(summary),
                                              "embedded": bool(vectors)})
        return len(chunks)

    async def add_file(self, pack: str, path: Path, **kw: Any) -> int:
        path = Path(path)
        raw = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() in (".html", ".htm"):
            from ..hands.feeds import html_to_text

            raw = re.sub(r"(?is)<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "\n\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n\n", raw)
            raw = re.sub(r"(?i)</p>|<br\s*/?>", "\n\n", raw)
            raw = "\n\n".join(html_to_text(b, limit=None) for b in raw.split("\n\n"))
        elif path.suffix.lower() not in (".md", ".txt", ".markdown", ".rst"):
            raise ValueError(f"unsupported file type {path.suffix}: use .md, .txt or .html")
        return await self.add_text(pack, path.stem, path.stem.replace("_", " "), raw, source=str(path.name), **kw)

    # --- retrieval -----------------------------------------------------------------------
    def _bm25(self, pack: str, query: str, n: int) -> list[int]:
        terms = [t.lower() for t in _WORDS.findall(query)]
        if not terms:
            return []
        if self.fts:
            q = " OR ".join('"' + t.replace('"', "") + '"' for t in terms)
            rows = self.store.all("SELECT rowid FROM kb_fts WHERE kb_fts MATCH ? AND pack = ? ORDER BY bm25(kb_fts) LIMIT ?", (q, pack, n))
            return [r["rowid"] for r in rows]
        docs = [(r["id"], _WORDS.findall(f"{r['context']} {r['text']}".lower()))
                for r in self.store.all("SELECT id, text, context FROM kb_chunks WHERE pack = ?", (pack,))]
        if not docs:
            return []
        avg = sum(len(d) for _, d in docs) / len(docs)
        df = {t: sum(1 for _, d in docs if t in d) for t in set(terms)}
        scores = []
        for cid, d in docs:
            s = 0.0
            for t in terms:
                f = d.count(t)
                if f:
                    idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(d) / avg))
            if s > 0:
                scores.append((s, cid))
        return [cid for _, cid in sorted(scores, reverse=True)[:n]]

    async def _vector(self, pack: str, query: str, n: int, model: str) -> list[int]:
        from ..models.caller import embed

        [qv], _ = await embed(self.p.caller, binding=ModelBinding(model=model), inputs=[query], agent="librarian", scope="role:librarian")
        scored = []
        for r in self.store.all("SELECT id, vector FROM kb_chunks WHERE pack = ? AND vector IS NOT NULL", (pack,)):
            v = array("f")
            v.frombytes(r["vector"])
            dot = sum(a * b for a, b in zip(qv, v))
            norm = math.sqrt(sum(a * a for a in v)) * math.sqrt(sum(b * b for b in qv)) or 1.0
            scored.append((dot / norm, r["id"]))
        return [cid for _, cid in sorted(scored, reverse=True)[:n]]

    async def search(self, pack: str, query: str, *, k: int = 6, candidates: int = 40, rewrite: bool = False,
                     rerank: bool = False) -> list[Hit]:
        m = self.manifest(pack)
        queries = [query]
        if rewrite:
            vocab = self._vocabulary(pack)
            task = TaskSpec(goal="Rewrite the question into precise search queries for this corpus", deliverable="Rewrites JSON",
                            acceptance=["1 to 3 queries", "each query uses words from the corpus vocabulary"],
                            output_format="JSON only", inputs={"question": query}, budget=BudgetSpec(tokens=4000))
            v = await _slot(self.p, "rewriter", Rewrites, task,
                            [Document("corpus", canonical_json({"description": m["description"], "vocabulary": vocab}), pack)],
                            _role_binding(self.p, "rewriter", "classifier", "analyst"))
            queries += [q for q in v.queries if q.strip() and q != query]
        fused: dict[int, float] = {}
        via: dict[int, set[str]] = {}
        for q in queries:
            lists = [("bm25", self._bm25(pack, q, candidates))]
            if m.get("embed_model"):
                lists.append(("vector", await self._vector(pack, q, candidates, m["embed_model"])))
            for name, ids in lists:
                for rank, cid in enumerate(ids):
                    fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                    via.setdefault(cid, set()).add(name)
        top = sorted(fused, key=lambda c: fused[c], reverse=True)[:candidates]
        hits = self._hits(top, fused, via)
        if rerank and hits:
            task = TaskSpec(goal="Score how well each chunk answers the query", deliverable="Rerank JSON",
                            acceptance=["one score per chunk_id", "score is an integer from 0 to 3"],
                            output_format="JSON only", inputs={"query": query}, budget=BudgetSpec(tokens=12000))
            docs = [Document(str(h.chunk_id), h.text[:800], h.title) for h in hits[:20]]
            v = await _slot(self.p, "reranker", Rerank, task, docs, _role_binding(self.p, "reranker", "classifier", "analyst"))
            boost = {s.chunk_id: s.score for s in v.scores}
            for h in hits:
                if h.chunk_id in boost:
                    h.score = boost[h.chunk_id] + h.score  # model score first, fusion breaks ties
                    h.via.append("rerank")
            hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def _hits(self, ids: list[int], scores: dict[int, float], via: dict[int, set[str]]) -> list[Hit]:
        if not ids:
            return []
        rows = {r["id"]: r for r in self.store.all(
            f"SELECT c.id, c.doc_id, c.section, c.text, c.context, d.title, d.source FROM kb_chunks c "
            f"JOIN kb_docs d ON d.pack = c.pack AND d.doc_id = c.doc_id WHERE c.id IN ({','.join('?' * len(ids))})", tuple(ids))}
        return [Hit(i, rows[i]["doc_id"], rows[i]["title"], rows[i]["section"] or "", rows[i]["text"], rows[i]["context"] or "",
                    rows[i]["source"] or "", round(scores[i], 6), sorted(via.get(i, set()))) for i in ids if i in rows]

    def _vocabulary(self, pack: str, n: int = 60) -> list[str]:
        counts: dict[str, int] = {}
        for r in self.store.all("SELECT text FROM kb_chunks WHERE pack = ? LIMIT 400", (pack,)):
            for w in _WORDS.findall(r["text"].lower()):
                if len(w) > 3:
                    counts[w] = counts.get(w, 0) + 1
        return [w for w, _ in sorted(counts.items(), key=lambda x: -x[1])[:n]]

    # --- evaluation -------------------------------------------------------------------------
    def add_question(self, pack: str, question: str, expected_doc_ids: list[str]) -> None:
        m = self.manifest(pack)
        m["questions"].append({"q": question, "expected": expected_doc_ids})
        self._save_manifest(m)
        from ..verify.ratchet import Ratchet

        Ratchet(self.p).add_cases(f"kb.{pack}", [{"q": question, "expected": expected_doc_ids}], source="owner")

    async def evaluate(self, pack: str, *, k: int = 5, rewrite: bool = False) -> dict[str, Any]:
        from ..verify.ratchet import Ratchet

        rat = Ratchet(self.p)
        cases = rat.cases(f"kb.{pack}")
        if not cases:
            raise ValueError(f"pack {pack} has no test questions: add some with `roj kb question`")
        misses, found = [], 0
        for c in cases:
            hits = await self.search(pack, c["q"], k=k, rewrite=rewrite)
            got = {h.doc_id for h in hits}
            if got & set(c["expected"]):
                found += 1
            else:
                misses.append({"q": c["q"], "expected": c["expected"], "got": sorted(got)})
        recall = found / len(cases)
        gate = rat.evaluate(f"kb.{pack}", f"recall@{k}", recall)
        return {"pack": pack, f"recall@{k}": round(recall, 4), "questions": len(cases), "misses": misses,
                "threshold": gate.threshold, "passed": gate.passed, "fts5": self.fts}
