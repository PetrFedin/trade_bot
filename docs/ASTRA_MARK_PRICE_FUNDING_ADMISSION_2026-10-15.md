# ASTRA — historical Mark Price funding admission

Date: 2026-10-15. **Research only. No strategy promotion.**
Status: source-admission framework implemented; genuine historical mark-price
dataset has NOT yet been collected or qualified.

## Why this gate is required

The existing ASTRA funding studies use retained public Bybit settlement rates,
but some historical simulations approximate funding notional from entry price.
That is not venue-exact for USDT perpetuals.

Bybit's current official documentation states that for USDT perpetual
contracts:

`Funding Fee = Position Value × Funding Rate`

and

`Position Value = Contract Quantity × Mark Price`.

The mark price must therefore be tied to the funding settlement, not silently
substituted with last traded price, daily close or original entry price.
Bybit also notes that a position opened or closed roughly five seconds around a
funding event may have uncertain inclusion in that funding cycle.

Historical Mark Price Klines are available through the public V5
`/v5/market/mark-price-kline` endpoint. That endpoint gives OHLC bars rather
than an account-authoritative instantaneous settlement record. This research
gate therefore treats a **1-minute mark candle beginning exactly at each
funding timestamp as an uncertainty envelope**, not as proof of the exact mark
used by the user's account.

## Admission contract

For every retained funding settlement of a symbol, require exactly one mark
price source row with:

- identical UTC settlement-minute timestamp;
- canonical symbol;
- 1-minute interval only;
- open/high/low/close finite and OHLC-valid;
- SHA-256-bound raw CSV;
- manifest identifying Bybit public Mark Price Kline endpoint;
- linear category;
- `research_only=true`;
- `strategy_promotion_allowed=false`.

The set of mark timestamps must be **exactly equal** to the set of retained
funding timestamps. Missing or extra mark minutes fail closed. No interpolation,
forward filling, nearest-candle substitution or Last Price fallback is allowed.

## Funding cashflow uncertainty

For USDT perpetual quantity `Q`, funding rate `R` and 1-minute mark price
range `[L,H]`, the candidate cashflow is bounded by evaluating:

`Q × mark × R`

at both `L` and `H`, with account sign reversed for long positions when the
rate is positive. This is a source-resolution envelope only.

The module explicitly returns:

- `mark_price_exact_at_settlement_verified=false`;
- `account_funding_ledger_verified=false`;
- `provider_payload_signature_verified=false`;
- `strategy_replay_completed=false`;
- `profitability_proven=false`.

No research result may relabel these fields to true without stronger evidence.

## What remains before portfolio replay

1. Collect historical 1-minute Bybit Mark Price Klines for every funding
   timestamp used in the selected six-coin and broad-21 comparison windows.
2. Preserve raw query ranges, provider response bytes or canonical extracts,
   timestamp coverage and SHA-256 manifests.
3. Run this admission tool per symbol.
4. Integrate mark-price low/high envelopes into the exact daily cash-feedback
   funding replay.
5. Keep funding-participation uncertainty for positions opened/closed around
   settlement times distinct from mark-price uncertainty.
6. Compare both six-major and broad-21 portfolios on the same fully-covered
   chronology, same cash, fee/slippage assumptions and original Strategy A.
7. Independently review arithmetic and negative outcomes before any paper/live
   discussion.

The current positive six-major historical replay therefore remains a
**model-dependent research observation**, not proof of live expected return.

`PROFITABILITY_NOT_PROVEN`. Production/main/broker remain HOLD.
