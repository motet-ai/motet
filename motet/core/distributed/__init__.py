"""
Motet - Distributed Package Exports

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Public exports for distributed services.

    IMPORTANT:
        Keep this module lightweight. Importing heavy submodules here can easily create circular
        imports during Celery worker startup. Prefer importing implementation modules directly
        at the call site when needed.

Dependencies:
    - distributed service modules (worker readiness, lifecycle, monitoring, redis utilities)

Usage:
    from motet.core.distributed import get_sync_binary_redis_client

Notes:
    - Load commands through the bundle deploy pipeline.
"""


from typing import Any
import importlib
__all__ = [
    'WorkerReadinessService',
    'WorkerState', 
    'WorkerInfo',
    'get_readiness_service',
    'WorkerLifecycleService',
    'TerminationReason',
    'TerminationMethod',
    'WorkerHealthMetrics',
    'get_lifecycle_service',
    'ManagerStatusRegistry',
    'ManagerType',
    'ManagerStatus',
    'RedisCommandDataManager',
    'get_redis_command_data_manager',
    'get_binary_redis_client',
    'get_sync_binary_redis_client',
]

_LAZY_IMPORTS = {
    "WorkerReadinessService": ("motet.core.distributed.worker_readiness", "WorkerReadinessService"),
    "WorkerState": ("motet.core.distributed.worker_readiness", "WorkerState"),
    "WorkerInfo": ("motet.core.distributed.worker_readiness", "WorkerInfo"),
    "get_readiness_service": ("motet.core.distributed.worker_readiness", "get_readiness_service"),
    "WorkerLifecycleService": ("motet.core.distributed.worker_lifecycle", "WorkerLifecycleService"),
    "TerminationReason": ("motet.core.distributed.worker_lifecycle", "TerminationReason"),
    "TerminationMethod": ("motet.core.distributed.worker_lifecycle", "TerminationMethod"),
    "WorkerHealthMetrics": ("motet.core.distributed.worker_lifecycle", "WorkerHealthMetrics"),
    "get_lifecycle_service": ("motet.core.distributed.worker_lifecycle", "get_lifecycle_service"),
    "ManagerStatusRegistry": ("motet.core.distributed.manager_status", "ManagerStatusRegistry"),
    "ManagerType": ("motet.core.distributed.manager_status", "ManagerType"),
    "ManagerStatus": ("motet.core.distributed.manager_status", "ManagerStatus"),
    "RedisCommandDataManager": ("motet.core.distributed.redis_command_data_manager", "RedisCommandDataManager"),
    "get_redis_command_data_manager": ("motet.core.distributed.redis_command_data_manager", "get_redis_command_data_manager"),
    "get_binary_redis_client": ("motet.core.distributed.redis_manager", "get_binary_redis_client"),
    "get_sync_binary_redis_client": ("motet.core.distributed.redis_manager", "get_sync_binary_redis_client"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)
    raise AttributeError(f"module 'motet.core.distributed' has no attribute '{name}'")