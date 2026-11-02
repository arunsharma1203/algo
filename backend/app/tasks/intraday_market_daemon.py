"""
15-MINUTE MARKET-HOURS DATA HOARDING & OOS EVALUATION DAEMON
============================================================
Automated background pipeline running strictly during Indian market hours (09:15 to 15:30 IST Mon-Fri).

Key Responsibilities:
1. Market-Hours Gate: Runs only when market is open; sleeps with zero CPU/network when closed.
2. Ingest 15m candles for F&O Underlyings & liquid universe into SQLite ohlcv storage.
3. Synchronize Qlib binary provider (calendars/15min.txt and binary feature arrays).
4. Advance forward Out-Of-Sample (OOS) bars for any pending research candidates.
5. Reconcile open virtual recommendations against latest prices for SL/TP hits.
"""

import time
import logging
from datetime import datetime
from typing import Dict, Any, List

from app.data.data_gateway import DataGateway
from app.qlib_engine.india_data_adapter import get_qlib_adapter
from app.qlib_engine.fno_universe import resolve_fno_universe
from app.qlib_engine.virtual_tracker import QlibVirtualTracker
from app.analytics.research_orchestrator.research_orchestrator import ResearchOrchestrator
from app.analytics.research_orchestrator.candidate_vault import CandidateVault
from app.analytics.master_logger import MasterLogger
from app.data.historical_data_layer import get_db_path

logger = logging.getLogger(__name__)


def init_anchor_schedule_ledger(db_path: str = None) -> None:
    path = db_path or get_db_path()
    import sqlite3
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS consensus_anchor_schedule_ledger (
                date TEXT,
                anchor_name TEXT,
                scheduled_time TEXT,
                status TEXT,
                executed_at TEXT,
                scan_id TEXT,
                trades_count INTEGER,
                PRIMARY KEY (date, anchor_name)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def check_and_recover_missed_anchors(force_run: bool = False, db_path: str = None) -> List[Dict[str, Any]]:
    """
    Operational recovery for missed timed anchors (09:30, 11:30, 13:30).
    Runs strictly during active market hours.
    Idempotent: if an anchor has already run or been recovered, it is skipped.
    Safe: only triggers if within a 30-minute recovery window; expired anchors are skipped.
    Zero duplicate scans, zero duplicate trades.
    """
    init_anchor_schedule_ledger(db_path)
    now_dt = datetime.now()
    today_str = now_dt.strftime("%Y-%m-%d")
    now_m = now_dt.hour * 60 + now_dt.minute

    anchors = {
        "09:30": 570,
        "11:30": 690,
        "13:30": 810
    }

    import sqlite3
    path = db_path or get_db_path()
    conn = sqlite3.connect(path, timeout=10.0)
    recovered = []
    try:
        cur = conn.cursor()
        rows = cur.execute("SELECT anchor_name, status FROM consensus_anchor_schedule_ledger WHERE date = ?", (today_str,)).fetchall()
        status_map = {r[0]: r[1] for r in rows}

        for anchor_name, tgt_m in anchors.items():
            if anchor_name in status_map:
                continue

            if now_m >= tgt_m:
                elapsed_m = now_m - tgt_m
                if elapsed_m <= 30 or force_run:
                    logger.warning(
                        f"[MarketDaemon] MISSED ANCHOR DETECTED: {anchor_name} was missed ({elapsed_m}m ago). "
                        f"Executing single safe recovery scan."
                    )
                    from app.analytics.consensus_scanner import ConsensusScanner
                    from app.analytics.consensus_tracker import ConsensusTracker
                    import asyncio

                    recov_scan_id = f"recovery_{anchor_name.replace(':', '')}_{today_str}_{int(time.time())}"
                    c_scan_res = asyncio.run(
                        ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="ALL_DATABASE_STOCKS", top_k=10)
                    )
                    c_trades = c_scan_res.get("unanimous_trades") or c_scan_res.get("consensus_trades", [])
                    saved_count = 0
                    for tr in c_trades:
                        tr_ts = tr.get("decision_timestamp") or tr.get("timestamp")
                        saved, _ = ConsensusTracker.save_consensus_trade(
                            tr, strategy="INTRADAY", source="ANCHOR_RECOVERY", decision_timestamp=tr_ts
                        )
                        if saved:
                            saved_count += 1

                    try:
                        from app.analytics.consensus_game_engine import ConsensusGameEngine
                        ConsensusGameEngine.sync_and_dispatch_arena_alerts()
                    except Exception:
                        pass

                    cur.execute("""
                        INSERT OR REPLACE INTO consensus_anchor_schedule_ledger (
                            date, anchor_name, scheduled_time, status, executed_at, scan_id, trades_count
                        ) VALUES (?, ?, ?, 'RECOVERED', ?, ?, ?)
                    """, (today_str, anchor_name, f"{anchor_name}:00", now_dt.isoformat(), recov_scan_id, len(c_trades)))
                    conn.commit()

                    recovered.append({
                        "anchor": anchor_name,
                        "status": "RECOVERED",
                        "scan_id": recov_scan_id,
                        "trades": len(c_trades),
                        "saved": saved_count
                    })
                else:
                    logger.info(f"[MarketDaemon] Missed anchor {anchor_name} expired ({elapsed_m}m past scheduled time). Marking MISSED_EXPIRED.")
                    cur.execute("""
                        INSERT OR REPLACE INTO consensus_anchor_schedule_ledger (
                            date, anchor_name, scheduled_time, status, executed_at, scan_id, trades_count
                        ) VALUES (?, ?, ?, 'MISSED_EXPIRED', ?, NULL, 0)
                    """, (today_str, anchor_name, f"{anchor_name}:00", now_dt.isoformat()))
                    conn.commit()
    finally:
        conn.close()

    return recovered


