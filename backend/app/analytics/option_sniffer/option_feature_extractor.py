"""
Option Sniffer: Point-in-Time Feature Engineering Engine.

Extracts aligned, leak-free feature vectors for the Option Alpha Model:
  1. Underlying Technicals: Multi-horizon momentum, ATR, RSI, VWAP distance, India VIX, Macro regime.
  2. Upstream AI Context: Frozen Qlib Alpha-158 conviction, Legacy Ensemble score, Consensus agreement.
  3. Option Microstructure: Strike moneyness, DTE (time to expiry), Greeks (Delta, Gamma, Theta, Vega), IV,
     bid-ask spread %, order book queue imbalance, log volume, and log Open Interest.
  4. Train/Serve Equivalence: Uses identical calculation formulas in both historical backfill and live serving.
"""

import math
import numpy as np
import pandas as pd
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Tuple

FEATURE_NAMES = [
    "spot_price",
    "strike",
    "is_call",
    "moneyness",
    "abs_moneyness",
    "dte_days",
    "delta",
    "gamma",
    "theta",
    "vega",
    "iv",
    "spread_pct",
    "bid_ask_imbalance",
    "log_volume",
    "log_oi",
    "spot_ret_1",
    "spot_ret_3",
    "spot_ret_6",
    "spot_rsi",
    "spot_atr_pct",
    "spot_vwap_dist_pct",
    "india_vix",
    "macro_bullish",
    "macro_bearish",
    "upstream_qlib_score",
    "upstream_legacy_score",
    "upstream_consensus_conf",
    "upstream_aligned",
]


