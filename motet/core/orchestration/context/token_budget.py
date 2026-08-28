"""
Motet - Token Budget Context Provider

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-07-28

Description:
    Implements token budgeting and final token counting for the context
    preparation pipeline. It keeps protected agent skill messages and
    prompt-policy system prefixes (client harness + Motet appendix) while
    trimming older conversation history and sanitizing any orphaned tool-call
    spans left behind by budget enforcement. It also enforces the request image
    budget so context replay cannot exceed the model renderer's multimodal
    limits.

Dependencies:
    - time for provider timing metrics
    - context.tool_calls for post-budget tool-call sanitization
    - context.types for shared pipeline state

Usage:
    state = TokenBudgetProvider().apply(state, data=data, motet=motet, logger=logger)

Notes:
    - Token counting remains the existing word-based approximation until a
      model-specific tokenizer is introduced.
    - Image budgeting keeps the newest image parts first, because later turns
      are most likely to be relevant to the current request.
"""

from __future__ import annotations

import time
from typing import Any, List

from .tool_calls import sanitize_orphan_tool_call_messages
from .types import ContextPipelineState


def approx_tokens(messages: List[Any]) -> int:
    """Approximate token count using the existing word-count heuristic."""

    return len(" ".join([(msg.content or "") for msg in messages]).split())


class TokenBudgetProvider:
    """Apply context token budget and final token accounting."""

    name = "token_budget"

    def apply(
        self,
        state: ContextPipelineState,
        *,
        data: Any,
        motet: Any,
        logger: Any,
    ) -> ContextPipelineState:
        t0 = time.perf_counter()
        cfg = getattr(getattr(motet, "stack", None), "config", None)
        budget = None
        try:
            budget = int(data.max_context_tokens) if data.max_context_tokens is not None else None
        except Exception:
            budget = None
        if budget is None:
            try:
                budget = int(getattr(cfg, "token_budget", 0) or 0)
            except Exception:
                budget = 0

        if budget and budget > 0:
            from motet.core.agents.prompt_policy import is_prompt_policy_protected

            protected_kinds = {"agent_skill", "agent_skill_catalog"}
            protected = [
                msg
                for msg in state.messages
                if (getattr(msg, "metadata", None) or {}).get("content_kind") in protected_kinds
                or is_prompt_policy_protected(msg)
            ]
            protected_ids = {id(msg) for msg in protected}
            kept: List[Any] = []
            running = sum(len((msg.content or "").split()) for msg in protected)
            for msg in reversed([m for m in state.messages if id(m) not in protected_ids]):
                words = len((msg.content or "").split())
                if kept and running + words > budget:
                    break
                kept.append(msg)
                running += words
            kept_ids = {id(msg) for msg in kept}
            state.messages = [msg for msg in state.messages if id(msg) in protected_ids or id(msg) in kept_ids]
            state.messages, budget_sanitize_stats = sanitize_orphan_tool_call_messages(state.messages)
            if budget_sanitize_stats["removed_assistant_calls"] > 0 or budget_sanitize_stats["removed_tool_messages"] > 0:
                logger.warning(
                    "prepare_context_orphan_tool_calls_pruned_after_budgeting",
                    conversation_id=motet.conversation_id,
                    removed_assistant_calls=budget_sanitize_stats["removed_assistant_calls"],
                    removed_tool_messages=budget_sanitize_stats["removed_tool_messages"],
                )
            state.context_info["token_budget"] = budget
            state.context_info["token_budget_applied"] = True
        else:
            state.context_info["token_budget_applied"] = False

        self._apply_image_budget(state, motet=motet, logger=logger)
        state.context_info["token_count"] = approx_tokens(state.messages)
        state.timings["token_budgeting_s"] = round(time.perf_counter() - t0, 3)
        return state

    def _apply_image_budget(
        self,
        state: ContextPipelineState,
        *,
        motet: Any,
        logger: Any,
    ) -> None:
        max_images = self._resolve_max_images(motet)
        image_refs = self._collect_image_refs(state.messages)
        image_count = len(image_refs)

        if image_count <= max_images:
            state.context_info["image_count"] = image_count
            state.context_info["image_budget_applied"] = False
            state.context_info["max_images"] = max_images
            return

        keep_ids = {part_id for part_id, _msg_idx in image_refs[-max_images:]} if max_images > 0 else set()
        removed = 0
        for msg in state.messages:
            parts = getattr(msg, "content_parts", None) or []
            if not parts:
                continue
            filtered_parts: List[Any] = []
            changed = False
            for part in parts:
                if self._is_image_part(part) and id(part) not in keep_ids:
                    removed += 1
                    changed = True
                    continue
                filtered_parts.append(part)
            if changed:
                msg.content_parts = filtered_parts  # type: ignore[attr-defined]

        state.context_info["image_count"] = image_count - removed
        state.context_info["image_budget_applied"] = True
        state.context_info["image_parts_pruned"] = removed
        state.context_info["max_images"] = max_images
        logger.info(
            "prepare_context_image_budget_applied",
            conversation_id=getattr(motet, "conversation_id", None),
            original_image_count=image_count,
            retained_image_count=image_count - removed,
            removed_image_count=removed,
            max_images=max_images,
        )

    def _resolve_max_images(self, motet: Any) -> int:
        cfg = getattr(getattr(motet, "stack", None), "config", None)
        raw = getattr(cfg, "max_images", None)
        if raw is None:
            raw = getattr(cfg, "multimodal_max_images", None)
        try:
            return max(0, int(raw)) if raw is not None else 8
        except Exception:
            return 8

    def _collect_image_refs(self, messages: List[Any]) -> List[tuple[int, int]]:
        refs: List[tuple[int, int]] = []
        for msg_idx, msg in enumerate(messages):
            for part in getattr(msg, "content_parts", None) or []:
                if self._is_image_part(part):
                    refs.append((id(part), msg_idx))
        return refs

    def _is_image_part(self, part: Any) -> bool:
        part_type = getattr(part, "type", None) if not isinstance(part, dict) else part.get("type")
        if part_type != "media":
            return False
        media_type = getattr(part, "media_type", None) if not isinstance(part, dict) else part.get("media_type")
        return media_type == "image"
