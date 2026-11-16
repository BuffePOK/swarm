"""The model catalog the Architect chooses from: chat models on the gateway, with family, price, context
window and the Lab's card. Built by code from the registry (no tokens).

The owner binds only the leads (`architect`, `leader`, optionally `cheap`). Every agent the Architect
creates gets a concrete model and fallbacks from this catalog, and the stage linter checks the choice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..prompting.profiles import family

if TYPE_CHECKING:
    from ..project import Project

NON_CHAT = re.compile(r"embed|ocr|voxtral|whisper|tts|transcri|moderation|rerank|image|imagen|veo|-fim|fim-|audio|realtime", re.I)


def is_chat(model_id: str, meta: dict[str, Any]) -> bool:
    kind = str(meta.get("kind") or "").lower()
    if kind:
        return kind == "chat"  # the gateway knows (apigate reports kind)
    return not NON_CHAT.search(model_id)


def cards(home: Path) -> dict[str, dict[str, Any]]:
    out = {}
    d = Path(home) / "playbook" / "models"
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            c = json.loads(f.read_text("utf-8"))
            out[c["model"]] = c
        except (ValueError, KeyError):
            continue
    return out


def chat_models(project: "Project") -> list[dict[str, Any]]:
    return catalog_for(project.inst, project.settings.pricing)


def catalog_for(inst: Any, pricing: Any) -> list[dict[str, Any]]:
    cs = cards(inst.home)
    out = []
    for r in inst.store.all("SELECT id, status, blocked_until, meta FROM models WHERE status = 'available' ORDER BY id"):
        meta = json.loads(r["meta"] or "{}")
        if not is_chat(r["id"], meta) or meta.get("alias_of"):
            continue
        price, known = pricing.price_for(r["id"])
        c = cs.get(r["id"], {})
        out.append({"id": r["id"], "family": family(r["id"]), "context": meta.get("context_window"), "in": price.input,
                    "out": price.output, "price_known": known, "blocked": bool(r["blocked_until"]),
                    "card": {k: c[k] for k in ("score", "tokens_per_case", "latency_s", "tool_calling", "available") if k in c}})
    return out


def render(models: list[dict[str, Any]]) -> str:
    """One line per model, compact: what the Architect needs to staff a team."""
    lines = ["id | family | USD per 1M in/out | context | Lab card (score 0-1, tokens per case, tool calling)"]
    for m in models:
        if m["blocked"] or m["card"].get("available") is False:
            continue
        price = f"{m['in']:g}/{m['out']:g}" + ("" if m["price_known"] else " (unknown price: budgeted at this default)")
        c = m["card"]
        card = (f"score {c['score']:.2f}, {c.get('tokens_per_case', 0):.0f} tok, tools {'yes' if c.get('tool_calling') else 'no'}"
                if "score" in c else "no card yet")
        lines.append(f"{m['id']} | {m['family']} | {price} | {m['context'] or '?'} | {card}")
    return "\n".join(lines)


def import_apigate_prices(text: str) -> tuple[dict[str, dict[str, float]], dict[str, float] | None]:
    """apigate's price list ([[price]] provider/pattern/in/out, [defaults]) -> roj pricing globs 'provider/pattern'."""
    import tomllib

    doc = tomllib.loads(text)
    models: dict[str, dict[str, float]] = {}
    for row in doc.get("price", []):
        prov, pat = str(row["provider"]).strip(), str(row["pattern"]).strip()
        models[f"{prov}/{pat}"] = {"input": float(row["in"]), "output": float(row["out"])}
    d = doc.get("defaults")
    return models, ({"input": float(d["in"]), "output": float(d["out"])} if d else None)


def write_pricing(home: Path, models: dict[str, dict[str, float]]) -> Path:
    lines = ["# Written by `roj pricing import` from the gateway's price list. Entries in roj.toml [pricing.models] win.",
             "# USD per 1M tokens. Re-run the import after changing prices in the gateway.", ""]
    for k in sorted(models):
        v = models[k]
        lines += [f'[models."{k}"]', f"input = {v['input']}", f"output = {v['output']}", ""]
    path = Path(home) / "pricing.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
