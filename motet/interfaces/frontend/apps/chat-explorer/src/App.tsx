/**
 * Motet - Chat Explorer - App
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-27
 *
 * Description:
 *     Top-level React application for the Chat Explorer UI. This file is the main
 *     composition layer that wires together all custom hooks and components:
 *
 *     - Authentication (useAuth): JWT, API key, SSO, token refresh.
 *       Unauthenticated users see SignedOutPage; the chat shell is unmounted.
 *     - Conversations (useConversation): List management, create/rename/delete
 *     - Attachments (useAttachments): File uploads, blob URL previews, cleanup
 *     - Event Bus (useEventBus): Optional SSE subscription for debugging
 *     - Chat Logic (useMotetChat): Message streaming, Reasoning Chain
 *
 *     The UI is built with Ant Design's Layout system:
 *     - Header: Agent/surface pickers, auth status, settings dropdown
 *     - Left Sider: Conversation list
 *     - Content: Chat thread + input area
 *     - Right Sider: Observability panel (Reasoning Chain, events, errors)
 *
 * Dependencies:
 *     - React: UI framework
 *     - Ant Design: Layout, Card, Flex, Space, Typography, Badge, Spin
 *     - Ant Design X: Bubble, Attachments, Sender, ThoughtChain (Reasoning Chain)
 *     - Custom hooks: useAuth, useAttachments, useEventBus, useConversation, useMotetChat
 *     - Custom components: ChatHeader, LeftSidebar, RightSidebar, ChatInputArea, ChatThread
 *
 * Usage:
 *     Imported and rendered by `src/main.tsx` as the root component.
 *
 * Notes:
 *     - Keep this file focused on composition; push logic into hooks/components.
 *     - Theme (dark/light mode) is managed at the App level and persisted to localStorage;
 *       defaults to dark when no preference is saved.
 *     - The App component wraps AppContent in ConfigProvider to provide theme tokens.
 *     - Conversation scope is (agent_id, surface_id); surface defaults to demo_chat
 *       and options come from /api/v1/surfaces filtered by agent allow-list.
 *     - Model override is a single ``provider : model`` select below the composer,
 *       with a key icon when the provider has an API key. Models without a key
 *       stay in the list but cannot be selected. Enable thinking and reasoning
 *       effort sit to the right of the select.
 *     - Retrieval is a composer popover (This chat / My files / Workspace) with a
 *       closed-state chip; Advanced holds optional file IDs and tags.
 *     - Auto-rename from first user message only for demo_chat surface; other
 *       surfaces keep unset titles as "New Chat" until the user renames.
 *     - Agent/surface catalogs refetch when credentials appear or disappear,
 *       not when the JWT string rotates.
 */
import { useState, useEffect, useMemo, useRef } from "react";
import {
  ConfigProvider,
  Layout,
  theme,
  Card,
  Flex,
  Space,
  Typography,
  Badge,
  Spin,
  Alert
} from "antd";
import { useAuth } from "./hooks/useAuth";
import { useAttachments } from "./hooks/useAttachments";
import { useEventBus } from "./hooks/useEventBus";
import { useConversation } from "./hooks/useConversation";
import { useMotetChat } from "./hooks/useMotetChat";
import {
  isLegacyTruncatedAutoTitle,
  shouldFlushAutoTitlePersist,
  shouldQueueAutoTitle,
} from "./hooks/conversationHistoryApply";
import {
  defaultRagControlsValue,
  resolveAgentDisplayName,
  treatsThinkingAsAlwaysOn,
  useRequestContext,
  SignedOutPage,
  markSignedOut,
  clearSignedOutFlag,
  wasSignedOut,
  appLogoutRedirectUri,
  finishRemoteLogout,
  type RagControlsValue,
  type ReasoningEffort,
} from "@motet/ui-common";
import { ChatHeader } from "./components/ChatHeader";
import { LeftSidebar } from "./components/LeftSidebar";
import { RightSidebar } from "./components/RightSidebar";
import { ChatInputArea } from "./components/ChatInputArea";
import { ChatThread } from "./components/ChatThread";
import { AuthModal } from "./components/AuthModal";
import { RenameModal } from "./components/RenameModal";
import type { AuthState } from "./types";
import { storageKey } from "./utils/storageMigration";
import "./App.css";

const { Content } = Layout;
const { useToken } = theme;
const { Text } = Typography;

