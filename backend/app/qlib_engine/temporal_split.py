"""
QLIB TEMPORAL SPLIT & LEAKAGE PREVENTION ENGINE
===============================================
Enforces strict chronological partitioning for quantitative time-series models.

Partitions:
1. TRAIN: Model estimation only.
2. VALIDATION: Model selection, hyperparameter tuning, early stopping.
3. LOCKED OOS: Out-of-Sample forward validation evaluated strictly once.

Leakage Invariants:
- Zero random splitting.
- Zero future lookahead.
- Transformations and normalizations fitted exclusively on TRAIN window.
- Locked OOS remains untouched during candidate selection.
"""

import pandas as pd
from typing import Dict, Any, Tuple, List
from datetime import datetime


class TemporalSplitError(ValueError):
    """Raised when temporal ordering or chronological boundaries are violated."""
    pass


class TemporalSplitter:
    """
    Computes and enforces chronological boundaries for Qlib Dataset segments.
    """

    @staticmethod
    def partition_dates(
        dates: List[str],
        train_ratio: float = 0.70,
        val_ratio: float = 0.15,
        purge_bars: int = 5,
        embargo_bars: int = 2
    ) -> Dict[str, Any]:
        """
        Splits sorted list of date strings into non-overlapping chronological segments,
        incorporating label horizon purge bars (to prevent forward label lookahead from train to valid)
        and embargo bars (between valid and test).
        """
        if not dates or len(dates) < 30:
            raise TemporalSplitError(f"Insufficient dates for temporal partitioning: {len(dates) if dates else 0} < 30")

        sorted_dates = sorted(list(set(dates)))
        n = len(sorted_dates)

        raw_train_end_idx = int(n * train_ratio)
        raw_val_end_idx = int(n * (train_ratio + val_ratio))

        # Apply purge: remove trailing `purge_bars` from train to ensure forward labels (e.g. 5-day horizon)
        # do not overlap with validation segment
        effective_train_end_idx = max(5, raw_train_end_idx - max(0, purge_bars))
        val_start_idx = raw_train_end_idx

        # Apply embargo: remove trailing `embargo_bars` from validation before test segment
        effective_val_end_idx = max(val_start_idx + 3, raw_val_end_idx - max(0, embargo_bars))
        oos_start_idx = raw_val_end_idx

        if effective_train_end_idx < 5 or (effective_val_end_idx - val_start_idx) < 3 or (n - oos_start_idx) < 3:
            raise TemporalSplitError("Temporal split partitions are too small. Need more historical data.")

        train_segment = (sorted_dates[0], sorted_dates[effective_train_end_idx - 1])
        purged_segment = (sorted_dates[effective_train_end_idx], sorted_dates[val_start_idx - 1]) if val_start_idx > effective_train_end_idx else None
        val_segment = (sorted_dates[val_start_idx], sorted_dates[effective_val_end_idx - 1])
        embargo_segment = (sorted_dates[effective_val_end_idx], sorted_dates[oos_start_idx - 1]) if oos_start_idx > effective_val_end_idx else None
        oos_segment = (sorted_dates[oos_start_idx], sorted_dates[-1])

        # Verify strict monotonicity
        t_end = pd.to_datetime(train_segment[1])
        v_start = pd.to_datetime(val_segment[0])
        v_end = pd.to_datetime(val_segment[1])
        o_start = pd.to_datetime(oos_segment[0])

        if not (t_end < v_start and v_end < o_start):
            raise TemporalSplitError(
                f"Temporal boundary overlap detected! "
                f"Train: {train_segment}, Val: {val_segment}, OOS: {oos_segment}"
            )

        return {
            "train": train_segment,
            "valid": val_segment,
            "test": oos_segment,
            "purge_bars": purge_bars,
            "embargo_bars": embargo_bars,
            "purged_range": purged_segment,
            "embargo_range": embargo_segment
        }

    @staticmethod
    def create_segments_from_range(
        start_date: str,
        end_date: str,
        calendar_dates: List[str],
        train_ratio: float = 0.70,
        val_ratio: float = 0.15,
        purge_bars: int = 5,
        embargo_bars: int = 2
    ) -> Dict[str, Any]:
        """
        Filters calendar dates within [start_date, end_date] and partitions them.
        """
        filtered = [d for d in calendar_dates if start_date <= d <= end_date]
        return TemporalSplitter.partition_dates(
            filtered,
            train_ratio=train_ratio,
            val_ratio=val_ratio,
            purge_bars=purge_bars,
            embargo_bars=embargo_bars
        )
