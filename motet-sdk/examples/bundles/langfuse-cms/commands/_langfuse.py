"""
Motet SDK - Langfuse CMS: Shared Langfuse Cloud Client

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
HTTP helpers for Langfuse Cloud prompt management and optional generation
export. Used by this example bundle's commands and tools, including the
``inject_langfuse_prompt`` turn hook (live Chat Explorer fetch) and the
CLI wrapper command. Credentials come from Motet vault (preferred) or
environment variables for local demos. Uses httpx — not the langfuse
Python SDK — so workers need no extra deps.

Prompt reads use the public v2 prompts API. Turn export posts one OTLP span per
turn to ``/api/public/otel/v1/traces``: the older ``/api/public/ingestion`` batch
endpoint is deprecated ahead of Langfuse v4, and its bare ``generation-create``
left the Traces page empty because no trace record accompanied the observation.

Dependencies:
- httpx: HTTPS client for Langfuse Cloud public API
- os / urllib.parse / uuid / json / time: credential env + OTLP request building

Usage:
  from . import _langfuse as lf

  creds = lf.resolve_credentials(motet)
  text, meta = lf.fetch_system_prompt(creds, name=lf.DEFAULT_PROMPT_NAME)
  lf.record_generation(creds, model="openai/gpt-4o-mini", ...)

Notes:
- Underscore modules are skipped by the command loader but importable via
  ``from . import _langfuse``.
- Tools load this module with importlib using the bundle package name.
- Fail-soft for generation push: callers should catch and continue.
- Prefer an explicit Cloud host (EU vs US); env may default to EU cloud.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

DEFAULT_PROMPT_NAME = "langfuse_cms.prompt_manager"
DEFAULT_LABEL = "production"
DEFAULT_VAULT_KEY = "langfuse"
DEFAULT_HOST = "https://cloud.langfuse.com"
# OTLP/HTTP traces endpoint. Langfuse also accepts the collector-style
# /api/public/otel base; the signal-specific path is what a hand-rolled exporter
# posts to. Replaces the deprecated /api/public/ingestion batch endpoint.
OTEL_TRACES_PATH = "/api/public/otel/v1/traces"
OTEL_SPAN_KIND_CLIENT = 3
OTEL_STATUS_CODE_OK = 1
FALLBACK_SYSTEM_PROMPT = (
    "You are a helpful Motet demo agent whose system prompt can optionally "
    "be managed in Langfuse Cloud.\n\n"
    "When answering, be concise and practical. If the user asks about prompt "
    "management, explain that they can list/get/update the Langfuse prompt "
    "named langfuse_cms.prompt_manager (label production by default) using the "
    "langfuse-cms prompt tools. Chat Explorer turns load that prompt live "
    "via context_inject (langfuse-cms.inject_langfuse_prompt).\n\n"
    "If Langfuse credentials are missing or Cloud is unreachable, you still "
    "answer using this static fallback prompt — that is intentional."
)


class LangfuseConfigError(RuntimeError):
    """Raised when Langfuse credentials or host cannot be resolved."""


class LangfuseAPIError(RuntimeError):
    """Raised when a Langfuse Cloud API call fails."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _pick(data: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_host(host: str) -> str:
    return host.rstrip("/")


def credentials_from_mapping(data: Dict[str, Any], *, require_host: bool = True) -> Dict[str, str]:
    """Build credentials dict from vault payload or similar mapping."""
    public_key = _pick(data, "public_key", "langfuse_public_key", "pk")
    secret_key = _pick(data, "secret_key", "langfuse_secret_key", "sk")
    host = _pick(data, "host", "langfuse_host", "base_url", "langfuse_base_url")

    if not public_key or not secret_key:
        raise LangfuseConfigError(
            "Langfuse credentials require public_key and secret_key "
            "(or langfuse_public_key / langfuse_secret_key)"
        )
    if not host:
        if require_host:
            raise LangfuseConfigError(
                "Langfuse host is required (e.g. https://cloud.langfuse.com "
                "or https://us.cloud.langfuse.com) — set host in vault or LANGFUSE_HOST"
            )
        host = DEFAULT_HOST

    return {
        "public_key": public_key,
        "secret_key": secret_key,
        "host": _normalize_host(host),
    }


