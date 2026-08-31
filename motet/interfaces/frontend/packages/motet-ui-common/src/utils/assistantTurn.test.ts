/**
 * Motet UI Common - Assistant Turn Slice Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
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
  asSpawnChildCards,
  assistantTranscriptTurnSlice,
  assistantTurnSlices,
  groupTranscriptAssistantTurns,
  resolvePrimaryAgentKey,
  resolveTranscriptPrimaryAgentKey,
  spawnCardsForTurn,
  peerSpeakerSlices,
  projectLiveSpawnChildMessage,
  resolveDisplayedLiveMessage,
  type TranscriptHistoryMessage,
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
  const grouped = groupTranscriptAssistantTurns<TranscriptHistoryMessage>(
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
  const speakers = peerSpeakerSlices(slices.filter((s) => !s.isPrimary), []);
  assert.deepEqual(
    speakers.map((s) => s.agentKey),
    ["expert-panel.optimist", "expert-panel.skeptic", "expert-panel.synthesizer"]
  );
  assert.equal(speakers[0].text, "Optimistic take");
});

test("peerSpeakerSlices: drops spawn children and spawn card keys", () => {
  const speakers = peerSpeakerSlices(
    [
      {
        agentKey: "expert-panel.optimist",
        agentName: "Panel Optimist",
        text: "pro",
        thinkingText: "look on the bright side",
        thinkingComplete: true,
        isPrimary: false,
      },
      {
        agentKey: "core.default.spawn-1",
        agentName: "Sub-agent 1",
        text: "Sacramento Austin",
        thinkingText: "pick two",
        thinkingComplete: true,
        isPrimary: false,
        childConversationId: "iso-abc",
      },
    ],
    [{ child_conversation_id: "iso-abc", agent_id: "core.default.spawn-1", title: "US capitals" }]
  );
  assert.equal(speakers.length, 1);
  assert.equal(speakers[0].agentKey, "expert-panel.optimist");
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
  const grouped = groupTranscriptAssistantTurns<TranscriptHistoryMessage>(
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

test("mapHistoryToMessages: restores thinking_text onto agentStreams", () => {
  const mapped = mapHistoryToMessages(
    [
      { role: "user", content: "price?" },
      {
        role: "assistant",
        content: "12",
        agent_id: "core.default.spawn-1",
        thinking_text: "Look up the list price.",
      },
      {
        role: "assistant",
        content: "done",
        agent_id: "core.default",
        thinking_text: "I should combine the findings.",
      },
    ],
    "core.default"
  );
  assert.equal(mapped.length, 2);
  const streams = mapped[1].meta?.agentStreams as Record<
    string,
    { thinkingText?: string; thinkingComplete?: boolean }
  >;
  assert.equal(streams["core.default.spawn-1"].thinkingText, "Look up the list price.");
  assert.equal(streams["core.default.spawn-1"].thinkingComplete, true);
  assert.equal(streams["core.default"].thinkingText, "I should combine the findings.");
});

test("mapHistoryToMessages: restores tool_summaries onto agentStreams", () => {
  const mapped = mapHistoryToMessages(
    [
      { role: "user", content: "price?" },
      {
        role: "assistant",
        content: "12",
        agent_id: "core.default.spawn-1",
        tool_summaries: [{ tool_name: "core.web_search", status: "success", preview: "list price", step: 1 }],
      },
      {
        role: "assistant",
        content: "done",
        agent_id: "core.default",
        tool_summaries: [{ tool_name: "core.spawn_agents", status: "success" }],
      },
    ],
    "core.default"
  );
  assert.equal(mapped.length, 2);
  const streams = mapped[1].meta?.agentStreams as Record<
    string,
    { toolSummaries?: Array<{ tool_name: string; status: string; preview?: string; step?: number }> }
  >;
  assert.equal(streams["core.default.spawn-1"].toolSummaries?.[0].tool_name, "core.web_search");
  assert.equal(streams["core.default.spawn-1"].toolSummaries?.[0].preview, "list price");
  assert.equal(streams["core.default.spawn-1"].toolSummaries?.[0].step, 1);
  assert.equal(streams["core.default"].toolSummaries?.[0].tool_name, "core.spawn_agents");
});

test("mapHistoryToMessages: restores cost_usd onto agentStreams", () => {
  const mapped = mapHistoryToMessages(
    [
      { role: "user", content: "price?" },
      {
        role: "assistant",
        content: "12",
        agent_id: "core.default.spawn-1",
        cost_usd: 0.004,
      },
      {
        role: "assistant",
        content: "done",
        agent_id: "core.default",
        cost_usd: 0.011,
      },
    ],
    "core.default"
  );
  const streams = mapped[1].meta?.agentStreams as Record<string, { costUsd?: number }>;
  assert.equal(streams["core.default.spawn-1"].costUsd, 0.004);
  assert.equal(streams["core.default"].costUsd, 0.011);
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

test("spawnCardsForTurn: live slices without a child id do not invent one", () => {
  const cards = spawnCardsForTurn(
    {},
    "conv-1",
    [
      {
        agentKey: "core.default.spawn-1",
        agentName: "Sub-agent 1",
        text: "Price is $12.",
        thinkingText: "",
        thinkingComplete: true,
        isPrimary: false,
      },
    ]
  );
  assert.equal(cards.length, 0);
});

test("asSpawnChildCards: keeps child_conversation_id and drops empty rows", () => {
  const cards = asSpawnChildCards([
    { child_conversation_id: "conv-1__spawn_1", title: "research pricing", preview: "price is 12" },
    { child_conversation_id: "", title: "ignored" },
    { title: "no id" },
  ]);
  assert.equal(cards.length, 1);
  assert.equal(cards[0].child_conversation_id, "conv-1__spawn_1");
  assert.equal(cards[0].title, "research pricing");
});

test("spawnCardsForTurn: prefers persisted pointers over live slices", () => {
  const cards = spawnCardsForTurn(
    {
      spawn_children: [
        { child_conversation_id: "conv-1__spawn_1", title: "research pricing" },
      ],
    },
    "conv-1",
    [
      {
        agentKey: "core.default.spawn-1",
        agentName: "Sub-agent 1",
        text: "live preview",
        thinkingText: "",
        thinkingComplete: true,
        isPrimary: false,
      },
    ]
  );
  assert.equal(cards.length, 1);
  assert.equal(cards[0].child_conversation_id, "conv-1__spawn_1");
  assert.equal(cards[0].title, "research pricing");
  assert.equal(cards[0].preview, undefined);
});

test("spawnCardsForTurn: uses childConversationId from a live slice", () => {
  const cards = spawnCardsForTurn(
    {},
    "conv-1",
    [
      {
        agentKey: "core.default.spawn-1",
        agentName: "Sub-agent 1",
        text: "Price is $12.",
        thinkingText: "",
        thinkingComplete: true,
        isPrimary: false,
        childConversationId: "iso-abc",
      },
    ]
  );
  assert.equal(cards.length, 1);
  assert.equal(cards[0].child_conversation_id, "iso-abc");
  assert.equal(cards[0].agent_id, "core.default.spawn-1");
  assert.equal(cards[0].preview, "Price is $12.");
});

test("mapHistoryToMessages: copies spawn_children onto the parent bubble", () => {
  const mapped = mapHistoryToMessages(
    [
      { role: "user", content: "fan out" },
      {
        role: "assistant",
        content: "here is the synthesis",
        agent_id: "core.default",
        spawn_children: [
          {
            child_conversation_id: "conv-1__spawn_1",
            agent_id: "core.default.spawn-1",
            title: "research pricing",
            thinking_text: "Look up the list price.",
            tool_summaries: [{ tool_name: "core.web_search", status: "success", preview: "list price", step: 1 }],
            cost_usd: 0.004,
          },
        ],
      },
    ],
    "core.default"
  );
  assert.equal(mapped.length, 2);
  const cards = mapped[1].meta?.spawn_children as Array<{ child_conversation_id: string; title: string }>;
  assert.equal(cards[0].child_conversation_id, "conv-1__spawn_1");
  assert.equal(cards[0].title, "research pricing");
  const streams = mapped[1].meta?.agentStreams as Record<
    string,
    { thinkingText?: string; toolSummaries?: Array<{ tool_name: string }>; costUsd?: number; childConversationId?: string }
  >;
  assert.equal(streams["core.default.spawn-1"].thinkingText, "Look up the list price.");
  assert.equal(streams["core.default.spawn-1"].toolSummaries?.[0].tool_name, "core.web_search");
  assert.equal(streams["core.default.spawn-1"].costUsd, 0.004);
  assert.equal(streams["core.default.spawn-1"].childConversationId, "conv-1__spawn_1");
});

test("projectLiveSpawnChildMessage: one agent slice from the live parent turn", () => {
  const projected = projectLiveSpawnChildMessage(
    {
      role: "assistant",
      content: "parent plus child",
      status: "updating",
      meta: {
        spawn_children: [
          { child_conversation_id: "iso-abc", agent_id: "core.default.spawn-1", title: "research" },
        ],
        agentStreams: {
          "core.default": { contentText: "working" },
          "core.default.spawn-1": {
            contentText: "price is 12",
            thinkingText: "look it up",
            thinkingComplete: false,
            toolExecutions: [{ tool_name: "core.web_search", status: "executing" }],
          },
        },
      },
    },
    "iso-abc"
  );
  assert.ok(projected);
  assert.equal(projected?.content, "price is 12");
  assert.equal(projected?.status, "updating");
  const streams = projected?.meta.agentStreams as Record<string, { thinkingText?: string; childConversationId?: string }>;
  assert.equal(streams["core.default"], undefined);
  assert.equal(streams["core.default.spawn-1"].thinkingText, "look it up");
  assert.equal(streams["core.default.spawn-1"].childConversationId, "iso-abc");
});

test("projectLiveSpawnChildMessage: unknown child is null", () => {
  assert.equal(
    projectLiveSpawnChildMessage(
      {
        meta: {
          spawn_children: [{ child_conversation_id: "iso-abc", agent_id: "core.default.spawn-1" }],
        },
      },
      "iso-other"
    ),
    null
  );
});

test("resolveDisplayedLiveMessage: parent stream stays on the parent chat", () => {
  const live = { role: "assistant", content: "parent", meta: { agentStreams: { "core.default.spawn-1": {} } } };
  assert.equal(resolveDisplayedLiveMessage(live, "conv-parent", "conv-parent"), live);
});

test("resolveDisplayedLiveMessage: spawn child still projects from the parent stream", () => {
  const live = {
    role: "assistant",
    content: "parent plus child",
    status: "updating",
    meta: {
      spawn_children: [
        { child_conversation_id: "iso-abc", agent_id: "core.default.spawn-1", title: "research" },
      ],
      agentStreams: {
        "core.default.spawn-1": { contentText: "price is 12" },
      },
    },
  };
  const shown = resolveDisplayedLiveMessage(live, "iso-abc", "conv-parent", { role: "assistant", content: "" });
  assert.equal(shown?.content, "price is 12");
});

test("resolveDisplayedLiveMessage: missing display or stream id is not the parent turn", () => {
  const live = { role: "assistant", content: "parent" };
  assert.equal(resolveDisplayedLiveMessage(live, "", "conv-parent"), null);
  assert.equal(resolveDisplayedLiveMessage(live, "conv-parent", ""), null);
});

test("resolveDisplayedLiveMessage: unrelated chat keeps its own origin message", () => {
  const live = {
    role: "assistant",
    content: "parent",
    meta: { agentStreams: { "core.default.spawn-1": { contentText: "leaked" } } },
  };
  const origin = { role: "assistant", content: "other chat" };
  assert.equal(resolveDisplayedLiveMessage(live, "conv-other", "conv-parent", origin), origin);
  assert.equal(resolveDisplayedLiveMessage(live, "conv-other", "conv-parent"), null);
});
