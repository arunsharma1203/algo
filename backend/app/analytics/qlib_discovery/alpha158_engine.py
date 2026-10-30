"""
QLIB CANONICAL Alpha158 FACTOR IMPLEMENTATION
=============================================
Provenance:
    Implementation: Exact Microsoft Qlib Alpha158 Factor Specification
    Source Reference: qlib/contrib/data/loader.py (Alpha158DL.get_feature_config)
    Total Factors: Exactly 158 factors (0 padding, 0 truncation)
    Causal Integrity: Strict past-and-current bar access only (no future lookahead)
    Framework: NumPy, Pandas, SciPy

Factor Breakdown (Total 158):
    1. K-Line Price Patterns (9 factors):
       - KMID:  ($close - $open) / $open
       - KLEN:  ($high - $low) / $open
       - KMID2: ($close - $open) / ($high - $low + 1e-12)
       - KUP:   ($high - max($open, $close)) / $open
       - KUP2:  ($high - max($open, $close)) / ($high - $low + 1e-12)
       - KLOW:  (min($open, $close) - $low) / $open
       - KLOW2: (min($open, $close) - $low) / ($high - $low + 1e-12)
       - KSFT:  (2 * $close - $high - $low) / $open
       - KSFT2: (2 * $close - $high - $low) / ($high - $low + 1e-12)
    2. Normalized Price Features (4 factors):
       - OPEN0: $open / $close
       - HIGH0: $high / $close
       - LOW0:  $low / $close
       - VWAP0: $vwap / $close
    3. Multi-Horizon Rolling Operators across windows [5, 10, 20, 30, 60] (29 x 5 = 145 factors):
       - ROC:   Rate of change ($close.shift(d) / $close)
       - MA:    Simple moving average normalized (Mean($close, d) / $close)
       - STD:   Close price standard deviation normalized (Std($close, d) / $close)
       - BETA:  Linear regression slope normalized (Slope($close, d) / $close)
       - RSQR:  Linear regression R-squared (Rsquare($close, d))
       - RESI:  Linear regression latest residual normalized (Resi($close, d) / $close)
       - MAX:   Rolling maximum high normalized (Max($high, d) / $close)
       - MIN:   Rolling minimum low normalized (Min($low, d) / $close)
       - QTLU:  80% quantile of close normalized (Quantile($close, d, 0.8) / $close)
       - QTLD:  20% quantile of close normalized (Quantile($close, d, 0.2) / $close)
       - RANK:  Percentile rank of current close in rolling window
       - RSV:   Relative strength value (($close - Min($low, d)) / (Max($high, d) - Min($low, d) + 1e-12))
       - IMAX:  Days since rolling maximum high normalized (IdxMax($high, d) / d)
       - IMIN:  Days since rolling minimum low normalized (IdxMin($low, d) / d)
       - IMXD:  Difference between IMAX and IMIN ((IdxMax - IdxMin) / d)
       - CORR:  Rolling correlation of close and log(volume + 1)
       - CORD:  Rolling correlation of price return and volume return
       - CNTP:  Percentage of up bars (Mean($close > Ref($close, 1), d))
       - CNTN:  Percentage of down bars (Mean($close < Ref($close, 1), d))
       - CNTD:  Difference between up and down bar percentages (CNTP - CNTN)
       - SUMP:  Up price change ratio (Sum(max(diff, 0), d) / Sum(|diff|, d))
       - SUMN:  Down price change ratio (Sum(max(-diff, 0), d) / Sum(|diff|, d))
       - SUMD:  Net price direction ratio (SUMP - SUMN)
       - VMA:   Volume moving average normalized (Mean($volume, d) / ($volume + 1e-12))
       - VSTD:  Volume standard deviation normalized (Std($volume, d) / ($volume + 1e-12))
       - WVMA:  Volume-weighted price change volatility
       - VSUMP: Up volume change ratio
       - VSUMN: Down volume change ratio
       - VSUMD: Net volume direction ratio
"""

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from typing import List, Tuple, Dict, Any

PROVENANCE_LABEL = "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
FEATURE_VERSION = "qlib_research_v1"

