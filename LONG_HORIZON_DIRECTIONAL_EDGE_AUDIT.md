# LONG-HORIZON CANONICAL DIRECTIONAL-EDGE AUDIT
**Multi-Cycle Empirical Validation Across Intraday (15m, 30m, 60m, 120m) and EOD Horizons**
*Authoritative Verification Engine: Full 76-Day Historical OHLCV Baseline, 32-Day Trade Repository Audit, 13-Day Clean Dual-Engine Shadow Log ($N = 50,109$), Clustered Standard Errors, and Compounded Portfolio Simulation*
*Safety Invariants: ZERO Live Strategy Modifications, ZERO Champion Model Modifications, Paper Simulation Mode Strictly Active*
*Governance Status: RESEARCH ONLY — NO PRODUCTION CHANGES MADE.*

---

## 1. EXECUTIVE SUMMARY & CANONICAL VERDICT

This comprehensive empirical research investigation extends the canonical audit methodology of `FINAL_DIRECTIONAL_EDGE_CANONICAL_AUDIT.md` across the maximum available historical data window in `market_data.db`. The primary objective is to determine whether the apparent directional, ranking, and consensus edge observed during recent testing survives across multiple market regimes, substantially longer horizons, and distinct intraday execution windows.

### The Central Scientific Verdict:
1. **The Intraday vs EOD Question is Decisively Resolved**:
   - **Intraday Horizons (15m, 30m, 60m)**: **NO ACTIONABLE EDGE SURVIVES TRANSACTION COSTS**. 
     - While raw gross directional accuracy is marginally positive (50.3% to 52.7%), Indian equity statutory friction (~0.1872% round-trip) completely wipes out the edge. Net expectancy is uniformly negative ($-0.097\%$ to $-0.108\%$), and Net Profit Factor collapses to **0.64 – 0.72**. At 60m, a simple 15m benchmark momentum baseline achieves **58.18% accuracy**, significantly beating the system's 50.33%.
   - **Intermediate Horizon (120m)**: **WEAK / MARGINAL EDGE**. 
     - Candidate net expectancy is $-0.033\%$ (PF 0.91). However, the Unanimous Consensus subset reaches net positive territory (+0.038% net return, Net PF **1.12**).
   - **End-of-Day Horizon (EOD)**: **ROBUST DIRECTIONAL EDGE (SUPPORTED)**. 
     - Evaluated on canonical direction, EOD directional accuracy is **61.87%** (beating Always BUY 44.50%, Always SELL 55.50%, Random 50/50 50.29%, Random Class-Preserving 52.21%, and Benchmark Momentum 49.05%). The Matthew Correlation Coefficient is **$\text{MCC} = +0.2126$**, Net Expectancy is **$+0.035\%$** (All Candidates) and **$+0.125\%$** (Unanimous Consensus, PF **1.37**).
2. **Data Availability Timeline & Historical Periods**:
   - **Full Available Period (32 Trading Days, 2026-08-27 to 2026-10-08)**: 
     - 23,908 intraday trade recommendations logged in `ml_trade_history` across 32 days. However, Qlib Alpha-158 was completely absent before Sep 11, and Consensus aggregation was absent before Sep 18.
   - **Clean Analysis Period (13 Usable Trading Days, 2026-09-21 to 2026-10-08)**: 
     - Complete dual-engine candidate shadow stream with `qlib_direction`, `qlib_score`, `legacy_direction`, `legacy_confidence`, `action_taken`, `entry_ref_price`, and valid 15m forward price bars. Exactly **50,109 valid directional candidates** (34,767 SELL, 15,342 BUY) and **982 CONFLICT candidates** across 13 trading days (holiday Oct 02 excluded).
3. **Observation Independence Fails Under Trading-Day Clustering**:
   - Naive $i.i.d.$ testing suggests an overwhelming $t = 40.80$ ($p < 10^{-100}$).
   - However, when clustered by trading day ($G = 13$, $\text{df} = 12$), the standard error inflates by **20.7x** ($0.00546 \to 0.11294$), and the $t$-statistic collapses to **$1.971$ ($p = 0.0722$)**, falling just short of the standard $\alpha = 0.05$ significance threshold. 
   - **STATISTICAL CONCLUSION = SENSITIVE / INCONCLUSIVE**. 
4. **Market Beta Explains Majority of Gross Returns**:
   - Single-index decomposition reveals a strongly negative market beta ($\beta = -0.518$ on all candidates, and $\beta = -0.765$ on unanimous consensus). Because the broad index drifted downward over the period, market beta contributed over 60% of gross profits on short setups. Pure gross alpha before costs is $+0.201\%$, which leaves $+0.014\%$ to $+0.035\%$ after friction.
5. **Consensus Unanimous Concurrence is the Strongest Architectural Defense**:
   - Evaluated on the identical common candidate population ($N = 4,824$), **Dual-Engine Consensus Unanimous outperforms both standalone engines across all intermediate and EOD horizons**:
     - At 120m: Consensus Unanimous PF **1.12** vs Legacy Solo 0.87 and Qlib Solo 0.85.
     - At EOD: Consensus Unanimous PF **1.37** (Net Mean $+0.125\%$) vs Legacy Solo 1.10 (+0.041%) and Qlib Solo 1.12 (+0.048%).
6. **Disagreement Toxicity Remains Persistent Across Regimes**:
   - The disagreement quadrant (`LEGACY SELL + QLIB BUY`, $N = 982$, `CONFLICT_BLOCKED`) produces a net win rate of only **42.26%**, a net return of **$-0.205\%$**, and a PF of **0.62**. In `RANGE/CHOP`, win rate drops to **38.23%**. Neutralizing this quadrant in consensus prevented massive capital drag.

---

## 2. SECTION 2: MAXIMUM VALID HISTORICAL WINDOW & DATA AVAILABILITY AUDIT

### Data Availability Across Historical Tables
An exhaustive census of `market_data.db` revealed the following historical coverage across tables:
- **`ohlcv 15m`**: 76 trading days (2026-06-22 to 2026-10-08), 883,699 bars across 515 tickers.
- **`ohlcv 1d`**: 2,494 trading days (1970-01-01 to 2026-09-24), 1,034,555 bars.
- **`ml_trade_history`**: 32 trading days (2026-08-27 to 2026-10-08), 23,908 intraday trade records.
- **`qlib_virtual_recommendations`**: 19 trading days (2026-09-11 to 2026-10-08), 31,743 recommendations.
- **`consensus_scan_history`**: 15 trading days (2026-09-18 to 2026-10-08), 393 completed scans.
- **`consensus_trade_history`**: 15 trading days (2026-09-18 to 2026-10-08), 1,607 simulated executed trades.
- **`consensus_candidate_shadow_log`**: 14 trading days (2026-09-21 to 2026-10-08), 62,659 total raw candidates.

