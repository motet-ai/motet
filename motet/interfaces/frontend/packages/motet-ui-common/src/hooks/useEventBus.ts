/**
 * Motet - Motet UI Common - Event Bus Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-01-28
 *
 * Description:
 *     SSE (Server-Sent Events) subscriber for real-time system observability.
 *     Connects to an events endpoint and maintains a rolling buffer of events.
 *
 * Dependencies:
 *     - React: useState, useRef, useEffect, useMemo
 *
 * Usage:
 *     import { useEventBus } from "@motet/ui-common/hooks";
 *     const { eventBus, errors } = useEventBus(auth, enabled);
 */
import { useState, useRef, useEffect, useMemo } from "react";
import { AuthState } from "../types";
import { parseSseBuffer } from "../utils";
import { buildHeaders } from "./useAuth";

/** Configuration options for useEventBus hook */
export interface UseEventBusOptions {
  /** SSE endpoint URL */
  endpoint?: string;
  /** Maximum number of events to keep in buffer */
  bufferSize?: number;
  /** Reconnection delay in milliseconds */
  reconnectDelay?: number;
}

const defaultOptions: UseEventBusOptions = {
  endpoint: "/api/v1/events",
  bufferSize: 100,
  reconnectDelay: 5000
};

/**
 * React hook for subscribing to the server event bus via SSE.
 *
 * @param auth - Current auth state for constructing authenticated headers
 * @param enabled - Whether event watching is enabled (user toggle)
 * @param options - Configuration options
 */
export function useEventBus(
  auth: AuthState,
  enabled: boolean,
  options: UseEventBusOptions = {}
) {
  const opts = { ...defaultOptions, ...options };

  // Rolling buffer of received events (newest first)
  const [eventBus, setEventBus] = useState<any[]>([]);

  // Connection/parsing errors for display in UI
  const [errors, setErrors] = useState<string[]>([]);

  // AbortController for canceling the SSE fetch on cleanup
  const eventsAbortRef = useRef<AbortController | null>(null);

  // Memoize auth key and headers to prevent unnecessary reconnections
  const authKey = useMemo(
    () => JSON.stringify(auth),
    [auth.jwt, auth.serviceAccountToken, auth.apiKey, auth.principal, auth.tenant, auth.roles]
  );
  const eventHeaders = useMemo(() => buildHeaders(auth), [authKey]);

  // SSE connection management
  useEffect(() => {
    if (!enabled) {
      if (eventsAbortRef.current) {
        eventsAbortRef.current.abort();
        eventsAbortRef.current = null;
      }
      return;
    }
    
    if (!auth.jwt && !auth.serviceAccountToken && !auth.apiKey) {
      return;
    }
    
    let isMounted = true;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
    
    const connect = async () => {
      if (!isMounted || !enabled) return;
      if (!auth.jwt && !auth.serviceAccountToken && !auth.apiKey) return;
      
      const ctrl = new AbortController();
      eventsAbortRef.current = ctrl;
      
      try {
        const resp = await fetch(opts.endpoint!, {
          headers: { ...eventHeaders },
          signal: ctrl.signal
        });
        
        if (!resp.ok) {
          if (resp.status === 401) return;
          console.warn("Event bus connection failed", resp.status);
          if (isMounted && enabled) {
            reconnectTimeout = setTimeout(connect, opts.reconnectDelay);
          }
          return;
        }
        
        if (!resp.body) return;
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        
        while (isMounted && enabled) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";
          for (const part of parts) {
            const evs = parseSseBuffer(part);
            evs.forEach((ev) => {
              if (ev.event === "event_bus") {
                setEventBus((prev) => [ev.data, ...prev].slice(0, opts.bufferSize));
              }
            });
          }
        }
        
        if (isMounted && enabled && !ctrl.signal.aborted) {
          reconnectTimeout = setTimeout(connect, opts.reconnectDelay);
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") return;
        console.error("Event bus error:", err);
        setErrors((prev) => [`events: ${String(err)}`, ...prev].slice(0, 10));
        
        if (isMounted && enabled) {
          reconnectTimeout = setTimeout(connect, opts.reconnectDelay);
        }
      }
    };
    
    connect();
    
    return () => {
      isMounted = false;
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (eventsAbortRef.current) {
        eventsAbortRef.current.abort();
        eventsAbortRef.current = null;
      }
    };
  }, [authKey, enabled, eventHeaders, opts.endpoint, opts.reconnectDelay, opts.bufferSize, auth.jwt, auth.serviceAccountToken, auth.apiKey]);

  // Clear events function
  const clearEvents = () => setEventBus([]);
  const clearErrors = () => setErrors([]);

  return { eventBus, errors, clearEvents, clearErrors };
}
