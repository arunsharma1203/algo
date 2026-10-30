"""
QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION (Alpha360)
================================================================
Provenance:
    Implementation Version: qlib_research_v1
    Methodology: Microsoft Qlib Alpha360 Factor Family Specification
    Reference: Microsoft Research Qlib (quant investment platform)
    Specification Source: qlib/contrib/data/handler.py (Alpha360)
    Causal Integrity: Strict past-and-current bar access only (no future lookahead)
    Framework: NumPy, Pandas, SciPy

This module computes the 360-factor normalized sequence feature set (60 periods x 6 channels:
open, high, low, close, volume, vwap) for both Intraday (15m) and Swing (1D) timeframes.
"""

import numpy as np
import pandas as pd
from typing import List, Optional

PROVENANCE_LABEL = "QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION"
FEATURE_VERSION = "qlib_research_v1"

def compute_alpha360_features(df: pd.DataFrame, window: int = 60) -> pd.DataFrame:
    """
    Computes 360 normalized sequence features for each bar t:
    6 channels: [open, high, low, close, volume, vwap] over past 60 bars (lag 0 to lag 59).
    
    Normalization:
        - Price channels normalized by close at bar t: (price_lag / close_t - 1.0)
        - Volume channel normalized by 60-bar mean volume: (vol_lag / mean_vol_t - 1.0)
    """
    if df is None or len(df) < window + 5:
        raise ValueError(f"Insufficient bars for Alpha360 feature calculation ({len(df) if df is not None else 0} < {window + 5})")

    work = df.copy()
    col_map = {str(c).lower(): c for c in work.columns}
    
    o = work[col_map.get('open', 'open')].values.astype(np.float64)
    h = work[col_map.get('high', 'high')].values.astype(np.float64)
    l = work[col_map.get('low', 'low')].values.astype(np.float64)
    c = work[col_map.get('close', 'close')].values.astype(np.float64)
    v = work[col_map.get('volume', 'volume')].values.astype(np.float64)
    
    # Calculate VWAP: (close * volume) / volume
    # For rolling 60 bars:
    pv = c * v
    n = len(df)
    eps = 1e-8
    
    # Pre-allocate feature matrix: shape (n, window * 6)
    feature_names = []
    channels = ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME', 'VWAP']
    for ch in channels:
        for lag in range(window - 1, -1, -1):
            feature_names.append(f'{ch}_{lag}')
            
    out_matrix = np.zeros((n, len(feature_names)), dtype=np.float64)
    
    # Fast vectorized rolling calculation
    for t in range(window - 1, n):
        c_t = c[t] if c[t] > eps else eps
        w_start = t - window + 1
        w_end = t + 1
        
        w_o = o[w_start:w_end]
        w_h = h[w_start:w_end]
        w_l = l[w_start:w_end]
        w_c = c[w_start:w_end]
        w_v = v[w_start:w_end]
        w_pv = pv[w_start:w_end]
        
        # Volume mean
        v_mean = np.mean(w_v)
        if v_mean < eps:
            v_mean = eps
            
        # VWAP per bar approximation: price * volume / mean
        w_vwap = np.where(w_v > eps, w_pv / (w_v + eps), w_c)
        
        # Normalized channels:
        norm_o = w_o / c_t - 1.0
        norm_h = w_h / c_t - 1.0
        norm_l = w_l / c_t - 1.0
        norm_c = w_c / c_t - 1.0
        norm_v = w_v / v_mean - 1.0
        norm_vwap = w_vwap / c_t - 1.0
        
        out_matrix[t, :] = np.concatenate([norm_o, norm_h, norm_l, norm_c, norm_v, norm_vwap])
        
    feat_df = pd.DataFrame(out_matrix, index=df.index, columns=feature_names)
    feat_df.replace([np.inf, -np.inf], np.nan, inplace=True)
    feat_df.fillna(0.0, inplace=True)
    
    return feat_df

def get_alpha360_feature_names(window: int = 60) -> List[str]:
    """Returns the frozen list of 360 factor names."""
    channels = ['OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME', 'VWAP']
    feature_names = []
    for ch in channels:
        for lag in range(window - 1, -1, -1):
            feature_names.append(f'{ch}_{lag}')
    return feature_names
