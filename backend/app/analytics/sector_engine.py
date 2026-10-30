"""
Real-Time Sector Momentum & Alignment Engine.
Maps Indian equity symbols to authoritative NSE sector indices and validates
trade direction against institutional sector momentum using Upstox V3 live quotes.
Zero forward bias, zero dummy data, single-call bulk retrieval.
"""

import json
import logging
import os
from typing import Dict, Any, Optional, Tuple, List

from app.data.upstox_provider import fetch_upstox_sector_quotes, NSE_SECTOR_KEYS

logger = logging.getLogger(__name__)

# Primary Industry to NSE Sector Index Mapping
INDUSTRY_TO_SECTOR_MAP: Dict[str, str] = {
    "Fast Moving Consumer Goods": "NIFTY FMCG",
    "Automobile and Auto Components": "NIFTY AUTO",
    "Financial Services": "NIFTY BANK",
    "Banks": "NIFTY BANK",
    "Information Technology": "NIFTY IT",
    "Metals & Mining": "NIFTY METAL",
    "Healthcare": "NIFTY PHARMA",
    "Pharmaceuticals": "NIFTY PHARMA",
    "Realty": "NIFTY REALTY",
    "Real Estate": "NIFTY REALTY",
    "Oil Gas & Consumable Fuels": "NIFTY ENERGY",
    "Power": "NIFTY ENERGY",
    "Consumer Services": "NIFTY FMCG",
    "Consumer Durables": "NIFTY AUTO",
    "Telecommunication": "NIFTY IT",
    "Media, Entertainment & Publication": "NIFTY IT",
    "Capital Goods": "NIFTY METAL",
    "Construction Materials": "NIFTY METAL",
    "Chemicals": "NIFTY COMMODITIES"
}

