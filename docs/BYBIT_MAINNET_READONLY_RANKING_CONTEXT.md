# Bybit mainnet read-only ranking context (v115)

## Purpose

This layer connects the already-qualified authenticated Bybit mainnet **read-only** account boundary to the already-qualified live evidence ranking pipeline.

It answers questions the market-only registry cannot answer by itself:

- what is the account's current total equity;
- how much balance is currently available under the Unified Account margin model;
- how much initial and maintenance margin is already consumed;
- what unrealised perpetual PnL is present;
- which symbols already have long, short or hedged exposure;
- how large the existing gross/net position value is;
- what the unchanged fixed strategy's proposed notional represents relative to the currently available sizing capital.

It does **not** add a live order surface.

## Credential boundary

Credentials remain isolated in the existing PR #50 environment variables:

```text
BYBIT_MAINNET_READONLY_API_KEY
BYBIT_MAINNET_READONLY_API_SECRET
BYBIT_MAINNET_READONLY_SITE
```

The runtime first calls the existing `GET /v5/user/query-api` verification path and requires:

- the returned key identity to match the configured key;
- `readOnly=1`;
- a concrete IP binding;
- an allowlisted regional Bybit mainnet host.

Only after those checks does the probe read account, wallet and positions.

No raw API key or API secret is stored in v115. The audit record keeps only the SHA-256 API-key fingerprint already produced by the qualified read-only boundary.

Official Bybit documentation:

- API key information: https://bybit-exchange.github.io/docs/v5/user/apikey-info
- wallet balance: https://bybit-exchange.github.io/docs/v5/account/wallet-balance

## Equity is not the same as available balance

Bybit documents `totalEquity` as account total equity in USD and `totalAvailableBalance` as account available balance in USD. They are not interchangeable.

The v115 sizing context therefore uses:

```text
sizing_capital_usd_equivalent = min(totalEquity, totalAvailableBalance)
```

only when both values are positive and finite.

This is deliberately named **USD-equivalent sizing capital**. It is not presented as USDT cash, withdrawable USDT, or a guaranteed executable amount. The existing strategy planner still receives a decimal equity input, but the `equity_source` is explicitly:

```text
BYBIT_MAINNET_READONLY_AVAILABLE_CAPITAL_USD_EQUIVALENT
```

so downstream audit can distinguish real-account sizing context from the prior research reference equity.

If no positive sizing capital exists, the account-aware refresh stops before market ranking.

## Exposure context

Current Bybit positions are grouped by symbol:

- `Buy` position value contributes to long exposure;
- `Sell` position value contributes to short exposure;
- both directions on the same symbol are represented as hedged exposure;
- gross exposure = long value + short value;
- net exposure = long value - short value.

The account overlay preserves the evidence ranking order. It does not boost or demote a coin based on the current account.

For each ranked candidate it reports the existing relationship:

- `NO_SIGNAL`;
- `NO_EXISTING_POSITION`;
- `SAME_DIRECTION_EXISTING_POSITION`;
- `OPPOSING_EXISTING_POSITION`;
- `EXISTING_HEDGED_POSITION`.

The proposed notional is additionally shown relative to sizing capital and against a conservative current-gross-plus-proposed-notional upper bound.

If Bybit reports an open position without a usable `positionValue`, the account exposure is marked incomplete and the operational account-aware refresh fails closed before ranking. Missing position value is never treated as zero exposure.

## What this layer intentionally does not do

This layer does not introduce arbitrary new account-risk thresholds such as:

- maximum gross leverage;
- maximum margin utilisation;
- maximum same-symbol concentration;
- maximum correlated exposure;
- automatic rejection of same-direction positions;
- automatic close/reduce/hedge actions.

Those are decision policies and require their own explicit qualification. v115 first establishes a trustworthy observed account state and auditable sizing/exposure facts.

## Live flow

```text
persisted qualified evidence matrix
-> verify read-only Bybit API key + IP binding
-> read account / wallet / USDT-settled positions
-> build v115 safe account context
-> require positive available sizing capital
-> require complete position-value exposure
-> existing current Top-10/Top-50 public market refresh
-> existing fixed 5m strategy signal
-> current OI/crowding/funding
-> existing evidence-ranked v111 opportunity snapshot
-> v115 account overlay without reranking
-> operator review
```

