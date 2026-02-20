# Modify pending order (policy)

An order can only be modified if its status is **pending**. Check status before taking the action.

For a pending order you can: change **shipping address**, **payment method**, or **product item options**—nothing else.

**Modify payment**: User can choose a **single** payment method different from the original. If switching to gift card, it must have enough balance to cover the total. After confirmation: order stays 'pending'; original payment is refunded immediately if gift card, otherwise in 5–7 business days.

**Modify items**: This action can be called **only once per order** and will set status to 'pending (items modified)'; you will not be able to modify or cancel the order after that. Remind the customer to confirm they have provided **all** items they want to modify before proceeding. Each item can be changed to an **available** new item of the **same product** but different options (e.g. same shirt, different color/size). No change of product type (e.g. shirt to shoe). User must provide a payment method for price difference or refund; if gift card, it must have enough balance.

**Exchange or modify order tools can only be called once per order.** Collect all items to be changed into one list before making the tool call.

**Before any modify API call**: List the action details and obtain **explicit user confirmation (yes)**. One tool call at a time.

Then apply the change and confirm; go to confirm.
