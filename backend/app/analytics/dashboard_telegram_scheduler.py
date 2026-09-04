import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from app.data.historical_data_layer import get_db_path
from app.analytics.master_logger import MasterLogger
from app.analytics.dashboard_intelligence_service import DashboardIntelligenceService, REPORT_VERSION
from app.analytics.telegram_notifier import send_telegram_document

logger = logging.getLogger(__name__)


def _ensure_deliveries_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_report_deliveries (
            report_date TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            report_version TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            telegram_message_id TEXT,
            status TEXT NOT NULL
        );
    """)
    conn.commit()


class DashboardTelegramScheduler:
    """
    Manages daily automated and on-demand Telegram delivery of the Dashboard Market Intelligence PDF.
    Guarantees deterministic deduplication: exactly one delivery per report date unless forced.
    """

    @classmethod
    def get_delivery_status(cls, report_date: str) -> Optional[Dict[str, Any]]:
        """Retrieves delivery status for a specific date from SQLite."""
        try:
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            _ensure_deliveries_table(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT report_date, snapshot_id, report_version, sent_at, telegram_message_id, status
                FROM dashboard_report_deliveries
                WHERE report_date = ?
            """, (report_date,))
            row = cur.fetchone()
            conn.close()
            if row:
                return {
                    "report_date": row[0],
                    "snapshot_id": row[1],
                    "report_version": row[2],
                    "sent_at": row[3],
                    "telegram_message_id": row[4],
                    "status": row[5]
                }
            return None
        except Exception as e:
            logger.error(f"Failed to check delivery status: {e}")
            return None

    @classmethod
    def send_daily_report(cls, force: bool = False) -> Dict[str, Any]:
        """
        Executes the end-to-end Dashboard Report delivery pipeline:
        1. Snapshot acquisition
        2. Deduplication check
        3. PDF generation
        4. Telegram document transmission
        5. SQLite persistence & Master Logger audit
        """
        now = datetime.now()
        report_date = now.strftime("%Y-%m-%d")

        # 1. Deduplication check
        existing = cls.get_delivery_status(report_date)
        if existing and existing.get("status") == "DELIVERED" and not force:
            logger.info(f"Dashboard report for {report_date} already delivered. Suppressing duplicate.")
            try:
                MasterLogger.log_event(
                    category="TELEGRAM",
                    event_type="DASHBOARD_REPORT_TELEGRAM_DUPLICATE_SUPPRESSED",
                    message=f"Dashboard report for {report_date} already sent at {existing.get('sent_at')}. Duplicate suppressed.",
                    details={"report_date": report_date, "sent_at": existing.get("sent_at")}
                )
            except Exception:
                pass
            return {
                "status": "duplicate_suppressed",
                "message": f"Report for {report_date} already delivered today.",
                "report_date": report_date,
                "sent_at": existing.get("sent_at")
            }

        # 2. Acquire current normalized Dashboard snapshot
        try:
            snapshot = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=True)
            snapshot_id = snapshot.get("snapshot_id", "unknown_snapshot")
        except Exception as e:
            logger.error(f"Failed to acquire dashboard snapshot for report delivery: {e}")
            return {"status": "error", "message": f"Snapshot failed: {e}"}

        # 3. Generate Dashboard PDF
        try:
            from app.analytics.dashboard_report_pdf_generator import DashboardReportPDFGenerator
            MasterLogger.log_event(
                category="DASHBOARD",
                event_type="DASHBOARD_REPORT_PDF_STARTED",
                message=f"Generating Dashboard PDF report for date {report_date}",
                details={"snapshot_id": snapshot_id, "report_date": report_date}
            )
            pdf_bytes = DashboardReportPDFGenerator.generate_pdf(snapshot)
            MasterLogger.log_event(
                category="DASHBOARD",
                event_type="DASHBOARD_REPORT_PDF_COMPLETED",
                message=f"Dashboard PDF generated successfully ({len(pdf_bytes)} bytes)",
                details={"snapshot_id": snapshot_id, "size_bytes": len(pdf_bytes)}
            )
        except Exception as e:
            logger.error(f"Failed to generate dashboard PDF for Telegram delivery: {e}")
            MasterLogger.log_event(
                category="DASHBOARD",
                event_type="DASHBOARD_REPORT_PDF_FAILED",
                message=f"Dashboard PDF generation failed: {e}",
                severity="ERROR"
            )
            return {"status": "error", "message": f"PDF generation failed: {e}"}

        # 4. Build concise Telegram caption with actual dashboard metrics
        filename = f"Daily_Market_Report_{report_date}.pdf"
        
        m_status = snapshot.get("market_status", {}).get("status_label", "MARKET UPDATE")
        regime_val = snapshot.get("regime", {}).get("composite_regime", "NEUTRAL")
        date_disp = snapshot.get("market_status", {}).get("date_display", report_date)
        gen_time = snapshot.get("market_status", {}).get("ist_time", now.strftime("%H:%M IST"))

        nifty_str = "--"
        bank_nifty_str = "--"
        vix_str = "--"

        for idx in snapshot.get("indian_markets", []):
            sym = idx.get("name")
            pct = idx.get("change_pct")
            if pct is not None:
                sign = "+" if pct > 0 else ""
                val_str = f"{sign}{pct:.2f}%"
                if "NIFTY 50" in sym:
                    nifty_str = val_str
                elif "BANK NIFTY" in sym:
                    bank_nifty_str = val_str
                elif "INDIA VIX" in sym:
                    vix_str = val_str

        caption = (
            f"📊 <b>DAILY MARKET INTELLIGENCE REPORT</b>\n"
            f"📅 {date_disp}\n\n"
            f"• <b>Market Status:</b> {m_status}\n"
            f"• <b>Macro Regime:</b> {regime_val}\n"
            f"• <b>NIFTY 50:</b> {nifty_str}\n"
            f"• <b>BANK NIFTY:</b> {bank_nifty_str}\n"
            f"• <b>INDIA VIX:</b> {vix_str}\n\n"
            f"⏰ <i>Generated: {gen_time}</i>\n"
            f"⚠️ <i>Informational quantitative intelligence — not live positions.</i>"
        )

        # 5. Dispatch via Telegram Document API
        try:
            MasterLogger.log_event(
                category="TELEGRAM",
                event_type="DASHBOARD_REPORT_TELEGRAM_STARTED",
                message=f"Starting Telegram document dispatch for {filename}",
                details={"snapshot_id": snapshot_id, "filename": filename}
            )
            success = send_telegram_document(pdf_bytes, filename, caption=caption)

            # Record in SQLite
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            _ensure_deliveries_table(conn)
            sent_at = datetime.now().isoformat()
            status = "DELIVERED" if success else "FAILED"

            conn.execute("""
                INSERT OR REPLACE INTO dashboard_report_deliveries 
                (report_date, snapshot_id, report_version, sent_at, telegram_message_id, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (report_date, snapshot_id, REPORT_VERSION, sent_at, "delivered" if success else None, status))
            conn.commit()
            conn.close()

            if success:
                MasterLogger.log_event(
                    category="TELEGRAM",
                    event_type="DASHBOARD_REPORT_TELEGRAM_COMPLETED",
                    message=f"Daily Market Intelligence Report delivered to Telegram successfully.",
                    details={"report_date": report_date, "snapshot_id": snapshot_id}
                )
                return {
                    "status": "delivered",
                    "report_date": report_date,
                    "snapshot_id": snapshot_id,
                    "filename": filename,
                    "sent_at": sent_at
                }
            else:
                MasterLogger.log_event(
                    category="TELEGRAM",
                    event_type="DASHBOARD_REPORT_TELEGRAM_FAILED",
                    message="Telegram dispatch returned unsuccessful status (check token/chat configuration).",
                    severity="WARNING"
                )
                return {
                    "status": "delivery_failed",
                    "report_date": report_date,
                    "message": "Telegram API rejected document or credentials unconfigured."
                }
        except Exception as e:
            logger.error(f"Error during Telegram report delivery: {e}")
            MasterLogger.log_event(
                category="TELEGRAM",
                event_type="DASHBOARD_REPORT_TELEGRAM_FAILED",
                message=f"Telegram delivery exception: {e}",
                severity="ERROR"
            )
            return {"status": "error", "message": str(e)}


def execute_daily_dashboard_telegram_job():
    """Entrypoint function called by APScheduler for scheduled daily delivery."""
    logger.info("⏰ [APScheduler] Triggering scheduled Daily Dashboard Report Telegram delivery...")
    res = DashboardTelegramScheduler.send_daily_report(force=False)
    logger.info(f"Daily Dashboard Report delivery result: {res}")
    return res