The market data site is taken from the same `BYBIT_MAINNET_READONLY_SITE` used for the authenticated account boundary, keeping public and private Bybit regional host selection consistent.

## PostgreSQL audit trail

`astra_bybit_mainnet_readonly_context_v115` is append-only and references the exact v111 ranking snapshot used for the account overlay.

It persists only safe operational state:

- ranking snapshot ID;
- observation timestamp;
- regional API host;
- API key fingerprint, not the key;
- verified read-only/IP-binding booleans;
- equity / wallet / margin / available balance / UPL / IM / MM;
- sizing capital;
- gross / long / short / net position value;
- position count and completeness;
- canonical safe context JSON.

Update/delete mutations are rejected by trigger. `PUBLIC` privileges are revoked.

The existing v111 evidence snapshot is content-addressed. During a normal account-aware refresh it has already been read from the same PostgreSQL store; the existing persistence path is idempotent and does not change the stored evidence observation time on conflict.

## Operational GitHub workflow

The account-aware tool already performs the complete authenticated read-only probe and registry refresh. The operational workflow now exposes that path without putting credentials into the repository.

Configure GitHub Actions **Secrets**:

```text
BYBIT_MAINNET_READONLY_API_KEY
BYBIT_MAINNET_READONLY_API_SECRET
BYBIT_OPPORTUNITY_DATABASE_DSN
```

Configure GitHub Actions **Variables**:

```text
BYBIT_MAINNET_READONLY_SITE=eu
BYBIT_MAINNET_READONLY_OPERATIONAL_ENABLED=false
```

Use the regional profile actually associated with the issued API key. `global` is used only when the site variable is absent during an explicit manual run.

### First connection sequence

1. Create a dedicated Bybit mainnet HMAC API key with **Read-Only** enabled.
2. Bind it to the fixed egress IP address or addresses of the runner/deployment path that will make the request.
3. Save key and secret as protected GitHub Actions secrets; never place them in a PR, issue, chat transcript, repository file or workflow input.
4. Keep `BYBIT_MAINNET_READONLY_OPERATIONAL_ENABLED=false`.
5. Apply the already-qualified v110/v111/v115 PostgreSQL migrations through the controlled migration path.
6. Run `bybit-mainnet-readonly-ranking-context` manually through `workflow_dispatch`.
7. Verify the sanitized artifact reports `read_only_verified=true`, `ip_binding_verified=true`, `order_writes_supported=false` and `bybit_live_order_routing_allowed=false`.
8. Only after the manual probe is proven stable may the repository variable be changed to `BYBIT_MAINNET_READONLY_OPERATIONAL_ENABLED=true` to enable the ten-minute read-only refresh.

The scheduled job runs at minute 7/17/27/37/47/57. This offset reduces contention with the market/shadow/materialization jobs already using the 0/5-minute portions of the ten-minute cycle. GitHub cron remains best-effort rather than a hard real-time scheduler.

### Operational outputs

A successful run writes:

- current evidence-ranked Top-50/Top-10 JSON using real account sizing capital;
- sanitized account-aware context JSON;
- explicit status JSON;
- a short operational log.

Artifacts are retained for seven days because they contain account balances and exposure context even though they contain no API secret or raw API key.

Missing secrets/DSN produce `READONLY_OPERATIONAL_CONFIG_UNAVAILABLE`. Missing v115 schema or regional public access is reported explicitly. A failed key-identity, read-only, secret-marker or IP-binding verification is a hard workflow failure.

## Safety boundary

Every v115 object enforces:

```text
operator_review_required=true
trade_actionable=false
order_writes_supported=false
live_mainnet_order_routing_allowed=false
```

The account-aware wrapper has no create/amend/cancel methods. The underlying PR #50 mainnet client remains GET-only.

This infrastructure can support operator-approved demo or future separately qualified execution workflows, but it does not authorize autonomous real-money trading by the assistant or the research/ranking subsystem.
