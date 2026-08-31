/**
 * Motet - Chat Explorer - localStorage Migration
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-29
 *
 * Description:
 *     One-time migration of Demo Chat X localStorage keys (demo_chat_x_*) to
 *     Chat Explorer keys (chat_explorer_*). Same-origin path rename does not
 *     clear storage; this renames the app key prefix while preserving values.
 *
 * Dependencies:
 *     - Browser localStorage
 *
 * Usage:
 *     import { migrateDemoChatXStorage, STORAGE_PREFIX } from "./storageMigration";
 *     migrateDemoChatXStorage(); // call once at app boot
 *
 * Notes:
 *     - Does not delete old keys (safe for multi-tab during deploy).
 *     - Copies only when the new key is empty/missing.
 *     - Scoped conversation keys use prefix matching.
 */

/** New Chat Explorer localStorage key prefix. */
export const STORAGE_PREFIX = "chat_explorer";

/** Legacy Demo Chat X localStorage key prefix. */
export const LEGACY_STORAGE_PREFIX = "demo_chat_x";

const SIMPLE_SUFFIXES = [
  "auth",
  "last_api_key",
  "agent_id",
  "surface_id",
  "overrides",
  "dark_mode",
  "debug_stream",
  "cost_display",
] as const;

let migrated = false;

/**
 * Copy a single key from legacy → new when the new key is empty.
 */
function copyIfMissing(legacyKey: string, newKey: string): void {
  try {
    const existing = localStorage.getItem(newKey);
    if (existing != null && String(existing).length > 0) return;
    const legacy = localStorage.getItem(legacyKey);
    if (legacy == null || String(legacy).length === 0) return;
    localStorage.setItem(newKey, legacy);
  } catch {
    // Ignore localStorage access errors (private mode, quota, etc.).
  }
}

/**
 * Migrate all known Demo Chat X keys to Chat Explorer prefixes.
 * Safe to call multiple times; runs at most once per page load.
 */
export function migrateDemoChatXStorage(): void {
  if (migrated) return;
  migrated = true;

  try {
    for (const suffix of SIMPLE_SUFFIXES) {
      copyIfMissing(`${LEGACY_STORAGE_PREFIX}_${suffix}`, `${STORAGE_PREFIX}_${suffix}`);
    }

    // Scoped conversation keys and any other demo_chat_x_* leftovers.
    const legacyRoot = `${LEGACY_STORAGE_PREFIX}_`;
    const keys: string[] = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.startsWith(legacyRoot)) keys.push(key);
    }
    for (const legacyKey of keys) {
      const newKey = `${STORAGE_PREFIX}_${legacyKey.slice(legacyRoot.length)}`;
      copyIfMissing(legacyKey, newKey);
    }
  } catch {
    // Ignore enumeration / access errors.
  }
}

/** Build a Chat Explorer storage key from a suffix (e.g. "auth"). */
export function storageKey(suffix: string): string {
  return `${STORAGE_PREFIX}_${suffix}`;
}
