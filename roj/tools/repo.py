"""Tools for working on a git checkout (used by the self-improvement loop).

They act only inside `ctx.repo_root` (a throwaway git worktree, never the running
installation), refuse protected paths, and run tests with an environment that holds no
secrets. Without `ctx.repo_root` they refuse to work.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..core.kernel import StepContext  # noqa: F401  (resolved by get_type_hints for the generated tool cards)
from .registry import ToolError, tool

SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "dist", "build"}


def root(ctx: "StepContext") -> Path:
    r = getattr(ctx, "repo_root", None)
    if not r:
        raise ToolError("no repository is attached to this task")
    return Path(r).resolve()


def inside(ctx: "StepContext", path: str) -> Path:
    base = root(ctx)
    p = (base / path).resolve()
    if p != base and base not in p.parents:
        raise PermissionError("path is outside the repository")
    if ".git" in p.relative_to(base).parts:
        raise PermissionError("the .git directory is off limits")
    return p


def is_protected(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def _writable(ctx: "StepContext", path: str) -> Path:
    p = inside(ctx, path)
    rel = p.relative_to(root(ctx)).as_posix()
    if is_protected(rel, ctx.project.settings.selfdev.protected):
        raise PermissionError(f"{rel} is owner-only (protected): propose the change in your summary instead")
    if rel.startswith("tests/") and p.exists() and rel in getattr(ctx, "repo_base_files", set()):
        raise PermissionError(f"{rel} is an existing test: tests may be added, not changed (ratchet). Add a new test file.")
    return p


def clean_env(repo: Path) -> dict[str, str]:
    """Environment for agent-written code: no API keys, no tokens, nothing named ROJ_*."""
    keep = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR")}
    return {**keep, "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(repo), "ROJ_SELFDEV_SANDBOX": "1",
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}  # Windows would otherwise write cp1252


_isolation: list[str] | None = None


def isolation_prefix() -> list[str]:
    """`unshare` into a private network namespace with only loopback, when the kernel allows it."""
    global _isolation
    if _isolation is None:
        _isolation = []
        if sys.platform.startswith("linux") and shutil.which("unshare") and shutil.which("ip"):
            probe = subprocess.run(["unshare", "-rn", "sh", "-c", "ip link set lo up"], capture_output=True, timeout=10)
            if probe.returncode == 0:
                _isolation = ["unshare", "-rn", "sh", "-c", 'ip link set lo up; exec "$@"', "sh"]
    return _isolation


def run_pytest(repo: Path, target: str = "", timeout_s: int = 600, extra: tuple[str, ...] = ()) -> dict:
    cmd = [*isolation_prefix(), sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *extra]
    if target:
        cmd.append(target)
    try:
        r = subprocess.run(cmd, cwd=repo, env=clean_env(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"exit": -1, "passed": 0, "failed": 0, "tail": f"timed out after {timeout_s}s", "network_isolated": bool(isolation_prefix())}
    out = (r.stdout + "\n" + r.stderr).strip()
    count = lambda word: int(m.group(1)) if (m := re.search(rf"(\d+) {word}", out[-600:])) else 0  # noqa: E731
    return {"exit": r.returncode, "passed": count("passed"), "failed": count("failed") + count("error"), "tail": out[-3000:],
            "network_isolated": bool(isolation_prefix())}


@tool("repo.list", "List files of the repository (optionally under a folder) with their line counts", timeout_s=20)
def repo_list(ctx: "StepContext", path: str = ".") -> list[str]:
    base, start = root(ctx), inside(ctx, path)
    out = []
    for f in sorted(start.rglob("*")):
        if f.is_file() and not (set(f.relative_to(base).parts) & SKIP_DIRS) and f.stat().st_size < 400_000:
            try:
                out.append(f"{f.relative_to(base).as_posix()} ({sum(1 for _ in f.open('rb'))} lines)")
            except OSError:
                continue
    return out[:600]


@tool("repo.read", "Read lines of a repository file (numbered); read only the part you need", timeout_s=10)
def repo_read(ctx: "StepContext", path: str, start: int = 1, lines: int = 200) -> str:
    text = inside(ctx, path).read_text(encoding="utf-8", errors="replace").splitlines()
    part = text[max(0, start - 1): start - 1 + min(lines, 400)]
    return "\n".join(f"{i}\t{line}" for i, line in enumerate(part, max(1, start))) + f"\n[{len(text)} lines total]"


@tool("repo.search", "Search the repository with a regular expression; returns path:line: text", timeout_s=30)
def repo_search(ctx: "StepContext", pattern: str, path: str = ".") -> list[str]:
    base, rx, out = root(ctx), re.compile(pattern), []
    for f in sorted(inside(ctx, path).rglob("*")):
        if not f.is_file() or (set(f.relative_to(base).parts) & SKIP_DIRS) or f.suffix in (".png", ".zip", ".db", ".lock"):
            continue
        try:
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f.relative_to(base).as_posix()}:{n}: {line.strip()[:200]}")
                    if len(out) >= 80:
                        return out
        except OSError:
            continue
    return out


@tool("repo.write", "Create or overwrite a file in the repository worktree", tier="t1", side_effects="writes a worktree file", timeout_s=10)
def repo_write(ctx: "StepContext", path: str, text: str) -> dict:
    p = _writable(ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return {"path": p.relative_to(root(ctx)).as_posix(), "lines": text.count("\n") + 1}


@tool("repo.replace", "Replace one exact occurrence of text in a repository file (cheaper than rewriting the file)", tier="t1",
      side_effects="writes a worktree file", timeout_s=10)
def repo_replace(ctx: "StepContext", path: str, old: str, new: str) -> dict:
    p = _writable(ctx, path)
    s = p.read_text(encoding="utf-8")
    if s.count(old) != 1:
        raise ToolError(f"`old` must occur exactly once in {path}, found {s.count(old)} times: include more surrounding text")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    return {"path": path, "replaced": 1}


@tool("repo.test", "Run the test suite (or one test file) in the worktree; returns counts and the end of the output", tier="t1",
      side_effects="runs the repository's tests in a sandboxed process", timeout_s=660)
async def repo_test(ctx: "StepContext", target: str = "") -> dict:
    if target:
        inside(ctx, target.split("::")[0])
    return await asyncio.to_thread(run_pytest, root(ctx), target, ctx.project.settings.selfdev.test_timeout_s)
