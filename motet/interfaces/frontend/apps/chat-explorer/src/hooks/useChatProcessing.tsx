/**
 * Motet - Chat Explorer - Chat Processing Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-05
 *
 * Description:
 *     Processes streaming chat message metadata into Reasoning sidebar panels.
 *     When SSE frames include agent_id, reasoning/thinking/steps are
 *     accumulated per qualified agent id; otherwise a single __default__ bucket is used.
 *
 * Dependencies:
 *     - react: useState, useRef, useEffect
 *     - @ant-design/x: ThoughtChainItemType
 *
 * Notes:
 *     - Per-agent accumulators reset on new user turn and on conversation switch
 *     - Final meta frame must merge into accumulators before building panels
 */
import { useState, useRef, useEffect } from "react";
import { type ThoughtChainItemType } from "@ant-design/x";
import {
  type ReasoningStepEvent,
  type WorkflowStepEvent,
  type AgentReasoningPanel,
  type AgentStreamSlice,
  DEFAULT_STREAM_AGENT_KEY,
  resolveAgentDisplayName,
  formatExecutionStatusLine,
} from "@motet/ui-common";

/**
 * Superset shape of the SDK's opaque message-info objects.
 * Avoids repeating verbose inline casts throughout this hook.
 */
type MsgInfo = {
  id?: string;
  role?: string;
  status?: string;
  meta?: Record<string, unknown>;
  message?: {
    id?: string;
    role?: string;
    status?: string;
    meta?: Record<string, unknown>;
  };
};

type PerAgentAccum = {
  stepLines: Map<number, string[]>;
  stepLineSets: Map<number, Set<string>>;
  currentStep: number | null;
  processedReasoningStepsRef: Set<string>;
  reasoningStepsRef: Map<string, ReasoningStepEvent>;
  workflowStepsRef: Map<string, WorkflowStepEvent>;
  fallbackStepsRef: Map<string, unknown>;
  processedToolExecutionsRef: Set<string>;
  processedWorkflowStepsRef: Set<string>;
};

function createEmptyAccum(): PerAgentAccum {
  return {
    stepLines: new Map(),
    stepLineSets: new Map(),
    currentStep: null,
    processedReasoningStepsRef: new Set(),
    reasoningStepsRef: new Map(),
    workflowStepsRef: new Map(),
    fallbackStepsRef: new Map(),
    processedToolExecutionsRef: new Set(),
    processedWorkflowStepsRef: new Set()
  };
}

/** Extract per-agent stream map from message meta. */
function streamsFromMeta(meta: Record<string, unknown> | undefined): Record<string, AgentStreamSlice> {
  const raw = meta?.agentStreams as Record<string, AgentStreamSlice> | undefined;
  if (raw && Object.keys(raw).length > 0) {
    return raw;
  }
  return { [DEFAULT_STREAM_AGENT_KEY]: {} };
}

/**
 * Merge one agent's latest slice into that agent's step-line accumulators (deduped).
 * Must run on every meta update including the final frame when status → success, or the
 * last reasoning_step / workflow lines never land in the sidebar.
 */
