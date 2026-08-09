from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from app.runtime.alpaca_external_probe_v101 import (
    AlpacaPaperGateway,
    Credentials,
    ReadOnlyExternalProbe,
    UrllibTransport,
    WebsocketsConnector,
)

UTC = timezone.utc


def main() -> int:
    credentials = Credentials.from_environment()
    generation = int(os.environ.get("ASTRA_EXTERNAL_PROBE_GENERATION", "1"))
    if generation <= 0:
        raise ValueError("ASTRA_EXTERNAL_PROBE_GENERATION must be positive")
    gateway = AlpacaPaperGateway(
        credentials=credentials,
        transport=UrllibTransport(),
        writes_enabled=False,
    )
    probe = ReadOnlyExternalProbe(
        credentials=credentials,
        gateway=gateway,
        connector=WebsocketsConnector(),
        generation=generation,
        timeout_seconds=10.0,
    )
    account, orders, stream = probe.run(now=datetime.now(UTC))
    report = {
        "provider": "alpaca",
        "environment": "paper",
        "account_status": account.status,
        "account_currency": account.currency,
        "trading_blocked": account.trading_blocked,
        "open_order_count": len(orders),
        "stream_authenticated": stream.authenticated,
        "stream_listening": stream.listening,
        "credential_fingerprint": stream.credential_fingerprint,
        "rest_endpoint": stream.rest_endpoint,
        "stream_endpoint": stream.stream_endpoint,
        "reasons": list(stream.reasons),
        "paper_order_writes_enabled": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
    }
    print(json.dumps(report, sort_keys=True))
    if account.status.upper() != "ACTIVE":
        return 2
    if stream.reasons or not stream.authenticated or not stream.listening:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
