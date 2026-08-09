from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from datetime import datetime, timedelta, timezone
from typing import Sequence

from app.runtime.signing_authority_v108 import (
    SignatureEnvelopeV108,
    SignatureReplayErrorV108,
    SignatureReplayLedgerV108,
    SigningPurposeV108,
)

UTC = timezone.utc


def _envelopes(index: int) -> tuple[SignatureEnvelopeV108, ...]:
    now = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)
    signature = base64.b64encode(b"x" * 64).decode("ascii")
    purposes = (
        SigningPurposeV108.RELEASE_APPROVAL,
        SigningPurposeV108.RISK_APPROVAL,
        SigningPurposeV108.CONTROLLER_COMMAND,
    )
    return tuple(
        SignatureEnvelopeV108(
            signature_id=f"sig-{index}-{purpose.value.lower()}",
            purpose=purpose,
            domain="astra.rollout.authorization.v108",
            payload_digest=f"{index:064x}",
            key_id=f"key-{purpose.value.lower()}",
            key_generation=1,
            keyring_generation=1,
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
            nonce=f"nonce-{index}-{purpose.value.lower()}",
            signature_b64=signature,
        )
        for purpose in purposes
    )


def run_stress(*, iterations: int = 1000, workers: int = 8) -> dict[str, object]:
    if iterations <= 0 or workers <= 0:
        raise ValueError("iterations and workers must be positive")
    ledger = SignatureReplayLedgerV108()
    failures: list[str] = []

    def consume(index: int) -> None:
        try:
            ledger.consume_many(_envelopes(index))
        except Exception as exc:  # pragma: no cover - emitted in result
            failures.append(f"{index}:{type(exc).__name__}")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(consume, range(iterations)))

    replay_rejected = False
    try:
        ledger.consume_many(_envelopes(0))
    except SignatureReplayErrorV108:
        replay_rejected = True
    result = {
        "schema": 108,
        "iterations": iterations,
        "workers": workers,
        "failures": len(failures),
        "failure_samples": failures[:10],
        "ledger_size": ledger.size,
        "expected_ledger_size": iterations * 3,
        "replay_rejected": replay_rejected,
    }
    result["status"] = (
        "PASS"
        if not failures and ledger.size == iterations * 3 and replay_rejected
        else "FAIL"
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-stress-v108")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    result = run_stress(iterations=args.iterations, workers=args.workers)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
