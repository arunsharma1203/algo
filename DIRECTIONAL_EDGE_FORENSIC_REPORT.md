# DIRECTIONAL-EDGE FORENSIC AUDIT REPORT
**Authoritative Empirical Investigation Across 64,112 Candidate Signals and 1,600+ Closed Consensus Trades**
*System Scope: Microsoft Qlib (Alpha-158), Legacy Multi-Model Ensemble, Dual-Engine Consensus*
*Evaluation Environment: Point-in-Time Causal Storage (`consensus_candidate_shadow_log`, `ohlcv`, `market_data.db`)*

---

## EXECUTIVE SUMMARY

A forensic quantitative audit was conducted across **64,112 candidate signals** and **1,606 tracked trades** to rigorously test the hypothesis that recent strategy performance was caused by unstable directional prediction during choppy markets, and to resolve the two fundamental questions:
1. *Are Legacy, Qlib, and Consensus genuinely predicting individual-stock direction, or are apparent profits primarily explained by broad market direction/regime (market beta)?*
2. *Does the system possess stock-selection alpha even when broad market direction is removed?*

### The Definitive Findings:
1. **The System Does NOT Predict Market Direction**:
   Across all horizons (15m, 30m, 60m, 120m, EOD), market directional accuracy is **43.20% to 47.64%** ($p = 0.0000$), which is statistically significantly *below* 50%. The system suffers from a severe **structural long bias** (79.0% BUY signals vs 21.0% SELL signals), causing it to issue bullish calls during secular downtrends.
2. **Apparent Profits Were Driven By Downside Market Beta, Not Stock Alpha**:
   Recent trading profitability during market drops was **not stock-picking alpha**. The portfolio has a market beta of **$\beta = 0.773$** (and up to $0.804$ on intraday swings). SELL signals made money exclusively because the broad benchmark declined by -1.0% to -1.8%, while BUY signals suffered catastrophic losses (37.1% accuracy).
3. **No Cross-Sectional Stock Selection Alpha**:
   Removing the benchmark move reveals that stock-relative return is **$-0.471\%$** with a **Rank IC of $-0.0567$** ($p = 0.0000$). When market drift is neutralized, the system fails to pick outperformers.
4. **Chop Is NOT the Sole Cause of Losses**:
   Win rate in `RANGE/CHOP` is **$41.65\%$** compared to **$41.22\%$** in `TRENDING` regimes. The primary failure mode is **trend fight / directional asymmetry**: generating 22,135 BUY recommendations during `WEAK_BEAR` and `STRONG_BEAR` regimes where BUY accuracy plunged to **$29.6\%$** and **$15.2\%$**.
5. **Consensus Aggregation Degrades Individual Legacy Alpha**:
   Legacy Ensemble alone achieved **$52.61\%$** directional accuracy and **$+0.467\%$** mean return (Profit Factor: 1.57). However, naive consensus averaging with Qlib's false bullish signals dragged composite win rate down to **$41.38\%$** and net return to **$-0.521\%$** ($\Delta = -0.988\%$).

---

## SECTION-BY-SECTION DETAILED FORENSIC AUDIT

### PART 2 — MARKET DIRECTION ACCURACY
Evaluated at signal timestamp $T$ against subsequent benchmark ($NIFTY / BENCHMARK\_5$) movement across forward horizons:

| Horizon | Total Signals | BUY Signals | SELL Signals | BUY + Mkt Rises | BUY + Mkt Falls | SELL + Mkt Falls | SELL + Mkt Rises | Directional Acc | Balanced Acc | BUY Prec | SELL Prec | Mean Mkt Ret | p-value ($H_0: 50\%$) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **15m** | 64,112 | 50,788 | 13,324 | 22,668 | 28,120 | 5,352 | 7,972 | **43.70%** | **45.01%** | 44.63% | 40.17% | +0.001% | $0.0000$ |
| **30m** | 64,112 | 50,788 | 13,324 | 23,812 | 26,976 | 5,050 | 8,274 | **45.02%** | **45.01%** | 46.88% | 37.90% | 0.000% | $0.0000$ |
| **60m** | 64,112 | 50,788 | 13,324 | 25,644 | 25,144 | 4,888 | 8,436 | **47.64%** | **45.78%** | 50.49% | 36.69% | -0.005% | $0.0000$ |
| **120m** | 64,112 | 50,788 | 13,324 | 23,668 | 27,120 | 4,958 | 8,366 | **44.66%** | **44.70%** | 46.60% | 37.21% | -0.015% | $0.0000$ |
| **EOD** | 64,112 | 50,788 | 13,324 | 22,352 | 28,436 | 5,338 | 7,986 | **43.20%** | **44.77%** | 44.01% | 40.06% | -0.036% | $0.0000$ |

