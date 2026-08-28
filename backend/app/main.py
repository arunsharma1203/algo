from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from app.tasks.hoarder import hoard_intraday_data

# Set up logging for scheduler
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize APScheduler
scheduler = BackgroundScheduler()
# Run hoarding job every day at 16:00 IST (approximate if server is in IST)
scheduler.add_job(hoard_intraday_data, 'cron', hour=16, minute=0)
scheduler.start()
from fastapi.middleware.cors import CORSMiddleware
from app.api.backtest import router as backtest_router
from app.api.market import router as market_router
from app.api.intraday_ml import router as intraday_ml_router
from app.api.ml_lab import router as ml_lab_router
from app.api.broker import router as broker_router
from app.api.swing_ml import router as swing_router
from app.api.ml_backtest import router as ml_backtest_router

app = FastAPI(title="Swing Trading AI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(backtest_router, prefix="/api/backtest", tags=["backtest"])
app.include_router(market_router, prefix="/api/market", tags=["market"])
app.include_router(intraday_ml_router, prefix="/api/ml", tags=["ml"])
app.include_router(ml_lab_router, prefix="/api/ml", tags=["ml_lab"])
app.include_router(broker_router, prefix="/api/broker", tags=["broker"])
app.include_router(swing_router, prefix="/api/ml", tags=["swing_ml"])
app.include_router(ml_backtest_router, prefix="/api/ml", tags=["ml_backtest"])

@app.get("/")
def read_root():
    return {"message": "Swing Trading AI API is running"}

from fastapi.responses import StreamingResponse
from app.tasks.hoarder import hoard_intraday_data_stream

from pydantic import BaseModel

class HoarderRequest(BaseModel):
    data_source: str = 'yfinance'
    api_key: str = ''

@app.post("/api/hoarder/trigger")
def trigger_hoarder(req: HoarderRequest):
    return StreamingResponse(hoard_intraday_data_stream(req.data_source, req.api_key), media_type="text/event-stream")
