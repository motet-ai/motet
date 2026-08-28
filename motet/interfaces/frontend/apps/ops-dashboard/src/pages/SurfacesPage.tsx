/**
 * Motet - Ops Dashboard - Surfaces Catalog Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Platform catalog of conversation surfaces (ADR-0083). Shows which agents
 *     may use each surface (explicit allow-list vs default-all).
 *
 * Last Modified: 2026-08-24
 */
import { useMemo, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { message } from "../antdApp";
import { AppstoreOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import type { Scope } from "../hooks/useScope";
import {
  SURFACES_CATALOG_QUERY_KEY,
  createSurface,
  deleteSurface,
  fetchSurfaces,
  updateSurface,
  type SurfaceInfo,
} from "../api/surfaces";

const { Title, Text } = Typography;

interface SurfacesPageProps {
  scope: Scope;
}

interface AgentListItem {
  qualified_id: string;
  display_name?: string;
  allowed_surface_ids?: string[] | null;
}


async function fetchAgents(): Promise<AgentListItem[]> {
  const response = await fetch("/api/v1/agents", { headers: getAuthHeaders() });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  const data = await response.json();
  return Array.isArray(data?.agents) ? data.agents : [];
}

function agentsForSurface(surfaceId: string, agents: AgentListItem[]): {
  qualified_id: string;
  display_name?: string;
  access: "explicit" | "all";
}[] {
  return agents
    .filter((agent) => {
      const allowed = agent.allowed_surface_ids;
      if (allowed == null || allowed.length === 0) return true;
      return allowed.includes(surfaceId);
    })
    .map((agent) => {
      const allowed = agent.allowed_surface_ids;
      const access: "explicit" | "all" =
        allowed == null || allowed.length === 0 ? "all" : "explicit";
      return {
        qualified_id: agent.qualified_id,
        display_name: agent.display_name,
        access,
      };
    })
    .sort((a, b) => a.qualified_id.localeCompare(b.qualified_id));
}

export function SurfacesPage({ scope }: SurfacesPageProps) {
  void scope;
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [editSurface, setEditSurface] = useState<SurfaceInfo | null>(null);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: SURFACES_CATALOG_QUERY_KEY,
    queryFn: fetchSurfaces,
  });

  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: fetchAgents,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: SURFACES_CATALOG_QUERY_KEY });

  const createMutation = useMutation({
    mutationFn: createSurface,
    onSuccess: () => {
      message.success("Surface created");
      setCreateOpen(false);
      createForm.resetFields();
      void invalidate();
    },
    onError: (err: Error) => message.error(err.message),
  });

  const updateMutation = useMutation({
    mutationFn: (values: { id: string; display_name?: string; description?: string }) =>
      updateSurface(values.id, {
        display_name: values.display_name,
        description: values.description,
      }),
    onSuccess: () => {
      message.success("Surface updated");
      setEditSurface(null);
      editForm.resetFields();
      void invalidate();
    },
    onError: (err: Error) => message.error(err.message),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteSurface,
    onSuccess: () => {
      message.success("Surface deleted");
      void invalidate();
    },
    onError: (err: Error) => message.error(err.message),
  });

  const surfaces = data?.surfaces ?? [];
  const canManage = data?.can_manage ?? false;

  const rows = useMemo(
    () =>
      surfaces.map((surface) => ({
        ...surface,
        key: surface.id,
        agents: agentsForSurface(surface.id, agents),
      })),
    [surfaces, agents],
  );

  const columns = [
    {
      title: "Surface",
      dataIndex: "id",
      key: "id",
      sorter: (a: SurfaceInfo, b: SurfaceInfo) => a.id.localeCompare(b.id),
      render: (_: string, record: SurfaceInfo) => (
        <div>
          <Text strong style={{ fontFamily: "monospace" }}>
            {record.id}
          </Text>
          <div>
            <Text type="secondary">{record.display_name}</Text>
          </div>
        </div>
      ),
    },
    {
      title: "Description",
      dataIndex: "description",
      key: "description",
      ellipsis: true,
      render: (d: string | null | undefined) =>
        d ? <Text>{d}</Text> : <Text type="secondary">—</Text>,
    },
    {
      title: "Type",
      dataIndex: "builtin",
      key: "builtin",
      width: 110,
      render: (builtin: boolean) =>
        builtin ? <Tag color="blue">builtin</Tag> : <Tag>custom</Tag>,
    },
    {
      title: "Agents with access",
      key: "agents",
      render: (_: unknown, record: { agents: ReturnType<typeof agentsForSurface> }) => {
        if (!record.agents.length) {
          return <Text type="secondary">None</Text>;
        }
        return (
          <Space size={[4, 4]} wrap>
            {record.agents.map((agent) => (
              <Tag
                key={agent.qualified_id}
                color={agent.access === "all" ? "default" : "green"}
                title={
                  agent.access === "all"
                    ? "All surfaces (default)"
                    : "Explicit allow-list"
                }
              >
                {agent.display_name || agent.qualified_id}
                {agent.access === "all" ? " · all" : ""}
              </Tag>
            ))}
          </Space>
        );
      },
    },
    {
      title: "Actions",
      key: "actions",
      width: 180,
      render: (_: unknown, record: SurfaceInfo) => (
        <Space>
          <Button
            size="small"
            disabled={!canManage}
            onClick={() => {
              setEditSurface(record);
              editForm.setFieldsValue({
                display_name: record.display_name,
                description: record.description ?? "",
              });
            }}
          >
            Edit
          </Button>
          <Popconfirm
            title="Delete this surface?"
            disabled={!canManage || record.builtin}
            onConfirm={() => deleteMutation.mutate(record.id)}
          >
            <Button
              size="small"
              danger
              disabled={!canManage || record.builtin}
              loading={deleteMutation.isPending}
            >
              Delete
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div
        className="page-header"
        style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}
      >
        <div>
          <Title level={2}>
            <AppstoreOutlined style={{ marginRight: 12 }} />
            Surfaces
          </Title>
          <Text type="secondary">
            Conversation channel catalog. Agents with empty allow-lists inherit all surfaces.
          </Text>
        </div>
        <Space>
          <Button icon={<ReloadOutlined />} onClick={() => refetch()} loading={isFetching}>
            Refresh
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!canManage}
            onClick={() => setCreateOpen(true)}
          >
            Create surface
          </Button>
        </Space>
      </div>

      {error && (
        <Alert
          type="error"
          title="Failed to load surfaces"
          description={String(error)}
          style={{ marginBottom: 16 }}
        />
      )}

      {!canManage && (
        <Alert
          type="info"
          showIcon
          title="Read-only"
          description="Admin or ops_dashboard principal required to create or edit surfaces."
          style={{ marginBottom: 16 }}
        />
      )}

      <Table
        size="small"
        loading={isLoading}
        dataSource={rows}
        columns={columns as any}
        pagination={{ pageSize: 25 }}
      />

      <Modal
        title="Create surface"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        confirmLoading={createMutation.isPending}
        destroyOnHidden
      >
        <Form
          form={createForm}
          layout="vertical"
          onFinish={(values) => createMutation.mutate(values)}
        >
          <Form.Item
            name="id"
            label="Surface ID"
            rules={[
              { required: true, message: "Required" },
              {
                pattern: /^[a-z][a-z0-9_]{1,62}$/,
                message: "Lowercase slug: start with letter, letters/digits/underscore",
              },
            ]}
          >
            <Input placeholder="partner_portal" />
          </Form.Item>
          <Form.Item name="display_name" label="Display name">
            <Input placeholder="Partner Portal" />
          </Form.Item>
          <Form.Item name="description" label="Description">
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={editSurface ? `Edit ${editSurface.id}` : "Edit surface"}
        open={Boolean(editSurface)}
        onCancel={() => setEditSurface(null)}
        onOk={() => editForm.submit()}
        confirmLoading={updateMutation.isPending}
        destroyOnHidden
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={(values) => {
            if (!editSurface) return;
            updateMutation.mutate({ id: editSurface.id, ...values });
          }}
        >
          <Form.Item name="display_name" label="Display name">
            <Input />
          </Form.Item>
          <Form.Item name="description" label="Description">
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
