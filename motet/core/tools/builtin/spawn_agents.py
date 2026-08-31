"""
Motet - Spawn Agents Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Parallel sub-agent fan-out as an ordinary tool.

    The calling loop names concrete work items; each becomes one sub-agent
    running on its own Celery worker via the ``agent_loop`` command. Children write
    tokens and thinking to the parent task stream with ``{parent}.spawn-N``
    as ``agent_id``. Each child is claimed and registered at mint, and the
    spawn instruction is persisted as that conversation's first user
    message so Explorer can open the running child. The child is listed under
    the parent chat agent and follows up as ``core.subagent``. Results come back as a
    single observation, so the parent loop synthesizes them the way it
    handles any other tool result — holding the full turn context, and free
    to fan out again or act on what came back.

Dependencies:
    - agent_loop / AgentData: the sub-agent entry point; one Celery task per task item
      so workers actually overlap
    - MotetContext.join: parallel dispatch and fan-in
    - motet.core.conversations.children: the child-conversation lifecycle
      (mint isolated id, claim + register, brief before the run, reply row and
      card pointer after) — this tool only brackets its agent_loop calls with it
    - tool_filter_metadata (delegated on the turn's metadata): the parent's
      ToolFilter snapshot, which sub-agents inherit so they cannot reach past
      what the parent agent was granted

Usage:
    Called by the model, not by Motet code:

        spawn_agents(tasks=[
            {"instruction": "Find current pricing for the Acme enterprise tier",
             "tools": ["core.http_get_browser"]},
            {"instruction": "Summarize published Acme outage postmortems from 2026",
             "tools": ["core.web_search"]},
        ])

    Returns one result block per task, in the order the tasks were given.

Notes:
    - **Each task declares the tools its sub-agent needs.** Inheriting only the
      parent's filter left every child in discovery mode, starting blind and
      spending its whole budget on ``core.tools_search`` / ``core.tool_call``
      before reaching any real work — observed live, with all three children of
      a fan-out returning nothing after 3m52s while the parent then redid the
      work itself. Declared names become the child's ``required_tools``, which
      discovery force-includes, so the child holds real schemas immediately.

    Copying the parent's *resolved* tools is not available as an alternative:
    under progressive disclosure nobody holds a resolved list. The
    conversation shortlist is deliberately a small frozen bag of meta tools, so the parent reaches ``core.http_get_browser``
    through ``core.tool_call`` as well. Only the calling model knows what a
    given slice of work needs, so it is asked.
    - **Declaring is the child's catalog, bounded by the parent's grant.**
      Resolved schemas are passed as ``AgentData.tools``, so the loop skips
      discovery and does not admit ``core.tools_search`` / ``core.tool_call``.
      Names the parent could not use (and the always-sticky meta tools) are
      dropped rather than honored. An undeclared task still runs discovery.
      ``discover=True`` on a task is the opt-in that keeps discovery even
      when tools were declared: the names stay a ``required_tools`` pin,
      ``tools_search`` / ``tool_call`` stay available, and the child gets
      the discovery worker brief. Default remains the cage.
    - **Recursion is blocked by tool subtraction, not a depth counter.**
      Sub-agents inherit the parent's filter with ``core.spawn_agents`` added to
      ``exclude_tools``, so a child cannot fan out again. Depth 3 at width 10
      would be 1000 agents.
    - **Discovery-mode agents only.** Fan-out rests on progressive disclosure: children inherit a filter and reach the catalog through
      ``core.tools_search``. A parent whose ToolFilter is prefix/explicit/category
      has no delegable filter snapshot, and inventing one would either widen the
      child's reach past the parent's grant or silently narrow it, so the call is
      refused instead.
    - Handback tools are never inherited: they suspend the *turn*, and
      a sub-agent has nowhere to hand back to. Enforcement is structural rather
      than a name filter — they arrive as caller-supplied schemas rather than
      registry entries, so a child that is passed no handback fields cannot
      surface them through discovery.
    - **Children get a static worker system prompt, not the Motet assistant
      fallback.** Children receive a static worker system prompt, not
      ``_build_agentic_system_prompt``. The worker string comes from
      ``core.subagent`` (rails included) and is identical for every child so
      sibling first-call prefixes stay cacheable; declared tool
      names stay on ``required_tools``, not interpolated into the prompt. The
      loop's trailing wrap-up (remaining rounds) is shared with parent turns.
    - **The live observation is the children's write-ups.** Each entry
      keeps ``task`` / ``status`` / ``response`` / ``tools_used`` /
      ``stop_reason`` with the full ``final_response``. A ``text`` field
      on the envelope is that same content as prose so the parent loop
      does not have to ``artifact_read`` to see what it already paid
      for. A copy is still stored as a tool artifact so the 8k
      observation clip has somewhere to point. Children write tokens and
      thinking to the parent task stream with their own ``agent_id``
      (``{parent}.spawn-N``) so the chat UI can attribute each slice.
      Child snapshot-tool
      *keys* (``http_get`` / ``http_get_browser`` / ``web_search``) are
      returned in ``meta`` so the parent can refuse the same call this
      turn. The parent does not receive the child's page body; an
      inherited hit points at this observation, not a missing local
      fetch.       Successful write-ups are stored as the first turn of an isolated
      child conversation (opaque ``iso-…`` id with parent/root pointers), registered so they
      appear in the conversation list. The parent turn keeps a slim
      card pointer (``meta.spawn_children`` on the tool envelope) rather
      than nested assistant rows. The parent loop copies those pointers
      onto the turn result so finalize can persist them on the parent
      transcript. Provider thinking, tool summaries, and cost on the child
      turn are for conversation reload and are not replayed as
      assistant content on the parent.
    - Width is capped by ``MAX_FANOUT_WIDTH``. Over the cap the call is rejected
      with the limit stated, rather than silently truncating declared work.
    - **Partial failure is recovered, not propagated.** ``join`` raises when any
      child fails even with ``fail_fast=False`` — that flag stops sibling
      cancellation, not the raise — and the branches that did succeed are on
      ``GatherExecutionError.partial_results`` in the same unwrapped shape as a
      successful join. If *every* branch fails the tool call itself errors, so
      the model does not read failure text as findings.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from motet.core.agents.registry import (
    CORE_SUBAGENT_ID,
    AgentConfig,
    builtin_subagent_config,
    get_agent_registry,
)
from motet.core.types import Message

from ..protocol import err, ok
from ..registry import ToolRegistry

logger = structlog.get_logger(__name__)

TOOL_NAME = "core.spawn_agents"

# Width rail. Each task is a worker slot held for the duration of the fan-out,
# so an unbounded list starves the pool until gather timeout (ADR-0023 Risk 5).
MAX_FANOUT_WIDTH = 8

# Stop reasons that mean the child ran out of road rather than answering —
# unless the loop finalized a tools-off write-up (``finalized=True``), in
# which case final_response is findings, not scaffolding.
INCOMPLETE_STOP_REASONS = frozenset(
    {
        "max_iterations",
        "max_model_calls",
        "max_cost",
        "max_prompt_tokens",
        "max_tool_time",
        "stalled",
        "error",
    }
)


def spawn_child_id(parent_agent_id: str, index: int) -> str:
    """Qualified id for a spawn_agents child: ``{parent}.spawn-N`` (0-based index)."""
    return f"{parent_agent_id}.spawn-{index + 1}"


def _persist_spawn_children(
    motet: Any,
    parent_agent_id: str,
    tasks: Sequence[Any],
    entries: List[Dict[str, Any]],
    child_cids: Sequence[str],
    brief_written: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Persist each successful child reply on its isolated conversation and return parent pointers."""
    if not getattr(motet, "memory", None):
        return []
    from motet.core.conversations.children import (
        complete_child_conversation,
        parent_registry_scope,
    )

    registry_agent, surface_id = parent_registry_scope(motet, parent_agent_id)
    written = brief_written or set()
    pointers: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if entry.get("status") != "success":
            continue
        text = str(entry.get("response") or "").strip()
        if not text:
            continue
        if index >= len(child_cids):
            continue
        child_cid = child_cids[index]
        if not child_cid:
            continue
        instruction = ""
        if index < len(tasks):
            instruction = str(getattr(tasks[index], "instruction", "") or "").strip()
        if not instruction:
            instruction = str(entry.get("task") or "").strip()
        pointer = complete_child_conversation(
            motet,
            child_cid=child_cid,
            reply_text=text,
            instruction=instruction,
            registry_agent_id=registry_agent,
            pointer_agent_id=spawn_child_id(parent_agent_id, index),
            surface_id=surface_id,
            brief_written=child_cid in written,
            thinking_text=str(entry.get("thinking_text") or "") or None,
            tool_summaries=entry.get("tool_summaries") or None,
            cost_usd=entry.get("cost_usd"),
        )
        if pointer:
            pointers.append(pointer)
    return pointers


