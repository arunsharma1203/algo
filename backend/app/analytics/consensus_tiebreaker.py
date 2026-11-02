"""
Consensus Tiebreaker & Trade Selection Engine.

Provides an economically meaningful, deterministic priority hierarchy for breaking ties
between consensus candidates in Consensus Game Arena and Consensus Tracker feeds:

Hierarchy:
1. Composite Conviction Score (if difference > epsilon = 0.05, higher score wins outright)
2. Risk-to-Reward Ratio (higher asymmetric payoff wins)
3. Inter-Engine Agreement Spread (|qlib_score - legacy_score|, lower spread wins)
4. Sector Momentum Alignment (directional sector strength from open)
5. VWAP Momentum Alignment (favorable distance from intraday VWAP)
6. Deterministic Alphabetical Symbol Sort (ticker string, 100% ID-independent)

ABSOLUTE HISTORICAL DATA LOCK:
For trades prior to HISTORICAL_TIEBREAKER_CUTOFF (2026-09-24 14:45:00),
the historical ranking logic is preserved byte-for-byte to guarantee zero
state drift and zero retroactive change to past trading records or P&L.
"""

import json
from functools import cmp_to_key
from typing import Any, Dict, List, Optional, Tuple, Union

# Strict historical lock: all trades detected before this timestamp preserve their exact historical tiebreaker
HISTORICAL_TIEBREAKER_CUTOFF = "2026-09-24 14:45:00"
EPSILON_SCORE = 0.05  # 0.05% score conviction tolerance


