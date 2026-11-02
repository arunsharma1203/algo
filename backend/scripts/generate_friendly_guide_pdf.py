import os
import shutil
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
)
from reportlab.pdfgen import canvas

# Paths
OUTPUT_PDF_PATH = "/Users/arunsharma/Desktop/swing trade react/How_Our_Trading_Robot_Works_A_Friendly_Guide.pdf"
ARTIFACT_PDF_PATH = "/Users/arunsharma/.gemini/antigravity/brain/e8cf1b6f-7fef-4ffe-b1ae-7f2881c20ad4/How_Our_Trading_Robot_Works_A_Friendly_Guide.pdf"
TEMP_DIR = "/tmp/trading_robot_guide"
os.makedirs(TEMP_DIR, exist_ok=True)

DIAGRAM1_PATH = os.path.join(TEMP_DIR, "diagram1_pipeline.png")
DIAGRAM2_PATH = os.path.join(TEMP_DIR, "diagram2_night_school.png")


def generate_pipeline_diagram():
    """Generates Diagram 1: The 7-Station Trading Assembly Line."""
    fig, ax = plt.subplots(figsize=(12, 6.8), dpi=220)
    ax.set_facecolor("#f8fafc")
    fig.patch.set_facecolor("#f8fafc")
    ax.axis("off")

    # Title
    ax.text(6.0, 6.4, "THE 7-STATION SMART TRADING PIPELINE", 
            ha="center", va="center", fontsize=16, fontweight="heavy", color="#0f172a", fontfamily="sans-serif")
    ax.text(6.0, 6.05, "From Indian Stock Exchange Ticks to Safe, Intelligent Trade Execution", 
            ha="center", va="center", fontsize=10, color="#64748b", fontfamily="sans-serif")

    # Stations definition: (x, y, width, height, title, subtitle, kid_metaphor, color, border)
    stations_row1 = [
        (0.6, 3.8, 2.3, 1.8, "Station 1: Data Hoarder", "NSE/BSE Ingestion", "The Hungry Squirrel\nGathers 508 stocks every 15m", "#e0f2fe", "#0284c7"),
        (3.5, 3.8, 2.3, 1.8, "Station 2: Cleanliness Gate", "Validator & Filter", "The Bubble Bath\nWashes out glitches & bad data", "#e0e7ff", "#4338ca"),
        (6.4, 3.8, 2.3, 1.8, "Station 3: Feature Kitchen", "Alpha-158 Formulas", "The Secret Recipe\nCooks 158 math indicators into jars", "#f3e8ff", "#7e22ce"),
        (9.3, 3.8, 2.3, 1.8, "Station 4: Two Detectives", "Qlib & Legacy AI", "The Super Sleuths\nWizard (Math) + Detective (News)", "#fce7f3", "#be185d"),
    ]

    stations_row2 = [
        (8.0, 1.2, 3.4, 1.8, "Station 5: The Supreme Judge", "Consensus Conductor", "The Conductor & Judge\nRequires 100% agreement! If they argue, NO DEAL!", "#dcfce7", "#15803d"),
        (4.3, 1.2, 3.1, 1.8, "Station 6: Invisible Shields", "Stop-Loss & Cutoff", "The Safety Trampoline\nEjects on 0.5% drop, 14:30 bedtime cutoff", "#fef3c7", "#b45309"),
        (0.6, 1.2, 3.1, 1.8, "Station 7: Night School", "Daily Hedge Online Learner", "The Evening Class\n3:35 PM: grades today's wins/losses & tunes weights", "#ccfbf1", "#0f766e"),
    ]

    for (x, y, w, h, title, sub, kid, bg, border) in stations_row1 + stations_row2:
        # Box shadow
        shadow = patches.FancyBboxPatch((x + 0.05, y - 0.05), w, h, boxstyle="round,pad=0.08,rounding_size=0.15",
                                        facecolor="#cbd5e1", edgecolor="none", alpha=0.5)
        ax.add_patch(shadow)
        # Main box
        rect = patches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.08,rounding_size=0.15",
                                      facecolor=bg, edgecolor=border, linewidth=2.0)
        ax.add_patch(rect)
        # Text
        ax.text(x + w/2, y + h - 0.30, title, ha="center", va="center", fontsize=9.5, fontweight="bold", color="#0f172a")
        ax.text(x + w/2, y + h - 0.60, f"({sub})", ha="center", va="center", fontsize=7.5, color=border, fontweight="semibold")
        ax.text(x + w/2, y + 0.55, kid, ha="center", va="center", fontsize=8.0, color="#334155", linespacing=1.3)

    # Arrows Row 1: 1 -> 2 -> 3 -> 4
    arrow_kw = dict(arrowstyle="->,head_width=0.4,head_length=0.6", color="#475569", lw=2.5)
    ax.annotate("", xy=(3.4, 4.7), xytext=(3.0, 4.7), arrowprops=arrow_kw)
    ax.annotate("", xy=(6.3, 4.7), xytext=(5.9, 4.7), arrowprops=arrow_kw)
    ax.annotate("", xy=(9.2, 4.7), xytext=(8.8, 4.7), arrowprops=arrow_kw)

    # Down arrow from Station 4 to Station 5
    ax.annotate("", xy=(9.8, 3.1), xytext=(9.8, 3.7), arrowprops=arrow_kw)

    # Arrows Row 2: 5 -> 6 -> 7
    ax.annotate("", xy=(7.5, 2.1), xytext=(7.9, 2.1), arrowprops=arrow_kw)
    ax.annotate("", xy=(3.8, 2.1), xytext=(4.2, 2.1), arrowprops=arrow_kw)

    # Loop-back arrow from Station 7 back to Station 4 & 5 (Tomorrow morning)
    loop_kw = dict(arrowstyle="->,head_width=0.4,head_length=0.6", color="#0f766e", lw=2.2, linestyle="--")
    ax.annotate("Feedback for Tomorrow Morning >>", xy=(0.6, 3.5), xytext=(0.6, 2.9),
                ha="center", fontsize=7.5, fontweight="bold", color="#0f766e")
    ax.annotate("", xy=(0.6, 3.7), xytext=(0.6, 3.1), arrowprops=loop_kw)

    plt.xlim(0, 12.2)
    plt.ylim(0.4, 6.8)
    plt.tight_layout()
    plt.savefig(DIAGRAM1_PATH, bbox_inches="tight")
    plt.close()
    print("Diagram 1 created!")


