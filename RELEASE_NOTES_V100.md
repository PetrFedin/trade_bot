# ASTRA 7.30.0 / Schema 100

- concrete Alpaca paper REST adapter wrapper;
- opaque environment-backed paper credentials;
- hard-pinned paper REST and WebSocket endpoints;
- independent read/write rate limits;
- bounded read-only retry policy;
- no blind retry for order mutations;
- authenticated binary `trade_updates` parser;
- duplicate suppression, generation fencing and regression quarantine;
- PostgreSQL evidence/cursor migration, CLI, audits and CI.

No new forecasting formula was added. Live trading remains disabled.
