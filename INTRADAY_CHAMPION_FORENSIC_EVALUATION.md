# INTRADAY CHAMPION FORENSIC EVALUATION
**Target Environment:** Stable (`/Users/arunsharma/Desktop/swing trade react`, branch `qlib-production-v1`)  
**Audit Type:** Read-Only Post-Remediation Forensic Champion Validation  
**Scope:** Active Production Intraday Champions Only  
**Date:** October 2, 2026  

---

## 1. Executive Factual Summary

This forensic evaluation investigates the empirical performance of the two active Intraday production champions currently deployed in the Stable repository under corrected, realistic market accounting. Neither champion was modified, retrained, overwritten, or re-parameterized.

### Evaluated Production Champions:
1. **Legacy Intraday Champion**: `backend/models/intraday/champion_ensemble.pkl`
   - **Architecture:** Soft-voting ensemble combining `RandomForestClassifier`, `GradientBoostingClassifier`, and `SVC(probability=True)` on 5 technical features (`rsi`, `macd`, `macd_diff`, `adx`, `returns`).
   - **Artifact SHA-256:** `f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91`
   - **Evaluation Dataset:** 12,465 historical recommendations recorded in `ml_trade_history` (`trade_type = 'INTRADAY'`, `engine = 'LEGACY'`, Aug 27 – Oct 2, 2026).
2. **Qlib Active Intraday Champion**: `backend/models/qlib/challengers/intraday/qlib_challenger_intraday_20260918_171233/model.pkl`
   - **Architecture:** LightGBM Regressor (`LGBModel`) trained on Microsoft Qlib `Alpha158` (158 quantitative factors) at 15-minute bar frequency.
   - **Artifact SHA-256:** `0b42602a7790d598ca24e868bb5e70ef64c2c61ff5cff131a6a4ab5fcbb4f176` (byte-for-byte identical to active pointer `backend/models/qlib/intraday/active_model.pkl`).
   - **Evaluation Datasets:**
     - *Formal OOS Backtest:* 1,160 trades (Sep 4 – Sep 18, 2026) under corrected 15-minute bar-by-bar MTM (`INTRADAY_BAR_MTM_V1`).
     - *Forward Paper Recommendations:* 5,163 trades in `ml_trade_history` and 22,722 virtual recommendations in `qlib_virtual_recommendations` (Sep 21 – Oct 2, 2026).

### Primary Factual Findings:
- **Legacy Intraday Champion:**
  - *Trade-by-Trade Economic Ground Truth (12,025 valid closed trades):* Gross Win Rate 50.59%, Net Win Rate 45.33%, Gross Profit Factor 1.2373, Net Profit Factor 0.5966, Mean Trade Net Return -0.1325%, Cumulative Net Return -1,588.72%.
  - *Constrained Portfolio Simulation (₹100,000 capital, max 5 positions, 1/ticker, 0.1872% friction):* 12,296 signals rejected due to capacity (98.96%); 124 trades executed; Net Win Rate 42.74%; Net Profit Factor 0.3085; Net Portfolio Return -6.25% (Ending capital ₹93,748.35); Max Drawdown 6.08%; Annualized Daily-Session Sharpe -8.74.
- **Qlib Active Intraday Champion:**
  - *Formal Out-of-Sample Backtest (1,160 trades, Sep 4–18):* Gross Win Rate 50.60%, Net Win Rate 36.72% (under 25 bps configured friction), Gross Profit Factor 1.3300, Net Profit Factor 0.7200. Under corrected 15m intraday bar MTM (`INTRADAY_BAR_MTM_V1`), Net Sharpe is -18.79, Max Drawdown is 1.63%, and Final Equity is 98.37.
  - *Forward Paper Production Signals (5,163 signals, 205 executed under 5-slot portfolio constraint, Sep 21–Oct 2):* Net Win Rate 52.20% (107/205), Net Profit Factor 1.6492, Mean Trade Net Return +0.2382%, Net Portfolio Return +10.18% (Ending capital ₹110,175.22), Max Drawdown 1.63%, Annualized Daily-Session Sharpe +4.23 over 10 trading sessions (small sample caveat applies).
  - *Forward Unfiltered Virtual Pool (22,722 signals, Sep 21–Oct 2):* Gross Win Rate 41.38%, Net Win Rate 34.55%, Gross Profit Factor 0.8566, Net Profit Factor 0.6033, Mean Trade Net Return -0.2667%, Cumulative Net Return -5,850.60%.

