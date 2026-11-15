"""Deterministic stand-ins for models, served by the fake gateway.

`fake-smart` / `fake-smart-b` play every morning-brief role well enough to drive the
whole pipeline offline, including deliberate mistakes the gates must catch:
a hallucinated extra source, an invented number, an unverifiable claim.
`fake-sloppy` returns invalid JSON first and valid JSON after a repair message.
They are test fixtures, not models: real runs use the owner's gateway.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urlsplit

TOPICS = [
    {"name": "monetary-policy", "keywords": ["central bank", "policy rate", "rate cut", "interest rate", "policymakers", "holds"], "weight": 1.5},
    {"name": "inflation", "keywords": ["inflation", "consumer prices", "cpi", "prices rose"], "weight": 1.4},
    {"name": "energy", "keywords": ["oil", "crude", "brent", "gas", "barrels", "power prices"], "weight": 1.3},
    {"name": "equities", "keywords": ["shares", "index", "rally", "stocks", "equities"], "weight": 1.0},
    {"name": "fx", "keywords": ["currency", "per dollar", "exchange rate", "weakened", "yen"], "weight": 1.1},
    {"name": "industry-trade", "keywords": ["factory", "industrial production", "exports", "tariff", "trade"], "weight": 1.2},
    {"name": "regulation", "keywords": ["regulators", "capital buffer", "financial stability"], "weight": 0.9},
]

# The planner's first draft is deliberately weaker than the teacher: missing keywords and one
# false-positive keyword ("best"), so rule induction has something measurable to fix.
DRAFT_TOPICS = [
    {"name": "monetary-policy", "keywords": ["central bank", "fomc", "basis points"], "weight": 1.5},
    {"name": "inflation", "keywords": ["cpi", "price index", "deflation"], "weight": 1.4},
    {"name": "energy", "keywords": ["opec", "natural gas", "petrol"], "weight": 1.3},
    {"name": "equities", "keywords": ["best", "stocks", "nasdaq"], "weight": 1.0},
    {"name": "fx", "keywords": ["forex", "fx market", "exchange rate"], "weight": 1.1},
    {"name": "industry-trade", "keywords": ["tariff", "pmi", "manufacturing"], "weight": 1.2},
    {"name": "regulation", "keywords": ["supervisor", "rulebook", "basel"], "weight": 0.9},
]
TRUTH = {t["name"]: t["keywords"] for t in TOPICS}

V1 = '''import re
from urllib.parse import urljoin


def parse(raw, base_url):
    items = []
    rx = r'<li class="story"><a href="([^"]+)">([^<]+)</a><time datetime="([^"]+)">[^<]*</time><p class="dek">([^<]*)</p></li>'
    for m in re.finditer(rx, raw):
        items.append({"title": m.group(2).strip(), "link": urljoin(base_url, m.group(1)),
                      "published": m.group(3), "summary": m.group(4).strip()})
    return items
'''

V2 = '''import re
from urllib.parse import urljoin


def parse(raw, base_url):
    items = []
    rx = r'<div class="card"><h3><a href="([^"]+)">([^<]+)</a></h3><span data-ts="([^"]+)"></span><p>([^<]*)</p></div>'
    for m in re.finditer(rx, raw):
        items.append({"title": m.group(2).strip(), "link": urljoin(base_url, m.group(1)),
                      "published": m.group(3), "summary": m.group(4).strip()})
    return items
'''


def _docs(text: str) -> dict[str, Any]:
    out = {}
    for did, body in re.findall(r'<document id="([^"]+)"[^>]*>\n(.*?)\n</document>', text, re.S):
        raw = html.unescape(body)
        try:
            out[did] = json.loads(raw)
        except json.JSONDecodeError:
            out[did] = raw
    return out


def _first_sentence(s: str) -> str:
    s = s.strip()
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    return (m.group(1) if m else s)[:300]


def _topics_for(text: str) -> list[str]:
    t = text.lower()
    return [r["name"] for r in TOPICS if any(k in t for k in r["keywords"])]


def respond(model: str, messages: list[dict[str, Any]]) -> str:
    system = next((m["content"] for m in messages if m.get("role") == "system"), "")
    user = "\n".join(m["content"] or "" for m in messages if m.get("role") == "user")
    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    repairing = last_user.startswith(("Twoja odpowiedź", "Your answer failed"))
    if model.startswith("fake-sloppy"):
        return '{"answer": "42"}' if repairing else 'Sure! {answer: 42'
    role = (re.search(r'<role name="([^"]+)"', system) or re.search(r'## ROLE \(name="([^"]+)"', system) or [None, ""])[1]
    whole = html.unescape(system + "\n" + user)
    case = re.search(r'"case_id":\s*"([a-z_]+:\d+)"', whole)
    if case:
        return _eval(model, whole, case.group(1))
    if role == "summarizer":
        return "DONE: earlier steps summarised.\nFACTS: see sources.\nOPEN: continue the task."
    docs = _docs(user)
    fn = {"news-planner": _planner, "news-critic": _critic, "adapter-coder": _coder, "news-classifier": _classifier,
          "news-analyst": _analyst, "news-checker": _checker, "news-scout": _scout, "news-assessor": _assessor,
          "rule-smith": _rule_smith, "librarian": _librarian, "rewriter": _rewriter, "reranker": _reranker,
          "orchestrator": _lead, "guardian": _guardian, "lab-researcher": _lab_researcher, "examiner": _examiner,
          "closed-book": _closed_book, "selfdev-planner": _selfdev_planner, "selfdev-reviewer": _selfdev_reviewer, "mission-intake": _mission_intake,
          "mission-architect": _mission_plan, "mission-critic": _mission_critic, "mission-review": _mission_review,
          "mission-chat": _mission_chat}.get(role)
    if role in ("researcher", "writer", "verifier", "analyst", "coder", "critic", "repo-coder") or role.startswith("w-"):
        return json.dumps(_agent(role, user, messages))
    if fn is None:
        return json.dumps({"answer": "unknown role"})
    inputs = re.search(r'(?:Dane wejściowe|Inputs): (\{.*\})', html.unescape(user))
    docs["_inputs"] = json.loads(inputs.group(1)) if inputs else {}
    return json.dumps(fn(docs))


def _planner(docs: dict[str, Any]) -> dict[str, Any]:
    report = docs["probe_report"]
    sources = report["sources"]
    usable = [s for s in sources if s["status"] in ("ok", "needs_adapter")]
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(sources[0]["url"])) if sources else "https://example.invalid"
    revising = "critique" in docs
    return {
        "selected": [{"source_id": s["source_id"], "weight": {"primary": 1.5, "secondary": 1.0}.get(s["tier"], 0.7),
                      "tier": s["tier"], "region": s["region"], "topics": [],
                      "reason": f"probe {s['status']} with {s['items']} items"} for s in usable],
        "rejected": [{"source_id": s["source_id"], "reason": f"probe status {s['status']}"} for s in sources if s not in usable],
        "gaps": ["Latin America"] if not revising else [],
        "extra_candidates": [] if revising else [
            {"name": "Imaginary Times", "url": f"{origin}/imaginary/rss.xml", "why": "Latin America coverage",
             "tier": "secondary", "region": "latam"}],
        "topics": DRAFT_TOPICS,
        "instruments": [{"instrument_id": i["instrument_id"], "reason": "available"} for i in report.get("instruments", [])
                        if i["status"] == "ok"],
        "notes": ["revised after critique"] if revising else [],
    }


def _critic(docs: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": "revise", "issues": [
        {"issue": "Asia coverage depends on a single newsroom", "severity": "medium", "fix": "add a second Asian source when one passes the probe"},
        {"issue": "no Latin America source", "severity": "low", "fix": "probe a regional business feed"},
    ]}


def _coder(docs: dict[str, Any]) -> dict[str, Any]:
    page = docs.get("page_excerpt", "")
    page = page if isinstance(page, str) else json.dumps(page)
    return {"code": V2 if 'class="card"' in page else V1, "notes": "regex over the story list"}


def _classifier(docs: dict[str, Any]) -> dict[str, Any]:
    out = []
    for e in docs.get("events", []):
        text = " ".join(e.get("headlines", [])) + " " + e.get("summary", "")
        topics = _topics_for(text)
        out.append({"event_id": e["event_id"], "topics": topics, "relevance": 2 if topics else 0})
    return {"events": out}


def _analyst(docs: dict[str, Any]) -> dict[str, Any]:
    events = []
    for e in docs.get("events", []):
        items = e["items"]
        first = items[0]
        claim1 = _first_sentence(first["summary"] or first["title"])
        claims = [{"text": claim1, "item_ids": [first["item_id"]]}]
        if len(items) > 1:
            claims.append({"text": _first_sentence(items[1]["summary"] or items[1]["title"]), "item_ids": [items[1]["item_id"]]})
        text = " ".join(i["title"] + " " + i["summary"] for i in items)
        topics = _topics_for(text) or e.get("topics", [])
        why = "It shifts expectations for interest rates, currencies and risk assets."
        if "energy" in topics:
            why += " Analysts expect fuel costs to rise 7.7% next month."  # invented number: the gate must drop it
        if "rally" in text.lower():
            claims.append({"text": "Investors expect the rally to continue all week.", "item_ids": [first["item_id"]]})
        words = first["title"].split()
        events.append({
            "event_id": e["event_id"], "headline": " ".join(words[:12]), "summary": claim1, "why_it_matters": why,
            "channels": topics[:3] or ["sentiment"], "confidence": "high" if len(items) > 1 else "medium", "claims": claims,
        })
    return {"events": events, "watch_today": ["Nordland central bank press conference", "Atlantica retail sales release"],
            "one_line": "Rates on hold in Nordland, cooler inflation in Atlantica and an oil jump set the tone."}


def _checker(docs: dict[str, Any]) -> dict[str, Any]:
    out = []
    for c in docs.get("claims", []):
        snippets = " ".join(x["snippet"].lower() for x in c.get("cited", []))
        ok = c["text"].lower().rstrip(".") in snippets
        out.append({"event_id": c["event_id"], "claim_index": c["claim_index"], "supported": ok,
                    "note": "" if ok else "not stated in the cited snippets"})
    return {"verdicts": out}


def _scout(docs: dict[str, Any]) -> dict[str, Any]:
    cov = docs.get("coverage", {})
    probed = docs.get("already_probed", [])
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(probed[0]["url"])) if probed else "https://example.invalid"
    if cov.get("round") == 2:
        return {"candidates": [
            {"name": "Andes Business Journal", "url": f"{origin}/latam/rss.xml", "why": "closes the Latin America gap",
             "tier": "secondary", "region": "latam"},
            {"name": "Ghost Gazette", "url": f"{origin}/ghost/rss.xml", "why": "Latin America coverage", "tier": "unknown", "region": "latam"},
        ], "targets": cov.get("open_gaps", [])}
    return {"candidates": [], "targets": []}


def _assessor(docs: dict[str, Any]) -> dict[str, Any]:
    items, sources = [], {}
    for it in docs.get("items", []):
        text = f"{it['title']} {it['summary']}".lower()
        topics = [n for n, kws in TRUTH.items() if any(k in text for k in kws)]
        relevance = 0 if ("lifestyle" in text or "coffee" in text) else (2 if topics else 1)
        items.append({"item_id": it["item_id"], "relevance": relevance, "topics": topics})
        sources.setdefault(it["source_id"], []).append(relevance)
    return {"items": items, "sources": [{"source_id": k, "quality": max(1, min(5, round(1 + 2 * sum(v) / len(v)))), "note": ""}
                                        for k, v in sources.items()]}


def _rule_smith(docs: dict[str, Any]) -> dict[str, Any]:
    rules = {r["name"]: list(r["keywords"]) for r in docs.get("rules", [])}
    weights = {r["name"]: r.get("weight", 1.0) for r in docs.get("rules", [])}
    for err in docs.get("errors", []):
        text = err["text"].lower()
        for t in set(err["gold"]) - set(err["predicted"]):  # missed: add the teacher keyword found in the text
            for k in TRUTH.get(t, []):
                if k in text and k not in rules[t]:
                    rules[t].append(k)
        for t in set(err["predicted"]) - set(err["gold"]):  # false match: drop keywords the teacher would not use
            rules[t] = [k for k in rules[t] if not (k in text and k not in TRUTH.get(t, []))] or rules[t]
    return {"topics": [{"name": n, "keywords": kws, "weight": weights[n]} for n, kws in rules.items()], "notes": "added missed keywords"}


def _librarian(docs: dict[str, Any]) -> dict[str, Any]:
    text = next((v for k, v in docs.items() if not k.startswith("_")), "")
    text = text if isinstance(text, str) else json.dumps(text)
    return {"summary": _first_sentence(re.sub(r"#+ ", "", text))[:300] or "A document."}


def _rewriter(docs: dict[str, Any]) -> dict[str, Any]:
    q = docs["_inputs"].get("question", "")
    vocab = set(docs.get("corpus", {}).get("vocabulary", []))
    words = [w for w in re.findall(r"\w+", q.lower()) if w in vocab]
    return {"queries": [" ".join(words)] if words else [q]}


def _reranker(docs: dict[str, Any]) -> dict[str, Any]:
    q = set(re.findall(r"\w{4,}", docs["_inputs"].get("query", "").lower()))
    out = []
    for k, v in docs.items():
        if k.isdigit():
            overlap = len(q & set(re.findall(r"\w{4,}", str(v).lower())))
            out.append({"chunk_id": int(k), "score": min(3, overlap)})
    return {"scores": out}


def _agent(role: str, user: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Tool-calling agents: research with kb.search, write, verify (loop until 'revised')."""
    tool_results = [m["content"] for m in messages if m.get("role") == "tool"]
    inputs = re.search(r'(?:Dane wejściowe|Inputs): (\{.*\})', html.unescape(user))
    inp = json.loads(inputs.group(1)) if inputs else {}
    if role == "researcher":
        if not tool_results:
            pack = (inp.get("knowledge_packs") or ["macro"])[0]
            return {"__tool_calls__": [{"name": "kb__search", "arguments": {"pack": pack, "query": "policy rate decision", "k": 2}},
                                       {"name": "calc", "arguments": {"expression": "4.25 - 0.25"}}]}
        found = json.loads(tool_results[0]).get("result", [])
        titles = [h.get("title") for h in found] if isinstance(found, list) else []
        return {"__tool_calls__": [{"name": "submit_result", "arguments": {
            "status": "done", "summary": f"Found {len(titles)} sources: {', '.join(t for t in titles if t)}; next step 4.0",
            "output": {"sources": titles, "calc": json.loads(tool_results[1]).get("result") if len(tool_results) > 1 else None},
            "confidence": 0.8}}]}
    if role == "repo-coder":
        return _repo_coder(inp, tool_results)
    if role.startswith("w-"):
        if not tool_results:
            return {"__tool_calls__": [{"name": "repo__list", "arguments": {"path": "."}}]}
        if len(tool_results) == 1:
            files = json.loads(tool_results[0]).get("result") or []
            return {"__tool_calls__": [{"name": "kb__note", "arguments": {"pack": "project-map", "title": "Repository overview",
                                                                         "text": "The repository contains these files: " + "; ".join(map(str, files[:20]))}}]}
        return {"__tool_calls__": [{"name": "submit_result", "arguments": {
            "status": "done", "summary": "listed the repository and saved an overview to the pack project-map",
            "output": {"pack": "project-map"}, "confidence": 0.7}}]}
    docs = _docs(user)
    if role == "writer":
        revise = any(isinstance(v, dict) and v.get("status") == "failed" for v in docs.values())
        return {"status": "done", "summary": ("revised draft" if revise else "first draft") + " of the brief", "output": {}, "confidence": 0.6}
    if role == "verifier":
        draft = next((v for k, v in docs.items() if k.startswith("write") and isinstance(v, dict)), {})
        ok = "revised" in str(draft.get("summary", ""))
        return {"status": "done" if ok else "failed", "summary": "criteria met" if ok else "needs a revision: sources missing",
                "output": {}, "confidence": 0.7}
    return {"status": "done", "summary": f"{role} finished", "output": {}}


