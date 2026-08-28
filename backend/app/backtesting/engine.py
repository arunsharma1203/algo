import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional
from app.models.strategy import StrategyJSON, ConditionDef, RuleGroup
from app.indicators.engine import get_indicator_col_name

class BacktestEngine:
    def __init__(self, df: pd.DataFrame, strategy: StrategyJSON, initial_capital: float = 100000.0):
        self.df = df
        self.strategy = strategy
        self.capital = initial_capital
        self.equity_curve = []
        self.trades = []
        
        self.position = 0 # Number of shares
        self.entry_price = 0.0
        self.entry_date = None
        self.trade_cost = 0.0
        
    def _evaluate_condition(self, condition: ConditionDef, row_idx: int) -> bool:
        # Get left value
        if isinstance(condition.left, float) or isinstance(condition.left, int):
            left_val = float(condition.left)
        else:
            col = get_indicator_col_name(condition.left)
            left_val = self.df.at[row_idx, col]
            
        # Get right value
        if isinstance(condition.right, float) or isinstance(condition.right, int):
            right_val = float(condition.right)
        else:
            col = get_indicator_col_name(condition.right)
            right_val = self.df.at[row_idx, col]
            
        # Check NaNs
        if pd.isna(left_val) or pd.isna(right_val):
            return False
            
        op = condition.operator
        if op == '>': return left_val > right_val
        elif op == '<': return left_val < right_val
        elif op == '>=': return left_val >= right_val
        elif op == '<=': return left_val <= right_val
        elif op == '==': return left_val == right_val
        elif op == '!=': return left_val != right_val
        elif op == 'crosses_above':
            if row_idx == 0: return False
            prev_left = self.df.at[row_idx-1, get_indicator_col_name(condition.left) if not isinstance(condition.left, (float, int)) else None]
            prev_right = self.df.at[row_idx-1, get_indicator_col_name(condition.right) if not isinstance(condition.right, (float, int)) else None]
            if pd.isna(prev_left) or pd.isna(prev_right): return False
            return (prev_left <= prev_right) and (left_val > right_val)
        elif op == 'crosses_below':
            if row_idx == 0: return False
            prev_left = self.df.at[row_idx-1, get_indicator_col_name(condition.left) if not isinstance(condition.left, (float, int)) else None]
            prev_right = self.df.at[row_idx-1, get_indicator_col_name(condition.right) if not isinstance(condition.right, (float, int)) else None]
            if pd.isna(prev_left) or pd.isna(prev_right): return False
            return (prev_left >= prev_right) and (left_val < right_val)
            
        return False
        
    def _evaluate_rule_group(self, group: RuleGroup, row_idx: int) -> bool:
        if not group.conditions:
            return False
            
        results = [self._evaluate_condition(c, row_idx) for c in group.conditions]
        
        if group.logic == 'ALL':
            return all(results)
        else:
            return any(results)

    def run(self):
        # Transaction costs simplified: 0.1% total per trade leg for Indian markets
        cost_pct = 0.001 
        
        for idx in range(len(self.df)):
            date = self.df.at[idx, 'date']
            close_price = self.df.at[idx, 'close']
            
            # Simple assumption: evaluate on close, execute on close for simplicity in MVP
            # "Next day open" could be added by executing at idx+1 open.
            
            if self.position == 0:
                # Check entry
                if self._evaluate_rule_group(self.strategy.entry, idx):
                    # Buy
                    max_buy_amt = self.capital
                    shares = max_buy_amt // close_price
                    if shares > 0:
                        cost = shares * close_price * cost_pct
                        self.position = shares
                        self.capital -= (shares * close_price + cost)
                        self.entry_price = close_price
                        self.entry_date = date
                        self.trade_cost += cost
            else:
                # Check exit
                exit_signal = False
                exit_reason = ""
                
                # 1. Check Strategy Exit Rules
                if self._evaluate_rule_group(self.strategy.exit, idx):
                    exit_signal = True
                    exit_reason = "Exit Rules Met"
                    
                # 2. Check Stop Loss
                if not exit_signal and self.strategy.risk.stop_loss_pct:
                    sl_price = self.entry_price * (1 - self.strategy.risk.stop_loss_pct/100)
                    if close_price <= sl_price:
                        exit_signal = True
                        exit_reason = "Stop Loss Hit"
                        
                # 3. Check Take Profit
                if not exit_signal and self.strategy.risk.take_profit_pct:
                    tp_price = self.entry_price * (1 + self.strategy.risk.take_profit_pct/100)
                    if close_price >= tp_price:
                        exit_signal = True
                        exit_reason = "Take Profit Hit"
                
                if exit_signal:
                    # Sell
                    cost = self.position * close_price * cost_pct
                    revenue = self.position * close_price
                    self.capital += (revenue - cost)
                    self.trade_cost += cost
                    
                    # Record Trade
                    gross_pnl = revenue - (self.position * self.entry_price)
                    net_pnl = gross_pnl - self.trade_cost
                    try:
                        holding_days = (pd.to_datetime(date) - pd.to_datetime(self.entry_date)).days
                    except:
                        holding_days = 0
                    
                    self.trades.append({
                        "entry_date": self.entry_date.isoformat() if hasattr(self.entry_date, 'isoformat') else str(self.entry_date),
                        "exit_date": date.isoformat() if hasattr(date, 'isoformat') else str(date),
                        "entry_price": self.entry_price,
                        "exit_price": close_price,
                        "shares": self.position,
                        "gross_pnl": gross_pnl,
                        "net_pnl": net_pnl,
                        "return_pct": (net_pnl / (self.position * self.entry_price)) * 100,
                        "holding_days": holding_days,
                        "exit_reason": exit_reason
                    })
                    
                    self.position = 0
                    self.entry_price = 0.0
                    self.trade_cost = 0.0

            # Record Equity
            current_value = self.capital + (self.position * close_price if self.position > 0 else 0)
            self.equity_curve.append({
                "date": date.isoformat() if hasattr(date, 'isoformat') else str(date),
                "equity": current_value,
                "close": close_price
            })
            
        return {
            "equity_curve": self.equity_curve,
            "trades": self.trades,
            "final_capital": self.capital + (self.position * self.df.at[len(self.df)-1, 'close'] if self.position > 0 else 0)
        }
