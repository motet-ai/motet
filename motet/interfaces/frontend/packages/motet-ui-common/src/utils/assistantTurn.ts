/**
 * Motet UI Common - Assistant Turn Slices
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Pure helpers that turn per-agent stream buckets into one assistant-turn
 *     layout: spawned / peer agents first, selected (or first non-spawn)
 *     agent last. Chat Explorer renders spawn children as cards that open
 *     the isolated child conversation, and in-thread peer agents (panelists)
 *     as speaker blocks with name, thinking, and full reply.
 *
 * Dependencies:
 *     - Agent display name resolution from the registry (and spawn labels)
 *
 * Usage:
 *     const slices = assistantTurnSlices(msg.meta, agents, selectedAgentId);
 *     const children = slices.filter((s) => !s.isPrimary);
 *     const primary = slices.find((s) => s.isPrimary);
 *     const restored = groupTranscriptAssistantTurns(mappedHistory, selectedAgentId);
 *
 * Notes:
 *     - A slice is included when it has reply text or thinking text
 *     - Primary is the selected chat agent when that id is in the stream;
 *       otherwise the first non-spawn key, then the first key
 *     - Reloaded history is grouped: consecutive attributed assistants between
 *       user rows become one message with reconstructed agentStreams. thinking_text
 *       and tool_summaries on a history row are copied onto that agent's stream.
 *       spawn_children also seed those streams so the parent rail can restore
 *       sub-agent steps and cost after reload. A live parent stream is only
 *       shown on that parent or a projected spawn child — never an unrelated
 *       conversation.
 */

import type { SpawnChildCard, ToolSummaryRow } from "../types";
import { resolveAgentDisplayName, isSpawnAgentId, type AgentRegistryEntry } from "./agents";
import { knownCostUsd } from "./formatting";

export type AssistantTurnSlice = {
  agentKey: string;
  agentName: string;
  text: string;
  thinkingText: string;
  thinkingComplete: boolean;
  isPrimary: boolean;
  childConversationId?: string;
};

type AgentStreamView = {
  contentText?: string;
  contentComplete?: boolean;
  thinkingText?: string;
  thinkingComplete?: boolean;
  toolSummaries?: ToolSummaryRow[];
  costUsd?: number;
  parent_agent_id?: string;
  childConversationId?: string;
};

function asToolSummaries(raw: unknown): ToolSummaryRow[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((row): row is ToolSummaryRow => {
    return !!row && typeof row === "object" && typeof (row as ToolSummaryRow).tool_name === "string";
  });
}

/** Sanitize spawn card pointers from a history or live message. */
export function asSpawnChildCards(raw: unknown): SpawnChildCard[] {
  if (!Array.isArray(raw)) return [];
  const out: SpawnChildCard[] = [];
  for (const row of raw) {
    if (!row || typeof row !== "object") continue;
    const childId = String((row as SpawnChildCard).child_conversation_id || "").trim();
    const title = String((row as SpawnChildCard).title || "").trim();
    if (!childId) continue;
    const agentId = String((row as SpawnChildCard).agent_id || "").trim();
    const turnAgentId = String((row as SpawnChildCard).turn_agent_id || "").trim();
    const preview = String((row as SpawnChildCard).preview || "").trim();
    const cost = knownCostUsd((row as SpawnChildCard).cost_usd);
    const thinking = String((row as SpawnChildCard).thinking_text || "").trim();
    const summaries = asToolSummaries((row as SpawnChildCard).tool_summaries);
    out.push({
      child_conversation_id: childId,
      title: title || "Sub-agent",
      ...(agentId ? { agent_id: agentId } : {}),
      ...(turnAgentId ? { turn_agent_id: turnAgentId } : {}),
      ...(preview ? { preview } : {}),
      ...(cost != null ? { cost_usd: cost } : {}),
      ...(thinking ? { thinking_text: thinking } : {}),
      ...(summaries.length ? { tool_summaries: summaries } : {}),
    });
  }
  return out;
}

/**
 * Cards for a parent turn. Prefer persisted or streamed ``spawn_children``.
 * Live slices may carry ``childConversationId`` after the spawn event.
 */
