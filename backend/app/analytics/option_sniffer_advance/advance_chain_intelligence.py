"""
Option Sniffer Advance: Institutional Cross-Sectional Option Chain & Microstructure Intelligence.

Computes real-time institutional quant volatility & order-flow metrics from the shared
in-memory Upstox Market Data Feed V3 (`OptionStreamManager`) and completed 1-minute index bars
(`continuous_index_ohlcv`) with ZERO additional Upstox REST API calls and ZERO look-ahead bias:
  1. Net Dealer Gamma Exposure (`GEX`) & Gamma Regime (`LONG_GAMMA_PINNING` vs `SHORT_GAMMA_ACCELERATION`)
  2. Variance Risk Premium (`VRP = ATM_IV - RV_15m_Annualized`)
  3. Put-Call Parity Synthetic Future (`F_syn = K_ATM + C_mid - P_mid`) & Synthetic Basis (`bps`)
  4. Cross-Strike Open Interest & Volume Put-Call Ratios (`PCR_OI`, `PCR_Volume`)
  5. Institutional OI Walls (`Call_Wall_Strike`, `Put_Wall_Strike`, `Max_Pain_Strike`)
  6. 25-Delta IV Skew (`IV_OTM_PE - IV_OTM_CE`) & Cross-Chain Order Book Imbalance (`Chain_OBI`)
  7. Point-in-Time Causal Wilder `ADX(14)`, `Kaufman ER(14)`, `ATR_Ratio(5/20)`, and `Session TPB`.
"""

import logging
import math
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from app.analytics.option_sniffer.option_regime_gate import (
    compute_atr_ratio,
    compute_kaufman_er,
    compute_wilder_adx_canonical,
)
from app.analytics.option_sniffer_advance.advance_constants import (
    CONTRACTION_ATR_RATIO_MAX,
    CONTRACTION_ER_MAX,
    DEFAULT_LOT_SIZES,
    STRATEGY_CONTRACTION_THETA_SELLER,
    STRATEGY_WAVE1_GAMMA_SNIPER,
    STRIKE_INTERVALS,
    to_ist_naive,
)
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)


