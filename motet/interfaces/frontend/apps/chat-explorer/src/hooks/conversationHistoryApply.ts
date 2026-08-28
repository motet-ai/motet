/**
 * Motet - Chat Explorer - Conversation History Apply
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Predicates for when restored conversation history may be written into
 *     useXChat, and when a first-message auto-title may PATCH the server.
 *     The SDK keeps conversationKey and the message store one or
 *     two frames behind the activeConversationKey prop. Writing earlier
 *     updates the previous chat's store and leaves the selected thread empty.
 *     A late GET for a brand-new id must not replace messages the user already
 *     streamed in this thread. Auto-title must wait until agent_turn has
 *     claimed the local "+" id or rename returns 403.
 *
 * Dependencies:
 *     - None
 *
 * Usage:
 *     if (shouldApplyHistory({ pendingKey, activeKey, storeReadyKey, ... })) {
 *       setMessages(messageInfos);
 *     }
 *
 * Notes:
 *     - storeReadyKey is the conversation id whose setMessages identity is
 *       current. It updates only when useXChat swaps stores.
 */

/**
 * True when pending history belongs to the active conversation and useXChat
 * has already swapped to that conversation's message store.
 */
export function isHistoryStoreReady(
  pendingKey: string,
  activeKey: string,
  storeReadyKey: string | null,
): boolean {
  return pendingKey === activeKey && storeReadyKey === pendingKey;
}

/**
 * True when restored history may replace the current store.
 *
 * A brand-new chat fetches GET /conversations/:id as soon as the id is
 * created. That GET can return after the user has already sent, and the
 * snapshot is often user-only. Applying it wipes the assistant bubble.
 */
export function shouldApplyHistory(args: {
  pendingKey: string;
  activeKey: string;
  storeReadyKey: string | null;
  isRequesting: boolean;
  localMessageCount: number;
  alreadyHydrated: boolean;
}): boolean {
  if (!isHistoryStoreReady(args.pendingKey, args.activeKey, args.storeReadyKey)) {
    return false;
  }
  if (args.isRequesting) {
    return false;
  }
  if (!args.alreadyHydrated && args.localMessageCount > 0) {
    return false;
  }
  return true;
}

/**
 * True when the first local user message may set the sidebar label to a
 * snippet of that message. Requires the useXChat store to belong to the
 * active conversation so a lagged store from the previous thread cannot
 * title a brand-new "+" chat.
 */
export function shouldQueueAutoTitle(args: {
  storeReadyKey: string | null;
  activeKey: string;
  label: string | undefined;
  alreadyUpdated: boolean;
  hasUserMessage: boolean;
}): boolean {
  if (!isHistoryStoreReady(args.activeKey, args.activeKey, args.storeReadyKey)) {
    return false;
  }
  if (args.alreadyUpdated || !args.hasUserMessage) {
    return false;
  }
  return (args.label || "New Chat") === "New Chat";
}

/**
 * True when the stored title looks like a 40-character first-message snippet
 * (``…`` suffix) and the loaded first user message is the longer original.
 */
export function isLegacyTruncatedAutoTitle(label: string | undefined, content: string): boolean {
  const title = String(label || "").trim();
  if (!title.endsWith("...")) return false;
  const prefix = title.slice(0, -3);
  return prefix.length > 0 && content.startsWith(prefix);
}

/**
 * True when the queued auto-title may PATCH the server.
 *
 * ``conversation_rename`` refuses unclaimed ids (403). "+" mints a local id;
 * ownership is bound on the first agent_turn. Persist only after that turn
 * has finished for the pending conversation.
 */
export function shouldFlushAutoTitlePersist(args: {
  pendingKey: string | null;
  turnCompletedForPending: boolean;
  isRequesting: boolean;
}): boolean {
  if (!args.pendingKey || args.isRequesting) {
    return false;
  }
  return args.turnCompletedForPending;
}
