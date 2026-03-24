# Claude Code: retail prompt optimizer + `llm-probe` MCP

## MCP registration

The repo root [`.mcp.json`](../.mcp.json) includes server **`llm-probe`**, which runs:

```bash
uv run python -m llm_gateway.mcp_llm_probe
```

It exposes one tool: **`llm_chat`** (messages + model + optional provider/seed/reasoning).

## Agent definition

Full instructions and workflow: [`.claude/agents/retail-prompt-optimizer.md`](../.claude/agents/retail-prompt-optimizer.md).

## Example invocation

From the **repository root** (so Claude Code loads `.mcp.json`):

```bash
claude --agent retail-prompt-optimizer -p "Traces: outputs/<run>/experiment_1/task_2__trial_1__seed_X.json. Target model: gpt-5-nano, reasoning high. Optimize: gepa/examples/tau2_retail_mermaid/seed_solo_v1.md — work in results/prompt_optimizer_runs/ only until I approve merge."
```

Adjust paths and model to match your run.

## After the agent finishes

Run a full solo sweep yourself:

```bash
uv run python -m domains.retail.run_solo_tasks --config configs/openai_simulation_v1.yaml --fresh
```

Use the YAML that matches your target assistant model and policy.

---

## Why it looks like “nothing is happening”

### 1. MCP stdio servers are quiet on stdout

The probe speaks the MCP protocol on **stdout**, so you will not see a normal “server started” banner there. Optional logs go to **stderr** when `LLM_PROBE_VERBOSE=1` (enabled in [`.mcp.json`](../.mcp.json) for `llm-probe`).

### 2. Run Claude Code from the repo root

`.mcp.json` is loaded from the **current working directory**. Start Claude Code (or `claude`) with cwd = `tau2-mermaid` root, same as [scripts/run_agent_tasks.py](../scripts/run_agent_tasks.py).

### 3. Restart / reload after editing `.mcp.json`

After adding `llm-probe`, **restart** the Claude Code session (or reload MCP servers) so the new server is spawned.

### 4. Confirm the tool exists

In the Claude Code UI, open **MCP / tools** (wording varies by version) and check for server **`llm-probe`** and tool **`llm_chat`** (sometimes shown as `mcp__llm-probe__llm_chat`).

### 5. Use an interactive session for visible streaming

One-shot CLI (`claude -p "..."`) may feel “silent” until the final JSON. Prefer **interactive** mode so you see tool calls and streaming text.

### 6. Sanity-check the probe outside Claude

From repo root:

```bash
LLM_PROBE_VERBOSE=1 uv run python -m llm_gateway.mcp_llm_probe
```

It will wait for stdio (normal). The check is really “Claude spawned this process”: use verbose logs when the agent calls `llm_chat`.

### 7. Watch the optimizer’s own paper trail

The `retail-prompt-optimizer` agent should write under `results/prompt_optimizer_runs/<run>/` (`probes/`, `metrics.md`). If that folder never appears, the agent may not have been invoked with a concrete trace path and task — give it an absolute path to a trace JSON in the prompt.
