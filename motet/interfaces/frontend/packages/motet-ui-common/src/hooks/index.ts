/**
 * Motet - Motet UI Common - Hooks Index
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Public exports for shared React hooks.
 */

export { useAuth, buildHeaders, buildAuthHeaders } from "./useAuth";
export type { UseAuthOptions } from "./useAuth";

export { useEventBus } from "./useEventBus";
export type { UseEventBusOptions } from "./useEventBus";

export { useThrottle } from "./useThrottle";

export { useAttachments } from "./useAttachments";

export { useConversationManager, computeInitialConversations } from "./useConversationManager";
export {
  normalizeAgentId,
  scopeStorageKey,
  scopeListCacheKey,
  scopeKey,
  isAuthenticated,
  authIdentityKey,
  jwtSubject,
  planConversationListSync,
  cachedConversationsFor,
} from "./useConversationManager";
export type {
  ConversationEntry,
  ConversationStore,
  ConversationListScope,
  UseConversationManagerOptions,
  ConversationListSyncPlan,
} from "./useConversationManager";

export { useRequestContext } from "./useRequestContext";
export type { UseRequestContextOptions } from "./useRequestContext";
