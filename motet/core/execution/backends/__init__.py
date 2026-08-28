"""
Motet - Execution backends

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-07

Description:
    Backend implementations for motet.core.execution.run_execution.
"""

from __future__ import annotations

from .docker import run_docker
from .kata_docker import run_kata_docker
from .subprocess import run_subprocess

__all__ = ["run_docker", "run_kata_docker", "run_subprocess"]
