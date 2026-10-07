"""
CONSENSUS META-LEARNER V2: WALK-FORWARD EMPIRICAL TRAINER
=========================================================
Trains and evaluates Consensus Meta-Learner V2 using strictly point-in-time
chronological walk-forward splits on historical consensus trade data:
- Dev (Train) Split: 2026-09-18 to 2026-09-29 (~685 trades)
- Validation Split: 2026-09-30 to 2026-10-01 (~86 trades)
- Untouched OOS: 2026-10-02 to 2026-10-06 (~465 trades)

Target Definition:
P(NET-POSITIVE TRADE) where:
net_return = profit_pct - cost_hurdle_pct
Default friction cost: 15 bps (0.15%) round-trip brokerage + slippage.

Features:
- 39 Point-in-time contextual features across Groups A-G.
- Redundancy and L2 regularization applied to prevent overfitting.
- Isotonic regression / Platt sigmoid for probability calibration.
"""

import os
import json
import sqlite3
import logging
from typing import Dict, Any, List, Tuple, Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss

from app.analytics.consensus_meta_feature_extractor import (
    ConsensusMetaFeatureExtractor,
    FEATURE_NAMES,
    GROUP_SLICES
)
from app.analytics.consensus_meta_learner_v2 import (
    ConsensusMetaLearnerV2,
    DEFAULT_V2_ARTIFACT,
    DEFAULT_V2_MANIFEST,
    logit,
    sigmoid
)

logger = logging.getLogger(__name__)


