import logging
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

SEVERE_REGULATORY_SHOCK_KEYWORDS = (
    "sebi order",
    "sebi ban",
    "fraud",
    "forensic audit",
    "auditor resign",
    "default",
    "insolvency",
    "nclt",
    "usfda",
    "form 483",
    "import alert",
    "warning letter",
    "ed raid",
    "cbi",
    "income tax search",
    "plant fire",
    "suspension",
    "guidance cut",
    "profit drops",
    "net loss",
)

_TICKER_NEWS_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_NEWS_CACHE_TTL_SEC = 900.0  # 15-minute live cache per ticker
_NSE_COOKIE_LOCK = threading.Lock()
_NSE_SHARED_SESSION: Any = None
_NSE_COOKIE_INIT_TS: float = 0.0
_NSE_COOKIE_TTL_SEC = 600.0  # Reuse NSE session cookies for 10 minutes


def _get_shared_nse_session(headers: Dict[str, str], timeout_sec: float = 2.0) -> Any:
    global _NSE_SHARED_SESSION, _NSE_COOKIE_INIT_TS
    import requests

    now_ep = time.time()
    with _NSE_COOKIE_LOCK:
        if _NSE_SHARED_SESSION is None or (now_ep - _NSE_COOKIE_INIT_TS) > _NSE_COOKIE_TTL_SEC:
            s = requests.Session()
            try:
                s.get("https://www.nseindia.com", headers=headers, timeout=timeout_sec)
            except Exception:
                pass
            _NSE_SHARED_SESSION = s
            _NSE_COOKIE_INIT_TS = now_ep
        return _NSE_SHARED_SESSION


