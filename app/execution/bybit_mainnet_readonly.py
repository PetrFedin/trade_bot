from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlencode

from app.execution.bybit_rest_policy import (
    BybitRestPolicy,
    BybitRestProtocolError,
    SleepFn,
    raise_for_bybit_response,
    run_bybit_read_with_retry,
)

_DEFAULT_MAINNET_HOST = "api.bybit.com"
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
_RECV_WINDOW_MS = 5000
_ALLOWED_READ_PATHS = frozenset(
    {
        "/v5/user/query-api",
        "/v5/account/wallet-balance",
        "/v5/account/info",
        "/v5/account/fee-rate",
        "/v5/account/transaction-log",
        "/v5/position/list",
        "/v5/position/closed-pnl",
        "/v5/execution/list",
        "/v5/order/realtime",
        "/v5/order/history",
    }
)
_ALLOWED_MARGIN_MODES = frozenset(
    {"ISOLATED_MARGIN", "REGULAR_MARGIN", "PORTFOLIO_MARGIN"}
)


class BybitMainnetReadOnlyError(RuntimeError):
    """Raised when the mainnet account boundary cannot prove read-only safety."""


@dataclass(frozen=True)
class BybitMainnetReadOnlyHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


class BybitMainnetReadOnlyTransport(Protocol):
    """A deliberately GET-only transport contract."""

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> Mapping[str, Any] | BybitMainnetReadOnlyHttpJson: ...


@dataclass(frozen=True)
class BybitMainnetApiKeyInfo:
    key_fingerprint_sha256: str
    read_only: bool
    ip_bindings: tuple[str, ...]
    key_type: int | None
    note: str | None
    permissions: tuple[str, ...]
    environment: str = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    def validate(self) -> None:
        if len(self.key_fingerprint_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.key_fingerprint_sha256
        ):
            raise ValueError("Bybit API-key fingerprint must be lowercase sha256 hex")
        if not self.read_only:
            raise BybitMainnetReadOnlyError("Bybit API key is not read-only")
        if any(not value.strip() for value in self.ip_bindings):
            raise ValueError("Bybit API-key IP bindings must be non-empty strings")
        if self.key_type is not None and self.key_type not in {1, 2}:
            raise ValueError("Bybit API-key type is unsupported")
        if self.environment != "BYBIT_MAINNET_READONLY":
            raise ValueError("Bybit mainnet read-only environment marker is invalid")
        if self.live_mainnet_order_routing_allowed or self.order_writes_supported:
            raise ValueError("Bybit mainnet read-only metadata cannot grant order writes")


