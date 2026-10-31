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
    MIN_CAUSAL_WARMUP_BARS,
    POST_HARVEST_COOLDOWN_SECONDS,
    POST_HARVEST_EXHAUSTION_ATR_RATIO_MAX,
    POST_HARVEST_EXHAUSTION_ER_MAX,
    SHORT_MAX_ITM_STRIKES,
    SHORT_MIN_ENTRY_BID_PRICE,
    STRATEGY_CONTRACTION_THETA_SELLER,
    STRATEGY_DIAGONAL_OTM_WING_SELLER,
    STRATEGY_WAVE1_GAMMA_SNIPER,
    STRATEGY_WAVE2_EXHAUSTION_SELLER,
)
from app.analytics.option_sniffer_advance.advance_db import AdvanceDB
from app.analytics.option_sniffer_advance.advance_paper_execution import AdvancePaperExecution

logger = logging.getLogger(__name__)


class AdvanceSignalEngine:
    """Institutional Dual-Book Regime Router & Signal Evaluator."""

    _COOLDOWNS: Dict[str, float] = {}

    def __init__(self, model: Optional[OptionAlphaModel] = None):
        self.model = model or OptionAlphaModel()

    @classmethod
    def reset_state(cls) -> None:
        cls._COOLDOWNS.clear()

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
        kaufman_er = float(chain_intel.get("kaufman_er") or 0.0)
        atr_ratio = float(chain_intel.get("atr_ratio") or 1.0)
        net_gex = float(chain_intel.get("net_gex") or 0.0)
        vrp = float(chain_intel.get("vrp") or 0.0)
        syn_basis = float(chain_intel.get("synthetic_basis_bps") or 0.0)
        pcr_oi = float(chain_intel.get("pcr_oi") or 1.0)
        completed_bars = int(chain_intel.get("completed_bars_count") or 0)

        strike_int = float(STRIKE_INTERVALS.get(u_sym, 50.0))
        if spot_ltp > 0 and strike > 0:
            otm_strikes = round(
                ((strike - spot_ltp) / strike_int) if opt_type == "CE" else ((spot_ltp - strike) / strike_int),
                2,
            )
        else:
            otm_strikes = 0.0

        # 2. Extract 28 Point-in-Time Features & Run Option Alpha Model
        features = OptionFeatureExtractor.extract_features(
            contract=contract,
            quote=quote,
            underlying_quote=underlying_quote,
            underlying_df=underlying_df,
            macro_context=macro_context,
            upstream_signals=upstream_signals,
            as_of_time=now_dt,
        )
        pred = self.model.predict_contract(features, option_type=opt_type)
        cal_prob = float(pred.get("calibrated_prob") or 0.50)
        exp_net_ret = float(pred.get("expected_net_return_pct") or 0.0)

        rejection_reasons: List[str] = []

        # 3. Microstructure & Warm-up Hard Hurdles
        if enforce_cutoff and (not is_option_trading_window(now_dt) or not is_option_entry_window(now_dt)):
            rejection_reasons.append("Outside intraday option entry window (09:30-13:15 IST)")
        if now_dt.hour == 9 and now_dt.minute < 43:
            rejection_reasons.append(f"Causal 28-bar Wilder ADX(14) warm-up incomplete before 09:43 IST ({completed_bars}/28 bars)")
        if age_ms > MAX_QUOTE_AGE_MS:
            rejection_reasons.append(f"Stale option quote ({age_ms}ms > {MAX_QUOTE_AGE_MS}ms)")
        if bid < MIN_BID_PRICE or ask < bid or spread_pct > MAX_SPREAD_PCT:
            rejection_reasons.append(f"Spread/quote quality fail (Bid=₹{bid:.2f}, Spread={spread_pct:.2f}%)")
        if oi < MIN_OI or vol < MIN_VOLUME:
            rejection_reasons.append(f"Insufficient liquidity (OI={int(oi)}, Vol={int(vol)})")

        # 4. Check Active Dual-Book Positions & Post-Harvest Cooldown State
        open_trades = AdvanceDB.get_open_trades()
        u_open_long = [
            t for t in open_trades if t.get("book_type") == BOOK_LONG_BUY and str(t.get("underlying") or "").upper() == u_sym
        ]
        u_open_short = [
            t for t in open_trades if t.get("book_type") == BOOK_SHORT_SELL and str(t.get("underlying") or "").upper() == u_sym
        ]
        is_long_open = len(u_open_long) > 0
        is_short_open = len(u_open_short) > 0

        long_open_elapsed_sec = 99999.0
        if is_long_open:
            try:
                l_et = datetime.fromisoformat(str(u_open_long[0]["entry_time"]).replace("T", " ")[:19])
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
        is_post_harvest_after_close = recent_harvest is not None

        # 5. Institutional Dual-Book Regime Classification
        can_short_extrinsic = (
            bid >= SHORT_MIN_ENTRY_BID_PRICE
            and ((otm_strikes >= -SHORT_MAX_ITM_STRIKES) or (kaufman_er <= 0.15))
        )

        is_contraction_short = (not is_long_open) and (atr_ratio <= CONTRACTION_ATR_RATIO_MAX) and (kaufman_er < CONTRACTION_ER_MAX)
        is_wave2_exhaustion_short = is_post_harvest_after_close and (
            atr_ratio <= POST_HARVEST_EXHAUSTION_ATR_RATIO_MAX or kaufman_er <= POST_HARVEST_EXHAUSTION_ER_MAX
        )
        is_diagonal_otm_short = (
            is_long_open
            and (long_open_elapsed_sec >= DIAGONAL_MIN_LONG_ELAPSED_SECONDS)
            and (otm_strikes > DIAGONAL_MIN_OTM_STRIKES)
        )

        is_short_regime = can_short_extrinsic and (
            is_contraction_short or is_wave2_exhaustion_short or is_diagonal_otm_short
        )

        if is_short_regime:
            book_type = BOOK_SHORT_SELL
            direction = DIRECTION_SELL_CE if opt_type == "CE" else DIRECTION_SELL_PE
            if is_wave2_exhaustion_short:
                strategy_mode = STRATEGY_WAVE2_EXHAUSTION_SELLER
            elif is_diagonal_otm_short:
                strategy_mode = STRATEGY_DIAGONAL_OTM_WING_SELLER
            else:
                strategy_mode = STRATEGY_CONTRACTION_THETA_SELLER

            if is_short_open:
                rejection_reasons.append(f"Short Book Anti-Clustering: Active 1-Lot Short {u_sym} position already open")
            if cal_prob < 0.44:
                rejection_reasons.append(f"Activity conviction below hurdle ({cal_prob:.2f} < 0.44)")
        else:
            book_type = BOOK_LONG_BUY
            direction = DIRECTION_BUY_CE if opt_type == "CE" else DIRECTION_BUY_PE
            strategy_mode = STRATEGY_WAVE1_GAMMA_SNIPER

            if is_long_open:
                rejection_reasons.append(f"Long Book Anti-Clustering: Active 1-Lot Long {u_sym} position already open")
            if is_post_harvest_after_close:
                rem_m = max(1, int(recent_harvest.get("remaining_seconds", 1800)) // 60)
                rejection_reasons.append(
                    f"Wave-1 Exhaustion Guard: Recent {u_sym} {opt_type} winner harvested +{recent_harvest.get('mfe_pct', 15.0):.1f}% MFE ({rem_m}m cooldown remaining)"
                )
            if cal_prob < 0.48 or exp_net_ret < 1.5:
                rejection_reasons.append(f"Model conviction below Long Sniper hurdle (P={cal_prob:.2f}, E[R]={exp_net_ret:.1f}%)")
            if kaufman_er < 0.25 and adx_14 < 18.0:
                rejection_reasons.append(f"Trend expansion insufficient for Long Buying (ADX={adx_14:.1f}, ER={kaufman_er:.2f})")

        # 6. Circuit Breaker & Cooldown Check
        cb = AdvanceDB.get_circuit_breaker_telemetry(as_of_time=now_dt)
        if cb.get("is_session_halted") or cb.get("is_pause_active"):
            rejection_reasons.append(f"Sandbox Circuit Breaker Active: {cb.get('halt_reason')}")

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
        }

        if status in (STATE_ACTIVE, STATE_QUALIFIED):
            AdvanceDB.save_signal(sig_payload)

        if is_active:
            self._COOLDOWNS[cd_key] = now_epoch
            trade_id = AdvancePaperExecution.open_trade(
                sig_payload, as_of_time=now_dt, enforce_cutoff=enforce_cutoff
            )
            sig_payload["opened_trade_id"] = trade_id

        return sig_payload

