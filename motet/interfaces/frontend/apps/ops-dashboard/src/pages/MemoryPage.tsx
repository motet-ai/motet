/**
 * Motet - Ops Dashboard - Memory Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Tenant-scoped memory browser for the manage app. Lists recent memories
 *     on load, filters by tier/type/scope/tag/agent/conversation, supports contains
 *     and semantic search, and forgets or retags selected rows.
 */
import { useEffect, useMemo, useState, type Key } from "react";
import {
  Typography,
  Card,
  Table,
  Tag,
  Button,
  Space,
  Alert,
  Statistic,
  Input,
  Popconfirm,
  Descriptions,
  theme,
  Tooltip,
  Select,
  Radio,
  Modal,
  Form,
  Checkbox,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { message } from "../antdApp";
import {
  DatabaseOutlined,
  SearchOutlined,
  DeleteOutlined,
  PlusOutlined,
  TagsOutlined,
  ReloadOutlined,
  InfoCircleOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import { scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;
const { Search, TextArea } = Input;

interface MemoryPageProps {
  scope: Scope;
}

interface MemoryStats {
  total_memories: number;
  last_24h: number;
  memory_types: number;
  tagged_count: number;
  type_breakdown?: Record<string, number>;
  tier_breakdown?: Record<string, number>;
  scope_breakdown?: Record<string, number>;
  motet_breakdown?: Record<string, number>;
  tenant_breakdown?: Record<string, number>;
  agent_breakdown?: Record<string, number>;
  vector_enabled?: boolean;
  error?: string;
}

interface MemoryEntry {
  id: string;
  content: string;
  type?: string;
  tags?: string[];
  metadata?: Record<string, any>;
  motet_id?: string;
  tenant_id?: string;
  principal_id?: string;
  conversation_id?: string;
  scope_type?: string;
  scope_id?: string;
  created_at?: string;
  similarity?: number;
  search_score?: number;
  relevance?: number;
}

interface MemoryBrowseResult {
  items: MemoryEntry[];
  total: number;
  limit: number;
  offset: number;
  query?: string | null;
}

type SearchMode = "contains" | "semantic";

const TIER_COLORS: Record<string, string> = {
  wm: "default",
  stm: "orange",
  ltm: "blue",
};

const SCOPE_COLORS: Record<string, string> = {
  GLOBAL: "cyan",
  global: "cyan",
  CONVERSATION: "green",
  conversation: "green",
  TASK: "orange",
  task: "orange",
  PRINCIPAL: "blue",
  principal: "blue",
  COLLECTIVE: "purple",
  collective: "purple",
  BACKGROUND: "default",
  background: "default",
  working: "default",
};

async function parseApiError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    if (typeof body?.error === "string") return body.error;
    return JSON.stringify(body?.detail ?? body);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

function memoryTier(entry: MemoryEntry): string | undefined {
  const tags = new Set(entry.tags || []);
  if (tags.has("ltm")) return "ltm";
  if (tags.has("stm")) return "stm";
  if (tags.has("wm")) return "wm";
  return undefined;
}

function memoryAgentId(entry: MemoryEntry): string | undefined {
  const fromMeta = entry.metadata?.agent_id;
  if (typeof fromMeta === "string" && fromMeta.trim()) {
    return fromMeta.trim();
  }
  const tagged = (entry.tags || []).find((tag) => tag.startsWith("agent:") && tag.length > 6);
  return tagged ? tagged.slice("agent:".length) : undefined;
}

function agentMatches(entry: MemoryEntry, wanted?: string): boolean {
  if (!wanted) return true;
  const needle = wanted.startsWith("agent:") ? wanted.slice("agent:".length) : wanted;
  const agentId = memoryAgentId(entry);
  if (!agentId) return false;
  if (agentId === needle) return true;
  const short = agentId.includes(".") ? agentId.slice(agentId.lastIndexOf(".") + 1) : agentId;
  return short.toLowerCase() === needle.toLowerCase();
}

interface AgentCatalogItem {
  qualified_id: string;
  display_name?: string;
}

async function fetchAgentCatalog(scope: Scope): Promise<AgentCatalogItem[]> {
  const response = await fetch(scopedUrl("/api/v1/agents", scope), {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    return [];
  }
  const data = await response.json();
  return Array.isArray(data?.agents) ? data.agents : [];
}

function similarityOf(entry: MemoryEntry): number | undefined {
  const candidates = [
    entry.similarity,
    entry.search_score,
    entry.relevance,
    entry.metadata?.search_score,
    entry.metadata?.relevance,
  ];
  for (const value of candidates) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return undefined;
}

function createdAtMs(entry: MemoryEntry): number {
  if (!entry.created_at) return 0;
  const parsed = Date.parse(entry.created_at);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function formatCreatedAt(value?: string): string {
  if (!value) return "—";
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Date(parsed).toLocaleString();
}

function throwApiError(response: Response, detail: string): never {
  const error = new Error(detail) as Error & { status?: number };
  error.status = response.status;
  throw error;
}

function shouldRetryMemoryQuery(failureCount: number, error: unknown): boolean {
  const status = (error as { status?: number } | undefined)?.status;
  if (status === 401 || status === 403) {
    return false;
  }
  return failureCount < 1;
}

async function fetchMemoryStats(scope: Scope): Promise<MemoryStats> {
  const response = await fetch(scopedUrl("/api/v1/memories/stats", scope), {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throwApiError(response, await parseApiError(response));
  }
  return await response.json();
}

async function browseMemories(
  scope: Scope,
  filters: {
    q?: string;
    type?: string;
    tag?: string;
    tier?: string;
    conversation_id?: string;
    agent?: string;
    limit?: number;
  },
): Promise<MemoryBrowseResult> {
  const response = await fetch(
    scopedUrl("/api/v1/memories/browse", scope, {
      q: filters.q,
      type: filters.type,
      tag: filters.tag,
      tier: filters.tier,
      conversation_id: filters.conversation_id,
      agent: filters.agent,
      limit: filters.limit ?? DEFAULT_BROWSE_LIMIT,
      offset: 0,
    }),
    { headers: getAuthHeaders() },
  );
  if (!response.ok) {
    throwApiError(response, await parseApiError(response));
  }
  return await response.json();
}

async function searchMemoriesSemantic(
  scope: Scope,
  query: string,
  tag?: string,
): Promise<MemoryEntry[]> {
  const response = await fetch(
    scopedUrl("/api/v1/memories/search", scope, {
      q: query,
      top_k: 50,
      tag,
    }),
    { headers: getAuthHeaders() },
  );
  if (!response.ok) {
    throw new Error(await parseApiError(response));
  }
  const data = await response.json();
  if (Array.isArray(data)) {
    return data as MemoryEntry[];
  }
  if (Array.isArray(data?.items)) {
    return data.items as MemoryEntry[];
  }
  if (Array.isArray(data?.memories)) {
    return data.memories as MemoryEntry[];
  }
  return [];
}

async function forgetMemories(
  scope: Scope,
  body: {
    memory_ids?: string[];
    conversation_id?: string;
    filter_tag?: string;
    tenant_id?: string | null;
    motet_id?: string | null;
  },
): Promise<{ deleted: number }> {
  const response = await fetch("/api/v1/memories/forget", {
    method: "POST",
    headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      memory_ids: body.memory_ids,
      conversation_id: body.conversation_id,
      filter_tag: body.filter_tag,
      tenant_id: body.tenant_id || scope.tenantId || undefined,
      motet_id: body.motet_id || scope.motetId || undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(await parseApiError(response));
  }
  return await response.json();
}

async function tagMemories(
  scope: Scope,
  memoryIds: string[],
  tags: string[],
  op: "add" | "remove",
): Promise<void> {
  const response = await fetch("/api/v1/memories/tag", {
    method: "POST",
    headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({ memory_ids: memoryIds, tags, op }),
  });
  if (!response.ok) {
    throw new Error(await parseApiError(response));
  }
}

async function storeMemoryNote(
  content: string,
  tags: string[],
  longTerm: boolean,
  conversationId?: string,
): Promise<void> {
  const response = await fetch("/api/v1/memories/store", {
    method: "POST",
    headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      content,
      type: "note",
      tags,
      long_term: longTerm,
      conversation_id: conversationId || undefined,
    }),
  });
  if (!response.ok) {
    throw new Error(await parseApiError(response));
  }
}

async function clearScopedMemories(scope: Scope): Promise<{ memory: number }> {
  const response = await fetch(scopedUrl("/api/v1/memories/clear", scope), {
    method: "POST",
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(await parseApiError(response));
  }
  return await response.json();
}

const DEFAULT_BROWSE_LIMIT = 200;
const BROWSE_LIMIT_OPTIONS = [
  { value: 100, label: "100" },
  { value: 200, label: "200" },
  { value: 500, label: "500" },
  { value: 1000, label: "1,000" },
  { value: 5000, label: "5,000 (slower)" },
];

function groupByStore(entries: MemoryEntry[]): Map<string, MemoryEntry[]> {
  const groups = new Map<string, MemoryEntry[]>();
  for (const entry of entries) {
    const key = `${entry.tenant_id || ""}::${entry.motet_id || ""}`;
    const list = groups.get(key) || [];
    list.push(entry);
    groups.set(key, list);
  }
  return groups;
}

export function MemoryPage({ scope }: MemoryPageProps) {
  const { token: themeToken } = theme.useToken();
  const queryClient = useQueryClient();
  const [searchQuery, setSearchQuery] = useState("");
  const [committedQuery, setCommittedQuery] = useState("");
  const [searchMode, setSearchMode] = useState<SearchMode>("contains");
  const [typeFilter, setTypeFilter] = useState<string | undefined>();
  const [tierFilter, setTierFilter] = useState<string | undefined>();
  const [tagFilter, setTagFilter] = useState<string | undefined>();
  const [conversationFilter, setConversationFilter] = useState<string | undefined>();
  const [agentFilter, setAgentFilter] = useState<string | undefined>();
  const [browseLimit, setBrowseLimit] = useState(DEFAULT_BROWSE_LIMIT);
  const [semanticItems, setSemanticItems] = useState<MemoryEntry[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [mutating, setMutating] = useState(false);
  const [expandedRows, setExpandedRows] = useState<string[]>([]);
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([]);
  const [storeOpen, setStoreOpen] = useState(false);
  const [tagOpen, setTagOpen] = useState(false);
  const [tagTargetIds, setTagTargetIds] = useState<string[]>([]);
  const [storeForm] = Form.useForm();
  const [tagForm] = Form.useForm();

  const hasClearScope = Boolean(scope.tenantId || scope.motetId);
  const browseFilters = useMemo(
    () => ({
      q: searchMode === "contains" ? committedQuery.trim() || undefined : undefined,
      type: typeFilter,
      tag: tagFilter,
      tier: tierFilter,
      conversation_id: conversationFilter,
      agent: agentFilter,
      limit: browseLimit,
    }),
    [searchMode, committedQuery, typeFilter, tagFilter, tierFilter, conversationFilter, agentFilter, browseLimit],
  );

  const statsQuery = useQuery({
    queryKey: ["memory-stats", scope.tenantId, scope.motetId],
    queryFn: () => fetchMemoryStats(scope),
    retry: shouldRetryMemoryQuery,
    refetchInterval: (query) => (query.state.status === "success" ? 30_000 : false),
  });

  const browseQuery = useQuery({
    queryKey: ["memory-browse", scope.tenantId, scope.motetId, browseFilters],
    queryFn: () => browseMemories(scope, browseFilters),
    retry: shouldRetryMemoryQuery,
    refetchInterval: (query) => (query.state.status === "success" ? 30_000 : false),
  });

  const agentsQuery = useQuery({
    queryKey: ["memory-agents", scope.tenantId, scope.motetId],
    queryFn: () => fetchAgentCatalog(scope),
    staleTime: 60_000,
  });

  useEffect(() => {
    setSemanticItems(null);
    setSearchError(null);
    setSelectedRowKeys([]);
    setExpandedRows([]);
  }, [scope.tenantId, scope.motetId]);

  const stats = statsQuery.data;
  const browseItems = browseQuery.data?.items || [];
  const tableItems =
    searchMode === "semantic" && semanticItems
      ? semanticItems.filter((entry) => {
          if (typeFilter && entry.type !== typeFilter) return false;
          if (tierFilter && memoryTier(entry) !== tierFilter) return false;
          if (tagFilter && !(entry.tags || []).includes(tagFilter)) return false;
          if (conversationFilter && entry.conversation_id !== conversationFilter) return false;
          if (!agentMatches(entry, agentFilter)) return false;
          return true;
        })
      : browseItems;

  const selectedEntries = tableItems.filter((entry) => selectedRowKeys.includes(entry.id));

  const refreshLists = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["memory-stats"] }),
      queryClient.invalidateQueries({ queryKey: ["memory-browse"] }),
    ]);
  };

  const handleSearch = async (query: string) => {
    const trimmed = query.trim();
    setSearchQuery(query);
    setCommittedQuery(trimmed);
    setSearchError(null);
    if (searchMode === "contains") {
      setSemanticItems(null);
      return;
    }
    if (!trimmed) {
      setSemanticItems(null);
      return;
    }
    setSearching(true);
    try {
      const semanticTag = agentFilter
        ? agentFilter.startsWith("agent:")
          ? agentFilter
          : `agent:${agentFilter}`
        : tagFilter || tierFilter;
      const items = await searchMemoriesSemantic(scope, trimmed, semanticTag);
      setSemanticItems(items);
    } catch (error) {
      setSemanticItems([]);
      setSearchError(error instanceof Error ? error.message : String(error));
    } finally {
      setSearching(false);
    }
  };

  const applyChipFilter = (kind: "type" | "tier" | "agent", value: string) => {
    if (kind === "type") {
      setTypeFilter((current) => (current === value ? undefined : value));
    } else if (kind === "tier") {
      setTierFilter((current) => (current === value ? undefined : value));
    } else if (kind === "agent") {
      setAgentFilter((current) => (current === value ? undefined : value));
    }
  };

  const handleForgetEntries = async (entries: MemoryEntry[]) => {
    if (entries.length === 0) return;
    setMutating(true);
    try {
      let deleted = 0;
      for (const [key, group] of groupByStore(entries)) {
        const [tenantId, motetId] = key.split("::");
        const result = await forgetMemories(scope, {
          memory_ids: group.map((entry) => entry.id),
          tenant_id: tenantId || scope.tenantId,
          motet_id: motetId || scope.motetId,
        });
        deleted += result.deleted || group.length;
      }
      message.success(`Forgot ${deleted} memor${deleted === 1 ? "y" : "ies"}`);
      setSelectedRowKeys([]);
      setSemanticItems(null);
      await refreshLists();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "Failed to forget memories");
    } finally {
      setMutating(false);
    }
  };

  const handleForgetConversation = async (conversationId: string, entry: MemoryEntry) => {
    setMutating(true);
    try {
      const result = await forgetMemories(scope, {
        conversation_id: conversationId,
        tenant_id: entry.tenant_id || scope.tenantId,
        motet_id: entry.motet_id || scope.motetId,
      });
      message.success(`Forgot ${result.deleted} memor${result.deleted === 1 ? "y" : "ies"} in conversation`);
      setSelectedRowKeys([]);
      setSemanticItems(null);
      await refreshLists();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "Failed to forget conversation memories");
    } finally {
      setMutating(false);
    }
  };

  const handleClearScoped = async () => {
    if (!hasClearScope) return;
    setClearing(true);
    try {
      const result = await clearScopedMemories(scope);
      message.success(`Cleared ${result.memory || 0} memories in the selected scope`);
      setSelectedRowKeys([]);
      setSemanticItems(null);
      await refreshLists();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "Failed to clear memories");
    } finally {
      setClearing(false);
    }
  };

  const openTagModal = (ids: string[]) => {
    setTagTargetIds(ids);
    tagForm.resetFields();
    setTagOpen(true);
  };

  const handleTagSubmit = async () => {
    const values = await tagForm.validateFields();
    const tags = String(values.tags || "")
      .split(",")
      .map((tag: string) => tag.trim())
      .filter(Boolean);
    if (tags.length === 0) {
      message.error("Enter at least one tag");
      return;
    }
    setMutating(true);
    try {
      await tagMemories(scope, tagTargetIds, tags, values.op);
      message.success(`${values.op === "remove" ? "Removed" : "Added"} tags on ${tagTargetIds.length} memories`);
      setTagOpen(false);
      await refreshLists();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "Failed to update tags");
    } finally {
      setMutating(false);
    }
  };

  const handleStoreSubmit = async () => {
    const values = await storeForm.validateFields();
    const tags = String(values.tags || "")
      .split(",")
      .map((tag: string) => tag.trim())
      .filter(Boolean);
    if (values.longTerm && !tags.includes("ltm")) {
      tags.push("ltm");
    }
    setMutating(true);
    try {
      await storeMemoryNote(values.content, tags, Boolean(values.longTerm), values.conversationId);
      message.success("Stored memory note");
      setStoreOpen(false);
      storeForm.resetFields();
      await refreshLists();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "Failed to store memory");
    } finally {
      setMutating(false);
    }
  };

  const columns: ColumnsType<MemoryEntry> = [
    {
      title: "Content",
      dataIndex: "content",
      key: "content",
      ellipsis: true,
      render: (content: string) => (
        <Tooltip title={content}>
          <Text style={{ maxWidth: 360, display: "block" }} ellipsis>
            {content || "—"}
          </Text>
        </Tooltip>
      ),
    },
    {
      title: "Created",
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      defaultSortOrder: "descend",
      sorter: (a, b) => createdAtMs(a) - createdAtMs(b),
      render: (value: string) => <Text style={{ fontSize: 11 }}>{formatCreatedAt(value)}</Text>,
    },
    {
      title: "Type",
      dataIndex: "type",
      key: "type",
      width: 130,
      render: (type: string) =>
        type ? (
          <Tag style={{ cursor: "pointer" }} onClick={() => setTypeFilter(type)}>
            {type}
          </Tag>
        ) : (
          "—"
        ),
    },
    {
      title: "Agent",
      key: "agent",
      width: 150,
      render: (_: unknown, record: MemoryEntry) => {
        const agentId = memoryAgentId(record);
        if (!agentId) {
          return <Text type="secondary">—</Text>;
        }
        const catalog = (agentsQuery.data || []).find((agent) => agent.qualified_id === agentId);
        const label = catalog?.display_name || agentId;
        return (
          <Tooltip title={agentId}>
            <Tag style={{ cursor: "pointer" }} onClick={() => setAgentFilter(agentId)}>
              {label}
            </Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "Tier",
      key: "tier",
      width: 80,
      render: (_: unknown, record: MemoryEntry) => {
        const tier = memoryTier(record);
        return tier ? (
          <Tag color={TIER_COLORS[tier]} style={{ cursor: "pointer" }} onClick={() => setTierFilter(tier)}>
            {tier}
          </Tag>
        ) : (
          <Text type="secondary">—</Text>
        );
      },
    },
    {
      title: "Scope",
      dataIndex: "scope_type",
      key: "scope_type",
      width: 120,
      render: (value: string) =>
        value ? <Tag color={SCOPE_COLORS[value] || "default"}>{value}</Tag> : "—",
    },
    {
      title: "Conversation",
      dataIndex: "conversation_id",
      key: "conversation_id",
      width: 140,
      render: (conversationId: string) =>
        conversationId ? (
          <Tooltip title="Filter to this conversation">
            <Text
              code
              style={{ fontSize: 10, cursor: "pointer" }}
              onClick={() =>
                setConversationFilter((current) =>
                  current === conversationId ? undefined : conversationId,
                )
              }
            >
              {conversationId.length > 12 ? `${conversationId.slice(0, 8)}…` : conversationId}
            </Text>
          </Tooltip>
        ) : (
          "—"
        ),
    },
    {
      title: "Tags",
      dataIndex: "tags",
      key: "tags",
      width: 160,
      render: (tags: string[]) =>
        tags && tags.length > 0 ? (
          <Space size={2} wrap>
            {tags.slice(0, 3).map((tag) => (
              <Tag
                key={tag}
                color="purple"
                style={{ fontSize: 10, cursor: "pointer" }}
                onClick={() => setTagFilter(tag)}
              >
                {tag}
              </Tag>
            ))}
            {tags.length > 3 && (
              <Text type="secondary" style={{ fontSize: 10 }}>
                +{tags.length - 3}
              </Text>
            )}
          </Space>
        ) : (
          "—"
        ),
    },
    ...(searchMode === "semantic" && semanticItems
      ? [
          {
            title: "Score",
            key: "similarity",
            width: 80,
            render: (_: unknown, record: MemoryEntry) => {
              const score = similarityOf(record);
              return score === undefined ? "—" : score.toFixed(3);
            },
          } as ColumnsType<MemoryEntry>[number],
        ]
      : []),
    {
      title: "Actions",
      key: "actions",
      width: 170,
      render: (_: unknown, record: MemoryEntry) => (
        <Space size={4} wrap>
          <Button size="small" icon={<TagsOutlined />} onClick={() => openTagModal([record.id])}>
            Tag
          </Button>
          <Popconfirm
            title="Forget this memory?"
            description="Deletes the KV row and any matching vector document."
            onConfirm={() => handleForgetEntries([record])}
            okText="Forget"
            okButtonProps={{ danger: true }}
          >
            <Button size="small" danger icon={<DeleteOutlined />} loading={mutating}>
              Forget
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const expandedRowRender = (record: MemoryEntry) => (
    <div style={{ padding: 8 }}>
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="Full Content">
          <pre
            style={{
              fontSize: 11,
              margin: 0,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxHeight: 200,
              overflow: "auto",
              background: themeToken.colorFillSecondary,
              padding: 8,
              borderRadius: 4,
            }}
          >
            {record.content}
          </pre>
        </Descriptions.Item>
        {record.id && (
          <Descriptions.Item label="ID">
            <Text code copyable>
              {record.id}
            </Text>
          </Descriptions.Item>
        )}
        {record.conversation_id && (
          <Descriptions.Item label="Conversation">
            <Space>
              <Text code copyable>
                {record.conversation_id}
              </Text>
              <Button size="small" onClick={() => setConversationFilter(record.conversation_id)}>
                Filter
              </Button>
              <Popconfirm
                title="Forget this conversation's memories?"
                description="Deletes every memory tagged to this conversation in its tenant store."
                onConfirm={() => handleForgetConversation(record.conversation_id!, record)}
                okText="Forget conversation"
                okButtonProps={{ danger: true }}
              >
                <Button size="small" danger>
                  Forget conversation
                </Button>
              </Popconfirm>
            </Space>
          </Descriptions.Item>
        )}
        {record.tags && record.tags.length > 0 && (
          <Descriptions.Item label="Tags">
            <Space wrap>
              {record.tags.map((tag) => (
                <Tag key={tag} color="purple" style={{ cursor: "pointer" }} onClick={() => setTagFilter(tag)}>
                  {tag}
                </Tag>
              ))}
            </Space>
          </Descriptions.Item>
        )}
        {memoryAgentId(record) && (
          <Descriptions.Item label="Agent">
            <Text
              code
              copyable
              style={{ cursor: "pointer" }}
              onClick={() => setAgentFilter(memoryAgentId(record))}
            >
              {memoryAgentId(record)}
            </Text>
          </Descriptions.Item>
        )}
        {(record.tenant_id || record.motet_id || record.principal_id) && (
          <Descriptions.Item label="Identity">
            <Space wrap>
              {record.tenant_id && <Tag>tenant: {record.tenant_id}</Tag>}
              {record.motet_id && <Tag>motet: {record.motet_id}</Tag>}
              {record.principal_id && (
                <Text code copyable style={{ fontSize: 11 }}>
                  {record.principal_id}
                </Text>
              )}
            </Space>
          </Descriptions.Item>
        )}
        {record.metadata && Object.keys(record.metadata).length > 0 && (
          <Descriptions.Item label="Metadata">
            <pre
              style={{
                fontSize: 10,
                margin: 0,
                maxHeight: 150,
                overflow: "auto",
                background: themeToken.colorFillSecondary,
                padding: 8,
                borderRadius: 4,
              }}
            >
              {JSON.stringify(record.metadata, null, 2)}
            </pre>
          </Descriptions.Item>
        )}
      </Descriptions>
    </div>
  );

  const typeOptions = Object.keys(stats?.type_breakdown || {}).map((type) => ({
    value: type,
    label: `${type} (${stats?.type_breakdown?.[type] || 0})`,
  }));
  const tagOptions = Array.from(
    new Set(tableItems.flatMap((entry) => entry.tags || [])),
  ).map((tag) => ({ value: tag, label: tag }));
  const agentNameById = new Map(
    (agentsQuery.data || []).map((agent) => [agent.qualified_id, agent.display_name || agent.qualified_id]),
  );
  const agentIds = new Set<string>([
    ...Object.keys(stats?.agent_breakdown || {}).filter((id) => id !== "unattributed"),
    ...(agentsQuery.data || []).map((agent) => agent.qualified_id),
  ]);
  const agentOptions = Array.from(agentIds).map((id) => {
    const count = stats?.agent_breakdown?.[id];
    const name = agentNameById.get(id);
    const title = name && name !== id ? `${name} (${id})` : id;
    return { value: id, label: count !== undefined ? `${title} (${count})` : title };
  });

  const listError = browseQuery.error || statsQuery.error;
  const showingSemantic = searchMode === "semantic" && Boolean(semanticItems);
  const clearCount = stats?.total_memories || 0;

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <DatabaseOutlined style={{ marginRight: 12 }} />
            Memory
          </Title>
          <Text type="secondary">Browse, search, tag, and forget memories in the selected tenant/motet scope</Text>
        </div>
        <Space align="center" wrap>
          <Button icon={<ReloadOutlined />} onClick={() => refreshLists()}>
            Refresh
          </Button>
          <Button icon={<PlusOutlined />} onClick={() => setStoreOpen(true)}>
            Store note
          </Button>
          <Tooltip title={hasClearScope ? undefined : "Select a tenant or motet in the header before clearing"}>
            <Popconfirm
              title="Clear scoped memories"
              description={`This permanently deletes ${clearCount} memor${clearCount === 1 ? "y" : "ies"} in the selected tenant/motet. This cannot be undone.`}
              onConfirm={handleClearScoped}
              okText="Clear scope"
              cancelText="Cancel"
              okButtonProps={{ danger: true }}
              disabled={!hasClearScope}
            >
              <Button icon={<DeleteOutlined />} danger loading={clearing} disabled={!hasClearScope}>
                Clear scope
              </Button>
            </Popconfirm>
          </Tooltip>
        </Space>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 16 }}>
        <Card size="small">
          <Statistic title="Total Memories" value={stats?.total_memories || 0} formatter={(val) => Number(val).toLocaleString()} />
        </Card>
        <Card size="small">
          <Statistic
            title="Last 24h"
            value={stats?.last_24h || 0}
            styles={{ content: { color: themeToken.colorSuccess } }}
          />
        </Card>
        <Card size="small">
          <Statistic
            title="Tagged"
            value={stats?.tagged_count || 0}
            styles={{ content: { color: themeToken.colorInfo } }}
          />
        </Card>
        <Card size="small">
          <Statistic
            title="Vector index"
            value={stats?.vector_enabled ? "On" : "Off"}
            styles={{ content: { color: stats?.vector_enabled ? themeToken.colorSuccess : themeToken.colorTextSecondary } }}
          />
        </Card>
      </div>

      {(stats?.tier_breakdown || stats?.type_breakdown || stats?.scope_breakdown || stats?.agent_breakdown) && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 16 }}>
            <div>
              <Text strong style={{ fontSize: 12, marginBottom: 8, display: "block" }}>
                Tiers
              </Text>
              <Space wrap size={4}>
                {Object.entries(stats?.tier_breakdown || {}).map(([tier, count]) => (
                  <Tag
                    key={tier}
                    color={TIER_COLORS[tier] || "default"}
                    style={{ cursor: tier === "untagged" ? "default" : "pointer" }}
                    onClick={() => tier !== "untagged" && applyChipFilter("tier", tier)}
                  >
                    {tier}: {count}
                  </Tag>
                ))}
              </Space>
            </div>
            <div>
              <Text strong style={{ fontSize: 12, marginBottom: 8, display: "block" }}>
                Types
              </Text>
              <Space wrap size={4}>
                {Object.entries(stats?.type_breakdown || {}).map(([type, count]) => (
                  <Tag key={type} style={{ cursor: "pointer" }} onClick={() => applyChipFilter("type", type)}>
                    {type}: {count}
                  </Tag>
                ))}
              </Space>
            </div>
            <div>
              <Text strong style={{ fontSize: 12, marginBottom: 8, display: "block" }}>
                Scopes
              </Text>
              <Space wrap size={4}>
                {Object.entries(stats?.scope_breakdown || {}).map(([scopeType, count]) => (
                  <Tag key={scopeType} color={SCOPE_COLORS[scopeType] || "default"}>
                    {scopeType}: {count}
                  </Tag>
                ))}
              </Space>
            </div>
            <div>
              <Text strong style={{ fontSize: 12, marginBottom: 8, display: "block" }}>
                Agents
              </Text>
              <Space wrap size={4}>
                {Object.entries(stats?.agent_breakdown || {}).map(([agentId, count]) => (
                  <Tag
                    key={agentId}
                    style={{ cursor: agentId === "unattributed" ? "default" : "pointer" }}
                    onClick={() => agentId !== "unattributed" && applyChipFilter("agent", agentId)}
                  >
                    {agentNameById.get(agentId) || agentId}: {count}
                  </Tag>
                ))}
              </Space>
            </div>
          </div>
        </Card>
      )}

      {stats?.vector_enabled === false && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          title="Semantic search uses the vector index"
          description="MOTET_ENABLE_VECTOR_MEMORY is off, so only long-term rows that were indexed elsewhere can appear in semantic results. Contains search still lists KV memories."
        />
      )}

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space orientation="vertical" style={{ width: "100%" }} size={12}>
          <Space wrap>
            <Text strong>
              <SearchOutlined /> Search
            </Text>
            <Radio.Group
              value={searchMode}
              onChange={(event) => {
                setSearchMode(event.target.value);
                setSemanticItems(null);
                setSearchError(null);
              }}
              options={[
                { value: "contains", label: "Contains" },
                { value: "semantic", label: "Semantic" },
              ]}
            />
          </Space>
          <Search
            placeholder={
              searchMode === "contains"
                ? "Filter by text in content or tags"
                : "Semantic query against the vector index"
            }
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            onSearch={handleSearch}
            enterButton="Search"
            loading={searching}
            allowClear
            style={{ maxWidth: 640 }}
          />
          <Text type="secondary" style={{ fontSize: 11 }}>
            <InfoCircleOutlined />{" "}
            {searchMode === "contains"
              ? `Substring match on content and tags in the newest ${browseLimit} memories in this scope.`
              : "Vector similarity via memory recall. Results are from the authenticated principal's index; pick a tenant to narrow KV filters."}
          </Text>
          <Space wrap>
            <Select
              allowClear
              showSearch
              optionFilterProp="label"
              placeholder="Agent"
              value={agentFilter}
              onChange={setAgentFilter}
              options={agentOptions}
              style={{ minWidth: 220 }}
            />
            <Select
              allowClear
              placeholder="Type"
              value={typeFilter}
              onChange={setTypeFilter}
              options={typeOptions}
              style={{ minWidth: 160 }}
            />
            <Select
              allowClear
              placeholder="Tier"
              value={tierFilter}
              onChange={setTierFilter}
              options={[
                { value: "wm", label: "wm" },
                { value: "stm", label: "stm" },
                { value: "ltm", label: "ltm" },
              ]}
              style={{ minWidth: 120 }}
            />
            <Select
              allowClear
              showSearch
              placeholder="Tag"
              value={tagFilter}
              onChange={setTagFilter}
              options={tagOptions}
              style={{ minWidth: 160 }}
            />
            <Input
              allowClear
              placeholder="Conversation id"
              value={conversationFilter}
              onChange={(event) => setConversationFilter(event.target.value || undefined)}
              style={{ minWidth: 200 }}
            />
            <Space size={6}>
              <Text type="secondary">Newest</Text>
              <Select
                value={browseLimit}
                onChange={setBrowseLimit}
                options={BROWSE_LIMIT_OPTIONS}
                style={{ minWidth: 150 }}
              />
            </Space>
          </Space>
        </Space>
      </Card>

      {listError && (
        <Alert
          title="Error loading memories"
          description={String(listError instanceof Error ? listError.message : listError)}
          type="error"
          style={{ marginBottom: 16 }}
        />
      )}
      {searchError && (
        <Alert title="Search failed" description={searchError} type="error" style={{ marginBottom: 16 }} />
      )}
      {stats?.error && (
        <Alert title="Stats warning" description={stats.error} type="warning" style={{ marginBottom: 16 }} />
      )}

      <Card
        title={
          showingSemantic
            ? `Semantic results for "${searchQuery}" (${tableItems.length})`
            : `Newest ${browseQuery.data?.total ?? tableItems.length}${
                stats?.total_memories ? ` of ${stats.total_memories}` : ""
              }`
        }
      >
        {selectedRowKeys.length > 0 && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            title={`${selectedRowKeys.length} selected`}
            action={
              <Space>
                <Button size="small" onClick={() => setSelectedRowKeys([])} disabled={mutating}>
                  Clear
                </Button>
                <Button
                  size="small"
                  icon={<TagsOutlined />}
                  onClick={() => openTagModal(selectedRowKeys.map(String))}
                  disabled={mutating}
                >
                  Tag selected
                </Button>
                <Popconfirm
                  title={`Forget ${selectedRowKeys.length} selected memories?`}
                  onConfirm={() => handleForgetEntries(selectedEntries)}
                  okText="Forget"
                  okButtonProps={{ danger: true }}
                >
                  <Button size="small" danger icon={<DeleteOutlined />} loading={mutating}>
                    Forget selected
                  </Button>
                </Popconfirm>
              </Space>
            }
          />
        )}
        <Table
          dataSource={tableItems}
          columns={columns}
          rowKey="id"
          loading={(browseQuery.isPending && !browseQuery.isError) || searching}
          pagination={{ pageSize: 50, showSizeChanger: true, pageSizeOptions: [50, 100, 200] }}
          size="small"
          rowSelection={{
            selectedRowKeys,
            onChange: (keys) => setSelectedRowKeys(keys),
          }}
          expandable={{
            expandedRowRender,
            expandedRowKeys: expandedRows,
            onExpandedRowsChange: (keys) => setExpandedRows(keys as string[]),
          }}
          locale={{ emptyText: "No memories in this scope. Store a note or relax the filters." }}
        />
      </Card>

      <Modal
        title="Store memory note"
        open={storeOpen}
        onCancel={() => setStoreOpen(false)}
        onOk={handleStoreSubmit}
        confirmLoading={mutating}
        okText="Store"
      >
        <Form form={storeForm} layout="vertical" initialValues={{ longTerm: true }}>
          <Form.Item name="content" label="Content" rules={[{ required: true, message: "Content is required" }]}>
            <TextArea rows={5} />
          </Form.Item>
          <Form.Item name="tags" label="Tags" extra="Comma-separated. ltm is added when long-term is checked.">
            <Input placeholder="docs, imported" />
          </Form.Item>
          <Form.Item name="conversationId" label="Conversation id">
            <Input placeholder="optional" />
          </Form.Item>
          <Form.Item name="longTerm" valuePropName="checked">
            <Checkbox>Index as long-term memory</Checkbox>
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`Tag ${tagTargetIds.length} memor${tagTargetIds.length === 1 ? "y" : "ies"}`}
        open={tagOpen}
        onCancel={() => setTagOpen(false)}
        onOk={handleTagSubmit}
        confirmLoading={mutating}
        okText="Apply"
      >
        <Form form={tagForm} layout="vertical" initialValues={{ op: "add" }}>
          <Form.Item name="op" label="Operation">
            <Radio.Group
              options={[
                { value: "add", label: "Add" },
                { value: "remove", label: "Remove" },
              ]}
            />
          </Form.Item>
          <Form.Item name="tags" label="Tags" rules={[{ required: true, message: "Enter at least one tag" }]}>
            <Input placeholder="important, reviewed" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
