# First run on your machine (Windows)

> Since v0.3 the main path is the server: see [SERVER.md](SERVER.md) and [WALKTHROUGH.md](WALKTHROUGH.md). This page remains valid for local experiments.

Goal of the first session: install, check everything with `roj doctor`, run the offline demo,
run the real bootstrap and one real brief, then send one diagnostic zip for a joint review.
Nothing below sends anything to anyone except your own gateway, the public news feeds and
your own Telegram bot.

## 1. Install (once)

```powershell
# PowerShell, in the folder where you unpacked roj
py -3.12 -m pip install uv          # or use an existing uv
uv sync --locked --extra dev
uv run pytest -q                    # expect: all tests pass (about 20 s)
```

Pick a data folder with space (logs, reports and the content store grow over time):

```powershell
setx ROJ_HOME "D:\roj-home"         # new terminals see it; for this one also:
$env:ROJ_HOME = "D:\roj-home"
uv run roj init
```

If `roj doctor` later reports an unknown timezone: `uv pip install tzdata`.

## 2. Configure

1. Compare `D:\roj-home\roj.toml` with `examples\roj.toml.example` and copy the parts you need:
   `[gateway] base_url`, `[models.*]` (exact ids from `uv run roj models --refresh`), `[pricing.models.*]`.
2. Secrets go to environment variables, never into files:
   ```powershell
   setx ROJ_API_KEY "<gateway key, if it needs one>"
   setx ROJ_TELEGRAM_TOKEN "<token from @BotFather>"
   setx FRED_API_KEY "<free key from fred.stlouisfed.org>"   # optional: indices, yields, Brent, VIX
   ```
3. Telegram chat id: send any message to your bot, open
   `https://api.telegram.org/bot<TOKEN>/getUpdates`, copy `message.chat.id` into `[notify] telegram_chat_id`.

## 3. Check

```powershell
uv run roj doctor            # configuration, gateway, role bindings, prices, Telegram, projects
uv run roj doctor --live     # + one tiny request per bound model, prompt-cache probe, Telegram getMe, one feed
uv run roj telegram test     # a test message in your chat
```

Fix every FAIL. WARN lines say what is missing and why it matters.

## 4. Offline demo (no tokens)

```powershell
# terminal 1
uv run python -m roj.testing.fake_gateway --port 18045
# terminal 2
uv run python -m roj.testing.fake_web --port 18046
```

Use a separate home for the demo so it does not touch your real one:

```powershell
$env:ROJ_HOME = "D:\roj-demo"; uv run roj init
# in D:\roj-demo\roj.toml: base_url = "http://127.0.0.1:18045/v1", bind every [models.*] to fake-smart
# (critic, checker, classifier to fake-smart-b), [web] per_host_interval_s = 0
uv run roj project create demo
uv run roj skill bootstrap -p demo news_digest --seeds offline:http://127.0.0.1:18046
uv run roj approvals list -p demo
uv run roj approvals approve -p demo <ID>; uv run roj task resume -p demo
uv run roj skill run -p demo news_digest --no-send
uv run roj logs show -p demo
```

## 5. The real thing

```powershell
$env:ROJ_HOME = "D:\roj-home"
uv run roj project create morning
uv run roj skill bootstrap -p morning news_digest            # deep R&D: 10-16 model calls
```

Read the research report it names (`projects\morning\reports\research-news_digest-*.md`):
sources probed and rejected, discovery rounds, critique, learned topic rules with their
scores, ranking calibration. Then approve it (Telegram button, `roj panel`, or CLI) and:

```powershell
uv run roj skill run -p morning news_digest                  # first real brief, 2-3 model calls
uv run roj schedule add -p morning news_digest --at 08:00
uv run roj scheduler windows-task                            # prints two schtasks commands: run them
uv run roj panel                                             # local panel; open the printed URL once
```

Rate the brief with the 1-5 buttons under the Telegram message.

## 6. Send me the logs

```powershell
uv run roj logs export --days 2
```

It prints the path of `roj-diagnostics-*.zip` in `D:\roj-home\exports`. It contains task logs
(`*.log` readable, `*.jsonl` raw), `roj.log`, doctor results, settings, project health,
reports and every model request/response of the period. API keys and the Telegram token are
not in it (they live in environment variables, and log payloads are masked). Add
`--no-prompts` if you do not want the prompts and answers included.

What I will look at together with you: real token cost per role vs ADR-0006, which sources
the probe accepted, rule scores on visible and hidden cases, what the gates removed, latency
and cache hits per model, any errors or fallbacks.
