/**
 * Motet UI Common - Assistant Turn Slices
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Pure helpers that turn per-agent stream buckets into one assistant-turn
 *     layout: spawned / peer agents first, selected (or first non-spawn)
 *     agent last. Chat Explorer renders that as a single bubble with nested
 *     sub-agent sections.
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
 *       user rows become one message with reconstructed agentStreams (no thinking)
 */

import { resolveAgentDisplayName, isSpawnAgentId, type AgentRegistryEntry } from "./agents";

export type AssistantTurnSlice = {
  agentKey: string;
  agentName: string;
  text: string;
  thinkingText: string;
  thinkingComplete: boolean;
  isPrimary: boolean;
};

type AgentStreamView = {
  contentText?: string;
  contentComplete?: boolean;
  thinkingText?: string;
  thinkingComplete?: boolean;
  parent_agent_id?: string;
};

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
    slices.push({
      agentKey,
      agentName,
      text,
      thinkingText,
      thinkingComplete: !!stream.thinkingComplete,
      isPrimary: agentKey === primaryKey,
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
    streams[aid] = {
      contentText: prev?.contentText ? `${prev.contentText}\n\n${text}` : text,
      contentComplete: true,
      thinkingComplete: true,
      ...(parent ? { parent_agent_id: parent } : {}),
    };
  }
  const keys = Object.keys(streams);
  const primaryKey = resolveTranscriptPrimaryAgentKey(keys, selectedAgentId) || keys[0];
  const primaryText = (primaryKey && streams[primaryKey]?.contentText) || group[group.length - 1].content;
  const attachments = mergeAttachments(group);
  return {
    role: "assistant",
    content: primaryText,
    meta: {
      agent_id: primaryKey,
      agentStreams: streams,
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
  if (!text) return null;
  const aid = typeof msg.meta?.agent_id === "string" ? msg.meta.agent_id.trim() : "";
  if (!aid) return null;
  const { agentName } = resolveAgentDisplayName(aid, agents);
  return {
    agentKey: aid,
    agentName,
    text,
    thinkingText: "",
    thinkingComplete: true,
    isPrimary: true,
  };
}