class SpawnTask(BaseModel):
    """One unit of fan-out work: what to do, and what to do it with."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(
        ...,
        description=(
            "Self-contained instruction for one sub-agent. The sub-agent sees "
            "only this text, not the conversation, so name any context it needs."
        ),
    )
    tools: List[str] = Field(
        default_factory=list,
        description=(
            "Exact tool names this sub-agent may use, e.g. 'core.web_search'. "
            "Naming them is the child's catalog — it cannot search for more "
            "unless discover is true. Leave empty only if the work needs no "
            "tools: a sub-agent that has to go find its own tools usually "
            "spends its whole budget doing so and returns nothing. Names you "
            "cannot use yourself are ignored."
        ),
    )
    discover: bool = Field(
        default=False,
        description=(
            "Opt in to catalog search for this task. Default is false: named "
            "tools are the child's only catalog. Set true only when the slice "
            "may need tools you cannot name in advance; the child can then "
            "spend its budget searching. Declared names are still pinned so "
            "they show up immediately."
        ),
    )


class SpawnAgentsParams(BaseModel):
    """Parameters for the parallel sub-agent fan-out tool.

    This declares the schema the model sees, and — because ``tool_execution``
    validates parameters against it before entering the tool — it is also the
    effective gate on shape and count. Advertising ``minItems`` / ``maxItems``
    is what keeps violations rare in the first place; ``run_spawn_agents``
    re-checks only for callers that bypass this layer.
    """

    model_config = ConfigDict(extra="forbid")

    tasks: List[SpawnTask] = Field(
        ...,
        description=(
            "Independent work items to run in parallel, one sub-agent each. Use "
            "for work whose parts do not depend on each other's results."
        ),
        min_length=2,
        max_length=MAX_FANOUT_WIDTH,
    )

    @field_validator("tasks", mode="before")
    @classmethod
    def _accept_bare_instructions(cls, value: Any) -> Any:
        """Tolerate ``tasks: ["do a", "do b"]`` from models that skip the object form.

        The advertised schema is objects, because that is what prompts the model
        to declare tools at all. Coercing the string form keeps a reasonable call
        from failing validation over shape.
        """
        if isinstance(value, list):
            return [{"instruction": v} if isinstance(v, str) else v for v in value]
        return value


def _get_motet_context_optional() -> Any:
    """Return the current MotetContext when running inside tool_execution."""
    try:
        from motet.core.commands.decorator import get_motet_context

        return get_motet_context()
    except Exception:
        return None


def _subagent_config() -> AgentConfig:
    """Live ``core.subagent`` config, or the shipped builtin if unregistered."""
    try:
        cfg = get_agent_registry().get(CORE_SUBAGENT_ID)
        if cfg is not None:
            return cfg
    except Exception:
        pass
    return builtin_subagent_config()


def _child_conversation_history(*, discover: bool = False) -> List[Message]:
    """Worker brief from ``core.subagent`` so first turn and follow-up match.

    Two static strings, one per grant, so siblings that share a mode keep a
    cacheable prefix. The child's static rails are in the string; live
    remaining counts arrive as the loop's trailing wrap-up. Declared tool
    names are not interpolated — they land on ``required_tools`` and, when
    caged, on ``AgentData.tools``.
    """
    cfg = _subagent_config()
    prompt = str(cfg.system_prompt or "")
    if discover:
        meta = cfg.metadata if isinstance(cfg.metadata, dict) else {}
        stored = meta.get("discovery_system_prompt")
        if stored:
            prompt = str(stored)
    return [Message(role="system", content=prompt)]


def _subagent_loop_rails() -> Dict[str, Any]:
    """First-turn loop rails from the registered ``core.subagent``."""
    cfg = _subagent_config()
    meta = cfg.metadata if isinstance(cfg.metadata, dict) else {}
    raw_tool_time = meta.get("max_tool_time_ms")
    try:
        max_tool_time_ms = int(raw_tool_time) if raw_tool_time is not None else None
    except (TypeError, ValueError):
        max_tool_time_ms = None
    return {
        "max_iterations": int(cfg.max_iterations),
        "max_tools": int(cfg.max_tools),
        "max_cost_usd": cfg.max_cost_usd,
        "max_prompt_tokens": cfg.max_prompt_tokens,
        "max_tool_time_ms": max_tool_time_ms,
    }


def _always_sticky_tool_names() -> frozenset:
    """Deferred: importing tool_shortlist at module load reorders the registry."""
    from ...reasoning.react.tool_shortlist import ALWAYS_STICKY_TOOL_NAMES

    return frozenset(ALWAYS_STICKY_TOOL_NAMES)


def _allowed_declared_tools(
    parent_filter_metadata: Dict[str, Any],
    declared_tools: Sequence[str],
) -> List[str]:
    """Declared names the parent could itself call, minus discovery meta tools."""
    excluded = {
        *(parent_filter_metadata.get("exclude_tools") or []),
        TOOL_NAME,
        *_always_sticky_tool_names(),
    }
    return list(dict.fromkeys(name for name in declared_tools if name and name not in excluded))


def resolve_child_tool_schemas(motet: Any, names: Sequence[str]) -> Optional[List[Any]]:
    """Export canonical schemas for *names*. ``None`` means do not cage.

    An empty export must not become ``tools=[]``: that is an explicit no-tools
    turn, not discovery. Falling back to ``None`` keeps the required_tools pin
    and lets the child search — worse than a cage, better than a mute worker.

    Also used by ``agent_turn`` to rebuild the spawn tool cage from the
    child's stored spawn contract on follow-up turns.
    """
    if not names:
        return None
    registry = getattr(motet, "tools", None)
    if registry is None:
        return None
    from motet.core.tools.schema_exporter import ToolSchemaExporter

    exporter = ToolSchemaExporter(
        registry=registry,
        function_discovery_store=getattr(motet, "function_discovery_store", None),
    )
    schemas = exporter.export_canonical(
        preselected_tools=list(names),
        max_tools=len(names),
    )
    return schemas or None


def _child_filter_metadata(
    parent_filter_metadata: Dict[str, Any],
    declared_tools: Sequence[str] = (),
    *,
    caged: bool = False,
) -> Dict[str, Any]:
    """
    Parent's ToolFilter snapshot with ``core.spawn_agents`` subtracted.

    When *caged*, declared names are the child's catalog: they become
    ``required_tools`` and the always-sticky discovery tools are excluded so
    ``core.tool_call`` cannot reopen the parent's grant. When not caged
    (undeclared, or schemas failed to resolve), declared names stay a
    ``required_tools`` pin and discovery remains.

    ADR-0138 decision 6: recursion is bounded here rather than by a depth
    counter. The same subtraction is why a task cannot declare
    ``core.spawn_agents`` back into existence.

    Handback tools need no entry in this list. They are caller-supplied schemas
    injected from ``AgenticLoopData.handback_tools``, not registry entries, so a
    child that is never handed them cannot discover them — the AgentData built
    below passes no handback fields.
    """
    sticky = _always_sticky_tool_names()
    child = dict(parent_filter_metadata)
    excluded = {*(child.get("exclude_tools") or []), TOOL_NAME}
    if caged:
        excluded.update(sticky)
    child["exclude_tools"] = sorted(excluded)

    if caged:
        child["required_tools"] = _allowed_declared_tools(
            parent_filter_metadata, declared_tools
        )
        return child

    pinned = [
        name
        for name in (*(child.get("required_tools") or []), *declared_tools)
        if name and name not in excluded and name not in sticky
    ]
    child["required_tools"] = list(dict.fromkeys(pinned))
    return child


def _normalize_tasks(raw_tasks: Any) -> List[SpawnTask]:
    """Coerce the ``tasks`` payload into SpawnTask objects, dropping blanks.

    Accepts the object form, the bare-string form, and already-constructed
    SpawnTask instances, because this function is reached both through
    ``tool_execution`` (validated) and by direct calls (not).
    """
    if not isinstance(raw_tasks, list):
        return []

    tasks: List[SpawnTask] = []
    for item in raw_tasks:
        if isinstance(item, SpawnTask):
            candidate = item
        elif isinstance(item, str):
            candidate = SpawnTask(instruction=item)
        elif isinstance(item, dict):
            instruction = item.get("instruction")
            if not isinstance(instruction, str):
                continue
            declared = item.get("tools")
            candidate = SpawnTask(
                instruction=instruction,
                tools=[str(t) for t in declared if str(t or "").strip()]
                if isinstance(declared, list)
                else [],
                discover=bool(item.get("discover")),
            )
        else:
            continue

        instruction = candidate.instruction.strip()
        if instruction:
            tasks.append(
                SpawnTask(
                    instruction=instruction,
                    tools=list(candidate.tools),
                    discover=candidate.discover,
                )
            )
    return tasks


def _summarize(result: Any) -> str:
    """Pull the answer text out of an agent result."""
    if isinstance(result, dict):
        for key in ("final_response", "content", "response"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _thinking_text(result: Any) -> str:
    """Display-only reasoning from a child loop result."""
    from motet.core.orchestration.turn.complete import extract_thinking_text

    return extract_thinking_text(result) or ""


def _tool_summaries(result: Any) -> List[Dict[str, Any]]:
    """Display-only tool name/status/preview rows from a child loop result."""
    from motet.core.orchestration.turn.complete import extract_tool_summaries
    from motet.core.reasoning.react.loop_results import summarize_tool_results

    stored = extract_tool_summaries(result)
    if stored:
        return stored
    if not isinstance(result, dict):
        return []
    candidates: List[Any] = [result]
    for key in ("data", "result"):
        nested = result.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        rows = summarize_tool_results(candidate.get("tool_results") or [])
        if rows:
            return rows
    return []


def _cost_usd(result: Any) -> Optional[float]:
    """Priced loop cost from a child result, or None when unpriced."""
    from motet.core.orchestration.turn.complete import extract_turn_cost

    return extract_turn_cost(result)


def _stop_reason(result: Any) -> str:
    """How the sub-agent's loop ended, per the loop's terminal contract."""
    if isinstance(result, dict):
        value = result.get("stop_reason")
        if isinstance(value, str):
            return value
    return ""


def _tools_used(result: Any) -> List[str]:
    """Tool names a sub-agent actually ran, for the observation's provenance.

    Rail stops leave ``tool_results`` empty. ``executed_signatures`` still
    names every Motet-tool call as ``tool_name:hash``.
    """
    if not isinstance(result, dict):
        return []
    names = [
        str(name)
        for entry in (result.get("tool_results") or [])
        if isinstance(entry, dict) and entry.get("status") == "success"
        for name in [entry.get("tool_name")]
        if name
    ]
    if not names:
        for signature in result.get("executed_signatures") or []:
            if not isinstance(signature, str) or ":" not in signature:
                continue
            names.append(signature.split(":", 1)[0])
    return list(dict.fromkeys(names))


def _collect_snapshot_state(results: Sequence[Any]) -> tuple[Dict[str, Any], List[str]]:
    """Merge snapshot-tool cache entries from every child loop result."""
    from ..cache_control import inherit_snapshot_cache

    merged_cache: Dict[str, Any] = {}
    merged_signatures: List[str] = []
    for raw in results:
        if not isinstance(raw, dict) or raw.get("_error"):
            continue
        inherit_snapshot_cache(
            merged_cache,
            merged_signatures,
            raw.get("observation_cache"),
            raw.get("executed_signatures"),
        )
    return merged_cache, merged_signatures


def _store_full_fanin(motet: Any, full_entries: List[Dict[str, Any]]) -> Optional[str]:
    """Persist uncapped results so the parent can artifact_read the rest."""
    store = getattr(motet, "artifact_store", None)
    if store is None or not hasattr(store, "put"):
        return None
    try:
        from motet.core.artifacts import ArtifactKind
        from motet.core.config import Config

        ttl = int(getattr(Config(), "tool_result_artifact_ttl_seconds", 604800) or 604800)
        metadata = {
            "source": TOOL_NAME,
            "task_id": getattr(motet, "task_id", None),
            "conversation_id": getattr(motet, "conversation_id", None),
        }
        return store.put(
            payload={"results": full_entries},
            content_type="application/json",
            metadata={k: v for k, v in metadata.items() if v is not None},
            ttl_seconds=ttl,
            kind=ArtifactKind.TOOL_ARTIFACT,
        )
    except Exception as exc:
        logger.warning(
            "spawn_agents_artifact_store_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return None


def _format_observation(res: Dict[str, Any]) -> str:
    """Parent-visible prose: every child's write-up, in task order."""
    if res.get("status") != "success":
        return f"spawn_agents(error={res.get('error')})"
    inner = res.get("result")
    if not isinstance(inner, dict):
        return "spawn_agents(ok)"
    lines: List[str] = []
    for index, entry in enumerate(inner.get("results") or [], start=1):
        if not isinstance(entry, dict):
            continue
        status = entry.get("status") or "unknown"
        reason = entry.get("stop_reason") or ""
        header = f"{index}. [{status}] {entry.get('task') or ''}"
        if reason:
            header += f" ({reason})"
        lines.append(header.strip())
        response = str(entry.get("response") or "").strip()
        if response:
            lines.append(response)
        elif entry.get("error"):
            lines.append(str(entry.get("error")))
        lines.append("")
    return "\n".join(lines).strip()