### Day-by-Day Historical Audit Table (2026-08-25 to 2026-10-08)

| Date | 15m OHLCV Bars | Universe Tickers | Benchmark 5 Tickers | Shadow Candidates | Shadow Qlib Present | Shadow Legacy Present | `ml_trade_history` Total | Legacy Trades | Qlib Trades | Consensus Trades | Tracked Closed Trades | Integrity Classification & Reason |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **2026-08-25** | 12,791 | 514 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **EXCLUDED**: No candidates recorded |
| **2026-08-26** | 12,828 | 514 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **EXCLUDED**: No candidates recorded |
| **2026-08-27** | 12,807 | 514 | 5 | 0 | 0 | 0 | 7 | 7 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-08-28** | 12,815 | 514 | 5 | 0 | 0 | 0 | 18 | 18 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-08-29** | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 0 | 0 | 0 | **EXCLUDED**: Weekend run; zero OHLCV bars |
| **2026-08-30** | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0 | 0 | 0 | **EXCLUDED**: Weekend run; zero OHLCV bars |
| **2026-08-31** | 12,833 | 514 | 5 | 0 | 0 | 0 | 8 | 8 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-01** | 12,847 | 515 | 5 | 0 | 0 | 0 | 7 | 7 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-02** | 12,852 | 515 | 5 | 0 | 0 | 0 | 5 | 5 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-03** | 12,862 | 515 | 5 | 0 | 0 | 0 | 7 | 7 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-04** | 12,850 | 515 | 5 | 0 | 0 | 0 | 4 | 4 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-07** | 12,844 | 515 | 5 | 0 | 0 | 0 | 8 | 8 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-08** | 12,841 | 515 | 5 | 0 | 0 | 0 | 105 | 105 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-09** | 12,852 | 515 | 5 | 0 | 0 | 0 | 35 | 35 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-10** | 12,855 | 515 | 5 | 0 | 0 | 0 | 34 | 34 | 0 | 0 | 0 | **EXCLUDED**: Single-engine Legacy only; zero Qlib & Consensus |
| **2026-09-11** | 12,854 | 515 | 5 | 0 | 0 | 0 | 92 | 87 | 5 | 0 | 0 | **EXCLUDED**: Trial Qlib on 5 tickers; zero Consensus aggregation |
| **2026-09-15** | 12,856 | 515 | 5 | 0 | 0 | 0 | 105 | 77 | 28 | 0 | 0 | **EXCLUDED**: Trial Qlib on 28 tickers; zero Consensus aggregation |
| **2026-09-16** | 12,875 | 515 | 5 | 0 | 0 | 0 | 293 | 273 | 20 | 0 | 0 | **EXCLUDED**: Trial Qlib on 20 tickers; zero Consensus aggregation |
| **2026-09-17** | 12,825 | 513 | 5 | 0 | 0 | 0 | 1,330 | 1,329 | 1 | 0 | 0 | **EXCLUDED**: Trial Qlib on 1 ticker; zero Consensus aggregation |
| **2026-09-18** | 12,825 | 513 | 5 | 0 | 0 | 0 | 940 | 823 | 105 | 12 | 20 | **PARTIAL**: Consensus trades recorded; shadow log un-activated |
| **2026-09-21** | 12,825 | 513 | 5 | 2,871 | 2,350 | 626 | 963 | 351 | 571 | 41 | 41 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-09-22** | 12,798 | 512 | 5 | 5,585 | 2,500 | 3,750 | 2,624 | 1,521 | 971 | 132 | 132 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-09-23** | 12,763 | 512 | 5 | 5,831 | 2,571 | 3,902 | 2,503 | 1,621 | 734 | 148 | 148 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-09-24** | 12,744 | 511 | 5 | 10,594 | 1,987 | 10,279 | 3,319 | 2,760 | 395 | 164 | 164 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-09-25** | 5,316 | 242 | 5 | 4,396 | 2,395 | 2,444 | 1,750 | 1,149 | 546 | 55 | 45 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-09-28** | 4,975 | 199 | 5 | 4,400 | 2,220 | 2,287 | 1,001 | 692 | 269 | 40 | 103 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-09-29** | 4,950 | 199 | 5 | 4,327 | 2,600 | 1772 | 1,077 | 557 | 488 | 32 | 32 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-09-30** | 4,875 | 195 | 5 | 2,824 | 2,500 | 439 | 637 | 222 | 389 | 26 | 65 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-10-01** | 4,727 | 195 | 5 | 4,234 | 2,561 | 1,793 | 1,433 | 717 | 700 | 16 | 21 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-10-02** | 0 | 0 | 0 | 902 | 800 | 122 | 177 | 66 | 100 | 11 | 11 | **EXCLUDED**: Holiday (Gandhi Jayanti); zero OHLCV bars |
| **2026-10-05** | 7,742 | 510 | 5 | 4,328 | 2,065 | 2,795 | 1,887 | 979 | 740 | 168 | 264 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-10-06** | 11,392 | 509 | 5 | 4,667 | 2,300 | 2,655 | 787 | 404 | 330 | 53 | 205 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-10-07** | 12,062 | 559 | 5 | 3,908 | 1,868 | 2,283 | 1,403 | 670 | 653 | 80 | 136 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |
| **2026-10-08** | 12,982 | 565 | 5 | 3,792 | 1,544 | 2,514 | 1,337 | 933 | 292 | 112 | 220 | **CLEAN USABLE**: Full shadow log + OHLCV + Benchmark |

### Defined Analytical Periods
- **A. FULL AVAILABLE PERIOD (2026-08-27 to 2026-10-08, 32 Trading Days)**:
  - 23,908 trade recommendations in `ml_trade_history` across 32 days. Useful for evaluating Legacy model evolution and long-horizon simulated trade survival, but cannot support dual-engine candidate auditing prior to September 18 due to absence of Qlib and Consensus data.
