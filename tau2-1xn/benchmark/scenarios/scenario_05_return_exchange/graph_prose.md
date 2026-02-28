# Return and Exchange Workflow (Branching)

When a customer requests a return or exchange, use the exact node IDs below. Every path ends at an **end** node (end1, end2, end3, end4, or end5); you must call transition_to_node with that end node when done.

1. **start** — Begin when the customer requests a return or exchange.
2. **verify_purchase** — Verify purchase and customer identity.
3. **check_window** — Within return window? (e.g. 30 days)
   - **No** → **deny_return** → **offer_alternatives** → **end1**
   - **Yes** → **check_condition**
4. **check_condition** — Check item condition.
5. **choice** — Refund or exchange?
   - **Refund** → **process_refund** → **send_refund_confirmation** → **end2**
   - **Exchange** → **check_stock**: if available → **process_exchange** → **send_exchange_confirmation** → **end3**; if unavailable → **offer_refund_or_credit** → **end5**
   - **Store credit** → **issue_credit** → **send_credit_confirmation** → **end4**

Do not skip steps. Always finish by transitioning to the correct end node (end1, end2, end3, end4, or end5) for your path.
