"""
Motet SDK - Package Version

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Resolves the motet-sdk package version from installed package
    metadata, falling back to motet-sdk/pyproject.toml when the
    distribution is not installed.

Dependencies:
    - importlib.metadata: installed distribution version
    - tomllib: read [project].version from pyproject.toml
    - pathlib: walk from this file to the matching pyproject.toml

Usage:
    from motet_sdk import __version__
    from motet_sdk._version import get_version

    assert get_version() == __version__

Notes:
    - Canonical SDK version lives in motet-sdk/pyproject.toml
      [project].version. It stays lockstep with the motet runtime
      version until a compatibility matrix is published.
    - motet-cli --version uses this value.
    - The SDK is Apache 2.0 and has no FSL conversion clock.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_DISTRIBUTION = "motet-sdk"


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
