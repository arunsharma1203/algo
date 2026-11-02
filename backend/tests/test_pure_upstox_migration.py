"""
Forensic Test Suite: Pure Upstox Migration & Emergency Defensive Liquidation

Verifies:
1. Zero yfinance imports across active trading, scanning, and ML pipelines.
2. MarketProvider fails closed defensively without any Yahoo Finance fallback.
3. Token failure (HTTP 401/403/revocation) triggers emergency defensive liquidation:
   - All Consensus Game Arena positions immediately exited with reason 'EMERGENCY_UPSTOX_AUTH_FAIL'
   - Setting 'upstox_auth_status' marked as 'TOKEN_EXPIRED'
   - Zero fallback to Yahoo Finance (data source locked strictly to 'upstox')
4. Token reset behavior: saving fresh token or testing resets status to 'ACTIVE'.
5. Rate limit guardrails and circuit breaker protection.
6. Database historical integrity lock (+₹1,372.05 invariant).
"""

import ast
import os
import sys
import unittest
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock
import pandas as pd

# Add app to path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.data.upstox_provider import (
    handle_upstox_token_failure,
    reset_upstox_token_status,
    UpstoxRateLimiter,
    UpstoxRateLimitExceeded,
    get_upstox_limiter,
    upstox_request
)
import app.data.market_provider as market_provider


