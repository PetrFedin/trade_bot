# Offline Qualification Verifier v1

This document is the operator contract for the independent ASTRA qualification verifier.

The verifier is intentionally network-independent and does **not** provide broker, OMS, risk, strategy-promotion or live/mainnet execution authority.

## Invocation

Run the verifier directly as a Python module:

```bash
python -m tools.qualification_offline_verify_v1 \
  --artifact ./artifact.json \
  --trusted-roots ./trusted-roots.json \
  --state-in ./trust-state.json \
  --state-out ./next-trust-state.json \
  --observed-at 2026-10-07T09:00:00+00:00
```

The module form is deliberate: the verifier does not mutate the historical release identity merely to add a console entry point.

## Inputs

### Portable artifact

`--artifact` must contain a canonical ASTRA Portable Artifact v1.

Verification order:

`artifact codec -> safe typed bundle decoder -> QualificationVerificationServiceV4`

Malformed canonical JSON, digest mismatches, schema mismatches and typed reconstruction failures fail closed.

### Trusted roots

`--trusted-roots` is a local JSON document with schema:

```json
{
  "schema_version": "astra-qualification-trusted-roots-v1",
  "roots": [
    {
      "key_id": "root-key-id",
      "public_key_b64": "..."
    }
  ]
}
```

Only explicit local Ed25519 public roots are accepted in v1. Dynamic key discovery is not part of this contract.

### Local TrustState

Normal verification requires `--state-in`.

The independently persisted local TrustState must exactly match the portable artifact embedded TrustState before bundle verification proceeds.

The embedded TrustState is transport context, not sufficient local authority.

A mismatch fails closed and no state advancement occurs.

## Genesis bootstrap

Missing local state never silently means genesis.

Bootstrap requires the explicit flag:

```bash
--allow-genesis-bootstrap
```

Bootstrap is accepted only when:

- profile event count is zero;
- profile event head is the genesis digest;
- Trust Checkpoint v4 SHA is the genesis digest;
- the embedded transparency anchor exactly matches the root-anchored base-v3 transparency head.

This is not trust-on-first-use for an arbitrary external root.

## observed_at

`--observed-at` is mandatory and must be timezone-aware ISO-8601.

Keeping verification time explicit makes repeated qualification evidence reproducible and prevents the verifier from silently depending on wall-clock time.

## Exit codes

- `0` — verification outcome is `VERIFIED` and the qualification is usable;
- `2` — verification completed, but the outcome is `REJECTED` or verified-but-unusable;
- `3` — malformed artifact/configuration/operator input.

`VERIFIED` and `usable` are intentionally separate concepts.

A cryptographically valid lifecycle update may make qualification unusable, for example after revocation or supersession. Such an authenticated update may still advance TrustState so the verifier retains the newest proven history.

A `REJECTED` artifact never advances TrustState.

## State output

`--state-out` is optional.

When a verified transition returns a next TrustState, the CLI writes it using:

`temporary file -> write -> flush -> fsync where supported -> atomic replace`

Errors and rejected verification preserve the previous trusted state.

Persistent multi-generation authority, CAS, process locking, recovery and history are handled by the subsequent Persistent TrustState Authority v1 layer rather than being overstated by this single-file CLI.

## Machine output

stdout is canonical JSON intended for machine consumption.

The result binds, at minimum:

- artifact identity;
- bundle identity;
- verification outcome;
- usability;
- failure code/detail when present;
- previous/next TrustState where applicable;
- verification timestamp;
- CLI contract version.

## Security boundary

The verifier requires no private key and no network connection.

It must not import or invoke broker, OMS, strategy, risk or live-routing authority.

Successful verification proves only the bounded qualification evidence represented by the verified artifact. It does not prove profitability, regulatory approval or permission to trade live capital.
