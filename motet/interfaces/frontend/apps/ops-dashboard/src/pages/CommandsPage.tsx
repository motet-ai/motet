/**
 * Motet - Ops Dashboard - Commands Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Lists all registered command types (core + bundle) from GET /api/v1/commands.
 * Shows discovery ``description`` when the API includes it (CommandRegistration).
 */
import { useMemo, useState } from "react";
import { Typography, Card, Table, Alert, Tag, Input } from "antd";
import { ThunderboltOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { namespaceFromQualifiedName } from "@motet/ui-common";
import { getAuthHeaders } from "../api/http";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface CommandsPageProps {
  scope: Scope;
}

interface CommandInfo {
  key: string;
  command_type: string;
  implementation_type: string;
  bundle_id?: string;
  description?: string;
  namespace: string;
}


async function fetchCommands(): Promise<CommandInfo[]> {
  const headers = getAuthHeaders();
  const response = await fetch("/api/v1/commands", { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  const data = await response.json();
  const list: Array<Record<string, unknown>> = Array.isArray(data)
    ? data
    : Array.isArray((data as { commands?: unknown })?.commands)
      ? (data as { commands: Array<Record<string, unknown>> }).commands
      : Array.isArray((data as { data?: { commands?: unknown } })?.data?.commands)
        ? (data as { data: { commands: Array<Record<string, unknown>> } }).data.commands
        : [];

  return list.map((c) => {
    const command_type = String(c.command_type ?? "");
    const namespace = namespaceFromQualifiedName(command_type);
    const description =
      c.description != null && String(c.description).trim()
        ? String(c.description).trim()
        : undefined;
    return {
      key: command_type,
      command_type,
      implementation_type: String(c.implementation_type ?? "—"),
      bundle_id: c.bundle_id != null ? String(c.bundle_id) : undefined,
      description,
      namespace,
    };
  });
}

export function CommandsPage({ scope }: CommandsPageProps) {
  const [search, setSearch] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["commands", scope.tenantId, scope.motetId],
    queryFn: fetchCommands,
    refetchInterval: 3000,
  });

  // Normalize to array: backend returns { commands, total }; React Query cache may occasionally hold a different shape.
  const rawList = Array.isArray(data) ? data : [];
  const commands = rawList.slice().sort((a, b) => a.command_type.localeCompare(b.command_type));
  const normalizedSearch = search.trim().toLowerCase();
  const filteredCommands = useMemo(() => {
    if (!normalizedSearch) {
      return commands;
    }
    return commands.filter((cmd) => {
      const haystack =
        `${cmd.command_type} ${cmd.namespace} ${cmd.bundle_id ?? ""} ${cmd.implementation_type} ${cmd.description ?? ""}`.toLowerCase();
      return haystack.includes(normalizedSearch);
    });
  }, [commands, normalizedSearch]);

  return (
    <div>
      <div className="page-header" style={{ marginBottom: 16 }}>
        <Title level={2}>
          <ThunderboltOutlined style={{ marginRight: 12 }} />
          Commands
        </Title>
        <Text type="secondary">Registered command types available for execution (core and bundle)</Text>
      </div>

      {error && (
        <Alert
          title="Error loading commands"
          description={String(error)}
          type="error"
          style={{ marginBottom: 16 }}
        />
      )}

      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <div>
            <Text type="secondary">Registered Commands: </Text>
            <Text strong>{commands.length}</Text>
          </div>
          <div>
            <Text type="secondary">Visible After Filter: </Text>
            <Text strong>{filteredCommands.length}</Text>
          </div>
        </div>
      </Card>

      <Card>
        <Input.Search
          allowClear
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by command id, namespace, bundle, implementation, or description"
          style={{ maxWidth: 560, marginBottom: 12 }}
        />
        <Table
          rowKey="key"
          loading={isLoading}
          dataSource={filteredCommands}
          pagination={{ pageSize: 25 }}
          columns={[
            {
              title: "Command Id",
              dataIndex: "command_type",
              key: "command_type",
              width: 260,
              render: (name: string) => <Text code>{name}</Text>,
            },
            {
              title: "Namespace",
              dataIndex: "namespace",
              key: "namespace",
              width: 110,
              render: (namespace: string) => <Tag>{namespace}</Tag>,
            },
            {
              title: "Bundle",
              dataIndex: "bundle_id",
              key: "bundle_id",
              width: 130,
              render: (bundle_id: string | undefined) =>
                bundle_id ? <Tag color="blue">{bundle_id}</Tag> : <Text type="secondary">core</Text>,
            },
            {
              title: "Implementation",
              dataIndex: "implementation_type",
              key: "implementation_type",
              width: 130,
              render: (type: string) => <Tag color="default">{type}</Tag>,
            },
            {
              title: "Description",
              dataIndex: "description",
              key: "description",
              ellipsis: true,
              render: (description: string | undefined) =>
                description ? (
                  <Text ellipsis={{ tooltip: description }}>{description}</Text>
                ) : (
                  <Text type="secondary">—</Text>
                ),
            },
          ]}
          locale={{ emptyText: "No commands registered" }}
        />
      </Card>
    </div>
  );
}
