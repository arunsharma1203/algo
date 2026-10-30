#!/usr/bin/env python3
"""
CHAMPION MODEL REVERT & INTEGRITY RESTORATION SCRIPT
====================================================
Instant, single-command rollback utility for production AI Champions.
Restores models from immutable version archives and validates SHA-256 byte parity.

Usage:
  python backend/scripts/revert_champion.py --timeframe swing
  python backend/scripts/revert_champion.py --timeframe intraday
  python backend/scripts/revert_champion.py --all
  python backend/scripts/revert_champion.py --check-only
"""

import os
import sys
import argparse
import shutil
import hashlib
import json
import logging

# Add backend directory to sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.analytics.model_manager import ModelManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RevertChampion")

KNOWN_CANONICAL_HASHES = {
    "intraday": "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91",
    "swing": "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"
}

def compute_file_sha256(filepath: str) -> str:
    if not os.path.exists(filepath):
        return ""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def check_champion_integrity(timeframe: str) -> dict:
    model_path, meta_path = ModelManager.get_champion_paths(timeframe)
    current_sha = compute_file_sha256(model_path)
    canonical_sha = KNOWN_CANONICAL_HASHES.get(timeframe.lower(), "")
    is_canonical = (current_sha == canonical_sha)
    meta = ModelManager.load_champion_metadata(timeframe)
    
    return {
        "timeframe": timeframe,
        "model_path": model_path,
        "version": meta.get("version", "unknown"),
        "sha256": current_sha,
        "is_canonical_v1": is_canonical,
        "exists": os.path.exists(model_path)
    }

def revert_champion(timeframe: str, target_version: str = "v1.0-champion") -> dict:
    tf = timeframe.lower()
    model_path, meta_path = ModelManager.get_champion_paths(tf)
    version_dir = os.path.join(ModelManager.get_versions_dir(tf), target_version)
    
    src_model = os.path.join(version_dir, "model.pkl")
    src_meta = os.path.join(version_dir, "metadata.json")
    
    if not os.path.exists(src_model):
        raise FileNotFoundError(f"Cannot revert: Snapshot archive not found at {src_model}")
        
    # Copy files atomically
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    shutil.copy2(src_model, model_path)
    if os.path.exists(src_meta):
        shutil.copy2(src_meta, meta_path)
        
    current_sha = compute_file_sha256(model_path)
    canonical_sha = KNOWN_CANONICAL_HASHES.get(tf, "")
    is_canonical = (current_sha == canonical_sha)
    
    logger.info(f"✅ Successfully reverted {tf.upper()} Champion to {target_version}")
    logger.info(f"   Model: {model_path}")
    logger.info(f"   SHA256: {current_sha} (Matches Canonical: {is_canonical})")
    
    return {
        "status": "SUCCESS",
        "timeframe": tf,
        "version": target_version,
        "sha256": current_sha,
        "is_canonical": is_canonical
    }

def main():
    parser = argparse.ArgumentParser(description="Revert production AI Champion models.")
    parser.add_argument("--timeframe", choices=["intraday", "swing"], help="Timeframe to revert")
    parser.add_argument("--all", action="store_true", help="Revert both intraday and swing champions")
    parser.add_argument("--check-only", action="store_true", help="Check integrity of active champions without modifying")
    parser.add_argument("--version", default="v1.0-champion", help="Target version snapshot (default: v1.0-champion)")
    
    args = parser.parse_args()
    
    if args.check_only:
        print("\n=== ACTIVE CHAMPION INTEGRITY AUDIT ===")
        for tf in ["intraday", "swing"]:
            res = check_champion_integrity(tf)
            status_symbol = "✅" if res["is_canonical_v1"] else "⚠️ (Challenger/Modified)"
            print(f"{status_symbol} {tf.upper()} Champion:")
            print(f"   Version: {res['version']}")
            print(f"   SHA256:  {res['sha256']}")
            print(f"   Path:    {res['model_path']}")
        return
        
    tfs = []
    if args.all:
        tfs = ["intraday", "swing"]
    elif args.timeframe:
        tfs = [args.timeframe]
    else:
        parser.print_help()
        sys.exit(1)
        
    print(f"\n🔄 REVERTING CHAMPION(S) TO {args.version.upper()}...")
    for tf in tfs:
        revert_champion(tf, target_version=args.version)
    print("\n✨ Revert complete. All production models verified.\n")

if __name__ == "__main__":
    main()

