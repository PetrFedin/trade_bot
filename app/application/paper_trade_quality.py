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
    last_exit_intent_id: str | None = None
    last_exit_reason: str | None = None

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
        if (self.last_exit_intent_id is None) != (self.last_exit_reason is None):
            raise ValueError("last exit intent and reason must be supplied together")
        if self.last_exit_intent_id is not None and not self.last_exit_intent_id.strip():
            raise ValueError("last_exit_intent_id cannot be empty")
        if self.last_exit_reason is not None and not self.last_exit_reason.strip():
            raise ValueError("last_exit_reason cannot be empty")


@dataclass(frozen=True)
class ClosedPaperTradeQuality:
    episode_id: str
    strategy_id: str
    symbol: str
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


@dataclass
class _ReplayEpisode:
    episode_id: str
    opened_at: datetime
    purchased_quantity: Decimal
    sold_quantity: Decimal
    open_quantity: Decimal
    entry_cash_out: Decimal
    exit_cash_in: Decimal
    peak_reference_price: Decimal
    trough_reference_price: Decimal
    last_observed_at: datetime
    updated_at: datetime
    last_exit_intent_id: str | None = None
    last_exit_reason: str | None = None


class SQLitePaperTradeQualityStore:
    """Event-sourced paper trade quality with deterministic derived episodes.

    Exact fills, fresh-price observations and exit-intent reasons are durable facts.
    Open and closed trade rows are derived by replaying those facts in broker timestamp
    order. This deliberately tolerates a later-delivered earlier partial fill, matching
    the recovery behavior of ``PaperTradeFillAccounting``.
    """

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
                CREATE TABLE IF NOT EXISTS paper_trade_quality_prices (
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    reference_price TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, symbol, observed_at)
                );
                CREATE TABLE IF NOT EXISTS paper_trade_quality_exit_intents (
                    intent_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    exit_reason TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );
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
                    last_exit_intent_id TEXT,
                    last_exit_reason TEXT,
                    PRIMARY KEY (strategy_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS paper_trade_quality_closed (
                    episode_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
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
            else:
                connection.execute(
                    """INSERT INTO paper_trade_quality_exit_intents
                    (intent_id, strategy_id, symbol, exit_reason, registered_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (intent_id, strategy_id, symbol, exit_reason, moment.isoformat()),
                )
            self._rebuild_symbol(
                connection,
                strategy_id=strategy_id,
                symbol=symbol,
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
            existing = connection.execute(
                "SELECT * FROM paper_trade_quality_fills WHERE fill_id=?",
                (fill.fill_id,),
            ).fetchone()
            applied = existing is None
            if existing is None:
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
            else:
                self._validate_duplicate_fill(
                    existing,
                    strategy_id=strategy_id,
                    fill=fill,
                )
            self._rebuild_symbol(
                connection,
                strategy_id=strategy_id,
                symbol=fill.symbol,
            )
            current = self._select_open(
                connection,
                strategy_id=strategy_id,
                symbol=fill.symbol,
            )
            closed = None
            if fill.side is Side.SELL:
                closed = self._closed_ending_with_fill(
                    connection,
                    strategy_id=strategy_id,
                    symbol=fill.symbol,
                    intent_id=fill.order_intent_id,
                    closed_at=fill.occurred_at.astimezone(UTC),
                )
            connection.execute("COMMIT")
            return PaperTradeQualityFillResult(
                applied=applied,
                open_trade=current,
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
        if not symbol or symbol != symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        if not reference_price.is_finite() or reference_price <= 0:
            raise ValueError("reference_price must be positive and finite")
        moment = _aware(observed_at, "observed_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT reference_price FROM paper_trade_quality_prices
                WHERE strategy_id=? AND symbol=? AND observed_at=?""",
                (strategy_id, symbol, moment.isoformat()),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO paper_trade_quality_prices
                    (strategy_id, symbol, observed_at, reference_price)
                    VALUES (?, ?, ?, ?)""",
                    (strategy_id, symbol, moment.isoformat(), str(reference_price)),
                )
            elif Decimal(str(existing["reference_price"])) != reference_price:
                raise ValueError("PAPER_TRADE_QUALITY_PRICE_OBSERVATION_CONFLICT")
            self._rebuild_symbol(
                connection,
                strategy_id=strategy_id,
                symbol=symbol,
            )
            current = self._select_open(
                connection,
                strategy_id=strategy_id,
                symbol=symbol,
            )
            connection.execute("COMMIT")
            return current
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
            "WHERE strategy_id=? ORDER BY closed_at, episode_id"
        )
        params: tuple[object, ...] = (strategy_id,)
        if limit is not None:
            query = (
                "SELECT * FROM (SELECT * FROM paper_trade_quality_closed "
                "WHERE strategy_id=? ORDER BY closed_at DESC, episode_id DESC LIMIT ?) "
                "ORDER BY closed_at, episode_id"
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

    def _rebuild_symbol(
        self,
        connection: sqlite3.Connection,
        *,
        strategy_id: str,
        symbol: str,
    ) -> None:
        connection.execute(
            "DELETE FROM paper_trade_quality_open WHERE strategy_id=? AND symbol=?",
            (strategy_id, symbol),
        )
        connection.execute(
            "DELETE FROM paper_trade_quality_closed WHERE strategy_id=? AND symbol=?",
            (strategy_id, symbol),
        )
        fill_rows = connection.execute(
            """SELECT * FROM paper_trade_quality_fills
            WHERE strategy_id=? AND symbol=?""",
            (strategy_id, symbol),
        ).fetchall()
        price_rows = connection.execute(
            """SELECT * FROM paper_trade_quality_prices
            WHERE strategy_id=? AND symbol=?""",
            (strategy_id, symbol),
        ).fetchall()
        reasons = {
            str(row["intent_id"]): str(row["exit_reason"])
            for row in connection.execute(
                """SELECT intent_id, exit_reason
                FROM paper_trade_quality_exit_intents
                WHERE strategy_id=? AND symbol=?""",
                (strategy_id, symbol),
            ).fetchall()
        }
        events: list[tuple[datetime, int, str, str, sqlite3.Row]] = []
        for row in fill_rows:
            side = Side(str(row["side"]))
            priority = 0 if side is Side.BUY else 2
            events.append(
                (
                    datetime.fromisoformat(str(row["occurred_at"])),
                    priority,
                    str(row["fill_id"]),
                    "FILL",
                    row,
                )
            )
        for row in price_rows:
            events.append(
                (
                    datetime.fromisoformat(str(row["observed_at"])),
                    1,
                    str(row["observed_at"]),
                    "PRICE",
                    row,
                )
            )
        events.sort(key=lambda item: (item[0], item[1], item[2]))

        episode: _ReplayEpisode | None = None
        for occurred_at, _, _, kind, row in events:
            if kind == "PRICE":
                if episode is None:
                    continue
                price = Decimal(str(row["reference_price"]))
                episode.peak_reference_price = max(
                    episode.peak_reference_price,
                    price,
                )
                episode.trough_reference_price = min(
                    episode.trough_reference_price,
                    price,
                )
                episode.last_observed_at = occurred_at
                episode.updated_at = occurred_at
                continue

            side = Side(str(row["side"]))
            quantity = Decimal(str(row["quantity"]))
            price = Decimal(str(row["price"]))
            fee = Decimal(str(row["fee"]))
            intent_id = str(row["order_intent_id"])
            fill_id = str(row["fill_id"])
            if side is Side.BUY:
                if episode is None:
                    episode = _ReplayEpisode(
                        episode_id=fill_id,
                        opened_at=occurred_at,
                        purchased_quantity=quantity,
                        sold_quantity=Decimal("0"),
                        open_quantity=quantity,
                        entry_cash_out=quantity * price + fee,
                        exit_cash_in=Decimal("0"),
                        peak_reference_price=price,
                        trough_reference_price=price,
                        last_observed_at=occurred_at,
                        updated_at=occurred_at,
                    )
                    continue
                if episode.sold_quantity > 0:
                    raise ValueError(
                        "PAPER_TRADE_QUALITY_SCALE_IN_AFTER_EXIT_NOT_SUPPORTED"
                    )
                episode.purchased_quantity += quantity
                episode.open_quantity += quantity
                episode.entry_cash_out += quantity * price + fee
                episode.peak_reference_price = max(
                    episode.peak_reference_price,
                    price,
                )
                episode.trough_reference_price = min(
                    episode.trough_reference_price,
                    price,
                )
                episode.last_observed_at = max(
                    episode.last_observed_at,
                    occurred_at,
                )
                episode.updated_at = max(episode.updated_at, occurred_at)
                continue

            if episode is None:
                raise ValueError("PAPER_TRADE_QUALITY_SELL_WITHOUT_OPEN_TRADE")
            if quantity > episode.open_quantity:
                raise ValueError("PAPER_TRADE_QUALITY_EXIT_EXCEEDS_OPEN_QUANTITY")
            episode.exit_cash_in += quantity * price - fee
            episode.sold_quantity += quantity
            episode.open_quantity -= quantity
            episode.peak_reference_price = max(episode.peak_reference_price, price)
            episode.trough_reference_price = min(
                episode.trough_reference_price,
                price,
            )
            episode.last_observed_at = max(
                episode.last_observed_at,
                occurred_at,
            )
            episode.updated_at = max(episode.updated_at, occurred_at)
            episode.last_exit_intent_id = intent_id
            episode.last_exit_reason = reasons.get(
                intent_id,
                UNATTRIBUTED_EXIT_REASON,
            )
            if episode.open_quantity == 0:
                self._insert_closed(
                    connection,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    episode=episode,
                    closed_at=occurred_at,
                )
                episode = None

        if episode is not None:
            state = OpenPaperTradeQuality(
                strategy_id=strategy_id,
                symbol=symbol,
                episode_id=episode.episode_id,
                opened_at=episode.opened_at,
                updated_at=episode.updated_at,
                purchased_quantity=episode.purchased_quantity,
                sold_quantity=episode.sold_quantity,
                open_quantity=episode.open_quantity,
                entry_cash_out=episode.entry_cash_out,
                exit_cash_in=episode.exit_cash_in,
                peak_reference_price=episode.peak_reference_price,
                trough_reference_price=episode.trough_reference_price,
                last_observed_at=episode.last_observed_at,
                last_exit_intent_id=episode.last_exit_intent_id,
                last_exit_reason=episode.last_exit_reason,
            )
            state.validate()
            self._write_open(connection, state)

    @staticmethod
    def _insert_closed(
        connection: sqlite3.Connection,
        *,
        strategy_id: str,
        symbol: str,
        episode: _ReplayEpisode,
        closed_at: datetime,
    ) -> None:
        if episode.last_exit_intent_id is None or episode.last_exit_reason is None:
            raise RuntimeError("flat trade is missing final exit identity")
        quantity = episode.purchased_quantity
        average_entry_cost = episode.entry_cash_out / quantity
        average_exit_proceeds = episode.exit_cash_in / quantity
        net_pnl = episode.exit_cash_in - episode.entry_cash_out
        return_fraction = net_pnl / episode.entry_cash_out
        mfe = max(
            Decimal("0"),
            (episode.peak_reference_price - average_entry_cost) / average_entry_cost,
        )
        mae = max(
            Decimal("0"),
            (average_entry_cost - episode.trough_reference_price) / average_entry_cost,
        )
        maximum_favorable_pnl = max(
            Decimal("0"),
            (episode.peak_reference_price - average_entry_cost) * quantity,
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
        connection.execute(
            """INSERT INTO paper_trade_quality_closed (
                episode_id, strategy_id, symbol, opened_at, closed_at, quantity,
                average_entry_cost, average_exit_proceeds, net_pnl, return_fraction,
                maximum_favorable_excursion_fraction,
                maximum_adverse_excursion_fraction, mfe_capture_ratio,
                mfe_giveback_fraction, exit_intent_id, exit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode.episode_id,
                strategy_id,
                symbol,
                episode.opened_at.astimezone(UTC).isoformat(),
                closed_at.astimezone(UTC).isoformat(),
                str(quantity),
                str(average_entry_cost),
                str(average_exit_proceeds),
                str(net_pnl),
                str(return_fraction),
                str(mfe),
                str(mae),
                None if capture is None else str(capture),
                None if giveback is None else str(giveback),
                episode.last_exit_intent_id,
                episode.last_exit_reason,
            ),
        )

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
    def _closed_ending_with_fill(
        connection: sqlite3.Connection,
        *,
        strategy_id: str,
        symbol: str,
        intent_id: str,
        closed_at: datetime,
    ) -> ClosedPaperTradeQuality | None:
        row = connection.execute(
            """SELECT * FROM paper_trade_quality_closed
            WHERE strategy_id=? AND symbol=? AND exit_intent_id=? AND closed_at=?""",
            (strategy_id, symbol, intent_id, closed_at.isoformat()),
        ).fetchone()
        return None if row is None else SQLitePaperTradeQualityStore._closed_row(row)

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
                last_observed_at, last_exit_intent_id, last_exit_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                state.last_exit_intent_id,
                state.last_exit_reason,
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
            last_exit_intent_id=(
                None
                if row["last_exit_intent_id"] is None
                else str(row["last_exit_intent_id"])
            ),
            last_exit_reason=(
                None
                if row["last_exit_reason"] is None
                else str(row["last_exit_reason"])
            ),
        )
        state.validate()
        return state

    @staticmethod
    def _closed_row(row: sqlite3.Row) -> ClosedPaperTradeQuality:
        return ClosedPaperTradeQuality(
            episode_id=str(row["episode_id"]),
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
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
    """Strategy-scoped exact-fill observer plus observed-price MFE/MAE tracker.

    MFE/MAE are based on prices actually supplied to ``observe_price`` and exact fill
    prices. They are not a claim about unobserved market highs/lows between samples.
    """

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
