/**
 * Motet - Ops Dashboard - Live task cancel
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Calls POST /api/v1/tasks/{task_id}/cancel from the Manage Tasks views.
 */

import { getAuthHeaders } from "../api/http";

export function taskStatusIsCancellable(status: string | undefined): boolean {
  return ["running", "executing", "pending"].includes(String(status || "").toLowerCase());
}

export async function cancelLiveTask(taskId: string, reason?: string): Promise<void> {
  const response = await fetch(`/api/v1/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST",
    headers: {
      ...getAuthHeaders(),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      reason: reason || "operator requested stop from Manage",
    }),
  });
  if (!response.ok) {
    let detail = `Failed to cancel task: ${response.status}`;
    try {
      const data = (await response.json()) as { detail?: string };
      if (data?.detail) detail = data.detail;
    } catch {
      // keep status text
    }
    throw new Error(detail);
  }
}
