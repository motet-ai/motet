/**
 * Motet - Motet UI Common - Auth Modal
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Modal dialog for viewing and editing authentication credentials.
 *
 * Dependencies:
 *     - Ant Design: Alert, Button, Form, Input, Modal, Select
 *
 * Usage:
 *     import { AuthModal } from "@motet/ui-common/components";
 */
import React, { useMemo, useState } from "react";
import { Alert, Button, Form, Input, Modal, Select, Space, Typography } from "antd";
import type { AuthState } from "../../types";

/**
 * Props for the AuthModal component.
 */
export interface AuthModalProps {
  /** Whether the modal is visible */
  open: boolean;
  /** Callback to close the modal */
  onCancel: () => void;
  /** Current authentication state */
  auth: AuthState;
  /** Callback to update authentication state */
  setAuth: React.Dispatch<React.SetStateAction<AuthState>>;
}

/**
 * Modal dialog for manual authentication credential entry.
 */
export function AuthModal({ open, onCancel, auth, setAuth }: AuthModalProps) {
  const [showJwtDebug, setShowJwtDebug] = useState<boolean>(false);

  const idToken = useMemo(() => localStorage.getItem("motet_id_token") || "", [auth.jwt]);

  const decodeJwt = (token: string) => {
    if (!token) {
      return { error: "No token available.", header: null, payload: null };
    }
    const parts = token.split(".");
    if (parts.length !== 3) {
      return { error: "JWT is not in header.payload.signature format.", header: null, payload: null };
    }
    try {
      const header = JSON.parse(atob(parts[0]));
      const payload = JSON.parse(atob(parts[1]));
      return { error: null, header, payload };
    } catch (err) {
      return { error: "Unable to decode JWT. Make sure this is a valid JWT.", header: null, payload: null };
    }
  };

  const accessDebug = useMemo(() => decodeJwt(auth.jwt), [auth.jwt]);
  const idDebug = useMemo(() => decodeJwt(idToken), [idToken]);

  return (
    <Modal
      title="Authentication & Headers"
      open={open}
      onCancel={onCancel}
      footer={null}
      width={600}
    >
      {auth.jwt && (
        <Alert
          type="success"
          title="Authenticated with Keycloak SSO"
          description="You are logged in via Keycloak. The JWT token is stored securely."
          showIcon
          closable
          onClose={() => {
            setAuth((a) => ({ ...a, jwt: "" }));
            localStorage.removeItem("motet_jwt_token");
            localStorage.removeItem("motet_access_token");
            localStorage.removeItem("motet_id_token");
          }}
          style={{ marginBottom: 16 }}
        />
      )}
      <Modal
        title="JWT Claims Debug"
        open={showJwtDebug}
        onCancel={() => setShowJwtDebug(false)}
        footer={null}
        width={700}
      >
        <Space orientation="vertical" style={{ width: "100%" }} size="middle">
          {accessDebug.error ? (
            <Alert type="warning" title={`Access token: ${accessDebug.error}`} showIcon />
          ) : (
            <>
              <Typography.Text type="secondary">Access Token Header</Typography.Text>
              <pre style={{ background: "#0f0f0f", color: "#e6e6e6", padding: 12, borderRadius: 6, overflow: "auto" }}>
                {JSON.stringify(accessDebug.header, null, 2)}
              </pre>
              <Typography.Text type="secondary">Access Token Claims</Typography.Text>
              <pre style={{ background: "#0f0f0f", color: "#e6e6e6", padding: 12, borderRadius: 6, overflow: "auto" }}>
                {JSON.stringify(accessDebug.payload, null, 2)}
              </pre>
            </>
          )}
          {idDebug.error ? (
            <Alert type="warning" title={`ID token: ${idDebug.error}`} showIcon />
          ) : (
            <>
              <Typography.Text type="secondary">ID Token Header</Typography.Text>
              <pre style={{ background: "#0f0f0f", color: "#e6e6e6", padding: 12, borderRadius: 6, overflow: "auto" }}>
                {JSON.stringify(idDebug.header, null, 2)}
              </pre>
              <Typography.Text type="secondary">ID Token Claims</Typography.Text>
              <pre style={{ background: "#0f0f0f", color: "#e6e6e6", padding: 12, borderRadius: 6, overflow: "auto" }}>
                {JSON.stringify(idDebug.payload, null, 2)}
              </pre>
            </>
          )}
        </Space>
      </Modal>
      <Form layout="vertical">
        <Form.Item label="API Key (X-API-Key)">
          <Input
            value={auth.apiKey}
            onChange={(e) => setAuth((a) => ({ ...a, apiKey: e.target.value }))}
            placeholder="X-API-Key"
          />
        </Form.Item>
        <Form.Item label="JWT Token (Bearer)">
          <Input
            value={auth.jwt}
            onChange={(e) => setAuth((a) => ({ ...a, jwt: e.target.value }))}
            placeholder="Bearer token"
            addonAfter={
              auth.jwt ? (
                <Space size="small">
                  <Button
                    type="link"
                    size="small"
                    onClick={() => setShowJwtDebug(true)}
                  >
                    JWT Claims Debug
                  </Button>
                  <Button
                    type="link"
                    size="small"
                    onClick={() => {
                      setAuth((a) => ({ ...a, jwt: "" }));
                      localStorage.removeItem("motet_jwt_token");
                      localStorage.removeItem("motet_access_token");
                      localStorage.removeItem("motet_id_token");
                    }}
                  >
                    Clear
                  </Button>
                </Space>
              ) : null
            }
          />
        </Form.Item>
        <Form.Item label="Service Account Token">
          <Input
            value={auth.serviceAccountToken}
            onChange={(e) => setAuth((a) => ({ ...a, serviceAccountToken: e.target.value }))}
            placeholder="sa_..."
          />
        </Form.Item>
        <Form.Item label="Principal (X-Principal-Id)">
          <Input
            value={auth.principal}
            onChange={(e) => setAuth((a) => ({ ...a, principal: e.target.value }))}
          />
        </Form.Item>
        <Form.Item label="Tenant (X-Tenant-Id)">
          <Input
            value={auth.tenant}
            onChange={(e) => setAuth((a) => ({ ...a, tenant: e.target.value }))}
          />
        </Form.Item>
        <Form.Item label="Roles (X-Roles)">
          <Select
            mode="tags"
            value={(auth.roles || "").split(",").filter(Boolean)}
            onChange={(vals) => setAuth((a) => ({ ...a, roles: vals.join(",") }))}
            tokenSeparators={[",", " "]}
            placeholder="comma or space separated"
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