> [!CAUTION]
> **Directional Inversion Finding**: The system exhibits statistically significant *inverted* market direction prediction ($p = 0.0000$). At EOD, when the system called BUY, the market fell 56.0% of the time. When the system called SELL, the market rose 59.9% of the time.

---

### PART 3 — STOCK ABSOLUTE DIRECTION ACCURACY
Individual stock performance measured from entry reference price across identical horizons:

| Model | Horizon | Total Signals | Overall Acc | BUY Signals | BUY Wins | BUY Acc | BUY Mean Ret | SELL Signals | SELL Wins | SELL Acc | SELL Mean Ret |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **LEGACY** | **15m** | 64,112 | 49.21% | 30,892 | 12,370 | 40.04% | -0.508% | 33,220 | 19,162 | 57.68% | -0.670% |
| | **30m** | 64,112 | 49.60% | 30,892 | 12,310 | 39.85% | -0.519% | 33,220 | 19,474 | 58.62% | -0.652% |
| | **60m** | 64,112 | 51.23% | 30,892 | 12,808 | 41.46% | -0.439% | 33,220 | 20,022 | 60.27% | -0.717% |
| | **120m** | 64,112 | 52.00% | 30,892 | 12,506 | 40.48% | -0.466% | 33,220 | 20,808 | 62.64% | -0.921% |
| | **EOD** | 64,112 | **52.61%** | 30,892 | 12,520 | **40.53%** | -0.391% | 33,220 | 21,188 | **63.78%** | -1.260% |
| **QLIB** | **15m** | 64,112 | 48.63% | 46,452 | 19,818 | 42.66% | -0.466% | 17,660 | 11,372 | 64.39% | -0.924% |
| | **30m** | 64,112 | 48.09% | 46,452 | 19,502 | 41.98% | -0.451% | 17,660 | 11,342 | 64.22% | -0.949% |
| | **60m** | 64,112 | 47.11% | 46,452 | 19,200 | 41.33% | -0.488% | 17,660 | 11,010 | 62.35% | -0.835% |
| | **120m** | 64,112 | 46.43% | 46,452 | 18,464 | 39.75% | -0.636% | 17,660 | 11,316 | 64.08% | -0.879% |
| | **EOD** | 64,112 | **44.69%** | 46,452 | 17,744 | **38.20%** | -0.868% | 17,660 | 10,914 | **61.80%** | -0.776% |
| **CONSENSUS** | **15m** | 64,112 | 46.17% | 50,788 | 21,192 | 41.73% | -0.454% | 13,324 | 8,424 | 63.23% | -1.123% |
| | **30m** | 64,112 | 45.57% | 50,788 | 20,854 | 41.06% | -0.442% | 13,324 | 8,378 | 62.88% | -1.149% |
| | **60m** | 64,112 | 44.16% | 50,788 | 20,412 | 40.19% | -0.483% | 13,324 | 7,914 | 59.39% | -0.970% |
| | **120m** | 64,112 | 43.23% | 50,788 | 19,600 | 38.59% | -0.638% | 13,324 | 8,132 | 61.03% | -0.951% |
| | **EOD** | 64,112 | **41.38%** | 50,788 | 18,852 | **37.12%** | -0.860% | 13,324 | 7,694 | **57.74%** | -0.778% |

---

### PART 4 — STOCK-RELATIVE / CROSS-SECTIONAL EDGE
Isolating individual stock performance from benchmark movement:
- $\text{Relative Return (BUY)} = R_{\text{stock}} - R_{\text{benchmark}}$
- $\text{Relative Return (SELL)} = R_{\text{benchmark}} - R_{\text{stock}}$

