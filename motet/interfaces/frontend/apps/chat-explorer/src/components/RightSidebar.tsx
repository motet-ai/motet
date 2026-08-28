/**
 * Motet - Chat Explorer - Right Sidebar
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-24
 *
 * Description:
 *     Collapsible right sidebar providing observability into the AI reasoning process.
 *     When SSE frames include agent_id, Reasoning shows one subsection per agent;
 *     a single agent uses the same layout as before (no extra chrome).
 *
 * Dependencies:
 *     - @ant-design/x: ThoughtChain, Think
 *     - Ant Design: Button, Card, Collapse, Layout/Sider, Space, Tree, Typography
 */
import { useState, useEffect, useMemo } from "react";
import { Button, Card, Collapse, Layout, Space, Tree, Typography } from "antd";
import { MenuUnfoldOutlined, MenuFoldOutlined } from "@ant-design/icons";
import { ThoughtChain, Think, type ThoughtChainItemType } from "@ant-design/x";
import { jsonToTreeData, getEventRootLabel } from "../utils";
import { type AgentReasoningPanel, DEFAULT_STREAM_AGENT_KEY } from "../types";

const { Sider } = Layout;
const { Text } = Typography;

interface RightSidebarProps {
  token: unknown;
  collapsed: boolean;
  setCollapsed: (collapsed: boolean) => void;
  /** Per-agent reasoning (thinking + steps); one entry when only one stream */
  reasoningPanels: AgentReasoningPanel[];
  /** Selected chat agent (used to seed initial accordion panel before first stream frames). */
  selectedAgentId?: string;
  selectedAgentName?: string;
  eventBus: unknown[];
  errors: string[];
  watchEventsEnabled: boolean;
  showErrorsEnabled: boolean;
}

function ReasoningPanelBody({
  panel,
  headingMode
}: {
  panel: AgentReasoningPanel;
  /** `full`: show registry name + id (single-panel). `none`: title is on Collapse header only. */
  headingMode: "full" | "none";
}) {
  const showHeading =
    headingMode === "full" && panel.agentKey !== DEFAULT_STREAM_AGENT_KEY;

  return (
    <Space orientation="vertical" style={{ width: "100%" }} size="middle">
      {showHeading && (
        <div style={{ marginBottom: 4 }}>
          <Text strong style={{ fontSize: 14 }}>
            {panel.agentName}
          </Text>
          <Text type="secondary" style={{ display: "block", fontSize: 11, marginTop: 2 }}>
            {panel.agentKey}
          </Text>
        </div>
      )}
      {panel.thinkingText != null && panel.thinkingText.length > 0 && (
        <div>
          <Text strong style={{ display: "block", marginBottom: 8 }}>
            Thinking
          </Text>
          <Think
            title={panel.thinkingComplete ? "Done thinking" : "Thinking..."}
            loading={!panel.thinkingComplete}
            defaultExpanded={true}
          >
            {panel.thinkingText}
          </Think>
        </div>
      )}
      <div>
        <Text strong style={{ display: "block", marginBottom: 8 }}>
          Steps
        </Text>
        {panel.thoughtChainItems.length === 0 ? (
          <Text type="secondary">No steps emitted</Text>
        ) : (
          <div style={{ maxHeight: 280, overflow: "auto" }}>
            <ThoughtChain items={panel.thoughtChainItems as ThoughtChainItemType[]} line="solid" />
          </div>
        )}
      </div>
    </Space>
  );
}

export function RightSidebar({
  token,
  collapsed,
  setCollapsed,
  reasoningPanels,
  selectedAgentId,
  selectedAgentName,
  eventBus,
  errors,
  watchEventsEnabled,
  showErrorsEnabled
}: RightSidebarProps) {
  const t = token as { colorBgLayout?: string; colorBorder?: string; colorBgContainer?: string };

  const seededPanel: AgentReasoningPanel = {
    agentKey: selectedAgentId || DEFAULT_STREAM_AGENT_KEY,
    displayLabel: selectedAgentName || "Assistant",
    agentName: selectedAgentName || "Assistant",
    thoughtChainItems: [],
    thinkingText: null,
    thinkingComplete: false
  };
  const effectivePanels = reasoningPanels.length > 0 ? reasoningPanels : [seededPanel];

  const [expandedKeys, setExpandedKeys] = useState<string[]>(() =>
    effectivePanels.map((p) => p.agentKey)
  );

  const panelKeyString = useMemo(
    () => effectivePanels.map((p) => p.agentKey).join(","),
    [effectivePanels]
  );

  useEffect(() => {
    const panelKeys = panelKeyString.split(",").filter(Boolean);
    const newKeys = panelKeys.filter((k) => !expandedKeys.includes(k));
    if (newKeys.length > 0) {
      setExpandedKeys((prev) => [...prev, ...newKeys]);
    }
  }, [panelKeyString]);

  const reasoningInner =
    (
      <Collapse
        size="small"
        activeKey={expandedKeys}
        onChange={(keys) => setExpandedKeys(Array.isArray(keys) ? (keys as string[]) : [keys as string])}
        items={effectivePanels.map((p) => ({
          key: p.agentKey,
          label: (
            <span>
              <Text strong>{p.agentName}</Text>
              {p.agentKey !== DEFAULT_STREAM_AGENT_KEY && (
                <Text type="secondary" style={{ marginLeft: 8, fontSize: 11, fontWeight: "normal" }}>
                  {p.agentKey}
                </Text>
              )}
            </span>
          ),
          children: <ReasoningPanelBody panel={p} headingMode="none" />
        }))}
      />
    );

  return (
    <Sider
      width={360}
      collapsible
      collapsed={collapsed}
      onCollapse={setCollapsed}
      trigger={null}
      className="rail rail-right-sider"
      style={{ background: t.colorBgLayout, borderLeft: `1px solid ${t.colorBorder}` }}
    >
      <Space orientation="vertical" style={{ width: "100%", background: t.colorBgLayout }}>
        <Button
          type="text"
          icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
          onClick={() => setCollapsed(!collapsed)}
          block
          style={{ marginBottom: 8 }}
        >
          {!collapsed && "Collapse"}
        </Button>

        {!collapsed && (
          <>
            {reasoningInner}
            {watchEventsEnabled && (
              <Card title="Event Bus" size="small" style={{ background: t.colorBgContainer }}>
                {eventBus.length === 0 ? (
                  <Text type="secondary">No events</Text>
                ) : (
                  <div>
                    {eventBus.map((item: unknown, idx: number) => (
                      <div
                        key={idx}
                        style={{ padding: "8px 0", borderBottom: `1px solid ${t.colorBorder}` }}
                      >
                        <Tree
                          showLine
                          selectable={false}
                          defaultExpandAll={false}
                          treeData={jsonToTreeData(item, `event:${idx}`, getEventRootLabel(item, idx))}
                        />
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            )}
            {showErrorsEnabled && (
              <Card title="Errors" size="small" style={{ background: t.colorBgContainer }}>
                {errors.length === 0 ? (
                  <Text type="secondary">No errors</Text>
                ) : (
                  <div>
                    {errors.map((err: string, idx: number) => (
                      <div
                        key={idx}
                        style={{ padding: "8px 0", borderBottom: `1px solid ${t.colorBorder}` }}
                      >
                        <Text type="danger">{err}</Text>
                      </div>
                    ))}
                  </div>
                )}
              </Card>
            )}
          </>
        )}
      </Space>
    </Sider>
  );
}
