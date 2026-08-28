import pandas as pd
from sklearn.ensemble import RandomForestClassifier
import numpy as np

class TradeMetaLearner:
    def __init__(self):
        self.model = RandomForestClassifier(n_estimators=20, max_depth=3, random_state=42)
        self.is_trained = False
        
    def evaluate_new_trade(self, ticker, direction, trade_type, base_confidence, nlp_sentiment):
        """
        Takes the raw trade from the Hunter ML and evaluates it against historical AI performance.
        Returns: (Adjusted Confidence, Meta-Learner Message)
        """
        # 1. Fetch evaluated history to see how past trades performed
        from app.api.ml_history import evaluate_ml_history
        history = evaluate_ml_history()
        
        # Filter only resolved trades
        resolved = [t for t in history if t['outcome'] != 'OPEN']
        
        if len(resolved) < 5:
            return base_confidence, "Meta-Learner warming up (Needs 5+ resolved trades to learn)."
            
        # 2. Build Training Dataset for Meta-Learner
        df = pd.DataFrame(resolved)
        
        # Feature Engineering for the Meta-Learner
        df['is_bullish'] = (df['direction'] == 'BULLISH').astype(int)
        df['is_swing'] = (df['trade_type'] == 'SWING').astype(int)
        
        # Target: 1 if profitable, 0 if stopped out
        df['target'] = (df['profit_pct'] > 0).astype(int)
        
        features = ['confidence', 'is_bullish', 'is_swing']
        
        X = df[features]
        y = df['target']
        
        # Only train if we have both classes (wins and losses)
        if len(y.unique()) < 2:
            return base_confidence, "Meta-Learner warming up (Needs both wins and losses to calibrate)."
            
        # 3. Train the Meta-Learner on past mistakes
        self.model.fit(X, y)
        self.is_trained = True
        
        # 4. Predict success of the NEW trade
        X_new = pd.DataFrame([{
            'confidence': base_confidence,
            'is_bullish': 1 if direction == 'BULLISH' else 0,
            'is_swing': 1 if trade_type == 'SWING' else 0
        }])
        
        # Get probability that this trade will be successful (Target=1)
        success_prob = self.model.predict_proba(X_new)[0][1]
        
        # 5. Adjust the final conviction score mathematically
        # If success_prob is high (>0.6), boost the score.
        # If success_prob is low (<0.4), penalize the score.
        adjustment = 0
        message = "Meta-Learner confirms Hunter conviction."
        
        if success_prob < 0.3:
            adjustment = -15
            message = f"Meta-Learner Veto: Historical data shows low win-rate ({int(success_prob*100)}%) for similar setups. Score severely penalized."
        elif success_prob < 0.45:
            adjustment = -5
            message = f"Meta-Learner Warning: Cautious historical performance. Score reduced."
        elif success_prob > 0.7:
            adjustment = 10
            message = f"Meta-Learner Boost: High historical win-rate ({int(success_prob*100)}%) for similar setups. Score boosted!"
            
        final_score = base_confidence + adjustment
        # Clamp between 0 and 100
        final_score = max(0, min(100, final_score))
        
        return final_score, message

meta_learner = TradeMetaLearner()
