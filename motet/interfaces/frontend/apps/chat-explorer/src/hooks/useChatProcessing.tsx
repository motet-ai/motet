/**
 * Motet - Chat Explorer - Chat Processing Hook
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Processes streaming chat message metadata into Reasoning sidebar panels.
 *     When SSE frames include agent_id, reasoning/steps are
 *     accumulated per qualified agent id; otherwise a single __default__ bucket is used.
 *     Tool and workflow status lines update in place (executing → completed).
 *
 * Dependencies:
 *     - react: useState, useRef, useEffect
 *     - @ant-design/x: ThoughtChainItemType
 *
 * Notes:
 *     - Per-conversation accumulators: switching chats saves and restores
 *       the live Step N grouping instead of replaying a flat tool list
 *     - Panels come from this conversation's message streams only — leftover
 *       accumulators from another chat are not listed
 *     - After a switch, rebuild steps from toolSummaries or live toolExecutions
 *     - Thinking stays in the chat bubble (parent) or spawn card (child);
 *       the parent rail never renders Think. An agent is listed when
 *       thinking starts, when it has tool or workflow steps, or when a
 *       priced cost arrives. Thinking text stays in the bubble or card.
 *     - Final meta frame must merge into accumulators before building panels
 *     - Tool/workflow status upserts by identity so completed replaces executing
 *     - Sidebar steps are newest-first (live and restored)
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
  stepsFromAgentStreamSlice,
  isConductorSidebarThought,
  knownCostUsd,
  positiveLoopStep,
} from "@motet/ui-common";
import { includeReasoningPanel, sliceHasThinking, sliceIsThinkingActive } from "./reasoningPanelInclude";

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

type LineSlot = { stepNum: number; line: string };

type PerAgentAccum = {
  stepLines: Map<number, string[]>;
  stepLineSets: Map<number, Set<string>>;
  currentStep: number | null;
  processedReasoningSteps: Set<string>;
  /** Tool/workflow identity → current status line (completed replaces executing). */
  lineBySlot: Map<string, LineSlot>;
};

function createEmptyAccum(): PerAgentAccum {
  return {
    stepLines: new Map(),
    stepLineSets: new Map(),
    currentStep: null,
    processedReasoningSteps: new Set(),
    lineBySlot: new Map(),
  };
}

function appendStepLine(accum: PerAgentAccum, stepNum: number, line: string): void {
  const trimmed = (line || "").trim();
  if (!trimmed) return;
  const seen = accum.stepLineSets.get(stepNum);
  if (seen?.has(trimmed)) return;
  if (!seen) accum.stepLineSets.set(stepNum, new Set([trimmed]));
  else seen.add(trimmed);
  accum.stepLines.set(stepNum, [...(accum.stepLines.get(stepNum) || []), trimmed]);
}

function upsertStepLine(
  accum: PerAgentAccum,
  stepNum: number,
  slotKey: string,
  line: string
): void {
  const trimmed = (line || "").trim();
  if (!trimmed) return;
  const existing = accum.lineBySlot.get(slotKey);
  if (existing?.line === trimmed) return;
  if (existing) {
    const lines = accum.stepLines.get(existing.stepNum) || [];
    const idx = lines.lastIndexOf(existing.line);
    if (idx >= 0) {
      const next = [...lines];
      next[idx] = trimmed;
      accum.stepLines.set(existing.stepNum, next);
      const seen = accum.stepLineSets.get(existing.stepNum);
      if (seen) {
        seen.delete(existing.line);
        seen.add(trimmed);
      }
      accum.lineBySlot.set(slotKey, { stepNum: existing.stepNum, line: trimmed });
      return;
    }
  }
  appendStepLine(accum, stepNum, trimmed);
  accum.lineBySlot.set(slotKey, { stepNum, line: trimmed });
}

function applyStatusLine(
  accum: PerAgentAccum,
  slotKey: string,
  name: string,
  status: string,
  opts: { durationMs?: number; error?: string; stepNum?: number } = {}
): void {
  const line = formatExecutionStatusLine(name, status, opts);
  if (line) upsertStepLine(accum, opts.stepNum ?? accum.currentStep ?? 1, slotKey, line);
}

