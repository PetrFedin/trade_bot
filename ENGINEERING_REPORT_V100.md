# ASTRA 7.30.0 / Schema 100 engineering report

Schema 100 adds a concrete Alpaca paper-trading boundary without enabling live routing.

## Runtime controls

- credentials are loaded only from `ASTRA_ALPACA_PAPER_KEY_ID` and `ASTRA_ALPACA_PAPER_SECRET_KEY`;
- representations and evidence contain only a short credential fingerprint;
- REST and WebSocket endpoints are pinned to Alpaca paper hosts;
- reads use bounded retries for transport, timeout, throttle and server failures;
- submit, replace and cancel mutations are never automatically retried;
- read and mutation traffic have independent local token buckets;
- the trade-update stream requires authorization and an acknowledged `trade_updates` subscription;
- binary JSON frames are supported;
- duplicate frames are idempotent;
- filled-quantity or broker-time regression quarantines the stream;
- generation fencing rejects events from stale stream workers.

## Safety boundary

The adapter can be constructed with paper writes enabled only by an explicit caller. Repository defaults keep writes disabled. Live endpoints, live credentials and live order routing are not supported by this module.
