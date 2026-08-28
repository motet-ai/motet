"""
Motet - Runtime Package Version

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Resolves the Motet runtime product version from installed package
    metadata, falling back to the repo-root pyproject.toml when the
    distribution is not installed (editable source checkouts).

Dependencies:
    - importlib.metadata: installed distribution version
    - tomllib: read [project].version from pyproject.toml
    - pathlib: walk from this file to the matching pyproject.toml

Usage:
    from motet._version import get_version
    from motet import __version__

    assert get_version() == __version__

Notes:
    - Canonical product version lives in the root pyproject.toml
      [project].version for the ``motet`` distribution.
    - HTTP, CLI, embedding-server, and ``GET /api/v1/version`` should call
      this helper (or ``motet.__version__``) instead of a second literal.
    - Do not treat this string as an FSL conversion date.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_DISTRIBUTION = "motet"


def get_version(distribution: str = _DISTRIBUTION) -> str:
    """Return the version string for ``distribution``.

    Prefers ``importlib.metadata``. If the package is not installed,
    walks parents of this file for a pyproject.toml whose
    ``[project].name`` matches ``distribution``.
    """
    try:
        return version(distribution)
    except PackageNotFoundError:
        resolved = _version_from_pyproject(distribution, Path(__file__).resolve().parent)
        if resolved is None:
            raise RuntimeError(
                f"Unable to resolve version for {distribution}: package is not "
                "installed and no matching pyproject.toml was found"
            ) from None
        return resolved


def _version_from_pyproject(distribution: str, start: Path) -> str | None:
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if not candidate.is_file():
            continue
        with candidate.open("rb") as handle:
            project = (tomllib.load(handle).get("project") or {})
        name = project.get("name")
        project_version = project.get("version")
        if name == distribution and project_version:
            return str(project_version)
    return None
