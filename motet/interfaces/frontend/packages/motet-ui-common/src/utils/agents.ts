/**
 * Motet UI Common - Agent Display Utilities
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Pure utility functions for resolving agent display names from qualified
 *     registry ids for stream routing and display. Spawned children
 *     (`{parent}.spawn-N`) are labeled Sub-agent N when they are not in the
 *     registry.
 *
 * Usage:
 *     import { shortAgentLabel, resolveAgentDisplayName, isSpawnAgentId } from "@motet/ui-common/utils";
 *
 *     shortAgentLabel("core.default")           // → "default"
 *     resolveAgentDisplayName("core.default", agents).agentName  // → "Default Assistant"
 *     resolveAgentDisplayName("core.default.spawn-1", []).agentName  // → "Sub-agent 1"
 */

import { DEFAULT_STREAM_AGENT_KEY } from "../types";

/** `{parent}.spawn-N` — identity assigned by core.spawn_agents. */
const SPAWN_AGENT_RE = /\.spawn-(\d+)$/;

/** Minimal shape for agent registry entries used by display resolution. */
export type AgentRegistryEntry = {
  qualified_id: string;
  display_name?: string;
};

/** True when the id is a spawn_agents child (`{parent}.spawn-N`). */
export function isSpawnAgentId(qualifiedId: string): boolean {
  return SPAWN_AGENT_RE.test(qualifiedId);
}

/** 1-based spawn ordinal, or null when the id is not a spawn child. */
export function spawnAgentOrdinal(qualifiedId: string): number | null {
  const match = qualifiedId.match(SPAWN_AGENT_RE);
  if (!match) return null;
  const n = Number(match[1]);
  return Number.isFinite(n) ? n : null;
}

/** Short label for sidebar headers (e.g. core.default → default). */
export function shortAgentLabel(qualifiedId: string): string {
  const parts = qualifiedId.split(".");
  return parts.length > 1 ? parts.slice(1).join(".") : qualifiedId;
}

/** Resolve registry display_name for a qualified id; fallback to short id segment. */
export function resolveAgentDisplayName(
  qualifiedId: string,
  agents: AgentRegistryEntry[]
): { agentName: string; displayLabel: string } {
  const displayLabel = shortAgentLabel(qualifiedId);
  if (qualifiedId === DEFAULT_STREAM_AGENT_KEY) {
    return { agentName: "Assistant", displayLabel: "Assistant" };
  }
  const hit = agents.find((a) => a.qualified_id === qualifiedId);
  const name = hit?.display_name?.trim();
  if (name) {
    return { agentName: name, displayLabel };
  }
  const spawnN = spawnAgentOrdinal(qualifiedId);
  if (spawnN != null) {
    const spawnLabel = `Sub-agent ${spawnN}`;
    return { agentName: spawnLabel, displayLabel: spawnLabel };
  }
  return { agentName: displayLabel, displayLabel };
}
