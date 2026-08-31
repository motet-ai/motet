"""
Motet - Unified Workflow System

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-29

Description:
    Unified workflow management system.
    Provides command-based workflow definitions, execution, state management, and discovery.
    Fixed MCP tool naming: Uses canonical dot-separated format (mcp.server_id.tool_name) with proper tool names.
    Fixed Playwright tools: Cut over to Microsoft @playwright/mcp browser_* tools (navigate / snapshot / take_screenshot).

    Every workflow step is a distributed command execution.

    **Step ownership and suspension** (issue #149): tool-shaped steps may declare
    ``ownership: handback`` (client executes) vs default ``motet``; elicitation
    steps and ``requires_confirmation`` pause via WorkflowCheckpoint without
    inventing a second wire protocol. See workflow/checkpoint.py and
    workflow consumer section.

    **Bounded nesting** (issue #189): workflows may call other workflows (and
    themselves) as ``workflow_execution`` steps with a depth budget; each frame
    is a fresh DAG with its own ``workflow_run_id``.

    **Sequential foreach**: optional ``foreach`` / ``loop_var`` /
    ``max_loop_iterations`` on ``WorkflowStep`` run a command once per list item
    with overlay context (``{{item}}``, ``{{loop.index}}``, ``{{loop.previous}}``).

    **Repeat-until** (``until``): a break condition checked after each iteration
    against the iteration result bound to ``result``. Without ``foreach`` the step
    repeats up to ``max_loop_iterations`` times, so a gate can be retried until it
    passes; the step reports ``stopped_reason`` for dependents to gate on.

    **Conversation isolation** (``isolate_conversation``): when set, each step
    invocation (or each foreach iteration) runs under a new opaque child
    ``conversation_id`` with stored parent/root pointers so transcript history
    does not accumulate across chunks; parent Redis history is retained for
    audit/cost.

    **Discovery keywords**: optional ``keywords`` plus tokens from the workflow
    id, name, and step ``tool_name`` values are indexed for ``core.tools_search``
    so composed workflows (e.g. navigate_screenshot) rank as "browser" /
    "playwright" / "url", not only as ``workflow_navigate_screenshot``.

Dependencies:
    - pydantic: Data validation and serialization
    - datetime: Time and date handling
    - Distributed command system
    - Concurrency primitives

Usage:
    from motet.core.workflow import (
        Workflow, WorkflowStep, WorkflowRegistry, WorkflowExecutor
    )
    
    workflow = Workflow(
        workflow_id="my_workflow",
        name="My Workflow",
        steps={
            "step1": WorkflowStep(
                step_id="step1",
                command_type="tool_execution",
                command_data={"tool_name": "core.web_search", "parameters": {"query": "AI"}}
            )
        }
    )
    
    WorkflowRegistry.register(workflow)
    executor = WorkflowExecutor()
    result = executor.execute_workflow(workflow, motet)

Notes:
    - Every workflow step is a distributed command execution via motet.do()
    - Supports declarative parallelism via dependency analysis
    - Automatic context propagation (task_id, conversation_id, etc.)
    - WorkflowRegistry for predefined workflow templates
    - WorkflowExecutor for stateless workflow execution
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Set, Generator

# Note: TransformData and TransformOperation are now in commands/transform.py
# They are imported here for backward compatibility in test files
try:
    from motet.core.commands.builtin.transform import TransformData, TransformOperation, transform
except ImportError:
    # During initial import this may not be available yet
    TransformData = None
    TransformOperation = None
    transform = None


class WorkflowStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class WorkflowStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class WorkflowStep(BaseModel):
    """
    A workflow step is a command execution with execution control.
    
    Every step executes a distributed command via motet.do().
    """
    # Step Identity
    step_id: str
    name: str
    
    # Command to Execute
    command_type: str = ""  # Command function name (e.g., "tool_execution")
    command_data: Dict[str, Any] = Field(default_factory=dict)  # Command data as dict
    dependencies: List[str] = Field(default_factory=list)
    
    # Alternate field names still accepted (module_name / operation / parameters)
    module_name: str = ""
    operation: str = ""
    parameters: Dict[str, Any] = Field(default_factory=dict)
    
    # Execution Context
    execution_context: Optional[Dict[str, Any]] = Field(default=None)

    # Step ownership / suspension (issue #149). Ownership is who executes a tool;
    # suspend reasons (confirmation, elicitation, oauth) are orthogonal.
    ownership: Literal["motet", "handback"] = Field(
        default="motet",
        description="Who executes a tool-shaped step: motet (server) or handback (client).",
    )
    step_type: Literal["command", "elicitation"] = Field(
        default="command",
        description="command = distributed command step; elicitation = human input pause.",
    )
    requires_confirmation: bool = Field(
        default=False,
        description="When True on a Motet-owned tool step, pause for approve/reject before execute.",
    )
    elicitation_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON schema for elicitation answers (step_type=elicitation).",
    )
    elicitation_prompt: Optional[str] = Field(
        default=None,
        description="Prompt shown when pausing for elicitation.",
    )
    
    # Step-Level Control
    continue_on_failure: bool = False
    fallback_step_id: Optional[str] = None
    skip_condition: Optional[str] = None
    step_retry_attempts: int = 0
    step_retry_delay_seconds: float = 1.0

    # Sequential foreach
    foreach: Optional[str] = Field(
        default=None,
        description=(
            "Context path to a list (e.g. 'parse_plan.chunks'). When set, the step "
            "command runs once per item sequentially with overlay context."
        ),
    )
    loop_var: str = Field(
        default="item",
        description="Name bound to the current foreach item in template substitution.",
    )
    max_loop_iterations: int = Field(
        default=20,
        ge=1,
        description="Hard cap on foreach iterations; exceeding it fails the step.",
    )
    until: Optional[str] = Field(
        default=None,
        description=(
            "Break condition evaluated after each iteration, same operators as "
            "skip_condition, against the iteration result bound to 'result' "
            "(e.g. 'if_equals:result.passed:True'). Without foreach, the step "
            "repeats up to max_loop_iterations times until the condition holds."
        ),
    )
    isolate_conversation: bool = Field(
        default=False,
        description=(
            "When True, run this step (or each foreach iteration) under a child "
            "conversation_id derived from the parent. Keeps within-turn transcript "
            "history while preventing cross-chunk context blow-up; parent transcripts "
            "remain in Redis for audit/cost."
        ),
    )
    
    # Legacy
    timeout_seconds: int = 30
    retry_attempts: int = 0
    
    # Runtime State
    status: WorkflowStepStatus = WorkflowStepStatus.PENDING
    result: Optional[Any] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    execution_time_ms: Optional[float] = None
    retry_count: int = 0
    worker_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _accept_yaml_type_alias(cls, data: Any) -> Any:
        """Bundle YAML may use ``type: elicitation``; map to step_type."""
        if isinstance(data, dict) and "step_type" not in data and data.get("type"):
            data = dict(data)
            data["step_type"] = data.pop("type")
        return data

    @field_validator("ownership", mode="before")
    @classmethod
    def _normalize_ownership(cls, v: Any) -> str:
        if v is None or v == "":
            return "motet"
        return str(v).strip().lower()

    def is_tool_shaped(self) -> bool:
        """True when this step may legally declare ownership=handback."""
        cmd = (self.command_type or "").strip()
        return cmd in (
            "tool_execution",
            "core.tool_execution",
            "",  # legacy tool_name-only steps
        ) or bool(self.command_data.get("tool_name") or self.parameters.get("tool_name"))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "step_id": self.step_id,
            "name": self.name,
            "command_type": self.command_type,
            "command_data": self.command_data,
            "execution_context": self.execution_context,
            "ownership": self.ownership,
            "step_type": self.step_type,
            "requires_confirmation": self.requires_confirmation,
            "elicitation_schema": self.elicitation_schema,
            "elicitation_prompt": self.elicitation_prompt,
            "module_name": self.module_name,
            "operation": self.operation,
            "parameters": self.parameters,
            "dependencies": self.dependencies,
            "continue_on_failure": self.continue_on_failure,
            "fallback_step_id": self.fallback_step_id,
            "skip_condition": self.skip_condition,
            "step_retry_attempts": self.step_retry_attempts,
            "step_retry_delay_seconds": self.step_retry_delay_seconds,
            "foreach": self.foreach,
            "loop_var": self.loop_var,
            "max_loop_iterations": self.max_loop_iterations,
            "until": self.until,
            "isolate_conversation": self.isolate_conversation,
            "timeout_seconds": self.timeout_seconds,
            "retry_attempts": self.retry_attempts,
            "status": self.status.value,
            "error": self.error,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "execution_time_ms": self.execution_time_ms,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowStep":
        """Create a WorkflowStep from a serialized dict (incl. bundle YAML step format)."""
        step_status = data.get("status", WorkflowStepStatus.PENDING)
        if isinstance(step_status, str):
            step_status = WorkflowStepStatus(step_status)
        started_at = data.get("started_at")
        completed_at = data.get("completed_at")
        try:
            started_at_dt = datetime.fromisoformat(started_at) if started_at else None
        except Exception:
            started_at_dt = None
        try:
            completed_at_dt = datetime.fromisoformat(completed_at) if completed_at else None
        except Exception:
            completed_at_dt = None
        # command_type and command_data (bundle YAML uses "parameters" -> command_data)
        command_type = str(data.get("command_type", ""))
        raw_command_data = data.get("command_data")
        if raw_command_data is not None:
            command_data = dict(raw_command_data)
        else:
            command_data = dict(data.get("parameters") or {})
        name = str(data.get("name") or data.get("description") or "")
        step_type = data.get("step_type") or data.get("type") or "command"
        return cls(
            step_id=str(data.get("step_id")),
            name=name,
            command_type=command_type,
            command_data=command_data,
            dependencies=list(data.get("dependencies") or []),
            execution_context=data.get("execution_context"),
            ownership=str(data.get("ownership") or "motet"),
            step_type=str(step_type),
            requires_confirmation=bool(data.get("requires_confirmation", False)),
            elicitation_schema=data.get("elicitation_schema") or data.get("schema"),
            elicitation_prompt=data.get("elicitation_prompt") or data.get("prompt"),
            continue_on_failure=bool(data.get("continue_on_failure", False)),
            fallback_step_id=data.get("fallback_step_id"),
            skip_condition=data.get("skip_condition"),
            step_retry_attempts=int(data.get("step_retry_attempts", 0)),
            step_retry_delay_seconds=float(data.get("step_retry_delay_seconds", 1.0)),
            foreach=data.get("foreach"),
            loop_var=str(data.get("loop_var") or "item"),
            max_loop_iterations=int(data.get("max_loop_iterations", 20)),
            until=data.get("until"),
            isolate_conversation=bool(data.get("isolate_conversation", False)),
            module_name=str(data.get("module_name", "")),
            operation=str(data.get("operation", "")),
            parameters=dict(data.get("parameters") or {}),
            timeout_seconds=int(data.get("timeout_seconds", 30)),
            retry_attempts=int(data.get("retry_attempts", 0)),
            status=step_status,
            result=data.get("result"),
            error=data.get("error"),
            started_at=started_at_dt,
            completed_at=completed_at_dt,
            execution_time_ms=(
                float(_et_ms) if (_et_ms := data.get("execution_time_ms")) is not None else None
            ),
            retry_count=int(data.get("retry_count", 0)),
            worker_id=data.get("worker_id"),
        )


_DISCOVERY_KEYWORD_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "this",
        "that",
        "it",
        "as",
        "workflow",
        "core",
        "mcp",
        "tool",
        "tools",
        "step",
        "command",
    }
)


def workflow_discovery_keywords(workflow: Any) -> List[str]:
    """
    Keywords the discovery index and tools_search should match for a workflow.

    Combines author-supplied ``keywords`` with tokens from the workflow id,
    display name, and step ``tool_name`` values so a composed workflow like
    navigate_screenshot is findable as "browser" / "playwright" even when those
    words appear only on the nested MCP tools.
    """
    ordered: List[str] = []
    seen: Set[str] = set()

    def _add(raw: Any) -> None:
        if not isinstance(raw, str):
            return
        for token in raw.replace(".", " ").replace("_", " ").replace("-", " ").split():
            norm = token.strip().lower()
            if not norm or norm in _DISCOVERY_KEYWORD_STOP or norm in seen:
                continue
            if len(norm) <= 1:
                continue
            seen.add(norm)
            ordered.append(norm)

    for kw in getattr(workflow, "keywords", None) or []:
        _add(kw)
    _add(getattr(workflow, "workflow_id", "") or "")
    _add(getattr(workflow, "name", "") or "")
    steps = getattr(workflow, "steps", None) or {}
    step_iter = steps.values() if isinstance(steps, dict) else steps
    for step in step_iter:
        cmd_data = getattr(step, "command_data", None)
        if cmd_data is None and isinstance(step, dict):
            cmd_data = step.get("command_data")
        if isinstance(cmd_data, dict):
            _add(cmd_data.get("tool_name") or "")
    return ordered


class Workflow(BaseModel):
    """Unified workflow model - directed acyclic graph of command executions"""
    workflow_id: str
    name: str
    description: str = ""
    steps: Dict[str, WorkflowStep] = Field(default_factory=dict)
    execution_order: List[List[str]] = Field(default_factory=list)
    
    # LLM Function Calling - Input Parameter Declaration
    required_inputs: Optional[List[str]] = Field(
        default=None,
        description="Explicitly declare which parameters LLM must provide (if None, auto-detected)"
    )
    input_parameters: Optional[Dict[str, Dict[str, Any]]] = Field(
        default=None,
        description="Optional detailed JSON schemas for LLM-provided parameters"
    )

    keywords: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional discovery keywords (e.g. browser, playwright, url). Combined "
            "with tokens from the workflow id, name, and step tool names when "
            "indexing for core.tools_search."
        ),
    )

    # Use-for visibility: which contexts can use this workflow (tool discovery, facilitation, etc.)
    use_for: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of use cases: 'tool' = discoverable as tool and indexed for semantic search; "
            "'facilitation' = run by facilitator_turn only. Default None/empty is treated as ['tool']."
        ),
    )

    # Presentation hint: which field of the final step's output is the primary
    # content to surface to the LLM after workflow completion.  When set,
    # format_workflow_steps includes that field in full without truncation so
    # the LLM can present the actual workflow output directly.  If not set the
    # generic `result` key is used as a fallback.
    output_field: Optional[str] = Field(
        default=None,
        description=(
            "Name of the field in the final step's output that contains the primary presentable content "
            "(e.g. 'digest_markdown', 'summary', 'report').  When provided, the agentic loop surfaces "
            "this field in full so the LLM can present it directly without hallucinating missing content."
        ),
    )

    presentation: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional agentic-loop presentation hints. When user_facing=true and requires_llm=false, "
            "the loop may stream workflow output directly to the user without a post-workflow LLM pass. "
            "passthrough_field names the final-step field to stream (defaults to output_field). "
            "response_wrap may be 'json_fence' to wrap string/JSON output in markdown fences."
        ),
    )

    # Client tool schemas for non-agent entry (issue #149). Agent turns inject
    # schemas from AgenticLoopData.handback_tools instead.
    handback_tools: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="OpenAI-shaped tool schemas for ownership=handback steps (non-agent entry).",
    )

    # Opt-in progress checkpoints without a prior suspend (issue #149 durability).
    durable: bool = Field(
        default=False,
        description=(
            "When True, assign a workflow_run_id and persist progress after each "
            "level even if the run never suspends. Default False keeps short "
            "workflows cheap."
        ),
    )
    max_nesting_depth: Optional[int] = Field(
        default=None,
        description=(
            "Optional per-workflow nest depth cap for issue #189. When unset, "
            "MOTET_WORKFLOW_MAX_NESTING_DEPTH (default 5) applies."
        ),
    )

    # State management
    status: WorkflowStatus = WorkflowStatus.CREATED
    context: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    def model_post_init(self, __context):
        """Calculate execution order after initialization"""
        if self.steps and not self.execution_order:
            self.execution_order = self._calculate_execution_order()

    def get_use_for(self) -> List[str]:
        """Return effective use_for list; default is ['tool'] when unset or empty."""
        if self.use_for and len(self.use_for) > 0:
            return list(self.use_for)
        return ["tool"]

    def is_used_for_tool(self) -> bool:
        """True if this workflow should be exposed as a tool (indexed and in export_canonical_schemas)."""
        return "tool" in self.get_use_for()

    def discovery_keywords(self) -> List[str]:
        """Keywords used by function discovery and tools_search for this workflow."""
        return workflow_discovery_keywords(self)

    def add_step(self, step: WorkflowStep):
        """Add a step to the workflow"""
        self.steps[step.step_id] = step
        self.execution_order = self._calculate_execution_order()
    
    def _calculate_execution_order(self) -> List[List[str]]:
        """Calculate execution order based on dependencies using topological sort"""
        if not self.steps:
            return []
        
        # Initialize dependency tracking
        in_degree = {}
        graph = {}
        
        for step_id in self.steps:
            in_degree[step_id] = 0
            graph[step_id] = []
        
        # Build dependency graph
        for step_id, step in self.steps.items():
            for dep in step.dependencies:
                if dep in graph:
                    graph[dep].append(step_id)
                    in_degree[step_id] += 1
                else:
                    raise ValueError(f"Dependency '{dep}' not found for step '{step_id}'")
        
        # Topological sort to get execution levels
        execution_levels = []
        remaining = set(self.steps.keys())
        
        while remaining:
            # Find steps with no dependencies
            current_level = [step_id for step_id in remaining if in_degree[step_id] == 0]
            
            if not current_level:
                # Circular dependency detected
                raise ValueError(f"Circular dependency detected in workflow {self.workflow_id}")
            
            execution_levels.append(current_level)
            
            # Remove current level and update in_degrees
            for step_id in current_level:
                remaining.remove(step_id)
                for neighbor in graph[step_id]:
                    in_degree[neighbor] -= 1
        
        return execution_levels
    
    def get_workflow_inputs(self) -> Set[str]:
        """
        Get LLM-provided workflow inputs.
        
        Returns parameters the LLM should provide when calling this workflow.
        Uses explicit required_inputs if set, otherwise auto-detects.
        
        Auto-detection algorithm:
        1. Scan ONLY first-level steps (dependencies=[])
        2. Extract {param} placeholders from command_data
        3. Exclude step output references ({step_id.field})
        4. Include initial workflow.context keys (if not step IDs)
        
        Returns:
            Set of parameter names that LLM should provide
        """
        import re
        import json
        
        # Use explicit declaration if provided
        if self.required_inputs is not None:
            return set(self.required_inputs)
        
        # Auto-detect from first-level steps
        step_ids = set(self.steps.keys())
        inputs = set()
        
        # Pattern: matches {word.chars[0].more} placeholders
        param_pattern = re.compile(r'\{([\w\.]+)(?:\[[\d]+\])?\}')
        
        # Scan first-level steps only (no dependencies)
        for step_id, step in self.steps.items():
            if step.dependencies:
                continue  # Skip dependent steps
            
            data_str = json.dumps(step.command_data)
            for match in param_pattern.finditer(data_str):
                param = match.group(1)
                
                # Exclude step output references
                if '.' in param:
                    base = param.split('.')[0]
                    if base in step_ids:
                        continue  # This is {step_id.field}, not an input
                
                # Exclude step IDs themselves
                if param in step_ids:
                    continue
                
                # This is a workflow input
                inputs.add(param)
        
        # Include initial context keys (if not step outputs)
        for key in self.context.keys():
            if key not in step_ids:
                inputs.add(key)
        
        return inputs
    
    def get_ready_steps(self) -> List[str]:
        """Get steps that are ready to execute (dependencies completed)"""
        ready_steps = []
        
        for step_id, step in self.steps.items():
            if step.status != WorkflowStepStatus.PENDING:
                continue
            
            # Check if all dependencies are completed
            dependencies_met = all(
                self.steps[dep_id].status == WorkflowStepStatus.COMPLETED
                for dep_id in step.dependencies
                if dep_id in self.steps
            )
            
            if dependencies_met:
                ready_steps.append(step_id)
        
        return ready_steps
    
    def get_step_results(self, step_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Get results from specified steps or all completed steps"""
        if step_ids is None:
            step_ids = list(self.steps.keys())
        
        results = {}
        for step_id in step_ids:
            step = self.steps.get(step_id)
            if step and step.status == WorkflowStepStatus.COMPLETED and step.result is not None:
                results[step_id] = step.result
        
        return results
    
    def is_complete(self) -> bool:
        """Check if workflow is complete (all steps completed or failed)"""
        if not self.steps:
            return True
        
        return all(
            step.status in [WorkflowStepStatus.COMPLETED, WorkflowStepStatus.FAILED, WorkflowStepStatus.SKIPPED]
            for step in self.steps.values()
        )
    
    def has_failures(self) -> bool:
        """Check if workflow has any failed steps"""
        return any(step.status == WorkflowStepStatus.FAILED for step in self.steps.values())
    
    def merge_context(self, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merge parameter overrides into workflow context.
        
        Args:
            overrides: Parameter values to merge into context
            
        Returns:
            Merged context dictionary
        """
        context = dict(self.context or {})
        context.update(overrides)
        return context
    
    def to_execution_data(self, context_overrides: Optional[Dict[str, Any]] = None) -> Any:
        """
        Convert Workflow to WorkflowExecutionData format.
        
        This method provides a clean way to convert a Workflow object into the
        WorkflowExecutionData format required by the workflow_execution command.
        
        Args:
            context_overrides: Optional parameter values to merge into workflow context
            
        Returns:
            WorkflowExecutionData instance ready for execution
        """
        from motet.core.commands.command_data_classes import WorkflowExecutionData
        
        # Merge context with overrides if provided
        context = self.merge_context(context_overrides or {})
        
        # Convert steps (Dict[str, WorkflowStep]) to workflow_steps (List[Dict]).
        # Execution control must ride along: from_execution_data rebuilds steps from
        # these dicts, so anything omitted here is silently dropped from the run
        # (a foreach step would execute once, an until step would not retry).
        # Runtime state (status/result/timestamps) is deliberately excluded.
        workflow_steps = []
        for step_id, step in self.steps.items():
            workflow_steps.append({
                "step_id": step_id,
                "name": step.name,
                "command_type": step.command_type,
                "command_data": step.command_data,
                "dependencies": step.dependencies,
                "execution_context": step.execution_context,
                "ownership": step.ownership,
                "step_type": step.step_type,
                "requires_confirmation": step.requires_confirmation,
                "elicitation_schema": step.elicitation_schema,
                "elicitation_prompt": step.elicitation_prompt,
                "continue_on_failure": step.continue_on_failure,
                "fallback_step_id": step.fallback_step_id,
                "skip_condition": step.skip_condition,
                "step_retry_attempts": step.step_retry_attempts,
                "step_retry_delay_seconds": step.step_retry_delay_seconds,
                "foreach": step.foreach,
                "loop_var": step.loop_var,
                "max_loop_iterations": step.max_loop_iterations,
                "until": step.until,
                "isolate_conversation": step.isolate_conversation,
            })
        
        return WorkflowExecutionData(
            workflow_id=self.workflow_id,
            workflow_name=self.name,
            workflow_steps=workflow_steps,
            context=context,
            description=self.description,
            handback_tools=self.handback_tools,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
            "required_inputs": self.required_inputs,
            "input_parameters": self.input_parameters,
            "use_for": self.use_for,
            "output_field": self.output_field,
            "presentation": self.presentation,
            "handback_tools": self.handback_tools,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "steps": {step_id: step.to_dict() for step_id, step in self.steps.items()},
            "execution_order": self.execution_order,
            "context": self.context,
            "metadata": self.metadata,
        }

    @classmethod
    def from_execution_data(cls, data: Any, original_workflow: Optional["Workflow"] = None) -> "Workflow":
        """
        Create Workflow from WorkflowExecutionData, preserving metadata from original.
        
        This method converts WorkflowExecutionData (used by workflow_execution command) back
        into a Workflow object. It preserves input_parameters and required_inputs from the
        original workflow definition if provided.
        
        Args:
            data: WorkflowExecutionData from workflow_execution command
            original_workflow: Optional original workflow definition to preserve metadata from
            
        Returns:
            Workflow instance ready for execution
        """
        # Convert workflow_steps (List[Dict]) to steps (Dict[str, WorkflowStep]).
        # Always rebuild from dicts (issue #189): never shallow-share registry
        # WorkflowStep instances across nested frames (self-recursion mutates).
        steps = {}
        step_dicts = list(data.workflow_steps or [])
        if not step_dicts and original_workflow:
            step_dicts = [
                {**step.to_dict(), "step_id": sid}
                for sid, step in original_workflow.steps.items()
            ]
        for step_dict in step_dicts:
            step_id = step_dict.get("step_id")
            if not step_id:
                continue

            # Check if it's new format (has command_type) or old format (has tool_name)
            if "command_type" in step_dict:
                # New format - rebuild from a copy so frames stay isolated
                steps[step_id] = WorkflowStep(**dict(step_dict))
            elif "tool_name" in step_dict:
                # Old format - convert to new format
                steps[step_id] = WorkflowStep(
                    step_id=step_id,
                    name=step_dict.get("name", step_id),
                    command_type="tool_execution",
                    command_data={
                        "tool_name": step_dict.get("tool_name"),
                        "parameters": step_dict.get("parameters", {}) or step_dict.get("params_template", {})
                    },
                    dependencies=step_dict.get("dependencies", []),
                    execution_context=step_dict.get("execution_context")
                )
            else:
                raise ValueError(f"Step {step_id} missing command_type or tool_name")
        
        data_handback = getattr(data, "handback_tools", None)
        # Create Workflow object, preserving metadata from original if provided
        workflow = cls(
            workflow_id=data.workflow_id,
            name=data.workflow_name,
            description=getattr(data, 'description', ''),
            steps=steps,
            context=getattr(data, 'context', {}) or {},
            # Preserve input_parameters, required_inputs, use_for, output_field from original definition
            input_parameters=original_workflow.input_parameters if original_workflow else None,
            required_inputs=original_workflow.required_inputs if original_workflow else None,
            use_for=original_workflow.use_for if original_workflow else None,
            keywords=original_workflow.keywords if original_workflow else None,
            output_field=original_workflow.output_field if original_workflow else None,
            presentation=original_workflow.presentation if original_workflow else None,
            handback_tools=(
                data_handback
                if data_handback is not None
                else (original_workflow.handback_tools if original_workflow else None)
            ),
            durable=bool(getattr(original_workflow, "durable", False)) if original_workflow else False,
            max_nesting_depth=(
                getattr(original_workflow, "max_nesting_depth", None)
                if original_workflow
                else None
            ),
        )
        
        return workflow
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workflow":
        """Create a Workflow from a serialized dict."""
        wf_status = data.get("status", WorkflowStatus.CREATED)
        if isinstance(wf_status, str):
            wf_status = WorkflowStatus(wf_status)
        created_at = data.get("created_at")
        started_at = data.get("started_at")
        completed_at = data.get("completed_at")
        try:
            created_at_dt = datetime.fromisoformat(created_at) if created_at else datetime.utcnow()
        except Exception:
            created_at_dt = datetime.utcnow()
        try:
            started_at_dt = datetime.fromisoformat(started_at) if started_at else None
        except Exception:
            started_at_dt = None
        try:
            completed_at_dt = datetime.fromisoformat(completed_at) if completed_at else None
        except Exception:
            completed_at_dt = None

        # Build steps; input may be dict keyed by step_id or a list of step dicts
        steps_in = data.get("steps") or {}
        steps: Dict[str, WorkflowStep] = {}
        if isinstance(steps_in, dict):
            for sid, sdata in steps_in.items():
                try:
                    # Ensure step_id consistency
                    sdata = dict(sdata or {})
                    sdata.setdefault("step_id", sid)
                    step = WorkflowStep.from_dict(sdata)
                    steps[sid] = step
                except Exception:
                    continue  # skip malformed step data during deserialization
        elif isinstance(steps_in, list):
            for sdata in steps_in:
                try:
                    step = WorkflowStep.from_dict(sdata)
                    steps[step.step_id] = step
                except Exception:
                    continue  # skip malformed step data during deserialization

        presentation_raw = data.get("presentation")
        presentation = (
            dict(presentation_raw)
            if isinstance(presentation_raw, dict) and presentation_raw
            else None
        )

        wf = cls(
            workflow_id=str(data.get("workflow_id")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            keywords=data.get("keywords"),
            steps=steps,
            required_inputs=data.get("required_inputs"),
            input_parameters=data.get("input_parameters"),
            use_for=data.get("use_for"),
            output_field=data.get("output_field") or None,
            presentation=presentation,
            handback_tools=data.get("handback_tools"),
            durable=bool(data.get("durable", False)),
            max_nesting_depth=data.get("max_nesting_depth"),
            context=dict(data.get("context") or {}),
            metadata=dict(data.get("metadata") or {}),
            status=wf_status,
            created_at=created_at_dt,
            started_at=started_at_dt,
            completed_at=completed_at_dt,
            execution_order=list(data.get("execution_order") or []),
        )
        # If execution_order wasn't provided, __post_init__ already computed it
        return wf


# ============================================================================
# COMMAND REGISTRY + UTILITIES - split into workflow.utils
# ============================================================================

from .utils import (
    get_command_by_name,
    list_registered_commands,
    validate_workflow,
    validate_execution_context,
    substitute_parameters,
)
from .builder import (
    run_workflow_builder,
    BuilderResult,
    BuilderError,
)
from .builtins import register_builtin_workflows
from .executor import WorkflowExecutor as WorkflowExecutor
from .registry import WorkflowRegistry as WorkflowRegistry
from .checkpoint import (
    PendingInteraction,
    WorkflowCheckpoint,
    WorkflowResumeConflict,
    WorkflowRunStatus,
    WorkflowSuspendNotConsumable,
    WorkflowSuspendReason,
    claim_workflow_run_for_resume,
    find_workflow_run_id_by_interaction,
    list_paused_workflow_runs,
    load_workflow_checkpoint,
    store_workflow_checkpoint,
)

# NOTE: Command registration is automatic via @distributed_command decorator.
# Built-in workflow templates are registered at import time.
register_builtin_workflows(Workflow=Workflow, WorkflowStep=WorkflowStep, WorkflowRegistry=WorkflowRegistry)


__all__ = [
    "WorkflowStepStatus",
    "WorkflowStatus",
    "WorkflowStep",
    "Workflow",
    "workflow_discovery_keywords",
    # Unified Workflow Architecture
    "WorkflowExecutor",
    "WorkflowRegistry",
    "get_command_by_name",
    "list_registered_commands",
    "validate_workflow",
    "validate_execution_context",
    "substitute_parameters",
    "run_workflow_builder",
    "BuilderResult",
    "BuilderError",
    # Issue #149 / #189: workflow suspension + nesting
    "PendingInteraction",
    "WorkflowCheckpoint",
    "WorkflowResumeConflict",
    "WorkflowRunStatus",
    "WorkflowSuspendNotConsumable",
    "WorkflowSuspendReason",
    "claim_workflow_run_for_resume",
    "find_workflow_run_id_by_interaction",
    "list_paused_workflow_runs",
    "load_workflow_checkpoint",
    "store_workflow_checkpoint",
]
