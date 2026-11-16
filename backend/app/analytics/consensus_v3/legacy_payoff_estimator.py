"""
Layer 3 — Strictly Independent Legacy Payoff & Risk Estimator (`consensus_v3/legacy_payoff_estimator.py`)
=========================================================================================================
Contract:
  - Independently estimates the SAME opportunity's expected net payoff (`bps`), win probability (`P_win`),
    and adverse excursion (`MAE_est_bps`) using ONLY causal stock/regime features.
  - STRICT INDEPENDENCE INVARIANT: Zero Qlib scores, ranks, or outputs are ever passed into Legacy!
  - INCREMENTAL VALUE GATE:
      * Book 2 (`15m` Sniffer): `ACTIVE` (demonstrated `+₹24,561` OOS net lift over Qlib alone in 58d WF).
      * Book 1 (`20d` Positional): `SHADOW_ONLY` (demoted per "retain only demonstrated incremental value"
        because `Qlib 20d + Shield` beat `Qlib + Legacy` `+15.47%` vs `-0.16%` in the 459d 2025-2026 Lockbox).
"""

import json
import os
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.consensus_v3.baseline_engine import load_fno_eligible_set
from app.analytics.consensus_v3.math_and_costs import (
    RegularizedBoostedStumpPAV,
    compute_nse_mis_charges,
    round_nse_eq_tick,
    sha256_json,
)

MODEL_DIR_V3 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "models",
    "consensus_v3",
)

LEGACY_INDEPENDENT_FEATURES = [
    "TOD_RVOL",
    "ADR_15D_PCT",
    "CLV_ALIGNED",
    "COUNTER_VWAP_ATR",
    "COUNTER_GAP_PCT",
    "ALIGNED_MKT_RET",
    "ALIGNED_MKT_BREADTH_CENTERED",
]


def _sim_passive_train_payoff_bps(
    blist: List[Tuple[str, float, float, float, float, float]],
    entry_idx: int,
    sdir: str,
    lim_px: float,
    sl_px: float,
    liq_1pct: float,
) -> Optional[Tuple[float, float]]:
    """Simulates post-only limit (`3 bps` trade-through) to `15:15 IST` strictly inside completed train sessions."""
    if entry_idx >= len(blist) or blist[entry_idx][0] >= "15:00":
        return None
    is_buy = sdir == "BUY"
    _, b0_o, b0_h, b0_l, _, _ = blist[entry_idx]
    if is_buy:
        if b0_o <= lim_px or b0_l > lim_px * 0.9997:
            return None
    else:
        if b0_o >= lim_px or b0_h < lim_px * 1.0003:
            return None

    epx = lim_px
    xpx = epx
    mae_pct = 0.0
    for k in range(entry_idx, len(blist)):
        hm, bo, bh, bl, bc, _ = blist[k]
        if hm >= "15:15":
            xpx = bo
            adv = ((epx - bo) / epx * 100.0) if is_buy else ((bo - epx) / epx * 100.0)
            mae_pct = max(mae_pct, adv)
            break
        if k > entry_idx:
            if is_buy and bo <= sl_px:
                xpx = bo
                mae_pct = max(mae_pct, (epx - bo) / epx * 100.0)
                break
            if (not is_buy) and bo >= sl_px:
                xpx = bo
                mae_pct = max(mae_pct, (bo - epx) / epx * 100.0)
                break
        if is_buy and bl <= sl_px:
            xpx = sl_px
            mae_pct = max(mae_pct, (epx - sl_px) / epx * 100.0)
            break
        if (not is_buy) and bh >= sl_px:
            xpx = sl_px
            mae_pct = max(mae_pct, (sl_px - epx) / epx * 100.0)
            break
        adv = ((epx - bl) / epx * 100.0) if is_buy else ((bh - epx) / epx * 100.0)
        mae_pct = max(mae_pct, adv)
    else:
        xpx = blist[-1][4]

    gross_pct = ((xpx - epx) / epx * 100.0) if is_buy else ((epx - xpx) / epx * 100.0)
    sh = max(1, int(min(100000.0, liq_1pct) / epx))
    cost_bps = compute_nse_mis_charges(epx, xpx, sh, sdir, True, True, 1.0)["total_cost_bps"]
    return (gross_pct * 100.0 - cost_bps), (mae_pct * 100.0)


