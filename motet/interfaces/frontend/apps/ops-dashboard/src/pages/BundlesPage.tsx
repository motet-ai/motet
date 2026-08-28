/**
 * Motet - Ops Dashboard - Bundles (Deploy) Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Deploy page: list deployed bundles, deploy form with
 *     Deploy and Validate (lint-only SSE) buttons, upload tab, undeploy with confirm.
 *     Uses GET/POST/DELETE /api/v1/deploy and POST /api/v1/deploy/validate.
 *
 * Last Modified: 2026-08-24
 */
import { useRef, useState } from "react";
import {
  Typography,
  Card,
  Table,
  Tag,
  Button,
  Space,
  Alert,
  Form,
  Input,
  Switch,
  Popconfirm,
  Modal,
  Tabs,
  Descriptions,
} from "antd";
import { message } from "../antdApp";
import {
  CloudUploadOutlined,
  DeleteOutlined,
  ExperimentOutlined,
  DownOutlined,
  RightOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getAuthHeaders } from "../api/http";
import { scopedUrl } from "../api/scope";
import type { Scope } from "../hooks/useScope";

/** Load JSZip at runtime from ESM CDN so we don't depend on node_modules resolution (works in Docker/workspaces). */
async function loadJSZip(): Promise<new () => { file: (path: string, content: Blob | string) => void; generateAsync: (opts: { type: "blob" }) => Promise<Blob> }> {
  const mod = await import(/* @vite-ignore */ "https://esm.sh/jszip@3.10.1" as string);
  return mod.default as typeof import("jszip");
}

const { Title, Text } = Typography;

interface BundlesPageProps {
  scope: Scope;
}

interface BundleCatalog {
  commands: string[];
  tools: string[];
  workflows: string[];
  agents: string[];
  mcp_servers: string[];
  model_ids: string[];
  skills?: Array<{
    id?: string;
    name?: string;
    description?: string;
    path?: string;
  }>;
  // Bundle execution artifact pipeline: pinned image / digest / tier
  // / requirements hash that this bundle@version will pull at run time.
  // Empty object (not undefined) when the bundle did not declare config/exec.yaml.
  exec?: {
    oci_image_ref?: string;
    exec_artifact_digest?: string;
    base_image_stack?: string;
    requirements_path?: string;
    requirements_sha256?: string;
  };
  bundle_version: string;
}

interface BundleTargeting {
  worker_ids?: string[];
  worker_tags?: string[];
  motet_ids?: string[];
  tenant_ids?: string[];
}

interface DeployedBundle {
  bundle_id: string;
  bundle_version?: string;
  bundle_ref?: string;
  status?: string;
  deployed_at?: string | number;
  deploy_job_id?: string;
  manifest_version?: string;
  targeting?: BundleTargeting;
  catalog?: BundleCatalog;
  worker_state?: Record<string, unknown>;
}

interface ListResponse {
  bundles: DeployedBundle[];
  total: number;
}


// Platform-managed image stacks: single registry entry as
// returned by GET /api/v1/exec/image-stacks. Mirrors the Pydantic
// ImageStackResponse on the server.
interface ImageStack {
  name: string;
  oci_image_ref: string;
  description: string;
  builtin: boolean;
  is_pinned: boolean;
}

interface ImageStacksResponse {
  stacks: ImageStack[];
}

