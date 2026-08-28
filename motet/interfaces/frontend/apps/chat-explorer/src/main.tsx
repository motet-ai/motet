/**
 * Motet - Chat Explorer - Main Entry
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-04
 *
 * Description:
 *     Browser entrypoint that mounts the Chat Explorer React app.
 *
 * Dependencies:
 *     - React / ReactDOM: application bootstrap
 *     - ./utils/storageMigration: one-time demo_chat_x_* → chat_explorer_* copy
 *
 * Usage:
 *     Loaded by Vite as the app entry.
 */
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./App.css";
import { migrateDemoChatXStorage } from "./utils/storageMigration";

migrateDemoChatXStorage();

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <App />
);

