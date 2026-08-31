/**
 * Motet - Chat Explorer - Reasoning Panel Include
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     When an agent stream belongs on the right-rail reasoning list.
 *     Thinking text stays in the bubble or spawn card; the rail lists
 *     the agent as soon as thinking starts, when it has steps, or when
 *     a priced cost arrives. An in-progress think is a spinner only.
 *
 * Dependencies:
 *     - @motet/ui-common agent stream types and cost helper
 */

import { type AgentStreamSlice } from "@motet/ui-common/types";
import { knownCostUsd } from "@motet/ui-common/utils";

function sliceHasToolSteps(slice: AgentStreamSlice): boolean {
  return (
    (Array.isArray(slice.toolSummaries) && slice.toolSummaries.length > 0) ||
    (Array.isArray(slice.toolExecutions) && slice.toolExecutions.length > 0)
  );
}

function stepsHaveLines(steps: Array<{ step: number; lines: string[] }>): boolean {
  return steps.some((step) => step.lines.length > 0);
}

/** True when this agent has started a thinking trace (text may still be empty). */
export function sliceHasThinking(slice: AgentStreamSlice): boolean {
  if (String(slice.thinkingText || "").trim()) return true;
  return typeof slice.thinkingComplete === "boolean";
}

/**
 * True while this agent is still thinking. Finished turns and complete
 * traces are not active, even if thinking text is stored.
 */
export function sliceIsThinkingActive(slice: AgentStreamSlice, turnLive: boolean): boolean {
  if (slice.thinkingComplete === true) return false;
  if (!sliceHasThinking(slice)) return false;
  if (slice.thinkingComplete === false) return true;
  return turnLive;
}

/** Thinking belongs in the bubble / spawn card, not the parent rail. */
export function includeReasoningPanel(
  _aid: string,
  slice: AgentStreamSlice,
  steps: Array<{ step: number; lines: string[] }>
): boolean {
  const hasSteps = stepsHaveLines(steps) || sliceHasToolSteps(slice);
  return hasSteps || knownCostUsd(slice.costUsd) != null || sliceHasThinking(slice);
}
