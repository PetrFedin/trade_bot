"""Read instrument rules from Bybit's public endpoint and print them.

The parsing and the specification model live under app/marketdata and app/domain and do
not open sockets: transport is supplied by the caller, the way the rest of the
application already takes its transports. This is that caller, and it lives in tools/
because reaching the network is an operational concern rather than a domain one.

Public market endpoint, no credentials, read-only. Nothing here can place or amend an
order.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.marketdata.bybit_instruments import (  # noqa: E402
    ENDPOINT,
    BybitInstrumentError,
    load_instrument,
)

ALLOWED_SCHEME = "https"
ALLOWED_HOST = "api.bybit.com"


def http_source(*, category: str, symbol: str) -> dict:
    """Fetch one instruments-info envelope, refusing anything but the public host."""
    query = urllib.parse.urlencode({"category": category, "symbol": symbol})
    url = f"{ENDPOINT}?{query}"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != ALLOWED_SCHEME or parts.hostname != ALLOWED_HOST:
        raise BybitInstrumentError(f"refusing to open {parts.scheme}://{parts.hostname}")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - checked above
            return json.load(response)
    except (urllib.error.URLError, TimeoutError) as error:
        raise BybitInstrumentError(f"instrument request failed: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--category", default="linear")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    specs = []
    for symbol in args.symbols:
        try:
            specs.append(load_instrument(http_source, symbol, category=args.category))
        except BybitInstrumentError as error:
            print(f"{symbol}: {error}", file=sys.stderr)
    if not specs:
        return 1

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "symbol": spec.symbol,
                        "status": spec.status.value,
                        "tick_size": str(spec.tick_size),
                        "quantity_step": str(spec.quantity_step),
                        "minimum_quantity": str(spec.minimum_quantity),
                        "maximum_quantity": str(spec.maximum_quantity),
                        "minimum_notional": str(spec.minimum_notional),
                        "revision": spec.revision,
                        "observed_timestamp": spec.observed_timestamp.isoformat(),
                    }
                    for spec in specs
                ],
                indent=2,
            )
        )
        return 0

    header = (
        f"{'symbol':<12}{'status':<12}{'tick':>12}"
        f"{'step':>12}{'min qty':>12}{'min notional':>14}"
    )
    print(header)
    for spec in specs:
        print(
            f"{spec.symbol:<12}{spec.status.value:<12}{str(spec.tick_size):>12}"
            f"{str(spec.quantity_step):>12}{str(spec.minimum_quantity):>12}"
            f"{str(spec.minimum_notional):>14}"
        )
        print(f"{'':12}revision {spec.revision[:32]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