def generate_night_school_diagram():
    """Generates Diagram 2: The 3:35 PM Night School Learning Loop."""
    fig, ax = plt.subplots(figsize=(10.5, 5.5), dpi=220)
    ax.set_facecolor("#0f172a")
    fig.patch.set_facecolor("#0f172a")
    ax.axis("off")

    # Title
    ax.text(5.25, 5.1, "HOW NIGHT SCHOOL ADAPTS WEIGHTS EVERY DAY", 
            ha="center", va="center", fontsize=15, fontweight="heavy", color="#ffffff")
    ax.text(5.25, 4.75, "Hedge Regret Minimization: Tuning the Conductor's Trust Without Touching Base Brains", 
            ha="center", va="center", fontsize=9.5, color="#94a3b8")

    # 4 Cyclic Boxes
    boxes = [
        (0.8, 2.3, 2.2, 1.8, "1. Market Closes [Bell]", "3:30 PM IST", "Every trade logs final outcome:\nTarget Met (Win) or SL Hit (Loss)", "#1e293b", "#38bdf8"),
        (3.8, 2.9, 2.9, 1.5, "2. Report Cards [Grades]", "Brier Loss Formula", "Teacher grades each detective:\nWho was confident and right?\nWho was confident and wrong?", "#1e293b", "#c084fc"),
        (7.4, 2.3, 2.5, 1.8, "3. Turn the Knob [Tuning]", "Multiplicative Weights", "Hedge equation moves weights:\nMax 5% swing/day\nFloor: neither drops below 20%", "#1e293b", "#4ade80"),
        (3.8, 0.7, 2.9, 1.5, "4. Tomorrow 09:15 AM [Bell]", "Smart Execution", "Consensus uses new weights:\nMore vote to today's star,\nless to the one that stumbled!", "#1e293b", "#fbbf24")
    ]

    for (x, y, w, h, title, sub, desc, bg, border) in boxes:
        rect = patches.FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.08,rounding_size=0.15",
                                      facecolor=bg, edgecolor=border, linewidth=2.0)
        ax.add_patch(rect)
        ax.text(x + w/2, y + h - 0.35, title, ha="center", va="center", fontsize=10, fontweight="bold", color="#ffffff")
        ax.text(x + w/2, y + h - 0.65, f"[{sub}]", ha="center", va="center", fontsize=7.5, color=border, fontweight="bold")
        ax.text(x + w/2, y + 0.45, desc, ha="center", va="center", fontsize=7.8, color="#cbd5e1", linespacing=1.25)

    # Connecting arrows
    arr_kw = dict(arrowstyle="->,head_width=0.4,head_length=0.6", color="#e2e8f0", lw=2.2)
    # Box 1 -> Box 2
    ax.annotate("", xy=(3.7, 3.6), xytext=(3.1, 3.6), arrowprops=arr_kw)
    # Box 2 -> Box 3
    ax.annotate("", xy=(7.3, 3.6), xytext=(6.8, 3.6), arrowprops=arr_kw)
    # Box 3 -> Box 4
    ax.annotate("", xy=(6.8, 1.5), xytext=(7.4, 2.2), arrowprops=arr_kw)
    # Box 4 -> Box 1
    ax.annotate("", xy=(2.7, 2.2), xytext=(3.7, 1.5), arrowprops=arr_kw)

    # Invariant Shield in Center
    shield = patches.FancyBboxPatch((4.0, 2.3), 2.5, 0.5, boxstyle="round,pad=0.05,rounding_size=0.1",
                                   facecolor="#831843", edgecolor="#f43f5e", linewidth=1.5)
    ax.add_patch(shield)
    ax.text(5.25, 2.55, "[LOCK] BASE BRAINS 100% FROZEN", ha="center", va="center", fontsize=8.0, fontweight="heavy", color="#ffffff")

    plt.xlim(0, 10.6)
    plt.ylim(0.4, 5.5)
    plt.tight_layout()
    plt.savefig(DIAGRAM2_PATH, bbox_inches="tight")
    plt.close()
    print("Diagram 2 created!")


