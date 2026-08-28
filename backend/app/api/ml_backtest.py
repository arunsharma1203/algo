from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.svm import SVC
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.ensemble import VotingClassifier
import numpy as np

router = APIRouter()

class MLBacktestRequest(BaseModel):
    ticker: str
    model_type: str # 'SWING' or 'INTRADAY'

def calculate_metrics(equity_curve, trades, initial_capital=100000):
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0,
            "max_drawdown": 0.0, "sharpe_ratio": 0.0, "final_equity": initial_capital
        }
        
    winning_trades = [t for t in trades if t['pnl'] > 0]
    win_rate = (len(winning_trades) / len(trades)) * 100
    
    total_pnl = sum(t['pnl'] for t in trades)
    
    # Drawdown
    eq_series = pd.Series([point['equity'] for point in equity_curve])
    cummax = eq_series.cummax()
    drawdown = (eq_series - cummax) / cummax
    max_drawdown = abs(drawdown.min() * 100) if not drawdown.empty else 0
    
    # Sharpe (approximate daily)
    if len(eq_series) > 1:
        daily_returns = eq_series.pct_change().dropna()
        if daily_returns.std() != 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
        else:
            sharpe = 0
    else:
        sharpe = 0
        
    return {
        "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_drawdown, 2),
        "sharpe_ratio": round(sharpe, 2),
        "final_equity": round(eq_series.iloc[-1] if not eq_series.empty else initial_capital, 2)
    }

@router.post("/backtest-simulate")
async def run_ml_backtest(req: MLBacktestRequest):
    try:
        ticker = req.ticker
        model_type = req.model_type
        
        # 1. Fetch Data
        if model_type == "SWING":
            df = yf.download(ticker, period="5y", interval="1d", progress=False)
            if df.empty or len(df) < 500:
                raise Exception("Not enough daily data for Swing ML backtest (need at least 2 years).")
        else:
            df = yf.download(ticker, period="60d", interval="15m", progress=False)
            if df.empty or len(df) < 500:
                raise Exception("Not enough 15m data for Intraday ML backtest.")
                
        # 2. Feature Engineering
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
            
        df['close'] = df['Close']
        df['high'] = df['High']
        df['low'] = df['Low']
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # MACD
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        df['macd_diff'] = macd - signal
        
        # ATR
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        df['atr'] = true_range.rolling(14).mean()
        
        # ADX Approximation (Simplified for speed)
        df['adx'] = true_range.rolling(14).mean() / df['close'] * 100 
        
        df = df.dropna()
        
        # 3. Label Engineering (The "Future")
        if model_type == "SWING":
            # Swing looks 5 days ahead for a 3% gain
            df['Future_Ret'] = df['close'].shift(-5) / df['close'] - 1
            df['Target'] = (df['Future_Ret'] > 0.03).astype(int)
        else:
            # Intraday looks 4 candles (1 hour) ahead for a 0.5% gain
            df['Future_Ret'] = df['close'].shift(-4) / df['close'] - 1
            df['Target'] = (df['Future_Ret'] > 0.005).astype(int)
            
        df = df.dropna()
        
        if len(df) < 100:
            raise Exception("Not enough valid feature rows to train the model.")
            
        # 4. Train/Test Split
        # Train on first 70% of data, Backtest on final 30%
        split_idx = int(len(df) * 0.7)
        train_df = df.iloc[:split_idx]
        test_df = df.iloc[split_idx:]
        
        features = ['rsi', 'macd_diff', 'adx', 'atr']
        X_train = train_df[features]
        y_train = train_df['Target']
        
        if y_train.sum() == 0 or y_train.sum() == len(y_train):
            raise Exception("No positive targets found in training data. Model cannot learn.")
            
        # 5. Train the Ensemble Model Natively
        rf_clf = RandomForestClassifier(n_estimators=50, random_state=42, max_depth=5)
        gb_clf = GradientBoostingClassifier(n_estimators=50, learning_rate=0.1, max_depth=3, random_state=42)
        svm_clf = make_pipeline(StandardScaler(), SVC(probability=True, random_state=42))
        
        ensemble = VotingClassifier(
            estimators=[('rf', rf_clf), ('gb', gb_clf), ('svm', svm_clf)],
            voting='soft'
        )
        ensemble.fit(X_train, y_train)
        
        # 6. Walk-Forward Backtest Simulator
        capital = 100000
        equity_curve = []
        trades = []
        
        in_trade = False
        entry_price = 0
        sl_price = 0
        tp_price = 0
        trade_start_date = None
        qty = 0
        
        for idx, row in test_df.iterrows():
            current_price = row['close']
            current_date = idx.isoformat() if hasattr(idx, 'isoformat') else str(idx)
            
            # Record daily equity
            if in_trade:
                current_equity = capital + (qty * (current_price - entry_price))
            else:
                current_equity = capital
                
            equity_curve.append({
                "date": current_date.split('T')[0] if 'T' in current_date else current_date.split(' ')[0],
                "equity": round(current_equity, 2),
                "close": round(current_price, 2)
            })
            
            # Manage Open Trade
            if in_trade:
                if row['low'] <= sl_price:
                    # SL Hit
                    exit_price = sl_price
                    pnl = qty * (exit_price - entry_price)
                    capital += pnl
                    trades.append({
                        "entry_date": trade_start_date,
                        "exit_date": current_date,
                        "type": "LONG",
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "pnl": round(pnl, 2),
                        "status": "SL HIT"
                    })
                    in_trade = False
                elif row['high'] >= tp_price:
                    # TP Hit
                    exit_price = tp_price
                    pnl = qty * (exit_price - entry_price)
                    capital += pnl
                    trades.append({
                        "entry_date": trade_start_date,
                        "exit_date": current_date,
                        "type": "LONG",
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "pnl": round(pnl, 2),
                        "status": "TARGET MET"
                    })
                    in_trade = False
                # EOD Square off for Intraday (Simulated at end of dataset or end of day)
                # For simplicity, we hold until SL or TP in this mock
                continue
                
            # Scan for New Trade
            if not in_trade:
                X_live = row[features].values.reshape(1, -1)
                prob = ensemble.predict_proba(X_live)[0][1]
                
                # If highly confident
                if prob > 0.55:
                    entry_price = current_price
                    # Set SL and TP
                    if model_type == "SWING":
                        sl_price = entry_price - (2 * row['atr'])
                        tp_price = entry_price + (4 * row['atr'])
                    else:
                        sl_price = entry_price - (1.5 * row['atr'])
                        tp_price = entry_price + (3 * row['atr'])
                        
                    qty = int(capital / entry_price)
                    trade_start_date = current_date
                    in_trade = True
                    
        # Close any open trades at the end of the simulation
        if in_trade:
            exit_price = test_df.iloc[-1]['close']
            pnl = qty * (exit_price - entry_price)
            capital += pnl
            trades.append({
                "entry_date": trade_start_date,
                "exit_date": current_date,
                "type": "LONG",
                "entry_price": round(entry_price, 2),
                "exit_price": round(exit_price, 2),
                "pnl": round(pnl, 2),
                "status": "SQUARED OFF"
            })
            # Update the last equity point
            equity_curve[-1]['equity'] = round(capital, 2)
            
        metrics = calculate_metrics(equity_curve, trades)
        
        return {
            "status": "success",
            "metrics": metrics,
            "trades": trades[::-1], # Newest first
            "equity_curve": equity_curve
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=400, detail=str(e))