async function fetchImageStacks(): Promise<ImageStacksResponse> {
  const headers = getAuthHeaders();
  const response = await fetch("/api/v1/exec/image-stacks", { headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  const data = await response.json();
  return { stacks: data.stacks ?? [] };
}

async function fetchBundles(scope: Scope): Promise<ListResponse> {
  const headers = getAuthHeaders();
  const response = await fetch(scopedUrl("/api/v1/deploy", scope), { headers });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  const data = await response.json();
  return { bundles: data.bundles ?? [], total: data.total ?? 0 };
}

async function deployBundle(body: {
  repo_url: string;
  branch: string;
  path: string;
  repo_creds_path?: string;
  interactive?: boolean;
  targeting?: Record<string, unknown>;
}): Promise<{ deploy_job_id: string; bundle_id: string; status_url: string; status?: string }> {
  const headers = { ...getAuthHeaders(), "Content-Type": "application/json" };
  const response = await fetch("/api/v1/deploy", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? response.statusText);
  }
  return response.json();
}

async function undeployBundle(bundleId: string): Promise<{ status: string }> {
  const headers = getAuthHeaders();
  const response = await fetch(`/api/v1/deploy/${encodeURIComponent(bundleId)}`, {
    method: "DELETE",
    headers,
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error((err as { detail?: string }).detail ?? response.statusText);
  }
  return response.json();
}

export function BundlesPage({ scope }: BundlesPageProps) {
  const [deployForm] = Form.useForm();
  const [validateModalOpen, setValidateModalOpen] = useState(false);
  const [validateLog, setValidateLog] = useState<string[]>([]);
  const [validateLoading, setValidateLoading] = useState(false);
  const [deployLoading, setDeployLoading] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadFolderFiles, setUploadFolderFiles] = useState<File[]>([]);
  const [uploadLoading, setUploadLoading] = useState(false);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const zipInputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();

  const { data, isLoading, error, dataUpdatedAt } = useQuery({
    queryKey: ["bundles", scope.tenantId, scope.motetId],
    queryFn: () => fetchBundles(scope),
    refetchInterval: 5000,
  });

  const bundles = data?.bundles ?? [];
  const total = data?.total ?? 0;

  // Image stacks are platform-scope (not tenant-scope) so we do NOT key the
  // query on scope. Refresh slowly — operators don't change env vars at runtime.
  const { data: stacksData, isLoading: stacksLoading, error: stacksError } = useQuery({
    queryKey: ["image-stacks"],
    queryFn: fetchImageStacks,
    refetchInterval: 60000,
  });
  const imageStacks = stacksData?.stacks ?? [];

  const handleDeploy = async () => {
    const values = await deployForm.validateFields().catch(() => null);
    if (!values) return;
    setDeployLoading(true);
    try {
      const result = await deployBundle({
        repo_url: values.repo_url,
        branch: values.branch,
        path: values.path,
        repo_creds_path: values.repo_creds_path || undefined,
        interactive: !!values.interactive,
      });
      message.success(`Deploy started. Bundle: ${result.bundle_id || "(resolving…)"}. Poll status: ${result.status_url}`);
      deployForm.resetFields();
      await queryClient.invalidateQueries({ queryKey: ["bundles"] });
    } catch (e) {
      message.error(e instanceof Error ? e.message : "Deploy failed");
    } finally {
      setDeployLoading(false);
    }
  };

  const handleValidateOnly = async () => {
    const values = await deployForm.validateFields().catch(() => null);
    if (!values) return;
    setValidateModalOpen(true);
    setValidateLog([]);
    setValidateLoading(true);
    const headers = { ...getAuthHeaders(), "Content-Type": "application/json" };
    try {
      const response = await fetch("/api/v1/deploy/validate", {
        method: "POST",
        headers,
        body: JSON.stringify({
          repo_url: values.repo_url,
          branch: values.branch,
          path: values.path,
          repo_creds_path: values.repo_creds_path || undefined,
        }),
      });
      if (!response.ok || !response.body) {
        setValidateLog((prev) => [...prev, `Error: HTTP ${response.status}`]);
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const logLines: string[] = [];
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() ?? "";
        let eventType = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventType = line.slice(7).trim();
          if (line.startsWith("data: ")) {
            const data = line.slice(6).trim();
            try {
              const obj = JSON.parse(data);
              logLines.push(`[${eventType}] ${JSON.stringify(obj)}`);
            } catch {
              logLines.push(`[${eventType}] ${data}`);
            }
          }
        }
      }
      if (buffer.trim()) logLines.push(buffer);
      if (logLines.length === 0) {
        logLines.push("No events received.");
      }
      setValidateLog(logLines);
    } catch (e) {
      setValidateLog((prev) => [...prev, `Error: ${e instanceof Error ? e.message : String(e)}`]);
    } finally {
      setValidateLoading(false);
    }
  };

  const handleUndeploy = async (bundleId: string) => {
    try {
      await undeployBundle(bundleId);
      message.success(`Undeployed ${bundleId}`);
      await queryClient.invalidateQueries({ queryKey: ["bundles"] });
    } catch (e) {
      message.error(e instanceof Error ? e.message : "Undeploy failed");
    }
  };

  const buildZipFromFolder = async (files: File[]): Promise<Blob> => {
    const JSZipClass = await loadJSZip();
    const zip = new JSZipClass();
    if (files.length === 0) throw new Error("No files selected");
    const paths = files.map((f) => (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name);
    const hasRelative = paths.every((p) => p.includes("/"));
    const prefix = hasRelative ? paths[0].split("/")[0] + "/" : "";
    for (const file of files) {
      const rel = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
      const zipPath = prefix ? rel.replace(prefix, "") : rel;
      if (zipPath && !zipPath.endsWith("/")) zip.file(zipPath, file);
    }
    return zip.generateAsync({ type: "blob" });
  };

  const handleUpload = async () => {
    let blob: Blob;
    if (uploadFolderFiles.length > 0) {
      try {
        blob = await buildZipFromFolder(uploadFolderFiles);
      } catch (e) {
        message.error(e instanceof Error ? e.message : "Failed to build zip from folder");
        return;
      }
    } else if (uploadFile) {
      blob = uploadFile;
    } else {
      message.warning("Select a bundle folder or a .zip file first");
      return;
    }
    const hundredMB = 100 * 1024 * 1024;
    if (blob.size > hundredMB) {
      message.error(`Upload exceeds 100MB limit (${(blob.size / (1024 * 1024)).toFixed(1)} MB)`);
      return;
    }
    setUploadLoading(true);
    try {
      const formData = new FormData();
      formData.append("bundle", blob, "bundle.zip");
      const headers = getAuthHeaders();
      const response = await fetch("/api/v1/deploy/upload", {
        method: "POST",
        headers,
        body: formData,
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error((err as { detail?: string }).detail ?? response.statusText);
      }
      const data = await response.json();
      message.success(`Deploy started: ${data.bundle_id || "bundle"}. Poll status: ${data.status_url}`);
      setUploadFile(null);
      setUploadFolderFiles([]);
      await queryClient.invalidateQueries({ queryKey: ["bundles"] });
    } catch (e) {
      message.error(e instanceof Error ? e.message : "Upload deploy failed");
    } finally {
      setUploadLoading(false);
    }
  };

  const handleValidateUpload = async () => {
    let blob: Blob;
    if (uploadFolderFiles.length > 0) {
      try {
        blob = await buildZipFromFolder(uploadFolderFiles);
      } catch (e) {
        message.error(e instanceof Error ? e.message : "Failed to build zip from folder");
        return;
      }
    } else if (uploadFile) {
      blob = uploadFile;
    } else {
      message.warning("Select a bundle folder or a .zip file first");
      return;
    }
    const hundredMB = 100 * 1024 * 1024;
    if (blob.size > hundredMB) {
      message.error(`Upload exceeds 100MB limit (${(blob.size / (1024 * 1024)).toFixed(1)} MB)`);
      return;
    }
    setValidateModalOpen(true);
    setValidateLog([]);
    setValidateLoading(true);
    const headers = getAuthHeaders();
    try {
      const formData = new FormData();
      formData.append("bundle", blob, "bundle.zip");
      const response = await fetch("/api/v1/deploy/validate-upload", {
        method: "POST",
        headers,
        body: formData,
      });
      if (!response.ok || !response.body) {
        setValidateLog((prev) => [...prev, `Error: HTTP ${response.status}`]);
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const logLines: string[] = [];
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() ?? "";
        let eventType = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventType = line.slice(7).trim();
          if (line.startsWith("data: ")) {
            const data = line.slice(6).trim();
            try {
              const obj = JSON.parse(data);
              logLines.push(`[${eventType}] ${JSON.stringify(obj)}`);
            } catch {
              logLines.push(`[${eventType}] ${data}`);
            }
          }
        }
      }
      if (buffer.trim()) logLines.push(buffer);
      if (logLines.length === 0) {
        logLines.push("No events received. The server may have closed the stream (e.g. validation failed).");
      }
      setValidateLog(logLines);
    } catch (e) {
      setValidateLog((prev) => [...prev, `Error: ${e instanceof Error ? e.message : String(e)}`]);
    } finally {
      setValidateLoading(false);
    }
  };

  const formatDeployedAt = (raw: string | number | undefined) => {
    if (raw == null || raw === "") return "—";
    const t = typeof raw === "string" ? parseFloat(raw) : raw;
    if (Number.isNaN(t)) return "—";
    return new Date(t * 1000).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

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

  const targetingSummary = (t: BundleTargeting | undefined) => {
    if (!t) return "Global";
    const parts: string[] = [];
    if (t.worker_ids?.length) parts.push(`${t.worker_ids.length} worker(s)`);
    if (t.worker_tags?.length) parts.push(`tags: ${t.worker_tags.join(", ")}`);
    if (t.motet_ids?.length) parts.push(`${t.motet_ids.length} motet(s)`);
    if (t.tenant_ids?.length) parts.push(`${t.tenant_ids.length} tenant(s)`);
    return parts.length ? parts.join(" · ") : "Global";
  };

  /**
   * Compact image-ref display: keep the short repo tail + a 12-char digest prefix.
   * Pin by digest: the catalog SHOULD carry @sha256:... refs; the
   * full 64-char digest is too long for a table cell so we truncate but keep
   * enough to be unambiguous and round-trippable via copy.
   */
  const formatImageRef = (ref: string | undefined): string => {
    if (!ref) return "—";
    const at = ref.lastIndexOf("@");
    if (at === -1) return ref;
    const left = ref.slice(0, at);
    const digest = ref.slice(at + 1);
    const repoTail = left.split("/").slice(-2).join("/") || left;
    const shortDigest = digest.length > 19 ? `${digest.slice(0, 19)}…` : digest;
    return `${repoTail}@${shortDigest}`;
  };

  /**
   * Surface for "is this bundle pinned by digest?" — mutable tags (no @sha256:)
   * are a deploy-time convenience but MUST NOT be the prod runtime ref for
   * pinned production bundles.
   */
  const isPinnedByDigest = (ref: string | undefined): boolean =>
    !!ref && ref.includes("@sha256:");

  const statusTag = (status: string | undefined) => {
    if (!status) return "—";
    const map: Record<string, { color: string }> = {
      complete: { color: "green" },
      no_change: { color: "default" },
      degraded: { color: "orange" },
      failed: { color: "red" },
      publishing: { color: "blue" },
      propagating: { color: "blue" },
    };
    const c = map[status] ?? { color: "default" };
    return <Tag color={c.color}>{status}</Tag>;
  };

  const renderBundleDetails = (r: DeployedBundle) => (
    <div style={{ padding: "8px 16px", background: "rgba(0,0,0,0.02)", borderRadius: 4 }}>
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="name">{r.bundle_id}</Descriptions.Item>
        <Descriptions.Item label="version (manifest)">{r.manifest_version || "—"}</Descriptions.Item>
        <Descriptions.Item label="bundle_version (tree SHA)">
          <Text code style={{ fontSize: 11 }}>{r.catalog?.bundle_version ?? r.bundle_version ?? "—"}</Text>
        </Descriptions.Item>
        <Descriptions.Item label="bundle_ref (commit)">{r.bundle_ref ? <Text code>{String(r.bundle_ref).slice(0, 12)}</Text> : "—"}</Descriptions.Item>
        <Descriptions.Item label="status">{statusTag(r.status)}</Descriptions.Item>
        <Descriptions.Item label="deployed_at">{formatDeployedAt(r.deployed_at)}</Descriptions.Item>
        <Descriptions.Item label="deploy_job_id">{r.deploy_job_id ? <Text code style={{ fontSize: 11 }}>{r.deploy_job_id}</Text> : "—"}</Descriptions.Item>
        <Descriptions.Item label="targeting">
          {r.targeting ? (
            <pre style={{ margin: 0, fontSize: 11, whiteSpace: "pre-wrap" }}>
              {JSON.stringify(r.targeting, null, 2)}
            </pre>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="catalog.commands">
          {r.catalog?.commands?.length ? (
            <Space wrap size={[4, 4]}>
              {r.catalog.commands.map((c) => (
                <Tag key={c} style={{ fontSize: 10 }}>{c}</Tag>
              ))}
            </Space>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="catalog.tools">
          {r.catalog?.tools?.length ? (
            <Space wrap size={[4, 4]}>
              {r.catalog.tools.map((t) => (
                <Tag key={t} style={{ fontSize: 10 }} color="green">{t}</Tag>
              ))}
            </Space>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="catalog.workflows">
          {r.catalog?.workflows?.length ? (
            <Space wrap size={[4, 4]}>
              {r.catalog.workflows.map((w) => (
                <Tag key={w} style={{ fontSize: 10 }} color="purple">{w}</Tag>
              ))}
            </Space>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="catalog.agents">
          {r.catalog?.agents?.length ? (
            <Space wrap size={[4, 4]}>
              {r.catalog.agents.map((a) => (
                <Tag key={a} style={{ fontSize: 10 }} color="geekblue">{a}</Tag>
              ))}
            </Space>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="catalog.mcp_servers">
          {r.catalog?.mcp_servers?.length ? (
            <Space wrap size={[4, 4]}>
              {r.catalog.mcp_servers.map((s) => (
                <Tag key={s} style={{ fontSize: 10 }} color="cyan">{s}</Tag>
              ))}
            </Space>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="catalog.model_ids">
          {r.catalog?.model_ids?.length ? (
            <Space wrap size={[4, 4]}>
              {r.catalog.model_ids.map((m) => (
                <Tag key={m} style={{ fontSize: 10 }}>{m}</Tag>
              ))}
            </Space>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="catalog.skills">
          {r.catalog?.skills?.length ? (
            <Space wrap size={[4, 4]}>
              {r.catalog.skills.map((s, idx) => (
                <Tag
                  key={s.id ?? s.name ?? `skill-${idx}`}
                  style={{ fontSize: 10 }}
                  color="magenta"
                  title={s.description ?? undefined}
                >
                  {s.id ?? s.name ?? "skill"}
                </Tag>
              ))}
            </Space>
          ) : (
            "—"
          )}
        </Descriptions.Item>
        <Descriptions.Item label="Execution image">
          {r.catalog?.exec && Object.keys(r.catalog.exec).length > 0 ? (
            <Descriptions size="small" column={1} bordered={false} colon style={{ marginBottom: 0 }}>
              <Descriptions.Item label="oci_image_ref">
                {r.catalog.exec.oci_image_ref ? (
                  <Space size={6} wrap>
                    <Text code copyable style={{ fontSize: 11 }}>
                      {r.catalog.exec.oci_image_ref}
                    </Text>
                    {isPinnedByDigest(r.catalog.exec.oci_image_ref) ? (
                      <Tag color="green" style={{ fontSize: 10 }}>pinned by digest</Tag>
                    ) : (
                      <Tag
                        color="orange"
                        style={{ fontSize: 10 }}
                        title="Production image refs should be pinned by digest (image@sha256:...) so the same bytes are pulled every time."
                      >
                        mutable tag
                      </Tag>
                    )}
                  </Space>
                ) : (
                  "—"
                )}
              </Descriptions.Item>
              <Descriptions.Item label="base_image_stack">
                {r.catalog.exec.base_image_stack ? (
                  <Tag color="blue" style={{ fontSize: 10 }}>{r.catalog.exec.base_image_stack}</Tag>
                ) : (
                  "—"
                )}
              </Descriptions.Item>
              <Descriptions.Item label="requirements_path">
                {r.catalog.exec.requirements_path ? (
                  <Text code style={{ fontSize: 11 }}>{r.catalog.exec.requirements_path}</Text>
                ) : (
                  "—"
                )}
              </Descriptions.Item>
              <Descriptions.Item label="requirements_sha256">
                {r.catalog.exec.requirements_sha256 ? (
                  <Text code copyable={{ text: r.catalog.exec.requirements_sha256 }} style={{ fontSize: 11 }}>
                    {`${r.catalog.exec.requirements_sha256.slice(0, 12)}…`}
                  </Text>
                ) : (
                  "—"
                )}
              </Descriptions.Item>
              <Descriptions.Item label="exec_artifact_digest">
                {r.catalog.exec.exec_artifact_digest ? (
                  <Text code copyable={{ text: r.catalog.exec.exec_artifact_digest }} style={{ fontSize: 11 }}>
                    {`${r.catalog.exec.exec_artifact_digest.slice(0, 19)}…`}
                  </Text>
                ) : (
                  "—"
                )}
              </Descriptions.Item>
            </Descriptions>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              No config/exec.yaml — bundle has no pinned execution image.
            </Text>
          )}
        </Descriptions.Item>
        <Descriptions.Item label="worker_state (loaded on)">
          {r.worker_state && typeof r.worker_state === "object" && Object.keys(r.worker_state).length > 0 ? (
            <Space wrap size={[4, 4]}>
              {Object.keys(r.worker_state).map((wid) => (
                <Tag key={wid}>{wid}</Tag>
              ))}
            </Space>
          ) : (
            "—"
          )}
        </Descriptions.Item>
      </Descriptions>
    </div>
  );

  const columns = [
    {
      title: "Bundle ID",
      dataIndex: "bundle_id",
      key: "bundle_id",
      render: (id: string) => <Text code strong>{id}</Text>,
    },
    {
      title: "Version",
      key: "bundle_version",
      render: (_: unknown, r: DeployedBundle) => (
        <span title={r.catalog?.bundle_version ?? r.bundle_version ?? undefined}>
          {(r.catalog?.bundle_version ?? r.bundle_version ?? "—").slice(0, 12)}
          {((r.catalog?.bundle_version ?? r.bundle_version)?.length ?? 0) > 12 ? "…" : ""}
        </span>
      ),
    },
    {
      title: "Ref",
      key: "bundle_ref",
      render: (_: unknown, r: DeployedBundle) =>
        r.bundle_ref ? (
          <Text type="secondary" style={{ fontSize: 11 }} copyable>
            {String(r.bundle_ref).slice(0, 7)}
          </Text>
        ) : (
          "—"
        ),
    },
    {
      title: "Status",
      key: "status",
      render: (_: unknown, r: DeployedBundle) => statusTag(r.status),
    },
    {
      title: "Commands",
      key: "commands",
      render: (_: unknown, r: DeployedBundle) => {
        const n = r.catalog?.commands?.length ?? 0;
        return n ? <Tag color="blue">{n}</Tag> : "—";
      },
    },
    {
      title: "Tools",
      key: "tools",
      render: (_: unknown, r: DeployedBundle) => {
        const n = r.catalog?.tools?.length ?? 0;
        return n ? <Tag color="green">{n}</Tag> : "—";
      },
    },
    {
      title: "Workflows",
      key: "workflows",
      render: (_: unknown, r: DeployedBundle) => {
        const n = r.catalog?.workflows?.length ?? 0;
        return n ? <Tag color="purple">{n}</Tag> : "—";
      },
    },
    {
      title: "Agents",
      key: "agents",
      render: (_: unknown, r: DeployedBundle) => {
        const n = r.catalog?.agents?.length ?? 0;
        return n ? <Tag color="geekblue">{n}</Tag> : "—";
      },
    },
    {
      title: "MCP",
      key: "mcp",
      render: (_: unknown, r: DeployedBundle) => {
        const n = r.catalog?.mcp_servers?.length ?? 0;
        return n ? <Tag color="cyan">{n}</Tag> : "—";
      },
    },
    {
      title: "Skills",
      key: "skills",
      render: (_: unknown, r: DeployedBundle) => {
        const n = r.catalog?.skills?.length ?? 0;
        return n ? <Tag color="magenta">{n}</Tag> : "—";
      },
    },
    {
      title: "Execution image",
      key: "exec_image",
      render: (_: unknown, r: DeployedBundle) => {
        const ref = r.catalog?.exec?.oci_image_ref;
        const stack = r.catalog?.exec?.base_image_stack;
        if (!ref && !stack) {
          return <Text type="secondary" style={{ fontSize: 11 }}>—</Text>;
        }
        return (
          <Space size={4} orientation="vertical">
            {ref ? (
              <Text
                style={{ fontSize: 11 }}
                title={ref}
                type={isPinnedByDigest(ref) ? undefined : "warning"}
              >
                {formatImageRef(ref)}
              </Text>
            ) : null}
            {stack ? (
              <Tag color="blue" style={{ fontSize: 10 }}>{stack}</Tag>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: "Targeting",
      key: "targeting",
      ellipsis: true,
      render: (_: unknown, r: DeployedBundle) => (
        <Text type="secondary" style={{ fontSize: 12 }} title={targetingSummary(r.targeting)}>
          {targetingSummary(r.targeting)}
        </Text>
      ),
    },
    {
      title: "Workers",
      key: "workers",
      render: (_: unknown, r: DeployedBundle) => {
        const n = r.worker_state && typeof r.worker_state === "object" ? Object.keys(r.worker_state).length : 0;
        return n ? <Tag>{n}</Tag> : "—";
      },
    },
    {
      title: "Deployed",
      key: "deployed_at",
      render: (_: unknown, r: DeployedBundle) => formatDeployedAt(r.deployed_at),
    },
    {
      title: "Actions",
      key: "actions",
      width: 160,
      fixed: "right" as const,
      render: (_: unknown, r: DeployedBundle) => (
        <Space size="small">
          <Popconfirm
            title="Undeploy bundle"
            description={`Remove bundle "${r.bundle_id}" from all workers?`}
            onConfirm={() => handleUndeploy(r.bundle_id)}
            okText="Undeploy"
            okButtonProps={{ danger: true }}
            cancelText="Cancel"
          >
            <Button type="link" danger size="small" icon={<DeleteOutlined />}>
              Undeploy
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const commonFormItems = (
    <>
      <Form.Item name="repo_url" label="Repo URL" rules={[{ required: true }]}>
        <Input placeholder="https://github.com/org/repo" />
      </Form.Item>
      <Form.Item name="branch" label="Branch / ref" rules={[{ required: true }]} initialValue="main">
        <Input placeholder="main" />
      </Form.Item>
      <Form.Item name="path" label="Path in repo" rules={[{ required: true }]}>
        <Input placeholder="extensions/my-bundle" />
      </Form.Item>
      <Form.Item name="repo_creds_path" label="Vault path (optional)">
        <Input placeholder="vault://deploy/github-token" />
      </Form.Item>
    </>
  );

  return (
    <div>
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <Title level={2}>
            <CloudUploadOutlined style={{ marginRight: 12 }} />
            Bundles
          </Title>
          <Text type="secondary">Deploy and manage Motet bundles (commands, tools, workflows, agents, mcp tools, etc…).</Text>
        </div>
        {dataUpdatedAt && (
          <Text type="secondary" style={{ fontSize: 12, marginTop: 8 }}>
            Updated: {formatLastUpdated(dataUpdatedAt)}
          </Text>
        )}
      </div>

      <Tabs
        style={{ marginTop: 16 }}
        items={[
          {
            key: "list",
            label: "Deployed bundles",
            children: (
              <>
                {error && (
                  <Alert
                    type="error"
                    title="Failed to load bundles"
                    description={error instanceof Error ? error.message : String(error)}
                    style={{ marginBottom: 16 }}
                  />
                )}
                <Card>
                  <Table
                    rowKey="bundle_id"
                    columns={columns}
                    dataSource={bundles}
                    loading={isLoading}
                    pagination={false}
                    scroll={{ x: "max-content" }}
                    locale={{ emptyText: "No bundles deployed" }}
                    expandable={{
                      expandedRowRender: (record) => renderBundleDetails(record),
                      expandIcon: ({ expanded, onExpand, record }) =>
                        expanded ? (
                          <DownOutlined style={{ marginRight: 8 }} onClick={(e) => onExpand(record, e)} />
                        ) : (
                          <RightOutlined style={{ marginRight: 8 }} onClick={(e) => onExpand(record, e)} />
                        ),
                    }}
                  />
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary">Total: {total} bundle(s)</Text>
                  </div>
                </Card>
              </>
            ),
          },
          {
            key: "deploy",
            label: "Deploy (git)",
            children: (
              <Card title="Deploy a bundle from git" extra={<CloudUploadOutlined />}>
                <Form form={deployForm} layout="vertical" onFinish={handleDeploy}>
                  {commonFormItems}
                  <Form.Item name="interactive" label="Interactive (deployer persona)" valuePropName="checked" initialValue={false}>
                    <Switch />
                  </Form.Item>
                  <Form.Item>
                    <Space>
                      <Button type="primary" htmlType="submit" loading={deployLoading} icon={<CloudUploadOutlined />}>
                        Deploy
                      </Button>
                      <Button
                        type="default"
                        onClick={handleValidateOnly}
                        loading={validateLoading}
                        icon={<ExperimentOutlined />}
                      >
                        Validate
                      </Button>
                      <Text type="secondary">Validate runs lint only (no deploy).</Text>
                    </Space>
                  </Form.Item>
                </Form>
              </Card>
            ),
          },
          {
            key: "upload",
            label: "Upload",
            children: (
              <Card title="Upload bundle" extra={<UploadOutlined />}>
                <Space orientation="vertical" style={{ width: "100%" }} size="middle">
                  <Text type="secondary">
                    Select the bundle root directory (containing manifest.yaml, commands/, tools/, etc.) and the UI will zip and upload it.
                    Or upload an existing .zip. No git required.
                  </Text>
                  <Space wrap align="end" size="middle">
                    <div>
                      <Text strong style={{ display: "block", marginBottom: 4 }}>Bundle folder</Text>
                      <input
                        ref={folderInputRef}
                        type="file"
                        multiple
                        onChange={(e) => {
                          const list = e.target.files;
                          if (list) setUploadFolderFiles(Array.from(list));
                          setUploadFile(null);
                        }}
                        style={{ display: "none" }}
                        {...({
                          webkitdirectory: "",
                          directory: "true",
                        } as unknown as React.InputHTMLAttributes<HTMLInputElement>)}
                      />
                      <Button type="default" onClick={() => folderInputRef.current?.click()}>
                        Choose folder
                      </Button>
                    </div>
                    <div>
                      <Text strong style={{ display: "block", marginBottom: 4 }}>Or .zip file</Text>
                      <input
                        ref={zipInputRef}
                        type="file"
                        accept=".zip"
                        onChange={(e) => {
                          setUploadFile(e.target.files?.[0] ?? null);
                          setUploadFolderFiles([]);
                        }}
                        style={{ display: "none" }}
                      />
                      <Button type="default" onClick={() => zipInputRef.current?.click()}>
                        Choose .zip file
                      </Button>
                    </div>
                    <div>
                      <Text strong style={{ display: "block", marginBottom: 4 }}>
                        {uploadFolderFiles.length > 0
                          ? `Folder: ${(uploadFolderFiles[0] as File & { webkitRelativePath?: string }).webkitRelativePath?.split("/")[0] ?? "—"} (${uploadFolderFiles.length} files)`
                          : uploadFile
                            ? `Selected: ${uploadFile.name} (${(uploadFile.size / 1024).toFixed(1)} KB)`
                            : "\u00A0"}
                      </Text>
                      <Space>
                        <Button
                          type="primary"
                          onClick={handleUpload}
                          loading={uploadLoading}
                          disabled={!uploadFile && uploadFolderFiles.length === 0}
                          icon={<UploadOutlined />}
                        >
                          Deploy
                        </Button>
                        <Button
                          type="default"
                          onClick={handleValidateUpload}
                          loading={validateLoading}
                          disabled={!uploadFile && uploadFolderFiles.length === 0}
                          icon={<ExperimentOutlined />}
                        >
                          Validate
                        </Button>
                      </Space>
                    </div>
                  </Space>
                  <Text type="secondary">Validate runs lint only (no deploy).</Text>
                </Space>
              </Card>
            ),
          },
          {
            key: "image-stacks",
            label: "Image stacks",
            children: (
              <>
                {stacksError && (
                  <Alert
                    type="error"
                    title="Failed to load image stacks"
                    description={stacksError instanceof Error ? stacksError.message : String(stacksError)}
                    style={{ marginBottom: 16 }}
                  />
                )}
                <Card
                  title="Platform image stacks"
                  extra={<Text type="secondary" style={{ fontSize: 12 }}>Read-only — set via MOTET_IMAGE_STACK_*</Text>}
                >
                  <Alert
                    type="info"
                    showIcon
                    title="What is an image stack?"
                    description={
                      <span>
                        Motet&apos;s term for the curated base image layer a bundle&apos;s exec image is built on top of
                        (analogous to a Cloud Native Buildpacks &quot;stack&quot;). A bundle picks one via{" "}
                        <Text code>config/exec.yaml</Text> <Text code>base_image_stack</Text>. Operators register new
                        stacks (or override builtins) by setting{" "}
                        <Text code>MOTET_IMAGE_STACK_&lt;NAME&gt;=image@sha256:...</Text> on the API and deployer
                        workers. Stacks shown here as <Tag color="orange">unpinned</Tag> are recognized names but have
                        no resolvable image yet — bundles targeting them will fall back to the in-repo Dockerfile
                        default.
                      </span>
                    }
                    style={{ marginBottom: 12 }}
                  />
                  <Table
                    rowKey="name"
                    loading={stacksLoading}
                    dataSource={imageStacks}
                    pagination={false}
                    locale={{ emptyText: "No image stacks registered" }}
                    columns={[
                      {
                        title: "Name",
                        dataIndex: "name",
                        key: "name",
                        render: (n: string) => <Text code>{n}</Text>,
                      },
                      {
                        title: "Source",
                        key: "source",
                        render: (_: unknown, r: ImageStack) =>
                          r.builtin ? (
                            <Tag color="blue">builtin</Tag>
                          ) : (
                            <Tag color="purple">env</Tag>
                          ),
                      },
                      {
                        title: "Status",
                        key: "status",
                        render: (_: unknown, r: ImageStack) =>
                          r.is_pinned ? (
                            <Tag color="green">pinned</Tag>
                          ) : (
                            <Tag color="orange">unpinned</Tag>
                          ),
                      },
                      {
                        title: "Image",
                        dataIndex: "oci_image_ref",
                        key: "oci_image_ref",
                        render: (ref: string) =>
                          ref ? (
                            <Text
                              code
                              copyable={{ text: ref }}
                              style={{ fontSize: 11 }}
                              type={ref.includes("@sha256:") ? undefined : "warning"}
                              title={ref.includes("@sha256:") ? "digest-pinned" : "mutable tag — recommend digest pin"}
                            >
                              {ref}
                            </Text>
                          ) : (
                            <Text type="secondary" style={{ fontSize: 11 }}>—</Text>
                          ),
                      },
                      {
                        title: "Description",
                        dataIndex: "description",
                        key: "description",
                        render: (d: string) =>
                          d ? <Text style={{ fontSize: 12 }}>{d}</Text> : <Text type="secondary">—</Text>,
                      },
                    ]}
                  />
                  <div style={{ marginTop: 8 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      Total: {imageStacks.length} stack(s)
                    </Text>
                  </div>
                </Card>
              </>
            ),
          },
          {
            key: "hot",
            label: "Hot Load",
            children: (
              <Card title="Hot deploy (local development)">
                <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
                  <Alert
                    type="info"
                    showIcon
                    title="Use hot deploy for fast local iteration"
                    description="Hot deploy is dev-only and optimized for quick command/tool/workflow edits. Use Deploy/Upload tabs for artifact-backed, production-like deployment."
                  />

                  <div>
                    <Text strong>Recommended stack commands</Text>
                    <pre
                      style={{
                        marginTop: 8,
                        padding: 12,
                        background: "rgba(0,0,0,0.03)",
                        borderRadius: 6,
                        fontSize: 12,
                        overflowX: "auto",
                      }}
                    >
{`motet-cli local up
motet-cli local status
motet-cli local doctor
motet-cli local manage`}
                    </pre>
                  </div>

                  <div>
                    <Text strong>Hot deploy (Mutagen sync)</Text>
                    <pre
                      style={{
                        marginTop: 8,
                        padding: 12,
                        background: "rgba(0,0,0,0.03)",
                        borderRadius: 6,
                        fontSize: 12,
                        overflowX: "auto",
                      }}
                    >
{`# Hot deploy (Mutagen sync)
motet-cli bundle hot-deploy .

# Force fresh worker discovery when needed
motet-cli bundle hot-deploy . --disable-discovered-container-caching`}
                    </pre>
                  </div>

                  <div>
                    <Text strong>Common troubleshooting</Text>
                    <pre
                      style={{
                        marginTop: 8,
                        padding: 12,
                        background: "rgba(0,0,0,0.03)",
                        borderRadius: 6,
                        fontSize: 12,
                        overflowX: "auto",
                      }}
                    >
{`No workers passed filtering (CapabilityFilter):
  - Restart stack and recheck readiness:
    motet-cli local restart
    motet-cli workers readiness

Hot deploy bundle path does not exist:
  - Retry with:
    motet-cli bundle hot-deploy . --disable-discovered-container-caching

Mutagen not found:
  - Install mutagen and run:
    motet-cli bundle hot-deploy .`}
                    </pre>
                  </div>
                </Space>
              </Card>
            ),
          },
        ]}
      />

      <Modal
        title="Validate Bundle"
        open={validateModalOpen}
        onCancel={() => setValidateModalOpen(false)}
        footer={[
          <Button key="close" onClick={() => setValidateModalOpen(false)}>
            Close
          </Button>,
        ]}
        width={880}
        destroyOnHidden
      >
        <div
          style={{
            maxHeight: 560,
            overflow: "auto",
            fontFamily: "monospace",
            fontSize: 12,
            background: "#fafafa",
            color: "rgba(0, 0, 0, 0.88)",
            padding: 12,
            borderRadius: 4,
          }}
        >
          {validateLoading && <div>Validating…</div>}
          {validateLog.map((line, i) => (
            <div key={i} style={{ marginBottom: 4 }}>
              {line}
            </div>
          ))}
          {!validateLoading && validateLog.length === 0 && (
            <Text type="secondary">No events yet. Click Validate on the Deploy (git) or Upload tab to run lint.</Text>
          )}
        </div>
      </Modal>
    </div>
  );
}