type ModelInfo = {
  provider: string;
  name: string;
  display_name?: string;
  capabilities: string[];
  max_output_tokens: number;
  supported_adapters: string[];
  default_adapter: string;
  fallback_adapters: string[];
  supported_builtin_tools: string[];
  requires_api_key?: boolean;
  has_api_key?: boolean;
};

type AgentInfo = {
  qualified_id: string;
  display_name?: string;
  allowed_surface_ids?: string[] | null;
};

type AgentListResponse = {
  agents: AgentInfo[];
  total: number;
};

type SurfaceInfo = {
  id: string;
  display_name?: string;
};

type SurfacesListResponse = {
  surfaces: SurfaceInfo[];
  total: number;
};

// defaultOverrides and overrides state are now managed by useRequestContext from @motet/ui-common

/**
 * Checks if the user is authenticated.
 * Returns true if user has JWT, service account token, or API key.
 */
function isAuthenticated(auth: AuthState): boolean {
  return !!(
    (auth.jwt && auth.jwt.trim().length > 20) ||
    (auth.serviceAccountToken && auth.serviceAccountToken.trim().length > 0) ||
    (auth.apiKey && auth.apiKey.trim().length > 0)
  );
}

/**
 * Inner component that consumes Ant Design theme tokens.
 * Must be rendered inside ConfigProvider to access the design token context.
 * This separation allows the outer App to manage theme state before ConfigProvider.
 */