---

## 2. Stable Runtime Verification

### Classification: VERIFIED

Forensic tracing of the production codebase in `/Users/arunsharma/Desktop/swing trade react` confirms:
1. **Model Loading:**
   - Legacy Intraday Champion is loaded via `ModelManager.load_champion("intraday")` in `backend/app/analytics/model_manager.py:169-192` from `backend/models/intraday/champion_ensemble.pkl`.
   - Qlib Active Champion is loaded via `QlibModelRegistry.load_active_model("INTRADAY")` in `backend/app/qlib_engine/model_registry.py:563-597` from `backend/models/qlib/intraday/active_model.pkl`.
2. **Cryptographic Validation:**
   - `QlibModelRegistry` computes the SHA-256 hash of `active_model.pkl` on every load and validates it against `active_manifest.json:artifact_sha256`. Mismatches raise `QlibModelIntegrityError`.
3. **Decision Pipeline:**
   - Production decision logic resides in `evaluate_ticker` (`backend/app/analytics/decision_engine.py:105-748`). Both manual scans and autonomous autopilot routes execute the same pipeline.
4. **Meta-Learner & Calibration Runtime:**
   - `meta_learner.py` loads `meta_learner_clean_v1.pkl` (SHA-256: `39420042db200d771804f46ff3c1c9842bfddbb2d9774614ff732c525f0a0d4c`).
   - Normal production default operates in `HEURISTIC` mode (`runtime_mode = HEURISTIC`).
   - `calibration.py` enforces domain isolation via `REFUSED_DOMAIN_MISMATCH` when heuristic inputs are passed, preventing double calibration and score domain corruption.
5. **Session Square-Off:**
   - Intraday positions are forced closed at 15:15:00 IST via `intraday_squareoff.py` / `autopilot_scanner.py`. Overnight carry is strictly prohibited for intraday trades.
6. **Portfolio Heat & Sizing:**
   - `kelly_sizer.py` enforces a 6.0% maximum portfolio heat ceiling. Only `LIVE_POSITION` and `PAPER_POSITION` records contribute heat; virtual recommendations (`NOT_A_POSITION`) contribute 0% heat.

---

## 3. Legacy Champion Evaluation

### Classification: FAILED (Net Profitability & Realized Sharpe)

```
========================================================================================
LEGACY INTRADAY CHAMPION: backend/models/intraday/champion_ensemble.pkl
Artifact SHA-256: f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91
========================================================================================
Metric                              Model Metadata Claim     Empirical Realized (DB)
----------------------------------------------------------------------------------------
F1 Score                            0.6850                   N/A (Categorical)
Claimed Win Rate                    58.20%                   45.33% (Net Realized)
Claimed Profit Factor               N/A                      0.5966 (Net Realized)
Claimed Sharpe Ratio                +1.45                    -8.74 (Constrained Portfolio)
Claimed Max Drawdown                8.50%                    6.08% (Constrained Portfolio)
Gross Win Rate                      N/A                      50.59% (6,083 / 12,025)
Net Win Rate                        N/A                      45.33% (5,451 / 12,025)
Gross Profit Factor                 N/A                      1.2373
Net Profit Factor                   N/A                      0.5966
Gross Mean Return / Trade           N/A                      +0.0547%
Net Mean Return / Trade             N/A                      -0.1325%
Total Net Cumulative Return Sum     N/A                      -1,588.72%
========================================================================================
```

### Forensic Finding:
The Legacy Champion's metadata claims (Sharpe 1.45, Win Rate 58.20%) were synthetic baseline priors fitted on an arbitrary 5-row seed matrix during initial setup, not measured live trading metrics. Across 12,025 valid closed historical trades, the model generates a modest gross edge (+0.0547% gross mean return, 1.2373 gross PF), but this edge is entirely consumed by the 0.1872% round-trip statutory friction, producing an overall net loss of -1,588.72% on an unconstrained basis and -6.25% on a 5-slot portfolio basis.

---

## 4. Qlib Active Champion Evaluation

### Classification: PARTIALLY VERIFIED

