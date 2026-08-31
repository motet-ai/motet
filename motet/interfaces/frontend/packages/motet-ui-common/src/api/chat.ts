/**
 * Motet UI Common - Chat API Protocol
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-29
 *
 * Description:
 *     Framework-agnostic protocol types and SSE event reducer for the
 *     Motet /api/v1/chat endpoint. Any frontend (React, Vue, plain JS)
 *     can use these types and the reducer to implement a Motet chat client.
 *
 *     The reducer handles all standard Motet SSE event types:
 *       token, thinking, usage, step, workflow_step, reasoning, reasoning_step,
 *       reasoning_meta, conversation_analyzed, turn, end, auth_required,
 *       error, tool_execution_started, tool_execution_completed,
 *       tool_execution_failed.
 *
 *     Budget-stop Continue (issue #188): ``end.stop_reason`` of
 *     ``max_iterations`` / ``max_model_calls`` is stored on message meta so
 *     UIs can offer an explicit Continue action (``continue_after_budget``).
 *
 * Dependencies:
 *     - None (framework-agnostic)
 *
 * Usage:
 *     import {
 *       type ChatMessage, type ChatInput, type ChatOutput,
 *       reduceChatEvent,
 *     } from "@motet/ui-common/api";
 *
 *     const msg = reduceChatEvent(currentMessage, { event: "token", data: { t: "Hello" } });
 */

import { DEFAULT_STREAM_AGENT_KEY } from "../types";
import { asSpawnChildCards } from "../utils/assistantTurn";
import { knownCostUsd, positiveLoopStep } from "../utils/formatting";

function eventCostUsd(data: unknown): number | undefined {
  if (!data || typeof data !== "object") return undefined;
  const amount = knownCostUsd((data as { cost_usd?: unknown }).cost_usd);
  return amount ?? undefined;
}

function withRunningCost(
  prev: Record<string, unknown>,
  data: unknown,
): Record<string, unknown> {
  const costUsd = eventCostUsd(data);
  return costUsd != null ? { ...prev, costUsd } : prev;
}

// ─────────────────────────────────────────────────────────────────────────────
// PROTOCOL TYPES
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Canonical media part (ADR-0064 / ADR-0113). Produced by the backend for
 * artifact-backed media such as generated images. Mirrors the serialized
 * `MediaPart` (exclude_none) shape from the runtime.
 */
export type MediaPart = {
  /** Always "media" for canonical media parts. */
  type?: string;
  /** "image" | "audio" | "video" | "file". */
  media_type?: string;
  /** MIME type, e.g. "image/png". */
  mime_type?: string;
  /** Stored artifact id; fetch bytes via /api/v1/artifacts/{id}/preview. */
  artifact_id?: string;
  /** Optional direct URL (when not artifact-backed). */
  url?: string;
  /** Optional filename / alt text for accessibility. */
  filename?: string;
  alt?: string;
};

/**
 * Chat message in a Motet conversation.
 * Used for both user input and assistant responses.
 */
export type ChatMessage = {
  id?: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  status?: "local" | "loading" | "updating" | "success" | "error";
  taskId?: string;
  meta?: Record<string, any>;
  attachments?: Array<{
    artifact_id: string;
    filename: string;
    content_type: string;
    bytes: number;
  }>;
  /** Assistant-generated media (e.g. images) surfaced on the turn end (ADR-0113). */
  media?: MediaPart[];
};

/** Typed Continue user message after a budget stop (issue #188). */
export const CONTINUE_AFTER_BUDGET_USER_MESSAGE =
  "Continue working on this task.";

/** ``stop_reason`` values that should offer an explicit Continue affordance. */
export const BUDGET_STOP_REASONS = new Set([
  "max_iterations",
  "max_model_calls",
]);

export function isBudgetStopReason(stopReason: unknown): boolean {
  return (
    typeof stopReason === "string" && BUDGET_STOP_REASONS.has(stopReason)
  );
}

/**
 * Request payload for /api/v1/chat.
 */
export type ChatInput = {
  messages: Array<{
    role: string;
    content: string;
    attachments?: Array<{
      artifact_id: string;
      filename: string;
      content_type: string;
      bytes: number;
    }>;
  }>;
  stream: boolean;
  overrides?: Record<string, any>;
  conversation_id?: string;
  agent_id?: string;
  surface_id?: string;
  artifact_rag_scope?: "conversation" | "principal" | "motet";
  artifact_ids?: string[];
  artifact_tags?: string[];
  artifact_collection_id?: string;
  allow_broader_artifact_rag_scope?: boolean;
  /** Issue #188: structured Continue — new turn with a fresh budget. */
  continue_after_budget?: boolean;
  headers?: Record<string, string>;
};

/**
 * Single parsed SSE event from the chat stream.
 */
export type ChatOutput = {
  event?: string;
  data?: any;
};

// ─────────────────────────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────────────────────────

