/**
 * Motet UI Common - Namespacing Utilities
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-04-01
 *
 * Description:
 *     Shared helpers for parsing and normalizing namespaced IDs in frontend apps.
 */

export const CORE_NAMESPACE = "core";

/**
 * Returns the namespace segment from a qualified name.
 * Bare names default to the core namespace.
 */
export function namespaceFromQualifiedName(name: string): string {
  const normalized = (name ?? "").trim();
  if (!normalized) {
    return CORE_NAMESPACE;
  }
  const dotIndex = normalized.indexOf(".");
  if (dotIndex <= 0) {
    return CORE_NAMESPACE;
  }
  return normalized.slice(0, dotIndex);
}

/**
 * Ensures a name is qualified, defaulting bare IDs to core.<id>.
 */
export function qualifyWithCoreNamespace(name: string): string {
  const normalized = (name ?? "").trim();
  if (!normalized) {
    return `${CORE_NAMESPACE}.`;
  }
  return normalized.includes(".") ? normalized : `${CORE_NAMESPACE}.${normalized}`;
}

