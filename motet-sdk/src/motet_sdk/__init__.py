"""
Motet Developer Kit (SDK).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Public API for bundle authors: decorator, context protocol, capabilities,
models, manifest validation, concurrency primitives, and test doubles. Use this
package when building bundles that run on a Motet runtime.
"""

from motet_sdk.capabilities import WorkerCapability
from motet_sdk.concurrency import (
    WorkerEvent,
    WorkerExecutor,
    WorkerLocal,
    WorkerLock,
    WorkerRLock,
    WorkerSemaphore,
    WorkerThread,
    worker_sleep,
    run_async_safe,
)
from motet_sdk.command import distributed_command, get_motet_context, resolve_current_identity
from motet_sdk.context import MotetContext
from motet_sdk.manifest import BundleManifest, load_manifest, validate_manifest
from motet_sdk.models import (
    ApplyExecutionError,
    BaseCommandData,
    CommandError,
    CommandExecutionError,
    CommandMetadata,
    GatherExecutionError,
    IdentityContext,
)
from motet_sdk.motet_namespace import motet
from motet_sdk.preparation import ArtifactFeatureMatch, ArtifactPrepManifest
from motet_sdk.testing import MockMotetContext
from motet_sdk._version import get_version

__version__ = get_version()

__all__ = [
    "__version__",
    "distributed_command",
    "get_motet_context",
    "resolve_current_identity",
    "motet",
    "MotetContext",
    "WorkerCapability",
    "BaseCommandData",
    "CommandError",
    "CommandExecutionError",
    "GatherExecutionError",
    "ApplyExecutionError",
    "CommandMetadata",
    "IdentityContext",
    "BundleManifest",
    "ArtifactFeatureMatch",
    "ArtifactPrepManifest",
    "load_manifest",
    "validate_manifest",
    "MockMotetContext",
    # Concurrency (pool-agnostic in the runtime)
    "WorkerLock",
    "WorkerRLock",
    "WorkerEvent",
    "WorkerSemaphore",
    "WorkerLocal",
    "WorkerThread",
    "WorkerExecutor",
    "worker_sleep",
    "run_async_safe",
]
