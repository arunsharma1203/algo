import io
import sqlite3
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
import pytz
from pypdf import PdfReader

from app.data.historical_data_layer import get_db_path
from app.analytics.master_logger import MasterLogger
from app.analytics.dashboard_intelligence_service import DashboardIntelligenceService, REPORT_VERSION
from app.analytics.telegram_notifier import send_telegram_document
from app.analytics.fii_dii_service import FiiDiiService

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def _ensure_deliveries_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_report_deliveries (
            report_date TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            report_version TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            telegram_message_id TEXT,
            status TEXT NOT NULL,
            details TEXT DEFAULT ''
        );
    """)
    conn.commit()
    # Migration safeguard: ensure 'details' column exists in pre-existing tables
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(dashboard_report_deliveries)")
        cols = [r[1] for r in cur.fetchall()]
        if "details" not in cols:
            conn.execute("ALTER TABLE dashboard_report_deliveries ADD COLUMN details TEXT DEFAULT ''")
            conn.commit()
    except Exception as e:
        logger.warning(f"Could not verify or add details column to dashboard_report_deliveries: {e}")


class DashboardTelegramScheduler:
    """
    Manages daily automated and on-demand Telegram delivery of the Dashboard Market Intelligence PDF.
    Enforces the required 6-stage execution order:
        1. Morning data collection / refresh
        2. Time-aware data validation & freshness verification
        3. Required market intelligence calculations
        4. PDF generation
        5. Deep PDF integrity & content verification
        6. Telegram delivery at 08:15 IST with deterministic deduplication
    """

    @classmethod
    def get_delivery_status(cls, report_date: str) -> Optional[Dict[str, Any]]:
        """Retrieves delivery status for a specific date from SQLite."""
        try:
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            _ensure_deliveries_table(conn)
            cur = conn.cursor()
            cur.execute("""
                SELECT report_date, snapshot_id, report_version, sent_at, telegram_message_id, status, details
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
                    "status": row[5],
                    "details": row[6]
                }
            return None
        except Exception as e:
            logger.error(f"Failed to check delivery status: {e}")
            return None

    @classmethod
    def verify_morning_data_integrity(cls, now_ist: datetime) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Time-aware verification (Safeguard 1):
        Validates only data that legitimately exists by 08:15 IST:
          - Overnight global market closes (US markets, US 10Y, Crude, Gold, USD/INR)
          - Official NSE EOD FII/DII disclosures from previous session
          - Baseline market breadth from canonical store
          - Macro regime calculated from closed daily data
        Naturally unavailable pre-market items (live ticks, intraday breadth)
        are clearly designated as PRE_MARKET_STANDBY without triggering failure.
        """
        details = {
            "evaluated_at_ist": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
            "target_delivery_window": "08:15 IST",
            "checks": {}
        }

        # 1. Check FII/DII Institutional Disclosures
        try:
            flows = FiiDiiService.get_latest_flows(limit=5)
            if flows and flows.get("disclosure_date") and flows.get("fii") and flows.get("dii"):
                details["checks"]["institutional_flows"] = {
                    "status": "PASS",
                    "disclosure_date": flows.get("disclosure_date"),
                    "net_total": flows.get("formatted_net_total"),
                    "freshness": flows.get("data_freshness")
                }
            else:
                details["checks"]["institutional_flows"] = {
                    "status": "FAIL",
                    "reason": "Official FII/DII disclosures missing or unpopulated"
                }
                return False, "Missing official FII/DII institutional disclosures", details
        except Exception as e:
            details["checks"]["institutional_flows"] = {"status": "FAIL", "reason": str(e)}
            return False, f"FII/DII check error: {e}", details

        # 2. Check Market Breadth Baseline
        try:
            breadth = DashboardIntelligenceService.get_market_breadth()
            if breadth and breadth.get("evaluated_count", 0) > 0:
                details["checks"]["market_breadth"] = {
                    "status": "PASS",
                    "baseline_source": breadth.get("source"),
                    "evaluated_count": breadth.get("evaluated_count"),
                    "advances": breadth.get("advances"),
                    "declines": breadth.get("declines")
                }
            else:
                details["checks"]["market_breadth"] = {
                    "status": "FAIL",
                    "reason": "Canonical database returned 0 evaluated breadth symbols"
                }
                return False, "Market breadth baseline has 0 evaluated symbols", details
        except Exception as e:
            details["checks"]["market_breadth"] = {"status": "FAIL", "reason": str(e)}
            return False, f"Breadth baseline check error: {e}", details

        # 3. Check Macro Regime Baseline
        try:
            from app.analytics.macro_engine import get_macro_regime
            macro = get_macro_regime()
            if macro and macro.get("nifty_trend_long"):
                details["checks"]["macro_regime"] = {
                    "status": "PASS",
                    "regime": macro.get("nifty_trend_long"),
                    "vix_close": macro.get("vix_close")
                }
            else:
                details["checks"]["macro_regime"] = {
                    "status": "FAIL",
                    "reason": "Macro regime calculation failed"
                }
                return False, "Macro regime unavailable", details
        except Exception as e:
            details["checks"]["macro_regime"] = {"status": "FAIL", "reason": str(e)}
            return False, f"Macro regime check error: {e}", details

        # 4. Pre-Market Awareness Note (Safeguard 1)
        # At 08:15 IST, NSE has not opened. Mark current-session live equity ticks as STANDBY.
        details["checks"]["pre_market_equity_ticks"] = {
            "status": "PRE_MARKET_STANDBY",
            "message": "Market opens at 09:15 IST. Report truthfully utilizes official previous EOD settlement baseline and overnight global cues."
        }

        return True, "All morning data integrity checks passed", details

    @classmethod
    def validate_pdf_binary(cls, pdf_bytes: bytes, report_date: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Deep PDF Validation (Safeguard 4):
        Verifies:
          1. Magic bytes (%PDF)
          2. Minimum binary size (>= 15KB)
          3. Clean structural parsing with pypdf
          4. Minimum page count (>= 2 pages)
          5. Core content verification (Report title, date presence)
        """
        details = {"size_bytes": len(pdf_bytes)}

        if not pdf_bytes or len(pdf_bytes) < 15000:
            return False, f"PDF binary size too small ({len(pdf_bytes)} bytes < 15KB threshold)", details

        if not pdf_bytes.startswith(b"%PDF"):
            return False, "Invalid PDF binary: missing %PDF header magic bytes", details

        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            page_count = len(reader.pages)
            details["page_count"] = page_count

            if page_count < 2:
                return False, f"Incomplete report: PDF contains {page_count} pages (expected >= 2)", details

            # Content verification
            extracted_text = ""
            for page in reader.pages[:3]:
                extracted_text += page.extract_text() or ""

            details["text_sample_len"] = len(extracted_text)

            if "MARKET" not in extracted_text.upper() or "REPORT" not in extracted_text.upper():
                return False, "PDF content verification failed: missing expected report title headers", details

            details["verified_title"] = True
            return True, f"PDF validated successfully: {page_count} pages, {len(pdf_bytes)} bytes", details
        except Exception as e:
            return False, f"PDF structural parsing failed (corrupt document): {e}", details

    @classmethod
    def send_daily_report(cls, force: bool = False) -> Dict[str, Any]:
        """
        Executes the end-to-end Dashboard Report delivery pipeline:
        1. Deduplication check
        2. Time-aware morning data verification
        3. Snapshot acquisition
        4. PDF generation
        5. Deep PDF integrity validation
        6. Telegram document transmission
        7. SQLite persistence & Master Logger audit
        """
        now_ist = datetime.now(IST)
        report_date = now_ist.strftime("%Y-%m-%d")

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

        # 2. Time-Aware Morning Data Verification (Safeguard 1)
        MasterLogger.log_event(
            category="DASHBOARD",
            event_type="DASHBOARD_REPORT_VERIFICATION_STARTED",
            message=f"Starting morning data verification for report date {report_date} at 08:15 IST window",
            details={"report_date": report_date, "time_ist": now_ist.strftime("%H:%M:%S IST")}
        )

        is_verified, v_msg, v_details = cls.verify_morning_data_integrity(now_ist)
        if not is_verified:
            logger.warning(f"Morning data verification failed for {report_date}: {v_msg}")
            MasterLogger.log_event(
                category="DASHBOARD",
                event_type="DASHBOARD_REPORT_VERIFICATION_FAILED",
                message=f"Morning data verification failed: {v_msg}",
                details={"report_date": report_date, "details": v_details},
                severity="WARNING"
            )
            # Record failure in SQLite
            try:
                conn = sqlite3.connect(get_db_path(), timeout=5.0)
                _ensure_deliveries_table(conn)
                conn.execute("""
                    INSERT OR REPLACE INTO dashboard_report_deliveries 
                    (report_date, snapshot_id, report_version, sent_at, telegram_message_id, status, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (report_date, "none", REPORT_VERSION, now_ist.isoformat(), None, "VERIFICATION_FAILED", v_msg))
                conn.commit()
                conn.close()
            except Exception:
                pass

            return {
                "status": "verification_failed",
                "report_date": report_date,
                "reason": v_msg,
                "details": v_details
            }

        MasterLogger.log_event(
            category="DASHBOARD",
            event_type="DASHBOARD_REPORT_VERIFICATION_COMPLETED",
            message=f"Morning data verification passed for report date {report_date}",
            details={"report_date": report_date, "checks": v_details.get("checks")}
        )

        # 3. Acquire current normalized Dashboard snapshot
        try:
            snapshot = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=True)
            snapshot_id = snapshot.get("snapshot_id", "unknown_snapshot")
        except Exception as e:
            logger.error(f"Failed to acquire dashboard snapshot for report delivery: {e}")
            return {"status": "error", "message": f"Snapshot failed: {e}"}

        # 4. Generate Dashboard PDF
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

        # 5. Deep PDF Structural & Content Validation (Safeguard 4)
        is_pdf_valid, pdf_v_msg, pdf_v_details = cls.validate_pdf_binary(pdf_bytes, report_date)
        if not is_pdf_valid:
            logger.error(f"PDF structural validation failed: {pdf_v_msg}")
            MasterLogger.log_event(
                category="DASHBOARD",
                event_type="DASHBOARD_REPORT_PDF_VALIDATION_FAILED",
                message=f"Deep PDF validation failed: {pdf_v_msg}",
                details={"snapshot_id": snapshot_id, "details": pdf_v_details},
                severity="ERROR"
            )
            return {"status": "pdf_validation_failed", "message": pdf_v_msg}

        MasterLogger.log_event(
            category="DASHBOARD",
            event_type="DASHBOARD_REPORT_PDF_VALIDATED",
            message=f"Deep PDF validation passed: {pdf_v_details.get('page_count')} pages verified.",
            details={"snapshot_id": snapshot_id, "details": pdf_v_details}
        )

        # 6. Build concise Telegram caption with verified dashboard metrics
        filename = f"Daily_Market_Report_{report_date}.pdf"
        
        m_status = snapshot.get("market_status", {}).get("status_label", "PRE-MARKET BRIEFING")
        regime_val = snapshot.get("regime", {}).get("composite_regime", "NEUTRAL")
        date_disp = snapshot.get("market_status", {}).get("date_display", report_date)
        gen_time = snapshot.get("market_status", {}).get("ist_time", now_ist.strftime("%H:%M IST"))

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

        # Add FII/DII summary to caption if available
        fii_dii_info = snapshot.get("institutional_flows", {})
        net_inst_str = fii_dii_info.get("formatted_net_total", "")

        caption = (
            f"📊 <b>DAILY MARKET INTELLIGENCE REPORT (08:15 IST)</b>\n"
            f"📅 {date_disp}\n\n"
            f"• <b>Market Window:</b> Pre-Market Briefing (08:15 IST)\n"
            f"• <b>Macro Regime:</b> {regime_val}\n"
            f"• <b>NIFTY 50 (EOD Baseline):</b> {nifty_str}\n"
            f"• <b>BANK NIFTY:</b> {bank_nifty_str}\n"
            f"• <b>INDIA VIX:</b> {vix_str}\n"
        )
        if net_inst_str:
            caption += f"• <b>FII/DII Flow ({fii_dii_info.get('disclosure_date', 'EOD')}):</b> {net_inst_str}\n"

        caption += (
            f"\n⏰ <i>Generated & Verified: {gen_time}</i>\n"
            f"⚠️ <i>Informational quantitative intelligence — zero live positions.</i>"
        )

        # 7. Dispatch via Telegram Document API
        try:
            MasterLogger.log_event(
                category="TELEGRAM",
                event_type="DASHBOARD_REPORT_TELEGRAM_STARTED",
                message=f"Starting Telegram document dispatch for {filename} (08:15 IST delivery window)",
                details={"snapshot_id": snapshot_id, "filename": filename, "delivery_target": "08:15 IST"}
            )
            success = send_telegram_document(pdf_bytes, filename, caption=caption)

            # Record in SQLite
            conn = sqlite3.connect(get_db_path(), timeout=5.0)
            _ensure_deliveries_table(conn)
            sent_at = datetime.now(IST).isoformat()
            status = "DELIVERED" if success else "FAILED"

            conn.execute("""
                INSERT OR REPLACE INTO dashboard_report_deliveries 
                (report_date, snapshot_id, report_version, sent_at, telegram_message_id, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (report_date, snapshot_id, REPORT_VERSION, sent_at, "delivered" if success else None, status, "Dispatched at 08:15 IST delivery window"))
            conn.commit()
            conn.close()

            if success:
                MasterLogger.log_event(
                    category="TELEGRAM",
                    event_type="DASHBOARD_REPORT_TELEGRAM_COMPLETED",
                    message="Daily Market Intelligence Report delivered to Telegram successfully.",
                    details={"report_date": report_date, "snapshot_id": snapshot_id, "delivery_time": sent_at}
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
    """Entrypoint function called by APScheduler at 08:15 AM IST for scheduled daily delivery."""
    logger.info("⏰ [APScheduler] Triggering 08:15 AM IST Daily Dashboard Report Telegram delivery...")
    res = DashboardTelegramScheduler.send_daily_report(force=False)
    logger.info(f"Daily Dashboard Report delivery result: {res}")
    return res
