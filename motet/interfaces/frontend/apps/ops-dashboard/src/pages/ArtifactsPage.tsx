/**
 * Motet - Ops Dashboard - Artifacts Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Tenant-scoped artifact listing with derived-text chunk indexing status and reindex.
 *     Supports multi-select checkboxes with bulk delete and bulk reindex of selected artifacts.
 */
import { useEffect, useState, type Key } from "react";
import { Typography, Card, Table, Tag, Space, Alert, Descriptions, Statistic, Button, Popconfirm, Tooltip, theme, Switch, Checkbox } from "antd";
import { message } from "../antdApp";
import { FileOutlined, DownloadOutlined, EyeOutlined, DeleteOutlined, PictureOutlined, FileTextOutlined, FileUnknownOutlined, ReloadOutlined } from "@ant-design/icons";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import { applyScopeParams, scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface ArtifactsPageProps {
  scope: Scope;
}

interface ArtifactMetadata {
  id: string;
  kind: string;
  content_type: string;
  bytes: number;
  checksum_sha256: string;
  created_at: number;
  expires_at?: number;
  source_artifact_id?: string;
  tenant_id?: string;
  principal_id?: string;
  motet_id?: string;
  metadata?: Record<string, any>;
}

interface ArtifactsResponse {
  items: ArtifactMetadata[];
  total?: number;
  limit: number;
  offset: number;
}

interface ArtifactIndexingStatusItem {
  artifact_id: string;
  artifact_rag_globally_enabled: boolean;
  index_role: string;
  source_artifact_id?: string;
  derived_text_artifact_id?: string;
  indexing_enabled: boolean;
  chunks_indexed: number;
  summary: string;
  detail?: string;
}

interface ArtifactReindexResponse {
  command_type: string;
  task_id: string;
  status: "queued" | "running" | "success" | "error";
  result?: any;
  error?: string;
}

async function fetchArtifacts(scope: Scope, kind?: string): Promise<ArtifactsResponse> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl("/api/v1/artifacts", scope, { limit: 100, kind }), { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  return response.json();
}

const INDEXING_STATUS_MAX_IDS_PER_REQUEST = 80;

async function fetchArtifactsIndexingStatus(scope: Scope, artifactIds: string[]): Promise<Map<string, ArtifactIndexingStatusItem>> {
  const m = new Map<string, ArtifactIndexingStatusItem>();
  if (artifactIds.length === 0) return m;
  const headers = getAuthHeaders();
  for (let i = 0; i < artifactIds.length; i += INDEXING_STATUS_MAX_IDS_PER_REQUEST) {
    const slice = artifactIds.slice(i, i + INDEXING_STATUS_MAX_IDS_PER_REQUEST);
    const params = new URLSearchParams();
    applyScopeParams(params, scope);
    for (const id of slice) {
      params.append("artifact_id", id);
    }
    const response = await fetch(`/api/v1/artifacts/indexing-status?${params.toString()}`, { headers });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    const data = await response.json();
    for (const item of data.items || []) {
      const row = item as ArtifactIndexingStatusItem;
      m.set(row.artifact_id, row);
    }
  }
  return m;
}

async function postReindexText(artifactId: string, scope: Scope): Promise<ArtifactReindexResponse> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/reindex`, scope), {
    method: "POST",
    headers,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json();
}

function getReindexSkipReason(status?: ArtifactIndexingStatusItem): string | null {
  if (!status) return null;
  if (!status.indexing_enabled) return "indexing disabled";
  if (status.summary === "awaiting_derivation") return "awaiting derived text";
  if (status.summary === "index_unavailable") return "index unavailable";
  if (status.summary === "unsupported_kind") return "unsupported kind";
  if (status.summary === "missing_source_link") return "missing source link";
  return null;
}

interface SelectedReindexResult {
  reindexed_count: number;
  skipped_count: number;
  failed_count: number;
  skips: Array<{ artifact_id: string; reason: string }>;
  failures: Array<{ artifact_id: string; error: string }>;
}

async function reindexSelectedArtifacts(
  artifactIds: string[],
  scope: Scope,
  indexingById: Map<string, ArtifactIndexingStatusItem>,
): Promise<SelectedReindexResult> {
  let reindexed_count = 0;
  let skipped_count = 0;
  let failed_count = 0;
  const skips: Array<{ artifact_id: string; reason: string }> = [];
  const failures: Array<{ artifact_id: string; error: string }> = [];

  // Sequential reindexes avoid stampeding workers/embeddings for large selections.
  for (const artifactId of artifactIds) {
    const skipReason = getReindexSkipReason(indexingById.get(artifactId));
    if (skipReason) {
      skipped_count += 1;
      skips.push({ artifact_id: artifactId, reason: skipReason });
      continue;
    }
    try {
      await postReindexText(artifactId, scope);
      reindexed_count += 1;
    } catch (err) {
      failed_count += 1;
      failures.push({
        artifact_id: artifactId,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  return { reindexed_count, skipped_count, failed_count, skips, failures };
}

async function patchIndexingPolicy(artifactId: string, scope: Scope, indexingEnabled: boolean): Promise<void> {
  const headers = { ...getAuthHeaders(), "Content-Type": "application/json" };
  const response = await fetch(scopedUrl(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/indexing-policy`, scope), {
    method: "PATCH",
    headers,
    body: JSON.stringify({ indexing_enabled: indexingEnabled }),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
}

