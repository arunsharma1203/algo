import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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


def _fetch_live_exchange_and_rss_news(
    base_symbol: str,
    timeout_sec: float = 2.5,
    max_filing_age_days: float = 7.0,
) -> List[Dict[str, Any]]:
    """
    Fetches live corporate announcements from NSE India + Google News India Business RSS
    strictly within `max_filing_age_days` (default 7d; cached 15m; fails safely to `[]` if offline).
    Discards stale archival exchange rows (e.g. multi-year-old announcements) before RSS fallback check.
    """
    now_ep = time.time()
    now_dt = datetime.now()
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
                for row in data[:10]:
                    desc = str(row.get("desc") or "").strip()
                    att = str(row.get("attchmntText") or "").strip()
                    dt_raw = str(row.get("an_dt") or "").strip()
                    parsed_dt = None
                    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
                        try:
                            parsed_dt = datetime.strptime(dt_raw, fmt)
                            break
                        except Exception:
                            continue
                    # Enforce strict recency window so multi-month/year archival NSE rows are never scored as live news
                    if parsed_dt is None:
                        continue
                    age_d = (now_dt - parsed_dt).total_seconds() / 86400.0
                    if age_d > max_filing_age_days or age_d < -0.1:
                        continue
                    if desc or att:
                        items.append({
                            "title": f"{base_symbol}: {desc}",
                            "summary": att[:280],
                            "providerPublishTime": parsed_dt.timestamp(),
                            "source": "NSE_CORPORATE_FILING",
                        })
    except Exception:
        pass

    # 2. Google News India Business RSS Fallback / Supplement (`when:2d`) whenever recent NSE filings < 3
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
                            pub_ts = pdt.timestamp()
                        except Exception:
                            pub_ts = None
                    if pub_ts is None:
                        continue
                    rss_age_d = (now_ep - pub_ts) / 86400.0
                    if rss_age_d > max_filing_age_days or rss_age_d < -0.1:
                        continue
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


_FINBERT_SUBPROCESS_SCRIPT = """
import json, os, sys
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["OMP_NUM_THREADS"] = "1"
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

model_name = sys.argv[1]
try:
    tok = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
    mdl = AutoModelForSequenceClassification.from_pretrained(model_name, local_files_only=True)
except Exception:
    tok = AutoTokenizer.from_pretrained(model_name)
    mdl = AutoModelForSequenceClassification.from_pretrained(model_name)
mdl.eval()
dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
mdl.to(dev)
id2label = {i: mdl.config.id2label[i].lower() for i in range(len(mdl.config.id2label))}
sys.stdout.write(json.dumps({"ready": True, "device": str(dev)}) + "\\n")
sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    txt = str(req.get("text") or "")
    inputs = tok(txt, return_tensors="pt", truncation=True, max_length=512, padding=True)
    inputs = {k: v.to(dev) for k, v in inputs.items()}
    with torch.no_grad():
        probs = torch.nn.functional.softmax(mdl(**inputs).logits, dim=-1)[0].cpu().tolist()
    lmap = {id2label[i]: float(probs[i]) for i in range(len(probs))}
    pos = lmap.get("positive", 0.0)
    neg = lmap.get("negative", 0.0)
    neu = lmap.get("neutral", 0.0)
    sys.stdout.write(json.dumps({
        "neg": round(neg, 4),
        "neu": round(neu, 4),
        "pos": round(pos, 4),
        "compound": round(pos - neg, 4),
    }) + "\\n")
    sys.stdout.flush()
"""


