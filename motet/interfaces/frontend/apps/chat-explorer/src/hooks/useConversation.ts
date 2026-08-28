/**
 * Motet - Chat Explorer - Conversation Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-04
 *
 * Description:
 *     Ant Design X adapter for the shared useConversationManager hook.
 *     Bridges useXConversations (Ant Design X SDK) to the framework-agnostic
 *     ConversationStore interface from @motet/ui-common.
 *
 *     All CRUD, API sync, caching, and scope logic lives in
 *     useConversationManager. This file only provides the Ant Design X
 *     state layer.
 *
 * Dependencies:
 *     - @ant-design/x-sdk: useXConversations
 *     - @motet/ui-common: useConversationManager, computeInitialConversations
 *
 * Usage:
 *     const result = useConversation(auth, scope);
 *
 * Notes:
 *     - Storage prefix is chat_explorer_conversation (migrated from
 *       demo_chat_x_conversation via migrateDemoChatXStorage at boot).
 */
import { useState } from "react";
import { useXConversations } from "@ant-design/x-sdk";
import {
  useConversationManager,
  computeInitialConversations,
  normalizeAgentId,
  type ConversationEntry,
  type ConversationListScope,
} from "@motet/ui-common";
import type { AuthState } from "../types";

const STORAGE_PREFIX = "chat_explorer_conversation";
/** Pre-rename current-conversation key (before demo_chat_x_conversation_*). */
const LEGACY_CURRENT_PREFIX = "demo_chat_x_current_conversation";

function migrateLegacyCurrentConversationKey(scope?: ConversationListScope | null): void {
  try {
    const agent = normalizeAgentId(scope?.agent_id);
    const surface = (scope?.surface_id || "*").trim() || "*";
    const newKey = `${STORAGE_PREFIX}_current:${agent}:${surface}`;
    const oldKey = `${LEGACY_CURRENT_PREFIX}:${agent}:${surface}`;

    const existing = localStorage.getItem(newKey);
    if (existing && existing.trim().length > 0) return;

    const legacy = localStorage.getItem(oldKey);
    if (legacy && legacy.trim().length > 0) {
      localStorage.setItem(newKey, legacy.trim());
    }
  } catch {
    // Ignore localStorage access errors.
  }
}

export type { ConversationListScope };

/**
 * React hook for managing conversation list and CRUD operations.
 * Wraps the shared useConversationManager with Ant Design X state management.
 */
export function useConversation(auth?: AuthState | null, scope?: ConversationListScope | null) {
  const [initial] = useState(() => {
    migrateLegacyCurrentConversationKey(scope);
    return computeInitialConversations(STORAGE_PREFIX, scope);
  });

  const xConversations = useXConversations({
    defaultConversations: initial.conversations,
    defaultActiveConversationKey: initial.activeKey,
  }) as ReturnType<typeof useXConversations> & {
    setConversations: (list: Array<{ key: string; label: string; timestamp?: number }>) => void;
  };

  // ConversationData extends AnyObject — label/timestamp exist at runtime
  // but aren't declared in the type. Cast through the adapter boundary.
  return useConversationManager(
    {
      conversations: xConversations.conversations as ConversationEntry[],
      activeConversationKey: xConversations.activeConversationKey,
      setActiveConversationKey: xConversations.setActiveConversationKey,
      addConversation: xConversations.addConversation,
      setConversation: xConversations.setConversation,
      removeConversation: xConversations.removeConversation,
      setConversations: xConversations.setConversations,
    },
    auth,
    scope,
    { storageKeyPrefix: STORAGE_PREFIX },
  );
}
