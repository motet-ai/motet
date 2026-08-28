/**
 * Motet - Admin Dashboard - Main App
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     Main application component using Ant Design Layout system:
 *     - Header: Title, scope selector, user menu
 *     - Left Sider: Navigation menu (collapsible)
 *     - Content: Active page (SPA routing via React Router)
 *     - Right Sider: AI assistant chat (collapsible)
 *
 *     Theme: Motet-adjacent ops shell (steel blue accent, elevated surfaces).
 *     Pages render directly in the main content area (no outer Card).
 *     The API docs route is full-bleed so ReDoc fills the pane.
 *     API and Documentation collapse the left nav so the page gets the width.
 *
 *     State Persistence:
 *     - Theme (dark/light mode) is persisted to localStorage; defaults to dark when unset
 *     - Scope (tenant/motet filters) is persisted to URL query params
 *     - Sidebar collapsed state is NOT persisted (resets on reload)
 *
 * Dependencies:
 *     - React: UI framework
 *     - React Router: Client-side SPA routing
 *     - Ant Design: Layout, ConfigProvider
 *     - @motet/ui-common: useAuth, AuthModal, SignedOutPage
 *
 * Notes:
 *     Logout snapshots tokens, marks the tab signed out, clears the local
 *     session immediately (unmounting the shell for SignedOutPage), then
 *     revokes the server refresh token and redirects to the IdP. Do not
 *     await those network calls before clearing or Logout looks dead.
 *     Catalog routes wait until identity has applied tenant/motet scope so
 *     the first query key is the scoped one. JWT rotation does not refetch
 *     identity or rebuild the ConfigProvider theme.
 */
