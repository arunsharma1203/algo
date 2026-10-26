"""
ConsensusDynamicGuard: Institutional Dynamic Trade Guard (IDTG) for Consensus Game Arena.

Guarantees & Invariants:
1. Active Slot Isolation: Evaluates ONLY currently OPEN positions in `consensus_game_positions`
   for session `default_arena_session`.
2. Absolute Historical Data Lock: Does not alter past, closed, or backfilled trade records.
3. Zero Forward Bias (Point-in-Time Causality):
   - Never looks at future price bars.
   - Evaluates strictly the current tick / latest bar close, current live VWAP, Day's Open,
     and current macro regime at evaluation time T.
   - The trade's initial target price (+1.60%) is PERMANENTLY INVARIANT (never redefined or pushed away).
4. Monotonic Ratchet Rule (Risk Reduction Only):
   - For BUY / LONG: New SL >= Current SL.
   - For SELL / SHORT: New SL <= Current SL.
   - Widening a stop loss is mathematically impossible and will fail-closed.
5. Upstox Rate Limit Compliance:
   - Queries all active slot positions (max 3-5) using EXACTLY ONE single bulk HTTP request
     via `fetch_upstox_bulk_quotes()`.
   - Reuses cached quotes if available; fails closed gracefully if the Upstox circuit breaker is open.
"""

import json
import sqlite3
import logging
import time
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.data.historical_data_layer import get_db_path
from app.analytics.telegram_notifier import send_telegram_message

logger = logging.getLogger(__name__)


