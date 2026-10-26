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
)
from app.analytics.option_sniffer.option_db import OptionDB

logger = logging.getLogger(__name__)


class OptionContractDiscovery:
    """
    Manages discovery, filtering, and dynamic updating of monitored option contracts
    for NIFTY and BANKNIFTY.
    """

    _CONTRACTS_CACHE: Dict[str, Dict[str, Any]] = {}  # underlying -> {data, timestamp}
    _CACHE_TTL_SECONDS: float = 3600.0  # 1 hour cache TTL for static contract lists
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

        try:
            logger.info(f"[OptionDiscovery] Discovering option contracts for {u_upper} via {url}...")
            res = upstox_request("GET", url, headers=headers, params=params, timeout=8.0)
            if res.status_code != 200:
                logger.error(f"[OptionDiscovery] Upstox returned status {res.status_code}: {res.text[:200]}")
                return cached["contracts"] if cached else []

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
            return cached["contracts"] if cached else []

    @classmethod
    def get_available_expiries(cls, underlying: str = "NIFTY", force_refresh: bool = False) -> List[str]:
        """Returns sorted list of future expiry dates (YYYY-MM-DD) for the underlying."""
        contracts = cls.fetch_raw_contracts(underlying, force_refresh=force_refresh)
        today_str = datetime.now().strftime("%Y-%m-%d")

        expiries: Set[str] = set()
        for c in contracts:
            exp = c.get("expiry")
            if exp and exp >= today_str:
                expiries.add(exp)

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
            # 1. Determine Spot Price
            spot = spot_prices.get(underlying)
            if not spot:
                spot = spot_prices.get(f"{underlying}.NS")
            if not spot:
                # Query cached or live spot
                try:
                    spot = fetch_upstox_ltp(underlying)
                except Exception:
                    spot = None

            if not spot or spot <= 0:
                # Safe fallback to recent typical range if spot is temporarily unavailable during closed session
                spot = 22500.0 if underlying == "NIFTY" else 48500.0
                logger.info(f"[OptionDiscovery] Using reference anchor spot ₹{spot} for {underlying} initialization.")

            # 2. Get Expiries
            expiries = cls.get_available_expiries(underlying, force_refresh=force_refresh)
            if not expiries:
                logger.warning(f"[OptionDiscovery] No valid expiries found for {underlying}.")
                continue

            current_expiry = expiries[0]
            selected_expiries = [current_expiry]
            # Include next expiry if current expiry is today or tomorrow (rollover awareness)
            try:
                curr_dt = datetime.strptime(current_expiry, "%Y-%m-%d").date()
                if (curr_dt - date.today()).days <= 1 and len(expiries) > 1:
                    selected_expiries.append(expiries[1])
            except Exception:
                pass

            # 3. Calculate ATM and Strike Window
            atm_strike = cls.calculate_atm_strike(underlying, spot)
            cls._LAST_ATM_STRIKES[underlying] = atm_strike
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
            OptionDB.deactivate_expired_candidates()
        except Exception:
            pass

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
