/**
 * Motet - Ops Dashboard - API Docs Page
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Renders ReDoc in a full-bleed iframe in the main content pane.
 *     Uses a same-origin relative URL so the iframe loads from the same
 *     host as the dashboard (avoids Docker/internal hostnames the
 *     browser cannot resolve).
 *
 * Notes:
 *     The /api-docs route drops app-main padding so this iframe can
 *     fill the pane. Iframe background is forced white so a transparent
 *     framed document cannot show the dark-mode dashboard through
 *     ReDoc's light-theme text. /redoc HTML also injects color-scheme:
 *     light (see interfaces/http.py).
 */
const iframeStyle: React.CSSProperties = {
  display: "block",
  width: "100%",
  height: "100%",
  flex: 1,
  minHeight: 0,
  border: "none",
  background: "#ffffff",
  colorScheme: "light",
};

export function ApiDocsPage() {
  return (
    <iframe
      title="API documentation (ReDoc)"
      src="/redoc"
      style={iframeStyle}
    />
  );
}
