"""SQLite persistence for trade logging and opportunity tracking."""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class TradeDB:
    """Thread-safe SQLite database for logging opportunities and trades."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            import os
            data_dir = os.getenv("DATA_DIR", ".")
            db_path = os.path.join(data_dir, "trades.db")
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent reads while writing
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                type TEXT NOT NULL,
                market TEXT NOT NULL,
                prices TEXT,
                total_cost REAL,
                net_profit REAL,
                net_roi REAL,
                depth REAL,
                action TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER REFERENCES opportunities(id),
                timestamp TEXT NOT NULL,
                platform TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                status TEXT NOT NULL,
                fill_price REAL,
                order_id TEXT,
                outcome TEXT
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id INTEGER REFERENCES opportunities(id),
                market_identifier TEXT NOT NULL,
                platform TEXT NOT NULL,
                entry_timestamp TEXT NOT NULL,
                settlement_timestamp TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                realized_pnl REAL,
                expected_pnl REAL
            );

            CREATE TABLE IF NOT EXISTS partial_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER REFERENCES trades(id),
                opportunity_id INTEGER REFERENCES opportunities(id),
                platform TEXT NOT NULL,
                token_id TEXT,
                side TEXT NOT NULL,
                fill_price REAL NOT NULL,
                size REAL NOT NULL,
                hedge_status TEXT NOT NULL DEFAULT 'pending',
                hedge_attempts INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS reward_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                market_key TEXT NOT NULL,
                order_id TEXT,
                event TEXT NOT NULL,
                size REAL,
                spread REAL,
                resting_seconds INTEGER,
                timestamp INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                from_platform TEXT NOT NULL,
                to_platform TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                tx_hash TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                idempotency_key TEXT NOT NULL UNIQUE,
                error TEXT
            );
        """)
        self.conn.commit()

        # Create indexes for common query patterns (safe — IF NOT EXISTS)
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_trades_opportunity_id
                ON trades(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_positions_status
                ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_positions_opportunity_id
                ON positions(opportunity_id);
            CREATE INDEX IF NOT EXISTS idx_partial_fills_hedge_status
                ON partial_fills(hedge_status, created_at);
            CREATE INDEX IF NOT EXISTS idx_opportunities_timestamp
                ON opportunities(timestamp);
        """)
        self.conn.commit()

        # Safe migration: add slippage column if it doesn't exist
        try:
            self.conn.execute("ALTER TABLE trades ADD COLUMN slippage REAL")
            self.conn.commit()
        except sqlite3.OperationalError:
            logger.debug("Migration: slippage column already exists")

        # Safe migration: add reward columns if they don't exist
        try:
            self.conn.execute("ALTER TABLE trades ADD COLUMN reward_score REAL")
            self.conn.commit()
        except sqlite3.OperationalError:
            logger.debug("Migration: reward_score column already exists")

        try:
            self.conn.execute("ALTER TABLE trades ADD COLUMN reward_yield_usdc REAL")
            self.conn.commit()
        except sqlite3.OperationalError:
            logger.debug("Migration: reward_yield_usdc column already exists")

        # Safe migration: add outcome to trades. Polymarket BUY_NO legs are
        # logged with side="BUY", so settlement cannot recover the traded
        # outcome (yes/no) from the side column — persist it explicitly.
        try:
            self.conn.execute("ALTER TABLE trades ADD COLUMN outcome TEXT")
            self.conn.commit()
        except sqlite3.OperationalError:
            logger.debug("Migration: outcome column already exists on trades")

        # Safe migration: add market_ticker to positions. Settlement checks
        # query the platform API by ticker (e.g. KXEPLSPREAD-...), but
        # market_identifier holds the human title (e.g. "Chelsea: Spreads").
        # Without this column the Kalshi settlement lookup fails on every
        # position, so positions never settle and realized_pnl stays NULL.
        try:
            self.conn.execute("ALTER TABLE positions ADD COLUMN market_ticker TEXT")
            self.conn.commit()
        except sqlite3.OperationalError:
            logger.debug("Migration: market_ticker column already exists on positions")

        # Safe migration: add hedge identifier columns to partial_fills.
        # Without these, _hedge_betfair/_smarkets/_sxbet/_matchbook/_gemini cannot
        # locate the original market and silently fail every hedge attempt.
        for col in (
            "_market_id", "_selection_id", "_contract_id",
            "_market_hash", "_runner_id", "_outcome_id", "_symbol",
        ):
            try:
                self.conn.execute(f"ALTER TABLE partial_fills ADD COLUMN {col} TEXT")
                self.conn.commit()
            except sqlite3.OperationalError:
                logger.debug("Migration: %s column already exists on partial_fills", col)

    def log_opportunity(
        self,
        opp_type: str,
        market: str,
        prices: str,
        total_cost: float,
        net_profit: float,
        net_roi: float,
        depth: float,
        action: str,
    ) -> int | None:
        """Log a detected opportunity. Returns the opportunity ID."""
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO opportunities
                   (timestamp, type, market, prices, total_cost, net_profit, net_roi, depth, action)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    opp_type,
                    market,
                    prices,
                    total_cost,
                    net_profit,
                    net_roi,
                    depth,
                    action,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def log_trade(
        self,
        opportunity_id: int,
        platform: str,
        side: str,
        price: float,
        size: float,
        status: str,
        fill_price: float | None = None,
        order_id: str | None = None,
        outcome: str | None = None,
    ) -> int | None:
        """Log a trade leg. Returns the trade ID.

        outcome is the traded market outcome (e.g. "yes"/"no") — distinct
        from side (BUY/SELL) so settlement can score Polymarket BUY_NO legs.
        """
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO trades
                   (opportunity_id, timestamp, platform, side, price, size, status, fill_price, order_id, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    opportunity_id,
                    datetime.now(timezone.utc).isoformat(),
                    platform,
                    side,
                    price,
                    size,
                    status,
                    fill_price,
                    order_id,
                    outcome,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def update_trade_status(self, trade_id: int, status: str, fill_price: float | None = None,
                            slippage: float | None = None):
        """Update the status of a trade leg, optionally with fill price and slippage."""
        with self._lock:
            if fill_price is not None:
                self.conn.execute(
                    "UPDATE trades SET status = ?, fill_price = ? WHERE id = ?",
                    (status, fill_price, trade_id),
                )
            else:
                self.conn.execute(
                    "UPDATE trades SET status = ? WHERE id = ?",
                    (status, trade_id),
                )
            if slippage is not None:
                self.conn.execute(
                    "UPDATE trades SET slippage = ? WHERE id = ?",
                    (slippage, trade_id),
                )
            self.conn.commit()

    def set_trade_order_id(self, trade_id: int, order_id: str | None):
        """Persist the exchange order ID on a trade row.

        Used when a leg fails with an unconfirmed cancel: the order may still
        be live at the venue, so the ID must survive restarts for
        recovery.reconcile_orphaned_positions() to find it.
        """
        with self._lock:
            self.conn.execute(
                "UPDATE trades SET order_id = ? WHERE id = ?",
                (order_id, trade_id),
            )
            self.conn.commit()

    def get_daily_pnl(self) -> float:
        """Get realized P&L from positions settled today, plus expected P&L from open positions today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            row = self.conn.execute(
                """SELECT COALESCE(SUM(realized_pnl), 0) as total
                   FROM positions
                   WHERE status = 'settled' AND settlement_timestamp LIKE ?""",
                (f"{today}%",),
            ).fetchone()
            realized = row["total"]
            row2 = self.conn.execute(
                """SELECT COALESCE(SUM(expected_pnl), 0) as total
                   FROM positions
                   WHERE status = 'open' AND entry_timestamp LIKE ?""",
                (f"{today}%",),
            ).fetchone()
            return realized + row2["total"]

    def get_daily_trade_count(self) -> int:
        """Count trades actually executed (filled) today (UTC)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM trades WHERE timestamp LIKE ? AND status = 'filled'",
                (f"{today}%",),
            ).fetchone()
            return row["cnt"]

    def get_open_positions_count(self) -> int:
        """Count positions that are currently open (not yet settled)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM positions WHERE status = 'open'"
            ).fetchone()
            return row["cnt"]

    def get_recent_opportunities(self, limit: int = 50) -> list[dict]:
        """Get recent opportunities."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM opportunities ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_trades_for_opportunity(self, opportunity_id: int) -> list[dict]:
        """Get all trade legs for an opportunity."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE opportunity_id = ? ORDER BY id",
                (opportunity_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def create_position(
        self,
        opportunity_id: int,
        market_identifier: str,
        platform: str,
        expected_pnl: float,
        market_ticker: str | None = None,
    ) -> int | None:
        """Create an open position after a trade is filled. Returns position ID.

        market_ticker is the platform-native id used for settlement lookups
        (e.g. a Kalshi ticker). When omitted, settlement falls back to
        market_identifier, which only works if that already holds a ticker.
        """
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO positions
                   (opportunity_id, market_identifier, platform, entry_timestamp, status, expected_pnl, market_ticker)
                   VALUES (?, ?, ?, ?, 'open', ?, ?)""",
                (
                    opportunity_id,
                    market_identifier,
                    platform,
                    datetime.now(timezone.utc).isoformat(),
                    expected_pnl,
                    market_ticker,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def settle_position(self, position_id: int, realized_pnl: float, status: str = "settled"):
        """Mark a position as settled with realized P&L."""
        with self._lock:
            self.conn.execute(
                """UPDATE positions
                   SET status = ?, realized_pnl = ?, settlement_timestamp = ?
                   WHERE id = ?""",
                (status, realized_pnl, datetime.now(timezone.utc).isoformat(), position_id),
            )
            self.conn.commit()

    def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status = 'open' ORDER BY entry_timestamp"
            ).fetchall()
            return [dict(r) for r in rows]

    def is_market_active(self, market: str) -> bool:
        """Check if there's an open position for this market."""
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) as cnt FROM positions
                   WHERE market_identifier = ? AND status = 'open'""",
                (market,),
            ).fetchone()
            return row["cnt"] > 0

    def has_recent_trade(self, market: str, window_secs: float = 60.0) -> bool:
        """Return True if a non-skipped opportunity for this market exists within window.

        Skipped opportunities (action LIKE 'skipped:%') are excluded so that
        recording a skip does not trigger a false-positive dedup on the next
        legitimate execution attempt. HARDEN-05.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_secs)).isoformat()
        with self._lock:
            row = self.conn.execute(
                """SELECT 1 FROM opportunities
                   WHERE market = ? AND timestamp > ? AND action NOT LIKE 'skipped:%'
                   LIMIT 1""",
                (market, cutoff),
            ).fetchone()
        return row is not None

    def get_active_market_expected_pnl(self, market: str) -> float | None:
        """Get the expected P&L of the best open position for this market.

        Returns None if no open position exists.
        """
        with self._lock:
            row = self.conn.execute(
                """SELECT MAX(expected_pnl) as best_pnl FROM positions
                   WHERE market_identifier = ? AND status = 'open'""",
                (market,),
            ).fetchone()
            return row["best_pnl"] if row and row["best_pnl"] is not None else None

    def get_pending_trades(self) -> list[dict]:
        """Get trades with status 'pending' (may be orphaned from a crash)."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE status = 'pending' ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def get_open_positions_with_trades(self) -> list[dict]:
        """Get open positions with their associated trade order IDs.

        Returns positions joined with their trade legs so crash recovery
        can check order status on each platform.
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT p.*, t.platform as trade_platform, t.order_id, t.status as trade_status
                   FROM positions p
                   LEFT JOIN trades t ON t.opportunity_id = p.opportunity_id
                   WHERE p.status = 'open'
                   ORDER BY p.id, t.id"""
            ).fetchall()
            return [dict(r) for r in rows]

    def get_avg_slippage(self) -> float:
        """Get average slippage across all trades that have slippage data."""
        with self._lock:
            row = self.conn.execute(
                "SELECT AVG(slippage) as avg_slip FROM trades WHERE slippage IS NOT NULL"
            ).fetchone()
            return row["avg_slip"] if row and row["avg_slip"] is not None else 0.0

    def log_partial_fill(
        self,
        trade_id: int,
        opportunity_id: int,
        platform: str,
        token_id: str,
        side: str,
        fill_price: float,
        size: float,
        market_id: str | None = None,
        selection_id: str | None = None,
        contract_id: str | None = None,
        market_hash: str | None = None,
        runner_id: str | None = None,
        outcome_id: str | None = None,
        symbol: str | None = None,
    ) -> int | None:
        """Log a partial fill for hedging. Returns partial_fill ID.

        Platform-specific identifiers (market_id, selection_id, etc.) must be
        persisted because the hedger needs them to place the opposing order on
        Betfair, Smarkets, SX Bet, Matchbook, and Gemini.
        """
        with self._lock:
            cur = self.conn.execute(
                """INSERT INTO partial_fills
                   (trade_id, opportunity_id, platform, token_id, side, fill_price, size, created_at,
                    _market_id, _selection_id, _contract_id, _market_hash, _runner_id, _outcome_id, _symbol)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (trade_id, opportunity_id, platform, token_id, side, fill_price, size,
                 datetime.now(timezone.utc).isoformat(),
                 market_id, selection_id, contract_id, market_hash, runner_id, outcome_id, symbol),
            )
            self.conn.commit()
            return cur.lastrowid

    def get_pending_partial_fills(self) -> list[dict]:
        """Get partial fills awaiting hedge."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM partial_fills WHERE hedge_status = 'pending' ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_partial_fill(self, pf_id: int, status: str, attempts: int | None = None):
        """Update partial fill hedge status."""
        with self._lock:
            if attempts is not None:
                self.conn.execute(
                    "UPDATE partial_fills SET hedge_status = ?, hedge_attempts = ? WHERE id = ?",
                    (status, attempts, pf_id),
                )
            else:
                self.conn.execute(
                    "UPDATE partial_fills SET hedge_status = ? WHERE id = ?",
                    (status, pf_id),
                )
            if status in ("hedged", "failed"):
                self.conn.execute(
                    "UPDATE partial_fills SET resolved_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), pf_id),
                )
            self.conn.commit()

    # ---------------------------------------------------------------------------
    # Dashboard queries
    # ---------------------------------------------------------------------------

    def get_daily_pnl_history(self, days: int = 30) -> list[dict]:
        """Get daily realized P&L aggregated by date for the last N days.

        Returns:
            List of dicts with keys 'date' (YYYY-MM-DD) and 'pnl' (float).
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT date(settlement_timestamp) as date,
                          SUM(realized_pnl) as pnl
                   FROM positions
                   WHERE status = 'settled'
                     AND settlement_timestamp >= date('now', ?)
                   GROUP BY date(settlement_timestamp)
                   ORDER BY date""",
                (f"-{days} days",),
            ).fetchall()
            return [{"date": r["date"], "pnl": round(r["pnl"], 4)} for r in rows]

    def get_positions_by_platform(self) -> list[dict]:
        """Get open position counts and total expected P&L grouped by platform.

        Returns:
            List of dicts with keys 'platform', 'count', 'total_expected_pnl'.
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT platform,
                          COUNT(*) as count,
                          COALESCE(SUM(expected_pnl), 0) as total_expected_pnl
                   FROM positions
                   WHERE status = 'open'
                   GROUP BY platform
                   ORDER BY count DESC"""
            ).fetchall()
            return [
                {
                    "platform": r["platform"],
                    "count": r["count"],
                    "total_expected_pnl": round(r["total_expected_pnl"], 4),
                }
                for r in rows
            ]

    def get_strategy_pnl(self) -> list[dict]:
        """Get realized P&L per strategy type by joining trades with opportunities.

        Uses the opportunities.net_profit column as a proxy for trade P&L since
        the trades table does not have a dedicated pnl column. net_profit and
        win_count are aggregated once per opportunity (de-duplicated across its
        trade legs); trade_count is the number of trade legs.

        Returns:
            List of dicts with keys: strategy, trade_count, win_count,
            total_pnl, avg_profit. Ordered by total_pnl descending.
        """
        with self._lock:
            rows = self.conn.execute(
                """WITH opp_pnl AS (
                       SELECT DISTINCT o.id, o.type, o.net_profit
                       FROM opportunities o
                       WHERE EXISTS (
                           SELECT 1 FROM trades t WHERE t.opportunity_id = o.id
                       )
                   ),
                   leg_counts AS (
                       SELECT o.type AS type, COUNT(t.id) AS trade_count
                       FROM trades t
                       JOIN opportunities o ON t.opportunity_id = o.id
                       GROUP BY o.type
                   )
                   SELECT p.type AS strategy,
                          lc.trade_count AS trade_count,
                          SUM(CASE WHEN p.net_profit > 0 THEN 1 ELSE 0 END) AS win_count,
                          COALESCE(SUM(p.net_profit), 0) AS total_pnl,
                          COALESCE(AVG(p.net_profit), 0) AS avg_profit
                   FROM opp_pnl p
                   JOIN leg_counts lc ON lc.type = p.type
                   GROUP BY p.type, lc.trade_count
                   ORDER BY total_pnl DESC"""
            ).fetchall()
            return [
                {
                    "strategy": r["strategy"],
                    "trade_count": r["trade_count"],
                    "win_count": r["win_count"],
                    "total_pnl": round(r["total_pnl"], 4),
                    "avg_profit": round(r["avg_profit"], 4),
                }
                for r in rows
            ]

    def get_opportunities_after(self, after_id: int, limit: int = 500) -> list[dict]:
        """Return opportunities with id > after_id, oldest first (for mirroring)."""
        with self._lock:
            cur = self.conn.execute(
                """SELECT id, timestamp, type, market, prices, total_cost,
                          net_profit, net_roi, depth, action
                   FROM opportunities WHERE id > ? ORDER BY id ASC LIMIT ?""",
                (after_id, limit),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_opportunity_stats_by_type(self) -> list[dict]:
        """Get opportunity statistics grouped by type.

        Returns:
            List of dicts with type, count, avg_roi, avg_profit, total_profit.
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT type,
                          COUNT(*) as count,
                          AVG(net_roi) as avg_roi,
                          AVG(net_profit) as avg_profit,
                          SUM(net_profit) as total_profit
                   FROM opportunities
                   GROUP BY type
                   ORDER BY count DESC"""
            ).fetchall()
            return [
                {
                    "type": r["type"],
                    "count": r["count"],
                    "avg_roi": round(r["avg_roi"] or 0, 4),
                    "avg_profit": round(r["avg_profit"] or 0, 4),
                    "total_profit": round(r["total_profit"] or 0, 4),
                }
                for r in rows
            ]

    def get_recent_trades(self, limit: int = 100) -> list[dict]:
        """Get recent trades with their opportunity context.

        Returns:
            List of trade dicts enriched with opportunity type and market.
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT t.*, o.type as opp_type, o.market as opp_market
                   FROM trades t
                   LEFT JOIN opportunities o ON t.opportunity_id = o.id
                   ORDER BY t.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_cumulative_pnl(self) -> float:
        """Get total realized P&L across all settled positions."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) as total FROM positions WHERE status = 'settled'"
            ).fetchone()
            return round(row["total"], 4)

    def get_failed_trades(self, limit: int = 50) -> list[dict]:
        """Get recent failed trades with opportunity context.

        Args:
            limit: Maximum number of failed trades to return.

        Returns:
            List of failed trade dicts enriched with opportunity type and market.
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT t.*, o.type as opp_type, o.market as opp_market
                   FROM trades t
                   LEFT JOIN opportunities o ON t.opportunity_id = o.id
                   WHERE t.status = 'failed'
                   ORDER BY t.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_failure_stats(self) -> dict:
        """Get failure statistics: counts by platform, by hour, and overall rate.

        Returns:
            Dict with keys 'by_platform', 'by_hour', 'total_failed',
            'total_trades', and 'failure_rate'.
        """
        with self._lock:
            # Total counts
            total_row = self.conn.execute(
                "SELECT COUNT(*) as total FROM trades"
            ).fetchone()
            failed_row = self.conn.execute(
                "SELECT COUNT(*) as total FROM trades WHERE status = 'failed'"
            ).fetchone()
            total = total_row["total"]
            failed = failed_row["total"]

            # By platform
            platform_rows = self.conn.execute(
                """SELECT platform, COUNT(*) as count
                   FROM trades WHERE status = 'failed'
                   GROUP BY platform ORDER BY count DESC"""
            ).fetchall()

            # By hour (last 24 hours, hourly buckets)
            hour_rows = self.conn.execute(
                """SELECT strftime('%Y-%m-%dT%H:00:00', timestamp) as hour,
                          COUNT(*) as failed_count,
                          (SELECT COUNT(*) FROM trades t2
                           WHERE strftime('%Y-%m-%dT%H:00:00', t2.timestamp)
                                 = strftime('%Y-%m-%dT%H:00:00', t.timestamp)
                          ) as total_count
                   FROM trades t
                   WHERE status = 'failed'
                     AND timestamp >= datetime('now', '-24 hours')
                   GROUP BY hour
                   ORDER BY hour"""
            ).fetchall()

            return {
                "total_failed": failed,
                "total_trades": total,
                "failure_rate": round(failed / total, 4) if total > 0 else 0.0,
                "by_platform": [
                    {"platform": r["platform"], "count": r["count"]}
                    for r in platform_rows
                ],
                "by_hour": [
                    {
                        "hour": r["hour"],
                        "failed": r["failed_count"],
                        "total": r["total_count"],
                    }
                    for r in hour_rows
                ],
            }

    # ---------------------------------------------------------------------------
    # Admin / maintenance
    # ---------------------------------------------------------------------------

    def purge_opportunities_by_type(self, opp_type: str) -> dict:
        """Delete all opportunities and their associated trades for a given type.

        Also removes any positions and partial fills linked to those opportunities.
        Returns a summary dict with counts of deleted rows.

        Args:
            opp_type: The opportunity type to purge (e.g. 'SpreadKalshi').

        Returns:
            Dict with keys 'opportunities', 'trades', 'positions', 'partial_fills'.
        """
        with self._lock:
            # Get opportunity IDs first
            rows = self.conn.execute(
                "SELECT id FROM opportunities WHERE type = ?", (opp_type,)
            ).fetchall()
            opp_ids = [r["id"] for r in rows]

            if not opp_ids:
                return {"opportunities": 0, "trades": 0, "positions": 0, "partial_fills": 0}

            placeholders = ",".join("?" for _ in opp_ids)

            # Delete in dependency order: partial_fills -> trades -> positions -> opportunities
            pf_cur = self.conn.execute(
                f"DELETE FROM partial_fills WHERE opportunity_id IN ({placeholders})",
                opp_ids,
            )
            tr_cur = self.conn.execute(
                f"DELETE FROM trades WHERE opportunity_id IN ({placeholders})",
                opp_ids,
            )
            pos_cur = self.conn.execute(
                f"DELETE FROM positions WHERE opportunity_id IN ({placeholders})",
                opp_ids,
            )
            opp_cur = self.conn.execute(
                f"DELETE FROM opportunities WHERE type = ?", (opp_type,)
            )
            self.conn.commit()

            result = {
                "opportunities": opp_cur.rowcount,
                "trades": tr_cur.rowcount,
                "positions": pos_cur.rowcount,
                "partial_fills": pf_cur.rowcount,
            }
            logger.info("Purged %s: %s", opp_type, result)
            return result

    def get_db_stats(self) -> dict:
        """Get row counts for all tables — useful for dashboard diagnostics.

        Returns:
            Dict with table names as keys and row counts as values.
        """
        with self._lock:
            stats = {}
            for table in ("opportunities", "trades", "positions", "partial_fills"):
                row = self.conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
                stats[table] = row["cnt"]
            return stats

    def get_performance_stats(self) -> dict:
        """Compute historical performance statistics from settled positions.

        Returns:
            Dict with win_rate, avg_pnl, total_trades, max_win, max_loss,
            sharpe_ratio (approximation), avg_hold_time, and per-strategy stats.
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT p.realized_pnl, p.expected_pnl,
                          p.entry_timestamp, p.settlement_timestamp,
                          o.type as opp_type
                   FROM positions p
                   LEFT JOIN opportunities o ON p.opportunity_id = o.id
                   WHERE p.status = 'settled'
                   ORDER BY p.settlement_timestamp"""
            ).fetchall()

        if not rows:
            return {
                "total_settled": 0, "win_rate": 0, "avg_pnl": 0,
                "max_win": 0, "max_loss": 0, "sharpe_ratio": 0,
                "avg_hold_seconds": 0, "strategy_breakdown": [],
            }

        pnls = []
        hold_times = []
        strategy_pnls: dict[str, list[float]] = {}

        for r in rows:
            pnl = r["realized_pnl"] or 0
            pnls.append(pnl)
            opp_type = r["opp_type"] or "Unknown"
            strategy_pnls.setdefault(opp_type, []).append(pnl)

            # Hold time
            entry = r["entry_timestamp"]
            settle = r["settlement_timestamp"]
            if entry and settle:
                try:
                    from datetime import datetime, timezone
                    t0 = datetime.fromisoformat(entry.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(settle.replace("Z", "+00:00"))
                    hold_times.append((t1 - t0).total_seconds())
                except Exception:
                    pass

        wins = sum(1 for p in pnls if p > 0)
        total = len(pnls)
        avg_pnl = sum(pnls) / total if total else 0
        max_win = max(pnls) if pnls else 0
        max_loss = min(pnls) if pnls else 0

        # Simplified Sharpe: mean / std of PnL (no risk-free rate)
        import math
        mean = avg_pnl
        if total > 1:
            variance = sum((p - mean) ** 2 for p in pnls) / (total - 1)
            std = math.sqrt(variance) if variance > 0 else 0
            sharpe = mean / std if std > 0 else 0
        else:
            sharpe = 0

        # Strategy breakdown
        breakdown = []
        for stype, spnls in sorted(strategy_pnls.items()):
            s_wins = sum(1 for p in spnls if p > 0)
            breakdown.append({
                "type": stype,
                "count": len(spnls),
                "win_rate": s_wins / len(spnls) if spnls else 0,
                "total_pnl": sum(spnls),
                "avg_pnl": sum(spnls) / len(spnls) if spnls else 0,
            })

        return {
            "total_settled": total,
            "win_rate": wins / total if total else 0,
            "avg_pnl": avg_pnl,
            "total_pnl": sum(pnls),
            "max_win": max_win,
            "max_loss": max_loss,
            "sharpe_ratio": round(sharpe, 4),
            "avg_hold_seconds": sum(hold_times) / len(hold_times) if hold_times else 0,
            "strategy_breakdown": breakdown,
        }

    def log_reward_metric(self, platform: str, market_key: str, order_id: str,
                         event: str, size: float, spread: float,
                         resting_seconds: int) -> None:
        """Log Kalshi reward tracking metrics (no Polymarket public API).

        Args:
            platform: "polymarket" or "kalshi"
            market_key: Market identifier
            order_id: Exchange order ID
            event: "placed" or "cancelled"
            size: Order size in dollars
            spread: Bid-ask spread as decimal (0.05 = 5%)
            resting_seconds: Seconds order has been resting
        """
        import time
        with self._lock:
            self.conn.execute(
                """INSERT INTO reward_metrics
                   (platform, market_key, order_id, event, size, spread, resting_seconds, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (platform, market_key, order_id, event, size, spread, resting_seconds,
                 int(time.time())),
            )
            self.conn.commit()

    def get_reward_metrics(self, since_ts: int = 0) -> list[dict]:
        """Read reward_metrics rows for cross-engine sync.

        Args:
            since_ts: Only return rows with timestamp at or after this
                Unix-seconds value (default 0 = all rows). Inclusive so rows
                written in the same second as the last sync are not skipped;
                the downstream upsert dedupes on (engine, source_key).

        Returns:
            A list of dicts, oldest first, each with id, platform, market_key,
            order_id, event, size, spread, resting_seconds, timestamp.
        """
        with self._lock:
            cursor = self.conn.execute(
                """SELECT id, platform, market_key, order_id, event, size, spread,
                          resting_seconds, timestamp
                   FROM reward_metrics
                   WHERE timestamp >= ?
                   ORDER BY timestamp ASC, id ASC""",
                (since_ts,),
            )
            return [dict(row) for row in cursor.fetchall()]

    # ---------------------------------------------------------------------
    # Treasury / auto-rebalance audit (Strategy #18)
    # ---------------------------------------------------------------------

    def log_transfer(
        self,
        from_platform: str,
        to_platform: str,
        amount_usd: float,
        idempotency_key: str,
        status: str = "pending",
        tx_hash: str | None = None,
        error: str | None = None,
    ) -> int | None:
        """Insert a row into the transfers table.

        Idempotency: ``idempotency_key`` has UNIQUE constraint, so a replayed
        transfer raises ``sqlite3.IntegrityError``. The treasury layer catches
        that and returns the existing row's id without re-executing.
        """
        with self._lock:
            try:
                cur = self.conn.execute(
                    """INSERT INTO transfers
                       (timestamp, from_platform, to_platform, amount_usd,
                        tx_hash, status, idempotency_key, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        from_platform,
                        to_platform,
                        amount_usd,
                        tx_hash,
                        status,
                        idempotency_key,
                        error,
                    ),
                )
                self.conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                # Replay — return existing row id
                row = self.conn.execute(
                    "SELECT id FROM transfers WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                return row["id"] if row else None

    def update_transfer(
        self,
        transfer_id: int,
        status: str,
        tx_hash: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update transfer status (pending → succeeded / failed)."""
        with self._lock:
            self.conn.execute(
                """UPDATE transfers SET status = ?, tx_hash = COALESCE(?, tx_hash),
                                        error = COALESCE(?, error)
                   WHERE id = ?""",
                (status, tx_hash, error, transfer_id),
            )
            self.conn.commit()

    def claim_transfer(
        self,
        from_platform: str,
        to_platform: str,
        amount_usd: float,
        idempotency_key: str,
    ) -> tuple[int | None, bool]:
        """Atomically claim an idempotency key for a pending transfer."""
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO transfers
                   (timestamp, from_platform, to_platform, amount_usd,
                    status, idempotency_key)
                   VALUES (?, ?, ?, ?, 'pending', ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    from_platform,
                    to_platform,
                    amount_usd,
                    idempotency_key,
                ),
            )
            created = cur.rowcount == 1
            row = self.conn.execute(
                "SELECT id FROM transfers WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            self.conn.commit()
            return (row["id"] if row else None), created

    def get_transfer_by_idempotency_key(self, idempotency_key: str) -> dict | None:
        """Return the transfer already associated with an idempotency key."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM transfers WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_transfers_today(self) -> list[dict]:
        """Return transfers initiated in the last 24h. Used for daily limits."""
        cutoff = (datetime.now(timezone.utc).timestamp() - 86400)
        with self._lock:
            rows = self.conn.execute(
                """SELECT * FROM transfers
                   WHERE strftime('%s', timestamp) >= ?
                   ORDER BY id DESC""",
                (str(int(cutoff)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