import { useState, useEffect, useMemo, useRef } from "react";
import { Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import { ConfigProvider, Layout, Spin, theme } from "antd";
import { AntdAppProvider } from "./antdApp";
import {
  useAuth,
  AuthModal,
  SignedOutPage,
  RequireRole,
  ADMIN_ROLES,
  markSignedOut,
  clearSignedOutFlag,
  wasSignedOut,
  appLogoutRedirectUri,
  finishRemoteLogout,
} from "@motet/ui-common";
import { HeaderBar } from "./layout/HeaderBar";
import { LeftSidebar } from "./layout/LeftSidebar";
import { RightSidebar } from "./layout/RightSidebar";
import { getAuthHeaders, setLiveAuth } from "./api/http";
import { useScope, type Scope } from "./hooks/useScope";
import { ThemeProvider } from "./context/ThemeContext";
import "./App.css";

// Page imports
import { WorkersPage } from "./pages/WorkersPage";
import { InstanceManagersPage } from "./pages/InstanceManagersPage";
import { MCPServersPage } from "./pages/MCPServersPage";
import { WorkspaceContainersPage } from "./pages/WorkspaceContainersPage";
import { TasksPage } from "./pages/TasksPage";
import { SchedulesPage } from "./pages/SchedulesPage";
import { MemoryPage } from "./pages/MemoryPage";
import { VaultPage } from "./pages/VaultPage";
import { WorkflowsPage } from "./pages/WorkflowsPage";
import { BundlesPage } from "./pages/BundlesPage";
import { CommandsPage } from "./pages/CommandsPage";
import { ToolsPage } from "./pages/ToolsPage";
import { SkillsPage } from "./pages/SkillsPage";
import { AgentsPage } from "./pages/AgentsPage";
import { SurfacesPage } from "./pages/SurfacesPage";
import { ArtifactsPage } from "./pages/ArtifactsPage";
import { ModelsPage } from "./pages/ModelsPage";
import { CostPage } from "./pages/CostPage";
import { TenantsPage } from "./pages/TenantsPage";
import { ApiDocsPage } from "./pages/ApiDocsPage";
import { DeveloperDocsPage } from "./pages/DeveloperDocsPage";
import { TaskFlowPage } from "./pages/TaskFlowPage";

const { useToken } = theme;

// Storage key prefix for this app (prevents collision with chat-x)
const STORAGE_PREFIX = "admin_dashboard";

interface CurrentPrincipalScope {
  id?: string | null;
  tenant_id?: string | null;
  motet_id?: string | null;
  roles?: string[];
}


/**
 * Inner component that consumes Ant Design theme tokens.
 * Must be rendered inside ConfigProvider to access the design token context.
 * Receives darkMode/setDarkMode from parent to keep theme state synchronized.
 */
function AppContent({ 
  darkMode, 
  setDarkMode 
}: { 
  darkMode: boolean; 
  setDarkMode: (value: boolean) => void;
}) {
  const { token } = useToken();
  const location = useLocation();
  const navigate = useNavigate();

  // ─────────────────────────────────────────────────────────────────────────────
  // AUTHENTICATION: JWT, SSO, API key management
  // ─────────────────────────────────────────────────────────────────────────────
  const {
    auth,
    setAuth,
    userDisplayName,
    setUserDisplayName,
    showAuthModal,
    setShowAuthModal,
    handleSsoLogin,
    isAuthenticated,
    logout,
    buildHeaders,
  } = useAuth({
    storageKey: `${STORAGE_PREFIX}_auth`,
    ssoRedirectUri: "/manage",
  });

  // ─────────────────────────────────────────────────────────────────────────────
  // SCOPE: Tenant/Motet filtering (persisted to URL params)
  // ─────────────────────────────────────────────────────────────────────────────
  const { scope, setScope } = useScope();
  const [principalRoles, setPrincipalRoles] = useState<string[]>([]);
  const [principalId, setPrincipalId] = useState<string | null>(null);
  const identityAppliedRef = useRef(false);
  const pendingIdentityScopeRef = useRef<Scope | null>(null);
  const [catalogReady, setCatalogReady] = useState(() => !isAuthenticated);
  const [signedOut, setSignedOut] = useState(() => wasSignedOut());

  setLiveAuth(auth);

  useEffect(() => {
    if (!isAuthenticated) return;
    clearSignedOutFlag();
    setSignedOut(false);
  }, [isAuthenticated]);

  // Apply identity scope once per login, then show catalog routes. Do not
  // depend on JWT string or current scope — rotation and "All Tenants"
  // must not rewrite the URL or remount pages.
  useEffect(() => {
    if (!isAuthenticated) {
      identityAppliedRef.current = false;
      pendingIdentityScopeRef.current = null;
      setCatalogReady(false);
      setPrincipalRoles([]);
      setPrincipalId(null);
      return;
    }
    if (identityAppliedRef.current) {
      setCatalogReady(true);
      return;
    }

    let cancelled = false;
    (async () => {
      let waitingForUrlScope = false;
      try {
        const response = await fetch("/api/v1/identity/me", { headers: getAuthHeaders() });
        if (response.ok) {
          const identity = (await response.json()) as CurrentPrincipalScope;
          if (cancelled) return;
          setPrincipalRoles(Array.isArray(identity.roles) ? identity.roles : []);
          setPrincipalId(identity.id || null);
          const params = new URLSearchParams(window.location.search);
          const urlHasScope = Boolean(params.get("tenant") || params.get("motet"));
          if (!urlHasScope && (identity.tenant_id || identity.motet_id)) {
            const nextScope = {
              tenantId: identity.tenant_id || null,
              motetId: identity.motet_id || null,
            };
            pendingIdentityScopeRef.current = nextScope;
            waitingForUrlScope = true;
            setScope(nextScope);
          }
        }
      } catch {
        // Leave scope/roles unset if identity lookup fails; pages fall back to principal scope.
      } finally {
        if (!cancelled) {
          identityAppliedRef.current = true;
          if (!waitingForUrlScope) {
            setCatalogReady(true);
          } else {
            window.setTimeout(() => {
              if (pendingIdentityScopeRef.current) {
                pendingIdentityScopeRef.current = null;
                setCatalogReady(true);
              }
            }, 750);
          }
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, setScope]);

  // Finish the first-paint gate after setSearchParams commits identity scope.
  useEffect(() => {
    const pending = pendingIdentityScopeRef.current;
    if (!pending) return;
    if (scope.tenantId === pending.tenantId && scope.motetId === pending.motetId) {
      pendingIdentityScopeRef.current = null;
      setCatalogReady(true);
    }
  }, [scope.tenantId, scope.motetId]);

  // ─────────────────────────────────────────────────────────────────────────────
  // UI STATE: Sidebar collapsed states (NOT persisted - resets on reload)
  // ─────────────────────────────────────────────────────────────────────────────
  const [leftSiderCollapsed, setLeftSiderCollapsed] = useState(false);
  const [rightSiderCollapsed, setRightSiderCollapsed] = useState(true);

  // Get current path for navigation highlight
  const currentPath = location.pathname;
  const isApiDocs = currentPath === "/api-docs";
  const isDocsSurface =
    isApiDocs || currentPath.startsWith("/developer-docs");

  useEffect(() => {
    setLeftSiderCollapsed(isDocsSurface);
  }, [isDocsSurface]);

  const handleLogout = () => {
    const idToken = localStorage.getItem("motet_id_token");
    const headers = buildHeaders();
    const redirectUri = appLogoutRedirectUri();
    markSignedOut();
    setSignedOut(true);
    logout();
    void finishRemoteLogout({ headers, idToken, redirectUri });
  };

  const authModal = (
    <AuthModal
      open={showAuthModal}
      onCancel={() => setShowAuthModal(false)}
      auth={auth}
      setAuth={setAuth}
    />
  );

  if (!isAuthenticated) {
    return (
      <ThemeProvider darkMode={darkMode}>
        <SignedOutPage
          variant={signedOut ? "signed_out" : "welcome"}
          productLabel="Administration"
          logoSrc={`${import.meta.env.BASE_URL}images/Motet Identity Row - ${darkMode ? "KO" : "Black"}.svg`}
          isDarkMode={darkMode}
          onToggleDarkMode={() => setDarkMode(!darkMode)}
          onSsoLogin={handleSsoLogin}
          onOpenAuthModal={() => setShowAuthModal(true)}
        />
        {authModal}
      </ThemeProvider>
    );
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // RENDER: Full dashboard layout (header, left sider, main, right sidebar)
  // ─────────────────────────────────────────────────────────────────────────────
  return (
    <ThemeProvider darkMode={darkMode}>
    <Layout className="app-shell" style={{ background: token.colorBgLayout }}>
      {authModal}

      {/* Header */}
      <HeaderBar
        token={token}
        scope={scope}
        onScopeChange={setScope}
        isAuthenticated={isAuthenticated}
        userDisplayName={userDisplayName}
        isDarkMode={darkMode}
        onToggleDarkMode={() => setDarkMode(!darkMode)}
        onOpenAuthModal={() => setShowAuthModal(true)}
        onLogout={handleLogout}
      />

      {/* Two-column layout: left sidebar + content. Right sidebar overlays when expanded. */}
      <div
        className="app-body"
        style={{
          display: "flex",
          flex: 1,
          minHeight: 0,
          width: "100%",
          background: token.colorBgLayout,
        }}
      >
        <LeftSidebar
          token={token}
          collapsed={leftSiderCollapsed}
          setCollapsed={setLeftSiderCollapsed}
          currentPath={currentPath}
          onNavigate={navigate}
          userRoles={principalRoles}
          principalId={principalId}
        />

        <main
          className="app-main"
          style={{
            flex: 1,
            minWidth: 0,
            display: isApiDocs ? "flex" : undefined,
            flexDirection: isApiDocs ? "column" : undefined,
            padding: isApiDocs ? 0 : "20px 24px",
            paddingRight: isApiDocs
              ? (rightSiderCollapsed ? 48 : 0)
              : (rightSiderCollapsed ? 56 : 24),
            overflow: isApiDocs ? "hidden" : "auto",
            background: token.colorBgLayout,
          }}
        >
          {isAuthenticated && !catalogReady ? (
            <div style={{ display: "flex", justifyContent: "center", padding: 80 }}>
              <Spin />
            </div>
          ) : (
          <Routes>
            <Route path="/" element={<Navigate to={{ pathname: "/workers", search: location.search }} replace />} />
            <Route path="/workers" element={<WorkersPage scope={scope} />} />
            <Route path="/instance-managers" element={<InstanceManagersPage />} />
            <Route path="/mcp-servers" element={<MCPServersPage />} />
            <Route path="/workspace-containers" element={<WorkspaceContainersPage scope={scope} />} />
            <Route path="/tasks" element={<TasksPage scope={scope} />} />
            <Route path="/schedules" element={<SchedulesPage scope={scope} />} />
            <Route path="/memory" element={<MemoryPage scope={scope} />} />
            <Route
              path="/vault"
              element={
                <RequireRole roles={ADMIN_ROLES} userRoles={principalRoles} principalId={principalId}>
                  <VaultPage scope={scope} />
                </RequireRole>
              }
            />
            <Route path="/bundles" element={<BundlesPage scope={scope} />} />
            <Route path="/commands" element={<CommandsPage scope={scope} />} />
            <Route path="/tools" element={<ToolsPage scope={scope} />} />
            <Route path="/skills" element={<SkillsPage scope={scope} />} />
            <Route path="/agents" element={<AgentsPage scope={scope} />} />
            <Route path="/surfaces" element={<SurfacesPage scope={scope} />} />
            <Route
              path="/tenants"
              element={
                <RequireRole roles={ADMIN_ROLES} userRoles={principalRoles} principalId={principalId}>
                  <TenantsPage scope={scope} />
                </RequireRole>
              }
            />
            <Route path="/workflows" element={<WorkflowsPage scope={scope} />} />
            <Route path="/artifacts" element={<ArtifactsPage scope={scope} />} />
            <Route path="/models" element={<ModelsPage scope={scope} />} />
            <Route path="/cost" element={<CostPage scope={scope} />} />
            <Route path="/api-docs" element={<ApiDocsPage />} />
            <Route path="/developer-docs" element={<DeveloperDocsPage />} />
            <Route path="/developer-docs/:docId" element={<DeveloperDocsPage />} />
            <Route path="/task-flow" element={<TaskFlowPage />} />
          </Routes>
          )}
        </main>
      </div>

      {/* Right sidebar: fixed overlay so main content is not squished when expanded */}
      <div
        className="right-sidebar-overlay"
        style={{
          position: "fixed",
          right: 0,
          top: 56,
          bottom: 0,
          width: rightSiderCollapsed ? 48 : 640,
          zIndex: 100,
        }}
      >
        <RightSidebar
          token={token}
          collapsed={rightSiderCollapsed}
          setCollapsed={setRightSiderCollapsed}
          scope={scope}
          currentPath={currentPath}
          buildHeaders={buildHeaders}
          darkMode={darkMode}
        />
      </div>
    </Layout>
    </ThemeProvider>
  );
}

/**
 * Root App component.
 * Manages theme state (dark/light mode) and provides it to the entire app via ConfigProvider.
 * This outer component exists because theme configuration must be set before ConfigProvider
 * renders, so we can't use useToken() here — that's why we have the inner AppContent.
 */
export default function App() {
  // Initialize dark mode from localStorage (same pattern as chat-x)
  const [darkMode, setDarkMode] = useState<boolean>(() => {
    const saved = localStorage.getItem(`${STORAGE_PREFIX}_dark_mode`);
    return saved ? JSON.parse(saved) : true;
  });

  // Persist dark mode to localStorage on change
  useEffect(() => {
    localStorage.setItem(`${STORAGE_PREFIX}_dark_mode`, JSON.stringify(darkMode));
  }, [darkMode]);

  const antdTheme = useMemo(() => {
    const lightBgLayout = "#f0f3f5";
    const lightBgContainer = "#ffffff";
    const darkBgLayout = "#0f1215";
    const darkBgContainer = "#1a1f24";
    const primary = darkMode ? "#748ffc" : "#3b5bdb";
    const primarySoft = darkMode ? "rgba(116, 143, 252, 0.14)" : "rgba(59, 91, 219, 0.10)";
    const primaryActive = darkMode ? "rgba(116, 143, 252, 0.20)" : "rgba(59, 91, 219, 0.16)";
    const primaryLink = darkMode ? "#91a7ff" : "#3b5bdb";
    return {
      algorithm: darkMode ? theme.darkAlgorithm : theme.defaultAlgorithm,
      token: {
        colorPrimary: primary,
        colorInfo: primary,
        colorLink: primaryLink,
        borderRadius: 8,
        fontSize: 14,
        colorBgLayout: darkMode ? darkBgLayout : lightBgLayout,
        colorBgContainer: darkMode ? darkBgContainer : lightBgContainer,
        colorBorder: darkMode ? "#2a323a" : "#e2e8ee",
        colorBorderSecondary: darkMode ? "#232a31" : "#ebf0f4",
      },
      components: {
        Card: {
          colorBgContainer: darkMode ? darkBgContainer : lightBgContainer,
          borderRadiusLG: 10,
        },
        Layout: {
          headerBg: darkMode ? darkBgContainer : lightBgContainer,
          bodyBg: darkMode ? darkBgLayout : lightBgLayout,
          siderBg: darkMode ? darkBgLayout : lightBgLayout,
          triggerBg: "transparent",
        },
        Menu: {
          itemBg: "transparent",
          subMenuItemBg: "transparent",
          itemSelectedBg: primarySoft,
          itemSelectedColor: primary,
          itemHoverBg: darkMode ? "rgba(255, 255, 255, 0.04)" : "rgba(15, 18, 21, 0.04)",
          itemActiveBg: primaryActive,
          itemBorderRadius: 8,
          itemMarginInline: 8,
          activeBarBorderWidth: 0,
        },
        Button: {
          borderRadius: 8,
        },
      },
    };
  }, [darkMode]);

  return (
    <ConfigProvider
      theme={antdTheme}
    >
      <AntdAppProvider>
        <AppContent darkMode={darkMode} setDarkMode={setDarkMode} />
      </AntdAppProvider>
    </ConfigProvider>
  );
}
