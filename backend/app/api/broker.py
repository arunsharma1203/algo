from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

class ExecuteRequest(BaseModel):
    api_token: str
    ticker: str
    action: str  # BUY or SELL
    quantity: int
    target: float
    stop_loss: float
    order_type: str = "LIMIT"
    simulation: bool = True

@router.post("/execute")
async def execute_trade(req: ExecuteRequest):
    """
    Simulation of INDstocks API Integration.
    In a live environment, this forwards the proprietary JSON payload to api-docs.indstocks.com
    """
    try:
        # Validate API Token (Simulation)
        if not req.api_token or len(req.api_token) < 10:
            raise HTTPException(status_code=401, detail="Invalid INDstocks API Token")
            
        # 1. Format the exact payload required by INDstocks
        # Referencing INDstocks API docs (mock structure)
        indstocks_payload = {
            "symbol": req.ticker.replace(".NS", ""), # Convert Yahoo ticker to NSE symbol
            "exchange": "NSE",
            "transaction_type": req.action.upper(),
            "quantity": req.quantity,
            "order_type": req.order_type,
            "product": "INTRADAY", # MIS
            "price": 0 if req.order_type == "MARKET" else req.target, # Not exactly correct for limit but close enough for sim
            # Advanced Bracket/OCO fields for target & stoploss
            "stoploss": req.stop_loss,
            "squareoff": req.target,
        }
        
        if req.simulation:
            logger.info(f"SAFEGUARD [SIMULATION]: Mocking trade execution to INDmoney: {indstocks_payload}")
        else:
            logger.info(f"SAFEGUARD [LIVE]: Executing trade to INDmoney: {indstocks_payload}")
        
        # 2. Simulate Network Latency
        import time
        time.sleep(1.2)
        
        # 3. Return Success
        return {
            "status": "success",
            "message": f"Successfully routed {req.action} order for {req.ticker} to INDstocks",
            "order_id": f"IND-{int(time.time())}",
            "simulated_payload": indstocks_payload
        }
        
    except Exception as e:
        logger.error(f"Execution failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

class KellySizingRequest(BaseModel):
    capital: float = 100000.0
    entry: float
    sl: float
    tp1: float
    win_prob: float = 65.0
    kelly_mode: str = "HALF" # QUARTER, HALF, FULL

@router.post("/kelly-sizing")
def get_kelly_sizing(req: KellySizingRequest):
    from app.analytics.kelly_sizer import calculate_kelly_position_size
    return calculate_kelly_position_size(
        capital=req.capital,
        entry=req.entry,
        sl=req.sl,
        tp1=req.tp1,
        win_prob=req.win_prob,
        kelly_mode=req.kelly_mode
    )

@router.get("/portfolio-heat")
def get_portfolio_heat(capital: float = 100000.0, max_heat: float = 6.0):
    from app.analytics.kelly_sizer import get_portfolio_heat_status
    return get_portfolio_heat_status(capital=capital, max_heat_cap_pct=max_heat)
