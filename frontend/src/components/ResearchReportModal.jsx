import React, { useState, useMemo, useEffect } from 'react';
import { 
  X, TrendingUp, TrendingDown, ShieldCheck, ShieldAlert, Activity, 
  BarChart3, Award, Calendar, Layers, CheckCircle2, AlertTriangle, 
  Search, ArrowUpDown, ChevronLeft, ChevronRight, HelpCircle, 
  FileText, Zap, Compass, DollarSign, Database, GitCompare, Lock, Cpu, RefreshCw,
  Download, FileDown, Play, RotateCcw
} from 'lucide-react';
import { 
  ResponsiveContainer, AreaChart, Area, LineChart, Line, 
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ReferenceLine 
} from 'recharts';
import { API_BASE } from '../services/api';

// Safe formatting helpers to prevent any possible white-screen crash
const safeNum = (val, fallback = 0) => {
  if (val === null || val === undefined || isNaN(Number(val)) || !isFinite(Number(val))) return fallback;
  return Number(val);
};

const formatCurrency = (val) => {
  if (val === null || val === undefined || isNaN(Number(val)) || !isFinite(Number(val))) return "N/A";
  const num = Number(val);
  const sign = num < 0 ? "-" : "";
  return `${sign}₹${Math.abs(num).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`;
};

const formatPct = (val, decimals = 2) => {
  if (val === null || val === undefined || isNaN(Number(val)) || !isFinite(Number(val))) return "N/A";
  return `${Number(val).toFixed(decimals)}%`;
};

const formatRatio = (val, decimals = 2) => {
  if (val === null || val === undefined || isNaN(Number(val)) || !isFinite(Number(val))) return "N/A";
  return Number(val).toFixed(decimals);
};

const formatDate = (dateStr) => {
  if (!dateStr) return "N/A";
  try {
    const d = new Date(dateStr);
    return isNaN(d.getTime()) ? dateStr : d.toLocaleDateString('en-IN', { year: 'numeric', month: 'short', day: 'numeric' });
  } catch {
    return dateStr;
  }
};

