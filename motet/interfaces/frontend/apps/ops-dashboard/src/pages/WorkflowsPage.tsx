/**
 * Motet - Ops Dashboard - Workflows Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 */
import { useState } from "react";
import {
  Typography,
  Card,
  Table,
  Tag,
  Space,
  Alert,
  Descriptions,
  Collapse,
  Button,
  Popconfirm,
  Tooltip,
} from "antd";
import { message } from "../antdApp";
import { ApartmentOutlined, DeleteOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

function isUserWorkflow(workflowId: string): boolean {
  return workflowId.startsWith("user.");
}

interface WorkflowsPageProps {
  scope: Scope;
}

interface WorkflowStep {
  step_id: string;
  name: string;
  command_type: string;
  command_data?: Record<string, any>;
  dependencies?: string[];
  execution_context?: Record<string, any>;
}

interface WorkflowInfo {
  workflow_id: string;
  name: string;
  description?: string;
  step_count: number;
  source?: string;
  bundle_id?: string;
  input_parameters?: Record<string, unknown> | null;
  required_inputs?: string[] | null;
  use_for?: string[] | null;
  output_field?: string | null;
  trigger_patterns?: string[];
  steps?: Record<string, WorkflowStep>;
  execution_order?: string[];
}

interface WorkflowsResponse {
  registered_workflows: WorkflowInfo[];
}


async function fetchWorkflows(): Promise<WorkflowsResponse> {
  const headers = getAuthHeaders();
  
  const response = await fetch("/api/v1/workflows", { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  const data = await response.json();
  
  // API returns { registered_workflows: [...] }
  return { 
    registered_workflows: data.registered_workflows || [] 
  };
}

async function deleteUserWorkflow(workflowId: string): Promise<void> {
  const headers = getAuthHeaders();
  const response = await fetch(`/api/v1/workflows/${encodeURIComponent(workflowId)}`, {
    method: "DELETE",
    headers,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      const apiDetail = body?.detail;
      if (typeof apiDetail === "string") {
        detail = apiDetail;
      } else if (apiDetail?.errors?.[0]?.message) {
        detail = apiDetail.errors[0].message;
      } else if (typeof body?.message === "string") {
        detail = body.message;
      }
    } catch {
      // keep status text
    }
    throw new Error(detail);
  }
}

export function WorkflowsPage({ scope }: WorkflowsPageProps) {
  const [expandedRows, setExpandedRows] = useState<string[]>([]);
  const queryClient = useQueryClient();
  
  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["workflows", scope.tenantId, scope.motetId],
    queryFn: fetchWorkflows,
    refetchInterval: 2000, // Refresh every 2 seconds
  });

  const deleteMutation = useMutation({
    mutationFn: deleteUserWorkflow,
    onSuccess: (_result, workflowId) => {
      message.success(`Unregistered ${workflowId}`);
      void queryClient.invalidateQueries({
        queryKey: ["workflows", scope.tenantId, scope.motetId],
      });
    },
    onError: (err: Error) => {
      message.error(`Failed to unregister workflow: ${err.message}`);
    },
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

  const workflows = data?.registered_workflows || [];
  const totalCount = workflows.length;

  const columns = [
    {
      title: "Workflow ID",
      dataIndex: "workflow_id",
      key: "workflow_id",
      sorter: (a: WorkflowInfo, b: WorkflowInfo) => (a.workflow_id || "").localeCompare(b.workflow_id || ""),
      render: (id: string) => (
        <Text code style={{ fontSize: 13, fontWeight: 600 }}>{id}</Text>
      ),
    },
    {
      title: "Description",
      dataIndex: "description",
      key: "description",
      ellipsis: true,
      render: (desc: string) => desc || <Text type="secondary">—</Text>,
    },
    {
      title: "Steps",
      dataIndex: "step_count",
      key: "step_count",
      width: 100,
      sorter: (a: WorkflowInfo, b: WorkflowInfo) => (a.step_count || 0) - (b.step_count || 0),
      render: (count: number) => (
        <Tag color={count > 0 ? "blue" : "default"}>{count} steps</Tag>
      ),
    },
    {
      title: "Actions",
      key: "actions",
      width: 100,
      render: (_: unknown, record: WorkflowInfo) => {
        const canDelete = isUserWorkflow(record.workflow_id);
        if (!canDelete) {
          return (
            <Tooltip title="Only user.* workflows can be unregistered">
              <Button size="small" danger icon={<DeleteOutlined />} disabled />
            </Tooltip>
          );
        }
        return (
          <Popconfirm
            title="Unregister workflow"
            description={`Remove ${record.workflow_id} from the catalog and worker registries?`}
            onConfirm={() => deleteMutation.mutate(record.workflow_id)}
            okText="Delete"
            cancelText="Cancel"
            okButtonProps={{ danger: true }}
          >
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              loading={
                deleteMutation.isPending &&
                deleteMutation.variables === record.workflow_id
              }
            />
          </Popconfirm>
        );
      },
    },
  ];

  const expandedRowRender = (record: WorkflowInfo) => (
    <div style={{ padding: 8 }}>
      <Descriptions size="small" column={1} bordered>
        {record.description && (
          <Descriptions.Item label="Description">{record.description}</Descriptions.Item>
        )}
        <Descriptions.Item label="Bundle">
          <Text code>{record.bundle_id || "core"}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="Source">
          <Tag color={record.source === "registry" ? "green" : "gold"}>
            {record.source || "unknown"}
          </Tag>
        </Descriptions.Item>
        <Descriptions.Item label="Output Field">
          {record.output_field ? <Text code>{record.output_field}</Text> : <Text type="secondary">—</Text>}
        </Descriptions.Item>
        <Descriptions.Item label="Use For">
          {record.use_for && record.use_for.length > 0 ? (
            <Space wrap>
              {record.use_for.map((item) => (
                <Tag key={item} color="blue">{item}</Tag>
              ))}
            </Space>
          ) : (
            <Text type="secondary">—</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="Required Inputs">
          {record.required_inputs && record.required_inputs.length > 0 ? (
            <Space wrap>
              {record.required_inputs.map((item) => (
                <Tag key={item} color="purple">{item}</Tag>
              ))}
            </Space>
          ) : (
            <Text type="secondary">—</Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="Input Parameters">
          {record.input_parameters && Object.keys(record.input_parameters).length > 0 ? (
            <pre style={{ fontSize: 11, margin: 0, maxHeight: 220, overflow: "auto" }}>
              {JSON.stringify(record.input_parameters, null, 2)}
            </pre>
          ) : (
            <Text type="secondary">—</Text>
          )}
        </Descriptions.Item>
        {record.execution_order && record.execution_order.length > 0 && (
          <Descriptions.Item label="Execution Order">
            <Space wrap>
              {record.execution_order.map((stepId, idx) => (
                <Tag key={stepId} color="blue">{idx + 1}. {stepId}</Tag>
              ))}
            </Space>
          </Descriptions.Item>
        )}
        {record.steps && Object.keys(record.steps).length > 0 && (
          <Descriptions.Item label="Steps">
            <div style={{ display: "grid", gap: 8 }}>
              {Object.entries(record.steps).map(([stepId, step]) => (
                <div key={stepId} style={{ 
                  background: "rgba(0,0,0,0.02)", 
                  padding: "8px 12px", 
                  borderRadius: 4,
                  border: "1px solid rgba(0,0,0,0.06)"
                }}>
                  <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
                    <Text code>{step.step_id}</Text>
                    {step.name && <Text strong>{step.name}</Text>}
                  </div>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <Tag color="purple">{step.command_type}</Tag>
                    {step.dependencies && step.dependencies.length > 0 && (
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        depends on: {step.dependencies.join(", ")}
                      </Text>
                    )}
                  </div>
                  {step.command_data && Object.keys(step.command_data).length > 0 && (
                    <Collapse size="small" style={{ marginTop: 8 }}>
                      <Collapse.Panel header="Command Data" key="1">
                        <pre style={{ fontSize: 10, margin: 0, maxHeight: 150, overflow: "auto" }}>
                          {JSON.stringify(step.command_data, null, 2)}
                        </pre>
                      </Collapse.Panel>
                    </Collapse>
                  )}
                </div>
              ))}
            </div>
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
            <ApartmentOutlined style={{ marginRight: 12 }} />
            Workflows
          </Title>
          <Text type="secondary">Registered workflows available for LLM function calling</Text>
        </div>
        {dataUpdatedAt && (
          <Text type="secondary" style={{ fontSize: 11 }}>
            Updated: {formatLastUpdated(dataUpdatedAt)}
          </Text>
        )}
      </div>

      {/* Stats Row */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 24, alignItems: "center" }}>
          <div>
            <Text type="secondary">Registered Workflows: </Text>
            <Text strong>{totalCount}</Text>
          </div>
          <div>
            <Text type="secondary">Total Steps: </Text>
            <Text strong>{workflows.reduce((sum, w) => sum + (w.step_count || 0), 0)}</Text>
          </div>
        </div>
      </Card>

      {error && (
        <Alert
          title="Error loading workflows"
          description={String(error)}
          type="error"
          style={{ marginBottom: 16 }}
        />
      )}

      <Card>
        <Table
          dataSource={workflows}
          columns={columns}
          rowKey="workflow_id"
          loading={isLoading}
          pagination={{ pageSize: 20 }}
          expandable={{
            expandedRowRender,
            expandedRowKeys: expandedRows,
            onExpandedRowsChange: (keys) => setExpandedRows(keys as string[]),
          }}
          locale={{ emptyText: "No workflows registered" }}
        />
      </Card>
    </div>
  );
}
