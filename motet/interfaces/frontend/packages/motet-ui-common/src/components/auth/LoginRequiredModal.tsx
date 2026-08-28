/**
 * Motet - Motet UI Common - Login Required Modal
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-01-28
 *
 * Description:
 *     Modal dialog that blocks the UI when user is not authenticated.
 *     Prevents all interactions until user logs in.
 *
 * Dependencies:
 *     - Ant Design: Button, Modal, Space, Typography
 *     - @ant-design/icons: LoginOutlined, KeyOutlined
 *
 * Usage:
 *     import { LoginRequiredModal } from "@motet/ui-common/components";
 */
import React from "react";
import { Button, Modal, Space, Typography } from "antd";
import { LoginOutlined, KeyOutlined } from "@ant-design/icons";

const { Title, Paragraph } = Typography;

/**
 * Props for the LoginRequiredModal component.
 */
export interface LoginRequiredModalProps {
  /** Whether the modal is visible (should be true when user is logged out) */
  open: boolean;
  /** Callback to open SSO login popup */
  onSsoLogin: () => void;
  /** Callback to open manual auth settings modal */
  onOpenAuthModal: () => void;
  /** Optional custom title */
  title?: string;
  /** Optional custom description */
  description?: string;
}

/**
 * Modal dialog that blocks the UI when user is not authenticated.
 */
export function LoginRequiredModal({ 
  open, 
  onSsoLogin, 
  onOpenAuthModal,
  title = "Authentication Required",
  description = "You must be logged in to use this application. Please choose one of the following authentication methods:"
}: LoginRequiredModalProps) {
  return (
    <Modal
      title={
        <Space>
          <LoginOutlined />
          <span>Login Required</span>
        </Space>
      }
      open={open}
      closable={false}
      maskClosable={false}
      footer={null}
      width={500}
      centered
    >
      <Space orientation="vertical" size="large" style={{ width: "100%" }}>
        <div>
          <Title level={4}>{title}</Title>
          <Paragraph>{description}</Paragraph>
        </div>

        <Space orientation="vertical" size="middle" style={{ width: "100%" }}>
          <Button
            type="primary"
            size="large"
            icon={<LoginOutlined />}
            onClick={onSsoLogin}
            block
          >
            Login with SSO (Keycloak)
          </Button>

          <Button
            size="large"
            icon={<KeyOutlined />}
            onClick={onOpenAuthModal}
            block
          >
            Manual Authentication (API Key / JWT)
          </Button>
        </Space>

        <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 16 }}>
          After logging in, you'll be able to access all features.
        </Paragraph>
      </Space>
    </Modal>
  );
}
