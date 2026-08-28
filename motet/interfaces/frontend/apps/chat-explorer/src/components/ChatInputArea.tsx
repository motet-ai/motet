/**
 * Motet - Chat Explorer - Chat Input Area
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Message composition area with file attachment, RAG context, and model controls.
 *
 *     Components:
 *     1. Model select: single searchable list of ``provider : model`` options
 *        (plus Auto). A key icon marks providers with an API key; models
 *        without a key stay listed but cannot be selected. Enable thinking
 *        and reasoning effort sit to the right.
 *
 *     2. Sender: Text input with send button
 *        - Supports Enter to send, Shift+Enter for new line
 *        - Shows loading state during request
 *        - Prefix buttons to toggle attachments and RAG context settings
 *
 *     3. Attachments Panel (collapsible):
 *        - File count display
 *        - Drop zone for drag-and-drop
 *        - File cards showing upload status
 *        - Close button to collapse panel
 *
 *     4. Retrieval popover (search icon):
 *        - This chat / My files / Workspace chips
 *        - Advanced file IDs and tags
 *        - Closed-state chip showing the current scope; clear resets to This chat
 *
 *     Guards:
 *     - Prevents sending while uploads are in progress
 *     - Prevents sending empty messages (unless attachments present)
 *     - Prevents double-submit during request
 *
 * Dependencies:
 *     - @ant-design/x: Sender, Attachments components
 *     - Ant Design: Button, Flex, message, Popover, Select, Switch, Tag, Tooltip, Typography, Badge, Space
 *     - @motet/ui-common: RagControls, rag helpers, ReasoningEffort, treatsThinkingAsAlwaysOn
 *     - @ant-design/icons: PaperClipOutlined, CloseOutlined, CloudUploadOutlined, FileSearchOutlined, KeyOutlined
 *
 * Usage:
 *     <ChatInputArea
 *       inputValue={inputValue}
 *       setInputValue={setInputValue}
 *       isRequesting={isRequesting}
 *       onSend={handleSend}
 *       showAttachments={showAttachments}
 *       // ... other props
 *     />
 *
 * Notes:
 *     - Model override is one searchable select of ``provider : model`` values
 *       below the sender. Clearing it (or choosing Auto) leaves routing to the backend.
 *     - ``has_api_key`` from ``GET /api/v1/models`` drives the key icon; models
 *       that ``requires_api_key`` and lack a key are listed but disabled.
 *     - Enable thinking sits to the right of the model select. When it is on,
 *       the reasoning-effort select appears to the right of the switch.
 */
import React, { useMemo } from "react";
import { Button, Flex, message, Popover, Select, Switch, Tag, Tooltip, Typography, Badge, Space } from "antd";
import { Attachments, Sender } from "@ant-design/x";
import { PaperClipOutlined, CloseOutlined, CloudUploadOutlined, FileSearchOutlined, KeyOutlined } from "@ant-design/icons";
import {
  RagControls,
  defaultRagControlsValue,
  ragControlsIsCustom,
  summarizeRagControls,
  treatsThinkingAsAlwaysOn,
  type RagControlsValue,
  type ReasoningEffort,
} from "@motet/ui-common";
import { randomId } from "../utils";
import { type DraftUploadItem } from "../types";

const { Text } = Typography;

const MODEL_VALUE_SEP = "\u001f";

type ModelInfo = {
  provider: string;
  name: string;
  display_name?: string;
  requires_api_key?: boolean;
  has_api_key?: boolean;
};

type ModelSelectOption = {
  value: string;
  label: React.ReactNode;
  searchLabel: string;
  disabled?: boolean;
  title?: string;
};

function modelCredentialFlags(model: ModelInfo): { showKey: boolean; disabled: boolean } {
  if (typeof model.has_api_key !== "boolean") {
    return { showKey: false, disabled: false };
  }
  const requiresKey = model.requires_api_key !== false;
  return {
    showKey: Boolean(model.has_api_key),
    disabled: requiresKey && !model.has_api_key,
  };
}

function modelOptionNode(text: string, showKey: boolean): React.ReactNode {
  if (!showKey) return text;
  return (
    <span className="model-option-label">
      <KeyOutlined className="model-option-key" aria-hidden />
      <span>{text}</span>
    </span>
  );
}

function encodeModelValue(provider: string, name: string): string {
  return `${provider}${MODEL_VALUE_SEP}${name}`;
}

function decodeModelValue(value: string): { provider: string; name: string } {
  const sep = value.indexOf(MODEL_VALUE_SEP);
  if (sep < 0) return { provider: "", name: "" };
  return { provider: value.slice(0, sep), name: value.slice(sep + 1) };
}