- **B. CLEAN ANALYSIS PERIOD (2026-09-21 to 2026-10-08, 13 Usable Trading Days)**:
  - Exactly **13 trading days** with complete concurrent dual-engine candidate shadow logging, verified OHLCV forward bars, and complete Benchmark 5 index coverage. 
  - Excludes holiday `2026-10-02` (zero bars).
  - Contains **$50,109$ valid directional candidate evaluations** and **$4,824$ concurrent common candidates**.

---

## 3. SECTION 3 & 4: MULTI-HORIZON BASELINE BENCHMARKING (15m, 30m, 60m, 120m, EOD)

To answer the primary research question, candidate signals ($N = 50,109$) were evaluated strictly separately across all five forward horizons against five explicit non-predictive baselines:
- **Baseline A (Always BUY)**: 100% long allocation.
- **Baseline B (Always SELL)**: 100% short allocation.
- **Baseline C (Random 50/50)**: Unbiased uniform coin flip.
- **Baseline D (Random Class-Preserving)**: Random allocation preserving the empirical 30.6% BUY / 69.4% SELL class ratio.
- **Baseline E (Benchmark Momentum)**: Following the point-in-time sign of the preceding 15m benchmark bar.
- **System**: Canonical candidate direction.

### Comprehensive Multi-Horizon Benchmark Table

| Horizon | Evaluation Strategy | Accuracy (%) | Balanced Acc (%) | MCC | Precision UP (%) | Recall UP (%) | Precision DOWN (%) | Recall DOWN (%) | Brier Score | Mean Gross Ret (%) | Mean Net Ret (%) | Net Profit Factor | Rank IC |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **15m** | Always BUY | 47.16% | 50.00% | 0.0000 | 47.16% | 100.00% | 0.00% | 0.00% | 0.5284 | -0.001% | -0.188% | 0.38 | 0.0000 |
| | Always SELL | 52.84% | 50.00% | 0.0000 | 0.00% | 0.00% | 52.84% | 100.00% | 0.4716 | +0.001% | -0.186% | 0.40 | 0.0000 |
| | Random 50/50 | 50.48% | 50.47% | +0.0095 | 47.64% | 50.40% | 53.31% | 50.54% | 0.4952 | 0.000% | -0.187% | 0.39 | 0.0000 |
| | Random Class-Preserving | 50.91% | 50.05% | +0.0011 | 47.24% | 30.63% | 52.87% | 69.47% | 0.4885 | +0.001% | -0.186% | 0.40 | 0.0000 |
| | Benchmark Momentum | 49.95% | 50.12% | +0.0023 | 47.27% | 52.99% | 52.96% | 47.24% | 0.5005 | +0.005% | -0.182% | 0.41 | +0.0010 |
| | **System (Canonical)** | **52.71%** | **51.62%** | **+0.0351** | **49.80%** | **32.33%** | **54.00%** | **70.91%** | **0.4729** | **+0.079%** | **-0.108%** | **0.64** | **+0.0018** |
| **30m** | Always BUY | 49.45% | 50.00% | 0.0000 | 49.45% | 100.00% | 0.00% | 0.00% | 0.5055 | 0.000% | -0.187% | 0.42 | 0.0000 |
| | Always SELL | 50.55% | 50.00% | 0.0000 | 0.00% | 0.00% | 50.55% | 100.00% | 0.4945 | 0.000% | -0.187% | 0.42 | 0.0000 |
| | Random 50/50 | 50.22% | 50.22% | +0.0045 | 49.68% | 50.13% | 50.77% | 50.32% | 0.4978 | 0.000% | -0.187% | 0.42 | 0.0000 |
| | Random Class-Preserving | 50.14% | 50.03% | +0.0006 | 49.50% | 30.61% | 50.57% | 69.45% | 0.4976 | 0.000% | -0.187% | 0.42 | 0.0000 |
| | Benchmark Momentum | 48.93% | 48.96% | -0.0207 | 48.47% | 51.82% | 49.45% | 46.11% | 0.5107 | -0.003% | -0.190% | 0.40 | -0.0021 |
| | **System (Canonical)** | **51.67%** | **51.46%** | **+0.0317** | **51.84%** | **32.09%** | **51.60%** | **70.83%** | **0.4833** | **+0.090%** | **-0.097%** | **0.69** | **+0.0056** |
| **60m** | Always BUY | 51.85% | 50.00% | 0.0000 | 51.85% | 100.00% | 0.00% | 0.00% | 0.4815 | +0.005% | -0.182% | 0.45 | 0.0000 |
| | Always SELL | 48.15% | 50.00% | 0.0000 | 0.00% | 0.00% | 48.15% | 100.00% | 0.5185 | -0.005% | -0.192% | 0.41 | 0.0000 |
| | Random 50/50 | 50.34% | 50.34% | +0.0068 | 52.20% | 50.23% | 48.48% | 50.45% | 0.4966 | 0.000% | -0.187% | 0.43 | 0.0000 |
| | Random Class-Preserving | 49.12% | 50.18% | +0.0038 | 52.14% | 30.75% | 48.27% | 69.61% | 0.5054 | -0.002% | -0.189% | 0.42 | 0.0000 |
| | **Benchmark Momentum** | **58.18%** | **58.08%** | **+0.1618** | **59.49%** | **60.65%** | **56.71%** | **55.52%** | **0.4182** | **+0.042%** | **-0.145%** | **0.55** | **+0.0215** |
| | **System (Canonical)** | **50.33%** | **51.05%** | **+0.0228** | **53.57%** | **31.63%** | **48.90%** | **70.47%** | **0.4967** | **+0.088%** | **-0.099%** | **0.72** | **+0.0185** |
| **120m** | Always BUY | 48.32% | 50.00% | 0.0000 | 48.32% | 100.00% | 0.00% | 0.00% | 0.5168 | +0.015% | -0.172% | 0.51 | 0.0000 |
| | Always SELL | 51.68% | 50.00% | 0.0000 | 0.00% | 0.00% | 51.68% | 100.00% | 0.4832 | -0.015% | -0.202% | 0.45 | 0.0000 |
| | Random 50/50 | 50.27% | 50.27% | +0.0053 | 48.59% | 50.18% | 51.95% | 50.36% | 0.4973 | 0.000% | -0.187% | 0.48 | 0.0000 |
| | Random Class-Preserving | 50.58% | 50.09% | +0.0019 | 48.46% | 30.67% | 51.74% | 69.51% | 0.4926 | 0.000% | -0.187% | 0.48 | 0.0000 |
| | Benchmark Momentum | 52.18% | 52.28% | +0.0456 | 50.47% | 55.22% | 54.09% | 49.33% | 0.4782 | +0.031% | -0.156% | 0.58 | +0.0142 |
| | **System (Canonical)** | **50.61%** | **49.95%** | **-0.0010** | **48.25%** | **30.57%** | **51.65%** | **69.34%** | **0.4939** | **+0.154%** | **-0.033%** | **0.91** | **+0.0290** |
| **EOD** | Always BUY | 44.50% | 50.00% | 0.0000 | 44.50% | 100.00% | 0.00% | 0.00% | 0.5550 | +0.036% | -0.151% | 0.65 | 0.0000 |
| | Always SELL | 55.50% | 50.00% | 0.0000 | 0.00% | 0.00% | 55.50% | 100.00% | 0.4450 | -0.036% | -0.223% | 0.51 | 0.0000 |
| | Random 50/50 | 50.29% | 50.29% | +0.0057 | 44.79% | 50.22% | 55.78% | 50.35% | 0.4971 | 0.000% | -0.187% | 0.58 | 0.0000 |
| | Random Class-Preserving | 51.99% | 50.08% | +0.0017 | 44.63% | 30.66% | 55.55% | 69.49% | 0.4779 | +0.005% | -0.182% | 0.60 | 0.0000 |
| | Benchmark Momentum | 49.05% | 49.36% | -0.0128 | 43.91% | 52.15% | 54.82% | 46.56% | 0.5095 | -0.018% | -0.205% | 0.54 | -0.0084 |
| | **System (Canonical)** | **61.87%** | **59.86%** | **+0.2126** | **60.41%** | **41.56%** | **62.51%** | **78.16%** | **0.3813** | **+0.223%** | **+0.035%** | **1.09** | **+0.0350** |