def _lead(docs: dict[str, Any]) -> dict[str, Any]:
    ledger = docs.get("ledger", {})
    progress = ledger.get("progress", []) if isinstance(ledger, dict) else []
    workers = docs["_inputs"].get("workers", {})
    if not progress:
        return {"done": False, "next_member": next(iter(workers), None), "instruction": "Find the latest policy rate decisions in the knowledge pack",
                "reason": "nothing known yet", "facts": [], "plan": ["research", "finish"]}
    last = progress[-1]
    return {"done": last["status"] == "done", "final_summary": f"Research complete: {last['summary']}",
            "next_member": last.get("member"), "instruction": "Try again with a narrower query", "reason": "check result",
            "facts": [last["summary"][:120]], "plan": ["finish"]}


def _guardian(docs: dict[str, Any]) -> dict[str, Any]:
    contracts = docs.get("contracts", [])
    issues = [{"team": c["team"], "issue": "team did not finish", "evidence": c["result"].get("status", ""), "severity": "high"}
              for c in contracts if c["result"].get("status") != "done"]
    return {"verdict": "issues" if issues else "consistent", "issues": issues}


# --- Lab team: eval cases, reading, novelty, self-improvement ---------------------------------

def _eval(model: str, whole: str, case_id: str) -> str:
    """Solves generated eval cases from their reference answer, with two built-in weaknesses the
    experiments must find: fake-smart-b needs a reasoning step for multi-step kinds, and
    fake-smart loses accuracy when the prompt is laid out as markdown."""
    import hashlib

    from roj.lab.tasks import case_by_id

    case = case_by_id(case_id)
    h = int(hashlib.sha256(f"{model}|{case_id}".encode()).hexdigest()[:8], 16) % 100
    thinks = "thinking>" in whole.split("## TASK")[0].split("<task>")[0]
    wrong = False
    if model.startswith("fake-smart-b"):
        wrong = not thinks and case.kind in ("arith", "grounded", "needle") and h < 70
    elif "## ROLE" in whole:
        wrong = h < 55
    answer = json.dumps({} if wrong else case.answer)
    return f"<thinking>Reading the task and checking each value against the data.</thinking>\n{answer}" if thinks else answer