class CandidateMetrics:
    def __init__(self, raw: Union[Tuple, Dict[str, Any]], score_index: int = 12):
        if isinstance(raw, dict):
            self.id = int(raw.get("id") or 0)
            self.timestamp = str(raw.get("timestamp") or "")
            self.ticker = str(raw.get("ticker") or raw.get("symbol") or "").strip().upper()
            self.direction = str(raw.get("direction") or "BUY").upper()
            self.entry = float(raw.get("entry") or raw.get("entry_price") or 0.0)
            self.sl = float(raw.get("sl") or raw.get("stop_loss") or 0.0)
            self.tp = float(raw.get("tp") or raw.get("target_price") or 0.0)
            self.score = float(raw.get("score") or raw.get("composite_score") or 0.0)

            # RR ratio
            rr = float(raw.get("risk_reward_ratio") or 0.0)
            if rr <= 0.0 and self.entry > 0 and self.sl > 0 and self.tp > 0:
                risk = abs(self.entry - self.sl)
                reward = abs(self.tp - self.entry)
                rr = round(reward / risk, 2) if risk > 0 else 1.25
            self.risk_reward_ratio = rr if rr > 0 else 1.25

            # Scores
            qm = raw.get("qlib_metrics") or {}
            lm = raw.get("legacy_metrics") or {}
            self.qlib_score = float(raw.get("qlib_score") or qm.get("score") or 0.0)
            self.legacy_score = float(raw.get("legacy_score") or lm.get("confidence") or 0.0)

            # Details json / metadata
            details = raw.get("details_json")
            if isinstance(details, str) and details.strip():
                try:
                    details = json.loads(details)
                except Exception:
                    details = {}
            elif not isinstance(details, dict):
                details = {}

            self.sector_pct = float(raw.get("sector_pct_from_open") or details.get("sector_pct_from_open") or 0.0)
            self.sector_trend = str(raw.get("sector_trend") or details.get("sector_trend") or "NEUTRAL").upper()
            self.vwap = float(raw.get("vwap") or details.get("vwap") or 0.0)
            self.day_open = float(raw.get("day_open") or details.get("day_open") or 0.0)
        else:
            # Tuple from SQLite query
            self.id = int(raw[0] or 0)
            self.timestamp = str(raw[1] or "")
            self.ticker = str(raw[2] or "").strip().upper()
            self.direction = str(raw[3] or "BUY").upper()
            self.entry = float(raw[4] or 0.0)

            if len(raw) > score_index:
                self.score = float(raw[score_index] or 0.0)
            else:
                self.score = float(raw[8] or 0.0) if len(raw) > 8 else 0.0

            if len(raw) >= 13 and score_index == 12:
                self.sl = float(raw[5] or 0.0)
                self.tp = float(raw[6] or 0.0)
            else:
                self.sl = 0.0
                self.tp = 0.0

            details = {}
            self.qlib_score = 0.0
            self.legacy_score = 0.0
            self.risk_reward_ratio = 1.25

            # If extended query includes extra columns
            if len(raw) >= 17 and score_index == 12:
                self.qlib_score = float(raw[13] or 0.0)
                self.legacy_score = float(raw[14] or 0.0)
                self.risk_reward_ratio = float(raw[15] or 1.25)
                det_raw = raw[16]
                if isinstance(det_raw, str) and det_raw.strip():
                    try:
                        details = json.loads(det_raw)
                    except Exception:
                        pass
            elif len(raw) > 9 and isinstance(raw[-1], str) and raw[-1].startswith("{"):
                try:
                    details = json.loads(raw[-1])
                except Exception:
                    pass

            # Extract from details if not explicitly populated
            if self.risk_reward_ratio <= 1.25 and "risk_reward_ratio" in details:
                self.risk_reward_ratio = float(details["risk_reward_ratio"] or 1.25)
            elif self.risk_reward_ratio <= 1.25 and self.entry > 0 and self.sl > 0 and self.tp > 0:
                risk = abs(self.entry - self.sl)
                reward = abs(self.tp - self.entry)
                self.risk_reward_ratio = round(reward / risk, 2) if risk > 0 else 1.25

            if self.qlib_score == 0.0 and "qlib_metrics" in details:
                self.qlib_score = float(details["qlib_metrics"].get("score") or 0.0)
            if self.legacy_score == 0.0 and "legacy_metrics" in details:
                self.legacy_score = float(details["legacy_metrics"].get("confidence") or 0.0)

            self.sector_pct = float(details.get("sector_pct_from_open") or 0.0)
            self.sector_trend = str(details.get("sector_trend") or "NEUTRAL").upper()
            self.vwap = float(details.get("vwap") or 0.0)
            self.day_open = float(details.get("day_open") or 0.0)


def compare_candidates_economically(a: CandidateMetrics, b: CandidateMetrics) -> int:
    """
    Compares two candidate trades under the economic tie-breaker hierarchy.
    Returns:
       1 if a > b (a wins)
      -1 if a < b (b wins)
       0 if exactly equal
    """
    # 1. Primary: Composite Score (outside epsilon)
    score_diff = a.score - b.score
    if abs(score_diff) > EPSILON_SCORE:
        return 1 if score_diff > 0 else -1

    # Within EPSILON_SCORE: Economically meaningful tiebreakers:
    # 2. Risk-to-Reward Ratio (higher asymmetric payoff wins)
    rr_diff = a.risk_reward_ratio - b.risk_reward_ratio
    if abs(rr_diff) > 0.01:
        return 1 if rr_diff > 0 else -1

    # 3. Inter-Engine Agreement Spread (lower spread = higher confluence)
    spread_a = abs(a.qlib_score - a.legacy_score) if (a.qlib_score > 0 and a.legacy_score > 0) else 999.0
    spread_b = abs(b.qlib_score - b.legacy_score) if (b.qlib_score > 0 and b.legacy_score > 0) else 999.0
    if abs(spread_a - spread_b) > 0.1:
        return 1 if spread_a < spread_b else -1

    # 4. Sector Momentum Alignment
    sec_dir_a = a.sector_pct if a.direction == "BUY" else -a.sector_pct
    sec_dir_b = b.sector_pct if b.direction == "BUY" else -b.sector_pct
    sec_diff = sec_dir_a - sec_dir_b
    if abs(sec_diff) > 0.05:
        return 1 if sec_diff > 0 else -1

    # 5. VWAP Momentum Breakout
    if a.vwap > 0 and b.vwap > 0:
        vwap_m_a = (a.entry - a.vwap) / a.vwap if a.direction == "BUY" else (a.vwap - a.entry) / a.vwap
        vwap_m_b = (b.entry - b.vwap) / b.vwap if b.direction == "BUY" else (b.vwap - b.entry) / b.vwap
        vwap_diff = vwap_m_a - vwap_m_b
        if abs(vwap_diff) > 0.0001:
            return 1 if vwap_diff > 0 else -1

    # 6. Deterministic Alphabetical Tiebreaker (earlier in alphabet wins)
    if a.ticker != b.ticker:
        return 1 if a.ticker < b.ticker else -1

    return 0


