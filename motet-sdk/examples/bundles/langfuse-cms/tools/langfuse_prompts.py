"""
Motet SDK - Langfuse CMS: Prompt Tools

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-05

Description:
Tools to get, list, and update (create a new version of) prompts in
Langfuse Cloud for the demo agent. Credentials come from Motet vault
(key ``langfuse``) or LANGFUSE_* environment variables.

Dependencies:
- motet_sdk: @motet.tool, get_motet_context
- pydantic: tool parameter schemas
- commands/_langfuse: shared Langfuse Cloud HTTP helpers

Usage:
  langfuse-cms.get_prompt(name="langfuse_cms.prompt_manager", label="production")
  langfuse-cms.list_prompts()
  langfuse-cms.update_prompt(prompt="You are …", labels=["production"])

Notes:
- Registered under langfuse-cms.* via the bundle loader.
- update_prompt POSTs a new version; Langfuse treats same-name creates as versions.
- Shared helpers are loaded lazily from the commands package (load_order:
  commands before tools).
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from motet_sdk import get_motet_context, motet

_DEFAULT_PROMPT_NAME = "langfuse_cms.prompt_manager"
_DEFAULT_LABEL = "production"
_HELPER_MODULE = "bundle.langfuse-cms.commands._langfuse"


def _lf() -> Any:
    return importlib.import_module(_HELPER_MODULE)


class GetPromptParams(BaseModel):
    """Input for get_prompt."""

    name: str = Field(
        default=_DEFAULT_PROMPT_NAME,
        description="Langfuse prompt name",
    )
    label: str = Field(
        default=_DEFAULT_LABEL,
        description="Prompt label (e.g. production, staging)",
    )
    version: Optional[int] = Field(
        default=None,
        description="Optional exact version; when set, label is ignored",
    )


class ListPromptsParams(BaseModel):
    """Input for list_prompts."""

    name: Optional[str] = Field(default=None, description="Optional name filter")
    label: Optional[str] = Field(default=None, description="Optional label filter")
    page: int = Field(default=1, ge=1, description="Page number (1-based)")
    limit: int = Field(default=20, ge=1, le=100, description="Page size")


class UpdatePromptParams(BaseModel):
    """Input for update_prompt (creates a new Langfuse prompt version)."""

    name: str = Field(
        default=_DEFAULT_PROMPT_NAME,
        description="Langfuse prompt name",
    )
    prompt: str = Field(..., description="New prompt text (text-type prompt)")
    labels: List[str] = Field(
        default_factory=lambda: [_DEFAULT_LABEL],
        description="Labels to apply to the new version",
    )
    prompt_type: str = Field(
        default="text",
        description="Langfuse prompt type (text or chat); demo uses text",
    )


def _creds() -> Dict[str, str]:
    return _lf().resolve_credentials(get_motet_context(), require_host=True)


def _fmt_get(res: Dict[str, Any]) -> str:
    return (
        f"get_prompt(name={res.get('name')!r}, label={res.get('label')!r}, "
        f"version={res.get('version')})"
    )


def _fmt_list(res: Dict[str, Any]) -> str:
    return f"list_prompts(count={res.get('count', 0)})"


def _fmt_update(res: Dict[str, Any]) -> str:
    return (
        f"update_prompt(name={res.get('name')!r}, version={res.get('version')}, "
        f"labels={res.get('labels')})"
    )


@motet.tool(
    description=(
        "Fetch a prompt from Langfuse Cloud by name and label (or version). "
        "Default name is langfuse_cms.prompt_manager, label production. "
        "Returns prompt text plus Langfuse metadata."
    ),
    name="get_prompt",
    schema=GetPromptParams,
    observation_formatter=_fmt_get,
    category="langfuse-cms",
    cost_class="low",
    keywords=["langfuse", "prompt", "get", "cloud"],
)
def get_prompt(params: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch one Langfuse Cloud prompt version by name/label."""
    parsed = GetPromptParams(**(params or {}))
    helper = _lf()
    try:
        creds = _creds()
        if parsed.version is not None:
            payload = helper.get_prompt(creds, parsed.name, version=parsed.version)
            label = None
        else:
            payload = helper.get_prompt(creds, parsed.name, label=parsed.label)
            label = parsed.label
        text = helper.extract_system_prompt_text(payload)
        return {
            "ok": True,
            "name": payload.get("name") or parsed.name,
            "label": label,
            "version": payload.get("version"),
            "type": payload.get("type"),
            "prompt_text": text,
            "raw": {
                "labels": payload.get("labels"),
                "tags": payload.get("tags"),
                "config": payload.get("config"),
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "name": parsed.name,
            "label": parsed.label,
        }


@motet.tool(
    description=(
        "List prompt names/versions/labels from Langfuse Cloud. "
        "Optional filters: name, label, page, limit."
    ),
    name="list_prompts",
    schema=ListPromptsParams,
    observation_formatter=_fmt_list,
    category="langfuse-cms",
    cost_class="low",
    keywords=["langfuse", "prompt", "list", "cloud"],
)
def list_prompts(params: Dict[str, Any]) -> Dict[str, Any]:
    """List Langfuse Cloud prompts (metadata)."""
    parsed = ListPromptsParams(**(params or {}))
    helper = _lf()
    try:
        creds = _creds()
        payload = helper.list_prompts(
            creds,
            name=parsed.name,
            label=parsed.label,
            page=parsed.page,
            limit=parsed.limit,
        )
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        return {
            "ok": True,
            "count": len(data),
            "data": data,
            "meta": payload.get("meta"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "count": 0,
            "data": [],
        }


@motet.tool(
    description=(
        "Create a new Langfuse Cloud prompt version (text type by default) and "
        "optionally apply labels such as production. Use to update the demo "
        "agent prompt named langfuse_cms.prompt_manager."
    ),
    name="update_prompt",
    schema=UpdatePromptParams,
    observation_formatter=_fmt_update,
    category="langfuse-cms",
    cost_class="low",
    keywords=["langfuse", "prompt", "update", "create", "cloud"],
)
def update_prompt(params: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new Langfuse prompt version with optional labels."""
    parsed = UpdatePromptParams(**(params or {}))
    helper = _lf()
    try:
        creds = _creds()
        prompt_body: Any = parsed.prompt
        if parsed.prompt_type == "chat":
            prompt_body = [{"role": "system", "content": parsed.prompt}]
        payload = helper.create_prompt_version(
            creds,
            name=parsed.name,
            prompt=prompt_body,
            prompt_type=parsed.prompt_type,
            labels=list(parsed.labels or []),
        )
        return {
            "ok": True,
            "name": payload.get("name") or parsed.name,
            "version": payload.get("version"),
            "labels": payload.get("labels") or parsed.labels,
            "type": payload.get("type") or parsed.prompt_type,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "name": parsed.name,
        }
