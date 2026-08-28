/**
 * Motet - Admin Dashboard - Skill Workspaces Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Operator view of per-conversation workspace containers.
 *     One row per ``(tenant_id, conversation_id, bundle_id, skill_name, image_stack)``
 *     tuple. Mirrors the InstanceManagersPage shape (header card with operator gates,
 *     table with expandable details) so operators learn one panel layout for the
 *     whole "Motet platform state" surface.
 *
 *     What we deliberately do NOT show here:
 *       - Per-container env / mount paths / runtime: surfacing those would
 *         drift toward a "workspace container shell" UX. The dashboard is for
 *         observability only; lifecycle actions (restart, force-reap) live
 *         in the CLI.
 *       - A "create container" affordance: containers are lazily created on
 *         the first runner call. Empty state is a feature, not a workflow.
 *
 *     Data source:
 *     - GET /api/v1/workspace-containers (JWT required; same contract as
 *       ``/api/v1/workers/managers/status``).
 */
import {
  Typography,
  Card,
  Table,
  Tag,
  Alert,
  Statistic,
  Tooltip,
  Badge,
  Space,
  Descriptions,
  Collapse,
  Empty,
  Switch,
} from "antd";
import { useQuery } from "@tanstack/react-query";
import {
  ClusterOutlined,
  FireOutlined,
  SnippetsOutlined,
  ContainerOutlined,
  CheckCircleOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useState } from "react";
