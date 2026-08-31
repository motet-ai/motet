"""
Motet - Agent Configuration Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-31

Description:
    Agent configuration registry. Lookup from fully-qualified agent_id
    (e.g. core.default, core.motet_admin, core.subagent) to AgentConfig used
    when invoking the agent command. Provides ToolFilter, TurnHooks (including
    after_finalize), AgentConfig, and AgentConfigRegistry with in-code
    registration for built-in agents. Bare chat names are opt-in via
    AgentConfig.aliases only (issue #186); agent_id alone is never
    auto-registered as a global alias.

Dependencies:
    - pydantic: AgentConfig, ToolFilter, TurnHooks models
    - motet.core.tools.registry: ToolRegistry for resolve_tools
    - motet.core.tools.schema_exporter: ToolSchemaExporter for canonical schemas

Usage:
    from motet.core.agents import get_agent_registry, AgentConfig

    registry = get_agent_registry()
    config = registry.get("core.motet_admin")
    tools = resolve_tools(config.tool_filter, tool_registry, schema_exporter)

Notes:
    - Built-in agents (core.default, core.motet_admin, core.subagent) registered at import.
    - ``core.subagent`` is ``builtin_subagent_config()``: rails on the AgentConfig,
      briefs formatted from those fields. Spawn reads the live registry entry.
    - ``selectable=False`` agents are callable but omitted from new-chat pickers.
    - Use register_agent(AgentConfig(...)) for normal registration; base register(key, item,...) requires key == qualified id.
    - Canonical address is always ``{bundle_id}.{agent_id}`` (or ``core.{agent_id}``).
    - Explicit ``aliases`` are optional global shortcuts; colliding claims fail fast.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Set, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

import structlog

from motet.core.types import OutputContract, ReasoningEffort
from motet.core.registry import (
    RegistryScope,
    ScopeGrant,
    ScopedRegistry,
    normalize_namespace,
    namespace_to_bundle_id,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# ToolFilter (ADR-0078 §1, ADR-0093 enhancements)
# ---------------------------------------------------------------------------


def _to_list(v: Optional[Union[str, List[str]]]) -> List[str]:
    """Normalize str or List[str] to List[str]."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [x for x in v if isinstance(x, str) and x.strip()]


def get_discovery_filter_metadata(tool_filter: Optional["ToolFilter"]) -> Optional[Dict[str, Any]]:
    """
    Extract filter metadata for discovery mode (ADR-0093).
    Used when tools=None: agentic loop builds the meta-disclosure shortlist
    and applies these filters / required_tools.
    Returns None if tool_filter is None or mode != discovery.
    """
    if not tool_filter or tool_filter.mode != "discovery":
        return None
    return {
        "exclude_tools": list(tool_filter.exclude_tools or []),
        "exclude_workflows": tool_filter.exclude_workflows,
        "no_workflows": bool(tool_filter.no_workflows),
        "required_tools": list(tool_filter.required_tools or []),
        "required_workflows": list(tool_filter.required_workflows or []),
        "prefix": _to_list(tool_filter.prefix),
        "category": _to_list(tool_filter.category),
    }


