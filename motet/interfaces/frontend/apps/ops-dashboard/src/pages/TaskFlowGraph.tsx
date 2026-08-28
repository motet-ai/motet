/**
 * Motet - Admin Dashboard - Task Flow React Flow Diagram
 *
 * Copyright (c) 2024-2026 Motet Contributors
 * Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.
 *
 * Author: Matt Chisholm <matt@motet.dev>
 * Last Modified: 2026-08-12
 *
 * Description:
 *     Interactive task-flow graph using React Flow (@xyflow/react) with dagre layout.
 *     Replaces Mermaid for large command trees: pan/zoom, minimap, collapse/expand,
 *     and click-to-inspect. Used by TaskFlowPage. Commands stamped with
 *     `agentic_loop_iteration` are clustered and wrapped in a labeled group box.
 *
 * Dependencies:
 * - @xyflow/react: interactive node/edge canvas
 * - @dagrejs/dagre: automatic LR tree layout
 * - taskFlowShared: command colors, labels, TaskFlowData types
 *
 * Usage:
 *     <TaskFlowGraph
 *       taskFlow={data}
 *       isDarkMode={false}
 *       onNodeSelect={(commandId) => goToCommand(commandId)}
 *     />
 *
 * Notes:
 * - Graphs with more than AUTO_COLLAPSE_THRESHOLD nodes start collapsed past depth 1.
 * - Click a node to jump to the Commands tab detail for that command_id.
 * - Pass isDarkMode from ThemeContext so the canvas follows the manage app theme.
 * - Agentic-loop rounds render as dashed "Iteration N" boxes behind child commands.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  memo,
  type MouseEvent,
} from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import dagre from "@dagrejs/dagre";
import { Button, Space, Typography, theme } from "antd";
import {
  baseCommandType,
  getCommandTypeColors,
  getCommandNodeLabelLines,
  getAgenticLoopIterationNumber,
  type TaskFlowCommand,
  type TaskFlowData,
} from "./taskFlowShared";

const { Text } = Typography;

/** Auto-collapse deeper levels when the full tree exceeds this size. */
const AUTO_COLLAPSE_THRESHOLD = 60;
const NODE_WIDTH = 200;
const NODE_HEIGHT = 88;
const ITER_GROUP_PAD_X = 16;
const ITER_GROUP_PAD_TOP = 28;
const ITER_GROUP_PAD_BOTTOM = 14;

export interface CommandFlowNodeData {
  commandId: string;
  commandType: string;
  labelLines: string[];
  fill: string;
  stroke: string;
  textColor: string;
  inProgress: boolean;
  hasChildren: boolean;
  isExpanded: boolean;
  hiddenDescendantCount: number;
  showExpandControls: boolean;
  iteration?: number;
  onToggleExpand?: (commandId: string) => void;
  [key: string]: unknown;
}

type CommandFlowNode = Node<CommandFlowNodeData, "command">;

export interface IterationGroupNodeData {
  iteration: number;
  label: string;
  fill: string;
  stroke: string;
  labelColor: string;
  [key: string]: unknown;
}

type IterationGroupNode = Node<IterationGroupNodeData, "iterationGroup">;
type TaskFlowRfNode = CommandFlowNode | IterationGroupNode;

function isCommandInProgress(cmd: TaskFlowCommand | null | undefined): boolean {
  if (!cmd?.status) return false;
  return ["running", "executing", "pending"].includes(String(cmd.status).toLowerCase());
}

