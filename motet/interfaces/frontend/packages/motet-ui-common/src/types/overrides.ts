/**
 * Motet UI Common - Request Override Types
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Type definitions for model and behavior overrides that can be passed
 *     to the Motet chat API. Used by useRequestContext and chat providers.
 *
 * Dependencies:
 *     - None (types-only module)
 *
 * Usage:
 *     import { Overrides, ReasoningEffort } from "@motet/ui-common";
 */

/**
 * Reasoning effort when extended thinking is enabled, ordered cheapest to deepest.
 *
 * Mirrors the backend canonical ladder. Providers support different subsets and the
 * backend adapters clamp per provider (e.g. OpenAI max only on gpt-5.6 Responses; xAI has
 * no "max"; Kimi K3 requires it), so every rung is selectable here regardless of model.
 */
export type ReasoningEffort = "low" | "medium" | "high" | "xhigh" | "max";

/**
 * Model and behavior overrides that can be passed to the chat API.
 */
export type Overrides = {
  /** Override the model provider (e.g., "openai", "anthropic") */
  model_provider?: string;
  /** Override the model name (e.g., "gpt-4", "claude-3") */
  model_name?: string;
  /** Optional model profile name for routing/policy overrides */
  model_profile_name?: string;
  /** Enable extended thinking/reasoning (provider summaries) for capable models (e.g. o-series, gpt-5) */
  enable_thinking?: boolean;
  /** Reasoning effort when enable_thinking is true: low, medium, high, xhigh, or max (Kimi K3) */
  reasoning_effort?: ReasoningEffort;
};
