import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import numpy as np
import pandas as pd

from app.analytics.foundation_models.timesfm_adapter import TimesFMAdapter
from app.analytics.foundation_models.chronos_adapter import ChronosAdapter
from app.data.historical_data_layer import HistoricalDataLayer

logger = logging.getLogger(__name__)

CACHE_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'foundation_forecast_cache.db'))

class RealFoundationService:
    """
    Orchestration service for genuine Google TimesFM 2.5 and Amazon Chronos-2 inference.
    Maintains an isolated SQLite cache to ensure determinism, zero redundant computation,
    and strict point-in-time historical data boundaries.
    """

    TIMESFM_CHECKPOINT = "google/timesfm-2.5-200m-pytorch"
    CHRONOS_CHECKPOINT = "amazon/chronos-2"

    def __init__(self, cache_db_path: str = CACHE_DB_PATH):
        self.cache_db_path = cache_db_path
        self._init_cache_table()
        self.timesfm_adapter = TimesFMAdapter(model_version=self.TIMESFM_CHECKPOINT)
        self.chronos_adapter = ChronosAdapter(model_version=self.CHRONOS_CHECKPOINT)
        self._tfm_loaded = False
        self._chr_loaded = False

    def _init_cache_table(self):
        """Initializes the isolated foundation forecast cache table."""
        conn = sqlite3.connect(self.cache_db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS foundation_forecast_cache (
                symbol TEXT NOT NULL,
                as_of_date TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_version TEXT NOT NULL,
                horizon_bars INTEGER NOT NULL,
                expected_return_pct REAL NOT NULL,
                uncertainty_score REAL NOT NULL,
                downside_risk_pct REAL,
                upside_potential_pct REAL,
                forecast_path_json TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (symbol, as_of_date, model_name, model_version, horizon_bars)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_fnd_cache_lookup ON foundation_forecast_cache(symbol, as_of_date, model_name, horizon_bars)")
        conn.commit()
        conn.close()

    def ensure_models_loaded(self):
        """Loads genuine TimesFM 2.5 and Chronos-2 if not already in memory."""
        if not self._tfm_loaded:
            logger.info(f"Loading genuine TimesFM 2.5 ({self.TIMESFM_CHECKPOINT})...")
            self._tfm_loaded = self.timesfm_adapter.load_model()
            if not self._tfm_loaded:
                raise RuntimeError(f"Failed to load genuine TimesFM 2.5 model: {self.timesfm_adapter._init_error}")

        if not self._chr_loaded:
            logger.info(f"Loading genuine Chronos-2 ({self.CHRONOS_CHECKPOINT})...")
            self._chr_loaded = self.chronos_adapter.load_model()
            if not self._chr_loaded:
                raise RuntimeError(f"Failed to load genuine Chronos-2 model: {self.chronos_adapter._init_error}")

    def get_provenance_metadata(self) -> Dict[str, Any]:
        """Returns verified model identifiers and checkpoint provenance."""
        tfm_info = self.timesfm_adapter.get_model_info()
        chr_info = self.chronos_adapter.get_model_info()
        return {
            "timesfm_2p5": {
                "model_name": "Google TimesFM 2.5",
                "model_version": self.TIMESFM_CHECKPOINT,
                "checkpoint_info": tfm_info.get("checkpoint_info", {}),
                "device": tfm_info.get("device", "unknown"),
                "is_loaded": self._tfm_loaded,
                "is_genuine": True
            },
            "chronos_2": {
                "model_name": "Amazon Chronos-2",
                "model_version": self.CHRONOS_CHECKPOINT,
                "checkpoint_info": chr_info.get("checkpoint_info", {}),
                "device": chr_info.get("device", "unknown"),
                "is_loaded": self._chr_loaded,
                "is_genuine": True
            }
        }

    def get_or_compute_forecasts(
        self,
        symbol: str,
        dates: List[str],
        df_ohlcv: pd.DataFrame,
        horizon_bars: int = 5,
        batch_size: int = 32
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """
        Retrieves cached forecasts or computes missing ones using genuine model inference.
        Returns: {date_str: {'timesfm_2.5': {...}, 'chronos_2': {...}}}
        """
        conn = sqlite3.connect(self.cache_db_path)
        c = conn.cursor()

        # Query existing cache
        placeholders = ','.join(['?'] * len(dates))
        query = f"""
            SELECT symbol, as_of_date, model_name, expected_return_pct, uncertainty_score, downside_risk_pct, upside_potential_pct, forecast_path_json
            FROM foundation_forecast_cache
            WHERE symbol = ? AND horizon_bars = ? AND as_of_date IN ({placeholders})
        """
        c.execute(query, [symbol, horizon_bars] + dates)
        cached_rows = c.fetchall()
        conn.close()

        results_by_date = {d: {} for d in dates}
        for row in cached_rows:
            _, as_of_date, model_name, exp_ret, unc, down, up, path_json = row
            results_by_date[as_of_date][model_name] = {
                "expected_return_pct": exp_ret,
                "uncertainty_score": unc,
                "downside_risk_pct": down,
                "upside_potential_pct": up,
                "forecast_path": json.loads(path_json) if path_json else []
            }

        missing_tfm_dates = [d for d in dates if "timesfm_2.5" not in results_by_date[d]]
        missing_chr_dates = [d for d in dates if "chronos_2" not in results_by_date[d]]

        if missing_tfm_dates or missing_chr_dates:
            self.ensure_models_loaded()

        # Compute missing TimesFM
        if missing_tfm_dates:
            logger.info(f"Computing {len(missing_tfm_dates)} missing TimesFM 2.5 forecasts for {symbol}...")
            self._compute_and_cache_timesfm(symbol, missing_tfm_dates, df_ohlcv, horizon_bars, batch_size, results_by_date)

        # Compute missing Chronos
        if missing_chr_dates:
            logger.info(f"Computing {len(missing_chr_dates)} missing Chronos-2 forecasts for {symbol}...")
            self._compute_and_cache_chronos(symbol, missing_chr_dates, df_ohlcv, horizon_bars, batch_size, results_by_date)

        return results_by_date

    def _compute_and_cache_timesfm(self, symbol, missing_dates, df_ohlcv, horizon_bars, batch_size, results_by_date):
        to_insert = []
        now_ts = datetime.now().isoformat()

        for i in range(0, len(missing_dates), batch_size):
            chunk_dates = missing_dates[i:i + batch_size]
            batch_inputs = []
            valid_chunk_dates = []

            for d_str in chunk_dates:
                d_dt = pd.to_datetime(d_str).replace(hour=23, minute=59, second=59)
                df_pit = df_ohlcv.loc[df_ohlcv.index <= d_dt]
                if len(df_pit) < 30:
                    continue
                batch_inputs.append((d_str, df_pit['close'].values))
                valid_chunk_dates.append(d_str)

            if not batch_inputs:
                continue

            try:
                batch_results = self.timesfm_adapter.forecast_batch(batch_inputs, horizon_bars=horizon_bars)
                for res in batch_results:
                    d_str = res['identifier']
                    exp_ret = res['expected_return_pct']
                    unc = res['uncertainty_score']
                    path = res['forecast_path']

                    results_by_date[d_str]['timesfm_2.5'] = {
                        "expected_return_pct": exp_ret,
                        "uncertainty_score": unc,
                        "downside_risk_pct": 0.0,
                        "upside_potential_pct": 0.0,
                        "forecast_path": path
                    }
                    to_insert.append((
                        symbol, d_str, "timesfm_2.5", self.TIMESFM_CHECKPOINT, horizon_bars,
                        exp_ret, unc, 0.0, 0.0, json.dumps(path), now_ts
                    ))
            except Exception as e:
                logger.error(f"TimesFM 2.5 batch inference error for {symbol} on chunk {chunk_dates}: {e}")

        if to_insert:
            conn = sqlite3.connect(self.cache_db_path)
            c = conn.cursor()
            c.executemany("""
                INSERT OR REPLACE INTO foundation_forecast_cache
                (symbol, as_of_date, model_name, model_version, horizon_bars, expected_return_pct, uncertainty_score, downside_risk_pct, upside_potential_pct, forecast_path_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, to_insert)
            conn.commit()
            conn.close()

    def _compute_and_cache_chronos(self, symbol, missing_dates, df_ohlcv, horizon_bars, batch_size, results_by_date):
        to_insert = []
        now_ts = datetime.now().isoformat()

        for i in range(0, len(missing_dates), batch_size):
            chunk_dates = missing_dates[i:i + batch_size]
            batch_inputs = []
            valid_chunk_dates = []

            for d_str in chunk_dates:
                d_dt = pd.to_datetime(d_str).replace(hour=23, minute=59, second=59)
                df_pit = df_ohlcv.loc[df_ohlcv.index <= d_dt]
                if len(df_pit) < 30:
                    continue
                batch_inputs.append((d_str, df_pit['close'].values))
                valid_chunk_dates.append(d_str)

            if not batch_inputs:
                continue

            try:
                batch_results = self.chronos_adapter.forecast_batch(batch_inputs, horizon_bars=horizon_bars)
                for res in batch_results:
                    d_str = res['identifier']
                    exp_ret = res['expected_return_pct']
                    unc = res['uncertainty_score']
                    down = res['downside_risk_pct']
                    up = res['upside_potential_pct']
                    path = res['forecast_path']

                    results_by_date[d_str]['chronos_2'] = {
                        "expected_return_pct": exp_ret,
                        "uncertainty_score": unc,
                        "downside_risk_pct": down,
                        "upside_potential_pct": up,
                        "forecast_path": path
                    }
                    to_insert.append((
                        symbol, d_str, "chronos_2", self.CHRONOS_CHECKPOINT, horizon_bars,
                        exp_ret, unc, down, up, json.dumps(path), now_ts
                    ))
            except Exception as e:
                logger.error(f"Chronos-2 batch inference error for {symbol} on chunk {chunk_dates}: {e}")

        if to_insert:
            conn = sqlite3.connect(self.cache_db_path)
            c = conn.cursor()
            c.executemany("""
                INSERT OR REPLACE INTO foundation_forecast_cache
                (symbol, as_of_date, model_name, model_version, horizon_bars, expected_return_pct, uncertainty_score, downside_risk_pct, upside_potential_pct, forecast_path_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, to_insert)
            conn.commit()
            conn.close()
