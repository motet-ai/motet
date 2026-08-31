/**
 * Motet - Chat Explorer - Chat Header
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-31
 *
 * Description:
 *     Top application header bar providing authentication controls and settings.
 *
 *     Features:
 *     - SSO Login button (when not authenticated)
 *     - User menu (JWT): Person icon + display name; dropdown with Motet Settings, Auth Settings, Logout.
 *     - Service Account / API Key menu: Motet Settings, Auth Settings, and Logout (clears SA/API key).
 *     - Unauthenticated: Motet Settings + Auth Settings icon buttons.
 *     - Theme: Moon/sun icon button for one-click dark/light toggle (same pattern as manage app).
 *     - Agent and surface Selects in the header (surface list filtered by agent allow-list).
 *     - Settings popup: Watch events, Show errors, cost display toggles.
 *
 * Dependencies:
 *     - Ant Design: Button, Dropdown, Flex, Layout, Modal, Select, Space, Switch, Tooltip, Typography
 *     - @ant-design/icons: LogoutOutlined, MoonOutlined, SettingOutlined, SlidersOutlined, SunOutlined, UserOutlined
 *     - ../types: AuthState, CostDisplayPrefs
 *
 * Usage:
 *     <ChatHeader
 *       auth={auth}
 *       userDisplayName={userDisplayName}
 *       onSsoLogin={handleSsoLogin}
 *       // ... other props
 *     />
 *
 * Notes:
 *     - Chat model, thinking, and reasoning effort are chosen below the composer.
 */
import React, { useMemo, useState } from "react";
import { Button, Dropdown, Flex, Layout, Modal, Select, Space, Switch, Tooltip, Typography } from "antd";
import { LogoutOutlined, MoonOutlined, SettingOutlined, SlidersOutlined, SunOutlined, UserOutlined } from "@ant-design/icons";
import { type AuthState, type CostDisplayPrefs } from "../types";
import { COST_DISPLAY_TOGGLES } from "../utils/costDisplay";

const { Header } = Layout;
const { Text } = Typography;

type AgentInfo = {
  qualified_id: string;
  display_name?: string;
  allowed_surface_ids?: string[] | null;
  selectable?: boolean;
};

type SurfaceInfo = {
  id: string;
  display_name?: string;
};

/**
 * Props for the ChatHeader component.
 */
interface ChatHeaderProps {
  /** Ant Design theme token for consistent styling */
  token: any;
  /** Current authentication state */
  auth: AuthState;
  /** User's display name from identity endpoint (null if not fetched) */
  userDisplayName: string | null;
  /** Callback to initiate SSO login popup */
  onSsoLogin: () => void;
  /** Callback to log out (clears JWT / IdP session) */
  onLogout: () => void;
  /** Callback to clear service account token and/or API key */
  onClearNonJwtAuth: () => void;
  /** Callback to open auth settings modal */
  onOpenAuthModal: () => void;
  /** Whether dark mode is enabled */
  darkMode: boolean;
  /** Callback to toggle dark mode */
  setDarkMode: (checked: boolean) => void;
  /** Whether event bus watching is enabled */
  watchEvents: boolean;
  /** Callback to toggle event watching */
  setWatchEvents: (checked: boolean) => void;
  /** Whether error panel is visible */
  showErrors: boolean;
  /** Callback to toggle error visibility */
  setShowErrors: (checked: boolean) => void;
  /** Which priced cost lines to show */
  costDisplay: CostDisplayPrefs;
  /** Replace cost-display prefs */
  onCostDisplayChange: (next: CostDisplayPrefs) => void;

  /** Available agents from `/api/v1/agents` */
  availableAgents: AgentInfo[];
  /** Selected qualified agent ID (empty = backend default) */
  selectedAgentId: string;
  /** Callback when user selects an agent */
  onSelectAgentId: (agentId: string) => void;
  /** Surfaces catalog from `/api/v1/surfaces` */
  availableSurfaces: SurfaceInfo[];
  /** Selected surface/channel id for conversation scope */
  selectedSurfaceId: string;
  /** Callback when user selects a surface */
  onSelectSurfaceId: (surfaceId: string) => void;
}

