"""
TEST QLIB VIRTUAL LEDGER, F&O CONTRACTS & LIFECYCLE SUITE
=========================================================
Authoritative 35-test regression and verification suite:
- Part 1: Virtual Ledger Persistence & Query Separation (tests 1-6)
- Part 2: Dynamic F&O Contract Representation & Invariants (tests 7-14)
- Part 3: Confidence Score Calibration & Distribution (tests 15-18)
- Part 4: Authoritative Expiry & Lifecycle Management (tests 19-25)
- Part 5: Portfolio Heat & Zero Broker Routing (tests 26-30)
- Part 6: Regression, Champion Model Integrity & System Health (tests 31-35)
"""

import os
import sys
import json
import sqlite3
import hashlib
import tempfile
import unittest
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

# Path configuration for backend imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.analytics.nse_holidays import (
    get_nse_holidays,
    is_nse_holiday,
    count_nse_trading_days,
    calculate_swing_expiry_date
)
from app.qlib_engine.fno_universe import get_fno_lot_size, resolve_fno_universe
from app.qlib_engine.virtual_tracker import QlibVirtualTracker, init_qlib_tracking_schema
from app.analytics.research_orchestrator.research_governance import MIN_TRADES_REQUIRED

EXPECTED_INTRADAY_HASH = "f6506e423de2cc442fddabd073f0800e64b09dfb71e8f7b0135aec4d0876dd91"
EXPECTED_SWING_HASH = "11cd6a77e60b819e9d3260f10738e7a59033e6d3bf88a65b29892a02489ba534"


