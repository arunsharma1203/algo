"""
Canonical Stateless NSE Options Accounting & Structural Risk Engine.

Pure Python standard-library implementation (zero FastAPI, SQLite, Pandas, or Upstox SDK
imports at module load time) so BOTH production engines and standalone `/private/tmp`
research scripts can import the exact same verified accounting functions safely.

Conventions:
  - Signed Quantity (`qty`):
      * `qty > 0`: Long / bought option leg
      * `qty < 0`: Short / written option leg
  - Cashflow Identity:
      * `entry_cashflow = -qty * entry_price`  (< 0 debit paid, > 0 credit received)
      * `exit_cashflow  = +qty * exit_price`   (> 0 sale/settlement receipt, < 0 buyback cost)
      * `gross_pnl      = entry_cashflow + exit_cashflow`
  - NSE Option Tick Size:
      * ₹0.05 per unit. Market orders cannot execute below ₹0.05; cash-settled worthless
        expiry settles at exact ₹0.00 with 0 closing market orders.
  - NSE Index Expiry Settlement (NIFTY / BANKNIFTY):
      * European-style, cash-settled (zero early assignment or stock pin-assignment).
      * Un-squared-off ITM LONG (`qty > 0`) options incur statutory exercise STT on
        intrinsic settlement value (`0.125%` < 2026-04-01; `0.15%` >= 2026-04-01 per
        Finance Act 2026). Short ITM option writers (`qty < 0`) do NOT pay exercise STT.
  - Margin Honesty:
      * Any local margin number is strictly tagged `UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN`
        and `can_certify_live_margin = False`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN
import math
from typing import Any, Dict, List, Optional, Sequence

NSE_OPTION_TICK = Decimal("0.05")
_PAISA = Decimal("0.01")
_ZERO = Decimal("0.00")
_ONE = Decimal("1")
_BPS_DENOM = Decimal("10000")


def _to_finite_decimal(val: Any, field_name: str) -> Decimal:
    if val is None or isinstance(val, bool):
        raise ValueError(f"{field_name} must be a finite number, got {val!r}")
    if isinstance(val, Decimal):
        if not val.is_finite():
            raise ValueError(f"{field_name} must be finite, got {val!r}")
        return val
    if isinstance(val, int):
        return Decimal(val)
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"{field_name} must be finite, got {val!r}")
        return Decimal(str(val))
    try:
        dec = Decimal(str(val))
    except Exception as exc:
        raise ValueError(f"{field_name} could not be parsed as Decimal: {val!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"{field_name} must be finite, got {val!r}")
    return dec


def _q2(val: Decimal) -> Decimal:
    return val.quantize(_PAISA, rounding=ROUND_HALF_EVEN)


def round_to_nse_tick(
    price: float,
    mode: str = "nearest",
    allow_zero_settlement: bool = False,
) -> float:
    """
    Rounds an option price to the NSE ₹0.05 tick grid.

    Args:
        price: Raw option price in INR.
        mode:
            - `"nearest"`: nearest ₹0.05 (half-even on exact half-ticks).
            - `"adverse_buy"`: ceiling to ₹0.05 (buying to open long or cover short).
            - `"adverse_sell"`: floor to ₹0.05 (selling to open short or exit long).
        allow_zero_settlement:
            If True, exact 0.0 is permitted (for worthless expiry settlement).
            Otherwise market order prices must be > 0 and clamp to minimum ₹0.05.
    """
    dec = _to_finite_decimal(price, "price")
    if allow_zero_settlement and dec == _ZERO:
        return 0.0
    if dec <= _ZERO:
        raise ValueError(f"Tradable option price must be > 0 (got {price!r})")

    ticks = dec / NSE_OPTION_TICK
    if mode == "nearest":
        q_ticks = ticks.quantize(_ONE, rounding=ROUND_HALF_EVEN)
    elif mode == "adverse_buy":
        q_ticks = ticks.quantize(_ONE, rounding=ROUND_CEILING)
    elif mode == "adverse_sell":
        q_ticks = ticks.quantize(_ONE, rounding=ROUND_FLOOR)
    else:
        raise ValueError(f"Unsupported tick rounding mode: {mode!r}")

    rounded = q_ticks * NSE_OPTION_TICK
    if rounded < NSE_OPTION_TICK:
        rounded = NSE_OPTION_TICK
    return float(_q2(rounded))


def apply_adverse_slippage_and_tick(
    raw_price: float,
    side: str,
    impact_bps: float = 0.0,
    half_spread_pts: float = 0.0,
) -> float:
    """
    Applies half-spread + adverse impact slippage (in bps) and rounds adversely to ₹0.05.
    """
    px = _to_finite_decimal(raw_price, "raw_price")
    if px <= _ZERO:
        raise ValueError(f"raw_price must be > 0, got {raw_price!r}")
    bps = _to_finite_decimal(impact_bps, "impact_bps")
    hsp = _to_finite_decimal(half_spread_pts, "half_spread_pts")
    if bps < _ZERO or hsp < _ZERO:
        raise ValueError("impact_bps and half_spread_pts must be >= 0")

    side_u = str(side).upper().strip()
    if side_u == "BUY":
        slipped = (px + hsp) * (_ONE + (bps / _BPS_DENOM))
        return round_to_nse_tick(float(slipped), mode="adverse_buy", allow_zero_settlement=False)
    if side_u == "SELL":
        base = px - hsp
        if base <= NSE_OPTION_TICK:
            return float(NSE_OPTION_TICK)
        slipped = base * max(_ZERO, _ONE - (bps / _BPS_DENOM))
        if slipped <= NSE_OPTION_TICK:
            return float(NSE_OPTION_TICK)
        return round_to_nse_tick(float(slipped), mode="adverse_sell", allow_zero_settlement=False)
    raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")


def compute_intrinsic_value(option_type: str, strike: float, settlement_spot: float) -> float:
    """Computes European option intrinsic value at expiry settlement."""
    ot = str(option_type).upper().strip()
    if ot not in ("CE", "PE"):
        raise ValueError(f"option_type must be 'CE' or 'PE', got {option_type!r}")
    k = _to_finite_decimal(strike, "strike")
    s = _to_finite_decimal(settlement_spot, "settlement_spot")
    if k <= _ZERO or s <= _ZERO:
        raise ValueError(f"strike and settlement_spot must be > 0 (got strike={strike!r}, spot={settlement_spot!r})")
    intr = max(_ZERO, s - k) if ot == "CE" else max(_ZERO, k - s)
    return float(_q2(intr))


def get_upstox_option_tariff(trade_date: Optional[Any] = None) -> Dict[str, Any]:
    """
    Returns effective-dated statutory & Upstox tariff rates for NSE index/equity options.
    Matches `option_constants.get_option_tariff_schedule` and adds statutory ITM option
    exercise STT (`0.125%` < 2026-04-01; `0.15%` >= 2026-04-01 per Finance Act 2026).
    """
    if trade_date == "LEGACY":
        return {
            "schedule_name": "UPSTOX_LEGACY_DEFAULT_TARIFF",
            "effective_date": "DEFAULT",
            "stt_sell_rate": Decimal("0.00125"),
            "stt_exercise_intrinsic_rate": Decimal("0.00125"),
            "exchange_turnover_rate": Decimal("0.000535"),
            "exchange_core_rate": Decimal("0.000535"),
            "ipft_rate": Decimal("0.0"),
            "sebi_turnover_rate": Decimal("0.000001"),
            "stamp_duty_buy_rate": Decimal("0.00003"),
            "brokerage_per_order": Decimal("20.00"),
            "gst_rate": Decimal("0.18"),
            "gst_includes_sebi": True,
            "regulatory_ref": "LEGACY_DEFAULT",
        }

    if trade_date is None:
        target_date_str = datetime.now().strftime("%Y-%m-%d")
    elif isinstance(trade_date, str):
        target_date_str = trade_date[:10]
    elif isinstance(trade_date, (datetime, date)):
        target_date_str = trade_date.strftime("%Y-%m-%d")
    else:
        target_date_str = str(trade_date)[:10]

    if len(target_date_str) != 10 or target_date_str[4] != "-" or target_date_str[7] != "-":
        raise ValueError(f"Invalid trade_date format (expected YYYY-MM-DD): {trade_date!r}")

    if target_date_str >= "2026-04-01":
        stt_sell = Decimal("0.0015")
        stt_ex = Decimal("0.0015")
        stt_lbl = "STT_APR_2026_0.15PCT"
    elif target_date_str >= "2024-10-01":
        stt_sell = Decimal("0.0010")
        stt_ex = Decimal("0.00125")
        stt_lbl = "STT_OCT_2024_0.10PCT"
    else:
        stt_sell = Decimal("0.000625")
        stt_ex = Decimal("0.00125")
        stt_lbl = "STT_PRE_OCT_2024_0.0625PCT"

    if target_date_str >= "2026-03-01":
        exch_rate = Decimal("0.0003553")
        exch_core = Decimal("0.000355299")
        ipft_rate = Decimal("0.000000001")
        exch_lbl = "NSE_MAR_2026_0.03553PCT"
        reg_ref = "NSE/FA/73061"
    elif target_date_str >= "2024-10-01":
        exch_rate = Decimal("0.0003553")
        exch_core = Decimal("0.0003503")
        ipft_rate = Decimal("0.0000050")
        exch_lbl = "NSE_OCT_2024_0.03553PCT"
        reg_ref = "NSE/FA/64232_56129"
    else:
        exch_rate = Decimal("0.000535")
        exch_core = Decimal("0.000500")
        ipft_rate = Decimal("0.000035")
        exch_lbl = "NSE_PRE_OCT_2024_0.0535PCT"
        reg_ref = "NSE_LEGACY"

    return {
        "schedule_name": f"{stt_lbl}_{exch_lbl}",
        "effective_date": target_date_str,
        "stt_sell_rate": stt_sell,
        "stt_exercise_intrinsic_rate": stt_ex,
        "exchange_turnover_rate": exch_rate,
        "exchange_core_rate": exch_core,
        "ipft_rate": ipft_rate,
        "sebi_turnover_rate": Decimal("0.000001"),
        "stamp_duty_buy_rate": Decimal("0.00003"),
        "brokerage_per_order": Decimal("20.00"),
        "gst_rate": Decimal("0.18"),
        "gst_includes_sebi": False,
        "regulatory_ref": reg_ref,
    }


@dataclass(frozen=True)
class OptionLeg:
    """
    Canonical signed option leg specification.

    `qty > 0` = Long (buy to open), `qty < 0` = Short (sell to open).
    """

    option_type: str
    strike: float
    qty: int
    entry_price: float
    exit_price: Optional[float] = None
    exit_mode: str = "MARKET_ORDER"  # "MARKET_ORDER" | "CASH_SETTLED_EXPIRY"
    settlement_spot: Optional[float] = None
    lot_size: Optional[int] = None
    charge_settlement_brokerage_on_itm_exercise: bool = False
    label: str = ""

    def validate(self) -> None:
        ot = str(self.option_type).upper().strip()
        if ot not in ("CE", "PE"):
            raise ValueError(f"option_type must be 'CE' or 'PE', got {self.option_type!r}")
        if isinstance(self.qty, bool) or not isinstance(self.qty, int) or self.qty == 0:
            raise ValueError(f"qty must be a non-zero integer, got {self.qty!r}")
        if self.lot_size is not None:
            if isinstance(self.lot_size, bool) or not isinstance(self.lot_size, int) or self.lot_size <= 0:
                raise ValueError(f"lot_size must be a positive integer, got {self.lot_size!r}")
            if abs(self.qty) % self.lot_size != 0:
                raise ValueError(f"abs(qty)={abs(self.qty)} is not a multiple of lot_size={self.lot_size}")

        k = _to_finite_decimal(self.strike, "strike")
        ep = _to_finite_decimal(self.entry_price, "entry_price")
        if k <= _ZERO:
            raise ValueError(f"strike must be > 0, got {self.strike!r}")
        if ep <= _ZERO:
            raise ValueError(f"entry_price must be > 0, got {self.entry_price!r}")

        mode = str(self.exit_mode).upper().strip()
        if mode == "MARKET_ORDER":
            if self.exit_price is None:
                raise ValueError("exit_price is required when exit_mode == 'MARKET_ORDER'")
            xp = _to_finite_decimal(self.exit_price, "exit_price")
            if xp <= _ZERO:
                raise ValueError(f"exit_price must be > 0 for MARKET_ORDER, got {self.exit_price!r}")
        elif mode == "CASH_SETTLED_EXPIRY":
            if self.settlement_spot is None:
                raise ValueError("settlement_spot is required when exit_mode == 'CASH_SETTLED_EXPIRY'")
            sp = _to_finite_decimal(self.settlement_spot, "settlement_spot")
            if sp <= _ZERO:
                raise ValueError(f"settlement_spot must be > 0, got {self.settlement_spot!r}")
        else:
            raise ValueError(f"Unsupported exit_mode: {self.exit_mode!r}")


def compute_leg_accounting(
    leg: OptionLeg,
    trade_date: Optional[Any] = None,
    extra_slippage_rate_per_leg: float = 0.0,
) -> Dict[str, Any]:
    """
    Computes exact signed cashflows, turnovers, Upstox statutory charges, and net P&L
    for a single option leg (`MARKET_ORDER` square-off or `CASH_SETTLED_EXPIRY`).
    """
    leg.validate()
    schedule = get_upstox_option_tariff(trade_date)
    slip_rate = _to_finite_decimal(extra_slippage_rate_per_leg, "extra_slippage_rate_per_leg")
    if slip_rate < _ZERO:
        raise ValueError(f"extra_slippage_rate_per_leg must be >= 0, got {extra_slippage_rate_per_leg!r}")

    qty_signed = Decimal(leg.qty)
    abs_qty = Decimal(abs(leg.qty))
    entry_px = _to_finite_decimal(leg.entry_price, "entry_price")
    mode = str(leg.exit_mode).upper().strip()

    if mode == "MARKET_ORDER":
        exit_px = _to_finite_decimal(leg.exit_price, "exit_price")
        executed_orders = 2
        if leg.qty > 0:
            buy_turnover = entry_px * abs_qty
            sell_turnover = exit_px * abs_qty
        else:
            sell_turnover = entry_px * abs_qty
            buy_turnover = exit_px * abs_qty
        exercise_intrinsic_turnover = _ZERO
        market_turnover = buy_turnover + sell_turnover
    else:
        intr = Decimal(str(compute_intrinsic_value(leg.option_type, leg.strike, float(leg.settlement_spot))))
        exit_px = intr
        is_itm = intr > _ZERO
        executed_orders = 2 if (is_itm and leg.charge_settlement_brokerage_on_itm_exercise) else 1
        if leg.qty > 0:
            buy_turnover = entry_px * abs_qty
            sell_turnover = _ZERO
            exercise_intrinsic_turnover = (intr * abs_qty) if is_itm else _ZERO
        else:
            sell_turnover = entry_px * abs_qty
            buy_turnover = _ZERO
            exercise_intrinsic_turnover = _ZERO
        # Exchange/SEBI market turnover applies strictly to market-traded premium legs
        market_turnover = buy_turnover + sell_turnover

    entry_cashflow = _q2(-qty_signed * entry_px)
    exit_cashflow = _q2(qty_signed * exit_px)
    gross_pnl = _q2(entry_cashflow + exit_cashflow)

    brokerage = _q2(schedule["brokerage_per_order"] * Decimal(executed_orders))
    stt_sell = _q2(sell_turnover * schedule["stt_sell_rate"])
    stt_exercise = _q2(exercise_intrinsic_turnover * schedule["stt_exercise_intrinsic_rate"])
    stt_total = _q2(stt_sell + stt_exercise)
    exchange_fee = _q2(market_turnover * schedule["exchange_turnover_rate"])
    exchange_core_fee = _q2(market_turnover * schedule["exchange_core_rate"])
    ipft_fee = _q2(market_turnover * schedule["ipft_rate"])
    sebi_fee = _q2(market_turnover * schedule["sebi_turnover_rate"])

    gst_base = (
        brokerage + exchange_fee + sebi_fee
        if schedule["gst_includes_sebi"]
        else brokerage + exchange_fee
    )
    gst = _q2(gst_base * schedule["gst_rate"])
    stamp_duty = _q2(buy_turnover * schedule["stamp_duty_buy_rate"])

    statutory_charges = _q2(brokerage + stt_total + exchange_fee + sebi_fee + gst + stamp_duty)
    additional_slippage = _q2(market_turnover * slip_rate)
    total_friction = _q2(statutory_charges + additional_slippage)
    net_pnl = _q2(gross_pnl - total_friction)

    return {
        "label": leg.label or f"{'LONG' if leg.qty > 0 else 'SHORT'} {int(leg.strike)} {leg.option_type.upper()}",
        "option_type": leg.option_type.upper().strip(),
        "strike": float(leg.strike),
        "qty": leg.qty,
        "exit_mode": mode,
        "entry_price": float(entry_px),
        "exit_price_effective": float(exit_px),
        "executed_orders": executed_orders,
        "entry_cashflow": float(entry_cashflow),
        "exit_cashflow": float(exit_cashflow),
        "buy_premium_turnover": float(_q2(buy_turnover)),
        "sell_premium_turnover": float(_q2(sell_turnover)),
        "exercise_intrinsic_turnover": float(_q2(exercise_intrinsic_turnover)),
        "market_turnover": float(_q2(market_turnover)),
        "brokerage": float(brokerage),
        "stt_sell": float(stt_sell),
        "stt_exercise": float(stt_exercise),
        "stt": float(stt_total),
        "exchange_fee": float(exchange_fee),
        "exchange_core_fee": float(exchange_core_fee),
        "ipft_fee": float(ipft_fee),
        "sebi_fee": float(sebi_fee),
        "gst": float(gst),
        "stamp_duty": float(stamp_duty),
        "statutory_charges": float(statutory_charges),
        "additional_slippage": float(additional_slippage),
        "total_friction": float(total_friction),
        "gross_pnl": float(gross_pnl),
        "net_pnl": float(net_pnl),
        "tariff_schedule": schedule["schedule_name"],
    }


def compute_structural_package_risk(
    legs: Sequence[OptionLeg],
    reference_spot: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Computes exact piecewise-linear European expiry payoff bounds AND intermediate
    incomplete-leg execution risk (e.g., bought wing fills, short leg misses).
    """
    if not legs:
        raise ValueError("legs must be non-empty")
    for leg in legs:
        leg.validate()

    # Check asymptotic slope as S -> +infinity: only Calls remain active
    net_call_qty = sum(leg.qty for leg in legs if leg.option_type.upper().strip() == "CE")
    unbounded_upside_risk = net_call_qty < 0

    entry_cashflow = sum(
        (-Decimal(leg.qty) * _to_finite_decimal(leg.entry_price, "entry_price") for leg in legs),
        _ZERO,
    )
    strikes = sorted({_to_finite_decimal(leg.strike, "strike") for leg in legs})
    max_k = max(strikes)
    ref_s = _to_finite_decimal(reference_spot, "reference_spot") if reference_spot is not None else max_k
    if ref_s <= _ZERO:
        raise ValueError(f"reference_spot must be > 0, got {reference_spot!r}")

    knots = [Decimal("0")] + strikes + [max(max_k * Decimal("2"), ref_s * Decimal("2"))]

    def _expiry_pnl_at_spot(spot_val: Decimal) -> Decimal:
        total = entry_cashflow
        for leg in legs:
            ot = leg.option_type.upper().strip()
            k = _to_finite_decimal(leg.strike, "strike")
            intr = max(_ZERO, spot_val - k) if ot == "CE" else max(_ZERO, k - spot_val)
            total += Decimal(leg.qty) * intr
        return total

    payoffs = [_expiry_pnl_at_spot(s_pt) for s_pt in knots]
    min_knot_pnl = min(payoffs)

    if unbounded_upside_risk:
        final_max_loss = math.inf
        is_defined_risk = False
    else:
        final_max_loss = float(_q2(max(_ZERO, -min_knot_pnl)))
        # Uncovered short Put has finite loss down to S=0, but only counts as hedged/defined-spread
        # if net put slope toward S=0 is >= 0 (long put wing >= short put qty).
        net_put_qty = sum(leg.qty for leg in legs if leg.option_type.upper().strip() == "PE")
        is_defined_risk = net_put_qty >= 0

    # Intermediate state risk: buy-first incomplete spread (long legs fill, short legs reject/miss)
    long_legs_outlay = sum(
        (
            Decimal(leg.qty) * _to_finite_decimal(leg.entry_price, "entry_price")
            for leg in legs
            if leg.qty > 0
        ),
        _ZERO,
    )
    has_short_leg = any(leg.qty < 0 for leg in legs)
    has_long_leg = any(leg.qty > 0 for leg in legs)

    # Conservative diagnostic floor ONLY (never certified as broker SPAN+ELM)
    if not has_short_leg:
        heuristic_floor = float(_q2(long_legs_outlay))
    elif is_defined_risk and not math.isinf(final_max_loss):
        heuristic_floor = float(_q2(max(Decimal(str(final_max_loss)), long_legs_outlay)))
    else:
        short_abs_qty = sum(abs(leg.qty) for leg in legs if leg.qty < 0)
        short_credit = sum(
            (
                Decimal(abs(leg.qty)) * _to_finite_decimal(leg.entry_price, "entry_price")
                for leg in legs
                if leg.qty < 0
            ),
            _ZERO,
        )
        # NSE rough conservative diagnostic lower bound (~15% underlying notional + short premium)
        heuristic_floor = float(_q2(Decimal(short_abs_qty) * ref_s * Decimal("0.15") + short_credit))

    return {
        "net_entry_cashflow_inr": float(_q2(entry_cashflow)),
        "is_defined_risk": is_defined_risk,
        "unbounded_upside_risk": unbounded_upside_risk,
        "final_package_max_loss_inr": final_max_loss,
        "buy_first_incomplete_leg_exposure_inr": float(_q2(long_legs_outlay)),
        "sell_first_creates_uncovered_short": has_short_leg,
        "is_multi_leg_package": len(legs) > 1 and has_short_leg and has_long_leg,
        "heuristic_margin_floor_inr": heuristic_floor,
        "margin_status": "UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN",
        "can_certify_live_margin": False,
    }


