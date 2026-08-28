/**
 * Motet UI Common - Artifact RAG Controls
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Compact retrieval controls: This chat / My files / Workspace chips, with
 *     optional file IDs and tags behind Advanced. Broader scopes send the
 *     server confirm flag automatically.
 *
 * Dependencies:
 *     - antd: Alert, Button, Flex, Input, Select, Segmented, Space, Typography
 *
 * Usage:
 *     <RagControls value={rag} onChange={setRag} artifactOptions={attached} />
 *
 * Notes:
 *     - Choosing My files or Workspace sets allowBroaderScope on the request.
 *     - Advanced stays collapsed unless IDs, tags, or a collection are already set.
 */

import { useRef, useState } from "react";
import { Alert, Button, Flex, Input, Select, Segmented, Space, Typography } from "antd";
import type { RagControlsValue, ArtifactRagScope } from "../types/rag";

const { Text } = Typography;

const scopeOptions: Array<{ label: string; value: ArtifactRagScope }> = [
  { label: "This chat", value: "conversation" },
  { label: "My files", value: "principal" },
  { label: "Workspace", value: "motet" },
];

function normalizeValues(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

export type RagArtifactOption = {
  value: string;
  label: string;
};

export type RagControlsProps = {
  value: RagControlsValue;
  onChange: (next: RagControlsValue) => void;
  disabled?: boolean;
  title?: string | null;
  showCollection?: boolean;
  /** Attached or recent artifacts offered in the Advanced file picker. */
  artifactOptions?: RagArtifactOption[];
};

export function RagControls({
  value,
  onChange,
  disabled = false,
  title = null,
  showCollection = false,
  artifactOptions = [],
}: RagControlsProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const hasAdvancedValues =
    value.artifactIds.length > 0 ||
    value.artifactTags.length > 0 ||
    Boolean(value.artifactCollectionId?.trim());
  const [advancedOpen, setAdvancedOpen] = useState(hasAdvancedValues);

  const popupContainer = () => rootRef.current || document.body;

  const update = (patch: Partial<RagControlsValue>) => {
    const next = { ...value, ...patch };
    next.allowBroaderScope = next.scope !== "conversation";
    onChange(next);
  };

  return (
    <div ref={rootRef} className="rag-controls">
      <Space orientation="vertical" size="small" style={{ width: "100%" }}>
        {title ? (
          <Text strong style={{ fontSize: 13 }}>
            {title}
          </Text>
        ) : null}

        <div>
          <Text type="secondary" style={{ display: "block", marginBottom: 6, fontSize: 12 }}>
            Search which files?
          </Text>
          <Segmented
            block
            size="small"
            disabled={disabled}
            value={value.scope}
            options={scopeOptions}
            onChange={(scope) => update({ scope: scope as ArtifactRagScope })}
          />
        </div>

        {value.scope === "principal" && (
          <Text type="secondary" style={{ fontSize: 12, lineHeight: 1.4 }}>
            Includes files you uploaded in other chats.
          </Text>
        )}
        {value.scope === "motet" && (
          <Alert
            type="warning"
            showIcon
            title="Workspace search"
            description="May include other people’s files in this environment when policy allows. Narrow with tags or specific files."
          />
        )}

        {advancedOpen && (
          <Flex gap="small" wrap="wrap">
            <div style={{ flex: "1 1 220px", minWidth: 200 }}>
              <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
                Files
              </Text>
              <Select
                mode="tags"
                size="small"
                value={value.artifactIds}
                disabled={disabled}
                placeholder={artifactOptions.length > 0 ? "Pick attached files or paste IDs" : "Optional artifact IDs"}
                tokenSeparators={[","]}
                options={artifactOptions}
                getPopupContainer={popupContainer}
                onChange={(artifactIds) => update({ artifactIds: normalizeValues(artifactIds) })}
                style={{ width: "100%" }}
              />
            </div>

            <div style={{ flex: "1 1 220px", minWidth: 200 }}>
              <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
                Tags
              </Text>
              <Select
                mode="tags"
                size="small"
                value={value.artifactTags}
                disabled={disabled}
                placeholder="policy, contract, project:alpha"
                tokenSeparators={[","]}
                getPopupContainer={popupContainer}
                onChange={(artifactTags) => update({ artifactTags: normalizeValues(artifactTags) })}
                style={{ width: "100%" }}
              />
            </div>
          </Flex>
        )}

        {advancedOpen && showCollection && (
          <div>
            <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
              Collection
            </Text>
            <Input
              size="small"
              value={value.artifactCollectionId || ""}
              disabled={disabled}
              placeholder="Optional collection id"
              onChange={(event) => update({ artifactCollectionId: event.target.value })}
            />
          </div>
        )}

        <Button
          type="link"
          size="small"
          disabled={disabled}
          style={{ paddingInline: 0, height: "auto" }}
          onClick={() => setAdvancedOpen((open) => !open)}
        >
          {advancedOpen ? "Hide advanced" : "Advanced"}
        </Button>
      </Space>
    </div>
  );
}