/** Agent stream key for an isolated spawn child on a live parent turn. */
export function spawnAgentKeyForChildConversation(
  meta: Record<string, unknown> | undefined,
  childConversationId: string
): string {
  const childId = String(childConversationId || "").trim();
  if (!childId || !meta) return "";
  for (const card of asSpawnChildCards(meta.spawn_children)) {
    if (card.child_conversation_id === childId && card.agent_id) {
      return card.agent_id;
    }
  }
  const streams = meta.agentStreams as Record<string, AgentStreamView> | undefined;
  if (!streams) return "";
  for (const [agentKey, stream] of Object.entries(streams)) {
    if (String(stream?.childConversationId || "").trim() === childId) {
      return agentKey;
    }
  }
  return "";
}

/**
 * One-agent assistant message for a spawn child, projected from the parent
 * turn that is still streaming. The child view uses this so thinking, tokens,
 * and tool steps update live without leaving the parent stream.
 */
export function projectLiveSpawnChildMessage(
  parentMessage: {
    role?: string;
    content?: string;
    status?: string;
    meta?: Record<string, unknown>;
  } | null | undefined,
  childConversationId: string
): { role: "assistant"; content: string; status: "updating" | "success"; meta: Record<string, unknown> } | null {
  const childId = String(childConversationId || "").trim();
  if (!parentMessage || !childId) return null;
  const meta = (parentMessage.meta || {}) as Record<string, unknown>;
  const agentKey = spawnAgentKeyForChildConversation(meta, childId);
  if (!agentKey) return null;
  const streams = (meta.agentStreams || {}) as Record<string, AgentStreamView>;
  const stream = { ...(streams[agentKey] || {}), childConversationId: childId };
  const parentStatus = String(parentMessage.status || "");
  return {
    role: "assistant",
    content: String(stream.contentText || ""),
    status: parentStatus === "success" || parentStatus === "error" ? "success" : "updating",
    meta: {
      agentStreams: { [agentKey]: stream },
    },
  };
}

/**
 * What the visible conversation should show from an in-flight parent stream.
 * Unrelated chats get ``originMessage`` (or null) so spawn children do not
 * bleed into another thread's bubble or right rail.
 */
export function resolveDisplayedLiveMessage<
  T extends {
    role?: string;
    content?: string;
    status?: string;
    meta?: Record<string, unknown>;
  },
>(
  liveMessage: T,
  displayConversationId: string,
  streamConversationId: string,
  originMessage?: T | null
): T | null {
  const display = String(displayConversationId || "").trim();
  const stream = String(streamConversationId || "").trim();
  if (!display || !stream) {
    return originMessage ?? null;
  }
  if (display === stream) {
    return liveMessage;
  }
  const projected = projectLiveSpawnChildMessage(liveMessage, display);
  if (projected) {
    return projected as T;
  }
  return originMessage ?? null;
}

export function spawnCardsForTurn(
  meta: Record<string, unknown> | undefined,
  _parentConversationId: string,
  childSlices: AssistantTurnSlice[]
): SpawnChildCard[] {
  const stored = asSpawnChildCards(meta?.spawn_children);
  if (stored.length) return stored;
  const cards: SpawnChildCard[] = [];
  for (const slice of childSlices) {
    const childId = String(slice.childConversationId || "").trim();
    if (!childId) continue;
    const preview = (slice.text || "").replace(/\s+/g, " ").trim();
    cards.push({
      child_conversation_id: childId,
      agent_id: slice.agentKey,
      title: slice.agentName,
      ...(preview ? { preview: preview.slice(0, 160) } : {}),
    });
  }
  return cards;
}

/**
 * In-thread peer agents (expert-panel, roundtable invitees) — not spawn
 * children. Chat Explorer renders these as speaker blocks in this conversation.
 */
export function peerSpeakerSlices(
  childSlices: AssistantTurnSlice[],
  spawnCards: SpawnChildCard[] = []
): AssistantTurnSlice[] {
  const spawnKeys = new Set(
    spawnCards
      .map((card) => String(card.agent_id || "").trim())
      .filter(Boolean)
  );
  const spawnCids = new Set(
    spawnCards.map((card) => String(card.child_conversation_id || "").trim()).filter(Boolean)
  );
  return childSlices.filter((slice) => {
    if (isSpawnAgentId(slice.agentKey)) return false;
    if (spawnKeys.has(slice.agentKey)) return false;
    const childId = String(slice.childConversationId || "").trim();
    if (childId && spawnCids.has(childId)) return false;
    return !!(slice.text.trim() || slice.thinkingText.trim());
  });
}

