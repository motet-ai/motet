/**
 * Motet - Ops Dashboard - Surfaces Catalog API client
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Fetch helpers for /api/v1/surfaces and agent surface allow-list updates.
 *
 * Last Modified: 2026-08-17
 */
import { getAuthHeaders } from "./http";

export const SURFACES_CATALOG_QUERY_KEY = ["surfaces"] as const;

export interface SurfaceInfo {
  id: string;
  display_name: string;
  description?: string | null;
  builtin: boolean;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
}

export interface SurfacesListResponse {
  surfaces: SurfaceInfo[];
  total: number;
  can_manage: boolean;
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

export async function fetchSurfaces(): Promise<SurfacesListResponse> {
  const response = await fetch("/api/v1/surfaces", { headers: getAuthHeaders() });
  if (!response.ok) throw new Error(await parseError(response));
  const data = await response.json();
  return {
    surfaces: Array.isArray(data?.surfaces) ? data.surfaces : [],
    total: Number(data?.total ?? 0),
    can_manage: Boolean(data?.can_manage),
  };
}

export async function createSurface(input: {
  id: string;
  display_name?: string;
  description?: string;
}): Promise<SurfaceInfo> {
  const response = await fetch("/api/v1/surfaces", {
    method: "POST",
    headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function updateSurface(
  surfaceId: string,
  input: { display_name?: string; description?: string },
): Promise<SurfaceInfo> {
  const response = await fetch(`/api/v1/surfaces/${encodeURIComponent(surfaceId)}`, {
    method: "PATCH",
    headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}

export async function deleteSurface(surfaceId: string): Promise<void> {
  const response = await fetch(`/api/v1/surfaces/${encodeURIComponent(surfaceId)}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });
  if (!response.ok) throw new Error(await parseError(response));
}

export async function putAgentSurfaces(
  qualifiedId: string,
  body: { allowed_surface_ids?: string[] | null; clear?: boolean },
): Promise<{ qualified_id: string; allowed_surface_ids: string[] | null }> {
  const response = await fetch(
    `/api/v1/agents/${encodeURIComponent(qualifiedId)}/surfaces`,
    {
      method: "PUT",
      headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  if (!response.ok) throw new Error(await parseError(response));
  return response.json();
}
