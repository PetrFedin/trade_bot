# Schema 100 operator runbook

## Before connecting

- verify the host is `paper-api.alpaca.markets`;
- verify the credential fingerprint against the approved paper account;
- confirm the live domain is absent from configuration;
- keep paper writes disabled during account and stream qualification.

## Stream qualification

1. Authenticate the WebSocket connection.
2. Confirm `authorization.status=authorized`.
3. Subscribe to `trade_updates`.
4. Confirm the `listening` response includes `trade_updates`.
5. Quarantine on unknown events, time regression, filled-quantity regression or stale generation.

## Mutation incident

Never retry an ambiguous submit, replace or cancel blindly. Use read-only lookup and reconciliation from Schema 99. Engage the kill switch when any residual fill or unidentified order exists.
