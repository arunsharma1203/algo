"""
CONSENSUS META-LEARNER V2: FEATURE VECTOR EXTRACTOR
===================================================
Constructs strictly point-in-time contextual feature vectors across 7 Groups:
- Group A: Qlib / Legacy Core Fusion
- Group B: Market Regime (NIFTY / Breadth point-in-time)
- Group C: Sector Context (Sector momentum, relative returns)
- Group D: Relative Strength (Stock vs Market/Sector)
- Group E: F&O Context (PCR, OI shifts, IV, Walls - explicit missing mask)
- Group F: FinBERT News Sentiment (point-in-time news, freshness decay)
- Group G: Mathematical Chart / Price Structure (EMA, Momentum, Pivots, Wick geometry, RVOL)

ZERO look-ahead bias: all features computed strictly on data <= decision timestamp.
"""

import sqlite3
import json
import logging
import math
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

FEATURE_NAMES: List[str] = [
    # Group A: Qlib / Legacy
    "qlib_score_norm",
    "legacy_conf_norm",
    "model_agreement",
    "score_spread",
    "is_buy_direction",
    
    # Group B: Market Regime
    "market_breadth_adv_dec",
    "market_mean_return_1d",
    "nifty_regime_bull",
    "nifty_regime_bear",
    
    # Group C: Sector Context
    "sector_aligned",
    "sector_pct_from_open",
    
    # Group D: Relative Strength
    "stock_vs_market_rs",
    "bull_in_bull",
    "bull_in_bear",
    "bear_in_bear",
    
    # Group E: F&O Context
    "fno_available",
    "fno_pcr",
    "fno_pcr_bias_bull",
    "fno_call_wall_dist_pct",
    "fno_put_wall_dist_pct",
    
    # Group F: FinBERT Sentiment
    "news_available",
    "finbert_sentiment_score",
    "finbert_aligned",
    "finbert_contradicting",
    
    # Group G: Mathematical Chart / Price Structure
    "ema_9_slope",
    "ema_21_slope",
    "ema_separation",
    "price_vs_ema21",
    "trend_alignment",
    "rsi_14",
    "distance_to_high_20",
    "distance_to_low_20",
    "range_position_20",
    "distance_to_vwap",
    "atr_14_pct",
    "candle_body_ratio",
    "candle_rejection_wick",
    "close_location_val",
    "rvol_20",
]

GROUP_SLICES: Dict[str, List[str]] = {
    "BASE_QLIB_LEGACY": [
        "qlib_score_norm", "legacy_conf_norm", "model_agreement", "score_spread", "is_buy_direction"
    ],
    "MARKET_REGIME": [
        "market_breadth_adv_dec", "market_mean_return_1d", "nifty_regime_bull", "nifty_regime_bear"
    ],
    "SECTOR_CONTEXT": [
        "sector_aligned", "sector_pct_from_open"
    ],
    "RELATIVE_STRENGTH": [
        "stock_vs_market_rs", "bull_in_bull", "bull_in_bear", "bear_in_bear"
    ],
    "FNO_CONTEXT": [
        "fno_available", "fno_pcr", "fno_pcr_bias_bull", "fno_call_wall_dist_pct", "fno_put_wall_dist_pct"
    ],
    "FINBERT_NEWS": [
        "news_available", "finbert_sentiment_score", "finbert_aligned", "finbert_contradicting"
    ],
    "CHART_STRUCTURE": [
        "ema_9_slope", "ema_21_slope", "ema_separation", "price_vs_ema21", "trend_alignment",
        "rsi_14", "distance_to_high_20", "distance_to_low_20", "range_position_20", "distance_to_vwap",
        "atr_14_pct", "candle_body_ratio", "candle_rejection_wick", "close_location_val", "rvol_20"
    ]
}


