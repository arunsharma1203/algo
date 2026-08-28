import pandas as pd
from sklearn.ensemble import RandomForestClassifier
import numpy as np
import logging

logger = logging.getLogger(__name__)

class TradeMetaLearner:
    def __init__(self):
        self.model = RandomForestClassifier(n_estimators=30, max_depth=4, random_state=42)
        self.is_trained = False
        
    def evaluate_new_trade(self, ticker, direction, trade_type, base_confidence, nlp_sentiment=0, macro_state=None, atr_pct=2.0, volume_ratio=1.0):
        """
        Takes raw trade parameters from the base ML hunters and evaluates them
        against multi-feature market conditions and historical AI mistakes.
        
        Parameters:
            ticker (str): Ticker symbol
            direction (str): 'BULLISH' or 'BEARISH'
            trade_type (str): 'INTRADAY' or 'SWING'
            base_confidence (float): Base ML conviction score (0-100+)
            nlp_sentiment (float): FinBERT sentiment score (-100 to +100)
            macro_state (dict): Output from get_macro_regime()
            atr_pct (float): Normalized ATR as percentage of price
            volume_ratio (float): Relative Volume (Current Vol / 20-period SMA Vol)
            
        Returns:
            (adjusted_score, meta_message, telemetry_dict)
        """
        if macro_state is None:
            macro_state = {}
            
        nifty_trend = macro_state.get('nifty_trend_short', 'BULLISH')
        vix_status = macro_state.get('vix_status', 'NORMAL')
        is_bullish = 1 if direction == 'BULLISH' else 0
        is_swing = 1 if trade_type == 'SWING' else 0
        
        # Determine Macro Alignment
        macro_aligned = 1 if (is_bullish and nifty_trend == 'BULLISH') or (not is_bullish and nifty_trend == 'BEARISH') else 0
        
        # 1. Fetch evaluated history to see how past trades performed
        from app.api.ml_history import evaluate_ml_history
        try:
            history = evaluate_ml_history()
            resolved = [t for t in history if t.get('outcome') not in ('OPEN', None)]
        except Exception as e:
            logger.warning(f"Meta-Learner failed reading history: {e}")
            resolved = []
            
        success_prob = 0.5
        ml_trained_msg = "Meta-Learner prior active (training data accumulating)."
        
        # 2. If sufficient resolved trades exist, train Random Forest Meta-Model
        if len(resolved) >= 5:
            try:
                df = pd.DataFrame(resolved)
                df['is_bullish'] = (df['direction'] == 'BULLISH').astype(int)
                df['is_swing'] = (df.get('trade_type', 'INTRADAY') == 'SWING').astype(int)
                df['target'] = (df['profit_pct'] > 0).astype(int)
                
                # Synthetic or default fallback features for legacy historical records
                if 'atr_pct' not in df.columns:
                    df['atr_pct'] = 2.0
                if 'volume_ratio' not in df.columns:
                    df['volume_ratio'] = 1.0
                if 'macro_aligned' not in df.columns:
                    df['macro_aligned'] = 1
                    
                features = ['confidence', 'is_bullish', 'is_swing', 'atr_pct', 'volume_ratio', 'macro_aligned']
                
                if len(df['target'].unique()) >= 2:
                    X = df[features].fillna(0)
                    y = df['target']
                    self.model.fit(X, y)
                    self.is_trained = True
                    
                    X_new = pd.DataFrame([{
                        'confidence': base_confidence,
                        'is_bullish': is_bullish,
                        'is_swing': is_swing,
                        'atr_pct': atr_pct,
                        'volume_ratio': volume_ratio,
                        'macro_aligned': macro_aligned
                    }])
                    success_prob = float(self.model.predict_proba(X_new)[0][1])
                    ml_trained_msg = f"Meta-Learner RF Active (Win-rate prior: {int(success_prob * 100)}%)"
            except Exception as e:
                logger.error(f"Meta-Learner training error: {e}")

        # 3. Factor Adjustments & Telemetry
        adjustments = {}
        
        # A. ML Success Probability Adjustment
        if success_prob < 0.35:
            adjustments['historical_rf'] = -10.0
        elif success_prob < 0.45:
            adjustments['historical_rf'] = -4.0
        elif success_prob > 0.65:
            adjustments['historical_rf'] = +6.0
        else:
            adjustments['historical_rf'] = 0.0
            
        # B. Volume Breakout Multiplier
        if volume_ratio >= 2.0:
            adjustments['volume_surge'] = +6.0
        elif volume_ratio >= 1.4:
            adjustments['volume_surge'] = +3.0
        elif volume_ratio < 0.7:
            adjustments['volume_surge'] = -5.0
        else:
            adjustments['volume_surge'] = 0.0
            
        # C. Volatility (ATR) Regime
        if trade_type == 'SWING':
            if atr_pct > 5.5:
                adjustments['volatility'] = -5.0 # Excessive volatility for swing holding
            elif 1.8 <= atr_pct <= 4.0:
                adjustments['volatility'] = +3.0 # Sweet spot for swing trends
            else:
                adjustments['volatility'] = 0.0
        else: # INTRADAY
            if atr_pct > 2.5:
                adjustments['volatility'] = +4.0 # High range good for intraday momentum
            elif atr_pct < 0.8:
                adjustments['volatility'] = -4.0 # Flat/stagnant
            else:
                adjustments['volatility'] = 0.0

        # D. Macro Alignment
        if macro_aligned == 1:
            adjustments['macro_alignment'] = +4.0
        else:
            adjustments['macro_alignment'] = -8.0 # Strong headwind
            
        total_adjustment = sum(adjustments.values())
        final_score = max(0.0, min(100.0, base_confidence + total_adjustment))
        
        # Build intuitive meta-message
        reasons = []
        if adjustments['volume_surge'] > 0:
            reasons.append(f"Volume Surge ({volume_ratio:.1f}x)")
        elif adjustments['volume_surge'] < 0:
            reasons.append(f"Low Volume ({volume_ratio:.1f}x)")
            
        if adjustments['macro_alignment'] > 0:
            reasons.append("Aligned with NIFTY")
        elif adjustments['macro_alignment'] < 0:
            reasons.append("Opposing Macro Trend")
            
        if adjustments['volatility'] < 0:
            reasons.append(f"Excessive Volatility ({atr_pct:.1f}% ATR)")
        elif adjustments['volatility'] > 0:
            reasons.append(f"Optimal Volatility ({atr_pct:.1f}% ATR)")
            
        summary_reasons = ", ".join(reasons) if reasons else "Neutral conditions"
        action_verb = "boosted" if total_adjustment > 0 else "penalized" if total_adjustment < 0 else "confirmed"
        meta_message = f"Meta-Learner {action_verb} score by {total_adjustment:+.1f} pts ({summary_reasons}). {ml_trained_msg}"
        
        telemetry = {
            "atr_pct": round(float(atr_pct), 2),
            "volume_ratio": round(float(volume_ratio), 2),
            "macro_aligned": bool(macro_aligned),
            "macro_trend": nifty_trend,
            "vix_status": vix_status,
            "success_prob": round(float(success_prob) * 100, 1),
            "total_adjustment": round(float(total_adjustment), 1),
            "adjustments_breakdown": adjustments,
            "message": meta_message
        }
        
        return final_score, meta_message, telemetry

meta_learner = TradeMetaLearner()