def credentials_from_env(*, require_host: bool = False) -> Optional[Dict[str, str]]:
    """Resolve credentials from LANGFUSE_* environment variables, if present."""
    public_key = (os.environ.get("LANGFUSE_PUBLIC_KEY") or "").strip()
    secret_key = (os.environ.get("LANGFUSE_SECRET_KEY") or "").strip()
    host = (
        os.environ.get("LANGFUSE_HOST")
        or os.environ.get("LANGFUSE_BASE_URL")
        or ""
    ).strip()
    if not public_key or not secret_key:
        return None
    payload: Dict[str, Any] = {
        "public_key": public_key,
        "secret_key": secret_key,
    }
    if host:
        payload["host"] = host
    return credentials_from_mapping(payload, require_host=require_host)


def resolve_credentials(
    motet: Any = None,
    *,
    vault_key: str = DEFAULT_VAULT_KEY,
    require_host: bool = True,
) -> Dict[str, str]:
    """
    Resolve Langfuse Cloud credentials from vault then env.

    Vault credential id/key defaults to ``langfuse`` with JSON fields
    ``public_key``, ``secret_key``, and ``host``.
    """
    if motet is not None:
        vault = getattr(motet, "vault", None)
        context = getattr(motet, "distributed_context", None) or getattr(motet, "context", None)
        if vault is not None and context is not None and hasattr(vault, "get_credential"):
            try:
                raw = vault.get_credential(credential_key=vault_key, context=context)
            except TypeError:
                raw = vault.get_credential(vault_key, context)
            except Exception:
                raw = None
            if isinstance(raw, dict) and raw:
                # Some vault payloads nest under "data" / "secret"
                nested = raw.get("data") if isinstance(raw.get("data"), dict) else None
                if nested is None and isinstance(raw.get("secret"), dict):
                    nested = raw["secret"]
                try:
                    return credentials_from_mapping(nested or raw, require_host=require_host)
                except LangfuseConfigError:
                    pass

    env_creds = credentials_from_env(require_host=require_host)
    if env_creds:
        return env_creds

    raise LangfuseConfigError(
        f"No Langfuse credentials found (vault key '{vault_key}' or "
        "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST)"
    )


def extract_system_prompt_text(prompt_payload: Dict[str, Any]) -> str:
    """
    Extract a system-prompt string from a Langfuse prompt API response.

    Supports ``type=text`` (string prompt) and ``type=chat`` (message list).
    For chat prompts, prefers the first system message; otherwise joins
    all message contents.
    """
    prompt = prompt_payload.get("prompt")
    prompt_type = (prompt_payload.get("type") or "").lower()

    if isinstance(prompt, str):
        return prompt.strip()

    if isinstance(prompt, list):
        system_parts: List[str] = []
        all_parts: List[str] = []
        for item in prompt:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            all_parts.append(content.strip())
            if (item.get("role") or "").lower() == "system":
                system_parts.append(content.strip())
        if system_parts:
            return "\n\n".join(system_parts)
        if all_parts:
            return "\n\n".join(all_parts)

    if prompt_type == "text" and prompt is not None:
        return str(prompt).strip()

    raise LangfuseAPIError("Langfuse prompt payload had no usable text/chat content")


def _auth(creds: Dict[str, str]) -> tuple[str, str]:
    return creds["public_key"], creds["secret_key"]


def _request(
    creds: Dict[str, str],
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Any:
    url = f"{creds['host']}{path}"
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method,
                url,
                params=params,
                json=json_body,
                auth=_auth(creds),
                headers=request_headers,
            )
    except httpx.HTTPError as exc:
        raise LangfuseAPIError(f"Langfuse request failed: {exc}") from exc

    if response.status_code >= 400:
        body: Any
        try:
            body = response.json()
        except Exception:
            body = response.text
        raise LangfuseAPIError(
            f"Langfuse API {method} {path} returned {response.status_code}",
            status_code=response.status_code,
            body=body,
        )

    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except Exception:
        return {"raw": response.text}


def get_prompt(
    creds: Dict[str, str],
    name: str,
    *,
    label: str = DEFAULT_LABEL,
    version: Optional[int] = None,
) -> Dict[str, Any]:
    """GET /api/public/v2/prompts/{name}."""
    params: Dict[str, Any] = {}
    if version is not None:
        params["version"] = version
    else:
        params["label"] = label
    encoded = quote(name, safe="")
    result = _request(creds, "GET", f"/api/public/v2/prompts/{encoded}", params=params)
    if not isinstance(result, dict):
        raise LangfuseAPIError("Unexpected Langfuse get_prompt response")
    return result


