from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_demo_protection_client import (
    BybitDemoProtectionVerifiedOrderClient,
)


@dataclass(frozen=True)
class BybitDemoStopRatchetRequest:
    symbol: str
    side: str
    previous_stop_loss_price: Decimal
    new_stop_loss_price: Decimal
    current_last_price: Decimal
    trigger_by: str = "LastPrice"

    def validate(self) -> None:
        if self.symbol != self.symbol.strip().upper() or not self.symbol.endswith("USDT"):
            raise ValueError("Bybit demo stop ratchet symbol must be normalized USDT")
        if self.side not in {"Buy", "Sell"}:
            raise ValueError("Bybit demo stop ratchet side must be Buy or Sell")
        for name, value in (
            ("previous_stop_loss_price", self.previous_stop_loss_price),
            ("new_stop_loss_price", self.new_stop_loss_price),
            ("current_last_price", self.current_last_price),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"Bybit demo stop ratchet {name} must be positive and finite")
        if self.trigger_by not in {"LastPrice", "MarkPrice", "IndexPrice"}:
            raise ValueError("Bybit demo stop ratchet trigger type is unsupported")
        if self.side == "Buy":
            if not (
                self.previous_stop_loss_price
                < self.new_stop_loss_price
                < self.current_last_price
            ):
                raise ValueError(
                    "long Bybit demo stop ratchet must satisfy previous < new < current"
                )
        elif not (
            self.current_last_price
            < self.new_stop_loss_price
            < self.previous_stop_loss_price
        ):
            raise ValueError(
                "short Bybit demo stop ratchet must satisfy current < new < previous"
            )


@dataclass(frozen=True)
class BybitDemoStopRatchetAck:
    symbol: str
    previous_stop_loss_price: Decimal
    stop_loss_price: Decimal
    accepted: bool = True
    environment: str = "BYBIT_DEMO"
    live_mainnet_order: bool = False


class BybitDemoStopRatchetClient(BybitDemoProtectionVerifiedOrderClient):
    """Demo-only adapter that can tighten only the full-position stop-loss field."""

    @property
    def stop_ratchet_write_supported(self) -> bool:
        return True

    def ratchet_position_stop_loss(
        self,
        request: BybitDemoStopRatchetRequest,
    ) -> BybitDemoStopRatchetAck:
        request.validate()
        self._signed_post(  # noqa: SLF001 - constrained safety adapter extends base client.
            "/v5/position/trading-stop",
            {
                "category": "linear",
                "symbol": request.symbol,
                "stopLoss": format(request.new_stop_loss_price, "f"),
                "slTriggerBy": request.trigger_by,
                "tpslMode": "Full",
                "positionIdx": 0,
            },
        )
        return BybitDemoStopRatchetAck(
            symbol=request.symbol,
            previous_stop_loss_price=request.previous_stop_loss_price,
            stop_loss_price=request.new_stop_loss_price,
        )
