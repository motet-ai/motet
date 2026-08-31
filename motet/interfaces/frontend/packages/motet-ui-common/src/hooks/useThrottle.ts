/**
 * Motet - Motet UI Common - Throttle Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Generic throttling hook that limits how frequently a value can update.
 *     Unlike debouncing, throttling ensures regular updates during continuous
 *     changes, making it ideal for streaming scenarios. An optional reset key
 *     snaps to the latest value immediately (conversation store swap).
 *
 * Dependencies:
 *     - React: useState, useRef, useEffect
 *
 * Usage:
 *     import { useThrottle } from "@motet/ui-common/hooks";
 *     const throttled = useThrottle(messages, 50, storeReadyKey);
 */
import { useState, useRef, useEffect } from "react";

/**
 * Throttles value updates to a minimum interval.
 *
 * @param value - The value to throttle
 * @param interval - Minimum milliseconds between updates
 * @param resetKey - When this changes, publish ``value`` immediately
 * @returns The throttled value
 */
export function useThrottle<T>(value: T, interval: number, resetKey?: string | null): T {
  const [throttledValue, setThrottledValue] = useState<T>(value);
  const lastUpdated = useRef<number>(0);
  const trailingTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const latestValue = useRef<T>(value);
  const lastResetKey = useRef(resetKey);

  useEffect(() => {
    latestValue.current = value;
  });

  useEffect(() => {
    if (resetKey === lastResetKey.current) return;
    lastResetKey.current = resetKey;
    if (trailingTimeout.current) {
      clearTimeout(trailingTimeout.current);
      trailingTimeout.current = null;
    }
    setThrottledValue(value);
    lastUpdated.current = Date.now();
  }, [resetKey, value]);

  useEffect(() => {
    const now = Date.now();
    const timeSinceLastUpdate = now - lastUpdated.current;

    if (timeSinceLastUpdate >= interval) {
      setThrottledValue(value);
      lastUpdated.current = now;
      
      if (trailingTimeout.current) {
        clearTimeout(trailingTimeout.current);
        trailingTimeout.current = null;
      }
    } else {
      if (!trailingTimeout.current) {
        const remaining = interval - timeSinceLastUpdate;
        trailingTimeout.current = setTimeout(() => {
          setThrottledValue(latestValue.current);
          lastUpdated.current = Date.now();
          trailingTimeout.current = null;
        }, remaining);
      }
    }
  }, [value, interval]);
  
  useEffect(() => {
    return () => {
      if (trailingTimeout.current) {
        clearTimeout(trailingTimeout.current);
      }
    };
  }, []);

  return throttledValue;
}
