from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Mapping, Sequence
from threading import Event
from types import FrameType

from app.application.bybit_product_composition import (
    bootstrap_bybit_product_session,
    build_bybit_product_composition,
)
from app.runtime.bybit_product_config import BybitProductConfig, BybitProductConfigError
from app.runtime.bybit_product_service import (
    BybitProductServiceResult,
    BybitProductServiceStatus,
)

_CONFIG_ERROR_EXIT = 2
_SERVICE_EXIT_CODES = {
    BybitProductServiceStatus.STOPPED: 0,
    BybitProductServiceStatus.STARTUP_BLOCKED: 20,
    BybitProductServiceStatus.STARTUP_FAILED: 21,
    BybitProductServiceStatus.CYCLE_FAILED: 22,
}


class _StopController:
    def __init__(self) -> None:
        self._event = Event()

    def request(self, _signum: int, _frame: FrameType | None) -> None:
        self._event.set()

    def __call__(self) -> bool:
        return self._event.is_set()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astra-bybit-product",
        description="Run the canonical fail-closed ASTRA Bybit product runtime.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "run",
        help="Run the continuous Bybit product supervisor using PostgreSQL authority.",
    )
    commands.add_parser(
        "bootstrap-session",
        help="Initialize the session-risk ledger while broker and local state are flat.",
    )
    return parser


def _install_signal_handlers(stop: _StopController) -> None:
    signal.signal(signal.SIGINT, stop.request)
    signal.signal(signal.SIGTERM, stop.request)


def run_product(
    config: BybitProductConfig,
    *,
    install_signal_handlers: bool = True,
    max_cycles: int | None = None,
) -> BybitProductServiceResult:
    """Run only the canonical composition; no alternate broker/runtime path is exposed."""

    config.validate(require_universe=True)
    composition = build_bybit_product_composition(config)
    if composition.live_mainnet_order_routing_allowed is not False:
        raise ValueError("canonical Bybit product composition unexpectedly permits mainnet routing")

    stop = _StopController()
    if install_signal_handlers:
        _install_signal_handlers(stop)
    return composition.run(stop_requested=stop, max_cycles=max_cycles)


def _service_report(result: BybitProductServiceResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "reasons": list(result.reasons),
        "completed_cycles": result.completed_cycles,
        "graceful_stop": result.graceful_stop,
        "diagnostics_only": result.diagnostics_only,
        "strategy_retuning_allowed": result.strategy_retuning_allowed,
        "live_mainnet_order_routing_allowed": result.live_mainnet_order_routing_allowed,
    }


def _service_exit_code(status: BybitProductServiceStatus) -> int:
    try:
        return _SERVICE_EXIT_CODES[status]
    except KeyError as exc:
        raise ValueError(f"unsupported Bybit product service status: {status!r}") from exc


def _emit(payload: Mapping[str, object], *, error: bool = False) -> None:
    print(json.dumps(dict(payload), sort_keys=True), file=sys.stderr if error else sys.stdout)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)

    try:
        config = BybitProductConfig.from_env(
            env,
            require_universe=args.command == "run",
        )
    except BybitProductConfigError as exc:
        _emit(
            {
                "status": "CONFIG_REJECTED",
                "error_type": type(exc).__name__,
                "reason": str(exc),
                "live_mainnet_order_routing_allowed": False,
            },
            error=True,
        )
        return _CONFIG_ERROR_EXIT

    if args.command == "bootstrap-session":
        opening_equity = bootstrap_bybit_product_session(config)
        _emit(
            {
                "status": "SESSION_BOOTSTRAPPED",
                "opening_equity_usdt": str(opening_equity),
                "live_mainnet_order_routing_allowed": False,
            }
        )
        return 0

    result = run_product(config)
    _emit(_service_report(result))
    return _service_exit_code(result.status)


if __name__ == "__main__":
    raise SystemExit(main())
