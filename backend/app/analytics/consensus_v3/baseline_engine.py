"""
Layer 1 — Baseline Engine (Universe Eligibility, Published Momentum, Turnover Controls & Telemetry)
===================================================================================================
Responsibilities:
  1. Historical / point-in-time eligible liquid universe (`fno_universe_cache` NSE F&O-181 Cash Equities,
     `price >= ₹50`, `15d` median daily turnover `>= ₹10 Cr`, `ADR15 >= 1.2%`).
  2. Resolves exact Upstox Cash Equity `NSE_EQ|<ISIN>` instrument keys for every symbol.
  3. Book 1 (`SWING_20D_CORE`): Published `12-1` Sharpe-Momentum (`close[t-20]/close[t-252] - 1` / `vol60`),
     scheduled `20`-trading-day rebalance, and explicit `Top-8 / Rank-78%` hysteresis turnover control.
  4. Book 2 (`INTRADAY_15M_SNIFFER`): Intraday `ORB30` Momentum benchmark & `PDH`/`PDL` failed-sweep rule
     (`09:30..12:30 IST`, `max 4` slots, `max 6` fills/day) with full preserved telemetry (`RSI`, `ADX`, `ATR`, `RVOL`, `VWAP`).
"""

import json
import math
import os
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from app.analytics.consensus_v3 import resolve_v3_db_path as get_db_path

_INSTRUMENT_KEY_CACHE: Optional[Dict[str, str]] = None

# Canonical corporate-rename aliases & direct ISIN fallbacks so 181/181 NSE F&O symbols resolve valid NSE_EQ keys
NSE_FNO_SYMBOL_ALIASES: Dict[str, str] = {
    "GMRINFRA": "GMRAIRPORT",
    "GUJGASLTD": "GUJENERGY",
    "L&TFH": "LTF",
    "MCDOWELL-N": "UNITDSPR",
    "PEL": "PIRAMALFIN",
}

NSE_FNO_ISIN_FALLBACKS: Dict[str, str] = {
    "GMRINFRA": "NSE_EQ|INE776C01039",
    "GUJGASLTD": "NSE_EQ|INE844O01030",
    "L&TFH": "NSE_EQ|INE498L01015",
    "MCDOWELL-N": "NSE_EQ|INE854D01024",
    "PEL": "NSE_EQ|INE202B01038",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "LTIM": "NSE_EQ|INE214T01019",
}