def run_15m_market_tick_pipeline(force_run: bool = False) -> Dict[str, Any]:
    """
    Authoritative 15-minute market-hours daemon tick.
    Safe, fail-closed, rate-limit protected.
    """
    start_time = time.time()
    now_iso = datetime.now().isoformat()

    # 1. Market-Hours Gate
    is_open = DataGateway.is_market_open(force_override=force_run)
    if not is_open:
        logger.info("[MarketDaemon] Market is currently closed (Active: Mon-Fri 09:15-15:30 IST). 15m tick skipped.")
        return {
            "status": "MARKET_CLOSED",
            "message": "Market is closed. Daemon sleeping until next session.",
            "is_market_open": False,
            "timestamp": now_iso
        }

    logger.info(f"[MarketDaemon] Active market session detected. Starting 15-minute tick at {now_iso}...")
    MasterLogger.log_event(
        category="SCHEDULER",
        event_type="MARKET_TICK_15M_STARTED",
        message=f"Starting 15-minute market tick at {now_iso}",
        details={"force_run": force_run}
    )

    # 2. Resolve target universe — use ALL_DATABASE_STOCKS as the primary source
    target_symbols = []
    try:
        from app.analytics.universe_config import resolve_universe_tickers
        target_symbols = resolve_universe_tickers("ALL_DATABASE_STOCKS")
        logger.info(f"[MarketDaemon] Primary universe resolved: {len(target_symbols)} stocks from ALL_DATABASE_STOCKS")
    except Exception as e:
        logger.warning(f"[MarketDaemon] ALL_DATABASE_STOCKS resolution error: {e}")

    try:
        fno_res = resolve_fno_universe(force_refresh=False)
        fno_symbols = fno_res.get("symbols", []) if isinstance(fno_res, dict) else (fno_res or [])
        fno_clean = [s.replace(".NS", "").replace(".BO", "").strip().upper() for s in fno_symbols if isinstance(s, str)]
        pre_count = len(target_symbols)
        target_set = set(s.replace(".NS", "").replace(".BO", "").strip().upper() for s in target_symbols)
        for sym in fno_clean:
            if sym not in target_set:
                target_symbols.append(sym)
                target_set.add(sym)
        if len(target_symbols) > pre_count:
            logger.info(f"[MarketDaemon] F&O enrichment added {len(target_symbols) - pre_count} additional symbols")
    except Exception as e:
        logger.info(f"[MarketDaemon] F&O enrichment skipped (non-critical): {e}")

    if not target_symbols:
        logger.warning("[MarketDaemon] Universe resolution failed — falling back to liquid large-caps only")
        target_symbols = [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
            "BHARTIARTL", "ITC", "KOTAKBANK", "LT", "TATAMOTORS", "AXISBANK"
        ]

    clean_symbols = [s.replace(".NS", "").replace(".BO", "").strip().upper() for s in target_symbols]

    # 3. Continuous WebSocket Ingestion & Deterministic 15m Bar Finalization Phase
    from app.data.upstox_stream_service import get_stream_service
    stream_svc = get_stream_service()
    if not stream_svc._is_running:
        stream_svc.start()

    # Flush completed 15m bars constructed locally from continuous 1m WebSocket stream
    ws_flushed_count = stream_svc.aggregate_and_flush_completed_bars()
    try:
        from app.data.index_candle_ingestion_prototype import get_index_candle_ingestion_service
        get_index_candle_ingestion_service().heal_intraday_gaps_if_needed()
    except Exception as _idx_heal_err:
        logger.debug(f"[MarketDaemon] Index 1m gap self-heal note: {_idx_heal_err}")
    logger.info(
        f"[MarketDaemon] Continuous WebSocket Feed active. Flushed {ws_flushed_count} "
        f"completed 15m bars to SQLite across ~507 universe (zero REST quota consumed)."
    )

    # Reconnect / Gap Recovery Fallback:
    # Check if WebSocket stream is disconnected or if any priority/arena symbols missed session bars
    hoarded_count = ws_flushed_count
    hoarded_symbols = list(clean_symbols)
    now_dt = datetime.now()
    today_str = now_dt.strftime('%Y-%m-%d')

    from app.data.historical_data_layer import get_db_path
    import sqlite3

    session_slots = stream_svc.get_market_session_15m_slots(now_dt)
    completed_slots = [
        (s_start, s_end)
        for s_start, s_end in session_slots
        if now_dt >= s_end
    ]

    missing_session_symbols = []
    if completed_slots:
        latest_slot_start = completed_slots[-1][0].strftime("%Y-%m-%d %H:%M:%S")
        _conn = sqlite3.connect(get_db_path(), timeout=10.0)
        _conn.execute("PRAGMA busy_timeout = 10000;")
        _latest_rows = _conn.execute(
            "SELECT DISTINCT ticker FROM ohlcv WHERE timeframe = '15m' AND date = ?",
            (latest_slot_start,)
        ).fetchall()
        _conn.close()
        _latest_symbols = {r[0].replace(".NS", "").replace(".BO", "").strip().upper() for r in _latest_rows if r[0]}
        missing_session_symbols = [s for s in clean_symbols if s not in _latest_symbols]
    else:
        _conn = sqlite3.connect(get_db_path(), timeout=10.0)
        _conn.execute("PRAGMA busy_timeout = 10000;")
        _today_rows = _conn.execute(
            "SELECT DISTINCT ticker FROM ohlcv WHERE timeframe = '15m' AND date >= ?",
            (f"{today_str} 09:15:00",)
        ).fetchall()
        _conn.close()
        _today_symbols = {r[0].replace(".NS", "").replace(".BO", "").strip().upper() for r in _today_rows if r[0]}
        missing_session_symbols = [s for s in clean_symbols if s not in _today_symbols]

    if missing_session_symbols:
        logger.info(
            f"[MarketDaemon] Detected {len(missing_session_symbols)} symbols missing session 15m bars "
            f"(target slot: {completed_slots[-1][0].strftime('%H:%M') if completed_slots else '09:15'}). "
            "Engaging rate-governed REST gap recovery for top priority symbols..."
        )
        open_arena_symbols = []
        try:
            from app.analytics.consensus_game_engine import ConsensusGameEngine
            arena_open = ConsensusGameEngine.get_positions(status="OPEN")
            open_arena_symbols = [p.get("ticker", "").replace(".NS", "").replace(".BO", "").strip().upper() for p in arena_open if p.get("ticker")]
        except Exception:
            pass

        core_anchors = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "LT"]

        gap_priority = [s for s in open_arena_symbols if s in missing_session_symbols]
        gap_priority += [s for s in core_anchors if s in missing_session_symbols and s not in gap_priority]
        gap_priority += [s for s in missing_session_symbols if s not in gap_priority]

        # Strictly throttle recovery to preserve the 64/min ceiling (max 10 symbols per tick)
        recovery_batch = gap_priority[:10]
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from app.data.market_provider import fetch_candles

        def _recover_single_stock(sym: str) -> int:
            try:
                df_15m = DataGateway.get_ohlcv(sym, timeframe="15m", limit=30)
                if df_15m is not None and not df_15m.empty:
                    return DataGateway.persist_intraday_candles(
                        ticker=sym, df=df_15m, timeframe="15m", source="upstox_recovery"
                    )
            except Exception as e:
                logger.debug(f"[MarketDaemon] REST recovery note for {sym}: {e}")
            return 0

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {executor.submit(_recover_single_stock, sym): sym for sym in recovery_batch}
            for future in as_completed(futures):
                try:
                    c = future.result()
                    if c > 0:
                        hoarded_count += 1
                except Exception:
                    pass

    # Tier 2 Secondary Hoarding: Strictly deferred during market hours to preserve 90%+ quota for scans
    logger.info(
        f"[MarketDaemon] Full universe stream active: {len(clean_symbols)} symbols covered via WebSocket V3. "
        "REST quota reserved for live execution quotes and gap recovery."
    )

    # 4. Qlib Binary Sync Phase (sync full universe with completed 15m bars)
    qlib_synced = False
    try:
        adapter = get_qlib_adapter()
        sync_candidates = list(clean_symbols)
        sync_res = adapter.sync_intraday_universe(tickers=sync_candidates)
        qlib_synced = bool(sync_res.get("synced", False))
    except Exception as e:
        logger.warning(f"[MarketDaemon] Qlib binary sync warning: {e}")

    # 5. Out-of-Sample (OOS) Candidate Forward Evaluation Phase
    oos_eval_count = 0
    try:
        import sqlite3
        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        c = conn.cursor()
        c.execute("SELECT candidate_id FROM research_candidate_vault WHERE status = 'OOS_PENDING' LIMIT 10;")
        pending_candidates = [row[0] for row in c.fetchall()]
        conn.close()

        for cand_id in pending_candidates:
            try:
                ResearchOrchestrator.evaluate_oos_candidate_live(cand_id)
                oos_eval_count += 1
            except Exception as ce:
                logger.debug(f"[MarketDaemon] OOS evaluation note for {cand_id}: {ce}")
    except Exception as e:
        logger.warning(f"[MarketDaemon] OOS candidate evaluation check warning: {e}")

    # 6. Trade Reconciliation Phase
    reconciled_trades = 0
    try:
        # Reconcile Qlib open recommendations
        reconciled_trades += QlibVirtualTracker.reconcile_open_recommendations(force_refresh=True)
    except Exception as e:
        logger.debug(f"[MarketDaemon] Qlib reconciliation note: {e}")

    try:
        # Reconcile Legacy open trades
        from app.analytics.autonomous_bot import autonomous_bot
        autonomous_bot.sweep_active_trades()
    except Exception as e:
        logger.debug(f"[MarketDaemon] Legacy trade reconciliation note: {e}")

    # 7. Consensus Evaluation & Telegram Dispatch Phase
    consensus_signals_count = 0
    telegram_alerts_sent = 0
    try:
        from app.analytics.consensus_scanner import ConsensusScanner
        from app.analytics.consensus_tracker import ConsensusTracker
        import asyncio
        import concurrent.futures

        def _run_consensus_scan():
            return asyncio.run(
                ConsensusScanner.run_consensus_scan(strategy="INTRADAY", universe="ALL_DATABASE_STOCKS", top_k=10)
            )

        with concurrent.futures.ThreadPoolExecutor() as pool:
            c_scan_res = pool.submit(_run_consensus_scan).result()

        consensus_trades = c_scan_res.get("unanimous_trades") or c_scan_res.get("consensus_trades", [])
        consensus_signals_count = len(consensus_trades)

        for c_trade in consensus_trades:
            c_ts = c_trade.get("decision_timestamp") or c_trade.get("timestamp")
            saved, reason = ConsensusTracker.save_consensus_trade(
                c_trade, strategy="INTRADAY", source="CONSENSUS_DAEMON", decision_timestamp=c_ts
            )
            # Raw scan trades are recorded in consensus_trade_history for live tracking.
            # Telegram notifications are strictly handled below via Arena slot entries (so no alerts outside the 5 game slots).

        # Record anchor milestone completion in ledger if this tick falls on an anchor
        try:
            now_dt_anch = datetime.now()
            now_m = now_dt_anch.hour * 60 + now_dt_anch.minute
            for anchor_name, tgt_m in {"09:30": 570, "11:30": 690, "13:30": 810}.items():
                if abs(now_m - tgt_m) <= 7:
                    init_anchor_schedule_ledger()
                    _c = sqlite3.connect(get_db_path(), timeout=5.0)
                    _c.execute("""
                        INSERT OR REPLACE INTO consensus_anchor_schedule_ledger (
                            date, anchor_name, scheduled_time, status, executed_at, scan_id, trades_count
                        ) VALUES (?, ?, ?, 'COMPLETED', ?, ?, ?)
                    """, (now_dt_anch.strftime("%Y-%m-%d"), anchor_name, f"{anchor_name}:00", now_dt_anch.isoformat(), c_scan_res.get("scan_id"), consensus_signals_count))
                    _c.commit()
                    _c.close()
        except Exception as _anch_e:
            logger.debug(f"[MarketDaemon] Anchor ledger logging note: {_anch_e}")

        # Check and safely recover any missed prior anchors (e.g. after process restart)
        try:
            recovered_anchors = check_and_recover_missed_anchors(force_run=force_run)
            if recovered_anchors:
                logger.info(f"[MarketDaemon] Successfully recovered {len(recovered_anchors)} missed anchors: {recovered_anchors}")
        except Exception as _recov_e:
            logger.debug(f"[MarketDaemon] Missed anchor recovery note: {_recov_e}")

        # Dispatch Arena slot alerts once for the fully persisted batch
        try:
            from app.analytics.consensus_game_engine import ConsensusGameEngine
            ConsensusGameEngine.sync_and_dispatch_arena_alerts()
        except Exception as arena_err:
            logger.debug(f"[MarketDaemon] Arena alert dispatch note: {arena_err}")
    except Exception as e:
        logger.warning(f"[MarketDaemon] Consensus execution/alert phase note: {e}")

    # 8. Consensus Trade Reconciliation Phase
    consensus_reconciled = 0
    try:
        from app.analytics.consensus_tracker import ConsensusTracker
        consensus_reconciled = ConsensusTracker.reconcile_open_consensus_trades()
        reconciled_trades += consensus_reconciled
    except Exception as e:
        logger.debug(f"[MarketDaemon] Consensus trade reconciliation note: {e}")

    # 8b. Consensus Dynamic Trade Guard (Active Arena Slot Monitoring)
    try:
        from app.analytics.consensus_dynamic_guard import ConsensusDynamicGuard
        ConsensusDynamicGuard.audit_active_arena_positions(session_id="default_arena_session")
    except Exception as g_err:
        logger.debug(f"[MarketDaemon] Dynamic Trade Guard audit note: {g_err}")

    # 9. Consensus Online Learning & Weight Adaptation Phase
    learning_update = {}
    try:
        now_dt = datetime.now()
        # Trigger daily weight adaptation after 15:30 IST or when trades were reconciled
        if (now_dt.hour == 15 and now_dt.minute >= 30) or now_dt.hour >= 16 or consensus_reconciled > 0:
            from app.analytics.consensus_learning_engine import ConsensusLearningEngine
            learning_update = ConsensusLearningEngine.execute_daily_weight_update(strategy="INTRADAY")
            if learning_update.get("updated"):
                MasterLogger.log_event(
                    category="CONSENSUS_LEARNING",
                    event_type="CONSENSUS_WEIGHTS_ADAPTED",
                    message=f"Consensus weights adapted via Hedge: Qlib={learning_update['weights']['w_qlib']}, Legacy={learning_update['weights']['w_legacy']}",
                    details=learning_update
                )
    except Exception as le:
        logger.debug(f"[MarketDaemon] Consensus learning update note: {le}")

    elapsed = round(time.time() - start_time, 2)
    logger.info(
        f"[MarketDaemon] 15-minute tick complete in {elapsed}s | "
        f"Hoarded: {hoarded_count} symbols | Qlib Synced: {qlib_synced} | "
        f"OOS Evaluated: {oos_eval_count} | Consensus Signals: {consensus_signals_count} | "
        f"Telegram Alerts: {telegram_alerts_sent} | Reconciled: {reconciled_trades}"
    )

    result = {
        "status": "SUCCESS",
        "timestamp": now_iso,
        "is_market_open": True,
        "symbols_scanned": len(clean_symbols),
        "symbols_hoarded": hoarded_count,
        "qlib_synced": qlib_synced,
        "oos_candidates_evaluated": oos_eval_count,
        "consensus_signals_count": consensus_signals_count,
        "telegram_alerts_sent": telegram_alerts_sent,
        "consensus_reconciled": consensus_reconciled,
        "reconciled_trades": reconciled_trades,
        "elapsed_seconds": elapsed
    }

    MasterLogger.log_event(
        category="SCHEDULER",
        event_type="MARKET_TICK_15M_COMPLETED",
        message=f"15m market tick completed in {elapsed}s (Hoarded: {hoarded_count}, Consensus: {consensus_signals_count}, Alerts: {telegram_alerts_sent})",
        details=result
    )

    return result

