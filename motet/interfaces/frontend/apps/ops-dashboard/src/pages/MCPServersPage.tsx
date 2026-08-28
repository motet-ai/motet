/**
 * Motet - Admin Dashboard - MCP Servers Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Per-service MCP health (weather, playwright, google_workspace, …).
 *     Instance Managers remains the sibling process view; this page is one
 *     row per configured service_id including configured-but-down.
 *
 *     Data source: GET /api/v1/mcp/servers (JWT required; same contract as
 *     /api/v1/workers/managers/status). Actions enqueue Redis control
 *     commands (restart / disable / enable) and require admin.
 */
import { Typography, Card, Table, Tag, Alert, Statistic, Tooltip, Badge, Space, Descriptions, Collapse, Empty, Button, Popconfirm } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiOutlined, CheckCircleOutlined, CloseCircleOutlined, SyncOutlined, PauseCircleOutlined } from "@ant-design/icons";
import { Link } from "react-router-dom";
import { getAuthHeaders } from "../api/http";

const { Title, Text } = Typography;

interface MCPServerEntry {
  service_id: string;
  manager_id: string;
  status: string;
  healthy: boolean;
  transport: string;
  visibility?: string | null;
  lifecycle_duration?: string | null;
  state_model?: string | null;
  auth_type: string;
  instance_count: number;
  instance_ids: string[];
  pids: number[];
  restart_count_window: number;
  restart_budget_remaining: number;
  last_error?: string | null;
  last_ready_at?: number | null;
  last_removed_at?: number | null;
  last_restarted_at?: number | null;
  tool_names: string[];
  tool_count: number;
  updated_at: number;
  disabled: boolean;
}

interface MCPServersResponse {
  status: string;
  servers: MCPServerEntry[];
  timestamp: number;
}


async function fetchServers(): Promise<MCPServerEntry[]> {
  const response = await fetch("/api/v1/mcp/servers", { headers: getAuthHeaders() });
  if (!response.ok) {
    throw new Error(`Failed to fetch MCP servers: ${response.status}`);
  }
  const data: MCPServersResponse = await response.json();
  if (data.status !== "success") {
    throw new Error("API returned error status");
  }
  return data.servers || [];
}

async function postAction(serviceId: string, action: "restart" | "disable" | "enable"): Promise<void> {
  const response = await fetch(`/api/v1/mcp/servers/${encodeURIComponent(serviceId)}/${action}`, {
    method: "POST",
    headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
    body: "{}",
  });
  if (!response.ok && response.status !== 202) {
    const text = await response.text();
    throw new Error(text || `Action failed: ${response.status}`);
  }
}

