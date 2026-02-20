# Classify

Route the request to the correct node within policy scope (book, modify, cancel, refunds/compensation):

- **general_info**: questions about policies, baggage, check-in, membership, or other non-booking topics (use only info from tools or policy).
- **new_booking**: customer wants to make a new reservation.
- **modify_booking**: change flights (same origin/destination/trip type), cabin, baggage, or passenger details on an existing booking.
- **cancel_booking**: cancel a booking or segment.
- **handle_complaint**: delays, cancelled flights, or complaints about service; may involve refunds or compensation per policy.
- **escalate**: request cannot be handled with your tools or policy (e.g. any portion of flight already flown, or user insists on human). Transfer only then; use transfer_to_human_agents then the exact transfer message.

After classifying, call the corresponding node or reply briefly and then call that node.
