/**
 * Motet - Chat Explorer - Frontend Utilities
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-01-28
 *
 * Description:
 *     Re-exports shared utilities from @motet/ui-common and adds chat-specific utilities.
 */
import { Typography } from "antd";

// Re-export shared utilities
export { randomId, debugLog, parseSseBuffer } from "@motet/ui-common";

const { Text } = Typography;

// ─────────────────────────────────────────────────────────────────────────────
// CHAT-SPECIFIC UTILITIES
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Converts a JSON value into Ant Design Tree component data format.
 */
export function jsonToTreeData(value: unknown, keyPrefix: string, label: string): any[] {
  const asNode = (nodeLabel: string, nodeValue: unknown, pathKey: string): any => {
    const isArray = Array.isArray(nodeValue);
    const isObject = !!nodeValue && typeof nodeValue === "object" && !isArray;

    if (!isArray && !isObject) {
      const display = nodeValue === null ? "null" : String(nodeValue);
      return {
        key: pathKey,
        title: (
          <span>
            <Text strong>{nodeLabel}</Text>
            <Text type="secondary">: </Text>
            <Text>{display}</Text>
          </span>
        ),
        isLeaf: true,
      };
    }

    if (isArray) {
      const arr = nodeValue as unknown[];
      return {
        key: pathKey,
        title: (
          <span>
            <Text strong>{nodeLabel}</Text>
            <Text type="secondary">{` [${arr.length}]`}</Text>
          </span>
        ),
        children: arr.map((v, i) => asNode(String(i), v, `${pathKey}.${i}`)),
      };
    }

    const obj = nodeValue as Record<string, unknown>;
    const keys = Object.keys(obj).sort();
    return {
      key: pathKey,
      title: (
        <span>
          <Text strong>{nodeLabel}</Text>
          <Text type="secondary">{` {${keys.length}}`}</Text>
        </span>
      ),
      children: keys.map((k) => asNode(k, obj[k], `${pathKey}.${k}`)),
    };
  };

  return [asNode(label, value, keyPrefix)];
}

/**
 * Creates a display label for an event bus item.
 */
export function getEventRootLabel(item: any, idx: number): string {
  const kind = item?.kind ?? item?.event ?? item?.type ?? `event ${idx + 1}`;
  const rawTs = item?.timestamp ?? item?.started_at ?? item?.created_at ?? item?.time ?? null;
  if (!rawTs) return String(kind);

  const d = new Date(rawTs);
  const tsLabel = Number.isNaN(d.getTime()) ? String(rawTs) : d.toLocaleString();
  return `${String(kind)} • ${tsLabel}`;
}
