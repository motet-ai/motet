"""
Motet - Workflow Utilities

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Utility functions for workflow command discovery, validation, and parameter
    substitution. Extracted from workflow package initialization module to keep
    core workflow API compact while preserving behavior and import compatibility.
    Validates sequential foreach step fields and the shared
    condition vocabulary used by ``skip_condition`` and ``until``.

Dependencies:
    - typing: Type hints
    - re/json: Parameter placeholder substitution
    - command_type_registry: Distributed command discovery

Usage:
    from motet.core.workflow.utils import substitute_parameters

    payload = substitute_parameters(
        {"parameters": {"url": "{navigate.url}"}},
        {"navigate": {"url": "https://example.com"}},
    )

Notes:
    - Supports {x} and {{x}} placeholder forms.
    - Supports dot notation and array indexing paths.
    - Quoted placeholders ("{{x}}" as the whole value) keep the value's native
      type; embedded placeholders render non-string values as escaped JSON text
      so the enclosing JSON string stays valid.
    - Canonical {{x}} placeholders resolve strictly: missing/None paths become
      None (whole value) or empty text (embedded). Single-brace {x} placeholders
      leave the literal text on a miss.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, TYPE_CHECKING

from ..registry import parse_qualified_name, qualify_with_default_namespace

if TYPE_CHECKING:
    from . import Workflow


# Condition operators shared by ``skip_condition`` and ``until``. Declared here
# (not in executor) because validate_workflow needs them and executor already
# imports from utils.
WORKFLOW_CONDITION_TYPES = frozenset(
    {"if_empty", "if_not_empty", "if_equals", "if_contains", "if_failed"}
)


def parse_condition_type(condition: str) -> str | None:
    """Return the operator of a ``type:path[:literal]`` condition, or None if malformed."""
    parts = condition.split(":", 2)
    if len(parts) < 2:
        return None
    return parts[0]


def get_command_by_name(name: str) -> Callable:
    """
    Get command function by name using unified CommandTypeRegistry.

    This is a convenience wrapper around CommandTypeRegistry for workflow execution.
    All commands are automatically registered via @distributed_command decorator.
    """
    from motet.core.commands.command_type_registry import command_type_registry

    registration = command_type_registry.get(name)
    if not registration:
        # Backward-compat: accept unprefixed command names in legacy workflows/tests.
        prefixed = qualify_with_default_namespace(name)
        registration = command_type_registry.get(prefixed)

    if not registration:
        available_types = command_type_registry.get_command_types()
        available = ", ".join(sorted(available_types))
        raise ValueError(f"Unknown command '{name}'. Available commands: {available}")

    return registration.implementation


def list_registered_commands() -> List[str]:
    """List all registered command names."""
    from motet.core.commands.command_type_registry import command_type_registry

    # Backward-compat: return legacy unprefixed names when in core namespace.
    names: List[str] = []
    for command_type in command_type_registry.get_command_types():
        namespace, local_name = parse_qualified_name(command_type)
        if namespace == "core" and local_name:
            names.append(local_name)
        names.append(command_type)
    return sorted(set(names))


def validate_workflow(workflow: Workflow) -> None:
    """Validate workflow structure, command references, and foreach step fields."""
    import re

    from motet.core.commands.command_type_registry import command_type_registry

    for step in workflow.steps.values():
        ownership = getattr(step, "ownership", "motet") or "motet"
        step_type = getattr(step, "step_type", "command") or "command"
        if ownership == "handback":
            if step_type == "elicitation":
                raise ValueError(
                    f"Step '{step.step_id}' cannot combine ownership=handback with "
                    f"step_type=elicitation (ownership is tool execution only)"
                )
            if not step.is_tool_shaped():
                raise ValueError(
                    f"Step '{step.step_id}' has ownership=handback but is not "
                    f"tool-shaped (command_type={step.command_type!r})"
                )
        if step_type == "elicitation":
            if ownership != "motet":
                raise ValueError(
                    f"Step '{step.step_id}' elicitation steps must use ownership=motet"
                )
            if not (step.elicitation_schema or step.command_data.get("schema")):
                raise ValueError(
                    f"Step '{step.step_id}' elicitation requires elicitation_schema"
                )

        # A step loops when it iterates a list (foreach) or repeats up to
        # max_loop_iterations until a condition holds (until). Both share the
        # same loop machinery and therefore the same guards.
        if step.foreach or step.until:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", step.loop_var or ""):
                raise ValueError(
                    f"Step '{step.step_id}' foreach loop_var must be a valid identifier, "
                    f"got {step.loop_var!r}"
                )
            if step.max_loop_iterations < 1:
                raise ValueError(
                    f"Step '{step.step_id}' max_loop_iterations must be >= 1, "
                    f"got {step.max_loop_iterations}"
                )
            # Nested foreach via workflow_execution would re-enter the loop machinery
            # without a depth guard; reject in v1 (ADR-0122).
            cmd = (step.command_type or "").strip()
            if cmd in (
                "workflow_execution",
                "core.workflow_execution",
                "full_workflow_execution",
                "core.full_workflow_execution",
            ):
                raise ValueError(
                    f"Step '{step.step_id}' cannot use foreach with command_type "
                    f"'{step.command_type}' (nested foreach / recursive workflow loops "
                    f"are not supported in v1)"
                )

        # An unknown `until` operator evaluates False forever, which silently
        # burns the full iteration budget. Reject the typo at build time.
        if step.until:
            condition_type = parse_condition_type(step.until)
            if condition_type not in WORKFLOW_CONDITION_TYPES:
                raise ValueError(
                    f"Step '{step.step_id}' has invalid until condition {step.until!r}; "
                    f"expected '<operator>:<path>[:<value>]' with operator one of "
                    f"{sorted(WORKFLOW_CONDITION_TYPES)}"
                )

        # Elicitation steps do not dispatch a distributed command.
        if step_type == "elicitation":
            continue

        if step.command_type and command_type_registry.get(step.command_type) is None:
            # Backward-compat: allow unprefixed command types if core.<name> exists.
            prefixed = qualify_with_default_namespace(step.command_type)
            if command_type_registry.get(prefixed) is None:
                raise ValueError(f"Unknown command type: {step.command_type}")


def validate_execution_context(execution_context: Dict[str, Any]) -> None:
    """Validate execution context fields against DistributedCommandContext."""
    try:
        from motet.core.commands.distributed import DistributedCommandContext

        valid_fields = set(DistributedCommandContext.model_fields.keys())
        unknown = set(execution_context.keys()) - valid_fields
        if unknown:
            raise ValueError(f"Unknown fields: {unknown}")
    except ImportError:
        pass


def substitute_parameters(data: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Substitute parameters from context into command data.

    Resolution semantics:
    - Canonical ``{{path}}`` placeholders resolve **strictly**: a missing or
      ``None`` path becomes JSON ``null`` when the placeholder occupies a
      whole value position (``"{{x}}"``) and empty text when embedded inside
      a larger string. Consumers therefore see ``None`` / ``""`` instead of
      the literal ``{{...}}`` template — no downstream "unsubstituted
      template" sniffing is needed.
    - Legacy single-brace ``{path}`` placeholders keep the historical
      leave-literal behavior on a miss, so prose that legitimately contains
      ``{word}`` is never silently deleted.
    """
    import json
    import re

    data_str = json.dumps(data)
    pattern = (
        r'"\{\{([\w\.\[\]]+)\}\}"'
        r'|\{\{([\w\.\[\]]+)\}\}'
        r'|"\{([\w\.\[\]]+)\}"'
        r'|\{([\w\.\[\]]+)\}'
    )

    def _walk(expression: str) -> tuple[bool, Any]:
        """Resolve a dot/index path; returns (found, value)."""
        parts = re.split(r"\.(?![^\[]*\])", expression)
        value: Any = context
        for part in parts:
            if value is None:
                return False, None

            array_match = re.match(r"^(\w+)\[(\d+)\]$", part)
            if array_match:
                field_name = array_match.group(1)
                index = int(array_match.group(2))
                if isinstance(value, dict):
                    value = value.get(field_name)
                else:
                    return False, None
                if isinstance(value, list):
                    if 0 <= index < len(value):
                        value = value[index]
                    else:
                        return False, None
                else:
                    return False, None
            elif re.match(r"^\[(\d+)\]$", part):
                index = int(part[1:-1])
                if isinstance(value, list):
                    if 0 <= index < len(value):
                        value = value[index]
                    else:
                        return False, None
                else:
                    return False, None
            else:
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    return False, None
        return True, value

    def replace_match(match: Any) -> str:
        expression = match.group(1) or match.group(2) or match.group(3) or match.group(4)
        has_quotes = (match.group(1) is not None) or (match.group(3) is not None)
        is_canonical = (match.group(1) is not None) or (match.group(2) is not None)

        found, value = _walk(expression)

        if not found or value is None:
            if is_canonical:
                # Strict {{...}} semantics: whole-value → null, embedded → "".
                return "null" if has_quotes else ""
            return match.group(0)

        if has_quotes:
            # Placeholder occupies a whole JSON value position — the value
            # replaces it verbatim (str stays str, list stays list, ...).
            return json.dumps(value)
        # Embedded inside an existing JSON string literal — render the
        # value as text (JSON for non-strings, e.g. a list of acceptance
        # checks) and escape it so the enclosing JSON string stays valid.
        text = value if isinstance(value, str) else json.dumps(value)
        return json.dumps(text)[1:-1]

    result_str = re.sub(pattern, replace_match, data_str)
    return json.loads(result_str)

