"""
Motet SDK - Worker capability enum.

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Last Modified: 2026-08-24

Bundle authors use WorkerCapability in @distributed_command(required_capabilities=[...])
to declare which worker capabilities a command needs.
"""

from enum import Enum


class WorkerCapability(str, Enum):
    """Capabilities that workers can provide. Used for command routing."""

    REASONING = "reasoning"
    MODEL_INFERENCE = "model_inference"
    MODEL_STREAMING = "model_streaming"
    # Worker can serve local (on-box) model inference (advertised only when a local model
    # is actually usable). Routing that prefers/falls back on this is a separate follow-on.
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
