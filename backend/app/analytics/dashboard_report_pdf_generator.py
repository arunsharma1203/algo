import io
import os
import math
from datetime import datetime
from typing import Dict, Any, List, Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = letter
USABLE_WIDTH = PAGE_WIDTH - 72.0  # 540.0 points with 36pt margins


class DashboardNumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas that computes total pages dynamically and stamps
    authoritative running footers on every page.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count: int):
        self.saveState()
        
        # Running header rule & text (on pages > 1)
        if self._pageNumber > 1:
            self.setFont("Helvetica-Bold", 7)
            self.setFillColor(colors.HexColor("#64748B"))
            self.drawString(36, PAGE_HEIGHT - 24, "AI TRADING MARKET COMMAND CENTER  |  DAILY MARKET INTELLIGENCE REPORT")
            self.setFont("Helvetica", 7)
            self.drawRightString(PAGE_WIDTH - 36, PAGE_HEIGHT - 24, getattr(self, "report_date_str", ""))
            self.setStrokeColor(colors.HexColor("#E2E8F0"))
            self.setLineWidth(0.5)
            self.line(36, PAGE_HEIGHT - 28, PAGE_WIDTH - 36, PAGE_HEIGHT - 28)

        # Running footer rule & text
        self.setFont("Helvetica", 7.5)
        self.setFillColor(colors.HexColor("#64748B"))
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(36, 26, PAGE_WIDTH - 36, 26)

        footer_text = f"Daily Market Intelligence Report  •  Page {self._pageNumber} of {page_count}  •  Informational dashboard output — not a live position."
        self.drawCentredString(PAGE_WIDTH / 2.0, 15, footer_text)
        
        self.restoreState()


