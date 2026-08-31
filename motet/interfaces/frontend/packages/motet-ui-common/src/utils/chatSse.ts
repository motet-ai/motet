/**
 * Motet UI Common - Chat SSE Consumer
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-30
 *
 * Description:
 *     Reads a fetch Response body as Motet chat SSE and yields complete
 *     events. Keeps a remainder buffer so a token split across chunks is
 *     not parsed twice or dropped.
 *
 * Dependencies:
 *     - parseSseBuffer for event framing
 *
 * Usage:
 *     const res = await fetch("/api/v1/chat", { method: "POST", ... });
 *     await consumeChatSse(res, (evt) => turns.applyChunk(cid, evt), signal);
 *
 * Notes:
 *     - Incomplete trailing bytes are flushed with a terminator on close
 */

import type { SSEvent } from "../types";

/** Parses a Server-Sent Events buffer into structured events. */
export function parseSseBuffer(buffer: string): SSEvent[] {
  const out: SSEvent[] = [];
  const blocks = buffer.split("\n\n");

  for (const block of blocks) {
    if (!block.trim()) continue;

    const lines = block.split("\n");
    let evt = "message";
    const dataLines: string[] = [];

    for (const line of lines) {
      if (line.startsWith("event:")) {
        evt = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }

    const dataRaw = dataLines.join("\n");
    let data: unknown = dataRaw;
    try {
      data = dataRaw ? JSON.parse(dataRaw) : dataRaw;
    } catch {
      data = dataRaw;
    }

    out.push({ event: evt, data });
  }

  return out;
}

export async function consumeChatSse(
  response: Response,
  onEvent: (event: SSEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      if (signal?.aborted) break;
      const { done, value } = await reader.read();
      if (done) {
        const tail = buffer.trim()
          ? parseSseBuffer(buffer.endsWith("\n\n") ? buffer : `${buffer}\n\n`)
          : [];
        for (const evt of tail) onEvent(evt);
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const block of parts) {
        if (!block.trim()) continue;
        for (const evt of parseSseBuffer(`${block}\n\n`)) {
          onEvent(evt);
        }
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // Reader may already be released after abort.
    }
  }
}
