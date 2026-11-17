"""
Canonical NSE Cash Equity Accounting (`MIS` Intraday & `CNC` Delivery) & Pure-Python ML Primitives
==================================================================================================
Enforces:
  - Exact NSE ₹0.05 equity tick rounding (`round_nse_eq_tick`).
  - Whole-share integer sizing (`compute_whole_shares`) strictly bounded by unreserved funded cash & liquidity cap.
  - Exact Upstox + NSE statutory charges (`STT`, `Stamp Duty`, `NSE Txn`, `SEBI/IPFT`, `DP Debit`, `18% GST`, `Brokerage`) + realistic tier slippage.
  - Deterministic, zero-leakage Ridge + OLS Slope Calibrator + Regularized Boosted Stumps with Pool-Adjacent-Violators (`PAV`) Isotonic Calibration.
"""

import hashlib
import math
import statistics
from typing import Dict, List, Sequence, Tuple


def round_nse_eq_tick(price: float, tick: float = 0.05) -> float:
    """Rounds an NSE cash equity price to the nearest ₹0.05 tick."""
    if price <= 0:
        return 0.05
    return round(round(float(price) / tick) * tick, 2)


def compute_whole_shares(
    allocatable_cash_inr: float,
    price_inr: float,
    max_slot_inr: float,
    liquidity_cap_inr: float,
) -> int:
    """Computes strictly non-negative integer whole shares without leverage (`0` if unaffordable)."""
    if price_inr <= 0 or allocatable_cash_inr <= 0 or max_slot_inr <= 0 or liquidity_cap_inr <= 0:
        return 0
    eff_budget = min(float(allocatable_cash_inr), float(max_slot_inr), float(liquidity_cap_inr))
    if eff_budget < price_inr:
        return 0
    return max(0, int(eff_budget // float(price_inr)))


def compute_nse_mis_charges(
    entry_px: float,
    exit_px: float,
    shares: int,
    direction: str,
    is_fno_eligible: bool = True,
    passive_entry: bool = True,
    cost_mult: float = 1.0,
) -> Dict[str, float]:
    """
    Exact NSE + Upstox Cash Intraday (`MIS`) round-trip statutory taxes, brokerage, and slippage:
      - Brokerage: min(₹20, 0.05%) per leg
      - STT: 0.025% on SELL leg
      - Stamp Duty: 0.003% on BUY leg
      - NSE Exchange Txn: 0.00297% on turnover
      - SEBI + IPFT: 0.0002% on turnover
      - GST: 18% on (Brokerage + Exchange + SEBI)
      - Slippage: 2.5 bps passive adverse selection + 5.0 bps market exit (or 5.0/8.5 bps/leg market-market)
    """
    if shares <= 0 or entry_px <= 0 or exit_px <= 0:
        return {"statutory_and_brokerage_inr": 0.0, "slippage_inr": 0.0, "total_cost_inr": 0.0, "total_cost_bps": 0.0}

    is_buy = str(direction).upper() in ("BUY", "LONG", "BULLISH")
    buy_val = (entry_px * shares) if is_buy else (exit_px * shares)
    sell_val = (exit_px * shares) if is_buy else (entry_px * shares)
    turnover = buy_val + sell_val

    brok = min(20.0, 0.0005 * buy_val) + min(20.0, 0.0005 * sell_val)
    stt = 0.00025 * sell_val
    stamp = 0.00003 * buy_val
    exch = 0.0000297 * turnover
    sebi = 0.000002 * turnover
    gst = 0.18 * (brok + exch + sebi)
    stat_brok = (brok + stt + stamp + exch + sebi + gst) * cost_mult

    if passive_entry:
        slip = ((entry_px * shares) * 0.00025 + (exit_px * shares) * (0.00050 if is_fno_eligible else 0.00085)) * cost_mult
    else:
        bps_leg = 5.0 if is_fno_eligible else 8.5
        slip = turnover * (bps_leg / 10000.0) * cost_mult

    tot = stat_brok + slip
    notional = max(1e-9, entry_px * shares)
    return {
        "statutory_and_brokerage_inr": round(stat_brok, 2),
        "slippage_inr": round(slip, 2),
        "total_cost_inr": round(tot, 2),
        "total_cost_bps": round((tot / notional) * 10000.0, 2),
    }


def compute_nse_cnc_charges(
    entry_px: float,
    exit_px: float,
    shares: int,
    is_fno_eligible: bool = True,
    cost_mult: float = 1.0,
) -> Dict[str, float]:
    """
    Exact NSE + Upstox Cash Delivery (`CNC`) round-trip statutory taxes, DP debit, brokerage, and slippage:
      - STT: 0.10% on BUY + 0.10% on SELL (20 bps statutory!)
      - Stamp Duty: 0.015% on BUY
      - NSE Exchange Txn: 0.00297% on turnover; SEBI + IPFT: 0.0002% on turnover
      - Brokerage: min(₹20, 2.5%) per leg + DP Demat Debit ₹21.83 (₹18.5 + 18% GST)
      - GST: 18% on (Brokerage + Exchange + SEBI)
      - Slippage: 8.0 bps/leg (F&O-181) or 12.0 bps/leg (Non-F&O)
    """
    if shares <= 0 or entry_px <= 0 or exit_px <= 0:
        return {"statutory_and_brokerage_inr": 0.0, "slippage_inr": 0.0, "total_cost_inr": 0.0, "total_cost_bps": 0.0}

    buy_val = entry_px * shares
    sell_val = exit_px * shares
    turnover = buy_val + sell_val

    brok = min(20.0, 0.025 * buy_val) + min(20.0, 0.025 * sell_val)
    stt = 0.0010 * buy_val + 0.0010 * sell_val
    stamp = 0.00015 * buy_val
    exch = 0.0000297 * turnover
    sebi = 0.000002 * turnover
    gst = 0.18 * (brok + exch + sebi)
    dp_debit = 21.83
    stat_brok = (brok + stt + stamp + exch + sebi + gst + dp_debit) * cost_mult

    slip_bps = 8.0 if is_fno_eligible else 12.0
    slip = turnover * (slip_bps / 10000.0) * cost_mult
    tot = stat_brok + slip
    notional = max(1e-9, entry_px * shares)
    return {
        "statutory_and_brokerage_inr": round(stat_brok, 2),
        "slippage_inr": round(slip, 2),
        "total_cost_inr": round(tot, 2),
        "total_cost_bps": round((tot / notional) * 10000.0, 2),
    }


def cs_zscore(vals: Sequence[float]) -> List[float]:
    """Point-in-time cross-sectional z-score clipped to [-4, +4]."""
    n = len(vals)
    if n == 0:
        return []
    m = sum(vals) / n
    sd = max(1e-6, statistics.stdev(vals) if n >= 2 else 1.0)
    return [max(-4.0, min(4.0, (float(v) - m) / sd)) for v in vals]


def solve_ridge(X: Sequence[Sequence[float]], y: Sequence[float], l2: float = 1500.0) -> List[float]:
    """Deterministic Gaussian-elimination L2 Ridge solver (intercept at index 0 unpenalized)."""
    if not X:
        return []
    p = len(X[0])
    xtx = [[0.0] * p for _ in range(p)]
    xty = [0.0] * p
    for row, target in zip(X, y):
        t_val = float(target)
        for j in range(p):
            rj = float(row[j])
            xty[j] += rj * t_val
            for k in range(j, p):
                xtx[j][k] += rj * float(row[k])
    for j in range(p):
        for k in range(j):
            xtx[j][k] = xtx[k][j]
        if j > 0:
            xtx[j][j] += l2

    aug = [xtx[i][:] + [xty[i]] for i in range(p)]
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(aug[r][col]))
        aug[col], aug[piv] = aug[piv], aug[col]
        denom = aug[col][col]
        if abs(denom) < 1e-12:
            continue
        for k in range(col, p + 1):
            aug[col][k] /= denom
        for r in range(p):
            if r != col and abs(aug[r][col]) > 1e-12:
                f = aug[r][col]
                for k in range(col, p + 1):
                    aug[r][k] -= f * aug[col][k]
    return [aug[i][p] for i in range(p)]


def fit_ols_1d(xs: Sequence[float], ys: Sequence[float]) -> Tuple[float, float]:
    """Fits univariate OLS slope calibrator `E[y] = a + b * x` on training fold to undo L2 shrinkage."""
    n = len(xs)
    if n < 20:
        return 0.0, 1.0
    mx = sum(xs) / n
    my = sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    if vx <= 1e-12:
        return my, 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = max(0.0, min(5.0, cov / vx))
    a = my - b * mx
    return a, b


class RegularizedBoostedStumpPAV:
    """
    Low-variance Gradient Boosted Decision Stumps paired with Pool-Adjacent-Violators (PAV)
    monotonic isotonic probability calibration. Serializable to pure JSON.
    """

    def __init__(self, n_trees: int = 8, lr: float = 0.10, min_leaf: int = 30):
        self.n_trees = n_trees
        self.lr = lr
        self.min_leaf = min_leaf
        self.base_log_odds = 0.0
        self.stumps: List[Tuple[int, float, float, float]] = []
        self.pav: List[Tuple[float, float, float]] = []

    @staticmethod
    def _sig(z: float) -> float:
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-min(30.0, z)))
        ez = math.exp(max(-30.0, z))
        return ez / (1.0 + ez)

    def fit(self, X: Sequence[Sequence[float]], y_bin: Sequence[int], max_rows: int = 12000) -> None:
        self.stumps, self.pav = [], []
        n0 = len(y_bin)
        if n0 < 40:
            self.base_log_odds = 0.0
            return
        if n0 > max_rows:
            step = max(1, n0 // max_rows)
            X = X[::step]
            y_bin = y_bin[::step]
        n = len(y_bin)
        pos = max(0.05, min(0.95, sum(y_bin) / n))
        self.base_log_odds = math.log(pos / (1.0 - pos))
        f_vals = [self.base_log_odds] * n
        nf = len(X[0])
        splits = []
        for j in range(nf):
            cs = sorted(float(r[j]) for r in X)
            splits.append(sorted(set(cs[min(n - 1, int(n * q))] for q in (0.15, 0.30, 0.50, 0.70, 0.85))))

        ml = min(self.min_leaf, max(15, n // 10))
        for _ in range(self.n_trees):
            res = [float(y_bin[i]) - self._sig(f_vals[i]) for i in range(n)]
            tot_r = sum(res)
            bg, bst = -1.0, None
            for j in range(nf):
                for thr in splits[j]:
                    ls, lc = 0.0, 0
                    for i in range(n):
                        if float(X[i][j]) <= thr:
                            ls += res[i]
                            lc += 1
                    rc = n - lc
                    if lc < ml or rc < ml:
                        continue
                    rs = tot_r - ls
                    g = (ls * ls) / (lc + 30.0) + (rs * rs) / (rc + 30.0)
                    if g > bg:
                        bg = g
                        bst = (j, float(thr), ls / (0.25 * lc + 15.0), rs / (0.25 * rc + 15.0))
            if bst is None:
                break
            self.stumps.append(bst)
            j, thr, lv, rv = bst
            for i in range(n):
                f_vals[i] += self.lr * (lv if float(X[i][j]) <= thr else rv)

        # Pre-aggregate exact/near-identical discrete tree scores `s` BEFORE Pool-Adjacent-Violators
        # so tied `(s, y=0)` / `(s, y=1)` boundary rows never create spurious 0.00 or 1.00 singleton blocks.
        grouped: Dict[float, List[float]] = {}
        for s, yb in zip(f_vals, y_bin):
            key = round(float(s), 9)
            if key not in grouped:
                grouped[key] = [float(s), float(s), float(yb), 1.0]
            else:
                grouped[key][2] += float(yb)
                grouped[key][3] += 1.0
        blocks = [grouped[k] for k in sorted(grouped.keys())]
        idx = 0
        while idx < len(blocks) - 1:
            if (blocks[idx][2] / blocks[idx][3]) > (blocks[idx + 1][2] / blocks[idx + 1][3]):
                blocks[idx][1] = blocks[idx + 1][1]
                blocks[idx][2] += blocks[idx + 1][2]
                blocks[idx][3] += blocks[idx + 1][3]
                blocks.pop(idx + 1)
                if idx > 0:
                    idx -= 1
            else:
                idx += 1
        self.pav = [(float(b[0]), float(b[1]), float(b[2] / b[3])) for b in blocks]

    def predict_proba(self, row: Sequence[float]) -> float:
        s = round(
            self.base_log_odds
            + sum(self.lr * (lv if float(row[j]) <= thr else rv) for j, thr, lv, rv in self.stumps),
            9,
        )
        if not self.pav:
            return self._sig(s)
        if s <= self.pav[0][0]:
            return self.pav[0][2]
        if s >= self.pav[-1][1]:
            return self.pav[-1][2]
        for _, mx, pr in self.pav:
            if s <= mx + 1e-9:
                return pr
        return self.pav[-1][2]

    def to_dict(self) -> Dict:
        return {
            "n_trees": self.n_trees,
            "lr": self.lr,
            "min_leaf": self.min_leaf,
            "base_log_odds": self.base_log_odds,
            "stumps": [list(t) for t in self.stumps],
            "pav": [list(b) for b in self.pav],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "RegularizedBoostedStumpPAV":
        inst = cls(
            n_trees=int(data.get("n_trees", 8)),
            lr=float(data.get("lr", 0.10)),
            min_leaf=int(data.get("min_leaf", 30)),
        )
        inst.base_log_odds = float(data.get("base_log_odds", 0.0))
        inst.stumps = [(int(t[0]), float(t[1]), float(t[2]), float(t[3])) for t in data.get("stumps", [])]
        inst.pav = [(float(b[0]), float(b[1]), float(b[2])) for b in data.get("pav", [])]
        return inst


def sha256_json(payload: Dict) -> str:
    import json
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
