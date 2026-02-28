# IT Ticket Routing Workflow (Branching)

When a user submits an IT support ticket, use the exact node IDs below. Every path ends at **end**; you must call transition_to_node with "end" when done.

1. **start** — Begin when the user submits a ticket.
2. **capture_issue** — Capture issue description (summary, category, attachments).
3. **classify_priority** — Priority?
   - **Low** → **assign_l1** → **send_auto_response** → **notify_user** → **end**
   - **Medium** → **assign_l2** → **send_auto_response** → **notify_user** → **end**
   - **High** → **assign_l2_urgent** → **send_urgent_response** → **notify_user** → **end**

Do not skip steps. Always finish by transitioning to **end**.
