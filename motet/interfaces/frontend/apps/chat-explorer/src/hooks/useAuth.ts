/**
 * Motet - Chat Explorer - Auth Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Re-exports auth hook from @motet/ui-common with Chat Explorer defaults
 *     (storage keys + SSO redirect URI /chat-explorer).
 *
 * Dependencies:
 *     - @motet/ui-common: useAuth, buildHeaders
 *     - ./../utils/storageMigration: Chat Explorer storage key helpers
 *
 * Usage:
 *     const { auth, setAuth, handleSsoLogin } = useAuth();
 *
 * Notes:
 *     Call migrateDemoChatXStorage() once at app boot before this hook runs
 *     so demo_chat_x_* keys are copied to chat_explorer_*. Logout also
 *     clears the last-API-key slot so the restore effect cannot sign the
 *     user back in.
 */
import { useAuth as useAuthBase, buildHeaders as baseBuildHeaders, buildAuthHeaders as baseBuildAuthHeaders } from "@motet/ui-common";
import type { UseAuthOptions } from "@motet/ui-common";
import { useCallback, useEffect } from "react";
import { storageKey } from "../utils/storageMigration";

const AUTH_KEY = storageKey("auth");
const LAST_API_KEY = storageKey("last_api_key");

// Re-export utilities
export function buildHeaders(auth: any) {
  const effectiveAuth = { ...auth };
  if (!effectiveAuth.apiKey) {
    try {
      const fallback = localStorage.getItem(LAST_API_KEY) || "";
      if (fallback.trim()) effectiveAuth.apiKey = fallback;
    } catch {
      // ignore
    }
  }
  return baseBuildHeaders(effectiveAuth);
}

export function buildAuthHeaders(auth: any) {
  const effectiveAuth = { ...auth };
  if (!effectiveAuth.apiKey) {
    try {
      const fallback = localStorage.getItem(LAST_API_KEY) || "";
      if (fallback.trim()) effectiveAuth.apiKey = fallback;
    } catch {
      // ignore
    }
  }
  return baseBuildAuthHeaders(effectiveAuth);
}

/**
 * useAuth hook with Chat Explorer defaults.
 */
export function useAuth(options?: UseAuthOptions) {
  const authState = useAuthBase({
    storageKey: AUTH_KEY,
    ssoRedirectUri: "/chat-explorer",
    ...options
  });

  // App-local API key fallback persistence for refresh reliability.
  useEffect(() => {
    try {
      if (authState.auth.apiKey && authState.auth.apiKey.trim()) {
        localStorage.setItem(LAST_API_KEY, authState.auth.apiKey);
      }
    } catch {
      // ignore
    }
  }, [authState.auth.apiKey]);

  useEffect(() => {
    if (authState.auth.apiKey && authState.auth.apiKey.trim()) return;
    try {
      const fallback = localStorage.getItem(LAST_API_KEY) || "";
      if (fallback.trim()) {
        authState.setAuth((prev: any) => ({ ...prev, apiKey: fallback }));
      }
    } catch {
      // ignore
    }
  }, [authState.auth.apiKey, authState.setAuth]);

  const logoutBase = authState.logout;
  const logout = useCallback(() => {
    try {
      localStorage.removeItem(LAST_API_KEY);
    } catch {
      // ignore
    }
    logoutBase();
  }, [logoutBase]);

  return { ...authState, logout };
}