/** Extract per-agent stream map from message meta. */
function streamsFromMeta(meta: Record<string, unknown> | undefined): Record<string, AgentStreamSlice> {
  const raw = meta?.agentStreams as Record<string, AgentStreamSlice> | undefined;
  if (raw && Object.keys(raw).length > 0) {
    return raw;
  }
  return { [DEFAULT_STREAM_AGENT_KEY]: {} };
}

function thoughtChainItemsFromSteps(
  aid: string,
  steps: Array<{ step: number; lines: string[] }>,
  activeStep: number | null = null,
  thinkingActive = false
): ThoughtChainItemType[] {
  return [...steps]
    .sort((a, b) => b.step - a.step)
    .map(({ step, lines }) => ({
      key: `${aid}:step:${step}`,
      title: `Step ${step}:`,
      description: lines.map((line, idx) => (
        <div key={`${aid}:${step}:${idx}`}>{line}</div>
      )),
      // A live think must not flip the step to loading — ThoughtChain
      // replaces the description with a spinner when status is loading.
      status: !thinkingActive && activeStep === step ? "loading" : "success",
    }));
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

  if (slice.reasoning_step) {
    const rs = slice.reasoning_step as ReasoningStepEvent;
    const rsSig = JSON.stringify(rs);
    const rsKey = typeof rs?.step === "number" ? `reasoning:${rs.step}:${rsSig}` : `reasoning:auto:${rsSig}`;
    if (!accum.processedReasoningSteps.has(rsKey)) {
      accum.processedReasoningSteps.add(rsKey);
      const stepNum = positiveLoopStep(rs?.step);
      const resolvedStep = stepNum ?? (accum.currentStep ?? 0) + 1;
      accum.currentStep = resolvedStep;
      if (rs?.thought && !isConductorSidebarThought(rs.thought)) {
        appendStepLine(accum, resolvedStep, rs.thought);
      }
    }
  }

  if (slice.workflow_step) {
    const ws = slice.workflow_step as WorkflowStepEvent;
    const stepId = ws?.step_id || ws?.step_name || ws?.trace_id || "workflow";
    applyStatusLine(
      accum,
      `workflow:${stepId}`,
      ws?.step_name || ws?.command_type || "workflow_step",
      (ws?.status || "").toLowerCase(),
      { durationMs: ws?.duration_ms, error: ws?.error }
    );
  }

  if (slice.toolExecutions && Array.isArray(slice.toolExecutions)) {
    for (const toolExec of slice.toolExecutions) {
      const toolName = toolExec?.toolName || "unknown tool";
      applyStatusLine(
        accum,
        `tool:${toolExec?.toolCallId || toolName}`,
        toolName,
        toolExec?.status || "",
        {
          durationMs: toolExec?.durationMs,
          error: toolExec?.error,
          stepNum: positiveLoopStep(toolExec?.step),
        }
      );
    }
  }

  if (slice.step !== undefined) {
    appendStepLine(accum, accum.currentStep ?? 1, `Step event: ${JSON.stringify(slice.step)}`);
  }
}

type ConvProcessState = {
  byAgent: Map<string, PerAgentAccum>;
  activeAssistantMsgId: string | null;
  lastUserTurnKey: string | null;
};

function unwrapMsg(raw: unknown): { info: MsgInfo; msg: NonNullable<MsgInfo["message"]> } {
  const info = raw as MsgInfo;
  return { info, msg: (info?.message || info) as NonNullable<MsgInfo["message"]> };
}

function findLastMessage(
  messages: unknown[],
  match: (info: MsgInfo, msg: NonNullable<MsgInfo["message"]>) => boolean
): MsgInfo | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const { info, msg } = unwrapMsg(messages[i]);
    if (match(info, msg)) return info;
  }
  return undefined;
}

function isStreamingStatus(status: unknown): boolean {
  return status === "updating" || status === "loading";
}