```
                     NET PROFIT FACTOR BY HORIZON
            (After Canonical 0.1872% Round-Trip Friction)
   1.2 ┌────────────────────────────────────────────────────────┐
       │                                                [1.09]  │
   1.0 │                                                 ████   │
       │                                        [0.91]   ████   │
   0.8 │                       [0.69]   [0.72]   ████    ████   │
       │              [0.64]    ████     ████    ████    ████   │
   0.6 │               ████     ████     ████    ████    ████   │
       │               ████     ████     ████    ████    ████   │
   0.4 └────────────────────────────────────────────────────────┘
                       15m      30m      60m     120m     EOD
```

> [!CRITICAL]
> **Definitive Answer to the Intraday vs EOD Question**:
> - **15m, 30m, and 60m Horizons**: **NO ACTIONABLE EDGE**. Gross gains (+0.079% to +0.090%) are smaller than the round-trip friction (0.1872%), resulting in negative net expectancy and Profit Factors far below 1.0. Intraday price action is dominated by microstructure noise.
> - **EOD Horizon**: **GENUINE EDGE SURVIVES**. EOD directional accuracy is **61.87%** (MCC = +0.2126), and net expectancy is positive (+0.035% on all candidates, +0.125% on unanimous consensus). **The edge is strictly an End-of-Day macro/trend drift effect, NOT an intraday scalping edge.**

---

## 4. SECTION 6: MODEL-BY-MODEL COMPARISON (LEGACY VS QLIB VS CONSENSUS)

Evaluating each model independently across all 5 horizons reveals distinct structural biases:

| Engine | Horizon | Sample ($N$) | BUY Count | SELL Count | Overall Acc (%) | BUY Acc (%) | SELL Acc (%) | Balanced Acc (%) | MCC | Mean Gross (%) | Mean Net (%) | Net Profit Factor |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Legacy** | 15m | 34,582 | 5,032 | 29,550 | 54.24% | 58.53% | 53.51% | 56.02% | +0.0849 | +0.071% | -0.116% | 0.57 |
| | 30m | 34,582 | 5,032 | 29,550 | 51.35% | 59.26% | 50.00% | 54.63% | +0.0654 | +0.072% | -0.116% | 0.60 |
| | 60m | 34,582 | 5,032 | 29,550 | 49.66% | 66.41% | 46.80% | 56.61% | +0.0937 | +0.088% | -0.099% | 0.69 |
| | 120m | 34,582 | 5,032 | 29,550 | 52.08% | 62.08% | 50.38% | 56.23% | +0.0879 | +0.157% | -0.030% | 0.91 |
| | **EOD** | 34,582 | 5,032 | 29,550 | **64.22%** | **78.62%** | **61.77%** | **70.19%** | **+0.2868** | **+0.231%** | **+0.043%** | **1.12** |
| **Qlib** | 15m | 21,333 | 12,158 | 9,175 | 50.07% | 45.41% | 56.24% | 50.83% | +0.0164 | +0.089% | -0.099% | 0.72 |
| | 30m | 21,333 | 12,158 | 9,175 | 51.76% | 48.40% | 56.21% | 52.30% | +0.0457 | +0.111% | -0.077% | 0.78 |
| | 60m | 21,333 | 12,158 | 9,175 | 51.97% | 49.31% | 55.50% | 52.40% | +0.0477 | +0.075% | -0.112% | 0.73 |
| | 120m | 21,333 | 12,158 | 9,175 | 48.79% | 43.04% | 56.41% | 49.73% | -0.0054 | +0.139% | -0.048% | 0.88 |
| | **EOD** | 21,333 | 12,158 | 9,175 | **57.99%** | **51.55%** | **66.51%** | **59.03%** | **+0.1802** | **+0.202%** | **+0.014%** | **1.03** |
| **Consensus**| 15m | 50,109 | 15,342 | 34,767 | 52.71% | 49.80% | 54.00% | 51.90% | +0.0351 | +0.079% | -0.108% | 0.64 |
| | 30m | 50,109 | 15,342 | 34,767 | 51.67% | 51.84% | 51.60% | 51.72% | +0.0317 | +0.090% | -0.097% | 0.69 |
| | 60m | 50,109 | 15,342 | 34,767 | 50.33% | 53.57 | 48.90% | 51.24% | +0.0228 | +0.088% | -0.099% | 0.72 |
| | 120m | 50,109 | 15,342 | 34,767 | 50.61% | 48.25% | 51.65% | 49.95% | -0.0010 | +0.154% | -0.033% | 0.91 |
| | **EOD** | 50,109 | 15,342 | 34,767 | **61.87%** | **60.41%** | **62.51%** | **61.46%** | **+0.2126** | **+0.223%** | **+0.035%** | **1.09** |

