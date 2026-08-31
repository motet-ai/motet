/**
 * Motet - Chat Explorer - Shared Types
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-29
 *
 * Description:
 *     Type definitions for the Chat Explorer frontend.
 *     Re-exports shared types from @motet/ui-common and adds chat-specific types.
 */

// Re-export shared types from @motet/ui-common
export type { AuthState, SSEvent } from "@motet/ui-common";
export { defaultAuthState } from "@motet/ui-common";

// Re-export SSE protocol types from @motet/ui-common
export type {
  ReasoningStepEvent,
  WorkflowStepEvent,
  AgentStreamSlice,
  AgentReasoningPanel,
} from "@motet/ui-common";
export { DEFAULT_STREAM_AGENT_KEY } from "@motet/ui-common";

// Re-export attachment types from @motet/ui-common
export type { DraftUploadItem } from "@motet/ui-common";

// Re-export request override types from @motet/ui-common
export type { Overrides, ReasoningEffort } from "@motet/ui-common";
export type { ArtifactRagScope, RagControlsValue } from "@motet/ui-common";
export type { CostDisplayPrefs } from "../utils/costDisplay";
