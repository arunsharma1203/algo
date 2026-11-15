---
trigger: always_on
description: "Strict point-in-time causality, chronological expiry-embargo splits, fail-closed missing-data handling, and zero inline math/gate re-implementation across repo and /private/tmp audits."
---

# Causal Point-in-Time & Anti-Hallucination Invariants

These rules apply unconditionally across **both** repository code and standalone `/private/tmp/` research/audit scripts.

## 1. Strict Point-in-Time Indexing (`timestamp <= t`)
- Every feature, regime tag, VWAP, OI baseline, opening-range bound (`orb15`/`orb30`), and ML input evaluated at decision time `t` MUST depend strictly on completed market data with `bar_close_ts <= t`.
- **Hard Bans**:
  - `.shift(-n)` with `n > 0`, `bfill()`, `fillna(method="bfill")`, or future interpolation.
  - Full-series / full-session normalization (`mean`/`std`/`min`/`max`/`quantile` computed across `0..T` instead of `0..t`).
  - Passing unsliced full-day bar dictionaries when evaluating pre-warmup timestamps (`< 09:45 IST`).
  - Consuming multi-timeframe candles (e.g., `15m` heavyweights) before their completion boundary (`bar_start + timeframe <= decision_ts`).

## 2. Intra-Bar Event & Capital Causality
- Within any bar `t`, `open` events strictly precede `high`/`low`/`close` events.
- A simulator entry evaluated for `bar[t].open` MUST NEVER consume cash, margin reserve, or position-slot capacity released by an exit at `bar[t].close`.
- Trailing ratchets on 1-minute OHLC bars without tick order must update strictly from completed bar `close` (`compute_close_only_ratchet`), never assuming favorable intrabar `high`-before-`low` ordering.

## 3. Chronological Splits, Fit-on-Train Only & Multiplicity Accounting
- Use strictly chronological train / validation / holdout splits with contract-expiry embargo gaps so no open trade or overlapping weekly option lifecycle straddles a split boundary. Random K-Fold on financial time series is forbidden.
- Fit all scalers, imputers, KNN regime memories, PAV/isotonic calibrators, and probability thresholds strictly on the training fold; apply frozen transforms out-of-sample.
- Log total candidate/parameter trials (`N_trials`) and evaluate incremental gate/ML value on **exact matched-opportunity cohorts** (never conflate censoring/trade-count reduction with per-trade edge).

## 4. Fail-Closed Missing Data & Honest Risk/Margin Bounds
- If required option quotes, hedge wings, IV/OI, or bar records are missing, stale, zero (`bid <= 0`), or crossed (`ask < bid`), fail closed (`None` / `BLOCKED` / skip trade). Never invent synthetic option prices or silently emit `0` / `0%` metrics.
- Never certify capital adequacy or return-on-margin for short/spread options from local heuristic constants (`₹26.5k`, `₹55k`, `₹135k`); tag local margin floors `UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN`.

## 5. Zero Re-Typed Math or Control Flow in Research Scripts
- Never re-type option P&L, Upstox statutory fees, `₹0.05` tick rounding, or spread risk math inline—even in `/private/tmp/` scripts. Import `app.analytics.common.options_accounting` directly.
- Never re-type production `if`/`elif` signal or exit gates from memory in audits; import the pure production evaluator directly or prove equivalence with an automated matched-input parity test.
