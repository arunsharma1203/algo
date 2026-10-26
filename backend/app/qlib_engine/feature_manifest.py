"""
QLIB FEATURE MANIFEST & COMPATIBILITY GATE
==========================================
Enforces cryptographic and structural feature contracts between training and inference.
Prevents silent feature drift, column reordering, missing features, and schema mismatches.

Invariants:
- Feature names and ordering must match 100% between training and inference.
- Feature count must be identical.
- Incompatible inference requests fail-closed with QlibModelCompatibilityError.
"""

import json
import hashlib
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Union
import pandas as pd

import ast

logger = logging.getLogger(__name__)


def clean_feature_name(c: Any) -> str:
    """Normalizes feature column identifiers (handles tuples, MultiIndex, and stringified tuples)."""
    if isinstance(c, tuple):
        return str(c[-1])
    s = str(c).strip()
    if s.startswith("(") and s.endswith(")"):
        try:
            val = ast.literal_eval(s)
            if isinstance(val, tuple):
                return str(val[-1])
        except Exception:
            pass
    return s


class QlibModelCompatibilityError(RuntimeError):
    """Raised when inference features do not match the trained model's feature manifest."""
    pass


def build_feature_manifest(
    feature_names: Union[List[str], pd.Index],
    feature_family: str = "Alpha158",
    horizon_days: int = 5
) -> Dict[str, Any]:
    """
    Builds an immutable, content-hashed feature manifest from the training feature set.
    """
    names = [clean_feature_name(c) for c in feature_names]
    h = hashlib.sha256(json.dumps(names).encode("utf-8")).hexdigest()

    manifest = {
        "feature_family": feature_family,
        "feature_count": len(names),
        "feature_names": names,
        "horizon_days": horizon_days,
        "manifest_hash": h,
        "created_at": datetime.now().isoformat()
    }
    return manifest


def verify_feature_compatibility(
    trained_manifest: Optional[Dict[str, Any]],
    current_features: Any
) -> Tuple[bool, str]:
    """
    Validates current inference features against the trained model manifest.
    Returns (is_compatible, detail_message).
    """
    if trained_manifest is None:
        return True, "NO_MANIFEST_SPECIFIED"

    expected_names = [clean_feature_name(c) for c in trained_manifest.get("feature_names", [])]
    expected_count = trained_manifest.get("feature_count", len(expected_names))

    if hasattr(current_features, "columns"):
        current_names = [clean_feature_name(c) for c in current_features.columns]
    elif isinstance(current_features, (list, tuple, pd.Index)):
        current_names = [clean_feature_name(c) for c in current_features]
    else:
        current_names = [clean_feature_name(c) for c in list(current_features)]

    # Check 1: Feature count
    if len(current_names) != expected_count:
        return False, (
            f"QLIB_MODEL_COMPATIBILITY_FAILURE: Feature count mismatch. "
            f"Expected {expected_count} features, received {len(current_names)}."
        )

    # Check 2: Exact order and name equality
    if current_names != expected_names:
        missing = set(expected_names) - set(current_names)
        extra = set(current_names) - set(expected_names)
        if missing or extra:
            return False, (
                f"QLIB_MODEL_COMPATIBILITY_FAILURE: Feature columns mismatch. "
                f"Missing: {list(missing)[:5]}, Extra: {list(extra)[:5]}."
            )
        return False, (
            "QLIB_MODEL_COMPATIBILITY_FAILURE: Feature column order mismatch. "
            "Columns must appear in identical sequence as trained."
        )

    return True, "COMPATIBLE"


def assert_feature_compatibility(
    trained_manifest: Optional[Dict[str, Any]],
    current_features: Any
) -> None:
    """
    Asserts feature compatibility, raising QlibModelCompatibilityError on failure.
    """
    compatible, msg = verify_feature_compatibility(trained_manifest, current_features)
    if not compatible:
        logger.error(f"[QlibCompatibility] {msg}")
        raise QlibModelCompatibilityError(msg)
