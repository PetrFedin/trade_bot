from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

_BYBIT_DEMO_HOST = "api-demo.bybit.com"
_BYBIT_DEMO_BASE_URL = f"https://{_BYBIT_DEMO_HOST}"
_ALLOWED_PATHS = {
    "/v5/account/fee-rate",
    "/v5/order/create",
    "/v5/order/cancel",
    "/v5/order/realtime",
    "/v5/order/history",
    "/v5/execution/list",
    "/v5/position/list",
    "/v5/position/trading-stop",
}


@dataclass(frozen=True)
class BybitDemoHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class BybitDemoOrderRequest:
    symbol: str
    side: str
    quantity: Decimal
    order_link_id: str
    reduce_only: bool = False

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit demo order side must be Buy or Sell")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("Bybit demo order quantity must be positive and finite")
        if not self.order_link_id.startswith("ASTRA-DEMO-"):
            raise ValueError("Bybit demo orderLinkId must use ASTRA-DEMO- namespace")
        if len(self.order_link_id) > 36:
            raise ValueError("Bybit demo orderLinkId exceeds 36 characters")


@dataclass(frozen=True)
class BybitDemoProtectionRequest:
    symbol: str
    side: str
    average_entry_price: Decimal
    take_profit_price: Decimal
    stop_loss_price: Decimal
    trigger_by: str = "LastPrice"

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit demo protection side must be Buy or Sell")
        for name, value in (
            ("average_entry_price", self.average_entry_price),
            ("take_profit_price", self.take_profit_price),
            ("stop_loss_price", self.stop_loss_price),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"Bybit demo protection {name} must be positive and finite")
        if self.side == "Buy":
            if not self.stop_loss_price < self.average_entry_price < self.take_profit_price:
                raise ValueError("long Bybit demo TP/SL must bracket entry price")
        elif not self.take_profit_price < self.average_entry_price < self.stop_loss_price:
            raise ValueError("short Bybit demo TP/SL must bracket entry price")
        _validate_trigger_by(self.trigger_by)


@dataclass(frozen=True)
class BybitDemoRunnerProtectionRequest:
    """Exchange-native hard stop + delayed trailing stop with no fixed take profit."""

    symbol: str
    side: str
    average_entry_price: Decimal
    stop_loss_price: Decimal
    trailing_stop_distance: Decimal
    trailing_active_price: Decimal
    trigger_by: str = "LastPrice"

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit demo runner side must be Buy or Sell")
        for name, value in (
            ("average_entry_price", self.average_entry_price),
            ("stop_loss_price", self.stop_loss_price),
            ("trailing_stop_distance", self.trailing_stop_distance),
            ("trailing_active_price", self.trailing_active_price),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"Bybit demo runner {name} must be positive and finite")
        if self.side == "Buy":
            if not self.stop_loss_price < self.average_entry_price < self.trailing_active_price:
                raise ValueError("long Bybit demo runner must have stop < entry < activation")
        elif not self.trailing_active_price < self.average_entry_price < self.stop_loss_price:
            raise ValueError("short Bybit demo runner must have activation < entry < stop")
        _validate_trigger_by(self.trigger_by)


@dataclass(frozen=True)
class BybitDemoOrderAck:
    order_id: str
    order_link_id: str
    accepted: bool
    environment: str = "BYBIT_DEMO"
    live_mainnet_order: bool = False


@dataclass(frozen=True)
class BybitDemoProtectionAck:
    symbol: str
    take_profit_price: Decimal
    stop_loss_price: Decimal
    accepted: bool = True
    environment: str = "BYBIT_DEMO"
    live_mainnet_order: bool = False


@dataclass(frozen=True)
class BybitDemoRunnerProtectionAck:
    symbol: str
    stop_loss_price: Decimal
    trailing_stop_distance: Decimal
    trailing_active_price: Decimal
    accepted: bool = True
    environment: str = "BYBIT_DEMO"
    live_mainnet_order: bool = False


@dataclass(frozen=True)
class BybitDemoPosition:
    symbol: str
    side: str
    size: Decimal
    average_price: Decimal | None
    unrealised_pnl: Decimal | None


@dataclass(frozen=True)
class BybitDemoFeeRate:
    symbol: str
    taker_fee_rate: Decimal
    maker_fee_rate: Decimal


Transport = Callable[[str, str, Mapping[str, str], str | None], BybitDemoHttpJson]
ClockMs = Callable[[], int]


