/**
 * Motet - Motet UI Common - Shared Utilities
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
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

// Agent display utilities
export { shortAgentLabel, resolveAgentDisplayName, isSpawnAgentId, spawnAgentOrdinal } from "./agents";
export type { AgentRegistryEntry } from "./agents";
export {
  assistantTurnSlices,
  assistantTranscriptTurnSlice,
  asSpawnChildCards,
  groupTranscriptAssistantTurns,
  resolvePrimaryAgentKey,
  resolveTranscriptPrimaryAgentKey,
  spawnCardsForTurn,
  peerSpeakerSlices,
  spawnAgentKeyForChildConversation,
  projectLiveSpawnChildMessage,
  resolveDisplayedLiveMessage,
} from "./assistantTurn";
export {
  LiveTurnRegistry,
  chatOutputConversationId,
  isRenderableLiveMessage,
  shouldClearLiveTurn,
  shouldKeepLiveStreamOverHistory,
  tagChatOutputConversation,
} from "./liveTurns";
export type { LiveTurn } from "./liveTurns";
export { consumeChatSse, parseSseBuffer } from "./chatSse";
export type { AssistantTurnSlice, TranscriptHistoryMessage } from "./assistantTurn";

// Execution status formatting
export {
  formatCostUsd,
  formatExecutionStatusLine,
  groupToolSummariesIntoSteps,
  isConductorSidebarThought,
  knownCostUsd,
  positiveLoopStep,
  stepsFromAgentStreamSlice,
  sumKnownCostUsd,
  toolExecutionsToSummaries,
  toolSummaryStatusLines,
} from "./formatting";
export type { ToolSummaryRow } from "./formatting";
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