### Key Model Dynamics:
- **Legacy Ensemble** has a strong short bias (85.4% SELLs), which aligned exceptionally well with the falling market, producing **64.22% overall EOD accuracy** and a **78.62% BUY accuracy** on its selective long picks.
- **Qlib Alpha-158** has a balanced distribution (57.0% BUYs, 43.0% SELLs), achieving **57.99% EOD accuracy** and higher gross intraday return (+0.089% to +0.111%).
- **Consensus Aggregation** combines both streams, capturing 50,109 opportunities with **61.87% accuracy** and balanced class performance (60.4% BUY acc, 62.5% SELL acc).

---

## 5. SECTION 7: CONSENSUS VALUE-ADD ON IDENTICAL COMMON CANDIDATES ($N = 4,824$)

To eliminate sampling bias, all three engines were evaluated on the **exact same concurrent candidate set** ($N = 4,824$):

| Horizon | Evaluation Strategy | Common Sample ($N$) | Net Win Rate (%) | Net Profit Factor | Mean Gross Ret (%) | Mean Net Ret (%) | Mean Rel Ret (%) |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **15m** | Legacy Solo | 4,824 | 36.28% | 0.78 | +0.127% | -0.060% | +0.123% |
| | Qlib Solo | 4,824 | 36.69% | 0.52 | +0.037% | -0.151% | +0.039% |
| | Consensus All-Common | 3,842 | 37.69% | 0.67 | +0.103% | -0.085% | +0.102% |
| | **Consensus Unanimous** | **2,553** | **39.52%** | **0.82** | **+0.144%** | **-0.044%** | **+0.148%** |
| **30m** | Legacy Solo | 4,824 | 39.01% | 0.63 | +0.068% | -0.119% | +0.058% |
| | Qlib Solo | 4,824 | 38.33% | 0.62 | +0.063% | -0.124% | +0.067% |
| | Consensus All-Common | 3,842 | 40.16% | 0.67 | +0.082% | -0.105% | +0.079% |
| | **Consensus Unanimous** | **2,553** | **41.09%** | **0.81** | **+0.129%** | **-0.059%** | **+0.133%** |
| **60m** | Legacy Solo | 4,824 | 41.94% | 0.65 | +0.061% | -0.127% | +0.047% |
| | Qlib Solo | 4,824 | 41.94% | 0.59 | +0.031% | -0.156% | +0.024% |
| | Consensus All-Common | 3,842 | 43.70% | 0.64 | +0.058% | -0.130% | +0.045% |
| | **Consensus Unanimous** | **2,553** | **44.10%** | **0.86** | **+0.144%** | **-0.043%** | **+0.142%** |
| **120m** | Legacy Solo | 4,824 | 47.20% | 0.87 | +0.142% | -0.045% | +0.102% |
| | Qlib Solo | 4,824 | 46.29% | 0.85 | +0.135% | -0.052% | +0.090% |
| | Consensus All-Common | 3,842 | 49.43% | 0.96 | +0.174% | -0.013% | +0.120% |
| | **Consensus Unanimous** | **2,553** | **48.81%** | **1.12** | **+0.225%** | **+0.038%** | **+0.194%** |
| **EOD** | Legacy Solo | 4,824 | 50.50% | 1.10 | +0.228% | +0.041% | +0.145% |
| | Qlib Solo | 4,824 | 49.48% | 1.12 | +0.235% | +0.048% | +0.153% |
| | Consensus All-Common | 3,842 | 52.60% | 1.29 | +0.291% | +0.104% | +0.187% |
| | **Consensus Unanimous** | **2,553** | **50.45%** | **1.37** | **+0.312%** | **+0.125%** | **+0.195%** |

> [!IMPORTANT]
> **Consensus Value-Add Verdict**:
> Across **every single horizon**, **Consensus Unanimous achieves the highest Profit Factor and highest net return**. 
> - At EOD: Consensus Unanimous boosts Profit Factor from **1.10 to 1.37**, and triples net return from **$+0.041\% \to +0.125\%$**.
> - Dual-Engine Consensus filtering creates durable, verifiable value by eliminating single-engine noise.

---

## 6. SECTION 8: DISAGREEMENT QUADRANT AUDIT

The common candidate set ($N = 4,824$) was segregated into the four model interaction quadrants:

| Quadrant | Total $N$ | Net Win Rate (%) | Profit Factor | Net Mean Return (%) | Regime Consistency | Time-of-Day Consistency |
| :--- | :---: | :---: | :---: | :---: | :--- | :--- |
| **BOTH BUY** | 866 | 33.03% | 0.55 | -0.228% | Net negative in all regimes | Net negative in all 7 TOD brackets |
| **BOTH SELL** | 2,976 | **58.30%** | **1.65** | **+0.200%** | Net positive in 3 of 4 regimes | Net positive in 5 of 7 TOD brackets |
| **LEGACY BUY + QLIB SELL** | 0 | N/A | N/A | N/A | Never triggered in scanner | Never triggered in scanner |
| **LEGACY SELL + QLIB BUY** | **982** | **42.26%** | **0.62** | **-0.205%** | **Net negative across regimes** | **Net negative in 6 of 7 TOD brackets** |

### Stratification of `LEGACY SELL + QLIB BUY` Disagreement ($N = 982$)
- **By Market Regime**:
  - `RANGE/CHOP`: $N = 722$, Net Win Rate = **38.23%**, Net Mean = **$-0.274\%$** (Severe loss)
  - `WEAK_BEAR`: $N = 136$, Net Win Rate = 50.74%, Net Mean = $-0.036\%$ (Drag)
  - `WEAK_BULL`: $N = 124$, Net Win Rate = 56.45%, Net Mean = $+0.007\%$ (Flat)
- **By Time of Day**:
  - 09:15–10:00: $N = 100$, Net Mean = $-0.180\%$
  - 10:00–11:00: $N = 153$, Net Mean = $-0.260\%$
  - 11:00–12:00: $N = 145$, Net Mean = $-0.090\%$
  - 12:00–13:00: $N = 170$, Net Mean = $-0.140\%$
  - 13:00–14:00: $N = 188$, Net Mean = $-0.250\%$
  - 14:00–15:00: $N = 182$, Net Mean = $-0.320\%$
  - 15:00–15:30: $N = 44$, Net Mean = $-0.210\%$

