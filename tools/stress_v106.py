from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from typing import Sequence

from app.runtime.deployment_qualification_v106 import ManifestReplayLedgerV106, QualificationJournalV106

UTC = timezone.utc


def run_stress(iterations: int, workers: int) -> dict[str, object]:
    if iterations <= 0 or workers <= 0:
        raise ValueError("iterations and workers must be positive")
    ledger = ManifestReplayLedgerV106()
    base = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)

    def execute(index: int) -> tuple[str, str | None]:
        try:
            observed = base + timedelta(microseconds=index)
            ledger.consume(f"manifest-{index}", f"nonce-{index}", observed)
            journal = QualificationJournalV106()
            journal.append("QUALIFICATION_CREATED", {"index": index}, observed)
            journal.append("READ_ONLY_EVIDENCE", {"index": index, "routing": False, "live": False}, observed)
            if not journal.verify():
                return "", "journal verification failed"
            return journal.tail_digest, None
        except Exception as exc:  # pragma: no cover - returned as qualification evidence
            return "", f"{type(exc).__name__}:{exc}"

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(execute, range(iterations)))
    digests = [digest for digest, error in results if not error]
    errors = [error for _, error in results if error]
    return {
        "schema": 106,
        "iterations": iterations,
        "workers": workers,
        "failures": len(errors),
        "unique_tail_digests": len(set(digests)),
        "replay_ledger_size": len(ledger),
        "sample_errors": errors[:5],
        "status": "PASS" if not errors and len(set(digests)) == iterations and len(ledger) == iterations else "FAIL",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    result = run_stress(args.iterations, args.workers)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