FNO_SECTOR_MAP: Dict[str, str] = {
    # Pharma & Healthcare
    "LAURUSLABS": "PHARMA_HEALTH", "DIVISLAB": "PHARMA_HEALTH", "AUROPHARMA": "PHARMA_HEALTH",
    "SUNPHARMA": "PHARMA_HEALTH", "CIPLA": "PHARMA_HEALTH", "DRREDDY": "PHARMA_HEALTH",
    "LUPIN": "PHARMA_HEALTH", "BIOCON": "PHARMA_HEALTH", "TORNTPHARM": "PHARMA_HEALTH",
    "ALKEM": "PHARMA_HEALTH", "GLENMARK": "PHARMA_HEALTH", "GRANULES": "PHARMA_HEALTH",
    "ZYDUSLIFE": "PHARMA_HEALTH", "ABBOTINDIA": "PHARMA_HEALTH", "APOLLOHOSP": "PHARMA_HEALTH",
    "IPCALAB": "PHARMA_HEALTH", "SYNGENE": "PHARMA_HEALTH", "LALPATHLAB": "PHARMA_HEALTH",
    "METROPOLIS": "PHARMA_HEALTH",
    # Banking
    "HDFCBANK": "BANKING", "ICICIBANK": "BANKING", "SBIN": "BANKING", "KOTAKBANK": "BANKING",
    "AXISBANK": "BANKING", "INDUSINDBK": "BANKING", "BANKBARODA": "BANKING", "PNB": "BANKING",
    "CANBK": "BANKING", "FEDERALBNK": "BANKING", "IDFCFIRSTB": "BANKING", "AUBANK": "BANKING",
    "BANDHANBNK": "BANKING", "RBLBANK": "BANKING", "CUB": "BANKING",
    # Financials, NBFC & Exchanges
    "MCX": "FINANCIALS", "BAJFINANCE": "FINANCIALS", "BAJAJFINSV": "FINANCIALS",
    "CHOLAFIN": "FINANCIALS", "SHRIRAMFIN": "FINANCIALS", "MUTHOOTFIN": "FINANCIALS",
    "MANAPPURAM": "FINANCIALS", "LICHSGFIN": "FINANCIALS", "PFC": "FINANCIALS",
    "RECLTD": "FINANCIALS", "SBILIFE": "FINANCIALS", "HDFCLIFE": "FINANCIALS",
    "ICICIPRULI": "FINANCIALS", "ICICIGI": "FINANCIALS", "SBICARD": "FINANCIALS",
    "HDFCAMC": "FINANCIALS", "ABCAPITAL": "FINANCIALS", "L&TFH": "FINANCIALS",
    "M&MFIN": "FINANCIALS", "PEL": "FINANCIALS", "CANFINHOME": "FINANCIALS", "IEX": "FINANCIALS",
    # IT & Technology
    "TCS": "IT_TECH", "INFY": "IT_TECH", "HCLTECH": "IT_TECH", "WIPRO": "IT_TECH",
    "TECHM": "IT_TECH", "LTIM": "IT_TECH", "PERSISTENT": "IT_TECH", "COFORGE": "IT_TECH",
    "MPHASIS": "IT_TECH", "LTTS": "IT_TECH", "OFSS": "IT_TECH", "BSOFT": "IT_TECH",
    "NAUKRI": "IT_TECH", "DIXON": "IT_TECH", "INDIAMART": "IT_TECH",
    # Auto & Auto Ancillaries
    "MARUTI": "AUTO", "TATAMOTORS": "AUTO", "M&M": "AUTO", "BAJAJ-AUTO": "AUTO",
    "EICHERMOT": "AUTO", "HEROMOTOCO": "AUTO", "TVSMOTOR": "AUTO", "ASHOKLEY": "AUTO",
    "MOTHERSON": "AUTO", "BOSCHLTD": "AUTO", "BHARATFORG": "AUTO", "BALKRISIND": "AUTO",
    "MRF": "AUTO", "APOLLOTYRE": "AUTO", "EXIDEIND": "AUTO", "ESCORTS": "AUTO",
    # Metals & Mining
    "TATASTEEL": "METALS_MINING", "JSWSTEEL": "METALS_MINING", "HINDALCO": "METALS_MINING",
    "VEDL": "METALS_MINING", "JINDALSTEL": "METALS_MINING", "SAIL": "METALS_MINING",
    "NMDC": "METALS_MINING", "NATIONALUM": "METALS_MINING", "HINDCOPPER": "METALS_MINING",
    "COALINDIA": "METALS_MINING",
    # Capital Goods, Defence & Infra
    "LT": "CAPITAL_GOODS", "HAL": "CAPITAL_GOODS", "BEL": "CAPITAL_GOODS", "BHEL": "CAPITAL_GOODS",
    "SIEMENS": "CAPITAL_GOODS", "ABB": "CAPITAL_GOODS", "CUMMINSIND": "CAPITAL_GOODS",
    "POLYCAB": "CAPITAL_GOODS", "HAVELLS": "CAPITAL_GOODS", "ASTRAL": "CAPITAL_GOODS",
    "CONCOR": "CAPITAL_GOODS", "ADANIPORTS": "CAPITAL_GOODS", "GMRINFRA": "CAPITAL_GOODS",
    "IRCTC": "CAPITAL_GOODS", "INDIGO": "CAPITAL_GOODS", "VOLTAS": "CAPITAL_GOODS",
    # Energy, Oil & Power
    "RELIANCE": "ENERGY_POWER", "ONGC": "ENERGY_POWER", "NTPC": "ENERGY_POWER",
    "POWERGRID": "ENERGY_POWER", "TATAPOWER": "ENERGY_POWER", "BPCL": "ENERGY_POWER",
    "IOC": "ENERGY_POWER", "HINDPETRO": "ENERGY_POWER", "GAIL": "ENERGY_POWER",
    "IGL": "ENERGY_POWER", "MGL": "ENERGY_POWER", "GUJGASLTD": "ENERGY_POWER",
    "PETRONET": "ENERGY_POWER", "TORNTPHARM": "PHARMA_HEALTH",
    # Chemicals & Agri
    "NAVINFLUOR": "CHEMICALS_AGRI", "PIIND": "CHEMICALS_AGRI", "SRF": "CHEMICALS_AGRI",
    "UPL": "CHEMICALS_AGRI", "AARTIIND": "CHEMICALS_AGRI", "DEEPAKNTR": "CHEMICALS_AGRI",
    "TATACHEM": "CHEMICALS_AGRI", "ATUL": "CHEMICALS_AGRI", "COROMANDEL": "CHEMICALS_AGRI",
    "CHAMBLFERT": "CHEMICALS_AGRI", "GNFC": "CHEMICALS_AGRI", "BALRAMCHIN": "CHEMICALS_AGRI",
    # FMCG, Retail & Consumer
    "ITC": "FMCG_CONSUMER", "HINDUNILVR": "FMCG_CONSUMER", "NESTLEIND": "FMCG_CONSUMER",
    "BRITANNIA": "FMCG_CONSUMER", "TATACONSUM": "FMCG_CONSUMER", "DABUR": "FMCG_CONSUMER",
    "MARICO": "FMCG_CONSUMER", "GODREJCP": "FMCG_CONSUMER", "COLPAL": "FMCG_CONSUMER",
    "TITAN": "FMCG_CONSUMER", "TRENT": "FMCG_CONSUMER", "ASIANPAINT": "FMCG_CONSUMER",
    "BERGEPAINT": "FMCG_CONSUMER", "PIDILITIND": "FMCG_CONSUMER", "PAGEIND": "FMCG_CONSUMER",
    "ABFRL": "FMCG_CONSUMER", "BATAINDIA": "FMCG_CONSUMER", "JUBLFOOD": "FMCG_CONSUMER",
    "UBL": "FMCG_CONSUMER", "MCDOWELL-N": "FMCG_CONSUMER", "INDHOTEL": "FMCG_CONSUMER",
    # Cement & Realty
    "ULTRACEMCO": "CEMENT_REALTY", "GRASIM": "CEMENT_REALTY", "AMBUJACEM": "CEMENT_REALTY",
    "ACC": "CEMENT_REALTY", "SHREECEM": "CEMENT_REALTY", "DALBHARAT": "CEMENT_REALTY",
    "JKCEMENT": "CEMENT_REALTY", "RAMCOCEM": "CEMENT_REALTY", "DLF": "CEMENT_REALTY",
    "GODREJPROP": "CEMENT_REALTY", "OBEROIRLTY": "CEMENT_REALTY",
    # Telecom & Media
    "BHARTIARTL": "TELECOM_MEDIA", "INDUSTOWER": "TELECOM_MEDIA", "IDEA": "TELECOM_MEDIA",
    "TATACOMM": "TELECOM_MEDIA", "SUNTV": "TELECOM_MEDIA", "ZEEL": "TELECOM_MEDIA",
    "PVRINOX": "TELECOM_MEDIA",
}