```
========================================================================================
QLIB ACTIVE CHAMPION: qlib_challenger_intraday_20260918_171233
Artifact SHA-256: 0b42602a7790d598ca24e868bb5e70ef64c2c61ff5cff131a6a4ab5fcbb4f176
Active Pointer: backend/models/qlib/intraday/active_model.pkl (VERIFIED IDENTICAL)
========================================================================================
Dataset / Metric                     OOS Backtest (Sep 4-18)     Forward Paper (Sep 21-Oct 2)
----------------------------------------------------------------------------------------
Total Signals Evaluated              1,160                       5,163 (ml_trade) / 22,722 (virt)
Gross Win Rate                       50.60%                      44.44% (ml_trade) / 41.38% (virt)
Net Win Rate                         36.72%                      40.47% (ml_trade) / 34.55% (virt)
Gross Profit Factor                  1.3300                      1.4161 (ml_trade) / 0.8566 (virt)
Net Profit Factor                    0.7200                      0.9166 (ml_trade) / 0.6033 (virt)
Gross Mean Trade Return              +0.1100%                    +0.1484% (ml_trade) / -0.0795% (virt)
Net Mean Trade Return                -0.1400%                    -0.0388% (ml_trade) / -0.2667% (virt)
Portfolio Net Return (5 slots)       -1.63% (Ending ₹98.37)      +10.18% (Ending ₹110,175.22)
Portfolio Executed Net Win Rate      36.72%                      52.20% (107 / 205 executed)
Portfolio Executed Net PF            0.7200                      1.6492 (ml_trade)
Portfolio Max Drawdown               1.63%                       1.63% (ml_trade)
Annualized Sharpe Ratio              -18.79 (15m MTM)            +4.23 (Daily-session, 10 sessions)
Capital Utilization                  20.00%                      Capped by daily cash recycling
========================================================================================
```

### Forensic Finding:
1. **OOS Backtest Period (Sep 4 – Sep 18):** Under corrected 15-minute bar MTM (`INTRADAY_BAR_MTM_V1`), the model was net negative (-0.14% net mean return, 0.72 net PF, -18.79 Sharpe). The original manifest reported Sharpe 0.00 and Drawdown 0.00% due to an accounting bug (`DAILY_MTM_V1` timestamp mismatch).
2. **Forward Paper Production Signals (Sep 21 – Oct 2):** When filtered through the production decision engine and recorded in `ml_trade_history` under 5-slot portfolio constraints, the model achieved a net win rate of 52.20% (107/205) and net profit factor of 1.6492, yielding +10.18% net portfolio return over 10 sessions. However, the unfiltered raw virtual recommendation stream (22,722 signals) had a net profit factor of 0.6033 and net win rate of 34.55%, proving that the decision engine qualification filters are essential to prevent friction degradation.

---

## 5. Data Provenance

### Classification: VERIFIED

Data feeds and resolution across all evaluated trades:
1. **Historical OHLCV:** Local SQLite `ohlcv` table (1,870,393 rows) providing 15-minute and 1-day candles.
2. **Live Intraday Feeds:** Upstox API v2 (`https://api.upstox.com/v2/market-quote/quotes`) providing real-time quotes, VWAP, day open, and market depth.
3. **Fallback Feeds:** Yahoo Finance 15m delayed quotes used as secondary fallback when Upstox token is unauthenticated.
4. **Data Integrity Flags in Database:**
   - `price_source` distribution across 12,465 legacy trades:
     - `Upstox Real-Time (0ms)`: 6,644 (53.30%)
     - `Yahoo Finance (15m Delay)`: 4,433 (35.56%)
     - `Market Feed`: 1,212 (9.72%)
     - `Model Candle Close`: 129 (1.03%)
     - `Candle Close`: 14 (0.11%)
     - `None`: 33 (0.26%)
   - Trades using `Yahoo Finance (15m Delay)` (35.56%) represent stale quote signals where execution entry price could not be guaranteed at tick accuracy.

---

## 6. Historical Entry Price Provenance

### Classification: FAILED (Zero Live Broker Execution Entries)

