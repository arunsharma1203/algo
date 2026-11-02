"""
DETERMINISTIC TEMPORAL SPLIT & LEAKAGE PREVENTION ENGINE
========================================================
Implements strict chronological partitioning based on information availability.
Guarantees:
1. Entry time dictates observation availability.
2. Exit time dictates label availability.
3. No trade whose outcome resolved AFTER an information boundary enters that partition.
4. Purge handling removes boundary-crossing straddles.
5. Locked OOS remains strictly unread by training and calibration.
"""

import pandas as pd
from typing import Dict, Any, Tuple


class TemporalSplitValidationError(ValueError):
    """Raised when temporal boundary or leakage invariants are violated."""
    pass


def make_temporal_splits(
    df: pd.DataFrame,
    train_ratio: float = 0.70,
    val_ratio: float = 0.15,
    timestamp_col: str = "timestamp",
    exit_time_col: str = "exit_time"
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """
    Partitions trades into non-overlapping chronological segments (Train, Validation, Locked OOS)
    with strict purge filtering around boundary crossings.

    Returns: (train_df, val_df, oos_df, diagnostics)
    """
    if df.empty or len(df) < 50:
        raise TemporalSplitValidationError(f"Insufficient trade samples for temporal splitting: {len(df)} < 50")

    trades = df.copy()
    trades[timestamp_col] = pd.to_datetime(trades[timestamp_col], format='mixed', utc=True)
    
    # Fill missing exit_time with timestamp if missing
    if exit_time_col not in trades.columns or trades[exit_time_col].isnull().all():
        trades["_exit_ts"] = trades[timestamp_col]
    else:
        trades["_exit_ts"] = pd.to_datetime(trades[exit_time_col], format='mixed', utc=True).fillna(trades[timestamp_col])

    # Sort strictly chronologically by entry timestamp
    trades = trades.sort_values(by=timestamp_col).reset_index(drop=True)
    n = len(trades)

    raw_train_end_idx = int(n * train_ratio)
    raw_val_end_idx = int(n * (train_ratio + val_ratio))

    train_cutoff = trades.loc[raw_train_end_idx - 1, timestamp_col]
    val_cutoff = trades.loc[raw_val_end_idx - 1, timestamp_col]

    # Partition by entry timestamp
    train_mask = trades[timestamp_col] <= train_cutoff
    val_mask = (trades[timestamp_col] > train_cutoff) & (trades[timestamp_col] <= val_cutoff)
    oos_mask = trades[timestamp_col] > val_cutoff

    raw_train = trades[train_mask].copy()
    raw_val = trades[val_mask].copy()
    raw_oos = trades[oos_mask].copy()

    # PURGE FILTERING:
    # 1. Any trade in Train whose exit resolved AFTER train_cutoff leaks future validation data -> PURGE
    purged_train_straddles = raw_train[raw_train["_exit_ts"] > train_cutoff]
    clean_train = raw_train[raw_train["_exit_ts"] <= train_cutoff].copy()

    # 2. Any trade in Validation whose exit resolved AFTER val_cutoff leaks future OOS data -> PURGE
    purged_val_straddles = raw_val[raw_val["_exit_ts"] > val_cutoff]
    clean_val = raw_val[raw_val["_exit_ts"] <= val_cutoff].copy()

    clean_oos = raw_oos.copy()

    # Hard Invariant Assertions
    if not clean_train.empty:
        max_train_exit = clean_train["_exit_ts"].max()
        if max_train_exit > train_cutoff:
            raise TemporalSplitValidationError(
                f"Train split leakage detected! Max train exit {max_train_exit} > train cutoff {train_cutoff}"
            )

    if not clean_val.empty:
        max_val_exit = clean_val["_exit_ts"].max()
        if max_val_exit > val_cutoff:
            raise TemporalSplitValidationError(
                f"Validation split leakage detected! Max validation exit {max_val_exit} > val cutoff {val_cutoff}"
            )

    # Clean up temporary column
    clean_train = clean_train.drop(columns=["_exit_ts"], errors="ignore")
    clean_val = clean_val.drop(columns=["_exit_ts"], errors="ignore")
    clean_oos = clean_oos.drop(columns=["_exit_ts"], errors="ignore")

    diagnostics = {
        "total_source_trades": n,
        "train_cutoff": str(train_cutoff),
        "val_cutoff": str(val_cutoff),
        "raw_train_count": len(raw_train),
        "purged_train_straddles_count": len(purged_train_straddles),
        "clean_train_count": len(clean_train),
        "raw_val_count": len(raw_val),
        "purged_val_straddles_count": len(purged_val_straddles),
        "clean_val_count": len(clean_val),
        "locked_oos_count": len(clean_oos),
        "train_date_range": (str(clean_train[timestamp_col].min()), str(clean_train[timestamp_col].max())) if not clean_train.empty else (None, None),
        "val_date_range": (str(clean_val[timestamp_col].min()), str(clean_val[timestamp_col].max())) if not clean_val.empty else (None, None),
        "oos_date_range": (str(clean_oos[timestamp_col].min()), str(clean_oos[timestamp_col].max())) if not clean_oos.empty else (None, None),
        "leakage_invariants_verified": True
    }

    return clean_train, clean_val, clean_oos, diagnostics
