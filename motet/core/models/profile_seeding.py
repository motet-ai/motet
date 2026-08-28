"""
Motet - Model Profile Seeding

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Optional startup-time seeding of Redis-backed ModelProfiles from a YAML/JSON config file.

    This mirrors the pattern used by MCP services (`config/mcp_instance_manager.yaml`) where Docker
    mounts a config file and services read it at startup to initialize distributed state.

Dependencies:
    - yaml: YAML parsing for config files (already used elsewhere for MCP config)
    - motet.core.models.profiles: ModelProfile persistence helpers
    - motet.core.distributed.redis_manager: Distributed lock helpers for idempotent seeding
    - structlog: Structured logging

Usage:
    # API process (async)
    await seed_model_profiles_if_configured(cfg)

    # Celery worker process (sync)
    seed_model_profiles_if_configured_sync(cfg)

Notes:
    - Seeding is idempotent and guarded by a Redis distributed lock.
    - If overwrite=False, existing profiles are left unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from ..config import Config
from ..distributed.redis_manager import acquire_distributed_lock, acquire_distributed_lock_sync
from .profiles import ModelProfile, load_model_profile, load_model_profile_sync, store_model_profile, store_model_profile_sync

logger = structlog.get_logger(__name__)


def _read_seed_file(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))

    text = p.read_text(encoding="utf-8")

    # Determine parser from extension
    suffix = p.suffix.lower().lstrip(".")
    if suffix in {"yaml", "yml"}:
        import yaml

        return yaml.safe_load(text) or {}
    if suffix == "json":
        return json.loads(text) or {}

    # Default: try YAML first (more forgiving), then JSON.
    try:
        import yaml

        return yaml.safe_load(text) or {}
    except Exception:
        return json.loads(text) or {}


def _parse_profiles(doc: Dict[str, Any]) -> List[ModelProfile]:
    """
    Parse a seed document into ModelProfile objects.

    Supported formats:
      - {"profiles": [<ModelProfile dicts>]}
      - [<ModelProfile dicts>]  (bare list)
    """
    if isinstance(doc, list):
        raw = doc
    else:
        raw = doc.get("profiles") if isinstance(doc, dict) else None

    if not isinstance(raw, list):
        return []

    out: List[ModelProfile] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(ModelProfile.model_validate(entry))
    return out


async def seed_model_profiles_if_configured(cfg: Config) -> Dict[str, Any]:
    """
    Seed model profiles from cfg.model_profile_seed_file if enabled.

    Returns:
        dict status payload for logs/diagnostics.
    """
    if not bool(getattr(cfg, "seed_model_profiles_on_startup", False)):
        return {"status": "skipped", "reason": "seed_model_profiles_on_startup=false"}

    path = getattr(cfg, "model_profile_seed_file", None)
    if not isinstance(path, str) or not path.strip():
        return {"status": "skipped", "reason": "model_profile_seed_file not set"}

    lock = await acquire_distributed_lock("model_profiles", "lock:model_profiles:seed", ttl_seconds=60)
    if not lock:
        return {"status": "skipped", "reason": "seed_lock_not_acquired"}

    try:
        doc = _read_seed_file(path.strip())
        profiles = _parse_profiles(doc)
        if not profiles:
            return {"status": "skipped", "reason": "no_profiles_in_seed_file"}

        overwrite = bool(getattr(cfg, "model_profile_seed_overwrite", False))
        created = 0
        skipped = 0
        for prof in profiles:
            existing = await load_model_profile(tenant_id=prof.tenant_id, profile_name=prof.name)
            if existing is not None and not overwrite:
                skipped += 1
                continue
            await store_model_profile(profile=prof)
            created += 1

        return {"status": "success", "seeded": created, "skipped": skipped, "file": path}

    except Exception as e:
        logger.error("model_profile_seed_failed", error=str(e), file=path, exc_info=True)
        raise
    finally:
        try:
            await lock.release()
        except Exception:
            logger.warning("model_profile_seed_lock_release_failed", exc_info=True)


def seed_model_profiles_if_configured_sync(cfg: Config) -> Dict[str, Any]:
    """
    Synchronous version for Celery workers.
    """
    if not bool(getattr(cfg, "seed_model_profiles_on_startup", False)):
        return {"status": "skipped", "reason": "seed_model_profiles_on_startup=false"}

    path = getattr(cfg, "model_profile_seed_file", None)
    if not isinstance(path, str) or not path.strip():
        return {"status": "skipped", "reason": "model_profile_seed_file not set"}

    lock = acquire_distributed_lock_sync("model_profiles", "lock:model_profiles:seed", ttl_seconds=60)
    if not lock:
        return {"status": "skipped", "reason": "seed_lock_not_acquired"}

    try:
        doc = _read_seed_file(path.strip())
        profiles = _parse_profiles(doc)
        if not profiles:
            return {"status": "skipped", "reason": "no_profiles_in_seed_file"}

        overwrite = bool(getattr(cfg, "model_profile_seed_overwrite", False))
        created = 0
        skipped = 0
        for prof in profiles:
            existing = load_model_profile_sync(tenant_id=prof.tenant_id, profile_name=prof.name)
            if existing is not None and not overwrite:
                skipped += 1
                continue
            store_model_profile_sync(profile=prof)
            created += 1

        return {"status": "success", "seeded": created, "skipped": skipped, "file": path}

    except Exception as e:
        logger.error("model_profile_seed_failed", error=str(e), file=path, exc_info=True)
        raise
    finally:
        try:
            lock.release_sync()
        except Exception:
            logger.warning("model_profile_seed_lock_release_failed", exc_info=True)


__all__ = ["seed_model_profiles_if_configured", "seed_model_profiles_if_configured_sync"]

