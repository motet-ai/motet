/**
 * Motet UI Common - Execution Status Formatting
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-03-24
 *
 * Description:
 *     Pure formatting functions for rendering execution status lines
 *     (workflow steps, tool executions) with consistent emoji-prefixed style.
 *
 * Usage:
 *     import { formatExecutionStatusLine } from "@motet/ui-common/utils";
 *
 *     formatExecutionStatusLine("web_search", "completed", { durationMs: 320 })
 *     // → "✅ web_search completed (320ms)"
 */

/** Format a status line for an execution event (workflow_step, tool execution, etc.). */
export function formatExecutionStatusLine(
  name: string,
  status: string,
  opts: { durationMs?: number; error?: string } = {}
): string | null {
  const durationStr = opts.durationMs ? ` (${opts.durationMs}ms)` : "";
  const errorStr = opts.error ? `: ${String(opts.error).slice(0, 50)}` : "";
  switch (status) {
    case "started":
    case "running":
      return `⚙️ Executing ${name}...`;
    case "completed":
      return `✅ ${name} completed${durationStr}`;
    case "failed":
    case "error":
      return `❌ ${name} failed${errorStr}${durationStr}`;
    default:
      return null;
  }
}
