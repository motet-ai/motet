/**
 * Motet UI Common - Live Turn Registry
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Framework-agnostic map of in-flight chat turns keyed by conversation
 *     id. Each conversation reduces its own SSE into its own message.
 *     The visible thread overlays that conversation's turn, or a projected
 *     spawn child, and never another chat's stream.
 *
 * Dependencies:
 *     - reduceChatEvent for SSE folding
 *     - resolveDisplayedLiveMessage for owner vs spawn-child vs unrelated
 *
 * Usage:
 *     const turns = new LiveTurnRegistry();
 *     turns.start("conv-a");
 *     turns.applyChunk("conv-a", { event: "token", data: { t: "Hi" } });
 *     const shown = turns.overlayFor(visibleConversationId);
 *     if (turns.isBusy(visibleConversationId)) disableComposer();
 *
 * Notes:
 *     - start() resets only that conversation; other turns keep running
 *     - end/error mark the turn inactive; clear() drops it after the owner
 *       has been shown the final message
 */

import { reduceChatEvent, type ChatMessage, type ChatOutput, type ReduceResult } from "../api/chat";
import { asSpawnChildCards, resolveDisplayedLiveMessage } from "./assistantTurn";

export type LiveTurn = {
  conversationId: string;
  message: ChatMessage;
  active: boolean;
};

/** True when a live message has something to show besides a loading bubble. */
export function isRenderableLiveMessage(message: ChatMessage | null | undefined): boolean {
  if (!message) return false;
  if (String(message.content || "").trim()) return true;
  const meta = message.meta;
  if (!meta || typeof meta !== "object") return false;
  if (Array.isArray(meta.spawn_children) && meta.spawn_children.length > 0) return true;
  const streams = meta.agentStreams;
  if (!streams || typeof streams !== "object") return false;
  return Object.values(streams).some((slice) => {
    const row = (slice || {}) as {
      contentText?: string;
      thinkingText?: string;
      toolSummaries?: unknown[];
      toolExecutions?: unknown[];
    };
    return (
      String(row.contentText || "").trim().length > 0 ||
      String(row.thinkingText || "").trim().length > 0 ||
      (Array.isArray(row.toolSummaries) && row.toolSummaries.length > 0) ||
      (Array.isArray(row.toolExecutions) && row.toolExecutions.length > 0)
    );
  });
}

export function chatOutputConversationId(chunk: unknown): string {
  if (!chunk || typeof chunk !== "object") return "";
  const id = (chunk as { conversation_id?: unknown }).conversation_id;
  return typeof id === "string" ? id.trim() : "";
}

export function tagChatOutputConversation(
  chunk: ChatOutput,
  conversationId: string
): ChatOutput & { conversation_id: string } {
  return { ...chunk, conversation_id: conversationId };
}

/**
 * True when the visible chat owns this turn and the SSE has finished.
 * Leaving mid-turn must not clear another conversation's slot.
 */
export function shouldClearLiveTurn(args: {
  displayConversationId: string;
  streamConversationId: string | null | undefined;
  displayIsRequesting: boolean;
  streamIsActive?: boolean;
}): boolean {
  const stream = String(args.streamConversationId || "").trim();
  const display = String(args.displayConversationId || "").trim();
  if (!stream || !display) return false;
  if (args.streamIsActive) return false;
  return display === stream && !args.displayIsRequesting;
}

/**
 * True when the in-memory SSE store should win over a GET history snapshot.
 */
export function shouldKeepLiveStreamOverHistory(args: {
  liveHasAgentStreams: boolean;
  liveHasToolSummaries: boolean;
  liveHasToolExecutions: boolean;
  liveIsStreaming: boolean;
  historyHasToolSummaries: boolean;
}): boolean {
  if (!args.liveHasAgentStreams) return false;
  if (args.liveIsStreaming || args.liveHasToolExecutions) return true;
  if (args.historyHasToolSummaries && !args.liveHasToolSummaries) return false;
  return true;
}

export class LiveTurnRegistry {
  private turns = new Map<string, LiveTurn>();
  private listeners = new Set<() => void>();

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private emit(): void {
    this.listeners.forEach((listener) => listener());
  }

  start(conversationId: string): void {
    const id = String(conversationId || "").trim();
    if (!id) return;
    this.turns.set(id, {
      conversationId: id,
      message: { role: "assistant", content: "", status: "loading" },
      active: true,
    });
    this.emit();
  }

  applyChunk(
    conversationId: string,
    chunk: ChatOutput | undefined,
    origin?: ChatMessage
  ): ReduceResult {
    const id = String(conversationId || "").trim();
    const prev = id ? this.turns.get(id) : undefined;
    const result = reduceChatEvent(prev?.message || origin, chunk);
    if (!id) return result;
    const eventName =
      chunk && typeof chunk.event === "string" ? chunk.event.trim() : "";
    const active = !!chunk && eventName !== "end" && eventName !== "error";
    this.turns.set(id, {
      conversationId: id,
      message: result.message,
      active,
    });
    this.emit();
    return result;
  }

  markInactive(conversationId: string): void {
    const id = String(conversationId || "").trim();
    const prev = this.turns.get(id);
    if (!prev || !prev.active) return;
    this.turns.set(id, { ...prev, active: false });
    this.emit();
  }

  clear(conversationId: string): void {
    const id = String(conversationId || "").trim();
    if (!id || !this.turns.has(id)) return;
    this.turns.delete(id);
    this.emit();
  }

  get(conversationId: string): LiveTurn | null {
    const id = String(conversationId || "").trim();
    return (id && this.turns.get(id)) || null;
  }

  isActive(conversationId: string): boolean {
    return !!this.get(conversationId)?.active;
  }

  hasActive(): boolean {
    for (const turn of this.turns.values()) {
      if (turn.active) return true;
    }
    return false;
  }

  overlayFor(displayConversationId: string): ChatMessage | null {
    const display = String(displayConversationId || "").trim();
    if (!display) return null;
    for (const turn of this.turns.values()) {
      const shown = resolveDisplayedLiveMessage(
        turn.message,
        display,
        turn.conversationId,
        null
      );
      if (shown && isRenderableLiveMessage(shown)) return shown;
    }
    return null;
  }

  overlayOwner(displayConversationId: string): string | null {
    const display = String(displayConversationId || "").trim();
    if (!display) return null;
    if (this.turns.has(display)) return display;
    for (const turn of this.turns.values()) {
      const shown = resolveDisplayedLiveMessage(
        turn.message,
        display,
        turn.conversationId,
        null
      );
      if (shown) return turn.conversationId;
    }
    return null;
  }

  isBusy(displayConversationId: string): boolean {
    const owner = this.overlayOwner(displayConversationId);
    return !!owner && this.isActive(owner);
  }

  /** Owner ids and spawn-child ids for turns that are still streaming. */
  inFlightIds(): string[] {
    const ids = new Set<string>();
    for (const turn of this.turns.values()) {
      if (!turn.active) continue;
      ids.add(turn.conversationId);
      for (const card of asSpawnChildCards(turn.message.meta?.spawn_children)) {
        ids.add(card.child_conversation_id);
      }
    }
    return [...ids];
  }
}
