# Retail Prompt Optimizer Run — 2026-03-24

## Goal
Improve the retail agent policy so gpt-5-nano (low reasoning) passes more DB + communication checks on solo tasks.

## Target Model
- Model: gpt-5-nano
- Reasoning: low
- Temperature: 0
- Seed: 25738320

## Trace Source
`outputs/_retail_seed_solo_v1.md_gpt-5-nano_temp_0_reasoning_low__03-24_06-19/experiment_1/`

## Policy File (do not edit directly)
`gepa/examples/tau2_retail_mermaid/seed_solo_v1.md`

## Failure Summary (22 tasks, 3 passes = ~14% pass rate)

### Passing tasks
- task_12, task_68, task_113

### Failing tasks (19)

| Pattern | Tasks | Count |
|---------|-------|-------|
| Premature transfer (no order lookup) | task_23, task_27, task_32, task_33, task_34, task_43, task_56, task_73, task_81, task_86, task_91, task_102, task_103 | 13 |
| Order ID format missing '#' | task_17, task_45, task_78 | 3 |
| Partial cancel → fallback not executed | task_33, task_34, task_56, task_81 | 4 |
| Cross-reference address (fetch another order first) | task_78, task_86, task_102 | 3 |
| Wrong cancel reason | task_66 | 1 |
| No order lookup from user details | task_2, task_27, task_42, task_43, task_73, task_91 | 6 |

## Root Cause Analysis

### Pattern 1: Premature Transfer (highest impact)
After authenticating the user, the model immediately transfers to human agents for multi-step tasks. The model sees:
- "exchange from multiple orders" → transfers (task_23)
- "return + exchange from same order" → transfers (task_27, task_91)
- "partial cancel, if not possible do X" → transfers (task_33, task_34, task_81)
- "conditional action based on order status" → transfers (task_56)
- "return everything except X" → transfers (task_73)
- "exchange + address change" → transfers (task_86, task_102)

Root cause: The policy says "transfer if request cannot be handled within scope of your actions" but these ARE within scope. The model interprets complex/multi-step requests as "out of scope."

### Pattern 2: Order ID Format
The `#` prefix is required for all order IDs (e.g., `#W8665881`). When ticket says "W8665881" or "9502127", model calls get_order_details("W8665881") — missing the `#` — gets "not found" error, then transfers.

Policy has no mention of the `#` prefix requirement.

### Pattern 3: No Order Lookup Workflow
When ticket doesn't specify an order ID, model doesn't know to call `get_user_details` to find the user's orders. It transfers instead.

Policy doesn't explicitly say: look up user details → find orders → proceed.

### Pattern 4: Partial Cancel Fallback
When ticket says "if partial cancel not possible, cancel whole order" — model correctly identifies partial cancel isn't possible, then transfers instead of executing the fallback.

### Pattern 5: Cross-Reference Address
When ticket says "change address to match order #X" — model needs to call get_order_details(#X) first to get the address, then use it. Instead it transfers.

### Pattern 6: Wrong Cancel Reason
task_66: Model used "ordered by mistake" but should have used "no longer needed" (the ticket said "can't change to coat, so cancel it" — a fallback that implies "no longer needed").

## Hypothesis List

H1: Add explicit instruction: "Never transfer to human agent until you have looked up the user's details and all relevant orders."
H2: Add order ID format rule: "Order IDs always start with '#W' followed by 7 digits. If you see an order number like 'W1234567' or '1234567', prepend '#' to make '#W1234567'."
H3: Add fallback execution instruction: "When a ticket provides conditional instructions ('if X is not possible, do Y'), you must attempt X first. If X fails, execute Y directly — do not transfer to human agents."
H4: Add cross-reference instruction: "If the customer says 'use the address from another order', call get_order_details on that reference order to retrieve its address first."
H5: Add step-by-step workflow: "When no order ID is given, call get_user_details to retrieve the list of the user's orders, then call get_order_details for relevant ones."

## Candidates
- v01.md: Apply H1+H2+H3+H4+H5 (comprehensive fix)
