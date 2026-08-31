/**
 * Motet UI Common - Live Turn Registry Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Unit tests for the conversation-keyed live-turn map.
 *     Run: npx tsx --test src/utils/liveTurns.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  LiveTurnRegistry,
  chatOutputConversationId,
  isRenderableLiveMessage,
  shouldClearLiveTurn,
  tagChatOutputConversation,
} from "./liveTurns";

test("isRenderableLiveMessage: empty loading assistant is not shown", () => {
  assert.equal(isRenderableLiveMessage({ role: "assistant", content: "", status: "loading" }), false);
  assert.equal(isRenderableLiveMessage({ role: "assistant", content: "hi" }), true);
});

test("LiveTurnRegistry: inFlightIds includes the owner and spawn children", () => {
  const turns = new LiveTurnRegistry();
  turns.start("conv-parent");
  assert.deepEqual(turns.inFlightIds(), ["conv-parent"]);
  turns.applyChunk("conv-parent", { event: "token", data: { t: "hi" } });
  const live = turns.get("conv-parent")?.message;
  assert.ok(live);
  live.meta = {
    spawn_children: [
      { child_conversation_id: "iso-abc", agent_id: "core.default.spawn-1", title: "research" },
    ],
  };
  assert.deepEqual(new Set(turns.inFlightIds()), new Set(["conv-parent", "iso-abc"]));
  turns.applyChunk("conv-parent", { event: "end", data: {} });
  assert.deepEqual(turns.inFlightIds(), []);
});

test("LiveTurnRegistry: start() is not an overlayable assistant bubble", () => {
  const turns = new LiveTurnRegistry();
  turns.start("conv-a");
  assert.equal(turns.overlayFor("conv-a"), null);
  assert.equal(turns.isBusy("conv-a"), true);
  turns.applyChunk("conv-a", { event: "token", data: { t: "hello" } });
  assert.equal(turns.overlayFor("conv-a")?.content, "hello");
});

test("LiveTurnRegistry: two conversations reduce independently", () => {
  const turns = new LiveTurnRegistry();
  turns.start("conv-a");
  turns.start("conv-b");
  turns.applyChunk("conv-a", { event: "token", data: { t: "alpha" } });
  turns.applyChunk("conv-b", { event: "token", data: { t: "beta" } });
  assert.equal(turns.get("conv-a")?.message.content, "alpha");
  assert.equal(turns.get("conv-b")?.message.content, "beta");
  assert.equal(turns.isActive("conv-a"), true);
  assert.equal(turns.isActive("conv-b"), true);
  assert.equal(turns.isBusy("conv-a"), true);
  assert.equal(turns.isBusy("conv-b"), true);
  assert.equal(turns.isBusy("conv-other"), false);
});

test("LiveTurnRegistry: overlay is the owner or a projected spawn child", () => {
  const turns = new LiveTurnRegistry();
  turns.start("conv-parent");
  turns.applyChunk("conv-parent", { event: "token", data: { t: "parent" } });
  const live = turns.get("conv-parent")?.message;
  assert.ok(live);
  live.meta = {
    spawn_children: [
      { child_conversation_id: "iso-abc", agent_id: "core.default.spawn-1", title: "research" },
    ],
    agentStreams: {
      "core.default": { contentText: "parent" },
      "core.default.spawn-1": { contentText: "child-text" },
    },
  };
  assert.equal(turns.overlayFor("conv-parent"), live);
  assert.equal(turns.overlayFor("iso-abc")?.content, "child-text");
  assert.equal(turns.overlayFor("conv-other"), null);
  assert.equal(turns.overlayOwner("iso-abc"), "conv-parent");
  assert.equal(turns.isBusy("iso-abc"), true);
});

test("LiveTurnRegistry: end inactivates only that conversation", () => {
  const turns = new LiveTurnRegistry();
  turns.start("conv-a");
  turns.start("conv-b");
  turns.applyChunk("conv-a", { event: "end", data: {} });
  assert.equal(turns.isActive("conv-a"), false);
  assert.equal(turns.isActive("conv-b"), true);
  assert.equal(turns.get("conv-a")?.message.role, "assistant");
  assert.equal(turns.overlayFor("conv-a"), null);
});

test("LiveTurnRegistry: start resets only the named conversation", () => {
  const turns = new LiveTurnRegistry();
  turns.start("conv-a");
  turns.applyChunk("conv-a", { event: "token", data: { t: "keep" } });
  turns.start("conv-b");
  turns.applyChunk("conv-b", { event: "token", data: { t: "new" } });
  assert.equal(turns.get("conv-a")?.message.content, "keep");
  assert.equal(turns.get("conv-b")?.message.content, "new");
});

test("shouldClearLiveTurn: finished owner only", () => {
  assert.equal(
    shouldClearLiveTurn({
      displayConversationId: "conv-a",
      streamConversationId: "conv-a",
      displayIsRequesting: false,
      streamIsActive: false,
    }),
    true
  );
  assert.equal(
    shouldClearLiveTurn({
      displayConversationId: "conv-b",
      streamConversationId: "conv-a",
      displayIsRequesting: false,
      streamIsActive: false,
    }),
    false
  );
  assert.equal(
    shouldClearLiveTurn({
      displayConversationId: "conv-a",
      streamConversationId: "conv-a",
      displayIsRequesting: false,
      streamIsActive: true,
    }),
    false
  );
});

test("tagChatOutputConversation: chunk carries the owner id", () => {
  const tagged = tagChatOutputConversation({ event: "token", data: { t: "x" } }, "conv-a");
  assert.equal(chatOutputConversationId(tagged), "conv-a");
});
