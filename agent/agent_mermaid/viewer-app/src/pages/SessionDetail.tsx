import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeProps,
  type NodeTypes,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

const API_BASE = ''

interface GraphState {
  mermaid_source?: string
  skeleton?: string
  path?: string[]
  current_node?: string | null
  entry_node?: string
  nodes?: string[]
  edges?: [string, string, string | null][]
  node_id_to_shape?: Record<string, string>
}

interface SessionDetailData {
  session_id: string
  events: Array<{
    id: string
    ts: number
    tool: string
    params: Record<string, unknown>
    result_summary: string
  }>
  graph_state: Record<string, GraphState>
  frontmatter?: string
  node_prompts?: Record<string, string>
  rest_md?: string
}

function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false })
}

type SopNodeData = { label: string; shape?: string; visited?: boolean; current?: boolean }

function SopNode(props: NodeProps<Node<SopNodeData>>) {
  const { data } = props
  const shape = data?.shape ?? 'rectangle'
  const visited = data?.visited ?? false
  const current = data?.current ?? false
  const classes = ['react-flow-node', shape, visited && 'visited', current && 'current'].filter(Boolean).join(' ')
  return (
    <div className={classes}>
      <span className="label">{data?.label ?? ''}</span>
    </div>
  )
}

const nodeTypes: NodeTypes = { sop: SopNode as NodeTypes['sop'] }

// Simple layout: place nodes in a grid-like order by graph topology
function buildFlowNodesEdges(
  graph: GraphState,
): { nodes: Node<SopNodeData>[]; edges: Edge[] } {
  const nodes = graph.nodes ?? []
  const edgeList = graph.edges ?? []
  const path = new Set(graph.path ?? [])
  const current = graph.current_node ?? null
  const shapeMap = graph.node_id_to_shape ?? {}

  const nodeMap = new Map<string, { x: number; y: number }>()
  const level: Record<number, string[]> = {}
  const inDegree: Record<string, number> = {}
  nodes.forEach((n) => { inDegree[n] = 0 })
  edgeList.forEach(([, to]) => { inDegree[to] = (inDegree[to] ?? 0) + 1 })
  const queue: string[] = nodes.filter((n) => inDegree[n] === 0)
  let l = 0
  while (queue.length) {
    level[l] = [...queue]
    const next: string[] = []
    for (const u of queue) {
      for (const [, v] of edgeList.filter((e) => e[0] === u)) {
        inDegree[v]--
        if (inDegree[v] === 0) next.push(v)
      }
    }
    queue.length = 0
    queue.push(...next)
    l++
  }
  const maxLevel = Math.max(0, ...Object.keys(level).map(Number))
  const DX = 180
  const DY = 80
  for (let i = 0; i <= maxLevel; i++) {
    const row = level[i] ?? []
    row.forEach((id, j) => {
      nodeMap.set(id, { x: j * DX, y: i * DY })
    })
  }

  const flowNodes: Node<SopNodeData>[] = nodes.map((id) => {
    const pos = nodeMap.get(id) ?? { x: 0, y: 0 }
    return {
      id,
      type: 'sop',
      position: pos,
      data: {
        label: id,
        shape: shapeMap[id] ?? 'rectangle',
        visited: path.has(id),
        current: id === current,
      },
    }
  })

  const flowEdges: Edge[] = edgeList.map(([from, to, label], idx) => ({
    id: `e-${from}-${to}-${idx}`,
    source: from,
    target: to,
    label: label ?? undefined,
  }))

  return { nodes: flowNodes, edges: flowEdges }
}

