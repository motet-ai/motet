"""
Motet - Local Model Inference

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Local model inference system for running LLMs on local hardware.
    Supports CPU, NVIDIA GPUs, and Apple Silicon (Metal) with automatic
    hardware detection and graceful fallback.

    Public symbols are lazy-loaded so importing submodules (e.g.
    ``motet.core.models.local.reasoning`` for the LocalAdapter path) does not
    pull in ``inference_manager`` and its optional deps (``psutil``, llama.cpp)
    in API/worker images that only need the client or parsing helpers.

Dependencies:
    - Redis: Inter-process communication via streams
    - Optional: vllm (NVIDIA), llama-cpp-python (CPU/Metal), transformers

Usage:
    # In parent process (like MCPInstanceManager)
    from motet.core.models.local import LocalInferenceManager

    manager = LocalInferenceManager()
    await manager.start()

    # In workers
    from motet.core.models.local import LocalInferenceClient

    client = LocalInferenceClient(redis_client)
    response = await client.infer(model="phi-4-mini", messages=[...])

Notes:
    - Manager runs in parent process, manages inference workers
    - Client is lightweight, works on any Celery pool type
    - Pure I/O operation for workers (via Redis Streams)
    - Automatic hardware detection and engine selection
    - Graceful fallback to API inference if local fails
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "LocalInferenceManager",
    "LocalInferenceClient",
    "LocalModelCache",
    "inference_client",
    "inference_manager",
    "model_cache",
    "profiles",
    "reasoning",
]

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "LocalInferenceManager": (
        "motet.core.models.local.inference_manager",
        "LocalInferenceManager",
    ),
    "LocalInferenceClient": (
        "motet.core.models.local.inference_client",
        "LocalInferenceClient",
    ),
    "LocalModelCache": ("motet.core.models.local.model_cache", "LocalModelCache"),
}

_LAZY_SUBMODULES: dict[str, str] = {
    "inference_client": "motet.core.models.local.inference_client",
    "inference_manager": "motet.core.models.local.inference_manager",
    "model_cache": "motet.core.models.local.model_cache",
    "profiles": "motet.core.models.local.profiles",
    "reasoning": "motet.core.models.local.reasoning",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        module_name, attr_name = _LAZY_IMPORTS[name]
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)
    if name in _LAZY_SUBMODULES:
        return importlib.import_module(_LAZY_SUBMODULES[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