async function deleteArtifact(artifactId: string, scope: Scope): Promise<void> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl(`/api/v1/artifacts/${artifactId}`, scope), {
    method: "DELETE",
    headers,
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
}

interface SelectedDeleteResult {
  deleted_count: number;
  failed_count: number;
  failures: Array<{ artifact_id: string; error: string }>;
}

async function deleteSelectedArtifacts(artifactIds: string[], scope: Scope): Promise<SelectedDeleteResult> {
  let deleted_count = 0;
  let failed_count = 0;
  const failures: Array<{ artifact_id: string; error: string }> = [];

  // Sequential deletes keep load modest and avoid stampeding the API for large selections.
  for (const artifactId of artifactIds) {
    try {
      await deleteArtifact(artifactId, scope);
      deleted_count += 1;
    } catch (err) {
      failed_count += 1;
      failures.push({
        artifact_id: artifactId,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  return { deleted_count, failed_count, failures };
}

function parseFilenameFromContentDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1]);
    } catch {
      return utf8Match[1];
    }
  }
  const plainMatch = header.match(/filename="?([^"]+)"?/i);
  return plainMatch?.[1] || fallback;
}

async function openPreviewWithAuth(artifactId: string, scope: Scope): Promise<void> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl(`/api/v1/artifacts/${artifactId}/preview`, scope), { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  const blob = await response.blob();
  const blobUrl = URL.createObjectURL(blob);
  window.open(blobUrl, "_blank", "noopener,noreferrer");
  // Give the new tab time to load the blob URL, then revoke.
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
}

async function downloadWithAuth(artifact: ArtifactMetadata, scope: Scope): Promise<void> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl(`/api/v1/artifacts/${artifact.id}/download`, scope), { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  const blob = await response.blob();
  const blobUrl = URL.createObjectURL(blob);
  const fallback = `${artifact.id}.dat`;
  const filename = parseFilenameFromContentDisposition(
    response.headers.get("Content-Disposition"),
    fallback
  );
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(blobUrl);
}

const ARTIFACT_KINDS = [
  { value: "user_upload", label: "User Upload", color: "blue" },
  { value: "tool_artifact", label: "Tool Artifact", color: "green" },
  { value: "derived_text", label: "Derived Text", color: "orange" },
  { value: "derived_ocr", label: "Derived OCR", color: "purple" },
  { value: "derived_page_image", label: "Page Image", color: "cyan" },
  { value: "derived_image_thumb", label: "Thumbnail", color: "magenta" },
  { value: "derived_image_base", label: "Base Image", color: "gold" },
  { value: "derived_image_detail", label: "Detail Image", color: "lime" },
  { value: "derived_image_roi", label: "ROI Image", color: "volcano" },
  { value: "unknown", label: "Unknown", color: "default" },
];

