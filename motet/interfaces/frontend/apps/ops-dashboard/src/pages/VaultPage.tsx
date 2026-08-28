/**
 * Motet - Ops Dashboard - Vault Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Last Modified: 2026-08-24
 */
import { useState } from "react";
import { Typography, Card, Table, Tag, Button, Space, Alert, Statistic, Tabs, Form, Input, Select, Popconfirm, Descriptions, Modal, theme } from "antd";
import { message } from "../antdApp";
import { LockOutlined, KeyOutlined, ApiOutlined, PlusOutlined, DeleteOutlined, EyeOutlined, EyeInvisibleOutlined, CheckCircleOutlined, CloseCircleOutlined, SyncOutlined } from "@ant-design/icons";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import { scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface VaultPageProps {
  scope: Scope;
}

interface VaultStats {
  total_credentials: number;
  active_credentials: number;
  expired_credentials: number;
  vault_status: string;
}

interface Credential {
  credential_id: string;
  credential_type: string;
  description?: string;
  created_at?: string;
  updated_at?: string;
  expires_at?: string;
  metadata?: Record<string, any>;
}

interface OAuthServer {
  server_id: string;
  status: string;
  authenticated: boolean;
  expires_at?: string;
  scopes?: string[];
  error?: string;
}


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

async function fetchVaultStats(scope: Scope): Promise<VaultStats | null> {
  const response = await fetch(scopedUrl("/api/v1/vault/stats", scope), {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(await parseApiError(response));
  }
  const data = await response.json();
  if (data.status === "success") {
    return data.stats ?? null;
  }
  throw new Error(typeof data?.error === "string" ? data.error : "Vault stats failed");
}

async function fetchCredentials(scope: Scope): Promise<Credential[]> {
  const response = await fetch(scopedUrl("/api/v1/vault/credentials", scope), {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(await parseApiError(response));
  }
  const data = await response.json();
  if (data.status === "success") {
    return data.credentials || data.data?.credentials || [];
  }
  throw new Error(typeof data?.error === "string" ? data.error : "Vault credentials failed");
}

async function fetchOAuthServers(): Promise<OAuthServer[]> {
  const headers = getAuthHeaders();
  
  // Known OAuth server IDs - in production, these would come from config
  const serverIds = ["github", "google", "slack", "microsoft"];
  const servers: OAuthServer[] = [];
  
  for (const serverId of serverIds) {
    try {
      const response = await fetch(`/api/v1/oauth/${serverId}/status`, { headers });
      if (response.ok) {
        const data = await response.json();
        servers.push({
          server_id: serverId,
          status: data.status || "unknown",
          authenticated: data.authenticated || false,
          expires_at: data.expires_at,
          scopes: data.scopes,
        });
      } else {
        servers.push({
          server_id: serverId,
          status: "not_configured",
          authenticated: false,
        });
      }
    } catch {
      servers.push({
        server_id: serverId,
        status: "error",
        authenticated: false,
        error: "Failed to fetch status",
      });
    }
  }
  
  return servers;
}

async function deleteCredential(credentialId: string): Promise<boolean> {
  try {
    const headers = { ...getAuthHeaders(), "Content-Type": "application/json" };
    
    const response = await fetch("/api/v1/vault/credentials", {
      method: "DELETE",
      headers,
      body: JSON.stringify({ credential_id: credentialId }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function addCredential(data: {
  credential_id: string;
  credential_type: string;
  credential_data_json: string;
  description?: string;
  scope?: string;
  security_level?: string;
}): Promise<{ success: boolean; error?: string }> {
  try {
    const trimmed = (data.credential_data_json ?? "").trim();
    if (!trimmed) {
      return { success: false, error: "Credential data (JSON) is required" };
    }
    let credentialData: Record<string, unknown>;
    try {
      credentialData = JSON.parse(trimmed) as Record<string, unknown>;
    } catch {
      return { success: false, error: "Invalid JSON. Use e.g. {\"client_id\": \"...\", \"client_secret\": \"...\"} or {\"value\": \"...\"}" };
    }
    if (typeof credentialData !== "object" || credentialData === null || Array.isArray(credentialData)) {
      return { success: false, error: "Credential data must be a JSON object" };
    }
    const headers = { ...getAuthHeaders(), "Content-Type": "application/json" };
    const credentialType = data.credential_type === "other" ? "custom" : data.credential_type;
    const body = {
      credential_id: data.credential_id,
      credential_data: credentialData,
      credential_type: credentialType,
      scope: data.scope ?? "principal",
      security_level: data.security_level ?? "confidential",
      description: data.description ?? "",
    };
    const response = await fetch("/api/v1/vault/credentials", {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      console.error("Vault store credential failed", response.status, err);
      const detail = typeof err?.detail === "string" ? err.detail : err?.detail?.msg ?? "Request failed";
      return { success: false, error: detail };
    }
    return { success: true };
  } catch (e) {
    console.error("Vault addCredential error", e);
    return { success: false, error: String(e) };
  }
}

async function initiateOAuth(serverId: string): Promise<{ auth_url?: string; error?: string }> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/oauth/${serverId}/initiate`, {
      method: "POST",
      headers,
    });
    return await response.json();
  } catch (e) {
    return { error: String(e) };
  }
}

async function refreshOAuthToken(serverId: string): Promise<boolean> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/oauth/${serverId}/refresh`, {
      method: "POST",
      headers,
    });
    return response.ok;
  } catch {
    return false;
  }
}

async function revokeOAuthToken(serverId: string): Promise<boolean> {
  const headers = getAuthHeaders();
  
  try {
    const response = await fetch(`/api/v1/oauth/${serverId}/revoke`, {
      method: "DELETE",
      headers,
    });
    return response.ok;
  } catch {
    return false;
  }
}

export function VaultPage({ scope }: VaultPageProps) {
  const { token: themeToken } = theme.useToken();
  const [showAddModal, setShowAddModal] = useState(false);
  const [addingCredential, setAddingCredential] = useState(false);
  const [form] = Form.useForm();
  
  const { data: stats, isError: statsIsError, error: statsError, refetch: refetchStats, dataUpdatedAt } = useQuery({
    queryKey: ["vault-stats", scope.tenantId, scope.motetId],
    // Identity auto-fills scope after login; keep the first payload on screen.
    queryFn: () => fetchVaultStats(scope),
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const { data: credentials, isPending: credentialsPending, isError: credentialsIsError, error: credentialsError, refetch: refetchCredentials } = useQuery({
    queryKey: ["vault-credentials", scope.tenantId, scope.motetId],
    queryFn: () => fetchCredentials(scope),
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const { data: oauthServers, isPending: oauthPending, refetch: refetchOAuth } = useQuery({
    queryKey: ["oauth-servers"],
    queryFn: fetchOAuthServers,
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: false,
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

  const credentialList = credentials || [];
  const oauthList = oauthServers || [];

  const handleDelete = async (credentialId: string) => {
    const success = await deleteCredential(credentialId);
    if (success) {
      message.success("Credential deleted");
      refetchCredentials();
      refetchStats();
    } else {
      message.error("Failed to delete credential");
    }
  };

  const handleAddCredential = async (values: Record<string, unknown>) => {
    setAddingCredential(true);
    try {
      const result = await addCredential({
        credential_id: String(values.credential_id ?? ""),
        credential_type: String(values.credential_type ?? ""),
        credential_data_json: String(values.credential_data_json ?? ""),
        description: values.description != null ? String(values.description) : undefined,
        scope: values.scope != null ? String(values.scope) : undefined,
        security_level: values.security_level != null ? String(values.security_level) : undefined,
      });
      if (result.success) {
        message.success("Credential added");
        setShowAddModal(false);
        form.resetFields();
        refetchCredentials();
        refetchStats();
      } else {
        message.error(result.error ?? "Failed to add credential");
      }
    } finally {
      setAddingCredential(false);
    }
  };

  const handleOAuthConnect = async (serverId: string) => {
    const result = await initiateOAuth(serverId);
    if (result.auth_url) {
      window.open(result.auth_url, "_blank", "width=600,height=700");
      message.info("OAuth window opened. Complete authentication there.");
    } else {
      message.error(result.error || "Failed to initiate OAuth");
    }
  };

  const handleOAuthRefresh = async (serverId: string) => {
    const success = await refreshOAuthToken(serverId);
    if (success) {
      message.success("Token refreshed");
      refetchOAuth();
    } else {
      message.error("Failed to refresh token");
    }
  };

  const handleOAuthRevoke = async (serverId: string) => {
    const success = await revokeOAuthToken(serverId);
    if (success) {
      message.success("Token revoked");
      refetchOAuth();
    } else {
      message.error("Failed to revoke token");
    }
  };

  const credentialColumns = [
    {
      title: "Credential ID",
      dataIndex: "credential_id",
      key: "credential_id",
      sorter: (a: Credential, b: Credential) => (a.credential_id || "").localeCompare(b.credential_id || ""),
      render: (id: string) => <Text code>{id}</Text>,
    },
    {
      title: "Type",
      dataIndex: "credential_type",
      key: "credential_type",
      sorter: (a: Credential, b: Credential) => (a.credential_type || "").localeCompare(b.credential_type || ""),
      filters: [...new Set(credentialList.map(c => c.credential_type).filter(Boolean))].map(t => ({ text: t, value: t })),
      onFilter: (value: any, record: Credential) => record.credential_type === value,
      render: (type: string) => <Tag color="blue">{type}</Tag>,
    },
    {
      title: "Description",
      dataIndex: "description",
      key: "description",
      ellipsis: true,
      render: (desc: string) => desc || <Text type="secondary">—</Text>,
    },
    {
      title: "Created",
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
      sorter: (a: Credential, b: Credential) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime(),
      defaultSortOrder: "descend" as const,
      render: (ts: string) => ts ? new Date(ts).toLocaleString() : "—",
    },
    {
      title: "Actions",
      key: "actions",
      width: 100,
      render: (_: any, record: Credential) => (
        <Popconfirm
          title="Delete Credential"
          description="Are you sure you want to delete this credential?"
          onConfirm={() => handleDelete(record.credential_id)}
          okText="Delete"
          cancelText="Cancel"
          okButtonProps={{ danger: true }}
        >
          <Button size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ];

  const oauthColumns = [
    {
      title: "Server",
      dataIndex: "server_id",
      key: "server_id",
      render: (id: string) => <Text strong style={{ textTransform: "capitalize" }}>{id}</Text>,
    },
    {
      title: "Status",
      key: "status",
      render: (_: any, record: OAuthServer) => {
        if (record.authenticated) {
          return <Tag color="success" icon={<CheckCircleOutlined />}>Connected</Tag>;
        }
        if (record.status === "not_configured") {
          return <Tag color="default">Not Configured</Tag>;
        }
        if (record.status === "error") {
          return <Tag color="error" icon={<CloseCircleOutlined />}>Error</Tag>;
        }
        return <Tag color="warning">Not Connected</Tag>;
      },
    },
    {
      title: "Expires",
      dataIndex: "expires_at",
      key: "expires_at",
      render: (ts: string) => ts ? new Date(ts).toLocaleString() : "—",
    },
    {
      title: "Scopes",
      dataIndex: "scopes",
      key: "scopes",
      render: (scopes: string[]) => scopes?.length ? (
        <Space wrap size={4}>
          {scopes.slice(0, 3).map(s => <Tag key={s} style={{ fontSize: 10 }}>{s}</Tag>)}
          {scopes.length > 3 && <Tag>+{scopes.length - 3}</Tag>}
        </Space>
      ) : "—",
    },
    {
      title: "Actions",
      key: "actions",
      width: 200,
      render: (_: any, record: OAuthServer) => (
        <Space size={4}>
          {!record.authenticated && record.status !== "not_configured" && (
            <Button size="small" type="primary" onClick={() => handleOAuthConnect(record.server_id)}>
              Connect
            </Button>
          )}
          {record.authenticated && (
            <>
              <Button size="small" icon={<SyncOutlined />} onClick={() => handleOAuthRefresh(record.server_id)}>
                Refresh
              </Button>
              <Popconfirm
                title="Revoke OAuth Token"
                description="This will disconnect this OAuth integration."
                onConfirm={() => handleOAuthRevoke(record.server_id)}
                okText="Revoke"
                cancelText="Cancel"
                okButtonProps={{ danger: true }}
              >
                <Button size="small" danger>Revoke</Button>
              </Popconfirm>
            </>
          )}
        </Space>
      ),
    },
  ];

  const credentialTypes = [
    { value: "api_key", label: "API Key" },
    { value: "oauth_token", label: "OAuth Token" },
    { value: "database_password", label: "Database Password" },
    { value: "ssh_key", label: "SSH Key" },
    { value: "certificate", label: "Certificate" },
    { value: "service_account_key", label: "Service Account Key" },
    { value: "other", label: "Other" },
  ];

  const scopeOptions = [
    { value: "principal", label: "Principal (only me)" },
    { value: "tenant", label: "Tenant (my org)" },
    { value: "motet", label: "Motet" },
    { value: "global", label: "Global (e.g. OAuth client credentials)" },
  ];

  const securityLevelOptions = [
    { value: "public", label: "Public" },
    { value: "internal", label: "Internal" },
    { value: "confidential", label: "Confidential" },
    { value: "secret", label: "Secret" },
    { value: "top_secret", label: "Top Secret" },
  ];

  const tabItems = [
    {
      key: "credentials",
      label: (
        <span>
          <KeyOutlined />
          Credentials
        </span>
      ),
      children: (
        <>
          <div style={{ marginBottom: 16, display: "flex", justifyContent: "flex-end" }}>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setShowAddModal(true)}>
              Add Credential
            </Button>
          </div>
          <Table
            dataSource={credentialList}
            columns={credentialColumns}
            rowKey="credential_id"
            loading={credentialsPending}
            pagination={{ pageSize: 10 }}
            size="small"
            locale={{ emptyText: "No credentials stored" }}
          />
        </>
      ),
    },
    {
      key: "oauth",
      label: (
        <span>
          <ApiOutlined />
          OAuth Connections
        </span>
      ),
      children: (
        <>
          <Alert
            title="OAuth Integration"
            description="Manage OAuth authentication for MCP servers. The OAuth Proxy Service handles authentication flows and stores tokens securely in the vault."
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
          />
          <Table
            dataSource={oauthList}
            columns={oauthColumns}
            rowKey="server_id"
            loading={oauthPending}
            pagination={false}
            size="small"
            locale={{ emptyText: "No OAuth servers configured" }}
          />
        </>
      ),
    },
  ];

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <LockOutlined style={{ marginRight: 12 }} />
            Vault
          </Title>
          <Text type="secondary">Manage credentials and OAuth connections</Text>
        </div>
        {dataUpdatedAt && (
          <Text type="secondary" style={{ fontSize: 11 }}>
            Updated: {formatLastUpdated(dataUpdatedAt)}
          </Text>
        )}
      </div>

      {(credentialsIsError || statsIsError) && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          title="Vault unavailable"
          description={
            (credentialsError instanceof Error && credentialsError.message) ||
            (statsError instanceof Error && statsError.message) ||
            "Credential list or stats failed. Check that you are signed in as admin."
          }
        />
      )}

      {/* Stats Row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 16 }}>
        <Card size="small">
          <Statistic
            title="Total Credentials"
            value={stats?.total_credentials || 0}
          />
        </Card>
        <Card size="small">
          <Statistic
            title="Active Credentials"
            value={stats?.active_credentials || 0}
            styles={{ content: { color: themeToken.colorSuccess } }}
          />
        </Card>
        <Card size="small">
          <Statistic
            title="Expired Credentials"
            value={stats?.expired_credentials || 0}
            styles={{ content: { color: (stats?.expired_credentials || 0) > 0 ? themeToken.colorError : undefined } }}
          />
        </Card>
        <Card size="small">
          <Statistic
            title="Vault Status"
            value={stats?.vault_status === "healthy" ? "Healthy" : stats?.vault_status === "unhealthy" ? "Unhealthy" : stats?.vault_status || "Unknown"}
            styles={{ content: { color: stats?.vault_status === "healthy" ? themeToken.colorSuccess : stats?.vault_status === "unhealthy" ? themeToken.colorError : undefined } }}
          />
        </Card>
      </div>

      <Card>
        <Tabs items={tabItems} />
      </Card>

      {/* Add Credential Modal */}
      <Modal
        title="Add New Credential"
        open={showAddModal}
        onCancel={() => setShowAddModal(false)}
        footer={null}
      >
        <Form form={form} layout="vertical" onFinish={handleAddCredential} initialValues={{ scope: "principal", security_level: "confidential" }}>
          <Form.Item
            name="credential_id"
            label="Credential ID"
            rules={[{ required: true, message: "Please enter a credential ID" }]}
            extra="For OAuth client credentials use: oauth:client_credentials:{server_id} (e.g. oauth:client_credentials:google_workspace) and set Scope to Global."
          >
            <Input placeholder="e.g., openai-api-key or oauth:client_credentials:google_workspace" />
          </Form.Item>
          <Form.Item
            name="credential_type"
            label="Type"
            rules={[{ required: true, message: "Please select a type" }]}
          >
            <Select options={credentialTypes} placeholder="Select credential type" />
          </Form.Item>
          <Form.Item
            name="scope"
            label="Scope"
          >
            <Select options={scopeOptions} placeholder="Who can access this credential" />
          </Form.Item>
          <Form.Item
            name="security_level"
            label="Security level"
          >
            <Select options={securityLevelOptions} placeholder="Classification level" />
          </Form.Item>
          <Form.Item
            name="credential_data_json"
            label="Credential Data (JSON)"
            rules={[{ required: true, message: "Please enter credential data as JSON" }]}
            extra='Enter the credential payload as JSON. For OAuth client credentials use: {"client_id": "...", "client_secret": "..."}. For simple values: {"value": "..."}. Other examples: {"api_key": "sk-..."}, {"token": "abc123"}.'
          >
            <Input.TextArea
              rows={4}
              placeholder='{"client_id": "your-client-id", "client_secret": "your-secret"}'
              style={{ fontFamily: "monospace", fontSize: 13 }}
            />
          </Form.Item>
          <Form.Item name="description" label="Description">
            <Input.TextArea placeholder="Optional description" rows={2} />
          </Form.Item>
          <Form.Item>
            <Space wrap>
              <Button type="primary" htmlType="submit" loading={addingCredential}>
                Add Credential
              </Button>
              <Button
                onClick={() => {
                  const json = form.getFieldValue("credential_data_json")?.trim() ?? "";
                  if (!json) {
                    message.warning("Enter credential data first");
                    return;
                  }
                  try {
                    const parsed = JSON.parse(json);
                    message.success("Valid JSON");
                    console.log("Credential data (parsed):", parsed);
                  } catch (e) {
                    message.error("Invalid JSON: " + (e instanceof Error ? e.message : String(e)));
                  }
                }}
              >
                Validate JSON
              </Button>
              <Button onClick={() => setShowAddModal(false)}>Cancel</Button>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
