from fastapi import APIRouter, HTTPException
from app.models.strategy import BacktestRequest, IndicatorDef
from app.data.market_data import fetch_historical_data
from app.indicators.engine import apply_indicators
from app.backtesting.engine import BacktestEngine
from app.analytics.metrics import calculate_metrics

router = APIRouter()

@router.post("/")
async def run_backtest(req: BacktestRequest):
    try:
        import time
        start_time = time.time()
        
        # 1. Fetch Data
        if req.interval == '15m':
            from app.api.ml_history import get_ml_training_data
            hist_df = get_ml_training_data(req.ticker)
            
            # Fetch recent live 15m to supplement
            import yfinance as yf
            live_df = yf.download(req.ticker, period="60d", interval="15m", progress=False)
            if not live_df.empty:
                live_df = live_df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
                live_df = live_df.reset_index()
                live_df = live_df.rename(columns={live_df.columns[0]: 'datetime'})
                live_df['datetime'] = live_df['datetime'].astype(str)
                hist_df['datetime'] = hist_df['datetime'].astype(str)
                df = pd.concat([hist_df, live_df]).drop_duplicates(subset=['datetime'], keep='last').sort_values('datetime')
            else:
                df = hist_df
                
            # Filter by date range
            if not df.empty:
                df['date_only'] = df['datetime'].str[:10]
                df = df[(df['date_only'] >= req.start_date) & (df['date_only'] <= req.end_date)].copy()
                df = df.drop(columns=['date_only'])
                df['date'] = df['datetime']
            
            if df.empty:
                raise Exception(f"No 15m data available for {req.ticker} in this date range.")
                
            df.attrs["source"] = "AI Intraday Vault"
        else:
            df = fetch_historical_data(req.ticker, req.start_date, req.end_date)
        
        # 2. Extract Required Indicators from Strategy JSON
        needed_indicators = []
        def _extract_indicators(conds):
            for c in conds:
                if isinstance(c.left, IndicatorDef): needed_indicators.append(c.left)
                if isinstance(c.right, IndicatorDef): needed_indicators.append(c.right)
                
        if req.strategy.entry.conditions:
            _extract_indicators(req.strategy.entry.conditions)
        if req.strategy.exit.conditions:
            _extract_indicators(req.strategy.exit.conditions)
            
        # 3. Apply Indicators
        df = apply_indicators(df, needed_indicators)
        
        # 4. Run Backtest
        engine = BacktestEngine(df, req.strategy, req.initial_capital)
        bt_results = engine.run()
        
        # 5. Calculate Metrics
        metrics = calculate_metrics(bt_results['equity_curve'], bt_results['trades'], req.initial_capital)
        
        # Add Drawdown to equity curve for charting
        eq_df = pd.DataFrame(bt_results['equity_curve'])
        if not eq_df.empty:
            eq_df['cummax'] = eq_df['equity'].cummax()
            eq_df['drawdown'] = (eq_df['equity'] - eq_df['cummax']) / eq_df['cummax'] * 100
            bt_results['equity_curve'] = eq_df[['date', 'equity', 'close', 'drawdown']].to_dict(orient='records')
        
        exec_time = time.time() - start_time
        
        return {
            "status": "success",
            "metrics": metrics,
            "trades": bt_results['trades'],
            "equity_curve": bt_results['equity_curve'],
            "metadata": {
                "execution_time_ms": int(exec_time * 1000),
                "data_points": len(df),
                "source": df.attrs.get("source", "Yahoo Finance")
            }
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))
        
import pandas as pd # Needed for drawdown calculation in the router