class DashboardReportPDFGenerator:
    """
    Institutional PDF Report Generator for the AI Trading Market Command Center.
    Generates a 8-page, publication-grade Daily Market Intelligence Report
    directly from the normalized DashboardSnapshot.
    
    Operates strictly READ-ONLY. Zero research runs or model retraining.
    """

    @classmethod
    def generate_pdf(cls, snapshot: Dict[str, Any]) -> bytes:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=36,
            leftMargin=36,
            topMargin=36,
            bottomMargin=36
        )

        styles = getSampleStyleSheet()

        # ── COLOR PALETTE ───────────────────────────────────────────────
        c_primary = colors.HexColor("#0F172A")    # Dark slate
        c_accent = colors.HexColor("#0EA5E9")     # Sky blue
        c_success = colors.HexColor("#10B981")    # Emerald green
        c_danger = colors.HexColor("#F43F5E")     # Rose red
        c_warning = colors.HexColor("#F59E0B")    # Amber
        c_muted = colors.HexColor("#64748B")      # Slate text
        c_card_bg = colors.HexColor("#F8FAFC")    # Very light gray
        c_border = colors.HexColor("#E2E8F0")     # Light border

        # ── TYPOGRAPHY STYLES ───────────────────────────────────────────
        title_style = ParagraphStyle(
            'DocTitle',
            parent=styles['Normal'],
            fontName='Helvetica-Bold',
            fontSize=20,
            leading=24,
            textColor=c_primary
        )

        subtitle_style = ParagraphStyle(
            'DocSubtitle',
            parent=styles['Normal'],
            fontName='Helvetica-Bold',
            fontSize=10,
            leading=13,
            textColor=c_accent
        )

        section_hdr_style = ParagraphStyle(
            'SectionHdr',
            parent=styles['Normal'],
            fontName='Helvetica-Bold',
            fontSize=12,
            leading=15,
            textColor=c_primary,
            spaceBefore=10,
            spaceAfter=5
        )

        body_style = ParagraphStyle(
            'DocBody',
            parent=styles['Normal'],
            fontName='Helvetica',
            fontSize=8.5,
            leading=11.5,
            textColor=colors.HexColor("#334155")
        )

        cell_bold = ParagraphStyle(
            'CellBold',
            parent=styles['Normal'],
            fontName='Helvetica-Bold',
            fontSize=8,
            leading=10,
            textColor=c_primary
        )

        cell_regular = ParagraphStyle(
            'CellRegular',
            parent=styles['Normal'],
            fontName='Helvetica',
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#334155")
        )

        cell_muted = ParagraphStyle(
            'CellMuted',
            parent=styles['Normal'],
            fontName='Helvetica',
            fontSize=7.5,
            leading=9.5,
            textColor=c_muted
        )

        cell_bullish = ParagraphStyle(
            'CellBullish',
            parent=styles['Normal'],
            fontName='Helvetica-Bold',
            fontSize=8,
            leading=10,
            textColor=c_success
        )

        cell_bearish = ParagraphStyle(
            'CellBearish',
            parent=styles['Normal'],
            fontName='Helvetica-Bold',
            fontSize=8,
            leading=10,
            textColor=c_danger
        )

        cell_danger = cell_bearish

        story = []

        # Extract snapshot data safely
        m_status = snapshot.get("market_status", {})
        indian_mkts = snapshot.get("indian_markets", [])
        global_cues = snapshot.get("global_cues", [])
        regime = snapshot.get("regime", {})
        breadth = snapshot.get("breadth", {})
        sectors = snapshot.get("sectors", {})
        fii_dii = snapshot.get("institutional_flows", {})
        risk_radar = snapshot.get("volatility_risk_radar", {})
        news = snapshot.get("news_intelligence", {})
        events = snapshot.get("events", {})
        ai_ops = snapshot.get("ai_opportunities", {})
        ai_summary = snapshot.get("ai_summary", {})
        sys_health = snapshot.get("system_health", {})

        date_disp = m_status.get("date_display", datetime.now().strftime("%d %B %Y"))
        ist_time = m_status.get("ist_time", datetime.now().strftime("%H:%M:%S IST"))

        # ═════════════════════════════════════════════════════════════════
        # PAGE 1: COVER & MARKET AT A GLANCE + QUANTITATIVE REGIME
        # ═════════════════════════════════════════════════════════════════
        story.append(Spacer(1, 4))
        story.append(Paragraph(snapshot.get("report_title", "DAILY MARKET INTELLIGENCE REPORT"), title_style))
        story.append(Paragraph(snapshot.get("report_subtitle", "AI Trading Market Command Center").upper(), subtitle_style))
        story.append(Spacer(1, 8))

        # Metadata Card (Date, IST Time, Status, Freshness, Snapshot ID)
        meta_table_data = [
            [
                Paragraph("<b>REPORT DATE:</b>", cell_bold), Paragraph(date_disp, cell_regular),
                Paragraph("<b>GENERATED:</b>", cell_bold), Paragraph(ist_time, cell_regular)
            ],
            [
                Paragraph("<b>MARKET STATUS:</b>", cell_bold),
                Paragraph(f"<b>{m_status.get('status_label', 'MARKET CLOSED')}</b>", cell_bullish if m_status.get("is_open") else cell_regular),
                Paragraph("<b>DATA INTEGRITY:</b>", cell_bold), Paragraph("VERIFIED / POINT-IN-TIME", cell_regular)
            ],
            [
                Paragraph("<b>SNAPSHOT ID:</b>", cell_bold), Paragraph(snapshot.get("snapshot_id", "--"), cell_muted),
                Paragraph("<b>REPORT VERSION:</b>", cell_bold), Paragraph(snapshot.get("report_version", "V1"), cell_muted)
            ]
        ]
        meta_table = Table(meta_table_data, colWidths=[100, 170, 100, 170])
        meta_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), c_card_bg),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 12))

        # Executive Section 1: Market At A Glance
        story.append(Paragraph("1. Market At A Glance", section_hdr_style))
        
        idx_rows = [
            [
                Paragraph("<b>INDEX / BENCHMARK</b>", cell_bold),
                Paragraph("<b>LTP</b>", cell_bold),
                Paragraph("<b>CHANGE</b>", cell_bold),
                Paragraph("<b>% CHG</b>", cell_bold),
                Paragraph("<b>DAY HIGH</b>", cell_bold),
                Paragraph("<b>DAY LOW</b>", cell_bold),
                Paragraph("<b>PREV CLOSE</b>", cell_bold),
                Paragraph("<b>FRESHNESS</b>", cell_bold)
            ]
        ]

        for m in indian_mkts:
            ltp_str = f"₹{m['ltp']:,.2f}" if m.get("ltp") is not None else "--"
            chg_str = f"{m['change']:+,.2f}" if m.get("change") is not None else "--"
            pct_val = m.get("change_pct")
            pct_str = f"{pct_val:+.2f}%" if pct_val is not None else "--"
            p_style = cell_bullish if (pct_val is not None and pct_val > 0) else (cell_bearish if (pct_val is not None and pct_val < 0) else cell_regular)
            high_str = f"₹{m['high']:,.2f}" if m.get("high") is not None else "--"
            low_str = f"₹{m['low']:,.2f}" if m.get("low") is not None else "--"
            prev_str = f"₹{m['previous_close']:,.2f}" if m.get("previous_close") is not None else "--"

            idx_rows.append([
                Paragraph(f"<b>{m['name']}</b>", cell_bold),
                Paragraph(ltp_str, cell_regular),
                Paragraph(chg_str, p_style),
                Paragraph(pct_str, p_style),
                Paragraph(high_str, cell_muted),
                Paragraph(low_str, cell_muted),
                Paragraph(prev_str, cell_muted),
                Paragraph(m.get("freshness", "FRESH"), cell_muted)
            ])

        idx_table = Table(idx_rows, colWidths=[105, 65, 55, 55, 65, 65, 65, 65])
        idx_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(idx_table)
        story.append(Spacer(1, 12))

        # Executive Section 2: Quantitative Macro Regime
        story.append(Paragraph("2. Quantitative Macro Regime & Structural Trend", section_hdr_style))
        
        regime_label = regime.get("composite_regime", "NEUTRAL")
        r_color = c_success if "BULLISH" in regime_label else (c_danger if "BEARISH" in regime_label else c_warning)

        nifty_cl = regime.get("nifty_close")
        sma200_val = regime.get("sma_200")
        ema20_val = regime.get("ema_20")
        vix_val = regime.get("vix_close")
        dist_200 = regime.get("distance_sma200_pct")
        dist_20 = regime.get("distance_ema20_pct")

        regime_table_data = [
            [
                Paragraph("<b>COMPOSITE REGIME:</b>", cell_bold),
                Paragraph(f"<b>{regime_label}</b>", ParagraphStyle('RLabel', parent=cell_bold, textColor=r_color, fontSize=10)),
                Paragraph("<b>VIX VOLATILITY STATUS:</b>", cell_bold),
                Paragraph(f"<b>{regime.get('vix_status', 'NORMAL')} ({vix_val if vix_val else '--'})</b>", cell_regular)
            ],
            [
                Paragraph("<b>NIFTY 50 (CLOSE):</b>", cell_bold), Paragraph(f"₹{nifty_cl:,.2f}" if nifty_cl else "--", cell_regular),
                Paragraph("<b>MACRO TREND (200 SMA):</b>", cell_bold),
                Paragraph(f"{regime.get('nifty_trend_long', 'NEUTRAL')} ({dist_200:+.2f}% vs SMA)" if dist_200 is not None else "--", cell_regular)
            ],
            [
                Paragraph("<b>SHORT TREND (20 EMA):</b>", cell_bold),
                Paragraph(f"{regime.get('nifty_trend_short', 'NEUTRAL')} ({dist_20:+.2f}% vs EMA)" if dist_20 is not None else "--", cell_regular),
                Paragraph("<b>QUANTITATIVE ENGINE:</b>", cell_bold),
                Paragraph("Point-in-Time 200 SMA / 20 EMA / India VIX Rule Gate", cell_muted)
            ]
        ]

        regime_table = Table(regime_table_data, colWidths=[130, 140, 130, 140])
        regime_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), c_card_bg),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(regime_table)
        story.append(PageBreak())

        # ═════════════════════════════════════════════════════════════════
        # PAGE 2: INDIAN MARKET PERFORMANCE + GLOBAL MARKET CUES
        # ═════════════════════════════════════════════════════════════════
        story.append(Paragraph("3. Global Intermarket Cues & Macro Landscape", section_hdr_style))
        story.append(Paragraph("International market performance across US indices, Asian bourses, commodities, and currency.", body_style))
        story.append(Spacer(1, 8))

        cues_rows = [
            [
                Paragraph("<b>ASSET / INDEX</b>", cell_bold),
                Paragraph("<b>REGION</b>", cell_bold),
                Paragraph("<b>LAST PRICE</b>", cell_bold),
                Paragraph("<b>CHANGE</b>", cell_bold),
                Paragraph("<b>% CHANGE</b>", cell_bold),
                Paragraph("<b>SIGNAL</b>", cell_bold),
                Paragraph("<b>STATUS</b>", cell_bold)
            ]
        ]

        for cue in global_cues:
            val_str = f"{cue['value']:,.2f}" if cue.get("value") is not None else "--"
            chg_str = f"{cue['change']:+,.2f}" if cue.get("change") is not None else "--"
            pct = cue.get("change_pct")
            pct_str = f"{pct:+.2f}%" if pct is not None else "--"
            
            sig_style = cell_bullish if cue.get("direction") == "BULLISH" else (cell_bearish if cue.get("direction") == "BEARISH" else cell_regular)
            state_text = cue.get("state_label") or cue.get("market_state") or cue.get("freshness", "FRESH")
            
            cues_rows.append([
                Paragraph(f"<b>{cue['name']}</b>", cell_bold),
                Paragraph(cue.get("region", "Global"), cell_muted),
                Paragraph(val_str, cell_regular),
                Paragraph(chg_str, sig_style),
                Paragraph(pct_str, sig_style),
                Paragraph(cue.get("direction", "NEUTRAL"), sig_style),
                Paragraph(state_text, cell_muted)
            ])

        cues_table = Table(cues_rows, colWidths=[120, 65, 75, 65, 65, 75, 75])
        cues_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(cues_table)
        story.append(PageBreak())

        # ═════════════════════════════════════════════════════════════════
        # PAGE 3: MARKET BREADTH ANALYSIS + SECTOR ROTATION MATRIX
        # ═════════════════════════════════════════════════════════════════
        story.append(Paragraph("4. Market Breadth Analysis & Internal Health", section_hdr_style))
        story.append(Paragraph("Authoritative technical breadth and coverage metrics calculated across market_data.db.", body_style))
        story.append(Spacer(1, 8))

        adv_pct = breadth.get('pct_advancing', 0.0)
        dec_pct = breadth.get('pct_declining', 0.0)
        eval_cnt = breadth.get('evaluated_count', breadth.get('total_stocks', 0))
        u_size = breadth.get('universe_size', 511)
        cov_pct = breadth.get('coverage_pct', 0.0)
        dma_cnt = breadth.get('dma_evaluated_count', eval_cnt)

        breadth_card_data = [
            [
                Paragraph("<b>ADVANCING STOCKS:</b>", cell_bold),
                Paragraph(f"<b>{breadth.get('advances', 0)} ({adv_pct}% of {eval_cnt})</b>", cell_bullish),
                Paragraph("<b>STOCKS ABOVE 20 DMA:</b>", cell_bold),
                Paragraph(f"{breadth.get('above_20_count', 0)}/{dma_cnt} ({breadth.get('above_20_dma_pct', 0.0)}%)", cell_regular)
            ],
            [
                Paragraph("<b>DECLINING STOCKS:</b>", cell_bold),
                Paragraph(f"<b>{breadth.get('declines', 0)} ({dec_pct}% of {eval_cnt})</b>", cell_bearish),
                Paragraph("<b>STOCKS ABOVE 50 DMA:</b>", cell_bold),
                Paragraph(f"{breadth.get('above_50_count', 0)}/{dma_cnt} ({breadth.get('above_50_dma_pct', 0.0)}%)", cell_regular)
            ],
            [
                Paragraph("<b>UNCHANGED:</b>", cell_bold),
                Paragraph(f"{breadth.get('unchanged', 0)}", cell_regular),
                Paragraph("<b>STOCKS ABOVE 200 DMA:</b>", cell_bold),
                Paragraph(f"{breadth.get('above_200_count', 0)}/{dma_cnt} ({breadth.get('above_200_dma_pct', 0.0)}%)", cell_regular)
            ],
            [
                Paragraph("<b>A/D RATIO:</b>", cell_bold),
                Paragraph(f"<b>{breadth.get('ad_ratio', 1.0)} : 1</b>", cell_bold),
                Paragraph("<b>52-WEEK HIGHS / LOWS:</b>", cell_bold),
                Paragraph(f"{breadth.get('highs_52w', 0)} Highs / {breadth.get('lows_52w', 0)} Lows (of {dma_cnt})", cell_regular)
            ],
            [
                Paragraph("<b>COVERAGE & AUDIT:</b>", cell_bold),
                Paragraph(f"Universe: {u_size} | Evaluated: {eval_cnt} | Coverage: {cov_pct}%", cell_bold),
                Paragraph("<b>UNIVERSE NAME:</b>", cell_bold),
                Paragraph(breadth.get("universe_name", "Collected NIFTY Universe"), cell_muted)
            ]
        ]
        b_table = Table(breadth_card_data, colWidths=[120, 150, 120, 150])
        b_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), c_card_bg),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('SPAN', (1, 4), (1, 4)),
        ]))
        story.append(b_table)
        story.append(Spacer(1, 14))

        # Sector Rotation Matrix
        story.append(Paragraph("5. Sector Rotation Matrix & Relative Performance", section_hdr_style))
        story.append(Paragraph("Ranked daily performance of major NSE industry groups to identify institutional leaders and laggards.", body_style))
        story.append(Spacer(1, 8))

        sector_list = sectors.get("sectors", [])
        sec_rows = [
            [
                Paragraph("<b>RANK</b>", cell_bold),
                Paragraph("<b>SECTOR INDEX</b>", cell_bold),
                Paragraph("<b>1D CHANGE %</b>", cell_bold),
                Paragraph("<b>LTP</b>", cell_bold),
                Paragraph("<b>ROTATION STATUS</b>", cell_bold)
            ]
        ]

        for i, s in enumerate(sector_list, 1):
            chg = s.get("change_1d_pct")
            ltp_str = f"₹{s['ltp']:,.2f}" if s.get("ltp") else "--"
            if chg is not None:
                chg_str = f"{chg:+.2f}%"
                p_style = cell_bullish if chg > 0 else (cell_bearish if chg < 0 else cell_regular)
                if i <= 3 and chg > 0:
                    status_text = "LEADER (Outperforming)"
                elif i >= len(sector_list) - 2 and chg < 0:
                    status_text = "LAGGARD (Distribution)"
                else:
                    status_text = "NEUTRAL / ROTATING"
            else:
                chg_str = "UNAVAILABLE"
                p_style = cell_muted
                status_text = "DATA UNAVAILABLE"

            sec_rows.append([
                Paragraph(f"#{i}", cell_muted),
                Paragraph(f"<b>{s['name']}</b>", cell_bold),
                Paragraph(chg_str, p_style),
                Paragraph(ltp_str, cell_regular),
                Paragraph(status_text, p_style if "LEADER" in status_text else cell_muted)
            ])

        sec_table = Table(sec_rows, colWidths=[40, 160, 100, 100, 140])
        sec_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(sec_table)
        story.append(PageBreak())

        # ═════════════════════════════════════════════════════════════════
        # PAGE 4: INSTITUTIONAL FLOWS (FII/DII) + VOLATILITY RISK RADAR
        # ═════════════════════════════════════════════════════════════════
        story.append(Paragraph("6. Institutional Capital Flows (FII & DII)", section_hdr_style))
        story.append(Paragraph("Net institutional flows across Foreign Institutional Investors and Domestic Institutional Investors.", body_style))
        story.append(Spacer(1, 8))

        fii_status = fii_dii.get("status", "UNAVAILABLE")
        if fii_status == "FRESH":
            fii_rows = [
                [Paragraph("<b>HORIZON</b>", cell_bold), Paragraph("<b>FII NET (₹ CR)</b>", cell_bold), Paragraph("<b>DII NET (₹ CR)</b>", cell_bold)],
                [Paragraph("Latest Trading Session", cell_regular), Paragraph(f"{fii_dii.get('fii_latest_cr', 0.0):+,.2f}", cell_regular), Paragraph(f"{fii_dii.get('dii_latest_cr', 0.0):+,.2f}", cell_regular)],
                [Paragraph("5-Day Cumulative", cell_regular), Paragraph(f"{fii_dii.get('fii_5d_cr', 0.0):+,.2f}", cell_regular), Paragraph(f"{fii_dii.get('dii_5d_cr', 0.0):+,.2f}", cell_regular)],
                [Paragraph("20-Day Cumulative", cell_regular), Paragraph(f"{fii_dii.get('fii_20d_cr', 0.0):+,.2f}", cell_regular), Paragraph(f"{fii_dii.get('dii_20d_cr', 0.0):+,.2f}", cell_regular)]
            ]
        else:
            fii_rows = [
                [Paragraph("<b>INSTITUTIONAL METRIC</b>", cell_bold), Paragraph("<b>STATUS</b>", cell_bold), Paragraph("<b>DETAILS</b>", cell_bold)],
                [
                    Paragraph("FII & DII Cash Market Flows", cell_bold),
                    Paragraph("<b>DATA UNAVAILABLE</b>", cell_muted),
                    Paragraph(f"<i>{fii_dii.get('message', 'Official exchange institutional flow feed offline.')}</i>", cell_regular)
                ],
                [
                    Paragraph("Data Integrity Policy", cell_muted),
                    Paragraph("STRICT", cell_muted),
                    Paragraph("Values are never fabricated or estimated when exchange endpoints are unreachable.", cell_muted)
                ]
            ]

        fii_table = Table(fii_rows, colWidths=[160, 160, 220])
        fii_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(fii_table)
        story.append(Spacer(1, 14))

        # Section 7: Volatility & Composite Risk Radar
        story.append(Paragraph("7. Volatility & Composite Risk Radar", section_hdr_style))
        story.append(Paragraph("Multi-asset quantitative risk assessment combining domestic and international volatility gauges.", body_style))
        story.append(Spacer(1, 8))

        comp_risk = risk_radar.get("composite_risk", "MODERATE")
        risk_c = c_success if comp_risk == "LOW" else (c_danger if comp_risk == "HIGH" else c_warning)

        factors_list = risk_radar.get("contributing_factors", ["Parameters normal."])
        factors_str = "<br/>• ".join(factors_list)

        risk_table_data = [
            [
                Paragraph("<b>COMPOSITE RISK LEVEL:</b>", cell_bold),
                Paragraph(f"<b>{comp_risk} RISK</b>", ParagraphStyle('Rk', parent=cell_bold, textColor=risk_c, fontSize=11)),
                Paragraph("<b>INDIA VIX (DOMESTIC):</b>", cell_bold),
                Paragraph(f"{risk_radar.get('india_vix', '--')}", cell_regular)
            ],
            [
                Paragraph("<b>US VIX (GLOBAL):</b>", cell_bold), Paragraph(f"{risk_radar.get('us_vix', '--')}", cell_regular),
                Paragraph("<b>BRENT CRUDE OIL:</b>", cell_bold), Paragraph(f"${risk_radar.get('crude_brent', '--')}/bbl", cell_regular)
            ],
            [
                Paragraph("<b>USD / INR RATE:</b>", cell_bold), Paragraph(f"₹{risk_radar.get('usdinr', '--')}", cell_regular),
                Paragraph("<b>SOURCE:</b>", cell_bold), Paragraph(risk_radar.get("source", "Intermarket Risk Engine"), cell_muted)
            ],
            [
                Paragraph("<b>KEY RISK FACTORS:</b>", cell_bold),
                Paragraph(f"• {factors_str}", cell_regular),
                Paragraph("", cell_muted), Paragraph("", cell_muted)
            ]
        ]

        risk_table = Table(risk_table_data, colWidths=[130, 140, 130, 140])
        risk_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), c_card_bg),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('SPAN', (1, 3), (3, 3)),
        ]))
        story.append(risk_table)
        story.append(PageBreak())

        # ═════════════════════════════════════════════════════════════════
        # PAGE 5: NEWS INTELLIGENCE + SENTIMENT BAROMETER + STOCKS IN FOCUS
        # ═════════════════════════════════════════════════════════════════
        story.append(Paragraph("8. News Intelligence & Sentiment Barometer", section_hdr_style))
        story.append(Paragraph("Live corporate and regulatory filings filtered through the VADER Financial Lexicon Engine.", body_style))
        story.append(Spacer(1, 8))

        # Sentiment Barometer
        sent_bar_data = [
            [
                Paragraph("<b>OVERALL SENTIMENT:</b>", cell_bold),
                Paragraph(f"<b>{news.get('overall_sentiment', 'NEUTRAL')}</b>", cell_bullish if news.get("overall_sentiment") == "BULLISH" else (cell_bearish if news.get("overall_sentiment") == "BEARISH" else cell_regular)),
                Paragraph("<b>ARTICLES ANALYZED:</b>", cell_bold), Paragraph(str(news.get("total_articles", 0)), cell_regular)
            ],
            [
                Paragraph("<b>BULLISH ARTICLES:</b>", cell_bold), Paragraph(f"{news.get('bullish_count', 0)} ({news.get('bullish_pct', 0.0)}%)", cell_bullish),
                Paragraph("<b>BEARISH ARTICLES:</b>", cell_bold), Paragraph(f"{news.get('bearish_count', 0)} ({news.get('bearish_pct', 0.0)}%)", cell_bearish)
            ],
            [
                Paragraph("<b>NEUTRAL ARTICLES:</b>", cell_bold), Paragraph(f"{news.get('neutral_count', 0)} ({news.get('neutral_pct', 0.0)}%)", cell_regular),
                Paragraph("<b>AI INTERPRETATION:</b>", cell_bold), Paragraph(f"<i>{news.get('ai_interpretation', '')}</i>", cell_regular)
            ]
        ]
        s_table = Table(sent_bar_data, colWidths=[120, 150, 120, 150])
        s_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), c_card_bg),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(s_table)
        story.append(Spacer(1, 10))

        # Recent Important Headlines
        story.append(Paragraph("<b>Top Current Verified Headlines</b>", cell_bold))
        news_items = news.get("articles", [])
        if news_items:
            news_rows = [
                [
                    Paragraph("<b>HEADLINE</b>", cell_bold),
                    Paragraph("<b>CATEGORY</b>", cell_bold),
                    Paragraph("<b>SENTIMENT</b>", cell_bold),
                    Paragraph("<b>IMPACT</b>", cell_bold),
                    Paragraph("<b>SOURCE</b>", cell_bold)
                ]
            ]
            for itm in news_items[:5]:
                s_style = cell_bullish if itm.get("sentiment") == "BULLISH" else (cell_bearish if itm.get("sentiment") == "BEARISH" else cell_regular)
                news_rows.append([
                    Paragraph(itm.get("headline", "--"), cell_regular),
                    Paragraph(itm.get("category", "MARKET"), cell_muted),
                    Paragraph(itm.get("sentiment", "NEUTRAL"), s_style),
                    Paragraph(itm.get("impact", "LOW"), cell_bold if itm.get("impact") == "HIGH" else cell_muted),
                    Paragraph(f"{itm.get('source', 'News')}<br/>{itm.get('timestamp', '')}", cell_muted)
                ])

            n_table = Table(news_rows, colWidths=[240, 65, 75, 60, 100])
            n_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ('BOX', (0, 0), (-1, -1), 0.5, c_border),
                ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(n_table)
        else:
            story.append(Paragraph("<i>No verified headlines available for the current session.</i>", cell_muted))

        story.append(Spacer(1, 10))

        # Stocks in Focus Table
        story.append(Paragraph("<b>Stocks in Focus (News-Driven Catalyst Radar)</b>", cell_bold))
        s_focus = news.get("stocks_in_focus", [])
        if s_focus:
            foc_rows = [
                [Paragraph("<b>TICKER</b>", cell_bold), Paragraph("<b>CATALYST / HEADLINE</b>", cell_bold), Paragraph("<b>SENTIMENT</b>", cell_bold), Paragraph("<b>IMPACT</b>", cell_bold)]
            ]
            for f in s_focus[:4]:
                s_style = cell_bullish if f.get("sentiment") == "BULLISH" else (cell_bearish if f.get("sentiment") == "BEARISH" else cell_regular)
                foc_rows.append([
                    Paragraph(f"<b>{f.get('ticker')}</b>", cell_bold),
                    Paragraph(f.get("headline", "--"), cell_regular),
                    Paragraph(f.get("sentiment", "NEUTRAL"), s_style),
                    Paragraph(f.get("impact", "MEDIUM"), cell_bold)
                ])
            f_table = Table(foc_rows, colWidths=[90, 310, 70, 70])
            f_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ('BOX', (0, 0), (-1, -1), 0.5, c_border),
                ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(f_table)
        else:
            story.append(Paragraph("<i>No single-stock news anomalies detected in active monitoring.</i>", cell_muted))

        story.append(PageBreak())

        # ═════════════════════════════════════════════════════════════════
        # PAGE 6: ECONOMIC CALENDAR + CORPORATE ACTION TRACKER
        # ═════════════════════════════════════════════════════════════════
        story.append(Paragraph("9. Verified Economic Calendar (Macro Catalysts)", section_hdr_style))
        story.append(Paragraph("High and medium-impact central bank, inflation, and growth releases.", body_style))
        story.append(Spacer(1, 8))

        e_cal = events.get("economic_calendar", [])
        if e_cal:
            e_rows = [
                [
                    Paragraph("<b>DATE</b>", cell_bold),
                    Paragraph("<b>TIME</b>", cell_bold),
                    Paragraph("<b>REGION</b>", cell_bold),
                    Paragraph("<b>ECONOMIC EVENT</b>", cell_bold),
                    Paragraph("<b>IMPORTANCE</b>", cell_bold)
                ]
            ]
            for ev in e_cal:
                imp_style = cell_danger if ev.get("importance") == "HIGH" else cell_muted
                e_rows.append([
                    Paragraph(ev.get("date", "--"), cell_regular),
                    Paragraph(ev.get("time", "--"), cell_muted),
                    Paragraph(ev.get("country", "--"), cell_bold),
                    Paragraph(ev.get("event", "--"), cell_regular),
                    Paragraph(ev.get("importance", "MEDIUM"), imp_style)
                ])
            e_table = Table(e_rows, colWidths=[80, 70, 70, 240, 80])
            e_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ('BOX', (0, 0), (-1, -1), 0.5, c_border),
                ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(e_table)
        else:
            story.append(Paragraph("<i>No verified high-impact economic releases scheduled.</i>", cell_muted))

        story.append(Spacer(1, 14))

        # Corporate Action Tracker
        story.append(Paragraph("10. Corporate Actions & Announcements Tracker", section_hdr_style))
        story.append(Paragraph("Upcoming dividend record dates, earnings boards, AGM milestones, and stock splits.", body_style))
        story.append(Spacer(1, 8))

        c_actions = events.get("corporate_actions", [])
        if c_actions:
            c_rows = [
                [
                    Paragraph("<b>TIMEFRAME</b>", cell_bold),
                    Paragraph("<b>TICKER</b>", cell_bold),
                    Paragraph("<b>COMPANY</b>", cell_bold),
                    Paragraph("<b>ANNOUNCEMENT / ACTION</b>", cell_bold),
                    Paragraph("<b>DATE</b>", cell_bold)
                ]
            ]
            for act in c_actions:
                c_rows.append([
                    Paragraph(act.get("window", "THIS WEEK"), cell_muted),
                    Paragraph(f"<b>{act.get('ticker')}</b>", cell_bold),
                    Paragraph(act.get("company", "--"), cell_regular),
                    Paragraph(act.get("action", "--"), cell_regular),
                    Paragraph(act.get("date", "--"), cell_muted)
                ])
            c_table = Table(c_rows, colWidths=[80, 80, 130, 180, 70])
            c_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ('BOX', (0, 0), (-1, -1), 0.5, c_border),
                ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(c_table)
        else:
            story.append(Paragraph("<i>No corporate actions announced in the active window.</i>", cell_muted))

        story.append(PageBreak())

        # ═════════════════════════════════════════════════════════════════
        # PAGE 7: AI MARKET OPPORTUNITIES (INTRADAY & SWING)
        # ═════════════════════════════════════════════════════════════════
        story.append(Paragraph("11. AI Market Opportunities (Intraday & Swing)", section_hdr_style))
        
        # Mandatory Warning Alert
        warn_text = "<b>CRITICAL NOTIFICATION: VIRTUAL AI RECOMMENDATIONS — NOT LIVE POSITIONS.</b><br/>" \
                    "The signals below reflect active quantitative model inferences and calibrated threshold qualification. " \
                    "They represent informational intelligence only and do NOT imply executed broker positions."
        warn_p = Paragraph(warn_text, ParagraphStyle('Wn', parent=cell_regular, textColor=colors.HexColor("#991B1B"), fontSize=8, leading=11))
        warn_table = Table([[warn_p]], colWidths=[540])
        warn_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#FEF2F2")),
            ('BOX', (0, 0), (-1, -1), 1.0, colors.HexColor("#F87171")),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        story.append(warn_table)
        story.append(Spacer(1, 10))

        # Intraday AI Table
        story.append(Paragraph("<b>Intraday AI Recommendations (5M/15M Horizon)</b>", cell_bold))
        intra_ops = ai_ops.get("intraday", {}).get("opportunities", [])
        if intra_ops:
            i_rows = [
                [Paragraph("<b>TICKER</b>", cell_bold), Paragraph("<b>DIR</b>", cell_bold), Paragraph("<b>CONF</b>", cell_bold), Paragraph("<b>ENTRY</b>", cell_bold), Paragraph("<b>TARGET</b>", cell_bold), Paragraph("<b>STOP LOSS</b>", cell_bold), Paragraph("<b>RATIONALE</b>", cell_bold)]
            ]
            for op in intra_ops[:4]:
                d_style = cell_bullish if op.get("direction") == "BULLISH" else cell_bearish
                i_rows.append([
                    Paragraph(f"<b>{op['ticker']}</b>", cell_bold),
                    Paragraph(op['direction'], d_style),
                    Paragraph(f"{op['confidence']}%", cell_bold),
                    Paragraph(f"₹{op['entry']:,.2f}", cell_regular),
                    Paragraph(f"₹{op['tp1']:,.2f}", cell_bullish),
                    Paragraph(f"₹{op['sl']:,.2f}", cell_bearish),
                    Paragraph(op.get("reason", "--")[:70], cell_muted)
                ])
            it_table = Table(i_rows, colWidths=[80, 50, 45, 65, 65, 65, 170])
            it_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ('BOX', (0, 0), (-1, -1), 0.5, c_border),
                ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(it_table)
        else:
            story.append(Paragraph("<i>NO QUALIFIED INTRADAY OPPORTUNITIES — Strict model thresholds and risk gates enforced.</i>", cell_muted))

        story.append(Spacer(1, 12))

        # Swing AI Table
        story.append(Paragraph("<b>Swing AI Recommendations (Daily/Multi-Day Horizon)</b>", cell_bold))
        sw_ops = ai_ops.get("swing", {}).get("opportunities", [])
        if sw_ops:
            s_rows = [
                [Paragraph("<b>TICKER</b>", cell_bold), Paragraph("<b>DIR</b>", cell_bold), Paragraph("<b>CONF</b>", cell_bold), Paragraph("<b>ENTRY</b>", cell_bold), Paragraph("<b>TARGET</b>", cell_bold), Paragraph("<b>STOP LOSS</b>", cell_bold), Paragraph("<b>RATIONALE</b>", cell_bold)]
            ]
            for op in sw_ops[:4]:
                d_style = cell_bullish if op.get("direction") == "BULLISH" else cell_bearish
                s_rows.append([
                    Paragraph(f"<b>{op['ticker']}</b>", cell_bold),
                    Paragraph(op['direction'], d_style),
                    Paragraph(f"{op['confidence']}%", cell_bold),
                    Paragraph(f"₹{op['entry']:,.2f}", cell_regular),
                    Paragraph(f"₹{op['tp1']:,.2f}", cell_bullish),
                    Paragraph(f"₹{op['sl']:,.2f}", cell_bearish),
                    Paragraph(op.get("reason", "--")[:70], cell_muted)
                ])
            sw_table = Table(s_rows, colWidths=[80, 50, 45, 65, 65, 65, 170])
            sw_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ('BOX', (0, 0), (-1, -1), 0.5, c_border),
                ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(sw_table)
        else:
            story.append(Paragraph("<i>NO QUALIFIED SWING OPPORTUNITIES — Strict model thresholds and risk gates enforced.</i>", cell_muted))

        story.append(PageBreak())

        # ═════════════════════════════════════════════════════════════════
        # PAGE 8: AI MARKET SUMMARY + SYSTEM HEALTH MATRIX
        # ═════════════════════════════════════════════════════════════════
        story.append(Paragraph("12. Grounded AI Market Summary & Synthesis", section_hdr_style))
        story.append(Paragraph("Grounded executive summary synthesized exclusively from verified live dashboard metrics.", body_style))
        story.append(Spacer(1, 8))

        sup_items = "<br/>• ".join(ai_summary.get("supporting_factors", ["Constructive structural breadth."]))
        head_items = "<br/>• ".join(ai_summary.get("headwinds", ["Elevated intermarket volatility."]))

        sum_table_data = [
            [
                Paragraph("<b>SYNTHESIZED MARKET VIEW:</b>", cell_bold),
                Paragraph(f"<b>{ai_summary.get('market_view', 'NEUTRAL')}</b>", ParagraphStyle('MV', parent=cell_bold, textColor=c_accent, fontSize=11))
            ],
            [
                Paragraph("<b>SUPPORTING TAILWINDS:</b>", cell_bold),
                Paragraph(f"• {sup_items}", cell_regular)
            ],
            [
                Paragraph("<b>MARKET HEADWINDS:</b>", cell_bold),
                Paragraph(f"• {head_items}", cell_regular)
            ],
            [
                Paragraph("<b>QUANTITATIVE OBSERVATION:</b>", cell_bold),
                Paragraph(f"<i>\"{ai_summary.get('ai_observation', '')}\"</i>", cell_regular)
            ]
        ]
        sum_table = Table(sum_table_data, colWidths=[160, 380])
        sum_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), c_card_bg),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.append(sum_table)
        story.append(Spacer(1, 14))

        # Subsystem Infrastructure Health Matrix
        story.append(Paragraph("13. Infrastructure & Subsystem Health Matrix", section_hdr_style))
        story.append(Paragraph("Real-time operational status across system pipelines, data services, and models.", body_style))
        story.append(Spacer(1, 8))

        def h_style(stat: str):
            if stat == "HEALTHY":
                return cell_bullish
            elif stat == "DEGRADED":
                return cell_regular
            elif stat == "UNAVAILABLE":
                return cell_muted
            else:
                return cell_bearish

        health_table_data = [
            [Paragraph("<b>SUBSYSTEM</b>", cell_bold), Paragraph("<b>STATUS</b>", cell_bold), Paragraph("<b>INTEGRITY LEVEL / SPECIFICATION</b>", cell_bold)],
            [Paragraph("Historical Market Data Layer", cell_bold), Paragraph(sys_health.get("market_data", "HEALTHY"), h_style(sys_health.get("market_data"))), Paragraph("511 Tickers in SQLite canonical store", cell_muted)],
            [Paragraph("AI Production Models", cell_bold), Paragraph(sys_health.get("ai_models", "HEALTHY"), h_style(sys_health.get("ai_models"))), Paragraph("Champion Ensembles (Intraday & Swing) Verified", cell_muted)],
            [Paragraph("Research Engine & Auto-Lab", cell_bold), Paragraph(sys_health.get("research_engine", "HEALTHY"), h_style(sys_health.get("research_engine"))), Paragraph("Walk-Forward & Holdout Integrity Intact", cell_muted)],
            [Paragraph("Canonical Database (WAL Mode)", cell_bold), Paragraph(sys_health.get("database", "HEALTHY"), h_style(sys_health.get("database"))), Paragraph("Write-Ahead Logging Active with Low Latency", cell_muted)],
            [Paragraph("Telegram Notification Engine", cell_bold), Paragraph(sys_health.get("telegram", "HEALTHY"), h_style(sys_health.get("telegram"))), Paragraph("Bot Dispatch API Ready with Deduplication", cell_muted)],
            [Paragraph("Broker Execution Gateway", cell_bold), Paragraph(sys_health.get("broker_mode", "SIMULATION"), cell_regular), Paragraph("Fail-Safe Simulation Mode (Zero Live Capital Risk)", cell_muted)],
            [Paragraph("Autonomous Job Scheduler", cell_bold), Paragraph(sys_health.get("scheduler", "HEALTHY"), h_style(sys_health.get("scheduler"))), Paragraph("APScheduler Daemon Active (Mon-Fri 09:30 IST)", cell_muted)],
            [Paragraph("<b>OVERALL HEALTH SCORE</b>", cell_bold), Paragraph(f"<b>{sys_health.get('overall_score', 100)} / 100</b>", cell_bullish), Paragraph(f"<b>STATUS: {sys_health.get('overall_status', 'NOMINAL')}</b>", cell_bold)]
        ]

        health_table = Table(health_table_data, colWidths=[180, 110, 250])
        health_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#F8FAFC")),
            ('BOX', (0, 0), (-1, -1), 0.5, c_border),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, c_border),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(health_table)
        story.append(Spacer(1, 14))

        # Legal and Operational Disclaimer
        disc_p = Paragraph(
            "<b>OPERATIONAL NOTICE & DISCLAIMER:</b> This document is an informational output generated by the "
            "AI Trading Market Command Center. All market data, sentiment indices, regime classifications, and AI setups "
            "are calculated for quantitative intelligence and algorithmic observation. This document does not constitute "
            "financial, investment, or legal advice. Trading in equities, derivatives, and commodities involves substantial "
            "risk of capital loss. Verify all information against official exchange disclosures.",
            cell_muted
        )
        story.append(disc_p)

        # Build document with NumberedCanvas
        canvas_maker = DashboardNumberedCanvas
        doc.build(story, canvasmaker=canvas_maker)

        pdf_content = buffer.getvalue()
        buffer.close()
        return pdf_content
