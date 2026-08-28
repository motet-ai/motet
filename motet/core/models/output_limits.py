"""
Motet - Model Output Token Limits

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-29

Description:
    Resolve max output tokens for model requests from request settings and
    ModelSpec.max_output_tokens. Keeps adapter wire defaults aligned with the
    registry capacity ceiling instead of inventing a magic 8000.

Dependencies:
    - typing: Mapping / Optional annotations
    - motet.core.models.registry: get_model_spec for ModelSpec lookup

Usage:
    from motet.core.models.output_limits import (
        apply_max_tokens_from_spec,
        resolve_max_output_tokens,
    )

    # Model-command merge (request wins; fill from ModelSpec when unset)
    apply_max_tokens_from_spec(effective_settings, spec)

    # Adapter wire params when settings omit max_tokens
    max_tokens = resolve_max_output_tokens(
        settings,
        provider="deepseek",
        model_name="deepseek-v4-pro",
        fallback=None,  # omit wire field when unset + unregistered
    )

Notes:
    - Request keys checked (first wins): max_tokens, max_completion_tokens,
      max_output_tokens.
    - ModelSpec.max_output_tokens is a capacity ceiling; values <= 0 are ignored
      (embedding / image models).
    - Prefer fallback=None so adapters do not invent magic 8k defaults; omit the
      wire field (or fail if the provider requires the field, e.g. Anthropic).
    - Local adapters may keep a tighter direct-call default; model commands still
      fill from ModelSpec via apply_max_tokens_from_spec.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .registry import get_model_spec
from .specs import ModelSpec


def _positive_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def request_max_output_tokens(settings: Mapping[str, Any]) -> Optional[int]:
    """Return an explicit request max-output setting, if present."""
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        parsed = _positive_int(settings.get(key))
        if parsed is not None:
            return parsed
    return None


def apply_max_tokens_from_spec(
    effective_model_settings: Dict[str, Any],
    spec: Optional[ModelSpec],
) -> None:
    """
    Fill ``max_tokens`` from ``ModelSpec.max_output_tokens`` when unset.

    Mutates ``effective_model_settings`` in place. Request / profile values win.
    """
    if request_max_output_tokens(effective_model_settings) is not None:
        return
    if spec is None:
        return
    ceiling = _positive_int(getattr(spec, "max_output_tokens", None))
    if ceiling is None:
        return
    effective_model_settings["max_tokens"] = ceiling


def resolve_max_output_tokens(
    settings: Mapping[str, Any],
    *,
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    fallback: Optional[int] = None,
) -> Optional[int]:
    """
    Resolve max output tokens for adapter wire params.

    Precedence: request settings → ModelSpec.max_output_tokens → fallback.
    Returns None when no request value, no positive ModelSpec ceiling, and
    fallback is None (callers should omit the wire field or fail if required).
    """
    requested = request_max_output_tokens(settings)
    if requested is not None:
        return requested

    prov = provider or settings.get("provider")
    name = model_name or settings.get("model_name")
    if isinstance(prov, str) and prov and isinstance(name, str) and name:
        spec = get_model_spec(prov, name)
        ceiling = _positive_int(getattr(spec, "max_output_tokens", None) if spec else None)
        if ceiling is not None:
            return ceiling

    return _positive_int(fallback)


__all__ = [
    "apply_max_tokens_from_spec",
    "request_max_output_tokens",
    "resolve_max_output_tokens",
]
