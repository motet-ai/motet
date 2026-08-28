"""
Motet - Product Version Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Verifies that runtime, SDK, HTTP, and CLI product version strings
    come from package metadata rather than independent literals.

Dependencies:
    - tomllib: read [project].version from each pyproject.toml
    - pathlib: locate repo and SDK pyproject files
    - motet / motet_sdk: version helpers and public __version__
    - FastAPI create_app: OpenAPI version and license metadata

Usage:
    pytest tests/unit/test_product_version.py -q

Notes:
    - Does not hardcode 0.1.0; it compares live metadata to pyproject.
    - Command-registration and artifact strategy versions are out of scope.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from click.testing import CliRunner

import motet
from motet._version import get_version as get_motet_version
from motet.core.embedding.server.app import app as embedding_app
from motet.interfaces.http import create_app
from motet_sdk import __version__ as sdk_version
from motet_sdk._version import get_version as get_sdk_version
from motet_sdk.cli.main import main_group


def _project_version(pyproject: Path, name: str) -> str:
    with pyproject.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["name"] == name
    return str(project["version"])


def test_motet_version_matches_root_pyproject() -> None:
    expected = _project_version(Path("pyproject.toml"), "motet")
    assert get_motet_version() == expected
    assert motet.__version__ == expected


def test_sdk_version_matches_sdk_pyproject() -> None:
    expected = _project_version(Path("motet-sdk/pyproject.toml"), "motet-sdk")
    assert get_sdk_version() == expected
    assert sdk_version == expected


def test_runtime_and_sdk_versions_are_lockstep() -> None:
    assert motet.__version__ == sdk_version


def test_http_app_reports_product_version_and_fsl_license() -> None:
    app = create_app()
    assert app.version == motet.__version__
    license_info = app.openapi()["info"]["license"]
    assert "Functional Source License" in license_info["name"]
    assert license_info.get("name") != "MIT"


def test_embedding_server_reports_product_version() -> None:
    assert embedding_app.version == motet.__version__


def test_cli_version_option_uses_sdk_package_version() -> None:
    result = CliRunner().invoke(main_group, ["--version"])
    assert result.exit_code == 0
    assert sdk_version in result.output
