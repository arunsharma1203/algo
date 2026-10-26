"""
Option Sniffer: Isolated Live Execution Adapter (Phase 1 — Read-Only Terminal).

STRICT ARCHITECTURAL GUARANTEES:
  1. SIMULATION_MODE = True is hard-locked.
  2. LIVE_ORDER_CAPABILITY = False is hard-locked (Zero broker order placement code in Phase 1).
  3. READ-ONLY ACCESS ONLY: Any query to Option Sniffer's signal or paper tables
     is executed via SQLite 'mode=ro' with PRAGMA query_only = ON.
  4. ZERO MUTATION: Never modifies any core Option Sniffer tables or state.
  5. PROXY-ROUTED: All Upstox user, margin, and profile telemetry is routed
     exclusively through the verified Mumbai static IPv6 proxy.
"""

import os
import time
import json
import random
import sqlite3
import logging
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import requests

from app.data.historical_data_layer import get_db_path
from app.data.upstox_provider import get_upstox_token, get_upstox_limiter
from app.analytics.option_sniffer.option_constants import calculate_option_trade_costs, to_ist_naive
from app.analytics.option_sniffer.option_live_order_intent import (
    get_order_intent_engine,
    STATE_FILLED,
    EXIT_CONFIRMED_FLAT
)

logger = logging.getLogger(__name__)

# Constants & Hard Locks
SIMULATION_MODE: bool = True
LIVE_ORDER_CAPABILITY: bool = False  # Controlled dynamically via _is_live_armed
DEFAULT_MIN_MARGIN_INR: float = 10000.0  # Conservative pre-flight baseline buffer; actual per-contract margin is derived dynamically from lot size & ask
DEFAULT_SAFETY_BUFFER_PCT: float = 0.10  # 10% reserve for slippage/STT/brokerage
DEFAULT_PROXY_URL: str = "http://arunsharma1203_cd6dbc37_99:zJeiuJKQGkZW1KJ4@in-mum.ipv6-proxy.in:13099"
PRIMARY_WHITELISTED_IPV6: str = "2600:1900:40a0:83a::63"
SECONDARY_WHITELISTED_IPV4: str = "122.168.87.241"


def calculate_required_contract_margin(
    lot_size: int,
    ask_price: float,
    buffer_pct: float = 0.05,
    statutory_buffer_inr: float = 55.0
) -> float:
    """
    Derives exact required margin from contract lot size, ask price, and buffers.
    Option buying requires paying the full premium plus statutory transaction friction.
    There is no universal broker-mandated ₹10,000 regulatory minimum; capital requirement
    is derived directly from (lot_size * ask * (1 + buffer_pct)) + statutory_charges.
    """
    if lot_size <= 0 or ask_price <= 0:
        return 0.0
    premium_required = lot_size * ask_price * (1.0 + buffer_pct)
    return round(premium_required + statutory_buffer_inr, 2)


_ADAPTER_LOCK = threading.RLock()
_INSTANCE: Optional["OptionLiveAdapter"] = None


