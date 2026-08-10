# Release Governance

This document defines the repository-side release contract for ASTRA Trade Bot. Repository evidence and GitHub server-side state are treated separately: repository files may describe the expected state, but `release-governance` must also compare that expectation with the GitHub branch summary.

## Current enforcement boundary

Repository-side controls are implemented and executable:

- `requirements.lock` is SHA-256 hash locked and checked for freshness against `pyproject.toml`.
- operational GitHub Actions use exact approved Node 24-native commit SHAs.
- release qualification runs dependency audit, deterministic regression, wheel/sdist build, SPDX SBOM generation and release-manifest generation.
- trusted `main`/`v*` builds generate signed GitHub/Sigstore SLSA provenance and signed SBOM attestations.
- pull-request code does not receive attestation/OIDC write privileges; the privileged attestation job only runs after a trusted push.
- release/rollback ownership is defined in `release/ownership.json` and validated by the `release-governance` workflow.
- `release-governance` queries the GitHub `main` branch summary and fails if the observed protection state drifts from the machine-readable ownership contract.
- live/external routing and production authority remain fail-closed during release qualification.

## Verified server-side branch state

The GitHub branch summary for `main` currently reports:

- `protected=false`;
- `protection.enabled=false`;
- required status-check enforcement `off`.

Accordingly, the machine contract records `branch_protection_verification=VERIFIED_DISABLED`. This is stronger evidence than the earlier `UNVERIFIED_INTEGRATION_FORBIDDEN` state: the branch is currently known **not** to be protected.

The dedicated branch-protection detail endpoint remains unavailable to the current integration, so this repository does not claim detailed review/ruleset configuration beyond what the branch summary exposes. In particular, required CODEOWNER review, force-push restrictions and detailed repository-ruleset state are not claimed as enabled.

## Assigned technical ownership

The repository-side ownership contract is machine-readable and fail-closed:

- artifact release owner: `@PetrFedin`;
- rollback owner: `@PetrFedin`;
- independent live approver: **unassigned**;
- branch-protection verification: `VERIFIED_DISABLED`;
- expected `main` protection: disabled;
- expected required status-check enforcement: `off`;
- artifact release allowed: `true`;
- live release allowed: `false`.

The same person may own artifact release and technical rollback while the product is still paper-only. That assignment is **not** treated as independent live approval. The validator rejects a future live-release claim unless branch protection is verified as enabled and a distinct independent live approver is assigned.

Canonical `main` ownership and branch-state evidence:

- commit: `9cf92a9993d9fd54896e5696115f673633d3ac2a`;
- workflow run: `31440945455`;
- retained evidence artifact: `9082811204`;
- artifact digest: `sha256:deb125851a0ca2db9486c342b723cb957cde2b87cb759d3d510423a8c1d5f672`.

That run performs the live GitHub branch-summary comparison and writes the observed current `main` SHA, protection state and required-status-check enforcement into the evidence artifact. Future qualifying runs fail if the observed GitHub state drifts from the committed machine contract.

## Trusted build evidence

First qualified signed provenance on canonical `main`:

- commit: `3dc298b4f6d8fba504e560762d101cae6d4070bc`;
- workflow run: `31336212403`;
- SLSA build-provenance attestation: `39704670`;
- signed SBOM attestation: `39704671`;
- retained evidence artifact: `9044407752`;
- evidence artifact digest: `sha256:50638e11323333a99e46dca19b39a57901986b5bb6392353232b94824136f2c3`.

The attestation workflow validates transferred SHA-256 checksums before signing. The same wheel/sdist subjects are used for build provenance and SBOM attestation.

## Release candidate requirements

A release candidate must satisfy all repository-side requirements below before it can be considered technically qualified:

1. The candidate commit is reachable from canonical `main`.
2. If a `v*` tag is used, the tag is exactly `v<project.version>` from `pyproject.toml`.
3. `release-governance` validates the ownership contract and artifact-release readiness.
4. `release-governance` re-reads the GitHub `main` branch summary and rejects protection-state drift.
5. `release-provenance` qualification succeeds from the hash-locked dependency graph.
6. Lock freshness and `pip-audit` succeed.
7. Full deterministic regression succeeds.
8. Wheel/sdist, release manifest and SPDX SBOM are generated from the same candidate commit.
9. SHA-256 transfer verification succeeds before attestation.
10. SLSA build provenance and SBOM attestations are generated successfully.
11. `external_order_routing_allowed` and `live_trading_allowed` remain false unless a separately governed live-release process has explicitly met all live-readiness prerequisites.
12. Server-side branch protection and required-review state must be enabled and independently verified before any release process relies on them as enforcement controls.
13. A live release additionally requires a distinct independent live approver; repository-side artifact ownership alone does not satisfy that requirement.

At present item 12 is **not satisfied**: the current branch summary reports protection disabled.

## Tag and artifact immutability

Published artifacts must not be replaced in place. A corrected build requires a new project version and a new tag. The governance/provenance workflows reject a `v*` tag whose version does not exactly match `pyproject.toml`, or whose commit is not reachable from `origin/main`.

## Rollback contract

Rollback is a source-control and deployment decision, not an artifact mutation:

- technical rollback owner: `@PetrFedin`;
- identify the last qualified commit with valid governance, provenance and SBOM evidence;
- revert the offending source change or advance to a corrective version;
- rebuild and re-attest from the resulting commit;
- do not overwrite previously attested distributions;
- keep live/external execution disabled unless the separate operational approval path explicitly permits it.

Technical release and rollback ownership are assigned and machine-validated. Independent live approval remains deliberately unassigned, and `main` server-side protection is currently verified disabled; neither gap is hidden by repository-side governance.
