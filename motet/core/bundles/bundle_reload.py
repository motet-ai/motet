"""
Motet - Bundle Reload/Unload Commands (AI Worker Side)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    AI-worker distributed commands for bundle lifecycle management.
    These commands are dispatched by the deployer worker and execute on standard
    AI workers (not on the deployer worker).

    Commands:
    - core.reload_bundle: Pull artifact from shared store, extract to PLUGIN_ROOT,
    load config/model-spec/MCP/commands/tools, update search index. All-or-nothing
    per-worker atomicity: rolls back on any failure.
    - core.unload_bundle: Unregister all bundle artifacts from registries and search
    index; remove PLUGIN_ROOT/<bundle_id>/.
    - core.hot_reload_bundle: Dev-only reload from a shared local bundle path.

    Reload acks include ``command_schemas`` (Pydantic ``model_json_schema()`` per
    registered command) so the deployer can merge them into the Redis bundle
    catalog for manage/API list rows.

    Startup catch-up is implemented in worker_initialization.py via
    `load_bundles_on_startup()` which queries the bundle registry and pulls any
    bundles not yet loaded or not at the current version. Hot-mode bundles
    (no Redis artifact) rehydrate from PLUGIN_ROOT or the ``hot:<path>``
    fingerprint so worker restarts do not drop hot-deployed commands (#125).

Dependencies:
    - motet.core.commands.decorator / motet: @motet.command, get_motet_context
    - motet.core.bundles.deploy: Artifact store helpers, registry helpers
    - motet.core.commands.command_type_registry: CommandTypeRegistry
    - motet.core.tools.registry: Tool registry
    - motet.core.tools.function_discovery_vector_store: Search index refresh
    - importlib: Dynamic module loading from bundle filesystem path
    - pathlib: Bundle directory management

Usage:
    # Dispatched by publish_bundle via motet.apply()
    from motet.core.bundles.bundle_reload import (
        reload_bundle, unload_bundle, ReloadBundleData, UnloadBundleData,
        load_bundles_on_startup,
    )

Notes:
    - All-or-nothing per worker: any failure rolls back and raises CommandExecutionError.
    - Worker must have a writable PLUGIN_ROOT (default: /tmp/imf_bundles or MOTET_PLUGIN_ROOT).
    - Commands, tools, and workflows are registered under namespaced keys: bundle_id.name.
    - Workflows are loaded from YAML files in the workflows/ directory using WorkflowRegistry.
    - MCP config merge is V1 config-only (no bundled server scripts in V1).
    - Targeting metadata is stored with each registry entry for request-time filtering.
    - When loading bundle commands, _inject_motet_sdk_runtime_bridge ensures
      bundles that use 'from motet_sdk import distributed_command, MotetContext' get
      the runtime's real decorator and context (no need to install motet-sdk in worker image).
    - _load_bundle purges every bundle.<id>.* sys.modules entry, package parents included,
      before loading any section. Keeping a parent kept its submodule attributes, so
      'from. import _helpers' served the previous revision and a redeployed command ran
      against a stale helper (#169). commands/, tools/, and routing/ are all registered as
      packages with an authoritative __path__ so shared underscore modules work uniformly.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

import structlog
from pydantic import BaseModel, Field

from motet import motet
from motet.core.commands.base_command_data import BaseCommandData
from motet.core.commands.decorator import (
    bundle_command_namespace,
    bundle_tool_namespace,
    get_motet_context,
)
logger = structlog.get_logger(__name__)

PLUGIN_ROOT = Path(os.getenv("MOTET_PLUGIN_ROOT", "/tmp/imf_bundles"))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ReloadBundleData(BaseCommandData):
    """
    Input data for core.reload_bundle (AI-worker command).

    Dispatched by core.publish_bundle via motet.apply(). The execution context
    (conversation_id, tenant_id, principal_id) propagates automatically.
    """

    bundle_id: str = Field(..., description="Bundle slug (manifest name)")
    bundle_version: str = Field(..., description="Git tree SHA of the artifact to load")
    targeting: Optional[Dict[str, Any]] = Field(
        None,
        description="BundleTargeting dict — stored with registry entries for request-time filtering",
    )
    target_worker_id: Optional[str] = Field(
        None,
        description="If set, route this reload to this worker only (used by publish_bundle to ensure one reload per worker).",
    )


class UnloadBundleData(BaseCommandData):
    """
    Input data for core.unload_bundle (AI-worker command).

    Dispatched by core.undeploy_bundle via motet.apply(). Each invocation
    includes target_worker_id so the router sends the unload to the intended
    worker (mirrors the reload/hot-reload pattern).
    """

    bundle_id: str = Field(..., description="Bundle slug to unload")
    target_worker_id: Optional[str] = Field(
        None,
        description="If set, route this unload to this worker only (used by undeploy_bundle to ensure one unload per worker).",
    )


class HotReloadBundleData(BaseCommandData):
    """
    Input data for core.hot_reload_bundle (AI-worker dev-only command).

    The bundle_path must be readable inside the worker container (shared mount).
    """

    bundle_id: str = Field(..., description="Bundle slug (manifest name)")
    bundle_version: str = Field(..., description="Local content hash/version for observability")
    bundle_path: str = Field(..., description="Shared local path to bundle root")
    targeting: Optional[Dict[str, Any]] = Field(
        None,
        description="BundleTargeting dict — stored with registry entries for request-time filtering",
    )
    target_worker_id: Optional[str] = Field(
        None,
        description="If set, route this reload to this worker only",
    )


# ---------------------------------------------------------------------------
# AI-worker commands
# ---------------------------------------------------------------------------


@motet.command(
    description="Pull a bundle artifact onto this worker, extract it, and register its commands, tools, and workflows.",
    timeout_seconds=120)
def reload_bundle(data: ReloadBundleData) -> Dict[str, Any]:
    """
    Pull a bundle artifact from the shared store, extract it to PLUGIN_ROOT/<bundle_id>/,
    and load config → model spec → MCP config → commands/tools/strategies into worker
    registries. Updates the command/tool search index after loading.

    All-or-nothing per worker: if any step fails, the previous bundle snapshot is
    restored and CommandExecutionError is raised. The deployer marks this worker
    as 'failed' and may retry once automatically (motet.apply retry semantics).
    """
    motet = get_motet_context()
    from motet.core.bundles.deploy import _fetch_artifact, _unpack_artifact
    from motet.core.commands.response_models import CommandExecutionError

    redis_client = motet.redis
    bundle_dir = PLUGIN_ROOT / data.bundle_id
    backup_dir = PLUGIN_ROOT / f".{data.bundle_id}.backup"

    # --- Create backup of current bundle dir for rollback ---
    if bundle_dir.exists():
        if backup_dir.exists():
            _remove_tree_if_present(backup_dir)
        shutil.copytree(bundle_dir, backup_dir)

    try:
        # --- Fetch artifact from shared store ---
        artifact_bytes = _fetch_artifact(redis_client, data.bundle_id, data.bundle_version)
        if not artifact_bytes:
            raise CommandExecutionError(
                error_type="ArtifactNotFound",
                message=f"Artifact not found in store for bundle '{data.bundle_id}' version '{data.bundle_version}'",
                details={"bundle_id": data.bundle_id, "bundle_version": data.bundle_version},
                recoverable=False,
                command_type="core.reload_bundle",
                command_id=motet.command_id,
            )

        # --- Extract artifact to bundle dir ---
        if bundle_dir.exists():
            _remove_tree_if_present(bundle_dir)
        _unpack_artifact(artifact_bytes, bundle_dir)

        logger.info(
            "reload_bundle_extracted",
            bundle_id=data.bundle_id,
            bundle_version=data.bundle_version,
            bundle_dir=str(bundle_dir),
        )

        # --- Load bundle contents (config → model spec → MCP → commands/tools) ---
        loaded = _load_bundle(data.bundle_id, bundle_dir, data.targeting, data.bundle_version)

        # --- Prune stale renamed/deleted symbols, then update search index ---
        _prune_stale_bundle_registrations(data.bundle_id, loaded)
        _refresh_search_index(bundle_id=data.bundle_id, loaded=loaded)

        # Persist version marker so startup catch-up can re-register after restart
        # without re-fetching the artifact when the tree is already current.
        (bundle_dir / ".bundle_version").write_text(data.bundle_version)

        # --- Clean up backup ---
        if backup_dir.exists():
            _remove_tree_if_present(backup_dir)

        logger.info(
            "reload_bundle_success",
            bundle_id=data.bundle_id,
            bundle_version=data.bundle_version,
            registered_commands=loaded.get("commands", []),
            registered_tools=loaded.get("tools", []),
            registered_workflows=loaded.get("workflows", []),
            registered_agents=loaded.get("agents", []),
            registered_skills=loaded.get("skills", []),
            command_schema_count=len(loaded.get("command_schemas") or {}),
        )
        return {
            "bundle_id": data.bundle_id,
            "bundle_version": data.bundle_version,
            "load_status": "loaded",
            "registered_commands": loaded.get("commands", []),
            "registered_tools": loaded.get("tools", []),
            "registered_workflows": loaded.get("workflows", []),
            "registered_agents": loaded.get("agents", []),
            "registered_skills": loaded.get("skills", []),
            "command_schemas": loaded.get("command_schemas") or {},
        }

    except (CommandExecutionError, Exception) as exc:
        # --- Rollback to previous snapshot ---
        logger.error(
            "reload_bundle_failed_rolling_back",
            bundle_id=data.bundle_id,
            error=str(exc),
            exc_info=True,
        )
        _rollback_bundle_dir(data.bundle_id, bundle_dir, backup_dir)
        if not isinstance(exc, CommandExecutionError):
            raise CommandExecutionError(
                error_type=type(exc).__name__,
                message=f"Bundle reload failed for '{data.bundle_id}': {exc}",
                details={"bundle_id": data.bundle_id, "error": str(exc)},
                recoverable=False,
                command_type="core.reload_bundle",
                command_id=motet.command_id,
            ) from exc
        raise


@motet.command(
    description="Unregister a bundle's commands, tools, and workflows from this worker and remove its plugin files.",
    timeout_seconds=60)
def unload_bundle(data: UnloadBundleData) -> Dict[str, Any]:
    """
    Unregister all artifacts contributed by bundle_id from worker registries
    (CommandTypeRegistry, tool registry, model spec, MCP config, search index)
    and remove PLUGIN_ROOT/<bundle_id>/.
    """
    bundle_dir = PLUGIN_ROOT / data.bundle_id

    # Unregister commands (all keys prefixed bundle_id.)
    _unregister_bundle_commands(data.bundle_id)

    # Unregister tools
    _unregister_bundle_tools(data.bundle_id)

    # Unregister workflows
    _unregister_bundle_workflows(data.bundle_id)

    # Unregister agent configs
    _unregister_bundle_agents(data.bundle_id)

    # Unregister bundle skills (ADR-0073)
    _unregister_bundle_skills(data.bundle_id)

    # Unregister model spec entries contributed by this bundle
    _unregister_bundle_model_spec(data.bundle_id)

    # Unregister MCP servers this bundle registered (before deleting yaml)
    _unregister_bundle_mcp(data.bundle_id, bundle_dir)

    # Remove bundle dir
    if bundle_dir.exists():
        _remove_tree_if_present(bundle_dir)

    # Refresh search index
    _refresh_search_index(
        bundle_id=data.bundle_id,
        loaded={"commands": [], "tools": [], "workflows": [], "skills": []},
    )

    logger.info("unload_bundle_success", bundle_id=data.bundle_id)
    return {"bundle_id": data.bundle_id, "load_status": "unloaded"}


@motet.command(
    description="Dev-only: reload a bundle on this worker from a shared local filesystem path.",
    timeout_seconds=90)
def hot_reload_bundle(data: HotReloadBundleData) -> Dict[str, Any]:
    """
    Dev-only local bundle reload from a shared filesystem path.

    Copies source bundle files from data.bundle_path to PLUGIN_ROOT/<bundle_id>,
    then runs the standard bundle loading path to refresh registries/indexes.
    """
    from motet.core.commands.response_models import CommandExecutionError

    source_dir = Path(data.bundle_path).resolve()
    if not source_dir.exists() or not source_dir.is_dir():
        motet = get_motet_context()
        raise CommandExecutionError(
            error_type="InvalidBundlePath",
            message=f"Hot reload source path does not exist: {source_dir}",
            details={"bundle_path": str(source_dir), "bundle_id": data.bundle_id},
            recoverable=False,
            command_type="core.hot_reload_bundle",
            command_id=motet.command_id,
        )

    bundle_dir = PLUGIN_ROOT / data.bundle_id
    backup_dir = PLUGIN_ROOT / f".{data.bundle_id}.backup"

    if bundle_dir.exists():
        if backup_dir.exists():
            _remove_tree_if_present(backup_dir)
        shutil.copytree(bundle_dir, backup_dir)

    try:
        t0 = time.perf_counter()
        if bundle_dir.exists():
            _remove_tree_if_present(bundle_dir)
        t_copy_start = time.perf_counter()
        shutil.copytree(source_dir, bundle_dir)
        t_copy_end = time.perf_counter()

        t_load_start = time.perf_counter()
        loaded = _load_bundle(data.bundle_id, bundle_dir, data.targeting, data.bundle_version)
        t_load_end = time.perf_counter()

        _prune_stale_bundle_registrations(data.bundle_id, loaded)
        t_index_start = time.perf_counter()
        _refresh_search_index(bundle_id=data.bundle_id, loaded=loaded)
        t_index_end = time.perf_counter()

        # Hot deploys publish no Redis artifact; the version marker is how
        # load_bundles_on_startup re-registers after a worker restart (#125).
        (bundle_dir / ".bundle_version").write_text(data.bundle_version)

        if backup_dir.exists():
            _remove_tree_if_present(backup_dir)

        t_total_end = time.perf_counter()
        timings_ms = {
            "copy_ms": (t_copy_end - t_copy_start) * 1000.0,
            "load_ms": (t_load_end - t_load_start) * 1000.0,
            "index_ms": (t_index_end - t_index_start) * 1000.0,
            "total_ms": (t_total_end - t0) * 1000.0,
        }

        logger.info(
            "hot_reload_bundle_success",
            bundle_id=data.bundle_id,
            bundle_version=data.bundle_version,
            bundle_path=str(source_dir),
            registered_commands=loaded.get("commands", []),
            registered_tools=loaded.get("tools", []),
            registered_workflows=loaded.get("workflows", []),
            registered_agents=loaded.get("agents", []),
            registered_skills=loaded.get("skills", []),
            command_schema_count=len(loaded.get("command_schemas") or {}),
            timings_ms=timings_ms,
        )
        return {
            "bundle_id": data.bundle_id,
            "bundle_version": data.bundle_version,
            "bundle_path": str(source_dir),
            "load_status": "loaded",
            "registered_commands": loaded.get("commands", []),
            "registered_tools": loaded.get("tools", []),
            "registered_workflows": loaded.get("workflows", []),
            "registered_agents": loaded.get("agents", []),
            "registered_skills": loaded.get("skills", []),
            "command_schemas": loaded.get("command_schemas") or {},
            "timings_ms": timings_ms,
        }
    except (CommandExecutionError, Exception) as exc:
        logger.error(
            "hot_reload_bundle_failed_rolling_back",
            bundle_id=data.bundle_id,
            bundle_version=data.bundle_version,
            bundle_path=str(source_dir),
            error=str(exc),
            exc_info=True,
        )
        _rollback_bundle_dir(data.bundle_id, bundle_dir, backup_dir)
        if not isinstance(exc, CommandExecutionError):
            motet = get_motet_context()
            raise CommandExecutionError(
                error_type=type(exc).__name__,
                message=f"Hot reload failed for '{data.bundle_id}': {exc}",
                details={"bundle_id": data.bundle_id, "bundle_path": str(source_dir), "error": str(exc)},
                recoverable=False,
                command_type="core.hot_reload_bundle",
                command_id=motet.command_id,
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Bundle loading logic
# ---------------------------------------------------------------------------


def _load_bundle_skills(
    bundle_id: str,
    bundle_dir: Path,
    targeting: Any,
    bundle_version: Optional[str],
    *,
    runner_tools_out: Optional[List[str]] = None,
) -> List[str]:
    """Register each ``skills/<name>/SKILL.md`` into the worker skill registry (ADR-0073).

    When a sibling ``skills/<name>/runners.yaml`` exists (ADR-0101 Slice B),
    each declared runner is also registered as a namespaced tool
    ``{bundle_id}.{skill_name}.{runner}``. The names of those tools are
    appended to ``runner_tools_out`` so the caller can include them in
    the bundle's loaded["tools"] list (this keeps prune + semantic-index
    sync correct on redeploy).
    """
    skills_root = bundle_dir / "skills"
    if not skills_root.is_dir():
        return []

    from motet.core.skills.parser import parse_skill_markdown
    from motet.core.skills.registry import RegisteredSkill, get_skill_registry
    from motet.core.skills.runtime import register_runners_for_skill

    reg = get_skill_registry()
    registered: List[str] = []
    targeting_meta: Optional[Dict[str, Any]] = None
    if targeting is not None:
        try:
            targeting_meta = targeting.model_dump() if hasattr(targeting, "model_dump") else dict(targeting)
        except Exception:
            targeting_meta = None

    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        md_path = skill_dir / "SKILL.md"
        if not md_path.is_file():
            continue
        try:
            doc = parse_skill_markdown(md_path)
        except Exception as e:
            logger.warning(
                "load_bundle_skill_parse_failed",
                bundle_id=bundle_id,
                path=str(md_path),
                error=str(e),
            )
            continue
        if doc.name != skill_dir.name:
            logger.warning(
                "load_bundle_skill_name_dir_mismatch",
                bundle_id=bundle_id,
                directory=skill_dir.name,
                frontmatter_name=doc.name,
            )
        skill_id = f"{bundle_id}.{doc.name}"
        reg.register(
            RegisteredSkill(
                skill_id=skill_id,
                bundle_id=bundle_id,
                name=doc.name,
                description=doc.description,
                skill_md_path=md_path.resolve(),
                source="bundle",
                bundle_version=bundle_version,
                targeting=targeting_meta,
            )
        )
        registered.append(skill_id)
        logger.info("load_bundle_skill_registered", bundle_id=bundle_id, skill_id=skill_id)

        # ADR-0101 Slice B: register declared runners as namespaced tools.
        try:
            runner_tool_names = register_runners_for_skill(
                bundle_id=bundle_id,
                skill_name=doc.name,
                skill_dir=skill_dir.resolve(),
                bundle_id_for_staging=bundle_id,
            )
        except Exception as exc:
            # Bundle reload SHOULD fail loudly on a malformed runners.yaml so
            # the operator sees the problem in the deploy stream — this
            # matches how bundle tools are loaded (a bad tool file raises).
            raise RuntimeError(
                f"Failed to register runners for skill '{doc.name}' "
                f"in bundle '{bundle_id}': {exc}"
            ) from exc
        if runner_tools_out is not None:
            runner_tools_out.extend(runner_tool_names)
        if runner_tool_names:
            logger.info(
                "load_bundle_skill_runners_registered",
                bundle_id=bundle_id,
                skill_id=skill_id,
                count=len(runner_tool_names),
                tools=runner_tool_names,
            )
    return sorted(set(registered))


def _command_schemas_from_registry(command_types: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Build ``command_type → JSON Schema`` from registered Pydantic data classes.

    Best-effort: import/schema failures omit that command rather than failing reload.
    Used in reload acks so the deployer can persist schemas into the Redis catalog.
    """
    from motet.core.commands.command_type_registry import command_type_registry

    out: Dict[str, Dict[str, Any]] = {}
    for command_type in command_types:
        try:
            reg = command_type_registry.get(command_type)
            data_class = getattr(reg, "data_class", None) if reg is not None else None
            if data_class is None:
                continue
            schema = data_class.model_json_schema()
            if isinstance(schema, dict):
                out[str(command_type)] = schema
        except Exception as e:
            logger.debug(
                "command_schema_extract_failed",
                command_type=command_type,
                error=str(e),
                error_type=type(e).__name__,
            )
    return out


