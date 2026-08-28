/**
 * Motet - Ops Dashboard - Tenants Catalog Page (ADR-0126)
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Manage the tenant / Motet (environment) catalog used by the header scope selector.
 *
 * Last Modified: 2026-08-24
 */
import { useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { message } from "../antdApp";
import { GlobalOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Scope } from "../hooks/useScope";
import {
  TENANTS_CATALOG_QUERY_KEY,
  createMotet,
  createTenant,
  deleteMotet,
  deleteTenant,
  ensureTenantDefaults,
  fetchTenantCatalog,
  updateMotet,
  updateTenant,
  type MotetInfo,
  type TenantInfo,
} from "../api/tenants";

const { Title, Text } = Typography;

interface TenantsPageProps {
  scope: Scope;
}

export function TenantsPage({ scope }: TenantsPageProps) {
  void scope;
  const queryClient = useQueryClient();
  const [tenantModalOpen, setTenantModalOpen] = useState(false);
  const [motetModalOpen, setMotetModalOpen] = useState(false);
  const [motetTenantId, setMotetTenantId] = useState<string | null>(null);
  const [tenantForm] = Form.useForm();
  const [motetForm] = Form.useForm();

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: TENANTS_CATALOG_QUERY_KEY,
    queryFn: () => fetchTenantCatalog(true),
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: TENANTS_CATALOG_QUERY_KEY });

  const seedMutation = useMutation({
    mutationFn: ensureTenantDefaults,
    onSuccess: (result) => {
      message.success(
        `Defaults ensured (created tenants=${result.created.tenants ?? 0}, motets=${result.created.motets ?? 0})`
      );
      void invalidate();
    },
    onError: (err: Error) => message.error(err.message),
  });

  const createTenantMutation = useMutation({
    mutationFn: createTenant,
    onSuccess: () => {
      message.success("Tenant created");
      setTenantModalOpen(false);
      tenantForm.resetFields();
      void invalidate();
    },
    onError: (err: Error) => message.error(err.message),
  });

  const createMotetMutation = useMutation({
    mutationFn: (values: {
      tenant_id: string;
      id: string;
      name?: string;
      description?: string;
    }) =>
      createMotet(values.tenant_id, {
        id: values.id,
        name: values.name,
        description: values.description,
      }),
    onSuccess: () => {
      message.success("Motet created");
      setMotetModalOpen(false);
      motetForm.resetFields();
      setMotetTenantId(null);
      void invalidate();
    },
    onError: (err: Error) => message.error(err.message),
  });

  const tenants = data?.tenants ?? [];
  const canManage = data?.can_access_all_tenants ?? false;

  const motetColumns = [
    {
      title: "Motet ID",
      dataIndex: "id",
      key: "id",
      render: (id: string) => <Text code>{id}</Text>,
    },
    {
      title: "Name",
      dataIndex: "name",
      key: "name",
    },
    {
      title: "Status",
      dataIndex: "status",
      key: "status",
      render: (status: string) => (
        <Tag color={status === "active" ? "green" : "default"}>{status}</Tag>
      ),
    },
    {
      title: "Actions",
      key: "actions",
      render: (_: unknown, record: MotetInfo) =>
        canManage ? (
          <Space size="small">
            <Button
              size="small"
              onClick={() => {
                const next =
                  record.status === "disabled" ? "active" : "disabled";
                void updateMotet(record.tenant_id, record.id, { status: next })
                  .then(() => {
                    message.success(`Motet ${next}`);
                    void invalidate();
                  })
                  .catch((err: Error) => message.error(err.message));
              }}
            >
              {record.status === "disabled" ? "Enable" : "Disable"}
            </Button>
            <Popconfirm
              title="Delete this Motet?"
              onConfirm={() =>
                void deleteMotet(record.tenant_id, record.id)
                  .then(() => {
                    message.success("Motet deleted");
                    void invalidate();
                  })
                  .catch((err: Error) => message.error(err.message))
              }
            >
              <Button size="small" danger>
                Delete
              </Button>
            </Popconfirm>
          </Space>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ];

  const columns = [
    {
      title: "Tenant ID",
      dataIndex: "id",
      key: "id",
      render: (id: string) => <Text code>{id}</Text>,
    },
    {
      title: "Name",
      dataIndex: "name",
      key: "name",
    },
    {
      title: "Status",
      dataIndex: "status",
      key: "status",
      render: (status: string) => (
        <Tag color={status === "active" ? "green" : "default"}>{status}</Tag>
      ),
    },
    {
      title: "Motets",
      key: "motet_count",
      render: (_: unknown, record: TenantInfo) => record.motets?.length ?? 0,
    },
    {
      title: "Actions",
      key: "actions",
      render: (_: unknown, record: TenantInfo) =>
        canManage ? (
          <Space size="small">
            <Button
              size="small"
              onClick={() => {
                setMotetTenantId(record.id);
                motetForm.setFieldsValue({ tenant_id: record.id });
                setMotetModalOpen(true);
              }}
            >
              Add Motet
            </Button>
            <Button
              size="small"
              onClick={() => {
                const next =
                  record.status === "disabled" ? "active" : "disabled";
                void updateTenant(record.id, { status: next })
                  .then(() => {
                    message.success(`Tenant ${next}`);
                    void invalidate();
                  })
                  .catch((err: Error) => message.error(err.message));
              }}
            >
              {record.status === "disabled" ? "Enable" : "Disable"}
            </Button>
            <Popconfirm
              title="Delete this tenant and all Motets?"
              description="Uses force delete."
              onConfirm={() =>
                void deleteTenant(record.id, true)
                  .then(() => {
                    message.success("Tenant deleted");
                    void invalidate();
                  })
                  .catch((err: Error) => message.error(err.message))
              }
            >
              <Button size="small" danger>
                Delete
              </Button>
            </Popconfirm>
          </Space>
        ) : (
          <Text type="secondary">Read only</Text>
        ),
    },
  ];

  return (
    <div>
      <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
        <div
          className="page-header"
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "flex-start",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          <div>
            <Title level={2}>
              <GlobalOutlined style={{ marginRight: 12 }} />
              Tenants
            </Title>
            <Text type="secondary">
              Catalog of tenants and Motets (environments) for the header scope
              selector. Identity still comes from JWT/service accounts; this
              page only manages the operator catalog.
            </Text>
          </div>
          <Space wrap>
            <Button
              icon={<ReloadOutlined />}
              loading={isFetching}
              onClick={() => void refetch()}
            >
              Refresh
            </Button>
            {canManage && (
              <>
                <Button
                  loading={seedMutation.isPending}
                  onClick={() => seedMutation.mutate()}
                >
                  Seed Defaults
                </Button>
                <Button
                  type="primary"
                  icon={<PlusOutlined />}
                  onClick={() => setTenantModalOpen(true)}
                >
                  Create Tenant
                </Button>
              </>
            )}
          </Space>
        </div>

        {!canManage && (
          <Alert
            type="info"
            showIcon
            title="You can view your tenant only. Creating or deleting catalog entries requires admin."
          />
        )}

        {error && (
          <Alert
            type="error"
            showIcon
            title="Failed to load tenant catalog"
            description={error instanceof Error ? error.message : String(error)}
          />
        )}

        <Table
          rowKey="id"
          loading={isLoading}
          columns={columns}
          dataSource={tenants}
          pagination={false}
          expandable={{
            expandedRowRender: (record: TenantInfo) => (
              <Table
                size="small"
                rowKey="id"
                columns={motetColumns}
                dataSource={record.motets ?? []}
                pagination={false}
              />
            ),
            rowExpandable: (record) => (record.motets?.length ?? 0) > 0,
          }}
          locale={{
            emptyText: canManage
              ? "No tenants yet. Use Seed Defaults or Create Tenant."
              : "No tenants visible for your principal.",
          }}
        />
      </Space>

      <Modal
        title="Create Tenant"
        open={tenantModalOpen}
        onCancel={() => setTenantModalOpen(false)}
        onOk={() => tenantForm.submit()}
        confirmLoading={createTenantMutation.isPending}
        destroyOnHidden
      >
        <Form
          form={tenantForm}
          layout="vertical"
          onFinish={(values) => createTenantMutation.mutate(values)}
        >
          <Form.Item
            name="id"
            label="Tenant ID"
            rules={[{ required: true, message: "Required" }]}
          >
            <Input placeholder="acme" />
          </Form.Item>
          <Form.Item name="name" label="Name">
            <Input placeholder="Acme Corp" />
          </Form.Item>
          <Form.Item name="description" label="Description">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="Create Motet"
        open={motetModalOpen}
        onCancel={() => {
          setMotetModalOpen(false);
          setMotetTenantId(null);
        }}
        onOk={() => motetForm.submit()}
        confirmLoading={createMotetMutation.isPending}
        destroyOnHidden
      >
        <Form
          form={motetForm}
          layout="vertical"
          initialValues={{ tenant_id: motetTenantId ?? undefined }}
          onFinish={(values) => createMotetMutation.mutate(values)}
        >
          <Form.Item
            name="tenant_id"
            label="Tenant"
            rules={[{ required: true, message: "Required" }]}
          >
            <Select
              options={tenants.map((t) => ({ value: t.id, label: t.name }))}
              placeholder="Select tenant"
            />
          </Form.Item>
          <Form.Item
            name="id"
            label="Motet ID"
            rules={[{ required: true, message: "Required" }]}
          >
            <Input placeholder="prod" />
          </Form.Item>
          <Form.Item name="name" label="Name">
            <Input placeholder="Production" />
          </Form.Item>
          <Form.Item name="description" label="Description">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