A rigorous audit of the `entry_price_source`, `slippage_drag`, and `effective_entry` columns reveals:
1. **Recorded Execution Entries:** **0 (0.00%)**. There are zero live broker order fill prices in `ml_trade_history`.
2. **Synthetic Slippage Deduction:** 10,451 trades (83.84%) had a retroactive constant `0.08%` deducted as `slippage_drag` (`effective_entry = entry * 1.0008` for buy, `entry * 0.9992` for sell).
3. **Reference Prices Masquerading as Executions:** 100% of recorded historical entries were paper/virtual reference quotes generated at signal time, not filled market orders.
4. **Delayed Quotes:** 12,158 trades (97.54%) were tagged with `quote_freshness = 'DELAYED'` in historical records.
5. **Entry Integrity Summary:**
   - Genuine execution entries: 0 (0.0%)
   - Reference market feed entries: 12,158 (97.54%)
   - Candle-close entries: 143 (1.15%)
   - Missing / unclassified entries: 164 (1.31%)

---

## 7. Label Integrity

### Classification: VERIFIED (Remediated Ground Truth)

The historical database previously used `outcome = 'TARGET MET'` as the sole indicator of a winning trade. Under our corrected economic ground truth calculation (`compute_realized_net_pnl`):
```sql
gross_pnl_pct = (exit_price - entry) / entry * 100  [if BULLISH]
net_pnl_pct   = gross_pnl_pct - 0.1872%
```

### Forensic Cross-Tabulation (Legacy Intraday: 12,465 trades):
| Recorded DB Outcome | NET_LOSING | NET_PROFITABLE | NEUTRAL | UNRESOLVED | Total Rows |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SL HIT** | 3,857 | 0 | 0 | 0 | 3,857 |
| **SQUARED OFF (3:15 PM)** | 2,547 | 616 | 347 | 0 | 3,510 |
| **SQUARED OFF (RECONCILED)** | 78 | 139 | 7 | 0 | 224 |
| **TARGET MET** | 92 | 4,696 | 85 | 0 | 4,873 |
| **SYSTEM_TEST_ARTIFACT** | 0 | 0 | 0 | 1 | 1 |
| **Total** | **6,574** | **5,451** | **439** | **1** | **12,465** |

### Critical Observations:
1. **Profitable Square-Offs Uncounted Historically:** 755 square-offs (616 at 3:15 PM + 139 reconciled) were actually net profitable after costs, but were categorized as non-wins under the legacy binary target-met metric.
2. **Target-Met Losing Trades:** 92 trades reached TP1 gross, but transaction costs turned them into net losses.
3. **True Economic Ground Truth:** 5,451 net profitable trades (45.33%), 6,574 net losing trades (54.67%), 439 neutral trades, and 1 unresolved system test row.

---

## 8. Realistic Cost Model

### Classification: VERIFIED

Statutory transaction friction is computed dynamically via `compute_trade_friction_pct` (`backend/app/analytics/trade_economics.py:25-58`):

| Fee Component | Statutory Basis | Configured Rate |
| :--- | :--- | :--- |
| **Brokerage** | Standard discount broker flat/turnover schedule | 0.0600% (0.03% buy + 0.03% sell) |
| **Securities Transaction Tax (STT)** | NSE equity intraday schedule | 0.0250% (sell side turnover) |
| **Exchange Transaction Charges** | NSE cash market schedule | 0.0069% (0.00345% each side) |
| **SEBI Turnover Fee** | ₹10 per crore round-trip | 0.0002% |
| **Stamp Duty** | Indian Stamp Act schedule | 0.0030% (buy side turnover) |
| **Goods & Services Tax (GST)** | 18% on (Brokerage + Exchange + SEBI) | 0.0121% (exact: 0.0671% × 18%) |
| **Execution Slippage Drag** | 4 bps entry + 4 bps exit | 0.0800% |
| **Total Round-Trip Friction** | Sum of configured components | **0.1872% (18.72 bps)** |

*Distinction:* The 0.1872% is a configured statutory and execution assumption based on published Indian regulatory schedules, not an observed execution fill cost, since zero live broker orders have been executed.

---

## 9. Portfolio Reconstruction

### Classification: VERIFIED

Simulations were reconstructed using realistic capital constraints:
- **Starting Capital:** ₹100,000.00
- **Max Concurrent Positions:** 5
- **Max Exposure per Position:** 20% of current equity (₹20,000 initial allocation)
- **Position Allocation:** Cash-limited; no leverage; 1 position per ticker max
- **Round-Trip Friction:** 0.1872% deducted from every closed trade
- **Square-Off:** Daily forced exit by 15:15:00 IST

