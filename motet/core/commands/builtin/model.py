"""
Motet - Distributed Model Commands

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Distributed model inference command system for the Motet distributed framework.
    Provides unified distributed commands for model inference, streaming, and embeddings
    through distributed workers. Includes model settings management, provider support,
    high-concurrency worker optimization, native function calling support,
    finish_reason extraction for stopping condition detection, and unified task-level
    streaming pattern. Adapter citations (OpenAI, Grok, DeepSeek, …) are
    forwarded on inference and stream results so ``core.web_search`` can keep
    the native LLM path.

    Request context:
    - Multimodal rendering requires request-scoped identity/isolation context to safely fetch artifacts.
    - This is carried via `request_context` (tenant/principal/motet, budgets), kept separate from
    `model_settings` (temperature/max_tokens/etc.).

    Output limits:
    - When request/profile omit max_tokens, effective settings fill from
    ModelSpec.max_output_tokens so adapters do not invent a magic 8000.

Dependencies:
    - os: Environment variable management
    - time: Timestamp and performance tracking
    - typing: Type hints and annotations
    - Distributed command system
    - Model inference and streaming

Usage:
    from motet.core.commands.builtin.model import model_inference, model_stream
    from motet.core.commands.command_data_classes import ModelInferenceData, ModelStreamData
    from motet.core.types import Message, RequestContext
    
    # Model inference (decorator-based distributed command)
    result = motet.do(
        model_inference,
        data=ModelInferenceData(
            messages=[Message(role="user", content="Hello")],
            model_settings={"provider": "openai", "model_name": "gpt-4o-mini", "temperature": 0.7},
            request_context=RequestContext(tenant_id="default", principal_id="user-123"),
        ),
    )
    
    # Model inference with native function calling (ADR-0045)
    result = motet.do(
        model_inference,
        data=ModelInferenceData(
            messages=[Message(role="user", content="What's the weather?")],
            model_settings={"provider": "openai", "model_name": "gpt-4o"},
            tools=[{"type": "function", "function": {...}}],
            request_context=RequestContext(tenant_id="default", principal_id="user-123"),
        ),
    )
    
    # Model streaming (writes tokens to Redis stream via motet.stream_token inside the command)
    result = motet.do(
        model_stream,
        data=ModelStreamData(
            messages=[Message(role="user", content="Hello")],
            stream_key="task:task-123:response",
            model_settings={"provider": "openai", "model_name": "gpt-4o-mini", "temperature": 0.2},
            request_context=RequestContext(tenant_id="default", principal_id="user-123"),
        ),
    )

Notes:
    - Supports multiple model providers (OpenAI, Anthropic, etc.)
    - Includes model inference, streaming, and embedding operations
    - Provides high-concurrency worker optimization for model operations
    - Supports model settings management and provider configuration
    - Includes comprehensive error handling and retry logic
    - Integrates with distributed worker routing and capability management
    - Supports both synchronous and streaming model operations
    - Native function calling support for tool discovery
    - Citations from any adapter are included on inference and stream results
"""


import os
import time
import json
import base64
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type, TYPE_CHECKING

import structlog

from motet import motet
from motet.core.commands.distributed import DistributedCommand, DistributedCommandContext

from motet.core.commands.capabilities import WorkerCapability
from motet.core.commands.decorator import get_motet_context
from motet.core.commands.command_data_classes import ModelInferenceData, ModelStreamData, EmbeddingData, ImageGenerationData
from motet.core.types import Message, RequestContext, tool_schema_name
from motet.core.workers.observers import EventPriority
from motet.core.models.adapters.provider_builtin_tools import tool_canonical_to_wire

if TYPE_CHECKING:
    from motet.core.types import CanonicalToolSchema, StopReason


@dataclass
class StreamEventResult:
    """Result of processing a single stream event."""
    should_break: bool = False
    tokens_streamed: int = 0
    final_content: str = ""
    tool_calls_canonical: Optional[List[Dict[str, Any]]] = None
    citations: Optional[List[Dict[str, Any]]] = None
    finish_reason: str = "stop"
    reasoning_delta: str = ""
    # ADR-0064 R10: Opaque provider reasoning blocks from the final ThinkingEvent
    # (e.g. OpenAI encrypted reasoning items) for stateless multi-turn replay.
    reasoning_blocks: Optional[List[Dict[str, Any]]] = None



logger = structlog.get_logger(__name__)


# ================================================================================================
# ADR-0062: Multimodal request context augmentation (provider-agnostic trigger)
# ================================================================================================

def _has_image_content_parts(messages: List[Message]) -> bool:
    """
    Detect whether any message contains an image part in `content_parts`.

    Notes:
        - We intentionally avoid importing image-specific classes here to keep this command layer lightweight.
        - This works with both typed parts (Pydantic models with `.type`) and dict-shaped parts.
    """
    for m in messages:
        parts = getattr(m, "content_parts", None) or []
        for p in parts:
            p_type = getattr(p, "type", None)
            if p_type is None and isinstance(p, dict):
                p_type = p.get("type")
            # ADR-0064: MediaPart generalization
            if p_type == "media":
                media_type = getattr(p, "media_type", None)
                if media_type is None and isinstance(p, dict):
                    media_type = p.get("media_type")
                if media_type == "image":
                    return True
    return False


def _tool_names_from(tools: Optional[List[Any]]) -> List[str]:
    """Extract tool names from canonical schemas or dicts for observability.

    Used to report the agent tools actually forwarded to the model, which the
    provider built-in ``tools``/``tools_enabled`` flags do not capture (those are
    provider-native built-ins only; prompt-injected local tools read as false).
    """
    return [name for name in (tool_schema_name(t) for t in tools or []) if name]


def _tools_to_canonical(
    tools: Optional[List[Any]],
    *,
    strict: bool = True,
) -> Optional[List["CanonicalToolSchema"]]:
    """Accept CanonicalToolSchema or canonical-like ``{name, json_schema}`` dicts (ADR-0137).

    Provider shapes (Chat Completions ``function.name``, Responses ``type=function``,
    Anthropic ``input_schema``) are rejected. Adapters own those wires.
    """

    if not tools:
        return None

    from motet.core.types import CanonicalToolSchema

    out: List[CanonicalToolSchema] = []
    for t in tools:
        if isinstance(t, CanonicalToolSchema):
            out.append(t)
            continue
        if not isinstance(t, dict):
            if strict:
                raise ValueError(
                    f"Non-dict tool schema is not allowed in strict_canonical_tools mode: {type(t).__name__}"
                )
            continue

        if isinstance(t.get("name"), str) and isinstance(t.get("json_schema"), dict):
            out.append(
                CanonicalToolSchema(
                    name=t["name"],
                    description=t.get("description") or "",
                    json_schema=t["json_schema"],
                )
            )
            continue

        if strict:
            raise ValueError(
                "Unrecognized tool schema dict in strict_canonical_tools mode. "
                f"Pass CanonicalToolSchema or {{name, json_schema}}. Keys={sorted(list(t.keys()))}"
            )

    return out or None


def _citations_payload(llm_resp: Any) -> Optional[List[Dict[str, Any]]]:
    """Serialize adapter citations for the command result (any Responses host)."""
    citations = getattr(llm_resp, "citations", None) or []
    if not citations:
        return None
    return [c.model_dump() for c in citations]


def _stop_reason_to_finish_reason(stop_reason: "StopReason") -> str:
    """Map canonical StopReason to legacy OpenAI-ish finish_reason strings for compatibility."""

    from motet.core.types import StopReason as CanonicalStopReason

    if stop_reason == CanonicalStopReason.TOOL_CALLS:
        return "tool_calls"
    if stop_reason == CanonicalStopReason.LENGTH_LIMIT:
        return "length"
    if stop_reason == CanonicalStopReason.SAFETY_FILTER:
        return "content_filter"
    if stop_reason == CanonicalStopReason.STOP_SEQUENCE:
        return "stop"
    return "stop"


