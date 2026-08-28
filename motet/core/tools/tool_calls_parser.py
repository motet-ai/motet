"""
Motet - Tool Calls Parser

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Extract tool calls from model_inference payloads and normalize them to
    ``{tool_name, parameters, id}`` dicts via the codec.

    Provider Chat Completions / Anthropic routing was deleted.
    Local XML / ``<tool_call>`` parsing lives in ``motet.core.models.local.reasoning``.

Dependencies:
    - motet.core.models.adapters.tool_call_codec: lift mixed payloads
    - structlog: Structured logging

Usage:
    from motet.core.tools.tool_calls_parser import (
        extract_tool_calls_from_response,
        parse_tool_calls,
    )

    model_result = motet.do(model_inference, data=inference_data)
    raw = extract_tool_calls_from_response(model_result)
    parsed = parse_tool_calls(raw)

Notes:
    - Reads ``tool_calls_canonical`` on the command payload only (issue #225).
    - Wire names are canonicalized by ``inbound_tool_call_request``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from ..models.adapters.tool_call_codec import (
    inbound_tool_call_request,
    tool_call_request_from_unknown,
)

logger = structlog.get_logger(__name__)


def extract_tool_calls_from_response(
    model_result: Dict[str, Any],
    provider: str = "openai",
) -> List[Any]:
    """Extract tool-call payloads from a model_inference result dict.

    Reads ``tool_calls_canonical`` only. Leftover ``tool_calls`` keys on the
    payload or ``raw`` dict are ignored (issue #225).
    """
    _ = provider  # retained for call-site compatibility
    canonical = model_result.get("tool_calls_canonical")
    if canonical:
        logger.debug("extracted_tool_calls_canonical", count=len(canonical))
        return list(canonical)

    logger.debug("no_tool_calls_found_in_response")
    return []


def parse_tool_calls(
    tool_calls: Optional[List[Any]],
    provider: str = "openai",
    tool_name_mapping: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Normalize mixed tool-call payloads to ``{tool_name, parameters, id}``.

    Provider format routing is gone (ADR-0137). Shape detection is the codec.
    """
    _ = provider
    if not tool_calls:
        return []

    mapping = tool_name_mapping or {}
    results: List[Dict[str, Any]] = []
    for tc in tool_calls:
        lifted = tool_call_request_from_unknown(tc)
        if lifted is None:
            continue
        req = inbound_tool_call_request(
            call_id=lifted.call_id,
            tool_name=lifted.tool_name,
            arguments_json=lifted.arguments_json,
            kind=lifted.kind,
        )
        name = mapping.get(lifted.tool_name, req.tool_name)
        params = req.arguments if isinstance(req.arguments, dict) else {}
        results.append(
            {
                "id": req.call_id,
                "tool_name": name,
                "parameters": params,
                "confidence": 1.0,
                "reasoning": f"LLM selected {name} via native function calling",
            }
        )
    return results


__all__ = [
    "extract_tool_calls_from_response",
    "parse_tool_calls",
]
