/**
 * Motet - Admin Dashboard - Tasks Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Displays recent command executions from /api/v1/debug/commands.
 *     Sends the manage-app tenant/motet scope as query params.
 *     Shows command type, status, worker assignment, and timing info.
 *     Per-task Flow/JSON/Cancel actions are in the first column; TTL is last.
 *     Cancel calls POST /api/v1/tasks/{task_id}/cancel for running rows.
 */
import { useState, useMemo, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { Typography, Card, Table, Tag, Alert, Button, Statistic, Space, Tooltip, Popconfirm, theme } from "antd";
import { message } from "../antdApp";
import { useQuery } from "@tanstack/react-query";
import {
  UnorderedListOutlined,
  ReloadOutlined,
  ClockCircleOutlined,
  DeleteOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  SyncOutlined,
  CodeOutlined,
  NodeIndexOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { getAuthHeaders } from "../api/http";
import { scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";
import { cancelLiveTask, taskStatusIsCancellable } from "./taskCancel";

const { Title, Text } = Typography;

interface TasksPageProps {
  scope: Scope;
}

// Command info matching the API response from /api/v1/debug/commands
interface CommandInfo {
  command_id: string;
  command_type: string;
  task_id: string;
  conversation_id?: string;
  created_at: string;
  status: string;
  worker_id: string;
  principal_id?: string;
  tenant_id?: string;
  motet_id?: string;
  ttl_remaining: number | null;
}

interface CommandsResponse {
  total_commands: number;
  commands: CommandInfo[];
}

// Aggregated task info for display (one row per task)
interface TaskRow {
  task_id: string;
  conversation_id?: string;
  command_count: number;
  status: string;  // Overall status based on command statuses
  workers: string[];  // Unique workers involved
  created_at: string;  // Earliest command created_at
  latest_at: string;  // Latest command created_at
  ttl_remaining: number | null;  // Min TTL from commands
  duration_ms: number | null;  // Total duration (sum of all command durations)
  success_rate: number;  // Percentage of completed commands (0-1)
  principal_id?: string;  // Principal who initiated the task
  tenant_id?: string;  // Tenant ID
  motet_id?: string;  // Motet ID
}

async function fetchCommands(scope: Scope): Promise<CommandsResponse> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl("/api/v1/debug/commands", scope, { limit: 100 }), { headers });

  if (!response.ok) {
    throw new Error(`Failed to fetch commands: ${response.status}`);
  }

  return response.json();
}

async function copyTaskFlowJson(taskId: string): Promise<void> {
  try {
    const response = await fetch(`/api/v1/debug/task-flow/${encodeURIComponent(taskId)}`, {
      headers: getAuthHeaders(),
    });

    if (!response.ok) {
      let errorMessage = `Failed to load task flow JSON: ${response.status}`;
      try {
        const errorData = await response.json() as { detail?: string };
        if (errorData?.detail) errorMessage = errorData.detail;
      } catch {
        // Ignore JSON parsing failures and fall back to HTTP status text.
      }
      throw new Error(errorMessage);
    }

    const taskFlowText = await response.text();
    await navigator.clipboard.writeText(taskFlowText);
    message.success("Task flow JSON copied to clipboard");
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : "Failed to copy task flow JSON";
    message.error(errorMessage);
  }
}

// Group commands by task_id to create task rows
function groupCommandsIntoTasks(commands: CommandInfo[]): TaskRow[] {
  const taskMap = new Map<string, CommandInfo[]>();

  // Group commands by task_id
  for (const cmd of commands) {
    const taskId = cmd.task_id || cmd.command_id; // Fallback to command_id if no task_id
    if (!taskMap.has(taskId)) {
      taskMap.set(taskId, []);
    }
    taskMap.get(taskId)!.push(cmd);
  }

  // Create TaskRow for each task
  const tasks: TaskRow[] = [];
  for (const [taskId, cmds] of taskMap) {
    // Determine overall status
    const hasRunning = cmds.some(c => ["running", "executing", "pending"].includes(c.status?.toLowerCase()));
    const hasCancelled = cmds.some(c => c.status?.toLowerCase() === "cancelled");
    const hasFailed = cmds.some(c => ["failed", "error"].includes(c.status?.toLowerCase()));
    const allCompleted = cmds.every(c => ["completed", "success"].includes(c.status?.toLowerCase()));

    let status = "unknown";
    if (hasRunning) status = "running";
    else if (hasCancelled) status = "cancelled";
    else if (hasFailed) status = "failed";
    else if (allCompleted) status = "completed";
    else if (cmds.length > 0) status = cmds[0].status;

    // Get unique workers
    const workers = [...new Set(cmds.map(c => c.worker_id).filter(Boolean))];

    // Get time range
    const sortedByTime = [...cmds].sort((a, b) => 
      new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
    );
    const created_at = sortedByTime[0]?.created_at || "";
    const latest_at = sortedByTime[sortedByTime.length - 1]?.created_at || "";

    // Get minimum TTL
    const ttls = cmds.map(c => c.ttl_remaining).filter((t): t is number => t !== null && t >= 0);
    const ttl_remaining = ttls.length > 0 ? Math.min(...ttls) : null;

    // Calculate elapsed time from first to last command timestamp
    // Note: This is elapsed time, not execution time (which requires task-flow API)
    let duration_ms: number | null = null;
    if (created_at && latest_at) {
      const startTime = new Date(created_at).getTime();
      const endTime = new Date(latest_at).getTime();
      if (!isNaN(startTime) && !isNaN(endTime) && endTime >= startTime) {
        duration_ms = endTime - startTime;
      }
    }

    // Calculate success rate (completed commands / total commands)
    const completedCount = cmds.filter(c => ["completed", "success"].includes(c.status?.toLowerCase())).length;
    const success_rate = cmds.length > 0 ? completedCount / cmds.length : 0;

    // Get principal_id, tenant_id, motet_id, conversation_id from first command (they should be consistent across task)
    const principal_id = sortedByTime[0]?.principal_id;
    const tenant_id = sortedByTime[0]?.tenant_id;
    const motet_id = sortedByTime[0]?.motet_id;
    const conversation_id = sortedByTime[0]?.conversation_id;

    tasks.push({
      task_id: taskId,
      conversation_id,
      command_count: cmds.length,
      status,
      workers,
      created_at,
      latest_at,
      ttl_remaining,
      duration_ms,
      success_rate,
      principal_id,
      tenant_id,
      motet_id,
    });
  }

  // Sort by latest_at descending (most recent first)
  return tasks.sort((a, b) => 
    new Date(b.latest_at).getTime() - new Date(a.latest_at).getTime()
  );
}

// Helper to get status color
function getStatusColor(status: string): string {
  switch (status?.toLowerCase()) {
    case "completed":
    case "success":
      return "success";
    case "running":
    case "executing":
    case "pending":
      return "processing";
    case "failed":
    case "error":
      return "error";
    case "cancelled":
      return "warning";
    default:
      return "default";
  }
}

// Helper to get status icon
function getStatusIcon(status: string) {
  switch (status?.toLowerCase()) {
    case "completed":
    case "success":
      return <CheckCircleOutlined style={{ color: "#52c41a" }} />;
    case "running":
    case "executing":
    case "pending":
      return <SyncOutlined spin style={{ color: "#1890ff" }} />;
    case "failed":
    case "error":
      return <CloseCircleOutlined style={{ color: "#ff4d4f" }} />;
    default:
      return null;
  }
}

async function clearAllTasks(): Promise<{ deleted_count: number }> {
  const headers = getAuthHeaders();
  const response = await fetch("/api/v1/debug/tasks/clear", {
    method: "DELETE",
    headers,
  });

  if (!response.ok) {
    throw new Error(`Failed to clear tasks: ${response.status}`);
  }

  return response.json();
}

export function TasksPage({ scope }: TasksPageProps) {
  const navigate = useNavigate();
  const [clearing, setClearing] = useState(false);
  const [cancellingId, setCancellingId] = useState<string | null>(null);

  const { data, isLoading, error, refetch, dataUpdatedAt } = useQuery({
    queryKey: ["debug-tasks", scope.tenantId, scope.motetId],
    queryFn: () => fetchCommands(scope),
    refetchInterval: 2000, // Refresh every 2 seconds
  });

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

  const commands = data?.commands || [];
  const totalCommands = data?.total_commands || 0;
  
  // Group commands into task rows (one row per task)
  const tasks = groupCommandsIntoTasks(commands);

  const handleCancelTask = useCallback(async (taskId: string) => {
    setCancellingId(taskId);
    try {
      await cancelLiveTask(taskId);
      message.success("Cancel requested");
      refetch();
    } catch (err) {
      message.error(err instanceof Error ? err.message : "Failed to cancel task");
    } finally {
      setCancellingId(null);
    }
  }, [refetch]);

  const handleClearAll = async () => {
    setClearing(true);
    try {
      const result = await clearAllTasks();
      message.success(`Cleared ${result.deleted_count || "all"} tasks`);
      refetch();
    } catch (err) {
      message.error(`Failed to clear tasks: ${err}`);
    } finally {
      setClearing(false);
    }
  };

  const columns = useMemo(() => [
    {
      title: "",
      key: "actions",
      width: 168,
      render: (_: unknown, record: TaskRow) => (
        <Space size={4}>
          <Tooltip title="Open task flow">
            <Button
              type="primary"
              ghost
              size="small"
              icon={<NodeIndexOutlined />}
              onClick={() => {
                navigate(`/task-flow?taskId=${encodeURIComponent(record.task_id)}`);
              }}
            >
              Flow
            </Button>
          </Tooltip>
          <Tooltip title="Copy task flow JSON to clipboard">
            <Button
              size="small"
              icon={<CodeOutlined />}
              aria-label="Copy task flow JSON"
              onClick={() => void copyTaskFlowJson(record.task_id)}
            />
          </Tooltip>
          {taskStatusIsCancellable(record.status) && (
            <Popconfirm
              title="Cancel this task?"
              description="The turn stops. An in-flight model call may finish the current generation."
              okText="Cancel task"
              cancelText="Keep running"
              okButtonProps={{ danger: true }}
              onConfirm={() => void handleCancelTask(record.task_id)}
            >
              <Tooltip title="Cancel running task">
                <Button
                  size="small"
                  danger
                  icon={<StopOutlined />}
                  aria-label="Cancel task"
                  loading={cancellingId === record.task_id}
                />
              </Tooltip>
            </Popconfirm>
          )}
        </Space>
      ),
    },
    {
      title: "Task",
      dataIndex: "task_id",
      key: "task_id",
      width: 140,
      render: (id: string) => id ? (
        <Text code copyable={{ text: id }}>
          ...{id?.slice(-5)}
        </Text>
      ) : "—",
    },
    {
      title: "Conversation",
      dataIndex: "conversation_id",
      key: "conversation_id",
      width: 140,
      render: (id: string) => id ? (
        <Text code copyable={{ text: id }}>
          ...{id?.slice(-5)}
        </Text>
      ) : <Text type="secondary">—</Text>,
    },
    {
      title: "Principal",
      dataIndex: "principal_id",
      key: "principal_id",
      width: 140,
      sorter: (a: TaskRow, b: TaskRow) => (a.principal_id || "").localeCompare(b.principal_id || ""),
      render: (principal: string) => principal ? (
        <Tooltip title={principal}>
          <Text code copyable={{ text: principal }}>
            ...{principal.slice(-5)}
          </Text>
        </Tooltip>
      ) : <Text type="secondary">—</Text>,
    },
    {
      title: "# Cmds",
      dataIndex: "command_count",
      key: "command_count",
      width: 100,
      sorter: (a: TaskRow, b: TaskRow) => (a.command_count || 0) - (b.command_count || 0),
      render: (count: number) => (
        <Tag color="blue">{count}</Tag>
      ),
    },
    {
      title: "Status",
      dataIndex: "status",
      key: "status",
      filters: [
        { text: "Completed", value: "completed" },
        { text: "Running", value: "running" },
        { text: "Executing", value: "executing" },
        { text: "Failed", value: "failed" },
        { text: "Cancelled", value: "cancelled" },
      ],
      onFilter: (value: any, record: TaskRow) => record.status?.toLowerCase() === value,
      render: (status: string) => (
        <Tag color={getStatusColor(status)}>
          {status?.toUpperCase() || "UNKNOWN"}
        </Tag>
      ),
    },
    {
      title: "Success",
      dataIndex: "success_rate",
      key: "success_rate",
      width: 90,
      sorter: (a: TaskRow, b: TaskRow) => (a.success_rate || 0) - (b.success_rate || 0),
      render: (rate: number) => (
        <Tag color={rate >= 1 ? "success" : rate >= 0.5 ? "warning" : "error"}>
          {Math.round(rate * 100)}%
        </Tag>
      ),
    },
    {
      title: "Workers",
      dataIndex: "workers",
      key: "workers",
      sorter: (a: TaskRow, b: TaskRow) => (a.workers?.length || 0) - (b.workers?.length || 0),
      render: (workers: string[]) => workers?.length > 0 ? (
        <Tooltip title={workers.join(", ")}>
          <Text code>{workers.length}</Text>
        </Tooltip>
      ) : "—",
    },
    {
      title: "Created",
      dataIndex: "created_at",
      key: "created_at",
      sorter: (a: TaskRow, b: TaskRow) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime(),
      defaultSortOrder: "descend" as const,
      render: (ts: string) => {
        if (!ts) return "—";
        const date = new Date(ts);
        return (
          <Tooltip title={date.toLocaleString()}>
            <Text>{date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</Text>
          </Tooltip>
        );
      },
    },
    {
      title: "TTL",
      dataIndex: "ttl_remaining",
      key: "ttl_remaining",
      width: 110,
      sorter: (a: TaskRow, b: TaskRow) => (a.ttl_remaining || 0) - (b.ttl_remaining || 0),
      render: (ttl: number | null) => {
        if (ttl === null || ttl < 0) return "—";
        const minutes = Math.floor(ttl / 60);
        const seconds = ttl % 60;
        return (
          <Text type="secondary">
            <ClockCircleOutlined style={{ marginRight: 4 }} />
            {minutes}m {seconds}s
          </Text>
        );
      },
    },
  ], [navigate, cancellingId, handleCancelTask]);

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <UnorderedListOutlined style={{ marginRight: 12 }} />
            Tasks
          </Title>
          <Text type="secondary">View recent command executions and task flows</Text>
        </div>
        <Space align="center">
          {dataUpdatedAt && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              Updated: {formatLastUpdated(dataUpdatedAt)}
            </Text>
          )}
          <Popconfirm
            title="Clear All Tasks"
            description="Permanently delete ALL recent tasks from the database? This action cannot be undone."
            onConfirm={handleClearAll}
            okText="Yes, Clear All"
            cancelText="Cancel"
            okButtonProps={{ danger: true }}
          >
            <Button icon={<DeleteOutlined />} danger loading={clearing} disabled={tasks.length === 0}>
              Clear All
            </Button>
          </Popconfirm>
        </Space>
      </div>

      {/* Compact Stats Bar */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "flex-start", alignItems: "center", gap: 32 }}>
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Tasks</Text>}
            value={tasks.length}
            styles={{ content: { fontSize: 18 } }}
          />
          <Statistic
            title={<Text type="secondary" style={{ fontSize: 11 }}>Total Commands</Text>}
            value={totalCommands}
            styles={{ content: { fontSize: 18 } }}
          />
        </div>
      </Card>

      {error && (
        <Alert
          type="error"
          title="Failed to load tasks"
          description={String(error)}
          style={{ marginBottom: 16 }}
        />
      )}

      <Card>
        <Table
          dataSource={tasks}
          columns={columns}
          rowKey="task_id"
          loading={isLoading}
          pagination={{ pageSize: 20 }}
          size="middle"
        />
      </Card>
    </div>
  );
}
