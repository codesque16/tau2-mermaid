# New booking (policy)

**Identity**: Obtain the **user id** first. Then trip type, origin, destination.

**Cabin**: Cabin class must be the same across all flights in the reservation. Classes: **basic economy**, **economy**, **business** (basic economy is distinct from economy).

**Passengers**: At most five passengers per reservation. Collect **first name, last name, date of birth** for each. All passengers fly the same flights in the same cabin.

**Payment** (from user profile only): At most one travel certificate, one credit card, and up to three gift cards. Remaining travel certificate balance is not refundable.

**Checked bags** (do not add bags the user does not need):
- Regular member: basic economy 0, economy 1, business 2 free per passenger. Silver: 1 / 2 / 3. Gold: 2 / 3 / 4.
- Each extra bag: $50.

**Travel insurance**: Ask if the user wants it. $30 per passenger; enables full refund if cancel for health or weather reasons.

**Before calling any booking API**: List the action details and obtain **explicit user confirmation (yes)**. Make only one tool call at a time; do not respond and call a tool in the same turn.

Then complete the booking, provide confirmation and reservation id; go to confirm.
