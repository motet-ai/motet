/**
 * Motet UI Common - RAG Control Types
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Apache License, Version 2.0. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-25
 *
 * Description:
 *     Shared types and display helpers for artifact retrieval scope used by
 *     chat surfaces and reusable UI components.
 */

export type ArtifactRagScope = "conversation" | "principal" | "motet";

export type RagControlsValue = {
  scope: ArtifactRagScope;
  artifactIds: string[];
  artifactTags: string[];
  artifactCollectionId?: string;
  allowBroaderScope: boolean;
};

export const defaultRagControlsValue: RagControlsValue = {
  scope: "conversation",
  artifactIds: [],
  artifactTags: [],
  artifactCollectionId: "",
  allowBroaderScope: false
};

/** Short label for the retrieval-scope chip and segmented control. */
export function ragScopeShortLabel(scope: ArtifactRagScope): string {
  if (scope === "principal") return "My files";
  if (scope === "motet") return "Workspace";
  return "This chat";
}

/** True when retrieval is narrower or broader than conversation-only defaults. */
export function ragControlsIsCustom(value: RagControlsValue): boolean {
  return (
    value.scope !== "conversation" ||
    value.artifactIds.length > 0 ||
    value.artifactTags.length > 0 ||
    Boolean(value.artifactCollectionId?.trim()) ||
    value.allowBroaderScope
  );
}

/** Closed-state summary, e.g. ``This chat`` or ``My files · 2 tags``. */
export function summarizeRagControls(value: RagControlsValue): string {
  const parts = [ragScopeShortLabel(value.scope)];
  const fileCount = value.artifactIds.length;
  const tagCount = value.artifactTags.length;
  if (fileCount > 0) {
    parts.push(`${fileCount} file${fileCount === 1 ? "" : "s"}`);
  }
  if (tagCount > 0) {
    parts.push(`${tagCount} tag${tagCount === 1 ? "" : "s"}`);
  }
  return parts.join(" · ");
}