function AppContent({ darkMode, setDarkMode }: { darkMode: boolean; setDarkMode: (value: boolean) => void }) {
  // Access theme tokens from ConfigProvider context (colors, borders, etc.)
  const { token } = useToken();
  
  // ─────────────────────────────────────────────────────────────────────────────
  // CUSTOM HOOKS: Each hook encapsulates a specific domain of functionality.
  // The hooks are designed to be composable and share minimal state between them.
  // ─────────────────────────────────────────────────────────────────────────────
  const { 
    auth, 
    setAuth, 
    showAuthModal, 
    setShowAuthModal, 
    userDisplayName, 
    handleSsoLogin,
    authorizedMessages,
    openOAuthPopup,
    buildHeaders,
    logout,
  } = useAuth();
  const [signedOut, setSignedOut] = useState(() => wasSignedOut());

  // Harden auth persistence on refresh for demo usage (API-key workflows).
  useEffect(() => {
    try {
      localStorage.setItem(storageKey("auth"), JSON.stringify(auth));
      if (auth.apiKey && auth.apiKey.trim()) {
        localStorage.setItem(storageKey("last_api_key"), auth.apiKey);
      }
    } catch {
      // ignore persistence errors
    }
  }, [auth]);

  // Restore API key fallback if auth object exists but apiKey is temporarily empty on refresh.
  useEffect(() => {
    if (auth.apiKey && auth.apiKey.trim()) return;
    try {
      const lastApiKey = localStorage.getItem(storageKey("last_api_key")) || "";
      if (lastApiKey.trim()) {
        setAuth((prev) => ({ ...prev, apiKey: lastApiKey }));
      }
    } catch {
      // ignore localStorage errors
    }
  }, [auth.apiKey, setAuth]);

  const [selectedAgentId, setSelectedAgentId] = useState<string>(() => {
    try {
      return String(localStorage.getItem(storageKey("agent_id")) || "");
    } catch {
      return "";
    }
  });

  const [selectedSurfaceId, setSelectedSurfaceId] = useState<string>(() => {
    try {
      const stored = String(localStorage.getItem(storageKey("surface_id")) || "").trim();
      return stored || "demo_chat";
    } catch {
      return "demo_chat";
    }
  });

  const {
    conversations,
    activeConversationKey,
    // addConversation, // Not used directly in UI
    setConversation,
    handleNewConversation,
    handleConversationChange,
    handleRenameConversation,
    handleRenameSubmit,
    handleDeleteConversation,
    handleDeleteAllConversations,
    persistConversationTitle,
    conversationId,
    // setConversationId, // Managed by hook
    showRenameModal,
    setShowRenameModal,
    renameValue,
    setRenameValue,
    updatedTitlesRef,
    refreshConversationList,
  } = useConversation(auth, {
    agent_id: selectedAgentId || "default",
    surface_id: selectedSurfaceId || "demo_chat",
  });

  // Ref to the Attachments component for programmatic file selection.
  const attachmentsRef = useRef<any>(null);

  // ─────────────────────────────────────────────────────────────────────────────
  // LOCAL APP STATE: UI toggles and transient values not owned by hooks.
  // ─────────────────────────────────────────────────────────────────────────────
  const {
    overrides,
    setOverrides,
  } = useRequestContext({
    storageKey: storageKey("overrides"),
  });

  // Attachments hook: manages file uploads, blob URL caching, and server cleanup.
  // Requires `auth` for authenticated API calls to /api/v1/artifacts.
  // Passes conversationId to associate uploads with the current conversation.
  // IMPORTANT: Pass overrides so upload-triggered derivations (e.g., OCR) can align with selected model.
  const {
    attachmentList,
    setAttachmentList,
    imageBlobUrls,
    showAttachments,
    setShowAttachments,
    draftUploads,
    setDraftUploads,
    handleUpload,
    ensureImagePreview,
    videoStreamUrls,
    ensureVideoSource,
    deleteArtifactBestEffort,
    removeBlobUrl,
    fileCardList
  } = useAttachments(auth, conversationId, overrides);
  const [inputValue, setInputValue] = useState<string>("");
  const [watchEvents, setWatchEvents] = useState(false);
  const [showErrors, setShowErrors] = useState<boolean>(false);
  const [showRagControls, setShowRagControls] = useState<boolean>(false);
  const [siderCollapsed, setSiderCollapsed] = useState<boolean>(false);       // Left sidebar collapse state
  const [rightSiderCollapsed, setRightSiderCollapsed] = useState<boolean>(false); // Right sidebar collapse state
  const [ragControls, setRagControls] = useState<RagControlsValue>(defaultRagControlsValue);
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [availableAgents, setAvailableAgents] = useState<AgentInfo[]>([]);
  const [availableSurfaces, setAvailableSurfaces] = useState<SurfaceInfo[]>([]);
  const selectedAgentQualifiedId = selectedAgentId || "core.default";
  const selectedAgentDisplayName = useMemo(
    () => resolveAgentDisplayName(selectedAgentQualifiedId, availableAgents).agentName,
    [availableAgents, selectedAgentQualifiedId]
  );

  // Overrides are persisted automatically by useRequestContext

  // Keep overrides aligned when an always-on model (e.g. Kimi K3) is selected.
  useEffect(() => {
    const provider = String(overrides?.model_provider || "");
    const modelName = String(overrides?.model_name || "");
    if (!treatsThinkingAsAlwaysOn(provider, modelName)) return;
    if (overrides?.enable_thinking === true && overrides?.reasoning_effort === "max") return;
    setOverrides((prev) => ({
      ...(prev || {}),
      enable_thinking: true,
      reasoning_effort: "max",
    }));
  }, [
    overrides?.model_provider,
    overrides?.model_name,
    overrides?.enable_thinking,
    overrides?.reasoning_effort,
    setOverrides,
  ]);

  useEffect(() => {
    try {
      localStorage.setItem(storageKey("agent_id"), selectedAgentId || "");
    } catch {
      // ignore
    }
  }, [selectedAgentId]);

  useEffect(() => {
    try {
      localStorage.setItem(storageKey("surface_id"), selectedSurfaceId || "demo_chat");
    } catch {
      // ignore
    }
  }, [selectedSurfaceId]);

  // Credential presence only — JWT string rotation must not refetch catalogs.
  const authenticated = isAuthenticated(auth);

  useEffect(() => {
    if (!authenticated) return;
    clearSignedOutFlag();
    setSignedOut(false);
  }, [authenticated]);

  // Fetch model list for the model picker. Auth headers let the API include vault keys.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/v1/models", {
          headers: authenticated ? buildHeaders() : undefined,
        });
        if (!res.ok) return;
        const data = (await res.json()) as ModelInfo[];
        if (!cancelled && Array.isArray(data)) setAvailableModels(data);
      } catch {
        // ignore (UI can still operate without a picker)
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authenticated]);

  // Drop a persisted override if that provider has no API key.
  useEffect(() => {
    const provider = String(overrides?.model_provider || "").trim();
    const name = String(overrides?.model_name || "").trim();
    if (!provider || !name || availableModels.length === 0) return;
    const match = availableModels.find((m) => m.provider === provider && m.name === name);
    if (
      match &&
      typeof match.has_api_key === "boolean" &&
      match.requires_api_key &&
      !match.has_api_key
    ) {
      setOverrides((prev) => ({
        ...(prev || {}),
        model_provider: "",
        model_name: "",
      }));
    }
  }, [availableModels, overrides?.model_provider, overrides?.model_name, setOverrides]);

  // Fetch role-filtered agents for the selector.
  useEffect(() => {
    let cancelled = false;
    if (!authenticated) {
      setAvailableAgents([]);
      return;
    }
    (async () => {
      try {
        const headers = buildHeaders();
        const res = await fetch("/api/v1/agents", { headers });
        if (!res.ok) {
          if (!cancelled) setAvailableAgents([]);
          return;
        }
        const data = (await res.json()) as AgentListResponse;
        const agents = Array.isArray(data?.agents) ? data.agents : [];
        if (!cancelled) setAvailableAgents(agents);
      } catch {
        if (!cancelled) setAvailableAgents([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authenticated]);

  // Fetch surfaces catalog for the surface picker.
  useEffect(() => {
    let cancelled = false;
    if (!authenticated) {
      setAvailableSurfaces([]);
      return;
    }
    (async () => {
      try {
        const headers = buildHeaders();
        const res = await fetch("/api/v1/surfaces", { headers });
        if (!res.ok) {
          if (!cancelled) setAvailableSurfaces([]);
          return;
        }
        const data = (await res.json()) as SurfacesListResponse;
        const surfaces = Array.isArray(data?.surfaces) ? data.surfaces : [];
        if (!cancelled) setAvailableSurfaces(surfaces);
      } catch {
        if (!cancelled) setAvailableSurfaces([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authenticated]);

  // Clear stale selection when current selection is no longer visible/allowed.
  useEffect(() => {
    if (!selectedAgentId) return;
    // During initial load availableAgents is often empty; don't clear yet.
    if (availableAgents.length === 0) return;
    if (!availableAgents.some((a) => a.qualified_id === selectedAgentId)) {
      setSelectedAgentId("");
    }
  }, [availableAgents, selectedAgentId]);

  // When agent changes, keep surface if allowed; otherwise prefer demo_chat or first allowed.
  useEffect(() => {
    if (availableSurfaces.length === 0) return;
    const agentId = selectedAgentId || "core.default";
    const agent = availableAgents.find((a) => a.qualified_id === agentId);
    const allowed = agent?.allowed_surface_ids;
    const current = (selectedSurfaceId || "demo_chat").trim() || "demo_chat";
    const catalogIds = new Set(availableSurfaces.map((s) => s.id));
    const allowedSet =
      allowed == null || allowed.length === 0
        ? catalogIds
        : new Set(allowed.filter((id) => catalogIds.has(id)));
    if (allowedSet.has(current)) return;
    if (allowedSet.has("demo_chat")) {
      setSelectedSurfaceId("demo_chat");
      return;
    }
    const first = Array.from(allowedSet)[0];
    if (first) setSelectedSurfaceId(first);
  }, [availableAgents, availableSurfaces, selectedAgentId, selectedSurfaceId]);

  // ─────────────────────────────────────────────────────────────────────────────
  // EVENT BUS: Optional SSE subscription for observing system events (debugging).
  // Only connects when `watchEvents` is true and user is authenticated.
  // ─────────────────────────────────────────────────────────────────────────────
  const { eventBus, errors: eventErrors } = useEventBus(auth, watchEvents);

  // ─────────────────────────────────────────────────────────────────────────────
  // CHAT LOGIC: Core hook integrating Ant Design X's useXChat with Motet backend.
  // Handles message streaming, throttling, Reasoning Chain extraction, and OAuth prompts.
  // ─────────────────────────────────────────────────────────────────────────────
  const {
    messages: throttledMessages, // Use throttled messages for UI
    bubbleListItems,
    onRequest,
    isRequesting,
    requestContext,
    reasoningPanels,
    thinkingState,
    historyWarning,
    storeReadyKey,
  } = useMotetChat(
    auth,
    conversationId,
    activeConversationKey,
    imageBlobUrls,
    ensureImagePreview,
    videoStreamUrls,
    ensureVideoSource,
    darkMode,
    overrides,
    ragControls,
    // Always send a concrete agent id (empty selector → core.default) so chat
    // requests and agent_turn Inputs never omit / null agent_id.
    selectedAgentQualifiedId,
    selectedSurfaceId || "demo_chat",
    availableAgents,
    authorizedMessages,
    openOAuthPopup
  );

  // ─────────────────────────────────────────────────────────────────────────────
  // SIDE EFFECTS
  // ─────────────────────────────────────────────────────────────────────────────

  /**
   * Auto-rename conversation title with first user message — demo_chat only.
   * Other surfaces (e.g. openai_compat) keep the registry default ("New Chat")
   * until the user renames manually.
   *
   * Sidebar label updates immediately. PATCH waits until the first turn
   * finishes: "+" ids are local-only until agent_turn claims them, and
   * conversation_rename returns 403 for unclaimed ids.
   */
  const pendingAutoTitleRef = useRef<{ key: string; title: string } | null>(null);
  const completedTurnsRef = useRef<Set<string>>(new Set());
  const inFlightTurnKeysRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const surface = (selectedSurfaceId || "demo_chat").trim() || "demo_chat";
    if (surface !== "demo_chat") return;

    const firstUserMsg = throttledMessages.find((msgInfo: any) => {
      const msg = msgInfo.message || msgInfo;
      return msg.role === "user";
    });
    const msg = firstUserMsg ? (firstUserMsg.message || firstUserMsg) : null;
    const content = typeof msg?.content === "string" ? msg.content.trim() : "";
    const currentConv = conversations.find((c: any) => c.key === activeConversationKey);
    const canSetNewTitle = shouldQueueAutoTitle({
      storeReadyKey,
      activeKey: activeConversationKey,
      label: currentConv?.label,
      alreadyUpdated: updatedTitlesRef.current.has(activeConversationKey),
      hasUserMessage: Boolean(content),
    });
    const canExpandLegacyTitle =
      Boolean(content) &&
      !updatedTitlesRef.current.has(activeConversationKey) &&
      isLegacyTruncatedAutoTitle(currentConv?.label, content);
    if (!canSetNewTitle && !canExpandLegacyTitle) {
      return;
    }

    const oneLine = content.replace(/\s+/g, " ").trim();
    const title = oneLine.length > 500 ? oneLine.slice(0, 500) : oneLine;
    if (currentConv) {
      setConversation(activeConversationKey, { ...currentConv, label: title });
    }
    updatedTitlesRef.current.add(activeConversationKey);
    pendingAutoTitleRef.current = { key: activeConversationKey, title };
  }, [
    throttledMessages,
    activeConversationKey,
    storeReadyKey,
    conversations,
    selectedSurfaceId,
    setConversation,
    updatedTitlesRef,
  ]);

  useEffect(() => {
    if (isRequesting) {
      inFlightTurnKeysRef.current.add(activeConversationKey);
      return;
    }
    for (const key of inFlightTurnKeysRef.current) {
      completedTurnsRef.current.add(key);
    }
    inFlightTurnKeysRef.current.clear();

    const pending = pendingAutoTitleRef.current;
    if (
      !shouldFlushAutoTitlePersist({
        pendingKey: pending?.key ?? null,
        turnCompletedForPending: Boolean(
          pending && completedTurnsRef.current.has(pending.key)
        ),
        isRequesting,
      })
    ) {
      return;
    }
    pendingAutoTitleRef.current = null;
    persistConversationTitle(pending!.key, pending!.title);
  }, [isRequesting, activeConversationKey, persistConversationTitle]);

  // ─────────────────────────────────────────────────────────────────────────────
  // EVENT HANDLERS
  // ─────────────────────────────────────────────────────────────────────────────

  /**
   * Handles sending a chat message.
   * - Validates that there's content or attachments to send
   * - Packages attachments metadata with the message
   * - Clears input state after sending
   * - Blocks sending if user is not authenticated
   */
  const handleSend = () => {
    // Block sending if user is not authenticated
    if (!isAuthenticated(auth)) {
      setShowAuthModal(true);
      return;
    }

    const trimmed = String(inputValue || "").trim();
    // Attachments validation is handled in ChatInputArea or here?
    // ChatInputArea checks for "uploading" status.
    // Here we check for empty content.
    if (!trimmed && attachmentList.length === 0) return;

    onRequest({
      messages: [{
        role: "user",
        content: trimmed,
        ...(attachmentList.length > 0 ? {
          attachments: attachmentList.map(att => ({
            artifact_id: att.artifact_id,
            filename: att.filename,
            content_type: att.content_type,
            bytes: att.bytes
          }))
        } : {})
      }],
      ...requestContext
    });

    setInputValue("");
    setAttachmentList([]);
    setDraftUploads([]);
    setShowAttachments(false);
  };

  const handleLogout = () => {
    const idToken = localStorage.getItem("motet_id_token");
    const headers = buildHeaders();
    const redirectUri = appLogoutRedirectUri();
    markSignedOut();
    setSignedOut(true);
    logout();
    void finishRemoteLogout({ headers, idToken, redirectUri });
  };

  const authModal = (
    <AuthModal
      open={showAuthModal}
      onCancel={() => setShowAuthModal(false)}
      auth={auth}
      setAuth={setAuth}
    />
  );

  // ─────────────────────────────────────────────────────────────────────────────
  // RENDER: Signed-out landing (no chat shell, no leftover session UI)
  // ─────────────────────────────────────────────────────────────────────────────
  if (!authenticated) {
    return (
      <>
        <SignedOutPage
          variant={signedOut ? "signed_out" : "welcome"}
          productLabel="Chat Explorer"
          logoSrc={`${import.meta.env.BASE_URL}images/Motet Identity Row - ${darkMode ? "KO" : "Black"}.svg`}
          isDarkMode={darkMode}
          onToggleDarkMode={() => setDarkMode(!darkMode)}
          onSsoLogin={handleSsoLogin}
          onOpenAuthModal={() => setShowAuthModal(true)}
        />
        {authModal}
      </>
    );
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // RENDER: Main application layout
  // ─────────────────────────────────────────────────────────────────────────────
  return (
    <>
      <Layout className="app-shell" style={{ background: token.colorBgLayout }}>
        <ChatHeader
          token={token}
          auth={auth}
          userDisplayName={userDisplayName}
          onSsoLogin={handleSsoLogin}
          onLogout={handleLogout}
          onOpenAuthModal={() => setShowAuthModal(true)}
          onClearNonJwtAuth={handleLogout}
          darkMode={darkMode}
          setDarkMode={setDarkMode}
          watchEvents={watchEvents}
          setWatchEvents={setWatchEvents}
          showErrors={showErrors}
          setShowErrors={setShowErrors}
          availableAgents={availableAgents}
          selectedAgentId={selectedAgentId}
          onSelectAgentId={setSelectedAgentId}
          availableSurfaces={availableSurfaces}
          selectedSurfaceId={selectedSurfaceId || "demo_chat"}
          onSelectSurfaceId={(surfaceId) => {
            setSelectedSurfaceId((surfaceId || "").trim() || "demo_chat");
          }}
        />

        <Layout style={{ background: token.colorBgLayout }}>
          <LeftSidebar
            token={token}
            collapsed={siderCollapsed}
            setCollapsed={setSiderCollapsed}
            conversations={conversations}
            activeKey={activeConversationKey}
            onNewConversation={handleNewConversation}
            onSelectConversation={handleConversationChange}
            onRenameConversation={handleRenameConversation}
            onDeleteConversation={handleDeleteConversation}
            onDeleteAllConversations={handleDeleteAllConversations}
            onRefreshConversationList={refreshConversationList}
          />

          <Content className="content" style={{ background: token.colorBgLayout }}>
            <Card 
              style={{ background: token.colorBgContainer }}
              styles={{ body: { paddingBottom: 12 } }}
              title={
                <Flex justify="space-between" align="center">
                  <Space>
                    <Text>Conversation</Text>
                    {thinkingState && (
                      <Badge 
                        status="processing" 
                        text={
                          <Space size="small">
                            <Spin size="small" />
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              {thinkingState.charAt(0) + thinkingState.slice(1).toLowerCase()}
                            </Text>
                          </Space>
                        }
                      />
                    )}
                  </Space>
                </Flex>
              }
            >
              <div className="chat-container">
                {historyWarning ? (
                  <Alert
                    type="warning"
                    showIcon
                    message="Conversation cannot be opened"
                    description={historyWarning}
                    style={{ marginBottom: 12 }}
                  />
                ) : null}
                <ChatThread token={token} items={bubbleListItems} />
                <ChatInputArea
                  token={token}
                  inputValue={inputValue}
                  setInputValue={setInputValue}
                  isRequesting={isRequesting}
                  onSend={handleSend}
                  showAttachments={showAttachments}
                  setShowAttachments={setShowAttachments}
                  fileCardList={fileCardList}
                  draftUploads={draftUploads}
                  setDraftUploads={setDraftUploads}
                  handleUpload={handleUpload}
                  attachmentsRef={attachmentsRef}
                  disabled={!authenticated}
                  showRagControls={showRagControls}
                  setShowRagControls={setShowRagControls}
                  ragControls={ragControls}
                  setRagControls={setRagControls}
                  availableModels={availableModels}
                  selectedModelProvider={String(overrides?.model_provider || "")}
                  selectedModelName={String(overrides?.model_name || "")}
                  onSelectModel={(provider, modelName) => {
                    setOverrides((prev) => {
                      const next = {
                        ...(prev || {}),
                        model_provider: provider || "",
                        model_name: modelName || "",
                      };
                      if (treatsThinkingAsAlwaysOn(next.model_provider, next.model_name)) {
                        next.enable_thinking = true;
                        next.reasoning_effort = "max";
                      }
                      return next;
                    });
                  }}
                  enableThinking={!!overrides?.enable_thinking}
                  onEnableThinking={(v) => {
                    setOverrides((prev) => ({ ...(prev || {}), enable_thinking: v }));
                  }}
                  reasoningEffort={(overrides?.reasoning_effort as ReasoningEffort) || "medium"}
                  onReasoningEffortChange={(v) => {
                    setOverrides((prev) => ({ ...(prev || {}), reasoning_effort: v || "medium" }));
                  }}
                  onAttachmentsChange={(info) => {
                    const nextUids = new Set(info.fileList.map((f: any) => f.uid));
                    setDraftUploads((prev) => prev.filter((d) => nextUids.has(d.uid)));
                    setAttachmentList((prev) => {
                      const removed = prev.filter((a) => !nextUids.has(a.artifact_id));
                      for (const r of removed) {
                        removeBlobUrl(r.artifact_id);
                        deleteArtifactBestEffort(r.artifact_id);
                      }
                      return prev.filter((a) => nextUids.has(a.artifact_id));
                    });
                  }}
                />
              </div>
            </Card>
          </Content>

          <RightSidebar
            token={token}
            collapsed={rightSiderCollapsed}
            setCollapsed={setRightSiderCollapsed}
            reasoningPanels={reasoningPanels}
            selectedAgentId={selectedAgentQualifiedId}
            selectedAgentName={selectedAgentDisplayName}
            eventBus={eventBus}
            errors={eventErrors}
            watchEventsEnabled={watchEvents}
            showErrorsEnabled={showErrors}
          />
        </Layout>
      </Layout>

      {authModal}
      <RenameModal
        open={showRenameModal}
        onCancel={() => setShowRenameModal(false)}
        onOk={handleRenameSubmit}
            value={renameValue}
        onChange={setRenameValue}
      />
    </>
  );
}

/**
 * Root App component.
 * Manages theme state (dark/light mode) and provides it to the entire app via ConfigProvider.
 * This outer component exists because theme configuration must be set before ConfigProvider
 * renders, so we can't use useToken() here — that's why we have the inner AppContent.
 */
function App() {
  const [darkMode, setDarkMode] = useState<boolean>(() => {
    const saved = localStorage.getItem(storageKey("dark_mode"));
    return saved ? JSON.parse(saved) : true;
  });

  // Persist dark mode
  useEffect(() => {
    localStorage.setItem(storageKey("dark_mode"), JSON.stringify(darkMode));
  }, [darkMode]);

  return (
    <ConfigProvider 
      theme={{ 
        token: { colorPrimary: "#1677ff" },
        algorithm: darkMode ? theme.darkAlgorithm : theme.defaultAlgorithm,
        components: {
          Card: {
            colorBgContainer: darkMode ? '#141414' : '#ffffff',
          },
          Layout: {
            headerBg: darkMode ? '#141414' : '#ffffff',
            bodyBg: darkMode ? '#000000' : '#f5f5f5',
          },
        },
      }}
    >
      <AppContent darkMode={darkMode} setDarkMode={setDarkMode} />
    </ConfigProvider>
  );
}

export default App;
