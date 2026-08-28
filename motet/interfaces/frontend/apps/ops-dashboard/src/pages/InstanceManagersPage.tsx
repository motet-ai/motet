/**
 * Motet - Admin Dashboard - Instance Managers Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 *
 * Description:
 *     Top-level view of all instance managers (MCP and Local Inference), listed by
 *     canonical ``manager_id`` with status, served workers, instance pool counts,
 *     request stats, resource usage, and metadata.
 *
 *     Decoupling from `WorkersPage`:
 *     - `WorkersPage` shows managers attached to a specific worker via
 *       ``served_workers`` (per-worker affinity view). Sibling deployments
 *       where the manager has no canonical worker owner
 *       (e.g. ``mcp-manager`` in docker compose) won't appear there.
 *     - This page is the canonical home for those manager-as-service rows.
 *
 *     Data source:
 *     - GET /api/v1/workers/managers/status (JWT required; ``/readiness``
 *       remains the unauthenticated probe).
 *
 * Dependencies:
 *     - React Query for polling + cache
 *     - Ant Design Table / Card / Badge for layout
 */
import { Typography, Card, Table, Tag, Alert, Statistic, Tooltip, Badge, Space, Descriptions, Collapse, Empty } from "antd";
import { useQuery } from "@tanstack/react-query";
import { ApiOutlined, ThunderboltOutlined, SyncOutlined, CheckCircleOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { getAuthHeaders } from "../api/http";

const { Title, Text } = Typography;

interface ManagerEntry {
  manager_id: string;
  served_workers: string[];
  worker_id: string;
  type: string;
  status: string;
  pid?: number;
  last_update?: number;
  instances?: { total: number; healthy: number; unhealthy: number };
  stats?: { total_requests: number; active_requests: number; errors: number; uptime_seconds: number };
  resources?: { memory_mb: number; cpu_percent: number };
  metadata?: Record<string, unknown>;
}

interface ManagersResponse {
  status: string;
  managers: Record<string, ManagerEntry>;
  timestamp: number;
}


async function fetchManagers(): Promise<ManagerEntry[]> {
  const headers = getAuthHeaders();
  const response = await fetch("/api/v1/workers/managers/status", { headers });
  if (!response.ok) {
    throw new Error(`Failed to fetch instance managers: ${response.status}`);
  }
  const data: ManagersResponse = await response.json();
  if (data.status !== "success") {
    throw new Error("API returned error status");
  }
  return Object.values(data.managers || {});
}

function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function getStatusBadge(status: string) {
  switch (status) {
    case "running":
      return <Badge status="success" text={<span>{status}</span>} />;
    case "starting":
      return <Badge status="processing" text={<span>{status}</span>} />;
    case "stale":
      return <Badge status="warning" text={<span>{status}</span>} />;
    case "stopping":
    case "stopped":
      return <Badge status="default" text={<span>{status}</span>} />;
    case "error":
    case "unhealthy":
      return <Badge status="error" text={<span>{status}</span>} />;
    default:
      return <Badge status="default" text={<span>{status || "unknown"}</span>} />;
  }
}

function getStatusIcon(status: string) {
  switch (status) {
    case "running":
      return <CheckCircleOutlined style={{ color: "#52c41a" }} />;
    case "starting":
      return <SyncOutlined spin />;
    case "stale":
      return <SyncOutlined />;
    case "stopped":
    case "stopping":
      return <CloseCircleOutlined style={{ color: "#999" }} />;
    case "error":
    case "unhealthy":
      return <CloseCircleOutlined style={{ color: "#ff4d4f" }} />;
    default:
      return <ApiOutlined />;
  }
}

function getTypeIcon(type: string) {
  if (type === "local_inference") return <ThunderboltOutlined />;
  return <ApiOutlined />;
}

function ManagerExpandedRow({ record }: { record: ManagerEntry }) {
  const items = [];

  items.push({
    key: "identity",
    label: "Identity",
    children: (
      <Descriptions size="small" column={2} bordered>
        <Descriptions.Item label="manager_id">
          <Text code>{record.manager_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="type">
          <Tag>{record.type}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="status">{getStatusBadge(record.status)}</Descriptions.Item>
        <Descriptions.Item label="pid">{record.pid ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="bootstrap worker_id">
          <Tooltip title="Which worker (or service) brought this manager up. Informational only — not used for message routing.">
            <Text code>{record.worker_id}</Text>
          </Tooltip>
        </Descriptions.Item>
        <Descriptions.Item label="last update">
          {record.last_update ? new Date(record.last_update * 1000).toLocaleString() : "—"}
        </Descriptions.Item>
      </Descriptions>
    ),
  });

  items.push({
    key: "served",
    label: `Served Workers (${record.served_workers?.length ?? 0})`,
    children: record.served_workers && record.served_workers.length > 0 ? (
      <Space wrap size={[4, 4]}>
        {record.served_workers.map((w) => (
          <Tag key={w} color="blue">{w}</Tag>
        ))}
      </Space>
    ) : (
      <Text type="secondary">No workers reported</Text>
    ),
  });

  if (record.metadata && Object.keys(record.metadata).length > 0) {
    items.push({
      key: "metadata",
      label: "Metadata",
      children: (
        <Descriptions size="small" column={2} bordered>
          {Object.entries(record.metadata).map(([k, v]) => (
            <Descriptions.Item key={k} label={k}>
              {typeof v === "object" ? <Text code>{JSON.stringify(v)}</Text> : String(v)}
            </Descriptions.Item>
          ))}
        </Descriptions>
      ),
    });
  }

  return <Collapse items={items} size="small" ghost defaultActiveKey={["identity", "served"]} />;
}

export function InstanceManagersPage() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["instance-managers"],
    queryFn: fetchManagers,
    refetchInterval: 2000,
  });

  const managers = data || [];

  // Aggregate stats for the top bar.
  const totalManagers = managers.length;
  const runningManagers = managers.filter((m) => m.status === "running").length;
  const totalInstances = managers.reduce((sum, m) => sum + (m.instances?.total ?? 0), 0);
  const healthyInstances = managers.reduce((sum, m) => sum + (m.instances?.healthy ?? 0), 0);
  const totalActive = managers.reduce((sum, m) => sum + (m.stats?.active_requests ?? 0), 0);
  const totalRequests = managers.reduce((sum, m) => sum + (m.stats?.total_requests ?? 0), 0);
  const totalErrors = managers.reduce((sum, m) => sum + (m.stats?.errors ?? 0), 0);

  const columns = [
    {
      title: "Manager",
      dataIndex: "manager_id",
      key: "manager_id",
      sorter: (a: ManagerEntry, b: ManagerEntry) =>
        (a.manager_id || "").localeCompare(b.manager_id || ""),
      render: (id: string, record: ManagerEntry) => (
        <Space size={6}>
          {getTypeIcon(record.type)}
          <Text code style={{ fontSize: 12 }}>{id}</Text>
        </Space>
      ),
    },
    {
      title: "Type",
      dataIndex: "type",
      key: "type",
      width: 130,
      filters: [
        { text: "MCP", value: "mcp" },
        { text: "Local Inference", value: "local_inference" },
      ],
      onFilter: (value: any, record: ManagerEntry) => record.type === value,
      render: (type: string) => (
        <Tag color={type === "mcp" ? "purple" : "geekblue"}>{type}</Tag>
      ),
    },
    {
      title: "Status",
      dataIndex: "status",
      key: "status",
      width: 110,
      filters: [
        { text: "Running", value: "running" },
        { text: "Starting", value: "starting" },
        { text: "Stale", value: "stale" },
        { text: "Stopped", value: "stopped" },
        { text: "Error", value: "error" },
      ],
      onFilter: (value: any, record: ManagerEntry) => record.status === value,
      render: (status: string) => (
        <Space size={4}>{getStatusIcon(status)} {status}</Space>
      ),
    },
    {
      title: "Served Workers",
      key: "served_workers",
      render: (_: any, record: ManagerEntry) => {
        const served = record.served_workers || [];
        if (served.length === 0) {
          return <Text type="secondary" style={{ fontSize: 11 }}>none reported</Text>;
        }
        return (
          <Tooltip title={served.join(", ")}>
            <Space size={4} wrap>
              {served.slice(0, 3).map((w) => (
                <Tag key={w} style={{ fontSize: 10 }}>{w}</Tag>
              ))}
              {served.length > 3 && (
                <Tag style={{ fontSize: 10 }}>+{served.length - 3}</Tag>
              )}
            </Space>
          </Tooltip>
        );
      },
    },
    {
      title: "Instances",
      key: "instances",
      width: 110,
      align: "center" as const,
      sorter: (a: ManagerEntry, b: ManagerEntry) =>
        (a.instances?.total ?? 0) - (b.instances?.total ?? 0),
      render: (_: any, record: ManagerEntry) => {
        const total = record.instances?.total ?? 0;
        const healthy = record.instances?.healthy ?? 0;
        const unhealthy = record.instances?.unhealthy ?? 0;
        const hasUnhealthy = unhealthy > 0;
        return (
          <Tooltip title={`${healthy} healthy, ${unhealthy} unhealthy`}>
            <Tag color={hasUnhealthy ? "warning" : total > 0 ? "success" : "default"}>
              {healthy}/{total}
            </Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "Requests",
      key: "requests",
      width: 130,
      align: "center" as const,
      sorter: (a: ManagerEntry, b: ManagerEntry) =>
        (a.stats?.total_requests ?? 0) - (b.stats?.total_requests ?? 0),
      render: (_: any, record: ManagerEntry) => {
        const active = record.stats?.active_requests ?? 0;
        const total = record.stats?.total_requests ?? 0;
        return (
          <Tooltip title={`${active} active, ${total} total`}>
            <Text style={{ fontSize: 12 }}>
              {total} <Text type="secondary" style={{ fontSize: 11 }}>({active} active)</Text>
            </Text>
          </Tooltip>
        );
      },
    },
    {
      title: "Errors",
      key: "errors",
      width: 80,
      align: "center" as const,
      sorter: (a: ManagerEntry, b: ManagerEntry) =>
        (a.stats?.errors ?? 0) - (b.stats?.errors ?? 0),
      render: (_: any, record: ManagerEntry) => {
        const errors = record.stats?.errors ?? 0;
        return errors > 0 ? (
          <Tag color="error">{errors}</Tag>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>0</Text>
        );
      },
    },
    {
      title: "Memory",
      key: "memory",
      width: 90,
      align: "right" as const,
      sorter: (a: ManagerEntry, b: ManagerEntry) =>
        (a.resources?.memory_mb ?? 0) - (b.resources?.memory_mb ?? 0),
      render: (_: any, record: ManagerEntry) => (
        <Text style={{ fontSize: 12 }}>{Math.round(record.resources?.memory_mb ?? 0)} MB</Text>
      ),
    },
    {
      title: "Uptime",
      key: "uptime",
      width: 80,
      align: "right" as const,
      sorter: (a: ManagerEntry, b: ManagerEntry) =>
        (a.stats?.uptime_seconds ?? 0) - (b.stats?.uptime_seconds ?? 0),
      render: (_: any, record: ManagerEntry) => (
        <Text style={{ fontSize: 12 }}>{formatUptime(record.stats?.uptime_seconds ?? 0)}</Text>
      ),
    },
  ];

  return (
    <div>
      <div className="page-header">
        <Title level={2}>
          <ApiOutlined style={{ marginRight: 12 }} />
          Instance Managers
        </Title>
        <Text type="secondary">
          MCP and Local Inference managers. Sibling deployments are listed
          here as first-class entities, independent of any worker.
        </Text>
      </div>

      {/* Compact stats bar */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 16 }}>
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Running</Text>}
            value={runningManagers}
            suffix={<Text type="secondary" style={{ fontSize: 12 }}>/ {totalManagers}</Text>}
            styles={{ content: { fontSize: 18, color: "#52c41a" } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Instances</Text>}
            value={healthyInstances}
            suffix={<Text type="secondary" style={{ fontSize: 12 }}>/ {totalInstances}</Text>}
            styles={{ content: { fontSize: 18 } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Active Requests</Text>}
            value={totalActive}
            styles={{ content: { fontSize: 18, color: totalActive > 0 ? "#1890ff" : undefined } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Total Requests</Text>}
            value={totalRequests}
            styles={{ content: { fontSize: 18 } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Errors</Text>}
            value={totalErrors}
            styles={{ content: { fontSize: 18, color: totalErrors > 0 ? "#ff4d4f" : undefined } }}
          />
        </div>
      </Card>

      {error && (
        <Alert
          type="error"
          showIcon
          title="Failed to load instance managers"
          description={error instanceof Error ? error.message : String(error)}
          style={{ marginBottom: 16 }}
          action={<a onClick={() => refetch()}>Retry</a>}
        />
      )}

      <Card size="small">
        {managers.length === 0 && !isLoading && !error ? (
          <Empty
            description={
              <Space orientation="vertical" size={4}>
                <Text>No instance managers reporting status.</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  Make sure the sibling manager services (<Text code>mcp-manager</Text> and{" "}
                  <Text code>local-inference</Text>) are running and publishing to{" "}
                  <Text code>imf:manager_status:*</Text>.
                </Text>
              </Space>
            }
          />
        ) : (
          <Table
            rowKey={(r) => `${r.type}:${r.manager_id}`}
            dataSource={managers}
            columns={columns}
            loading={isLoading}
            size="small"
            pagination={{ pageSize: 25, hideOnSinglePage: true, showSizeChanger: false }}
            expandable={{
              expandedRowRender: (record) => <ManagerExpandedRow record={record} />,
              rowExpandable: () => true,
            }}
          />
        )}
      </Card>
    </div>
  );
}
