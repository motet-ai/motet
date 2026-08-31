/**
 * Motet UI Common - Conversations API Client
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-31
 *
 * Description:
 *     Typed client for /api/v1/conversations (list, get, rename, delete).
 *     Pure fetch-based — no framework dependencies. mapHistoryToMessages
 *     groups consecutive attributed assistant rows into one turn.
 *
 * Usage:
 *     import { listConversations, getConversation } from "@motet/ui-common/api";
 */

import type { SpawnChildCard, ToolSummaryRow } from "../types";
import { groupTranscriptAssistantTurns } from "../utils/assistantTurn";
import { knownCostUsd } from "../utils/formatting";

// ─────────────────────────────────────────────────────────────────────────────
// RESPONSE TYPES
// ─────────────────────────────────────────────────────────────────────────────

export type ConversationItem = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  /** Agent scope (e.g. core.default) */
  agent_id?: string | null;
  /** Surface/channel (e.g. demo_chat, ops_dashboard) */
  surface_id?: string | null;
  /** Follow-up agent when it differs from list-scope agent_id */
  turn_agent_id?: string | null;
  /** Immediate parent, or null for a root chat */
  parent_conversation_id: string | null;
};

export type ConversationListResponse = {
  conversations: ConversationItem[];
};

export type ConversationHistoryAttachment = {
  artifact_id: string;
  content_type: string;
  filename?: string;
  bytes?: number;
};

export type ConversationHistoryItem = {
  content?: string;
  text?: string;
  role?: string;
  created_at?: string;
  /** Qualified agent id when present */
  agent_id?: string;
  /** Immediate parent agent when this row is a nested loop */
  parent_agent_id?: string;
  /** Provider reasoning stored for this assistant turn (reload display) */
  thinking_text?: string;
  /** Short tool name/status/preview for this assistant turn (reload display) */
  tool_summaries?: ToolSummaryRow[];
  /** Priced estimate for this assistant row; omitted when unknown */
  cost_usd?: number;
  /** Isolated spawn-child conversations created during this assistant turn */
  spawn_children?: SpawnChildCard[];
  /** Artifact references for media (e.g. images) so UI can display them */
  attachments?: ConversationHistoryAttachment[];
};

export type ConversationDetailResponse = {
  conversation_id: string;
  history: ConversationHistoryItem[];
  counts: { memory?: number; vector?: number };
  summary?: string | null;
  warning?: string | null;
  /** Priced conversation rollup; omitted when unknown */
  cost_usd?: number | null;
  /** Follow-up agent when it differs from the list-scope agent */
  turn_agent_id?: string | null;
  /** Immediate parent, or null for a root chat */
  parent_conversation_id: string | null;
};

// ─────────────────────────────────────────────────────────────────────────────
// REQUEST TYPES
// ─────────────────────────────────────────────────────────────────────────────

/** Optional scope for list (agent_id, surface_id). */
export type ListConversationsParams = {
  agent_id?: string | null;
  surface_id?: string | null;
};

// ─────────────────────────────────────────────────────────────────────────────
// API FUNCTIONS
// ─────────────────────────────────────────────────────────────────────────────

const BASE = "/api/v1/conversations";

/**
 * List conversations for the current principal.
 * Pass agent_id and surface_id to scope the list.
 * On 401, returns null so the app can still work (e.g. new chat).
 */
export async function listConversations(
  headers: Record<string, string>,
  params?: ListConversationsParams
): Promise<ConversationItem[] | null> {
  try {
    const search = new URLSearchParams();
    if (params?.agent_id != null && params.agent_id !== "") search.set("agent_id", params.agent_id);
    if (params?.surface_id != null && params.surface_id !== "") search.set("surface_id", params.surface_id);
    const url = search.toString() ? `${BASE}?${search.toString()}` : BASE;
    const r = await fetch(url, { headers });
    if (!r.ok) {
      if (r.status === 401) return null;
      throw new Error(`${r.status} ${r.statusText}`);
    }
    const data: ConversationListResponse = await r.json();
    return data.conversations ?? [];
  } catch {
    return null;
  }
}

/**
 * Get one conversation (history and metadata). Returns null on 404 or error.
 */
