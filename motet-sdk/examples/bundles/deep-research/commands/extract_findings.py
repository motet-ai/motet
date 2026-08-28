"""
Motet SDK - Deep Research Example: Per-Page Finding Extraction Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
Fetch a single page with core.http_get_browser, then use LLM inference
to extract structured findings from the page content.  Demonstrates
combining tool execution with model inference in a single command.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Called by deep-research.analyze_sources using motet.apply:
  deep-research.extract_findings(url="https://...", topic="quantum computing")

Notes:
- Combines two capabilities in one command: browser-based fetching
  (motet.tools.execute) and LLM analysis (motet.models.infer).
- Returns structured success/failure so the pipeline tolerates individual
  page failures gracefully.
- core.http_get_browser returns page text as "main_content"; a page with no
  extractable text is reported as low relevance rather than sent to the LLM.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from pydantic import Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class ExtractFindingsData(BaseCommandData):
    """Input for deep-research.extract_findings."""

    url: str = Field(..., description="Page URL to fetch and analyze")
    topic: str = Field(..., description="Research topic for context")
    title: str = Field(default="", description="Page title from search results")
    snippet: str = Field(default="", description="Search result snippet")
    max_chars: int = Field(
        default=4000,
        ge=500,
        le=12000,
        description="Maximum page content characters to send to the LLM",
    )
    fetch_timeout: float = Field(
        default=30.0,
        ge=5.0,
        le=90.0,
        description="Browser fetch timeout in seconds",
    )
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")


_EXTRACT_PROMPT = """\
You are a research analyst.  Given the PAGE CONTENT below, extract key \
findings relevant to the RESEARCH TOPIC.

Rules:
- Return a JSON object with these fields:
  "findings": list of 2-5 concise factual statements (strings)
  "relevance": "high", "medium", or "low"
  "summary": one-sentence summary of what this page contributes
- If the page is not relevant, return {{"findings": [], "relevance": "low", "summary": "Not relevant."}}
- Output ONLY valid JSON — no commentary.

RESEARCH TOPIC: {topic}

PAGE CONTENT:
{content}
"""


def _parse_extraction(text: str) -> Dict[str, Any]:
    """Parse LLM extraction output into a structured dict."""
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return {
                    "findings": parsed.get("findings", []),
                    "relevance": parsed.get("relevance", "low"),
                    "summary": parsed.get("summary", ""),
                }
        except (json.JSONDecodeError, TypeError):
            pass
    return {"findings": [], "relevance": "low", "summary": "Extraction failed."}


@motet.command(
    timeout_seconds=90,
    required_capabilities=[
        WorkerCapability.TOOL_EXECUTION,
        WorkerCapability.MODEL_INFERENCE,
    ],
)
def extract_findings(data: ExtractFindingsData, motet: MotetContext) -> Dict[str, Any]:
    """Fetch a page and use an LLM to extract structured findings."""
    try:
        tool_result = motet.tools.execute(
            "core.http_get_browser",
            {
                "url": data.url,
                "max_chars": data.max_chars,
                "timeout": data.fetch_timeout,
            },
        )
        if not isinstance(tool_result, dict):
            tool_result = {"raw_result": tool_result}

        content = (
            tool_result.get("main_content")
            or tool_result.get("content")
            or tool_result.get("text")
            or tool_result.get("markdown")
            or ""
        )
        content = str(content)[: data.max_chars]
    except Exception as exc:
        return {
            "ok": False,
            "url": data.url,
            "error": f"fetch failed: {exc}",
            "findings": [],
            "relevance": "low",
            "summary": "",
        }

    # Report a thin page rather than spending tokens on it. Never fall back to
    # stringifying the tool payload — that bills the LLM for a dict repr.
    if len(content.strip()) < 100:
        return {
            "ok": True,
            "url": data.url,
            "title": data.title or str(tool_result.get("title") or data.url),
            "findings": [],
            "relevance": "low",
            "summary": "Page had insufficient content.",
        }

    prompt = _EXTRACT_PROMPT.format(topic=data.topic, content=content)

    try:
        result = motet.models.infer(
            provider=data.provider,
            model_name=data.model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=1000,
        )
        llm_text = result.get("content") or result.get("response") or ""
        extracted = _parse_extraction(llm_text)
    except Exception as exc:
        extracted = {"findings": [], "relevance": "low", "summary": f"LLM error: {exc}"}

    return {
        "ok": True,
        "url": data.url,
        "title": data.title or tool_result.get("title", data.url),
        **extracted,
    }