### Results:
1. **Legacy Intraday Champion:**
   - Signals Evaluated: 12,465
   - Excluded (Missing timestamps): 40 (0.32%)
   - Executable Signals: 12,425
   - **Rejected by Portfolio Constraints:** **12,296 (98.96%)**
   - Executed Trades: 124
   - Ending Capital: ₹93,748.35 (-6.25% Net Return)
   - Realized P&L: ₹-6,251.65 (Costs Paid: ₹4,508.64)
   - Max Drawdown: 6.08%
   - Capital Utilization: Mean 67.37%, Max 100.00%
2. **Qlib Active Champion (Forward Paper Signals):**
   - Signals Evaluated: 5,163
   - Excluded: 0
   - **Rejected by Portfolio Constraints:** **4,958 (96.03%)**
   - Executed Trades: 205
   - Ending Capital: ₹110,175.22 (+10.18% Net Return)
   - Realized P&L: ₹+10,175.22 (Costs Paid: ₹8,290.98)
   - Max Drawdown: 1.63%
   - Capital Utilization: Capped by daily recycling

---

## 10. MTM Methodology

### Classification: VERIFIED

1. **Flaw in Prior `DAILY_MTM_V1` Accounting:**
   - Previous Qlib backtests used midnight timestamps (`00:00:00`) to mark 15-minute intraday bars. Because no intraday bar matches midnight, the equity curve recorded exactly zero daily price changes (flat 100.0 equity), falsely reporting Sharpe = 0.00, Drawdown = 0.00%, and Capital Utilization = 0.00%.
2. **Corrected `INTRADAY_BAR_MTM_V1` Accounting:**
   - Positions are marked at every completed 15-minute bar using actual bar close prices from `ohlcv`.
   - On the Sep 4 – Sep 18 out-of-sample backtest (1,160 trades, 250 intraday marks across 10 sessions), the true MTM equity curve dropped from 100.0 to 98.37, revealing a true Net Sharpe of -18.79 and Max Drawdown of 1.63%.

---

## 11. Gross Performance

### Classification: VERIFIED

Trade-by-trade gross performance (before deducting regulatory fees and slippage):

| Metric | Legacy Intraday (12,025 trades) | Qlib Active OOS (1,160 trades) | Qlib Forward ml_trade (5,031 trades) |
| :--- | :--- | :--- | :--- |
| **Gross Win Rate** | 50.59% (6,083 wins) | 50.60% (587 wins) | 44.44% (2,236 wins) |
| **Gross Profit Factor** | 1.2373 | 1.3300 | 1.4161 |
| **Gross Mean Return** | +0.0547% | +0.1100% | +0.1484% |
| **Gross Median Return**| +0.0234% | N/A | -0.1115% |
| **Total Gross Return Sum**| +655.06% | +127.60% | +746.44% |

---

## 12. Net Performance

### Classification: VERIFIED

Trade-by-trade net performance (after 0.1872% statutory friction):

| Metric | Legacy Intraday (12,025 trades) | Qlib Active OOS (1,160 trades) | Qlib Forward ml_trade (5,031 trades) |
| :--- | :--- | :--- | :--- |
| **Net Win Rate** | 45.33% (5,451 wins) | 36.72% (426 wins) | 40.47% (2,036 wins) |
| **Net Profit Factor** | 0.5966 | 0.7200 | 0.9166 |
| **Net Mean Return** | -0.1325% | -0.1400% | -0.0388% |
| **Net Median Return** | -0.1638% | N/A | -0.2987% |
| **Total Net Return Sum** | -1,588.72% | -162.40% | -195.36% |
| **Total Friction Drag** | 2,243.78% (18.72 bps/trade)| 290.00% (25.0 bps/trade) | 941.80% (18.72 bps/trade) |

*Finding:* On an unconstrained per-signal basis, both champions experience net degradation because average gross win sizes (+0.05% to +0.14%) are smaller than the 0.1872% round-trip cost.

---

## 13. Directional Breakdown

### Classification: VERIFIED

### Legacy Intraday Champion:
- **BULLISH (BUY / LONG):** 1,456 trades (11.68% of signals, 1,405 valid)
  - Gross Win Rate: 46.12% | Net Win Rate: 41.64%
  - Gross Profit Factor: 0.9647 | Net Profit Factor: 0.5239
  - Gross Mean Return: -0.0110% | Net Mean Return: -0.1982%
  - Total Net Return: -275.86%