class BybitDemoOrderClient:
    """Authenticated Bybit V5 client that can only reach api-demo.bybit.com.

    This intentionally has no configurable host and no mainnet order method. Live routing
    requires a separate future adapter and an explicit promotion process.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        recv_window_ms: int = 5000,
        transport: Transport | None = None,
        clock_ms: ClockMs | None = None,
    ) -> None:
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("Bybit demo API key and secret are required")
        if not 1000 <= recv_window_ms <= 10_000:
            raise ValueError("Bybit recv window must be within [1000, 10000] ms")
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self._recv_window = str(recv_window_ms)
        self._transport = _https_transport if transport is None else transport
        self._clock_ms = (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms

    @property
    def environment(self) -> str:
        return "BYBIT_DEMO"

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck:
        request.validate()
        payload = {
            "category": "linear",
            "symbol": request.symbol,
            "side": request.side,
            "orderType": "Market",
            "qty": _decimal_text(request.quantity),
            "timeInForce": "IOC",
            "reduceOnly": request.reduce_only,
            "orderLinkId": request.order_link_id,
        }
        response = self._signed_post("/v5/order/create", payload)
        result = _result(response)
        order_id = result.get("orderId")
        order_link_id = result.get("orderLinkId")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("Bybit demo order acknowledgement missing orderId")
        if order_link_id != request.order_link_id:
            raise ValueError("Bybit demo order acknowledgement orderLinkId mismatch")
        return BybitDemoOrderAck(order_id, order_link_id, True)

    def set_full_position_protection(
        self,
        request: BybitDemoProtectionRequest,
    ) -> BybitDemoProtectionAck:
        """Legacy fixed full-position TP/SL helper retained for benchmark compatibility."""

        request.validate()
        self._signed_post(
            "/v5/position/trading-stop",
            {
                "category": "linear",
                "symbol": request.symbol,
                "takeProfit": _decimal_text(request.take_profit_price),
                "stopLoss": _decimal_text(request.stop_loss_price),
                "tpTriggerBy": request.trigger_by,
                "slTriggerBy": request.trigger_by,
                "tpslMode": "Full",
                "positionIdx": 0,
            },
        )
        return BybitDemoProtectionAck(
            symbol=request.symbol,
            take_profit_price=request.take_profit_price,
            stop_loss_price=request.stop_loss_price,
        )

    def set_open_ended_position_protection(
        self,
        request: BybitDemoRunnerProtectionRequest,
    ) -> BybitDemoRunnerProtectionAck:
        """Set hard SL plus delayed trailing protection without a fixed take-profit ceiling."""

        request.validate()
        self._signed_post(
            "/v5/position/trading-stop",
            {
                "category": "linear",
                "symbol": request.symbol,
                "stopLoss": _decimal_text(request.stop_loss_price),
                "slTriggerBy": request.trigger_by,
                "trailingStop": _decimal_text(request.trailing_stop_distance),
                "activePrice": _decimal_text(request.trailing_active_price),
                "tpslMode": "Full",
                "positionIdx": 0,
            },
        )
        return BybitDemoRunnerProtectionAck(
            symbol=request.symbol,
            stop_loss_price=request.stop_loss_price,
            trailing_stop_distance=request.trailing_stop_distance,
            trailing_active_price=request.trailing_active_price,
        )

    def cancel_order(self, *, symbol: str, order_link_id: str) -> BybitDemoOrderAck:
        _validate_symbol(symbol)
        if not order_link_id.startswith("ASTRA-DEMO-"):
            raise ValueError("Bybit demo cancel requires ASTRA-DEMO- orderLinkId")
        response = self._signed_post(
            "/v5/order/cancel",
            {
                "category": "linear",
                "symbol": symbol,
                "orderLinkId": order_link_id,
            },
        )
        result = _result(response)
        order_id = result.get("orderId")
        returned_link = result.get("orderLinkId")
        if not isinstance(order_id, str) or returned_link != order_link_id:
            raise ValueError("Bybit demo cancel acknowledgement mismatch")
        return BybitDemoOrderAck(order_id, returned_link, True)

    def get_fee_rate(self, *, symbol: str) -> BybitDemoFeeRate:
        """Read account-specific demo fees so edge/risk math can use the actual tier."""

        _validate_symbol(symbol)
        response = self._signed_get(
            "/v5/account/fee-rate",
            {"category": "linear", "symbol": symbol},
        )
        rows = _result(response).get("list")
        if not isinstance(rows, list):
            raise ValueError("Bybit demo fee rate response missing list")
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping) and row.get("symbol") == symbol
        ]
        if len(matches) != 1:
            raise ValueError(f"Bybit demo fee rate response missing {symbol}")
        row = matches[0]
        return BybitDemoFeeRate(
            symbol=symbol,
            taker_fee_rate=_fee_rate_decimal(row, "takerFeeRate"),
            maker_fee_rate=_fee_rate_decimal(row, "makerFeeRate"),
        )

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]:
        if settle_coin != settle_coin.strip().upper() or settle_coin != "USDT":
            raise ValueError("Bybit demo position query currently requires USDT")
        response = self._signed_get(
            "/v5/position/list",
            {"category": "linear", "settleCoin": settle_coin},
        )
        result = _result(response)
        rows = result.get("list")
        if not isinstance(rows, list):
            raise ValueError("Bybit demo position response missing list")
        positions: list[BybitDemoPosition] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("Bybit demo position row must be an object")
            symbol = row.get("symbol")
            side = row.get("side")
            if not isinstance(symbol, str) or not isinstance(side, str):
                raise ValueError("Bybit demo position row missing symbol/side")
            positions.append(
                BybitDemoPosition(
                    symbol=symbol,
                    side=side,
                    size=_required_decimal(row, "size"),
                    average_price=_optional_decimal(row, "avgPrice"),
                    unrealised_pnl=_optional_decimal(row, "unrealisedPnl"),
                )
            )
        return tuple(positions)

    def get_executions(
        self,
        *,
        symbol: str,
        order_link_id: str | None = None,
        limit: int = 50,
    ) -> tuple[Mapping[str, Any], ...]:
        _validate_symbol(symbol)
        if not 1 <= limit <= 100:
            raise ValueError("Bybit execution limit must be within [1, 100]")
        params: dict[str, str] = {
            "category": "linear",
            "symbol": symbol,
            "limit": str(limit),
        }
        if order_link_id is not None:
            if not order_link_id.startswith("ASTRA-DEMO-"):
                raise ValueError("Bybit demo execution query requires ASTRA-DEMO- orderLinkId")
            params["orderLinkId"] = order_link_id
        response = self._signed_get("/v5/execution/list", params)
        rows = _result(response).get("list")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise ValueError("Bybit demo execution response missing list")
        return tuple(rows)

    def _signed_get(self, path: str, params: Mapping[str, str]) -> BybitDemoHttpJson:
        query = urlencode(params)
        timestamp = str(self._clock_ms())
        signature = self._signature(timestamp, query)
        headers = self._headers(timestamp, signature)
        url = f"{_BYBIT_DEMO_BASE_URL}{path}?{query}"
        response = self._transport("GET", url, headers, None)
        return _validate_response(response)

    def _signed_post(self, path: str, payload: Mapping[str, Any]) -> BybitDemoHttpJson:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        timestamp = str(self._clock_ms())
        signature = self._signature(timestamp, body)
        headers = self._headers(timestamp, signature) | {"Content-Type": "application/json"}
        url = f"{_BYBIT_DEMO_BASE_URL}{path}"
        response = self._transport("POST", url, headers, body)
        return _validate_response(response)

    def _signature(self, timestamp: str, query_or_body: str) -> str:
        plain = timestamp + self._api_key + self._recv_window + query_or_body
        return hmac.new(
            self._api_secret.encode("utf-8"),
            plain.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, timestamp: str, signature: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self._recv_window,
            "X-BAPI-SIGN": signature,
        }


def _validate_response(response: BybitDemoHttpJson) -> BybitDemoHttpJson:
    if response.status_code != 200:
        raise ValueError(f"Bybit demo HTTP request failed:{response.status_code}")
    if response.payload.get("retCode") != 0:
        raise ValueError(f"Bybit demo API error:{response.payload.get('retMsg')}")
    return response


def _result(response: BybitDemoHttpJson) -> Mapping[str, Any]:
    result = response.payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("Bybit demo response missing result")
    return result


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit symbol must be normalized USDT linear symbol")


def _validate_trigger_by(trigger_by: str) -> None:
    if trigger_by not in {"LastPrice", "MarkPrice", "IndexPrice"}:
        raise ValueError("unsupported Bybit demo protection trigger price")


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None:
        raise ValueError(f"Bybit demo response missing {field}")
    try:
        return Decimal(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit demo response has invalid {field}") from exc


def _optional_decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit demo response has invalid {field}") from exc


def _fee_rate_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = _required_decimal(row, field)
    if not value.is_finite() or value < 0 or value >= 1:
        raise ValueError(f"Bybit demo {field} must be finite and within [0, 1)")
    return value


def _https_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: str | None,
) -> BybitDemoHttpJson:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != _BYBIT_DEMO_HOST:
        raise ValueError("Bybit demo transport rejected non-demo endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Bybit demo transport rejected ambiguous URL authority")
    if parsed.port not in (None, 443):
        raise ValueError("Bybit demo transport requires HTTPS port 443")
    if parsed.path not in _ALLOWED_PATHS:
        raise ValueError("Bybit demo transport rejected non-allowlisted path")
    if method not in {"GET", "POST"}:
        raise ValueError("Bybit demo transport rejected unsupported HTTP method")
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = HTTPSConnection(_BYBIT_DEMO_HOST, 443, timeout=30)
    try:
        connection.request(method, target, body=body, headers=dict(headers))
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Bybit demo response must be a JSON object")
        return BybitDemoHttpJson(
            status_code=response.status,
            headers={key: value for key, value in response.getheaders()},
            payload=payload,
        )
    finally:
        connection.close()