"""
Motet - Live Adapter Capability Matrix (ADR-0064)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-27

Description:
    Live API capability-test targets derived from MODEL_REGISTRY.
    Default matrix = one chat canary per provider: newest release-month cohort
    (via ModelSpec.released_at), then cheapest by input pricing among models that
    support stream + tool_use. Aliases (registry key != wire name) are skipped.
    Image-generation-only specs are skipped.

    Opt-in via MOTET_LIVE_ADAPTER_MATRIX=1 plus per-provider API keys.

    Override the default matrix with:
      MOTET_LIVE_ADAPTER_CASES=openai:gpt-5.5,deepseek:deepseek-v4-pro

    Also provides ``live_cacheable_system_text`` for ADR-0124 live prompt-cache
    hit checks (prefix large enough for Anthropic / OpenAI minimums).

Dependencies:
    - motet.core.models.specs / registry / adapters

Usage:
    from tests.fixtures.live_adapter_matrix import iter_live_cases, resolve_credentials
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from motet.core.models.registry import get_model_spec
from motet.core.models.specs import (
    CAP_IMAGE_GENERATION,
    CAP_STREAM,
    CAP_TOOL_USE,
    MODEL_REGISTRY,
    ModelSpec,
)


@dataclass(frozen=True)
class LiveAdapterCase:
    """One live API target."""

    provider: str
    model: str
    adapter_name: str
    spec: ModelSpec


# Providers included in the default live matrix (order is stable for pytest ids).
_LIVE_PROVIDERS: tuple[str, ...] = (
    "openai",
    "anthropic",
    "gemini",
    "moonshot",
    "deepseek",
    "xai",
    "meta",
)

_MAX_COST = Decimal("Infinity")


def _is_chat_capable(spec: ModelSpec) -> bool:
    """True unless the spec is image-generation-only (no chat path)."""
    caps = set(spec.capabilities or set())
    return caps != {CAP_IMAGE_GENERATION}


def _is_alias(registry_key: str, spec: ModelSpec) -> bool:
    """Skip convenience / adapter-force aliases (key differs from wire model id)."""
    return registry_key != spec.name


def _input_cost_per_1k(spec: ModelSpec) -> Decimal:
    if spec.pricing is None or spec.pricing.input_per_1k is None:
        return _MAX_COST
    return Decimal(spec.pricing.input_per_1k)


def _pick_live_canary(provider: str) -> Optional[str]:
    """
    Newest release-month chat canary for a provider, preferring lower input cost.

    Selection rules:
      1. Chat-capable, non-alias, CAP_STREAM + CAP_TOOL_USE
      2. Prefer specs with released_at set
      3. Restrict to the newest calendar month among those dates
      4. Within that cohort, pick lowest input_per_1k (then registry key)
    """
    models = MODEL_REGISTRY.get(provider) or {}
    candidates: List[Tuple[str, ModelSpec]] = []
    for key, spec in models.items():
        if not _is_chat_capable(spec):
            continue
        if _is_alias(key, spec):
            continue
        caps = set(spec.capabilities or set())
        if CAP_STREAM not in caps or CAP_TOOL_USE not in caps:
            continue
        candidates.append((key, spec))

    if not candidates:
        # Fallback: any chat-capable non-alias (e.g. reasoning-only o-series).
        candidates = [
            (key, spec)
            for key, spec in models.items()
            if _is_chat_capable(spec) and not _is_alias(key, spec)
        ]
    if not candidates:
        return None

    dated = [(k, s) for k, s in candidates if s.released_at is not None]
    pool = dated or candidates
    if dated:
        newest = max(s.released_at for _, s in dated if s.released_at is not None)
        assert isinstance(newest, date)
        cohort = [
            (k, s)
            for k, s in dated
            if s.released_at is not None
            and s.released_at.year == newest.year
            and s.released_at.month == newest.month
        ]
    else:
        cohort = pool

    cohort.sort(key=lambda item: (_input_cost_per_1k(item[1]), item[0]))
    return cohort[0][0]


def default_live_cases() -> Tuple[Tuple[str, str], ...]:
    """
    One live canary model per provider using released_at + pricing.

    See ``_pick_live_canary``. Override with MOTET_LIVE_ADAPTER_CASES when needed.
    """
    out: List[Tuple[str, str]] = []
    for provider in _LIVE_PROVIDERS:
        key = _pick_live_canary(provider)
        if key:
            out.append((provider, key))
    return tuple(out)


def live_matrix_enabled() -> bool:
    """True when the operator explicitly opted into live LLM spend."""
    return os.getenv("MOTET_LIVE_ADAPTER_MATRIX", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _parse_case_overrides() -> Optional[List[tuple[str, str]]]:
    raw = os.getenv("MOTET_LIVE_ADAPTER_CASES", "").strip()
    if not raw:
        return None
    out: List[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(
                f"Invalid MOTET_LIVE_ADAPTER_CASES entry {part!r}; expected provider:model"
            )
        provider, model = part.split(":", 1)
        out.append((provider.strip().lower(), model.strip()))
    return out or None


def resolve_credentials(provider: str) -> Optional[Dict[str, str]]:
    """Return adapter credentials dict or None if the provider key is missing."""
    p = (provider or "").strip().lower()
    env_map = {
        "openai": ("MOTET_OPENAI_API_KEY", "OPENAI_API_KEY"),
        "anthropic": ("MOTET_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        "gemini": ("MOTET_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "moonshot": ("MOTET_MOONSHOT_API_KEY", "MOONSHOT_API_KEY"),
        "deepseek": ("MOTET_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
        "xai": ("MOTET_XAI_API_KEY", "XAI_API_KEY"),
        "meta": ("MOTET_META_API_KEY", "MODEL_API_KEY", "META_API_KEY"),
    }
    names = env_map.get(p)
    if not names:
        return None
    api_key = None
    for name in names:
        val = os.getenv(name)
        if val and val.strip():
            api_key = val.strip()
            break
    if not api_key:
        return None
    creds: Dict[str, str] = {
        "api_key": api_key,
        f"{p}_api_key": api_key,
    }
    # Optional base URL overrides (OpenAI-compatible hosts).
    base_env = {
        "moonshot": "MOONSHOT_API_BASE",
        "deepseek": "DEEPSEEK_API_BASE",
        "xai": "XAI_API_BASE",
        "meta": "META_API_BASE",
        "openai": "OPENAI_API_BASE",
    }.get(p)
    if base_env:
        base = os.getenv(base_env)
        if base and base.strip():
            creds["base_url"] = base.strip()
    return creds


def iter_live_cases() -> List[LiveAdapterCase]:
    """Build live cases from registry defaults or MOTET_LIVE_ADAPTER_CASES."""
    pairs = _parse_case_overrides() or list(default_live_cases())
    cases: List[LiveAdapterCase] = []
    for provider, model in pairs:
        spec = get_model_spec(provider, model)
        if spec is None:
            continue
        adapter_name = spec.default_adapter
        if not adapter_name:
            continue
        cases.append(
            LiveAdapterCase(
                provider=provider,
                model=model,
                adapter_name=adapter_name,
                spec=spec,
            )
        )
    return cases


# Solid 32x32 red PNG (≥512 pixels for xAI; 1x1 often rejected elsewhere).
LIVE_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAK0lEQVR42u3OIQEA"
    "AAwEoetfeovxBoGnq1tKQEBAQEBAQEBAQEBAQEBgHXhUDfhqeP5ugAAAAABJRU5ErkJggg=="
)

# ~80 chars; repeated to clear provider cache-minimum prefixes (Anthropic Opus/
# Haiku families need ~4096 tokens; Sonnet/OpenAI ~1024). ~4 chars/token.
_CACHE_SYSTEM_PAD_UNIT = (
    "Motet sticky tool-set cache fixture. Keep this system prefix byte-identical "
    "across turns so provider prompt caches can hit the tools-then-system segment. "
)


def live_cacheable_system_text(*, min_tokens: int = 4200) -> str:
    """
    Build a stable system prompt large enough to be cache-eligible.

    Providers silently skip caching below their minimum prefix size; pad past
    the highest Motet-relevant threshold (~4096) so live hit assertions are
    meaningful for Claude Opus/Haiku canaries as well as Sonnet/OpenAI.
    """
    target_chars = max(1024, int(min_tokens) * 4)
    parts: List[str] = [
        "You are a Motet live-adapter cache probe. Follow the user message exactly.\n\n"
    ]
    while sum(len(p) for p in parts) < target_chars:
        parts.append(_CACHE_SYSTEM_PAD_UNIT)
    return "".join(parts)