def list_prompts(
    creds: Dict[str, str],
    *,
    name: Optional[str] = None,
    label: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
) -> Dict[str, Any]:
    """GET /api/public/v2/prompts."""
    params: Dict[str, Any] = {"page": page, "limit": limit}
    if name:
        params["name"] = name
    if label:
        params["label"] = label
    result = _request(creds, "GET", "/api/public/v2/prompts", params=params)
    if not isinstance(result, dict):
        raise LangfuseAPIError("Unexpected Langfuse list_prompts response")
    return result


def create_prompt_version(
    creds: Dict[str, str],
    *,
    name: str,
    prompt: Any,
    prompt_type: str = "text",
    labels: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """POST /api/public/v2/prompts — creates a new version (and labels if given)."""
    body: Dict[str, Any] = {
        "name": name,
        "type": prompt_type,
        "prompt": prompt,
    }
    if labels:
        body["labels"] = labels
    if config:
        body["config"] = config
    result = _request(creds, "POST", "/api/public/v2/prompts", json_body=body)
    if not isinstance(result, dict):
        raise LangfuseAPIError("Unexpected Langfuse create_prompt response")
    return result


def fetch_system_prompt(
    creds: Dict[str, str],
    *,
    name: str = DEFAULT_PROMPT_NAME,
    label: str = DEFAULT_LABEL,
) -> Tuple[str, Dict[str, Any]]:
    """
    Fetch prompt text and metadata from Langfuse Cloud.

    Returns:
        (system_prompt_text, meta) where meta includes name, label, version, type.
    """
    payload = get_prompt(creds, name, label=label)
    text = extract_system_prompt_text(payload)
    meta = {
        "name": payload.get("name") or name,
        "label": label,
        "version": payload.get("version"),
        "type": payload.get("type"),
        "source": "langfuse",
    }
    return text, meta


def _otlp_attribute(key: str, value: Any) -> Optional[Dict[str, Any]]:
    """One OTLP KeyValue, or None for values Langfuse cannot use."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        # OTLP/JSON encodes int64 as a string.
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, (dict, list)):
        return {"key": key, "value": {"stringValue": json.dumps(value, default=str)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _first_int(source: Dict[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


def record_generation(
    creds: Dict[str, str],
    *,
    model: str,
    input_messages: List[Dict[str, Any]],
    output: str,
    usage: Optional[Dict[str, Any]] = None,
    cost_usd: Optional[float] = None,
    name: str = "langfuse-cms.turn",
    metadata: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    prompt_name: Optional[str] = None,
    prompt_version: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send one generation to Langfuse as an OTLP span (``POST /api/public/otel/v1/traces``).

    OTLP rather than the older ``/api/public/ingestion`` batch endpoint for two
    reasons. Ingestion is deprecated ahead of Langfuse v4, and a bare
    ``generation-create`` produced an observation whose trace never existed — the
    Traces page stayed empty and rendered its "connect your app" onboarding, which
    reads like a credential problem. A span implies its trace, so the turn is
    visible where users look for it.

    Cost goes in ``gen_ai.usage.cost``, the attribute Langfuse maps to a
    generation's own cost field. ``langfuse.observation.cost_details`` is only
    parsed for spans emitted by the Langfuse SDK (scope name ``langfuse-sdk*``),
    so it is silently dropped for a plain OTLP exporter like this one.

    ``session_id`` (a Motet conversation id) groups a conversation's turns into one
    Langfuse session. Failures are raised to the caller; hooks catch and continue.
    """
    # A span id is 8 bytes and a trace id 16, both hex in OTLP/JSON.
    span_id = uuid.uuid4().hex[:16]
    otel_trace_id = (trace_id or uuid.uuid4().hex).replace("-", "")[:32].rjust(32, "0")

    now_ns = time.time_ns()
    attributes: List[Dict[str, Any]] = []

    def add(key: str, value: Any) -> None:
        attribute = _otlp_attribute(key, value)
        if attribute is not None:
            attributes.append(attribute)

    add("langfuse.observation.type", "generation")
    add("langfuse.trace.name", name)
    add("langfuse.observation.model.name", model)
    # gen_ai.* alongside the langfuse.* names so the span still resolves a model
    # if read by a generic OTel GenAI consumer.
    add("gen_ai.request.model", model)
    add("langfuse.observation.input", input_messages)
    add("langfuse.observation.output", output)
    add("langfuse.session.id", session_id)
    add("langfuse.user.id", user_id)
    add("langfuse.observation.prompt.name", prompt_name)
    add("langfuse.observation.prompt.version", prompt_version)

    if cost_usd is not None:
        add("gen_ai.usage.cost", float(cost_usd))

    if usage:
        add(
            "gen_ai.usage.input_tokens",
            _first_int(usage, "prompt_tokens", "input", "input_tokens"),
        )
        add(
            "gen_ai.usage.output_tokens",
            _first_int(usage, "completion_tokens", "output", "output_tokens"),
        )

    # The langfuse.observation.metadata prefix lifts keys to top-level metadata,
    # where they are filterable; unprefixed attributes land in an opaque catch-all.
    for key, value in (metadata or {}).items():
        add(f"langfuse.observation.metadata.{key}", value)

    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        attr
                        for attr in (
                            _otlp_attribute("service.name", "motet"),
                            _otlp_attribute("service.namespace", "langfuse-cms"),
                        )
                        if attr is not None
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "motet.langfuse-cms"},
                        "spans": [
                            {
                                "traceId": otel_trace_id,
                                "spanId": span_id,
                                "name": name,
                                "kind": OTEL_SPAN_KIND_CLIENT,
                                "startTimeUnixNano": str(now_ns),
                                "endTimeUnixNano": str(now_ns),
                                "attributes": attributes,
                                "status": {"code": OTEL_STATUS_CODE_OK},
                            }
                        ],
                    }
                ],
            }
        ]
    }

    result = _request(
        creds,
        "POST",
        OTEL_TRACES_PATH,
        json_body=payload,
        # Opt into v4 real-time ingestion so the turn shows up immediately
        # instead of on the next batch flush.
        headers={"x-langfuse-ingestion-version": "4"},
    )
    return {
        "trace_id": otel_trace_id,
        "observation_id": span_id,
        "endpoint": OTEL_TRACES_PATH,
        "ingestion_response": result,
    }


