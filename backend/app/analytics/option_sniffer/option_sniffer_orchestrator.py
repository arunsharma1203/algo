"""
Option Sniffer: Central Subsystem Orchestrator Daemon.

Coordinates all Option Sniffer components:
  1. Contract Discovery & ATM Expiry Tracking.
  2. WebSocket V3 Live Feed Ingestion via OptionStreamManager.
  3. Real-Time Option Alpha Model Inference.
  4. Signal State Machine Evaluation & Telegram Alerts.
  5. Continuous Paper Trade Position Surveillance & Exit Execution.
  6. Health Monitoring & REST API telemetry endpoints.
"""

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
    calculate_option_trade_costs,
)

logger = logging.getLogger(__name__)


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
        now_iso = datetime.now().isoformat()

        with self._lock:
            # 0. Check for New Trading Day Rollover or Empty Universe
            today_str = datetime.now().strftime("%Y-%m-%d")
            last_date = getattr(self, "_last_discovery_date", "")
            if last_date != today_str or not self.contracts:
                logger.info(f"[OptionSnifferOrchestrator] Discovering active ATM contracts for session {today_str}...")
                self.contracts = OptionContractDiscovery.discover_monitored_universe(force_refresh=True)
                self.stream_manager.sync_monitored_contracts(self.contracts)
                self._last_discovery_date = today_str
                self._last_discovery_time = time.time()

            # 0b. Self-Healing Live Quote Ingestion:
            # If quotes are stale (> 15s) or missing from WebSocket, pull fresh quotes via REST batch quote
            active_quotes = self.stream_manager.get_all_active_option_quotes()
            has_fresh_quotes = bool(active_quotes and any(q.get("quote_age_ms", 99999) <= 15000 for q in active_quotes))
            if not has_fresh_quotes:
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
            closed_trades = OptionPaperExecution.update_open_trades(live_quotes)

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
                    upstream_signals = {
                        "consensus_direction": (latest_c.get("unanimous_trades") or [{}])[0].get("direction", "NEUTRAL") if latest_c.get("unanimous_trades") else "NEUTRAL",
                        "consensus_confidence": (latest_c.get("unanimous_trades") or [{}])[0].get("composite_score", 50.0) if latest_c.get("unanimous_trades") else 50.0,
                    }
            except Exception:
                pass

            # 4. Evaluate each candidate contract
            evaluated_count = 0
            qualified_signals: List[Dict[str, Any]] = []
            active_signals: List[Dict[str, Any]] = []

            for contract in self.contracts:
                ikey = contract.get("instrument_key")
                quote = self.stream_manager.get_option_quote(ikey)
                if not quote:
                    continue

                u_sym = contract.get("underlying", "NIFTY")
                u_quote = self.stream_manager.get_underlying_quote(u_sym)
                if not u_quote:
                    continue

                evaluated_count += 1
                res = self.signal_engine.evaluate_contract(
                    contract=contract,
                    quote=quote,
                    underlying_quote=u_quote,
                    macro_context=macro_info,
                    upstream_signals=upstream_signals
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

    # ──────────────────────────────────────────────────────────────────────────
    # API EXPOSURE METHODS
    # ──────────────────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Telemetry status for API responses."""
        with self._lock:
            pnl_data = OptionDB.get_pnl_summary()
            health = self.stream_manager.get_health_status()
            model = self.signal_engine.model
            return {
                "service": "OPTION_SNIFFER",
                "mode": "PAPER_ONLY",
                "is_running": self._is_running,
                "total_sweeps": self._total_sweeps,
                "monitored_contracts_count": len(self.contracts),
                "model": {
                    "version": model.version,
                    "sha256": model.artifact_hash,
                    "metrics": model.metadata.get("metrics", {}),
                },
                "feed_health": health,
                "pnl_summary": pnl_data,
                "last_sweep_results": self._last_sweep_results,
            }

    def get_candidates(self) -> List[Dict[str, Any]]:
        """Active candidates with live quotes."""
        candidates = OptionDB.get_active_candidates()
        if not candidates and self.contracts:
            candidates = self.contracts

        # If stream manager has no quotes or quotes are all stale, proactively refresh
        cached_quotes = self.stream_manager.get_all_active_option_quotes()
        if not cached_quotes or all(q.get("quote_age_ms", 99999) > 15000 for q in cached_quotes):
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
        now_dt = datetime.now()
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

            # Target & Stop Loss Levels
            target_pct = TARGET_GAIN_PCT
            stop_loss_pct = STOP_LOSS_PCT
            target_price = round(entry_ask * (1.0 + TARGET_GAIN_PCT / 100.0), 2)
            stop_loss_price = round(entry_ask * (1.0 - STOP_LOSS_PCT / 100.0), 2)

            # Trailing Ratchet Stop
            trailing_stop_active = bool(mfe_pct >= TRAILING_STOP_TRIGGER_PCT)
            trailing_stop_price = round(peak_price * (1.0 - TRAILING_STOP_DISTANCE_PCT / 100.0), 2) if trailing_stop_active else None
            effective_stop_price = trailing_stop_price if (trailing_stop_active and trailing_stop_price and trailing_stop_price > stop_loss_price) else stop_loss_price

            # Distance metrics
            sl_distance_pct = round(((current_bid - effective_stop_price) / effective_stop_price * 100.0), 2) if effective_stop_price > 0 else 0.0
            tp_distance_pct = round(((target_price - current_bid) / current_bid * 100.0), 2) if current_bid > 0 else 0.0

            # Mark-to-market costs & unrealized P&L
            cost_info = calculate_option_trade_costs(
                buy_price=entry_ask,
                sell_price=current_bid,
                quantity=quantity,
                apply_slippage=True
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
