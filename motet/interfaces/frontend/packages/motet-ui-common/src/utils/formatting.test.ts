import assert from "node:assert/strict";
import { test } from "node:test";
import {
  formatCostUsd,
  groupToolSummariesIntoSteps,
  isConductorSidebarThought,
  knownCostUsd,
  positiveLoopStep,
  stepsFromAgentStreamSlice,
  sumKnownCostUsd,
  toolExecutionsToSummaries,
  toolSummaryStatusLines,
} from "./formatting";

test("toolSummaryStatusLines: success includes preview", () => {
  assert.deepEqual(
    toolSummaryStatusLines({
      tool_name: "core.browse_page",
      status: "success",
      preview: "CNN homepage",
    }),
    ["✅ core.browse_page completed\nCNN homepage"]
  );
});

test("toolSummaryStatusLines: success includes duration", () => {
  assert.deepEqual(
    toolSummaryStatusLines({
      tool_name: "core.http_get_browser",
      status: "success",
      duration_ms: 2364,
    }),
    ["✅ core.http_get_browser completed (2364ms)"]
  );
});

test("isConductorSidebarThought: drops loop conductor lines", () => {
  assert.equal(isConductorSidebarThought("Starting agentic loop iteration 2"), true);
  assert.equal(isConductorSidebarThought("LLM decided to use: core.tool_call"), true);
  assert.equal(isConductorSidebarThought("LLM provided final response"), true);
  assert.equal(isConductorSidebarThought("✅ core.http_get_browser completed (2364ms)"), false);
});

test("positiveLoopStep: only finite values above zero", () => {
  assert.equal(positiveLoopStep(2), 2);
  assert.equal(positiveLoopStep("3"), 3);
  assert.equal(positiveLoopStep(0), undefined);
  assert.equal(positiveLoopStep(-1), undefined);
  assert.equal(positiveLoopStep(undefined), undefined);
});

test("knownCostUsd: absent and zero stay unknown", () => {
  assert.equal(knownCostUsd(undefined), null);
  assert.equal(knownCostUsd(0), null);
  assert.equal(knownCostUsd(0.0124), 0.0124);
  assert.equal(formatCostUsd(0), null);
  assert.equal(formatCostUsd(0.0124), "$0.0124");
  assert.equal(sumKnownCostUsd([0.01, undefined, 0.02]), 0.03);
  assert.equal(sumKnownCostUsd([0, null]), null);
});

test("groupToolSummariesIntoSteps: uses stored step, else index", () => {
  const grouped = groupToolSummariesIntoSteps([
    { tool_name: "core.browse_page", status: "success", preview: "CNN homepage", step: 1 },
    { tool_name: "core.web_search", status: "success", step: 1 },
    { tool_name: "core.http_get", status: "error", preview: "timeout" },
  ]);
  assert.equal(grouped.length, 2);
  assert.equal(grouped[0].step, 1);
  assert.equal(grouped[0].lines.length, 2);
  assert.match(grouped[0].lines[0], /browse_page/);
  assert.equal(grouped[1].step, 3);
  assert.match(grouped[1].lines[0], /http_get/);
});

test("stepsFromAgentStreamSlice: live toolExecutions rebuild steps when summaries are absent", () => {
  const fromLive = stepsFromAgentStreamSlice({
    toolExecutions: [
      { toolName: "core.web_search", status: "completed", preview: "list price", durationMs: 120, step: 2 },
      { toolName: "core.browse_page", status: "completed", step: 2 },
    ],
  });
  assert.equal(fromLive.length, 1);
  assert.equal(fromLive[0].step, 2);
  assert.equal(fromLive[0].lines.length, 2);
  assert.match(fromLive[0].lines[0], /web_search/);
  assert.match(fromLive[0].lines[0], /list price/);
  assert.equal(
    toolExecutionsToSummaries([{ toolName: "core.web_search", status: "completed", step: 2 }])[0].step,
    2
  );
});

test("stepsFromAgentStreamSlice: persisted summaries win over live executions", () => {
  const grouped = stepsFromAgentStreamSlice({
    toolSummaries: [{ tool_name: "core.browse_page", status: "success", step: 2 }],
    toolExecutions: [{ toolName: "core.web_search", status: "completed" }],
  });
  assert.equal(grouped.length, 1);
  assert.equal(grouped[0].step, 2);
  assert.match(grouped[0].lines[0], /browse_page/);
});
