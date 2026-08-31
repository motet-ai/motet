/**
 * Motet - Chat Explorer - Conversation History Apply Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
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
  pendingTitlesToFlush,
  shouldClearLiveTurn,
  shouldFetchConversationHistory,
  shouldKeepLiveStreamOverHistory,
  shouldQueueAutoTitle,
  shouldQueueAutoTitleFromSend,
  shouldWriteSendToStore,
  firstUserMessageText,
  autoTitleFromUserText,
} from "./conversationHistoryApply";

test("shouldFetchConversationHistory: skip while this chat owns the live stream", () => {
  assert.equal(
    shouldFetchConversationHistory({
      canFetch: true,
      ownerStreamLive: true,
      viewingChildDuringParentTurn: false,
    }),
    false,
  );
});

test("shouldFetchConversationHistory: hydrate when idle", () => {
  assert.equal(
    shouldFetchConversationHistory({
      canFetch: true,
      ownerStreamLive: false,
      viewingChildDuringParentTurn: false,
    }),
    true,
  );
});

test("shouldFetchConversationHistory: poll a spawn child during the parent turn", () => {
  assert.equal(
    shouldFetchConversationHistory({
      canFetch: true,
      ownerStreamLive: false,
      viewingChildDuringParentTurn: true,
    }),
    true,
  );
});

test("shouldFetchConversationHistory: no credentials", () => {
  assert.equal(
    shouldFetchConversationHistory({
      canFetch: false,
      ownerStreamLive: false,
      viewingChildDuringParentTurn: false,
    }),
    false,
  );
});

test("shouldClearLiveTurn: only the stream owner going idle ends the live turn", () => {
  assert.equal(
    shouldClearLiveTurn({
      displayConversationId: "conv-a",
      streamConversationId: "conv-a",
      displayIsRequesting: false,
    }),
    true,
  );
  assert.equal(
    shouldClearLiveTurn({
      displayConversationId: "conv-b",
      streamConversationId: "conv-a",
      displayIsRequesting: false,
    }),
    false,
  );
  assert.equal(
    shouldClearLiveTurn({
      displayConversationId: "conv-a",
      streamConversationId: "conv-a",
      displayIsRequesting: true,
    }),
    false,
  );
  assert.equal(
    shouldClearLiveTurn({
      displayConversationId: "conv-a",
      streamConversationId: "conv-a",
      displayIsRequesting: false,
      streamIsActive: true,
    }),
    false,
  );
});

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

test("shouldApplyHistory: hydrate a spawn child while the parent turn is in flight", () => {
  assert.equal(
    shouldApplyHistory({
      pendingKey: "iso-child",
      activeKey: "iso-child",
      storeReadyKey: "iso-child",
      isRequesting: true,
      localMessageCount: 0,
      alreadyHydrated: false,
      streamingConversationKey: "conv-parent",
    }),
    true,
  );
});

test("shouldApplyHistory: skip the owner while its live stream is still active", () => {
  assert.equal(
    shouldApplyHistory({
      pendingKey: "conv-a",
      activeKey: "conv-a",
      storeReadyKey: "conv-a",
      isRequesting: false,
      localMessageCount: 2,
      alreadyHydrated: true,
      streamingConversationKey: "conv-a",
      ownerLiveActive: true,
    }),
    false,
  );
});

test("shouldApplyHistory: GET may replace a live-only seed on a spawn child", () => {
  assert.equal(
    shouldApplyHistory({
      pendingKey: "iso-child",
      activeKey: "iso-child",
      storeReadyKey: "iso-child",
      isRequesting: true,
      localMessageCount: 1,
      alreadyHydrated: false,
      streamingConversationKey: "conv-parent",
    }),
    true,
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

test("shouldQueueAutoTitleFromSend: titles from outgoing text, not the store", () => {
  assert.equal(
    shouldQueueAutoTitleFromSend({
      label: "New Chat",
      alreadyUpdated: false,
      hasUserMessage: true,
    }),
    true,
  );
  assert.equal(
    shouldQueueAutoTitleFromSend({
      label: "New Chat",
      alreadyUpdated: true,
      hasUserMessage: true,
    }),
    false,
  );
});

test("shouldWriteSendToStore: only the ready conversation", () => {
  assert.equal(shouldWriteSendToStore("conv-new", "conv-new"), true);
  assert.equal(shouldWriteSendToStore("conv-old", "conv-new"), false);
  assert.equal(shouldWriteSendToStore(null, "conv-new"), false);
});

test("firstUserMessageText: first user body only", () => {
  assert.equal(firstUserMessageText([]), "");
  assert.equal(
    firstUserMessageText([
      { message: { role: "assistant", content: "hi" } },
      { message: { role: "user", content: "  weather  " } },
      { message: { role: "user", content: "later" } },
    ]),
    "weather",
  );
});

test("autoTitleFromUserText: collapse whitespace and cap length", () => {
  assert.equal(autoTitleFromUserText("  hello\nworld  "), "hello world");
  assert.equal(autoTitleFromUserText("x".repeat(600)).length, 500);
});

test("isLegacyTruncatedAutoTitle: 40-char snippet of the first user message", () => {
  const content = "please navigate and screenshot https://example.com and do more";
  assert.equal(isLegacyTruncatedAutoTitle(`${content.slice(0, 40)}...`, content), true);
  assert.equal(isLegacyTruncatedAutoTitle("Renamed chat", content), false);
});

test("shouldKeepLiveStreamOverHistory: keep live when history has no summaries", () => {
  assert.equal(
    shouldKeepLiveStreamOverHistory({
      liveHasAgentStreams: true,
      liveHasToolSummaries: false,
      liveHasToolExecutions: false,
      liveIsStreaming: false,
      historyHasToolSummaries: false,
    }),
    true,
  );
});

test("shouldKeepLiveStreamOverHistory: replace token-only live when only history has summaries", () => {
  assert.equal(
    shouldKeepLiveStreamOverHistory({
      liveHasAgentStreams: true,
      liveHasToolSummaries: false,
      liveHasToolExecutions: false,
      liveIsStreaming: false,
      historyHasToolSummaries: true,
    }),
    false,
  );
});

test("shouldKeepLiveStreamOverHistory: keep live tool executions even when history has summaries", () => {
  assert.equal(
    shouldKeepLiveStreamOverHistory({
      liveHasAgentStreams: true,
      liveHasToolSummaries: false,
      liveHasToolExecutions: true,
      liveIsStreaming: false,
      historyHasToolSummaries: true,
    }),
    true,
  );
});

test("shouldKeepLiveStreamOverHistory: keep an in-flight assistant", () => {
  assert.equal(
    shouldKeepLiveStreamOverHistory({
      liveHasAgentStreams: true,
      liveHasToolSummaries: false,
      liveHasToolExecutions: false,
      liveIsStreaming: true,
      historyHasToolSummaries: true,
    }),
    true,
  );
});

test("shouldKeepLiveStreamOverHistory: empty live store always applies history", () => {
  assert.equal(
    shouldKeepLiveStreamOverHistory({
      liveHasAgentStreams: false,
      liveHasToolSummaries: false,
      liveHasToolExecutions: false,
      liveIsStreaming: false,
      historyHasToolSummaries: true,
    }),
    false,
  );
});

test("pendingTitlesToFlush: each chat persists after its own turn", () => {
  const pending = new Map([
    ["conv-a", "hello"],
    ["conv-b", "weather"],
  ]);
  const seen = new Set(["conv-a", "conv-b"]);
  assert.deepEqual(
    pendingTitlesToFlush({
      pending,
      inFlightIds: new Set(["conv-b"]),
      seenInFlight: seen,
    }),
    [{ key: "conv-a", title: "hello" }],
  );
  assert.deepEqual(
    pendingTitlesToFlush({
      pending,
      inFlightIds: new Set(),
      seenInFlight: seen,
    }),
    [
      { key: "conv-a", title: "hello" },
      { key: "conv-b", title: "weather" },
    ],
  );
});

test("pendingTitlesToFlush: wait until that chat has been in flight", () => {
  assert.deepEqual(
    pendingTitlesToFlush({
      pending: new Map([["conv-new", "hello"]]),
      inFlightIds: new Set(),
      seenInFlight: new Set(),
    }),
    [],
  );
});
