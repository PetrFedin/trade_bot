from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

from app.marketdata.bybit_http import decode_public_json_response

_BPS = Decimal("10000")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_DAY_MS = 86_400_000
_INSTRUMENT_PATH = "/v5/market/instruments-info"
_TICKER_PATH = "/v5/market/tickers"
_ALLOWED_MAINNET_HOSTS = frozenset(
    {
        "api.bybit.com",
        "api.bytick.com",
        "api.bybit.nl",
        "api.bybit.tr",
        "api.bybit.kz",
        "api.bybitgeorgia.ge",
        "api.bybit.ae",
        "api.bybit.eu",
        "api.bybit.id",
        "api.manepa.jp",
        "api-spark-fintech.com",
    }
)
_NON_CRYPTO_SYMBOL_TYPES = frozenset({"commodity", "stock", "forex", "ETF"})
_STABLE_BASE_COINS = frozenset(
    {
        "USDT",
        "USDC",
        "USDE",
        "DAI",
        "FDUSD",
        "TUSD",
        "USDD",
        "PYUSD",
        "USD1",
        "RLUSD",
    }
)


@dataclass(frozen=True)
class BybitResearchUniversePolicy:
    top_n: int = 10
    minimum_listing_days: int = 90
    minimum_turnover_24h_usdt: Decimal = Decimal("20000000")
    minimum_open_interest_value_usdt: Decimal = Decimal("5000000")
    maximum_spread_bps: Decimal = Decimal("25")
    maximum_abs_funding_rate: Decimal = Decimal("0.01")
    turnover_weight: Decimal = Decimal("0.35")
    open_interest_weight: Decimal = Decimal("0.30")
    spread_weight: Decimal = Decimal("0.20")
    history_weight: Decimal = Decimal("0.15")

    def validate(self) -> None:
        if not 1 <= self.top_n <= 50:
            raise ValueError("Bybit research universe top_n must be within [1, 50]")
        if self.minimum_listing_days < 1:
            raise ValueError("Bybit research universe listing age must be positive")
        for name, value in (
            ("minimum_turnover_24h_usdt", self.minimum_turnover_24h_usdt),
            ("minimum_open_interest_value_usdt", self.minimum_open_interest_value_usdt),
            ("maximum_spread_bps", self.maximum_spread_bps),
            ("maximum_abs_funding_rate", self.maximum_abs_funding_rate),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(
                    f"Bybit research universe {name} must be finite and non-negative"
                )
        if self.maximum_spread_bps <= 0 or self.maximum_abs_funding_rate <= 0:
            raise ValueError("Bybit research universe spread/funding limits must be positive")
        weights = (
            self.turnover_weight,
            self.open_interest_weight,
            self.spread_weight,
            self.history_weight,
        )
        if any(not value.is_finite() or value < 0 for value in weights):
            raise ValueError(
                "Bybit research universe score weights must be finite and non-negative"
            )
        if sum(weights, start=_ZERO) != _ONE:
            raise ValueError("Bybit research universe score weights must sum exactly to 1")


@dataclass(frozen=True)
class BybitResearchInstrument:
    symbol: str
    base_coin: str
    quote_coin: str
    settle_coin: str
    contract_type: str
    status: str
    symbol_type: str
    launch_time_ms: int
    delivery_time_ms: int
    is_pre_listing: bool

    def structural_reasons(
        self,
        *,
        now_ms: int,
        minimum_listing_days: int,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.symbol != self.symbol.strip().upper() or not self.symbol:
            reasons.append("INVALID_SYMBOL")
        if self.base_coin != self.base_coin.strip().upper() or not self.base_coin:
            reasons.append("INVALID_BASE_COIN")
        if self.quote_coin != self.quote_coin.strip().upper() or not self.quote_coin:
            reasons.append("INVALID_QUOTE_COIN")
        if self.settle_coin != self.settle_coin.strip().upper() or not self.settle_coin:
            reasons.append("INVALID_SETTLE_COIN")
        if self.status != "Trading":
            reasons.append("NOT_TRADING")
        if self.contract_type != "LinearPerpetual":
            reasons.append("NOT_LINEAR_PERPETUAL")
        if self.quote_coin != "USDT" or self.settle_coin != "USDT":
            reasons.append("NOT_USDT_SETTLED")
        if self.symbol_type in _NON_CRYPTO_SYMBOL_TYPES:
            reasons.append("NON_CRYPTO_LINEAR_PRODUCT")
        if self.base_coin in _STABLE_BASE_COINS:
            reasons.append("STABLECOIN_BASE_EXCLUDED")
        if self.is_pre_listing:
            reasons.append("PRE_LISTING_EXCLUDED")
        if self.launch_time_ms < 0 or self.launch_time_ms > now_ms:
            reasons.append("INVALID_LAUNCH_TIME")
        elif now_ms - self.launch_time_ms < minimum_listing_days * _DAY_MS:
            reasons.append("INSUFFICIENT_LISTING_HISTORY")
        if self.delivery_time_ms < 0:
            reasons.append("INVALID_DELIVERY_TIME")
        elif self.delivery_time_ms != 0:
            if self.delivery_time_ms <= now_ms:
                reasons.append("DELIVERED_OR_DELISTED")
            else:
                reasons.append("SCHEDULED_DELISTING")
        return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class BybitResearchTicker:
    symbol: str
    last_price: Decimal
    bid_price: Decimal
    ask_price: Decimal
    turnover_24h_usdt: Decimal
    volume_24h: Decimal
    open_interest: Decimal
    open_interest_value_usdt: Decimal
    funding_rate: Decimal
    price_24h_fraction: Decimal

    @property
    def spread_bps(self) -> Decimal:
        if not self.bid_price.is_finite() or not self.ask_price.is_finite():
            return Decimal("Infinity")
        if self.bid_price <= 0 or self.ask_price <= 0 or self.ask_price < self.bid_price:
            return Decimal("Infinity")
        midpoint = (self.bid_price + self.ask_price) / Decimal("2")
        if midpoint <= 0:
            return Decimal("Infinity")
        return (self.ask_price - self.bid_price) / midpoint * _BPS

    def market_reasons(self, policy: BybitResearchUniversePolicy) -> tuple[str, ...]:
        reasons: list[str] = []
        values = (
            ("last_price", self.last_price),
            ("bid_price", self.bid_price),
            ("ask_price", self.ask_price),
            ("turnover_24h_usdt", self.turnover_24h_usdt),
            ("volume_24h", self.volume_24h),
            ("open_interest", self.open_interest),
            ("open_interest_value_usdt", self.open_interest_value_usdt),
            ("funding_rate", self.funding_rate),
            ("price_24h_fraction", self.price_24h_fraction),
        )
        for name, value in values:
            if not value.is_finite():
                reasons.append(f"NON_FINITE_{name.upper()}")
        top_of_book_finite = all(
            value.is_finite() for value in (self.last_price, self.bid_price, self.ask_price)
        )
        if top_of_book_finite:
            if self.last_price <= 0 or self.bid_price <= 0 or self.ask_price <= 0:
                reasons.append("INVALID_TOP_OF_BOOK")
            elif self.ask_price < self.bid_price:
                reasons.append("CROSSED_TOP_OF_BOOK")
            elif self.spread_bps > policy.maximum_spread_bps:
                reasons.append("SPREAD_ABOVE_MAXIMUM")
        if self.turnover_24h_usdt.is_finite():
            if self.turnover_24h_usdt < policy.minimum_turnover_24h_usdt:
                reasons.append("TURNOVER_24H_BELOW_MINIMUM")
        if self.volume_24h.is_finite() and self.volume_24h < 0:
            reasons.append("NEGATIVE_VOLUME_24H")
        if self.open_interest.is_finite() and self.open_interest < 0:
            reasons.append("NEGATIVE_OPEN_INTEREST")
        if self.open_interest_value_usdt.is_finite():
            if self.open_interest_value_usdt < policy.minimum_open_interest_value_usdt:
                reasons.append("OPEN_INTEREST_VALUE_BELOW_MINIMUM")
        if self.funding_rate.is_finite():
            if abs(self.funding_rate) > policy.maximum_abs_funding_rate:
                reasons.append("FUNDING_RATE_EXTREME")
        return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class BybitResearchUniverseCandidate:
    rank: int
    symbol: str
    score: Decimal
    listing_days: int
    turnover_24h_usdt: Decimal
    open_interest_value_usdt: Decimal
    spread_bps: Decimal
    funding_rate: Decimal
    price_24h_fraction: Decimal
    turnover_percentile: Decimal
    open_interest_percentile: Decimal
    spread_quality_percentile: Decimal
    history_percentile: Decimal


@dataclass(frozen=True)
class BybitResearchUniverseSelection:
    observed_at_ms: int
    host: str
    policy: BybitResearchUniversePolicy
    selected: tuple[BybitResearchUniverseCandidate, ...]
    eligible_symbol_count: int
    excluded_reasons: Mapping[str, tuple[str, ...]]
    source_instrument_count: int
    source_ticker_count: int
    complete_top_n: bool
    blockers: tuple[str, ...]
    research_only: bool = True
    strategy_parameters_changed: bool = False
    strategy_promotion_allowed: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        self.policy.validate()
        validate_bybit_public_research_host(self.host)
        if isinstance(self.observed_at_ms, bool) or self.observed_at_ms < 0:
            raise ValueError("Bybit research universe observation time is invalid")
        if len(self.selected) > self.policy.top_n:
            raise ValueError("Bybit research universe selected more than top_n")
        if self.complete_top_n != (len(self.selected) == self.policy.top_n):
            raise ValueError("Bybit research universe completeness flag is inconsistent")
        if self.eligible_symbol_count < len(self.selected):
            raise ValueError("Bybit research universe eligible count is inconsistent")
        if self.source_instrument_count < self.eligible_symbol_count:
            raise ValueError("Bybit research universe instrument count is inconsistent")
        if self.source_ticker_count < self.eligible_symbol_count:
            raise ValueError("Bybit research universe ticker count is inconsistent")
        if self.complete_top_n and self.blockers:
            raise ValueError("complete Bybit research universe cannot carry blockers")
        if not self.complete_top_n and "INSUFFICIENT_ELIGIBLE_SYMBOLS" not in self.blockers:
            raise ValueError("incomplete Bybit research universe must explain missing candidates")
        previous_score: Decimal | None = None
        seen: set[str] = set()
        for expected_rank, item in enumerate(self.selected, start=1):
            if item.rank != expected_rank:
                raise ValueError("Bybit research universe ranks must be contiguous")
            if item.symbol in seen:
                raise ValueError("Bybit research universe contains duplicate symbol")
            if previous_score is not None and item.score > previous_score:
                raise ValueError("Bybit research universe must be score-descending")
            if not item.score.is_finite() or not _ZERO <= item.score <= _ONE:
                raise ValueError("Bybit research universe score must be within [0, 1]")
            seen.add(item.symbol)
            previous_score = item.score
        if (
            not self.research_only
            or self.strategy_parameters_changed
            or self.strategy_promotion_allowed
            or self.demo_activation_allowed
            or self.live_activation_allowed
            or self.bybit_live_order_routing_allowed
        ):
            raise ValueError("Bybit research universe cannot grant trading activation")


@dataclass(frozen=True)
class BybitResearchUniverseHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


Transport = Callable[[str, Mapping[str, str]], BybitResearchUniverseHttpJson]


class BybitResearchUniverseClient:
    """Read-only public Bybit universe snapshot used only for research candidate selection."""

    def __init__(
        self,
        *,
        host: str = "api.bybit.com",
        transport: Transport | None = None,
        maximum_instrument_pages: int = 10,
    ) -> None:
        self.host = validate_bybit_public_research_host(host)
        if not 1 <= maximum_instrument_pages <= 50:
            raise ValueError("Bybit instrument pagination bound must be within [1, 50]")
        self._transport = _https_transport if transport is None else transport
        self._maximum_instrument_pages = maximum_instrument_pages

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    @property
    def order_writes_supported(self) -> bool:
        return False

    def fetch_and_select(
        self,
        *,
        observed_at_ms: int,
        policy: BybitResearchUniversePolicy | None = None,
    ) -> BybitResearchUniverseSelection:
        active = BybitResearchUniversePolicy() if policy is None else policy
        active.validate()
        instruments = self.fetch_instruments()
        tickers = self.fetch_tickers()
        return select_bybit_research_universe(
            instruments,
            tickers,
            observed_at_ms=observed_at_ms,
            host=self.host,
            policy=active,
        )

    def fetch_instruments(self) -> tuple[BybitResearchInstrument, ...]:
        cursor = ""
        pages = 0
        rows: list[BybitResearchInstrument] = []
        seen_symbols: set[str] = set()
        while True:
            pages += 1
            if pages > self._maximum_instrument_pages:
                raise ValueError("Bybit research instrument pagination exceeded safety bound")
            query: dict[str, str] = {"category": "linear", "limit": "1000"}
            if cursor:
                query["cursor"] = cursor
            payload = self._get(_INSTRUMENT_PATH, query)
            result = _result_object(payload, expected_category="linear")
            raw_rows = result.get("list")
            if not isinstance(raw_rows, list):
                raise ValueError("Bybit research instrument response missing list")
            for raw in raw_rows:
                if not isinstance(raw, Mapping):
                    raise ValueError("Bybit research instrument row must be an object")
                instrument = _parse_instrument(raw)
                if instrument.symbol in seen_symbols:
                    raise ValueError(
                        "Bybit research instrument pagination returned duplicate symbol"
                    )
                seen_symbols.add(instrument.symbol)
                rows.append(instrument)
            next_cursor = result.get("nextPageCursor")
            if next_cursor in (None, ""):
                break
            if not isinstance(next_cursor, str) or next_cursor == cursor:
                raise ValueError("Bybit research instrument cursor is invalid")
            cursor = next_cursor
        return tuple(sorted(rows, key=lambda item: item.symbol))

    def fetch_tickers(self) -> tuple[BybitResearchTicker, ...]:
        payload = self._get(_TICKER_PATH, {"category": "linear"})
        result = _result_object(payload, expected_category="linear")
        raw_rows = result.get("list")
        if not isinstance(raw_rows, list):
            raise ValueError("Bybit research ticker response missing list")
        tickers: list[BybitResearchTicker] = []
        seen: set[str] = set()
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                raise ValueError("Bybit research ticker row must be an object")
            ticker = _parse_ticker(raw)
            if ticker.symbol in seen:
                raise ValueError("Bybit research ticker response contains duplicate symbol")
            seen.add(ticker.symbol)
            tickers.append(ticker)
        return tuple(sorted(tickers, key=lambda item: item.symbol))

    def _get(self, path: str, query: Mapping[str, str]) -> Mapping[str, Any]:
        if path not in {_INSTRUMENT_PATH, _TICKER_PATH}:
            raise ValueError("Bybit research universe rejected non-allowlisted public path")
        url = f"https://{self.host}{path}?{urlencode(query)}"
        response = self._transport(url, {"Accept": "application/json"})
        if response.status_code != 200:
            raise ValueError(f"Bybit research universe request failed:{response.status_code}")
        if response.payload.get("retCode") != 0:
            raise ValueError(
                f"Bybit research universe API error:{response.payload.get('retMsg')}"
            )
        return response.payload


def select_bybit_research_universe(
    instruments: Sequence[BybitResearchInstrument],
    tickers: Sequence[BybitResearchTicker],
    *,
    observed_at_ms: int,
    host: str = "api.bybit.com",
    policy: BybitResearchUniversePolicy | None = None,
) -> BybitResearchUniverseSelection:
    active = BybitResearchUniversePolicy() if policy is None else policy
    active.validate()
    normalized_host = validate_bybit_public_research_host(host)
    if (
        isinstance(observed_at_ms, bool)
        or not isinstance(observed_at_ms, int)
        or observed_at_ms < 0
    ):
        raise ValueError(
            "Bybit research universe observation time must be non-negative integer ms"
        )
    instrument_map = _unique_by_symbol(instruments, kind="instrument")
    ticker_map = _unique_by_symbol(tickers, kind="ticker")
    eligible: list[tuple[BybitResearchInstrument, BybitResearchTicker]] = []
    excluded: dict[str, tuple[str, ...]] = {}
    for symbol, instrument in instrument_map.items():
        reasons = list(
            instrument.structural_reasons(
                now_ms=observed_at_ms,
                minimum_listing_days=active.minimum_listing_days,
            )
        )
        ticker = ticker_map.get(symbol)
        if ticker is None:
            reasons.append("TICKER_MISSING")
        else:
            reasons.extend(ticker.market_reasons(active))
        deduped = tuple(dict.fromkeys(reasons))
        if deduped:
            excluded[symbol] = deduped
            continue
        if ticker is None:
            raise RuntimeError(
                "Bybit research universe lost ticker after eligibility validation"
            )
        eligible.append((instrument, ticker))

    turnover_values = [ticker.turnover_24h_usdt for _instrument, ticker in eligible]
    oi_values = [ticker.open_interest_value_usdt for _instrument, ticker in eligible]
    spread_values = [ticker.spread_bps for _instrument, ticker in eligible]
    history_values = [
        Decimal((observed_at_ms - instrument.launch_time_ms) // _DAY_MS)
        for instrument, _ticker in eligible
    ]
    scored: list[
        tuple[str, Decimal, int, BybitResearchTicker, tuple[Decimal, ...]]
    ] = []
    for instrument, ticker in eligible:
        listing_days = (observed_at_ms - instrument.launch_time_ms) // _DAY_MS
        turnover_rank = _percentile(ticker.turnover_24h_usdt, turnover_values)
        oi_rank = _percentile(ticker.open_interest_value_usdt, oi_values)
        spread_rank = _percentile(ticker.spread_bps, spread_values, reverse=True)
        history_rank = _percentile(Decimal(listing_days), history_values)
        score = (
            turnover_rank * active.turnover_weight
            + oi_rank * active.open_interest_weight
            + spread_rank * active.spread_weight
            + history_rank * active.history_weight
        )
        scored.append(
            (
                instrument.symbol,
                score,
                listing_days,
                ticker,
                (turnover_rank, oi_rank, spread_rank, history_rank),
            )
        )
    scored.sort(key=lambda item: (-item[1], -item[3].turnover_24h_usdt, item[0]))
    selected = tuple(
        BybitResearchUniverseCandidate(
            rank=rank,
            symbol=symbol,
            score=score,
            listing_days=listing_days,
            turnover_24h_usdt=ticker.turnover_24h_usdt,
            open_interest_value_usdt=ticker.open_interest_value_usdt,
            spread_bps=ticker.spread_bps,
            funding_rate=ticker.funding_rate,
            price_24h_fraction=ticker.price_24h_fraction,
            turnover_percentile=percentiles[0],
            open_interest_percentile=percentiles[1],
            spread_quality_percentile=percentiles[2],
            history_percentile=percentiles[3],
        )
        for rank, (symbol, score, listing_days, ticker, percentiles) in enumerate(
            scored[: active.top_n], start=1
        )
    )
    complete = len(selected) == active.top_n
    blockers = () if complete else ("INSUFFICIENT_ELIGIBLE_SYMBOLS",)
    selection = BybitResearchUniverseSelection(
        observed_at_ms=observed_at_ms,
        host=normalized_host,
        policy=active,
        selected=selected,
        eligible_symbol_count=len(eligible),
        excluded_reasons=excluded,
        source_instrument_count=len(instrument_map),
        source_ticker_count=len(ticker_map),
        complete_top_n=complete,
        blockers=blockers,
    )
    selection.validate()
    return selection


def validate_bybit_public_research_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized != host or normalized not in _ALLOWED_MAINNET_HOSTS:
        raise ValueError("Bybit public research host is not in the audited mainnet allowlist")
    return normalized


def _unique_by_symbol(items: Sequence[Any], *, kind: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        symbol = getattr(item, "symbol", None)
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f"Bybit research {kind} item has invalid symbol")
        if symbol in result:
            raise ValueError(f"Bybit research {kind} input contains duplicate symbol")
        result[symbol] = item
    return result


def _percentile(
    value: Decimal,
    values: Sequence[Decimal],
    *,
    reverse: bool = False,
) -> Decimal:
    if not values:
        raise ValueError("Bybit research percentile requires a non-empty population")
    if any(not item.is_finite() for item in values) or not value.is_finite():
        raise ValueError("Bybit research percentile requires finite values")
    better = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    percentile = (Decimal(better) + Decimal(equal) / Decimal("2")) / Decimal(len(values))
    return _ONE - percentile if reverse else percentile


def _parse_instrument(row: Mapping[str, Any]) -> BybitResearchInstrument:
    is_pre_listing = row.get("isPreListing", False)
    if not isinstance(is_pre_listing, bool):
        raise ValueError("Bybit research instrument isPreListing must be boolean")
    return BybitResearchInstrument(
        symbol=_required_text(row, "symbol"),
        base_coin=_required_text(row, "baseCoin"),
        quote_coin=_required_text(row, "quoteCoin"),
        settle_coin=_required_text(row, "settleCoin"),
        contract_type=_required_text(row, "contractType"),
        status=_required_text(row, "status"),
        symbol_type=_optional_text(row.get("symbolType")),
        launch_time_ms=_required_non_negative_int(row, "launchTime"),
        delivery_time_ms=_required_non_negative_int(row, "deliveryTime"),
        is_pre_listing=is_pre_listing,
    )


def _parse_ticker(row: Mapping[str, Any]) -> BybitResearchTicker:
    return BybitResearchTicker(
        symbol=_required_text(row, "symbol"),
        last_price=_required_decimal(row, "lastPrice"),
        bid_price=_required_decimal(row, "bid1Price"),
        ask_price=_required_decimal(row, "ask1Price"),
        turnover_24h_usdt=_required_decimal(row, "turnover24h"),
        volume_24h=_required_decimal(row, "volume24h"),
        open_interest=_required_decimal(row, "openInterest"),
        open_interest_value_usdt=_required_decimal(row, "openInterestValue"),
        funding_rate=_required_decimal(row, "fundingRate"),
        price_24h_fraction=_required_decimal(row, "price24hPcnt"),
    )


def _result_object(
    payload: Mapping[str, Any],
    *,
    expected_category: str,
) -> Mapping[str, Any]:
    result = payload.get("result")
    if not isinstance(result, Mapping) or result.get("category") != expected_category:
        raise ValueError("Bybit research universe response missing expected result category")
    return result


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Bybit research universe response missing {field}")
    return value


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Bybit research universe optional text field is invalid")
    return value


def _required_non_negative_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"Bybit research universe response missing {field}")
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise ValueError(f"Bybit research universe response has invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"Bybit research universe response has negative {field}")
    return parsed


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value in (None, ""):
        raise ValueError(f"Bybit research universe response missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Bybit research universe response has invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"Bybit research universe response has non-finite {field}")
    return parsed


def _https_transport(
    url: str,
    headers: Mapping[str, str],
) -> BybitResearchUniverseHttpJson:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_MAINNET_HOSTS:
        raise ValueError("Bybit research universe transport rejected endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Bybit research universe transport rejected ambiguous authority")
    if parsed.port not in (None, 443):
        raise ValueError("Bybit research universe transport requires HTTPS port 443")
    if parsed.path not in {_INSTRUMENT_PATH, _TICKER_PATH}:
        raise ValueError("Bybit research universe transport rejected unexpected path")
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = HTTPSConnection(parsed.hostname, 443, timeout=30)
    try:
        connection.request("GET", target, headers=dict(headers))
        response = connection.getresponse()
        response_headers = {key: value for key, value in response.getheaders()}
        payload = decode_public_json_response(
            status_code=response.status,
            headers=response_headers,
            body=response.read(),
        )
        return BybitResearchUniverseHttpJson(
            status_code=response.status,
            headers=response_headers,
            payload=payload,
        )
    finally:
        connection.close()
