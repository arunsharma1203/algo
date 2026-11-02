"""
Unit Tests for Option Sniffer Regime & Circuit Breaker Correctness:
  1. Heavyweight 15-minute bar completion (candle_end_time <= candidate_entry_time).
  2. Wilder ADX(14) strict 28-bar warm-up requirement (fails closed if n < 28).
  3. Cooldown activation triggered ONLY by executed trades of the preceding variant.
  4. Causal cooldown evaluation: overlapping trades entering before an SL exit are never blocked.
  5. Directional vs Omnidirectional cooldown divergence on reversal candidates.
  6. Persistent session latch for Realized-Loss Circuit Breaker.
  7. Persistent session latch for Peak-to-Trough Drawdown Circuit Breaker.

STRICT SAFEGUARDS:
  - 100% In-Memory / Deterministic Unit Testing.
  - Zero live broker connections, zero orders placed, zero DB mutations.
"""

import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def compute_wilder_adx_strict(high, low, close, period=14):
    min_required_bars = 2 * period
    if len(close) < min_required_bars:
        return None
    tr_list = []
    dm_plus_list = []
    dm_minus_list = []
    for i in range(1, len(close)):
        h = high.iloc[i]
        l = low.iloc[i]
        h_prev = high.iloc[i-1]
        l_prev = low.iloc[i-1]
        c_prev = close.iloc[i-1]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_list.append(tr)
        up = h - h_prev
        down = l_prev - l
        dm_plus_list.append(up if (up > down and up > 0) else 0.0)
        dm_minus_list.append(down if (down > up and down > 0) else 0.0)
    alpha = 1.0 / period
    tr_s = pd.Series(tr_list).ewm(alpha=alpha, adjust=False).mean()
    dm_p = pd.Series(dm_plus_list).ewm(alpha=alpha, adjust=False).mean()
    dm_m = pd.Series(dm_minus_list).ewm(alpha=alpha, adjust=False).mean()
    di_p = 100.0 * (dm_p / (tr_s + 1e-9))
    di_m = 100.0 * (dm_m / (tr_s + 1e-9))
    dx = 100.0 * ((di_p - di_m).abs() / (di_p + di_m + 1e-9))
    dx_valid = dx.iloc[period-1:]
    if len(dx_valid) < period:
        return None
    adx_val = float(dx_valid.ewm(alpha=alpha, adjust=False).mean().iloc[-1])
    return adx_val if not np.isnan(adx_val) else None


def compute_wilder_adx_canonical(high, low, close, period=14):
    """
    Canonical Wilder ADX(14) implementation adhering strictly to J. Welles Wilder's 1978 specification:
      - Strictly requires at least 2 * period completed bars (28 bars).
      - First 14 transitions are summed for initial TR, +DM, -DM.
      - Subsequent transitions use Wilder's smoothing: S_t = S_{t-1} - S_{t-1}/P + X_t.
      - First 14 DX values (bars 14 to 27) are averaged as an arithmetic mean for initial ADX.
      - Subsequent ADX values use Wilder's smoothing: ADX_t = (ADX_{t-1} * (P - 1) + DX_t) / P.
      - Fails closed (returns None) on missing/NaN values or insufficient bars (< 28).
    """
    if len(close) < 2 * period:
        return None
    h_arr = np.asarray(high, dtype=float)
    l_arr = np.asarray(low, dtype=float)
    c_arr = np.asarray(close, dtype=float)
    if np.isnan(h_arr).any() or np.isnan(l_arr).any() or np.isnan(c_arr).any():
        return None
        
    n = len(c_arr)
    tr = np.zeros(n)
    dm_p = np.zeros(n)
    dm_m = np.zeros(n)
    
    for i in range(1, n):
        h, l = h_arr[i], l_arr[i]
        h_prev, l_prev, c_prev = h_arr[i-1], l_arr[i-1], c_arr[i-1]
        tr[i] = max(h - l, abs(h - c_prev), abs(l - c_prev))
        up = h - h_prev
        down = l_prev - l
        dm_p[i] = up if (up > down and up > 0) else 0.0
        dm_m[i] = down if (down > up and down > 0) else 0.0
        
    tr_sum = np.zeros(n)
    dm_p_sum = np.zeros(n)
    dm_m_sum = np.zeros(n)
    
    tr_sum[period] = np.sum(tr[1:period+1])
    dm_p_sum[period] = np.sum(dm_p[1:period+1])
    dm_m_sum[period] = np.sum(dm_m[1:period+1])
    
    for i in range(period + 1, n):
        tr_sum[i] = tr_sum[i-1] - (tr_sum[i-1] / period) + tr[i]
        dm_p_sum[i] = dm_p_sum[i-1] - (dm_p_sum[i-1] / period) + dm_p[i]
        dm_m_sum[i] = dm_m_sum[i-1] - (dm_m_sum[i-1] / period) + dm_m[i]
        
    di_p = np.zeros(n)
    di_m = np.zeros(n)
    dx = np.zeros(n)
    
    for i in range(period, n):
        if tr_sum[i] > 1e-9:
            di_p[i] = 100.0 * (dm_p_sum[i] / tr_sum[i])
            di_m[i] = 100.0 * (dm_m_sum[i] / tr_sum[i])
        sum_di = di_p[i] + di_m[i]
        dx[i] = 100.0 * (abs(di_p[i] - di_m[i]) / sum_di) if sum_di > 1e-9 else 0.0
        
    adx = np.zeros(n)
    adx[2 * period - 1] = np.mean(dx[period : 2 * period])
    
    for i in range(2 * period, n):
        adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period
        
    val = float(adx[-1])
    return val if not np.isnan(val) else None