function buildAdjacency(taskFlow: TaskFlowData): {
  nodes: Array<{ id: string; type: string; data: TaskFlowCommand | null }>;
  children: Map<string, string[]>;
  parents: Map<string, string>;
  roots: string[];
} {
  const { execution_flow, commands } = taskFlow;
  const nodes: Array<{ id: string; type: string; data: TaskFlowCommand | null }> = [];
  const cmdById = new Map((commands ?? []).map((c) => [c.command_id, c]));

  if (execution_flow?.nodes?.length) {
    execution_flow.nodes.forEach((node) => {
      nodes.push({ id: node.id, type: node.type, data: cmdById.get(node.id) ?? null });
    });
  } else if (commands?.length) {
    commands.forEach((cmd) => {
      nodes.push({ id: cmd.command_id, type: cmd.command_type, data: cmd });
    });
  }

  const children = new Map<string, string[]>();
  const parents = new Map<string, string>();
  const nodeIds = new Set(nodes.map((n) => n.id));

  if (execution_flow?.edges?.length) {
    execution_flow.edges.forEach((edge) => {
      if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) return;
      if (!children.has(edge.source)) children.set(edge.source, []);
      children.get(edge.source)!.push(edge.target);
      parents.set(edge.target, edge.source);
    });
  } else {
    const sorted = [...nodes].sort((a, b) => {
      const timeA = a.data?.created_at ? new Date(a.data.created_at).getTime() : 0;
      const timeB = b.data?.created_at ? new Date(b.data.created_at).getTime() : 0;
      return timeA - timeB;
    });
    for (let i = 0; i < sorted.length - 1; i++) {
      const source = sorted[i].id;
      const target = sorted[i + 1].id;
      if (!children.has(source)) children.set(source, []);
      children.get(source)!.push(target);
      parents.set(target, source);
    }
  }

  const roots = nodes.map((n) => n.id).filter((id) => !parents.has(id));

  children.forEach((kids) => {
    kids.sort((a, b) => {
      const cmdA = cmdById.get(a);
      const cmdB = cmdById.get(b);
      const ia = getAgenticLoopIterationNumber(cmdA) ?? Number.MAX_SAFE_INTEGER;
      const ib = getAgenticLoopIterationNumber(cmdB) ?? Number.MAX_SAFE_INTEGER;
      if (ia !== ib) return ia - ib;
      const ta = cmdA?.created_at ?? "";
      const tb = cmdB?.created_at ?? "";
      if (ta !== tb) return ta.localeCompare(tb);
      return a.localeCompare(b);
    });
  });

  return { nodes, children, parents, roots };
}

function countHiddenDescendants(
  rootId: string,
  children: Map<string, string[]>,
  visible: Set<string>
): number {
  let count = 0;
  const stack = [...(children.get(rootId) ?? [])];
  while (stack.length) {
    const id = stack.pop()!;
    if (visible.has(id)) continue;
    count += 1;
    const kids = children.get(id);
    if (kids) stack.push(...kids);
  }
  return count;
}

function initialExpandedIds(
  roots: string[],
  children: Map<string, string[]>,
  totalNodes: number
): Set<string> {
  const expanded = new Set<string>();
  if (totalNodes <= AUTO_COLLAPSE_THRESHOLD) {
    children.forEach((_kids, id) => expanded.add(id));
    return expanded;
  }
  for (const root of roots) {
    expanded.add(root);
  }
  return expanded;
}

function computeVisibleIds(
  roots: string[],
  children: Map<string, string[]>,
  expanded: Set<string>
): Set<string> {
  const visible = new Set<string>();
  const queue = [...roots];
  while (queue.length) {
    const id = queue.shift()!;
    if (visible.has(id)) continue;
    visible.add(id);
    if (!expanded.has(id)) continue;
    for (const child of children.get(id) ?? []) {
      queue.push(child);
    }
  }
  return visible;
}

function iterationGroupNodeId(iteration: number): string {
  return `iter-group-${iteration}`;
}

function layoutWithDagre(nodes: CommandFlowNode[], edges: Edge[]): CommandFlowNode[] {
  const useGroups = nodes.some((n) => n.data.iteration != null);
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: "LR",
    nodesep: useGroups ? 52 : 36,
    ranksep: 80,
    marginx: 24,
    marginy: 32,
  });

  nodes.forEach((node) => {
    g.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  });
  edges.forEach((edge) => {
    g.setEdge(edge.source, edge.target);
  });
  dagre.layout(g);

  return nodes.map((node) => {
    const pos = g.node(node.id);
    return {
      ...node,
      position: {
        x: (pos?.x ?? 0) - NODE_WIDTH / 2,
        y: (pos?.y ?? 0) - NODE_HEIGHT / 2,
      },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    };
  });
}

function iterationGroupColors(isDarkMode: boolean): {
  fill: string;
  stroke: string;
  labelColor: string;
} {
  return isDarkMode
    ? { fill: "rgba(77, 171, 247, 0.10)", stroke: "#74c0fc", labelColor: "#a5d8ff" }
    : { fill: "rgba(13, 110, 253, 0.06)", stroke: "#0d6efd", labelColor: "#0a58ca" };
}