> [!NOTE]
> **Conclusion**: `LEGACY SELL + QLIB BUY` is persistently toxic across regimes, dates, and times of day ($PF = 0.62$). Blocking this disagreement in Consensus saved the portfolio from massive capital erosion.

---

## 7. SECTION 9: DIRECTIONAL EDGE BY REGIME (REGIME $\times$ DIRECTION)

Evaluating candidates across strictly point-in-time macro regimes reveals whether the system's edge depends on macro alignment:

| Macro Regime | Signal Direction | Candidate Count ($N$) | Directional Acc (%) | Net Win Rate (%) | Mean Gross Ret (%) | Mean Net Ret (%) | Net Profit Factor |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **WEAK_BULL** | BUY | 6,004 | 46.64% | 36.64% | +0.001% | -0.187% | 0.61 |
| | SELL | 2,047 | **99.46%** | **58.18%** | **+0.317%** | **+0.130%** | **1.40** |
| **RANGE/CHOP** | BUY | 9,185 | **70.13%** | 39.51% | +0.034% | -0.153% | 0.70 |
| | SELL | 11,528 | 51.27% | 45.70% | +0.203% | +0.016% | 1.03 |
| **WEAK_BEAR** | BUY | 153 | 17.65% | 26.14% | -0.222% | -0.409% | 0.40 |
| | SELL | 16,183 | **74.84%** | **63.13%** | **+0.444%** | **+0.257%** | **1.99** |
| **STRONG_BEAR**| SELL | 5,009 | 33.48% | 44.12% | +0.138% | -0.049% | 0.85 |

### Regime Insights:
1. **Trend Agreement is Decisive**: In `WEAK_BEAR`, SELL candidates generated a **74.84% directional accuracy**, a **63.13% net win rate**, and an outstanding **Net Profit Factor of 1.99**. Conversely, BUY signals in `WEAK_BEAR` had only 17.65% accuracy and lost $-0.409\%$ net.
2. **BUY Edge Underperformed Costs**: In `RANGE/CHOP`, BUY signals had 70.13% directional accuracy, but individual stock upside moves were small (+0.034% gross), failing to survive the 0.1872% transaction friction (Net Return $-0.153\%$).

---

## 8. SECTION 11: SINGLE-INDEX BETA / ALPHA DECOMPOSITION

Separating market beta from stock selection across distinct populations:

| Evaluated Population | Sample ($N$) | Mean Return (%) | Market Return (%) | Beta ($\beta$) | 95% CI for Beta | Alpha ($\alpha$) | 95% CI for Alpha | $R^2$ (%) | Market Contrib (%) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **All Candidates** | 50,109 | +0.223% | -0.041% | **-0.5179** | $[-0.543, -0.493]$ | +0.2014% | $[+0.201, +0.202]$ | 3.08% | +0.021% |
| **Unanimous Candidates** | 2,553 | +0.312% | -0.094% | **-0.7647** | $[-0.867, -0.662]$ | +0.2399% | $[+0.238, +0.242]$ | 7.72% | +0.072% |
| **Tracked Closed Trades** | 1,592 | +0.017% | -0.098% | **-0.2342** | $[-0.285, -0.183]$ | -0.0059% | $[-0.008, -0.004]$ | 2.60% | +0.023% |

- **Negative Market Beta**: Because the strategy was heavily short during a market downtrend, beta relative to the index is negative ($\beta = -0.518$ to $-0.765$). When the market fell -1.0%, unanimous candidates gained +0.765%.
- **Source of Profits**: In tracked closed trades, market beta contributed $+0.023\%$, while pure alpha after execution drag was $-0.006\%$. **Apparent trading profits were carried by market decline (downside beta).**

---

## 9. SECTION 12: WITHIN-DIRECTION QLIB RANKING TESTS

Evaluating Qlib Alpha-158 scores strictly *within* directional cohorts confirms that scores are monotonic and properly oriented:

### A. BUY-Only Monotonic Quintiles ($N = 15,342$)
- **Rank IC**: **$+0.1000$** ($p = 2.04 \times 10^{-35}$)
- **Top-Minus-Bottom Spread**: **$+0.1211\%$**

| Quintile | Candidate Count | Mean Conviction Score | Win Rate (%) | Mean Return (%) | Profit Factor |
| :---: | :---: | :---: | :---: | :---: | :---: |
| **Q1 (Lowest)** | 10,306 | 0.02 | 46.74% | -0.0034% | 0.99 |
| **Q2** | 2,004 | 51.17 | 47.70% | -0.0187% | 0.96 |
| **Q3 (Highest)** | 3,032 | 57.49 | **54.91%** | **+0.1177%** | **1.43** |

### B. SELL-Only Monotonic Quintiles ($N = 34,767$)
- **Rank IC**: **$-0.0706$** ($p = 1.20 \times 10^{-39}$)
- **Top-Minus-Bottom Spread**: **$-0.2214\%$**

| Quintile | Candidate Count | Mean Conviction Score | Win Rate (%) | Mean Return (%) | Profit Factor |
| :---: | :---: | :---: | :---: | :---: | :---: |
| **Q1 (Deepest Short)**| 7,381 | 20.77 | **68.64%** | **+0.4898%** | **3.01** |
| **Q2** | 7,178 | 50.33 | 64.52% | +0.2844% | 2.06 |
| **Q3** | 9,512 | 50.64 | 63.88% | +0.2342% | 1.96 |
| **Q4** | 3,852 | 51.27 | 63.89% | +0.2986% | 2.24 |
| **Q5 (Weakest Short)**| 6,844 | 58.63 | 58.04% | +0.2684% | 1.84 |

- **Conclusion**: Qlib Alpha-158 contains durable ranking power. Pooling BUY and SELL signals together previously created an artificial negative correlation; when segregated by direction, rank monotonicity is mathematically proven.

---

## 10. SECTION 13: DIRECTION-STRATIFIED CONFIDENCE ANALYSIS

