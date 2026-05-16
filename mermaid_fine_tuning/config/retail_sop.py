"""The anchor retail SOP. Used as one-shot example in Gemini prompts
and as the primary domain in training/eval."""

RETAIL_MERMAID = """flowchart TD
    START([User contacts Agent]) --> AUTH[Authenticate user via email or name + zip code]

    AUTH -->|failed| AUTH_FAIL([Inform user — retry or end])
    AUTH -->|authenticated| ROUTE{Identify user intent}

    ROUTE -->|info request| INFO[Look up profile / order / product info]
    ROUTE -->|cancel order| CANCEL_CHECK{Order status = pending?}
    ROUTE -->|modify order| MOD_CHECK{Order status = pending?}
    ROUTE -->|return order| RETURN_CHECK{Order status = delivered?}
    ROUTE -->|exchange order| EXCHANGE_CHECK{Order status = delivered?}
    ROUTE -->|out of scope| TRANSFER[Transfer to human agent]

    INFO --> END([Ask if anything else])

    CANCEL_CHECK -->|yes| CANCEL[Collect order ID + reason — confirm — cancel]
    CANCEL_CHECK -->|no| CANCEL_DENIED([Inform user: not cancellable])
    CANCEL --> END

    MOD_CHECK -->|yes| MOD_ROUTE{What to modify?}
    MOD_CHECK -->|no| MOD_DENIED([Inform user: not modifiable])

    MOD_ROUTE -->|address| MOD_ADDRESS[Collect new address — confirm — update]
    MOD_ROUTE -->|payment| MOD_PAYMENT[Collect new payment method — confirm — update]
    MOD_ROUTE -->|items| MOD_ITEMS[Collect ALL item changes + payment method — confirm — update]

    MOD_ADDRESS --> END
    MOD_PAYMENT --> END
    MOD_ITEMS --> END

    RETURN_CHECK -->|yes| RETURN[Collect items + refund method — confirm — process]
    RETURN_CHECK -->|no, but pending| CANCEL_CHECK
    RETURN_CHECK -->|no, other status| RETURN_DENIED([Inform user: not returnable])
    RETURN --> END

    EXCHANGE_CHECK -->|yes| EXCHANGE[Collect ALL exchanges + payment method — confirm — process]
    EXCHANGE_CHECK -->|no, but pending| MOD_CHECK
    EXCHANGE_CHECK -->|no, other status| EXCHANGE_DENIED([Inform user: not exchangeable])
    EXCHANGE --> END

    TRANSFER --> TRANSFER_END([Human agent handoff complete])
"""

RETAIL_GLOBAL_POLICIES = [
    "single_user_per_conversation: Authenticate exactly one user at the start. Deny requests involving a different user.",
    "one_tool_per_turn: Never combine a tool call with a user-facing response in the same turn.",
    "confirmation_before_mutations: Before any DB-updating action, list full details and wait for explicit 'yes'. Append the exact phrase: 'Please confirm so I can process this for you. Please note that the action is not yet complete, and I will notify you once it is successfully processed.'",
    "batch_processing: For multiple actions, investigate ALL first, present in one message, get single combined confirmation.",
    "no_fabrication: Only use data provided by user or returned by tools.",
    "single_action_per_order: Each order can undergo only ONE mutation. Present financial outcome of each option if conflict.",
    "actionable_order_statuses: May only act on orders with status 'pending' or 'delivered'.",
    "timestamps_est: All times in DB are EST 24-hour.",
    "refund_timing: Gift card refunds immediate; others 5-7 business days.",
    "product_vs_item_ids: Product ID = type; Item ID = variant. Distinct.",
    "transfer_policy: If out of scope, inform user and ASK before transferring.",
    "tie_breaking: When multiple options tie, select cheapest. Do not ask user to choose.",
    "order_discovery: If no order ID given, retrieve user details and check ALL orders.",
    "calculations: MUST use the calculate tool for ALL math.",
    "policy_vs_scope: Policy-limited requests stay in flow; do not transfer.",
    "lost_items: Lost items are ineligible for returns/exchanges/refunds. Inform and deny; do not transfer.",
    "post_action_info: Provide post-action details (new total, refund amount) only AFTER tool execution.",
]

