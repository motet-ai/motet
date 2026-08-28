/**
 * Motet - Chat Explorer - Left Sidebar
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-27
 *
 * Description:
 *     Collapsible left sidebar containing the conversation list and controls.
 *
 *     Features:
 *     - Collapsible sidebar (collapse button at top)
 *     - "New conversation" and "Delete all" buttons (delete all with confirmation modal)
 *     - Single-conversation Delete also asks for confirmation
 *     - Conversation list using Ant Design X Conversations component
 *     - List scrolls inside the card so it stays within the remaining viewport
 *     - Context menu on each conversation with Rename/Delete options
 *     - Tooltip with the full title when the list label is truncated
 *     - Active conversation highlighting
 *     - Graceful handling of collapsed state (hides list)
 *     - List label is "New Chat" when title is unset; custom / auto titles otherwise
 *     - Memoized so chat-stream re-renders do not rebuild the list or close menus
 *
 * Dependencies:
 *     - @ant-design/x: Conversations component for list display
 *     - Ant Design: Button, Card, Layout/Sider, Space
 *     - @ant-design/icons: MenuUnfoldOutlined, MenuFoldOutlined, etc.
 *
 * Usage:
 *     <LeftSidebar
 *       collapsed={siderCollapsed}
 *       setCollapsed={setSiderCollapsed}
 *       conversations={conversations}
 *       activeKey={activeConversationKey}
 *       onNewConversation={handleNewConversation}
 *       // ... other props
 *     />
 *
 * Notes:
 *     - The sider uses `.rail-left-sider` so the conversation card fills the
 *       remaining viewport below the header and the list scrolls inside the card.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Card, Layout, Modal, Space, Tooltip } from "antd";
import { MenuUnfoldOutlined, MenuFoldOutlined, PlusOutlined, SyncOutlined, EditOutlined, DeleteOutlined, ClearOutlined } from "@ant-design/icons";
import { Conversations } from "@ant-design/x";

const { Sider } = Layout;

/** Display label: custom or auto title when set, otherwise "New Chat". */
function conversationListLabel(conv: { key?: string; label?: string }): string {
  const title = String(conv.label || "").trim();
  return title || "New Chat";
}