def run_spawn_agents(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run each task as a parallel sub-agent and return their results together.

    Args:
        params: ``{"tasks": [{"instruction": ..., "tools": [...]}, ...]}``
            validated against SpawnAgentsParams. Bare instruction strings are
            accepted and coerced.

    Returns:
        Tool protocol envelope. On success, ``result.results`` holds one
        row per task in submission order: ``task``, full ``response``,
        ``tools_used``, ``stop_reason``, and a ``status`` of ``success``,
        ``incomplete`` (budget/stall without a finalized write-up), or
        ``error``. Only ``success`` rows carry response text. Envelope
        ``text`` is the same write-ups as prose for the parent
        observation. A copy is stored on ``result.artifact_id`` when the
        store is available so an 8k clip still has a pointer. Child
        snapshot-tool cache entries and parent card pointers
        (``meta.spawn_children``) are in ``meta`` for the parent loop
        to inherit. The call errors when no child answered, so an
        all-empty fan-out cannot read as findings.
    """
    # In the normal path these checks do not fire: tool_execution validates
    # parameters against the registered SpawnAgentsParams schema first, so a
    # bad shape or count is already rejected with Pydantic's wording. They stay
    # for the two cases that reach here anyway — direct `run_spawn_agents`
    # calls, which skip that layer, and whitespace-only instructions, which
    # satisfy `minItems: 2` and so are invisible to the schema.
    if not isinstance(params.get("tasks"), list):
        return err("spawn_agents requires a 'tasks' list of work items.")

    tasks = _normalize_tasks(params.get("tasks"))
    if len(tasks) < 2:
        return err(
            "spawn_agents needs at least 2 non-empty tasks. For a single piece of "
            "work, call the tool it needs directly."
        )
    if len(tasks) > MAX_FANOUT_WIDTH:
        # Reject rather than truncate: silently dropping declared work would let
        # the model believe it ran. The schema's maxItems is what the model
        # actually sees, and normally rejects this before the tool is entered.
        return err(
            f"spawn_agents accepts at most {MAX_FANOUT_WIDTH} tasks, got {len(tasks)}. "
            "Group related items, or run the fan-out in more than one call."
        )

    motet = _get_motet_context_optional()
    if motet is None:
        return err(
            "spawn_agents requires a distributed command context and cannot run "
            "in-process."
        )

    metadata = dict(getattr(motet, "metadata", {}) or {})
    parent_filter_metadata = metadata.get("tool_filter_metadata")
    if not isinstance(parent_filter_metadata, dict):
        # See module docstring: no delegable filter means no faithful inheritance.
        return err(
            "spawn_agents is only available to discovery-mode agents, because "
            "sub-agents inherit the parent's tool filter. Run the work directly, "
            "or use a workflow for declared parallel steps."
        )

    from motet.core.commands.response_models import GatherExecutionError
    from motet.core.reasoning.react import AgentData, agent_loop
    from motet.core.reasoning.react.agent_data import (
        DEFAULT_MODEL_NAME,
        DEFAULT_MODEL_PROVIDER,
    )
    from motet.core.reasoning.reasoning_events import emit_reasoning_event

    # One catalog per task. Default: declared names that resolve become
    # AgentData.tools (the cage). discover=True, or no declared names, keeps
    # the required_tools pin and stays on the discovery path.
    allowed_by_task = [
        _allowed_declared_tools(parent_filter_metadata, task.tools) for task in tasks
    ]
    wants_discovery = [
        bool(task.discover) or not allowed_by_task[index]
        for index, task in enumerate(tasks)
    ]
    schemas_by_task = [
        None
        if wants_discovery[index]
        else resolve_child_tool_schemas(motet, allowed_by_task[index])
        for index in range(len(tasks))
    ]
    child_filters = [
        _child_filter_metadata(
            parent_filter_metadata,
            task.tools,
            caged=schemas_by_task[index] is not None,
        )
        for index, task in enumerate(tasks)
    ]

    parent_agent_id = metadata.get("agent_id") or "agent"
    stream_key = getattr(motet, "stream_key", None)
    parent_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    parent_cid = str(getattr(motet, "conversation_id", None) or "").strip()
    children_by_index: List[Any] = []
    if parent_cid:
        from motet.core.conversations.children import (
            create_child_conversation,
            parent_registry_scope,
        )

        registry_agent, surface_id = parent_registry_scope(motet, parent_agent_id)
        for index, task in enumerate(tasks):
            children_by_index.append(
                create_child_conversation(
                    motet,
                    instruction=task.instruction,
                    registry_agent_id=registry_agent,
                    pointer_agent_id=spawn_child_id(parent_agent_id, index),
                    surface_id=surface_id,
                    kind="spawn",
                    turn_agent_id=CORE_SUBAGENT_ID,
                    spawn_contract={
                        "discover": wants_discovery[index],
                        "tools": list(allowed_by_task[index]),
                        "tool_filter_metadata": child_filters[index],
                    },
                )
            )
    else:
        children_by_index = [None] * len(tasks)

    child_cids: List[str] = [
        child.conversation_id if child is not None else "" for child in children_by_index
    ]
    early_pointers: List[Dict[str, Any]] = [
        child.pointer for child in children_by_index if child is not None
    ]
    brief_written: Set[str] = {
        child.conversation_id
        for child in children_by_index
        if child is not None and child.brief_written
    }

    emit_reasoning_event(
        motet,
        strategy="agentic_loop",
        step=1,
        thought=f"Running {len(tasks)} tasks in parallel",
        action="spawn_agents",
        observation="; ".join(t.instruction[:80] for t in tasks),
        stream_key=stream_key,
        spawn_children=early_pointers or None,
    )

    undeclared = sum(1 for names in allowed_by_task if not names)
    logger.info(
        "spawn_agents_started",
        task_count=len(tasks),
        parent_agent_id=parent_agent_id,
        # Children with no declared tools must go find their own, which is what
        # exhausted every child of the fan-out that motivated this field.
        undeclared_tool_tasks=undeclared,
        discover_tasks=sum(1 for flag in wants_discovery if flag),
        task_id=getattr(motet, "task_id", None),
    )

    child_ids = [spawn_child_id(parent_agent_id, index) for index in range(len(tasks))]
    rails = _subagent_loop_rails()
    calls = []
    for index, task in enumerate(tasks):
        child_data = AgentData(
            agent_id=child_ids[index],
            parent_agent_id=parent_agent_id,
            use_task_stream=True,
            base_stream_key=stream_key,
            metadata={
                **parent_metadata,
                "agent_id": child_ids[index],
                "parent_agent_id": parent_agent_id,
                **(
                    {
                        "parent_conversation_id": children_by_index[index].parent_conversation_id,
                        "root_conversation_id": children_by_index[index].root_conversation_id,
                    }
                    if children_by_index[index] is not None
                    else {}
                ),
            },
            input=task.instruction,
            # Worker brief only. The parent transcript would make a child
            # re-answer the user's original question; an empty history would
            # let the loop inject the Motet assistant fallback (MUST fetch
            # via http_get_browser). Same string on every sibling so the
            # system prefix stays cacheable; declared tools are already on
            # required_tools.
            conversation_history=_child_conversation_history(
                discover=wants_discovery[index]
            ),
            tool_filter_metadata=child_filters[index],
            # None keeps discovery. A list (even of one schema) is the cage:
            # the loop will not rebuild a shortlist or persist it onto the
            # parent conversation.
            tools=schemas_by_task[index],
            max_iterations=rails["max_iterations"],
            max_tools=rails["max_tools"],
            max_cost_usd=rails["max_cost_usd"],
            max_prompt_tokens=rails["max_prompt_tokens"],
            max_tool_time_ms=rails["max_tool_time_ms"],
            model_provider=metadata.get("model_provider") or DEFAULT_MODEL_PROVIDER,
            model_name=metadata.get("model_name") or DEFAULT_MODEL_NAME,
            model_profile_name=metadata.get("model_profile_name"),
            enable_thinking=bool(metadata.get("enable_thinking", False)),
            reasoning_effort=metadata.get("reasoning_effort") or "medium",
        )
        child_cid = child_cids[index]
        if child_cid:
            calls.append(agent_loop(data=child_data, conversation_id=child_cid))
        else:
            calls.append((agent_loop, child_data))

    try:
        results = motet.join(calls, fail_fast=False)
    except GatherExecutionError as exc:
        # fail_fast=False stops gather from *cancelling* the siblings, but join
        # still raises when any child fails. The successful branches are on
        # partial_results, already unwrapped to the same shape as a successful
        # join. Recovering them here is the difference between one slow
        # sub-agent costing its own answer and costing all of them.
        logger.warning(
            "spawn_agents_partial_failure",
            task_count=len(tasks),
            recovered=len(exc.partial_results),
            error=exc.message,
        )
        results = list(exc.partial_results)
    except Exception as exc:
        logger.error(
            "spawn_agents_failed",
            task_count=len(tasks),
            error=str(exc),
            exc_info=True,
        )
        return err(f"Parallel sub-agents failed: {exc}")

    entries: List[Dict[str, Any]] = []
    succeeded = 0
    incomplete = 0
    for index, task in enumerate(tasks):
        raw = results[index] if index < len(results) else None
        if isinstance(raw, dict) and raw.get("_error"):
            entries.append(
                {
                    "task": task.instruction,
                    "status": "error",
                    "response": "",
                    "error": raw.get("message") or "sub-agent failed",
                    "stop_reason": "",
                    "tools_used": [],
                }
            )
            continue

        stop_reason = _stop_reason(raw)
        writeup = _summarize(raw)
        finalized = isinstance(raw, dict) and bool(raw.get("finalized")) and bool(writeup)
        if stop_reason in INCOMPLETE_STOP_REASONS and not finalized:
            # Scaffolding text ("Maximum iterations reached…"), not an answer.
            # A successful tools-off finalize is findings and is counted below.
            incomplete += 1
            reason = f"sub-agent stopped early ({stop_reason}) without an answer"
            if not task.tools:
                reason += "; it was given no tools and had to search for its own"
            entries.append(
                {
                    "task": task.instruction,
                    "status": "incomplete",
                    "response": "",
                    "error": reason,
                    "stop_reason": stop_reason,
                    "tools_used": _tools_used(raw),
                }
            )
            continue

        succeeded += 1
        success_row: Dict[str, Any] = {
            "task": task.instruction,
            "status": "success",
            "response": writeup,
            "tools_used": _tools_used(raw),
            "stop_reason": stop_reason,
            "thinking_text": _thinking_text(raw),
            "tool_summaries": _tool_summaries(raw),
            "cost_usd": _cost_usd(raw),
        }
        if index < len(child_cids) and child_cids[index]:
            success_row["child_conversation_id"] = child_cids[index]
        entries.append(success_row)

    pointers: List[Dict[str, Any]] = []
    if parent_cid:
        persisted_by_cid: Dict[str, Dict[str, Any]] = {
            str(pointer.get("child_conversation_id") or "").strip(): pointer
            for pointer in _persist_spawn_children(
                motet,
                str(parent_agent_id),
                tasks,
                entries,
                child_cids,
                brief_written=brief_written,
            )
        }
        from motet.core.conversations.children import child_pointer

        for index, entry in enumerate(entries):
            if entry.get("status") != "success":
                continue
            child_cid = str(entry.get("child_conversation_id") or "").strip()
            if not child_cid:
                continue
            persisted = persisted_by_cid.get(child_cid)
            if persisted:
                pointers.append(persisted)
                continue
            # Persist failed for this child (fail-soft). Synthesize the card
            # anyway so the parent turn matches the registered sidebar row.
            instruction = ""
            if index < len(tasks):
                instruction = str(getattr(tasks[index], "instruction", "") or "").strip()
            pointers.append(
                child_pointer(
                    child_cid=child_cid,
                    agent_id=spawn_child_id(str(parent_agent_id), index),
                    title=instruction or str(entry.get("task") or ""),
                    preview=str(entry.get("response") or ""),
                    cost_usd=entry.get("cost_usd"),
                    thinking_text=str(entry.get("thinking_text") or "") or None,
                    tool_summaries=entry.get("tool_summaries") or None,
                )
            )

    emit_reasoning_event(
        motet,
        strategy="agentic_loop",
        step=2,
        thought="Parallel sub-agents complete",
        action="spawn_agents_complete",
        observation=(
            f"{succeeded}/{len(tasks)} answered"
            + (f", {incomplete} out of budget" if incomplete else "")
        ),
        stream_key=stream_key,
    )

    if succeeded == 0:
        # No branch produced an answer. Reporting that as a successful tool call
        # with an all-empty payload invites the model to summarize the failures
        # as findings, or to silently redo the whole fan-out itself.
        logger.error(
            "spawn_agents_all_failed",
            task_count=len(tasks),
            incomplete=incomplete,
            task_id=getattr(motet, "task_id", None),
        )
        if incomplete == len(tasks):
            # Budget, not breakage. Say so, or the model retries the same shape
            # and buys the same nothing.
            hint = (
                "Name the tools each task needs so the sub-agents do not spend "
                "their budget searching for them"
                if undeclared
                else "Narrow each task so it needs fewer steps"
            )
            return err(
                f"All {len(tasks)} parallel sub-agents ran out of budget before "
                f"answering. {hint}, or do the work directly in this turn "
                "instead of fanning out."
            )
        first_error = next(
            (e["error"] for e in entries if e.get("error")), "sub-agents failed"
        )
        return err(
            f"All {len(tasks)} parallel sub-agents failed. First error: {first_error}"
        )

    logger.info(
        "spawn_agents_complete",
        task_count=len(tasks),
        succeeded=succeeded,
        incomplete=incomplete,
        task_id=getattr(motet, "task_id", None),
    )

    artifact_id = _store_full_fanin(motet, entries)
    snapshot_cache, snapshot_signatures = _collect_snapshot_state(results)
    payload: Dict[str, Any] = {"results": entries}
    if artifact_id:
        payload["artifact_id"] = artifact_id

    envelope_meta: Dict[str, Any] = {
        "task_count": len(tasks),
        "succeeded": succeeded,
        "incomplete": incomplete,
        "snapshot_cache": snapshot_cache,
        "snapshot_signatures": snapshot_signatures,
    }
    if pointers:
        envelope_meta["spawn_children"] = pointers
    envelope = ok(
        payload,
        meta=envelope_meta,
    )
    # Loop extract reads ``text``; without it the parent sees a JSON dump
    # of the rows and treats a long fan-in as something it must re-fetch.
    envelope["text"] = _format_observation(envelope)
    return envelope


def register(registry: ToolRegistry) -> None:
    """Register the parallel sub-agent fan-out tool."""
    registry.register(
        name=TOOL_NAME,
        func=run_spawn_agents,
        description=(
            "Use this instead of running the same kind of work several times "
            "in a row. Run independent pieces of work at the same time, one "
            "sub-agent each, and get all their results back together. Use when "
            "a request splits into parts that do not depend on each other. "
            "Each task needs a self-contained instruction plus the names of "
            "the tools it requires; sub-agents cannot see the conversation or "
            "each other, and one left to find its own tools will usually run "
            "out of budget first. Set discover=true on a task only when that "
            "slice may need tools you cannot name. Do not use for steps that "
            "must happen in order."
        ),
        category="orchestration",
        priority=5,
        data_types=["parallel", "research", "comparison"],
        keywords=[
            "parallel",
            "in parallel",
            "simultaneously",
            "at the same time",
            "at once",
            "all at once",
            "fan out",
            "compare",
            "alternatives",
            "options",
            "research",
            "investigate",
        ],
        tool_schema=SpawnAgentsParams,
        observation_formatter=_format_observation,
        contextualize_observation=False,
    )


__all__ = [
    "INCOMPLETE_STOP_REASONS",
    "MAX_FANOUT_WIDTH",
    "SpawnAgentsParams",
    "SpawnTask",
    "register",
    "resolve_child_tool_schemas",
    "run_spawn_agents",
    "spawn_child_id",
]
