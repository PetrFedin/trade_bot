from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.execution.bybit_demo import BybitDemoOrderClient


class BybitDemoBrokerTruthClient(BybitDemoOrderClient):
    """Read-capable demo broker client used by startup/recovery reconciliation.

    It deliberately reuses the existing demo-only authenticated transport. Order mutation stays
    owned by ``BybitDemoOrderClient``; this class only exposes the missing open-order truth read.
    """

    def get_open_orders(
        self,
        *,
        settle_coin: str = "USDT",
        limit: int = 50,
    ) -> tuple[Mapping[str, Any], ...]:
        if settle_coin != settle_coin.strip().upper() or settle_coin != "USDT":
            raise ValueError("Bybit demo open-order query currently requires USDT")
        if not 1 <= limit <= 50:
            raise ValueError("Bybit demo open-order limit must be within [1, 50]")
        response = self._signed_get(
            "/v5/order/realtime",
            {
                "category": "linear",
                "settleCoin": settle_coin,
                "openOnly": "0",
                "limit": str(limit),
            },
        )
        result = response.payload.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("Bybit demo open-order response missing result")
        rows = result.get("list")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise ValueError("Bybit demo open-order response missing list")
        return tuple(rows)