- **BEARISH (SELL / SHORT):** 11,009 trades (88.32% of signals, 10,620 valid)
  - Gross Win Rate: 51.18% | Net Win Rate: 45.82%
  - Gross Profit Factor: 1.2881 | Net Profit Factor: 0.6092
  - Gross Mean Return: +0.0633% | Net Mean Return: -0.1239%
  - Total Net Return: -1,312.86%

*Asymmetry Analysis:* Legacy Champion exhibits an 88.3% short bias. The long side is negative even before friction (0.96 gross PF).

### Qlib Active Champion (Forward ml_trade):
- **BULLISH (BUY / LONG):** 2,310 trades (45.92% of valid)
  - Gross Win Rate: 40.61% | Net Win Rate: 35.63%
  - Gross Profit Factor: 1.1423 | Net Profit Factor: 0.7268
  - Net Mean Return: -0.1341% | Total Net Return: -309.67%
- **BEARISH (SELL / SHORT):** 2,721 trades (54.08% of valid)
  - Gross Win Rate: 47.70% | Net Win Rate: 44.58%
  - Gross Profit Factor: 1.6695 | Net Profit Factor: 1.0946
  - Net Mean Return: +0.0420% | Total Net Return: +114.31%

*Asymmetry Analysis:* Qlib Active Champion shows substantially better balance (46% Long / 54% Short). The short side achieved a net profit factor of 1.0946 in forward paper trading.

---

## 14. Regime Breakdown

### Classification: PARTIALLY VERIFIED (Data Limitation)

1. **Persisted Regime Data in Legacy Trades:**
   - `UNKNOWN`: 12,438 trades (99.78%) — point-in-time macro state was not serialized into the database for older records.
   - `BEARISH`: 27 trades (0.22%) — Gross WR 51.85%, Net WR 44.44%, Net PF 0.5412.
2. **NIFTY / VIX Historical Data:**
   - The SQLite `ohlcv` table contains 0 rows for `^NSEI` or `INDIAVIX`.
   - Per audit rules, no retrospective regime labels were fabricated using external data.

---

## 15. Time-of-Day Breakdown

### Classification: VERIFIED

Performance segmented by signal entry window:

### Legacy Intraday Champion:
| Time Window (IST) | Trades | Valid | Gross WR | Net WR | Gross PF | Net PF | Net Mean | Net Total |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **09:15–10:00** | 4,154 | 4,137 | 53.03% | 52.36% | 1.2117 | 0.6366 | -0.1301% | -537.14% |
| **10:00–11:00** | 1,511 | 1,450 | 53.86% | 50.07% | 1.0703 | 0.5396 | -0.1679% | -243.17% |
| **11:00–12:00** | 1,255 | 1,231 | 55.56% | 52.15% | 1.2590 | 0.6557 | -0.1196% | -146.77% |
| **12:00–13:00** | 857 | 838 | 69.93% | 65.51% | 2.4056 | 1.2188 | +0.0502% | +41.78% |
| **13:00–14:00** | 1,076 | 1,024 | 50.68% | 44.43% | 0.9660 | 0.4400 | -0.1957% | -199.62% |
| **14:00–15:00** | 1,050 | 973 | 48.30% | 35.97% | 0.8688 | 0.3487 | -0.2162% | -209.91% |
| **15:00–15:15** | 349 | 312 | 41.03% | 22.76% | 0.8455 | 0.2351 | -0.2092% | -64.86% |
| **OUTSIDE_HOURS** | 2,213 | 2,060 | 35.00% | 23.88% | 1.5605 | 0.5850 | -0.1117% | -229.03% |

*Key Finding:* 2,213 legacy signals (17.75%) were recorded outside normal trading hours (09:15–15:15 IST), representing off-market scans or test loops. Only the 12:00–13:00 window demonstrated net profitability (Net PF 1.2188).

