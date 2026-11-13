"""
S2_05 Causal Walk-Forward Meta-Learner & Point-in-Time Feature Extractor.

Implements the exact Hunt V7 Fold-18 (`test_month = 2026-11`, trained strictly on
`2026-05 .. 2026-10` with `2026-10-30` purged) `CausalWalkForwardMetaV7` stacked model:
  - Sample-weighted L2 Logistic Regression (45%)
  - Regularized Shallow Newton GBDT Stumps (35%)
  - Core-only KNN-25 Regime Memory (20%)
  - Out-of-Sample PAV Isotonic Conformal Calibrator (`0.60 * raw + 0.40 * pav_cal`)

Zero modification to `champion_ensemble.pkl` or any `.pkl` model artifact.
"""

import hashlib
import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_WEIGHTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s205_meta_weights.json")


def compute_adx_series_causal(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> Tuple[List[float], List[float], List[float]]:
    """Exact Wilder ADX(14), +DI(14), -DI(14) causal series."""
    n = len(closes)
    adx_out = [0.0] * n
    pdi_out = [0.0] * n
    mdi_out = [0.0] * n
    if n < 2 * period:
        return adx_out, pdi_out, mdi_out

    tr = [0.0] * n
    dmp = [0.0] * n
    dmm = [0.0] * n
    for i in range(1, n):
        h, l = highs[i], lows[i]
        hp, lp, cp = highs[i - 1], lows[i - 1], closes[i - 1]
        tr[i] = max(h - l, abs(h - cp), abs(l - cp))
        up = h - hp
        dn = lp - l
        dmp[i] = up if (up > dn and up > 0) else 0.0
        dmm[i] = dn if (dn > up and dn > 0) else 0.0

    trs = sum(tr[1 : period + 1])
    ps = sum(dmp[1 : period + 1])
    ms = sum(dmm[1 : period + 1])
    dxs = []
    p_di = (100.0 * ps / trs) if trs > 1e-9 else 0.0
    m_di = (100.0 * ms / trs) if trs > 1e-9 else 0.0
    pdi_out[period] = round(p_di, 2)
    mdi_out[period] = round(m_di, 2)
    dxs.append((100.0 * abs(p_di - m_di) / (p_di + m_di)) if (p_di + m_di) > 1e-9 else 0.0)

    adx_val = 0.0
    for i in range(period + 1, n):
        trs = trs - (trs / period) + tr[i]
        ps = ps - (ps / period) + dmp[i]
        ms = ms - (ms / period) + dmm[i]
        p_di = (100.0 * ps / trs) if trs > 1e-9 else 0.0
        m_di = (100.0 * ms / trs) if trs > 1e-9 else 0.0
        dx = (100.0 * abs(p_di - m_di) / (p_di + m_di)) if (p_di + m_di) > 1e-9 else 0.0
        dxs.append(dx)
        pdi_out[i] = round(p_di, 2)
        mdi_out[i] = round(m_di, 2)
        if len(dxs) == period:
            adx_val = sum(dxs) / period
            adx_out[i] = round(adx_val, 2)
        elif len(dxs) > period:
            adx_val = (adx_val * (period - 1) + dx) / period
            adx_out[i] = round(adx_val, 2)
    return adx_out, pdi_out, mdi_out


def compute_er_series_causal(closes: List[float], period: int = 20) -> List[float]:
    """Exact Kaufman Efficiency Ratio ER(20) causal series."""
    n = len(closes)
    out = [0.0] * n
    for i in range(period, n):
        net = abs(closes[i] - closes[i - period])
        tot = 0.0
        for k in range(i - period + 1, i + 1):
            tot += abs(closes[k] - closes[k - 1])
        out[i] = round(net / tot, 3) if tot > 1e-9 else 0.0
    return out


def compute_atrr_series_causal(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    sp: int = 5,
    lp: int = 20,
) -> List[float]:
    """Exact ATR(5)/ATR(20) volatility expansion ratio causal series."""
    n = len(closes)
    out = [1.0] * n
    if n <= lp:
        return out
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    for i in range(lp, n):
        as_ = sum(trs[i - sp + 1 : i + 1]) / sp
        al_ = sum(trs[i - lp + 1 : i + 1]) / lp
        out[i] = round(as_ / al_, 3) if al_ > 1e-9 else 1.0
    return out


def compute_rsi14_series_causal(closes: List[float]) -> List[float]:
    """Exact Wilder RSI(14) causal series."""
    n = len(closes)
    rsi14 = [50.0] * n
    avg_g = avg_l = 0.0
    for i in range(1, n):
        chg = closes[i] - closes[i - 1]
        g = max(0.0, chg)
        lv = max(0.0, -chg)
        if i <= 14:
            avg_g += g / 14.0
            avg_l += lv / 14.0
            if i == 14:
                rs = avg_g / max(1e-9, avg_l)
                rsi14[i] = 100.0 - (100.0 / (1.0 + rs))
        else:
            avg_g = (avg_g * 13.0 + g) / 14.0
            avg_l = (avg_l * 13.0 + lv) / 14.0
            rs = avg_g / max(1e-9, avg_l)
            rsi14[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi14


def extract_causal_features_s205(
    ctx: Dict[str, Any],
    i: int,
    primary_eso: Tuple[str, float, str],
    is_credit: bool,
    is_bull: bool,
    ecode: int,
    lot_sz: int,
) -> List[float]:
    """
    Exact 30-element point-in-time causal feature vector at close of bar `i`.
    Bit-for-bit identical to `extract_causal_features_v7`.
    """
    bts = ctx["ts"][i]
    hm = int(bts[11:13]) * 60 + int(bts[14:16])
    spot = ctx["c"][i]
    tpb = ctx["tpb"][i]
    ds = 1.0 if is_bull else -1.0
    step = float(ctx.get("strike_step", 50.0))

    dte = ctx["front_dte"] if primary_eso[0] == ctx["front_exp"] else ctx["next_dte"]
    s_px = ctx["strad_front_px"][i] if primary_eso[0] == ctx["front_exp"] else ctx["strad_next_px"][i]
    s_vw = ctx["strad_front_vwap"][i] if primary_eso[0] == ctx["front_exp"] else ctx["strad_next_vwap"][i]
    strad_r = (s_px / s_vw) if s_vw > 0 else 1.0

    vix_now = ctx["vix_map"].get(bts, ctx["vix_open"])
    vix_chg = ((vix_now - ctx["vix_open"]) / ctx["vix_open"] * 100.0) if ctx["vix_open"] > 0 else 0.0

    day_h = max(ctx["h"][: i + 1])
    day_l = min(ctx["l"][: i + 1])
    day_rng = ((day_h - day_l) / spot * 100.0) if spot > 0 else 0.3
    escaped_pd = 1.0 if (ctx["pd_h"] > 0 and (spot > ctx["pd_h"] or spot < ctx["pd_l"])) else 0.0

    ost = ctx["opt_stats"].get(primary_eso)
    ob = ctx["day_eso"].get(primary_eso, {}).get(bts)
    opt_px = ob[3] if ob else 100.0
    if ost and ost[0][i] > 0:
        opt_vw_r = opt_px / ost[0][i]
        oi_r_lag3 = min(2.5, ost[5][i])
        dd_from_pk = (opt_px / max(1.0, ost[2][i])) - 1.0
    else:
        opt_vw_r = 1.0
        oi_r_lag3 = 1.0
        dd_from_pk = 0.0

    ot = primary_eso[2]
    moneyness = ((spot - primary_eso[1]) if ot == "CE" else (primary_eso[1] - spot)) / step
    notional_k = (opt_px * lot_sz) / 10000.0

    return [
        1.0 if is_credit else -1.0,                       # 0: Credit vs Debit
        1.0 if ecode == 1 else 0.0,                       # 1: OI Writer engine
        1.0 if ecode == 2 else 0.0,                       # 2: Straddle/Range engine
        1.0 if ecode == 3 else 0.0,                       # 3: Spike Exhaustion engine
        1.0 if ecode == 4 else 0.0,                       # 4: Breakout/Momentum engine
        (hm - 600.0) / 180.0,                             # 5: Normalized minute
        float(dte) / 6.0,                                 # 6: DTE
        1.0 if dte == 0 else 0.0,                         # 7: 0-DTE flag
        ctx["adx"][i] / 35.0,                             # 8: Wilder ADX14
        ((ctx["pdi"][i] - ctx["mdi"][i]) * ds) / 25.0,    # 9: Aligned DI spread
        ctx["er"][i],                                     # 10: Kaufman ER20
        ctx["atrr"][i] - 1.0,                             # 11: ATR(5)/ATR(20) - 1
        ((spot - tpb) * ds) / step,                       # 12: Aligned Spot - TPB
        ctx["vwap_z"][i] * ds,                            # 13: Aligned VWAP Z-score
        (ctx["rsi14"][i] - 50.0) * ds / 25.0,             # 14: Aligned Wilder RSI14
        day_rng,                                          # 15: Intraday range %
        escaped_pd,                                       # 16: Outside PDH/PDL
        ctx["cpr_width_pct"],                             # 17: Prior-day CPR width %
        vix_now / 16.0,                                   # 18: Live India VIX
        vix_chg / 3.0,                                    # 19: Intraday VIX % change
        ctx["ivp_20d"] / 100.0,                           # 20: 20d IVP
        strad_r - 1.0,                                    # 21: Straddle / Straddle_VWAP - 1
        ctx["hw"][i] * ds,                                # 22: Completed 15m Heavyweights
        (ctx["syn_basis"][i] * ds) / (0.3 * step),        # 23: Futures / Put-Call Parity Basis
        (ctx["oi_bull_diff"][i] * ds) * 5.0,              # 24: 3m-lagged Cross-Strike OI Diff
        opt_vw_r - 1.0,                                   # 25: Option / Option_VWAP - 1
        oi_r_lag3 - 1.0,                                  # 26: 3m-lagged Option OI buildup - 1
        dd_from_pk,                                       # 27: Option drawdown from session peak
        moneyness,                                        # 28: Moneyness in strike steps
        notional_k,                                       # 29: Rupee notional (10k)
    ]


class S205CausalMetaModel:
    """
    Deterministic runtime inference engine for the frozen Fold-18 (`2026-11`)
    `CausalWalkForwardMetaV7` stacked conformal meta-learner.
    """

    def __init__(self, weights_path: Optional[str] = None):
        path = weights_path or _WEIGHTS_PATH
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        expected_sha = str(payload.get("weights_sha256") or "")
        if expected_sha:
            core_str = json.dumps(
                {
                    k: payload[k]
                    for k in ("means", "stds", "w_lr", "b_lr", "base_lo", "trees", "knn_X", "knn_y", "knn_w", "iso_blocks")
                },
                sort_keys=True,
            )
            actual_sha = hashlib.sha256(core_str.encode("utf-8")).hexdigest()
            if actual_sha != expected_sha:
                raise RuntimeError(f"[S205MetaModel] SHA256 mismatch: expected {expected_sha}, got {actual_sha}")
            self.weights_sha256 = actual_sha
        else:
            self.weights_sha256 = "UNVERIFIED"

        self.candidate_id: str = str(payload.get("candidate_id") or "S2_05_V6B_ML_FIXED_SIM")
        self.fold_id: int = int(payload.get("fold_id") or 18)
        self.test_month: str = str(payload.get("test_month") or "2026-11")
        self.embargoed_train_end: str = str(payload.get("embargoed_train_end") or "2026-10-30")
        self.chosen_params: Dict[str, float] = dict(payload.get("chosen_params") or {"oi_ratio_min": 1.05, "opt_vwap_max": 0.94, "sl_scale": 1.0})
        self.p_min: float = float(payload.get("chosen_p_min") or 0.56)
        self.mode: str = str(payload.get("ml_mode") or "STACKED_GBDT")
        self.lr: float = float(payload.get("lr") or 0.10)
        self.k_nn: int = int(payload.get("k_nn") or 21)

        self.means: List[float] = [float(x) for x in payload["means"]]
        self.stds: List[float] = [float(x) for x in payload["stds"]]
        self.w_lr: List[float] = [float(x) for x in payload["w_lr"]]
        self.b_lr: float = float(payload["b_lr"])
        self.base_lo: float = float(payload["base_lo"])
        self.trees: List[Tuple[int, float, float, float]] = [
            (int(t[0]), float(t[1]), float(t[2]), float(t[3])) for t in payload["trees"]
        ]
        self.knn_X: List[List[float]] = [[float(v) for v in row] for row in payload["knn_X"]]
        self.knn_y: List[float] = [float(v) for v in payload["knn_y"]]
        self.knn_w: List[float] = [float(v) for v in payload["knn_w"]]
        self.iso_blocks: List[Tuple[float, float, float]] = [
            (float(b[0]), float(b[1]), float(b[2])) for b in payload["iso_blocks"]
        ]
        self.fitted: bool = True

    @staticmethod
    def _sig(z: float) -> float:
        if z >= 18.0:
            return 0.99999998
        if z <= -18.0:
            return 0.00000002
        return 1.0 / (1.0 + math.exp(-z))

    def explain_prediction(self, x: List[float]) -> Dict[str, Any]:
        """Computes calibrated probability + individual stacked learner components."""
        if not self.fitted or len(x) != len(self.means):
            return {
                "prob": 0.50,
                "p_min": self.p_min,
                "passed": False,
                "p_lr": 0.50,
                "p_gbdt": 0.50,
                "p_knn": 0.50,
                "raw_score": 0.50,
                "pav_cal": 0.50,
            }

        xs = [max(-4.0, min(4.0, (x[j] - self.means[j]) / self.stds[j])) for j in range(len(x))]
        p_lr = self._sig(self.b_lr + sum(self.w_lr[j] * xs[j] for j in range(len(xs))))

        p_knn = p_lr
        if self.knn_X:
            dists = []
            for idx_k, xk in enumerate(self.knn_X):
                d2 = 0.0
                for j in (0, 5, 6, 8, 10, 12, 18, 21, 23, 24, 25, 26):
                    diff = xs[j] - xk[j]
                    d2 += diff * diff
                dists.append((d2, self.knn_y[idx_k], self.knn_w[idx_k]))
            dists.sort(key=lambda z: z[0])
            top = dists[: min(self.k_nn, len(dists))]
            num = den = 0.0
            for d2, yk, wk in top:
                inv_d = wk / (1.0 + d2)
                num += inv_d * yk
                den += inv_d
            if den > 0:
                p_knn = num / den

        f_gb = self.base_lo
        for j1, t1, vL, vR in self.trees:
            f_gb += self.lr * (vL if xs[j1] <= t1 else vR)
        p_gb = self._sig(f_gb)

        if self.mode == "LR_ONLY":
            raw = p_lr
        elif self.mode == "LR_KNN" or not self.trees:
            raw = 0.65 * p_lr + 0.35 * p_knn
        else:
            raw = 0.45 * p_lr + 0.35 * p_gb + 0.20 * p_knn

        cal = raw
        if self.iso_blocks:
            cal = self.iso_blocks[-1][2]
            for _, hi_r, cp in self.iso_blocks:
                if raw <= hi_r:
                    cal = cp
                    break

        prob = 0.60 * raw + 0.40 * cal
        return {
            "prob": round(prob, 4),
            "p_min": round(self.p_min, 4),
            "passed": bool(prob >= self.p_min),
            "p_lr": round(p_lr, 4),
            "p_gbdt": round(p_gb, 4),
            "p_knn": round(p_knn, 4),
            "raw_score": round(raw, 4),
            "pav_cal": round(cal, 4),
        }

    def predict_proba(self, x: List[float]) -> float:
        return float(self.explain_prediction(x)["prob"])


_SINGLETON_META: Optional[S205CausalMetaModel] = None


def get_s205_meta_model() -> S205CausalMetaModel:
    global _SINGLETON_META
    if _SINGLETON_META is None:
        _SINGLETON_META = S205CausalMetaModel()
    return _SINGLETON_META

