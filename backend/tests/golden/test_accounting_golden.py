"""
Golden Verification Suite for Canonical NSE Options Accounting (`options_accounting.py`).

Enforces:
  1. Exact paisa-level agreement across all 5 hand-calculated multi-leg & expiry golden cases.
  2. Adverse ₹0.05 NSE tick rounding + bps slippage invariants.
  3. Unbounded naked-call vs finite naked-put vs defined-spread structural risk detection,
     plus incomplete buy-first spread risk detection and unverified-margin labeling.
  4. Single-leg round-trip parity with `option_constants.calculate_option_trade_costs`.
  5. Fail-closed rejection of NaN, Inf, zero/negative market prices, invalid lot multiples,
     and zero-side-effect stdlib-only import isolation.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.analytics.common.options_accounting import (
    OptionLeg,
    apply_adverse_slippage_and_tick,
    compute_package_accounting,
    compute_single_leg_roundtrip,
    compute_structural_package_risk,
    round_to_nse_tick,
)
from app.analytics.option_sniffer.option_constants import calculate_option_trade_costs
from app.data.database import set_test_db_override

_GOLDEN_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "s2_05_golden_cases.json")


class TestCanonicalOptionsAccountingGolden(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp(prefix="golden_acct_")
        set_test_db_override(os.path.join(self._tmp_dir, "isolated_test.db"))

    def tearDown(self) -> None:
        set_test_db_override(None)
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_all_golden_fixture_cases_exact_paisa(self) -> None:
        with open(_GOLDEN_JSON, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        cases = payload["cases"]
        self.assertGreaterEqual(len(cases), 5)

        for case in cases:
            cid = case["case_id"]
            with self.subTest(case_id=cid):
                if "raw_quotes" in case:
                    bps = float(case["slippage_bps_applied_to_raw_quotes"])
                    for rq in case["raw_quotes"]:
                        ent_side = "BUY" if rq["qty"] > 0 else "SELL"
                        ext_side = "SELL" if rq["qty"] > 0 else "BUY"
                        got_ent = apply_adverse_slippage_and_tick(rq["raw_entry"], side=ent_side, impact_bps=bps)
                        got_ext = apply_adverse_slippage_and_tick(rq["raw_exit"], side=ext_side, impact_bps=bps)
                        self.assertEqual(got_ent, rq["expected_slipped_entry"])
                        self.assertEqual(got_ext, rq["expected_slipped_exit"])

                legs = [OptionLeg(**ld) for ld in case["legs"]]
                res = compute_package_accounting(
                    legs=legs,
                    trade_date=case["trade_date"],
                    reference_spot=case["reference_spot"],
                )
                exp = case["expected"]

                for field in (
                    "executed_orders",
                    "entry_cashflow",
                    "exit_cashflow",
                    "gross_pnl",
                    "brokerage",
                    "stt_sell",
                    "stt_exercise",
                    "stt",
                    "exchange_fee",
                    "sebi_fee",
                    "gst",
                    "stamp_duty",
                    "statutory_charges",
                    "net_pnl",
                ):
                    self.assertEqual(res[field], exp[field], f"{cid} mismatch on {field}")

                # Exact additive fee & cashflow identities
                comp_sum = round(
                    res["brokerage"]
                    + res["stt"]
                    + res["exchange_fee"]
                    + res["sebi_fee"]
                    + res["gst"]
                    + res["stamp_duty"],
                    2,
                )
                self.assertEqual(res["statutory_charges"], comp_sum)
                self.assertEqual(res["gross_pnl"], round(res["entry_cashflow"] + res["exit_cashflow"], 2))
                self.assertEqual(res["net_pnl"], round(res["gross_pnl"] - res["total_friction"], 2))

                risk = res["risk"]
                self.assertEqual(risk["is_defined_risk"], exp["is_defined_risk"])
                self.assertEqual(risk["unbounded_upside_risk"], exp["unbounded_upside_risk"])
                self.assertEqual(risk["final_package_max_loss_inr"], exp["final_package_max_loss_inr"])
                self.assertEqual(
                    risk["buy_first_incomplete_leg_exposure_inr"],
                    exp["buy_first_incomplete_leg_exposure_inr"],
                )
                self.assertEqual(risk["margin_status"], "UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN")
                self.assertFalse(risk["can_certify_live_margin"])

    def test_post_apr2026_itm_exercise_stt_rate_is_015pct(self) -> None:
        leg = OptionLeg(
            option_type="PE",
            strike=25000.0,
            qty=65,
            entry_price=60.0,
            exit_mode="CASH_SETTLED_EXPIRY",
            settlement_spot=24800.0,
            lot_size=65,
        )
        res = compute_package_accounting([leg], trade_date="2026-11-12", reference_spot=24800.0)
        # Intrinsic = 200 * 65 = ₹13,000; Finance Act 2026 >= 2026-04-01 exercise STT = 0.15% = ₹19.50
        self.assertEqual(res["exercise_intrinsic_turnover"], 13000.00)
        self.assertEqual(res["stt_exercise"], 19.50)
        self.assertEqual(res["stt_sell"], 0.00)
        self.assertEqual(res["executed_orders"], 1)

    def test_structural_risk_naked_call_unbounded_and_naked_put_catastrophic(self) -> None:
        naked_call = OptionLeg(option_type="CE", strike=25000.0, qty=-65, entry_price=100.0, exit_price=80.0)
        rc = compute_structural_package_risk([naked_call], reference_spot=25000.0)
        self.assertFalse(rc["is_defined_risk"])
        self.assertTrue(rc["unbounded_upside_risk"])
        self.assertTrue(math.isinf(rc["final_package_max_loss_inr"]))

        naked_put = OptionLeg(option_type="PE", strike=25000.0, qty=-65, entry_price=100.0, exit_price=80.0)
        rp = compute_structural_package_risk([naked_put], reference_spot=25000.0)
        self.assertFalse(rp["is_defined_risk"])
        self.assertFalse(rp["unbounded_upside_risk"])
        # Max downside to S=0 is (25000 - 100) * 65 = ₹16,18,500.00 (far above ₹2L capital)
        self.assertEqual(rp["final_package_max_loss_inr"], 1618500.00)

    def test_single_leg_parity_with_option_constants(self) -> None:
        for dt_str in ("2024-05-15", "2025-06-15", "2026-03-15", "2026-11-10", "LEGACY"):
            for buy_px, sell_px, qty in ((84.50, 102.25, 65), (145.00, 118.40, 130), (38.20, 39.10, 65)):
                ref = calculate_option_trade_costs(
                    buy_price=buy_px,
                    sell_price=sell_px,
                    quantity=qty,
                    apply_slippage=True,
                    slippage_rate_per_leg=0.0025,
                    trade_date=dt_str,
                )
                canon_long = compute_single_leg_roundtrip(
                    option_type="CE",
                    strike=25000.0,
                    signed_qty=qty,
                    entry_price=buy_px,
                    exit_price=sell_px,
                    trade_date=dt_str,
                    apply_slippage=True,
                    slippage_rate_per_leg=0.0025,
                )
                canon_short = compute_single_leg_roundtrip(
                    option_type="PE",
                    strike=25000.0,
                    signed_qty=-qty,
                    entry_price=sell_px,
                    exit_price=buy_px,
                    trade_date=dt_str,
                    apply_slippage=True,
                    slippage_rate_per_leg=0.0025,
                )
                for key in ("brokerage", "stt", "exchange_fee", "sebi_fee", "gst", "stamp_duty", "statutory_charges"):
                    self.assertAlmostEqual(canon_long[key], ref[key], delta=0.01)
                    self.assertAlmostEqual(canon_short[key], ref[key], delta=0.01)
                self.assertEqual(canon_long["gross_pnl"], round((sell_px - buy_px) * qty, 2))
                self.assertEqual(canon_short["gross_pnl"], round((sell_px - buy_px) * qty, 2))

    def test_tick_rounding_and_fail_closed_validation(self) -> None:
        self.assertEqual(round_to_nse_tick(0.02, mode="adverse_sell", allow_zero_settlement=False), 0.05)
        self.assertEqual(round_to_nse_tick(0.0, mode="nearest", allow_zero_settlement=True), 0.0)
        self.assertEqual(apply_adverse_slippage_and_tick(0.05, side="SELL", impact_bps=500.0), 0.05)

        for bad in (0.0, -5.0, float("nan"), float("inf"), None, True):
            with self.assertRaises(ValueError):
                round_to_nse_tick(bad)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            OptionLeg(option_type="CE", strike=25000.0, qty=50, entry_price=100.0, exit_price=110.0, lot_size=65).validate()

        with self.assertRaises(ValueError):
            OptionLeg(option_type="XX", strike=25000.0, qty=65, entry_price=100.0, exit_price=110.0).validate()

    def test_pure_stdlib_import_isolation_for_research_scripts(self) -> None:
        backend_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
        code = (
            "import sys; "
            "import app.analytics.common.options_accounting as oa; "
            "forbidden = [m for m in sys.modules if m.startswith(('fastapi', 'sqlite3', 'pandas', 'upstox_client'))]; "
            "assert not forbidden, f'Unexpected heavy imports: {forbidden}'; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=backend_root,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "OK")


if __name__ == "__main__":
    unittest.main()
