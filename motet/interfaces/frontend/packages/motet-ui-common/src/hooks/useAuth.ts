/**
 * Motet - Motet UI Common - Auth Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Central authentication management hook for Motet UI applications.
 *     Handles multiple authentication methods and provides header construction.
 *
 *     Authentication Methods (in priority order):
 *     1. JWT (Bearer token) - From SSO/Keycloak login
 *     2. Service Account Token - For machine-to-machine auth
 *     3. API Key (X-API-Key header) - Simple key-based auth
 *     4. Dev Headers (X-Principal-Id, X-Tenant-Id) - Development mode fallback
 *
 *     Identity display-name fetch keys on the principal (JWT sub / API key),
 *     so token refresh does not refetch or re-render the shell.
 *
 * Dependencies:
 *     - React: useState, useEffect, useRef, useCallback, useMemo
 *     - conversationAuth: authIdentityKey for stable principal identity
 *
 * Usage:
 *     import { useAuth, buildHeaders } from "@motet/ui-common/hooks";
 *     const { auth, setAuth, handleSsoLogin, userDisplayName } = useAuth();
 */
import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { AuthState, defaultAuthState } from "../types";
import { debugLog } from "../utils";
import { authIdentityKey } from "./conversationAuth";

/**
 * Constructs HTTP headers for API requests based on current auth state.
 * Used for streaming SSE endpoints (includes Accept: text/event-stream).
 *
 * Priority: JWT > Service Account Token > API Key > Dev Headers
 */
export function buildHeaders(auth: AuthState): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream"
  };
  if (auth.apiKey) headers["X-API-Key"] = auth.apiKey;
  if (auth.jwt) {
    headers["Authorization"] = `Bearer ${auth.jwt}`;
  } else if (auth.serviceAccountToken) {
    headers["Authorization"] = `Bearer ${auth.serviceAccountToken}`;
  }
  // Always include dev identity headers. In production the backend ignores
  // them (allow_insecure_principal_headers=false). In Docker dev mode they
  // provide principal identity as a fallback when JWT is missing or stale.
  headers["X-Principal-Id"] = auth.principal || "demo-user";
  headers["X-Tenant-Id"] = auth.tenant || "default";
  if (auth.roles) headers["X-Roles"] = auth.roles;
  return headers;
}

/**
 * Constructs HTTP headers for non-streaming API requests.
 * Omits Accept and Content-Type headers to allow browser to set them.
 */
export function buildAuthHeaders(auth: AuthState): Record<string, string> {
  const headers = buildHeaders(auth);
  delete headers["Accept"];
  delete headers["Content-Type"];
  return headers;
}

/** Configuration options for useAuth hook */
export interface UseAuthOptions {
  /** localStorage key for persisting auth state */
  storageKey?: string;
  /** Redirect URI for SSO login */
  ssoRedirectUri?: string;
  /** Identity endpoint for fetching user info */
  identityEndpoint?: string;
  /** Token refresh endpoint */
  refreshEndpoint?: string;
}

const defaultOptions: UseAuthOptions = {
  storageKey: "motet_auth",
  ssoRedirectUri: "/",
  identityEndpoint: "/api/v1/identity/me",
  refreshEndpoint: "/api/v1/auth/refresh"
};

/**
 * React hook for managing authentication state and flows.
 * Provides auth credentials, header builders, and OAuth popup handlers.
 */