function buildIterationGroupNodes(
  commandNodes: CommandFlowNode[],
  isDarkMode: boolean
): IterationGroupNode[] {
  const byIter = new Map<number, CommandFlowNode[]>();
  for (const node of commandNodes) {
    const iter = node.data.iteration;
    if (iter == null) continue;
    const list = byIter.get(iter) ?? [];
    list.push(node);
    byIter.set(iter, list);
  }
  const colors = iterationGroupColors(isDarkMode);
  const groups: IterationGroupNode[] = [];
  for (const iter of [...byIter.keys()].sort((a, b) => a - b)) {
    const members = byIter.get(iter) ?? [];
    if (members.length === 0) continue;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of members) {
      minX = Math.min(minX, node.position.x);
      minY = Math.min(minY, node.position.y);
      maxX = Math.max(maxX, node.position.x + NODE_WIDTH);
      maxY = Math.max(maxY, node.position.y + NODE_HEIGHT);
    }
    if (!Number.isFinite(minX) || !Number.isFinite(minY)) continue;
    const width = maxX - minX + ITER_GROUP_PAD_X * 2;
    const height = maxY - minY + ITER_GROUP_PAD_TOP + ITER_GROUP_PAD_BOTTOM;
    groups.push({
      id: iterationGroupNodeId(iter),
      type: "iterationGroup",
      position: {
        x: minX - ITER_GROUP_PAD_X,
        y: minY - ITER_GROUP_PAD_TOP,
      },
      style: { width, height },
      data: {
        iteration: iter,
        label: `Iteration ${iter}`,
        fill: colors.fill,
        stroke: colors.stroke,
        labelColor: colors.labelColor,
      },
      selectable: false,
      draggable: false,
      connectable: false,
      focusable: false,
      zIndex: -1,
    });
  }
  return groups;
}

const CommandNode = memo(function CommandNode({ data }: NodeProps<CommandFlowNode>) {
  return (
    <div
      className={
        data.inProgress ? "task-flow-rf-node task-flow-rf-node--in-progress" : "task-flow-rf-node"
      }
      style={{
        width: NODE_WIDTH,
        minHeight: NODE_HEIGHT - 8,
        background: data.fill,
        border: `2px solid ${data.stroke}`,
        borderStyle: data.inProgress ? "dashed" : "solid",
        color: data.textColor,
        borderRadius: 6,
        padding: "8px 10px",
        fontSize: 11,
        lineHeight: 1.25,
        boxShadow: "0 1px 3px rgba(0,0,0,0.12)",
        cursor: "pointer",
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0.5 }} />
      <div style={{ fontWeight: 600, marginBottom: 2, wordBreak: "break-word" }}>
        {data.labelLines[0] ?? data.commandType}
      </div>
      {data.labelLines.slice(1).map((line, i) => (
        <div key={i} style={{ opacity: 0.92, wordBreak: "break-word" }}>
          {line}
        </div>
      ))}
      {data.showExpandControls && data.hasChildren && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            data.onToggleExpand?.(data.commandId);
          }}
          style={{
            marginTop: 6,
            fontSize: 10,
            padding: "1px 6px",
            borderRadius: 4,
            border: `1px solid ${data.textColor}`,
            background: "transparent",
            color: data.textColor,
            cursor: "pointer",
            opacity: 0.95,
          }}
        >
          {data.isExpanded
            ? "Collapse"
            : `Expand${data.hiddenDescendantCount > 0 ? ` (+${data.hiddenDescendantCount})` : ""}`}
        </button>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0.5 }} />
    </div>
  );
});

const IterationGroupNode = memo(function IterationGroupNode({
  data,
}: NodeProps<IterationGroupNode>) {
  return (
    <div
      className="task-flow-rf-iter-group"
      style={{
        width: "100%",
        height: "100%",
        boxSizing: "border-box",
        border: `1.5px dashed ${data.stroke}`,
        borderRadius: 10,
        background: data.fill,
        padding: "6px 10px",
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          color: data.labelColor,
          letterSpacing: 0.02,
          lineHeight: 1.2,
        }}
      >
        {data.label}
      </div>
    </div>
  );
});

const nodeTypes = { command: CommandNode, iterationGroup: IterationGroupNode };

interface TaskFlowGraphInnerProps {
  taskFlow: TaskFlowData;
  isDarkMode: boolean;
  onNodeSelect?: (commandId: string) => void;
  fillHeight?: boolean;
}

