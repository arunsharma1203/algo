"""
DUAL-ENGINE CONSENSUS SCANNER (MULTI-MODEL CONFLUENCE)
======================================================
Coordinates parallel execution of:
1. Microsoft Qlib Quantitative Alpha Factor Expressions (Alpha158 + GBDT)
2. Legacy Multi-Model AI Ensemble (TA-Lib Indicators + RF/GB/SVC Ensemble + Platt Calibration)

Strict Invariants:
- Zero coupling between engines (isolation at architectural boundary).
- Confluence evaluated at the aggregation layer.
- Unanimous setups highlighted with blended risk parameters.
- Directional conflicts explicitly flagged with high-risk warnings.
- State persistence: Latest scan results persisted in SQLite and cached in memory.
- Active execution tracking: Live stage progress exposed for real-time frontend monitoring.
- Zero dummy data: All metrics sourced from live models, OHLCV indicators, and Alpha158 features.
"""

import time
import uuid
import json
import sqlite3
import logging
import asyncio
from datetime import datetime
from typing import Dict, Any, List, Optional

from app.data.historical_data_layer import get_db_path
from app.qlib_engine.scanners import QlibScanners
from app.analytics.smart_scanner import SmartScannerPipeline
from app.analytics.master_logger import MasterLogger

logger = logging.getLogger(__name__)


def normalize_symbol(symbol: str) -> str:
    """Normalizes ticker symbol across exchange variations (e.g. INFY.NS -> INFY)."""
    if not symbol:
        return ""
    return symbol.upper().replace(".NS", "").replace(".BO", "").strip()


def normalize_direction(direction: str, is_bullish: Optional[bool] = None) -> str:
    """Normalizes direction to standard BUY or SELL."""
    d = (direction or "").upper().strip()
    if d in ("BUY", "BULLISH", "LONG") or is_bullish is True:
        return "BUY"
    if d in ("SELL", "BEARISH", "SHORT") or is_bullish is False:
        return "SELL"
    return "BUY"


