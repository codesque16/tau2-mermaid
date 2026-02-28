# HR Onboarding Document Submission (Branching with Loop)

When a new hire begins onboarding, use the exact node IDs below. The workflow ends at **end**; you must call transition_to_node with "end" when done.

1. **start** — Begin when the new hire starts onboarding.
2. **send_checklist** — Send the document checklist.
3. **collect_docs** — Collect submitted documents.
4. **docs_complete** — All required docs received?
   - **No** → **send_reminder** → **wait_resubmit** → loop back to **collect_docs**
   - **Yes** → **verify_docs**
5. **verify_docs** — Verify document validity.
6. **setup_accounts** — Setup system accounts.
7. **schedule_orientation** — Schedule orientation.
8. **send_welcome** — Send welcome packet.
9. **end** — Conclude the workflow (call transition_to_node with "end" when done).

Do not skip steps. When documents are complete, proceed through verify_docs → setup_accounts → schedule_orientation → send_welcome → **end**.