# The authoritative list of 158 canonical factor names in exact Qlib order
CANONICAL_ALPHA158_NAMES = [
    "KMID", "KLEN", "KMID2", "KUP", "KUP2", "KLOW", "KLOW2", "KSFT", "KSFT2",
    "OPEN0", "HIGH0", "LOW0", "VWAP0",
    "ROC5", "ROC10", "ROC20", "ROC30", "ROC60",
    "MA5", "MA10", "MA20", "MA30", "MA60",
    "STD5", "STD10", "STD20", "STD30", "STD60",
    "BETA5", "BETA10", "BETA20", "BETA30", "BETA60",
    "RSQR5", "RSQR10", "RSQR20", "RSQR30", "RSQR60",
    "RESI5", "RESI10", "RESI20", "RESI30", "RESI60",
    "MAX5", "MAX10", "MAX20", "MAX30", "MAX60",
    "MIN5", "MIN10", "MIN20", "MIN30", "MIN60",
    "QTLU5", "QTLU10", "QTLU20", "QTLU30", "QTLU60",
    "QTLD5", "QTLD10", "QTLD20", "QTLD30", "QTLD60",
    "RANK5", "RANK10", "RANK20", "RANK30", "RANK60",
    "RSV5", "RSV10", "RSV20", "RSV30", "RSV60",
    "IMAX5", "IMAX10", "IMAX20", "IMAX30", "IMAX60",
    "IMIN5", "IMIN10", "IMIN20", "IMIN30", "IMIN60",
    "IMXD5", "IMXD10", "IMXD20", "IMXD30", "IMXD60",
    "CORR5", "CORR10", "CORR20", "CORR30", "CORR60",
    "CORD5", "CORD10", "CORD20", "CORD30", "CORD60",
    "CNTP5", "CNTP10", "CNTP20", "CNTP30", "CNTP60",
    "CNTN5", "CNTN10", "CNTN20", "CNTN30", "CNTN60",
    "CNTD5", "CNTD10", "CNTD20", "CNTD30", "CNTD60",
    "SUMP5", "SUMP10", "SUMP20", "SUMP30", "SUMP60",
    "SUMN5", "SUMN10", "SUMN20", "SUMN30", "SUMN60",
    "SUMD5", "SUMD10", "SUMD20", "SUMD30", "SUMD60",
    "VMA5", "VMA10", "VMA20", "VMA30", "VMA60",
    "VSTD5", "VSTD10", "VSTD20", "VSTD30", "VSTD60",
    "WVMA5", "WVMA10", "WVMA20", "WVMA30", "WVMA60",
    "VSUMP5", "VSUMP10", "VSUMP20", "VSUMP30", "VSUMP60",
    "VSUMN5", "VSUMN10", "VSUMN20", "VSUMN30", "VSUMN60",
    "VSUMD5", "VSUMD10", "VSUMD20", "VSUMD30", "VSUMD60"
]

