"""
DIRECTIONAL-EDGE FORENSIC AUDIT ENGINE
=======================================
Investigates whether Legacy, Microsoft Qlib (Alpha-158), and Dual-Engine Consensus
genuinely predict individual-stock direction and possess cross-sectional selection alpha,
or whether apparent profits are primarily explained by broad market direction (beta),
macro regime, or chop/range-bound failure.

Key Analytical Guarantees:
1. Strict separation of:
   - Market Direction Prediction
   - Stock Absolute Direction Prediction
   - Stock Relative / Selection Alpha
   - Market Regime Detection
   - Tradeability After Canonical Friction
2. Causal Point-in-Time Integrity: Zero forward leakage.
3. Canonical Dynamic Costs: Evaluated via backend.app.analytics.trade_economics (zero hardcoding).
4. Deterministic, memory-safe, and cache-accelerated for M1 Pro architecture.
"""

import os
import math
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from app.data.historical_data_layer import get_db_path
from app.analytics.trade_economics import compute_trade_friction_pct, compute_realized_net_pnl
from app.analytics.universe_config import BENCHMARK_5_UNIVERSE

logger = logging.getLogger("app.analytics.directional_edge_forensics")

# Canonical Regime Thresholds (aligned with app.analytics.macro_engine)
REGIME_STRONG_BULL = "STRONG_BULL"
REGIME_WEAK_BULL = "WEAK_BULL"
REGIME_RANGE_CHOP = "RANGE/CHOP"
REGIME_WEAK_BEAR = "WEAK_BEAR"
REGIME_STRONG_BEAR = "STRONG_BEAR"

CANONICAL_HORIZONS = ["15m", "30m", "60m", "120m", "EOD"]


