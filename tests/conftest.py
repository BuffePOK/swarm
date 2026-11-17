from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # for wf_fixtures

from roj.project import Installation  # noqa: E402
from roj.testing.fake_gateway import start_fake_gateway  # noqa: E402
from roj.testing.fake_web import start_fake_web  # noqa: E402

TOML = """\
timezone = "Europe/Warsaw"

[gateway]
base_url = "{base_url}"
api_key_env = "ROJ_TEST_KEY"
timeout_s = 10

[budget]
tokens_per_day = 1000000
usd_per_day = 5.0
default_max_tokens = 256

[language]
prompt_language = "pl"
output_language = "en"

[pricing]
strict = false

[pricing.default]
input = 15.0
output = 75.0

[pricing.models."fake-ok"]
input = 1.0
cached_input = 0.1
output = 2.0

[pricing.models."fake-*"]
input = 0.5
output = 1.0

[pricing.models."fake-smart*"]
input = 3.0
cached_input = 0.3
output = 15.0

[web]
per_host_interval_s = 0
timeout_s = 5

[notify]
telegram_chat_id = "42"
telegram_token_env = "ROJ_TEST_TG"
telegram_api = "{web_url}"

[models.planner]
model = "fake-smart"
[models.critic]
model = "fake-smart-b"
[models.coder]
model = "fake-smart"
[models.classifier]
model = "fake-smart-b"
[models.analyst]
model = "fake-smart"
[models.checker]
model = "fake-smart-b"

[retry]
max_attempts = 3
base_delay_s = 0.001
max_delay_s = 0.01
rate_limit_max_wait_s = 0
breaker_threshold = 3
breaker_cooldown_s = 60
[search]
provider = "searxng"
searxng_url = "{web_url}"
arxiv_url = "{web_url}/arxiv"
semantic_scholar_url = "{web_url}/s2"
daily_limit = 5
sources = ["index", "hn", "github", "wikipedia", "stackexchange"]
crawl_daily_pages = 50
[search.endpoints]
hn = "{web_url}/api/hn"
github = "{web_url}/api/github"
wikipedia = "{web_url}/w/api.php"
stackexchange = "{web_url}/api/se"
"""


@pytest.fixture
def gateway():
    server, state, base_url = start_fake_gateway()
    yield state, base_url
    server.shutdown()
    server.server_close()


@pytest.fixture
def web():
    server, state, base_url = start_fake_web()
    yield state, base_url
    server.shutdown()
    server.server_close()


@pytest.fixture
def home(tmp_path, gateway, web, monkeypatch):
    _, base_url = gateway
    _, web_url = web
    monkeypatch.setenv("ROJ_TEST_TG", "123:TEST")
    monkeypatch.setenv("FRED_API_KEY", "test-key")
    h = tmp_path / "home"
    Installation.init(h)
    (h / "roj.toml").write_text(TOML.format(base_url=base_url, web_url=web_url), encoding="utf-8")
    return h


@pytest.fixture
def inst(home):
    i = Installation(home)
    yield i
    i.close()


@pytest.fixture
def project(inst):
    p = inst.create_project("t")
    yield p
    p.close()
