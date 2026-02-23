# Mermaid SOP Conversion Prompt

## Instructions

Convert the above agent prompt into a structured SOP using a Mermaid flowchart. The graph will be served to the agent in two layers:

1. **Skeleton graph** in the system prompt — topology only, no detailed instructions. Node IDs and decision text are the only readable content.
2. **Full node detail** delivered progressively via `goto_node()` at runtime.

Because of this, **node IDs must be self-documenting** — they are the primary way the agent understands the flow from the skeleton alone.

Guidelines:
- **Convert** procedural instructions (if/then logic, decision trees, multi-step workflows) into the flowchart
- **Keep as prose** anything that's global context, tone guidance, or doesn't map naturally to a flow
- Don't over-decompose — trust the model to handle straightforward steps without spelling out every micro-action. A node can represent a meaningful *chunk* of work, not just one atomic action.

## Node ID Conventions

Node IDs follow a `PREFIX_DOMAIN` pattern so the skeleton graph reads as a narrative.

**Prefixes by purpose:**

| Prefix | Purpose | Example |
|--------|---------|---------|
| `AUTH_` | Authentication / identity verification | `AUTH_EMAIL`, `AUTH_USER` |
| `ROUTE_` | Intent routing / top-level dispatch | `ROUTE_INTENT` |
| `CHK_` | Status check / validation before action | `CHK_CANCEL`, `CHK_RETURN` |
| `IS_` | Boolean condition / status decision | `IS_PENDING`, `IS_DELIVERED` |
| `COLLECT_` | Gather inputs from user | `COLLECT_CANCEL`, `COLLECT_RETURN` |
| `DO_` | Execute an action / call a write tool | `DO_CANCEL`, `DO_EXCHANGE` |
| `END_` | Successful / neutral terminal | `END_CANCEL`, `END_MODIFY` |
| `DENY_` | Rejection terminal | `DENY_CANCEL`, `DENY_RETURN` |
| `ESCALATE_` | Handoff terminal | `ESCALATE_HUMAN` |

**Domain suffixes** identify the flow: `_CANCEL`, `_RETURN`, `_EXCH`, `_MOD`, `_PAY`, `_ADDR`, etc.

**Reading test:** A skeleton path like `START → AUTH → ROUTE → CHK_CANCEL → IS_PENDING → COLLECT_CANCEL → DO_CANCEL → END_CANCEL` should tell the story without any node text.

**Decision nodes** (`{rhombus}`) keep their text in the skeleton since it defines the branching logic, so their IDs are less critical but should still follow conventions: `IS_PENDING`, `ROUTE_INTENT`, `MOD_TYPE`.

## Mermaid Conventions

**Format:** Always `flowchart TD`, starting with `START([User contacts Agent])`

**Node shapes by purpose:**

| Shape | Syntax | Use for |
|-------|--------|---------|
| Stadium | `([text])` | Start, end, and terminal outcomes |
| Rectangle | `[text]` | Actions, steps, collecting info |
| Rhombus | `{text}` | Decisions, intent routing |
| Parallelogram | `[/text/]` | Annotations — reminders, guardrails, and tool calls |

**Parallelogram prefixes** — use these to distinguish annotation types:
- `[/REMINDER: ...text.../]` — guardrails, do's and don'ts, important caveats
- `[/TOOLS: tool_name/]` — tool calls (only if tools are specified in source prompt)
- Prefixes can be combined in one node: `[/REMINDER: Verify first. TOOLS: get_order/]`

> ⚠️ **Mermaid syntax gotcha:** Parentheses `()` inside ANY node text cause parse errors — Mermaid interprets them as shape delimiters. Use `"double quotes"` or rephrase. E.g. `[/TOOLS: cancel_order/]` not `[/TOOLS: cancel_order(id)/]`

**Links and branching:**
- Arrow for flow: `A --> B`
- Condition on link: `A -->|order.status == 'shipped'| B`
- Default/fallback: `A -->|else| B`
- Error/escalation (dotted): `A -.->|unresolved| ESCALATE_HUMAN([Escalate to human])`

