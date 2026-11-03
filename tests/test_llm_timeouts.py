"""The live review against a slow or failing endpoint: a real local HTTP server and the real OpenAI SDK (timeouts and
retries happen in the SDK, which a mocked transport would bypass). Covers the request timeout, no hidden retries,
the review client's settings, and the smoke command's diagnosis."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from restaurant_ops.config import settings


class FakeModelAPI:
    """An OpenAI-compatible /chat/completions endpoint. `ping_delay` applies to plain requests, `review_delay` to
    structured ones (response_format set); `status` != 200 answers with an error."""

    def __init__(self, ping_delay=0.0, review_delay=0.0, status=200):
        self.ping_delay, self.review_delay, self.status = ping_delay, review_delay, status
        self.requests: list[dict] = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append(body)
                time.sleep(api.review_delay if body.get("response_format") else api.ping_delay)
                if api.status != 200:
                    payload, code = {"error": {"message": "invalid api key", "type": "auth"}}, api.status
                else:
                    content = (json.dumps({"keep": [1], "rationale": "cod uses stock on hand", "confidence": 0.8})
                               if body.get("response_format") else "OK")
                    payload, code = {"id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                                     "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                                     "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}}, 200
                data = json.dumps(payload).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):  # the client gave up (timeout)
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def meta_live(monkeypatch):
    from restaurant_ops.agents import llm

    apis = []

    def start(**kw):
        api = FakeModelAPI(**kw)
        apis.append(api)
        monkeypatch.setattr(settings, "LLM_MODE", "live")
        monkeypatch.setattr(settings, "LLM_PROVIDER", "meta")
        monkeypatch.setattr(settings, "META_API_KEY", "test-key")
        monkeypatch.setattr(settings, "META_BASE_URL", api.url)
        llm.get_review_llm.cache_clear()
        llm.get_llm.cache_clear()
        return api

    yield start
    for a in apis:
        a.close()
    llm.get_review_llm.cache_clear()
    llm.get_llm.cache_clear()


def test_review_client_has_a_timeout_no_retries_and_low_effort(meta_live):
    from restaurant_ops.agents import llm

    meta_live()
    review = llm.get_review_llm()
    assert review.request_timeout == settings.LLM_TIMEOUT_S
    assert review.max_retries == settings.LLM_MAX_RETRIES == 0
    assert review.reasoning_effort == settings.META_REVIEW_REASONING_EFFORT == "low"
    batch = llm.get_llm()  # the batch planning agents keep the SDK defaults (long tool-calling runs)
    assert batch.request_timeout is None and batch.reasoning_effort is None


def test_a_slow_endpoint_times_out_once_without_retries(meta_live, monkeypatch):
    from restaurant_ops.agents import llm
    from restaurant_ops.live.agents.base import _review
    from restaurant_ops.live.llm_eval import SMOKE_CANDIDATES, SMOKE_SITUATION

    api = meta_live(review_delay=3.0)
    monkeypatch.setattr(settings, "LLM_TIMEOUT_S", 0.6)
    llm.get_review_llm.cache_clear()
    t0 = time.perf_counter()
    with pytest.raises(Exception) as e:
        _review("menu", SMOKE_SITUATION, SMOKE_CANDIDATES)
    took = time.perf_counter() - t0
    assert "Timeout" in type(e.value).__name__
    assert took < 2.0, f"the request should stop at the timeout, took {took:.1f}s"
    assert len(api.requests) == 1, "no hidden SDK retries"
    assert api.requests[0]["reasoning_effort"] == "low"


def test_smoke_tells_a_slow_review_from_a_slow_endpoint(meta_live):
    from restaurant_ops.live import llm_eval

    meta_live(ping_delay=0.0, review_delay=3.0)
    r = llm_eval.smoke(timeout=0.8)
    assert r["ok"] is False and r["ping"]["ok"] and r["review"]["kind"] == "timeout"
    assert "--effort" in r["hint"] and r["settings"]["reasoning_effort"] == "low"

    meta_live(ping_delay=3.0)
    r = llm_eval.smoke(timeout=0.8)
    assert r["ok"] is False and r["ping"]["kind"] == "timeout" and "--timeout 60" in r["hint"]


def test_smoke_reports_auth_errors_and_success(meta_live):
    from restaurant_ops.live import llm_eval

    meta_live(status=401)
    r = llm_eval.smoke(timeout=5)
    assert r["ping"]["kind"] == "auth" and "API key" in r["hint"]

    api = meta_live()
    r = llm_eval.smoke(timeout=5, effort="")
    assert r["ok"] and r["review"]["keep"] == [1] and r["review"]["tokens_in"] == 50
    assert r["settings"]["reasoning_effort"] == "API default"
    assert all("reasoning_effort" not in b for b in api.requests), "effort '' sends no reasoning_effort"