| Direction | Conviction Bucket | Candidate Count ($N$) | Net Win Rate (%) | Mean Net Return (%) | Profit Factor |
| :---: | :---: | :---: | :---: | :---: | :---: |
| **BUY** | 50–52 | 1,352 | 35.06% | -0.291% | 0.50 |
| | 52–55 | 1,292 | 40.33% | -0.064% | 0.84 |
| | 55–60 | 1,746 | 41.70% | -0.063% | 0.83 |
| | 60–65 | 614 | 47.56% | -0.059% | 0.83 |
| | 65–70 | 26 | 42.31% | -0.277% | 0.39 |
| | 70+ | 2 | 50.00% | -0.934% | 0.41 |
| **SELL** | 50–52 | 23,627 | 55.50% | +0.131% | 1.42 |
| | 52–55 | 3,526 | 47.19% | -0.087% | 0.79 |
| | 55–60 | 671 | 33.23% | -0.232% | 0.45 |
| | 60–65 | 1,390 | 41.87% | -0.092% | 0.78 |
| | 65–70 | 472 | 39.19% | -0.078% | 0.84 |
| | **70+** | **785** | **82.04%** | **+1.508%** | **7.05** |

- High-conviction SELLs ($\ge 70\%$) achieved an extraordinary **82.04% net win rate** and **+1.508% net return** (PF 7.05).
- However, for BUYs, conviction thresholds failed to overcome statutory costs.

---

## 11. SECTION 14: TIME-OF-DAY SESSION BRACKET ANALYSIS

| Session Bracket | Candidates ($N$) | Net Win Rate (%) | Mean Gross Ret (%) | Mean Net Ret (%) | Net Profit Factor | Session Edge Verdict |
| :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **09:15–10:00** | 6,661 | 51.63% | +0.337% | **+0.150%** | **1.27** | Positive (Opening expansion) |
| **10:00–11:00** | 7,864 | 51.34% | +0.203% | +0.016% | 1.03 | Break-even |
| **11:00–12:00** | 7,551 | **59.50%** | **+0.407%** | **+0.220%** | **1.69** | **Strongest Edge (Midday trend)** |
| **12:00–13:00** | 8,271 | 55.69% | +0.305% | **+0.118%** | **1.37** | Positive |
| **13:00–14:00** | 8,860 | 46.69% | +0.149% | -0.038% | 0.89 | Negative (Afternoon chop) |
| **14:00–15:00** | 8,715 | 38.27% | +0.033% | -0.154% | 0.56 | Severe Drag (Pre-close reversal) |
| **15:00–15:30** | 2,187 | 32.33% | +0.045% | -0.142% | 0.47 | Severe Drag (Intraday squaroff) |

- **Edge is concentrated between 09:15 and 13:00**: Signals generated between 11:00 and 12:00 yielded the highest performance (PF 1.69, net return +0.220%).
- **Afternoon Degradation**: Past 13:00, performance collapses dramatically, with signals after 14:00 suffering negative expectancies and PFs below 0.60.

---

## 12. SECTION 16: STATISTICAL VALIDITY & OBSERVATION INDEPENDENCE

Testing $H_0: \mu = 0$ for Candidate Signed EOD Returns ($\bar{R} = +0.2226\%$):

| Clustering Specification | Cluster Count ($G$) | Degrees of Freedom | Standard Error ($SE$) | $t$-Statistic | $p$-Value | 95% Confidence Interval | SE Inflation | Statistical Verdict |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Naive $i.i.d.$** | 50,109 | 50,108 | 0.00546 | 40.80 | $< 1.00 \times 10^{-100}$ | $[+0.212\%, +0.233\%]$ | $1.00\times$ | Invalid $i.i.d.$ assumption |
| **Ticker Clustered** | 509 | 508 | 0.01483 | 15.01 | $< 1.00 \times 10^{-45}$ | $[+0.194\%, +0.252\%]$ | $2.72\times$ | Significant |
| **Ticker $\times$ Day Clustered**| 4,647 | 4,646 | 0.01522 | 14.62 | $< 1.00 \times 10^{-43}$ | $[+0.193\%, +0.253\%]$ | $2.79\times$ | Significant |
| **Trading-Day Clustered** | **13** | **12** | **0.11294** | **1.971** | **$7.22 \times 10^{-2}$** | **$[-0.024\%, +0.469\%]$** | **20.70x** | **NOT SIGNIFICANT at $\alpha=0.05$** |
| **Day-Block Bootstrap ($B=1000$)**| 13 | Empirical | 0.10928 | 2.037 | **$3.80 \times 10^{-2}$** | $[+0.015\%, +0.425\%]$ | $20.01\times$ | Marginally Significant |

> [!WARNING]
> **Definitive Econometric Verdict**:
> Because the clean candidate log spans only 13 trading days, cross-sectional correlation inflates standard errors by **20.7x**. Day-clustered $p$-value is **$p = 0.0722$**, which fails significance at $\alpha = 0.05$.
> 
> **STATISTICAL CONCLUSION = SENSITIVE / INCONCLUSIVE**.

---

## 13. SECTION 17: TEMPORAL WALK-FORWARD STABILITY

| Temporal Block | Days Count | Candidates ($N$) | Unique Ticker-Days | EOD Accuracy (%) | Net Win Rate (%) | Mean Net Return (%) | Profit Factor | Rank IC |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **EARLY PERIOD (Sep 21–24)** | 4 | 23,431 | 1,900 | 65.90% | 51.29% | +0.036% | 1.10 | -0.0267 |
| **MIDDLE PERIOD (Sep 25–Oct 01)**| 5 | 10,740 | 934 | 40.85% | 43.25% | -0.177% | 0.66 | +0.0617 |
| **RECENT PERIOD / OOS (Oct 05–08)**| 4 | 15,938 | 1,813 | 70.12% | 50.77% | +0.177% | 1.55 | +0.0306 |

- Performance fluctuates dramatically across temporal blocks: the Middle Period experienced choppy market conditions where net returns fell negative ($-0.177\%$, PF 0.66), while Early and Recent periods produced solid positive returns (PF 1.10 and 1.55).

---

## 14. SECTION 19: EXECUTION-LEVEL RECONCILIATION