def _lab_researcher(docs: dict[str, Any]) -> dict[str, Any]:
    fams = docs["_inputs"].get("families") or ["fake-smart"]
    ids = [k for k in docs if not k.startswith("_")]
    return {"claims": [{"family": fams[i % len(fams)], "claim": "Separate instructions from data with XML tags and put long documents first",
                        "source": d, "knob": "structure", "value": "xml"} for i, d in enumerate(ids[:3])]
            + [{"family": fams[0], "claim": "Give the model a concrete example of the expected output", "source": "invented-source"}]}


def _examiner(docs: dict[str, Any]) -> dict[str, Any]:
    items = [{"question": f"What is {m.group(1)[0].lower() + m.group(1)[1:]}?", "answer": m.group(2)}
             for m in re.finditer(r"((?:The|Our) [^.]+?) is ([^.]+)\.", str(docs.get("doc", "")))]
    return {"items": items[:6] or [{"question": "What is the document about?", "answer": "unknown topic"}]}


def _closed_book(docs: dict[str, Any]) -> dict[str, Any]:
    known = {"capital of france": "Paris", "boiling point of water": "100", "largest planet": "Jupiter"}
    return {"answers": [next((v for k, v in known.items() if k in q.lower()), "unknown") for q in docs["_inputs"].get("questions", [])]}


