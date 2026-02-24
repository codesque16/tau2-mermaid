# MCP Tools Spec: SOP Graph Navigation

A lightweight MCP toolset that turns Mermaid SOP markdown files into a progressive discovery system for AI agents. Three tools work together: `load_graph` parses the agent definition file, `todo` manages multi-intent planning, and `goto_node` delivers instructions progressively.

## Architecture Overview

```
┌──────────────────────────────────────────────────┐
│  Agent SOP File (single .md)                     │
│  ┌────────────┐ ┌──────────┐ ┌──────────────┐   │
│  │ Frontmatter │ │ Mermaid  │ │ Node Prompts │   │
│  │ config,     │ │ graph    │ │ yaml + prose  │   │
│  │ model, MCP  │ │          │ │ per node      │   │
│  └────────────┘ └──────────┘ └──────────────┘   │
└──────────────────────────────────────────────────┘
                      │
                  load_graph
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
  ┌──────────────┐          ┌──────────┐
  │ System Prompt│          │ Server   │
  │ Global Rules │          │ State    │
  │ Domain Ref   │          │ (graph,  │
  │ Mermaid      │          │  node    │
  │ (as-is)      │          │  prompts)│
  └──────────────┘          └──────────┘
          │                       │
          ▼                       ▼
     ┌─────────┐           ┌──────────┐
     │  todo   │           │goto_node │
     │ planning│           │execution │
     └─────────┘           └──────────┘
```

---

## Tool 1: `load_graph`

Parses an agent SOP markdown file — frontmatter, mermaid graph, and node prompts. The mermaid graph goes directly into the system prompt. Node prompts are stored server-side for `goto_node` to deliver progressively.

Called once at agent initialization.

### Input

```json
{
  "name": "load_graph",
  "description": "Parse an agent SOP markdown file. Extracts the mermaid graph for the system prompt and stores node prompts for progressive delivery via goto_node.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "sop_file": {
        "type": "string",
        "description": "Path or URL to the agent SOP markdown file"
      }
    },
    "required": ["sop_file"]
  }
}
```

### Output

```json
{
  "agent": "retail_customer_support",
  "version": "1.0",
  "entry_node": "START",
  "model": {
    "provider": "anthropic",
    "name": "claude-sonnet-4-5-20250929",
    "temperature": 0.2,
    "max_tokens": 1024
  },
  "mcp_servers": [
    {"name": "retail-tools", "url": "https://mcp.retailco.com/sse"}
  ],
  "graph": {
    "node_count": 30,
    "decision_nodes": ["ROUTE", "IS_PENDING_C", "IS_PENDING_M", "MOD_TYPE", "IS_GC_OK", "IS_DELIVERED_R", "IS_DELIVERED_E"],
    "terminal_nodes": ["END_INFO", "END_CANCEL", "END_MOD", "END_RETURN", "END_EXCH", "END_UADDR", "DENY_CANCEL", "DENY_MOD", "DENY_RETURN", "DENY_EXCH", "DENY_PAY", "ESCALATE_HUMAN"],
    "nodes_with_prompts": ["AUTH", "INFO", "CHK_CANCEL", "COLLECT_CANCEL", "DO_CANCEL", "CHK_MOD", "COLLECT_MOD_ITEMS", "DO_MOD_ITEMS", "COLLECT_RETURN", "DO_RETURN", "COLLECT_EXCH", "DO_EXCH", "ESCALATE_HUMAN"]
  },
  "system_prompt_sections": ["Role", "Global Rules", "Domain Reference", "SOP Flowchart"]
}
```

### What `load_graph` does

1. **Parses frontmatter** — extracts agent config, model settings, MCP servers, tool list
2. **Extracts mermaid graph** — validates syntax, builds node/edge graph structure
3. **Parses Node Prompts** — matches `### NODE_ID` sections, extracts yaml metadata (tools, examples) and prompt text
4. **Validates consistency** — tools referenced in node prompts exist in frontmatter `tools` list, node IDs in prompts match mermaid node IDs
5. **Builds system prompt** — combines Role, Global Rules, Domain Reference, and mermaid graph (as-is) for the agent's system prompt
6. **Stores node prompts server-side** — keyed by node ID for `goto_node` delivery

