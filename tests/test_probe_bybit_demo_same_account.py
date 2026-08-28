from __future__ import annotations

import json
from types import SimpleNamespace

import tools.probe_bybit_demo_same_account as cli

_GIT_SHA = "a" * 40


def _set_credentials(monkeypatch) -> None:
    monkeypatch.setenv("BYBIT_DEMO_READONLY_API_KEY", "readonly-key-secret-marker")
    monkeypatch.setenv("BYBIT_DEMO_READONLY_API_SECRET", "readonly-secret-marker")
    monkeypatch.setenv("BYBIT_DEMO_TRADING_API_KEY", "trading-key-secret-marker")
    monkeypatch.setenv("BYBIT_DEMO_TRADING_API_SECRET", "trading-secret-marker")


def test_success_artifact_contains_only_boolean_account_proof(monkeypatch, tmp_path) -> None:
    _set_credentials(monkeypatch)
    monkeypatch.setattr(cli, "BybitDemoAccountIdentityInspector", lambda **_kwargs: object())
    monkeypatch.setattr(
        cli,
        "prove_same_bybit_demo_account",
        lambda *_args: SimpleNamespace(
            passed=True,
            to_payload=lambda: {
                "schema": "BYBIT_DEMO_SAME_ACCOUNT_PREFLIGHT_V1",
                "status": "VERIFIED_SAME_ACCOUNT",
                "passed": True,
                "reasons": [],
                "same_user_id": True,
                "same_parent_uid": True,
                "same_master_scope": True,
                "authenticated_get_only": True,
                "order_write_performed": False,
                "order_writes_supported": False,
                "live_mainnet_order_routing_allowed": False,
            },
        ),
    )
    output = tmp_path / "same-account.json"

    code = cli.main(["--git-sha", _GIT_SHA, "--output", str(output)])

    assert code == 0
    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["status"] == "VERIFIED_SAME_ACCOUNT"
    assert payload["git_sha"] == _GIT_SHA
    assert payload["same_user_id"] is True
    for marker in (
        "readonly-key-secret-marker",
        "readonly-secret-marker",
        "trading-key-secret-marker",
        "trading-secret-marker",
    ):
        assert marker not in text


def test_failure_artifact_never_serializes_exception_text(monkeypatch, tmp_path) -> None:
    _set_credentials(monkeypatch)
    secret_marker = "uid=123456 secret=do-not-expose"
    monkeypatch.setattr(cli, "BybitDemoAccountIdentityInspector", lambda **_kwargs: object())

    def _fail(*_args):
        raise RuntimeError(secret_marker)

    monkeypatch.setattr(cli, "prove_same_bybit_demo_account", _fail)
    output = tmp_path / "same-account.json"

    code = cli.main(["--git-sha", _GIT_SHA, "--output", str(output)])

    assert code == 2
    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["status"] == "PREFLIGHT_FAILED"
    assert payload["error_type"] == "RuntimeError"
    assert payload["passed"] is False
    assert payload["order_write_performed"] is False
    assert payload["live_mainnet_order_routing_allowed"] is False
    assert secret_marker not in text
    assert "123456" not in text