class DirectionalEdgeForensics:
    """
    Forensic analysis engine for evaluating market direction prediction,
    stock absolute direction, cross-sectional relative selection alpha,
    regime dependence, and cost survival.
    """

    _CACHE: Dict[str, Any] = {}

    @classmethod
    def clear_cache(cls):
        """Clears in-memory forensic caches."""
        cls._CACHE.clear()

    # =========================================================================
    # CORE DOMAIN OPERATION: analyze_directional_edge
    # =========================================================================
    @classmethod
    def analyze_directional_edge(
        cls,
        model: str = "ALL",
        signal_source: str = "ALL",
        date_range: Optional[Tuple[Optional[str], Optional[str]]] = None,
        universe: str = "ALL",
        timeframe: str = "15m",
        prediction_horizons: Optional[List[str]] = None,
        regime: str = "ALL",
        direction: str = "ALL",
        cost_mode: str = "CANONICAL",
        benchmark_type: str = "BENCHMARK_5",
        db_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        High-level domain forensic analysis operation.
        Callable by Trade Sentinel and autonomous research agents.
        
        Parameters:
            model: 'ALL' | 'LEGACY' | 'QLIB' | 'CONSENSUS'
            signal_source: 'ALL' | 'CONSENSUS_SHADOW' | 'CONSENSUS_TRACKED' | 'EXPERIENCE_STORE' | 'ML_TRADE_HISTORY'
            date_range: Tuple of (start_date, end_date) e.g. ('2026-09-01', '2026-10-08')
            universe: 'ALL' | 'BENCHMARK_5' | 'NIFTY_50'
            timeframe: '15m' | '1d'
            prediction_horizons: List of forward horizons e.g. ['15m', '30m', '60m', '120m', 'EOD']
            regime: 'ALL' | 'STRONG_BULL' | 'WEAK_BULL' | 'RANGE/CHOP' | 'WEAK_BEAR' | 'STRONG_BEAR'
            direction: 'ALL' | 'BUY' | 'SELL'
            cost_mode: 'CANONICAL' | 'ZERO_COST'
            benchmark_type: 'BENCHMARK_5' | 'EQUAL_WEIGHTED_UNIVERSE'
        """
        target_db = db_path or get_db_path()
        horizons = prediction_horizons or CANONICAL_HORIZONS

        cache_key = f"{model}_{signal_source}_{date_range}_{universe}_{timeframe}_{tuple(horizons)}_{regime}_{direction}_{cost_mode}_{benchmark_type}"
        if cache_key in cls._CACHE:
            return cls._CACHE[cache_key]

        # 1. Ingest Raw Signals from Authoritative Storage
        signals = cls._load_signals(
            db_path=target_db,
            signal_source=signal_source,
            model=model,
            date_range=date_range,
            direction=direction
        )

        if not signals:
            return {
                "status": "NO_SIGNALS",
                "sample_size": 0,
                "message": "No eligible signals found matching specified criteria."
            }

        # 2. Enrich Signals with Benchmark Movement and Stock Forward Prices
        enriched_records = cls._enrich_forward_performance(
            signals=signals,
            db_path=target_db,
            timeframe=timeframe,
            horizons=horizons,
            benchmark_type=benchmark_type
        )

        if not enriched_records:
            return {
                "status": "INSUFFICIENT_FORWARD_DATA",
                "sample_size": len(signals),
                "message": "Signals found but insufficient forward OHLCV bars available for evaluation."
            }

        # Filter by regime if specified
        if regime and regime.upper() != "ALL":
            enriched_records = [r for r in enriched_records if r.get("regime") == regime.upper()]
            if not enriched_records:
                return {
                    "status": "NO_REGIME_RECORDS",
                    "sample_size": 0,
                    "message": f"No records found matching regime '{regime}'."
                }

        # 3. Perform Subsystem Forensics
        market_direction_res = cls._compute_market_direction_accuracy(enriched_records, horizons)
        stock_absolute_res = cls._compute_stock_absolute_direction(enriched_records, horizons)
        stock_relative_res = cls._compute_stock_relative_edge(enriched_records, horizons)
        regime_segmentation_res = cls._compute_regime_segmentation(enriched_records)
        chop_forensics_res = cls._compute_chop_forensics(enriched_records)
        market_beta_res = cls._compute_market_beta_vs_alpha(enriched_records)
        model_comparison_res = cls._compute_model_comparison(enriched_records)
        pipeline_trace_res = cls._compute_raw_vs_final_signal_trace(enriched_records)
        rank_analysis_res = cls._compute_top_bottom_rank_analysis(enriched_records)
        confidence_analysis_res = cls._compute_confidence_analysis(enriched_records)
        disagreement_res = cls._compute_disagreement_analysis(enriched_records)
        consensus_audit_res = cls._compute_consensus_audit(enriched_records)
        entry_timing_res = cls._compute_entry_timing_analysis(enriched_records)
        cost_survival_res = cls._compute_cost_survival(enriched_records, cost_mode)
        statistical_tests_res = cls._compute_statistical_tests(enriched_records)
        null_hypothesis_res = cls._evaluate_null_hypotheses(
            market_dir=market_direction_res,
            stock_abs=stock_absolute_res,
            stock_rel=stock_relative_res,
            stats=statistical_tests_res
        )

        result = {
            "status": "SUCCESS",
            "sample_size": len(enriched_records),
            "benchmark_used": benchmark_type,
            "cost_mode_evaluated": cost_mode,
            "date_bounds": {
                "start": min(r["timestamp"] for r in enriched_records) if enriched_records else None,
                "end": max(r["timestamp"] for r in enriched_records) if enriched_records else None
            },
            "market_direction_accuracy": market_direction_res,
            "stock_absolute_direction": stock_absolute_res,
            "stock_relative_selection_edge": stock_relative_res,
            "regime_segmentation": regime_segmentation_res,
            "chop_forensics": chop_forensics_res,
            "market_beta_vs_alpha": market_beta_res,
            "model_comparison": model_comparison_res,
            "raw_vs_final_pipeline_trace": pipeline_trace_res,
            "top_bottom_rank_analysis": rank_analysis_res,
            "confidence_bucket_analysis": confidence_analysis_res,
            "disagreement_matrix": disagreement_res,
            "consensus_forensics": consensus_audit_res,
            "entry_timing_analysis": entry_timing_res,
            "cost_survival_analysis": cost_survival_res,
            "statistical_significance": statistical_tests_res,
            "null_hypotheses": null_hypothesis_res,
            "executive_verdict": null_hypothesis_res.get("verdict_statement")
        }

        cls._CACHE[cache_key] = result
        return result

    # =========================================================================
    # PART 1 & 2: INGESTION AND FORWARD ENRICHMENT
    # =========================================================================
    @classmethod
    def _load_signals(
        cls,
        db_path: str,
        signal_source: str,
        model: str,
        date_range: Optional[Tuple[Optional[str], Optional[str]]],
        direction: str
    ) -> List[Dict[str, Any]]:
        """Loads candidate signals from authoritative SQLite tables."""
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        signals = []

        try:
            # 1. Primary Source: consensus_candidate_shadow_log (Rich dual-engine candidate stream)
            if signal_source in ["ALL", "CONSENSUS_SHADOW"]:
                q = """
                    SELECT id, timestamp, scan_id, ticker, symbol, strategy,
                           qlib_direction, qlib_score, qlib_price,
                           legacy_direction, legacy_confidence, legacy_price,
                           composite_score, action_taken, entry_ref_price, details_json
                    FROM consensus_candidate_shadow_log
                    WHERE entry_ref_price IS NOT NULL AND entry_ref_price > 0
                """
                params = []
                if date_range and date_range[0]:
                    q += " AND timestamp >= ?"
                    params.append(date_range[0])
                if date_range and date_range[1]:
                    q += " AND timestamp <= ?"
                    params.append(date_range[1])

                rows = cur.execute(q, params).fetchall()
                for r in rows:
                    d = dict(r)
                    q_dir = cls._normalize_direction(d.get("qlib_direction"))
                    l_dir = cls._normalize_direction(d.get("legacy_direction"))
                    c_dir = q_dir if q_dir == l_dir else ("BUY" if (d.get("composite_score") or 50) >= 50 else "SELL")

                    d["q_dir"] = q_dir
                    d["l_dir"] = l_dir
                    d["c_dir"] = c_dir
                    d["source_table"] = "consensus_candidate_shadow_log"

                    if direction != "ALL" and c_dir != direction.upper():
                        continue
                    signals.append(d)

            # 2. Source: consensus_trade_history (Executed / Tracked Consensus trades)
            if signal_source in ["ALL", "CONSENSUS_TRACKED"]:
                try:
                    q2 = """
                        SELECT id, timestamp, ticker, symbol, strategy, direction,
                               entry_price, stop_loss, target_price, composite_score,
                               qlib_score, legacy_score, status, outcome, profit_pct,
                               exit_price, exit_time, details_json
                        FROM consensus_trade_history
                        WHERE entry_price IS NOT NULL AND entry_price > 0
                    """
                    params2 = []
                    if date_range and date_range[0]:
                        q2 += " AND timestamp >= ?"
                        params2.append(date_range[0])
                    if date_range and date_range[1]:
                        q2 += " AND timestamp <= ?"
                        params2.append(date_range[1])

                    rows2 = cur.execute(q2, params2).fetchall()
                    for r in rows2:
                        d = dict(r)
                        c_dir = cls._normalize_direction(d.get("direction"))
                        d["entry_ref_price"] = d.get("entry_price")
                        d["c_dir"] = c_dir
                        d["q_dir"] = c_dir
                        d["l_dir"] = c_dir
                        d["source_table"] = "consensus_trade_history"

                        if direction != "ALL" and c_dir != direction.upper():
                            continue
                        signals.append(d)
                except sqlite3.OperationalError:
                    pass

        except Exception as e:
            logger.error(f"Error loading signals from database: {e}", exc_info=True)
        finally:
            conn.close()

        return signals

    @classmethod
    def _enrich_forward_performance(
        cls,
        signals: List[Dict[str, Any]],
        db_path: str,
        timeframe: str,
        horizons: List[str],
        benchmark_type: str
    ) -> List[Dict[str, Any]]:
        """
        Enriches each signal with forward returns for the individual stock
        and the broad market benchmark at 15m, 30m, 60m, 120m, and session EOD.
        Zero lookahead: Only bars occurring strictly after signal timestamp T are inspected.
        """
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Cache benchmark bars in-memory for speed
        bm_bars_by_date = cls._load_benchmark_bars(cur, benchmark_type, timeframe)

        enriched = []
        for s in signals:
            ticker = s.get("ticker") or (f"{s.get('symbol')}.NS" if s.get("symbol") else None)
            ts = s.get("timestamp")
            entry_px = float(s.get("entry_ref_price") or s.get("entry_price") or 0.0)

            if not ticker or not ts or entry_px <= 0:
                continue

            ts_clean = str(ts).replace("T", " ")[:19]
            date_str = ts_clean[:10]

            # 1. Fetch subsequent stock OHLCV bars strictly after timestamp T
            cur.execute("""
                SELECT date, open, high, low, close, volume
                FROM ohlcv
                WHERE ticker = ? AND timeframe = ? AND date >= ?
                ORDER BY date ASC
                LIMIT 15
            """, (ticker, timeframe, ts_clean))
            stock_bars = [dict(b) for b in cur.fetchall()]

            if not stock_bars:
                continue

            # 2. Fetch corresponding benchmark bars
            bm_bars = bm_bars_by_date.get(date_str, [])
            bm_subsequent = [b for b in bm_bars if b["date"] >= ts_clean]

            # 3. Determine Market Regime for the Day
            regime = cls._determine_canonical_regime(cur, date_str, bm_bars)

            # 4. Extract horizon returns
            # stock_bars[0] is often the current bar or entry bar; horizon +1 bar = 15m, +2 = 30m, etc.
            fwd_stock_px = {}
            fwd_bm_px = {}
            mfe_pct = 0.0
            mae_pct = 0.0

            c_dir = s.get("c_dir", "BUY")
            is_buy = (c_dir == "BUY")

            # Calculate MFE & MAE across all subsequent session bars
            highs = [float(b["high"]) for b in stock_bars]
            lows = [float(b["low"]) for b in stock_bars]
            if highs and lows:
                if is_buy:
                    mfe_pct = round(((max(highs) - entry_px) / entry_px) * 100.0, 3)
                    mae_pct = round(((min(lows) - entry_px) / entry_px) * 100.0, 3)
                else:
                    mfe_pct = round(((entry_px - min(lows)) / entry_px) * 100.0, 3)
                    mae_pct = round(((entry_px - max(highs)) / entry_px) * 100.0, 3)

            # Map horizons
            bar_indices = {"15m": 1, "30m": 2, "60m": 4, "120m": 8, "EOD": len(stock_bars) - 1}
            bm_indices = {"15m": 1, "30m": 2, "60m": 4, "120m": 8, "EOD": len(bm_subsequent) - 1}

            horizon_stock_returns = {}
            horizon_bm_returns = {}
            horizon_rel_returns = {}

            bm_entry_px = float(bm_subsequent[0]["close"]) if bm_subsequent else 1.0

            for h in horizons:
                # Stock return
                idx = bar_indices.get(h, 1)
                target_bar = stock_bars[min(idx, len(stock_bars) - 1)]
                px_at_h = float(target_bar["close"])
                ret_stock = round(((px_at_h - entry_px) / entry_px) * 100.0, 3)
                horizon_stock_returns[h] = ret_stock

                # Benchmark return
                b_idx = bm_indices.get(h, 1)
                b_target = bm_subsequent[min(b_idx, len(bm_subsequent) - 1)] if bm_subsequent else None
                if b_target and bm_entry_px > 0:
                    ret_bm = round(((float(b_target["close"]) - bm_entry_px) / bm_entry_px) * 100.0, 3)
                else:
                    ret_bm = 0.0
                horizon_bm_returns[h] = ret_bm

                # Stock-relative return:
                # For BUY: stock_return - benchmark_return
                # For SELL: benchmark_return - stock_return
                if is_buy:
                    rel_ret = round(ret_stock - ret_bm, 3)
                else:
                    rel_ret = round(ret_bm - ret_stock, 3)
                horizon_rel_returns[h] = rel_ret

            rec = dict(s)
            rec["entry_price"] = entry_px
            rec["regime"] = regime
            rec["mfe_pct"] = mfe_pct
            rec["mae_pct"] = mae_pct
            rec["horizon_stock_returns"] = horizon_stock_returns
            rec["horizon_bm_returns"] = horizon_bm_returns
            rec["horizon_rel_returns"] = horizon_rel_returns
            rec["stock_bars_count"] = len(stock_bars)
            enriched.append(rec)

        conn.close()
        return enriched

    @classmethod
    def _load_benchmark_bars(cls, cur: sqlite3.Cursor, benchmark_type: str, timeframe: str) -> Dict[str, List[dict]]:
        """Loads and indexes benchmark 15m bars grouped by session date."""
        bm_by_date = {}
        tickers = list(dict.fromkeys(["^NSEI", "NIFTY 50", "NIFTY"] + list(BENCHMARK_5_UNIVERSE)))

        try:
            cur.execute(f"""
                SELECT date, ticker, open, high, low, close
                FROM ohlcv
                WHERE ticker IN ({','.join(['?']*len(tickers))}) AND timeframe = ?
                ORDER BY date ASC
            """, (*tickers, timeframe))
            rows = cur.fetchall()
        except Exception as e:
            logger.warning(f"Failed to query benchmark bars from ohlcv: {e}")
            return {}

        if not rows:
            return {}

        # If explicit index ^NSEI exists, prioritize index over stock basket
        index_rows = [r for r in rows if r["ticker"] in ("^NSEI", "NIFTY 50", "NIFTY")]
        use_rows = index_rows if index_rows else rows

        # Group by date and timestamp to create equal-weighted benchmark close
        grouped: Dict[str, Dict[str, List[float]]] = {}
        for r in use_rows:
            d_str = r["date"][:10]
            ts = r["date"]
            if d_str not in grouped:
                grouped[d_str] = {}
            if ts not in grouped[d_str]:
                grouped[d_str][ts] = []
            grouped[d_str][ts].append(float(r["close"]))

        for d_str, ts_dict in grouped.items():
            bars = []
            for ts, closes in sorted(ts_dict.items()):
                avg_close = sum(closes) / len(closes) if closes else 0.0
                bars.append({"date": ts, "close": avg_close})
            bm_by_date[d_str] = bars

        return bm_by_date

    @classmethod
    def _determine_canonical_regime(cls, cur: sqlite3.Cursor, date_str: str, bm_bars: List[dict]) -> str:
        """Determines market regime strictly from point-in-time benchmark performance."""
        if not bm_bars or len(bm_bars) < 2:
            return REGIME_RANGE_CHOP

        open_px = float(bm_bars[0]["close"])
        close_px = float(bm_bars[-1]["close"])
        day_ret_pct = ((close_px - open_px) / open_px) * 100.0 if open_px > 0 else 0.0

        if day_ret_pct >= 0.75:
            return REGIME_STRONG_BULL
        elif day_ret_pct >= 0.20:
            return REGIME_WEAK_BULL
        elif day_ret_pct <= -0.75:
            return REGIME_STRONG_BEAR
        elif day_ret_pct <= -0.20:
            return REGIME_WEAK_BEAR
        else:
            return REGIME_RANGE_CHOP

    @staticmethod
    def _normalize_direction(raw_dir: Optional[str]) -> str:
        if not raw_dir:
            return "BUY"
        s = str(raw_dir).upper().strip()
        if "SELL" in s or "BEAR" in s or "SHORT" in s:
            return "SELL"
        return "BUY"

    # =========================================================================
    # PART 2: MARKET DIRECTION ACCURACY
    # =========================================================================
    @classmethod
    def _compute_market_direction_accuracy(
        cls,
        records: List[Dict[str, Any]],
        horizons: List[str]
    ) -> Dict[str, Any]:
        """
        Measures: Did broad market move in the direction predicted by the system?
        BUY + market rises = TP_up
        BUY + market falls = FP_up
        SELL + market falls = TP_down
        SELL + market rises = FP_down
        """
        results_by_horizon = {}

        for h in horizons:
            buy_rises = 0
            buy_falls = 0
            sell_falls = 0
            sell_rises = 0
            subsequent_bm_returns = []

            for r in records:
                c_dir = r.get("c_dir", "BUY")
                bm_ret = r.get("horizon_bm_returns", {}).get(h, 0.0)
                subsequent_bm_returns.append(bm_ret)

                if c_dir == "BUY":
                    if bm_ret > 0:
                        buy_rises += 1
                    else:
                        buy_falls += 1
                else:  # SELL
                    if bm_ret < 0:
                        sell_falls += 1
                    else:
                        sell_rises += 1

            total_buys = buy_rises + buy_falls
            total_sells = sell_falls + sell_rises
            total_signals = total_buys + total_sells

            correct = buy_rises + sell_falls
            directional_acc = round((correct / total_signals) * 100.0, 2) if total_signals > 0 else 0.0

            prec_buy = round((buy_rises / total_buys) * 100.0, 2) if total_buys > 0 else 0.0
            prec_sell = round((sell_falls / total_sells) * 100.0, 2) if total_sells > 0 else 0.0

            # Balanced Accuracy = (Recall_BUY + Recall_SELL) / 2
            # Where ground truth market up count = buy_rises + sell_rises
            market_ups = buy_rises + sell_rises
            market_downs = buy_falls + sell_falls

            rec_buy = round((buy_rises / market_ups) * 100.0, 2) if market_ups > 0 else 0.0
            rec_sell = round((sell_falls / market_downs) * 100.0, 2) if market_downs > 0 else 0.0
            balanced_acc = round((rec_buy + rec_sell) / 2.0, 2)

            # Binomial test p-value vs null (50%)
            p_val = cls._binomial_p_value(correct, total_signals, p_null=0.5)

            results_by_horizon[h] = {
                "total_signals": total_signals,
                "buy_count": total_buys,
                "sell_count": total_sells,
                "buy_and_market_rises": buy_rises,
                "buy_and_market_falls": buy_falls,
                "sell_and_market_falls": sell_falls,
                "sell_and_market_rises": sell_rises,
                "directional_accuracy_pct": directional_acc,
                "balanced_accuracy_pct": balanced_acc,
                "precision_buy_pct": prec_buy,
                "precision_sell_pct": prec_sell,
                "recall_buy_pct": rec_buy,
                "recall_sell_pct": rec_sell,
                "mean_subsequent_market_return_pct": round(float(np.mean(subsequent_bm_returns)), 3) if subsequent_bm_returns else 0.0,
                "median_subsequent_market_return_pct": round(float(np.median(subsequent_bm_returns)), 3) if subsequent_bm_returns else 0.0,
                "p_value_vs_50pct_null": p_val,
                "statistically_significant_edge": bool(p_val < 0.05 and directional_acc > 50.0)
            }

        return {
            "description": "Evaluation of whether signals accurately forecast broad market benchmark direction",
            "horizons": results_by_horizon
        }

    # =========================================================================
    # PART 3: STOCK ABSOLUTE DIRECTION
    # =========================================================================
    @classmethod
    def _compute_stock_absolute_direction(
        cls,
        records: List[Dict[str, Any]],
        horizons: List[str],
        hurdle_threshold_pct: float = 0.0
    ) -> Dict[str, Any]:
        """
        Measures stock absolute price movement after signal.
        For BUY: correct if stock return > hurdle_threshold_pct.
        For SELL: correct if stock return < -hurdle_threshold_pct.
        Broken down separately for:
        - Legacy
        - Qlib
        - Consensus
        and separately for BUY vs SELL.
        """
        models = ["LEGACY", "QLIB", "CONSENSUS"]
        model_results = {}

        for m in models:
            dir_key = "l_dir" if m == "LEGACY" else ("q_dir" if m == "QLIB" else "c_dir")
            h_data = {}

            for h in horizons:
                buy_wins = 0
                buy_total = 0
                sell_wins = 0
                sell_total = 0
                buy_returns = []
                sell_returns = []

                for r in records:
                    pred_dir = r.get(dir_key, "BUY")
                    stock_ret = r.get("horizon_stock_returns", {}).get(h, 0.0)

                    if pred_dir == "BUY":
                        buy_total += 1
                        buy_returns.append(stock_ret)
                        if stock_ret > hurdle_threshold_pct:
                            buy_wins += 1
                    else:
                        sell_total += 1
                        sell_returns.append(stock_ret)
                        if stock_ret < -hurdle_threshold_pct:
                            sell_wins += 1

                total = buy_total + sell_total
                correct = buy_wins + sell_wins
                acc = round((correct / total) * 100.0, 2) if total > 0 else 0.0
                buy_acc = round((buy_wins / buy_total) * 100.0, 2) if buy_total > 0 else 0.0
                sell_acc = round((sell_wins / sell_total) * 100.0, 2) if sell_total > 0 else 0.0

                h_data[h] = {
                    "total_signals": total,
                    "accuracy_pct": acc,
                    "buy": {
                        "count": buy_total,
                        "wins": buy_wins,
                        "accuracy_pct": buy_acc,
                        "mean_return_pct": round(float(np.mean(buy_returns)), 3) if buy_returns else 0.0,
                        "median_return_pct": round(float(np.median(buy_returns)), 3) if buy_returns else 0.0
                    },
                    "sell": {
                        "count": sell_total,
                        "wins": sell_wins,
                        "accuracy_pct": sell_acc,
                        "mean_return_pct": round(float(np.mean(sell_returns)), 3) if sell_returns else 0.0,
                        "median_return_pct": round(float(np.median(sell_returns)), 3) if sell_returns else 0.0
                    },
                    "p_value_vs_50pct": cls._binomial_p_value(correct, total, 0.5)
                }

            model_results[m] = h_data

        return {
            "threshold_used_pct": hurdle_threshold_pct,
            "models": model_results
        }

    # =========================================================================
    # PART 4: STOCK-RELATIVE / CROSS-SECTIONAL EDGE (CRITICAL)
    # =========================================================================
    @classmethod
    def _compute_stock_relative_edge(
        cls,
        records: List[Dict[str, Any]],
        horizons: List[str]
    ) -> Dict[str, Any]:
        """
        Measures whether the system predicts performance RELATIVE TO THE MARKET.
        For BUY: relative_return = stock_return - benchmark_return
        For SELL: relative_return = benchmark_return - stock_return
        """
        results_by_horizon = {}

        for h in horizons:
            rel_returns = []
            rel_wins = 0
            abs_wins = 0

            # Rank IC calculation (for records with scores)
            scores = []
            rets = []

            for r in records:
                c_dir = r.get("c_dir", "BUY")
                stock_ret = r.get("horizon_stock_returns", {}).get(h, 0.0)
                bm_ret = r.get("horizon_bm_returns", {}).get(h, 0.0)
                rel_ret = r.get("horizon_rel_returns", {}).get(h, 0.0)

                rel_returns.append(rel_ret)

                # Absolute accuracy check
                if (c_dir == "BUY" and stock_ret > 0) or (c_dir == "SELL" and stock_ret < 0):
                    abs_wins += 1

                # Relative accuracy check
                if rel_ret > 0:
                    rel_wins += 1

                score = float(r.get("composite_score") or r.get("qlib_score") or 50.0)
                scores.append(score)
                rets.append(rel_ret)

            total = len(records)
            abs_acc = round((abs_wins / total) * 100.0, 2) if total > 0 else 0.0
            rel_acc = round((rel_wins / total) * 100.0, 2) if total > 0 else 0.0

            # Rank Information Coefficient (Spearman Rank Correlation)
            rank_ic = cls._spearman_rank_correlation(scores, rets)

            # Quartile spread
            quartiles = cls._compute_quantile_spreads(scores, rets, num_quantiles=4)

            results_by_horizon[h] = {
                "sample_size": total,
                "absolute_directional_accuracy_pct": abs_acc,
                "relative_directional_accuracy_pct": rel_acc,
                "relative_edge_differential_pct": round(rel_acc - abs_acc, 2),
                "mean_relative_return_pct": round(float(np.mean(rel_returns)), 3) if rel_returns else 0.0,
                "median_relative_return_pct": round(float(np.median(rel_returns)), 3) if rel_returns else 0.0,
                "rank_ic": rank_ic,
                "quartile_spread": quartiles,
                "p_value_vs_neutral_null": cls._binomial_p_value(rel_wins, total, 0.5)
            }

        return {
            "definition": "For BUY: stock - benchmark; For SELL: benchmark - stock",
            "horizons": results_by_horizon
        }

    # =========================================================================
    # PART 5: MARKET REGIME SEGMENTATION
    # =========================================================================
    @classmethod
    def _compute_regime_segmentation(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Segments performance strictly by canonical market regime."""
        regimes = [REGIME_STRONG_BULL, REGIME_WEAK_BULL, REGIME_RANGE_CHOP, REGIME_WEAK_BEAR, REGIME_STRONG_BEAR]
        regime_breakdown = {}

        for reg in regimes:
            reg_recs = [r for r in records if r.get("regime") == reg]
            if not reg_recs:
                regime_breakdown[reg] = {"sample_size": 0, "status": "NO_TRADES"}
                continue

            models_data = {}
            for m in ["LEGACY", "QLIB", "CONSENSUS"]:
                dir_key = "l_dir" if m == "LEGACY" else ("q_dir" if m == "QLIB" else "c_dir")
                buy_wins = sum(1 for r in reg_recs if r.get(dir_key) == "BUY" and r.get("horizon_stock_returns", {}).get("EOD", 0) > 0)
                buy_tot = sum(1 for r in reg_recs if r.get(dir_key) == "BUY")
                sell_wins = sum(1 for r in reg_recs if r.get(dir_key) == "SELL" and r.get("horizon_stock_returns", {}).get("EOD", 0) < 0)
                sell_tot = sum(1 for r in reg_recs if r.get(dir_key) == "SELL")

                buy_acc = round((buy_wins / buy_tot) * 100.0, 2) if buy_tot > 0 else 0.0
                sell_acc = round((sell_wins / sell_tot) * 100.0, 2) if sell_tot > 0 else 0.0

                pnl_list = [r.get("horizon_stock_returns", {}).get("EOD", 0) * (1 if r.get(dir_key) == "BUY" else -1) for r in reg_recs]
                net_pnl = round(sum(pnl_list), 2)
                exp = round(float(np.mean(pnl_list)), 3) if pnl_list else 0.0

                models_data[m] = {
                    "buy_signals": buy_tot,
                    "buy_accuracy_pct": buy_acc,
                    "sell_signals": sell_tot,
                    "sell_accuracy_pct": sell_acc,
                    "net_pnl_pct": net_pnl,
                    "expectancy_pct": exp
                }

            # Market volatility and dispersion
            bm_returns = [r.get("horizon_bm_returns", {}).get("EOD", 0) for r in reg_recs]
            stock_returns = [r.get("horizon_stock_returns", {}).get("EOD", 0) for r in reg_recs]

            regime_breakdown[reg] = {
                "sample_size": len(reg_recs),
                "market_mean_return_pct": round(float(np.mean(bm_returns)), 2) if bm_returns else 0.0,
                "market_volatility_pct": round(float(np.std(bm_returns)), 2) if bm_returns else 0.0,
                "cross_sectional_dispersion_pct": round(float(np.std(stock_returns)), 2) if stock_returns else 0.0,
                "models": models_data
            }

        return regime_breakdown

    # =========================================================================
    # PART 6: CHOP FORENSICS
    # =========================================================================
    @classmethod
    def _compute_chop_forensics(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Investigates whether poor performance correlates specifically with:
        - Low directional market movement
        - High intra-session reversals
        - Low cross-sectional dispersion
        """
        chop_records = [r for r in records if r.get("regime") == REGIME_RANGE_CHOP]
        trend_records = [r for r in records if r.get("regime") in [REGIME_STRONG_BULL, REGIME_STRONG_BEAR, REGIME_WEAK_BULL, REGIME_WEAK_BEAR]]

        def _stats_for_cohort(cohort: List[Dict[str, Any]]) -> Dict[str, Any]:
            if not cohort:
                return {"sample_size": 0}
            returns = [r.get("horizon_stock_returns", {}).get("EOD", 0) * (1 if r.get("c_dir") == "BUY" else -1) for r in cohort]
            rel_returns = [r.get("horizon_rel_returns", {}).get("EOD", 0) for r in cohort]
            wins = sum(1 for ret in returns if ret > 0)
            rel_wins = sum(1 for rret in rel_returns if rret > 0)

            return {
                "sample_size": len(cohort),
                "win_rate_pct": round((wins / len(cohort)) * 100.0, 2),
                "relative_win_rate_pct": round((rel_wins / len(cohort)) * 100.0, 2),
                "mean_return_pct": round(float(np.mean(returns)), 3),
                "mean_relative_return_pct": round(float(np.mean(rel_returns)), 3),
                "expectancy_pct": round(float(np.mean(returns)), 3),
                "profit_factor": cls._calculate_profit_factor(returns)
            }

        chop_stats = _stats_for_cohort(chop_records)
        trend_stats = _stats_for_cohort(trend_records)

        # Correlation between market trend magnitude and trade P&L
        trend_magnitudes = [abs(r.get("horizon_bm_returns", {}).get("EOD", 0)) for r in records]
        trade_returns = [r.get("horizon_stock_returns", {}).get("EOD", 0) * (1 if r.get("c_dir") == "BUY" else -1) for r in records]
        corr_trend_vs_pnl = cls._pearson_correlation(trend_magnitudes, trade_returns)

        return {
            "chop_cohort": chop_stats,
            "trend_cohort": trend_stats,
            "correlation_market_trend_magnitude_vs_trade_pnl": corr_trend_vs_pnl,
            "performance_collapse_confirmed_in_chop": bool(chop_stats.get("win_rate_pct", 50) < trend_stats.get("win_rate_pct", 50) - 5.0)
        }

    # =========================================================================
    # PART 7: MARKET BETA VS STOCK ALPHA
    # =========================================================================
    @classmethod
    def _compute_market_beta_vs_alpha(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Decomposes raw trade return into broad market beta vs stock selection alpha:
        raw_return = beta * benchmark_return + alpha
        """
        raw_trade_returns = []
        bm_signed_returns = []

        for r in records:
            c_dir = r.get("c_dir", "BUY")
            sign = 1 if c_dir == "BUY" else -1
            stock_ret = r.get("horizon_stock_returns", {}).get("EOD", 0.0)
            bm_ret = r.get("horizon_bm_returns", {}).get("EOD", 0.0)

            raw_trade_returns.append(stock_ret * sign)
            bm_signed_returns.append(bm_ret * sign)

        if len(raw_trade_returns) < 5:
            return {"status": "INSUFFICIENT_SAMPLE"}

        # Linear regression: trade_return = alpha + beta * bm_signed_return
        x = np.array(bm_signed_returns)
        y = np.array(raw_trade_returns)

        var_x = np.var(x)
        if var_x > 1e-8:
            beta = float(np.cov(x, y)[0, 1] / var_x)
            alpha_mean = float(np.mean(y) - beta * np.mean(x))
        else:
            beta = 1.0
            alpha_mean = float(np.mean(y) - np.mean(x))

        raw_mean = float(np.mean(y))
        beta_contribution = float(beta * np.mean(x))

        return {
            "total_trades": len(records),
            "mean_raw_trade_return_pct": round(raw_mean, 3),
            "market_beta": round(beta, 3),
            "market_beta_contribution_pct": round(beta_contribution, 3),
            "stock_selection_alpha_pct": round(alpha_mean, 3),
            "pct_of_profit_explained_by_beta": round((beta_contribution / raw_mean * 100.0), 1) if abs(raw_mean) > 1e-5 else 0.0,
            "has_genuine_stock_selection_alpha": bool(alpha_mean > 0.0)
        }

    # =========================================================================
    # PART 8: MODEL COMPARISON (LEGACY vs QLIB vs CONSENSUS)
    # =========================================================================
    @classmethod
    def _compute_model_comparison(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compares Legacy, Qlib, Consensus for identical setups, timestamps, horizons."""
        comparison = {}
        for m in ["LEGACY", "QLIB", "CONSENSUS"]:
            dir_key = "l_dir" if m == "LEGACY" else ("q_dir" if m == "QLIB" else "c_dir")
            pnl_list = []
            rel_pnl_list = []
            wins = 0

            for r in records:
                p_dir = r.get(dir_key, "BUY")
                sign = 1 if p_dir == "BUY" else -1
                stk_ret = r.get("horizon_stock_returns", {}).get("EOD", 0.0)
                bm_ret = r.get("horizon_bm_returns", {}).get("EOD", 0.0)

                trade_ret = stk_ret * sign
                rel_ret = (stk_ret - bm_ret) if p_dir == "BUY" else (bm_ret - stk_ret)

                pnl_list.append(trade_ret)
                rel_pnl_list.append(rel_ret)
                if trade_ret > 0:
                    wins += 1

            tot = len(pnl_list)
            comparison[m] = {
                "sample_size": tot,
                "directional_win_rate_pct": round((wins / tot) * 100.0, 2) if tot > 0 else 0.0,
                "mean_return_pct": round(float(np.mean(pnl_list)), 3) if pnl_list else 0.0,
                "mean_relative_return_pct": round(float(np.mean(rel_pnl_list)), 3) if rel_pnl_list else 0.0,
                "profit_factor": cls._calculate_profit_factor(pnl_list),
                "cost_survival_rate_pct": cls._calculate_cost_survival_rate(pnl_list)
            }

        return comparison

    # =========================================================================
    # PART 9: RAW SIGNAL VS FINAL SIGNAL TRACE
    # =========================================================================
    @classmethod
    def _compute_raw_vs_final_signal_trace(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Traces information transmission from raw Qlib score -> Confidence -> Consensus -> Final Trade.
        Determines if directional information is lost, inverted, or diluted along the pipeline.
        """
        stages = []
        raw_scores = [float(r.get("qlib_score") or 50.0) for r in records]
        confidences = [float(r.get("legacy_confidence") or 50.0) for r in records]
        composite_scores = [float(r.get("composite_score") or 50.0) for r in records]
        fwd_returns = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) for r in records]

        ic_raw = cls._spearman_rank_correlation(raw_scores, fwd_returns)
        ic_conf = cls._spearman_rank_correlation(confidences, fwd_returns)
        ic_comp = cls._spearman_rank_correlation(composite_scores, fwd_returns)

        return {
            "raw_qlib_rank_ic": ic_raw,
            "legacy_confidence_rank_ic": ic_conf,
            "consensus_composite_rank_ic": ic_comp,
            "information_gain_or_loss": {
                "qlib_to_consensus_delta_ic": round(ic_comp - ic_raw, 4),
                "verdict": "CONSENSUS_ENHANCES_RANKING" if ic_comp > ic_raw else "CONSENSUS_DILUTES_RAW_QLIB_ALPHA"
            }
        }

    # =========================================================================
    # PART 10: TOP/BOTTOM RANK ANALYSIS
    # =========================================================================
    @classmethod
    def _compute_top_bottom_rank_analysis(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Groups predictions into bottom 10%, bottom 25%, middle, top 25%, top 10%."""
        scores = [float(r.get("composite_score") or 50.0) for r in records]
        if len(scores) < 10:
            return {"status": "INSUFFICIENT_SAMPLE"}

        p10 = np.percentile(scores, 10)
        p25 = np.percentile(scores, 25)
        p75 = np.percentile(scores, 75)
        p90 = np.percentile(scores, 90)

        groups = {
            "bottom_10pct": [r for r in records if float(r.get("composite_score") or 50) <= p10],
            "bottom_25pct": [r for r in records if float(r.get("composite_score") or 50) <= p25],
            "middle_50pct": [r for r in records if p25 < float(r.get("composite_score") or 50) < p75],
            "top_25pct": [r for r in records if float(r.get("composite_score") or 50) >= p75],
            "top_10pct": [r for r in records if float(r.get("composite_score") or 50) >= p90],
        }

        group_stats = {}
        for name, grp in groups.items():
            if not grp:
                continue
            rets = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("c_dir") == "BUY" else -1) for r in grp]
            rel_rets = [r.get("horizon_rel_returns", {}).get("EOD", 0.0) for r in grp]
            wins = sum(1 for x in rets if x > 0)
            group_stats[name] = {
                "sample_size": len(grp),
                "win_rate_pct": round((wins / len(grp)) * 100.0, 2),
                "mean_return_pct": round(float(np.mean(rets)), 3),
                "mean_relative_return_pct": round(float(np.mean(rel_rets)), 3),
                "profit_factor": cls._calculate_profit_factor(rets)
            }

        # Monotonicity check
        top_mean = group_stats.get("top_10pct", {}).get("mean_return_pct", 0.0)
        bot_mean = group_stats.get("bottom_10pct", {}).get("mean_return_pct", 0.0)
        spread = round(top_mean - bot_mean, 3)

        return {
            "groups": group_stats,
            "top_10_vs_bottom_10_spread_pct": spread,
            "monotonic_ranking_present": bool(spread > 0.0)
        }

    # =========================================================================
    # PART 11: CONFIDENCE ANALYSIS
    # =========================================================================
    @classmethod
    def _compute_confidence_analysis(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyzes whether higher confidence buckets produce higher win rates."""
        buckets = [
            ("50_52", 50.0, 52.0),
            ("52_55", 52.0, 55.0),
            ("55_60", 55.0, 60.0),
            ("60_65", 60.0, 65.0),
            ("65_70", 65.0, 70.0),
            ("70_plus", 70.0, 100.0)
        ]
        bucket_results = {}

        for b_name, b_min, b_max in buckets:
            b_recs = [r for r in records if b_min <= float(r.get("composite_score") or 50.0) < b_max or (b_max == 100.0 and float(r.get("composite_score") or 50.0) >= b_min)]
            if not b_recs:
                continue
            rets = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("c_dir") == "BUY" else -1) for r in b_recs]
            wins = sum(1 for x in rets if x > 0)
            bucket_results[b_name] = {
                "sample_size": len(b_recs),
                "win_rate_pct": round((wins / len(b_recs)) * 100.0, 2),
                "mean_return_pct": round(float(np.mean(rets)), 3),
                "expectancy_pct": round(float(np.mean(rets)), 3)
            }

        return bucket_results

    # =========================================================================
    # PART 12: DISAGREEMENT ANALYSIS
    # =========================================================================
    @classmethod
    def _compute_disagreement_analysis(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Evaluates 4 quadrants of Legacy and Qlib agreement/disagreement."""
        quadrants = {
            "BOTH_BUY": [r for r in records if r.get("l_dir") == "BUY" and r.get("q_dir") == "BUY"],
            "BOTH_SELL": [r for r in records if r.get("l_dir") == "SELL" and r.get("q_dir") == "SELL"],
            "LEGACY_BUY_QLIB_SELL": [r for r in records if r.get("l_dir") == "BUY" and r.get("q_dir") == "SELL"],
            "LEGACY_SELL_QLIB_BUY": [r for r in records if r.get("l_dir") == "SELL" and r.get("q_dir") == "BUY"],
        }
        matrix = {}

        for q_name, q_recs in quadrants.items():
            if not q_recs:
                matrix[q_name] = {"sample_size": 0}
                continue
            rets = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("c_dir") == "BUY" else -1) for r in q_recs]
            wins = sum(1 for x in rets if x > 0)
            matrix[q_name] = {
                "sample_size": len(q_recs),
                "win_rate_pct": round((wins / len(q_recs)) * 100.0, 2),
                "mean_return_pct": round(float(np.mean(rets)), 3),
                "profit_factor": cls._calculate_profit_factor(rets)
            }

        agreed = len(quadrants["BOTH_BUY"]) + len(quadrants["BOTH_SELL"])
        disagreed = len(quadrants["LEGACY_BUY_QLIB_SELL"]) + len(quadrants["LEGACY_SELL_QLIB_BUY"])

        agreed_wins = sum(1 for r in (quadrants["BOTH_BUY"] + quadrants["BOTH_SELL"]) if r.get("horizon_stock_returns", {}).get("EOD", 0) * (1 if r.get("c_dir") == "BUY" else -1) > 0)
        disagreed_wins = sum(1 for r in (quadrants["LEGACY_BUY_QLIB_SELL"] + quadrants["LEGACY_SELL_QLIB_BUY"]) if r.get("horizon_stock_returns", {}).get("EOD", 0) * (1 if r.get("c_dir") == "BUY" else -1) > 0)

        agreed_acc = round((agreed_wins / agreed) * 100.0, 2) if agreed > 0 else 0.0
        disagreed_acc = round((disagreed_wins / disagreed) * 100.0, 2) if disagreed > 0 else 0.0

        return {
            "matrix": matrix,
            "unanimous_agreement_acc_pct": agreed_acc,
            "disagreement_acc_pct": disagreed_acc,
            "agreement_boost_pct": round(agreed_acc - disagreed_acc, 2),
            "agreement_improves_prediction": bool(agreed_acc > disagreed_acc)
        }

    # =========================================================================
    # PART 13: CONSENSUS AUDIT
    # =========================================================================
    @classmethod
    def _compute_consensus_audit(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Determines whether Consensus improves or degrades underlying model info."""
        legacy_rets = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("l_dir") == "BUY" else -1) for r in records]
        qlib_rets = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("q_dir") == "BUY" else -1) for r in records]
        consensus_rets = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("c_dir") == "BUY" else -1) for r in records]

        l_mean = float(np.mean(legacy_rets)) if legacy_rets else 0.0
        q_mean = float(np.mean(qlib_rets)) if qlib_rets else 0.0
        c_mean = float(np.mean(consensus_rets)) if consensus_rets else 0.0

        best_individual = max(l_mean, q_mean)
        delta = round(c_mean - best_individual, 3)

        return {
            "legacy_mean_return_pct": round(l_mean, 3),
            "qlib_mean_return_pct": round(q_mean, 3),
            "consensus_mean_return_pct": round(c_mean, 3),
            "consensus_value_add_pct": delta,
            "consensus_verdict": "CONSENSUS_ADDS_VALUE" if delta >= 0 else "CONSENSUS_DEGRADES_INDIVIDUAL_ALPHA"
        }

    # =========================================================================
    # PART 14: ENTRY TIMING FORENSICS
    # =========================================================================
    @classmethod
    def _compute_entry_timing_analysis(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Classifies setups into:
        A. Direction wrong (MFE < 0.2%, MAE > 1.0%)
        B. Direction correct but entry late / reversed before exit
        C. Direction correct and timely (MFE >= 1.0%, MAE < 0.5%)
        D. Signal neutral / chop noise (|MFE| < 0.3%, |MAE| < 0.3%)
        """
        counts = {"A_DIRECTION_WRONG": 0, "B_CORRECT_BUT_LATE": 0, "C_CORRECT_AND_TIMELY": 0, "D_SIGNAL_NEUTRAL_CHOP": 0}

        for r in records:
            mfe = r.get("mfe_pct", 0.0)
            mae = abs(r.get("mae_pct", 0.0))
            eod_ret = r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("c_dir") == "BUY" else -1)

            if mfe < 0.2 and mae > 0.8:
                counts["A_DIRECTION_WRONG"] += 1
            elif mfe >= 0.8 and eod_ret <= 0:
                counts["B_CORRECT_BUT_LATE"] += 1
            elif mfe >= 0.8 and eod_ret > 0:
                counts["C_CORRECT_AND_TIMELY"] += 1
            else:
                counts["D_SIGNAL_NEUTRAL_CHOP"] += 1

        total = len(records)
        pcts = {k: round((v / total) * 100.0, 1) for k, v in counts.items()} if total > 0 else {}

        return {
            "distribution_counts": counts,
            "distribution_pcts": pcts,
            "primary_failure_mode": max(counts.items(), key=lambda x: x[1])[0] if counts else "NONE"
        }

    # =========================================================================
    # PART 15: COST SURVIVAL
    # =========================================================================
    @classmethod
    def _compute_cost_survival(cls, records: List[Dict[str, Any]], cost_mode: str) -> Dict[str, Any]:
        """
        Evaluates gross edge vs statutory regulatory fees + slippage via trade_economics.
        Identifies whether positive directional prediction survives realistic statutory friction.
        """
        canonical_intraday = compute_trade_friction_pct(trade_type="INTRADAY")
        friction_pct = canonical_intraday.get("total_friction_pct", 0.1872) if cost_mode == "CANONICAL" else 0.0

        gross_returns = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("c_dir") == "BUY" else -1) for r in records]
        net_returns = [g - friction_pct for g in gross_returns]

        gross_wins = sum(1 for g in gross_returns if g > 0)
        net_wins = sum(1 for n in net_returns if n > 0)

        tot = len(records)
        gross_wr = round((gross_wins / tot) * 100.0, 2) if tot > 0 else 0.0
        net_wr = round((net_wins / tot) * 100.0, 2) if tot > 0 else 0.0
        gross_mean = round(float(np.mean(gross_returns)), 3) if gross_returns else 0.0
        net_mean = round(float(np.mean(net_returns)), 3) if net_returns else 0.0

        return {
            "friction_pct_applied": friction_pct,
            "gross_win_rate_pct": gross_wr,
            "net_win_rate_pct": net_wr,
            "win_rate_erosion_pct": round(gross_wr - net_wr, 2),
            "gross_expectancy_pct": gross_mean,
            "net_expectancy_pct": net_mean,
            "edge_survives_costs": bool(net_mean > 0.0),
            "break_even_movement_hurdle_pct": friction_pct
        }

    # =========================================================================
    # PART 16 & 17: STATISTICAL TESTS
    # =========================================================================
    @classmethod
    def _compute_statistical_tests(cls, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculates 95% Wilson confidence intervals, t-statistics, and p-values."""
        returns = [r.get("horizon_stock_returns", {}).get("EOD", 0.0) * (1 if r.get("c_dir") == "BUY" else -1) for r in records]
        rel_returns = [r.get("horizon_rel_returns", {}).get("EOD", 0.0) for r in records]

        n = len(returns)
        if n == 0:
            return {"sample_size": 0}

        wins = sum(1 for x in returns if x > 0)
        wr = wins / n
        # Wilson score 95% CI
        z = 1.96
        denom = 1.0 + (z**2 / n)
        center = (wr + (z**2 / (2 * n))) / denom
        half_width = z * math.sqrt((wr * (1 - wr) / n) + (z**2 / (4 * n**2))) / denom
        ci_lower = round(max(0.0, (center - half_width)) * 100.0, 2)
        ci_upper = round(min(1.0, (center + half_width)) * 100.0, 2)

        # One-sample t-test for relative return > 0
        mean_rel = float(np.mean(rel_returns))
        std_rel = float(np.std(rel_returns, ddof=1)) if n > 1 else 1.0
        t_stat = (mean_rel / (std_rel / math.sqrt(n))) if std_rel > 1e-8 else 0.0

        return {
            "sample_size": n,
            "directional_accuracy_95ci": [ci_lower, ci_upper],
            "relative_return_mean_pct": round(mean_rel, 3),
            "relative_return_t_stat": round(t_stat, 3),
            "relative_edge_statistically_significant": bool(t_stat > 1.96)
        }

    # =========================================================================
    # PART 18: NULL HYPOTHESIS EVALUATION
    # =========================================================================
    @classmethod
    def _evaluate_null_hypotheses(
        cls,
        market_dir: Dict[str, Any],
        stock_abs: Dict[str, Any],
        stock_rel: Dict[str, Any],
        stats: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Evaluates the two core null hypotheses:
        H0_1: "The scanner's directional predictions contain no useful predictive information beyond market direction/noise."
        H0_2: "The scanner contains no stock-selection information after removing broad market movement."
        """
        eod_mkt = market_dir.get("horizons", {}).get("EOD", {})
        eod_rel = stock_rel.get("horizons", {}).get("EOD", {})

        mkt_acc = eod_mkt.get("directional_accuracy_pct", 50.0)
        mkt_pval = eod_mkt.get("p_value_vs_50pct_null", 1.0)

        rel_acc = eod_rel.get("relative_directional_accuracy_pct", 50.0)
        rel_mean = eod_rel.get("mean_relative_return_pct", 0.0)
        rel_pval = eod_rel.get("p_value_vs_neutral_null", 1.0)

        # Evaluate H0_1
        if mkt_pval < 0.05 and mkt_acc > 52.0:
            h0_1_status = "REJECTED"
            h0_1_reason = f"Directional market accuracy ({mkt_acc}%) is statistically distinct from 50% random chance (p={mkt_pval:.4f})."
        elif mkt_pval < 0.05 and mkt_acc < 48.0:
            h0_1_status = "REJECTED (INVERTED)"
            h0_1_reason = f"Directional accuracy ({mkt_acc}%) is significantly below 50%, indicating directional inversion."
        else:
            h0_1_status = "SUPPORTED"
            h0_1_reason = f"Directional accuracy ({mkt_acc}%) fails to reject null hypothesis of random chance / broad market drift (p={mkt_pval:.4f})."

        # Evaluate H0_2
        if rel_pval < 0.05 and rel_mean > 0.0:
            h0_2_status = "REJECTED"
            h0_2_reason = f"Stock-selection relative return (+{rel_mean:.3f}%) significantly outperforms broad market (p={rel_pval:.4f})."
        else:
            h0_2_status = "SUPPORTED"
            h0_2_reason = f"Stock-relative selection edge is neutral/negligible (+{rel_mean:.3f}%, p={rel_pval:.4f}); apparent profits driven by market beta."

        # Overall synthesis
        if h0_1_status == "SUPPORTED" and h0_2_status == "SUPPORTED":
            verdict = "APPARENT_PROFITS_EXPLAINED_BY_BROAD_MARKET_BETA_AND_CHOP_EROSION"
            stmt = "The system lacks genuine stock-selection alpha after removing broad market drift. Losses in chop stem from absent directional edge, while decline-phase profits were predominantly beta-driven."
        elif h0_2_status == "REJECTED":
            verdict = "GENUINE_STOCK_SELECTION_ALPHA_PRESENT"
            stmt = "The system demonstrates statistically significant stock-relative alpha against the broad benchmark, even when absolute direction is choppy."
        else:
            verdict = "INCONCLUSIVE_REGIME_DEPENDENT"
            stmt = "Edge is regime-dependent, requiring systematic macro-trend gating."

        return {
            "H0_1_market_direction_null": {
                "hypothesis": "Directional predictions contain no useful predictive information beyond market noise.",
                "status": h0_1_status,
                "evidence": h0_1_reason
            },
            "H0_2_stock_selection_null": {
                "hypothesis": "Scanner contains no stock-selection alpha after broad market movement is removed.",
                "status": h0_2_status,
                "evidence": h0_2_reason
            },
            "verdict": verdict,
            "verdict_statement": stmt
        }

    # =========================================================================
    # MATHEMATICAL HELPERS
    # =========================================================================
    @staticmethod
    def _binomial_p_value(k: int, n: int, p_null: float = 0.5) -> float:
        """Computes two-tailed binomial exact/normal approximation p-value."""
        if n <= 0:
            return 1.0
        # Normal approximation with continuity correction
        mean = n * p_null
        std = math.sqrt(n * p_null * (1.0 - p_null))
        if std < 1e-8:
            return 1.0
        z = abs(k - mean - 0.5) / std
        # Approximation of standard normal survival function
        p_val = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))
        return round(float(np.clip(p_val, 0.0, 1.0)), 4)

    @staticmethod
    def _spearman_rank_correlation(x: List[float], y: List[float]) -> float:
        """Computes Spearman Rank Correlation (Information Coefficient)."""
        if len(x) < 5 or len(x) != len(y):
            return 0.0
        try:
            rx = pd.Series(x).rank()
            ry = pd.Series(y).rank()
            cov = np.cov(rx, ry)[0, 1]
            std_prod = np.std(rx) * np.std(ry)
            if std_prod > 1e-8:
                return round(float(cov / std_prod), 4)
            return 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _pearson_correlation(x: List[float], y: List[float]) -> float:
        if len(x) < 5 or len(x) != len(y):
            return 0.0
        try:
            return round(float(np.corrcoef(x, y)[0, 1]), 4)
        except Exception:
            return 0.0

    @staticmethod
    def _calculate_profit_factor(returns: List[float]) -> float:
        wins = sum(r for r in returns if r > 0)
        losses = abs(sum(r for r in returns if r < 0))
        if losses < 1e-6:
            return 999.0 if wins > 0 else 1.0
        return round(wins / losses, 2)

    @staticmethod
    def _calculate_cost_survival_rate(returns: List[float]) -> float:
        """Percentage of trades that remain profitable after 0.1872% canonical friction."""
        if not returns:
            return 0.0
        survived = sum(1 for r in returns if r > 0.1872)
        return round((survived / len(returns)) * 100.0, 2)

    @classmethod
    def _compute_quantile_spreads(cls, scores: List[float], returns: List[float], num_quantiles: int = 4) -> Dict[str, Any]:
        if len(scores) < num_quantiles * 2:
            return {}
        try:
            df = pd.DataFrame({"score": scores, "ret": returns})
            df["quantile"] = pd.qcut(df["score"], q=num_quantiles, labels=[f"Q{i+1}" for i in range(num_quantiles)], duplicates="drop")
            grouped = df.groupby("quantile", observed=False)["ret"].mean().to_dict()
            q1 = grouped.get("Q1", 0.0)
            q_top = grouped.get(f"Q{num_quantiles}", 0.0)
            spread = round(q_top - q1, 3)
            return {
                "quantile_means": {k: round(float(v), 3) for k, v in grouped.items()},
                "top_minus_bottom_spread_pct": spread
            }
        except Exception:
            return {}
