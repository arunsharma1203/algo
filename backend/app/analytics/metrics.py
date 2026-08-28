import pandas as pd
import numpy as np

def calculate_metrics(equity_curve: list, trades: list, initial_capital: float):
    if not equity_curve:
        return {}
        
    df_eq = pd.DataFrame(equity_curve)
    final_capital = df_eq.iloc[-1]['equity']
    
    total_return = ((final_capital - initial_capital) / initial_capital) * 100
    
    # CAGR
    days = (pd.to_datetime(df_eq.iloc[-1]['date']) - pd.to_datetime(df_eq.iloc[0]['date'])).days
    years = max(days / 365.25, 0.01)
    cagr = ((final_capital / initial_capital) ** (1/years) - 1) * 100
    
    # Drawdown
    df_eq['cummax'] = df_eq['equity'].cummax()
    df_eq['drawdown'] = (df_eq['equity'] - df_eq['cummax']) / df_eq['cummax'] * 100
    max_dd = df_eq['drawdown'].min()
    
    # Trade metrics
    total_trades = len(trades)
    winning_trades = len([t for t in trades if t['net_pnl'] > 0])
    losing_trades = total_trades - winning_trades
    
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    
    gross_profit = sum(t['net_pnl'] for t in trades if t['net_pnl'] > 0)
    gross_loss = abs(sum(t['net_pnl'] for t in trades if t['net_pnl'] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999 if gross_profit > 0 else 0)
    
    return {
        "initial_capital": initial_capital,
        "final_capital": final_capital,
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor
    }
