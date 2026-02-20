# Cancel pending order (policy)

An order can only be cancelled if its status is **pending**. Check status before taking the action.

**Obtain**: order id and **reason** for cancellation. The reason must be either **'no longer needed'** or **'ordered by mistake'**. Other reasons are not acceptable.

**Before calling the cancel API**: List the action (order id, reason) and obtain **explicit user confirmation (yes)**. One tool call at a time.

After user confirmation: order status becomes 'cancelled'; total is refunded via the original payment method—**immediately** if gift card, otherwise in **5 to 7 business days**.

Then confirm and give refund timeline; go to confirm.