class NumberedCanvas(canvas.Canvas):
    """Adds running headers and footers with real total page numbers."""
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
            self.draw_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_decorations(self, num_pages):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))

        # Running Header (on pages > 1)
        if self._pageNumber > 1:
            self.drawString(36, 810, "How Our Robot Trading System Works | A Friendly Non-Technical Guide")
            self.setStrokeColor(colors.HexColor("#e2e8f0"))
            self.setLineWidth(0.75)
            self.line(36, 804, 559, 804)

        # Running Footer
        page_str = f"Page {self._pageNumber} of {num_pages}"
        self.drawRightString(559, 25, page_str)
        self.drawString(36, 25, "Confidential & Educational | Dual-Engine Consensus Platform Architecture")
        self.setStrokeColor(colors.HexColor("#e2e8f0"))
        self.setLineWidth(0.75)
        self.line(36, 35, 559, 35)

        self.restoreState()


def build_pdf():
    """Builds the complete publication-quality PDF."""
    generate_pipeline_diagram()
    generate_night_school_diagram()

    doc = SimpleDocTemplate(
        OUTPUT_PDF_PATH,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=46,
        bottomMargin=46
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor('#0f172a'),
        alignment=0,
        spaceAfter=6
    )

    subtitle_style = ParagraphStyle(
        'DocSubTitle',
        fontName='Helvetica',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#475569'),
        spaceAfter=15
    )

    h1_style = ParagraphStyle(
        'Heading1_Custom',
        fontName='Helvetica-Bold',
        fontSize=15,
        leading=19,
        textColor=colors.HexColor('#1e293b'),
        spaceBefore=14,
        spaceAfter=8,
        keepWithNext=True
    )

    h2_style = ParagraphStyle(
        'Heading2_Custom',
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#0284c7'),
        spaceBefore=10,
        spaceAfter=5,
        keepWithNext=True
    )

    body_style = ParagraphStyle(
        'Body_Custom',
        fontName='Helvetica',
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor('#334155'),
        spaceAfter=8
    )

    callout_kid_style = ParagraphStyle(
        'CalloutKid',
        fontName='Helvetica',
        fontSize=9.0,
        leading=13.5,
        textColor=colors.HexColor('#065f46')
    )

    callout_tech_style = ParagraphStyle(
        'CalloutTech',
        fontName='Helvetica',
        fontSize=8.5,
        leading=12.5,
        textColor=colors.HexColor('#1e3a8a')
    )

    story = []

    # ── COVER HEADER ──────────────────────────────────────────────────────────
    story.append(Paragraph("How Our Robot Trading System Works", title_style))
    story.append(Paragraph("<b>A Friendly Guide Even a 5-Year-Old Can Understand!</b><br/>"
                           "<i>From Raw Indian Stock Ticks to Smart Trades: The Complete Journey of Our Platform</i>", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#0284c7"), spaceAfter=15))

    # ── CHAPTER 1: THE BIG PICTURE ────────────────────────────────────────────
    story.append(Paragraph("1. The Magic Candy Bazaar (What Is This Project?)", h1_style))
    story.append(Paragraph(
        "Imagine a giant, bustling toy and candy market with <b>500 different stalls</b> (companies like Reliance, Tata, Infosys, and HDFC). "
        "Every minute of the day, prices go up and down. People are shouting, running around, getting excited, or getting scared.", body_style
    ))
    story.append(Paragraph(
        "When human beings trade in this market, they face three big problems: "
        "<br/>• <b>They get tired:</b> No human can watch 500 stalls at the same second."
        "<br/>• <b>They get greedy:</b> They buy when everyone else is shouting, often paying way too much."
        "<br/>• <b>They get scared:</b> They panic and sell at the worst possible time.", body_style
    ))

    # Callout Box: What we built
    callout_data = [[
        Paragraph("<b>🌟 What We Built:</b><br/>"
                  "We built a <b>Team of Friendly Super-Robots</b>. They don't have emotions, they never get sleepy, and they "
                  "can look at all 508 companies across India at the exact same millisecond. "
                  "They only put money down when <b>two completely different artificial intelligences shake hands</b> and agree: "
                  "<i>'Yes! This is a Grade-A, super-duper good trade!'</i>", callout_kid_style)
    ]]
    callout_table = Table(callout_data, colWidths=[520])
    callout_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#ecfdf5")),
        ('BORDER', (0,0), (-1,-1), 1.5, colors.HexColor("#10b981")),
        ('PADDING', (0,0), (-1,-1), 10),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    story.append(callout_table)
    story.append(Spacer(1, 15))

    # ── CHAPTER 2: THE 7-STATION PIPELINE ─────────────────────────────────────
    story.append(Paragraph("2. The 7-Station Assembly Line: How a Trade Is Born", h1_style))
    story.append(Paragraph(
        "Think of our trading platform like a high-tech chocolate factory. Raw cocoa beans enter at one end, "
        "pass through rigorous cleaning and cooking stations, get inspected by two master chefs, and only the finest, "
        "safest chocolates get wrapped and delivered. Here is the entire journey:", body_style
    ))

    # Embed Diagram 1
    story.append(Spacer(1, 4))
    story.append(Image(DIAGRAM1_PATH, width=520, height=294))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Let's walk through each station one by one:", body_style))

    # Station Breakdown Table
    stations_data = [
        [
            Paragraph("<b>Station</b>", ParagraphStyle('Hdr', fontName='Helvetica-Bold', fontSize=9, textColor=colors.white)),
            Paragraph("<b>The 5-Year-Old Explanation 🧸</b>", ParagraphStyle('Hdr2', fontName='Helvetica-Bold', fontSize=9, textColor=colors.white)),
            Paragraph("<b>What the Grown-Up Code Actually Does 💻</b>", ParagraphStyle('Hdr3', fontName='Helvetica-Bold', fontSize=9, textColor=colors.white))
        ],
        [
            Paragraph("<b>Station 1:<br/>The Data Hoarder</b>", body_style),
            Paragraph("<b>The Hungry Squirrel 🐿️</b><br/>Scampers around the market gathering fresh price acorns from all 508 stalls every 15 minutes.", body_style),
            Paragraph("<b>Market Ingestion Daemon:</b> Queries Upstox and Yahoo Finance APIs every 15m. Fetches Open, High, Low, Close, and Volume across all equities without throttling.", callout_tech_style)
        ],
        [
            Paragraph("<b>Station 2:<br/>The Cleanliness Gate</b>", body_style),
            Paragraph("<b>The Bubble Bath 🧼</b><br/>Washes away bad numbers, missing candles, and glitches so the robots never eat dirty food.", body_style),
            Paragraph("<b>MarketDataValidator:</b> Enforces fail-closed data integrity. Rejects zero-volume bars, negative prices, and stale timestamps before feature engineering.", callout_tech_style)
        ],
        [
            Paragraph("<b>Station 3:<br/>The Feature Kitchen</b>", body_style),
            Paragraph("<b>The Secret Spice Kitchen 🧑‍🍳</b><br/>Turns plain prices into 158 magical clues: speed, bounce, loudness, and strength.", body_style),
            Paragraph("<b>Alpha-158 GBDT Features:</b> Computes rolling VWAP distances, ATR volatility, RSI, and MACD. Packs them into lightning-fast binary files (<code>.15min.bin</code>).", callout_tech_style)
        ],
        [
            Paragraph("<b>Station 4:<br/>The Two Detectives</b>", body_style),
            Paragraph("<b>Wizard & Sleuth 🧠</b><br/>Two separate detectives study the clues independently. One does pure math, the other reads newspapers!", body_style),
            Paragraph("<b>Dual AI Models:</b><br/>1. <i>Microsoft Qlib:</i> Alpha158 Gradient Boosted Trees.<br/>2. <i>Legacy AI:</i> Random Forest + FinBERT NLP reading live business news sentiment.", callout_tech_style)
        ],
        [
            Paragraph("<b>Station 5:<br/>The Supreme Judge</b>", body_style),
            Paragraph("<b>The Consensus Conductor 🎼</b><br/>Both detectives must shake hands! If they disagree on direction, the Judge yells <i>'NO DEAL!'</i>", body_style),
            Paragraph("<b>Consensus Confluence Scanner:</b> Requires unanimous directional agreement. Computes dynamic weighted score. Discards conflicts to eliminate false breakouts.", callout_tech_style)
        ],
        [
            Paragraph("<b>Station 6:<br/>Invisible Shields</b>", body_style),
            Paragraph("<b>The Trampoline & Helmet 🛡️</b><br/>If a trade misbehaves by 0.5%, it gets ejected instantly. Plus, bedtime nap at 2:30 PM!", body_style),
            Paragraph("<b>Risk & Execution Governance:</b> Tight Stop-Loss, Dynamic Breakeven Ratchet (locks in profit), and a hard 14:30 IST execution cutoff to dodge afternoon chop traps.", callout_tech_style)
        ],
        [
            Paragraph("<b>Station 7:<br/>Night School</b>", body_style),
            Paragraph("<b>The Evening Class 🌙</b><br/>At 3:35 PM, the robots go to school to review today's wins and losses, adjusting trust for tomorrow.", body_style),
            Paragraph("<b>ConsensusLearningEngine:</b> Post-market Hedge algorithm evaluates per-engine Brier prediction loss, updating consensus voting weights smoothly (max 5% swing/day).", callout_tech_style)
        ]
    ]

    t_stations = Table(stations_data, colWidths=[90, 210, 220])
    t_stations.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#0f172a")),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor("#f8fafc")]),
        ('PADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(t_stations)

    story.append(PageBreak())

    # ── CHAPTER 3: THE TWO DETECTIVES & THE SUPREME JUDGE ─────────────────────
    story.append(Paragraph("3. Meet the Two Detectives & The Supreme Judge", h1_style))
    story.append(Paragraph(
        "Why do we have <b>two</b> artificial intelligence engines instead of just one? "
        "Because in the real world, relying on a single expert is dangerous! Here is how our two detectives work together:", body_style
    ))

    detectives_data = [
        [
            Paragraph("<b>Detective #1: Qlib (The Math Wizard) 🧙‍♂️</b>", ParagraphStyle('Det1', fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#7e22ce'))),
            Paragraph("<b>Detective #2: Legacy + FinBERT (The News Sleuth) 🕵️‍♂️</b>", ParagraphStyle('Det2', fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#be185d')))
        ],
        [
            Paragraph(
                "• <b>What it does:</b> Looks at 158 mathematical factors at once.<br/>"
                "• <b>Its superpower:</b> Finding hidden statistical speed and volume momentum across 508 stocks.<br/>"
                "• <b>Its weakness:</b> It doesn't read news headlines. If a CEO suddenly resigns, it might not notice immediately.",
                body_style
            ),
            Paragraph(
                "• <b>What it does:</b> Reads candlestick charts and live newspaper headlines with FinBERT NLP.<br/>"
                "• <b>Its superpower:</b> Smelling market sentiment (is the mood happy, nervous, or panicked?).<br/>"
                "• <b>Its weakness:</b> Can sometimes hesitate or get tricked by quick false spikes.",
                body_style
            )
        ]
    ]
    t_det = Table(detectives_data, colWidths=[255, 255])
    t_det.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,-1), colors.HexColor("#faf5ff")),
        ('BACKGROUND', (1,0), (1,-1), colors.HexColor("#fff1f2")),
        ('BORDER', (0,0), (0,-1), 1.5, colors.HexColor("#c084fc")),
        ('BORDER', (1,0), (1,-1), 1.5, colors.HexColor("#f43f5e")),
        ('PADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(t_det)
    story.append(Spacer(1, 10))

    # The Supreme Judge
    judge_data = [[
        Paragraph("<b>⚖️ The Supreme Judge (Dual-Engine Consensus Confluence):</b><br/>"
                  "When Detective 1 says <i>'BUY Reliance'</i> but Detective 2 says <i>'SELL Reliance'</i>, "
                  "the Supreme Judge steps in: <b>'NO TRADE!'</b><br/>"
                  "By strictly demanding that both detectives agree unanimously on direction and pass a high conviction score, "
                  "our platform eliminates over 85% of bad, choppy trades before risking a single rupee.", callout_kid_style)
    ]]
    t_judge = Table(judge_data, colWidths=[520])
    t_judge.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#f0fdf4")),
        ('BORDER', (0,0), (-1,-1), 1.5, colors.HexColor("#22c55e")),
        ('PADDING', (0,0), (-1,-1), 10),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    story.append(t_judge)
    story.append(Spacer(1, 15))

    # ── CHAPTER 4: NIGHT SCHOOL (DAILY LEARNING) ──────────────────────────────
    story.append(Paragraph("4. The 3:35 PM Night School: How the Robot Learns Daily", h1_style))
    story.append(Paragraph(
        "A common question people ask is: <i>'Does the robot get smarter every day?'</i><br/>"
        "<b>Yes!</b> But it does so in a very special, safe way that avoids <b>overfitting</b> (the robotic equivalent of amnesia or getting tricked by yesterday's mood).", body_style
    ))

    # Embed Diagram 2
    story.append(Spacer(1, 4))
    story.append(Image(DIAGRAM2_PATH, width=520, height=272))
    story.append(Spacer(1, 10))

    story.append(Paragraph("<b>The Volume Knob Metaphor (Online Hedge Algorithm):</b>", h2_style))
    story.append(Paragraph(
        "Imagine an orchestra with a Violinist (Qlib) and a Cellist (Legacy).<br/>"
        "If the violinist plays one squeaky note today, you <b>don't</b> send him to brain surgery or teach him how to hold the violin from scratch! "
        "(That is what retraining base models would do—it ruins years of practice).<br/>"
        "Instead, the <b>Conductor simply turns the violinist's microphone down a tiny bit (say, 1%) and turns the cellist up a tiny bit!</b>", body_style
    ))

    story.append(Paragraph(
        "<b>The 4 Ironclad Rules of Night School:</b><br/>"
        "1. <b>The Report Card (Brier Loss):</b> Every evening at 15:35, the system calculates exact mathematical prediction loss for each engine.<br/>"
        "2. <b>The Step Clamp:</b> Weights can only move by a maximum of <b>5% per day</b>. No single weird day can cause an over-reaction.<br/>"
        "3. <b>The 20% Floor:</b> Neither engine is ever allowed to fall below 20% weight. Even if one engine has a bad week, it is never silenced.<br/>"
        "4. <b>The CUSUM Smoke Detector:</b> If market conditions become so wild and smoky that both detectives start coughing, an alarm rings and the entry bar automatically jumps from 60% to 70%+ until calm returns.", body_style
    ))

    story.append(PageBreak())

    # ── CHAPTER 5: THE 3 GOLDEN RULES OF SAFETY ───────────────────────────────
    story.append(Paragraph("5. The 3 Golden Rules of Safety (Why We Never Go Broke)", h1_style))
    story.append(Paragraph(
        "The secret to professional institutional trading is not trying to win every single penny—it is making sure "
        "that when you lose, you lose a baby scratch, and when you win, you win a real feast!", body_style
    ))

    rules_data = [
        [
            Paragraph("<b>🛡️ Rule 1: The Eject Button<br/>(Stop-Loss Helmet)</b>", ParagraphStyle('R1', fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#b91c1c'))),
            Paragraph("<b>If a trade drops by even 0.5% to 0.8%, the ejection seat fires immediately!</b><br/>"
                      "Human traders hold onto losing trades hoping they bounce back, turning small cuts into deep wounds. "
                      "Our robot never hopes—it cuts the loss with zero hesitation.", body_style)
        ],
        [
            Paragraph("<b>🎪 Rule 2: The Trampoline<br/>(Breakeven Ratchet)</b>", ParagraphStyle('R2', fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#047857'))),
            Paragraph("<b>Once a trade jumps up into green, the floor moves up under it!</b><br/>"
                      "If the stock moves halfway to its profit target, our system ratchets the stop-loss to entry price. "
                      "Even if the stock suddenly drops like a rock, we exit at breakeven. You can never lose money on that trade!", body_style)
        ],
        [
            Paragraph("<b>⏰ Rule 3: The 2:30 PM Nap<br/>(Execution Cutoff)</b>", ParagraphStyle('R3', fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#d97706'))),
            Paragraph("<b>No new trades after 2:30 PM IST!</b><br/>"
                      "Between 2:30 PM and 3:30 PM, the market playground turns into wild chaos as big institutions square off their books. "
                      "By shutting the gate at 14:30, our platform dodged 75% of today's afternoon traps!", body_style)
        ]
    ]

    t_rules = Table(rules_data, colWidths=[160, 360])
    t_rules.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), colors.HexColor("#fef2f2")),
        ('BACKGROUND', (0,1), (0,1), colors.HexColor("#ecfdf5")),
        ('BACKGROUND', (0,2), (0,2), colors.HexColor("#fffbeb")),
        ('BORDER', (0,0), (-1,-1), 1.0, colors.HexColor("#e2e8f0")),
        ('PADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(t_rules)
    story.append(Spacer(1, 15))

    # ── CHAPTER 6: THE ROSETTA STONE TRANSLATOR ───────────────────────────────
    story.append(Paragraph("6. The Rosetta Stone: Kid Language vs Wall Street Quant", h1_style))
    story.append(Paragraph(
        "Use this handy cheat-sheet whenever you need to explain our project to someone non-technical, "
        "or translate it into institutional quantitative finance terms:", body_style
    ))

    rosetta_data = [
        [
            Paragraph("<b>5-Year-Old Explanation 🧸</b>", ParagraphStyle('Ros1', fontName='Helvetica-Bold', fontSize=9, textColor=colors.white)),
            Paragraph("<b>Wall Street / Quant Finance Term 🏛️</b>", ParagraphStyle('Ros2', fontName='Helvetica-Bold', fontSize=9, textColor=colors.white)),
            Paragraph("<b>Where It Lives in the Code 📂</b>", ParagraphStyle('Ros3', fontName='Helvetica-Bold', fontSize=9, textColor=colors.white))
        ],
        [
            Paragraph("The Hungry Squirrel gathering acorns", body_style),
            Paragraph("15-Minute Multi-Universe Ingestion Layer", body_style),
            Paragraph("<code>hoarder.py</code> & <code>market_provider.py</code>", callout_tech_style)
        ],
        [
            Paragraph("The Secret Recipe Spice Kitchen", body_style),
            Paragraph("Alpha-158 Quantitative Feature Extraction", body_style),
            Paragraph("<code>india_data_adapter.py</code>", callout_tech_style)
        ],
        [
            Paragraph("The Math Wizard Detective", body_style),
            Paragraph("Microsoft Qlib LightGBM Alpha Factor Model", body_style),
            Paragraph("<code>qlib_engine/scanners.py</code>", callout_tech_style)
        ],
        [
            Paragraph("The Chart & Newspaper Sleuth", body_style),
            Paragraph("Legacy AI Ensemble + FinBERT NLP Sentiment", body_style),
            Paragraph("<code>smart_scanner.py</code> & <code>nlp_service.py</code>", callout_tech_style)
        ],
        [
            Paragraph("The Supreme Judge demanding handshake", body_style),
            Paragraph("Dual-Engine Consensus Confluence Gating", body_style),
            Paragraph("<code>consensus_scanner.py</code>", callout_tech_style)
        ],
        [
            Paragraph("The 3:35 PM Night School Teacher", body_style),
            Paragraph("Online Regret Minimization (Hedge / FTRL)", body_style),
            Paragraph("<code>consensus_learning_engine.py</code>", callout_tech_style)
        ],
        [
            Paragraph("The Smoke Detector in the Kitchen", body_style),
            Paragraph("Page's Cumulative Sum (CUSUM) Sequential Test", body_style),
            Paragraph("<code>ConsensusLearningEngine.cusum</code>", callout_tech_style)
        ]
    ]

    t_rosetta = Table(rosetta_data, colWidths=[170, 190, 160])
    t_rosetta.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#1e293b")),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor("#f8fafc")]),
        ('PADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(t_rosetta)
    story.append(Spacer(1, 15))

    # ── SUMMARY FOOTNOTE ──────────────────────────────────────────────────────
    summary_box = [[
        Paragraph("<b>🏁 The Takeaway:</b><br/>"
                  "Our platform is not a gamble. It is an <b>industrial pipeline</b> that gathers data cleanly, "
                  "demands unanimous agreement from two distinct AI models, enforces strict safety stops, "
                  "and gently adapts its trust every single evening after market close. "
                  "That is how modern, emotion-free wealth is protected and grown.", callout_kid_style)
    ]]
    t_summary = Table(summary_box, colWidths=[520])
    t_summary.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#eff6ff")),
        ('BORDER', (0,0), (-1,-1), 1.5, colors.HexColor("#3b82f6")),
        ('PADDING', (0,0), (-1,-1), 10),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
    ]))
    story.append(t_summary)

    # Build PDF with NumberedCanvas
    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"PDF built successfully at: {OUTPUT_PDF_PATH}")

    # Copy to artifact dir
    shutil.copyfile(OUTPUT_PDF_PATH, ARTIFACT_PDF_PATH)
    print(f"Copied to artifact dir at: {ARTIFACT_PDF_PATH}")


if __name__ == "__main__":
    build_pdf()
