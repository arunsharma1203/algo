---
trigger: glob
globs: "backend/**/*.py"
description: "Canonical NSE/Upstox options accounting, signed multi-leg conventions, 0.05 tick rounding, and mandatory golden/prefix pytest gates."
---

# NSE Options Microstructure & Canonical Accounting Rules

## 1. Single Canonical Source of Truth
- Route all new option P&L, multi-leg spread cashflows, Upstox statutory fees, adverse `₹0.05` tick rounding, NSE expiry settlement, and structural risk calculations through `backend/app/analytics/common/options_accounting.py` (pure Python stdlib, zero DB/network side effects at import time).
- Do not introduce new inline fee formulas, single-leg shortcuts for hedged spreads, hardcoded margin constants, or fallback fee literals (such as `or 55.0`).
- Do not modify existing live/paper execution callers without explicit user approval and golden parity verification.

## 2. Explicit Signed Leg, Tick & Brokerage Conventions
- **Signed Quantity**: `qty > 0` for bought/long option legs; `qty < 0` for written/short option legs (`abs(qty)` must be a positive multiple of `lot_size` when lot validation is enabled).
- **Cashflow Identity**: Entry cashflow = `-qty * entry_fill_price`; Exit/settlement cashflow = `+qty * exit_fill_price`; Gross P&L = `sum(entry_cashflow + exit_cashflow)` across all physical legs.
- **Adverse NSE Tick Rounding (`₹0.05`)**: Apply slippage/spread first, then round adversely to `₹0.05` (`ROUND_CEILING` when buying/covering, `ROUND_FLOOR` when selling/exiting long), floored at `₹0.05` for market orders and `₹0.00` only for cash-settled worthless expiry.
- **Per-Order Brokerage & Statutory Tariff**: Charge `₹20.00` per **executed market order** (e.g., 2-leg vertical open + market close = `4` orders = `₹80.00`; worthless OTM cash-settled expiry = `0` exit orders). Apply effective-dated Upstox rates (sell-premium STT, NSE+IPFT composite turnover, SEBI `0.0001%`, buy-side Stamp `0.003%`, `18%` GST on `brokerage + exchange_fee` excluding SEBI).

## 3. NSE European Index Expiry & Incomplete-Leg Risk
- NIFTY/BANKNIFTY index options are **European and cash-settled** (no early or stock pin assignment).
- Un-squared-off ITM **long** (`qty > 0`) options held through expiry incur statutory **exercise STT on intrinsic value** (`0.125%` `< 2026-04-01`; `0.15%` `>= 2026-04-01`), whereas short ITM writers (`qty < 0`) pay sell-premium STT at entry, not exercise STT.
- Multi-leg risk checks must report **both** `final_package_max_loss_inr` AND `buy_first_incomplete_leg_exposure_inr` (peak exposure if bought wing fills while short leg rejects/misses).

## 4. Mandatory Verification Gate
- Before concluding any change touching options math, backtesting, or feature pipelines, run in isolated test mode (`set_test_db_override`):
  ```bash
  /usr/bin/python3 -B -m unittest discover -v -s backend/tests/golden -p "test_*.py"
  # or inside backend venv:
  backend/venv/bin/python -B -m unittest discover -v -s backend/tests/golden -p "test_*.py"
  ```
- Never weaken a golden fixture or prefix-invariance assertion to make a failing test pass.