@dataclass(frozen=True)
class BybitMainnetWalletBalance:
    total_equity_usd: Decimal
    total_wallet_balance_usd: Decimal
    total_margin_balance_usd: Decimal
    total_available_balance_usd: Decimal
    total_perp_upl_usd: Decimal
    total_initial_margin_usd: Decimal
    total_maintenance_margin_usd: Decimal
    usdt_wallet_balance: Decimal | None
    environment: str = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        values = (
            self.total_equity_usd,
            self.total_wallet_balance_usd,
            self.total_margin_balance_usd,
            self.total_available_balance_usd,
            self.total_perp_upl_usd,
            self.total_initial_margin_usd,
            self.total_maintenance_margin_usd,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("Bybit mainnet wallet fields must be finite")
        if self.total_equity_usd < 0:
            raise ValueError("Bybit mainnet total equity cannot be negative")
        if self.total_initial_margin_usd < 0 or self.total_maintenance_margin_usd < 0:
            raise ValueError("Bybit mainnet margin requirements cannot be negative")
        if self.usdt_wallet_balance is not None and not self.usdt_wallet_balance.is_finite():
            raise ValueError("Bybit mainnet USDT wallet balance must be finite")
        if self.live_mainnet_order_routing_allowed:
            raise ValueError("read-only wallet snapshot cannot grant live routing")


@dataclass(frozen=True)
class BybitMainnetAccountInfo:
    margin_mode: str
    unified_margin_status: int
    updated_time_ms: int
    environment: str = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        if self.margin_mode not in _ALLOWED_MARGIN_MODES:
            raise ValueError("Bybit mainnet account returned unsupported margin mode")
        if self.unified_margin_status <= 0:
            raise ValueError("Bybit mainnet unified margin status must be positive")
        if self.updated_time_ms < 0:
            raise ValueError("Bybit mainnet account updated time cannot be negative")
        if self.live_mainnet_order_routing_allowed:
            raise ValueError("read-only account snapshot cannot grant live routing")


@dataclass(frozen=True)
class BybitMainnetPosition:
    symbol: str
    side: str
    size: Decimal
    position_idx: int
    average_price: Decimal | None
    mark_price: Decimal | None
    position_value: Decimal | None
    unrealised_pnl: Decimal | None
    liquidation_price: Decimal | None
    leverage: Decimal | None
    environment: str = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit mainnet position side must be Buy or Sell")
        if not self.size.is_finite() or self.size <= 0:
            raise ValueError("Bybit mainnet open position size must be positive and finite")
        if self.position_idx not in {0, 1, 2}:
            raise ValueError("Bybit mainnet positionIdx is invalid")
        for name, value in (
            ("average_price", self.average_price),
            ("mark_price", self.mark_price),
            ("position_value", self.position_value),
            ("unrealised_pnl", self.unrealised_pnl),
            ("liquidation_price", self.liquidation_price),
            ("leverage", self.leverage),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"Bybit mainnet position {name} must be finite")
        if self.live_mainnet_order_routing_allowed:
            raise ValueError("read-only position snapshot cannot grant live routing")


class BybitMainnetReadOnlyHttpsTransport:
    """Allowlisted mainnet HTTPS transport with no mutation method."""

    environment = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(
        self,
        *,
        host: str = _DEFAULT_MAINNET_HOST,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.host = validate_bybit_mainnet_readonly_host(host)
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Bybit mainnet read timeout must be within (0, 60] seconds")
        self._timeout_seconds = timeout_seconds

    def get(
        self,
        *,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
    ) -> BybitMainnetReadOnlyHttpJson:
        _validate_read_path(path)
        target = path if not query_string else f"{path}?{query_string}"
        connection = http.client.HTTPSConnection(
            self.host,
            443,
            timeout=self._timeout_seconds,
        )
        try:
            connection.request("GET", target, headers=dict(headers))
            response = connection.getresponse()
            body = response.read()
            response_headers = {key: value for key, value in response.getheaders()}
        finally:
            connection.close()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            if response.status != 200:
                payload = {}
            else:
                raise BybitRestProtocolError(
                    "Bybit mainnet read-only endpoint returned invalid JSON",
                    retryable_read=False,
                    ambiguous_mutation=False,
                    http_status=response.status,
                ) from exc
        if not isinstance(payload, dict):
            raise BybitRestProtocolError(
                "Bybit mainnet read-only response must be an object",
                retryable_read=False,
                ambiguous_mutation=False,
                http_status=response.status,
            )
        return BybitMainnetReadOnlyHttpJson(
            status_code=response.status,
            headers=response_headers,
            payload=payload,
        )


class BybitMainnetReadOnlyClient:
    """Authenticated Bybit mainnet reads with a hard zero-mutation surface.

    The client deliberately has no POST transport and no order-placement, cancellation,
    leverage, margin, transfer, withdrawal, or position-protection methods. API-key safety is
    proved against ``/v5/user/query-api`` before a production probe may be considered ready.
    """

    environment = "BYBIT_MAINNET_READONLY"
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        host: str = _DEFAULT_MAINNET_HOST,
        transport: BybitMainnetReadOnlyTransport | None = None,
        clock_ms: Callable[[], int] | None = None,
        recv_window_ms: int = _RECV_WINDOW_MS,
        rest_policy: BybitRestPolicy | None = None,
        sleep_fn: SleepFn = time.sleep,
    ) -> None:
        _validate_credential(api_key, name="api_key")
        _validate_credential(api_secret, name="api_secret")
        self.host = validate_bybit_mainnet_readonly_host(host)
        if not 1000 <= recv_window_ms <= 10_000:
            raise ValueError("Bybit mainnet recv window must be within [1000, 10000]")
        active_policy = BybitRestPolicy() if rest_policy is None else rest_policy
        active_policy.validate()
        self._api_key = api_key
        self._api_secret = api_secret
        self._transport = (
            BybitMainnetReadOnlyHttpsTransport(
                host=self.host,
                timeout_seconds=active_policy.request_timeout_seconds,
            )
            if transport is None
            else transport
        )
        self._clock_ms = (
            (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms
        )
        self._recv_window_ms = recv_window_ms
        self._rest_policy = active_policy
        self._sleep_fn = sleep_fn

    @property
    def api_key_fingerprint_sha256(self) -> str:
        return hashlib.sha256(self._api_key.encode("utf-8")).hexdigest()

    def verify_read_only_api_key(
        self,
        *,
        require_ip_binding: bool = True,
    ) -> BybitMainnetApiKeyInfo:
        result = self._private_get_result(path="/v5/user/query-api", query={})
        raw_read_only = result.get("readOnly")
        if raw_read_only is None or isinstance(raw_read_only, bool):
            raise BybitRestProtocolError(
                "Bybit API-key information has invalid readOnly flag",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        try:
            read_only_value = int(str(raw_read_only))
        except (TypeError, ValueError) as exc:
            raise BybitRestProtocolError(
                "Bybit API-key information has invalid readOnly flag",
                retryable_read=False,
                ambiguous_mutation=False,
            ) from exc
        if read_only_value != 1:
            raise BybitMainnetReadOnlyError(
                "Bybit mainnet key must be created as read-only; read/write keys are rejected"
            )
        returned_key = result.get("apiKey")
        if isinstance(returned_key, str) and returned_key:
            if not hmac.compare_digest(returned_key, self._api_key):
                raise BybitMainnetReadOnlyError(
                    "Bybit API-key identity does not match configured credential"
                )
        secret_marker = result.get("secret")
        if secret_marker is not None and (
            not isinstance(secret_marker, str) or len(secret_marker) != 0
        ):
            raise BybitRestProtocolError(
                "Bybit API-key information unexpectedly exposed secret material",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        raw_ips = result.get("ips", [])
        if not isinstance(raw_ips, list) or any(not isinstance(value, str) for value in raw_ips):
            raise BybitRestProtocolError(
                "Bybit API-key information has invalid IP binding list",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        ip_bindings = tuple(value.strip() for value in raw_ips if value.strip())
        if require_ip_binding and not ip_bindings:
            raise BybitMainnetReadOnlyError(
                "Bybit mainnet read-only key must be bound to at least one server IP"
            )
        raw_type = result.get("type")
        key_type = None if raw_type is None or raw_type == "" else _required_int(result, "type")
        raw_note = result.get("note")
        note = raw_note if isinstance(raw_note, str) and raw_note else None
        permissions = _flatten_permissions(result.get("permissions"))
        info = BybitMainnetApiKeyInfo(
            key_fingerprint_sha256=self.api_key_fingerprint_sha256,
            read_only=True,
            ip_bindings=ip_bindings,
            key_type=key_type,
            note=note,
            permissions=permissions,
        )
        info.validate()
        return info

    def get_wallet_balance(self) -> BybitMainnetWalletBalance:
        result = self._private_get_result(
            path="/v5/account/wallet-balance",
            query={"accountType": "UNIFIED"},
        )
        rows = _result_rows(result, context="wallet balance")
        if len(rows) != 1:
            raise RuntimeError("Bybit mainnet wallet balance must return exactly one account row")
        row = rows[0]
        if row.get("accountType") != "UNIFIED":
            raise RuntimeError("Bybit mainnet wallet balance returned a non-UNIFIED account")
        balance = BybitMainnetWalletBalance(
            total_equity_usd=_required_decimal(row, "totalEquity"),
            total_wallet_balance_usd=_required_decimal(row, "totalWalletBalance"),
            total_margin_balance_usd=_required_decimal(row, "totalMarginBalance"),
            total_available_balance_usd=_required_decimal(row, "totalAvailableBalance"),
            total_perp_upl_usd=_required_decimal(row, "totalPerpUPL"),
            total_initial_margin_usd=_required_decimal(row, "totalInitialMargin"),
            total_maintenance_margin_usd=_required_decimal(row, "totalMaintenanceMargin"),
            usdt_wallet_balance=_optional_coin_wallet_decimal(row, coin="USDT"),
        )
        balance.validate()
        return balance

    def get_account_info(self) -> BybitMainnetAccountInfo:
        result = self._private_get_result(path="/v5/account/info", query={})
        raw_margin_mode = result.get("marginMode")
        if not isinstance(raw_margin_mode, str):
            raise ValueError("Bybit mainnet account info is missing marginMode")
        info = BybitMainnetAccountInfo(
            margin_mode=raw_margin_mode,
            unified_margin_status=_required_int(result, "unifiedMarginStatus"),
            updated_time_ms=_required_int(result, "updatedTime"),
        )
        info.validate()
        return info

    def get_positions(
        self,
        *,
        settle_coin: str = "USDT",
        limit: int = 200,
        max_pages: int = 10,
    ) -> tuple[BybitMainnetPosition, ...]:
        if settle_coin != settle_coin.strip().upper() or not settle_coin.isalnum():
            raise ValueError("Bybit mainnet settle coin must be normalized uppercase text")
        rows = self._paginate(
            path="/v5/position/list",
            base_query={
                "category": "linear",
                "settleCoin": settle_coin,
                "limit": str(_validate_limit(limit, maximum=200)),
            },
            max_pages=max_pages,
        )
        positions: list[BybitMainnetPosition] = []
        for row in rows:
            size = _required_decimal(row, "size")
            if size == 0:
                continue
            raw_symbol = row.get("symbol")
            raw_side = row.get("side")
            if not isinstance(raw_symbol, str) or not isinstance(raw_side, str):
                raise ValueError("Bybit mainnet position is missing symbol or side")
            position = BybitMainnetPosition(
                symbol=raw_symbol,
                side=raw_side,
                size=size,
                position_idx=_required_int(row, "positionIdx"),
                average_price=_optional_decimal(row, "avgPrice"),
                mark_price=_optional_decimal(row, "markPrice"),
                position_value=_optional_decimal(row, "positionValue"),
                unrealised_pnl=_optional_decimal(row, "unrealisedPnl"),
                liquidation_price=_optional_decimal(row, "liqPrice"),
                leverage=_optional_decimal(row, "leverage"),
            )
            position.validate()
            positions.append(position)
        return tuple(positions)

    def get_executions(
        self,
        *,
        symbol: str,
        limit: int = 100,
        max_pages: int = 10,
    ) -> tuple[Mapping[str, Any], ...]:
        _validate_symbol(symbol)
        return self._paginate(
            path="/v5/execution/list",
            base_query={
                "category": "linear",
                "symbol": symbol,
                "limit": str(_validate_limit(limit, maximum=100)),
            },
            max_pages=max_pages,
        )

    def get_closed_pnl(
        self,
        *,
        symbol: str,
        limit: int = 100,
        max_pages: int = 10,
    ) -> tuple[Mapping[str, Any], ...]:
        _validate_symbol(symbol)
        return self._paginate(
            path="/v5/position/closed-pnl",
            base_query={
                "category": "linear",
                "symbol": symbol,
                "limit": str(_validate_limit(limit, maximum=100)),
            },
            max_pages=max_pages,
        )

    def get_open_orders(
        self,
        *,
        symbol: str,
        limit: int = 50,
        max_pages: int = 10,
    ) -> tuple[Mapping[str, Any], ...]:
        _validate_symbol(symbol)
        return self._paginate(
            path="/v5/order/realtime",
            base_query={
                "category": "linear",
                "symbol": symbol,
                "openOnly": "0",
                "limit": str(_validate_limit(limit, maximum=50)),
            },
            max_pages=max_pages,
        )

    def _paginate(
        self,
        *,
        path: str,
        base_query: Mapping[str, str],
        max_pages: int,
    ) -> tuple[Mapping[str, Any], ...]:
        if isinstance(max_pages, bool) or not 1 <= max_pages <= 100:
            raise ValueError("Bybit mainnet max_pages must be within [1, 100]")
        rows: list[Mapping[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(max_pages):
            query = dict(base_query)
            if cursor:
                query["cursor"] = cursor
            result = self._private_get_result(path=path, query=query)
            rows.extend(_result_rows(result, context=path))
            raw_cursor = result.get("nextPageCursor")
            next_cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
            if next_cursor is None:
                return tuple(rows)
            if next_cursor in seen_cursors:
                raise RuntimeError("Bybit mainnet read-only pagination cursor loop detected")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RuntimeError("Bybit mainnet read-only pagination exceeded max_pages")

    def _private_get_result(
        self,
        *,
        path: str,
        query: Mapping[str, str],
    ) -> Mapping[str, Any]:
        _validate_read_path(path)

        def _request_once() -> Mapping[str, Any]:
            query_string = urlencode(sorted(query.items()))
            timestamp = str(self._clock_ms())
            recv_window = str(self._recv_window_ms)
            signature_payload = timestamp + self._api_key + recv_window + query_string
            signature = hmac.new(
                self._api_secret.encode("utf-8"),
                signature_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            raw_response = self._transport.get(
                path=path,
                query_string=query_string,
                headers={
                    "X-BAPI-API-KEY": self._api_key,
                    "X-BAPI-SIGN": signature,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": recv_window,
                },
            )
            response = _normalize_response(raw_response)
            raise_for_bybit_response(
                status_code=response.status_code,
                headers=response.headers,
                payload=response.payload,
                mutation=False,
            )
            result = response.payload.get("result")
            if not isinstance(result, Mapping):
                raise BybitRestProtocolError(
                    "Bybit mainnet read-only result must be an object",
                    retryable_read=False,
                    ambiguous_mutation=False,
                )
            return result

        return run_bybit_read_with_retry(
            _request_once,
            policy=self._rest_policy,
            sleep_fn=self._sleep_fn,
            clock_ms=self._clock_ms,
        )


def validate_bybit_mainnet_readonly_host(host: str) -> str:
    if not isinstance(host, str) or host not in _ALLOWED_MAINNET_HOSTS:
        raise BybitMainnetReadOnlyError(
            "Bybit mainnet host must be one of the audited regional allowlist hosts"
        )
    if host != host.strip().lower() or "/" in host or ":" in host:
        raise BybitMainnetReadOnlyError("Bybit mainnet host must be a bare normalized hostname")
    return host


def _validate_read_path(path: str) -> None:
    if not isinstance(path, str) or path not in _ALLOWED_READ_PATHS:
        raise BybitMainnetReadOnlyError("Bybit mainnet path is outside the read-only allowlist")
    if "?" in path or "#" in path:
        raise BybitMainnetReadOnlyError("Bybit mainnet path must not contain query or fragment")


def _validate_credential(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Bybit mainnet {name} is required")
    if value != value.strip() or any(character in value for character in "\r\n\t"):
        raise ValueError(f"Bybit mainnet {name} contains surrounding or control whitespace")
    if value.lower() in {"changeme", "placeholder", "your_api_key", "your_api_secret"}:
        raise ValueError(f"Bybit mainnet {name} cannot use a placeholder")


def _normalize_response(
    response: Mapping[str, Any] | BybitMainnetReadOnlyHttpJson,
) -> BybitMainnetReadOnlyHttpJson:
    if isinstance(response, BybitMainnetReadOnlyHttpJson):
        return response
    if not isinstance(response, Mapping):
        raise BybitRestProtocolError(
            "Bybit mainnet read-only transport returned invalid response type",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    return BybitMainnetReadOnlyHttpJson(status_code=200, headers={}, payload=dict(response))


def _result_rows(result: Mapping[str, Any], *, context: str) -> tuple[Mapping[str, Any], ...]:
    raw_rows = result.get("list")
    if not isinstance(raw_rows, list):
        raise BybitRestProtocolError(
            f"Bybit mainnet {context} result.list must be an array",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    rows: list[Mapping[str, Any]] = []
    for row in raw_rows:
        if not isinstance(row, Mapping):
            raise BybitRestProtocolError(
                f"Bybit mainnet {context} row must be an object",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        rows.append(dict(row))
    return tuple(rows)


def _flatten_permissions(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise BybitRestProtocolError(
            "Bybit API-key permissions must be an object",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    if any(not isinstance(category, str) for category in raw):
        raise BybitRestProtocolError(
            "Bybit API-key permission category must be text",
            retryable_read=False,
            ambiguous_mutation=False,
        )
    flattened: list[str] = []
    for category in sorted(raw):
        values = raw[category]
        if not isinstance(values, list):
            raise BybitRestProtocolError(
                "Bybit API-key permissions have invalid structure",
                retryable_read=False,
                ambiguous_mutation=False,
            )
        for value in values:
            if not isinstance(value, str):
                raise BybitRestProtocolError(
                    "Bybit API-key permission value must be text",
                    retryable_read=False,
                    ambiguous_mutation=False,
                )
            flattened.append(f"{category}:{value}")
    return tuple(flattened)


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    raw = row.get(field)
    if raw is None or raw == "":
        raise ValueError(f"Bybit mainnet response is missing {field}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Bybit mainnet response has invalid {field}") from exc
    if not value.is_finite():
        raise ValueError(f"Bybit mainnet response has non-finite {field}")
    return value


def _optional_decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    raw = row.get(field)
    if raw is None or raw == "":
        return None
    return _required_decimal(row, field)


def _optional_coin_wallet_decimal(
    row: Mapping[str, Any],
    *,
    coin: str,
) -> Decimal | None:
    raw_coins = row.get("coin")
    if raw_coins is None:
        return None
    if not isinstance(raw_coins, list):
        raise ValueError("Bybit mainnet wallet coin field must be an array")
    matches = [
        item
        for item in raw_coins
        if isinstance(item, Mapping) and item.get("coin") == coin
    ]
    if len(matches) > 1:
        raise ValueError(f"Bybit mainnet wallet returned duplicate {coin} rows")
    if not matches:
        return None
    return _required_decimal(matches[0], "walletBalance")


def _required_int(row: Mapping[str, Any], field: str) -> int:
    raw = row.get(field)
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"Bybit mainnet response is missing {field}")
    try:
        return int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit mainnet response has invalid {field}") from exc


def _validate_symbol(symbol: str) -> None:
    if not isinstance(symbol, str) or symbol != symbol.strip().upper():
        raise ValueError("Bybit mainnet symbol must be normalized uppercase text")
    if not symbol.endswith("USDT") or not symbol[:-4].isalnum():
        raise ValueError("Bybit mainnet symbol must be a normalized USDT symbol")


def _validate_limit(limit: int, *, maximum: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError(f"Bybit mainnet limit must be within [1, {maximum}]")
    return limit