function formatTime(ts?: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function statusBadge(status: string) {
  switch (status) {
    case "running":
      return <Badge status="success" text={status} />;
    case "starting":
      return <Badge status="processing" text={status} />;
    case "failed":
      return <Badge status="error" text={status} />;
    case "disabled":
      return <Badge status="default" text={status} />;
    case "auth_required":
      return <Badge status="warning" text={status} />;
    default:
      return <Badge status="default" text={status || "unknown"} />;
  }
}

function statusIcon(status: string) {
  switch (status) {
    case "running":
      return <CheckCircleOutlined style={{ color: "#52c41a" }} />;
    case "starting":
      return <SyncOutlined spin />;
    case "failed":
      return <CloseCircleOutlined style={{ color: "#ff4d4f" }} />;
    case "disabled":
      return <PauseCircleOutlined style={{ color: "#999" }} />;
    default:
      return <ApiOutlined />;
  }
}

function ExpandedRow({ record }: { record: MCPServerEntry }) {
  return (
    <Collapse
      size="small"
      ghost
      defaultActiveKey={["meta", "instances"]}
      items={[
        {
          key: "meta",
          label: "Isolation & auth",
          children: (
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="visibility">{record.visibility || "—"}</Descriptions.Item>
              <Descriptions.Item label="lifecycle">{record.lifecycle_duration || "—"}</Descriptions.Item>
              <Descriptions.Item label="state_model">{record.state_model || "—"}</Descriptions.Item>
              <Descriptions.Item label="auth">{record.auth_type}</Descriptions.Item>
              <Descriptions.Item label="last ready">{formatTime(record.last_ready_at)}</Descriptions.Item>
              <Descriptions.Item label="last restart">{formatTime(record.last_restarted_at)}</Descriptions.Item>
              <Descriptions.Item label="last error" span={2}>
                {record.last_error || "—"}
              </Descriptions.Item>
            </Descriptions>
          ),
        },
        {
          key: "instances",
          label: `Instances (${record.instance_ids.length})`,
          children: record.instance_ids.length ? (
            <Space wrap>
              {record.instance_ids.map((id) => (
                <Tag key={id} style={{ fontSize: 11 }}>{id}</Tag>
              ))}
            </Space>
          ) : (
            <Text type="secondary">No live instances</Text>
          ),
        },
        {
          key: "tools",
          label: `Tools (${record.tool_count})`,
          children: record.tool_names.length ? (
            <Space wrap>
              {record.tool_names.map((n) => (
                <Tag key={n}>{n}</Tag>
              ))}
            </Space>
          ) : (
            <Text type="secondary">None captured at discovery</Text>
          ),
        },
      ]}
    />
  );
}

export function MCPServersPage() {
  const queryClient = useQueryClient();
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["mcp-servers"],
    queryFn: fetchServers,
    refetchInterval: 2000,
  });
  const servers = data || [];

  const action = useMutation({
    mutationFn: ({ serviceId, op }: { serviceId: string; op: "restart" | "disable" | "enable" }) =>
      postAction(serviceId, op),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["mcp-servers"] }),
  });

  const running = servers.filter((s) => s.status === "running").length;
  const failed = servers.filter((s) => s.status === "failed").length;
  const disabled = servers.filter((s) => s.disabled || s.status === "disabled").length;

  const columns = [
    {
      title: "Service",
      dataIndex: "service_id",
      key: "service_id",
      sorter: (a: MCPServerEntry, b: MCPServerEntry) => a.service_id.localeCompare(b.service_id),
      render: (id: string) => <Text code style={{ fontSize: 12 }}>{id}</Text>,
    },
    {
      title: "Status",
      dataIndex: "status",
      key: "status",
      width: 140,
      filters: [
        { text: "Running", value: "running" },
        { text: "Failed", value: "failed" },
        { text: "Disabled", value: "disabled" },
        { text: "Not started", value: "not_started" },
      ],
      onFilter: (value: unknown, record: MCPServerEntry) => record.status === value,
      render: (status: string) => (
        <Space size={4}>{statusIcon(status)} {statusBadge(status)}</Space>
      ),
    },
    {
      title: "Transport",
      dataIndex: "transport",
      key: "transport",
      width: 110,
      render: (t: string) => <Tag>{t}</Tag>,
    },
    {
      title: "Instances",
      key: "instances",
      width: 90,
      align: "center" as const,
      render: (_: unknown, record: MCPServerEntry) => (
        <Tag color={record.healthy ? "success" : record.instance_count ? "warning" : "default"}>
          {record.instance_count}
        </Tag>
      ),
    },
    {
      title: "Restarts",
      key: "restarts",
      width: 110,
      align: "center" as const,
      render: (_: unknown, record: MCPServerEntry) => (
        <Tooltip title={`${record.restart_budget_remaining} remaining in window`}>
          <Text style={{ fontSize: 12 }}>
            {record.restart_count_window}
            <Text type="secondary"> / {record.restart_count_window + record.restart_budget_remaining}</Text>
          </Text>
        </Tooltip>
      ),
    },
    {
      title: "Tools",
      dataIndex: "tool_count",
      key: "tool_count",
      width: 70,
      align: "center" as const,
    },
    {
      title: "Manager",
      dataIndex: "manager_id",
      key: "manager_id",
      render: (id: string) => (
        <Link to="/instance-managers">
          <Text code style={{ fontSize: 11 }}>{id}</Text>
        </Link>
      ),
    },
    {
      title: "Actions",
      key: "actions",
      width: 220,
      render: (_: unknown, record: MCPServerEntry) => (
        <Space size={4}>
          <Popconfirm
            title={`Restart ${record.service_id}?`}
            onConfirm={() => action.mutate({ serviceId: record.service_id, op: "restart" })}
          >
            <Button size="small" disabled={record.disabled}>Restart</Button>
          </Popconfirm>
          {record.disabled ? (
            <Button
              size="small"
              onClick={() => action.mutate({ serviceId: record.service_id, op: "enable" })}
            >
              Enable
            </Button>
          ) : (
            <Popconfirm
              title={`Disable ${record.service_id}?`}
              onConfirm={() => action.mutate({ serviceId: record.service_id, op: "disable" })}
            >
              <Button size="small">Disable</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div className="page-header">
        <Title level={2}>
          <ApiOutlined style={{ marginRight: 12 }} />
          MCP Servers
        </Title>
        <Text type="secondary">
          Per-service health for the sibling MCP manager. A failed Playwright
          row does not mean weather is down.
        </Text>
      </div>

      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 16 }}>
          <Statistic title={<Text type="secondary" style={{ fontSize: 11 }}>Running</Text>} value={running} suffix={<Text type="secondary">/ {servers.length}</Text>} styles={{ content: { fontSize: 18, color: "#52c41a" } }} />
          <Statistic title={<Text type="secondary" style={{ fontSize: 11 }}>Failed</Text>} value={failed} styles={{ content: { fontSize: 18, color: failed ? "#ff4d4f" : undefined } }} />
          <Statistic title={<Text type="secondary" style={{ fontSize: 11 }}>Disabled</Text>} value={disabled} styles={{ content: { fontSize: 18 } }} />
        </div>
      </Card>

      {error && (
        <Alert
          type="error"
          showIcon
          title="Failed to load MCP servers"
          description={error instanceof Error ? error.message : String(error)}
          style={{ marginBottom: 16 }}
          action={<a onClick={() => refetch()}>Retry</a>}
        />
      )}
      {action.isError && (
        <Alert
          type="error"
          showIcon
          title="MCP action failed"
          description={action.error instanceof Error ? action.error.message : String(action.error)}
          style={{ marginBottom: 16 }}
        />
      )}

      <Card size="small">
        {servers.length === 0 && !isLoading && !error ? (
          <Empty
            description={
              <Space orientation="vertical" size={4}>
                <Text>No MCP services reporting status.</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  The sibling <Text code>mcp-manager</Text> publishes per-service
                  records to <Text code>imf:manager_status:*:mcp:services</Text>.
                </Text>
              </Space>
            }
          />
        ) : (
          <Table
            rowKey={(r) => `${r.manager_id}:${r.service_id}`}
            dataSource={servers}
            columns={columns}
            loading={isLoading}
            size="small"
            pagination={{ pageSize: 25, hideOnSinglePage: true, showSizeChanger: false }}
            expandable={{
              expandedRowRender: (record) => <ExpandedRow record={record} />,
            }}
          />
        )}
      </Card>
    </div>
  );
}
