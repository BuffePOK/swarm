"""Gate and sandbox for model-written code (ADR-0005).

Generated adapters are small pure functions `parse(raw: str, base_url: str) -> list[dict]`.
Before one is ever executed:
  1. static check: AST allow-list of imports and builtins, no dunder access, no I/O;
  2. execution in a separate interpreter (`python -I -S`: no site-packages, no .pth,
     no environment-driven imports), stdin/stdout only, timeout, and CPU/memory limits
     on POSIX;
  3. output validated against a schema.
This is a strong guard-rail against prompt-injected or careless code, not a security
boundary against a determined attacker; installing code is therefore also T2 (owner).
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ALLOWED_IMPORTS = {
    "re", "json", "html", "html.parser", "datetime", "math", "string", "itertools", "collections",
    "unicodedata", "urllib.parse", "xml.etree.ElementTree", "email.utils", "typing", "dataclasses",
}
FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "input", "breakpoint", "help", "exit", "quit", "memoryview", "object", "type", "super",
    "classmethod", "staticmethod", "property",
}
MAX_SOURCE_CHARS = 20_000
MAX_ITEMS = 500

_HARNESS = r'''
import json, sys
sys.setrecursionlimit(2000)
src = open(sys.argv[1], encoding="utf-8").read()
ns = {"__name__": "adapter"}
exec(compile(src, "adapter.py", "exec"), ns)
payload = json.loads(sys.stdin.read())
out = ns["parse"](payload["raw"], payload["base_url"])
sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))
'''


class SandboxError(Exception):
    pass


def check_code(src: str, entrypoint: str = "parse") -> list[str]:
    problems: list[str] = []
    if len(src) > MAX_SOURCE_CHARS:
        return [f"source longer than {MAX_SOURCE_CHARS} characters"]
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    entry = None
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.FunctionDef) and node.name == entrypoint:
                entry = node
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue  # docstring
        problems.append(f"line {node.lineno}: only imports, functions and constants allowed at top level")
    if entry is None:
        problems.append(f"missing function {entrypoint}(raw, base_url)")
    elif len(entry.args.args) != 2:
        problems.append(f"{entrypoint} must take exactly (raw, base_url)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS:
                    problems.append(f"line {node.lineno}: import {a.name} not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "") not in ALLOWED_IMPORTS:
                problems.append(f"line {node.lineno}: from {node.module} import ... not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"line {node.lineno}: name {node.id} not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"line {node.lineno}: dunder attribute {node.attr} not allowed")
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            problems.append(f"line {node.lineno}: dunder name {node.id} not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.Lambda)):
            problems.append(f"line {node.lineno}: {type(node).__name__} not allowed")
    return sorted(set(problems))


def _limits() -> None:  # POSIX only, runs in the child before exec
    """Each limit is best effort: macOS rejects RLIMIT_AS, and an exception here would stop the child.
    The CPU limit and the parent's wall-clock timeout always apply."""
    import resource

    for name, value in (("RLIMIT_CPU", 10), ("RLIMIT_AS", 768 * 1024 * 1024), ("RLIMIT_NOFILE", 16)):
        limit = getattr(resource, name, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass


def run_adapter(src: str, raw: str, base_url: str, *, timeout_s: float = 15.0) -> list[dict[str, Any]]:
    problems = check_code(src)
    if problems:
        raise SandboxError("static check failed: " + "; ".join(problems[:8]))
    with tempfile.TemporaryDirectory(prefix="roj-sbx-") as d:
        dpath = Path(d)
        (dpath / "adapter.py").write_text(src, encoding="utf-8")
        (dpath / "harness.py").write_text(_HARNESS, encoding="utf-8")
        env = {"PYTHONIOENCODING": "utf-8", "PATH": os.environ.get("PATH", "")}
        if sys.platform == "win32":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", str(dpath / "harness.py"), str(dpath / "adapter.py")],
                input=json.dumps({"raw": raw, "base_url": base_url}), capture_output=True, text=True,
                encoding="utf-8", timeout=timeout_s, cwd=d, env=env,
                preexec_fn=_limits if sys.platform != "win32" else None,
            )
        except subprocess.TimeoutExpired as e:
            raise SandboxError(f"adapter timed out after {timeout_s}s") from e
    if proc.returncode != 0:
        raise SandboxError("adapter raised: " + proc.stderr.strip().splitlines()[-1][:300] if proc.stderr.strip() else f"exit {proc.returncode}")
    try:
        out = json.loads(proc.stdout or "null")
    except json.JSONDecodeError as e:
        raise SandboxError("adapter output is not JSON") from e
    return validate_items(out)


def validate_items(out: Any) -> list[dict[str, Any]]:
    if not isinstance(out, list):
        raise SandboxError("parse() must return a list of dicts")
    items = []
    for i, it in enumerate(out[:MAX_ITEMS]):
        if not isinstance(it, dict):
            raise SandboxError(f"item {i} is not a dict")
        title, link = it.get("title"), it.get("link")
        if not isinstance(title, str) or not isinstance(link, str) or not title.strip() or not link.strip():
            raise SandboxError(f"item {i} needs non-empty string title and link")
        items.append({
            "title": title.strip()[:300], "link": link.strip()[:1000],
            "published": str(it["published"])[:40] if it.get("published") else None,
            "summary": str(it.get("summary") or "")[:500],
        })
    return items
