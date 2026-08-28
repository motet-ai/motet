"""
Motet - Agentic Loop Observation Formatting Helpers

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Observation formatting helpers for the agentic loop (issue #147). Pure
    extraction from agentic_loop.py with no behavior change: workflow step
    summaries for LLM consumption, MCP result text extraction, and the
    per-tool observation clip policy (MOTET_AGENTIC_TOOL_OBSERVATION_MAX_CHARS)
    applied before tool messages enter conversation history. Transcript-wide
    compaction is not here; rebuild it later if a long tool-heavy turn needs
    it.

Dependencies:
    - os: Read MOTET_AGENTIC_TOOL_OBSERVATION_MAX_CHARS for clip budget
    - motet.core.tools.result_formatting: Shared MCP text unwrap

Usage:
    from motet.core.reasoning.react.loop_observations import (
        format_workflow_steps,
        extract_text_from_mcp_result,
        clip_observation,
        _TOOL_OBSERVATION_MAX_CHARS,
    )

    text = format_workflow_steps(workflow_result)
    clipped = clip_observation(text, artifact_id=artifact_id)

Notes:
    - Mechanically extracted from agentic_loop.py (issue #147 Priority 1).
    - Clip policy stays local here; tools with contextualize_observation=False
      (e.g. core.worker_exec / core.edge_exec) skip ContextManager summarization,
      so this is the only guard between a large capture and the model context.
    - MCP text unwrap lives in motet.core.tools.result_formatting and is
      re-exported here so loop_execution / tests keep a stable import path.
    - Naming: the three helpers are public (no leading underscore) because they
      cross a module boundary; only the clip budget stays module-private. A
      leading underscore here would make Pyright report the definition as
      unaccessed and the importer as reportPrivateUsage.
    - loop_execution is the production caller for all three helpers; agentic_loop
      does not use them directly. Import and patch them here.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from motet.core.tools.result_formatting import extract_text_from_mcp_result


def format_workflow_steps(workflow_result: Dict[str, Any]) -> str:
    """
    Format workflow execution steps into readable text for LLM consumption.

    For the **final step** of a successful workflow, surfaces content without
    any length trimming so the LLM receives the actual workflow payload and
    does not need to fabricate a response from partial context.

    Primary output field resolution (in order):
    1. ``workflow_result["output_field"]`` — explicit declaration in the workflow
       YAML (e.g. ``output_field: digest_markdown``).  The named field is read
       from the final step's data and included verbatim.
    2. Fallback: the ``result`` key from the final step's data, also untrimmed.
       Intermediate steps always use this fallback.

    Workflow authors who want rich content (markdown, reports, summaries, etc.)
    surfaced to the LLM should declare ``output_field`` in their workflow YAML.
    Without it the LLM only sees whatever is in the ``result`` key of each step.

    Args:
        workflow_result: Workflow execution result dict produced by WorkflowExecutor,
            containing ``step_results``, ``workflow_id``, ``workflow_name``,
            ``execution_time_ms``, and optionally ``output_field``.

    Returns:
        Formatted text describing workflow execution and steps for LLM consumption.
    """
    if not isinstance(workflow_result, dict):
        return str(workflow_result)

    workflow_name = workflow_result.get("workflow_name", workflow_result.get("workflow_id", "Unknown Workflow"))
    step_results = workflow_result.get("step_results", {})
    execution_time_ms = workflow_result.get("execution_time_ms", 0)
    output_field = workflow_result.get("output_field")

    if not step_results:
        return f"Workflow '{workflow_name}' completed (no steps executed)"

    step_ids = list(step_results.keys())
    last_step_id = step_ids[-1] if step_ids else None

    lines = [
        f"✅ Workflow '{workflow_name}' completed successfully",
        f"Execution time: {execution_time_ms:.0f}ms",
        "",
        "**Workflow Steps:**",
    ]

    for step_id, step_result in step_results.items():
        if not isinstance(step_result, dict):
            continue

        step_status = step_result.get("status", "unknown")
        step_data = step_result.get("data") or {}

        step_name = step_data.get("name", step_id) if isinstance(step_data, dict) else step_id
        tool_name = step_data.get("tool_name", "") if isinstance(step_data, dict) else ""

        status_icon = "✅" if step_status == "success" else "❌"
        lines.append(f"\n{status_icon} **{step_name}** ({step_id})")

        if tool_name:
            lines.append(f"   Tool: {tool_name}")

        if isinstance(step_data, dict):
            is_last_step = step_id == last_step_id

            if is_last_step and step_status == "success" and output_field:
                # Workflow declared its primary output field — surface it in full.
                rich_output = step_data.get(output_field)
                if rich_output:
                    lines.append(f"\n{rich_output}")
                else:
                    # The declared field was empty; fall back to result key.
                    result_val = step_data.get("result")
                    if isinstance(result_val, str) and result_val:
                        lines.append(f"   Result: {result_val}")
            else:
                # Intermediate steps, or final step without output_field declaration:
                # show the result key — no trim.
                step_result_data = step_data.get("result") or {}
                if isinstance(step_result_data, dict):
                    result_text = extract_text_from_mcp_result(step_result_data)
                    if result_text:
                        lines.append(f"   Result: {result_text}")
                elif isinstance(step_result_data, str) and step_result_data:
                    lines.append(f"   Result: {step_result_data}")

        if step_status != "success":
            error = step_result.get("error", "Unknown error")
            lines.append(f"   Error: {error}")

    return "\n".join(lines)


# extract_text_from_mcp_result is imported from result_formatting (shared with
# core.transform mcp_text). Re-exported so loop_execution and tests keep this path.


#: Max characters of a single tool result surfaced to the LLM as a tool message.
#: Tools registered with ``contextualize_observation=False`` (e.g. core.worker_exec /
#: core.edge_exec) skip ContextManager summarization, so this is the only
#: guard between a large capture (default 1 MiB) and the model context window.
#: Default 8k (was 32k) to keep multi-tool implement chunks from ballooning
#: prompt tokens; full results may still be in TOOL_ARTIFACT for artifact_read.
_TOOL_OBSERVATION_MAX_CHARS = int(
    os.getenv("MOTET_AGENTIC_TOOL_OBSERVATION_MAX_CHARS", "8000")
)


def clip_observation(
    text: str,
    limit: int = 0,
    *,
    artifact_id: Optional[str] = None,
) -> str:
    """Clip an observation string to ``limit`` chars, preferring the tail.

    CLI tools (pytest, compose, git) put the verdict at the end of their output,
    so keep a short head for context and the remainder as tail — mirroring
    ``motet.core.execution.capture.truncate_output_pair``. When truncated and an
    ``artifact_id`` is available, append a pointer so the model can fetch the
    full payload via ``core.artifact_read`` if needed.
    """
    limit = limit or _TOOL_OBSERVATION_MAX_CHARS
    if limit <= 0 or len(text) <= limit:
        return text
    pointer = ""
    aid = (artifact_id or "").strip()
    if aid:
        pointer = f"\n[full result in artifact_id={aid}; use core.artifact_read if needed]"
    marker = "\n...[observation truncated]...\n"
    budget = max(limit - len(pointer), 64)
    keep_head = max((budget - len(marker)) // 4, 0)
    keep_tail = budget - len(marker) - keep_head
    if keep_tail <= 0:
        return text[-budget:] + pointer
    return text[:keep_head] + marker + text[-keep_tail:] + pointer