class FinBERTAnalyzer:
    """
    Fault-Isolated FinBERT Neural Sentiment Analyzer (`ProsusAI/finbert`).
    Runs neural PyTorch inference inside a dedicated persistent child worker process so PyTorch's
    `libomp` / Metal runtime never collides with LightGBM/Qlib OpenMP inside the FastAPI server!
    Includes seamless defensive fallback to VADER for offline/stdlib test isolation.
    """

    def __init__(self, model_name: str = "ProsusAI/finbert"):
        self.model_name = model_name
        self._proc: Optional[subprocess.Popen] = None
        self._model = None  # Truthy sentinel once neural worker is active
        self._load_failed = False
        self._fallback_analyzer = None
        self._infer_lock = threading.RLock()
        self.lexicon = FINANCIAL_LEXICON_UPDATES.copy()

    def _ensure_worker(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        if self._load_failed:
            return False
        with self._infer_lock:
            if self._proc is not None and self._proc.poll() is None:
                return True
            if self._load_failed:
                return False
            try:
                # Verify torch & transformers exist in this Python environment before spawning
                import importlib.util
                if importlib.util.find_spec("torch") is None or importlib.util.find_spec("transformers") is None:
                    self._load_failed = True
                    return False

                env = os.environ.copy()
                env["TOKENIZERS_PARALLELISM"] = "false"
                env["OMP_NUM_THREADS"] = "1"
                proc = subprocess.Popen(
                    [sys.executable, "-B", "-c", _FINBERT_SUBPROCESS_SCRIPT, self.model_name],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    env=env,
                )
                ready_line = proc.stdout.readline().strip() if proc.stdout else ""
                info = json.loads(ready_line) if ready_line else {}
                if not info.get("ready"):
                    proc.kill()
                    raise RuntimeError("FinBERT worker did not report ready")
                self._proc = proc
                self._model = True
                logger.info(f"[FinBERT] Isolated neural worker initialized on {info.get('device')}.")
                return True
            except Exception as e:
                logger.warning(f"[FinBERT] Worker initialization fallback ({e}). Using VADER.")
                self._proc = None
                self._model = None
                self._load_failed = True
                return False

    def polarity_scores(self, text: str) -> Dict[str, float]:
        """
        Calculates polarity scores using isolated FinBERT deep neural classification.
        Output dictionary matches standard sentiment interface:
        {'neg': float, 'neu': float, 'pos': float, 'compound': float}
        """
        if self._ensure_worker():
            with self._infer_lock:
                try:
                    assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
                    self._proc.stdin.write(json.dumps({"text": str(text or "")[:1500]}) + "\n")
                    self._proc.stdin.flush()
                    out_line = self._proc.stdout.readline().strip()
                    if out_line:
                        return json.loads(out_line)
                except Exception as e:
                    logger.warning(f"[FinBERT] Worker pipe exception ({e}); falling back cleanly.")
                    try:
                        if self._proc is not None:
                            self._proc.kill()
                    except Exception:
                        pass
                    self._proc = None
                    self._model = None

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

    def analyze_ticker_news(
        self,
        ticker: str,
        as_of_timestamp: Optional[datetime] = None,
        max_age_days: float = 7.0,
        shock_max_age_days: float = 3.0,
    ) -> Dict[str, Any]:
        """
        Fetches news for a ticker and calculates recency-weighted, point-in-time sentiment using FinBERT.

        CRITICAL RULES:
          1. Future articles (`pub_dt > as_of_timestamp`) are strictly discarded (zero lookahead bias).
          2. Stale articles (`age > max_age_days`, default 7d) are strictly discarded (zero archival contamination).
          3. Emergency regulatory shock flags (`is_severe_negative_shock`) require fresh filings `<= shock_max_age_days` (default 72h).
        """
        if as_of_timestamp is None:
            as_of_timestamp = datetime.now()

        try:
            base_ticker = ticker.split('.')[0].strip().upper()
            eval_lag_days = abs((datetime.now() - as_of_timestamp).total_seconds()) / 86400.0
            news_items = _fetch_live_exchange_and_rss_news(base_ticker, max_filing_age_days=max_age_days) if eval_lag_days <= 3.0 else []

            if not news_items or len(news_items) == 0:
                return {
                    "score": 0.0,
                    "headline": "No recent exchange news available (last 7d).",
                    "status": "unavailable",
                    "articles_analyzed": 0,
                    "max_neg_prob": 0.0,
                    "regulatory_shock_keywords": [],
                    "is_severe_negative_shock": False,
                    "engine": "FinBERT (ProsusAI/finbert)"
                }

            weighted_compound_sum = 0.0
            weight_sum = 0.0
            best_headline = ""
            max_intensity = -1.0
            max_neg_prob = 0.0
            recent_shock_neg_prob = 0.0
            shock_hits_found: List[str] = []
            valid_news = 0

            for item in news_items:
                # 1. Point-in-time & staleness filter: require valid publication timestamp within [as_of - max_age_days, as_of]
                pub_time = item.get('providerPublishTime')
                if not pub_time:
                    continue
                try:
                    pub_dt = datetime.fromtimestamp(float(pub_time))
                except Exception:
                    continue
                if pub_dt > as_of_timestamp:
                    continue
                item_age_days = (as_of_timestamp - pub_dt).total_seconds() / 86400.0
                if item_age_days > max_age_days:
                    continue

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

                # Severe regulatory shock veto/exit applies strictly to fresh news (<= shock_max_age_days, default 72h)
                if item_age_days <= shock_max_age_days:
                    recent_shock_neg_prob = max(recent_shock_neg_prob, neg_p)
                    for kw in SEVERE_REGULATORY_SHOCK_KEYWORDS:
                        if kw in text_lc and kw not in shock_hits_found:
                            shock_hits_found.append(kw)

                recency_w = 1.0 if item_age_days <= 1.0 else (0.85 if item_age_days <= 3.0 else 0.60)
                weighted_compound_sum += float(scores['compound']) * recency_w
                weight_sum += recency_w
                intensity = abs(float(scores['compound'])) * recency_w
                if intensity > max_intensity:
                    max_intensity = intensity
                    best_headline = title

            if valid_news == 0 or weight_sum <= 0.0:
                return {
                    "score": 0.0,
                    "headline": "Recent filings reviewed (neutral/no relevant mentions in last 7d).",
                    "status": "neutral",
                    "articles_analyzed": 0,
                    "max_neg_prob": 0.0,
                    "regulatory_shock_keywords": [],
                    "is_severe_negative_shock": False,
                    "engine": "FinBERT (ProsusAI/finbert)"
                }

            avg_compound = weighted_compound_sum / weight_sum
            normalized_score = round(avg_compound * 100.0, 1)
            is_severe_shock = bool(recent_shock_neg_prob >= 0.85 and len(shock_hits_found) > 0)

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
