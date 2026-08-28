/**
 * Motet - Admin Dashboard - Right Sidebar (AI Chat)
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Description:
 *     Collapsible right sidebar using Ant Design Layout.Sider.
 *     Contains AI assistant chat for contextual help.
 */
import { useMemo, useState, useRef, useEffect } from "react";
import { Button, Layout, Space, Typography, Card, Input, List, Tag } from "antd";
import {
  MessageOutlined,
  ExpandAltOutlined,
  ShrinkOutlined,
  SendOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { parseSseBuffer } from "@motet/ui-common";
import type { Scope } from "../hooks/useScope";
import { MarkdownWithMermaid } from "../components/MarkdownWithMermaid";

const { Sider } = Layout;
const { Text } = Typography;
const { TextArea } = Input;

interface RightSidebarProps {
  token: any;
  collapsed: boolean;
  setCollapsed: (collapsed: boolean) => void;
  scope: Scope;
  currentPath: string;
  buildHeaders: () => Record<string, string>;
  darkMode: boolean;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

const STORAGE_KEY = "admin_dashboard_admin_conversation_id";

function makeConversationId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `admin:${crypto.randomUUID().replace(/-/g, "")}`;
  }
  return `admin:${Math.random().toString(16).slice(2)}${Date.now().toString(16)}`;
}

function currentPageFromPath(path: string): string {
  const normalized = (path || "").replace(/^\/+/, "");
  if (!normalized) return "workers";
  return normalized.split("/")[0] || "workers";
}

export function RightSidebar({
  token,
  collapsed,
  setCollapsed,
  scope,
  currentPath,
  buildHeaders,
  darkMode,
}: RightSidebarProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [conversationId, setConversationId] = useState<string>(() => {
    const existing = localStorage.getItem(STORAGE_KEY);
    if (existing && existing.startsWith("admin:")) return existing;
    const created = makeConversationId();
    localStorage.setItem(STORAGE_KEY, created);
    return created;
  });

  const currentPage = useMemo(() => currentPageFromPath(currentPath), [currentPath]);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const newChat = () => {
    const nextId = makeConversationId();
    setConversationId(nextId);
    localStorage.setItem(STORAGE_KEY, nextId);
    setMessages([]);
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || isSending) return;

    const nextMessages: ChatMessage[] = [...messages, { role: "user", content: text }];
    setMessages(nextMessages);
    setInput("");
    setIsSending(true);

    let assistantText = "";
    setMessages([...nextMessages, { role: "assistant", content: "" }]);

    try {
      const payload = {
        stream: true,
        agent_id: "motet_admin",
        surface_id: "ops_dashboard",
        conversation_id: conversationId,
        messages: nextMessages.map((m) => ({ role: m.role, content: m.content })),
        context: {
          page_context: {
            current_page: currentPage,
            page_state: {
              tenant_id: scope.tenantId,
              motet_id: scope.motetId,
              route: currentPath,
            },
          },
        },
      };

      const response = await fetch("/api/v1/chat", {
        method: "POST",
        headers: buildHeaders(),
        body: JSON.stringify(payload),
      });
      if (!response.ok || !response.body) {
        throw new Error(`Chat request failed (${response.status})`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        const events = parseSseBuffer(parts.join("\n\n"));
        for (const ev of events) {
          const dataObj = typeof ev.data === "object" && ev.data ? ev.data : null;
          const tokenText =
            typeof ev.data === "string"
              ? ev.data
              : dataObj?.t ?? dataObj?.text ?? dataObj?.delta ?? dataObj?.content ?? "";

          if (ev.event === "token" || ev.event === "text_delta") {
            assistantText += tokenText;
            setMessages([...nextMessages, { role: "assistant", content: assistantText }]);
          } else if (
            ev.event === "message" &&
            (dataObj?.type === "text_delta" || dataObj?.event === "text_delta")
          ) {
            assistantText += tokenText;
            setMessages([...nextMessages, { role: "assistant", content: assistantText }]);
          } else if (ev.event === "end" && !assistantText) {
            const fallback =
              (typeof ev.data === "object" && (ev.data?.content || ev.data?.final_response || ev.data?.response)) || "";
            assistantText = String(fallback || "");
            setMessages([...nextMessages, { role: "assistant", content: assistantText }]);
          } else if (ev.event === "error") {
            const errText = typeof ev.data === "string" ? ev.data : ev.data?.error || "unknown error";
            assistantText = assistantText || `Error: ${errText}`;
            setMessages([...nextMessages, { role: "assistant", content: assistantText }]);
          }
        }
      }
      if (!assistantText) {
        setMessages([...nextMessages, { role: "assistant", content: "No response received." }]);
      }
    } catch (error: any) {
      setMessages([...nextMessages, { role: "assistant", content: `Error: ${String(error?.message || error)}` }]);
    } finally {
      setIsSending(false);
    }
  };

  return (
    <Sider
      width={640}
      collapsedWidth={48}
      collapsible
      collapsed={collapsed}
      onCollapse={setCollapsed}
      trigger={null}
      reverseArrow
      style={{
        background: token.colorBgLayout,
        borderLeft: `1px solid ${token.colorBorder}`,
      }}
    >
      <Space orientation="vertical" style={{ width: "100%", height: "100%", padding: 8 }}>
        {/* Header with toggle */}
        <Button
          type="text"
          icon={collapsed ? <MessageOutlined /> : <MessageOutlined />}
          onClick={() => setCollapsed(!collapsed)}
          block
          style={{ marginBottom: 8 }}
        >
          {!collapsed && "Motet AI Assistant"}
        </Button>

        {!collapsed && (
          <Card
            size="small"
     
            style={{
              background: token.colorBgContainer,
              flex: 1,
            }}
            styles={{ body: { padding: 16 } }}
          >
            <div style={{ display: "flex", flexDirection: "column", gap: 8, height: "100%" }}>
              <Space style={{ justifyContent: "space-between" }}>
                <Tag color="blue">{currentPage}</Tag>
                <Button size="small" icon={<PlusOutlined />} onClick={newChat}>
                  New
                </Button>
              </Space>
              <div
                style={{
                  flex: 1,
                  minHeight: 280,
                  maxHeight: 420,
                  overflowY: "auto",
                  border: `1px solid ${token.colorBorderSecondary}`,
                  borderRadius: token.borderRadius,
                  padding: 8,
                  background: token.colorBgLayout,
                }}
              >
                {messages.length === 0 ? (
                  <Text type="secondary">Ask about workers, tasks, schedules, deployments, and costs.</Text>
                ) : (
                  <>
                    <List
                      dataSource={messages}
                      renderItem={(item) => (
                        <List.Item style={{ display: "block", padding: "6px 0" }}>
                          <Text strong={item.role === "user"}>
                            {item.role === "user" ? "You" : "Admin Assistant"}
                          </Text>
                          <div style={{ marginTop: 4, fontSize: 12 }}>
                            {item.role === "assistant" ? (
                              <MarkdownWithMermaid content={item.content || ""} darkMode={darkMode} />
                            ) : (
                              item.content
                            )}
                          </div>
                        </List.Item>
                      )}
                    />
                    <div ref={messagesEndRef} />
                  </>
                )}
              </div>
              <TextArea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                rows={3}
                placeholder="Ask the admin assistant..."
                onPressEnter={(e) => {
                  if (!e.shiftKey) {
                    e.preventDefault();
                    void sendMessage();
                  }
                }}
              />
              <Button type="primary" icon={<SendOutlined />} loading={isSending} onClick={() => void sendMessage()}>
                Send
              </Button>
            </div>
          </Card>
        )}

      
      </Space>
    </Sider>
  );
}
