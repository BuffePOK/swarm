import asyncio

import pytest

from roj.knowledge.packs import KnowledgeBase, chunk_text

DOCS = {
    "ecb-policy": "# ECB monetary policy\n\nThe Governing Council sets the deposit facility rate. "
                  "Decisions are announced every six weeks.\n\n## Instruments\n\nOpen market operations and standing facilities.",
    "oil-market": "# Oil market basics\n\nBrent and WTI are benchmark crude grades. OPEC decisions move supply.",
    "fx-guide": "# Currency markets\n\nThe euro dollar pair is the most traded exchange rate. Yen weakness matters for exporters.",
    "coffee": "# Cafe guide\n\nThe best espresso bars in town, ranked by our editors.",
}


def run(c):
    return asyncio.run(c)


def test_chunking_by_sections_with_overlap():
    text = "# A\n\n" + "First sentence. " * 60 + "\n\n# B\n\nShort part."
    chunks = chunk_text(text, max_chars=300, overlap=0.12)
    assert {c.section for c in chunks} == {"A", "B"} and all(len(c.text) <= 340 for c in chunks)
    assert " … " in chunks[1].text  # overlap tail carried into the next chunk


def _kb(project, embed=True):
    kb = KnowledgeBase(project)
    kb.create("macro", "Notes about central banks, energy and FX for the morning brief", embed_model="fake-embed" if embed else None)
    for doc_id, text in DOCS.items():
        run(kb.add_text("macro", doc_id, doc_id, text, source=f"{doc_id}.md"))
    return kb


def test_hybrid_search_finds_the_right_document(project, gateway):
    kb = _kb(project)
    hits = run(kb.search("macro", "deposit facility rate decision", k=3))
    assert hits[0].doc_id == "ecb-policy" and set(hits[0].via) == {"bm25", "vector"}
    assert "From 'ecb-policy'" in hits[0].context
    assert run(kb.add_text("macro", "oil-market", "oil-market", DOCS["oil-market"])) == 0  # unchanged doc is skipped
    run(kb.add_text("macro", "oil-market", "oil-market", DOCS["oil-market"] + " Prices are quoted per barrel."))
    n_fts = project.store.one("SELECT COUNT(*) n FROM kb_fts WHERE pack = 'macro'")["n"]
    n_chunks = project.store.one("SELECT COUNT(*) n FROM kb_chunks WHERE pack = 'macro'")["n"]
    assert n_fts == n_chunks  # replaced, not duplicated


def test_rewrite_rerank_and_contextualize(project, gateway):
    kb = _kb(project, embed=False)
    run(kb.add_text("macro", "boj", "boj", "# Bank of Japan\n\nYield curve control ended. The yen reacts to rate signals.",
                    contextualize=True))
    assert "Bank of Japan" in project.store.one("SELECT context FROM kb_chunks WHERE doc_id = 'boj'")["context"]
    hits = run(kb.search("macro", "what moves the yen exchange rate", k=3, rewrite=True, rerank=True))
    assert hits[0].doc_id in ("fx-guide", "boj") and "rerank" in hits[0].via


def test_recall_eval_is_gated(project, gateway):
    kb = _kb(project)
    kb.add_question("macro", "When does the Governing Council decide?", ["ecb-policy"])
    kb.add_question("macro", "benchmark crude grades", ["oil-market"])
    res = run(kb.evaluate("macro", k=2))
    assert res["recall@2"] == 1.0 and res["passed"] and res["threshold"] == pytest.approx(0.98)


def test_pure_python_bm25_fallback(project, gateway):
    kb = _kb(project, embed=False)
    kb.fts = False
    assert run(kb.search("macro", "OPEC supply Brent", k=1))[0].doc_id == "oil-market"
