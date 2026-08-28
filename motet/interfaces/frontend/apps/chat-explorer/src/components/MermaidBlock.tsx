/**
 * Motet - Chat Explorer - Mermaid Rendering (re-export)
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-03-24
 *
 * Description:
 *     Re-exports MermaidBlock and renderMarkdownWithMermaid from @motet/ui-common.
 *     Local alias `renderAssistantMarkdownWithMermaid` preserved for existing callers.
 */

import { renderMarkdownWithMermaid } from "@motet/ui-common";

export { MermaidBlock, renderMarkdownWithMermaid } from "@motet/ui-common";

export const renderAssistantMarkdownWithMermaid = renderMarkdownWithMermaid;
