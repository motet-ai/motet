/**
 * Motet - Ops Dashboard - Tenants Catalog API Client (ADR-0126)
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Fetch helpers for /api/v1/tenants used by ScopeSelector and TenantsPage.
 *
 * Last Modified: 2026-08-17
 */
import { getAuthHeaders } from "./http";

/**
 * Sentinel meaning "every tenant the caller may see". Scope state still uses
 * null for that case; this value is only sent to APIs that can aggregate, so
 * they can tell "all tenants" apart from "the caller's own tenant".
 */
export const ALL_TENANTS = "__all__";

export interface MotetInfo {
  id: string;
  name: string;
  tenant_id: string;
  status?: string;
  description?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface TenantInfo {
  id: string;
  name: string;
  status?: string;
  description?: string | null;
  created_at?: string;
  updated_at?: string;
  motets?: MotetInfo[];
}

export interface TenantListResponse {
  tenants: TenantInfo[];
  can_access_all_tenants: boolean;
}

async function parseError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    return JSON.stringify(body?.detail ?? body);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

export async function fetchTenantCatalog(
  includeMotets = true
): Promise<TenantListResponse> {
  const params = new URLSearchParams();
  if (includeMotets) params.set("include_motets", "true");
  const response = await fetch(`/api/v1/tenants?${params}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  return response.json();
}

export async function ensureTenantDefaults(): Promise<{ created: Record<string, number> }> {
  const response = await fetch("/api/v1/tenants/ensure-defaults", {
    method: "POST",
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  return response.json();
}

export async function createTenant(payload: {
  id: string;
  name?: string;
  description?: string;
  status?: string;
}): Promise<TenantInfo> {
  const response = await fetch("/api/v1/tenants", {
    method: "POST",
    headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  return response.json();
}

export async function updateTenant(
  tenantId: string,
  payload: { name?: string; description?: string; status?: string }
): Promise<TenantInfo> {
  const response = await fetch(`/api/v1/tenants/${encodeURIComponent(tenantId)}`, {
    method: "PATCH",
    headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  return response.json();
}

export async function deleteTenant(tenantId: string, force = false): Promise<void> {
  const params = force ? "?force=true" : "";
  const response = await fetch(
    `/api/v1/tenants/${encodeURIComponent(tenantId)}${params}`,
    {
      method: "DELETE",
      headers: getAuthHeaders(),
    }
  );
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
}

export async function createMotet(
  tenantId: string,
  payload: { id: string; name?: string; description?: string; status?: string }
): Promise<MotetInfo> {
  const response = await fetch(
    `/api/v1/tenants/${encodeURIComponent(tenantId)}/motets`,
    {
      method: "POST",
      headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  return response.json();
}

export async function updateMotet(
  tenantId: string,
  motetId: string,
  payload: { name?: string; description?: string; status?: string }
): Promise<MotetInfo> {
  const response = await fetch(
    `/api/v1/tenants/${encodeURIComponent(tenantId)}/motets/${encodeURIComponent(motetId)}`,
    {
      method: "PATCH",
      headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
  return response.json();
}

export async function deleteMotet(tenantId: string, motetId: string): Promise<void> {
  const response = await fetch(
    `/api/v1/tenants/${encodeURIComponent(tenantId)}/motets/${encodeURIComponent(motetId)}`,
    {
      method: "DELETE",
      headers: getAuthHeaders(),
    }
  );
  if (!response.ok) {
    throw new Error(await parseError(response));
  }
}

export const TENANTS_CATALOG_QUERY_KEY = ["tenants", "catalog"] as const;
