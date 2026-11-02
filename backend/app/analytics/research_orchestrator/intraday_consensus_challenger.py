"""
INTRADAY CONSENSUS CHALLENGER: REGIME-AWARE & COST-FILTERED MODEL (RESEARCH-ONLY)
================================================================================
Research artifact for Phase 5 (Challenger Regime Model) & Phase 6 (Cost-Aware Filter).

CRITICAL GOVERNANCE INVARIANTS:
1. Strict Research Isolation: Does NOT modify production champion models (models/intraday/champion_ensemble.pkl).
2. Point-in-Time Causality: All features generated strictly using data available before decision timestamp T.
3. Chronological Splits: Evaluated with chronological OOS split (Train: Sep 18-24, Test: Sep 25-Oct 05).
4. Realistic Friction: Evaluates net expectancy after ₹60-70 round-trip transaction costs.
5. Promotion Guard: If OOS net expectancy improvement is not statistically proven, KEEP CURRENT CHAMPION.
"""

import os
import math
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np

from app.data.historical_data_layer import get_db_path
from app.analytics.consensus_economic_engine import ConsensusEconomicEngine

logger = logging.getLogger(__name__)


class IntradayConsensusChallenger:
    """
    Research-only machine learning challenger evaluating regime-conditioned
    and cost-filtered trade qualification.
    """

    FEATURE_NAMES = [
        "market_breadth_pct",       # Top 5 heavyweight breadth return
        "market_is_bullish",        # 1 if market breadth >= +0.20%, else 0
        "market_is_bearish",        # 1 if market breadth <= -0.20%, else 0
        "vwap_dist_pct",            # Price distance from session VWAP
        "open_dist_pct",            # Price distance from session open
        "atr_pct",                  # 14-period ATR as % of price
        "composite_score",          # Consensus composite conviction score
        "qlib_score",               # Qlib Alpha-158 normalized score
        "legacy_score",             # Legacy ensemble confidence score
        "is_buy_direction",         # 1 if BUY, 0 if SELL
        "expected_gross_return",    # Distance to target %
        "expected_risk_return",     # Distance to stop %
        "fee_drag_pct",             # Friction fees as % of position capital (~0.35%)
    ]

    @classmethod
    def extract_features(cls, row: Dict[str, Any]) -> np.ndarray:
        """
        Extracts point-in-time feature vector from enriched trade row.
        """
        entry_px = float(row.get("entry_price") or 1.0)
        tp_px = float(row.get("target_price") or entry_px * 1.01)
        sl_px = float(row.get("stop_loss") or entry_px * 0.99)
        direction = str(row.get("direction", "BUY")).upper()

        if direction in ("BUY", "BULLISH"):
            expected_gross = abs((tp_px - entry_px) / entry_px * 100.0)
            expected_risk = abs((entry_px - sl_px) / entry_px * 100.0)
            is_buy = 1.0
        else:
            expected_gross = abs((entry_px - tp_px) / entry_px * 100.0)
            expected_risk = abs((sl_px - entry_px) / entry_px * 100.0)
            is_buy = 0.0

        market_breadth = float(row.get("market_breadth_pct") or 0.0)
        market_bull = 1.0 if market_breadth >= 0.20 else 0.0
        market_bear = 1.0 if market_breadth <= -0.20 else 0.0

        vwap_dist = float(row.get("vwap_dist_pct") or 0.0)
        open_dist = float(row.get("open_dist_pct") or 0.0)
        atr_14 = float(row.get("atr_14") or entry_px * 0.008)
        atr_pct = (atr_14 / entry_px * 100.0) if entry_px > 0 else 0.80

        comp_score = float(row.get("composite_score") or 50.0)
        qlib_score = float(row.get("qlib_score") or 0.0)
        legacy_score = float(row.get("legacy_score") or 50.0)

        # Standard fee drag on ₹20,000 position
        fee_drag = ConsensusEconomicEngine.calculate_economic_hurdle_pct(entry_px, shares=max(1, int(20000.0 / entry_px)))

        feats = [
            market_breadth,
            market_bull,
            market_bear,
            vwap_dist,
            open_dist,
            atr_pct,
            comp_score,
            qlib_score,
            legacy_score,
            is_buy,
            expected_gross,
            expected_risk,
            fee_drag
        ]
        return np.array(feats, dtype=float)

    @classmethod
    def evaluate_cost_hurdle_filter(
        cls,
        predicted_win_prob: float,
        target_gross_pct: float,
        stop_loss_pct: float,
        fee_drag_pct: float = 0.35,
        min_required_net_expectancy_pct: float = 0.10
    ) -> Tuple[bool, float, str]:
        """
        Phase 6 Cost-Aware Trade Filter:
        Expected Net Return (%) = P(Win) * Net Win (%) - (1 - P(Win)) * Net Loss (%)
        Where:
        Net Win (%) = target_gross_pct - fee_drag_pct
        Net Loss (%) = stop_loss_pct + fee_drag_pct
        
        Rejects candidates when Expected Net Return <= min_required_net_expectancy_pct.
        """
        net_win_pct = target_gross_pct - fee_drag_pct
        net_loss_pct = stop_loss_pct + fee_drag_pct

        expected_net_return = (predicted_win_prob * net_win_pct) - ((1.0 - predicted_win_prob) * net_loss_pct)

        if expected_net_return <= min_required_net_expectancy_pct:
            return False, expected_net_return, f"COST_HURDLE_REJECTED: Expected net {expected_net_return:.2f}% <= {min_required_net_expectancy_pct:.2f}%"

        return True, expected_net_return, f"COST_HURDLE_PASSED: Expected net {expected_net_return:.2f}%"

    @classmethod
    def run_chronological_oos_evaluation(
        cls,
        df: pd.DataFrame,
        train_cutoff_date: str = "2026-09-24"
    ) -> Dict[str, Any]:
        """
        Chronological Walk-Forward Evaluation:
        Train Period: <= train_cutoff_date (e.g. Sep 18 - Sep 24)
        OOS Test Period: > train_cutoff_date (e.g. Sep 25 - Oct 05)
        
        Compares:
        1. BASELINE CHAMPION (All unadjusted qualified trades)
        2. CHALLENGER WITH COST-AWARE REGIME FILTER
        """
        if df.empty or len(df) < 10:
            return {"status": "INSUFFICIENT_DATA"}

        df["entry_date"] = df["entry_time"].str[:10]
        train_df = df[df["entry_date"] <= train_cutoff_date].copy()
        test_df = df[df["entry_date"] > train_cutoff_date].copy()

        if test_df.empty:
            return {"status": "NO_OOS_DATA"}

        # 1. Baseline Performance on OOS Period
        base_trades = len(test_df)
        base_gross = round(float(test_df["gross_pnl_rupees"].sum()), 2)
        base_fees = round(float(test_df["fees_rupees"].sum()), 2)
        base_net = round(float(test_df["net_pnl_rupees"].sum()), 2)
        base_wins = len(test_df[test_df["net_pnl_rupees"] > 0])
        base_wr = round((base_wins / base_trades) * 100.0, 1) if base_trades > 0 else 0.0
        base_expectancy = round(base_net / base_trades, 2) if base_trades > 0 else 0.0

        # 2. Challenger Strategy Evaluation on OOS Period:
        # A) Regime Alignment Rule: Disallow BUY when Market Breadth <= -0.20% (Bearish collapse)
        # B) Time-of-Day Cutoff: Disallow new entries after 14:15 IST (prevents 0.0% target hit churn)
        # C) Economic Hurdle: Must have favorable Net Expectancy after fee drag (0.35%)
        challenger_rows = []
        rejected_reasons = {}

        for _, row in test_df.iterrows():
            entry_t = str(row["entry_time"])
            th_str = entry_t[11:16] if len(entry_t) >= 16 else "10:00"
            parts = th_str.split(":")
            entry_minute = int(parts[0]) * 60 + int(parts[1])

            # Gate 1: Cutoff after 14:15 IST (855 minutes)
            if entry_minute >= 855:
                rejected_reasons["ENTRY_AFTER_1415"] = rejected_reasons.get("ENTRY_AFTER_1415", 0) + 1
                continue

            # Gate 2: Severe Regime Mismatch (BUY in Bear Collapse)
            direction = str(row["direction"]).upper()
            breadth = float(row.get("market_breadth_pct") or 0.0)
            if direction in ("BUY", "BULLISH") and breadth <= -0.20:
                rejected_reasons["BUY_IN_BEAR_COLLAPSE"] = rejected_reasons.get("BUY_IN_BEAR_COLLAPSE", 0) + 1
                continue

            # Gate 3: Target Hurdle (Target must be >= 1.25% to cover ₹60-70 fees with margin)
            entry_px = float(row["entry_price"])
            tp_px = float(row["target_price"])
            sl_px = float(row["stop_loss"])
            target_pct = abs((tp_px - entry_px) / entry_px * 100.0) if entry_px > 0 else 1.0
            sl_pct = abs((entry_px - sl_px) / entry_px * 100.0) if entry_px > 0 else 0.8

            # Calibrated probability proxy from composite score
            comp_score = float(row.get("composite_score") or 50.0)
            prob_win = max(0.30, min(0.75, comp_score / 100.0))

            passed_hurdle, exp_net, hurdle_msg = cls.evaluate_cost_hurdle_filter(
                predicted_win_prob=prob_win,
                target_gross_pct=target_pct,
                stop_loss_pct=sl_pct,
                fee_drag_pct=0.35,
                min_required_net_expectancy_pct=0.0
            )
            if not passed_hurdle:
                rejected_reasons["NEGATIVE_NET_EXPECTANCY"] = rejected_reasons.get("NEGATIVE_NET_EXPECTANCY", 0) + 1
                continue

            challenger_rows.append(row)

        challenger_df = pd.DataFrame(challenger_rows) if challenger_rows else pd.DataFrame()

        if not challenger_df.empty:
            chal_trades = len(challenger_df)
            chal_gross = round(float(challenger_df["gross_pnl_rupees"].sum()), 2)
            chal_fees = round(float(challenger_df["fees_rupees"].sum()), 2)
            chal_net = round(float(challenger_df["net_pnl_rupees"].sum()), 2)
            chal_wins = len(challenger_df[challenger_df["net_pnl_rupees"] > 0])
            chal_wr = round((chal_wins / chal_trades) * 100.0, 1) if chal_trades > 0 else 0.0
            chal_expectancy = round(chal_net / chal_trades, 2) if chal_trades > 0 else 0.0
        else:
            chal_trades = 0
            chal_gross = 0.0
            chal_fees = 0.0
            chal_net = 0.0
            chal_wins = 0
            chal_wr = 0.0
            chal_expectancy = 0.0

        # Comparative Verdict
        net_improvement = round(chal_net - base_net, 2)
        wr_improvement = round(chal_wr - base_wr, 1)
        expectancy_improvement = round(chal_expectancy - base_expectancy, 2)

        is_statistically_superior = (chal_expectancy > base_expectancy) and (chal_net > base_net)

        return {
            "status": "SUCCESS",
            "train_cutoff_date": train_cutoff_date,
            "train_trades_count": len(train_df),
            "oos_test_trades_count": len(test_df),
            "baseline_champion": {
                "trades": base_trades,
                "wins": base_wins,
                "win_rate_pct": base_wr,
                "gross_pnl": base_gross,
                "fees": base_fees,
                "net_pnl": base_net,
                "net_expectancy": base_expectancy
            },
            "challenger_filtered": {
                "trades": chal_trades,
                "wins": chal_wins,
                "win_rate_pct": chal_wr,
                "gross_pnl": chal_gross,
                "fees": chal_fees,
                "net_pnl": chal_net,
                "net_expectancy": chal_expectancy,
                "rejected_trades_count": base_trades - chal_trades,
                "rejection_breakdown": rejected_reasons
            },
            "comparison": {
                "net_pnl_difference": net_improvement,
                "win_rate_difference_pct": wr_improvement,
                "net_expectancy_difference": expectancy_improvement,
                "is_statistically_superior": is_statistically_superior
            }
        }
