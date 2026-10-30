import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock
from app.analytics.nlp_engine import FinBERTAnalyzer, FinancialSentimentAnalyzer, nlp_engine
from app.analytics.autonomous_bot import evaluate_single_trade_risk


class TestFinBERTSentiment(unittest.TestCase):

    def setUp(self):
        self.analyzer = FinancialSentimentAnalyzer()

    def test_01_polarity_scores_keys(self):
        """Verify polarity_scores returns neg, neu, pos, compound matching standard interface."""
        scores = self.analyzer.analyzer.polarity_scores("Company reports solid quarterly profit.")
        self.assertIn("neg", scores)
        self.assertIn("neu", scores)
        self.assertIn("pos", scores)
        self.assertIn("compound", scores)
        self.assertIsInstance(scores["compound"], float)
        self.assertGreaterEqual(scores["compound"], -1.0)
        self.assertLessEqual(scores["compound"], 1.0)

    def test_02_bullish_vs_bearish_differentiation(self):
        """Verify FinBERT distinguishes strongly positive vs strongly negative financial events."""
        bullish = self.analyzer.analyzer.polarity_scores("TCS beats all revenue estimates with record order bookings.")
        bearish = self.analyzer.analyzer.polarity_scores("Company defaults on debt payment and fraud inquiry launched.")
        self.assertGreater(bullish["pos"], bullish["neg"])
        self.assertGreater(bearish["neg"], bearish["pos"])
        self.assertGreater(bullish["compound"], bearish["compound"])

    def test_03_point_in_time_future_news_filtering(self):
        """Verify news published strictly after as_of_timestamp is excluded."""
        as_of = datetime(2026, 1, 1, 12, 0, 0)
        future_ts = datetime(2026, 1, 2, 12, 0, 0).timestamp()
        past_ts = datetime(2025, 12, 31, 12, 0, 0).timestamp()

        mock_news = [
            {
                "title": "RELIANCE massive discovery in 2026",
                "summary": "RELIANCE will announce big plans tomorrow",
                "providerPublishTime": future_ts
            },
            {
                "title": "RELIANCE past operational update",
                "summary": "RELIANCE completed maintenance successfully",
                "providerPublishTime": past_ts
            }
        ]

        with patch("yfinance.Ticker") as mock_ticker:
            mock_inst = MagicMock()
            mock_inst.news = mock_news
            mock_ticker.return_value = mock_inst

            res = self.analyzer.analyze_ticker_news("RELIANCE.NS", as_of_timestamp=as_of)
            self.assertEqual(res["status"], "active")
            self.assertEqual(res["articles_analyzed"], 1)
            self.assertIn("RELIANCE past operational update", res["headline"])

    def test_04_entity_relevance_filtering(self):
        """Verify articles that do not mention the base ticker are excluded."""
        mock_news = [
            {
                "title": "INFOSYS reports quarterly results",
                "summary": "Tech sector performance overview",
                "providerPublishTime": datetime.now().timestamp()
            }
        ]

        with patch("yfinance.Ticker") as mock_ticker:
            mock_inst = MagicMock()
            mock_inst.news = mock_news
            mock_ticker.return_value = mock_inst

            # Analyzing RELIANCE when news only mentions INFOSYS
            res = self.analyzer.analyze_ticker_news("RELIANCE.NS")
            self.assertEqual(res["status"], "neutral")
            self.assertEqual(res["articles_analyzed"], 0)
            self.assertEqual(res["score"], 0.0)

    def test_05_offline_fallback_safety(self):
        """Verify that when torch/transformers fails, defensive fallback operates with zero crash."""
        analyzer_fail = FinBERTAnalyzer()
        analyzer_fail._load_failed = True
        scores = analyzer_fail.polarity_scores("Market rally continues for blue-chip equities.")
        self.assertIn("compound", scores)
        self.assertIn("pos", scores)

    def test_06_autonomous_bot_risk_breakdown_finbert(self):
        """Verify autonomous_bot risk evaluator includes FinBERT sentiment breakdown without KeyError."""
        trade = {
            'id': 101,
            'ticker': 'TATAMOTORS.NS',
            'direction': 'BEARISH',
            'entry': 850.0,
            'sl': 880.0,
            'confidence': 68.0,
            'trade_type': 'INTRADAY',
            'nlp_sentiment': 45.0,
            'timestamp': datetime.now().isoformat()
        }

        with patch("app.analytics.nlp_engine.nlp_engine.analyze_ticker_news", return_value={"score": 45.0, "headline": "Record EV sales surge"}):
            audit = evaluate_single_trade_risk(trade, macro={"nifty_trend_short": "BULLISH", "india_vix": 14.0}, current_price=852.0)

        self.assertIsNotNone(audit)
        self.assertIn("FinBERT Financial Sentiment", audit["model_breakdown"])
        self.assertTrue(audit["model_breakdown"]["FinBERT Financial Sentiment"]["triggered"])
        self.assertIn("VADER Financial Sentiment", audit["model_breakdown"])


if __name__ == '__main__':
    unittest.main()

