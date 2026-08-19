from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Mapping, Sequence
from threading import Event
from types import FrameType

from app.application.bybit_operator_control import (
    BybitOperatorAction,
    BybitOperatorSnapshot,
    PostgresBybitOperatorControl,
)
from app.application.bybit_product_composition import (
    bootstrap_bybit_product_session,
    build_bybit_product_composition,
)
from app.observability.json_events import StructuredJsonEventLogger
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
_OPERATOR_MUTATIONS = frozenset({"pause", "resume", "read-only", "kill", "clear-kill"})


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
        description="Run and operate the canonical fail-closed ASTRA Bybit product runtime.",
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
    operator = commands.add_parser(
        "operator",
        help="Inspect or change durable PostgreSQL operator safety state.",
    )
    operator_commands = operator.add_subparsers(dest="operator_command", required=True)
    operator_commands.add_parser("status", help="Show the current durable operator state.")
    history = operator_commands.add_parser("history", help="Show append-only operator actions.")
    history.add_argument("--limit", type=int, default=100)
    for name, help_text in (
        ("pause", "Block new entries while preserving active-trade safety management."),
        ("resume", "Allow new entries after explicit operator review."),
        ("read-only", "Enter read-only mode for broker/runtime investigation."),
        ("kill", "Engage the durable kill state for new risk."),
        ("clear-kill", "Clear KILLED to PAUSED; a separate resume is still required."),
    ):
        action = operator_commands.add_parser(name, help=help_text)
        action.add_argument("--actor", required=True)
        action.add_argument("--reason", required=True)
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


def _logger(config: BybitProductConfig) -> StructuredJsonEventLogger:
    return StructuredJsonEventLogger(level=config.log_level)


def _operator_control(config: BybitProductConfig) -> PostgresBybitOperatorControl:
    return PostgresBybitOperatorControl(config.database_url)


def _operator_snapshot_report(
    snapshot: BybitOperatorSnapshot,
    *,
    status: str,
) -> dict[str, object]:
    if snapshot.live_mainnet_order_routing_allowed is not False:
        raise ValueError("operator snapshot unexpectedly permits mainnet routing")
    if snapshot.active_trade_safety_management_allowed is not True:
        raise ValueError("operator snapshot disabled active-trade safety management")
    return {
        "status": status,
        "mode": snapshot.mode.value,
        "generation": snapshot.generation,
        "updated_at": snapshot.updated_at.isoformat(),
        "updated_by": snapshot.updated_by,
        "reason": snapshot.reason,
        "new_entries_allowed": snapshot.new_entries_allowed,
        "read_only_mode": snapshot.read_only_mode,
        "kill_switch_engaged": snapshot.kill_switch_engaged,
        "active_trade_safety_management_allowed": (
            snapshot.active_trade_safety_management_allowed
        ),
        "live_mainnet_order_routing_allowed": False,
    }


def _operator_action_report(action: BybitOperatorAction) -> dict[str, object]:
    return {
        "action_id": action.action_id,
        "generation": action.generation,
        "from_mode": action.from_mode.value,
        "to_mode": action.to_mode.value,
        "actor": action.actor,
        "reason": action.reason,
        "occurred_at": action.occurred_at.isoformat(),
    }


def _run_operator_command(
    args: argparse.Namespace,
    config: BybitProductConfig,
) -> int:
    control = _operator_control(config)
    if control.live_mainnet_order_routing_allowed is not False:
        raise ValueError("operator control unexpectedly permits mainnet routing")
    command = str(args.operator_command)
    if command == "status":
        _emit(_operator_snapshot_report(control.inspect(), status="OPERATOR_STATE"))
        return 0
    if command == "history":
        actions = control.history(limit=args.limit)
        _emit(
            {
                "status": "OPERATOR_HISTORY",
                "actions": [_operator_action_report(action) for action in actions],
                "live_mainnet_order_routing_allowed": False,
            }
        )
        return 0
    if command not in _OPERATOR_MUTATIONS:
        raise ValueError(f"unsupported operator command: {command}")
    operations = {
        "pause": control.pause,
        "resume": control.resume,
        "read-only": control.enter_read_only,
        "kill": control.kill,
        "clear-kill": control.clear_kill,
    }
    snapshot = operations[command](actor=args.actor, reason=args.reason)
    _emit(_operator_snapshot_report(snapshot, status="OPERATOR_UPDATED"))
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    operator_only = args.command == "operator"

    try:
        config = BybitProductConfig.from_env(
            env,
            require_credentials=not operator_only,
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

    if operator_only:
        return _run_operator_command(args, config)

    logger = _logger(config)
    if args.command == "bootstrap-session":
        logger.emit(
            "INFO",
            "BYBIT_SESSION_BOOTSTRAP_STARTING",
            fields={"config": dict(config.redacted())},
        )
        try:
            opening_equity = bootstrap_bybit_product_session(config)
        except Exception as exc:
            logger.emit(
                "CRITICAL",
                "BYBIT_SESSION_BOOTSTRAP_FAILED",
                fields={"error_type": type(exc).__name__},
            )
            raise
        logger.emit(
            "INFO",
            "BYBIT_SESSION_BOOTSTRAPPED",
            fields={"opening_equity_usdt": str(opening_equity)},
        )
        _emit(
            {
                "status": "SESSION_BOOTSTRAPPED",
                "opening_equity_usdt": str(opening_equity),
                "live_mainnet_order_routing_allowed": False,
            }
        )
        return 0

    logger.emit(
        "INFO",
        "BYBIT_PRODUCT_STARTING",
        fields={"config": dict(config.redacted())},
    )
    try:
        result = run_product(config)
    except Exception as exc:
        logger.emit(
            "CRITICAL",
            "BYBIT_PRODUCT_CRASHED",
            fields={"error_type": type(exc).__name__},
        )
        raise
    report = _service_report(result)
    logger.emit(
        "INFO" if result.status is BybitProductServiceStatus.STOPPED else "ERROR",
        "BYBIT_PRODUCT_STOPPED",
        fields=report,
    )
    _emit(report)
    return _service_exit_code(result.status)


if __name__ == "__main__":
    raise SystemExit(main())