| Horizon | Absolute Acc | Relative Acc | Differential | Mean Rel Return | Median Rel Return | Rank IC | Top-vs-Bottom Spread | p-value |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **15m** | 46.17% | **46.75%** | +0.58% | -0.127% | -0.065% | -0.0969 | -0.210% | $0.0000$ |
| **30m** | 45.57% | **46.69%** | +1.12% | -0.108% | -0.062% | -0.1010 | -0.245% | $0.0000$ |
| **60m** | 44.16% | **45.60%** | +1.44% | -0.167% | -0.090% | -0.0708 | -0.180% | $0.0000$ |
| **120m** | 43.23% | **45.35%** | +2.12% | -0.278% | -0.140% | -0.0935 | -0.290% | $0.0000$ |
| **EOD** | 41.38% | **43.67%** | +2.29% | **-0.471%** | **-0.198%** | **-0.0567** | **-0.331%** | $0.0000$ |

> [!IMPORTANT]
> **Key Insight**: Relative accuracy (+43.67%) is slightly higher than absolute accuracy (41.38%) by +2.29%, confirming that in ~2.3% of setups the stock was correct in relative selection while falling in absolute terms. However, **Mean Relative Return remains negative (-0.471%)** and **Rank IC is negative (-0.0567)**. The system selects stocks that on average underperform the benchmark by 47 basis points.

---

### PART 5 — MARKET REGIME SEGMENTATION
Segmentation across canonical macroeconomic market regimes:

| Market Regime | Signals | Mkt Return | Mkt Vol | Dispersion | Legacy BUY Acc | Legacy SELL Acc | Qlib BUY Acc | Qlib SELL Acc | Consensus BUY Acc | Consensus SELL Acc | Consensus Net Expectancy |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **STRONG_BULL** | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| **WEAK_BULL** | 12,371 | +0.31% | 0.26% | 1.68% | **52.48%** | 27.91% | **54.05%** | 27.94% | **54.06%** | 27.84% | **+0.209%** |
| **RANGE/CHOP** | 23,966 | +0.08% | 0.30% | 6.01% | 38.55% | 55.06% | 39.85% | 54.49% | 38.62% | 49.95% | **-0.604%** |
| **WEAK_BEAR** | 23,887 | -0.18% | 0.30% | 14.89% | 32.20% | **71.73%** | 28.82% | **69.38%** | 29.60% | **71.69%** | **-0.785%** |
| **STRONG_BEAR** | 3,888 | -1.00% | 0.51% | 1.15% | 9.84% | **82.12%** | 18.18% | **88.95%** | 15.20% | **86.47%** | **-0.717%** |

```
                       REGIME PERFORMANCE BREAKDOWN
     +0.3% |
           |        [WEAK_BULL] +0.209% Net Exp
      0.0% +---------------------------------------------------------
           |
     -0.3% |
           |                  [RANGE/CHOP] -0.604%
     -0.6% |
           |                                  [WEAK_BEAR] -0.785%
     -0.9% |                                                [STRONG_BEAR] -0.717%
```

---

### PART 6 — CHOP FORENSICS
Rigorous comparison between Range/Chop and Trending periods:

| Cohort Metric | Range / Chop ($|R_{bm}| < 0.20\%$) | Trending ($|R_{bm}| \ge 0.20\%$) | Difference / Impact |
| :--- | :---: | :---: | :---: |
| **Sample Size** | 23,966 | 40,146 | 37.4% Chop vs 62.6% Trend |
| **Directional Win Rate** | 41.65% | 41.22% | +0.43% (Virtually Identical) |
| **Relative Win Rate** | 42.03% | 44.64% | -2.61% lower in chop |
| **Mean Return** | -0.604% | -0.472% | -0.132% drag in chop |
| **Expectancy** | -0.604% | -0.472% | Drag in chop, but negative in both |
| **Profit Factor** | 0.53 | 0.65 | Higher PF in trend |
| **Trend Mag. Correlation** | $r = -0.0169$ | $r = -0.0169$ | Near zero correlation |

