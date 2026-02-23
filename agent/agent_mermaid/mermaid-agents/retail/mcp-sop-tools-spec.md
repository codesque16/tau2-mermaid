# MCP Tools Spec: SOP Graph Navigation

A lightweight MCP toolset that turns Mermaid SOP flowcharts into a progressive discovery system for AI agents. Three tools work together: `load_graph` parses the graph, `todo` manages multi-intent planning, and `goto_node` delivers instructions progressively.

## Architecture Overview

```
┌─────────────────────────────────────────────┐
│  System Prompt                              │
│  ┌───────────────┐  ┌───────────────────┐   │
│  │ Global Rules   │  │ Skeleton Graph    │   │
│  │ Domain Ref     │  │ (topology only,   │   │
│  │ Legend          │  │  no detail)       │   │
│  └───────────────┘  └───────────────────┘   │
└─────────────────────────────────────────────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
     ┌─────────┐ ┌─────────┐ ┌──────────┐
     │  todo   │ │goto_node│ │load_graph│
     │ planning│ │execution│ │  setup   │
     └─────────┘ └─────────┘ └──────────┘
```

## Tool 1: `load_graph`

Parses a Mermaid SOP flowchart and returns a skeleton for the system prompt plus an internal full graph for `goto_node` to query.

Called once at conversation start or when the agent is initialized.

### Input

```json
{
  "name": "load_graph",
  "description": "Parse a Mermaid SOP flowchart. Returns a skeleton graph for the system prompt and stores the full graph for goto_node lookups.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "graph_id": {
        "type": "string",
        "description": "Unique identifier for this graph, e.g. 'retail_support_v1'"
      },
      "mermaid_source": {
        "type": "string",
        "description": "Raw mermaid flowchart TD source text"
      }
    },
    "required": ["graph_id", "mermaid_source"]
  }
}
```

### Output

```json
{
  "graph_id": "retail_support_v1",
  "skeleton": "flowchart TD\n  START --> AUTH\n  AUTH --> ROUTE{User intent?}\n  ROUTE -->|cancel| CHK_CANCEL --> IS_PENDING_C{status?}\n  ...",
  "entry_node": "START",
  "node_count": 42,
  "decision_nodes": ["ROUTE", "IS_PENDING_C", "IS_PENDING_M", "MOD_TYPE", "CHK_GC_PAY", "IS_DELIVERED_R", "IS_DELIVERED_E"],
  "terminal_nodes": ["END_INFO", "END_CANCEL", "END_MOD", "END_RET", "END_EXCH", "END_UADDR", "DENY_CANCEL", "DENY_MOD", "DENY_RET", "DENY_EXCH", "DENY_PAY", "END_TRANSFER"]
}
```

### Skeleton generation rules

The skeleton strips:
- All text content from rectangle nodes (keeps only node ID)
- All annotation/parallelogram nodes (REMINDER, TOOLS)
- Backtick-quoted markdown content

The skeleton preserves:
- All node IDs and edge connections
- Decision node text (rhombus `{text}`)
- Edge conditions/labels
- Terminal node text (stadium `([text])`)
- Graph topology and structure


---

## Tool 2: `todo`

A task planner for multi-intent conversations, modeled after Claude Code's `TodoWrite`. The agent writes the entire todo list on each call. Internal to the agent, not exposed to the user.

### Input

```json
{
  "name": "todo",
  "description": "Create and manage a structured task list for the current conversation. Write the full updated list on each call. Use to capture, prioritize, and track progress on user requests. Tasks are internal to the agent and not shown to the user.",
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
              "description": "Optional context, dependencies, or findings from other tasks. Use to carry information across tasks, e.g. 'Use 456 Elm Ave — found from luggage order #W9834521'"
            },
            "status": {
              "type": "string",
              "enum": ["pending", "in_progress", "completed"],
              "description": "Current task status"
            },
            "completion_node": {
              "type": "string",
              "description": "The terminal SOP node ID that marks this task as done, e.g. 'END_MOD'. When goto_node reaches a completion_node, it reminds the agent to update todos."
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
// Initial capture — user states three requests
{
  "todos": [
    {"content": "Change shipping address on pending order", "status": "pending", "completion_node": "END_MOD"},
    {"content": "Update default user address", "status": "pending", "completion_node": "END_UADDR"},
    {"content": "Exchange tablet for cheapest option", "status": "pending", "completion_node": "END_EXCH"}
  ]
}

// Task 1 in progress — discovered address from luggage order, noted on task 2 for later
{
  "todos": [
    {"content": "Change shipping address on pending order", "status": "in_progress", "completion_node": "END_MOD"},
    {"content": "Update default user address", "status": "pending", "completion_node": "END_UADDR", "note": "Use 456 Elm Ave from luggage order #W9834521"},
    {"content": "Exchange tablet for cheapest option", "status": "pending", "completion_node": "END_EXCH"}
  ]
}

// Agent reaches END_MOD via goto_node → response includes todo reminder → agent updates:
{
  "todos": [
    {"content": "Change shipping address on pending order", "status": "completed", "completion_node": "END_MOD"},
    {"content": "Update default user address", "status": "in_progress", "completion_node": "END_UADDR", "note": "Use 456 Elm Ave from luggage order #W9834521"},
    {"content": "Exchange tablet for cheapest option", "status": "pending", "completion_node": "END_EXCH"}
  ]
}

// All done
{
  "todos": [
    {"content": "Change shipping address on pending order", "status": "completed", "completion_node": "END_MOD"},
    {"content": "Update default user address", "status": "completed", "completion_node": "END_UADDR"},
    {"content": "Exchange tablet for cheapest option", "status": "completed", "completion_node": "END_EXCH"}
  ]
}
```

