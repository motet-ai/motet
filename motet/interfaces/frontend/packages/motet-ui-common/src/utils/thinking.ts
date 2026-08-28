/**
 * Motet UI Common - Thinking / Reasoning Helpers
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Helpers for models where extended thinking is always on (e.g. Kimi K3
 *     when explicitly selected, Meta Muse Spark). Used by chat settings UI to
 *     lock the thinking toggle and effort control. Stack auto default is OpenAI
 *     and is not always-on.
 *
 * Usage:
 *     import { isAlwaysOnThinkingModel, treatsThinkingAsAlwaysOn } from "@motet/ui-common";
 */

/**
 * True when the selected provider/model always runs with thinking enabled
 * (API does not support disabling thinking).
 */
export function isAlwaysOnThinkingModel(
  provider?: string | null,
  modelName?: string | null,
): boolean {
  const p = String(provider || "").trim().toLowerCase();
  const n = String(modelName || "").trim().toLowerCase();
  if (p === "moonshot" && n === "kimi-k3") {
    return true;
  }
  // Muse Spark rejects reasoning.effort=none (HTTP 400).
  return p === "meta" && n.startsWith("muse-spark-");
}

/**
 * True when the chat UI should treat thinking as always on for the selection.
 * "Auto" (empty provider/name) follows the cheaper stack default (OpenAI) and
 * is not always-on; only explicit always-on models (e.g. Kimi K3) lock the UI.
 */
export function treatsThinkingAsAlwaysOn(
  provider?: string | null,
  modelName?: string | null,
): boolean {
  return isAlwaysOnThinkingModel(provider, modelName);
}
