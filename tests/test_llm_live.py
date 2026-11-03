"""Opt-in check against a real LLM provider (costs a request).

    RUN_LIVE_LLM=1 uv run pytest tests/test_llm_live.py -s

Uses LLM_PROVIDER / LLM_MODEL and the provider's API key from .env. Skipped unless RUN_LIVE_LLM=1 and a key is set
(ollama needs no key). One review call through the live path; it must parse into a judgment within LLM_TIMEOUT_S.
"""
from __future__ import annotations

import json
import os

import pytest

from restaurant_ops.config import settings

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_LLM") != "1" or (settings.LLM_PROVIDER != "ollama" and not settings.provider_api_key),
    reason="set RUN_LIVE_LLM=1 and the provider's API key to call a real model")


def test_real_provider_review(monkeypatch):
    from restaurant_ops.agents import llm
    from restaurant_ops.live import llm_eval

    monkeypatch.setattr(settings, "LLM_MODE", "live")
    llm.get_llm.cache_clear()
    r = llm_eval.smoke()
    print(json.dumps(r, indent=2))
    assert r["ok"], r.get("error")
    assert r["keep"] or r["rationale"], "the model returned an empty judgment"
    assert not r["ignored_indices"], "the model referred to candidates that do not exist"
