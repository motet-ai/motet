/**
 * Motet - Admin Dashboard - Task Flow Page (Popup Route)
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-26
 *
 * Description:
 *     In-app task flow view for a single task (main dashboard layout, not a popup).
 *     Opened from the Tasks table via "Flow", or from Cost events with
 *     ?taskId=&commandId= to jump to a specific command detail.
 *     Keeps the Tasks page header for continuity.
 *     Cancel on a still-running task calls POST /api/v1/tasks/{task_id}/cancel.
 *
 * Notes:
 *     Dark mode comes from ThemeContext (not hard-coded Ant token hex sniffing).
 */
import { useState, useEffect, useCallback, useRef } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import {
  Typography,
  Card,
  Button,
  Space,
  Spin,
  Alert,
  Tabs,
  Tag,
  Select,
  theme,
  Popconfirm,
} from "antd";
import {
  ReloadOutlined,
  NodeIndexOutlined,
  UnorderedListOutlined,
  ArrowLeftOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { message } from "../antdApp";
import { cancelLiveTask, taskStatusIsCancellable } from "./taskCancel";
import {
  fetchTaskFlow,
  fetchCommandDebugData,
  getCommandTypeColors,
  baseCommandType,
  getAgenticLoopIterationLabel,
  type TaskFlowData,
  type TaskFlowCommand,
  type TaskFlowEvent,
  type CommandDebugData,
} from "./taskFlowShared";
import { TaskFlowGraph } from "./TaskFlowGraph";
import { useTheme } from "../context/ThemeContext";

const { Title, Text } = Typography;

function getStatusIconEmoji(status: string): string {
  switch (status?.toLowerCase()) {
    case "completed":
    case "success":
      return "✅";
    case "error":
    case "failed":
      return "❌";
    case "running":
    case "executing":
      return "🔄";
    default:
      return "⏳";
  }
}

function getStatusColorHex(status: string): string {
  switch (status?.toLowerCase()) {
    case "completed":
    case "success":
      return "#28a745";
    case "error":
    case "failed":
      return "#dc3545";
    case "running":
    case "executing":
      return "#ffc107";
    default:
      return "#6c757d";
  }
}

function getAdditionalInfo(cmd: TaskFlowCommand): string {
  const base = baseCommandType(cmd.command_type);
  const inputs = cmd.inputs as Record<string, unknown> | undefined;
  const results = cmd.results as Record<string, unknown> | undefined;

  if (base === "workflow_execution") {
    const name =
      (inputs?.workflow_name as string) ||
      (results?.workflow_name as string) ||
      "";
    if (name && typeof name === "string" && name.trim()) return name;
  }
  if (base === "tool_execution") {
    const toolName =
      (inputs?.tool_name as string) ||
      (inputs?.parameters as Record<string, unknown>)?.tool_name ||
      (results?.tool_name as string) ||
      "";
    if (toolName && typeof toolName === "string" && toolName.trim()) return toolName;
  }
  if (base === "model_inference" || base === "model_stream") {
    const modelSettings = (inputs?.model_settings || results?.model_settings) as Record<string, unknown> | undefined;
    let modelName = "";
    if (modelSettings && typeof modelSettings === "object") {
      modelName =
        (modelSettings.model as string) ||
        (modelSettings.model_name as string) ||
        "";
    }
    if (!modelName && results?.model) {
      const m = String(results.model);
      modelName = m.includes(":") ? m.split(":")[1]?.trim() ?? m : m;
    }
    if (modelName && typeof modelName === "string" && modelName.trim()) return modelName;
  }
  return "";
}

function getWorkflowNameDisplay(cmd: TaskFlowCommand): string {
  if (baseCommandType(cmd.command_type) !== "workflow_execution") return "";
  const name =
    (cmd.inputs as Record<string, unknown>)?.workflow_name ||
    (cmd.results as Record<string, unknown>)?.workflow_name ||
    "";
  return name && typeof name === "string" && name.trim() ? String(name) : "";
}

function getToolNameDisplay(cmd: TaskFlowCommand): string {
  if (baseCommandType(cmd.command_type) !== "tool_execution") return "";
  const inputs = cmd.inputs as Record<string, unknown> | undefined;
  const toolName =
    (inputs?.tool_name as string) ||
    (inputs?.parameters as Record<string, unknown>)?.tool_name ||
    (cmd.results as Record<string, unknown>)?.tool_name ||
    "";
  return toolName && typeof toolName === "string" && toolName.trim() ? String(toolName) : "";
}

function getModelNameDisplay(cmd: TaskFlowCommand): string {
  const base = baseCommandType(cmd.command_type);
  if (base !== "model_inference" && base !== "model_stream") return "";
  const inputs = cmd.inputs as Record<string, unknown> | undefined;
  const results = cmd.results as Record<string, unknown> | undefined;
  const modelSettings = (inputs?.model_settings || results?.model_settings) as Record<string, unknown> | undefined;
  let modelName = "";
  if (modelSettings && typeof modelSettings === "object") {
    modelName =
      (modelSettings.model as string) ||
      (modelSettings.model_name as string) ||
      "";
  }
  if (!modelName && results?.model) {
    const m = String(results.model);
    modelName = m.includes(":") ? m.split(":")[1]?.trim() ?? m : m;
  }
  return modelName && typeof modelName === "string" && modelName.trim() ? String(modelName) : "";
}

function isCommandInProgress(cmd: TaskFlowCommand): boolean {
  const s = (cmd.status ?? "").toLowerCase();
  return ["running", "executing", "pending"].includes(s);
}

function getDurationDisplay(cmd: TaskFlowCommand): string {
  if (isCommandInProgress(cmd) && cmd.duration_ms == null) return "TBD";
  if (cmd.duration_ms != null) return `${cmd.duration_ms}ms`;
  return "N/A";
}

/** True when the task-flow endpoint truncated this command's inputs/results for display. */
function isDisplayTruncated(cmd: TaskFlowCommand): boolean {
  const inputs = cmd.inputs as Record<string, unknown> | undefined;
  if (inputs && "..." in inputs) return true;
  const results = cmd.results as Record<string, unknown> | undefined;
  if (results && typeof results.content === "string" && results.content.endsWith("... [truncated]")) {
    return true;
  }
  return false;
}

export function TaskFlowPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const taskId = searchParams.get("taskId") ?? "";
  const focusCommandId = searchParams.get("commandId") ?? "";
  const { token } = theme.useToken();
  const focusHandledRef = useRef<string | null>(null);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = "Motet Administration : Task Flow";
    return () => {
      document.title = previousTitle;
    };
  }, []);

  const handleBackToTasks = () => {
    navigate("/tasks");
  };
  const [taskFlow, setTaskFlow] = useState<TaskFlowData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [cancelling, setCancelling] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [activeTab, setActiveTab] = useState<string>(
    focusCommandId ? "commands" : "visualization"
  );
  const [eventFilterType, setEventFilterType] = useState<string>("all");
  const [fullCommandData, setFullCommandData] = useState<Record<string, CommandDebugData>>({});
  const [loadingFullData, setLoadingFullData] = useState<Record<string, boolean>>({});
  const [fullDataErrors, setFullDataErrors] = useState<Record<string, string>>({});

  const scrollToCommand = useCallback((commandId: string) => {
    setActiveTab("commands");
    window.setTimeout(() => {
      document.getElementById(`cmd-${commandId}`)?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }, 120);
  }, []);

  const handleGraphNodeSelect = useCallback(
    (commandId: string) => {
      scrollToCommand(commandId);
    },
    [scrollToCommand]
  );

  // Deep-link from Cost (or elsewhere): /task-flow?taskId=…&commandId=…
  useEffect(() => {
    if (!focusCommandId || !taskFlow || loading) return;
    if (focusHandledRef.current === focusCommandId) return;
    const exists = taskFlow.commands?.some((c) => c.command_id === focusCommandId);
    if (!exists) return;
    focusHandledRef.current = focusCommandId;
    scrollToCommand(focusCommandId);
  }, [focusCommandId, taskFlow, loading, scrollToCommand]);

  const loadFullCommandData = useCallback(async (commandId: string) => {
    setLoadingFullData((prev) => ({ ...prev, [commandId]: true }));
    setFullDataErrors((prev) => {
      const next = { ...prev };
      delete next[commandId];
      return next;
    });
    try {
      const data = await fetchCommandDebugData(commandId);
      if (data && (data.command_data || data.result)) {
        setFullCommandData((prev) => ({ ...prev, [commandId]: data }));
      } else {
        setFullDataErrors((prev) => ({
          ...prev,
          [commandId]: "Full data unavailable (command data may have expired from Redis).",
        }));
      }
    } catch (error) {
      const message =
        error instanceof Error && error.message
          ? error.message
          : "Failed to load full command data";
      setFullDataErrors((prev) => ({ ...prev, [commandId]: message }));
    } finally {
      setLoadingFullData((prev) => ({ ...prev, [commandId]: false }));
    }
  }, []);

  // Use ThemeContext — do not sniff hard-coded container hexes (theme tokens change).
  const isDarkMode = useTheme();

  const isTaskCompleted =
    taskFlow?.events?.some(
      (e) => e.kind === "task_completed" || (e.data && (e.data as Record<string, unknown>).kind === "task_completed")
    ) ?? false;
  const hasActiveCommand = (taskFlow?.commands || []).some((command) =>
    taskStatusIsCancellable(command.status)
  );

  const fetchData = useCallback(
    async (isInitial = false) => {
      if (!taskId) return;
      if (isInitial) setLoading(true);
      else setIsRefreshing(true);
      setError(null);
      try {
        const data = await fetchTaskFlow(taskId);
        if (data) {
          setTaskFlow(data);
          setLastRefresh(new Date());
        } else {
          setError("Failed to load task flow");
        }
      } catch (err) {
        setError(String(err));
      } finally {
        setLoading(false);
        setIsRefreshing(false);
      }
    },
    [taskId]
  );

  const handleCancelTask = async () => {
    if (!taskId) return;
    setCancelling(true);
    try {
      await cancelLiveTask(taskId);
      message.success("Cancel requested");
      await fetchData(false);
    } catch (err) {
      message.error(err instanceof Error ? err.message : "Failed to cancel task");
    } finally {
      setCancelling(false);
    }
  };

  useEffect(() => {
    fetchData(true);
  }, [fetchData]);

  useEffect(() => {
    if (!autoRefresh || isTaskCompleted || !taskId) return;
    const t = setInterval(() => fetchData(false), 3000);
    return () => clearInterval(t);
  }, [autoRefresh, isTaskCompleted, taskId, fetchData]);

  const tasksPageHeader = (
    <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
      <div>
        <Title level={2}>
          <UnorderedListOutlined style={{ marginRight: 12 }} />
          Tasks
        </Title>
        <Text type="secondary">View recent command executions and task flows</Text>
      </div>
    </div>
  );

  if (!taskId) {
    return (
      <div>
        {tasksPageHeader}
        <Alert type="warning" title="Missing task ID" description="Open this page from the Tasks table using the “Flow” button for a specific task." />
        <div style={{ marginTop: 16 }}>
          <Button icon={<ArrowLeftOutlined />} onClick={handleBackToTasks}>
            Back to Tasks
          </Button>
        </div>
      </div>
    );
  }

  if (loading && !taskFlow) {
    return (
      <div>
        {tasksPageHeader}
        <div style={{ padding: 24, textAlign: "center" }}>
          <Spin size="large" />
          <div style={{ marginTop: 16 }}>
            <Text type="secondary">Loading task flow…</Text>
          </div>
        </div>
      </div>
    );
  }

  if (error && !taskFlow) {
    return (
      <div>
        {tasksPageHeader}
        <Alert type="error" title="Failed to load task flow" description={error} />
        <div style={{ marginTop: 16 }}>
          <Button onClick={() => fetchData(true)}>Retry</Button>
          <Button icon={<ArrowLeftOutlined />} onClick={handleBackToTasks} style={{ marginLeft: 8 }}>
            Back to Tasks
          </Button>
        </div>
      </div>
    );
  }

  const { commands = [], events = [] } = taskFlow ?? {};
  const colorPalette = getCommandTypeColors(isDarkMode);

  // Fixed legend order and labels matching task_flow_popup.html
  const LEGEND_ITEMS: { key: string; label: string }[] = [
    { key: "conversation_analysis", label: "conversation_analysis" },
    { key: "agent", label: "agent / agentic_loop" },
    { key: "model_inference", label: "model_inference" },
    { key: "memory_reset", label: "memory_reset" },
    { key: "prepare_context", label: "prepare_context" },
    { key: "model_stream", label: "model_stream" },
    { key: "finalize_turn", label: "finalize_turn" },
    { key: "tool_execution", label: "tool_execution" },
    { key: "workflow_execution", label: "workflow_execution" },
    { key: "distributed_command", label: "distributed_command" },
  ];

  const tabItems = [
    {
      key: "visualization",
      label: (
        <span>
          <NodeIndexOutlined /> Visualization
        </span>
      ),
      children: (
        <div style={{ paddingTop: 8 }}>
          {taskFlow && commands.length > 0 ? (
            <>
              <div
                style={{
                  marginBottom: 10,
                  padding: 10,
                  background: isDarkMode ? "#2d2d2d" : "#f8f9fa",
                  borderRadius: 4,
                }}
              >
                <div
                  style={{
                    margin: 0,
                    marginBottom: 8,
                    fontSize: 12,
                    fontWeight: 600,
                    color: isDarkMode ? "#adb5bd" : "#495057",
                  }}
                >
                  Command Type Colors:
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8, fontSize: 12 }}>
                  {LEGEND_ITEMS.map(({ key, label }) => {
                    const colors =
                      colorPalette[key] ?? {
                        fill: "#868e96",
                        stroke: "#adb5bd",
                        color: "#000",
                      };
                    return (
                      <span
                        key={key}
                        style={{ display: "flex", alignItems: "center", gap: 4 }}
                      >
                        <div
                          style={{
                            width: 12,
                            height: 12,
                            background: colors.fill,
                            borderRadius: 2,
                          }}
                        />
                        {label}
                      </span>
                    );
                  })}
                </div>
              </div>
              <TaskFlowGraph
                taskFlow={taskFlow}
                isDarkMode={isDarkMode}
                onNodeSelect={handleGraphNodeSelect}
              />
            </>
          ) : (
            <Text type="secondary">No visualization available</Text>
          )}
        </div>
      ),
    },
    {
      key: "commands",
      label: (
        <span>
          <NodeIndexOutlined /> Commands ({commands.length})
        </span>
      ),
      children: (() => {
        const executionFlow = taskFlow?.execution_flow;
        const edges = executionFlow?.edges ?? [];
        const haveRealEdges = edges.length > 0;
        const childMap: Record<string, string[]> = {};
        if (haveRealEdges) {
          edges.forEach((edge: { source: string; target: string }) => {
            if (!childMap[edge.source]) childMap[edge.source] = [];
            childMap[edge.source].push(edge.target);
          });
        }
        const jsonContentStyle = {
          background: isDarkMode ? "#1a1a2e" : "#f8f9fa",
          padding: 10,
          borderRadius: 4,
          fontFamily: "monospace",
          fontSize: 12,
          whiteSpace: "pre-wrap",
          marginTop: 4,
          marginBottom: 0,
        };
        const commandItemStyle = {
          marginBottom: 15,
          padding: 10,
          border: `1px solid ${isDarkMode ? "#434343" : "#ddd"}`,
          borderRadius: 4,
          background: isDarkMode ? token.colorBgElevated : "#fff",
        };
        const tocBoxStyle = {
          marginBottom: 30,
          padding: 20,
          background: isDarkMode ? "#2d2d2d" : "#f8f9fa",
          borderRadius: 8,
          border: `1px solid ${isDarkMode ? "#434343" : "#dee2e6"}`,
        };
        return (
          <div style={{ paddingTop: 8 }}>
            {commands.length > 0 ? (
              <>
                {/* Table of Contents - matching task_flow_popup.html */}
                <div style={tocBoxStyle}>
                  <div
                    style={{
                      margin: 0,
                      marginBottom: 15,
                      fontSize: 14,
                      fontWeight: 600,
                      color: isDarkMode ? "#adb5bd" : "#495057",
                    }}
                  >
                    📋 Command Execution Table of Contents
                  </div>
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
                      gap: 10,
                    }}
                  >
                    {commands.map((cmd) => {
                      const additionalInfo = getAdditionalInfo(cmd);
                      const iterationLabel = getAgenticLoopIterationLabel(cmd);
                      const statusColor = getStatusColorHex(cmd.status);
                      const inProgress = isCommandInProgress(cmd);
                      return (
                        <a
                          key={cmd.command_id}
                          href={`#cmd-${cmd.command_id}`}
                          className={inProgress ? "task-flow-command-in-progress" : undefined}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                            padding: "10px 12px",
                            background: isDarkMode ? "#1f1f1f" : "#fff",
                            border: `1px solid ${isDarkMode ? "#434343" : "#dee2e6"}`,
                            borderRadius: 4,
                            textDecoration: "none",
                            color: isDarkMode ? "#e4e4e4" : "#495057",
                            borderLeft: `4px solid ${statusColor}`,
                          }}
                        >
                          <span style={{ fontSize: 16 }}>{getStatusIconEmoji(cmd.status)}</span>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div
                              style={{
                                fontWeight: 500,
                                fontSize: 13,
                                whiteSpace: "nowrap",
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                              }}
                            >
                              {cmd.command_type}
                              {additionalInfo ? ` • ${additionalInfo}` : ""}
                              {iterationLabel ? ` • ${iterationLabel}` : ""}
                            </div>
                            <div
                              style={{
                                fontSize: 11,
                                color: isDarkMode ? "#8c8c8c" : "#6c757d",
                                whiteSpace: "nowrap",
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                              }}
                            >
                              {cmd.command_id.substring(0, 12)}... • {getDurationDisplay(cmd)}
                            </div>
                          </div>
                        </a>
                      );
                    })}
                  </div>
                </div>
                {/* Command detail blocks - matching task_flow_popup.html */}
                {commands.map((cmd) => {
                  const workflowName = getWorkflowNameDisplay(cmd);
                  const toolName = getToolNameDisplay(cmd);
                  const modelName = getModelNameDisplay(cmd);
                  const iterationLabel = getAgenticLoopIterationLabel(cmd);
                  const childIds = childMap[cmd.command_id] ?? [];
                  const inProgress = isCommandInProgress(cmd);
                  const full = fullCommandData[cmd.command_id];
                  const fullError = fullDataErrors[cmd.command_id];
                  const truncated = isDisplayTruncated(cmd);
                  const displayInputs =
                    (full?.command_data as Record<string, unknown> | undefined) ?? cmd.inputs;
                  const displayResults =
                    (full?.result as Record<string, unknown> | undefined) ?? cmd.results;
                  return (
                    <div
                      key={cmd.command_id}
                      id={`cmd-${cmd.command_id}`}
                      className={inProgress ? "task-flow-command-in-progress" : undefined}
                      style={{
                        ...commandItemStyle,
                        scrollMarginTop: 20,
                      }}
                    >
                      <div
                        style={{
                          fontWeight: "bold",
                          color: "#007bff",
                          marginBottom: 8,
                        }}
                      >
                        {cmd.command_type} ({cmd.command_id})
                      </div>
                      <p style={{ margin: "0 0 8px 0", fontSize: 13 }}>
                        <strong>Status:</strong> {cmd.status} | <strong>Duration:</strong>{" "}
                        {getDurationDisplay(cmd)} | <strong>Worker:</strong> {cmd.worker_id ?? "N/A"}
                        {workflowName ? <> | <strong>Workflow:</strong> {workflowName}</> : null}
                        {toolName ? <> | <strong>Tool:</strong> {toolName}</> : null}
                        {modelName ? <> | <strong>Model:</strong> {modelName}</> : null}
                        {iterationLabel ? <> | <strong>Loop:</strong> {iterationLabel}</> : null}
                      </p>
                      <div style={{ marginBottom: 8, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                        {full ? (
                          <Tag color="success">Full data loaded</Tag>
                        ) : (
                          <Button
                            size="small"
                            loading={loadingFullData[cmd.command_id] ?? false}
                            type={truncated ? "primary" : "default"}
                            onClick={() => loadFullCommandData(cmd.command_id)}
                          >
                            Load full data
                          </Button>
                        )}
                        {truncated && !full && (
                          <Text type="warning" style={{ fontSize: 12 }}>
                            Some fields were truncated for display
                          </Text>
                        )}
                        {fullError && (
                          <Text type="danger" style={{ fontSize: 12 }}>
                            {fullError}
                          </Text>
                        )}
                      </div>
                      {displayInputs != null && Object.keys(displayInputs).length > 0 && (
                        <details style={{ marginTop: 8 }} open>
                          <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 13 }}>
                            Inputs ({Object.keys(displayInputs).length} keys{full?.command_data ? ", full" : ""})
                          </summary>
                          <pre style={jsonContentStyle}>
                            {JSON.stringify(displayInputs, null, 2)}
                          </pre>
                        </details>
                      )}
                      {haveRealEdges && childIds.length > 0 && (
                        <div style={{ marginTop: 8 }}>
                          <p style={{ margin: "0 0 6px 0", fontWeight: 600 }}>Calls Commands:</p>
                          <ul style={{ margin: "6px 0 10px 16px", padding: 0 }}>
                            {childIds.map((childId) => {
                              const childCmd = commands.find((c) => c.command_id === childId);
                              const labelType = childCmd?.command_type ?? "unknown";
                              const labelStatus = childCmd?.status ?? "pending";
                              const labelWorker = childCmd?.worker_id ?? "N/A";
                              const shortId = childId.substring(0, 12) + "...";
                              return (
                                <li key={childId} style={{ margin: "2px 0" }}>
                                  <a
                                    href={`#cmd-${childId}`}
                                    style={{ color: "#007bff", textDecoration: "none" }}
                                  >
                                    {labelType} • {labelStatus} • {shortId} • worker: {labelWorker}
                                  </a>
                                </li>
                              );
                            })}
                          </ul>
                        </div>
                      )}
                      {displayResults != null && Object.keys(displayResults).length > 0 && (
                        <details style={{ marginTop: 8 }} open>
                          <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 13 }}>
                            Results ({Object.keys(displayResults).length} keys{full?.result ? ", full" : ""})
                          </summary>
                          <pre style={jsonContentStyle}>
                            {JSON.stringify(displayResults, null, 2)}
                          </pre>
                        </details>
                      )}
                    </div>
                  );
                })}
              </>
            ) : (
              <Text type="secondary">No commands</Text>
            )}
          </div>
        );
      })(),
    },
    {
      key: "events",
      label: (
        <span>
          <UnorderedListOutlined /> Events ({events.length})
        </span>
      ),
      children: (() => {
        const eventTypes = [...new Set(events.map((e) => e.kind || "unknown"))].sort();
        const filteredEvents =
          eventFilterType === "all"
            ? events
            : events.filter((e) => (e.kind || "unknown") === eventFilterType);
        const displayEvents = filteredEvents.slice(0, 100);
        return (
          <div style={{ paddingTop: 8 }}>
            <div
              style={{
                marginBottom: 15,
                display: "flex",
                alignItems: "center",
                gap: 15,
                flexWrap: "wrap",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span style={{ fontSize: 14 }}>Filter by type:</span>
                <Select
                  value={eventFilterType}
                  onChange={setEventFilterType}
                  options={[
                    { value: "all", label: "All Events" },
                    ...eventTypes.map((t) => ({ value: t, label: t })),
                  ]}
                  style={{ minWidth: 160 }}
                  size="small"
                />
              </div>
            </div>
            <div>
              {events.length > 0 ? (
                <>
                  {displayEvents.length > 0 ? (
                    displayEvents.map((event: TaskFlowEvent, idx: number) => {
                const commandId = event.command_id ?? (event.data?.command_id as string | undefined);
                const commandType =
                  event.command_type ?? (event.data?.command_type as string | undefined);
                return (
                  <Card
                    key={idx}
                    size="small"
                    style={{ marginBottom: 8, background: token.colorBgElevated }}
                    styles={{ body: { padding: 10 } }}
                  >
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        marginBottom: 6,
                        flexWrap: "wrap",
                      }}
                    >
                      <Tag color="purple">{event.kind}</Tag>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        {event.timestamp
                          ? new Date(event.timestamp).toLocaleString()
                          : "—"}
                      </Text>
                      {commandType && (
                        <Tag color="blue" style={{ fontSize: 10 }}>
                          {commandType}
                        </Tag>
                      )}
                      {commandId && (
                        <Text code style={{ fontSize: 10 }}>
                          {commandId.slice(0, 12)}...
                        </Text>
                      )}
                    </div>
                    {event.data && Object.keys(event.data).length > 0 && (
                      <details>
                        <summary
                          style={{
                            cursor: "pointer",
                            fontSize: 12,
                            color: token.colorPrimary,
                          }}
                        >
                          Event Data ({Object.keys(event.data).length} fields)
                        </summary>
                        <pre
                          style={{
                            fontSize: 12,
                            background: token.colorFillSecondary,
                            padding: 10,
                            borderRadius: 4,
                            marginTop: 4,
                            marginBottom: 0,
                            whiteSpace: "pre-wrap",
                            wordBreak: "break-all",
                            fontFamily: "monospace",
                          }}
                        >
                          {JSON.stringify(event.data, null, 2)}
                        </pre>
                      </details>
                    )}
                  </Card>
                );
              })
                  ) : (
                    <Text type="secondary" style={{ textAlign: "center", padding: 20, display: "block" }}>
                      No events match the selected filter.
                    </Text>
                  )}
                  {filteredEvents.length > 100 && (
                    <Text type="secondary" style={{ display: "block", textAlign: "center", padding: 8 }}>
                      Showing first 100 of {filteredEvents.length} events
                    </Text>
                  )}
                </>
              ) : (
                <Text type="secondary" style={{ textAlign: "center", padding: 20, display: "block" }}>
                  No events
                </Text>
              )}
            </div>
          </div>
        );
      })(),
    },
  ];

  return (
    <div style={{ padding: 0 }}>
      {tasksPageHeader}

      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          flexWrap: "wrap",
          gap: 16,
          marginBottom: 16,
        }}
      >
        <div>
          <Button
            type="link"
            icon={<ArrowLeftOutlined />}
            onClick={handleBackToTasks}
            style={{ paddingLeft: 0, marginBottom: 12, height: "auto" }}
          >
            Back to Tasks
          </Button>
          <Title level={4} style={{ margin: 0 }}>
            Task ID: <Text code copyable={{ text: taskId }}>{taskId.slice(0, 16)}…</Text>
          </Title>
        </div>
        <Space size={12} wrap>
          {lastRefresh && !isRefreshing && (
            <Text type="secondary" style={{ fontSize: 11 }}>
              Last: {lastRefresh.toLocaleTimeString()}
            </Text>
          )}
          <Button
            size="small"
            icon={<ReloadOutlined spin={isRefreshing} />}
            onClick={() => fetchData(false)}
            disabled={isRefreshing}
          >
            Refresh
          </Button>
          <Button
            size="small"
            type={autoRefresh && !isTaskCompleted ? "primary" : "default"}
            onClick={() => setAutoRefresh(!autoRefresh)}
            disabled={isTaskCompleted}
          >
            Auto-refresh: {autoRefresh && !isTaskCompleted ? "ON" : "OFF"}
          </Button>
          {hasActiveCommand && (
            <Popconfirm
              title="Cancel this task?"
              description="The turn stops. An in-flight model call may finish the current generation."
              okText="Cancel task"
              cancelText="Keep running"
              okButtonProps={{ danger: true }}
              onConfirm={() => void handleCancelTask()}
            >
              <Button size="small" danger icon={<StopOutlined />} loading={cancelling}>
                Cancel task
              </Button>
            </Popconfirm>
          )}
          {isTaskCompleted && <Tag color="success">Task Completed</Tag>}
        </Space>
      </div>

      {/* Task Progress - floats above, click to scroll to top (matches task_flow_popup.html) */}
      {taskFlow && commands.length > 0 && (
        <div
          role="button"
          tabIndex={0}
          title="Click to scroll to top"
          onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              window.scrollTo({ top: 0, behavior: "smooth" });
            }
          }}
          style={{
            position: "fixed",
            top: 60,
            right: 15,
            zIndex: 1000,
            padding: 12,
            fontFamily: token.fontFamily,
            background: token.colorBgContainer,
            border: `1px solid ${token.colorBorderSecondary}`,
            borderRadius: token.borderRadius,
            boxShadow: token.boxShadowSecondary,
            cursor: "pointer",
          }}
        >
          <div
            style={{
              fontWeight: 600,
              marginBottom: 8,
              fontSize: 13,
              color: token.colorText,
            }}
          >
            Task Progress
          </div>
          {(() => {
            const completed = commands.filter((c) =>
              ["completed", "success"].includes((c.status || "").toLowerCase())
            ).length;
            const running = commands.filter((c) =>
              ["running", "executing", "pending"].includes((c.status || "").toLowerCase())
            ).length;
            const failed = commands.filter((c) =>
              ["failed", "error"].includes((c.status || "").toLowerCase())
            ).length;
            const total = commands.length;
            const percentage = total > 0 ? Math.round((completed / total) * 100) : 0;
            const barColor = failed > 0 ? token.colorError : token.colorSuccess;
            return (
              <>
                <div style={{ marginBottom: 4, fontSize: 12, color: token.colorText }}>
                  ✅ Completed: {completed}/{total} ({percentage}%)
                </div>
                {running > 0 && (
                  <div style={{ marginBottom: 4, fontSize: 12, color: token.colorText }}>
                    🟡 Running: {running}
                  </div>
                )}
                {failed > 0 && (
                  <div style={{ marginBottom: 4, fontSize: 12, color: token.colorText }}>
                    ❌ Failed: {failed}
                  </div>
                )}
                <div
                  style={{
                    width: "100%",
                    background: token.colorFillSecondary,
                    borderRadius: 3,
                    height: 6,
                    marginTop: 6,
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      width: `${percentage}%`,
                      background: barColor,
                      height: "100%",
                      borderRadius: 3,
                      transition: "width 0.3s ease",
                    }}
                  />
                </div>
              </>
            );
          })()}
        </div>
      )}

      <Card>
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} />
      </Card>
    </div>
  );
}
