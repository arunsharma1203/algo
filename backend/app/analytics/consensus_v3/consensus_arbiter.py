"""
Layers 4 & 6 — Consensus Arbiter & Explicit Exit Controller (`consensus_v3/consensus_arbiter.py`)
================================================================================================
Responsibilities:
  - Layer 4 (Consensus):
      * Compares expected NET payoff (`bps`) after exact statutory charges + realistic spread/slippage.
      * Measures true model disagreement (`|E_qlib_net_bps - E_legacy_net_bps|`).
      * Penalizes uncertainty (`Edge_bps = E_cons_net_bps - 0.20 * Disagreement_bps`).
      * Applies Macro Breadth/SMA50 Portfolio Exposure Shield (`Book 1`).
      * Explicitly ABSTAINS (`ABSTAIN_*`) whenever evidence is insufficient or engines disagree!
  - Layer 6 (Exits):
      * Book 1 (`20d` Scheduled Positional): Scheduled `20`-day rebalance + `Rank >= 78%` hysteresis
        + Macro Shield step-down + Corporate-Action Guard (`0` ML exit model initially).
      * Book 2 (`15m` Intraday Sniffer): Explicit structural stop (`max(SessionHigh * 1.0015, Limit + 0.55 * ATR15)`)
        + Mandatory `15:15:00 IST` NSE `MIS` square-off (`0` ML exit model initially).
"""

from typing import Any, Dict, List

from app.analytics.consensus_v3.legacy_payoff_estimator import LegacyPayoffEstimator
from app.analytics.consensus_v3.math_and_costs import (
    compute_nse_cnc_charges,
    compute_nse_mis_charges,
    round_nse_eq_tick,
)


class ExitController:
    """Explicit rule-based exit parameters & triggers (ML Exit Challenger locked behind Phase-2 gate)."""

    @staticmethod
    def build_book2_sniffer_exit_plan(item: Dict[str, Any], direction: str, limit_px: float) -> Dict[str, Any]:
        atr_px = float(item["atr_px"])
        if direction == "SELL":
            stop_raw = max(float(item["session_high"]) * 1.0015, limit_px + 0.55 * atr_px)
        else:
            stop_raw = min(float(item["session_low"]) * 0.9985, limit_px - 0.55 * atr_px)
        stop_px = round_nse_eq_tick(stop_raw)
        stop_dist_bps = round(abs(stop_px - limit_px) / max(1e-6, limit_px) * 10000.0, 1)
        return {
            "stop_loss_px": stop_px,
            "stop_distance_bps": stop_dist_bps,
            "scheduled_squareoff_hm": "15:15",
            "limit_tif_minutes": 15,
            "exit_rule_version": "EXPLICIT_STRUCTURAL_SL_PLUS_1515_SQUAREOFF_V1",
            "ml_exit_challenger_active": False,
        }

    @staticmethod
    def build_book1_rebalance_exit_plan() -> Dict[str, Any]:
        return {
            "scheduled_rebalance_trading_days": 20,
            "hysteresis_keep_rank_floor": 0.78,
            "regime_shield_enabled": True,
            "corporate_action_split_guard": True,
            "exit_rule_version": "SCHEDULED_20D_HYSTERESIS_SHIELD_V1",
            "ml_exit_challenger_active": False,
        }


