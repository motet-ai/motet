/**
 * Motet - Admin Dashboard - Header Bar
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-17
 *
 * Description:
 *     Header using Ant Design Layout.Header with Motet mark, Administration
 *     label, scope selector, theme toggle, and user menu.
 */
import { Layout, Space, Button, Dropdown, Flex, Typography } from "antd";
import {
  UserOutlined,
  SettingOutlined,
  LogoutOutlined,
  SunOutlined,
  MoonOutlined,
} from "@ant-design/icons";
import { ScopeSelector } from "../components/ScopeSelector";
import type { Scope } from "../hooks/useScope";

const { Header } = Layout;
const { Text } = Typography;

interface HeaderBarProps {
  token: any;
  scope: Scope;
  onScopeChange: (scope: Partial<Scope>) => void;
  isAuthenticated: boolean;
  userDisplayName: string | null;
  isDarkMode: boolean;
  onToggleDarkMode: () => void;
  onOpenAuthModal: () => void;
  onLogout: () => void;
}

export function HeaderBar({
  token,
  scope,
  onScopeChange,
  isAuthenticated,
  userDisplayName,
  isDarkMode,
  onToggleDarkMode,
  onOpenAuthModal,
  onLogout,
}: HeaderBarProps) {
  const userMenuItems = [
    {
      key: "settings",
      icon: <SettingOutlined />,
      label: "Auth Settings",
      onClick: onOpenAuthModal,
    },
    { type: "divider" as const },
    {
      key: "logout",
      icon: <LogoutOutlined />,
      label: "Logout",
      danger: true,
      onClick: onLogout,
    },
  ];

  return (
    <Header
      className="app-header"
      style={{
        background: token.colorBgContainer,
        borderBottom: `1px solid ${token.colorBorderSecondary}`,
        boxShadow: isDarkMode
          ? "0 1px 0 rgba(255, 255, 255, 0.03)"
          : "0 1px 0 rgba(15, 18, 21, 0.04)",
        padding: "0 24px",
        height: 56,
        lineHeight: "56px",
      }}
    >
      <Flex justify="space-between" align="center" style={{ height: "100%" }}>
        <Space size="large" align="center">
          <Flex align="center" gap={12} className="app-brand">
            <img
              src={`${import.meta.env.BASE_URL}images/Motet Identity Row - ${isDarkMode ? "KO" : "Black"}.svg`}
              alt="Motet"
              style={{ height: 22, display: "block" }}
            />
            <Text
              className="app-brand-label"
              style={{
                color: token.colorTextSecondary,
                fontSize: 13,
                fontWeight: 500,
                letterSpacing: "0.04em",
                textTransform: "uppercase",
                lineHeight: 1,
                borderLeft: `1px solid ${token.colorBorder}`,
                paddingLeft: 12,
              }}
            >
              Administration
            </Text>
          </Flex>
          <ScopeSelector
            scope={scope}
            onChange={onScopeChange}
            enabled={isAuthenticated}
          />
        </Space>

        <Space size="middle">
          <Button
            type="text"
            icon={isDarkMode ? <SunOutlined /> : <MoonOutlined />}
            onClick={onToggleDarkMode}
            aria-label={isDarkMode ? "Switch to light mode" : "Switch to dark mode"}
          />
          <Dropdown menu={{ items: userMenuItems }} trigger={["click"]}>
            <Button type="text" icon={<UserOutlined />}>
              {userDisplayName || "User"}
            </Button>
          </Dropdown>
        </Space>
      </Flex>
    </Header>
  );
}