---

## Tool 2: `todo`

A task planner for multi-intent conversations, modeled after Claude Code's `TodoWrite`. The agent writes the entire todo list on each call. Internal to the agent, not exposed to the user.

### Input

```json
{
  "name": "todo",
  "description": "Create and manage a structured task list for the current conversation. Write the full updated list on each call. Tasks are internal to the agent and not shown to the user.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "todos": {
        "description": "The updated todo list",
        "items": {
          "properties": {
            "content": {
              "type": "string",
              "minLength": 1,
              "description": "Task description"
            },
            "note": {
              "type": "string",
              "description": "Optional context, dependencies, or findings from other tasks"
            },
            "status": {
              "type": "string",
              "enum": ["pending", "in_progress", "completed"],
              "description": "Current task status"
            },
            "completion_node": {
              "type": "string",
              "description": "Terminal SOP node ID that marks this task as done. When goto_node reaches this node, it reminds the agent to update todos."
            }
          },
          "required": ["content", "status"],
          "type": "object"
        },
        "type": "array"
      }
    },
    "required": ["todos"]
  }
}
```

### Usage pattern

The agent writes the full list each time, updating statuses, notes, and reordering as needed:

```json
// Initial capture
{
  "todos": [
    {"content": "Change shipping address on pending order", "status": "pending", "completion_node": "END_MOD"},
    {"content": "Update default user address", "status": "pending", "completion_node": "END_UADDR"},
    {"content": "Exchange tablet for cheapest option", "status": "pending", "completion_node": "END_EXCH"}
  ]
}

// Task 1 in progress — discovered address, noted on task 2
{
  "todos": [
    {"content": "Change shipping address on pending order", "status": "in_progress", "completion_node": "END_MOD"},
    {"content": "Update default user address", "status": "pending", "completion_node": "END_UADDR", "note": "Use 456 Elm Ave from luggage order #W9834521"},
    {"content": "Exchange tablet for cheapest option", "status": "pending", "completion_node": "END_EXCH"}
  ]
}

// Agent reaches END_MOD → todo_reminder → agent updates
{
  "todos": [
    {"content": "Change shipping address on pending order", "status": "completed", "completion_node": "END_MOD"},
    {"content": "Update default user address", "status": "in_progress", "completion_node": "END_UADDR", "note": "Use 456 Elm Ave from luggage order #W9834521"},
    {"content": "Exchange tablet for cheapest option", "status": "pending", "completion_node": "END_EXCH"}
  ]
}
```

### Output

```json
{
  "todos": [...],
  "summary": {"pending": 2, "in_progress": 1, "completed": 0}
}
```

---

## Tool 3: `goto_node`

Progressive delivery of node instructions. The agent calls this to move through the graph. Returns the node description (from mermaid), the full prompt and metadata (from Node Prompts), and valid next edges.

### Input

```json
{
  "name": "goto_node",
  "description": "Move to a node in the SOP graph. Returns the node description, prompt, tools, examples, and valid next edges. Validates that the transition is legal from the current position.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "node_id": {
        "type": "string",
        "description": "Target node ID to move to"
      }
    },
    "required": ["node_id"]
  }
}
```

### Output

```json
{
  "node": {
    "id": "COLLECT_CANCEL",
    "type": "rectangle",
    "description": "Collect: order_id, reason",
    "prompt": "Collect and confirm:\n1. **order_id**\n2. **reason**: must be 'no longer needed' OR 'ordered by mistake'\n\n- If user gives a different reason, politely explain only these two are accepted\n- Do not suggest which reason to pick\n- If user is unsure, offer to help with return or exchange instead",
    "tools": ["cancel_pending_order"],
    "examples": [
      {"user": "I want to cancel order 123", "agent": "I can help with that. Could you tell me the reason — is it 'no longer needed' or 'ordered by mistake'?"}
    ]
  },
  "edges": [
    {"to": "DO_CANCEL", "condition": null}
  ],
  "path": ["START", "AUTH", "ROUTE", "CHK_CANCEL", "IS_PENDING_C", "COLLECT_CANCEL"],
  "valid": true
}
```

