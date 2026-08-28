/**
 * Motet UI Common - Conversation Manager Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Framework-agnostic conversation list management. Encapsulates CRUD
 *     operations, API synchronization, localStorage caching, and scope-based
 *     conversation switching by agent and surface scope.
 *
 *     The hook accepts a ConversationStore interface that abstracts the
 *     underlying state management (e.g., Ant Design X useXConversations).
 *     This allows any UI framework to use the conversation management logic
 *     without coupling to a specific component library.
 *
 *     Features:
 *     - Create, switch, rename, delete conversations
 *     - Load conversation list from API when authenticated
 *     - Scope-based caching (agent_id, surface_id)
 *     - localStorage persistence for active conversation and list snapshot
 *     - Stale response rejection via monotonic version counter
 *     - Track auto-updated titles
 *     - Clear list/active conversation on logout; replace (not merge) on
 *       principal change so principals do not see each other's sidebar
 *     - JWT refresh for the same principal does not reset or refetch the list
 *
 * Dependencies:
 *     - React: useState, useCallback, useRef, useEffect
 *     - @motet/ui-common: randomId, buildHeaders, listConversations,
 *       conversationAuth (identity + sync plan)
 *
 * Usage:
 *     import { useConversationManager } from "@motet/ui-common";
 *     const manager = useConversationManager(store, auth, scope);
 */
import { useState, useCallback, useRef, useEffect } from "react";
import type { AuthState } from "../types";
import { randomId, qualifyWithCoreNamespace } from "../utils";
import { buildHeaders } from "./useAuth";
import {
  listConversations,
  updateConversationTitle,
  deleteConversation,
} from "../api/conversations";
import {
  authIdentityKey,
  isAuthenticated,
  planConversationListSync,
} from "./conversationAuth";

export {
  authIdentityKey,
  isAuthenticated,
  jwtSubject,
  planConversationListSync,
} from "./conversationAuth";
export type { ConversationListSyncPlan } from "./conversationAuth";

// ─────────────────────────────────────────────────────────────────────────────
// TYPES
// ─────────────────────────────────────────────────────────────────────────────

/** A single conversation list entry. */
export type ConversationEntry = {
  key: string;
  label: string;
  timestamp?: number;
};

/**
 * Abstraction over the conversation list state management.
 * Implementations must provide CRUD operations on the list; the hook
 * handles all orchestration, API calls, and caching.
 */
export interface ConversationStore {
  conversations: ConversationEntry[];
  activeConversationKey: string;
  setActiveConversationKey: (key: string) => void;
  addConversation: (entry: ConversationEntry, position: "prepend" | "append") => void;
  setConversation: (key: string, entry: ConversationEntry) => void;
  removeConversation: (key: string) => void;
  setConversations: (list: ConversationEntry[]) => void;
}

/** Scope for conversation list (agent + surface). */
export type ConversationListScope = {
  agent_id?: string | null;
  surface_id?: string | null;
};

/** Options for useConversationManager. */
export type UseConversationManagerOptions = {
  /** localStorage key prefix (defaults to "motet_conversation"). */
  storageKeyPrefix?: string;
};

// ─────────────────────────────────────────────────────────────────────────────
// PURE HELPERS (exported for reuse by wrapper hooks)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Mirror the backend's resolve_id: bare names without a "." are assumed to
 * live under the "core" namespace. This ensures "default" and "core.default"
 * produce the same cache / scope key.
 */
export function normalizeAgentId(raw?: string | null): string {
  const id = (raw || "default").trim() || "default";
  return qualifyWithCoreNamespace(id);
}

export function scopeStorageKey(prefix: string, scope?: ConversationListScope | null): string {
  const agent = normalizeAgentId(scope?.agent_id);
  const surface = (scope?.surface_id || "*").trim() || "*";
  return `${prefix}_current:${agent}:${surface}`;
}

export function scopeListCacheKey(prefix: string, scope?: ConversationListScope | null): string {
  const agent = normalizeAgentId(scope?.agent_id);
  const surface = (scope?.surface_id || "*").trim() || "*";
  return `${prefix}_list:${agent}:${surface}`;
}