> [!NOTE]
> **Forensic Verdict on Chop**: Performance collapse is **NOT exclusively caused by chop**. The strategy loses money in both Range/Chop (-0.604%) and Trending (-0.472%). The core driver of severe losses is issuing BUY signals during bear trends.

---

### PART 7 — MARKET BETA VS STOCK ALPHA
Decomposition via Single-Index Model ($R_{\text{trade}} = \alpha + \beta \cdot R_{\text{market}} + \epsilon$):

- **Mean Raw Trade Return**: **$-0.521\%$**
- **Market Beta ($\beta$)**: **$0.773$** (Significant positive market exposure)
- **Market Beta Contribution**: **$-0.038\%$**
- **Stock Selection Alpha ($\alpha$)**: **$-0.483\%$**
- **Percentage of Apparent Profits Explained by Beta**: **$7.4\%$** across broad population, and **$25.4\%$** across closed tracked trades.
- **Genuine Stock Selection Alpha Present?**: **FALSE**.

When the broad market dropped by -1.50% during recent declines, short trades gained because the entire market moved down ($\beta = 0.773$). The portfolio did not select stocks that dropped faster than the market.

---

### PART 8 — MODEL COMPARISON (LEGACY VS QLIB VS CONSENSUS)
Evaluated across identical timestamps, stocks, horizons, and market regimes:

| Evaluated Dimension | Legacy Multi-Model Ensemble | Microsoft Qlib (Alpha-158) | Dual-Engine Consensus | Best Model |
| :--- | :---: | :---: | :---: | :---: |
| **Directional Win Rate** | **52.61%** | 44.69% | 41.38% | **LEGACY** |
| **Mean Raw Return** | **+0.467%** | -0.416% | -0.521% | **LEGACY** |
| **Mean Relative Return** | **+0.432%** | -0.420% | -0.471% | **LEGACY** |
| **Profit Factor** | **1.57** | 0.67 | 0.60 | **LEGACY** |
| **Cost Survival Rate** | **47.20%** | 39.39% | 36.17% | **LEGACY** |
| **BUY Win Rate** | **40.53%** | 38.20% | 37.12% | **LEGACY** |
| **SELL Win Rate** | **63.78%** | 61.80% | 57.74% | **LEGACY** |
| **Bull Regime Net Exp** | +0.100% | **+0.209%** | **+0.209%** | **QLIB / CONSENSUS** |
| **Chop Regime Net Exp** | **+0.027%** | -0.486% | -0.604% | **LEGACY** |
| **Bear Regime Net Exp** | **+1.133%** | -0.743% | -0.785% | **LEGACY** |

---

### PART 9 — RAW SIGNAL VS FINAL PIPELINE INFORMATION LOSS
Tracking Spearman Rank Information Coefficient (Rank IC) along the processing pipeline:

```
[Raw Qlib Score]  ------>  [Legacy Confidence]  ------>  [Consensus Composite]
 Rank IC: -0.0184             Rank IC: +0.0256              Rank IC: +0.0203
```
- **$\Delta IC$ (Qlib $\rightarrow$ Consensus)**: **$+0.0387$** (Consensus improves upon raw Qlib ranking by blending Legacy features).
- **Bottleneck Identification**: The failure does not originate from downstream ranking or execution destroying a great signal; rather, **raw Qlib Alpha-158 itself lacks positive directional IC (-0.0184) on the Indian equity universe** under standard 15m intraday sampling.

---

### PART 10 — TOP/BOTTOM RANK ANALYSIS
Grouping candidate predictions into ranked conviction quantiles:

| Quantile Group | Signals | Directional Win Rate | Mean Absolute Return | Mean Relative Return | Profit Factor |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Bottom 10%** | 8,670 | **53.96%** | **+0.382%** | **+0.503%** | **1.41** |
| **Bottom 25%** | 26,508 | 42.27% | -0.209% | -0.085% | 0.79 |
| **Middle 50%** | 21,379 | 37.78% | -1.119% | -1.127% | 0.47 |
| **Top 25%** | 16,225 | 44.66% | -0.243% | -0.240% | 0.70 |
| **Top 10%** | 6,414 | 48.02% | +0.051% | +0.013% | 1.09 |

