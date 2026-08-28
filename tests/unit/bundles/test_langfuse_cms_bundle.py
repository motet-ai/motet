"""
Motet - Unit tests for the langfuse-cms example bundle

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
Unit tests for Langfuse Cloud credential resolution, prompt text extraction,
the context_inject live-fetch command, and the wrapper command's fallback /
infer / fail-soft generation push path. No network calls — httpx and vault
are mocked.

Dependencies:
- pytest
- motet_sdk.testing.MockMotetContext
- _langfuse_cms_test_loader

Usage:
  pytest tests/unit/bundles/test_langfuse_cms_bundle.py -q
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from motet_sdk.testing import MockMotetContext

from _langfuse_cms_test_loader import (
    load_command_module,
    load_helper_module,
    load_tool_module,
)


@pytest.fixture(scope="module")
def lf():
    return load_helper_module()


@pytest.fixture(scope="module")
def turn_mod():
    return load_command_module("agent_turn_with_langfuse_prompt")


@pytest.fixture(scope="module")
def inject_mod():
    return load_command_module("inject_langfuse_prompt")


@pytest.fixture(scope="module")
def record_mod():
    return load_command_module("record_turn_to_langfuse")


@pytest.fixture(scope="module")
def prompts_mod():
    return load_tool_module("langfuse_prompts")


def _unwrap(fn):
    return getattr(fn, "__wrapped__", fn)


def test_extract_text_prompt(lf):
    text = lf.extract_system_prompt_text(
        {"type": "text", "prompt": "  You are concise.  ", "version": 2}
    )
    assert text == "You are concise."


def test_extract_chat_prefers_system(lf):
    text = lf.extract_system_prompt_text(
        {
            "type": "chat",
            "prompt": [
                {"role": "system", "content": "System A"},
                {"role": "user", "content": "User B"},
            ],
        }
    )
    assert text == "System A"


def test_credentials_from_mapping_requires_host(lf):
    with pytest.raises(lf.LangfuseConfigError, match="host"):
        lf.credentials_from_mapping(
            {"public_key": "pk", "secret_key": "sk"},
            require_host=True,
        )


def test_credentials_from_mapping_ok(lf):
    creds = lf.credentials_from_mapping(
        {
            "public_key": "pk-lf-x",
            "secret_key": "sk-lf-y",
            "host": "https://us.cloud.langfuse.com/",
        }
    )
    assert creds["host"] == "https://us.cloud.langfuse.com"
    assert creds["public_key"] == "pk-lf-x"


def test_resolve_credentials_from_vault(lf):
    vault = Mock()
    vault.get_credential = Mock(
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://cloud.langfuse.com",
        }
    )
    motet = MockMotetContext(vault=vault, distributed_context=Mock())
    creds = lf.resolve_credentials(motet, require_host=True)
    assert creds["public_key"] == "pk"
    vault.get_credential.assert_called()


def test_resolve_credentials_env(lf, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    vault = Mock()
    vault.get_credential = Mock(return_value=None)
    motet = MockMotetContext(vault=vault, distributed_context=Mock())
    creds = lf.resolve_credentials(motet, require_host=True)
    assert creds["public_key"] == "pk-env"


def test_record_turn_skips_without_credentials(record_mod, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    vault = Mock()
    vault.get_credential = Mock(return_value=None)
    motet = MockMotetContext(vault=vault, distributed_context=Mock())
    data = record_mod.RecordTurnToLangfuseData(
        messages=[{"role": "user", "content": "hi"}],
        assistant_response="hello",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    result = _unwrap(record_mod.record_turn_to_langfuse)(data, motet)
    assert result["ok"] is False
    assert result.get("skipped") is True


def test_record_turn_pushes_generation(record_mod):
    vault = Mock()
    vault.get_credential = Mock(
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://us.cloud.langfuse.com",
        }
    )
    motet = MockMotetContext(vault=vault, distributed_context=Mock())
    with patch.object(
        record_mod.lf,
        "record_generation",
        return_value={"trace_id": "t-ww", "observation_id": "o-ww"},
    ) as push:
        data = record_mod.RecordTurnToLangfuseData(
            messages=[{"role": "user", "content": "hi"}],
            assistant_response="I am Wonder Woman.",
            agent_id="langfuse-cms.prompt-manager",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            cost_usd=0.002,
            model="openai/gpt-4o-mini",
            context={"langfuse_prompt_source": "langfuse", "langfuse_prompt_version": 2},
        )
        result = _unwrap(record_mod.record_turn_to_langfuse)(data, motet)

    assert result["ok"] is True
    assert result["trace_id"] == "t-ww"
    push.assert_called_once()
    kwargs = push.call_args.kwargs
    # Prompt linkage is a first-class Langfuse field, not opaque metadata.
    assert kwargs["prompt_name"] == "langfuse_cms.prompt_manager"
    assert kwargs["prompt_version"] == 2
    # Conversation becomes the Langfuse session so turns group together.
    assert kwargs["session_id"] == motet.conversation_id


def test_record_turn_omits_prompt_link_on_fallback(record_mod):
    """A fallback turn did not run the Cloud prompt, so claiming a version would
    attribute the generation to a prompt it never used."""
    vault = Mock()
    vault.get_credential = Mock(
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://us.cloud.langfuse.com",
        }
    )
    motet = MockMotetContext(vault=vault, distributed_context=Mock())
    with patch.object(
        record_mod.lf,
        "record_generation",
        return_value={"trace_id": "t-fb", "observation_id": "o-fb"},
    ) as push:
        data = record_mod.RecordTurnToLangfuseData(
            messages=[{"role": "user", "content": "hi"}],
            assistant_response="hello",
            model="openai/gpt-4o-mini",
            context={
                "langfuse_prompt_source": "fallback",
                "langfuse_prompt_fallback_reason": "no credentials",
            },
        )
        result = _unwrap(record_mod.record_turn_to_langfuse)(data, motet)

    assert result["ok"] is True
    kwargs = push.call_args.kwargs
    assert kwargs["prompt_name"] is None
    assert kwargs["prompt_version"] is None
    assert kwargs["metadata"]["prompt_fallback_reason"] == "no credentials"


def test_inject_falls_back_without_credentials(inject_mod, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    vault = Mock()
    vault.get_credential = Mock(return_value=None)
    motet = MockMotetContext(vault=vault, distributed_context=Mock())

    data = inject_mod.InjectLangfusePromptData(messages=[], context={})
    result = _unwrap(inject_mod.inject_langfuse_prompt)(data, motet)

    assert result["context_patch"]["langfuse_prompt_source"] == "fallback"
    assert "langfuse_prompt_fallback_reason" in result["context_patch"]
    assert len(result["system_messages"]) == 1
    assert "Langfuse Cloud" in result["system_messages"][0]


def test_inject_uses_langfuse_prompt(inject_mod):
    vault = Mock()
    vault.get_credential = Mock(
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://cloud.langfuse.com",
        }
    )
    motet = MockMotetContext(vault=vault, distributed_context=Mock())

    with patch.object(
        inject_mod.lf,
        "fetch_system_prompt",
        return_value=(
            "Live CMS prompt",
            {
                "name": "langfuse_cms.prompt_manager",
                "label": "production",
                "version": 9,
                "source": "langfuse",
            },
        ),
    ):
        data = inject_mod.InjectLangfusePromptData(messages=[], context={})
        result = _unwrap(inject_mod.inject_langfuse_prompt)(data, motet)

    assert result["system_messages"] == ["Live CMS prompt"]
    assert result["context_patch"]["langfuse_prompt_source"] == "langfuse"
    assert result["context_patch"]["langfuse_prompt_version"] == 9


def test_inject_respects_context_overrides(inject_mod):
    vault = Mock()
    vault.get_credential = Mock(
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://cloud.langfuse.com",
        }
    )
    motet = MockMotetContext(vault=vault, distributed_context=Mock())

    with patch.object(
        inject_mod.lf,
        "resolve_turn_system_prompt",
        return_value={
            "system_prompt": "Staging voice",
            "prompt_source": "langfuse",
            "fallback_reason": None,
            "creds": {"public_key": "pk"},
            "prompt_meta": {"version": 1},
        },
    ) as resolve:
        data = inject_mod.InjectLangfusePromptData(
            messages=[],
            context={
                "langfuse_prompt_name": "langfuse_cms.summarizer",
                "langfuse_prompt_label": "staging",
            },
        )
        result = _unwrap(inject_mod.inject_langfuse_prompt)(data, motet)

    resolve.assert_called_once()
    kwargs = resolve.call_args.kwargs
    assert kwargs["prompt_name"] == "langfuse_cms.summarizer"
    assert kwargs["prompt_label"] == "staging"
    assert result["system_messages"] == ["Staging voice"]


def test_wrapper_falls_back_without_credentials(turn_mod, monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    vault = Mock()
    vault.get_credential = Mock(return_value=None)
    models = Mock()
    models.infer = Mock(
        return_value={
            "content": "Hello from fallback",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cost_usd": 0.001,
        }
    )
    motet = MockMotetContext(
        vault=vault,
        models=models,
        distributed_context=Mock(),
    )

    data = turn_mod.AgentTurnWithLangfusePromptData(
        message="Hi",
        record_to_langfuse=True,
    )
    result = _unwrap(turn_mod.agent_turn_with_langfuse_prompt)(data, motet)

    assert result["prompt_source"] == "fallback"
    assert result["content"] == "Hello from fallback"
    assert result["langfuse_generation"]["status"] == "skipped_no_credentials"
    models.infer.assert_called_once()
    system_msg = models.infer.call_args.kwargs["messages"][0]["content"]
    assert "Langfuse Cloud" in system_msg


def test_wrapper_uses_langfuse_prompt_and_records(turn_mod):
    vault = Mock()
    vault.get_credential = Mock(
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://cloud.langfuse.com",
        }
    )
    models = Mock()
    models.infer = Mock(
        return_value={
            "content": "Cloud prompt reply",
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "total_tokens": 7,
            "cost_usd": 0.002,
        }
    )
    motet = MockMotetContext(
        vault=vault,
        models=models,
        distributed_context=Mock(),
    )

    with patch.object(
        turn_mod.lf,
        "fetch_system_prompt",
        return_value=(
            "You are the Cloud prompt.",
            {
                "name": "langfuse_cms.prompt_manager",
                "label": "production",
                "version": 3,
                "source": "langfuse",
            },
        ),
    ), patch.object(
        turn_mod.lf,
        "record_generation",
        return_value={"trace_id": "t1", "observation_id": "o1"},
    ) as record:
        data = turn_mod.AgentTurnWithLangfusePromptData(
            message="Hi",
            record_to_langfuse=True,
        )
        result = _unwrap(turn_mod.agent_turn_with_langfuse_prompt)(data, motet)

    assert result["prompt_source"] == "langfuse"
    assert result["content"] == "Cloud prompt reply"
    assert result["langfuse_generation"]["status"] == "recorded"
    assert result["langfuse_generation"]["trace_id"] == "t1"
    system_msg = models.infer.call_args.kwargs["messages"][0]["content"]
    assert system_msg == "You are the Cloud prompt."
    record.assert_called_once()


def test_wrapper_generation_push_fail_soft(turn_mod):
    vault = Mock()
    vault.get_credential = Mock(
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://cloud.langfuse.com",
        }
    )
    models = Mock()
    models.infer = Mock(return_value={"content": "ok"})
    motet = MockMotetContext(
        vault=vault,
        models=models,
        distributed_context=Mock(),
    )

    with patch.object(
        turn_mod.lf,
        "fetch_system_prompt",
        return_value=(
            "Cloud",
            {"name": "x", "label": "production", "version": 1, "source": "langfuse"},
        ),
    ), patch.object(
        turn_mod.lf,
        "record_generation",
        side_effect=RuntimeError("ingestion down"),
    ):
        data = turn_mod.AgentTurnWithLangfusePromptData(message="Hi")
        result = _unwrap(turn_mod.agent_turn_with_langfuse_prompt)(data, motet)

    assert result["content"] == "ok"
    assert result["langfuse_generation"]["status"] == "error"
    assert "ingestion down" in result["langfuse_generation"]["error"]


def _otlp_span(captured):
    """The single span out of a captured OTLP payload."""
    return captured["json_body"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _attrs(span):
    """OTLP KeyValue list flattened to {key: unwrapped value}."""
    out = {}
    for item in span["attributes"]:
        value = item["value"]
        out[item["key"]] = next(iter(value.values()))
    return out


@pytest.fixture
def captured_otlp(lf):
    """Capture what record_generation would POST, without a network call."""
    captured = {}

    def _fake_request(creds, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json_body"] = kwargs.get("json_body")
        captured["headers"] = kwargs.get("headers")
        return {"partialSuccess": {}}

    with patch.object(lf, "_request", side_effect=_fake_request):
        yield captured


def test_record_generation_posts_otlp_span(lf, captured_otlp):
    """The deprecated /api/public/ingestion batch endpoint is gone, and a span
    implies its trace — which is what makes the turn visible on the Traces page."""
    result = lf.record_generation(
        {"public_key": "pk", "secret_key": "sk", "host": "https://us.cloud.langfuse.com"},
        model="openai/gpt-4o-mini",
        input_messages=[{"role": "user", "content": "hi"}],
        output="hello",
        usage={"prompt_tokens": 317, "completion_tokens": 40, "total_tokens": 357},
        cost_usd=0.00007155,
        name="langfuse-cms.agent_turn",
        session_id="conv-1",
    )

    assert captured_otlp["method"] == "POST"
    assert captured_otlp["path"] == "/api/public/otel/v1/traces"
    # v4 real-time ingestion, otherwise the turn waits for a batch flush.
    assert captured_otlp["headers"]["x-langfuse-ingestion-version"] == "4"

    span = _otlp_span(captured_otlp)
    assert span["name"] == "langfuse-cms.agent_turn"
    # OTLP ids are hex: 16 bytes for a trace, 8 for a span.
    assert len(span["traceId"]) == 32
    assert len(span["spanId"]) == 16
    assert result["trace_id"] == span["traceId"]
    assert result["observation_id"] == span["spanId"]


def test_record_generation_maps_cost_to_native_field(lf, captured_otlp):
    """gen_ai.usage.cost is what Langfuse reads for a generation's cost.
    langfuse.observation.cost_details is only parsed for Langfuse SDK spans, so
    it would be silently dropped here — and cost in metadata never reaches the
    cost columns at all."""
    lf.record_generation(
        {"public_key": "pk", "secret_key": "sk", "host": "https://us.cloud.langfuse.com"},
        model="openai/gpt-4o-mini",
        input_messages=[{"role": "user", "content": "hi"}],
        output="hello",
        usage={"prompt_tokens": 317, "completion_tokens": 40},
        cost_usd=0.00007155,
        metadata={"agent_id": "langfuse-cms.prompt-manager"},
    )

    attrs = _attrs(_otlp_span(captured_otlp))
    assert attrs["gen_ai.usage.cost"] == pytest.approx(0.00007155)
    assert attrs["langfuse.observation.type"] == "generation"
    assert attrs["langfuse.observation.model.name"] == "openai/gpt-4o-mini"
    # int64 is string-encoded in OTLP/JSON.
    assert attrs["gen_ai.usage.input_tokens"] == "317"
    assert attrs["gen_ai.usage.output_tokens"] == "40"
    # The metadata prefix is what makes a key filterable in Langfuse.
    assert attrs["langfuse.observation.metadata.agent_id"] == "langfuse-cms.prompt-manager"


def test_record_generation_omits_absent_cost_and_session(lf, captured_otlp):
    lf.record_generation(
        {"public_key": "pk", "secret_key": "sk", "host": "https://us.cloud.langfuse.com"},
        model="openai/gpt-4o-mini",
        input_messages=[{"role": "user", "content": "hi"}],
        output="hello",
    )

    attrs = _attrs(_otlp_span(captured_otlp))
    # An unpriced turn must not report a cost of zero.
    assert "gen_ai.usage.cost" not in attrs
    assert "langfuse.session.id" not in attrs
    assert "gen_ai.usage.input_tokens" not in attrs


def test_record_generation_links_session_and_prompt_version(lf, captured_otlp):
    lf.record_generation(
        {"public_key": "pk", "secret_key": "sk", "host": "https://us.cloud.langfuse.com"},
        model="openai/gpt-4o-mini",
        input_messages=[{"role": "user", "content": "hi"}],
        output="hello",
        session_id="conv-42",
        user_id="user-7",
        prompt_name="langfuse_cms.prompt_manager",
        prompt_version=3,
    )

    attrs = _attrs(_otlp_span(captured_otlp))
    assert attrs["langfuse.session.id"] == "conv-42"
    assert attrs["langfuse.user.id"] == "user-7"
    assert attrs["langfuse.observation.prompt.name"] == "langfuse_cms.prompt_manager"
    assert attrs["langfuse.observation.prompt.version"] == "3"


def test_get_prompt_tool_ok(prompts_mod, lf):
    vault = Mock()
    vault.get_credential = Mock(
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://cloud.langfuse.com",
        }
    )
    motet = MockMotetContext(vault=vault, distributed_context=Mock())

    with patch(
        "motet_sdk.get_motet_context",
        return_value=motet,
    ), patch.object(
        prompts_mod,
        "_lf",
        return_value=lf,
    ), patch.object(
        lf,
        "resolve_credentials",
        return_value={
            "public_key": "pk",
            "secret_key": "sk",
            "host": "https://cloud.langfuse.com",
        },
    ), patch.object(
        lf,
        "get_prompt",
        return_value={
            "name": "langfuse_cms.prompt_manager",
            "type": "text",
            "prompt": "Hello",
            "version": 1,
        },
    ):
        result = prompts_mod.get_prompt({"name": "langfuse_cms.prompt_manager"})

    assert result["ok"] is True
    assert result["prompt_text"] == "Hello"