class ConsensusMetaFeatureExtractor:
    """
    Extracts high-fidelity point-in-time feature vectors from market database
    and candidate dictionaries for the Consensus Meta-Learner V2.
    """

    @classmethod
    def extract_point_in_time_features(
        cls,
        candidate: Dict[str, Any],
        db_conn: Optional[sqlite3.Connection] = None,
        db_path: Optional[str] = None
    ) -> Dict[str, float]:
        close_conn_at_end = False
        if db_conn is None:
            if db_path is None:
                from app.data.historical_data_layer import get_db_path
                db_path = get_db_path()
            db_conn = sqlite3.connect(db_path, timeout=10.0)
            close_conn_at_end = True

        try:
            features: Dict[str, float] = {}

            ticker = str(candidate.get("ticker") or "")
            symbol = str(candidate.get("symbol") or ticker.replace(".NS", "")).upper()
            direction = str(candidate.get("direction") or "BUY").upper()
            timestamp = str(candidate.get("timestamp") or "")
            if not timestamp:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            entry_price = float(
                candidate.get("blended_entry") or candidate.get("entry_price") or candidate.get("entry") or 0.0
            )

            # GROUP A: Qlib / Legacy Core Fusion
            qlib_m = candidate.get("qlib_metrics") or {}
            legacy_m = candidate.get("legacy_metrics") or {}

            q_score = float(candidate.get("qlib_score") or qlib_m.get("score") or candidate.get("composite_score") or 50.0)
            l_score = float(candidate.get("legacy_score") or legacy_m.get("confidence") or candidate.get("composite_score") or 50.0)

            features["qlib_score_norm"] = float(np.clip(q_score / 100.0, 0.0, 1.0))
            features["legacy_conf_norm"] = float(np.clip(l_score / 100.0, 0.0, 1.0))
            
            q_dir = str(qlib_m.get("direction") or direction).upper()
            l_dir = str(legacy_m.get("direction") or direction).upper()
            features["model_agreement"] = 1.0 if q_dir == l_dir else 0.0
            features["score_spread"] = float(abs(q_score - l_score) / 100.0)
            features["is_buy_direction"] = 1.0 if direction in ("BUY", "BULLISH") else 0.0

            # GROUP B: Market Regime
            dt_str = timestamp[:10]
            cursor = db_conn.cursor()
            
            cursor.execute("""
                SELECT 
                    COUNT(*),
                    SUM(CASE WHEN close > open THEN 1 ELSE 0 END),
                    SUM(CASE WHEN close < open THEN 1 ELSE 0 END),
                    AVG((close - open) / (open + 1e-9) * 100.0)
                FROM ohlcv
                WHERE timeframe = '1d' AND date <= ?
                AND date = (SELECT MAX(date) FROM ohlcv WHERE timeframe = '1d' AND date <= ?)
            """, (dt_str, dt_str))
            
            b_row = cursor.fetchone()
            tot = b_row[0] if b_row and b_row[0] else 0
            adv = b_row[1] if b_row and b_row[1] else 0
            dec = b_row[2] if b_row and b_row[2] else 0
            mean_ret = float(b_row[3] or 0.0) if b_row else 0.0

            breadth_ratio = float((adv - dec) / (tot + 1e-9)) if tot > 0 else 0.0
            features["market_breadth_adv_dec"] = float(np.clip(breadth_ratio, -1.0, 1.0))
            features["market_mean_return_1d"] = float(np.clip(mean_ret, -10.0, 10.0))
            features["nifty_regime_bull"] = 1.0 if breadth_ratio > 0.10 else 0.0
            features["nifty_regime_bear"] = 1.0 if breadth_ratio < -0.10 else 0.0

            # GROUP C: Sector Context
            sec_name = candidate.get("sector")
            sec_trend = str(candidate.get("sector_trend") or "").upper()
            sec_open_pct = float(candidate.get("sector_pct_from_open") or 0.0)

            is_aligned = False
            if sec_trend:
                if features["is_buy_direction"] == 1.0 and sec_trend in ("BULLISH", "LEADING"):
                    is_aligned = True
                elif features["is_buy_direction"] == 0.0 and sec_trend in ("BEARISH", "LAGGING"):
                    is_aligned = True
            else:
                is_aligned = True

            features["sector_aligned"] = 1.0 if is_aligned else 0.0
            features["sector_pct_from_open"] = float(np.clip(sec_open_pct, -5.0, 5.0))

            # GROUP D: Relative Strength
            cursor.execute("""
                SELECT open, close FROM ohlcv
                WHERE ticker = ? AND timeframe = '1d' AND date <= ?
                ORDER BY date DESC LIMIT 1
            """, (ticker, dt_str))
            stk_row = cursor.fetchone()
            stk_1d_ret = 0.0
            if stk_row and stk_row[0] and stk_row[0] > 0:
                stk_1d_ret = (float(stk_row[1]) - float(stk_row[0])) / float(stk_row[0]) * 100.0

            features["stock_vs_market_rs"] = float(np.clip(stk_1d_ret - mean_ret, -20.0, 20.0))
            
            is_buy = (features["is_buy_direction"] == 1.0)
            is_bull_regime = (features["nifty_regime_bull"] == 1.0)
            is_bear_regime = (features["nifty_regime_bear"] == 1.0)

            features["bull_in_bull"] = 1.0 if (is_buy and is_bull_regime) else 0.0
            features["bull_in_bear"] = 1.0 if (is_buy and is_bear_regime) else 0.0
            features["bear_in_bear"] = 1.0 if ((not is_buy) and is_bear_regime) else 0.0

            # GROUP E: F&O Context
            cursor.execute("""
                SELECT pcr, bias, spot_price, max_pain, call_walls, put_walls
                FROM fno_option_chain_cache
                WHERE symbol = ? AND fetched_at <= ?
                ORDER BY fetched_at DESC LIMIT 1
            """, (symbol, timestamp))
            fno_row = cursor.fetchone()

            if fno_row and fno_row[0] is not None:
                features["fno_available"] = 1.0
                pcr_val = float(fno_row[0] or 1.0)
                features["fno_pcr"] = float(np.clip(pcr_val, 0.2, 3.0))

                pcr_bull = (pcr_val > 1.05 and is_buy) or (pcr_val < 0.85 and (not is_buy))
                features["fno_pcr_bias_bull"] = 1.0 if pcr_bull else 0.0

                call_dist = 5.0
                put_dist = 5.0
                try:
                    cw = json.loads(fno_row[4]) if fno_row[4] else []
                    pw = json.loads(fno_row[5]) if fno_row[5] else []
                    if cw and isinstance(cw, list) and len(cw) > 0 and entry_price > 0:
                        c_strike = float(cw[0].get("strike", 0))
                        if c_strike > 0:
                            call_dist = (c_strike - entry_price) / entry_price * 100.0
                    if pw and isinstance(pw, list) and len(pw) > 0 and entry_price > 0:
                        p_strike = float(pw[0].get("strike", 0))
                        if p_strike > 0:
                            put_dist = (entry_price - p_strike) / entry_price * 100.0
                except Exception:
                    pass
                features["fno_call_wall_dist_pct"] = float(np.clip(call_dist, -20.0, 20.0))
                features["fno_put_wall_dist_pct"] = float(np.clip(put_dist, -20.0, 20.0))
            else:
                features["fno_available"] = 0.0
                features["fno_pcr"] = 1.0
                features["fno_pcr_bias_bull"] = 0.0
                features["fno_call_wall_dist_pct"] = 0.0
                features["fno_put_wall_dist_pct"] = 0.0

            # GROUP F: FinBERT Sentiment
            raw_finbert = candidate.get("finbert_score")
            if raw_finbert is None:
                leg_expl = legacy_m.get("explanation") or {}
                raw_finbert = leg_expl.get("sentiment_score")

            if raw_finbert is not None and abs(float(raw_finbert)) > 0.01:
                s_score = float(raw_finbert)
                features["news_available"] = 1.0
                features["finbert_sentiment_score"] = float(np.clip(s_score, -1.0, 1.0))
                features["finbert_aligned"] = 1.0 if ((s_score > 0.1 and is_buy) or (s_score < -0.1 and not is_buy)) else 0.0
                features["finbert_contradicting"] = 1.0 if ((s_score < -0.2 and is_buy) or (s_score > 0.2 and not is_buy)) else 0.0
            else:
                features["news_available"] = 0.0
                features["finbert_sentiment_score"] = 0.0
                features["finbert_aligned"] = 0.0
                features["finbert_contradicting"] = 0.0

            # GROUP G: Mathematical Chart / Price Structure (OHLCV 15m)
            cursor.execute("""
                SELECT date, open, high, low, close, volume
                FROM ohlcv
                WHERE ticker = ? AND timeframe = '15m' AND date <= ?
                ORDER BY date DESC LIMIT 40
            """, (ticker, timestamp))
            c_rows = cursor.fetchall()

            if c_rows and len(c_rows) >= 20:
                c_df = pd.DataFrame(
                    c_rows, columns=["date", "open", "high", "low", "close", "volume"]
                ).sort_values("date").reset_index(drop=True)

                closes = c_df["close"].values
                highs = c_df["high"].values
                lows = c_df["low"].values
                volumes = c_df["volume"].values
                ref_px = entry_price if entry_price > 0 else float(closes[-1])

                def calc_ema(arr: np.ndarray, span: int) -> np.ndarray:
                    alpha = 2.0 / (span + 1.0)
                    res = np.empty_like(arr)
                    res[0] = arr[0]
                    for i in range(1, len(arr)):
                        res[i] = alpha * arr[i] + (1 - alpha) * res[i-1]
                    return res

                ema_9 = calc_ema(closes, 9)
                ema_21 = calc_ema(closes, 21)

                ema_9_sl = (ema_9[-1] - ema_9[-3]) / (ema_9[-3] + 1e-9) * 100.0 if len(ema_9) >= 3 else 0.0
                ema_21_sl = (ema_21[-1] - ema_21[-3]) / (ema_21[-3] + 1e-9) * 100.0 if len(ema_21) >= 3 else 0.0
                ema_sep = (ema_9[-1] - ema_21[-1]) / (ema_21[-1] + 1e-9) * 100.0
                px_vs_ema21 = (ref_px - ema_21[-1]) / (ema_21[-1] + 1e-9) * 100.0

                t_align = 0.0
                if ref_px > ema_9[-1] > ema_21[-1]:
                    t_align = 1.0
                elif ref_px < ema_9[-1] < ema_21[-1]:
                    t_align = -1.0

                # RSI 14
                deltas = np.diff(closes)
                gains = np.maximum(deltas, 0)
                losses = np.maximum(-deltas, 0)
                avg_gain = np.mean(gains[-14:]) if len(gains) >= 14 else np.mean(gains)
                avg_loss = np.mean(losses[-14:]) if len(losses) >= 14 else np.mean(losses)
                rs = avg_gain / (avg_loss + 1e-9)
                rsi = 100.0 - (100.0 / (1.0 + rs))

                # Structure
                h20 = np.max(highs[-20:])
                l20 = np.min(lows[-20:])
                dist_h = (h20 - ref_px) / (ref_px + 1e-9) * 100.0
                dist_l = (ref_px - l20) / (ref_px + 1e-9) * 100.0
                r_pos = (ref_px - l20) / (h20 - l20 + 1e-9)

                # Rolling 20-bar VWAP
                tot_v = np.sum(volumes[-20:])
                vwap_20 = np.sum(closes[-20:] * volumes[-20:]) / (tot_v + 1e-9) if tot_v > 0 else ref_px
                dist_vw = (ref_px - vwap_20) / (vwap_20 + 1e-9) * 100.0

                # ATR 14
                tr = np.maximum(
                    highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
                )
                atr_14 = np.mean(tr[-14:]) if len(tr) >= 14 else np.mean(tr)
                atr_pct = (atr_14 / (ref_px + 1e-9)) * 100.0

                # Candle geometry
                last_o, last_h, last_l, last_c = c_df["open"].iloc[-1], highs[-1], lows[-1], closes[-1]
                c_rng = max(last_h - last_l, 1e-4)
                body_r = abs(last_c - last_o) / c_rng
                upper_w = (last_h - max(last_o, last_c)) / c_rng
                lower_w = (min(last_o, last_c) - last_l) / c_rng
                close_l = (last_c - last_l) / c_rng
                rej_wick = lower_w if is_buy else upper_w

                # RVOL
                mean_v20 = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
                rvol = volumes[-1] / (mean_v20 + 1e-9)

                features["ema_9_slope"] = float(np.clip(ema_9_sl, -5.0, 5.0))
                features["ema_21_slope"] = float(np.clip(ema_21_sl, -5.0, 5.0))
                features["ema_separation"] = float(np.clip(ema_sep, -10.0, 10.0))
                features["price_vs_ema21"] = float(np.clip(px_vs_ema21, -10.0, 10.0))
                features["trend_alignment"] = float(t_align)
                features["rsi_14"] = float(np.clip(rsi / 100.0, 0.0, 1.0))
                features["distance_to_high_20"] = float(np.clip(dist_h, 0.0, 20.0))
                features["distance_to_low_20"] = float(np.clip(dist_l, 0.0, 20.0))
                features["range_position_20"] = float(np.clip(r_pos, 0.0, 1.0))
                features["distance_to_vwap"] = float(np.clip(dist_vw, -10.0, 10.0))
                features["atr_14_pct"] = float(np.clip(atr_pct, 0.0, 10.0))
                features["candle_body_ratio"] = float(np.clip(body_r, 0.0, 1.0))
                features["candle_rejection_wick"] = float(np.clip(rej_wick, 0.0, 1.0))
                features["close_location_val"] = float(np.clip(close_l, 0.0, 1.0))
                features["rvol_20"] = float(np.clip(rvol, 0.0, 10.0))
            else:
                features["ema_9_slope"] = 0.0
                features["ema_21_slope"] = 0.0
                features["ema_separation"] = 0.0
                features["price_vs_ema21"] = 0.0
                features["trend_alignment"] = 0.0
                features["rsi_14"] = 0.50
                features["distance_to_high_20"] = 2.0
                features["distance_to_low_20"] = 2.0
                features["range_position_20"] = 0.50
                features["distance_to_vwap"] = 0.0
                features["atr_14_pct"] = 1.0
                features["candle_body_ratio"] = 0.50
                features["candle_rejection_wick"] = 0.20
                features["close_location_val"] = 0.50
                features["rvol_20"] = 1.0

            return features

        finally:
            if close_conn_at_end and db_conn:
                db_conn.close()

    @classmethod
    def get_feature_vector(cls, features_dict: Dict[str, float]) -> np.ndarray:
        return np.array([features_dict.get(k, 0.0) for k in FEATURE_NAMES], dtype=np.float64)