function getKindConfig(kind: string) {
  return ARTIFACT_KINDS.find(k => k.value === kind) || { value: kind, label: kind, color: "default" };
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function getFileIcon(contentType: string) {
  if (contentType.startsWith("image/")) return <PictureOutlined />;
  if (contentType.startsWith("text/") || contentType.includes("json")) return <FileTextOutlined />;
  if (contentType.includes("pdf")) return <FileTextOutlined />;
  return <FileUnknownOutlined />;
}

export function ArtifactsPage({ scope }: ArtifactsPageProps) {
  const [expandedRows, setExpandedRows] = useState<string[]>([]);
  const [kindFilter, setKindFilter] = useState<string | undefined>(undefined);
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const queryClient = useQueryClient();
  const { token: themeToken } = theme.useToken();

  // Clear selection when scope or kind filter changes.
  useEffect(() => {
    setSelectedRowKeys([]);
  }, [scope.tenantId, scope.motetId, kindFilter]);
  
  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["artifacts", scope.tenantId, scope.motetId, kindFilter],
    queryFn: () => fetchArtifacts(scope, kindFilter),
    refetchInterval: 2000,
  });

  const deleteMutation = useMutation({
    mutationFn: (artifactId: string) => deleteArtifact(artifactId, scope),
    onSuccess: (_result, artifactId) => {
      message.success("Artifact deleted");
      setSelectedRowKeys((prev) => prev.filter((key) => key !== artifactId));
      queryClient.invalidateQueries({ queryKey: ["artifacts"] });
    },
    onError: (err: Error) => {
      message.error(`Failed to delete: ${err.message}`);
    },
  });

  const deleteSelectedMutation = useMutation({
    mutationFn: (artifactIds: string[]) => deleteSelectedArtifacts(artifactIds, scope),
    onSuccess: (result) => {
      if (result.failed_count > 0) {
        message.warning(
          `Deleted ${result.deleted_count} artifact${result.deleted_count === 1 ? "" : "s"}; ${result.failed_count} could not be deleted`
        );
      } else {
        message.success(
          `Deleted ${result.deleted_count} artifact${result.deleted_count === 1 ? "" : "s"}`
        );
      }
      setSelectedRowKeys([]);
      queryClient.invalidateQueries({ queryKey: ["artifacts"] });
      queryClient.invalidateQueries({ queryKey: ["artifacts-indexing-status"] });
    },
    onError: (err: Error) => {
      message.error(`Failed to delete selected artifacts: ${err.message}`);
    },
  });

  const artifacts = data?.items || [];
  const artifactIds = artifacts.map((a) => a.id);

  const { data: indexingStatusMap, isPending: indexingStatusPending } = useQuery({
    queryKey: ["artifacts-indexing-status", scope.tenantId, scope.motetId, artifactIds.join("|")],
    queryFn: () => fetchArtifactsIndexingStatus(scope, artifactIds),
    enabled: artifactIds.length > 0,
    refetchInterval: 4000,
  });
  const indexingById = indexingStatusMap ?? new Map<string, ArtifactIndexingStatusItem>();

  const reindexMutation = useMutation({
    mutationFn: (id: string) => postReindexText(id, scope),
    onSuccess: (result) => {
      message.success(result.status === "success" ? "Text reindex completed" : "Text reindex queued");
      queryClient.invalidateQueries({ queryKey: ["artifacts-indexing-status"] });
    },
    onError: (err: Error) => {
      message.error(err.message || "Text reindex failed");
    },
  });

  const reindexSelectedMutation = useMutation({
    mutationFn: (ids: string[]) => reindexSelectedArtifacts(ids, scope, indexingById),
    onSuccess: (result) => {
      const parts: string[] = [];
      if (result.reindexed_count > 0) {
        parts.push(
          `Indexed ${result.reindexed_count} artifact${result.reindexed_count === 1 ? "" : "s"}`
        );
      }
      if (result.skipped_count > 0) {
        parts.push(`skipped ${result.skipped_count}`);
      }
      if (result.failed_count > 0) {
        parts.push(`failed ${result.failed_count}`);
      }
      const summary = parts.join("; ") || "No artifacts were indexed";
      if (result.failed_count > 0 || (result.reindexed_count === 0 && result.skipped_count > 0)) {
        message.warning(summary);
      } else {
        message.success(summary);
      }
      queryClient.invalidateQueries({ queryKey: ["artifacts-indexing-status"] });
    },
    onError: (err: Error) => {
      message.error(`Failed to index selected artifacts: ${err.message}`);
    },
  });

  const indexingPolicyMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => patchIndexingPolicy(id, scope, enabled),
    onSuccess: (_result, variables) => {
      message.success(variables.enabled ? "Indexing enabled" : "Indexing disabled");
      queryClient.invalidateQueries({ queryKey: ["artifacts"] });
      queryClient.invalidateQueries({ queryKey: ["artifacts-indexing-status"] });
    },
    onError: (err: Error) => {
      message.error(err.message || "Failed to update indexing policy");
    },
  });

  const indexingFeatureDisabled =
    indexingById.size > 0 && [...indexingById.values()].every((v) => !v.artifact_rag_globally_enabled);

  const formatLastUpdated = (timestamp: number) => {
    if (!timestamp) return "";
    return new Date(timestamp).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  };

  // Calculate stats
  const totalCount = artifacts.length;
  const totalBytes = artifacts.reduce((sum, a) => sum + a.bytes, 0);
  const kindCounts = artifacts.reduce((acc, a) => {
    acc[a.kind] = (acc[a.kind] || 0) + 1;
    return acc;
  }, {} as Record<string, number>);

  const selectedCount = selectedRowKeys.length;
  const bulkActionPending =
    deleteSelectedMutation.isPending || reindexSelectedMutation.isPending;
  const selectedReindexEligibleCount = selectedRowKeys.reduce((count, key) => {
    return getReindexSkipReason(indexingById.get(String(key))) ? count : count + 1;
  }, 0);
  const artifactIdsKey = artifactIds.join("|");

  // Drop selections for rows that disappeared after refresh/delete.
  useEffect(() => {
    if (selectedRowKeys.length === 0) return;
    const visible = new Set(artifactIdsKey ? artifactIdsKey.split("|") : []);
    const next = selectedRowKeys.filter((key) => visible.has(String(key)));
    if (next.length !== selectedRowKeys.length) {
      setSelectedRowKeys(next);
    }
  }, [artifactIdsKey, selectedRowKeys]);

  const selectedIdSet = new Set(selectedRowKeys.map(String));
  const allVisibleSelected = totalCount > 0 && artifactIds.every((id) => selectedIdSet.has(id));
  const someVisibleSelected = artifactIds.some((id) => selectedIdSet.has(id));

  const toggleRowSelected = (artifactId: string, checked: boolean) => {
    setSelectedRowKeys((prev) => {
      if (checked) {
        if (prev.some((key) => String(key) === artifactId)) return prev;
        return [...prev, artifactId];
      }
      return prev.filter((key) => String(key) !== artifactId);
    });
  };

  const toggleSelectAllVisible = (checked: boolean) => {
    if (checked) {
      setSelectedRowKeys((prev) => {
        const next = new Set(prev.map(String));
        for (const id of artifactIds) next.add(id);
        return Array.from(next);
      });
      return;
    }
    const visible = new Set(artifactIds);
    setSelectedRowKeys((prev) => prev.filter((key) => !visible.has(String(key))));
  };

  const confirmDeleteSelected = () => {
    deleteSelectedMutation.mutate(selectedRowKeys.map(String));
  };

  const confirmReindexSelected = () => {
    reindexSelectedMutation.mutate(selectedRowKeys.map(String));
  };

  const columns = [
    {
      title: "ID",
      dataIndex: "id",
      key: "id",
      width: 120,
      render: (id: string) => (
        <Tooltip title={id}>
          <Text code style={{ fontSize: 10 }}>{id.slice(0, 12)}...</Text>
        </Tooltip>
      ),
    },
    {
      title: "Kind",
      dataIndex: "kind",
      key: "kind",
      width: 140,
      filters: ARTIFACT_KINDS.map(k => ({ text: k.label, value: k.value })),
      onFilter: (value: any, record: ArtifactMetadata) => record.kind === value,
      render: (kind: string) => {
        const config = getKindConfig(kind);
        return <Tag color={config.color}>{config.label}</Tag>;
      },
    },
    {
      title: "Type",
      dataIndex: "content_type",
      key: "content_type",
      width: 150,
      render: (type: string) => (
        <Space size={4}>
          {getFileIcon(type)}
          <Text style={{ fontSize: 11 }}>{type}</Text>
        </Space>
      ),
    },
    {
      title: "Size",
      dataIndex: "bytes",
      key: "bytes",
      width: 90,
      sorter: (a: ArtifactMetadata, b: ArtifactMetadata) => a.bytes - b.bytes,
      render: (bytes: number) => <Text>{formatBytes(bytes)}</Text>,
    },
    {
      title: "Created",
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
      sorter: (a: ArtifactMetadata, b: ArtifactMetadata) => a.created_at - b.created_at,
      defaultSortOrder: "descend" as const,
      render: (ts: number) => (
        <Text style={{ fontSize: 11 }}>{new Date(ts * 1000).toLocaleString()}</Text>
      ),
    },
    {
      title: "Indexing",
      key: "indexing",
      width: 118,
      render: (_: unknown, record: ArtifactMetadata) => {
        const st = indexingById.get(record.id);
        if (!st) {
          return <Text type="secondary" style={{ fontSize: 11 }}>…</Text>;
        }
        if (st.summary === "indexed") {
          return (
            <Tooltip title={st.detail || `${st.chunks_indexed} chunks indexed in Valkey Search`}>
              <Tag color="green">{st.chunks_indexed} chunks</Tag>
            </Tooltip>
          );
        }
        if (st.summary === "awaiting_derivation") {
          return (
            <Tooltip title={st.detail}>
              <Tag>Pending text</Tag>
            </Tooltip>
          );
        }
        if (st.summary === "ready_not_indexed") {
          return (
            <Tooltip title={st.detail}>
              <Tag color="orange">Not indexed</Tag>
            </Tooltip>
          );
        }
        if (st.summary === "indexing_disabled") {
          return (
            <Tooltip title={st.detail}>
              <Tag color="default">Disabled</Tag>
            </Tooltip>
          );
        }
        if (st.summary === "index_unavailable") {
          return (
            <Tooltip title={st.detail}>
              <Tag color="red">Index error</Tag>
            </Tooltip>
          );
        }
        return (
          <Tooltip title={st.detail || st.summary}>
            <Tag color="default">—</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: (
        <Space size={8}>
          <span>Actions</span>
          <Tooltip title={allVisibleSelected ? "Clear selection" : "Select all"}>
            <Checkbox
              checked={allVisibleSelected}
              indeterminate={!allVisibleSelected && someVisibleSelected}
              disabled={totalCount === 0 || bulkActionPending}
              onChange={(e) => toggleSelectAllVisible(e.target.checked)}
            />
          </Tooltip>
        </Space>
      ),
      key: "actions",
      width: 196,
      render: (_: any, record: ArtifactMetadata) => (
        <Space size={4}>
          <Tooltip title="Preview">
            <Button
              type="text"
              size="small"
              icon={<EyeOutlined />}
              onClick={async () => {
                try {
                  await openPreviewWithAuth(record.id, scope);
                } catch (err) {
                  const msg = err instanceof Error ? err.message : String(err);
                  message.error(`Preview failed: ${msg}`);
                }
              }}
            />
          </Tooltip>
          <Tooltip title="Download">
            <Button
              type="text"
              size="small"
              icon={<DownloadOutlined />}
              onClick={async () => {
                try {
                  await downloadWithAuth(record, scope);
                } catch (err) {
                  const msg = err instanceof Error ? err.message : String(err);
                  message.error(`Download failed: ${msg}`);
                }
              }}
            />
          </Tooltip>
          <Tooltip title="Reindex derived-text chunks. Server needs MOTET_ARTIFACT_RAG_ENABLED and embedding-capable workers; otherwise the API returns an error.">
            <Button
              type="text"
              size="small"
              icon={<ReloadOutlined />}
              loading={reindexMutation.isPending}
              disabled={
                reindexMutation.isPending ||
                bulkActionPending ||
                (artifactIds.length > 0 && indexingStatusPending) ||
                Boolean(getReindexSkipReason(indexingById.get(record.id)))
              }
              onClick={() => reindexMutation.mutate(record.id)}
            />
          </Tooltip>
          <Popconfirm
            title="Delete artifact?"
            description="This action cannot be undone."
            onConfirm={() => deleteMutation.mutate(record.id)}
            okText="Delete"
            cancelText="Cancel"
          >
            <Tooltip title="Delete">
              <Button
                type="text"
                size="small"
                danger
                icon={<DeleteOutlined />}
                loading={deleteMutation.isPending}
              />
            </Tooltip>
          </Popconfirm>
          <Tooltip title={selectedIdSet.has(record.id) ? "Deselect" : "Select for bulk actions"}>
            <Checkbox
              checked={selectedIdSet.has(record.id)}
              disabled={bulkActionPending}
              onChange={(e) => toggleRowSelected(record.id, e.target.checked)}
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  const expandedRowRender = (record: ArtifactMetadata) => (
    <div style={{ padding: 8 }}>
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label="Artifact ID" span={2}>
          <Text code copyable>{record.id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="Checksum (SHA256)" span={2}>
          <Text code style={{ fontSize: 10 }}>{record.checksum_sha256}</Text>
        </Descriptions.Item>
        {record.source_artifact_id && (
          <Descriptions.Item label="Source Artifact" span={2}>
            <Text code>{record.source_artifact_id}</Text>
          </Descriptions.Item>
        )}
        {record.tenant_id && (
          <Descriptions.Item label="Tenant">
            <Tag>🏢 {record.tenant_id}</Tag>
          </Descriptions.Item>
        )}
        {record.motet_id && (
          <Descriptions.Item label="Motet">
            <Tag>🏗️ {record.motet_id}</Tag>
          </Descriptions.Item>
        )}
        {record.principal_id && (
          <Descriptions.Item label="Principal" span={2}>
            <Text code style={{ fontSize: 10 }}>{record.principal_id}</Text>
          </Descriptions.Item>
        )}
        {record.expires_at && (
          <Descriptions.Item label="Expires" span={2}>
            <Text type={record.expires_at * 1000 < Date.now() ? "danger" : undefined}>
              {new Date(record.expires_at * 1000).toLocaleString()}
            </Text>
          </Descriptions.Item>
        )}
        {record.metadata && Object.keys(record.metadata).length > 0 && (
          <Descriptions.Item label="Metadata" span={2}>
            <pre style={{ 
              fontSize: 10, 
              margin: 0, 
              maxHeight: 150, 
              overflow: "auto",
              background: themeToken.colorFillSecondary,
              padding: 8,
              borderRadius: 4,
            }}>
              {JSON.stringify(record.metadata, null, 2)}
            </pre>
          </Descriptions.Item>
        )}
        {(() => {
          const st = indexingById.get(record.id);
          if (!st) return null;
          return (
            <>
              <Descriptions.Item label="Chunk indexing (server)" span={2}>
                <Tag color={st.artifact_rag_globally_enabled ? "green" : "red"}>
                  {st.artifact_rag_globally_enabled ? "Enabled server-side" : "Disabled (MOTET_ARTIFACT_RAG_ENABLED)"}
                </Tag>
              </Descriptions.Item>
              <Descriptions.Item label="Indexing status" span={2}>
                <Space orientation="vertical" size={4}>
                  <Text code style={{ fontSize: 11 }}>{st.summary}</Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    Role: {st.index_role}
                    {st.source_artifact_id ? ` · source ${st.source_artifact_id.slice(0, 8)}…` : ""}
                    {st.derived_text_artifact_id ? ` · derived ${st.derived_text_artifact_id.slice(0, 8)}…` : ""}
                  </Text>
                  {st.detail && <Text style={{ fontSize: 11 }}>{st.detail}</Text>}
                </Space>
              </Descriptions.Item>
              <Descriptions.Item label="Indexing eligible" span={2}>
                <Space size={8}>
                  <Switch
                    size="small"
                    checked={st.indexing_enabled}
                    loading={indexingPolicyMutation.isPending}
                    disabled={st.index_role === "unsupported" || st.summary === "missing_source_link"}
                    onChange={(enabled) => indexingPolicyMutation.mutate({ id: record.id, enabled })}
                  />
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    Controls whether this artifact source participates in text chunk indexing.
                  </Text>
                </Space>
              </Descriptions.Item>
            </>
          );
        })()}
      </Descriptions>
    </div>
  );

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <FileOutlined style={{ marginRight: 12 }} />
            Artifacts
          </Title>
          <Text type="secondary">Uploaded files, derived artifacts, and derived-text chunk indexing</Text>
        </div>
        {dataUpdatedAt && (
          <Text type="secondary" style={{ fontSize: 11 }}>
            Updated: {formatLastUpdated(dataUpdatedAt)}
          </Text>
        )}
      </div>

      {/* Stats Row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 16 }}>
        <Card size="small">
          <Statistic title="Total Artifacts" value={totalCount} />
        </Card>
        <Card size="small">
          <Statistic title="Total Size" value={formatBytes(totalBytes)} />
        </Card>
        <Card size="small">
          <Statistic 
            title="User Uploads" 
            value={kindCounts["user_upload"] || 0} 
            styles={{ content: { color: themeToken.colorPrimary } }}
          />
        </Card>
        <Card size="small">
          <Statistic 
            title="Derived" 
            value={totalCount - (kindCounts["user_upload"] || 0) - (kindCounts["tool_artifact"] || 0)} 
            styles={{ content: { color: themeToken.colorWarning } }}
          />
        </Card>
      </div>

      {/* Kind Breakdown */}
      {Object.keys(kindCounts).length > 0 && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Text strong style={{ marginRight: 12 }}>By Kind:</Text>
          <Space wrap>
            {Object.entries(kindCounts).map(([kind, count]) => {
              const config = getKindConfig(kind);
              return (
                <Tag key={kind} color={config.color}>
                  {config.label}: {count}
                </Tag>
              );
            })}
          </Space>
        </Card>
      )}

      {indexingFeatureDisabled && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          title="Derived-text chunk indexing is off on this server"
          description="Set MOTET_ARTIFACT_RAG_ENABLED=true and ensure workers expose embeddings. Status columns still show derivation readiness."
        />
      )}

      {error && (
        <Alert
          title="Error loading artifacts"
          description={String(error)}
          type="error"
          style={{ marginBottom: 16 }}
        />
      )}

      <Card>
        {selectedCount > 0 && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            title={`${selectedCount} artifact${selectedCount === 1 ? "" : "s"} selected`}
            action={
              <Space size={8}>
                <Button size="small" onClick={() => setSelectedRowKeys([])} disabled={bulkActionPending}>
                  Clear
                </Button>
                <Tooltip title="Index or reindex derived-text chunks. Skips artifacts that are not eligible (disabled, awaiting text, unsupported, etc.).">
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    disabled={bulkActionPending || selectedReindexEligibleCount === 0}
                    loading={reindexSelectedMutation.isPending}
                    onClick={confirmReindexSelected}
                  >
                    Index Selected ({selectedReindexEligibleCount}/{selectedCount})
                  </Button>
                </Tooltip>
                <Popconfirm
                  title={`Delete ${selectedCount} selected artifact${selectedCount === 1 ? "" : "s"}?`}
                  description="This action cannot be undone."
                  onConfirm={confirmDeleteSelected}
                  okText="Delete"
                  cancelText="Cancel"
                  okButtonProps={{ danger: true, loading: deleteSelectedMutation.isPending }}
                >
                  <Button
                    size="small"
                    danger
                    icon={<DeleteOutlined />}
                    disabled={bulkActionPending}
                    loading={deleteSelectedMutation.isPending}
                  >
                    Delete Selected ({selectedCount})
                  </Button>
                </Popconfirm>
              </Space>
            }
          />
        )}
        <Table
          dataSource={artifacts}
          columns={columns}
          rowKey="id"
          loading={isLoading}
          pagination={{ pageSize: 20 }}
          expandable={{
            expandedRowRender,
            expandedRowKeys: expandedRows,
            onExpandedRowsChange: (keys) => setExpandedRows(keys as string[]),
          }}
          locale={{ emptyText: "No artifacts found" }}
        />
      </Card>
    </div>
  );
}
