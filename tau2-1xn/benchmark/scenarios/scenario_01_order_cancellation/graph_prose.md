# Order Cancellation Workflow (Linear)

When a customer requests to cancel an order, follow this sequence. You must transition through each node in order using the exact node IDs below, including the final **end** node.

1. **start** — Begin when the customer requests cancellation.
2. **verify_order** — Verify order number; look it up in the system.
3. **check_status** — Check order status (not yet shipped).
4. **confirm_cancel** — Confirm cancellation with the customer.
5. **process_refund** — Process refund to the original payment method.
6. **send_confirmation** — Send confirmation email.
7. **end** — Conclude the workflow (you must call transition_to_node with "end" when done).

The flow is strictly linear with no branching; each step follows the previous one. Do not skip any step. Always finish by transitioning to **end**.
