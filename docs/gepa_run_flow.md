# GEPA run flow (tau2-mermaid retail)

This document describes how a **GEPA** (`optimize_anything`) run works end-to-end: what runs in which order, how **multi-component** candidates fit in, how **Pareto** candidates are chosen and updated, and how that maps to the **tau2 retail** stack (solo agent, optional diagnosis LLM, reflection LLM).

GEPA’s own names are used first; informal aliases are in parentheses where helpful.

---

## 1. Roles (mental model)

| Piece | What it is in code | What it does |
|--------|---------------------|----------------|
| **Candidate** | `dict[str, str]` (or a single string wrapped as `{"current_candidate": ...}`) | Named **text artifacts** the optimizer edits (one policy string, or many components). |
| **Evaluator** | Your `evaluator(candidate, example)` (and optional `opt_state`) | **Executes** the task: runs the retail solo agent, scores the trajectory (e.g. DB match 0/1), returns **score** + **side_info** (traces, qualitative feedback). Counts as **metric calls**. |
| **Reflection LM** (“reflector”) | `config.reflection.reflection_lm` | Proposes **new text** for one or more components using a template with `<curr_param>` (current text) and `<side_info>` (formatted feedback). Implemented via `InstructionProposalSignature` in `gepa`. |
| **Candidate selector** (“which program to mutate”) | `config.engine.candidate_selection_strategy`, e.g. `pareto` | Picks **which existing candidate index** in the pool to use as the **parent** for the next reflective step. |
| **Module / component selector** (“which part of the program to edit”) | `config.reflection.module_selector`, e.g. `round_robin` or `all` | Picks **which key(s)** of the parent `dict` the reflection LM may rewrite this iteration (`tools_markdown`, `mermaid_instructions`, …). |
| **Merge proposer** (optional) | `config.merge` | Occasionally combines two parents from the frontier; **off** in default retail YAML. |
| **Valuation / Pareto bookkeeping** | `GEPAState` + `val_evaluation_policy` | After a **new** candidate is accepted, runs (or reuses) **full validation** scores and updates **per-example Pareto sets** and aggregate metrics. |

There is no separate class named “curator”; **candidate selection** (Pareto vs current-best, etc.) plus **state updates** together play that role.

---

## 2. Bootstrapping: seed → first full val evaluation

1. **Seed candidate** is loaded from YAML:
   - **Single-file** (`tau2_retail_mermaid`): `gepa.seed_policy_path` → one string → internally `{"current_candidate": text}`.
   - **Multi-component** (`tau2_retail_components`): `gepa.seed_components` → `{"tools_markdown": ..., "mermaid_instructions": ..., "mermaid_graph": ...}`.

2. **`list_of_named_predictors`** is set to **`list(seed_candidate.keys())`** (insertion order in Python 3.7+). This defines **round-robin order** for components (see §5).

3. The engine performs an **initial full validation** pass on the seed: every validation example is evaluated, scores recorded, and the **Pareto frontier** is initialized from that.

4. **Metric calls** increase with each evaluated (example, candidate) pair according to caching and `val_evaluation_policy` (default **full_eval** every time).

---

## 3. Main loop (high level)

Implemented in `gepa.core.engine.GEPAEngine.run()`:

```
while not stopped:
    optionally: merge proposer (if enabled and scheduled)
    reflective_mutation_proposer.propose(state)
    if proposal improves minibatch and passes acceptance:
        full val evaluation of new candidate → add to pool → update Pareto
```

**Budget** stops the run when `max_metric_calls` or custom stoppers say so.

---

## 4. One reflective iteration (detail)

This is `ReflectiveMutationProposer.propose()` in `gepa.proposer.reflective_mutation.reflective_mutation`.

### 4.1 Pick parent program (candidate index)

- **`candidate_selection_strategy: pareto`** (`ParetoCandidateSelector`): chooses an index by sampling from programs that lie on the **Pareto front** across validation examples (stochastic; uses RNG).
- **`current_best`**: always the index with best aggregate val score.
- **`epsilon_greedy`**, **`top_k_pareto`**: variants (see `gepa.strategies.candidate_selector`).

