/**
 * Motet - Motet UI Common
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Shared UI components, hooks, types, and utilities for Motet applications.
 *     This package provides common functionality used across chat-explorer,
 *     ops-dashboard, and other Motet frontend applications.
 *
 * Usage:
 *     import { useAuth, AuthModal, SignedOutPage, AuthState } from "@motet/ui-common";
 */

// Types
export type { AuthState, SSEvent } from "./types";
export { defaultAuthState } from "./types";
export type {
  ReasoningStepEvent,
  WorkflowStepEvent,
  AgentStreamSlice,
  AgentReasoningPanel,
  ToolSummaryRow,
  SpawnChildCard,
} from "./types";
export { DEFAULT_STREAM_AGENT_KEY } from "./types";
export type { AttachmentState, DraftUploadItem, PresetIcons } from "./types";
export { inferPresetIcon, inferFileCardProps } from "./types";
export type { Overrides, ReasoningEffort } from "./types";
export type { ArtifactRagScope, RagControlsValue } from "./types";
export {
  defaultRagControlsValue,
  ragControlsIsCustom,
  ragScopeShortLabel,
  summarizeRagControls,
} from "./types";

// Hooks
export { useAuth, buildHeaders, buildAuthHeaders } from "./hooks";
export type { UseAuthOptions } from "./hooks";
export { useEventBus } from "./hooks";
export type { UseEventBusOptions } from "./hooks";
export { useThrottle } from "./hooks";
export { useAttachments } from "./hooks";
export { useConversationManager, computeInitialConversations } from "./hooks";
export { normalizeAgentId } from "./hooks";
export type {
  ConversationEntry,
  ConversationStore,
  ConversationListScope,
  UseConversationManagerOptions,
} from "./hooks";
export { useRequestContext } from "./hooks";
export type { UseRequestContextOptions } from "./hooks";
export { useLiveTurns } from "./hooks";

// Components
export {
  AuthModal,
  LoginRequiredModal,
  SignedOutPage,
  RequireRole,
  hasAnyRole,
  ADMIN_ROLES,
  SIGNED_OUT_STORAGE_KEY,
  markSignedOut,
  clearSignedOutFlag,
  wasSignedOut,
  appLogoutRedirectUri,
  finishRemoteLogout,
} from "./components";
export type {
  AuthModalProps,
  LoginRequiredModalProps,
  SignedOutPageProps,
  SignedOutVariant,
  RequireRoleProps,
} from "./components";
export { MermaidBlock, renderMarkdownWithMermaid } from "./components";
export { RenameModal } from "./components";
export type { RenameModalProps } from "./components";
export { RagControls } from "./components";
export type { RagControlsProps, RagArtifactOption } from "./components";
export { MediaRenderer } from "./components";
export type { MediaRendererProps } from "./components";

// Utils
export { randomId, debugLog, parseSseBuffer, consumeChatSse } from "./utils";
export {
  LiveTurnRegistry,
  chatOutputConversationId,
  isRenderableLiveMessage,
  shouldClearLiveTurn,
  shouldKeepLiveStreamOverHistory,
  tagChatOutputConversation,
} from "./utils";
export type { LiveTurn } from "./utils";
export {
  shortAgentLabel,
  resolveAgentDisplayName,
  isSpawnAgentId,
  spawnAgentOrdinal,
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
} from "./utils";
export type { AgentRegistryEntry, AssistantTurnSlice, TranscriptHistoryMessage } from "./utils";
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
} from "./utils";
export { CORE_NAMESPACE, namespaceFromQualifiedName, qualifyWithCoreNamespace } from "./utils";
export { isAlwaysOnThinkingModel, treatsThinkingAsAlwaysOn } from "./utils";

// API clients
export {
  listConversations,
  getConversation,
  getConversationCost,
  updateConversationTitle,
  deleteConversation,
  mapHistoryToMessages,
} from "./api";
export type {
  ConversationItem,
  ConversationListResponse,
  ConversationHistoryAttachment,
  ConversationHistoryItem,
  ConversationDetailResponse,
  ListConversationsParams,
  ConversationCostResponse,
} from "./api";

// Chat protocol (framework-agnostic SSE reducer)
export {
  reduceChatEvent,
  streamAgentKeyFromData,
  withAgentStream,
  CONTINUE_AFTER_BUDGET_USER_MESSAGE,
  BUDGET_STOP_REASONS,
  isBudgetStopReason,
} from "./api";
export type {
  ChatMessage,
  ChatInput,
  ChatOutput,
  ReduceResult,
  MediaPart,
} from "./api";
