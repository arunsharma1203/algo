from typing import Dict, List, Any
from app.api.intraday_ml import INDIAN_STOCK_UNIVERSE

# 1. Production Live Universe (52 Large-Cap & Liquid Momentum Equities)
# PRESERVED UNTOUCHED FOR LIVE SCANNING
LIVE_UNIVERSE = list(INDIAN_STOCK_UNIVERSE)

# 2. Benchmark 5 Universe (Production Training Benchmark)
BENCHMARK_5_UNIVERSE = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS"
]

# 3. Expanded Research Universe (100 Liquid Equities with 10-Year Historical Depth)
RESEARCH_100_UNIVERSE = list(set(INDIAN_STOCK_UNIVERSE + [
    "ASIANPAINT.NS", "AXISBANK.NS", "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS",
    "BHARTIARTL.NS", "BPCL.NS", "BRITANNIA.NS", "CIPLA.NS", "COALINDIA.NS",
    "DIVISLAB.NS", "DRREDDY.NS", "EICHERMOT.NS", "GRASIM.NS", "HCLTECH.NS",
    "HDFCBANK.NS", "HDFCLIFE.NS", "HEROMOTOCO.NS", "HINDALCO.NS", "HINDUNILVR.NS",
    "ICICIBANK.NS", "INDUSINDBK.NS", "INFY.NS", "ITC.NS", "JSWSTEEL.NS",
    "KOTAKBANK.NS", "LT.NS", "M&M.NS", "MARUTI.NS", "NESTLEIND.NS",
    "NTPC.NS", "ONGC.NS", "POWERGRID.NS", "RELIANCE.NS", "SBILIFE.NS",
    "SBIN.NS", "SUNPHARMA.NS", "TATACONSUM.NS", "TATAMOTORS.NS", "TATASTEEL.NS",
    "TCS.NS", "TECHM.NS", "TITAN.NS", "ULTRACEMCO.NS", "WIPRO.NS",
    "AMBUJACEM.NS", "AUROPHARMA.NS", "BANKBARODA.NS", "BOSCHLTD.NS", "CANBK.NS",
    "CHOLAFIN.NS", "COLPAL.NS", "DABUR.NS", "DLF.NS", "GAIL.NS",
    "GODREJCP.NS", "HAVELLS.NS", "ICICIGI.NS", "ICICIPRULI.NS", "INDIGO.NS",
    "JINDALSTEL.NS", "LICI.NS", "LUPIN.NS", "MARICO.NS", "MUTHOOTFIN.NS",
    "NAUKRI.NS", "PIDILITIND.NS", "PFC.NS", "PNB.NS", "RECLTD.NS",
    "SIEMENS.NS", "SRF.NS", "TORNTPHARM.NS", "TVSMOTOR.NS", "VEDL.NS",
    "VOLTAS.NS", "ZYDUSLIFE.NS", "ABB.NS", "ADANIPOWER.NS", "BEL.NS",
    "HAL.NS", "JIOFIN.NS", "TRENT.NS", "VBL.NS", "ZOMATO.NS"
]))

# 4. Preset Definitions
UNIVERSE_PRESETS: Dict[str, Dict[str, Any]] = {
    "BENCHMARK_5": {
        "name": "Production Training Benchmark (5 Heavyweights)",
        "tickers": BENCHMARK_5_UNIVERSE,
        "description": "The 5 primary sector heavyweights used for automated Champion model training and Optuna tuning.",
        "survivorship_bias": "MODERATE — Uses current top 5 market leaders."
    },
    "LIVE_52": {
        "name": "Live Scanner Universe (52 Stocks)",
        "tickers": LIVE_UNIVERSE,
        "description": "The 52 high-liquidity stocks evaluated by the real-time Intraday and Swing ML scanners.",
        "survivorship_bias": "MODERATE — Evaluates surviving 2026 constituents back into 2021."
    },
    "RESEARCH_100": {
        "name": "Expanded Research Universe (100 Stocks)",
        "tickers": RESEARCH_100_UNIVERSE,
        "description": "Broad 100-stock liquid universe for 10-year multi-asset research and portfolio walk-forward testing.",
        "survivorship_bias": "MODERATE-HIGH — Retrospective selection of current 100 liquid stocks. Delisted historical members are not included."
    }
}

def get_universe(name: str = "BENCHMARK_5") -> Dict[str, Any]:
    """Retrieves universe configuration and survivorship bias disclosures."""
    clean_name = name.strip().upper()
    if clean_name in UNIVERSE_PRESETS:
        return UNIVERSE_PRESETS[clean_name]
    return UNIVERSE_PRESETS["BENCHMARK_5"]

