"""
Motet - Agentic Loop Skills / Artifact Sidecar Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
    Skills activation and artifact-view sidecar helpers for the agentic loop
    (issue #147). Pure extraction from agentic_loop.py with no behavior change:
    attachment-tool pin names, artifact_view staged-media sidecar construction
    and eviction, and progressive exposure of runner tools advertised by
    ``core.activate_skill`` results.

Dependencies:
    - structlog: Structured logging for distributed tracing
    - AgenticLoopData: tool_filter_metadata / tools mutation after skill activation
    - loop_discovery: ensure_tool_filter_required_tools, tool_schema_name
    - Message / TextPart / MediaPart: canonical sidecar message construction
    - ToolSchemaExporter: resolve runner tool schemas after activation

Usage:
    from motet.core.reasoning.react.loop_skills import (
        build_artifact_view_sidecar,
        expose_activated_skill_runner_tools,
        evict_stale_artifact_view_sidecars,
    )

    sidecar = build_artifact_view_sidecar(
        tool_call, raw_result, current_iteration=1
    )

Notes:
    - Mechanically extracted from agentic_loop.py (issue #147).
    - This module is the home of these symbols: import and patch them here.
      agentic_loop calls the attachment-pin and skill-exposure helpers;
      build_artifact_view_sidecar is called from loop_execution.
    - Naming: helpers crossing a module boundary are public; the sidecar kind and
      max-age constants stay module-private. A leading underscore on a
      cross-module name makes Pyright report the definition as unaccessed and
      the importer as reportPrivateUsage.
    - Artifact_view sidecars inject staged frame MediaParts as synthetic user
      messages with content_kind metadata; stale image parts are evicted after
      ``_SIDECAR_MAX_AGE_ITERATIONS`` while text breadcrumbs remain.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import structlog

from ...types import MediaPart, Message, TextPart, tool_schema_name
from .agentic_loop_data import AgenticLoopData
from .loop_discovery import ensure_tool_filter_required_tools

logger = structlog.get_logger(__name__)


def _activation_runner_tool_names_from_result(result: Any) -> List[str]:
    """Extract callable tools advertised by core.activate_skill results."""
    payload = result
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if isinstance(payload, dict) and payload.get("status") == "success" and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if not isinstance(payload, dict):
        return []

    raw_tools = payload.get("tools") or payload.get("runner_tools") or []
    if not isinstance(raw_tools, list):
        raw_tools = []

    names: List[str] = []
    for item in raw_tools:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    execution = payload.get("execution")
    if isinstance(execution, dict):
        tool_name = str(execution.get("tool") or "").strip()
        if tool_name and tool_name not in names:
            names.append(tool_name)
    return names


ATTACHMENT_TOOL_NAMES = (
    "core.artifact_read",
    "core.search_artifacts",
    "core.artifact_view",
)
ARTIFACT_VIEW_TOOL_NAMES = {"core.artifact_view", "core__artifact_view"}
_ARTIFACT_VIEW_SIDECAR_KIND = "artifact_view_sidecar"
_SIDECAR_MAX_AGE_ITERATIONS = 2


def conversation_has_attachments(history: List[Any]) -> bool:
    for msg in reversed(history):
        role = getattr(msg, "role", None) if not isinstance(msg, dict) else msg.get("role")
        if role != "user":
            continue
        attachments = getattr(msg, "attachments", None) if not isinstance(msg, dict) else msg.get("attachments")
        return bool(attachments)
    return False


def evict_stale_artifact_view_sidecars(
    history: List[Any],
    *,
    current_iteration: int,
    max_age_iterations: int = _SIDECAR_MAX_AGE_ITERATIONS,
) -> None:
    """Drop image parts from old artifact_view sidecars while keeping text breadcrumbs."""

    for msg in history:
        metadata = getattr(msg, "metadata", None) or {}
        if metadata.get("content_kind") != _ARTIFACT_VIEW_SIDECAR_KIND:
            continue
        created_iteration = int(metadata.get("iteration") or 0)
        if current_iteration - created_iteration < max_age_iterations:
            continue
        parts = list(getattr(msg, "content_parts", None) or [])
        text_parts = [part for part in parts if getattr(part, "type", None) == "text"]
        if text_parts:
            msg.content_parts = text_parts  # type: ignore[attr-defined]
        elif getattr(msg, "content", None):
            msg.content_parts = [TextPart(text=str(msg.content))]  # type: ignore[attr-defined]


def build_artifact_view_sidecar(
    tool_call: Dict[str, Any],
    raw_result: Dict[str, Any],
    *,
    current_iteration: int,
) -> Optional[Message]:
    """Build a synthetic user sidecar message for staged artifact_view frames."""

    if not isinstance(raw_result, dict) or not raw_result.get("sidecar_required"):
        return None
    staged_media = raw_result.get("staged_media")
    if not isinstance(staged_media, list) or not staged_media:
        return None

    tool_call_id = str(tool_call.get("tool_call_id") or "")
    artifact_id = str(raw_result.get("artifact_id") or "")
    timestamps = [str(value) for value in (raw_result.get("timestamps_ms") or [])]
    timestamp_label = "/".join(timestamps) if timestamps else "unknown"
    text = (
        f"Retrieved frames for artifact {artifact_id} at {timestamp_label}ms "
        f"— evidence for tool call {tool_call_id}, not a new user message."
    )
    content_parts: List[Any] = [TextPart(text=text)]
    for item in staged_media:
        if not isinstance(item, dict):
            continue
        frame_id = str(item.get("artifact_id") or "").strip()
        if not frame_id:
            continue
        content_parts.append(
            MediaPart(
                media_type="image",
                mime_type=str(item.get("mime_type") or "image/jpeg"),
                artifact_id=frame_id,
                detail="auto",
            )
        )

    if len(content_parts) == 1:
        return None

    return Message(
        role="user",
        content=text,
        content_parts=content_parts,
        metadata={
            "content_kind": _ARTIFACT_VIEW_SIDECAR_KIND,
            "tool_call_id": tool_call_id,
            "artifact_id": artifact_id,
            "iteration": current_iteration,
        },
    )


def expose_activated_skill_runner_tools(
    tool_calls: List[Dict[str, Any]],
    tool_results: List[Dict[str, Any]],
    data: AgenticLoopData,
    motet: Any,
) -> List[str]:
    """
    Pin runner tools returned by core.activate_skill so the next iteration can call them.

    Runner definitions still come from the bundle's registered ``runners.yaml`` tools.
    This helper only changes exposure after a successful skill activation.
    """
    activated_call_ids = {
        str(call.get("tool_call_id") or "")
        for call in tool_calls
        if str(call.get("tool_name") or "") in {"core.activate_skill", "core__activate_skill"}
    }
    if not activated_call_ids:
        return []

    runner_tool_names: List[str] = []
    for result in tool_results:
        if str(result.get("tool_call_id") or "") not in activated_call_ids:
            continue
        for name in _activation_runner_tool_names_from_result(result.get("result")):
            if name not in runner_tool_names:
                runner_tool_names.append(name)

    if not runner_tool_names:
        return []

    data.tool_filter_metadata = ensure_tool_filter_required_tools(
        data.tool_filter_metadata,
        runner_tool_names,
    )

    tool_registry = getattr(motet, "tools", None)
    if tool_registry is None:
        return runner_tool_names

    existing = {tool_schema_name(schema) for schema in (data.tools or [])}
    missing = [name for name in runner_tool_names if name not in existing]
    if not missing:
        return runner_tool_names

    try:
        from ...tools.schema_exporter import ToolSchemaExporter

        schema_exporter = ToolSchemaExporter(
            registry=tool_registry,
            function_discovery_store=getattr(motet, "function_discovery_store", None),
        )
        extra = schema_exporter.export_canonical(
            preselected_tools=missing,
            max_tools=len(missing),
        )
    except Exception as e:
        logger.warning(
            "agentic_loop_activate_skill_runner_schema_export_failed",
            tools=missing,
            error=str(e),
        )
        return runner_tool_names

    if extra:
        data.tools = list(data.tools or []) + [
            schema for schema in extra if tool_schema_name(schema) not in existing
        ]
        logger.info(
            "agentic_loop_activated_skill_runner_tools_exposed",
            tools=runner_tool_names,
            added=len(extra),
        )
    return runner_tool_names
