/**
 * Motet - Motet UI Common - Shared Types
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-31
 *
 * Description:
 *     Shared type definitions for Motet UI components.
 *     These types are used across hooks and components for type safety.
 *
 * Dependencies:
 *     - None (types-only module)
 *
 * Usage:
 *     import { AuthState, SSEvent } from "@motet/ui-common/types";
 */

// ─────────────────────────────────────────────────────────────────────────────
// AUTHENTICATION TYPES
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Authentication credentials state.
 * Multiple auth methods are supported with priority: JWT > Service Account > API Key > Dev Headers
 */
export type AuthState = {
  /** JWT token from SSO/Keycloak (Bearer auth) */
  jwt: string;
  /** Service account token for machine-to-machine auth */
  serviceAccountToken: string;
  /** Principal ID for dev mode (X-Principal-Id header) */
  principal: string;
  /** Tenant ID for dev mode (X-Tenant-Id header) */
  tenant: string;
  /** Comma-separated roles for dev mode (X-Roles header) */
  roles: string;
  /** API key for simple auth (X-API-Key header) */
  apiKey: string;
};

/** Default auth state for new users (no credentials, dev mode fallbacks). */
export const defaultAuthState: AuthState = {
  jwt: "",
  serviceAccountToken: "",
  principal: "demo-user",
  tenant: "default",
  roles: "",
  apiKey: ""
};

// ─────────────────────────────────────────────────────────────────────────────
// EVENT TYPES
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Parsed Server-Sent Event from SSE streams.
 */
export type SSEvent = {
  /** Event type name (e.g., "message", "event_bus") */
  event: string;
  /** Parsed event payload (JSON object or raw string) */
  data: any;
};

// ─────────────────────────────────────────────────────────────────────────────
// SSE PROTOCOL TYPES — Backend event contract shapes
// ─────────────────────────────────────────────────────────────────────────────

/** Qualified registry id from SSE; default bucket when absent. */
export const DEFAULT_STREAM_AGENT_KEY = "__default__";

/** Reasoning step event from ReAct-style reasoning. */
export type ReasoningStepEvent = {
  kind?: string;
  task_id?: string;
  trace_id?: string;
  source?: string;
  strategy?: string;
  step?: number;
  thought?: string;
  action?: string | null;
  observation?: string | null;
};

/** Workflow step event from workflow execution. */
export type WorkflowStepEvent = {
  kind?: string;
  task_id?: string;
  trace_id?: string;
  source?: string;
  workflow_id?: string;
  workflow_name?: string;
  step_id?: string;
  step_name?: string;
  command_type?: string;
  status?: string;
  duration_ms?: number;
  error?: string;
  timestamp?: string;
};

/** Short tool row stored for conversation reload (sidebar Step N). */
export type ToolSummaryRow = {
  tool_name: string;
  status: string;
  preview?: string;
  step?: number;
  duration_ms?: number;
};

/** Card pointer from a parent turn to an isolated spawn-child conversation. */
export type SpawnChildCard = {
  child_conversation_id: string;
  agent_id?: string;
  turn_agent_id?: string;
  title: string;
  preview?: string;
  cost_usd?: number;
  thinking_text?: string;
  tool_summaries?: ToolSummaryRow[];
};

/** Per-agent payload merged in chat message meta.agentStreams (task-stream attribution). */
export type AgentStreamSlice = {
  contentText?: string;
  contentComplete?: boolean;
  thinkingText?: string;
  thinkingComplete?: boolean;
  reasoning_step?: ReasoningStepEvent;
  workflow_step?: WorkflowStepEvent;
  toolExecutions?: Array<{
    toolName?: string;
    toolCallId?: string;
    status?: string;
    preview?: string;
    durationMs?: number;
    error?: string;
    startedAt?: number;
    completedAt?: number;
    /** Loop iteration when the tool started; used to rebuild Step N after a conversation switch. */
    step?: number;
  }>;
  /** Latest loop iteration from reasoning_step; copied onto new toolExecutions. */
  currentStep?: number;
  toolSummaries?: ToolSummaryRow[];
  /** Priced loop estimate for this agent; omitted when unknown. */
  costUsd?: number;
  step?: unknown;
  turn?: unknown;
  thinkingState?: string | null;
  [key: string]: unknown;
};

/** One right-sidebar column: steps and cost for a single agent id. */
export type AgentReasoningPanel = {
  agentKey: string;
  /** Short id segment (e.g. `default` for `core.default`) when no registry name */
  displayLabel: string;
  /** Human-readable name from agent registry (`display_name`) when available */
  agentName: string;
  /** Items for reasoning chain display */
  thoughtChainItems: unknown[];
  thinkingText: string | null;
  thinkingComplete: boolean;
  /** True when this agent has started thinking; the rail does not show the text. */
  thinkingStarted?: boolean;
  /** True while this agent is still thinking; rail shows a spinner, not the text. */
  thinkingActive?: boolean;
  /** Priced estimate for this agent; omitted when unknown. */
  costUsd?: number | null;
};

// ─────────────────────────────────────────────────────────────────────────────
// ATTACHMENT TYPES
// ─────────────────────────────────────────────────────────────────────────────

export type {
  AttachmentState,
  DraftUploadItem,
  PresetIcons,
  VideoDerivationStatus,
  VideoDerivationTrackStatus,
} from "./attachments";
export {
  inferPresetIcon,
  inferFileCardProps,
  initialVideoDerivationStatus,
  formatVideoDerivationProcessingStep,
  formatVideoDerivationReadyDescription,
  videoDerivationTrackFromEventStatus,
} from "./attachments";

// ─────────────────────────────────────────────────────────────────────────────
// REQUEST OVERRIDE TYPES
// ─────────────────────────────────────────────────────────────────────────────

export type { Overrides, ReasoningEffort } from "./overrides";

// ─────────────────────────────────────────────────────────────────────────────
// RAG CONTROL TYPES
// ─────────────────────────────────────────────────────────────────────────────

export type { ArtifactRagScope, RagControlsValue } from "./rag";
export {
  defaultRagControlsValue,
  ragControlsIsCustom,
  ragScopeShortLabel,
  summarizeRagControls,
} from "./rag";
