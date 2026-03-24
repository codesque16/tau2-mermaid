# Final Candidate — v01

## Summary

This is the optimized policy for gpt-5-nano with low reasoning on the retail solo tasks.

**Candidate file:** `results/prompt_optimizer_runs/20260324_gpt5nano_low/candidates/v01.md`

**Baseline pass rate:** 3/22 (~14%)
**Estimated new pass rate:** 17-20/22 (~77-91%) based on probe results

## Changes Made vs. Baseline

### Change 1: Anti-transfer guard (addresses ~13 failing tasks)

**Problem:** After auth, model immediately calls `transfer_to_human_agents` for multi-step tasks, cross-order tasks, or conditional tasks. These are all within scope.

**Fix added:**
```
**IMPORTANT — Do NOT transfer to a human agent prematurely.** You should transfer the user to a human agent **only if the request type is completely outside the scope of your capabilities** (i.e., not one of: cancel/modify/return/exchange an order, update user or order address, look up order or product information). All of the following are within your capabilities and must be handled directly — do not transfer:

- Returning or exchanging items from delivered orders
- Cancelling or modifying pending orders
- Looking up order details or product information
- Changing user default address or order shipping address
- Handling multiple requests in one ticket
- Executing conditional requests ("if X is not possible, do Y")
- Finding an order when no order ID is given (look it up via get_user_details)
- Using an address from a reference order (look it up via get_order_details)
```

**Probe result:** Model stopped transferring for task_27 (return+exchange), task_73 (return everything except X), task_81 (partial cancel fallback), task_86 (cross-reference address), task_91, task_56, task_2.

### Change 2: Order lookup workflow (addresses ~8 tasks)

**Problem:** When ticket does not include an order ID, model transfers instead of looking up the user's orders.

**Fix added:**
```
## Workflow when no order ID is provided

If the ticket does not specify an order ID, you must **not** transfer. Instead:
1. After authenticating the user, call `get_user_details` to retrieve their profile and list of orders.
2. Call `get_order_details` for each relevant order to identify the right one.
3. Proceed with the requested action.
```

**Probe result:** Model correctly calls get_user_details after auth for task_27, task_43, task_73, etc.

### Change 3: Order ID format (addresses ~3 tasks)

**Problem:** Model calls tools with `W8665881` or `9502127` instead of `#W8665881`, gets "Order not found", then transfers.

**Fix added:**
```
## Order ID format

**Order IDs always start with '#W' followed by 7 digits. Example: '#W8665881'.**

If a ticket provides an order number without the '#' prefix — like 'W8665881' or '8665881' — you must prepend '#' to form '#W8665881' before passing it to any tool. Never call an order tool with an ID missing the '#' prefix.
```

**Probe result:** Model uses correct format for task_17.

### Change 4: Cross-referencing addresses from another order (addresses ~3 tasks)

**Problem:** When customer says "use the address from order #X" or "use my DC address from one of my orders", model transfers instead of fetching the reference order first.

**Fix added:**
```
## Cross-referencing addresses from another order

If the customer says "change address to match order #X" or "use the address from another order (customer does not reveal it)":
1. Call `get_order_details` on the reference order (#X) to retrieve its address fields.
2. If the customer did not name the reference order, call `get_user_details` first to list all orders, then call `get_order_details` on each until you find the one matching the described location (e.g., "Washington DC address").
3. Use the retrieved address to update the target order or user profile.
```

**Probe result:** Model correctly describes scanning orders for DC address for task_86, task_102.

### Change 5: Partial cancellation and conditional fallback (addresses ~4 tasks)

**Problem:** Model says "partial cancel not possible" then transfers. Should execute the fallback stated in the ticket.

**Fix added:**
```
## Partial cancellation and conditional fallback

**Partial cancellation (canceling only some items within an order) is NOT possible.** Cancellation always applies to the entire order.

When a ticket includes conditional instructions, execute them yourself — do **not** transfer to a human agent:

- "Cancel items X; if partial cancel not possible, cancel the whole order" → Since partial cancel is never possible, **cancel the whole order** with the appropriate reason.
- "Cancel items X; if partial cancel not possible, do nothing" → Since partial cancel is not possible, take no cancellation action.
- "If X not possible, do Y" → Attempt X; if X fails or is not applicable, execute Y directly.
```

**Probe result:** Model correctly cancels whole order for task_81, executes conditional logic for task_56.

### Change 6: Cancel reason selection (addresses ~1-2 tasks)

**Problem:** task_66 used 'ordered by mistake' when 'no longer needed' was expected (customer wanted different item as first choice).

**Fix added:**
```
## Cancel reason selection

The cancel reason must be one of: **'no longer needed'** or **'ordered by mistake'**.

Choose based on context:
- **'no longer needed'**: the customer no longer wants the items (changed mind, wants a different item, lost the item, found it elsewhere, wants to cancel as a fallback when modification isn't possible, etc.)
- **'ordered by mistake'**: the customer accidentally placed the order or accidentally selected wrong items

When in doubt, use **'no longer needed'**.
```

## How to apply

Copy the content of `candidates/v01.md` to replace `gepa/examples/tau2_retail_mermaid/seed_solo_v1.md` (after review), then run:

```bash
uv run python -m domains.retail.run_solo_tasks --config configs/openai_simulation_v1.yaml --fresh
```

Or to test with the candidate file directly, update the policy path in the config to point to `results/prompt_optimizer_runs/20260324_gpt5nano_low/candidates/v01.md`.

## Known remaining risks

1. **Wrong variant selection** (Pattern 5, ~3 tasks): Even when the model proceeds to do an exchange/modify, it may pick the wrong color/size variant. This is a model capability issue (gpt-5-nano low reasoning may not reliably match variant attributes). A potential fix would be to add more explicit instructions for variant matching (e.g., "call get_product_details to list all variants; match ALL specified attributes exactly"), but this was not probed.

2. **Return + exchange on same order**: task_27 expects exchange-only (customer's stated preference), and the new policy correctly handles this. But other tasks with similar patterns may behave differently.

3. **Task_56 item selection**: The correct item is the "cheapest air purifier" which requires calling get_product_details and sorting by price. The model may select the wrong variant without explicit instruction to find the minimum-priced variant.
