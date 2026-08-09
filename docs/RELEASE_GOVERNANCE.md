# Release Governance

This document defines the repository-side release contract for ASTRA Trade Bot. It does not claim that GitHub server-side branch protection or required-review settings are enabled unless those settings are independently verified through the GitHub control plane.

## Current enforcement boundary

Repository-side controls are implemented and executable:

- `requirements.lock` is SHA-256 hash locked and checked for freshness against `pyproject.toml`.
- operational GitHub Actions use exact approved Node 24-native commit SHAs.
- release qualification runs dependency audit, deterministic regression, wheel/sdist build, SPDX SBOM generation and release-manifest generation.
- trusted `main`/`v*` builds generate signed GitHub/Sigstore SLSA provenance and signed SBOM attestations.
- pull-request code does not receive attestation/OIDC write privileges; the privileged attestation job only runs after a trusted push.
- live/external routing and production authority remain fail-closed during release qualification.

Server-side controls are **UNVERIFIED** from the current automation integration:

- `main` branch protection / repository ruleset enforcement;
- required approving reviews / required CODEOWNER review;
- force-push/deletion restrictions;
- required status-check selection at the GitHub server layer.

The current GitHub integration receives `403 Resource not accessible by integration` from the branch-protection endpoint, so repository files must not be treated as proof of server-side enforcement.

## Trusted build evidence

First qualified signed provenance on canonical `main`:

- commit: `3dc298b4f6d8fba504e560762d101cae6d4070bc`
- workflow run: `31336212403`
- SLSA build-provenance attestation: `39704670`
- signed SBOM attestation: `39704671`
- retained evidence artifact: `9044407752`
- evidence artifact digest: `sha256:50638e11323333a99e46dca19b39a57901986b5bb6392353232b94824136f2c3`

The attestation workflow validates transferred SHA-256 checksums before signing. The same wheel/sdist subjects are used for build provenance and SBOM attestation.

## Release candidate requirements

A release candidate must satisfy all repository-side requirements below before it can be considered technically qualified:

1. The candidate commit is reachable from canonical `main`.
2. If a `v*` tag is used, the tag is exactly `v<project.version>` from `pyproject.toml`.
3. `release-provenance` qualification succeeds from the hash-locked dependency graph.
4. Lock freshness and `pip-audit` succeed.
5. Full deterministic regression succeeds.
6. Wheel/sdist, release manifest and SPDX SBOM are generated from the same candidate commit.
7. SHA-256 transfer verification succeeds before attestation.
8. SLSA build provenance and SBOM attestations are generated successfully.
9. `external_order_routing_allowed` and `live_trading_allowed` remain false unless a separately governed live-release process has explicitly met all live-readiness prerequisites.
10. Server-side branch protection and required-review state is verified independently before any release process relies on it as an enforcement control.

## Tag and artifact immutability

Published artifacts must not be replaced in place. A corrected build requires a new project version and a new tag. The provenance workflow rejects a `v*` tag whose version does not exactly match `pyproject.toml`, or whose commit is not reachable from `origin/main`.

## Rollback contract

Rollback is a source-control and deployment decision, not an artifact mutation:

- identify the last qualified commit with valid provenance and SBOM attestations;
- revert the offending source change or advance to a corrective version;
- rebuild and re-attest from the resulting commit;
- do not overwrite previously attested distributions;
- keep live/external execution disabled unless the separate operational approval path explicitly permits it.

An operational release owner and rollback approver still need to be assigned as an external governance control. This document intentionally does not invent those approvals.
