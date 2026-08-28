/**
 * Motet - Ops Dashboard - Markdown With Mermaid (re-export)
 *
 * Re-exports from @motet/ui-common with a props-based wrapper matching
 * the original MarkdownWithMermaid component API.
 */
import React from "react";
import { renderMarkdownWithMermaid } from "@motet/ui-common";

export interface MarkdownWithMermaidProps {
  content: string;
  darkMode?: boolean;
}

export function MarkdownWithMermaid({ content, darkMode = false }: MarkdownWithMermaidProps): React.ReactNode {
  return renderMarkdownWithMermaid(content, darkMode);
}
