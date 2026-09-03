import math
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional
import logging

logger = logging.getLogger(__name__)

class QuantRiskEngine:
    """
    Financial & Quantitative Risk Engine.
    Computes rigorous portfolio performance metrics, statistical risk profiles (Sharpe, Sortino,
    Calmar, VaR, CVaR), strategy regime analysis, and model calibration drift tracking.
    """

    MIN_SAMPLE_FOR_STATS = 5
    RISK_FREE_RATE_ANNUAL = 0.065 # 6.5% RBI Repo Rate benchmark proxy

    @classmethod
    def compute_performance_metrics(cls, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Computes institutional-grade performance and risk metrics on trade history.
        Gracefully handles small sample sizes with INSUFFICIENT DATA markers.
        """
        if not trades:
            return cls._empty_metrics("No trades recorded.")

        df = pd.DataFrame(trades)
        
        # Filter for realized/closed trades
        if 'outcome' in df.columns:
            closed_mask = df['outcome'].notna() & (df['outcome'] != 'OPEN') & (df['outcome'] != '')
        elif 'status' in df.columns:
            closed_mask = df['status'] == 'CLOSED'
        else:
            closed_mask = pd.Series([False] * len(df))

        closed_df = df[closed_mask].copy()
        
        # Coerce numeric profit
        if 'profit_pct' in closed_df.columns:
            closed_df['profit_pct'] = pd.to_numeric(closed_df['profit_pct'], errors='coerce')
            closed_df = closed_df.dropna(subset=['profit_pct'])
        else:
            closed_df = pd.DataFrame()
        
        total_recorded = len(df)
        total_closed = len(closed_df)
        total_open = total_recorded - total_closed

        if total_closed == 0:
            return cls._empty_metrics("No closed trades evaluated yet.", total_recorded=total_recorded, total_open=total_open)

        returns = closed_df['profit_pct'].values
        
        # P&L calculations
        gross_pnl = float(np.sum(returns))
        # Incorporate transaction friction (slippage + statutory taxes ~0.10% per round trip)
        friction_per_trade = 0.10
        net_pnl = float(gross_pnl - (total_closed * friction_per_trade))

        wins = returns[returns > 0]
        losses = returns[returns < 0]
        evens = returns[returns == 0]

        win_count = len(wins)
        loss_count = len(losses)
        even_count = len(evens)

        win_rate = (win_count / total_closed) * 100.0
        loss_rate = (loss_count / total_closed) * 100.0

        avg_win = float(np.mean(wins)) if win_count > 0 else 0.0
        avg_loss = float(np.mean(losses)) if loss_count > 0 else 0.0
        win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

        total_gain = float(np.sum(wins)) if win_count > 0 else 0.0
        total_loss = abs(float(np.sum(losses))) if loss_count > 0 else 0.0
        profit_factor = (total_gain / total_loss) if total_loss > 0 else (float('inf') if total_gain > 0 else 0.0)

        # Mathematical Expectancy = (Win% * AvgWin) - (Loss% * AvgLoss)
        expectancy = ((win_rate / 100.0) * avg_win) + ((loss_rate / 100.0) * avg_loss)

        # Drawdown calculation
        cumulative_curve = np.cumsum(returns)
        running_max = np.maximum.accumulate(cumulative_curve)
        drawdowns = running_max - cumulative_curve
        max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
        recovery_factor = (net_pnl / max_drawdown) if max_drawdown > 0 else (net_pnl if net_pnl > 0 else 0.0)

        # Consecutive streaks
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        curr_wins = 0
        curr_losses = 0
        for r in returns:
            if r > 0:
                curr_wins += 1
                curr_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, curr_wins)
            elif r < 0:
                curr_losses += 1
                curr_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, curr_losses)
            else:
                curr_wins = 0
                curr_losses = 0

        # Sample size gate for statistical ratios
        is_statistically_significant = total_closed >= cls.MIN_SAMPLE_FOR_STATS
        
        if is_statistically_significant and np.std(returns) > 0:
            std_dev = float(np.std(returns))
            mean_ret = float(np.mean(returns))
            
            # Annualized daily Sharpe proxy (assuming 252 trading periods)
            rf_per_period = cls.RISK_FREE_RATE_ANNUAL / 252.0 * 100.0 # in %
            excess_return = mean_ret - rf_per_period
            sharpe_ratio = (excess_return / std_dev) * math.sqrt(252)

            # Sortino Ratio (Downside deviation only)
            downside_returns = returns[returns < 0]
            downside_std = float(np.std(downside_returns)) if len(downside_returns) > 1 else std_dev
            sortino_ratio = (excess_return / downside_std) * math.sqrt(252) if downside_std > 0 else 0.0

            # Calmar Ratio
            cagr_proxy = mean_ret * 252
            calmar_ratio = (cagr_proxy / max_drawdown) if max_drawdown > 0 else 0.0

            # Parametric & Historical Value at Risk (VaR 95%)
            var_95_historical = float(np.percentile(returns, 5))
            cvar_95 = float(np.mean(returns[returns <= var_95_historical])) if len(returns[returns <= var_95_historical]) > 0 else var_95_historical
            tail_loss_worst = float(np.min(returns))
        else:
            sharpe_ratio = None
            sortino_ratio = None
            calmar_ratio = None
            var_95_historical = None
            cvar_95 = None
            tail_loss_worst = float(np.min(returns)) if len(returns) > 0 else 0.0

        return {
            "status": "HEALTHY",
            "statistically_significant": is_statistically_significant,
            "sample_status": "SUFFICIENT DATA" if is_statistically_significant else f"INSUFFICIENT DATA ({total_closed}/{cls.MIN_SAMPLE_FOR_STATS} closed trades)",
            "total_recorded": total_recorded,
            "total_closed": total_closed,
            "total_open": total_open,
            "gross_pnl_pct": round(gross_pnl, 2),
            "net_pnl_pct": round(net_pnl, 2),
            "win_rate_pct": round(win_rate, 2),
            "loss_rate_pct": round(loss_rate, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "∞ (No Losses)",
            "expectancy_pct": round(expectancy, 2),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "win_loss_ratio": round(win_loss_ratio, 2) if win_loss_ratio != float('inf') else "∞",
            "max_drawdown_pct": round(max_drawdown, 2),
            "recovery_factor": round(recovery_factor, 2),
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
            "sharpe_ratio": round(sharpe_ratio, 2) if sharpe_ratio is not None else "INSUFFICIENT DATA",
            "sortino_ratio": round(sortino_ratio, 2) if sortino_ratio is not None else "INSUFFICIENT DATA",
            "calmar_ratio": round(calmar_ratio, 2) if calmar_ratio is not None else "INSUFFICIENT DATA",
            "var_95_pct": round(var_95_historical, 2) if var_95_historical is not None else "INSUFFICIENT DATA",
            "cvar_95_pct": round(cvar_95, 2) if cvar_95 is not None else "INSUFFICIENT DATA",
            "tail_loss_worst_pct": round(tail_loss_worst, 2),
            "transaction_friction_drag_pct": round(total_closed * friction_per_trade, 2)
        }

    @classmethod
    def compute_regime_analysis(cls, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyzes trading performance segmented by Macro & Volatility Regimes.
        """
        if not trades:
            return {"regimes": {}, "summary": "No trades available for regime segmentation."}

        df = pd.DataFrame(trades)
        if 'outcome' in df.columns:
            closed_mask = df['outcome'].notna() & (df['outcome'] != 'OPEN') & (df['outcome'] != '')
        elif 'status' in df.columns:
            closed_mask = df['status'] == 'CLOSED'
        else:
            closed_mask = pd.Series([False] * len(df))

        closed_df = df[closed_mask].copy()
        
        if closed_df.empty or 'profit_pct' not in closed_df.columns:
            return {"regimes": {}, "summary": "No closed trades available for regime segmentation."}

        closed_df['profit_pct'] = pd.to_numeric(closed_df['profit_pct'], errors='coerce')
        closed_df = closed_df.dropna(subset=['profit_pct'])

        # Extract macro regime from explanation JSON if present
        def extract_regime(row):
            exp = row.get('explanation')
            if isinstance(exp, dict):
                return exp.get('macro_regime', 'UNKNOWN')
            elif isinstance(exp, str) and 'macro_regime' in exp:
                try:
                    import json
                    parsed = json.loads(exp)
                    return parsed.get('macro_regime', 'UNKNOWN')
                except:
                    pass
            # Fallback to trade direction regime
            return row.get('direction', 'NEUTRAL')

        closed_df['regime'] = closed_df.apply(extract_regime, axis=1)

        regimes_output = {}
        for regime, group in closed_df.groupby('regime'):
            returns = group['profit_pct'].values
            n_trades = len(returns)
            wins = returns[returns > 0]
            losses = returns[returns < 0]
            
            w_rate = (len(wins) / n_trades) * 100.0 if n_trades > 0 else 0.0
            tot_gain = float(np.sum(wins)) if len(wins) > 0 else 0.0
            tot_loss = abs(float(np.sum(losses))) if len(losses) > 0 else 0.0
            pf = (tot_gain / tot_loss) if tot_loss > 0 else (float('inf') if tot_gain > 0 else 0.0)
            net_p = float(np.sum(returns))

            regimes_output[str(regime)] = {
                "trades": n_trades,
                "win_rate_pct": round(w_rate, 1),
                "profit_factor": round(pf, 2) if pf != float('inf') else "∞",
                "net_pnl_pct": round(net_p, 2),
                "expectancy_pct": round(float(np.mean(returns)), 2) if n_trades > 0 else 0.0
            }

        return {
            "regimes": regimes_output,
            "total_regimes_tracked": len(regimes_output),
            "summary": f"Tracked {len(closed_df)} closed trades across {len(regimes_output)} distinct market regimes."
        }

    @classmethod
    def compute_model_drift(cls, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Monitors model calibration drift, Brier score drift, and rolling decay over recent trade horizons.
        """
        if not trades:
            return {
                "health": "INSUFFICIENT_DATA",
                "recommendation": "MONITOR",
                "message": "No historical trades available to compute model drift."
            }

        df = pd.DataFrame(trades)
        if 'outcome' in df.columns:
            closed_mask = df['outcome'].notna() & (df['outcome'] != 'OPEN') & (df['outcome'] != '')
        elif 'status' in df.columns:
            closed_mask = df['status'] == 'CLOSED'
        else:
            closed_mask = pd.Series([False] * len(df))

        closed_df = df[closed_mask].copy()
        
        if len(closed_df) < 5 or 'profit_pct' not in closed_df.columns or 'confidence' not in closed_df.columns:
            return {
                "health": "INSUFFICIENT_DATA",
                "recommendation": "MONITOR",
                "trades_evaluated": len(closed_df),
                "message": f"Only {len(closed_df)} closed trades recorded. Minimum 5 required for drift calibration."
            }

        closed_df['profit_pct'] = pd.to_numeric(closed_df['profit_pct'], errors='coerce')
        closed_df['confidence'] = pd.to_numeric(closed_df['confidence'], errors='coerce')
        closed_df = closed_df.dropna(subset=['profit_pct', 'confidence'])

        confidences = (closed_df['confidence'].values / 100.0).clip(0.0, 1.0)
        outcomes = (closed_df['profit_pct'].values > 0).astype(int)

        # 1. Brier Score: Mean Squared Error between confidence and binary outcome
        brier_score = float(np.mean((confidences - outcomes) ** 2))

        # 2. Calibration Gap: Expected Win Rate vs Actual Win Rate
        mean_predicted_prob = float(np.mean(confidences))
        actual_win_rate = float(np.mean(outcomes))
        calibration_gap = abs(mean_predicted_prob - actual_win_rate)

        # 3. Rolling Win Rate over recent 20 trades
        recent_20 = closed_df.tail(20)
        recent_20_win_rate = float(np.mean(recent_20['profit_pct'].values > 0)) * 100.0 if len(recent_20) > 0 else actual_win_rate * 100.0
        lifetime_win_rate = actual_win_rate * 100.0

        # Health Classification
        if brier_score <= 0.20 and calibration_gap <= 0.15:
            health = "HEALTHY"
            recommendation = "NO ACTION"
            detail = "Model predictions match realized market outcomes with low Brier error."
        elif brier_score <= 0.28 and calibration_gap <= 0.25:
            health = "WATCH"
            recommendation = "MONITOR"
            detail = "Minor divergence between predicted confidence and outcome distribution."
        elif recent_20_win_rate < (lifetime_win_rate - 15.0):
            health = "DECAYING"
            recommendation = "OOS VALIDATE"
            detail = f"Recent 20 trades win rate ({recent_20_win_rate:.1f}%) lagged lifetime ({lifetime_win_rate:.1f}%)."
        else:
            health = "CRITICAL"
            recommendation = "RESEARCH"
            detail = f"Severe calibration gap ({calibration_gap:.2f}) and Brier score ({brier_score:.3f})."

        return {
            "health": health,
            "recommendation": recommendation,
            "trades_evaluated": len(closed_df),
            "brier_score": round(brier_score, 4),
            "expected_win_rate_pct": round(mean_predicted_prob * 100.0, 1),
            "actual_win_rate_pct": round(actual_win_rate * 100.0, 1),
            "calibration_gap_pct": round(calibration_gap * 100.0, 1),
            "recent_20_win_rate_pct": round(recent_20_win_rate, 1),
            "lifetime_win_rate_pct": round(lifetime_win_rate, 1),
            "detail": detail
        }

    @classmethod
    def _empty_metrics(cls, message: str, total_recorded: int = 0, total_open: int = 0) -> Dict[str, Any]:
        return {
            "status": "HEALTHY",
            "statistically_significant": False,
            "sample_status": "INSUFFICIENT DATA",
            "total_recorded": total_recorded,
            "total_closed": 0,
            "total_open": total_open,
            "gross_pnl_pct": 0.0,
            "net_pnl_pct": 0.0,
            "win_rate_pct": 0.0,
            "loss_rate_pct": 0.0,
            "profit_factor": "INSUFFICIENT DATA",
            "expectancy_pct": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "win_loss_ratio": "INSUFFICIENT DATA",
            "max_drawdown_pct": 0.0,
            "recovery_factor": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "sharpe_ratio": "INSUFFICIENT DATA",
            "sortino_ratio": "INSUFFICIENT DATA",
            "calmar_ratio": "INSUFFICIENT DATA",
            "var_95_pct": "INSUFFICIENT DATA",
            "cvar_95_pct": "INSUFFICIENT DATA",
            "tail_loss_worst_pct": 0.0,
            "transaction_friction_drag_pct": 0.0,
            "message": message
        }
