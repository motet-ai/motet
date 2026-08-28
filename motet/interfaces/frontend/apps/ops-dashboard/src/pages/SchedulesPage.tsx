/**
 * Motet - Ops Dashboard - Schedules Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Last Modified: 2026-08-17
 */
import { useState } from "react";
import { Typography, Card, Table, Tag, Button, Space, Alert, Statistic, Form, Input, Select, InputNumber, Popconfirm, Modal, Descriptions, Tooltip, theme, Tabs, DatePicker, Collapse } from "antd";
import { message } from "../antdApp";
import type { Dayjs } from "dayjs";
import { ClockCircleOutlined, PlusOutlined, DeleteOutlined, PauseCircleOutlined, PlayCircleOutlined, InfoCircleOutlined, CheckCircleOutlined, CloseCircleOutlined, SyncOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import { scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface SchedulesPageProps {
  scope: Scope;
}

interface ScheduleStats {
  total_schedules: number;
  status_breakdown: {
    active: number;
    paused: number;
    completed: number;
    failed: number;
  };
  type_breakdown: {
    immediate: number;
    delayed: number;
    recurring: number;
    conditional: number;
  };
  command_type_breakdown: Record<string, number>;
  last_updated: string;
}

interface Schedule {
  schedule_id: string;
  name: string;
  description?: string;
  command_type: string;
  command_data?: Record<string, any>;
  schedule_type: string;
  status: string; // active, paused, completed, failed
  cron_expression?: string;
  interval_seconds?: number;
  next_execution_at?: string;
  last_execution_at?: string;
  execution_count: number;
  consecutive_failures: number;
  max_consecutive_failures?: number;
  created_at?: string;
  tenant_id?: string;
  created_by?: string;
}

interface CommandType {
  type: string;
  description?: string;
  class_name?: string;
}

async function fetchScheduleStats(scope: Scope): Promise<ScheduleStats | null> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(scopedUrl("/api/v1/schedules/stats/summary", scope), { headers });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}

async function fetchSchedules(scope: Scope): Promise<Schedule[]> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(scopedUrl("/api/v1/schedules/", scope), { headers });
    if (!response.ok) return [];
    const data = await response.json();
    return Array.isArray(data) ? data : (data.schedules || []);
  } catch {
    return [];
  }
}

async function fetchCommandTypes(): Promise<CommandType[]> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch("/api/v1/schedules/command-types", { headers });
    if (!response.ok) return [];
    const data = await response.json();
    return Array.isArray(data) ? data : (data.command_types || []);
  } catch {
    return [];
  }
}

