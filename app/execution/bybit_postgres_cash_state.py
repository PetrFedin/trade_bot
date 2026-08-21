from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.execution.bybit_demo_cash_reconciliation import BybitDemoCashBaseline

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresBybitDemoCashBaselineStore:
    """Immutable USDT cash baseline for fail-closed broker/local reconciliation."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True
    _BASELINE_KEY = "USDT"

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use Bybit cash state")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def initialize(self, baseline: BybitDemoCashBaseline) -> BybitDemoCashBaseline:
        baseline.validate()
        created_at = datetime.fromtimestamp(baseline.created_time_ms / 1000, tz=UTC)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_cash_baseline
                        (baseline_key, currency, wallet_balance_usdt,
                         cumulative_all_in_pnl_usdt, session_revision,
                         created_time_ms, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (baseline_key) DO NOTHING""",
                        (
                            self._BASELINE_KEY,
                            baseline.currency,
                            baseline.wallet_balance_usdt,
                            baseline.cumulative_all_in_pnl_usdt,
                            baseline.session_revision,
                            baseline.created_time_ms,
                            created_at,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise FileExistsError("Bybit cash baseline already exists")
        return self.load()

    def load(self) -> BybitDemoCashBaseline:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT currency, wallet_balance_usdt,
                              cumulative_all_in_pnl_usdt, session_revision,
                              created_time_ms
                    FROM astra_bybit_cash_baseline
                    WHERE baseline_key=%s""",
                    (self._BASELINE_KEY,),
                )
                row = cursor.fetchone()
        if row is None:
            raise FileNotFoundError(self._BASELINE_KEY)
        baseline = BybitDemoCashBaseline(
            currency=str(row["currency"]),
            wallet_balance_usdt=Decimal(str(row["wallet_balance_usdt"])),
            cumulative_all_in_pnl_usdt=Decimal(
                str(row["cumulative_all_in_pnl_usdt"])
            ),
            session_revision=str(row["session_revision"]),
            created_time_ms=int(row["created_time_ms"]),
        )
        baseline.validate()
        return baseline
