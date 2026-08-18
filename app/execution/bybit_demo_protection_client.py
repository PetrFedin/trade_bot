from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.execution.bybit_demo import BybitDemoOrderClient, BybitDemoPosition


@dataclass(frozen=True)
class BybitDemoProtectionPosition(BybitDemoPosition):
    """Demo position enriched with exchange-reported TP/SL/trailing state."""

    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None
    trailing_stop_distance: Decimal | None = None


class BybitDemoProtectionVerifiedOrderClient(BybitDemoOrderClient):
    """Order client whose position reads can prove exchange-native protection state."""

    @property
    def protection_state_read_supported(self) -> bool:
        return True

    def get_positions(
        self,
        *,
        settle_coin: str = "USDT",
    ) -> tuple[BybitDemoProtectionPosition, ...]:
        if settle_coin != settle_coin.strip().upper() or settle_coin != "USDT":
            raise ValueError("Bybit demo position query currently requires USDT")
        response = self._signed_get(  # noqa: SLF001 - internal safety adapter extends base client.
            "/v5/position/list",
            {"category": "linear", "settleCoin": settle_coin},
        )
        payload_result = response.payload.get("result")
        if not isinstance(payload_result, Mapping):
            raise ValueError("Bybit demo protection position response missing result")
        rows = payload_result.get("list")
        if not isinstance(rows, list):
            raise ValueError("Bybit demo protection position response missing list")

        positions: list[BybitDemoProtectionPosition] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("Bybit demo protection position row must be an object")
            symbol = row.get("symbol")
            side = row.get("side")
            if not isinstance(symbol, str) or not isinstance(side, str):
                raise ValueError("Bybit demo protection position row missing symbol/side")
            positions.append(
                BybitDemoProtectionPosition(
                    symbol=symbol,
                    side=side,
                    size=_required_non_negative_decimal(row, "size"),
                    average_price=_optional_finite_decimal(row, "avgPrice"),
                    unrealised_pnl=_optional_finite_decimal(row, "unrealisedPnl"),
                    liquidation_price=_optional_finite_decimal(row, "liqPrice"),
                    take_profit_price=_optional_protection_decimal(row, "takeProfit"),
                    stop_loss_price=_optional_protection_decimal(row, "stopLoss"),
                    trailing_stop_distance=_optional_protection_decimal(
                        row,
                        "trailingStop",
                    ),
                )
            )
        return tuple(positions)


def _required_non_negative_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or value == "":
        raise ValueError(f"Bybit demo protection position missing {field}")
    parsed = _decimal(value, field)
    if parsed < 0:
        raise ValueError(f"Bybit demo protection position invalid {field}")
    return parsed


def _optional_finite_decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    value = row.get(field)
    if value in (None, ""):
        return None
    return _decimal(value, field)


def _optional_protection_decimal(row: Mapping[str, Any], field: str) -> Decimal | None:
    value = row.get(field)
    if value in (None, "", 0, "0", "0.0", "0.00"):
        return None
    parsed = _decimal(value, field)
    if parsed <= 0:
        raise ValueError(f"Bybit demo protection position invalid {field}")
    return parsed


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Bybit demo protection position invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"Bybit demo protection position non-finite {field}")
    return parsed
