/**
 * Motet - Chat Explorer - Motet Chat Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Core chat integration hook that bridges Ant Design X's useXChat with the
 *     Motet AI backend. Handles the complete chat lifecycle:
 *
 *     Message Flow:
 *     1. User calls onRequest with message content
 *     2. MotetChatProvider sends SSE request to /api/v1/chat
 *     3. Streaming tokens update messages array
 *     4. useThrottle limits UI updates to prevent jank (50ms)
 *     5. useChatProcessing extracts Reasoning Chain metadata
 *     6. bubbleListItems transforms messages into Bubble.List format
 *
 *     Features:
 *     - Streaming message updates via SSE
 *     - 50ms throttling to prevent excessive re-renders
 *     - Reasoning Chain extraction for reasoning visualization
 *     - OAuth authorization prompts for external services
 *     - Attachment preview prefetching for images
 *     - Markdown + Mermaid rendering for assistant responses
 *     - One assistant bubble per turn: sub-agent thinking and replies nest
 *       in a scrollable, collapsible stack; the selected chat agent's
 *       thinking and synthesis follow below it
 *     - Option A: loads history from GET /api/v1/conversations/:id when switching conv (not on JWT refresh)
 *     - History is applied only after useXChat's per-conversation store has
 *       switched; writing earlier lands in the previous chat and the selected
 *       thread stays empty
 *     - Reloaded history groups consecutive attributed assistants from one
 *       user turn into a single bubble (same nested layout as live; no thinking)
 *     - Surfaces conversation_get.warning when stored rows cannot be decrypted
 *     - Markdown + Mermaid rendering for all assistant bubbles (including during streaming)
 *     - Explicit Continue after max_iterations / max_model_calls budget stops (issue #188)
 *
 * Dependencies:
 *     - @ant-design/x-sdk: useXChat for chat state management
 *     - @ant-design/x: FileCard for attachments, Think for in-bubble thinking traces
 *     - MotetChatProvider: SSE streaming provider
 *     - useThrottle: Limits update frequency
 *     - useChatProcessing: Extracts metadata from messages
 *
 * Usage:
 *     const { bubbleListItems, onRequest, isRequesting } = useMotetChat(
 *       auth, conversationId, activeConversationKey, imageBlobUrls,
 *       ensureImagePreview, videoStreamUrls, ensureVideoSource, darkMode,
 *       overrides, ragControls, agentId, surfaceId, availableAgents,
 *       authorizedMessages, openOAuthPopup
 *     );
 *
 * Notes:
 *     - bubbleListItems keys must be stable across renders (avoid Date.now())
 *     - Image previews are prefetched when attachments appear in messages
 *     - OAuth prompts are rendered inline as special message content
 *     - One assistant bubble per turn; sub-agent sections sit in a
 *       scrollable collapse stack, then the selected agent's thinking
 *       and synthesis
 *     - useXChat keeps conversationKey and the message store one/two frames
 *       behind the activeConversationKey prop; do not setMessages until the
 *       store identity has caught up
 */
import React, { useMemo, useEffect, useRef, useCallback, useState } from "react";
import { useXChat } from "@ant-design/x-sdk";
import { FileCard, Think } from "@ant-design/x";
import { Collapse } from "antd";
import { MotetChatProvider, type ChatMessage, type ChatInput, type ChatOutput } from "../chatProvider";
import { renderAssistantMarkdownWithMermaid } from "../components/MermaidBlock";
import {
  assistantTurnSlices,
  assistantTranscriptTurnSlice,
  MediaRenderer,
  isBudgetStopReason,
  CONTINUE_AFTER_BUDGET_USER_MESSAGE,
  type AssistantTurnSlice,
  type RagControlsValue,
} from "@motet/ui-common";
import { useChatProcessing } from "./useChatProcessing";
import { useThrottle } from "./useThrottle";
import { type AttachmentState } from "../types/attachments";
import { inferFileCardProps } from "../types/attachments";
import { type AuthState } from "../types";
import { buildHeaders } from "./useAuth";
import { getConversation, mapHistoryToMessages } from "../api/conversations";
import { shouldApplyHistory } from "./conversationHistoryApply";

/** True if model thinking text is present on the assistant message (any agent bucket). */
function hasAssistantThinkingMeta(meta: Record<string, unknown> | undefined): boolean {
  if (!meta) return false;
  const streams = meta.agentStreams as Record<string, { thinkingText?: string }> | undefined;
  if (!streams) return false;
  return Object.values(streams).some((a) => (a?.thinkingText || "").length > 0);
}

