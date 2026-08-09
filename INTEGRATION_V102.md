# Schema 102 integration

1. Create and seal `SoakCampaignPlanV102`.
2. Acquire `FileLeaseStoreV102` and retain the returned fencing token.
3. Start the campaign once.
4. Claim only the due run slot.
5. Execute Schema 101 qualification outside the orchestrator.
6. Convert the result into sealed `QualificationRunEvidenceV102`.
7. Record evidence before `evidence_max_age` expires.
8. Persist evidence for at least `evidence_retention`.
9. Stop scheduling immediately when campaign state is `BLOCKED` or `QUARANTINED`.
10. Treat `eligible_for_extended_paper_soak` only as permission for a longer paper campaign.