export async function getConversation(
  conversationId: string,
  headers: Record<string, string>
): Promise<ConversationDetailResponse | null> {
  try {
    const r = await fetch(`${BASE}/${conversationId}`, { headers });
    if (!r.ok) {
      if (r.status === 404) return null;
      throw new Error(`${r.status} ${r.statusText}`);
    }
    return (await r.json()) as ConversationDetailResponse;
  } catch {
    return null;
  }
}

/**
 * Rename a conversation (update display title). Throws on error.
 */
export async function updateConversationTitle(
  conversationId: string,
  title: string,
  headers: Record<string, string>
): Promise<{ conversation_id: string; title: string }> {
  const r = await fetch(`${BASE}/${conversationId}`, {
    method: "PATCH",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ title: title.trim() }),
  });
  if (!r.ok) {
    const msg = (await r.json().catch(() => ({}))).detail ?? `${r.status} ${r.statusText}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return (await r.json()) as { conversation_id: string; title: string };
}

/**
 * Delete a conversation (registry + memory/vector). Throws on error.
 */
export async function deleteConversation(
  conversationId: string,
  headers: Record<string, string>
): Promise<{ conversation_id: string; cleared: { memory?: number; vector?: number } }> {
  const r = await fetch(`${BASE}/${conversationId}`, {
    method: "DELETE",
    headers,
  });
  if (!r.ok) {
    const msg = (await r.json().catch(() => ({}))).detail ?? `${r.status} ${r.statusText}`;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return (await r.json()) as { conversation_id: string; cleared: { memory?: number; vector?: number } };
}

/**
 * Map API history items to chat message shape (role, content, attachments).
 * Excludes tool/system messages so only user-facing transcript is returned.
 * Consecutive attributed assistant rows from one user turn are folded into a
 * single message with reconstructed agentStreams (selected agent, else last row).
 * thinking_text, tool_summaries, cost_usd, and spawn_children on a history item
 * are copied onto that agent's stream / the parent message.
 */
export function mapHistoryToMessages(
  history: ConversationHistoryItem[],
  selectedAgentId?: string
): Array<{
  role: "user" | "assistant";
  content: string;
  meta?: Record<string, unknown>;
  attachments?: Array<{
    artifact_id: string;
    content_type: string;
    filename?: string;
    bytes?: number;
    status: "ready";
  }>;
}> {
  const mapped = (history || [])
    .filter((m) => {
      const role = (m.role || "user") as string;
      if (role === "tool" || role === "system") return false;
      const thinking = (m.thinking_text || "").trim();
      const summaries = Array.isArray(m.tool_summaries) ? m.tool_summaries : [];
      const cost = knownCostUsd(m.cost_usd);
      const spawnChildren = Array.isArray(m.spawn_children) ? m.spawn_children : [];
      if (role === "assistant" && !(m.content ?? m.text ?? "").trim() && !thinking && summaries.length === 0 && cost == null && spawnChildren.length === 0) return false;
      return true;
    })
    .map((m) => {
      const attachments = m.attachments?.map((a) => ({
        artifact_id: a.artifact_id,
        content_type: a.content_type,
        filename: a.filename ?? "image",
        bytes: a.bytes ?? 0,
        status: "ready" as const,
      }));
      const thinking = (m.thinking_text || "").trim();
      const summaries = Array.isArray(m.tool_summaries) ? m.tool_summaries : [];
      const cost = knownCostUsd(m.cost_usd);
      const spawnChildren = Array.isArray(m.spawn_children) ? m.spawn_children : [];
      return {
        role: (m.role || "user") as "user" | "assistant",
        content: m.content ?? m.text ?? "",
        ...((m.agent_id || m.parent_agent_id || thinking || summaries.length || cost != null || spawnChildren.length)
          ? {
              meta: {
                ...(m.agent_id ? { agent_id: m.agent_id } : {}),
                ...(m.parent_agent_id ? { parent_agent_id: m.parent_agent_id } : {}),
                ...(thinking ? { thinking_text: thinking } : {}),
                ...(summaries.length ? { tool_summaries: summaries } : {}),
                ...(cost != null ? { cost_usd: cost } : {}),
                ...(spawnChildren.length ? { spawn_children: spawnChildren } : {}),
              },
            }
          : {}),
        ...(attachments?.length ? { attachments } : {}),
      };
    });
  return groupTranscriptAssistantTurns(mapped, selectedAgentId);
}