def _rolling_linear_regression(series: pd.Series, d: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Vectorized rolling linear regression over past d bars:
    y = beta * x + alpha
    where x = 0, 1, ..., d-1.
    Returns: (beta, rsqr, resi)
    """
    n = len(series)
    beta = np.full(n, np.nan, dtype=np.float32)
    rsqr = np.full(n, np.nan, dtype=np.float32)
    resi = np.full(n, np.nan, dtype=np.float32)

    if n < d:
        return beta, rsqr, resi

    # x centered weights: sum(w_i) = 0
    w = np.arange(d, dtype=np.float64) - (d - 1) / 2.0
    s_xx = d * (d**2 - 1) / 12.0

    y_vals = series.values.astype(np.float64)
    # Rolling S_xy using np.convolve with reversed weights w (strictly causal)
    s_xy = np.convolve(y_vals, w[::-1], mode='full')[:n]
    s_xy[:d - 1] = np.nan

    b = s_xy / s_xx
    y_mean = series.rolling(d).mean().values
    y_var = series.rolling(d).var().values * (d - 1)  # S_yy

    r2 = np.clip((b**2 * s_xx) / (y_var + 1e-12), 0.0, 1.0)
    # Residual at the latest bar: y_last - (y_mean + beta * (d - 1)/2)
    res = y_vals - (y_mean + b * (d - 1) / 2.0)

    beta[:] = b.astype(np.float32)
    rsqr[:] = r2.astype(np.float32)
    resi[:] = res.astype(np.float32)
    return beta, rsqr, resi

def _rolling_argextrema(arr: np.ndarray, d: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Computes IMAX, IMIN, and IMXD:
    IMAX: Days since rolling maximum (0 = current bar, normalized by d)
    IMIN: Days since rolling minimum (0 = current bar, normalized by d)
    IMXD: (IdxMax - IdxMin) / d
    """
    n = len(arr)
    imax = np.full(n, np.nan, dtype=np.float32)
    imin = np.full(n, np.nan, dtype=np.float32)
    imxd = np.full(n, np.nan, dtype=np.float32)

    if n >= d:
        v = sliding_window_view(arr, d)
        # argmax along axis 1 gives index 0..d-1
        max_idx = (d - 1) - np.argmax(v, axis=1)
        min_idx = (d - 1) - np.argmin(v, axis=1)
        imax[d - 1:] = max_idx / float(d)
        imin[d - 1:] = min_idx / float(d)
        imxd[d - 1:] = (max_idx - min_idx) / float(d)

    return imax, imin, imxd

def _rolling_rank(arr: np.ndarray, d: int) -> np.ndarray:
    """
    Percentile rank of current close within rolling window of length d.
    """
    n = len(arr)
    rank = np.full(n, np.nan, dtype=np.float32)
    if n >= d:
        v = sliding_window_view(arr, d)
        current = arr[d - 1:, None]
        # Count elements strictly less + 0.5 * equal, divided by d
        r = (np.sum(v < current, axis=1) + 0.5 * np.sum(v == current, axis=1)) / float(d)
        rank[d - 1:] = r
    return rank

def compute_alpha158_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes the canonical 158 factors of the Qlib Alpha158 specification.
    
    All operations are strictly causal (only bars <= t are used).
    Returns a DataFrame containing exactly 158 factor columns matching
    CANONICAL_ALPHA158_NAMES with zero padding and zero truncation.
    """
    if df is None or len(df) < 65:
        raise ValueError(f"Insufficient bars for Alpha158 calculation ({len(df) if df is not None else 0} < 65)")

    work = df.copy()
    col_map = {str(c).lower(): c for c in work.columns}

    o = work[col_map.get('open', 'open')].astype(float)
    h = work[col_map.get('high', 'high')].astype(float)
    l = work[col_map.get('low', 'low')].astype(float)
    c = work[col_map.get('close', 'close')].astype(float)
    v = work[col_map.get('volume', 'volume')].astype(float)

    eps = 1e-12
    hl_diff = (h - l) + eps
    o_safe = o.replace(0, eps)
    c_safe = c.replace(0, eps)
    v_safe = v.replace(0, eps)

    # Compute VWAP if not explicitly provided
    if 'vwap' in col_map:
        vwap = work[col_map['vwap']].astype(float)
    else:
        pv = c * v
        vwap = (pv.cumsum() / v.cumsum().replace(0, eps)).fillna(c)

    feats: Dict[str, Any] = {}

    # ── 1. K-Line Factors (9 features) ──────────────────────────────────
    feats['KMID'] = (c - o) / o_safe
    feats['KLEN'] = (h - l) / o_safe
    feats['KMID2'] = (c - o) / hl_diff
    max_oc = np.maximum(o, c)
    min_oc = np.minimum(o, c)
    feats['KUP'] = (h - max_oc) / o_safe
    feats['KUP2'] = (h - max_oc) / hl_diff
    feats['KLOW'] = (min_oc - l) / o_safe
    feats['KLOW2'] = (min_oc - l) / hl_diff
    feats['KSFT'] = (2 * c - h - l) / o_safe
    feats['KSFT2'] = (2 * c - h - l) / hl_diff

    # ── 2. Normalized Price Features (4 features) ───────────────────────
    feats['OPEN0'] = o / c_safe
    feats['HIGH0'] = h / c_safe
    feats['LOW0'] = l / c_safe
    feats['VWAP0'] = vwap / c_safe

    # ── 3. Multi-Horizon Rolling Operators across windows [5, 10, 20, 30, 60]
    windows = [5, 10, 20, 30, 60]

    # Precompute common series
    c_diff = c - c.shift(1)
    c_diff_pos = np.maximum(c_diff, 0.0)
    c_diff_neg = np.maximum(-c_diff, 0.0)
    c_diff_abs = np.abs(c_diff)

    v_diff = v - v.shift(1)
    v_diff_pos = np.maximum(v_diff, 0.0)
    v_diff_neg = np.maximum(-v_diff, 0.0)
    v_diff_abs = np.abs(v_diff)

    log_v = np.log(v + 1.0)
    log_v_ret = np.log(v / v.shift(1).replace(0, eps) + 1.0).fillna(0.0)
    c_ret = (c / c.shift(1).replace(0, eps)).fillna(1.0)
    vol_price_chg = np.abs(c / c.shift(1).replace(0, eps) - 1.0) * v

    c_vals = c.values.astype(np.float64)
    h_vals = h.values.astype(np.float64)
    l_vals = l.values.astype(np.float64)

    for d in windows:
        # ROC: Ref($close, d) / $close
        feats[f'ROC{d}'] = c.shift(d) / c_safe

        # MA: Mean($close, d) / $close
        feats[f'MA{d}'] = c.rolling(d).mean() / c_safe

        # STD: Std($close, d) / $close
        feats[f'STD{d}'] = c.rolling(d).std() / c_safe

        # BETA, RSQR, RESI: Linear Regression Operators
        b, r2, res = _rolling_linear_regression(c, d)
        feats[f'BETA{d}'] = pd.Series(b, index=df.index) / c_safe
        feats[f'RSQR{d}'] = pd.Series(r2, index=df.index)
        feats[f'RESI{d}'] = pd.Series(res, index=df.index) / c_safe

        # MAX: Max($high, d) / $close
        max_h = h.rolling(d).max()
        feats[f'MAX{d}'] = max_h / c_safe

        # MIN: Min($low, d) / $close
        min_l = l.rolling(d).min()
        feats[f'MIN{d}'] = min_l / c_safe

        # QTLU: Quantile($close, d, 0.8) / $close
        feats[f'QTLU{d}'] = c.rolling(d).quantile(0.8) / c_safe

        # QTLD: Quantile($close, d, 0.2) / $close
        feats[f'QTLD{d}'] = c.rolling(d).quantile(0.2) / c_safe

        # RANK: Rank($close, d)
        r = _rolling_rank(c_vals, d)
        feats[f'RANK{d}'] = pd.Series(r, index=df.index)

        # RSV: ($close - Min($low, d)) / (Max($high, d) - Min($low, d) + 1e-12)
        feats[f'RSV{d}'] = (c - min_l) / (max_h - min_l + eps)

        # IMAX, IMIN, IMXD: Aroon / Days since extrema
        imax, imin, imxd = _rolling_argextrema(h_vals, d)
        feats[f'IMAX{d}'] = pd.Series(imax, index=df.index)
        _, imin_l, _ = _rolling_argextrema(l_vals, d)
        feats[f'IMIN{d}'] = pd.Series(imin_l, index=df.index)
        feats[f'IMXD{d}'] = feats[f'IMAX{d}'] - feats[f'IMIN{d}']

        # CORR: Corr($close, Log($volume + 1), d)
        feats[f'CORR{d}'] = c.rolling(d).corr(log_v)

        # CORD: Corr($close / Ref($close, 1), Log($volume / Ref($volume, 1) + 1), d)
        feats[f'CORD{d}'] = c_ret.rolling(d).corr(log_v_ret)

        # CNTP: Mean($close > Ref($close, 1), d)
        feats[f'CNTP{d}'] = (c_diff > 0).astype(float).rolling(d).mean()

        # CNTN: Mean($close < Ref($close, 1), d)
        feats[f'CNTN{d}'] = (c_diff < 0).astype(float).rolling(d).mean()

        # CNTD: CNTP - CNTN
        feats[f'CNTD{d}'] = feats[f'CNTP{d}'] - feats[f'CNTN{d}']

        # SUMP, SUMN, SUMD
        sum_c_abs = c_diff_abs.rolling(d).sum() + eps
        sump = c_diff_pos.rolling(d).sum() / sum_c_abs
        sumn = c_diff_neg.rolling(d).sum() / sum_c_abs
        feats[f'SUMP{d}'] = sump
        feats[f'SUMN{d}'] = sumn
        feats[f'SUMD{d}'] = sump - sumn

        # VMA: Mean($volume, d) / ($volume + 1e-12)
        feats[f'VMA{d}'] = v.rolling(d).mean() / v_safe

        # VSTD: Std($volume, d) / ($volume + 1e-12)
        feats[f'VSTD{d}'] = v.rolling(d).std() / v_safe

        # WVMA: Std(price_chg * vol, d) / (Mean(price_chg * vol, d) + 1e-12)
        wvma_std = vol_price_chg.rolling(d).std()
        wvma_mean = vol_price_chg.rolling(d).mean() + eps
        feats[f'WVMA{d}'] = wvma_std / wvma_mean

        # VSUMP, VSUMN, VSUMD
        sum_v_abs = v_diff_abs.rolling(d).sum() + eps
        vsump = v_diff_pos.rolling(d).sum() / sum_v_abs
        vsumn = v_diff_neg.rolling(d).sum() / sum_v_abs
        feats[f'VSUMP{d}'] = vsump
        feats[f'VSUMN{d}'] = vsumn
        feats[f'VSUMD{d}'] = vsump - vsumn

    # Assemble into DataFrame in exact canonical order
    feature_df = pd.DataFrame({col: feats[col] for col in CANONICAL_ALPHA158_NAMES}, index=df.index)

    # Clean inf and NaN
    feature_df.replace([np.inf, -np.inf], np.nan, inplace=True)
    feature_df.fillna(0.0, inplace=True)

    return feature_df

def get_alpha158_feature_names() -> List[str]:
    """Returns the canonical list of 158 factor names."""
    return list(CANONICAL_ALPHA158_NAMES)
