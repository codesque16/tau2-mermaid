# Insurance Claim with Investigation and Escalation (Complex)

When a claim is received, use the exact node IDs below. Paths end at **end_reject** or **end_success**; you must call transition_to_node with that end node when done.

1. **start** — Begin when the claim is received.
2. **verify_policyholder** — Verify policyholder identity.
3. **policy_active** — Policy active?
   - **No** → **reject_claim** → **end_reject**
   - **Yes** → **assess_damage**
4. **assess_damage** — Assess damage type and amount.
5. **high_value** — Damage > $5000?
   - **No** → **auto_approve** → **calculate_payout** → then step 7.
   - **Yes** → **assign_senior** → **investigation_needed**
6. **investigation_needed** — Investigation needed?
   - **No** → **calculate_payout** → then step 7.
   - **Yes** → **request_docs** → **review_docs** → **docs_sufficient**: if No, loop back to **request_docs**; if Yes → **calculate_payout**
7. **calculate_payout** — Calculate payout.
8. **approval_required** — Approval required?
   - **No** → **issue_payment** → **end_success**
   - **Yes** → **escalate_approval** → **manager_decision**: Approve → **issue_payment** → **end_success**; Reject → **reject_claim** → **end_reject**

Do not skip steps. Always finish by transitioning to **end_reject** or **end_success** as appropriate.