def resolve_fno_sector(symbol: str) -> str:
    bare = str(symbol or "").replace(".NS", "").replace(".BO", "").strip().upper()
    return FNO_SECTOR_MAP.get(bare, "DIVERSIFIED_FNO")


def load_nse_eq_instrument_map() -> Dict[str, str]:
    """Loads `SYMBOL -> NSE_EQ|<ISIN>` map from local `upstox_nse_instruments.json` + `NSE_INSTRUMENT_MAP` + corporate-rename aliases."""
    global _INSTRUMENT_KEY_CACHE
    if _INSTRUMENT_KEY_CACHE is not None:
        return _INSTRUMENT_KEY_CACHE

    mapping: Dict[str, str] = dict(NSE_FNO_ISIN_FALLBACKS)
    json_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "upstox_nse_instruments.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, str) and v.startswith("NSE_EQ|"):
                        mapping[k.replace(".NS", "").strip().upper()] = v.strip()
        except Exception:
            pass

    try:
        from app.data.upstox_provider import NSE_INSTRUMENT_MAP
        for k, v in NSE_INSTRUMENT_MAP.items():
            if isinstance(v, str) and v.startswith("NSE_EQ|"):
                mapping[k.replace(".NS", "").strip().upper()] = v.strip()
    except Exception:
        pass

    for old_sym, new_sym in NSE_FNO_SYMBOL_ALIASES.items():
        if old_sym not in mapping and new_sym in mapping:
            mapping[old_sym] = mapping[new_sym]

    _INSTRUMENT_KEY_CACHE = mapping
    return mapping


def load_fno_eligible_set(conn: sqlite3.Connection) -> Set[str]:
    """Returns canonical `.NS` ticker set for the 181 NSE F&O-eligible cash equities."""
    rows = conn.execute("SELECT symbol FROM fno_universe_cache").fetchall()
    out: Set[str] = set()
    for (sym,) in rows:
        if not sym:
            continue
        s = str(sym).strip().upper()
        out.add(s if s.endswith((".NS", ".BO")) else f"{s}.NS")
    return out