function stepsFromAccum(accum: PerAgentAccum): Array<{ step: number; lines: string[] }> {
  return Array.from(accum.stepLines.entries()).map(([step, lines]) => ({ step, lines }));
}

function panelForAgent(
  aid: string,
  slice: AgentStreamSlice,
  steps: Array<{ step: number; lines: string[] }>,
  agents: Array<{ qualified_id: string; display_name?: string }>,
  liveActive: number | null = null,
  turnLive = false
): AgentReasoningPanel {
  const { agentName, displayLabel } = resolveAgentDisplayName(aid, agents);
  const thinkingActive = sliceIsThinkingActive(slice, turnLive);
  const fromAccum = steps.some((step) => step.lines.length > 0);
  const panelSteps = fromAccum ? steps : stepsFromAgentStreamSlice(slice);
  return {
    agentKey: aid,
    displayLabel,
    agentName,
    thoughtChainItems: thoughtChainItemsFromSteps(aid, panelSteps, liveActive, thinkingActive),
    thinkingText: null,
    thinkingComplete: !thinkingActive,
    thinkingStarted: sliceHasThinking(slice),
    thinkingActive,
    costUsd: knownCostUsd(slice.costUsd),
  };
}

export function useChatProcessing(
  throttledMessages: unknown[],
  activeConversationKey: string,
  availableAgents: Array<{ qualified_id: string; display_name?: string }> = [],
  storeReadyKey: string | null = null
): {
  reasoningPanels: AgentReasoningPanel[];
  thinkingState: string | null;
} {
  const [reasoningPanels, setReasoningPanels] = useState<AgentReasoningPanel[]>([]);
  const [thinkingState, setThinkingState] = useState<string | null>(null);

  const stateByConvRef = useRef<Map<string, ConvProcessState>>(new Map());
  const byAgentAccumRef = useRef<Map<string, PerAgentAccum>>(new Map());
  const activeAssistantMsgIdRef = useRef<string | null>(null);
  const lastUserTurnKeyRef = useRef<string | null>(null);
  const lastConversationKeyRef = useRef<string | null>(null);
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
    if (lastConversationKeyRef.current !== activeConversationKey) {
      const prev = lastConversationKeyRef.current;
      if (prev) {
        stateByConvRef.current.set(prev, {
          byAgent: byAgentAccumRef.current,
          activeAssistantMsgId: activeAssistantMsgIdRef.current,
          lastUserTurnKey: lastUserTurnKeyRef.current,
        });
      }
      lastConversationKeyRef.current = activeConversationKey;
      const saved = stateByConvRef.current.get(activeConversationKey);
      if (saved) {
        byAgentAccumRef.current = saved.byAgent;
        activeAssistantMsgIdRef.current = saved.activeAssistantMsgId;
        lastUserTurnKeyRef.current = saved.lastUserTurnKey;
      } else {
        byAgentAccumRef.current = new Map();
        activeAssistantMsgIdRef.current = null;
        lastUserTurnKeyRef.current = null;
      }
      setReasoningPanels([]);
      setThinkingState(null);
    }

    if (storeReadyKey != null && storeReadyKey !== activeConversationKey) {
      return;
    }

    const agents = availableAgentsRef.current;
    const latestUser = findLastMessage(throttledMessages, (_info, msg) => msg.role === "user");
    if (latestUser) {
      const { info, msg } = unwrapMsg(latestUser);
      const turnKey = String(
        info.id || msg.id || `${activeConversationKey}:user:${throttledMessages.lastIndexOf(latestUser)}`
      );
      if (lastUserTurnKeyRef.current == null) {
        lastUserTurnKeyRef.current = turnKey;
      } else if (turnKey !== lastUserTurnKeyRef.current) {
        lastUserTurnKeyRef.current = turnKey;
        byAgentAccumRef.current = new Map();
        activeAssistantMsgIdRef.current = null;
        setReasoningPanels([]);
        setThinkingState(null);
      }
    }

    const streamingAssistantInfo = findLastMessage(throttledMessages, (info, msg) => {
      return msg.role === "assistant" && isStreamingStatus(info.status || msg.status);
    });
    if (streamingAssistantInfo) {
      const { info, msg } = unwrapMsg(streamingAssistantInfo);
      const streamingId = info.id || msg.id || null;
      if (streamingId) activeAssistantMsgIdRef.current = String(streamingId);
    }

    const lastAssistant = findLastMessage(throttledMessages, (_info, msg) => msg.role === "assistant");
    const lastAssistantMeta = lastAssistant ? unwrapMsg(lastAssistant).msg.meta : undefined;

    if (!streamingAssistantInfo && !activeAssistantMsgIdRef.current) {
      const hasLiveAccum = Array.from(byAgentAccumRef.current.values()).some(
        (accum) => accum.stepLines.size > 0
      );
      if (hasLiveAccum) {
        const streams = streamsFromMeta(lastAssistantMeta);
        const panelsAcc: AgentReasoningPanel[] = [];
        for (const aid of Object.keys(streams)) {
          const accum = byAgentAccumRef.current.get(aid);
          const slice = streams[aid] || {};
          const steps = accum ? stepsFromAccum(accum) : [];
          if (!includeReasoningPanel(aid, slice, steps)) {
            continue;
          }
          panelsAcc.push(panelForAgent(aid, slice, steps, agents));
        }
        setReasoningPanels(panelsAcc);
        return;
      }
      const restoredStreams = streamsFromMeta(lastAssistantMeta);
      const restoredKeys = Object.keys(restoredStreams).filter((aid) => {
        const slice = restoredStreams[aid] || {};
        return includeReasoningPanel(aid, slice, stepsFromAgentStreamSlice(slice));
      });
      if (!lastAssistantMeta || restoredKeys.length === 0) {
        setReasoningPanels([]);
        return;
      }
      setReasoningPanels(
        restoredKeys.map((aid) => {
          const slice = restoredStreams[aid] || {};
          return panelForAgent(aid, slice, stepsFromAgentStreamSlice(slice), agents);
        })
      );
      return;
    }

    const trackedId = activeAssistantMsgIdRef.current;
    const activeInfo =
      streamingAssistantInfo ||
      (trackedId
        ? findLastMessage(throttledMessages, (info, msg) => {
            const id = info.id || msg.id;
            return msg.role === "assistant" && !!id && String(id) === trackedId;
          })
        : undefined);

    const activeMsg = activeInfo ? unwrapMsg(activeInfo).msg : undefined;
    const activeStatus = activeInfo ? (activeInfo.status || activeMsg?.status) : null;
    const meta = activeMsg?.meta;

    if (meta?.thinkingState) {
      setThinkingState(meta.thinkingState as string);
    } else if (!isStreamingStatus(activeStatus)) {
      setThinkingState(null);
    }

    const streams = streamsFromMeta(meta);
    const agentKeys = Object.keys(streams);

    if (!meta && !streamingAssistantInfo) {
      return;
    }

    if (meta) {
      for (const aid of agentKeys) {
        applySliceToAccum(aid, streams[aid] || {}, getOrCreateAccum);
      }
    }

    const live = isStreamingStatus(activeStatus);
    setReasoningPanels(
      agentKeys.flatMap((aid) => {
        const slice = streams[aid] || {};
        const accum = getOrCreateAccum(aid);
        const steps = stepsFromAccum(accum);
        if (!includeReasoningPanel(aid, slice, steps)) {
          return [];
        }
        return [
          panelForAgent(
            aid,
            slice,
            steps,
            agents,
            live ? accum.currentStep : null,
            live
          ),
        ];
      })
    );

    if (!streamingAssistantInfo && trackedId && (activeStatus === "success" || activeStatus === "error")) {
      activeAssistantMsgIdRef.current = null;
    }
  }, [throttledMessages, activeConversationKey, storeReadyKey]);

  return {
    reasoningPanels,
    thinkingState
  };
}
