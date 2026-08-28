/**
 * Motet - Admin Dashboard - Left Sidebar Navigation
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Collapsible left sidebar using Ant Design Layout.Sider.
 *     Navigation is grouped into Runtime / Data / Platform / Docs sections.
 *     The Motet product version sits in the chrome row to the left of
 *     the collapse control.
 *
 * Notes:
 *     Light mode uses a stronger hairline on the sider edge so the nav
 *     boundary remains visible against the shared layout background.
 *     Version comes from GET /api/v1/developer-docs (same product version
 *     as the documentation rail). Hidden when the sider is collapsed;
 *     when expanded it sits in the chrome row left of the collapse button.
 */
import { Button, Layout, Menu, Tooltip } from "antd";
import type { MenuProps } from "antd";
import {
  MenuUnfoldOutlined,
  MenuFoldOutlined,
  ClusterOutlined,
  UnorderedListOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
  LockOutlined,
  ApartmentOutlined,
  FileOutlined,
  ApiOutlined,
  DollarOutlined,
  BookOutlined,
  CloudUploadOutlined,
  ContainerOutlined,
  RobotOutlined,
  ThunderboltOutlined,
  ToolOutlined,
  GlobalOutlined,
  AppstoreOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { ADMIN_ROLES, hasAnyRole } from "@motet/ui-common";
import { useTheme } from "../context/ThemeContext";

async function fetchMotetVersion(): Promise<string> {
  const response = await fetch("/api/v1/developer-docs");
  if (!response.ok) throw new Error(response.statusText);
  const data = await response.json();
  return typeof data.version === "string" ? data.version : "";
}

const { Sider } = Layout;

const menuItems: MenuProps["items"] = [
  {
    type: "group",
    label: "Runtime",
    children: [
      { key: "/agents", icon: <RobotOutlined />, label: "Agents" },
      { key: "/workers", icon: <ClusterOutlined />, label: "Agent Workers" },
      { key: "/instance-managers", icon: <ApiOutlined />, label: "Instance Managers" },
      { key: "/mcp-servers", icon: <ApiOutlined />, label: "MCP Servers" },
      { key: "/workspace-containers", icon: <ContainerOutlined />, label: "Skill Workspaces" },
      { key: "/tasks", icon: <UnorderedListOutlined />, label: "Tasks" },
      { key: "/schedules", icon: <ClockCircleOutlined />, label: "Schedules" },
    ],
  },
  {
    type: "group",
    label: "Data",
    children: [
      { key: "/memory", icon: <DatabaseOutlined />, label: "Memory" },
      { key: "/vault", icon: <LockOutlined />, label: "Vault" },
      { key: "/artifacts", icon: <FileOutlined />, label: "Artifacts" },
    ],
  },
  {
    type: "group",
    label: "Platform",
    children: [
      { key: "/tenants", icon: <GlobalOutlined />, label: "Tenants" },
      { key: "/bundles", icon: <CloudUploadOutlined />, label: "Bundles" },
      { key: "/commands", icon: <ThunderboltOutlined />, label: "Commands" },
      { key: "/tools", icon: <ToolOutlined />, label: "Tools" },
      { key: "/skills", icon: <BookOutlined />, label: "Skills" },
      { key: "/surfaces", icon: <AppstoreOutlined />, label: "Surfaces" },
      { key: "/workflows", icon: <ApartmentOutlined />, label: "Workflows" },
      { key: "/models", icon: <ApiOutlined />, label: "Models" },
      { key: "/cost", icon: <DollarOutlined />, label: "Cost" },
    ],
  },
  {
    type: "group",
    label: "Docs",
    children: [
      { key: "/api-docs", icon: <ApiOutlined />, label: "API" },
      { key: "/developer-docs", icon: <BookOutlined />, label: "Documentation" },
    ],
  },
];

const ADMIN_ONLY_KEYS = new Set(["/vault", "/tenants"]);

function filterMenuItems(
  items: MenuProps["items"],
  isAdmin: boolean,
): MenuProps["items"] {
  if (isAdmin || !items) {
    return items;
  }
  return items
    .map((item) => {
      if (!item) {
        return item;
      }
      if ("children" in item && Array.isArray(item.children)) {
        const children = filterMenuItems(item.children, isAdmin);
        if (!children || children.length === 0) {
          return null;
        }
        return { ...item, children };
      }
      if ("key" in item && item.key && ADMIN_ONLY_KEYS.has(String(item.key))) {
        return null;
      }
      return item;
    })
    .filter((item): item is NonNullable<typeof item> => item != null);
}

interface LeftSidebarProps {
  token: any;
  collapsed: boolean;
  setCollapsed: (collapsed: boolean) => void;
  currentPath: string;
  onNavigate: (path: string) => void;
  userRoles?: string[];
  principalId?: string | null;
}

export function LeftSidebar({
  token,
  collapsed,
  setCollapsed,
  currentPath,
  onNavigate,
  userRoles = [],
  principalId = null,
}: LeftSidebarProps) {
  const darkMode = useTheme();
  const { data: version } = useQuery({
    queryKey: ["motet-version"],
    queryFn: fetchMotetVersion,
    staleTime: 5 * 60 * 1000,
  });
  const selectedKey =
    currentPath.startsWith("/developer-docs")
      ? "/developer-docs"
      : currentPath === "/task-flow"
        ? "/tasks"
        : currentPath;

  const collapseButton = (
    <Button
      type="text"
      className="sider-collapse-btn"
      icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
      onClick={() => setCollapsed(!collapsed)}
      aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
    />
  );

  return (
    <Sider
      className={`app-sider${darkMode ? " app-sider--dark" : " app-sider--light"}`}
      width={228}
      collapsedWidth={64}
      collapsible
      collapsed={collapsed}
      onCollapse={setCollapsed}
      trigger={null}
      style={{
        background: token.colorBgLayout,
      }}
    >
      <div className={`sider-chrome${collapsed ? " sider-chrome--collapsed" : ""}`}>
        {!collapsed && version ? (
          <span
            className="sider-version"
            title={`Motet ${version}`}
            style={{
              fontSize: 12,
              fontWeight: 500,
              fontVariantNumeric: "tabular-nums",
              lineHeight: 1.2,
              color: token.colorTextSecondary,
            }}
          >
            {`version ${version}`}
          </span>
        ) : null}
        {collapsed ? (
          <Tooltip title="Expand navigation" placement="right">
            {collapseButton}
          </Tooltip>
        ) : (
          collapseButton
        )}
      </div>

      <Menu
        mode="inline"
        selectedKeys={[selectedKey]}
        items={filterMenuItems(menuItems, hasAnyRole(userRoles, ADMIN_ROLES, principalId))}
        onClick={({ key }) => onNavigate(key)}
        inlineCollapsed={collapsed}
        className="app-sider-menu"
        style={{
          border: "none",
          background: "transparent",
        }}
      />
    </Sider>
  );
}
