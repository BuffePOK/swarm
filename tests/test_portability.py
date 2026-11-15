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
