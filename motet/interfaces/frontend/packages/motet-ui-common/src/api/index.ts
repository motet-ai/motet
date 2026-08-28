/**
 * Motet UI Common - API Client Exports
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-03-24
 *
 * Description:
 *     Public exports for Motet API clients.
 */

export {
  listConversations,
  getConversation,
  updateConversationTitle,
  deleteConversation,
  mapHistoryToMessages,
} from "./conversations";

export type {
  ConversationItem,
  ConversationListResponse,
  ConversationHistoryAttachment,
  ConversationHistoryItem,
  ConversationDetailResponse,
  ListConversationsParams,
} from "./conversations";

export {
  reduceChatEvent,
  streamAgentKeyFromData,
  withAgentStream,
  CONTINUE_AFTER_BUDGET_USER_MESSAGE,
  BUDGET_STOP_REASONS,
  isBudgetStopReason,
} from "./chat";

export type {
  ChatMessage,
  ChatInput,
  ChatOutput,
  ReduceResult,
  MediaPart,
} from "./chat";