The `path` is managed by the harness — the agent does not need to track its own position. The harness uses the path for transition validation and includes it in every response for observability.

**Nodes without prompts** return just the description from the mermaid:

```json
{
  "node": {
    "id": "IS_PENDING_C",
    "type": "rhombus",
    "description": "status == pending?"
  },
  "edges": [
    {"to": "DENY_CANCEL", "condition": "no"},
    {"to": "COLLECT_CANCEL", "condition": "yes"}
  ],
  "path": [...],
  "valid": true
}
```

### Transition validation

`goto_node` validates that the requested node is reachable from the current position:

- **Valid**: Target node is a direct edge from current position, or is `START`
- **Invalid**: Target node is not reachable — response includes valid alternatives

```json
{
  "valid": false,
  "error": "Cannot reach COLLECT_EXCH from IS_PENDING_C",
  "current_node": "IS_PENDING_C",
  "valid_next": ["DENY_CANCEL", "COLLECT_CANCEL"]
}
```

### Special behaviors

- `goto_node("START")` — always valid, resets the path
- `goto_node("ROUTE")` — always valid, allows looping back for multi-intent conversations
- `goto_node` on a terminal node — marks the path as complete and checks if this node matches any `completion_node` in the todo list. If so, includes a reminder:

```json
{
  "node": {"id": "END_MOD", "type": "stadium", "description": "End / Restart"},
  "edges": [],
  "path": ["START", "AUTH", "ROUTE", "CHK_MOD", "IS_PENDING_M", "COLLECT_MOD_ADDR", "DO_MOD_ADDR", "END_MOD"],
  "valid": true,
  "todo_reminder": "Reached completion node END_MOD. Update todos and proceed to next task."
}
```

---

## End-to-End Example: Sophia Martin Scenario

**User says:** "Hi, I need to change the address on one of my orders, update my default address, and also exchange my tablet for a cheaper one."

### Phase 1: Planning

```
Agent calls: todo({todos: [
  {content: "Change shipping address on pending order", status: "pending", completion_node: "END_MOD"},
  {content: "Update default user address", status: "pending", completion_node: "END_UADDR"},
  {content: "Exchange tablet for cheapest option", status: "pending", completion_node: "END_EXCH"}
]})
```

Agent responds: "I can help with all three. Let me start by verifying your identity, then we'll work through each one."

### Phase 2: Authentication

```
Agent calls: goto_node("START")
→ {node: {description: "User contacts Agent"}, edges: [{to: "AUTH"}]}

Agent calls: goto_node("AUTH")
→ {node: {description: "Authenticate via email or name + zip",
          prompt: "Authenticate the user via email OR name + zip code. Must verify even if user provides user_id directly.",
          tools: ["find_user_id_by_email", "find_user_id_by_name_zip"]},
   edges: [{to: "ROUTE"}]}
```

Agent asks for email → user provides sophia.martin4832@example.com → agent calls `find_user_id_by_email`.

### Phase 3: Task 1 — Change order address

```
Agent calls: goto_node("ROUTE") → decision node, shows all edges
Agent calls: goto_node("CHK_MOD")
→ {node: {description: "Check order status", tools: ["get_order_details"]},
   edges: [{to: "IS_PENDING_M"}]}
```

Agent looks up orders, finds the one with wrong address. User refuses to give new address, insists agent look it up from luggage order.

Agent uses `get_order_details` on luggage order, extracts address. Updates todo with discovered context:

```
Agent calls: todo({todos: [
  {content: "Change shipping address on pending order", status: "in_progress", completion_node: "END_MOD"},
  {content: "Update default user address", status: "pending", completion_node: "END_UADDR", note: "Use 456 Elm Ave from luggage order #W9834521"},
  {content: "Exchange tablet for cheapest option", status: "pending", completion_node: "END_EXCH"}
]})
```

Agent proceeds through `COLLECT_MOD_ADDR` → `DO_MOD_ADDR` → reaches `END_MOD`:

```
Agent calls: goto_node("END_MOD")
→ {node: {description: "End / Restart"},
   todo_reminder: "Reached completion node END_MOD. Update todos and proceed to next task."}
```

