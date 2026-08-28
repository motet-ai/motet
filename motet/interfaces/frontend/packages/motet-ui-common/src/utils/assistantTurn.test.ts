/**
 * Motet UI Common - Assistant Turn Slice Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Unit tests for primary-agent selection, one-turn slice ordering, and
 *     grouping consecutive transcript assistant rows on reload.
 *     Run: npx tsx --test src/utils/assistantTurn.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { isSpawnAgentId, resolveAgentDisplayName } from "./agents";
import {
  assistantTranscriptTurnSlice,
  assistantTurnSlices,
  groupTranscriptAssistantTurns,
  resolvePrimaryAgentKey,
  resolveTranscriptPrimaryAgentKey,
} from "./assistantTurn";
import { mapHistoryToMessages } from "../api/conversations";

const agents = [{ qualified_id: "core.default", display_name: "Default Assistant" }];

test("isSpawnAgentId: spawn suffix only", () => {
  assert.equal(isSpawnAgentId("core.default.spawn-1"), true);
  assert.equal(isSpawnAgentId("core.default.spawn-12"), true);
  assert.equal(isSpawnAgentId("core.default"), false);
  assert.equal(isSpawnAgentId("expert-panel.researcher"), false);
});

test("resolveAgentDisplayName: spawn children become Sub-agent N", () => {
  assert.equal(resolveAgentDisplayName("core.default.spawn-2", agents).agentName, "Sub-agent 2");
  assert.equal(resolveAgentDisplayName("core.default", agents).agentName, "Default Assistant");
});

test("resolvePrimaryAgentKey: selected agent wins when present", () => {
  assert.equal(
    resolvePrimaryAgentKey(["core.default.spawn-1", "core.default"], "core.default"),
    "core.default"
  );
});

test("resolvePrimaryAgentKey: first non-spawn when selected is absent", () => {
  assert.equal(
    resolvePrimaryAgentKey(["core.default.spawn-1", "expert-panel.chair"], "core.default"),
    "expert-panel.chair"
  );
});

test("assistantTurnSlices: children first, primary last, thinking kept", () => {
  const slices = assistantTurnSlices(
    {
      agentStreams: {
        "core.default": {
          contentText: "Here is the synthesis.",
          thinkingText: "I should combine the findings.",
          thinkingComplete: true,
        },
        "core.default.spawn-1": {
          contentText: "Price is $12.",
          thinkingText: "Look up the price.",
          thinkingComplete: true,
        },
      },
    },
    agents,
    "core.default"
  );

  assert.equal(slices.length, 2);
  assert.equal(slices[0].agentKey, "core.default.spawn-1");
  assert.equal(slices[0].isPrimary, false);
  assert.equal(slices[0].agentName, "Sub-agent 1");
  assert.equal(slices[0].thinkingText, "Look up the price.");
  assert.equal(slices[1].agentKey, "core.default");
  assert.equal(slices[1].isPrimary, true);
  assert.equal(slices[1].text, "Here is the synthesis.");
});

test("assistantTurnSlices: thinking-only child is still a slice", () => {
  const slices = assistantTurnSlices(
    {
      agentStreams: {
        "core.default": { contentText: "Done." },
        "core.default.spawn-1": { thinkingText: "Working...", thinkingComplete: false },
      },
    },
    agents,
    "core.default"
  );
  assert.equal(slices[0].text, "");
  assert.equal(slices[0].thinkingText, "Working...");
  assert.equal(slices[0].thinkingComplete, false);
});

test("assistantTranscriptTurnSlice: restored history is a single primary slice", () => {
  const slice = assistantTranscriptTurnSlice(
    { content: "Hello", meta: { agent_id: "core.default" } },
    agents
  );
  assert.ok(slice);
  assert.equal(slice.isPrimary, true);
  assert.equal(slice.agentName, "Default Assistant");
  assert.equal(slice.thinkingText, "");
});

test("resolveTranscriptPrimaryAgentKey: selected wins, else last row", () => {
  assert.equal(
    resolveTranscriptPrimaryAgentKey(
      ["expert-panel.optimist", "expert-panel.synthesizer", "core.default"],
      "core.default"
    ),
    "core.default"
  );
  assert.equal(
    resolveTranscriptPrimaryAgentKey(
      ["expert-panel.optimist", "expert-panel.synthesizer"],
      "core.default"
    ),
    "expert-panel.synthesizer"
  );
});

test("groupTranscriptAssistantTurns: panel rows become one bubble", () => {
  const grouped = groupTranscriptAssistantTurns(
    [
      { role: "user", content: "Run a panel on AI" },
      { role: "assistant", content: "Optimistic take", meta: { agent_id: "expert-panel.optimist" } },
      { role: "assistant", content: "Skeptical take", meta: { agent_id: "expert-panel.skeptic" } },
      { role: "assistant", content: "## Executive Summary", meta: { agent_id: "expert-panel.synthesizer" } },
      { role: "assistant", content: "Here is the assessment.", meta: { agent_id: "core.default" } },
    ],
    "core.default"
  );
  assert.equal(grouped.length, 2);
  assert.equal(grouped[0].role, "user");
  assert.equal(grouped[1].role, "assistant");
  assert.equal(grouped[1].content, "Here is the assessment.");
  assert.equal(grouped[1].meta?.agent_id, "core.default");
  const streams = grouped[1].meta?.agentStreams as Record<string, { contentText?: string }>;
  assert.equal(streams["expert-panel.optimist"].contentText, "Optimistic take");
  assert.equal(streams["expert-panel.skeptic"].contentText, "Skeptical take");
  assert.equal(streams["expert-panel.synthesizer"].contentText, "## Executive Summary");
  assert.equal(streams["core.default"].contentText, "Here is the assessment.");

  const slices = assistantTurnSlices(grouped[1].meta, [
    ...agents,
    { qualified_id: "expert-panel.optimist", display_name: "Panel Optimist" },
    { qualified_id: "expert-panel.skeptic", display_name: "Panel Skeptic" },
    { qualified_id: "expert-panel.synthesizer", display_name: "Panel Synthesizer" },
  ], "core.default");
  assert.equal(slices.filter((s) => !s.isPrimary).length, 3);
  assert.equal(slices.find((s) => s.isPrimary)?.agentKey, "core.default");
});

test("groupTranscriptAssistantTurns: two user turns stay two bubbles", () => {
  const grouped = groupTranscriptAssistantTurns(
    [
      { role: "user", content: "First" },
      { role: "assistant", content: "A", meta: { agent_id: "core.default" } },
      { role: "user", content: "Second" },
      { role: "assistant", content: "B", meta: { agent_id: "core.default" } },
    ],
    "core.default"
  );
  assert.equal(grouped.length, 4);
  assert.equal(grouped[1].content, "A");
  assert.equal(grouped[3].content, "B");
});

test("groupTranscriptAssistantTurns: assistant without agent_id stays separate", () => {
  const grouped = groupTranscriptAssistantTurns(
    [
      { role: "user", content: "Hi" },
      { role: "assistant", content: "legacy reply" },
      { role: "assistant", content: "panel", meta: { agent_id: "expert-panel.optimist" } },
      { role: "assistant", content: "root", meta: { agent_id: "core.default" } },
    ],
    "core.default"
  );
  assert.equal(grouped.length, 3);
  assert.equal(grouped[1].content, "legacy reply");
  assert.equal(grouped[2].meta?.agent_id, "core.default");
  const streams = grouped[2].meta?.agentStreams as Record<string, unknown>;
  assert.equal(Object.keys(streams).length, 2);
});

test("mapHistoryToMessages: keeps parent_agent_id on grouped spawn streams", () => {
  const mapped = mapHistoryToMessages(
    [
      { role: "user", content: "price?" },
      {
        role: "assistant",
        content: "12",
        agent_id: "core.default.spawn-1",
        parent_agent_id: "core.default",
      },
      { role: "assistant", content: "done", agent_id: "core.default" },
    ],
    "core.default"
  );
  assert.equal(mapped.length, 2);
  const streams = mapped[1].meta?.agentStreams as Record<
    string,
    { parent_agent_id?: string }
  >;
  assert.equal(streams["core.default.spawn-1"].parent_agent_id, "core.default");
});

test("mapHistoryToMessages: groups a panel transcript", () => {
  const mapped = mapHistoryToMessages(
    [
      { role: "user", content: "panel please" },
      { role: "assistant", content: "pro", agent_id: "expert-panel.optimist" },
      { role: "assistant", content: "con", agent_id: "expert-panel.skeptic" },
      { role: "assistant", content: "balanced", agent_id: "core.default" },
    ],
    "core.default"
  );
  assert.equal(mapped.length, 2);
  assert.equal(mapped[1].content, "balanced");
  const streams = mapped[1].meta?.agentStreams as Record<string, { contentText?: string }>;
  assert.equal(streams["expert-panel.optimist"].contentText, "pro");
  assert.equal(streams["expert-panel.skeptic"].contentText, "con");
});
