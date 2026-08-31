/**
 * Motet UI Common - Chat SSE Reducer Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-29
 *
 * Description:
 *     Unit tests for reduceChatEvent cost handling on usage and end frames.
 *     Run: npx tsx --test src/api/chat.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { reduceChatEvent } from "./chat";

test("usage: stamps running cost_usd onto the agent slice", () => {
  const first = reduceChatEvent(
    { role: "assistant", content: "", status: "updating" },
    { event: "usage", data: { cost_usd: 0.0012, prompt_tokens: 80, agent_id: "core.default" } },
  );
  assert.equal(first.message.status, "updating");
  assert.equal(first.message.meta?.agentStreams["core.default"].costUsd, 0.0012);

  const second = reduceChatEvent(first.message, {
    event: "usage",
    data: { cost_usd: 0.004, prompt_tokens: 200, agent_id: "core.default" },
  });
  assert.equal(second.message.meta?.agentStreams["core.default"].costUsd, 0.004);
});

test("usage: absent or zero cost does not overwrite a known amount", () => {
  const priced = reduceChatEvent(
    { role: "assistant", content: "" },
    { event: "usage", data: { cost_usd: 0.002, agent_id: "core.default" } },
  );
  const unpriced = reduceChatEvent(priced.message, {
    event: "usage",
    data: { prompt_tokens: 10, agent_id: "core.default" },
  });
  assert.equal(unpriced.message.meta?.agentStreams["core.default"].costUsd, 0.002);

  const zero = reduceChatEvent(priced.message, {
    event: "usage",
    data: { cost_usd: 0, agent_id: "core.default" },
  });
  assert.equal(zero.message.meta?.agentStreams["core.default"].costUsd, 0.002);
});

test("reasoning_step: stamps currentStep onto later tool executions", () => {
  const stepped = reduceChatEvent(
    { role: "assistant", content: "" },
    { event: "reasoning_step", data: { step: 2, thought: "search", agent_id: "core.default" } },
  );
  assert.equal(stepped.message.meta?.agentStreams["core.default"].currentStep, 2);

  const started = reduceChatEvent(stepped.message, {
    event: "tool_execution_started",
    data: { tool_name: "core.web_search", tool_call_id: "c1", agent_id: "core.default" },
  });
  const running = started.message.meta?.agentStreams["core.default"].toolExecutions?.[0];
  assert.equal(running?.step, 2);

  const done = reduceChatEvent(started.message, {
    event: "tool_execution_completed",
    data: { tool_name: "core.web_search", status: "success", agent_id: "core.default" },
  });
  assert.equal(done.message.meta?.agentStreams["core.default"].toolExecutions?.[0].step, 2);
});

test("usage: spawn child cost stays on that agent", () => {
  const { message, agentKey } = reduceChatEvent(
    { role: "assistant", content: "" },
    {
      event: "usage",
      data: { cost_usd: 0.011, agent_id: "core.default.spawn-1", parent_agent_id: "core.default" },
    },
  );
  assert.equal(agentKey, "core.default.spawn-1");
  assert.equal(message.meta?.agentStreams["core.default.spawn-1"].costUsd, 0.011);
  assert.equal(message.meta?.agentStreams["core.default"], undefined);
});

test("reasoning_step: stamps child_conversation_id onto the spawn stream", () => {
  const { message } = reduceChatEvent(
    { role: "assistant", content: "", status: "updating" },
    {
      event: "reasoning_step",
      data: {
        agent_id: "core.default",
        spawn_children: [
          { child_conversation_id: "iso-abc", agent_id: "core.default.spawn-1", title: "research" },
        ],
      },
    },
  );
  assert.equal(
    message.meta?.agentStreams["core.default.spawn-1"].childConversationId,
    "iso-abc",
  );
  assert.equal((message.meta?.spawn_children as { child_conversation_id: string }[])[0].child_conversation_id, "iso-abc");
});
