/**
 * Motet - Ops Dashboard - Agents Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Last Modified: 2026-08-27
 */
import { useState, type ReactNode } from "react";
import {
  Typography,
  Card,
  Table,
  Tag,
  Space,
  Alert,
  Tooltip,
  Statistic,
  Button,
  Modal,
  Select,
  Switch,
  theme,
} from "antd";
import { message } from "../antdApp";
import { RobotOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import { scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";
import {
  SURFACES_CATALOG_QUERY_KEY,
  fetchSurfaces,
  putAgentSurfaces,
} from "../api/surfaces";

const { Title, Text } = Typography;

/** Turn hook slots in execution order (matches TurnHooks and agent_turn). */
const TURN_HOOK_PHASES: { key: string; label: string; multi?: boolean }[] = [
  { key: "conversation_analysis", label: "Conversation analysis" },
  { key: "context_inject", label: "Context inject", multi: true },
  { key: "memory_reset", label: "Memory reset" },
  { key: "context_prepare", label: "Context prepare" },
  { key: "finalize", label: "Finalize" },
  { key: "after_finalize", label: "After finalize", multi: true },
];

function DetailGrid({
  rows,
}: {
  rows: { label: string; value: ReactNode }[];
}): ReactNode {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "minmax(140px, 180px) 1fr",
        gap: "8px 16px",
        alignItems: "start",
      }}
    >
      {rows.map(({ label, value }) => (
        <div key={label} style={{ display: "contents" }}>
          <Text type="secondary">{label}</Text>
          <div>{value}</div>
        </div>
      ))}
    </div>
  );
}

function monoTags(values: string[] | undefined | null, color?: string): ReactNode {
  if (!values || values.length === 0) {
    return <Text type="secondary">—</Text>;
  }
  return (
    <Space size={4} wrap>
      {values.map((v) => (
        <Tag key={v} color={color} style={{ fontFamily: "monospace", fontSize: 11 }}>
          {v}
        </Tag>
      ))}
    </Space>
  );
}

function asStringList(value: unknown): string[] {
  if (value == null) return [];
  if (Array.isArray(value)) return value.map((v) => String(v)).filter(Boolean);
  if (typeof value === "string" && value.trim()) return [value];
  return [];
}

function renderTurnHooks(turnHooks: Record<string, unknown> | undefined): ReactNode {
  const hooks = turnHooks || {};
  const knownKeys = new Set(TURN_HOOK_PHASES.map((p) => p.key));
  const extraKeys = Object.keys(hooks).filter((k) => !knownKeys.has(k));

  const renderValue = (value: unknown): ReactNode => {
    if (value == null || value === "") {
      return <Text type="secondary">skipped</Text>;
    }
    if (Array.isArray(value)) {
      if (value.length === 0) {
        return <Text type="secondary">skipped</Text>;
      }
      return monoTags(value.map(String), "purple");
    }
    return (
      <Tag color="purple" style={{ fontFamily: "monospace", fontSize: 11 }}>
        {String(value)}
      </Tag>
    );
  };

  const rows = [
    ...TURN_HOOK_PHASES.map(({ key, label }) => ({
      label,
      value: renderValue(hooks[key]),
    })),
    ...extraKeys.map((key) => ({
      label: key,
      value: renderValue(hooks[key]),
    })),
  ];
  return <DetailGrid rows={rows} />;
}

function renderToolFilter(toolFilter: Record<string, any> | undefined): ReactNode {
  const tf = toolFilter || {};
  const mode = String(tf.mode || "discovery");
  const rows: { label: string; value: ReactNode }[] = [
    { label: "Mode", value: <Tag color="cyan">{mode}</Tag> },
  ];
  const listFields: { key: string; label: string }[] = [
    { key: "required_tools", label: "Required tools" },
    { key: "required_workflows", label: "Required workflows" },
    { key: "exclude_tools", label: "Exclude tools" },
    { key: "exclude_workflows", label: "Exclude workflows" },
    { key: "prefix", label: "Prefix" },
    { key: "category", label: "Category" },
  ];
  for (const { key, label } of listFields) {
    const items = asStringList(tf[key]);
    if (items.length > 0) {
      rows.push({ label, value: monoTags(items) });
    }
  }
  if (tf.no_workflows) {
    rows.push({ label: "No workflows", value: <Tag color="orange">true</Tag> });
  }
  return <DetailGrid rows={rows} />;
}