function renderThinkBlock(thinkingText: string, thinkingComplete: boolean): React.ReactNode {
  if (!thinkingText) return null;
  return (
    <div className="assistant-turn-thinking">
      <Think
        title={thinkingComplete ? "Done thinking" : "Thinking..."}
        loading={!thinkingComplete}
        defaultExpanded={!thinkingComplete}
      >
        {thinkingText}
      </Think>
    </div>
  );
}

/** Stable color from agent key for per-agent avatars. */
function colorFromAgentKey(agentKey: string): string {
  let hash = 0;
  for (let i = 0; i < agentKey.length; i += 1) {
    hash = (hash * 31 + agentKey.charCodeAt(i)) | 0;
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue} 70% 42%)`;
}

function initialsFromAgentName(agentName: string): string {
  const words = agentName
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return "AI";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return `${words[0][0] || ""}${words[1][0] || ""}`.toUpperCase();
}

function buildAgentAvatar(agentName: string, agentKey: string): React.ReactNode {
  return (
    <div
      title={agentName}
      style={{
        width: 24,
        height: 24,
        borderRadius: "50%",
        background: colorFromAgentKey(agentKey),
        color: "#fff",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: 0.2,
        userSelect: "none"
      }}
    >
      {initialsFromAgentName(agentName)}
    </div>
  );
}

/**
 * React hook for integrating Motet AI chat with Ant Design X components.
 *
 * @param auth - Auth state for constructing request headers
 * @param conversationId - Current conversation ID for API calls
 * @param activeConversationKey - Active conversation key for SDK state
 * @param imageBlobUrls - Cached blob URLs for image previews
 * @param ensureImagePreview - Function to fetch image previews
 * @param videoStreamUrls - Cached tokenized stream URLs for video playback (ADR-0118)
 * @param ensureVideoSource - Function to mint playback/stream URLs for videos
 * @param darkMode - Whether dark mode is enabled (affects styling)
 * @param overrides - Model/behavior overrides
 * @param ragControls - Artifact RAG context controls to include in chat requests
 * @param agentId - Selected qualified agent id for requests
 * @param surfaceId - Selected surface/channel id for conversation registry (ADR-0083)
 * @param availableAgents - Registry list (for reasoning sidebar display names)
 * @param authorizedMessages - Map of messages that completed OAuth
 * @param openOAuthPopup - Function to open OAuth popup
 */
export function useMotetChat(
  auth: AuthState,
  conversationId: string,
  activeConversationKey: string,
  imageBlobUrls: Map<string, string>,
  ensureImagePreview: (id: string) => Promise<string | undefined>,
  videoStreamUrls: Map<string, string>,
  ensureVideoSource: (id: string) => Promise<string | undefined>,
  darkMode: boolean,
  overrides: any,
  ragControls: RagControlsValue,
  agentId: string,
  surfaceId: string,
  availableAgents: Array<{ qualified_id: string; display_name?: string }>,
  authorizedMessages: Map<string, { serviceId: string; displayName: string }>,
  openOAuthPopup: (endpoint: string, serviceId: string, messageId: string, conversationId?: string) => void
) {
  // ─────────────────────────────────────────────────────────────────────────────
  // SETUP: Provider and request context
  // ─────────────────────────────────────────────────────────────────────────────

  // Create singleton MotetChatProvider for SSE streaming
  const provider = useMemo(() => new MotetChatProvider("/api/v1/chat"), []);

  // Build request context with current auth and settings (surface_id for conversation registry)
  const requestContext = useMemo(() => ({
    headers: buildHeaders(auth),
    overrides,
    conversation_id: conversationId,
    surface_id: (surfaceId || "").trim() || "demo_chat",
    artifact_rag_scope: ragControls.scope,
    artifact_ids: ragControls.artifactIds.length > 0 ? ragControls.artifactIds : undefined,
    artifact_tags: ragControls.artifactTags.length > 0 ? ragControls.artifactTags : undefined,
    artifact_collection_id: ragControls.artifactCollectionId?.trim() || undefined,
    allow_broader_artifact_rag_scope:
      ragControls.scope !== "conversation" && ragControls.allowBroaderScope,
    agent_id: (agentId || "").trim() || "core.default",
  }), [auth, overrides, conversationId, agentId, surfaceId, ragControls]);

  // ─────────────────────────────────────────────────────────────────────────────
  // CHAT STATE: Messages and streaming status
  // ─────────────────────────────────────────────────────────────────────────────

  // Core chat hook from Ant Design X SDK
  const { messages, onRequest, isRequesting, setMessages } = useXChat<ChatMessage, ChatMessage, ChatInput, ChatOutput>({
    provider,
    conversationKey: activeConversationKey,
  });

  /** Issue #188: Continue after budget stop — new turn, fresh budget (not resume). */
  const continueAfterBudget = useCallback(() => {
    if (isRequesting) return;
    onRequest({
      messages: [
        {
          role: "user",
          content: CONTINUE_AFTER_BUDGET_USER_MESSAGE,
        },
      ],
      continue_after_budget: true,
      ...requestContext,
    });
  }, [isRequesting, onRequest, requestContext]);

  // Keep latest setMessages in a ref so the load-history effect can call it after async fetch without needing it in deps (avoids duplicate GET when setMessages ref changes each render).
  const setMessagesRef = useRef(setMessages);
  setMessagesRef.current = setMessages;
  const isRequestingRef = useRef(isRequesting);
  isRequestingRef.current = isRequesting;
  const activeConversationKeyRef = useRef(activeConversationKey);
  activeConversationKeyRef.current = activeConversationKey;

  /** Latest auth for history GET headers (JWT refresh must not retrigger this effect). */
  const authRef = useRef(auth);
  authRef.current = auth;
  const hydratedConversationKeysRef = useRef<Set<string>>(new Set());
  const pendingHistoryRef = useRef<{
    key: string;
    messageInfos: Array<{ id: string; message: ChatMessage; status: "success" }>;
  } | null>(null);
  const [historyWarning, setHistoryWarning] = useState<string | null>(null);
  // useXChat creates the per-key store synchronously on first mount, then lags
  // one/two frames on later key changes (conversationKey state + store swap).
  const storeReadyKeyRef = useRef<string | null>(activeConversationKey);
  const [storeReadyKey, setStoreReadyKey] = useState<string | null>(activeConversationKey);

  /** False → true on login; stays true across JWT refresh (deps use credential strings, not `auth` identity). */
  const canFetchHistory = useMemo(
    () =>
      !!(
        (auth?.jwt && auth.jwt.trim().length > 0) ||
        (auth?.apiKey && auth.apiKey.trim().length > 0) ||
        (auth?.serviceAccountToken && auth.serviceAccountToken.trim().length > 0)
      ),
    [auth?.jwt, auth?.apiKey, auth?.serviceAccountToken]
  );

  const messagesRef = useRef<unknown[]>([]);
  messagesRef.current = messages as unknown[];

  const applyPendingHistoryIfStoreReady = () => {
    const pending = pendingHistoryRef.current;
    if (!pending) return;
    if (
      !shouldApplyHistory({
        pendingKey: pending.key,
        activeKey: activeConversationKeyRef.current,
        storeReadyKey: storeReadyKeyRef.current,
        isRequesting: isRequestingRef.current,
        localMessageCount: Array.isArray(messagesRef.current) ? messagesRef.current.length : 0,
        alreadyHydrated: hydratedConversationKeysRef.current.has(pending.key),
      })
    ) {
      if (
        !isRequestingRef.current &&
        !hydratedConversationKeysRef.current.has(pending.key) &&
        Array.isArray(messagesRef.current) &&
        messagesRef.current.length > 0
      ) {
        hydratedConversationKeysRef.current.add(pending.key);
        pendingHistoryRef.current = null;
      }
      return;
    }

    // Only inspect this conversation's messages — messagesRef is stale until the
    // store swaps, so richer-state must run after storeReadyKey matches.
    if (hydratedConversationKeysRef.current.has(pending.key)) {
      const current = messagesRef.current as Array<{ message?: { meta?: { agentStreams?: unknown } } }>;
      const hasRicherStreamedState = current.some(
        (mi) =>
          !!(mi?.message?.meta?.agentStreams && Object.keys(mi.message.meta.agentStreams as object).length > 0)
      );
      if (hasRicherStreamedState) {
        pendingHistoryRef.current = null;
        return;
      }
    }

    setMessagesRef.current(pending.messageInfos);
    hydratedConversationKeysRef.current.add(pending.key);
    pendingHistoryRef.current = null;
  };

  // setMessages identity changes when useXChat swaps to the active conversation store.
  useEffect(() => {
    const ready = activeConversationKeyRef.current;
    storeReadyKeyRef.current = ready;
    setStoreReadyKey(ready);
    applyPendingHistoryIfStoreReady();
  }, [setMessages]);

  // Option A: Load history when switching conversations or gaining credentials — not when JWT rotates (avoids races with active SSE / replacing in-flight messages).
  // Guard: never overwrite a live stream with history. Reloaded rows reconstruct
  // reply text in agentStreams but omit thinking and tool traces.
  useEffect(() => {
    let cancelled = false;
    if (!activeConversationKey || !canFetchHistory) return;
    setHistoryWarning(null);
    const headers = buildHeaders(authRef.current);
    const key = activeConversationKey;
    getConversation(key, headers).then((detail) => {
      if (cancelled) return;
      if (activeConversationKeyRef.current !== key) return;
      if (!detail?.history?.length) {
        const stored = detail?.counts?.memory ?? 0;
        setHistoryWarning(
          detail?.warning
          || (stored > 0
            ? "This conversation has stored messages that cannot be decrypted with the current tenant encryption key. Start a new chat to continue."
            : null)
        );
        return;
      }
      setHistoryWarning(null);
      if (activeConversationKeyRef.current === key && isRequestingRef.current) return;
      const mapped = mapHistoryToMessages(detail.history, agentId);
      const messageInfos = mapped.map((m, i) => ({
        id: `restored-${key}-${i}`,
        message: { ...m, status: "success" as const } as ChatMessage,
        status: "success" as const,
      }));
      pendingHistoryRef.current = { key, messageInfos };
      applyPendingHistoryIfStoreReady();
    });
    return () => {
      cancelled = true;
    };
  }, [activeConversationKey, canFetchHistory, agentId]);

  // Throttle message updates to 50ms to prevent jank during streaming
  const throttledMessages = useThrottle(messages, 50);

  // ─────────────────────────────────────────────────────────────────────────────
  // METADATA EXTRACTION: Reasoning Chain
  // ─────────────────────────────────────────────────────────────────────────────

  // Extract reasoning steps and workflow steps from message metadata
  const { reasoningPanels, thinkingState } = useChatProcessing(
    throttledMessages,
    activeConversationKey,
    availableAgents
  );

  // ─────────────────────────────────────────────────────────────────────────────
  // IMAGE PREFETCHING: Load authenticated previews for attachments
  // ─────────────────────────────────────────────────────────────────────────────

  // Collect all image artifact IDs that need previews (memoized to prevent churn)
  const requiredImageIds = useMemo(() => {
    const ids = new Set<string>();
    for (const msgInfo of throttledMessages as any[]) {
      const msg = (msgInfo?.message || msgInfo) as any;
      const atts = (msg?.attachments || msg?.meta?.attachments) as AttachmentState[] | undefined;
      if (Array.isArray(atts)) {
        for (const a of atts) {
          if (a?.content_type?.startsWith("image/") && a.artifact_id) {
            ids.add(a.artifact_id);
          }
        }
      }
      // ADR-0113: assistant-generated media (e.g. images) surfaced on the turn end.
      const media = msg?.media as Array<any> | undefined;
      if (Array.isArray(media)) {
        for (const m of media) {
          const isImage =
            m?.media_type === "image" ||
            (typeof m?.mime_type === "string" && m.mime_type.startsWith("image/"));
          if (isImage && m?.artifact_id && !m?.url) {
            ids.add(m.artifact_id);
          }
        }
      }
      // ADR-0113: images referenced inline in assistant text as `![alt](artifact:<id>)`
      // must be prefetched so the markdown renderer can resolve them to blob URLs.
      const content = typeof msg?.content === "string" ? msg.content : "";
      if (content.includes("artifact:")) {
        const re = /artifact:([0-9a-fA-F-]{36})/g;
        let mm: RegExpExecArray | null;
        while ((mm = re.exec(content)) !== null) {
          ids.add(mm[1]);
        }
      }
    }
    return ids;
  }, [throttledMessages]);

  useEffect(() => {
    for (const id of requiredImageIds) {
      if (!imageBlobUrls.has(id)) {
        void ensureImagePreview(id);
      }
    }
  }, [requiredImageIds, ensureImagePreview, imageBlobUrls]);

  // ADR-0118: collect video artifact IDs (user attachments + assistant media)
  // that need a tokenized stream URL for <video> playback.
  const requiredVideoIds = useMemo(() => {
    const ids = new Set<string>();
    for (const msgInfo of throttledMessages as any[]) {
      const msg = (msgInfo?.message || msgInfo) as any;
      const atts = (msg?.attachments || msg?.meta?.attachments) as AttachmentState[] | undefined;
      if (Array.isArray(atts)) {
        for (const a of atts) {
          if (a?.content_type?.startsWith("video/") && a.artifact_id) {
            ids.add(a.artifact_id);
          }
        }
      }
      const media = msg?.media as Array<any> | undefined;
      if (Array.isArray(media)) {
        for (const m of media) {
          const isVideo =
            m?.media_type === "video" ||
            (typeof m?.mime_type === "string" && m.mime_type.startsWith("video/"));
          if (isVideo && m?.artifact_id && !m?.url) {
            ids.add(m.artifact_id);
          }
        }
      }
    }
    return ids;
  }, [throttledMessages]);

  useEffect(() => {
    for (const id of requiredVideoIds) {
      if (!videoStreamUrls.has(id)) {
        void ensureVideoSource(id);
      }
    }
  }, [requiredVideoIds, ensureVideoSource, videoStreamUrls]);

  // ─────────────────────────────────────────────────────────────────────────────
  // BUBBLE LIST: Transform messages into Ant Design X Bubble.List format
  // ─────────────────────────────────────────────────────────────────────────────

  /**
   * Transforms throttled messages into Bubble.List items for rendering.
   * Handles:
   * - User messages with optional attachments
   * - Assistant messages with markdown + mermaid rendering
   * - OAuth authorization prompts
   * - Authorized service confirmations
   * - Loading/typing states during streaming
   */
  const bubbleListItems = useMemo(() => {
    return throttledMessages
      .filter((msgInfo: any) => {
        const msg = msgInfo.message || msgInfo;
        if (msg?.role === "tool" || msg?.role === "system") return false;
        // Don't show assistant messages with no content when done (tool-call placeholder).
        // Keep in-progress assistant messages (loading/updating) so the thinking bubble appears.
        if (msg?.role === "assistant") {
          const st = msgInfo.status ?? msg?.status;
          const inProgress = st === "loading" || st === "updating";
          const hasMedia = Array.isArray(msg?.media) && msg.media.length > 0;
          const hasContent = !!((msg?.content ?? "").trim() || hasAssistantThinkingMeta(msg?.meta) || hasMedia);
          if (!inProgress && !hasContent) return false;
        }
        return true;
      })
      .flatMap((msgInfo: any, idx: number): Array<{
        key: string; role: string; placement: "start" | "end";
        avatar: React.ReactNode; content: React.ReactNode;
        variant: "filled" | "outlined" | "shadow" | "borderless";
        loading: boolean; typing: any;
      }> => {
      const msg = msgInfo.message || msgInfo;

      // CRITICAL: Keys must be stable across renders.
      // Using Date.now() here would cause key changes every render, breaking React reconciliation.
      const messageId = msgInfo.id || msg.id || `${activeConversationKey}:${msg.role}:${idx}`;
      
      const authorizedInfo = authorizedMessages.get(messageId);
      const authRequired = msg.meta?.auth_required;
      let content: React.ReactNode;
      let assistantAvatar: React.ReactNode = "🤖";
      
      let attachmentsNode: React.ReactNode = null;
      if (msg.role === "user" && (msg.attachments || msg.meta?.attachments)) {
        const atts = (msg.attachments || msg.meta?.attachments) as AttachmentState[] | undefined;
        if (atts && atts.length > 0) {
          const fileCardItems = atts.map(att => {
            const blobUrl = imageBlobUrls.get(att.artifact_id);
            const effectiveAtt =
              att?.content_type?.startsWith("image/") && !blobUrl
                ? { ...att, status: "uploading" as const }
                : att;
            return inferFileCardProps(effectiveAtt as any, { imageSrc: blobUrl });
          });

          // ADR-0118: inline players for video attachments (tokenized stream
          // URLs preserve HTTP Range seek/scrub; blob URLs would not).
          const videoAtts = atts.filter((a) => a?.content_type?.startsWith("video/") && a.artifact_id);
          const videoPlayers = videoAtts
            .map((a) => {
              const src = videoStreamUrls.get(a.artifact_id);
              if (!src) return null;
              return (
                <video
                  key={`video:${a.artifact_id}`}
                  src={src}
                  controls
                  preload="metadata"
                  style={{
                    maxWidth: "100%",
                    maxHeight: 384,
                    borderRadius: 8,
                    display: "block",
                    background: "#000",
                    marginTop: 8,
                  }}
                />
              );
            })
            .filter(Boolean);

          attachmentsNode = (
            <div style={{ marginBottom: 8 }}>
              <FileCard.List items={fileCardItems as any} />
              {videoPlayers}
            </div>
          );
        }
      }
      
      // ADR-0113: assistant-generated media (e.g. images) rendered via the shared MediaRenderer.
      // Skip any artifact already referenced inline as `![alt](artifact:<id>)` in the text so
      // it isn't shown twice (the markdown renderer resolves those inline).
      const inlineArtifactIds = new Set<string>();
      if (msg.role === "assistant" && typeof msg.content === "string" && msg.content.includes("artifact:")) {
        const re = /artifact:([0-9a-fA-F-]{36})/g;
        let mm: RegExpExecArray | null;
        while ((mm = re.exec(msg.content)) !== null) inlineArtifactIds.add(mm[1]);
      }
      const surfacedMedia =
        msg.role === "assistant" && Array.isArray(msg.media)
          ? msg.media.filter((m: any) => !(m?.artifact_id && inlineArtifactIds.has(m.artifact_id)))
          : [];
      const mediaNode: React.ReactNode =
        surfacedMedia.length > 0 ? (
          <MediaRenderer
            media={surfacedMedia}
            resolveImageUrl={(id: string) => imageBlobUrls.get(id)}
            onRequestImage={ensureImagePreview}
            resolveVideoUrl={(id: string) => videoStreamUrls.get(id)}
            onRequestVideo={ensureVideoSource}
            darkMode={darkMode}
          />
        ) : null;

      if (authorizedInfo && msg.role === "assistant") {
        content = (
          <div style={{ 
            padding: "12px", 
            background: darkMode ? "#162312" : "#f6ffed", 
            border: `1px solid ${darkMode ? "#389e0d" : "#b7eb8f"}`, 
            borderRadius: "8px", 
            margin: "8px 0" 
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <span style={{ fontSize: "24px" }}>✅</span>
              <span style={{ color: darkMode ? "#95de64" : "#389e0d", fontWeight: "bold" }}>
                {authorizedInfo.displayName} has been authorized. You can now retry your request.
              </span>
            </div>
          </div>
        );
      } else if (authRequired && msg.role === "assistant") {
        const serviceId = authRequired.service_id || "unknown";
        const displayName = authRequired.display_name || serviceId;
        const authEndpoint = authRequired.authorization_endpoint || `/api/v1/oauth/${serviceId}/initiate`;
        const message = authRequired.message || `${displayName} requires authorization to continue.`;
        
        content = (
          <div style={{ 
            padding: "12px", 
            background: darkMode ? "#2a2a00" : "#fffbe6", 
            border: `1px solid ${darkMode ? "#d4b106" : "#ffe58f"}`, 
            borderRadius: "8px", 
            margin: "8px 0" 
          }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <span style={{ fontSize: "24px" }}>🔐</span>
              <span style={{ color: darkMode ? "#ffd666" : "#ad6800", fontWeight: "bold" }}>Authorization Required</span>
            </div>
            <span style={{ margin: "8px 0", color: darkMode ? "#ffd666" : "#ad6800", display: "block" }}>{message}</span>
            <button
              className="ant-btn ant-btn-primary"
              onClick={() => openOAuthPopup(authEndpoint, serviceId, messageId, conversationId)}
              style={{ marginTop: "8px" }}
            >
              Authorize {displayName}
            </button>
          </div>
        );
      } else {
        if (msg.role === "assistant") {
          const st = msgInfo.status ?? msg?.status;
          const showContinue =
            isBudgetStopReason(msg?.meta?.stop_reason) &&
            st === "success" &&
            !isRequesting;
          const continueNode = showContinue ? (
            <div
              style={{
                marginTop: 12,
                padding: "10px 12px",
                background: darkMode ? "#111b26" : "#e6f4ff",
                border: `1px solid ${darkMode ? "#15325b" : "#91caff"}`,
                borderRadius: 8,
              }}
            >
              <div
                style={{
                  color: darkMode ? "#91caff" : "#0958d9",
                  marginBottom: 8,
                  fontSize: 13,
                }}
              >
                Turn budget exhausted ({String(msg.meta.stop_reason)}). Continue
                starts a new turn with a fresh budget.
              </div>
              <button
                type="button"
                className="ant-btn ant-btn-primary"
                onClick={continueAfterBudget}
                disabled={isRequesting}
              >
                Continue
              </button>
            </div>
          ) : null;
          const streamSlices = assistantTurnSlices(msg?.meta, availableAgents, agentId);
          const transcriptSlice = assistantTranscriptTurnSlice(msg, availableAgents);
          const turnSlices: AssistantTurnSlice[] =
            streamSlices.length > 0 ? streamSlices : (transcriptSlice ? [transcriptSlice] : []);
          const renderSliceMarkdown = (text: string) =>
            text
              ? renderAssistantMarkdownWithMermaid(text, darkMode, {
                  resolveImageUrl: (id: string) => imageBlobUrls.get(id),
                  onRequestImage: ensureImagePreview,
                })
              : null;
          if (turnSlices.length > 0) {
            const childSlices = turnSlices.filter((s) => !s.isPrimary);
            const primarySlice = turnSlices.find((s) => s.isPrimary) ?? turnSlices[turnSlices.length - 1];
            const inProgress = st === "loading" || st === "updating";
            assistantAvatar = buildAgentAvatar(primarySlice.agentName, primarySlice.agentKey);
            content = (
              <div className="assistant-turn">
                {childSlices.length > 0 ? (
                  <div className="assistant-turn-subagents">
                    <Collapse
                      key={inProgress ? "live" : "done"}
                      size="small"
                      bordered={false}
                      className="assistant-turn-subagents-collapse"
                      defaultActiveKey={inProgress ? childSlices.map((s) => s.agentKey) : []}
                      items={childSlices.map((s) => ({
                        key: s.agentKey,
                        label: (
                          <div className="assistant-turn-subagent-label">
                            {buildAgentAvatar(s.agentName, s.agentKey)}
                            <span>{s.agentName}</span>
                          </div>
                        ),
                        children: (
                          <>
                            {renderThinkBlock(s.thinkingText, s.thinkingComplete)}
                            {renderSliceMarkdown(s.text)}
                          </>
                        ),
                      }))}
                    />
                  </div>
                ) : null}
                <div className="assistant-turn-primary">
                  {primarySlice ? renderThinkBlock(primarySlice.thinkingText, primarySlice.thinkingComplete) : null}
                  {primarySlice ? renderSliceMarkdown(primarySlice.text) : null}
                  {mediaNode}
                  {continueNode}
                </div>
              </div>
            );
          } else {
            const singleText = (msg.content || "").trim();
            content = (
              <>
                {singleText
                  ? renderAssistantMarkdownWithMermaid(singleText, darkMode, {
                      resolveImageUrl: (id: string) => imageBlobUrls.get(id),
                      onRequestImage: ensureImagePreview,
                    })
                  : null}
                {mediaNode}
                {continueNode}
              </>
            );
          }
        } else {
          content = (
            <>
              {attachmentsNode}
              {msg.content}
            </>
          );
        }
      }
      
      // Typing animation only works with plain string content.
      const canType = msgInfo.status === "updating" && !!msg.content;

      return [{
        key: messageId,
        role: msg.role,
        placement: (msg.role === "user" ? "end" : "start") as "end" | "start",
        avatar: msg.role === "assistant" ? assistantAvatar : "👤",
        content,
        variant: (msg.role === "user" ? "filled" : "outlined") as "filled" | "outlined" | "shadow" | "borderless",
        loading:
          msgInfo.status === "loading" ||
          (msgInfo.status === "updating" && !msg.content && !hasAssistantThinkingMeta(msg?.meta)),
        typing: canType ? { step: 30, interval: 30 } as any : undefined,
      }];
    });
  }, [
    throttledMessages,
    authorizedMessages,
    darkMode,
    imageBlobUrls,
    ensureImagePreview,
    videoStreamUrls,
    ensureVideoSource,
    openOAuthPopup,
    conversationId,
    activeConversationKey,
    availableAgents,
    agentId,
    isRequesting,
    continueAfterBudget,
  ]);

  return {
    messages: throttledMessages,
    bubbleListItems,
    onRequest,
    isRequesting,
    requestContext,
    reasoningPanels,
    thinkingState,
    continueAfterBudget,
    historyWarning,
    storeReadyKey,
  };
}

