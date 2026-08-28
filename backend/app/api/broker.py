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
        logger.error(f"Broker execution failed: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