class ToolFilter(BaseModel):
    """
    Determines which tools are available to the agent.

    Mode-specific selection:
    - explicit: required_tools + required_workflows define the complete set (both may be empty).
    - prefix: tools whose name starts with any of the prefixes.
    - category: tools whose registry category is in the list.
    - discovery: meta-tool progressive disclosure — frozen shortlist
      (help + tools_search + tool_call + pins / required_tools); catalog via
      tools_search → tool_call. Filter metadata is passed through.

    Cross-mode filters (apply after selection in all modes):
    - exclude_tools, exclude_workflows: remove from result.
    - no_workflows: when True, exclude all workflows (convenience for nested agents).
    - prefix, category: filter (narrow) result when used outside their primary mode.
    - required_tools, required_workflows: add to result (in non-explicit modes).
    """

    mode: Literal["explicit", "prefix", "category", "discovery"] = Field(
        default="discovery",
        description=(
            "How tools are resolved: explicit (required only), prefix, category, "
            "or discovery (meta-tool progressive disclosure)."
        ),
    )
    # Explicit mode: complete set. Other modes: additive.
    required_tools: Optional[List[str]] = Field(
        default=None,
        description="Tool names to include. In explicit mode: the only tools. Else: added to selection.",
    )
    required_workflows: Optional[List[str]] = Field(
        default=None,
        description="Workflow IDs to include (e.g. expert-panel.discuss). Same semantics as required_tools.",
    )
    # Cross-mode: remove from result
    exclude_tools: Optional[List[str]] = Field(
        default=None,
        description="Tool/workflow names to exclude (as LLM sees them, e.g. workflow_expert-panel.discuss).",
    )
    exclude_workflows: Optional[List[str]] = Field(
        default=None,
        description="Workflow IDs to exclude (e.g. expert-panel.discuss). Maps to workflow_<id> when filtering.",
    )
    no_workflows: bool = Field(
        default=False,
        description="When True, exclude all workflows from the result. Convenience for nested agents that must not re-invoke workflows.",
    )
    # Cross-mode: filter (narrow) result. In prefix/category modes, primary selector.
    prefix: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Filter to tools whose name starts with any of these prefixes.",
    )
    category: Optional[Union[str, List[str]]] = Field(
        default=None,
        description="Filter to tools whose registry category is in this list.",
    )
    # Deprecated: use required_tools instead (ADR-0093)
    explicit_tools: Optional[List[str]] = Field(
        default=None,
        description="Deprecated. Use required_tools. Kept for migration.",
    )


# ---------------------------------------------------------------------------
# TurnHooks (ADR-0078 §1)
# ---------------------------------------------------------------------------


class TurnHooks(BaseModel):
    """
    Commands that run around the agent's core reasoning loop.

    Each single-command slot is a registered command name or None (skip).
    List slots are additive. Names are looked up in the command registry
    at turn time; an unknown name warns and skips, except finalize, which
    falls back to core.finalize_turn so a typo cannot drop the transcript.
    Hook names are also checked when the agent is loaded. Unknown field
    names (for example a renamed slot) fail at parse so they cannot skip
    a phase silently.
    """

    model_config = ConfigDict(extra="forbid")

    context_inject: Optional[List[str]] = Field(
        default=None,
        description=(
            "Additive commands run after analysis to inject system messages "
            "and an optional context patch. Each command receives TurnContextHookData."
        ),
    )
    after_finalize: Optional[List[str]] = Field(
        default=None,
        description=(
            "Commands run after finalize on a completed turn (fail-soft). "
            "Use for optional export/observability. Does not replace core.finalize_turn. "
            "Each command receives TurnAfterFinalizeData."
        ),
    )
    conversation_analysis: Optional[str] = Field(
        default=None,
        description=(
            "Observation-only analysis command before reasoning. "
            "None skips. Motet default value: core.conversation_analysis."
        ),
    )
    context_prepare: Optional[str] = Field(
        default=None,
        description=(
            "Command that loads context into the turn before reasoning. "
            "None skips. Motet default value: core.prepare_context."
        ),
    )
    memory_reset: Optional[str] = Field(
        default=None,
        description=(
            "Command to reset working memory before preparing context. "
            "None skips. Motet default value: core.memory_reset."
        ),
    )
    finalize: Optional[str] = Field(
        default=None,
        description=(
            "Turn commit step: persist the transcript (and update memory). "
            "None skips. Motet default value: core.finalize_turn. "
            "An unregistered name falls back to core.finalize_turn."
        ),
    )