export function scopeKey(scope?: ConversationListScope | null): string {
  return `${normalizeAgentId(scope?.agent_id)}:${(scope?.surface_id || "*").trim() || "*"}`;
}

/**
 * Load cached conversation list from localStorage for a given scope.
 */
export function cachedConversationsFor(
  prefix: string,
  targetScope?: ConversationListScope | null,
): ConversationEntry[] {
  try {
    const raw = localStorage.getItem(scopeListCacheKey(prefix, targetScope));
    const arr = raw ? (JSON.parse(raw) as Array<{ id: string; title: string; updated_at: number }>) : [];
    if (Array.isArray(arr) && arr.length > 0) {
      return arr.map((c) => ({ key: c.id, label: c.title || "New Chat", timestamp: c.updated_at || Date.now() }));
    }
  } catch { /* ignore */ }
  return [];
}

/**
 * Compute initial boot conversations and active key from localStorage cache.
 * Call once during hook initialization (inside useState initializer).
 */
export function computeInitialConversations(
  prefix: string,
  scope?: ConversationListScope | null,
): { conversations: ConversationEntry[]; activeKey: string } {
  const fallbackKey = randomId();

  let convs: ConversationEntry[];
  try {
    const cached = cachedConversationsFor(prefix, scope);
    convs = cached.length > 0 ? cached : [{ key: fallbackKey, label: "New Chat", timestamp: Date.now() }];
  } catch {
    convs = [{ key: fallbackKey, label: "New Chat", timestamp: Date.now() }];
  }

  let activeKey = convs[0]?.key || fallbackKey;
  try {
    const stored = localStorage.getItem(scopeStorageKey(prefix, scope))?.trim();
    if (stored && convs.some((c) => c.key === stored)) {
      activeKey = stored;
    }
  } catch { /* ignore */ }

  return { conversations: convs, activeKey };
}

// ─────────────────────────────────────────────────────────────────────────────
// HOOK
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Framework-agnostic conversation management hook.
 *
 * Accepts a ConversationStore (provided by the UI framework adapter) and
 * orchestrates all CRUD, API sync, caching, and scope logic.
 */