### Qlib Active Champion (ml_trade):
| Time Window (IST) | Trades | Valid | Gross WR | Net WR | Gross PF | Net PF | Net Mean | Net Total |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **09:15–10:00** | 1,494 | 1,463 | 37.80% | 35.61% | 1.1610 | 0.7704 | -0.1213% | -177.48% |
| **10:00–11:00** | 1,253 | 1,224 | 47.71% | 44.53% | 1.4739 | 0.9619 | -0.0175% | -21.46% |
| **11:00–12:00** | 646 | 633 | 50.87% | 48.03% | 1.7954 | 1.2059 | +0.0919% | +58.19% |
| **12:00–13:00** | 373 | 371 | 46.36% | 43.67% | 1.5544 | 1.0514 | +0.0250% | +9.29% |
| **13:00–14:00** | 452 | 445 | 43.15% | 38.43% | 1.3675 | 0.8801 | -0.0562% | -25.00% |
| **14:00–15:00** | 504 | 485 | 45.36% | 39.79% | 1.3573 | 0.8538 | -0.0656% | -31.82% |
| **15:00–15:15** | 126 | 119 | 45.38% | 33.61% | 1.6530 | 0.8757 | -0.0424% | -5.04% |
| **OUTSIDE_HOURS** | 315 | 291 | 47.77% | 34.36% | 2.2225 | 0.9730 | -0.0070% | -2.05% |

---

## 16. Out-of-Sample Integrity

### Classification: VERIFIED

Chronological partitioning of the evaluation periods:
1. **Legacy Intraday Champion:**
   - *Training Data:* Synthetic prior seed matrix (pre-August 2026).
   - *Evaluation Period:* August 27, 2026 – October 2, 2026.
   - *Status:* **NOT PRISTINE**. Extensively queried in prior system iterations.
2. **Qlib Active Champion (`qlib_challenger_intraday_20260918_171233`):**
   - *Training Period:* `2024-01-01` to `2026-06-30`
   - *Validation Period:* `2026-07-01` to `2026-08-31`
   - *Formal Backtest OOS:* `2026-09-04` to `2026-09-18` (1,160 trades, frozen in manifest on Sep 18). **NOT PRISTINE** (inspected during promotion).
   - *Forward Paper Period:* `2026-09-21` to `2026-10-02` (5,163 signals in `ml_trade_history`). Strictly out-of-sample relative to model fitting cutoff (`2026-06-30`).

---

## 17. Independent Metric Reproduction

### Classification: VERIFIED (Discrepancies Investigated & Reconciled)

### Comparison: Model Reported Manifest vs Independent Recomputation
| Champion / Metric | Manifest / Metadata Report | Independent Recomputation | Discrepancy Cause |
| :--- | :--- | :--- | :--- |
| **Legacy Sharpe** | +1.45 | -8.74 (Constrained Portfolio) | Metadata was synthetic prior, not production history |
| **Legacy Win Rate** | 58.20% | 45.33% (Net Realized) | Metadata was synthetic prior; DB has true friction |
| **Legacy Profit Factor**| N/A | 0.5966 (Net Realized) | Gross edge consumed by 0.1872% friction |
| **Qlib OOS Trades** | 1,160 | 1,160 | **EXACT MATCH** |
| **Qlib OOS Win Rate**| 36.72% | 36.72% | **EXACT MATCH** |
| **Qlib OOS Net PF** | 0.7200 | 0.7200 | **EXACT MATCH** |
| **Qlib OOS Sharpe** | 0.00 (`DAILY_MTM_V1`) | -18.79 (`INTRADAY_BAR_MTM_V1`)| Timestamp mismatch fixed: midnight vs 15m bars |
| **Qlib OOS Max DD** | 0.00% (`DAILY_MTM_V1`)| 1.63% (`INTRADAY_BAR_MTM_V1`) | Timestamp mismatch fixed: midnight vs 15m bars |
| **Qlib Forward Sharpe**| N/A | +4.23 (10 daily sessions) | Reconstructed on 205 executed portfolio trades |

---

## 18. Legacy vs Qlib Descriptive Comparison

### Classification: VERIFIED