class AdvanceChainIntelligence:
    """Computes point-in-time cross-sectional option chain and underlying regime intelligence."""

    @staticmethod
    def _normalize_iv_pct(raw_iv: float) -> float:
        """Normalizes Upstox IV (which may be decimal 0.16 or percentage 16.0) to percentage [1.0, 150.0]."""
        val = float(raw_iv or 0.0)
        if val <= 0:
            return 14.5
        if val < 1.5:
            return round(val * 100.0, 2)
        return round(val, 2)

    @classmethod
    def compute_causal_index_technicals(
        cls,
        underlying: str = "NIFTY",
        as_of_time: Optional[datetime] = None,
        fallback_spot: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Computes strictly causal point-in-time index technicals from completed 1m bars
        in `continuous_index_ohlcv` strictly prior to `as_of_time`.
        """
        now_dt = to_ist_naive(as_of_time)
        date_str = now_dt.strftime("%Y-%m-%d")
        cutoff_str = now_dt.strftime("%Y-%m-%d %H:%M:00")
        u_sym = str(underlying or "NIFTY").upper().strip()

        bars: List[Dict[str, Any]] = []
        try:
            with sqlite3.connect(get_db_path(), timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT timestamp, open, high, low, close, volume
                    FROM continuous_index_ohlcv
                    WHERE ticker = ?
                      AND timestamp >= ?
                      AND timestamp < ?
                    ORDER BY timestamp ASC
                    """,
                    (u_sym, f"{date_str} 09:15:00", cutoff_str),
                ).fetchall()
                bars = [dict(r) for r in rows]
        except Exception as e:
            logger.debug(f"[AdvanceChainIntelligence] Index bars query note: {e}")

        # If running on weekend/off-hours with 0 bars today, load most recent completed session for telemetry display
        is_historical_display = False
        if len(bars) < 5 and as_of_time is None:
            try:
                with sqlite3.connect(get_db_path(), timeout=5.0) as conn:
                    conn.row_factory = sqlite3.Row
                    last_d_row = conn.execute(
                        "SELECT substr(MAX(timestamp), 1, 10) FROM continuous_index_ohlcv WHERE ticker = ?",
                        (u_sym,),
                    ).fetchone()
                    if last_d_row and last_d_row[0]:
                        last_d = last_d_row[0]
                        rows = conn.execute(
                            """
                            SELECT timestamp, open, high, low, close, volume
                            FROM continuous_index_ohlcv
                            WHERE ticker = ? AND timestamp >= ? AND timestamp <= ?
                            ORDER BY timestamp ASC
                            """,
                            (u_sym, f"{last_d} 09:15:00", f"{last_d} 15:30:00"),
                        ).fetchall()
                        bars = [dict(r) for r in rows]
                        is_historical_display = True
            except Exception:
                pass

        n_bars = len(bars)
        if n_bars >= 28:
            highs = np.array([float(b["high"]) for b in bars], dtype=float)
            lows = np.array([float(b["low"]) for b in bars], dtype=float)
            closes = np.array([float(b["close"]) for b in bars], dtype=float)

            adx_res = compute_wilder_adx_canonical(highs, lows, closes, 14)
            adx_14 = round(float(adx_res[0]), 2) if adx_res else 0.0
            plus_di = round(float(adx_res[1]), 2) if adx_res else 0.0
            minus_di = round(float(adx_res[2]), 2) if adx_res else 0.0

            er_14 = round(float(compute_kaufman_er(closes, 14) or 0.0), 3)
            atr_rat = round(float(compute_atr_ratio(highs, lows, closes, 5, 20) or 1.0), 3)
            tpb = round(float(np.mean((highs + lows + closes) / 3.0)), 2)
            spot = float(closes[-1]) if fallback_spot <= 0 else fallback_spot

            # Annualized 15-minute Realized Volatility (RV_15m_ann) from 1-minute log returns
            recent_c = closes[-16:]
            log_rets = np.diff(np.log(np.maximum(recent_c, 1.0)))
            rv_15m_ann = (
                round(float(np.std(log_rets, ddof=1) * math.sqrt(252.0 * 375.0) * 100.0), 2)
                if len(log_rets) >= 3
                else 12.0
            )
            tr_arr = np.maximum(
                highs[1:] - lows[1:],
                np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
            )
            atr_14_pts = round(float(np.mean(tr_arr[-14:])), 2) if len(tr_arr) >= 14 else 15.0
        else:
            spot = fallback_spot
            adx_14, plus_di, minus_di = 0.0, 0.0, 0.0
            er_14 = 0.0
            atr_rat = 1.0
            tpb = spot
            rv_15m_ann = 12.5
            atr_14_pts = 15.0

        return {
            "underlying": u_sym,
            "completed_bars_count": 0 if is_historical_display else n_bars,
            "display_bars_count": n_bars,
            "spot_price": round(spot, 2),
            "session_tpb": tpb,
            "adx_14": adx_14,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "kaufman_er": er_14,
            "atr_ratio": atr_rat,
            "atr_14_pts": atr_14_pts,
            "rv_15m_ann": rv_15m_ann,
        }

    @classmethod
    def compute_chain_intelligence(
        cls,
        underlying: str = "NIFTY",
        option_quotes: Optional[List[Dict[str, Any]]] = None,
        underlying_quote: Optional[Dict[str, Any]] = None,
        as_of_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Computes cross-sectional institutional option chain analytics for `underlying`:
          - Net Dealer GEX (`net_gex`) & `gex_regime`
          - ATM IV, Realized Volatility (`rv_15m_ann`), and Variance Risk Premium (`vrp = atm_iv - rv_15m_ann`)
          - Put-Call Parity Synthetic Future (`synthetic_future`) & Basis (`synthetic_basis_bps`)
          - Cross-Strike Open Interest & Volume PCR (`pcr_oi`, `pcr_volume`)
          - Call OI Wall (`call_wall_strike`), Put OI Wall (`put_wall_strike`), and Max Pain (`max_pain_strike`)
          - 25-Delta IV Skew (`iv_skew_25d`) and Order Book Imbalance (`chain_obi`)
        """
        u_sym = str(underlying or "NIFTY").upper().strip()
        lot_size = DEFAULT_LOT_SIZES.get(u_sym, 65)
        strike_int = STRIKE_INTERVALS.get(u_sym, 50.0)

        if option_quotes is None or underlying_quote is None:
            try:
                from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager

                sm = OptionStreamManager()
                if option_quotes is None:
                    option_quotes = sm.get_all_active_option_quotes()
                if underlying_quote is None:
                    underlying_quote = sm.get_underlying_quote(u_sym) or {}
            except Exception:
                option_quotes = option_quotes or []
                underlying_quote = underlying_quote or {}

        u_quotes = [
            q for q in (option_quotes or []) if str(q.get("underlying") or "").upper().strip() == u_sym
        ]
        raw_spot = float((underlying_quote or {}).get("spot_ltp") or 0.0)
        idx_tech = cls.compute_causal_index_technicals(
            underlying=u_sym, as_of_time=as_of_time, fallback_spot=raw_spot
        )
        spot = float(idx_tech["spot_price"] or raw_spot or (52000.0 if "BANK" in u_sym else 22800.0))

        ce_quotes = [q for q in u_quotes if str(q.get("option_type") or "").upper() == "CE"]
        pe_quotes = [q for q in u_quotes if str(q.get("option_type") or "").upper() == "PE"]

        # 1. Cross-Strike PCR (OI & Volume) and Call/Put OI Walls
        total_ce_oi = sum(float(q.get("oi") or 0.0) for q in ce_quotes)
        total_pe_oi = sum(float(q.get("oi") or 0.0) for q in pe_quotes)
        pcr_oi = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        total_ce_vol = sum(float(q.get("volume") or 0.0) for q in ce_quotes)
        total_pe_vol = sum(float(q.get("volume") or 0.0) for q in pe_quotes)
        pcr_volume = round(total_pe_vol / total_ce_vol, 2) if total_ce_vol > 0 else 1.0

        call_wall_strike = (
            float(max(ce_quotes, key=lambda q: float(q.get("oi") or 0.0)).get("strike") or 0.0)
            if ce_quotes
            else round(spot / strike_int) * strike_int + (2 * strike_int)
        )
        put_wall_strike = (
            float(max(pe_quotes, key=lambda q: float(q.get("oi") or 0.0)).get("strike") or 0.0)
            if pe_quotes
            else round(spot / strike_int) * strike_int - (2 * strike_int)
        )

        # 2. Net Dealer Gamma Exposure (GEX in ₹ Crores per 1% spot move)
        # Dealer assumption: Market makers are net long calls (sold by covered/overwrite or pinned) and net short puts
        net_gex_raw = 0.0
        for q in u_quotes:
            gamma = float(q.get("gamma") or 0.0012)
            oi = float(q.get("oi") or 0.0)
            sign = 1.0 if str(q.get("option_type") or "").upper() == "CE" else -1.0
            net_gex_raw += sign * gamma * oi * lot_size * (spot ** 2) * 0.01 / 1e7
        net_gex = round(net_gex_raw, 2)
        if net_gex > 5.0:
            gex_regime = "LONG_GAMMA_PINNING"
        elif net_gex < -5.0:
            gex_regime = "SHORT_GAMMA_ACCELERATION"
        else:
            gex_regime = "GAMMA_NEUTRAL_TRANSITION"

        # 3. ATM Implied Volatility, Variance Risk Premium (VRP), and 25-Delta IV Skew
        atm_strike = round(spot / strike_int) * strike_int
        nearest_quotes = sorted(u_quotes, key=lambda q: abs(float(q.get("strike") or spot) - spot))[:2]
        if nearest_quotes:
            atm_iv = round(
                float(np.mean([cls._normalize_iv_pct(q.get("iv") or 14.5) for q in nearest_quotes])),
                2,
            )
        else:
            atm_iv = 14.5

        rv_15m_ann = float(idx_tech["rv_15m_ann"])
        vrp = round(atm_iv - rv_15m_ann, 2)

        otm_pe = [q for q in pe_quotes if float(q.get("strike") or spot) < spot]
        otm_ce = [q for q in ce_quotes if float(q.get("strike") or spot) > spot]
        pe_otm_iv = (
            float(np.mean([cls._normalize_iv_pct(q.get("iv")) for q in otm_pe])) if otm_pe else atm_iv
        )
        ce_otm_iv = (
            float(np.mean([cls._normalize_iv_pct(q.get("iv")) for q in otm_ce])) if otm_ce else atm_iv
        )
        iv_skew_25d = round(pe_otm_iv - ce_otm_iv, 2)

        # 4. Put-Call Parity Synthetic Future (F_syn = K + C_mid - P_mid) & Synthetic Basis (bps)
        ce_by_k = {float(q.get("strike") or 0.0): q for q in ce_quotes}
        pe_by_k = {float(q.get("strike") or 0.0): q for q in pe_quotes}
        common_strikes = sorted(
            set(ce_by_k.keys()).intersection(set(pe_by_k.keys())),
            key=lambda k: abs(k - spot),
        )
        if common_strikes:
            k_ref = common_strikes[0]
            c_q = ce_by_k[k_ref]
            p_q = pe_by_k[k_ref]
            c_mid = (float(c_q.get("bid") or c_q.get("ltp") or 0.0) + float(c_q.get("ask") or c_q.get("ltp") or 0.0)) / 2.0
            p_mid = (float(p_q.get("bid") or p_q.get("ltp") or 0.0) + float(p_q.get("ask") or p_q.get("ltp") or 0.0)) / 2.0
            synthetic_future = round(k_ref + c_mid - p_mid, 2)
        else:
            synthetic_future = round(spot, 2)
        synthetic_basis_bps = round(((synthetic_future - spot) / spot) * 10000.0, 2) if spot > 0 else 0.0

        # 5. Max Pain Strike Estimation across monitored strikes
        all_strikes = sorted({float(q.get("strike") or 0.0) for q in u_quotes if float(q.get("strike") or 0.0) > 0})
        if all_strikes:
            best_mp = atm_strike
            min_payout = float("inf")
            for k_test in all_strikes:
                payout = 0.0
                for q in ce_quotes:
                    k_c = float(q.get("strike") or 0.0)
                    if k_test > k_c:
                        payout += (k_test - k_c) * float(q.get("oi") or 0.0)
                for q in pe_quotes:
                    k_p = float(q.get("strike") or 0.0)
                    if k_test < k_p:
                        payout += (k_p - k_test) * float(q.get("oi") or 0.0)
                if payout < min_payout:
                    min_payout = payout
                    best_mp = k_test
            max_pain_strike = best_mp
        else:
            max_pain_strike = atm_strike

        # 6. Cross-Chain Order Book Imbalance (OBI_CE - OBI_PE)
        def _book_obi(qs: List[Dict[str, Any]]) -> float:
            b_sum = sum(float(q.get("bid_qty") or 100.0) for q in qs)
            a_sum = sum(float(q.get("ask_qty") or 100.0) for q in qs)
            return (b_sum - a_sum) / (b_sum + a_sum + 1.0) if qs else 0.0

        chain_obi = round(_book_obi(ce_quotes) - _book_obi(pe_quotes), 3)

        # 7. Active Strategy Mode Recommendation based on Causal Regime
        atr_rat = float(idx_tech["atr_ratio"])
        er_val = float(idx_tech["kaufman_er"])
        adx_val = float(idx_tech["adx_14"])

        if atr_rat <= CONTRACTION_ATR_RATIO_MAX and er_val < CONTRACTION_ER_MAX:
            active_strategy_mode = STRATEGY_CONTRACTION_THETA_SELLER
            regime_summary = (
                f"Volatility Contraction (ATR Ratio {atr_rat:.2f} <= {CONTRACTION_ATR_RATIO_MAX}, ER {er_val:.2f}). "
                f"Short Book (1-Lot SELL Theta/VRP Harvester) active."
            )
        elif adx_val >= 20.0 and er_val >= 0.30:
            active_strategy_mode = STRATEGY_WAVE1_GAMMA_SNIPER
            regime_summary = (
                f"Trend Expansion (ADX {adx_val:.1f}, ER {er_val:.2f}, ATR Ratio {atr_rat:.2f}). "
                f"Long Book (1-Lot BUY Wave-1 Gamma Sniper) + Diagonal OTM Short Wing active."
            )
        else:
            active_strategy_mode = "DUAL_BOOK_SELECTIVE_SURVEILLANCE"
            regime_summary = (
                f"Selective Dual-Book Surveillance (ADX {adx_val:.1f}, ER {er_val:.2f}, VRP {vrp:+.1f}%). "
                f"Monitoring for Wave-1 breakout or Wave-2 exhaustion."
            )

        return {
            "timestamp": to_ist_naive(as_of_time).isoformat(),
            "underlying": u_sym,
            "spot_price": round(spot, 2),
            "session_tpb": idx_tech["session_tpb"],
            "completed_bars_count": idx_tech["completed_bars_count"],
            "display_bars_count": idx_tech["display_bars_count"],
            "synthetic_future": synthetic_future,
            "synthetic_basis_bps": synthetic_basis_bps,
            "atm_iv": atm_iv,
            "rv_15m_ann": rv_15m_ann,
            "vrp": vrp,
            "iv_skew_25d": iv_skew_25d,
            "net_gex": net_gex,
            "gex_regime": gex_regime,
            "pcr_oi": pcr_oi,
            "pcr_volume": pcr_volume,
            "call_wall_strike": call_wall_strike,
            "put_wall_strike": put_wall_strike,
            "max_pain_strike": max_pain_strike,
            "chain_obi": chain_obi,
            "adx_14": adx_val,
            "plus_di": idx_tech["plus_di"],
            "minus_di": idx_tech["minus_di"],
            "kaufman_er": er_val,
            "atr_ratio": atr_rat,
            "atr_14_pts": idx_tech["atr_14_pts"],
            "active_strategy_mode": active_strategy_mode,
            "regime_summary": regime_summary,
        }
