/**
 * Motet - Motet UI Common - Conversation Auth Identity
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Stable principal identity and conversation-list sync decisions for the
 *     conversation manager. Identity is the authenticated principal (JWT `sub`,
 *     API key, or service-account token), not the raw JWT string, so token
 *     refresh does not look like a user change. Sync planning decides whether
 *     to reload, replace, or reset the sidebar without an empty intermediate list
 *     on first mount.
 *
 * Dependencies:
 *     - AuthState: credential fields used to derive identity
 *
 * Usage:
 *     const identity = authIdentityKey(auth);
 *     const plan = planConversationListSync({
 *       identity,
 *       prevIdentity,
 *       scope: scopeKey(scope),
 *       prevScope,
 *     });
 *
 * Notes:
 *     - JWT identity uses the `sub` claim so rotation of the same principal is a
 *       no-op for the sidebar.
 *     - First authenticated mount keeps the already-hydrated cache visible and
 *       replaces from the API when the response arrives.
 *     - Logout, a different principal, or a method switch still resets locally
 *       so one principal does not inherit another's list.
 */

import type { AuthState } from "../types";

/** True when any supported credential is present. */
export function isAuthenticated(auth: AuthState | null | undefined): boolean {
  if (!auth) return false;
  return !!(
    (auth.jwt && auth.jwt.trim().length > 20) ||
    (auth.serviceAccountToken && auth.serviceAccountToken.trim().length > 0) ||
    (auth.apiKey && auth.apiKey.trim().length > 0)
  );
}

/**
 * Decode a JWT payload and return `sub`, or null if the token is not a
 * well-formed JWT with a string subject.
 */
export function jwtSubject(jwt: string): string | null {
  try {
    const parts = jwt.split(".");
    if (parts.length !== 3) return null;
    const payloadPart = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const padded = payloadPart + "=".repeat((4 - (payloadPart.length % 4)) % 4);
    const payload = JSON.parse(atob(padded));
    const sub = typeof payload?.sub === "string" ? payload.sub.trim() : "";
    return sub || null;
  } catch {
    return null;
  }
}

/**
 * Stable identity string for detecting principal/credential changes.
 * JWT refresh for the same `sub` yields the same key.
 */
export function authIdentityKey(auth?: AuthState | null): string {
  if (!auth || !isAuthenticated(auth)) return "";
  if (auth.jwt && auth.jwt.trim().length > 20) {
    const sub = jwtSubject(auth.jwt);
    return sub ? `jwt:${sub}` : "jwt";
  }
  if (auth.serviceAccountToken && auth.serviceAccountToken.trim()) {
    return `sa:${auth.serviceAccountToken.trim()}`;
  }
  if (auth.apiKey && auth.apiKey.trim()) return `key:${auth.apiKey.trim()}`;
  return "";
}

/** What the conversation-list effect should do on this auth/scope tick. */
export type ConversationListSyncPlan = {
  /** Fetch the server conversation list. */
  load: boolean;
  /** Replace the local list with the server result (vs merge). */
  replace: boolean;
  /** Immediately drop the visible list to a single empty conversation. */
  resetLocal: boolean;
  /** Restore the per-scope localStorage snapshot before the fetch. */
  restoreCached: boolean;
};

/**
 * Decide how to sync the sidebar after an auth or scope tick.
 *
 * - Logout (`identity` empty): reset locally, do not fetch.
 * - Same principal and scope (including JWT rotation): no-op.
 * - First authenticated mount (`prevIdentity` is null): load+replace without
 *   wiping the already-hydrated cache.
 * - Login after logout, or a different principal: reset then replace from API.
 * - Scope change: restore that scope's cache, then replace from API.
 */
export function planConversationListSync(args: {
  identity: string;
  prevIdentity: string | null;
  scope: string;
  prevScope: string | null;
}): ConversationListSyncPlan {
  const { identity, prevIdentity, scope, prevScope } = args;

  if (!identity) {
    return { load: false, replace: false, resetLocal: true, restoreCached: false };
  }

  const isFirstAuthSync = prevIdentity === null;
  const loggedIn = prevIdentity === "";
  const authChanged =
    prevIdentity !== null && prevIdentity !== "" && prevIdentity !== identity;
  const isScopeChange = prevScope !== null && prevScope !== scope;

  if (!isFirstAuthSync && !loggedIn && !authChanged && !isScopeChange) {
    return { load: false, replace: false, resetLocal: false, restoreCached: false };
  }

  if (authChanged || loggedIn) {
    return { load: true, replace: true, resetLocal: true, restoreCached: false };
  }

  if (isScopeChange) {
    return { load: true, replace: true, resetLocal: false, restoreCached: true };
  }

  return { load: true, replace: true, resetLocal: false, restoreCached: false };
}
