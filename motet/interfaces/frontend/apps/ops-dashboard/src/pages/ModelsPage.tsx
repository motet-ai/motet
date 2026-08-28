/**
 * Motet - Ops Dashboard - Models Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 */
import { useState } from "react";
import { Typography, Card, Table, Tag, Button, Space, Alert, Tabs, Descriptions, Collapse, Tooltip, theme } from "antd";
import { ApiOutlined, CheckCircleOutlined, InfoCircleOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface ModelsPageProps {
  scope: Scope;
}

interface ModelSpec {
  model_id: string;
  provider?: string;
  display_name?: string;
  description?: string;
  capabilities?: string[];
  supported_adapters?: string[];
  context_window?: number;
  max_output_tokens?: number;
  input_cost_per_1k?: number;
  output_cost_per_1k?: number;
  supports_streaming?: boolean;
  supports_tools?: boolean;
  supports_vision?: boolean;
  default_temperature?: number;
  aliases?: string[];
}

interface ModelProfile {
  name: string;
  tenant_id?: string;
  routing?: Record<string, any>;
  policies?: Record<string, any>;
  [key: string]: any;
}


async function fetchModelSpecs(): Promise<ModelSpec[]> {
  const headers = getAuthHeaders();
  
  const response = await fetch("/api/v1/models", { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  const data = await response.json();
  
  // Handle different response formats
  if (Array.isArray(data)) {
    return data;
  }
  if (data.models) {
    return data.models;
  }
  return [];
}

async function fetchModelProfile(name: string): Promise<ModelProfile | null> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/models/profiles/${encodeURIComponent(name)}`, { headers });
    if (!response.ok) {
      return null;
    }
    return await response.json();
  } catch {
    return null;
  }
}

export function ModelsPage({ scope }: ModelsPageProps) {
  const { token: themeToken } = theme.useToken();
  const [expandedRows, setExpandedRows] = useState<string[]>([]);
  const [profileName, setProfileName] = useState("default");
  
  const { data: models, isLoading, error, refetch, dataUpdatedAt } = useQuery({
    queryKey: ["models", scope.tenantId, scope.motetId],
    queryFn: fetchModelSpecs,
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

  const { data: profile, isLoading: profileLoading, refetch: refetchProfile } = useQuery({
    queryKey: ["model-profile", profileName, scope.tenantId],
    queryFn: () => fetchModelProfile(profileName),
  });

  const modelList = models || [];

  const columns = [
    {
      title: "Model ID",
      dataIndex: "model_id",
      key: "model_id",
      sorter: (a: ModelSpec, b: ModelSpec) => (a.model_id || "").localeCompare(b.model_id || ""),
      render: (id: string, record: ModelSpec) => (
        <div>
          <Text strong style={{ fontFamily: "monospace" }}>{id}</Text>
          {record.display_name && record.display_name !== id && (
            <div><Text type="secondary" style={{ fontSize: 11 }}>{record.display_name}</Text></div>
          )}
        </div>
      ),
    },
    {
      title: "Provider",
      dataIndex: "provider",
      key: "provider",
      width: 120,
      sorter: (a: ModelSpec, b: ModelSpec) => (a.provider || "").localeCompare(b.provider || ""),
      filters: [...new Set(modelList.map(m => m.provider).filter(Boolean))].map(p => ({ text: p, value: p })),
      onFilter: (value: any, record: ModelSpec) => record.provider === value,
      render: (p: string) => p ? <Tag color="blue">{p}</Tag> : <Text type="secondary">—</Text>,
    },
    {
      title: "Context",
      dataIndex: "context_window",
      key: "context_window",
      width: 100,
      sorter: (a: ModelSpec, b: ModelSpec) => (a.context_window || 0) - (b.context_window || 0),
      render: (ctx: number) => ctx ? (
        <Tooltip title={`${ctx.toLocaleString()} tokens`}>
          <Text>{(ctx / 1000).toFixed(0)}K</Text>
        </Tooltip>
      ) : <Text type="secondary">—</Text>,
    },
    {
      title: "Capabilities",
      key: "capabilities",
      width: 180,
      render: (_: any, record: ModelSpec) => (
        <Space size={4} wrap>
          {record.supports_streaming && <Tag color="green" style={{ fontSize: 10 }}>Stream</Tag>}
          {record.supports_tools && <Tag color="purple" style={{ fontSize: 10 }}>Tools</Tag>}
          {record.supports_vision && <Tag color="orange" style={{ fontSize: 10 }}>Vision</Tag>}
        </Space>
      ),
    },
    {
      title: "Cost ($/1K)",
      key: "cost",
      width: 140,
      sorter: (a: ModelSpec, b: ModelSpec) => ((a.input_cost_per_1k || 0) + (a.output_cost_per_1k || 0)) - ((b.input_cost_per_1k || 0) + (b.output_cost_per_1k || 0)),
      render: (_: any, record: ModelSpec) => {
        if (!record.input_cost_per_1k && !record.output_cost_per_1k) {
          return <Text type="secondary">—</Text>;
        }
        return (
          <Tooltip title={`Input: $${record.input_cost_per_1k || 0}/1K, Output: $${record.output_cost_per_1k || 0}/1K`}>
            <Text style={{ fontSize: 11 }}>
              In: ${record.input_cost_per_1k?.toFixed(4) || "—"} / Out: ${record.output_cost_per_1k?.toFixed(4) || "—"}
            </Text>
          </Tooltip>
        );
      },
    },
  ];

  const expandedRowRender = (record: ModelSpec) => (
    <div style={{ padding: 8 }}>
      <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }} bordered>
        {record.description && (
          <Descriptions.Item label="Description" span={3}>{record.description}</Descriptions.Item>
        )}
        {record.capabilities && record.capabilities.length > 0 && (
          <Descriptions.Item label="Capabilities" span={3}>
            <Space wrap>
              {record.capabilities.map(c => (
                <Tag key={c} color="cyan">{c}</Tag>
              ))}
            </Space>
          </Descriptions.Item>
        )}
        {record.supported_adapters && record.supported_adapters.length > 0 && (
          <Descriptions.Item label="Adapters" span={3}>
            <Space wrap>
              {record.supported_adapters.map(a => (
                <Tag key={a}>{a}</Tag>
              ))}
            </Space>
          </Descriptions.Item>
        )}
        {record.aliases && record.aliases.length > 0 && (
          <Descriptions.Item label="Aliases" span={3}>
            <Space wrap>
              {record.aliases.map(a => (
                <Tag key={a} color="default">{a}</Tag>
              ))}
            </Space>
          </Descriptions.Item>
        )}
        <Descriptions.Item label="Max Output">{record.max_output_tokens?.toLocaleString() || "—"}</Descriptions.Item>
        <Descriptions.Item label="Default Temp">{record.default_temperature ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="Context Window">{record.context_window?.toLocaleString() || "—"}</Descriptions.Item>
      </Descriptions>
    </div>
  );

  const tabItems = [
    {
      key: "specs",
      label: "Model Specs",
      children: (
        <>
          {error && (
            <Alert
              title="Error loading models"
              description={String(error)}
              type="error"
              style={{ marginBottom: 16 }}
            />
          )}
          <Table
            dataSource={modelList}
            columns={columns}
            rowKey={(record) => `${record.provider}:${record.model_id ?? record.name}`}
            loading={isLoading}
            pagination={{ pageSize: 15 }}
            expandable={{
              expandedRowRender,
              expandedRowKeys: expandedRows,
              onExpandedRowsChange: (keys) => setExpandedRows(keys as string[]),
            }}
            locale={{ emptyText: "No models available" }}
          />
        </>
      ),
    },
    {
      key: "profile",
      label: "Model Profile",
      children: (
        <div>
          <div style={{ marginBottom: 16, display: "flex", gap: 8, alignItems: "center" }}>
            <Text>Profile Name:</Text>
            <input
              type="text"
              value={profileName}
              onChange={(e) => setProfileName(e.target.value)}
              style={{ 
                padding: "4px 8px", 
                borderRadius: 4, 
                border: `1px solid ${themeToken.colorBorder}`,
                background: themeToken.colorBgContainer,
                color: themeToken.colorText,
              }}
            />
            <Button size="small" onClick={() => refetchProfile()} loading={profileLoading}>
              Load Profile
            </Button>
          </div>
          <Card size="small">
            <pre style={{ 
              fontSize: 12, 
              margin: 0, 
              maxHeight: 500, 
              overflow: "auto",
              background: themeToken.colorFillSecondary,
              padding: 12,
              borderRadius: 4,
            }}>
              {profile ? JSON.stringify(profile, null, 2) : "No profile loaded or profile not found"}
            </pre>
          </Card>
          <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
            <InfoCircleOutlined /> Source: GET /api/v1/models/profiles/{profileName}
          </Text>
        </div>
      ),
    },
  ];

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <ApiOutlined style={{ marginRight: 12 }} />
            Models
          </Title>
          <Text type="secondary">Available models and their capabilities</Text>
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
            <Text type="secondary">Total Models: </Text>
            <Text strong>{modelList.length}</Text>
          </div>
          <div>
            <Text type="secondary">With Tools: </Text>
            <Text strong>{modelList.filter(m => m.supports_tools).length}</Text>
          </div>
          <div>
            <Text type="secondary">With Vision: </Text>
            <Text strong>{modelList.filter(m => m.supports_vision).length}</Text>
          </div>
        </div>
      </Card>

      <Card>
        <Tabs items={tabItems} />
      </Card>
    </div>
  );
}
