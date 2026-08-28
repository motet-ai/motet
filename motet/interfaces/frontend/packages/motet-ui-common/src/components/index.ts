/**
 * Motet - Motet UI Common - Components Index
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Public exports for shared UI components.
 */

// Auth components
export {
  AuthModal,
  LoginRequiredModal,
  SignedOutPage,
  RequireRole,
  hasAnyRole,
  ADMIN_ROLES,
  SIGNED_OUT_STORAGE_KEY,
  markSignedOut,
  clearSignedOutFlag,
  wasSignedOut,
  appLogoutRedirectUri,
  finishRemoteLogout,
} from "./auth";
export type {
  AuthModalProps,
  LoginRequiredModalProps,
  SignedOutPageProps,
  SignedOutVariant,
  RequireRoleProps,
} from "./auth";

// Markdown + Mermaid rendering
export { MermaidBlock, renderMarkdownWithMermaid } from "./markdown";

// Modals
export { RenameModal } from "./RenameModal";
export type { RenameModalProps } from "./RenameModal";

// Artifact RAG controls
export { RagControls } from "./RagControls";
export type { RagControlsProps, RagArtifactOption } from "./RagControls";

// Media rendering (canonical media parts: generated images, etc.)
export { MediaRenderer } from "./MediaRenderer";
export type { MediaRendererProps } from "./MediaRenderer";