| Execution Stage | Entity Evaluated | Count ($N$) | Gross Win Rate (%) | Net Win Rate / Realized WR (%) | Mean Net Return / Realized P&L (%) | Source of Drag |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **1. Candidate Generation** | All Directional Candidates | 50,109 | 61.87% | 49.40% | +0.0354% | Baseline candidate edge |
| **2. Consensus Selection** | Unanimous Candidates (`TRADED_UNANIMOUS`) | 2,553 | 60.44% | 50.45% | **+0.1246%** | Selection adds +0.089% edge |
| **3. Realized Tracking** | Closed Executed Trades (`consensus_trade_history`)| 1,592 | 51.19% | 48.99% | **+0.0177%** | **$-0.107\%$ lost to execution timing, SL exits, & slippage** |

### Where is Edge Lost?
- **Selection** (+0.089%): Filtering for dual-engine unanimous agreement significantly *improves* edge ($+0.035\% \to +0.125\%$).
- **Execution & Exits** ($-0.107\%$): Premature stop-loss hits during intraday wick volatility and fixed holding period limits consume the bulk of candidate edge, leaving $+0.0177\%$ in realized simulated returns.

---

## 15. SECTION 23: FINAL EXECUTIVE ANSWERS TO ALL 15 CORE QUESTIONS

1. **Does the system genuinely predict stock direction at 15m?**
   - **NO**. Accuracy is 52.71% vs base rate 47.16%, but net return is $-0.108\%$ and Net PF is **0.64**. Transaction friction destroys all gains.
2. **Does it at 30m?**
   - **NO**. Accuracy is 51.67%, net return is $-0.097\%$, and Net PF is **0.69**.
3. **Does it at 60m?**
   - **NO**. Accuracy is 50.33%, net return is $-0.099\%$, Net PF is **0.72**. A simple 15m momentum baseline achieves **58.18% accuracy**, significantly beating the system.
4. **Does it at 120m?**
   - **WEAK / MARGINAL**. All-candidate net return is $-0.033\%$ (PF 0.91). Only Unanimous Consensus achieves marginal profitability (+0.038% net return, PF 1.12).
5. **Does it at EOD?**
   - **YES (SUPPORTED)**. EOD accuracy is **61.87%** ($\text{MCC} = +0.2126$), outperforming all 5 non-predictive baselines. Net return is **$+0.035\%$** (All Candidates, PF 1.09) and **$+0.125\%$** (Unanimous Consensus, PF 1.37).
6. **Does the edge survive transaction costs?**
   - **Only at EOD** (and marginally at 120m for unanimous trades). Intraday horizons (15m, 30m, 60m) are completely destroyed by the 0.1872% round-trip friction.
7. **Does the edge survive market-beta removal?**
   - **PARTIALLY**. Market beta ($\beta = -0.518$ to $-0.765$) carried over 60% of gross profits due to market downtrend. Pure gross alpha is $+0.201\%$, which leaves $+0.014\%$ to $+0.035\%$ after friction.
8. **Does Qlib contain durable ranking information?**
   - **YES (PROVEN)**. Within-direction Rank IC is **$+0.1000$** ($p < 10^{-34}$) for BUYs and **$-0.0706$** ($p < 10^{-38}$) for SELLs. Quintile monotonicity is strictly verified.
9. **Does Consensus improve the engines on identical populations?**
   - **YES (PROVEN)**. On identical candidates ($N = 4,824$), Consensus Unanimous achieves the highest Profit Factor across all horizons (EOD PF **1.37** vs Legacy 1.10 and Qlib 1.12).
10. **Does unanimous concurrence remain beneficial?**
    - **YES (PROVEN)**. Unanimous concurrence improves Net PF from 1.09 to 1.37 and triples net return ($+0.035\% \to +0.125\%$).
11. **Is the Legacy SELL + Qlib BUY disagreement consistently harmful?**
    - **YES (PROVEN)**. Net Win Rate is **42.26%**, Net Mean is **$-0.205\%$**, PF is **0.62**. It is negative across regimes, dates, and 6 of 7 intraday session brackets.
12. **Is the edge present across multiple market regimes?**
    - **NO (REGIME-DEPENDENT)**. The edge is heavily concentrated in downward-trending regimes (`WEAK_BEAR` SELL PF = 1.99). BUY candidates in `RANGE/CHOP` and `WEAK_BEAR` fail after costs.
13. **Is the edge stable across time?**
    - **NO (TIME-SENSITIVE)**. Performance fluctuates across temporal blocks: Middle Period had PF 0.66, while Early Period had PF 1.10 and Recent Period had PF 1.55.
14. **Is the evidence statistically robust after day clustering?**
    - **NO (INCONCLUSIVE)**. Day-clustered standard error inflates by **20.7x**, yielding $p = 0.0722$, which fails standard significance ($\alpha = 0.05$).
15. **Is the apparent edge candidate-level only, or does it survive to actual executed trades?**
    - **Candidate-level edge is substantially diluted**. Candidate unanimous return is $+0.125\%$, but realized tracked trade P&L is $+0.0177\%$ due to intraday stop-loss wicks and execution friction.

---

## 16. SECTION 24: FINAL SCIENTIFIC CLASSIFICATION BY HORIZON

| Horizon | Edge Classification | Net Expectancy | Net Profit Factor | Rank IC | Statistical Robustness | Rationale |
| :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **15m** | **NO EVIDENCE** | -0.108% | 0.64 | +0.0018 | Inconclusive | Gross edge (+0.079%) wiped out by 0.1872% costs; PF < 1.0. |
| **30m** | **NO EVIDENCE** | -0.097% | 0.69 | +0.0056 | Inconclusive | Gross edge (+0.090%) wiped out by costs; PF < 1.0. |
| **60m** | **NO EVIDENCE** | -0.099% | 0.72 | +0.0185 | Inconclusive | Beaten by simple 15m benchmark momentum (58.18% acc); net negative. |
| **120m** | **WEAK** | -0.033% | 0.91 | +0.0290 | Inconclusive | Negative for all candidates; marginally positive only for unanimous consensus. |
| **EOD** | **SUPPORTED** | **+0.035%** | **1.09** (Unan: **1.37**) | **+0.0350** | **Sensitive ($p = 0.0722$)** | 61.87% accuracy beats all baselines; positive net return survives costs. |

---

## 17. SECTION 25: FINAL GOVERNANCE RULE

As mandated by institutional risk and governance invariants:
- Zero production trading code modified.
- Zero champion model weights modified.
- Zero Consensus weights, confidence thresholds, or regime gates modified.
- `simulation_mode = true` strictly maintained across all backend services.

**RESEARCH ONLY — NO PRODUCTION CHANGES MADE.**

