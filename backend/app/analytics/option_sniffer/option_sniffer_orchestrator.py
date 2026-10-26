"""
Option Sniffer: Institutional Orchestrator

Coordinates all Option Sniffer components:
  1. Contract Discovery & ATM Expiry Tracking.
  2. WebSocket V3 Live Feed Ingestion via OptionStreamManager.
  3. Real-Time Option Alpha Model Inference.
  4. Signal State Machine Evaluation & Telegram Alerts.
  5. Continuous Paper Trade Position Surveillance & Exit Execution.
  6. Health Monitoring & REST API telemetry endpoints.
"""

import os
import hashlib
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from app.analytics.option_sniffer.option_contract_discovery import OptionContractDiscovery
from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
from app.analytics.option_sniffer.option_signal_engine import OptionSignalEngine
from app.analytics.option_sniffer.option_paper_execution import OptionPaperExecution
from app.analytics.option_sniffer.option_alpha_model import OptionAlphaModel
from app.analytics.option_sniffer.option_db import OptionDB
from app.analytics.option_sniffer.option_constants import (
    TARGET_GAIN_PCT,
    STOP_LOSS_PCT,
    TRAILING_STOP_TRIGGER_PCT,
    TRAILING_STOP_DISTANCE_PCT,
    BREAKEVEN_FLOOR_PCT,
    OPTION_EXECUTION_ENGINE_MODE,
    ENABLE_APEX_QUANT_ENGINE,
    THETA_STAGNATION_TIMEOUT_MINUTES,
    THETA_STAGNATION_MIN_MFE_PCT,
    THETA_STAGNATION_EXIT_LOSS_CAP_PCT,
    PEAK_PROFIT_LOCK_TRIGGER_INR,
    PEAK_PROFIT_GIVEBACK_MAX_RATIO,
    CRBI_BREAKOUT_VOLUME_MIN_RATIO,
    CRBI_RELAXED_MODEL_PROB,
    CRBI_RELAXED_KAUFMAN_ER,
    calculate_option_trade_costs,
    is_option_trading_window,
    to_ist_naive,
)

logger = logging.getLogger(__name__)

_CORE_MODULE_FILES = [
    "option_constants.py",
    "option_signal_engine.py",
    "option_paper_execution.py",
    "option_sniffer_orchestrator.py",
    "option_db.py",
]