/** Positive loop iteration from an SSE payload or a previous slice. */
export function loopStepFromUnknown(data: unknown, fallback?: number): number | undefined {
  if (data && typeof data === "object") {
    const fromPayload = positiveLoopStep((data as { step?: unknown }).step);
    if (fromPayload != null) return fromPayload;
  }
  return positiveLoopStep(fallback);
}

/**
 * Resolve the per-agent stream key from an SSE event payload.
 * Falls back to DEFAULT_STREAM_AGENT_KEY when no agent_id is present.
 */
export function streamAgentKeyFromData(data: unknown): string {
  if (
    data &&
    typeof data === "object" &&
    typeof (data as { agent_id?: string }).agent_id === "string"
  ) {
    const id = (data as { agent_id: string }).agent_id.trim();
    if (id.length > 0) return id;
  }
  return DEFAULT_STREAM_AGENT_KEY;
}

/**
 * Immutably update one agent's slice inside meta.agentStreams.
 * Returns the resolved agent key and the fully-constructed meta object.
 */
export function withAgentStream(
  base: ChatMessage,
  data: unknown,
  updater: (prevSlice: Record<string, any>) => Record<string, any>,
): { aid: string; meta: Record<string, any> } {
  const aid = streamAgentKeyFromData(data);
  const prevRoot = base.meta || {};
  const streams = { ...(prevRoot.agentStreams || {}) };
  streams[aid] = updater({ ...(streams[aid] || {}) });
  return { aid, meta: attachSpawnChildConversationIds({ ...prevRoot, agentStreams: streams }) };
}

/** Copy ``child_conversation_id`` from spawn cards onto the matching agent stream. */
export function attachSpawnChildConversationIds(meta: Record<string, any>): Record<string, any> {
  const cards = asSpawnChildCards(meta.spawn_children);
  if (!cards.length) return meta;
  const streams = { ...(meta.agentStreams || {}) };
  let changed = false;
  for (const card of cards) {
    const aid = String(card.agent_id || "").trim();
    if (!aid) continue;
    const prev = streams[aid] || {};
    if (prev.childConversationId === card.child_conversation_id) continue;
    streams[aid] = { ...prev, childConversationId: card.child_conversation_id };
    changed = true;
  }
  return changed ? { ...meta, agentStreams: streams } : meta;
}

// ─────────────────────────────────────────────────────────────────────────────
// SSE EVENT REDUCER
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Result of reducing a single SSE event against the current message state.
 */
export type ReduceResult = {
  message: ChatMessage;
  /** Resolved agent key for this event (useful for debug logging). */
  agentKey: string;
};

/**
 * Framework-agnostic reducer that applies a single SSE event to a ChatMessage.
 *
 * Given the current message state and a parsed SSE chunk, returns the updated
 * message. This encodes the canonical Motet SSE protocol so any UI framework
 * can consume the chat stream without reimplementing event handling.
 */
