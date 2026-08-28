/**
 * Motet - Chat Explorer - Chat Provider
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-07
 *
 * Description:
 *     Ant Design X chat provider that wraps the framework-agnostic
 *     Motet SSE protocol reducer from @motet/ui-common.
 *
 *     The protocol types (ChatMessage, ChatInput, ChatOutput) and
 *     SSE event handling (reduceChatEvent) live in @motet/ui-common
 *     so any UI framework can consume the Motet chat stream.
 *     This file adds only the Ant Design X integration layer.
 *
 * Dependencies:
 *     - @motet/ui-common: Protocol types and SSE event reducer
 *     - @ant-design/x-sdk: AbstractChatProvider, TransformMessage, XRequest
 */
import {
  AbstractChatProvider,
  type TransformMessage,
  type XRequestOptions,
  XRequest,
} from "@ant-design/x-sdk";

import {
  type ChatMessage,
  type ChatInput,
  type ChatOutput,
  reduceChatEvent,
} from "@motet/ui-common";

import { DEFAULT_STREAM_AGENT_KEY } from "./types";

// Re-export protocol types so existing importers don't break
export type { ChatMessage, ChatInput, ChatOutput };

// ─────────────────────────────────────────────────────────────────────────────
// HELPER FUNCTIONS
// ─────────────────────────────────────────────────────────────────────────────

function buildHeaders(
  opts: XRequestOptions<ChatInput, ChatOutput>,
): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  const extra = (opts?.params as any)?.headers as
    | Record<string, string>
    | undefined;
  if (extra) {
    Object.entries(extra).forEach(([k, v]) => {
      if (v) headers[k] = v;
    });
  }
  return headers;
}

// ─────────────────────────────────────────────────────────────────────────────
// CHAT PROVIDER CLASS
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Ant Design X chat provider for the Motet AI backend.
 *
 * Delegates all SSE event handling to the shared `reduceChatEvent` from
 * @motet/ui-common and adds optional streaming diagnostics.
 */
export class MotetChatProvider extends AbstractChatProvider<
  ChatMessage,
  ChatInput,
  ChatOutput
