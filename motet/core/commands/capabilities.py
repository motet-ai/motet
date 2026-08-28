"""
Motet - Worker Capabilities

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
    The `WorkerCapability` vocabulary: what a worker can do, and therefore what
    a command may require. Commands declare `required_capabilities=[...]` and
    the worker router matches those against each worker's advertised set.

    This lives in a leaf module rather than in `distributed.py` because naming a
    capability is the single most common reason unrelated packages import from
    the command layer — reasoning, tools, and workers all do it purely to
    annotate a command, and pulling that vocabulary out of a 2.5k-line dispatch
    module makes the dependency legible. Importing it costs an enum: nothing
    here reaches `distributed`, msgpack, or any concrete command.

    Note that `core.workers` would be the intuitive home, but `distributed.py`
    cannot import from that package at module scope without recreating the
    circular import documented at the top of it.

Dependencies:
    - enum: stdlib only, deliberately. Keep it that way so any layer can import
      this without dragging in orchestration.

Usage:
    from motet.core.commands.capabilities import WorkerCapability

    @motet.command(required_capabilities=[WorkerCapability.TOOL_EXECUTION])
    def my_command(data: MyData, motet: MotetContext) -> Dict[str, Any]:
        ...

Notes:
    - Mirrors `motet_sdk.capabilities`, which declares the same enum for bundle
      authors; the runtime one is injected over it by the bridge in
      `bundle_reload.py`. Add new members to both.
    - Values are the wire form stored in Redis worker-readiness records, so
      renaming a member's *value* invalidates advertised capability sets.
"""

from enum import Enum


class WorkerCapability(str, Enum):
    """Capabilities that workers can provide"""
    REASONING = "reasoning"
    MODEL_INFERENCE = "model_inference"
    MODEL_STREAMING = "model_streaming"
    # Worker can serve local (on-box) model inference via a reachable LocalInferenceManager
    # with at least one usable model. Advertised presence-aware (ADR-0104 Open Q10, ADR-0042);
    # routing preference/fallback that consumes this is a separate follow-on (not yet wired).
    LOCAL_INFERENCE = "local_inference"
    TOOL_EXECUTION = "tool_execution"
    MEMORY_OPERATIONS = "memory_operations"
    MEMORY_STORAGE = "memory_storage"
    MEMORY_RETRIEVAL = "memory_retrieval"
    VECTOR_OPERATIONS = "vector_operations"
    WEB_SEARCH = "web_search"
    HTTP_REQUESTS = "http_requests"
    HTTP_OPERATIONS = "http_operations"
    BROWSER_OPERATIONS = "browser_operations"
    FILE_OPERATIONS = "file_operations"
    MEDIA_PROCESSING = "media_processing"
    EMBEDDINGS = "embeddings"
    TASK_SCHEDULING = "task_scheduling"
    MCP_INTEGRATION = "mcp_integration"
    PROCESS_ISOLATION = "process_isolation"
    CONNECTION_POOLING = "connection_pooling"
    WORKER_LIFECYCLE_MANAGEMENT = "worker_lifecycle_management"
    DEPLOYMENT = "deployment"
    # Edge worker capabilities (ADR-0095)
    EDGE_EXECUTION = "edge_execution"
    EDGE_FILE_READ = "edge_file_read"
    EDGE_FILE_WRITE = "edge_file_write"
    EDGE_FILE_SEARCH = "edge_file_search"
    EDGE_SHELL_EXEC = "edge_shell_exec"
    WORKER_SHELL_EXEC = "worker_shell_exec"
    EDGE_PROCESS_CONTROL = "edge_process_control"
    EDGE_CLIPBOARD = "edge_clipboard"
    EDGE_APP_CONTROL = "edge_app_control"
    EDGE_BROWSER = "edge_browser"


__all__ = ["WorkerCapability"]
