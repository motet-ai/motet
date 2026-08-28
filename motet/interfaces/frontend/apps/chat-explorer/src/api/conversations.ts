/**
 * Motet - Chat Explorer - Conversations API Client
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-03-24
 *
 * Description:
 *     Re-exports the conversations API client from @motet/ui-common.
 *     App-specific code should import from here for a single migration point.
 */

export {
  listConversations,
  getConversation,
  updateConversationTitle,
  deleteConversation,
  mapHistoryToMessages,
} from "@motet/ui-common/api";

export type {
  ConversationItem,
  ConversationListResponse,
  ConversationHistoryAttachment,
  ConversationHistoryItem,
  ConversationDetailResponse,
  ListConversationsParams,
} from "@motet/ui-common/api";
