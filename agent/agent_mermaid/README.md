# Mermaid agent

Agent type that uses a **structured folder layout** and **progressive discovery**: the system prompt and workflow diagram are loaded from a mermaid-agent folder; the LLM can call `enter_mermaid_node(node_id)` to load task-specific instructions for a node on demand.

## Folder structure (mermaid-agents)

Agents live under `agent_mermaid/mermaid-agents/`. Each agent is a folder, e.g. `mermaid-agents/airline/`:

```
mermaid-agents/
└── <agent_name>/
    ├── index.md           # System prompt and high-level instructions
    ├── agent-mermaid.md   # Mermaid diagram (visualization + traversal)
    └── nodes/
        ├── intake/
        │   └── index.md   # Task-specific instructions for this node
        ├── classify/
        │   └── index.md
        └── ...
```

- **index.md**: Content is used as the agent’s system prompt.
- **agent-mermaid.md**: Mermaid flowchart (e.g. `flowchart TD ...`). Injected into the system prompt so the model knows the graph and can choose nodes.
- **nodes/****/index.md**: Instructions for that workflow step. Returned by the `enter_mermaid_node` tool when the model calls it with that `node_id`.

## Usage

```python
from agent import create_agent, AgentConfig

config = AgentConfig(system_prompt="")  # Overridden by agent's index.md
agent = create_agent(
    "mermaid",
    name="assistant",
    config=config,
    model="gemini/gemini-2.0-flash",
    agent_name="airline",  # folder name under mermaid-agents/
    mermaid_agents_root=None,  # optional; default is agent_mermaid/mermaid-agents
)
# Same interface as BaseAgent: respond_stream(incoming, on_chunk=...)
```

The agent inherits from `BaseAgent` and can be used anywhere other agents are (e.g. orchestrator simulations).

## Behavior

1. On init, the agent loads `index.md` and `agent-mermaid.md` from the agent folder and lists available nodes from `nodes/`.
2. Each user turn is handled in a loop: the model may call `enter_mermaid_node(node_id)`; the tool returns the content of `nodes/<node_id>/index.md`.
3. The model can call multiple nodes in one turn (or reply with text only). When it stops calling tools, its final text reply is returned as the turn response.

This keeps the initial context smaller and lets the model pull in instructions only for the nodes it needs (progressive discovery).