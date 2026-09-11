import React, { useState } from 'react';
import { 
  HelpCircle, BookOpen, Compass, ShieldAlert, Cpu, Database, Activity, 
  Target, Search, Layers, Zap, TrendingUp, AlertTriangle, FileText, 
  CheckCircle2, ChevronRight, ExternalLink, Terminal, Sliders, Play, 
  Send, Lock, Info, BarChart2
} from 'lucide-react';
import { Link } from 'react-router-dom';

export default function HelpCenter() {
  const [activeTab, setActiveTab] = useState('getting-started');
  const [searchQuery, setSearchQuery] = useState('');

  const sections = [
    { id: 'getting-started', label: '1. Getting Started', icon: Compass },
    { id: 'dashboard', label: '2. Dashboard', icon: Activity },
    { id: 'watchlist-scanner', label: '3. Watchlist Scanner', icon: Search },
    { id: 'intraday-scanner', label: '4. Intraday Scanner', icon: Zap },
    { id: 'swing-scanner', label: '5. Swing Scanner', icon: Target },
    { id: 'ai-brain', label: '6. AI Brain & Models', icon: Cpu },
    { id: 'data-lab', label: '7. Data Lab & Research', icon: Database },
    { id: 'model-lab', label: '8. Model Lab & Challenger', icon: Layers },
    { id: 'trade-history', label: '9. AI Memory & Trades', icon: FileText },
    { id: 'autopilot', label: '10. Autopilot', icon: Play },
    { id: 'risk-management', label: '11. Risk & Kelly Sizing', icon: ShieldAlert },
    { id: 'fii-dii', label: '12. FII / DII Flows', icon: TrendingUp },
    { id: 'system-audit', label: '13. Master Audit & Logs', icon: Terminal },
    { id: 'telegram-reports', label: '14. Telegram Reports', icon: Send },
    { id: 'which-tool', label: '15. Which Tool Should I Use?', icon: BookOpen },
    { id: 'troubleshooting', label: '16. Troubleshooting & FAQs', icon: AlertTriangle },
    { id: 'glossary', label: '17. Plain-English Glossary', icon: HelpCircle },
    { id: 'safety-warnings', label: '18. Safety & Risk Guidance', icon: Lock }
  ];

  const filteredSections = sections.filter(s => 
    s.label.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <div className="min-h-screen bg-slate-50/60 pb-20 text-slate-800">
      {/* Top Banner */}
      <div className="bg-slate-900 text-white border-b border-slate-800 px-6 py-8 shadow-sm">
        <div className="max-w-7xl mx-auto flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2.5">
              <span className="bg-indigo-500 text-white p-2 rounded-xl shadow-sm">
                <BookOpen size={22} />
              </span>
              <h1 className="text-2xl font-black tracking-tight text-white">
                Platform User Guide & Knowledge Center
              </h1>
              <span className="text-[11px] font-bold uppercase tracking-wider bg-indigo-950/80 text-indigo-300 border border-indigo-700/50 px-2.5 py-0.5 rounded-md">
                v2.4 Production
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-2 max-w-2xl">
              Complete, beginner-friendly operating instructions, screen-by-screen walkthroughs, button-by-button catalogs, and quantitative risk guidelines for the Swing Trading AI platform.
            </p>
          </div>

          <div className="flex items-center gap-3">
            <Link 
              to="/" 
              className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-xl text-xs font-bold transition flex items-center gap-1.5 border border-slate-700"
            >
              Back to Dashboard <ChevronRight size={14} />
            </Link>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-6 mt-8">
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-8">
          
          {/* ── SIDEBAR NAVIGATION ───────────────────────────────────── */}
          <div className="lg:col-span-1 space-y-4">
            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs sticky top-4">
              <div className="mb-3">
                <input
                  type="text"
                  placeholder="Search guide topics..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full text-xs bg-slate-50 border border-slate-200 rounded-xl px-3.5 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-500 font-medium"
                />
              </div>

              <div className="space-y-1 max-h-[70vh] overflow-y-auto pr-1">
                {filteredSections.map((sec) => {
                  const Icon = sec.icon;
                  const isActive = activeTab === sec.id;
                  return (
                    <button
                      key={sec.id}
                      onClick={() => setActiveTab(sec.id)}
                      className={`w-full text-left px-3 py-2 rounded-xl text-xs font-semibold flex items-center gap-2.5 transition ${
                        isActive
                          ? 'bg-indigo-600 text-white shadow-xs'
                          : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                      }`}
                    >
                      <Icon size={15} className={isActive ? 'text-white' : 'text-slate-400'} />
                      <span className="truncate">{sec.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          {/* ── MAIN CONTENT DISPLAY ─────────────────────────────────── */}
          <div className="lg:col-span-3 space-y-8">

            {/* 1. GETTING STARTED */}
            {activeTab === 'getting-started' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Compass className="text-indigo-600" /> Getting Started
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">
                    Introduction to the platform architecture, navigation, and safe operational guidelines.
                  </p>
                </div>

                <div className="space-y-4 text-sm text-slate-600 leading-relaxed">
                  <h3 className="font-bold text-slate-900 text-base">What is this platform?</h3>
                  <p>
                    This is an institutional quantitative trading terminal and AI research laboratory designed for the Indian equity market (NSE). It combines statistical machine learning models (Random Forest, Gradient Boosting, Support Vector Machines, and DoubleEnsemble neural models) with rigorous risk-control mathematical gates (Kelly sizing and portfolio heat caps).
                  </p>

                  <div className="p-4 bg-blue-50/70 border border-blue-200 rounded-xl text-xs text-blue-900 space-y-2">
                    <span className="font-bold flex items-center gap-1.5 text-blue-950">
                      <Info size={16} /> The Four Operational Tiers Explained:
                    </span>
                    <ul className="list-disc pl-5 space-y-1 text-[11px] text-blue-800">
                      <li><strong>Tier 1 — Research & Simulation:</strong> Run backtests and walk-forward simulations in the Data Lab. These tests run in complete isolation and never place orders or alter trade history.</li>
                      <li><strong>Tier 2 — Virtual Scanner Recommendations:</strong> When Intraday or Swing Scanners identify high-conviction trades, they are saved as virtual recommendations with <code>NOT_A_POSITION</code>. They consume 0% portfolio heat.</li>
                      <li><strong>Tier 3 — Paper Positions:</strong> Simulated forward trades that track realistic entries, exits, slippage, and PnL without risking real capital.</li>
                      <li><strong>Tier 4 — Live Broker Execution:</strong> Real order transmission to an authorized exchange broker (Indmoney / Zerodha). <em>By default, this is protected in fail-safe simulation mode with 0 live broker orders permitted.</em></li>
                    </ul>
                  </div>

                  <h3 className="font-bold text-slate-900 text-base mt-6">Navigation Layout</h3>
                  <p>
                    Use the dark sidebar on the left to navigate between modules. On mobile screens, tap the top menu icon to reveal the navigation drawer.
                  </p>
                </div>
              </div>
            )}

            {/* 2. DASHBOARD */}
            {activeTab === 'dashboard' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Activity className="text-blue-600" /> Dashboard Walkthrough
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">The primary market intelligence control center.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">WHAT IS THIS?</h3>
                    <p className="mt-1">
                      The Dashboard is your morning and intraday bird's-eye view of the Indian stock market. It aggregates macroeconomic regime conditions, NIFTY benchmark trends, India VIX volatility, market advance/decline breadth, official NSE FII/DII institutional disclosures, and composite intermarket risk radar.
                    </p>
                  </div>

                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">WHEN SHOULD I USE IT?</h3>
                    <p className="mt-1">
                      Always check the Dashboard first before looking for individual stock setups. If the Macro Regime is <strong>BEARISH</strong> or India VIX has spiked, the AI Decision Engine penalizes long trades and tightens risk parameters.
                    </p>
                  </div>

                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">WHAT SHOULD I PRESS? (Button Catalog)</h3>
                    <div className="space-y-3 mt-2">
                      <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                        <span className="font-bold font-mono text-xs text-blue-700">Refresh All Data</span>
                        <p className="text-[11px] mt-1">Forces an immediate recalculation of the live dashboard snapshot from canonical databases and verified market feeds.</p>
                      </div>
                      <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                        <span className="font-bold font-mono text-xs text-blue-700">Download PDF Report</span>
                        <p className="text-[11px] mt-1">Generates and downloads a multi-page, publication-grade Daily Market Intelligence PDF containing institutional flow analysis, market breadth, and sector performance.</p>
                      </div>
                      <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                        <span className="font-bold font-mono text-xs text-blue-700">Send to Telegram</span>
                        <p className="text-[11px] mt-1">Manually triggers verified PDF document dispatch to your configured Telegram channel. Duplicate protection prevents sending twice on the same day unless forced.</p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* 3. WATCHLIST SCANNER */}
            {activeTab === 'watchlist-scanner' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Search className="text-indigo-600" /> Watchlist Scanner Terminal
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">High-speed parallel quantitative screening terminal with zero local storage.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">WHAT IS THIS?</h3>
                    <p className="mt-1">
                      A fast, parallel multi-factor screener. It evaluates stocks against technical regime scores, volume surge ratios (current volume vs 20-day SMA volume), setup classifications (A+ Breakouts, Pullbacks, Oversold Rebounds), and queries the Authoritative Production Champion Decision Engine for AI conviction percentages.
                    </p>
                  </div>

                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">BUTTON-BY-BUTTON GUIDE</h3>
                    <div className="space-y-3 mt-2">
                      <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                        <span className="font-bold font-mono text-xs text-slate-900">Preset Tabs (My Watchlist, NIFTY 50, Bank Nifty, NIFTY IT, LIVE 52)</span>
                        <p className="text-[11px] mt-1">Switches the target scanning pool with one click. Presets are resolved dynamically by the backend universe engine.</p>
                      </div>
                      <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                        <span className="font-bold font-mono text-xs text-blue-700">Add to Watchlist</span>
                        <p className="text-[11px] mt-1">Adds a symbol directly to the backend SQLite database (<code>user_watchlist</code> table). Stored permanently on the server with zero browser localStorage dependency.</p>
                      </div>
                      <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                        <span className="font-bold font-mono text-xs text-blue-700">Inspect</span>
                        <p className="text-[11px] mt-1">Opens the slide-out deep inspection drawer to view ATR stop-loss levels, dynamic targets, and informational Half-Kelly position sizing.</p>
                      </div>
                      <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                        <span className="font-bold font-mono text-xs text-slate-900">Send to Intraday / Swing Scanner</span>
                        <p className="text-[11px] mt-1">Transfers the symbol to the deep ML scanner for full multi-timeframe feature processing. <em>Never creates a broker order directly.</em></p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* 4. INTRADAY SCANNER */}
            {activeTab === 'intraday-scanner' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Zap className="text-amber-500" /> Intraday ML Scanner
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">15-minute candle machine learning sweep across liquid Indian momentum equities.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">WHAT IS THIS?</h3>
                    <p className="mt-1">
                      The Intraday ML Scanner examines 60 days of 15-minute historical bars for each stock in the chosen universe. It extracts technical momentum features, evaluates them against the Intraday Champion Model, applies the Meta-Learner arbitrator, and outputs qualified intraday setups with dynamic 1.5x ATR stops and 2.0x ATR targets.
                    </p>
                  </div>

                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">WHAT HAPPENS WHEN I CLICK "RUN INTRADAY ML SWEEP"?</h3>
                    <ol className="list-decimal pl-5 space-y-1.5 mt-2">
                      <li>The Macro Regime Engine analyzes NIFTY 50 (20 EMA) and INDIA VIX to establish directional bias.</li>
                      <li>Candidate pool is loaded from the backend (NIFTY 500, LIVE 52, or WATCHLIST).</li>
                      <li>15-minute candles are processed in a streaming pipeline with live terminal logging.</li>
                      <li>Each symbol is evaluated by the Shared Decision Engine. Only signals with calibrated confidence ≥ 60% qualify.</li>
                    </ol>
                  </div>
                </div>
              </div>
            )}

            {/* 5. SWING SCANNER */}
            {activeTab === 'swing-scanner' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Target className="text-indigo-600" /> Swing ML Scanner
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Daily candle swing trading model evaluating 5-year historical market structure.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">WHAT IS THIS?</h3>
                    <p className="mt-1">
                      Designed for multi-day position holding (typically 3 to 15 trading sessions). It analyzes daily candles (1D) over 5 years, computing macro alignment (200 SMA), ADX trend strength, and multi-model consensus from the Swing Champion Ensemble.
                    </p>
                  </div>

                  <div>
                    <h3 className="font-bold text-slate-900 text-sm">WHEN SHOULD I USE IT?</h3>
                    <p className="mt-1">
                      Run this scanner during the market close (around 15:15 to 15:30 IST) or after market hours to identify overnight swing candidates for the next trading day.
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* 6. AI BRAIN & DECISION ENGINE */}
            {activeTab === 'ai-brain' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Cpu className="text-purple-600" /> AI Brain & Decision Engine Architecture
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Understanding how models vote, arbitrate, and calibrate predictions.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <p>
                    The platform does not rely on a single model. It operates a multi-layer decision pipeline:
                  </p>

                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                      <span className="font-bold text-slate-900 block">1. Base Models</span>
                      <p className="text-[11px] text-slate-500 mt-1">Random Forest, Gradient Boosting, and Support Vector Classifier independently score probability.</p>
                    </div>
                    <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                      <span className="font-bold text-slate-900 block">2. Meta-Learner</span>
                      <p className="text-[11px] text-slate-500 mt-1">Arbitrates between model disagreements based on current volatility and historical model accuracy in that regime.</p>
                    </div>
                    <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                      <span className="font-bold text-slate-900 block">3. Probability Calibration</span>
                      <p className="text-[11px] text-slate-500 mt-1">Isotonic and Platt calibration map raw model confidence to true statistical frequencies.</p>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* 7. DATA LAB & RESEARCH */}
            {activeTab === 'data-lab' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Database className="text-cyan-600" /> 10Y Research Data Lab
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Deep quantitative research, walk-forward testing, and model discovery.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <p>
                    The Data Lab is a sandbox for experimenting with machine learning algorithms over a 10-year historical dataset (511 Indian equities).
                  </p>

                  <div className="p-4 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-900">
                    <strong className="block mb-1">Crucial Research Rule:</strong>
                    Research runs in an isolated workspace. No research experiment can ever modify or overwrite the production Champion model without passing the formal Challenger Promotion Gate.
                  </div>
                </div>
              </div>
            )}

            {/* 8. MODEL LAB & CHALLENGER */}
            {activeTab === 'model-lab' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Layers className="text-indigo-600" /> Model Lab & Challenger Governance
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Champion vs Challenger governance and strict out-of-sample promotion gates.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <h3 className="font-bold text-slate-900 text-sm">How does a model become Production Champion?</h3>
                  <p>
                    A new AI model (such as Qlib DoubleEnsemble or TimesFM) is treated as a <strong>Challenger</strong>. To replace the incumbent Champion, it must pass three mandatory gates:
                  </p>
                  <ul className="list-disc pl-5 space-y-1">
                    <li><strong>Locked OOS Evaluation:</strong> Tested on out-of-sample data that was never seen during training or tuning.</li>
                    <li><strong>Statistical Superiority:</strong> Must achieve higher Sharpe ratio, higher Win Rate, and lower maximum drawdown than the Champion.</li>
                    <li><strong>Independent Forward Holdout:</strong> Must prove predictive edge over ≥30 verified forward market trades.</li>
                  </ul>
                </div>
              </div>
            )}

            {/* 9. AI MEMORY & TRADES */}
            {activeTab === 'trade-history' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <FileText className="text-emerald-600" /> AI Memory & Trade History
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Authoritative trading journal and historical performance tracking.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <p>
                    Every qualified recommendation is logged into <code>ml_trade_history</code>. The AI Guard continuously monitors active trades, tracking whether price touches Stop Loss, Target 1, Target 2, or squares off at 15:15 IST.
                  </p>
                </div>
              </div>
            )}

            {/* 10. AUTOPILOT */}
            {activeTab === 'autopilot' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Play className="text-rose-600" /> Autopilot Autonomous Sweeper
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Autonomous background screening daemon with fail-closed safety.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <p>
                    Autopilot runs scheduled scans during market hours without manual intervention:
                  </p>
                  <ul className="list-disc pl-5 space-y-1">
                    <li><strong>09:30 IST:</strong> Morning Momentum Sweep</li>
                    <li><strong>11:30 IST:</strong> Mid-Day Continuation Sweep</li>
                    <li><strong>13:30 IST:</strong> Afternoon Breakout Sweep</li>
                  </ul>
                  <p className="mt-2">
                    Autopilot honors the portfolio heat ceiling (max 6.0%). If heat is maxed out, Autopilot automatically blocks new entries and logs a protective risk alert.
                  </p>
                </div>
              </div>
            )}

            {/* 11. RISK & KELLY SIZING */}
            {activeTab === 'risk-management' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <ShieldAlert className="text-rose-600" /> Risk Management & Kelly Sizing
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Mathematical position sizing and capital preservation rules.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <h3 className="font-bold text-slate-900 text-sm">Half-Kelly Position Sizing</h3>
                  <p>
                    Full Kelly formula can be volatile. The platform uses <strong>Half-Kelly (50% multiplier)</strong> to compute mathematically optimal share quantities based on the AI model's win probability and the trade's reward-to-risk ratio.
                  </p>
                  <h3 className="font-bold text-slate-900 text-sm mt-4">Portfolio Heat Ceiling (6.0%)</h3>
                  <p>
                    Total combined risk across all open positions cannot exceed 6.0% of portfolio equity. If 4 positions risk 1.5% each, heat reaches 6.0% and all scanners automatically block new trades until an existing trade closes.
                  </p>
                </div>
              </div>
            )}

            {/* 12. FII / DII FLOWS */}
            {activeTab === 'fii-dii' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <TrendingUp className="text-blue-600" /> FII / DII Institutional Flows
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Interpreting official National Stock Exchange of India (NSE) institutional filings.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <p>
                    Foreign Institutional Investors (FII) and Domestic Institutional Investors (DII) drive macro market liquidity:
                  </p>
                  <ul className="list-disc pl-5 space-y-1">
                    <li><strong>Domestic Absorption:</strong> DII net buying exceeds FII net selling (bullish support).</li>
                    <li><strong>Dual Institutional Inflow:</strong> Both FII and DII are net buyers (strong bullish momentum).</li>
                    <li><strong>Dual Institutional Outflow:</strong> Both are net sellers (high cash preservation regime).</li>
                  </ul>
                  <p className="text-[11px] text-slate-500 mt-2">
                    Note: Values reflect official End-of-Day exchange disclosures and are not real-time tick feeds.
                  </p>
                </div>
              </div>
            )}

            {/* 13. MASTER AUDIT & LOGS */}
            {activeTab === 'system-audit' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Terminal className="text-slate-700" /> Master Audit Log & System Health
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Diagnostic subsystem telemetry and cryptographic integrity checks.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <p>
                    The System Audit page (accessible via <code>/audit</code> in the sidebar) monitors all subsystems in real time: Database WAL mode, APScheduler health, Telegram connectivity, Model hash immutability, and ProcessPool worker lifecycle.
                  </p>
                </div>
              </div>
            )}

            {/* 14. TELEGRAM REPORTS */}
            {activeTab === 'telegram-reports' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <Send className="text-blue-500" /> Daily Telegram Reports
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Automated 08:15 AM IST morning PDF delivery and verification chain.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600 leading-relaxed">
                  <h3 className="font-bold text-slate-900 text-sm">Scheduled Morning Delivery (08:15 IST)</h3>
                  <p>
                    Every trading day (Monday to Friday) at exactly <strong>08:15 AM IST</strong>, the system generates and dispatches the Daily Market Intelligence Report PDF directly to Telegram.
                  </p>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200 space-y-2">
                    <span className="font-bold text-slate-900 block">The 6-Stage Execution Chain:</span>
                    <ol className="list-decimal pl-5 space-y-1 text-[11px]">
                      <li><strong>Morning Data Collection:</strong> Overnights from US and Asian markets gathered.</li>
                      <li><strong>Integrity Check:</strong> Verifies FII/DII disclosures, market breadth baseline, and macro regime. (Naturally unavailable live ticks are marked standby).</li>
                      <li><strong>Market Intelligence Calculation:</strong> Snapshot compiled.</li>
                      <li><strong>PDF Generation:</strong> Publication-grade multi-page document rendered.</li>
                      <li><strong>Deep PDF Validation:</strong> Document parsed with <code>pypdf</code> to confirm size (&gt;15KB), valid pages (&ge;2), and correct title headers.</li>
                      <li><strong>Telegram Dispatch:</strong> Document delivered with summary caption and deduplication.</li>
                    </ol>
                  </div>
                </div>
              </div>
            )}

            {/* 15. WHICH TOOL SHOULD I USE? */}
            {activeTab === 'which-tool' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <BookOpen className="text-indigo-600" /> "Which Tool Should I Use?" Decision Guide
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Quick visual cheat sheet mapping your intention to the right screen.</p>
                </div>

                <div className="space-y-3 text-xs">
                  <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 flex items-center justify-between">
                    <div>
                      <span className="font-bold text-slate-900 block">"I want to know what is happening in the broad market today"</span>
                      <span className="text-slate-500 text-[11px]">Check market regime, India VIX, FII/DII institutional flows, and intermarket risk.</span>
                    </div>
                    <Link to="/" className="px-3 py-1.5 bg-blue-600 text-white font-bold rounded-lg shrink-0">Open Dashboard</Link>
                  </div>

                  <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 flex items-center justify-between">
                    <div>
                      <span className="font-bold text-slate-900 block">"I want to quickly check my personal stocks for volume surges and setup tags"</span>
                      <span className="text-slate-500 text-[11px]">Screen your personal database watchlist or NIFTY 50 / Bank Nifty presets in parallel.</span>
                    </div>
                    <Link to="/scanner" className="px-3 py-1.5 bg-indigo-600 text-white font-bold rounded-lg shrink-0">Open Watchlist Terminal</Link>
                  </div>

                  <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 flex items-center justify-between">
                    <div>
                      <span className="font-bold text-slate-900 block">"I want intraday trade setups (15m bars, enter and exit today)"</span>
                      <span className="text-slate-500 text-[11px]">Run the 15-minute machine learning sweep across liquid momentum stocks.</span>
                    </div>
                    <Link to="/ai-scan" className="px-3 py-1.5 bg-amber-600 text-white font-bold rounded-lg shrink-0">Open Intraday ML</Link>
                  </div>

                  <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 flex items-center justify-between">
                    <div>
                      <span className="font-bold text-slate-900 block">"I want multi-day swing opportunities (holding 3 to 15 days)"</span>
                      <span className="text-slate-500 text-[11px]">Scan daily candles using the 5-year trend alignment model.</span>
                    </div>
                    <Link to="/swing-scan" className="px-3 py-1.5 bg-emerald-600 text-white font-bold rounded-lg shrink-0">Open Swing ML</Link>
                  </div>

                  <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 flex items-center justify-between">
                    <div>
                      <span className="font-bold text-slate-900 block">"I want to see why the AI rejected a setup or understand model accuracy"</span>
                      <span className="text-slate-500 text-[11px]">Inspect calibration curves, base model probabilities, and feature importances.</span>
                    </div>
                    <Link to="/ml-lab" className="px-3 py-1.5 bg-purple-600 text-white font-bold rounded-lg shrink-0">Open AI Brain & Lab</Link>
                  </div>
                </div>
              </div>
            )}

            {/* 16. TROUBLESHOOTING & FAQS */}
            {activeTab === 'troubleshooting' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <AlertTriangle className="text-amber-500" /> Troubleshooting & FAQs
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Frequently asked questions and error message resolution.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-600">
                  <div className="p-4 bg-slate-50 rounded-xl border border-slate-200">
                    <h4 className="font-bold text-slate-900">Why did the scanner show 0 qualified setups today?</h4>
                    <p className="mt-1">
                      This is normal and protective behavior. If the market is choppy, volatile, or macro regime is Bearish, the Decision Engine filters out marginal setups. In quantitative trading, avoiding bad trades is just as important as finding winners.
                    </p>
                  </div>

                  <div className="p-4 bg-slate-50 rounded-xl border border-slate-200">
                    <h4 className="font-bold text-slate-900">What does "Data Freshness: STALE" mean?</h4>
                    <p className="mt-1">
                      It indicates the historical candles for that stock have not received new updates in over 3 days (e.g. over long weekends or if an equity has low trading frequency). The system warns you instead of pretending the data is live.
                    </p>
                  </div>

                  <div className="p-4 bg-slate-50 rounded-xl border border-slate-200">
                    <h4 className="font-bold text-slate-900">Can Watchlist Scanner execute real live broker orders?</h4>
                    <p className="mt-1">
                      No. Watchlist Scanner is strictly a quantitative analysis terminal. It does not place orders, does not consume portfolio heat, and does not alter production trade history.
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* 17. GLOSSARY */}
            {activeTab === 'glossary' && (
              <div className="bg-white border border-slate-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-slate-100 pb-4">
                  <h2 className="text-xl font-black text-slate-900 flex items-center gap-2">
                    <HelpCircle className="text-indigo-600" /> Plain-English Quantitative Glossary
                  </h2>
                  <p className="text-xs text-slate-500 mt-1">Clear, unpretentious explanations of trading and machine learning terminology.</p>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Champion Model</span>
                    <p className="text-[11px] text-slate-600 mt-1">The currently deployed, authoritative AI ensemble running production scans. Its binary weights are cryptographically verified and immutable.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Challenger Model</span>
                    <p className="text-[11px] text-slate-600 mt-1">A candidate AI model being evaluated in research. It cannot touch live trading until proving superiority across multiple locked out-of-sample tests.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Out-of-Sample (OOS)</span>
                    <p className="text-[11px] text-slate-600 mt-1">A portion of market data kept in a vault while training models. Evaluating on OOS data tests if the model actually learned real market patterns or just memorized the past.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Sharpe Ratio</span>
                    <p className="text-[11px] text-slate-600 mt-1">A measure of return produced relative to the volatility taken. Higher is better, but it should always be considered alongside drawdown.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Maximum Drawdown</span>
                    <p className="text-[11px] text-slate-600 mt-1">The largest peak-to-trough percentage drop in account equity. Tells you how painful a losing streak could be.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Portfolio Heat</span>
                    <p className="text-[11px] text-slate-600 mt-1">The sum total percentage of your capital at risk across all simultaneously open trades if all stop-losses were hit. Capped at 6.0%.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Half-Kelly</span>
                    <p className="text-[11px] text-slate-600 mt-1">A position-sizing formula that allocates 50% of the mathematically optimal Kelly fraction, balancing growth with safety.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Average True Range (ATR)</span>
                    <p className="text-[11px] text-slate-600 mt-1">A measure of how much a stock normally moves in a day or candle. Used to place volatility-adjusted stop losses.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Volume Surge</span>
                    <p className="text-[11px] text-slate-600 mt-1">How many times higher today's volume is compared to its 20-day average. A surge of 2.0x indicates strong institutional participation.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Slippage & Friction</span>
                    <p className="text-[11px] text-slate-600 mt-1">The difference between the theoretical price and actual execution price due to market latency and bid-ask spreads.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Data Leakage</span>
                    <p className="text-[11px] text-slate-600 mt-1">A rookie algorithmic error where future data accidentally leaks into the training set, causing unrealistic simulated results.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Point-in-Time Integrity</span>
                    <p className="text-[11px] text-slate-600 mt-1">Ensuring calculations at 10:00 AM only use information that truly existed at 10:00 AM.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Alpha158 / Alpha360</span>
                    <p className="text-[11px] text-slate-600 mt-1">Standardized factor formulas developed by Microsoft Qlib for calculating volume, volatility, momentum, and return features.</p>
                  </div>

                  <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                    <span className="font-bold text-indigo-700 block">Meta-Learner</span>
                    <p className="text-[11px] text-slate-600 mt-1">A supervisory machine learning layer that arbitrates between multiple underlying models based on market volatility regime.</p>
                  </div>
                </div>
              </div>
            )}

            {/* 18. SAFETY & RISK GUIDANCE */}
            {activeTab === 'safety-warnings' && (
              <div className="bg-white border border-rose-200 rounded-2xl p-6 shadow-xs space-y-6">
                <div className="border-b border-rose-100 pb-4">
                  <h2 className="text-xl font-black text-rose-700 flex items-center gap-2">
                    <Lock className="text-rose-600" /> Mandatory Safety & Production Invariants
                  </h2>
                  <p className="text-xs text-rose-500 mt-1">Essential safeguards protecting real capital and system reliability.</p>
                </div>

                <div className="space-y-4 text-xs text-slate-700 leading-relaxed">
                  <div className="p-4 bg-rose-50 border border-rose-200 rounded-xl space-y-2">
                    <span className="font-bold text-rose-900 block text-sm">Key Operating Rules:</span>
                    <ul className="list-disc pl-5 space-y-1.5 text-rose-800">
                      <li><strong>Research Experiments Never Mutate Champions:</strong> Training a new model in Data Lab or Optuna tuning never changes the production Champion model. Promotion requires explicit audit approval.</li>
                      <li><strong>Scanner Recommendation &ne; Live Position:</strong> Just because a stock appears on Intraday or Swing scanner with 75% confidence does not mean an order was placed. Recommendations are virtual until executed.</li>
                      <li><strong>Virtual Recommendations Consume 0% Heat:</strong> Tracked trade recommendations with <code>NOT_A_POSITION</code> do not lock your capital ceiling.</li>
                      <li><strong>Never Lower Thresholds for Trade Volume:</strong> Lowering confidence thresholds below 60% merely generates low-quality noise and degrades win rates.</li>
                      <li><strong>Holdout Data is Vaulted:</strong> Forward holdout periods must remain strictly locked to prevent data overfitting.</li>
                    </ul>
                  </div>
                </div>
              </div>
            )}

          </div>
        </div>
      </div>
    </div>
  );
}

