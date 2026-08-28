/**
 * Motet - Motet UI Common - Shared Utilities
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Shared utility functions for Motet UI components.
 *
 * Dependencies:
 *     - None (pure functions)
 *
 * Usage:
 *     import { randomId, debugLog, parseSseBuffer } from "@motet/ui-common/utils";
 */

import type { SSEvent } from "../types";

// Agent display utilities
export { shortAgentLabel, resolveAgentDisplayName, isSpawnAgentId, spawnAgentOrdinal } from "./agents";
export type { AgentRegistryEntry } from "./agents";
export {
  assistantTurnSlices,
  assistantTranscriptTurnSlice,
  groupTranscriptAssistantTurns,
  resolvePrimaryAgentKey,
  resolveTranscriptPrimaryAgentKey,
} from "./assistantTurn";
export type { AssistantTurnSlice, TranscriptHistoryMessage } from "./assistantTurn";

// Execution status formatting
export { formatExecutionStatusLine } from "./formatting";
export { CORE_NAMESPACE, namespaceFromQualifiedName, qualifyWithCoreNamespace } from "./namespacing";

// Thinking / reasoning helpers (always-on models such as Kimi K3)
export { isAlwaysOnThinkingModel, treatsThinkingAsAlwaysOn } from "./thinking";

/**
 * Generates a random alphanumeric ID.
 * Uses base-36 encoding of a random number, producing IDs like "k7x9n2m".
 */
export const randomId = () => Math.random().toString(36).slice(2);

/**
 * Logs messages to console only in development mode.
 * Can be manually enabled by setting localStorage.setItem('DEBUG_LOG_ENABLED', 'true')
 *
 * @param args - Arguments to pass to console.log
 */
export const debugLog = (...args: any[]) => {
  const manualOverride = localStorage.getItem('DEBUG_LOG_ENABLED');
  if (manualOverride === 'true') {
    // eslint-disable-next-line no-console
    console.log('[DEBUG]', ...args);
    return;
  }
  if (manualOverride === 'false') {
    return;
  }
  
  // Default: only log in development mode
  if ((import.meta as any)?.env?.DEV) {
    // eslint-disable-next-line no-console
    console.log(...args);
  }
};

/**
 * Parses a Server-Sent Events (SSE) formatted buffer into structured events.
 *
 * @param buffer - Raw SSE text buffer (may contain multiple events)
 * @returns Array of parsed SSEvent objects
 */
export function parseSseBuffer(buffer: string): SSEvent[] {
  const out: SSEvent[] = [];
  const blocks = buffer.split("\n\n");

  for (const block of blocks) {
    if (!block.trim()) continue;

    const lines = block.split("\n");
    let evt = "message";
    const dataLines: string[] = [];

    for (const line of lines) {
      if (line.startsWith("event:")) {
        evt = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }

    const dataRaw = dataLines.join("\n");
    let data: any = dataRaw;
    try {
      data = dataRaw ? JSON.parse(dataRaw) : dataRaw;
    } catch {
      data = dataRaw;
    }

    out.push({ event: evt, data });
  }

  return out;
}