function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}): ReactNode {
  const { token } = theme.useToken();
  return (
    <div style={{ marginBottom: 20 }}>
      <Text
        strong
        style={{
          display: "block",
          marginBottom: 10,
          paddingBottom: 6,
          borderBottom: `1px solid ${token.colorBorderSecondary}`,
          fontSize: 13,
          letterSpacing: "0.02em",
          textTransform: "uppercase",
          color: token.colorTextSecondary,
        }}
      >
        {title}
      </Text>
      {children}
    </div>
  );
}

interface AgentsPageProps {
  scope: Scope;
}

interface AgentListItem {
  qualified_id: string;
  agent_id: string;
  bundle_id?: string | null;
  display_name?: string;
  description?: string;
  allowed_roles?: string[];
  aliases?: string[];
  system_prompt?: string;
  tool_filter?: Record<string, any>;
  turn_hooks?: Record<string, any>;
  allowed_surface_ids?: string[] | null;
  model_provider?: string | null;
  model_name?: string | null;
  model_profile_name?: string | null;
  temperature?: number;
  max_iterations?: number;
  max_model_calls?: number | null;
  max_tools?: number;
  enable_thinking?: boolean;
  reasoning_effort?: string | null;
  conversation_id_prefix?: string | null;
  metadata?: Record<string, any> | null;
  skill_ids?: string[] | null;
  skill_mode?: string;
  skill_max_per_turn?: number;
}

interface AgentListResponse {
  agents: AgentListItem[];
  total: number;
}

async function fetchAgents(scope: Scope): Promise<AgentListItem[]> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl("/api/v1/agents", scope), { headers });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }
  const data = (await response.json()) as AgentListResponse;
  return Array.isArray(data?.agents) ? data.agents : [];
}

