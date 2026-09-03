"""
Shared Decision Engine — Authoritative Final Qualification Gate
================================================================

This module contains the SINGLE authoritative decision pipeline that BOTH
manual and autonomous scanners must call. The trigger can differ; the brain
cannot.

Pipeline:
    1. Data Validation (required)
    2. Feature Engineering (required)
    3. Base Model Inference — RF/GB/SVC Ensemble (required)
    4. Optional Signals — NLP/VADER, F&O, TimesFM, Chronos, Macro (best-effort)
    5. Meta-Learner Arbitration (required)
    6. Calibration (required)
    7. Final Qualification Gate
    8. Pipeline Telemetry

Usage:
    from app.analytics.decision_engine import evaluate_ticker, QualificationResult

    result = evaluate_ticker(
        ticker="RELIANCE.NS",
        df=historical_df,            # pre-fetched OHLCV DataFrame
        champion_model=model,        # loaded champion ensemble
        champion_meta=meta,          # champion metadata dict
        trade_type="INTRADAY",       # or "SWING"
        source="MANUAL",             # or "AUTOPILOT"
    )

    if result.qualified:
        save_ml_trade(...)
"""

import logging
import numpy as np
import pandas as pd
import ta
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger(__name__)


@dataclass
class QualificationResult:
    """Immutable result of the shared decision engine for a single ticker."""
    ticker: str
    qualified: bool
    direction: str  # BULLISH or BEARISH
    is_bullish: bool
    confidence: float  # Final calibrated confidence (0-100)
    raw_confidence: float  # Pre-calibration confidence
    entry: float
    sl: float
    tp1: float
    tp2: float
    base_probs: Tuple[float, float, float]  # (RF, GB, SVC)
    rejection_reason: Optional[str] = None
    meta_learner_msg: str = ""
    telemetry: Dict[str, Any] = field(default_factory=dict)
    calibration: Dict[str, Any] = field(default_factory=dict)
    explanation: Dict[str, Any] = field(default_factory=dict)
    pipeline_components: Dict[str, Any] = field(default_factory=dict)

    # Optional enrichment data (populated if available)
    nlp_sentiment: float = 0.0
    nlp_headline: str = ""
    fno_info: Optional[Dict[str, Any]] = None
    timesfm_forecast: Optional[Dict[str, Any]] = None
    chronos_forecast: Optional[Dict[str, Any]] = None
    foundation_features: Optional[Any] = None

    # Price integrity and strategy direction fields
    model_direction: str = "BULLISH"
    strategy_direction: str = "BULLISH"
    reference_price: float = 0.0
    model_candle_close: float = 0.0
    price_source: str = "Candle Close"
    price_timestamp: str = ""
    price_is_fresh: bool = False

    atr_pct: float = 0.0
    volume_ratio: float = 1.0
    rsi: float = 50.0
    adx: float = 0.0
    macd_diff: float = 0.0
    score: float = 0.0


# ─── Intraday features ────────────────────────────────────────────────
INTRADAY_FEATURES = ['rsi', 'macd', 'macd_diff', 'adx', 'returns']
SWING_FEATURES = ['rsi', 'macd', 'macd_diff', 'adx', 'atr']


