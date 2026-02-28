# Insurance Claim Type Routing (Branching)

When a customer files an insurance claim, use the exact node IDs below. Every path ends at an **end** node (end1, end2, or end3); you must call transition_to_node with that end node when done.

1. **start** — Begin when the customer files a claim.
2. **verify_policy** — Verify policy and identity.
3. **claim_type** — Type of claim?
   - **Auto** → **collect_auto** → **assign_auto** → **send_auto_confirmation** → **end1**
   - **Home** → **collect_home** → **assign_home** → **send_home_confirmation** → **end2**
   - **Health** → **collect_health** → **assign_health** → **send_health_confirmation** → **end3**

Do not skip steps. Always finish by transitioning to the correct end node (end1, end2, or end3) for the claim type.
