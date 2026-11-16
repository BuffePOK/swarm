"""Conditions of other platforms, reproduced on Linux (CI on 2026-11-15 failed on Windows and macOS)."""

import os
import subprocess
import sys

import pytest

from roj.skills.sandbox import run_adapter


def test_time_zones_work_without_system_zoneinfo(tmp_path):
    # Windows (and minimal containers) have no /usr/share/zoneinfo; the tzdata package must cover it.
    code = "from zoneinfo import ZoneInfo; from datetime import datetime; print(datetime(2026, 7, 1, tzinfo=ZoneInfo('Europe/Warsaw')).utcoffset())"
    env = {**os.environ, "PYTHONTZPATH": str(tmp_path / "no-zoneinfo-here")}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "2:00:00"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource limits")
def test_sandbox_runs_when_the_os_rejects_a_memory_limit(monkeypatch):
    # macOS rejects RLIMIT_AS; the child must still start with the limits it can get.
    import resource

    real = resource.setrlimit

    def picky(limit, values):
        if limit == resource.RLIMIT_AS:
            raise ValueError("not supported on this platform")
        return real(limit, values)

    monkeypatch.setattr(resource, "setrlimit", picky)
    code = "def parse(raw, base_url):\n    return [{'title': 'Story ' + raw, 'link': base_url + 'a'}]\n"
    [item] = run_adapter(code, "one", "http://x.example/", timeout_s=20)
    assert (item["title"], item["link"]) == ("Story one", "http://x.example/a")


def test_windows_paths_in_settings_get_a_clear_error(tmp_path):
    from roj.config import Settings

    (tmp_path / "roj.toml").write_text('[storage]\nbackup_dir = "D:\\Users\\roj"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="forward slashes"):
        Settings.load(tmp_path)


def test_every_text_subprocess_decodes_utf8():
    # Windows decodes child output as cp1252 unless told otherwise; git diffs and test output are UTF-8.
    # CI on 2026-11-16: `git diff` of a file with Polish letters came back as None on Windows.
    import ast
    from pathlib import Path

    missing = []
    for f in sorted(Path(__file__).resolve().parents[1].joinpath("roj").rglob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("run", "check_output", "Popen"):
                kw = {k.arg for k in node.keywords}
                if "text" in kw and "encoding" not in kw:
                    missing.append(f"{f.name}:{node.lineno}")
    assert missing == []


def test_git_output_with_non_ascii_survives(tmp_path):
    from roj.lab.selfdev import git

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("Zażółć gęślą jaźń\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    assert "Zażółć gęślą jaźń" in git(tmp_path, "diff", "--cached")


def test_remove_tree_clears_read_only_files(tmp_path, monkeypatch):
    # Windows refuses to delete read-only files (git writes its objects read-only), CI on 2026-11-16.
    import shutil

    from roj.util import remove_tree

    target = tmp_path / "tree"
    (target / "objects").mkdir(parents=True)
    locked = target / "objects" / "pack.idx"
    locked.write_text("x", encoding="utf-8")
    real_unlink = os.unlink
    refused = []

    def windows_like_unlink(path, *a, **kw):
        if str(path).endswith("pack.idx") and os.stat(path, dir_fd=kw.get("dir_fd")).st_mode & 0o200 == 0:
            refused.append(path)
            raise PermissionError(13, "Access is denied", str(path))
        return real_unlink(path, *a, **kw)

    os.chmod(locked, 0o444)
    monkeypatch.setattr(os, "unlink", windows_like_unlink)
    shutil.rmtree(target, ignore_errors=True)
    assert target.exists() and refused  # the old way leaves the tree behind
    assert remove_tree(target) is True and not target.exists()


def test_remove_tree_removes_a_git_repository(tmp_path):
    from roj.util import remove_tree

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
                   check=True, capture_output=True)
    assert remove_tree(repo) is True and not repo.exists()