class ConsensusScanner:
    """
    Orchestrates concurrent quantitative alpha (Qlib) and technical ensemble (Legacy) scans,
    detecting Grade-A Confluence setups and highlighting directional conflicts.
    Maintains persistent scan history and real-time execution telemetry.
    """

    _ACTIVE_SCAN: Optional[Dict[str, Any]] = None
    _LATEST_SCAN_RESULTS: Dict[str, Dict[str, Any]] = {}
    _SCHEMA_INITIALIZED: bool = False
    _STOP_REQUESTED: bool = False
    _CURRENT_TASK: Optional[asyncio.Task] = None

    @classmethod
    def stop_active_scan(cls) -> Dict[str, Any]:
        """Stops/cancels any actively running consensus scan immediately."""
        cls._STOP_REQUESTED = True
        scan_info = cls.get_active_scan()
        if cls._CURRENT_TASK and not cls._CURRENT_TASK.done():
            cls._CURRENT_TASK.cancel()
        cls._ACTIVE_SCAN = None
        MasterLogger.log_event(
            category="SCAN_MANUAL",
            event_type="CONSENSUS_SCAN_CANCELLED",
            message=f"Consensus scan {scan_info.get('scan_id', 'unknown')} stopped by user request.",
            details=scan_info
        )
        return {"status": "STOPPED", "message": "Consensus scan successfully stopped.", "stopped_scan": scan_info}

    @classmethod
    def is_stop_requested(cls) -> bool:
        """Returns True if user has requested to cancel the active scan."""
        return cls._STOP_REQUESTED

    @classmethod
    def _ensure_schema(cls) -> None:
        """Initializes the dedicated latest_consensus_scans table in SQLite."""
        if cls._SCHEMA_INITIALIZED:
            return
        try:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS latest_consensus_scans (
                        strategy TEXT PRIMARY KEY,
                        universe TEXT,
                        scan_id TEXT,
                        completed_at TEXT,
                        elapsed_seconds REAL,
                        payload_json TEXT
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS consensus_scan_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scan_id TEXT UNIQUE,
                        strategy TEXT,
                        universe TEXT,
                        completed_at TEXT,
                        unanimous_count INTEGER,
                        qlib_solo_count INTEGER,
                        legacy_solo_count INTEGER,
                        conflicts_count INTEGER,
                        elapsed_seconds REAL,
                        payload_json TEXT
                    );
                """)
            conn.close()
            cls._SCHEMA_INITIALIZED = True
        except Exception as e:
            logger.warning(f"Could not initialize latest_consensus_scans table: {e}")

    @classmethod
    def get_active_scan(cls) -> Dict[str, Any]:
        """
        Returns live progress status of currently running consensus scan, if any.
        Used by frontend to seamlessly resume progress tracking after browser refresh.
        """
        if cls._ACTIVE_SCAN:
            elapsed = round(time.time() - cls._ACTIVE_SCAN.get("start_time", time.time()), 1)
            return {
                "active": True,
                "scan_id": cls._ACTIVE_SCAN.get("scan_id"),
                "strategy": cls._ACTIVE_SCAN.get("strategy"),
                "universe": cls._ACTIVE_SCAN.get("universe"),
                "started_at": cls._ACTIVE_SCAN.get("started_at"),
                "stage": cls._ACTIVE_SCAN.get("stage"),
                "stage_name": cls._ACTIVE_SCAN.get("stage_name"),
                "progress": cls._ACTIVE_SCAN.get("progress", 0),
                "universe_count": cls._ACTIVE_SCAN.get("universe_count", 0),
                "message": cls._ACTIVE_SCAN.get("message", "Processing scan..."),
                "elapsed_seconds": elapsed
            }
        return {"active": False}

    @classmethod
    def get_latest_scan(cls, strategy: str = "INTRADAY") -> Optional[Dict[str, Any]]:
        """
        Returns the most recent completed consensus scan result.
        Checks SQLite timestamp to guarantee cache freshness across external workers/daemons.
        """
        strat_key = strategy.upper().strip()
        cls._ensure_schema()
        try:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            row = conn.execute(
                "SELECT completed_at, payload_json FROM latest_consensus_scans WHERE strategy = ?",
                (strat_key,)
            ).fetchone()
            conn.close()
            if row and row[1]:
                completed_at, payload_str = row[0], row[1]
                # Return in-memory cache if it matches the latest SQLite completed_at
                if strat_key in cls._LATEST_SCAN_RESULTS:
                    cached = cls._LATEST_SCAN_RESULTS[strat_key]
                    if cached.get("completed_at") == completed_at:
                        return cached
                payload = json.loads(payload_str)
                cls._LATEST_SCAN_RESULTS[strat_key] = payload
                return payload
        except Exception as e:
            logger.warning(f"Failed to load latest consensus scan from database: {e}")

        # Fallback to in-memory cache if DB query failed
        if strat_key in cls._LATEST_SCAN_RESULTS:
            return cls._LATEST_SCAN_RESULTS[strat_key]

        return None

    @classmethod
    def get_scan_history(cls, strategy: str = "INTRADAY", limit: int = 10) -> List[Dict[str, Any]]:
        """Returns recent consensus scans from historical audit table."""
        cls._ensure_schema()
        strat_key = strategy.upper().strip()
        history = []
        try:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)
            cur = conn.execute("""
                SELECT scan_id, strategy, universe, completed_at, 
                       unanimous_count, qlib_solo_count, legacy_solo_count, 
                       conflicts_count, elapsed_seconds, payload_json
                FROM consensus_scan_history 
                WHERE strategy = ? 
                ORDER BY id DESC LIMIT ?
            """, (strat_key, limit))
            for row in cur.fetchall():
                try:
                    payload = json.loads(row[9]) if row[9] else {}
                except Exception:
                    payload = {}
                history.append({
                    "scan_id": row[0],
                    "strategy": row[1],
                    "universe": row[2],
                    "completed_at": row[3],
                    "unanimous_count": row[4],
                    "qlib_solo_count": row[5],
                    "legacy_solo_count": row[6],
                    "conflicts_count": row[7],
                    "elapsed_seconds": row[8],
                    "summary": payload.get("summary", {}),
                    "macro_regime": payload.get("macro_regime", {})
                })
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to fetch consensus scan history: {e}")
        return history

    @classmethod
    def _save_latest_scan(cls, strategy: str, payload: Dict[str, Any]) -> None:
        """Saves scan result to in-memory cache, history audit log, and latest persistent storage."""
        strat_key = strategy.upper().strip()
        cls._ensure_schema()

        current_unanimous = payload.get("unanimous_trades") or []

        try:
            conn = sqlite3.connect(get_db_path(), timeout=10.0)

            # Check if previous scan had trades and current sweep has 0 — retain previous trades for UI continuity
            prev_row = conn.execute(
                "SELECT payload_json FROM latest_consensus_scans WHERE strategy = ?",
                (strat_key,)
            ).fetchone()
            if prev_row and prev_row[0]:
                try:
                    prev_payload = json.loads(prev_row[0])
                    prev_trades = prev_payload.get("unanimous_trades") or []
                    if len(prev_trades) > 0 and len(current_unanimous) == 0:
                        payload["previous_active_trades"] = prev_trades
                        payload["previous_active_scan_at"] = prev_payload.get("completed_at")
                        payload["previous_active_scan_id"] = prev_payload.get("scan_id")
                    elif len(current_unanimous) > 0:
                        # Clear out any stale previous_active_trades if current has fresh trades
                        payload.pop("previous_active_trades", None)
                        payload.pop("previous_active_scan_at", None)
                        payload.pop("previous_active_scan_id", None)
                except Exception:
                    pass

            with conn:
                # 1. Permanent historical log
                conn.execute("""
                    INSERT OR REPLACE INTO consensus_scan_history (
                        scan_id, strategy, universe, completed_at, 
                        unanimous_count, qlib_solo_count, legacy_solo_count, 
                        conflicts_count, elapsed_seconds, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    payload.get("scan_id", ""),
                    strat_key,
                    payload.get("universe", ""),
                    payload.get("completed_at", datetime.now().isoformat()),
                    len(payload.get("unanimous_trades", [])),
                    len(payload.get("qlib_solo", [])),
                    len(payload.get("legacy_solo", [])),
                    len(payload.get("conflicting_trades", [])),
                    payload.get("elapsed_seconds", 0.0),
                    json.dumps(payload)
                ))

                # 2. Latest scan pointer
                conn.execute("""
                    INSERT INTO latest_consensus_scans (strategy, universe, scan_id, completed_at, elapsed_seconds, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(strategy) DO UPDATE SET
                        universe = excluded.universe,
                        scan_id = excluded.scan_id,
                        completed_at = excluded.completed_at,
                        elapsed_seconds = excluded.elapsed_seconds,
                        payload_json = excluded.payload_json
                """, (
                    strat_key,
                    payload.get("universe", ""),
                    payload.get("scan_id", ""),
                    payload.get("completed_at", datetime.now().isoformat()),
                    payload.get("elapsed_seconds", 0.0),
                    json.dumps(payload)
                ))
            conn.close()
        except Exception as e:
            logger.warning(f"Could not persist latest consensus scan to database: {e}")

        cls._LATEST_SCAN_RESULTS[strat_key] = payload

    @classmethod
    def evaluate_timed_anchor_filter(
        cls,
        ticker: str,
        direction: str,
        current_price: float,
        live_vwap: Optional[float] = None,
        live_open: Optional[float] = None,
        eval_time: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Step 2 Quantitative Filter for Timed Scans (Applied to 09:30, 11:30, 13:30 institutional anchors).
        Evaluates point-in-time opening candle range (ORB) and intraday VWAP/session trend.
        Uses local SQLite 15m OHLCV bars without external API calls (100% rate-limit compliant).
        """
        now = eval_time or datetime.now()
        cur_m = now.hour * 60 + now.minute
        target_minutes = {"09:30": 570, "11:30": 690, "13:30": 810}

        anchor_milestone = None
        best_diff = 999
        for name, tgt_m in target_minutes.items():
            diff = cur_m - tgt_m
            # Strictly causal window: Anchor triggers AT or AFTER the milestone (0 to +30m)
            # 09:30 anchor: 09:30 - 10:00 (570m - 600m)
            # 11:30 anchor: 11:30 - 12:00 (690m - 720m)
            # 13:30 anchor: 13:30 - 14:00 (810m - 840m)
            if 0 <= diff <= 30 and diff < best_diff:
                best_diff = diff
                anchor_milestone = name

        if not anchor_milestone:
            return {
                "is_anchor_window": False,
                "anchor_milestone": None,
                "passed": True,
                "quality_boost": 0.0,
                "reason": "OFF_ANCHOR"
            }

        direction_upper = str(direction).upper()

        # 1. 09:30 Anchor -> Opening Candle Range (ORB / Position in Range)
        if anchor_milestone == "09:30":
            try:
                today_str = now.strftime("%Y-%m-%d")
                conn = sqlite3.connect(get_db_path(), timeout=5.0)
                cur = conn.cursor()
                clean_sym = ticker if "." in ticker else f"{ticker}.NS"
                cur.execute("""
                    SELECT open, high, low, close, volume 
                    FROM ohlcv 
                    WHERE ticker = ? AND timeframe = '15m' AND date LIKE ?
                    ORDER BY date ASC LIMIT 1
                """, (clean_sym, f"{today_str}%"))
                b0 = cur.fetchone()
                conn.close()

                if not b0:
                    return {
                        "is_anchor_window": True,
                        "anchor_milestone": anchor_milestone,
                        "passed": True,
                        "quality_boost": 0.0,
                        "reason": "NO_ORB_BAR_PASS_DEFAULT"
                    }

                b_high, b_low = float(b0[1]), float(b0[2])
                rng = b_high - b_low
                ref_px = current_price if current_price > 0 else float(b0[3])
                pos = (ref_px - b_low) / rng if rng > 0 else 0.5

                if direction_upper in ("BUY", "BULLISH"):
                    passed = pos >= 0.40
                    qb = round((pos - 0.50) * 10.0, 1) if passed else -50.0
                    reason = f"ORB BUY pos {pos:.2f} (>= 0.40 required)"
                else:
                    passed = pos <= 0.60
                    qb = round((0.50 - pos) * 10.0, 1) if passed else -50.0
                    reason = f"ORB SELL pos {pos:.2f} (<= 0.60 required)"

                return {
                    "is_anchor_window": True,
                    "anchor_milestone": anchor_milestone,
                    "passed": passed,
                    "pos_in_range": round(pos, 2),
                    "quality_boost": qb,
                    "reason": reason
                }
            except Exception as e:
                return {
                    "is_anchor_window": True,
                    "anchor_milestone": anchor_milestone,
                    "passed": True,
                    "quality_boost": 0.0,
                    "reason": f"ORB_FALLBACK: {e}"
                }

        # 2. 11:30 & 13:30 Anchors -> Intraday VWAP & Session Return Alignment
        else:
            # Fallback to local 15m OHLCV bars if live_open or live_vwap are not passed in
            if not live_open or not live_vwap or live_open <= 0 or live_vwap <= 0:
                try:
                    today_str = now.strftime("%Y-%m-%d")
                    conn = sqlite3.connect(get_db_path(), timeout=5.0)
                    cur = conn.cursor()
                    clean_sym = ticker if "." in ticker else f"{ticker}.NS"
                    cur.execute("""
                        SELECT open, high, low, close, volume 
                        FROM ohlcv 
                        WHERE ticker = ? AND timeframe = '15m' AND date LIKE ?
                        ORDER BY date ASC
                    """, (clean_sym, f"{today_str}%"))
                    bars = cur.fetchall()
                    conn.close()
                    if bars:
                        if not live_open or live_open <= 0:
                            live_open = float(bars[0][0])
                        if not live_vwap or live_vwap <= 0:
                            sum_pv = sum(((float(b[1]) + float(b[2]) + float(b[3])) / 3.0) * float(b[4] or 1.0) for b in bars)
                            sum_v = sum(float(b[4] or 1.0) for b in bars)
                            if sum_v > 0:
                                live_vwap = sum_pv / sum_v
                except Exception:
                    pass

            ref_px = current_price
            vwap_dist = ((ref_px - live_vwap) / live_vwap * 100.0) if (live_vwap and live_vwap > 0) else 0.0
            day_pct = ((ref_px - live_open) / live_open * 100.0) if (live_open and live_open > 0) else 0.0

            if anchor_milestone == "13:30":
                # Afternoon Breakout Anchor (13:30 IST): Strict Runway & Directional Momentum Gate.
                # Intraday trades taken at 13:30 have barely 95 minutes before mandatory 15:05 square-off.
                # They MUST hold the correct side of VWAP and have positive session momentum (no counter-trend bottom/top picking).
                if direction_upper in ("BUY", "BULLISH"):
                    passed = (vwap_dist >= 0.0) and (day_pct >= 0.10)
                    qb = round(vwap_dist, 2) if passed else -50.0
                    reason = f"13:30 BUY runway momentum: VWAP dist {vwap_dist:+.2f}% (>=0% req), Day {day_pct:+.2f}% (>=+0.10% req)"
                else:
                    passed = (vwap_dist <= 0.0) and (day_pct <= -0.10)
                    qb = round(-vwap_dist, 2) if passed else -50.0
                    reason = f"13:30 SELL runway momentum: VWAP dist {vwap_dist:+.2f}% (<=0% req), Day {day_pct:+.2f}% (<=-0.10% req)"
            else:
                # 11:30 Mid-Day Continuation Anchor: allows healthy pullbacks
                if direction_upper in ("BUY", "BULLISH"):
                    passed = (vwap_dist >= -0.40) and (day_pct >= -1.5)
                    qb = round(vwap_dist, 2) if passed else -50.0
                    reason = f"VWAP BUY dist {vwap_dist:+.2f}%, Day {day_pct:+.2f}%"
                else:
                    passed = (vwap_dist <= +0.40) and (day_pct <= +1.5)
                    qb = round(-vwap_dist, 2) if passed else -50.0
                    reason = f"VWAP SELL dist {vwap_dist:+.2f}%, Day {day_pct:+.2f}%"

            return {
                "is_anchor_window": True,
                "anchor_milestone": anchor_milestone,
                "passed": passed,
                "vwap_dist_pct": round(vwap_dist, 2),
                "day_pct": round(day_pct, 2),
                "quality_boost": qb,
                "reason": reason
            }

    @classmethod
    async def run_consensus_scan(
        cls,
        strategy: str = "INTRADAY",
        universe: str = "ALL_DATABASE_STOCKS",
        top_k: int = 5
    ) -> Dict[str, Any]:
        """
        Executes parallel scans across both Qlib and Legacy engines, enriches trades with
        complete technicalities & model probability breakdowns, and matches setups.
        """
        start_time = time.time()
        scan_id = f"consensus_{strategy.lower()}_{uuid.uuid4().hex[:8]}"
        strat_upper = strategy.upper()

        cls._STOP_REQUESTED = False
        cls._CURRENT_TASK = asyncio.current_task()

        cls._ACTIVE_SCAN = {
            "scan_id": scan_id,
            "strategy": strat_upper,
            "universe": universe,
            "start_time": start_time,
            "started_at": datetime.now().isoformat(),
            "stage": "RESOLVING_UNIVERSE",
            "stage_name": "Universe Discovery",
            "progress": 10,
            "universe_count": 0,
            "message": f"Resolving target equities for {universe}..."
        }

        logger.info(f"[{scan_id}] Starting Dual-Engine Consensus Scan ({strat_upper} | {universe})")
        MasterLogger.log_event(
            category="SCAN_MANUAL",
            event_type="CONSENSUS_SCAN_STARTED",
            message=f"Starting Dual-Engine Consensus Scan ({strat_upper} | {universe})",
            universe=universe,
            details={"scan_id": scan_id, "strategy": strat_upper, "top_k": top_k}
        )

        try:
            from app.analytics.universe_config import resolve_universe_tickers
            u_tickers = resolve_universe_tickers(universe) if universe else None
            total_symbols = len(u_tickers) if u_tickers else 509

            if cls._ACTIVE_SCAN:
                cls._ACTIVE_SCAN.update({
                    "stage": "RUNNING_ENGINES",
                    "stage_name": "Dual-Engine Evaluation",
                    "progress": 35,
                    "universe_count": total_symbols,
                    "message": f"Evaluating {total_symbols} equities concurrently across Microsoft Qlib (Alpha158) & Legacy AI Ensemble..."
                })

            loop = asyncio.get_running_loop()

            # 1. Execute Qlib scan in executor
            def _exec_qlib():
                q_start = time.time()
                try:
                    # Expand candidate pool for confluence synthesis to prevent "Truncate Before Join" starvation.
                    # Qlib provides top-tier candidates (top 50) across universe; final results are sliced to requested top_k.
                    confluence_pool_k = max(top_k * 10, 50)
                    if strat_upper == "SWING":
                        res = QlibScanners.run_swing_scan(universe=universe, tickers=u_tickers, top_k=confluence_pool_k)
                    else:
                        res = QlibScanners.run_intraday_scan(tickers=u_tickers, top_k=confluence_pool_k, universe=universe)
                    res["elapsed"] = time.time() - q_start
                    return res
                except Exception as e:
                    logger.error(f"[{scan_id}] Qlib scan failed: {e}", exc_info=True)
                    return {
                        "status": "ERROR",
                        "error": str(e),
                        "opportunities": [],
                        "rejected": [],
                        "elapsed": time.time() - q_start
                    }

            # 2. Execute Legacy sweep
            async def _exec_legacy():
                l_start = time.time()
                try:
                    tf = "swing" if strat_upper == "SWING" else "intraday"
                    res = await SmartScannerPipeline.run_sweep_batch(
                        timeframe=tf,
                        universe=universe,
                        custom_tickers=u_tickers,
                        min_confidence=50.0
                    )
                    res["elapsed"] = time.time() - l_start
                    return res
                except Exception as e:
                    logger.error(f"[{scan_id}] Legacy sweep failed: {e}", exc_info=True)
                    return {
                        "status": "ERROR",
                        "error": str(e),
                        "candidates": [],
                        "summary": {},
                        "elapsed": time.time() - l_start
                    }

            # Launch concurrently
            qlib_task = loop.run_in_executor(None, _exec_qlib)
            legacy_task = _exec_legacy()

            qlib_res, legacy_res = await asyncio.gather(qlib_task, legacy_task)

            if cls._STOP_REQUESTED:
                logger.info(f"[{scan_id}] Consensus scan stopped by user request.")
                cls._ACTIVE_SCAN = None
                return {
                    "scan_id": scan_id,
                    "strategy": strat_upper,
                    "universe": universe,
                    "status": "STOPPED",
                    "message": "Scan stopped by user request.",
                    "elapsed_seconds": round(time.time() - start_time, 2),
                    "unanimous_trades": [],
                    "conflicting_trades": [],
                    "qlib_solo": [],
                    "legacy_solo": []
                }

            if cls._ACTIVE_SCAN:
                cls._ACTIVE_SCAN.update({
                    "stage": "SYNTHESIZING_CONFLUENCE",
                    "stage_name": "Confluence Synthesis",
                    "progress": 85,
                    "message": "Synthesizing cross-engine confluence, calculating blended risk parameters, and screening conflicts..."
                })

            # 3. Process opportunities
            qlib_opps_raw = qlib_res.get("recommendations") or qlib_res.get("opportunities") or []
            legacy_cands_raw = legacy_res.get("candidates", [])

            # Index by normalized symbol
            qlib_map: Dict[str, Dict[str, Any]] = {}
            for opp in qlib_opps_raw:
                sym = normalize_symbol(opp.get("ticker", ""))
                if sym and sym not in qlib_map:
                    qlib_map[sym] = opp

            legacy_map: Dict[str, Dict[str, Any]] = {}
            for cand in legacy_cands_raw:
                sym = normalize_symbol(cand.get("ticker", ""))
                if sym and sym not in legacy_map:
                    legacy_map[sym] = cand

            all_symbols = set(qlib_map.keys()) | set(legacy_map.keys())

            # Dynamic Regret-Minimizing Consensus Weighting & CUSUM Risk Gate
            active_weights = {"w_qlib": 0.50, "w_legacy": 0.50}
            cusum_penalty = 0.0
            cusum_info: Dict[str, Any] = {}
            try:
                from app.analytics.consensus_learning_engine import ConsensusLearningEngine
                active_weights = ConsensusLearningEngine.get_current_weights(strategy=strat_upper)
                cusum_info = ConsensusLearningEngine.get_cusum_status(strategy=strat_upper)
                cusum_penalty = float(cusum_info.get("conviction_penalty_pct", 0.0))
            except Exception as we:
                logger.debug(f"Consensus weight retrieval note: {we}")

            w_qlib = float(active_weights.get("w_qlib", 0.50))
            w_legacy = float(active_weights.get("w_legacy", 0.50))

            # Retrieve Macro Regime context & NIFTY Trend Filter early for trade gating
            macro_info: Dict[str, Any] = {}
            try:
                from app.analytics.macro_engine import get_macro_regime
                macro_info = get_macro_regime()
            except Exception as me:
                logger.debug(f"Consensus scan macro retrieval note: {me}")

            unanimous_trades: List[Dict[str, Any]] = []
            conflicting_trades: List[Dict[str, Any]] = []
            qlib_solo: List[Dict[str, Any]] = []
            legacy_solo: List[Dict[str, Any]] = []
            shadow_candidates_to_log: List[Dict[str, Any]] = []

            # ── REAL-TIME PRICE ANCHOR ─────────────────────────────────────────────────
            # Fetch ONE bulk Upstox LTP for every qualified symbol that appeared in either
            # engine. This replaces stale Qlib binary-file prices (which can lag 4+ hours)
            # and any stale Legacy candle closes with sub-second Upstox REAL_TIME quotes.
            # Rate limit: get_bulk_live_quotes_with_meta() enforces upstox_limiter internally;
            # one call per scan batch (up to 500 symbols/call) — well within Upstox limits.
            # Falls back gracefully: if Upstox unavailable, returns empty dict → engines'
            # own prices are used unchanged (fail-open, candle close preserved).
            _fresh_ltp_cache: Dict[str, float] = {}
            _fresh_vwap_cache: Dict[str, float] = {}
            _fresh_open_cache: Dict[str, float] = {}
            _sector_leaderboard: Dict[str, Any] = {}

            try:
                from app.data.upstox_provider import fetch_upstox_bulk_quotes
                from app.analytics.sector_engine import SectorEngine

                _all_tickers = [
                    (qlib_map.get(s) or legacy_map.get(s) or {}).get("ticker") or f"{s}.NS"
                    for s in all_symbols
                ]
                _all_tickers = [t for t in _all_tickers if t]
                if _all_tickers:
                    _bulk_quotes = fetch_upstox_bulk_quotes(_all_tickers)
                    for _sym, _q in _bulk_quotes.items():
                        _clean_k = _sym.replace(".NS", "").replace(".BO", "").strip().upper()
                        if _q.get("ltp") and float(_q["ltp"]) > 0:
                            _fresh_ltp_cache[_sym] = float(_q["ltp"])
                            _fresh_ltp_cache[_clean_k] = float(_q["ltp"])
                        if _q.get("vwap") and float(_q["vwap"]) > 0:
                            _fresh_vwap_cache[_sym] = float(_q["vwap"])
                            _fresh_vwap_cache[_clean_k] = float(_q["vwap"])
                        if _q.get("open") and float(_q["open"]) > 0:
                            _fresh_open_cache[_sym] = float(_q["open"])
                            _fresh_open_cache[_clean_k] = float(_q["open"])

                    logger.info(
                        f"[{scan_id}] Upstox bulk quotes: {len(_fresh_ltp_cache)}/{len(_all_tickers)} "
                        f"REAL_TIME prices, VWAP & Open cached"
                    )

                if strat_upper == "INTRADAY":
                    _sector_leaderboard = SectorEngine.get_sector_leaderboard()
            except Exception as _ltp_err:
                logger.warning(f"[{scan_id}] Bulk quote/sector fetch note (non-fatal, using engine prices): {_ltp_err}")
            # ──────────────────────────────────────────────────────────────────────────

            for sym in all_symbols:
                has_qlib = sym in qlib_map
                has_legacy = sym in legacy_map

                if has_qlib and has_legacy:
                    q = qlib_map[sym]
                    l = legacy_map[sym]

                    q_dir = normalize_direction(q.get("direction"))
                    l_dir = normalize_direction(l.get("direction"), is_bullish=l.get("is_bullish"))

                    q_price = float(q.get("entry_price") or q.get("entry") or 0.0)
                    l_price = float(l.get("entry") or l.get("entry_price") or 0.0)

                    # Apply fresh Upstox REAL_TIME price if available for this symbol.
                    # This corrects Qlib binary-data lag (can be 4+ hrs stale) and any
                    # stale Legacy candle closes. Both engines anchor to the same live price.
                    _fresh_ltp = _fresh_ltp_cache.get(sym) or _fresh_ltp_cache.get(f"{sym}.NS")
                    if _fresh_ltp and _fresh_ltp > 0:
                        q_price = _fresh_ltp
                        l_price = _fresh_ltp
                    else:
                        # Fallback when fresh Upstox live feed is unavailable:
                        # Stale Qlib binary prices can lag hours or days behind market reality.
                        # If divergence between q_price and l_price > 5.0%, extreme stale mismatch.
                        if q_price > 0 and l_price > 0:
                            div_pct = abs(q_price - l_price) / min(q_price, l_price) * 100.0
                            if div_pct > 5.0:
                                logger.info(
                                    f"[{scan_id}] Divergent Qlib price ({q_price:.2f}) vs Legacy candle ({l_price:.2f}) "
                                    f"divergence={div_pct:.2f}% > 5.0%. Stale blend prevented; anchoring strictly to Legacy candle."
                                )
                                q_price = l_price

                    blended_entry = round((q_price + l_price) / 2.0 if (q_price and l_price) else (q_price or l_price), 2)

                    q_score = float(q.get("confidence") or q.get("score") or q.get("recommendation_score") or 0.0)
                    q_score_pct = round(q_score * 100.0, 1) if abs(q_score) <= 1.0 else round(q_score, 1)
                    l_score_pct = round(float(l.get("confidence") or 0.0), 1)

                    raw_composite = (w_qlib * abs(q_score_pct)) + (w_legacy * l_score_pct)
                    composite_score = round(max(0.0, raw_composite - cusum_penalty), 1)

                    l_explanation = l.get("explanation") or {}
                    l_base_probs = l.get("base_probs") or {}

                    if q_dir == l_dir:
                        # Unanimous match
                        q_sl = float(q.get("stop_loss") or q.get("sl") or 0.0)
                        l_sl = float(l.get("sl") or l.get("stop_loss") or 0.0)
                        q_tp = float(q.get("target_price") or q.get("tp") or q.get("tp1") or 0.0)
                        l_tp = float(l.get("tp1") or l.get("tp") or l.get("target_price") or 0.0)

                        # Standard baseline risk percentages by strategy horizon
                        default_risk_pct = 0.010 if strat_upper == "INTRADAY" else 0.025
                        default_reward_pct = default_risk_pct * 1.5
                        min_required_rr = 1.25

                        # Calculate relative risk & reward percentages from each engine's own entry anchor
                        # to prevent payoff compression when quote references diverge across engines.
                        q_risk_pct = None
                        l_risk_pct = None
                        q_reward_pct = None
                        l_reward_pct = None

                        if q_dir == "BUY":
                            if q_price > 0 and 0 < q_sl < q_price:
                                q_risk_pct = (q_price - q_sl) / q_price
                            if l_price > 0 and 0 < l_sl < l_price:
                                l_risk_pct = (l_price - l_sl) / l_price
                            if q_price > 0 and q_tp > q_price:
                                q_reward_pct = (q_tp - q_price) / q_price
                            if l_price > 0 and l_tp > l_price:
                                l_reward_pct = (l_tp - l_price) / l_price
                        else:  # SELL
                            if q_price > 0 and q_sl > q_price:
                                q_risk_pct = (q_sl - q_price) / q_price
                            if l_price > 0 and l_sl > l_price:
                                l_risk_pct = (l_sl - l_price) / l_price
                            if q_price > 0 and 0 < q_tp < q_price:
                                q_reward_pct = (q_price - q_tp) / q_price
                            if l_price > 0 and 0 < l_tp < l_price:
                                l_reward_pct = (l_price - l_tp) / l_price

                        # Multi-engine percentage blend weighted by dynamic consensus weights
                        if q_risk_pct is not None and l_risk_pct is not None:
                            blended_risk_pct = (w_qlib * q_risk_pct) + (w_legacy * l_risk_pct)
                        elif q_risk_pct is not None:
                            blended_risk_pct = q_risk_pct
                        elif l_risk_pct is not None:
                            blended_risk_pct = l_risk_pct
                        else:
                            blended_risk_pct = default_risk_pct

                        if q_reward_pct is not None and l_reward_pct is not None:
                            blended_reward_pct = (w_qlib * q_reward_pct) + (w_legacy * l_reward_pct)
                        elif q_reward_pct is not None:
                            blended_reward_pct = q_reward_pct
                        elif l_reward_pct is not None:
                            blended_reward_pct = l_reward_pct
                        else:
                            blended_reward_pct = default_reward_pct

                        # Enforce non-zero risk floor and institutional R:R sanity floor
                        blended_risk_pct = max(0.003, blended_risk_pct)
                        if blended_reward_pct < (blended_risk_pct * min_required_rr):
                            blended_reward_pct = round(blended_risk_pct * min_required_rr, 4)

                        # Project consistent, non-inverted SL/TP levels from blended entry
                        if q_dir == "BUY":
                            blended_sl = round(blended_entry * (1.0 - blended_risk_pct), 2)
                            blended_tp = round(blended_entry * (1.0 + blended_reward_pct), 2)
                        else:
                            blended_sl = round(blended_entry * (1.0 + blended_risk_pct), 2)
                            blended_tp = round(blended_entry * (1.0 - blended_reward_pct), 2)

                        risk = abs(blended_entry - blended_sl) if blended_sl else 1.0
                        reward = abs(blended_tp - blended_entry) if blended_tp else 2.0
                        rr_ratio = round(reward / risk, 2) if risk > 0 else 2.0

                        # Precision Gating for Intraday:
                        # 1. Statistical Conviction Floor (>= 50.0% directional edge)
                        MIN_CONVICTION_FLOOR = 50.0
                        conviction_ok = (composite_score >= MIN_CONVICTION_FLOOR) if strat_upper == "INTRADAY" else True

                        # 2. Sector Momentum Alignment Gate
                        sec_ok, sec_reason, sec_telem = (True, "Sector aligned", {})
                        if strat_upper == "INTRADAY":
                            from app.analytics.sector_engine import SectorEngine
                            sec_ok, sec_reason, sec_telem = SectorEngine.is_sector_aligned(sym, q_dir, leaderboard=_sector_leaderboard)

                        # 3. Institutional VWAP & Day's Open Gating
                        vwap_ok = True
                        vwap_reason = "VWAP aligned"
                        live_vwap = _fresh_vwap_cache.get(sym)
                        live_open = _fresh_open_cache.get(sym)

                        if strat_upper == "INTRADAY":
                            now_dt = datetime.now()
                            is_orb_window = (now_dt.hour < 10)

                            if is_orb_window:
                                # 09:15-10:00 IST Opening Range: strict breakout confirmation above Open & VWAP
                                if q_dir == "BUY":
                                    if live_vwap and blended_entry < (live_vwap * 0.999):
                                        vwap_ok = False
                                        vwap_reason = f"Entry ₹{blended_entry:.2f} < VWAP ₹{live_vwap:.2f}"
                                    elif live_open and blended_entry < (live_open * 0.999):
                                        vwap_ok = False
                                        vwap_reason = f"Entry ₹{blended_entry:.2f} < Open ₹{live_open:.2f}"
                                elif q_dir == "SELL":
                                    if live_vwap and blended_entry > (live_vwap * 1.001):
                                        vwap_ok = False
                                        vwap_reason = f"Entry ₹{blended_entry:.2f} > VWAP ₹{live_vwap:.2f}"
                                    elif live_open and blended_entry > (live_open * 1.001):
                                        vwap_ok = False
                                        vwap_reason = f"Entry ₹{blended_entry:.2f} > Open ₹{live_open:.2f}"
                            else:
                                # 10:00-15:30 IST Established Session: align with Institutional Timed Anchor tolerance
                                # Allows institutional pullbacks within -0.40% of VWAP and -1.50% of Open for Longs (and inverse for Shorts)
                                if q_dir == "BUY":
                                    if live_vwap and ((blended_entry - live_vwap) / live_vwap * 100.0) < -0.40:
                                        vwap_ok = False
                                        vwap_reason = f"Entry ₹{blended_entry:.2f} < VWAP -0.40% (₹{live_vwap:.2f})"
                                    elif live_open and ((blended_entry - live_open) / live_open * 100.0) < -1.50:
                                        vwap_ok = False
                                        vwap_reason = f"Entry ₹{blended_entry:.2f} < Open -1.50% (₹{live_open:.2f})"
                                elif q_dir == "SELL":
                                    if live_vwap and ((blended_entry - live_vwap) / live_vwap * 100.0) > 0.40:
                                        vwap_ok = False
                                        vwap_reason = f"Entry ₹{blended_entry:.2f} > VWAP +0.40% (₹{live_vwap:.2f})"
                                    elif live_open and ((blended_entry - live_open) / live_open * 100.0) > 1.50:
                                        vwap_ok = False
                                        vwap_reason = f"Entry ₹{blended_entry:.2f} > Open +1.50% (₹{live_open:.2f})"

                        # 4. NIFTY 50 Intraday Macro Guard Gate
                        macro_ok = True
                        macro_reason = "Macro index aligned"
                        if strat_upper == "INTRADAY" and macro_info:
                            try:
                                from app.analytics.macro_engine import is_direction_permitted
                                macro_ok, macro_reason = is_direction_permitted(
                                    direction=q_dir,
                                    trade_type="INTRADAY",
                                    macro_state=macro_info
                                )
                            except Exception as me:
                                logger.debug(f"Macro check skipped for {sym}: {me}")

                        # 5. Live Market Target/SL Runway Sanity Gate
                        # Ensure the target has not already been achieved and SL has not been breached in live market.
                        # If the live market is already at or past the target (or stop loss), this setup is unviable.
                        target_runway_ok = True
                        runway_reason = "Target and SL runway viable"
                        check_px = _fresh_ltp or blended_entry

                        if q_dir == "BUY":
                            if blended_tp > 0 and check_px >= blended_tp:
                                target_runway_ok = False
                                runway_reason = f"Price ₹{check_px:.2f} already reached or exceeded target ₹{blended_tp:.2f}"
                            elif blended_sl > 0 and check_px <= blended_sl:
                                target_runway_ok = False
                                runway_reason = f"Price ₹{check_px:.2f} already breached stop loss ₹{blended_sl:.2f}"
                        else:  # SELL
                            if blended_tp > 0 and check_px <= blended_tp:
                                target_runway_ok = False
                                runway_reason = f"Price ₹{check_px:.2f} already reached or exceeded target ₹{blended_tp:.2f}"
                            elif blended_sl > 0 and check_px >= blended_sl:
                                target_runway_ok = False
                                runway_reason = f"Price ₹{check_px:.2f} already breached stop loss ₹{blended_sl:.2f}"

                        is_actionable = bool(conviction_ok and sec_ok and vwap_ok and macro_ok and target_runway_ok)
                        precision_reasons = []
                        if not conviction_ok:
                            precision_reasons.append(f"Low Conviction ({composite_score:.1f}% < {MIN_CONVICTION_FLOOR}%)")
                        if not sec_ok:
                            precision_reasons.append(sec_reason)
                        if not vwap_ok:
                            precision_reasons.append(vwap_reason)
                        if not macro_ok:
                            precision_reasons.append(macro_reason)
                        if not target_runway_ok:
                            precision_reasons.append(runway_reason)

                        precision_status = "QUALIFIED" if is_actionable else f"BLOCKED: {', '.join(precision_reasons)}"

                        # Evaluate Step 2 Institutional Anchor Quant Filters for Timed Scans
                        timed_anchor_eval = cls.evaluate_timed_anchor_filter(
                            ticker=sym,
                            direction=q_dir,
                            current_price=blended_entry,
                            live_vwap=live_vwap,
                            live_open=live_open
                        )

                        _cand_dict = {
                            "ticker": l.get("ticker") or q.get("ticker") or f"{sym}.NS",
                            "symbol": sym,
                            "direction": q_dir,
                            "strategy": strat_upper,
                            "trade_type": strat_upper,
                            "timeframe": "15m" if strat_upper == "INTRADAY" else "1d",
                            "confluence_status": "UNANIMOUS",
                            "composite_score": composite_score,
                            "entry_price": blended_entry,
                            "entry": blended_entry,
                            "stop_loss": blended_sl,
                            "sl": blended_sl,
                            "target_price": blended_tp,
                            "tp": blended_tp,
                            "blended_entry": blended_entry,
                            "blended_sl": blended_sl,
                            "blended_tp": blended_tp,
                            "risk_reward_ratio": rr_ratio,
                            "is_actionable": is_actionable,
                            "precision_status": precision_status,
                            "precision_reasons": precision_reasons,
                            "macro_aligned": macro_ok,
                            "macro_reason": macro_reason,
                            "vwap": live_vwap,
                            "day_open": live_open,
                            "timed_anchor_quality": timed_anchor_eval,
                            "sector": sec_telem.get("sector"),
                            "sector_pct_from_open": sec_telem.get("sector_pct_from_open"),
                            "sector_trend": sec_telem.get("sector_trend"),
                            "qlib_metrics": {
                                "direction": q_dir,
                                "score": q_score_pct,
                                "raw_prediction": q.get("raw_prediction", q_score),
                                "calibrated_probability": q.get("calibrated_probability", q_score_pct),
                                "rank_percentile": q.get("rank_percentile"),
                                "rank": q.get("rank", 1),
                                "entry": q_price,
                                "stop_loss": q_sl,
                                "target_price": q_tp,
                                "atr_14": q.get("atr_14"),
                                "model_id": q.get("model_id")
                            },
                            "legacy_metrics": {
                                "direction": l_dir,
                                "confidence": l_score_pct,
                                "raw_confidence": l.get("raw_confidence", l_score_pct),
                                "entry": l_price,
                                "sl": l_sl,
                                "tp1": l_tp,
                                "tp2": l.get("tp2"),
                                "base_probs": l_base_probs,
                                "explanation": l_explanation
                            }
                        }

                        # Consensus Meta-Learner V2 (De Prado Meta-Labeling Challenger)
                        try:
                            from app.analytics.consensus_meta_learner_v2 import ConsensusMetaLearnerV2
                            _v2_eval = ConsensusMetaLearnerV2().predict_candidate(_cand_dict)
                            _cand_dict["meta_learner_v2"] = {
                                "base_conviction": _v2_eval.get("base_conviction", composite_score),
                                "meta_adjustment": _v2_eval.get("meta_adjustment", 0.0),
                                "final_conviction": _v2_eval.get("final_conviction", composite_score),
                                "calibrated_probability": _v2_eval.get("calibrated_probability"),
                                "group_attributions": _v2_eval.get("group_attributions", {}),
                                "status": _v2_eval.get("status", "UNAVAILABLE")
                            }
                        except Exception as _v2_err:
                            logger.debug(f"Meta-Learner V2 evaluation note: {_v2_err}")
                            _cand_dict["meta_learner_v2"] = {
                                "base_conviction": composite_score,
                                "meta_adjustment": 0.0,
                                "final_conviction": composite_score,
                                "group_attributions": {},
                                "status": "ERROR"
                            }

                        unanimous_trades.append(_cand_dict)
                        shadow_candidates_to_log.append({
                            "ticker": l.get("ticker") or q.get("ticker") or f"{sym}.NS",
                            "symbol": sym,
                            "qlib_direction": q_dir,
                            "qlib_score": q_score_pct,
                            "qlib_price": q_price,
                            "legacy_direction": l_dir,
                            "legacy_confidence": l_score_pct,
                            "legacy_price": l_price,
                            "composite_score": composite_score,
                            "action_taken": "TRADED_UNANIMOUS" if is_actionable else "FILTERED_PRECISION_GATE",
                            "entry_ref_price": blended_entry,
                            "details": {
                                "risk_reward_ratio": rr_ratio,
                                "blended_sl": blended_sl,
                                "blended_tp": blended_tp,
                                "is_actionable": is_actionable,
                                "precision_status": precision_status
                            }
                        })
                    else:
                        # Directional conflict
                        conflicting_trades.append({
                            "ticker": l.get("ticker") or q.get("ticker") or f"{sym}.NS",
                            "symbol": sym,
                            "strategy": strat_upper,
                            "trade_type": strat_upper,
                            "timeframe": "15m" if strat_upper == "INTRADAY" else "1d",
                            "confluence_status": "CONFLICT",
                            "warning": "Directional conflict: Quantitative alpha and momentum indicators disagree. High risk of whipsaw.",
                            "qlib_direction": q_dir,
                            "qlib_score": q_score_pct,
                            "legacy_direction": l_dir,
                            "legacy_confidence": l_score_pct,
                            "entry": blended_entry,
                            "entry_price": blended_entry,
                            "qlib_metrics": {
                                "direction": q_dir,
                                "score": q_score_pct,
                                "raw_prediction": q.get("raw_prediction", q_score),
                                "rank_percentile": q.get("rank_percentile"),
                                "entry": q_price,
                                "stop_loss": q.get("stop_loss") or q.get("sl"),
                                "target_price": q.get("target_price") or q.get("tp")
                            },
                            "legacy_metrics": {
                                "direction": l_dir,
                                "confidence": l_score_pct,
                                "raw_confidence": l.get("raw_confidence", l_score_pct),
                                "entry": l_price,
                                "sl": l.get("sl"),
                                "tp1": l.get("tp1"),
                                "tp2": l.get("tp2"),
                                "base_probs": l_base_probs,
                                "explanation": l_explanation
                            }
                        })
                        shadow_candidates_to_log.append({
                            "ticker": l.get("ticker") or q.get("ticker") or f"{sym}.NS",
                            "symbol": sym,
                            "qlib_direction": q_dir,
                            "qlib_score": q_score_pct,
                            "qlib_price": q_price,
                            "legacy_direction": l_dir,
                            "legacy_confidence": l_score_pct,
                            "legacy_price": l_price,
                            "composite_score": composite_score,
                            "action_taken": "CONFLICT_BLOCKED",
                            "entry_ref_price": blended_entry,
                            "details": {"warning": "Directional conflict"}
                        })

                elif has_qlib:
                    q = qlib_map[sym]
                    q_dir = normalize_direction(q.get("direction"))
                    q_score = float(q.get("score") or q.get("recommendation_score") or 0.0)
                    q_score_pct = round(q_score * 100.0, 1) if abs(q_score) <= 1.0 else round(q_score, 1)
                    orig_q_entry = float(q.get("entry_price") or q.get("entry") or 0.0)
                    q_entry = orig_q_entry
                    q_sl = float(q.get("stop_loss") or q.get("sl") or 0.0)
                    q_tp = float(q.get("target_price") or q.get("tp") or 0.0)

                    # Apply fresh Upstox LTP for qlib_solo branch and dynamically re-anchor SL/TP
                    if _fresh_ltp_cache.get(sym, 0) > 0:
                        q_entry = _fresh_ltp_cache[sym]
                        if orig_q_entry > 0 and q_sl > 0:
                            if q_dir == "BUY":
                                risk_pct = (orig_q_entry - q_sl) / orig_q_entry
                                reward_pct = (q_tp - orig_q_entry) / orig_q_entry if q_tp > orig_q_entry else 0.02
                                q_sl = round(q_entry * (1.0 - max(0.003, risk_pct)), 2)
                                q_tp = round(q_entry * (1.0 + max(0.005, reward_pct)), 2)
                            else:
                                risk_pct = (q_sl - orig_q_entry) / orig_q_entry
                                reward_pct = (orig_q_entry - q_tp) / orig_q_entry if (0 < q_tp < orig_q_entry) else 0.02
                                q_sl = round(q_entry * (1.0 + max(0.003, risk_pct)), 2)
                                q_tp = round(q_entry * (1.0 - max(0.005, reward_pct)), 2)

                    qlib_solo.append({
                        "ticker": q.get("ticker") or f"{sym}.NS",
                        "symbol": sym,
                        "direction": q_dir,
                        "strategy": strat_upper,
                        "trade_type": strat_upper,
                        "timeframe": "15m" if strat_upper == "INTRADAY" else "1d",
                        "source": "QLIB_SOLO",
                        "score": q_score_pct,
                        "raw_prediction": q.get("raw_prediction", q_score),
                        "calibrated_probability": q.get("calibrated_probability", q_score_pct),
                        "rank_percentile": q.get("rank_percentile"),
                        "rank": q.get("rank", 1),
                        "entry": q_entry,
                        "entry_price": q_entry,
                        "sl": q_sl,
                        "stop_loss": q_sl,
                        "tp": q_tp,
                        "target_price": q_tp,
                        "atr_14": q.get("atr_14"),
                        "model_id": q.get("model_id"),
                        "model_hash": q.get("model_hash"),
                        "pnl_status": q.get("pnl_status", "FRESH")
                    })
                    shadow_candidates_to_log.append({
                        "ticker": q.get("ticker") or f"{sym}.NS",
                        "symbol": sym,
                        "qlib_direction": q_dir,
                        "qlib_score": q_score_pct,
                        "qlib_price": q_entry,
                        "legacy_direction": None,
                        "legacy_confidence": None,
                        "legacy_price": None,
                        "composite_score": q_score_pct,
                        "action_taken": "SOLO_QLIB",
                        "entry_ref_price": q_entry,
                        "details": {"rank": q.get("rank", 1)}
                    })

                elif has_legacy:
                    l = legacy_map[sym]
                    l_dir = normalize_direction(l.get("direction"), is_bullish=l.get("is_bullish"))
                    l_score_pct = round(float(l.get("confidence") or 0.0), 1)
                    orig_l_entry = float(l.get("entry") or 0.0)
                    l_entry = orig_l_entry
                    l_sl = float(l.get("sl") or 0.0)
                    l_tp1 = float(l.get("tp1") or 0.0)
                    l_tp2 = float(l.get("tp2") or 0.0)

                    # Apply fresh Upstox LTP for legacy_solo branch too and re-anchor SL/TP
                    if _fresh_ltp_cache.get(sym, 0) > 0:
                        l_entry = _fresh_ltp_cache[sym]
                        if orig_l_entry > 0:
                            delta = l_entry - orig_l_entry
                            l_sl = round(l_sl + delta, 2)
                            l_tp1 = round(l_tp1 + delta, 2)
                            l_tp2 = round(l_tp2 + delta, 2) if l_tp2 > 0 else round(l_tp1 * 1.01, 2)

                    legacy_solo.append({
                        "ticker": l.get("ticker") or f"{sym}.NS",
                        "symbol": sym,
                        "direction": l_dir,
                        "strategy": strat_upper,
                        "trade_type": strat_upper,
                        "timeframe": "15m" if strat_upper == "INTRADAY" else "1d",
                        "source": "LEGACY_SOLO",
                        "confidence": l_score_pct,
                        "raw_confidence": l.get("raw_confidence", l_score_pct),
                        "entry": l_entry,
                        "entry_price": l_entry,
                        "sl": l_sl,
                        "stop_loss": l_sl,
                        "tp": l_tp1,
                        "tp1": l_tp1,
                        "target_price": l_tp1,
                        "tp2": l_tp2,
                        "base_probs": l.get("base_probs", {}),
                        "explanation": l.get("explanation", {}),
                        "timestamp": l.get("timestamp") or datetime.now().isoformat()
                    })
                    shadow_candidates_to_log.append({
                        "ticker": l.get("ticker") or f"{sym}.NS",
                        "symbol": sym,
                        "qlib_direction": None,
                        "qlib_score": None,
                        "qlib_price": None,
                        "legacy_direction": l_dir,
                        "legacy_confidence": l_score_pct,
                        "legacy_price": l_entry,
                        "composite_score": l_score_pct,
                        "action_taken": "SOLO_LEGACY",
                        "entry_ref_price": l_entry,
                        "details": {"confidence": l_score_pct}
                    })

            # Sort results
            unanimous_trades.sort(key=lambda x: x["composite_score"], reverse=True)
            conflicting_trades.sort(key=lambda x: max(abs(x["qlib_score"]), x["legacy_confidence"]), reverse=True)
            qlib_solo.sort(key=lambda x: abs(x["score"]), reverse=True)
            legacy_solo.sort(key=lambda x: x["confidence"], reverse=True)

            # Retrieve Macro Regime context if not already fetched
            if not macro_info:
                try:
                    from app.analytics.macro_engine import get_macro_regime
                    macro_info = get_macro_regime()
                except Exception:
                    pass

            elapsed = round(time.time() - start_time, 2)
            result_payload = {
                "scan_id": scan_id,
                "strategy": strat_upper,
                "universe": universe,
                "status": "SUCCESS",
                "elapsed_seconds": elapsed,
                "completed_at": datetime.now().isoformat(),
                "macro_regime": {
                    "verdict": macro_info.get("regime_verdict", "NEUTRAL_CHOP"),
                    "intraday_verdict": macro_info.get("intraday_regime_verdict", "INTRADAY_NEUTRAL_CHOP"),
                    "intraday_change_pct": macro_info.get("intraday_change_pct", 0.0),
                    "intraday_open_change_pct": macro_info.get("intraday_open_change_pct", 0.0),
                    "nifty_close": macro_info.get("nifty_close", 0.0),
                    "ema_20": macro_info.get("ema_20", 0.0),
                    "distance_ema20_pct": macro_info.get("distance_ema20_pct", 0.0),
                    "vix_status": macro_info.get("vix_status", "NORMAL")
                },
                "summary": {
                    "unanimous_count": len(unanimous_trades),
                    "conflict_count": len(conflicting_trades),
                    "qlib_solo_count": len(qlib_solo),
                    "legacy_solo_count": len(legacy_solo),
                    "total_unique_symbols": len(all_symbols)
                },
                "engines": {
                    "qlib": {
                        "status": qlib_res.get("status", "SUCCESS"),
                        "error": qlib_res.get("error"),
                        "elapsed_seconds": round(qlib_res.get("elapsed", 0.0), 2),
                        "opportunities_found": len(qlib_opps_raw),
                        "data_freshness": qlib_res.get("data_freshness")
                    },
                    "legacy": {
                        "status": legacy_res.get("status", "SUCCESS"),
                        "error": legacy_res.get("error"),
                        "elapsed_seconds": round(legacy_res.get("elapsed", 0.0), 2),
                        "candidates_found": len(legacy_cands_raw)
                    }
                },
                "unanimous_trades": unanimous_trades[:top_k],
                "consensus_trades": unanimous_trades[:top_k],
                "conflicting_trades": conflicting_trades[:top_k],
                "qlib_solo": qlib_solo[:top_k],
                "legacy_solo": legacy_solo[:top_k],
                "active_weights": active_weights,
                "cusum_risk_gate": {
                    "penalty_applied": cusum_penalty > 0,
                    "conviction_penalty": cusum_penalty,
                    "status": cusum_info.get("status", "NORMAL")
                },
                "screened_out_trades": {
                    "qlib_rejected": qlib_res.get("rejected", []),
                    "legacy_rejected_count": legacy_res.get("summary", {}).get("rejected_count", 0),
                    "macro_regime_verdict": macro_info.get("regime_verdict", "NEUTRAL_CHOP"),
                    "macro_regime_reason": (
                        f"NIFTY 50 is in {macro_info.get('regime_verdict')}: Long breakouts strictly suppressed by Fail-Closed Macro Gating."
                        if macro_info.get("regime_verdict") == "BEAR_DISTRIBUTION" else None
                    )
                }
            }

            # Persist latest scan result
            cls._save_latest_scan(strat_upper, result_payload)

            # Persist unanimous trades to consensus_trade_history for live tracking (Manual & Daemon)
            try:
                from app.analytics.consensus_tracker import ConsensusTracker
                for u_trade in unanimous_trades:
                    # Persist all discovered unanimous trades with full precision metadata attached
                    ConsensusTracker.save_consensus_trade(u_trade, strategy=strat_upper, source="MANUAL")

                # Persist evaluated shadow candidates for off-policy credit assignment & regret evaluation
                if shadow_candidates_to_log:
                    ConsensusTracker.log_shadow_candidates(
                        candidates=shadow_candidates_to_log,
                        scan_id=scan_id,
                        strategy=strat_upper
                    )

                # Ensure Arena slot entry alerts are immediately evaluated & dispatched
                try:
                    from app.analytics.consensus_game_engine import ConsensusGameEngine
                    ConsensusGameEngine.sync_and_dispatch_arena_alerts()
                except Exception as alert_err:
                    logger.debug(f"[ConsensusScanner] Arena alert dispatch error: {alert_err}")
            except Exception as pe:
                logger.warning(f"Failed to persist unanimous trades or shadow candidates: {pe}")


            MasterLogger.log_event(
                category="SCAN_MANUAL",
                event_type="CONSENSUS_SCAN_COMPLETED",
                message=f"Consensus Scan {scan_id} finished in {elapsed}s (Unanimous: {len(unanimous_trades)}, Conflicts: {len(conflicting_trades)})",
                universe=universe,
                details={
                    "scan_id": scan_id,
                    "unanimous": len(unanimous_trades),
                    "conflicts": len(conflicting_trades),
                    "elapsed": elapsed
                }
            )

            return result_payload

        finally:
            cls._ACTIVE_SCAN = None
            cls._CURRENT_TASK = None