def _fetch_live_exchange_and_rss_news(base_symbol: str, timeout_sec: float = 2.5) -> List[Dict[str, Any]]:
    """
    Fetches live corporate announcements from NSE India + Google News India Business RSS
    strictly for point-in-time live risk monitoring (cached 15m; fails safely to `[]` if offline).
    """
    now_ep = time.time()
    cached = _TICKER_NEWS_CACHE.get(base_symbol)
    if cached and (now_ep - cached[0]) < _NEWS_CACHE_TTL_SEC:
        return cached[1]

    items: List[Dict[str, Any]] = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, text/xml, application/rss+xml, */*",
    }

    # 1. Official NSE Corporate Announcements (Tier-1 Exchange Filings via shared cookie session)
    try:
        s = _get_shared_nse_session(headers, timeout_sec=2.0)
        nse_url = f"https://www.nseindia.com/api/corporate-announcements?index=equities&symbol={urllib.parse.quote(base_symbol)}"
        r_nse = s.get(nse_url, headers={**headers, "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements"}, timeout=timeout_sec)
        if r_nse.status_code == 200:
            data = r_nse.json()
            if isinstance(data, list):
                for row in data[:8]:
                    desc = str(row.get("desc") or "").strip()
                    att = str(row.get("attchmntText") or "").strip()
                    dt_raw = str(row.get("an_dt") or "").strip()
                    pub_ts = None
                    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
                        try:
                            pub_ts = datetime.strptime(dt_raw, fmt).timestamp()
                            break
                        except Exception:
                            continue
                    if desc or att:
                        items.append({
                            "title": f"{base_symbol}: {desc}",
                            "summary": att[:280],
                            "providerPublishTime": pub_ts,
                            "source": "NSE_CORPORATE_FILING",
                        })
    except Exception:
        pass

    # 2. Google News India Business RSS Fallback / Supplement (`when:2d`)
    if len(items) < 3:
        try:
            import requests

            q = urllib.parse.quote(f'"{base_symbol}" NSE stock when:2d')
            rss_url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
            r_rss = requests.get(rss_url, headers=headers, timeout=timeout_sec)
            if r_rss.status_code == 200 and r_rss.text:
                root = ET.fromstring(r_rss.text)
                for node in root.findall(".//item")[:8]:
                    title = (node.findtext("title") or "").strip()
                    pub_s = (node.findtext("pubDate") or "").strip()
                    pub_ts = None
                    if pub_s:
                        try:
                            pdt = parsedate_to_datetime(pub_s)
                            if pdt.tzinfo is not None:
                                pdt = pdt.astimezone(timezone.utc).replace(tzinfo=None)
                            pub_ts = pdt.timestamp()
                        except Exception:
                            pub_ts = None
                    if title:
                        items.append({
                            "title": title,
                            "summary": "",
                            "providerPublishTime": pub_ts,
                            "source": "GOOGLE_NEWS_IN_RSS",
                        })
        except Exception:
            pass

    _TICKER_NEWS_CACHE[base_symbol] = (now_ep, items)
    return items

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


class FinBERTAnalyzer:
    """
    FinBERT Neural Sentiment Analyzer (ProsusAI/finbert).
    Processes financial texts into probability distributions over positive, negative, and neutral classes.
    Includes seamless lazy initialization and defensive fallback to VADER for offline/test isolation.
    """

    def __init__(self, model_name: str = "ProsusAI/finbert"):
        self.model_name = model_name
        self._tokenizer = None
        self._model = None
        self._device = None
        self._load_failed = False
        self._fallback_analyzer = None
        self.lexicon = FINANCIAL_LEXICON_UPDATES.copy()

    def _load_model(self):
        if self._model is not None or self._load_failed:
            return
        try:
            import torch
            from transformers import AutoTokenizer, AutoModelForSequenceClassification

            logger.info(f"[FinBERT] Loading model {self.model_name}...")
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            self._model = AutoModelForSequenceClassification.from_pretrained(self.model_name)
            self._model.eval()

            # Assign device (MPS on Apple Silicon, CUDA, or CPU)
            if torch.cuda.is_available():
                self._device = torch.device("cuda")
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                self._device = torch.device("mps")
            else:
                self._device = torch.device("cpu")

            self._model.to(self._device)
            logger.info(f"[FinBERT] Successfully initialized on device: {self._device}")
        except Exception as e:
            logger.warning(f"[FinBERT] Initialization error ({e}). Engaging defensive fallback.")
            self._load_failed = True

    def polarity_scores(self, text: str) -> Dict[str, float]:
        """
        Calculates polarity scores using FinBERT deep neural classification.
        Output dictionary matches standard sentiment interface:
        {'neg': float, 'neu': float, 'pos': float, 'compound': float}
        where compound = pos - neg (-1.0 to +1.0).
        """
        self._load_model()

        if self._model is not None and self._tokenizer is not None:
            try:
                import torch

                inputs = self._tokenizer(
                    text,
                    return_tensors="pt",
                    truncation=True,
                    max_length=512,
                    padding=True
                )
                if self._device is not None:
                    inputs = {k: v.to(self._device) for k, v in inputs.items()}

                with torch.no_grad():
                    outputs = self._model(**inputs)
                    probs = torch.nn.functional.softmax(outputs.logits, dim=-1)[0].cpu().numpy()

                id2label = self._model.config.id2label
                label2prob = {id2label[i].lower(): float(probs[i]) for i in range(len(probs))}

                pos = float(label2prob.get("positive", 0.0))
                neg = float(label2prob.get("negative", 0.0))
                neu = float(label2prob.get("neutral", 0.0))
                compound = pos - neg

                return {
                    "neg": round(neg, 4),
                    "neu": round(neu, 4),
                    "pos": round(pos, 4),
                    "compound": round(compound, 4)
                }
            except Exception as e:
                logger.warning(f"[FinBERT] Inference exception: {e}")

        # Defensive fallback to VADER
        if self._fallback_analyzer is None:
            try:
                from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
                self._fallback_analyzer = SentimentIntensityAnalyzer()
                self._fallback_analyzer.lexicon.update(self.lexicon)
            except Exception as fe:
                logger.error(f"[FinBERT] Fallback analyzer creation failed: {fe}")
                return {"neg": 0.0, "neu": 1.0, "pos": 0.0, "compound": 0.0}

        return self._fallback_analyzer.polarity_scores(text)


class FinancialSentimentAnalyzer:
    """
    Production Financial Sentiment Engine powered by FinBERT (ProsusAI/finbert).
    Processes live news filings with financial domain sentiment classification
    and strict point-in-time timestamp filtering to prevent lookahead bias.
    """

    def __init__(self):
        self.analyzer = FinBERTAnalyzer()
        self.engine_name = "FinBERT Financial Sentiment Engine"

    def analyze_ticker_news(self, ticker: str, as_of_timestamp: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Fetches news for a ticker and calculates point-in-time sentiment using FinBERT.

        CRITICAL RULE:
        Articles published AFTER as_of_timestamp are strictly filtered out to prevent future-news leakage.
        """
        if as_of_timestamp is None:
            as_of_timestamp = datetime.now()

        try:
            base_ticker = ticker.split('.')[0].strip().upper()
            # Strictly point-in-time: only query live feeds when evaluating current/recent timestamps (<= 3 days);
            # never leak current live news onto historical backtest timestamps!
            age_days = abs((datetime.now() - as_of_timestamp).total_seconds()) / 86400.0
            news_items = _fetch_live_exchange_and_rss_news(base_ticker) if age_days <= 3.0 else []

            if not news_items or len(news_items) == 0:
                return {
                    "score": 0.0,
                    "headline": "No recent exchange news available.",
                    "status": "unavailable",
                    "articles_analyzed": 0,
                    "max_neg_prob": 0.0,
                    "regulatory_shock_keywords": [],
                    "is_severe_negative_shock": False,
                    "engine": "FinBERT (ProsusAI/finbert)"
                }

            total_compound = 0.0
            best_headline = ""
            max_intensity = -1.0
            max_neg_prob = 0.0
            shock_hits_found: List[str] = []
            valid_news = 0

            for item in news_items:
                # 1. Point-in-time filter: check publication time
                pub_time = item.get('providerPublishTime')
                if pub_time:
                    try:
                        pub_dt = datetime.fromtimestamp(pub_time)
                        if pub_dt > as_of_timestamp:
                            continue
                    except Exception:
                        pass

                title = item.get('title', '')
                if not title and 'content' in item:
                    title = item['content'].get('title', '')

                summary = item.get('summary', '')
                if not summary and 'content' in item:
                    summary = item['content'].get('summary', '')

                text = f"{title}. {summary}".strip()
                text_lc = text.lower()

                # Entity relevance filter (NSE corporate filing items already scoped to base_ticker)
                if item.get("source") != "NSE_CORPORATE_FILING" and base_ticker not in text.upper():
                    continue

                valid_news += 1
                scores = self.analyzer.polarity_scores(text)
                neg_p = float(scores.get("neg", 0.0))
                max_neg_prob = max(max_neg_prob, neg_p)
                for kw in SEVERE_REGULATORY_SHOCK_KEYWORDS:
                    if kw in text_lc and kw not in shock_hits_found:
                        shock_hits_found.append(kw)

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
                    "articles_analyzed": 0,
                    "max_neg_prob": 0.0,
                    "regulatory_shock_keywords": [],
                    "is_severe_negative_shock": False,
                    "engine": "FinBERT (ProsusAI/finbert)"
                }

            avg_compound = total_compound / valid_news
            normalized_score = round(avg_compound * 100.0, 1)
            is_severe_shock = bool(max_neg_prob >= 0.85 and len(shock_hits_found) > 0)

            return {
                "score": normalized_score,
                "headline": best_headline or "Analyzed verified filings.",
                "status": "active",
                "articles_analyzed": valid_news,
                "max_neg_prob": round(max_neg_prob, 4),
                "regulatory_shock_keywords": shock_hits_found,
                "is_severe_negative_shock": is_severe_shock,
                "engine": "FinBERT (ProsusAI/finbert)"
            }

        except Exception as e:
            logger.warning(f"Sentiment analysis error for {ticker}: {e}")
            return {
                "score": 0.0,
                "headline": "Sentiment feed offline.",
                "status": "unavailable",
                "articles_analyzed": 0,
                "max_neg_prob": 0.0,
                "regulatory_shock_keywords": [],
                "is_severe_negative_shock": False,
                "engine": "FinBERT (ProsusAI/finbert)"
            }


nlp_engine = FinancialSentimentAnalyzer()


def get_nlp_analyzer() -> FinancialSentimentAnalyzer:
    """Returns the process-wide FinBERT + NSE Corporate Announcements singleton."""
    return nlp_engine
