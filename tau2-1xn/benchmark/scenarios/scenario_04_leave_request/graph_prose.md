# Leave Request Submission Workflow (Linear)

When an employee submits a leave request, follow this sequence. Use the exact node IDs below for transition_to_node, including the final **end** node.

1. **start** — Begin when the employee submits a leave request.
2. **validate_dates** — Validate leave dates (not in the past, end after start).
3. **check_balance** — Check that the employee has sufficient leave balance.
4. **submit_request** — Submit the request to the manager (assume approval).
5. **record_approval** — Record approval in the HR system.
6. **notify_employee** — Notify the employee of the outcome.
7. **end** — Conclude the workflow (call transition_to_node with "end" when done).

Do not skip any step. Always finish by transitioning to **end**.
