/**
 * Motet - Motet UI Common - Signed-Out Session
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Session flag and remote logout helpers for the signed-out landing page.
 *     The flag survives an identity-provider redirect so returning users still
 *     see "You've been signed out" instead of a first-visit welcome.
 *
 * Dependencies:
 *     - sessionStorage for the in-tab signed-out flag
 *     - fetch for Motet and identity-provider logout
 *
 * Usage:
 *     markSignedOut();
 *     logout();
 *     void finishRemoteLogout({ headers, idToken, redirectUri: appLogoutRedirectUri() });
 *
 * Notes:
 *     Call markSignedOut and clear local credentials before finishRemoteLogout.
 *     Do not await remote logout before clearing or the UI looks frozen.
 */

export const SIGNED_OUT_STORAGE_KEY = "motet_signed_out";

/** Record a voluntary logout in this tab. */
export function markSignedOut(): void {
  try {
    sessionStorage.setItem(SIGNED_OUT_STORAGE_KEY, "1");
  } catch {
    // Private mode or disabled storage — copy falls back to welcome.
  }
}

/** Clear the signed-out flag after a successful login. */
export function clearSignedOutFlag(): void {
  try {
    sessionStorage.removeItem(SIGNED_OUT_STORAGE_KEY);
  } catch {
    // ignore
  }
}

/** True when this tab signed out (including after an IdP redirect back). */
export function wasSignedOut(): boolean {
  try {
    return sessionStorage.getItem(SIGNED_OUT_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

/** Current app URL without a trailing slash, for post_logout_redirect_uri. */
export function appLogoutRedirectUri(): string {
  return (
    `${window.location.origin}${window.location.pathname}`.replace(/\/$/, "") ||
    window.location.origin
  );
}

/**
 * Revoke the Motet refresh token and redirect to the identity provider
 * when a logout URL is available. The local session must already be cleared.
 */
export async function finishRemoteLogout(options: {
  headers?: Record<string, string>;
  idToken?: string | null;
  redirectUri: string;
  timeoutMs?: number;
}): Promise<void> {
  const timeoutMs = options.timeoutMs ?? 4000;
  const headers = options.headers || {};
  try {
    if (headers.Authorization) {
      await fetch("/api/v1/auth/logout", {
        method: "GET",
        headers,
        signal: AbortSignal.timeout(timeoutMs),
      });
    }
  } catch {
    // Local session is already cleared.
  }
  try {
    const logoutParams = new URLSearchParams({
      post_logout_redirect_uri: options.redirectUri,
    });
    if (options.idToken) {
      logoutParams.set("id_token_hint", options.idToken);
    }
    const res = await fetch(
      `/api/v1/auth/identity-provider-logout?${logoutParams.toString()}`,
      { signal: AbortSignal.timeout(timeoutMs) },
    );
    if (!res.ok) {
      return;
    }
    const data = (await res.json()) as { url?: string | null };
    if (data.url) {
      window.location.href = data.url;
    }
  } catch {
    // Stay on the signed-out page if IdP logout is unavailable.
  }
}