def _build_request_context_for_multimodal(
    *,
    motet: Any,
    messages: List[Message],
    provider: Optional[str],
    model_name: Optional[str],
    request_context: Optional[RequestContext],
) -> Optional[RequestContext]:
    """
    Build/augment request_context for multimodal rendering (ADR-0062).

    Args:
        motet: MotetContext for identity/tracing extraction.
        messages: List of messages to check for image content parts.
        provider: Model provider name (e.g., "openai").
        model_name: Model name (e.g., "gpt-4o").
        request_context: Existing RequestContext or None.

    Returns:
        A typed RequestContext with multimodal enabled and identity populated,
        or the original request_context if multimodal is not applicable.

    Notes:
        - This keeps `model_settings` model-only (temperature, max_tokens, etc.).
        - Identity/isolation/budgets live in RequestContext and are consumed by providers/renderers.
    """
    from motet.core.types import RequestContext

    if not provider or not model_name:
        return request_context

    if not _has_image_content_parts(messages):
        return request_context

    # Only auto-enable when the chosen model supports vision.
    try:
        from motet.core.models.specs import CAP_VISION
        from motet.core.models.registry import model_supports

        if not model_supports(provider, model_name, CAP_VISION):
            return request_context
    except Exception as e:
        # Vision check is optional; fail-safe without multimodal
        logger.debug("vision_capability_check_failed", error=str(e))
        return request_context

    # Build update dict with identity, tracing, isolation, and multimodal budgets
    updates: Dict[str, Any] = {}

    # Identity / tenancy (fail-closed in provider if missing)
    if not (request_context and request_context.tenant_id):
        updates["tenant_id"] = getattr(motet, "tenant_id", None)
    if not (request_context and request_context.principal_id):
        updates["principal_id"] = getattr(motet, "principal_id", None)
    if not (request_context and request_context.motet_id):
        updates["motet_id"] = getattr(motet, "motet_id", None)
    if not (request_context and request_context.conversation_id):
        updates["conversation_id"] = getattr(motet, "conversation_id", None)

    # Tracing / observability
    if not (request_context and request_context.task_id):
        updates["task_id"] = getattr(motet, "task_id", None)
    if not (request_context and request_context.command_id):
        updates["command_id"] = getattr(motet, "command_id", None)
    try:
        dctx = getattr(motet, "distributed_context", None)
        if dctx is not None:
            if not (request_context and request_context.parent_command_id):
                updates["parent_command_id"] = getattr(dctx, "parent_command_id", None)
            if not (request_context and request_context.trace_id):
                updates["trace_id"] = getattr(dctx, "trace_id", None)
            # Isolation semantics
            if not (request_context and request_context.tenant_isolation_required is not True):
                updates["tenant_isolation_required"] = bool(getattr(dctx, "tenant_isolation_required", True))
            if not (request_context and request_context.worker_security_level != "standard"):
                updates["worker_security_level"] = getattr(dctx, "worker_security_level", "standard")
    except Exception as e:
        # Best-effort distributed-context enrichment; skip if motet/dctx raises
        logger.debug("distributed_context_enrichment_failed", error=str(e))

    # Multimodal enable + budgets
    updates["enable_multimodal"] = True
    if not (request_context and request_context.max_images != 8):
        updates["max_images"] = 8
    if (provider or "").strip().lower() == "openai":
        if not (request_context and request_context.max_image_bytes != 20 * 1024 * 1024):
            updates["max_image_bytes"] = 20 * 1024 * 1024

    # Create new or augment existing RequestContext
    if request_context is None:
        return RequestContext(**updates)
    else:
        return request_context.model_copy(update=updates)


def _ensure_request_context_identity(
    *,
    motet: Any,
    request_context: Optional[RequestContext],
) -> Optional[RequestContext]:
    """
    Ensure request_context includes identity/isolation/tracing fields from MotetContext.

    Why:
        - ADR-0064 ModelProfiles are tenant-scoped and require a tenant_id to load from Redis.
        - Many call-sites omit request_context for non-multimodal requests; without this, profile routing/policy is skipped
          and we fall back to ModelSpec (observed via adapter_selection_source="model_spec").

    Notes:
        - This does NOT enable multimodal; that is handled separately by _build_request_context_for_multimodal.
        - We only fill missing fields; we do not overwrite explicitly provided request_context values.
    """

    from motet.core.types import RequestContext as _RequestContext

    updates: Dict[str, Any] = {}

    # Identity / tenancy
    if not (request_context and request_context.tenant_id):
        updates["tenant_id"] = getattr(motet, "tenant_id", None)
    if not (request_context and request_context.principal_id):
        updates["principal_id"] = getattr(motet, "principal_id", None)
    if not (request_context and request_context.motet_id):
        updates["motet_id"] = getattr(motet, "motet_id", None)
    if not (request_context and request_context.conversation_id):
        updates["conversation_id"] = getattr(motet, "conversation_id", None)

    # Tracing / correlation
    if not (request_context and request_context.task_id):
        updates["task_id"] = getattr(motet, "task_id", None)
    if not (request_context and request_context.command_id):
        updates["command_id"] = getattr(motet, "command_id", None)
    try:
        dctx = getattr(motet, "distributed_context", None)
        if dctx is not None:
            if not (request_context and request_context.parent_command_id):
                updates["parent_command_id"] = getattr(dctx, "parent_command_id", None)
            if not (request_context and request_context.trace_id):
                updates["trace_id"] = getattr(dctx, "trace_id", None)
            if request_context is None:
                updates["tenant_isolation_required"] = bool(getattr(dctx, "tenant_isolation_required", True))
                updates["worker_security_level"] = getattr(dctx, "worker_security_level", "standard")
    except Exception as e:
        # Best-effort distributed-context enrichment; skip if motet/dctx raises
        logger.debug("distributed_context_identity_enrichment_failed", error=str(e))

    # Nothing to do
    if not any(v is not None for v in updates.values()):
        return request_context

    if request_context is None:
        return _RequestContext(**updates)
    return request_context.model_copy(update=updates)


def _apply_model_profile_name(
    *,
    motet: Any,
    request_context: Optional[RequestContext],
    cfg: Any,
) -> Optional[RequestContext]:
    """
    Apply ADR-0064 ModelProfile selection to request_context.

    Precedence:
        1) Explicit request_context.model_profile_name (per-call)
        2) Distributed command metadata hint: motet.metadata["model_profile_name"] (propagates across sub-commands)
        3) Scheduled-default profile when running under a schedule (cfg.scheduled_model_profile_name)
        4) Otherwise: no-op (global cfg.model_profile_name will be used as fallback)
    """

    # If explicitly set on request_context, honor it.
    rc_name = str(getattr(request_context, "model_profile_name", "") or "").strip() if request_context else ""
    if rc_name:
        return request_context

    # Metadata hint from parent commands (e.g., agent_turn, schedule command).
    meta = getattr(motet, "metadata", None)
    if isinstance(meta, dict):
        hinted = str(meta.get("model_profile_name") or "").strip()
        if hinted:
            if request_context is None:
                return RequestContext(model_profile_name=hinted)
            return request_context.model_copy(update={"model_profile_name": hinted})

    # Scheduled-default (policy)
    dctx = getattr(motet, "distributed_context", None)
    is_scheduled = False
    try:
        if dctx is not None:
            # Best-effort: schedule_type/schedule_name are set by the scheduling system.
            is_scheduled = bool(getattr(dctx, "schedule_type", None) or getattr(dctx, "schedule_name", None))
    except Exception as e:
        # Best-effort; treat as not scheduled if dctx access fails
        logger.debug("schedule_detection_failed", error=str(e))
        is_scheduled = False

    if is_scheduled:
        scheduled_profile = str(getattr(cfg, "scheduled_model_profile_name", "") or "").strip()
        if scheduled_profile:
            if request_context is None:
                return RequestContext(model_profile_name=scheduled_profile)
            return request_context.model_copy(update={"model_profile_name": scheduled_profile})

    return request_context


def _get_provider_credentials(
    *,
    motet: Any,
    provider: Optional[str],
) -> Optional[Dict[str, str]]:
    """
    Resolve provider credentials (vault -> config fallback).

    Notes:
        - Avoid logging secrets.
        - Best-effort: returns None if not found.
    """
    if not provider or provider == "local":
        return None

    # Vault first
    vault_client = getattr(motet, "vault", None)
    if vault_client:
        try:
            dctx = getattr(getattr(motet, "_command", None), "distributed_context", None)
            api_key = vault_client.get_api_key(provider, dctx)
            if api_key:
                logger.debug("model_provider_api_key_from_vault", provider=provider)
                return {f"{provider}_api_key": api_key}
            logger.debug("model_provider_api_key_missing_in_vault", provider=provider)
        except Exception as e:
            logger.warning(
                "model_provider_api_key_vault_lookup_failed",
                provider=provider,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )

    # Config fallback
    try:
        from motet.core.config import Config

        cfg = Config()
        api_key_attr = f"{provider}_api_key"
        api_key = getattr(cfg, api_key_attr, None)
        if api_key:
            logger.debug("model_provider_api_key_from_config", provider=provider)
            return {f"{provider}_api_key": api_key}
    except Exception as e:
        logger.warning(
            "model_provider_api_key_config_lookup_failed",
            provider=provider,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )

    return None


