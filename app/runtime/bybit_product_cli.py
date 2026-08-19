from __future__ import annotations

from tools.bybit_product import main as _canonical_main


def main() -> int:
    """Compatibility entrypoint delegating to the single canonical Bybit product CLI.

    The legacy runtime entrypoint keeps its historical 0/2 process-exit contract while all
    configuration, signal handling, composition and product supervision live in tools.bybit_product.
    """

    canonical_exit = _canonical_main(["run"])
    return 0 if canonical_exit == 0 else 2


def bootstrap_session_main() -> int:
    """Compatibility entrypoint for explicit session-risk bootstrap."""

    return _canonical_main(["bootstrap-session"])


if __name__ == "__main__":
    raise SystemExit(main())