def _selfdev_planner(docs: dict[str, Any]) -> dict[str, Any]:
    return {"items": [
        {"title": "Add a slugify helper for report file names", "problem": "Report names are built ad hoc in several places.",
         "change": "Add toy/text.py with slugify(text) and a test for it.", "files": ["toy/text.py", "tests/test_slug.py"],
         "test_plan": "A new test checks 3 inputs including Polish letters.", "expected_gain": "one tested helper instead of copies",
         "risk": "low", "size_lines": 30},
        {"title": "Loosen the permission checks", "problem": "Approvals slow things down.", "change": "Edit the policy module.",
         "files": ["roj/core/policy.py"], "test_plan": "No test is needed for this change.", "expected_gain": "speed", "risk": "high",
         "size_lines": 10}]}


def _selfdev_reviewer(docs: dict[str, Any]) -> dict[str, Any]:
    bad = "os.system" in str(docs.get("diff", ""))
    return {"verdict": "reject" if bad else "approve", "summary": "runs a shell command" if bad else "small change with a test that covers it",
            "issues": ["toy/text.py: os.system call"] if bad else []}


SLUG = '''import re
import unicodedata


def slugify(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text.replace("ł", "l").replace("Ł", "L")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
'''
SLUG_TEST = '''from toy.text import slugify


def test_slugify():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("Zażółć gęślą jaźń") == "zazolc-gesla-jazn"
    assert slugify("  a  b ") == "a-b"
'''