function applySliceToAccum(
  aid: string,
  slice: AgentStreamSlice,
  getOrCreateAccum: (id: string) => PerAgentAccum
): void {
  const accum = getOrCreateAccum(aid);

  const addLine = (stepNum: number, line: string) => {
    const trimmed = (line || "").trim();
    if (!trimmed) return;
    const seen = accum.stepLineSets.get(stepNum);
    if (seen?.has(trimmed)) return;
    if (!seen) accum.stepLineSets.set(stepNum, new Set([trimmed]));
    else seen.add(trimmed);
    const prev = accum.stepLines.get(stepNum) || [];
    accum.stepLines.set(stepNum, [...prev, trimmed]);
  };

  if (slice.reasoning_step) {
    const rs = slice.reasoning_step as ReasoningStepEvent;
    const rsSig = JSON.stringify(rs);
    const rsKey = typeof rs?.step === "number" ? `reasoning:${rs.step}:${rsSig}` : `reasoning:auto:${rsSig}`;
    if (!accum.processedReasoningStepsRef.has(rsKey)) {
      accum.processedReasoningStepsRef.add(rsKey);
      const stepNum = typeof rs?.step === "number" ? rs.step : undefined;
      const resolvedStep = stepNum != null ? stepNum : (accum.currentStep ?? 0) + 1;
      accum.currentStep = resolvedStep;
      accum.reasoningStepsRef.set(`reasoning:${resolvedStep}`, rs);
      if (rs?.thought) {
        addLine(resolvedStep, rs.thought);
      }
    }
  }

  if (slice.workflow_step) {
    const ws = slice.workflow_step as WorkflowStepEvent;
    const traceId = ws?.trace_id;
    const wfKey = ws?.step_id
      ? `workflow:${ws.step_id}`
      : traceId
        ? `workflow:${traceId}`
        : `workflow:${JSON.stringify(ws)}`;
    accum.workflowStepsRef.set(wfKey, ws);
    const stepId = ws?.step_id || ws?.step_name || wfKey;
    const status = (ws?.status || "").toLowerCase();
    const wsKey = `${stepId}:${status}`;
    if (!accum.processedWorkflowStepsRef.has(wsKey)) {
      accum.processedWorkflowStepsRef.add(wsKey);
      const line = formatExecutionStatusLine(
        ws?.step_name || ws?.command_type || "workflow_step",
        status,
        { durationMs: ws?.duration_ms, error: ws?.error }
      );
      if (line) addLine(accum.currentStep ?? 1, line);
    }
  }

  if (slice.toolExecutions && Array.isArray(slice.toolExecutions)) {
    for (const toolExec of slice.toolExecutions) {
      const toolName = toolExec?.toolName || "unknown tool";
      const status = toolExec?.status || "";
      const execKey = `${toolExec?.toolCallId || toolName}:${status}`;
      if (accum.processedToolExecutionsRef.has(execKey)) continue;
      const line = formatExecutionStatusLine(toolName, status, {
        durationMs: toolExec?.durationMs,
        error: toolExec?.error,
      });
      if (line) {
        accum.processedToolExecutionsRef.add(execKey);
        addLine(accum.currentStep ?? 1, line);
      }
    }
  }

  if (slice.step !== undefined) {
    const key = `step:${JSON.stringify(slice.step)}`;
    accum.fallbackStepsRef.set(key, slice.step);
    addLine(accum.currentStep ?? 1, `Step event: ${JSON.stringify(slice.step)}`);
  }
}

