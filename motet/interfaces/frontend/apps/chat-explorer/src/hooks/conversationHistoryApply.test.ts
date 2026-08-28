/**
 * Motet - Chat Explorer - Conversation History Apply Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Unit tests for the useXChat store-ready and apply gates used when
 *     hydrating conversation history. Run:
 *     npx tsx --test src/hooks/conversationHistoryApply.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  isHistoryStoreReady,
  shouldApplyHistory,
  isLegacyTruncatedAutoTitle,
  shouldFlushAutoTitlePersist,
  shouldQueueAutoTitle,
} from "./conversationHistoryApply";

test("isHistoryStoreReady: first mount store is already the active key", () => {
  assert.equal(isHistoryStoreReady("conv-a", "conv-a", "conv-a"), true);
});

test("isHistoryStoreReady: history arrived before the store swapped", () => {
  assert.equal(isHistoryStoreReady("conv-b", "conv-b", "conv-a"), false);
});

test("isHistoryStoreReady: stale pending for a conversation that is no longer active", () => {
  assert.equal(isHistoryStoreReady("conv-a", "conv-b", "conv-b"), false);
});

test("isHistoryStoreReady: store not marked ready yet", () => {
  assert.equal(isHistoryStoreReady("conv-b", "conv-b", null), false);
});

test("shouldApplyHistory: empty existing thread hydrates", () => {
  assert.equal(
    shouldApplyHistory({
      pendingKey: "conv-a",
      activeKey: "conv-a",
      storeReadyKey: "conv-a",
      isRequesting: false,
      localMessageCount: 0,
      alreadyHydrated: false,
    }),
    true,
  );
});

test("shouldApplyHistory: skip while a turn is in flight", () => {
  assert.equal(
    shouldApplyHistory({
      pendingKey: "conv-a",
      activeKey: "conv-a",
      storeReadyKey: "conv-a",
      isRequesting: true,
      localMessageCount: 1,
      alreadyHydrated: false,
    }),
    false,
  );
});

test("shouldApplyHistory: skip first hydrate when the new chat already has local messages", () => {
  assert.equal(
    shouldApplyHistory({
      pendingKey: "conv-new",
      activeKey: "conv-new",
      storeReadyKey: "conv-new",
      isRequesting: false,
      localMessageCount: 2,
      alreadyHydrated: false,
    }),
    false,
  );
});

test("shouldQueueAutoTitle: first user message on a ready New Chat", () => {
  assert.equal(
    shouldQueueAutoTitle({
      storeReadyKey: "conv-new",
      activeKey: "conv-new",
      label: "New Chat",
      alreadyUpdated: false,
      hasUserMessage: true,
    }),
    true,
  );
});

test("shouldQueueAutoTitle: skip while the store still belongs to the previous chat", () => {
  assert.equal(
    shouldQueueAutoTitle({
      storeReadyKey: "conv-old",
      activeKey: "conv-new",
      label: "New Chat",
      alreadyUpdated: false,
      hasUserMessage: true,
    }),
    false,
  );
});

test("isLegacyTruncatedAutoTitle: 40-char snippet of the first user message", () => {
  const content = "please navigate and screenshot https://example.com and do more";
  assert.equal(isLegacyTruncatedAutoTitle(`${content.slice(0, 40)}...`, content), true);
  assert.equal(isLegacyTruncatedAutoTitle("Renamed chat", content), false);
});

test("shouldFlushAutoTitlePersist: wait until the first turn finishes", () => {
  assert.equal(
    shouldFlushAutoTitlePersist({
      pendingKey: "conv-new",
      turnCompletedForPending: false,
      isRequesting: true,
    }),
    false,
  );
  assert.equal(
    shouldFlushAutoTitlePersist({
      pendingKey: "conv-new",
      turnCompletedForPending: true,
      isRequesting: false,
    }),
    true,
  );
});
