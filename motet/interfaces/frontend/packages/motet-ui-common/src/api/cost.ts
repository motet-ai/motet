/**
 * Motet UI Common - Cost API Client
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-29
 *
 * Description:
 *     Typed client for conversation cost rollups on /api/v1/cost.
 *
 * Usage:
 *     import { getConversationCost } from "@motet/ui-common/api";
 */

import { knownCostUsd } from "../utils/formatting";

export type ConversationCostResponse = {
  conversation_id: string;
  event_count: number;
  cost_usd?: number | null;
  include_children?: boolean;
};

/**
 * Priced conversation rollup, or null when unknown (not free).
 */
export async function getConversationCost(
  conversationId: string,
  headers: Record<string, string>
): Promise<number | null> {
  const cid = (conversationId || "").trim();
  if (!cid) return null;
  try {
    const r = await fetch(`/api/v1/cost/conversation/${encodeURIComponent(cid)}`, { headers });
    if (!r.ok) return null;
    const data = (await r.json()) as ConversationCostResponse;
    return knownCostUsd(data.cost_usd);
  } catch {
    return null;
  }
}
