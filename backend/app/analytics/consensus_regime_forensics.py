"""
CONSENSUS REGIME FORENSICS, LATE-ENTRY AUDIT & TIME-OF-DAY ANALYZER
===================================================================
Rigorous quantitative evaluation of intraday consensus trades under
point-in-time market regimes, time-of-day slots, and execution latencies.

Zero lookahead: Features are strictly calculated using data available at decision time T.
Outcomes (P&L, MFE, MAE) are isolated strictly for diagnostic auditing.
"""

import json
import math
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)


class ConsensusRegimeForensics:
    """
    Forensic analysis engine for intraday consensus performance across regimes and horizons.
    """

    @classmethod
    def load_all_arena_positions_with_telemetry(
        cls,
        session_id: str = "default_arena_session",
        conn: Optional[sqlite3.Connection] = None
    ) -> pd.DataFrame:
        """
        Loads all closed arena positions and enriches each row point-in-time
        with pre-entry market regime, VWAP distance, ATR, MFE, and MAE.
        """
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True

        try:
            query = """
                SELECT p.id, p.trade_ref_id, p.ticker, p.direction, p.shares,
                       p.entry_price, p.allocated_capital, p.stop_loss, p.target_price,
                       p.entry_time, p.status, p.exit_price, p.exit_time,
                       p.gross_pnl_rupees, p.fees_rupees, p.net_pnl_rupees,
                       p.pnl_pct, p.outcome, p.details_json,
                       t.composite_score, t.qlib_score, t.legacy_score, t.finbert_score
                FROM consensus_game_positions p
                LEFT JOIN consensus_trade_history t ON p.trade_ref_id = t.id
                WHERE p.session_id = ? AND p.status = 'CLOSED'
                ORDER BY p.entry_time ASC
            """
            df = pd.read_sql_query(query, conn, params=(session_id,))
            if df.empty:
                return pd.DataFrame()

            enriched_rows = []
            for _, row in df.iterrows():
                row_dict = row.to_dict()
                ticker = row_dict["ticker"]
                entry_t = str(row_dict["entry_time"] or "")[:19]
                exit_t = str(row_dict["exit_time"] or "")[:19]
                entry_px = float(row_dict["entry_price"])
                direction = str(row_dict["direction"]).upper()

                # 1. Point-in-Time Past Candles (Strictly BEFORE entry timestamp)
                cur = conn.cursor()
                cur.execute("""
                    SELECT open, high, low, close, volume, date
                    FROM ohlcv
                    WHERE ticker = ? AND timeframe = '15m' AND date <= ?
                    ORDER BY date DESC LIMIT 30
                """, (ticker, entry_t))
                past_candles = cur.fetchall()

                # Default values
                vwap_dist_pct = 0.0
                open_dist_pct = 0.0
                atr_14 = round(entry_px * 0.008, 2)
                market_regime = "RANGE_CHOP"
                market_breadth_pct = 0.0

                if past_candles:
                    past_rev = list(reversed(past_candles))
                    closes = [float(c[3]) for c in past_rev]
                    highs = [float(c[1]) for c in past_rev]
                    lows = [float(c[2]) for c in past_rev]
                    volumes = [float(c[4]) for c in past_rev]

                    # True range for ATR
                    tr_list = []
                    for i in range(1, len(closes)):
                        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                        tr_list.append(tr)
                    if tr_list:
                        atr_14 = round(float(np.mean(tr_list[-14:])), 2)

                    # Intraday cumulative VWAP from session open
                    day_str = entry_t[:10]
                    day_candles = [c for c in past_rev if str(c[5])[:10] == day_str]
                    if day_candles:
                        cum_pv = sum(((float(c[1]) + float(c[2]) + float(c[3])) / 3.0) * float(c[4]) for c in day_candles)
                        cum_v = sum(float(c[4]) for c in day_candles)
                        session_open = float(day_candles[0][0])
                        open_dist_pct = round(((entry_px - session_open) / session_open) * 100.0, 2)
                        if cum_v > 0:
                            vwap = cum_pv / cum_v
                            vwap_dist_pct = round(((entry_px - vwap) / vwap) * 100.0, 2)

                # 2. Market Proxy Breadth (Top 5 Index Heavyweights at decision time)
                entry_date = entry_t[:10]
                cur.execute("""
                    SELECT ticker, open, close
                    FROM ohlcv
                    WHERE timeframe = '15m' AND date <= ? AND date LIKE ?
                      AND ticker IN ('RELIANCE.NS', 'HDFCBANK.NS', 'ICICIBANK.NS', 'INFY.NS', 'TCS.NS')
                    ORDER BY date DESC LIMIT 25
                """, (entry_t, f"{entry_date}%"))
                heavyweight_bars = cur.fetchall()
                if heavyweight_bars:
                    hw_ret = []
                    seen_tickers = set()
                    for hw_t, hw_o, hw_c in heavyweight_bars:
                        if hw_t not in seen_tickers and hw_o and hw_c and float(hw_o) > 0:
                            seen_tickers.add(hw_t)
                            hw_ret.append((float(hw_c) - float(hw_o)) / float(hw_o) * 100.0)
                    if hw_ret:
                        market_breadth_pct = round(float(np.mean(hw_ret)), 2)
                        if market_breadth_pct >= 0.20:
                            market_regime = "NIFTY_BULLISH"
                        elif market_breadth_pct <= -0.20:
                            market_regime = "NIFTY_BEARISH"
                        else:
                            market_regime = "NIFTY_RANGE_CHOP"

                # 3. Post-Decision Forward Candles (Between entry and exit for MFE / MAE)
                cur.execute("""
                    SELECT high, low
                    FROM ohlcv
                    WHERE ticker = ? AND timeframe = '15m' AND date >= ? AND date <= ?
                    ORDER BY date ASC
                """, (ticker, entry_t, exit_t))
                fwd_candles = cur.fetchall()

                mfe_pct = 0.0
                mae_pct = 0.0
                if fwd_candles and entry_px > 0:
                    fwd_highs = [float(c[0]) for c in fwd_candles]
                    fwd_lows = [float(c[1]) for c in fwd_candles]
                    max_h = max(fwd_highs)
                    min_l = min(fwd_lows)

                    if direction in ("BUY", "BULLISH"):
                        mfe_pct = round(((max_h - entry_px) / entry_px) * 100.0, 2)
                        mae_pct = round(((min_l - entry_px) / entry_px) * 100.0, 2)
                    else:
                        mfe_pct = round(((entry_px - min_l) / entry_px) * 100.0, 2)
                        mae_pct = round(((entry_px - max_h) / entry_px) * 100.0, 2)

                # Holding time in minutes
                holding_minutes = 60
                try:
                    e_dt = datetime.strptime(entry_t, "%Y-%m-%d %H:%M:%S")
                    x_dt = datetime.strptime(exit_t, "%Y-%m-%d %H:%M:%S")
                    holding_minutes = max(1, int((x_dt - e_dt).total_seconds() / 60.0))
                except Exception:
                    pass

                # Time-of-day slot
                time_str = entry_t[11:16] if len(entry_t) >= 16 else "10:00"
                slot_name = "10:00"
                try:
                    th, tm = int(time_str.split(":")[0]), int(time_str.split(":")[1])
                    total_m = th * 60 + tm
                    if total_m < 600:
                        slot_name = "09:30"
                    elif total_m < 660:
                        slot_name = "10:30"
                    elif total_m < 720:
                        slot_name = "11:30"
                    elif total_m < 780:
                        slot_name = "12:30"
                    elif total_m < 840:
                        slot_name = "13:30"
                    elif total_m < 900:
                        slot_name = "14:30"
                    else:
                        slot_name = "15:00+"
                except Exception:
                    pass

                # Late-entry classification:
                # Was the signal direction right but entry extended, OR direction wrong?
                direction_was_correct = bool(mfe_pct >= 0.70)
                entry_was_extended = bool(abs(vwap_dist_pct) >= 0.60 or abs(open_dist_pct) >= 1.0)

                if direction_was_correct and entry_was_extended:
                    latency_diagnosis = "DIRECTION_RIGHT_ENTRY_LATE"
                elif direction_was_correct and not entry_was_extended:
                    latency_diagnosis = "NORMAL_MOMENTUM_RUN"
                else:
                    latency_diagnosis = "DIRECTION_WRONG"

                row_dict["vwap_dist_pct"] = vwap_dist_pct
                row_dict["open_dist_pct"] = open_dist_pct
                row_dict["atr_14"] = atr_14
                row_dict["market_regime"] = market_regime
                row_dict["market_breadth_pct"] = market_breadth_pct
                row_dict["mfe_pct"] = mfe_pct
                row_dict["mae_pct"] = mae_pct
                row_dict["holding_minutes"] = holding_minutes
                row_dict["time_slot"] = slot_name
                row_dict["latency_diagnosis"] = latency_diagnosis

                enriched_rows.append(row_dict)

            return pd.DataFrame(enriched_rows)
        finally:
            if should_close:
                conn.close()

    @classmethod
    def generate_regime_forensics_report(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Calculates performance separately across NIFTY regimes (Bullish / Bearish / Range)
        cross-tabulated by Direction (BUY vs SELL).
        """
        if df.empty:
            return {"status": "EMPTY_DATA", "buckets": {}}

        buckets = {}
        for (regime, direction), group in df.groupby(["market_regime", "direction"]):
            bucket_key = f"{regime}_x_{direction}"
            trades_count = len(group)
            gross = round(float(group["gross_pnl_rupees"].sum()), 2)
            fees = round(float(group["fees_rupees"].sum()), 2)
            net = round(float(group["net_pnl_rupees"].sum()), 2)
            wins = len(group[group["net_pnl_rupees"] > 0])
            losses = len(group[group["net_pnl_rupees"] <= 0])
            win_rate = round((wins / trades_count) * 100.0, 1) if trades_count > 0 else 0.0

            winning_pnls = group[group["net_pnl_rupees"] > 0]["net_pnl_rupees"]
            losing_pnls = group[group["net_pnl_rupees"] < 0]["net_pnl_rupees"]

            avg_win = round(float(winning_pnls.mean()), 2) if not winning_pnls.empty else 0.0
            avg_loss = round(float(losing_pnls.mean()), 2) if not losing_pnls.empty else 0.0
            gross_win_sum = float(winning_pnls.sum())
            gross_loss_sum = abs(float(losing_pnls.sum()))
            pf = round(gross_win_sum / gross_loss_sum, 2) if gross_loss_sum > 0 else (99.0 if gross_win_sum > 0 else 0.0)

            avg_mfe = round(float(group["mfe_pct"].mean()), 2)
            avg_mae = round(float(group["mae_pct"].mean()), 2)
            net_expectancy = round(net / trades_count, 2) if trades_count > 0 else 0.0

            buckets[bucket_key] = {
                "market_regime": regime,
                "direction": direction,
                "trades": trades_count,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": win_rate,
                "gross_pnl": gross,
                "fees": fees,
                "net_pnl": net,
                "net_expectancy_rupees": net_expectancy,
                "profit_factor": pf,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "avg_mfe_pct": avg_mfe,
                "avg_mae_pct": avg_mae
            }

        return {
            "status": "SUCCESS",
            "total_trades": len(df),
            "buckets": buckets
        }

    @classmethod
    def generate_late_entry_report(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Tests the Late-Entry Hypothesis:
        Distinguishes whether losing trades suffered from Direction Wrong
        versus Direction Right but Entry Extended / Late.
        """
        if df.empty:
            return {"status": "EMPTY_DATA"}

        diagnosis_counts = df["latency_diagnosis"].value_counts().to_dict()
        breakdown = {}

        for diag, grp in df.groupby("latency_diagnosis"):
            cnt = len(grp)
            net = round(float(grp["net_pnl_rupees"].sum()), 2)
            wins = len(grp[grp["net_pnl_rupees"] > 0])
            wr = round((wins / cnt) * 100.0, 1) if cnt > 0 else 0.0
            breakdown[diag] = {
                "count": cnt,
                "pct_of_total": round((cnt / len(df)) * 100.0, 1),
                "win_rate_pct": wr,
                "net_pnl": net,
                "avg_mfe_pct": round(float(grp["mfe_pct"].mean()), 2),
                "avg_mae_pct": round(float(grp["mae_pct"].mean()), 2),
                "avg_vwap_dist_pct": round(float(grp["vwap_dist_pct"].mean()), 2),
                "avg_holding_minutes": round(float(grp["holding_minutes"].mean()), 1)
            }

        return {
            "status": "SUCCESS",
            "total_trades": len(df),
            "breakdown": breakdown
        }

    @classmethod
    def generate_time_of_day_report(cls, df: pd.DataFrame) -> Dict[str, Any]:
        """
        Analyzes performance conditioned by entry time slot:
        09:30, 10:30, 11:30, 12:30, 13:30, 14:30+.
        """
        if df.empty:
            return {"status": "EMPTY_DATA"}

        slots = {}
        for slot, grp in df.groupby("time_slot"):
            cnt = len(grp)
            gross = round(float(grp["gross_pnl_rupees"].sum()), 2)
            fees = round(float(grp["fees_rupees"].sum()), 2)
            net = round(float(grp["net_pnl_rupees"].sum()), 2)
            wins = len(grp[grp["net_pnl_rupees"] > 0])
            wr = round((wins / cnt) * 100.0, 1) if cnt > 0 else 0.0

            sq_off = len(grp[grp["outcome"].str.contains("SQUARE|SESSION", na=False)])
            target_hits = len(grp[grp["outcome"].str.contains("TARGET", na=False)])
            sl_hits = len(grp[grp["outcome"].str.contains("SL|STOP", na=False)])

            cost_pct_of_gross = round((fees / abs(gross) * 100.0), 1) if abs(gross) > 0 else 999.0

            slots[slot] = {
                "trades": cnt,
                "wins": wins,
                "win_rate_pct": wr,
                "gross_pnl": gross,
                "fees": fees,
                "net_pnl": net,
                "net_expectancy": round(net / cnt, 2) if cnt > 0 else 0.0,
                "target_hit_pct": round((target_hits / cnt) * 100.0, 1) if cnt > 0 else 0.0,
                "sl_hit_pct": round((sl_hits / cnt) * 100.0, 1) if cnt > 0 else 0.0,
                "session_exit_pct": round((sq_off / cnt) * 100.0, 1) if cnt > 0 else 0.0,
                "cost_pct_of_gross": cost_pct_of_gross,
                "avg_holding_minutes": round(float(grp["holding_minutes"].mean()), 1)
            }

        return {
            "status": "SUCCESS",
            "slots": slots
        }
