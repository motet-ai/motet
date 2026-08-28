/**
 * Motet - Admin Dashboard - Task Flow Shared Types and Helpers
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Shared types, API fetch, and color/label helpers for task flow visualization.
 *     Used by TaskFlowPage (popup route) and TaskFlowGraph (React Flow diagram).
 */

import { getAuthHeaders } from "../api/http";

// Task flow data from /api/v1/debug/task-flow/{task_id}
export interface TaskFlowCommand {
  command_id: string;
  command_type: string;
  created_at: string;
  executed_at?: string;
  completed_at?: string;
  status: string;
  worker_id: string;
  parent_command_id?: string;
  duration_ms?: number;
  conversation_id?: string;
  agentic_loop_iteration?: number;
  inputs?: Record<string, unknown>;
  results?: Record<string, unknown>;
}

export interface TaskFlowEvent {
  kind: string;
  timestamp: string;
  command_id?: string;
  command_type?: string;
  data?: Record<string, unknown>;
}

export interface TaskFlowSummary {
  total_duration_ms?: number;
  success_rate?: number;
  worker_distribution?: Record<string, number>;
  status?: string;
  message?: string;
}

export interface ExecutionFlowNode {
  id: string;
  type: string;
  status: string;
  worker_id?: string;
  duration_ms?: number;
  created_at?: string;
}

export interface ExecutionFlowEdge {
  source: string;
  target: string;
  type?: string;
}

export interface ExecutionFlow {
  nodes: ExecutionFlowNode[];
  edges: ExecutionFlowEdge[];
}

export interface TaskFlowData {
  task_id: string;
  total_commands: number;
  commands: TaskFlowCommand[];
  events: TaskFlowEvent[];
  summary: TaskFlowSummary;
  execution_flow?: ExecutionFlow;
}

export async function fetchTaskFlow(taskId: string): Promise<TaskFlowData | null> {
  try {
    const headers = getAuthHeaders();
    const response = await fetch(`/api/v1/debug/task-flow/${taskId}`, { headers });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

// Full (untruncated) command data from /api/v1/debug/commands/{command_id}
export interface CommandDebugData {
  command_id: string;
  metadata?: Record<string, unknown> | null;
  command_data?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
}

export async function fetchCommandDebugData(
  commandId: string
): Promise<CommandDebugData | null> {
  try {
    const headers = getAuthHeaders();
    const response = await fetch(
      `/api/v1/debug/commands/${encodeURIComponent(commandId)}`,
      { headers }
    );
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const body = (await response.json()) as { detail?: string };
        if (body?.detail) detail = body.detail;
      } catch {
        // ignore non-JSON error bodies
      }
      throw new Error(detail);
    }
    return await response.json();
  } catch (error) {
    if (error instanceof Error) throw error;
    throw new Error("Failed to load command debug data");
  }
}

export function getStatusColor(status: string): string {
  switch (status?.toLowerCase()) {
    case "completed":
    case "success":
      return "success";
    case "running":
    case "pending":
      return "processing";
    case "failed":
    case "error":
      return "error";
    default:
      return "default";
  }
}

// Command type color mapping - light mode (matching task_flow_popup.html + additional types)
export const COMMAND_TYPE_COLORS_LIGHT: Record<string, { fill: string; stroke: string; color: string }> = {
  conversation_analysis: { fill: "#007bff", stroke: "#0056b3", color: "#fff" },
  agent_turn: { fill: "#007bff", stroke: "#0056b3", color: "#fff" },
  agent: { fill: "#0d6efd", stroke: "#0a58ca", color: "#fff" },
  agentic_loop: { fill: "#0d6efd", stroke: "#0a58ca", color: "#fff" },
  model_inference: { fill: "#28a745", stroke: "#1e7e34", color: "#fff" },
  model_stream: { fill: "#20c997", stroke: "#1a9f7a", color: "#fff" },
  prepare_context: { fill: "#17a2b8", stroke: "#117a8b", color: "#fff" },
  memory_reset: { fill: "#6c757d", stroke: "#545b62", color: "#fff" },
  memory_store: { fill: "#495057", stroke: "#343a40", color: "#fff" },
  memory_recall: { fill: "#5c636a", stroke: "#495057", color: "#fff" },
  tool_execution: { fill: "#e83e8c", stroke: "#c82333", color: "#fff" },
  workflow_execution: { fill: "#ffc107", stroke: "#d39e00", color: "#000" },
  dispatch: { fill: "#6610f2", stroke: "#520dc2", color: "#fff" },
  gather: { fill: "#20c997", stroke: "#1a9f7a", color: "#fff" },
  map: { fill: "#0dcaf0", stroke: "#0aa2c0", color: "#000" },
  distributed_command: { fill: "#dc3545", stroke: "#bd2130", color: "#fff" },
  finalize_turn: { fill: "#6f42c1", stroke: "#5a2d91", color: "#fff" },
  complexity_analysis: { fill: "#198754", stroke: "#146c43", color: "#fff" },
};