/** Minimal chat row used when folding GET /conversations history into one turn. */
export type TranscriptHistoryMessage = {
  role: "user" | "assistant";
  content: string;
  meta?: Record<string, unknown>;
  attachments?: unknown[];
};

/** Pick the turn's main agent from stream keys and the selected chat agent. */
export function resolvePrimaryAgentKey(
  keys: string[],
  selectedAgentId?: string
): string | undefined {
  if (keys.length === 0) return undefined;
  const selected = (selectedAgentId || "").trim();
  if (selected && keys.includes(selected)) return selected;
  const firstNonSpawn = keys.find((key) => !isSpawnAgentId(key));
  return firstNonSpawn || keys[0];
}

/**
 * Primary for a reloaded turn. Selected chat agent when it wrote a row;
 * otherwise the last key (backend emits sub-agents, then the root).
 */
export function resolveTranscriptPrimaryAgentKey(
  keys: string[],
  selectedAgentId?: string
): string | undefined {
  if (keys.length === 0) return undefined;
  const selected = (selectedAgentId || "").trim();
  if (selected && keys.includes(selected)) return selected;
  return keys[keys.length - 1];
}

function messageAgentId(msg: TranscriptHistoryMessage): string {
  const aid = msg.meta?.agent_id;
  return typeof aid === "string" ? aid.trim() : "";
}

/**
 * Per-agent slices for one assistant turn (children first, primary last).
 */
export function assistantTurnSlices(
  meta: Record<string, unknown> | undefined,
  agents: AgentRegistryEntry[],
  selectedAgentId?: string
): AssistantTurnSlice[] {
  if (!meta) return [];
  const streams = meta.agentStreams as Record<string, AgentStreamView> | undefined;
  if (!streams) return [];
  const keys = Object.keys(streams);
  if (keys.length === 0) return [];

  const primaryKey = resolvePrimaryAgentKey(keys, selectedAgentId);
  const slices: AssistantTurnSlice[] = [];

  for (const agentKey of keys) {
    const stream = streams[agentKey] || {};
    const text = (stream.contentText || "").trim();
    const thinkingText = String(stream.thinkingText || "");
    if (!text && !thinkingText) continue;
    const { agentName } = resolveAgentDisplayName(agentKey, agents);
    const childConversationId = String(stream.childConversationId || "").trim();
    slices.push({
      agentKey,
      agentName,
      text,
      thinkingText,
      thinkingComplete: !!stream.thinkingComplete,
      isPrimary: agentKey === primaryKey,
      ...(childConversationId ? { childConversationId } : {}),
    });
  }

  slices.sort((a, b) => Number(a.isPrimary) - Number(b.isPrimary));
  return slices;
}

function mergeAttachments(
  messages: TranscriptHistoryMessage[]
): TranscriptHistoryMessage["attachments"] {
  const seen = new Set<string>();
  const out: unknown[] = [];
  for (const msg of messages) {
    for (const att of msg.attachments || []) {
      const id =
        att && typeof att === "object" && "artifact_id" in att
          ? String((att as { artifact_id?: string }).artifact_id || "")
          : "";
      const key = id || JSON.stringify(att);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(att);
    }
  }
  return out.length > 0 ? out : undefined;
}

/** Seed parent-turn streams from persisted spawn cards so reload matches live rails. */
function applySpawnChildrenToStreams(
  streams: Record<string, AgentStreamView>,
  cards: SpawnChildCard[]
): void {
  for (const card of cards) {
    const aid = String(card.agent_id || "").trim();
    if (!aid) continue;
    const prev = streams[aid] || {};
    const thinking = (card.thinking_text || prev.thinkingText || "").trim();
    const summaries = asToolSummaries(card.tool_summaries);
    const costUsd = knownCostUsd(card.cost_usd) ?? prev.costUsd;
    streams[aid] = {
      ...prev,
      contentText: prev.contentText || card.preview || "",
      contentComplete: true,
      thinkingText: prev.thinkingText || thinking,
      thinkingComplete: true,
      toolSummaries: prev.toolSummaries?.length ? prev.toolSummaries : summaries,
      childConversationId: card.child_conversation_id,
      ...(costUsd != null ? { costUsd } : {}),
    };
  }
}

