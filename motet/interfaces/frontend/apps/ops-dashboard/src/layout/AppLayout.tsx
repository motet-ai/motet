/**
 * Motet - Ops Dashboard - App Layout
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 */
import React from "react";

interface AppLayoutProps {
  header: React.ReactNode;
  leftSidebar: React.ReactNode;
  rightSidebar: React.ReactNode;
  children: React.ReactNode;
}

export function AppLayout({ header, leftSidebar, rightSidebar, children }: AppLayoutProps) {
  return (
    <div className="app-layout">
      <header className="app-header">{header}</header>
      <div className="app-body">
        <aside className="left-sidebar">{leftSidebar}</aside>
        <main className="main-content">{children}</main>
        <aside className="right-sidebar">{rightSidebar}</aside>
      </div>
    </div>
  );
}
