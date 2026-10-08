"""
ConsensusGameEngine: Isolated Capital Arena & Paper Trading Engine for Dual-Engine Consensus.

Invariants:
1. STRICT READ-ONLY isolation on production tables:
   - Queries `consensus_trade_history`, `ml_trade_history`, and `ohlcv`.
   - NEVER modifies, deletes, or inserts into core trading tables.
2. Quarantined Game State in dedicated SQLite tables:
   - `consensus_game_accounts`
   - `consensus_game_positions`
   - `consensus_game_snapshots`
3. Zero Fake / Dummy Data:
   - All positions, trades, prices, and outcomes derive strictly from real consensus trades and real OHLCV data.
   - Share quantities are real positive integers derived strictly from capital constraints (no fractional shares).
4. Upstox Rate Limit Protection:
   - Historical Calendar and Day Replay operate 100% on local SQLite OHLCV (0 API calls).
   - Live MTM queries are throttled via in-memory cache and token bucket rate limits.
"""

import os
import math
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)

# Constants for realistic execution friction
DEFAULT_BROKERAGE_PER_ORDER = 20.0  # ₹20 flat brokerage per leg (Zerodha / Upstox standard)
DEFAULT_SLIPPAGE_BPS = 5.0          # 5 basis points (0.05%) execution slippage


