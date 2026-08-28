/**
 * Motet - Ops Dashboard - Ant Design App Bridge
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Wraps the dashboard in antd ``App`` so ``message`` toasts use the
 *     ConfigProvider theme. Pages import ``message`` from this module
 *     instead of the static ``antd`` helper.
 *
 * Usage:
 *     <ConfigProvider theme={...}>
 *       <AntdAppProvider>
 *         <AppContent />
 *       </AntdAppProvider>
 *     </ConfigProvider>
 *
 *     import { message } from "../antdApp";
 *     message.success("Saved");
 */
import { App as AntdApp } from "antd";
import { message as staticMessage } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import type { ReactNode } from "react";

let bound: MessageInstance | null = null;

function bindAppMessage(api: MessageInstance): void {
  bound = api;
}

function target(): MessageInstance {
  return bound ?? staticMessage;
}

/** Theme-aware message API once ``AntdAppProvider`` is mounted. */
export const message: Pick<
  MessageInstance,
  "success" | "error" | "info" | "warning" | "loading" | "open" | "destroy"
> = {
  success: (...args) => target().success(...args),
  error: (...args) => target().error(...args),
  info: (...args) => target().info(...args),
  warning: (...args) => target().warning(...args),
  loading: (...args) => target().loading(...args),
  open: (...args) => target().open(...args),
  destroy: (...args) => target().destroy(...args),
};

function MessageBinder({ children }: { children: ReactNode }) {
  const api = AntdApp.useApp();
  bindAppMessage(api.message);
  return <>{children}</>;
}

export function AntdAppProvider({ children }: { children: ReactNode }) {
  return (
    <AntdApp>
      <MessageBinder>{children}</MessageBinder>
    </AntdApp>
  );
}
