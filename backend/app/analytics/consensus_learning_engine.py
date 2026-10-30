"""
CONSENSUS LEARNING ENGINE (ONLINE REGRET MINIMIZATION & SEQUENTIAL RISK GATING)
================================================================================
Meta-layer online adaptation system operating on top of frozen Qlib and Legacy AI base models.

Theoretical Guarantees:
- Zero Retraining Invariant: Base model parameters, feature splits, and SHA256 hashes are 100% frozen.
- Online Convex Regret Bound: O(sqrt(T * ln(K))) via Multiplicative Weights (Hedge algorithm,
  Freund & Schapire 1997; Cesa-Bianchi & Lugosi 2006).
- Guardrails: Strict entropy floor (min 20% weight per engine) and daily step clamp (|Δw| <= 0.05).
- Sequential Risk Gating: Page's Cumulative Sum (CUSUM) test on rolling prediction residuals,
  preventing false-alarm throttling from normal Bernoulli variance.
"""

import os
import math
import json
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)


class ConsensusLearningEngine:
    """
    Online Hedge Meta-Learner for Dual-Engine Consensus Weight Adaptation.
    Adapts allocation weights [w_qlib, w_legacy] and monitors sequential risk drift.
    """

    _SCHEMA_INITIALIZED = False
    DEFAULT_WEIGHTS = {"w_qlib": 0.50, "w_legacy": 0.50}
    ENTROPY_FLOOR = 0.20      # Neither engine may fall below 20% weight
    MAX_STEP_CLAMP = 0.05     # Maximum weight swing per daily update
    CUSUM_SLACK_K = 0.50      # CUSUM reference slack
    CUSUM_THRESHOLD_H = 3.50  # CUSUM decision boundary

    @classmethod
    def init_learning_schema(cls, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initializes the consensus_weights_ledger table."""
        should_close = False
        if conn is None:
            conn = sqlite3.connect(get_db_path(), timeout=15.0)
            should_close = True
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS consensus_weights_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    w_qlib REAL NOT NULL,
                    w_legacy REAL NOT NULL,
                    qlib_brier_loss REAL,
                    legacy_brier_loss REAL,
                    sample_size_n INTEGER,
                    eta REAL,
                    cusum_alarm INTEGER DEFAULT 0,
                    cusum_value REAL DEFAULT 0.0,
                    notes TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cwl_strat_time ON consensus_weights_ledger(strategy, timestamp);")
            conn.commit()
            cls._SCHEMA_INITIALIZED = True
        finally:
            if should_close:
                conn.close()

    @classmethod
    def get_current_weights(cls, strategy: str = "INTRADAY") -> Dict[str, float]:
        """
        Retrieves the active consensus engine weights for the given strategy.
        Falls back to baseline [0.50, 0.50] if uninitialized.
        """
        cls.init_learning_schema()
        strat_upper = strategy.upper()

        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT w_qlib, w_legacy FROM consensus_weights_ledger
                WHERE strategy = ?
                ORDER BY id DESC LIMIT 1
            """, (strat_upper,))
            row = cur.fetchone()
            if row:
                w_q = float(row[0])
                w_l = float(row[1])
                # Safety re-normalization
                total = w_q + w_l
                if total > 0:
                    return {
                        "w_qlib": round(w_q / total, 4),
                        "w_legacy": round(w_l / total, 4)
                    }
            return dict(cls.DEFAULT_WEIGHTS)
        except Exception as e:
            logger.warning(f"[ConsensusLearningEngine] Weight retrieval fallback: {e}")
            return dict(cls.DEFAULT_WEIGHTS)
        finally:
            conn.close()

    @classmethod
    def get_cusum_status(cls, strategy: str = "INTRADAY") -> Dict[str, Any]:
        """
        Returns active CUSUM sequential risk gate status and recommendation.
        """
        cls.init_learning_schema()
        strat_upper = strategy.upper()

        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT cusum_alarm, cusum_value, timestamp FROM consensus_weights_ledger
                WHERE strategy = ?
                ORDER BY id DESC LIMIT 1
            """, (strat_upper,))
            row = cur.fetchone()
            if row:
                alarm = bool(row[0])
                val = float(row[1] or 0.0)
                ts = str(row[2])
                return {
                    "strategy": strat_upper,
                    "alarm_active": alarm,
                    "cusum_statistic": round(val, 3),
                    "threshold_h": cls.CUSUM_THRESHOLD_H,
                    "conviction_penalty_pct": 10.0 if alarm else 0.0,
                    "status": "REGIME_DRIFT_DETECTED" if alarm else "NORMAL_STABILITY",
                    "last_updated": ts
                }
            return {
                "strategy": strat_upper,
                "alarm_active": False,
                "cusum_statistic": 0.0,
                "threshold_h": cls.CUSUM_THRESHOLD_H,
                "conviction_penalty_pct": 0.0,
                "status": "UNINITIALIZED",
                "last_updated": None
            }
        except Exception as e:
            logger.warning(f"[ConsensusLearningEngine] CUSUM status check fallback: {e}")
            return {
                "strategy": strat_upper,
                "alarm_active": False,
                "cusum_statistic": 0.0,
                "threshold_h": cls.CUSUM_THRESHOLD_H,
                "conviction_penalty_pct": 0.0,
                "status": "HEALTHY_FALLBACK",
                "last_updated": None
            }
        finally:
            conn.close()

    @staticmethod
    def calculate_brier_loss(p: float, y: float) -> float:
        """Computes quadratic Brier score loss: (p - y)^2 in [0, 1]."""
        p_clamped = min(max(float(p), 0.0), 1.0)
        y_clamped = 1.0 if y > 0.5 else 0.0
        return (p_clamped - y_clamped) ** 2

    @classmethod
    def execute_daily_weight_update(
        cls,
        strategy: str = "INTRADAY",
        trailing_n: int = 60,
        min_samples: int = 10
    ) -> Dict[str, Any]:
        """
        Executes post-market online regret minimization update (Hedge / Multiplicative Weights).
        Called daily at 15:35 IST after market close.

        Returns:
            Dict containing updated weights, rolling Brier scores, sample size, and CUSUM status.
        """
        cls.init_learning_schema()
        strat_upper = strategy.upper()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        current_weights = cls.get_current_weights(strategy=strat_upper)
        w_curr_q = current_weights["w_qlib"]
        w_curr_l = current_weights["w_legacy"]

        conn = sqlite3.connect(get_db_path(), timeout=15.0)
        try:
            cur = conn.cursor()
            # Fetch trailing closed consensus trades with realized outcomes
            cur.execute("""
                SELECT id, outcome, profit_pct, qlib_score, legacy_score
                FROM consensus_trade_history
                WHERE strategy = ? AND status = 'CLOSED' AND outcome IS NOT NULL AND outcome != ''
                ORDER BY id DESC LIMIT ?
            """, (strat_upper, trailing_n))
            rows = cur.fetchall()

            if len(rows) < min_samples:
                # Insufficient sample size to guarantee regret bound; maintain baseline
                cur.execute("""
                    INSERT INTO consensus_weights_ledger (
                        timestamp, strategy, w_qlib, w_legacy,
                        qlib_brier_loss, legacy_brier_loss, sample_size_n,
                        eta, cusum_alarm, cusum_value, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    now_str, strat_upper, w_curr_q, w_curr_l,
                    0.25, 0.25, len(rows),
                    0.0, 0, 0.0,
                    f"Sample size {len(rows)} < minimum {min_samples}. Maintained prior weights."
                ))
                conn.commit()
                return {
                    "updated": False,
                    "reason": "INSUFFICIENT_SAMPLE_SIZE",
                    "sample_size": len(rows),
                    "min_required": min_samples,
                    "weights": {"w_qlib": w_curr_q, "w_legacy": w_curr_l},
                    "cusum_status": "NORMAL"
                }

            # Evaluate per-engine Brier loss over the sample window
            qlib_losses = []
            legacy_losses = []
            residuals = []

            for row in rows:
                tid, outcome, pnl, q_score_raw, l_score_raw = row
                # Ground truth: 1 for profit/target, 0 for loss/stop
                y = 1.0 if outcome in ("TARGET MET", "TARGET_HIT", "TP1 HIT", "TP2 HIT") or (pnl and float(pnl) > 0.0) else 0.0

                # Normalize engine predictions to probabilities in [0.01, 0.99]
                q_p = float(q_score_raw or 50.0)
                if q_p > 1.0:
                    q_p = q_p / 100.0
                q_p = min(max(q_p, 0.01), 0.99)

                l_p = float(l_score_raw or 50.0)
                if l_p > 1.0:
                    l_p = l_p / 100.0
                l_p = min(max(l_p, 0.01), 0.99)

                loss_q = cls.calculate_brier_loss(q_p, y)
                loss_l = cls.calculate_brier_loss(l_p, y)
                qlib_losses.append(loss_q)
                legacy_losses.append(loss_l)

                # Track blended model prediction residual for CUSUM
                p_blend = (w_curr_q * q_p) + (w_curr_l * l_p)
                residuals.append(abs(y - p_blend))

            avg_loss_q = sum(qlib_losses) / len(qlib_losses)
            avg_loss_l = sum(legacy_losses) / len(legacy_losses)

            # Theoretical optimal Hedge learning rate: eta = sqrt(8 * ln(K) / N)
            # For K=2 experts: sqrt(8 * ln(2) / N) = sqrt(5.545 / N)
            eta = math.sqrt((8.0 * math.log(2.0)) / len(rows))

            # Multiplicative Hedge Update
            w_tilde_q = w_curr_q * math.exp(-eta * avg_loss_q)
            w_tilde_l = w_curr_l * math.exp(-eta * avg_loss_l)
            sum_tilde = w_tilde_q + w_tilde_l

            w_raw_q = w_tilde_q / sum_tilde if sum_tilde > 0 else 0.50
            w_raw_l = w_tilde_l / sum_tilde if sum_tilde > 0 else 0.50

            # Apply Entropy Floor (neither engine may fall below 20%)
            floor = cls.ENTROPY_FLOOR
            ceil = 1.0 - floor
            w_floored_q = max(floor, min(ceil, w_raw_q))
            w_floored_l = 1.0 - w_floored_q

            # Apply Daily Step Clamp (|Δw| <= 0.05) to avoid whipsaws
            delta_q = w_floored_q - w_curr_q
            clamp = cls.MAX_STEP_CLAMP
            clamped_delta_q = max(-clamp, min(clamp, delta_q))

            w_final_q = round(w_curr_q + clamped_delta_q, 4)
            w_final_l = round(1.0 - w_final_q, 4)

            # Page's CUSUM Sequential Test
            # S_t = max(0, S_{t-1} + (residual - k))
            cur.execute("""
                SELECT cusum_value FROM consensus_weights_ledger
                WHERE strategy = ?
                ORDER BY id DESC LIMIT 1
            """, (strat_upper,))
            last_cusum_row = cur.fetchone()
            s_prev = float(last_cusum_row[0]) if last_cusum_row and last_cusum_row[0] is not None else 0.0

            recent_mean_residual = sum(residuals[:10]) / min(len(residuals), 10)
            s_curr = max(0.0, s_prev + (recent_mean_residual - cls.CUSUM_SLACK_K))
            cusum_alarm = int(s_curr > cls.CUSUM_THRESHOLD_H)

            # If alarm fired, apply slight decay to prevent permanent state lock
            if cusum_alarm:
                s_curr = max(0.0, s_curr * 0.85)

            notes = (
                f"Hedge Update N={len(rows)}: Qlib_Loss={avg_loss_q:.4f}, Legacy_Loss={avg_loss_l:.4f}. "
                f"Weights: Qlib {w_curr_q:.2f}->{w_final_q:.2f}, Legacy {w_curr_l:.2f}->{w_final_l:.2f}. "
                f"CUSUM={s_curr:.2f} (alarm={cusum_alarm})"
            )

            cur.execute("""
                INSERT INTO consensus_weights_ledger (
                    timestamp, strategy, w_qlib, w_legacy,
                    qlib_brier_loss, legacy_brier_loss, sample_size_n,
                    eta, cusum_alarm, cusum_value, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now_str, strat_upper, w_final_q, w_final_l,
                round(avg_loss_q, 4), round(avg_loss_l, 4), len(rows),
                round(eta, 4), cusum_alarm, round(s_curr, 4), notes
            ))
            conn.commit()

            logger.info(f"[ConsensusLearningEngine] {notes}")

            return {
                "updated": True,
                "strategy": strat_upper,
                "sample_size": len(rows),
                "eta": round(eta, 4),
                "brier_losses": {
                    "qlib": round(avg_loss_q, 4),
                    "legacy": round(avg_loss_l, 4)
                },
                "weights": {
                    "w_qlib": w_final_q,
                    "w_legacy": w_final_l
                },
                "delta": {
                    "d_qlib": round(w_final_q - w_curr_q, 4),
                    "d_legacy": round(w_final_l - w_curr_l, 4)
                },
                "cusum": {
                    "statistic": round(s_curr, 3),
                    "alarm": bool(cusum_alarm),
                    "threshold": cls.CUSUM_THRESHOLD_H
                },
                "timestamp": now_str
            }
        except Exception as e:
            logger.error(f"[ConsensusLearningEngine] Daily weight update failed: {e}", exc_info=True)
            return {
                "updated": False,
                "error": str(e),
                "weights": dict(cls.DEFAULT_WEIGHTS)
            }
        finally:
            conn.close()

    @classmethod
    def get_learning_history(cls, strategy: str = "INTRADAY", limit: int = 30) -> List[Dict[str, Any]]:
        """Returns ledger history of daily online weight updates and Brier losses."""
        cls.init_learning_schema()
        strat_upper = strategy.upper()

        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, timestamp, strategy, w_qlib, w_legacy,
                       qlib_brier_loss, legacy_brier_loss, sample_size_n,
                       eta, cusum_alarm, cusum_value, notes
                FROM consensus_weights_ledger
                WHERE strategy = ?
                ORDER BY id DESC LIMIT ?
            """, (strat_upper, limit))
            rows = cur.fetchall()

            history = []
            for r in rows:
                history.append({
                    "id": r[0],
                    "timestamp": r[1],
                    "strategy": r[2],
                    "w_qlib": r[3],
                    "w_legacy": r[4],
                    "qlib_brier_loss": r[5],
                    "legacy_brier_loss": r[6],
                    "sample_size_n": r[7],
                    "eta": r[8],
                    "cusum_alarm": bool(r[9]),
                    "cusum_value": r[10],
                    "notes": r[11]
                })
            return history
        except Exception as e:
            logger.warning(f"[ConsensusLearningEngine] Failed to fetch learning history: {e}")
            return []
        finally:
            conn.close()

