"""LLM access: `get_llm()` for the configured provider, and the mock-mode switch.

Provider and model come from env only:
- `LLM_PROVIDER=meta` (default): Meta Model API (Muse Spark) through its OpenAI-compatible endpoint (`META_API_KEY`, `META_BASE_URL`, `META_MODEL`); only `tool_choice="auto"` is accepted, so structured output uses `json_schema`.
- `LLM_PROVIDER=openai`: OpenAI (`OPENAI_API_KEY`, `OPENAI_MODEL`, optional `OPENAI_BASE_URL`).
- `LLM_PROVIDER=anthropic`: Anthropic (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`).
- `LLM_PROVIDER=ollama`: local Ollama (`OLLAMA_MODEL`).
`LLM_MODEL` overrides the model, either as a bare name or as `provider:model`.
LangSmith tracing is optional and configured purely through env vars (`config.Settings` exports them).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from restaurant_ops.config import settings


class _ToolCallingChatOpenAI(ChatOpenAI):
    """ChatOpenAI whose structured output uses function calling instead of `json_schema`.

    `AgentReport.payload` is a free-form dict, which strict JSON-schema mode rejects, and OpenAI-compatible
    endpoints such as Meta's support tool calling more reliably than `response_format=json_schema`.
    """

    def with_structured_output(self, schema: Any = None, *, method: str = "function_calling", **kwargs: Any):
        return super().with_structured_output(schema, method=method, **kwargs)


class _MetaChatOpenAI(ChatOpenAI):
    """ChatOpenAI for Meta Model API (https://dev.meta.ai/docs).

    Model API accepts only `tool_choice="auto"`, so function-calling structured output (which names the tool) is
    rejected with HTTP 400. Structured output uses `response_format={"type": "json_schema"}` instead, sent as a plain
    dict with `strict: false`: passing the Pydantic class would make the OpenAI SDK send a strict schema, which
    `AgentReport.payload` (a free-form dict) fails. Decoding is schema-constrained either way.

    With every field optional and extra keys allowed, Muse Spark was seen emitting keys such as `"actions:"`, which
    Pydantic silently drops. The schema is therefore closed (every property required, no extra keys), and returned
    keys are cleaned before validation as a second line of defence.
    """

    def with_structured_output(self, schema: Any = None, *, include_raw: bool = False, **kwargs: Any):
        kwargs.pop("method", None)
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            return super().with_structured_output(schema, method="json_schema", include_raw=include_raw, **kwargs)
        fmt = {"name": schema.__name__, "schema": _closed(schema.model_json_schema()), "strict": False}
        if include_raw:
            def to_model(out: dict) -> dict:
                if out.get("parsed") is not None:
                    out["parsed"] = schema.model_validate(_clean_keys(out["parsed"]))
                return out
        else:
            def to_model(out: dict) -> BaseModel:
                return schema.model_validate(_clean_keys(out))
        runnable = super().with_structured_output(fmt, method="json_schema", include_raw=include_raw, **kwargs)
        return runnable | RunnableLambda(to_model)


def _closed(node: Any) -> Any:
    """Copy of a JSON schema where every object with declared properties requires all of them and allows no others.
    Objects without properties (free-form dicts such as `Action.payload`) stay open."""
    if isinstance(node, list):
        return [_closed(x) for x in node]
    if not isinstance(node, dict):
        return node
    out = {k: _closed(v) for k, v in node.items()}
    if isinstance(out.get("properties"), dict) and out["properties"]:
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _clean_keys(obj: Any) -> Any:
    """Strip stray punctuation from top-level and nested model keys (`"actions:"` -> `actions`); payload dicts
    are cleaned too, which is harmless for their snake_case keys."""
    if isinstance(obj, list):
        return [_clean_keys(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    return {re.sub(r"[^A-Za-z0-9_]", "", k) or k: _clean_keys(v) for k, v in obj.items()}


def is_mock() -> bool:
    """True when agents are replaced by the rule-based policies in `mock_policies.py`."""
    return settings.LLM_MODE != "live"


def describe() -> str:
    """Short `provider:model` label for logs and the UI."""
    return f"{settings.LLM_PROVIDER}:{settings.model_name}"


def _no_temperature(model: str) -> bool:
    """OpenAI reasoning models (o-series, gpt-5*) reject a temperature setting."""
    return model.startswith(("o1", "o3", "o4", "gpt-5"))


@lru_cache(maxsize=1)
def get_llm() -> BaseChatModel:
    """The shared chat model (temperature 0 where supported). Only called in live mode."""
    provider, model = settings.LLM_PROVIDER, settings.model_name
    if provider == "meta":
        # Muse Spark is tuned for its default sampling, so no temperature (per the Model API docs)
        kwargs = {"model": model, "api_key": settings.META_API_KEY, "base_url": settings.META_BASE_URL}
        if settings.META_REASONING_EFFORT:
            kwargs["reasoning_effort"] = settings.META_REASONING_EFFORT
        return _MetaChatOpenAI(**kwargs)
    if provider == "openai":
        kwargs: dict[str, Any] = {"model": model, "api_key": settings.OPENAI_API_KEY}
        if settings.OPENAI_BASE_URL:
            kwargs["base_url"] = settings.OPENAI_BASE_URL
        if not _no_temperature(model):
            kwargs["temperature"] = 0
        return _ToolCallingChatOpenAI(**kwargs)
    return init_chat_model(f"{provider}:{model}", temperature=0)