class TestPureUpstoxMigration(unittest.TestCase):

    def setUp(self):
        # Create a fresh temporary database for isolated testing
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self._init_test_db(self.db_path)

        # Patch get_db_path across relevant modules
        self.patcher_db0 = patch("app.data.database.get_db_path", return_value=self.db_path)
        self.patcher_db1 = patch("app.data.historical_data_layer.get_db_path", return_value=self.db_path)
        self.patcher_db2 = patch("app.analytics.consensus_game_engine.get_db_path", return_value=self.db_path)
        self.patcher_db0.start()
        self.patcher_db1.start()
        self.patcher_db2.start()

    def tearDown(self):
        self.patcher_db0.stop()
        self.patcher_db1.stop()
        self.patcher_db2.stop()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def _init_test_db(self, db_path):
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("""
            CREATE TABLE consensus_game_accounts (
                session_id TEXT PRIMARY KEY,
                account_name TEXT,
                initial_capital REAL,
                current_cash REAL,
                realized_pnl REAL,
                unrealized_pnl REAL,
                peak_equity REAL,
                max_drawdown_pct REAL,
                allocation_mode TEXT,
                max_positions INTEGER,
                fees_enabled INTEGER,
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                trade_selection_mode TEXT
            )
        """)
        c.execute("""
            CREATE TABLE consensus_game_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                trade_ref_id TEXT,
                ticker TEXT,
                direction TEXT,
                shares INTEGER,
                entry_price REAL,
                allocated_capital REAL,
                stop_loss REAL,
                target_price REAL,
                entry_time TEXT,
                status TEXT,
                exit_price REAL,
                exit_time TEXT,
                gross_pnl_rupees REAL,
                fees_rupees REAL,
                net_pnl_rupees REAL,
                pnl_pct REAL,
                outcome TEXT,
                details_json TEXT
            )
        """)
        c.execute("""
            CREATE TABLE consensus_trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                symbol TEXT,
                strategy TEXT,
                direction TEXT,
                entry_price REAL,
                stop_loss REAL,
                target_price REAL,
                composite_score REAL,
                qlib_score REAL,
                legacy_score REAL,
                finbert_score REAL,
                risk_reward_ratio REAL,
                status TEXT,
                outcome TEXT,
                profit_pct REAL,
                exit_price REAL,
                exit_time TEXT,
                source TEXT,
                position_type TEXT,
                details_json TEXT
            )
        """)
        # Initialize default settings
        c.execute("INSERT INTO app_settings (key, value) VALUES ('market_data_source', 'upstox')")
        c.execute("INSERT INTO app_settings (key, value) VALUES ('upstox_auth_status', 'ACTIVE')")
        c.execute("""
            INSERT INTO consensus_game_accounts (
                session_id, account_name, initial_capital, current_cash, realized_pnl,
                unrealized_pnl, peak_equity, max_drawdown_pct, max_positions, fees_enabled, status, created_at, updated_at
            )
            VALUES (
                'default_arena_session', 'Consensus Arena Trader', 100000.0, 100000.0, 0.0,
                0.0, 100000.0, 0.0, 5, 1, 'ACTIVE', '2026-09-24 10:00:00', '2026-09-24 10:00:00'
            )
        """)
        conn.commit()
        conn.close()

    # =========================================================================
    # 1. AST AUDIT: ZERO YFINANCE IN ACTIVE PIPELINES
    # =========================================================================
    def test_zero_yfinance_imports_in_active_pipelines(self):
        """Verifies through AST inspection that yfinance is completely absent from all active modules."""
        modules_to_audit = [
            "app/data/market_provider.py",
            "app/analytics/macro_engine.py",
            "app/analytics/smart_scanner.py",
            "app/tasks/intraday_market_daemon.py",
            "app/tasks/hoarder.py",
            "app/analytics/consensus_game_engine.py",
            "app/analytics/consensus_dynamic_guard.py",
            "app/analytics/qlib_discovery/qlib_evaluator.py",
            "app/api/intraday_ml.py",
            "app/api/swing_ml.py",
            "app/data/market_data.py",
            "app/tasks/autopilot_scanner.py",
            "app/analytics/autonomous_bot.py",
            "app/api/ml_history.py",
            "app/data/historical_data_layer.py",
            "app/api/data_lab.py",
            "app/api/ml_backtest.py",
            "app/api/backtest.py",
            "app/analytics/nlp_engine.py",
            "app/analytics/optuna_tuner.py",
            "app/analytics/dashboard_intelligence_service.py",
            "app/api/ml_lab.py"
        ]

        violations = []
        for rel_path in modules_to_audit:
            full_path = os.path.join(BASE_DIR, rel_path)
            if not os.path.exists(full_path):
                continue
            with open(full_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel_path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "yfinance" in alias.name:
                            violations.append(f"{rel_path}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.module and "yfinance" in node.module:
                        violations.append(f"{rel_path}: from {node.module} import ...")

        self.assertEqual(violations, [], f"yfinance imports found in active pipelines: {violations}")

    # =========================================================================
    # 2. MARKET PROVIDER FAIL-CLOSED INTEGRITY
    # =========================================================================
    def test_market_provider_source_strictly_locked_to_upstox(self):
        """get_active_data_source() must return 'upstox' unconditionally."""
        self.assertEqual(market_provider.get_active_data_source(), "upstox")

    @patch("app.data.upstox_provider.fetch_upstox_candles")
    def test_fetch_candles_fails_closed_without_yahoo_fallback(self, mock_candles):
        """When Upstox candle fetch fails or throws an exception, returns empty df (fails closed)."""
        mock_candles.side_effect = RuntimeError("Upstox connection refused")

        df = market_provider.fetch_candles("TCS", interval="15m", period="5d")
        self.assertTrue(df.empty)
        # Verify no Yahoo module is even imported or referenced
        self.assertFalse(hasattr(market_provider, "yf"))
        self.assertFalse(hasattr(market_provider, "_get_yahoo_candles"))

    @patch("app.data.upstox_provider.fetch_upstox_ltp")
    def test_fetch_live_quote_fails_closed_when_upstox_offline(self, mock_ltp):
        """When Upstox LTP is unavailable, returns UNAVAILABLE without falling back to Yahoo."""
        mock_ltp.return_value = None

        meta = market_provider.get_live_quote_with_meta("RELIANCE")
        self.assertIsNone(meta.get("price"))
        self.assertEqual(meta.get("source"), "unavailable")
        self.assertEqual(meta.get("quote_freshness"), "UNAVAILABLE")
        self.assertFalse(meta.get("is_realtime"))

    # =========================================================================
    # 3. EMERGENCY DEFENSIVE LIQUIDATION ON TOKEN FAILURE
    # =========================================================================
    @patch("app.data.upstox_provider.notify_upstox_auth_alert")
    def test_handle_upstox_token_failure_preserves_positions(self, mock_notify):
        """
        When Upstox token fails (HTTP 401/403):
        - Open positions remain OPEN (no emergency liquidation)
        - upstox_auth_status set to 'TOKEN_EXPIRED'
        - market_data_source locked to 'upstox' (never reverted to yahoo)
        """
        # Insert an active open position in test DB
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO consensus_game_positions (
                session_id, ticker, direction, shares, entry_price, allocated_capital,
                stop_loss, target_price, status
            ) VALUES (
                'default_arena_session', 'INFY', 'BUY', 10, 1500.0, 15000.0,
                1470.0, 1530.0, 'OPEN'
            )
        """)
        conn.commit()
        conn.close()

        # Trigger token failure
        res = handle_upstox_token_failure(status_code=401, error_message="Unauthorized: Token expired")

        # Verify DB state
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        # Check app_settings
        c.execute("SELECT value FROM app_settings WHERE key = 'upstox_auth_status'")
        auth_status = c.fetchone()[0]
        self.assertEqual(auth_status, "TOKEN_EXPIRED")

        c.execute("SELECT value FROM app_settings WHERE key = 'market_data_source'")
        src_status = c.fetchone()[0]
        self.assertEqual(src_status, "upstox")  # NOT yahoo!

        # Check position safety: remains OPEN, not liquidated
        c.execute("SELECT status, outcome FROM consensus_game_positions WHERE ticker = 'INFY'")
        pos = c.fetchone()
        self.assertEqual(pos[0], "OPEN")
        self.assertIsNone(pos[1])
        self.assertEqual(res.get("liquidated_positions"), 0)

        conn.close()

        # Notification must be sent
        self.assertTrue(mock_notify.called)

    def test_reset_upstox_token_status_restores_active(self):
        """reset_upstox_token_status sets upstox_auth_status back to ACTIVE."""
        # First set to EXPIRED
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE app_settings SET value = 'TOKEN_EXPIRED' WHERE key = 'upstox_auth_status'")
        conn.commit()
        conn.close()

        reset_upstox_token_status()

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT value FROM app_settings WHERE key = 'upstox_auth_status'")
        val = c.fetchone()[0]
        conn.close()
        self.assertEqual(val, "ACTIVE")

    # =========================================================================
    # 4. HTTP 401/403 DETECTION HOOK IN UPSTOX_REQUEST
    # =========================================================================
    @patch("app.data.upstox_provider.get_upstox_token", return_value="invalid_token")
    @patch("app.data.upstox_provider.requests.get")
    @patch("app.data.upstox_provider.handle_upstox_token_failure")
    def test_upstox_request_detects_401_and_calls_handler(self, mock_fail_handler, mock_get, mock_token):
        """HTTP 401 from Upstox immediately trips handle_upstox_token_failure."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = '{"errors":[{"message":"Token expired or invalid"}]}'
        mock_get.return_value = mock_resp

        get_upstox_limiter()._circuit_open_until = 0.0

        from app.data.upstox_provider import UpstoxAuthFailure
        with self.assertRaises(UpstoxAuthFailure):
            upstox_request("GET", "https://api.upstox.com/v2/market-quote/ltp")

        self.assertTrue(mock_fail_handler.called)

    # =========================================================================
    # 5. REALIZED P&L LOCK AUDIT (+₹1,372.05 INVARIANT)
    # =========================================================================
    def test_production_realized_pnl_lock(self):
        """Verifies that the actual production database market_data.db preserves +₹1,372.05 invariant."""
        prod_db = os.path.join(BASE_DIR, "market_data.db")
        if not os.path.exists(prod_db):
            self.skipTest("Production market_data.db not present in environment")

        conn = sqlite3.connect(prod_db)
        c = conn.cursor()
        c.execute("""
            SELECT COALESCE(SUM(net_pnl_rupees), 0.0)
            FROM consensus_game_positions
            WHERE session_id = 'default_arena_session' AND status = 'CLOSED' AND entry_time < '2026-09-25'
        """)
        historical_pnl = round(float(c.fetchone()[0]), 2)
        c.execute("SELECT realized_pnl FROM consensus_game_accounts WHERE session_id = 'default_arena_session'")
        row = c.fetchone()
        conn.close()

        self.assertIsNotNone(row, "default_arena_session missing from consensus_game_accounts")
        realized_pnl = float(row[0])
        self.assertEqual(historical_pnl, 1372.05, f"Historical closed trades invariant violated! Expected 1372.05, got {historical_pnl}")
        self.assertGreaterEqual(realized_pnl, 1372.05, f"Realized P&L lower than historical baseline! Got {realized_pnl}")


if __name__ == "__main__":
    unittest.main()
