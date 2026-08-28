"""
Motet - Artifact Preparation Selector

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Selects artifact preparation strategies using deterministic
    hot-path dispatch. The selector considers explicit caller hints, per-artifact
    disabled strategies, artifact kind, content type, filename extension, and
    registered preparation manifests.

Dependencies:
    - pathlib for extension normalization
    - preparation models and strategy protocol
    - built-in strategies package for core strategy defaults

Usage:
    selector = ArtifactPrepSelector()
    decision = selector.select(context)

Notes:
    - Cold-path planner support is intentionally not enabled here; no-match
      decisions return diagnostics and callers can choose fallback behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import ArtifactFeatureMatch, ArtifactPrepManifest, ArtifactPrepPlan, ArtifactPrepResult
from .strategies import builtin_strategies, ensure_builtin_prep_tools_registered
from .strategy import ArtifactPrepContext, ArtifactPrepStrategy


@dataclass(frozen=True)
class ArtifactPrepSelection:
    """A selected strategy and its declarative plan."""

    strategy: ArtifactPrepStrategy
    plan: ArtifactPrepPlan


class RegisteredToolPrepStrategy:
    """Adapter that makes registry-backed prep tools selectable/executable."""

    def __init__(self, *, tool_name: str, tool: Any, manifest: ArtifactPrepManifest) -> None:
        self.tool_name = tool_name
        self.tool = tool
        self.manifest = manifest

    def plan(self, context: ArtifactPrepContext) -> ArtifactPrepPlan:
        config_hash = getattr(context, "config", {}) or {}
        from .hashing import canonical_json_hash

        return ArtifactPrepPlan(
            source_artifact_id=str(context.source_artifact_id or getattr(context.artifact, "id", "") or ""),
            strategy_id=self.manifest.strategy_id,
            strategy_version=self.manifest.strategy_version,
            prep_decision_source="dispatch",
            expected_chunk_kinds=list(self.manifest.produces_chunk_kinds),
            canonical_config_hash=canonical_json_hash(
                {
                    "tool_name": self.tool_name,
                    "strategy": self.manifest.strategy_id,
                    "strategy_version": self.manifest.strategy_version,
                    "config": config_hash,
                }
            ),
        )

    def prepare(self, plan: ArtifactPrepPlan, context: ArtifactPrepContext) -> ArtifactPrepResult:
        result = self.tool.func(
            {
                "plan": plan.model_dump(mode="json"),
                "context": context.model_dump(mode="python"),
            }
        )
        if isinstance(result, ArtifactPrepResult):
            return result
        return ArtifactPrepResult.model_validate(result)


def _normalize_content_type(content_type: str) -> str:
    return (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()


def _normalize_extension(filename: Optional[str], extension: Optional[str] = None) -> str:
    raw = extension or Path(filename or "").suffix
    if not raw:
        return ""
    raw = raw.lower().strip()
    return raw if raw.startswith(".") else f".{raw}"


def _artifact_kind_value(artifact: Any) -> str:
    raw = getattr(artifact, "kind", "")
    return str(getattr(raw, "value", raw))


def _metadata_matches(match: ArtifactFeatureMatch, metadata: dict[str, Any]) -> bool:
    for key, expected in match.metadata_hints.items():
        if metadata.get(key) != expected:
            return False
    return True


def _pattern_matches(value: str, patterns: Iterable[str]) -> bool:
    normalized = value.lower()
    for pattern in patterns:
        pat = str(pattern or "").lower().strip()
        if not pat:
            continue
        if pat.endswith("/*") and normalized.startswith(pat[:-1]):
            return True
        if pat == normalized:
            return True
    return False


def manifest_matches(manifest: ArtifactPrepManifest, context: ArtifactPrepContext) -> bool:
    """Return True if a manifest handles the context artifact."""

    kind = _artifact_kind_value(context.artifact)
    content_type = _normalize_content_type(context.payload_info.content_type)
    extension = _normalize_extension(context.payload_info.filename, context.payload_info.extension)
    metadata = getattr(context.artifact, "metadata", {}) or {}
    size = int(context.payload_info.bytes or getattr(context.artifact, "bytes", 0) or 0)

    for match in manifest.handles:
        if match.kinds and kind not in set(str(value) for value in match.kinds):
            continue
        if match.content_types and not _pattern_matches(content_type, match.content_types):
            continue
        if match.extensions and extension not in {str(value).lower() for value in match.extensions}:
            continue
        if match.min_bytes is not None and size < match.min_bytes:
            continue
        if match.max_bytes is not None and size > match.max_bytes:
            continue
        if not _metadata_matches(match, metadata):
            continue
        return True
    return False


class ArtifactPrepSelector:
    """Deterministic hot-path preparation strategy selector."""

    def __init__(self, strategies: Optional[list[ArtifactPrepStrategy]] = None) -> None:
        if strategies is not None:
            self._strategies = list(strategies)
        else:
            ensure_builtin_prep_tools_registered()
            self._strategies = list(builtin_strategies())
            self._strategies.extend(self._registry_strategies(exclude_ids={s.manifest.strategy_id for s in self._strategies}))

    def _registry_strategies(self, *, exclude_ids: set[str]) -> list[ArtifactPrepStrategy]:
        from motet.core.tools.registry import registry as tool_registry

        strategies: list[ArtifactPrepStrategy] = []
        for tool_name, tool in sorted(tool_registry.list_items().items()):
            raw_manifest = getattr(tool, "prep_manifest", None)
            if raw_manifest is None:
                continue
            manifest = (
                raw_manifest
                if isinstance(raw_manifest, ArtifactPrepManifest)
                else ArtifactPrepManifest.model_validate(raw_manifest)
            )
            if manifest.strategy_id in exclude_ids:
                continue
            exclude_ids.add(manifest.strategy_id)
            strategies.append(RegisteredToolPrepStrategy(tool_name=tool_name, tool=tool, manifest=manifest))
        return strategies

    @property
    def strategies(self) -> list[ArtifactPrepStrategy]:
        """Return known strategy instances."""

        return list(self._strategies)

    def select(self, context: ArtifactPrepContext) -> ArtifactPrepSelection:
        """Select the highest-priority matching strategy and return its plan."""

        disabled = set(context.hints.disable_strategies or [])
        metadata = getattr(context.artifact, "metadata", {}) or {}
        disabled.update(str(value) for value in metadata.get("disable_strategies", []) or [])
        explicit = context.hints.prep_strategy_id or metadata.get("prep_strategy_id")

        candidates = [strategy for strategy in self._strategies if strategy.manifest.strategy_id not in disabled]
        if explicit:
            for strategy in candidates:
                if strategy.manifest.strategy_id == explicit:
                    return ArtifactPrepSelection(strategy=strategy, plan=strategy.plan(context))
            raise ValueError(f"Requested artifact preparation strategy is not registered or enabled: {explicit}")

        matched = [strategy for strategy in candidates if manifest_matches(strategy.manifest, context)]
        if not matched:
            # Deterministic terminal fallback only for reliable text representations.
            text_strategy = next((strategy for strategy in candidates if strategy.manifest.strategy_id == "text_default"), None)
            content_type = _normalize_content_type(context.payload_info.content_type)
            if text_strategy and (content_type.startswith("text/") or content_type in {"application/xml"}):
                plan = text_strategy.plan(context)
                plan.confidence = 0.6
                plan.diagnostics.append("fallback_text_strategy")
                return ArtifactPrepSelection(strategy=text_strategy, plan=plan)
            raise ValueError(f"No artifact preparation strategy matched content_type={context.payload_info.content_type}")

        matched.sort(key=lambda strategy: (-int(strategy.manifest.priority), strategy.manifest.strategy_id))
        selected = matched[0]
        return ArtifactPrepSelection(strategy=selected, plan=selected.plan(context))

