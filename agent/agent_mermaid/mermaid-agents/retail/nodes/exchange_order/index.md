# Exchange delivered order (policy)

An order can only be exchanged if its status is **delivered**. Check status before taking the action. Remind the customer to confirm they have provided **all** items to be exchanged before proceeding.

For each item: exchange to an **available** new item of the **same product** but different options (e.g. same shirt, different color/size). No change of product type (e.g. shirt to shoe).

**Exchange or modify order tools can only be called once per order.** Collect all items to be exchanged into one list before making the tool call.

User must provide a **payment method** to pay or receive refund for the price difference. If gift card, it must have enough balance.

**Before calling the exchange API**: List the action details and obtain **explicit user confirmation (yes)**. One tool call at a time.

After user confirmation: order status becomes 'exchange requested'; user receives an email regarding how to return items. No new order is placed.

Then confirm and mention the email; go to confirm.
