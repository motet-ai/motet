/**
 * Motet UI Common - Live Turns Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Subscribes a React tree to LiveTurnRegistry updates so overlay and
 *     per-conversation busy flags re-render when any in-flight turn changes.
 *
 * Dependencies:
 *     - react
 *     - LiveTurnRegistry
 *
 * Usage:
 *     const live = useLiveTurns(registry);
 *     const overlay = live.overlayFor(visibleId);
 *     const busy = live.isBusy(visibleId);
 *
 * Notes:
 *     - The registry instance must be stable (create once per provider)
 */

import { useEffect, useMemo, useState } from "react";
import type { ChatMessage } from "../api/chat";
import type { LiveTurn, LiveTurnRegistry } from "../utils/liveTurns";

function sameStringSet(left: ReadonlySet<string>, right: ReadonlySet<string>): boolean {
  if (left.size !== right.size) return false;
  for (const id of left) {
    if (!right.has(id)) return false;
  }
  return true;
}

export function useLiveTurns(registry: LiveTurnRegistry): {
  epoch: number;
  inFlightIds: ReadonlySet<string>;
  overlayFor: (displayConversationId: string) => ChatMessage | null;
  overlayOwner: (displayConversationId: string) => string | null;
  isBusy: (displayConversationId: string) => boolean;
  isActive: (conversationId: string) => boolean;
  get: (conversationId: string) => LiveTurn | null;
} {
  const [epoch, setEpoch] = useState(0);
  const [inFlightIds, setInFlightIds] = useState<ReadonlySet<string>>(
    () => new Set(registry.inFlightIds())
  );
  useEffect(
    () =>
      registry.subscribe(() => {
        setEpoch((n) => n + 1);
        const next = new Set(registry.inFlightIds());
        setInFlightIds((prev) => (sameStringSet(prev, next) ? prev : next));
      }),
    [registry]
  );
  return useMemo(
    () => ({
      epoch,
      inFlightIds,
      overlayFor: (id) => registry.overlayFor(id),
      overlayOwner: (id) => registry.overlayOwner(id),
      isBusy: (id) => registry.isBusy(id),
      isActive: (id) => registry.isActive(id),
      get: (id) => registry.get(id),
    }),
    [epoch, inFlightIds, registry]
  );
}
