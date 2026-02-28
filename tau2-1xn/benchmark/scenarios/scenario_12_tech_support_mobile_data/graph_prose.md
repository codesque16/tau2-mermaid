# Tech Support Path 2: Mobile Data Issues (Complex)

Use the exact node IDs below. Workflow ends at **End_Resolve** or **End_Escalate_Tech**; you must call transition_to_node with that end when done.

1. **Start** — User reports a data issue.
2. **P2_Start** — Path 2: Mobile Data Issues.
3. **P2_S0_RunSpeedTest** — Run speed test.
4. **P2_S0_Decision_Result** — Speed test result? **No connection** → Path 2.1 (P2_1_Start). **Excellent** → **End_Resolve**. **Slow** → Path 2.2 (P2_2_Start).
5. **P2_1_Start** → **P2_1_S0_Check** → **P2_S0_Decision_NoConnection** — No connection? **Yes** → **P2_1_S1_VerifyService**. **No** → **P2_2_Start**.
6. **P2_1_S1_VerifyService** → **P2_1_RetestAfterVerify** → **P2_1_Decision_Connectivity** — Data restored? **Yes** → **End_Resolve**. **No** → **P2_1_S2_Decision_Traveling**.
7. **P2_1_S2_Decision_Traveling** — User traveling? **Yes** → **P2_1_S2_CheckRoaming** (then data roaming OFF? → turn ON → retest → connectivity? → End_Resolve or **P2_1_S2_VerifyLineRoaming**; line not enabled? → enable roaming → retest; else **P2_1_S3_CheckMobileData**). **No** → **P2_1_S3_CheckMobileData**.
8. **P2_1_S3_CheckMobileData** → **P2_1_S3_Decision_MobileDataOFF** — Mobile data OFF? **Yes** → turn ON → retest → **End_Resolve** or **P2_1_S4_CheckDataUsage**. **No** → **P2_1_S4_CheckDataUsage**.
9. **P2_1_S4_CheckDataUsage** → **P2_1_S4_Decision_DataExceeded** — Data exceeded? **No** → **End_Escalate_Tech**. **Yes** → **P2_1_S4_AskPlanOrRefuel** → **P2_1_S4_Decision_ChangePlan** — Change plan? **Yes** → gather/select/apply plan → retest → connectivity/excellent → **End_Resolve** or **P2_2_Start**. **No** → refuel flow → confirm refuel? **Yes** → apply refuel → retest → **End_Resolve** or **P2_2_Start**; **No** → **End_Escalate_Tech**. Connectivity after data? **No** → **End_Escalate_Tech**.
10. **P2_2_Start** → **P2_2_S0_CheckSlow** → **P2_2_S1_CheckDataRestriction** → **P2_2_S1_Decision_DataSaverON** — Data saver ON? **Yes** → turn OFF → retest → **End_Resolve** or **P2_2_S2_CheckNetworkMode**. **No** → **P2_2_S2_CheckNetworkMode**.
11. **P2_2_S2_CheckNetworkMode** → **P2_2_S2_Decision_OldMode** — 2G/3G? **Yes** → change to 5G → retest → **End_Resolve** or **P2_2_S3_CheckVPN**. **No** → **P2_2_S3_CheckVPN**.
12. **P2_2_S3_CheckVPN** → **P2_2_S3_Decision_VPNActive** — VPN active? **Yes** → turn OFF → retest → **End_Resolve** or **End_Escalate_Tech**. **No** → **End_Escalate_Tech**.

Always finish by transitioning to **End_Resolve** or **End_Escalate_Tech**.
