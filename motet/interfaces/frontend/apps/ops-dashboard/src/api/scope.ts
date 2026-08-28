/**
 * Motet - Ops Dashboard - Manage-app scope query helper
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-17
 *
 * Description:
 *     Shared tenant/motet query-string helpers for manage-app fetches.
 *     Null scope fields mean "All Tenants" / "All Motets" and are omitted.
 */
import type { Scope } from "../hooks/useScope";

export function applyScopeParams(params: URLSearchParams, scope: Scope): void {
  if (scope.tenantId) params.set("tenant_id", scope.tenantId);
  if (scope.motetId) params.set("motet_id", scope.motetId);
}

export function scopedUrl(
  path: string,
  scope: Scope,
  extra?: Record<string, string | number | boolean | null | undefined>,
): string {
  const params = new URLSearchParams();
  applyScopeParams(params, scope);
  if (extra) {
    for (const [key, value] of Object.entries(extra)) {
      if (value === undefined || value === null || value === "") {
        continue;
      }
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  if (!qs) {
    return path;
  }
  return path.includes("?") ? `${path}&${qs}` : `${path}?${qs}`;
}
