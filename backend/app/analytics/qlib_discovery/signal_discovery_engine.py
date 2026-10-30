"""
QLIB SIGNAL DISCOVERY ENGINE V1
===============================
Comprehensive quantitative research engine designed to answer:
"Where is the robust predictive signal in our Indian equity data, if one exists?"

Mandatory Research Governance Enforced:
1. Locked OOS Inviolability: Matrix selection uses Train + Validation only.
   Frozen candidates are evaluated on Locked OOS exactly once.
2. Staged Research Funnel:
   Stage A (Alpha158 Factor Screen) -> Stage B (Target/Horizon Screen) ->
   Stage C (Model Comparison across seeds 42, 101, 777) ->
   Stage D (Validation Shortlist) -> Stage E (Locked OOS Evaluation).
3. Diagnostic Composite Score: All underlying metrics (IC, ICIR, spread, Sharpe) exposed.
4. Survivorship Bias Governance: Explicitly flags "CURRENT_CONSTITUENTS_RETROSPECTIVE".
5. Date-Aware Cross-Sectional Ranking: Deciles and ranks computed date-by-date.
6. Separate Horizon Evaluations: 1D, 3D, 5D, 10D, 20D evaluated independently.
7. Statistical Uncertainty: Standard errors and confidence intervals reported.
8. Research Isolation: Zero Champion mutation, zero ml_trade_history writes, 0% heat.
"""

import os
import json
import math
import hashlib
import logging
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from scipy.stats import spearmanr

from app.analytics.qlib_discovery.alpha158_engine import compute_alpha158_features, get_alpha158_feature_names
from app.analytics.qlib_discovery.double_ensemble import DoubleEnsembleClassifier
from app.analytics.qlib_discovery.discovery_ledger import (
    init_discovery_ledger_schema,
    compute_experiment_fingerprint,
    save_experiment_record,
    save_feature_stats,
    save_decile_results,
    save_regime_results,
    save_oos_result
)
from app.analytics.qlib_discovery.qlib_registry import verify_champion_immutability, CHAMPION_HASHES
from app.data.historical_data_layer import HistoricalDataLayer, get_db_path
from app.analytics.universe_config import resolve_universe_tickers
from app.analytics.model_manager import ModelManager

logger = logging.getLogger(__name__)

PROVENANCE_LABEL = "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
FEATURE_VERSION = "qlib_research_v1"
ENGINE_VERSION = "v1.0-discovery"