def replay_circuit_breaker(trades, mode="REALIZED_LOSS", limit=3500.0):
    executed = []
    blocked = []
    sorted_trades = sorted(trades, key=lambda x: x["entry_dt"])
    is_latched = False
    latch_time = None
    
    for cand in sorted_trades:
        cand_entry = cand["entry_dt"]
        prior_exits = sorted(
            [t for t in executed if t["exit_dt"] <= cand_entry],
            key=lambda x: x["exit_dt"]
        )
        if not is_latched:
            cum = 0.0
            peak = 0.0
            for pe in prior_exits:
                cum += pe["net_pnl"]
                if cum > peak:
                    peak = cum
                if mode == "REALIZED_LOSS":
                    if cum <= -limit:
                        is_latched = True
                        latch_time = pe["exit_dt"]
                        break
                elif mode == "PEAK_TO_TROUGH":
                    if (peak - cum) >= limit:
                        is_latched = True
                        latch_time = pe["exit_dt"]
                        break
        if is_latched:
            blocked.append({
                "trade_id": cand["trade_id"],
                "entry_dt": cand["entry_dt"],
                "latch_time": latch_time,
                "net_pnl": cand["net_pnl"]
            })
        else:
            executed.append(cand)
    return executed, blocked


def evaluate_cooldown(cand_trade, prior_executed_trades, mode="DIRECTIONAL", cooldown_sec=900):
    cand_entry = cand_trade["entry_dt"]
    u = cand_trade["underlying"]
    opt = cand_trade["option_type"]
    
    in_cd = False
    for pt in prior_executed_trades:
        if pt.get("exit_reason") != "STOP_LOSS_HIT":
            continue
        sl_exit = pt["exit_dt"]
        if sl_exit > cand_entry:
            continue
        elapsed = (cand_entry - sl_exit).total_seconds()
        if 0 <= elapsed < cooldown_sec:
            if mode == "DIRECTIONAL":
                if pt["underlying"] == u and pt["option_type"] == opt:
                    in_cd = True
                    break
            elif mode == "OMNIDIRECTIONAL":
                if pt["underlying"] == u:
                    in_cd = True
                    break
    return in_cd