def resolve_credentials_from_motet_or_env(motet: Any) -> Dict[str, str]:
    """Convenience alias used by tools."""
    return resolve_credentials(motet, require_host=True)


def resolve_turn_system_prompt(
    motet: Any = None,
    *,
    prompt_name: str = DEFAULT_PROMPT_NAME,
    prompt_label: str = DEFAULT_LABEL,
    vault_key: str = DEFAULT_VAULT_KEY,
) -> Dict[str, Any]:
    """
    Resolve the effective system prompt for a turn (Langfuse Cloud or fallback).

    Never raises — callers (context_inject / wrapper) always get a usable
    ``system_prompt`` string plus ``prompt_source`` / ``fallback_reason``.
    """
    try:
        creds = resolve_credentials(motet, vault_key=vault_key, require_host=True)
    except LangfuseConfigError as exc:
        return {
            "system_prompt": FALLBACK_SYSTEM_PROMPT,
            "prompt_source": "fallback",
            "fallback_reason": str(exc),
            "creds": None,
            "prompt_meta": {"name": prompt_name, "label": prompt_label},
        }

    try:
        text, meta = fetch_system_prompt(creds, name=prompt_name, label=prompt_label)
        if not text.strip():
            return {
                "system_prompt": FALLBACK_SYSTEM_PROMPT,
                "prompt_source": "fallback",
                "fallback_reason": "Langfuse prompt was empty",
                "creds": creds,
                "prompt_meta": meta,
            }
        return {
            "system_prompt": text,
            "prompt_source": "langfuse",
            "fallback_reason": None,
            "creds": creds,
            "prompt_meta": meta,
        }
    except Exception as exc:
        return {
            "system_prompt": FALLBACK_SYSTEM_PROMPT,
            "prompt_source": "fallback",
            "fallback_reason": f"{type(exc).__name__}: {exc}",
            "creds": creds,
            "prompt_meta": {"name": prompt_name, "label": prompt_label},
        }
