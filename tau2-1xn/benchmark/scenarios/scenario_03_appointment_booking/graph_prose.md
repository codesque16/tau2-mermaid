# Appointment Booking Workflow (Linear)

When a customer requests an appointment, follow this sequence. Use the exact node IDs below for transition_to_node, including the final **end** node.

1. **start** — Begin when the customer requests an appointment.
2. **collect_preferences** — Collect date, time, and service preferences.
3. **check_availability** — Check the calendar for open slots.
4. **propose_slots** — Propose available slots to the customer.
5. **confirm_booking** — Confirm booking details with the customer.
6. **send_reminder** — Send booking confirmation and reminder.
7. **end** — Conclude the workflow (call transition_to_node with "end" when done).

Do not skip any step. Always finish by transitioning to **end**.
