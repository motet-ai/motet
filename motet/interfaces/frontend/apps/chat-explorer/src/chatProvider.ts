/**
 * Motet - Chat Explorer - Chat Provider
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Ant Design X chat provider that wraps the framework-agnostic
 *     Motet SSE protocol reducer and LiveTurnRegistry from @motet/ui-common.
 *
 *     Each conversation_id keeps its own fetch and reduced message. A send
 *     in one chat does not abort or overwrite another chat's stream.
 *     Chunks reduce into that owner slot. The hook overlays the owner (or
 *     a projected spawn child) onto the visible thread.
 *
 * Dependencies:
 *     - @motet/ui-common: Protocol types, SSE consumer, live-turn map
 *     - @ant-design/x-sdk: AbstractChatProvider, TransformMessage
 */
import {
  AbstractChatProvider,
  type TransformMessage,
  type XRequestOptions,
} from "@ant-design/x-sdk";

import {
  type ChatMessage,
  type ChatInput,
  type ChatOutput,
  LiveTurnRegistry,
  chatOutputConversationId,
  consumeChatSse,
  tagChatOutputConversation,
} from "@motet/ui-common";

import { DEFAULT_STREAM_AGENT_KEY } from "./types";

// Re-export protocol types so existing importers don't break
export type { ChatMessage, ChatInput, ChatOutput };

type StreamCallbacks = {
  onUpdate?: (data: ChatOutput, responseHeaders: Headers) => unknown;
  onSuccess?: (data: ChatOutput[], responseHeaders: Headers) => unknown;
  onError?: (error: unknown, errorInfo?: unknown, responseHeaders?: Headers) => unknown;
};

function buildHeaders(params: Partial<ChatInput> | undefined): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  const extra = params?.headers as Record<string, string> | undefined;
  if (extra) {
    Object.entries(extra).forEach(([k, v]) => {
      if (v) headers[k] = v;
    });
  }
  return headers;
}

/**
 * One AbortController and callback set per conversation so XRequest's
 * single-controller run() cannot abort a still-running sibling chat.
 */
class MultiplexChatRequest {
  readonly manual = true;
  options: { callbacks?: StreamCallbacks; params?: Partial<ChatInput> } = {
    callbacks: {},
    params: {},
  };
  private controllers = new Map<string, AbortController>();
  private callbacksByCid = new Map<string, StreamCallbacks>();

  constructor(
    private readonly baseUrl: string,
    private readonly liveTurns: LiveTurnRegistry
  ) {}

  run(params: ChatInput, opts?: { sdkOwned?: boolean }): boolean {
    const cid = String(params?.conversation_id || "").trim();
    if (!cid) return false;
    const callbacks = this.options.callbacks || {};
    this.callbacksByCid.set(cid, callbacks);
    this.controllers.get(cid)?.abort();
    const controller = new AbortController();
    this.controllers.set(cid, controller);
    this.liveTurns.start(cid);
    const sdkOwned = opts?.sdkOwned !== false;

    void fetch(this.baseUrl, {
      method: "POST",
      headers: buildHeaders(params),
      body: JSON.stringify(params),
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const headers = res.headers;
        await consumeChatSse(
          res,
          (evt) => {
            const tagged = tagChatOutputConversation(evt, cid);
            if (sdkOwned) {
              callbacks.onUpdate?.(tagged, headers);
            } else {
              this.liveTurns.applyChunk(cid, tagged);
            }
          },
          controller.signal
        );
        this.liveTurns.markInactive(cid);
        if (sdkOwned) {
          callbacks.onSuccess?.([], headers);
        }
      })
      .catch((error: unknown) => {
        this.liveTurns.markInactive(cid);
        const err = error instanceof Error ? error : new Error(String(error));
        if (sdkOwned) {
          callbacks.onError?.(err);
        }
      })
      .finally(() => {
        if (this.controllers.get(cid) === controller) {
          this.controllers.delete(cid);
        }
      });
    return true;
  }

  abort(conversationId?: string): void {
    const cid = String(conversationId || "").trim();
    if (cid) {
      this.controllers.get(cid)?.abort();
      return;
    }
    this.controllers.forEach((controller) => controller.abort());
  }
}

/**
 * Ant Design X chat provider for the Motet AI backend.
 *
 * Delegates SSE folding to LiveTurnRegistry / reduceChatEvent and runs
 * one fetch per conversation.
 */
export class MotetChatProvider extends AbstractChatProvider<
  ChatMessage,
  ChatInput,
  ChatOutput
> {
  /** Conversation the user is looking at (may be a spawn child mid-parent-turn). */
  displayConversationKey = "";
  readonly liveTurns: LiveTurnRegistry;
  private readonly _multiplex: MultiplexChatRequest;
  private _debugStreamEnabled: boolean = false;
  private _debugTokenCount: number = 0;
  private _debugTotalChars: number = 0;
  private _debugStartMs: number | null = null;
  private _debugLastLogMs: number = 0;

  constructor(baseUrl = "/api/v1/chat") {
    const liveTurns = new LiveTurnRegistry();
    const multiplex = new MultiplexChatRequest(baseUrl, liveTurns);
    super({
      request: multiplex as unknown as ConstructorParameters<
        typeof AbstractChatProvider<ChatMessage, ChatInput, ChatOutput>
      >[0]["request"],
    });
    this.liveTurns = liveTurns;
    this._multiplex = multiplex;

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

  overlayFor(displayConversationId: string): ChatMessage | null {
    return this.liveTurns.overlayFor(displayConversationId);
  }

  overlayOwner(displayConversationId: string): string | null {
    return this.liveTurns.overlayOwner(displayConversationId);
  }

  isBusy(displayConversationId: string): boolean {
    return this.liveTurns.isBusy(displayConversationId);
  }

  isActive(conversationId: string): boolean {
    return this.liveTurns.isActive(conversationId);
  }

  subscribeLiveTurn(listener: () => void): () => void {
    return this.liveTurns.subscribe(listener);
  }

  clearLiveTurn(conversationId?: string): void {
    const id = String(conversationId || this.displayConversationKey || "").trim();
    if (id) this.liveTurns.clear(id);
  }

  abortConversation(conversationId: string): void {
    this._multiplex.abort(conversationId);
  }

  /**
   * Start this conversation's SSE without writing into the visible useXChat
   * store. Used when the store still belongs to another chat.
   */
  startOwnerStream(params: Partial<ChatInput>): void {
    const transformed = this.transformParams(params, { params: {} } as XRequestOptions<ChatInput, ChatOutput>);
    const cid = String(transformed.conversation_id || "").trim();
    if (!cid) return;
    this.liveTurns.start(cid);
    this._multiplex.run(transformed, { sdkOwned: false });
  }

  transformLocalMessage(requestParams: Partial<ChatInput>): ChatMessage {
    const messages = requestParams.messages;
    const last = messages?.length ? messages[messages.length - 1] : undefined;
    const cid =
      String(requestParams.conversation_id || this.displayConversationKey || "").trim();
    if (cid) this.liveTurns.start(cid);
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
    const cid = chatOutputConversationId(chunk);
    if (!cid) {
      const fallback = originMessage || { role: "assistant" as const, content: "" };
      if (!chunk) {
        return { ...fallback, status: fallback.status === "error" ? "error" : "success" };
      }
      return fallback;
    }
    const { message, agentKey } = this.liveTurns.applyChunk(cid, chunk, originMessage);

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
              (cid ? ` conv=${cid}` : "") +
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
            (elapsed != null ? `, elapsed=${elapsed}ms` : "") +
            (cid ? ` conv=${cid}` : ""),
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
