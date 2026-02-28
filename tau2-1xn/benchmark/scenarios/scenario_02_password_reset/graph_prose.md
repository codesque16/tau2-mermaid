# Password Reset Workflow (Linear)

When a user requests a password reset, follow this sequence. Use the exact node IDs below for transition_to_node, including the final **end** node.

1. **start** — Begin when the user requests a password reset.
2. **verify_identity** — Verify user identity (e.g. security questions, registered email).
3. **send_reset_link** — Send a time-limited reset link to the user's registered email.
4. **wait_for_click** — (User clicks link; proceed when they return.)
5. **validate_token** — Validate the reset token when the user returns via the link.
6. **set_new_password** — User sets and confirms a new password.
7. **end** — Conclude the workflow (call transition_to_node with "end" when done).

Do not skip any step. Always finish by transitioning to **end**.