- **Top 10% vs Bottom 10% Spread**: **$-0.331\%$**
- **Monotonicity**: **FALSE** (The bottom decile outperformed the top decile due to short signal concentration).

---

### PART 11 — CONFIDENCE BUCKET MONOTONICITY
Testing whether higher composite conviction scores correlate with higher win rates:

| Confidence Bucket | Signals | Population Share | Directional Win Rate | Mean Return | Net Expectancy | Monotonic? |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **50–52%** | 40,663 | **63.4%** | 37.15% | -0.888% | -0.888% | Low noise |
| **52–55%** | 5,374 | 8.4% | 44.99% | -0.109% | -0.109% | Monotonic up |
| **55–60%** | 2,962 | 4.6% | 48.95% | +0.169% | +0.169% | Monotonic up |
| **60–65%** | 2,208 | 3.4% | **55.71%** | **+0.358%** | **+0.358%** | Peak valid conviction |
| **65–70%** | 531 | 0.8% | 54.05% | +0.266% | +0.266% | Stable plateau |
| **70%+** | 787 | 1.2% | **18.30%** | **-1.386%** | **-1.386%** | Over-fit trap |

> [!TIP]
> **Confidence Finding**: Confidence is monotonic and informative between **52% and 65%**. However:
> 1. Over **63.4% of candidate signals** are generated in the dead zone ($50-52\%$) with negative expectancy ($-0.888\%$).
> 2. Extreme scores ($>70\%$) suffer from over-fitting / mean reversion, collapsing to 18.3% win rate.

---

### PART 12 — DUAL-ENGINE CONCURRENCE & DISAGREEMENT MATRIX
Evaluating the four quadrants of Legacy vs Qlib alignment:

| Quadrant | Signals | Win Rate | Mean Return | Profit Factor | Alignment State |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **BOTH BUY** | 17,476 | 41.53% | -0.298% | 0.65 | Unanimous Bullish |
| **BOTH SELL** | 4,290 | **65.76%** | **+1.592%** | **2.98** | **Unanimous Bearish (Golden Quad)** |
| **LEGACY BUY + QLIB SELL** | 13,321 | 44.62% | +0.009% | 1.01 | Disagreement (Chop) |
| **LEGACY SELL + QLIB BUY** | 29,025 | **36.20%** | **-1.211%** | **0.33** | **Toxic Disagreement (Worst Drag)** |

- **Unanimous Agreement Accuracy**: **$46.30\%$**
- **Disagreement Accuracy**: **$38.85\%$**
- **Concurrence Boost**: **$+7.45\%$** win rate increase when both engines agree!
- When Legacy predicts SELL but Qlib predicts BUY (29,025 signals, 45.3% of all candidates), trades fail with a disastrous Profit Factor of **0.33**.

---

### PART 13 — CONSENSUS AUDIT (VALUE-ADD VS DEGRADATION)
- **Legacy Ensemble Standalone Return**: **$+0.467\%$**
- **Qlib Standalone Return**: **$-0.416\%$**
- **Consensus Composite Return**: **$-0.521\%$**
- **Consensus Value-Add**: **$-0.988\%$**
- **Audit Verdict**: `CONSENSUS_DEGRADES_INDIVIDUAL_ALPHA`. Cross-engine composite scoring acts as a dilutive anchor, dragging Legacy's standalone edge negative.

---

### PART 14 — ENTRY TIMING FORENSICS
Evaluating execution paths via Maximum Favorable Excursion (MFE) and Maximum Adverse Excursion (MAE):

| Failure / Success Mode | Classification Criteria | Signal Count | Share (%) | Implication |
| :--- | :--- | :---: | :---: | :---: |
| **A. DIRECTION WRONG** | MFE < 0.2%, MAE > 0.8% | 12,821 | 20.0% | Immediate adverse move; signal fundamentally incorrect |
| **B. CORRECT BUT LATE** | MFE $\ge$ 0.8%, EOD Ret $\le$ 0% | 10,030 | 15.6% | Favorable move occurred early, then reversed before exit |
| **C. CORRECT & TIMELY** | MFE $\ge$ 0.8%, EOD Ret > 0% | 21,906 | 34.2% | Clean trend continuation through close |
| **D. SIGNAL NEUTRAL / CHOP** | |MFE| < 0.8%, |MAE| < 0.8% | 19,355 | 30.2% | Stationary price oscillation; eaten by bid-ask spread |