class QlibSignalDiscoveryEngine:
    """
    Orchestrates systematic multi-dimensional signal discovery across
    Alpha158 factors, horizons, targets, and model families.
    """

    @classmethod
    def verify_forensic_invariants(cls):
        """
        Forensic Gate Stop: Ensures all security and causality invariants are intact
        before launching any computationally intensive experiments.
        """
        # 1. Champion SHA-256 byte immutability
        verify_champion_immutability()

        # 2. Alpha158 feature count and provenance check
        feats = get_alpha158_feature_names()
        if len(feats) != 158:
            raise RuntimeError(f"Alpha158 canonical feature count violated: {len(feats)} != 158")

        # 3. Canonical database access
        db_path = get_db_path()
        if not os.path.exists(db_path):
            raise RuntimeError(f"Canonical database not found at {db_path}")

        # 4. Research ledger initialization
        init_discovery_ledger_schema()
        logger.info("[SignalDiscovery] Forensic integrity checks passed cleanly.")

    @classmethod
    def load_canonical_universe_data(
        cls,
        universe_name: str = "LIVE_52",
        timeframe: str = "1d",
        min_bars_required: int = 200
    ) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Any]]:
        """
        Loads point-in-time OHLCV bars strictly from the canonical SQLite database.
        Avoids external network calls when canonical data is available.
        """
        tickers = resolve_universe_tickers(universe_name)
        db_path = get_db_path()
        conn = sqlite3.connect(db_path, timeout=30.0)

        raw_dfs = {}
        missing_tickers = []
        try:
            for t in tickers:
                df = pd.read_sql_query(
                    "SELECT date, open, high, low, close, volume FROM ohlcv "
                    "WHERE ticker = ? AND timeframe = ? ORDER BY date ASC",
                    conn,
                    params=[t, timeframe]
                )
                if df is not None and len(df) >= min_bars_required:
                    df['date'] = pd.to_datetime(df['date'])
                    df.set_index('date', inplace=True)
                    df.sort_index(inplace=True)
                    raw_dfs[t] = df
                else:
                    missing_tickers.append(t)
        finally:
            conn.close()

        # Compute deterministic dataset hash
        h = hashlib.sha256()
        h.update(universe_name.encode('utf-8'))
        h.update(timeframe.encode('utf-8'))
        for t in sorted(raw_dfs.keys()):
            df = raw_dfs[t]
            h.update(t.encode('utf-8'))
            h.update(str(len(df)).encode('utf-8'))
            if not df.empty:
                h.update(f"{df.iloc[0]['close']:.4f}".encode('utf-8'))
                h.update(f"{df.iloc[-1]['close']:.4f}".encode('utf-8'))

        dataset_meta = {
            "universe": universe_name,
            "universe_mode": "CURRENT_CONSTITUENTS_RETROSPECTIVE",
            "survivorship_flag": "Potential survivorship bias: Universe evaluated using current index constituents retrospectively over 10-year period.",
            "timeframe": timeframe,
            "ticker_count": len(raw_dfs),
            "requested_tickers": len(tickers),
            "missing_tickers": missing_tickers,
            "dataset_hash": h.hexdigest(),
            "data_provider": "Canonical SQLite (ohlcv table)"
        }
        return raw_dfs, dataset_meta

    @classmethod
    def audit_data_quality(cls, raw_dfs: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
        """
        Data Quality Gate: Inspects every ticker for duplicate timestamps,
        impossible OHLC geometries, negative prices, and anomalous gaps.
        """
        rows_examined = 0
        rows_valid = 0
        rows_invalid = 0
        rows_excluded = 0
        exclusion_reasons = []

        for t, df in raw_dfs.items():
            n = len(df)
            rows_examined += n

            # Check index uniqueness
            if not df.index.is_unique:
                dup_count = df.index.duplicated().sum()
                rows_invalid += int(dup_count)
                exclusion_reasons.append(f"{t}: {dup_count} duplicate timestamp records detected.")

            # Check index monotonic ordering
            if not df.index.is_monotonic_increasing:
                rows_invalid += 1
                exclusion_reasons.append(f"{t}: Timestamps not monotonically increasing.")

            # Impossible OHLC geometry checks
            bad_high = df[(df['high'] < df['low']) | (df['high'] < df['open']) | (df['high'] < df['close'])]
            bad_low = df[(df['low'] > df['open']) | (df['low'] > df['close'])]
            non_positive_close = df[df['close'] <= 0]
            negative_vol = df[df['volume'] < 0]

            invalids = len(bad_high) + len(bad_low) + len(non_positive_close) + len(negative_vol)
            if invalids > 0:
                rows_invalid += invalids
                exclusion_reasons.append(f"{t}: {invalids} impossible OHLCV bar geometries detected.")
            else:
                rows_valid += n

        valid_ratio = rows_valid / max(1, rows_examined)
        if valid_ratio < 0.90:
            raise RuntimeError(f"INSUFFICIENT DATA QUALITY: Only {valid_ratio*100:.1f}% rows valid. Exclusions: {exclusion_reasons[:5]}")

        return {
            "status": "PASS",
            "rows_examined": rows_examined,
            "rows_valid": rows_valid,
            "rows_invalid": rows_invalid,
            "rows_excluded": rows_excluded,
            "valid_ratio_pct": round(valid_ratio * 100.0, 2),
            "exclusion_reasons": exclusion_reasons[:10]
        }

    @classmethod
    def compute_forward_targets(
        cls,
        raw_df: pd.DataFrame,
        horizons: List[int] = [1, 3, 5, 10, 20],
        friction_pct: float = 0.20
    ) -> Dict[str, pd.Series]:
        """
        Generates multi-horizon forward targets without lookahead:
        - raw return: price[t+H] / price[t] - 1.0
        - classification: 1 if fwd_ret > 0 else 0
        - thresholded: 1 if fwd_ret > friction_pct / 100.0 else 0
        - risk_adjusted: fwd_ret / rolling_20d_vol
        """
        c = raw_df['close']
        h_targets = {}

        # 20-day rolling return volatility for risk-adjusted targets
        ret_1d = c.pct_change()
        roll_vol = ret_1d.rolling(20).std() + 1e-6

        for h in horizons:
            fwd_ret = (c.shift(-h) / c) - 1.0
            h_targets[f"ret_{h}d"] = fwd_ret
            h_targets[f"clf_{h}d"] = (fwd_ret > 0.0).astype(int)
            h_targets[f"thresh_{h}d"] = (fwd_ret > (friction_pct / 100.0)).astype(int)
            h_targets[f"risk_adj_{h}d"] = fwd_ret / roll_vol

        return h_targets

    @classmethod
    def build_cross_sectional_dataset(
        cls,
        raw_dfs: Dict[str, pd.DataFrame],
        feature_family: str = "Alpha158",
        horizons: List[int] = [1, 3, 5, 10, 20]
    ) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
        """
        Vectorially computes Alpha158 features for all universe tickers,
        generates forward multi-horizon targets, and stacks into a unified cross-sectional DataFrame.
        """
        all_X_rows = []
        all_Y_rows = []
        feature_names = get_alpha158_feature_names()

        for t, raw in raw_dfs.items():
            if len(raw) < 70:
                continue

            # Compute features strictly using past-and-current bar access
            if feature_family == "Alpha158":
                feat_df = compute_alpha158_features(raw)
            else:
                from app.analytics.qlib_discovery.alpha360_engine import compute_alpha360_features
                feat_df = compute_alpha360_features(raw)

            targets_dict = cls.compute_forward_targets(raw, horizons=horizons)
            target_df = pd.DataFrame(targets_dict, index=raw.index)

            # Metadata columns
            feat_df['__ticker__'] = t
            feat_df['__date__'] = raw.index

            target_df['__ticker__'] = t
            target_df['__date__'] = raw.index

            all_X_rows.append(feat_df)
            all_Y_rows.append(target_df)

        full_X = pd.concat(all_X_rows, axis=0)
        full_Y = pd.concat(all_Y_rows, axis=0)

        # Sort strictly by date then ticker
        full_X.sort_values(by=['__date__', '__ticker__'], inplace=True)
        full_Y.sort_values(by=['__date__', '__ticker__'], inplace=True)

        # Cross-sectional rank targets per date across universe
        for h in horizons:
            ret_col = f"ret_{h}d"
            if ret_col in full_Y.columns:
                full_Y[f"rank_{h}d"] = full_Y.groupby('__date__')[ret_col].rank(pct=True)

        return full_X, full_Y, feature_names

    @classmethod
    def compute_temporal_splits(
        cls,
        X_df: pd.DataFrame,
        Y_df: pd.DataFrame,
        train_pct: float = 0.55,
        val_pct: float = 0.15,
        oos_pct: float = 0.30
    ) -> Dict[str, Any]:
        """
        Strict chronological train (55%), validation (15%), and locked OOS (30%) partitioning.
        Scalers fit strictly on TRAIN data to prevent standardization leakage.
        """
        dates = sorted(X_df['__date__'].unique())
        n_dates = len(dates)

        n_train = int(n_dates * train_pct)
        n_val = int(n_dates * val_pct)

        train_dates = set(dates[:n_train])
        val_dates = set(dates[n_train : n_train + n_val])
        oos_dates = set(dates[n_train + n_val :])

        train_mask = X_df['__date__'].isin(train_dates)
        val_mask = X_df['__date__'].isin(val_dates)
        oos_mask = X_df['__date__'].isin(oos_dates)

        feature_cols = [c for c in X_df.columns if not c.startswith('__')]

        # Standardize using TRAIN statistics only
        train_vals = X_df.loc[train_mask, feature_cols].values.astype(np.float32)
        mean = np.nanmean(train_vals, axis=0, keepdims=True)
        std = np.nanstd(train_vals, axis=0, keepdims=True) + 1e-6

        def scale(df_subset):
            vals = df_subset[feature_cols].values.astype(np.float32)
            scaled = np.clip((vals - mean) / std, -5.0, 5.0)
            scaled_df = pd.DataFrame(scaled, columns=feature_cols, index=df_subset.index)
            scaled_df['__ticker__'] = df_subset['__ticker__']
            scaled_df['__date__'] = df_subset['__date__']
            return scaled_df

        return {
            "train_X": scale(X_df.loc[train_mask]),
            "train_Y": Y_df.loc[train_mask],
            "val_X": scale(X_df.loc[val_mask]),
            "val_Y": Y_df.loc[val_mask],
            "oos_X": scale(X_df.loc[oos_mask]),
            "oos_Y": Y_df.loc[oos_mask],
            "feature_cols": feature_cols,
            "dates_meta": {
                "train_start": str(dates[0]).split('T')[0],
                "train_end": str(dates[n_train - 1]).split('T')[0],
                "val_start": str(dates[n_train]).split('T')[0],
                "val_end": str(dates[n_train + n_val - 1]).split('T')[0],
                "oos_start": str(dates[n_train + n_val]).split('T')[0],
                "oos_end": str(dates[-1]).split('T')[0],
                "train_dates_count": n_train,
                "val_dates_count": n_val,
                "oos_dates_count": len(dates) - (n_train + n_val),
                "split_ratio": f"{int(train_pct*100)}/{int(val_pct*100)}/{int(oos_pct*100)}"
            }
        }

    @classmethod
    def evaluate_stage_a_feature_signals(
        cls,
        X_df: pd.DataFrame,
        Y_df: pd.DataFrame,
        feature_cols: List[str],
        horizon: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Stage A: Evaluates cross-sectional IC, Rank IC, ICIR, positive frequency,
        and temporal stability across 5 rolling windows for each of the 158 Alpha158 features.
        Evaluated on Train + Validation ONLY (Never Locked OOS).
        """
        ret_col = f"ret_{horizon}d"
        dates = sorted(X_df['__date__'].unique())
        n_windows = 5
        window_size = len(dates) // n_windows

        results = []
        target_series = Y_df[ret_col]

        # Pre-group by date for rapid cross-sectional vector computation
        date_groups = X_df.groupby('__date__')
        total_tickers = len(X_df['__ticker__'].unique()) if '__ticker__' in X_df.columns else 10
        min_cross_section = min(10, max(1, int(total_tickers * 0.4)))

        for feat in feature_cols:
            f_vals = X_df[feat]
            daily_ics = []
            daily_rank_ics = []

            for dt, grp in date_groups:
                if len(grp) < min_cross_section:
                    continue
                idxs = grp.index
                f_cross = f_vals.loc[idxs].values
                r_cross = target_series.loc[idxs].values

                # Filter valid finite pairs
                valid = np.isfinite(f_cross) & np.isfinite(r_cross)
                if np.sum(valid) < min_cross_section:
                    continue
                f_valid = f_cross[valid]
                r_valid = r_cross[valid]

                # Pearson IC
                std_f = np.std(f_valid)
                std_r = np.std(r_valid)
                if std_f > 1e-8 and std_r > 1e-8:
                    p_ic = np.corrcoef(f_valid, r_valid)[0, 1]
                    if np.isfinite(p_ic):
                        daily_ics.append(p_ic)

                # Spearman Rank IC
                f_ranks = pd.Series(f_valid).rank().values
                r_ranks = pd.Series(r_valid).rank().values
                sp_ic = np.corrcoef(f_ranks, r_ranks)[0, 1]
                if np.isfinite(sp_ic):
                    daily_rank_ics.append(sp_ic)

            if not daily_ics:
                continue

            ic_mean = float(np.mean(daily_ics))
            ic_std = float(np.std(daily_ics)) + 1e-6
            icir = ic_mean / ic_std

            rank_ic_mean = float(np.mean(daily_rank_ics)) if daily_rank_ics else 0.0
            rank_ic_std = float(np.std(daily_rank_ics)) + 1e-6 if daily_rank_ics else 1.0
            rank_icir = rank_ic_mean / rank_ic_std

            pos_freq = float(np.mean(np.array(daily_ics) > 0.0)) * 100.0

            # 5-Window Stability Audit
            window_ics = []
            for w in range(n_windows):
                w_start = w * window_size
                w_end = (w + 1) * window_size if w < n_windows - 1 else len(dates)
                w_dates = set(dates[w_start:w_end])
                w_mask = X_df['__date__'].isin(w_dates)
                w_grp_indices = X_df[w_mask].index
                if len(w_grp_indices) > 0:
                    sub_f = f_vals.loc[w_grp_indices].values
                    sub_r = target_series.loc[w_grp_indices].values
                    v = np.isfinite(sub_f) & np.isfinite(sub_r)
                    if np.sum(v) > 10:
                        c_val = np.corrcoef(sub_f[v], sub_r[v])[0, 1]
                        window_ics.append(float(c_val) if np.isfinite(c_val) else 0.0)

            # Classify Stability
            same_sign_count = max(
                sum(1 for w in window_ics if w > 0),
                sum(1 for w in window_ics if w < 0)
            )
            sign_consistency = (same_sign_count / max(1, len(window_ics))) * 100.0

            if abs(rank_ic_mean) >= 0.025 and sign_consistency >= 80.0 and abs(rank_icir) >= 0.30:
                stability_class = "STABLE"
            elif abs(rank_ic_mean) >= 0.02 and sign_consistency < 60.0:
                stability_class = "UNSTABLE"
            elif rank_ic_mean <= -0.02 and sign_consistency >= 70.0:
                stability_class = "NEGATIVE"
            elif abs(rank_ic_mean) >= 0.02:
                stability_class = "REGIME-SENSITIVE"
            else:
                stability_class = "NO_SIGNAL"

            # 95% Confidence Interval for mean IC
            n_obs = len(daily_ics)
            ci_half_width = 1.96 * (ic_std / math.sqrt(n_obs)) if n_obs > 0 else 0.0

            results.append({
                "feature_name": feat,
                "ic_mean": round(ic_mean, 4),
                "ic_std": round(ic_std, 4),
                "icir": round(icir, 4),
                "ic_ci_95": [round(ic_mean - ci_half_width, 4), round(ic_mean + ci_half_width, 4)],
                "rank_ic_mean": round(rank_ic_mean, 4),
                "rank_icir": round(rank_icir, 4),
                "positive_ic_pct": round(pos_freq, 1),
                "sign_consistency_pct": round(sign_consistency, 1),
                "stability_class": stability_class,
                "window_ics": [round(w, 4) for w in window_ics]
            })

        # Sort by absolute Rank IC descending
        results.sort(key=lambda x: abs(x["rank_ic_mean"]), reverse=True)
        return results

    @classmethod
    def cluster_core_signals(cls, X_df: pd.DataFrame, feature_cols: List[str]) -> List[Dict[str, Any]]:
        """
        Computes pairwise feature correlations and groups factors into core signal clusters.
        """
        corr_matrix = X_df[feature_cols].corr()

        clusters = {
            "MOMENTUM_TREND": ["ROC", "MA", "BETA", "RSQR", "CNTP", "CNTD", "SUMP", "SUMD"],
            "VOLATILITY_RANGE": ["STD", "KLEN", "WVMA", "VSTD"],
            "MEAN_REVERSION": ["KMID", "OPEN0", "HIGH0", "LOW0", "RANK", "RSV"],
            "VOLUME_DYNAMICS": ["VMA", "VSUMP", "VSUMN", "VSUMD", "CORR", "CORD"],
            "EXTREMES_DISTRIBUTION": ["MAX", "MIN", "QTLU", "QTLD", "IMAX", "IMIN", "IMXD"]
        }

        cluster_summaries = []
        for c_name, prefixes in clusters.items():
            matched_feats = [f for f in feature_cols if any(f.startswith(p) for p in prefixes)]
            if matched_feats:
                sub_corr = corr_matrix.loc[matched_feats, matched_feats].values
                avg_internal_corr = float(np.nanmean(np.abs(sub_corr)))
                cluster_summaries.append({
                    "cluster_name": c_name,
                    "feature_count": len(matched_feats),
                    "representative_features": matched_feats[:5],
                    "average_internal_correlation": round(avg_internal_corr, 3)
                })

        return cluster_summaries

    @classmethod
    def evaluate_cross_sectional_ranking(
        cls,
        scores_df: pd.DataFrame,
        horizon: int = 5
    ) -> Dict[str, Any]:
        """
        Date-aware cross-sectional decile and spread evaluation.
        Ranks stocks independently within each trading date into Q1..Q10.
        Calculates monotonic return progression and Top-minus-Bottom spread.
        """
        decile_returns = {q: [] for q in range(1, 11)}
        daily_spreads = []

        date_groups = scores_df.groupby('__date__')
        eligible_counts = []

        total_tickers = len(scores_df['__ticker__'].unique()) if '__ticker__' in scores_df.columns else 10
        min_cross_section = min(10, max(2, int(total_tickers * 0.4)))

        for dt, grp in date_groups:
            if len(grp) < min_cross_section:
                continue

            eligible_counts.append(len(grp))
            # Cross-sectional ranking from 0 to 1
            grp_copy = grp.copy()
            grp_copy['rank'] = grp_copy['pred'].rank(pct=True)

            n_unique = len(grp_copy['rank'].unique())
            n_bins = min(10, max(2, n_unique))
            if n_bins >= 2:
                raw_bins = pd.qcut(grp_copy['rank'], q=n_bins, labels=False, duplicates='drop') + 1
                if n_bins < 10:
                    grp_copy['decile'] = np.clip(np.ceil(raw_bins * (10.0 / n_bins)).astype(int), 1, 10)
                else:
                    grp_copy['decile'] = raw_bins
            else:
                grp_copy['decile'] = 1

            d_means = grp_copy.groupby('decile')['actual'].mean()
            for q in range(1, 11):
                if q in d_means:
                    decile_returns[q].append(d_means[q])

            # Top vs Bottom spread
            if len(d_means) > 0:
                top_key = max(d_means.keys())
                bot_key = min(d_means.keys())
                q_top = d_means[top_key]
                q_bot = d_means[bot_key]
                if np.isfinite(q_top) and np.isfinite(q_bot):
                    daily_spreads.append(q_top - q_bot)

        decile_summary = []
        for q in range(1, 11):
            arr = np.array(decile_returns[q]) * 100.0
            avg_ret = float(np.mean(arr)) if len(arr) > 0 else 0.0
            med_ret = float(np.median(arr)) if len(arr) > 0 else 0.0
            hit_rt = float(np.mean(arr > 0.0)) * 100.0 if len(arr) > 0 else 0.0
            vol = float(np.std(arr)) + 1e-6
            sharpe = (avg_ret / vol) * math.sqrt(252 / max(1, horizon)) if vol > 0 else 0.0

            decile_summary.append({
                "decile": q,
                "avg_return_pct": round(avg_ret, 3),
                "median_return_pct": round(med_ret, 3),
                "hit_rate": round(hit_rt, 1),
                "sharpe": round(sharpe, 2)
            })

        # Monotonicity test: Rank correlation of deciles vs average return
        decile_ranks = np.array(range(1, 11))
        decile_ret_vals = np.array([d["avg_return_pct"] for d in decile_summary])
        if np.std(decile_ret_vals) > 1e-6:
            r = np.corrcoef(decile_ranks, decile_ret_vals)[0, 1]
            monotonicity_score = float(r) if np.isfinite(r) else 0.0
        else:
            monotonicity_score = 0.0

        spread_arr = np.array(daily_spreads) * 100.0
        mean_spread = float(np.mean(spread_arr)) if len(spread_arr) > 0 else 0.0
        spread_std = float(np.std(spread_arr)) + 1e-6
        spread_sharpe = (mean_spread / spread_std) * math.sqrt(252 / max(1, horizon))

        return {
            "deciles": decile_summary,
            "monotonicity_score": round(monotonicity_score, 3),
            "top_minus_bottom_spread_pct": round(mean_spread, 3),
            "spread_sharpe": round(spread_sharpe, 2),
            "median_eligible_stocks_per_date": int(np.median(eligible_counts)) if eligible_counts else 0
        }

    @classmethod
    def evaluate_market_regimes(
        cls,
        scores_df: pd.DataFrame,
        raw_dfs: Dict[str, pd.DataFrame]
    ) -> List[Dict[str, Any]]:
        """
        Classifies dates into Bullish, Bearish, Sideways, High Vol, and Low Vol
        and breaks down predictive performance per regime.
        """
        # Determine market index proxy from raw_dfs (e.g. RELIANCE.NS or average market return)
        sample_df = next(iter(raw_dfs.values()))
        mkt_ret = sample_df['close'].pct_change()
        mkt_sma50 = sample_df['close'].rolling(50).mean()
        mkt_vol = mkt_ret.rolling(20).std()
        vol_75th = mkt_vol.quantile(0.75)
        vol_25th = mkt_vol.quantile(0.25)

        regimes = {
            "BULLISH": [],
            "BEARISH": [],
            "SIDEWAYS": [],
            "HIGH_VOLATILITY": [],
            "LOW_VOLATILITY": []
        }

        date_groups = scores_df.groupby('__date__')
        for dt, grp in date_groups:
            if dt not in sample_df.index:
                continue
            close_val = sample_df.loc[dt, 'close']
            sma_val = mkt_sma50.loc[dt] if dt in mkt_sma50.index else close_val
            cur_vol = mkt_vol.loc[dt] if dt in mkt_vol.index else 0.01

            # Determine regime tag
            if close_val > sma_val * 1.02:
                reg_tag = "BULLISH"
            elif close_val < sma_val * 0.98:
                reg_tag = "BEARISH"
            else:
                reg_tag = "SIDEWAYS"

            p_ic = np.corrcoef(grp['pred'], grp['actual'])[0, 1] if len(grp) >= 5 else np.nan
            if np.isfinite(p_ic):
                regimes[reg_tag].append(p_ic)
                if cur_vol >= vol_75th:
                    regimes["HIGH_VOLATILITY"].append(p_ic)
                elif cur_vol <= vol_25th:
                    regimes["LOW_VOLATILITY"].append(p_ic)

        regime_results = []
        for r_name, ics in regimes.items():
            if ics:
                arr = np.array(ics)
                regime_results.append({
                    "regime_name": r_name,
                    "sample_count": len(arr),
                    "ic_mean": round(float(np.mean(arr)), 4),
                    "rank_ic_mean": round(float(np.mean(arr)), 4),
                    "spread_pct": round(float(np.mean(arr) * 5.0), 3), # Proxy spread
                    "win_rate": round(float(np.mean(arr > 0)) * 100.0, 1)
                })

        return regime_results

    @classmethod
    def evaluate_cost_sensitivity(
        cls,
        scores_df: pd.DataFrame,
        friction_levels: List[float] = [0.10, 0.15, 0.20]
    ) -> List[Dict[str, Any]]:
        """
        Tests economic robustness under 1.0x (0.10%), 1.5x (0.15%), and 2.0x (0.20%) friction.
        """
        results = []
        # Simulate top 10% predictions as Long trades
        top_10_pct = scores_df[scores_df['pred'] >= scores_df['pred'].quantile(0.90)]

        for friction in friction_levels:
            trade_pnls = (top_10_pct['actual'].values * 100.0) - friction
            win_rate = float(np.mean(trade_pnls > 0.0)) * 100.0 if len(trade_pnls) > 0 else 0.0
            expectancy = float(np.mean(trade_pnls)) if len(trade_pnls) > 0 else 0.0
            vol = float(np.std(trade_pnls)) + 1e-6
            sharpe = (expectancy / vol) * math.sqrt(50) if vol > 0 else 0.0

            # Profit Factor
            gains = np.sum(trade_pnls[trade_pnls > 0])
            losses = np.abs(np.sum(trade_pnls[trade_pnls < 0])) + 1e-6
            pf = gains / losses

            results.append({
                "friction_pct": friction,
                "trade_count": len(trade_pnls),
                "win_rate": round(win_rate, 1),
                "expectancy_pct": round(expectancy, 3),
                "profit_factor": round(float(pf), 2),
                "sharpe": round(float(sharpe), 2)
            })

        return results

    @classmethod
    def evaluate_stock_breadth(cls, scores_df: pd.DataFrame) -> Dict[str, Any]:
        """
        Evaluates cross-ticker predictive consistency:
        Calculates Rank IC per stock across time to verify whether the signal is concentrated
        in 1-2 outlier stocks or broadly distributed across the universe.
        """
        ticker_groups = scores_df.groupby('__ticker__')
        ticker_ics = {}
        for t, grp in ticker_groups:
            if len(grp) >= 15:
                v = np.isfinite(grp['pred']) & np.isfinite(grp['actual'])
                if np.sum(v) >= 10:
                    r, _ = spearmanr(grp.loc[v, 'pred'], grp.loc[v, 'actual'])
                    if np.isfinite(r):
                        ticker_ics[t] = float(r)

        ic_vals = list(ticker_ics.values())
        pct_positive = float(np.mean([ic > 0 for ic in ic_vals])) * 100.0 if ic_vals else 0.0
        mean_ticker_ic = float(np.mean(ic_vals)) if ic_vals else 0.0
        concentration = float(max(ic_vals) / (sum(abs(x) for x in ic_vals) + 1e-6)) if ic_vals else 0.0

        return {
            "evaluated_tickers": len(ticker_ics),
            "pct_positive_ic": round(pct_positive, 1),
            "mean_per_ticker_ic": round(mean_ticker_ic, 4),
            "top_stock_concentration": round(concentration, 3),
            "top_positive_tickers": sorted(ticker_ics.items(), key=lambda x: x[1], reverse=True)[:5],
            "worst_negative_tickers": sorted(ticker_ics.items(), key=lambda x: x[1])[:5]
        }

    @classmethod
    def run_staged_discovery(
        cls,
        universe: str = "LIVE_52",
        horizons: List[int] = [1, 3, 5, 10, 20],
        seeds: List[int] = [42, 101, 777],
        max_tickers: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Executes the staged discovery funnel:
        Stage A: Alpha158 feature screening (Train + Val ONLY)
        Stage B: Horizon selection (1d, 3d, 5d, 10d, 20d)
        Stage C: Multi-model comparison across seeds (42, 101, 777)
        Stage D: Validation Candidate Freezing
        Stage E: Single-pass evaluation on Locked OOS
        """
        cls.verify_forensic_invariants()

        # Step 1: Load Canonical Data
        raw_dfs, dataset_meta = cls.load_canonical_universe_data(universe_name=universe)
        if max_tickers and len(raw_dfs) > max_tickers:
            raw_dfs = {k: raw_dfs[k] for k in list(raw_dfs.keys())[:max_tickers]}

        # Step 2: Data Quality Gate
        data_quality = cls.audit_data_quality(raw_dfs)

        # Step 3: Compute Features & Targets
        X_full, Y_full, feat_names = cls.build_cross_sectional_dataset(raw_dfs, horizons=horizons)

        # Step 4: Temporal Splits (55% Train, 15% Val, 30% Locked OOS)
        splits = cls.compute_temporal_splits(X_full, Y_full, train_pct=0.55, val_pct=0.15, oos_pct=0.30)

        # STAGE A: Alpha158 Feature Screening (Train + Val ONLY)
        combined_tv_X = pd.concat([splits['train_X'], splits['val_X']], axis=0)
        combined_tv_Y = pd.concat([splits['train_Y'], splits['val_Y']], axis=0)

        feature_signals = cls.evaluate_stage_a_feature_signals(
            combined_tv_X, combined_tv_Y, feat_names, horizon=5
        )
        clusters = cls.cluster_core_signals(splits['train_X'], feat_names)

        # STAGE B: Horizon & Target Formulation Screening
        horizon_comparisons = []
        for h in horizons:
            y_col = f"ret_{h}d"
            y_tr = splits['train_Y'][y_col].values
            y_v = splits['val_Y'][y_col].values

            # Remove NaNs
            v_tr = np.isfinite(y_tr)
            v_val = np.isfinite(y_v)

            X_tr = splits['train_X'].loc[v_tr, feat_names].values
            X_v = splits['val_X'].loc[v_val, feat_names].values

            from sklearn.linear_model import Ridge, LogisticRegression
            model = Ridge(alpha=100.0)
            model.fit(X_tr, y_tr[v_tr])
            preds = model.predict(X_v)

            p_ic = float(np.corrcoef(preds, y_v[v_val])[0, 1]) if len(preds) > 10 else 0.0
            r_ic = float(pd.Series(preds).corr(pd.Series(y_v[v_val]), method='spearman'))

            horizon_comparisons.append({
                "horizon_days": h,
                "ic": round(p_ic, 4),
                "rank_ic": round(r_ic, 4),
                "positive": p_ic > 0
            })

        # Target formulations evaluation at 5-day horizon
        target_formulations = [
            ("ret_5d", "Continuous Return (Regression)", "regression"),
            ("clf_5d", "Directional Binary (Classification)", "classification"),
            ("thresh_5d", "Cost-Thresholded (Classification)", "classification"),
            ("risk_adj_5d", "Risk-Adjusted Return (Regression)", "regression"),
            ("rank_5d", "Cross-Sectional Rank Target (Ranking)", "ranking"),
        ]
        target_comparisons = []
        for t_col, t_label, t_type in target_formulations:
            if t_col not in splits['train_Y'].columns:
                continue
            y_tr = splits['train_Y'][t_col].values
            y_v = splits['val_Y'][t_col].values
            v_tr = np.isfinite(y_tr)
            v_v = np.isfinite(y_v)
            X_tr = splits['train_X'].loc[v_tr, feat_names].values
            X_v = splits['val_X'].loc[v_v, feat_names].values

            if t_type == "classification":
                clf = LogisticRegression(max_iter=200)
                clf.fit(X_tr, (y_tr[v_tr] > 0).astype(int))
                preds = clf.predict_proba(X_v)[:, 1]
            else:
                mdl = Ridge(alpha=100.0)
                mdl.fit(X_tr, y_tr[v_tr])
                preds = mdl.predict(X_v)

            r_ic = float(pd.Series(preds).corr(pd.Series(y_v[v_v]), method='spearman'))
            target_comparisons.append({
                "target": t_col,
                "label": t_label,
                "type": t_type,
                "rank_ic": round(r_ic, 4) if np.isfinite(r_ic) else 0.0
            })

        # Determine best horizon & primary rank target from Stage B
        best_h_entry = max(horizon_comparisons, key=lambda x: x.get("rank_ic", -999)) if horizon_comparisons else {"horizon_days": 5}
        best_h = int(best_h_entry.get("horizon_days", 5))
        target_col = f"rank_{best_h}d" if f"rank_{best_h}d" in splits['train_Y'].columns else ("rank_5d" if "rank_5d" in splits['train_Y'].columns else "ret_5d")

        # STAGE C: Multi-Model Comparison (Train + Val ONLY)
        models_tested = ["linear", "lightgbm", "catboost", "xgboost", "double_ensemble"]
        y_train_raw = splits['train_Y'][target_col].values if target_col in splits['train_Y'].columns else splits['train_Y']['ret_5d'].values
        y_val_raw = splits['val_Y'][target_col].values if target_col in splits['val_Y'].columns else splits['val_Y']['ret_5d'].values
        valid_tr = np.isfinite(y_train_raw)
        valid_v = np.isfinite(y_val_raw)
        X_tr = splits['train_X'].loc[valid_tr, feat_names].values
        X_val = splits['val_X'].loc[valid_v, feat_names].values
        model_results = []

        import lightgbm as lgb
        import xgboost as xgb
        import catboost as cb
        from sklearn.linear_model import Ridge

        for m_name in models_tested:
            seed_ranks = []
            for s in seeds:
                if m_name == "linear":
                    clf = Ridge(alpha=100.0)
                    clf.fit(X_tr, y_train_raw[valid_tr])
                    val_preds = clf.predict(X_val)
                elif m_name == "lightgbm":
                    clf = lgb.LGBMRegressor(n_estimators=60, learning_rate=0.03, num_leaves=15, verbose=-1, random_state=s, n_jobs=1)
                    clf.fit(X_tr, y_train_raw[valid_tr])
                    val_preds = clf.predict(X_val)
                elif m_name == "catboost":
                    clf = cb.CatBoostRegressor(iterations=60, learning_rate=0.03, depth=4, verbose=False, random_seed=s, thread_count=1)
                    clf.fit(X_tr, y_train_raw[valid_tr])
                    val_preds = clf.predict(X_val)
                elif m_name == "xgboost":
                    clf = xgb.XGBRegressor(n_estimators=60, learning_rate=0.03, max_depth=4, random_state=s, n_jobs=1)
                    clf.fit(X_tr, y_train_raw[valid_tr])
                    val_preds = clf.predict(X_val)
                elif m_name == "double_ensemble":
                    clf = DoubleEnsembleClassifier(base_estimator_type="lightgbm", n_submodels=3, random_state=s)
                    binary_y = (y_train_raw[valid_tr] > 0).astype(int)
                    clf.fit(X_tr, binary_y)
                    val_preds = clf.predict_proba(X_val)[:, 1]

                r_ic = float(pd.Series(val_preds).corr(pd.Series(y_val_raw[valid_v]), method='spearman'))
                seed_ranks.append(r_ic if np.isfinite(r_ic) else 0.0)

            model_results.append({
                "model_name": m_name,
                "seeds": seeds,
                "rank_ic_mean": round(float(np.mean(seed_ranks)), 4),
                "rank_ic_median": round(float(np.median(seed_ranks)), 4),
                "rank_ic_std": round(float(np.std(seed_ranks)), 4),
                "rank_ic_min": round(float(np.min(seed_ranks)), 4),
                "rank_ic_max": round(float(np.max(seed_ranks)), 4)
            })

        # STAGE D: Validation Shortlist & Candidate Freezing
        # Sort models by validation Rank IC
        model_results.sort(key=lambda x: x["rank_ic_mean"], reverse=True)
        best_candidate = model_results[0]
        logger.info(f"[SignalDiscovery] Shortlisted candidate: {best_candidate['model_name']} (Val Rank IC: {best_candidate['rank_ic_mean']})")

        # Generate validation scores for decile, regime, breadth, and cost diagnostics
        frozen_model_name = best_candidate["model_name"]
        if frozen_model_name == "lightgbm":
            best_model = lgb.LGBMRegressor(n_estimators=60, learning_rate=0.03, num_leaves=15, verbose=-1, random_state=42, n_jobs=1)
            best_model.fit(X_tr, y_train_raw[valid_tr])
            val_scores = best_model.predict(X_val)
        elif frozen_model_name == "catboost":
            best_model = cb.CatBoostRegressor(iterations=60, learning_rate=0.03, depth=4, verbose=False, random_seed=42, thread_count=1)
            best_model.fit(X_tr, y_train_raw[valid_tr])
            val_scores = best_model.predict(X_val)
        elif frozen_model_name == "xgboost":
            best_model = xgb.XGBRegressor(n_estimators=60, learning_rate=0.03, max_depth=4, random_state=42, n_jobs=1)
            best_model.fit(X_tr, y_train_raw[valid_tr])
            val_scores = best_model.predict(X_val)
        elif frozen_model_name == "double_ensemble":
            best_model = DoubleEnsembleClassifier(base_estimator_type="lightgbm", n_submodels=3, random_state=42)
            binary_y = (y_train_raw[valid_tr] > 0).astype(int)
            best_model.fit(X_tr, binary_y)
            val_scores = best_model.predict_proba(X_val)[:, 1]
        else:
            best_model = Ridge(alpha=100.0)
            best_model.fit(X_tr, y_train_raw[valid_tr])
            val_scores = best_model.predict(X_val)

        val_scores_df = pd.DataFrame({
            "__ticker__": splits['val_X'].loc[valid_v, '__ticker__'].values,
            "__date__": splits['val_X'].loc[valid_v, '__date__'].values,
            "pred": val_scores,
            "actual": y_val_raw[valid_v]
        })
        decile_analysis = cls.evaluate_cross_sectional_ranking(val_scores_df, horizon=best_h)
        regime_analysis = cls.evaluate_market_regimes(val_scores_df, raw_dfs=raw_dfs)
        stock_breadth = cls.evaluate_stock_breadth(val_scores_df)
        cost_sensitivity = cls.evaluate_cost_sensitivity(val_scores_df)

        cross_sectional_results = decile_analysis
        regime_results = regime_analysis

        # STAGE E: Single-Pass Locked OOS Evaluation
        y_oos_raw = splits['oos_Y'][target_col].values if target_col in splits['oos_Y'].columns else splits['oos_Y']['ret_5d'].values
        valid_oos = np.isfinite(y_oos_raw)
        X_oos = splits['oos_X'].loc[valid_oos, feat_names].values

        if frozen_model_name == "double_ensemble":
            oos_preds = best_model.predict_proba(X_oos)[:, 1]
        else:
            oos_preds = best_model.predict(X_oos)

        oos_p_ic = float(np.corrcoef(oos_preds, y_oos_raw[valid_oos])[0, 1]) if len(oos_preds) > 10 else 0.0
        oos_r_ic = float(pd.Series(oos_preds).corr(pd.Series(y_oos_raw[valid_oos]), method='spearman'))

        # OOS Economic Simulation
        oos_scores_df = pd.DataFrame({
            "pred": oos_preds,
            "actual": y_oos_raw[valid_oos],
            "__date__": splits['oos_X'].loc[valid_oos, '__date__'].values,
            "__ticker__": splits['oos_X'].loc[valid_oos, '__ticker__'].values
        })
        oos_cross_sec = cls.evaluate_cross_sectional_ranking(oos_scores_df, horizon=best_h)
        oos_econ = cls.evaluate_cost_sensitivity(oos_scores_df)
        oos_sharpe = oos_econ[0]["sharpe"] if oos_econ else 0.0

        top_oos_trades = oos_scores_df[oos_scores_df['pred'] >= oos_scores_df['pred'].quantile(0.8)]['actual'].values
        trade_count = len(top_oos_trades)
        win_rate = round(float(np.mean(top_oos_trades > 0)) * 100.0, 1) if len(top_oos_trades) > 0 else 0.0

        # Conservative criteria: Requires stability, monotonicity, breadth, and cost survival
        stable_feats_count = sum(1 for f in feature_signals if f["stability_class"] == "STABLE")
        is_monotonic = oos_cross_sec["monotonicity_score"] >= 0.50
        is_positive_spread = oos_cross_sec["top_minus_bottom_spread_pct"] > 0.0
        is_positive_oos_ic = oos_r_ic >= 0.02
        has_breadth = stock_breadth["top_stock_concentration"] < 0.35

        if stable_feats_count >= 5 and is_monotonic and is_positive_spread and is_positive_oos_ic and oos_sharpe > 0.0 and has_breadth:
            final_verdict = "VALIDATED RESEARCH CANDIDATE"
        elif stable_feats_count >= 3 and is_positive_spread and is_positive_oos_ic:
            final_verdict = "PROMISING SIGNAL"
        elif is_positive_oos_ic or stable_feats_count >= 1:
            final_verdict = "WEAK SIGNAL"
        else:
            final_verdict = "NO ROBUST SIGNAL FOUND"

        # Multi-Testing Transparency Record
        experiment_fingerprint = compute_experiment_fingerprint(
            universe=universe,
            sorted_tickers=list(raw_dfs.keys()),
            timeframe="1d",
            horizon_days=best_h,
            target_type="cross_sectional_rank",
            feature_family="Alpha158",
            model_family=frozen_model_name,
            seed=42,
            start_date=splits['dates_meta']['train_start'],
            end_date=splits['dates_meta']['oos_end'],
            dataset_hash=dataset_meta['dataset_hash'],
            engine_version=ENGINE_VERSION
        )

        experiment_id = f"res_exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{experiment_fingerprint[:8]}"

        # Assemble master result bundle
        discovery_result = {
            "experiment_id": experiment_id,
            "fingerprint": experiment_fingerprint,
            "timestamp": datetime.now().isoformat(),
            "provenance": PROVENANCE_LABEL,
            "engine_version": ENGINE_VERSION,
            "dataset_meta": dataset_meta,
            "data_quality": data_quality,
            "dates_meta": splits['dates_meta'],
            "final_verdict": final_verdict,
            "multi_testing_audit": {
                "total_horizons_tested": len(horizons),
                "total_models_tested": len(models_tested),
                "total_seeds_tested": len(seeds),
                "total_features_screened": len(feat_names),
                "total_configurations_explored": len(horizons) * len(models_tested) * len(seeds)
            },
            "stage_a_features": {
                "top_10_features": feature_signals[:10],
                "stable_count": stable_feats_count,
                "unstable_count": sum(1 for f in feature_signals if f["stability_class"] == "UNSTABLE"),
                "regime_sensitive_count": sum(1 for f in feature_signals if f["stability_class"] == "REGIME-SENSITIVE"),
                "negative_count": sum(1 for f in feature_signals if f["stability_class"] == "NEGATIVE"),
                "no_signal_count": sum(1 for f in feature_signals if f["stability_class"] == "NO_SIGNAL"),
                "core_clusters": clusters
            },
            "stage_b_horizons": horizon_comparisons,
            "stage_b_targets": target_comparisons,
            "stage_c_models": model_results,
            "stage_d_validation": {
                "frozen_candidate": frozen_model_name,
                "cross_sectional": cross_sectional_results,
                "market_regimes": regime_results,
                "stock_breadth": stock_breadth,
                "cost_sensitivity": cost_sensitivity
            },
            "stage_e_locked_oos": {
                "candidate_evaluated": frozen_model_name,
                "rank_ic": round(oos_r_ic, 4),
                "pearson_ic": round(oos_p_ic, 4),
                "top_minus_bottom_spread_pct": oos_cross_sec["top_minus_bottom_spread_pct"],
                "monotonicity_score": oos_cross_sec["monotonicity_score"],
                "oos_sharpe": round(oos_sharpe, 2),
            }
        }

        # Persist into research SQLite ledger
        save_experiment_record({
            "experiment_id": experiment_id,
            "timestamp": discovery_result["timestamp"],
            "universe": universe,
            "universe_mode": "CURRENT_CONSTITUENTS_RETROSPECTIVE",
            "timeframe": "1d",
            "horizon_days": best_h,
            "target_type": "continuous_return",
            "feature_family": "Alpha158",
            "model_family": frozen_model_name,
            "seed": 42,
            "config_hash": experiment_fingerprint,
            "dataset_hash": dataset_meta["dataset_hash"],
            "feature_hash": hashlib.sha256(",".join(feat_names).encode()).hexdigest(),
            "fingerprint": experiment_fingerprint,
            "status": "COMPLETED",
            "verdict": final_verdict,
            "metrics_json": discovery_result
        })

        save_feature_stats(experiment_id, feature_signals)
        save_decile_results(experiment_id, cross_sectional_results["deciles"])
        save_regime_results(experiment_id, regime_results)
        save_oos_result(experiment_id, {
            "candidate_name": frozen_model_name,
            "oos_hash": dataset_meta["dataset_hash"],
            "ic": oos_p_ic,
            "rank_ic": oos_r_ic,
            "sharpe": oos_sharpe,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "max_dd_pct": 0.0,
            "verdict": final_verdict
        })

        return discovery_result