function modelOptionLabel(provider: string, name: string, displayName?: string): string {
  const pretty = String(displayName || "").trim();
  const modelLabel = pretty && pretty !== name ? `${pretty} (${name})` : pretty || name;
  return `${provider} : ${modelLabel}`;
}

/**
 * Props for the ChatInputArea component.
 */
interface ChatInputAreaProps {
  /** Ant Design theme token for consistent styling */
  token: any;
  /** Current input text value */
  inputValue: string;
  /** Callback to update input value */
  setInputValue: (val: string) => void;
  /** Whether a request is currently in progress */
  isRequesting: boolean;
  /** Callback to send the message */
  onSend: () => void;
  /** Whether the input is disabled (e.g., when user is not authenticated) */
  disabled?: boolean;
  
  // ─────────────────────────────────────────────────────────────────────────────
  // Attachments props
  // ─────────────────────────────────────────────────────────────────────────────
  
  /** Whether the attachments panel is visible */
  showAttachments: boolean;
  /** Callback to toggle attachments panel */
  setShowAttachments: React.Dispatch<React.SetStateAction<boolean>>;
  /** Combined list of draft and completed file cards for display */
  fileCardList: any[];
  /** In-progress upload items */
  draftUploads: DraftUploadItem[];
  /** Callback to update draft uploads */
  setDraftUploads: React.Dispatch<React.SetStateAction<DraftUploadItem[]>>;
  /** Function to upload a file and return artifact metadata */
  handleUpload: (file: File) => Promise<{ artifact_id: string; filename: string; content_type: string; bytes: number; derivations_pending: boolean }>;
  /** Ref to Attachments component for programmatic control */
  attachmentsRef: React.MutableRefObject<any>;
  /** Callback when attachments list changes (for cleanup) */
  onAttachmentsChange: (info: any) => void;
  /** Whether the RAG context panel is visible */
  showRagControls: boolean;
  /** Callback to toggle RAG context controls */
  setShowRagControls: React.Dispatch<React.SetStateAction<boolean>>;
  /** Current RAG context controls */
  ragControls: RagControlsValue;
  /** Callback to update RAG context controls */
  setRagControls: (next: RagControlsValue) => void;

  /** Available models from `/api/v1/models` */
  availableModels: ModelInfo[];
  /** Selected provider override (empty = auto) */
  selectedModelProvider: string;
  /** Selected model override (empty = auto) */
  selectedModelName: string;
  /** Set both provider and model together (empty pair = auto) */
  onSelectModel: (provider: string, modelName: string) => void;
  /** Enable extended thinking (reasoning summaries) for capable models */
  enableThinking: boolean;
  /** Callback to toggle enable thinking */
  onEnableThinking: (enabled: boolean) => void;
  /** Reasoning effort when thinking is enabled: low | medium | high | xhigh | max */
  reasoningEffort: ReasoningEffort;
  /** Callback when user changes reasoning effort */
  onReasoningEffortChange: (effort: ReasoningEffort) => void;
}

