from __future__ import annotations

import json
import signal
from threading import Event

from app.application.bybit_product_composition import (
    bootstrap_bybit_product_session,
    build_bybit_product_composition,
)
from app.runtime.bybit_product_config import BybitProductConfig
from app.runtime.bybit_product_service import BybitProductServiceStatus


def main() -> int:
    config = BybitProductConfig.from_env(require_universe=True)
    composition = build_bybit_product_composition(config)
    stop_event = Event()

    def _request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    _emit(
        {
            "event": "BYBIT_PRODUCT_STARTING",
            "config": dict(config.redacted()),
        }
    )
    result = composition.run(stop_requested=stop_event.is_set)
    _emit(
        {
            "event": "BYBIT_PRODUCT_STOPPED",
            "status": result.status.value,
            "reasons": result.reasons,
            "completed_cycles": result.completed_cycles,
            "graceful_stop": result.graceful_stop,
            "live_mainnet_order_routing_allowed": result.live_mainnet_order_routing_allowed,
        }
    )
    return 0 if result.status is BybitProductServiceStatus.STOPPED else 2


def bootstrap_session_main() -> int:
    config = BybitProductConfig.from_env(require_universe=False)
    opening_equity = bootstrap_bybit_product_session(config)
    _emit(
        {
            "event": "BYBIT_SESSION_RISK_BOOTSTRAPPED",
            "opening_equity_usdt": str(opening_equity),
            "environment": config.environment,
            "broker": config.broker,
            "live_mainnet_order_routing_allowed": False,
        }
    )
    return 0


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


if __name__ == "__main__":
    raise SystemExit(main())