def _normalize_adapter_credentials(
    provider: str,
    credentials: Optional[Dict[str, str]],
    spec: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Normalize credentials for adapter consumption.
    
    Adapters expect both 'api_key' and '{provider}_api_key' keys for compatibility.
    This helper ensures credentials are in the expected format.
    When spec.base_url is set (e.g. for OpenAI-compatible providers), it is merged in
    so the same model can be served by different hosts via different specs.
    
    Args:
        provider: The provider name (openai, anthropic, moonshot, etc.)
        credentials: Raw credentials dict from _get_provider_credentials
        spec: Optional ModelSpec; if present and spec.base_url is set, it is used (env override for moonshot only)
        
    Returns:
        Normalized credentials dict with both generic and provider-specific keys
    """
    if not credentials:
        out = {}
    else:
        provider_key = f"{provider}_api_key"
        api_key = credentials.get(provider_key) or credentials.get("api_key")
        if not api_key:
            out = dict(credentials)
        else:
            out = {
                "api_key": api_key,
                provider_key: api_key,
                **credentials,
            }
    # base_url: prefer ModelSpec (same model, different host via different spec/alias)
    base_url = None
    if spec is not None and getattr(spec, "base_url", None):
        base_url = spec.base_url
    if base_url is None and provider == "moonshot":
        import os
        base_url = os.getenv("MOONSHOT_API_BASE", "https://api.moonshot.ai/v1")
    if base_url is None and provider == "xai":
        import os
        base_url = os.getenv("XAI_API_BASE", "https://api.x.ai/v1")
    if base_url is None and provider == "deepseek":
        import os
        base_url = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    if base_url is None and provider == "meta":
        import os
        base_url = os.getenv("META_API_BASE", "https://api.meta.ai/v1")
    if base_url:
        out["base_url"] = base_url
    return out


def _load_route_override_if_enabled(
    *,
    provider: str,
    model_name: str,
    request_context: Optional[RequestContext],
    cfg: Any,
) -> Any:
    """
    Best-effort load of ModelProfile route override for (tenant_id, profile_name).

    Returns:
        A ModelRouteOverride (or None) from `motet.core.models.profiles.resolve_route_override`.
    """
    if not bool(getattr(cfg, "enable_model_profiles", False)):
        return None
    if not bool(getattr(request_context, "tenant_id", None)):
        return None

    from motet.core.models.profiles import load_model_profile_sync, resolve_route_override

    profile = load_model_profile_sync(
        tenant_id=str(getattr(request_context, "tenant_id") or ""),
        profile_name=str(
            str(getattr(request_context, "model_profile_name", "") or "").strip()
            or getattr(cfg, "model_profile_name", "default")
        ),
    )
    return resolve_route_override(profile, provider=provider, model_name=model_name)


def _select_adapter_and_effective_model_settings(
    *,
    provider: str,
    model_name: str,
    model_settings: Dict[str, Any],
    request_context: Optional[RequestContext],
    cfg: Any,
) -> Any:
    """
    Apply ADR-0064 adapter routing precedence:
    request override -> model profile -> ModelSpec -> env default.

    Returns:
        (selection, effective_model_settings, route_override)
    """
    from motet.core.models.adapters.routing import select_adapter_name
    from motet.core.models.registry import get_model_spec

    spec = get_model_spec(provider, model_name)
    route_override = _load_route_override_if_enabled(
        provider=provider,
        model_name=model_name,
        request_context=request_context,
        cfg=cfg,
    )

    # Merge default model_settings (spec/profile) below request model_settings (request wins).
    effective_model_settings: Dict[str, Any] = dict(getattr(spec, "default_model_settings", None) or {})
    if route_override is not None:
        effective_model_settings.update(getattr(route_override, "model_settings", None) or {})
    effective_model_settings.update(model_settings or {})

    # Resolve model_name from ModelSpec so alias registry keys (e.g. "claude-sonnet-4.5")
    # are mapped to the real API model name (e.g. "claude-sonnet-4-5-20250929").
    if spec and spec.name:
        effective_model_settings["model_name"] = spec.name

    # When callers omit max_tokens (common for Cursor / OpenAI-compat), use the
    # ModelSpec capacity ceiling instead of leaving adapters to invent 8000.
    from motet.core.models.output_limits import apply_max_tokens_from_spec

    apply_max_tokens_from_spec(effective_model_settings, spec)

    # ADR-0064: Only request reasoning when caller explicitly sets enable_thinking=True (e.g. from chat UI).
    # Do NOT auto-enable for reasoning-capable models; otherwise every model call (analysis, discovery, etc.)
    # would request reasoning and the API would return it even when the user turned thinking off.
    #
    # Guard: if enable_thinking=True but the model spec lacks CAP_REASONING, strip it before it reaches
    # the adapter. Without this, models like claude-3-5-sonnet-latest (no CAP_REASONING) get
    # thinking: {enabled} sent to the API, which returns a 400 and silently kills the stream.
    if effective_model_settings.get("enable_thinking"):
        from motet.core.models.specs import CAP_REASONING
        if spec is None:
            # Model not in registry — no capability info available. Strip thinking to avoid a
            # 400 from providers that don't support it (e.g. claude-3-5-sonnet-latest).
            logger.warning(
                "enable_thinking_stripped_model_not_in_registry",
                provider=provider,
                model_name=model_name,
                note="Register the model with CAP_REASONING in specs.py to enable thinking.",
            )
            effective_model_settings["enable_thinking"] = False
        elif CAP_REASONING not in (spec.capabilities or set()):
            logger.warning(
                "enable_thinking_stripped_model_lacks_reasoning_capability",
                provider=provider,
                model_name=model_name,
                spec_name=spec.name,
                note="Set enable_thinking=True only with models that have CAP_REASONING (e.g. claude-sonnet-4, claude-opus-4, kimi-k2.5, kimi-k3).",
            )
            effective_model_settings["enable_thinking"] = False

    profile_override_obj: Optional[Dict[str, Any]] = None
    if route_override is not None and getattr(route_override, "adapter", None):
        profile_override_obj = {"adapter": getattr(route_override, "adapter")}

    selection = select_adapter_name(
        provider=provider,
        model_name=model_name,
        model_settings=effective_model_settings,
        profile_override=profile_override_obj,
    )
    return selection, effective_model_settings, route_override, spec


def _resolve_provider_and_model(
    cfg: Any,
    provider: Optional[str],
    model_name: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """
    Resolve provider/model_name with Config defaults for partially specified settings.

    Args:
        cfg: Config instance with provider defaults.
        provider: Explicit provider (optional).
        model_name: Explicit model name (optional).

    Returns:
        (provider, model_name) after applying defaults.
    """
    if not provider and model_name:
        provider = "openai"
    if provider and not model_name:
        if provider == "openai":
            model_name = cfg.openai_model_name or cfg.model_name
        elif provider == "anthropic":
            model_name = cfg.anthropic_model_name or cfg.model_name
        elif provider == "gemini":
            model_name = cfg.gemini_model_name or cfg.model_name
        elif provider == "mock":
            model_name = "mock-small"
        else:
            model_name = cfg.model_name
    if not provider and not model_name:
        provider = cfg.model_provider
        if provider == "openai":
            model_name = cfg.openai_model_name or cfg.model_name
        elif provider == "anthropic":
            model_name = cfg.anthropic_model_name or cfg.model_name
        elif provider == "gemini":
            model_name = cfg.gemini_model_name or cfg.model_name
        elif provider == "mock":
            model_name = "mock-small"
        else:
            model_name = cfg.model_name
    # MOTET_MODEL_PROVIDER=mock with an OpenAI leftover model_name is a common
    # test misconfig; coerce to the registered mock ModelSpec id.
    if provider == "mock" and model_name:
        name = str(model_name).strip()
        if not name or name.startswith("gpt-") or name.startswith("o1") or name.startswith("o3"):
            model_name = "mock-small"
    return provider, model_name


def _resolve_image_provider_and_model(
    cfg: Any,
    provider: Optional[str],
    model_name: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """
    Resolve provider/model for image generation (ADR-0113).

    Unlike _resolve_provider_and_model (which defaults to the text chat model), this defaults
    to the configured image model (cfg.image_model_provider / cfg.image_model_name). The default
    text model cannot generate images, so callers that omit model_settings (e.g. the
    core.image_generation tool) must resolve to an image-capable model instead.

    Args:
        cfg: Config instance with image-model defaults.
        provider: Explicit provider (optional).
        model_name: Explicit model name (optional).

    Returns:
        (provider, model_name) after applying image-model defaults.
    """
    if not provider and not model_name:
        return cfg.image_model_provider, cfg.image_model_name
    if provider and not model_name:
        # Provider given without a model: use the configured image model only when the
        # provider matches; otherwise leave model unset so the caller fails fast.
        if provider == cfg.image_model_provider:
            return provider, cfg.image_model_name
        return provider, None
    if model_name and not provider:
        return cfg.image_model_provider, model_name
    return provider, model_name


def _resolve_builtin_tools_policy(
    *,
    provider: str,
    cfg: Any,
    route_override: Any,
    adapter: Any,
    adapter_name: str,
    model_name: str,
    spec: Any,
    request_enable_tools: Optional[bool] = None,
) -> Any:
    """
    Determine provider-native built-in tool policy (profile overrides env), then capability-gate.

    Returns:
        (builtin_tool_schemas, builtin_tool_names, tools_configured)
    """
    profile_enable: Optional[bool] = getattr(route_override, "tools_enabled", None) if route_override else None
    profile_allow: Optional[List[str]] = getattr(route_override, "tool_allowlist", None) if route_override else None
    profile_deny: Optional[List[str]] = getattr(route_override, "tool_denylist", None) if route_override else None

    if request_enable_tools is not None:
        effective_enable = bool(request_enable_tools)
    else:
        effective_enable = profile_enable if profile_enable is not None else bool(getattr(cfg, "enable_tools", False))
    tools_configured = bool(getattr(cfg, "enable_tools", False)) or (
        profile_enable is not None or profile_allow is not None or profile_deny is not None
    )

    allowlist_csv = (
        ",".join([s.strip() for s in (profile_allow or []) if isinstance(s, str) and s.strip()])
        if profile_allow is not None
        else getattr(cfg, "tool_allowlist", None)
    )
    denylist_csv = (
        ",".join([s.strip() for s in (profile_deny or []) if isinstance(s, str) and s.strip()])
        if profile_deny is not None
        else getattr(cfg, "tool_denylist", None)
    )

    builtin_tool_names: List[str] = []

    if effective_enable:
        caps = adapter.capabilities(model=model_name)
        if bool(getattr(caps, "supports_builtin_tools", False)):
            from motet.core.models.adapters.provider_builtin_tools import get_provider_builtin_tool_names

            # Get enabled provider builtin tool names (policy-filtered)
            enabled_names = get_provider_builtin_tool_names(
                provider=str(provider or ""),
                allowlist_csv=allowlist_csv,
                denylist_csv=denylist_csv,
            )
            
            # Capability-gate built-ins by ModelSpec.supported_builtin_tools (if present).
            supported = getattr(spec, "supported_builtin_tools", None)
            if isinstance(supported, list):
                supported_set = set(str(x) for x in supported)
                enabled_names = [n for n in enabled_names if n in supported_set]
            
            builtin_tool_names = sorted(enabled_names)

    # Return empty schemas list - actual schemas are built in _apply_builtin_tools
    return [], builtin_tool_names, tools_configured


def _apply_builtin_tools(
    *,
    provider: str,
    model_name: str,
    canonical_tools: Optional[List[Any]],
    cfg: Any,
    route_override: Any,
    adapter: Any,
    adapter_name: str,
    spec: Any,
    request_enable_tools: Optional[bool] = None,
    request_tools_explicitly_empty: bool = False,
) -> tuple[List[Any], bool, List[str], bool]:
    # `tools=[]` (as opposed to tools=None) is a caller's explicit "no tools"
    # declaration — e.g. the adaptive no-tools fast path. Merging provider
    # built-ins (server-side web_search) would silently re-arm tools AND bill
    # the tool definition (~2.5k prompt tokens on Anthropic) on every call.
    if request_tools_explicitly_empty:
        return [], False, [], False

    _, builtin_tool_names, tools_configured = _resolve_builtin_tools_policy(
        provider=provider,
        cfg=cfg,
        route_override=route_override,
        adapter=adapter,
        adapter_name=adapter_name,
        model_name=model_name,
        spec=spec,
        request_enable_tools=request_enable_tools,
    )
    tools_enabled = False
    tools: List[str] = []
    merged_tools = list(canonical_tools or [])
    
    # Unified web_search (ADR-0064): one canonical "web_search" when any provider has built-in web search
    has_web_search_builtin = any(str(name).endswith(".web_search") for name in builtin_tool_names)
    if has_web_search_builtin:
        # Remove any existing web_search tools to avoid duplicates
        merged_tools = [
            t
            for t in merged_tools
            if getattr(t, "name", None) != "web_search"
            and not str(getattr(t, "name", "")).startswith("mcp.web-search.")
        ]
        # Add unified web_search schema (adapters map this to their wire format)
        from motet.core.models.adapters.provider_builtin_tools import get_unified_web_search_schema
        merged_tools.append(get_unified_web_search_schema())
        tools_enabled = True
        tools = ["web_search"]
        logger.info(
            "enabled_builtin_tools",
            provider=provider,
            model=model_name,
            builtin_tools=tools,
            note="Provider web_search enabled via unified schema (ADR-0064).",
        )
    
    return merged_tools, tools_enabled, tools, tools_configured


def _apply_wire_names(
    tools: Optional[List["CanonicalToolSchema"]],
) -> Optional[List["CanonicalToolSchema"]]:
    """
    Apply wire-format name transformation to all namespaced tool names before they are sent to any
    provider adapter.

    Any canonical dotted name (mcp.server.tool, core.tool_name, bundle_id.tool_name) is converted
    to the double-underscore wire format so all provider adapters receive provider-safe names and
    need no naming logic of their own.
    """
    if not tools:
        return tools
    return [
        t.model_copy(update={"name": tool_canonical_to_wire(t.name)}) if "." in t.name else t
        for t in tools
    ]


def _apply_wire_names_to_messages(
    messages: Optional[List["Message"]],
) -> Optional[List["Message"]]:
    """Apply wire-format names on assistant tool calls and tool-result names (ADR-0137).

    Gemini matches ``function_response.name`` to the declared function name, so
    ``role="tool"`` ``Message.name`` must be wired the same way as the call.
    """
    from motet.core.models.adapters.tool_call_codec import tool_calls_from_message

    if not messages:
        return messages
    result: List["Message"] = []
    for msg in messages:
        calls = tool_calls_from_message(msg)
        if msg.role == "assistant" and calls:
            wired = [
                tc.model_copy(update={"tool_name": tool_canonical_to_wire(tc.tool_name)})
                for tc in calls
            ]
            result.append(msg.model_copy(update={"tool_calls_canonical": wired}))
        elif msg.role == "tool" and msg.name and "." in msg.name:
            result.append(msg.model_copy(update={"name": tool_canonical_to_wire(msg.name)}))
        else:
            result.append(msg)
    return result


def _handle_stream_event(
    *,
    ev: Any,
    motet: Any,
    usage_data: Dict[str, Optional[int]],
    state: StreamEventResult,
    allow_citations: bool,
    error_label: str,
    stream_key: Optional[str] = None,
) -> StreamEventResult:
    """
    Process a single stream event and update state.
    
    Args:
        ev: The stream event to process
        motet: MotetContext for streaming tokens/events
        usage_data: Mutable dict to accumulate usage metrics
        state: Current stream state (will be updated and returned)
        allow_citations: Whether to process citation events
        error_label: Label for error messages (e.g., "Openai")
        stream_key: Optional Redis stream key; when set, tokens/events use this instead of motet.stream_key.
    
    Returns:
        Updated StreamEventResult with should_break=True if stream should end
    """
    from motet.core.types import (
        CitationsEvent,
        ErrorEvent,
        StopEvent,
        TextDeltaEvent,
        ThinkingEvent,
        ToolCallCompleteEvent,
        ToolCallDeltaEvent,
        ToolUseEvent,
        UsageEvent,
    )

    # Reset reasoning_delta for this event (only ThinkingEvent sets it)
    state.reasoning_delta = ""

    if isinstance(ev, TextDeltaEvent):
        token = ev.text
        motet.stream_token(token, stream_key=stream_key)
        state.tokens_streamed += 1
        state.final_content += token
        return state
    
    if allow_citations and isinstance(ev, CitationsEvent):
        state.citations = [c.model_dump() for c in ev.citations]
        motet.stream_event("citations", citations=state.citations, stream_key=stream_key)
        return state
    
    if isinstance(ev, ToolCallDeltaEvent):
        # Progress only: the completed call still arrives as ToolCallCompleteEvent and
        # remains the sole input to state.tool_calls_canonical. Streaming the fragments
        # matters because argument generation can run for minutes (a whole-file write),
        # and a consumer watching only text deltas sees silence for that whole window.
        motet.stream_event(
            "tool_call_delta",
            call_id=ev.call_id,
            tool_name=ev.tool_name,
            arguments_delta=ev.arguments_delta,
            stream_key=stream_key,
        )
        return state

    if isinstance(ev, ToolCallCompleteEvent):
        if state.tool_calls_canonical is None:
            state.tool_calls_canonical = []
        try:
            args_obj = json.loads(ev.arguments_json) if isinstance(ev.arguments_json, str) else None
        except Exception as e:
            # Malformed tool-call args; keep raw string, args_obj=None for compatibility
            logger.debug("tool_call_arguments_json_parse_failed", error=str(e))
            args_obj = None
        tc_dict: Dict[str, Any] = {
            "call_id": ev.call_id,
            "tool_name": ev.tool_name or "",
            "arguments_json": ev.arguments_json,
            "arguments": args_obj,
        }
        # ADR-0064: Include kind for provider-executed builtins
        if getattr(ev, "kind", None):
            tc_dict["kind"] = ev.kind
        # Provider thought signature (e.g. Gemini) — required for replay on later turns
        if getattr(ev, "thought_signature", None):
            tc_dict["thought_signature"] = ev.thought_signature
        state.tool_calls_canonical.append(tc_dict)
        return state
    
    if isinstance(ev, ToolUseEvent):
        motet.stream_event(
            "tool_use",
            kind=ev.kind,
            tool_name=ev.tool_name,
            tool_call_id=ev.tool_call_id,
            status=ev.status,
            metadata=ev.metadata,
            stream_key=stream_key,
        )
        return state
    
    if isinstance(ev, ThinkingEvent):
        motet.stream_event(
            "thinking",
            text=ev.text,
            is_complete=ev.is_complete,
            stream_key=stream_key,
        )
        # ADR-0064 R10: Return reasoning text for persistence (model_stream accumulates)
        state.reasoning_delta = ev.text if isinstance(ev.text, str) else ""
        # Capture opaque provider reasoning blocks (e.g. OpenAI encrypted reasoning items)
        # from the final event for stateless multi-turn replay. Not streamed to clients.
        blocks = getattr(ev, "blocks", None)
        if blocks:
            state.reasoning_blocks = blocks
        return state
    
    if isinstance(ev, UsageEvent):
        if ev.usage:
            usage_data["prompt_tokens"] = ev.usage.prompt_tokens
            usage_data["completion_tokens"] = ev.usage.output_tokens
            usage_data["total_tokens"] = ev.usage.total_tokens
            usage_data["cache_read_tokens"] = ev.usage.cache_read_tokens
            usage_data["cache_creation_tokens"] = ev.usage.cache_creation_tokens
            usage_data["reasoning_tokens"] = ev.usage.reasoning_tokens
        return state
    
    if isinstance(ev, ErrorEvent):
        raise RuntimeError(f"{error_label} stream error ({ev.error_type}): {ev.message}")
    
    if isinstance(ev, StopEvent):
        state.finish_reason = _stop_reason_to_finish_reason(ev.reason)
        if state.tool_calls_canonical:
            state.finish_reason = "tool_calls"
        state.should_break = True
        return state
    
    return state

# ================================================================================================
# DECORATED COMMAND PATTERN (ADR-0030) - RECOMMENDED FOR NEW CODE
# ================================================================================================


def _root_conversation_id_from_motet(motet: Any) -> Optional[str]:
    """Denormalized root for isolated child cost rollup, if present."""
    meta = getattr(motet, "metadata", None)
    if not isinstance(meta, dict):
        return None
    root = str(meta.get("root_conversation_id") or "").strip()
    return root or None


def _track_inference_cost(
    result: Dict[str, Any],
    motet,
) -> Dict[str, Any]:
    """
    Track cost for model inference result (ADR-0018).
    
    Non-blocking, non-critical - failures are logged but don't impact inference.
    
    Args:
        result: Inference result dict with usage data
        motet: MotetContext for tenant/task info
        
    Returns:
        Result dict with cost_usd added
    """
    try:
        from motet.core.cost import track_model_result
        
        cost_usd = track_model_result(
            result,
            tenant_id=motet.tenant_id or "default",
            task_id=motet.task_id,
            conversation_id=motet.conversation_id,
            command_id=motet.command_id,
            principal_id=getattr(motet, "principal_id", None) or None,
            root_conversation_id=_root_conversation_id_from_motet(motet),
        )
        result["cost_usd"] = cost_usd
    except Exception as e:
        # Cost tracking is non-critical - log and continue
        logger.debug("model_inference_cost_tracking_failed", error=str(e))
    
    return result


def _check_budget_before_inference(motet, provider: str, model_name: str) -> None:
    """
    Check budget before model inference (ADR-0018).
    
    Raises BudgetExceededError if budget limit is exceeded.
    Non-blocking for budget warnings (logs but continues).
    
    Args:
        motet: MotetContext for tenant info
        provider: Model provider
        model_name: Model name for cost estimation
    """
    try:
        from motet.core.cost import check_budget_before_inference, BudgetExceededError
        
        tenant_id = motet.tenant_id or "default"
        
        # Check budget - raises BudgetExceededError if blocked
        check_budget_before_inference(
            tenant_id=tenant_id,
            provider=provider,
            model=model_name,
            estimated_tokens=1000,  # Conservative estimate
        )
    except ImportError:
        # Cost module not available
        pass
    except Exception as e:
        # Log budget check failures but don't block inference
        # The BudgetExceededError is re-raised by check_budget_before_inference
        if "BudgetExceededError" in type(e).__name__:
            raise
        logger.debug("budget_check_failed", error=str(e))


@motet.command(
    description="Generate text embeddings for one or more strings via the distributed embedding service.",
    timeout_seconds=30,
    required_capabilities=[WorkerCapability.EMBEDDINGS]
)
def embedding_generation(data: EmbeddingData) -> Dict[str, Any]:
    """
    Generate embeddings for texts using distributed embedding service.
    
    This is the RECOMMENDED way to generate embeddings using the decorator pattern (ADR-0030).
    
    Args:
        data: EmbeddingData with texts and optional model
        
    Returns:
        Dict with embeddings, model, and count
        
    Example:
        result = motet.do(
            embedding_generation,
            data=EmbeddingData(texts=["hello", "world"], model="all-minilm-l6-v2")
        )
    """
    motet = get_motet_context()
    
    # Access embedding service from worker context
    embedding_service = motet._worker_context.get("embedding_service")
    if not embedding_service:
        raise ValueError("Embedding service not available in worker context")
    
    # Generate embeddings synchronously (ADR-0033: gevent/eventlet compatible)
    embeddings = embedding_service.embed_batch(data.texts, model=data.model)
    
    return {
        "embeddings": embeddings,
        "model": data.model,
        "count": len(embeddings)
    }


@motet.command(
    description="Run non-streaming LLM inference via the model registry (chat completion, structured output, embeddings-adjacent text generation).",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE]
)
def model_inference(data: ModelInferenceData) -> Dict[str, Any]:
    """
    Execute model inference using distributed agents or model registry.
    
    This is the RECOMMENDED way to execute model inference using the decorator pattern (ADR-0030).
    
    Supports:
    - API-based inference (OpenAI, Anthropic, etc.) via model registry
    - Local inference (ADR-0042) with GPU support
    - Native function calling (ADR-0045) via tools parameter
    - Automatic credential management via vault
    
    Args:
        data: ModelInferenceData with messages, model settings, and optional tools
        
    Returns:
        Dict with inference results, token counts, and timing
        
    Example:
        result = motet.do(
            model_inference,
            data=ModelInferenceData(
                messages=[Message(role="user", content="Hello")],
                model_settings={
                    "provider": "openai",
                    "model_name": "gpt-4",
                    "temperature": 0.7,
                    "max_tokens": 1000
                }
            )
        )
    """
    motet = get_motet_context()
    start_time = time.time()
    
    # Check if local inference is requested via provider="local" in model_settings (ADR-0042)
    model_settings = data.model_settings or {}
    request_context = data.request_context
    provider = model_settings.get("provider")
    model_name = model_settings.get("model_name")

    # ADR-0064: Canonical-only execution. Always resolve provider/model_name via Config
    # (removes legacy agent_default fallback path).
    from motet.core.config import Config
    cfg = Config()
    
    provider, model_name = _resolve_provider_and_model(cfg, provider, model_name)

    if not provider or not model_name:
        raise ValueError("model_inference requires model_settings.provider and model_settings.model_name")
    
    # ADR-0018: Check budget before inference (raises BudgetExceededError if blocked)
    _check_budget_before_inference(motet, provider, model_name)

    # Ensure identity context exists so tenant-scoped ModelProfiles can be applied even
    # when call-sites omit request_context (ADR-0064).
    request_context = _ensure_request_context_identity(motet=motet, request_context=request_context)
    request_context = _apply_model_profile_name(motet=motet, request_context=request_context, cfg=cfg)

    # ADR-0062: auto-enable multimodal rendering by augmenting request_context (not model_settings)
    request_context = _build_request_context_for_multimodal(
        motet=motet,
        messages=data.messages,
        provider=provider,
        model_name=model_name,
        request_context=request_context,
    )

    logger.info("model_inference_model_selected", provider=provider, model=model_name)

    credentials = _get_provider_credentials(motet=motet, provider=provider)

    # ADR-0064: Adapter routing (request -> model profile -> ModelSpec -> env)
    selection, effective_model_settings, route_override, spec = _select_adapter_and_effective_model_settings(
        provider=str(provider or ""),
        model_name=str(model_name or ""),
        model_settings=dict(model_settings or {}),
        request_context=request_context,
        cfg=cfg,
    )

    # ADR-0064: Unified adapter routing for all providers
    if selection.adapter_name:
        from motet.core.models.adapters import adapter_registry
        from motet.core.types import LLMRequest, ToolCallRequest

        adapter_name = selection.adapter_name
        adapter_provider = selection.provider or provider

        # Normalize credentials for adapter consumption (spec.base_url merged in when set)
        normalized_credentials = _normalize_adapter_credentials(
            adapter_provider, credentials, spec=spec
        )

        adapter = adapter_registry.build(
            adapter_provider,
            adapter_name,
            credentials=normalized_credentials,
        )

        canonical_tools = _tools_to_canonical(data.tools, strict=getattr(cfg, "strict_canonical_tools", True))
        # Agent tools forwarded to the model (pre-builtin-merge) for honest
        # observability — distinct from the provider built-in flags below.
        request_tool_names = _tool_names_from(canonical_tools)
        request_tool_count = len(request_tool_names)
        canonical_tools, tools_enabled, tools, tools_configured = _apply_builtin_tools(
            provider=adapter_provider,
            model_name=model_name,
            canonical_tools=canonical_tools,
            cfg=cfg,
            route_override=route_override,
            adapter=adapter,
            adapter_name=adapter_name,
            spec=spec,
            request_enable_tools=effective_model_settings.get("enable_tools"),
            request_tools_explicitly_empty=(data.tools is not None and len(data.tools) == 0),
        )
        canonical_tools = _apply_wire_names(canonical_tools)
        wire_messages = _apply_wire_names_to_messages(data.messages)
        # Use spec.name as provider model ID so alias registry keys (e.g. gpt-4o-mini-chat)
        # still send the correct model ID to the API.
        provider_model_id = spec.name if spec else model_name
        llm_req = LLMRequest(
            messages=wire_messages or [],
            tools=canonical_tools,
            # ADR-0114: forward the canonical structured-output contract so
            # adapters that support it (e.g. local GBNF-constrained decoding)
            # can guarantee parseable output.
            output_contract=getattr(data, "output_contract", None),
            model_settings={**(effective_model_settings or {}), "model_name": provider_model_id, "provider": adapter_provider},
            request_context=request_context,
            skill_refs=getattr(data, "skill_refs", None),
        )

        inference_start = time.time()
        llm_resp = adapter.complete(llm_req)
        inference_time = time.time() - inference_start

        tool_calls_canonical: List[Dict[str, Any]] = []
        for item in llm_resp.output_items:
            if isinstance(item, ToolCallRequest):
                tc = item.model_dump(mode="json", exclude_none=True)
                tool_calls_canonical.append(tc)

        citations_out = _citations_payload(llm_resp)

        finish_reason = _stop_reason_to_finish_reason(llm_resp.stop_reason)
        if tool_calls_canonical:
            finish_reason = "tool_calls"

        return _track_inference_cost({
            "inference_time_seconds": inference_time,
            "inference_backend": "adapter",
            "adapter": f"{adapter_provider}:{adapter_name}",
            "adapter_selection_source": selection.source,
            "api_mode": getattr(cfg, "openai_api_mode", None) if adapter_provider == "openai" else None,
            "provider": adapter_provider,
            "model_name": model_name,
            "tools_enabled": tools_enabled,
            "tools": tools,
            "tools_configured": tools_configured,
            "request_tool_count": request_tool_count,
            "request_tool_names": request_tool_names,
            "content": llm_resp.output_text or "",
            "citations": citations_out,
            "model": f"{adapter_provider}:{model_name}",
            "finish_reason": finish_reason,
            # ADR-0064: canonical tool call envelope (required)
            "tool_calls_canonical": tool_calls_canonical or None,
            # ADR-0064 R10: Reasoning for multi-turn replay (agentic loop persists on assistant message)
            "reasoning_content": getattr(llm_resp, "reasoning_content", None),
            "reasoning_blocks": getattr(llm_resp, "reasoning_blocks", None),
            # ADR-0064: effective thinking settings used for this request (for debugging/observability)
            "enable_thinking": effective_model_settings.get("enable_thinking"),
            "reasoning_effort": effective_model_settings.get("reasoning_effort"),
            "prompt_tokens": (llm_resp.usage.prompt_tokens if llm_resp.usage else None),
            "completion_tokens": (llm_resp.usage.output_tokens if llm_resp.usage else None),
            "total_tokens": (llm_resp.usage.total_tokens if llm_resp.usage else None),
            # ADR-0064 R9: Extended usage envelope
            "cache_read_tokens": (llm_resp.usage.cache_read_tokens if llm_resp.usage else None),
            "cache_creation_tokens": (llm_resp.usage.cache_creation_tokens if llm_resp.usage else None),
            "reasoning_tokens": (llm_resp.usage.reasoning_tokens if llm_resp.usage else None),
            "raw": (llm_resp.raw_provider_metadata.get("raw") if llm_resp.raw_provider_metadata else None),
        }, motet)

    raise ValueError(
        "No adapter available for provider/model; provider objects are no longer supported."
    )


def _fetch_image_bytes(image: Any) -> tuple[bytes, str]:
    """
    Resolve a GeneratedImage (ADR-0113) into raw bytes + mime type.

    Prefers inline base64; falls back to fetching a temporary provider URL (which can expire).
    """
    mime_type = getattr(image, "mime_type", None) or "image/png"
    b64 = getattr(image, "base64_data", None)
    if b64:
        try:
            return base64.b64decode(b64), mime_type
        except Exception as e:
            raise ValueError(f"Invalid base64 image data: {e}") from e

    url = getattr(image, "url", None)
    if url:
        import urllib.request

        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (provider image URL)
            payload = resp.read()
            ctype = resp.headers.get("Content-Type") or mime_type
        return payload, ctype

    raise ValueError("GeneratedImage has neither base64_data nor url")


@motet.command(
    description="Generate image(s) from a text prompt and store them as artifacts for later use in the conversation.",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE]
)
def image_generation(data: ImageGenerationData) -> Dict[str, Any]:
    """
    Generate image(s) from a text prompt and store them as artifacts (ADR-0113).

    This is the dedicated text-to-image path (Pattern B). It routes provider/model via the
    same ADR-0064 precedence as model_inference, capability-gates on image generation, calls
    the adapter's generate_images(), stores each image via create_artifact
    (ArtifactKind.GENERATED_IMAGE), and returns canonical artifact-backed MediaParts.

    Args:
        data: ImageGenerationData with prompt and model settings.

    Returns:
        Dict with artifact_ids, media (serialized MediaParts), provider/model, and counts.

    Example:
        result = motet.do(
            image_generation,
            data=ImageGenerationData(
                prompt="a watercolor fox in a misty forest",
                model_settings={"provider": "openai", "model_name": "gpt-image-1"},
                size="1024x1024",
            ),
        )
    """
    motet = get_motet_context()
    start_time = time.time()

    model_settings = data.model_settings or {}
    request_context = data.request_context
    provider = model_settings.get("provider")
    model_name = model_settings.get("model_name")

    from motet.core.config import Config
    cfg = Config()

    # ADR-0113: default to the configured image model (not the text chat model).
    provider, model_name = _resolve_image_provider_and_model(cfg, provider, model_name)
    if not provider or not model_name:
        raise ValueError("image_generation requires model_settings.provider and model_settings.model_name")

    if not (data.prompt or "").strip():
        raise ValueError("image_generation requires a non-empty prompt")

    # ADR-0018: budget gate before an (often expensive) image call.
    _check_budget_before_inference(motet, provider, model_name)

    request_context = _ensure_request_context_identity(motet=motet, request_context=request_context)
    request_context = _apply_model_profile_name(motet=motet, request_context=request_context, cfg=cfg)

    credentials = _get_provider_credentials(motet=motet, provider=provider)

    selection, effective_model_settings, route_override, spec = _select_adapter_and_effective_model_settings(
        provider=str(provider or ""),
        model_name=str(model_name or ""),
        model_settings=dict(model_settings or {}),
        request_context=request_context,
        cfg=cfg,
    )

    if not selection.adapter_name:
        raise ValueError("No adapter available for provider/model for image generation")

    from motet.core.models.adapters import adapter_registry
    from motet.core.types import ImageGenerationRequest, MediaPart
    from motet.core.artifacts.types import ArtifactKind
    from motet.core.commands.builtin.artifacts import create_artifact
    from motet.core.commands.command_data_classes import CreateArtifactData

    adapter_provider = selection.provider or provider
    normalized_credentials = _normalize_adapter_credentials(adapter_provider, credentials, spec=spec)
    adapter = adapter_registry.build(adapter_provider, selection.adapter_name, credentials=normalized_credentials)

    # ADR-0113: capability gate. Fail fast rather than routing to a text-only model.
    caps = adapter.capabilities(model=model_name)
    if not bool(getattr(caps, "supports_image_generation", False)):
        raise ValueError(
            f"Model {adapter_provider}:{model_name} does not support image generation "
            f"(CAP_IMAGE_GENERATION). Choose an image-generation-capable model."
        )

    provider_model_id = spec.name if spec else model_name

    # Resolve optional input images (edit/variation) to canonical MediaParts.
    input_images: Optional[List[MediaPart]] = None
    if data.input_image_artifact_ids:
        input_images = [
            MediaPart(type="media", media_type="image", mime_type="image/png", artifact_id=aid)
            for aid in data.input_image_artifact_ids
        ]

    img_req = ImageGenerationRequest(
        prompt=data.prompt,
        n=int(data.n or 1),
        size=data.size,
        quality=data.quality,
        background=data.background,
        input_images=input_images,
        model_settings={**(effective_model_settings or {}), "model_name": provider_model_id, "provider": adapter_provider},
        request_context=request_context,
    )

    logger.info(
        "image_generation_started",
        **motet.log_fields(provider=adapter_provider, model=model_name, n=int(data.n or 1))
    )

    gen_start = time.time()
    img_resp = adapter.generate_images(img_req)
    inference_time = time.time() - gen_start

    artifact_ids: List[str] = []
    media: List[Dict[str, Any]] = []
    for idx, image in enumerate(img_resp.images or []):
        payload, content_type = _fetch_image_bytes(image)
        filename = data.filename or f"generated_{idx}.png"
        created = motet.do(
            create_artifact,
            data=CreateArtifactData(
                payload=payload,
                content_type=content_type,
                kind=ArtifactKind.GENERATED_IMAGE.value,
                filename=filename,
                trigger_derivations=bool(data.trigger_derivations),
                ttl_seconds=data.ttl_seconds,
                metadata={
                    "source": "image_generation",
                    "provider": adapter_provider,
                    "model_name": model_name,
                    "prompt": data.prompt,
                    "revised_prompt": getattr(image, "revised_prompt", None),
                },
            ),
        )
        artifact_id = created.get("artifact_id") if isinstance(created, dict) else None
        if artifact_id:
            artifact_ids.append(artifact_id)
            media.append(
                MediaPart(
                    type="media",
                    media_type="image",
                    mime_type=content_type,
                    artifact_id=artifact_id,
                ).model_dump(exclude_none=True)
            )

    logger.info(
        "image_generation_completed",
        **motet.log_fields(
            provider=adapter_provider,
            model=model_name,
            images=len(artifact_ids),
            inference_time_seconds=inference_time,
        )
    )

    result = {
        "provider": adapter_provider,
        "model_name": model_name,
        "model": f"{adapter_provider}:{model_name}",
        "adapter": f"{adapter_provider}:{selection.adapter_name}",
        "adapter_selection_source": selection.source,
        "inference_backend": "adapter",
        "inference_time_seconds": inference_time,
        "image_count": len(artifact_ids),
        "artifact_ids": artifact_ids,
        "media": media,
        "prompt_tokens": (img_resp.usage.prompt_tokens if img_resp.usage else None),
        "completion_tokens": (img_resp.usage.output_tokens if img_resp.usage else None),
        "total_tokens": (img_resp.usage.total_tokens if img_resp.usage else None),
    }
    return _track_inference_cost(result, motet)


@motet.command(
    description="Run streaming LLM inference with real-time token events on the task stream for interactive replies.",
    timeout_seconds=300,
    required_capabilities=[WorkerCapability.MODEL_INFERENCE]
)
def model_stream(data: ModelStreamData) -> Dict[str, Any]:
    """
    Execute model inference with real-time token streaming to Redis.
    
    This is the RECOMMENDED way to stream model responses using the decorator pattern (ADR-0030).
    
    Streams tokens to Redis as they arrive, enabling real-time UI updates.
    
    Args:
        data: ModelStreamData with messages, stream_key, and model settings
        
    Returns:
        Dict with tokens_streamed and final_content
        
    Example:
        result = motet.do(
            model_stream,
            data=ModelStreamData(
                messages=[Message(role="user", content="Tell me a story")],
                stream_key="stream:task-123:response",
                model_settings={
                    "provider": "openai",
                    "model_name": "gpt-4",
                    "temperature": 0.7,
                    "max_tokens": 2000
                }
            )
        )
    """
    motet = get_motet_context()
    start_time = time.time()
    
    model_settings = data.model_settings or {}
    request_context = data.request_context
    provider = model_settings.get("provider")
    model_name = model_settings.get("model_name")

    # ADR-0064: Canonical-only execution. Always resolve provider/model_name via Config
    # (removes legacy agent_default fallback path).
    from motet.core.config import Config
    cfg = Config()

    provider, model_name = _resolve_provider_and_model(cfg, provider, model_name)

    # ADR-0018: Check budget before inference (raises BudgetExceededError if blocked)
    if provider and model_name:
        _check_budget_before_inference(motet, provider, model_name)

    credentials = _get_provider_credentials(motet=motet, provider=provider)
    
    # Get Redis connection for streaming
    from motet.core.distributed.redis_manager import get_sync_redis_client
    redis_client = get_sync_redis_client()
    
    # Use motet.stream_key for writing (set from data.stream_key in decorator when present)
    stream_key = motet.stream_key
    # Capture requested stream_key from input so response echoes it (avoids mismatch if motet used fallback)
    _requested_stream_key = getattr(data, "stream_key", None) if hasattr(data, "stream_key") else None
    if not (_requested_stream_key and str(_requested_stream_key).strip()) and isinstance(data, dict):
        _requested_stream_key = data.get("stream_key")
    response_stream_key = (_requested_stream_key and str(_requested_stream_key).strip()) or stream_key

    tokens_streamed = 0
    # Stream state (uses dataclass for cleaner event handling)
    stream_state = StreamEventResult()
    inference_backend = "model_registry"
    adapter_used: Optional[str] = None
    adapter_selection_source: Optional[str] = None
    api_mode: Optional[str] = None
    tools_enabled: bool = False
    tools: List[str] = []
    tools_configured: bool = False
    # Agent (request) tools actually forwarded to the model, distinct from the
    # provider-native built-in tools tracked by tools_enabled/tools above. The
    # built-in flags read false even when the agent passes a full tool list
    # (e.g. local models inject schemas into the prompt), which is misleading
    # when debugging "did the model get tools?"; these fields make it explicit.
    request_tool_count: int = 0
    request_tool_names: List[str] = []
    # ADR-0064 R9: Usage accumulation from streaming events
    usage_data: Dict[str, Optional[int]] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "reasoning_tokens": None,
    }
    # ADR-0064 R10: Accumulate reasoning from ThinkingEvent (used in adapter stream path)
    reasoning_content_parts: List[str] = []
    
    try:
        # NOTE: Do NOT clear the stream key - unified task-level streaming (ADR-0050)
        # All commands write to the same task:{task_id}:response stream
        # Clearing here would delete all previous events from other commands
        logger.debug("model_stream_start", stream_key=stream_key)
        
        # Use model registry for all streaming calls (ADR-0064)
        if provider and model_name:
            # Ensure identity context exists so tenant-scoped ModelProfiles can be applied even
            # when call-sites omit request_context (ADR-0064).
            request_context = _ensure_request_context_identity(motet=motet, request_context=request_context)
            request_context = _apply_model_profile_name(motet=motet, request_context=request_context, cfg=cfg)

            # ADR-0062: auto-enable multimodal rendering by augmenting request_context (not model_settings)
            request_context = _build_request_context_for_multimodal(
                motet=motet,
                messages=data.messages,
                provider=provider,
                model_name=model_name,
                request_context=request_context,
            )

            used_adapter = False
            selection, effective_model_settings, route_override, spec = _select_adapter_and_effective_model_settings(
                provider=str(provider or ""),
                model_name=str(model_name or ""),
                model_settings=dict(model_settings or {}),
                request_context=request_context,
                cfg=cfg,
            )
            adapter_selection_source = selection.source

            # ADR-0064: Unified adapter routing for all providers
            if selection.adapter_name:
                from motet.core.models.adapters import adapter_registry
                from motet.core.types import LLMRequest

                used_adapter = True
                inference_backend = "adapter"
                adapter_provider = selection.provider or provider
                adapter_used = f"{adapter_provider}:{selection.adapter_name}"
                
                # Normalize credentials for adapter consumption (spec.base_url merged in when set)
                normalized_credentials = _normalize_adapter_credentials(
                    adapter_provider, credentials, spec=spec
                )
                
                adapter = adapter_registry.build(
                    adapter_provider,
                    selection.adapter_name,
                    credentials=normalized_credentials,
                )

                canonical_tools = _tools_to_canonical(data.tools, strict=getattr(cfg, "strict_canonical_tools", True))
                # Record the agent tools forwarded to the model (pre-builtin-merge)
                # for honest observability — these are what actually reach the model.
                request_tool_names = _tool_names_from(canonical_tools)
                request_tool_count = len(request_tool_names)
                canonical_tools, tools_enabled, tools, tools_configured = _apply_builtin_tools(
                    provider=adapter_provider,
                    model_name=model_name,
                    canonical_tools=canonical_tools,
                    cfg=cfg,
                    route_override=route_override,
                    adapter=adapter,
                    adapter_name=selection.adapter_name,
                    spec=spec,
                    request_enable_tools=effective_model_settings.get("enable_tools"),
                    request_tools_explicitly_empty=(data.tools is not None and len(data.tools) == 0),
                )
                canonical_tools = _apply_wire_names(canonical_tools)
                wire_messages = _apply_wire_names_to_messages(data.messages)
                # Use spec.name as provider model ID so alias registry keys (e.g. gpt-4o-mini-chat)
                # still send the correct model ID to the API.
                provider_model_id = spec.name if spec else model_name
                llm_req = LLMRequest(
                    messages=wire_messages or [],
                    tools=canonical_tools,
                    # ADR-0114: forward the canonical structured-output contract
                    # so adapters that support it (e.g. local GBNF-constrained
                    # decoding) can guarantee parseable output.
                    output_contract=getattr(data, "output_contract", None),
                    model_settings={**(effective_model_settings or {}), "model_name": provider_model_id, "provider": adapter_provider},
                    request_context=request_context,
                    skill_refs=getattr(data, "skill_refs", None),
                )

                allow_citations = True

                for ev in adapter.stream(llm_req):
                    stream_state = _handle_stream_event(
                        ev=ev,
                        motet=motet,
                        usage_data=usage_data,
                        state=stream_state,
                        allow_citations=allow_citations,
                        error_label=str(adapter_provider).title(),
                        stream_key=response_stream_key,
                    )
                    if stream_state.reasoning_delta:
                        reasoning_content_parts.append(stream_state.reasoning_delta)
                    if stream_state.should_break:
                        break

            if not used_adapter:
                raise ValueError(
                    "No adapter available for provider/model; provider objects are no longer supported."
                )
        else:
            raise ValueError("model_stream requires model_settings.provider and model_settings.model_name")
        
        # Send stream completion event (not task-level "end" - that's sent by agent_turn)
        # Ensure any buffered tokens flush before completion (use same stream as tokens).
        motet.flush_token_buffer(stream_key=response_stream_key)
        motet.stream_event("stream_complete", final=stream_state.final_content, stream_key=response_stream_key)
        
        # NOTE: Do NOT set expiration - only the root turn command manages stream TTL (ADR-0050)
        
    except Exception as e:
        # Send error event
        try:
            motet.stream_event("error", error=str(e))
        except Exception:
            # Best-effort error event; don't mask original exception
            logger.debug("model_stream_error_event_emit_failed", exc_info=True)
        raise
    finally:
        redis_client.close()
    
    # ADR-0064 R10: Full reasoning_content for persistence (from stream ThinkingEvent accumulation)
    reasoning_content_str = "".join(reasoning_content_parts) if reasoning_content_parts else None

    # effective_model_settings is set inside the provider/model_name block; use it for observability
    effective_settings = effective_model_settings if (provider and model_name) else {}
    result = {
        "tokens_streamed": stream_state.tokens_streamed,
        "stream_key": response_stream_key,
        "final_content": stream_state.final_content,
        # ADR-0064: canonical tool call envelope (preferred for internal execution)
        "tool_calls_canonical": stream_state.tool_calls_canonical,
        "citations": stream_state.citations,
        "finish_reason": stream_state.finish_reason,
        # ADR-0064 R10: Reasoning for multi-turn replay (agentic loop persists on assistant message)
        "reasoning_content": reasoning_content_str,
        "reasoning_blocks": stream_state.reasoning_blocks,  # Opaque provider blocks from final ThinkingEvent (e.g. OpenAI encrypted reasoning)
        # ADR-0064: execution provenance (additive)
        "inference_backend": inference_backend,
        "adapter": adapter_used,
        "adapter_selection_source": adapter_selection_source,
        "api_mode": api_mode,
        "provider": provider,
        "model_name": model_name,
        # ADR-0064: effective thinking settings used for this request (for debugging/observability)
        "enable_thinking": effective_settings.get("enable_thinking"),
        "reasoning_effort": effective_settings.get("reasoning_effort"),
        # ADR-0064: provider-native built-in tools (additive)
        "tools_enabled": tools_enabled,
        "tools": tools,
        "tools_configured": tools_configured,
        # Agent/request tools actually forwarded to the model (distinct from the
        # provider built-in flags above, which are false for prompt-injected tools).
        "request_tool_count": request_tool_count,
        "request_tool_names": request_tool_names,
        # ADR-0064 R9: Extended usage envelope from streaming
        "prompt_tokens": usage_data["prompt_tokens"],
        "completion_tokens": usage_data["completion_tokens"],
        "total_tokens": usage_data["total_tokens"],
        "cache_read_tokens": usage_data["cache_read_tokens"],
        "cache_creation_tokens": usage_data["cache_creation_tokens"],
        "reasoning_tokens": usage_data["reasoning_tokens"],
    }
    
    # ADR-0018: Track cost to Redis (non-blocking, non-critical)
    try:
        from motet.core.cost import track_model_result
        
        cost_usd = track_model_result(
            result,
            tenant_id=motet.tenant_id or "default",
            task_id=motet.task_id,
            conversation_id=motet.conversation_id,
            command_id=motet.command_id,
            principal_id=getattr(motet, "principal_id", None) or None,
            root_conversation_id=_root_conversation_id_from_motet(motet),
        )
        result["cost_usd"] = cost_usd
    except Exception as e:
        # Cost tracking is non-critical - log and continue
        logger.debug("model_stream_cost_tracking_failed", error=str(e))
    
    return result


# ================================================================================================
# Exports
# ================================================================================================

__all__ = [
    # Decorated functions (RECOMMENDED - ADR-0030)
    "model_inference",
    "model_stream",
    "embedding_generation",
    "image_generation",
    # Data classes
    "ModelInferenceData",
    "ModelStreamData",
    "EmbeddingData",
    "ImageGenerationData",
]