def evaluate_ticker(
    ticker: str,
    df,  # pandas DataFrame with OHLCV
    champion_model,
    champion_meta: Dict[str, Any],
    trade_type: str = "INTRADAY",
    source: str = "MANUAL",
    macro_state: Optional[Dict[str, Any]] = None,
    qualification_threshold: float = 0.0,
    skip_enrichment: bool = False,
) -> QualificationResult:
    """
    Authoritative shared decision engine.

    Runs the FULL pipeline for a single ticker:
      Validation → Features → Ensemble → NLP → F&O → Foundation → Meta-Learner → Calibration → Gate

    Args:
        ticker: NSE/BSE symbol
        df: Pre-fetched OHLCV DataFrame (already validated or raw)
        champion_model: Loaded champion VotingClassifier ensemble
        champion_meta: Champion metadata dict (version, features, etc.)
        trade_type: 'INTRADAY' or 'SWING'
        source: 'MANUAL' or 'AUTOPILOT' — informational, does NOT change logic
        macro_state: Pre-fetched macro regime dict (optional, fetched if None)
        qualification_threshold: Minimum calibrated confidence to qualify (0 = always qualify)
        skip_enrichment: If True, skip NLP/F&O/Foundation (for batch speed)

    Returns:
        QualificationResult with all pipeline outputs and pipeline_components telemetry.
    """
    from app.data.validator import MarketDataValidator

    pipeline = {
        "data_validation": False,
        "feature_engineering": False,
        "rf": False,
        "gb": False,
        "svc": False,
        "ensemble": False,
        "nlp_vader": "SKIPPED",
        "fno": "SKIPPED",
        "timesfm": "SKIPPED",
        "chronos": "SKIPPED",
        "macro": False,
        "meta_learner": False,
        "calibration": False,
        "final_gate": False,
        "source": source,
        "qualified": False,
    }

    features = SWING_FEATURES if trade_type == "SWING" else INTRADAY_FEATURES
    champ_v = champion_meta.get("version", "v1.0-champion")
    min_rows = 60 if trade_type == "SWING" else 30
    timeframe = "1d" if trade_type == "SWING" else "15m"

    # ── 1. Data Validation ──────────────────────────────────────────
    try:
        val_report = MarketDataValidator.validate_ohlcv(
            df, ticker=ticker, timeframe=timeframe, min_rows=min_rows
        )
        if not val_report["valid"]:
            return _rejection(ticker, "DATA_VALIDATION_FAILED", pipeline, val_report.get("reason", "Invalid data"))
        pipeline["data_validation"] = True
    except Exception as e:
        return _rejection(ticker, "DATA_VALIDATION_ERROR", pipeline, str(e))

    # ── 2. Feature Engineering ──────────────────────────────────────
    try:
        df_work = df.copy()
        if isinstance(df_work.columns, pd.MultiIndex) or (len(df_work.columns) > 0 and isinstance(df_work.columns[0], tuple)):
            df_work.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df_work.columns]
        else:
            df_work.columns = [str(col).lower() for col in df_work.columns]

        df_work['rsi'] = ta.momentum.RSIIndicator(df_work['close'], window=14).rsi()
        macd_ind = ta.trend.MACD(df_work['close'])
        df_work['macd'] = macd_ind.macd()
        df_work['macd_diff'] = macd_ind.macd_diff()
        df_work['adx'] = ta.trend.ADXIndicator(
            df_work['high'], df_work['low'], df_work['close'], window=14
        ).adx()
        df_work['atr'] = ta.volatility.AverageTrueRange(
            df_work['high'], df_work['low'], df_work['close'], window=14
        ).average_true_range()
        df_work['returns'] = df_work['close'].pct_change()

        ml_df = df_work.dropna(subset=features).copy()
        if len(ml_df) < min_rows:
            return _rejection(ticker, "INSUFFICIENT_DATA", pipeline,
                              f"Only {len(ml_df)} rows after feature engineering (need {min_rows})")
        pipeline["feature_engineering"] = True
    except Exception as e:
        return _rejection(ticker, "FEATURE_ENGINEERING_ERROR", pipeline, str(e))

    # ── 3. Base Model Inference (RF / GB / SVC Ensemble) ────────────
    latest_row = ml_df.iloc[-1]
    latest_features = latest_row[features].values.reshape(1, -1)
    latest_features = np.nan_to_num(latest_features)

    p_rf, p_gb, p_svm = 50.0, 50.0, 50.0
    prob_up_raw = 50.0

    if champion_model is None:
        return _rejection(ticker, "NO_CHAMPION_MODEL", pipeline, "Champion model is None")

    try:
        prob_up_raw = float(champion_model.predict_proba(latest_features)[0][1]) * 100.0
        pipeline["ensemble"] = True

        if hasattr(champion_model, 'estimators_') and len(champion_model.estimators_) == 3:
            try:
                p_rf = float(champion_model.estimators_[0].predict_proba(latest_features)[0][1]) * 100.0
                p_gb = float(champion_model.estimators_[1].predict_proba(latest_features)[0][1]) * 100.0
                p_svm = float(champion_model.estimators_[2].predict_proba(latest_features)[0][1]) * 100.0
                pipeline["rf"] = True
                pipeline["gb"] = True
                pipeline["svc"] = True
            except Exception:
                p_rf, p_gb, p_svm = prob_up_raw, prob_up_raw, prob_up_raw
                pipeline["rf"] = True
                pipeline["gb"] = True
                pipeline["svc"] = True
    except Exception as e:
        return _rejection(ticker, "MODEL_INFERENCE_ERROR", pipeline, str(e))

    base_probs = (p_rf, p_gb, p_svm)

    # Direction assessment
    is_bullish = bool(prob_up_raw >= 50.0)
    model_direction = "BULLISH" if is_bullish else "BEARISH"

    # ── CRITICAL: CASH-EQUITY SWING SHORT BAN ────────────────────────
    # In Indian cash equity markets, short positions cannot be carried overnight.
    # Therefore, SWING recommendations must strictly be BULLISH / LONG only.
    if trade_type == "SWING" and not is_bullish:
        pipeline["final_gate"] = True
        pipeline["qualified"] = False
        return _rejection(
            ticker=ticker,
            reason_code="SWING_CASH_SHORT_DISALLOWED",
            pipeline=pipeline,
            detail="Cash-equity swing short disallowed (Indian equity multi-day positions must be LONG)",
            model_direction=model_direction,
            raw_confidence=prob_up_raw
        )

    strategy_direction = "BULLISH" if is_bullish else "BEARISH"
    direction = strategy_direction

    # Compute technical indicator parameters
    latest_close = float(latest_row['close'])
    model_candle_close = latest_close
    latest_atr = float(latest_row.get('atr', 0.0))
    latest_rsi = float(latest_row.get('rsi', 50.0))
    latest_adx = float(latest_row.get('adx', 0.0))
    latest_macd_diff = float(latest_row.get('macd_diff', 0.0))

    atr_pct_val = (latest_atr / latest_close * 100) if latest_close > 0 else 2.0
    vol_sma20 = ml_df['volume'].rolling(20).mean().iloc[-1] if 'volume' in ml_df.columns else 0
    vol_ratio = float(latest_row['volume'] / vol_sma20) if (vol_sma20 and vol_sma20 > 0) else 1.0

    # ── REFERENCE / EXECUTION PRICE INTEGRITY ─────────────────────────
    # Distinguish Model Candle Close (for features) vs Live Execution LTP (for entry/orders)
    reference_price = model_candle_close
    price_source = "Model Candle Close"
    price_timestamp = datetime.now().strftime("%H:%M:%S IST")
    price_is_fresh = False

    if not skip_enrichment:
        try:
            from app.data.market_provider import get_live_quote_with_meta
            quote_meta = get_live_quote_with_meta(ticker)
            live_price = quote_meta.get("price")
            if live_price is not None and float(live_price) > 0:
                reference_price = float(live_price)
                price_source = quote_meta.get("source_name", "Live Market Feed")
                price_timestamp = quote_meta.get("timestamp", price_timestamp)
                price_is_fresh = bool(quote_meta.get("is_realtime", False))
        except Exception as e:
            logger.warning(f"Could not retrieve live quote for {ticker}: {e}")

    entry_price = reference_price

    if trade_type == "SWING":
        sl = float(entry_price - (latest_atr * 2))
        tp1 = float(entry_price + (latest_atr * 3))
        tp2 = float(entry_price + (latest_atr * 6))
    else:
        atr_multiplier = 1.5 if atr_pct_val <= 2.5 else 2.0
        if is_bullish:
            sl = float(entry_price - (latest_atr * atr_multiplier))
            tp1 = float(entry_price + (latest_atr * atr_multiplier))
            tp2 = float(entry_price + (latest_atr * (atr_multiplier * 2)))
        else:
            sl = float(entry_price + (latest_atr * atr_multiplier))
            tp1 = float(entry_price - (latest_atr * atr_multiplier))
            tp2 = float(entry_price - (latest_atr * (atr_multiplier * 2)))

    # ── 4. Macro Regime ─────────────────────────────────────────────
    if macro_state is None:
        try:
            from app.analytics.macro_engine import get_macro_regime
            macro_state = get_macro_regime()
        except Exception:
            macro_state = {}
    pipeline["macro"] = True

    # ── 5. Optional Enrichment (NLP, F&O, Foundation Models) ────────
    nlp_sentiment = 0.0
    nlp_headline = ""
    fno_info = None
    found_features = None
    timesfm_dict = None
    chronos_dict = None

    if not skip_enrichment:
        # NLP / VADER
        try:
            from app.analytics.nlp_engine import nlp_engine
            nlp_result = nlp_engine.analyze_ticker_news(ticker)
            nlp_sentiment = nlp_result.get('score', 0)
            nlp_headline = nlp_result.get('headline', '')
            pipeline["nlp_vader"] = True
        except Exception:
            pipeline["nlp_vader"] = "OFFLINE"

        # F&O — only for intraday
        if trade_type == "INTRADAY":
            try:
                from app.analytics.fno_engine import fetch_nse_option_chain
                clean_sym = ticker.replace('.NS', '').replace('.BO', '')
                fno_info = fetch_nse_option_chain(clean_sym)
                pipeline["fno"] = True if fno_info and fno_info.get("is_live_nse") else "UNAVAILABLE"
            except Exception:
                pipeline["fno"] = "OFFLINE"

        # Foundation Models (TimesFM & Chronos) — advisory, non-blocking
        try:
            from app.analytics.foundation_models.manager import foundation_model_manager
            horizon = 5 if trade_type == "SWING" else 1
            tfm_tf = "1d" if trade_type == "SWING" else "15m"
            tfm_res, chr_res, found_features = foundation_model_manager.generate_foundation_signals(
                symbol=ticker,
                historical_df=ml_df,
                timeframe=tfm_tf,
                horizon_bars=horizon,
                as_of_time=datetime.now()
            )
            timesfm_dict = tfm_res.to_dict() if tfm_res else None
            chronos_dict = chr_res.to_dict() if chr_res else None
            pipeline["timesfm"] = True if (tfm_res and tfm_res.status == "success") else "OFFLINE"
            pipeline["chronos"] = True if (chr_res and chr_res.status == "success") else "OFFLINE"
        except Exception:
            pipeline["timesfm"] = "OFFLINE"
            pipeline["chronos"] = "OFFLINE"
    else:
        pipeline["nlp_vader"] = "SKIPPED"
        pipeline["fno"] = "SKIPPED"
        pipeline["timesfm"] = "SKIPPED"
        pipeline["chronos"] = "SKIPPED"

    # ── 6. Meta-Learner ─────────────────────────────────────────────
    adjusted_score = prob_up_raw
    meta_message = ""
    telemetry = {}

    try:
        from app.analytics.meta_learner import meta_learner
        adjusted_score, meta_message, telemetry = meta_learner.evaluate_new_trade(
            ticker=ticker,
            direction=direction,
            trade_type=trade_type,
            base_confidence=prob_up_raw,
            base_probs=base_probs,
            nlp_sentiment=nlp_sentiment,
            macro_state=macro_state,
            atr_pct=atr_pct_val,
            volume_ratio=vol_ratio,
            foundation_features=found_features
        )
        pipeline["meta_learner"] = True
    except Exception as e:
        logger.warning(f"[DecisionEngine] Meta-Learner failed for {ticker}: {e}")
        pipeline["meta_learner"] = "FAILED"

    # ── 7. Calibration ──────────────────────────────────────────────
    calibrated_score = adjusted_score
    calib_meta = {"status": "uncalibrated"}
    try:
        from app.analytics.calibration import calibrator
        calibrated_score, raw_score_out, calib_meta = calibrator.calibrate(adjusted_score)
        pipeline["calibration"] = True
    except Exception:
        pipeline["calibration"] = "FAILED"

    # ── 8. Final Qualification Gate ─────────────────────────────────
    pipeline["final_gate"] = True
    qualified = True
    rejection_reason = None

    if qualification_threshold > 0 and calibrated_score < qualification_threshold:
        qualified = False
        rejection_reason = f"Below threshold ({calibrated_score:.1f}% < {qualification_threshold:.1f}%)"

    pipeline["qualified"] = qualified

    # Compose score (compatible with existing usage)
    score_val = calibrated_score
    if trade_type == "SWING":
        technical_bonus = 0
        if latest_rsi < 40 and latest_macd_diff > 0:
            technical_bonus += 15
        if latest_adx > 25:
            technical_bonus += 10
        score_val = float(calibrated_score + technical_bonus)

    # Build explanation payload
    explanation_payload = {
        "base_score": round(float(prob_up_raw), 1),
        "raw_score": round(float(adjusted_score), 1),
        "calibrated_score": round(float(calibrated_score), 1),
        "calibration_meta": calib_meta,
        "nlp_sentiment": nlp_sentiment,
        "nlp_headline": nlp_headline,
        "atr_pct": round(atr_pct_val, 2),
        "volume_ratio": round(vol_ratio, 2),
        "macro_regime": telemetry.get("macro_trend", macro_state.get("nifty_trend_short", "UNKNOWN")),
        "macro_aligned": telemetry.get("macro_aligned", True),
        "meta_message": meta_message,
        "timesfm": timesfm_dict,
        "chronos": chronos_dict,
        "fno_pcr": fno_info.get("pcr") if fno_info else None,
        "fno_max_pain": fno_info.get("max_pain") if fno_info else None,
        "champion_version": champ_v,
        "source": source,
        "pipeline_components": pipeline,
        "model_direction": model_direction,
        "strategy_direction": strategy_direction,
        "reference_price": round(reference_price, 2),
        "model_candle_close": round(model_candle_close, 2),
        "price_source": price_source,
        "price_timestamp": price_timestamp,
        "price_is_fresh": price_is_fresh,
    }

    try:
        from app.analytics.master_logger import MasterLogger
        MasterLogger.log_event(
            category="DECISION_ENGINE",
            event_type="QUALIFIED",
            message=f"{ticker} qualified with {calibrated_score:.1f}% confidence ({direction})",
            ticker=ticker,
            details={"confidence": calibrated_score, "trade_type": trade_type, "entry": entry_price, "source": source}
        )
    except Exception:
        pass

    return QualificationResult(
        ticker=ticker,
        qualified=qualified,
        direction=direction,
        is_bullish=is_bullish,
        confidence=calibrated_score,
        raw_confidence=prob_up_raw,
        entry=round(entry_price, 2),
        sl=round(sl, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        base_probs=base_probs,
        rejection_reason=rejection_reason,
        meta_learner_msg=meta_message,
        telemetry=telemetry,
        calibration=calib_meta,
        explanation=explanation_payload,
        pipeline_components=pipeline,
        nlp_sentiment=nlp_sentiment,
        nlp_headline=nlp_headline,
        fno_info=fno_info,
        timesfm_forecast=timesfm_dict,
        chronos_forecast=chronos_dict,
        foundation_features=found_features,
        model_direction=model_direction,
        strategy_direction=strategy_direction,
        reference_price=round(reference_price, 2),
        model_candle_close=round(model_candle_close, 2),
        price_source=price_source,
        price_timestamp=price_timestamp,
        price_is_fresh=price_is_fresh,
        atr_pct=round(atr_pct_val, 2),
        volume_ratio=round(vol_ratio, 2),
        rsi=latest_rsi,
        adx=latest_adx,
        macd_diff=latest_macd_diff,
        score=score_val,
    )


def _rejection(
    ticker: str,
    reason_code: str,
    pipeline: dict,
    detail: str,
    model_direction: str = "NEUTRAL",
    raw_confidence: float = 0.0
) -> QualificationResult:
    """Creates a rejected QualificationResult with explicit model and rejection diagnostics."""
    pipeline["qualified"] = False
    try:
        from app.analytics.master_logger import MasterLogger
        MasterLogger.log_event(
            category="DECISION_ENGINE",
            event_type="REJECTED",
            message=f"{ticker} rejected: {reason_code} ({detail})",
            ticker=ticker,
            details={"reason_code": reason_code, "detail": detail, "model_direction": model_direction},
            severity="DEBUG" if "DISALLOWED" in reason_code else "INFO"
        )
    except Exception:
        pass

    return QualificationResult(
        ticker=ticker,
        qualified=False,
        direction="NEUTRAL",
        is_bullish=False,
        confidence=0.0,
        raw_confidence=raw_confidence,
        entry=0.0,
        sl=0.0,
        tp1=0.0,
        tp2=0.0,
        base_probs=(0.0, 0.0, 0.0),
        rejection_reason=f"{reason_code}: {detail}",
        pipeline_components=pipeline,
        model_direction=model_direction,
        strategy_direction="REJECTED",
        explanation={
            "rejection_reason": f"{reason_code}: {detail}",
            "model_direction": model_direction,
            "pipeline_components": pipeline
        }
    )