def _repo_coder(inp: dict[str, Any], tool_results: list[str]) -> dict[str, Any]:
    if not tool_results:
        return {"__tool_calls__": [{"name": "repo__write", "arguments": {"path": "toy/text.py", "text": SLUG}},
                                   {"name": "repo__write", "arguments": {"path": "tests/test_slug.py", "text": SLUG_TEST}},
                                   {"name": "repo__write", "arguments": {"path": "tests/test_base.py", "text": "def test_x():\n    assert True\n"}}]}
    if len(tool_results) == 3:
        return {"__tool_calls__": [{"name": "repo__test", "arguments": {"target": "tests/test_slug.py"}}]}
    last = json.loads(tool_results[-1])
    ok = last.get("ok") and last["result"].get("failed") == 0
    return {"__tool_calls__": [{"name": "submit_result", "arguments": {
        "status": "done" if ok else "failed", "summary": "added slugify with a test; the existing test file was refused as expected",
        "output": {"tests": last.get("result", {}).get("passed")}, "confidence": 0.8}}]}


# --- missions ----------------------------------------------------------------------------------

def _mission_intake(docs: dict[str, Any]) -> dict[str, Any]:
    return {"understanding": "The owner wants the project to improve itself in stages, starting with understanding its own code. "
                             "The biggest unknown is which part matters most to him.",
            "questions": [{"id": "focus", "question": "Which area should the first stages focus on?", "why": "it decides the first team",
                           "options": ["token cost", "reliability", "documentation"], "default": "documentation"},
                          {"id": "pace", "question": "How many stages per day may run?", "why": "budget planning", "default": "1"}]}


