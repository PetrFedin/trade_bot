from __future__ import annotations

import pytest

import app.runtime.bybit_product_cli as legacy_cli


def test_legacy_main_delegates_to_canonical_run_and_preserves_exit_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _canonical(argv):
        calls.append(list(argv))
        return 22

    monkeypatch.setattr(legacy_cli, "_canonical_main", _canonical)

    assert legacy_cli.main() == 2
    assert calls == [["run"]]


def test_legacy_main_preserves_success_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy_cli, "_canonical_main", lambda _argv: 0)

    assert legacy_cli.main() == 0


def test_legacy_bootstrap_delegates_without_second_bootstrap_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _canonical(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(legacy_cli, "_canonical_main", _canonical)

    assert legacy_cli.bootstrap_session_main() == 0
    assert calls == [["bootstrap-session"]]