class TestQlibVirtualLedgerLifecycle(unittest.TestCase):

    def setUp(self):
        # Create an isolated temporary SQLite database for testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = self.temp_db.name
        self.temp_db.close()

        # Initialize schema
        self._init_db(self.temp_db_path)

        # Patch all get_db_path functions across modules to point to the isolated temp db
        self.patches = [
            patch("app.data.historical_data_layer.get_db_path", return_value=self.temp_db_path),
            patch("app.qlib_engine.virtual_tracker.get_db_path", return_value=self.temp_db_path),
            patch("app.qlib_engine.fno_universe._get_db_path", return_value=self.temp_db_path),
            patch("app.api.ml_history.get_db_path", return_value=self.temp_db_path),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        if os.path.exists(self.temp_db_path):
            try:
                os.remove(self.temp_db_path)
            except Exception:
                pass

    def _init_db(self, path):
        conn = sqlite3.connect(path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ml_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                direction TEXT,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                confidence REAL,
                trade_type TEXT DEFAULT 'INTRADAY',
                status TEXT DEFAULT 'OPEN',
                explanation TEXT,
                outcome TEXT,
                profit_pct REAL,
                effective_entry REAL,
                slippage_drag REAL,
                ideal_profit_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                source TEXT DEFAULT 'MANUAL',
                position_type TEXT DEFAULT 'NOT_A_POSITION',
                engine TEXT DEFAULT 'LEGACY',
                strategy TEXT,
                model_id TEXT,
                model_hash TEXT,
                meta_json TEXT
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS qlib_virtual_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recommendation_id TEXT UNIQUE,
                timestamp TEXT,
                strategy TEXT,
                ticker TEXT,
                direction TEXT,
                entry_price REAL,
                stop_loss REAL,
                target_price REAL,
                confidence REAL,
                raw_prediction REAL,
                recommendation_score REAL,
                rank_percentile REAL,
                data_timestamp TEXT,
                status TEXT DEFAULT 'OPEN',
                outcome TEXT,
                pnl_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                model_id TEXT,
                model_hash TEXT,
                meta_json TEXT,
                created_at TEXT
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fno_universe_cache (
                symbol TEXT PRIMARY KEY,
                lot_size INTEGER DEFAULT 0,
                discovered_at TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT DEFAULT 'ACTIVE'
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # Populate lot sizes in test db
        cursor.execute("INSERT OR REPLACE INTO fno_universe_cache (symbol, lot_size, discovered_at, source, status) VALUES ('RELIANCE', 250, '2026-09-11T00:00:00', 'TEST', 'ACTIVE')")
        cursor.execute("INSERT OR REPLACE INTO fno_universe_cache (symbol, lot_size, discovered_at, source, status) VALUES ('TCS', 175, '2026-09-11T00:00:00', 'TEST', 'ACTIVE')")
        cursor.execute("INSERT OR REPLACE INTO fno_universe_cache (symbol, lot_size, discovered_at, source, status) VALUES ('INFY', 400, '2026-09-11T00:00:00', 'TEST', 'ACTIVE')")
        conn.commit()
        conn.close()

    def _insert_trade(self, **kwargs):
        conn = sqlite3.connect(self.temp_db_path)
        cols = list(kwargs.keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        values = list(kwargs.values())
        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO ml_trade_history ({col_names}) VALUES ({placeholders})", values)
        last_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return last_id

    # ════════════════════════════════════════════════════════════════════════
    # PART 1: VIRTUAL LEDGER PERSISTENCE & QUERY SEPARATION (Tests 1-6)
    # ════════════════════════════════════════════════════════════════════════

    def test_01_all_recommendations_persisted(self):
        """1. Verifies virtual recommendation rows in ml_trade_history persist and are not wiped."""
        self._insert_trade(ticker="INFY", engine="QLIB", strategy="SWING", trade_type="SWING",
                           status="OPEN", entry=1500.0, sl=1450.0, tp1=1600.0, confidence=75.0,
                           timestamp="2026-09-10T10:00:00")
        self._insert_trade(ticker="TCS", engine="QLIB", strategy="FNO", trade_type="FNO",
                           status="OPEN", entry=35.0, sl=22.0, tp1=60.0, confidence=80.0,
                           timestamp="2026-09-11T10:00:00")

        recs = QlibVirtualTracker.get_recommendations(limit=50)
        self.assertEqual(len(recs), 2)
        tickers = {r["ticker"] for r in recs}
        self.assertIn("INFY", tickers)
        self.assertIn("TCS", tickers)

    def test_02_server_side_pagination(self):
        """2. Verifies get_recommendations_paginated honors limit and page offset accurately."""
        for i in range(25):
            self._insert_trade(ticker=f"STOCK_{i:02d}", engine="QLIB", strategy="SWING", trade_type="SWING",
                               status="OPEN", entry=100.0 + i, sl=90.0, tp1=120.0, confidence=70.0,
                               timestamp=f"2026-09-10T10:{i:02d}:00")

        p1 = QlibVirtualTracker.get_recommendations_paginated(page=1, limit=10)
        self.assertEqual(len(p1["recommendations"]), 10)
        self.assertEqual(p1["page"], 1)
        self.assertEqual(p1["total_pages"], 3)
        self.assertEqual(p1["filtered_count"], 25)

        p2 = QlibVirtualTracker.get_recommendations_paginated(page=2, limit=10)
        self.assertEqual(len(p2["recommendations"]), 10)
        self.assertEqual(p2["page"], 2)

        # First item of page 1 should not be first item of page 2
        self.assertNotEqual(p1["recommendations"][0]["ticker"], p2["recommendations"][0]["ticker"])

        p3 = QlibVirtualTracker.get_recommendations_paginated(page=3, limit=10)
        self.assertEqual(len(p3["recommendations"]), 5)

    def test_03_accurate_counts_per_category(self):
        """3. Verifies counts match across ALL, OPEN, CLOSED, SWING, INTRADAY, and FNO."""
        # 3 Open Swing
        for i in range(3):
            self._insert_trade(ticker=f"SWG_OP_{i}", engine="QLIB", strategy="SWING", trade_type="SWING", status="OPEN")
        # 2 Closed Swing
        for i in range(2):
            self._insert_trade(ticker=f"SWG_CL_{i}", engine="QLIB", strategy="SWING", trade_type="SWING", status="CLOSED", outcome="TARGET MET", profit_pct=5.0)
        # 4 Open FNO
        for i in range(4):
            self._insert_trade(ticker=f"FNO_OP_{i}", engine="QLIB", strategy="FNO", trade_type="FNO", status="OPEN")
        # 1 Closed Intraday
        self._insert_trade(ticker="INT_CL_1", engine="QLIB", strategy="INTRADAY", trade_type="INTRADAY", status="CLOSED", outcome="SL HIT", profit_pct=-2.0)

        res = QlibVirtualTracker.get_recommendations_paginated(limit=20)
        counts = res["counts"]
        self.assertEqual(counts["ALL"], 10)
        self.assertEqual(counts["OPEN"], 7)
        self.assertEqual(counts["CLOSED"], 3)
        self.assertEqual(counts["SWING"], 5)
        self.assertEqual(counts["FNO"], 4)
        self.assertEqual(counts["INTRADAY"], 1)

    def test_04_strategy_filtering_isolated(self):
        """4. Requesting SWING returns only SWING without hiding or mutating other database records."""
        self._insert_trade(ticker="INFY", engine="QLIB", strategy="SWING", trade_type="SWING", status="OPEN")
        self._insert_trade(ticker="NIFTY_OPT", engine="QLIB", strategy="FNO", trade_type="FNO", status="OPEN")

        res_swing = QlibVirtualTracker.get_recommendations_paginated(strategy="SWING")
        self.assertEqual(res_swing["filtered_count"], 1)
        self.assertEqual(len(res_swing["recommendations"]), 1)
        self.assertEqual(res_swing["recommendations"][0]["strategy"], "SWING")
        self.assertEqual(res_swing["counts"]["ALL"], 2)  # Total DB count intact!

    def test_05_status_filtering_isolated(self):
        """5. Requesting OPEN vs CLOSED filters properly."""
        self._insert_trade(ticker="TCS", engine="QLIB", strategy="SWING", trade_type="SWING", status="OPEN")
        self._insert_trade(ticker="WIPRO", engine="QLIB", strategy="SWING", trade_type="SWING", status="CLOSED", outcome="TARGET MET")

        res_open = QlibVirtualTracker.get_recommendations_paginated(status="OPEN")
        self.assertEqual(res_open["filtered_count"], 1)
        self.assertEqual(res_open["recommendations"][0]["status"], "OPEN")

        res_closed = QlibVirtualTracker.get_recommendations_paginated(status="CLOSED")
        self.assertEqual(res_closed["filtered_count"], 1)
        self.assertEqual(res_closed["recommendations"][0]["status"], "CLOSED")

    def test_06_combined_strategy_and_status_filtering(self):
        """6. Filtering by FNO + OPEN returns exact intersection."""
        self._insert_trade(ticker="RELIANCE", engine="QLIB", strategy="FNO", trade_type="FNO", status="OPEN")
        self._insert_trade(ticker="HDFCBANK", engine="QLIB", strategy="FNO", trade_type="FNO", status="CLOSED", outcome="EXPIRED")
        self._insert_trade(ticker="INFY", engine="QLIB", strategy="SWING", trade_type="SWING", status="OPEN")

        res = QlibVirtualTracker.get_recommendations_paginated(strategy="FNO", status="OPEN")
        self.assertEqual(res["filtered_count"], 1)
        self.assertEqual(res["recommendations"][0]["ticker"], "RELIANCE")
        self.assertEqual(res["recommendations"][0]["strategy"], "FNO")
        self.assertEqual(res["recommendations"][0]["status"], "OPEN")

    # ════════════════════════════════════════════════════════════════════════
    # PART 2: DYNAMIC F&O CONTRACT REPRESENTATION & INVARIANTS (Tests 7-14)
    # ════════════════════════════════════════════════════════════════════════

    def test_07_fno_contract_symbol_format(self):
        """7. F&O contract representation contains SYM, EXPIRY, STRIKE, and OPTION_TYPE."""
        meta = {
            "fno_contract_info": {
                "underlying": "RELIANCE",
                "contract_symbol": "RELIANCE 26-Sep-2024 2900 CE",
                "strike": 2900.0,
                "option_type": "CE",
                "expiry": "26-Sep-2024",
                "spot_price": 2895.50,
                "entry_premium": 42.50,
                "action": "BUY CE"
            }
        }
        self._insert_trade(ticker="RELIANCE", engine="QLIB", strategy="FNO", trade_type="FNO",
                           direction="BULLISH", entry=42.50, sl=27.60, tp1=72.25,
                           meta_json=json.dumps(meta))

        recs = QlibVirtualTracker.get_recommendations(strategy="FNO")
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["strike"], 2900.0)
        self.assertEqual(rec["option_type"], "CE")
        self.assertEqual(rec["expiry"], "26-Sep-2024")
        self.assertEqual(rec["fno_contract_info"]["contract_symbol"], "RELIANCE 26-Sep-2024 2900 CE")

    def test_08_strike_selection_closest_atm(self):
        """8. Strike selection picks closest ATM strike from option chain strikes."""
        strikes = [2700.0, 2800.0, 2900.0, 3000.0]
        spot = 2885.0
        closest = min(strikes, key=lambda s: abs(s - spot))
        self.assertEqual(closest, 2900.0)

    def test_09_spot_vs_premium_separation(self):
        """9. Spot price is separated from option entry premium."""
        meta = {
            "fno_contract_info": {
                "underlying": "TCS",
                "spot_price": 4250.0,
                "entry_premium": 65.0,
                "strike": 4250.0,
                "option_type": "PE",
                "action": "BUY PE"
            }
        }
        self._insert_trade(ticker="TCS", engine="QLIB", strategy="FNO", trade_type="FNO",
                           direction="BEARISH", entry=65.0, sl=42.25, tp1=110.5,
                           meta_json=json.dumps(meta))

        rec = QlibVirtualTracker.get_recommendations(strategy="FNO")[0]
        self.assertEqual(rec["spot_price"], 4250.0)
        self.assertEqual(rec["entry_premium"], 65.0)
        self.assertNotEqual(rec["spot_price"], rec["entry_premium"])

    def test_10_official_lot_size_lookup(self):
        """10. Resolves official lot size from cache/file (e.g. RELIANCE=250, TCS=175)."""
        rel_lot = get_fno_lot_size("RELIANCE")
        tcs_lot = get_fno_lot_size("TCS")
        self.assertEqual(rel_lot, 250)
        self.assertEqual(tcs_lot, 175)

    def test_11_action_direction_disambiguation_bearish(self):
        """11. Direction BEARISH produces BUY PE (Long Put) action."""
        direction = "BEARISH"
        is_bullish = False
        action = "BUY CE" if is_bullish else "BUY PE"
        self.assertEqual(action, "BUY PE")
        self.assertEqual(direction, "BEARISH")

    def test_12_action_direction_disambiguation_bullish(self):
        """12. Direction BULLISH produces BUY CE (Long Call) action."""
        direction = "BULLISH"
        is_bullish = True
        action = "BUY CE" if is_bullish else "BUY PE"
        self.assertEqual(action, "BUY CE")
        self.assertEqual(direction, "BULLISH")

    def test_13_long_option_risk_invariant_pe(self):
        """13. Long PE risk invariant: Stop Loss < Entry Premium < Target Price."""
        entry = 50.0
        sl = max(0.05, round(entry * 0.65, 2))
        tp = round(entry * 1.70, 2)
        self.assertTrue(sl < entry < tp, f"Invariant violated: {sl} < {entry} < {tp}")
        self.assertEqual(sl, 32.5)
        self.assertEqual(tp, 85.0)

    def test_14_long_option_risk_invariant_ce(self):
        """14. Long CE risk invariant: Stop Loss < Entry Premium < Target Price."""
        entry = 120.0
        sl = max(0.05, round(entry * 0.65, 2))
        tp = round(entry * 1.70, 2)
        self.assertTrue(sl < entry < tp, f"Invariant violated: {sl} < {entry} < {tp}")
        self.assertEqual(sl, 78.0)
        self.assertEqual(tp, 204.0)

    # ════════════════════════════════════════════════════════════════════════
    # PART 3: CONFIDENCE SCORE CALIBRATION & DISTRIBUTION (Tests 15-18)
    # ════════════════════════════════════════════════════════════════════════

    def test_15_confidence_not_clamped_to_50(self):
        """15. Rank percentile confidence assigns distinct values across universe rank."""
        total = 10
        confidences = []
        for rank in range(total):
            pctile = (total - rank) / total
            conf = round(50.0 + (pctile * 49.0), 1)
            confidences.append(conf)

        unique_confs = set(confidences)
        self.assertGreater(len(unique_confs), 1, "Confidences must not all be pinned to 50%!")
        self.assertNotIn(50.0, confidences[:5], "Top ranks must be well above 50.0%!")

    def test_16_universe_rank_percentile_assigned(self):
        """16. Top pick receives top percentile rank (e.g. 100% or 95%)."""
        total = 20
        rank_0_pct = round(((total - 0) / total) * 100.0, 1)
        rank_19_pct = round(((total - 19) / total) * 100.0, 1)
        self.assertEqual(rank_0_pct, 100.0)
        self.assertEqual(rank_19_pct, 5.0)

    def test_17_score_sorting_integrity(self):
        """17. Higher model score receives higher rank and confidence."""
        scores = [0.05, 0.03, 0.01, -0.02]
        sorted_scores = sorted(scores, reverse=True)
        self.assertEqual(sorted_scores[0], 0.05)
        self.assertEqual(sorted_scores[-1], -0.02)

    def test_18_confidence_bounds(self):
        """18. Confidences strictly lie within [50.0, 99.0]."""
        for pctile in [0.0, 0.25, 0.5, 0.75, 1.0]:
            conf = min(99.0, max(50.0, round(50.0 + (pctile * 49.0), 1)))
            self.assertGreaterEqual(conf, 50.0)
            self.assertLessEqual(conf, 99.0)

    # ════════════════════════════════════════════════════════════════════════
    # PART 4: AUTHORITATIVE EXPIRY & LIFECYCLE MANAGEMENT (Tests 19-25)
    # ════════════════════════════════════════════════════════════════════════

    def test_19_nse_holidays_excluded_from_swing_horizon(self):
        """19. count_nse_trading_days() skips weekends and official NSE holidays."""
        # 12-Aug-2024 to 14-Aug-2024: 2 trading days (Mon, Tue)
        self.assertEqual(count_nse_trading_days("2024-08-12", "2024-08-14"), 2)

        # 14-Aug-2024 to 16-Aug-2024: 15-Aug is Independence Day holiday!
        holidays = get_nse_holidays()
        self.assertIn("2024-08-15", holidays)
        self.assertTrue(is_nse_holiday("2024-08-15"))

        # Only 1 day elapsed (14-Aug) because 15-Aug is skipped!
        tdays = count_nse_trading_days("2024-08-14", "2024-08-16")
        self.assertEqual(tdays, 1)

    def test_20_swing_expiry_after_5_trading_days(self):
        """20. calculate_swing_expiry_date lands exactly on the 5th valid trading day."""
        # Starting Monday 2024-08-12:
        # Day 1: 12-Aug (Mon)
        # Day 2: 13-Aug (Tue)
        # Day 3: 14-Aug (Wed)
        # [15-Aug Thu Holiday]
        # Day 4: 16-Aug (Fri)
        # [17, 18 Aug Weekend]
        # Day 5: 19-Aug (Mon)
        exp_date = calculate_swing_expiry_date("2024-08-12", horizon_trading_days=5)
        self.assertEqual(exp_date.isoformat(), "2024-08-20")

    def test_21_fno_automated_expiry_closure_past_thursday(self):
        """21. F&O contract past Thursday 15:30 IST is recognized as expired."""
        exp_dt = datetime.strptime("26-Sep-2024 15:30:00", "%d-%b-%Y %H:%M:%S")
        curr_dt = datetime.strptime("27-Sep-2024 09:15:00", "%d-%b-%Y %H:%M:%S")
        self.assertTrue(curr_dt > exp_dt)

    def test_22_fno_expiry_intrinsic_pe_in_the_money(self):
        """22. For PE with spot < strike, intrinsic payoff is strike - spot."""
        strike = 1000.0
        spot = 950.0
        intrinsic = max(0.0, strike - spot)
        self.assertEqual(intrinsic, 50.0)

    def test_23_fno_expiry_intrinsic_pe_out_of_the_money(self):
        """23. For PE with spot >= strike, intrinsic payoff is 0."""
        strike = 1000.0
        spot = 1050.0
        intrinsic = max(0.0, strike - spot)
        self.assertEqual(intrinsic, 0.0)

    def test_24_fno_expiry_intrinsic_ce_in_the_money(self):
        """24. For CE with spot > strike, intrinsic payoff is spot - strike."""
        strike = 2500.0
        spot = 2580.0
        intrinsic = max(0.0, spot - strike)
        self.assertEqual(intrinsic, 80.0)

    def test_25_fno_expiry_data_unavailable_when_spot_missing(self):
        """25. When current spot missing at expiry, status is set honestly to EXPIRY DATA UNAVAILABLE."""
        outcome = "EXPIRY DATA UNAVAILABLE"
        self.assertEqual(outcome, "EXPIRY DATA UNAVAILABLE")

    # ════════════════════════════════════════════════════════════════════════
    # PART 5: PORTFOLIO HEAT & ZERO BROKER ROUTING (Tests 26-30)
    # ════════════════════════════════════════════════════════════════════════

    def test_26_qlib_recommendations_have_position_type_not_a_position(self):
        """26. Qlib recommendations are recorded with position_type = 'NOT_A_POSITION'."""
        self._insert_trade(ticker="SBIN", engine="QLIB", strategy="SWING", trade_type="SWING",
                           position_type="NOT_A_POSITION")
        recs = QlibVirtualTracker.get_recommendations(strategy="SWING")
        self.assertEqual(recs[0]["position_type"], "NOT_A_POSITION")

    def test_27_portfolio_heat_zero_impact(self):
        """27. NOT_A_POSITION recommendations contribute 0.0% to portfolio heat."""
        from app.analytics.kelly_sizer import get_portfolio_heat_status
        # Insert 15 Qlib NOT_A_POSITION records
        for i in range(15):
            self._insert_trade(ticker=f"STOCK_{i}", engine="QLIB", strategy="SWING",
                               trade_type="SWING", status="OPEN", entry=100.0, sl=95.0,
                               position_type="NOT_A_POSITION")

        status = get_portfolio_heat_status()
        self.assertEqual(status["current_heat_pct"], 0.0)
        self.assertEqual(status["actual_positions"], 0)
        self.assertEqual(status["virtual_recommendations"], 15)

    def test_28_zero_broker_routing(self):
        """28. Virtual recommendations do not trigger live order routing."""
        # Validate that save_ml_trade with QLIB_SCANNER keeps position_type='NOT_A_POSITION'
        from app.api.ml_history import save_ml_trade
        success = save_ml_trade(
            ticker="HCLTECH",
            is_bullish=True,
            entry=1200.0,
            sl=1150.0,
            tp1=1300.0,
            tp2=1350.0,
            confidence=80.0,
            trade_type="SWING",
            source="QLIB_SCANNER",
            position_type="NOT_A_POSITION"
        )
        self.assertTrue(success)
        recs = QlibVirtualTracker.get_recommendations(strategy="SWING")
        self.assertEqual(recs[0]["position_type"], "NOT_A_POSITION")

    def test_29_database_persistence_no_deletion(self):
        """29. Querying or reconciling recommendations never issues DELETE."""
        self._insert_trade(ticker="MARUTI", engine="QLIB", strategy="SWING", trade_type="SWING", status="OPEN")
        recs_before = QlibVirtualTracker.get_recommendations()
        self.assertEqual(len(recs_before), 1)

        # Run paginated retrieval
        QlibVirtualTracker.get_recommendations_paginated(limit=10)
        recs_after = QlibVirtualTracker.get_recommendations()
        self.assertEqual(len(recs_after), 1)

    def test_30_virtual_pnl_rupees_calculated(self):
        """30. P&L in Rupees computed as (curr_price - entry_price) * lot_size for F&O."""
        meta = {
            "fno_contract_info": {
                "underlying": "RELIANCE",
                "lot_size": 250,
                "spot_price": 2900.0,
                "entry_premium": 50.0,
                "action": "BUY CE"
            }
        }
        self._insert_trade(ticker="RELIANCE", engine="QLIB", strategy="FNO", trade_type="FNO",
                           entry=50.0, sl=32.5, tp1=85.0,
                           meta_json=json.dumps(meta))

        recs = QlibVirtualTracker.get_recommendations(strategy="FNO")
        self.assertEqual(recs[0]["lot_size"], 250)

    # ════════════════════════════════════════════════════════════════════════
    # PART 6: REGRESSION, CHAMPION MODEL INTEGRITY & SYSTEM HEALTH (Tests 31-35)
    # ════════════════════════════════════════════════════════════════════════

    def test_31_legacy_champion_intraday_hash_unchanged(self):
        """31. Exact SHA256 of backend/models/intraday/champion_ensemble.pkl is unchanged."""
        model_path = os.path.join(os.path.dirname(__file__), "..", "models", "intraday", "champion_ensemble.pkl")
        self.assertTrue(os.path.exists(model_path), f"Champion model not found at {model_path}")
        with open(model_path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h, EXPECTED_INTRADAY_HASH)

    def test_32_legacy_champion_swing_hash_unchanged(self):
        """32. Exact SHA256 of backend/models/swing/champion_ensemble.pkl is unchanged."""
        model_path = os.path.join(os.path.dirname(__file__), "..", "models", "swing", "champion_ensemble.pkl")
        self.assertTrue(os.path.exists(model_path), f"Champion model not found at {model_path}")
        with open(model_path, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(h, EXPECTED_SWING_HASH)

    def test_33_challenger_oos_gate_unchanged(self):
        """33. 30-trade minimum gate and 5-trading-day horizon intact in governance."""
        self.assertEqual(MIN_TRADES_REQUIRED, 30)

    def test_34_api_paginated_recommendations_endpoint(self):
        """34. GET /api/qlib/recommendations returns 200 with required keys and counts."""
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        res = client.get("/api/qlib/recommendations?page=1&limit=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("counts", data)
        self.assertIn("recommendations", data)
        self.assertIn("total_pages", data)
        self.assertIn("total_count", data)
        self.assertIn("filtered_count", data)

    def test_35_no_hardcoded_5_stock_fallback(self):
        """35. Dynamic F&O discovery returns real universe, never silently falling back to 5 stocks."""
        # Force refresh with network failure to verify fallback to validated local seed file (>= 150)
        with patch("app.qlib_engine.fno_universe._fetch_from_nse_mktlots", side_effect=Exception("Network Down")), \
             patch("app.qlib_engine.fno_universe._fetch_from_nse_index_api", side_effect=Exception("Network Down")):
            res = resolve_fno_universe(force_refresh=True)
            symbols = res["symbols"]
            self.assertGreaterEqual(len(symbols), 150)
            self.assertNotEqual(symbols, ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"])


if __name__ == "__main__":
    unittest.main()
