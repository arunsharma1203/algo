from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from app.tasks.hoarder import hoard_intraday_data

# Set up logging for scheduler
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Enable SQLite Write-Ahead Logging (WAL) for concurrent read/writes
def init_db_wal():
    import sqlite3
    try:
        conn = sqlite3.connect('market_data.db', timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.commit()
        conn.close()
        logger.info("SQLite WAL (Write-Ahead Logging) Mode Enabled.")
    except Exception as e:
        logger.warning(f"Could not enable WAL mode: {e}")

init_db_wal()

# Initialize APScheduler
scheduler = BackgroundScheduler()
# Run hoarding job every day at 16:00 IST (approximate if server is in IST)
scheduler.add_job(hoard_intraday_data, 'cron', hour=16, minute=0)

# 🤖 AUTONOMOUS BOT: Active Trade Manager
# Runs every 5 minutes to cross-reference open trades against all 4 ML models
try:
    from app.analytics.autonomous_bot import active_trade_tracker
    scheduler.add_job(active_trade_tracker, 'interval', minutes=5)
except Exception as e:
    logger.error(f"Failed to load autonomous bot: {e}")

# 🔄 WEEKLY RETRAINING PIPELINE
# Runs every Sunday at 23:00 to retrain models and apply Champion vs Challenger gate
try:
    from app.analytics.retrain_models import execute_retraining_pipeline
    scheduler.add_job(execute_retraining_pipeline, 'cron', day_of_week='sun', hour=23, minute=0)
except Exception as e:
    logger.error(f"Failed to schedule weekly retraining pipeline: {e}")

scheduler.start()
from fastapi.middleware.cors import CORSMiddleware
from app.api.backtest import router as backtest_router
from app.api.market import router as market_router
from app.api.intraday_ml import router as intraday_ml_router
from app.api.ml_lab import router as ml_lab_router
from app.api.broker import router as broker_router
from app.api.swing_ml import router as swing_router
from app.api.ml_backtest import router as ml_backtest_router
from app.api.settings import router as settings_router

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
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])

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
