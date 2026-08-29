import numpy as np
import pandas as pd
from typing import List, Dict, Any

def calculate_advanced_metrics(trades: List[Dict[str, Any]], equity_curve: List[Dict[str, Any]], initial_capital: float = 100000.0) -> Dict[str, Any]:
    """
    Computes comprehensive institutional-grade quantitative metrics for strategy backtesting.
    """
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "final_equity": initial_capital,
            "max_drawdown": 0.0,
            "max_drawdown_duration_bars": 0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "win_loss_ratio": 0.0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0
        }

    pnls = [float(t.get('pnl', 0.0)) for t in trades]
    winning_trades = [p for p in pnls if p > 0]
    losing_trades = [p for p in pnls if p < 0]

    win_count = len(winning_trades)
    loss_count = len(losing_trades)
    total_trades = len(trades)
    win_rate = (win_count / total_trades) * 100.0 if total_trades > 0 else 0.0

    total_pnl = sum(pnls)
    gross_profit = sum(winning_trades)
    gross_loss = abs(sum(losing_trades))

    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (round(gross_profit, 2) if gross_profit > 0 else 1.0)
    avg_win = round(gross_profit / win_count, 2) if win_count > 0 else 0.0
    avg_loss = round(gross_loss / loss_count, 2) if loss_count > 0 else 0.0
    win_loss_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else (avg_win if avg_win > 0 else 1.0)

    # Expectancy ($): (P_win * Avg Win) - (P_loss * Avg Loss)
    p_win = win_count / total_trades if total_trades > 0 else 0
    p_loss = loss_count / total_trades if total_trades > 0 else 0
    expectancy = round((p_win * avg_win) - (p_loss * avg_loss), 2)

    # Drawdown & Durations
    eq_values = [float(point.get('equity', initial_capital)) for point in equity_curve] if equity_curve else [initial_capital]
    eq_series = pd.Series(eq_values)
    cummax = eq_series.cummax()
    dd_series = (eq_series - cummax) / cummax
    max_drawdown = abs(float(dd_series.min() * 100)) if not dd_series.empty else 0.0

    # Max Drawdown Duration (consecutive bars in drawdown)
    in_dd = dd_series < 0
    dd_durations = (~in_dd).cumsum()[in_dd].value_counts()
    max_dd_duration = int(dd_durations.max()) if not dd_durations.empty else 0

    # Sharpe & Sortino (Annualized with 252 trading days)
    if len(eq_series) > 1:
        returns = eq_series.pct_change().dropna()
        mean_ret = returns.mean()
        std_ret = returns.std()
        
        # Downside deviation only (for Sortino)
        downside_returns = returns[returns < 0]
        downside_std = downside_returns.std() if len(downside_returns) > 1 else std_ret
        
        sharpe = round((mean_ret / std_ret) * np.sqrt(252), 2) if std_ret > 0 else 0.0
        sortino = round((mean_ret / downside_std) * np.sqrt(252), 2) if (downside_std and downside_std > 0) else sharpe
    else:
        sharpe = 0.0
        sortino = 0.0

    # Consecutive Streaks
    max_cons_wins = 0
    max_cons_losses = 0
    curr_wins = 0
    curr_losses = 0
    for p in pnls:
        if p > 0:
            curr_wins += 1
            curr_losses = 0
            if curr_wins > max_cons_wins:
                max_cons_wins = curr_wins
        elif p < 0:
            curr_losses += 1
            curr_wins = 0
            if curr_losses > max_cons_losses:
                max_cons_losses = curr_losses
        else:
            curr_wins = 0
            curr_losses = 0

    return {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "final_equity": round(eq_values[-1], 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_duration_bars": max_dd_duration,
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        "max_consecutive_wins": max_cons_wins,
        "max_consecutive_losses": max_cons_losses
    }

def run_monte_carlo_simulation(trades: List[Dict[str, Any]], initial_capital: float = 100000.0, n_simulations: int = 1000, horizon_trades: int = 50) -> Dict[str, Any]:
    """
    Runs a 1,000-path Monte Carlo Bootstrap Resampling Simulation to forecast portfolio equity dispersion,
    worst-case drawdowns, and ruin probability.
    """
    if not trades:
        cone = [{"trade_num": i, "p05": initial_capital, "p25": initial_capital, "p50": initial_capital, "p75": initial_capital, "p95": initial_capital} for i in range(horizon_trades + 1)]
        return {
            "confidence_cone": cone,
            "expected_max_drawdown": 0.0,
            "prob_drawdown_25pct": 0.0,
            "prob_drawdown_50pct": 0.0,
            "p05_final_equity": initial_capital,
            "p50_final_equity": initial_capital,
            "p95_final_equity": initial_capital,
            "n_simulations": n_simulations
        }

    trade_pct_returns = []
    for t in trades:
        entry = float(t.get('entry_price', 1.0))
        pnl = float(t.get('pnl', 0.0))
        if entry > 0:
            trade_pct_returns.append(pnl / initial_capital)
        else:
            trade_pct_returns.append(0.0)

    trade_pct_returns = np.array(trade_pct_returns)
    if len(trade_pct_returns) == 0:
        trade_pct_returns = np.array([0.0])

    np.random.seed(42)
    simulated_paths = np.zeros((n_simulations, horizon_trades + 1))
    simulated_paths[:, 0] = initial_capital

    max_drawdowns = []
    ruin_count_25 = 0
    ruin_count_50 = 0

    for sim_idx in range(n_simulations):
        sampled_returns = np.random.choice(trade_pct_returns, size=horizon_trades, replace=True)
        equity_path = np.zeros(horizon_trades + 1)
        equity_path[0] = initial_capital
        
        current_eq = initial_capital
        peak_eq = initial_capital
        max_dd_sim = 0.0
        
        for step_idx, ret in enumerate(sampled_returns, 1):
            current_eq = max(0.0, current_eq + (initial_capital * ret))
            equity_path[step_idx] = current_eq
            
            if current_eq > peak_eq:
                peak_eq = current_eq
            dd = (peak_eq - current_eq) / peak_eq if peak_eq > 0 else 0.0
            if dd > max_dd_sim:
                max_dd_sim = dd

        simulated_paths[sim_idx, :] = equity_path
        max_drawdowns.append(max_dd_sim * 100.0)
        
        if max_dd_sim >= 0.25:
            ruin_count_25 += 1
        if max_dd_sim >= 0.50:
            ruin_count_50 += 1

    p05 = np.percentile(simulated_paths, 5, axis=0)
    p25 = np.percentile(simulated_paths, 25, axis=0)
    p50 = np.percentile(simulated_paths, 50, axis=0)
    p75 = np.percentile(simulated_paths, 75, axis=0)
    p95 = np.percentile(simulated_paths, 95, axis=0)

    confidence_cone = []
    for step in range(horizon_trades + 1):
        confidence_cone.append({
            "trade_num": step,
            "p05": round(float(p05[step]), 2),
            "p25": round(float(p25[step]), 2),
            "p50": round(float(p50[step]), 2),
            "p75": round(float(p75[step]), 2),
            "p95": round(float(p95[step]), 2)
        })

    return {
        "confidence_cone": confidence_cone,
        "expected_max_drawdown": round(float(np.median(max_drawdowns)), 2),
        "prob_drawdown_25pct": round((ruin_count_25 / n_simulations) * 100.0, 1),
        "prob_drawdown_50pct": round((ruin_count_50 / n_simulations) * 100.0, 1),
        "p05_final_equity": round(float(p05[-1]), 2),
        "p50_final_equity": round(float(p50[-1]), 2),
        "p95_final_equity": round(float(p95[-1]), 2),
        "n_simulations": n_simulations
    }
