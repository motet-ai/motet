/**
 * Motet - Ops Dashboard - Scope Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Manages tenant/motet scope selection with URL persistence.
 *
 * Last Modified: 2026-08-24
 *
 * Notes:
 *     The scope object is referentially stable when tenant/motet query
 *     params do not change, so catalog pages do not refetch on parent
 *     re-renders (JWT refresh, polling).
 */
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

export interface Scope {
  tenantId: string | null;  // null = "All Tenants"
  motetId: string | null;  // null = "All Motets"
}

export function useScope() {
  const [searchParams, setSearchParams] = useSearchParams();

  const tenantId = searchParams.get("tenant");
  const motetId = searchParams.get("motet");
  const scope = useMemo<Scope>(
    () => ({ tenantId, motetId }),
    [tenantId, motetId],
  );

  // Update scope in URL params
  const setScope = useCallback(
    (newScope: Partial<Scope>) => {
      const params = new URLSearchParams(searchParams);

      if (newScope.tenantId !== undefined) {
        if (newScope.tenantId) {
          params.set("tenant", newScope.tenantId);
        } else {
          params.delete("tenant");
        }
        // Clear motet when tenant changes
        if (newScope.motetId === undefined) {
          params.delete("motet");
        }
      }

      if (newScope.motetId !== undefined) {
        if (newScope.motetId) {
          params.set("motet", newScope.motetId);
        } else {
          params.delete("motet");
        }
      }

      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams]
  );

  return { scope, setScope };
}