def _compute_module_hashes() -> Dict[str, str]:
    """Computes SHA-256 hashes of on-disk Option Sniffer python files."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    hashes = {}
    for fname in _CORE_MODULE_FILES:
        fpath = os.path.join(base_dir, fname)
        if os.path.exists(fpath):
            try:
                with open(fpath, "rb") as f:
                    hashes[fname] = hashlib.sha256(f.read()).hexdigest()
            except Exception:
                hashes[fname] = "READ_ERROR"
        else:
            hashes[fname] = "MISSING"
    return hashes

# Recorded once when the Python interpreter imports this module into RAM
_BOOT_MODULE_HASHES: Dict[str, str] = _compute_module_hashes()
_BOOT_TIMESTAMP: str = to_ist_naive().isoformat()



class OptionSnifferOrchestrator:
    """
    Singleton orchestrator managing background surveillance and paper-trading lifecycle.
    """

    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._init_lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        with self._init_lock:
            if hasattr(self, "_initialized") and self._initialized:
                return

            self._lock = threading.RLock()
            self._is_running = False
            self._worker_thread: Optional[threading.Thread] = None

            self.stream_manager = OptionStreamManager()
            self.signal_engine = OptionSignalEngine()
            self.contracts: List[Dict[str, Any]] = []

            self._last_sweep_time: float = 0.0
            self._last_discovery_time: float = 0.0
            self._last_discovery_date: str = ""
            self._total_sweeps: int = 0
            self._last_sweep_results: Dict[str, Any] = {}

            self._initialized = True
            logger.info("[OptionSnifferOrchestrator] Initialized OptionSnifferOrchestrator singleton.")

    def start(self) -> bool:
        """Starts background surveillance daemon."""
        with self._lock:
            if self._is_running:
                logger.info("[OptionSnifferOrchestrator] Already running.")
                return True

            self._is_running = True

            # 1. Initial Contract Discovery
            try:
                self.contracts = OptionContractDiscovery.discover_monitored_universe()
                self.stream_manager.sync_monitored_contracts(self.contracts)
                self._last_discovery_time = time.time()
            except Exception as de:
                logger.warning(f"[OptionSnifferOrchestrator] Initial discovery error: {de}")

            # 2. Start worker thread
            self._worker_thread = threading.Thread(
                target=self._surveillance_loop,
                name="OptionSnifferWorker",
                daemon=True
            )
            self._worker_thread.start()
            logger.info("✅ [OptionSnifferOrchestrator] Surveillance daemon active.")
            return True

    def stop(self) -> None:
        """Stops background surveillance loop."""
        with self._lock:
            self._is_running = False
            logger.info("[OptionSnifferOrchestrator] Surveillance daemon stopped.")

    def _surveillance_loop(self) -> None:
        """Periodic background evaluation loop (runs every 10 seconds)."""
        while self._is_running:
            try:
                time.sleep(10.0)
                if not self._is_running:
                    break

                self.run_surveillance_sweep()

            except Exception as e:
                logger.error(f"[OptionSnifferOrchestrator] Surveillance loop error: {e}", exc_info=True)
                time.sleep(5.0)

    def run_surveillance_sweep(self) -> Dict[str, Any]:
        """
        Executes an evaluation sweep across all active option contracts:
          1. Surveillance of existing open paper positions.
          2. Check for dynamic ATM strike shifts.
          3. Evaluate candidate contracts through quality gates.
          4. Record data health.
        """
        sweep_start = time.time()
        now_dt = to_ist_naive()
        now_iso = now_dt.isoformat()

        with self._lock:
            # 0. Check for New Trading Day Rollover or Empty Universe
            today_str = now_dt.strftime("%Y-%m-%d")
            last_date = getattr(self, "_last_discovery_date", "")
            if last_date != today_str or not self.contracts:
                logger.info(f"[OptionSnifferOrchestrator] Discovering active ATM contracts for session {today_str}...")
                self.contracts = OptionContractDiscovery.discover_monitored_universe(force_refresh=True)
                self.stream_manager.sync_monitored_contracts(self.contracts)
                self._last_discovery_date = today_str
                self._last_discovery_time = time.time()

            # 0b. Self-Healing Live Quote Ingestion:
            # Verify that quotes exist and neither underlyings nor candidate options are stale (> 15s).
            # If any quote is stale or missing, pull fresh quotes via REST batch quote so every contract has fresh data.
            active_quotes = self.stream_manager.get_all_active_option_quotes()
            stale_count = sum(1 for q in active_quotes if q.get("quote_age_ms", 99999) > 15000)
            underlying_stale = False
            for u in ("NIFTY", "BANKNIFTY"):
                uq = self.stream_manager.get_underlying_quote(u)
                if not uq or uq.get("quote_age_ms", 99999) > 15000:
                    underlying_stale = True
                    break

            needs_rest_refresh = (
                not active_quotes
                or len(active_quotes) < len(self.contracts)
                or stale_count > 0
                or underlying_stale
            )
            if needs_rest_refresh:
                self.stream_manager.refresh_quotes_via_rest()

            # 1. Update active paper positions
            live_quotes = {q["instrument_key"]: q for q in self.stream_manager.get_all_active_option_quotes()}
            open_trades_raw = OptionDB.get_open_paper_trades()
            for ot in open_trades_raw:
                ot_key = ot.get("instrument_key")
                if ot_key and ot_key not in live_quotes:
                    ot_q = self.stream_manager.get_option_quote(ot_key) or self.stream_manager._option_quotes.get(ot_key)
                    if ot_q:
                        live_quotes[ot_key] = ot_q

            underlying_quotes = {
                u: self.stream_manager.get_underlying_quote(u)
                for u in ("NIFTY", "BANKNIFTY")
            }
            closed_trades = OptionPaperExecution.update_open_trades(
                live_quotes,
                underlying_quotes_map=underlying_quotes,
                as_of_time=now_dt
            )

            # 2. Dynamic ATM Shift Check (Refresh candidate universe if spot moved > 1 strike)
            now = time.time()
            if (now - self._last_discovery_time) > 1800.0:  # Every 30 mins minimum or upon shift
                for u in ("NIFTY", "BANKNIFTY"):
                    u_quote = self.stream_manager.get_underlying_quote(u)
                    if u_quote and OptionContractDiscovery.check_dynamic_atm_shift(u, u_quote["spot_ltp"]):
                        logger.info(f"[OptionSnifferOrchestrator] ATM shift detected for {u}. Refreshing contracts...")
                        self.contracts = OptionContractDiscovery.discover_monitored_universe()
                        self.stream_manager.sync_monitored_contracts(self.contracts)
                        self._last_discovery_time = now
                        break

            # 3. Upstream Context & Macro Retrieval
            macro_info = {}
            try:
                from app.analytics.macro_engine import get_macro_regime
                macro_info = get_macro_regime()
            except Exception:
                pass

            upstream_signals = {}
            try:
                from app.analytics.consensus_scanner import ConsensusScanner
                latest_c = ConsensusScanner.get_latest_scan("INTRADAY")
                if latest_c:
                    unanimous = latest_c.get("unanimous_trades") or []
                    conflicts_count = int(latest_c.get("conflicted_count") or latest_c.get("conflicts_count") or 0)
                    unanimous_count = len(unanimous)

                    buy_votes = sum(1 for t in unanimous if str(t.get("direction", "")).upper() in ("BUY", "BULLISH"))
                    sell_votes = sum(1 for t in unanimous if str(t.get("direction", "")).upper() in ("SELL", "BEARISH"))

                    c_dir = "NEUTRAL"
                    if buy_votes > sell_votes and buy_votes > 0:
                        c_dir = "BULLISH"
                    elif sell_votes > buy_votes and sell_votes > 0:
                        c_dir = "BEARISH"

                    is_conflicted = (conflicts_count > (unanimous_count * 2)) and (conflicts_count > 5)

                    upstream_signals = {
                        "consensus_direction": c_dir,
                        "consensus_confidence": unanimous[0].get("composite_score", 50.0) if unanimous else 50.0,
                        "unanimous_count": unanimous_count,
                        "conflicts_count": conflicts_count,
                        "is_conflicted": is_conflicted,
                    }
            except Exception:
                pass

            # 4. Evaluate each candidate contract (Only during active trading session before 15:15 IST)
            evaluated_count = 0
            qualified_signals: List[Dict[str, Any]] = []
            active_signals: List[Dict[str, Any]] = []

            # Session Cutoff Guard: strictly halt candidate evaluations and new trades after 15:15 IST
            if not is_option_trading_window(now_dt):
                logger.info(
                    f"⏸️ [OptionSnifferOrchestrator] Past 15:15 IST cutoff or market closed ({now_dt.strftime('%H:%M:%S')} IST). "
                    f"Candidate evaluations and new trade signals halted. Open positions: {len(open_trades_raw) - len(closed_trades)}."
                )
            else:
                for contract in self.contracts:
                    ikey = contract.get("instrument_key")
                    quote = self.stream_manager.get_option_quote(ikey)
                    if not quote:
                        continue

                    u_sym = contract.get("underlying", "NIFTY")
                    u_quote = self.stream_manager.get_underlying_quote(u_sym)
                    if not u_quote:
                        continue

                    u_df = None
                    try:
                        if hasattr(self.stream_manager, "get_underlying_history_df"):
                            u_df = self.stream_manager.get_underlying_history_df(u_sym)
                    except Exception as df_err:
                        logger.debug(f"[OptionSnifferOrchestrator] Underlying history fetch note: {df_err}")
                        u_df = None

                    evaluated_count += 1
                    res = self.signal_engine.evaluate_contract(
                        contract=contract,
                        quote=quote,
                        underlying_quote=u_quote,
                        underlying_df=u_df,
                        macro_context=macro_info,
                        upstream_signals=upstream_signals,
                        as_of_time=now_dt,
                        enforce_cutoff=True
                    )

                    if res.get("status") == "QUALIFIED":
                        qualified_signals.append(res)
                    elif res.get("status") == "ACTIVE":
                        active_signals.append(res)

            # 5. Record Health Snapshot
            health_telem = self.stream_manager.get_health_status()
            OptionDB.record_data_health(health_telem)

            elapsed = round(time.time() - sweep_start, 2)
            self._total_sweeps += 1
            self._last_sweep_time = time.time()

            results = {
                "timestamp": now_iso,
                "elapsed_seconds": elapsed,
                "total_contracts_monitored": len(self.contracts),
                "contracts_evaluated": evaluated_count,
                "qualified_signals": qualified_signals,
                "active_signals": active_signals,
                "closed_trades_in_sweep": len(closed_trades),
                "health": health_telem,
            }
            self._last_sweep_results = results
            return results

    @classmethod
    def _select_winner_takes_all_candidates(cls, candidate_signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Sweep-Level Winner-Takes-All Mutex (Pillar 3: Anti-Clustering):
        When multiple candidate signals of the same underlying qualify in the same sweep,
        strictly selects the single best strike per underlying based on Convex Efficiency Score:
            Score = (calibrated_prob * delta) / max(0.05, spread_pct)
        Suppresses redundant adjacent strikes to eliminate multi-strike correlated risk.
        """
        if not candidate_signals:
            return []

        from collections import defaultdict
        grouped = defaultdict(list)
        for sig in candidate_signals:
            u = str(sig.get("underlying") or "NIFTY").upper()
            grouped[u].append(sig)

        winning_signals = []
        for u, sigs in grouped.items():
            if len(sigs) == 1:
                winning_signals.append(sigs[0])
            else:
                def score(s):
                    p = float(s.get("calibrated_prob") or 0.44)
                    d = abs(float(s.get("delta") or 0.50))
                    sp = max(0.05, float(s.get("spread_pct") or 0.25))
                    return (p * d) / sp
                best_sig = max(sigs, key=score)
                winning_signals.append(best_sig)

        return winning_signals

    # ──────────────────────────────────────────────────────────────────────────
    # API EXPOSURE METHODS
    # ──────────────────────────────────────────────────────────────────────────

    def get_market_regime_telemetry(self) -> Dict[str, Any]:
        """Provides real-time intraday market regime and option shield status."""
        try:
            from app.analytics.macro_engine import get_macro_regime
            macro_info = get_macro_regime()
        except Exception:
            macro_info = {}

        u_quote = self.stream_manager.get_underlying_quote("NIFTY") or {}
        spot = float(u_quote.get("spot_ltp") or 0.0)
        day_open = float(u_quote.get("open") or spot)
        day_high = float(u_quote.get("high") or spot)
        day_low = float(u_quote.get("low") or spot)
        day_vwap = float(u_quote.get("vwap") or spot)

        range_pts = round(day_high - day_low, 2)
        range_pct = round((range_pts / day_low * 100.0), 2) if day_low > 0 else 0.0
        drift_vwap_pct = round(((spot - day_vwap) / day_vwap * 100.0), 2) if day_vwap > 0 else 0.0

        macro_verdict = str(
            macro_info.get("intraday_regime_verdict") or macro_info.get("regime_verdict") or "NEUTRAL_CHOP"
        ).upper()
        vix = float(macro_info.get("vix_close") or 14.5)

        is_macro_chop = any(k in macro_verdict for k in ("CHOP", "NEUTRAL", "SIDEWAYS", "RANGE"))
        is_trapped_in_day_range = (spot > 0) and (day_low > 0) and (range_pts < 100.0) and (spot > (day_low + 8.0)) and (spot < (day_high - 8.0))
        is_breakout_high = (spot > 0) and (day_high > 0) and (spot >= day_high - 5.0) and (drift_vwap_pct >= 0.0)
        is_breakdown_low = (spot > 0) and (day_low > 0) and (spot <= day_low + 5.0) and (drift_vwap_pct <= 0.0)

        if (is_macro_chop or is_trapped_in_day_range) and not (is_breakout_high or is_breakdown_low):
            regime = "RANGEBOUND_CHOP"
            shield = "SHIELD_PAUSED"
            badge_color = "amber"
            title = "RANGEBOUND / CHOP"
            range_desc = f"{range_pts:.1f} pt ({range_pct:.2f}%) range" if range_pts > 0 else "neutral breadth"
            reason = f"NIFTY trapped in {range_desc} around VWAP. Option buying shielded."
        elif is_breakout_high or ("BULL" in macro_verdict and drift_vwap_pct > 0.02):
            regime = "TRENDING_BULL"
            shield = "ACTIVE_BULL"
            badge_color = "emerald"
            title = "BULLISH TREND"
            if drift_vwap_pct > 0.02:
                reason = f"Bullish momentum above VWAP ({drift_vwap_pct:+.2f}%). Calls active, Puts blocked."
            else:
                reason = f"Testing day high near VWAP ({drift_vwap_pct:+.2f}%). Calls active."
        elif is_breakdown_low or ("BEAR" in macro_verdict and drift_vwap_pct < -0.02):
            regime = "TRENDING_BEAR"
            shield = "ACTIVE_BEAR"
            badge_color = "rose"
            title = "BEARISH TREND"
            if drift_vwap_pct < -0.02:
                reason = f"Bearish pressure below VWAP ({drift_vwap_pct:+.2f}%). Puts active, Calls blocked."
            else:
                reason = f"Testing day low near VWAP ({drift_vwap_pct:+.2f}%). Puts active."
        else:
            regime = "VOLATILITY_SQUEEZE"
            shield = "WATCHING_SQUEEZE"
            badge_color = "cyan"
            title = "VOLATILITY SQUEEZE"
            if abs(drift_vwap_pct) <= 0.05:
                reason = f"Consolidating near VWAP equilibrium ({drift_vwap_pct:+.2f}%). Dynamic regime shield monitoring."
            else:
                reason = f"Range compression ({range_pts:.1f} pts, VWAP drift {drift_vwap_pct:+.2f}%). Monitoring for high/low breakout."

        return {
            "regime": regime,
            "title": title,
            "shield": shield,
            "badge_color": badge_color,
            "reason": reason,
            "underlying": "NIFTY",
            "spot": spot,
            "day_open": day_open,
            "day_high": day_high,
            "day_low": day_low,
            "day_vwap": day_vwap,
            "range_pts": range_pts,
            "range_pct": range_pct,
            "drift_vwap_pct": drift_vwap_pct,
            "macro_verdict": macro_verdict,
            "vix": vix,
        }

    @classmethod
    def check_runtime_disk_parity(cls) -> Dict[str, Any]:
        """
        Compares the in-memory boot hashes against the current on-disk module hashes.
        Detects silent code updates on disk that have NOT been loaded into memory.
        """
        current_disk_hashes = _compute_module_hashes()
        stale_files = []
        for fname, boot_h in _BOOT_MODULE_HASHES.items():
            disk_h = current_disk_hashes.get(fname)
            if boot_h != disk_h:
                stale_files.append(fname)

        is_in_sync = len(stale_files) == 0
        status = "SYNCHRONIZED" if is_in_sync else "DESYNCHRONIZED_STALE_RAM"
        message = (
            "All core Option Sniffer modules in sync between disk and running RAM."
            if is_in_sync
            else f"CRITICAL: Memory is running stale bytecode for: {', '.join(stale_files)}. Restart backend to apply disk changes!"
        )

        return {
            "status": status,
            "in_sync": is_in_sync,
            "message": message,
            "boot_timestamp": _BOOT_TIMESTAMP,
            "stale_files": stale_files,
            "boot_hashes": _BOOT_MODULE_HASHES,
            "current_disk_hashes": current_disk_hashes,
        }

    def get_status(self) -> Dict[str, Any]:
        """Telemetry status for API responses."""
        pnl_data = OptionDB.get_pnl_summary()
        health = self.stream_manager.get_health_status()
        model = self.signal_engine.model
        now_dt = to_ist_naive()
        regime_telem = self.get_market_regime_telemetry()
        parity = self.check_runtime_disk_parity()
        cb_telem = OptionDB.get_daily_circuit_breaker_telemetry()
        return {
            "service": "OPTION_SNIFFER",
            "mode": "PAPER_ONLY",
            "engine_mode": OPTION_EXECUTION_ENGINE_MODE,
            "is_running": self._is_running,
            "is_trading_window": is_option_trading_window(now_dt),
            "cutoff_time": "15:15 IST",
            "total_sweeps": self._total_sweeps,
            "monitored_contracts_count": len(self.contracts),
            "model": {
                "version": model.version,
                "sha256": model.artifact_hash,
                "metrics": model.metadata.get("metrics", {}),
            },
            "runtime_parity": parity,
            "circuit_breaker": cb_telem,
            "apex_quant": {
                "enabled": ENABLE_APEX_QUANT_ENGINE,
                "stagnation_timeout_minutes": THETA_STAGNATION_TIMEOUT_MINUTES,
                "stagnation_min_mfe_pct": THETA_STAGNATION_MIN_MFE_PCT,
                "stagnation_exit_loss_cap_pct": THETA_STAGNATION_EXIT_LOSS_CAP_PCT,
                "peak_profit_lock_trigger_inr": PEAK_PROFIT_LOCK_TRIGGER_INR,
                "peak_profit_giveback_max_ratio": PEAK_PROFIT_GIVEBACK_MAX_RATIO,
                "crbi_breakout_volume_min_ratio": CRBI_BREAKOUT_VOLUME_MIN_RATIO,
                "crbi_relaxed_model_prob": CRBI_RELAXED_MODEL_PROB,
                "crbi_relaxed_kaufman_er": CRBI_RELAXED_KAUFMAN_ER,
                "peak_profit_floor": cb_telem.get("peak_profit_floor", 0.0),
                "is_peak_lock_tripped": cb_telem.get("is_peak_lock_tripped", False),
            },
            "feed_health": health,
            "market_regime": regime_telem,
            "pnl_summary": pnl_data,
            "last_sweep_results": self._last_sweep_results,
        }

    def get_candidates(self) -> List[Dict[str, Any]]:
        """Active candidates with live quotes."""
        candidates = self.contracts if self.contracts else OptionDB.get_active_candidates()

        # If stream manager has no quotes or any candidate quote is stale, proactively refresh
        cached_quotes = self.stream_manager.get_all_active_option_quotes()
        has_stale_or_missing = (
            not cached_quotes
            or len(cached_quotes) < len(candidates)
            or any(q.get("quote_age_ms", 99999) > 15000 for q in cached_quotes)
        )
        if has_stale_or_missing:
            try:
                self.stream_manager.refresh_quotes_via_rest()
            except Exception as e:
                logger.debug(f"[OptionSnifferOrchestrator] Candidate quote fetch note: {e}")

        output = []
        for c in candidates:
            ikey = c.get("instrument_key")
            q = self.stream_manager.get_option_quote(ikey)
            merged = dict(c)
            if q:
                merged["quote"] = q
            output.append(merged)
        return output

    def get_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent option signals."""
        return OptionDB.get_recent_signals(limit=limit)

    def get_paper_trades(self) -> Dict[str, Any]:
        """
        Open and closed paper trades decorated with real-time mark-to-market prices,
        stop-loss levels, take-profit targets, trailing stops, and net unrealized P&L.
        """
        open_raw = OptionDB.get_open_paper_trades()
        now_dt = to_ist_naive()
        decorated_open: List[Dict[str, Any]] = []

        for trade in open_raw:
            t = dict(trade)
            ikey = t.get("instrument_key")
            quote = None
            if ikey:
                quote = self.stream_manager.get_option_quote(ikey)
                if not quote:
                    quote = self.stream_manager._option_quotes.get(ikey)

            entry_ask = float(t.get("entry_ask") or t.get("entry_price_used") or 0.0)
            quantity = int(t.get("quantity") or 0)

            # Live execution prices strictly at market bid
            if quote and float(quote.get("bid") or 0.0) > 0:
                current_bid = float(quote.get("bid"))
                current_ask = float(quote.get("ask") or current_bid)
                current_ltp = float(quote.get("ltp") or current_bid)
            elif quote and float(quote.get("ltp") or 0.0) > 0:
                current_ltp = float(quote.get("ltp"))
                current_bid = round(current_ltp * 0.995, 2)
                current_ask = round(current_ltp * 1.005, 2)
            else:
                current_bid = float(t.get("peak_price") or entry_ask)
                current_ask = entry_ask
                current_ltp = current_bid

            # Extrema tracking & excursions
            peak_price = max(float(t.get("peak_price") or entry_ask), current_bid)
            trough_price = min(float(t.get("trough_price") or entry_ask), current_bid)
            mfe_pct = round(((peak_price - entry_ask) / entry_ask * 100.0), 2) if entry_ask > 0 else 0.0
            mae_pct = round(((trough_price - entry_ask) / entry_ask * 100.0), 2) if entry_ask > 0 else 0.0

            # Holding duration
            try:
                entry_dt = datetime.fromisoformat(t["entry_time"])
                duration_sec = int((now_dt - entry_dt).total_seconds())
            except Exception:
                duration_sec = int(t.get("holding_duration_seconds") or 0)

            # Target & Stop Loss Levels (Dynamic V2 or Classic V1)
            target_pct = float(t.get("dynamic_target_pct") or TARGET_GAIN_PCT)
            stop_loss_pct = float(t.get("dynamic_sl_pct") or STOP_LOSS_PCT)
            target_price = round(entry_ask * (1.0 + target_pct / 100.0), 2)
            stop_loss_price = round(entry_ask * (1.0 - stop_loss_pct / 100.0), 2)

            # Trailing Ratchet Stop
            ratchet_trigger_pct = min(TRAILING_STOP_TRIGGER_PCT, max(12.0, round(target_pct * 0.50, 1)))
            trailing_stop_active = bool(mfe_pct >= ratchet_trigger_pct)
            trailing_stop_price = round(peak_price * (1.0 - TRAILING_STOP_DISTANCE_PCT / 100.0), 2) if trailing_stop_active else None
            breakeven_floor = round(entry_ask * (1.0 + BREAKEVEN_FLOOR_PCT / 100.0), 2)
            effective_trail = max(trailing_stop_price, breakeven_floor) if trailing_stop_price else breakeven_floor
            effective_stop_price = effective_trail if (trailing_stop_active and effective_trail > stop_loss_price) else stop_loss_price

            # Distance metrics
            sl_distance_pct = round(((current_bid - effective_stop_price) / effective_stop_price * 100.0), 2) if effective_stop_price > 0 else 0.0
            tp_distance_pct = round(((target_price - current_bid) / current_bid * 100.0), 2) if current_bid > 0 else 0.0

            # Mark-to-market costs & unrealized P&L
            cost_info = calculate_option_trade_costs(
                buy_price=entry_ask,
                sell_price=current_bid,
                quantity=quantity,
                apply_slippage=True,
                trade_date=t.get("entry_time") or to_ist_naive()
            )
            unrealized_gross = round((current_bid - entry_ask) * quantity, 2)
            unrealized_charges = round(cost_info["total_friction"], 2)
            unrealized_net = round(unrealized_gross - unrealized_charges, 2)
            unrealized_pnl_pct = round(((current_bid - entry_ask) / entry_ask * 100.0), 2) if entry_ask > 0 else 0.0
            total_invested = entry_ask * quantity
            unrealized_net_pnl_pct = round((unrealized_net / total_invested * 100.0), 2) if total_invested > 0 else 0.0

            t.update({
                "current_bid": current_bid,
                "current_ask": current_ask,
                "current_ltp": current_ltp,
                "peak_price": peak_price,
                "trough_price": trough_price,
                "mfe_pct": mfe_pct,
                "mae_pct": mae_pct,
                "holding_duration_seconds": duration_sec,
                "target_pct": target_pct,
                "stop_loss_pct": stop_loss_pct,
                "target_price": target_price,
                "stop_loss_price": stop_loss_price,
                "trailing_stop_trigger_pct": TRAILING_STOP_TRIGGER_PCT,
                "trailing_stop_distance_pct": TRAILING_STOP_DISTANCE_PCT,
                "trailing_stop_active": trailing_stop_active,
                "trailing_stop_price": trailing_stop_price,
                "effective_stop_price": effective_stop_price,
                "sl_distance_pct": sl_distance_pct,
                "tp_distance_pct": tp_distance_pct,
                "unrealized_gross_pnl": unrealized_gross,
                "unrealized_charges": unrealized_charges,
                "unrealized_net_pnl": unrealized_net,
                "unrealized_pnl_pct": unrealized_pnl_pct,
                "unrealized_net_pnl_pct": unrealized_net_pnl_pct,
            })
            decorated_open.append(t)

        pnl_summary = OptionDB.get_pnl_summary()
        tot_unrealized_net = round(sum(t.get("unrealized_net_pnl", 0.0) for t in decorated_open), 2)
        tot_unrealized_gross = round(sum(t.get("unrealized_gross_pnl", 0.0) for t in decorated_open), 2)
        tot_unrealized_charges = round(sum(t.get("unrealized_charges", 0.0) for t in decorated_open), 2)

        pnl_summary["unrealized_net_pnl"] = tot_unrealized_net
        pnl_summary["unrealized_gross_pnl"] = tot_unrealized_gross
        pnl_summary["unrealized_charges"] = tot_unrealized_charges
        pnl_summary["today_realized_net_pnl"] = pnl_summary.get("today_net_pnl", 0.0)
        pnl_summary["today_total_net_pnl"] = round(pnl_summary.get("today_net_pnl", 0.0) + tot_unrealized_net, 2)
        pnl_summary["all_time_total_net_pnl"] = round(pnl_summary.get("total_net_pnl", 0.0) + tot_unrealized_net, 2)

        return {
            "open_trades": decorated_open,
            "closed_trades": OptionDB.get_closed_paper_trades(limit=50),
            "pnl_summary": pnl_summary,
        }

    def get_calibration_stats(self) -> Dict[str, Any]:
        """Model calibration and ablation statistics."""
        model = self.signal_engine.model
        return {
            "version": model.version,
            "sha256": model.artifact_hash,
            "calibration_stats": model.metadata.get("calibration_stats", {}),
            "ablation_results": model.metadata.get("ablation_results", {}),
            "metrics": model.metadata.get("metrics", {}),
        }


def get_option_sniffer_orchestrator() -> OptionSnifferOrchestrator:
    """Returns singleton instance of OptionSnifferOrchestrator."""
    return OptionSnifferOrchestrator()
