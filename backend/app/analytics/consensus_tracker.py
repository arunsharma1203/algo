"""
ConsensusTracker: Authoritative Tracking & Lifecycle Reconciliation for Dual-Engine Consensus Trades.

Invariants:
- Dedicated physical isolation in `consensus_trade_history` table in SQLite.
- Mirrors into `ml_trade_history` with `engine = 'CONSENSUS'` and `position_type = 'NOT_A_POSITION'` (0 portfolio heat consumed).
- Session-level deduplication (anti-spam gate prevents multiple alerts for the same stock within the same session).
- Reconciles open trades against live 15m OHLCV bars (TARGET MET, SL HIT, SQUARED OFF).
"""

import json
import math
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from app.data.historical_data_layer import get_db_path
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)


class ConsensusTracker:
    _schema_initialized = False
    _last_reconcile_time: float = 0.0

    @classmethod
    def init_consensus_tracking_schema(cls, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initializes the dedicated consensus_trade_history table."""
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consensus_trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    target_price REAL NOT NULL,
                    composite_score REAL NOT NULL,
                    qlib_score REAL,
                    legacy_score REAL,
                    finbert_score REAL,
                    risk_reward_ratio REAL,
                    status TEXT DEFAULT 'OPEN',
                    outcome TEXT,
                    profit_pct REAL,
                    exit_price REAL,
                    exit_time TEXT,
                    source TEXT DEFAULT 'CONSENSUS_DAEMON',
                    position_type TEXT DEFAULT 'NOT_A_POSITION',
                    details_json TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cth_status ON consensus_trade_history(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cth_timestamp ON consensus_trade_history(timestamp);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cth_ticker_date ON consensus_trade_history(ticker, timestamp);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cth_status_time ON consensus_trade_history(status, timestamp);")

            # Dual-engine shadow candidate evaluation ledger (Credit Assignment & Selection Bias Logging)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consensus_candidate_shadow_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    scan_id TEXT,
                    ticker TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    qlib_direction TEXT,
                    qlib_score REAL,
                    qlib_price REAL,
                    legacy_direction TEXT,
                    legacy_confidence REAL,
                    legacy_price REAL,
                    composite_score REAL,
                    action_taken TEXT NOT NULL,
                    entry_ref_price REAL,
                    forward_15m_price REAL,
                    forward_eod_price REAL,
                    forward_return_pct REAL,
                    outcome TEXT,
                    qlib_loss REAL,
                    legacy_loss REAL,
                    details_json TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ccsl_scan_id ON consensus_candidate_shadow_log(scan_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ccsl_strat_time ON consensus_candidate_shadow_log(strategy, timestamp);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ccsl_action ON consensus_candidate_shadow_log(action_taken);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ccsl_ticker_time ON consensus_candidate_shadow_log(ticker, timestamp);")

            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ml_trade_engine_status_time ON ml_trade_history(engine, status, timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_qlib_virtual_status_time ON qlib_virtual_recommendations(status, timestamp);")
            except Exception:
                pass
            conn.commit()
            cls._schema_initialized = True
        finally:
            if should_close:
                conn.close()

    @classmethod
    def log_shadow_candidates(
        cls,
        candidates: List[Dict[str, Any]],
        scan_id: str,
        strategy: str = "INTRADAY"
    ) -> int:
        """
        Persists evaluated dual-engine candidates into consensus_candidate_shadow_log.
        Enables off-policy credit assignment and regret minimization on counterfactuals.
        """
        if not candidates:
            return 0

        cls.init_consensus_tracking_schema()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        strat_upper = strategy.upper()

        records_to_insert = []
        for c in candidates:
            ticker = str(c.get("ticker") or "").strip().upper()
            sym = str(c.get("symbol") or ticker.replace(".NS", "").replace(".BO", "")).strip().upper()
            if not ticker:
                continue

            records_to_insert.append((
                now_str,
                scan_id,
                ticker,
                sym,
                strat_upper,
                c.get("qlib_direction"),
                c.get("qlib_score"),
                c.get("qlib_price"),
                c.get("legacy_direction"),
                c.get("legacy_confidence"),
                c.get("legacy_price"),
                c.get("composite_score"),
                c.get("action_taken", "EVALUATED"),
                c.get("entry_ref_price"),
                c.get("forward_15m_price"),
                c.get("forward_eod_price"),
                c.get("forward_return_pct"),
                c.get("outcome"),
                c.get("qlib_loss"),
                c.get("legacy_loss"),
                json.dumps(c.get("details") or {}, default=str)
            ))

        if not records_to_insert:
            return 0

        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        try:
            cur = conn.cursor()
            cur.executemany("""
                INSERT INTO consensus_candidate_shadow_log (
                    timestamp, scan_id, ticker, symbol, strategy,
                    qlib_direction, qlib_score, qlib_price,
                    legacy_direction, legacy_confidence, legacy_price,
                    composite_score, action_taken, entry_ref_price,
                    forward_15m_price, forward_eod_price, forward_return_pct,
                    outcome, qlib_loss, legacy_loss, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, records_to_insert)
            conn.commit()
            return len(records_to_insert)
        except Exception as e:
            logger.warning(f"[ConsensusTracker] Failed to log shadow candidates: {e}")
            return 0
        finally:
            conn.close()

    @classmethod
    def _is_active_arena_trade(cls, ticker: str, conn: sqlite3.Connection) -> bool:
        """
        Authoritatively checks whether the ticker currently holds an open active position
        in the Consensus Arena (consensus_game_positions).
        """
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='consensus_game_positions'")
            if not cur.fetchone():
                return False
            cur.execute("""
                SELECT id FROM consensus_game_positions
                WHERE session_id = 'default_arena_session' AND ticker = ? AND status = 'OPEN'
                LIMIT 1
            """, (ticker,))
            return cur.fetchone() is not None
        except Exception as e:
            logger.warning(f"[ConsensusTracker] Could not check active arena positions: {e}")
            return False

    @classmethod
    def _get_recently_closed_exit_time(cls, ticker: str, conn: sqlite3.Connection) -> Optional[str]:
        """
        Checks if the ticker recently had an active position closed in the Consensus Arena
        and returns its exit_time.
        """
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='consensus_game_positions'")
            if not cur.fetchone():
                return None
            cur.execute("""
                SELECT exit_time FROM consensus_game_positions
                WHERE session_id = 'default_arena_session' AND ticker = ? AND status = 'CLOSED'
                ORDER BY exit_time DESC, id DESC LIMIT 1
            """, (ticker,))
            row = cur.fetchone()
            return str(row[0]) if row and row[0] else None
        except Exception as e:
            logger.warning(f"[ConsensusTracker] Could not check closed arena positions: {e}")
            return None

    @classmethod
    def save_consensus_trade(
        cls,
        trade: Dict[str, Any],
        strategy: str = "INTRADAY",
        source: str = "CONSENSUS_DAEMON",
        decision_timestamp: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Persists a qualified dual-engine consensus trade candidate.
        Protects active Arena trades from duplicate positions while allowing
        previously unselected candidates to compete in subsequent 15-minute scans.
        Returns:
            (saved: bool, reason: str)
        """
        cls.init_consensus_tracking_schema()
        ticker = str(trade.get("ticker") or trade.get("symbol") or "").strip().upper()
        if not ticker:
            return False, "INVALID_TICKER"

        clean_sym = ticker.replace(".NS", "").replace(".BO", "")
        direction = str(trade.get("direction", "BUY")).upper()
        entry_price = float(trade.get("blended_entry") or trade.get("entry") or trade.get("entry_price") or 0.0)
        stop_loss = float(trade.get("blended_sl") or trade.get("sl") or trade.get("stop_loss") or 0.0)
        target_price = float(trade.get("blended_tp") or trade.get("tp") or trade.get("target_price") or 0.0)
        composite_score = float(trade.get("composite_score") or 0.0)
        rr_ratio = float(trade.get("risk_reward_ratio") or 2.0)

        qlib_m = trade.get("qlib_metrics", {})
        legacy_m = trade.get("legacy_metrics", {})
        qlib_score = float(qlib_m.get("score") or trade.get("qlib_score") or 0.0)
        legacy_score = float(legacy_m.get("confidence") or trade.get("legacy_score") or 0.0)
        finbert_score = float(legacy_m.get("sentiment_score") or legacy_m.get("explanation", {}).get("sentiment_score") or 0.0)

        now_str = str(decision_timestamp or trade.get("decision_timestamp") or trade.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if "T" in now_str:
            now_str = now_str.replace("T", " ")[:19]

        # 0. Immediate Execution Integrity Gates: Hard Session Guard & Entry Cutoff Gate
        # Rejects pre-market (<09:15), post-market (>15:30), weekends, and entries past cutoff (>=15:00)
        force_override = bool(trade.get("force_override", False))
        if strategy.upper() == "INTRADAY" and not force_override:
            from app.analytics.consensus_economic_engine import ConsensusEconomicEngine
            is_sess_ok, sess_msg = ConsensusEconomicEngine.is_valid_trading_session(now_str)
            if not is_sess_ok:
                logger.warning(f"[ConsensusTracker] Intraday entry for {ticker} rejected by Hard Session Guard: {sess_msg}")
                return False, f"SESSION_REJECTED: {sess_msg}"

            is_cutoff_hit, cutoff_msg = ConsensusEconomicEngine.is_entry_cutoff_breached(now_str)
            if is_cutoff_hit:
                logger.warning(f"[ConsensusTracker] Intraday entry for {ticker} rejected by Entry Cutoff Gate: {cutoff_msg}")
                return False, f"ENTRY_CUTOFF_REJECTED: {cutoff_msg}"

        if trade.get("is_actionable") is False:
            return False, f"PRECISION_GATE_BLOCKED: {trade.get('precision_status')}"

        # Target / SL Pre-Flight Sanity Gate
        # Verify that current live market price has not already breached the target or stop loss before opening
        try:
            from app.data.upstox_provider import fetch_upstox_ltp, get_cached_upstox_quote
            current_live_ltp = fetch_upstox_ltp(ticker)
            if not current_live_ltp:
                cached_q = get_cached_upstox_quote(ticker)
                if cached_q and cached_q.get("ltp"):
                    current_live_ltp = float(cached_q["ltp"])

            check_price = current_live_ltp or entry_price
            if check_price and check_price > 0:
                if direction == "BUY":
                    if target_price > 0 and check_price >= target_price:
                        return False, f"REJECTED_ALREADY_HIT_TARGET: Price ₹{check_price:.2f} >= Target ₹{target_price:.2f}"
                    if stop_loss > 0 and check_price <= stop_loss:
                        return False, f"REJECTED_ALREADY_HIT_SL: Price ₹{check_price:.2f} <= SL ₹{stop_loss:.2f}"
                elif direction == "SELL":
                    if target_price > 0 and check_price <= target_price:
                        return False, f"REJECTED_ALREADY_HIT_TARGET: Price ₹{check_price:.2f} <= Target ₹{target_price:.2f}"
                    if stop_loss > 0 and check_price >= stop_loss:
                        return False, f"REJECTED_ALREADY_HIT_SL: Price ₹{check_price:.2f} >= SL ₹{stop_loss:.2f}"
        except Exception as e:
            logger.debug(f"Pre-flight live sanity check error for {ticker}: {e}")

        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        try:
            cur = conn.cursor()

            # 1. Authoritative Active Trade Guard: Check if ticker currently has an active open position in Arena
            if cls._is_active_arena_trade(ticker, conn):
                return False, f"REJECTED_ACTIVE_DUPLICATE: Active Arena position already open for {ticker}"

            # 2. Cooldown Guard: Enforce 15-minute cooldown if an active position was closed recently
            recent_exit_t = cls._get_recently_closed_exit_time(ticker, conn)
            if recent_exit_t:
                try:
                    exit_dt = datetime.strptime(str(recent_exit_t)[:19], "%Y-%m-%d %H:%M:%S")
                    elapsed = (datetime.now() - exit_dt).total_seconds()
                    if 0 <= elapsed < 900:
                        return False, f"RECENTLY_CLOSED_COOLDOWN (exit={recent_exit_t}, remaining={int(900 - elapsed)}s)"
                except Exception:
                    pass

            # 3. Intra-Scan 15-Minute Bucket Check: Deduplicate candidate updates within the exact same 15m scan window
            # Standardize to 15-minute floor bucket to prevent minute-by-minute splitting
            try:
                dt_val = datetime.strptime(now_str[:19], "%Y-%m-%d %H:%M:%S")
                b_m = (dt_val.minute // 15) * 15
                bucket_start = dt_val.replace(minute=b_m, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
                bucket_end = (dt_val.replace(minute=b_m, second=0, microsecond=0) + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
                bucket_prefix = bucket_start[:16]
            except Exception:
                bucket_start = now_str[:16]
                bucket_end = now_str[:16] + ":99"
                bucket_prefix = now_str[:16]

            cur.execute("""
                SELECT id, composite_score FROM consensus_trade_history
                WHERE ticker = ? AND strategy = ? AND (
                    timestamp LIKE ? OR (timestamp >= ? AND timestamp < ?)
                )
                ORDER BY id DESC LIMIT 1
            """, (ticker, strategy.upper(), f"{bucket_prefix}%", bucket_start, bucket_end))
            same_bucket = cur.fetchone()
            if same_bucket:
                existing_id = same_bucket[0]
                cur.execute("""
                    UPDATE consensus_trade_history
                    SET composite_score = ?, qlib_score = ?, legacy_score = ?, finbert_score = ?,
                        stop_loss = ?, target_price = ?, details_json = ?
                    WHERE id = ?
                """, (composite_score, qlib_score, legacy_score, finbert_score, stop_loss, target_price, json.dumps(trade, default=str), existing_id))
                conn.commit()
                return False, f"UPDATED_CURRENT_SCAN_CANDIDATE (DUPLICATE_SESSION_TRADE, bucket={bucket_prefix}, id={existing_id})"

            # 4. Insert Fresh Candidate Observation for the current 15-minute scan bucket
            cur.execute("""
                INSERT INTO consensus_trade_history (
                    timestamp, ticker, symbol, strategy, direction,
                    entry_price, stop_loss, target_price, composite_score,
                    qlib_score, legacy_score, finbert_score, risk_reward_ratio,
                    status, outcome, source, position_type, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', NULL, ?, 'NOT_A_POSITION', ?)
            """, (
                now_str, ticker, clean_sym, strategy.upper(), direction,
                entry_price, stop_loss, target_price, composite_score,
                qlib_score, legacy_score, finbert_score, rr_ratio,
                source, json.dumps(trade, default=str)
            ))
            conn.commit()

            # Mirror to ml_trade_history so unified reporting captures it
            try:
                from app.api.ml_history import save_ml_trade
                is_bullish = bool(direction == "BUY")
                save_ml_trade(
                    ticker=ticker,
                    is_bullish=is_bullish,
                    entry=entry_price,
                    sl=stop_loss,
                    tp1=target_price,
                    tp2=round(target_price * 1.01 if is_bullish else target_price * 0.99, 2),
                    confidence=composite_score,
                    trade_type=strategy.upper(),
                    explanation=f"Dual-Engine Consensus Confluence: Qlib={qlib_score}, Legacy={legacy_score}%, FinBERT={finbert_score}",
                    source=source,
                    position_type="NOT_A_POSITION",
                    engine="CONSENSUS",
                    strategy=strategy.upper()
                )
            except Exception as e:
                logger.warning(f"[ConsensusTracker] Could not mirror to ml_trade_history: {e}")

            logger.info(f"[ConsensusTracker] Successfully saved consensus trade: {ticker} {direction} @ ₹{entry_price}")
            return True, "SAVED"
        finally:
            conn.close()

    @classmethod
    def dispatch_telegram_alert(cls, trade: Dict[str, Any], strategy: str = "INTRADAY") -> bool:
        """
        Dispatches a high-priority Telegram alert for a qualified consensus confluence setup.
        Returns True if sent, False if suppressed or failed.
        """
        try:
            ticker = str(trade.get("ticker") or trade.get("symbol") or "").strip().upper()
            direction = str(trade.get("direction", "BUY")).upper()
            entry_price = float(trade.get("blended_entry") or trade.get("entry") or 0.0)
            stop_loss = float(trade.get("blended_sl") or trade.get("sl") or 0.0)
            target_price = float(trade.get("blended_tp") or trade.get("tp") or 0.0)
            composite_score = float(trade.get("composite_score") or 0.0)
            rr_ratio = float(trade.get("risk_reward_ratio") or 2.0)

            qlib_m = trade.get("qlib_metrics", {})
            legacy_m = trade.get("legacy_metrics", {})
            qlib_score = qlib_m.get("score") or trade.get("qlib_score") or "N/A"
            legacy_score = legacy_m.get("confidence") or trade.get("legacy_score") or "N/A"
            finbert_score = legacy_m.get("sentiment_score") or legacy_m.get("explanation", {}).get("sentiment_score") or 0.0

            arrow = "🟢 BUY" if direction == "BUY" else "🔴 SHORT/SELL"
            sentiment_tag = "BULLISH" if finbert_score > 0 else ("BEARISH" if finbert_score < 0 else "NEUTRAL")

            msg = (
                f"🎯 <b>DUAL-ENGINE CONSENSUS SIGNAL</b>\n"
                f"Grade-A Cross-Engine Confluence\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<b>Ticker:</b> <code>{ticker}</code> [{arrow}]\n"
                f"<b>Strategy:</b> {strategy.upper()} (15m)\n"
                f"<b>Composite Conviction:</b> <b>{composite_score:.1f}%</b>\n"
                f"─────────────────────────\n"
                f"• <b>Qlib Alpha-158:</b> {qlib_score}\n"
                f"• <b>Legacy Ensemble:</b> {legacy_score}%\n"
                f"• <b>FinBERT Sentiment:</b> {finbert_score:+.2f} ({sentiment_tag})\n"
                f"─────────────────────────\n"
                f"<b>Blended Entry:</b> ₹{entry_price:.2f}\n"
                f"<b>Conservative Stop Loss:</b> ₹{stop_loss:.2f}\n"
                f"<b>Profit Target (TP1):</b> ₹{target_price:.2f}\n"
                f"<b>Risk / Reward:</b> 1:{rr_ratio:.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<i>⚡ Tracked automatically in Consensus Engine</i>"
            )

            send_telegram_message(msg)
            logger.info(f"[ConsensusTracker] Telegram alert sent for {ticker} ({direction})")
            return True
        except Exception as e:
            logger.warning(f"[ConsensusTracker] Failed sending Telegram alert: {e}")
            return False

    @classmethod
    def reconcile_open_consensus_trades(
        cls,
        force_squareoff: bool = False,
        now: Optional[datetime] = None,
        min_interval_sec: float = 0.0
    ) -> int:
        """
        Reconciles open consensus recommendations against the latest 15m candle feeds
        and real-time live market quotes.
        Detects TARGET MET, SL HIT, and square-off (15:15 IST).
        Returns number of reconciled trades.
        """
        import time as _time
        if min_interval_sec > 0 and (_time.time() - getattr(cls, '_last_reconcile_time', 0.0)) < min_interval_sec and not force_squareoff:
            return 0

        cls.init_consensus_tracking_schema()
        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        reconciled_count = 0
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, ticker, direction, entry_price, stop_loss, target_price, timestamp, strategy
                FROM consensus_trade_history
                WHERE status = 'OPEN'
            """)
            open_trades = cur.fetchall()
            if not open_trades:
                return 0

            cls._last_reconcile_time = _time.time()

            current_dt = now or datetime.now()
            now_time = current_dt.time()
            now_str = current_dt.strftime("%Y-%m-%d %H:%M:%S")

            # Check if after 15:15 IST for intraday auto-square-off
            is_eod_squareoff = force_squareoff or (now_time.hour == 15 and now_time.minute >= 15) or (now_time.hour > 15)

            # Single bulk live quote query for ALL open trade tickers (strictly 1 Upstox call)
            open_tickers = list({str(t[1]).strip() for t in open_trades if t[1]})
            bulk_quotes = {}
            if open_tickers:
                try:
                    from app.data.market_provider import get_bulk_live_quotes_with_meta
                    bulk_quotes = get_bulk_live_quotes_with_meta(open_tickers)
                except Exception as ex:
                    logger.debug(f"[ConsensusTracker] Could not fetch bulk live quotes: {ex}")

            for tid, ticker, direction, entry, sl, tp, ts, strat in open_trades:
                trade_date = ts[:10]
                clean_ts = str(ts or "").replace("T", " ")[:19]
                if len(clean_ts) == 10:
                    clean_ts += " 00:00:00"

                # Fetch candles that started strictly AFTER the trade entry timestamp in chronological order
                # (Point-in-Time causality: evaluate bars sequentially from trade entry forward)
                cur.execute("""
                    SELECT open, high, low, close, date
                    FROM ohlcv
                    WHERE ticker = ? AND timeframe = '15m' AND date > ?
                    ORDER BY date ASC LIMIT 50
                """, (ticker, clean_ts))
                bars = cur.fetchall()

                # Extract live quote for this ticker if available
                q_data = bulk_quotes.get(ticker) if bulk_quotes else None
                ltp = float(q_data["price"]) if q_data and float(q_data.get("price") or 0.0) > 0 else None

                from app.analytics.consensus_dynamic_guard import ConsensusDynamicGuard
                from app.analytics.consensus_economic_engine import ConsensusEconomicEngine
                profit_lock_thresh = ConsensusDynamicGuard.PROFIT_LOCK_THRESHOLD_PCT / 100.0  # 1.10%
                be_spread = ConsensusDynamicGuard.BREAKEVEN_LOCK_SPREAD_PCT / 100.0          # 0.35%

                # Position size / capital-aware fee-compensated economic breakeven price (+0.35% covers roundtrip friction)
                econ_be_sl = round(entry * (1.0 + be_spread) if direction == "BUY" else entry * (1.0 - be_spread), 2)

                outcome = None
                exit_price = None
                ratcheted = False
                last_bar_close = None

                # ── Phase 1: Chronological Bar Evaluation ──
                for b_open, b_high, b_low, b_close, b_date in bars:
                    b_open, b_high, b_low, b_close = float(b_open), float(b_high), float(b_low), float(b_close)
                    last_bar_close = b_close

                    if direction == "BUY":
                        # Target hit check on this bar
                        if tp > entry and b_high >= tp:
                            if b_low <= sl and b_open < tp:
                                outcome = "ECONOMIC_BREAKEVEN" if sl > entry else "SL HIT"
                                exit_price = sl
                            else:
                                outcome = "TARGET MET"
                                exit_price = tp
                            break
                        # Stop loss hit check against the SL active during this bar
                        elif b_low <= sl:
                            outcome = "ECONOMIC_BREAKEVEN" if sl > entry else "SL HIT"
                            exit_price = sl
                            break
                        # Dynamic Ratchet: if position survived this bar and touched profit threshold,
                        # ratchet SL to true economic breakeven price covering all round-trip costs
                        elif b_high >= entry * (1.0 + profit_lock_thresh):
                            new_sl = round(max(sl, econ_be_sl), 2)
                            if new_sl > sl:
                                sl = new_sl
                                ratcheted = True
                    else:  # SELL
                        if tp < entry and b_low <= tp:
                            if b_high >= sl and b_open > tp:
                                outcome = "ECONOMIC_BREAKEVEN" if sl < entry else "SL HIT"
                                exit_price = sl
                            else:
                                outcome = "TARGET MET"
                                exit_price = tp
                            break
                        elif b_high >= sl:
                            outcome = "ECONOMIC_BREAKEVEN" if sl < entry else "SL HIT"
                            exit_price = sl
                            break
                        elif b_low <= entry * (1.0 - profit_lock_thresh):
                            new_sl = round(min(sl, econ_be_sl), 2)
                            if new_sl < sl:
                                sl = new_sl
                                ratcheted = True

                # ── Phase 2: Live Real-Time Tick Evaluation (if position survived completed bars) ──
                if outcome is None and ltp is not None:
                    dwell_ok = True
                    if strat == "INTRADAY" and len(bars) == 0 and not is_eod_squareoff:
                        try:
                            trade_dt = datetime.strptime(clean_ts[:19], "%Y-%m-%d %H:%M:%S")
                            trade_age_sec = (current_dt - trade_dt).total_seconds()
                            if trade_age_sec < 60.0:
                                dwell_ok = False
                        except Exception:
                            pass

                    if dwell_ok:
                        if direction == "BUY":
                            if tp > entry and ltp >= tp:
                                outcome = "TARGET MET"
                                exit_price = tp
                            elif ltp <= sl:
                                outcome = "ECONOMIC_BREAKEVEN" if sl > entry else "SL HIT"
                                exit_price = sl
                            elif ltp >= entry * (1.0 + profit_lock_thresh):
                                new_sl = round(max(sl, econ_be_sl), 2)
                                if new_sl > sl:
                                    sl = new_sl
                                    ratcheted = True
                        else:  # SELL
                            if tp < entry and ltp <= tp:
                                outcome = "TARGET MET"
                                exit_price = tp
                            elif ltp >= sl:
                                outcome = "ECONOMIC_BREAKEVEN" if sl < entry else "SL HIT"
                                exit_price = sl
                            elif ltp <= entry * (1.0 - profit_lock_thresh):
                                new_sl = round(min(sl, econ_be_sl), 2)
                                if new_sl < sl:
                                    sl = new_sl
                                    ratcheted = True

                # ── Phase 3: End-of-Day Session Exit (15:15 IST) ──
                if outcome is None and is_eod_squareoff and strat == "INTRADAY" and current_dt.strftime("%Y-%m-%d") >= trade_date:
                    outcome = "SESSION_EXIT"
                    exit_price = ltp if ltp is not None else (last_bar_close if last_bar_close is not None else entry)
                    exit_price = ltp if ltp is not None else (last_bar_close if last_bar_close is not None else entry)

                # Persist ratcheted SL if changed
                if ratcheted:
                    cur.execute("UPDATE consensus_trade_history SET stop_loss = ? WHERE id = ?", (sl, tid))
                    try:
                        cur.execute("UPDATE ml_trade_history SET sl = ? WHERE ticker = ? AND engine = 'CONSENSUS' AND status = 'OPEN'", (sl, ticker))
                    except Exception:
                        pass
                    try:
                        cur.execute("UPDATE consensus_game_positions SET stop_loss = ? WHERE trade_ref_id = ? AND status = 'OPEN'", (sl, tid))
                    except Exception:
                        pass
                    conn.commit()
                    logger.info(f"[ConsensusTracker] Point-in-time ratcheted SL to Breakeven ₹{sl} for trade #{tid} ({ticker})")

                if outcome and exit_price is not None:
                    pnl_pct = round(((exit_price - entry) / entry * 100.0) if direction == "BUY" else ((entry - exit_price) / entry * 100.0), 2)
                    cur.execute("""
                        UPDATE consensus_trade_history
                        SET status = 'CLOSED', outcome = ?, exit_price = ?, profit_pct = ?, exit_time = ?
                        WHERE id = ?
                    """, (outcome, exit_price, pnl_pct, now_str, tid))
                    conn.commit()

                    # Also update mirrored record in ml_trade_history
                    try:
                        cur.execute("""
                            UPDATE ml_trade_history
                            SET status = 'CLOSED', outcome = ?, exit_price = ?, profit_pct = ?, exit_time = ?
                            WHERE ticker = ? AND engine = 'CONSENSUS' AND status = 'OPEN'
                        """, (outcome, exit_price, pnl_pct, now_str, ticker))
                        conn.commit()
                    except Exception:
                        pass

                    reconciled_count += 1
                    logger.info(f"[ConsensusTracker] Reconciled trade #{tid} ({ticker}): {outcome} with PnL {pnl_pct}%")

            if reconciled_count > 0:
                try:
                    from app.analytics.consensus_game_engine import ConsensusGameEngine
                    ConsensusGameEngine.sync_and_dispatch_arena_alerts()
                except Exception as ex:
                    logger.debug(f"[ConsensusTracker] Could not dispatch arena exit alerts: {ex}")

            return reconciled_count
        finally:
            conn.close()

    @classmethod
    def get_consensus_performance_stats(cls, days: int = 30, filter_mode: str = "ALL_QUALIFIED") -> Dict[str, Any]:
        """
        Computes comparative performance statistics for consensus trades.
        """
        cls.init_consensus_tracking_schema()
        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, timestamp, ticker, direction, entry_price, exit_price, outcome, profit_pct, composite_score,
                       stop_loss, target_price, qlib_score, legacy_score, risk_reward_ratio, details_json
                FROM consensus_trade_history
                WHERE status = 'CLOSED' AND outcome IS NOT NULL
                ORDER BY timestamp ASC
            """)
            rows = cur.fetchall()

            if filter_mode == "HIGHEST_CONFLUENCE_ONLY":
                from collections import defaultdict
                by_scan = defaultdict(list)
                for r in rows:
                    ts_key = str(r[1]).replace("T", " ")[:16]
                    by_scan[ts_key].append(r)
                filtered_rows = []
                for ts_key in sorted(by_scan.keys()):
                    batch = by_scan[ts_key]
                    from app.analytics.consensus_tiebreaker import ConsensusTieBreaker
                    best = ConsensusTieBreaker.select_best_candidate(
                        batch,
                        score_index=8,
                        historical_id_sign=-1
                    )
                    filtered_rows.append(best)
                rows = filtered_rows
            elif filter_mode in ("TIMED_ANCHOR_CONFLUENCE", "TIMED_SCANS_ONLY", "TIMED_SCANS_HIGH_CONFLUENCE"):
                from collections import defaultdict
                by_day = defaultdict(list)
                for r in rows:
                    ts = str(r[1] or "")
                    dt = ts[:10]
                    by_day[dt].append(r)

                target_minutes = {"09:30": 570, "11:30": 690, "13:30": 810}
                selected_ids = set()

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
                                    diff = abs(b_mins - tgt_m)
                                    if diff <= 45 and diff < best_diff:
                                        best_diff = diff
                                        best_batch_tm = b_tm
                                else:
                                    diff = b_mins - tgt_m
                                    if 0 <= diff <= 30 and diff < best_diff:
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

                            best = ConsensusTieBreaker.select_best_candidate(
                                b_trades,
                                score_index=8,
                                historical_id_sign=-1,
                                quality_boost_evaluator=_eval_anchor_filter
                            )
                            selected_ids.add(best[0])

                rows = [r for r in rows if r[0] in selected_ids]

            total_trades = len(rows)
            if total_trades == 0:
                return {
                    "total_trades": 0,
                    "win_rate": 0.0,
                    "target_hits": 0,
                    "sl_hits": 0,
                    "squared_off": 0,
                    "avg_profit_pct": 0.0,
                    "total_profit_pct": 0.0,
                    "cumulative_series": [],
                    "trades": []
                }

            wins = sum(1 for r in rows if float(r[7] or 0.0) > 0)
            sls = sum(1 for r in rows if float(r[7] or 0.0) < 0)
            sqs = sum(1 for r in rows if float(r[7] or 0.0) == 0.0)

            pnls = [float(r[7] or 0.0) for r in rows]
            avg_pnl = round(sum(pnls) / total_trades, 2)
            total_pnl = round(sum(pnls), 2)
            win_rate = round((wins / total_trades) * 100.0, 1)

            cum = 0.0
            cum_series = []
            trade_list = []

            for r in rows:
                pnl = float(r[7] or 0.0)
                cum = round(cum + pnl, 2)
                cum_series.append({
                    "date": r[1][:10],
                    "time": r[1],
                    "ticker": r[2],
                    "cum_pnl": cum
                })
                trade_list.append({
                    "id": r[0],
                    "timestamp": r[1],
                    "ticker": r[2],
                    "direction": r[3],
                    "entry_price": r[4],
                    "exit_price": r[5],
                    "outcome": r[6],
                    "profit_pct": pnl,
                    "score": r[8]
                })

            return {
                "total_trades": total_trades,
                "filter_mode": filter_mode,
                "win_rate": win_rate,
                "target_hits": wins,
                "sl_hits": sls,
                "squared_off": sqs,
                "avg_profit_pct": avg_pnl,
                "total_profit_pct": total_pnl,
                "cumulative_series": cum_series,
                "trades": list(reversed(trade_list))[:20]
            }
        finally:
            conn.close()

    @classmethod
    def get_all_tracked_trades(
        cls,
        strategy: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Retrieves all persisted consensus trades with full lifecycle tracking.
        Enriches OPEN trades with live current LTP and unrealized PnL.
        """
        cls.init_consensus_tracking_schema()
        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            query = "SELECT * FROM consensus_trade_history"
            params = []
            clauses = []
            if strategy:
                clauses.append("strategy = ?")
                params.append(strategy.upper())
            if status:
                clauses.append("status = ?")
                params.append(status.upper())
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)


            rows = cur.execute(query, params).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["timeframe"] = "15m" if d.get("strategy") == "INTRADAY" else "1d"
                if d.get("details_json"):
                    try:
                        det = json.loads(d["details_json"])
                        if isinstance(det, dict) and "meta_learner_v2" in det:
                            d["meta_learner_v2"] = det["meta_learner_v2"]
                    except Exception:
                        pass
                results.append(d)

            # Enrich OPEN trades with live LTP and unrealized profit_pct
            open_tickers = [d["ticker"] for d in results if d.get("status") == "OPEN"]
            live_quotes = {}
            if open_tickers:
                try:
                    from app.data.market_provider import get_bulk_live_quotes_with_meta
                    live_quotes = get_bulk_live_quotes_with_meta(open_tickers)
                except Exception:
                    pass

            for d in results:
                if d.get("status") == "OPEN":
                    q = live_quotes.get(d["ticker"])
                    ltp = float(q.get("price") or 0.0) if q else 0.0
                    if ltp > 0:
                        d["current_price"] = ltp
                        entry = float(d.get("entry_price") or 1.0)
                        if d.get("direction") == "BUY":
                            d["profit_pct"] = round(((ltp - entry) / entry) * 100.0, 2)
                        else:
                            d["profit_pct"] = round(((entry - ltp) / entry) * 100.0, 2)
                    else:
                        d["current_price"] = d.get("entry_price")

            return results
        finally:
            conn.close()

    @classmethod
    def get_multi_engine_comparison(cls, days: int = 30, filter_mode: str = "ALL_QUALIFIED") -> Dict[str, Any]:
        """
        Compiles authoritative comparative performance across:
        1. Qlib Engine (Alpha-158 GBDT)
        2. Legacy Engine (Ensemble + FinBERT)
        3. Dual-Engine Consensus (Unanimous Confluence)
        """
        cls.init_consensus_tracking_schema()
        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        try:
            cur = conn.cursor()

            def _compute_engine_stats(rows):
                if not rows:
                    return {
                        "total_trades": 0,
                        "win_rate": 0.0,
                        "wins": 0,
                        "losses": 0,
                        "total_pnl": 0.0,
                        "avg_pnl": 0.0,
                        "profit_factor": 1.0,
                        "cum_by_date": {}
                    }
                total = len(rows)
                wins = sum(1 for r in rows if float(r.get("profit_pct") or 0.0) > 0)
                losses = sum(1 for r in rows if float(r.get("profit_pct") or 0.0) < 0)
                win_rate = round((wins / total) * 100.0, 1) if total > 0 else 0.0

                gross_profit = sum(r["profit_pct"] for r in rows if r["profit_pct"] > 0)
                gross_loss = abs(sum(r["profit_pct"] for r in rows if r["profit_pct"] < 0))
                profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (2.5 if gross_profit > 0 else 1.0)

                total_pnl = round(sum(r["profit_pct"] for r in rows), 2)
                avg_pnl = round(total_pnl / total, 2) if total > 0 else 0.0

                cum = 0.0
                cum_by_date = {}
                for r in rows:
                    cum = round(cum + r["profit_pct"], 2)
                    d = str(r["timestamp"])[:10]
                    cum_by_date[d] = cum

                return {
                    "total_trades": total,
                    "win_rate": win_rate,
                    "wins": wins,
                    "losses": losses,
                    "total_pnl": total_pnl,
                    "avg_pnl": avg_pnl,
                    "profit_factor": profit_factor,
                    "cum_by_date": cum_by_date
                }

            cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else "1970-01-01"

            # 1. Consensus Trades
            cur.execute("""
                SELECT id, timestamp, ticker, direction, entry_price, exit_price, outcome, profit_pct, composite_score
                FROM consensus_trade_history
                WHERE status = 'CLOSED' AND outcome IS NOT NULL AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (cutoff_date,))
            c_rows = [
                {
                    "id": r[0], "timestamp": r[1], "ticker": r[2], "direction": r[3],
                    "entry": r[4], "exit": r[5], "outcome": r[6], "profit_pct": float(r[7] or 0.0),
                    "score": r[8], "engine": "CONSENSUS"
                } for r in cur.fetchall()
            ]

            # 2. Legacy Trades
            cur.execute("""
                SELECT id, timestamp, ticker, direction, entry, exit_price, outcome, profit_pct, confidence
                FROM ml_trade_history
                WHERE (engine IS NULL OR engine = 'LEGACY' OR engine = '') 
                  AND status = 'CLOSED' AND outcome IS NOT NULL AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (cutoff_date,))
            l_rows = [
                {
                    "id": r[0], "timestamp": r[1], "ticker": r[2], "direction": r[3],
                    "entry": r[4], "exit": r[5], "outcome": r[6], "profit_pct": float(r[7] or 0.0),
                    "score": r[8], "engine": "LEGACY"
                } for r in cur.fetchall()
            ]

            # 3. Qlib Trades
            q_rows = []
            try:
                cur.execute("""
                    SELECT id, timestamp, ticker, direction, entry_price, exit_price, outcome, pnl_pct, confidence
                    FROM qlib_virtual_recommendations
                    WHERE status = 'CLOSED' AND outcome IS NOT NULL AND timestamp >= ?
                    ORDER BY timestamp ASC
                """, (cutoff_date,))
                q_rows = [
                    {
                        "id": r[0], "timestamp": r[1], "ticker": r[2], "direction": r[3],
                        "entry": r[4], "exit": r[5], "outcome": r[6], "profit_pct": float(r[7] or 0.0),
                        "score": r[8], "engine": "QLIB"
                    } for r in cur.fetchall()
                ]
            except Exception:
                pass

            if not q_rows:
                cur.execute("""
                    SELECT id, timestamp, ticker, direction, entry, exit_price, outcome, profit_pct, confidence
                    FROM ml_trade_history
                    WHERE engine = 'QLIB' AND status = 'CLOSED' AND outcome IS NOT NULL AND timestamp >= ?
                    ORDER BY timestamp ASC
                """, (cutoff_date,))
                q_rows = [
                    {
                        "id": r[0], "timestamp": r[1], "ticker": r[2], "direction": r[3],
                        "entry": r[4], "exit": r[5], "outcome": r[6], "profit_pct": float(r[7] or 0.0),
                        "score": r[8], "engine": "QLIB"
                    } for r in cur.fetchall()
                ]

            if filter_mode == "HIGHEST_CONFLUENCE_ONLY":
                def _pick_top_per_scan(rows):
                    if not rows:
                        return []
                    from collections import defaultdict
                    by_scan = defaultdict(list)
                    for r in rows:
                        ts_key = str(r.get("timestamp", "")).replace("T", " ")[:16]
                        by_scan[ts_key].append(r)
                    top_list = []
                    for ts_key in sorted(by_scan.keys()):
                        batch = by_scan[ts_key]
                        from app.analytics.consensus_tiebreaker import ConsensusTieBreaker
                        best = ConsensusTieBreaker.select_best_candidate(
                            batch,
                            score_index=8,
                            historical_id_sign=1
                        )
                        top_list.append(best)
                    return top_list

                c_rows = _pick_top_per_scan(c_rows)
                l_rows = _pick_top_per_scan(l_rows)
                q_rows = _pick_top_per_scan(q_rows)
            elif filter_mode in ("TIMED_ANCHOR_CONFLUENCE", "TIMED_SCANS_ONLY", "TIMED_SCANS_HIGH_CONFLUENCE"):
                def _pick_top_timed_anchors(rows):
                    if not rows:
                        return []
                    from collections import defaultdict
                    by_day = defaultdict(list)
                    for r in rows:
                        ts = str(r.get("timestamp") or "")
                        dt = ts[:10]
                        by_day[dt].append(r)

                    target_minutes = {"09:30": 570, "11:30": 690, "13:30": 810}
                    top_list = []
                    for dt, d_trades in by_day.items():
                        d_batches = defaultdict(list)
                        for tr in d_trades:
                            ts = str(tr.get("timestamp") or "")
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
                                        diff = abs(b_mins - tgt_m)
                                        if diff <= 45 and diff < best_diff:
                                            best_diff = diff
                                            best_batch_tm = b_tm
                                    else:
                                        diff = b_mins - tgt_m
                                        if 0 <= diff <= 30 and diff < best_diff:
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

                                best = ConsensusTieBreaker.select_best_candidate(
                                    b_trades,
                                    score_index=8,
                                    historical_id_sign=1,
                                    quality_boost_evaluator=_eval_anchor_filter
                                )
                                top_list.append(best)
                    return top_list

                c_rows = _pick_top_timed_anchors(c_rows)
                l_rows = _pick_top_timed_anchors(l_rows)
                q_rows = _pick_top_timed_anchors(q_rows)

            c_stats = _compute_engine_stats(c_rows)
            l_stats = _compute_engine_stats(l_rows)
            q_stats = _compute_engine_stats(q_rows)

            # Build unified date timeline
            all_dates = sorted(list(set(
                list(c_stats["cum_by_date"].keys()) +
                list(l_stats["cum_by_date"].keys()) +
                list(q_stats["cum_by_date"].keys())
            )))

            timeline = []
            last_c, last_l, last_q = 0.0, 0.0, 0.0
            for d in all_dates:
                if d in c_stats["cum_by_date"]:
                    last_c = c_stats["cum_by_date"][d]
                if d in l_stats["cum_by_date"]:
                    last_l = l_stats["cum_by_date"][d]
                if d in q_stats["cum_by_date"]:
                    last_q = q_stats["cum_by_date"][d]
                timeline.append({
                    "date": d,
                    "consensus": last_c,
                    "legacy": last_l,
                    "qlib": last_q
                })

            # Recent merged trade log
            all_recent = sorted(c_rows + l_rows + q_rows, key=lambda x: str(x["timestamp"]), reverse=True)[:30]

            return {
                "status": "SUCCESS",
                "filter_mode": filter_mode,
                "summary": {
                    "consensus": {
                        "name": "Dual-Engine Consensus",
                        "badge": "Confluence (Grade-A)",
                        "total_trades": c_stats["total_trades"],
                        "win_rate": c_stats["win_rate"],
                        "wins": c_stats["wins"],
                        "losses": c_stats["losses"],
                        "total_pnl": c_stats["total_pnl"],
                        "avg_pnl": c_stats["avg_pnl"],
                        "profit_factor": c_stats["profit_factor"]
                    },
                    "legacy": {
                        "name": "Legacy Multi-Model",
                        "badge": "Ensemble + FinBERT",
                        "total_trades": l_stats["total_trades"],
                        "win_rate": l_stats["win_rate"],
                        "wins": l_stats["wins"],
                        "losses": l_stats["losses"],
                        "total_pnl": l_stats["total_pnl"],
                        "avg_pnl": l_stats["avg_pnl"],
                        "profit_factor": l_stats["profit_factor"]
                    },
                    "qlib": {
                        "name": "Microsoft Qlib",
                        "badge": "Alpha-158 GBDT",
                        "total_trades": q_stats["total_trades"],
                        "win_rate": q_stats["win_rate"],
                        "wins": q_stats["wins"],
                        "losses": q_stats["losses"],
                        "total_pnl": q_stats["total_pnl"],
                        "avg_pnl": q_stats["avg_pnl"],
                        "profit_factor": q_stats["profit_factor"]
                    }
                },
                "timeline": timeline,
                "recent_trades": all_recent
            }
        finally:
            conn.close()

    @classmethod
    def get_shadow_candidates(
        cls,
        strategy: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Retrieves recent shadow candidate records from consensus_candidate_shadow_log.
        """
        cls.init_consensus_tracking_schema()
        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        try:
            cur = conn.cursor()
            query = "SELECT id, timestamp, scan_id, ticker, symbol, strategy, qlib_direction, qlib_score, legacy_direction, legacy_confidence, composite_score, action_taken, entry_ref_price, forward_return_pct, outcome FROM consensus_candidate_shadow_log"
            params = []
            conditions = []
            if strategy:
                conditions.append("strategy = ?")
                params.append(strategy.upper())
            if action:
                conditions.append("action_taken = ?")
                params.append(action.upper())
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()
            results = []
            for r in rows:
                results.append({
                    "id": r[0],
                    "timestamp": r[1],
                    "scan_id": r[2],
                    "ticker": r[3],
                    "symbol": r[4],
                    "strategy": r[5],
                    "qlib_direction": r[6],
                    "qlib_score": r[7],
                    "legacy_direction": r[8],
                    "legacy_confidence": r[9],
                    "composite_score": r[10],
                    "action_taken": r[11],
                    "entry_ref_price": r[12],
                    "forward_return_pct": r[13],
                    "outcome": r[14]
                })
            return results
        except Exception as e:
            logger.warning(f"[ConsensusTracker] Error retrieving shadow candidates: {e}")
            return []
        finally:
            conn.close()

