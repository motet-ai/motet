/**
 * Motet - Admin Dashboard - Workers Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Displays worker status and health metrics from /api/v1/workers/readiness.
 *     Shows system stats, individual worker details, and health indicators.
 *     Table columns are memoized so the 2s readiness poll does not rebuild
 *     the table body.
 */
import { useState, useCallback, useMemo } from "react";
import { Typography, Card, Table, Tag, Alert, Statistic, Progress, Tooltip, Collapse, Descriptions, Space, Badge, List, Button, Popconfirm } from "antd";
import { message } from "../antdApp";
import { useQuery } from "@tanstack/react-query";
import {
  ClusterOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  StopOutlined,
  SyncOutlined,
  ToolOutlined,
  ThunderboltOutlined,
  ApiOutlined,
  AppstoreOutlined,
} from "@ant-design/icons";
import { getAuthHeaders } from "../api/http";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface WorkersPageProps {
  scope: Scope;
}

// Tool info from API
interface ToolInfo {
  name: string;
  description?: string;
  category?: string;
  is_mcp?: boolean;
}

// Manager info from /managers/status
// Manager info from /managers/status: managers are identified by ``manager_id`` (canonical) and
// list the workers they serve via ``served_workers``. ``worker_id`` is
// retained as the bootstrap-attribution telemetry tag (which worker initially
// brought this manager up; for sibling deployments this is
// the manager's own service identity, e.g. ``mcp-manager``).
interface ManagerInfo {
  manager_id?: string;
  served_workers?: string[];
  type: string;
  worker_id: string;
  status: string;
  pid?: number;
  instances?: { total: number; healthy: number };
  stats?: { total_requests: number; active_requests: number; errors: number };
  resources?: { memory_mb: number };
}

// Worker info matching the API response from /api/v1/workers/readiness
interface WorkerInfo {
  worker_id: string;
  state: "ready" | "accepting" | "busy" | "idle" | "offline" | "draining" | "starting" | "warming" | "unhealthy" | "stopped" | "terminating" | "restarting";
  capabilities: string[];
  active_commands: number;
  max_concurrency: number;
  utilization_percent: number;
  tool_count: number;
  mcp_tool_count: number;
  tools: ToolInfo[];
  warmup_completed: boolean;
  warmup_duration_ms: number | null;
  pool_type: string;
  last_heartbeat: number;
  heartbeat_age_seconds: number | null;
  memory_usage_mb: number;
  cpu_usage_percent: number;
  uptime_seconds: number;
  is_healthy: boolean;
  managers?: Record<string, ManagerInfo>;
}

interface SystemStats {
  total_workers: number;
  ready_workers: number;
  state_distribution: Record<string, number>;
  total_capacity: number;
  active_commands: number;
  utilization_percent: number;
  average_tools_per_worker: number;
}

interface WorkersResponse {
  status: string;
  timestamp: string;
  system_stats: SystemStats;
  workers: Record<string, Omit<WorkerInfo, "worker_id">>;
}


async function fetchWorkers(): Promise<{ workers: WorkerInfo[]; stats: SystemStats }> {
  const headers = getAuthHeaders();
  // Fetch worker readiness and manager status in parallel
  const [workerResponse, managerResponse] = await Promise.all([
    fetch("/api/v1/workers/readiness", { headers }),
    fetch("/api/v1/workers/managers/status", { headers }).catch(() => null),
  ]);

  if (!workerResponse.ok) {
    throw new Error(`Failed to fetch workers: ${workerResponse.status}`);
  }

  const data: WorkersResponse = await workerResponse.json();
  
  if (data.status !== "success") {
    throw new Error("API returned error status");
  }

  // Parse manager data if available
  let managers: Record<string, ManagerInfo> = {};
  if (managerResponse?.ok) {
    try {
      const managerData = await managerResponse.json();
      if (managerData.status === "success") {
        managers = managerData.managers || {};
      }
    } catch {
      // Ignore manager fetch errors
    }
  }

  // Transform the response to array format and attach managers.
  // A manager is attached to a worker if the worker appears in
  // ``served_workers`` (canonical), with a back-compat fallback to
  // ``worker_id`` matching for older publishers that haven't set
  // ``served_workers`` yet.
  const workers = Object.entries(data.workers || {}).map(([id, info]) => {
    const workerManagers: Record<string, ManagerInfo> = {};
    for (const [, manager] of Object.entries(managers)) {
      const served = manager.served_workers && manager.served_workers.length > 0
        ? manager.served_workers
        : [manager.worker_id];
      if (served.includes(id)) {
        // Key by manager_id so multiple managers of the same type don't
        // collide on the per-worker row.
        const key = manager.manager_id
          ? `${manager.type}:${manager.manager_id}`
          : manager.type;
        workerManagers[key] = manager;
      }
    }

    return {
      worker_id: id,
      ...info,
      managers: Object.keys(workerManagers).length > 0 ? workerManagers : undefined,
    };
  });

  return {
    workers,
    stats: data.system_stats,
  };
}