def compute_package_accounting(
    legs: Sequence[OptionLeg],
    trade_date: Optional[Any] = None,
    extra_slippage_rate_per_leg: float = 0.0,
    reference_spot: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Canonical multi-leg option package accounting + structural risk audit.
    """
    if not legs:
        raise ValueError("legs must contain at least 1 OptionLeg")

    leg_reports = [
        compute_leg_accounting(
            leg=leg,
            trade_date=trade_date,
            extra_slippage_rate_per_leg=extra_slippage_rate_per_leg,
        )
        for leg in legs
    ]
    risk = compute_structural_package_risk(legs=legs, reference_spot=reference_spot)

    def _sum_dec(key: str) -> float:
        return float(_q2(sum((Decimal(str(r[key])) for r in leg_reports), _ZERO)))

    executed_orders = sum(int(r["executed_orders"]) for r in leg_reports)
    gross_pnl = _sum_dec("gross_pnl")
    statutory_charges = _sum_dec("statutory_charges")
    additional_slippage = _sum_dec("additional_slippage")
    total_friction = _sum_dec("total_friction")
    net_pnl = float(_q2(Decimal(str(gross_pnl)) - Decimal(str(total_friction))))

    max_loss_after_entry_exit_fees = (
        math.inf
        if math.isinf(risk["final_package_max_loss_inr"])
        else float(_q2(Decimal(str(risk["final_package_max_loss_inr"])) + Decimal(str(total_friction))))
    )

    return {
        "legs": leg_reports,
        "leg_count": len(leg_reports),
        "executed_orders": executed_orders,
        "entry_cashflow": _sum_dec("entry_cashflow"),
        "exit_cashflow": _sum_dec("exit_cashflow"),
        "buy_premium_turnover": _sum_dec("buy_premium_turnover"),
        "sell_premium_turnover": _sum_dec("sell_premium_turnover"),
        "exercise_intrinsic_turnover": _sum_dec("exercise_intrinsic_turnover"),
        "market_turnover": _sum_dec("market_turnover"),
        "brokerage": _sum_dec("brokerage"),
        "stt_sell": _sum_dec("stt_sell"),
        "stt_exercise": _sum_dec("stt_exercise"),
        "stt": _sum_dec("stt"),
        "exchange_fee": _sum_dec("exchange_fee"),
        "exchange_core_fee": _sum_dec("exchange_core_fee"),
        "ipft_fee": _sum_dec("ipft_fee"),
        "sebi_fee": _sum_dec("sebi_fee"),
        "gst": _sum_dec("gst"),
        "stamp_duty": _sum_dec("stamp_duty"),
        "statutory_charges": statutory_charges,
        "additional_slippage": additional_slippage,
        "total_friction": total_friction,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "tariff_schedule": leg_reports[0]["tariff_schedule"],
        "risk": {
            **risk,
            "final_package_max_loss_after_friction_inr": max_loss_after_entry_exit_fees,
        },
    }


def compute_single_leg_roundtrip(
    option_type: str,
    strike: float,
    signed_qty: int,
    entry_price: float,
    exit_price: float,
    trade_date: Optional[Any] = None,
    apply_slippage: bool = False,
    slippage_rate_per_leg: float = 0.0025,
    reference_spot: Optional[float] = None,
) -> Dict[str, Any]:
    """Convenience single-leg round-trip wrapper (`signed_qty > 0` long, `signed_qty < 0` short)."""
    leg = OptionLeg(
        option_type=option_type,
        strike=strike,
        qty=signed_qty,
        entry_price=entry_price,
        exit_price=exit_price,
        exit_mode="MARKET_ORDER",
    )
    eff_slip = slippage_rate_per_leg if apply_slippage else 0.0
    return compute_package_accounting(
        legs=[leg],
        trade_date=trade_date,
        extra_slippage_rate_per_leg=eff_slip,
        reference_spot=reference_spot,
    )
