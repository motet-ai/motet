"""
Motet - Tool Result Formatting Helpers

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Shared helpers for formatting tool results into small, human-readable previews
    and for unwrapping MCP CallToolResult envelopes to text.

    Formats safe, capped previews for:
    - ToolInvocation.preview_observation (UI/debugging)
    - Provider-specific tool transcript rendering fallbacks (optional future use)

    ``extract_text_from_mcp_result`` is the shared MCP unwrap used by:
    - Agentic-loop observation formatting
    - ``core.transform`` ``mcp_text`` (workflow authoring)

    Key safety goals:
    - Never echo large base64/binary payloads in previews
    - Prefer readable, structured summaries for common tool families (web search, HTTP, downloads)
    - Always cap output deterministically

Dependencies:
    - json: Safe serialization for dict/list results
    - typing: Type hints

Usage:
    from motet.core.tools.result_formatting import (
        format_tool_result_preview,
        extract_text_from_mcp_result,
    )

    preview = format_tool_result_preview(
        tool_name="oauth_download_url_with_token",
        result={"status": 200, "content_type": "text/vtt", "bytes": 1234, "text": "..."},
        max_chars=200,
    )
    text = extract_text_from_mcp_result({"content": [{"type": "text", "text": "..."}]})

Notes:
    - This module must remain lightweight and avoid importing orchestration code to prevent
      circular imports during worker startup.
    - This is intentionally conservative: if a payload looks binary (e.g., base64 field),
      we emit metadata only.
    - MCP unwrap does not parse Playwright markdown reports; that is ``playwright_result``
      on ``core.transform``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


def _cap_chars(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _extract_text_from_mcp_content(result: Dict[str, Any]) -> Optional[str]:
    """
    Extract text from MCP-style responses:
      {"content": [{"type": "text", "text": "..."}]}
    """
    content_items = result.get("content")
    if not isinstance(content_items, list):
        return None
    parts: list[str] = []
    for item in content_items:
        if isinstance(item, dict):
            if item.get("type") == "text" and "text" in item:
                parts.append(str(item.get("text") or ""))
            elif "text" in item:
                parts.append(str(item.get("text") or ""))
        elif isinstance(item, str):
            parts.append(item)
    joined = "\n".join([p for p in parts if p])
    return joined or None


def extract_text_from_mcp_result(result: Any) -> str:
    """
    Extract readable text from an MCP CallToolResult (or similar dict).

    Resolution order:
      1. ``structuredContent.result`` (string, or dict with ``text``)
      2. ``content[]`` text parts (MCP standard)
      3. Common tool fields (``main_content``, ``text``, string ``result``)
      4. JSON stringification for other dicts; ``str()`` otherwise

    Does not parse Playwright ``### Result`` markdown — that is a separate
    transform op. Pass-through for values that are already strings.
    """
    if not result:
        return ""

    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and "result" in structured:
            text = structured["result"]
            if isinstance(text, str):
                return text
            if isinstance(text, dict) and "text" in text:
                return str(text["text"])

        mcp_text = _extract_text_from_mcp_content(result)
        if mcp_text:
            return mcp_text

        if "main_content" in result:
            main_content = result["main_content"]
            if isinstance(main_content, str):
                parts: list[str] = []
                if "title" in result:
                    parts.append(f"Title: {result['title']}")
                if "description" in result and result.get("description"):
                    parts.append(f"Description: {result['description']}")
                parts.append(f"\nContent:\n{main_content}")
                return "\n".join(parts)
        if isinstance(result.get("text"), str):
            return str(result["text"])
        result_value = result.get("result")
        if isinstance(result_value, str):
            return result_value

        return json.dumps(result, indent=2)

    if isinstance(result, str):
        return result

    return str(result)


def _looks_like_binary_payload(result: Dict[str, Any]) -> bool:
    """
    Heuristic: if the tool returned base64 or bytes-ish payload, avoid emitting it.
    """
    # Common fields used by tools that return raw/binary content.
    if "base64" in result:
        return True
    # Some tools may return large byte blobs under generic keys.
    for k in ("bytes", "content_bytes", "blob", "payload_bytes"):
        v = result.get(k)
        if isinstance(v, (bytes, bytearray)):
            return True
    return False


def _format_download_preview(tool_name: str, result: Dict[str, Any], max_chars: int) -> str:
    """
    Special-case for download-style tools (e.g., oauth_download_url_with_token).
    Never echo base64 content; only show text preview when present.
    """
    url = str(result.get("url") or "")
    content_type = str(result.get("content_type") or result.get("mime_type") or "")
    filename = result.get("filename")
    size_bytes = result.get("bytes")
    status = result.get("status")

    meta_parts: list[str] = []
    if url:
        meta_parts.append(f"url={url}")
    if content_type:
        meta_parts.append(f"content_type={content_type}")
    if isinstance(status, int):
        meta_parts.append(f"status={status}")
    if isinstance(size_bytes, int):
        meta_parts.append(f"bytes={size_bytes}")
    if filename:
        meta_parts.append(f"filename={filename}")
    meta = ", ".join(meta_parts) if meta_parts else "download"

    # Prefer text preview if present.
    text = result.get("text")
    if isinstance(text, str) and text:
        return _cap_chars(f"{tool_name}({meta})\n{text}", max_chars=max_chars)

    # Otherwise, explicitly avoid base64/binary.
    if "base64" in result:
        return _cap_chars(f"{tool_name}({meta}) [base64 omitted]", max_chars=max_chars)

    return _cap_chars(f"{tool_name}({meta})", max_chars=max_chars)


def _format_web_search_preview(result: Dict[str, Any], max_chars: int) -> str:
    query = str(result.get("query") or "")
    main_content = result.get("main_content")
    results = result.get("results")

    parts: list[str] = []
    if query:
        parts.append(f"Search Query: {query}")

    if isinstance(main_content, str) and main_content.strip():
        parts.append(main_content.strip())

    # Add a tiny top-N summary when results are structured.
    if isinstance(results, list) and results:
        summaries: list[str] = []
        for r in results[:3]:
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "").strip()
            url = str(r.get("url") or "").strip()
            if title and url:
                summaries.append(f"- {title} ({url})")
            elif title:
                summaries.append(f"- {title}")
        if summaries:
            parts.append("Top Results:\n" + "\n".join(summaries))

    preview = "\n\n".join([p for p in parts if p])
    return _cap_chars(preview or "Web search completed.", max_chars=max_chars)


def _format_http_preview(tool_name: str, result: Dict[str, Any], max_chars: int) -> str:
    status = result.get("status")
    url = str(result.get("url") or "")
    title = str(result.get("title") or "")

    # Prefer main_content/text snippet
    body = ""
    if isinstance(result.get("main_content"), str):
        body = str(result.get("main_content") or "")
    elif isinstance(result.get("text"), str):
        body = str(result.get("text") or "")

    head = f"{tool_name}"
    meta: list[str] = []
    if isinstance(status, int):
        meta.append(f"status={status}")
    if url:
        meta.append(f"url={url}")
    if title:
        meta.append(f"title={title}")
    if meta:
        head += "(" + ", ".join(meta) + ")"

    if body:
        return _cap_chars(f"{head}\n{body}", max_chars=max_chars)
    return _cap_chars(head, max_chars=max_chars)


def format_tool_result_preview(tool_name: str, result: Any, *, max_chars: int = 200) -> str:
    """
    Format a tool result into a small, readable preview suitable for ToolInvocation.preview_observation.
    """
    if result is None:
        return ""

    if isinstance(result, str):
        return _cap_chars(result, max_chars=max_chars)

    if isinstance(result, dict):
        # Prefer MCP content array extraction.
        mcp_text = _extract_text_from_mcp_content(result)
        if mcp_text:
            return _cap_chars(mcp_text, max_chars=max_chars)

        # Download tool special-casing (never show base64).
        tl = tool_name.lower().strip()
        if "download" in tl or tl in {"oauth_download_url_with_token", "core.oauth_download_url_with_token"}:
            return _format_download_preview(tool_name, result, max_chars=max_chars)

        # Web search summary
        if tl in {"web_search", "core.web_search"}:
            return _format_web_search_preview(result, max_chars=max_chars)

        # HTTP-ish summary
        if tl.startswith("http_") or tl.startswith("core.http_") or tl in {"http_get", "http_post", "core.http_get", "core.http_post"}:
            return _format_http_preview(tool_name, result, max_chars=max_chars)

        # If it looks binary, hide it.
        if _looks_like_binary_payload(result):
            # Try to emit metadata only.
            meta = {k: result.get(k) for k in ("status", "content_type", "bytes", "filename", "url") if k in result}
            if meta:
                return _cap_chars(f"{tool_name}({json.dumps(meta, ensure_ascii=False)}) [payload omitted]", max_chars=max_chars)
            return _cap_chars(f"{tool_name} [payload omitted]", max_chars=max_chars)

        # Common text/result fields
        if isinstance(result.get("text"), str):
            return _cap_chars(str(result.get("text") or ""), max_chars=max_chars)
        if isinstance(result.get("result"), str):
            return _cap_chars(str(result.get("result") or ""), max_chars=max_chars)

        # Compact JSON fallback
        try:
            return _cap_chars(json.dumps(result, ensure_ascii=False), max_chars=max_chars)
        except Exception:
            return _cap_chars(str(result), max_chars=max_chars)

    # Fallback for lists/other objects
    try:
        return _cap_chars(json.dumps(result, ensure_ascii=False), max_chars=max_chars)
    except Exception:
        return _cap_chars(str(result), max_chars=max_chars)


__all__ = ["format_tool_result_preview", "extract_text_from_mcp_result"]