function TaskFlowGraphInner({
  taskFlow,
  isDarkMode,
  onNodeSelect,
  fillHeight = false,
}: TaskFlowGraphInnerProps) {
  const { token } = theme.useToken();
  const { fitView } = useReactFlow();
  const colorPalette = getCommandTypeColors(isDarkMode);

  const adjacency = useMemo(() => buildAdjacency(taskFlow), [taskFlow]);
  const totalNodes = adjacency.nodes.length;

  const structureKey = useMemo(() => {
    const ids = adjacency.nodes
      .map((n) => n.id)
      .sort()
      .join(",");
    const edgeParts: string[] = [];
    adjacency.children.forEach((kids, src) => {
      kids.forEach((t) => edgeParts.push(`${src}->${t}`));
    });
    return `${ids}|${edgeParts.sort().join(",")}`;
  }, [adjacency]);

  const [expandedSet, setExpandedSet] = useState<Set<string>>(() =>
    initialExpandedIds(adjacency.roots, adjacency.children, totalNodes)
  );
  const [layoutTick, setLayoutTick] = useState(0);
  const prevLayoutKey = useRef<string>("");

  useEffect(() => {
    setExpandedSet(initialExpandedIds(adjacency.roots, adjacency.children, totalNodes));
    setLayoutTick((t) => t + 1);
  }, [structureKey, adjacency.roots, adjacency.children, totalNodes]);

  const toggleExpand = useCallback((commandId: string) => {
    setExpandedSet((prev) => {
      const next = new Set(prev);
      if (next.has(commandId)) next.delete(commandId);
      else next.add(commandId);
      return next;
    });
    setLayoutTick((t) => t + 1);
  }, []);

  const expandAll = useCallback(() => {
    const next = new Set<string>();
    adjacency.children.forEach((_kids, id) => next.add(id));
    setExpandedSet(next);
    setLayoutTick((t) => t + 1);
  }, [adjacency.children]);

  const collapseDeep = useCallback(() => {
    setExpandedSet(
      initialExpandedIds(
        adjacency.roots,
        adjacency.children,
        Math.max(totalNodes, AUTO_COLLAPSE_THRESHOLD + 1)
      )
    );
    setLayoutTick((t) => t + 1);
  }, [adjacency.roots, adjacency.children, totalNodes]);

  const [nodes, setNodes, onNodesChange] = useNodesState<TaskFlowRfNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    const { nodes: rawNodes, children, roots } = adjacency;
    const visible = computeVisibleIds(roots, children, expandedSet);
    const nodeById = new Map(rawNodes.map((n) => [n.id, n]));

    const showExpandControls =
      totalNodes > AUTO_COLLAPSE_THRESHOLD || visible.size < totalNodes;

    const flowNodes: CommandFlowNode[] = [...visible].map((id) => {
      const raw = nodeById.get(id)!;
      const cmd = raw.data;
      const baseType = baseCommandType(raw.type);
      const colors =
        colorPalette[baseType] ??
        (isDarkMode
          ? { fill: "#868e96", stroke: "#adb5bd", color: "#000" }
          : { fill: "#6c757d", stroke: "#545b62", color: "#fff" });
      const kids = children.get(id) ?? [];
      const isExpanded = expandedSet.has(id);
      const hiddenDescendantCount = isExpanded
        ? 0
        : countHiddenDescendants(id, children, visible);

      return {
        id,
        type: "command" as const,
        position: { x: 0, y: 0 },
        zIndex: 1,
        data: {
          commandId: id,
          commandType: raw.type,
          labelLines: getCommandNodeLabelLines(raw.type, cmd),
          fill: colors.fill,
          stroke: colors.stroke,
          textColor: colors.color,
          inProgress: isCommandInProgress(cmd),
          hasChildren: kids.length > 0,
          isExpanded,
          hiddenDescendantCount,
          showExpandControls,
          iteration: getAgenticLoopIterationNumber(cmd),
          onToggleExpand: toggleExpand,
        },
      };
    });

    const flowEdges: Edge[] = [];
    children.forEach((kids, source) => {
      if (!visible.has(source) || !expandedSet.has(source)) return;
      kids.forEach((target) => {
        if (!visible.has(target)) return;
        flowEdges.push({
          id: `${source}->${target}`,
          source,
          target,
          animated: isCommandInProgress(nodeById.get(target)?.data ?? null),
          style: { stroke: isDarkMode ? "#8c8c8c" : "#8c8c8c", strokeWidth: 1.5 },
        });
      });
    });

    const layoutKey = `${[...visible].sort().join(",")}|${flowEdges
      .map((e) => e.id)
      .sort()
      .join(",")}|${layoutTick}`;
    const laidOut = layoutWithDagre(flowNodes, flowEdges);
    const withGroups = (commandNodes: CommandFlowNode[]): TaskFlowRfNode[] => [
      ...buildIterationGroupNodes(commandNodes, isDarkMode),
      ...commandNodes,
    ];

    if (layoutKey === prevLayoutKey.current) {
      setNodes((prev) => {
        const prevById = new Map(prev.map((n) => [n.id, n]));
        const commands = laidOut.map((n) => {
          const old = prevById.get(n.id);
          return old && old.type === "command" ? { ...n, position: old.position } : n;
        });
        return withGroups(commands);
      });
      setEdges(flowEdges);
    } else {
      prevLayoutKey.current = layoutKey;
      setNodes(withGroups(laidOut));
      setEdges(flowEdges);
      requestAnimationFrame(() => {
        fitView({ padding: 0.12, duration: 200 });
      });
    }
  }, [
    adjacency,
    colorPalette,
    expandedSet,
    fitView,
    isDarkMode,
    layoutTick,
    setEdges,
    setNodes,
    toggleExpand,
    taskFlow,
    totalNodes,
  ]);

  const onNodeClick = useCallback(
    (_: MouseEvent, node: TaskFlowRfNode) => {
      if (node.type !== "command") return;
      onNodeSelect?.(node.data.commandId);
    },
    [onNodeSelect]
  );

  const heightStyle = fillHeight
    ? { height: "100%", minHeight: 360 }
    : { height: 520, minHeight: 360 };

  if (totalNodes === 0) {
    return <Text type="secondary">No commands available</Text>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, ...heightStyle }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          flexWrap: "wrap",
          flexShrink: 0,
        }}
      >
        <Space size={8} wrap>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {totalNodes} command{totalNodes === 1 ? "" : "s"}
            {totalNodes > AUTO_COLLAPSE_THRESHOLD
              ? " · large graph (collapsed past depth 1)"
              : ""}
          </Text>
          {totalNodes > AUTO_COLLAPSE_THRESHOLD && (
            <>
              <Button size="small" onClick={expandAll}>
                Expand all
              </Button>
              <Button size="small" onClick={collapseDeep}>
                Collapse deep
              </Button>
            </>
          )}
        </Space>
        <Text type="secondary" style={{ fontSize: 11 }}>
          Click a node to open command details · drag to pan · scroll to zoom
        </Text>
      </div>
      <div
        className={isDarkMode ? "task-flow-rf task-flow-rf--dark" : "task-flow-rf"}
        style={{
          flex: 1,
          minHeight: 0,
          border: `1px solid ${token.colorBorderSecondary}`,
          borderRadius: token.borderRadius,
          background: isDarkMode ? token.colorBgContainer : "#fafafa",
        }}
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          onNodeClick={onNodeClick}
          fitView
          minZoom={0.1}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
          colorMode={isDarkMode ? "dark" : "light"}
        >
          <Background gap={16} size={1} color={isDarkMode ? "#3a424a" : "#e8e8e8"} />
          <Controls showInteractive={false} />
          <MiniMap
            pannable
            zoomable
            nodeStrokeWidth={2}
            nodeColor={(n) => {
              if (n.type === "iterationGroup") {
                return isDarkMode ? "#1c3a5a" : "#d0e4ff";
              }
              return (n.data as CommandFlowNodeData | undefined)?.fill ?? "#888";
            }}
            maskColor={isDarkMode ? "rgba(0,0,0,0.55)" : "rgba(0,0,0,0.08)"}
          />
        </ReactFlow>
      </div>
    </div>
  );
}

export interface TaskFlowGraphProps {
  taskFlow: TaskFlowData;
  isDarkMode: boolean;
  onNodeSelect?: (commandId: string) => void;
  fillHeight?: boolean;
}

export function TaskFlowGraph(props: TaskFlowGraphProps) {
  return (
    <ReactFlowProvider>
      <TaskFlowGraphInner {...props} />
    </ReactFlowProvider>
  );
}