# ---------------------------------------------------------------------------
# AgentConfig (ADR-0078 §1)
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """Complete configuration for a named agent."""

    agent_id: str = Field(
        ...,
        description="Bare agent name (e.g. 'default', 'motet_admin'). Registry namespaces using bundle_id.",
    )
    display_name: str = Field(default="", description="Human-readable name for display in UI.")
    description: str = Field(default="", description="Short description of what this agent does.")
    allowed_roles: List[str] = Field(
        default=["*"],
        description="Roles allowed to invoke this agent. '*' = any authenticated principal.",
    )
    selectable: bool = Field(
        default=True,
        description=(
            "When true, chat UIs may offer this agent as a new-conversation "
            "picker option. When false the agent is still callable (follow-up, "
            "tools) but is not a start-a-chat choice."
        ),
    )
    system_prompt: str = Field(
        ...,
        description="System prompt defining the agent's identity, behavior, and constraints.",
    )
    tool_filter: ToolFilter = Field(
        default_factory=lambda: ToolFilter(mode="discovery"),
        description="How tools are selected for this agent.",
    )
    turn_hooks: TurnHooks = Field(
        default_factory=TurnHooks,
        description="Command names for orchestration phases around each turn. None = skip.",
    )
    model_provider: Optional[str] = Field(default=None, description="LLM provider override. None = stack default.")
    model_name: Optional[str] = Field(default=None, description="Model name override. None = stack default.")
    model_profile_name: Optional[str] = Field(default=None, description="Model profile for routing.")
    temperature: float = Field(default=0.2, description="Sampling temperature.", ge=0.0, le=2.0)
    max_iterations: int = Field(
        default=20,
        description="Maximum Motet-tool recursion iterations in the agentic loop.",
        ge=1,
    )
    max_model_calls: Optional[int] = Field(
        default=None,
        description=(
            "Hard cap on model inference calls per turn (safety rail for client "
            "handback loops). None defaults to max(max_iterations * 3, 30)."
        ),
        ge=1,
    )
    max_cost_usd: Optional[float] = Field(
        default=None,
        description=(
            "Stop the turn when accumulated model cost reaches this many USD. "
            "None inherits MOTET_AGENT_MAX_COST_USD (0.75). 0 disables."
        ),
        ge=0.0,
    )
    max_prompt_tokens: Optional[int] = Field(
        default=None,
        description=(
            "Stop the turn when accumulated prompt tokens reach this count. "
            "None inherits MOTET_AGENT_MAX_PROMPT_TOKENS (200000). 0 disables."
        ),
        ge=0,
    )
    max_tools: int = Field(default=20, description="Maximum tools per iteration.", ge=1)
    enable_thinking: bool = Field(
        default=False,
        description="Enable extended thinking for capable models.",
    )
    reasoning_effort: Optional[ReasoningEffort] = Field(
        default="medium",
        description=(
            "Reasoning effort when enable_thinking is True. Adapters clamp to what each "
            "provider supports (e.g. OpenAI 'max' only on gpt-5.6 Responses; xAI has no "
            "'max'). Use 'max' for Kimi K3."
        ),
    )
    conversation_id_prefix: Optional[str] = Field(
        default=None,
        description="Prefix for auto-generated conversation IDs. E.g. 'admin:' or 'deploy:'.",
    )
    bundle_id: Optional[str] = Field(
        default=None,
        description="Bundle that deployed this agent. None = core (built-in). Set by deploy pipeline.",
    )
    aliases: List[str] = Field(
        default_factory=list,
        description=(
            "Optional bare global shortcuts (e.g. 'planner', 'default') that resolve to this "
            "agent's qualified ID. Bare agent_id is not registered as an alias unless listed here."
        ),
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Opaque metadata passed through to LoopContext.metadata.",
    )
    allowed_surface_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Surfaces this agent may use (catalog ids). "
            "None or empty means all catalog surfaces. Manage-UI Redis overlays override this."
        ),
    )
    skill_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Explicit allowlist of canonical skill ids (e.g. bundle.my_skill). "
            "Cataloged directly in allowlist mode; optional prefilter in discovery mode."
        ),
    )
    skill_mode: Literal["allowlist", "discovery"] = Field(
        default="allowlist",
        description=(
            "Skill selection mode. "
            "'allowlist' discloses only skill_ids; 'discovery' discloses visible skill metadata "
            "for model-driven activation via core.activate_skill."
        ),
    )
    skill_max_per_turn: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of explicit user-requested skills activated by the harness in a single turn.",
    )
    output_contract: Optional[OutputContract] = Field(
        default=None,
        description=(
            "Structured-output contract for this agent's turns. Constrains one "
            "finalize model call after the loop stops. Per-call AgentTurnData "
            "output_contract wins when set. None keeps free text."
        ),
    )
    handoffs: List[str] = Field(
        default_factory=list,
        description=(
            "Qualified agent ids this agent may delegate to (e.g. bundle.agent). "
            "core.handoff is in the tool catalog; this list is the grant. "
            "The schema is also pinned on this agent when the list is non-empty."
        ),
    )

    @field_validator("output_contract", mode="before")
    @classmethod
    def _coerce_output_contract(cls, v: Any) -> Optional[OutputContract]:
        if v is None:
            return None
        if isinstance(v, OutputContract):
            return v
        if isinstance(v, dict):
            return OutputContract(**v)
        raise ValueError(f"output_contract must be OutputContract or dict, got {type(v).__name__}")

    @field_validator("handoffs", mode="before")
    @classmethod
    def _coerce_handoffs(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
        return [str(item).strip() for item in v if str(item).strip()]


# ---------------------------------------------------------------------------
# AgentConfigRegistry (ADR-0078 §2)
# ---------------------------------------------------------------------------


def _qualified_id(bundle_id: Optional[str], agent_id: str) -> str:
    """Compute fully-qualified agent ID from bundle_id and bare agent_id."""
    namespace = normalize_namespace(bundle_id)
    return f"{namespace}.{agent_id}"


def _scope_for_agent_config(config: AgentConfig) -> RegistryScope:
    """Build registry scope metadata for an agent config."""
    namespace = normalize_namespace(config.bundle_id)
    grants: List[ScopeGrant] = []
    roles = [role.strip() for role in (config.allowed_roles or ["*"]) if role and role.strip()]
    if not roles or "*" in roles:
        grants = [ScopeGrant()]
    else:
        grants = [ScopeGrant(role=role) for role in sorted(set(roles))]
    return RegistryScope(
        namespace=namespace,
        bundle_id=namespace_to_bundle_id(namespace),
        grants=grants,
    )


class AgentConfigRegistry(ScopedRegistry[AgentConfig]):
    """
    Registry of named agent configurations.

    Agents are stored under fully-qualified keys: '{namespace}.{agent_id}'.
    bundle_id=None → 'core.{agent_id}'; bundle_id='sales' → 'sales.{agent_id}'.
    All lookups use the fully-qualified key.
    """

    def __init__(self) -> None:
        super().__init__(registry_name="agent_config_registry")
        self._aliases: Dict[str, str] = {}
        self._aliases_by_qid: Dict[str, Set[str]] = {}

    def get(self, key: str) -> Optional[AgentConfig]:
        """Return config for a fully-qualified agent ID (e.g. 'core.default'), or None."""
        return super().get(key)

    def list(self) -> List[AgentConfig]:
        """Return all registered agent configurations."""
        return list(super().list_items().values())

    def register(
        self,
        key: str,
        item: AgentConfig,
        *,
        scope: Optional[RegistryScope] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register or replace an agent. ``key`` must equal ``_qualified_id(item.bundle_id, item.agent_id)``."""
        qid = _qualified_id(item.bundle_id, item.agent_id)
        if key != qid:
            raise ValueError(
                f"register key {key!r} must equal qualified id {qid!r} derived from agent_id and bundle_id"
            )
        config = item
        # Remove previously-registered aliases for this config key (if any).
        for old_alias in self._aliases_by_qid.get(qid, set()):
            if self._aliases.get(old_alias) == qid:
                self._aliases.pop(old_alias, None)

        # Opt-in bare shortcuts only (#186). Do not auto-claim config.agent_id globally.
        alias_set: Set[str] = {
            a.strip() for a in (config.aliases or []) if a and a.strip()
        }

        for alias in alias_set:
            if "." in alias:
                raise ValueError(
                    f"Invalid alias '{alias}' for {qid}: aliases must be bare names without dots"
                )
            existing = self._aliases.get(alias)
            if existing and existing != qid:
                raise ValueError(
                    f"Alias collision: bare alias '{alias}' is already claimed by "
                    f"'{existing}'; '{qid}' cannot register it. Use the qualified ID "
                    f"'{qid}', pick a different aliases entry, or unload the owner."
                )

        super().register(
            qid,
            config,
            scope=scope if scope is not None else _scope_for_agent_config(config),
            metadata=metadata,
        )
        for alias in alias_set:
            self._aliases[alias] = qid
        self._aliases_by_qid[qid] = alias_set

        logger.debug(
            "agent_config_registered",
            qualified_id=qid,
            agent_id=config.agent_id,
            bundle_id=config.bundle_id,
            aliases=sorted(alias_set),
        )

    def register_agent(self, config: AgentConfig) -> None:
        """Register an agent configuration (computes qualified key from ``bundle_id`` and ``agent_id``)."""
        self.register(_qualified_id(config.bundle_id, config.agent_id), config)

    def unregister(self, key: str) -> bool:
        """Unregister a fully-qualified agent config and its aliases. Returns True when removed."""
        existing = super().get(key)
        if existing is None:
            return False

        super().unregister(key)
        for alias in self._aliases_by_qid.pop(key, set()):
            if self._aliases.get(alias) == key:
                self._aliases.pop(alias, None)

        logger.debug(
            "agent_config_unregistered",
            qualified_id=key,
            agent_id=existing.agent_id,
            bundle_id=existing.bundle_id,
        )
        return True

    def unregister_bundle(self, bundle_id: str) -> List[str]:
        """Unregister all agents in a bundle namespace and return removed qualified IDs."""
        to_remove = super().unregister_namespace(bundle_id)
        for qid in to_remove:
            for alias in self._aliases_by_qid.pop(qid, set()):
                if self._aliases.get(alias) == qid:
                    self._aliases.pop(alias, None)
        return to_remove

    def resolve_id(self, raw_id: Optional[str]) -> str:
        """
        Resolve a request-supplied ID to a fully-qualified agent key when possible.

        Resolution order:
        1) Empty -> core.default
        2) Already qualified (contains ".") -> unchanged
        3) Explicit alias map hit -> qualified key
        4) Bare-name core fallback (if registered) -> core.{raw}
        5) Unchanged raw (caller can 404 on missing registry entry)

        Bare ``agent_id`` values are not aliases unless listed in ``AgentConfig.aliases``.
        Prefer qualified IDs (``{bundle_id}.{agent_id}``) for bundle agents.
        """
        raw = (raw_id or "").strip()
        if not raw:
            return "core.default"
        if "." in raw:
            return raw
        if raw in self._aliases:
            return self._aliases[raw]

        core_fallback = f"core.{raw}"
        if super().get(core_fallback) is not None:
            return core_fallback
        return raw


# ---------------------------------------------------------------------------
# Built-in agent configs (ADR-0078 §3)
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. Answer questions accurately and concisely. "
    "When you use tools, explain what you did and cite the results. "
    "If you are unsure, say so rather than guessing.\n"
    "\n"
    # ADR-0138 decision 9: this replaces the pre-turn clarification classifier.
    # Underspecification is a property of a request relative to available
    # capability, not of the sentence — only the model holding the tool schemas
    # can tell that "send the email" is missing a recipient.
    "Missing information: if a fact you need to act is absent, ask for exactly "
    "those items and do not start tool calls or guess a value. Ask once, "
    "listing what you need together. If the request is answerable as asked, "
    "answer it — do not interrogate the user for detail you do not need.\n"
    "\n"
    "Tool discovery: this turn's tool list is intentionally small. Motet hosts a "
    "live catalog of server-side capabilities (first-party tools, MCP integrations, "
    "workflows, memory, and more). Use core.tools_search to find a tool and get its "
    "JSON schema in the observation, then core.tool_call with that canonical name "
    "and matching parameters. When calling core.tools_search, pass a short intent "
    "phrase describing the task (e.g. 'navigate to a website and take a "
    "screenshot'), not a "
    "keyword list. Prefer a direct call when the tool is already listed. "
    "core.help is the internal operations router when you are unsure where to start. "
    "Never invent tool names."
)

MOTET_ADMIN_SYSTEM_PROMPT = (
    "You are the Motet Admin Assistant — an operational assistant for the Motet platform. "
    "You help operators understand and manage workers, schedules, deployments, vault, costs, and conversations. "
    "Read first, act second: default to explaining and diagnosing. Only take actions (rollback, propagate, pause, etc.) "
    "when the operator explicitly asks. When taking action, explain what you will do and any risks before invoking a tool. "
    "For destructive actions (rollback, undeploy, pause), confirm with the operator before proceeding. "
    "Never speculate about secret values; vault tools return metadata only. "
    "Cite the tool results you use when answering. If data is unavailable, say so."
)

CORE_SUBAGENT_ID = "core.subagent"


def _subagent_brief_from_config(cfg: "AgentConfig", *, discover: bool) -> str:
    """Worker brief whose rail numbers come from the same AgentConfig."""
    meta = cfg.metadata if isinstance(cfg.metadata, dict) else {}
    try:
        tool_time_ms = int(meta.get("max_tool_time_ms") or 0)
    except (TypeError, ValueError):
        tool_time_ms = 0
    seconds = max(1, tool_time_ms // 1000)
    catalog = (
        "You may search the catalog for tools this slice needs. Prefer any tools "
        "already listed for this turn before searching."
        if discover
        else "Use the tools already listed for this turn. Do not search the catalog "
        "for more."
    )
    return (
        "You are a focused subagent working one task. Stay on that task unless "
        "the user redirects you.\n"
        f"{catalog}\n"
        f"You have at most {cfg.max_iterations} tool rounds, "
        f"{cfg.max_tools} tool calls, and "
        f"{seconds} seconds of tool time. "
        "A partial answer with sources beats one more fetch."
    )


def builtin_subagent_config() -> AgentConfig:
    """Shipped ``core.subagent``. Register and spawn both use this object.

    Rails live on the AgentConfig. Briefs are formatted from those fields so
    a rail edit updates the first-turn / follow-up prompt in the same place.
    """
    cfg = AgentConfig(
        agent_id="subagent",
        display_name="Subagent",
        description=(
            "Parallel spawn worker. Used for core.spawn_agents children "
            "and follow-up on those conversations. Not a new-chat picker "
            "option."
        ),
        allowed_roles=["*"],
        selectable=False,
        system_prompt="You are a focused subagent working one task.",
        tool_filter=ToolFilter(
            mode="discovery",
            required_tools=[
                "core.help",
                "core.tools_search",
                "core.tool_call",
            ],
            exclude_tools=["core.spawn_agents"],
        ),
        turn_hooks=TurnHooks(
            conversation_analysis=None,
            memory_reset=None,
            context_prepare="core.prepare_context",
            finalize="core.finalize_turn",
        ),
        max_iterations=10,
        max_tools=8,
        max_cost_usd=0.20,
        max_prompt_tokens=80_000,
        metadata={"max_tool_time_ms": 60_000},
        bundle_id=None,
    )
    return cfg.model_copy(
        update={
            "system_prompt": _subagent_brief_from_config(cfg, discover=False),
            "metadata": {
                **dict(cfg.metadata or {}),
                "discovery_system_prompt": _subagent_brief_from_config(
                    cfg, discover=True
                ),
            },
        }
    )


def _register_builtin_agents(registry: AgentConfigRegistry) -> None:
    """Register core.default, core.motet_admin, and core.subagent at module load."""
    registry.register_agent(
        AgentConfig(
            agent_id="default",
            aliases=["agent", "default"],
            display_name="Motet Agent",
            description=(
                "General-purpose agent. Progressive disclosure: a small frozen "
                "shortlist (help + tools_search + tool_call) with the rest of the "
                "catalog reached via search → core.tool_call."
            ),
            allowed_roles=["*"],
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            tool_filter=ToolFilter(
                mode="discovery",
                # ADR-0128: pin the meta-disclosure trio into the frozen shortlist.
                required_tools=[
                    "core.help",
                    "core.tools_search",
                    "core.tool_call",
                ],
            ),
            turn_hooks=TurnHooks(
                conversation_analysis="core.conversation_analysis",
                memory_reset="core.memory_reset",
                context_prepare="core.prepare_context",
                finalize="core.finalize_turn",
            ),
            skill_mode="discovery",
            skill_max_per_turn=3,
            # Always-sticky meta tools (3) + largest keyword pin group (4).
            max_tools=8,
            bundle_id=None,
        )
    )
    registry.register_agent(
        AgentConfig(
            agent_id="motet_admin",
            aliases=["motet_admin"],
            display_name="Admin Assistant",
            description="Operational assistant for Motet administration.",
            allowed_roles=["admin", "operator", "motet-admin", "motet_admin"],
            system_prompt=MOTET_ADMIN_SYSTEM_PROMPT,
            tool_filter=ToolFilter(mode="prefix", prefix="motet_admin."),
            turn_hooks=TurnHooks(
                context_inject=["core.page_context"],
                conversation_analysis=None,
                context_prepare=None,
                finalize="core.finalize_turn",
            ),
            temperature=0.2,
            max_iterations=20,
            conversation_id_prefix="admin:",
            bundle_id=None,
        )
    )
    registry.register_agent(builtin_subagent_config())


# ---------------------------------------------------------------------------
# Singleton registry and resolve_tools
# ---------------------------------------------------------------------------

_agent_registry: Optional[AgentConfigRegistry] = None


def get_agent_registry() -> AgentConfigRegistry:
    """Return the global agent config registry (singleton). Built-in agents are registered on first access."""
    global _agent_registry
    if _agent_registry is None:
        _agent_registry = AgentConfigRegistry()
        _register_builtin_agents(_agent_registry)
    return _agent_registry


# ADR-0078 §4, ADR-0093: Tool resolution at invocation time
def resolve_tools(
    tool_filter: ToolFilter,
    tool_registry: Any,
    schema_exporter: Any,
    *,
    max_tools: Optional[int] = None,
) -> Optional[List[Any]]:
    """
    Resolve ToolFilter to concrete tool schemas (or None for discovery mode).

    - discovery: return None (agentic loop runs semantic discovery with filter metadata).
    - prefix: registry names starting with any prefix → export, then apply filters.
    - category: registry tools with matching category → export, then apply filters.
    - explicit: required_tools + required_workflows (with explicit_tools fallback) → export, then apply filters.

    Returns list of CanonicalToolSchema, or None to indicate discovery mode.
    """
    if tool_filter.mode == "discovery":
        return None

    # Migration: explicit_tools → required_tools (ADR-0093)
    req_tools = list(tool_filter.required_tools or [])
    if not req_tools and tool_filter.explicit_tools:
        req_tools = list(tool_filter.explicit_tools)
    req_workflows = list(tool_filter.required_workflows or [])
    prefix_list = _to_list(tool_filter.prefix)
    category_list = _to_list(tool_filter.category)
    exclude_tools_list = list(tool_filter.exclude_tools or [])
    exclude_workflows = tool_filter.exclude_workflows
    no_workflows = bool(tool_filter.no_workflows)

    all_tools = tool_registry.list_items()

    # 1. Select by mode
    tool_names: List[str] = []
    if tool_filter.mode == "prefix" and prefix_list:
        for p in prefix_list:
            tool_names.extend(n for n in all_tools if n.startswith(p))
        tool_names = sorted(set(tool_names))
    elif tool_filter.mode == "category" and category_list:
        cats = set(category_list)
        tool_names = sorted(
            n for n, t in all_tools.items()
            if getattr(t, "category", "general") in cats
        )
    elif tool_filter.mode == "explicit":
        for n in req_tools:
            if n in all_tools:
                tool_names.append(n)
        for wf_id in req_workflows:
            name = f"workflow_{wf_id}"
            tool_names.append(name)
        if req_tools:
            missing = [n for n in req_tools if n not in all_tools]
            if missing:
                logger.warning(
                    "resolve_tools_required_missing",
                    missing=missing,
                    note="Tools not in registry; may be registered on workers.",
                )

    # 2. Filter by prefix (when not primary selector)
    if prefix_list and tool_filter.mode != "prefix":
        tool_names = [n for n in tool_names if any(n.startswith(p) for p in prefix_list)]

    # 3. Filter by category (when not primary selector)
    if category_list and tool_filter.mode != "category":
        cats = set(category_list)
        filtered: List[str] = []
        for n in tool_names:
            if n.startswith("workflow_"):
                filtered.append(n)
            elif n in all_tools:
                t = all_tools.get(n)
                if t and getattr(t, "category", "general") in cats:
                    filtered.append(n)
        tool_names = filtered

    # 4. Exclude
    exclude_set = set(exclude_tools_list or [])
    if no_workflows:
        exclude_set.update(n for n in tool_names if n.startswith("workflow_"))
    for wf_id in exclude_workflows or []:
        exclude_set.add(f"workflow_{wf_id}")
    tool_names = [n for n in tool_names if n not in exclude_set]

    # 5. Add required (for non-explicit modes)
    if tool_filter.mode != "explicit":
        for n in req_tools:
            if n not in tool_names and n in all_tools:
                tool_names.append(n)
        for wf_id in req_workflows:
            name = f"workflow_{wf_id}"
            if name not in tool_names:
                tool_names.append(name)

    tool_names = list(dict.fromkeys(tool_names))

    if not tool_names:
        return []

    # Split: tools come from tool registry, workflows from WorkflowRegistry
    tools_only = [n for n in tool_names if not n.startswith("workflow_")]
    workflow_ids = [
        n.replace("workflow_", "", 1) for n in tool_names if n.startswith("workflow_")
    ]

    schemas: List[Any] = []
    if workflow_ids:
        from motet.core.workflow import WorkflowRegistry

        all_wf = WorkflowRegistry.export_canonical_schemas() or []
        wf_names = {f"workflow_{wid}" for wid in workflow_ids}
        schemas.extend(s for s in all_wf if getattr(s, "name", "") in wf_names)
    if tools_only:
        tool_schemas = schema_exporter.export_canonical(
            preselected_tools=tools_only,
            max_tools=max_tools,
        )
        schemas.extend(tool_schemas or [])

    return schemas


# ---------------------------------------------------------------------------
# Chat API helpers: aliases and conversation_id prefix
# ---------------------------------------------------------------------------

def resolve_agent_id(raw_id: Optional[str]) -> str:
    """
    Resolve raw agent_id from request to fully-qualified ID.
    Delegates to the registry alias index.
    """
    return get_agent_registry().resolve_id(raw_id)


def ensure_conversation_id_prefix(conversation_id: Optional[str], prefix: Optional[str]) -> str:
    """If prefix is set and conversation_id does not start with it, prepend prefix to a new UUID segment or return as-is."""
    if not prefix:
        return (conversation_id or "").strip() or ""
    cid = (conversation_id or "").strip()
    if cid.startswith(prefix):
        return cid
    if cid:
        return cid  # Caller may pass full ID; only auto-prefix when generating new
    import uuid
    return f"{prefix}{uuid.uuid4().hex}"