```
Agent calls: todo({todos: [
  {content: "Change shipping address on pending order", status: "completed", completion_node: "END_MOD"},
  {content: "Update default user address", status: "in_progress", completion_node: "END_UADDR", note: "Use 456 Elm Ave from luggage order #W9834521"},
  {content: "Exchange tablet for cheapest option", status: "pending", completion_node: "END_EXCH"}
]})
```

### Phase 4: Task 2 — Update default address

```
Agent calls: goto_node("ROUTE")
Agent calls: goto_node("COLLECT_USER_ADDR")
→ {node: {description: "Collect: user_id, new address",
          prompt: "Collect:\n1. **user_id**\n2. **new default address**"},
   edges: [{to: "DO_USER_ADDR"}]}
```

Agent reads the note on task 2 — already has the address. Doesn't ask user again.

```
Agent calls: goto_node("END_UADDR")
→ {todo_reminder: "Reached completion node END_UADDR. Update todos and proceed to next task."}

Agent calls: todo({todos: [
  {content: "Change shipping address on pending order", status: "completed", completion_node: "END_MOD"},
  {content: "Update default user address", status: "completed", completion_node: "END_UADDR"},
  {content: "Exchange tablet for cheapest option", status: "in_progress", completion_node: "END_EXCH"}
]})
```

### Phase 5: Task 3 — Exchange tablet

```
Agent calls: goto_node("ROUTE")
Agent calls: goto_node("CHK_EXCH")
→ {node: {description: "Check order status", tools: ["get_order_details"]},
   edges: [{to: "IS_DELIVERED_E"}]}
```

Agent finds tablet order, checks delivered status.

```
Agent calls: goto_node("COLLECT_EXCH")
→ {node: {description: "Collect: order_id, all item exchanges",
          prompt: "Collect ALL items to exchange at once:\n1. **order_id**\n2. **list of item_id → new_item_id**...\n\n- Remind user to confirm ALL items\n- No new order needed",
          tools: ["calculate", "exchange_delivered_order_items"],
          examples: [{user: "I want to swap my tablet for a different one", agent: "Sure! Which variant would you like instead?..."}]},
   edges: [{to: "DO_EXCH"}]}
```

Agent calls `get_product_details` to find cheapest tablet variant, uses `calculate` for price difference, confirms with user, executes exchange.

```
Agent calls: goto_node("END_EXCH")
→ {todo_reminder: "Reached completion node END_EXCH. Update todos and proceed to next task."}

Agent calls: todo({todos: [
  {content: "Change shipping address on pending order", status: "completed", completion_node: "END_MOD"},
  {content: "Update default user address", status: "completed", completion_node: "END_UADDR"},
  {content: "Exchange tablet for cheapest option", status: "completed", completion_node: "END_EXCH"}
]})
→ {summary: {pending: 0, in_progress: 0, completed: 3}}
```

All tasks complete. Agent summarizes what was done for the user.

---

## Implementation Notes

### State management
- `load_graph` stores the parsed graph and node prompts in server-side session state
- `todo` list is stored per-conversation in session state
- `goto_node` tracks the current path in session state
- All state is conversation-scoped — no persistence across conversations

### SOP file parser requirements
The parser needs to handle:
- YAML frontmatter (top-level `---` blocks)
- Mermaid `flowchart TD` extraction from fenced code blocks
- Node shapes: `([stadium])`, `["rectangle"]`, `{rhombus}`
- Edge labels: `-->|condition|`, dotted edges: `-.->|condition|`
- Comments: `%% ...`
- `## Node Prompts` section with `### NODE_ID` subsections
- Fenced `yaml` blocks within node prompt sections for tools/examples
- Free-form markdown after yaml blocks as prompt text

### Error handling
- `load_graph` with invalid mermaid → return parse errors with line numbers
- `load_graph` with tool mismatch → warn if node references tools not in frontmatter
- `goto_node` with unknown `node_id` → "Node not found. Valid nodes: [...]"
- `todo` with empty list → accepted (clears all tasks)

### Performance considerations
- The mermaid graph goes directly in the system prompt — no skeleton transformation needed
- `goto_node` responses are small — typically under 300 tokens including prompt and examples
- `todo` operations are trivial — no computation, just state management
- The main cost is one `load_graph` call at startup to parse the markdown file
