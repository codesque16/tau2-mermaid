# tau2-mermaid

Agent and orchestrator for tau2 mermaid simulations. Retail agent uses the SOP MCP server (load_graph, goto_node, todo) and optional viewer for session traces.

## Minimal setup: install, start MCP (retail), run simulation

### 1. Clone and install

```bash
git clone <repo-url>
cd tau2-mermaid
```

Create a virtualenv and install the project (Python 3.10+):

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

### 2. Optional: build the viewer

The viewer app shows sessions and the process graph at `/app/viewer` when the MCP server is running. Build it only if you want the web UI:

```bash
cd agent/agent_mermaid/viewer-app
npm install
npm run build
cd ../../..
```

### 3. Environment (API keys)

Create a `.env` in the repo root with your LLM API keys (used by the simulation). For example:

```bash
# LiteLLM / OpenAI
OPENAI_API_KEY=sk-...

# Or Google (for Gemini, if used in config)
GOOGLE_GENERATIVE_AI_API_KEY=...
```

The config `configs/mermaid_human.yaml` uses LiteLLM; set the key for the model you use (e.g. `gemini/gemini-3-flash-preview` or `gpt-4o`).

### 4. Start the MCP server (retail)

In a **first terminal**, start the SOP MCP server (required for the retail agent):

```bash
# From repo root, with venv activated:
python -m agent.agent_mermaid.sop_mcp_server
```

Or with uv (no venv needed):

```bash
uv run python -m agent.agent_mermaid.sop_mcp_server
```

Server runs at **http://localhost:8000**. The MCP endpoint is at `http://localhost:8000/mcp`.

### 5. Run the simulation

In a **second terminal**, from the repo root:

```bash
# With venv activated:
python main.py configs/mermaid_human.yaml
```

Or with uv:

```bash
uv run main.py configs/mermaid_human.yaml
```

This runs the **retail** mermaid agent (assistant) with a **human** user: you type replies at the `[user - Your response]:` prompt. The assistant uses the MCP server (load_graph, goto_node, todo) for the retail SOP.

To use the viewer: open **http://localhost:8000** (or http://localhost:8000/app/viewer) in a browser while the MCP server is running; sessions appear under Sessions after you run the simulation.

---

## Summary (copy-paste)

```bash
# Terminal 1
git clone <repo-url> && cd tau2-mermaid
python -m venv .venv && source .venv/bin/activate
pip install -e .
# Optional: cd agent/agent_mermaid/viewer-app && npm install && npm run build && cd ../../..
# Add .env with OPENAI_API_KEY or GOOGLE_GENERATIVE_AI_API_KEY as needed
python -m agent.agent_mermaid.sop_mcp_server

# Terminal 2 (from repo root)
source .venv/bin/activate
python main.py configs/mermaid_human.yaml
```


## Running the Retail Agent (Mermaid)

The retail agent uses an MCP server for SOP (standard operating procedure) support. **Start the MCP server first**, then run the agent.

### 1. Start the MCP server

From the project root (where `agent/` lives):

```bash
uv run python -m agent.agent_mermaid.sop_mcp_server
```

Keep this terminal running. The server listens on `http://localhost:8000/mcp` by default.

### 2. Run the retail agent

In another terminal, from the `tau2-bench` directory:

```bash
uv run tau2 run --domain retail --agent llm_mermaid_agent --agent-llm "gpt-4.1-mini" --user-llm "gpt-4.1-mini" --name "[MERMAID_AGENT][TEST_ALL]" --max-steps 60 --task-ids 96 --mcp-server-url http://localhost:8000/mcp --mcp-sop-file "retail/AGENTS.md"
```

- `--domain retail` — use the retail domain.
- `--agent llm_mermaid_agent` — use the Mermaid-backed LLM agent.
- `--mcp-server-url` and `--mcp-sop-file` — point the agent at the running MCP server and the retail SOP file.


### Important Commands

**Export trajectories to pages:**

```bash
uv run python scripts/export_trajectories_for_pages.py --add --name "[Retail][Mermaid_harness_v1][2026-02-25T13_34_45]" data/simulations/2026-02-25T13:34:45.328964_retail_llm_mermaid_agent_gpt-4.1-mini_user_simulator_gpt-4.1-mini.json
```

Example output:
```
Exported 114 tasks → docs/data/_Retail__Mermaid_harness_v1__2026-02-25T13_34_45_/
Wrote runs manifest: docs/data/runs.json (30 runs)
```

**Run tau2 with Mermaid agent:**

```bash
uv run tau2 run --domain retail --agent llm_mermaid_agent --agent-llm "gpt-4.1-mini" --user-llm "gpt-4.1-mini" --service-name "retail-mermaid-tau2" --name "[MERMAID_AGENT][ALL]" --max-steps 60 --seed 12345 --mcp-server-url http://localhost:8000/mcp --mcp-sop-file "retail/AGENTS.md"
```

**Start the MCP server:**

```bash
uv run python -m agent.agent_mermaid.sop_mcp_server
```