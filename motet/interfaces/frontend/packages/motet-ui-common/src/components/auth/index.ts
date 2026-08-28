/**
 * Motet - Motet UI Common - Auth Components Index
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Public exports for shared authentication UI.
 */

export { AuthModal } from "./AuthModal";
export type { AuthModalProps } from "./AuthModal";

export { LoginRequiredModal } from "./LoginRequiredModal";
export type { LoginRequiredModalProps } from "./LoginRequiredModal";

export { SignedOutPage } from "./SignedOutPage";
export type { SignedOutPageProps, SignedOutVariant } from "./SignedOutPage";

export {
  SIGNED_OUT_STORAGE_KEY,
  markSignedOut,
  clearSignedOutFlag,
  wasSignedOut,
  appLogoutRedirectUri,
  finishRemoteLogout,
} from "./signedOutSession";

export { RequireRole, hasAnyRole, ADMIN_ROLES } from "./RequireRole";
export type { RequireRoleProps } from "./RequireRole";
