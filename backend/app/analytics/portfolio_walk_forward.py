import os
import time
import logging
import numpy as np
import pandas as pd
import ta
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Callable
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, VotingClassifier
from sklearn.svm import SVC
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import f1_score
from sklearn.linear_model import LogisticRegression

from app.data.historical_data_layer import HistoricalDataLayer
from app.analytics.universe_config import get_universe, UNIVERSE_PRESETS
from app.analytics.optuna_tuner import load_best_params
from app.analytics.kelly_sizer import calculate_kelly_position_size
from app.api.ml_backtest import calculate_indian_trade_friction
from app.analytics.monte_carlo import calculate_advanced_metrics, run_monte_carlo_simulation

logger = logging.getLogger(__name__)

class MultiStockPortfolioWalkForwardEngine:
    """
    Multi-Stock Portfolio Walk-Forward Backtester.
    Simulates cross-sectional trading across multiple stocks simultaneously over a shared capital pool,
    incorporating weekly retraining, Champion/Challenger gate, OOF Platt calibration,
    cross-sectional ranking, Fractional Kelly sizing, and a 6.0% Portfolio Heat ceiling.
    """

    def __init__(self, tickers: List[str], initial_capital: float = 500000.0,
                 max_portfolio_heat: float = 6.0, max_single_risk_pct: float = 2.0,
                 brokerage: float = 20.0, slippage_pct: float = 0.08, kelly_mode: str = "HALF",
                 universe_name: str = "BENCHMARK_5",
                 progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
                 worker_count: int = 4,
                 model_factory: Optional[Callable[[], Any]] = None,
                 model_type: str = "LIGHTGBM_ALPHA",
                 history_years: Optional[float] = None,
                 start_date: Optional[str] = None):
        self.tickers = [t.strip().upper() for t in tickers if t]
        self.initial_capital = float(initial_capital)
        self.capital = float(initial_capital)
        self.max_portfolio_heat = max_portfolio_heat
        self.max_single_risk_pct = max_single_risk_pct
        self.brokerage = brokerage
        self.slippage_pct = slippage_pct
        self.kelly_mode = kelly_mode
        self.universe_name = universe_name
        self.progress_callback = progress_callback
        self.worker_count = worker_count
        self.model_factory = model_factory
        self.model_type = model_type
        self.history_years = float(history_years) if history_years else None
        
        # Calculate start_date if history_years is provided and start_date is not
        if start_date:
            self.start_date = str(start_date)
        elif self.history_years:
            self.start_date = (datetime.now() - timedelta(days=int(365.25 * self.history_years))).strftime('%Y-%m-%d')
        else:
            self.start_date = None

    def run(self) -> Dict[str, Any]:
        # 1. Ingest & Align Historical Data for all Universe Stocks
        stock_dfs = {}
        features = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']
        if self.model_type in ("LIGHTGBM", "LIGHTGBM_ALPHA", "ALPHA158"):
            features.extend(['roc_5', 'roc_10', 'roc_20', 'volatility_20', 'range_ratio', 'kdj_k', 'zscore_20', 'bb_pct'])
        purge_bars = 5

        for ticker in self.tickers:
            df = HistoricalDataLayer.get_historical_ohlcv(ticker, timeframe="1d", start_date=self.start_date)
            if df.empty or len(df) < 300:
                continue

            df = df.copy()
            df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
            macd = ta.trend.MACD(df['close'])
            df['macd'] = macd.macd()
            df['macd_diff'] = macd.macd_diff()
            df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
            df['returns'] = df['close'].pct_change()
            df['macro_ema'] = df['close'].ewm(span=20, adjust=False).mean()
            df['macro_bullish'] = df['close'] >= df['macro_ema']

            if self.model_type in ("LIGHTGBM", "LIGHTGBM_ALPHA", "ALPHA158"):
                df['roc_5'] = df['close'].pct_change(5)
                df['roc_10'] = df['close'].pct_change(10)
                df['roc_20'] = df['close'].pct_change(20)
                df['volatility_20'] = df['returns'].rolling(20).std()
                df['range_ratio'] = (df['high'] - df['low']) / (df['close'] + 1e-6)
                roll_low_14 = df['low'].rolling(14).min()
                roll_high_14 = df['high'].rolling(14).max()
                df['kdj_k'] = (df['close'] - roll_low_14) / (roll_high_14 - roll_low_14 + 1e-6)
                roll_mean_20 = df['close'].rolling(20).mean()
                df['zscore_20'] = (df['close'] - roll_mean_20) / (df['close'].rolling(20).std() + 1e-6)
                bb_std = df['close'].rolling(20).std()
                bb_upper = roll_mean_20 + 2 * bb_std
                bb_lower = roll_mean_20 - 2 * bb_std
                df['bb_pct'] = (df['close'] - bb_lower) / (bb_upper - bb_lower + 1e-6)

            # Target: forward 5-day return > 0
            df['future_5d'] = df['close'].shift(-5)
            df['target'] = (((df['future_5d'] - df['close']) / df['close']) > 0.0).astype(int)

            clean = df.dropna(subset=features + ['target', 'macro_ema']).copy()
            if len(clean) >= 300:
                stock_dfs[ticker] = clean

        if len(stock_dfs) < 2:
            raise ValueError(f"Insufficient valid stocks in universe ({len(stock_dfs)}). Minimum 2 required.")

        # Find common calendar index
        all_dates = sorted(list(set.intersection(*[set(df.index) for df in stock_dfs.values()])))
        if len(all_dates) < 350:
            # Fallback to union sorted
            all_dates = sorted(list(set.union(*[set(df.index) for df in stock_dfs.values()])))

        total_dates = len(all_dates)
        min_anchor_bars = min(250, int(total_dates * 0.40)) # ~1 year
        bars_per_week = 5
        holdout_start_idx = int(total_dates * 0.85)

        # 2. Portfolio State
        cash = self.initial_capital
        open_positions: Dict[str, Dict[str, Any]] = {}
        portfolio_trades = []
        equity_curve = []
        weekly_lifecycle = []
        performance_by_regime = {"BULLISH": {"trades": 0, "pnl": 0.0, "wins": 0}, "BEARISH": {"trades": 0, "pnl": 0.0, "wins": 0}}
        yearly_perf = {}

        hp = load_best_params(timeframe="swing")
        hp["model_type"] = self.model_type
        if self.model_factory is not None:
            hp["use_fast_test_model"] = True

        active_champion = None
        champion_f1 = 0.695
        champion_version = "v1.0-portfolio"

        rebalance_indices = list(range(min_anchor_bars, total_dates, bars_per_week))
        total_cycles = len(rebalance_indices)

        # Initialize persistent worker pool if parallel execution requested
        executor = None
        if self.worker_count > 1:
            from app.analytics.parallel_engine import _run_cv_split_worker, _worker_init
            executor = ProcessPoolExecutor(
                max_workers=min(4, self.worker_count),
                initializer=_worker_init,
                initargs=(1,)
            )
            from app.analytics.process_lifecycle_manager import ProcessLifecycleManager
            pool_key = f"pwf_{self.universe_name}_{int(time.time())}"
            ProcessLifecycleManager.register_worker_pool(pool_key, executor=executor)
        else:
            pool_key = None

        try:
            start_simulation_time = time.time()

            # 3. Synchronous Weekly Walk-Forward Loop
            for rebalance_idx in rebalance_indices:
                cycle_start_time = time.time()
                current_date_dt = all_dates[rebalance_idx]
                rebalance_date_str = str(current_date_dt)[:10]

                # A. Slicing multi-asset training data up to T_rebalance - purge_bars
                train_sub_dfs = []
                for t, df_stock in stock_dfs.items():
                    past_df = df_stock[df_stock.index < current_date_dt]
                    if len(past_df) > purge_bars + 50:
                        train_sub_dfs.append(past_df.iloc[:-purge_bars])

                if not train_sub_dfs:
                    continue

                combined_train = pd.concat(train_sub_dfs).sort_index()
                X_train = combined_train[features].values
                y_train = combined_train['target'].values.astype(int)

                if len(np.unique(y_train)) < 2:
                    continue

                training_start_date = str(combined_train.index[0])[:10]
                training_end_date = str(combined_train.index[-1])[:10]

                # B. 4-Split OOF Challenger Cross-Validation with Sub-Cycle Telemetry
                tscv = TimeSeriesSplit(n_splits=4)
                oof_y_val = []
                oof_preds_proba = []
                current_cycle_num = len(weekly_lifecycle) + 1
                cycle_symbol = self.tickers[(current_cycle_num - 1) % len(self.tickers)] if self.tickers else "UNIVERSE"
                cycle_active_symbol = cycle_symbol

                if self.progress_callback:
                    self.progress_callback({
                        "event_type": "CYCLE_STARTING",
                        "completed_cycles": len(weekly_lifecycle),
                        "total_cycles": total_cycles,
                        "current_cycle": current_cycle_num,
                        "rebalance_date": rebalance_date_str,
                        "training_start": training_start_date,
                        "training_end": training_end_date,
                        "sub_phase": "CV_SPLITS_DISPATCHED",
                        "models_fitted": len(weekly_lifecycle) * 15,
                        "trades_processed": len(portfolio_trades),
                        "current_symbol": cycle_symbol,
                        "timestamp": datetime.now().isoformat()
                    })

                splits_data = list(tscv.split(X_train))

                if executor is not None:
                    # Parallel CV execution across persistent worker pool
                    X_bytes = X_train.tobytes()
                    y_bytes = y_train.tobytes()
                    x_shape = X_train.shape
                    x_dtype = str(X_train.dtype)

                    future_to_idx = {
                        executor.submit(
                            _run_cv_split_worker,
                            s_idx + 1,
                            X_bytes,
                            y_bytes,
                            x_shape,
                            x_dtype,
                            t_idx.tolist(),
                            v_idx.tolist(),
                            hp,
                            current_cycle_num
                        ): s_idx + 1
                        for s_idx, (t_idx, v_idx) in enumerate(splits_data)
                    }

                    for future in as_completed(future_to_idx):
                        s_idx = future_to_idx[future]
                        try:
                            split_res = future.result()
                            if split_res.get("status") == "SUCCESS":
                                oof_y_val.extend(split_res.get("oof_y_val", []))
                                oof_preds_proba.extend(split_res.get("oof_preds_proba", []))

                            if self.progress_callback:
                                self.progress_callback({
                                    "event_type": "CV_SPLIT_COMPLETED",
                                    "completed_cycles": len(weekly_lifecycle),
                                    "total_cycles": total_cycles,
                                    "current_cycle": current_cycle_num,
                                    "split_idx": s_idx,
                                    "worker_pid": split_res.get("pid", os.getpid()),
                                    "split_runtime": split_res.get("runtime_seconds", 0.0),
                                    "models_fitted": len(weekly_lifecycle) * 15 + (s_idx * 3),
                                    "trades_processed": len(portfolio_trades),
                                    "current_symbol": cycle_symbol,
                                    "timestamp": datetime.now().isoformat()
                                })
                        except Exception as e:
                            logger.error(f"Error collecting CV split {s_idx}: {e}")
                else:
                    # Sequential CV execution
                    for s_idx, (t_idx, v_idx) in enumerate(splits_data):
                        X_t, X_v = X_train[t_idx], X_train[v_idx]
                        y_t, y_v = y_train[t_idx], y_train[v_idx]
                        if len(np.unique(y_t)) < 2 or len(np.unique(y_v)) < 2:
                            continue

                        if self.model_factory is not None:
                            ens_split = self.model_factory()
                        elif self.model_type in ("LIGHTGBM", "LIGHTGBM_ALPHA"):
                            import lightgbm as lgb
                            ens_split = lgb.LGBMClassifier(n_estimators=hp.get('lgb_n_estimators', 80), learning_rate=hp.get('lgb_learning_rate', 0.05), num_leaves=hp.get('lgb_num_leaves', 31), max_depth=hp.get('lgb_max_depth', 5), random_state=42, verbose=-1, n_jobs=1)
                        else:
                            rf_split = RandomForestClassifier(n_estimators=hp.get('rf_n_estimators', 80), max_depth=hp.get('rf_max_depth', 5), min_samples_split=hp.get('rf_min_samples_split', 2), random_state=42)
                            gb_split = GradientBoostingClassifier(n_estimators=hp.get('gb_n_estimators', 80), learning_rate=hp.get('gb_learning_rate', 0.08), max_depth=hp.get('gb_max_depth', 3), random_state=42)
                            svm_split = make_pipeline(StandardScaler(), SVC(C=hp.get('svm_c', 1.0), probability=True, random_state=42))
                            ens_split = VotingClassifier(estimators=[('rf', rf_split), ('gb', gb_split), ('svm', svm_split)], voting='soft')

                        ens_split.fit(X_t, y_t)

                        probs_split = ens_split.predict_proba(X_v)[:, 1]
                        oof_y_val.extend(y_v)
                        oof_preds_proba.extend(probs_split)

                # Fit Final Challenger Ensemble on entire multi-stock training slice
                if self.model_factory is not None:
                    challenger_model = self.model_factory()
                elif self.model_type in ("LIGHTGBM", "LIGHTGBM_ALPHA"):
                    import lightgbm as lgb
                    challenger_model = lgb.LGBMClassifier(n_estimators=hp.get('lgb_n_estimators', 80), learning_rate=hp.get('lgb_learning_rate', 0.05), num_leaves=hp.get('lgb_num_leaves', 31), max_depth=hp.get('lgb_max_depth', 5), random_state=42, verbose=-1, n_jobs=1)
                else:
                    rf_full = RandomForestClassifier(n_estimators=hp.get('rf_n_estimators', 80), max_depth=hp.get('rf_max_depth', 5), min_samples_split=hp.get('rf_min_samples_split', 2), random_state=42)
                    gb_full = GradientBoostingClassifier(n_estimators=hp.get('gb_n_estimators', 80), learning_rate=hp.get('gb_learning_rate', 0.08), max_depth=hp.get('gb_max_depth', 3), random_state=42)
                    svm_full = make_pipeline(StandardScaler(), SVC(C=hp.get('svm_c', 1.0), probability=True, random_state=42))
                    challenger_model = VotingClassifier(estimators=[('rf', rf_full), ('gb', gb_full), ('svm', svm_full)], voting='soft')

                challenger_model.fit(X_train, y_train)

                # Champion vs Challenger Safety Gate
                challenger_f1 = float(f1_score(oof_y_val, (np.array(oof_preds_proba) >= 0.50).astype(int), zero_division=0)) if oof_y_val else 0.0
                gate_passed = (challenger_f1 >= (champion_f1 - 0.01)) and (challenger_f1 > 0.50)

                if active_champion is None or gate_passed:
                    active_champion = challenger_model
                    champion_f1 = challenger_f1
                    decision = "PROMOTED"
                    try:
                        clean_v = champion_version.replace("v", "").replace("-portfolio", "")
                        champion_version = f"v{round(float(clean_v) + 0.1, 1)}-portfolio"
                    except Exception:
                        champion_version = "v2.0-portfolio"
                else:
                    decision = "RETAINED"

                weekly_lifecycle.append({
                    "cycle_idx": len(weekly_lifecycle) + 1,
                    "rebalance_date": rebalance_date_str,
                    "train_samples": len(combined_train),
                    "stocks_included": len(train_sub_dfs),
                    "challenger_f1": round(challenger_f1, 4),
                    "champion_f1": round(champion_f1, 4),
                    "decision": decision,
                    "active_version": champion_version
                })

                # Platt Sigmoid Calibrator Fitting
                calibrator_lr = None
                if len(oof_preds_proba) >= 30 and len(np.unique(oof_y_val)) >= 2:
                    try:
                        calibrator_lr = LogisticRegression(C=1.0, random_state=42)
                        calibrator_lr.fit(np.array(oof_preds_proba).reshape(-1, 1), np.array(oof_y_val))
                    except Exception:
                        calibrator_lr = None

                # C. Step forward through the OOS week bar-by-bar across all universe stocks
                week_dates = all_dates[rebalance_idx:min(total_dates, rebalance_idx + bars_per_week)]
                cycle_trades_opened = 0
                cycle_trades_closed = 0
                cycle_candidates_evaluated = 0
                cycle_candidates_accepted = 0
                cycle_candidates_rejected = 0

                for bar_dt in week_dates:
                    bar_date_str = str(bar_dt)[:10]
                    year_str = bar_date_str[:4]
                    if year_str not in yearly_perf:
                        yearly_perf[year_str] = {"trades": 0, "wins": 0, "pnl": 0.0}

                    # 1. Manage Open Positions
                    closed_tickers = []
                    total_open_equity = 0.0

                    for t, pos in open_positions.items():
                        if t not in stock_dfs or bar_dt not in stock_dfs[t].index:
                            continue

                        bar_row = stock_dfs[t].loc[bar_dt]
                        cur_close = float(bar_row['close'])
                        low_p = float(bar_row['low'])
                        high_p = float(bar_row['high'])

                        pos_val = pos['qty'] * cur_close
                        total_open_equity += pos_val

                        exit_occurred = False
                        exit_price = cur_close
                        status = "OPEN"

                        if low_p <= pos['sl_price']:
                            exit_price = pos['sl_price']
                            status = "SL HIT"
                            exit_occurred = True
                        elif high_p >= pos['tp_price']:
                            exit_price = pos['tp_price']
                            status = "TARGET MET"
                            exit_occurred = True

                        if exit_occurred:
                            gross_pnl = pos['qty'] * (exit_price - pos['entry_price'])
                            turnover = (pos['qty'] * pos['entry_price']) + (pos['qty'] * exit_price)
                            friction = calculate_indian_trade_friction(turnover, is_intraday=False, flat_brokerage=self.brokerage, slippage_pct=self.slippage_pct)
                            net_pnl = gross_pnl - friction

                            cash += (pos['qty'] * exit_price) - friction
                            is_win = bool(net_pnl > 0)

                            performance_by_regime[pos['regime']]["trades"] += 1
                            performance_by_regime[pos['regime']]["pnl"] += net_pnl
                            if is_win: performance_by_regime[pos['regime']]["wins"] += 1

                            yearly_perf[year_str]["trades"] += 1
                            yearly_perf[year_str]["pnl"] += net_pnl
                            if is_win: yearly_perf[year_str]["wins"] += 1

                            is_locked_holdout = bool(rebalance_idx >= holdout_start_idx)

                            portfolio_trades.append({
                                "entry_date": pos['entry_date'],
                                "exit_date": bar_date_str,
                                "ticker": t,
                                "entry_price": round(pos['entry_price'], 2),
                                "exit_price": round(exit_price, 2),
                                "qty": pos['qty'],
                                "gross_pnl": round(gross_pnl, 2),
                                "friction_cost": round(friction, 2),
                                "pnl": round(net_pnl, 2),
                                "status": status,
                                "regime": pos['regime'],
                                "champion_version": champion_version,
                                "is_locked_holdout": is_locked_holdout
                            })
                            closed_tickers.append(t)
                            cycle_trades_closed += 1

                    for t in closed_tickers:
                        del open_positions[t]

                    # Current Total Portfolio Equity
                    total_portfolio_equity = cash + total_open_equity
                    equity_curve.append({
                        "date": bar_date_str,
                        "equity": round(total_portfolio_equity, 2),
                        "cash": round(cash, 2),
                        "open_positions": len(open_positions)
                    })

                    # 2. Candidate Discovery & Cross-Sectional Ranking
                    current_portfolio_heat = sum(p['risk_pct'] for p in open_positions.values())
                    remaining_heat_budget = max(0.0, self.max_portfolio_heat - current_portfolio_heat)

                    if remaining_heat_budget >= self.max_single_risk_pct:
                        candidates = []

                        for t, df_stock in stock_dfs.items():
                            if t in open_positions or bar_dt not in df_stock.index:
                                continue

                            cycle_candidates_evaluated += 1
                            row = df_stock.loc[bar_dt]
                            X_bar = row[features].values.reshape(1, -1)
                            raw_prob = float(active_champion.predict_proba(X_bar)[0][1])

                            tech_bonus = 0.0
                            if float(row['rsi']) < 40 and float(row['macd_diff']) > 0:
                                tech_bonus += 0.08
                            if float(row['adx']) > 25:
                                tech_bonus += 0.05

                            is_macro = bool(row['macro_bullish'])
                            meta_adjusted_prob = raw_prob + tech_bonus - (0.0 if is_macro else 0.08)

                            if calibrator_lr is not None:
                                try:
                                    calibrated_prob = float(calibrator_lr.predict_proba([[meta_adjusted_prob]])[0][1])
                                except Exception:
                                    calibrated_prob = meta_adjusted_prob
                            else:
                                calibrated_prob = meta_adjusted_prob

                            if calibrated_prob >= 0.50:
                                candidates.append({
                                    "ticker": t,
                                    "calibrated_prob": calibrated_prob,
                                    "macro_regime": "BULLISH" if is_macro else "BEARISH",
                                    "close": float(row['close']),
                                    "atr": float(row['atr'])
                                })
                            else:
                                cycle_candidates_rejected += 1

                        # Cross-sectional ranking by calibrated probability
                        candidates = sorted(candidates, key=lambda c: c['calibrated_prob'], reverse=True)

                        for cand in candidates:
                            if remaining_heat_budget < self.max_single_risk_pct:
                                cycle_candidates_rejected += 1
                                break

                            entry_p = cand['close']
                            atr_val = cand['atr']
                            sl_p = entry_p - (2.0 * atr_val)
                            tp_p = entry_p + (4.0 * atr_val)

                            kelly_res = calculate_kelly_position_size(
                                capital=total_portfolio_equity,
                                entry=entry_p,
                                sl=sl_p,
                                tp1=tp_p,
                                win_prob=cand['calibrated_prob'] * 100.0,
                                kelly_mode=self.kelly_mode,
                                max_risk_cap_pct=self.max_single_risk_pct
                            )

                            qty_to_buy = int(kelly_res.get("quantity", 0))
                            cost_to_buy = qty_to_buy * entry_p

                            if qty_to_buy > 0 and cash >= cost_to_buy:
                                risk_pct = float(kelly_res.get("risk_amount", 0.0)) / total_portfolio_equity * 100.0
                                cash -= cost_to_buy
                                remaining_heat_budget -= risk_pct

                                open_positions[cand['ticker']] = {
                                    "entry_date": bar_date_str,
                                    "entry_price": entry_p,
                                    "sl_price": sl_p,
                                    "tp_price": tp_p,
                                    "qty": qty_to_buy,
                                    "risk_pct": risk_pct,
                                    "regime": cand['macro_regime'],
                                    "prob": cand['calibrated_prob']
                                }
                                cycle_active_symbol = cand['ticker']
                                cycle_trades_opened += 1
                                cycle_candidates_accepted += 1
                            else:
                                cycle_candidates_rejected += 1

                # Emit CYCLE_COMPLETED telemetry at end of each weekly cycle
                if self.progress_callback:
                    completed_cycles = len(weekly_lifecycle)
                    progress_pct = round((completed_cycles / total_cycles) * 100.0, 1) if total_cycles > 0 else 50.0
                    promotions = len([w for w in weekly_lifecycle if w.get("decision") == "PROMOTED"])
                    retentions = len([w for w in weekly_lifecycle if w.get("decision") == "RETAINED"])

                    elapsed_so_far = time.time() - start_simulation_time
                    cycle_duration = time.time() - cycle_start_time

                    eq_vals = [e['equity'] for e in equity_curve] if equity_curve else [self.initial_capital]
                    peak_eq = max(eq_vals)
                    cur_eq = eq_vals[-1]
                    cur_dd_pct = round(((peak_eq - cur_eq) / peak_eq) * 100.0, 2) if peak_eq > 0 else 0.0

                    cum_gross_pnl = sum(t.get('gross_pnl', 0.0) for t in portfolio_trades)
                    cum_net_pnl = sum(t.get('pnl', 0.0) for t in portfolio_trades)
                    cum_friction = sum(t.get('friction_cost', 0.0) for t in portfolio_trades)

                    self.progress_callback({
                        "event_type": "CYCLE_COMPLETED",
                        "completed_cycles": completed_cycles,
                        "total_cycles": total_cycles,
                        "current_cycle": completed_cycles,
                        "progress_percent": min(95.0, progress_pct),
                        "rebalance_date": rebalance_date_str,
                        "training_start": training_start_date,
                        "training_end": training_end_date,
                        "current_symbol": cycle_active_symbol,
                        "trades_processed": len(portfolio_trades),
                        "trades_opened": cycle_trades_opened,
                        "trades_closed": cycle_trades_closed,
                        "candidates_evaluated": cycle_candidates_evaluated,
                        "candidates_accepted": cycle_candidates_accepted,
                        "candidates_rejected": cycle_candidates_rejected,
                        "current_open_positions": len(open_positions),
                        "current_cash": round(cash, 2),
                        "current_equity": round(cur_eq, 2),
                        "peak_equity": round(peak_eq, 2),
                        "current_drawdown_pct": cur_dd_pct,
                        "cumulative_gross_pnl": round(cum_gross_pnl, 2),
                        "cumulative_net_pnl": round(cum_net_pnl, 2),
                        "cumulative_friction": round(cum_friction, 2),
                        "models_fitted": completed_cycles * 15,
                        "promotions": promotions,
                        "retentions": retentions,
                        "cycle_duration_seconds": round(cycle_duration, 2),
                        "elapsed_seconds": round(elapsed_so_far, 1),
                        "timestamp": datetime.now().isoformat()
                    })

            # Final Open Position Square Off
            had_open_positions = bool(open_positions)
            for t, pos in list(open_positions.items()):
                final_close = float(stock_dfs[t].iloc[-1]['close'])
                gross_pnl = pos['qty'] * (final_close - pos['entry_price'])
                turnover = (pos['qty'] * pos['entry_price']) + (pos['qty'] * final_close)
                friction = calculate_indian_trade_friction(turnover, is_intraday=False, flat_brokerage=self.brokerage, slippage_pct=self.slippage_pct)
                net_pnl = gross_pnl - friction
                cash += (pos['qty'] * final_close) - friction

                portfolio_trades.append({
                    "entry_date": pos['entry_date'],
                    "exit_date": str(all_dates[-1])[:10],
                    "ticker": t,
                    "entry_price": round(pos['entry_price'], 2),
                    "exit_price": round(final_close, 2),
                    "qty": pos['qty'],
                    "gross_pnl": round(gross_pnl, 2),
                    "friction_cost": round(friction, 2),
                    "pnl": round(net_pnl, 2),
                    "status": "SQUARED OFF (END)",
                    "regime": pos['regime'],
                    "champion_version": champion_version,
                    "is_locked_holdout": True
                })

            if had_open_positions:
                equity_curve.append({
                    "date": str(all_dates[-1])[:10],
                    "equity": round(cash, 2),
                    "cash": round(cash, 2),
                    "open_positions": 0
                })

            # Calculate Advanced Institutional Metrics
            metrics = calculate_advanced_metrics(portfolio_trades, equity_curve, initial_capital=self.initial_capital)
            monte_carlo = run_monte_carlo_simulation(portfolio_trades, initial_capital=self.initial_capital, n_simulations=1000, horizon_trades=50)

            holdout_trades = [t for t in portfolio_trades if t.get('is_locked_holdout', False)]
            holdout_metrics = {
                "total_trades": len(holdout_trades),
                "wins": len([t for t in holdout_trades if t['pnl'] > 0]),
                "win_rate_pct": round((len([t for t in holdout_trades if t['pnl'] > 0]) / len(holdout_trades) * 100.0), 1) if holdout_trades else 0.0,
                "net_pnl": round(sum(t['pnl'] for t in holdout_trades), 2),
                "holdout_samples": total_dates - holdout_start_idx
            }

            universe_info = get_universe(self.universe_name)
            results_payload = {
                "status": "success",
                "simulation_mode": "MULTI_STOCK_PORTFOLIO_WALK_FORWARD",
                "universe_name": universe_info["name"],
                "universe_size": len(self.tickers),
                "survivorship_bias_disclosure": universe_info["survivorship_bias"],
                "history_years": self.history_years,
                "start_date": self.start_date,
                "metrics": metrics,
                "monte_carlo": monte_carlo,
                "trades": portfolio_trades[::-1],
                "equity_curve": equity_curve,
                "champion_challenger_lifecycle": {
                    "total_weekly_cycles": len(weekly_lifecycle),
                    "promotions": len([w for w in weekly_lifecycle if w['decision'] == 'PROMOTED']),
                    "retentions": len([w for w in weekly_lifecycle if w['decision'] == 'RETAINED']),
                    "active_champion_version": champion_version,
                    "recent_cycles": weekly_lifecycle[-6:]
                },
                "locked_final_holdout": holdout_metrics,
                "performance_by_regime": {
                    "BULLISH_MACRO": {
                        "trades": performance_by_regime["BULLISH"]["trades"],
                        "win_rate_pct": round(performance_by_regime["BULLISH"]["wins"] / performance_by_regime["BULLISH"]["trades"] * 100, 1) if performance_by_regime["BULLISH"]["trades"] > 0 else 0.0,
                        "net_pnl": round(performance_by_regime["BULLISH"]["pnl"], 2)
                    },
                    "BEARISH_MACRO": {
                        "trades": performance_by_regime["BEARISH"]["trades"],
                        "win_rate_pct": round(performance_by_regime["BEARISH"]["wins"] / performance_by_regime["BEARISH"]["trades"] * 100, 1) if performance_by_regime["BEARISH"]["trades"] > 0 else 0.0,
                        "net_pnl": round(performance_by_regime["BEARISH"]["pnl"], 2)
                    }
                },
                "performance_by_year": {
                    y: {
                        "trades": data["trades"],
                        "win_rate_pct": round(data["wins"] / data["trades"] * 100, 1) if data["trades"] > 0 else 0.0,
                        "net_pnl": round(data["pnl"], 2)
                    } for y, data in sorted(yearly_perf.items()) if data["trades"] > 0
                },
                "portfolio_risk_parameters": {
                    "max_portfolio_heat_cap_pct": self.max_portfolio_heat,
                    "max_single_trade_risk_pct": self.max_single_risk_pct,
                    "kelly_fraction": self.kelly_mode,
                    "statutory_friction": "Indian Equities (STT, GST, Exchange, Brokerage ₹20, Slippage 8 bps)"
                }
            }
        finally:
            if pool_key is not None:
                from app.analytics.process_lifecycle_manager import ProcessLifecycleManager
                ProcessLifecycleManager.terminate_worker_pool(pool_key)
            elif executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)

        return results_payload

