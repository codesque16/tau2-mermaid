import { useCallback, useEffect, useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeProps,
  type NodeTypes,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { FlowNodeData, GraphJson } from '../utils/flowGraph'
import { graphJsonToFlow, layoutFlowGraph } from '../utils/flowGraph'

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
  graph_json?: GraphJson
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

const activeNodeStyles = 'ring-2 ring-cyan-400 shadow-[0_0_12px_rgba(34,211,238,0.5)]'
const visitedNodeStyles = 'ring-1 ring-offset-1 ring-offset-slate-900'

function ViewTerminalNode(props: NodeProps<Node<FlowNodeData>>) {
  const { data } = props
  const visited = (data?.visited) ?? false
  const current = (data?.current) ?? false
  return (
    <div className={`relative px-3 py-2 rounded-lg text-xs font-medium border bg-emerald-900/80 border-emerald-500/70 text-emerald-100 ${visited ? `${visitedNodeStyles} ring-emerald-400/50` : ''} ${current ? activeNodeStyles : ''}`}>
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !border-2 !bg-emerald-900 !border-emerald-500" />
      {data?.label ?? props.id}
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !border-2 !bg-emerald-900 !border-emerald-500" />
    </div>
  )
}

function ViewNormalNode(props: NodeProps<Node<FlowNodeData>>) {
  const { data } = props
  const visited = (data?.visited) ?? false
  const current = (data?.current) ?? false
  return (
    <div className={`relative px-3 py-2 rounded-lg text-xs font-medium border bg-slate-800 border-slate-600 text-slate-100 ${visited ? `${visitedNodeStyles} ring-slate-400/50` : ''} ${current ? activeNodeStyles : ''}`}>
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !border-2 !bg-slate-800 !border-slate-500" />
      {data?.label ?? props.id}
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !border-2 !bg-slate-800 !border-slate-500" />
    </div>
  )
}

function ViewDecisionNode(props: NodeProps<Node<FlowNodeData>>) {
  const { data } = props
  const visited = (data?.visited) ?? false
  const current = (data?.current) ?? false
  return (
    <div className={`relative px-3 py-2 rounded-lg text-xs font-medium border bg-amber-900/70 border-amber-500/60 text-amber-100 ${visited ? `${visitedNodeStyles} ring-amber-400/50` : ''} ${current ? activeNodeStyles : ''}`}>
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !border-2 !bg-amber-900 !border-amber-500" />
      {data?.label ?? props.id}
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !border-2 !bg-amber-900 !border-amber-500" />
    </div>
  )
}

const viewOnlyNodeTypes: NodeTypes = {
  terminal: ViewTerminalNode as NodeTypes['terminal'],
  normal: ViewNormalNode as NodeTypes['normal'],
  decision: ViewDecisionNode as NodeTypes['decision'],
}

/** Build path edge keys (source->target) for consecutive nodes in the trace. */
function pathEdgeSet(path: string[]): Set<string> {
  const set = new Set<string>()
  for (let i = 0; i < path.length - 1; i++) {
    set.add(`${path[i]}-${path[i + 1]}`)
  }
  return set
}

/** Build flow nodes/edges from session graph_state; highlight visited, current node, and path edges. */
function buildFlowFromGraphState(graph: GraphState): { nodes: Node<FlowNodeData>[]; edges: Edge[] } {
  const graphJson = graph.graph_json
  if (!graphJson?.nodes?.length) {
    return { nodes: [], edges: [] }
  }
  const path = graph.path ?? []
  const pathSet = new Set(path)
  const onPathEdges = pathEdgeSet(path)
  const current = graph.current_node ?? null
  const { nodes, edges } = graphJsonToFlow(graphJson)
  const withHighlights: Node<FlowNodeData>[] = nodes.map((n) => ({
    ...n,
    data: {
      ...n.data,
      visited: pathSet.has(n.id),
      current: n.id === current,
    } as FlowNodeData,
  }))
  const laidOut = layoutFlowGraph(withHighlights, edges)
  const edgesWithPathStyle: Edge[] = edges.map((e) => {
    const onPath = onPathEdges.has(`${e.source}-${e.target}`)
    return {
      ...e,
      style: onPath
        ? { stroke: 'hsl(187 85% 43%)', strokeWidth: 3 }
        : { stroke: 'hsl(215 25% 72%)', strokeWidth: 2 },
    }
  })
  return { nodes: laidOut, edges: edgesWithPathStyle }
}

export function SessionDetail() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const [data, setData] = useState<SessionDetailData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [panelOpen, setPanelOpen] = useState(false)
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  const fetchSession = useCallback(() => {
    if (!sessionId) return
    fetch(`${API_BASE}/api/connections/${sessionId}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('Session not found'))))
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [sessionId])

  useEffect(() => {
    fetchSession()
  }, [fetchSession])

  // Live session: poll for updates so node trace and logs stay in sync
  useEffect(() => {
    let interval: ReturnType<typeof setInterval> | undefined
    if (sessionId && data?.events?.length) {
      const lastTs = data.events[data.events.length - 1].ts
      const isLive = lastTs > Date.now() / 1000 - 300
      if (isLive) interval = setInterval(fetchSession, 2000)
    }
    return () => {
      if (interval) clearInterval(interval)
    }
  }, [sessionId, data?.events?.length, fetchSession])

  const graphState = useMemo(() => {
    if (!data?.graph_state) return null
    return Object.values(data.graph_state)[0] as GraphState | undefined
  }, [data?.graph_state])

  useEffect(() => {
    if (!graphState) {
      setNodes([])
      setEdges([])
      return
    }
    const { nodes: n, edges: e } = buildFlowFromGraphState(graphState)
    setNodes(n)
    setEdges(e)
  }, [graphState, setNodes, setEdges])

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node<FlowNodeData>) => {
    setSelectedNodeId(node.id)
    setPanelOpen(true)
  }, [])

  const selectedPrompt = selectedNodeId && data?.node_prompts?.[selectedNodeId]

  const isLive = useMemo(() => {
    if (!data?.events?.length) return false
    const last = data.events[data.events.length - 1].ts
    return last > Date.now() / 1000 - 300
  }, [data?.events])

  if (loading) return <div className="flex flex-1 items-center justify-center text-slate-500">Loading session…</div>
  if (error) return <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6"><p className="text-red-400">{error}</p><Link to="/sessions" className="text-slate-400 hover:text-slate-200">Back to Sessions</Link></div>
  if (!data) return null

  const showPanel = panelOpen && (selectedNodeId || data.frontmatter || data.rest_md)

  return (
    <div className="flex flex-1 min-h-0 flex-col bg-slate-950">
      <header className="flex items-center gap-4 border-b border-slate-700/80 bg-slate-900/50 px-4 py-3">
        <Link to="/sessions" className="text-sm font-medium text-slate-400 hover:text-slate-200 transition-colors">← Sessions</Link>
        <span className="font-mono text-sm font-semibold text-slate-200">{sessionId?.slice(0, 16)}…</span>
        {isLive && <span className="rounded-md bg-emerald-500/20 px-2 py-0.5 text-xs font-medium text-emerald-400">Live</span>}
      </header>
      <div className="flex flex-1 min-h-0">
        <div className="flex w-[42%] min-w-[280px] flex-col border-r border-slate-700/80 bg-slate-900/60">
          <div className="border-b border-slate-700/80 px-4 py-2 text-xs font-semibold uppercase tracking-wider text-slate-500">Execution Logs</div>
          <div className="flex-1 overflow-auto p-4 font-mono text-[0.8125rem]">
            {(data.events ?? []).map((e) => (
              <div key={e.id} className="mb-4 border-b border-slate-700/60 pb-3 last:border-0">
                <div className="mb-1 flex items-center gap-2">
                  <span className="font-semibold text-slate-200">{e.tool}</span>
                  <span className="text-[0.7rem] text-slate-600">{formatTs(e.ts)}</span>
                </div>
                <pre className="whitespace-pre-wrap break-all text-[0.75rem] text-slate-600">{JSON.stringify(e.params, null, 2).slice(0, 400)}</pre>
                {e.result_summary && <div className="mt-1 text-[0.7rem] text-slate-500">{e.result_summary.slice(0, 120)}</div>}
              </div>
            ))}
            {(!data.events || data.events.length === 0) && <p className="text-slate-500">No tool calls yet.</p>}
          </div>
        </div>
        <div className="flex flex-1 flex-col min-w-0 border-r border-slate-700/80 bg-slate-900/60">
          <div className="border-b border-slate-700/80 px-4 py-2 text-xs font-semibold uppercase tracking-wider text-slate-500">Process Graph</div>
          <div className="flex-1 min-h-[300px]">
            <ReactFlow
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={onNodeClick}
              nodeTypes={viewOnlyNodeTypes}
              defaultEdgeOptions={{
                type: 'straight',
                style: { stroke: 'hsl(215 25% 72%)', strokeWidth: 2 },
                labelStyle: { fill: 'hsl(215 20% 85%)', fontSize: 10 },
                labelBgStyle: { fill: 'hsl(215 30% 18%)', fillOpacity: 0.95 },
                labelBgBorderRadius: 4,
                labelBgPadding: [4, 6] as [number, number],
              }}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={true}
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
        {showPanel && (
          <div className="w-[360px] shrink-0 flex flex-col border-l border-slate-700/80 bg-slate-900/60">
            <div className="flex items-center justify-between border-b border-slate-700/80 px-4 py-3">
              <span className="text-sm font-semibold text-slate-200">{selectedNodeId ? `Node: ${selectedNodeId}` : 'Agent context'}</span>
              <button type="button" onClick={() => { setPanelOpen(false); setSelectedNodeId(null) }} className="rounded-md p-1.5 text-slate-500 hover:bg-slate-700/80 hover:text-slate-200" title="Close panel">
                <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div className="flex-1 min-h-0 overflow-auto p-4">
              {selectedNodeId ? (
                <div className="space-y-4">
                  <div>
                    <label className="mb-2 block text-xs font-medium uppercase tracking-wider text-slate-500">Node</label>
                    <div className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200">{selectedNodeId}</div>
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-medium uppercase tracking-wider text-slate-500">Label (in graph)</label>
                    <div className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200">{nodes.find((n) => n.id === selectedNodeId)?.data?.label ?? selectedNodeId}</div>
                  </div>
                  <div>
                    <label className="mb-2 block text-xs font-medium uppercase tracking-wider text-slate-500">Prompt</label>
                    <div className="min-h-[8rem] overflow-auto rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm markdown-preview"><ReactMarkdown>{selectedPrompt ?? '_No prompt for this node._'}</ReactMarkdown></div>
                  </div>
                </div>
              ) : (
                <>
                  {data.frontmatter && (<div className="mb-4"><label className="mb-2 block text-xs font-medium uppercase tracking-wider text-slate-500">Frontmatter</label><pre className="whitespace-pre-wrap rounded-lg border border-slate-700 bg-slate-950 p-3 text-[0.75rem] text-slate-300">{data.frontmatter}</pre></div>)}
                  {data.rest_md && (<div><label className="mb-2 block text-xs font-medium uppercase tracking-wider text-slate-500">Rest of markdown</label><div className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm markdown-preview"><ReactMarkdown>{data.rest_md || '_No content._'}</ReactMarkdown></div></div>)}
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