export const COMMAND_TYPE_COLORS_DARK: Record<string, { fill: string; stroke: string; color: string }> = {
  conversation_analysis: { fill: "#4dabf7", stroke: "#74c0fc", color: "#000" },
  agent_turn: { fill: "#4dabf7", stroke: "#74c0fc", color: "#000" },
  agent: { fill: "#339af0", stroke: "#4dabf7", color: "#000" },
  agentic_loop: { fill: "#339af0", stroke: "#4dabf7", color: "#000" },
  model_inference: { fill: "#51cf66", stroke: "#8ce99a", color: "#000" },
  model_stream: { fill: "#38d9a9", stroke: "#63e6be", color: "#000" },
  prepare_context: { fill: "#3bc9db", stroke: "#66d9e8", color: "#000" },
  memory_reset: { fill: "#868e96", stroke: "#adb5bd", color: "#000" },
  memory_store: { fill: "#adb5bd", stroke: "#ced4da", color: "#000" },
  memory_recall: { fill: "#9ba4ad", stroke: "#b8c0c8", color: "#000" },
  tool_execution: { fill: "#f06595", stroke: "#f783ac", color: "#000" },
  workflow_execution: { fill: "#ffd43b", stroke: "#ffe066", color: "#000" },
  dispatch: { fill: "#9775fa", stroke: "#b197fc", color: "#000" },
  gather: { fill: "#38d9a9", stroke: "#63e6be", color: "#000" },
  map: { fill: "#22b8cf", stroke: "#3bc9db", color: "#000" },
  distributed_command: { fill: "#ff6b6b", stroke: "#ff8787", color: "#000" },
  finalize_turn: { fill: "#9775fa", stroke: "#b197fc", color: "#000" },
  complexity_analysis: { fill: "#40c057", stroke: "#51cf66", color: "#000" },
};

export function baseCommandType(typeStr: string | undefined): string {
  if (!typeStr || typeof typeStr !== "string") return typeStr || "unknown";
  return typeStr.replace(/^core\./, "") || typeStr;
}

export function getCommandTypeColors(isDarkMode: boolean) {
  return isDarkMode ? COMMAND_TYPE_COLORS_DARK : COMMAND_TYPE_COLORS_LIGHT;
}

function isValidString(val: unknown): val is string {
  return typeof val === "string" && val.trim() !== "" && val.toLowerCase() !== "null";
}

/** 1-based agentic-loop round, or undefined if the command was not stamped. */
export function getAgenticLoopIterationNumber(
  cmd: TaskFlowCommand | null | undefined
): number | undefined {
  const raw = cmd?.agentic_loop_iteration;
  const n =
    typeof raw === "number"
      ? raw
      : typeof raw === "string"
        ? Number.parseInt(raw, 10)
        : NaN;
  if (!Number.isInteger(n) || n < 1) return undefined;
  return n;
}

/** Compact label for a stamped agentic-loop round (e.g. "iter 3"). */
export function getAgenticLoopIterationLabel(
  cmd: TaskFlowCommand | null | undefined
): string {
  const n = getAgenticLoopIterationNumber(cmd);
  return n == null ? "" : `iter ${n}`;
}

/**
 * Build compact multi-line label content for a command graph node.
 * Order: type, detail (tool/model/workflow), duration, optional counts.
 * Loop round is shown by the iteration group box, not repeated on each node.
 */
export function getCommandNodeLabelLines(
  nodeType: string,
  cmd: TaskFlowCommand | null | undefined
): string[] {
  const baseType = baseCommandType(nodeType);
  const inProgress =
    !!cmd?.status &&
    ["running", "executing", "pending"].includes(String(cmd.status).toLowerCase());
  const duration =
    inProgress && (cmd?.duration_ms === undefined || cmd?.duration_ms === null)
      ? "TBD"
      : cmd?.duration_ms !== undefined && cmd.duration_ms !== null
        ? `${cmd.duration_ms}ms`
        : "";

  const lines: string[] = [nodeType || "unknown"];

  if (baseType === "workflow_execution") {
    const workflowName = cmd?.inputs?.workflow_name;
    if (isValidString(workflowName)) lines.push(workflowName);
  }
  if (baseType === "tool_execution") {
    const toolName =
      cmd?.inputs?.tool_name ??
      (cmd?.inputs?.parameters as Record<string, unknown> | undefined)?.tool_name;
    if (isValidString(toolName)) lines.push(toolName);
  }
  if (baseType === "model_inference" || baseType === "model_stream") {
    let modelName = "";
    const modelSettings = cmd?.inputs?.model_settings as Record<string, unknown> | undefined;
    if (modelSettings && isValidString(modelSettings.model)) modelName = modelSettings.model as string;
    else if (modelSettings && isValidString(modelSettings.model_name)) {
      modelName = modelSettings.model_name as string;
    } else if (cmd?.results && isValidString(cmd.results.model)) {
      const m = cmd.results.model as string;
      modelName = m.includes(":") ? m.split(":")[1] : m;
    }
    if (isValidString(modelName)) lines.push(modelName);
  }

  if (duration) lines.push(duration);

  const extras: string[] = [];
  if (isValidString(cmd?.worker_id)) extras.push(cmd.worker_id);
  if (cmd?.inputs?.messages && Array.isArray(cmd.inputs.messages) && cmd.inputs.messages.length > 0) {
    extras.push(`${cmd.inputs.messages.length} msgs`);
  }
  if (cmd?.inputs?.tools && Array.isArray(cmd.inputs.tools) && cmd.inputs.tools.length > 0) {
    extras.push(`${cmd.inputs.tools.length} tools`);
  }
  if (cmd?.results?.tool_calls && Array.isArray(cmd.results.tool_calls) && cmd.results.tool_calls.length > 0) {
    extras.push(`${cmd.results.tool_calls.length} calls`);
  }
  if (extras.length) lines.push(extras.join(" · "));

  return lines;
}