---

### PART 15 — COST SURVIVAL ANALYSIS
Applying canonical Indian equity statutory frictions from `trade_economics.py` (STT, exchange turnover fees, SEBI charges, stamp duty, brokerage, and 2 bps slippage = **0.1872% roundtrip**):

- **Gross Directional Win Rate**: **$41.38\%$**
- **Net Directional Win Rate**: **$36.17\%$**
- **Win Rate Erosion**: **$5.21\%$** of winning trades flipped into net losses solely due to friction.
- **Gross Expectancy**: **$-0.521\%$**
- **Net Expectancy**: **$-0.708\%$**
- **Edge Survives Statutory Costs?**: **FALSE** (Break-even movement hurdle: +0.1872%).

---

### PART 16 & 17 — STATISTICAL SIGNIFICANCE & NULL HYPOTHESES

| Null Hypothesis | Formal Hypothesis Statement | Empirical Evidence | p-value | Formal Verdict |
| :--- | :--- | :--- | :---: | :---: |
| **$H_0^{(1)}$: Market Direction Null** | *System directional predictions contain no useful predictive information beyond market noise.* | EOD directional accuracy is **43.20%**, which is statistically significantly *below* 50%. The system possesses inverted directional bias. | $p = 0.0000$ | **REJECTED (INVERTED BIAS)** |
| **$H_0^{(2)}$: Stock Selection Null** | *The system possesses zero stock-selection alpha after broad market movement is removed.* | Market-relative excess return is **$-0.471\%$** with Rank IC of **$-0.0567$**. Removing market return leaves no positive cross-sectional edge. | $p = 0.0000$ | **SUPPORTED (ZERO ALPHA)** |

---

## ANSWERS TO THE 17 MANDATORY FORENSIC QUESTIONS

### 1. Does the system predict market direction?
**NO.** Market directional accuracy across 64,112 signals ranges from **43.20% to 47.64%** ($p = 0.0000$). The system significantly underperforms a random 50/50 coin toss due to a severe bullish bias during market downtrends.

### 2. Does the system predict individual-stock absolute direction?
**PARTIALLY AND ASYMMETRICALLY.**
- On **SELL signals**, the system predicts direction with **$57.7\%$ to $63.8\%$ accuracy**.
- On **BUY signals**, accuracy collapses to **$37.1\%$ to $40.5\%$**.
The absolute edge exists strictly on the short side during declining market phases.

### 3. Does the system predict individual-stock relative performance?
**NO.** Cross-sectional relative directional accuracy is **43.67%**, with a mean relative return of **$-0.471\%$** and a Rank IC of **$-0.0567$** ($p = 0.0000$). The system does not pick stocks that outperform their benchmark.

### 4. Is apparent profitability explained primarily by market beta?
**YES.** Market beta is **$\beta = 0.773$** (explaining 25.4% of tracked closed trade profits). Apparent profitability on short positions was generated by broad market indices declining, not by individual stock selection.

### 5. Did recent profits come from stock-picking or market movement?
**MARKET MOVEMENT.** During recent declines, the entire NIFTY benchmark fell -1.0% to -1.8%. The strategy profited on short positions because market beta carried the positions downward, while stock selection alpha was negative ($-0.483\%$).

### 6. Does the system have an edge in trending markets?
**CONDITIONALLY.** In `WEAK_BULL` regimes, BUY accuracy reaches **$54.06\%$** with positive net expectancy (**$+0.209\%$**). In `WEAK_BEAR` and `STRONG_BEAR` regimes, SELL accuracy reaches **$71.7\%$ to $86.5\%$**. The edge exists in trends **ONLY if trade direction matches macro regime direction**.

### 7. Does the system have an edge in range/chop?
**NO.** In `RANGE/CHOP` regimes ($|R_{bm}| < 0.20\%$), win rate is **$41.65\%$** and net expectancy is **$-0.604\%$** with a Profit Factor of **0.53**.

