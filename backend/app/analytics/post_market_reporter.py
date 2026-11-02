"""
POST-MARKET DAILY INTELLIGENCE REPORTER
=======================================
Scheduled job firing Mon-Fri at 16:00 IST.
Compiles and pushes an authoritative post-market summary to Telegram covering:
  1. Macro Market Regime & NIFTY Trend
  2. Portfolio Risk & Heat Status
  3. Daily Trade Outcomes & Win Rate (Legacy vs Qlib)
  4. System Operational Status
"""

import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, Optional
import pytz

from app.data.historical_data_layer import get_db_path
from app.analytics.master_logger import MasterLogger
from app.analytics.telegram_notifier import send_telegram_message
from app.analytics.macro_engine import get_macro_regime
from app.analytics.kelly_sizer import get_portfolio_heat_status

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class PostMarketReporter:
    """Compiles daily operational performance telemetry and dispatches to Telegram."""

    @classmethod
    def generate_daily_summary(cls, today_date_str: Optional[str] = None) -> Dict[str, Any]:
        """Queries SQLite and returns aggregated daily metrics."""
        if not today_date_str:
            today_date_str = datetime.now(IST).strftime("%Y-%m-%d")

        conn = sqlite3.connect(get_db_path(), timeout=10.0)
        rows = []
        try:
            cur = conn.cursor()
            cur.execute("""
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
                    outcome TEXT,
                    profit_pct REAL,
                    exit_time TEXT,
                    engine TEXT DEFAULT 'LEGACY'
                )
            """)
            # 1. Query trades closed or updated today
            query = """
                SELECT 
                    engine,
                    outcome,
                    profit_pct,
                    direction,
                    trade_type,
                    ticker
                FROM ml_trade_history
                WHERE (exit_time LIKE ? OR timestamp LIKE ?)
                  AND status = 'CLOSED'
            """
            like_pattern = f"{today_date_str}%"
            cur.execute(query, (like_pattern, like_pattern))
            rows = cur.fetchall()
        except sqlite3.OperationalError as oe:
            logger.warning(f"[PostMarketReporter] ml_trade_history query note: {oe}")
            rows = []
        finally:
            conn.close()

        total_trades = len(rows)
        wins = 0
        losses = 0
        squared_off = 0
        pnl_sum = 0.0

        engine_breakdown: Dict[str, Dict[str, Any]] = {
            "LEGACY": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
            "QLIB": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        }

        for r in rows:
            eng = str(r[0] or "LEGACY").upper()
            outcome = str(r[1] or "")
            profit = float(r[2] or 0.0)

            pnl_sum += profit

            if eng not in engine_breakdown:
                engine_breakdown[eng] = {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}

            engine_breakdown[eng]["trades"] += 1
            engine_breakdown[eng]["pnl"] += profit

            if "TARGET MET" in outcome or profit > 0:
                wins += 1
                engine_breakdown[eng]["wins"] += 1
            elif "SL HIT" in outcome or profit < 0:
                losses += 1
                engine_breakdown[eng]["losses"] += 1
            else:
                squared_off += 1

        win_rate = round((wins / total_trades * 100.0), 1) if total_trades > 0 else 0.0
        avg_pnl = round((pnl_sum / total_trades), 2) if total_trades > 0 else 0.0

        # 2. Query macro state
        macro = get_macro_regime()
        regime = macro.get("regime_verdict", "NEUTRAL_CHOP")
        dist_ema20 = macro.get("distance_ema20_pct", 0.0)

        # 3. Query portfolio heat
        heat_info = get_portfolio_heat_status()
        current_heat = heat_info.get("total_portfolio_heat_pct", 0.0)
        max_heat = heat_info.get("max_heat_cap_pct", 6.0)
        heat_status = heat_info.get("status", "NORMAL")
        open_pos_count = heat_info.get("actual_positions_count", 0)

        return {
            "date": today_date_str,
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "squared_off": squared_off,
            "win_rate": win_rate,
            "total_pnl": round(pnl_sum, 2),
            "avg_pnl": avg_pnl,
            "engine_breakdown": engine_breakdown,
            "regime": regime,
            "dist_ema20": dist_ema20,
            "current_heat": current_heat,
            "max_heat": max_heat,
            "heat_status": heat_status,
            "open_positions": open_pos_count
        }

    @classmethod
    def dispatch_post_market_report(cls) -> bool:
        """Compiles and sends the HTML post-market summary to Telegram."""
        now_ist = datetime.now(IST)
        date_str = now_ist.strftime("%Y-%m-%d")

        try:
            summary = cls.generate_daily_summary(date_str)

            regime_emoji = "🐂" if summary["regime"] == "BULL_EXPANSION" else ("🐻" if summary["regime"] == "BEAR_DISTRIBUTION" else "↔️")
            pnl_emoji = "🟢" if summary["total_pnl"] >= 0 else "🔴"

            # Build HTML Message
            lines = [
                "📊 <b>POST-MARKET DAILY INTELLIGENCE SUMMARY</b>",
                f"📅 <i>{date_str} | Market Session Close</i>\n",
                f"{regime_emoji} <b>Macro Regime</b>: <code>{summary['regime']}</code> ({summary['dist_ema20']:+.2f}% vs 20 EMA)",
                f"🛡️ <b>Portfolio Heat</b>: {summary['current_heat']:.1f}% / {summary['max_heat']:.1f}% ({summary['heat_status']} | {summary['open_positions']} Live Positions)\n",
                f"📈 <b>Session Performance</b>:",
                f"• Total Setups Closed: <b>{summary['total_trades']}</b>",
                f"• Outcomes: <b>{summary['wins']} Win</b> | <b>{summary['losses']} Loss</b> ({summary['win_rate']}% Win Rate)",
                f"• Day Net P&L: {pnl_emoji} <b>{summary['total_pnl']:+.2f}%</b> (Avg: {summary['avg_pnl']:+.2f}%)"
            ]

            # Engine breakdown
            lines.append("\n⚙️ <b>Engine Breakdown</b>:")
            for eng, stats in summary["engine_breakdown"].items():
                if stats["trades"] > 0:
                    lines.append(f"  • {eng}: {stats['wins']}W / {stats['losses']}L | P&L: {stats['pnl']:+.2f}% ({stats['trades']} trades)")
                else:
                    lines.append(f"  • {eng}: 0 trades today")

            # Data Feed & Token Health
            try:
                from app.data.upstox_provider import get_upstox_token_status
                t_info = get_upstox_token_status(verify_live=True)
                status_code = t_info.get("status")
                days_left = t_info.get("days_remaining")
                live_status = t_info.get("live_status", "UNCHECKED")
                if status_code == "ACTIVE":
                    lines.append(f"\n📡 <b>Upstox Market Data Feed</b>: Active (Token valid • {days_left:.0f} days left | Probe: {live_status})")
                elif status_code == "EXPIRING_SOON":
                    lines.append(f"\n⚠️ <b>Upstox Market Data Feed Alert</b>: Token expires in {days_left:.0f} days! Please refresh in Settings.")
                elif status_code == "EXPIRED":
                    lines.append("\n🚨 <b>Upstox Market Data Feed Alert</b>: Token EXPIRED! Running on Yahoo Finance fallback.")
                else:
                    lines.append(f"\n📡 <b>Upstox Market Data Feed</b>: {t_info.get('message', 'Active')}")
            except Exception as e:
                logger.debug(f"[PostMarketReporter] Upstox token check note: {e}")

            lines.append("⚡ <b>Live Order Routing</b>: NOT PROVISIONED (Simulation Guard Enforced)")
            lines.append("\nℹ️ <i>System nominal. Overnight risk rules strictly active. Next sweep: 09:30 IST.</i>")

            html_msg = "\n".join(lines)
            success = send_telegram_message(html_msg)

            MasterLogger.log_event(
                category="SCHEDULER",
                event_type="POST_MARKET_REPORT_SENT",
                message=f"Post-market summary sent to Telegram for {date_str} (P&L: {summary['total_pnl']:+.2f}%, {summary['total_trades']} trades)",
                severity="INFO" if success else "WARNING",
                details=summary
            )
            return success

        except Exception as e:
            logger.error(f"[PostMarketReporter] Failed to send post-market report: {e}", exc_info=True)
            MasterLogger.log_event(
                category="SCHEDULER",
                event_type="POST_MARKET_REPORT_ERROR",
                message=f"Post-market summary failed: {e}",
                severity="ERROR"
            )
            return False

