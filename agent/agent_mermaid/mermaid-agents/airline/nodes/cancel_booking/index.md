# Cancel booking (policy)

**Identity**: Obtain **user id** and **reservation id**. If the user does not know the reservation id, use tools to help locate it.

**Reason**: Obtain the reason for cancellation: change of plan, airline cancelled flight, or other.

**If any portion of the flight has already been flown**: You cannot cancel; transfer to a human agent.

**Otherwise**, cancellation is allowed if **any** of the following is true:
- Booking was made within the last 24 hours
- Flight was cancelled by the airline
- It is a business flight
- User has travel insurance and the reason is covered by insurance (health or weather)

The API does **not** check these rules—you must verify they apply before calling the API.

**Refund**: Refund goes to original payment methods within 5–7 business days.

**Before calling the cancel API**: List the action and obtain **explicit user confirmation (yes)**. One tool call at a time.

Then process the cancellation, confirm and give refund timeline; go to confirm.
