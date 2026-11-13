"""
Option Sniffer: Contract Discovery & Expiry Rollover Engine.

Discovers and maintains controlled ATM option contracts for NIFTY and BANKNIFTY:
  - Fetches option contract lists from Upstox API v2 (/v2/option/contract) via centralized rate governor.
  - Automatically identifies nearest current weekly/monthly expiry and next expiry.
  - Dynamically calculates current ATM strike from live spot price.
  - Selects controlled strike window (ATM +/- 5 strikes) for CE and PE.
  - Caches contract metadata to strictly avoid unnecessary REST calls.
  - Safely updates candidates when spot moves beyond 1 strike interval.
"""

import logging
import time
from datetime import datetime, date
from typing import Dict, Any, List, Optional, Set, Tuple

from app.data.upstox_provider import (
    upstox_request,
    get_upstox_token,
    UPSTOX_BASE_URL,
    fetch_upstox_ltp,
)
from app.analytics.option_sniffer.option_constants import (
    UNDERLYING_INSTRUMENTS,
    ATM_STRIKE_WINDOW,
    STRIKE_INTERVALS,
    DEFAULT_LOT_SIZES,
    MAX_OPTION_EXPIRY_DTE,
    to_ist_naive,
)
from app.analytics.option_sniffer.option_db import OptionDB

logger = logging.getLogger(__name__)


