# Return delivered order (policy)

An order can only be returned if its status is **delivered**. Check status before taking the action.

**Obtain**: order id and the **list of items** to be returned. User must provide a **payment method** to receive the refund. Refund must go to the **original payment method** or an **existing gift card**.

**Before calling the return API**: List the action (order id, items to return, refund method) and obtain **explicit user confirmation (yes)**. One tool call at a time.

After user confirmation: order status becomes 'return requested'; user receives an email with instructions on how to return items.

Then confirm and mention the email; go to confirm.
