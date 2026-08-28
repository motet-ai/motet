/**
 * Motet - Motet UI Common - Signed-Out Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Full-page signed-out landing used when the user is not authenticated.
 *     The app shell is not rendered, so session data is not left on screen.
 *
 * Dependencies:
 *     - Ant Design: Button, Card, Space, Typography, theme
 *     - @ant-design/icons: KeyOutlined, LoginOutlined, MoonOutlined, SunOutlined
 *
 * Usage:
 *     import { SignedOutPage } from "@motet/ui-common";
 *
 *     if (!isAuthenticated) {
 *       return (
 *         <SignedOutPage
 *           variant={signedOut ? "signed_out" : "welcome"}
 *           onSsoLogin={handleSsoLogin}
 *           onOpenAuthModal={() => setShowAuthModal(true)}
 *         />
 *       );
 *     }
 *
 * Notes:
 *     Render this instead of the authenticated shell. Keep AuthModal as a
 *     sibling so the API key / JWT path still works.
 */

import { Button, Card, Space, Typography, theme } from "antd";
import { KeyOutlined, LoginOutlined, MoonOutlined, SunOutlined } from "@ant-design/icons";

const { Title, Paragraph, Text } = Typography;
const { useToken } = theme;

export type SignedOutVariant = "welcome" | "signed_out";

export interface SignedOutPageProps {
  /** Open the SSO login popup */
  onSsoLogin: () => void;
  /** Open the manual API key / JWT modal */
  onOpenAuthModal: () => void;
  /** welcome = first visit; signed_out = after Logout */
  variant?: SignedOutVariant;
  /** App name shown in the description (e.g. Administration, Chat Explorer) */
  productLabel?: string;
  /** Motet wordmark image URL */
  logoSrc?: string;
  /** Current theme, for the optional toggle */
  isDarkMode?: boolean;
  /** Theme toggle; omit to hide the control */
  onToggleDarkMode?: () => void;
}

function copyFor(variant: SignedOutVariant, productLabel?: string): { title: string; description: string } {
  const product = productLabel || "Motet";
  if (variant === "signed_out") {
    return {
      title: "You've been signed out",
      description: `Sign in again to use ${product}.`,
    };
  }
  return {
    title: "Sign in to continue",
    description: `You need to be signed in to use ${product}.`,
  };
}

/**
 * Full-page signed-out / sign-in landing. Does not render a modal or mask.
 */
export function SignedOutPage({
  onSsoLogin,
  onOpenAuthModal,
  variant = "welcome",
  productLabel,
  logoSrc,
  isDarkMode,
  onToggleDarkMode,
}: SignedOutPageProps) {
  const { token } = useToken();
  const { title, description } = copyFor(variant, productLabel);

  return (
    <div
      style={{
        minHeight: "100vh",
        width: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        background: token.colorBgLayout,
        position: "relative",
      }}
    >
      {onToggleDarkMode ? (
        <Button
          type="text"
          aria-label={isDarkMode ? "Switch to light mode" : "Switch to dark mode"}
          icon={isDarkMode ? <SunOutlined /> : <MoonOutlined />}
          onClick={onToggleDarkMode}
          style={{ position: "absolute", top: 16, right: 16 }}
        />
      ) : null}

      <Card
        style={{
          width: "100%",
          maxWidth: 420,
          background: token.colorBgContainer,
          borderColor: token.colorBorderSecondary,
        }}
        styles={{ body: { padding: 32 } }}
      >
        <Space orientation="vertical" size="large" style={{ width: "100%" }}>
          <div>
            {logoSrc ? (
              <img
                src={logoSrc}
                alt="Motet"
                style={{ height: 28, display: "block", marginBottom: 20 }}
              />
            ) : (
              <Text strong style={{ fontSize: 18, display: "block", marginBottom: 20 }}>
                Motet
              </Text>
            )}
            <Title level={3} style={{ margin: 0 }}>
              {title}
            </Title>
            <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
              {description}
            </Paragraph>
          </div>

          <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
            <Button
              type="primary"
              size="large"
              icon={<LoginOutlined />}
              onClick={onSsoLogin}
              block
            >
              Sign in with SSO
            </Button>
            <Button
              size="large"
              icon={<KeyOutlined />}
              onClick={onOpenAuthModal}
              block
            >
              Use an API key or JWT
            </Button>
          </Space>
        </Space>
      </Card>
    </div>
  );
}