def _mission_plan(docs: dict[str, Any]) -> dict[str, Any]:
    fixing = bool(docs["_inputs"].get("issues_to_fix"))
    n = docs["_inputs"].get("stage_number", 1)
    member = {"agent": "w-code-analyst", "summary": "Reads the repository and writes an overview into a knowledge pack",
              "instructions": "Czytasz repozytorium projektu i zapisujesz zwięzły opis jego struktury do pakietu wiedzy project-map.",
              "binding": "analyst", "tools": ["repo.list", "kb.note"] + ([] if fixing else ["magic.wand"]),
              "task": "Opisz strukturę repozytorium i zapisz opis w pakiecie wiedzy project-map",
              "acceptance": ["W pakiecie project-map istnieje co najmniej 1 dokument"], "deliverable": "Wpis w pakiecie wiedzy project-map"}
    return {"charter": {"problem": "The project must understand itself before it can improve itself.",
                        "success_criteria": ["Every module has a description in a knowledge pack", "At least 1 merged improvement per week"],
                        "constraints": ["Code changes only through selfdev pull requests"], "out_of_scope": ["New product features"]},
            "roadmap": ["Map the repository", "Find the most expensive prompts", "First measured improvement"][n - 1:] or ["Keep improving"],
            "stage": {"objective": f"Stage {n}: build a first map of the repository as shared knowledge",
                      "acceptance": ["The pack project-map contains at least 1 document"],
                      "needs": [{"kind": "knowledge", "name": "project-map", "why": "agents need a shared description of the code"}],
                      "team": [member], "risks": ["The map may be too shallow"]}}


def _mission_critic(docs: dict[str, Any]) -> dict[str, Any]:
    return {"verdict": "approve", "issues": []}


def _mission_review(docs: dict[str, Any]) -> dict[str, Any]:
    results = docs.get("results", {})
    ok = all(r.get("status") == "done" for r in results.values()) if isinstance(results, dict) and results else False
    return {"verdict": "done" if ok else "rework", "summary": "The analyst listed the repository and saved one overview document to project-map.",
            "evidence": ["w-code-analyst reported pack project-map"], "lessons": ["A flat file list is not enough: describe modules next"],
            "questions": [], "change_requests": [{"title": "Add a slugify helper for report file names",
                                                  "goal": "Report names are built ad hoc; add one tested helper and use it in one place."}],
            "mission_complete": False}


def _mission_chat(docs: dict[str, Any]) -> dict[str, Any]:
    msg = str(docs["_inputs"].get("owner_message", ""))
    return {"reply": f"Understood. Mission status: {docs.get('mission', {}).get('status')}.",
            "directive": msg if any(w in msg.lower() for w in ("always", "zawsze", "never", "nigdy")) else None}
