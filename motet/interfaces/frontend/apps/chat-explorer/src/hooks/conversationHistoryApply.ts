/**
 * Motet - Chat Explorer - Conversation History Apply
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Predicates for when restored conversation history may be written into
 *     useXChat, when GET /conversations/:id may run, and when a first-message
 *     auto-title may PATCH the server.
 *     The SDK keeps conversationKey and the message store one or
 *     two frames behind the activeConversationKey prop. Writing earlier
 *     updates the previous chat's store and leaves the selected thread empty.
 *     A late GET for a brand-new id must not replace messages the user already
 *     streamed in this thread. Auto-title is keyed by conversation id:
 *     the outgoing send text titles that chat, and PATCH waits until that
 *     id has been in flight and is idle (agent_turn has claimed it). Do
 *     not read the throttled list, and do not write a send into useXChat
 *     until storeReadyKey is that conversation.
 *
 * Dependencies:
 *     - @motet/ui-common live-turn predicates
 *
 * Usage:
 *     if (shouldApplyHistory({ pendingKey, activeKey, storeReadyKey, ... })) {
 *       setMessages(messageInfos);
 *     }
 *
 * Notes:
 *     - storeReadyKey is the conversation id whose setMessages identity is
 *       current. It updates only when useXChat swaps stores.
 *     - shouldClearLiveTurn is false while the owner stream is still
 *       active, including when the visible chat is a different thread.
 */

import {
  shouldClearLiveTurn,
  shouldKeepLiveStreamOverHistory,
} from "@motet/ui-common/utils";

export { shouldClearLiveTurn, shouldKeepLiveStreamOverHistory };

/**
 * True when Chat Explorer may call GET /conversations/:id.
 *
 * Skip while this chat owns the live stream — the result is discarded and
 * a GET per SSE frame saturates workers. Poll only when viewing a spawn
 * child of an in-flight parent (brief at mint, reply after join).
 */
export function shouldFetchConversationHistory(args: {
  canFetch: boolean;
  ownerStreamLive: boolean;
  viewingChildDuringParentTurn: boolean;
}): boolean {
  if (!args.canFetch) return false;
  if (args.viewingChildDuringParentTurn) return true;
  return !args.ownerStreamLive;
}

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
  streamingConversationKey?: string | null;
  liveStreamActive?: boolean;
  ownerLiveActive?: boolean;
}): boolean {
  if (!isHistoryStoreReady(args.pendingKey, args.activeKey, args.storeReadyKey)) {
    return false;
  }
  const streamingKey = (args.streamingConversationKey || "").trim();
  const ownerTurnInFlight =
    !!args.ownerLiveActive ||
    (!streamingKey && args.isRequesting) ||
    (streamingKey === args.pendingKey &&
      (args.isRequesting || !!args.liveStreamActive));
  if (ownerTurnInFlight) {
    return false;
  }
  if (!args.alreadyHydrated && args.localMessageCount > 0) {
    const streaming = (args.streamingConversationKey || "").trim();
    if (streaming && streaming !== args.pendingKey) {
      return true;
    }
    return false;
  }
  return true;
}

/** First user-message body in a useXChat store (or empty). */
export function firstUserMessageText(messages: unknown[]): string {
  const firstUserMsg = (messages as Array<{ role?: string; message?: { role?: string; content?: unknown } }>).find(
    (msgInfo) => {
      const msg = msgInfo?.message || msgInfo;
      return msg?.role === "user";
    }
  );
  const raw = firstUserMsg ? firstUserMsg.message || firstUserMsg : null;
  const content = raw && "content" in raw ? raw.content : undefined;
  return typeof content === "string" ? content.trim() : "";
}

/** Sidebar / PATCH title from a user message (one line, capped). */
export function autoTitleFromUserText(content: string): string {
  const oneLine = content.replace(/\s+/g, " ").trim();
  return oneLine.length > 500 ? oneLine.slice(0, 500) : oneLine;
}

/** True when onRequest may write into this conversation's useXChat store. */
export function shouldWriteSendToStore(
  storeReadyKey: string | null,
  conversationId: string,
): boolean {
  const cid = String(conversationId || "").trim();
  return !!cid && storeReadyKey === cid;
}

/**
 * True when the first local user message may set the sidebar label to a
 * snippet of that message. Requires the useXChat store to belong to the
 * active conversation so a lagged store from the previous thread cannot
 * title a brand-new "+" chat. Pass the unthrottled store — never the
 * 50ms-throttled list.
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
 * True when this send may title the conversation. Uses the outgoing text
 * and conversation key — not the message store.
 */
export function shouldQueueAutoTitleFromSend(args: {
  label: string | undefined;
  alreadyUpdated: boolean;
  hasUserMessage: boolean;
}): boolean {
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
 * Pending auto-titles that may PATCH. Each key persists only after that
 * conversation has been in flight (agent_turn claimed the id) and is idle.
 * A sibling chat still streaming does not block or steal another key.
 */
export function pendingTitlesToFlush(args: {
  pending: ReadonlyMap<string, string>;
  inFlightIds: ReadonlySet<string>;
  seenInFlight: ReadonlySet<string>;
}): Array<{ key: string; title: string }> {
  const out: Array<{ key: string; title: string }> = [];
  for (const [key, title] of args.pending) {
    if (!key || !title) continue;
    if (args.inFlightIds.has(key)) continue;
    if (!args.seenInFlight.has(key)) continue;
    out.push({ key, title });
  }
  return out;
}

type MessageInfoLike = {
  status?: string;
  role?: string;
  message?: {
    status?: string;
    role?: string;
    meta?: {
      agentStreams?: Record<string, { toolSummaries?: unknown[]; toolExecutions?: unknown[] }>;
    };
  };
};

/** True when any restored or live message has at least one agent stream. */
export function messageInfosHaveAgentStreams(infos: MessageInfoLike[]): boolean {
  return infos.some((info) => {
    const streams = info?.message?.meta?.agentStreams;
    return !!streams && Object.keys(streams).length > 0;
  });
}

function eachAgentSlice(
  infos: MessageInfoLike[],
  visit: (slice: { toolSummaries?: unknown[]; toolExecutions?: unknown[] }) => boolean
): boolean {
  for (const info of infos) {
    const streams = info?.message?.meta?.agentStreams;
    if (!streams || typeof streams !== "object") continue;
    for (const slice of Object.values(streams)) {
      if (visit(slice || {})) return true;
    }
  }
  return false;
}

/** True when any agent stream already has persisted toolSummaries (reload shape). */
export function messageInfosHaveToolSummaries(infos: MessageInfoLike[]): boolean {
  return eachAgentSlice(
    infos,
    (slice) => Array.isArray(slice.toolSummaries) && slice.toolSummaries.length > 0
  );
}

/** True when any live SSE slice still has toolExecutions from this session. */
export function messageInfosHaveToolExecutions(infos: MessageInfoLike[]): boolean {
  return eachAgentSlice(
    infos,
    (slice) => Array.isArray(slice.toolExecutions) && slice.toolExecutions.length > 0
  );
}

/** True when an assistant bubble is still streaming. */
export function messageInfosHaveStreamingAssistant(infos: MessageInfoLike[]): boolean {
  return infos.some((info) => {
    const role = info?.message?.role || info?.role;
    const status = info?.status || info?.message?.status;
    return role === "assistant" && (status === "updating" || status === "loading");
  });
}