class OptionFeatureExtractor:
    """Computes point-in-time normalized feature arrays for Option Alpha Model."""

    @staticmethod
    def calculate_rsi(series: pd.Series, period: int = 14) -> float:
        """Calculates Relative Strength Index deterministically."""
        if len(series) < period + 1:
            return 50.0
        delta = series.diff().dropna()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(window=period, min_periods=period).mean().iloc[-1]
        avg_loss = loss.rolling(window=period, min_periods=period).mean().iloc[-1]
        if avg_loss == 0 or np.isnan(avg_loss):
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
        """Calculates Average True Range."""
        if len(df) < period + 1 or "high" not in df or "low" not in df or "close" not in df:
            return 1.0
        h = df["high"]
        l = df["low"]
        c_prev = df["close"].shift(1)
        tr = pd.concat([h - l, (h - c_prev).abs(), (l - c_prev).abs()], axis=1).max(axis=1)
        atr_val = tr.rolling(period).mean().iloc[-1]
        return float(atr_val) if not np.isnan(atr_val) and atr_val > 0 else 1.0

    @classmethod
    def extract_features(
        cls,
        contract: Dict[str, Any],
        quote: Dict[str, Any],
        underlying_quote: Dict[str, Any],
        underlying_df: Optional[pd.DataFrame] = None,
        macro_context: Optional[Dict[str, Any]] = None,
        upstream_signals: Optional[Dict[str, Any]] = None,
        as_of_time: Optional[datetime] = None
    ) -> Dict[str, float]:
        """
        Extracts a clean dictionary of features available strictly at decision time T.
        """
        now_dt = as_of_time or datetime.now()
        macro_context = macro_context or {}
        upstream_signals = upstream_signals or {}

        spot = float(underlying_quote.get("spot_ltp") or quote.get("spot_price") or 22500.0)
        strike = float(contract.get("strike") or quote.get("strike") or spot)
        opt_type = (contract.get("option_type") or quote.get("option_type") or "CE").upper()
        is_call = 1.0 if opt_type == "CE" else 0.0

        # Moneyness:
        # CE: (Spot - Strike) / Strike (positive = ITM, negative = OTM)
        # PE: (Strike - Spot) / Strike (positive = ITM, negative = OTM)
        moneyness = ((spot - strike) / strike) if is_call else ((strike - spot) / strike)
        abs_moneyness = abs(spot - strike) / strike

        # Time to Expiry (DTE in fractional days)
        expiry_str = contract.get("expiry") or quote.get("expiry")
        dte_days = 2.0  # fallback
        if expiry_str:
            try:
                exp_date = datetime.strptime(expiry_str[:10], "%Y-%m-%d").date()
                curr_date = now_dt.date()
                days_diff = (exp_date - curr_date).days
                # Market closes at 15:30 IST; fractional intraday decay
                rem_hours = max(0.0, 15.5 - (now_dt.hour + now_dt.minute / 60.0)) if days_diff == 0 else 6.25
                dte_days = max(0.05, days_diff + (rem_hours / 6.25))
            except Exception:
                dte_days = 2.0

        # Microstructure & Greeks
        delta = float(quote.get("delta") or (0.50 if is_call else -0.50))
        gamma = float(quote.get("gamma") or 0.001)
        theta = float(quote.get("theta") or -10.0)
        vega = float(quote.get("vega") or 5.0)
        iv = float(quote.get("iv") or 15.0)

        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
        ltp = float(quote.get("ltp") or ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 100.0))
        spread_pct = float(quote.get("spread_pct") or (((ask - bid) / ltp * 100.0) if ltp > 0 and ask >= bid else 1.0))

        bid_qty = float(quote.get("bid_qty") or 100.0)
        ask_qty = float(quote.get("ask_qty") or 100.0)
        bid_ask_imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty + 1.0)

        volume = float(quote.get("volume") or 1000.0)
        oi = float(quote.get("oi") or 50000.0)
        log_volume = math.log1p(max(0.0, volume))
        log_oi = math.log1p(max(0.0, oi))

        # Underlying Returns & Technicals
        spot_ret_1 = 0.0
        spot_ret_3 = 0.0
        spot_ret_6 = 0.0
        spot_rsi = 50.0
        spot_atr_pct = 0.80
        spot_vwap_dist = 0.0

        if underlying_df is not None and len(underlying_df) >= 15:
            closes = underlying_df["close"]
            curr_c = float(closes.iloc[-1])
            if len(closes) >= 2:
                spot_ret_1 = float((curr_c - closes.iloc[-2]) / closes.iloc[-2] * 100.0)
            if len(closes) >= 4:
                spot_ret_3 = float((curr_c - closes.iloc[-4]) / closes.iloc[-4] * 100.0)
            if len(closes) >= 7:
                spot_ret_6 = float((curr_c - closes.iloc[-7]) / closes.iloc[-7] * 100.0)

            spot_rsi = cls.calculate_rsi(closes, period=14)
            atr_val = cls.calculate_atr(underlying_df, period=14)
            spot_atr_pct = (atr_val / curr_c * 100.0) if curr_c > 0 else 0.80

            vwap_val = float(underlying_quote.get("vwap") or curr_c)
            spot_vwap_dist = float((curr_c - vwap_val) / vwap_val * 100.0) if vwap_val > 0 else 0.0
        else:
            vwap_val = float(underlying_quote.get("vwap") or spot)
            spot_vwap_dist = float((spot - vwap_val) / vwap_val * 100.0) if vwap_val > 0 else 0.0

        # India VIX & Macro Context
        india_vix = float(macro_context.get("india_vix") or 13.5)
        regime = str(macro_context.get("regime_verdict", "NEUTRAL_CHOP")).upper()
        macro_bullish = 1.0 if "BULL" in regime else 0.0
        macro_bearish = 1.0 if "BEAR" in regime else 0.0

        # Upstream AI Signals (Qlib + Legacy + Consensus)
        qlib_score = float(upstream_signals.get("qlib_score", 50.0))
        legacy_score = float(upstream_signals.get("legacy_score", 50.0))
        consensus_conf = float(upstream_signals.get("consensus_confidence", 50.0))
        consensus_dir = str(upstream_signals.get("consensus_direction", "NEUTRAL")).upper()

        # Is consensus direction aligned with this option?
        # CE aligned with BUY; PE aligned with SELL
        aligned = 0.0
        if (is_call and consensus_dir == "BUY") or (not is_call and consensus_dir == "SELL"):
            aligned = 1.0
        elif consensus_dir == "NEUTRAL":
            aligned = 0.5

        features = {
            "spot_price": round(spot, 2),
            "strike": round(strike, 2),
            "is_call": is_call,
            "moneyness": round(moneyness, 5),
            "abs_moneyness": round(abs_moneyness, 5),
            "dte_days": round(dte_days, 3),
            "delta": round(delta, 4),
            "gamma": round(gamma, 5),
            "theta": round(theta, 3),
            "vega": round(vega, 3),
            "iv": round(iv, 2),
            "spread_pct": round(spread_pct, 3),
            "bid_ask_imbalance": round(bid_ask_imbalance, 3),
            "log_volume": round(log_volume, 3),
            "log_oi": round(log_oi, 3),
            "spot_ret_1": round(spot_ret_1, 3),
            "spot_ret_3": round(spot_ret_3, 3),
            "spot_ret_6": round(spot_ret_6, 3),
            "spot_rsi": round(spot_rsi, 2),
            "spot_atr_pct": round(spot_atr_pct, 3),
            "spot_vwap_dist_pct": round(spot_vwap_dist, 3),
            "india_vix": round(india_vix, 2),
            "macro_bullish": macro_bullish,
            "macro_bearish": macro_bearish,
            "upstream_qlib_score": round(qlib_score, 1),
            "upstream_legacy_score": round(legacy_score, 1),
            "upstream_consensus_conf": round(consensus_conf, 1),
            "upstream_aligned": aligned,
        }
        return features

    @classmethod
    def to_feature_vector(cls, features: Dict[str, float]) -> np.ndarray:
        """Converts feature dictionary into standardized numpy array."""
        return np.array([features.get(f, 0.0) for f in FEATURE_NAMES], dtype=np.float32)
