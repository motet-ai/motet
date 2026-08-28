/**
 * Motet UI Common - Request Context Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Manages model overrides (provider, name, thinking mode) with automatic
 *     localStorage persistence.
 *
 *     This hook encapsulates the request-level configuration that any Motet
 *     chat UI needs: model selection, thinking mode, and reasoning effort.
 *     The state is persisted to localStorage so selections survive page refreshes.
 *
 * Dependencies:
 *     - React: useState, useEffect, useCallback
 *
 * Usage:
 *     import { useRequestContext } from "@motet/ui-common";
 *     const { overrides, setOverrides } = useRequestContext();
 */
import { useState, useEffect, useCallback } from "react";
import type { Overrides, ReasoningEffort } from "../types/overrides";

// ─────────────────────────────────────────────────────────────────────────────
// TYPES
// ─────────────────────────────────────────────────────────────────────────────

export type UseRequestContextOptions = {
  /** localStorage key for overrides (defaults to "motet_overrides"). */
  storageKey?: string;
  /** Custom default overrides (merged with built-in defaults). */
  defaults?: Partial<Overrides>;
};

// ─────────────────────────────────────────────────────────────────────────────
// DEFAULTS
// ─────────────────────────────────────────────────────────────────────────────

const DEFAULT_OVERRIDES: Overrides = {
  model_provider: "",
  model_name: "",
  model_profile_name: "default",
  enable_thinking: false,
  reasoning_effort: "medium",
};

// ─────────────────────────────────────────────────────────────────────────────
// HOOK
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Manages model/behavior overrides with localStorage persistence.
 */
export function useRequestContext(options?: UseRequestContextOptions) {
  const storageKey = options?.storageKey || "motet_overrides";
  const baseDefaults: Overrides = { ...DEFAULT_OVERRIDES, ...options?.defaults };

  const [overrides, setOverridesRaw] = useState<Overrides>(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (raw) return { ...baseDefaults, ...(JSON.parse(raw) as Overrides) };
    } catch { /* ignore */ }
    return baseDefaults;
  });

  // Persist overrides
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(overrides));
    } catch { /* ignore */ }
  }, [overrides, storageKey]);

  // ─────────────────────────────────────────────────────────────────────────────
  // TYPED SETTERS
  // ─────────────────────────────────────────────────────────────────────────────

  const setOverrides = useCallback((
    updater: Overrides | ((prev: Overrides) => Overrides),
  ) => {
    setOverridesRaw(updater);
  }, []);

  const setModelProvider = useCallback((provider: string) => {
    setOverridesRaw((prev) => ({ ...prev, model_provider: provider }));
  }, []);

  const setModelName = useCallback((name: string) => {
    setOverridesRaw((prev) => ({ ...prev, model_name: name }));
  }, []);

  const setModelProfileName = useCallback((name: string) => {
    setOverridesRaw((prev) => ({ ...prev, model_profile_name: name }));
  }, []);

  const setEnableThinking = useCallback((enabled: boolean) => {
    setOverridesRaw((prev) => ({ ...prev, enable_thinking: enabled }));
  }, []);

  const setReasoningEffort = useCallback((effort: ReasoningEffort) => {
    setOverridesRaw((prev) => ({ ...prev, reasoning_effort: effort }));
  }, []);

  return {
    overrides,
    setOverrides,

    // Typed convenience setters
    setModelProvider,
    setModelName,
    setModelProfileName,
    setEnableThinking,
    setReasoningEffort,
  };
}
