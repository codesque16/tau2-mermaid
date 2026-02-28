# Customer Complaint with Escalation and Callback Loop (Complex)

When a customer complaint is received, use the exact node IDs below. The workflow ends at **end_resolved**; you must call transition_to_node with "end_resolved" when done.

1. **start** — Begin when the complaint is received.
2. **acknowledge** — Acknowledge the complaint.
3. **gather_details** — Gather complaint details.
4. **can_resolve** — L1 can resolve?
   - **Yes** → **attempt_resolution** → **resolved**: if Yes → **confirm_satisfaction** → **close_ticket** → **end_resolved**; if No → **escalate_l2**
   - **No** → **escalate_l2**
5. **escalate_l2** → **l2_investigate** — L2 investigates.
6. **callback_needed** — Callback needed?
   - **Yes** → **schedule_callback** → **attempt_callback** → **callback_success**: if No → **reschedule** → loop back to **attempt_callback**; if Yes → **l2_resolve**
   - **No** → **l2_resolve**
7. **l2_resolve** → **still_escalate** — Further escalation?
   - **No** → **confirm_satisfaction** → **close_ticket** → **end_resolved**
   - **Yes** → **escalate_manager** → **manager_resolution** → **confirm_satisfaction** → **close_ticket** → **end_resolved**

Do not skip steps. Always finish by transitioning to **end_resolved**.
