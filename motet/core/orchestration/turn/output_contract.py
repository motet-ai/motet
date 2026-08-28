"""
Motet - Turn Output Contract

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Resolves and applies OutputContract on an agent turn. Per-call
    AgentTurnData.output_contract wins over context["output_contract"]
    over AgentConfig.output_contract. A turn with no contract makes no
    extra model call.

    When a contract is set, one constrained model call runs after the
    loop stops (same shape as a budget-finalize write-up). Validation
    failure is an error on the turn envelope. One constrained retry with
    the validation error in context is allowed; silently returning free
    text is not.

Dependencies:
    - motet.core.types.OutputContract: contract type
    - motet.core.commands.builtin.model.model_stream: constrained finalize call
    - motet.core.reasoning.react.agent_data: shared model fallback constants

Usage:
    from motet.core.orchestration.turn.output_contract import (
        apply_output_contract,
        resolve_output_contract,
        validate_contract_text,
    )

    contract = resolve_output_contract(data, context, agent_config)
    if contract is not None:
        text, result = apply_output_contract(motet, history, result, contract, ...)

Notes:
    - no_tools can attach the contract to its single model call so a second
      hop is not required.
    - JSON validation uses json.loads plus required-key checks from
      json_schema; a full JSON Schema engine is not required.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import structlog

from motet.core.reasoning.react.agent_data import (
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_PROVIDER,
)
from motet.core.types import Message, OutputContract

logger = structlog.get_logger(__name__)


def resolve_output_contract(
    data: Any,
    context: Optional[Dict[str, Any]],
    agent_config: Any,
) -> Optional[OutputContract]:
    """Per-call wins, then context, then the agent default."""
    for candidate in (
        getattr(data, "output_contract", None),
        (context or {}).get("output_contract"),
        getattr(agent_config, "output_contract", None),
    ):
        contract = _coerce_contract(candidate)
        if contract is not None:
            return contract
    return None


def _coerce_contract(value: Any) -> Optional[OutputContract]:
    if value is None:
        return None
    if isinstance(value, OutputContract):
        return value
    if isinstance(value, dict):
        return OutputContract.model_validate(value)
    return None


def validate_contract_text(text: str, contract: OutputContract) -> Optional[str]:
    """Return a validation error string, or None when the text satisfies the contract."""
    if contract.format != "json":
        return None
    raw = (text or "").strip()
    if not raw:
        return "output_contract requested JSON but the model returned empty text"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"output_contract JSON parse failed: {exc}"
    schema = contract.json_schema if isinstance(contract.json_schema, dict) else None
    if not schema:
        return None
    if schema.get("type") == "object" and not isinstance(parsed, dict):
        return "output_contract JSON Schema type=object but the model returned a non-object"
    required = schema.get("required")
    if isinstance(required, list) and isinstance(parsed, dict):
        missing = [key for key in required if key not in parsed]
        if missing:
            return f"output_contract JSON missing required keys: {', '.join(missing)}"
    return None


def apply_output_contract(
    motet: Any,
    *,
    history: List[Message],
    turn_result: Dict[str, Any],
    contract: OutputContract,
    provider: str,
    model_name: str,
    final_response: str,
    already_constrained: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Constrain one finalize call (or validate an already-constrained reply).

    Raises ValueError when validation fails after the allowed retry.
    """
    if already_constrained:
        error = validate_contract_text(final_response, contract)
        if error is None:
            return final_response, turn_result
        retry_text, retry_result = _constrained_call(
            motet,
            history=history,
            contract=contract,
            provider=provider,
            model_name=model_name,
            prior_text=final_response,
            validation_error=error,
        )
        retry_error = validate_contract_text(retry_text, contract)
        if retry_error is not None:
            raise ValueError(retry_error)
        return retry_text, _merge_usage(turn_result, retry_result, retry_text)

    text, result = _constrained_call(
        motet,
        history=history,
        contract=contract,
        provider=provider,
        model_name=model_name,
        prior_text=final_response,
        validation_error=None,
    )
    error = validate_contract_text(text, contract)
    if error is None:
        return text, _merge_usage(turn_result, result, text)
    retry_text, retry_result = _constrained_call(
        motet,
        history=history,
        contract=contract,
        provider=provider,
        model_name=model_name,
        prior_text=text,
        validation_error=error,
    )
    retry_error = validate_contract_text(retry_text, contract)
    if retry_error is not None:
        raise ValueError(retry_error)
    return retry_text, _merge_usage(_merge_usage(turn_result, result, text), retry_result, retry_text)


def _constrained_call(
    motet: Any,
    *,
    history: List[Message],
    contract: OutputContract,
    provider: str,
    model_name: str,
    prior_text: str,
    validation_error: Optional[str],
) -> Tuple[str, Dict[str, Any]]:
    from motet.core.commands.builtin.model import model_stream
    from motet.core.commands.command_data_classes import ModelStreamData

    messages: List[Message] = list(history)
    if prior_text:
        messages.append(Message(role="assistant", content=prior_text))
    if validation_error:
        messages.append(
            Message(
                role="user",
                content=(
                    "The previous reply did not satisfy the output contract. "
                    f"{validation_error} Return only valid output."
                ),
                metadata={"source": "output_contract", "cache_volatile": True},
            )
        )
    else:
        messages.append(
            Message(
                role="user",
                content=(
                    "Rewrite the answer so it satisfies the output contract. "
                    "Return only the contracted output, with no extra prose."
                ),
                metadata={"source": "output_contract", "cache_volatile": True},
            )
        )

    result = motet.do(
        model_stream,
        data=ModelStreamData(
            messages=messages,
            tools=[],
            stream_key=getattr(motet, "stream_key", None),
            output_contract=contract,
            model_settings={
                "provider": provider or DEFAULT_MODEL_PROVIDER,
                "model_name": model_name or DEFAULT_MODEL_NAME,
            },
        ),
    )
    text = str(result.get("final_content") or result.get("content") or "")
    return text, result if isinstance(result, dict) else {"content": text}


def _merge_usage(
    turn_result: Dict[str, Any],
    extra: Dict[str, Any],
    text: str,
) -> Dict[str, Any]:
    merged = dict(turn_result or {})
    merged["content"] = text
    if "final_response" in merged:
        merged["final_response"] = text
    extra_usage = extra.get("usage") if isinstance(extra.get("usage"), dict) else extra
    base_usage = merged.get("usage") if isinstance(merged.get("usage"), dict) else {}
    if isinstance(extra_usage, dict) and (
        "prompt_tokens" in extra_usage
        or "completion_tokens" in extra_usage
        or "total_tokens" in extra_usage
    ):
        merged["usage"] = {
            "prompt_tokens": int(base_usage.get("prompt_tokens") or 0)
            + int(extra_usage.get("prompt_tokens") or 0),
            "completion_tokens": int(base_usage.get("completion_tokens") or 0)
            + int(extra_usage.get("completion_tokens") or 0),
            "total_tokens": int(base_usage.get("total_tokens") or 0)
            + int(extra_usage.get("total_tokens") or 0),
        }
    return merged


__all__ = [
    "apply_output_contract",
    "resolve_output_contract",
    "validate_contract_text",
]
