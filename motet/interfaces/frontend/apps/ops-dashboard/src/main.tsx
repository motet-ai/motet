/**
 * Motet - Ops Dashboard - Main Entry Point
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Browser entry for the ops dashboard. Mounts React Query, the router
 *     (basename /manage), and the App shell.
 *
 * Notes:
 *     Query defaults keep the previous result visible when a query key
 *     changes (for example after identity applies tenant/motet scope) so
 *     tables do not flash empty.
 */
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { keepPreviousData, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./App.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30000, // 30 seconds
      retry: 1,
      placeholderData: keepPreviousData,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename="/manage" future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