class LegacyPayoffEstimator:
    """
    100% Independent Causal Payoff & Risk Estimator (Regularized Boosted Stumps + PAV Calibrator).
    """

    def __init__(self, model_dir: Optional[str] = None):
        self.model_dir = model_dir or MODEL_DIR_V3
        os.makedirs(self.model_dir, exist_ok=True)
        self._book2_artifact: Optional[Dict[str, Any]] = None

    def _book2_path(self) -> str:
        return os.path.join(self.model_dir, "legacy_book2_15m_independent_manifest.json")

    def ensure_ready(self, conn: sqlite3.Connection, max_train_date: Optional[str] = None) -> Dict[str, Any]:
        p = self._book2_path()
        if max_train_date is None and self._book2_artifact:
            return self._book2_artifact
        if max_train_date is None and os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    self._book2_artifact = json.load(f)
                return self._book2_artifact
            except Exception:
                pass

        art = self.fit_book2_independent_causal(conn, max_train_date=max_train_date)
        if max_train_date is None:
            self._book2_artifact = art
            with open(p, "w", encoding="utf-8") as f:
                json.dump(art, f, indent=2)
        return art

    def fit_book2_independent_causal(self, conn: sqlite3.Connection, max_train_date: Optional[str] = None) -> Dict[str, Any]:
        fno_set = load_fno_eligible_set(conn)
        date_clause = "AND substr(date,1,10) <= ?" if max_train_date else ""
        params: Tuple = (max_train_date,) if max_train_date else ()

        raw_by_t: Dict[str, List[Tuple[str, str, float, float, float, float, float]]] = defaultdict(list)
        for sym, dt_s, bo, bh, bl, bc, bv in conn.execute(
            f"SELECT ticker, date, open, high, low, close, volume FROM ohlcv WHERE timeframe='15m' {date_clause} ORDER BY ticker, date ASC",
            params,
        ):
            if sym not in fno_set:
                continue
            bo, bh, bl, bc, bv = float(bo), float(bh), float(bl), float(bc), float(bv or 0.0)
            if (bo == 100.0 and bh == 102.0 and bl == 99.0 and bc == 101.5 and bv == 50000.0) or bo <= 0 or bc <= 0 or bh < bl:
                continue
            s = str(dt_s)[:19]
            raw_by_t[sym].append((s[:10], s[11:16], bo, bh, bl, bc, bv))

        bars_td: Dict[Tuple[str, str], List[Tuple[str, float, float, float, float, float]]] = {}
        day_tc: Dict[str, int] = defaultdict(int)
        for sym, rows in raw_by_t.items():
            prev_c = None
            by_d: Dict[str, List[Tuple[str, float, float, float, float, float]]] = defaultdict(list)
            for d_s, hm, bo, bh, bl, bc, bv in rows:
                if prev_c is not None:
                    lim = 0.25 if not by_d[d_s] else 0.18
                    if not ((1.0 - lim) * prev_c <= bo <= (1.0 + lim) * prev_c and (1.0 - lim) * prev_c <= bc <= (1.0 + lim) * prev_c):
                        continue
                prev_c = bc
                by_d[d_s].append((hm, bo, bh, bl, bc, bv))
            for d_s, bls in by_d.items():
                if len(bls) >= 15:
                    bars_td[(sym, d_s)] = bls
                    day_tc[d_s] += 1

        vdays = sorted(d for d, c in day_tc.items() if c >= 80)
        if max_train_date is None and len(vdays) > 20:
            vdays = vdays[:-1]

        dstats: Dict[Tuple[str, int], Dict[str, Any]] = {}
        syms_on_d: Dict[int, List[str]] = defaultdict(list)
        for di, d_s in enumerate(vdays):
            for sym in fno_set:
                bls = bars_td.get((sym, d_s))
                if not bls:
                    continue
                syms_on_d[di].append(sym)
                do = bls[0][1]
                dh, dl = max(x[2] for x in bls), min(x[3] for x in bls)
                cv, tt, cvm, btm = 0.0, 0.0, {}, {}
                for b in bls:
                    cv += b[5]
                    bt = b[4] * b[5]
                    tt += bt
                    cvm[b[0]] = cv
                    btm[b[0]] = bt
                dstats[(sym, di)] = {"high": dh, "low": dl, "close": bls[-1][4], "rng": (dh - dl) / do * 100.0, "turn": tt, "cvm": cvm, "btm": btm}

        eval_hms = {"09:30", "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30", "11:45", "12:00", "12:15", "12:30"}
        Xl: List[List[float]] = []
        yl: List[int] = []
        win_bps, loss_bps, mae_bps_list = [], [], []

        for di in range(15, len(vdays)):
            d_s = vdays[di]
            hm_rets: Dict[str, List[float]] = defaultdict(list)
            for sym in syms_on_d[di]:
                bls = bars_td[(sym, d_s)]
                do = bls[0][1]
                for b in bls:
                    hm_rets[b[0]].append((b[4] - do) / do * 100.0)
            pit_mkt = {hm: (sum(vs) / len(vs), sum(1 for v in vs if v > 0) / len(vs)) for hm, vs in hm_rets.items() if vs}

            for sym in syms_on_d[di]:
                h15 = [dstats[(sym, j)] for j in range(di - 15, di) if (sym, j) in dstats]
                if len(h15) < 8:
                    continue
                pdh, pdl, pdc = h15[-1]["high"], h15[-1]["low"], h15[-1]["close"]
                adr = sum(x["rng"] for x in h15) / len(h15)
                med_turn = statistics.median(x["turn"] for x in h15)
                if pdc < 50.0 or adr < 1.2 or med_turn < 1e8:
                    continue
                bls = bars_td[(sym, d_s)]
                do = bls[0][1]
                atr_px = pdc * adr / 100.0
                gap_pct = (do - pdc) / pdc * 100.0
                cv, cpv, sh_h, sl_l = 0.0, 0.0, -1.0, 1e18
                for bi in range(len(bls) - 2):
                    hm, bo, bh, bl, bc, bv = bls[bi]
                    cv += bv
                    cpv += ((bh + bl + bc) / 3.0) * max(1.0, bv)
                    sh_h = max(sh_h, bh)
                    sl_l = min(sl_l, bl)
                    if hm not in eval_hms or bls[bi + 1][0] >= "15:00":
                        continue
                    hvs = [x["cvm"].get(hm, 0.0) for x in h15 if x["cvm"].get(hm, 0.0) > 0]
                    if len(hvs) < 4:
                        continue
                    rvol = cv / max(1.0, statistics.median(hvs))
                    vwap = cpv / max(1.0, cv)
                    vwap_atr = (bc - vwap) / max(1e-6, atr_px)
                    rng = bh - bl
                    clv = ((bc - bl) / rng) if rng > 1e-9 else 0.50

                    sdir = None
                    if (sh_h >= pdh * 0.999 or vwap_atr >= 0.45) and bc < bo and clv <= 0.40 and rvol >= 1.2:
                        sdir, dsgn, clv_al = "SELL", -1.0, 1.0 - clv
                    elif (sl_l <= pdl * 1.001 or vwap_atr <= -0.45) and bc > bo and clv >= 0.60 and rvol >= 1.2:
                        sdir, dsgn, clv_al = "BUY", 1.0, clv
                    if not sdir:
                        continue

                    lim_px = round_nse_eq_tick(bc - dsgn * 0.10 * atr_px)
                    sl_px = max(sh_h * 1.0015, lim_px + 0.55 * atr_px) if sdir == "SELL" else min(sl_l * 0.9985, lim_px - 0.55 * atr_px)
                    nhm = bls[bi + 1][0]
                    med_bt = statistics.median(x["btm"].get(nhm, x["turn"] / 25.0) for x in h15)
                    out = _sim_passive_train_payoff_bps(bls, bi + 1, sdir, lim_px, sl_px, 0.01 * med_bt)
                    if out is None:
                        continue
                    net_bps, mae_bps = out
                    mr, madv = pit_mkt.get(hm, (0.0, 0.50))
                    # Strictly 7 causal stock/regime features — ZERO Qlib inputs!
                    Xl.append([rvol, adr, clv_al, -dsgn * vwap_atr, -dsgn * gap_pct, dsgn * mr, dsgn * (madv - 0.50)])
                    mae_bps_list.append(mae_bps)
                    if net_bps > 0.0:
                        yl.append(1)
                        win_bps.append(net_bps)
                    else:
                        yl.append(0)
                        loss_bps.append(net_bps)

        clf = RegularizedBoostedStumpPAV(n_trees=8, lr=0.10, min_leaf=30)
        clf.fit(Xl, yl)
        m_win = (sum(win_bps) / len(win_bps)) if win_bps else 75.0
        m_loss = (sum(loss_bps) / len(loss_bps)) if loss_bps else -60.0
        m_mae = (sum(mae_bps_list) / len(mae_bps_list)) if mae_bps_list else 48.0

        core = {
            "model_id": "legacy_v3_book2_15m_independent_stump_pav",
            "declared_horizon": "ENTRY_TO_1515_IST_SQUAREOFF",
            "qlib_inputs_used": 0,
            "incremental_value_role_book2": "ACTIVE_RETAINED",
            "incremental_value_role_book1": "SHADOW_ONLY_DEMOTED",
            "features": LEGACY_INDEPENDENT_FEATURES,
            "mean_win_net_bps": round(m_win, 3),
            "mean_loss_net_bps": round(m_loss, 3),
            "mean_mae_bps": round(m_mae, 3),
            "train_samples": len(Xl),
            "max_train_date": vdays[-1] if vdays else (max_train_date or ""),
            "classifier": clf.to_dict(),
        }
        core["artifact_sha256"] = sha256_json(core)
        core["trained_at"] = datetime.now().isoformat()
        return core

    def score_book2_opportunity(self, item: Dict[str, Any], direction: str, artifact: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        art = artifact or self._book2_artifact
        if not art:
            return {"p_win": 0.50, "expected_net_bps": 0.0, "expected_mae_bps": 50.0}

        clf = RegularizedBoostedStumpPAV.from_dict(art["classifier"])
        dsgn = 1.0 if direction == "BUY" else -1.0
        clv_al = float(item["clv"]) if direction == "BUY" else (1.0 - float(item["clv"]))
        fv = [
            float(item["rvol"]),
            float(item["adr_pct"]),
            clv_al,
            -dsgn * float(item["vwap_atr"]),
            -dsgn * float(item["gap_pct"]),
            dsgn * float(item.get("mkt_ret_pct", 0.0)),
            dsgn * (float(item.get("mkt_breadth", 0.50)) - 0.50),
        ]
        p_win = clf.predict_proba(fv)
        m_win = float(art.get("mean_win_net_bps", 75.0))
        m_loss = float(art.get("mean_loss_net_bps", -60.0))
        e_net = p_win * m_win + (1.0 - p_win) * m_loss
        return {
            "p_win": round(p_win, 4),
            "expected_net_bps": round(e_net, 2),
            "expected_mae_bps": round(float(art.get("mean_mae_bps", 48.0)), 2),
        }

    @staticmethod
    def score_book1_shadow_telemetry(cand: Dict[str, Any]) -> Dict[str, Any]:
        """
        Book 1 Shadow-Only Legacy Payoff Telemetry (never gates Book 1 orders because Qlib+Shield
        outperformed Qlib+Legacy `+15.47%` vs `-0.16%` in the 459d 2025-2026 Lockbox).
        """
        rsi = float(cand.get("rsi", 50.0))
        sma_ok = bool(cand.get("sma_alignment_ok", False))
        sh = float(cand.get("sharpe_12_1", 0.0))
        p_shadow = min(0.75, max(0.25, 0.45 + 0.06 * sh + (0.05 if (sma_ok and 45.0 <= rsi <= 72.0) else -0.05)))
        return {
            "legacy_role": "SHADOW_ONLY",
            "legacy_shadow_p_win": round(p_shadow, 4),
            "legacy_shadow_note": "Recorded for incremental-value monitoring only; excluded from Book 1 execution gate.",
        }
