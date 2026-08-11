from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.trading import Fill, Side
from app.strategy.quality_monitor import (
    StrategyQualityGateDecision,
    TradeQualityMonitorPolicy,
    TradeQualityObservation,
    evaluate_strategy_quality_gate,
)

UNATTRIBUTED_EXIT_REASON = "UNATTRIBUTED_EXIT"


@dataclass(frozen=True)
class OpenPaperTradeQuality:
    strategy_id: str
    symbol: str
    episode_id: str
    opened_at: datetime
    updated_at: datetime
    purchased_quantity: Decimal
    sold_quantity: Decimal
    open_quantity: Decimal
    entry_cash_out: Decimal
    exit_cash_in: Decimal
    peak_reference_price: Decimal
    trough_reference_price: Decimal
    last_observed_at: datetime
    exit_intent_id: str | None = None
    exit_reason: str | None = None

    def validate(self) -> None:
        if not self.strategy_id.strip() or not self.episode_id.strip():
            raise ValueError("paper trade quality identity is required")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        _aware(self.opened_at, "opened_at")
        _aware(self.updated_at, "updated_at")
        _aware(self.last_observed_at, "last_observed_at")
        if self.opened_at > self.updated_at:
            raise ValueError("paper trade opened_at cannot exceed updated_at")
        if self.last_observed_at > self.updated_at:
            raise ValueError("last observation cannot exceed state update")
        for name, value in (
            ("purchased_quantity", self.purchased_quantity),
            ("sold_quantity", self.sold_quantity),
            ("open_quantity", self.open_quantity),
            ("entry_cash_out", self.entry_cash_out),
            ("exit_cash_in", self.exit_cash_in),
            ("peak_reference_price", self.peak_reference_price),
            ("trough_reference_price", self.trough_reference_price),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.purchased_quantity <= 0 or self.entry_cash_out <= 0:
            raise ValueError("open paper trade requires positive entry economics")
        if self.open_quantity <= 0:
            raise ValueError("open paper trade requires positive open quantity")
        if self.sold_quantity + self.open_quantity != self.purchased_quantity:
            raise ValueError("paper trade quantities do not reconcile")
        if self.peak_reference_price <= 0 or self.trough_reference_price <= 0:
            raise ValueError("paper trade reference prices must be positive")
        if self.peak_reference_price < self.trough_reference_price:
            raise ValueError("paper trade peak cannot be below trough")
        if (self.exit_intent_id is None) != (self.exit_reason is None):
            raise ValueError("exit intent and exit reason must be supplied together")
        if self.exit_intent_id is not None and not self.exit_intent_id.strip():
            raise ValueError("exit_intent_id cannot be empty")
        if self.exit_reason is not None and not self.exit_reason.strip():
            raise ValueError("exit_reason cannot be empty")


@dataclass(frozen=True)
class ClosedPaperTradeQuality:
    trade_id: int
    strategy_id: str
    symbol: str
    episode_id: str
    opened_at: datetime
    closed_at: datetime
    quantity: Decimal
    average_entry_cost: Decimal
    average_exit_proceeds: Decimal
    net_pnl: Decimal
    return_fraction: Decimal
    maximum_favorable_excursion_fraction: Decimal
    maximum_adverse_excursion_fraction: Decimal
    mfe_capture_ratio: Decimal | None
    mfe_giveback_fraction: Decimal | None
    exit_intent_id: str
    exit_reason: str

    def as_observation(self) -> TradeQualityObservation:
        return TradeQualityObservation(
            net_pnl=self.net_pnl,
            maximum_favorable_excursion_fraction=(
                self.maximum_favorable_excursion_fraction
            ),
            mfe_capture_ratio=self.mfe_capture_ratio,
            exit_reason=self.exit_reason,
        )


@dataclass(frozen=True)
class PaperTradeQualityFillResult:
    applied: bool
    open_trade: OpenPaperTradeQuality | None
    closed_trade: ClosedPaperTradeQuality | None


class SQLitePaperTradeQualityStore:
    """Atomic paper trade episode, fill journal, exit metadata and close history."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_trade_quality_open (
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    purchased_quantity TEXT NOT NULL,
                    sold_quantity TEXT NOT NULL,
                    open_quantity TEXT NOT NULL,
                    entry_cash_out TEXT NOT NULL,
                    exit_cash_in TEXT NOT NULL,
                    peak_reference_price TEXT NOT NULL,
                    trough_reference_price TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    exit_intent_id TEXT,
                    exit_reason TEXT,
                    PRIMARY KEY (strategy_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS paper_trade_quality_fills (
                    fill_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    order_intent_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_trade_quality_exit_intents (
                    intent_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exit_reason TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_trade_quality_closed (
                    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    episode_id TEXT NOT NULL UNIQUE,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    average_entry_cost TEXT NOT NULL,
                    average_exit_proceeds TEXT NOT NULL,
                    net_pnl TEXT NOT NULL,
                    return_fraction TEXT NOT NULL,
                    maximum_favorable_excursion_fraction TEXT NOT NULL,
                    maximum_adverse_excursion_fraction TEXT NOT NULL,
                    mfe_capture_ratio TEXT,
                    mfe_giveback_fraction TEXT,
                    exit_intent_id TEXT NOT NULL,
                    exit_reason TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()

    def open_trade(
        self,
        *,
        strategy_id: str,
        symbol: str,
    ) -> OpenPaperTradeQuality | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM paper_trade_quality_open
                WHERE strategy_id=? AND symbol=?""",
                (strategy_id, symbol),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._open_row(row)

    def register_exit_intent(
        self,
        *,
        intent_id: str,
        strategy_id: str,
        symbol: str,
        exit_reason: str,
        registered_at: datetime,
    ) -> None:
        if not intent_id.strip() or not strategy_id.strip() or not exit_reason.strip():
            raise ValueError("exit intent identity and reason are required")
        if not symbol or symbol != symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        moment = _aware(registered_at, "registered_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM paper_trade_quality_exit_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            expected = (strategy_id, symbol, exit_reason, moment.isoformat())
            if existing is not None:
                actual = (
                    str(existing["strategy_id"]),
                    str(existing["symbol"]),
                    str(existing["exit_reason"]),
                    str(existing["registered_at"]),
                )
                if actual != expected:
                    raise ValueError("PAPER_TRADE_EXIT_INTENT_CONFLICT")
                connection.execute("COMMIT")
                return
            connection.execute(
                """INSERT INTO paper_trade_quality_exit_intents
                (intent_id, strategy_id, symbol, exit_reason, registered_at)
                VALUES (?, ?, ?, ?, ?)""",
                (intent_id, strategy_id, symbol, exit_reason, moment.isoformat()),
            )
            connection.execute(
                """UPDATE paper_trade_quality_closed
                SET exit_reason=?
                WHERE exit_intent_id=? AND strategy_id=? AND symbol=?
                  AND exit_reason=?""",
                (
                    exit_reason,
                    intent_id,
                    strategy_id,
                    symbol,
                    UNATTRIBUTED_EXIT_REASON,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def apply_fill(
        self,
        *,
        strategy_id: str,
        fill: Fill,
    ) -> PaperTradeQualityFillResult:
        fill.validate()
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT * FROM paper_trade_quality_fills WHERE fill_id=?",
                (fill.fill_id,),
            ).fetchone()
            if duplicate is not None:
                self._validate_duplicate_fill(
                    duplicate,
                    strategy_id=strategy_id,
                    fill=fill,
                )
                current = self._select_open(
                    connection,
                    strategy_id=strategy_id,
                    symbol=fill.symbol,
                )
                connection.execute("COMMIT")
                return PaperTradeQualityFillResult(
                    applied=False,
                    open_trade=current,
                    closed_trade=None,
                )

            connection.execute(
                """INSERT INTO paper_trade_quality_fills (
                    fill_id, strategy_id, symbol, side, quantity, price, fee,
                    order_intent_id, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill.fill_id,
                    strategy_id,
                    fill.symbol,
                    fill.side.value,
                    str(fill.quantity),
                    str(fill.price),
                    str(fill.fee),
                    fill.order_intent_id,
                    fill.occurred_at.astimezone(UTC).isoformat(),
                ),
            )
            current = self._select_open(
                connection,
                strategy_id=strategy_id,
                symbol=fill.symbol,
            )
            if fill.side is Side.BUY:
                updated = self._apply_buy(
                    connection,
                    strategy_id=strategy_id,
                    fill=fill,
                    current=current,
                )
                connection.execute("COMMIT")
                return PaperTradeQualityFillResult(
                    applied=True,
                    open_trade=updated,
                    closed_trade=None,
                )

            if current is None:
                raise ValueError("PAPER_TRADE_QUALITY_SELL_WITHOUT_OPEN_TRADE")
            updated, closed = self._apply_sell(
                connection,
                strategy_id=strategy_id,
                fill=fill,
                current=current,
            )
            connection.execute("COMMIT")
            return PaperTradeQualityFillResult(
                applied=True,
                open_trade=updated,
                closed_trade=closed,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def observe_price(
        self,
        *,
        strategy_id: str,
        symbol: str,
        reference_price: Decimal,
        observed_at: datetime,
    ) -> OpenPaperTradeQuality | None:
        if not reference_price.is_finite() or reference_price <= 0:
            raise ValueError("reference_price must be positive and finite")
        moment = _aware(observed_at, "observed_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._select_open(
                connection,
                strategy_id=strategy_id,
                symbol=symbol,
            )
            if current is None:
                connection.execute("COMMIT")
                return None
            if moment < current.last_observed_at.astimezone(UTC):
                raise ValueError("stale paper trade quality price observation")
            if moment == current.last_observed_at.astimezone(UTC):
                connection.execute("COMMIT")
                return current
            updated = OpenPaperTradeQuality(
                strategy_id=current.strategy_id,
                symbol=current.symbol,
                episode_id=current.episode_id,
                opened_at=current.opened_at,
                updated_at=moment,
                purchased_quantity=current.purchased_quantity,
                sold_quantity=current.sold_quantity,
                open_quantity=current.open_quantity,
                entry_cash_out=current.entry_cash_out,
                exit_cash_in=current.exit_cash_in,
                peak_reference_price=max(
                    current.peak_reference_price,
                    reference_price,
                ),
                trough_reference_price=min(
                    current.trough_reference_price,
                    reference_price,
                ),
                last_observed_at=moment,
                exit_intent_id=current.exit_intent_id,
                exit_reason=current.exit_reason,
            )
            updated.validate()
            self._write_open(connection, updated)
            connection.execute("COMMIT")
            return updated
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def closed_trades(
        self,
        *,
        strategy_id: str,
        limit: int | None = None,
    ) -> tuple[ClosedPaperTradeQuality, ...]:
        if limit is not None and limit < 1:
            raise ValueError("closed trade limit must be positive")
        query = (
            "SELECT * FROM paper_trade_quality_closed "
            "WHERE strategy_id=? ORDER BY trade_id"
        )
        params: tuple[object, ...] = (strategy_id,)
        if limit is not None:
            query = (
                "SELECT * FROM (SELECT * FROM paper_trade_quality_closed "
                "WHERE strategy_id=? ORDER BY trade_id DESC LIMIT ?) "
                "ORDER BY trade_id"
            )
            params = (strategy_id, limit)
        connection = self._connect()
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return tuple(self._closed_row(row) for row in rows)

    def observations(
        self,
        *,
        strategy_id: str,
        limit: int | None = None,
    ) -> tuple[TradeQualityObservation, ...]:
        return tuple(
            trade.as_observation()
            for trade in self.closed_trades(strategy_id=strategy_id, limit=limit)
        )

    def _apply_buy(
        self,
        connection: sqlite3.Connection,
        *,
        strategy_id: str,
        fill: Fill,
        current: OpenPaperTradeQuality | None,
    ) -> OpenPaperTradeQuality:
        cash_out = fill.quantity * fill.price + fill.fee
        moment = fill.occurred_at.astimezone(UTC)
        if current is None:
            updated = OpenPaperTradeQuality(
                strategy_id=strategy_id,
                symbol=fill.symbol,
                episode_id=fill.fill_id,
                opened_at=moment,
                updated_at=moment,
                purchased_quantity=fill.quantity,
                sold_quantity=Decimal("0"),
                open_quantity=fill.quantity,
                entry_cash_out=cash_out,
                exit_cash_in=Decimal("0"),
                peak_reference_price=fill.price,
                trough_reference_price=fill.price,
                last_observed_at=moment,
            )
        else:
            if current.sold_quantity > 0:
                raise ValueError("PAPER_TRADE_QUALITY_SCALE_IN_AFTER_EXIT_NOT_SUPPORTED")
            updated = OpenPaperTradeQuality(
                strategy_id=current.strategy_id,
                symbol=current.symbol,
                episode_id=current.episode_id,
                opened_at=current.opened_at,
                updated_at=moment,
                purchased_quantity=current.purchased_quantity + fill.quantity,
                sold_quantity=current.sold_quantity,
                open_quantity=current.open_quantity + fill.quantity,
                entry_cash_out=current.entry_cash_out + cash_out,
                exit_cash_in=current.exit_cash_in,
                peak_reference_price=max(current.peak_reference_price, fill.price),
                trough_reference_price=min(current.trough_reference_price, fill.price),
                last_observed_at=max(current.last_observed_at, moment),
            )
        updated.validate()
        self._write_open(connection, updated)
        return updated

    def _apply_sell(
        self,
        connection: sqlite3.Connection,
        *,
        strategy_id: str,
        fill: Fill,
        current: OpenPaperTradeQuality,
    ) -> tuple[OpenPaperTradeQuality | None, ClosedPaperTradeQuality | None]:
        if fill.quantity > current.open_quantity:
            raise ValueError("PAPER_TRADE_QUALITY_EXIT_EXCEEDS_OPEN_QUANTITY")
        reason = self._exit_reason(
            connection,
            strategy_id=strategy_id,
            fill=fill,
        )
        if current.exit_intent_id is not None and (
            current.exit_intent_id != fill.order_intent_id
            or current.exit_reason != reason
        ):
            raise ValueError("PAPER_TRADE_QUALITY_MULTIPLE_EXIT_INTENTS_NOT_SUPPORTED")
        moment = fill.occurred_at.astimezone(UTC)
        exit_cash_in = current.exit_cash_in + fill.quantity * fill.price - fill.fee
        sold_quantity = current.sold_quantity + fill.quantity
        open_quantity = current.open_quantity - fill.quantity
        peak = max(current.peak_reference_price, fill.price)
        trough = min(current.trough_reference_price, fill.price)
        if open_quantity > 0:
            updated = OpenPaperTradeQuality(
                strategy_id=current.strategy_id,
                symbol=current.symbol,
                episode_id=current.episode_id,
                opened_at=current.opened_at,
                updated_at=moment,
                purchased_quantity=current.purchased_quantity,
                sold_quantity=sold_quantity,
                open_quantity=open_quantity,
                entry_cash_out=current.entry_cash_out,
                exit_cash_in=exit_cash_in,
                peak_reference_price=peak,
                trough_reference_price=trough,
                last_observed_at=max(current.last_observed_at, moment),
                exit_intent_id=fill.order_intent_id,
                exit_reason=reason,
            )
            updated.validate()
            self._write_open(connection, updated)
            return updated, None

        closed = self._close_trade(
            connection,
            current=current,
            fill=fill,
            exit_cash_in=exit_cash_in,
            peak=peak,
            trough=trough,
            exit_reason=reason,
        )
        connection.execute(
            "DELETE FROM paper_trade_quality_open WHERE strategy_id=? AND symbol=?",
            (strategy_id, fill.symbol),
        )
        return None, closed

    def _close_trade(
        self,
        connection: sqlite3.Connection,
        *,
        current: OpenPaperTradeQuality,
        fill: Fill,
        exit_cash_in: Decimal,
        peak: Decimal,
        trough: Decimal,
        exit_reason: str,
    ) -> ClosedPaperTradeQuality:
        quantity = current.purchased_quantity
        average_entry_cost = current.entry_cash_out / quantity
        average_exit_proceeds = exit_cash_in / quantity
        net_pnl = exit_cash_in - current.entry_cash_out
        return_fraction = net_pnl / current.entry_cash_out
        mfe = max(Decimal("0"), (peak - average_entry_cost) / average_entry_cost)
        mae = max(Decimal("0"), (average_entry_cost - trough) / average_entry_cost)
        maximum_favorable_pnl = max(
            Decimal("0"),
            (peak - average_entry_cost) * quantity,
        )
        capture = (
            net_pnl / maximum_favorable_pnl
            if maximum_favorable_pnl > 0
            else None
        )
        giveback = (
            (maximum_favorable_pnl - net_pnl) / maximum_favorable_pnl
            if maximum_favorable_pnl > 0
            else None
        )
        cursor = connection.execute(
            """INSERT INTO paper_trade_quality_closed (
                strategy_id, symbol, episode_id, opened_at, closed_at, quantity,
                average_entry_cost, average_exit_proceeds, net_pnl, return_fraction,
                maximum_favorable_excursion_fraction,
                maximum_adverse_excursion_fraction, mfe_capture_ratio,
                mfe_giveback_fraction, exit_intent_id, exit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                current.strategy_id,
                current.symbol,
                current.episode_id,
                current.opened_at.astimezone(UTC).isoformat(),
                fill.occurred_at.astimezone(UTC).isoformat(),
                str(quantity),
                str(average_entry_cost),
                str(average_exit_proceeds),
                str(net_pnl),
                str(return_fraction),
                str(mfe),
                str(mae),
                None if capture is None else str(capture),
                None if giveback is None else str(giveback),
                fill.order_intent_id,
                exit_reason,
            ),
        )
        row = connection.execute(
            "SELECT * FROM paper_trade_quality_closed WHERE trade_id=?",
            (int(cursor.lastrowid),),
        ).fetchone()
        if row is None:
            raise RuntimeError("closed paper trade quality row missing after insert")
        return self._closed_row(row)

    @staticmethod
    def _exit_reason(
        connection: sqlite3.Connection,
        *,
        strategy_id: str,
        fill: Fill,
    ) -> str:
        row = connection.execute(
            """SELECT exit_reason, strategy_id, symbol
            FROM paper_trade_quality_exit_intents WHERE intent_id=?""",
            (fill.order_intent_id,),
        ).fetchone()
        if row is None:
            return UNATTRIBUTED_EXIT_REASON
        if str(row["strategy_id"]) != strategy_id or str(row["symbol"]) != fill.symbol:
            raise ValueError("PAPER_TRADE_EXIT_INTENT_IDENTITY_MISMATCH")
        return str(row["exit_reason"])

    @staticmethod
    def _validate_duplicate_fill(
        row: sqlite3.Row,
        *,
        strategy_id: str,
        fill: Fill,
    ) -> None:
        expected = (
            strategy_id,
            fill.symbol,
            fill.side.value,
            str(fill.quantity),
            str(fill.price),
            str(fill.fee),
            fill.order_intent_id,
            fill.occurred_at.astimezone(UTC).isoformat(),
        )
        actual = (
            str(row["strategy_id"]),
            str(row["symbol"]),
            str(row["side"]),
            str(row["quantity"]),
            str(row["price"]),
            str(row["fee"]),
            str(row["order_intent_id"]),
            str(row["occurred_at"]),
        )
        if actual != expected:
            raise ValueError("PAPER_TRADE_QUALITY_FILL_CONFLICT")

    @staticmethod
    def _select_open(
        connection: sqlite3.Connection,
        *,
        strategy_id: str,
        symbol: str,
    ) -> OpenPaperTradeQuality | None:
        row = connection.execute(
            """SELECT * FROM paper_trade_quality_open
            WHERE strategy_id=? AND symbol=?""",
            (strategy_id, symbol),
        ).fetchone()
        return None if row is None else SQLitePaperTradeQualityStore._open_row(row)

    @staticmethod
    def _write_open(
        connection: sqlite3.Connection,
        state: OpenPaperTradeQuality,
    ) -> None:
        connection.execute(
            """INSERT INTO paper_trade_quality_open (
                strategy_id, symbol, episode_id, opened_at, updated_at,
                purchased_quantity, sold_quantity, open_quantity, entry_cash_out,
                exit_cash_in, peak_reference_price, trough_reference_price,
                last_observed_at, exit_intent_id, exit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(strategy_id, symbol) DO UPDATE SET
                episode_id=excluded.episode_id,
                opened_at=excluded.opened_at,
                updated_at=excluded.updated_at,
                purchased_quantity=excluded.purchased_quantity,
                sold_quantity=excluded.sold_quantity,
                open_quantity=excluded.open_quantity,
                entry_cash_out=excluded.entry_cash_out,
                exit_cash_in=excluded.exit_cash_in,
                peak_reference_price=excluded.peak_reference_price,
                trough_reference_price=excluded.trough_reference_price,
                last_observed_at=excluded.last_observed_at,
                exit_intent_id=excluded.exit_intent_id,
                exit_reason=excluded.exit_reason""",
            (
                state.strategy_id,
                state.symbol,
                state.episode_id,
                state.opened_at.astimezone(UTC).isoformat(),
                state.updated_at.astimezone(UTC).isoformat(),
                str(state.purchased_quantity),
                str(state.sold_quantity),
                str(state.open_quantity),
                str(state.entry_cash_out),
                str(state.exit_cash_in),
                str(state.peak_reference_price),
                str(state.trough_reference_price),
                state.last_observed_at.astimezone(UTC).isoformat(),
                state.exit_intent_id,
                state.exit_reason,
            ),
        )

    @staticmethod
    def _open_row(row: sqlite3.Row) -> OpenPaperTradeQuality:
        state = OpenPaperTradeQuality(
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            episode_id=str(row["episode_id"]),
            opened_at=datetime.fromisoformat(str(row["opened_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            purchased_quantity=Decimal(str(row["purchased_quantity"])),
            sold_quantity=Decimal(str(row["sold_quantity"])),
            open_quantity=Decimal(str(row["open_quantity"])),
            entry_cash_out=Decimal(str(row["entry_cash_out"])),
            exit_cash_in=Decimal(str(row["exit_cash_in"])),
            peak_reference_price=Decimal(str(row["peak_reference_price"])),
            trough_reference_price=Decimal(str(row["trough_reference_price"])),
            last_observed_at=datetime.fromisoformat(str(row["last_observed_at"])),
            exit_intent_id=(
                None if row["exit_intent_id"] is None else str(row["exit_intent_id"])
            ),
            exit_reason=(
                None if row["exit_reason"] is None else str(row["exit_reason"])
            ),
        )
        state.validate()
        return state

    @staticmethod
    def _closed_row(row: sqlite3.Row) -> ClosedPaperTradeQuality:
        return ClosedPaperTradeQuality(
            trade_id=int(row["trade_id"]),
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            episode_id=str(row["episode_id"]),
            opened_at=datetime.fromisoformat(str(row["opened_at"])),
            closed_at=datetime.fromisoformat(str(row["closed_at"])),
            quantity=Decimal(str(row["quantity"])),
            average_entry_cost=Decimal(str(row["average_entry_cost"])),
            average_exit_proceeds=Decimal(str(row["average_exit_proceeds"])),
            net_pnl=Decimal(str(row["net_pnl"])),
            return_fraction=Decimal(str(row["return_fraction"])),
            maximum_favorable_excursion_fraction=Decimal(
                str(row["maximum_favorable_excursion_fraction"])
            ),
            maximum_adverse_excursion_fraction=Decimal(
                str(row["maximum_adverse_excursion_fraction"])
            ),
            mfe_capture_ratio=(
                None
                if row["mfe_capture_ratio"] is None
                else Decimal(str(row["mfe_capture_ratio"]))
            ),
            mfe_giveback_fraction=(
                None
                if row["mfe_giveback_fraction"] is None
                else Decimal(str(row["mfe_giveback_fraction"]))
            ),
            exit_intent_id=str(row["exit_intent_id"]),
            exit_reason=str(row["exit_reason"]),
        )


class PaperTradeQualityTracker:
    """Strategy-scoped exact-fill observer plus fresh-price MFE/MAE tracker."""

    def __init__(
        self,
        *,
        store: SQLitePaperTradeQualityStore,
        strategy_id: str = "cross-sectional-quality-v2-paper-shadow",
    ) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        self.store = store
        self.strategy_id = strategy_id.strip()

    def observe_fill(self, fill: Fill) -> None:
        self.store.apply_fill(strategy_id=self.strategy_id, fill=fill)

    def observe_price(
        self,
        *,
        symbol: str,
        reference_price: Decimal,
        observed_at: datetime,
    ) -> OpenPaperTradeQuality | None:
        return self.store.observe_price(
            strategy_id=self.strategy_id,
            symbol=symbol,
            reference_price=reference_price,
            observed_at=observed_at,
        )

    def register_exit_intent(
        self,
        *,
        intent_id: str,
        symbol: str,
        exit_reason: str,
        registered_at: datetime,
    ) -> None:
        self.store.register_exit_intent(
            intent_id=intent_id,
            strategy_id=self.strategy_id,
            symbol=symbol,
            exit_reason=exit_reason,
            registered_at=registered_at,
        )

    def quality_gate(
        self,
        *,
        policy: TradeQualityMonitorPolicy,
    ) -> StrategyQualityGateDecision:
        observations = self.store.observations(
            strategy_id=self.strategy_id,
            limit=policy.window_trades,
        )
        return evaluate_strategy_quality_gate(observations, policy=policy)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)