async function requestWorkerAction(workerId: string, action: "start" | "stop" | "restart") {
  const headers = getAuthHeaders();
  const response = await fetch(`/api/v1/workers/${encodeURIComponent(workerId)}/${action}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...headers,
    },
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = (payload as any)?.detail || (payload as any)?.error || `Request failed (${response.status})`;
    throw new Error(detail);
  }

  return payload;
}

// Helper to format uptime
function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

// Helper to get state color
function getStateColor(state: string): string {
  switch (state) {
    case "ready":
    case "accepting":
      return "success";
    case "busy":
    case "restarting":
      return "processing";
    case "idle":
    case "stopped":
      return "default";
    case "draining":
    case "terminating":
      return "warning";
    case "starting":
    case "warming":
      return "cyan";
    case "offline":
    case "unhealthy":
    default:
      return "error";
  }
}

// Helper to get state icon
function getStateIcon(state: string) {
  switch (state) {
    case "ready":
    case "accepting":
      return <CheckCircleOutlined />;
    case "busy":
    case "restarting":
      return <SyncOutlined spin />;
    case "starting":
    case "warming":
      return <SyncOutlined />;
    case "stopped":
      return <StopOutlined />;
    case "terminating":
      return <CloseCircleOutlined />;
    case "draining":
      return <CloseCircleOutlined />;
    default:
      return <CloseCircleOutlined />;
  }
}

// Helper to get manager status icon
function getManagerStatusIcon(status: string): string {
  switch (status) {
    case "running": return "🟢";
    case "starting": return "🟡";
    case "stale": return "🟡";
    case "stopped": return "🔴";
    default: return "⚪";
  }
}

// Expandable row content component
function WorkerExpandedRow({ record }: { record: WorkerInfo }) {
  const capabilities = record.capabilities || [];
  const tools = record.tools || [];
  const managers = record.managers || {};

  const collapseItems = [];

  // Capabilities section
  if (capabilities.length > 0) {
    collapseItems.push({
      key: "capabilities",
      label: (
        <Space>
          <AppstoreOutlined />
          <span>Capabilities ({capabilities.length})</span>
        </Space>
      ),
      children: (
        <Space wrap size={[4, 4]}>
          {capabilities.map((cap) => (
            <Tag key={cap} color="blue">{cap}</Tag>
          ))}
        </Space>
      ),
    });
  }

  // Instance Managers section
  if (Object.keys(managers).length > 0) {
    collapseItems.push({
      key: "managers",
      label: (
        <Space>
          <ApiOutlined />
          <span>Instance Managers ({Object.keys(managers).length})</span>
        </Space>
      ),
      children: (
        <Space orientation="vertical" style={{ width: "100%" }}>
          {Object.entries(managers).map(([key, manager]) => {
            const baseName = manager.type === "mcp" ? "MCP Instance Manager" : "Local Inference Manager";
            const managerName = manager.manager_id
              ? `${baseName} (${manager.manager_id})`
              : baseName;
            return (
              <Card key={key} size="small" style={{ marginBottom: 8 }}>
                <Descriptions size="small" column={4}>
                  <Descriptions.Item label="Name">
                    <Badge 
                      status={manager.status === "running" ? "success" : manager.status === "stale" || manager.status === "starting" ? "warning" : "error"} 
                      text={managerName} 
                    />
                  </Descriptions.Item>
                  <Descriptions.Item label="Status">{getManagerStatusIcon(manager.status)} {manager.status}</Descriptions.Item>
                  <Descriptions.Item label="PID">{manager.pid || "N/A"}</Descriptions.Item>
                  {manager.instances && (
                    <Descriptions.Item label="Instances">{manager.instances.healthy}/{manager.instances.total}</Descriptions.Item>
                  )}
                  {manager.stats && (
                    <>
                      <Descriptions.Item label="Requests">{manager.stats.total_requests} ({manager.stats.active_requests} active)</Descriptions.Item>
                      {manager.stats.errors > 0 && (
                        <Descriptions.Item label="Errors">
                          <Tag color="error">{manager.stats.errors}</Tag>
                        </Descriptions.Item>
                      )}
                    </>
                  )}
                  {manager.resources && (
                    <Descriptions.Item label="Memory">{Math.round(manager.resources.memory_mb)}MB</Descriptions.Item>
                  )}
                </Descriptions>
              </Card>
            );
          })}
        </Space>
      ),
    });
  }

  // Available Tools section
  if (tools.length > 0) {
    collapseItems.push({
      key: "tools",
      label: (
        <Space>
          <ToolOutlined />
          <span>Available Tools ({tools.length})</span>
        </Space>
      ),
      children: (
        <List
          size="small"
          dataSource={tools}
          style={{ maxHeight: 300, overflow: "auto" }}
          renderItem={(tool: ToolInfo) => (
            <List.Item style={{ padding: "4px 0" }}>
              <List.Item.Meta
                title={
                  <Space size={4}>
                    <Text code style={{ fontSize: 12 }}>{tool.name}</Text>
                    {tool.is_mcp && <Tag color="purple" style={{ fontSize: 10 }}>MCP</Tag>}
                    {tool.category && <Tag style={{ fontSize: 10 }}>{tool.category}</Tag>}
                  </Space>
                }
                description={<Text type="secondary" style={{ fontSize: 11 }}>{tool.description || "No description"}</Text>}
              />
            </List.Item>
          )}
        />
      ),
    });
  }

  if (collapseItems.length === 0) {
    return <Text type="secondary">No additional details available</Text>;
  }

  return (
    <Collapse 
      items={collapseItems} 
      size="small" 
      ghost 
      style={{ background: "transparent" }}
    />
  );
}

const EMPTY_WORKERS: WorkerInfo[] = [];

export function WorkersPage({ scope }: WorkersPageProps) {
  const [actionLoading, setActionLoading] = useState<Record<string, boolean>>({});
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["workers", scope.tenantId, scope.motetId],
    queryFn: fetchWorkers,
    refetchInterval: 2000, // Refresh every 2 seconds
  });

  const workers = data?.workers ?? EMPTY_WORKERS;
  const stats = data?.stats;

  const runAction = useCallback(async (workerId: string, action: "start" | "stop" | "restart") => {
    const key = `${workerId}:${action}`;
    setActionLoading((prev) => ({ ...prev, [key]: true }));
    try {
      await requestWorkerAction(workerId, action);
      message.success(`${action.toUpperCase()} requested for ${workerId}`);
      refetch();
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Request failed";
      message.error(`Failed to ${action} ${workerId}: ${errorMessage}`);
    } finally {
      setActionLoading((prev) => ({ ...prev, [key]: false }));
    }
  }, [refetch]);

  const columns = useMemo(() => [
    {
      title: "Worker ID",
      dataIndex: "worker_id",
      key: "worker_id",
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.worker_id || "").localeCompare(b.worker_id || ""),
      render: (id: string) => <Text code style={{ fontSize: 11 }}>{id}</Text>,
    },
    {
      title: "State",
      dataIndex: "state",
      key: "state",
      width: 100,
      filters: [
        { text: "Ready", value: "ready" },
        { text: "Busy", value: "busy" },
        { text: "Idle", value: "idle" },
        { text: "Stopped", value: "stopped" },
        { text: "Restarting", value: "restarting" },
        { text: "Terminating", value: "terminating" },
        { text: "Draining", value: "draining" },
      ],
      onFilter: (value: any, record: WorkerInfo) => record.state === value,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.state || "").localeCompare(b.state || ""),
      render: (state: string, record: WorkerInfo) => (
        <Tooltip title={record.is_healthy ? "Healthy" : "Unhealthy"}>
          <Tag color={getStateColor(state)} icon={getStateIcon(state)}>
            {state.toUpperCase()}
          </Tag>
        </Tooltip>
      ),
    },
    {
      title: "Active",
      key: "active_commands",
      width: 80,
      align: "center" as const,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.active_commands || 0) - (b.active_commands || 0),
      render: (_: any, record: WorkerInfo) => (
        <Tooltip title={`${record.active_commands} of ${record.max_concurrency} slots in use`}>
          <Text strong style={{ color: record.active_commands > 0 ? "#1890ff" : undefined }}>
            {record.active_commands} / {record.max_concurrency}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: "Utilization",
      key: "utilization",
      width: 120,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.utilization_percent || 0) - (b.utilization_percent || 0),
      render: (_: any, record: WorkerInfo) => (
        <Progress
          percent={Math.round(record.utilization_percent)}
          size="small"
          status={record.utilization_percent > 80 ? "exception" : "normal"}
          format={(pct) => `${pct}%`}
        />
      ),
    },
    {
      title: "Tools",
      key: "tools",
      width: 100,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.tool_count || 0) - (b.tool_count || 0),
      render: (_: any, record: WorkerInfo) => (
        <Tooltip title={`${record.mcp_tool_count} MCP + ${record.tool_count - record.mcp_tool_count} native`}>
          <Tag icon={<ToolOutlined />}>
            {record.tool_count}
          </Tag>
        </Tooltip>
      ),
    },
    {
      title: "Pool",
      dataIndex: "pool_type",
      key: "pool_type",
      width: 80,
      filters: [...new Set(workers.map(w => w.pool_type).filter(Boolean))].map(p => ({ text: p, value: p })),
      onFilter: (value: any, record: WorkerInfo) => record.pool_type === value,
      render: (pool: string) => <Tag>{pool || "?"}</Tag>,
    },
    {
      title: "Memory",
      key: "memory",
      width: 80,
      align: "right" as const,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.memory_usage_mb || 0) - (b.memory_usage_mb || 0),
      render: (_: any, record: WorkerInfo) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {Math.round(record.memory_usage_mb)}MB
        </Text>
      ),
    },
    {
      title: "CPU",
      key: "cpu",
      width: 60,
      align: "right" as const,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.cpu_usage_percent || 0) - (b.cpu_usage_percent || 0),
      render: (_: any, record: WorkerInfo) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {Math.round(record.cpu_usage_percent)}%
        </Text>
      ),
    },
    {
      title: "Heartbeat",
      key: "heartbeat",
      width: 90,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.heartbeat_age_seconds || 0) - (b.heartbeat_age_seconds || 0),
      render: (_: any, record: WorkerInfo) => {
        const age = record.heartbeat_age_seconds;
        if (age === null) return <Text type="secondary">N/A</Text>;
        const icon = age < 30 ? "🟢" : age < 60 ? "🟡" : "🔴";
        return (
          <Tooltip title={`Last heartbeat ${age.toFixed(1)}s ago`}>
            <Text style={{ fontSize: 12 }}>{icon} {age.toFixed(1)}s</Text>
          </Tooltip>
        );
      },
    },
    {
      title: "Uptime",
      dataIndex: "uptime_seconds",
      key: "uptime",
      width: 70,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.uptime_seconds || 0) - (b.uptime_seconds || 0),
      render: (uptime: number) => formatUptime(uptime),
    },
    {
      title: "Warmup",
      key: "warmup",
      width: 90,
      sorter: (a: WorkerInfo, b: WorkerInfo) => (a.warmup_completed === b.warmup_completed ? 0 : a.warmup_completed ? -1 : 1),
      render: (_: any, record: WorkerInfo) => (
        record.warmup_completed ? (
          <Tag color="success" icon={<ThunderboltOutlined />}>
            {record.warmup_duration_ms ? `${record.warmup_duration_ms}ms` : "Done"}
          </Tag>
        ) : (
          <Tag color="processing" icon={<SyncOutlined spin />}>
            Warming
          </Tag>
        )
      ),
    },
    {
      title: "Actions",
      key: "actions",
      width: 180,
      render: (_: any, record: WorkerInfo) => {
        const startKey = `${record.worker_id}:start`;
        const stopKey = `${record.worker_id}:stop`;
        const restartKey = `${record.worker_id}:restart`;
        const isStopped = record.state === "stopped";
        return (
          <Space size={6}>
            {isStopped ? (
              <Button
                size="small"
                type="primary"
                loading={actionLoading[startKey]}
                onClick={() => runAction(record.worker_id, "start")}
              >
                Start
              </Button>
            ) : (
              <>
                <Popconfirm
                  title="Stop worker?"
                  okText="Stop"
                  cancelText="Cancel"
                  onConfirm={() => runAction(record.worker_id, "stop")}
                >
                  <Button size="small" danger loading={actionLoading[stopKey]}>
                    Stop
                  </Button>
                </Popconfirm>
                <Popconfirm
                  title="Restart worker?"
                  okText="Restart"
                  cancelText="Cancel"
                  onConfirm={() => runAction(record.worker_id, "restart")}
                >
                  <Button size="small" loading={actionLoading[restartKey]}>
                    Restart
                  </Button>
                </Popconfirm>
              </>
            )}
          </Space>
        );
      },
    },
  ], [actionLoading, runAction]);

  return (
    <div>
      <div className="page-header">
        <Title level={2}>
          <ClusterOutlined style={{ marginRight: 12 }} />
          Agent Workers
        </Title>
        <Text type="secondary">Monitor worker status and capabilities</Text>
      </div>

      {/* Compact Stats Bar */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 16 }}>
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Ready</Text>}
            value={stats?.ready_workers ?? 0}
            suffix={<Text type="secondary" style={{ fontSize: 12 }}>/ {stats?.total_workers ?? workers.length}</Text>}
            styles={{ content: { fontSize: 18, color: "#52c41a" } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Active</Text>}
            value={stats?.active_commands ?? 0}
            styles={{ content: { fontSize: 18, color: (stats?.active_commands ?? 0) > 0 ? "#1890ff" : undefined } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Capacity</Text>}
            value={stats?.total_capacity ?? 0}
            styles={{ content: { fontSize: 18 } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Utilization</Text>}
            value={Math.round(stats?.utilization_percent ?? 0)}
            suffix="%"
            styles={{ content: { fontSize: 18, color: (stats?.utilization_percent ?? 0) > 80 ? "#ff4d4f" : undefined } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Tools/Worker</Text>}
            value={Math.round(stats?.average_tools_per_worker ?? 0)}
            styles={{ content: { fontSize: 18 } }}
          />
          <Tooltip title={`State distribution: ${JSON.stringify(stats?.state_distribution || {})}`}>
            <Statistic
              title={<Text type="secondary" style={{ fontSize: 11 }}>Warmup</Text>}
              value={workers.filter(w => w.warmup_completed).length}
              suffix={<Text type="secondary" style={{ fontSize: 12 }}>/ {workers.length}</Text>}
              styles={{ content: { fontSize: 18 } }}
            />
          </Tooltip>
        </div>
      </Card>

      {/* Error state */}
      {error && (
        <Alert
          type="error"
          title="Failed to load workers"
          description={String(error)}
          style={{ marginBottom: 16 }}
        />
      )}

      {/* Table */}
      <Card>
        <Table
          dataSource={workers}
          columns={columns}
          rowKey="worker_id"
          loading={isLoading}
          pagination={false}
          size="middle"
          expandable={{
            expandedRowRender: (record) => <WorkerExpandedRow record={record} />,
            rowExpandable: (record) => 
              !!(record.capabilities?.length) || 
              !!(record.tools?.length) || 
              !!(record.managers && Object.keys(record.managers).length > 0),
          }}
        />
      </Card>
    </div>
  );
}