export function ChatInputArea({
  token,
  inputValue,
  setInputValue,
  isRequesting,
  onSend,
  disabled = false,
  showAttachments,
  setShowAttachments,
  fileCardList,
  draftUploads,
  setDraftUploads,
  handleUpload,
  attachmentsRef,
  onAttachmentsChange,
  showRagControls,
  setShowRagControls,
  ragControls,
  setRagControls,
  availableModels,
  selectedModelProvider,
  selectedModelName,
  onSelectModel,
  enableThinking,
  onEnableThinking,
  reasoningEffort,
  onReasoningEffortChange,
}: ChatInputAreaProps) {
  const ragControlsActive = ragControlsIsCustom(ragControls);
  const retrievalSummary = summarizeRagControls(ragControls);
  const artifactOptions = useMemo(
    () =>
      (fileCardList || [])
        .filter((item) => {
          const uid = String(item?.uid || "");
          if (!uid || uid.startsWith("temp-")) return false;
          if (item?.status === "uploading" || item?.status === "error") return false;
          return true;
        })
        .map((item) => ({
          value: String(item.uid),
          label: String(item.name || item.uid),
        })),
    [fileCardList],
  );

  const modelOptions = useMemo(() => {
    const items = (availableModels || [])
      .map((m) => {
        const provider = String(m.provider || "").trim();
        const name = String(m.name || "").trim();
        if (!provider || !name) return null;
        const text = modelOptionLabel(provider, name, m.display_name);
        const { showKey, disabled } = modelCredentialFlags(m);
        const option: ModelSelectOption = {
          value: encodeModelValue(provider, name),
          label: modelOptionNode(text, showKey),
          searchLabel: text,
          disabled,
          title: disabled ? "No API key configured for this provider" : text,
        };
        return option;
      })
      .filter((item): item is ModelSelectOption => item != null)
      .sort((a, b) => a.searchLabel.localeCompare(b.searchLabel));
    const selectedProvider = String(selectedModelProvider || "").trim();
    const selectedName = String(selectedModelName || "").trim();
    if (selectedProvider && selectedName) {
      const selectedValue = encodeModelValue(selectedProvider, selectedName);
      if (!items.some((item) => item.value === selectedValue)) {
        const text = modelOptionLabel(selectedProvider, selectedName);
        items.unshift({
          value: selectedValue,
          label: text,
          searchLabel: text,
        });
      }
    }
    return [{ label: "Auto", value: "", searchLabel: "Auto" }, ...items];
  }, [availableModels, selectedModelProvider, selectedModelName]);

  const selectedModelValue = useMemo(() => {
    const provider = String(selectedModelProvider || "").trim();
    const name = String(selectedModelName || "").trim();
    if (!provider || !name) return undefined;
    return encodeModelValue(provider, name);
  }, [selectedModelProvider, selectedModelName]);

  const thinkingAlwaysOn = useMemo(
    () => treatsThinkingAsAlwaysOn(selectedModelProvider, selectedModelName),
    [selectedModelProvider, selectedModelName],
  );
  const thinkingEnabled = thinkingAlwaysOn || enableThinking;
  const effortValue: ReasoningEffort = thinkingAlwaysOn ? "max" : reasoningEffort;
  const effortOptions = thinkingAlwaysOn
    ? [{ value: "max" as const, label: "Max (required)" }]
    : [
        { value: "low" as const, label: "Low" },
        { value: "medium" as const, label: "Medium" },
        { value: "high" as const, label: "High" },
        { value: "xhigh" as const, label: "Extra High" },
        { value: "max" as const, label: "Max" },
      ];

  /**
   * Custom upload handler for the Attachments component.
   * Handles the actual file upload to /api/v1/artifacts and updates draft state.
   */
  const handleCustomRequest = async ({ file, onProgress, onSuccess, onError }: any) => {
    const fileObj: any = file;
    const actualFile: File | undefined =
      fileObj instanceof File ? fileObj : (fileObj?.originFileObj as File | undefined);

    if (!actualFile) {
      onError?.(new Error("No file object found"));
      return;
    }

    const tempUid = `temp-${randomId()}`;
    setDraftUploads((prev) => [
      ...prev,
      {
        uid: tempUid,
        name: actualFile.name,
        size: actualFile.size,
        type: actualFile.type || "application/octet-stream",
        status: "uploading",
        percent: 0
      }
    ]);

    try {
      onProgress?.({ percent: 0 });
      setDraftUploads((prev) =>
        prev.map((d) => (d.uid === tempUid ? { ...d, percent: 0 } : d))
      );
      const uploaded = await handleUpload(actualFile);
      onProgress?.({ percent: 100 });
      setDraftUploads((prev) =>
        prev.map((d) => (d.uid === tempUid ? { ...d, percent: 100 } : d))
      );
      onSuccess?.({}, fileObj);
      setDraftUploads((prev) => prev.filter((d) => d.uid !== tempUid));
      if (uploaded.derivations_pending) {
        if (String(uploaded.content_type || "").startsWith("video/")) {
          message.success(`Uploaded ${uploaded.filename} — processing keyframes & transcript…`);
        } else {
          message.success(`Uploaded ${uploaded.filename} — processing…`);
        }
      } else {
        message.success(`Uploaded ${uploaded.filename}`);
      }
    } catch (error) {
      setDraftUploads((prev) =>
        prev.map((d) =>
          d.uid === tempUid
            ? { ...d, status: "error", error: (error as any)?.message || "Upload failed" }
            : d
        )
      );
      message.error(`Upload failed: ${(error as any)?.message || "unknown error"}`);
      onError?.(error as Error);
    }
  };

  return (
    <div className="input-area">
      {/* Collapsible Attachments panel */}
      {showAttachments && (
        <div 
          style={{ 
            padding: "12px 16px",
            background: token.colorBgElevated,
            borderRadius: 8,
            marginBottom: 8,
            border: `1px solid ${token.colorBorder}`
          }}
        >
          <Flex justify="space-between" align="center" style={{ marginBottom: 8 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {fileCardList.length > 0
                ? `${fileCardList.length} file${fileCardList.length > 1 ? 's' : ''} attached`
                : 'Attach files'
              }
            </Text>
            <Button
              type="text"
              size="small"
              icon={<CloseOutlined />}
              onClick={() => setShowAttachments(false)}
            />
          </Flex>
          <Attachments
            ref={attachmentsRef}
            items={fileCardList}
            placeholder={{
              icon: <CloudUploadOutlined style={{ fontSize: 24, color: token.colorTextSecondary }} />,
              title: "Drop files here or click to upload",
              description: "Supports images, PDFs, and documents"
            }}
            customRequest={handleCustomRequest}
            onChange={onAttachmentsChange}
            maxCount={5}
          />
        </div>
      )}
      <Sender
        value={inputValue}
        loading={isRequesting}
        disabled={disabled}
        prefix={
          <Space size={4}>
            <Badge count={fileCardList.length} size="small" offset={[0, 4]}>
              <Button
                type="text"
                icon={<PaperClipOutlined style={{ fontSize: 18, color: showAttachments ? token.colorPrimary : undefined }} />}
                onClick={() => setShowAttachments((prev) => !prev)}
                disabled={disabled}
              />
            </Badge>
            <Flex align="center" gap={4} className="retrieval-trigger">
              <Popover
                trigger="click"
                open={showRagControls}
                onOpenChange={(open) => {
                  if (!disabled) setShowRagControls(open);
                }}
                placement="topLeft"
                arrow={false}
                getPopupContainer={() => document.body}
                content={
                  <div className="retrieval-popover-content">
                    <RagControls
                      value={ragControls}
                      onChange={setRagControls}
                      disabled={disabled}
                      artifactOptions={artifactOptions}
                    />
                  </div>
                }
              >
                <Button
                  type="text"
                  title="Search which files?"
                  aria-label="Search which files"
                  icon={
                    <FileSearchOutlined
                      style={{ fontSize: 18, color: showRagControls || ragControlsActive ? token.colorPrimary : undefined }}
                    />
                  }
                  disabled={disabled}
                />
              </Popover>
              <Tag
                className={ragControlsActive ? "retrieval-chip retrieval-chip-active" : "retrieval-chip"}
                bordered={false}
                closable={ragControlsActive}
                onClose={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  setRagControls(defaultRagControlsValue);
                }}
                onClick={() => {
                  if (!disabled) setShowRagControls(true);
                }}
              >
                {retrievalSummary}
              </Tag>
            </Flex>
          </Space>
        }
        onSubmit={(value) => {
          // Block if disabled (e.g., not authenticated)
          if (disabled) return;
          // Guard against accidental double-submit
          if (isRequesting) return;
          const trimmed = String(value || "").trim();
          // Allow sending if there are attachments even if text is empty
          if (!trimmed && fileCardList.length === 0) return;
          
          if (draftUploads.some((d) => d.status === "uploading")) {
            message.warning("Please wait for uploads to finish before sending.");
            return;
          }
          
          onSend();
        }}
        onChange={(value) => setInputValue(value as string)}
        placeholder={disabled ? "Please log in to send messages..." : "Type your message... (Enter to send, Shift+Enter for new line)"}
      />
      <Flex className="input-model-row" align="center" gap={12} wrap="wrap">
        <Select
            className="input-model-select muted-until-hover-select"
          size="small"
          showSearch
          allowClear
          placeholder="Model (auto)"
          optionFilterProp="searchLabel"
          options={modelOptions}
          value={selectedModelValue}
          onChange={(value) => {
            if (!value) {
              onSelectModel("", "");
              return;
            }
            const chosen = modelOptions.find((item) => item.value === value);
            if (chosen?.disabled) return;
            const next = decodeModelValue(String(value));
            onSelectModel(next.provider, next.name);
          }}
        />
        <Flex className="input-thinking-control" align="center" gap={8}>
          <Tooltip
            title={thinkingAlwaysOn ? "Thinking is always on for Kimi K3." : undefined}
          >
            <Text className="input-thinking-label" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
              Enable thinking{thinkingAlwaysOn ? " (always on)" : ""}
            </Text>
          </Tooltip>
          <Switch
            size="small"
            checked={thinkingEnabled}
            disabled={thinkingAlwaysOn}
            onChange={onEnableThinking}
          />
        </Flex>
        {thinkingEnabled && (
          <Select
                className="input-reasoning-select muted-until-hover-select"
            size="small"
            aria-label="Reasoning effort"
            value={effortValue}
            disabled={thinkingAlwaysOn}
            options={effortOptions}
            onChange={(v) => onReasoningEffortChange((v as ReasoningEffort) || "medium")}
          />
        )}
      </Flex>
    </div>
  );
}

