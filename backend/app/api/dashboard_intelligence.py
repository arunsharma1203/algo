from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
import io
import logging
from datetime import datetime
from typing import Optional

from app.analytics.dashboard_intelligence_service import DashboardIntelligenceService
from app.analytics.dashboard_report_pdf_generator import DashboardReportPDFGenerator
from app.analytics.dashboard_telegram_scheduler import DashboardTelegramScheduler
from app.analytics.master_logger import MasterLogger

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/intelligence")
def get_dashboard_intelligence(force_refresh: bool = Query(False, description="Force refresh the cached snapshot")):
    """
    Returns the unified, normalized DashboardSnapshot JSON.
    Single Source of Truth for the React Dashboard and PDF Report.
    """
    try:
        snapshot = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=force_refresh)
        return snapshot
    except Exception as e:
        logger.error(f"Failed to generate dashboard snapshot: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve dashboard intelligence: {str(e)}")


@router.get("/report/pdf")
def download_dashboard_report_pdf(force_refresh: bool = Query(False, description="Force re-generation of today's report")):
    """
    Generates and streams the publication-grade Daily Market Intelligence Report PDF
    containing the CURRENT dashboard state.
    """
    try:
        snapshot = DashboardIntelligenceService.get_dashboard_snapshot(force_refresh=force_refresh)
        report_date = snapshot.get("report_date", datetime.now().strftime("%Y-%m-%d"))
        snapshot_id = snapshot.get("snapshot_id", "unknown")

        MasterLogger.log_event(
            category="DASHBOARD",
            event_type="DASHBOARD_REPORT_DOWNLOAD",
            message=f"Browser download initiated for Daily Market Report {report_date}",
            details={"snapshot_id": snapshot_id, "report_date": report_date}
        )

        pdf_bytes = DashboardReportPDFGenerator.generate_pdf(snapshot)
        filename = f"Daily_Market_Report_{report_date}.pdf"

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Access-Control-Expose-Headers": "Content-Disposition",
                "Cache-Control": "no-cache, no-store, must-revalidate"
            }
        )
    except Exception as e:
        logger.error(f"Failed to stream dashboard PDF: {e}")
        MasterLogger.log_event(
            category="DASHBOARD",
            event_type="DASHBOARD_REPORT_PDF_FAILED",
            message=f"PDF generation failed on download request: {e}",
            severity="ERROR"
        )
        raise HTTPException(status_code=500, detail=f"PDF report generation failed: {str(e)}")


@router.post("/report/telegram/send")
def trigger_telegram_daily_report(force: bool = Query(False, description="Force delivery even if already sent today")):
    """
    Dispatches the Daily Market Report PDF to the configured Telegram destination.
    Enforces deterministic deduplication: won't duplicate same-day reports unless force=True.
    """
    try:
        result = DashboardTelegramScheduler.send_daily_report(force=force)
        return result
    except Exception as e:
        logger.error(f"Error triggering Telegram delivery: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/report/status")
def get_report_delivery_status(date: Optional[str] = Query(None, description="Report date in YYYY-MM-DD format")):
    """
    Queries SQLite to determine whether today's report has already been delivered to Telegram.
    """
    report_date = date or datetime.now().strftime("%Y-%m-%d")
    status_info = DashboardTelegramScheduler.get_delivery_status(report_date)
    return {
        "report_date": report_date,
        "delivery": status_info or {"status": "NOT_DELIVERED", "message": "Report has not been sent to Telegram today."}
    }

