# Tech Support Path 3: MMS (Picture / Group Messaging) (Complex)

Use the exact node IDs below. Workflow ends at **End_Resolve** or **End_Escalate_Tech**; you must call transition_to_node with that end when done.

1. **Start** — User reports an MMS issue.
2. **P3_Start** — Path 3: MMS (Picture/Group Messaging).
3. **P3_S0_CheckMMS** — Step 3.0: Check if user is facing an MMS issue.
4. **P3_S0_Decision_MMSWorks** — Can send MMS? **Yes** → **End_Resolve**. **No** → **P3_S1_VerifyNetworkService**.
5. **P3_S1_VerifyNetworkService** → **P3_S1_AssumeServiceOK** → **P3_S1_Action_RetestMMS_P1** → **P3_S2_VerifyMobileData**.
6. **P3_S2_VerifyMobileData** → **P3_S2_AssumeDataOK** → **P3_S2_Action_RetestMMS_P2** → **P3_S3_CheckNetworkTech**.
7. **P3_S3_CheckNetworkTech** → **P3_S3_Decision_Is2G** — Connected to 2G only? **Yes** → **P3_S3_Action_ChangeNetworkMode** → **P3_S3_Action_VerifyMMSWorks2G** → **P3_S3_Decision_MMSWorksAfter2G**. **No** → **P3_S4_CheckWifiCalling**.
8. **P3_S3_Decision_MMSWorksAfter2G** — MMS works? **Yes** → **End_Resolve**. **No** → **P3_S4_CheckWifiCalling**.
9. **P3_S4_CheckWifiCalling** → **P3_S4_Decision_WifiCallingON** — Wi-Fi Calling ON? **Yes** → **P3_S4_Action_TurnWifiCallingOFF** → **P3_S4_Action_VerifyMMSWorksWifiOFF** → **P3_S4_Decision_MMSWorksAfterWifiOFF**. **No** → **P3_S5_VerifyAppPermissions**.
10. **P3_S4_Decision_MMSWorksAfterWifiOFF** — MMS works? **Yes** → **End_Resolve**. **No** → **P3_S5_VerifyAppPermissions**.
11. **P3_S5_VerifyAppPermissions** → **P3_S5_Decision_PermissionsMissing** — Storage or SMS permission missing? **Yes** → **P3_S5_Action_GrantPermissions** → **P3_S5_Action_VerifyMMSWorksPerms** → **P3_S5_Decision_MMSWorksAfterPerms**. **No** → **P3_S6_CheckAPNSettings**.
12. **P3_S5_Decision_MMSWorksAfterPerms** — MMS works? **Yes** → **End_Resolve**. **No** → **P3_S6_CheckAPNSettings**.
13. **P3_S6_CheckAPNSettings** → **P3_S6_Decision_MMSC_Missing** — MMSC URL missing? **Yes** → **P3_S6_Action_ResetAPN** → **P3_S6_Action_VerifyMMSWorksAPN** → **P3_S6_Decision_MMSWorksAfterAPN**. **No** → **End_Escalate_Tech**.
14. **P3_S6_Decision_MMSWorksAfterAPN** — MMS works? **Yes** → **End_Resolve**. **No** → **End_Escalate_Tech**.

Always finish by transitioning to **End_Resolve** or **End_Escalate_Tech**.
