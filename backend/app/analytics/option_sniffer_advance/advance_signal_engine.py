"""
Option Sniffer Advance: Institutional Dual-Book Signal Engine & Regime Router.

Evaluates candidate option contracts through causal microstructure, option chain intelligence,
and the Option Alpha Model, routing high-conviction setups into two complementary books:
  1. Book 1 (`LONG_BOOK_BUY` — 1 Lot BUY):
     - Wave-1 Gamma Sniper (`WAVE_1_GAMMA_SNIPER`) during confirmed directional trend expansion
       (>= 28 contiguous 1m bars, ADX >= 20, Kaufman ER >= 0.30 or CRB-I breakout, ATR Ratio > 0.85,
       Post-Harvest 60m Guard, 1 Long position per underlying).
  2. Book 2 (`SHORT_BOOK_SELL` — 1 Lot SELL):
     - Activates when a contract exhibits structural Theta/VRP/Exhaustion overpricing:
       * Mode S1 (`CONTRACTION_THETA_SELLER`): Volatility Contraction (ATR Ratio <= 0.85 & ER < 0.40)
       * Mode S2 (`WAVE_2_EXHAUSTION_SELLER`): Post-Harvest Wave-2 Exhaustion within 60m after a
         >= +15% MFE Wave-1 winner closes, when ATR Ratio <= 0.93 or ER <= 0.20.
       * Mode S3 (`DIAGONAL_OTM_WING_SELLER`): Late OTM Extension when a Wave-1 Long trade on the
         same underlying has been open >= 8 minutes (480s) and a far-OTM strike (> 1.5 strikes OTM)
         spikes into retail skew overpricing.
"""

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.analytics.option_sniffer.option_constants import (
    DEFAULT_LOT_SIZES,
    MAX_QUOTE_AGE_MS,
    MAX_SPREAD_PCT,
    MIN_BID_PRICE,
    MIN_OI,
    MIN_VOLUME,
    STATE_ACTIVE,
    STATE_QUALIFIED,
    STATE_WATCH,
    STRIKE_INTERVALS,
    is_option_entry_window,
    is_option_trading_window,
    to_ist_naive,
)
from app.analytics.option_sniffer.option_feature_extractor import OptionFeatureExtractor
from app.analytics.option_sniffer_advance.advance_chain_intelligence import AdvanceChainIntelligence
from app.analytics.option_sniffer_advance.advance_constants import (
    BOOK_LONG_BUY,
    BOOK_SHORT_SELL,
    CONTRACTION_ATR_RATIO_MAX,
    CONTRACTION_ER_MAX,
    DIAGONAL_MIN_LONG_ELAPSED_SECONDS,
    DIAGONAL_MIN_OTM_STRIKES,
    DIRECTION_BUY_CE,
    DIRECTION_BUY_PE,
    DIRECTION_SELL_CE,
    DIRECTION_SELL_PE,
    LONG_MAX_PULLBACK_FROM_HIGH_PTS_BANKNIFTY,
    LONG_MAX_PULLBACK_FROM_HIGH_PTS_NIFTY,
    MIN_CAUSAL_WARMUP_BARS,
    POST_HARVEST_COOLDOWN_SECONDS,
    POST_HARVEST_EXHAUSTION_ATR_RATIO_MAX,
    POST_HARVEST_EXHAUSTION_ER_MAX,
    SHORT_MAX_ITM_STRIKES,
    SHORT_MAX_OTM_STRIKES,
    SHORT_MIN_ENTRY_BID_PRICE,
    SHORT_MIN_OTM_STRIKES,
    STRATEGY_CONTRACTION_THETA_SELLER,
    STRATEGY_DIAGONAL_OTM_WING_SELLER,
    STRATEGY_WAVE1_GAMMA_SNIPER,
    STRATEGY_WAVE2_EXHAUSTION_SELLER,
    SUPER_TREND_AFTERNOON_MIN_ADX,
    SUPER_TREND_AFTERNOON_MIN_ATR_RATIO,
    SUPER_TREND_AFTERNOON_MIN_DTE_DAYS,
    SUPER_TREND_AFTERNOON_MIN_ER,
    SUPER_TREND_AFTERNOON_OVERRIDE_ENABLED,
    is_advance_entry_window,
)
from app.analytics.option_sniffer_advance.advance_db import AdvanceDB
from app.analytics.option_sniffer_advance.advance_paper_execution import AdvancePaperExecution

logger = logging.getLogger(__name__)


