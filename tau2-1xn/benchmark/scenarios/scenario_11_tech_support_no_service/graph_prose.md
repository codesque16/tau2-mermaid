# Tech Support Path 1: No Service / No Connection (Complex)

When a user reports no service or no connection, use the exact node IDs below. Paths end at **End_Resolve** or **End_Escalate_Tech**; you must call transition_to_node with that end when done.

1. **Start** — Begin when the user reports an issue.
2. **P1_Start** — Path 1: No Service/Connection.
3. **P1_S0_CheckStatusBar** — Check if the user is facing a no-service issue.
4. **P1_S0_Decision_NoService** — Status bar shows no service / airplane mode?
   - **No** → **End_Resolve**
   - **Yes** → **P1_S1_CheckAirplane**
5. **P1_S1_CheckAirplane** — Check airplane mode and network status.
6. **P1_S1_Decision_AirplaneON** — Airplane mode ON?
   - **Yes** → **P1_S1_Action_TurnAirplaneOFF** → **P1_S1_Action_VerifyRestored1** → **P1_S1_Decision_Restored1**
   - **No** → **P1_S2_VerifySIM**
7. **P1_S1_Decision_Restored1** — Service restored?
   - **Yes** → **End_Resolve**
   - **No** → **P1_S2_VerifySIM**
8. **P1_S2_VerifySIM** — Verify SIM card status.
9. **P1_S2_Decision_SIMMissing** — SIM missing?
   - **Yes** → **P1_S2_Action_ReseatSIM** → **P1_S2_Action_VerifySIMImprove** → **P1_S2_Decision_SIMImproved**
   - **No** → **P1_S2_Decision_SIMLocked**
10. **P1_S2_Decision_SIMImproved** — Service restored? Yes → **P1_S3_ResetAPN**. No → **End_Escalate_Tech**
11. **P1_S2_Decision_SIMLocked** — SIM locked (PIN/PUK)? Yes → **End_Escalate_Tech**. No → **P1_S3_ResetAPN**
12. **P1_S3_ResetAPN** → **P1_S3_User_Action_ResetAPN** → **P1_S3_RestartDevice** → **P1_S3_VerifyService** → **P1_S3_Decision_Resolved**
13. **P1_S3_Decision_Resolved** — Service restored? Yes → **End_Resolve**. No → **P1_S4_CheckSuspension**
14. **P1_S4_CheckSuspension** — Check line suspension.
15. **P1_S4_Decision_Suspended** — Line suspended? No → **End_Escalate_Tech**. Yes → **P1_S4_Decision_SuspensionType**
16. **P1_S4_Decision_SuspensionType** — Due to bill → **P1_S4_Decision_OverdueBill**. Contract end → **End_Escalate_Tech**
17. **P1_S4_Decision_OverdueBill** — Overdue bill? No → **P1_S4_Action_ResumeLine**. Yes → **P1_S4_Action_PaymentRequest** → … → **P1_S4_Action_ResumeLine**
18. **P1_S4_Action_ResumeLine** → **P1_S4_Action_Reboot** → **P1_S4_Action_VerifyService** → **P1_S4_Decision_ServiceRestored**
19. **P1_S4_Decision_ServiceRestored** — Service restored? Yes → **End_Resolve**. No → **End_Escalate_Tech**

Do not skip steps. Always finish by transitioning to **End_Resolve** or **End_Escalate_Tech**.