function SettingsSwitch({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <Flex justify="space-between" align="center">
      <Text>{label}</Text>
      <Switch checked={checked} onChange={onChange} />
    </Flex>
  );
}

export function ChatHeader({
  token,
  auth,
  userDisplayName,
  onSsoLogin,
  onLogout,
  onClearNonJwtAuth,
  onOpenAuthModal,
  darkMode,
  setDarkMode,
  watchEvents,
  setWatchEvents,
  showErrors,
  setShowErrors,
  costDisplay,
  onCostDisplayChange,
  availableAgents,
  selectedAgentId,
  onSelectAgentId,
  availableSurfaces,
  selectedSurfaceId,
  onSelectSurfaceId,
}: ChatHeaderProps) {
  const agentOptions = useMemo(() => {
    const current = String(selectedAgentId || "").trim();
    const options = (availableAgents || [])
      .filter((a) => a.selectable !== false || a.qualified_id === current)
      .map((a) => ({
        value: String(a.qualified_id || ""),
        label: a.display_name
          ? `${a.display_name} (${a.qualified_id})`
          : String(a.qualified_id || ""),
      }));
    if (current && !options.some((o) => o.value === current)) {
      options.unshift({ value: current, label: `${current} (current)` });
    }
    return options;
  }, [availableAgents, selectedAgentId]);

  const surfaceOptions = useMemo(() => {
    const agentId = String(selectedAgentId || "").trim() || "core.default";
    const agent =
      (availableAgents || []).find((a) => a.qualified_id === agentId) ||
      (availableAgents || []).find((a) => a.qualified_id === selectedAgentId);
    const allowed = agent?.allowed_surface_ids;
    const catalog = availableSurfaces || [];
    const filtered =
      allowed == null || allowed.length === 0
        ? catalog
        : catalog.filter((s) => allowed.includes(s.id));
    const options = filtered.map((s) => {
      const label = String(s.display_name || "").trim();
      return {
        value: s.id,
        label: label && label !== s.id ? `${label} (${s.id})` : s.id,
      };
    });
    // Keep current selection visible even if temporarily outside allow-list.
    const current = String(selectedSurfaceId || "").trim() || "demo_chat";
    if (current && !options.some((o) => o.value === current)) {
      options.unshift({ value: current, label: `${current} (current)` });
    }
    if (options.length === 0) {
      options.push({ value: "demo_chat", label: "demo_chat" });
    }
    return options;
  }, [availableAgents, availableSurfaces, selectedAgentId, selectedSurfaceId]);

  const [settingsOpen, setSettingsOpen] = useState(false);

  const hasJwt = !!(auth.jwt && auth.jwt.trim());
  const hasServiceAccount = !!(auth.serviceAccountToken && auth.serviceAccountToken.trim());
  const hasApiKey = !!(auth.apiKey && auth.apiKey.trim());
  const hasNonJwtAuth = hasServiceAccount || hasApiKey;

  const settingsMenuItems = [
    {
      key: "settings",
      icon: <SlidersOutlined />,
      label: "Motet Settings",
      onClick: () => setSettingsOpen(true),
    },
    {
      key: "auth",
      icon: <SettingOutlined />,
      label: "Auth Settings",
      onClick: onOpenAuthModal,
    },
  ];

  const userMenuItems = [
    ...settingsMenuItems,
    { type: "divider" as const },
    {
      key: "logout",
      icon: <LogoutOutlined />,
      label: "Logout",
      danger: true,
      onClick: onLogout,
    },
  ];

  const nonJwtMenuItems = [
    ...settingsMenuItems,
    { type: "divider" as const },
    {
      key: "logout",
      icon: <LogoutOutlined />,
      label: "Logout",
      danger: true,
      onClick: onClearNonJwtAuth,
    },
  ];

  const nonJwtAuthLabel = hasServiceAccount ? "Service Account" : "API Key";

  return (
    <Header
      className="app-header"
      style={{
        background: token.colorBgContainer,
        borderBottom: `1px solid ${token.colorBorder}`,
        padding: "0 24px",
        height: 56,
        lineHeight: "56px",
      }}
    >
      <Flex justify="space-between" align="center" style={{ height: "100%" }}>
        <img
          src={`${import.meta.env.BASE_URL}images/Motet Identity Row - ${darkMode ? "KO" : "Black"}.svg`}
          alt="Motet"
          style={{ height: 24 }}
        />
        <Space size="middle" align="center">
          <Flex align="center" gap={8} className="header-scope-selects" style={{ height: 32 }}>
            <Select
              size="small"
              className="header-scope-select header-agent-select muted-until-hover-select"
              aria-label="Agent"
              placeholder="Agent (default)"
              allowClear
              showSearch
              optionFilterProp="label"
              popupMatchSelectWidth={false}
              getPopupContainer={() => document.body}
              value={selectedAgentId || undefined}
              options={agentOptions}
              onChange={(v) => onSelectAgentId(String(v || ""))}
            />
            <Text type="secondary">Surface</Text>
            <Select
              size="small"
              className="header-scope-select header-surface-select muted-until-hover-select"
              aria-label="Surface"
              placeholder="Surface"
              showSearch
              optionFilterProp="label"
              popupMatchSelectWidth={false}
              getPopupContainer={() => document.body}
              value={String(selectedSurfaceId || "").trim() || "demo_chat"}
              options={surfaceOptions}
              onChange={(v) => onSelectSurfaceId(String(v || "demo_chat"))}
            />
          </Flex>
          <Tooltip title={darkMode ? "Switch to light mode" : "Switch to dark mode"}>
            <Button
              type="text"
              icon={darkMode ? <SunOutlined /> : <MoonOutlined />}
              onClick={() => setDarkMode(!darkMode)}
            />
          </Tooltip>
          {hasJwt && (
            <Dropdown menu={{ items: userMenuItems }} trigger={["click"]}>
              <Button type="text" icon={<UserOutlined />}>
                {userDisplayName || "User"}
              </Button>
            </Dropdown>
          )}
          {!hasJwt && hasNonJwtAuth && (
            <Dropdown menu={{ items: nonJwtMenuItems }} trigger={["click"]}>
              <Button type="text" icon={<UserOutlined />}>
                {nonJwtAuthLabel}
              </Button>
            </Dropdown>
          )}
          {!hasJwt && !hasNonJwtAuth && (
            <>
              <Tooltip title="Motet Settings">
                <Button type="text" icon={<SlidersOutlined />} onClick={() => setSettingsOpen(true)} />
              </Tooltip>
              <Tooltip title="Auth Settings">
                <Button type="text" icon={<SettingOutlined />} onClick={onOpenAuthModal} />
              </Tooltip>
            </>
          )}
          <Modal
            title="Motet Settings"
            open={settingsOpen}
            onCancel={() => setSettingsOpen(false)}
            footer={null}
            width={360}
            styles={{ body: { paddingTop: 8 } }}
          >
            <Space orientation="vertical" style={{ width: "100%" }} size="middle">
              <SettingsSwitch label="Watch events" checked={watchEvents} onChange={setWatchEvents} />
              <SettingsSwitch label="Show errors" checked={showErrors} onChange={setShowErrors} />
              {COST_DISPLAY_TOGGLES.map(({ key, label }) => (
                <SettingsSwitch
                  key={key}
                  label={label}
                  checked={costDisplay[key]}
                  onChange={(checked) => onCostDisplayChange({ ...costDisplay, [key]: checked })}
                />
              ))}
            </Space>
          </Modal>
        </Space>
      </Flex>
    </Header>
  );
}

