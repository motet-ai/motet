/**
 * Motet - Ops Dashboard - Cost Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Last Modified: 2026-08-24
 * Notes: Token breakdown and events show cache read + cache creation (ADR-0124).
 *        "All Tenants" scope aggregates across the catalog (ADR-0126); it is no
 *        longer an alias for the motet-global platform tenant.
 *        Task IDs on recent cost events link to the task flow view.
 *        Event rows also show cache savings and tenant (useful for All Tenants).
 *        Command IDs link to task flow and jump to that command's detail.
 */
import { Link } from "react-router-dom";
import { Typography, Card, Table, Tag, Button, Space, Alert, Statistic, Progress, Tooltip, theme, Tabs } from "antd";
import { DollarOutlined, WarningOutlined, CheckCircleOutlined, ExclamationCircleOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import type { Scope } from "../hooks/useScope";
import { ALL_TENANTS } from "../api/tenants";

const { Title, Text } = Typography;

interface CostPageProps {
  scope: Scope;
}

// API Response types matching /api/v1/cost endpoints
interface DailyCostSummary {
  tenant_id: string;
  date: string;
  total_cost_usd: number;
  model_costs_usd: number;
  infrastructure_costs_usd: number;
  total_requests: number;
  total_prompt_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_creation_tokens?: number;
  total_reasoning_tokens: number;
  cache_savings_usd: number;
  aggregated_tenant_ids?: string[] | null;
}

interface UsageSummary {
  tenant_id: string;
  date: string;
  daily: {
    cost_usd?: number;
    requests?: number;
    prompt_tokens?: number;
    output_tokens?: number;
  };
  monthly: {
    cost_usd?: number;
    requests?: number;
  };
  limits: {
    daily_limit_usd?: number;
    monthly_limit_usd?: number;
    alert_threshold_pct?: number;
  };
  budget_status: string;
  aggregated_tenant_ids?: string[] | null;
}

interface CostEvent {
  event_id: string;
  timestamp: string;
  provider: string;
  model: string;
  cost_usd: number;
  cache_savings_usd?: number;
  prompt_tokens: number;
  output_tokens: number;
  reasoning_tokens?: number;
  cache_read_tokens: number;
  cache_creation_tokens?: number;
  tenant_id?: string;
  task_id?: string;
  command_id?: string;
  conversation_id?: string;
  principal_id?: string;
}

interface CostByPrincipalSummary {
  tenant_id: string;
  date: string;
  by_principal: Record<string, number>;
}

interface CostEventsResponse {
  events: CostEvent[];
  count: number;
  has_more: boolean;
}

async function fetchCostSummary(tenantId: string): Promise<DailyCostSummary | null> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/cost/summary?tenant_id=${encodeURIComponent(tenantId)}`, { headers });
    if (!response.ok) {
      console.error("Cost summary fetch failed:", response.status, response.statusText);
      return null;
    }
    return await response.json();
  } catch (e) {
    console.error("Cost summary fetch error:", e);
    return null;
  }
}

async function fetchUsageSummary(tenantId: string): Promise<UsageSummary | null> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/cost/usage?tenant_id=${encodeURIComponent(tenantId)}`, { headers });
    if (!response.ok) {
      console.error("Usage summary fetch failed:", response.status, response.statusText);
      return null;
    }
    return await response.json();
  } catch (e) {
    console.error("Usage summary fetch error:", e);
    return null;
  }
}

