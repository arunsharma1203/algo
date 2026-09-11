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
        val_ratio: float = 0.15
    ) -> Dict[str, Tuple[str, str]]:
        """
        Splits sorted list of date strings into non-overlapping chronological segments.
        """
        if not dates or len(dates) < 30:
            raise TemporalSplitError(f"Insufficient dates for temporal partitioning: {len(dates) if dates else 0} < 30")

        sorted_dates = sorted(list(set(dates)))
        n = len(sorted_dates)

        train_end_idx = int(n * train_ratio)
        val_end_idx = int(n * (train_ratio + val_ratio))

        if train_end_idx < 10 or (val_end_idx - train_end_idx) < 5 or (n - val_end_idx) < 5:
            raise TemporalSplitError("Temporal split partitions are too small. Need more historical data.")

        train_segment = (sorted_dates[0], sorted_dates[train_end_idx - 1])
        val_segment = (sorted_dates[train_end_idx], sorted_dates[val_end_idx - 1])
        oos_segment = (sorted_dates[val_end_idx], sorted_dates[-1])

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
            "test": oos_segment
        }

    @staticmethod
    def create_segments_from_range(
        start_date: str,
        end_date: str,
        calendar_dates: List[str],
        train_ratio: float = 0.70,
        val_ratio: float = 0.15
    ) -> Dict[str, Tuple[str, str]]:
        """
        Filters calendar dates within [start_date, end_date] and partitions them.
        """
        filtered = [d for d in calendar_dates if start_date <= d <= end_date]
        return TemporalSplitter.partition_dates(filtered, train_ratio, val_ratio)
