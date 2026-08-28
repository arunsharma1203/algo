from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import yfinance as yf

# Fine-tune the standard VADER lexicon to better understand Wall Street / Dalal Street terminology
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
    def __init__(self):
        self.analyzer = SentimentIntensityAnalyzer()
        self.analyzer.lexicon.update(FINANCIAL_LEXICON_UPDATES)

    def analyze_ticker_news(self, ticker: str):
        """
        Fetches the latest news for a ticker from Yahoo Finance and runs NLP sentiment analysis.
        Returns a sentiment score between -100 (Extremely Bearish) and +100 (Extremely Bullish).
        """
        try:
            stock = yf.Ticker(ticker)
            news_items = stock.news
            
            if not news_items or len(news_items) == 0:
                return {"score": 0, "headline": "No recent news detected."}
                
            total_compound = 0
            best_headline = ""
            max_intensity = -1
            
            base_ticker = ticker.split('.')[0].upper()
            
            valid_news = 0
            for item in news_items:
                # Yahoo Finance news items have title and summary inside 'content' or directly
                title = item.get('title', '')
                if not title and 'content' in item:
                    title = item['content'].get('title', '')
                    
                summary = item.get('summary', '')
                if not summary and 'content' in item:
                    summary = item['content'].get('summary', '')
                    
                text = f"{title}. {summary}"
                
                # STRICT FILTER: Ensure the news is ACTUALLY about this exact stock.
                # Yahoo often bundles generic sector news or competitor news under a ticker.
                if base_ticker not in text.upper():
                    continue
                    
                valid_news += 1
                scores = self.analyzer.polarity_scores(text)
                total_compound += scores['compound']
                
                # Keep track of the most intense headline to show the user
                intensity = abs(scores['compound'])
                if intensity > max_intensity:
                    max_intensity = intensity
                    best_headline = title
                    
            if valid_news == 0:
                return {"score": 0, "headline": "No strictly related news detected."}
            avg_compound = total_compound / valid_news
            
            # Map [-1.0, 1.0] to [-100, 100]
            normalized_score = round(avg_compound * 100, 1)
            
            return {
                "score": normalized_score,
                "headline": best_headline if best_headline else "Analyzed recent exchange filings."
            }
            
        except Exception as e:
            print(f"NLP Error for {ticker}: {e}")
            return {"score": 0, "headline": "Failed to fetch NLP data."}

nlp_engine = FinancialSentimentAnalyzer()
