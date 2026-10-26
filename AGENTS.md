# AGENTS.md — System Rules & Safeguards

## 1. Zero Code Reverts & Regression Immunity (CRITICAL)
- **Never Revert Working Code**: When implementing bug fixes, feature upgrades, or refactorings, NEVER revert previously implemented, unrelated code to older versions.
- **Full File & Workspace Awareness**: Always inspect and analyze the full file content and related cross-workspace files (e.g. check worktrees or prior commits) before replacing or modifying code blocks.
- **Preserve Rich UI & Data Contracts**: Do not truncate or remove visual cards, drawers, telemetry indicators (RSI, ADX, ATR, F&O, NLP badges, chart/risk-reward tracks), or data contracts unless explicitly instructed by the user prompt.
- **Changes Must Always Be Loaded**: Ensure that all changes made to a file are properly saved, imported, and verified in the runtime environment.

## 2. Institutional Risk & Execution Safeguards
- **Paper-Only Execution**: `simulation_mode = true` must strictly remain active across all services. Zero live broker orders may be executed.
- **Model Registry Immutability**: Production model artifacts (`champion_ensemble.pkl`), feature weights, and SHA256 hashes must remain strictly unmodified.
- **Point-in-Time Causality**: All data ingestion, backfills, and metrics must maintain strict causal integrity. No forward-looking leakage or synthetic data falsification.

## 3. Consensus & Scanner Architecture
- **Dual-Engine Integrity**: Consensus features depend on both Microsoft Qlib (Alpha-158) and Legacy AI Multi-Model Ensemble. All metrics (ATR Volatility, RSI, ADX, F&O, FinBERT, Chart Patterns) must be fully computed and passed end-to-end to the frontend.
- **Real-Time Synchronization**: Auto-refresh loops must synchronize scans, tracked trades, and positions without causing stale closures or freezing UI state.

## 4. Editor Buffer & File Sync Integrity
- **VS Code Buffer Conflict Immunity**: Disk files updated or committed by agentic operations are strictly authoritative. Dirty in-memory editor buffers must never overwrite disk files. Always discard stale unsaved editor tabs (`Revert` / `Don't Save`) when disk files are modified.

## 5. Proactive Risk & Consequence Warnings & Disagreement Protocol (CRITICAL RULE)
- **Mandatory Disagreement Advisory (Warn Before Proceeding)**: Whenever the assistant disagrees with any user prompt, instruction, premise, or feature request (whether due to technical soundness, regression risks, trading capital safety, data causal integrity, model calibration, mathematical validity, or application stability), the assistant **MUST explicitly warn the user and explain the disagreement BEFORE proceeding with execution or editing any file**. The assistant must detail the specific risks, justify the disagreement with facts/code, and propose safer, robust alternatives for the user's explicit consideration.
- **Mandatory Consequence Advisory**: Every time the user provides a prompt, feature request, or instruction that could negatively impact application stability, trading capital, data causal integrity, model calibration, risk limits, or system goals (e.g. forward-looking bias, conflating portfolio alpha with benchmark returns, ungrounded LLM hallucinations, removing risk filters, or running unverified live orders), the assistant MUST explicitly warn the user of the potential consequences, explain the specific risks involved, and propose safer, robust alternatives before or alongside execution.

## 6. Exclusive Human Authority on Real Trading Arm/Disarm (CRITICAL RULE)
- **Zero Agentic Arm/Disarm Rights**: AI assistants and automated tools are strictly prohibited from arming or disarming live/real broker trading (via PIN bypass, automated curl, script, or API invocation).
- **Sole User Ownership**: The right to arm or disarm the system belongs exclusively and solely to the human USER through the interactive UI confirmation modal. AI assistants must never autonomously call the `/live/arm` or `/live/disarm` endpoints or forge the 4-digit security PIN.


