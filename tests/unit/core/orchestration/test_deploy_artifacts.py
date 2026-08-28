"""
Motet - Bundle Artifact Packaging Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Unit tests for deploy artifact zip normalization and tar extraction. These
    guard redeploys of persisted bundle roots where vendored archives can
    contain a path placeholder and concrete files below the same path.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

from motet.core.bundles.deploy import (
    _package_artifact,
    _unpack_artifact,
    _zip_to_bundle_files,
)


CONFLICT_PATH = "skills/claude-api/python/agent-sdk"
DESCENDANT_PATH = f"{CONFLICT_PATH}/README.md"


def test_zip_to_bundle_files_drops_prefix_conflict() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.yaml", 'name: "demo"\nversion: "0.1.0"\n')
        zf.writestr(CONFLICT_PATH, b"placeholder")
        zf.writestr(DESCENDANT_PATH, b"nested")

    bundle_files = _zip_to_bundle_files(buf.getvalue())

    assert CONFLICT_PATH not in bundle_files
    assert bundle_files[DESCENDANT_PATH] == b"nested"


def test_package_artifact_drops_prefix_conflict_before_tar_creation(tmp_path: Path) -> None:
    artifact = _package_artifact(
        {
            "manifest.yaml": b'name: "demo"\nversion: "0.1.0"\n',
            CONFLICT_PATH: b"placeholder",
            DESCENDANT_PATH: b"nested",
        }
    )

    dest = tmp_path / "bundle"
    _unpack_artifact(artifact, dest)

    assert not (dest / CONFLICT_PATH).is_file()
    assert (dest / DESCENDANT_PATH).read_bytes() == b"nested"


def test_unpack_artifact_skips_prefix_conflict_in_existing_artifact(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        conflict = tarfile.TarInfo(name=CONFLICT_PATH)
        conflict_payload = b"placeholder"
        conflict.size = len(conflict_payload)
        tar.addfile(conflict, io.BytesIO(conflict_payload))

        nested = tarfile.TarInfo(name=DESCENDANT_PATH)
        nested_payload = b"nested"
        nested.size = len(nested_payload)
        tar.addfile(nested, io.BytesIO(nested_payload))

    dest = tmp_path / "bundle"
    _unpack_artifact(buf.getvalue(), dest)

    assert not (dest / CONFLICT_PATH).is_file()
    assert (dest / DESCENDANT_PATH).read_bytes() == b"nested"
