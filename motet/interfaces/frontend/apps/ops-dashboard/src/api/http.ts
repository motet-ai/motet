/**
 * Motet - Ops Dashboard - HTTP auth headers
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-17
 *
 * Description:
 *     Shared Authorization header for manage-app fetches. Pages still call
 *     fetch themselves; this is not a request wrapper.
 *
 *     Live ``AuthState`` from ``useAuth`` is the source of truth (JWT,
 *     service-account token, API key, and dev identity headers). App.tsx
 *     calls ``setLiveAuth`` on every auth change. Storage is only a
 *     first-paint fallback so catalog fetches before that effect still
 *     send a Bearer token.
 *
 * Notes:
 *     Do not read only ``motet_jwt_token``. SSO stores ``motet_access_token``
 *     and ``auth.jwt`` in the persisted auth blob.
 */

import { buildAuthHeaders, defaultAuthState, type AuthState } from "@motet/ui-common";

const AUTH_STORAGE_KEYS = ["admin_dashboard_auth", "motet_auth"] as const;

let liveAuth: AuthState | null = null;

export function setLiveAuth(auth: AuthState | null): void {
  liveAuth = auth;
}

function readStoredJwt(): string {
  const access = (localStorage.getItem("motet_access_token") || "").trim();
  if (access.length > 20) {
    return access;
  }
  const legacy = (localStorage.getItem("motet_jwt_token") || "").trim();
  if (legacy.length > 20) {
    return legacy;
  }
  return "";
}

function authFromStorage(): AuthState {
  let persisted: Partial<AuthState> = {};
  for (const key of AUTH_STORAGE_KEYS) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) {
        continue;
      }
      persisted = JSON.parse(raw) as Partial<AuthState>;
      break;
    } catch {
      // Ignore malformed persisted auth blobs.
    }
  }
  const jwt = readStoredJwt() || (persisted.jwt || "").trim();
  return {
    ...defaultAuthState,
    ...persisted,
    jwt,
  };
}

export function getAuthHeaders(): Record<string, string> {
  const stored = authFromStorage();
  const live = liveAuth;
  const jwt =
    live?.jwt && live.jwt.trim().length > 20 ? live.jwt.trim() : stored.jwt;
  const serviceAccountToken =
    (live?.serviceAccountToken || "").trim() || stored.serviceAccountToken;
  const apiKey = (live?.apiKey || "").trim() || stored.apiKey;
  return buildAuthHeaders({
    ...defaultAuthState,
    ...stored,
    ...live,
    jwt,
    serviceAccountToken,
    apiKey,
  });
}
