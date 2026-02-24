/**
 * Auto-layout utilities for React Flow using dagre, d3-hierarchy, and elk.
 * Each function returns nodes with updated positions (same ids/data, new position).
 */

import type { Node, Edge } from '@xyflow/react'
import * as dagre from 'dagre'
import { hierarchy, tree } from 'd3-hierarchy'
import ELK from 'elkjs/lib/elk.bundled.js'

const DEFAULT_NODE_WIDTH = 180
const DEFAULT_NODE_HEIGHT = 44

export type LayoutAlgorithm = 'dagre' | 'd3' | 'elk'

/** Dagre-specific options (see https://github.com/dagrejs/dagre) */
export type DagreRankDir = 'TB' | 'BT' | 'LR' | 'RL'

export interface DagreLayoutOptions {
  /** Flow direction: TB = top-to-bottom, BT = bottom-to-top, LR = left-to-right, RL = right-to-left */
  rankdir?: DagreRankDir
  /** Vertical spacing between ranks (layers) */
  ranksep?: number
  /** Horizontal spacing between nodes in the same rank */
  nodesep?: number
  /** Spacing between edges */
  edgesep?: number
  /** Horizontal margin around the graph */
  marginx?: number
  /** Vertical margin around the graph */
  marginy?: number
}

export interface LayoutOptions {
  nodeWidth?: number
  nodeHeight?: number
  rankSep?: number
  nodeSep?: number
  /** Used by d3 and elk for tree size */
  width?: number
  height?: number
  /** Dagre-only options (used when layoutWithDagre is called) */
  dagre?: DagreLayoutOptions
}

const defaultOptions: Omit<Required<LayoutOptions>, 'dagre'> & { dagre?: DagreLayoutOptions } = {
  nodeWidth: DEFAULT_NODE_WIDTH,
  nodeHeight: DEFAULT_NODE_HEIGHT,
  rankSep: 60,
  nodeSep: 50,
  width: 800,
  height: 600,
}

function withDefaults(opts?: LayoutOptions): Omit<Required<LayoutOptions>, 'dagre'> & { dagre?: DagreLayoutOptions } {
  return { ...defaultOptions, ...opts }
}

/** Dagre node type expects width/height and gets x,y after layout */
interface DagreNode { width: number; height: number; x?: number; y?: number }

/**
 * Dagre: layered (Sugiyama-style) layout. Good for DAGs and flowcharts.
 * Options: rankdir, ranksep, nodesep, edgesep, marginx, marginy.
 */
export function layoutWithDagre<NodeData extends Record<string, unknown>>(
  nodes: Node<NodeData>[],
  edges: Edge[],
  opts?: LayoutOptions
): Node<NodeData>[] {
  const base = withDefaults(opts)
  const d = opts?.dagre ?? {}
  const rankdir = (d.rankdir ?? 'LR') as 'TB' | 'BT' | 'LR' | 'RL'
  const ranksep = d.ranksep ?? base.rankSep
  const nodesep = d.nodesep ?? base.nodeSep
  const edgesep = d.edgesep ?? 100
  const marginx = d.marginx ?? 0
  const marginy = d.marginy ?? 0
  const { nodeWidth, nodeHeight } = base

  const g = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}))
  g.setGraph({
    rankdir,
    ranksep,
    nodesep,
    edgesep,
    marginx,
    marginy,
  })

  nodes.forEach((node) => {
    g.setNode(node.id, { width: nodeWidth, height: nodeHeight })
  })
  edges.forEach((e) => {
    g.setEdge(e.source, e.target)
  })

  dagre.layout(g)

  return nodes.map((node) => {
    const d = g.node(node.id) as DagreNode | undefined
    if (!d || d.x == null || d.y == null) return node
    return {
      ...node,
      position: { x: d.x - nodeWidth / 2, y: d.y - nodeHeight / 2 },
    }
  })
}

/**
 * D3 hierarchy tree: single-root tree layout. Picks a root (node with no incoming edges, or START).
 */
export function layoutWithD3<NodeData extends Record<string, unknown>>(
  nodes: Node<NodeData>[],
  edges: Edge[],
  opts?: LayoutOptions
): Node<NodeData>[] {
  const { nodeWidth, nodeHeight, width, height } = withDefaults(opts)
  const inDegree = new Map<string, number>()
  const children = new Map<string, string[]>()
  nodes.forEach((n) => {
    inDegree.set(n.id, 0)
    children.set(n.id, [])
  })
  edges.forEach((e) => {
    inDegree.set(e.target, (inDegree.get(e.target) ?? 0) + 1)
    children.get(e.source)!.push(e.target)
  })

  const roots = nodes.filter((n) => inDegree.get(n.id) === 0).map((n) => n.id)
  const rootId = roots.includes('START') ? 'START' : roots[0]
  if (rootId == null) return nodes

  type TreeDatum = { id: string; children: TreeDatum[] }
  function buildHierarchy(id: string): TreeDatum {
    return {
      id,
      children: (children.get(id) ?? []).map(buildHierarchy),
    }
  }
  const rootData = buildHierarchy(rootId)
  const root = hierarchy(rootData, (d) => d.children)
  const treeLayout = tree<TreeDatum>()
    .size([width, height])
    .nodeSize([nodeWidth + 40, nodeHeight + 50])
  treeLayout(root)

  const posById = new Map<string, { x: number; y: number }>()
  root.each((d) => {
    const id = (d.data as TreeDatum).id
    posById.set(id, { x: d.x ?? 0, y: d.y ?? 0 })
  })

  return nodes.map((node) => {
    const p = posById.get(node.id)
    if (p == null) return node
    return {
      ...node,
      position: { x: p.x - nodeWidth / 2, y: p.y - nodeHeight / 2 },
    }
  })
}

/**
 * ELK: layered layout with more options. Returns a Promise.
 */
export async function layoutWithElk<NodeData extends Record<string, unknown>>(
  nodes: Node<NodeData>[],
  edges: Edge[],
  opts?: LayoutOptions
): Promise<Node<NodeData>[]> {
  const { nodeWidth, nodeHeight } = withDefaults(opts)
  const elk = new ELK()

  const elkGraph = {
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'DOWN',
      'elk.spacing.nodeNode': '60',
      'elk.layered.spacing.nodeNodeBetweenLayers': '60',
    },
    children: nodes.map((n) => ({
      id: n.id,
      width: nodeWidth,
      height: nodeHeight,
    })),
    edges: edges.map((e, i) => ({
      id: `e${i}`,
      sources: [e.source],
      targets: [e.target],
    })),
  }

  const laid = await elk.layout(elkGraph)
  const posById = new Map<string, { x: number; y: number }>()
  ;(laid.children ?? []).forEach((c) => {
    if (c.x != null && c.y != null) posById.set(c.id, { x: c.x, y: c.y })
  })

  return nodes.map((node) => {
    const p = posById.get(node.id)
    if (p == null) return node
    return {
      ...node,
      position: { x: p.x, y: p.y },
    }
  })
}