RETAIL_NODE_POLICIES = {
    "AUTH": {
        "tool_hints": ["find_user_id_by_email", "find_user_id_by_name_zip"],
        "policy": "Authenticate via email OR (full name + zip). MUST use one of the listed tools. Order-derived user IDs do NOT count. If fails, ask retry or end.",
    },
    "ROUTE": {
        "tool_hints": [],
        "policy": "Identify intent: info/cancel/modify/return/exchange/out-of-scope. Lost-item refund/return requests map to 'info', not transfer.",
    },
    "INFO": {
        "tool_hints": ["get_user_details", "get_order_details", "get_product_details"],
        "policy": "Look up profile/orders/products. No mutations. Lost-item refund requests denied here.",
    },
    "CANCEL_CHECK": {
        "tool_hints": ["get_order_details"],
        "policy": "Verify status = pending. If not, route back to ROUTE.",
    },
    "CANCEL": {
        "tool_hints": ["cancel_pending_order"],
        "policy": "Collect order ID + reason ('no longer needed'|'ordered by mistake'). Map user's informal reason to closest allowed. Confirm before tool call. After: status -> cancelled, refund per refund_timing.",
    },
    "MOD_CHECK": {
        "tool_hints": ["get_order_details"],
        "policy": "If no order ID, check ALL user orders. Verify status = pending. 'pending (items modified)' is ineligible.",
    },
    "MOD_ROUTE": {
        "tool_hints": [],
        "policy": "Determine: address / payment / items.",
    },
    "MOD_ADDRESS": {
        "tool_hints": ["modify_pending_order_address"],
        "policy": "Collect new address, confirm, update. Status stays pending.",
    },
    "MOD_PAYMENT": {
        "tool_hints": ["modify_pending_order_payment"],
        "policy": "Collect new payment. Must differ from original. Single method only. Gift card must cover total. Confirm, update. Original refunded.",
    },
    "MOD_ITEMS": {
        "tool_hints": ["modify_pending_order_items"],
        "policy": "Collect ALL item swaps in one pass. Same product type only; same item ID prohibited. For 'switch to cheapest', evaluate each item individually; exclude items already at cheapest. Calculate price diff via calculate tool. Payment method required for diff. Gift card balance must cover diff. MUST say verbatim: 'Please confirm you have listed all items you want to modify, as this action can only be performed once per order.' After: status -> 'pending (items modified)', no further changes.",
    },
    "RETURN_CHECK": {
        "tool_hints": ["get_order_details"],
        "policy": "Verify status = delivered. If pending, offer to cancel and jump to CANCEL_CHECK. Otherwise route back to ROUTE.",
    },
    "RETURN": {
        "tool_hints": ["return_delivered_order_items"],
        "policy": "Collect order ID + items + refund method (original payment OR existing gift card). Ask open-ended about refund method to keep user engaged. Confirm before tool call. After: status -> 'return requested'.",
    },
    "EXCHANGE_CHECK": {
        "tool_hints": ["get_order_details"],
        "policy": "Verify status = delivered. If pending, offer to modify items and jump to MOD_CHECK. Otherwise route back to ROUTE.",
    },
    "EXCHANGE": {
        "tool_hints": ["exchange_delivered_order_items"],
        "policy": "Collect ALL exchanges in one pass. Same product type only; same item ID prohibited. Calculate price diff via calculate tool. Payment for diff required. Gift card balance must cover diff. Remind user this can only be performed once per order. Confirm before tool call. After: status -> 'exchange requested'.",
    },
    "TRANSFER": {
        "tool_hints": ["transfer_to_human_agents"],
        "policy": "Call transfer_to_human_agents, then send exactly: 'YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.'",
    },
    "END": {
        "tool_hints": [],
        "policy": "Ask if anything else; if not, end conversation.",
    },
}


def render_retail_system_prompt() -> str:
    """Render the retail SOP into a system-prompt string."""
    lines = ["# SOP Mermaid Graph", "", "## Mermaid Conventions",
             "Format: flowchart TD. Stadium ([text]) for terminals, Rectangle [text] for actions, Rhombus {text} for decisions.",
             "Edge conditions are written as |condition|.", "",
             "## SOP Global Policies", ""]
    for p in RETAIL_GLOBAL_POLICIES:
        lines.append(f"- {p}")
    lines.extend(["", "## SOP Node Policies", "```yaml"])
    for node_id, policy in RETAIL_NODE_POLICIES.items():
        lines.append(f"{node_id}:")
        if policy["tool_hints"]:
            lines.append(f"  tool_hints: {', '.join(policy['tool_hints'])}")
        else:
            lines.append(f"  tool_hints: null")
        lines.append(f"  policy: |")
        lines.append(f"    {policy['policy']}")
    lines.extend(["```", "", "## SOP Flowchart", "", "```mermaid", RETAIL_MERMAID, "```"])
    return "\n".join(lines)