export function useChatProcessing(
  throttledMessages: unknown[],
  activeConversationKey: string,
  availableAgents: Array<{ qualified_id: string; display_name?: string }> = []
): {
  reasoningPanels: AgentReasoningPanel[];
  thinkingState: string | null;
} {
  const [reasoningPanels, setReasoningPanels] = useState<AgentReasoningPanel[]>([]);
  const [thinkingState, setThinkingState] = useState<string | null>(null);

  const byAgentAccumRef = useRef<Map<string, PerAgentAccum>>(new Map());
  const activeAssistantMsgIdRef = useRef<string | null>(null);
  const lastUserTurnKeyRef = useRef<string | null>(null);
  const availableAgentsRef = useRef(availableAgents);
  availableAgentsRef.current = availableAgents;

  function getOrCreateAccum(aid: string): PerAgentAccum {
    const m = byAgentAccumRef.current;
    if (!m.has(aid)) {
      m.set(aid, createEmptyAccum());
    }
    return m.get(aid)!;
  }

  useEffect(() => {
    let latestUserInfo: MsgInfo | null = null;
    let latestUserIdx = -1;
    for (let i = throttledMessages.length - 1; i >= 0; i -= 1) {
      const msgInfo = throttledMessages[i] as MsgInfo;
      const msg = msgInfo?.message || msgInfo;
      if (msg?.role === "user") {
        latestUserInfo = msgInfo;
        latestUserIdx = i;
        break;
      }
    }
    if (!latestUserInfo) return;

    const latestUserMsg = latestUserInfo.message || latestUserInfo;
    const turnKey = String(
      latestUserInfo.id || latestUserMsg?.id || `${activeConversationKey}:user:${latestUserIdx}`
    );

    if (lastUserTurnKeyRef.current == null) {
      lastUserTurnKeyRef.current = turnKey;
      return;
    }

    if (turnKey !== lastUserTurnKeyRef.current) {
      lastUserTurnKeyRef.current = turnKey;
      byAgentAccumRef.current = new Map();
      activeAssistantMsgIdRef.current = null;
      setReasoningPanels([]);
      setThinkingState(null);
    }
  }, [throttledMessages, activeConversationKey]);

  useEffect(() => {
    const streamingAssistantInfo = [...throttledMessages].reverse().find((raw: unknown) => {
      const mi = raw as MsgInfo;
      const m = mi.message || mi;
      const st = mi.status || m?.status;
      return m?.role === "assistant" && (st === "updating" || st === "loading");
    }) as MsgInfo | undefined;

    const streamingAssistantMsg = streamingAssistantInfo?.message || streamingAssistantInfo;

    if (streamingAssistantInfo) {
      const streamingId = streamingAssistantInfo.id || streamingAssistantMsg?.id || null;
      if (streamingId) activeAssistantMsgIdRef.current = String(streamingId);
    }

    if (!streamingAssistantInfo && !activeAssistantMsgIdRef.current) {
      return;
    }

    let activeInfo: MsgInfo | undefined =
      streamingAssistantInfo ||
      (throttledMessages.find((raw: unknown) => {
        const mi = raw as MsgInfo;
        const m = mi.message || mi;
        const id = mi.id || m?.id;
        return id && activeAssistantMsgIdRef.current && String(id) === activeAssistantMsgIdRef.current;
      }) as MsgInfo | undefined);

    if (!activeInfo && activeAssistantMsgIdRef.current) {
      activeInfo = [...throttledMessages].reverse().find((raw: unknown) => {
        const mi = raw as MsgInfo;
        const m = mi.message || mi;
        const id = mi.id || m?.id;
        return m?.role === "assistant" && id && String(id) === activeAssistantMsgIdRef.current;
      }) as MsgInfo | undefined;
    }

    const activeMsg = activeInfo?.message || activeInfo;
    const activeStatus = activeInfo ? (activeInfo.status || activeMsg?.status) : null;
    const meta = activeMsg?.meta;

    if (meta?.thinkingState) {
      setThinkingState(meta.thinkingState as string);
    } else if (activeStatus !== "updating" && activeStatus !== "loading") {
      setThinkingState(null);
    }

    const streams = streamsFromMeta(meta);
    const agentKeys = Object.keys(streams);

    if (!meta && !streamingAssistantInfo) {
      return;
    }

    // Apply latest slices to accumulators first (including the success frame — the old early-return
    // completion path skipped this and dropped final reasoning_step / workflow lines).
    if (meta) {
      for (const aid of agentKeys) {
        applySliceToAccum(aid, streams[aid] || {}, getOrCreateAccum);
      }
    }

    const panelsAcc: AgentReasoningPanel[] = [];

    for (const aid of agentKeys) {
      const slice = streams[aid] || {};
      const accum = getOrCreateAccum(aid);

      const stepNums = Array.from(accum.stepLines.keys()).sort((a, b) => a - b);
      const items: ThoughtChainItemType[] = stepNums.map((n) => {
        const lines = accum.stepLines.get(n) || [];
        const isActive =
          (activeStatus === "updating" || activeStatus === "loading") && accum.currentStep === n;
        return {
          key: `${aid}:step:${n}`,
          title: `Step ${n}:`,
          description: lines.map((line, idx) => (
            <div key={`${aid}:${n}:${idx}`}>{line}</div>
          )),
          status: isActive ? "loading" : "success"
        };
      });

      const { agentName, displayLabel } = resolveAgentDisplayName(aid, availableAgentsRef.current);
      panelsAcc.push({
        agentKey: aid,
        displayLabel,
        agentName,
        thoughtChainItems: items,
        thinkingText: slice.thinkingText ?? null,
        thinkingComplete: !!slice.thinkingComplete
      });
    }

    setReasoningPanels(panelsAcc);

    if (!streamingAssistantInfo && activeAssistantMsgIdRef.current && (activeStatus === "success" || activeStatus === "error")) {
      activeAssistantMsgIdRef.current = null;
    }
  }, [throttledMessages]);

  useEffect(() => {
    byAgentAccumRef.current = new Map();
    activeAssistantMsgIdRef.current = null;
    lastUserTurnKeyRef.current = null;
    setReasoningPanels([]);
    setThinkingState(null);
  }, [activeConversationKey]);

  return {
    reasoningPanels,
    thinkingState
  };
}
