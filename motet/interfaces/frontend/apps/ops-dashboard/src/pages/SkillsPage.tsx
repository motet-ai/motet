/**
 * Motet - Ops Dashboard - Skills Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Top-level operator view for installed Agent Skills discovered from
 *     deployed bundle catalogs.
 *
 * Last Modified: 2026-08-24
 */
import { useMemo, useState } from "react";
import { Alert, Card, Input, Space, Table, Tag, Typography } from "antd";
import { BookOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import { scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";

const { Title, Text } = Typography;

interface SkillsPageProps {
  scope: Scope;
}

interface BundleSkill {
  skill_id?: string;
  name?: string;
  description?: string;
  path?: string;
  dir?: string;
  dir_matches_name?: boolean;
  source?: string;
  bundle_id: string;
  bundle_version?: string;
  base_image_stack?: string;
  runtime_capabilities?: string[];
  requirements_path?: string;
  oci_image_ref?: string;
  execution_available?: boolean;
}

interface ListResponse {
  skills: BundleSkill[];
  total: number;
}

interface SkillRow {
  key: string;
  skillId: string;
  name: string;
  description?: string;
  bundleId: string;
  bundleVersion?: string;
  bundleStatus?: string;
  path?: string;
  dir?: string;
  dirMatchesName?: boolean;
  baseImageStack?: string;
  runtimeCapabilities: string[];
  requirementsPath?: string;
  ociImageRef?: string;
  executionAvailable: boolean;
}

async function fetchSkills(scope: Scope): Promise<ListResponse> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl("/api/v1/skills", scope), { headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  const data = await response.json();
  return { skills: data.skills ?? [], total: data.total ?? 0 };
}

function skillRowsFromResponse(skills: BundleSkill[]): SkillRow[] {
  return skills.map((skill, index) => {
    const name = skill.name || skill.dir || "skill";
    const skillId = skill.skill_id || `${skill.bundle_id}.${name}`;
    return {
      key: `${skill.bundle_id}:${skillId}:${index}`,
      skillId,
      name,
      description: skill.description,
      bundleId: skill.bundle_id,
      bundleVersion: skill.bundle_version,
      path: skill.path,
      dir: skill.dir,
      dirMatchesName: skill.dir_matches_name,
      baseImageStack: skill.base_image_stack,
      runtimeCapabilities: skill.runtime_capabilities ?? [],
      requirementsPath: skill.requirements_path,
      ociImageRef: skill.oci_image_ref,
      executionAvailable: skill.execution_available ?? true,
    };
  });
}

export function SkillsPage({ scope }: SkillsPageProps) {
  const [search, setSearch] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["skills", scope.tenantId, scope.motetId],
    queryFn: () => fetchSkills(scope),
    refetchInterval: 5000,
  });

  const rows = useMemo(
    () => skillRowsFromResponse(data?.skills ?? []).sort((a, b) => a.skillId.localeCompare(b.skillId)),
    [data?.skills],
  );
  const bundlesWithSkills = useMemo(
    () => new Set(rows.map((row) => row.bundleId)).size,
    [rows],
  );
  const executableRows = rows.filter((row) => row.baseImageStack || row.runtimeCapabilities.length > 0);
  const normalizedSearch = search.trim().toLowerCase();
  const filteredRows = useMemo(() => {
    if (!normalizedSearch) return rows;
    return rows.filter((row) => {
      const haystack = [
        row.skillId,
        row.name,
        row.description ?? "",
        row.bundleId,
        row.path ?? "",
        row.baseImageStack ?? "",
        row.runtimeCapabilities.join(" "),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(normalizedSearch);
    });
  }, [rows, normalizedSearch]);

  return (
    <div>
      <div className="page-header" style={{ marginBottom: 16 }}>
        <Title level={2}>
          <BookOutlined style={{ marginRight: 12 }} />
          Skills
        </Title>
        <Text type="secondary">
          Installed Agent Skills discovered from deployed bundle catalogs
        </Text>
      </div>

      {error && (
        <Alert
          title="Error loading skills"
          description={String(error)}
          type="error"
          style={{ marginBottom: 16 }}
        />
      )}

      <Card size="small" style={{ marginBottom: 16 }}>
        <Space size={24} wrap>
          <div>
            <Text type="secondary">Installed Skills: </Text>
            <Text strong>{rows.length}</Text>
          </div>
          <div>
            <Text type="secondary">Bundles With Skills: </Text>
            <Text strong>{bundlesWithSkills}</Text>
          </div>
          <div>
            <Text type="secondary">Runtime Hints: </Text>
            <Text strong>{executableRows.length}</Text>
          </div>
          <div>
            <Text type="secondary">Visible After Filter: </Text>
            <Text strong>{filteredRows.length}</Text>
          </div>
        </Space>
      </Card>

      <Card>
        <Input.Search
          allowClear
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by skill, bundle, description, path, or runtime capability"
          style={{ maxWidth: 620, marginBottom: 12 }}
        />
        <Table
          rowKey="key"
          loading={isLoading}
          dataSource={filteredRows}
          pagination={{ pageSize: 25 }}
          columns={[
            {
              title: "Skill",
              dataIndex: "skillId",
              key: "skillId",
              width: 260,
              render: (skillId: string, row: SkillRow) => (
                <Space orientation="vertical" size={0}>
                  <Text code>{skillId}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {row.name}
                  </Text>
                </Space>
              ),
            },
            {
              title: "Bundle",
              dataIndex: "bundleId",
              key: "bundleId",
              width: 190,
              render: (bundleId: string, row: SkillRow) => (
                <Space orientation="vertical" size={0}>
                  <Text code>{bundleId}</Text>
                  {row.bundleStatus ? <Tag>{row.bundleStatus}</Tag> : null}
                </Space>
              ),
            },
            {
              title: "Runtime",
              key: "runtime",
              width: 260,
              render: (_: unknown, row: SkillRow) => (
                <Space wrap size={[4, 4]}>
                  {row.baseImageStack ? <Tag color="blue">{row.baseImageStack}</Tag> : null}
                  {row.runtimeCapabilities.map((capability) => (
                    <Tag key={capability} color="purple">
                      {capability}
                    </Tag>
                  ))}
                  {!row.baseImageStack && row.runtimeCapabilities.length === 0 ? (
                    <Text type="secondary">platform default</Text>
                  ) : null}
                </Space>
              ),
            },
            {
              title: "Execution",
              key: "execution",
              width: 180,
              render: (_: unknown, row: SkillRow) => (
                <Space orientation="vertical" size={0}>
                  <Tag color={row.executionAvailable ? "green" : "default"}>
                    {row.executionAvailable ? "workspace shell" : "activation only"}
                  </Tag>
                  {row.requirementsPath ? (
                    <Text code style={{ fontSize: 11 }}>
                      {row.requirementsPath}
                    </Text>
                  ) : null}
                </Space>
              ),
            },
            {
              title: "Path",
              dataIndex: "path",
              key: "path",
              width: 220,
              render: (path: string | undefined, row: SkillRow) => (
                <Space orientation="vertical" size={0}>
                  {path ? <Text code>{path}</Text> : <Text type="secondary">—</Text>}
                  {row.dir && row.dirMatchesName === false ? (
                    <Tag color="orange">dir/name mismatch</Tag>
                  ) : null}
                </Space>
              ),
            },
            {
              title: "Description",
              dataIndex: "description",
              key: "description",
              render: (description: string | undefined) => description || <Text type="secondary">—</Text>,
            },
          ]}
          locale={{ emptyText: "No skills installed" }}
        />
      </Card>
    </div>
  );
}