export function useAuth(options: UseAuthOptions = {}) {
  const opts = { ...defaultOptions, ...options };
  const apiKeyStorageKey = `${opts.storageKey!}_api_key`;

  // Main auth state - restored from localStorage on mount
  const [auth, setAuth] = useState<AuthState>(() => {
    const saved = localStorage.getItem(opts.storageKey!);
    const base = saved ? { ...defaultAuthState, ...JSON.parse(saved) } : { ...defaultAuthState };
    // Fallback for API-key workflows: keep key in a dedicated slot to avoid losing it on refresh races.
    if (!base.apiKey) {
      const fallbackApiKey = localStorage.getItem(apiKeyStorageKey) || "";
      if (fallbackApiKey.trim()) {
        base.apiKey = fallbackApiKey;
      }
    }
    return base;
  });

  // User display name fetched from identity endpoint
  const [userDisplayName, setUserDisplayName] = useState<string | null>(null);

  // Tracks messages that triggered OAuth and were successfully authorized
  const [authorizedMessages, setAuthorizedMessages] = useState<Map<string, { serviceId: string; displayName: string }>>(new Map());

  // Controls visibility of the auth settings modal
  const [showAuthModal, setShowAuthModal] = useState<boolean>(false);
  
  // Refs for mutable state
  const pendingAuthMessagesRef = useRef<Map<string, string>>(new Map());
  const refreshLockRef = useRef<boolean>(false);
  const lastRefreshTimeRef = useRef<number>(0);
  const REFRESH_COOLDOWN_MS = 60 * 1000;

  // Principal identity (JWT sub / API key), not the raw token string.
  const identityKey = useMemo(
    () => authIdentityKey(auth),
    [auth.jwt, auth.serviceAccountToken, auth.apiKey]
  );
  const authRef = useRef(auth);
  authRef.current = auth;

  // Persist auth state immediately to avoid refresh races.
  const prevAuthRef = useRef<string>("");
  useEffect(() => {
    const authStr = JSON.stringify(auth);
    if (authStr === prevAuthRef.current) return;
    prevAuthRef.current = authStr;

    try {
      localStorage.setItem(opts.storageKey!, authStr);
      if (auth.apiKey && auth.apiKey.trim()) {
        localStorage.setItem(apiKeyStorageKey, auth.apiKey);
      } else {
        localStorage.removeItem(apiKeyStorageKey);
      }
    } catch (err) {
      console.error("Failed to persist auth:", err);
    }
  }, [auth, opts.storageKey, apiKeyStorageKey]);

  // Check for tokens from SSO on mount
  useEffect(() => {
    const accessToken = localStorage.getItem("motet_access_token");
    const legacyJwt = localStorage.getItem("motet_jwt_token");
    const token = (accessToken && accessToken.length > 20) ? accessToken : legacyJwt;
    if (token && token.length > 20) {
      setAuth((prev) => {
        if (prev.jwt === token) return prev;
        return { ...prev, jwt: token };
      });
    }
  }, []);

  // Listen for OAuth success messages (SSO)
  useEffect(() => {
    const messageHandler = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data && event.data.type === "oauth_success") {
        const accessToken = event.data.access_token || event.data.token;
        const idToken = event.data.id_token;
        if (accessToken) {
          setAuth((prev) => ({ ...prev, jwt: accessToken }));
          localStorage.setItem("motet_jwt_token", accessToken);
          localStorage.setItem("motet_access_token", accessToken);
          lastRefreshTimeRef.current = Date.now();
        }
        if (idToken) {
          localStorage.setItem("motet_id_token", idToken);
        }
        try {
          const decode = (token: string) => {
            const parts = token.split(".");
            if (parts.length !== 3) return null;
            return JSON.parse(atob(parts[1]));
          };
          const accessClaims = accessToken ? decode(accessToken) : null;
          const idClaims = idToken ? decode(idToken) : null;
          console.debug("[auth] access token claims", accessClaims);
          console.debug("[auth] id token claims", idClaims);
        } catch (err) {
          console.warn("[auth] failed to decode token claims", err);
        }
        debugLog("✅ OAuth authentication successful");
      }
    };
    window.addEventListener("message", messageHandler);
    return () => window.removeEventListener("message", messageHandler);
  }, []);

  // Listen for OAuth completion messages from popup (Service Authorization)
  useEffect(() => {
    const messageHandler = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      
      if (event.data && event.data.type === "oauth_complete") {
        const { provider, display_name, success, error } = event.data;
        debugLog("OAuth complete:", event.data);
        
        if (success) {
          const serviceId = provider || "unknown";
          const messageId = pendingAuthMessagesRef.current.get(serviceId);
          
          if (messageId) {
            setAuthorizedMessages((prev) => {
              const newMap = new Map(prev);
              newMap.set(messageId, { serviceId, displayName: display_name || serviceId });
              return newMap;
            });
            pendingAuthMessagesRef.current.delete(serviceId);
          }
          debugLog(`✅ ${display_name || provider} authorized successfully!`);
        } else {
          console.error(`❌ ${display_name || provider} authorization failed: ${error || "Unknown error"}`);
          const serviceId = provider || "unknown";
          pendingAuthMessagesRef.current.delete(serviceId);
        }
      }
    };
    
    window.addEventListener("message", messageHandler);
    return () => window.removeEventListener("message", messageHandler);
  }, []);

  // Helper: decode JWT expiration
  const getTokenExpiration = useCallback((jwt: string): number | null => {
    try {
      const parts = jwt.split('.');
      if (parts.length !== 3) return null;
      const payload = JSON.parse(atob(parts[1]));
      return payload.exp ? payload.exp * 1000 : null;
    } catch {
      return null;
    }
  }, []);

  // Helper: check if token is expiring soon
  const isTokenExpiringSoon = useCallback((jwt: string): boolean => {
    const exp = getTokenExpiration(jwt);
    if (!exp) return false;
    return (exp - Date.now()) < 5 * 60 * 1000;
  }, [getTokenExpiration]);

  // Refresh JWT token
  const refreshJwtToken = useCallback(async (): Promise<boolean> => {
    if (!auth.jwt) return false;
    if (refreshLockRef.current) {
      debugLog("Token refresh already in progress, skipping...");
      return false;
    }

    const timeSinceLastRefresh = Date.now() - lastRefreshTimeRef.current;
    if (timeSinceLastRefresh < REFRESH_COOLDOWN_MS) {
      debugLog(`Token refresh on cooldown (${Math.round((REFRESH_COOLDOWN_MS - timeSinceLastRefresh) / 1000)}s remaining)`);
      return false;
    }

    refreshLockRef.current = true;

    try {
      const headers = buildHeaders(auth);
      delete headers["Accept"];
      headers["Content-Type"] = "application/json";

      const response = await fetch(opts.refreshEndpoint!, {
        method: "POST",
        headers,
      });

      if (response.ok) {
        const data = await response.json();
        const accessToken = data.access_token || data.token;
        const idToken = data.id_token;
        const newToken = accessToken || idToken;

        if (newToken) {
          setAuth((prev) => ({ ...prev, jwt: newToken }));
          localStorage.setItem("motet_jwt_token", newToken);
          if (accessToken) {
            localStorage.setItem("motet_access_token", accessToken);
          }
          if (idToken) {
            localStorage.setItem("motet_id_token", idToken);
          }
          lastRefreshTimeRef.current = Date.now();
          debugLog("✅ Token refreshed successfully");
          refreshLockRef.current = false;
          return true;
        }
      } else if (response.status === 401) {
        console.warn("Token refresh failed - refresh token expired");
        setAuth((prev) => ({ ...prev, jwt: "" }));
        localStorage.removeItem("motet_jwt_token");
        localStorage.removeItem("motet_access_token");
        localStorage.removeItem("motet_id_token");
        setUserDisplayName(null);
        refreshLockRef.current = false;
        return false;
      }
    } catch (err) {
      console.error("Token refresh error:", err);
    }
    refreshLockRef.current = false;
    return false;
  }, [auth, opts.refreshEndpoint, REFRESH_COOLDOWN_MS]);

  const refreshJwtTokenRef = useRef(refreshJwtToken);
  refreshJwtTokenRef.current = refreshJwtToken;

  // Fetch user identity information. JWT rotation for the same principal
  // does not retrigger this (identityKey uses JWT sub, not the raw token).
  useEffect(() => {
    const fetchUserInfo = async () => {
      const currentAuth = authRef.current;
      if (!currentAuth.jwt && !currentAuth.serviceAccountToken && !currentAuth.apiKey) {
        setUserDisplayName(null);
        return;
      }

      try {
        const headers = buildHeaders(currentAuth);
        delete headers["Accept"];
        headers["Content-Type"] = "application/json";
        
        const response = await fetch(opts.identityEndpoint!, { headers });
        if (response.ok) {
          const data = await response.json();
          const nextName = data.display_name || null;
          setUserDisplayName((prev) => (prev === nextName ? prev : nextName));
        } else if (response.status === 401) {
          const timeSinceLastRefresh = Date.now() - lastRefreshTimeRef.current;
          if (timeSinceLastRefresh >= REFRESH_COOLDOWN_MS && !refreshLockRef.current) {
            const refreshed = await refreshJwtTokenRef.current();
            if (refreshed) {
              const newHeaders = buildHeaders({
                ...authRef.current,
                jwt: localStorage.getItem("motet_jwt_token") || "",
              });
              delete newHeaders["Accept"];
              newHeaders["Content-Type"] = "application/json";
              const retryResponse = await fetch(opts.identityEndpoint!, { headers: newHeaders });
              if (retryResponse.ok) {
                const retryData = await retryResponse.json();
                const nextName = retryData.display_name || null;
                setUserDisplayName((prev) => (prev === nextName ? prev : nextName));
              }
            }
          }
        } else {
          setUserDisplayName(null);
        }
      } catch (err) {
        console.error("Failed to fetch user info:", err);
        setUserDisplayName(null);
      }
    };

    fetchUserInfo();
  }, [identityKey, opts.identityEndpoint, REFRESH_COOLDOWN_MS]);

  // Proactive token refresh
  useEffect(() => {
    if (!auth.jwt) return;

    const checkAndRefresh = async () => {
      if (refreshLockRef.current) return;
      
      const timeSinceLastRefresh = Date.now() - lastRefreshTimeRef.current;
      if (timeSinceLastRefresh < REFRESH_COOLDOWN_MS) return;

      if (isTokenExpiringSoon(auth.jwt)) {
        debugLog("Token expiring soon, refreshing...");
        await refreshJwtToken();
      }
    };

    const interval = setInterval(checkAndRefresh, 2 * 60 * 1000);
    return () => clearInterval(interval);
  }, [auth.jwt, isTokenExpiringSoon, refreshJwtToken, REFRESH_COOLDOWN_MS]);

  // Handle SSO login
  const handleSsoLogin = useCallback(() => {
    const width = 600;
    const height = 700;
    const left = (screen.width - width) / 2;
    const top = (screen.height - height) / 2;
    const popup = window.open(
      `/api/v1/auth/login?redirect_uri=${encodeURIComponent(opts.ssoRedirectUri!)}`,
      "oauth_login",
      `width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`
    );
    if (!popup || popup.closed || typeof popup.closed === "undefined") {
      alert("Popup blocked. Please allow popups for this site and try again.");
    }
  }, [opts.ssoRedirectUri]);

  // Handle OAuth authorization for services
  const openOAuthPopup = useCallback((authEndpoint: string, serviceId: string, messageId: string, conversationId?: string) => {
    debugLog("Opening OAuth popup for:", serviceId, authEndpoint);
    pendingAuthMessagesRef.current.set(serviceId, messageId);
    
    let popupUrl = authEndpoint;
      const storedJwt = localStorage.getItem("motet_access_token") || localStorage.getItem("motet_jwt_token");
    const authParams = new URLSearchParams();
    
    if (storedJwt && storedJwt.length > 20) {
      authParams.set("token", storedJwt);
    } else if (auth.jwt) {
      authParams.set("token", auth.jwt);
    } else if (auth.serviceAccountToken) {
      authParams.set("token", auth.serviceAccountToken);
    } else if (auth.apiKey) {
      authParams.set("api_key", auth.apiKey);
    } else {
      authParams.set("principal_id", auth.principal || "demo-user");
      authParams.set("tenant_id", auth.tenant || "demo-org");
    }
    
    if (conversationId) {
      authParams.set("conversation_id", conversationId);
    }
    
    const separator = authEndpoint.includes("?") ? "&" : "?";
    popupUrl = authEndpoint + separator + authParams.toString();
    
    const width = 600;
    const height = 700;
    const left = (screen.width - width) / 2;
    const top = (screen.height - height) / 2;
    
    const oauthPopup = window.open(
      popupUrl,
      `oauth_${serviceId}`,
      `width=${width},height=${height},left=${left},top=${top},scrollbars=yes,resizable=yes`
    );
    
    if (!oauthPopup) {
      alert("Popup was blocked. Please allow popups for this site to authorize services.");
      return;
    }
    
    oauthPopup.focus();
  }, [auth]);

  // Check if user is authenticated
  const isAuthenticated = useMemo(() => {
    return !!(auth.jwt || auth.serviceAccountToken || auth.apiKey);
  }, [auth.jwt, auth.serviceAccountToken, auth.apiKey]);

  // Logout function
  const logout = useCallback(() => {
    setAuth(defaultAuthState);
    setUserDisplayName(null);
    localStorage.removeItem("motet_jwt_token");
    localStorage.removeItem("motet_access_token");
    localStorage.removeItem("motet_id_token");
    localStorage.removeItem(opts.storageKey!);
  }, [opts.storageKey]);

  return {
    auth,
    setAuth,
    userDisplayName,
    setUserDisplayName,
    authorizedMessages,
    setAuthorizedMessages,
    showAuthModal,
    setShowAuthModal,
    handleSsoLogin,
    openOAuthPopup,
    isAuthenticated,
    logout,
    buildHeaders: () => buildHeaders(auth),
    buildAuthHeaders: () => buildAuthHeaders(auth)
  };
}