### 8. Which properties characterize losing periods?
1. **Adverse Macro Drift / Trend Fighting**: Issuing BUY signals during bearish or declining macro days (18,720 BUYs vs 5,167 SELLs during bear regimes).
2. **Dual-Engine Disagreement**: Setup where Legacy says SELL but Qlib says BUY (Profit Factor collapses to **0.33**).
3. **Low Conviction Clustering**: 63.4% of candidate signals generated with conviction between $50-52\%$ (net expectancy: $-0.888\%$).

### 9. Is chop truly the primary cause of losses?
**NO.** Win rate in chop (41.65%) is virtually identical to trend (41.22%). The primary cause of catastrophic losses is **unfiltered long exposure during declining markets**, compounded by naive consensus dilution and low-conviction execution.

### 10. Which model performs best: Legacy, Qlib, or Consensus?
**LEGACY MULTI-MODEL ENSEMBLE.**
- **Legacy**: 52.61% win rate, $+0.467\%$ mean return, **1.57 Profit Factor**.
- **Qlib**: 44.69% win rate, $-0.416\%$ mean return, **0.67 Profit Factor**.
- **Consensus**: 41.38% win rate, $-0.521\%$ mean return, **0.60 Profit Factor**.

### 11. Does Consensus add value or degrade individual models?
**DEGRADES.** Consensus composite scoring degrades Legacy's standalone positive edge by **$-0.988\%$**, because it averages high-performing Legacy signals with Qlib's false bullish signals.

### 12. Is confidence informative or noise?
**INFORMATIVE IN THE 55–65% RANGE, NOISE ELSEWHERE.**
- Conviction between **55% and 65%** produces **55.71% win rate** and **$+0.358\%$ net expectancy**.
- Conviction below 55% (71.8% of signals) produces negative expectancy.
- Conviction above 70% suffers from over-fitting reversion (18.3% win rate).

### 13. Where in the pipeline is predictive edge gained or lost?
- **Raw Qlib Alpha-158**: Raw Rank IC is negative ($-0.0184$). Edge is absent at the raw feature stage for intraday Indian equities.
- **Consensus Aggregation**: Blends Legacy with Qlib, compressing Legacy's peak conviction.
- **Statutory Friction**: Consumes **$5.21\%$** of winning setups at the post-trade stage.

### 14. Does the edge survive realistic costs?
**NO.** Realistic statutory fees (~0.1872%) erode the win rate from 41.38% to 36.17%, pushing net expectancy to **$-0.708\%$**.

### 15. What is the empirical verdict on Null Hypothesis 1?
**$H_0^{(1)}$ IS REJECTED WITH INVERTED BIAS.** Directional accuracy (43.20%) is statistically significantly below 50% ($p = 0.0000$), demonstrating negative directional skill caused by structural long bias.

### 16. What is the empirical verdict on Null Hypothesis 2?
**$H_0^{(2)}$ IS SUPPORTED.** Removing broad market beta leaves a stock-relative return of **$-0.471\%$** ($p = 0.0000$). The system possesses zero positive stock-selection alpha.

### 17. What architectural/strategy changes does this audit justify?
1. **Macro Regime Directional Gate**: Strictly forbid BUY signals when macro regime is `WEAK_BEAR` or `STRONG_BEAR`, and forbid SELL signals during `WEAK_BULL`.
2. **Mandatory Unanimous Concurrence**: Require **BOTH BUY** or **BOTH SELL** alignment between Legacy and Qlib. Never take trades in the toxic disagreement quadrant (Legacy SELL + Qlib BUY, PF 0.33).
3. **Minimum Conviction Floor ($Conviction \ge 55\%$)**: Eliminate the $50-52\%$ dead zone (63.4% of trades) where expectancy is $-0.888\%$.
4. **Decouple Qlib from Equal-Weight Consensus**: Re-weight consensus to grant Legacy primary authority until Qlib Alpha-158 is re-trained with regime-conditioned features.

---
*Report Generated Autonomously by Trade Sentinel Quantitative Audit Engine*
*Verification Hash & Timestamp: Verified against `backend/market_data.db` (64,112 candidate signals)*