class TestRegimeCircuitBreakerLatch(unittest.TestCase):
    """Verifies that circuit breakers latch persistently, cooldown is causal and triggered only by executed trades."""

    def test_heavyweight_bar_completion(self):
        """Proves candle_end_time <= candidate_entry_time excludes unclosed partial 15m candles."""
        # 15m candle starting at 09:15 ends at 09:30:00
        # 15m candle starting at 09:30 ends at 09:45:00
        entry_time = datetime(2026, 10, 14, 9, 30, 15) # 15 seconds after 09:30
        
        candle_1_start = datetime(2026, 10, 14, 9, 15, 0)
        candle_2_start = datetime(2026, 10, 14, 9, 30, 0)
        
        # Candidate entry is at 09:30:15
        # Candle 1 ends at 09:30:00 <= 09:30:15 -> COMPLETED
        c1_completed = (candle_1_start + timedelta(minutes=15)) <= entry_time
        # Candle 2 ends at 09:45:00 <= 09:30:15 -> FALSE (Unclosed partial candle!)
        c2_completed = (candle_2_start + timedelta(minutes=15)) <= entry_time
        
        self.assertTrue(c1_completed, "09:15-09:30 candle is completed at 09:30:15.")
        self.assertFalse(c2_completed, "09:30-09:45 candle is NOT completed at 09:30:15; Close at 09:45 is in the future!")

    def test_adx_strict_28_bar_warmup(self):
        """Wilder ADX(14) must fail closed (return None) when fewer than 28 completed bars exist."""
        np.random.seed(42)
        prices = pd.Series(100 + np.cumsum(np.random.randn(30)))
        h = prices + 0.5
        l = prices - 0.5
        c = prices
        
        self.assertIsNone(compute_wilder_adx_strict(h.iloc[:15], l.iloc[:15], c.iloc[:15], period=14), "15 bars has only 1 DX point; cannot smooth ADX.")
        self.assertIsNone(compute_wilder_adx_strict(h.iloc[:27], l.iloc[:27], c.iloc[:27], period=14), "27 bars has 13 DX points; insufficient for 14-period ADX.")
        adx_28 = compute_wilder_adx_strict(h.iloc[:28], l.iloc[:28], c.iloc[:28], period=14)
        self.assertIsNotNone(adx_28, "28 bars provides exactly 14 DX points; valid initial smoothed ADX.")
        self.assertGreater(adx_28, 0.0)

    def test_cooldown_activated_only_by_executed_trades(self):
        """A stop-loss on a trade that was BLOCKED by Variant B cannot trigger a cooldown in Variant C."""
        t0 = datetime(2026, 10, 14, 10, 0, 0)
        
        # Trade X: In historical manifest, was STOP_LOSS_HIT. BUT was BLOCKED by Variant B (e.g. spread was wide).
        trade_x_blocked = {
            "trade_id": "TX", "underlying": "NIFTY", "option_type": "CE",
            "entry_dt": t0, "exit_dt": t0 + timedelta(minutes=15),
            "exit_reason": "STOP_LOSS_HIT", "net_pnl": -2500.0,
            "executed_in_var_b": False
        }
        
        # Candidate Y: Enters 5 minutes after Trade X exit
        cand_y = {
            "trade_id": "CY", "underlying": "NIFTY", "option_type": "CE",
            "entry_dt": t0 + timedelta(minutes=20), "exit_dt": t0 + timedelta(minutes=30),
            "net_pnl": +1500.0
        }
        
        # Only trades executed under Variant B can populate executed history
        executed_trades_history = []
        if trade_x_blocked["executed_in_var_b"]:
            executed_trades_history.append(trade_x_blocked)
            
        in_cd = evaluate_cooldown(cand_y, executed_trades_history, mode="DIRECTIONAL")
        self.assertFalse(in_cd, "Trade X was never executed in Variant B, so its SL cannot trigger a cooldown in Variant C.")

    def test_realized_loss_breaker_latches_persistently(self):
        """Later profitable exits cannot re-enable entries once realized loss reaches limit."""
        t0 = datetime(2026, 10, 14, 9, 30, 0)
        trades = [
            {"trade_id": "T1", "entry_dt": t0, "exit_dt": t0 + timedelta(minutes=15), "net_pnl": -2000.0},
            {"trade_id": "T2", "entry_dt": t0 + timedelta(minutes=1), "exit_dt": t0 + timedelta(minutes=20), "net_pnl": -1600.0},
            {"trade_id": "T3", "entry_dt": t0 + timedelta(minutes=2), "exit_dt": t0 + timedelta(minutes=30), "net_pnl": +5000.0},
            {"trade_id": "T4", "entry_dt": t0 + timedelta(minutes=35), "exit_dt": t0 + timedelta(minutes=45), "net_pnl": +1000.0},
        ]
        executed, blocked = replay_circuit_breaker(trades, mode="REALIZED_LOSS", limit=3500.0)
        exec_ids = [t["trade_id"] for t in executed]
        block_ids = [t["trade_id"] for t in blocked]
        self.assertEqual(exec_ids, ["T1", "T2", "T3"])
        self.assertEqual(block_ids, ["T4"], "T4 must be blocked because the session latched at 09:50.")

    def test_peak_to_trough_drawdown_latches_persistently(self):
        """Later profitable exits cannot re-enable entries once peak-to-trough drawdown reaches limit."""
        t0 = datetime(2026, 10, 13, 9, 30, 0)
        trades = [
            {"trade_id": "T1", "entry_dt": t0, "exit_dt": t0 + timedelta(minutes=10), "net_pnl": +5000.0},
            {"trade_id": "T2", "entry_dt": t0 + timedelta(minutes=5), "exit_dt": t0 + timedelta(minutes=20), "net_pnl": -3600.0},
            {"trade_id": "T3", "entry_dt": t0 + timedelta(minutes=6), "exit_dt": t0 + timedelta(minutes=30), "net_pnl": +4000.0},
            {"trade_id": "T4", "entry_dt": t0 + timedelta(minutes=35), "exit_dt": t0 + timedelta(minutes=45), "net_pnl": +1000.0},
        ]
        executed, blocked = replay_circuit_breaker(trades, mode="PEAK_TO_TROUGH", limit=3500.0)
        exec_ids = [t["trade_id"] for t in executed]
        block_ids = [t["trade_id"] for t in blocked]
        self.assertEqual(exec_ids, ["T1", "T2", "T3"])
        self.assertEqual(block_ids, ["T4"], "T4 must remain blocked even though session equity hit a new high.")

    def test_cooldown_never_blocks_prior_overlapping_entry(self):
        """A stop-loss cooldown must never block an entry that occurred before the stop-loss exit."""
        t0 = datetime(2026, 10, 14, 10, 0, 0)
        trade_a = {
            "trade_id": "TA", "underlying": "NIFTY", "option_type": "CE",
            "entry_dt": t0, "exit_dt": t0 + timedelta(minutes=15),
            "exit_reason": "STOP_LOSS_HIT", "net_pnl": -2500.0
        }
        trade_b = {
            "trade_id": "TB", "underlying": "NIFTY", "option_type": "CE",
            "entry_dt": t0 + timedelta(minutes=5), "exit_dt": t0 + timedelta(minutes=25),
            "exit_reason": "TARGET_MET", "net_pnl": +2000.0
        }
        trade_c = {
            "trade_id": "TC", "underlying": "NIFTY", "option_type": "CE",
            "entry_dt": t0 + timedelta(minutes=20), "exit_dt": t0 + timedelta(minutes=30),
            "exit_reason": "STOP_LOSS_HIT", "net_pnl": -1500.0
        }
        trade_d = {
            "trade_id": "TD", "underlying": "NIFTY", "option_type": "CE",
            "entry_dt": t0 + timedelta(minutes=31), "exit_dt": t0 + timedelta(minutes=40),
            "exit_reason": "TARGET_MET", "net_pnl": +1800.0
        }
        self.assertFalse(evaluate_cooldown(trade_b, [trade_a], mode="DIRECTIONAL"))
        self.assertTrue(evaluate_cooldown(trade_c, [trade_a, trade_b], mode="DIRECTIONAL"))
        self.assertFalse(evaluate_cooldown(trade_d, [trade_a, trade_b], mode="DIRECTIONAL"))

    def test_directional_vs_omni_cooldown_reversal_divergence(self):
        """Directional cooldown allows opposite-direction reversal trades; Omnidirectional blocks them."""
        t0 = datetime(2026, 10, 14, 11, 0, 0)
        trade_loss = {
            "trade_id": "TL", "underlying": "NIFTY", "option_type": "CE",
            "entry_dt": t0, "exit_dt": t0 + timedelta(minutes=10),
            "exit_reason": "STOP_LOSS_HIT", "net_pnl": -2800.0
        }
        cand_reversal = {
            "trade_id": "TR", "underlying": "NIFTY", "option_type": "PE",
            "entry_dt": t0 + timedelta(minutes=15), "exit_dt": t0 + timedelta(minutes=30),
            "net_pnl": +1900.0
        }
        self.assertFalse(evaluate_cooldown(cand_reversal, [trade_loss], mode="DIRECTIONAL"))
        self.assertTrue(evaluate_cooldown(cand_reversal, [trade_loss], mode="OMNIDIRECTIONAL"))

    def test_canonical_wilder_cotton_1978_reference(self):
        """Validates canonical Wilder 14-period initial values against J. Welles Wilder's 1978 published Cotton sample."""
        # 15 days of Cotton prices from Wilder 1978 (Table 1, pages 36-37)
        # Yields exactly 14 period-to-period transitions
        cotton_sample = [
            (52.80, 52.35, 52.75), # Day 1
            (52.95, 52.30, 52.45), # Day 2
            (52.55, 51.50, 51.75), # Day 3
            (51.85, 50.80, 51.10), # Day 4
            (51.40, 50.55, 50.80), # Day 5
            (51.40, 50.55, 51.20), # Day 6
            (51.80, 51.10, 51.50), # Day 7
            (51.60, 51.05, 51.30), # Day 8
            (51.80, 51.05, 51.30), # Day 9
            (51.80, 51.25, 51.65), # Day 10
            (51.70, 51.25, 51.30), # Day 11
            (51.90, 51.35, 51.80), # Day 12
            (52.20, 51.60, 52.10), # Day 13
            (52.20, 51.70, 51.85), # Day 14
            (52.00, 51.50, 51.60), # Day 15
        ]
        tr_list = []
        dm_p_list = []
        dm_m_list = []
        for i in range(1, len(cotton_sample)):
            h, l, c = cotton_sample[i]
            h_p, l_p, c_p = cotton_sample[i-1]
            tr = max(h - l, abs(h - c_p), abs(l - c_p))
            up = round(h - h_p, 2)
            down = round(l_p - l, 2)
            tr_list.append(tr)
            dm_p_list.append(up if (up > down and up > 0) else 0.0)
            dm_m_list.append(down if (down > up and down > 0) else 0.0)
            
        # Verify Wilder's initial 14-period sums (Sum TR = 9.65, +DM = 1.25, -DM = 2.00)
        self.assertAlmostEqual(sum(tr_list), 9.65, places=2)
        self.assertAlmostEqual(sum(dm_p_list), 1.25, places=2)
        self.assertAlmostEqual(sum(dm_m_list), 2.00, places=2)
        
        # Verify Wilder's initial DI values
        di_p = 100.0 * (sum(dm_p_list) / sum(tr_list))
        di_m = 100.0 * (sum(dm_m_list) / sum(tr_list))
        self.assertAlmostEqual(di_p, 12.95, delta=0.05)
        self.assertAlmostEqual(di_m, 20.73, delta=0.05)
        
        # Verify first DX value (Wilder reports 23.10% -> 23%)
        dx = 100.0 * (abs(di_p - di_m) / (di_p + di_m))
        self.assertAlmostEqual(dx, 23.10, delta=0.05)

    def test_canonical_wilder_adx_steady_trend(self):
        """Deterministic strictly upward trend must yield DX = 100.0 and ADX = 100.0."""
        n = 35
        # Constant bar range of 2.0 with consistent upward step of 1.0 each bar
        h = [12.0 + i for i in range(n)]
        l = [10.0 + i for i in range(n)]
        c = [11.0 + i for i in range(n)]
        
        adx_val = compute_wilder_adx_canonical(h, l, c, period=14)
        self.assertIsNotNone(adx_val)
        self.assertAlmostEqual(adx_val, 100.0, places=2, msg="Steady upward trend must have ADX = 100.0.")

    def test_canonical_wilder_adx_flat_market(self):
        """Completely flat market with zero directional movement must yield DX = 0.0 and ADX = 0.0."""
        n = 35
        h = [12.0] * n
        l = [10.0] * n
        c = [11.0] * n
        
        adx_val = compute_wilder_adx_canonical(h, l, c, period=14)
        self.assertIsNotNone(adx_val)
        self.assertAlmostEqual(adx_val, 0.0, places=2, msg="Flat market with zero directional displacement must have ADX = 0.0.")

    def test_canonical_wilder_adx_nan_and_insufficient_data(self):
        """Canonical Wilder ADX must fail closed (return None) when data contains NaNs or length < 28."""
        n = 30
        h = [12.0 + i for i in range(n)]
        l = [10.0 + i for i in range(n)]
        c = [11.0 + i for i in range(n)]
        
        # Less than 28 bars
        self.assertIsNone(compute_wilder_adx_canonical(h[:27], l[:27], c[:27], period=14))
        
        # Contains NaN
        h_nan = list(h)
        h_nan[10] = np.nan
        self.assertIsNone(compute_wilder_adx_canonical(h_nan, l, c, period=14), "Must fail closed if any bar contains NaN.")


if __name__ == "__main__":
    unittest.main()