### Output

Returns the list as written with a summary:

```json
{
  "todos": [
    {"content": "Change shipping address on pending order", "status": "in_progress", "completion_node": "END_MOD"},
    {"content": "Update default user address", "status": "pending", "completion_node": "END_UADDR", "note": "Use 456 Elm Ave from luggage order #W9834521"},
    {"content": "Exchange tablet for cheapest option", "status": "pending", "completion_node": "END_EXCH"}
  ],
  "summary": {"pending": 2, "in_progress": 1, "completed": 0}
}
```

---

## Tool 3: `goto_node`

Progressive delivery of node instructions. The agent calls this to move through the graph, receiving full detail only for the current position.

### Input

```json
{
  "name": "goto_node",
  "description": "Move to a node in the SOP graph. Returns the full node instructions plus all annotation nodes (REMINDER, TOOLS) between the current node and the next decision or action node. Validates that the transition is legal from the current position.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "graph_id": {
        "type": "string",
        "description": "Graph identifier from load_graph"
      },
      "node_id": {
        "type": "string",
        "description": "Target node ID to move to"
      }
    },
    "required": ["graph_id", "node_id"]
  }
}
```

### Output

```json
{
  "node": {
    "id": "COLLECT_CANCEL",
    "type": "rectangle",
    "text": "Collect and confirm:\n1. **order_id**\n2. **reason**: 'no longer needed' OR 'ordered by mistake'"
  },
  "annotations": [
    {"type": "REMINDER", "text": "Only these two reasons are acceptable"},
    {"type": "TOOLS", "text": "cancel_pending_order"}
  ],
  "edges": [
    {"to": "REFUND_CANCEL", "condition": null}
  ],
  "path": ["START", "AUTH", "ROUTE", "CHK_CANCEL", "IS_PENDING_C", "COLLECT_CANCEL"],
  "valid": true
}
```

The `path` is managed by the harness — the agent does not need to track its own position. The harness uses the path for transition validation and includes it in every response for observability.

### Annotation bundling

When the agent calls `goto_node`, the response bundles all consecutive annotation (parallelogram) nodes that follow the target node up to the next non-annotation node. This means:

```mermaid
COLLECT_CANCEL --> WARN_CANCEL[/REMINDER: Only two reasons/]
WARN_CANCEL --> DO_CANCEL[/TOOLS: cancel_pending_order/]
DO_CANCEL --> REFUND_CANCEL[/REMINDER: Gift card immediate, others 5-7 days. TOOLS: calculate/]
REFUND_CANCEL --> END_CANCEL([End / Restart])
```

A `goto_node("COLLECT_CANCEL")` returns all three annotations bundled, and `edges` points to `END_CANCEL` (the next non-annotation node the agent would actually transition to).

### Transition validation

`goto_node` validates that the requested node is reachable from the agent's current position:

- **Valid**: The target node is a direct edge from current position, or is the entry node `START`
- **Invalid**: The target node is not reachable — response includes `valid: false` with the list of valid next nodes

```json
// Invalid transition
{
  "valid": false,
  "error": "Cannot reach COLLECT_EXCH from IS_PENDING_C",
  "current_node": "IS_PENDING_C",
  "valid_next": ["DENY_CANCEL", "COLLECT_CANCEL"]
}
```

### Special behaviors

