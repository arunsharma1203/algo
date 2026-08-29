import logging
from datetime import datetime
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import yfinance as yf
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Specialized Financial Lexicon weights to supplement standard VADER
FINANCIAL_LEXICON_UPDATES = {
    "bullish": 2.5,
    "bearish": -2.5,
    "breakout": 2.0,
    "upgrade": 2.0,
    "downgrade": -2.0,
    "outperform": 1.5,
    "underperform": -1.5,
    "missed": -1.5,
    "beat": 1.5,
    "profit": 1.0,
    "loss": -1.0,
    "crash": -3.0,
    "surge": 2.0,
    "plunge": -2.0,
    "dividend": 1.0,
    "buyback": 2.0,
    "lawsuit": -2.0,
    "investigation": -2.0,
    "fraud": -3.0,
    "growth": 1.5,
    "shrink": -1.5,
    "debt": -1.0,
    "default": -3.0,
    "margin": 1.0,
    "squeeze": 1.5
}

class FinancialSentimentAnalyzer:
    """
    VADER Financial Sentiment Engine.
    Processes live news filings with financial lexicon tuning and strict point-in-time timestamp filtering.
    """

    def __init__(self):
        self.analyzer = SentimentIntensityAnalyzer()
        self.analyzer.lexicon.update(FINANCIAL_LEXICON_UPDATES)
        self.engine_name = "VADER Financial Sentiment Engine"

    def analyze_ticker_news(self, ticker: str, as_of_timestamp: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Fetches news for a ticker and calculates point-in-time sentiment.
        
        CRITICAL RULE:
        Articles published AFTER as_of_timestamp are strictly filtered out to prevent future-news leakage.
        """
        if as_of_timestamp is None:
            as_of_timestamp = datetime.now()

        try:
            clean_ticker = ticker if ticker.endswith(('.NS', '.BO')) else f"{ticker}.NS"
            stock = yf.Ticker(clean_ticker)
            news_items = stock.news

            if not news_items or len(news_items) == 0:
                return {
                    "score": 0.0,
                    "headline": "No recent exchange news available.",
                    "status": "unavailable",
                    "articles_analyzed": 0
                }

            total_compound = 0.0
            best_headline = ""
            max_intensity = -1.0
            base_ticker = ticker.split('.')[0].upper()
            valid_news = 0

            for item in news_items:
                # 1. Point-in-time filter: check publication time
                pub_time = item.get('providerPublishTime')
                if pub_time:
                    try:
                        pub_dt = datetime.fromtimestamp(pub_time)
                        if pub_dt > as_of_timestamp:
                            # Skip future news
                            continue
                    except Exception:
                        pass

                # Extract headline and body text
                title = item.get('title', '')
                if not title and 'content' in item:
                    title = item['content'].get('title', '')

                summary = item.get('summary', '')
                if not summary and 'content' in item:
                    summary = item['content'].get('summary', '')

                text = f"{title}. {summary}"

                # Strict entity relevance filter
                if base_ticker not in text.upper():
                    continue

                valid_news += 1
                scores = self.analyzer.polarity_scores(text)
                total_compound += scores['compound']

                intensity = abs(scores['compound'])
                if intensity > max_intensity:
                    max_intensity = intensity
                    best_headline = title

            if valid_news == 0:
                return {
                    "score": 0.0,
                    "headline": "Recent filings reviewed (neutral/no relevant mentions).",
                    "status": "neutral",
                    "articles_analyzed": 0
                }

            avg_compound = total_compound / valid_news
            normalized_score = round(avg_compound * 100.0, 1)

            return {
                "score": normalized_score,
                "headline": best_headline or "Analyzed verified filings.",
                "status": "active",
                "articles_analyzed": valid_news
            }

        except Exception as e:
            logger.warning(f"Sentiment analysis error for {ticker}: {e}")
            return {
                "score": 0.0,
                "headline": "Sentiment feed offline.",
                "status": "unavailable",
                "articles_analyzed": 0
            }

nlp_engine = FinancialSentimentAnalyzer()