function formatApiError(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((e: { loc?: string[]; msg?: string }) => {
        const loc = Array.isArray(e.loc) ? e.loc.join(".") : String(e.loc ?? "");
        const msg = e.msg ?? "Validation error";
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("; ");
  }
  if (detail && typeof detail === "object" && "msg" in detail) {
    return String((detail as { msg?: string }).msg ?? "Validation error");
  }
  return "Request failed";
}

async function createSchedule(data: any): Promise<{ success: boolean; error?: string }> {
  const headers = { ...getAuthHeaders(), "Content-Type": "application/json" };
  
  try {
    const response = await fetch("/api/v1/schedules/", {
      method: "POST",
      headers,
      body: JSON.stringify(data),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      const message = err.detail !== undefined ? formatApiError(err.detail) : response.statusText;
      return { success: false, error: message };
    }
    return { success: true };
  } catch (e) {
    return { success: false, error: String(e) };
  }
}

async function deleteSchedule(scheduleId: string): Promise<boolean> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/schedules/${scheduleId}/delete`, {
      method: "DELETE",
      headers,
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function suspendSchedule(scheduleId: string): Promise<boolean> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/schedules/${scheduleId}/suspend`, {
      method: "POST",
      headers,
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function resumeSchedule(scheduleId: string): Promise<boolean> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/schedules/${scheduleId}/resume`, {
      method: "POST",
      headers,
    });
    return response.ok;
  } catch {
    return false;
  }
}

export function SchedulesPage({ scope }: SchedulesPageProps) {
  const { token: themeToken } = theme.useToken();
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [creating, setCreating] = useState(false);
  const [expandedRows, setExpandedRows] = useState<string[]>([]);
  const [scheduleType, setScheduleType] = useState<"cron" | "interval" | "delayed">("interval");
  const [form] = Form.useForm();
  
  const { data: stats, isLoading: statsLoading, refetch: refetchStats, dataUpdatedAt } = useQuery({
    queryKey: ["schedule-stats", scope.tenantId, scope.motetId],
    queryFn: () => fetchScheduleStats(scope),
    refetchInterval: 2000, // Refresh every 2 seconds
  });

  const { data: schedules, isLoading: schedulesLoading, refetch: refetchSchedules } = useQuery({
    queryKey: ["schedules", scope.tenantId, scope.motetId],
    queryFn: () => fetchSchedules(scope),
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

  const { data: commandTypes } = useQuery({
    queryKey: ["command-types"],
    queryFn: fetchCommandTypes,
    staleTime: 300000, // 5 minutes
  });

  const scheduleList = schedules || [];
  const commandTypeList = commandTypes || [];

  const handleDelete = async (scheduleId: string) => {
    const success = await deleteSchedule(scheduleId);
    if (success) {
      message.success("Schedule deleted");
      refetchSchedules();
      refetchStats();
    } else {
      message.error("Failed to delete schedule");
    }
  };

  const handleSuspend = async (scheduleId: string) => {
    const success = await suspendSchedule(scheduleId);
    if (success) {
      message.success("Schedule suspended");
      refetchSchedules();
      refetchStats();
    } else {
      message.error("Failed to suspend schedule");
    }
  };

  const handleResume = async (scheduleId: string) => {
    const success = await resumeSchedule(scheduleId);
    if (success) {
      message.success("Schedule resumed");
      refetchSchedules();
      refetchStats();
    } else {
      message.error("Failed to resume schedule");
    }
  };

  const handleCreate = async (values: any) => {
    setCreating(true);
    try {
      // Build command_data from JSON string if provided
      let commandData = {};
      if (values.command_data) {
        try {
          commandData = JSON.parse(values.command_data);
        } catch {
          message.error("Invalid JSON in command data");
          return;
        }
      }

      const scheduleData: Record<string, unknown> = {
        name: values.name,
        description: values.description,
        command_type: values.command_type,
        command_data: commandData,
        ...(scheduleType === "delayed"
          ? {
              schedule_type: "delayed",
              scheduled_at: values.scheduled_at
                ? (values.scheduled_at as Dayjs).toISOString()
                : undefined,
            }
          : {
              schedule_type: "recurring",
              ...(scheduleType === "cron"
                ? { cron_expression: values.cron_expression }
                : { interval_seconds: values.interval_seconds }),
            }),
      };

      if (values.target_worker_id?.trim()) scheduleData.target_worker_id = values.target_worker_id.trim();
      if (values.preferred_worker_ids?.length) scheduleData.preferred_worker_ids = values.preferred_worker_ids;
      if (values.worker_affinity?.trim()) scheduleData.worker_affinity = values.worker_affinity.trim();
      if (values.avoid_worker_ids?.length) scheduleData.avoid_worker_ids = values.avoid_worker_ids;
      if (values.timeout_seconds != null && values.timeout_seconds > 0) scheduleData.timeout_seconds = values.timeout_seconds;
      if (values.max_retries != null && values.max_retries >= 0) scheduleData.max_retries = values.max_retries;

      const result = await createSchedule(scheduleData);
      if (result.success) {
        message.success("Schedule created");
        setShowCreateModal(false);
        form.resetFields();
        refetchSchedules();
        refetchStats();
      } else {
        message.error(result.error || "Failed to create schedule");
      }
    } finally {
      setCreating(false);
    }
  };

  const getStatusTag = (schedule: Schedule) => {
    const status = schedule.status?.toLowerCase();
    switch (status) {
      case "active":
        return <Tag color="success" icon={<CheckCircleOutlined />}>Active</Tag>;
      case "paused":
        return <Tag color="warning" icon={<PauseCircleOutlined />}>Paused</Tag>;
      case "failed":
        return <Tag color="error" icon={<CloseCircleOutlined />}>Failed</Tag>;
      case "completed":
        return <Tag color="default" icon={<CheckCircleOutlined />}>Completed</Tag>;
      default:
        return <Tag color="default">{schedule.status || "Unknown"}</Tag>;
    }
  };

  const columns = [
    {
      title: "Name",
      dataIndex: "name",
      key: "name",
      sorter: (a: Schedule, b: Schedule) => (a.name || "").localeCompare(b.name || ""),
      render: (name: string, record: Schedule) => (
        <div>
          <Text strong>{name}</Text>
          {record.description && (
            <div><Text type="secondary" style={{ fontSize: 11 }}>{record.description}</Text></div>
          )}
        </div>
      ),
    },
    {
      title: "Command",
      dataIndex: "command_type",
      key: "command_type",
      sorter: (a: Schedule, b: Schedule) => (a.command_type || "").localeCompare(b.command_type || ""),
      filters: [...new Set(scheduleList.map(s => s.command_type).filter(Boolean))].map(c => ({ text: c, value: c })),
      onFilter: (value: any, record: Schedule) => record.command_type === value,
      render: (cmd: string) => <Tag color="blue">{cmd}</Tag>,
    },
    {
      title: "Schedule",
      key: "schedule",
      sorter: (a: Schedule, b: Schedule) => (a.interval_seconds || 0) - (b.interval_seconds || 0),
      render: (_: any, record: Schedule) => {
        if (record.cron_expression) {
          return <Text code style={{ fontSize: 11 }}>{record.cron_expression}</Text>;
        }
        if (record.interval_seconds) {
          const mins = Math.floor(record.interval_seconds / 60);
          const secs = record.interval_seconds % 60;
          return <Text>Every {mins > 0 ? `${mins}m ` : ""}{secs > 0 ? `${secs}s` : ""}</Text>;
        }
        return "—";
      },
    },
    {
      title: "Status",
      key: "status",
      width: 120,
      filters: [
        { text: "Active", value: "active" },
        { text: "Paused", value: "paused" },
        { text: "Completed", value: "completed" },
        { text: "Failed", value: "failed" },
      ],
      onFilter: (value: any, record: Schedule) => record.status?.toLowerCase() === value,
      render: (_: any, record: Schedule) => getStatusTag(record),
    },
    {
      title: "Next Run",
      dataIndex: "next_execution_at",
      key: "next_execution_at",
      width: 160,
      sorter: (a: Schedule, b: Schedule) => new Date(a.next_execution_at || 0).getTime() - new Date(b.next_execution_at || 0).getTime(),
      render: (ts: string) => ts ? new Date(ts).toLocaleString() : "Not scheduled",
    },
    {
      title: "Runs",
      dataIndex: "execution_count",
      key: "execution_count",
      width: 80,
      sorter: (a: Schedule, b: Schedule) => (a.execution_count || 0) - (b.execution_count || 0),
      render: (count: number) => count?.toLocaleString() || "0",
    },
    {
      title: "Created By",
      dataIndex: "created_by",
      key: "created_by",
      width: 140,
      sorter: (a: Schedule, b: Schedule) => (a.created_by || "").localeCompare(b.created_by || ""),
      render: (principal: string) => principal ? (
        <Tooltip title={principal}>
          <Text style={{ fontSize: 11 }}>👤 {principal.length > 12 ? `${principal.slice(0, 12)}...` : principal}</Text>
        </Tooltip>
      ) : <Text type="secondary">—</Text>,
    },
    {
      title: "Actions",
      key: "actions",
      width: 160,
      render: (_: any, record: Schedule) => (
        <Space size={4}>
          {record.status?.toLowerCase() === "paused" ? (
            <Tooltip title="Resume">
              <Button size="small" icon={<PlayCircleOutlined />} onClick={() => handleResume(record.schedule_id)} />
            </Tooltip>
          ) : record.status?.toLowerCase() === "active" ? (
            <Tooltip title="Suspend">
              <Button size="small" icon={<PauseCircleOutlined />} onClick={() => handleSuspend(record.schedule_id)} />
            </Tooltip>
          ) : null}
          <Popconfirm
            title="Delete Schedule"
            description="Are you sure you want to delete this schedule?"
            onConfirm={() => handleDelete(record.schedule_id)}
            okText="Delete"
            cancelText="Cancel"
            okButtonProps={{ danger: true }}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const expandedRowRender = (record: Schedule) => (
    <div style={{ padding: 8 }}>
      <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} bordered>
        <Descriptions.Item label="Schedule ID">
          <Text code copyable>{record.schedule_id}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="Created">
          {record.created_at ? new Date(record.created_at).toLocaleString() : "—"}
        </Descriptions.Item>
        <Descriptions.Item label="Tenant">
          {record.tenant_id || "—"}
        </Descriptions.Item>
        <Descriptions.Item label="Last Execution">
          {record.last_execution_at ? new Date(record.last_execution_at).toLocaleString() : "Never"}
        </Descriptions.Item>
        <Descriptions.Item label="Failures">
          <Text type={record.consecutive_failures > 0 ? "danger" : undefined}>
            {record.consecutive_failures || 0}
            {record.max_consecutive_failures ? ` / ${record.max_consecutive_failures}` : ""}
          </Text>
        </Descriptions.Item>
        <Descriptions.Item label="Total Executions">{record.execution_count || 0}</Descriptions.Item>
        {record.command_data && Object.keys(record.command_data).length > 0 && (
          <Descriptions.Item label="Command Data" span={3}>
            <pre style={{ 
              fontSize: 10, 
              margin: 0, 
              maxHeight: 150, 
              overflow: "auto",
              background: themeToken.colorFillSecondary,
              padding: 8,
              borderRadius: 4,
            }}>
              {JSON.stringify(record.command_data, null, 2)}
            </pre>
          </Descriptions.Item>
        )}
      </Descriptions>
    </div>
  );

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <ClockCircleOutlined style={{ marginRight: 12 }} />
            Schedules
          </Title>
          <Text type="secondary">Manage scheduled commands and background tasks</Text>
        </div>
        <Space align="center">
          {dataUpdatedAt && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              Updated: {formatLastUpdated(dataUpdatedAt)}
            </Text>
          )}
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setShowCreateModal(true)}>
            Create Schedule
          </Button>
        </Space>
      </div>

      {/* Stats Row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 16 }}>
        <Card size="small">
          <Statistic
            title="Total Schedules"
            value={stats?.total_schedules || scheduleList.length}
          />
        </Card>
        <Card size="small">
          <Statistic
            title="Active"
            value={stats?.status_breakdown?.active || 0}
            styles={{ content: { color: themeToken.colorSuccess } }}
          />
        </Card>
        <Card size="small">
          <Statistic
            title="Recurring"
            value={stats?.type_breakdown?.recurring || 0}
            styles={{ content: { color: themeToken.colorInfo } }}
          />
        </Card>
        <Card size="small">
          <Statistic
            title="Failed"
            value={stats?.status_breakdown?.failed || 0}
            styles={{ content: { color: (stats?.status_breakdown?.failed || 0) > 0 ? themeToken.colorError : undefined } }}
          />
        </Card>
      </div>

      {/* Schedules Table */}
      <Card>
        <Table
          dataSource={scheduleList}
          columns={columns}
          rowKey="schedule_id"
          loading={schedulesLoading}
          pagination={{ pageSize: 15 }}
          expandable={{
            expandedRowRender,
            expandedRowKeys: expandedRows,
            onExpandedRowsChange: (keys) => setExpandedRows(keys as string[]),
          }}
          locale={{ emptyText: "No schedules configured" }}
        />
      </Card>

      {/* Create Schedule Modal */}
      <Modal
        title="Create New Schedule"
        open={showCreateModal}
        onCancel={() => setShowCreateModal(false)}
        footer={null}
        width={600}
      >
        <Form form={form} layout="vertical" onFinish={handleCreate}>
          <Form.Item
            name="name"
            label="Name"
            rules={[{ required: true, message: "Please enter a name" }]}
          >
            <Input placeholder="e.g., Daily Report Generation" />
          </Form.Item>
          
          <Form.Item name="description" label="Description">
            <Input.TextArea placeholder="Optional description" rows={2} />
          </Form.Item>

          <Form.Item
            name="command_type"
            label="Command Type"
            rules={[{ required: true, message: "Please select a command type" }]}
          >
            <Select 
              placeholder="Select command type"
              showSearch
              options={commandTypeList.map(ct => ({ value: ct.type, label: ct.description ? `${ct.type} — ${ct.description}` : ct.type }))}
            />
          </Form.Item>

          <Form.Item label="Schedule Type">
            <Select 
              value={scheduleType} 
              onChange={setScheduleType}
              options={[
                { value: "interval", label: "Interval (every X seconds)" },
                { value: "cron", label: "Cron Expression" },
                { value: "delayed", label: "Delayed (run once at date/time)" },
              ]}
            />
          </Form.Item>

          {scheduleType === "interval" && (
            <Form.Item
              name="interval_seconds"
              label="Interval (seconds)"
              rules={[{ required: true, message: "Please enter interval" }]}
            >
              <InputNumber min={1} max={86400} style={{ width: "100%" }} placeholder="e.g., 3600 for hourly" />
            </Form.Item>
          )}
          {scheduleType === "cron" && (
            <Form.Item
              name="cron_expression"
              label="Cron Expression"
              rules={[{ required: true, message: "Please enter cron expression" }]}
              extra="Format: minute hour day month weekday (e.g., '0 */2 * * *' for every 2 hours)"
            >
              <Input placeholder="e.g., 0 9 * * 1-5" />
            </Form.Item>
          )}
          {scheduleType === "delayed" && (
            <Form.Item
              name="scheduled_at"
              label="Run at (date & time)"
              rules={[{ required: true, message: "Please select date and time" }]}
            >
              <DatePicker showTime format="YYYY-MM-DD HH:mm:ss" style={{ width: "100%" }} />
            </Form.Item>
          )}

          <Form.Item
            name="command_data"
            label="Command Data (JSON)"
            extra="Optional JSON object with command parameters"
          >
            <Input.TextArea 
              placeholder='{"param1": "value1", "param2": 123}'
              rows={4}
              style={{ fontFamily: "monospace" }}
            />
          </Form.Item>

          <Collapse
            items={[
              {
                key: "timeout_retries",
                label: "Timeout & retries (optional)",
                children: (
                  <>
                    <Form.Item
                      name="timeout_seconds"
                      label="Timeout (seconds)"
                      extra="Max seconds per run (default: 300)"
                    >
                      <InputNumber min={1} max={86400} style={{ width: "100%" }} placeholder="300" />
                    </Form.Item>
                    <Form.Item
                      name="max_retries"
                      label="Max retries"
                      extra="Retries on failure (default: 3)"
                    >
                      <InputNumber min={0} max={20} style={{ width: "100%" }} placeholder="3" />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: "worker_targeting",
                label: "Worker targeting (optional)",
                children: (
                  <>
                    <Form.Item name="target_worker_id" label="Target worker ID" extra="Force execution on this worker only">
                      <Input placeholder="e.g., worker-1" allowClear />
                    </Form.Item>
                    <Form.Item name="preferred_worker_ids" label="Preferred worker IDs" extra="Prefer these workers if available">
                      <Select mode="tags" placeholder="Add worker IDs" tokenSeparators={[","]} allowClear />
                    </Form.Item>
                    <Form.Item name="worker_affinity" label="Worker affinity" extra="Affinity key for consistent worker selection">
                      <Input placeholder="e.g., region-us-east" allowClear />
                    </Form.Item>
                    <Form.Item name="avoid_worker_ids" label="Avoid worker IDs" extra="Do not run on these workers">
                      <Select mode="tags" placeholder="Add worker IDs to avoid" tokenSeparators={[","]} allowClear />
                    </Form.Item>
                  </>
                ),
              },
            ]}
          />

          <Form.Item style={{ marginTop: 24 }}>
            <Space>
              <Button type="primary" htmlType="submit" loading={creating}>
                Create Schedule
              </Button>
              <Button onClick={() => setShowCreateModal(false)}>Cancel</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
