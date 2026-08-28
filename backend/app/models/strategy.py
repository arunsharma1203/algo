from pydantic import BaseModel
from typing import List, Optional, Union, Dict, Any

class IndicatorDef(BaseModel):
    name: str  # EMA, SMA, RSI, MACD, close, volume
    params: Dict[str, Any] = {} # e.g. {"period": 20}

class ConditionDef(BaseModel):
    left: Union[IndicatorDef, float]
    operator: str # >, <, >=, <=, ==, !=, crosses_above, crosses_below
    right: Union[IndicatorDef, float]

class RuleGroup(BaseModel):
    logic: str # ALL (AND) or ANY (OR)
    conditions: List[ConditionDef]

class RiskManagement(BaseModel):
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    trailing_stop_pct: Optional[float] = None

class StrategyJSON(BaseModel):
    name: str
    entry: RuleGroup
    exit: RuleGroup
    risk: RiskManagement
    execution: Dict[str, Any] = {"model": "next_open"}

class BacktestRequest(BaseModel):
    ticker: str
    start_date: str
    end_date: str
    initial_capital: float = 100000.0
    interval: str = '1d' # 1d or 15m
    strategy: StrategyJSON
