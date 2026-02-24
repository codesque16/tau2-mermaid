import { useCallback, useEffect, useMemo, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import {
  ReactFlow,
  Background,
  Controls,
  useNodesState,
  useEdgesState,
  Handle,
  Position,
  type Node,
  type Edge,
  type NodeProps,
  type NodeTypes,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { LayoutAlgorithm, DagreRankDir, DagreLayoutOptions } from '../utils/autoLayout'
import { layoutWithDagre, layoutWithD3, layoutWithElk } from '../utils/autoLayout'
import type { GraphJson, GraphNodeJson, GraphEdgeJson, FlowNodeData } from '../utils/flowGraph'
import { graphJsonToFlow as graphJsonToFlowShared } from '../utils/flowGraph'

const API_BASE = ''

export type { GraphNodeJson, GraphEdgeJson, GraphJson }

/** One node's prompt entry from AGENTS.md Node Prompts YAML */
export type NodePromptEntry = { prompt: string; tools?: string[]; examples?: { user?: string; agent?: string }[] }

interface AgentContent {
  agent_name: string
  frontmatter: string
  rest_md: string
  mermaid: string
  /** node_id -> NodePromptEntry per MCP spec */
  node_prompts: Record<string, NodePromptEntry>
  graph_json: GraphJson
}

function graphJsonToFlow(graph: GraphJson): { nodes: Node<FlowNodeData>[]; edges: Edge[] } {
  return graphJsonToFlowShared(graph, { sourcePosition: Position.Right, targetPosition: Position.Left })
}

function flowToGraphJson(
  nodes: Node<FlowNodeData>[],
  edges: Edge[]
): GraphJson {
  return {
    nodes: nodes.map((n) => {
      const shape = (n.data?.shape as GraphNodeJson['shape']) ?? 'rectangle'
      const nodeType = (n.data?.nodeType as GraphNodeJson['node_type']) ?? (shape === 'stadium' ? 'terminal' : shape === 'rhombus' ? 'decision' : 'normal')
      return {
        id: n.id,
        label: (n.data?.label as string) ?? n.id,
        shape,
        node_type: nodeType,
        x: n.position.x,
        y: n.position.y,
      }
    }),
    edges: edges.map((e) => ({
      source: e.source,
      target: e.target,
      label: e.label != null ? String(e.label) : null,
    })),
  }
}

function TerminalNode(props: NodeProps<Node<FlowNodeData>>) {
  const { data } = props
  return (
    <div className="px-3 py-2 rounded-lg text-xs font-medium border bg-emerald-900/80 border-emerald-500/70 text-emerald-100 relative">
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !border-2 !bg-emerald-900 !border-emerald-500" />
      {data?.label ?? props.id}
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !border-2 !bg-emerald-900 !border-emerald-500" />
    </div>
  )
}

function NormalNode(props: NodeProps<Node<FlowNodeData>>) {
  const { data } = props
  return (
    <div className="px-3 py-2 rounded-lg text-xs font-medium border bg-slate-800 border-slate-600 text-slate-100 relative">
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !border-2 !bg-slate-800 !border-slate-500" />
      {data?.label ?? props.id}
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !border-2 !bg-slate-800 !border-slate-500" />
    </div>
  )
}

function DecisionNode(props: NodeProps<Node<FlowNodeData>>) {
  const { data } = props
  return (
    <div className="px-3 py-2 rounded-lg text-xs font-medium border bg-amber-900/70 border-amber-500/60 text-amber-100 relative">
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !border-2 !bg-amber-900 !border-amber-500" />
      {data?.label ?? props.id}
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !border-2 !bg-amber-900 !border-amber-500" />
    </div>
  )
}

const nodeTypes: NodeTypes = {
  terminal: TerminalNode as NodeTypes['terminal'],
  normal: NormalNode as NodeTypes['normal'],
  decision: DecisionNode as NodeTypes['decision'],
}

type EditorTab = 'frontmatter' | 'rest' | 'nodes'

export function Agents() {
  const [agents, setAgents] = useState<string[]>([])
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null)
  const [content, setContent] = useState<AgentContent | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [tab, setTab] = useState<EditorTab>('nodes')
  const [restViewMode, setRestViewMode] = useState<'preview' | 'edit'>('preview')
  const [promptViewMode, setPromptViewMode] = useState<'preview' | 'edit'>('preview')
  const [frontmatter, setFrontmatter] = useState('')
  const [restMd, setRestMd] = useState('')
  const [nodePrompts, setNodePrompts] = useState<Record<string, NodePromptEntry>>({})
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [layoutAlgorithm, setLayoutAlgorithm] = useState<LayoutAlgorithm>('dagre')
  const [layoutRunning, setLayoutRunning] = useState(false)
  const [dagreOptions, setDagreOptions] = useState<DagreLayoutOptions>({
    rankdir: 'LR',
    ranksep: 100,
    nodesep: 100,
    edgesep: 100,
    marginx: 0,
    marginy: 0,
  })
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  useEffect(() => {
    fetch(`${API_BASE}/api/agents`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('Failed to load'))))
      .then((data) => setAgents(data.agents ?? []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedAgent) {
      setContent(null)
      setLoadError(null)
      setFrontmatter('')
      setRestMd('')
      setNodePrompts({})
      setNodes([])
      setEdges([])
      setSelectedNodeId(null)
      return
    }
    setLoading(true)
    setLoadError(null)
    fetch(`${API_BASE}/api/agents/${encodeURIComponent(selectedAgent)}`)
      .then((r) => {
        if (!r.ok) throw new Error(r.status === 404 ? 'Agent not found' : 'Failed to load')
        return r.json()
      })
      .then((data: AgentContent) => {
        setContent(data)
        setFrontmatter(data.frontmatter ?? '')
        setRestMd(data.rest_md ?? '')
        setNodePrompts(data.node_prompts ?? {})
        const graph = data.graph_json ?? { nodes: [], edges: [] }
        console.log('[Agents] graph_json from API:', JSON.stringify(graph, null, 2))
        const { nodes: n, edges: e } = graphJsonToFlow(graph)
        console.log('[Agents] flow nodes count:', n.length, 'flow edges count:', e.length, 'flow edges:', e)
        if (n.length > 0) {
          const layouted = layoutWithDagre(n, e, { dagre: dagreOptions })
          setNodes(layouted)
        } else {
          setNodes(n)
        }
        setEdges(e)
      })
      .catch((e) => setLoadError(e.message))
      .finally(() => setLoading(false))
  }, [selectedAgent, setNodes, setEdges])

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node<FlowNodeData>) => {
    setSelectedNodeId(node.id)
  }, [])

  const runAutoLayout = useCallback(() => {
    if (nodes.length === 0) return
    setLayoutRunning(true)
    const run = async () => {
      try {
        if (layoutAlgorithm === 'dagre') {
          const next = layoutWithDagre(nodes, edges, { dagre: dagreOptions })
          setNodes(next)
        } else if (layoutAlgorithm === 'd3') {
          const next = layoutWithD3(nodes, edges)
          setNodes(next)
        } else {
          const next = await layoutWithElk(nodes, edges)
          setNodes(next)
        }
      } finally {
        setLayoutRunning(false)
      }
    }
    run()
  }, [nodes, edges, layoutAlgorithm, dagreOptions, setNodes])

  const handleSave = useCallback(() => {
    if (!selectedAgent) return
    setSaveStatus('saving')
    const graph_json = flowToGraphJson(nodes, edges)
    fetch(`${API_BASE}/api/agents/${encodeURIComponent(selectedAgent)}/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        frontmatter,
        rest_md: restMd,
        graph_json,
        node_prompts: nodePrompts,
      }),
    })
      .then((r) => {
        if (!r.ok) throw new Error('Save failed')
        return r.json()
      })
      .then(() => {
        setSaveStatus('saved')
        setTimeout(() => setSaveStatus('idle'), 2000)
      })
      .catch(() => setSaveStatus('error'))
  }, [selectedAgent, frontmatter, restMd, nodePrompts, nodes, edges])

  const updateNodeLabel = useCallback(
    (nodeId: string, label: string) => {
      setNodes((nds) =>
        nds.map((n) => (n.id === nodeId ? { ...n, data: { ...n.data, label } } : n))
      )
    },
    [setNodes]
  )

  const updateNodePrompt = useCallback((nodeId: string, prompt: string) => {
    setNodePrompts((prev) => ({
      ...prev,
      [nodeId]: { ...(prev[nodeId] ?? { prompt: '', tools: [], examples: [] }), prompt },
    }))
  }, [])

  const nodeList = useMemo(() => nodes.map((n) => n.id).sort(), [nodes])

  return (
    <div className="flex flex-1 min-h-0">
      {/* Sidebar: agent list */}
        <aside className="w-52 shrink-0 border-r border-slate-700/80 bg-slate-900/50 flex flex-col">
          <div className="px-4 py-3 border-b border-slate-700/80">
            <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              Agents
            </h2>
          </div>
        <div className="flex-1 overflow-auto py-2">
          {agents.length === 0 ? (
            <p className="px-4 py-2 text-sm text-slate-500">No agents</p>
          ) : (
            agents.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => setSelectedAgent(name)}
                className={`
                  w-full text-left px-4 py-2.5 text-sm font-medium transition-colors flex items-center gap-2
                  ${selectedAgent === name
                    ? 'bg-slate-700/80 text-slate-100 border-l-2 border-slate-400'
                    : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200 border-l-2 border-transparent'}
                `}
              >
                {name}
              </button>
            ))
          )}
        </div>
      </aside>

      {/* Main: empty state or editor */}
      <main className="flex-1 min-w-0 flex flex-col p-6 overflow-auto">
          {!selectedAgent && (
            <div className="flex flex-col items-center justify-center flex-1 text-slate-500">
              <p className="text-sm">Select an agent to view and edit its flowchart and config.</p>
            </div>
          )}

        {selectedAgent && loading && (
          <div className="flex items-center justify-center flex-1 text-slate-400">Loading…</div>
        )}

          {selectedAgent && loadError && (
            <div className="rounded-lg border border-red-900/50 bg-red-950/20 px-4 py-3 text-red-400 text-sm">
              {loadError}
            </div>
          )}

        {selectedAgent && content && !loading && !loadError && (
          <div className="flex flex-1 min-h-0 gap-4 max-h-[calc(100vh-8rem)]">
            {/* Card: flowchart */}
            <div className="flex-1 min-w-0 flex flex-col rounded-lg border border-slate-700/80 bg-slate-900/60 overflow-hidden shadow-sm">
              <div className="px-4 py-2.5 border-b border-slate-700/80 flex items-center justify-between gap-2 flex-wrap">
                <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
                  SOP Flowchart
                </span>
                <div className="flex items-center gap-2">
                  <select
                    value={layoutAlgorithm}
                    onChange={(e) => setLayoutAlgorithm(e.target.value as LayoutAlgorithm)}
                    className="px-3 py-1.5 rounded-lg bg-slate-700/80 border-0 text-slate-200 text-xs focus:ring-2 focus:ring-slate-500 focus:ring-offset-0 focus:ring-offset-slate-900"
                    title="Layout algorithm"
                  >
                    <option value="dagre">Dagre</option>
                    <option value="d3">D3 Hierarchy</option>
                    <option value="elk">ELK</option>
                  </select>
                  <button
                    type="button"
                    onClick={runAutoLayout}
                    disabled={layoutRunning || nodes.length === 0}
                    className="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-700/80 hover:bg-slate-600 text-slate-200 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                    title="Apply auto layout"
                  >
                    {layoutRunning ? 'Layout…' : 'Auto layout'}
                  </button>
                </div>
                <span className="text-xs text-slate-500">{content.agent_name}</span>
              </div>
              {layoutAlgorithm === 'dagre' && (
                <div className="px-4 py-2 border-b border-slate-700/60 bg-slate-800/40 flex flex-wrap items-center gap-4 text-xs">
                  <span className="text-slate-500 font-medium uppercase tracking-wider">Dagre settings</span>
                  <label className="flex items-center gap-2">
                    <span className="text-slate-400">Direction</span>
                    <select
                      value={dagreOptions.rankdir ?? 'LR'}
                      onChange={(e) => setDagreOptions((o) => ({ ...o, rankdir: e.target.value as DagreRankDir }))}
                      className="px-2 py-1 rounded bg-slate-800 border border-slate-600 text-slate-200 focus:ring-1 focus:ring-slate-500"
                    >
                      <option value="TB">Top → Bottom</option>
                      <option value="BT">Bottom → Top</option>
                      <option value="LR">Left → Right</option>
                      <option value="RL">Right → Left</option>
                    </select>
                  </label>
                  <label className="flex items-center gap-2">
                    <span className="text-slate-400">Rank sep</span>
                    <input
                      type="number"
                      min={20}
                      max={200}
                      value={dagreOptions.ranksep ?? 55}
                      onChange={(e) => setDagreOptions((o) => ({ ...o, ranksep: Number(e.target.value) || 55 }))}
                      className="w-16 px-2 py-1 rounded bg-slate-800 border border-slate-600 text-slate-200 focus:ring-1 focus:ring-slate-500"
                    />
                  </label>
                  <label className="flex items-center gap-2">
                    <span className="text-slate-400">Node sep</span>
                    <input
                      type="number"
                      min={10}
                      max={150}
                      value={dagreOptions.nodesep ?? 100}
                      onChange={(e) => setDagreOptions((o) => ({ ...o, nodesep: Number(e.target.value) || 100 }))}
                      className="w-16 px-2 py-1 rounded bg-slate-800 border border-slate-600 text-slate-200 focus:ring-1 focus:ring-slate-500"
                    />
                  </label>
                  <label className="flex items-center gap-2">
                    <span className="text-slate-400">Edge sep</span>
                    <input
                      type="number"
                      min={5}
                      max={80}
                      value={dagreOptions.edgesep ?? 55}
                      onChange={(e) => setDagreOptions((o) => ({ ...o, edgesep: Number(e.target.value) || 55 }))}
                      className="w-16 px-2 py-1 rounded bg-slate-800 border border-slate-600 text-slate-200 focus:ring-1 focus:ring-slate-500"
                    />
                  </label>
                </div>
              )}
              <div className="flex-1 min-h-[320px]">
                <ReactFlow
                  nodes={nodes}
                  edges={edges}
                  onNodesChange={onNodesChange}
                  onEdgesChange={onEdgesChange}
                  onNodeClick={onNodeClick}
                  nodeTypes={nodeTypes}
                  defaultEdgeOptions={{
                    type: 'straight',
                    style: { stroke: 'hsl(215 25% 72%)', strokeWidth: 2 },
                    labelStyle: { fill: 'hsl(215 20% 85%)', fontSize: 10 },
                    labelBgStyle: { fill: 'hsl(215 30% 18%)', fillOpacity: 0.95 },
                    labelBgBorderRadius: 4,
                    labelBgPadding: [4, 6] as [number, number],
                  }}
                  fitView
                  fitViewOptions={{ padding: 0.25 }}
                  minZoom={0.15}
                  maxZoom={1.5}
                >
                  <Background />
                  <Controls />
                </ReactFlow>
              </div>
            </div>

            {/* Card: config panel */}
            <div className="w-[360px] shrink-0 flex flex-col rounded-lg border border-slate-700/80 bg-slate-900/60 overflow-hidden shadow-sm">
              {/* Tab list: segment control style */}
              <div className="p-1.5 border-b border-slate-700/80">
                <div className="flex rounded-lg bg-slate-800/80 p-0.5 gap-0.5">
                  {([
                    { t: 'frontmatter' as const, label: 'Frontmatter' },
                    { t: 'rest' as const, label: 'Rest' },
                    { t: 'nodes' as const, label: 'Node prompts' },
                  ]).map(({ t, label }) => (
                    <button
                      key={t}
                      type="button"
                      onClick={() => setTab(t)}
                      className={`
                        flex-1 px-3 py-2 rounded-md text-xs font-medium transition-colors
                        ${tab === t
                          ? 'bg-slate-600 text-slate-100 shadow-sm'
                          : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'}
                      `}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex-1 min-h-0 flex flex-col p-4">
                {tab === 'frontmatter' && (
                  <div className="flex flex-col flex-1 min-h-0">
                    <label className="block text-xs font-medium text-slate-500 uppercase tracking-wider mb-2 shrink-0">
                      YAML frontmatter
                    </label>
                    <textarea
                      value={frontmatter}
                      onChange={(e) => setFrontmatter(e.target.value)}
                      className="flex-1 min-h-[8rem] w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-xs font-mono resize-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500"
                      spellCheck={false}
                    />
                  </div>
                )}
                {tab === 'rest' && (
                  <div className="flex flex-col flex-1 min-h-0">
                    <div className="flex items-center justify-between mb-2 shrink-0">
                      <label className="block text-xs font-medium text-slate-500 uppercase tracking-wider">
                        Rest of markdown
                      </label>
                      <div className="flex rounded-lg bg-slate-800/80 p-0.5 gap-0.5">
                        {(['preview', 'edit'] as const).map((mode) => (
                          <button
                            key={mode}
                            type="button"
                            onClick={() => setRestViewMode(mode)}
                            className={`
                              px-3 py-1.5 rounded-md text-xs font-medium capitalize transition-colors
                              ${restViewMode === mode
                                ? 'bg-slate-600 text-slate-100 shadow-sm'
                                : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'}
                            `}
                          >
                            {mode}
                          </button>
                        ))}
                      </div>
                    </div>
                    {restViewMode === 'edit' ? (
                      <textarea
                        value={restMd}
                        onChange={(e) => setRestMd(e.target.value)}
                        className="flex-1 min-h-[8rem] w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-xs font-mono resize-none focus:ring-2 focus:ring-blue-500/50 focus:border-blue-500"
                        spellCheck={false}
                      />
                    ) : (
                      <div className="flex-1 min-h-0 overflow-auto px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-sm markdown-preview">
                        <ReactMarkdown>{restMd || '_No content._'}</ReactMarkdown>
                      </div>
                    )}
                  </div>
                )}
                {tab === 'nodes' && (
                  <div className="flex flex-col flex-1 min-h-0">
                    <div className="shrink-0">
                      <label className="block text-xs font-medium text-slate-500 uppercase tracking-wider mb-2">
                        Node
                      </label>
                      <select
                        value={selectedNodeId ?? ''}
                        onChange={(e) => setSelectedNodeId(e.target.value || null)}
                        className="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-sm focus:ring-2 focus:ring-blue-500/50"
                      >
                        <option value="">Select a node</option>
                        {nodeList.map((id) => (
                          <option key={id} value={id}>
                            {id}
                          </option>
                        ))}
                      </select>
                    </div>
                    {selectedNodeId && (
                      <>
                        <div className="shrink-0 mt-4">
                          <label className="block text-xs font-medium text-slate-500 uppercase tracking-wider mb-2">
                            Label (in graph)
                          </label>
                          <input
                            type="text"
                            value={nodes.find((n) => n.id === selectedNodeId)?.data?.label ?? selectedNodeId}
                            onChange={(e) => updateNodeLabel(selectedNodeId, e.target.value)}
                            className="w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-sm focus:ring-2 focus:ring-blue-500/50"
                          />
                        </div>
                        <div className="flex flex-col flex-1 min-h-0 mt-4">
                          <div className="flex items-center justify-between mb-2 shrink-0">
                            <label className="block text-xs font-medium text-slate-500 uppercase tracking-wider">
                              Prompt
                            </label>
                            <div className="flex rounded-lg bg-slate-800/80 p-0.5 gap-0.5">
                              {(['preview', 'edit'] as const).map((mode) => (
                                <button
                                  key={mode}
                                  type="button"
                                  onClick={() => setPromptViewMode(mode)}
                                  className={`
                                    px-3 py-1.5 rounded-md text-xs font-medium capitalize transition-colors
                                    ${promptViewMode === mode
                                      ? 'bg-slate-600 text-slate-100 shadow-sm'
                                      : 'text-slate-500 hover:text-slate-300 hover:bg-slate-700/50'}
                                  `}
                                >
                                  {mode}
                                </button>
                              ))}
                            </div>
                          </div>
                          {promptViewMode === 'edit' ? (
                            <textarea
                              value={nodePrompts[selectedNodeId]?.prompt ?? ''}
                              onChange={(e) => updateNodePrompt(selectedNodeId, e.target.value)}
                              className="flex-1 min-h-[6rem] w-full px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-xs font-mono resize-none focus:ring-2 focus:ring-blue-500/50"
                              spellCheck={false}
                            />
                          ) : (
                            <div className="flex-1 min-h-0 overflow-auto px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-slate-200 text-sm markdown-preview">
                              <ReactMarkdown>{nodePrompts[selectedNodeId]?.prompt ?? '_No prompt._'}</ReactMarkdown>
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
              <div className="px-4 py-3 border-t border-slate-700/80 flex items-center justify-between gap-3">
                <span className="text-xs text-slate-500">
                  {saveStatus === 'saving' && 'Saving…'}
                  {saveStatus === 'saved' && 'Saved.'}
                  {saveStatus === 'error' && 'Save failed.'}
                </span>
                <button
                  type="button"
                  onClick={handleSave}
                  disabled={saveStatus === 'saving'}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-slate-700/80 hover:bg-slate-600 text-slate-100 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  Save AGENTS.md
                </button>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
