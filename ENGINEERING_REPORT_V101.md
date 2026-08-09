# Engineering report — Schema 101

## Objective

Provide a bounded, auditable procedure for proving that the external Alpaca paper account, authenticated event stream and minimal order round trip behave consistently before any longer paper deployment is allowed.

## Implemented boundaries

- Read-only account, open-order and stream probe before writes.
- Paper endpoint pinning for REST and WebSocket.
- Credentials loaded from environment and represented only by a short fingerprint.
- HMAC-sealed operator approval bound to generation, account, symbol, side, quantity, notional and expiry.
- Approval ID and nonce replay detection.
- Paper writes disabled by default.
- No automatic retry for submit, replace or cancel.
- Read-only lookup after ambiguous mutations.
- Sticky kill switch for unresolved outcomes, residual fills and protocol violations.
- Bounded cleanup proof requiring absence from open orders and a terminal or absent order snapshot.
- Append-only fsync JSONL evidence with sequence and SHA-256 chain verification.
- PostgreSQL schema with immutable event rows and false-only live-routing constraints.

## Deliberate exclusions

No live credentials, live endpoints, live routing permission, trading strategy changes or performance claims are included.