async function fetchCostEvents(tenantId: string, count: number = 20): Promise<CostEventsResponse | null> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/cost/events?count=${count}&tenant_id=${encodeURIComponent(tenantId)}`, { headers });
    if (!response.ok) {
      console.error("Cost events fetch failed:", response.status, response.statusText);
      return null;
    }
    return await response.json();
  } catch (e) {
    console.error("Cost events fetch error:", e);
    return null;
  }
}

async function fetchCostByPrincipal(tenantId: string): Promise<CostByPrincipalSummary | null> {
  const headers = getAuthHeaders();
  try {
    const response = await fetch(`/api/v1/cost/summary/by_principal?tenant_id=${encodeURIComponent(tenantId)}`, { headers });
    if (!response.ok) return null;
    return await response.json();
  } catch (e) {
    console.error("Cost by principal fetch error:", e);
    return null;
  }
}

export function CostPage({ scope }: CostPageProps) {
  const { token: themeToken } = theme.useToken();
  // "All Tenants" asks the API to sum the catalog. The API narrows this to the
  // caller's own tenant when they lack global scope.
  const tenantId = scope.tenantId || ALL_TENANTS;
  
  const { data: summary, isLoading: summaryLoading, error: summaryError, refetch: refetchSummary, dataUpdatedAt } = useQuery({
    queryKey: ["cost-summary", tenantId],
    queryFn: () => fetchCostSummary(tenantId),
    refetchInterval: 2000, // Refresh every 2 seconds
  });

  const { data: usage, isLoading: usageLoading, refetch: refetchUsage } = useQuery({
    queryKey: ["cost-usage", tenantId],
    queryFn: () => fetchUsageSummary(tenantId),
    refetchInterval: 2000, // Refresh every 2 seconds
  });

  const { data: eventsData, isLoading: eventsLoading, refetch: refetchEvents } = useQuery({
    queryKey: ["cost-events", tenantId],
    queryFn: () => fetchCostEvents(tenantId, 20),
    refetchInterval: 2000, // Refresh every 2 seconds
  });

  const { data: byPrincipalData } = useQuery({
    queryKey: ["cost-by-principal", tenantId],
    queryFn: () => fetchCostByPrincipal(tenantId),
    refetchInterval: 2000,
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

  const isLoading = summaryLoading || usageLoading || eventsLoading;
  const refetchAll = () => {
    refetchSummary();
    refetchUsage();
    refetchEvents();
  };

  const byPrincipal = byPrincipalData?.by_principal ?? {};
  const principalEntries = Object.entries(byPrincipal).sort((a, b) => b[1] - a[1]);

  const events = eventsData?.events || [];

  const aggregatedTenantIds = summary?.aggregated_tenant_ids ?? null;
  const scopeLabel = aggregatedTenantIds
    ? `All Tenants (${aggregatedTenantIds.length})`
    : summary?.tenant_id || tenantId;

  // Budget status from usage endpoint
  const budgetStatus = usage?.budget_status || "ok";
  const budgetsApply = budgetStatus !== "not_applicable";
  const dailyCost = usage?.daily?.cost_usd || 0;
  const dailyLimit = usage?.limits?.daily_limit_usd;
  const monthlyLimit = usage?.limits?.monthly_limit_usd;
  const monthlyCost = usage?.monthly?.cost_usd || 0;
  
  const dailyPct = dailyLimit && dailyLimit > 0 ? Math.min((dailyCost / dailyLimit) * 100, 100) : 0;
  const monthlyPct = monthlyLimit && monthlyLimit > 0 ? Math.min((monthlyCost / monthlyLimit) * 100, 100) : 0;
  
  const getBudgetStatusColor = (status: string) => {
    switch (status) {
      case "exceeded": return "error";
      case "critical": return "error";
      case "warning": return "warning";
      default: return "success";
    }
  };

  const getBudgetStatusIcon = (status: string) => {
    switch (status) {
      case "exceeded":
      case "critical":
        return <ExclamationCircleOutlined />;
      case "warning":
        return <WarningOutlined />;
      default:
        return <CheckCircleOutlined />;
    }
  };

  const eventColumns = [
    {
      title: "Time",
      dataIndex: "timestamp",
      key: "timestamp",
      width: 160,
      sorter: (a: CostEvent, b: CostEvent) => new Date(a.timestamp || 0).getTime() - new Date(b.timestamp || 0).getTime(),
      defaultSortOrder: "descend" as const,
      render: (ts: string) => ts ? new Date(ts).toLocaleTimeString() : "—",
    },
    {
      title: "Model",
      key: "model",
      width: 120,
      ellipsis: true,
      sorter: (a: CostEvent, b: CostEvent) => `${a.provider}/${a.model}`.localeCompare(`${b.provider}/${b.model}`),
      render: (_: any, record: CostEvent) => (
        <Tooltip title={`${record.provider}/${record.model}`}>
          <Text code style={{ fontSize: 11 }}>{record.model}</Text>
        </Tooltip>
      ),
    },
    {
      title: "Cost",
      dataIndex: "cost_usd",
      key: "cost_usd",
      width: 100,
      sorter: (a: CostEvent, b: CostEvent) => (a.cost_usd || 0) - (b.cost_usd || 0),
      render: (c: number) => <Text type="success">${(c || 0).toFixed(6)}</Text>,
    },
    {
      title: "Savings",
      dataIndex: "cache_savings_usd",
      key: "cache_savings_usd",
      width: 90,
      sorter: (a: CostEvent, b: CostEvent) =>
        (a.cache_savings_usd || 0) - (b.cache_savings_usd || 0),
      render: (savings: number) =>
        savings > 0 ? (
          <Tooltip title="USD saved vs full price from prompt-cache hits">
            <Text type="success" style={{ fontSize: 11 }}>
              ${savings.toFixed(6)}
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary" style={{ fontSize: 11 }}>
            —
          </Text>
        ),
    },
    {
      title: "Tokens",
      key: "tokens",
      width: 100,
      sorter: (a: CostEvent, b: CostEvent) => ((a.prompt_tokens || 0) + (a.output_tokens || 0)) - ((b.prompt_tokens || 0) + (b.output_tokens || 0)),
      render: (_: any, record: CostEvent) => {
        const prompt = record.prompt_tokens || 0;
        const output = record.output_tokens || 0;
        const reasoning = record.reasoning_tokens || 0;
        const tooltip = [
          `In: ${prompt.toLocaleString()}`,
          `Out: ${output.toLocaleString()}`,
          `Reasoning: ${reasoning.toLocaleString()}`,
        ].join(", ");
        return (
          <Tooltip title={tooltip}>
            <Text style={{ fontSize: 11 }}>
              {(prompt + output).toLocaleString()}
            </Text>
          </Tooltip>
        );
      },
    },
    {
      title: "Cache read",
      dataIndex: "cache_read_tokens",
      key: "cache_read_tokens",
      width: 90,
      sorter: (a: CostEvent, b: CostEvent) => (a.cache_read_tokens || 0) - (b.cache_read_tokens || 0),
      render: (cached: number) => cached ? (
        <Tag color="green" style={{ fontSize: 10 }}>{cached.toLocaleString()}</Tag>
      ) : "—",
    },
    {
      title: "Cache write",
      dataIndex: "cache_creation_tokens",
      key: "cache_creation_tokens",
      width: 95,
      sorter: (a: CostEvent, b: CostEvent) =>
        (a.cache_creation_tokens || 0) - (b.cache_creation_tokens || 0),
      render: (created: number) => created ? (
        <Tag color="blue" style={{ fontSize: 10 }}>{created.toLocaleString()}</Tag>
      ) : "—",
    },
    {
      title: "Tenant",
      dataIndex: "tenant_id",
      key: "tenant_id",
      width: 110,
      ellipsis: true,
      sorter: (a: CostEvent, b: CostEvent) =>
        (a.tenant_id || "").localeCompare(b.tenant_id || ""),
      render: (tid: string) =>
        tid ? (
          <Tooltip title={tid}>
            <Text code style={{ fontSize: 10 }}>
              {tid.length > 14 ? `${tid.slice(0, 12)}…` : tid}
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary" style={{ fontSize: 10 }}>
            —
          </Text>
        ),
    },
    {
      title: "Principal",
      dataIndex: "principal_id",
      key: "principal_id",
      width: 120,
      sorter: (a: CostEvent, b: CostEvent) => (a.principal_id || "").localeCompare(b.principal_id || ""),
      render: (pid: string) => pid ? (
        <Tooltip title={pid}>
          <Text code style={{ fontSize: 10 }}>{pid.length > 12 ? pid.slice(0, 10) + "…" : pid}</Text>
        </Tooltip>
      ) : (
        <Text type="secondary" style={{ fontSize: 10 }}>—</Text>
      ),
    },
    {
      title: "Task",
      dataIndex: "task_id",
      key: "task_id",
      width: 100,
      render: (id: string) => id ? (
        <Tooltip title={`Open task flow: ${id}`}>
          <Link to={`/task-flow?taskId=${encodeURIComponent(id)}`}>
            <Text code style={{ fontSize: 9, color: "inherit" }}>
              {id.slice(0, 8)}...
            </Text>
          </Link>
        </Tooltip>
      ) : "—",
    },
    {
      title: "Command",
      dataIndex: "command_id",
      key: "command_id",
      width: 100,
      render: (_: string, record: CostEvent) => {
        const commandId = record.command_id;
        if (!commandId) {
          return <Text type="secondary" style={{ fontSize: 10 }}>—</Text>;
        }
        if (!record.task_id) {
          return (
            <Tooltip title={commandId}>
              <Text code style={{ fontSize: 9 }}>
                {commandId.slice(0, 8)}...
              </Text>
            </Tooltip>
          );
        }
        const to = `/task-flow?taskId=${encodeURIComponent(record.task_id)}&commandId=${encodeURIComponent(commandId)}`;
        return (
          <Tooltip title={`Open command in task flow: ${commandId}`}>
            <Link to={to}>
              <Text code style={{ fontSize: 9, color: "inherit" }}>
                {commandId.slice(0, 8)}...
              </Text>
            </Link>
          </Tooltip>
        );
      },
    },
  ];

  const totalTokens = (summary?.total_prompt_tokens || 0) + (summary?.total_output_tokens || 0);

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <DollarOutlined style={{ marginRight: 12 }} />
            Cost
          </Title>
          <Text type="secondary">Track token usage and spending across models</Text>
        </div>
        <Space align="center">
          <Tooltip
            title={
              aggregatedTenantIds
                ? `Summed across ${aggregatedTenantIds.join(", ")}`
                : undefined
            }
          >
            <Text type="secondary" style={{ fontSize: 11 }}>Tenant: {scopeLabel}</Text>
          </Tooltip>
          {dataUpdatedAt && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              Updated: {formatLastUpdated(dataUpdatedAt)}
            </Text>
          )}
        </Space>
      </div>

      {summaryError && (
        <Alert
          title="Error loading cost data"
          description="Check that the cost tracking service is running and you are authenticated."
          type="error"
          style={{ marginBottom: 16 }}
        />
      )}

      {/* Summary Stats Cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 16, marginBottom: 16 }}>
        <Card 
          size="small" 
          style={{ 
            background: "linear-gradient(135deg, #667eea 0%, #764ba2 100%)", 
            border: "none" 
          }}
          styles={{ body: { padding: 16 } }}
        >
          <div style={{ color: "rgba(255,255,255,0.8)", fontSize: 12 }}>Today's Cost</div>
          <div style={{ color: "white", fontSize: 28, fontWeight: "bold" }}>
            ${(summary?.total_cost_usd || 0).toFixed(4)}
          </div>
        </Card>
        <Card 
          size="small" 
          style={{ 
            background: "linear-gradient(135deg, #11998e 0%, #38ef7d 100%)", 
            border: "none" 
          }}
          styles={{ body: { padding: 16 } }}
        >
          <div style={{ color: "rgba(255,255,255,0.8)", fontSize: 12 }}>Requests Today</div>
          <div style={{ color: "white", fontSize: 28, fontWeight: "bold" }}>
            {(summary?.total_requests || 0).toLocaleString()}
          </div>
        </Card>
        <Card 
          size="small" 
          style={{ 
            background: "linear-gradient(135deg, #ee9ca7 0%, #ffdde1 100%)", 
            border: "none" 
          }}
          styles={{ body: { padding: 16 } }}
        >
          <div style={{ color: "rgba(0,0,0,0.6)", fontSize: 12 }}>Cache Savings</div>
          <div style={{ color: "#333", fontSize: 28, fontWeight: "bold" }}>
            ${(summary?.cache_savings_usd || 0).toFixed(4)}
          </div>
        </Card>
        <Card 
          size="small" 
          style={{ 
            background: "linear-gradient(135deg, #fbc2eb 0%, #a6c1ee 100%)", 
            border: "none" 
          }}
          styles={{ body: { padding: 16 } }}
        >
          <div style={{ color: "rgba(0,0,0,0.6)", fontSize: 12 }}>Total Tokens</div>
          <div style={{ color: "#333", fontSize: 28, fontWeight: "bold" }}>
            {totalTokens.toLocaleString()}
          </div>
        </Card>
      </div>

      {/* Budget Status */}
      <Card size="small" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 16 }}>
          {budgetsApply ? (
            <Tag 
              color={getBudgetStatusColor(budgetStatus)} 
              icon={getBudgetStatusIcon(budgetStatus)}
              style={{ fontSize: 14, padding: "4px 12px" }}
            >
              {budgetStatus.toUpperCase()}
            </Tag>
          ) : (
            <Tag style={{ fontSize: 14, padding: "4px 12px" }}>N/A</Tag>
          )}
          <Text strong>Budget Status</Text>
          {!budgetsApply && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              Budgets are configured per tenant. Select a single tenant to see
              limits and thresholds.
            </Text>
          )}
        </div>
        
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
          {/* Daily Budget */}
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <Text>Daily Budget</Text>
              <Text>
                ${dailyCost.toFixed(4)} / {dailyLimit ? `$${dailyLimit.toFixed(2)}` : "Unlimited"}
              </Text>
            </div>
            {dailyLimit && dailyLimit > 0 && (
              <Progress
                percent={dailyPct}
                status={dailyPct >= 100 ? "exception" : dailyPct >= 80 ? "active" : "success"}
                format={(p) => `${p?.toFixed(0)}%`}
                size="small"
              />
            )}
          </div>
          
          {/* Monthly Budget */}
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <Text>Monthly Budget</Text>
              <Text>
                ${monthlyCost.toFixed(4)} / {monthlyLimit ? `$${monthlyLimit.toFixed(2)}` : "Unlimited"}
              </Text>
            </div>
            {monthlyLimit && monthlyLimit > 0 && (
              <Progress
                percent={monthlyPct}
                status={monthlyPct >= 100 ? "exception" : monthlyPct >= 80 ? "active" : "success"}
                format={(p) => `${p?.toFixed(0)}%`}
                size="small"
              />
            )}
          </div>
        </div>
      </Card>

      {/* Cost by Principal */}
      {principalEntries.length > 0 && (
        <Card size="small" title="Cost by Principal (today)" style={{ marginBottom: 16 }}>
          <Text type="secondary" style={{ display: "block", marginBottom: 12, fontSize: 12 }}>
            Spending attributed to each principal who called the model
          </Text>
          <Table
            size="small"
            dataSource={principalEntries.map(([principal_id, cost_usd]) => ({ key: principal_id, principal_id, cost_usd }))}
            columns={[
              {
                title: "Principal",
                dataIndex: "principal_id",
                key: "principal_id",
                render: (pid: string) => (
                  <Text code style={{ fontSize: 11 }}>{pid === "anonymous" ? "anonymous" : pid}</Text>
                ),
              },
              {
                title: "Cost (USD)",
                dataIndex: "cost_usd",
                key: "cost_usd",
                align: "right" as const,
                render: (c: number) => <Text type="success">${(c || 0).toFixed(6)}</Text>,
              },
            ]}
            pagination={false}
          />
        </Card>
      )}

      {/* Token Breakdown */}
      <Card size="small" title="Token Breakdown" style={{ marginBottom: 16 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
          <div style={{ background: themeToken.colorFillSecondary, padding: 12, borderRadius: 8, textAlign: "center" }}>
            <div style={{ fontSize: 11, color: themeToken.colorTextSecondary }}>Prompt</div>
            <div style={{ fontSize: 20, fontWeight: "bold" }}>{(summary?.total_prompt_tokens || 0).toLocaleString()}</div>
          </div>
          <div style={{ background: themeToken.colorFillSecondary, padding: 12, borderRadius: 8, textAlign: "center" }}>
            <div style={{ fontSize: 11, color: themeToken.colorTextSecondary }}>Output</div>
            <div style={{ fontSize: 20, fontWeight: "bold" }}>{(summary?.total_output_tokens || 0).toLocaleString()}</div>
          </div>
          <div style={{ background: themeToken.colorFillSecondary, padding: 12, borderRadius: 8, textAlign: "center" }}>
            <div style={{ fontSize: 11, color: themeToken.colorTextSecondary }}>Cache read</div>
            <div style={{ fontSize: 20, fontWeight: "bold" }}>{(summary?.total_cache_read_tokens || 0).toLocaleString()}</div>
          </div>
          <div style={{ background: themeToken.colorFillSecondary, padding: 12, borderRadius: 8, textAlign: "center" }}>
            <div style={{ fontSize: 11, color: themeToken.colorTextSecondary }}>Cache write</div>
            <div style={{ fontSize: 20, fontWeight: "bold" }}>{(summary?.total_cache_creation_tokens || 0).toLocaleString()}</div>
          </div>
          <div style={{ background: themeToken.colorFillSecondary, padding: 12, borderRadius: 8, textAlign: "center" }}>
            <div style={{ fontSize: 11, color: themeToken.colorTextSecondary }}>Reasoning</div>
            <div style={{ fontSize: 20, fontWeight: "bold" }}>{(summary?.total_reasoning_tokens || 0).toLocaleString()}</div>
          </div>
        </div>
      </Card>

      {/* Recent Cost Events */}
      <Card title="Recent Cost Events" size="small">
        <Table
          dataSource={events}
          columns={eventColumns}
          rowKey="event_id"
          loading={eventsLoading}
          pagination={{ pageSize: 10 }}
          size="small"
          locale={{ emptyText: "No cost events recorded yet" }}
        />
        {eventsData?.has_more && (
          <Text type="secondary" style={{ display: "block", textAlign: "center", marginTop: 8, fontSize: 11 }}>
            Showing latest {events.length} events
          </Text>
        )}
      </Card>
    </div>
  );
}