# Explicit High-Conviction Sector Anchors for Large & Mid Cap Universe
EXPLICIT_SECTOR_OVERRIDES: Dict[str, str] = {
    # Banking & Financials
    "HDFCBANK": "NIFTY BANK",
    "ICICIBANK": "NIFTY BANK",
    "SBIN": "NIFTY BANK",
    "KOTAKBANK": "NIFTY BANK",
    "AXISBANK": "NIFTY BANK",
    "RBLBANK": "NIFTY BANK",
    "INDUSINDBK": "NIFTY BANK",
    "BANKBARODA": "NIFTY BANK",
    "PNB": "NIFTY BANK",
    "FEDERALBNK": "NIFTY BANK",
    "IDFCFIRSTB": "NIFTY BANK",
    "BAJFINANCE": "NIFTY BANK",
    "BAJAJFINSV": "NIFTY BANK",
    "PFC": "NIFTY BANK",
    "RECLTD": "NIFTY BANK",

    # IT & Software
    "TCS": "NIFTY IT",
    "INFY": "NIFTY IT",
    "HCLTECH": "NIFTY IT",
    "WIPRO": "NIFTY IT",
    "TECHM": "NIFTY IT",
    "LTIM": "NIFTY IT",
    "PERSISTENT": "NIFTY IT",
    "COFORGE": "NIFTY IT",
    "MPHASIS": "NIFTY IT",
    "LTTS": "NIFTY IT",

    # Auto & Ancillaries
    "MARUTI": "NIFTY AUTO",
    "TATAMOTORS": "NIFTY AUTO",
    "M&M": "NIFTY AUTO",
    "BAJAJ-AUTO": "NIFTY AUTO",
    "HEROMOTOCO": "NIFTY AUTO",
    "EICHERMOT": "NIFTY AUTO",
    "BHARATFORG": "NIFTY AUTO",
    "MOTHERSON": "NIFTY AUTO",
    "BOSCHLTD": "NIFTY AUTO",
    "TVSMOTOR": "NIFTY AUTO",

    # FMCG & Consumer
    "ITC": "NIFTY FMCG",
    "HINDUNILVR": "NIFTY FMCG",
    "NESTLEIND": "NIFTY FMCG",
    "BRITANNIA": "NIFTY FMCG",
    "TATACONSUM": "NIFTY FMCG",
    "EMAMILTD": "NIFTY FMCG",
    "DABUR": "NIFTY FMCG",
    "MARICO": "NIFTY FMCG",
    "COLPAL": "NIFTY FMCG",
    "GODREJCP": "NIFTY FMCG",
    "VBL": "NIFTY FMCG",

    # Metals & Mining
    "TATASTEEL": "NIFTY METAL",
    "JSWSTEEL": "NIFTY METAL",
    "HINDALCO": "NIFTY METAL",
    "JINDALSTEL": "NIFTY METAL",
    "VEDL": "NIFTY METAL",
    "COALINDIA": "NIFTY METAL",
    "NMDC": "NIFTY METAL",
    "SAIL": "NIFTY METAL",

    # Pharma & Healthcare
    "SUNPHARMA": "NIFTY PHARMA",
    "DRREDDY": "NIFTY PHARMA",
    "CIPLA": "NIFTY PHARMA",
    "DIVISLAB": "NIFTY PHARMA",
    "LUPIN": "NIFTY PHARMA",
    "APOLLOHOSP": "NIFTY PHARMA",
    "AUROPHARMA": "NIFTY PHARMA",
    "ZYDUSLIFE": "NIFTY PHARMA",
    "TORNTPHARM": "NIFTY PHARMA",

    # Energy & Utilities
    "RELIANCE": "NIFTY ENERGY",
    "ONGC": "NIFTY ENERGY",
    "BPCL": "NIFTY ENERGY",
    "IOC": "NIFTY ENERGY",
    "NTPC": "NIFTY ENERGY",
    "POWERGRID": "NIFTY ENERGY",
    "GAIL": "NIFTY ENERGY",
    "ADANIGREEN": "NIFTY ENERGY",
    "TATAPOWER": "NIFTY ENERGY",

    # Realty & Infrastructure
    "DLF": "NIFTY REALTY",
    "GODREJPROP": "NIFTY REALTY",
    "OBEROIRLTY": "NIFTY REALTY",
    "PRESTIGE": "NIFTY REALTY",
    "LODHA": "NIFTY REALTY",
    "PHOENIXLTD": "NIFTY REALTY"
}

