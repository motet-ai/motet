/**
 * Motet - Ops Dashboard - Scope Selector
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Dropdown selectors for tenant and motet scope in the header.
 *     Loads options from the tenant/Motet catalog API (ADR-0126).
 *
 *     "All Tenants" and "All Motets" are explicit options rather than an empty
 *     value, so selecting the whole fleet is as discoverable as picking one
 *     tenant. They still map to null in scope state. Note that the platform
 *     tenant (motet-global) is an ordinary catalog row, not a synonym for
 *     "All Tenants".
 *
 * Last Modified: 2026-08-24
 *
 * Notes:
 *     Catalog fetch is ``enabled`` only after login. Fetching behind the
 *     login modal cached ``missing bearer token`` until a full refresh.
 */
import { Alert, Button, Select, Space, Typography } from "antd";
import { GlobalOutlined, ApartmentOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { Scope } from "../hooks/useScope";
import {
  TENANTS_CATALOG_QUERY_KEY,
  ensureTenantDefaults,
  fetchTenantCatalog,
  type MotetInfo,
} from "../api/tenants";

const { Text } = Typography;

// Widget-level values for the explicit "all" options. Scope state keeps using
// null, so nothing downstream needs to know about these.
const ALL_TENANTS_OPTION = "__all_tenants__";
const ALL_MOTETS_OPTION = "__all_motets__";

interface ScopeSelectorProps {
  scope: Scope;
  onChange: (scope: Partial<Scope>) => void;
  enabled?: boolean;
}

export function ScopeSelector({ scope, onChange, enabled = true }: ScopeSelectorProps) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const { data, isLoading, isError, error } = useQuery({
    queryKey: [...TENANTS_CATALOG_QUERY_KEY, enabled ? "in" : "out"],
    queryFn: () => fetchTenantCatalog(true),
    enabled,
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });

  const seedMutation = useMutation({
    mutationFn: ensureTenantDefaults,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: TENANTS_CATALOG_QUERY_KEY });
    },
  });

  const canAccessAll = data?.can_access_all_tenants ?? false;
  const tenants = (data?.tenants ?? []).filter((t) => t.status !== "disabled");
  const allMotets: MotetInfo[] = tenants.flatMap((t) =>
    (t.motets ?? [])
      .filter((m) => m.status !== "disabled")
      .map((m) => ({
        ...m,
        tenant_id: m.tenant_id || t.id,
      }))
  );

  const motets = scope.tenantId
    ? allMotets.filter((m) => m.tenant_id === scope.tenantId)
    : allMotets;

  const catalogEmpty = !isLoading && !isError && tenants.length === 0;

  return (
    <Space size="small" className="scope-selector" align="center">
      <span style={{ color: "#888", fontSize: 13 }}>Scope:</span>

      <Select
        value={
          scope.tenantId ?? (canAccessAll ? ALL_TENANTS_OPTION : undefined)
        }
        onChange={(value) =>
          onChange({
            tenantId: value === ALL_TENANTS_OPTION ? null : value,
            motetId: null,
          })
        }
        style={{ minWidth: 140 }}
        loading={isLoading}
        placeholder="Select Tenant"
        suffixIcon={<GlobalOutlined />}
        notFoundContent={catalogEmpty ? "No tenants in catalog" : undefined}
      >
        {canAccessAll && (
          <Select.Option key={ALL_TENANTS_OPTION} value={ALL_TENANTS_OPTION}>
            All Tenants
          </Select.Option>
        )}
        {tenants.map((t) => (
          <Select.Option key={t.id} value={t.id}>
            {t.name}
          </Select.Option>
        ))}
      </Select>

      <span style={{ color: "#ccc" }}>/</span>

      <Select
        value={scope.motetId ?? ALL_MOTETS_OPTION}
        onChange={(value) =>
          onChange({ motetId: value === ALL_MOTETS_OPTION ? null : value })
        }
        style={{ minWidth: 130 }}
        loading={isLoading}
        suffixIcon={<ApartmentOutlined />}
        notFoundContent={
          scope.tenantId && motets.length === 0
            ? "No Motets for tenant"
            : undefined
        }
      >
        <Select.Option key={ALL_MOTETS_OPTION} value={ALL_MOTETS_OPTION}>
          All Motets
        </Select.Option>
        {motets.map((m) => (
          <Select.Option key={`${m.tenant_id}:${m.id}`} value={m.id}>
            {m.name}
          </Select.Option>
        ))}
      </Select>

      {catalogEmpty && canAccessAll && (
        <Alert
          type="info"
          showIcon
          banner
          style={{ padding: "2px 8px", maxWidth: 420 }}
          title={
            <Space size="small">
              <Text style={{ fontSize: 12 }}>Catalog empty</Text>
              <Button
                size="small"
                type="link"
                loading={seedMutation.isPending}
                onClick={() => seedMutation.mutate()}
              >
                Seed Defaults
              </Button>
              <Button size="small" type="link" onClick={() => navigate("/tenants")}>
                Manage
              </Button>
            </Space>
          }
        />
      )}

      {isError && (
        <Text type="danger" style={{ fontSize: 11 }}>
          Catalog unavailable
          {error instanceof Error ? `: ${error.message}` : ""}
        </Text>
      )}
    </Space>
  );
}