So: the **reflector** does not pick the parent; **Pareto (or another selector)** does.

### 4.2 Sample a training minibatch

- **`batch_sampler`** (e.g. `epoch_shuffled`) draws **`reflection_minibatch_size`** example IDs from the **train** set.

### 4.3 Evaluate parent on minibatch **with traces**

- The **evaluator** runs for each example: retail solo simulation, reward, **`side_info`** returned as the “trajectory” for the adapter.
- If **all** scores equal `perfect_score` and **`skip_perfect_score`** is true, the iteration **skips** reflection (nothing to learn).

### 4.4 Choose which **component(s)** to update

- **`round_robin`**: for this **parent index**, GEPA keeps a cursor `named_predictor_id_to_update_next_for_program_candidate[parent_idx]`. Each iteration advances it **modulo** `len(list_of_named_predictors)`. **Exactly one** component name is returned (e.g. only `mermaid_graph` this round).
- **`all`**: **all** keys of the parent dict are updated in one iteration (one LM call per key inside `propose_new_texts`).

So: **which component is optimized when** = **round-robin order of `seed_candidate.keys()`** (for round_robin), or **all at once** (for `all`).

### 4.5 Build the **reflective dataset**

- `OptimizeAnythingAdapter.make_reflective_dataset()` groups **`side_info`** per component.
- Shared keys (score, task text, `qualitative_asi`, …) are copied; optional **`{component}_specific_info`** can add targeted fields.

### 4.6 Reflection LM proposes new text

- For each selected component, GEPA fills **`reflection_prompt_template`**:
  - Single string: same template for all components.
  - **`dict[str, str]`** (multi-component retail): **per-component** template, e.g. `# Optimizer tools_markdown` vs `# Optimizer mermaid_graph` in `reflection_prompts_components.md`.
- Placeholders: **`<curr_param>`** = current text for that component; **`<side_info>`** = serialized feedback for that minibatch.
- **`objective` / `background`** from markdown are already substituted when templates are built in `main.py`.

Output is **`new_instruction`** per component (raw model output).

### 4.7 Merge into full component text (**`gepa_generated_template`**)

- For each updated component, if **`gepa_generated_template`** is set:
  - **String**: same merge wrapper for all keys (legacy single-file policy).
  - **`dict[str, str]`** (`tau2_retail_components`): **per-component** file containing `<gepa_generated>`; the proposed text is spliced in (`_apply_gepa_generated_template`).

### 4.8 Minibatch acceptance gate

- The **child** candidate (parent copy + edited fields) is evaluated on the **same minibatch** without traces.
- **Accept** only if **sum of scores strictly increases** vs the parent on that minibatch. Otherwise the proposal is discarded (no new pool entry this iteration).

### 4.9 Full validation and Pareto update

- On acceptance, the engine runs **`_run_full_eval_and_add`**: full **validation** set evaluation, then **`state.update_state_with_new_program`**.
- The new program gets an index; **Pareto front** mappings are updated so non-dominated programs per validation example remain.

---

## 5. Multi-component integration (`tau2_retail_components`)

| Piece | Role |
|-------|------|
| **`gepa.fixed_retail_agent_rules_path`** | **Not** a GEPA component. Markdown read once and **prepended** to every assembled policy (e.g. one-shot mode, single-user rules). Edit the file to change; optimizer never mutates it. |
| `tools_markdown` | Seed / optimized markdown; written to a **temporary** `.md` path; **`assistant.mcp_tools_markdown_path`** points to it. |
| `mermaid_instructions` | **Only** how to interpret/navigate the mermaid diagram. Concatenated **after** the fixed retail rules. |
| `mermaid_graph` | SOP Global + **Node Policies** + `## SOP Flowchart` (fenced `mermaid`). Concatenated last. |

**Assembly order:** `fixed_retail_agent_rules` → `mermaid_instructions` → `mermaid_graph`.

**Reflection** uses **three** component templates (or default `# Optimizer` + `# Optimizer <name>`).

**GEPA YAML** uses:

- `gepa.fixed_retail_agent_rules_path` — fixed prefix file (required for this entrypoint).
- `gepa.seed_components` — paths to seed files per key.
- `gepa.gepa_merge_templates` — merge files per key (`<gepa_generated>`).

