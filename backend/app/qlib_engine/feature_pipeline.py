"""
REAL QLIB FEATURE PIPELINE
==========================
Extracts official Microsoft Qlib Alpha158 and Alpha360 features using
the installed `qlib.contrib.data.handler` classes.

Invariants:
- Uses ONLY official Microsoft Qlib handlers (NO Qlib-inspired or custom homemade clones).
- Strictly verified at runtime: module MUST be `qlib.contrib.data.handler`.
- Returns pandas DataFrame indexed by (datetime, instrument) with exact Qlib features.
"""

import logging
import pandas as pd
from typing import List, Dict, Any, Optional, Tuple

import qlib
from qlib.contrib.data.handler import Alpha158, Alpha360
from app.qlib_engine.india_data_adapter import ensure_qlib_ready

logger = logging.getLogger(__name__)


class QlibFeaturePipeline:
    """
    Official Microsoft Qlib feature extraction pipeline.
    """

    @classmethod
    def verify_feature_handler(cls, handler_cls) -> Dict[str, Any]:
        """
        Verifies that handler is the real Microsoft Qlib class.
        """
        module_name = getattr(handler_cls, "__module__", "")
        if "qlib.contrib.data.handler" not in module_name:
            raise RuntimeError(
                f"Integrity Violation: {handler_cls} is not from Microsoft Qlib! "
                f"Module resolved to: {module_name}"
            )
        return {
            "verified": True,
            "class_name": handler_cls.__name__,
            "module": module_name,
            "qlib_version": getattr(qlib, "__version__", "unknown")
        }

    @classmethod
    def get_feature_metadata(cls, feature_family: str = "Alpha158") -> Dict[str, Any]:
        """Returns verified feature family metadata."""
        fam = feature_family.upper()
        if fam == "ALPHA158":
            handler_cls = Alpha158
            expected_count = 158
        elif fam == "ALPHA360":
            handler_cls = Alpha360
            expected_count = 360
        else:
            raise ValueError(f"Unsupported Qlib feature family: {feature_family}. Use Alpha158 or Alpha360.")

        verification = cls.verify_feature_handler(handler_cls)
        return {
            "family": fam,
            "handler_class": handler_cls.__name__,
            "handler_module": verification["module"],
            "expected_factor_count": expected_count,
            "qlib_version": verification["qlib_version"],
            "real_qlib": True
        }

    @classmethod
    def create_handler(
        cls,
        instruments: List[str],
        start_time: str,
        end_time: str,
        freq: str = "day",
        feature_family: str = "Alpha158",
        fit_start_time: Optional[str] = None,
        fit_end_time: Optional[str] = None
    ):
        """
        Instantiates real Qlib DataHandler (Alpha158 or Alpha360).
        """
        ensure_qlib_ready()
        fam = feature_family.upper()
        clean_instruments = [inst.upper().replace(".NS", "").replace(".BO", "").strip() for inst in instruments]

        if fam == "ALPHA158":
            cls.verify_feature_handler(Alpha158)
            handler = Alpha158(
                instruments=clean_instruments,
                start_time=start_time,
                end_time=end_time,
                freq=freq,
                fit_start_time=fit_start_time or start_time,
                fit_end_time=fit_end_time or end_time
            )
        elif fam == "ALPHA360":
            cls.verify_feature_handler(Alpha360)
            handler = Alpha360(
                instruments=clean_instruments,
                start_time=start_time,
                end_time=end_time,
                freq=freq,
                fit_start_time=fit_start_time or start_time,
                fit_end_time=fit_end_time or end_time
            )
        else:
            raise ValueError(f"Unknown feature family: {feature_family}")

        return handler

    @classmethod
    def extract_features(
        cls,
        instruments: List[str],
        start_time: str,
        end_time: str,
        freq: str = "day",
        feature_family: str = "Alpha158",
        fit_start_time: Optional[str] = None,
        fit_end_time: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Extracts features using Microsoft Qlib's DataHandler.fetch().
        Returns DataFrame with multi-index (datetime, instrument).
        """
        handler = cls.create_handler(
            instruments=instruments,
            start_time=start_time,
            end_time=end_time,
            freq=freq,
            feature_family=feature_family,
            fit_start_time=fit_start_time,
            fit_end_time=fit_end_time
        )
        df_features = handler.fetch()
        logger.info(
            f"[QlibFeaturePipeline] Extracted {feature_family} features for {len(instruments)} symbols: "
            f"shape={df_features.shape}, memory={df_features.memory_usage().sum() / 1024 / 1024:.2f} MB"
        )
        return df_features
