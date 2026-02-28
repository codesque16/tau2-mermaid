# Graph Traversal Benchmark

Ablation benchmark comparing three representation conditions for workflow adherence:
- **Prose**: Workflow in natural language
- **Mermaid**: Workflow as Mermaid diagram
- **Mermaid + Harness**: Mermaid + runtime state tracking and transition validation

## Setup

Uses [uv](https://docs.astral.sh/uv/) as the package manager.

```bash
cd tau2-1xn/benchmark
uv sync
```

Set API keys for your chosen model (e.g. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`).

## Run Evaluation

```bash
# Full eval: 5 trials, all scenarios, all three conditions (default)
uv run python run_eval.py --model gpt-4o-mini --trials 5

# One scenario by number (see scenarios/index.json): e.g. scenario 10
uv run python run_eval.py --model gpt-4o-mini --trials 2 --scenario 10

# One scenario, one test: scenario 10, test 2 only
uv run python run_eval.py --model gpt-4o-mini --trials 2 --scenario 10 --test 2

# Multiple scenarios and tests: scenarios 5 and 10, test 1 and 2 where applicable
uv run python run_eval.py --model gpt-4o-mini --trials 2 -s 5 10 -t 1 2

# Conditions: only prose, or only prose + mermaid (omit for all three)
uv run python run_eval.py --model gpt-4o-mini --trials 2 --scenario 10 --conditions prose
uv run python run_eval.py --model gpt-4o-mini --trials 2 -c prose mermaid

# Concurrency: run N evaluations in parallel (default 1). Use -j 4 for 4 workers.
uv run python run_eval.py --model gpt-4o-mini --trials 5 -j 4

# By scenario ID (legacy)
uv run python run_eval.py --model gpt-4o-mini --trials 2 --scenarios scenario_01_order_cancellation
```

**Scenario and test index:** `scenarios/index.json` maps scenario numbers (1–10) to folders and lists test numbers per scenario. Use `--scenario N` / `-s N` and `--test N` / `-t N` for short-hand.

Run tests:

```bash
uv run pytest tests/ -v
```

## Logging and observability

- **Rich console**: Progress bar, header panel, and results table are printed with [Rich](https://rich.readthedocs.io/).
- **Logfire**: When enabled (default), [Logfire](https://logfire.pydantic.dev/) traces the run:
  - Top-level span `graph_traversal_eval` (model, trials, conditions)
  - Per-scenario span `scenario` (scenario_id, test_cases)
  - Per-run span `agent_run` (scenario_id, test_id, condition, trial, path, expected_path, passed, completed, turns)
  - LiteLLM is instrumented so each LLM call appears in the trace.

Options:

- `-v` / `--verbose`: Log each agent run (path vs expected, pass/fail) to the console.
- `--no-logfire`: Disable Logfire (no tracing, no `instrument_litellm`). Use for a quick run without Logfire.

Set `LOGFIRE_TOKEN` (and optionally `LOGFIRE_PROJECT`) to send traces to Pydantic Logfire; otherwise traces are local only.

## LiteLLM Model Strings

- OpenAI: `gpt-4o`, `gpt-4o-mini`, `gpt-4.1-mini`
- Anthropic: `claude-3-5-sonnet-20241022`, `claude-3-opus-20240229`
- Google: `gemini/gemini-2.0-flash`, `gemini/gemini-1.5-pro`

See [LiteLLM docs](https://docs.litellm.ai/docs/providers) for more.

## Metrics

- **pass^1**: Fraction of test cases that passed at least once (capability ceiling)
- **pass^k**: Fraction that passed ALL k trials (reliability floor)
- **path_accuracy**: Fraction of individual trials that passed

## Structure

```
benchmark/
├── run_eval.py          # Main eval script
├── scenarios/           # Scenario definitions
│   └── scenario_XX_*/
│       ├── graph.mermaid
│       ├── graph_prose.md
│       ├── metadata.json
│       └── test_cases/
├── src/graph_harness/
│   ├── harness.py       # GraphHarness, Graph
│   ├── conditions.py   # System prompts for 3 conditions
│   ├── agent.py        # LLM agent loop
│   └── eval_utils.py   # Path matching, pass^k
└── tests/
```
