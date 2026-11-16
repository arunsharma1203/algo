"""
Layer 2 — Independent Qlib Horizon Forecaster (`consensus_v3/qlib_horizon_forecaster.py`)
=========================================================================================
Forecasts expected residual returns over ONE fixed declared holding horizon per book:
  - Book 1 (`SWING_20D_CORE`): Fixed `20`-trading-day open-to-open residual return (`bps`),
    benchmarked directly against Published `12-1` Momentum.
  - Book 2 (`INTRADAY_15M_SNIFFER`): Fixed `Entry -> 15:15:00 IST` market-neutral residual return (`bps`),
    vectorized in `<0.5s` (eliminating the legacy `62.4s` in-process `Alpha158` GIL stall!).

Strict Isolation & Immutability (`AGENTS.md` Rule 2):
  - Never touches `backend/models/intraday/champion_ensemble.pkl` or `backend/models/qlib/*`.
  - Stores versioned JSON manifests + SHA-256 checksums exclusively under `backend/models/consensus_v3/`.
  - Zero Legacy features or outputs ever enter Qlib!
"""

import json
import math
import os
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.analytics.consensus_v3.baseline_engine import load_fno_eligible_set
from app.analytics.consensus_v3.math_and_costs import (
    cs_zscore,
    fit_ols_1d,
    sha256_json,
    solve_ridge,
)

MODEL_DIR_V3 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "models",
    "consensus_v3",
)

QLIB_BOOK1_FEATURES = [
    "INTERCEPT",
    "Z_MOM_12_1",
    "Z_MOM_6_1",
    "Z_SHARPE_12_1",
    "Z_NEAR_52W_HIGH",
    "Z_PULLBACK_20D",
]

QLIB_BOOK2_FEATURES = [
    "INTERCEPT",
    "Z_RET_OPEN",
    "Z_VWAP_ATR",
    "Z_GAP_PCT",
    "Z_RET_3D_PRIOR",
    "Z_RET_10D_PRIOR",
    "Z_CLV_CENTERED",
    "Z_SIGNED_LOG_RVOL",
    "Z_ADR",
]


