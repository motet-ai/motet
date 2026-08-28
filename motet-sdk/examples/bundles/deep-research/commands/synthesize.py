"""
Motet SDK - Deep Research Example: Synthesis and Memory Persistence Command

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
Synthesize all extracted findings into a coherent research report using
LLM inference, then persist the report in memory for future recall.
Demonstrates motet.models.infer() for multi-document synthesis and
motet.memory.store() for knowledge persistence.

Dependencies:
- motet_sdk: command decorator and MotetContext typing
- pydantic: structured command inputs

Usage:
Called as the final step of the deep-research workflow:
  deep-research.synthesize(
    topic="quantum computing",
    analyzed=[{"url": "...", "findings": [...], "relevance": "high", ...}]
  )

Notes:
- The synthesis prompt feeds the LLM all high- and medium-relevance
  findings so it can cross-reference and produce a unified narrative.
- The finished report is stored principal-scoped (scope_type="principal")
  with tags derived from the topic so recall_research can find it across
  conversations. Returns memory_id / memory_store_status for verification.
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from motet_sdk import BaseCommandData, MotetContext, WorkerCapability, motet


class AnalyzedItem(BaseModel):
    """One analyzed source from analyze_sources output."""

    url: str = Field(default="", description="Source URL")
    title: str = Field(default="", description="Page title")
    findings: List[str] = Field(default_factory=list, description="Extracted findings")
    relevance: str = Field(default="low", description="Relevance rating")
    summary: str = Field(default="", description="One-line source summary")
    ok: bool = Field(default=True, description="Whether extraction succeeded")


class SynthesizeData(BaseCommandData):
    """Input for deep-research.synthesize."""

    topic: str = Field(..., description="Research topic")
    analyzed: List[AnalyzedItem] = Field(
        default_factory=list, description="Analyzed source results"
    )
    provider: str = Field(default="openai", description="LLM provider")
    model_name: str = Field(default="gpt-4o-mini", description="LLM model name")
    store_in_memory: bool = Field(
        default=True,
        description="Whether to persist the report in memory",
    )


_SYNTHESIS_PROMPT = """\
You are a senior research analyst.  Synthesize the FINDINGS below into a \
clear, well-structured research report on the given TOPIC.

Report structure:
1. **Executive Summary** — 2-3 sentence overview
2. **Key Findings** — numbered list of the most important facts
3. **Analysis** — deeper discussion connecting the findings
4. **Sources** — list the source URLs

Use markdown formatting.  Be factual and cite which source each finding \
comes from where possible.

TOPIC: {topic}

FINDINGS:
{findings_block}
"""


def _build_findings_block(analyzed: List[AnalyzedItem]) -> str:
    """Format analyzed items into a text block for the synthesis prompt."""
    parts: List[str] = []
    for idx, item in enumerate(analyzed, start=1):
        if not item.findings and item.relevance == "low":
            continue
        title = item.title or item.url
        findings_text = "\n".join(f"  - {f}" for f in item.findings) if item.findings else "  (no findings)"
        parts.append(f"Source {idx}: {title}\nURL: {item.url}\n{findings_text}")
    return "\n\n".join(parts) if parts else "(No relevant findings were extracted.)"


@motet.command(
    timeout_seconds=120,
    required_capabilities=[
        WorkerCapability.MODEL_INFERENCE,
        WorkerCapability.MEMORY_OPERATIONS,
    ],
)
def synthesize(data: SynthesizeData, motet: MotetContext) -> Dict[str, Any]:
    """Synthesize findings into a research report and store in memory."""
    relevant = [a for a in data.analyzed if a.relevance in ("high", "medium")]
    if not relevant:
        relevant = data.analyzed

    source_urls = [a.url for a in relevant if a.url]
    source_count = len(source_urls)
    finding_count = sum(len(a.findings) for a in relevant)

    if finding_count == 0:
        report = (
            f"# Research Report: {data.topic}\n\n"
            "## Status\n"
            "No verified findings were extracted from fetched sources, so a synthesized report was not generated.\n\n"
            "## Next Step\n"
            "- Retry with a narrower topic or fewer constraints.\n"
            "- Increase search breadth (`num_queries` / `max_results_per_query`) and run again.\n"
            "- Verify provider web-search availability for this model/profile."
        )
        return {
            "topic": data.topic,
            "report": report,
            "source_count": source_count,
            "finding_count": finding_count,
            "source_urls": source_urls,
            "memory_id": None,
            "memory_store_status": "skipped_no_findings",
            "memory_store_error": None,
        }

    findings_block = _build_findings_block(relevant)
    prompt = _SYNTHESIS_PROMPT.format(topic=data.topic, findings_block=findings_block)

    result = motet.models.infer(
        provider=data.provider,
        model_name=data.model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=3000,
    )
    report = result.get("content") or result.get("response") or ""

    memory_id = None
    memory_store_status = "not_requested"
    memory_store_error = None
    if data.store_in_memory and report:
        memory_store_status = "pending"
        try:
            topic_words = data.topic.lower().split()[:5]
            tags = ["research", "deep-research"] + [w for w in topic_words if len(w) > 2]
            mem_result = motet.memory.store(
                content=report,
                type="research_report",
                tags=tags,
                metadata={
                    "topic": data.topic,
                    "source_count": source_count,
                    "finding_count": finding_count,
                    "bundle": "deep-research",
                },
                scope_type="principal",
            )
            memory_id = mem_result.get("memory_id") or mem_result.get("id")
            stored_flag = bool(mem_result.get("stored")) if isinstance(mem_result, dict) else False
            if memory_id or stored_flag:
                memory_store_status = "stored"
            else:
                memory_store_status = "store_returned_no_id"
        except Exception as exc:
            memory_store_status = "error"
            memory_store_error = str(exc)

    return {
        "topic": data.topic,
        "report": report,
        "source_count": source_count,
        "finding_count": finding_count,
        "source_urls": source_urls,
        "memory_id": memory_id,
        "memory_store_status": memory_store_status,
        "memory_store_error": memory_store_error,
    }
