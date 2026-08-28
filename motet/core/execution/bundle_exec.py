"""
Motet - Bundle exec catalog merge (Phase 3)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-07

Description:
    When ``core.worker_exec`` runs with the Docker backend and an optional ``bundle_id``,
    merge ``oci_image_ref`` / ``exec_artifact_digest`` from the published Redis bundle
    catalog (``config/exec.yaml`` at deploy time) if the request omits them.

Dependencies:
    - motet.core.execution.models (ExecutionRequest)
    - motet.core.bundles.deploy._get_catalog

Usage:
    req = ExecutionRequest(argv=[\"python\", \"-V\"], cwd=\"/work\", bundle_id=\"acme.tools\")
    req = merge_exec_catalog_into_request(req, redis_client=redis)

Notes:
    - CI/publish-time OCI builds remain future work; authors may pin ``oci_image_ref``
      in ``config/exec.yaml`` so workers pull a known image.
"""

from __future__ import annotations

from typing import Any

from .models import ExecutionRequest


def merge_exec_catalog_into_request(
    request: ExecutionRequest,
    *,
    redis_client: Any,
) -> ExecutionRequest:
    """Fill missing exec fields from ``bundle:{bundle_id}:catalog`` ``exec`` block."""
    bid = (request.bundle_id or "").strip()
    if not bid:
        return request

    from motet.core.bundles.deploy import _get_catalog

    cat = _get_catalog(redis_client, bid)
    if not cat:
        return request

    block = cat.get("exec")
    if not isinstance(block, dict):
        return request

    updates: dict[str, Any] = {}
    if not request.oci_image_ref:
        ref = block.get("oci_image_ref")
        if isinstance(ref, str) and ref.strip():
            updates["oci_image_ref"] = ref.strip()
    if not request.exec_artifact_digest:
        dig = block.get("exec_artifact_digest")
        if isinstance(dig, str) and dig.strip():
            updates["exec_artifact_digest"] = dig.strip()

    if not updates:
        return request
    return request.model_copy(update=updates)


__all__ = ["merge_exec_catalog_into_request"]