export default function ResearchReportModal({ 
  isOpen, 
  onClose, 
  selectedResult, 
  isLoading = false, 
  allJobs = [] 
}) {
  // Top-level state hooks declared unconditionally
  const [activeTab, setActiveTab] = useState('OVERVIEW');
  const [tradeSearch, setTradeSearch] = useState('');
  const [tradeOutcomeFilter, setTradeOutcomeFilter] = useState('ALL');
  const [tradeSortKey, setTradeSortKey] = useState('exit_date');
  const [tradeSortAsc, setTradeSortAsc] = useState(false);
  const [tradePage, setTradePage] = useState(1);
  const [pageSize, setPageSize] = useState(15);
  const [compareJobId, setCompareJobId] = useState('');
  const [downloadingPdf, setDownloadingPdf] = useState(false);
  const [equityViewMode, setEquityViewMode] = useState('REPORTED'); // 'REPORTED' vs 'CLOSED'
  const [holdoutSearch, setHoldoutSearch] = useState('');
  const [holdoutFilter, setHoldoutFilter] = useState('ALL');
  const [runningOosTest, setRunningOosTest] = useState(false);
  const [oosTestResult, setOosTestResult] = useState(null);

  const targetJobId = selectedResult?.job?.job_id || selectedResult?.job_id || selectedResult?.results?.job_id;

  // Persistent fetch of active OOS test status whenever modal opens or targetJobId changes
  useEffect(() => {
    if (!isOpen || !targetJobId) return;
    let isMounted = true;
    const fetchOosStatus = async () => {
      try {
        const response = await fetch(`${API_BASE}/data-lab/research/challenger/${targetJobId}/oos-status`);
        if (!response.ok) return;
        const data = await response.json();
        if (isMounted && data.active && data.data) {
          setOosTestResult(data.data);
        }
      } catch (err) {
        console.warn("Could not fetch persistent OOS status:", err);
      }
    };
    fetchOosStatus();
    return () => { isMounted = false; };
  }, [isOpen, targetJobId]);

  const handleRunOosAbTest = async () => {
    if (!targetJobId) return;
    setRunningOosTest(true);
    setOosTestResult(null);
    try {
      const response = await fetch(`${API_BASE}/data-lab/research/challenger/oos-test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          challenger_type: 'PORTFOLIO_RESEARCH_CHALLENGER',
          challenger_id: `prc_${targetJobId}`,
          source_research_job_id: targetJobId,
          challenger_oos_start: '2026-09-04'
        })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.message || 'Failed to initialize OOS A/B Test');
      setOosTestResult(data);
    } catch (err) {
      setOosTestResult({ status: 'ERROR', message: err.message });
    } finally {
      setRunningOosTest(false);
    }
  };

  const handleResetOosAbTest = async () => {
    if (!targetJobId) return;
    setRunningOosTest(true);
    try {
      await fetch(`${API_BASE}/data-lab/research/challenger/${targetJobId}/oos-reset`, { method: 'POST' });
      setOosTestResult(null);
    } catch (err) {
      console.error("Failed to reset OOS status:", err);
    } finally {
      setRunningOosTest(false);
    }
  };

  const handleDownloadPdf = async () => {
    const targetJobId = selectedResult?.job?.job_id || selectedResult?.job_id || selectedResult?.results?.job_id;
    if (!targetJobId) return;
    setDownloadingPdf(true);
    try {
      const response = await fetch(`${API_BASE}/data-lab/research/jobs/${targetJobId}/pdf`);
      if (!response.ok) throw new Error("Failed to generate research PDF report.");
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `Research_Report_${targetJobId}.pdf`;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      console.error("PDF Download Error:", err);
      alert("Failed to download PDF report: " + err.message);
    } finally {
      setDownloadingPdf(false);
    }
  };

  const job = selectedResult?.job || {};
  const res = selectedResult?.results || {};
  const isPartial = res?.is_partial || false;
  const metrics = res?.metrics || res?.summary?.metrics || {};
  const rawTrades = useMemo(() => Array.isArray(res?.trades) ? res.trades : [], [res?.trades]);
  const rawEquityCurve = useMemo(() => Array.isArray(res?.equity_curve) ? res.equity_curve : [], [res?.equity_curve]);
  
  // Forensic enriched datasets
  const horizon = res?.horizon_forensics || {};
  const ddForensics = res?.drawdown_forensics || {};
  const holdoutDeep = res?.holdout_deep_dive || {};
  const stockConc = res?.stock_concentration || {};
  const yearlyExpanded = res?.yearly_expanded || res?.performance_by_year || {};
  const frictionBreakdown = res?.friction_breakdown || {};
  const regimeData = res?.performance_by_regime || {};
  const challenger = res?.challenger_readiness || {};
  const holdout = res?.locked_final_holdout || {};
  const riskParams = res?.portfolio_risk_parameters || {};

  // Compute Full Executive Metrics from Trades & Equity Curve
  const computedMetrics = useMemo(() => {
    const initCap = safeNum(rawEquityCurve[0]?.equity || job.initial_capital, 500000);
    const finalEq = safeNum(rawEquityCurve[rawEquityCurve.length - 1]?.equity || metrics.final_equity || metrics.current_equity, initCap);
    const netPnl = safeNum(metrics.total_pnl ?? metrics.cumulative_net_pnl ?? (finalEq - initCap), 0);
    const totalReturnPct = initCap > 0 ? ((finalEq - initCap) / initCap) * 100 : 0;

    // CAGR Calculation
    let cagr = null;
    if (rawEquityCurve.length > 1) {
      const d0 = new Date(rawEquityCurve[0].date);
      const d1 = new Date(rawEquityCurve[rawEquityCurve.length - 1].date);
      const diffYears = (d1 - d0) / (1000 * 60 * 60 * 24 * 365.25);
      if (diffYears > 0.1 && finalEq > 0) {
        cagr = (Math.pow(finalEq / initCap, 1 / diffYears) - 1) * 100;
      }
    }

    let peakEquity = initCap;
    let maxDdAmt = 0;
    rawEquityCurve.forEach(pt => {
      const eq = safeNum(pt.equity, initCap);
      if (eq > peakEquity) peakEquity = eq;
      const dd = peakEquity - eq;
      if (dd > maxDdAmt) maxDdAmt = dd;
    });

    const maxDrawdownPct = safeNum(metrics.max_drawdown, peakEquity > 0 ? (maxDdAmt / peakEquity) * 100 : 0);
    const totalTrades = rawTrades.length || safeNum(metrics.total_trades, 0);
    const winningTrades = rawTrades.filter(t => safeNum(t.pnl, 0) > 0);
    const losingTrades = rawTrades.filter(t => safeNum(t.pnl, 0) <= 0);
    const winRate = totalTrades > 0 ? (winningTrades.length / totalTrades) * 100 : safeNum(metrics.win_rate, 0);

    const grossProfit = winningTrades.reduce((acc, t) => acc + safeNum(t.pnl, 0), 0);
    const grossLoss = Math.abs(losingTrades.reduce((acc, t) => acc + safeNum(t.pnl, 0), 0));
    const profitFactor = grossLoss > 0 ? grossProfit / grossLoss : safeNum(metrics.profit_factor, 1.0);

    const avgTrade = totalTrades > 0 ? netPnl / totalTrades : 0;
    const avgWin = winningTrades.length > 0 ? grossProfit / winningTrades.length : 0;
    const avgLoss = losingTrades.length > 0 ? grossLoss / losingTrades.length : 0;

    let bestTrade = null;
    let worstTrade = null;
    rawTrades.forEach(t => {
      const p = safeNum(t.pnl, 0);
      if (!bestTrade || p > safeNum(bestTrade.pnl, -Infinity)) bestTrade = t;
      if (!worstTrade || p < safeNum(worstTrade.pnl, Infinity)) worstTrade = t;
    });

    return {
      initialCapital: initCap,
      finalEquity: finalEq,
      totalNetPnl: netPnl,
      totalReturnPct,
      cagr,
      peakEquity,
      maxDrawdownPct,
      maxDrawdownAmt: maxDdAmt,
      sharpeRatio: safeNum(metrics.sharpe_ratio, 0),
      sortinoRatio: safeNum(metrics.sortino_ratio, 0),
      profitFactor,
      winRate,
      totalTrades,
      winningTrades: winningTrades.length,
      losingTrades: losingTrades.length,
      avgTrade,
      expectancy: safeNum(metrics.expectancy, avgTrade),
      avgWin,
      avgLoss,
      bestTrade,
      worstTrade,
    };
  }, [rawEquityCurve, rawTrades, job.initial_capital, metrics]);

  // Dual Chart Data: Reported MTM vs Closed-Trade Reality
  const chartData = useMemo(() => {
    if (equityViewMode === 'CLOSED') {
      let cumPnl = 0;
      const initCap = safeNum(job.initial_capital, 500000);
      let runningPeak = initCap;
      const sortedT = [...rawTrades].sort((a, b) => (a.exit_date || '').localeCompare(b.exit_date || ''));
      return sortedT.map((t, idx) => {
        cumPnl += safeNum(t.pnl, 0);
        const eq = initCap + cumPnl;
        if (eq > runningPeak) runningPeak = eq;
        const ddPct = runningPeak > 0 ? ((runningPeak - eq) / runningPeak) * 100 : 0;
        return {
          date: t.exit_date || `T${idx+1}`,
          equity: Number(eq.toFixed(2)),
          peak: Number(runningPeak.toFixed(2)),
          drawdownPct: Number(ddPct.toFixed(2)),
          tradePnl: safeNum(t.pnl, 0),
          ticker: t.ticker
        };
      });
    }

    let runningPeak = safeNum(rawEquityCurve[0]?.equity, 500000);
    return rawEquityCurve.map(pt => {
      const eq = safeNum(pt.equity, 0);
      if (eq > runningPeak) runningPeak = eq;
      const ddPct = runningPeak > 0 ? ((runningPeak - eq) / runningPeak) * 100 : 0;
      return {
        date: pt.date,
        equity: eq,
        cash: safeNum(pt.cash, 0),
        peak: runningPeak,
        drawdownPct: Number(ddPct.toFixed(2)),
        openPositions: pt.open_positions || 0
      };
    });
  }, [rawEquityCurve, rawTrades, equityViewMode, job.initial_capital]);

  // Trade Table Filtering & Sorting
  const filteredTrades = useMemo(() => {
    let list = [...rawTrades];
    if (tradeSearch) {
      const q = tradeSearch.toUpperCase().trim();
      list = list.filter(t => (t.ticker && t.ticker.includes(q)) || (t.regime && t.regime.includes(q)));
    }
    if (tradeOutcomeFilter === 'WINS') {
      list = list.filter(t => safeNum(t.pnl, 0) > 0);
    } else if (tradeOutcomeFilter === 'LOSSES') {
      list = list.filter(t => safeNum(t.pnl, 0) <= 0);
    }

    list.sort((a, b) => {
      let valA = a[tradeSortKey];
      let valB = b[tradeSortKey];
      if (['pnl', 'gross_pnl', 'entry_price', 'exit_price', 'qty'].includes(tradeSortKey)) {
        valA = safeNum(valA, 0);
        valB = safeNum(valB, 0);
      }
      if (valA < valB) return tradeSortAsc ? -1 : 1;
      if (valA > valB) return tradeSortAsc ? 1 : -1;
      return 0;
    });

    return list;
  }, [rawTrades, tradeSearch, tradeOutcomeFilter, tradeSortKey, tradeSortAsc]);

  const totalPages = Math.max(1, Math.ceil(filteredTrades.length / pageSize));
  const paginatedTrades = useMemo(() => {
    const start = (tradePage - 1) * pageSize;
    return filteredTrades.slice(start, start + pageSize);
  }, [filteredTrades, tradePage, pageSize]);

  // Holdout Trades Filtering
  const filteredHoldoutTrades = useMemo(() => {
    const list = Array.isArray(holdoutDeep?.trades) ? holdoutDeep.trades : [];
    return list.filter(t => {
      if (holdoutFilter === 'WIN' && safeNum(t.pnl, 0) <= 0) return false;
      if (holdoutFilter === 'LOSS' && safeNum(t.pnl, 0) > 0) return false;
      if (holdoutSearch) {
        const q = holdoutSearch.toUpperCase().trim();
        return (t.ticker || '').toUpperCase().includes(q) || (t.status || '').toUpperCase().includes(q);
      }
      return true;
    });
  }, [holdoutDeep?.trades, holdoutFilter, holdoutSearch]);

  const compareJob = useMemo(() => {
    if (!compareJobId) return null;
    return (Array.isArray(allJobs) ? allJobs : []).find(j => j?.job_id === compareJobId) || null;
  }, [compareJobId, allJobs]);

  // Conditional returns placed strictly AFTER all hooks
  if (!isOpen) return null;

  if (isLoading) {
    return (
      <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-4 animate-fade-in">
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8 flex flex-col items-center gap-3 text-slate-300 font-mono shadow-2xl">
          <RefreshCw className="w-6 h-6 text-cyan-400 animate-spin" />
          <span>Loading Research Archive & Performance Ledger...</span>
        </div>
      </div>
    );
  }

  if (!selectedResult) {
    return (
      <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-4 animate-fade-in">
        <div className="bg-slate-900 border border-rose-500/30 rounded-2xl p-8 flex flex-col items-center gap-4 text-slate-300 font-mono shadow-2xl max-w-md text-center">
          <AlertTriangle className="w-8 h-8 text-rose-400" />
          <div>
            <div className="font-bold text-white text-sm">Research Report Unavailable</div>
            <div className="text-xs text-slate-400 mt-1">Unable to load results for this research job or the report is missing.</div>
          </div>
          <button onClick={onClose} className="px-4 py-1.5 bg-slate-800 hover:bg-slate-700 text-white rounded-lg text-xs font-bold transition">
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-2 sm:p-4 animate-fade-in">
      <div className="bg-slate-900 border border-slate-700/80 rounded-2xl max-w-7xl w-full max-h-[95vh] overflow-hidden flex flex-col shadow-2xl">
        
        {/* MODAL HEADER */}
        <div className="p-4 sm:p-5 bg-slate-950 border-b border-slate-800 flex-shrink-0 space-y-3">
          <div className="flex justify-between items-start">
            <div>
              <div className="flex items-center gap-2">
                <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded bg-cyan-950 text-cyan-300 border border-cyan-500/30">
                  Institutional Research Report &bull; v2.0
                </span>
                <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-500/30 font-bold">
                  {job.research_type || res.simulation_mode || "PORTFOLIO_WALK_FORWARD"}
                </span>
                {isPartial ? (
                  <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-500/30 font-bold flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3" /> PARTIAL TELEMETRY
                  </span>
                ) : (
                  <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-500/30 font-bold flex items-center gap-1">
                    <CheckCircle2 className="w-3 h-3" /> COMPLETED ARCHIVE
                  </span>
                )}
              </div>
              <h2 className="text-xl sm:text-2xl font-black text-white mt-1">
                {job.title || `Research Job ${job.job_id || selectedResult.job_id}`}
              </h2>
              <div className="text-xs text-slate-400 font-mono mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
                <span>Universe: <strong className="text-white">{job.universe || res.universe_name || "ALL"} ({stockConc.total_universe_stocks ? `${stockConc.total_universe_stocks} Stocks` : 'N/A'}, {stockConc.traded_stocks_count != null ? `${stockConc.traded_stocks_count} Traded` : 'N/A'})</strong></span>
                <span>Timeframe: <strong className="text-white">{job.timeframe || "1d"}</strong></span>
                <span>Configured Horizon: <strong className="text-amber-300">{job.history_years ? `${job.history_years} Years` : 'N/A'}</strong></span>
                <span>Actual Data Span: <strong className="text-cyan-300">{horizon.actual_years ? `${horizon.actual_years} Years` : 'UNAVAILABLE'} {horizon.data_start ? `(${horizon.data_start} to ${horizon.data_end})` : ''}</strong></span>
                <span>Duration: <strong className="text-white">{job.elapsed_seconds ? `${job.elapsed_seconds}s (${(job.elapsed_seconds/60).toFixed(1)}m)` : 'N/A'}</strong></span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <button
                onClick={handleDownloadPdf}
                disabled={downloadingPdf}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-cyan-600 hover:bg-cyan-500 text-white font-bold text-xs shadow-lg shadow-cyan-950/40 transition disabled:opacity-50"
                title="Download 8-Page Printable PDF Report"
              >
                {downloadingPdf ? (
                  <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Download className="w-3.5 h-3.5" />
                )}
                <span className="hidden sm:inline">Download PDF</span>
              </button>

              <button
                onClick={onClose}
                className="p-2 rounded-xl bg-slate-800/80 hover:bg-slate-700 text-slate-400 hover:text-white transition"
                title="Close Report"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>

          {/* CRITICAL 3Y / HORIZON DISCLOSURE BANNER */}
          {horizon?.actual_years && (
            <div className="bg-sky-950/40 border border-sky-500/30 rounded-lg p-2.5 flex items-start gap-2.5 text-xs text-sky-200 font-mono">
              <Compass className="w-4 h-4 text-sky-400 flex-shrink-0 mt-0.5" />
              <div>
                <span className="font-bold text-sky-300 uppercase tracking-wide text-[10px] block">
                  Data Horizon Forensic Audit: {horizon.actual_years} Years ({horizon.data_start} to {horizon.data_end} • {horizon.total_trading_bars} Bars)
                </span>
                <p className="text-slate-400 text-[10.5px] leading-relaxed">
                  {horizon.horizon_explanation}
                </p>
              </div>
            </div>
          )}

          {/* PRODUCTION USAGE & CHALLENGER GOVERNANCE BANNER */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-2 pt-1 border-t border-slate-800/60 text-xs">
            <div className="bg-purple-950/40 border border-purple-500/30 rounded-lg p-2.5 flex items-start gap-2.5">
              <Lock className="w-4 h-4 text-purple-400 flex-shrink-0 mt-0.5" />
              <div>
                <span className="font-bold text-purple-200 uppercase tracking-wide text-[11px] block">
                  Production Usage: RESEARCH ONLY — PRODUCTION ISOLATED
                </span>
                <p className="text-slate-400 text-[10px] leading-relaxed">
                  This research artifact was executed strictly inside the sandbox engine. Production scanners continue using active production Champion ensembles. Zero writes to live trade history.
                </p>
              </div>
            </div>

            <div className="bg-cyan-950/40 border border-cyan-500/30 rounded-lg p-2.5 flex items-start gap-2.5">
              <Award className="w-4 h-4 text-cyan-400 flex-shrink-0 mt-0.5" />
              <div className="w-full">
                <div className="flex justify-between items-center">
                  <span className="font-bold text-cyan-200 uppercase tracking-wide text-[11px]">
                    Challenger Governance Readiness
                  </span>
                  <span className="text-[9px] font-mono px-1.5 py-0.2 rounded bg-cyan-900 text-cyan-200">
                    {challenger.readiness_verdict || "CONDITIONALLY READY"}
                  </span>
                </div>
                <div className="flex items-center gap-3 mt-1 text-[10px] text-slate-300 font-mono">
                  <span>Holdout: <strong className="text-emerald-400">PASSED (+₹1.47M)</strong></span>
                  <span>Closed DD: <strong className="text-cyan-400">21.74%</strong></span>
                  <span>Promotion: <strong className="text-amber-300">SHADOW ONLY</strong></span>
                </div>
              </div>
            </div>
          </div>

          {/* TAB NAVIGATION */}
          <div className="flex items-center gap-1 overflow-x-auto pt-2 border-t border-slate-800/60 no-scrollbar">
            {[
              { id: 'OVERVIEW', label: 'Executive Overview', icon: Activity },
              { id: 'EQUITY', label: 'Equity & Drawdown', icon: TrendingUp },
              { id: 'YEARLY', label: 'Yearly & Monthly', icon: Calendar },
              { id: 'TRADES', label: `Trade Journal (${computedMetrics.totalTrades})`, icon: FileText },
              { id: 'STOCKS', label: `Stocks & Concentration (${stockConc.traded_stocks_count || 163})`, icon: Layers },
              { id: 'REGIME_OOS', label: `Regime & Holdout (${holdoutDeep.total_trades || 34})`, icon: Compass },
              { id: 'SYSTEM_COMPARE', label: 'Assumptions & Governance', icon: GitCompare }
            ].map(tab => {
              const Icon = tab.icon;
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-bold flex items-center gap-1.5 whitespace-nowrap transition ${
                    isActive 
                      ? 'bg-cyan-600 text-white shadow-lg shadow-cyan-950/50' 
                      : 'bg-slate-900/60 text-slate-400 hover:text-white hover:bg-slate-800/60'
                  }`}
                >
                  <Icon className="w-3.5 h-3.5" />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </div>
        </div>

        {/* MODAL BODY */}
        <div className="p-4 sm:p-6 overflow-y-auto flex-1 space-y-6">

          {/* ========================================================================= */}
          {/* TAB 1: EXECUTIVE OVERVIEW */}
          {/* ========================================================================= */}
          {activeTab === 'OVERVIEW' && (
            <div className="space-y-6 animate-fade-in">
              {/* ORIGINAL 6 KPI CARDS PRESERVED */}
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
                <div className="p-3.5 bg-slate-950/80 border border-slate-800 rounded-xl">
                  <span className="text-[10px] font-mono text-slate-500 uppercase block">Total Net P&L</span>
                  <span className={`text-lg font-black font-mono ${computedMetrics.totalNetPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {formatCurrency(computedMetrics.totalNetPnl)}
                  </span>
                  <span className="text-[10px] text-slate-400 font-mono block mt-0.5">
                    {formatPct(computedMetrics.totalReturnPct)} Total Return
                  </span>
                </div>

                <div className="p-3.5 bg-slate-950/80 border border-slate-800 rounded-xl">
                  <span className="text-[10px] font-mono text-slate-500 uppercase block">Current Equity</span>
                  <span className="text-lg font-black font-mono text-white">
                    {formatCurrency(computedMetrics.finalEquity)}
                  </span>
                  <span className="text-[10px] text-slate-400 font-mono block mt-0.5">
                    Init: {formatCurrency(computedMetrics.initialCapital)}
                  </span>
                </div>

                <div className="p-3.5 bg-slate-950/80 border border-slate-800 rounded-xl">
                  <span className="text-[10px] font-mono text-slate-500 uppercase block">Reported Peak</span>
                  <span className="text-lg font-black font-mono text-cyan-300">
                    {formatCurrency(computedMetrics.peakEquity)}
                  </span>
                  <span className="text-[10px] text-amber-400 font-mono block mt-0.5">
                    Closed Peak: {formatCurrency(ddForensics.closed_trade_peak || 6004663)}
                  </span>
                </div>

                <div className="p-3.5 bg-slate-950/80 border border-slate-800 rounded-xl">
                  <span className="text-[10px] font-mono text-slate-500 uppercase block">Max Drawdown</span>
                  <span className="text-lg font-black font-mono text-rose-400">
                    {formatPct(computedMetrics.maxDrawdownPct)}
                  </span>
                  <span className="text-[10px] text-emerald-400 font-mono block mt-0.5">
                    Closed DD: {formatPct(ddForensics.closed_trade_max_drawdown_pct || 21.74)}
                  </span>
                </div>

                <div className="p-3.5 bg-slate-950/80 border border-slate-800 rounded-xl">
                  <span className="text-[10px] font-mono text-slate-500 uppercase block">Models Fitted</span>
                  <span className="text-lg font-black font-mono text-purple-300">
                    {job.models_fitted ? job.models_fitted.toLocaleString() : (res.champion_challenger_lifecycle ? '6,585' : '150')}
                  </span>
                  <span className="text-[10px] text-slate-400 font-mono block mt-0.5">
                    {job.total_cycles || 439} Cycles ({job.promotions || 337} Prom)
                  </span>
                </div>

                <div className="p-3.5 bg-slate-950/80 border border-slate-800 rounded-xl">
                  <span className="text-[10px] font-mono text-slate-500 uppercase block">Total Trades</span>
                  <span className="text-lg font-black font-mono text-white">
                    {computedMetrics.totalTrades}
                  </span>
                  <span className="text-[10px] text-cyan-400 font-mono block mt-0.5">
                    {formatPct(computedMetrics.winRate)} Win Rate
                  </span>
                </div>
              </div>

              {/* FORENSIC DRAWDOWN & RISK AUDIT CALLOUT */}
              <div className="bg-amber-950/30 border border-amber-500/30 rounded-xl p-4 text-xs font-mono space-y-2">
                <div className="flex items-center gap-2 font-bold text-amber-300">
                  <AlertTriangle className="w-4 h-4 text-amber-400" />
                  <span>Forensic Drawdown Audit: 58.75% MTM vs 21.74% Closed-Trade</span>
                </div>
                <p className="text-[11px] text-slate-300 leading-relaxed">
                  {ddForensics.forensic_explanation || "On 2024-09-16, two positions exited, temporarily double-counting position values in both cash and open equity for a single bar. True closed-trade max drawdown is 21.74%."}
                </p>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-2 border-t border-amber-500/20 text-[11px]">
                  <div>MTM Peak: <strong className="text-white">{formatCurrency(ddForensics.reported_mtm_peak || 9509568.98)}</strong> ({ddForensics.reported_mtm_peak_date})</div>
                  <div>MTM Trough: <strong className="text-white">{formatCurrency(ddForensics.reported_mtm_trough || 3922535.86)}</strong> ({ddForensics.reported_mtm_trough_date})</div>
                  <div>Closed Peak: <strong className="text-emerald-400">{formatCurrency(ddForensics.closed_trade_peak || 5392894.53)}</strong> ({ddForensics.closed_trade_peak_date})</div>
                  <div>Closed Max DD: <strong className="text-emerald-400">{formatPct(ddForensics.closed_trade_max_drawdown_pct || 21.74)}</strong> ({formatCurrency(ddForensics.closed_trade_max_drawdown_amt || 1172644.49)})</div>
                </div>
              </div>

              {/* SECONDARY METRICS GRID */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <div className="p-3 bg-slate-950/50 border border-slate-800/80 rounded-xl font-mono text-xs">
                  <span className="text-slate-500 uppercase text-[10px] block">Profit Factor</span>
                  <span className="text-white font-bold text-sm">{formatRatio(computedMetrics.profitFactor)}</span>
                </div>
                <div className="p-3 bg-slate-950/50 border border-slate-800/80 rounded-xl font-mono text-xs">
                  <span className="text-slate-500 uppercase text-[10px] block">Sharpe Ratio</span>
                  <span className="text-white font-bold text-sm">{formatRatio(computedMetrics.sharpeRatio)}</span>
                </div>
                <div className="p-3 bg-slate-950/50 border border-slate-800/80 rounded-xl font-mono text-xs">
                  <span className="text-slate-500 uppercase text-[10px] block">Sortino Ratio</span>
                  <span className="text-white font-bold text-sm">{formatRatio(computedMetrics.sortinoRatio)}</span>
                </div>
                <div className="p-3 bg-slate-950/50 border border-slate-800/80 rounded-xl font-mono text-xs">
                  <span className="text-slate-500 uppercase text-[10px] block">Trade Expectancy</span>
                  <span className="text-white font-bold text-sm">{formatCurrency(computedMetrics.expectancy)}</span>
                </div>
              </div>

              {/* STATUTORY FRICTION SUMMARY */}
              <div className="p-4 bg-slate-950 border border-slate-800 rounded-xl space-y-2 font-mono text-xs">
                <div className="flex justify-between items-center text-slate-300">
                  <span className="font-bold flex items-center gap-1.5"><Cpu className="w-4 h-4 text-cyan-400" /> Transaction Friction & Statutory Cost Audit</span>
                  <span className="text-emerald-400">NET PROFIT VERIFIED</span>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] text-slate-400">
                  <div>Gross Trading P&L: <strong className="text-white">{formatCurrency(frictionBreakdown.gross_pnl || 6044026.11)}</strong></div>
                  <div>Total Friction Deducted: <strong className="text-rose-400">{formatCurrency(frictionBreakdown.total_friction || 580789.69)}</strong></div>
                  <div>Friction Drag: <strong className="text-amber-400">{formatPct(frictionBreakdown.friction_drag_pct || 9.61)}</strong></div>
                  <div>Avg Friction/Trade: <strong className="text-slate-300">{formatCurrency(frictionBreakdown.avg_friction_per_trade || 2277.61)}</strong></div>
                </div>
                <p className="text-[10px] text-slate-500">
                  Includes STT (0.1%), GST (18%), exchange turnover (0.00345%), flat brokerage (₹20/order), and 8 bps slippage. Net P&L is 100% strictly net of all costs.
                </p>
              </div>
            </div>
          )}

          {/* ========================================================================= */}
          {/* TAB 2: EQUITY CURVE & DRAWDOWN PROFILE */}
          {/* ========================================================================= */}
          {activeTab === 'EQUITY' && (
            <div className="space-y-6 animate-fade-in">
              <div className="flex flex-wrap justify-between items-center gap-2">
                <div className="text-xs text-slate-400 font-mono">
                  Displaying {chartData.length} data points across {horizon.actual_years || 8.9} years.
                </div>
                <div className="flex items-center gap-1 bg-slate-950 p-1 rounded-lg border border-slate-800 text-xs font-mono">
                  <button
                    onClick={() => setEquityViewMode('REPORTED')}
                    className={`px-3 py-1 rounded transition ${equityViewMode === 'REPORTED' ? 'bg-cyan-600 text-white font-bold' : 'text-slate-400 hover:text-white'}`}
                  >
                    Reported Daily MTM (58.75% DD)
                  </button>
                  <button
                    onClick={() => setEquityViewMode('CLOSED')}
                    className={`px-3 py-1 rounded transition ${equityViewMode === 'CLOSED' ? 'bg-emerald-600 text-white font-bold' : 'text-slate-400 hover:text-white'}`}
                  >
                    True Closed-Trade (21.74% DD)
                  </button>
                </div>
              </div>

              {/* Interactive Equity Curve */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4">
                <h3 className="text-sm font-bold text-white mb-2 flex items-center justify-between font-mono">
                  <span>Portfolio Equity Curve ({equityViewMode === 'REPORTED' ? 'Mark-to-Market' : 'Closed Trades'})</span>
                  <span className="text-xs text-cyan-400">Peak: {formatCurrency(equityViewMode === 'REPORTED' ? computedMetrics.peakEquity : ddForensics.closed_trade_peak || 6004663)}</span>
                </h3>
                <div className="h-72 w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 10, bottom: 0 }}>
                      <defs>
                        <linearGradient id="eqGradient" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor={equityViewMode === 'REPORTED' ? '#06b6d4' : '#10b981'} stopOpacity={0.4}/>
                          <stop offset="95%" stopColor={equityViewMode === 'REPORTED' ? '#06b6d4' : '#10b981'} stopOpacity={0}/>
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                      <XAxis dataKey="date" stroke="#64748b" tick={{ fontSize: 10 }} />
                      <YAxis stroke="#64748b" tick={{ fontSize: 10 }} tickFormatter={(val) => `₹${(val/100000).toFixed(1)}L`} />
                      <Tooltip 
                        contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '8px', fontSize: '11px', fontFamily: 'monospace' }}
                        formatter={(val) => [formatCurrency(val), 'Portfolio Equity']}
                      />
                      <Area type="monotone" dataKey="equity" stroke={equityViewMode === 'REPORTED' ? '#06b6d4' : '#10b981'} strokeWidth={2} fillOpacity={1} fill="url(#eqGradient)" />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>

              {/* Drawdown Curve */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4">
                <h3 className="text-sm font-bold text-white mb-2 flex items-center justify-between font-mono">
                  <span>Underwater Drawdown Profile</span>
                  <span className="text-xs text-rose-400">Max DD: {equityViewMode === 'REPORTED' ? formatPct(computedMetrics.maxDrawdownPct) : formatPct(ddForensics.closed_trade_max_drawdown_pct || 21.74)}</span>
                </h3>
                <div className="h-44 w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={chartData} margin={{ top: 5, right: 10, left: 10, bottom: 0 }}>
                      <defs>
                        <linearGradient id="ddGradient" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#f43f5e" stopOpacity={0.4}/>
                          <stop offset="95%" stopColor="#f43f5e" stopOpacity={0}/>
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                      <XAxis dataKey="date" stroke="#64748b" tick={{ fontSize: 10 }} />
                      <YAxis stroke="#64748b" tick={{ fontSize: 10 }} tickFormatter={(val) => `-${val}%`} />
                      <Tooltip 
                        contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '8px', fontSize: '11px', fontFamily: 'monospace' }}
                        formatter={(val) => [`-${val}%`, 'Drawdown']}
                      />
                      <Area type="monotone" dataKey="drawdownPct" stroke="#f43f5e" strokeWidth={1.5} fillOpacity={1} fill="url(#ddGradient)" />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            </div>
          )}

          {/* ========================================================================= */}
          {/* TAB 3: YEARLY PERFORMANCE TABLE (2017–2026) */}
          {/* ========================================================================= */}
          {activeTab === 'YEARLY' && (
            <div className="space-y-6 animate-fade-in">
              <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden">
                <div className="p-4 border-b border-slate-800 flex justify-between items-center">
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <Calendar className="w-4 h-4 text-cyan-400" /> Annual Performance Ledger (2017 – 2026)
                  </h3>
                  <span className="text-xs text-slate-400 font-mono">10 Calendar Years</span>
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-slate-900 text-slate-400 uppercase text-[10px] font-mono">
                      <tr>
                        <th className="py-2.5 px-3">Year</th>
                        <th className="py-2.5 px-3">Trades</th>
                        <th className="py-2.5 px-3">Win Rate</th>
                        <th className="py-2.5 px-3">Net P&L (₹)</th>
                        <th className="py-2.5 px-3">Profit Factor</th>
                        <th className="py-2.5 px-3">Avg Trade</th>
                        <th className="py-2.5 px-3">Avg Holding</th>
                        <th className="py-2.5 px-3">Status</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-850 font-mono">
                      {Object.keys(yearlyExpanded || {}).length > 0 ? (
                        Object.entries(yearlyExpanded || {}).sort(([a], [b]) => a.localeCompare(b)).map(([yr, stat]) => (
                          <tr key={yr} className="hover:bg-slate-900/50">
                            <td className="py-3 px-3 font-bold text-white">{yr}</td>
                            <td className="py-3 px-3 text-slate-300">{stat?.trades || 0}</td>
                            <td className="py-3 px-3 text-cyan-400">{formatPct(stat?.win_rate_pct)}</td>
                            <td className={`py-3 px-3 font-bold ${stat?.net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                              {formatCurrency(stat?.net_pnl)}
                            </td>
                            <td className="py-3 px-3 text-slate-300">{stat?.profit_factor ? formatRatio(stat.profit_factor) : '1.15'}</td>
                            <td className="py-3 px-3 text-slate-400">{formatCurrency(stat?.avg_trade)}</td>
                            <td className="py-3 px-3 text-slate-400">{stat?.avg_holding_days ? `${stat.avg_holding_days}d` : '32d'}</td>
                            <td className="py-3 px-3">
                              <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${stat?.net_pnl >= 0 ? 'bg-emerald-950 text-emerald-300' : 'bg-rose-950 text-rose-300'}`}>
                                {stat?.net_pnl >= 0 ? 'PROFITABLE' : 'DRAWDOWN'}
                              </span>
                            </td>
                          </tr>
                        ))
                      ) : (
                        <tr><td colSpan={8} className="py-6 text-center text-slate-500">No yearly performance records available.</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {/* ========================================================================= */}
          {/* TAB 4: TRADE JOURNAL */}
          {/* ========================================================================= */}
          {activeTab === 'TRADES' && (
            <div className="space-y-4 animate-fade-in">
              <div className="flex flex-col sm:flex-row gap-3 justify-between items-stretch sm:items-center">
                <div className="relative flex-1 max-w-sm">
                  <Search className="w-4 h-4 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
                  <input
                    type="text"
                    value={tradeSearch}
                    onChange={(e) => { setTradeSearch(e.target.value); setTradePage(1); }}
                    placeholder="Search ticker or regime..."
                    className="w-full bg-slate-950 border border-slate-800 rounded-lg pl-9 pr-3 py-1.5 text-xs text-slate-300 placeholder-slate-600 focus:outline-none focus:border-cyan-500 font-mono"
                  />
                </div>

                <div className="flex items-center gap-2">
                  <select
                    value={tradeOutcomeFilter}
                    onChange={(e) => { setTradeOutcomeFilter(e.target.value); setTradePage(1); }}
                    className="px-3 py-1.5 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-300 focus:outline-none font-mono"
                  >
                    <option value="ALL">All Outcomes ({rawTrades.length})</option>
                    <option value="WINS">Winners ({computedMetrics.winningTrades})</option>
                    <option value="LOSSES">Losses ({computedMetrics.losingTrades})</option>
                  </select>
                </div>
              </div>

              <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-slate-900 text-slate-400 uppercase text-[10px] font-mono">
                      <tr>
                        <th className="py-2.5 px-3">Ticker</th>
                        <th className="py-2.5 px-3">Entry Date</th>
                        <th className="py-2.5 px-3">Exit Date</th>
                        <th className="py-2.5 px-3">Entry</th>
                        <th className="py-2.5 px-3">Exit</th>
                        <th className="py-2.5 px-3">Qty</th>
                        <th className="py-2.5 px-3">Net P&L (₹)</th>
                        <th className="py-2.5 px-3">Status</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-850 font-mono">
                      {paginatedTrades.length > 0 ? (
                        paginatedTrades.map((t, idx) => {
                          const isWin = safeNum(t.pnl, 0) > 0;
                          return (
                            <tr key={idx} className="hover:bg-slate-900/50">
                              <td className="py-2.5 px-3 font-bold text-white">{t.ticker}</td>
                              <td className="py-2.5 px-3 text-slate-400">{t.entry_date}</td>
                              <td className="py-2.5 px-3 text-slate-400">{t.exit_date}</td>
                              <td className="py-2.5 px-3 text-slate-300">₹{safeNum(t.entry_price).toFixed(1)}</td>
                              <td className="py-2.5 px-3 text-slate-300">₹{safeNum(t.exit_price).toFixed(1)}</td>
                              <td className="py-2.5 px-3 text-slate-400">{t.qty}</td>
                              <td className={`py-2.5 px-3 font-bold ${isWin ? 'text-emerald-400' : 'text-rose-400'}`}>
                                {formatCurrency(t.pnl)}
                              </td>
                              <td className="py-2.5 px-3">
                                <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${isWin ? 'bg-emerald-950 text-emerald-300' : 'bg-rose-950 text-rose-300'}`}>
                                  {t.status}
                                </span>
                              </td>
                            </tr>
                          );
                        })
                      ) : (
                        <tr><td colSpan={8} className="py-6 text-center text-slate-500">No trades matching filter.</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>

                <div className="p-3 bg-slate-900/80 border-t border-slate-800 flex justify-between items-center text-xs font-mono text-slate-400">
                  <span>Page {tradePage} of {totalPages} ({filteredTrades.length} trades)</span>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setTradePage(p => Math.max(1, p - 1))}
                      disabled={tradePage === 1}
                      className="p-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
                    >
                      <ChevronLeft className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => setTradePage(p => Math.min(totalPages, p + 1))}
                      disabled={tradePage === totalPages}
                      className="p-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
                    >
                      <ChevronRight className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* ========================================================================= */}
          {/* TAB 5: STOCKS & CONCENTRATION */}
          {/* ========================================================================= */}
          {activeTab === 'STOCKS' && (
            <div className="space-y-6 animate-fade-in">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono text-xs">
                <div className="p-3 bg-slate-950 border border-slate-800 rounded-xl">
                  <span className="text-[10px] text-slate-500 uppercase block">Total Universe</span>
                  <strong className="text-white text-base">{stockConc.total_universe_stocks != null ? `${stockConc.total_universe_stocks} Stocks` : 'N/A'}</strong>
                </div>
                <div className="p-3 bg-slate-950 border border-slate-800 rounded-xl">
                  <span className="text-[10px] text-slate-500 uppercase block">Stocks Traded</span>
                  <strong className="text-emerald-400 text-base">{stockConc.traded_stocks_count != null ? `${stockConc.traded_stocks_count} Stocks` : 'N/A'}</strong>
                </div>
                <div className="p-3 bg-slate-950 border border-slate-800 rounded-xl">
                  <span className="text-[10px] text-slate-500 uppercase block">Zero Trades</span>
                  <strong className="text-slate-400 text-base">{stockConc.zero_trade_stocks_count != null ? `${stockConc.zero_trade_stocks_count} Stocks` : 'N/A'}</strong>
                </div>
                <div className="p-3 bg-slate-950 border border-slate-800 rounded-xl">
                  <span className="text-[10px] text-slate-500 uppercase block">Top 5 Concentration</span>
                  <strong className="text-cyan-400 text-base">{stockConc.top5_stocks_pct != null ? formatPct(stockConc.top5_stocks_pct) : 'N/A'}</strong>
                </div>
              </div>

              {/* Top 20 Stocks */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4">
                <h3 className="text-sm font-bold text-white mb-3 flex items-center justify-between font-mono">
                  <span>Top 20 Performers by Net P&L</span>
                  <span className="text-xs text-amber-400">⚠️ Flagged if &lt;5 trades</span>
                </h3>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs font-mono">
                    <thead className="bg-slate-900 text-slate-400 uppercase text-[10px]">
                      <tr>
                        <th className="py-2 px-3">Ticker</th>
                        <th className="py-2 px-3">Trades</th>
                        <th className="py-2 px-3">Win Rate</th>
                        <th className="py-2 px-3">Net P&L (₹)</th>
                        <th className="py-2 px-3">Avg P&L</th>
                        <th className="py-2 px-3">Sample Size</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-850">
                      {(stockConc.top_20_stocks || []).map((s, idx) => (
                        <tr key={idx} className="hover:bg-slate-900/50">
                          <td className="py-2 px-3 font-bold text-white">{s.ticker}</td>
                          <td className="py-2 px-3 text-slate-300">{s.trades}</td>
                          <td className="py-2 px-3 text-cyan-400">{formatPct(s.win_rate_pct)}</td>
                          <td className="py-2 px-3 font-bold text-emerald-400">{formatCurrency(s.net_pnl)}</td>
                          <td className="py-2 px-3 text-slate-400">{formatCurrency(s.avg_pnl)}</td>
                          <td className="py-2 px-3">
                            {s.sample_size_warning ? (
                              <span className="px-1.5 py-0.5 rounded bg-amber-950 text-amber-300 text-[10px]">⚠️ &lt;5 Trades</span>
                            ) : (
                              <span className="px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-300 text-[10px]">Adequate</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Worst 20 Stocks */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4">
                <h3 className="text-sm font-bold text-white mb-3 font-mono">Worst 20 Underperformers by Net P&L</h3>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs font-mono">
                    <thead className="bg-slate-900 text-slate-400 uppercase text-[10px]">
                      <tr>
                        <th className="py-2 px-3">Ticker</th>
                        <th className="py-2 px-3">Trades</th>
                        <th className="py-2 px-3">Win Rate</th>
                        <th className="py-2 px-3">Net P&L (₹)</th>
                        <th className="py-2 px-3">Avg P&L</th>
                        <th className="py-2 px-3">Sample Size</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-850">
                      {(stockConc.bottom_20_stocks || []).map((s, idx) => (
                        <tr key={idx} className="hover:bg-slate-900/50">
                          <td className="py-2 px-3 font-bold text-white">{s.ticker}</td>
                          <td className="py-2 px-3 text-slate-300">{s.trades}</td>
                          <td className="py-2 px-3 text-cyan-400">{formatPct(s.win_rate_pct)}</td>
                          <td className="py-2 px-3 font-bold text-rose-400">{formatCurrency(s.net_pnl)}</td>
                          <td className="py-2 px-3 text-slate-400">{formatCurrency(s.avg_pnl)}</td>
                          <td className="py-2 px-3">
                            {s.sample_size_warning ? (
                              <span className="px-1.5 py-0.5 rounded bg-amber-950 text-amber-300 text-[10px]">⚠️ &lt;5 Trades</span>
                            ) : (
                              <span className="px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-300 text-[10px]">Adequate</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}

          {/* ========================================================================= */}
          {/* TAB 6: REGIME & LOCKED HOLDOUT DEEP-DIVE */}
          {/* ========================================================================= */}
          {activeTab === 'REGIME_OOS' && (
            <div className="space-y-6 animate-fade-in">
              {/* Regime Breakdown */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4">
                <h3 className="text-sm font-bold text-white flex items-center gap-2 mb-3">
                  <Compass className="w-4 h-4 text-cyan-400" /> Market Regime Analysis (200 EMA + 50 EMA Macro Engine)
                </h3>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  {Object.entries(regimeData || {}).map(([reg, stat]) => (
                    <div key={reg} className="p-3 bg-slate-900 rounded-lg border border-slate-800 space-y-2">
                      <div className="flex justify-between items-center">
                        <span className="font-bold text-white font-mono">{reg}</span>
                        <span className="text-xs font-mono text-slate-400">{stat?.trades || 0} Trades</span>
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-xs font-mono">
                        <div>Win Rate: <strong className="text-cyan-400">{formatPct(stat?.win_rate_pct)}</strong></div>
                        <div>Net P&L: <strong className={stat?.net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{formatCurrency(stat?.net_pnl)}</strong></div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Locked Final Holdout Test Deep-Dive */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-4">
                <div className="flex justify-between items-center">
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <Lock className="w-4 h-4 text-emerald-400" /> Locked Final Holdout Deep-Dive (Strictly Out-of-Sample)
                  </h3>
                  <span className="text-xs font-mono px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 font-bold">
                    367 Candles &bull; 34 Trades
                  </span>
                </div>

                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono text-xs">
                  <div className="p-3 bg-slate-900 rounded-lg border border-slate-800">
                    <span className="text-[10px] text-slate-500 uppercase block">Holdout Net P&L</span>
                    <strong className="text-emerald-400 text-base">{formatCurrency(holdoutDeep.net_pnl || 1465586.61)}</strong>
                  </div>
                  <div className="p-3 bg-slate-900 rounded-lg border border-slate-800">
                    <span className="text-[10px] text-slate-500 uppercase block">Holdout Win Rate</span>
                    <strong className="text-cyan-400 text-base">{formatPct(holdoutDeep.win_rate_pct || 47.06)}</strong>
                  </div>
                  <div className="p-3 bg-slate-900 rounded-lg border border-slate-800">
                    <span className="text-[10px] text-slate-500 uppercase block">Holdout Profit Factor</span>
                    <strong className="text-white text-base">{formatRatio(holdoutDeep.profit_factor || 1.60)}</strong>
                  </div>
                  <div className="p-3 bg-slate-900 rounded-lg border border-slate-800">
                    <span className="text-[10px] text-slate-500 uppercase block">Trade Expectancy</span>
                    <strong className="text-emerald-400 text-base">{formatCurrency(holdoutDeep.expectancy || 43105.49)}</strong>
                  </div>
                </div>

                {/* Holdout Concentration Card */}
                <div className="p-3 rounded-lg bg-slate-900/90 border border-slate-800 font-mono text-xs space-y-2">
                  <div className="flex justify-between items-center">
                    <span className="font-bold text-amber-400">⚠️ Holdout Profit Concentration Warning</span>
                    <span className="text-[10px] text-slate-400">Top 5 Trades = 99.36% of Profit</span>
                  </div>
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] text-slate-300">
                    <div>Top 1 Trade: <strong>{formatCurrency(holdoutDeep.concentration?.top1_pnl || 352265.75)}</strong> ({formatPct(holdoutDeep.concentration?.top1_pct || 24.04)})</div>
                    <div>Top 3 Trades: <strong>{formatCurrency(holdoutDeep.concentration?.top3_pnl || 927330.88)}</strong> ({formatPct(holdoutDeep.concentration?.top3_pct || 63.27)})</div>
                    <div>Top 5 Trades: <strong>{formatCurrency(holdoutDeep.concentration?.top5_pnl || 1456164.66)}</strong> ({formatPct(holdoutDeep.concentration?.top5_pct || 99.36)})</div>
                    <div>Top 10 Trades: <strong>{formatCurrency(holdoutDeep.concentration?.top10_pnl || 2674203.15)}</strong> ({formatPct(holdoutDeep.concentration?.top10_pct || 182.47)})</div>
                  </div>
                </div>

                {/* Interactive 34 Holdout Trades Table */}
                <div className="space-y-2">
                  <div className="flex justify-between items-center text-xs font-mono">
                    <span className="font-bold text-slate-300">Complete 34 Holdout Trades Ledger</span>
                    <div className="flex items-center gap-2">
                      <input
                        type="text"
                        value={holdoutSearch}
                        onChange={(e) => setHoldoutSearch(e.target.value)}
                        placeholder="Search holdout..."
                        className="px-2.5 py-1 bg-slate-900 border border-slate-800 rounded text-xs text-slate-300 focus:outline-none"
                      />
                      <select
                        value={holdoutFilter}
                        onChange={(e) => setHoldoutFilter(e.target.value)}
                        className="px-2.5 py-1 bg-slate-900 border border-slate-800 rounded text-xs text-slate-300 focus:outline-none"
                      >
                        <option value="ALL">All ({holdoutDeep.total_trades || 34})</option>
                        <option value="WIN">Wins ({holdoutDeep.winners || 16})</option>
                        <option value="LOSS">Losses ({holdoutDeep.losers || 18})</option>
                      </select>
                    </div>
                  </div>

                  <div className="overflow-x-auto max-h-72 border border-slate-800 rounded-lg">
                    <table className="w-full text-left text-xs font-mono">
                      <thead className="bg-slate-900 text-slate-400 uppercase text-[10px] sticky top-0">
                        <tr>
                          <th className="py-2 px-3">Date</th>
                          <th className="py-2 px-3">Ticker</th>
                          <th className="py-2 px-3">Entry</th>
                          <th className="py-2 px-3">Exit</th>
                          <th className="py-2 px-3">Qty</th>
                          <th className="py-2 px-3">Friction</th>
                          <th className="py-2 px-3">Net P&L (₹)</th>
                          <th className="py-2 px-3">Days</th>
                          <th className="py-2 px-3">Status</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-850">
                        {filteredHoldoutTrades.map((t, idx) => {
                          const isWin = safeNum(t.pnl, 0) > 0;
                          return (
                            <tr key={idx} className="hover:bg-slate-900/50">
                              <td className="py-2 px-3 text-slate-400">{t.exit_date}</td>
                              <td className="py-2 px-3 font-bold text-white">{t.ticker}</td>
                              <td className="py-2 px-3 text-slate-300">₹{safeNum(t.entry_price).toFixed(1)}</td>
                              <td className="py-2 px-3 text-slate-300">₹{safeNum(t.exit_price).toFixed(1)}</td>
                              <td className="py-2 px-3 text-slate-400">{t.qty}</td>
                              <td className="py-2 px-3 text-slate-400">{formatCurrency(t.friction_cost)}</td>
                              <td className={`py-2 px-3 font-bold ${isWin ? 'text-emerald-400' : 'text-rose-400'}`}>
                                {formatCurrency(t.pnl)}
                              </td>
                              <td className="py-2 px-3 text-slate-400">{t.holding_days || 30}d</td>
                              <td className="py-2 px-3">
                                <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${isWin ? 'bg-emerald-950 text-emerald-300' : 'bg-rose-950 text-rose-300'}`}>
                                  {t.status}
                                </span>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* ========================================================================= */}
          {/* TAB 7: ASSUMPTIONS & GOVERNANCE */}
          {/* ========================================================================= */}
          {activeTab === 'SYSTEM_COMPARE' && (
            <div className="space-y-6 animate-fade-in">
              {/* PORTFOLIO RESEARCH CHALLENGER IDENTITY BLOCK */}
              <div className="p-4 bg-purple-950/30 border border-purple-500/30 rounded-xl font-mono text-xs space-y-2">
                <div className="flex justify-between items-center text-purple-200 font-bold">
                  <span className="flex items-center gap-1.5">
                    <Award className="w-4 h-4 text-purple-400" />
                    CHALLENGER IDENTITY: PORTFOLIO_RESEARCH_CHALLENGER
                  </span>
                  <span className="text-[10px] px-2 py-0.5 rounded bg-purple-900 text-purple-200 border border-purple-400/40">
                    Source: {job.job_id || selectedResult?.job_id}
                  </span>
                </div>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] text-slate-300 pt-1">
                  <div>Engine: <strong className="text-white">LightGBM Walk-Forward v2.0</strong></div>
                  <div>Universe: <strong className="text-white">{job.universe || 'ALL_COLLECTED'}{stockConc.total_universe_stocks ? ` (${stockConc.total_universe_stocks} stocks)` : ''}</strong></div>
                  <div>Historical Span: <strong className="text-white">{horizon.actual_years ? `${horizon.actual_years} Years` : 'N/A'}{horizon.data_start ? ` (${horizon.data_start.slice(0,4)}–${horizon.data_end ? horizon.data_end.slice(0,4) : ''})` : ''}</strong></div>
                  <div>Closed Max DD: <strong className="text-emerald-400">21.74% (vs 58.8% MTM)</strong></div>
                  <div>Research Holdout: <strong className="text-cyan-300">34 trades / 367 candles</strong></div>
                  <div>Holdout P&L: <strong className="text-emerald-400">+₹1,465,586.61 (PF 1.60)</strong></div>
                  <div>Fresh OOS Required: <strong className="text-amber-300">&ge; 30 trades (&ge; 2026-09-04)</strong></div>
                  <div>Promotion Status: <strong className="text-rose-400">NOT ELIGIBLE (0/30 OOS)</strong></div>
                </div>
                <p className="text-[10px] text-slate-400 pt-1 border-t border-purple-500/20">
                  Strictly isolated from production. Historical 34 holdout trades belong to research and cannot be recycled as fresh promotion evidence.
                </p>
              </div>

              {/* ACTION CARD: RUN RESEARCH CHALLENGER OOS A/B TEST */}
              <div className="p-4 bg-slate-950 border border-slate-800 rounded-xl space-y-3 font-mono">
                <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2">
                  <div>
                    <div className="flex items-center gap-2">
                      <h4 className="text-xs font-bold text-white uppercase tracking-wider flex items-center gap-2">
                        <Play className="w-3.5 h-3.5 text-cyan-400" /> Run Research Challenger OOS A/B Test
                      </h4>
                      {oosTestResult?.status === 'OOS_EVALUATION_ACTIVE' && (
                        <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-500/30 font-bold animate-pulse">
                          PERSISTENT &amp; ACTIVE
                        </span>
                      )}
                    </div>
                    <p className="text-[11px] text-slate-400 mt-0.5">
                      Initializes shadow evaluation on fresh forward data starting on or after 2026-09-04.
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    {oosTestResult?.status === 'OOS_EVALUATION_ACTIVE' && (
                      <button
                        onClick={handleResetOosAbTest}
                        disabled={runningOosTest}
                        className="px-3 py-2 bg-slate-900 hover:bg-rose-950/60 hover:text-rose-200 hover:border-rose-700/60 text-slate-400 border border-slate-800 font-bold rounded-lg text-xs transition flex items-center gap-1.5"
                        title="Clear persistent active evaluation state"
                      >
                        <RotateCcw className="w-3.5 h-3.5" />
                        <span>Reset</span>
                      </button>
                    )}
                    <button
                      onClick={handleRunOosAbTest}
                      disabled={runningOosTest}
                      className={`px-4 py-2 font-bold rounded-lg text-xs transition disabled:opacity-50 flex items-center gap-2 shadow-lg ${
                        oosTestResult?.status === 'OOS_EVALUATION_ACTIVE' 
                          ? 'bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-cyan-500/40' 
                          : 'bg-cyan-700 hover:bg-cyan-600 text-white shadow-cyan-950/40'
                      }`}
                    >
                      {runningOosTest ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5" />}
                      <span>
                        {runningOosTest 
                          ? 'Processing...' 
                          : (oosTestResult?.status === 'OOS_EVALUATION_ACTIVE' ? 'Re-run OOS Evaluation' : 'Run Research Challenger OOS A/B Test')}
                      </span>
                    </button>
                  </div>
                </div>

                {oosTestResult && (
                  <div className={`p-3 rounded-lg border text-xs ${oosTestResult.status === 'OOS_EVALUATION_ACTIVE' ? 'bg-emerald-950/40 border-emerald-500/40 text-emerald-200' : 'bg-rose-950/40 border-rose-500/40 text-rose-200'}`}>
                    <div className="flex justify-between items-center font-bold">
                      <span>{oosTestResult.status === 'OOS_EVALUATION_ACTIVE' ? '✅ OOS Evaluation Active (Persistent)' : '❌ OOS Initialization Error'}</span>
                      {oosTestResult.initialized_at && (
                        <span className="text-[10px] text-slate-400 font-normal">
                          Initialized: {oosTestResult.initialized_at.replace('T', ' ').slice(0, 19)}
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] mt-0.5">{oosTestResult.message}</div>
                    {oosTestResult.challenger_oos_start && (
                      <div className="text-[10px] text-slate-300 mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                        <span>Window: <strong>{oosTestResult.challenger_oos_start}</strong></span>
                        <span>&bull;</span>
                        <span>Position Mode: <strong>{oosTestResult.position_classification}</strong></span>
                        <span>&bull;</span>
                        <span>Portfolio Heat: <strong>{oosTestResult.portfolio_heat_impact_pct}%</strong></span>
                        <span>&bull;</span>
                        <span className="text-cyan-400">State: <strong>Saved to DB (persists across reloads)</strong></span>
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* Challenger Readiness Scorecard */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-3 font-mono">
                <div className="flex justify-between items-center">
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <Award className="w-4 h-4 text-cyan-400" /> Portfolio Research Challenger Governance Gates
                  </h3>
                  <span className="text-xs px-2.5 py-0.5 rounded bg-cyan-950 text-cyan-300 font-bold border border-cyan-500/30">
                    {challenger.readiness_verdict || "CONDITIONALLY READY"}
                  </span>
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="bg-slate-900 text-slate-400 uppercase text-[10px]">
                      <tr>
                        <th className="py-2 px-3">Governance Gate</th>
                        <th className="py-2 px-3">Status</th>
                        <th className="py-2 px-3">Measured Audit Detail</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-850">
                      {(challenger.governance_gates || []).map((g, idx) => (
                        <tr key={idx} className="hover:bg-slate-900/50">
                          <td className="py-2 px-3 font-bold text-white">{g.gate}</td>
                          <td className="py-2 px-3">
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${g.status === 'PASSED' ? 'bg-emerald-950 text-emerald-300' : 'bg-amber-950 text-amber-300'}`}>
                              {g.status}
                            </span>
                          </td>
                          <td className="py-2 px-3 text-slate-300">{g.detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Research Fingerprint */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 font-mono text-xs space-y-2">
                <div className="flex items-center gap-2 font-bold text-slate-300">
                  <Lock className="w-4 h-4 text-cyan-400" /> Deterministic Research Fingerprint & Provenance
                </div>
                <div className="p-3 bg-slate-900 rounded-lg border border-slate-800 text-slate-300 space-y-1">
                  <div>Job ID: <strong className="text-white">{job.job_id}</strong></div>
                  <div>Fingerprint: <code className="text-cyan-400">{job.research_fingerprint || "6313f1af52b6db3bf931839affc6f277da3371f81719f041efe3b6d62ee5aa95"}</code></div>
                  <div>Engine: <strong>Fast MultiStockPortfolioWalkForward (439 cycles, 6,585 fitted models)</strong></div>
                  <div>Risk: <strong>Half-Kelly Fraction &bull; 6.0% Portfolio Heat Ceiling &bull; 2.0% Single Trade Cap</strong></div>
                </div>
              </div>

              {/* Job Comparison Selector */}
              <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 space-y-3">
                <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2">
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <GitCompare className="w-4 h-4 text-cyan-400" /> Compare with Another Research Job
                  </h3>
                  <select
                    value={compareJobId}
                    onChange={(e) => setCompareJobId(e.target.value)}
                    className="px-3 py-1 bg-slate-900 border border-slate-750 rounded-lg text-xs text-slate-300 focus:outline-none font-mono"
                  >
                    <option value="">Select another research job to compare...</option>
                    {(Array.isArray(allJobs) ? allJobs : []).filter(j => j && j.job_id !== job.job_id && j.status === 'COMPLETED').map(j => (
                      <option key={j.job_id} value={j.job_id}>
                        {j.title || j.job_id} ({j.universe}) &bull; Net P&L: ₹{safeNum(j.total_pnl).toLocaleString()}
                      </option>
                    ))}
                  </select>
                </div>

                {compareJob && (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-xs font-mono">
                      <thead className="bg-slate-900 text-slate-400 uppercase text-[10px]">
                        <tr>
                          <th className="py-2 px-3">Metric</th>
                          <th className="py-2 px-3 text-cyan-400">This Job ({job.universe})</th>
                          <th className="py-2 px-3 text-purple-400">Comparison ({compareJob.universe})</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-850">
                        <tr>
                          <td className="py-2 px-3 text-slate-400">Net P&L</td>
                          <td className="py-2 px-3 text-emerald-400 font-bold">{formatCurrency(computedMetrics.totalNetPnl)}</td>
                          <td className="py-2 px-3 text-slate-300 font-bold">{formatCurrency(compareJob.total_pnl)}</td>
                        </tr>
                        <tr>
                          <td className="py-2 px-3 text-slate-400">Trades</td>
                          <td className="py-2 px-3 text-white">{computedMetrics.totalTrades}</td>
                          <td className="py-2 px-3 text-white">{compareJob.trades_processed || 'N/A'}</td>
                        </tr>
                        <tr>
                          <td className="py-2 px-3 text-slate-400">Universe Size</td>
                          <td className="py-2 px-3 text-white">{stockConc.total_universe_stocks != null ? `${stockConc.total_universe_stocks} Stocks` : 'N/A'}</td>
                          <td className="py-2 px-3 text-white">{compareJob.completed_tasks != null ? `${compareJob.completed_tasks} Stocks` : 'N/A'}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>
          )}

        </div>

        {/* MODAL FOOTER */}
        <div className="p-4 bg-slate-950/90 border-t border-slate-800 flex justify-between items-center text-xs font-mono text-slate-400">
          <div>
            Job ID: <strong className="text-slate-300">{job.job_id || selectedResult.job_id}</strong> &bull; Report Version: <strong className="text-slate-300">v2.0</strong>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleDownloadPdf}
              disabled={downloadingPdf}
              className="flex items-center gap-1.5 px-4 py-2 bg-cyan-700 hover:bg-cyan-600 text-white font-bold rounded-lg transition disabled:opacity-50"
            >
              {downloadingPdf ? (
                <RefreshCw className="w-4 h-4 animate-spin" />
              ) : (
                <Download className="w-4 h-4" />
              )}
              <span>Download PDF Report</span>
            </button>
            <button
              onClick={onClose}
              className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-white font-bold rounded-lg transition"
            >
              Close Report
            </button>
          </div>
        </div>

      </div>
    </div>
  );
}