> {
  private _debugStreamEnabled: boolean = false;
  private _debugTokenCount: number = 0;
  private _debugTotalChars: number = 0;
  private _debugStartMs: number | null = null;
  private _debugLastLogMs: number = 0;
  private _debugRequestCount: number = 0;

  constructor(baseUrl = "/api/v1/chat") {
    super({
      request: XRequest<ChatInput, ChatOutput>(baseUrl, {
        manual: true,
        fetch: async (
          url: RequestInfo | URL,
          req: XRequestOptions<ChatInput, ChatOutput>,
        ): Promise<Response> => {
          if (this._debugStreamEnabled) {
            this._debugRequestCount += 1;
            this._debugStartMs = null;
            this._debugLastLogMs = 0;
            this._debugTokenCount = 0;
            this._debugTotalChars = 0;

            const msgs = (req.params as any)?.messages;
            const lastMsg =
              Array.isArray(msgs) && msgs.length > 0
                ? msgs[msgs.length - 1]
                : null;
            const preview = String(lastMsg?.content || "")
              .replace(/\n/g, "\\n")
              .slice(0, 80);
            // eslint-disable-next-line no-console
            console.log(
              `[chat-x stream] request#${this._debugRequestCount} start: conv=${String((req.params as any)?.conversation_id || "")} msg="${preview}"`,
            );
          }

          const res = await fetch(url, {
            method: "POST",
            headers: buildHeaders(req),
            body: JSON.stringify(req.params),
          });
          if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
          return res;
        },
      }),
    });

    try {
      if (typeof window !== "undefined") {
        this._debugStreamEnabled =
          window.localStorage?.getItem("chat_explorer_debug_stream") === "1";
      }
    } catch {
      this._debugStreamEnabled = false;
    }
  }

  transformParams(
    requestParams: Partial<ChatInput>,
    options: XRequestOptions<ChatInput, ChatOutput>,
  ): ChatInput {
    const baseParams = (options.params || {}) as Partial<ChatInput>;

    return {
      stream: true,
      overrides: requestParams.overrides ?? baseParams.overrides,
      conversation_id:
        requestParams.conversation_id ?? baseParams.conversation_id,
      agent_id: requestParams.agent_id ?? baseParams.agent_id,
      surface_id: requestParams.surface_id ?? baseParams.surface_id,
      artifact_rag_scope: requestParams.artifact_rag_scope ?? baseParams.artifact_rag_scope,
      artifact_ids: requestParams.artifact_ids ?? baseParams.artifact_ids,
      artifact_tags: requestParams.artifact_tags ?? baseParams.artifact_tags,
      artifact_collection_id:
        requestParams.artifact_collection_id ?? baseParams.artifact_collection_id,
      allow_broader_artifact_rag_scope:
        requestParams.allow_broader_artifact_rag_scope ??
        baseParams.allow_broader_artifact_rag_scope,
      continue_after_budget:
        requestParams.continue_after_budget ?? baseParams.continue_after_budget,
      messages: requestParams.messages || baseParams.messages || [],
      headers: requestParams.headers ?? baseParams.headers,
    };
  }

  transformLocalMessage(requestParams: Partial<ChatInput>): ChatMessage {
    const messages = requestParams.messages;
    const last = messages?.length ? messages[messages.length - 1] : undefined;
    return {
      role: "user",
      content: last?.content || "",
      status: "success",
      attachments: last?.attachments || undefined,
    };
  }

  transformMessage(
    info: TransformMessage<ChatMessage, ChatOutput>,
  ): ChatMessage {
    const { originMessage, chunk } = info;
    const { message, agentKey } = reduceChatEvent(originMessage, chunk);

    // Debug logging (only in this Ant Design X wrapper, not in the shared reducer)
    if (this._debugStreamEnabled && chunk) {
      const event =
        typeof chunk.event === "string" ? chunk.event.trim() : chunk.event;

      if (event === "token") {
        const now =
          typeof performance !== "undefined" ? performance.now() : Date.now();
        if (this._debugStartMs == null) {
          this._debugStartMs = now;
          this._debugLastLogMs = now;
          this._debugTokenCount = 0;
          this._debugTotalChars = 0;
        }
        const tok =
          typeof chunk.data === "string"
            ? chunk.data
            : chunk.data?.t ?? "";
        this._debugTokenCount += 1;
        this._debugTotalChars += String(tok || "").length;

        const shouldLog =
          now - this._debugLastLogMs >= 250 ||
          this._debugTokenCount % 50 === 0;
        if (shouldLog) {
          this._debugLastLogMs = now;
          const elapsed =
            this._debugStartMs != null
              ? Math.round(now - this._debugStartMs)
              : 0;
          const preview = String(tok || "")
            .replace(/\n/g, "\\n")
            .slice(0, 60);
          // eslint-disable-next-line no-console
          console.log(
            `[chat-x stream] token#${this._debugTokenCount} (+${String(tok || "").length} chars, total≈${this._debugTotalChars} chars) at ${elapsed}ms` +
              (agentKey !== DEFAULT_STREAM_AGENT_KEY
                ? ` agent_id=${agentKey}`
                : "") +
              `: "${preview}"`,
          );
        }
      } else if (event === "thinking") {
        const aid = agentKey;
        const len = String(
          message.meta?.agentStreams?.[aid]?.thinkingText || "",
        ).length;
        const isComplete = message.meta?.agentStreams?.[aid]?.thinkingComplete;
        // eslint-disable-next-line no-console
        console.log(
          `[chat-x stream] thinking agent_id=${aid} len=${len} complete=${isComplete}`,
        );
      } else if (
        event === "step" ||
        event === "workflow_step" ||
        event === "reasoning" ||
        event === "reasoning_step" ||
        event === "reasoning_meta" ||
        event === "conversation_analyzed"
      ) {
        // eslint-disable-next-line no-console
        console.log(`[chat-x stream] ${event} agent_id=${agentKey}`);
      } else if (event === "end") {
        const now =
          typeof performance !== "undefined" ? performance.now() : Date.now();
        const elapsed =
          this._debugStartMs != null
            ? Math.round(now - this._debugStartMs)
            : null;
        // eslint-disable-next-line no-console
        console.log(
          `[chat-x stream] end: tokens=${this._debugTokenCount}, totalChars≈${this._debugTotalChars}` +
            (elapsed != null ? `, elapsed=${elapsed}ms` : ""),
        );
        this._debugStartMs = null;
        this._debugLastLogMs = 0;
        this._debugTokenCount = 0;
        this._debugTotalChars = 0;
      }
    }

    return message;
  }
}