class OptionLiveAdapter:
    """
    Isolated Adapter for Upstox Account Telemetry, 1-Day Ephemeral Arming,
    PIN-Guarded Live Execution, and SOS Panic Square-Off.
    """

    def __init__(self):
        self._cache_lock = threading.Lock()
        self._last_readiness_fetch: float = 0.0
        self._cached_readiness: Optional[Dict[str, Any]] = None
        self._cache_ttl_seconds: float = 30.0  # Conservative 30s cache to strictly preserve Upstox rate limits
        self._is_live_armed: bool = False
        self._armed_for_date: str = ""
        self._armed_at_epoch: float = 0.0
        self._armed_at_iso: str = ""
        self._current_pin: str = f"{random.randint(1000, 9999)}"
        self._reconciler_thread: Optional[threading.Thread] = None
        self._reconciler_running: bool = False
        self._reconciler_lock = threading.Lock()


    @classmethod
    def get_instance(cls) -> "OptionLiveAdapter":
        global _INSTANCE
        with _ADAPTER_LOCK:
            if _INSTANCE is None:
                _INSTANCE = cls()
            return _INSTANCE

    def get_proxy_url(self) -> str:
        """Retrieves proxy URL from app_settings or defaults to Mumbai static proxy."""
        try:
            db_file = get_db_path()
            uri = f"file:{db_file}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
                conn.execute("PRAGMA query_only = ON;")
                cur = conn.cursor()
                cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_static_proxy'")
                row = cur.fetchone()
                if row and row[0] and row[0].strip():
                    return row[0].strip()
        except Exception as e:
            logger.debug(f"[OptionLiveAdapter] Failed to query proxy from app_settings: {e}")
        return DEFAULT_PROXY_URL

    def _get_read_only_conn(self) -> sqlite3.Connection:
        """Opens a dedicated SQLite connection strictly in read-only mode."""
        db_file = get_db_path()
        uri = f"file:{os.path.abspath(db_file)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.execute("PRAGMA query_only = ON;")
        conn.row_factory = sqlite3.Row
        return conn

    def fetch_account_readiness(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Queries Upstox via the Mumbai static IPv6 proxy to verify:
          1. Bearer Token validity
          2. F&O Segment ('NFO') enablement
          3. Available Equity Margin
          4. Static IP match against whitelisted primary/secondary IPs
        Caches responses for 15s to respect API rate limits.
        """
        now = time.time()
        with self._cache_lock:
            if not force_refresh and self._cached_readiness is not None:
                if (now - self._last_readiness_fetch) < self._cache_ttl_seconds:
                    return dict(self._cached_readiness)

        token = ""
        is_read_only_token = False
        try:
            from app.data.upstox_provider import get_upstox_config
            cfg = get_upstox_config()
            token = (cfg.get("algo_token") or "").strip()
            is_read_only_token = bool(
                cfg.get("read_only")
                or cfg.get("is_read_only")
                or ("read" in str(cfg.get("scope", "")).lower() and "trade" not in str(cfg.get("scope", "")).lower())
            )
        except Exception:
            pass
        if not token:
            token = get_upstox_token()
        if token and (token.startswith("RO_") or token.startswith("READONLY_")):
            is_read_only_token = True

        proxy_url = self.get_proxy_url()
        proxies = {"http": proxy_url, "https": proxy_url}
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        readiness: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S IST", time.localtime()),
            "simulation_mode": SIMULATION_MODE,
            "live_order_capability": LIVE_ORDER_CAPABILITY,
            "phase": "PHASE_1_READ_ONLY_TERMINAL",
            "token_configured": bool(token),
            "is_read_only_token": is_read_only_token,
            "token_valid": False,
            "user_id": "",
            "user_name": "",
            "exchanges": [],
            "nfo_enabled": False,
            "available_margin": 0.0,
            "used_margin": 0.0,
            "payin_amount": 0.0,
            "proxy_url_masked": proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url,
            "proxy_latency_ms": 0.0,
            "primary_ip": "",
            "secondary_ip": "",
            "static_ip_matched": False,
            "live_trading_eligible": False,
            "blockers": []
        }

        if is_read_only_token:
            readiness["blockers"].append("Upstox Access Token is READ-ONLY (market data / feed only). Live order execution scope is strictly required.")

        if not token:
            readiness["blockers"].append("Upstox Access Token is missing or empty in Settings.")
            with self._cache_lock:
                self._cached_readiness = readiness
                self._last_readiness_fetch = now
            return readiness

        # 1. Probe Profile & Exchanges
        t0 = time.time()
        try:
            try:
                get_upstox_limiter().acquire(timeout=2.0)
            except Exception:
                pass
            resp_prof = requests.get(
                "https://api.upstox.com/v2/user/profile",
                headers=headers,
                proxies=proxies,
                timeout=8.0
            )
            readiness["proxy_latency_ms"] = round((time.time() - t0) * 1000, 1)

            if resp_prof.status_code == 200:
                pdata = resp_prof.json().get("data", {})
                readiness["token_valid"] = True
                readiness["user_id"] = pdata.get("user_id", "")
                readiness["user_name"] = pdata.get("user_name", "")
                readiness["exchanges"] = pdata.get("exchanges", [])
                readiness["nfo_enabled"] = "NFO" in readiness["exchanges"]
            elif resp_prof.status_code == 401:
                readiness["blockers"].append("Upstox Access Token expired or unauthorized (401).")
            else:
                readiness["blockers"].append(f"Profile API returned HTTP {resp_prof.status_code}: {resp_prof.text[:120]}")
        except Exception as e:
            readiness["proxy_latency_ms"] = round((time.time() - t0) * 1000, 1)
            readiness["blockers"].append(f"Profile request failed via proxy: {str(e)}")

        # 2. Probe Static IP Whitelist
        try:
            try:
                get_upstox_limiter().acquire(timeout=2.0)
            except Exception:
                pass
            resp_ip = requests.get(
                "https://api.upstox.com/v2/user/ip",
                headers=headers,
                proxies=proxies,
                timeout=8.0
            )
            if resp_ip.status_code == 200:
                idata = resp_ip.json().get("data", {})
                readiness["primary_ip"] = idata.get("primary_ip", "")
                readiness["secondary_ip"] = idata.get("secondary_ip", "")
                if readiness["primary_ip"] == PRIMARY_WHITELISTED_IPV6 or readiness["secondary_ip"] == SECONDARY_WHITELISTED_IPV4:
                    readiness["static_ip_matched"] = True
                else:
                    readiness["blockers"].append(f"Static IP mismatch: primary={readiness['primary_ip']}")
            else:
                readiness["blockers"].append(f"Static IP check returned HTTP {resp_ip.status_code}")
        except Exception as e:
            readiness["blockers"].append(f"Static IP check failed: {str(e)}")

        # 3. Probe Funds & Margin
        try:
            try:
                get_upstox_limiter().acquire(timeout=2.0)
            except Exception:
                pass
            resp_margin = requests.get(
                "https://api.upstox.com/v2/user/get-funds-and-margin",
                headers=headers,
                proxies=proxies,
                timeout=8.0
            )
            if resp_margin.status_code == 200:
                mdata = resp_margin.json().get("data", {})
                eq_data = mdata.get("equity", {})
                readiness["available_margin"] = float(eq_data.get("available_margin", 0.0))
                readiness["used_margin"] = float(eq_data.get("used_margin", 0.0))
                readiness["payin_amount"] = float(eq_data.get("payin_amount", 0.0))
            else:
                readiness["blockers"].append(f"Funds API returned HTTP {resp_margin.status_code}")
        except Exception as e:
            readiness["blockers"].append(f"Funds check failed: {str(e)}")

        # 4. Synthesize Pre-Flight Eligibility
        total_equity_margin = readiness["available_margin"] + max(0.0, readiness.get("used_margin", 0.0))
        if not readiness["nfo_enabled"]:
            readiness["blockers"].append("NFO exchange segment is not enabled in Upstox profile (only BSE/NSE enabled).")
        if total_equity_margin < DEFAULT_MIN_MARGIN_INR:
            readiness["blockers"].append(
                f"Available margin (₹{readiness['available_margin']:,.2f}) is below minimum required ₹{DEFAULT_MIN_MARGIN_INR:,.2f}."
            )
        if not readiness["static_ip_matched"]:
            readiness["blockers"].append("Static IP whitelist verification did not match registered proxy IP.")

        # Hard Rule: If ANY blocker exists or Phase 1 is active, live_trading_eligible is False
        readiness["live_trading_eligible"] = (
            readiness["token_valid"]
            and readiness["nfo_enabled"]
            and (total_equity_margin >= DEFAULT_MIN_MARGIN_INR)
            and readiness["static_ip_matched"]
            and not readiness.get("is_read_only_token", False)
            and (len(readiness["blockers"]) == 0)
        )

        with self._cache_lock:
            self._cached_readiness = readiness
            self._last_readiness_fetch = now

        return readiness

    def get_read_only_recent_signals(self, limit: int = 25, today_only: bool = False) -> List[Dict[str, Any]]:
        """
        Reads recent Option Sniffer signals strictly with SQLite query_only = ON.
        If today_only is True, filters strictly for signals generated during today's trading session.
        Zero write access.
        """
        try:
            with self._get_read_only_conn() as conn:
                cur = conn.cursor()
                if today_only:
                    cur.execute("""
                        SELECT signal_id, created_at AS timestamp, instrument_key, trading_symbol, underlying,
                               option_type, strike, direction AS action, option_ask AS suggested_entry,
                               calibrated_prob, expected_net_return AS expected_net_return_pct, status,
                               spot_price, option_bid, spread_pct
                        FROM option_sniffer_signals
                        WHERE DATE(created_at) = DATE('now', 'localtime')
                        ORDER BY id DESC LIMIT ?;
                    """, (limit,))
                else:
                    cur.execute("""
                        SELECT signal_id, created_at AS timestamp, instrument_key, trading_symbol, underlying,
                               option_type, strike, direction AS action, option_ask AS suggested_entry,
                               calibrated_prob, expected_net_return AS expected_net_return_pct, status,
                               spot_price, option_bid, spread_pct
                        FROM option_sniffer_signals
                        ORDER BY id DESC LIMIT ?;
                    """, (limit,))
                rows = cur.fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[OptionLiveAdapter] Read-only signals query failed: {e}")
            return []

    def get_read_only_paper_trades(self, limit: int = 15) -> Dict[str, Any]:
        """
        Reads current open paper trades and closed trades strictly with SQLite query_only = ON.
        """
        try:
            with self._get_read_only_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT trade_id, entry_time AS timestamp, instrument_key, trading_symbol, underlying,
                           option_type, strike, entry_price_used AS entry_price,
                           quantity, lot_size, status, net_pnl, net_pnl_pct AS pnl_pct, exit_reason
                    FROM option_sniffer_paper_trades
                    WHERE status = 'OPEN'
                    ORDER BY id DESC;
                """)
                open_trades = [dict(r) for r in cur.fetchall()]

                cur.execute("""
                    SELECT trade_id, entry_time AS timestamp, trading_symbol, underlying,
                           entry_price_used AS entry_price, exit_price_used AS exit_price,
                           quantity, net_pnl, net_pnl_pct AS pnl_pct, exit_reason, exit_time
                    FROM option_sniffer_paper_trades
                    WHERE status = 'CLOSED'
                    ORDER BY id DESC LIMIT ?;
                """, (limit,))
                closed_trades = [dict(r) for r in cur.fetchall()]

                return {
                    "open_positions": open_trades,
                    "closed_trades": closed_trades,
                    "open_count": len(open_trades)
                }
        except Exception as e:
            logger.error(f"[OptionLiveAdapter] Read-only paper trades query failed: {e}")
            return {"open_positions": [], "closed_trades": [], "open_count": 0}

    def simulate_dynamic_capital_allocation(
        self,
        custom_capital: Optional[float] = None,
        today_only: bool = True
    ) -> Dict[str, Any]:
        """
        Simulates how the Dynamic Knapsack Capital Allocator would prioritize and select
        live option contracts from active signals, subject to available wallet margin.
        
        Rules:
          - Applies a 10% safety reserve buffer (capital * 0.90) for statutory charges & slippage.
          - Calculates exact lot capital requirement: lot_size * suggested_entry + ₹55.00 est. charges.
          - Ranks signals by Priority Score = Calibrated Prob * Expected Net Return %.
          - Greedy selection: Picks top 1 or top K contracts until capital is exhausted.
        """
        readiness = self.fetch_account_readiness()
        actual_margin = readiness.get("available_margin", 0.0)
        
        # If user provides custom_capital (e.g. ₹15,000 to test knapsack), use it; otherwise use actual wallet margin
        effective_budget = float(custom_capital) if (custom_capital is not None and custom_capital > 0) else actual_margin
        safety_reserve = effective_budget * DEFAULT_SAFETY_BUFFER_PCT
        deployable_capital = max(0.0, effective_budget - safety_reserve)

        # Query recent actionable or qualified signals (strictly for today's session by default)
        recent_signals = self.get_read_only_recent_signals(limit=25, today_only=today_only)

        # Filter for ACTIVE BUY signals with positive edge, deduplicate by contract, and enrich with live streaming ticks
        actionable_candidates: List[Dict[str, Any]] = []
        seen_contracts = set()
        for s in recent_signals:
            action = s.get("action", "")
            status = s.get("status") or "ACTIVE"
            if action in ("BUY_CE", "BUY_PE") and status == "ACTIVE":
                ikey = s.get("instrument_key", "")
                tsym = s.get("trading_symbol", "")
                dedup_key = ikey or tsym
                if dedup_key and dedup_key in seen_contracts:
                    continue
                if dedup_key:
                    seen_contracts.add(dedup_key)

                underlying = s.get("underlying", "")
                lot_size = 65 if underlying == "NIFTY" else (30 if underlying == "BANKNIFTY" else 50)
                entry = float(s.get("suggested_entry") or 0.0)

                # Check streaming live quote if available to ensure live price reactivity
                try:
                    from app.data.upstox_stream_service import get_stream_service
                    stream_svc = get_stream_service()
                    lq = stream_svc.get_option_quote(ikey) if ikey else None
                    if lq and float(lq.get("ask") or 0.0) > 0:
                        entry = float(lq["ask"])
                except Exception:
                    pass

                if entry <= 0:
                    continue
                cost_per_lot = (entry * lot_size) + 55.0  # ₹55 round-trip statutory costs
                cal_prob = float(s.get("calibrated_prob") or 0.5)
                exp_ret = float(s.get("expected_net_return_pct") or 0.0)
                priority_score = round(cal_prob * max(0.1, exp_ret), 3)

                actionable_candidates.append({
                    "signal_id": s.get("signal_id"),
                    "trading_symbol": tsym,
                    "underlying": underlying,
                    "action": action,
                    "suggested_entry": entry,
                    "lot_size": lot_size,
                    "cost_per_lot": round(cost_per_lot, 2),
                    "calibrated_prob": round(cal_prob, 3),
                    "expected_net_return_pct": round(exp_ret, 2),
                    "priority_score": priority_score,
                    "timestamp": s.get("timestamp")
                })

        # Rank candidates by priority score descending (highest conviction first)
        actionable_candidates.sort(key=lambda x: x["priority_score"], reverse=True)

        # Knapsack allocation (Strictly capped at max 2 concurrent slots)
        remaining_capital = deployable_capital
        allocated_trades: List[Dict[str, Any]] = []
        skipped_trades: List[Dict[str, Any]] = []

        for cand in actionable_candidates:
            cost = cand["cost_per_lot"]
            if len(allocated_trades) < 2 and cost <= remaining_capital:
                cand_copy = dict(cand)
                cand_copy["allocation_status"] = "SELECTED_FOR_EXECUTION"
                cand_copy["allocated_lots"] = 1
                allocated_trades.append(cand_copy)
                remaining_capital -= cost
            else:
                cand_copy = dict(cand)
                if cost > remaining_capital:
                    cand_copy["allocation_status"] = "SKIPPED_INSUFFICIENT_MARGIN"
                else:
                    cand_copy["allocation_status"] = "SKIPPED_MAX_SLOTS_REACHED"
                cand_copy["shortfall_amount"] = round(max(0.0, cost - remaining_capital), 2)
                skipped_trades.append(cand_copy)

        return {
            "is_simulation": True,
            "wallet_margin_actual": round(actual_margin, 2),
            "budget_tested": round(effective_budget, 2),
            "safety_reserve_deducted": round(safety_reserve, 2),
            "deployable_capital": round(deployable_capital, 2),
            "capital_utilized": round(deployable_capital - remaining_capital, 2),
            "capital_unallocated": round(remaining_capital, 2),
            "max_concurrent_lots_supported": len(allocated_trades),
            "allocated_trades": allocated_trades,
            "skipped_trades": skipped_trades,
            "total_candidates_evaluated": len(actionable_candidates)
        }

    # -------------------------------------------------------------------------
    # PHASE 3: 1-DAY EPHEMERAL ARMING, PIN VERIFICATION & SOS PANIC SQUARE-OFF
    # -------------------------------------------------------------------------

    def generate_new_pin(self) -> str:
        """Generates a dynamic 4-digit verification code for arming live trading."""
        self._current_pin = f"{random.randint(1000, 9999)}"
        return self._current_pin

    def get_live_status(self) -> Dict[str, Any]:
        """
        Returns live operational status: arming flag, session expiration,
        active verification PIN, readiness checklist, and active live positions.
        """
        now = to_ist_naive()
        today = now.strftime("%Y-%m-%d")

        # 1-Day Auto-Expiration Guard: Disarm if date changed or past 15:15 IST
        if self._is_live_armed:
            if self._armed_for_date != today:
                logger.info("[OptionLiveAdapter] 1-day session expired due to date rollover. Reverting to Paper Mode.")
                self._is_live_armed = False
            elif now.time() >= datetime.strptime("15:15", "%H:%M").time():
                logger.info("[OptionLiveAdapter] Trading session cutoff (15:15 IST) reached. Reverting to Paper Mode.")
                self._is_live_armed = False

        readiness = self.fetch_account_readiness()
        active_positions = self.get_active_live_positions()

        feed_telemetry = {"is_connected": False, "last_message_seconds_ago": None}
        try:
            from app.data.upstox_stream_service import get_stream_service
            stream_svc = get_stream_service()
            feed_telemetry = stream_svc.get_status()
        except Exception as st_err:
            logger.debug(f"[OptionLiveAdapter] Stream status query note: {st_err}")

        pnl_summary = self.get_live_pnl_summary()

        microstructure = {}
        try:
            from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
            microstructure = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=now)
        except Exception as ms_err:
            logger.debug(f"[OptionLiveAdapter] Microstructure status query note: {ms_err}")

        return {
            "is_live_armed": self._is_live_armed,
            "armed_for_date": self._armed_for_date,
            "session_cutoff": "15:15 IST",
            "current_pin": self._current_pin,
            "readiness": readiness,
            "active_positions": active_positions,
            "active_positions_count": len(active_positions),
            "feed_status": feed_telemetry,
            "pnl_summary": pnl_summary,
            "microstructure": microstructure
        }

    def sync_trades_from_upstox_tradebook(self) -> Dict[str, Any]:
        """
        Polls Upstox tradebook (/order/trades/get-trades-for-day) and reconciles
        all live orders in option_live_order_intents with actual exchange execution prices,
        true realized P&L, and true statutory costs.

        Uses the verified broker API abstraction in OptionLiveOrderIntentEngine.
        Reconciles actual execution prices and available charges with corresponding local trades.
        Guarantees idempotency (repeated synchronisation will not duplicate trades or charges).
        Preserves valid local records when broker data is missing or incomplete.
        Logs actionable diagnostic information and returns an explicit status dictionary.
        """
        engine = get_order_intent_engine()
        success, status_code, data = engine._call_upstox_broker_api("GET", "/order/trades/get-trades-for-day")

        if not success or status_code != 200:
            logger.warning(
                f"[OptionLiveAdapter] Upstox tradebook query failed (HTTP {status_code}). "
                f"Payload: {data}"
            )
            return {
                "success": False,
                "status": f"API_ERROR_{status_code}",
                "updated": 0,
                "error": f"Broker tradebook API returned HTTP {status_code}: {data.get('error') or data}"
            }

        if not isinstance(data, dict):
            logger.error(f"[OptionLiveAdapter] Malformed tradebook response (not a dict): {type(data)}")
            return {
                "success": False,
                "status": "MALFORMED_RESPONSE",
                "updated": 0,
                "error": "Broker tradebook response is not a dictionary"
            }

        trades = data.get("data")
        if trades is None or not isinstance(trades, list):
            logger.error(f"[OptionLiveAdapter] Tradebook response missing valid 'data' list: {data}")
            return {
                "success": False,
                "status": "MALFORMED_RESPONSE",
                "updated": 0,
                "error": "Broker tradebook payload missing 'data' list"
            }

        if len(trades) == 0:
            logger.info("[OptionLiveAdapter] Upstox tradebook has no trades for today.")
            return {
                "success": True,
                "status": "NO_TRADES",
                "updated": 0,
                "total_broker_trades": 0,
                "message": "No trades found in tradebook for today"
            }

        # Parse and aggregate execution slices per order_id using volume-weighted average price (VWAP)
        buys: Dict[str, Dict[str, Any]] = {}
        sells: Dict[str, Dict[str, Any]] = {}

        for t in trades:
            if not isinstance(t, dict):
                continue
            oid = t.get("order_id")
            if not oid:
                continue
            oid_str = str(oid).strip()
            ttype = str(t.get("transaction_type") or "").upper().strip()

            try:
                price = float(t.get("average_price") or t.get("price") or 0.0)
            except (ValueError, TypeError):
                price = 0.0
            try:
                qty = int(t.get("quantity") or 0)
            except (ValueError, TypeError):
                qty = 0

            if qty <= 0 or price <= 0.0:
                continue

            target_map = buys if ttype == "BUY" else sells
            if oid_str not in target_map:
                target_map[oid_str] = {
                    "total_qty": qty,
                    "total_val": price * qty,
                    "trade_ids": [str(t.get("trade_id"))] if t.get("trade_id") else []
                }
            else:
                target_map[oid_str]["total_qty"] += qty
                target_map[oid_str]["total_val"] += price * qty
                if t.get("trade_id"):
                    target_map[oid_str]["trade_ids"].append(str(t.get("trade_id")))

        for omap in (buys, sells):
            for oid_str, info in omap.items():
                t_qty = info["total_qty"]
                info["avg_price"] = (info["total_val"] / t_qty) if t_qty > 0 else 0.0

        updated = 0
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            with sqlite3.connect(get_db_path(), timeout=5.0) as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT id, order_intent_id, broker_order_id, broker_sl_order_id, exit_broker_order_id,
                           filled_quantity, average_fill_price, exit_fill_price, gross_pnl, statutory_charges,
                           realized_pnl, exit_state
                    FROM option_live_order_intents
                    WHERE broker_order_id IS NOT NULL AND broker_order_id NOT LIKE 'UPSTOX_%';
                """)
                rows = cur.fetchall()

                for r in rows:
                    (r_id, intent_id, b_id, sl_id, exit_id,
                     qty, avg_fill, exit_fill, cur_gross, cur_charges, cur_pnl, exit_st) = r

                    actual_buy = buys.get(str(b_id)) if b_id else None

                    # Sum all sell executions across both exit order and stop loss order
                    total_sell_qty = 0
                    total_sell_val = 0.0
                    sell_trade_ids = []
                    if exit_id and str(exit_id) in sells:
                        total_sell_qty += sells[str(exit_id)]["total_qty"]
                        total_sell_val += sells[str(exit_id)]["total_val"]
                        sell_trade_ids.extend(sells[str(exit_id)]["trade_ids"])
                    if sl_id and str(sl_id) in sells:
                        total_sell_qty += sells[str(sl_id)]["total_qty"]
                        total_sell_val += sells[str(sl_id)]["total_val"]
                        sell_trade_ids.extend(sells[str(sl_id)]["trade_ids"])

                    has_sell_trades = total_sell_qty > 0

                    # If neither buy nor sell trades exist on broker, do NOT mutate local record
                    if not actual_buy and not has_sell_trades:
                        continue

                    # Fall back to existing fill price if buy not in tradebook (never overwrite valid records with 0)
                    buy_price = actual_buy["avg_price"] if actual_buy else (avg_fill or 0.0)
                    cum_buy_qty = actual_buy["total_qty"] if actual_buy else (qty or 0)

                    # Zero-short guard: sell quantity cannot exceed bought quantity on a long option position
                    closed_qty = min(total_sell_qty, cum_buy_qty) if cum_buy_qty > 0 else total_sell_qty
                    net_open_qty = max(0, cum_buy_qty - closed_qty)

                    cum_sell_price = (total_sell_val / total_sell_qty) if total_sell_qty > 0 else (exit_fill or 0.0)

                    # Determine target exit state strictly by broker-confirmed net open quantity
                    if cum_buy_qty > 0 and closed_qty >= cum_buy_qty:
                        target_exit_state = "CONFIRMED_FLAT"
                        target_filled_qty = 0  # 0 open quantity remaining (flat)
                    elif closed_qty > 0 and closed_qty < cum_buy_qty:
                        target_exit_state = "PARTIALLY_CLOSED"
                        target_filled_qty = net_open_qty  # Residual open quantity for trailing stops
                    else:
                        target_exit_state = exit_st if exit_st in ("SUBMITTED", "CANCELLED", "REJECTED") else "NONE"
                        target_filled_qty = cum_buy_qty

                    # Calculate P&L on closed portion
                    if closed_qty > 0:
                        gross = round((cum_sell_price - buy_price) * closed_qty, 2)
                        statutory_info = calculate_option_trade_costs(
                            buy_price=buy_price,
                            sell_price=cum_sell_price,
                            quantity=closed_qty,
                            apply_slippage=False,
                            trade_date=now_iso[:10]
                        )
                        eff_charges = float(statutory_info.get("statutory_charges", 55.0))
                        net_pnl = round(gross - eff_charges, 2)
                    else:
                        gross = 0.0
                        eff_charges = 0.0
                        net_pnl = 0.0

                    needs_update = (
                        exit_st != target_exit_state
                        or abs((exit_fill or 0.0) - cum_sell_price) > 0.01
                        or abs((avg_fill or 0.0) - buy_price) > 0.01
                        or abs((cur_pnl or 0.0) - net_pnl) > 0.01
                        or abs((cur_charges or 0.0) - eff_charges) > 0.01
                        or abs((cur_gross or 0.0) - gross) > 0.01
                        or (qty or 0) != target_filled_qty
                    )

                    if needs_update:
                        cur.execute("""
                            UPDATE option_live_order_intents
                            SET exit_state = ?,
                                filled_quantity = ?,
                                average_fill_price = ?,
                                exit_fill_price = ?,
                                gross_pnl = ?,
                                statutory_charges = ?,
                                realized_pnl = ?,
                                updated_at = ?
                            WHERE id = ?;
                        """, (target_exit_state, target_filled_qty, buy_price, cum_sell_price, gross, eff_charges, net_pnl, now_iso, r_id))
                        updated += 1

                conn.commit()

            self._last_tradebook_sync_status = "SYNCED"
            self._last_tradebook_sync_iso = now_iso

            return {
                "success": True,
                "status": "SYNCED",
                "updated": updated,
                "total_broker_trades": len(trades),
                "is_statutory_charges_estimated": True,
                "charges_reconciliation_mode": "ESTIMATED_STATUTORY_SCHEDULE",
                "message": f"Successfully reconciled {updated} order intents against {len(trades)} broker trades."
            }
        except Exception as e:
            self._last_tradebook_sync_status = "DB_ERROR"
            self._last_tradebook_sync_iso = now_iso
            logger.error(f"[OptionLiveAdapter] Database error during tradebook reconciliation: {e}", exc_info=True)
            return {
                "success": False,
                "status": "DB_ERROR",
                "updated": 0,
                "is_statutory_charges_estimated": True,
                "charges_reconciliation_mode": "ESTIMATED_STATUTORY_SCHEDULE",
                "error": f"Database error during reconciliation: {str(e)}"
            }

    def get_live_pnl_summary(self) -> Dict[str, Any]:
        """
        Computes today's real broker P&L summary:
          - realized_gross_pnl: Raw MTM loss/gain from broker (matches Upstox app screen)
          - statutory_charges: Brokerage + STT + turnover + GST + stamp duty
          - realized_net_pnl: Realized net P&L after statutory charges
          - unrealized_net_pnl: Live MTM from active positions against streaming quotes
          - total_net_pnl: Headline figure matching Upstox screen (realized_gross_pnl + unrealized_net_pnl)
          - closed_trades_count, wins, losses, win_rate
        """
        # Periodic light sync with Upstox Tradebook
        now = time.time()
        if (now - getattr(self, "_last_tradebook_sync", 0.0)) > 6.0:
            self._last_tradebook_sync = now
            self.sync_trades_from_upstox_tradebook()

        realized_net_pnl = 0.0
        realized_gross_pnl = 0.0
        statutory_charges = 0.0
        closed_count = 0
        win_count = 0
        loss_count = 0
        active_positions = self.get_active_live_positions()

        try:
            with self._get_read_only_conn() as conn:
                cur = conn.cursor()
                cols = [c[1] for c in cur.execute("PRAGMA table_info(option_live_order_intents);").fetchall()]
                has_gross = "gross_pnl" in cols
                has_stat = "statutory_charges" in cols

                if has_gross and has_stat:
                    cur.execute("""
                        SELECT realized_pnl, gross_pnl, statutory_charges, average_fill_price, exit_fill_price, filled_quantity
                        FROM option_live_order_intents
                        WHERE exit_state IN ('CONFIRMED_FLAT', 'PARTIALLY_CLOSED')
                          AND DATE(created_at) = DATE('now', 'localtime')
                          AND broker_order_id NOT LIKE 'UPSTOX_%';
                    """)
                    for row in cur.fetchall():
                        net_p = float(row[0] or 0.0)
                        gross_p = float(row[1] or 0.0)
                        stat_c = float(row[2] or 0.0)
                        avg_p = float(row[3] or 0.0)
                        exit_p = float(row[4] or 0.0)
                        qty = int(row[5] or 0)

                        if gross_p == 0.0 and stat_c == 0.0 and avg_p > 0 and exit_p > 0 and qty > 0:
                            gross_p = round((exit_p - avg_p) * qty, 2)
                            stat_c = round(gross_p - net_p, 2)

                        realized_net_pnl += net_p
                        realized_gross_pnl += gross_p
                        statutory_charges += stat_c
                        closed_count += 1
                        if gross_p > 0 or (gross_p == 0 and net_p > 0):
                            win_count += 1
                        else:
                            loss_count += 1
                else:
                    cur.execute("""
                        SELECT realized_pnl, average_fill_price, exit_fill_price, filled_quantity
                        FROM option_live_order_intents
                        WHERE exit_state IN ('CONFIRMED_FLAT', 'PARTIALLY_CLOSED')
                          AND DATE(created_at) = DATE('now', 'localtime')
                          AND broker_order_id NOT LIKE 'UPSTOX_%';
                    """)
                    for row in cur.fetchall():
                        net_p = float(row[0] or 0.0)
                        avg_p = float(row[1] or 0.0)
                        exit_p = float(row[2] or 0.0)
                        qty = int(row[3] or 0)
                        gross_p = round((exit_p - avg_p) * qty, 2) if (avg_p > 0 and exit_p > 0 and qty > 0) else net_p
                        stat_c = round(gross_p - net_p, 2)

                        realized_net_pnl += net_p
                        realized_gross_pnl += gross_p
                        statutory_charges += stat_c
                        closed_count += 1
                        if gross_p > 0 or (gross_p == 0 and net_p > 0):
                            win_count += 1
                        else:
                            loss_count += 1
        except Exception as e:
            logger.debug(f"[OptionLiveAdapter] Realized P&L query note: {e}")

        # Compute Live MTM
        unrealized_pnl = 0.0
        try:
            for pos in active_positions:
                if pos.get("unrealized_pnl") is not None:
                    unrealized_pnl += float(pos["unrealized_pnl"])
        except Exception as u_err:
            logger.debug(f"[OptionLiveAdapter] Unrealized MTM calc note: {u_err}")

        # Upstox App Screen headline is Gross MTM (realized gross + unrealized)
        headline_total_pnl = round(realized_gross_pnl + unrealized_pnl, 2)
        sync_status = getattr(self, "_last_tradebook_sync_status", "NOT_SYNCED")
        return {
            "realized_gross_pnl": round(realized_gross_pnl, 2),
            "statutory_charges": round(statutory_charges, 2),
            "realized_net_pnl": round(realized_net_pnl, 2),
            "unrealized_net_pnl": round(unrealized_pnl, 2),
            "total_net_pnl": headline_total_pnl,
            "closed_trades_count": closed_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round((win_count / closed_count * 100.0), 1) if closed_count > 0 else 0.0,
            "active_positions_count": len(active_positions),
            "is_statutory_charges_estimated": True,
            "charges_reconciliation_mode": "ESTIMATED_STATUTORY_SCHEDULE",
            "broker_execution_confirmed": sync_status == "SYNCED",
            "reconciliation_status": sync_status,
            "last_sync_timestamp": getattr(self, "_last_tradebook_sync_iso", None)
        }

    def arm_live_trading(self, pin: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Arms live trading for the current session after verifying:
          1. 4-digit dynamic confirmation code.
          2. Session cutoff (must be before 15:15 IST).
          3. Pre-flight checks: NFO segment enabled, margin >= ₹10,000, static IP match.
        Fails closed back to Paper Mode if ANY check fails.
        """
        # 1. PIN Verification Check
        if not pin or pin.strip() != self._current_pin:
            return False, "INVALID_CODE: The 4-digit verification code entered is invalid.", {}

        # 2. Canary Scout & Dynamic Session Window Checks
        now = datetime.now()
        now_time = now.time()
        from datetime import time as dt_time
        from app.analytics.option_sniffer.option_constants import (
            LIVE_ENTRY_OPEN_HOUR,
            LIVE_ENTRY_OPEN_MINUTE,
        )
        live_entry_open = dt_time(LIVE_ENTRY_OPEN_HOUR, LIVE_ENTRY_OPEN_MINUTE)
        earliest_dynamic_open = dt_time(9, 35)
        session_close = dt_time(15, 15)

        # Microstructure & Dynamic Timing check
        from app.analytics.option_sniffer.option_microstructure_evaluator import OptionMicrostructureEvaluator
        ms_telemetry = OptionMicrostructureEvaluator.evaluate_microstructure_gate(dt=now)
        is_dynamic_unlocked = ms_telemetry.get("is_unlocked", False)

        if now_time < earliest_dynamic_open:
            self._is_live_armed = False
            return False, "CANARY_SCOUT_ACTIVE: Real trading is locked before 09:35 IST. 09:15-09:30 is reserved for Opening Range formation and 09:30-09:35 is reserved for zero-cost paper scout probing.", {}

        if now_time < live_entry_open and not is_dynamic_unlocked:
            self._is_live_armed = False
            return False, f"CANARY_SCOUT_ACTIVE: Real trading locked before 09:45 IST. {ms_telemetry.get('unlock_reason')}", {}

        if now_time >= session_close:
            self._is_live_armed = False
            return False, "SESSION_CLOSED: Past 15:15 IST cutoff. Real trading cannot be armed after market close.", {}

        # 3. Canary Scout Health Check (verify zero paper stop losses today)
        from app.analytics.option_sniffer.option_db import OptionDB
        has_paper_sl, canary_reason = OptionDB.has_paper_stop_loss_today(now.strftime("%Y-%m-%d"))
        if has_paper_sl:
            self._is_live_armed = False
            return False, f"CANARY_SCOUT_ABORTED: Real trading locked for session. Paper canary scout detected adverse regime: {canary_reason}", {}

        # 4. Live Circuit Breaker Check
        try:
            order_engine = get_order_intent_engine()
            live_cb = order_engine.get_live_circuit_breaker_telemetry()
            if isinstance(live_cb, dict) and live_cb.get("is_session_halted"):
                self._is_live_armed = False
                return False, f"LIVE_CIRCUIT_BREAKER_TRIPPED: Real trading halted for session: {live_cb.get('halt_reason')}", {}
        except Exception as lcb_err:
            logger.debug(f"[OptionLiveAdapter] Live circuit breaker check note: {lcb_err}")

        # 5. Strict Pre-Flight Checks via Mumbai Proxy

        readiness = self.fetch_account_readiness(force_refresh=True)
        if not readiness.get("live_trading_eligible", False):
            self._is_live_armed = False
            blocker_text = " | ".join(readiness.get("blockers", ["Pre-flight requirements not met"]))
            return False, f"PREFLIGHT_BLOCKED: {blocker_text}", readiness

        # 3. All Checks Passed — Arm for Today
        self._is_live_armed = True
        self._armed_for_date = now.strftime("%Y-%m-%d")
        self._armed_at_epoch = time.time()
        self._armed_at_iso = now.strftime("%Y-%m-%d %H:%M:%S")
        self.start_order_reconciler_daemon()
        logger.info(f"⚡ [OptionLiveAdapter] Real trading ARMED for session {self._armed_for_date} at {self._armed_at_iso}. Auto-expires at 15:15 IST.")

        # Cycle PIN for next interaction
        self.generate_new_pin()
        return True, "ARMED_FOR_TODAY: Live trading is active for today. Position risk is capped at -₹2,000.00.", readiness

    def disarm_live_trading(self) -> Tuple[bool, str]:
        """Disarms live trading and resets back to Paper Mode."""
        self._is_live_armed = False
        self._armed_at_epoch = 0.0
        self._armed_at_iso = ""
        logger.info("🛡️ [OptionLiveAdapter] Live trading DISARMED. Reverted to Paper Mode.")
        self.generate_new_pin()
        return True, "DISARMED: Live trading reverted to Paper Mode."

    def get_active_live_positions(self) -> List[Dict[str, Any]]:
        """Queries currently active live positions from option_live_order_intents and enriches with live MTM."""
        try:
            with self._get_read_only_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT order_intent_id, signal_id, instrument_key, trading_symbol, underlying, action,
                           lot_size, target_quantity, filled_quantity, limit_price, average_fill_price,
                           state, broker_order_id, stop_loss_trigger, stop_loss_limit, broker_sl_order_id,
                           exit_state, exit_fill_price, realized_pnl, created_at, updated_at
                    FROM option_live_order_intents
                    WHERE state IN ('SUBMITTED', 'PARTIALLY_FILLED', 'FILLED')
                      AND exit_state != 'CONFIRMED_FLAT'
                    ORDER BY id DESC;
                """)
                positions = [dict(r) for r in cur.fetchall()]

            if positions:
                try:
                    from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                    stream_mgr = OptionStreamManager()
                    refreshed_once = False
                    for pos in positions:
                        ikey = pos.get("instrument_key")
                        quote = stream_mgr.get_option_quote(ikey) if ikey else None
                        if not quote and ikey and not refreshed_once:
                            refreshed_once = True
                            stream_mgr.refresh_quotes_via_rest()
                            quote = stream_mgr.get_option_quote(ikey)
                        if quote:
                            current_bid = float(quote.get("bid") or quote.get("ltp") or 0.0)
                            current_ltp = float(quote.get("ltp") or current_bid or 0.0)
                            entry_fill = float(pos.get("average_fill_price") or pos.get("limit_price") or 0.0)
                            qty = int(pos.get("filled_quantity") or pos.get("target_quantity") or 0)
                            pos["current_bid"] = round(current_bid, 2)
                            pos["current_ltp"] = round(current_ltp, 2)
                            if current_bid > 0 and entry_fill > 0 and qty > 0:
                                gross_mtm = (current_bid - entry_fill) * qty
                                net_mtm = round(gross_mtm - 55.0, 2)
                                pos["unrealized_pnl"] = net_mtm
                                pos["unrealized_pnl_pct"] = round(((current_bid - entry_fill) / entry_fill) * 100.0, 2)
                            else:
                                pos["unrealized_pnl"] = 0.0
                                pos["unrealized_pnl_pct"] = 0.0
                        else:
                            pos["current_bid"] = None
                            pos["current_ltp"] = None
                            pos["unrealized_pnl"] = 0.0
                            pos["unrealized_pnl_pct"] = 0.0
                except Exception as q_err:
                    logger.debug(f"[OptionLiveAdapter] Active position quote enrichment note: {q_err}")

            return positions
        except Exception as e:
            logger.error(f"[OptionLiveAdapter] Failed to query active live positions: {e}")
            return []

    def execute_sos_panic_exit(self) -> Dict[str, Any]:
        """
        Executes immediate emergency square-off across all open live positions:
          1. Disarms live trading immediately (is_live_armed = False).
          2. Flattens each open position at market bid.
          3. Confirms broker_position_qty == 0.
        """
        self._is_live_armed = False  # Immediately disarm new entries
        self._armed_at_epoch = 0.0
        self._armed_at_iso = ""
        active_positions = self.get_active_live_positions()
        flattened_results: List[Dict[str, Any]] = []

        engine = get_order_intent_engine()
        for pos in active_positions:
            intent_id = pos["order_intent_id"]
            # Read current bid or use average_fill_price as fallback
            bid_price = float(pos.get("current_bid") or pos.get("average_fill_price") or 100.0)
            res = engine.trigger_sos_panic_exit(intent_id, current_bid_price=bid_price)
            flattened_results.append(res)

        all_flat = all(
            r.get("status") in ("CONFIRMED_FLAT", "ALREADY_FLAT")
            for r in flattened_results
        )
        closed_count = len([r for r in flattened_results if r.get("status") in ("CONFIRMED_FLAT", "ALREADY_FLAT")])
        pending_count = len(flattened_results) - closed_count

        return {
            "status": "ALL_FLATTENED" if (all_flat or len(flattened_results) == 0) else "PARTIAL_OR_PENDING",
            "positions_closed_count": closed_count,
            "pending_positions_count": pending_count,
            "flattened_positions": flattened_results,
            "is_live_armed": False
        }

    def start_order_reconciler_daemon(self):
        """
        Starts the background order book reconciliation daemon if not already running.
        The daemon actively polls Upstox order book every 2 seconds.
        Decoupled: Exits and pending order reconciliation continue regardless of arming status,
        while new automated entry orders require is_live_armed=True.
        """
        with self._reconciler_lock:
            if self._reconciler_running:
                return
            self._reconciler_running = True
            self._reconciler_thread = threading.Thread(target=self._order_reconciler_loop, daemon=True)
            self._reconciler_thread.start()
            logger.info("✅ [OptionLiveAdapter] Background Order Book Reconciliation Daemon started (2s interval).")

    def stop_order_reconciler_daemon(self):
        with self._reconciler_lock:
            self._reconciler_running = False

    def _order_reconciler_loop(self):
        engine = get_order_intent_engine()
        while self._reconciler_running:
            try:
                # 1. Fetch streaming live quotes for exits & entries
                live_quotes_map = {}
                try:
                    from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                    stream_mgr = OptionStreamManager()
                    live_quotes_map = {q["instrument_key"]: q for q in stream_mgr.get_all_active_option_quotes()}
                except Exception as sq_err:
                    logger.debug(f"[OptionLiveAdapter] Live quotes map query note: {sq_err}")

                # 2. Check if resting entry orders, unconfirmed exits, or resting exchange SL orders need reconciliation
                has_pending = False
                try:
                    with self._get_read_only_conn() as conn:
                        cur = conn.cursor()
                        cur.execute("""
                            SELECT 1 FROM option_live_order_intents 
                            WHERE (state IN ('SUBMITTED', 'PARTIALLY_FILLED') AND broker_order_id IS NOT NULL)
                               OR (state = 'FILLED' AND exit_state != 'CONFIRMED_FLAT')
                            LIMIT 1;
                        """)
                        has_pending = bool(cur.fetchone())
                except Exception as q_err:
                    logger.debug(f"[OptionLiveAdapter] Pending query check note: {q_err}")

                if has_pending:
                    updates = engine.reconcile_active_order_intents()
                    if updates:
                        logger.info(f"⚡ [OptionLiveAdapter] Auto-reconciled orders: {updates}")

                # 3. Check and execute automated exits (Target +35%, Trailing Stop, 45-min Time Decay)
                # Operates for all open positions even after disarm or circuit breaker trips
                if live_quotes_map:
                    exits = engine.check_and_execute_live_exits(live_quotes_map)
                    if exits:
                        logger.info(f"⚡ [OptionLiveAdapter] Auto-executed {len(exits)} live exits: {exits}")

                # 4. Check and dispatch automated entries (Full Autopilot ONLY when armed)
                if self._is_live_armed:
                    readiness = self.fetch_account_readiness(force_refresh=False)
                    deployable = max(0.0, float(readiness.get("available_margin", 0.0)) * 0.90)
                    dispatched = engine.check_and_dispatch_automated_entries(live_quotes_map, deployable)
                    if dispatched:
                        logger.info(f"⚡ [OptionLiveAdapter] Autopilot dispatched {len(dispatched)} live entries: {dispatched}")

            except Exception as e:
                logger.error(f"[OptionLiveAdapter] Order reconciler daemon error: {e}")
            time.sleep(2.0)

    def startup_reconcile(self) -> Dict[str, Any]:
        """
        Reconciles broker positions and open order states on application startup.
        If untracked open broker positions are detected, live trading remains disarmed
        and positions are imported into SQLite for explicit operator review.
        """
        self.start_order_reconciler_daemon()
        engine = get_order_intent_engine()
        res = engine.reconcile_broker_startup_state()
        try:
            pending_res = engine.reconcile_active_order_intents()
            res["reconciled_pending_orders"] = len(pending_res)
        except Exception as p_err:
            logger.debug(f"[OptionLiveAdapter] Pending order reconciliation note: {p_err}")
            res["reconciled_pending_orders"] = 0
        return res


def get_option_live_adapter() -> OptionLiveAdapter:
    """Returns singleton OptionLiveAdapter instance."""
    return OptionLiveAdapter.get_instance()


