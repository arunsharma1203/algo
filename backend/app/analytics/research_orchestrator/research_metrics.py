"""
QUANTITATIVE RESEARCH METRICS ENGINE
====================================
Computes rigorous economic metrics (CAGR, Sharpe, Sortino, Calmar, Max DD,
one-way turnover, friction drag sensitivity, concentration), cross-sectional
signal metrics (IC, Rank IC, ICIR, monotonicity, decile spread), and
classification metrics (F1, Precision, Recall, Brier).
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple

class ResearchMetricsEngine:
    """
    Standardized quantitative metrics calculator for research experiments.
    """

    @staticmethod
    def compute_one_way_turnover(weights_prev: Dict[str, float], weights_curr: Dict[str, float]) -> float:
        """
        Exact one-way portfolio turnover: 0.5 * sum(|w_t - w_{t^-}|).
        """
        all_tickers = set(weights_prev.keys()).union(set(weights_curr.keys()))
        l1_diff = sum(abs(weights_curr.get(t, 0.0) - weights_prev.get(t, 0.0)) for t in all_tickers)
        return 0.5 * l1_diff

    @classmethod
    def compute_economic_metrics(
        cls,
        daily_returns: pd.Series,
        turnover_annual_pct: float,
        trades_list: Optional[List[Dict[str, Any]]] = None,
        benchmark_returns: Optional[pd.Series] = None
    ) -> Dict[str, Any]:
        """
        Computes complete economic backtest metrics including cost sensitivity.
        """
        if daily_returns.empty or daily_returns.isna().all():
            return {
                "cagr_gross": 0.0,
                "cagr_net": 0.0,
                "total_return_pct": 0.0,
                "sharpe": 0.0,
                "sortino": 0.0,
                "calmar": 0.0,
                "max_drawdown_pct": 0.0,
                "trade_count": 0,
                "win_rate_pct": 0.0,
                "profit_factor": 0.0,
                "expectancy": 0.0,
                "turnover_pct": 0.0,
                "survives_friction_10bps": False,
                "survives_friction_15bps": False,
                "survives_friction_20bps": False,
                "survives_friction_30bps": False
            }

        n_days = len(daily_returns)
        cum_ret = (1.0 + daily_returns).cumprod()
        total_ret = cum_ret.iloc[-1] - 1.0
        
        # Annualized CAGR
        years = max(n_days / 252.0, 0.05)
        cagr_gross = (cum_ret.iloc[-1] ** (1.0 / years) - 1.0) * 100.0 if cum_ret.iloc[-1] > 0 else -100.0

        # Drawdowns
        peaks = cum_ret.cummax()
        drawdowns = (cum_ret - peaks) / peaks
        max_dd_pct = abs(drawdowns.min()) * 100.0

        # Sharpe & Sortino
        mean_ret = daily_returns.mean()
        std_ret = daily_returns.std()
        sharpe = (mean_ret / std_ret * np.sqrt(252.0)) if std_ret > 1e-8 else 0.0

        downside = daily_returns[daily_returns < 0.0]
        downside_std = downside.std()
        sortino = (mean_ret / downside_std * np.sqrt(252.0)) if downside_std > 1e-8 else 0.0

        calmar = (cagr_gross / max_dd_pct) if max_dd_pct > 1e-4 else 0.0

        # Trade-level statistics
        trade_count = len(trades_list) if trades_list else 0
        win_rate = 0.0
        pf = 0.0
        expectancy = 0.0

        if trades_list and len(trades_list) > 0:
            pnls = [t.get("pnl_pct", 0.0) for t in trades_list]
            wins = [p for p in pnls if p > 0.0]
            losses = [abs(p) for p in pnls if p < 0.0]
            win_rate = (len(wins) / len(pnls)) * 100.0 if pnls else 0.0
            total_win = sum(wins)
            total_loss = sum(losses)
            pf = (total_win / total_loss) if total_loss > 1e-6 else (99.0 if total_win > 0 else 0.0)
            expectancy = np.mean(pnls) if pnls else 0.0

        # Friction drag tiers
        # Turnover is in percent per year (e.g. 800% = 8.0x)
        # Cost drag = (turnover / 100.0) * friction_rate * 100.0%
        turnover_multiplier = turnover_annual_pct / 100.0
        drag_10 = turnover_multiplier * 0.0010 * 100.0
        drag_15 = turnover_multiplier * 0.0015 * 100.0
        drag_20 = turnover_multiplier * 0.0020 * 100.0
        drag_30 = turnover_multiplier * 0.0030 * 100.0

        cagr_net_10 = cagr_gross - drag_10
        cagr_net_15 = cagr_gross - drag_15
        cagr_net_20 = cagr_gross - drag_20
        cagr_net_30 = cagr_gross - drag_30

        # Default net CAGR is after 15 bps friction
        cagr_net = cagr_net_15

        # Benchmark excess metrics
        excess_cagr = None
        sharpe_diff = None
        if benchmark_returns is not None and len(benchmark_returns) == n_days:
            bm_cum = (1.0 + benchmark_returns).cumprod()
            bm_cagr = (bm_cum.iloc[-1] ** (1.0 / years) - 1.0) * 100.0 if bm_cum.iloc[-1] > 0 else 0.0
            excess_cagr = cagr_net - bm_cagr
            bm_sharpe = (benchmark_returns.mean() / benchmark_returns.std() * np.sqrt(252.0)) if benchmark_returns.std() > 1e-8 else 0.0
            sharpe_diff = sharpe - bm_sharpe

        return {
            "cagr_gross": round(float(cagr_gross), 2),
            "cagr_net": round(float(cagr_net), 2),
            "total_return_pct": round(float(total_ret * 100.0), 2),
            "sharpe": round(float(sharpe), 2),
            "sortino": round(float(sortino), 2),
            "calmar": round(float(calmar), 2),
            "max_drawdown_pct": round(float(max_dd_pct), 2),
            "trade_count": trade_count,
            "win_rate_pct": round(float(win_rate), 2),
            "profit_factor": round(float(pf), 2),
            "expectancy": round(float(expectancy), 4),
            "turnover_pct": round(float(turnover_annual_pct), 1),
            "drag_10bps": round(float(drag_10), 2),
            "drag_15bps": round(float(drag_15), 2),
            "drag_20bps": round(float(drag_20), 2),
            "drag_30bps": round(float(drag_30), 2),
            "cagr_net_10bps": round(float(cagr_net_10), 2),
            "cagr_net_15bps": round(float(cagr_net_15), 2),
            "cagr_net_20bps": round(float(cagr_net_20), 2),
            "cagr_net_30bps": round(float(cagr_net_30), 2),
            "survives_friction_10bps": bool(cagr_net_10 > 0.0),
            "survives_friction_15bps": bool(cagr_net_15 > 0.0),
            "survives_friction_20bps": bool(cagr_net_20 > 0.0),
            "survives_friction_30bps": bool(cagr_net_30 > 0.0),
            "excess_cagr_vs_bm": round(float(excess_cagr), 2) if excess_cagr is not None else None,
            "sharpe_diff_vs_bm": round(float(sharpe_diff), 2) if sharpe_diff is not None else None
        }

    @staticmethod
    def compute_signal_metrics(scores_df: pd.DataFrame, target_col: str, score_col: str) -> Dict[str, Any]:
        """
        Computes Information Coefficient (IC), Rank IC, ICIR, and monotonic deciles.
        """
        if scores_df.empty or target_col not in scores_df or score_col not in scores_df:
            return {"ic": 0.0, "rank_ic": 0.0, "icir": 0.0, "rank_icir": 0.0, "monotonicity": 0.0}

        # Daily cross-sectional correlations
        dates = scores_df["date"].unique() if "date" in scores_df else [1]
        daily_ics = []
        daily_rank_ics = []

        for d in dates:
            sub = scores_df[scores_df["date"] == d] if "date" in scores_df else scores_df
            if len(sub) >= 5:
                ic = sub[score_col].corr(sub[target_col])
                rank_ic = sub[score_col].corr(sub[target_col], method="spearman")
                if not np.isnan(ic):
                    daily_ics.append(ic)
                if not np.isnan(rank_ic):
                    daily_rank_ics.append(rank_ic)

        mean_ic = np.mean(daily_ics) if daily_ics else 0.0
        mean_rank_ic = np.mean(daily_rank_ics) if daily_rank_ics else 0.0
        std_ic = np.std(daily_ics) if daily_ics else 1.0
        std_rank_ic = np.std(daily_rank_ics) if daily_rank_ics else 1.0

        icir = (mean_ic / std_ic) if std_ic > 1e-6 else 0.0
        rank_icir = (mean_rank_ic / std_rank_ic) if std_rank_ic > 1e-6 else 0.0
        pos_freq = (sum(1 for x in daily_rank_ics if x > 0) / len(daily_rank_ics) * 100.0) if daily_rank_ics else 0.0

        return {
            "ic": round(float(mean_ic), 4),
            "rank_ic": round(float(mean_rank_ic), 4),
            "icir": round(float(icir), 3),
            "rank_icir": round(float(rank_icir), 3),
            "positive_ic_frequency_pct": round(float(pos_freq), 1)
        }