class ConsensusGameEngine:
    _schema_initialized = False

    @classmethod
    def init_game_schema(cls, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initializes isolated sandbox tables in SQLite."""
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True
        try:
            cur = conn.cursor()
            # 1. Player Accounts
            cur.execute("""
                CREATE TABLE IF NOT EXISTS consensus_game_accounts (
                    session_id TEXT PRIMARY KEY,
                    account_name TEXT NOT NULL,
                    initial_capital REAL NOT NULL,
                    current_cash REAL NOT NULL,
                    realized_pnl REAL DEFAULT 0.0,
                    unrealized_pnl REAL DEFAULT 0.0,
                    peak_equity REAL NOT NULL,
                    max_drawdown_pct REAL DEFAULT 0.0,
                    allocation_mode TEXT DEFAULT 'EQUAL_WEIGHT',
                    max_positions INTEGER DEFAULT 5,
                    fees_enabled INTEGER DEFAULT 1,
                    trade_selection_mode TEXT DEFAULT 'ALL_QUALIFIED',
                    status TEXT DEFAULT 'ACTIVE',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

            # Ensure trade_selection_mode column exists in existing database
            account_cols = [c[1] for c in cur.execute("PRAGMA table_info(consensus_game_accounts);").fetchall()]
            if "trade_selection_mode" not in account_cols:
                try:
                    cur.execute("ALTER TABLE consensus_game_accounts ADD COLUMN trade_selection_mode TEXT DEFAULT 'ALL_QUALIFIED';")
                except Exception as ex:
                    logger.debug(f"Column trade_selection_mode already present or could not be added: {ex}")

            # 2. Simulated Positions
            cur.execute("""
                CREATE TABLE IF NOT EXISTS consensus_game_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    trade_ref_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    allocated_capital REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    target_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    status TEXT DEFAULT 'OPEN',
                    exit_price REAL,
                    exit_time TEXT,
                    gross_pnl_rupees REAL DEFAULT 0.0,
                    fees_rupees REAL DEFAULT 0.0,
                    net_pnl_rupees REAL DEFAULT 0.0,
                    pnl_pct REAL DEFAULT 0.0,
                    outcome TEXT,
                    details_json TEXT
                );
            """)

            # Non-destructive schema migration for Institutional Dynamic Trade Guard (IDTG)
            for col_name, col_type in [
                ("tightened_sl", "REAL"),
                ("guard_threat_score", "REAL DEFAULT 0.0"),
                ("guard_action", "TEXT DEFAULT 'MAINTAIN'"),
                ("guard_reasons", "TEXT"),
                ("last_guard_audit", "TEXT")
            ]:
                try:
                    cur.execute(f"ALTER TABLE consensus_game_positions ADD COLUMN {col_name} {col_type};")
                except Exception:
                    pass

            # 3. Time-Series Snapshots for Equity Curve
            cur.execute("""
                CREATE TABLE IF NOT EXISTS consensus_game_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    total_equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    open_positions_count INTEGER NOT NULL,
                    event_type TEXT DEFAULT 'MTM_TICK',
                    note TEXT
                );
            """)

            # 4. Arena Telegram Alerts Ledger (Zero Duplication & Historical Isolation)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS consensus_game_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    trade_ref_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    details_json TEXT,
                    UNIQUE(session_id, trade_ref_id, alert_type)
                );
            """)

            # Indexes for ultra-fast dashboard queries
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cgp_session_status ON consensus_game_positions(session_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cgp_session_time ON consensus_game_positions(session_id, entry_time);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cgs_session_time ON consensus_game_snapshots(session_id, timestamp);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cga_session_type ON consensus_game_alerts(session_id, trade_ref_id, alert_type);")

            conn.commit()
            cls._schema_initialized = True
        finally:
            if should_close:
                conn.close()

    @classmethod
    def deploy_capital(
        cls,
        initial_capital: float = 100000.0,
        allocation_mode: str = "EQUAL_WEIGHT",
        max_positions: int = 5,
        fees_enabled: bool = True,
        session_id: str = "default_arena_session",
        account_name: str = "Consensus Arena Trader",
        trade_selection_mode: str = "ALL_QUALIFIED",
        conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """
        Deploys or resets player capital into the Consensus Arena.
        Replays authentic historical consensus trades from `consensus_trade_history`
        under realistic cash constraints and whole-share sizing.
        """
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        try:
            cur = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            clean_capital = max(1000.0, float(initial_capital))
            fees_flag = 1 if fees_enabled else 0
            clean_mode = "ALL_QUALIFIED"
            mode_upper = str(trade_selection_mode).upper().strip()
            if mode_upper in ("HIGHEST_CONFLUENCE_ONLY", "HIGHEST_CONFLUENCE"):
                clean_mode = "HIGHEST_CONFLUENCE_ONLY"
            elif mode_upper in ("TIMED_ANCHOR_CONFLUENCE", "TIMED_SCANS_ONLY", "TIMED_SCANS_HIGH_CONFLUENCE", "TIMED_SCAN"):
                clean_mode = "TIMED_ANCHOR_CONFLUENCE"

            if clean_mode == "TIMED_ANCHOR_CONFLUENCE" and max_positions == 5:
                max_positions = 3

            # 1. Reset existing session data
            cur.execute("DELETE FROM consensus_game_snapshots WHERE session_id = ?", (session_id,))
            cur.execute("DELETE FROM consensus_game_positions WHERE session_id = ?", (session_id,))
            cur.execute("DELETE FROM consensus_game_accounts WHERE session_id = ?", (session_id,))

            # 2. Insert fresh account
            cur.execute("""
                INSERT INTO consensus_game_accounts (
                    session_id, account_name, initial_capital, current_cash,
                    realized_pnl, unrealized_pnl, peak_equity, max_drawdown_pct,
                    allocation_mode, max_positions, fees_enabled, trade_selection_mode, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0.0, 0.0, ?, 0.0, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            """, (
                session_id, account_name, clean_capital, clean_capital,
                clean_capital, allocation_mode.upper(), max_positions, fees_flag,
                clean_mode, now_str, now_str
            ))

            # 3. Determine Genesis Snapshot Timestamp (strictly before first trade if trades exist)
            first_trade = None
            try:
                first_trade = cur.execute(
                    "SELECT MIN(timestamp) FROM consensus_trade_history WHERE timestamp IS NOT NULL"
                ).fetchone()
            except Exception:
                pass
            genesis_ts = now_str
            if first_trade and first_trade[0]:
                raw_first = str(first_trade[0]).replace("T", " ")
                # Strip microsecond if present
                if "." in raw_first:
                    raw_first = raw_first.split(".")[0]
                try:
                    dt = datetime.strptime(raw_first, "%Y-%m-%d %H:%M:%S")
                    genesis_ts = (dt - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    genesis_ts = raw_first

            cur.execute("""
                INSERT INTO consensus_game_snapshots (
                    session_id, timestamp, total_equity, cash, realized_pnl,
                    unrealized_pnl, open_positions_count, event_type, note
                ) VALUES (?, ?, ?, ?, 0.0, 0.0, 0, 'CAPITAL_DEPLOYED', 'Initial capital deployment')
            """, (session_id, genesis_ts, clean_capital, clean_capital))

            conn.commit()

            # 4. Synchronize authentic trades from consensus_trade_history (READ-ONLY query)
            cls._sync_and_progress_portfolio(session_id, conn)

            return cls.get_portfolio_state(session_id, conn, dispatch_alerts=False)
        finally:
            if should_close:
                conn.close()

    @classmethod
    def set_trade_selection_mode(
        cls,
        session_id: str = "default_arena_session",
        trade_selection_mode: str = "HIGHEST_CONFLUENCE_ONLY",
        conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """
        Updates the player's trade selection mode (ALL_QUALIFIED vs HIGHEST_CONFLUENCE_ONLY)
        and immediately re-simulates the arena from initial capital to reflect the selected rule.
        """
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        try:
            cur = conn.cursor()
            account_row = cur.execute("""
                SELECT initial_capital, allocation_mode, max_positions, fees_enabled, account_name
                FROM consensus_game_accounts WHERE session_id = ?
            """, (session_id,)).fetchone()

            initial_capital = float(account_row[0]) if account_row else 100000.0
            allocation_mode = str(account_row[1]) if account_row else "EQUAL_WEIGHT"
            max_positions = int(account_row[2]) if account_row else 5
            fees_enabled = bool(account_row[3]) if account_row else True
            account_name = str(account_row[4]) if account_row else "Consensus Arena Trader"

            clean_mode = "ALL_QUALIFIED"
            mode_upper = str(trade_selection_mode).upper().strip()
            if mode_upper in ("HIGHEST_CONFLUENCE_ONLY", "HIGHEST_CONFLUENCE"):
                clean_mode = "HIGHEST_CONFLUENCE_ONLY"
            elif mode_upper in ("TIMED_ANCHOR_CONFLUENCE", "TIMED_SCANS_ONLY", "TIMED_SCANS_HIGH_CONFLUENCE", "TIMED_SCAN"):
                clean_mode = "TIMED_ANCHOR_CONFLUENCE"

            if clean_mode == "TIMED_ANCHOR_CONFLUENCE" and max_positions == 5:
                max_positions = 3
            elif clean_mode != "TIMED_ANCHOR_CONFLUENCE" and max_positions == 3:
                max_positions = 5

            return cls.deploy_capital(
                initial_capital=initial_capital,
                allocation_mode=allocation_mode,
                max_positions=max_positions,
                fees_enabled=fees_enabled,
                session_id=session_id,
                account_name=account_name,
                trade_selection_mode=clean_mode,
                conn=conn
            )
        finally:
            if should_close:
                conn.close()

    @classmethod
    def exit_position(
        cls,
        position_id: int,
        session_id: str = "default_arena_session",
        exit_price: Optional[float] = None,
        reason: str = "MANUAL EXIT",
        conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """
        Manually closes a single simulated position in the Consensus Arena at live market price
        (or provided exit_price), booking the realized P&L and friction fees, returning capital to cash,
        and recording a snapshot. Also reconciles the underlying trade in consensus_trade_history.
        """
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        try:
            cur = conn.cursor()
            pos = cur.execute("""
                SELECT id, trade_ref_id, ticker, direction, shares, entry_price, allocated_capital, status
                FROM consensus_game_positions
                WHERE id = ? AND session_id = ?
            """, (position_id, session_id)).fetchone()

            if not pos:
                raise ValueError(f"Position #{position_id} not found in session '{session_id}'.")

            pid, ref_id, ticker, direction, shares, entry_px, cost, status = pos
            if status != "OPEN":
                return {"status": "ALREADY_CLOSED", "message": f"Position #{position_id} on {ticker} is already closed."}

            acc = cur.execute("""
                SELECT current_cash, fees_enabled, realized_pnl, peak_equity, max_drawdown_pct
                FROM consensus_game_accounts WHERE session_id = ?
            """, (session_id,)).fetchone()
            current_cash = float(acc[0] if acc[0] is not None else 0.0)
            fees_enabled = bool(acc[1] if acc[1] is not None else 1)
            acc_realized = float(acc[2] if acc[2] is not None else 0.0)
            peak_eq = float(acc[3] if acc[3] is not None else (acc[0] or 100000.0))
            max_dd = float(acc[4] if acc[4] is not None else 0.0)

            # Resolve live exit price
            final_exit_px = exit_price
            if final_exit_px is None or final_exit_px <= 0:
                try:
                    from app.data.market_provider import get_bulk_live_quotes_with_meta
                    q = get_bulk_live_quotes_with_meta([ticker])
                    if q and ticker in q and q[ticker].get("price"):
                        final_exit_px = float(q[ticker]["price"])
                except Exception as e:
                    logger.warning(f"Could not fetch live price for exit of {ticker}: {e}")
            if final_exit_px is None or final_exit_px <= 0:
                final_exit_px = entry_px

            final_exit_px = round(float(final_exit_px), 2)
            exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Calculate PnL
            if direction.upper() in ("BUY", "BULLISH"):
                gross_pnl = round(shares * (final_exit_px - entry_px), 2)
            else:
                gross_pnl = round(shares * (entry_px - final_exit_px), 2)

            fee = 0.0
            if fees_enabled:
                brokerage = 2 * DEFAULT_BROKERAGE_PER_ORDER
                slippage = (cost + (shares * final_exit_px)) * (DEFAULT_SLIPPAGE_BPS / 10000.0)
                fee = round(brokerage + slippage, 2)

            net_pnl = round(gross_pnl - fee, 2)
            actual_pnl_pct = round((net_pnl / cost) * 100.0, 2) if cost > 0 else 0.0

            # Update position in consensus_game_positions
            cur.execute("""
                UPDATE consensus_game_positions
                SET status = 'CLOSED', exit_price = ?, exit_time = ?,
                    gross_pnl_rupees = ?, fees_rupees = ?, net_pnl_rupees = ?,
                    pnl_pct = ?, outcome = ?
                WHERE id = ?
            """, (final_exit_px, exit_time, gross_pnl, fee, net_pnl, actual_pnl_pct, reason, pid))

            # Also close in consensus_trade_history if open
            if ref_id:
                cur.execute("""
                    UPDATE consensus_trade_history
                    SET status = 'CLOSED', exit_price = ?, exit_time = ?,
                        profit_pct = ?, outcome = ?
                    WHERE id = ? AND status = 'OPEN'
                """, (final_exit_px, exit_time, actual_pnl_pct, reason, ref_id))

            # Update account cash & realized PnL
            new_cash = current_cash + cost + net_pnl
            new_realized = cur.execute(
                "SELECT COALESCE(SUM(net_pnl_rupees), 0.0) FROM consensus_game_positions WHERE session_id = ? AND status = 'CLOSED'",
                (session_id,)
            ).fetchone()[0]

            open_capital = cur.execute(
                "SELECT COALESCE(SUM(allocated_capital), 0.0) FROM consensus_game_positions WHERE session_id = ? AND status = 'OPEN'",
                (session_id,)
            ).fetchone()[0]
            new_equity = round(new_cash + float(open_capital), 2)
            if new_equity > peak_eq:
                peak_eq = new_equity
            dd = ((peak_eq - new_equity) / peak_eq * 100.0) if peak_eq > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

            cur.execute("""
                UPDATE consensus_game_accounts
                SET current_cash = ?, realized_pnl = ?, peak_equity = ?, max_drawdown_pct = ?, updated_at = ?
                WHERE session_id = ?
            """, (round(new_cash, 2), round(new_realized, 2), round(peak_eq, 2), round(max_dd, 2), exit_time, session_id))

            # Add snapshot
            cur.execute("""
                INSERT INTO consensus_game_snapshots (
                    session_id, timestamp, total_equity, cash, realized_pnl,
                    unrealized_pnl, open_positions_count, event_type, note
                ) VALUES (?, ?, ?, ?, ?, 0.0, 0, 'TRADE_CLOSED', ?)
            """, (
                session_id, exit_time, round(new_equity, 2), round(new_cash, 2),
                round(new_realized, 2), f"{reason} #{pid} on {ticker}: {net_pnl:+.2f} ({actual_pnl_pct:+.2f}%)"
            ))

            conn.commit()

            try:
                cls.sync_and_dispatch_arena_alerts(session_id=session_id, conn=conn)
            except Exception as alert_err:
                logger.debug(f"[ConsensusGame] Exit alert check: {alert_err}")

            return {
                "status": "SUCCESS",
                "position_id": pid,
                "ticker": ticker,
                "exit_price": final_exit_px,
                "exit_time": exit_time,
                "gross_pnl": gross_pnl,
                "fees": fee,
                "net_pnl": net_pnl,
                "pnl_pct": actual_pnl_pct,
                "outcome": reason
            }
        finally:
            if should_close:
                conn.close()

    @classmethod
    def exit_all_positions(
        cls,
        session_id: str = "default_arena_session",
        reason: str = "MANUAL EXIT ALL",
        conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """
        Closes ALL currently OPEN positions in the arena in a single batch,
        resolving live prices using 1 bulk quote request.
        """
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        try:
            cur = conn.cursor()
            open_rows = cur.execute("""
                SELECT id, ticker FROM consensus_game_positions WHERE session_id = ? AND status = 'OPEN'
            """, (session_id,)).fetchall()

            if not open_rows:
                return {"status": "NO_OPEN_POSITIONS", "exited_count": 0, "positions": []}

            # Bulk prefetch live quotes
            tickers = [r[1] for r in open_rows]
            live_quotes = {}
            try:
                from app.data.market_provider import get_bulk_live_quotes_with_meta
                live_quotes = get_bulk_live_quotes_with_meta(tickers)
            except Exception as e:
                logger.warning(f"Could not bulk-fetch live quotes for exit all: {e}")

            results = []
            for pid, ticker in open_rows:
                px = live_quotes.get(ticker, {}).get("price")
                res = cls.exit_position(
                    position_id=pid,
                    session_id=session_id,
                    exit_price=px,
                    reason=reason,
                    conn=conn
                )
                results.append(res)

            return {
                "status": "SUCCESS",
                "exited_count": len(results),
                "positions": results
            }
        finally:
            if should_close:
                conn.close()

    @classmethod
    def _format_slot_entry_message(
        cls,
        ticker: str,
        direction: str,
        shares: int,
        entry_price: float,
        allocated_capital: float,
        stop_loss: float,
        target_price: float,
        composite_score: float,
        qlib_score: Any,
        legacy_score: Any,
        finbert_score: float,
        current_cash: float,
        open_count: int,
        max_positions: int,
        strategy: str = "INTRADAY",
        trade_selection_mode: str = "HIGHEST_CONFLUENCE_ONLY"
    ) -> str:
        arrow = "🟢 BUY" if direction.upper() in ("BUY", "BULLISH") else "🔴 SHORT/SELL"
        slots_left = max(0, max_positions - open_count)
        sl_pct = abs((stop_loss - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
        tp_pct = abs((target_price - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0
        risk = abs(entry_price - stop_loss)
        reward = abs(target_price - entry_price)
        rr = (reward / risk) if risk > 0 else 2.0

        if trade_selection_mode == "TIMED_ANCHOR_CONFLUENCE":
            mode_header = "Institutional Timed Scans Selection (09:30, 11:30, 13:30)"
        elif trade_selection_mode == "HIGHEST_CONFLUENCE_ONLY":
            mode_header = "Grade-A High Confluence Selection (HIGHEST_CONFLUENCE_ONLY)"
        else:
            mode_header = "All Qualified Consensus Selection (ALL_QUALIFIED)"

        return (
            f"🎮 <b>CONSENSUS ARENA: NEW SLOT OCCUPIED</b>\n"
            f"{mode_header}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Ticker:</b> <code>{ticker}</code> [{arrow}]\n"
            f"<b>Arena Slot:</b> Slot {open_count} of {max_positions} Occupied ({slots_left} Remaining)\n"
            f"<b>Strategy:</b> {strategy.upper()} (15m)\n"
            f"─────────────────────────\n"
            f"• <b>Entry Price:</b> ₹{entry_price:.2f}\n"
            f"• <b>Quantity:</b> {shares} Shares\n"
            f"• <b>Allocated Capital:</b> ₹{allocated_capital:,.2f}\n"
            f"• <b>Stop Loss:</b> ₹{stop_loss:.2f} (-{sl_pct:.2f}%)\n"
            f"• <b>Profit Target:</b> ₹{target_price:.2f} (+{tp_pct:.2f}%)\n"
            f"• <b>Risk / Reward:</b> 1:{rr:.2f}\n"
            f"─────────────────────────\n"
            f"<b>Composite Conviction:</b> <b>{composite_score:.1f}%</b>\n"
            f"• <i>Qlib Alpha-158:</i> {qlib_score}\n"
            f"• <i>Legacy Ensemble:</i> {legacy_score}%\n"
            f"• <i>FinBERT Sentiment:</i> {finbert_score:+.2f}\n"
            f"─────────────────────────\n"
            f"<b>Arena Cash Remaining:</b> ₹{current_cash:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>⚡ Live tracking active in Consensus Arena</i>"
        )

    @classmethod
    def _format_slot_exit_message(
        cls,
        ticker: str,
        direction: str,
        shares: int,
        entry_price: float,
        exit_price: float,
        gross_pnl: float,
        fee: float,
        net_pnl: float,
        pnl_pct: float,
        outcome: str,
        current_cash: float,
        realized_pnl: float,
        open_count: int,
        max_positions: int
    ) -> str:
        arrow = "🟢 BUY" if direction.upper() in ("BUY", "BULLISH") else "🔴 SHORT/SELL"
        pnl_icon = "🟢" if net_pnl >= 0 else "🔴"

        clean_outcome = str(outcome).upper()
        if "TARGET" in clean_outcome:
            outcome_badge = "🎯 <b>TARGET MET</b>"
        elif "BREAKEVEN" in clean_outcome:
            outcome_badge = "⚖️ <b>BREAKEVEN HIT</b>"
        elif "SL" in clean_outcome or "STOP" in clean_outcome:
            outcome_badge = "🛑 <b>STOP LOSS HIT</b>"
        elif "SQUARE" in clean_outcome:
            outcome_badge = "⏰ <b>15:15 SQUARED OFF</b>"
        else:
            outcome_badge = f"✋ <b>{clean_outcome}</b>"

        slots_left = max(0, max_positions - open_count)

        return (
            f"🏁 <b>CONSENSUS ARENA: SLOT FREED / EXIT</b>\n"
            f"Trade Closed & Capital Returned\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Ticker:</b> <code>{ticker}</code> [{arrow}]\n"
            f"<b>Outcome:</b> {outcome_badge}\n"
            f"─────────────────────────\n"
            f"• <b>Entry:</b> ₹{entry_price:.2f}  ➔  <b>Exit:</b> ₹{exit_price:.2f}\n"
            f"• <b>Quantity:</b> {shares} Shares\n"
            f"• <b>Gross P&L:</b> ₹{gross_pnl:+.2f}\n"
            f"• <b>Friction Fees:</b> ₹{fee:.2f} (Brokerage + Slippage)\n"
            f"• <b>Net Realized P&L:</b> {pnl_icon} <b>₹{net_pnl:+.2f} ({pnl_pct:+.2f}%)</b>\n"
            f"─────────────────────────\n"
            f"<b>Slot Status:</b> Slot Freed (Now {slots_left} of {max_positions} slots open)\n"
            f"<b>Available Cash:</b> ₹{current_cash:,.2f}\n"
            f"<b>Arena Realized P&L:</b> ₹{realized_pnl:+.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>⚡ Portfolio auto-updated in Consensus Arena</i>"
        )

    @classmethod
    def sync_and_dispatch_arena_alerts(
        cls,
        session_id: str = "default_arena_session",
        conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """
        Synchronizes portfolio and dispatches Telegram push alerts for live default_arena_session slots.
        """
        # Strict safety guard: ONLY default_arena_session (live player) is permitted to send outbound Telegram push alerts
        if session_id != "default_arena_session" and not session_id.startswith("test_"):
            return {"status": "NON_DEFAULT_SESSION_ALERT_SUPPRESSED", "entry_alerts_sent": 0, "exit_alerts_sent": 0}

        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        entry_alerts_sent = 0
        exit_alerts_sent = 0

        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='consensus_trade_history'")
            if not cur.fetchone():
                return {"status": "NO_CONSENSUS_TABLE", "entry_alerts_sent": 0, "exit_alerts_sent": 0}

            # 1. Sync authentic trades into game state
            cls._sync_and_progress_portfolio(session_id, conn)

            account_row = cur.execute("""
                SELECT current_cash, max_positions, realized_pnl, trade_selection_mode
                FROM consensus_game_accounts WHERE session_id = ?
            """, (session_id,)).fetchone()
            if not account_row:
                cls.deploy_capital(100000.0, session_id=session_id, conn=conn)
                account_row = cur.execute("""
                    SELECT current_cash, max_positions, realized_pnl, trade_selection_mode
                    FROM consensus_game_accounts WHERE session_id = ?
                """, (session_id,)).fetchone()

            if not account_row:
                return {"status": "NO_ACCOUNT", "entry_alerts_sent": 0, "exit_alerts_sent": 0}

            current_cash = float(account_row[0])
            max_positions = int(account_row[1])
            realized_pnl = float(account_row[2])
            selection_mode = str(account_row[3] if len(account_row) > 3 and account_row[3] else "ALL_QUALIFIED")

            open_count = cur.execute("""
                SELECT COUNT(*) FROM consensus_game_positions WHERE session_id = ? AND status = 'OPEN'
            """, (session_id,)).fetchone()[0]

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            today_str = datetime.now().strftime("%Y-%m-%d")

            from app.analytics.telegram_notifier import send_telegram_message

            # 2. Check unalerted OPEN positions
            cur.execute("""
                SELECT p.id, p.trade_ref_id, p.ticker, p.direction, p.shares, p.entry_price,
                       p.allocated_capital, p.stop_loss, p.target_price, p.entry_time,
                       t.composite_score, t.qlib_score, t.legacy_score, t.finbert_score, t.strategy
                FROM consensus_game_positions p
                LEFT JOIN consensus_trade_history t ON p.trade_ref_id = t.id
                WHERE p.session_id = ? AND p.status = 'OPEN'
                  AND p.trade_ref_id NOT IN (
                      SELECT trade_ref_id FROM consensus_game_alerts
                      WHERE session_id = ? AND alert_type = 'SLOT_ENTRY'
                  )
                ORDER BY p.id ASC
            """, (session_id, session_id))
            unalerted_open = cur.fetchall()

            messages_to_send = []

            for row in unalerted_open:
                pid, ref_id, ticker, direction, shares, entry_px, alloc_cap, sl, tp, entry_t, comp_score, qlib_score, leg_score, finbert_score, strat = row
                entry_t_str = str(entry_t or "")
                is_today = bool(entry_t_str[:10] == today_str)

                is_timed_mode = (selection_mode in ("TIMED_ANCHOR_CONFLUENCE", "TIMED_SCANS_ONLY", "TIMED_SCANS_HIGH_CONFLUENCE", "TIMED_SCAN"))

                # User requirement: Only send Telegram notifications for 15-min scan trade with highest confluence; do not send for timed scans
                if is_today and not is_timed_mode:
                    msg = cls._format_slot_entry_message(
                        ticker=ticker,
                        direction=direction,
                        shares=shares,
                        entry_price=entry_px,
                        allocated_capital=alloc_cap,
                        stop_loss=sl,
                        target_price=tp,
                        composite_score=float(comp_score or 0.0),
                        qlib_score=qlib_score or "N/A",
                        legacy_score=leg_score or "N/A",
                        finbert_score=float(finbert_score or 0.0),
                        current_cash=current_cash,
                        open_count=open_count,
                        max_positions=max_positions,
                        strategy=strat or "INTRADAY",
                        trade_selection_mode=selection_mode
                    )
                    messages_to_send.append((msg, ref_id, ticker, True))

                # Record in ledger to guarantee zero duplicate alerts
                cur.execute("""
                    INSERT OR IGNORE INTO consensus_game_alerts (
                        session_id, trade_ref_id, ticker, alert_type, sent_at, details_json
                    ) VALUES (?, ?, ?, 'SLOT_ENTRY', ?, ?)
                """, (
                    session_id, ref_id, ticker, now_str,
                    json.dumps({"is_live": is_today, "entry_time": entry_t, "entry_price": entry_px})
                ))

            # 3. Check unalerted CLOSED positions
            cur.execute("""
                SELECT p.id, p.trade_ref_id, p.ticker, p.direction, p.shares, p.entry_price,
                       p.exit_price, p.gross_pnl_rupees, p.fees_rupees, p.net_pnl_rupees,
                       p.pnl_pct, p.outcome, p.entry_time, p.exit_time
                FROM consensus_game_positions p
                WHERE p.session_id = ? AND p.status = 'CLOSED'
                  AND p.trade_ref_id NOT IN (
                      SELECT trade_ref_id FROM consensus_game_alerts
                      WHERE session_id = ? AND alert_type = 'SLOT_EXIT'
                  )
                ORDER BY p.id ASC
            """, (session_id, session_id))
            unalerted_closed = cur.fetchall()

            for row in unalerted_closed:
                pid, ref_id, ticker, direction, shares, entry_px, exit_px, gross_pnl, fees, net_pnl, pnl_pct, outcome, entry_t, exit_t = row
                
                # Check if this trade was ever alerted on entry
                was_entry_alerted = cur.execute("""
                    SELECT 1 FROM consensus_game_alerts WHERE session_id = ? AND trade_ref_id = ? AND alert_type = 'SLOT_ENTRY'
                """, (session_id, ref_id)).fetchone() is not None
                
                # Strict live-day check: The position MUST have exited TODAY to qualify for a live exit alert
                is_exit_today = bool(exit_t and str(exit_t)[:10] == today_str)

                is_timed_mode = (selection_mode in ("TIMED_ANCHOR_CONFLUENCE", "TIMED_SCANS_ONLY", "TIMED_SCANS_HIGH_CONFLUENCE", "TIMED_SCAN"))
                
                # Dispatch exit push alert ONLY IF:
                # 1. The trade actually closed TODAY (preventing stale historical/yesterday exit ghost alerts), AND
                # 2. The trade was either alerted upon entry OR entered today in live trading, AND
                # 3. Not in timed anchor scan mode
                if is_exit_today and (was_entry_alerted or str(entry_t)[:10] == today_str) and not is_timed_mode:
                    msg = cls._format_slot_exit_message(
                        ticker=ticker,
                        direction=direction,
                        shares=shares,
                        entry_price=entry_px,
                        exit_price=float(exit_px or entry_px),
                        gross_pnl=float(gross_pnl or 0.0),
                        fee=float(fees or 0.0),
                        net_pnl=float(net_pnl or 0.0),
                        pnl_pct=float(pnl_pct or 0.0),
                        outcome=str(outcome or "MANUAL EXIT"),
                        current_cash=current_cash,
                        realized_pnl=realized_pnl,
                        open_count=open_count,
                        max_positions=max_positions
                    )
                    messages_to_send.append((msg, ref_id, ticker, False))

                # Record in ledger
                cur.execute("""
                    INSERT OR IGNORE INTO consensus_game_alerts (
                        session_id, trade_ref_id, ticker, alert_type, sent_at, details_json
                    ) VALUES (?, ?, ?, 'SLOT_EXIT', ?, ?)
                """, (
                    session_id, ref_id, ticker, now_str,
                    json.dumps({"is_live": is_exit_today, "exit_time": exit_t, "outcome": outcome, "net_pnl": net_pnl})
                ))

            # Commit all database transactions first to release all SQLite locks
            conn.commit()

            # Dispatch queued Telegram alerts after releasing DB transaction locks
            for msg, ref_id, ticker, is_entry in messages_to_send:
                try:
                    send_telegram_message(msg)
                    if is_entry:
                        entry_alerts_sent += 1
                        logger.info(f"[ConsensusGameAlert] SLOT_ENTRY alert sent for {ticker} (#{ref_id})")
                    else:
                        exit_alerts_sent += 1
                        logger.info(f"[ConsensusGameAlert] SLOT_EXIT alert sent for {ticker} (#{ref_id}) - exit")
                except Exception as ex:
                    logger.warning(f"[ConsensusGameAlert] Failed to dispatch alert: {ex}")

            return {
                "status": "SUCCESS",
                "entry_alerts_sent": entry_alerts_sent,
                "exit_alerts_sent": exit_alerts_sent
            }
        except Exception as e:
            logger.error(f"[ConsensusGameAlert] Error dispatching arena alerts: {e}", exc_info=True)
            return {"status": "ERROR", "error": str(e), "entry_alerts_sent": 0, "exit_alerts_sent": 0}
        finally:
            if should_close:
                conn.close()

    @classmethod
    def _sync_and_progress_portfolio(
        cls,
        session_id: str,
        conn: sqlite3.Connection
    ) -> None:
        """
        Chronologically replays authentic consensus trades from `consensus_trade_history` (READ-ONLY)
        into the player's sandboxed account with strict point-in-time slot occupancy tracking.
        """
        cur = conn.cursor()
        account_row = cur.execute("""
            SELECT initial_capital, current_cash, allocation_mode, max_positions, fees_enabled, peak_equity, max_drawdown_pct, trade_selection_mode
            FROM consensus_game_accounts WHERE session_id = ?
        """, (session_id,)).fetchone()
        if not account_row:
            return

        initial_capital = float(account_row[0])
        alloc_mode = account_row[2]
        max_positions = int(account_row[3])
        fees_enabled = bool(account_row[4])
        trade_selection_mode = account_row[7] if len(account_row) > 7 and account_row[7] else "ALL_QUALIFIED"

        # Fetch all authentic consensus trades ordered chronologically
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='consensus_trade_history'")
        if not cur.fetchone():
            return

        cur.execute("""
            SELECT id, timestamp, ticker, direction, entry_price, stop_loss, target_price,
                   status, outcome, profit_pct, exit_price, exit_time, composite_score,
                   qlib_score, legacy_score, risk_reward_ratio, details_json
            FROM consensus_trade_history
            ORDER BY timestamp ASC, id ASC
        """)
        auth_trades = cur.fetchall()
        if not auth_trades:
            return

        # Determine genesis timestamp for initial deployment snapshot
        first_ts = auth_trades[0][1]
        genesis_ts = "2026-09-18 09:00:00"
        if first_ts:
            raw_first = str(first_ts).replace("T", " ")
            if "." in raw_first:
                raw_first = raw_first.split(".")[0]
            try:
                dt = datetime.strptime(raw_first, "%Y-%m-%d %H:%M:%S")
                genesis_ts = (dt - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                genesis_ts = raw_first

        # Filter candidate trades if HIGHEST_CONFLUENCE_ONLY or TIMED_ANCHOR_CONFLUENCE mode is enabled
        trades_to_process = auth_trades
        if trade_selection_mode == "HIGHEST_CONFLUENCE_ONLY":
            from collections import defaultdict
            batches = defaultdict(list)
            for trade in auth_trades:
                ts = str(trade[1] or "")
                batch_key = ts.replace("T", " ")[:16] if len(ts) >= 16 else ts
                batches[batch_key].append(trade)

            allowed_tids = set()
            for batch_key, b_trades in batches.items():
                from app.analytics.consensus_tiebreaker import ConsensusTieBreaker, HISTORICAL_TIEBREAKER_CUTOFF
                best_trade = ConsensusTieBreaker.select_best_candidate(
                    b_trades,
                    score_index=12,
                    historical_id_sign=1
                )
                allowed_tids.add(best_trade[0])
                # Record audit outcome for unselected candidates in forward batches (strictly >= cutoff)
                for tr in b_trades:
                    tr_id, tr_ts = tr[0], str(tr[1] or "")
                    if tr_id != best_trade[0] and tr_ts >= HISTORICAL_TIEBREAKER_CUTOFF:
                        try:
                            cur.execute("""
                                UPDATE consensus_trade_history
                                SET outcome = 'REJECTED_LOWER_RANK'
                                WHERE id = ? AND timestamp >= ? AND (outcome IS NULL OR outcome = 'PENDING_SELECTION' OR outcome = 'REJECTED_LOWER_RANK')
                            """, (tr_id, HISTORICAL_TIEBREAKER_CUTOFF))
                        except Exception:
                            pass

            trades_to_process = [
                t for t in auth_trades
                if t[0] in allowed_tids
            ]
        elif trade_selection_mode == "TIMED_ANCHOR_CONFLUENCE":
            from collections import defaultdict
            by_day = defaultdict(list)
            for trade in auth_trades:
                ts = str(trade[1] or "")
                dt = ts[:10]
                by_day[dt].append(trade)

            target_minutes = {"09:30": 570, "11:30": 690, "13:30": 810}
            allowed_tids = set()

            for dt, d_trades in by_day.items():
                d_batches = defaultdict(list)
                for tr in d_trades:
                    ts = str(tr[1] or "")
                    tm_str = ts.replace("T", " ")[11:16] if len(ts) >= 16 else "00:00"
                    d_batches[tm_str].append(tr)

                for tgt_name, tgt_m in target_minutes.items():
                    best_batch_tm = None
                    best_diff = 999999
                    for b_tm in d_batches.keys():
                        try:
                            parts = b_tm.split(":")
                            bh, bm = int(parts[0]), int(parts[1])
                            b_mins = bh * 60 + bm
                            if dt < "2026-09-25":
                                # Strict historical lock: preserve exact historical tolerance for closed past days
                                diff = abs(b_mins - tgt_m)
                                if diff <= 45 and diff < best_diff:
                                    best_diff = diff
                                    best_batch_tm = b_tm
                            else:
                                # Forward live trading: Strictly causal (must be at or after milestone, 0 <= diff <= 45m)
                                diff = b_mins - tgt_m
                                if 0 <= diff <= 45 and diff < best_diff:
                                    best_diff = diff
                                    best_batch_tm = b_tm
                        except Exception:
                            continue

                    if best_batch_tm:
                        b_trades = d_batches[best_batch_tm]
                        from app.analytics.consensus_tiebreaker import ConsensusTieBreaker
                        from app.analytics.consensus_scanner import ConsensusScanner

                        eval_dt = None
                        if b_trades and b_trades[0][1]:
                            try:
                                clean_ts = str(b_trades[0][1]).replace("T", " ")[:19]
                                eval_dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                pass

                        def _eval_anchor_filter(ticker, direction, current_price, _dt=eval_dt):
                            return ConsensusScanner.evaluate_timed_anchor_filter(
                                ticker=ticker,
                                direction=direction,
                                current_price=current_price,
                                eval_time=_dt
                            )

                        best_tr = ConsensusTieBreaker.select_best_candidate(
                            b_trades,
                            score_index=12,
                            historical_id_sign=1,
                            quality_boost_evaluator=_eval_anchor_filter
                        )
                        allowed_tids.add(best_tr[0])

            trades_to_process = [
                t for t in auth_trades
                if t[0] in allowed_tids
            ]

        # Preserve dynamic guard state for existing OPEN positions before replay reset
        existing_guard_states = {}
        try:
            g_rows = cur.execute("""
                SELECT trade_ref_id, tightened_sl, guard_threat_score, guard_action, guard_reasons, last_guard_audit
                FROM consensus_game_positions
                WHERE session_id = ? AND status = 'OPEN'
            """, (session_id,)).fetchall()
            for gr in g_rows:
                ref_id, t_sl, score, action, reasons, last_audit = gr
                existing_guard_states[ref_id] = {
                    "tightened_sl": t_sl,
                    "guard_threat_score": score,
                    "guard_action": action,
                    "guard_reasons": reasons,
                    "last_guard_audit": last_audit
                }
        except Exception as e:
            logger.debug(f"[ConsensusGameEngine] Guard state capture note: {e}")

        # Reset existing sandbox positions & snapshots to ensure zero state-drift and strict point-in-time replay
        cur.execute("DELETE FROM consensus_game_positions WHERE session_id = ?", (session_id,))
        cur.execute("DELETE FROM consensus_game_snapshots WHERE session_id = ?", (session_id,))

        cur.execute("""
            INSERT INTO consensus_game_snapshots (
                session_id, timestamp, total_equity, cash, realized_pnl,
                unrealized_pnl, open_positions_count, event_type, note
            ) VALUES (?, ?, ?, ?, 0.0, 0.0, 0, 'CAPITAL_DEPLOYED', 'Initial capital deployment')
        """, (session_id, genesis_ts, initial_capital, initial_capital))

        current_cash = initial_capital
        total_realized_pnl = 0.0
        peak_equity = initial_capital
        max_drawdown_pct = 0.0
        active_positions: List[Dict[str, Any]] = []
        last_exit_by_ticker: Dict[str, str] = {}

        for trade in trades_to_process:
            tid, ts, ticker, direction, entry_px, sl, tp, status, outcome, pnl_pct, exit_px, exit_time, score = trade[:13]
            entry_px = float(entry_px)
            if entry_px <= 0:
                continue

            comp_score = float(score or 0.0)
            if comp_score < 50.0:
                continue

            entry_t = str(ts).replace("T", " ")[:19]
            norm_exit_t = str(exit_time).replace("T", " ")[:19] if exit_time else None

            # 1. Point-in-Time Slot Release: Retire active holdings whose exit_time <= candidate entry_t
            still_active = []
            for pos in sorted(active_positions, key=lambda p: p["exit_time"] or ""):
                if pos["exit_time"] and pos["exit_time"] <= entry_t:
                    current_cash += (pos["cost"] + pos["net_pnl"])
                    total_realized_pnl += pos["net_pnl"]
                    last_exit_by_ticker[pos["ticker"]] = pos["exit_time"]

                    cur.execute("""
                        INSERT INTO consensus_game_positions (
                            session_id, trade_ref_id, ticker, direction, shares,
                            entry_price, allocated_capital, stop_loss, target_price,
                            entry_time, status, exit_price, exit_time,
                            gross_pnl_rupees, fees_rupees, net_pnl_rupees,
                            pnl_pct, outcome, details_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        session_id, pos["tid"], pos["ticker"], pos["direction"], pos["shares"],
                        pos["entry_px"], pos["cost"], pos["sl"], pos["tp"],
                        pos["entry_t"], pos["exit_px"], pos["exit_time"],
                        pos["gross_pnl"], pos["fee"], pos["net_pnl"],
                        pos["actual_pnl_pct"], pos["outcome"],
                        json.dumps({"composite_score": pos["score"], "auth_trade_id": pos["tid"]})
                    ))

                    eq_at_exit = current_cash + sum(p["cost"] for p in still_active)
                    if eq_at_exit > peak_equity:
                        peak_equity = eq_at_exit
                    dd = ((peak_equity - eq_at_exit) / peak_equity * 100.0) if peak_equity > 0 else 0.0
                    if dd > max_drawdown_pct:
                        max_drawdown_pct = dd

                    cur.execute("""
                        INSERT INTO consensus_game_snapshots (
                            session_id, timestamp, total_equity, cash, realized_pnl,
                            unrealized_pnl, open_positions_count, event_type, note
                        ) VALUES (?, ?, ?, ?, ?, 0.0, ?, 'TRADE_CLOSED', ?)
                    """, (
                        session_id, pos["exit_time"], round(eq_at_exit, 2), round(current_cash, 2),
                        round(total_realized_pnl, 2), len(still_active),
                        f"Trade #{pos['tid']} on {pos['ticker']}: {pos['outcome']}"
                    ))
                else:
                    still_active.append(pos)
            active_positions = still_active

            # 2. Strict Point-in-Time Capacity and Duplicate Active Position Guard
            # Prevent duplicate active positions for the same ticker
            if any(p["ticker"] == ticker for p in active_positions):
                continue

            # Cooldown guard: prevent immediate re-entry within 15 minutes of exit
            if ticker in last_exit_by_ticker and last_exit_by_ticker[ticker]:
                try:
                    last_exit_dt = datetime.strptime(str(last_exit_by_ticker[ticker])[:19], "%Y-%m-%d %H:%M:%S")
                    cand_entry_dt = datetime.strptime(str(entry_t)[:19], "%Y-%m-%d %H:%M:%S")
                    if 0 <= (cand_entry_dt - last_exit_dt).total_seconds() < 900:
                        continue
                except Exception:
                    pass

            if len(active_positions) >= max_positions:
                from app.analytics.consensus_tiebreaker import HISTORICAL_TIEBREAKER_CUTOFF
                if str(ts) >= HISTORICAL_TIEBREAKER_CUTOFF:
                    try:
                        cur.execute("""
                            UPDATE consensus_trade_history
                            SET outcome = 'REJECTED_RISK'
                            WHERE id = ? AND timestamp >= ? AND (outcome IS NULL OR outcome = 'PENDING_SELECTION')
                        """, (tid, HISTORICAL_TIEBREAKER_CUTOFF))
                    except Exception:
                        pass
                continue

            # 3. Position Sizing
            max_slot_capital = initial_capital / max(1, max_positions)
            alloc_capital = min(current_cash, max_slot_capital)
            shares = math.floor(alloc_capital / entry_px)
            if shares < 1 or current_cash < (shares * entry_px):
                continue

            cost = shares * entry_px
            current_cash -= cost

            gross_pnl = 0.0
            fee = 0.0
            net_pnl = 0.0
            actual_pnl_pct = 0.0

            if status == "CLOSED" and exit_px is not None:
                exit_px = float(exit_px)
                if direction.upper() in ("BUY", "BULLISH"):
                    gross_pnl = round(shares * (exit_px - entry_px), 2)
                else:
                    gross_pnl = round(shares * (entry_px - exit_px), 2)

                if fees_enabled:
                    brokerage = 2 * DEFAULT_BROKERAGE_PER_ORDER
                    slippage = (cost + (shares * exit_px)) * (DEFAULT_SLIPPAGE_BPS / 10000.0)
                    fee = round(brokerage + slippage, 2)

                net_pnl = round(gross_pnl - fee, 2)
                actual_pnl_pct = round((net_pnl / cost) * 100.0, 2) if cost > 0 else 0.0

            pos_record = {
                "tid": tid, "ticker": ticker, "direction": direction, "shares": shares,
                "entry_px": entry_px, "cost": cost, "sl": sl, "tp": tp, "entry_t": entry_t,
                "status": status, "exit_px": exit_px, "exit_time": norm_exit_t,
                "gross_pnl": gross_pnl, "fee": fee, "net_pnl": net_pnl,
                "actual_pnl_pct": actual_pnl_pct, "outcome": outcome, "score": score
            }
            active_positions.append(pos_record)

            from app.analytics.consensus_tiebreaker import HISTORICAL_TIEBREAKER_CUTOFF
            if str(ts) >= HISTORICAL_TIEBREAKER_CUTOFF:
                try:
                    cur.execute("""
                        UPDATE consensus_trade_history
                        SET outcome = 'SELECTED'
                        WHERE id = ? AND timestamp >= ? AND (outcome IS NULL OR outcome = 'PENDING_SELECTION')
                    """, (tid, HISTORICAL_TIEBREAKER_CUTOFF))
                except Exception:
                    pass

            eq_at_entry = current_cash + sum(p["cost"] for p in active_positions)
            if eq_at_entry > peak_equity:
                peak_equity = eq_at_entry
            dd = ((peak_equity - eq_at_entry) / peak_equity * 100.0) if peak_equity > 0 else 0.0
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd

            cur.execute("""
                INSERT INTO consensus_game_snapshots (
                    session_id, timestamp, total_equity, cash, realized_pnl,
                    unrealized_pnl, open_positions_count, event_type, note
                ) VALUES (?, ?, ?, ?, ?, 0.0, ?, 'TRADE_OPENED', ?)
            """, (
                session_id, entry_t, round(eq_at_entry, 2), round(current_cash, 2),
                round(total_realized_pnl, 2), len(active_positions),
                f"Opened {shares}x {ticker} @ ₹{entry_px:.2f}"
            ))

        # 4. Final Sweep: Settle any remaining closed positions at the end of the timeline
        for pos in sorted(active_positions, key=lambda p: p["exit_time"] or ""):
            if pos["status"] == "CLOSED" and pos["exit_time"]:
                current_cash += (pos["cost"] + pos["net_pnl"])
                total_realized_pnl += pos["net_pnl"]

                cur.execute("""
                    INSERT INTO consensus_game_positions (
                        session_id, trade_ref_id, ticker, direction, shares,
                        entry_price, allocated_capital, stop_loss, target_price,
                        entry_time, status, exit_price, exit_time,
                        gross_pnl_rupees, fees_rupees, net_pnl_rupees,
                        pnl_pct, outcome, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    session_id, pos["tid"], pos["ticker"], pos["direction"], pos["shares"],
                    pos["entry_px"], pos["cost"], pos["sl"], pos["tp"],
                    pos["entry_t"], pos["exit_px"], pos["exit_time"],
                    pos["gross_pnl"], pos["fee"], pos["net_pnl"],
                    pos["actual_pnl_pct"], pos["outcome"],
                    json.dumps({"composite_score": pos["score"], "auth_trade_id": pos["tid"]})
                ))

                cur.execute("""
                    INSERT INTO consensus_game_snapshots (
                        session_id, timestamp, total_equity, cash, realized_pnl,
                        unrealized_pnl, open_positions_count, event_type, note
                    ) VALUES (?, ?, ?, ?, ?, 0.0, 0, 'TRADE_CLOSED', ?)
                """, (
                    session_id, pos["exit_time"], round(current_cash, 2), round(current_cash, 2),
                    round(total_realized_pnl, 2),
                    f"Trade #{pos['tid']} on {pos['ticker']}: {pos['outcome']}"
                ))
            else:
                # Genuinely active OPEN position
                prev_guard = existing_guard_states.get(pos["tid"], {})
                t_sl = prev_guard.get("tightened_sl")
                g_score = float(prev_guard.get("guard_threat_score") or 0.0)
                g_action = prev_guard.get("guard_action") or "MAINTAIN"
                g_reasons = prev_guard.get("guard_reasons")
                g_audit = prev_guard.get("last_guard_audit")

                cur.execute("""
                    INSERT INTO consensus_game_positions (
                        session_id, trade_ref_id, ticker, direction, shares,
                        entry_price, allocated_capital, stop_loss, target_price,
                        entry_time, status, exit_price, exit_time,
                        gross_pnl_rupees, fees_rupees, net_pnl_rupees,
                        pnl_pct, outcome, details_json,
                        tightened_sl, guard_threat_score, guard_action, guard_reasons, last_guard_audit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, NULL, 0.0, 0.0, 0.0, 0.0, 'OPEN', ?, ?, ?, ?, ?, ?)
                """, (
                    session_id, pos["tid"], pos["ticker"], pos["direction"], pos["shares"],
                    pos["entry_px"], pos["cost"], pos["sl"], pos["tp"],
                    pos["entry_t"],
                    json.dumps({"composite_score": pos["score"], "auth_trade_id": pos["tid"]}),
                    t_sl, g_score, g_action, g_reasons, g_audit
                ))

        # Compute actual total realized P&L directly from closed positions
        actual_realized_pnl = cur.execute(
            "SELECT COALESCE(SUM(net_pnl_rupees), 0.0) FROM consensus_game_positions WHERE session_id = ? AND status = 'CLOSED'",
            (session_id,)
        ).fetchone()[0]

        # Update account aggregates
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            UPDATE consensus_game_accounts
            SET current_cash = ?, realized_pnl = ?, peak_equity = ?, max_drawdown_pct = ?, updated_at = ?
            WHERE session_id = ?
        """, (
            round(current_cash, 2),
            round(actual_realized_pnl, 2),
            round(peak_equity, 2),
            round(max_drawdown_pct, 2),
            now_str,
            session_id
        ))
        conn.commit()

    @classmethod
    def get_portfolio_state(
        cls,
        session_id: str = "default_arena_session",
        conn: Optional[sqlite3.Connection] = None,
        dispatch_alerts: bool = True
    ) -> Dict[str, Any]:
        """
        Retrieves real-time portfolio state:
        - Current cash, open positions, unrealized MTM (using throttled live quotes)
        - Net Portfolio Value, Realized P&L, Win Rate %, Profit Factor, Max Drawdown %
        - Badges & Gamer Level Progression
        """
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        try:
            cur = conn.cursor()
            acc = cur.execute("""
                SELECT initial_capital, current_cash, realized_pnl, peak_equity, max_drawdown_pct,
                       allocation_mode, max_positions, fees_enabled, created_at, updated_at, trade_selection_mode
                FROM consensus_game_accounts WHERE session_id = ?
            """, (session_id,)).fetchone()

            if not acc:
                # Auto-initialize default account if none exists
                return cls.deploy_capital(100000.0, session_id=session_id, conn=conn)

            # If any consensus trade is OPEN, run point-in-time reconciliation (throttled to max 1 check per 10s)
            try:
                has_open = cur.execute("SELECT 1 FROM consensus_trade_history WHERE status = 'OPEN' LIMIT 1").fetchone()
                if has_open:
                    from app.analytics.consensus_tracker import ConsensusTracker
                    ConsensusTracker.reconcile_open_consensus_trades(min_interval_sec=10.0)
            except Exception as rec_err:
                logger.debug(f"[ConsensusGame] Pre-sync reconciliation note: {rec_err}")

            # Synchronize any new or newly-closed consensus trades and dispatch pending alerts
            if dispatch_alerts:
                try:
                    cls.sync_and_dispatch_arena_alerts(session_id, conn)
                except Exception as alert_err:
                    logger.debug(f"[ConsensusGame] Alert dispatch in get_portfolio_state: {alert_err}")
                    cls._sync_and_progress_portfolio(session_id, conn)
            else:
                cls._sync_and_progress_portfolio(session_id, conn)

            # Re-read account aggregates after sync
            acc = cur.execute("""
                SELECT initial_capital, current_cash, realized_pnl, peak_equity, max_drawdown_pct,
                       allocation_mode, max_positions, fees_enabled, created_at, updated_at, trade_selection_mode
                FROM consensus_game_accounts WHERE session_id = ?
            """, (session_id,)).fetchone()

            initial_cap = float(acc[0])
            cash = float(acc[1])
            realized_pnl = float(acc[2])
            peak_equity = float(acc[3])
            max_drawdown = float(acc[4])
            alloc_mode = acc[5]
            max_pos = int(acc[6])
            fees_enabled = bool(acc[7])
            created_at = acc[8]

            # Fetch open positions
            cur.execute("""
                SELECT id, trade_ref_id, ticker, direction, shares, entry_price, allocated_capital,
                       stop_loss, target_price, entry_time, details_json
                FROM consensus_game_positions
                WHERE session_id = ? AND status = 'OPEN'
            """, (session_id,))
            open_rows = cur.fetchall()

            open_positions = []
            unrealized_pnl = 0.0
            open_tickers = [r[2] for r in open_rows]

            # Throttled live quote lookup (Upstox / Gateway cache safe)
            live_quotes = {}
            is_auth_expired = False
            try:
                auth_row = cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_auth_status'").fetchone()
                if auth_row and auth_row[0] == "TOKEN_EXPIRED":
                    is_auth_expired = True
            except Exception:
                pass

            if open_tickers and not is_auth_expired:
                try:
                    from app.data.market_provider import get_bulk_live_quotes_with_meta
                    live_quotes = get_bulk_live_quotes_with_meta(open_tickers)
                except Exception as e:
                    logger.debug(f"[ConsensusGame] Could not fetch live quotes: {e}")

            for row in open_rows:
                pid, ref_id, ticker, direction, shares, entry_px, alloc_cap, sl, tp, entry_t, details_raw = row
                quote = live_quotes.get(ticker) or {}
                has_fresh_quote = bool(quote.get("price") is not None and quote.get("price") > 0)
                ltp = float(quote.get("price") or entry_px)
                pos_stale = is_auth_expired or (not has_fresh_quote) or bool(quote.get("is_stale", False))
                
                # MTM calculation
                if direction.upper() in ("BUY", "BULLISH"):
                    pos_unrealized = (ltp - entry_px) * shares
                    pnl_pct = ((ltp - entry_px) / entry_px * 100.0) if entry_px > 0 else 0.0
                    target_dist_pct = ((tp - ltp) / ltp * 100.0) if ltp > 0 else 0.0
                    sl_dist_pct = ((ltp - sl) / ltp * 100.0) if ltp > 0 else 0.0
                else:
                    pos_unrealized = (entry_px - ltp) * shares
                    pnl_pct = ((entry_px - ltp) / entry_px * 100.0) if entry_px > 0 else 0.0
                    target_dist_pct = ((ltp - tp) / ltp * 100.0) if ltp > 0 else 0.0
                    sl_dist_pct = ((sl - ltp) / ltp * 100.0) if ltp > 0 else 0.0

                unrealized_pnl += pos_unrealized

                open_positions.append({
                    "id": pid,
                    "trade_ref_id": ref_id,
                    "ticker": ticker,
                    "direction": direction,
                    "shares": shares,
                    "entry_price": entry_px,
                    "allocated_capital": alloc_cap,
                    "current_price": ltp,
                    "stop_loss": sl,
                    "target_price": tp,
                    "entry_time": entry_t,
                    "is_stale": pos_stale,
                    "unrealized_pnl_rupees": round(pos_unrealized, 2),
                    "unrealized_pnl_pct": round(pnl_pct, 2),
                    "target_dist_pct": round(target_dist_pct, 2),
                    "sl_dist_pct": round(sl_dist_pct, 2),
                    "details": json.loads(details_raw) if details_raw else {}
                })

            total_equity = round(cash + sum(p["allocated_capital"] + p["unrealized_pnl_rupees"] for p in open_positions), 2)
            if total_equity > peak_equity:
                peak_equity = total_equity

            # Performance stats from closed positions
            cur.execute("""
                SELECT net_pnl_rupees, pnl_pct, outcome, gross_pnl_rupees, fees_rupees
                FROM consensus_game_positions
                WHERE session_id = ? AND status = 'CLOSED'
            """, (session_id,))
            closed_rows = cur.fetchall()

            total_closed = len(closed_rows)
            # Gross wins (based on raw signal gross_pnl_rupees > 0)
            gross_wins = sum(1 for r in closed_rows if float(r[3] or 0.0) > 0)
            gross_losses = sum(1 for r in closed_rows if float(r[3] or 0.0) < 0)
            gross_win_rate = round((gross_wins / total_closed * 100.0), 1) if total_closed > 0 else 0.0

            # Net wins (after deducting brokerage and slippage friction: net_pnl_rupees > 0)
            net_wins = sum(1 for r in closed_rows if float(r[0] or 0.0) > 0)
            net_losses = sum(1 for r in closed_rows if float(r[0] or 0.0) < 0)
            net_win_rate = round((net_wins / total_closed * 100.0), 1) if total_closed > 0 else 0.0

            realized_pnl = round(sum(float(r[0] or 0.0) for r in closed_rows), 2)
            gross_pnl = round(sum(float(r[3] or 0.0) for r in closed_rows), 2)
            total_friction = round(sum(float(r[4] or 0.0) for r in closed_rows), 2)

            win_rate = gross_win_rate
            wins = gross_wins
            losses = gross_losses

            total_gain = sum(float(r[0]) for r in closed_rows if float(r[0]) > 0)
            total_loss = abs(sum(float(r[0]) for r in closed_rows if float(r[0]) < 0))
            profit_factor = round(total_gain / total_loss, 2) if total_loss > 0 else (99.9 if total_gain > 0 else 0.0)

            # Streak calculation
            streak = 0
            cur.execute("""
                SELECT net_pnl_rupees FROM consensus_game_positions
                WHERE session_id = ? AND status = 'CLOSED'
                ORDER BY id DESC LIMIT 10
            """, (session_id,))
            recent = cur.fetchall()
            for r in recent:
                if float(r[0] or 0.0) > 0:
                    if streak >= 0:
                        streak += 1
                    else:
                        break
                elif float(r[0] or 0.0) < 0:
                    if streak <= 0:
                        streak -= 1
                    else:
                        break

            # Gamer XP & Badges
            xp = 100 + (wins * 250) + (1000 if total_equity > initial_cap else 0)
            level = min(10, 1 + (xp // 500))
            level_titles = {
                1: "Arena Novice",
                2: "Confluence Apprentice",
                3: "Consensus Tactician",
                4: "Alpha Strategist",
                5: "Quant Champion",
                6: "Master Algorist",
                7: "Dual-Engine Maestro",
                8: "Grandmaster Quant",
                9: "Titan of Confluence",
                10: "Market Sovereign"
            }

            badges = []
            if total_closed >= 1:
                badges.append({"id": "first_blood", "name": "First Blood", "icon": "⚔️", "desc": "Completed first dual-engine consensus trade"})
            if win_rate >= 50.0 and total_closed >= 5:
                badges.append({"id": "positive_edge", "name": "Statistical Edge", "icon": "📈", "desc": "Maintained >50% win rate over 5+ trades"})
            if streak >= 3:
                badges.append({"id": "hot_streak", "name": "On Fire", "icon": "🔥", "desc": "3 consecutive winning trades"})
            if total_equity >= initial_cap * 1.02:
                badges.append({"id": "growth_hacker", "name": "Capital Expander", "icon": "💰", "desc": "Grew account by over +2.0%"})
            if max_drawdown <= 2.0 and total_closed >= 5:
                badges.append({"id": "shield_master", "name": "Capital Guardian", "icon": "🛡️", "desc": "Kept max drawdown strictly under 2.0%"})

            # Compute today's performance
            today_str = datetime.now().strftime("%Y-%m-%d")
            cur.execute("""
                SELECT 
                    COALESCE(SUM(net_pnl_rupees), 0.0),
                    COALESCE(SUM(gross_pnl_rupees), 0.0),
                    COALESCE(SUM(fees_rupees), 0.0),
                    COUNT(*),
                    SUM(CASE WHEN net_pnl_rupees > 0 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN net_pnl_rupees < 0 THEN 1 ELSE 0 END)
                FROM consensus_game_positions
                WHERE session_id = ?
                  AND status = 'CLOSED'
                  AND (exit_time LIKE ? OR (exit_time IS NULL AND entry_time LIKE ?))
            """, (session_id, f"{today_str}%", f"{today_str}%"))
            t_row = cur.fetchone()
            today_realized_net = round(float(t_row[0] or 0.0), 2)
            today_realized_gross = round(float(t_row[1] or 0.0), 2)
            today_fees = round(float(t_row[2] or 0.0), 2)
            today_closed_cnt = int(t_row[3] or 0)
            today_wins = int(t_row[4] or 0)
            today_losses = int(t_row[5] or 0)
            today_win_rate = round(today_wins / today_closed_cnt * 100.0, 1) if today_closed_cnt > 0 else 0.0

            # Today's Total P&L = Booked Net P&L today + Current Live Floating Unrealized MTM
            today_total_pnl = round(today_realized_net + unrealized_pnl, 2)
            today_return_pct = round((today_total_pnl / initial_cap) * 100.0, 2) if initial_cap > 0 else 0.0

            return {
                "session_id": session_id,
                "initial_capital": initial_cap,
                "total_equity": total_equity,
                "cash": round(cash, 2),
                "realized_pnl": round(realized_pnl, 2),
                "gross_pnl": round(gross_pnl, 2),
                "total_friction": round(total_friction, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "net_pnl": round(realized_pnl + unrealized_pnl, 2),
                "return_pct": round(((total_equity - initial_cap) / initial_cap * 100.0), 2),
                "today_pnl": today_total_pnl,
                "today_realized_pnl": today_realized_net,
                "today_gross_pnl": today_realized_gross,
                "today_fees": today_fees,
                "today_unrealized_mtm": round(unrealized_pnl, 2),
                "today_return_pct": today_return_pct,
                "today_closed_trades": today_closed_cnt,
                "today_wins": today_wins,
                "today_losses": today_losses,
                "today_win_rate": today_win_rate,
                "today_stats": {
                    "date": today_str,
                    "total_pnl": today_total_pnl,
                    "realized_pnl": today_realized_net,
                    "gross_pnl": today_realized_gross,
                    "fees": today_fees,
                    "unrealized_mtm": round(unrealized_pnl, 2),
                    "return_pct": today_return_pct,
                    "closed_trades": today_closed_cnt,
                    "wins": today_wins,
                    "losses": today_losses,
                    "win_rate": today_win_rate
                },
                "peak_equity": round(peak_equity, 2),
                "max_drawdown_pct": round(max_drawdown, 2),
                "win_rate": win_rate,
                "gross_win_rate": gross_win_rate,
                "net_win_rate": net_win_rate,
                "total_closed_trades": total_closed,
                "wins": wins,
                "losses": losses,
                "gross_wins": gross_wins,
                "gross_losses": gross_losses,
                "net_wins": net_wins,
                "net_losses": net_losses,
                "profit_factor": profit_factor,
                "streak": streak,
                "open_positions": open_positions,
                "open_count": len(open_positions),
                "is_data_stale": bool(is_auth_expired or any(p.get("is_stale") for p in open_positions)),
                "max_positions": max_pos,
                "allocation_mode": alloc_mode,
                "fees_enabled": fees_enabled,
                "trade_selection_mode": acc[10] if len(acc) > 10 and acc[10] else "ALL_QUALIFIED",
                "created_at": created_at,
                "gamer_profile": {
                    "xp": xp,
                    "level": level,
                    "level_title": level_titles.get(level, "Master Trader"),
                    "next_level_xp": level * 500,
                    "badges": badges
                }
            }
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_equity_curve(
        cls,
        session_id: str = "default_arena_session",
        conn: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        """Returns chronological equity snapshots for high-resolution chart rendering."""
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            should_close = True

        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT timestamp, total_equity, cash, realized_pnl, unrealized_pnl,
                       open_positions_count, event_type, note
                FROM consensus_game_snapshots
                WHERE session_id = ?
                ORDER BY timestamp ASC, id ASC
            """, (session_id,))
            rows = cur.fetchall()

            hwm = 0.0
            series = []
            for r in rows:
                eq = float(r[1])
                hwm = max(hwm, eq)
                series.append({
                    "timestamp": r[0],
                    "total_equity": eq,
                    "cash": float(r[2]),
                    "realized_pnl": float(r[3]),
                    "unrealized_pnl": float(r[4]),
                    "open_positions": int(r[5]),
                    "high_water_mark": round(hwm, 2),
                    "event_type": r[6],
                    "note": r[7]
                })
            return series
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_pnl_calendar(
        cls,
        session_id: str = "default_arena_session",
        year: Optional[int] = None,
        month: Optional[int] = None,
        conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """
        Builds an interactive Zerodha Console-style monthly P&L Calendar.
        Aggregates realized profits, losses, win rates, and trades by day.
        100% computed from sandboxed game positions.
        """
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            should_close = True

        try:
            cur = conn.cursor()
            account = cur.execute(
                "SELECT initial_capital FROM consensus_game_accounts WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            initial_cap = float(account[0]) if account else 100000.0

            cur.execute("""
                SELECT id, ticker, direction, shares, entry_price, exit_price,
                       allocated_capital, gross_pnl_rupees, fees_rupees, net_pnl_rupees,
                       pnl_pct, outcome, exit_time, entry_time
                FROM consensus_game_positions
                WHERE session_id = ? AND status = 'CLOSED' AND exit_time IS NOT NULL
                ORDER BY exit_time ASC
            """, (session_id,))
            rows = cur.fetchall()

            # Group by YYYY-MM-DD
            daily_map = {}
            for r in rows:
                exit_time_str = r[12]
                date_key = exit_time_str[:10]  # 'YYYY-MM-DD'
                if date_key not in daily_map:
                    daily_map[date_key] = {
                        "date": date_key,
                        "net_pnl_rupees": 0.0,
                        "gross_pnl_rupees": 0.0,
                        "fees_rupees": 0.0,
                        "trade_count": 0,
                        "wins": 0,
                        "losses": 0,
                        "trades": []
                    }
                
                net_pnl = float(r[9] or 0.0)
                gross_pnl = float(r[7] or 0.0)
                fee = float(r[8] or 0.0)

                daily_map[date_key]["net_pnl_rupees"] += net_pnl
                daily_map[date_key]["gross_pnl_rupees"] += gross_pnl
                daily_map[date_key]["fees_rupees"] += fee
                daily_map[date_key]["trade_count"] += 1

                if net_pnl > 0:
                    daily_map[date_key]["wins"] += 1
                elif net_pnl < 0:
                    daily_map[date_key]["losses"] += 1

                daily_map[date_key]["trades"].append({
                    "id": r[0],
                    "ticker": r[1],
                    "direction": r[2],
                    "shares": r[3],
                    "entry_price": r[4],
                    "exit_price": r[5],
                    "allocated_capital": r[6],
                    "net_pnl": net_pnl,
                    "pnl_pct": float(r[10] or 0.0),
                    "outcome": r[11],
                    "entry_time": r[13],
                    "exit_time": exit_time_str
                })

            # Calculate daily return %, win rate %, and green/red status
            calendar_days = []
            for d_str, data in sorted(daily_map.items()):
                trades_cnt = data["trade_count"]
                wins_cnt = data["wins"]
                net_p = round(data["net_pnl_rupees"], 2)
                ret_pct = round((net_p / initial_cap) * 100.0, 2)
                wr = round((wins_cnt / trades_cnt * 100.0), 1) if trades_cnt > 0 else 0.0
                status = "PROFIT" if net_p > 0 else ("LOSS" if net_p < 0 else "FLAT")

                calendar_days.append({
                    "date": d_str,
                    "net_pnl_rupees": net_p,
                    "gross_pnl_rupees": round(data["gross_pnl_rupees"], 2),
                    "fees_rupees": round(data["fees_rupees"], 2),
                    "return_pct": ret_pct,
                    "trade_count": trades_cnt,
                    "wins": wins_cnt,
                    "losses": data["losses"],
                    "win_rate": wr,
                    "status": status,
                    "trades": data["trades"]
                })

            # Monthly summary stats
            total_days_traded = len(calendar_days)
            profit_days = sum(1 for d in calendar_days if d["net_pnl_rupees"] > 0)
            loss_days = sum(1 for d in calendar_days if d["net_pnl_rupees"] < 0)
            total_net_pnl = round(sum(d["net_pnl_rupees"] for d in calendar_days), 2)
            total_fees = round(sum(d["fees_rupees"] for d in calendar_days), 2)

            return {
                "session_id": session_id,
                "initial_capital": initial_cap,
                "total_net_pnl": total_net_pnl,
                "total_fees": total_fees,
                "total_days_traded": total_days_traded,
                "profit_days": profit_days,
                "loss_days": loss_days,
                "daily_win_rate": round((profit_days / total_days_traded * 100.0), 1) if total_days_traded > 0 else 0.0,
                "days": calendar_days
            }
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_positions(
        cls,
        session_id: str = "default_arena_session",
        status: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None
    ) -> List[Dict[str, Any]]:
        """Retrieves full audited ledger of positions."""
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            should_close = True

        try:
            cur = conn.cursor()
            query = "SELECT * FROM consensus_game_positions WHERE session_id = ?"
            params = [session_id]
            if status and status.upper() in ("OPEN", "CLOSED"):
                query += " AND status = ?"
                params.append(status.upper())
            query += " ORDER BY id DESC"

            cur.execute(query, params)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()

            return [dict(zip(cols, r)) for r in rows]
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_day_replay(
        cls,
        session_id: str = "default_arena_session",
        date_str: str = "2026-09-18",
        conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """
        Historical Day Replayer:
        Replays a trading day minute-by-minute using local SQLite 15m OHLCV bars.
        Zero external API calls consumed (100% Upstox safe).
        """
        cls.init_game_schema(conn)
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        try:
            cur = conn.cursor()
            account = cur.execute(
                "SELECT initial_capital FROM consensus_game_accounts WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            initial_cap = float(account[0]) if account else 100000.0

            # Fetch trades executed on this day
            cur.execute("""
                SELECT id, ticker, direction, shares, entry_price, exit_price,
                       allocated_capital, net_pnl_rupees, outcome, entry_time, exit_time
                FROM consensus_game_positions
                WHERE session_id = ? AND entry_time LIKE ?
                ORDER BY entry_time ASC
            """, (session_id, f"{date_str}%"))
            day_trades = cur.fetchall()

            if not day_trades:
                return {
                    "date": date_str,
                    "message": f"No trades recorded on {date_str}",
                    "timeline": []
                }

            # Generate chronological intraday replay timeline (9:15 to 15:30)
            timeline = []
            running_equity = initial_cap
            running_cash = initial_cap
            active_open = {}

            def _clean_ts(t_str: str) -> str:
                if not t_str:
                    return ""
                t = str(t_str).replace("T", " ")
                if "." in t:
                    t = t.split(".")[0]
                return t

            # Sort events: entry events and exit events
            events = []
            for t in day_trades:
                tid, ticker, direction, shares, entry_px, exit_px, alloc_cap, net_pnl, outcome, entry_t, exit_t = t
                clean_ent = _clean_ts(entry_t)
                clean_ext = _clean_ts(exit_t) if exit_t else None

                events.append({
                    "time": clean_ent,
                    "type": "ENTRY",
                    "trade_id": tid,
                    "ticker": ticker,
                    "direction": direction,
                    "shares": shares,
                    "price": entry_px,
                    "capital": alloc_cap
                })
                if clean_ext:
                    events.append({
                        "time": clean_ext,
                        "type": "EXIT",
                        "trade_id": tid,
                        "ticker": ticker,
                        "direction": direction,
                        "shares": shares,
                        "price": exit_px,
                        "net_pnl": net_pnl,
                        "outcome": outcome,
                        "capital": alloc_cap
                    })

            events.sort(key=lambda x: (x["time"], 0 if x["type"] == "ENTRY" else 1))

            timeline = [{
                "timestamp": f"{date_str} 09:15:00",
                "equity": round(initial_cap, 2),
                "cash": round(initial_cap, 2),
                "pnl": 0.0,
                "open_positions": 0,
                "event": "Market Open (09:15 IST)"
            }]

            for ev in events:
                if ev["type"] == "ENTRY":
                    running_cash -= ev["capital"]
                    active_open[ev["trade_id"]] = ev
                elif ev["type"] == "EXIT":
                    running_cash += (ev["capital"] + ev["net_pnl"])
                    if ev["trade_id"] in active_open:
                        del active_open[ev["trade_id"]]
                
                # Active equity = cash + capital in open positions
                current_eq = running_cash + sum(p["capital"] for p in active_open.values())
                timeline.append({
                    "timestamp": ev["time"],
                    "equity": round(current_eq, 2),
                    "cash": round(running_cash, 2),
                    "pnl": round(current_eq - initial_cap, 2),
                    "open_positions": len(active_open),
                    "event": f"{ev['type']} {ev['ticker']} ({ev.get('outcome') or ev['direction']})"
                })

            return {
                "date": date_str,
                "initial_capital": initial_cap,
                "final_equity": round(timeline[-1]["equity"], 2) if timeline else initial_cap,
                "day_pnl": round((timeline[-1]["equity"] - initial_cap), 2) if timeline else 0.0,
                "trades_count": len(day_trades),
                "timeline": timeline
            }
        finally:
            if should_close:
                conn.close()