def compute_rsi_14(closes: List[float]) -> float:
    if len(closes) < 15:
        return 50.0
    gains, losses = 0.0, 0.0
    for j in range(len(closes) - 14, len(closes)):
        diff = closes[j] - closes[j - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses <= 1e-9:
        return 95.0 if gains > 0 else 50.0
    rs = gains / losses
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def compute_adx_proxy_14(bars_ohlc: List[Tuple[float, float, float]]) -> float:
    """Fast causal 14-bar ADX directional strength calculation from `(high, low, close)` tuples."""
    if len(bars_ohlc) < 15:
        return 20.0
    window = bars_ohlc[-15:]
    tr_sum, pdm_sum, mdm_sum = 0.0, 0.0, 0.0
    for i in range(1, len(window)):
        h, l, _ = window[i]
        ph, pl, pc = window[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up = h - ph
        dn = pl - l
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        tr_sum += tr
        pdm_sum += pdm
        mdm_sum += mdm
    if tr_sum <= 1e-9:
        return 15.0
    pdi = 100.0 * pdm_sum / tr_sum
    mdi = 100.0 * mdm_sum / tr_sum
    denom = pdi + mdi
    if denom <= 1e-9:
        return 15.0
    dx = 100.0 * abs(pdi - mdi) / denom
    return round(min(75.0, max(10.0, dx)), 1)


class BaselineEngine:
    """
    Causal point-in-time data & baseline signal builder enforcing `bar_timestamp <= as_of_ts`.
    Zero forward-looking operations; zero full-session normalization.
    """

    @staticmethod
    def build_book1_20d_snapshot(
        conn: sqlite3.Connection,
        as_of_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Builds strictly point-in-time (`<= as_of_date`) daily snapshot for Book 1 (`20d` Scheduled Rebalance):
          - Stitches `1d` bars (`< 2026-09-24`) with completed `15m -> 1d` sessions (`>= 2026-09-24` and `<= as_of_date`).
          - Guards corporate-action / split jumps (`> +40%` or `< -35%` overnight).
          - Computes Published `12-1` Momentum, `6-1` Momentum, `Sharpe-12-1`, `52w-High` proximity, `20d` pullback,
            and market-level `Breadth_SMA50` + `Index_vs_SMA50` regime shield state!
        """
        fno_set = load_fno_eligible_set(conn)
        ikey_map = load_nse_eq_instrument_map()

        date_clause_1d = "AND date <= ?" if as_of_date else ""
        params_1d: Tuple = (f"{as_of_date} 23:59:59",) if as_of_date else ()

        raw_1d: Dict[str, Dict[str, Tuple[float, float, float, float, float]]] = defaultdict(dict)
        q1 = f"""
            SELECT ticker, date, open, high, low, close, volume
            FROM ohlcv
            WHERE timeframe='1d' AND date >= '2023-01-01' AND date < '2026-09-24' {date_clause_1d}
            ORDER BY ticker, date ASC
        """
        for sym, dt_s, bo, bh, bl, bc, bv in conn.execute(q1, params_1d):
            if sym in fno_set and float(bo) > 0 and float(bc) > 0 and float(bh) >= float(bl):
                raw_1d[sym][str(dt_s)[:10]] = (float(bo), float(bh), float(bl), float(bc), float(bv or 0.0))

        # Stitch completed 15m sessions >= 2026-09-24 strictly <= as_of_date
        if as_of_date is None or as_of_date >= "2026-09-24":
            now_ist = datetime.now()
            today_live_str = now_ist.strftime("%Y-%m-%d")
            hm_live = now_ist.strftime("%H:%M")
            q15 = f"""
                SELECT ticker, date, open, high, low, close, volume
                FROM ohlcv
                WHERE timeframe='15m' AND date >= '2026-09-24' {date_clause_1d}
                ORDER BY ticker, date ASC
            """
            by_td: Dict[Tuple[str, str], List[Tuple[float, float, float, float, float]]] = defaultdict(list)
            for sym, dt_s, bo, bh, bl, bc, bv in conn.execute(q15, params_1d):
                if sym in fno_set and float(bo) > 0 and float(bc) > 0 and float(bh) >= float(bl):
                    by_td[(sym, str(dt_s)[:10])].append((float(bo), float(bh), float(bl), float(bc), float(bv or 0.0)))
            for (sym, d_s), bls in by_td.items():
                # Never stitch an active intraday session before 15:30 IST market close into 1d bars
                if d_s == today_live_str and hm_live < "15:30":
                    continue
                min_bars_req = 24 if d_s == today_live_str else 15
                if len(bls) >= min_bars_req:
                    raw_1d[sym][d_s] = (
                        bls[0][0],
                        max(x[1] for x in bls),
                        min(x[2] for x in bls),
                        bls[-1][3],
                        sum(x[4] for x in bls),
                    )

        day_counts: Dict[str, int] = defaultdict(int)
        for sym, dm in raw_1d.items():
            for d_s in dm:
                day_counts[d_s] += 1
        valid_days = sorted(d for d, c in day_counts.items() if c >= 80)
        if not valid_days:
            return {"as_of_date": as_of_date, "candidates": [], "macro_shield": {"regime": "UNKNOWN", "target_slots": 0}}

        eval_date = valid_days[-1]
        d2i = {d: i for i, d in enumerate(valid_days)}
        ew_rets_by_di: Dict[int, List[float]] = defaultdict(list)
        b50_latest: List[float] = []
        candidates: List[Dict[str, Any]] = []

        for sym, dm in raw_1d.items():
            sl = []
            seg = 0
            for d_s in sorted(dm.keys()):
                if d_s not in d2i or d_s > eval_date:
                    continue
                bo, bh, bl, bc, bv = dm[d_s]
                isp = bool(sl and (bo / sl[-1]["c"] > 1.40 or bo / sl[-1]["c"] < 0.65 or bc / sl[-1]["c"] > 1.45 or bc / sl[-1]["c"] < 0.60))
                if isp:
                    seg = len(sl)
                if sl and not isp:
                    ew_rets_by_di[d2i[d_s]].append((bc - sl[-1]["c"]) / sl[-1]["c"] * 100.0)
                sl.append({"d": d_s, "di": d2i[d_s], "o": bo, "h": bh, "l": bl, "c": bc, "turn": bc * bv, "sp": isp, "segl": len(sl) - seg})

            if not sl or sl[-1]["d"] != eval_date or sl[-1]["segl"] < 255 or len(sl) < 255:
                continue

            r = sl[-1]
            c_now = r["c"]
            turns_60 = sorted(sl[j]["turn"] for j in range(-60, 0))
            med_turn_60 = turns_60[30]
            if c_now < 50.0 or med_turn_60 < 1e8:
                continue

            closes_200 = [sl[j]["c"] for j in range(-200, 0)]
            sma50 = sum(closes_200[-50:]) / 50.0
            sma200 = sum(closes_200) / 200.0
            b50_latest.append(1.0 if c_now > sma50 else 0.0)

            mom_12_1 = (sl[-21]["c"] / sl[-253]["c"] - 1.0) * 100.0
            mom_6_1 = (sl[-21]["c"] / sl[-127]["c"] - 1.0) * 100.0
            ret_20d = (c_now / sl[-21]["c"] - 1.0) * 100.0
            d60 = [(sl[j]["c"] / sl[j - 1]["c"] - 1.0) for j in range(-60, 0) if not sl[j]["sp"]]
            vol60 = (statistics.stdev(d60) * math.sqrt(252.0) * 100.0) if len(d60) >= 10 else 30.0
            sharpe_12_1 = mom_12_1 / max(5.0, vol60)
            hi252 = max(sl[j]["h"] for j in range(-252, 0))
            near_52w = c_now / max(1e-6, hi252)

            atr14 = sum(
                max(sl[j]["h"] - sl[j]["l"], abs(sl[j]["h"] - sl[j - 1]["c"]), abs(sl[j]["l"] - sl[j - 1]["c"]))
                for j in range(-14, 0)
            ) / 14.0
            rsi14 = compute_rsi_14([sl[j]["c"] for j in range(-20, 0)])
            adx14 = compute_adx_proxy_14([(sl[j]["h"], sl[j]["l"], sl[j]["c"]) for j in range(-16, 0)])
            ret_1d_pct = ((c_now / sl[-2]["c"]) - 1.0) * 100.0 if (len(sl) >= 2 and not sl[-1]["sp"]) else 0.0
            avg_turn_20 = sum(sl[j]["turn"] for j in range(-21, -1)) / 20.0
            rvol_20d = r["turn"] / max(1.0, avg_turn_20)
            bare = sym.replace(".NS", "").replace(".BO", "")

            candidates.append({
                "ticker": sym,
                "symbol": bare,
                "sector": resolve_fno_sector(bare),
                "instrument_key": ikey_map.get(bare, ""),
                "fno_eligible": True,
                "as_of_date": eval_date,
                "close": round(c_now, 2),
                "sma50": round(sma50, 2),
                "sma200": round(sma200, 2),
                "ret_1d_pct": round(ret_1d_pct, 2),
                "rvol_20d": round(rvol_20d, 2),
                "med_turnover_60d_inr": round(med_turn_60, 0),
                "mom_12_1_pct": round(mom_12_1, 2),
                "mom_6_1_pct": round(mom_6_1, 2),
                "sharpe_12_1": round(sharpe_12_1, 4),
                "near_52w_ratio": round(near_52w, 4),
                "pullback_20d_pct": round(-ret_20d, 2),
                "vol60_ann_pct": round(vol60, 2),
                "c_over_sma200_pct": round((c_now / sma200 - 1.0) * 100.0, 2),
                "sma_alignment_ok": bool(c_now > sma50 and sma50 > sma200),
                "atr14_px": round(atr14, 2),
                "atr_pct": round(atr14 / c_now * 100.0, 2),
                "rsi": rsi14,
                "adx": adx14,
            })

        # Compute causal Equal-Weight Market Index vs SMA50 + Breadth50
        midx = [100.0]
        for di in range(1, len(valid_days)):
            rs = ew_rets_by_di.get(di, [])
            r_m = (sum(rs) / len(rs)) if rs else 0.0
            midx.append(midx[-1] * (1.0 + r_m / 100.0))
        mkt_now = midx[-1]
        mkt_sma50 = sum(midx[-50:]) / min(50, len(midx))
        breadth_50 = (sum(b50_latest) / len(b50_latest)) if b50_latest else 0.50

        if mkt_now < mkt_sma50 and breadth_50 < 0.25:
            regime_label, target_slots = "BEAR_CASH_SHIELD", 0
        elif mkt_now < mkt_sma50 and breadth_50 < 0.40:
            regime_label, target_slots = "CAUTION_HALF_SHIELD", 4
        else:
            regime_label, target_slots = "BULL_FULL_DEPLOYMENT", 8

        # Published 12-1 Sharpe-Momentum percentile rank
        sh_sorted = sorted(c["sharpe_12_1"] for c in candidates)
        n_c = max(1, len(sh_sorted))
        for c in candidates:
            c["baseline_mom_pct"] = round(sum(1 for v in sh_sorted if v <= c["sharpe_12_1"]) / n_c, 4)

        return {
            "as_of_date": eval_date,
            "total_eligible": len(candidates),
            "macro_shield": {
                "regime": regime_label,
                "target_slots": target_slots,
                "max_slots": 8,
                "mkt_above_sma50": bool(mkt_now >= mkt_sma50),
                "breadth_above_sma50_pct": round(breadth_50 * 100.0, 1),
            },
            "candidates": candidates,
        }

    @staticmethod
    def build_book2_15m_snapshot(
        conn: sqlite3.Connection,
        as_of_ts: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Builds strictly point-in-time (`<= as_of_ts`) 15m cross-sectional F&O-181 snapshot for Book 2 Sniffer:
          - Loads prior ~22 trading days of 15m bars + prior 15d daily closes strictly `< session_date`.
          - Evaluates the latest completed 15m bar `<= as_of_ts` across all eligible F&O-181 stocks.
          - Computes `PDH`, `PDL`, `PDC`, `ADR15`, `TOD_RVOL`, `VWAP`, `CLV`, `ORB30`, `RSI`, `ADX`,
            market breadth/return, and flags both `PDH_SWEEP` / `PDL_SWEEP` and `ORB30_MOMENTUM` baselines!
        """
        fno_set = load_fno_eligible_set(conn)
        ikey_map = load_nse_eq_instrument_map()

        # Find recent 25 session dates <= as_of_ts
        ts_clause = "AND date <= ?" if as_of_ts else ""
        ts_params: Tuple = (as_of_ts,) if as_of_ts else ()
        recent_dates_rows = conn.execute(
            f"SELECT DISTINCT substr(date,1,10) as d FROM ohlcv WHERE timeframe='15m' {ts_clause} ORDER BY d DESC LIMIT 25",
            ts_params,
        ).fetchall()
        if len(recent_dates_rows) < 12:
            return {"as_of_ts": as_of_ts, "bar_hm": None, "items": [], "market_context": {}}

        min_date = recent_dates_rows[-1][0]
        q15 = f"""
            SELECT ticker, date, open, high, low, close, volume
            FROM ohlcv
            WHERE timeframe='15m' AND date >= ? {ts_clause}
            ORDER BY ticker, date ASC
        """
        q_params: Tuple = (f"{min_date} 00:00:00", as_of_ts) if as_of_ts else (f"{min_date} 00:00:00",)

        raw_by_t: Dict[str, List[Tuple[str, str, str, float, float, float, float, float]]] = defaultdict(list)
        for sym, dt_s, bo, bh, bl, bc, bv in conn.execute(q15, q_params):
            if sym not in fno_set:
                continue
            bo, bh, bl, bc, bv = float(bo), float(bh), float(bl), float(bc), float(bv or 0.0)
            if (bo == 100.0 and bh == 102.0 and bl == 99.0 and bc == 101.5 and bv == 50000.0) or bo <= 0 or bc <= 0 or bh < bl:
                continue
            s = str(dt_s)[:19]
            raw_by_t[sym].append((s, s[:10], s[11:16], bo, bh, bl, bc, bv))

        bars_by_td: Dict[Tuple[str, str], List[Tuple[str, str, float, float, float, float, float]]] = {}
        day_tc: Dict[str, int] = defaultdict(int)
        for sym, rows in raw_by_t.items():
            prev_c = None
            by_d: Dict[str, List[Tuple[str, str, float, float, float, float, float]]] = defaultdict(list)
            for s, d_s, hm, bo, bh, bl, bc, bv in rows:
                if prev_c is not None:
                    lim = 0.25 if not by_d[d_s] else 0.18
                    if not ((1.0 - lim) * prev_c <= bo <= (1.0 + lim) * prev_c and (1.0 - lim) * prev_c <= bc <= (1.0 + lim) * prev_c):
                        continue
                prev_c = bc
                by_d[d_s].append((s, hm, bo, bh, bl, bc, bv))
            for d_s, blist in by_d.items():
                bars_by_td[(sym, d_s)] = blist
                day_tc[d_s] += 1

        valid_days = sorted(d for d, c in day_tc.items() if c >= 60)
        if len(valid_days) < 11:
            return {"as_of_ts": as_of_ts, "bar_hm": None, "items": [], "market_context": {}}

        session_date = valid_days[-1]
        if as_of_ts is None:
            now_live = datetime.now()
            hm_live = now_live.strftime("%H:%M")
            today_live = now_live.strftime("%Y-%m-%d")
            # Fail-closed during active weekday market hours (09:15–15:30 IST) if today's 15m bars haven't reached >=60 F&O stocks yet
            if now_live.weekday() < 5 and "09:15" <= hm_live <= "15:30" and session_date != today_live:
                return {
                    "as_of_ts": None,
                    "session_date": today_live,
                    "bar_hm": None,
                    "is_sniffer_window": False,
                    "items": [],
                    "market_context": {"regime": "LIVE_FEED_WARMUP_FAIL_CLOSED", "fno_mkt_return_pct": 0.0, "fno_advance_ratio": 0.50},
                }

        prior_days = [d for d in valid_days[:-1] if sum(1 for s in fno_set if len(bars_by_td.get((s, d), [])) >= 15) >= 60][-15:]
        if len(prior_days) < 8:
            prior_days = valid_days[:-1][-15:]

        # Determine latest common bar timestamp `hm` on `session_date` with >= 30 stocks
        hm_counts: Dict[str, int] = defaultdict(int)
        for sym in fno_set:
            for b in bars_by_td.get((sym, session_date), []):
                hm_counts[b[1]] += 1
        valid_hms = sorted(hm for hm, cnt in hm_counts.items() if cnt >= 30)
        if not valid_hms:
            return {"as_of_ts": as_of_ts, "bar_hm": None, "items": [], "market_context": {}}
        target_hm = valid_hms[-1]

        # Compute point-in-time market return & breadth at `target_hm`
        mkt_rets: List[float] = []
        raw_items: List[Dict[str, Any]] = []

        for sym in sorted(fno_set):
            blist_today = [b for b in bars_by_td.get((sym, session_date), []) if b[1] <= target_hm]
            if not blist_today or blist_today[-1][1] != target_hm:
                continue

            hist_stats = []
            closes_daily = []
            for pd_s in prior_days:
                pbars = bars_by_td.get((sym, pd_s), [])
                if len(pbars) < 10:
                    continue
                po = pbars[0][2]
                ph = max(x[3] for x in pbars)
                pl = min(x[4] for x in pbars)
                pc = pbars[-1][5]
                closes_daily.append(pc)
                cv_p, tt_p = 0.0, 0.0
                cvm_p, btm_p = {}, {}
                for pb in pbars:
                    cv_p += pb[6]
                    bt_p = pb[5] * pb[6]
                    tt_p += bt_p
                    cvm_p[pb[1]] = cv_p
                    btm_p[pb[1]] = bt_p
                hist_stats.append({
                    "high": ph, "low": pl, "close": pc, "rng_pct": (ph - pl) / po * 100.0,
                    "turnover": tt_p, "cvm": cvm_p, "btm": btm_p,
                })

            if len(hist_stats) < 6:
                continue

            med_turn = statistics.median(x["turnover"] for x in hist_stats)
            pdh, pdl, pdc = hist_stats[-1]["high"], hist_stats[-1]["low"], hist_stats[-1]["close"]
            adr = sum(x["rng_pct"] for x in hist_stats) / len(hist_stats)
            if med_turn < 1e8 or adr < 1.2 or pdc < 50.0:
                continue

            r3 = ((pdc / closes_daily[-4] - 1.0) * 100.0) if len(closes_daily) >= 4 else 0.0
            r10 = ((pdc / closes_daily[-11] - 1.0) * 100.0) if len(closes_daily) >= 11 else r3

            d_o = blist_today[0][2]
            b_last = blist_today[-1]
            b_dt, hm, bo, bh, bl, bc, bv = b_last
            ret_open = (bc - d_o) / d_o * 100.0
            mkt_rets.append(ret_open)

            cv, cpv = 0.0, 0.0
            sh_h, sl_l = -1.0, 1e18
            orb30_h, orb30_l = -1.0, 1e18
            for b in blist_today:
                cv += b[6]
                cpv += ((b[3] + b[4] + b[5]) / 3.0) * max(1.0, b[6])
                sh_h = max(sh_h, b[3])
                sl_l = min(sl_l, b[4])
                if b[1] in ("09:15", "09:30"):
                    orb30_h = max(orb30_h, b[3])
                    orb30_l = min(orb30_l, b[4])

            vwap = cpv / max(1.0, cv)
            hvols = [x["cvm"].get(hm, 0.0) for x in hist_stats if x["cvm"].get(hm, 0.0) > 0]
            if len(hvols) < 4:
                continue
            rvol = cv / max(1.0, statistics.median(hvols))
            b_turns = [x["btm"].get(hm, x["turnover"] / 25.0) for x in hist_stats]
            med_bt = statistics.median(b_turns) if b_turns else (med_turn / 25.0)

            atr_px = pdc * adr / 100.0
            rng = bh - bl
            clv = ((bc - bl) / rng) if rng > 1e-9 else 0.50
            gap_pct = (d_o - pdc) / pdc * 100.0
            vwap_atr = (bc - vwap) / max(1e-6, atr_px)

            # Build trailing 18 completed 15m closes/OHLC for RSI14 & ADX14 telemetry
            recent_15m_bars = []
            if len(prior_days) >= 1:
                recent_15m_bars.extend(bars_by_td.get((sym, prior_days[-1]), [])[-16:])
            recent_15m_bars.extend(blist_today)
            rsi14 = compute_rsi_14([x[5] for x in recent_15m_bars[-20:]])
            adx14 = compute_adx_proxy_14([(x[3], x[4], x[5]) for x in recent_15m_bars[-16:]])

            # Structural opportunity detection
            sniffer_trigger = None
            if sh_h >= pdh * 1.0015 and bc < pdh and bc < bo and clv <= 0.30 and rvol >= 1.8:
                sniffer_trigger = "PDH_FAILED_SWEEP_SELL"
            elif sl_l <= pdl * 0.9985 and bc > pdl and bc > bo and clv >= 0.70 and rvol >= 1.8:
                sniffer_trigger = "PDL_FAILED_SWEEP_BUY"

            orb_trigger = None
            if "09:45" <= hm <= "11:30" and orb30_h > 0 and rvol >= 1.8:
                if bc > orb30_h * 1.001 and bc > vwap and clv >= 0.65:
                    orb_trigger = "ORB30_BREAKOUT_BUY"
                elif bc < orb30_l * 0.999 and bc < vwap and clv <= 0.35:
                    orb_trigger = "ORB30_BREAKDOWN_SELL"

            bare = sym.replace(".NS", "").replace(".BO", "")
            raw_items.append({
                "ticker": sym,
                "symbol": bare,
                "instrument_key": ikey_map.get(bare, ""),
                "fno_eligible": True,
                "session_date": session_date,
                "bar_dt": b_dt,
                "bar_hm": hm,
                "open": round(bo, 2),
                "high": round(bh, 2),
                "low": round(bl, 2),
                "close": round(bc, 2),
                "day_open": round(d_o, 2),
                "vwap": round(vwap, 2),
                "pdh": round(pdh, 2),
                "pdl": round(pdl, 2),
                "pdc": round(pdc, 2),
                "session_high": round(sh_h, 2),
                "session_low": round(sl_l, 2),
                "orb30_high": round(orb30_h, 2) if orb30_h > 0 else None,
                "orb30_low": round(orb30_l, 2) if orb30_l < 1e17 else None,
                "adr_pct": round(adr, 3),
                "atr_px": round(atr_px, 3),
                "rvol": round(rvol, 2),
                "clv": round(clv, 3),
                "gap_pct": round(gap_pct, 3),
                "ret_open_pct": round(ret_open, 3),
                "vwap_atr": round(vwap_atr, 3),
                "ret_3d_prior": round(r3, 3),
                "ret_10d_prior": round(r10, 3),
                "rsi": rsi14,
                "adx": adx14,
                "liq_1pct_bar_inr": round(0.01 * med_bt, 2),
                "sniffer_trigger": sniffer_trigger,
                "baseline_orb_trigger": orb_trigger,
            })

        mkt_ret = (sum(mkt_rets) / len(mkt_rets)) if mkt_rets else 0.0
        mkt_breadth = (sum(1 for v in mkt_rets if v > 0) / len(mkt_rets)) if mkt_rets else 0.50
        is_sniffer_window = "09:30" <= target_hm <= "12:30"

        for it in raw_items:
            it["mkt_ret_pct"] = round(mkt_ret, 3)
            it["mkt_breadth"] = round(mkt_breadth, 3)

        return {
            "as_of_ts": f"{session_date} {target_hm}:00",
            "session_date": session_date,
            "bar_hm": target_hm,
            "is_sniffer_window": is_sniffer_window,
            "market_context": {
                "fno_mkt_return_pct": round(mkt_ret, 3),
                "fno_advance_ratio": round(mkt_breadth, 3),
                "regime": "BULL" if mkt_ret > 0.35 else ("BEAR" if mkt_ret < -0.35 else "CHOP"),
            },
            "items": raw_items,
        }