/** Truncated list label with a tooltip of the full title when it overflows. */
function ConversationTitle({ title }: { title: string }) {
  const textRef = useRef<HTMLSpanElement>(null);
  const [overflows, setOverflows] = useState(false);

  useEffect(() => {
    const el = textRef.current;
    if (!el) return;
    const update = () => {
      setOverflows(el.scrollWidth > el.clientWidth + 1);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, [title]);

  return (
    <Tooltip
      title={overflows ? title : undefined}
      placement="right"
      autoAdjustOverflow={false}
      mouseEnterDelay={0.35}
      getPopupContainer={() => document.body}
      styles={{ body: { maxWidth: 360, whiteSpace: "normal" } }}
    >
      <span ref={textRef} className="conversation-title-text">
        {title}
      </span>
    </Tooltip>
  );
}

/**
 * Props for the LeftSidebar component.
 */
interface LeftSidebarProps {
  /** Ant Design theme token for consistent styling */
  token: any;
  /** Whether the sidebar is collapsed */
  collapsed: boolean;
  /** Callback to toggle collapse state */
  setCollapsed: (collapsed: boolean) => void;
  /** Array of conversation objects from useConversation */
  conversations: any[];
  /** Currently active conversation key */
  activeKey: string;
  /** Callback to create a new conversation */
  onNewConversation: () => void;
  /** Callback when a conversation is selected */
  onSelectConversation: (key: string) => void;
  /** Callback to rename a conversation */
  onRenameConversation: (key: string) => void;
  /** Callback to delete a conversation */
  onDeleteConversation: (key: string) => void;
  /** Callback to delete all conversations (show confirmation before calling) */
  onDeleteAllConversations?: () => void | Promise<void>;
  /** Callback to refresh conversation list from API (Option A) */
  onRefreshConversationList?: () => void | Promise<void>;
}

function sidebarPropsEqual(prev: LeftSidebarProps, next: LeftSidebarProps): boolean {
  return (
    prev.collapsed === next.collapsed &&
    prev.activeKey === next.activeKey &&
    prev.conversations === next.conversations &&
    prev.onNewConversation === next.onNewConversation &&
    prev.onSelectConversation === next.onSelectConversation &&
    prev.onRenameConversation === next.onRenameConversation &&
    prev.onDeleteConversation === next.onDeleteConversation &&
    prev.onDeleteAllConversations === next.onDeleteAllConversations &&
    prev.onRefreshConversationList === next.onRefreshConversationList &&
    prev.setCollapsed === next.setCollapsed &&
    prev.token?.colorBgLayout === next.token?.colorBgLayout &&
    prev.token?.colorBorder === next.token?.colorBorder &&
    prev.token?.colorBgContainer === next.token?.colorBgContainer &&
    prev.token?.colorError === next.token?.colorError
  );
}

function LeftSidebarInner({
  token,
  collapsed,
  setCollapsed,
  conversations,
  activeKey,
  onNewConversation,
  onSelectConversation,
  onRenameConversation,
  onDeleteConversation,
  onDeleteAllConversations,
  onRefreshConversationList,
}: LeftSidebarProps) {
  const handleDeleteAllClick = () => {
    Modal.confirm({
      title: "Delete all conversations?",
      content: "Are you sure? This cannot be undone. A new empty conversation will be created.",
      okText: "Delete all",
      okButtonProps: { danger: true },
      cancelText: "Cancel",
      onOk: () => void (onDeleteAllConversations?.() ?? Promise.resolve()),
    });
  };

  const handleDeleteOneClick = (key: string) => {
    if (conversations.length <= 1) return;
    const match = conversations.find((c: { key?: string }) => c.key === key);
    const title = conversationListLabel(match || { key });
    Modal.confirm({
      title: "Delete this conversation?",
      content: `${title} will be permanently deleted. This cannot be undone.`,
      okText: "Delete",
      okButtonProps: { danger: true },
      cancelText: "Cancel",
      onOk: () => onDeleteConversation(key),
    });
  };

  const items = useMemo(
    () =>
      conversations.map((conv: any) => ({
        key: conv.key,
        label: <ConversationTitle title={conversationListLabel(conv)} />,
        timestamp: conv.timestamp,
      })),
    [conversations],
  );

  const conversationMenu = useCallback(
    (conv: any) => ({
      items: [
        {
          key: "rename",
          icon: <EditOutlined />,
          label: "Rename",
          onClick: () => onRenameConversation(conv.key),
        },
        {
          key: "delete",
          icon: <DeleteOutlined />,
          label: "Delete",
          danger: true,
          disabled: conversations.length <= 1,
          onClick: () => handleDeleteOneClick(conv.key),
        },
      ],
    }),
    [conversations, onRenameConversation, onDeleteConversation],
  );

  return (
    <Sider
      width={280}
      collapsible
      collapsed={collapsed}
      onCollapse={setCollapsed}
      trigger={null}
      className="rail rail-left-sider"
      style={{ background: token.colorBgLayout, borderRight: `1px solid ${token.colorBorder}` }}
    >
      <div className="conversation-rail-stack" style={{ background: token.colorBgLayout }}>
        <Button
          type="text"
          icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
          onClick={() => setCollapsed(!collapsed)}
          block
          style={{ marginBottom: 8, flexShrink: 0 }}
        >
          {!collapsed && "Collapse"}
        </Button>
        <Card
          className="conversation-list-card"
          title={
            <Space style={{ width: "100%", justifyContent: "space-between" }}>
              <Button
                icon={<PlusOutlined />}
                onClick={onNewConversation}
                size="small"
                title="New conversation"
              />
              {!collapsed && (
                <Space.Compact size="small">
                  {onDeleteAllConversations && (
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<ClearOutlined />}
                      onClick={handleDeleteAllClick}
                      title="Delete all conversations"
                      disabled={conversations.length <= 1}
                    />
                  )}
                  {onRefreshConversationList && (
                    <Button
                      type="text"
                      size="small"
                      icon={<SyncOutlined />}
                      onClick={() => void onRefreshConversationList()}
                      title="Refresh conversation list"
                    />
                  )}
                </Space.Compact>
              )}
            </Space>
          }
          size="small"
          styles={{ body: { padding: 0 } }}
          style={{ background: token.colorBgContainer }}
        >
          {!collapsed && (
            <Conversations
              items={items}
              activeKey={activeKey}
              onActiveChange={onSelectConversation}
              menu={conversationMenu}
              style={{ height: "100%" }}
            />
          )}
        </Card>
      </div>
    </Sider>
  );
}

export const LeftSidebar = React.memo(LeftSidebarInner, sidebarPropsEqual);