import { getAuthHeaders } from "../api/http";
import { scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface WorkspaceContainerEntry {
  tenant_id: string;
  conversation_id: string;
  bundle_id: string;
  skill_name: string;
  image_stack: string;
  container_id: string;
  container_id_short: string;
  image: string;
  mode: "cold" | "warm";
  endpoint: string | null;
  created_at: number;
  last_active_at: number;
  idle_seconds: number;
  worker_attribution: string | null;
  script_sha256: string | null;
  script_logical_name: string | null;
}

interface WorkspaceContainersConfig {
  enabled: boolean;
  stateful_mode_enabled: boolean;
  idle_ttl_seconds: number;
  max_per_tenant: number;
  max_bytes: number;
}

interface WorkspaceContainersResponse {
  status: string;
  config: WorkspaceContainersConfig;
  tenants: Record<string, number>;
  containers: WorkspaceContainerEntry[];
  timestamp: number;
}

async function fetchWorkspaceContainers(scope: Scope): Promise<WorkspaceContainersResponse> {
  const headers = getAuthHeaders();
  const response = await fetch(
    scopedUrl("/api/v1/workspace-containers", { tenantId: scope.tenantId, motetId: null }),
    { headers },
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch workspace containers: ${response.status}`);
  }
  return (await response.json()) as WorkspaceContainersResponse;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KiB`;
  if (bytes < 1024 * 1024 * 1024) return `${Math.round(bytes / 1024 / 1024)} MiB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GiB`;
}

function ModeTag({ mode }: { mode: "cold" | "warm" }) {
  if (mode === "warm") {
    return (
      <Tooltip title="lifetime: stateful — long-lived in-container Python supervisor; module-level globals survive across calls.">
        <Tag color="volcano" icon={<FireOutlined />}>
          stateful
        </Tag>
      </Tooltip>
    );
  }
  return (
    <Tooltip title="lifetime: workspace — per-call docker exec; /scratch persists, in-process state does not.">
      <Tag color="blue" icon={<SnippetsOutlined />}>
          workspace
      </Tag>
    </Tooltip>
  );
}

function IdleBadge({ idleSeconds, ttl }: { idleSeconds: number; ttl: number }) {
  const fraction = ttl > 0 ? idleSeconds / ttl : 0;
  if (fraction >= 0.9) {
    return <Badge status="warning" text={<span>{formatDuration(idleSeconds)}</span>} />;
  }
  if (fraction >= 0.5) {
    return <Badge status="processing" text={<span>{formatDuration(idleSeconds)}</span>} />;
  }
  return <Badge status="success" text={<span>{formatDuration(idleSeconds)}</span>} />;
}

function ConfigCallout({ config }: { config: WorkspaceContainersConfig }) {
  if (!config.enabled) {
    return (
      <Alert
        type="warning"
        showIcon
        title="Workspace containers are disabled"
        description={
          <Text>
            <Text code>MOTET_WORKSPACE_CONTAINER_ENABLED=false</Text> on this stack.
            All <Text code>lifetime: workspace</Text> and{" "}
            <Text code>lifetime: stateful</Text>{" "}
            runner declarations will silently downgrade to per-call execution.
          </Text>
        }
        style={{ marginBottom: 16 }}
      />
    );
  }
  if (!config.stateful_mode_enabled) {
    return (
      <Alert
        type="info"
        showIcon
        title="Stateful mode is off; stateful runners downgrade to workspace"
        description={
          <Text>
            <Text code>MOTET_WORKSPACE_STATEFUL_MODE_ENABLED=false</Text>. Workspace
            lifetime still works (the <Text code>/scratch</Text> volume persists
            across calls); module-level globals do not.
          </Text>
        }
        style={{ marginBottom: 16 }}
      />
    );
  }
  return null;
}

function ContainerExpandedRow({ record }: { record: WorkspaceContainerEntry }) {
  const items = [];

  items.push({
    key: "identity",
    label: "Identity",
    children: (
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label="tenant_id">
          <Text code>{record.tenant_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="conversation_id">
          <Text code>{record.conversation_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="bundle_id">
          <Text code>{record.bundle_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="skill_name">
          <Text code>{record.skill_name}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="image_stack">
          <Tag color="geekblue">{record.image_stack}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="image">
          <Text code style={{ fontSize: 11 }}>{record.image}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="mode">
          <ModeTag mode={record.mode} />
        </Descriptions.Item>
        <Descriptions.Item label="container_id">
          <Tooltip title={record.container_id}>
            <Text code style={{ fontSize: 11 }}>{record.container_id_short}</Text>
          </Tooltip>
        </Descriptions.Item>
      </Descriptions>
    ),
  });

  items.push({
    key: "lifetime",
    label: "Lifetime",
    children: (
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label="created">
          {new Date(record.created_at * 1000).toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="last active">
          {new Date(record.last_active_at * 1000).toLocaleString()}
        </Descriptions.Item>
        <Descriptions.Item label="idle for">
          {formatDuration(record.idle_seconds)}
        </Descriptions.Item>
        <Descriptions.Item label="created by worker">
          <Tooltip title="Observability only. Any worker may dispatch into this workspace regardless of who created it.">
            <Text code style={{ fontSize: 11 }}>{record.worker_attribution ?? "—"}</Text>
          </Tooltip>
        </Descriptions.Item>
      </Descriptions>
    ),
  });

  if (record.mode === "warm") {
    items.push({
      key: "warm",
      label: "Warm Supervisor",
      children: (
        <Descriptions size="small" column={2} bordered>
          <Descriptions.Item label="loaded script">
            <Text code style={{ fontSize: 11 }}>
              {record.script_logical_name ?? "—"}
            </Text>
          </Descriptions.Item>
          <Descriptions.Item label="script SHA-256">
            <Tooltip title="Bundle redeploys with a different SHA force the container to be rebuilt so stale module-level state is never reused.">
              <Text code style={{ fontSize: 10 }}>
                {record.script_sha256 ? record.script_sha256.slice(0, 16) + "…" : "—"}
              </Text>
            </Tooltip>
          </Descriptions.Item>
        </Descriptions>
      ),
    });
  }

  return <Collapse items={items} size="small" ghost defaultActiveKey={["identity", "lifetime"]} />;
}

export function WorkspaceContainersPage({ scope }: { scope: Scope }) {
  const [autoRefresh, setAutoRefresh] = useState(true);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["workspace-containers", scope.tenantId],
    queryFn: () => fetchWorkspaceContainers(scope),
    refetchInterval: autoRefresh ? 3000 : false,
  });

  const containers = data?.containers ?? [];
  const config = data?.config;
  const tenants = data?.tenants ?? {};

  // Aggregate stats for the top bar.
  const totalContainers = containers.length;
  const warmCount = containers.filter((c) => c.mode === "warm").length;
  const coldCount = containers.filter((c) => c.mode === "cold").length;
  const tenantCount = Object.keys(tenants).length;
  const idleTtl = config?.idle_ttl_seconds ?? 1800;

  const columns = [
    {
      title: "Tenant",
      dataIndex: "tenant_id",
      key: "tenant_id",
      sorter: (a: WorkspaceContainerEntry, b: WorkspaceContainerEntry) =>
        a.tenant_id.localeCompare(b.tenant_id),
      render: (id: string) => <Text code style={{ fontSize: 12 }}>{id}</Text>,
    },
    {
      title: "Conversation",
      dataIndex: "conversation_id",
      key: "conversation_id",
      sorter: (a: WorkspaceContainerEntry, b: WorkspaceContainerEntry) =>
        a.conversation_id.localeCompare(b.conversation_id),
      render: (id: string) => (
        <Tooltip title={id}>
          <Text code style={{ fontSize: 11 }}>
            {id.length > 24 ? id.slice(0, 24) + "…" : id}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: "Bundle",
      dataIndex: "bundle_id",
      key: "bundle_id",
      width: 140,
      sorter: (a: WorkspaceContainerEntry, b: WorkspaceContainerEntry) =>
        a.bundle_id.localeCompare(b.bundle_id),
      render: (id: string) => <Text code style={{ fontSize: 11 }}>{id}</Text>,
    },
    {
      title: "Skill",
      dataIndex: "skill_name",
      key: "skill_name",
      width: 140,
      sorter: (a: WorkspaceContainerEntry, b: WorkspaceContainerEntry) =>
        a.skill_name.localeCompare(b.skill_name),
      render: (name: string) => <Text code style={{ fontSize: 11 }}>{name}</Text>,
    },
    {
      title: "Image Stack",
      dataIndex: "image_stack",
      key: "image_stack",
      width: 150,
      render: (stack: string) => <Tag color="geekblue">{stack}</Tag>,
    },
    {
      title: "Mode",
      dataIndex: "mode",
      key: "mode",
      width: 100,
      filters: [
        { text: "Cold", value: "cold" },
        { text: "Warm", value: "warm" },
      ],
      onFilter: (value: any, record: WorkspaceContainerEntry) => record.mode === value,
      render: (mode: "cold" | "warm") => <ModeTag mode={mode} />,
    },
    {
      title: "Container",
      dataIndex: "container_id_short",
      key: "container_id_short",
      width: 120,
      render: (short: string, record: WorkspaceContainerEntry) => (
        <Tooltip title={record.container_id}>
          <Text code style={{ fontSize: 11 }}>{short}</Text>
        </Tooltip>
      ),
    },
    {
      title: "Idle",
      key: "idle_seconds",
      width: 110,
      align: "right" as const,
      sorter: (a: WorkspaceContainerEntry, b: WorkspaceContainerEntry) =>
        a.idle_seconds - b.idle_seconds,
      defaultSortOrder: "ascend" as const,
      render: (_: any, record: WorkspaceContainerEntry) => (
        <IdleBadge idleSeconds={record.idle_seconds} ttl={idleTtl} />
      ),
    },
    {
      title: "Created",
      key: "created_at",
      width: 100,
      align: "right" as const,
      sorter: (a: WorkspaceContainerEntry, b: WorkspaceContainerEntry) =>
        a.created_at - b.created_at,
      render: (_: any, record: WorkspaceContainerEntry) => (
        <Tooltip title={new Date(record.created_at * 1000).toLocaleString()}>
          <Text style={{ fontSize: 12 }}>
            {formatDuration((Date.now() / 1000) - record.created_at)} ago
          </Text>
        </Tooltip>
      ),
    },
  ];

  return (
    <div>
      <div className="page-header">
        <Title level={2}>
          <ContainerOutlined style={{ marginRight: 12 }} />
          Skill Workspaces
        </Title>
        <Text type="secondary">
          Active per-skill conversation workspaces. One row per
          {" "}<Text code>(tenant, conversation, bundle, skill, image_stack)</Text>; any worker
          may dispatch into the backing workspace container.
        </Text>
      </div>

      {config && <ConfigCallout config={config} />}

      {/* Compact stats bar */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: 16,
          }}
        >
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Containers</Text>}
            value={totalContainers}
            styles={{ content: { fontSize: 18 } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Tenants</Text>}
            value={tenantCount}
            styles={{ content: { fontSize: 18 } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Cold / Warm</Text>}
            value={`${coldCount} / ${warmCount}`}
            styles={{ content: { fontSize: 18 } }}
          />
          {config && (
            <>
              <Statistic
                title={<Text type="secondary" style={{ fontSize: 11 }}>Idle TTL</Text>}
                value={formatDuration(config.idle_ttl_seconds)}
                styles={{ content: { fontSize: 18 } }}
              />
              <Statistic
                title={<Text type="secondary" style={{ fontSize: 11 }}>Per-Tenant Cap</Text>}
                value={config.max_per_tenant}
                styles={{ content: { fontSize: 18 } }}
              />
              <Statistic
                title={<Text type="secondary" style={{ fontSize: 11 }}>Max Disk</Text>}
                value={formatBytes(config.max_bytes)}
                styles={{ content: { fontSize: 18 } }}
              />
              <Statistic
                title={<Text type="secondary" style={{ fontSize: 11 }}>Master Switch</Text>}
                value=""
                formatter={() =>
                  config.enabled ? (
                    <Tag color="success" icon={<CheckCircleOutlined />}>enabled</Tag>
                  ) : (
                    <Tag color="error" icon={<StopOutlined />}>disabled</Tag>
                  )
                }
                styles={{ content: { fontSize: 18 } }}
              />
            </>
          )}
          <Space size={8}>
            <Text type="secondary" style={{ fontSize: 11 }}>Auto-refresh</Text>
            <Switch
              checked={autoRefresh}
              size="small"
              onChange={setAutoRefresh}
            />
          </Space>
        </div>
      </Card>

      {error && (
        <Alert
          type="error"
          showIcon
          title="Failed to load workspace containers"
          description={error instanceof Error ? error.message : String(error)}
          style={{ marginBottom: 16 }}
          action={<a onClick={() => refetch()}>Retry</a>}
        />
      )}

      <Card size="small">
        {containers.length === 0 && !isLoading && !error ? (
          <Empty
            description={
              <Space orientation="vertical" size={4}>
                <Text>No workspace containers are currently bound.</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  Containers are lazily created on the first runner call with{" "}
                  <Text code>lifetime: workspace</Text> or{" "}
                  <Text code>lifetime: stateful</Text>.
                  Run a skill in a conversation and refresh.
                </Text>
              </Space>
            }
          />
        ) : (
          <Table
            rowKey={(r) =>
              `${r.tenant_id}:${r.conversation_id}:${r.bundle_id}:${r.skill_name}:${r.image_stack}`
            }
            dataSource={containers}
            columns={columns}
            loading={isLoading}
            size="small"
            pagination={{
              pageSize: 25,
              hideOnSinglePage: true,
              showSizeChanger: false,
            }}
            expandable={{
              expandedRowRender: (record) => <ContainerExpandedRow record={record} />,
              rowExpandable: () => true,
            }}
          />
        )}
      </Card>

      {tenantCount > 0 && (
        <Card size="small" style={{ marginTop: 16 }}>
          <Title level={5} style={{ marginTop: 0 }}>
            <ClusterOutlined style={{ marginRight: 8 }} />
            Per-Tenant Cardinality
          </Title>
          <Space size={[4, 4]} wrap>
            {Object.entries(tenants)
              .sort(([, a], [, b]) => b - a)
              .map(([t, n]) => {
                const cap = config?.max_per_tenant ?? 100;
                const ratio = n / cap;
                const color =
                  ratio >= 0.9 ? "warning" : ratio >= 0.5 ? "processing" : "default";
                return (
                  <Tag key={t} color={color === "default" ? undefined : color}>
                    <Text code style={{ fontSize: 11 }}>{t}</Text>: {n} / {cap}
                  </Tag>
                );
              })}
          </Space>
        </Card>
      )}
    </div>
  );
}
