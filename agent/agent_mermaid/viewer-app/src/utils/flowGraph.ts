/**
 * Shared types and helpers for rendering graph_json (from mermaid) as React Flow.
 * Used by Agents page (editable) and Session detail (view-only).
 */

import type { Node, Edge } from '@xyflow/react'
import { Position } from '@xyflow/react'
import { layoutWithDagre } from './autoLayout'
import type { DagreLayoutOptions } from './autoLayout'

export interface GraphNodeJson {
  id: string
  label: string
  shape: 'rectangle' | 'stadium' | 'rhombus'
  node_type?: 'terminal' | 'normal' | 'decision'
  x: number
  y: number
}

export interface GraphEdgeJson {
  source: string
  target: string
  label?: string | null
}

export interface GraphJson {
  nodes: GraphNodeJson[]
  edges: GraphEdgeJson[]
}

export interface FlowNodeData extends Record<string, unknown> {
  label: string
  shape?: 'rectangle' | 'stadium' | 'rhombus'
  nodeType?: 'terminal' | 'normal' | 'decision'
  visited?: boolean
  current?: boolean
}

const DEFAULT_DAGRE: DagreLayoutOptions = {
  rankdir: 'LR',
  ranksep: 100,
  nodesep: 100,
  edgesep: 100,
  marginx: 0,
  marginy: 0,
}

export function graphJsonToFlow(
  graph: GraphJson,
  options?: { sourcePosition?: Position; targetPosition?: Position; includeHandles?: boolean }
): { nodes: Node<FlowNodeData>[]; edges: Edge[] } {
  const sourcePosition = options?.sourcePosition ?? Position.Right
  const targetPosition = options?.targetPosition ?? Position.Left
  const nodes: Node<FlowNodeData>[] = (graph.nodes || []).map((n) => {
    const nodeType = n.node_type ?? (n.shape === 'stadium' ? 'terminal' : n.shape === 'rhombus' ? 'decision' : 'normal')
    return {
      id: n.id,
      type: nodeType as 'terminal' | 'normal' | 'decision',
      position: { x: n.x ?? 0, y: n.y ?? 0 },
      data: {
        label: n.label ?? n.id,
        shape: n.shape ?? 'rectangle',
        nodeType,
      } as FlowNodeData,
    }
  })
  const edges: Edge[] = (graph.edges || []).map((e, i) => ({
    id: `e-${e.source}-${e.target}-${i}`,
    source: e.source,
    target: e.target,
    sourcePosition,
    targetPosition,
    label: e.label ?? undefined,
    type: 'straight',
    style: { stroke: 'hsl(215 25% 72%)', strokeWidth: 2 },
    labelStyle: { fill: 'hsl(215 20% 85%)', fontSize: 10 },
    labelBgStyle: { fill: 'hsl(215 30% 18%)', fillOpacity: 0.95 },
    labelBgBorderRadius: 4,
    labelBgPadding: [4, 6] as [number, number],
  }))
  return { nodes, edges }
}

/** Apply Dagre layout to nodes+edges from graphJsonToFlow. Returns new nodes with positions. */
export function layoutFlowGraph<NodeData extends FlowNodeData>(
  nodes: Node<NodeData>[],
  edges: Edge[],
  dagreOptions?: DagreLayoutOptions
): Node<NodeData>[] {
  return layoutWithDagre(nodes, edges, { dagre: dagreOptions ?? DEFAULT_DAGRE })
}
