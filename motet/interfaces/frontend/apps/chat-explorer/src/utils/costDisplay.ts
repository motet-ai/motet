/**
 * Motet - Chat Explorer - Cost Display Prefs
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-29
 *
 * Description:
 *     Which priced USD lines Chat Explorer shows, and how those prefs
 *     are read from localStorage.
 *
 * Dependencies:
 *     - ./storageMigration: chat_explorer_* key prefix
 *
 * Usage:
 *     const prefs = readCostDisplayPrefs();
 *     localStorage.setItem(storageKey("cost_display"), JSON.stringify(prefs));
 *
 * Notes:
 *     - Missing or invalid keys fall back to DEFAULT_COST_DISPLAY (all on).
 */

import { storageKey } from "./storageMigration";

export const COST_DISPLAY_KEYS = ["agent", "turn", "conversation"] as const;

export type CostDisplayKey = (typeof COST_DISPLAY_KEYS)[number];

export type CostDisplayPrefs = Record<CostDisplayKey, boolean>;

export const DEFAULT_COST_DISPLAY: CostDisplayPrefs = {
  agent: true,
  turn: true,
  conversation: true,
};

export const COST_DISPLAY_TOGGLES: { key: CostDisplayKey; label: string }[] = [
  { key: "agent", label: "Show agent cost" },
  { key: "turn", label: "Show turn cost" },
  { key: "conversation", label: "Show conversation cost" },
];

function preferBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function readCostDisplayPrefs(): CostDisplayPrefs {
  try {
    const raw = localStorage.getItem(storageKey("cost_display"));
    const parsed = raw ? (JSON.parse(raw) as Partial<CostDisplayPrefs>) : {};
    return Object.fromEntries(
      COST_DISPLAY_KEYS.map((key) => [key, preferBoolean(parsed[key], DEFAULT_COST_DISPLAY[key])]),
    ) as CostDisplayPrefs;
  } catch {
    return { ...DEFAULT_COST_DISPLAY };
  }
}
