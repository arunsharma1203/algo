# AI CODING INTEGRITY RULES & ARCHITECTURAL INVARIANTS

> **MANDATORY DIRECTIVE FOR ALL FUTURE AI AGENTS, COPILOTS, AND DEVELOPERS**
> Every line of code written in this repository must adhere strictly to these principles.
> Never trade truth for reassurance. Never insert mock models, fake weights, or hardcoded business metrics.

---

## 1. Absolute Platform Invariants

### 1.1 Production Champion Core
- The production Champion models (`models/swing/champion_ensemble.pkl`, `models/intraday/champion_ensemble.pkl`) are immutable except via formal cryptographic promotion.
- Authoritative baseline hashes:
  - **Swing Champion**: `11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534`
  - **Intraday Champion**: `f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91`
- Under NO circumstances may any test, script, or automated tool overwrite champion models directly.

### 1.2 Database Isolation & Safety
- All automated tests must run against isolated temporary SQLite databases (`set_test_db_override`).
- Writing to production `backend/market_data.db` during test execution is strictly forbidden and actively blocked by `DatabaseSafetyLockdownError`.
- Live trading orders and outbound Telegram alerts are blocked/suppressed during test runs.

### 1.3 Truth Over Appearance
- If data is unavailable: return **NO DATA** or **UNAVAILABLE**.
- If an API or query fails: return an explicit **ERROR**, never fallback `0`, `0%`, or empty mock arrays.
- If a candidate is not evaluated: return **OOS PENDING** with exact bar deficits, never unconditional `PASS` or `READY`.
- If an artifact is missing or altered: return **BLOCKED**.

---

## 2. Research & Model Integrity Directives

### 2.1 Real Model Artifact Foundation
- Every frozen research candidate MUST be a typed `ResearchModelArtifact` containing a genuinely fitted quantitative estimator or serialized model pipeline.
- Dictionaries containing `mock_weights`, `fake_weights`, or stub parameter dicts are prohibited and rejected at the schema level.
- Every artifact MUST have an immutable SHA-256 hash computed at freeze time. Loading an artifact requires byte-exact SHA-256 verification.

### 2.2 Deterministic Experiment Identity
- Every hypothesis and experiment MUST be identified by a canonical SHA-256 `config_hash`.
- `canonicalize_config` must encompass all material dimensions: model family, feature family, target horizon, data boundaries, hyperparameters, portfolio construction, position sizing, cost tiers, and random seed.
- Database uniqueness constraints (`idx_req_unique_mission_config`, `idx_rel_unique_mission_config`) prevent duplicate configurations from consuming research budget multiple times.

### 2.3 Forward OOS Evaluation
- OOS evaluation MUST test models strictly against new forward market bars that occurred after the training/validation cutoff date.
- Evaluators must never retrain on forward data or leak future labels.
- If fewer than `MIN_FORWARD_BARS` have elapsed, the candidate remains `OOS_PENDING`.

### 2.4 Cryptographic Promotion Path
- Candidate promotion is governed solely by `ModelRegistry.promote_candidate()`.
- Promotion verifies:
  1. Candidate status is `OOS_PASSED`.
  2. Candidate is production eligible (genuine artifact).
  3. Deployed model SHA-256 matches candidate artifact SHA-256 byte-for-byte.
- Silent fallback retraining or model substitution is strictly prohibited.

---

## 3. Data Gateway & Intraday Persistence

### 3.1 Single Source of Truth Data Access
- All market data operations must route through `DataGateway`.
- 15m intraday candles fetched from market feeds must be persisted and deduplicated in `ohlcv` with `timeframe='15m'` and `hoard_timestamp`.
- Daily 1d historical bars must remain untouched and isolated from intraday bar updates.

### 3.2 Portfolio Heat & Risk Separation
- Scanner recommendations (`NOT_A_POSITION`) contribute **0%** to portfolio heat.
- Only genuine open broker positions (`LIVE_POSITION`, `PAPER_POSITION`) contribute to heat ceilings.

---

## 4. Mandatory Verification Workflow

Before concluding any session that modifies backend or frontend code:
1. Run static integrity scanner:
   ```bash
   venv/bin/python tests/integrity/static_integrity_scanner.py
   ```
2. Run platform integrity guardrails:
   ```bash
   venv/bin/python -m unittest tests/integrity/test_platform_integrity_guardrails.py
   ```
3. Run core remediation suites:
   ```bash
   venv/bin/python -m unittest tests/test_test_isolation_lockdown.py tests/test_real_model_artifact_foundation.py tests/test_real_oos_forward_pipeline.py tests/test_model_registry_real_promotion.py tests/test_dynamic_metrics_api_integrity.py tests/test_experiment_uniqueness_integrity.py tests/test_intraday_data_persistence.py
   ```
4. Verify production champion SHA-256 hashes are unchanged.
5. If any test or integrity invariant fails: **STOP IMMEDIATELY AND FIX THE UNDERLYING ROOT CAUSE.** Never weaken a test or fake a result to make it pass.

