/**
 * Motet - Chat Explorer - Type Overrides
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-01-01
 *
 * Description:
 *     Local TypeScript module declarations for libraries that don't ship compatible types.
 *
 * Dependencies:
 *     - TypeScript: declaration merging / module declarations
 */
declare module "@ant-design/x-markdown" {
  import { ComponentType, PropsWithChildren } from "react";
  const Markdown: ComponentType<PropsWithChildren<{ className?: string }>>;
  export default Markdown;
}

