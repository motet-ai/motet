/**
 * Motet - Chat Explorer - Chat Processing Tests
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Unit tests for when an agent appears on the right-rail reasoning
 *     list. Run: npx tsx --test src/hooks/useChatProcessing.test.ts
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { includeReasoningPanel, sliceHasThinking, sliceIsThinkingActive } from "./reasoningPanelInclude";

test("sliceHasThinking: first thinking frame with no text yet", () => {
  assert.equal(sliceHasThinking({ thinkingComplete: false }), true);
  assert.equal(sliceHasThinking({ thinkingText: "hmm" }), true);
  assert.equal(sliceHasThinking({}), false);
});

test("includeReasoningPanel: list an agent as soon as thinking starts", () => {
  assert.equal(
    includeReasoningPanel("expert-panel.researcher", { thinkingComplete: false }, []),
    true,
  );
  assert.equal(
    includeReasoningPanel("expert-panel.researcher", { thinkingText: "look it up" }, []),
    true,
  );
});

test("includeReasoningPanel: still list when only cost or steps exist", () => {
  assert.equal(
    includeReasoningPanel("core.default", { costUsd: 0.002 }, []),
    true,
  );
  assert.equal(
    includeReasoningPanel(
      "core.default",
      { toolSummaries: [{ tool_name: "web_search", status: "completed" }] },
      [],
    ),
    true,
  );
});

test("includeReasoningPanel: skip an empty stream with no thinking, steps, or cost", () => {
  assert.equal(includeReasoningPanel("core.default", {}, []), false);
});

test("includeReasoningPanel: spawn children appear when they start thinking", () => {
  assert.equal(
    includeReasoningPanel("core.default.spawn-1", { thinkingComplete: false }, []),
    true,
  );
});

test("sliceIsThinkingActive: spinner only while the trace is in progress", () => {
  assert.equal(sliceIsThinkingActive({ thinkingComplete: false }, true), true);
  assert.equal(sliceIsThinkingActive({ thinkingComplete: true, thinkingText: "done" }, true), false);
  assert.equal(sliceIsThinkingActive({ thinkingText: "hmm" }, false), false);
  assert.equal(sliceIsThinkingActive({ thinkingText: "hmm" }, true), true);
});