class ConsensusArbiter:
    """Synthesizes Baseline, Independent Qlib, and Independent Legacy into actionable intents or explicit abstentions."""

    MIN_NET_EDGE_HURDLE_BPS = 8.0
    DISAGREEMENT_PENALTY_WEIGHT = 0.20

    @classmethod
    def arbitrate_book2_15m_sniffer(
        cls,
        scored_snapshot: Dict[str, Any],
        legacy_estimator: LegacyPayoffEstimator,
        legacy_artifact: Dict[str, Any],
        slot_cap_inr: float = 100000.0,
    ) -> Dict[str, Any]:
        items = scored_snapshot.get("items", [])
        is_window = bool(scored_snapshot.get("is_sniffer_window", False))

        approved: List[Dict[str, Any]] = []
        abstained: List[Dict[str, Any]] = []

        for it in items:
            trig = it.get("sniffer_trigger")
            if not trig:
                continue

            direction = "SELL" if trig == "PDH_FAILED_SWEEP_SELL" else "BUY"
            dsgn = 1.0 if direction == "BUY" else -1.0
            atr_px = float(it["atr_px"])
            limit_px = round_nse_eq_tick(float(it["close"]) - dsgn * 0.10 * atr_px)
            est_shares = max(1, int(min(slot_cap_inr, float(it["liq_1pct_bar_inr"])) // limit_px))
            cost_info = compute_nse_mis_charges(limit_px, limit_px, est_shares, direction, True, True, 1.0)
            est_cost_bps = cost_info["total_cost_bps"]

            # Qlib expected net payoff in trade direction (bps) over `-> 15:15 IST`
            e_qlib_gross_bps = (dsgn * float(it.get("qlib_stock_residual_pct", 0.0)) + 0.10 * float(it["adr_pct"])) * 100.0
            e_qlib_net_bps = round(e_qlib_gross_bps - est_cost_bps, 2)
            q_pct = float(it.get("qlib_pct_rank", 0.50))

            # 100% Independent Legacy payoff & risk estimate
            leg_est = legacy_estimator.score_book2_opportunity(it, direction, artifact=legacy_artifact)
            p_leg = leg_est["p_win"]
            e_leg_net_bps = leg_est["expected_net_bps"]

            disagreement_bps = round(abs(e_qlib_net_bps - e_leg_net_bps), 2)
            e_cons_net_bps = round(0.50 * e_qlib_net_bps + 0.50 * e_leg_net_bps, 2)
            edge_penalized_bps = round(e_cons_net_bps - cls.DISAGREEMENT_PENALTY_WEIGHT * disagreement_bps, 2)
            exit_plan = ExitController.build_book2_sniffer_exit_plan(it, direction, limit_px)
            rr_ratio = round(max(0.2, (e_cons_net_bps + est_cost_bps) / max(10.0, exit_plan["stop_distance_bps"])), 2)

            # Determine explicit decision / abstention reason
            decision = "APPROVED_POSTONLY_LIMIT"
            abstain_reason = None
            if not is_window:
                decision = "ABSTAIN"
                abstain_reason = "OUTSIDE_0930_1230_SNIFFER_WINDOW"
            elif direction == "BUY":
                # Pre-test verified: PDL Long fades lost net edge (-1.05% both-sides vs +0.66% short-only); abstain unless overridden
                decision = "ABSTAIN"
                abstain_reason = "ASYMMETRIC_LONG_SWEEP_ABSTAIN"
            elif e_qlib_net_bps <= 5.0 or q_pct > 0.55:
                decision = "ABSTAIN"
                abstain_reason = "QLIB_DISAGREES_OR_LOW_NET_PAYOFF"
            elif p_leg < 0.52 or e_leg_net_bps <= 5.0:
                decision = "ABSTAIN"
                abstain_reason = "LEGACY_LOW_WIN_PROB_ABSTAIN"
            elif edge_penalized_bps < cls.MIN_NET_EDGE_HURDLE_BPS:
                decision = "ABSTAIN"
                abstain_reason = f"BELOW_{int(cls.MIN_NET_EDGE_HURDLE_BPS)}BPS_DISAGREEMENT_ADJUSTED_HURDLE"

            record = {
                "book": "BOOK2_INTRADAY_MIS_SNIFFER",
                "strategy": "INTRADAY",
                "timeframe": "15m",
                "ticker": it["ticker"],
                "symbol": it["symbol"],
                "instrument_key": it["instrument_key"],
                "fno_eligible": True,
                "session_date": it["session_date"],
                "bar_dt": it["bar_dt"],
                "bar_hm": it["bar_hm"],
                "direction": direction,
                "trigger_type": trig,
                "signal_close_px": it["close"],
                "limit_entry_px": limit_px,
                "stop_loss_px": exit_plan["stop_loss_px"],
                "stop_distance_bps": exit_plan["stop_distance_bps"],
                "scheduled_exit_hm": exit_plan["scheduled_squareoff_hm"],
                "est_roundtrip_cost_bps": est_cost_bps,
                "qlib_pct_rank": q_pct,
                "e_qlib_net_bps": e_qlib_net_bps,
                "legacy_win_prob": p_leg,
                "e_legacy_net_bps": e_leg_net_bps,
                "disagreement_bps": disagreement_bps,
                "e_consensus_net_bps": e_cons_net_bps,
                "edge_penalized_bps": edge_penalized_bps,
                "risk_reward_ratio": rr_ratio,
                "decision": decision,
                "abstain_reason": abstain_reason,
                "liq_1pct_bar_inr": it["liq_1pct_bar_inr"],
                "telemetry": {
                    "rsi": it["rsi"],
                    "adx": it["adx"],
                    "atr_pct": it["adr_pct"],
                    "tod_rvol": it["rvol"],
                    "clv": it["clv"],
                    "vwap": it["vwap"],
                    "pdh": it["pdh"],
                    "pdl": it["pdl"],
                    "finbert_badge": "NEUTRAL_CAUSAL",
                },
                "exit_plan": exit_plan,
            }

            if decision == "APPROVED_POSTONLY_LIMIT":
                approved.append(record)
            else:
                abstained.append(record)

        approved.sort(key=lambda r: r["edge_penalized_bps"], reverse=True)
        abstained.sort(key=lambda r: r["edge_penalized_bps"], reverse=True)
        return {
            "as_of_ts": scored_snapshot.get("as_of_ts"),
            "session_date": scored_snapshot.get("session_date"),
            "bar_hm": scored_snapshot.get("bar_hm"),
            "is_sniffer_window": is_window,
            "market_context": scored_snapshot.get("market_context", {}),
            "universe_evaluated": len(items),
            "structural_triggers_found": len(approved) + len(abstained),
            "approved_signals": approved,
            "abstained_signals": abstained,
        }

    @classmethod
    def arbitrate_book1_20d_rebalance(
        cls,
        scored_snapshot: Dict[str, Any],
        slot_cap_inr: float = 50000.0,
    ) -> Dict[str, Any]:
        cands = scored_snapshot.get("candidates", [])
        shield = scored_snapshot.get("macro_shield", {})
        target_slots = int(shield.get("target_slots", 8))

        approved: List[Dict[str, Any]] = []
        abstained: List[Dict[str, Any]] = []
        exit_plan = ExitController.build_book1_rebalance_exit_plan()

        for c in cands:
            px = float(c["close"])
            sh = max(1, int(slot_cap_inr // px))
            cnc_cost_bps = compute_nse_cnc_charges(px, px, sh, True, 1.0)["total_cost_bps"]
            e_qlib_net_bps = round(float(c.get("qlib_expected_gross_bps", 0.0)) - cnc_cost_bps, 2)
            q_pct = float(c.get("qlib_pct_rank", 0.50))
            leg_shadow = LegacyPayoffEstimator.score_book1_shadow_telemetry(c)

            if target_slots == 0:
                decision, reason = "ABSTAIN", "MACRO_BEAR_CASH_SHIELD_ACTIVE"
            elif not c.get("sma_alignment_ok", False):
                decision, reason = "ABSTAIN", "BELOW_SMA50_OR_SMA200_TREND_FILTER"
            elif q_pct < 0.85 or e_qlib_net_bps <= 0.0:
                decision, reason = "ABSTAIN", "BELOW_TOP15PCT_QLIB_20D_RANK_OR_NEGATIVE_NET"
            else:
                decision, reason = "APPROVED_20D_REBALANCE_CANDIDATE", None

            rec = {
                "book": "BOOK1_SWING_CNC_CORE",
                "strategy": "SWING",
                "timeframe": "1d",
                "ticker": c["ticker"],
                "symbol": c["symbol"],
                "instrument_key": c["instrument_key"],
                "fno_eligible": True,
                "as_of_date": c["as_of_date"],
                "direction": "BUY",
                "close_px": px,
                "est_roundtrip_cost_bps": cnc_cost_bps,
                "baseline_mom_pct": c.get("baseline_mom_pct", 0.50),
                "qlib_pct_rank": q_pct,
                "e_qlib_net_bps": e_qlib_net_bps,
                "legacy_shadow": leg_shadow,
                "decision": decision,
                "abstain_reason": reason,
                "telemetry": {
                    "rsi": c["rsi"],
                    "adx": c["adx"],
                    "atr_pct": c["atr_pct"],
                    "mom_12_1_pct": c["mom_12_1_pct"],
                    "near_52w_ratio": c["near_52w_ratio"],
                },
                "exit_plan": exit_plan,
            }
            if decision == "APPROVED_20D_REBALANCE_CANDIDATE":
                approved.append(rec)
            else:
                abstained.append(rec)

        approved.sort(key=lambda r: r["qlib_pct_rank"], reverse=True)
        return {
            "as_of_date": scored_snapshot.get("as_of_date"),
            "macro_shield": shield,
            "universe_evaluated": len(cands),
            "approved_rebalance_leaders": approved[: max(target_slots, 12)],
            "abstained_count": len(abstained),
            "top_abstained_sample": abstained[:15],
        }