- `goto_node("START")` — always valid, resets the path. Used when looping back after "End / Restart" terminals.
- `goto_node` on a decision node — returns the decision text and all edges with conditions, but no annotations (decisions don't have them).
- `goto_node` on a terminal node — returns the terminal text, marks the path as complete, and checks if this node matches any `completion_node` in the todo list. If so, includes a reminder:

```json
// goto_node reaches END_MOD, which matches a todo completion_node
{
  "node": {"id": "END_MOD", "type": "stadium", "text": "End / Restart"},
  "annotations": [],
  "edges": [],
  "path": ["START", "AUTH", "ROUTE", "CHK_MOD", "IS_PENDING_M", "MOD_ADDR", "DO_MOD_ADDR", "END_MOD"],
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
Agent calls: goto_node("retail_v1", "START")
→ {node: START, edges: [{to: "AUTH"}]}

Agent calls: goto_node("retail_v1", "AUTH")  
→ {node: {text: "Authenticate via email OR name + zip code"}, 
   annotations: [{type: "REMINDER", text: "Must verify even if user provides user_id. TOOLS: find_user_id_by_email, find_user_id_by_name_zip"}],
   edges: [{to: "ROUTE"}]}
```

Agent asks for email → user provides sophia.martin4832@example.com → agent calls `find_user_id_by_email`.

### Phase 3: Task 1 — Change order address

```
Agent calls: goto_node("retail_v1", "ROUTE")  → decision node, shows all edges
Agent calls: goto_node("retail_v1", "CHK_MOD") 
→ {annotations: [{type: "TOOLS", text: "get_order_details"}], edges: [{to: "IS_PENDING_M"}]}
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

Agent proceeds through `MOD_ADDR` → `DO_MOD_ADDR` → reaches `END_MOD`:

```
Agent calls: goto_node("retail_v1", "END_MOD")
→ {node: {text: "End / Restart"}, 
   todo_reminder: "Reached completion node END_MOD. Update todos and proceed to next task."}
```

The `todo_reminder` prompts the agent to update:

```
Agent calls: todo({todos: [
  {content: "Change shipping address on pending order", status: "completed", completion_node: "END_MOD"},
  {content: "Update default user address", status: "in_progress", completion_node: "END_UADDR", note: "Use 456 Elm Ave from luggage order #W9834521"},
  {content: "Exchange tablet for cheapest option", status: "pending", completion_node: "END_EXCH"}
]})
```

### Phase 4: Task 2 — Update default address

```
Agent calls: goto_node("retail_v1", "ROUTE")
Agent calls: goto_node("retail_v1", "MOD_USER_ADDR")
→ {node: {text: "Collect: 1. user_id 2. new address"}, 
   annotations: [{type: "TOOLS", text: "modify_user_address"}]}
```

Agent reads the note on task 2 — already has the address. Doesn't ask user again.

```
Agent calls: goto_node("retail_v1", "END_UADDR")
→ {todo_reminder: "Reached completion node END_UADDR. Update todos and proceed to next task."}

Agent calls: todo({todos: [
  {content: "Change shipping address on pending order", status: "completed", completion_node: "END_MOD"},
  {content: "Update default user address", status: "completed", completion_node: "END_UADDR"},
  {content: "Exchange tablet for cheapest option", status: "in_progress", completion_node: "END_EXCH"}
]})
```

### Phase 5: Task 3 — Exchange tablet

```
Agent calls: goto_node("retail_v1", "ROUTE")  
Agent calls: goto_node("retail_v1", "CHK_EXCH")
→ {annotations: [{type: "TOOLS", text: "get_order_details"}], edges: [{to: "IS_DELIVERED_E"}]}
```

Agent finds tablet order, checks delivered status.

```
Agent calls: goto_node("retail_v1", "COLLECT_EXCH")
→ {node: {text: "Collect ALL items to exchange..."}, 
   annotations: [
     {type: "REMINDER", text: "Remind user to confirm ALL items. No new order needed."},
     {type: "TOOLS", text: "calculate, exchange_delivered_order_items"}
   ]}
```

Agent calls `get_product_details` to find cheapest tablet variant, uses `calculate` for price difference, confirms with user, executes exchange.

```
Agent calls: goto_node("retail_v1", "END_EXCH")
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
- `load_graph` stores the parsed graph in server-side session state, keyed by `graph_id`
- `todo` list is stored per-conversation in session state
- `goto_node` tracks the current path per `graph_id` in session state
- All state is conversation-scoped — no persistence across conversations

### Mermaid parser requirements
The parser needs to handle:
- `flowchart TD` format
- Node shapes: `([stadium])`, `[rectangle]`, `{rhombus}`, `[/parallelogram/]`
- Backtick-quoted markdown content in nodes: `` ["`...`"] ``
- Edge labels: `-->|condition|`
- Dotted edges: `-.->|condition|`
- Comments: `%% ...`

### Error handling
- `load_graph` with invalid mermaid → return parse errors with line numbers
- `goto_node` with unknown `graph_id` → "Graph not loaded. Call load_graph first."
- `goto_node` with unknown `node_id` → "Node not found. Valid nodes: [...]"
- `todo` operations on non-existent IDs → "Task not found."

### Performance considerations
- Skeleton generation strips ~60-70% of token content from a typical SOP graph
- `goto_node` responses are small — typically under 200 tokens
- `todo` operations are trivial — no computation, just state management
- The main cost is one `load_graph` call at startup to parse the mermaid
