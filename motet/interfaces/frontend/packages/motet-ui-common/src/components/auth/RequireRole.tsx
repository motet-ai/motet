/**
 * Motet - Motet UI Common - RequireRole
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-14
 *
 * Description:
 *     Page-level RBAC gate. Renders children when the current principal
 *     has at least one of the required roles (or the ops_dashboard id).
 *     Otherwise renders the fallback (default: a 403 result).
 *
 * Usage:
 *     <RequireRole roles={["admin", "motet-admin"]} userRoles={roles}>
 *       <VaultPage />
 *     </RequireRole>
 */
import React from "react";
import { Result } from "antd";

export const ADMIN_ROLES = ["admin", "motet-admin"] as const;

export function hasAnyRole(
  userRoles: string[] | undefined,
  required: readonly string[],
  principalId?: string | null,
): boolean {
  if (principalId === "ops_dashboard") {
    return true;
  }
  const have = new Set(userRoles || []);
  return required.some((role) => have.has(role));
}

export interface RequireRoleProps {
  roles: readonly string[];
  userRoles: string[];
  principalId?: string | null;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

export function RequireRole({
  roles,
  userRoles,
  principalId,
  children,
  fallback,
}: RequireRoleProps) {
  if (hasAnyRole(userRoles, roles, principalId)) {
    return <>{children}</>;
  }
  if (fallback !== undefined) {
    return <>{fallback}</>;
  }
  return (
    <Result
      status="403"
      title="Not authorized"
      subTitle="This page requires an administrator role."
    />
  );
}