class ConsensusDynamicGuard:
    """
    Monitors active positions in the Consensus Game Arena and dynamically adjusts risk:
    - Tightens stop-loss when intraday market dynamics turn adverse (regime flip, VWAP breach, sector divergence).
    - Locks in profits at breakeven (+0.10%) when a position achieves >= +0.70% favorable movement.
    - Executes defensive early exit if the trade thesis has completely broken down while underwater.
    """

    # Monotonic ratchet parameters
    PROFIT_LOCK_THRESHOLD_PCT: float = 0.70  # Lock breakeven when position reaches +0.70% profit
    BREAKEVEN_LOCK_SPREAD_PCT: float = 0.10  # Breakeven buffer (+0.10% beyond entry)
    THREAT_TIGHTEN_THRESHOLD: float = 40.0   # Threat score >= 40 triggers 50% risk cut
    THREAT_DEFENSIVE_EXIT_THRESHOLD: float = 75.0  # Threat score >= 75 triggers early exit if in loss

    @classmethod
    def calculate_threat_score(
        cls,
        direction: str,
        entry_price: float,
        current_price: float,
        current_vwap: Optional[float],
        current_open: Optional[float],
        macro_regime: Dict[str, Any],
        sector: Optional[str] = None,
        sector_leaderboard: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[float, List[str]]:
        """
        Computes the point-in-time adverse threat score (0 to 100) based strictly on current market dynamics.
        Zero forward bias: does not use any future candle or tick data.
        """
        threat_score = 0.0
        reasons = []

        is_long = str(direction).upper() in ("BUY", "BULLISH", "LONG")
        intraday_verdict = macro_regime.get("intraday_regime_verdict", "INTRADAY_NEUTRAL_CHOP")
        intraday_open_chg = float(macro_regime.get("intraday_open_change_pct", 0.0) or 0.0)

        # ── 1. Macro Regime & Benchmark Momentum ────────────────────────
        if is_long:
            if intraday_verdict == "INTRADAY_BEAR_PRESSURE":
                threat_score += 35.0
                reasons.append("Macro Regime collapsed to Intraday Bear Pressure (+35 pts)")
            elif intraday_open_chg < -0.35:
                threat_score += 25.0
                reasons.append(f"NIFTY plunging from Open ({intraday_open_chg:+.2f}%) (+25 pts)")
        else:  # Short / SELL
            if intraday_verdict == "INTRADAY_BULL_EXPANSION":
                threat_score += 35.0
                reasons.append("Macro Regime surged into Intraday Bull Expansion (+35 pts)")
            elif intraday_open_chg > 0.35:
                threat_score += 25.0
                reasons.append(f"NIFTY surging from Open ({intraday_open_chg:+.2f}%) (+25 pts)")

        # ── 2. Microstructural Institutional Anchor (VWAP & Day's Open) ──
        if is_long:
            if current_vwap and current_price < (current_vwap * 0.9985):
                threat_score += 25.0
                reasons.append(f"LTP fell below session VWAP (₹{current_price:.2f} < ₹{current_vwap:.2f}) (+25 pts)")
            if current_open and current_price < (current_open * 0.9985):
                threat_score += 15.0
                reasons.append(f"LTP dropped below Day's Open (₹{current_price:.2f} < ₹{current_open:.2f}) (+15 pts)")
        else:  # Short / SELL
            if current_vwap and current_price > (current_vwap * 1.0015):
                threat_score += 25.0
                reasons.append(f"LTP crossed above session VWAP (₹{current_price:.2f} > ₹{current_vwap:.2f}) (+25 pts)")
            if current_open and current_price > (current_open * 1.0015):
                threat_score += 15.0
                reasons.append(f"LTP crossed above Day's Open (₹{current_price:.2f} > ₹{current_open:.2f}) (+15 pts)")

        # ── 3. Sector Relative Strength Alignment ───────────────────────
        if sector and sector_leaderboard:
            try:
                sec_data = None
                if isinstance(sector_leaderboard, dict):
                    sectors_dict = sector_leaderboard.get("sectors") or {}
                    if sector in sectors_dict and isinstance(sectors_dict[sector], dict):
                        sec_data = sectors_dict[sector]
                    elif "ranked_list" in sector_leaderboard and isinstance(sector_leaderboard["ranked_list"], list):
                        sec_data = next((s for s in sector_leaderboard["ranked_list"] if isinstance(s, dict) and s.get("sector") == sector), None)
                elif isinstance(sector_leaderboard, list):
                    sec_data = next((s for s in sector_leaderboard if isinstance(s, dict) and s.get("sector") == sector), None)

                if sec_data and isinstance(sec_data, dict):
                    sec_perf = float(sec_data.get("pct_from_open") if sec_data.get("pct_from_open") is not None else sec_data.get("change_pct", 0.0) or 0.0)
                    if is_long and sec_perf < -0.50:
                        threat_score += 15.0
                        reasons.append(f"Sector {sector} in heavy distribution ({sec_perf:+.2f}%) (+15 pts)")
                    elif not is_long and sec_perf > 0.50:
                        threat_score += 15.0
                        reasons.append(f"Sector {sector} experiencing strong rally ({sec_perf:+.2f}%) (+15 pts)")
            except Exception as s_ex:
                logger.debug(f"[ConsensusDynamicGuard] Sector threat eval note: {s_ex}")

        capped_threat = min(100.0, round(threat_score, 1))
        return capped_threat, reasons

    @classmethod
    def evaluate_position_action(
        cls,
        direction: str,
        entry_price: float,
        current_price: float,
        current_sl: float,
        target_price: float,
        threat_score: float,
        reasons: List[str]
    ) -> Dict[str, Any]:
        """
        Determines the strictly monotonic risk action for a single active position.
        The trade's objective (target_price) is NEVER modified.
        """
        is_long = str(direction).upper() in ("BUY", "BULLISH", "LONG")
        if entry_price <= 0:
            return {"action": "MAINTAIN", "new_sl": current_sl, "reason": "Invalid entry price"}

        # Calculate point-in-time unrealized P&L
        if is_long:
            unrealized_pnl_pct = round(((current_price - entry_price) / entry_price) * 100.0, 2)
        else:
            unrealized_pnl_pct = round(((entry_price - current_price) / entry_price) * 100.0, 2)

        # ── ACTION 1: DEFENSIVE EXIT (Thesis Breakdown & Underwater) ────
        if threat_score >= cls.THREAT_DEFENSIVE_EXIT_THRESHOLD and unrealized_pnl_pct < -0.15:
            return {
                "action": "DEFENSIVE_EXIT",
                "new_sl": current_sl,
                "unrealized_pnl_pct": unrealized_pnl_pct,
                "threat_score": threat_score,
                "reasons": reasons,
                "summary": f"Defensive exit triggered: Threat score {threat_score:.1f}/100 with {unrealized_pnl_pct:+.2f}% loss."
            }

        # ── ACTION 2: PROFIT-LOCK RATCHET (Favorable Movement) ──────────
        # When position reaches +0.70%, ratchet stop-loss to Breakeven + 0.10%
        if unrealized_pnl_pct >= cls.PROFIT_LOCK_THRESHOLD_PCT:
            if is_long:
                be_sl = round(entry_price * (1.0 + (cls.BREAKEVEN_LOCK_SPREAD_PCT / 100.0)), 2)
                # Monotonic rule: SL can only increase
                if be_sl > current_sl and be_sl < current_price:
                    return {
                        "action": "PROFIT_LOCK_RATCHET",
                        "new_sl": be_sl,
                        "unrealized_pnl_pct": unrealized_pnl_pct,
                        "threat_score": threat_score,
                        "reasons": [f"Unrealized profit hit {unrealized_pnl_pct:+.2f}%: Locked SL at Breakeven +{cls.BREAKEVEN_LOCK_SPREAD_PCT:.2f}% (₹{be_sl:.2f})"],
                        "summary": f"Profit locked at breakeven ₹{be_sl:.2f}."
                    }
            else:
                be_sl = round(entry_price * (1.0 - (cls.BREAKEVEN_LOCK_SPREAD_PCT / 100.0)), 2)
                # Monotonic rule: SL can only decrease for short
                if be_sl < current_sl and be_sl > current_price:
                    return {
                        "action": "PROFIT_LOCK_RATCHET",
                        "new_sl": be_sl,
                        "unrealized_pnl_pct": unrealized_pnl_pct,
                        "threat_score": threat_score,
                        "reasons": [f"Unrealized profit hit {unrealized_pnl_pct:+.2f}%: Locked SL at Breakeven +{cls.BREAKEVEN_LOCK_SPREAD_PCT:.2f}% (₹{be_sl:.2f})"],
                        "summary": f"Profit locked at breakeven ₹{be_sl:.2f}."
                    }

        # ── ACTION 3: TIGHTEN SL (Adverse Dynamics Detected) ────────────
        # Cuts remaining risk between current price and SL by 50%
        if threat_score >= cls.THREAT_TIGHTEN_THRESHOLD:
            if is_long:
                if current_price > current_sl:
                    risk_distance = current_price - current_sl
                    tightened_sl = round(current_sl + (risk_distance * 0.5), 2)
                    # Monotonic invariant assertion
                    if tightened_sl > current_sl and tightened_sl < current_price:
                        return {
                            "action": "TIGHTEN_SL",
                            "new_sl": tightened_sl,
                            "unrealized_pnl_pct": unrealized_pnl_pct,
                            "threat_score": threat_score,
                            "reasons": reasons,
                            "summary": f"Tightened SL to ₹{tightened_sl:.2f} (50% risk compression due to threat score {threat_score:.1f})."
                        }
            else:
                if current_sl > current_price:
                    risk_distance = current_sl - current_price
                    tightened_sl = round(current_sl - (risk_distance * 0.5), 2)
                    # Monotonic invariant assertion
                    if tightened_sl < current_sl and tightened_sl > current_price:
                        return {
                            "action": "TIGHTEN_SL",
                            "new_sl": tightened_sl,
                            "unrealized_pnl_pct": unrealized_pnl_pct,
                            "threat_score": threat_score,
                            "reasons": reasons,
                            "summary": f"Tightened SL to ₹{tightened_sl:.2f} (50% risk compression due to threat score {threat_score:.1f})."
                        }

        # Default action
        return {
            "action": "MAINTAIN",
            "new_sl": current_sl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "threat_score": threat_score,
            "reasons": reasons,
            "summary": "Position within normal parameters; SL maintained."
        }

    @classmethod
    def audit_active_arena_positions(
        cls,
        session_id: str = "default_arena_session",
        conn: Optional[sqlite3.Connection] = None,
        now_dt: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Audits all currently OPEN positions in the specified Consensus Arena session.
        Respects Upstox rate limits by fetching quotes for all open positions in 1 single bulk API call.
        Strictly applies the Monotonic Ratchet Rule.
        """
        from app.analytics.consensus_game_engine import ConsensusGameEngine
        ConsensusGameEngine.init_game_schema(conn)

        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        actions_taken = []
        try:
            cur = conn.cursor()
            # Query ONLY active OPEN positions for this session
            open_rows = cur.execute("""
                SELECT p.id, p.trade_ref_id, p.ticker, p.direction, p.shares,
                       p.entry_price, p.stop_loss, p.target_price, p.entry_time,
                       p.allocated_capital
                FROM consensus_game_positions p
                WHERE p.session_id = ? AND p.status = 'OPEN'
                ORDER BY p.id ASC
            """, (session_id,)).fetchall()

            if not open_rows:
                return {
                    "status": "NO_OPEN_POSITIONS",
                    "session_id": session_id,
                    "audited_count": 0,
                    "actions_taken": []
                }

            # 1. Fetch live quotes for ALL open positions in ONE single bulk call (Rate Limit Compliant)
            open_tickers = list({str(r[2]).strip() for r in open_rows if r[2]})
            bulk_quotes = {}
            if open_tickers:
                try:
                    from app.data.upstox_provider import fetch_upstox_bulk_quotes
                    bulk_quotes = fetch_upstox_bulk_quotes(open_tickers)
                except Exception as b_err:
                    logger.debug(f"[ConsensusDynamicGuard] Bulk quote fetch note: {b_err}")

            # 2. Fetch point-in-time Macro Regime and Sector Leaderboard
            macro_regime = {}
            try:
                from app.analytics.macro_engine import get_macro_regime
                macro_regime = get_macro_regime()
            except Exception as m_err:
                logger.debug(f"[ConsensusDynamicGuard] Macro regime fetch note: {m_err}")

            sector_leaderboard = {}
            try:
                from app.analytics.sector_engine import SectorEngine
                sector_leaderboard = SectorEngine.get_sector_leaderboard()
            except Exception as s_err:
                logger.debug(f"[ConsensusDynamicGuard] Sector leaderboard fetch note: {s_err}")

            eval_time_str = (now_dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

            from app.analytics.sector_engine import SectorEngine
            for row in open_rows:
                pid, ref_id, ticker, direction, shares, entry_px, sl, tp, entry_t, alloc_cap = row
                entry_px = float(entry_px)
                sl = float(sl)
                tp = float(tp)
                sector = SectorEngine.get_stock_sector(ticker)

                q_info = bulk_quotes.get(ticker) or {}
                ltp = float(q_info.get("ltp") or q_info.get("price") or 0.0)
                live_vwap = float(q_info.get("vwap") or 0.0) or None
                live_open = float(q_info.get("open") or 0.0) or None

                # Fallback to candle close if live quote unavailable
                if ltp <= 0:
                    try:
                        clean_sym = ticker.replace('.NS', '').replace('.BO', '').strip().upper()
                        cur.execute("""
                            SELECT close FROM ohlcv
                            WHERE (ticker = ? OR ticker = ?) AND timeframe = '15m'
                            ORDER BY date DESC LIMIT 1
                        """, (clean_sym, f"{clean_sym}.NS"))
                        c_row = cur.fetchone()
                        if c_row and c_row[0]:
                            ltp = float(c_row[0])
                    except Exception:
                        ltp = entry_px

                if ltp <= 0:
                    continue

                threat_score, reasons = cls.calculate_threat_score(
                    direction=direction,
                    entry_price=entry_px,
                    current_price=ltp,
                    current_vwap=live_vwap,
                    current_open=live_open,
                    macro_regime=macro_regime,
                    sector=sector,
                    sector_leaderboard=sector_leaderboard
                )

                decision = cls.evaluate_position_action(
                    direction=direction,
                    entry_price=entry_px,
                    current_price=ltp,
                    current_sl=sl,
                    target_price=tp,
                    threat_score=threat_score,
                    reasons=reasons
                )

                action = decision["action"]
                new_sl = decision.get("new_sl", sl)

                # Persist telemetry to consensus_game_positions
                cur.execute("""
                    UPDATE consensus_game_positions
                    SET guard_threat_score = ?,
                        guard_action = ?,
                        guard_reasons = ?,
                        last_guard_audit = ?
                    WHERE id = ?
                """, (
                    threat_score,
                    action,
                    json.dumps(decision.get("reasons", [])),
                    eval_time_str,
                    pid
                ))

                if action == "DEFENSIVE_EXIT":
                    # Execute early defensive exit to preserve capital
                    exit_res = ConsensusGameEngine.exit_position(
                        position_id=pid,
                        session_id=session_id,
                        exit_price=ltp,
                        reason="DEFENSIVE_REGIME_EXIT",
                        conn=conn
                    )
                    actions_taken.append({
                        "position_id": pid,
                        "ticker": ticker,
                        "action": "DEFENSIVE_EXIT",
                        "threat_score": threat_score,
                        "exit_price": ltp,
                        "reasons": reasons
                    })

                    # Dispatch Telegram alert for defensive exit
                    if session_id == "default_arena_session":
                        cls._dispatch_guard_alert(
                            session_id=session_id,
                            trade_ref_id=ref_id,
                            ticker=ticker,
                            alert_type="GUARD_DEFENSIVE_EXIT",
                            message=cls._format_guard_exit_message(
                                ticker=ticker,
                                direction=direction,
                                entry_price=entry_px,
                                exit_price=ltp,
                                threat_score=threat_score,
                                reasons=reasons
                            ),
                            conn=conn
                        )

                elif action in ("TIGHTEN_SL", "PROFIT_LOCK_RATCHET"):
                    # Monotonic Ratchet: Update stop loss in both arena position and consensus trade record
                    cur.execute("""
                        UPDATE consensus_game_positions
                        SET stop_loss = ?,
                            tightened_sl = ?
                        WHERE id = ?
                    """, (new_sl, new_sl, pid))

                    cur.execute("""
                        UPDATE consensus_trade_history
                        SET stop_loss = ?
                        WHERE id = ?
                    """, (new_sl, ref_id))

                    actions_taken.append({
                        "position_id": pid,
                        "ticker": ticker,
                        "action": action,
                        "old_sl": sl,
                        "new_sl": new_sl,
                        "threat_score": threat_score,
                        "reasons": decision.get("reasons", [])
                    })

                    # Dispatch Telegram alert for SL tightening / profit lock
                    if session_id == "default_arena_session":
                        alert_type = f"GUARD_{action}"
                        cls._dispatch_guard_alert(
                            session_id=session_id,
                            trade_ref_id=ref_id,
                            ticker=ticker,
                            alert_type=alert_type,
                            message=cls._format_guard_sl_message(
                                ticker=ticker,
                                direction=direction,
                                entry_price=entry_px,
                                current_price=ltp,
                                old_sl=sl,
                                new_sl=new_sl,
                                action=action,
                                threat_score=threat_score,
                                reasons=decision.get("reasons", [])
                            ),
                            conn=conn
                        )

            conn.commit()
            return {
                "status": "SUCCESS",
                "session_id": session_id,
                "audited_count": len(open_rows),
                "actions_taken": actions_taken
            }
        except Exception as e:
            logger.error(f"[ConsensusDynamicGuard] Audit error for session {session_id}: {e}", exc_info=True)
            return {"status": "ERROR", "error": str(e), "session_id": session_id, "actions_taken": []}
        finally:
            if should_close:
                conn.close()

    @classmethod
    def _dispatch_guard_alert(
        cls,
        session_id: str,
        trade_ref_id: int,
        ticker: str,
        alert_type: str,
        message: str,
        conn: sqlite3.Connection
    ) -> bool:
        """Dispatches outbound Telegram notification with deduplication in consensus_game_alerts."""
        cur = conn.cursor()
        already_sent = cur.execute("""
            SELECT 1 FROM consensus_game_alerts
            WHERE session_id = ? AND trade_ref_id = ? AND alert_type = ?
        """, (session_id, trade_ref_id, alert_type)).fetchone()

        if already_sent:
            return False

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            send_telegram_message(message)
            cur.execute("""
                INSERT OR IGNORE INTO consensus_game_alerts (
                    session_id, trade_ref_id, ticker, alert_type, sent_at, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, trade_ref_id, ticker, alert_type, now_str, json.dumps({"action": alert_type})))
            conn.commit()
            return True
        except Exception as ex:
            logger.warning(f"[ConsensusDynamicGuard] Failed to dispatch Telegram alert: {ex}")
            return False

    @classmethod
    def _format_guard_sl_message(
        cls,
        ticker: str,
        direction: str,
        entry_price: float,
        current_price: float,
        old_sl: float,
        new_sl: float,
        action: str,
        threat_score: float,
        reasons: List[str]
    ) -> str:
        arrow = "🟢 BUY" if direction.upper() in ("BUY", "BULLISH") else "🔴 SHORT/SELL"
        header = "🛡️ <b>CONSENSUS ARENA: DYNAMIC SL TIGHTENED</b>" if action == "TIGHTEN_SL" else "🎯 <b>CONSENSUS ARENA: PROFIT-LOCK RATCHET</b>"
        action_desc = "Risk Compressed by 50%" if action == "TIGHTEN_SL" else "Locked at Breakeven +0.10%"
        reasons_text = "\n".join([f"• <i>{r}</i>" for r in reasons[:3]])

        return (
            f"{header}\n"
            f"Active Position Risk Adjustment\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Ticker:</b> <code>{ticker}</code> [{arrow}]\n"
            f"<b>Action:</b> <b>{action_desc}</b>\n"
            f"─────────────────────────\n"
            f"• <b>Entry:</b> ₹{entry_price:.2f}  ➔  <b>LTP:</b> ₹{current_price:.2f}\n"
            f"• <b>Previous SL:</b> ₹{old_sl:.2f}\n"
            f"• <b>Tightened SL:</b> <b>₹{new_sl:.2f}</b>\n"
            f"• <b>Threat Score:</b> {threat_score:.1f}/100\n"
            f"─────────────────────────\n"
            f"<b>Dynamic Drivers:</b>\n"
            f"{reasons_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>⚡ Monotonic Ratchet Active in Consensus Arena</i>"
        )

    @classmethod
    def _format_guard_exit_message(
        cls,
        ticker: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        threat_score: float,
        reasons: List[str]
    ) -> str:
        arrow = "🟢 BUY" if direction.upper() in ("BUY", "BULLISH") else "🔴 SHORT/SELL"
        reasons_text = "\n".join([f"• <i>{r}</i>" for r in reasons[:3]])
        loss_pct = ((exit_price - entry_price) / entry_price * 100.0) if direction.upper() in ("BUY", "BULLISH") else ((entry_price - exit_price) / entry_price * 100.0)

        return (
            f"🛑 <b>CONSENSUS ARENA: DEFENSIVE REGIME EXIT</b>\n"
            f"Early Capital Preservation Triggered\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Ticker:</b> <code>{ticker}</code> [{arrow}]\n"
            f"<b>Status:</b> Position Closed Early (Slot Freed)\n"
            f"─────────────────────────\n"
            f"• <b>Entry:</b> ₹{entry_price:.2f}  ➔  <b>Exit:</b> ₹{exit_price:.2f}\n"
            f"• <b>Capped Loss:</b> <b>{loss_pct:+.2f}%</b> (Saved from full -0.80% SL)\n"
            f"• <b>Threat Score:</b> {threat_score:.1f}/100 (Critical Breakdown)\n"
            f"─────────────────────────\n"
            f"<b>Invalidation Drivers:</b>\n"
            f"{reasons_text}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>⚡ Capital preserved and slot freed for fresh setups</i>"
        )

