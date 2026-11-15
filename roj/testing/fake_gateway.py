"""Offline OpenAI-compatible gateway for tests and dry runs (stdlib only).

Model names choose the behaviour, so every failure path can be exercised
without spending a single real token:

  fake-ok, fake-cheap, fake-*   200, deterministic echo, simulated prefix caching
  fake-smart, fake-smart-b      play the morning-brief roles (see fake_brains)
  fake-sloppy                   invalid JSON first, valid after a repair message
  fake-blocked*                 403 "disabled for violation of Terms of Service"
  fake-ratelimit*               429 with Retry-After: 0
  fake-flaky*                   500 for the first 2 requests, then 200
  fake-auth*                    401
  fake-bad*                     400
  anything not listed           404 model not found

Run:  python -m roj.testing.fake_gateway --port 8045
Then: base_url = "http://127.0.0.1:8045/v1"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .fake_brains import respond

DEFAULT_MODELS = ["fake-ok", "fake-cheap", "fake-blocked", "fake-ratelimit", "fake-flaky", "fake-auth", "fake-bad",
                  "fake-smart", "fake-smart-b", "fake-sloppy", "fake-embed"]


class FakeState:
    def __init__(self, models: list[str]):
        self.models = list(models)
        self.lock = threading.Lock()
        self.requests: Counter[str] = Counter()
        self.seen_prefixes: set[str] = set()
        self.flaky_failures: Counter[str] = Counter()


def _hash_embed(text: str, dims: int = 64) -> list[float]:
    """Hashing-trick bag of words: deterministic, crude, good enough to test hybrid retrieval."""
    import math
    import re as _re

    v = [0.0] * dims
    for w in _re.findall(r"[a-z0-9]{3,}", text.lower()):
        h = int(hashlib.md5(w[:6].encode()).hexdigest(), 16)
        v[h % dims] += 1.0 if (h >> 8) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _make_handler(state: FakeState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "roj-fake-gateway/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep test output clean
            return

        def _send(self, code: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in state.models]})
            elif self.path.endswith("/_stats"):
                with state.lock:
                    self._send(200, {"requests": dict(state.requests)})
            else:
                self._send(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/").endswith("/embeddings"):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                inputs = body.get("input") or []
                inputs = [inputs] if isinstance(inputs, str) else inputs
                with state.lock:
                    state.requests[str(body.get("model"))] += 1
                data = [{"object": "embedding", "index": i, "embedding": _hash_embed(x)} for i, x in enumerate(inputs)]
                self._send(200, {"object": "list", "data": data, "model": body.get("model"),
                                 "usage": {"prompt_tokens": sum(_tokens(x) for x in inputs), "total_tokens": sum(_tokens(x) for x in inputs)}})
                return
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._send(404, {"error": {"message": "not found"}})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            model = str(body.get("model", ""))
            messages = body.get("messages") or []
            with state.lock:
                state.requests[model] += 1
            if model not in state.models:
                self._send(404, {"error": {"message": f"model {model} not found"}})
                return
            if model.startswith("fake-blocked"):
                self._send(403, {"error": {"message": "This service has been disabled in this account for violation of Terms of Service. Please submit an appeal."}})
                return
            if model.startswith("fake-ratelimit"):
                self._send(429, {"error": {"message": "quota exceeded"}}, {"Retry-After": "0"})
                return
            if model.startswith("fake-auth"):
                self._send(401, {"error": {"message": "invalid api key"}})
                return
            if model.startswith("fake-bad"):
                self._send(400, {"error": {"message": "invalid request"}})
                return
            if model.startswith("fake-flaky"):
                with state.lock:
                    state.flaky_failures[model] += 1
                    fail = state.flaky_failures[model] <= 2
                if fail:
                    self._send(500, {"error": {"message": "internal error"}})
                    return
            system = "".join(m.get("content") or "" for m in messages if m.get("role") == "system")
            everything = "".join(str(m.get("content") or "") for m in messages)
            last_user = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")
            prefix = hashlib.sha256(system.encode()).hexdigest()
            with state.lock:
                cached = _tokens(system) if system and prefix in state.seen_prefixes else 0
                if system:
                    state.seen_prefixes.add(prefix)
            digest = hashlib.sha256(everything.encode()).hexdigest()[:12]
            tool_calls = None
            if model.startswith(("fake-smart", "fake-sloppy")):
                text = respond(model, messages)
                if text.startswith('{"__tool_calls__"'):
                    calls = json.loads(text)["__tool_calls__"]
                    tool_calls = [{"id": f"call_{digest}_{i}", "type": "function",
                                   "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for i, c in enumerate(calls)]
                    text = ""
            else:
                text = f"[{model}] ECHO {digest}: {last_user[:120]}"
            self._send(200, {
                "id": f"chatcmpl-{digest}",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text or None, **({"tool_calls": tool_calls} if tool_calls else {})},
                             "finish_reason": "tool_calls" if tool_calls else "stop"}],
                "usage": {
                    "prompt_tokens": _tokens(everything),
                    "completion_tokens": _tokens(text),
                    "total_tokens": _tokens(everything) + _tokens(text),
                    "prompt_tokens_details": {"cached_tokens": cached},
                },
            })

    return Handler


def start_fake_gateway(host: str = "127.0.0.1", port: int = 0, models: list[str] | None = None):
    """Start in a daemon thread. Returns (server, state, base_url)."""
    state = FakeState(models or DEFAULT_MODELS)
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, state, f"http://{host}:{server.server_address[1]}/v1"


def main() -> None:
    ap = argparse.ArgumentParser(description="roj offline fake gateway")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8045)
    args = ap.parse_args()
    state = FakeState(DEFAULT_MODELS)
    server = ThreadingHTTPServer((args.host, args.port), _make_handler(state))
    print(f"fake gateway on http://{args.host}:{args.port}/v1  models: {', '.join(DEFAULT_MODELS)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