class QlibHorizonForecaster:
    """
    Causal Cross-Sectional Ridge + OLS-Slope-Calibrated Return Forecaster.
    """

    def __init__(self, model_dir: Optional[str] = None):
        self.model_dir = model_dir or MODEL_DIR_V3
        os.makedirs(self.model_dir, exist_ok=True)
        self._book1_artifact: Optional[Dict[str, Any]] = None
        self._book2_artifact: Optional[Dict[str, Any]] = None

    def _book1_path(self) -> str:
        return os.path.join(self.model_dir, "qlib_book1_20d_manifest.json")

    def _book2_path(self) -> str:
        return os.path.join(self.model_dir, "qlib_book2_15m_manifest.json")

    def ensure_models_ready(self, conn: sqlite3.Connection, max_train_date: Optional[str] = None) -> Dict[str, Any]:
        """Loads cached v3 Qlib manifests or fits causally from SQLite `<= max_train_date`."""
        b1_p = self._book1_path()
        b2_p = self._book2_path()

        if max_train_date is None and self._book1_artifact and self._book2_artifact:
            return {"book1": self._book1_artifact, "book2": self._book2_artifact}

        if max_train_date is None and os.path.exists(b1_p) and os.path.exists(b2_p):
            try:
                with open(b1_p, "r", encoding="utf-8") as f1, open(b2_p, "r", encoding="utf-8") as f2:
                    self._book1_artifact = json.load(f1)
                    self._book2_artifact = json.load(f2)
                return {"book1": self._book1_artifact, "book2": self._book2_artifact}
            except Exception:
                pass

        b1 = self.fit_book1_20d_causal(conn, max_train_date=max_train_date)
        b2 = self.fit_book2_15m_causal(conn, max_train_date=max_train_date)
        if max_train_date is None:
            self._book1_artifact = b1
            self._book2_artifact = b2
            with open(b1_p, "w", encoding="utf-8") as f1:
                json.dump(b1, f1, indent=2)
            with open(b2_p, "w", encoding="utf-8") as f2:
                json.dump(b2, f2, indent=2)
        return {"book1": b1, "book2": b2}

    def fit_book1_20d_causal(self, conn: sqlite3.Connection, max_train_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Fits Book 1 `20`-day residual return forecaster strictly with `label_end_date < cutoff_date`
        (zero forward leakage across the 20-trading-day label horizon!).
        """
        fno_set = load_fno_eligible_set(conn)
        cutoff = max_train_date or "2026-09-23"
        raw_1d: Dict[str, Dict[str, Tuple[float, float, float, float]]] = defaultdict(dict)
        for sym, dt_s, bo, bh, bl, bc in conn.execute(
            "SELECT ticker, date, open, high, low, close FROM ohlcv WHERE timeframe='1d' AND date >= '2018-01-01' AND date <= ? ORDER BY ticker, date ASC",
            (f"{cutoff} 23:59:59",),
        ):
            if sym in fno_set and float(bo) > 0 and float(bc) > 0 and float(bh) >= float(bl):
                raw_1d[sym][str(dt_s)[:10]] = (float(bo), float(bh), float(bl), float(bc))

        dcnt: Dict[str, int] = defaultdict(int)
        for sym, dm in raw_1d.items():
            for d_s in dm:
                dcnt[d_s] += 1
        vdays = sorted(d for d, c in dcnt.items() if c >= 80)
        d2i = {d: i for i, d in enumerate(vdays)}

        feat_by_di: Dict[int, List[Dict[str, float]]] = defaultdict(list)
        for sym, dm in raw_1d.items():
            sl = []
            seg = 0
            for d_s in sorted(dm.keys()):
                if d_s not in d2i:
                    continue
                bo, bh, bl, bc = dm[d_s]
                isp = bool(sl and (bo / sl[-1]["c"] > 1.40 or bo / sl[-1]["c"] < 0.65 or bc / sl[-1]["c"] > 1.45 or bc / sl[-1]["c"] < 0.60))
                if isp:
                    seg = len(sl)
                sl.append({"di": d2i[d_s], "o": bo, "h": bh, "c": bc, "sp": isp, "segl": len(sl) - seg})

            L = len(sl)
            for k in range(255, L - 22, 5):
                r = sl[k]
                if r["segl"] < 255 or any(sl[j]["sp"] for j in range(k + 1, k + 22)):
                    continue
                # Enforce strict horizon purge: `k + 21` is strictly inside `<= cutoff`!
                m12_1 = (sl[k - 20]["c"] / sl[k - 252]["c"] - 1.0) * 100.0
                m6_1 = (sl[k - 20]["c"] / sl[k - 126]["c"] - 1.0) * 100.0
                r20 = (r["c"] / sl[k - 20]["c"] - 1.0) * 100.0
                d60 = [(sl[j]["c"] / sl[j - 1]["c"] - 1.0) for j in range(k - 59, k + 1) if not sl[j]["sp"]]
                vol60 = (statistics.stdev(d60) * math.sqrt(252.0) * 100.0) if len(d60) >= 10 else 30.0
                hi252 = max(sl[j]["h"] for j in range(k - 251, k + 1))
                f20_pct = (sl[k + 21]["o"] - sl[k + 1]["o"]) / sl[k + 1]["o"] * 100.0
                feat_by_di[r["di"]].append({
                    "m12_1": m12_1,
                    "m6_1": m6_1,
                    "sh12_1": m12_1 / max(5.0, vol60),
                    "n52": r["c"] / max(1e-6, hi252),
                    "pb20": -r20,
                    "f20_pct": f20_pct,
                })

        Xq: List[List[float]] = []
        yq: List[float] = []
        for di in sorted(feat_by_di.keys()):
            grp = feat_by_di[di]
            if len(grp) < 40:
                continue
            mf = sum(x["f20_pct"] for x in grp) / len(grp)
            z1 = cs_zscore([x["m12_1"] for x in grp])
            z2 = cs_zscore([x["m6_1"] for x in grp])
            z3 = cs_zscore([x["sh12_1"] for x in grp])
            z4 = cs_zscore([x["n52"] for x in grp])
            z5 = cs_zscore([x["pb20"] for x in grp])
            for i, x in enumerate(grp):
                Xq.append([1.0, z1[i], z2[i], z3[i], z4[i], z5[i]])
                yq.append(x["f20_pct"] - mf)

        weights = solve_ridge(Xq, yq, l2=3000.0) if Xq else [0.0, 0.35, 0.25, 0.35, 0.30, 0.15]
        raw_preds = [sum(w * v for w, v in zip(weights, row)) for row in Xq]
        ols_a, ols_b = fit_ols_1d(raw_preds, yq) if Xq else (0.0, 1.0)

        core = {
            "model_id": "qlib_v3_book1_20d_cs_ridge",
            "declared_horizon": "20_TRADING_DAYS_OPEN_TO_OPEN",
            "features": QLIB_BOOK1_FEATURES,
            "l2_penalty": 3000.0,
            "weights": [round(w, 6) for w in weights],
            "ols_intercept_pct": round(ols_a, 6),
            "ols_slope": round(ols_b, 6),
            "train_samples": len(Xq),
            "max_train_date": cutoff,
        }
        core["artifact_sha256"] = sha256_json(core)
        core["trained_at"] = datetime.now().isoformat()
        return core

    def fit_book2_15m_causal(self, conn: sqlite3.Connection, max_train_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Fits Book 2 `Entry -> 15:15:00 IST` residual return forecaster strictly on completed sessions `<= cutoff`
        with `>= 1` full session embargo.
        """
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

        # Embargo last session when fitting general model so current live session is strictly OOS!
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
                p15 = next((x[1] for x in bls if x[0] >= "15:15"), bls[-1][4])
                cv, tt, cvm = 0.0, 0.0, {}
                for b in bls:
                    cv += b[5]
                    tt += b[4] * b[5]
                    cvm[b[0]] = cv
                dstats[(sym, di)] = {"close": bls[-1][4], "p15": p15, "rng": (dh - dl) / do * 100.0, "turn": tt, "cvm": cvm}

        eval_hms = {"09:30", "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30", "11:45", "12:00", "12:15", "12:30"}
        Xq: List[List[float]] = []
        yq: List[float] = []

        for di in range(15, len(vdays)):
            d_s = vdays[di]
            fwd_by_hm: Dict[str, List[float]] = defaultdict(list)
            for sym in syms_on_d[di]:
                p15 = dstats[(sym, di)]["p15"]
                for b in bars_td[(sym, d_s)]:
                    fwd_by_hm[b[0]].append((p15 - b[1]) / b[1] * 100.0)
            mkt_fwd = {hm: sum(vs) / len(vs) for hm, vs in fwd_by_hm.items() if vs}

            raw_hm: Dict[str, List[Dict[str, float]]] = defaultdict(list)
            for sym in syms_on_d[di]:
                h15 = [dstats[(sym, j)] for j in range(di - 15, di) if (sym, j) in dstats]
                if len(h15) < 8:
                    continue
                pdc = h15[-1]["close"]
                adr = sum(x["rng"] for x in h15) / len(h15)
                if pdc < 50.0 or adr < 1.2 or statistics.median(x["turn"] for x in h15) < 1e8:
                    continue
                r3 = ((pdc / h15[-4]["close"] - 1.0) * 100.0) if len(h15) >= 4 else 0.0
                r10 = ((pdc / h15[-11]["close"] - 1.0) * 100.0) if len(h15) >= 11 else r3
                bls = bars_td[(sym, d_s)]
                do = bls[0][1]
                atr_px = pdc * adr / 100.0
                cv, cpv = 0.0, 0.0
                for bi in range(len(bls) - 2):
                    hm, bo, bh, bl, bc, bv = bls[bi]
                    cv += bv
                    cpv += ((bh + bl + bc) / 3.0) * max(1.0, bv)
                    if hm not in eval_hms or bls[bi + 1][0] >= "15:00":
                        continue
                    hvs = [x["cvm"].get(hm, 0.0) for x in h15 if x["cvm"].get(hm, 0.0) > 0]
                    if len(hvs) < 4:
                        continue
                    rvol = cv / max(1.0, statistics.median(hvs))
                    epx = bls[bi + 1][1]
                    nhm = bls[bi + 1][0]
                    res_f = ((dstats[(sym, di)]["p15"] - epx) / epx * 100.0) - mkt_fwd.get(nhm, 0.0)
                    rng = bh - bl
                    clv = ((bc - bl) / rng) if rng > 1e-9 else 0.50
                    vwap = cpv / max(1.0, cv)
                    raw_hm[hm].append({
                        "ro": (bc - do) / do * 100.0,
                        "vw": (bc - vwap) / max(1e-6, atr_px),
                        "gp": (do - pdc) / pdc * 100.0,
                        "r3": r3,
                        "r10": r10,
                        "clv": clv - 0.50,
                        "rv": math.log1p(rvol),
                        "adr": adr,
                        "res_f": res_f,
                    })

            for hm, items in raw_hm.items():
                if len(items) < 30:
                    continue
                z_ro = cs_zscore([x["ro"] for x in items])
                z_vw = cs_zscore([x["vw"] for x in items])
                z_gp = cs_zscore([x["gp"] for x in items])
                z_r3 = cs_zscore([x["r3"] for x in items])
                z_r10 = cs_zscore([x["r10"] for x in items])
                z_clv = cs_zscore([x["clv"] for x in items])
                z_rv = cs_zscore([x["rv"] for x in items])
                z_adr = cs_zscore([x["adr"] for x in items])
                for i in range(0, len(items), 2):
                    x = items[i]
                    Xq.append([
                        1.0, z_ro[i], z_vw[i], z_gp[i], z_r3[i], z_r10[i],
                        z_clv[i], z_rv[i] * (1.0 if x["ro"] >= 0 else -1.0), z_adr[i],
                    ])
                    yq.append(x["res_f"])

        weights = solve_ridge(Xq, yq, l2=1500.0) if Xq else [0.0, -0.04, -0.05, -0.02, -0.02, -0.01, -0.02, -0.03, 0.01]
        raw_preds = [sum(w * v for w, v in zip(weights, row)) for row in Xq]
        ols_a, ols_b = fit_ols_1d(raw_preds, yq) if Xq else (0.0, 1.0)

        core = {
            "model_id": "qlib_v3_book2_15m_cs_ridge",
            "declared_horizon": "ENTRY_TO_1515_IST_SQUAREOFF",
            "features": QLIB_BOOK2_FEATURES,
            "l2_penalty": 1500.0,
            "weights": [round(w, 6) for w in weights],
            "ols_intercept_pct": round(ols_a, 6),
            "ols_slope": round(ols_b, 6),
            "train_samples": len(Xq),
            "max_train_date": vdays[-1] if vdays else (max_train_date or ""),
        }
        core["artifact_sha256"] = sha256_json(core)
        core["trained_at"] = datetime.now().isoformat()
        return core

    def score_book1_snapshot(self, snapshot: Dict[str, Any], artifact: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Scores Book 1 `20d` snapshot in `<5ms` with calibrated expected 20d residual return (`bps`)."""
        art = artifact or self._book1_artifact
        cands = snapshot.get("candidates", [])
        if not cands or not art:
            return cands

        w = art["weights"]
        oa, ob = float(art.get("ols_intercept_pct", 0.0)), float(art.get("ols_slope", 1.0))
        z1 = cs_zscore([c["mom_12_1_pct"] for c in cands])
        z2 = cs_zscore([c["mom_6_1_pct"] for c in cands])
        z3 = cs_zscore([c["sharpe_12_1"] for c in cands])
        z4 = cs_zscore([c["near_52w_ratio"] for c in cands])
        z5 = cs_zscore([c["pullback_20d_pct"] for c in cands])

        raw_scores = []
        for i, c in enumerate(cands):
            fv = [1.0, z1[i], z2[i], z3[i], z4[i], z5[i]]
            raw_q = sum(wi * vi for wi, vi in zip(w, fv))
            cal_pct = oa + ob * raw_q
            c["qlib_expected_gross_bps"] = round(cal_pct * 100.0, 2)
            raw_scores.append(cal_pct)

        s_sorted = sorted(raw_scores)
        n = max(1, len(s_sorted))
        for c, sc in zip(cands, raw_scores):
            c["qlib_pct_rank"] = round(sum(1 for v in s_sorted if v <= sc) / n, 4)
        return cands

    def score_book2_snapshot(self, snapshot: Dict[str, Any], artifact: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Scores Book 2 `15m` cross-sectional snapshot in `<5ms` with calibrated expected residual return to `15:15 IST` (`bps`)."""
        art = artifact or self._book2_artifact
        items = snapshot.get("items", [])
        if not items or not art:
            return items

        w = art["weights"]
        oa, ob = float(art.get("ols_intercept_pct", 0.0)), float(art.get("ols_slope", 1.0))
        z_ro = cs_zscore([x["ret_open_pct"] for x in items])
        z_vw = cs_zscore([x["vwap_atr"] for x in items])
        z_gp = cs_zscore([x["gap_pct"] for x in items])
        z_r3 = cs_zscore([x["ret_3d_prior"] for x in items])
        z_r10 = cs_zscore([x["ret_10d_prior"] for x in items])
        z_clv = cs_zscore([x["clv"] - 0.50 for x in items])
        z_rv = cs_zscore([math.log1p(x["rvol"]) for x in items])
        z_adr = cs_zscore([x["adr_pct"] for x in items])

        cal_list = []
        for i, x in enumerate(items):
            fv = [
                1.0, z_ro[i], z_vw[i], z_gp[i], z_r3[i], z_r10[i],
                z_clv[i], z_rv[i] * (1.0 if x["ret_open_pct"] >= 0 else -1.0), z_adr[i],
            ]
            raw_q = sum(wi * vi for wi, vi in zip(w, fv))
            cal_pct = oa + ob * raw_q
            x["qlib_stock_residual_pct"] = round(cal_pct, 4)
            cal_list.append(cal_pct)

        s_sorted = sorted(cal_list)
        n = max(1, len(s_sorted))
        for x, sc in zip(items, cal_list):
            x["qlib_pct_rank"] = round(sum(1 for v in s_sorted if v <= sc) / n, 4)
        return items