export function AgentsPage({ scope }: AgentsPageProps) {
  const queryClient = useQueryClient();
  const [editAgent, setEditAgent] = useState<AgentListItem | null>(null);
  const [allSurfaces, setAllSurfaces] = useState(true);
  const [selectedSurfaceIds, setSelectedSurfaceIds] = useState<string[]>([]);

  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["agents", scope.tenantId, scope.motetId],
    queryFn: () => fetchAgents(scope),
    refetchInterval: 5000,
  });

  const { data: surfacesData } = useQuery({
    queryKey: SURFACES_CATALOG_QUERY_KEY,
    queryFn: fetchSurfaces,
  });

  const surfacesMutation = useMutation({
    mutationFn: (input: {
      qualified_id: string;
      allowed_surface_ids?: string[] | null;
      clear?: boolean;
    }) =>
      putAgentSurfaces(input.qualified_id, {
        allowed_surface_ids: input.allowed_surface_ids,
        clear: input.clear,
      }),
    onSuccess: () => {
      message.success("Agent surfaces updated");
      setEditAgent(null);
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (err: Error) => message.error(err.message),
  });

  const agents = data || [];
  const coreCount = agents.filter((a) => !a.bundle_id).length;
  const bundleCount = agents.length - coreCount;
  const canManage = surfacesData?.can_manage ?? false;
  const surfaceOptions = (surfacesData?.surfaces ?? []).map((s) => ({
    value: s.id,
    label: s.display_name ? `${s.display_name} (${s.id})` : s.id,
  }));

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

  const openEdit = (record: AgentListItem) => {
    const allowed = record.allowed_surface_ids;
    const isAll = allowed == null || allowed.length === 0;
    setEditAgent(record);
    setAllSurfaces(isAll);
    setSelectedSurfaceIds(isAll ? [] : [...allowed]);
  };

  const columns = [
    {
      title: "Agent",
      dataIndex: "qualified_id",
      key: "qualified_id",
      width: "28%",
      sorter: (a: AgentListItem, b: AgentListItem) =>
        String(a.qualified_id || "").localeCompare(String(b.qualified_id || "")),
      render: (_: string, record: AgentListItem) => (
        <div>
          <Text strong style={{ fontFamily: "monospace" }}>
            {record.qualified_id}
          </Text>
          {record.display_name && (
            <div>
              <Text type="secondary" style={{ fontSize: 11 }}>
                {record.display_name}
              </Text>
            </div>
          )}
        </div>
      ),
    },
    {
      title: "Description",
      dataIndex: "description",
      key: "description",
      ellipsis: true,
      render: (d: string) =>
        d ? (
          <Tooltip title={d}>
            <Text>{d}</Text>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: "Source",
      dataIndex: "bundle_id",
      key: "bundle_id",
      width: 140,
      filters: [
        { text: "core", value: "core" },
        { text: "bundle", value: "bundle" },
      ],
      onFilter: (value: any, record: AgentListItem) =>
        value === "core" ? !record.bundle_id : !!record.bundle_id,
      render: (bundleId: string | null | undefined) =>
        bundleId ? <Tag color="purple">{bundleId}</Tag> : <Tag color="blue">core</Tag>,
    },
    {
      title: "Actions",
      key: "actions",
      width: 120,
      render: (_: any, record: AgentListItem) => (
        <Button size="small" disabled={!canManage} onClick={() => openEdit(record)}>
          Surfaces
        </Button>
      ),
    },
  ];

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <RobotOutlined style={{ marginRight: 12 }} />
            Agents
          </Title>
          <Text type="secondary">Configured agents available through the agent registry</Text>
        </div>
        {dataUpdatedAt && (
          <Text type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
            Updated: {formatLastUpdated(dataUpdatedAt)}
          </Text>
        )}
      </div>

      {error && (
        <Alert
          type="error"
          title="Failed to load agents"
          description={String(error)}
          style={{ marginBottom: 16 }}
        />
      )}

      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
          <Statistic title="Total Agents" value={agents.length} styles={{ content: { fontSize: 18 } }} />
          <Statistic title="Core Agents" value={coreCount} styles={{ content: { fontSize: 18, color: "#1677ff" } }} />
          <Statistic title="Bundle Agents" value={bundleCount} styles={{ content: { fontSize: 18, color: "#722ed1" } }} />
        </div>
      </Card>

      <Card>
        <Table
          dataSource={agents}
          columns={columns}
          rowKey="qualified_id"
          loading={isLoading}
          pagination={{ pageSize: 20, showSizeChanger: true }}
          expandable={{
            expandedRowRender: (record: AgentListItem) => {
              const roles = record.allowed_roles || [];
              const aliases = record.aliases || [];
              const allowedSurfaces = record.allowed_surface_ids;
              const metadata = record.metadata;
              const modelLabel =
                record.model_provider || record.model_name
                  ? [record.model_provider, record.model_name].filter(Boolean).join(" / ")
                  : "stack default";
              return (
                <div style={{ padding: 8 }}>
                  <div
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      gap: 24,
                      marginBottom: 16,
                    }}
                  >
                    <div>
                      <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
                        Roles
                      </Text>
                      {roles.length > 0 ? (
                        <Space size={4} wrap>
                          {roles.map((r) => (
                            <Tag key={r} color={r === "*" ? "green" : "default"} style={{ fontSize: 11 }}>
                              {r}
                            </Tag>
                          ))}
                        </Space>
                      ) : (
                        <Text type="secondary">—</Text>
                      )}
                    </div>
                    <div>
                      <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
                        Aliases
                      </Text>
                      {aliases.length > 0 ? (
                        <Space size={4} wrap>
                          {aliases.map((a) => (
                            <Tag key={a} style={{ fontSize: 11 }}>
                              {a}
                            </Tag>
                          ))}
                        </Space>
                      ) : (
                        <Text type="secondary">—</Text>
                      )}
                    </div>
                    <div>
                      <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
                        Surfaces
                      </Text>
                      {allowedSurfaces == null || allowedSurfaces.length === 0 ? (
                        <Tag>All surfaces</Tag>
                      ) : (
                        <Space size={4} wrap>
                          {allowedSurfaces.map((sid) => (
                            <Tag key={sid} color="geekblue" style={{ fontSize: 11 }}>
                              {sid}
                            </Tag>
                          ))}
                        </Space>
                      )}
                    </div>
                    {record.conversation_id_prefix ? (
                      <div>
                        <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
                          Conversation prefix
                        </Text>
                        <Tag style={{ fontFamily: "monospace", fontSize: 11 }}>
                          {record.conversation_id_prefix}
                        </Tag>
                      </div>
                    ) : null}
                  </div>

                  <Section title="Model">
                    <DetailGrid
                      rows={[
                        {
                          label: "Provider / model",
                          value: (
                            <Text style={{ fontFamily: "monospace", fontSize: 12 }}>{modelLabel}</Text>
                          ),
                        },
                        {
                          label: "Profile",
                          value: record.model_profile_name ? (
                            <Tag style={{ fontFamily: "monospace", fontSize: 11 }}>
                              {record.model_profile_name}
                            </Tag>
                          ) : (
                            <Text type="secondary">—</Text>
                          ),
                        },
                        {
                          label: "Temperature",
                          value: <Text>{record.temperature ?? 0.2}</Text>,
                        },
                        {
                          label: "Thinking",
                          value: record.enable_thinking ? (
                            <Space size={4}>
                              <Tag color="gold">enabled</Tag>
                              {record.reasoning_effort ? (
                                <Tag style={{ fontSize: 11 }}>{record.reasoning_effort}</Tag>
                              ) : null}
                            </Space>
                          ) : (
                            <Text type="secondary">off</Text>
                          ),
                        },
                      ]}
                    />
                  </Section>

                  <Section title="Loop limits">
                    <DetailGrid
                      rows={[
                        {
                          label: "Max iterations",
                          value: <Text>{record.max_iterations ?? 20}</Text>,
                        },
                        {
                          label: "Max model calls",
                          value:
                            record.max_model_calls != null ? (
                              <Text>{record.max_model_calls}</Text>
                            ) : (
                              <Text type="secondary">default</Text>
                            ),
                        },
                        {
                          label: "Max tools",
                          value: <Text>{record.max_tools ?? 20}</Text>,
                        },
                      ]}
                    />
                  </Section>

                  <Section title="Skills">
                    <DetailGrid
                      rows={[
                        {
                          label: "Mode",
                          value: <Tag color="cyan">{record.skill_mode || "allowlist"}</Tag>,
                        },
                        {
                          label: "Max per turn",
                          value: <Text>{record.skill_max_per_turn ?? 3}</Text>,
                        },
                        {
                          label: "Skill IDs",
                          value: monoTags(record.skill_ids, "purple"),
                        },
                      ]}
                    />
                  </Section>

                  <Section title="Tool filter">{renderToolFilter(record.tool_filter)}</Section>

                  {metadata && Object.keys(metadata).length > 0 ? (
                    <Section title="Metadata">
                      <DetailGrid
                        rows={Object.entries(metadata).map(([key, value]) => ({
                          label: key,
                          value: (
                            <Text style={{ fontFamily: "monospace", fontSize: 12 }}>
                              {typeof value === "string" ? value : JSON.stringify(value)}
                            </Text>
                          ),
                        }))}
                      />
                    </Section>
                  ) : null}

                  <Section title="Turn hooks">{renderTurnHooks(record.turn_hooks)}</Section>

                  {record.system_prompt != null && record.system_prompt !== "" && (
                    <Section title="System prompt">
                      <pre
                        style={{
                          margin: 0,
                          whiteSpace: "pre-wrap",
                          maxHeight: 320,
                          overflow: "auto",
                        }}
                      >
                        {record.system_prompt}
                      </pre>
                    </Section>
                  )}
                </div>
              );
            },
          }}
        />
      </Card>

      <Modal
        title={editAgent ? `Surfaces · ${editAgent.qualified_id}` : "Agent surfaces"}
        open={Boolean(editAgent)}
        onCancel={() => setEditAgent(null)}
        confirmLoading={surfacesMutation.isPending}
        onOk={() => {
          if (!editAgent) return;
          if (allSurfaces) {
            surfacesMutation.mutate({
              qualified_id: editAgent.qualified_id,
              clear: true,
            });
            return;
          }
          surfacesMutation.mutate({
            qualified_id: editAgent.qualified_id,
            allowed_surface_ids: selectedSurfaceIds,
            clear: false,
          });
        }}
        destroyOnHidden
      >
        <Space orientation="vertical" style={{ width: "100%" }} size="middle">
          <div>
            <Text style={{ marginRight: 12 }}>All catalog surfaces</Text>
            <Switch checked={allSurfaces} onChange={setAllSurfaces} />
          </div>
          {!allSurfaces && (
            <Select
              mode="multiple"
              style={{ width: "100%" }}
              placeholder="Select surfaces"
              options={surfaceOptions}
              value={selectedSurfaceIds}
              onChange={setSelectedSurfaceIds}
            />
          )}
          <Text type="secondary">
            Clearing to “all” removes the manage-UI overlay so AgentConfig / default applies.
          </Text>
        </Space>
      </Modal>
    </div>
  );
}