def _load_bundle(
    bundle_id: str,
    bundle_dir: Path,
    targeting_raw: Optional[Dict[str, Any]],
    bundle_version: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Load all bundle artifacts in the prescribed order:
    1. config/  — routing config, feature flags
    2. skills/ — SKILL.md packages (ADR-0073)
    3. config/models.yaml — merge into worker model spec (add-only)
    4. config/mcp.yaml   — merge into MCP instance manager config (V1: config only)
    5. config/agents.yaml — register namespaced agent configs into AgentConfigRegistry
    6. config/surfaces.yaml — register_if_absent into surfaces catalog (no-op if exists)
    7. commands/ — register all @motet.command functions
    8. tools/    — register all custom tool definitions
    9. workflows/ — register workflow YAML definitions
    10. routing/  — load custom routing/strategy modules

    Returns a dict with namespaced registered names for commands/tools/workflows/agents/skills,
    plus ``command_schemas`` (JSON Schema maps for manage/API catalog rows).
    """
    from motet.core.bundles.deploy import BundleTargeting

    targeting: Optional[BundleTargeting] = None
    if targeting_raw:
        try:
            targeting = BundleTargeting(**targeting_raw)
        except Exception:
            pass  # targeting parse best-effort; continue without

    # Determine load order from manifest or use default
    manifest_path = bundle_dir / "manifest.yaml"
    load_order = ["config", "skills", "commands", "tools", "workflows", "routing"]
    if manifest_path.exists():
        try:
            import yaml  # type: ignore[import]
            manifest = yaml.safe_load(manifest_path.read_text()) or {}
            if "load_order" in manifest:
                load_order = manifest["load_order"]
        except Exception as e:
            logger.warning("load_bundle_manifest_parse_error", bundle_id=bundle_id, error=str(e))

    _inject_motet_sdk_runtime_bridge()

    # Drop every ``bundle.<id>.*`` module (packages included) before any section
    # loads, so a reload cannot execute a fresh command against a helper cached
    # from the previous deploy. Doing it here rather than per loader keeps
    # commands, tools, and routing on the same footing (#169).
    _purge_bundle_modules(bundle_id)
    # Bundle files were just written to disk by the caller; without this the
    # import machinery can serve a cached directory listing and miss new files.
    importlib.invalidate_caches()

    registered_commands: List[str] = []
    registered_tools: List[str] = []
    registered_workflows: List[str] = []
    registered_agents: List[str] = []
    registered_skills: List[str] = []

    runner_tools: List[str] = []

    for section in load_order:
        section_dir = bundle_dir / section
        if section == "config":
            registered_agents.extend(_load_bundle_config(bundle_id, bundle_dir, targeting))
        elif section == "skills":
            registered_skills.extend(
                _load_bundle_skills(
                    bundle_id,
                    bundle_dir,
                    targeting,
                    bundle_version,
                    runner_tools_out=runner_tools,
                )
            )
        elif section == "commands" and section_dir.exists():
            registered_commands.extend(_load_bundle_commands(bundle_id, section_dir, targeting))
        elif section == "tools" and section_dir.exists():
            registered_tools.extend(_load_bundle_tools(bundle_id, section_dir, targeting))
        elif section == "workflows" and section_dir.exists():
            registered_workflows.extend(_load_bundle_workflows(bundle_id, section_dir))
        elif section == "routing" and section_dir.exists():
            _load_bundle_routing(bundle_id, section_dir)
        elif section_dir.exists():
            logger.debug("load_bundle_unknown_section", bundle_id=bundle_id, section=section)

    # Runner-driven tools (ADR-0101 Slice B) are surfaced into the
    # bundle's tools list so prune + the semantic index see them on
    # equal footing with tools/*.py registrations.
    combined_tools = sorted(set(registered_tools) | set(runner_tools))
    command_schemas = _command_schemas_from_registry(registered_commands)

    if registered_agents:
        from motet.core.orchestration.turn.hook_resolve import validate_bundle_agent_hooks

        validate_bundle_agent_hooks(registered_agents)

    return {
        "commands": registered_commands,
        "tools": combined_tools,
        "workflows": registered_workflows,
        "agents": sorted(set(registered_agents)),
        "skills": sorted(set(registered_skills)),
        "command_schemas": command_schemas,
    }


def _prune_stale_bundle_registrations(bundle_id: str, loaded: Dict[str, Any]) -> None:
    """
    Remove stale namespaced registrations for this bundle after a successful load.

    This handles rename/delete cases (e.g., tool renamed from `bundle.old` to `bundle.new`)
    where the new load path doesn't automatically unregister removed symbols.
    """
    prefix = f"{bundle_id}."
    expected_commands = set(loaded.get("commands", []))
    expected_tools = set(loaded.get("tools", []))
    expected_workflows = set(loaded.get("workflows", []))
    expected_agents = set(loaded.get("agents", []))
    # Only prune skills when the load snapshot includes a skills list (partial dicts skip).
    expected_skills_opt: Optional[Set[str]] = set(loaded["skills"]) if "skills" in loaded else None

    # Commands
    try:
        from motet.core.commands.command_type_registry import command_type_registry
        all_commands = command_type_registry.get_command_types()
        stale_commands = [ct for ct in all_commands if ct.startswith(prefix) and ct not in expected_commands]
        for ct in stale_commands:
            try:
                command_type_registry.unregister(ct)
                logger.info("prune_stale_bundle_command", bundle_id=bundle_id, command_type=ct)
            except Exception as e:
                logger.warning(
                    "prune_stale_bundle_command_failed",
                    bundle_id=bundle_id,
                    command_type=ct,
                    error=str(e),
                )
    except Exception as e:
        logger.warning("prune_stale_bundle_commands_scan_failed", bundle_id=bundle_id, error=str(e))

    # Tools
    try:
        from motet.core.tools import registry as tool_registry
        if hasattr(tool_registry, "get_all_tool_names"):
            all_tools = tool_registry.get_all_tool_names()
        elif hasattr(tool_registry, "list_items"):
            all_tools = list(tool_registry.list_items().keys())
        else:
            all_tools = []
        stale_tools = [t for t in all_tools if t.startswith(prefix) and t not in expected_tools]
        for tool_name in stale_tools:
            try:
                tool_registry.unregister(tool_name)
                logger.info("prune_stale_bundle_tool", bundle_id=bundle_id, tool_name=tool_name)
            except Exception as e:
                logger.warning(
                    "prune_stale_bundle_tool_failed",
                    bundle_id=bundle_id,
                    tool_name=tool_name,
                    error=str(e),
                )
    except Exception as e:
        logger.warning("prune_stale_bundle_tools_scan_failed", bundle_id=bundle_id, error=str(e))

    # Workflows
    try:
        from motet.core.workflow import WorkflowRegistry
        stale_workflows = [
            wf.workflow_id
            for wf in WorkflowRegistry.list_all()
            if wf.workflow_id.startswith(prefix) and wf.workflow_id not in expected_workflows
        ]
        for wf_id in stale_workflows:
            try:
                WorkflowRegistry.unregister(wf_id)
                logger.info("prune_stale_bundle_workflow", bundle_id=bundle_id, workflow_id=wf_id)
            except Exception as e:
                logger.warning(
                    "prune_stale_bundle_workflow_failed",
                    bundle_id=bundle_id,
                    workflow_id=wf_id,
                    error=str(e),
                )
    except Exception as e:
        logger.warning("prune_stale_bundle_workflows_scan_failed", bundle_id=bundle_id, error=str(e))

    # Agents
    try:
        from motet.core.agents import get_agent_registry

        registry = get_agent_registry()
        stale_agents = [
            qid
            for cfg in registry.list()
            for qid in [f"{bundle_id}.{cfg.agent_id}"]
            if getattr(cfg, "bundle_id", None) == bundle_id and qid not in expected_agents
        ]
        for qid in stale_agents:
            try:
                registry.unregister(qid)
                logger.info("prune_stale_bundle_agent", bundle_id=bundle_id, qualified_id=qid)
            except Exception as e:
                logger.warning(
                    "prune_stale_bundle_agent_failed",
                    bundle_id=bundle_id,
                    qualified_id=qid,
                    error=str(e),
                )
    except Exception as e:
        logger.warning("prune_stale_bundle_agents_scan_failed", bundle_id=bundle_id, error=str(e))

    # Skills (ADR-0073)
    if expected_skills_opt is not None:
        try:
            from motet.core.skills import get_skill_registry

            reg = get_skill_registry()
            stale_skills = [
                rec.skill_id
                for rec in reg.list_all()
                if rec.skill_id.startswith(prefix) and rec.skill_id not in expected_skills_opt
            ]
            for skill_id in stale_skills:
                try:
                    reg.unregister_skill(skill_id)
                    logger.info("prune_stale_bundle_skill", bundle_id=bundle_id, skill_id=skill_id)
                except Exception as e:
                    logger.warning(
                        "prune_stale_bundle_skill_failed",
                        bundle_id=bundle_id,
                        skill_id=skill_id,
                        error=str(e),
                    )
        except Exception as e:
            logger.warning("prune_stale_bundle_skills_scan_failed", bundle_id=bundle_id, error=str(e))


def _load_bundle_surfaces(bundle_id: str, bundle_dir: Path) -> List[str]:
    """
    Register surfaces from config/surfaces.yaml into the Redis catalog.

    Existing surfaces are left unchanged (no-op). Returns surface ids touched.
    """
    surfaces_path: Optional[Path] = None
    for candidate in [
        bundle_dir / "config" / "surfaces.yaml",
        bundle_dir / "config" / "surfaces.yml",
        bundle_dir / "surfaces" / "surfaces.yaml",
        bundle_dir / "surfaces" / "surfaces.yml",
    ]:
        if candidate.exists():
            surfaces_path = candidate
            break
    if surfaces_path is None:
        return []

    try:
        from motet.core.bundles.deploy import (
            _extract_bundle_surfaces,
            _register_bundle_surfaces,
        )

        content = surfaces_path.read_bytes()
        # Map path relative to bundle root for extractor candidate matching.
        rel = str(surfaces_path.relative_to(bundle_dir)).replace("\\", "/")
        surfaces = _extract_bundle_surfaces(
            bundle_id,
            {rel: content},
            strict=True,
        )
        stats = _register_bundle_surfaces(bundle_id, surfaces)
        logger.info(
            "load_bundle_surfaces_registered",
            bundle_id=bundle_id,
            path=rel,
            **stats,
        )
        return [str(s.get("id")) for s in surfaces if s.get("id")]
    except Exception as e:
        logger.error(
            "load_bundle_surfaces_failed",
            bundle_id=bundle_id,
            error=str(e),
            exc_info=True,
        )
        raise RuntimeError(
            f"Failed to register surfaces for bundle '{bundle_id}': {e}"
        ) from e


def _load_bundle_config(
    bundle_id: str,
    bundle_dir: Path,
    targeting: Any,
) -> List[str]:
    """Load config/, models.yaml, mcp.yaml, agents.yaml, and surfaces.yaml entries."""
    registered_agents: List[str] = []
    # Model spec (add-only merge)
    for model_path in [bundle_dir / "config" / "models.yaml", bundle_dir / "models" / "models.yaml"]:
        if model_path.exists():
            _merge_model_spec(bundle_id, model_path)
            break

    # MCP config — enqueue register on the sibling manager control stream
    for mcp_path in [bundle_dir / "config" / "mcp.yaml", bundle_dir / "mcp" / "mcp.yaml"]:
        if mcp_path.exists():
            _merge_mcp_config(bundle_id, mcp_path)
            break

    # Agent config registry merge (strict)
    for agents_path in [
        bundle_dir / "config" / "agents.yaml",
        bundle_dir / "config" / "agents.yml",
        bundle_dir / "agents" / "agents.yaml",
        bundle_dir / "agents" / "agents.yml",
    ]:
        if agents_path.exists():
            registered_agents = _merge_agents_config(bundle_id, agents_path)
            break

    # Surfaces catalog (register_if_absent; shared Redis)
    _load_bundle_surfaces(bundle_id, bundle_dir)

    return registered_agents


# ---------------------------------------------------------------------------
# motet_sdk runtime bridge (ADR-0080 / ADR-0089) — non-mutating inject + restore
# ---------------------------------------------------------------------------

# sys.modules keys the bridge installs or replaces.
_SDK_BRIDGE_MODULE_KEYS: Tuple[str, ...] = (
    "motet_sdk",
    "motet_sdk.capabilities",
    "motet_sdk.context",
    "motet_sdk.command",
    "motet_sdk.models",
    "motet_sdk.motet_namespace",
    "motet_sdk.concurrency",
)

# Sentinel for "attribute was absent" in snapshots (distinct from None values).
_SDK_ATTR_MISSING: Any = object()

# Attributes historically mutated in-place on real SDK / runtime modules.
# Non-mutating inject no longer writes these on live objects, but snapshot/restore
# still tracks them so tests can fully reverse any legacy or partial inject.
_SDK_BRIDGE_ATTR_SPECS: Tuple[Tuple[str, str], ...] = (
    ("motet_sdk", "distributed_command"),
    ("motet_sdk", "motet"),
    ("motet_sdk", "MotetContext"),
    ("motet_sdk", "get_motet_context"),
    ("motet_sdk", "resolve_current_identity"),
    ("motet_sdk", "WorkerCapability"),
    ("motet_sdk", "BaseCommandData"),
    ("motet_sdk", "CommandError"),
    ("motet_sdk", "CommandMetadata"),
    ("motet_sdk", "IdentityContext"),
    ("motet_sdk", "CommandExecutionError"),
    ("motet_sdk", "GatherExecutionError"),
    ("motet_sdk", "ApplyExecutionError"),
    ("motet_sdk.command", "distributed_command"),
    ("motet_sdk.command", "get_motet_context"),
    ("motet_sdk.command", "resolve_current_identity"),
    ("motet_sdk.motet_namespace", "motet"),
    ("motet_sdk.context", "MotetContext"),
    ("motet_sdk.capabilities", "WorkerCapability"),
    ("motet_sdk.models", "BaseCommandData"),
    ("motet_sdk.models", "CommandError"),
    ("motet_sdk.models", "CommandMetadata"),
    ("motet_sdk.models", "IdentityContext"),
    ("motet_sdk.models", "CommandExecutionError"),
    ("motet_sdk.models", "GatherExecutionError"),
    ("motet_sdk.models", "ApplyExecutionError"),
    # Older inject setattr'd run_async_safe onto the real concurrency module.
    ("motet.core.workers.concurrency_primitives", "run_async_safe"),
)


def _copy_module_public_attrs(source: Any, dest_name: str) -> Any:
    """Build a fresh ModuleType, copying public attrs from *source* when present."""
    dest: Any = types.ModuleType(dest_name)
    if source is None:
        return dest
    for name in dir(source):
        if name.startswith("_"):
            continue
        try:
            setattr(dest, name, getattr(source, name))
        except Exception:
            continue
    if hasattr(source, "__all__"):
        try:
            dest.__all__ = list(getattr(source, "__all__"))
        except Exception:
            pass
    # Helpful for debugging / importlib; not required for bridge correctness.
    src_file = getattr(source, "__file__", None)
    if isinstance(src_file, str):
        dest.__file__ = src_file
    src_pkg = getattr(source, "__package__", None)
    if isinstance(src_pkg, str):
        dest.__package__ = src_pkg
    return dest


def _snapshot_motet_sdk_modules() -> Dict[str, Any]:
    """
    Snapshot ``sys.modules`` entries for ``motet_sdk*`` and critical attribute
    object identities so a later restore fully reverses bridge injection.

    Used by unit tests (and any scoped caller); production ``_load_bundle`` does
    not auto-restore — workers keep the bridge for the process lifetime.
    """
    modules: Dict[str, Any] = {key: sys.modules.get(key) for key in _SDK_BRIDGE_MODULE_KEYS}
    for key in list(sys.modules.keys()):
        if key == "motet_sdk" or key.startswith("motet_sdk."):
            modules.setdefault(key, sys.modules.get(key))
    # Track concurrency_primitives when present (legacy in-place run_async_safe).
    conc_key = "motet.core.workers.concurrency_primitives"
    if conc_key in sys.modules:
        modules.setdefault(conc_key, sys.modules.get(conc_key))

    attrs: Dict[Tuple[str, str], Any] = {}
    for mod_name, attr_name in _SDK_BRIDGE_ATTR_SPECS:
        mod = sys.modules.get(mod_name)
        if mod is None:
            attrs[(mod_name, attr_name)] = _SDK_ATTR_MISSING
            continue
        attrs[(mod_name, attr_name)] = getattr(mod, attr_name, _SDK_ATTR_MISSING)
    return {"modules": modules, "attrs": attrs}


def _restore_motet_sdk_modules(snapshot: Dict[str, Any]) -> None:
    """
    Restore ``sys.modules`` and attribute bindings captured by
    ``_snapshot_motet_sdk_modules``.

    Safe to call when inject used fresh ModuleType children (sys.modules swap
    alone is enough) and when older code mutated real SDK submodule attrs.
    """
    modules: Dict[str, Any] = dict(snapshot.get("modules") or {})
    attrs: Dict[Tuple[str, str], Any] = dict(snapshot.get("attrs") or {})

    # Drop bridge-only motet_sdk* keys that were not present pre-inject.
    for key in list(sys.modules.keys()):
        if key == "motet_sdk" or key.startswith("motet_sdk."):
            if key not in modules:
                sys.modules.pop(key, None)

    for key, mod in modules.items():
        if key == "motet.core.workers.concurrency_primitives":
            # Do not remove the real runtime module if it was only tracked for attrs.
            if mod is not None:
                sys.modules[key] = mod
            continue
        if mod is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = mod

    for (mod_name, attr_name), value in attrs.items():
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        if value is _SDK_ATTR_MISSING:
            if hasattr(mod, attr_name):
                try:
                    delattr(mod, attr_name)
                except Exception:
                    pass
            continue
        try:
            setattr(mod, attr_name, value)
        except Exception:
            pass


def snapshot_motet_sdk_runtime_bridge() -> Dict[str, Any]:
    """Public alias of ``_snapshot_motet_sdk_modules`` for tests and callers."""
    return _snapshot_motet_sdk_modules()


def restore_motet_sdk_runtime_bridge(snapshot: Dict[str, Any]) -> None:
    """Public alias of ``_restore_motet_sdk_modules`` for tests and callers."""
    _restore_motet_sdk_modules(snapshot)


@contextmanager
def motet_sdk_runtime_bridge() -> Generator[None, None, None]:
    """
    Context manager: snapshot SDK modules, inject the runtime bridge, yield,
    then fully restore (including any legacy in-place attribute values).

    Intended for unit tests and scoped tooling. Production ``_load_bundle``
    calls ``_inject_motet_sdk_runtime_bridge()`` directly and leaves the bridge
    installed for the worker process lifetime.
    """
    snap = _snapshot_motet_sdk_modules()
    try:
        _inject_motet_sdk_runtime_bridge()
        yield
    finally:
        _restore_motet_sdk_modules(snap)


def _inject_motet_sdk_runtime_bridge() -> None:
    """
    Inject runtime implementations into sys.modules['motet_sdk'] so that bundle
    command modules that do 'from motet_sdk import distributed_command, MotetContext'
    get the real decorator and context (ADR-0080).

    When motet_sdk is installed, we build a **fresh** bridge package and fresh
    submodule ModuleType objects (command, context, motet_namespace, models,
    capabilities, concurrency), copy needed public attrs, then set runtime
    symbols on those copies. The installed SDK submodule objects are never
    mutated in place — restoring sys.modules is sufficient to return to the
    SDK no-op decorator path (issue #116).

    When motet_sdk is not installed, we build a minimal bridge so SDK-style
    imports still work for decorators, context, and core SDK models.

    Does **not** auto-restore; workers keep the bridge for the process lifetime.
    Tests should use ``motet_sdk_runtime_bridge()`` or snapshot/restore helpers.
    """
    from motet.core.commands.decorator import (
    MotetContext as RuntimeMotetContext,
    distributed_command as runtime_distributed_command,
    get_motet_context as runtime_get_motet_context,
)
    from motet.core.workers.invoker_context import (
        IdentityContext as RuntimeIdentityContext,
        resolve_current_identity as runtime_resolve_current_identity,
    )
    from motet.core.commands.motet_namespace import motet as runtime_motet
    from motet.core.commands.capabilities import WorkerCapability as RuntimeWorkerCapability
    from motet.core.commands.base_command_data import BaseCommandData as RuntimeBaseCommandData
    from motet.core.commands.response_models import (
        ApplyExecutionError as RuntimeApplyExecutionError,
        CommandError as RuntimeCommandError,
        CommandExecutionError as RuntimeCommandExecutionError,
        CommandMetadata as RuntimeCommandMetadata,
        GatherExecutionError as RuntimeGatherExecutionError,
    )
    import motet.core.workers.concurrency_primitives as runtime_concurrency
    from motet.core.utils.async_helpers import run_async_safe as runtime_run_async_safe

    def _install_bridge(
        bridge: Any,
        *,
        capabilities_mod: Any,
        context_mod: Any,
        command_mod: Any,
        models_mod: Any,
        motet_ns_mod: Any,
        concurrency_mod: Any,
    ) -> None:
        bridge.distributed_command = runtime_distributed_command
        bridge.MotetContext = RuntimeMotetContext
        bridge.get_motet_context = runtime_get_motet_context
        bridge.resolve_current_identity = runtime_resolve_current_identity
        bridge.motet = runtime_motet  # ADR-0089: @motet.command, @motet.tool
        bridge.WorkerCapability = RuntimeWorkerCapability
        bridge.BaseCommandData = RuntimeBaseCommandData
        bridge.CommandError = RuntimeCommandError
        bridge.CommandMetadata = RuntimeCommandMetadata
        bridge.IdentityContext = RuntimeIdentityContext
        bridge.CommandExecutionError = RuntimeCommandExecutionError
        bridge.GatherExecutionError = RuntimeGatherExecutionError
        bridge.ApplyExecutionError = RuntimeApplyExecutionError

        capabilities_mod.WorkerCapability = RuntimeWorkerCapability
        context_mod.MotetContext = RuntimeMotetContext
        command_mod.distributed_command = runtime_distributed_command
        command_mod.get_motet_context = runtime_get_motet_context
        command_mod.resolve_current_identity = runtime_resolve_current_identity
        models_mod.BaseCommandData = RuntimeBaseCommandData
        models_mod.CommandError = RuntimeCommandError
        models_mod.CommandMetadata = RuntimeCommandMetadata
        models_mod.IdentityContext = RuntimeIdentityContext
        models_mod.CommandExecutionError = RuntimeCommandExecutionError
        models_mod.GatherExecutionError = RuntimeGatherExecutionError
        models_mod.ApplyExecutionError = RuntimeApplyExecutionError
        motet_ns_mod.motet = runtime_motet
        concurrency_mod.run_async_safe = runtime_run_async_safe

        bridge.capabilities = capabilities_mod
        bridge.context = context_mod
        bridge.command = command_mod
        bridge.models = models_mod
        bridge.motet_namespace = motet_ns_mod
        bridge.concurrency = concurrency_mod

        sys.modules["motet_sdk"] = bridge
        sys.modules["motet_sdk.capabilities"] = capabilities_mod
        sys.modules["motet_sdk.context"] = context_mod
        sys.modules["motet_sdk.command"] = command_mod
        sys.modules["motet_sdk.models"] = models_mod
        sys.modules["motet_sdk.motet_namespace"] = motet_ns_mod
        sys.modules["motet_sdk.concurrency"] = concurrency_mod

    # Fresh concurrency wrapper: copy runtime primitives without setattr on the
    # real motet.core.workers.concurrency_primitives module object.
    concurrency_mod = _copy_module_public_attrs(
        runtime_concurrency, "motet_sdk.concurrency"
    )

    try:
        import motet_sdk as real_sdk

        # Ensure common submodules are importable so we can clone them.
        real_capabilities = None
        real_context = None
        real_command = None
        real_models = None
        real_motet_ns = None
        try:
            import motet_sdk.capabilities as real_capabilities  # type: ignore[no-redef]
        except ImportError:
            real_capabilities = getattr(real_sdk, "capabilities", None)
        try:
            import motet_sdk.context as real_context  # type: ignore[no-redef]
        except ImportError:
            real_context = getattr(real_sdk, "context", None)
        try:
            import motet_sdk.command as real_command  # type: ignore[no-redef]
        except ImportError:
            real_command = getattr(real_sdk, "command", None)
        try:
            import motet_sdk.models as real_models  # type: ignore[no-redef]
        except ImportError:
            real_models = getattr(real_sdk, "models", None)
        try:
            import motet_sdk.motet_namespace as real_motet_ns  # type: ignore[no-redef]
        except ImportError:
            real_motet_ns = getattr(real_sdk, "motet_namespace", None)

        bridge: Any = _copy_module_public_attrs(real_sdk, "motet_sdk")
        # Package __path__ helps submodule imports resolve if anything walks it.
        real_path = getattr(real_sdk, "__path__", None)
        if real_path is not None:
            try:
                bridge.__path__ = list(real_path)  # type: ignore[attr-defined]
            except Exception:
                pass

        capabilities_mod = _copy_module_public_attrs(
            real_capabilities, "motet_sdk.capabilities"
        )
        context_mod = _copy_module_public_attrs(real_context, "motet_sdk.context")
        command_mod = _copy_module_public_attrs(real_command, "motet_sdk.command")
        models_mod = _copy_module_public_attrs(real_models, "motet_sdk.models")
        motet_ns_mod = _copy_module_public_attrs(
            real_motet_ns, "motet_sdk.motet_namespace"
        )

        _install_bridge(
            bridge,
            capabilities_mod=capabilities_mod,
            context_mod=context_mod,
            command_mod=command_mod,
            models_mod=models_mod,
            motet_ns_mod=motet_ns_mod,
            concurrency_mod=concurrency_mod,
        )
        return
    except ImportError:
        pass

    bridge_no_sdk: Any = types.ModuleType("motet_sdk")
    capabilities_mod = types.ModuleType("motet_sdk.capabilities")
    context_mod = types.ModuleType("motet_sdk.context")
    command_mod = types.ModuleType("motet_sdk.command")
    models_mod = types.ModuleType("motet_sdk.models")
    motet_ns_mod = types.ModuleType("motet_sdk.motet_namespace")
    _install_bridge(
        bridge_no_sdk,
        capabilities_mod=capabilities_mod,
        context_mod=context_mod,
        command_mod=command_mod,
        models_mod=models_mod,
        motet_ns_mod=motet_ns_mod,
        concurrency_mod=concurrency_mod,
    )



def _ensure_bundle_package_hierarchy(
    bundle_id: str,
    subpackage: str,
    directory: Path,
) -> None:
    """Register ``bundle`` / ``bundle.<id>`` / ``bundle.<id>.<sub>`` packages.

    Bundle module files are loaded individually by absolute path, but their
    dotted module names imply a package hierarchy. Registering the parents in
    ``sys.modules`` with a ``__path__`` pointing at the source directory lets
    bundle modules use relative imports for shared underscore modules (e.g.
    ``from . import _helpers``) instead of copy-pasting importlib shims.
    The names are only ``sys.modules`` keys — hyphenated bundle ids are fine
    because the import machinery resolves relative imports by string lookup.

    The subpackage ``__path__`` is **assigned**, not appended to: it must name
    exactly the directory being loaded right now. An append-only ``__path__``
    grows across reloads, and once it holds two directories for one bundle id
    (a changed ``MOTET_PLUGIN_ROOT``, a reused id, tests sharing a process)
    relative imports resolve against the *oldest* entry while module files load
    by absolute path from the newest — fresh command with a stale helper (#169).
    """
    names = [
        ("bundle", None),
        (f"bundle.{bundle_id}", None),
        (f"bundle.{bundle_id}.{subpackage}", str(directory)),
    ]
    for name, path in names:
        module = sys.modules.get(name)
        if module is None:
            module = types.ModuleType(name)
            module.__path__ = []  # namespace-style package
            sys.modules[name] = module
        if not hasattr(module, "__path__"):
            module.__path__ = []  # type: ignore[attr-defined]
        if path:
            # Authoritative, not additive — see docstring.
            module.__path__ = [path]  # type: ignore[attr-defined]


def _purge_bundle_modules(bundle_id: str, subpackage: Optional[str] = None) -> None:
    """Drop stale ``bundle.<id>.*`` modules before a (re)load.

    **The package parents must go too.** Purging only children (a prefix with a
    trailing dot) leaves the ``bundle.<id>.<sub>`` package object in
    ``sys.modules``, and importing a submodule binds it as an *attribute* on that
    parent. ``from . import _helpers`` resolves through ``getattr(parent,
    "_helpers")`` before it ever consults ``sys.modules`` or the filesystem, so
    the stale attribute wins and a redeployed command runs against the previous
    helper — the ``TypeError`` about a new keyword argument in #169. Dropping the
    parent forces a real import on the next load.

    The shared ``bundle`` root is left alone — other bundles hang off it.
    """
    if subpackage is None:
        prefix = f"bundle.{bundle_id}."
        exact = f"bundle.{bundle_id}"
    else:
        prefix = f"bundle.{bundle_id}.{subpackage}."
        exact = f"bundle.{bundle_id}.{subpackage}"
    for key in [k for k in sys.modules if k == exact or k.startswith(prefix)]:
        del sys.modules[key]


def _load_bundle_commands(
    bundle_id: str,
    commands_dir: Path,
    targeting: Any,
) -> List[str]:
    """
    Dynamically import all .py files in commands/ and register any
    @motet.command functions under 'bundle_id.command_name'.

    Bundles may use either 'from motet_sdk import ...' or
    'from motet.core.commands.decorator import ...';
    the motet_sdk bridge is injected so SDK-style imports get the runtime implementation.

    Shared underscore modules (e.g. ``_helpers.py``) are not loaded as command
    files, but the ``bundle.<id>.commands`` package is registered with a real
    ``__path__`` so command modules can import them relatively
    (``from . import _helpers``). ``_load_bundle`` purges stale modules from a
    prior load before calling this, so a redeploy always executes fresh code.

    Returns the list of namespaced command types that were successfully registered.
    """
    from motet.core.commands.command_type_registry import (
        command_type_registry,
        CommandImplementationType,
    )
    from motet.core.commands.command_data_registry import register_command_data

    registered: List[str] = []
    prefix = f"{bundle_id}."

    _ensure_bundle_package_hierarchy(bundle_id, "commands", commands_dir)

    for py_file in sorted(commands_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"bundle.{bundle_id}.commands.{py_file.stem}"
        try:
            commands_before = set(command_type_registry.get_command_types())
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            # Set bundle namespace while executing command modules so @motet.command
            # generates final namespaced types at decoration time (e.g. calculator.calculate).
            with bundle_command_namespace(bundle_id):
                spec.loader.exec_module(module)  # type: ignore[union-attr]

            # Register all decorated commands found in the module under namespaced types.
            for attr_name in dir(module):
                attr = getattr(module, attr_name, None)
                if attr is None:
                    continue
                # Decorated commands expose __command_type__ and __command_class__
                if not (callable(attr) and hasattr(attr, "__command_type__") and hasattr(attr, "__command_class__")):
                    continue
                raw_command_type = getattr(attr, "__command_type__", None)
                command_class = getattr(attr, "__command_class__", None)
                if not isinstance(raw_command_type, str) or command_class is None:
                    continue
                if isinstance(raw_command_type, str) and raw_command_type.startswith(f"{bundle_id}."):
                    namespaced_type = raw_command_type
                else:
                    namespaced_type = f"{bundle_id}.{raw_command_type}"
                try:
                    # Pull data_class from the existing bare-name registry entry if available
                    bare_reg = command_type_registry.get(raw_command_type)
                    data_class = bare_reg.data_class if bare_reg else None

                    metadata: Dict[str, Any] = {}
                    if targeting is not None:
                        try:
                            metadata["targeting"] = (
                                targeting.model_dump()
                                if hasattr(targeting, "model_dump")
                                else targeting
                            )
                        except Exception:
                            pass  # metadata extraction is best-effort for registration
                    command_type_registry.register_command(
                        command_type=namespaced_type,
                        implementation=command_class,
                        implementation_type=CommandImplementationType.DECORATOR_BASED,
                        data_class=data_class,
                        # Preserve discovery prose from the bare-name registration when
                        # namespacing; register_command will re-derive if empty (#194).
                        description=(bare_reg.description if bare_reg else None),
                        metadata=metadata or None,
                        bundle_id=bundle_id,
                        hot_loadable=True,
                        overwrite=True,
                    )
                    if data_class is not None:
                        register_command_data(namespaced_type, data_class, overwrite=True)
                    # Remove legacy bare-name registration if present.
                    if raw_command_type != namespaced_type:
                        command_type_registry.unregister(raw_command_type)
                        # Keep wrapper metadata aligned with final registry key.
                        setattr(attr, "__command_type__", namespaced_type)
                    registered.append(namespaced_type)
                    logger.info(
                        "load_bundle_command_registered",
                        bundle_id=bundle_id,
                        command_type=namespaced_type,
                    )
                except Exception as e:
                    logger.warning(
                        "load_bundle_command_register_failed",
                        command_type=namespaced_type,
                        error=str(e),
                    )

            # Hard guard: bundle command modules must not register non-namespaced command types.
            commands_after = set(command_type_registry.get_command_types())
            new_command_types = commands_after - commands_before
            unexpected = sorted(ct for ct in new_command_types if not ct.startswith(prefix))
            if unexpected:
                for command_type in unexpected:
                    command_type_registry.unregister(command_type)
                raise RuntimeError(
                    f"Command file '{py_file.name}' attempted to register non-namespaced command types: "
                    f"{unexpected}. Commands must use '{prefix}*' names."
                )
        except Exception as e:
            logger.error(
                "load_bundle_command_import_failed",
                bundle_id=bundle_id,
                file=str(py_file),
                error=str(e),
                exc_info=True,
            )
            raise RuntimeError(f"Failed to import command file '{py_file.name}': {e}") from e

    return registered


def _load_bundle_tools(
    bundle_id: str,
    tools_dir: Path,
    targeting: Any,
) -> List[str]:
    """
    Import tool definition files from tools/ and register under 'bundle_id.tool_name'.
    Tool files may define tools via the tool registry's register() API.

    Like commands, ``tools/`` is registered as a real package so tool modules can
    import shared underscore modules relatively (``from . import _helpers``).

    Returns list of namespaced tool names that were registered.
    """
    try:
        from motet.core.tools import registry as tool_registry
    except ImportError:
        logger.warning("load_bundle_tools_no_registry", bundle_id=bundle_id)
        return []

    _ensure_bundle_package_hierarchy(bundle_id, "tools", tools_dir)

    prefix = f"{bundle_id}."
    registered: set[str] = set()

    def _expected_tool_names_from_file(py_path: Path) -> Set[str]:
        """
        Parse a tool module and return expected namespaced tool IDs it defines.

        Supports:
        - @motet.tool(name="...") / @motet.tool()
        - @tool / @register_tool / @motet_tool
        - registry.register("bundle_id.tool_name", ...)
        """
        expected: Set[str] = set()
        try:
            import ast

            content = py_path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(py_path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    matched = False
                    explicit_name: Optional[str] = None
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Name) and dec.id in ("tool", "register_tool", "motet_tool"):
                            matched = True
                            break
                        if isinstance(dec, ast.Attribute):
                            if dec.attr == "tool" and getattr(dec.value, "id", None) == "motet":
                                matched = True
                                break
                        if isinstance(dec, ast.Call):
                            func = dec.func
                            if isinstance(func, ast.Name) and func.id in ("tool", "register_tool", "motet_tool"):
                                matched = True
                            elif isinstance(func, ast.Attribute):
                                if func.attr == "tool" and getattr(func.value, "id", None) == "motet":
                                    matched = True
                            if matched:
                                for kw in dec.keywords or []:
                                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                        if isinstance(kw.value.value, str) and kw.value.value.strip():
                                            explicit_name = kw.value.value.strip()
                                break
                    if matched:
                        tool_name = explicit_name or node.name
                        expected.add(f"{bundle_id}.{tool_name}")
                elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    call = node.value
                    func = call.func
                    is_register = (
                        isinstance(func, ast.Attribute) and func.attr == "register"
                    ) or (
                        isinstance(func, ast.Name) and func.id == "register"
                    )
                    if is_register and call.args and isinstance(call.args[0], ast.Constant):
                        value = call.args[0].value
                        if isinstance(value, str) and value.startswith(prefix):
                            expected.add(value)
        except Exception:
            return set()
        return expected

    for py_file in sorted(tools_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"bundle.{bundle_id}.tools.{py_file.stem}"
        try:
            if hasattr(tool_registry, "get_all_tool_names"):
                tools_before: set[str] = set(tool_registry.get_all_tool_names())
            elif hasattr(tool_registry, "list_items"):
                tools_before = set(tool_registry.list_items().keys())
            else:
                tools_before = set()

            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            with bundle_tool_namespace(bundle_id):
                spec.loader.exec_module(module)  # type: ignore[union-attr]

            if hasattr(tool_registry, "get_all_tool_names"):
                tools_after: set[str] = set(tool_registry.get_all_tool_names())
            elif hasattr(tool_registry, "list_items"):
                tools_after = set(tool_registry.list_items().keys())
            else:
                tools_after = set()

            new_tools = tools_after - tools_before
            unexpected = sorted(tool_name for tool_name in new_tools if not tool_name.startswith(prefix))
            if unexpected:
                for tool_name in unexpected:
                    tool_registry.unregister(tool_name)
                raise RuntimeError(
                    f"Tool file '{py_file.name}' attempted to register non-namespaced tools: "
                    f"{unexpected}. Tools must use '{prefix}*' names."
                )

            expected_tools = _expected_tool_names_from_file(py_file)
            if expected_tools:
                # Include both new and pre-existing tools declared by this module.
                # This keeps loaded["tools"] stable across redeploys so prune/index steps
                # don't treat unchanged tools as missing.
                registered.update(tool_name for tool_name in expected_tools if tool_name in tools_after)
            else:
                registered.update(tool_name for tool_name in new_tools if tool_name.startswith(prefix))
            logger.info("load_bundle_tool_module_loaded", bundle_id=bundle_id, file=py_file.name)
        except Exception as e:
            logger.error(
                "load_bundle_tool_import_failed",
                bundle_id=bundle_id,
                file=str(py_file),
                error=str(e),
                exc_info=True,
            )
            raise RuntimeError(f"Failed to import tool file '{py_file.name}': {e}") from e

    return sorted(registered)


def _load_bundle_routing(bundle_id: str, routing_dir: Path) -> None:
    """Import routing/strategy modules (best-effort; errors are warnings).

    Registered as a package so routing modules can import shared underscore
    modules relatively, matching commands and tools.
    """
    _ensure_bundle_package_hierarchy(bundle_id, "routing", routing_dir)

    for py_file in sorted(routing_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        module_name = f"bundle.{bundle_id}.routing.{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            logger.info("load_bundle_routing_module_loaded", bundle_id=bundle_id, file=py_file.name)
        except Exception as e:
            logger.warning("load_bundle_routing_import_failed", bundle_id=bundle_id, file=py_file.name, error=str(e))


def _load_bundle_workflows(bundle_id: str, workflows_dir: Path) -> List[str]:
    """
    Load all .yaml files from workflows/ and register them with WorkflowRegistry.

    Each workflow_id is namespaced as '{bundle_id}.{workflow_id}' to prevent
    collisions between bundles and built-in workflows.

    Returns the list of namespaced workflow IDs that were successfully registered.
    """
    try:
        import yaml  # type: ignore[import]
        from motet.core.workflow import Workflow, WorkflowRegistry
    except Exception as e:
        logger.warning("load_bundle_workflows_import_failed", bundle_id=bundle_id, error=str(e))
        return []

    registered: List[str] = []

    for yaml_file in sorted(workflows_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(yaml_file.read_text()) or {}

            # Namespace the workflow_id: bundle_id.original_id
            original_id = raw.get("workflow_id") or yaml_file.stem
            namespaced_id = f"{bundle_id}.{original_id}"
            raw["workflow_id"] = namespaced_id

            workflow = Workflow.from_dict(raw)
            WorkflowRegistry.register(workflow)
            registered.append(namespaced_id)
            logger.info(
                "load_bundle_workflow_registered",
                bundle_id=bundle_id,
                workflow_id=namespaced_id,
                file=yaml_file.name,
            )
        except Exception as e:
            logger.warning(
                "load_bundle_workflow_failed",
                bundle_id=bundle_id,
                file=yaml_file.name,
                error=str(e),
            )

    return registered


def _merge_model_spec(bundle_id: str, model_spec_path: Path) -> None:
    """Merge bundle model spec into the worker's model spec (add-only for V1)."""
    try:
        import yaml  # type: ignore[import]
        spec_data = yaml.safe_load(model_spec_path.read_text()) or {}
        # Try to use the model profile registry if available (optional integration)
        try:
            from motet.core.models.profile_registry import get_model_profile_registry
            registry = get_model_profile_registry()
            for profile_name, profile_data in spec_data.get("profiles", {}).items():
                namespaced = f"{bundle_id}.{profile_name}"
                registry.register_profile(namespaced, profile_data)
                logger.info("load_bundle_model_spec_merged", bundle_id=bundle_id, profile=namespaced)
        except Exception as e:
            logger.warning("load_bundle_model_spec_merge_failed", bundle_id=bundle_id, error=str(e))
    except Exception as e:
        logger.warning("load_bundle_model_spec_parse_failed", bundle_id=bundle_id, path=str(model_spec_path), error=str(e))


def _merge_mcp_config(bundle_id: str, mcp_config_path: Path) -> None:
    """Enqueue MCP register commands to the sibling manager (not in-process)."""
    try:
        import yaml  # type: ignore[import]
        mcp_data = yaml.safe_load(mcp_config_path.read_text()) or {}
        from motet.core.tools.mcp_motet.manager.control_commands import (
            enqueue_mcp_control_command,
            resolve_mcp_manager_id,
        )

        manager_id = resolve_mcp_manager_id()
        if not manager_id:
            logger.warning(
                "load_bundle_mcp_config_no_manager_id",
                bundle_id=bundle_id,
                note="Set MOTET_MCP_MANAGER_ID so bundle MCP config can reach the sibling manager",
            )
            return

        servers = mcp_data.get("servers")
        if isinstance(servers, dict):
            items = list(servers.items())
        else:
            items = []
            for entry in mcp_data.get("services") or []:
                if isinstance(entry, dict) and entry.get("service_id"):
                    items.append((entry["service_id"], entry))

        for server_id, server_conf in items:
            if not isinstance(server_conf, dict):
                continue
            namespaced_id = f"{bundle_id}.{server_id}"
            enqueue_mcp_control_command(
                manager_id,
                {
                    "op": "register",
                    "service_id": namespaced_id,
                    "config": server_conf,
                },
            )
            logger.info(
                "load_bundle_mcp_config_enqueued",
                bundle_id=bundle_id,
                server_id=namespaced_id,
                manager_id=manager_id,
            )
    except Exception as e:
        logger.warning(
            "load_bundle_mcp_config_merge_failed",
            bundle_id=bundle_id,
            error=str(e),
            exc_info=True,
        )


def _unregister_bundle_mcp(bundle_id: str, bundle_dir: Path) -> None:
    """Enqueue unregister for MCP services listed in the bundle mcp.yaml."""
    try:
        import yaml  # type: ignore[import]
        from motet.core.tools.mcp_motet.manager.control_commands import (
            enqueue_mcp_control_command,
            resolve_mcp_manager_id,
        )

        manager_id = resolve_mcp_manager_id()
        if not manager_id:
            return
        paths = [bundle_dir / "config" / "mcp.yaml", bundle_dir / "mcp" / "mcp.yaml"]
        mcp_data: Dict[str, Any] = {}
        for mcp_path in paths:
            if mcp_path.exists():
                mcp_data = yaml.safe_load(mcp_path.read_text()) or {}
                break
        servers = mcp_data.get("servers")
        ids: List[str] = []
        if isinstance(servers, dict):
            ids = [f"{bundle_id}.{sid}" for sid in servers.keys()]
        for entry in mcp_data.get("services") or []:
            if isinstance(entry, dict) and entry.get("service_id"):
                ids.append(f"{bundle_id}.{entry['service_id']}")
        for service_id in ids:
            enqueue_mcp_control_command(
                manager_id,
                {"op": "unregister", "service_id": service_id},
            )
            logger.info(
                "unload_bundle_mcp_enqueued",
                bundle_id=bundle_id,
                service_id=service_id,
                manager_id=manager_id,
            )
    except Exception as e:
        logger.warning(
            "unregister_bundle_mcp_failed",
            bundle_id=bundle_id,
            error=str(e),
        )


def _merge_agents_config(bundle_id: str, agents_config_path: Path) -> List[str]:
    """Merge bundle agents into AgentConfigRegistry under the bundle namespace."""
    try:
        import yaml  # type: ignore[import]
        from motet.core.agents import AgentConfig, get_agent_registry

        raw = yaml.safe_load(agents_config_path.read_text()) or {}
        entries = raw.get("agents") if isinstance(raw, dict) else raw
        if entries is None:
            return []
        if not isinstance(entries, list):
            raise RuntimeError(f"{agents_config_path} must define an 'agents' list or a top-level list")

        registry = get_agent_registry()
        registered: List[str] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise RuntimeError(f"{agents_config_path} entry #{i} must be an object")
            payload = dict(entry)
            payload["bundle_id"] = bundle_id
            config = AgentConfig(**payload)
            registry.register_agent(config)
            qid = f"{bundle_id}.{config.agent_id}"
            registered.append(qid)
            logger.info("load_bundle_agent_registered", bundle_id=bundle_id, qualified_id=qid)

        return sorted(set(registered))
    except Exception as e:
        raise RuntimeError(f"Failed to merge agents config '{agents_config_path.name}': {e}") from e


# ---------------------------------------------------------------------------
# Unload helpers
# ---------------------------------------------------------------------------


def _unregister_bundle_skills(bundle_id: str) -> None:
    """Remove all skill registry entries contributed by this bundle (ADR-0073)."""
    try:
        from motet.core.skills import get_skill_registry

        get_skill_registry().unregister_bundle(bundle_id)
        logger.info("unload_bundle_skills_unregistered", bundle_id=bundle_id)
    except Exception as e:
        logger.warning("unregister_bundle_skills_failed", bundle_id=bundle_id, error=str(e))


def _unregister_bundle_commands(bundle_id: str) -> None:
    """Remove all command_type entries under the bundle namespace (ADR-0079 / #61)."""
    try:
        from motet.core.commands.command_type_registry import command_type_registry

        removed = command_type_registry.unregister_namespace(bundle_id)
        for ct in removed:
            logger.info("unload_bundle_command_unregistered", command_type=ct)
    except Exception as e:
        logger.warning("unregister_bundle_commands_failed", bundle_id=bundle_id, error=str(e))

    # Also clean up sys.modules entries for this bundle (including the
    # bundle.<id> package parent registered for relative imports).
    module_prefix = f"bundle.{bundle_id}."
    to_remove_mods = [k for k in sys.modules if k.startswith(module_prefix)]
    for mod_key in to_remove_mods:
        del sys.modules[mod_key]
    sys.modules.pop(f"bundle.{bundle_id}", None)


def _unregister_bundle_tools(bundle_id: str) -> None:
    """Remove all tool entries prefixed with '{bundle_id}.' from the tool registry."""
    try:
        from motet.core.tools import registry as tool_registry
        prefix = f"{bundle_id}."
        if hasattr(tool_registry, "get_all_tool_names"):
            all_tools = tool_registry.get_all_tool_names()
        elif hasattr(tool_registry, "list_items"):
            all_tools = list(tool_registry.list_items().keys())
        else:
            return
        for tool_name in all_tools:
            if tool_name.startswith(prefix):
                try:
                    tool_registry.unregister(tool_name)
                    logger.info("unload_bundle_tool_unregistered", tool_name=tool_name)
                except Exception as e:
                    logger.warning("unload_bundle_tool_unregister_failed", tool_name=tool_name, error=str(e))
    except Exception as e:
        logger.warning("unregister_bundle_tools_failed", bundle_id=bundle_id, error=str(e))

    # Clean up tool module entries (covered by the shared bundle.{bundle_id}. prefix
    # in _unregister_bundle_commands, but guard here too for standalone calls)
    module_prefix = f"bundle.{bundle_id}.tools."
    to_remove = [k for k in sys.modules if k.startswith(module_prefix)]
    for mod_key in to_remove:
        del sys.modules[mod_key]


def _unregister_bundle_workflows(bundle_id: str) -> None:
    """Remove all WorkflowRegistry entries prefixed with '{bundle_id}.'."""
    try:
        from motet.core.workflow import WorkflowRegistry
        prefix = f"{bundle_id}."
        to_remove = [
            wf.workflow_id
            for wf in WorkflowRegistry.list_all()
            if wf.workflow_id.startswith(prefix)
        ]
        for wf_id in to_remove:
            try:
                WorkflowRegistry.unregister(wf_id)
                logger.info("unload_bundle_workflow_unregistered", workflow_id=wf_id)
            except Exception as e:
                logger.warning("unload_bundle_workflow_unregister_failed", workflow_id=wf_id, error=str(e))
    except Exception as e:
        logger.warning("unregister_bundle_workflows_failed", bundle_id=bundle_id, error=str(e))


def _unregister_bundle_model_spec(bundle_id: str) -> None:
    """Remove model spec entries contributed by this bundle."""
    try:
        from motet.core.models.profile_registry import get_model_profile_registry
        registry = get_model_profile_registry()
        prefix = f"{bundle_id}."
        if hasattr(registry, "get_all_profile_names"):
            for profile_name in registry.get_all_profile_names():
                if profile_name.startswith(prefix):
                    try:
                        registry.unregister_profile(profile_name)
                    except Exception:
                        pass  # best-effort unregister during bundle teardown
    except Exception as e:
        logger.warning("unregister_bundle_model_spec_failed", bundle_id=bundle_id, error=str(e))


def _unregister_bundle_agents(bundle_id: str) -> None:
    """Remove all agent configs contributed by this bundle from AgentConfigRegistry."""
    try:
        from motet.core.agents import get_agent_registry

        removed = get_agent_registry().unregister_bundle(bundle_id)
        for qid in removed:
            logger.info("unload_bundle_agent_unregistered", bundle_id=bundle_id, qualified_id=qid)
    except Exception as e:
        logger.warning("unregister_bundle_agents_failed", bundle_id=bundle_id, error=str(e))


# ---------------------------------------------------------------------------
# Search index refresh (ADR-0071: command/tool/workflow search index on deploy/reload)
# ---------------------------------------------------------------------------


def _refresh_search_index(
    bundle_id: Optional[str] = None,
    loaded: Optional[Dict[str, List[str]]] = None,
) -> None:
    """
    Rebuild the function discovery vector index so bundle-contributed tools,
    workflows, and commands are included in semantic search (ADR-0071).

    Obtains the vector store and registries from motet (when running as
    core.reload_bundle / core.unload_bundle) or from the worker context cache
    (when running from load_bundles_on_startup). Performs a full re-index
    (force_reindex=True) so the index reflects current registry state.
    """
    try:
        store = None
        tool_registry = None
        # Prefer motet context when running as a distributed command
        try:
            motet = get_motet_context()
            store = getattr(motet, "function_discovery_store", None)
            tool_registry = getattr(motet, "tools", None)
        except Exception:
            motet = None
        # Fall back to worker context cache (e.g. load_bundles_on_startup)
        if (store is None or tool_registry is None):
            try:
                from motet.core.workers.tasks import _worker_context_cache
                ctx = _worker_context_cache.get(os.getpid(), {})
                store = store or ctx.get("function_discovery_store")
                tool_registry = tool_registry or ctx.get("tool_registry")
            except Exception:
                pass  # worker context fallback best-effort; skip index refresh if unavailable
        if not store or not tool_registry:
            logger.warning(
                "refresh_search_index_skipped",
                reason="store or tool_registry not available",
                has_store=store is not None,
                has_tool_registry=tool_registry is not None,
            )
            return
        from motet.core.workflow import WorkflowRegistry
        if (
            bundle_id
            and loaded is not None
            and hasattr(store, "sync_bundle_entries")
        ):
            try:
                stats = store.sync_bundle_entries(
                    bundle_id=bundle_id,
                    tool_names=list(loaded.get("tools", [])),
                    workflow_ids=list(loaded.get("workflows", [])),
                    command_types=list(loaded.get("commands", [])),
                    tool_registry=tool_registry,
                    workflow_registry=WorkflowRegistry,
                )
                logger.debug(
                    "refresh_search_index_incremental_complete",
                    bundle_id=bundle_id,
                    removed=stats.get("removed", 0),
                    added=stats.get("added", 0),
                )
                return
            except Exception as e:
                logger.warning(
                    "refresh_search_index_incremental_failed_fallback_full",
                    bundle_id=bundle_id,
                    error=str(e),
                    exc_info=True,
                )

        if hasattr(store, "ensure_shared_index"):
            # Full reindex is destructive — it drops the shared index and
            # repopulates it from this worker's registry. Serialize it on the
            # writer lock so it cannot land on top of another worker's rebuild
            # or its in-flight incremental updates (#156).
            from motet.core.config import Config
            from motet.core.distributed.redis_manager import acquire_distributed_lock_sync

            cfg = Config()
            lock_key = getattr(
                cfg, "function_discovery_writer_lock_key", "motet:function_discovery:index_writer"
            )
            lock_ttl = int(getattr(cfg, "function_discovery_writer_lock_ttl_seconds", 120) or 120)
            outcome = store.ensure_shared_index(
                tool_registry,
                WorkflowRegistry,
                lock_factory=lambda: acquire_distributed_lock_sync(
                    "function_discovery_bundle_reload", lock_key, ttl_seconds=lock_ttl
                ),
                include_commands=True,
                force_reindex=True,
                wait_timeout_seconds=float(
                    getattr(cfg, "function_discovery_index_wait_seconds", 180) or 180
                ),
            )
            logger.debug("refresh_search_index_complete", outcome=outcome)
        elif hasattr(store, "index_tools_and_workflows"):
            store.index_tools_and_workflows(
                tool_registry,
                WorkflowRegistry,
                force_reindex=True,
                include_commands=True,
            )
            logger.debug("refresh_search_index_complete")
        elif hasattr(store, "reindex"):
            store.reindex()
            logger.debug("refresh_search_index_reindex_complete")
    except Exception as e:
        logger.warning("refresh_search_index_failed", error=str(e), exc_info=True)


# ---------------------------------------------------------------------------
# Rollback helper
# ---------------------------------------------------------------------------


def _rollback_bundle_dir(bundle_id: str, bundle_dir: Path, backup_dir: Path) -> None:
    """Restore previous bundle dir from backup on reload failure."""
    try:
        if bundle_dir.exists():
            _remove_tree_if_present(bundle_dir)
        if backup_dir.exists():
            shutil.copytree(backup_dir, bundle_dir)
            _remove_tree_if_present(backup_dir)
            logger.info("reload_bundle_rollback_complete", bundle_id=bundle_id)
        else:
            logger.info("reload_bundle_rollback_no_backup", bundle_id=bundle_id)
    except Exception as e:
        logger.error("reload_bundle_rollback_failed", bundle_id=bundle_id, error=str(e))


def _remove_tree_if_present(path: Path) -> None:
    """Remove a directory tree, tolerating concurrent or already-complete removal."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        logger.info("bundle_tree_already_removed", path=str(path))


# ---------------------------------------------------------------------------
# Startup catch-up
# ---------------------------------------------------------------------------


def load_bundles_on_startup() -> int:
    """
    Query the bundle registry on worker startup and load any bundles that are
    targeted at this worker but not yet loaded at the current bundle_version.

    Obtains its own Redis client so the caller (worker_initialization.py) does not
    need to pass one.  Returns the number of bundles successfully loaded.

    Called from worker_initialization.py on worker start.
    """
    try:
        from motet.core.distributed.redis_manager import get_sync_redis_client
        from motet.core.bundles.deploy import (
            _list_all_bundles,
            _fetch_artifact,
            _unpack_artifact,
            BundleTargeting,
        )
        from motet.core.workers.worker_utils import get_worker_id
        redis_client = get_sync_redis_client()
        worker_id = get_worker_id()
    except Exception as e:
        logger.warning("load_bundles_on_startup_init_failed", error=str(e))
        return 0

    try:
        all_bundles = _list_all_bundles(redis_client)
    except Exception as e:
        logger.warning("load_bundles_on_startup_list_failed", error=str(e))
        return 0

    loaded_count = 0
    for entry in all_bundles:
        bundle_id = entry.get("bundle_id", "")
        bundle_version = entry.get("bundle_version", "")
        if not bundle_id or not bundle_version:
            continue

        bundle_dir = PLUGIN_ROOT / bundle_id
        # Check if already at current version
        version_marker = bundle_dir / ".bundle_version"
        if version_marker.exists():
            loaded_version = version_marker.read_text().strip()
            if loaded_version == bundle_version:
                # Already unpacked at the right version; re-register commands in case the
                # worker restarted and lost its in-memory registry entries.
                try:
                    loaded = _load_bundle(bundle_id, bundle_dir, None, bundle_version)
                    _prune_stale_bundle_registrations(bundle_id, loaded)
                    _refresh_search_index(bundle_id=bundle_id, loaded=loaded)
                    logger.info("load_bundles_on_startup_reregistered", bundle_id=bundle_id, bundle_version=bundle_version)
                    loaded_count += 1
                except Exception as e:
                    logger.warning("load_bundles_on_startup_reregister_failed", bundle_id=bundle_id, error=str(e))
                continue

        # Targeting check (skip if this worker is not a target)
        targeting_raw = entry.get("targeting")
        targeting: Optional[BundleTargeting] = None
        if targeting_raw:
            try:
                targeting_dict = json.loads(targeting_raw) if isinstance(targeting_raw, str) else targeting_raw
                targeting = BundleTargeting(**targeting_dict)
                if targeting.worker_ids and worker_id not in targeting.worker_ids:
                    continue
            except Exception:
                pass  # targeting parse best-effort; skip entry

        targeting_dump = targeting.model_dump() if targeting else None
        mode = entry.get("mode") or ""
        fingerprint = entry.get("source_fingerprint") or ""
        if isinstance(fingerprint, bytes):
            fingerprint = fingerprint.decode()
        is_hot = mode == "hot" or (
            isinstance(fingerprint, str) and fingerprint.startswith("hot:")
        )

        # Hot deploys never write a Redis artifact. Prefer copying from the
        # ``hot:<path>`` fingerprint when that tree is still mounted; otherwise
        # re-register from whatever remains under PLUGIN_ROOT (#125).
        if is_hot:
            try:
                source_path: Optional[Path] = None
                if isinstance(fingerprint, str) and fingerprint.startswith("hot:"):
                    candidate = Path(fingerprint[len("hot:") :]).expanduser()
                    if candidate.exists() and candidate.is_dir():
                        source_path = candidate
                loaded_from = "plugin_root"
                if source_path is not None:
                    if bundle_dir.exists():
                        _remove_tree_if_present(bundle_dir)
                    shutil.copytree(source_path, bundle_dir)
                    loaded_from = "hot_source"
                if not bundle_dir.exists():
                    logger.warning(
                        "load_bundles_on_startup_hot_source_missing",
                        bundle_id=bundle_id,
                        bundle_version=bundle_version,
                        source_fingerprint=fingerprint,
                    )
                    continue
                loaded = _load_bundle(bundle_id, bundle_dir, targeting_dump, bundle_version)
                _prune_stale_bundle_registrations(bundle_id, loaded)
                version_marker.write_text(bundle_version)
                _refresh_search_index(bundle_id=bundle_id, loaded=loaded)
                logger.info(
                    "load_bundles_on_startup_hot_loaded",
                    bundle_id=bundle_id,
                    bundle_version=bundle_version,
                    loaded_from=loaded_from,
                )
                loaded_count += 1
            except Exception as e:
                logger.error(
                    "load_bundles_on_startup_hot_load_failed",
                    bundle_id=bundle_id,
                    bundle_version=bundle_version,
                    error=str(e),
                    exc_info=True,
                )
            continue

        # Artifact-backed load
        try:
            artifact_bytes = _fetch_artifact(redis_client, bundle_id, bundle_version)
            if not artifact_bytes:
                logger.warning("load_bundles_on_startup_artifact_missing", bundle_id=bundle_id, bundle_version=bundle_version)
                continue
            if bundle_dir.exists():
                _remove_tree_if_present(bundle_dir)
            _unpack_artifact(artifact_bytes, bundle_dir)
            loaded = _load_bundle(bundle_id, bundle_dir, targeting_dump, bundle_version)
            _prune_stale_bundle_registrations(bundle_id, loaded)
            # Write version marker
            version_marker.write_text(bundle_version)
            _refresh_search_index(bundle_id=bundle_id, loaded=loaded)
            logger.info("load_bundles_on_startup_loaded", bundle_id=bundle_id, bundle_version=bundle_version)
            loaded_count += 1
        except Exception as e:
            logger.error(
                "load_bundles_on_startup_load_failed",
                bundle_id=bundle_id,
                bundle_version=bundle_version,
                error=str(e),
                exc_info=True,
            )

    return loaded_count
