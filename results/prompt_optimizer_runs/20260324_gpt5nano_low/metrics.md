# Metrics — 2026-03-24

## Baseline (seed_solo_v1.md with gpt-5-nano low reasoning)

- Total tasks: 22
- Passing: 3 (task_12, task_68, task_113)
- Failing: 19
- Pass rate: ~14%

## Failure taxonomy

| Pattern | Count | Tasks |
|---------|-------|-------|
| Premature transfer after auth (no order lookup) | 13 | task_23, task_27, task_32, task_43, task_56, task_73, task_81, task_86, task_91, task_102, task_103, task_33/34 (conditional) |
| Order ID format missing '#' | 2 | task_17, task_45 (task_78 was missing # too but also premature transfer) |
| Conditional fallback not executed | 4 | task_33, task_34, task_56, task_81 |
| Cross-reference address not fetched | 3 | task_78, task_86, task_102 |
| Wrong cancel reason | 1 | task_66 |
| Information-only response missing action | 1 | task_2 (returned t-shirt info, then transferred before doing return) |

## Probes run

### probe_01_baseline_task27
- Policy: original seed_solo_v1.md
- Test: After auth for Isabella Johansson (task_27), does model transfer?
- Result: TRANSFERS immediately
- Score: FAIL (0/1)

### probe_02_antitransfer_task27
- Policy: v01 candidate (with anti-transfer + workflow instructions)
- Test: After auth for Isabella Johansson (task_27), does model proceed?
- Result: PROCEEDS to describe returning hose/backpack and exchanging boots
- Score: PASS (1/1) - behavioral improvement confirmed

### probe_03_partialcancel_task81
- Policy: v01 candidate
- Test: James Kim "cancel items; if partial not possible, cancel whole order"
- Result: PROCEEDS to cancel whole order correctly
- Score: PASS (1/1)

### probe_04_orderid_task17
- Policy: v01 candidate
- Test: Fatima Johnson wants to change address for order #W8665881
- Result: PROCEEDS correctly with #W prefix
- Score: PASS (1/1)

### probe_05_crossref_task86
- Policy: v01 candidate with cross-reference instruction
- Test: Yusuf Hernandez wants DC address from "one of the orders"
- Result: PROCEEDS to call get_user_details and scan orders for DC address
- Score: PASS (1/1)

### probe_06_conditional_task56
- Policy: v01 candidate
- Test: Ivan Hernandez - conditional cancel of air purifier
- Result: PROCEEDS to get user details, find order, execute conditional logic
- Score: PASS (1/1)

### probe_07_return_everything_task73
- Policy: v01 candidate
- Test: Fatima Wilson "return everything just bought except coffee machine"
- Result: PROCEEDS to find order and initiate return of all items except coffee machine
- Score: PASS (1/1)

### probe_08_noah_patel_task33
- Policy: v01 candidate
- Test: Noah Patel partial cancel fallback → change address to Seattle
- Result: PROCEEDS correctly, identifies partial cancel not possible, updates address
- Score: PASS (1/1) - but model asks for confirmation (violates one-shot mode somewhat)

## Overall probe pass rate: 7/8 probes passed

## v01 policy changes vs baseline

1. **Anti-transfer guard** (addresses ~13 tasks): Added explicit IMPORTANT section listing all in-scope actions and forbidding transfer except for out-of-scope requests.

2. **Order lookup workflow** (addresses ~8 tasks): Explicit 3-step workflow when no order ID is given: authenticate → get_user_details → get_order_details → proceed.

3. **Order ID format** (addresses ~3 tasks): Explicit rule that order IDs must start with '#W'. Any ID without '#' must have it prepended.

4. **Cross-reference address** (addresses ~3 tasks): Explicit instruction to call get_order_details on reference order to retrieve address, including when customer doesn't reveal which order.

5. **Partial cancellation fallback** (addresses ~4 tasks): Explicit statement that partial cancel is never possible, with worked examples of conditional instruction execution.

6. **Cancel reason selection** (addresses ~1-2 tasks): Added context-based guidance for choosing between 'no longer needed' vs 'ordered by mistake', defaulting to 'no longer needed'.

## Estimated new pass rate (prediction)

Assuming the anti-transfer + workflow fix works as probed, addressing the root cause of ~13 failures:
- Previously failing tasks likely fixed: task_23, task_27, task_32, task_33, task_34, task_43, task_56, task_73, task_81, task_86, task_91, task_102, task_103 (13 tasks)
- Order ID fix: task_17, task_45 (2 tasks)
- Cancel reason: task_66 (1 task)
- Caveats: task_2 (information + return), task_42 (address correction), task_78 (order ID + cross-ref)

Optimistic estimate: 3 (baseline) + ~16 (fixed) = ~19/22 = ~86% pass rate
Conservative estimate (accounting for execution-time tool selection errors): ~65-75%
