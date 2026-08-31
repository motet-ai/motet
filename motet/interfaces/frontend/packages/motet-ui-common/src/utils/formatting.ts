/**
 * Motet UI Common - Execution Status Formatting
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-29
 *
 * Description:
 *     Pure formatting functions for rendering execution status lines
 *     (workflow steps, tool executions) with consistent emoji-prefixed style.
 *
 * Usage:
 *     import { formatExecutionStatusLine } from "@motet/ui-common/utils";
 *
 *     formatExecutionStatusLine("web_search", "completed", { durationMs: 320 })
 *     // → "✅ web_search completed (320ms)"
 */

import type { ToolSummaryRow } from "../types";

export type { ToolSummaryRow };

/** Positive loop iteration, or undefined when the value is missing or not a step. */
export function positiveLoopStep(value: unknown): number | undefined {
  const parsed = typeof value === "number" ? value : Number(value);
  if (Number.isFinite(parsed) && parsed > 0) return parsed;
  return undefined;
}

/** Format a status line for an execution event (workflow_step, tool execution, etc.). */
export function formatExecutionStatusLine(
  name: string,
  status: string,
  opts: { durationMs?: number; error?: string } = {}
): string | null {
  const durationStr = opts.durationMs ? ` (${opts.durationMs}ms)` : "";
  const errorStr = opts.error ? `: ${String(opts.error).slice(0, 50)}` : "";
  switch (status) {
    case "started":
    case "running":
      return `⚙️ Executing ${name}...`;
    case "completed":
    case "success":
      return `✅ ${name} completed${durationStr}`;
    case "failed":
    case "error":
      return `❌ ${name} failed${errorStr}${durationStr}`;
    default:
      return null;
  }
}

/** True when a reasoning_step thought is loop conductor noise, not a tool line. */
export function isConductorSidebarThought(thought: string): boolean {
  const text = (thought || "").trim();
  if (!text) return false;
  if (/^Starting agentic loop iteration \d+$/.test(text)) return true;
  if (text.startsWith("LLM decided to use:")) return true;
  if (text === "LLM provided final response") return true;
  return false;
}

/** Status line plus optional preview for one stored tool summary. */
export function toolSummaryStatusLines(row: ToolSummaryRow): string[] {
  const name = row.tool_name || "unknown tool";
  const durationMs =
    typeof row.duration_ms === "number" && row.duration_ms > 0 ? row.duration_ms : undefined;
  const line = formatExecutionStatusLine(name, row.status || "success", {
    durationMs,
    error: row.status === "error" || row.status === "failed" ? row.preview : undefined,
  });
  const base = line || `${name} ${row.status || ""}`.trim();
  if (!base) return [];
  if (row.preview && row.status !== "error" && row.status !== "failed") {
    return [`${base}\n${row.preview}`];
  }
  return [base];
}

/**
 * Group stored tool summaries into sidebar Step N buckets.
 * Uses the loop iteration when present; otherwise one step per tool.
 */
export function groupToolSummariesIntoSteps(
  summaries: ToolSummaryRow[] | undefined
): Array<{ step: number; lines: string[] }> {
  if (!summaries?.length) return [];
  const byStep = new Map<number, string[]>();
  summaries.forEach((row, index) => {
    const step = positiveLoopStep(row.step) ?? index + 1;
    const prev = byStep.get(step) || [];
    byStep.set(step, [...prev, ...toolSummaryStatusLines(row)]);
  });
  return [...byStep.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([step, lines]) => ({ step, lines }));
}

/** Map live SSE toolExecutions onto the same row shape GET history stores. */
export function toolExecutionsToSummaries(
  executions:
    | Array<{
        toolName?: string;
        status?: string;
        preview?: string;
        durationMs?: number;
        error?: string;
        step?: number;
      }>
    | undefined
): ToolSummaryRow[] {
  if (!executions?.length) return [];
  return executions.map((row) => {
    const step = positiveLoopStep(row.step);
    return {
      tool_name: row.toolName || "unknown tool",
      status: row.status || "success",
      preview: row.preview || row.error,
      duration_ms: row.durationMs,
      ...(step != null ? { step } : {}),
    };
  });
}

/**
 * Sidebar steps for one agent slice. Prefer persisted toolSummaries;
 * otherwise group live toolExecutions by stamped loop step.
 */
export function stepsFromAgentStreamSlice(slice: {
  toolSummaries?: ToolSummaryRow[];
  toolExecutions?: Array<{
    toolName?: string;
    status?: string;
    preview?: string;
    durationMs?: number;
    error?: string;
    step?: number;
  }>;
}): Array<{ step: number; lines: string[] }> {
  if (slice.toolSummaries?.length) {
    return groupToolSummariesIntoSteps(slice.toolSummaries);
  }
  return groupToolSummariesIntoSteps(toolExecutionsToSummaries(slice.toolExecutions));
}

/** Priced USD amount, or null when unknown (not free). */
export function knownCostUsd(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return null;
  return value;
}

/** Format a priced estimate, or null when cost should stay hidden. */
export function formatCostUsd(value: unknown): string | null {
  const amount = knownCostUsd(value);
  if (amount == null) return null;
  return `$${amount.toFixed(4)}`;
}

/** Sum priced agent costs without treating missing values as zero. */
export function sumKnownCostUsd(values: unknown[]): number | null {
  let total = 0;
  let saw = false;
  for (const value of values) {
    const amount = knownCostUsd(value);
    if (amount == null) continue;
    total += amount;
    saw = true;
  }
  return saw ? total : null;
}