---

## 6. Retail-specific evaluator (executor + optional diagnosis)

In `gepa/examples/tau2_retail_mermaid/utils.py` (and **components** variant):

1. **Solo agent** runs the ticket with **`policy_text`** (assembled or single string).
2. **Score**: typically **1.0** success / **0.0** failure from retail evaluation.
3. **`side_info`**: task id, ticket, DB flags, **`candidate_preview`**, and on failure either **rule-based** lines or a **diagnosis LLM** using `# Evaluator` markdown (`$task_desc`, `$tools_list`, `$reward_info`, `$trace`, `$policy_preview`).

That diagnosis output is often stored as **`qualitative_asi`**, which flows into **reflection** as part of `<side_info>`.

So: **one** “task” evaluator + **optional second** LLM for richer failure text; both serve the **reflector**, not Pareto directly.

---

## 7. How Pareto is maintained

- **Frontier type** (`config.engine.frontier_type`, e.g. `hybrid`, `instance`, `objective`) controls how **multi-objective** or **per-example** dominance is computed (see GEPA docs / `GEPAState`).
- For each **validation example id**, the engine tracks which **candidate indices** are **non-dominated** among programs that have been evaluated on that id (`program_at_pareto_front_valset` / related structures).
- **`ParetoCandidateSelector`** samples a **parent index** from those **Pareto-optimal** programs (weighted by tracked scores), so reflection explores **diverse** strong programs instead of only the single highest aggregate.
- When a **new** candidate is added, **dominated** entries can drop off the front; **callbacks** like `on_pareto_front_updated` expose before/after for logging (e.g. Logfire).

**`best_idx` / aggregate score** in results use `val_evaluation_policy` (default **full_eval**): e.g. linear scalarization over the valset for reporting **best_candidate**.

---

## 8. Optional merge (not used in default retail configs)

If **`merge_enabled`**: on some iterations, **`MergeProposer`** may combine two parents from the frontier; the child is accepted only if it beats **both** parents on the merge minibatch, then full val + Pareto update. Retail YAMLs usually keep **`merge_enabled: false`**.

---

## 9. Quick reference: YAML knobs (retail)

| YAML key | Effect |
|----------|--------|
| `gepa.optimization_mode` | `single_task` / `multi_task` / `generalization` → dataset/valset for `optimize_anything`. |
| `gepa.reflection_minibatch_size` | Minibatch size for parent eval + proposal check. |
| `gepa.reflection_module_selector` | `round_robin` (one component per iteration) vs `all`. |
| `gepa.candidate_selection_strategy` | `pareto` vs `current_best` vs … |
| `gepa.frontier_type` | How Pareto sets are computed per val example / objective. |
| `gepa.max_metric_calls` | Primary budget cap. |
| `gepa.cache_evaluation` | Reuse (candidate, example) scores when safe. |

---

## 10. File map (this repo)

| Area | Location |
|------|----------|
| `optimize_anything` entry + config wiring | `gepa/src/gepa/optimize_anything.py` |
| Main loop + Pareto add | `gepa/src/gepa/core/engine.py` |
| Reflective proposal + component merge | `gepa/src/gepa/proposer/reflective_mutation/reflective_mutation.py` |
| Pareto parent sampling | `gepa/src/gepa/strategies/candidate_selector.py` |
| Round-robin / all components | `gepa/src/gepa/strategies/component_selector.py` |
| Evaluator → trajectories / reflective rows | `gepa/src/gepa/adapters/optimize_anything_adapter/optimize_anything_adapter.py` |
| Single-file retail GEPA driver | `gepa/examples/tau2_retail_mermaid/main.py` |
| Multi-component retail GEPA driver | `gepa/examples/tau2_retail_components/main.py` |
| Retail eval + diagnosis | `gepa/examples/tau2_retail_mermaid/utils.py`, `gepa/examples/tau2_retail_components/utils.py` |

---

*This describes the vendored `gepa` under `./gepa` in tau2-mermaid; always use that package with `uv` so behavior matches these paths.*
