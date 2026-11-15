"""Supply-chain gate (ADR-0001). Run in CI and before every release.

1. Core runtime dependencies <= 5.
2. Every installed distribution has an allowed license.
3. No unexpected .pth files that execute code at interpreter start
   (the vector used in the LiteLLM PyPI compromise of March 2026).

Exit code 0 = clean, 1 = problems found.
"""

from __future__ import annotations

import fnmatch
import importlib.metadata as md
import re
import site
import sys
import sysconfig
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_CORE_DEPS = 5

# Permissive licenses. MPL-2.0 is allowed for unmodified third-party dependencies
# (file-level copyleft, e.g. certifi pulled in by httpx); we never copy MPL code into roj.
ALLOWED = {"MIT", "BSD", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "APACHE-2.0", "ISC", "PSF-2.0", "PSF", "MPL-2.0",
           "0BSD", "UNLICENSE", "ZLIB"}
DENIED = re.compile(r"\b(A?GPL|SSPL|BUSL|COMMONS CLAUSE|CC-BY-NC)", re.I)

ALLOWED_PTH = ["_virtualenv.pth", "distutils-precedence.pth", "_editable_impl_*.pth", "__editable__*.pth",
               "*-nspkg.pth", "_roj*.pth"]

CLASSIFIER_MAP = {
    "MIT License": "MIT",
    "BSD License": "BSD",
    "Apache Software License": "APACHE-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}


def license_tokens(dist: md.Distribution) -> set[str]:
    meta = dist.metadata
    tokens: set[str] = set()
    expr = meta.get("License-Expression") or ""
    if expr:
        tokens |= {t.upper() for t in re.split(r"\s+(?:OR|AND|WITH)\s+|[()]", expr) if t.strip()}
    for c in meta.get_all("Classifier") or []:
        if c.startswith("License :: OSI Approved :: "):
            name = c.rsplit(" :: ", 1)[-1]
            tokens.add(CLASSIFIER_MAP.get(name, name.upper()))
    if not tokens:
        raw = (meta.get("License") or "").strip().upper()
        if raw and len(raw) < 40:
            tokens.add(raw.replace(" LICENSE", ""))
    return {t.strip() for t in tokens if t.strip()}


def check_licenses() -> list[str]:
    problems = []
    for dist in md.distributions():
        name = dist.metadata["Name"]
        tokens = license_tokens(dist)
        text = " ".join(tokens)
        if DENIED.search(text):
            problems.append(f"license denied: {name} ({text})")
        elif not tokens:
            problems.append(f"license unknown: {name}")
        elif not any(t in ALLOWED for t in tokens):
            problems.append(f"license not on allow-list: {name} ({text})")
    return problems


def check_pth() -> list[str]:
    dirs = {Path(p) for p in site.getsitepackages() + [sysconfig.get_paths()["purelib"]] if Path(p).exists()}
    problems = []
    for d in dirs:
        for pth in d.glob("*.pth"):
            if any(fnmatch.fnmatch(pth.name, pat) for pat in ALLOWED_PTH):
                continue
            executes = any(line.lstrip().startswith("import") for line in pth.read_text("utf-8", "replace").splitlines())
            problems.append(f"unexpected .pth file {'(executes code) ' if executes else ''}: {pth}")
    return problems


def check_core_deps() -> list[str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["dependencies"]
    return [f"core has {len(deps)} runtime dependencies > {MAX_CORE_DEPS}"] if len(deps) > MAX_CORE_DEPS else []


def main() -> int:
    problems = check_core_deps() + check_licenses() + check_pth()
    for p in problems:
        print(f"FAIL {p}")
    if not problems:
        print(f"supply chain OK: {sum(1 for _ in md.distributions())} distributions checked")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