class SectorEngine:
    _mapping_cache: Optional[Dict[str, str]] = None

    @classmethod
    def _load_mapping(cls) -> Dict[str, str]:
        if cls._mapping_cache is not None:
            return cls._mapping_cache

        json_path = os.path.join(os.path.dirname(__file__), "..", "data", "nse_sector_mapping.json")
        loaded = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                logger.info(f"[SectorEngine] Loaded {len(loaded)} stock industry mappings from {json_path}")
            except Exception as e:
                logger.warning(f"[SectorEngine] Failed to load {json_path}: {e}")

        cls._mapping_cache = loaded
        return cls._mapping_cache

    @classmethod
    def get_stock_sector(cls, symbol: str) -> Optional[str]:
        """
        Resolves an equity ticker to its primary NSE Sector Index (e.g. 'ITC' -> 'NIFTY FMCG').
        Checks explicit overrides first, then official NSE industry classification.
        """
        clean = symbol.replace('.NS', '').replace('.BO', '').strip().upper()
        if clean in EXPLICIT_SECTOR_OVERRIDES:
            return EXPLICIT_SECTOR_OVERRIDES[clean]

        mapping = cls._load_mapping()
        ind = mapping.get(clean) or mapping.get(symbol)
        if ind and ind in INDUSTRY_TO_SECTOR_MAP:
            return INDUSTRY_TO_SECTOR_MAP[ind]

        # Broad keyword match fallback
        if ind:
            ind_lower = ind.lower()
            if "bank" in ind_lower or "finan" in ind_lower:
                return "NIFTY BANK"
            elif "tech" in ind_lower or "software" in ind_lower:
                return "NIFTY IT"
            elif "auto" in ind_lower or "motor" in ind_lower:
                return "NIFTY AUTO"
            elif "fmcg" in ind_lower or "consumer" in ind_lower or "food" in ind_lower:
                return "NIFTY FMCG"
            elif "metal" in ind_lower or "steel" in ind_lower or "mining" in ind_lower:
                return "NIFTY METAL"
            elif "pharma" in ind_lower or "health" in ind_lower:
                return "NIFTY PHARMA"
            elif "realt" in ind_lower or "estate" in ind_lower:
                return "NIFTY REALTY"
            elif "oil" in ind_lower or "gas" in ind_lower or "power" in ind_lower or "energy" in ind_lower:
                return "NIFTY ENERGY"

        return None

    @classmethod
    def get_sector_leaderboard(cls, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Retrieves real-time sector quotes and ranks sectors by performance from open.
        Returns leaders (top 3 gaining), laggards (bottom 3 losing), and all sectors.
        """
        raw_quotes = fetch_upstox_sector_quotes(force_refresh=force_refresh)
        valid = [v for v in raw_quotes.values() if v.get("pct_from_open") is not None]
        valid.sort(key=lambda x: x["pct_from_open"], reverse=True)

        leaders = [s["sector"] for s in valid if s["pct_from_open"] > 0][:3]
        laggards = [s["sector"] for s in valid if s["pct_from_open"] < 0][-3:]

        return {
            "sectors": raw_quotes,
            "leaders": leaders,
            "laggards": laggards,
            "ranked_list": valid
        }

    @classmethod
    def is_sector_aligned(
        cls,
        symbol: str,
        direction: str,
        leaderboard: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates whether a proposed trade direction aligns with its parent sector's live momentum.
        
        Rules:
        - Blocks SELL if sector is a top gainer or up > +0.25% from open.
        - Blocks BUY if sector is a top loser or down < -0.25% from open.
        
        Returns:
            (is_aligned: bool, reason: str, telemetry: Dict[str, Any])
        """
        sector = cls.get_stock_sector(symbol)
        telemetry: Dict[str, Any] = {
            "stock": symbol,
            "sector": sector,
            "sector_pct_from_open": 0.0,
            "sector_trend": "NEUTRAL"
        }

        if not sector:
            return True, f"Sector unclassified for {symbol} (Passed)", telemetry

        if leaderboard is None:
            leaderboard = cls.get_sector_leaderboard()

        sec_data = leaderboard.get("sectors", {}).get(sector)
        if not sec_data:
            return True, f"Sector quotes unavailable for {sector} (Passed)", telemetry

        pct_open = float(sec_data.get("pct_from_open", 0.0))
        trend = sec_data.get("trend", "NEUTRAL")
        leaders = leaderboard.get("leaders", [])
        laggards = leaderboard.get("laggards", [])

        telemetry["sector_pct_from_open"] = pct_open
        telemetry["sector_trend"] = trend
        telemetry["is_leader"] = sector in leaders
        telemetry["is_laggard"] = sector in laggards

        dir_norm = str(direction).upper().strip()
        is_short = dir_norm in ("SELL", "BEARISH", "SHORT")
        is_long = dir_norm in ("BUY", "BULLISH", "LONG")

        if is_short:
            # Suppress shorting strong sectors
            if sector in leaders or pct_open >= 0.25:
                return (
                    False,
                    f"SECTOR_COUNTER_TREND: {symbol} in {sector} which is surging (+{pct_open:.2f}% from open). Counter-trend short blocked.",
                    telemetry
                )
        elif is_long:
            # Suppress buying collapsing sectors
            if sector in laggards or pct_open <= -0.25:
                return (
                    False,
                    f"SECTOR_COUNTER_TREND: {symbol} in {sector} which is falling ({pct_open:.2f}% from open). Counter-trend long blocked.",
                    telemetry
                )

        return True, f"Aligned with {sector} ({pct_open:+.2f}% from open)", telemetry

