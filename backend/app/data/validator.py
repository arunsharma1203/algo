import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

class DataValidationError(Exception):
    """Raised when market data fails quality, consistency, or sufficiency checks."""
    pass

class MarketDataValidator:
    """
    Standardized, fail-closed market data validator.
    Validates structural integrity, price logic, null rates, timestamps, and data sufficiency
    before data is allowed to enter feature engineering, Optuna tuning, model training, or inference.
    """

    REQUIRED_OHLCV_COLUMNS = ['open', 'high', 'low', 'close', 'volume']

    @classmethod
    def validate_ohlcv(
        cls,
        df: pd.DataFrame,
        ticker: str = "UNKNOWN",
        timeframe: str = "15m",  # '15m' or '1d'
        min_rows: int = 50,
        max_null_pct: float = 5.0,
        max_age_days: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Performs comprehensive, point-in-time data quality validation on an OHLCV DataFrame.

        Returns structured dictionary:
        {
            "valid": bool,
            "ticker": str,
            "rows": int,
            "timeframe": str,
            "errors": list[str],
            "warnings": list[str],
            "last_timestamp": str or None,
            "first_timestamp": str or None,
            "data_source": str
        }
        """
        errors: List[str] = []
        warnings: List[str] = []

        # 1. Existence and Type Check
        if df is None or not isinstance(df, pd.DataFrame):
            return cls._failure_result(ticker, timeframe, ["Input data is None or not a valid DataFrame."])

        if df.empty:
            return cls._failure_result(ticker, timeframe, ["DataFrame is completely empty."])

        # Normalize column names to lowercase for robust checking
        working_df = df.copy()
        if isinstance(working_df.columns, pd.MultiIndex):
            working_df.columns = working_df.columns.get_level_values(0)
        working_df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in working_df.columns]

        # 2. Required Columns Check
        missing_cols = [col for col in cls.REQUIRED_OHLCV_COLUMNS if col not in working_df.columns]
        if missing_cols:
            errors.append(f"Missing mandatory OHLCV columns: {missing_cols}")
            return cls._failure_result(ticker, timeframe, errors)

        # 3. Minimum Row Count Sufficiency
        row_count = len(working_df)
        if row_count < min_rows:
            errors.append(f"Insufficient row count: got {row_count} rows, required minimum is {min_rows}.")
            return cls._failure_result(ticker, timeframe, errors, rows=row_count)

        # 4. Null / Missing Value Rate Check
        null_counts = working_df[cls.REQUIRED_OHLCV_COLUMNS].isna().sum()
        max_null_in_col = null_counts.max()
        null_pct = (max_null_in_col / row_count) * 100.0

        if null_pct > max_null_pct:
            errors.append(f"Excessive null values: {null_pct:.2f}% nulls in columns (max allowed: {max_null_pct}%).")
        elif null_pct > 0:
            warnings.append(f"Contained {null_pct:.2f}% null values that will be dropped.")

        # 5. Price Logic & Mathematical Consistency
        clean_df = working_df.dropna(subset=cls.REQUIRED_OHLCV_COLUMNS).copy()
        for col in cls.REQUIRED_OHLCV_COLUMNS:
            clean_df[col] = pd.to_numeric(clean_df[col], errors='coerce')

        clean_df = clean_df.dropna(subset=cls.REQUIRED_OHLCV_COLUMNS)
        if len(clean_df) < min_rows:
            errors.append(f"Row count after dropping non-numeric/null values ({len(clean_df)}) is below threshold ({min_rows}).")
            return cls._failure_result(ticker, timeframe, errors, rows=len(clean_df))

        # Check for non-positive prices
        invalid_prices = (clean_df['open'] <= 0) | (clean_df['high'] <= 0) | (clean_df['low'] <= 0) | (clean_df['close'] <= 0)
        if invalid_prices.any():
            invalid_count = invalid_prices.sum()
            errors.append(f"Found {invalid_count} bars with non-positive price values (<= 0).")

        # Check High is highest and Low is lowest
        low_greater_than_high = (clean_df['low'] > clean_df['high'])
        if low_greater_than_high.any():
            errors.append(f"Found {low_greater_than_high.sum()} bars where Low > High.")

        low_greater_than_open_close = (clean_df['low'] > clean_df['open'] * 1.0001) | (clean_df['low'] > clean_df['close'] * 1.0001)
        if low_greater_than_open_close.any():
            errors.append(f"Found {low_greater_than_open_close.sum()} bars with Low > Open or Low > Close.")

        high_less_than_open_close = (clean_df['high'] < clean_df['open'] * 0.9999) | (clean_df['high'] < clean_df['close'] * 0.9999)
        if high_less_than_open_close.any():
            errors.append(f"Found {high_less_than_open_close.sum()} bars with High < Open or High < Close.")

        # Check negative volume
        negative_vol = (clean_df['volume'] < 0)
        if negative_vol.any():
            errors.append(f"Found {negative_vol.sum()} bars with negative volume.")

        # 6. Timestamp Validation
        first_ts = None
        last_ts = None
        ts_col = None
        for candidate in ['datetime', 'date', 'timestamp', 'index']:
            if candidate in clean_df.columns:
                ts_col = candidate
                break

        if ts_col:
            try:
                dt_series = pd.to_datetime(clean_df[ts_col])
                first_ts = str(dt_series.iloc[0])
                last_ts = str(dt_series.iloc[-1])

                # Check strict chronological order
                if not dt_series.is_monotonic_increasing:
                    warnings.append("Timestamps were not strictly ascending; sorting applied.")
            except Exception as e:
                warnings.append(f"Timestamp parsing warning: {e}")
        elif isinstance(clean_df.index, pd.DatetimeIndex):
            first_ts = str(clean_df.index[0])
            last_ts = str(clean_df.index[-1])

        # 7. Max Age / Freshness Check (if requested)
        if max_age_days and last_ts:
            try:
                last_dt = pd.to_datetime(last_ts).tz_localize(None) if pd.to_datetime(last_ts).tzinfo else pd.to_datetime(last_ts)
                now_dt = datetime.now()
                age_days = (now_dt - last_dt).days
                if age_days > max_age_days:
                    warnings.append(f"Latest data bar is {age_days} days old (max expected: {max_age_days} days).")
            except Exception:
                pass

        is_valid = len(errors) == 0
        data_source = str(df.attrs.get('source', 'unknown'))

        return {
            "valid": is_valid,
            "ticker": ticker,
            "rows": len(clean_df),
            "timeframe": timeframe,
            "errors": errors,
            "warnings": warnings,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "data_source": data_source
        }

    @classmethod
    def assert_valid_ohlcv(cls, df: pd.DataFrame, ticker: str = "UNKNOWN", timeframe: str = "15m", min_rows: int = 50) -> pd.DataFrame:
        """
        Validates OHLCV DataFrame and returns cleaned copy.
        Raises DataValidationError if critical validation fails.
        """
        report = cls.validate_ohlcv(df, ticker=ticker, timeframe=timeframe, min_rows=min_rows)
        if not report["valid"]:
            error_msg = f"Data validation failed for {ticker} ({timeframe}): {'; '.join(report['errors'])}"
            logger.error(error_msg)
            raise DataValidationError(error_msg)
        return df

    @classmethod
    def _failure_result(cls, ticker: str, timeframe: str, errors: List[str], rows: int = 0) -> Dict[str, Any]:
        return {
            "valid": False,
            "ticker": ticker,
            "rows": rows,
            "timeframe": timeframe,
            "errors": errors,
            "warnings": [],
            "first_timestamp": None,
            "last_timestamp": None,
            "data_source": "invalid"
        }