class ConsensusMetaTrainerV2:
    """
    Orchestrates dataset extraction, chronological walk-forward training,
    ablation studies, and out-of-sample performance reporting for Meta-Learner V2.
    """

    @classmethod
    def load_historical_consensus_dataset(
        cls,
        db_path: Optional[str] = None,
        cost_friction_pct: float = 0.15
    ) -> pd.DataFrame:
        """
        Loads closed consensus trades and extracts point-in-time feature vectors.
        """
        if db_path is None:
            from app.data.historical_data_layer import get_db_path
            db_path = get_db_path()

        conn = sqlite3.connect(db_path, timeout=15.0)
        try:
            trades_df = pd.read_sql_query("""
                SELECT id, timestamp, ticker, symbol, strategy, direction,
                       entry_price, stop_loss, target_price, composite_score,
                       qlib_score, legacy_score, finbert_score, risk_reward_ratio,
                       status, outcome, profit_pct, exit_price, exit_time, details_json
                FROM consensus_trade_history
                WHERE profit_pct IS NOT NULL
                ORDER BY timestamp ASC
            """, conn)

            logger.info(f"[MetaTrainer] Loaded {len(trades_df)} historical closed trades.")
            
            # Extract point-in-time features for each row
            records = []
            for idx, row in trades_df.iterrows():
                cand_dict = dict(row)
                if row.get("details_json"):
                    try:
                        d = json.loads(row["details_json"])
                        cand_dict.update(d)
                    except Exception:
                        pass
                
                feats = ConsensusMetaFeatureExtractor.extract_point_in_time_features(
                    candidate=cand_dict, db_conn=conn
                )
                
                # Calculate economic net outcome
                gross_pnl = float(row["profit_pct"] or 0.0)
                net_pnl = gross_pnl - cost_friction_pct
                is_net_positive = 1 if net_pnl > 0.0 else 0

                rec = {
                    "id": row["id"],
                    "timestamp": str(row["timestamp"]),
                    "date": str(row["timestamp"])[:10],
                    "ticker": row["ticker"],
                    "direction": row["direction"],
                    "composite_score": float(row["composite_score"] or 50.0),
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "target_net_positive": is_net_positive
                }
                rec.update(feats)
                records.append(rec)

            df_dataset = pd.DataFrame(records)
            return df_dataset
        finally:
            conn.close()

    @classmethod
    def train_and_evaluate(
        cls,
        df_dataset: pd.DataFrame,
        train_cutoff: str = "2026-09-29",
        val_cutoff: str = "2026-10-01",
        c_regularization: float = 0.5,
        save_artifact: bool = True
    ) -> Dict[str, Any]:
        """
        Executes chronological walk-forward training:
        - Train (Dev): <= train_cutoff
        - Validation: > train_cutoff AND <= val_cutoff
        - OOS (Test): > val_cutoff
        """
        train_df = df_dataset[df_dataset["date"] <= train_cutoff].copy()
        val_df = df_dataset[(df_dataset["date"] > train_cutoff) & (df_dataset["date"] <= val_cutoff)].copy()
        oos_df = df_dataset[df_dataset["date"] > val_cutoff].copy()

        logger.info(
            f"[MetaTrainer] Splits -> Train: {len(train_df)} | Val: {len(val_df)} | OOS: {len(oos_df)}"
        )

        X_train = train_df[FEATURE_NAMES].values
        y_train = train_df["target_net_positive"].values

        X_val = val_df[FEATURE_NAMES].values
        y_val = val_df["target_net_positive"].values

        X_oos = oos_df[FEATURE_NAMES].values
        y_oos = oos_df["target_net_positive"].values

        # 1. Fit L2-Regularized Logistic Classifier
        model = LogisticRegression(
            C=c_regularization,
            penalty="l2",
            solver="lbfgs",
            class_weight="balanced",
            max_iter=1000,
            random_state=42
        )
        model.fit(X_train, y_train)

        # 2. Fit Isotonic Probability Calibrator on Validation set
        val_raw_preds = model.predict_proba(X_val)[:, 1]
        calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
        calibrator.fit(val_raw_preds, y_val)

        # 3. Evaluate Baseline vs Meta-Learner V2 on Untouched OOS
        base_oos_trades = len(oos_df)
        base_net_pnl = float(oos_df["net_pnl"].sum())
        base_wins = int(oos_df["target_net_positive"].sum())
        base_wr = (base_wins / base_oos_trades * 100.0) if base_oos_trades > 0 else 0.0
        base_expectancy = (base_net_pnl / base_oos_trades) if base_oos_trades > 0 else 0.0

        # Meta-Learner V2 on OOS
        oos_raw_probs = model.predict_proba(X_oos)[:, 1]
        oos_cal_probs = calibrator.predict(oos_raw_probs)
        oos_df["meta_cal_prob"] = oos_cal_probs

        # Decision threshold: 0.50 probability hurdle
        meta_take_mask = oos_df["meta_cal_prob"] >= 0.50
        meta_taken_df = oos_df[meta_take_mask]
        meta_skipped_df = oos_df[~meta_take_mask]

        meta_oos_trades = len(meta_taken_df)
        meta_net_pnl = float(meta_taken_df["net_pnl"].sum()) if meta_oos_trades > 0 else 0.0
        meta_wins = int(meta_taken_df["target_net_positive"].sum()) if meta_oos_trades > 0 else 0
        meta_wr = (meta_wins / meta_oos_trades * 100.0) if meta_oos_trades > 0 else 0.0
        meta_expectancy = (meta_net_pnl / meta_oos_trades) if meta_oos_trades > 0 else 0.0

        losses_avoided = int((meta_skipped_df["net_pnl"] <= 0).sum())
        winners_rejected = int((meta_skipped_df["net_pnl"] > 0).sum())
        net_saved_by_filtering = float(-meta_skipped_df["net_pnl"].sum())

        # Calibration & Brier Score
        brier_baseline = brier_score_loss(y_oos, oos_df["composite_score"].values / 100.0)
        brier_meta = brier_score_loss(y_oos, oos_cal_probs)

        # 4. Feature Group Ablation Studies on OOS
        ablation_results = {}
        for group_name, feats in GROUP_SLICES.items():
            rem_feats = [f for f in FEATURE_NAMES if f not in feats]
            abl_model = LogisticRegression(C=c_regularization, penalty="l2", solver="lbfgs", class_weight="balanced", random_state=42)
            abl_model.fit(train_df[rem_feats].values, y_train)
            abl_probs = abl_model.predict_proba(oos_df[rem_feats].values)[:, 1]
            abl_taken = oos_df[abl_probs >= 0.50]
            abl_exp = float(abl_taken["net_pnl"].mean()) if len(abl_taken) > 0 else 0.0
            ablation_results[f"WITHOUT_{group_name}"] = {
                "trades": len(abl_taken),
                "net_expectancy": round(abl_exp, 3),
                "delta_expectancy_vs_all": round(meta_expectancy - abl_exp, 3)
            }

        # 5. Performance by Market Regime in OOS
        regime_breakdown = {}
        for reg_col, reg_label in [
            ("nifty_regime_bull", "NIFTY_BULLISH"),
            ("nifty_regime_bear", "NIFTY_BEARISH")
        ]:
            sub_oos = oos_df[oos_df[reg_col] == 1.0]
            if len(sub_oos) > 0:
                sub_meta = sub_oos[sub_oos["meta_cal_prob"] >= 0.50]
                regime_breakdown[reg_label] = {
                    "baseline_trades": len(sub_oos),
                    "baseline_net": round(float(sub_oos["net_pnl"].sum()), 2),
                    "baseline_expectancy": round(float(sub_oos["net_pnl"].mean()), 3),
                    "meta_trades": len(sub_meta),
                    "meta_net": round(float(sub_meta["net_pnl"].sum()), 2),
                    "meta_expectancy": round(float(sub_meta["net_pnl"].mean()), 3) if len(sub_meta) > 0 else 0.0
                }

        results = {
            "status": "SUCCESS",
            "train_period": f"<= {train_cutoff} ({len(train_df)} trades)",
            "val_period": f"{train_cutoff} to {val_cutoff} ({len(val_df)} trades)",
            "oos_period": f"> {val_cutoff} ({len(oos_df)} trades)",
            "feature_count": len(FEATURE_NAMES),
            "baseline_champion": {
                "trades": base_oos_trades,
                "net_pnl": round(base_net_pnl, 2),
                "win_rate_pct": round(base_wr, 1),
                "net_expectancy": round(base_expectancy, 3),
                "brier_score": round(brier_baseline, 4)
            },
            "meta_learner_v2": {
                "trades": meta_oos_trades,
                "net_pnl": round(meta_net_pnl, 2),
                "win_rate_pct": round(meta_wr, 1),
                "net_expectancy": round(meta_expectancy, 3),
                "brier_score": round(brier_meta, 4),
                "trades_filtered": len(meta_skipped_df),
                "losses_avoided": losses_avoided,
                "winners_rejected": winners_rejected,
                "net_capital_saved": round(net_saved_by_filtering, 2)
            },
            "comparison": {
                "expectancy_improvement": round(meta_expectancy - base_expectancy, 3),
                "net_pnl_improvement": round(meta_net_pnl - base_net_pnl, 2),
                "brier_score_improvement": round(brier_baseline - brier_meta, 4)
            },
            "ablation": ablation_results,
            "regime_breakdown": regime_breakdown,
            "feature_coefficients": {
                feat: round(float(coef), 4)
                for feat, coef in zip(FEATURE_NAMES, model.coef_[0])
            }
        }

        # 6. Save Challenger Artifact
        if save_artifact:
            challenger = ConsensusMetaLearnerV2(auto_load=False)
            challenger.model = model
            challenger.calibrator = calibrator
            challenger.feature_names = list(FEATURE_NAMES)
            challenger.group_slices = dict(GROUP_SLICES)
            challenger.save_model(DEFAULT_V2_ARTIFACT, DEFAULT_V2_MANIFEST, metrics_summary=results)

        return results