function mergeAssistantGroup(
  group: TranscriptHistoryMessage[],
  selectedAgentId?: string
): TranscriptHistoryMessage {
  const streams: Record<string, AgentStreamView> = {};
  for (const msg of group) {
    const aid = messageAgentId(msg);
    if (!aid) continue;
    const text = (msg.content || "").trim();
    const prev = streams[aid];
    const parentRaw = msg.meta?.parent_agent_id;
    const parent = typeof parentRaw === "string" ? parentRaw.trim() : "";
    const thinkingRaw = msg.meta?.thinking_text;
    const thinkingText = typeof thinkingRaw === "string" ? thinkingRaw : "";
    const summaries = asToolSummaries(msg.meta?.tool_summaries);
    const costUsd = knownCostUsd(msg.meta?.cost_usd) ?? prev?.costUsd;
    streams[aid] = {
      contentText: prev?.contentText ? `${prev.contentText}\n\n${text}` : text,
      contentComplete: true,
      thinkingText: prev?.thinkingText
        ? thinkingText
          ? `${prev.thinkingText}\n\n${thinkingText}`
          : prev.thinkingText
        : thinkingText,
      thinkingComplete: true,
      toolSummaries: prev?.toolSummaries?.length
        ? summaries.length
          ? [...prev.toolSummaries, ...summaries]
          : prev.toolSummaries
        : summaries,
      ...(costUsd != null ? { costUsd } : {}),
      ...(parent ? { parent_agent_id: parent } : {}),
    };
  }
  const keys = Object.keys(streams);
  const primaryKey = resolveTranscriptPrimaryAgentKey(keys, selectedAgentId) || keys[0];
  const primaryText = (primaryKey && streams[primaryKey]?.contentText) || group[group.length - 1].content;
  const attachments = mergeAttachments(group);
  const spawnChildren = group.flatMap((msg) => asSpawnChildCards(msg.meta?.spawn_children));
  applySpawnChildrenToStreams(streams, spawnChildren);
  return {
    role: "assistant",
    content: primaryText,
    meta: {
      agent_id: primaryKey,
      agentStreams: streams,
      ...(spawnChildren.length ? { spawn_children: spawnChildren } : {}),
    },
    ...(attachments ? { attachments } : {}),
  };
}

/**
 * Fold consecutive attributed assistant rows (sub-agents, then root) into one
 * message with agentStreams so reload uses the same nested-turn layout as live.
 * Assistants without agent_id stay as their own bubbles.
 */
export function groupTranscriptAssistantTurns<T extends TranscriptHistoryMessage>(
  messages: T[],
  selectedAgentId?: string
): T[] {
  const out: T[] = [];
  let pending: T[] = [];

  const flush = () => {
    if (pending.length === 0) return;
    out.push(mergeAssistantGroup(pending, selectedAgentId) as T);
    pending = [];
  };

  for (const msg of messages) {
    if (msg.role !== "assistant") {
      flush();
      out.push(msg);
      continue;
    }
    if (!messageAgentId(msg)) {
      flush();
      out.push(msg);
      continue;
    }
    pending.push(msg);
  }
  flush();
  return out;
}

/** Fallback slice when restored history only has meta.agent_id + content. */
export function assistantTranscriptTurnSlice(
  msg: { content?: string; meta?: Record<string, unknown> } | undefined,
  agents: AgentRegistryEntry[]
): AssistantTurnSlice | null {
  if (!msg) return null;
  const text = (msg.content || "").trim();
  const thinkingRaw = msg.meta?.thinking_text;
  const thinkingText = typeof thinkingRaw === "string" ? thinkingRaw : "";
  if (!text && !thinkingText) return null;
  const aid = typeof msg.meta?.agent_id === "string" ? msg.meta.agent_id.trim() : "";
  if (!aid) return null;
  const { agentName } = resolveAgentDisplayName(aid, agents);
  return {
    agentKey: aid,
    agentName,
    text,
    thinkingText,
    thinkingComplete: true,
    isPrimary: true,
  };
}
