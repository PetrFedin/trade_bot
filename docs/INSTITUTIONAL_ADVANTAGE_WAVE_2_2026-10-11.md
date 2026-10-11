# trade_bot — Auditable Strategy Research OS

Status: **PROPOSED, documentation only**. Date: 2026-10-11. This roadmap complements existing canonical plans and does not represent shipped functionality.

## Strategic enhancements
1. P0: leak-proof event-time data, symbol survivorship handling, deterministic costs, funding bounds and exchange precision checks.
2. P0: portfolio walk-forward and untouched holdout with immutable experiment registries, pre-committed decision rules and reproducible reports.
3. P0: risk lab for liquidation, tail exposure, capacity, turnover and regime decomposition; no claimed alpha before robust independent proof.
4. P1: execution simulator with order book slippage, fill uncertainty, fee tiers and capital-size impact.
5. P1: operator control tower: kill switch, sandbox-only first, model drift, outages and loss limits.
6. P2: institutional diligence pack with negative results, research lineage, performance limitations and external verification.

## Common implementation contract
- Inspect and preserve existing domain authorities, schemas, tasks and existing roadmap before implementation. Do not build a parallel source of truth.
- Every proposal becomes a bounded PR with owner, dependency graph, acceptance criteria, CI and rollback procedure.
- For recommendations, record source, freshness, confidence, effect estimate, human approval and outcome; distinguish measured impact from hypothetical scenarios.
- Introduce issuer/verifier credentials only where independently useful; support issuance, expiry, suspension/revocation, key rotation and privacy-preserving verification.
- Apply least privilege, per-tenant access, audit retention and independent negative-path tests.
- Demand functional device/browser proof, golden-path replay, exact-SHA CI and recoverability before production admission.
- Pilot with baseline and outcome metrics; no fabricated ROI, partner agreements, customer evidence or production readiness.

## Reference standards (evaluate applicability, do not assume compliance)
- W3C Verifiable Credentials 2.0: https://www.w3.org/TR/vc-data-model-2.0/
- OpenID4VC specifications: https://openid.net/wg/digital-credentials-protocols/specifications/
- NIST AI Risk Management Framework: https://www.nist.gov/itl/ai-risk-management-framework
- OWASP GenAI Security Project: https://genai.owasp.org/

## Execution gate
Discovery -> architecture boundary review -> single vertical slice -> adversarial tests -> human acceptance -> measured pilot -> commercial rollout. Missing external credentials, rights, legal applicability, real-world evidence, or CI is an explicit BLOCKED state.