export function useConversationManager(
  store: ConversationStore,
  auth?: AuthState | null,
  scope?: ConversationListScope | null,
  options?: UseConversationManagerOptions,
) {
  const prefix = options?.storageKeyPrefix || "motet_conversation";

  const {
    conversations,
    activeConversationKey,
    setActiveConversationKey,
    addConversation,
    setConversation,
    removeConversation,
    setConversations,
  } = store;

  const [conversationId, setConversationId] = useState<string>(activeConversationKey);

  // Rename modal state
  const [showRenameModal, setShowRenameModal] = useState(false);
  const [renameConvKey, setRenameConvKey] = useState<string>("");
  const [renameValue, setRenameValue] = useState<string>("");

  // ─────────────────────────────────────────────────────────────────────────────
  // REFS
  // ─────────────────────────────────────────────────────────────────────────────

  const updatedTitlesRef = useRef<Set<string>>(new Set());

  const authRef = useRef(auth);
  authRef.current = auth;
  const conversationsRef = useRef(conversations);
  conversationsRef.current = conversations;
  const prevScopeKeyRef = useRef<string | null>(null);
  const prevAuthIdentityRef = useRef<string | null>(null);
  const loadVersionRef = useRef(0);

  // ─────────────────────────────────────────────────────────────────────────────
  // INTERNAL HELPERS
  // ─────────────────────────────────────────────────────────────────────────────

  function clearScopeLocalCache(targetScope?: ConversationListScope | null) {
    try {
      localStorage.removeItem(scopeListCacheKey(prefix, targetScope));
      localStorage.removeItem(scopeStorageKey(prefix, targetScope));
    } catch {
      /* ignore */
    }
  }

  function resetToFreshConversation() {
    const newId = randomId();
    setConversations([{ key: newId, label: "New Chat", timestamp: Date.now() }]);
    setActiveConversationKey(newId);
    setConversationId(newId);
    updatedTitlesRef.current = new Set();
  }

  function restoreActiveForScope(
    targetScope: ConversationListScope | null | undefined,
    validKeys: Set<string>,
  ) {
    const stored = localStorage.getItem(scopeStorageKey(prefix, targetScope))?.trim();
    if (stored && validKeys.has(stored)) {
      setActiveConversationKey(stored);
      setConversationId(stored);
    } else if (validKeys.size > 0) {
      const first = validKeys.values().next().value!;
      setActiveConversationKey(first);
      setConversationId(first);
    } else {
      resetToFreshConversation();
    }
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // API SYNC
  // ─────────────────────────────────────────────────────────────────────────────

  const loadConversationList = useCallback(async (opts: { replace?: boolean; _v?: number } = {}) => {
    const replace = opts.replace === true;
    const version = opts._v;
    const currentAuth = authRef.current;
    if (!currentAuth || !isAuthenticated(currentAuth)) return;

    const headers = buildHeaders(currentAuth);
    const serverList = await listConversations(headers, {
      agent_id: scope?.agent_id ?? undefined,
      surface_id: scope?.surface_id ?? undefined,
    });

    if (version !== undefined && version !== loadVersionRef.current) return;
    if (serverList === null) return;

    if (replace) {
      const items = serverList.length > 0
        ? serverList.map((c) => ({ key: c.id, label: c.title || "New Chat", timestamp: c.updated_at }))
        : [{ key: randomId(), label: "New Chat", timestamp: Date.now() }];
      setConversations(items);
      restoreActiveForScope(scope, new Set(items.map((c) => c.key)));
    } else {
      const existingKeys = new Set(conversationsRef.current.map((c) => c.key));
      for (const c of serverList) {
        if (!existingKeys.has(c.id)) {
          addConversation({ key: c.id, label: c.title || "New Chat", timestamp: c.updated_at }, "append");
        }
      }
    }
  }, [addConversation, setConversations, setActiveConversationKey, scope?.agent_id, scope?.surface_id, prefix]);

  // Load on auth/scope change. Logout and principal change reset locally;
  // JWT rotation for the same principal is a no-op. First mount keeps the
  // hydrated cache visible and replaces from the API when it arrives.
  useEffect(() => {
    const identity = authIdentityKey(auth);
    const current = scopeKey(scope);
    const plan = planConversationListSync({
      identity,
      prevIdentity: prevAuthIdentityRef.current,
      scope: current,
      prevScope: prevScopeKeyRef.current,
    });

    prevAuthIdentityRef.current = identity || "";
    prevScopeKeyRef.current = identity ? current : null;

    if (plan.resetLocal) {
      clearScopeLocalCache(scope);
      resetToFreshConversation();
    } else if (plan.restoreCached) {
      const cached = cachedConversationsFor(prefix, scope);
      if (cached.length > 0) {
        setConversations(cached);
        restoreActiveForScope(scope, new Set(cached.map((c) => c.key)));
      } else {
        resetToFreshConversation();
      }
    }

    if (!plan.load) return;

    let version: number | undefined;
    if (plan.replace) {
      loadVersionRef.current += 1;
      version = loadVersionRef.current;
    }
    loadConversationList({ replace: plan.replace, _v: version });
  }, [auth, auth?.jwt, auth?.serviceAccountToken, auth?.apiKey, scope?.agent_id, scope?.surface_id, loadConversationList, setConversations, setActiveConversationKey, prefix]);

  // Persist active conversation per scope
  useEffect(() => {
    try {
      localStorage.setItem(scopeStorageKey(prefix, scope), conversationId);
    } catch { /* ignore */ }
  }, [conversationId, scope?.agent_id, scope?.surface_id, prefix]);

  // Persist list snapshot
  useEffect(() => {
    try {
      const snapshot = (conversations || []).map((c: any) => ({
        id: c.key,
        title: c.label || "New Chat",
        updated_at: Number(c.timestamp || Date.now()),
      }));
      localStorage.setItem(scopeListCacheKey(prefix, scope), JSON.stringify(snapshot));
    } catch { /* ignore */ }
  }, [conversations, scope?.agent_id, scope?.surface_id, prefix]);

  // Keep conversationId in sync
  useEffect(() => {
    setConversationId(activeConversationKey);
  }, [activeConversationKey]);

  // ─────────────────────────────────────────────────────────────────────────────
  // CRUD CALLBACKS
  // ─────────────────────────────────────────────────────────────────────────────

  const handleNewConversation = useCallback(() => {
    const newConvId = randomId();
    setConversationId(newConvId);
    addConversation({ key: newConvId, label: "New Chat", timestamp: Date.now() }, "prepend");
    setActiveConversationKey(newConvId);
  }, [addConversation, setActiveConversationKey]);

  const handleConversationChange = useCallback((key: string) => {
    setActiveConversationKey(key);
    setConversationId(key);
  }, [setActiveConversationKey]);

  const handleRenameConversation = useCallback((convKey: string) => {
    const conv = conversations.find((c: any) => c.key === convKey);
    if (conv) {
      setRenameConvKey(convKey);
      setRenameValue(conv.label || "");
      setShowRenameModal(true);
    }
  }, [conversations]);

  const persistConversationTitle = useCallback(
    async (convKey: string, title: string) => {
      if (!auth || !isAuthenticated(auth) || !title.trim()) return;
      try {
        await updateConversationTitle(convKey, title.trim(), buildHeaders(auth));
        loadConversationList();
      } catch (e) {
        console.warn("persist_conversation_title_failed", convKey, e);
      }
    },
    [auth, loadConversationList]
  );

  const handleRenameSubmit = useCallback(async () => {
    if (!renameConvKey || !renameValue.trim()) {
      setShowRenameModal(false);
      setRenameConvKey("");
      setRenameValue("");
      return;
    }
    const conv = conversations.find((c: any) => c.key === renameConvKey);
    if (conv) {
      if (auth && isAuthenticated(auth)) {
        try {
          await updateConversationTitle(renameConvKey, renameValue.trim(), buildHeaders(auth));
        } catch (e) {
          console.warn("rename_conversation_api_failed", renameConvKey, e);
        }
        loadConversationList();
      }
      setConversation(renameConvKey, { ...conv, label: renameValue.trim() });
    }
    setShowRenameModal(false);
    setRenameConvKey("");
    setRenameValue("");
  }, [renameConvKey, renameValue, conversations, setConversation, auth, loadConversationList]);

  const handleDeleteConversation = useCallback(async (convKey: string) => {
    if (conversations.length === 1) return;

    if (convKey === activeConversationKey) {
      const otherConv = conversations.find((c: any) => c.key !== convKey);
      if (otherConv) {
        setActiveConversationKey(otherConv.key);
        setConversationId(otherConv.key);
      }
    }

    if (auth && isAuthenticated(auth)) {
      try {
        await deleteConversation(convKey, buildHeaders(auth));
      } catch (e) {
        console.warn("delete_conversation_api_failed", convKey, e);
      }
      loadConversationList({ replace: true });
    }
    removeConversation(convKey);
  }, [conversations, activeConversationKey, removeConversation, setActiveConversationKey, auth, loadConversationList]);

  const handleDeleteAllConversations = useCallback(async () => {
    if (conversations.length === 0) return;
    const toDelete = [...conversations];
    const newId = randomId();
    setConversations([{ key: newId, label: "New Chat", timestamp: Date.now() }]);
    setActiveConversationKey(newId);
    setConversationId(newId);
    const headers = auth && isAuthenticated(auth) ? buildHeaders(auth) : null;
    for (const c of toDelete) {
      if (headers) {
        try {
          await deleteConversation(c.key, headers);
        } catch (e) {
          console.warn("delete_conversation_api_failed", c.key, e);
        }
      }
    }
    if (headers) loadConversationList({ replace: true });
  }, [conversations, setConversations, setActiveConversationKey, auth, loadConversationList]);

  return {
    conversations,
    activeConversationKey,
    setActiveConversationKey,
    setConversation,
    removeConversation,
    handleNewConversation,
    handleConversationChange,
    handleRenameConversation,
    handleRenameSubmit,
    handleDeleteConversation,
    handleDeleteAllConversations,
    persistConversationTitle,
    refreshConversationList: loadConversationList,

    conversationId,
    setConversationId,
    showRenameModal,
    setShowRenameModal,
    renameConvKey,
    setRenameConvKey,
    renameValue,
    setRenameValue,
    updatedTitlesRef,
  };
}