**Collecting inputs** — use markdown in backtick-quoted node text. **Bold** = required, *italic* = optional:

```
COLLECT_ORDER["`Collect from customer:
1. **order_id**
2. *zip code*`"]
```

**Terminal nodes** should encode the outcome clearly:
`DENY_RETURN([DENY: Return window expired])`, `END_REFUND([Issue refund + confirm via email])`, `END_RESTART([End / Restart])`

## Skeleton Generation

The skeleton is auto-generated from the full graph for the system prompt. It strips:
- All text content from rectangle nodes (keeps only node ID)
- All annotation/parallelogram nodes (REMINDER, TOOLS)
- Backtick-quoted markdown content

It preserves:
- All node IDs and edge connections
- Decision node text (`{rhombus}`)
- Edge conditions/labels
- Terminal node text (`([stadium])`)

Example — full graph:
```
COLLECT_CANCEL["`Collect and confirm:
1. **order_id**
2. **reason**: 'no longer needed' OR 'ordered by mistake'`"]
COLLECT_CANCEL --> WARN_CANCEL[/REMINDER: Only these two reasons are acceptable/]
WARN_CANCEL --> DO_CANCEL[/TOOLS: cancel_pending_order/]
DO_CANCEL --> END_CANCEL([End / Restart])
```

Becomes skeleton:
```
COLLECT_CANCEL --> DO_CANCEL --> END_CANCEL([End / Restart])
```

The agent sees the flow structure and can plan ahead. Detail arrives via `goto_node()` when the agent reaches each node.

## Output Format

The conversion should produce a single markdown file with this structure:

```markdown
# {Agent Name}

## Role
One-line description of what this agent does.

## Global Rules
Prose bullet points — behavioral constraints that apply throughout:
- Single user per conversation, tone, confirmation requirements, etc.
- Things that don't map to flowchart logic.

## Domain Reference
Structured reference material the agent needs — entities, statuses, 
attributes, enums. Use compact formatting (tables, comma-separated lists).
Not procedural — just lookup data.

## SOP Flowchart

Legend: `([stadium])` = start/end, `[rectangle]` = action/collect info, 
`{rhombus}` = decision. **Bold** = required input, *italic* = optional. 
Annotations (reminders, tool hints) are delivered progressively via `goto_node`.

\`\`\`mermaid
flowchart TD
    START([User contacts Agent]) --> ...
\`\`\`
```

**Section rules:**
- **Role** — keep to one sentence. The agent should know its identity immediately.
- **Global Rules** — only what applies *everywhere*. If a rule is specific to one flow (e.g. "cancel reasons must be X or Y"), put it in the flowchart as a REMINDER annotation, not here.
- **Domain Reference** — no procedures, no if/then logic. Just the data model. If it has a conditional, it belongs in the flowchart.
- **SOP Flowchart** — the full mermaid graph with all node detail, annotations, and edge conditions. This is the source of truth that `load_graph` will parse.

## Key Principles

1. **One flat graph** — no subgraphs. If the flow exceeds ~20 nodes, simplify by consolidating related steps into single descriptive nodes rather than splitting into subgraphs.
2. **Node IDs are the skeleton's narrative** — use the PREFIX_DOMAIN convention so the stripped graph remains readable and plannable.
3. **Node text is the instruction** — write node labels as what the agent should *do*, not abstract labels. `Verify ID and check eligibility` > `Step 3`.
4. **Every decision node must have all exit paths defined**, including a fallback/else.
5. **Annotations attach to the node they govern** — place parallelogram nodes directly after the relevant action node, linked inline. These are stripped in the skeleton but delivered by `goto_node()`.
6. **Don't invent tools or API calls** — only use `TOOLS:` prefixed nodes if the source prompt explicitly references tools, APIs, or function calls.
7. **Terminal nodes as completion markers** — terminal node IDs (e.g. `END_CANCEL`, `DENY_RETURN`) are used as `completion_node` values in the todo system. Name them clearly to reflect the outcome.