export function reduceChatEvent(
  originMessage: ChatMessage | undefined,
  chunk: ChatOutput | undefined,
): ReduceResult {
  const base: ChatMessage = originMessage || {
    role: "assistant",
    content: "",
  };

  if (!chunk) {
    return { message: { ...base, status: "success" }, agentKey: DEFAULT_STREAM_AGENT_KEY };
  }

  let { event, data } = chunk;

  event = typeof event === "string" ? event.trim() : event;

  if (typeof data === "string") {
    data = data.trim();
    try {
      data = JSON.parse(data);
    } catch {
      // keep as string
    }
  }

  switch (event) {
    case "token": {
      const tok = typeof data === "string" ? data : data?.t ?? "";
      const newContent = `${base.content || ""}${tok}`;
      const { aid, meta } = withAgentStream(base, data, (prev) => ({
        ...prev,
        contentText: `${prev.contentText || ""}${String(tok || "")}`,
      }));
      return {
        message: { ...base, content: newContent, meta, status: "updating" },
        agentKey: aid,
      };
    }

    case "thinking": {
      const text = typeof data === "object" ? data?.text ?? "" : "";
      const isComplete =
        typeof data === "object" ? data?.is_complete ?? false : false;
      const { aid, meta } = withAgentStream(base, data, (prev) => ({
        ...prev,
        thinkingText:
          (prev.thinkingText ?? "") +
          (typeof text === "string" ? text : String(text)),
        thinkingComplete: isComplete,
      }));
      return {
        message: { ...base, meta, status: "updating" },
        agentKey: aid,
      };
    }

    case "usage": {
      const { aid, meta } = withAgentStream(base, data, (prev) =>
        withRunningCost(prev, data),
      );
      return {
        message: { ...base, meta, status: "updating" },
        agentKey: aid,
      };
    }

    case "step":
    case "workflow_step":
    case "reasoning":
    case "reasoning_step":
    case "reasoning_meta":
    case "conversation_analyzed": {
      const { aid, meta } = withAgentStream(base, data, (prev) => {
        const currentStep = loopStepFromUnknown(data, prev.currentStep);
        return {
          ...prev,
          [event as string]: data,
          ...(currentStep != null ? { currentStep } : {}),
        };
      });
      const spawnChildren =
        data && typeof data === "object" && Array.isArray((data as { spawn_children?: unknown }).spawn_children)
          ? (data as { spawn_children: unknown[] }).spawn_children
          : [];
      const nextMeta = spawnChildren.length
        ? attachSpawnChildConversationIds({ ...meta, spawn_children: spawnChildren })
        : meta;
      return {
        message: {
          ...base,
          meta: nextMeta,
          status: "updating",
        },
        agentKey: aid,
      };
    }

    case "turn": {
      const state = typeof data === "object" ? data?.state : null;
      const { aid, meta } = withAgentStream(base, data, (prev) => ({
        ...prev,
        turn: data,
        thinkingState: state,
      }));
      return {
        message: {
          ...base,
          meta: { ...meta, thinkingState: state },
          status: "updating",
        },
        agentKey: aid,
      };
    }

    case "end":
    case "agent_turn_complete": {
      const endContent =
        typeof data === "object"
          ? data?.content ||
            data?.final_content ||
            data?.final_response ||
            data?.response ||
            ""
          : "";
      const nextContent =
        base.content && base.content.length > 0
          ? base.content
          : String(endContent || "");
      const { aid, meta } = withAgentStream(base, data, (prev) => ({
        ...withRunningCost(prev, data),
        contentText:
          prev.contentText && prev.contentText.length > 0
            ? prev.contentText
            : String(endContent || prev.contentText || ""),
        contentComplete: true,
      }));
      // ADR-0113: surface assistant-generated media (e.g. images) carried on `end`.
      const endMedia =
        typeof data === "object" && Array.isArray(data?.media)
          ? (data.media as MediaPart[]).filter(
              (m) => m && (m.artifact_id || m.url),
            )
          : undefined;
      // Issue #188: surface budget/other stop_reason for Continue affordance.
      const stopReason =
        typeof data === "object" && typeof data?.stop_reason === "string"
          ? data.stop_reason
          : undefined;
      return {
        message: {
          ...base,
          content: nextContent,
          status: event === "end" ? "success" : "updating",
          taskId: data?.task_id || base.taskId,
          media: endMedia && endMedia.length > 0 ? endMedia : base.media,
          meta: {
            ...meta,
            thinkingState: event === "end" ? null : meta.thinkingState,
            ...(stopReason ? { stop_reason: stopReason } : {}),
          },
        },
        agentKey: aid,
      };
    }

    case "auth_required": {
      return {
        message: {
          ...base,
          status: "error",
          meta: { ...(base.meta || {}), auth_required: data },
        },
        agentKey: DEFAULT_STREAM_AGENT_KEY,
      };
    }

    case "error": {
      const { aid, meta } = withAgentStream(base, data, (prev) => ({
        ...prev,
        error: data,
      }));
      return { message: { ...base, status: "error", meta }, agentKey: aid };
    }

    case "tool_execution_started": {
      const toolName = data?.tool_name || "unknown";
      const toolCallId = data?.tool_call_id;
      const { aid, meta } = withAgentStream(base, data, (prev) => {
        const step = loopStepFromUnknown(data, prev.currentStep);
        return {
          ...prev,
          toolExecutions: [
            ...(prev.toolExecutions || []),
            {
              toolName,
              toolCallId,
              status: "running",
              startedAt: Date.now(),
              ...(step != null ? { step } : {}),
            },
          ],
        };
      });
      return {
        message: { ...base, meta, status: "updating" },
        agentKey: aid,
      };
    }

    case "tool_execution_completed": {
      const toolName = data?.tool_name || "unknown";
      const preview = data?.preview;
      const durationMs = data?.duration_ms;
      const { aid, meta } = withAgentStream(base, data, (prev) => ({
        ...prev,
        toolExecutions: (prev.toolExecutions || []).map((t: any) =>
          t.toolName === toolName && t.status === "running"
            ? {
                ...t,
                status:
                  data?.status === "success" ? "completed" : "error",
                preview,
                durationMs,
                completedAt: Date.now(),
              }
            : t,
        ),
      }));
      return {
        message: { ...base, meta, status: "updating" },
        agentKey: aid,
      };
    }

    case "tool_execution_failed": {
      const toolName = data?.tool_name || "unknown";
      const errorMsg = data?.error;
      const durationMs = data?.duration_ms;
      const { aid, meta } = withAgentStream(base, data, (prev) => ({
        ...prev,
        toolExecutions: (prev.toolExecutions || []).map((t: any) =>
          t.toolName === toolName && t.status === "running"
            ? {
                ...t,
                status: "failed",
                error: errorMsg,
                durationMs,
                completedAt: Date.now(),
              }
            : t,
        ),
      }));
      return {
        message: { ...base, meta, status: "updating" },
        agentKey: aid,
      };
    }

    default: {
      return {
        message: { ...base, status: "updating" },
        agentKey: DEFAULT_STREAM_AGENT_KEY,
      };
    }
  }
}