export function SessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const [data, setData] = useState<SessionDetailData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<SopNodeData>>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  useEffect(() => {
    if (!sessionId) return
    fetch(`${API_BASE}/api/connections/${sessionId}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('Session not found'))))
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [sessionId])

  const graphState = useMemo(() => {
    if (!data?.graph_state) return null
    const first = Object.values(data.graph_state)[0] as GraphState | undefined
    return first
  }, [data?.graph_state])

  useEffect(() => {
    if (!graphState) return
    const { nodes: n, edges: e } = buildFlowNodesEdges(graphState)
    setNodes(n)
    setEdges(e)
  }, [graphState, setNodes, setEdges])

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node<SopNodeData>) => {
    setSelectedNodeId(node.id)
  }, [])

  const selectedPrompt = selectedNodeId && data?.node_prompts?.[selectedNodeId]

  const isLive = useMemo(() => {
    if (!data?.events?.length) return false
    const last = data.events[data.events.length - 1].ts
    return last > Date.now() / 1000 - 300
  }, [data?.events])

  if (loading) return <div className="page">Loading session…</div>
  if (error) return <div className="page"><p style={{ color: '#f87171' }}>{error}</p><Link to="/sessions">Back to Sessions</Link></div>
  if (!data) return null

  return (
    <div className="page" style={{ padding: 0, height: '100%', display: 'flex', flexDirection: 'column' }}>
      <header style={{ padding: '0.75rem 1.5rem', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: '1rem' }}>
        <Link to="/sessions" style={{ color: 'var(--primary)', textDecoration: 'none' }}>← Sessions</Link>
        <span className="font-mono" style={{ fontWeight: 600 }}>{sessionId?.slice(0, 16)}…</span>
        {isLive && <span className="badge badge-live">Live</span>}
      </header>
      <div className="split-view" style={{ flex: 1 }}>
        <div className="split-left">
          <div className="panel-header">Execution Logs</div>
          <div className="panel-body">
            {(data.events ?? []).map((e) => (
              <div key={e.id} className="trace-item">
                <div className="trace-meta">
                  <span className="trace-tool">{e.tool}</span>
                  <span style={{ color: 'var(--text-muted)', fontSize: '0.7rem' }}>{formatTs(e.ts)}</span>
                </div>
                <pre className="trace-params">{JSON.stringify(e.params, null, 2).slice(0, 400)}</pre>
                {e.result_summary && <div style={{ marginTop: 4, fontSize: '0.7rem', color: 'var(--text-muted)' }}>{e.result_summary.slice(0, 120)}</div>}
              </div>
            ))}
            {(!data.events || data.events.length === 0) && <p style={{ color: 'var(--text-muted)' }}>No tool calls yet.</p>}
          </div>
        </div>
        <div className="split-right">
          <div className="panel-header">Process Graph</div>
          <div className="flow-container">
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={onNodeClick}
              nodeTypes={nodeTypes}
              fitView
              fitViewOptions={{ padding: 0.2 }}
              minZoom={0.2}
              maxZoom={1.5}
            >
              <Background />
              <Controls />
              <MiniMap />
            </ReactFlow>
          </div>
        </div>
        {(selectedNodeId || data.frontmatter || data.rest_md) && (
          <div className="side-panel">
            <div className="side-panel-header">
              {selectedNodeId ? `Node: ${selectedNodeId}` : 'Agent context'}
            </div>
            <div className="side-panel-body">
              {selectedNodeId && (
                <>
                  <div style={{ marginBottom: 12, fontWeight: 600, fontSize: '0.7rem', textTransform: 'uppercase', color: 'var(--text-muted)' }}>Node prompt</div>
                  <div style={{ marginBottom: 16 }}>{selectedPrompt ?? '(No prompt for this node)'}</div>
                </>
              )}
              {data.frontmatter && (
                <>
                  <div style={{ marginBottom: 8, fontWeight: 600, fontSize: '0.7rem', textTransform: 'uppercase', color: 'var(--text-muted)' }}>Frontmatter</div>
                  <pre style={{ marginBottom: 16, whiteSpace: 'pre-wrap', fontSize: '0.75rem' }}>{data.frontmatter}</pre>
                </>
              )}
              {data.rest_md && (
                <>
                  <div style={{ marginBottom: 8, fontWeight: 600, fontSize: '0.7rem', textTransform: 'uppercase', color: 'var(--text-muted)' }}>Rest of markdown</div>
                  <pre style={{ whiteSpace: 'pre-wrap', fontSize: '0.75rem' }}>{data.rest_md}</pre>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