class OptionContractDiscovery:
    """
    Manages discovery, filtering, and dynamic updating of monitored option contracts
    for NIFTY and BANKNIFTY.
    """

    _CONTRACTS_CACHE: Dict[str, Dict[str, Any]] = {}  # underlying -> {data, timestamp}
    _FUTURES_CACHE: Dict[str, Any] = {"data": {}, "timestamp": 0.0, "session_date": ""}
    _CACHE_TTL_SECONDS: float = 3600.0  # 1 hour cache TTL for static contract lists
    _FUTURES_TTL_SECONDS: float = 43200.0  # 12 hour cache TTL for static futures master
    _LAST_ATM_STRIKES: Dict[str, float] = {}  # underlying -> last known ATM strike

    @classmethod
    def fetch_raw_contracts(cls, underlying: str = "NIFTY", force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Retrieves complete option contract list for the underlying from Upstox.
        Uses cached data if available to protect the 64/min REST budget.
        """
        u_upper = underlying.upper().strip()
        instrument_key = UNDERLYING_INSTRUMENTS.get(u_upper)
        if not instrument_key:
            logger.error(f"[OptionDiscovery] Unsupported underlying: {underlying}")
            return []

        now = time.time()
        cached = cls._CONTRACTS_CACHE.get(u_upper)
        if not force_refresh and cached and (now - cached["timestamp"]) < cls._CACHE_TTL_SECONDS:
            return cached["contracts"]

        token = get_upstox_token()
        if not token:
            logger.warning("[OptionDiscovery] Upstox token not available for contract discovery.")
            return []

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }
        url = f"{UPSTOX_BASE_URL}/option/contract"
        params = {"instrument_key": instrument_key}

        def _sqlite_fallback() -> List[Dict[str, Any]]:
            try:
                import json as _json
                today_s = to_ist_naive().date().strftime("%Y-%m-%d")
                conn = OptionDB.get_connection()
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT instrument_key, trading_symbol, underlying, expiry, strike, option_type, lot_size, metadata_json
                    FROM option_sniffer_candidates
                    WHERE underlying = ? AND expiry >= ?
                    """,
                    (u_upper, today_s),
                )
                rows = cur.fetchall()
                conn.close()
                fallback_list = []
                for r in rows:
                    meta = {}
                    try:
                        meta = _json.loads(r[7] or "{}")
                    except Exception:
                        pass
                    fallback_list.append({
                        "instrument_key": r[0],
                        "trading_symbol": r[1],
                        "underlying": r[2],
                        "expiry": r[3],
                        "strike_price": float(r[4] or 0.0),
                        "instrument_type": r[5],
                        "lot_size": int(r[6] or 65),
                        "exchange_token": meta.get("exchange_token"),
                        "tick_size": meta.get("tick_size", 0.05),
                        "freeze_quantity": meta.get("freeze_quantity"),
                        "weekly": meta.get("weekly", True),
                    })
                if fallback_list:
                    logger.info(f"[OptionDiscovery] Loaded {len(fallback_list)} cached contracts for {u_upper} from SQLite catalog.")
                return fallback_list
            except Exception as fb_err:
                logger.debug(f"[OptionDiscovery] SQLite contract fallback note: {fb_err}")
                return []

        try:
            logger.info(f"[OptionDiscovery] Discovering option contracts for {u_upper} via {url}...")
            res = upstox_request("GET", url, headers=headers, params=params, timeout=8.0)
            if res.status_code != 200:
                logger.error(f"[OptionDiscovery] Upstox returned status {res.status_code}: {res.text[:200]}")
                return cached["contracts"] if cached else _sqlite_fallback()

            payload = res.json()
            raw_contracts = payload.get("data", [])
            logger.info(f"[OptionDiscovery] Successfully discovered {len(raw_contracts)} contracts for {u_upper}.")

            cls._CONTRACTS_CACHE[u_upper] = {
                "contracts": raw_contracts,
                "timestamp": now
            }
            return raw_contracts
        except Exception as e:
            logger.error(f"[OptionDiscovery] Failed to fetch contracts for {u_upper}: {e}")
            return cached["contracts"] if cached else _sqlite_fallback()

    @classmethod
    def get_available_expiries(
        cls,
        underlying: str = "NIFTY",
        force_refresh: bool = False,
        max_dte: Optional[float] = None
    ) -> List[str]:
        """Returns sorted list of future expiry dates (YYYY-MM-DD) for the underlying within max_dte."""
        contracts = cls.fetch_raw_contracts(underlying, force_refresh=force_refresh)
        today = to_ist_naive().date()
        today_str = today.strftime("%Y-%m-%d")
        limit_dte = MAX_OPTION_EXPIRY_DTE if max_dte is None else max_dte

        expiries: Set[str] = set()
        for c in contracts:
            exp = c.get("expiry")
            if exp and exp >= today_str:
                try:
                    exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                    dte = (exp_date - today).days
                    if limit_dte is None or dte <= limit_dte:
                        expiries.add(exp)
                except Exception:
                    pass

        sorted_expiries = sorted(list(expiries))
        return sorted_expiries

    @classmethod
    def calculate_atm_strike(cls, underlying: str, spot_price: float) -> float:
        """Calculates nearest ATM strike for the given spot price and underlying."""
        interval = STRIKE_INTERVALS.get(underlying.upper(), 50.0)
        return round(round(spot_price / interval) * interval, 2)

    @classmethod
    def discover_monitored_universe(
        cls,
        spot_prices: Optional[Dict[str, float]] = None,
        force_refresh: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Discovers and selects the active controlled ATM option universe:
          - Underlyings: NIFTY and BANKNIFTY
          - Expiries: Current nearest expiry (and next expiry if within 3 days)
          - Strikes: ATM +/- 5 strikes (11 strikes for CE, 11 strikes for PE per expiry)
        Returns list of standardized contract dicts and updates SQLite catalog.
        """
        spot_prices = spot_prices or {}
        monitored_contracts: List[Dict[str, Any]] = []

        for underlying in ("NIFTY", "BANKNIFTY"):
            # 1. Determine Spot Price (prioritize live stream cache & continuous index DB before REST/fallback)
            spot = spot_prices.get(underlying)
            if not spot:
                spot = spot_prices.get(f"{underlying}.NS")
            if not spot or spot <= 0:
                try:
                    from app.analytics.option_sniffer.option_stream_manager import OptionStreamManager
                    uq = OptionStreamManager().get_underlying_quote(underlying) or {}
                    if float(uq.get("spot_ltp") or 0.0) > 0:
                        spot = float(uq["spot_ltp"])
                except Exception:
                    pass
            if not spot or spot <= 0:
                try:
                    conn = OptionDB.get_connection()
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT close FROM continuous_index_ohlcv WHERE ticker = ? ORDER BY timestamp DESC LIMIT 1",
                        (underlying,),
                    )
                    row = cur.fetchone()
                    conn.close()
                    if row and float(row[0] or 0.0) > 0:
                        spot = float(row[0])
                except Exception:
                    pass
            if not spot or spot <= 0:
                # Query cached or live spot via REST
                try:
                    spot = fetch_upstox_ltp(underlying)
                except Exception:
                    spot = None
            if not spot or spot <= 0:
                last_atm = cls._LAST_ATM_STRIKES.get(underlying)
                if last_atm and last_atm > 0:
                    spot = float(last_atm)

            if not spot or spot <= 0:
                # Safe fallback to recent typical range only if all live/DB/cache sources are empty
                spot = 22500.0 if underlying == "NIFTY" else 48500.0
                logger.info(f"[OptionDiscovery] Using reference anchor spot ₹{spot} for {underlying} initialization.")

            # 2. Calculate ATM Strike & Record Anchor (even if DTE filter skips underlying)
            atm_strike = cls.calculate_atm_strike(underlying, spot)
            cls._LAST_ATM_STRIKES[underlying] = atm_strike

            # 3. Get Expiries (strictly capped at MAX_OPTION_EXPIRY_DTE)
            expiries = cls.get_available_expiries(underlying, force_refresh=force_refresh)
            if not expiries:
                logger.warning(
                    f"[OptionDiscovery] No valid expiries <= {MAX_OPTION_EXPIRY_DTE} DTE found for {underlying}. "
                    f"Skipping {underlying} surveillance to protect against illiquid/low-gamma far-month contracts."
                )
                continue

            current_expiry = expiries[0]
            selected_expiries = [current_expiry]
            # Include next expiry if current expiry is today or tomorrow (rollover awareness)
            try:
                curr_dt = datetime.strptime(current_expiry, "%Y-%m-%d").date()
                if (curr_dt - to_ist_naive().date()).days <= 1 and len(expiries) > 1:
                    selected_expiries.append(expiries[1])
            except Exception:
                pass

            # 4. Calculate Strike Window
            interval = STRIKE_INTERVALS.get(underlying, 50.0)

            min_strike = atm_strike - (ATM_STRIKE_WINDOW * interval)
            max_strike = atm_strike + (ATM_STRIKE_WINDOW * interval)

            logger.info(
                f"[OptionDiscovery] {underlying} Spot: ₹{spot:.2f} | ATM Strike: {atm_strike} | "
                f"Range: [{min_strike} to {max_strike}] | Expiries: {selected_expiries}"
            )

            # 4. Filter Contracts
            raw_contracts = cls.fetch_raw_contracts(underlying, force_refresh=force_refresh)
            matched_for_underlying = 0

            for c in raw_contracts:
                c_exp = c.get("expiry")
                c_strike = float(c.get("strike_price") or 0.0)
                c_type = (c.get("instrument_type") or "").upper()

                if c_exp in selected_expiries and min_strike <= c_strike <= max_strike and c_type in ("CE", "PE"):
                    ikey = c.get("instrument_key")
                    if not ikey:
                        continue

                    lot = int(c.get("lot_size") or DEFAULT_LOT_SIZES.get(underlying, 65))
                    contract_obj = {
                        "instrument_key": ikey,
                        "trading_symbol": c.get("trading_symbol", f"{underlying} {int(c_strike)} {c_type}"),
                        "underlying": underlying,
                        "expiry": c_exp,
                        "strike": c_strike,
                        "option_type": c_type,
                        "lot_size": lot,
                        "underlying_key": UNDERLYING_INSTRUMENTS.get(underlying),
                        "atm_strike": atm_strike,
                        "moneyness": round((spot - c_strike) / c_strike, 4) if c_type == "CE" else round((c_strike - spot) / c_strike, 4),
                        "is_active": True,
                        "metadata": {
                            "exchange_token": c.get("exchange_token"),
                            "tick_size": c.get("tick_size", 0.05),
                            "freeze_quantity": c.get("freeze_quantity"),
                            "weekly": c.get("weekly", True)
                        }
                    }

                    monitored_contracts.append(contract_obj)
                    OptionDB.save_candidate_contract(contract_obj)
                    matched_for_underlying += 1

            logger.info(f"[OptionDiscovery] Monitored {matched_for_underlying} active option contracts for {underlying}.")

        try:
            active_keys = [c["instrument_key"] for c in monitored_contracts]
            OptionDB.sync_active_candidates(active_keys)
            OptionDB.deactivate_expired_candidates()
        except Exception as e:
            logger.debug(f"[OptionDiscovery] Candidate catalog sync note: {e}")

        return monitored_contracts

    @classmethod
    def check_dynamic_atm_shift(cls, underlying: str, current_spot: float) -> bool:
        """
        Checks if the underlying has moved >= 1 strike interval away from the last configured ATM strike.
        If shifted, returns True indicating contracts should be updated.
        """
        last_atm = cls._LAST_ATM_STRIKES.get(underlying.upper())
        if not last_atm:
            return True
        interval = STRIKE_INTERVALS.get(underlying.upper(), 50.0)
        new_atm = cls.calculate_atm_strike(underlying, current_spot)
        return abs(new_atm - last_atm) >= interval

    @classmethod
    def find_option_contract(
        cls,
        underlying: str,
        expiry: Optional[str],
        strike: float,
        option_type: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Resolves any specific option strike (including far-OTM ±450pt catastrophe wings
        or ±200pt spread wings) from the cached Upstox `/v2/option/contract` catalog
        without consuming extra REST calls.
        """
        u_upper = str(underlying or "NIFTY").upper().strip()
        opt_t = str(option_type or "CE").upper().strip()
        raw_contracts = cls.fetch_raw_contracts(u_upper, force_refresh=False)
        if not raw_contracts:
            return None

        target_exp = str(expiry or "").strip()[:10]
        if not target_exp:
            exps = cls.get_available_expiries(u_upper, force_refresh=False)
            if not exps:
                return None
            target_exp = exps[0]

        best_match: Optional[Dict[str, Any]] = None
        best_dist = 1e9
        for c in raw_contracts:
            c_exp = str(c.get("expiry") or "")[:10]
            c_type = str(c.get("instrument_type") or "").upper()
            if c_exp != target_exp or c_type != opt_t:
                continue
            c_strike = float(c.get("strike_price") or 0.0)
            dist = abs(c_strike - float(strike))
            if dist < best_dist:
                best_dist = dist
                best_match = c

        max_tol = 100.0 if "BANK" in u_upper else 50.0
        if not best_match or best_dist > max_tol:
            return None

        ikey = best_match.get("instrument_key")
        if not ikey:
            return None
        c_strike = float(best_match.get("strike_price") or strike)
        lot = int(best_match.get("lot_size") or DEFAULT_LOT_SIZES.get(u_upper, 65))
        return {
            "instrument_key": ikey,
            "trading_symbol": best_match.get("trading_symbol", f"{u_upper} {int(c_strike)} {opt_t}"),
            "underlying": u_upper,
            "expiry": target_exp,
            "strike": c_strike,
            "option_type": opt_t,
            "lot_size": lot,
            "underlying_key": UNDERLYING_INSTRUMENTS.get(u_upper),
            "is_active": True,
            "is_wing": True,
        }

    @classmethod
    def discover_front_futures(
        cls,
        underlyings: Tuple[str, ...] = ("NIFTY", "BANKNIFTY"),
        force_refresh: bool = False,
        allow_network: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Resolves active Front-Month and Next-Month NSE_FO Futures (`FUT`) contracts for NIFTY and BANKNIFTY.
        Uses 12h RAM cache + SQLite `app_settings` persistence (`upstox_front_futures_cache_v1`) so live restarts
        take <1ms and 0 network calls, refreshing once from Upstox's public `NSE.json.gz` master when needed.
        """
        import gzip
        import json as _json

        now = time.time()
        today_str = to_ist_naive().date().strftime("%Y-%m-%d")

        cached_data = cls._FUTURES_CACHE.get("data") or {}
        if (
            not force_refresh
            and cached_data
            and cls._FUTURES_CACHE.get("session_date") == today_str
            and (now - float(cls._FUTURES_CACHE.get("timestamp") or 0.0)) < cls._FUTURES_TTL_SECONDS
        ):
            return cached_data

        def _is_valid_fut_map(fmap: Dict[str, Any]) -> bool:
            if not isinstance(fmap, dict) or not fmap:
                return False
            for u in underlyings:
                front = (fmap.get(u) or {}).get("front")
                if not isinstance(front, dict) or str(front.get("expiry") or "") < today_str:
                    return False
            return True

        # 1. Check SQLite `app_settings` cache first (fast & offline-safe)
        if not force_refresh:
            try:
                conn = OptionDB.get_connection()
                cur = conn.cursor()
                cur.execute("SELECT value FROM app_settings WHERE key = 'upstox_front_futures_cache_v1'")
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    parsed = _json.loads(row[0])
                    if _is_valid_fut_map(parsed):
                        cls._FUTURES_CACHE = {"data": parsed, "timestamp": now, "session_date": today_str}
                        return parsed
            except Exception:
                pass

        if not allow_network:
            return cached_data

        # 2. Fetch Upstox public NSE exchange master (`NSE.json.gz`) once per day
        try:
            import requests

            url = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
            resp = requests.get(url, timeout=6.0)
            if resp.status_code == 200 and resp.content:
                raw_items = _json.loads(gzip.decompress(resp.content).decode("utf-8"))
                by_u: Dict[str, List[Dict[str, Any]]] = {u.upper(): [] for u in underlyings}
                target_set = {u.upper() for u in underlyings}

                for item in raw_items:
                    if str(item.get("segment") or "") != "NSE_FO":
                        continue
                    itype = str(item.get("instrument_type") or "").upper()
                    if itype not in ("FUT", "FUTIDX"):
                        continue
                    usym = str(item.get("underlying_symbol") or item.get("name") or "").upper().strip()
                    if usym not in target_set:
                        continue
                    ikey = item.get("instrument_key")
                    if not ikey:
                        continue
                    raw_exp = item.get("expiry")
                    exp_s = ""
                    if isinstance(raw_exp, (int, float)) and raw_exp > 1e10:
                        # Upstox master stores expiry in IST epoch ms
                        exp_dt = datetime.utcfromtimestamp((float(raw_exp) / 1000.0) + 19800.0).date()
                        exp_s = exp_dt.strftime("%Y-%m-%d")
                    elif raw_exp:
                        exp_s = str(raw_exp)[:10]
                    if not exp_s or exp_s < today_str:
                        continue
                    by_u[usym].append(
                        {
                            "instrument_key": ikey,
                            "trading_symbol": item.get("trading_symbol") or f"{usym} {exp_s} FUT",
                            "underlying": usym,
                            "expiry": exp_s,
                            "lot_size": int(item.get("lot_size") or DEFAULT_LOT_SIZES.get(usym, 65)),
                            "is_futures": True,
                        }
                    )

                resolved: Dict[str, Dict[str, Any]] = {}
                for u_k, flist in by_u.items():
                    if not flist:
                        continue
                    flist.sort(key=lambda x: x["expiry"])
                    front_c = dict(flist[0])
                    front_c["futures_slot"] = "FRONT"
                    next_c = dict(flist[1]) if len(flist) > 1 else dict(flist[0])
                    next_c["futures_slot"] = "NEXT"
                    resolved[u_k] = {"front": front_c, "next": next_c}

                if resolved:
                    cls._FUTURES_CACHE = {"data": resolved, "timestamp": now, "session_date": today_str}
                    try:
                        conn = OptionDB.get_connection()
                        with conn:
                            conn.execute(
                                "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)"
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('upstox_front_futures_cache_v1', ?)",
                                (_json.dumps(resolved),),
                            )
                        conn.close()
                    except Exception:
                        pass
                    logger.info(
                        f"[OptionDiscovery] Resolved Front-Month Futures: "
                        f"{ {k: v['front']['trading_symbol'] + ' (' + v['front']['instrument_key'] + ')' for k, v in resolved.items()} }"
                    )
                    return resolved
        except Exception as fe:
            logger.debug(f"[OptionDiscovery] Front-Month Futures discovery fallback note: {fe}")

        return cached_data