| Evaluation Metric | Legacy Intraday Champion | Qlib Active Intraday Champion |
| :--- | :--- | :--- |
| **Architecture** | RF + GB + SVC Voting Ensemble | LightGBM Regressor (Alpha158) |
| **Model Features** | 5 Technical Indicators | 158 Quantitative Alpha Factors |
| **Evaluation Period** | Aug 27 – Oct 2, 2026 (36 days) | Sep 21 – Oct 2, 2026 (12 days paper) |
| **Total Recorded Signals** | 12,465 | 5,163 (ml_trade) / 22,722 (virtual) |
| **Gross Win Rate** | 50.59% | 44.44% (ml_trade) |
| **Net Win Rate (Unconstrained)**| 45.33% | 40.47% (ml_trade) |
| **Gross Profit Factor** | 1.2373 | 1.4161 (ml_trade) |
| **Net Profit Factor** | 0.5966 | 0.9166 (ml_trade) |
| **Net Mean Trade Return** | -0.1325% | -0.0388% (ml_trade) |
| **Portfolio Executed Trades**| 124 (12,296 rejected / 98.96%)| 205 (4,958 rejected / 96.03%) |
| **Portfolio Executed Net WR** | 42.74% | 52.20% |
| **Portfolio Executed Net PF** | 0.3085 | 1.6492 |
| **Portfolio Net Return (₹100k)**| -6.25% (₹93,748.35) | +10.18% (₹110,175.22) |
| **Portfolio Max Drawdown** | 6.08% | 1.63% |
| **Portfolio Sharpe Ratio** | -8.74 (19 trading sessions) | +4.23 (10 trading sessions) |
| **Directional Bias** | 88.3% Short / 11.7% Long | 54.1% Short / 45.9% Long |
| **Average Holding Time** | 21.6 minutes | 89.0 minutes |
| **Data Quality Exclusions** | 40 missing timestamps (0.32%) | 0 missing timestamps (0.00%) |

---

## 19. Data Limitations

1. **Zero Live Broker Fills:** All historical database entries represent virtual paper signals generated by the scanner; no tick-by-tick order fills from an exchange order book are present.
2. **Slippage Representation:** 83.84% of trades had a fixed synthetic constant (0.08%) deducted retroactively rather than measuring live quote-to-fill execution slippage.
3. **Stale Quotes in Historical Scans:** 35.56% of legacy recommendations were generated using 15-minute delayed Yahoo Finance quotes when Upstox authentication was offline.
4. **Macro Point-in-Time Data:** Point-in-time macro regime was missing from 99.78% of legacy trade rows in the database, preventing historical regime stratification.
5. **Sample Size for Forward Qlib:** The forward paper evaluation of Qlib covers 10 trading sessions (Sep 21 – Oct 2, 2026). While positive (+10.18% net return, +4.23 Sharpe), a 10-session sample size is too short to guarantee statistical stationarity.

---

## 20. Remaining Uncertainties

1. **Execution Liquidity in Live Trading:** In live execution, market impact and liquidity constraints on mid-cap constituents may widen slippage beyond the modeled 0.08%.
2. **Slot Allocation Determinism:** In both models, 96% to 99% of generated signals are rejected because the 5 portfolio slots are full. The exact order of signal evaluation within a 15-minute bar determines which stocks enter the portfolio, creating path dependency.
3. **Square-Off Slippage at 15:15:** Forced session square-offs at 15:15 IST can coincide with institutional portfolio rebalancing auctions, where spreads widen and market-on-close orders may experience adverse fills.

---

## Safety Final Verification

At the conclusion of this read-only forensic evaluation:
- **Legacy Champion Artifact:** `backend/models/intraday/champion_ensemble.pkl`  
  SHA-256: `f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91` (**UNCHANGED**)
- **Qlib Active Champion Artifact:** `backend/models/qlib/challengers/intraday/qlib_challenger_intraday_20260918_171233/model.pkl`  
  SHA-256: `0b42602a7790d598ca24e868bb5e70ef64c2c61ff5cff131a6a4ab5fcbb4f176` (**UNCHANGED**)
- **Qlib Active Model Pointer:** `backend/models/qlib/intraday/active_model.pkl`  
  SHA-256: `0b42602a7790d598ca24e868bb5e70ef64c2c61ff5cff131a6a4ab5fcbb4f176` (**UNCHANGED**)
- **Historical Database:** `backend/market_data.db`
  - `ml_trade_history`: 18,494 rows (**UNCHANGED**)
  - `qlib_virtual_recommendations`: 23,089 rows (**UNCHANGED**)
  - `ohlcv`: 1,870,393 rows (**UNCHANGED**)
  - `consensus_trade_history`: 782 rows (**UNCHANGED**)
  - `forward_simulation_trades`: 0 rows (**UNCHANGED**)
  - `ml_trade_history` Full-Content Fingerprint SHA-256: `692b84446ec1a7a31be40fb61c390366bf6b884fe24353e19c63601b09348266` (**BYTE-FOR-BYTE IDENTICAL TO PRE-AUDIT BASELINE**)
- **Live Trading & Environment:** Zero broker orders submitted; no model retraining; no model promotions; no production configuration changes; zero trading threshold adjustments.
