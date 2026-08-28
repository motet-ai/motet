/**
 * Motet - Ops Dashboard - Tools Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 */
import { useMemo, useState } from "react";
import { Typography, Card, Table, Alert, Tag, Input, Space } from "antd";
import { ToolOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { namespaceFromQualifiedName } from "@motet/ui-common";
import { getAuthHeaders } from "../api/http";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface ToolsPageProps {
  scope: Scope;
}

interface ToolInfo {
  key: string;
  name: string;
  description?: string;
  hasSchema: boolean;
  namespace: string;
}


async function fetchTools(): Promise<ToolInfo[]> {
  const headers = getAuthHeaders();
  const response = await fetch("/api/v1/tools", { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  const data = (await response.json()) as Record<
    string,
    { description?: string; schema?: Record<string, unknown> | null }
  >;

  return Object.entries(data).map(([name, details]) => {
    const namespace = namespaceFromQualifiedName(name);
    return {
      key: name,
      name,
      description: details?.description,
      hasSchema: Boolean(details?.schema),
      namespace,
    };
  });
}

export function ToolsPage({ scope }: ToolsPageProps) {
  const [search, setSearch] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["tools", scope.tenantId, scope.motetId],
    queryFn: fetchTools,
    refetchInterval: 3000,
  });

  const tools = (data ?? []).slice().sort((a, b) => a.name.localeCompare(b.name));
  const normalizedSearch = search.trim().toLowerCase();
  const filteredTools = useMemo(() => {
    if (!normalizedSearch) {
      return tools;
    }
    return tools.filter((tool) => {
      const haystack = `${tool.name} ${tool.namespace} ${tool.description ?? ""}`.toLowerCase();
      return haystack.includes(normalizedSearch);
    });
  }, [tools, normalizedSearch]);

  return (
    <div>
      <div className="page-header" style={{ marginBottom: 16 }}>
        <Title level={2}>
          <ToolOutlined style={{ marginRight: 12 }} />
          Tools
        </Title>
        <Text type="secondary">Registered tools available for execution and function calling</Text>
      </div>

      {error && (
        <Alert
          title="Error loading tools"
          description={String(error)}
          type="error"
          style={{ marginBottom: 16 }}
        />
      )}

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space size={24} wrap>
          <div>
            <Text type="secondary">Registered Tools: </Text>
            <Text strong>{tools.length}</Text>
          </div>
          <div>
            <Text type="secondary">Visible After Filter: </Text>
            <Text strong>{filteredTools.length}</Text>
          </div>
        </Space>
      </Card>

      <Card>
        <Input.Search
          allowClear
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by tool id, namespace, or description"
          style={{ maxWidth: 520, marginBottom: 12 }}
        />
        <Table
          rowKey="key"
          loading={isLoading}
          dataSource={filteredTools}
          pagination={{ pageSize: 25 }}
          columns={[
            {
              title: "Tool Id",
              dataIndex: "name",
              key: "name",
              width: 315,
              render: (name: string) => <Text code>{name}</Text>,
            },
            {
              title: "Namespace",
              dataIndex: "namespace",
              key: "namespace",
              width: 140,
              render: (namespace: string) => <Tag>{namespace}</Tag>,
            },
            {
              title: "Schema",
              dataIndex: "hasSchema",
              key: "hasSchema",
              width: 120,
              render: (hasSchema: boolean) => (
                <Tag color={hasSchema ? "green" : "default"}>{hasSchema ? "yes" : "no"}</Tag>
              ),
            },
            {
              title: "Description",
              dataIndex: "description",
              key: "description",
              render: (description: string | undefined) => description || <Text type="secondary">—</Text>,
            },
          ]}
          locale={{ emptyText: "No tools registered" }}
        />
      </Card>
    </div>
  );
}