class AdvanceSignalEngine:
    """Institutional Dual-Book Regime Router & Signal Evaluator."""

    _COOLDOWNS: Dict[str, float] = {}
    _ACTIVE_UNDERLYING_LOCKS: Dict[str, float] = {}

    def __init__(self, model: Optional[OptionAlphaModel] = None):
        self.model = model or OptionAlphaModel()

    @classmethod
    def reset_state(cls) -> None:
        cls._COOLDOWNS.clear()
        cls._ACTIVE_UNDERLYING_LOCKS.clear()

    @classmethod
    def record_exit_cooldown(
        cls,
        book_type: str,
        instrument_key: str,
        as_of_time: Optional[datetime] = None,
    ) -> None:
        """Stamps per-contract cooldown on trade exit so same-sweep re-entry is impossible."""
        if not instrument_key:
            return
        now_epoch = to_ist_naive(as_of_time).timestamp()
        cls._COOLDOWNS[f"{book_type}_{instrument_key}"] = now_epoch

    def evaluate_contract(
        self,
        contract: Dict[str, Any],
        quote: Dict[str, Any],
        underlying_quote: Dict[str, Any],
        chain_intel: Optional[Dict[str, Any]] = None,
        underlying_df: Optional[Any] = None,
        macro_context: Optional[Dict[str, Any]] = None,
        upstream_signals: Optional[Dict[str, Any]] = None,
        as_of_time: Optional[datetime] = None,
        enforce_cutoff: bool = True,
    ) -> Dict[str, Any]:
        """
        Evaluates a single candidate option contract and routes it to either:
          - Book 1 (`LONG_BOOK_BUY` — 1 Lot BUY)
          - Book 2 (`SHORT_BOOK_SELL` — 1 Lot SELL)
        """
        now_dt = to_ist_naive(as_of_time)
        now_epoch = now_dt.timestamp()
        ikey = contract.get("instrument_key", "")
        tsym = contract.get("trading_symbol", ikey)
        u_sym = str(contract.get("underlying") or "NIFTY").upper().strip()
        opt_type = str(contract.get("option_type") or "CE").upper().strip()
        strike = float(contract.get("strike") or 0.0)
        expiry = str(contract.get("expiry") or "")
        lot_size = int(contract.get("lot_size") or DEFAULT_LOT_SIZES.get(u_sym, 65))

        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
        ltp = float(quote.get("ltp") or ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0))
        spread_pct = float(quote.get("spread_pct") or (((ask - bid) / ltp * 100.0) if ltp > 0 else 999.0))
        oi = float(quote.get("oi") or 0.0)
        vol = float(quote.get("volume") or 0.0)
        age_ms = int(quote.get("quote_age_ms") or 0)
        spot_ltp = float(underlying_quote.get("spot_ltp") or 0.0)

        # 1. Cross-Sectional Chain & Causal Index Intelligence
        if chain_intel is None:
            chain_intel = AdvanceChainIntelligence.compute_chain_intelligence(
                underlying=u_sym,
                underlying_quote=underlying_quote,
                as_of_time=now_dt,
            )

        adx_14 = float(chain_intel.get("adx_14") or 0.0)
        plus_di = float(chain_intel.get("plus_di") or 0.0)
        minus_di = float(chain_intel.get("minus_di") or 0.0)
        kaufman_er = float(chain_intel.get("kaufman_er") or 0.0)
        atr_ratio = float(chain_intel.get("atr_ratio") or 1.0)
        net_gex = float(chain_intel.get("net_gex") or 0.0)
        vrp = float(chain_intel.get("vrp") or 0.0)
        rv_15m_ann = float(chain_intel.get("rv_15m_ann") or chain_intel.get("rv_15m") or 12.0)
        syn_basis = float(chain_intel.get("synthetic_basis_bps") or 0.0)
        pcr_oi = float(chain_intel.get("pcr_oi") or 1.0)
        completed_bars = int(chain_intel.get("completed_bars_count") or 0)
        session_tpb = float(chain_intel.get("session_tpb") or spot_ltp)
        orb_high = float(chain_intel.get("orb_high") or 0.0)
        orb_low = float(chain_intel.get("orb_low") or 0.0)
        day_high = float(chain_intel.get("day_high") or underlying_quote.get("high") or 0.0)
        day_low = float(chain_intel.get("day_low") or underlying_quote.get("low") or 0.0)
        high_bar_idx = int(chain_intel.get("high_bar_idx") or 0)
        low_bar_idx = int(chain_intel.get("low_bar_idx") or 0)
        range_10m_pts = float(chain_intel.get("range_10m_pts") or 0.0)
        range_10m_pct = float(chain_intel.get("range_10m_pct") or 0.0)
        call_wall_strike = float(chain_intel.get("call_wall_strike") or 0.0)
        put_wall_strike = float(chain_intel.get("put_wall_strike") or 0.0)

        dte_days = 3
        if expiry:
            try:
                exp_dt = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
                dte_days = (exp_dt - now_dt.date()).days
            except Exception:
                dte_days = 3

        breakout_buf = 12.0 if "BANK" in u_sym else 4.5
        has_orb = (orb_high > 0.0) and (orb_low > 0.0)
        has_day_extrema = (day_high > 0.0) and (day_low > 0.0) and (day_high > day_low)
        is_day_high_breakout = has_day_extrema and (spot_ltp >= day_high - breakout_buf)
        is_day_low_breakdown = has_day_extrema and (spot_ltp <= day_low + breakout_buf)
        rally_from_low = ((spot_ltp - day_low) / day_low * 100.0) if has_day_extrema else 0.0
        pullback_from_high = ((day_high - spot_ltp) / day_high * 100.0) if has_day_extrema else 0.0

        is_v_bottom_recovery = (
            (rally_from_low >= 0.35)
            and (low_bar_idx > high_bar_idx)
            and (pullback_from_high <= 0.15)
            and (spot_ltp >= session_tpb)
        )
        is_v_top_rejection = (
            (pullback_from_high >= 0.35)
            and (high_bar_idx > low_bar_idx)
            and (rally_from_low <= 0.15)
            and (spot_ltp <= session_tpb)
        )
        is_orb_broken_ce = (not has_orb) or (spot_ltp >= orb_high) or is_v_bottom_recovery
        is_orb_broken_pe = (not has_orb) or (spot_ltp <= orb_low) or is_v_top_rejection

        strike_int = float(STRIKE_INTERVALS.get(u_sym, 50.0))
        if spot_ltp > 0 and strike > 0:
            otm_strikes = round(
                ((strike - spot_ltp) / strike_int) if opt_type == "CE" else ((spot_ltp - strike) / strike_int),
                2,
            )
        else:
            otm_strikes = 0.0

        # 2. Check Active Dual-Book Positions & Post-Harvest Cooldown State
        open_trades = AdvanceDB.get_open_trades()
        u_open_long = [
            t for t in open_trades if t.get("book_type") == BOOK_LONG_BUY and str(t.get("underlying") or "").upper() == u_sym
        ]
        u_open_short = [
            t for t in open_trades if t.get("book_type") == BOOK_SHORT_SELL and str(t.get("underlying") or "").upper() == u_sym
        ]
        is_long_open = len(u_open_long) > 0
        is_short_open = len(u_open_short) > 0
        parent_long_entry_time = str(u_open_long[0].get("entry_time") or "") if is_long_open else ""
        parent_long_opt_type = str(u_open_long[0].get("option_type") or "").upper().strip() if is_long_open else ""
        parent_long_mfe = float(u_open_long[0].get("mfe_pct") or 0.0) if is_long_open else 0.0
        parent_long_mae = float(u_open_long[0].get("mae_pct") or 0.0) if is_long_open else 0.0
        parent_long_net_pnl = float(u_open_long[0].get("net_pnl") or 0.0) if is_long_open else 0.0
        parent_has_live_ticks = (parent_long_mfe > 0.0) or (parent_long_mae < 0.0)
        parent_is_profitable = (not parent_has_live_ticks) or (parent_long_mfe >= 10.0 and parent_long_net_pnl >= 0.0)

        long_open_elapsed_sec = 99999.0
        if is_long_open:
            try:
                l_et = datetime.fromisoformat(parent_long_entry_time.replace("T", " ")[:19])
                long_open_elapsed_sec = max(0.0, (now_dt - l_et).total_seconds())
            except Exception:
                long_open_elapsed_sec = 0.0

        recent_harvest = AdvanceDB.get_recent_harvested_long_wave(
            underlying=u_sym,
            option_type=opt_type,
            as_of_time=now_dt,
            min_mfe_pct=15.0,
            cooldown_seconds=POST_HARVEST_COOLDOWN_SECONDS,
        )
        opp_type_for_harvest = "CE" if opt_type == "PE" else "PE"
        recent_opp_harvest = AdvanceDB.get_recent_harvested_long_wave(
            underlying=u_sym,
            option_type=opp_type_for_harvest,
            as_of_time=now_dt,
            min_mfe_pct=15.0,
            cooldown_seconds=POST_HARVEST_COOLDOWN_SECONDS,
        )
        is_post_harvest_after_close = recent_harvest is not None

        # Trend-Aligned Post-Harvest Put/Call Selling:
        # In a strong Bull trend (+DI - -DI >= 8.0 and Spot > Session TPB), after a CE Wave-1 harvest,
        # selling an OTM PE (0.60 <= otm_strikes <= 2.50) harvests volatility contraction with the trend at our back!
        is_trend_aligned_wave2_short = (
            (recent_opp_harvest is not None)
            and (otm_strikes >= SHORT_MIN_OTM_STRIKES)
            and (otm_strikes <= SHORT_MAX_OTM_STRIKES)
            and (atr_ratio <= POST_HARVEST_EXHAUSTION_ATR_RATIO_MAX)
            and (
                (opt_type == "PE" and (plus_di - minus_di) >= 8.0 and spot_ltp >= session_tpb)
                or (opt_type == "CE" and (minus_di - plus_di) >= 8.0 and spot_ltp <= session_tpb)
            )
        )
        active_wave2_harvest = recent_harvest if is_post_harvest_after_close else recent_opp_harvest

        # 3. Institutional Dual-Book Regime Classification
        can_short_extrinsic = (
            bid >= SHORT_MIN_ENTRY_BID_PRICE
            and ((otm_strikes >= -SHORT_MAX_ITM_STRIKES) or (kaufman_er <= 0.15))
        )

        is_contraction_short = (
            (not is_long_open)
            and (atr_ratio <= CONTRACTION_ATR_RATIO_MAX)
            and (kaufman_er < CONTRACTION_ER_MAX)
        )
        is_wave2_exhaustion_short = (
            is_post_harvest_after_close
            and (atr_ratio <= POST_HARVEST_EXHAUSTION_ATR_RATIO_MAX or kaufman_er <= POST_HARVEST_EXHAUSTION_ER_MAX)
        ) or is_trend_aligned_wave2_short
        is_diagonal_type_mismatch = (
            is_long_open
            and (long_open_elapsed_sec >= DIAGONAL_MIN_LONG_ELAPSED_SECONDS)
            and (otm_strikes >= DIAGONAL_MIN_OTM_STRIKES)
            and bool(parent_long_opt_type)
            and (opt_type != parent_long_opt_type)
        )
        is_diagonal_otm_short = (
            is_long_open
            and (not parent_long_opt_type or opt_type == parent_long_opt_type)
            and (long_open_elapsed_sec >= DIAGONAL_MIN_LONG_ELAPSED_SECONDS)
            and (otm_strikes >= DIAGONAL_MIN_OTM_STRIKES)
            and parent_is_profitable
        )

        is_short_regime = can_short_extrinsic and (
            is_contraction_short or is_wave2_exhaustion_short or is_diagonal_otm_short or is_diagonal_type_mismatch
        )

        # 13:15–14:15 IST Super-Trend Institutional Breakout Override (Long Book Wave-1 Only)
        is_super_trend_afternoon_breakout = (
            (not is_short_regime)
            and SUPER_TREND_AFTERNOON_OVERRIDE_ENABLED
            and (not is_option_entry_window(now_dt))
            and is_advance_entry_window(now_dt, allow_super_trend_afternoon=True)
            and (is_day_high_breakout if opt_type == "CE" else is_day_low_breakdown)
            and (kaufman_er >= SUPER_TREND_AFTERNOON_MIN_ER)
            and (adx_14 >= SUPER_TREND_AFTERNOON_MIN_ADX)
            and (atr_ratio >= SUPER_TREND_AFTERNOON_MIN_ATR_RATIO)
            and (dte_days >= SUPER_TREND_AFTERNOON_MIN_DTE_DAYS)
        )

        # 4. Derive Causal Intraday Directional Alignment when upstream_signals is absent/neutral
        effective_upstream = dict(upstream_signals or {})
        c_dir = str(effective_upstream.get("consensus_direction") or "").upper().strip()
        if not c_dir or c_dir == "NEUTRAL":
            u_open = float(underlying_quote.get("open") or spot_ltp)
            u_vwap = float(underlying_quote.get("vwap") or u_open or spot_ltp)
            if is_short_regime:
                effective_upstream["consensus_direction"] = "BULLISH" if opt_type == "CE" else "BEARISH"
                effective_upstream.setdefault("consensus_confidence", 65.0)
            else:
                if plus_di > 0 or minus_di > 0:
                    if plus_di > minus_di:
                        effective_upstream["consensus_direction"] = "BULLISH"
                        effective_upstream.setdefault("consensus_confidence", 65.0)
                    elif minus_di > plus_di:
                        effective_upstream["consensus_direction"] = "BEARISH"
                        effective_upstream.setdefault("consensus_confidence", 65.0)
                elif spot_ltp > 0 and u_open > 0:
                    if spot_ltp >= u_open and spot_ltp >= (u_vwap * 0.999):
                        effective_upstream["consensus_direction"] = "BULLISH"
                        effective_upstream.setdefault("consensus_confidence", 65.0)
                    elif spot_ltp <= u_open and spot_ltp <= (u_vwap * 1.001):
                        effective_upstream["consensus_direction"] = "BEARISH"
                        effective_upstream.setdefault("consensus_confidence", 65.0)
                    elif syn_basis >= 1.0:
                        effective_upstream["consensus_direction"] = "BULLISH"
                        effective_upstream.setdefault("consensus_confidence", 60.0)
                    elif syn_basis <= -1.0:
                        effective_upstream["consensus_direction"] = "BEARISH"
                        effective_upstream.setdefault("consensus_confidence", 60.0)

        # 5. Extract 28 Point-in-Time Features & Run Option Alpha Model
        features = OptionFeatureExtractor.extract_features(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=underlying_df,
            macro_context=macro_context,
            upstream_signals=effective_upstream,
            as_of_time=now_dt,
        )
        pred = self.model.predict_contract(features)
        cal_prob = float(pred.get("calibrated_prob") or 0.50)
        exp_net_ret = float(pred.get("expected_net_return_pct") or 0.0)

        # Structural OI Wall Shield Bonus for Short Book (selling at or beyond institutional OI Wall)
        if is_short_regime:
            if (opt_type == "CE" and call_wall_strike > 0 and strike >= call_wall_strike) or (
                opt_type == "PE" and put_wall_strike > 0 and strike <= put_wall_strike
            ):
                cal_prob = min(0.95, round(cal_prob + 0.02, 4))

        rejection_reasons: List[str] = []

        # 6. Microstructure & Warm-up Hard Hurdles
        if enforce_cutoff and (
            not is_option_trading_window(now_dt)
            or not is_advance_entry_window(now_dt, allow_super_trend_afternoon=is_super_trend_afternoon_breakout)
        ):
            rejection_reasons.append("Outside intraday option entry window (09:30-13:15 IST)")
        if now_dt.hour == 9 and now_dt.minute < 43:
            rejection_reasons.append(f"Causal 28-bar Wilder ADX(14) warm-up incomplete before 09:43 IST ({completed_bars}/28 bars)")
        if age_ms > MAX_QUOTE_AGE_MS:
            rejection_reasons.append(f"Stale option quote ({age_ms}ms > {MAX_QUOTE_AGE_MS}ms)")
        if bid < MIN_BID_PRICE or ask < bid or spread_pct > MAX_SPREAD_PCT:
            rejection_reasons.append(f"Spread/quote quality fail (Bid=₹{bid:.2f}, Spread={spread_pct:.2f}%)")
        if oi < MIN_OI or vol < MIN_VOLUME:
            rejection_reasons.append(f"Insufficient liquidity (OI={int(oi)}, Vol={int(vol)})")

        if is_short_regime:
            book_type = BOOK_SHORT_SELL
            direction = DIRECTION_SELL_CE if opt_type == "CE" else DIRECTION_SELL_PE
            if is_wave2_exhaustion_short:
                strategy_mode = STRATEGY_WAVE2_EXHAUSTION_SELLER
            elif is_diagonal_otm_short or is_diagonal_type_mismatch:
                strategy_mode = STRATEGY_DIAGONAL_OTM_WING_SELLER
            else:
                strategy_mode = STRATEGY_CONTRACTION_THETA_SELLER

            if is_short_open:
                rejection_reasons.append(f"Short Book Anti-Clustering: Active 1-Lot Short {u_sym} position already open")
            if is_diagonal_type_mismatch:
                rejection_reasons.append(
                    f"Diagonal Spread Type Mismatch: Active {u_sym} parent Long is {parent_long_opt_type}, cannot pair with opposite {opt_type} short wing"
                )
            if strategy_mode == STRATEGY_DIAGONAL_OTM_WING_SELLER and parent_long_entry_time:
                if AdvanceDB.has_diagonal_wing_for_active_long(u_sym, parent_long_entry_time):
                    rejection_reasons.append(
                        f"Diagonal Wing Anti-Churn: 1 Diagonal OTM Wing already executed for active {u_sym} parent Long wave"
                    )
            if strategy_mode == STRATEGY_WAVE2_EXHAUSTION_SELLER:
                if active_wave2_harvest and active_wave2_harvest.get("exit_time"):
                    if AdvanceDB.has_exhaustion_short_for_harvested_wave(
                        u_sym, opt_type, str(active_wave2_harvest.get("exit_time"))
                    ):
                        rejection_reasons.append(
                            f"Wave-2 Exhaustion Anti-Churn: 1 Exhaustion Short already executed for harvested {u_sym} {opt_type} wave"
                        )
                # Wave-2 Trend Continuation & Breakout Guard (prevents shorting Calls into a bull trend like Nov 6 Trade #41)
                if is_post_harvest_after_close and not is_trend_aligned_wave2_short:
                    if kaufman_er > POST_HARVEST_EXHAUSTION_ER_MAX:
                        if opt_type == "CE" and (
                            is_day_high_breakout or ((plus_di - minus_di) > 10.0 and spot_ltp > session_tpb)
                        ):
                            rejection_reasons.append(
                                f"Wave-2 Trend Continuation Guard: Counter-trend SELL_CE blocked while bull trend remains active (+DI={plus_di:.1f} > -DI={minus_di:.1f}, ER={kaufman_er:.2f} > {POST_HARVEST_EXHAUSTION_ER_MAX})"
                            )
                        elif opt_type == "PE" and (
                            is_day_low_breakdown or ((minus_di - plus_di) > 10.0 and spot_ltp < session_tpb)
                        ):
                            rejection_reasons.append(
                                f"Wave-2 Trend Continuation Guard: Counter-trend SELL_PE blocked while bear trend remains active (-DI={minus_di:.1f} > +DI={plus_di:.1f}, ER={kaufman_er:.2f} > {POST_HARVEST_EXHAUSTION_ER_MAX})"
                            )
            if strategy_mode == STRATEGY_CONTRACTION_THETA_SELLER:
                if otm_strikes < 0.20:
                    rejection_reasons.append(
                        f"Contraction Short Strike Discipline: Strike {strike:.0f} is too close/ITM ({otm_strikes:+.2f} strikes OTM < +0.20 min)"
                    )
                if vrp < 0.0:
                    rejection_reasons.append(
                        f"Contraction Short VRP Guard: VRP ({vrp:+.2f}% < 0.0%) indicates underpriced IV relative to 15m RV ({rv_15m_ann:.1f}%)"
                    )
                if opt_type == "CE":
                    if has_orb and spot_ltp > orb_high:
                        rejection_reasons.append(
                            f"Contraction Short ORB Breakout Guard: Spot ({spot_ltp:.1f}) > ORB High ({orb_high:.1f}); naked SELL_CE blocked"
                        )
                    elif (plus_di - minus_di) > 4.0 or kaufman_er > 0.20:
                        rejection_reasons.append(
                            f"Contraction Short Trend Conflict: +DI ({plus_di:.1f}) vs -DI ({minus_di:.1f}) with ER {kaufman_er:.2f}; naked SELL_CE blocked"
                        )
                elif opt_type == "PE":
                    if has_orb and spot_ltp < orb_low:
                        rejection_reasons.append(
                            f"Contraction Short ORB Breakdown Guard: Spot ({spot_ltp:.1f}) < ORB Low ({orb_low:.1f}); naked SELL_PE blocked"
                        )
                    elif (minus_di - plus_di) > 4.0 or kaufman_er > 0.20:
                        rejection_reasons.append(
                            f"Contraction Short Trend Conflict: -DI ({minus_di:.1f}) vs +DI ({plus_di:.1f}) with ER {kaufman_er:.2f}; naked SELL_PE blocked"
                        )
            recent_short_loss = AdvanceDB.get_recent_book_loss_cooldown(
                book_type=BOOK_SHORT_SELL,
                underlying=u_sym,
                option_type=opt_type,
                as_of_time=now_dt,
            )
            if recent_short_loss:
                rejection_reasons.append(
                    f"Short Book Post-Loss Cooldown: Recent {u_sym} {opt_type} {recent_short_loss['exit_reason']} "
                    f"({recent_short_loss['remaining_seconds']}s cooldown remaining)"
                )
            if cal_prob < 0.44:
                rejection_reasons.append(f"Activity conviction below hurdle ({cal_prob:.2f} < 0.44)")
        else:
            book_type = BOOK_LONG_BUY
            direction = DIRECTION_BUY_CE if opt_type == "CE" else DIRECTION_BUY_PE
            strategy_mode = STRATEGY_WAVE1_GAMMA_SNIPER

            if is_long_open:
                rejection_reasons.append(f"Long Book Anti-Clustering: Active 1-Lot Long {u_sym} position already open")
            raw_delta_abs = abs(float(quote.get("delta") or 0.50))
            if 0.0 < ask < 35.0:
                rejection_reasons.append(
                    f"Minimum Premium Discipline: Option ask ₹{ask:.2f} < ₹35.00 institutional floor"
                )
            if 0.0 < raw_delta_abs < 0.35 or raw_delta_abs > 0.72:
                rejection_reasons.append(
                    f"Minimum Delta Discipline (Institutional Delta Discipline): |Delta| {raw_delta_abs:.2f} outside [0.35, 0.72] convexity band"
                )
            if abs(otm_strikes) > 2.5:
                rejection_reasons.append(
                    f"Far-OTM Strike Discipline (ATM Proximity Discipline): Strike {strike:.0f} is {otm_strikes:+.2f} strikes from spot (exceeds ±2.50 max)"
                )
            if opt_type == "CE" and not is_orb_broken_ce:
                rejection_reasons.append(
                    f"Inside Opening Range Box: Spot ({spot_ltp:.1f}) < ORB High ({orb_high:.1f}) without chronological V-bottom; Long Call blocked"
                )
            elif opt_type == "PE" and not is_orb_broken_pe:
                rejection_reasons.append(
                    f"Inside Opening Range Box: Spot ({spot_ltp:.1f}) > ORB Low ({orb_low:.1f}) without chronological V-top; Long Put blocked"
                )
            # Rolling Session High/Low Extrema Pullback Guard (prevents mid-air buying far below Session High)
            max_pullback_pts = (
                LONG_MAX_PULLBACK_FROM_HIGH_PTS_BANKNIFTY
                if "BANK" in u_sym
                else LONG_MAX_PULLBACK_FROM_HIGH_PTS_NIFTY
            )
            if opt_type == "CE" and day_high > 0 and (day_high - spot_ltp) > max_pullback_pts and not is_v_bottom_recovery:
                rejection_reasons.append(
                    f"Session Extrema Pullback Guard: Spot ({spot_ltp:.1f}) is {day_high - spot_ltp:.1f} pts below Session High ({day_high:.1f} > {max_pullback_pts:.1f} pts max pullback)"
                )
            elif opt_type == "PE" and day_low > 0 and (spot_ltp - day_low) > max_pullback_pts and not is_v_top_rejection:
                rejection_reasons.append(
                    f"Session Extrema Pullback Guard: Spot ({spot_ltp:.1f}) is {spot_ltp - day_low:.1f} pts above Session Low ({day_low:.1f} > {max_pullback_pts:.1f} pts max bounce)"
                )
            # Institutional OI Wall Guard for In-Range Long Entries
            wall_prox_pts = 35.0 if "BANK" in u_sym else 15.0
            if (
                opt_type == "CE"
                and call_wall_strike > 0
                and not is_day_high_breakout
                and 0.0 < (call_wall_strike - spot_ltp) <= wall_prox_pts
                and kaufman_er < 0.38
            ):
                rejection_reasons.append(
                    f"Institutional Call OI Wall Resistance: Spot ({spot_ltp:.1f}) is within {call_wall_strike - spot_ltp:.1f} pts below Call Wall ({call_wall_strike:.0f}) without breakout velocity"
                )
            elif (
                opt_type == "PE"
                and put_wall_strike > 0
                and not is_day_low_breakdown
                and 0.0 < (spot_ltp - put_wall_strike) <= wall_prox_pts
                and kaufman_er < 0.38
            ):
                rejection_reasons.append(
                    f"Institutional Put OI Wall Support: Spot ({spot_ltp:.1f}) is within {spot_ltp - put_wall_strike:.1f} pts above Put Wall ({put_wall_strike:.0f}) without breakdown velocity"
                )
            if completed_bars >= 10 and 0.0 < range_10m_pct < 0.08:
                rejection_reasons.append(
                    f"10m Compression Shield: Underlying 10m range {range_10m_pts:.1f} pts ({range_10m_pct:.3f}% < 0.080%) indicates dead chop"
                )
            if is_post_harvest_after_close:
                rem_m = max(1, int(recent_harvest.get("remaining_seconds", 1800)) // 60)
                rejection_reasons.append(
                    f"Wave-1 Exhaustion Guard: Recent {u_sym} {opt_type} winner harvested +{recent_harvest.get('mfe_pct', 15.0):.1f}% MFE ({rem_m}m cooldown remaining)"
                )
            recent_long_loss = AdvanceDB.get_recent_book_loss_cooldown(
                book_type=BOOK_LONG_BUY,
                underlying=u_sym,
                option_type=opt_type,
                as_of_time=now_dt,
            )
            if recent_long_loss:
                rejection_reasons.append(
                    f"Long Book Post-Loss Cooldown: Recent {u_sym} {opt_type} {recent_long_loss['exit_reason']} "
                    f"({recent_long_loss['remaining_seconds']}s cooldown remaining)"
                )
            opp_opt_type = "PE" if opt_type == "CE" else "CE"
            recent_opp_loss = AdvanceDB.get_recent_book_loss_cooldown(
                book_type=BOOK_LONG_BUY,
                underlying=u_sym,
                option_type=opp_opt_type,
                as_of_time=now_dt,
                sl_cooldown_seconds=1800,
                stagnation_cooldown_seconds=1800,
            )
            is_extrema_break = is_day_high_breakout if opt_type == "CE" else is_day_low_breakdown
            if recent_opp_loss and not (is_extrema_break and kaufman_er >= 0.38):
                rem_flip_m = max(1, int(recent_opp_loss["remaining_seconds"]) // 60)
                rejection_reasons.append(
                    f"Anti-Ping-Pong Lockout: Active {rem_flip_m}m flip cooldown after recent {u_sym} {opp_opt_type} loss"
                )
            if plus_di > 0 or minus_di > 0:
                if opt_type == "CE" and minus_di > plus_di:
                    rejection_reasons.append(
                        f"Counter-trend Long Call blocked: -DI ({minus_di:.1f}) > +DI ({plus_di:.1f})"
                    )
                elif opt_type == "PE" and plus_di > minus_di:
                    rejection_reasons.append(
                        f"Counter-trend Long Put blocked: +DI ({plus_di:.1f}) > -DI ({minus_di:.1f})"
                    )
                elif abs(plus_di - minus_di) < 5.0:
                    rejection_reasons.append(
                        f"Directional Equilibrium Chop: |+DI ({plus_di:.1f}) - -DI ({minus_di:.1f})| < 5.0 minimum spread"
                    )
            if rv_15m_ann < 8.5 and vrp > 6.0 and not is_extrema_break:
                rejection_reasons.append(
                    f"Low-RV Gamma Pinning Trap blocked for Long Buying (RV_15m={rv_15m_ann:.1f}% < 8.5%, VRP=+{vrp:.1f}% > +6.0%)"
                )
            if cal_prob < 0.48 or exp_net_ret < 1.5:
                rejection_reasons.append(f"Model conviction below Long Sniper hurdle (P={cal_prob:.2f}, E[R]={exp_net_ret:.1f}%)")
            if kaufman_er < 0.28 or adx_14 < 18.0:
                rejection_reasons.append(
                    f"Trend expansion insufficient for Long Buying (requires ADX >= 18.0 & ER >= 0.28; got ADX={adx_14:.1f}, ER={kaufman_er:.2f})"
                )

        # 6. Circuit Breaker & Cooldown Check
        cb = AdvanceDB.get_circuit_breaker_telemetry(as_of_time=now_dt)
        if cb.get("is_session_halted") or cb.get("is_pause_active"):
            rejection_reasons.append(f"Sandbox Circuit Breaker Active: {cb.get('halt_reason')}")

        u_lock_key = f"{book_type}_{u_sym}"
        last_u_lock = self._ACTIVE_UNDERLYING_LOCKS.get(u_lock_key, 0.0)
        if 0 <= (now_epoch - last_u_lock) < 15.0:
            rejection_reasons.append(f"Same-Sweep Anti-Clustering Lock: {u_sym} {book_type} already triggered in current sweep")

        cd_key = f"{book_type}_{ikey}"
        last_cd = self._COOLDOWNS.get(cd_key, 0.0)
        if 0 <= (now_epoch - last_cd) < 300.0:
            rejection_reasons.append("Per-contract 5m signal cooldown active")

        is_active = len(rejection_reasons) == 0
        status = STATE_ACTIVE if is_active else (STATE_QUALIFIED if cal_prob >= 0.46 else STATE_WATCH)

        sig_payload = {
            "signal_id": f"adv_sig_{uuid.uuid4().hex[:10]}",
            "created_at": now_dt.isoformat(),
            "book_type": book_type,
            "strategy_mode": strategy_mode,
            "direction": direction,
            "status": status,
            "instrument_key": ikey,
            "trading_symbol": tsym,
            "underlying": u_sym,
            "strike": strike,
            "option_type": opt_type,
            "expiry": expiry,
            "lot_size": lot_size,
            "spot_price": spot_ltp,
            "option_bid": bid,
            "option_ask": ask,
            "spread_pct": spread_pct,
            "delta": float(quote.get("delta") or 0.0),
            "gamma": float(quote.get("gamma") or 0.0),
            "theta": float(quote.get("theta") or 0.0),
            "vega": float(quote.get("vega") or 0.0),
            "iv": float(quote.get("iv") or 0.0),
            "oi": oi,
            "volume": vol,
            "otm_strikes": otm_strikes,
            "adx_14": adx_14,
            "kaufman_er": kaufman_er,
            "atr_ratio": atr_ratio,
            "net_gex": net_gex,
            "vrp": vrp,
            "synthetic_basis_bps": syn_basis,
            "pcr_oi": pcr_oi,
            "calibrated_prob": round(cal_prob, 4),
            "expected_net_return": round(exp_net_ret, 2),
            "hurdles_passed": is_active,
            "rejection_reasons": rejection_reasons,
            "regime_telemetry": chain_intel,
            "is_super_trend_afternoon_breakout": is_super_trend_afternoon_breakout,
        }

        if status in (STATE_ACTIVE, STATE_QUALIFIED):
            AdvanceDB.save_signal(sig_payload)

        if is_active:
            self._COOLDOWNS[cd_key] = now_epoch
            self._ACTIVE_UNDERLYING_LOCKS[u_lock_key] = now_epoch
            trade_id = AdvancePaperExecution.open_trade(
                sig_payload, as_of_time=now_dt, enforce_cutoff=enforce_cutoff
            )
            sig_payload["opened_trade_id"] = trade_id

        return sig_payload

