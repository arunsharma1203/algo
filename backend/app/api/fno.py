from fastapi import APIRouter
from app.analytics.fno_engine import fetch_nse_option_chain

router = APIRouter()

@router.get("/option-chain/{symbol}")
def get_option_chain(symbol: str = "NIFTY"):
    """
    Returns live or cached NSE Option Chain metrics:
    PCR, Max Pain Strike, Top 3 Call Walls, Top 3 Put Walls, and Buildup status.
    """
    return fetch_nse_option_chain(symbol)

@router.get("/market-pcr")
def get_market_pcr():
    """Returns quick macro market PCR for NIFTY and BANKNIFTY."""
    nifty_fno = fetch_nse_option_chain("NIFTY")
    banknifty_fno = fetch_nse_option_chain("BANKNIFTY")
    return {
        "nifty": {
            "pcr": nifty_fno.get("pcr"),
            "sentiment": nifty_fno.get("pcr_sentiment"),
            "max_pain": nifty_fno.get("max_pain"),
            "buildup": nifty_fno.get("buildup")
        },
        "banknifty": {
            "pcr": banknifty_fno.get("pcr"),
            "sentiment": banknifty_fno.get("pcr_sentiment"),
            "max_pain": banknifty_fno.get("max_pain"),
            "buildup": banknifty_fno.get("buildup")
        }
    }