class ConsensusTieBreaker:
    @staticmethod
    def select_best_candidate(
        candidates: List[Any],
        score_index: int = 12,
        historical_id_sign: int = 1,
        quality_boost_evaluator: Optional[Any] = None
    ) -> Any:
        """
        Selects the single best candidate from a batch of candidates for a scan interval.
        Strictly preserves historical selections for trades prior to HISTORICAL_TIEBREAKER_CUTOFF.
        Applies economic tiebreaker for all future trades.
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        # Check if the batch is prior to HISTORICAL_TIEBREAKER_CUTOFF
        m0 = CandidateMetrics(candidates[0], score_index=score_index)
        is_historical = bool(m0.timestamp and m0.timestamp < HISTORICAL_TIEBREAKER_CUTOFF)

        if is_historical:
            # Historical selection logic: byte-for-byte exact replay
            if quality_boost_evaluator is not None and m0.timestamp >= "2026-09-24 12:15:00":
                def _hist_timed_key(cand):
                    m = CandidateMetrics(cand, score_index=score_index)
                    try:
                        eval_res = quality_boost_evaluator(m.ticker, m.direction, m.entry)
                        qb = float(eval_res.get("quality_boost") or 0.0)
                        passed = bool(eval_res.get("passed", True))
                        eff_score = m.score + (qb * 0.5)
                        return (eff_score if passed else -999.0, historical_id_sign * m.id)
                    except Exception:
                        return (m.score, historical_id_sign * m.id)
                return max(candidates, key=_hist_timed_key)
            else:
                return max(
                    candidates,
                    key=lambda c: (
                        CandidateMetrics(c, score_index=score_index).score,
                        historical_id_sign * CandidateMetrics(c, score_index=score_index).id
                    )
                )

        # Future selection: apply economic tiebreaker
        # If quality boost evaluator is provided (e.g. timed anchor scans):
        if quality_boost_evaluator is not None:
            # Apply quality boost to effective score
            enriched_candidates = []
            for c in candidates:
                m = CandidateMetrics(c, score_index=score_index)
                try:
                    eval_res = quality_boost_evaluator(m.ticker, m.direction, m.entry)
                    qb = float(eval_res.get("quality_boost") or 0.0)
                    passed = bool(eval_res.get("passed", True))
                    m.score = (m.score + (qb * 0.5)) if passed else -999.0
                except Exception:
                    pass
                enriched_candidates.append((m, c))

            def _compare_enriched(pair_a, pair_b):
                return compare_candidates_economically(pair_a[0], pair_b[0])

            best_pair = max(enriched_candidates, key=cmp_to_key(_compare_enriched))
            return best_pair[1]

        def _compare_raw(cand_a, cand_b):
            ma = CandidateMetrics(cand_a, score_index=score_index)
            mb = CandidateMetrics(cand_b, score_index=score_index)
            return compare_candidates_economically(ma, mb)

        return max(candidates, key=cmp_to_key(_compare_raw))

