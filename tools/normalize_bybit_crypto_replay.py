from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.strategy.crypto_replay_quality import normalize_crypto_replay_report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize Bybit crypto replay evidence")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = json.loads(args.input.read_text(encoding="utf-8"))
    normalized = normalize_crypto_replay_report(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "BYBIT_REPLAY_NORMALIZATION="
        + json.dumps(normalized["replay_quality_normalization"], sort_keys=True)
    )


if __name__ == "__main__":
    main()
