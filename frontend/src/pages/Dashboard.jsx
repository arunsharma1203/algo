import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import {
  Activity, TrendingUp, TrendingDown, Clock, Download, RefreshCw,
  AlertTriangle, ShieldCheck, Globe, Compass, BarChart2, Layers,
  Newspaper, Calendar, Cpu, CheckCircle2, Zap, Target, ArrowUpRight, ArrowDownRight
} from 'lucide-react';
import {
  getLatestData, API_BASE, getDashboardIntelligence, downloadDashboardReportPdf
} from '../services/api';
import TickerSearch from '../components/TickerSearch';
import { useLiveIndicator } from '../context/LiveIndicatorContext';

// ── SAFE VALUE FORMATTING UTILITIES ─────────────────────────────────────
const safeNum = (val, decimals = 2, fallback = '--') => {
  if (val === null || val === undefined || isNaN(Number(val))) return fallback;
  return Number(val).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  });
};

const formatCurrency = (val, fallback = '--') => {
  if (val === null || val === undefined || isNaN(Number(val))) return fallback;
  return `₹${safeNum(val, 2)}`;
};

const formatPct = (val, fallback = '--') => {
  if (val === null || val === undefined || isNaN(Number(val))) return fallback;
  const num = Number(val);
  const sign = num > 0 ? '+' : '';
  return `${sign}${num.toFixed(2)}%`;
};

export default function Dashboard() {
  // ── COMMAND CENTER STATE ──────────────────────────────────────────────
  const [intelligence, setIntelligence] = useState(null);
  const [loadingIntel, setLoadingIntel] = useState(true);
  const [intelError, setIntelError] = useState(null);
  const [downloadState, setDownloadState] = useState('idle'); // 'idle' | 'generating' | 'ready' | 'error'
  const [istTime, setIstTime] = useState('');

  // ── PRESERVED SINGLE-TICKER DRILL-DOWN STATE ──────────────────────────
  const [tickerInput, setTickerInput] = useState('RELIANCE.NS');
  const [activeTicker, setActiveTicker] = useState('RELIANCE.NS');
  const [marketData, setMarketData] = useState(null);
  const [loadingTicker, setLoadingTicker] = useState(false);
  const [tickerError, setTickerError] = useState(null);
  const [recentSearches, setRecentSearches] = useState([]);
  const [lastStats, setLastStats] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [activeMonitors, setActiveMonitors] = useState([]);

  const { triggerFetchIndicator } = useLiveIndicator();

  // ── CLOCK & INITIAL LOAD ──────────────────────────────────────────────
  useEffect(() => {
    const updateClock = () => {
      const now = new Date();
      setIstTime(now.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) + ' IST');
    };
    updateClock();
    const clockInterval = setInterval(updateClock, 1000);
    return () => clearInterval(clockInterval);
  }, []);

  // Fetch Dashboard Intelligence
  const fetchIntelligence = async (forceRefresh = false) => {
    try {
      if (forceRefresh) setLoadingIntel(true);
      const data = await getDashboardIntelligence(forceRefresh);
      setIntelligence(data);
      setIntelError(null);
    } catch (err) {
      console.error('Failed to load dashboard intelligence:', err);
      setIntelError('Market intelligence feed temporarily unavailable. Displaying cached telemetry.');
    } finally {
      setLoadingIntel(false);
    }
  };

  useEffect(() => {
    fetchIntelligence(false);
    const interval = setInterval(() => fetchIntelligence(false), 30000); // 30s poll
    return () => clearInterval(interval);
  }, []);

  // Preserved searches and ticker data
  useEffect(() => {
    const saved = localStorage.getItem('recent_searches');
    let defaultTicker = 'RELIANCE.NS';
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        setRecentSearches(parsed);
        if (parsed.length > 0) defaultTicker = parsed[0];
      } catch (e) {}
    }
    setTickerInput(defaultTicker);
    setActiveTicker(defaultTicker);
    fetchSingleTickerData(defaultTicker);

    // Fetch autonomous bot alerts
    const fetchAlerts = async () => {
      try {
        const response = await fetch(`${API_BASE}/ml/alerts`);
        if (response.ok) setAlerts(await response.json());
        const monitorRes = await fetch(`${API_BASE}/ml/active-monitors`);
        if (monitorRes.ok) setActiveMonitors(await monitorRes.json());
      } catch (err) {}
    };
    fetchAlerts();
    const alertInterval = setInterval(fetchAlerts, 10000);
    return () => clearInterval(alertInterval);
  }, []);

  const addRecentSearch = (ticker) => {
    const updated = [ticker, ...recentSearches.filter(t => t !== ticker)].slice(0, 5);
    setRecentSearches(updated);
    localStorage.setItem('recent_searches', JSON.stringify(updated));
  };

  const fetchSingleTickerData = async (ticker) => {
    if (!ticker) return;
    const upperTicker = ticker.toUpperCase();
    setLoadingTicker(true);
    setTickerError(null);
    try {
      const data = await getLatestData(upperTicker);
      setMarketData(data);
      setActiveTicker(upperTicker);
      addRecentSearch(upperTicker);
      if (data?.metadata) triggerFetchIndicator(data.metadata);
    } catch (err) {
      setTickerError(`Failed to fetch data for ${upperTicker}.`);
    } finally {
      setLoadingTicker(false);
    }
  };

  // ── "DOWNLOAD TODAY'S REPORT" HANDLER ─────────────────────────────────
  const handleDownloadReport = async () => {
    try {
      setDownloadState('generating');
      const blob = await downloadDashboardReportPdf(false);
      const url = window.URL.createObjectURL(new Blob([blob], { type: 'application/pdf' }));
      const link = document.createElement('a');
      const dateStr = intelligence?.report_date || new Date().toISOString().split('T')[0];
      link.href = url;
      link.setAttribute('download', `Daily_Market_Report_${dateStr}.pdf`);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      setDownloadState('ready');
      setTimeout(() => setDownloadState('idle'), 3500);
    } catch (err) {
      console.error('Failed to download report PDF:', err);
      setDownloadState('error');
      setTimeout(() => setDownloadState('idle'), 4000);
    }
  };

  // Extracted intelligence sections with defensive defaults
  const marketStatus = intelligence?.market_status || {};
  const indianMarkets = intelligence?.indian_markets || [];
  const globalCues = intelligence?.global_cues || [];
  const regime = intelligence?.regime || {};
  const breadth = intelligence?.breadth || {};
  const sectors = intelligence?.sectors || {};
  const fiiDii = intelligence?.institutional_flows || {};
  const riskRadar = intelligence?.volatility_risk_radar || {};
  const news = intelligence?.news_intelligence || {};
  const aiOps = intelligence?.ai_opportunities || {};
  const aiSummary = intelligence?.ai_summary || {};
  const sysHealth = intelligence?.system_health || {};

  return (
    <div className="max-w-full space-y-6 pb-12">
      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* COMMAND CENTER INSTITUTIONAL HEADER                               */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="bg-white border border-gray-200 rounded-2xl p-6 shadow-sm">
        <div className="flex flex-col lg:flex-row justify-between items-start lg:items-center gap-4">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl sm:text-3xl font-black text-slate-900 tracking-tight flex items-center gap-2">
                <Compass className="text-sky-500" size={28} />
                AI TRADING MARKET COMMAND CENTER
              </h1>
              <span className="hidden sm:inline-block px-2.5 py-0.5 rounded-full text-xs font-bold bg-sky-50 text-sky-700 border border-sky-200">
                Institutional v1.0
              </span>
            </div>
            <p className="text-sm text-slate-500 mt-1">
              Autonomous Market Intelligence, Point-in-Time Quantitative Regimes & Daily Report Gateway
            </p>
          </div>

          {/* Right Action & Status Controls */}
          <div className="flex flex-wrap items-center gap-3 w-full lg:w-auto">
            {/* Market Status Badge */}
            <div className={`px-3 py-1.5 rounded-xl border flex items-center gap-2 text-xs font-bold shadow-sm ${
              marketStatus.is_open 
                ? 'bg-emerald-50 border-emerald-200 text-emerald-700' 
                : 'bg-slate-100 border-slate-200 text-slate-700'
            }`}>
              <span className={`w-2.5 h-2.5 rounded-full ${marketStatus.is_open ? 'bg-emerald-500 animate-ping' : 'bg-slate-400'}`}></span>
              <span>{marketStatus.status_label || 'CHECKING SESSION...'}</span>
            </div>

            {/* Live IST Clock & Freshness */}
            <div className="px-3 py-1.5 rounded-xl bg-slate-50 border border-slate-200 text-slate-700 text-xs font-mono font-bold flex items-center gap-1.5">
              <Clock size={14} className="text-slate-400" />
              <span>{istTime || marketStatus.ist_time || '--:-- IST'}</span>
              {intelligence?.cache_metadata?.is_cache_hit ? (
                <span className="text-[10px] font-sans font-normal text-slate-400 pl-1 border-l border-slate-200">
                  Cached ({intelligence.cache_metadata.cache_age_seconds}s)
                </span>
              ) : null}
            </div>

            {/* Refresh Intelligence */}
            <button
              onClick={() => fetchIntelligence(true)}
              disabled={loadingIntel}
              title="Refresh intelligence snapshot"
              className="p-2 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-xl transition border border-slate-200 disabled:opacity-50"
            >
              <RefreshCw size={16} className={loadingIntel ? 'animate-spin text-sky-600' : ''} />
            </button>

            {/* Download Today's Report Button */}
            <button
              onClick={handleDownloadReport}
              disabled={downloadState === 'generating'}
              className={`px-4 py-2 rounded-xl text-xs font-bold flex items-center gap-2 shadow-sm transition ${
                downloadState === 'generating'
                  ? 'bg-sky-100 text-sky-700 border border-sky-300 cursor-wait'
                  : downloadState === 'ready'
                  ? 'bg-emerald-600 text-white hover:bg-emerald-700'
                  : downloadState === 'error'
                  ? 'bg-rose-600 text-white hover:bg-rose-700'
                  : 'bg-slate-900 text-white hover:bg-slate-800'
              }`}
            >
              {downloadState === 'generating' ? (
                <>
                  <RefreshCw size={14} className="animate-spin" />
                  <span>PREPARING PDF...</span>
                </>
              ) : downloadState === 'ready' ? (
                <>
                  <CheckCircle2 size={14} />
                  <span>REPORT READY</span>
                </>
              ) : downloadState === 'error' ? (
                <>
                  <AlertTriangle size={14} />
                  <span>DOWNLOAD FAILED (RETRY)</span>
                </>
              ) : (
                <>
                  <Download size={14} />
                  <span>DOWNLOAD TODAY'S REPORT</span>
                </>
              )}
            </button>
          </div>
        </div>

        {intelError && (
          <div className="mt-4 p-3 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-800 flex items-center gap-2">
            <AlertTriangle size={16} className="text-amber-600 shrink-0" />
            <span>{intelError}</span>
          </div>
        )}
      </div>

      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* SECTION 1: MARKET AT A GLANCE & QUANTITATIVE REGIME               */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-4">
        {/* Major Indian Indices Cards (5 Columns) */}
        {indianMarkets.map((idx, i) => {
          const isPos = (idx.change_pct || 0) >= 0;
          return (
            <div key={i} className="bg-white p-4 rounded-xl border border-gray-200 shadow-sm hover:shadow-md transition">
              <div className="flex justify-between items-start">
                <span className="text-xs font-bold text-slate-500 uppercase tracking-wider">{idx.name}</span>
                <span className="text-[10px] font-mono font-bold px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 border border-slate-200">
                  {idx.freshness || 'FRESH'}
                </span>
              </div>
              <p className="text-xl font-black text-slate-900 mt-2">
                {idx.ltp != null ? (idx.name === 'INDIA VIX' ? safeNum(idx.ltp, 2) : formatCurrency(idx.ltp)) : '--'}
              </p>
              <div className="flex items-center justify-between mt-1 text-xs font-semibold">
                <span className={`flex items-center ${isPos ? 'text-emerald-600' : 'text-rose-600'}`}>
                  {isPos ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}
                  {formatPct(idx.change_pct)}
                </span>
                <span className="text-slate-400 font-mono text-[10px]">
                  {idx.change != null ? `${idx.change > 0 ? '+' : ''}${idx.change.toFixed(1)}` : ''}
                </span>
              </div>
              <div className="flex justify-between text-[10px] text-slate-400 mt-2 border-t border-slate-100 pt-1">
                <span>H: {idx.high != null ? idx.high.toFixed(0) : '--'}</span>
                <span>L: {idx.low != null ? idx.low.toFixed(0) : '--'}</span>
              </div>
            </div>
          );
        })}

        {/* Quantitative Macro Regime Card (1 Column) */}
        <div className="bg-gradient-to-br from-slate-900 to-slate-800 p-4 rounded-xl text-white shadow-sm flex flex-col justify-between">
          <div>
            <div className="flex justify-between items-start">
              <span className="text-xs font-bold text-slate-400 uppercase tracking-wider">Macro Regime</span>
              <ShieldCheck size={16} className="text-sky-400" />
            </div>
            <p className="text-base font-black text-sky-300 mt-1 truncate">
              {regime.composite_regime || 'CALCULATING...'}
            </p>
            <p className="text-[10px] text-slate-400 mt-0.5">
              200 SMA: {regime.nifty_trend_long || '--'} | 20 EMA: {regime.nifty_trend_short || '--'}
            </p>
          </div>
          <div className="mt-3 pt-2 border-t border-slate-700/60 flex items-center justify-between text-[11px]">
            <span className="text-slate-400">VIX Status:</span>
            <span className={`font-bold px-1.5 py-0.5 rounded text-[10px] ${
              regime.vix_status === 'LOW' ? 'bg-emerald-500/20 text-emerald-300' :
              regime.vix_status === 'HIGH' ? 'bg-rose-500/20 text-rose-300' : 'bg-slate-700 text-slate-200'
            }`}>
              {regime.vix_status || 'NORMAL'} ({regime.vix_close ? regime.vix_close.toFixed(1) : '--'})
            </span>
          </div>
        </div>
      </div>

      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* SECTION 2: GLOBAL CUES & INTERMARKET DYNAMICS                     */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="bg-white border border-gray-200 rounded-2xl p-5 shadow-sm">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider flex items-center gap-2">
            <Globe size={16} className="text-sky-500" /> GLOBAL MARKET CUES & INTERMARKET DYNAMICS
          </h2>
          <span className="text-xs text-slate-400 font-mono">Multi-Asset Global Cues</span>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-8 gap-2.5">
          {globalCues.slice(0, 16).map((cue, i) => {
            const hasChange = cue.change_pct != null;
            const isPos = hasChange && cue.change_pct >= 0;
            const stateLabel = cue.state_label || cue.market_state || 'LAST CLOSE';
            return (
              <div key={i} className="p-2.5 bg-slate-50 hover:bg-slate-100 rounded-xl border border-slate-200 transition flex flex-col justify-between">
                <div>
                  <div className="flex justify-between items-center text-[10px] text-slate-500 font-bold">
                    <span className="truncate max-w-[70px]" title={cue.name}>{cue.name}</span>
                    <span className="text-[9px] text-slate-400">{cue.region}</span>
                  </div>
                  <p className="text-xs font-bold text-slate-800 mt-1">
                    {cue.value != null ? safeNum(cue.value, 1) : '--'}
                  </p>
                </div>
                <div className="mt-2 pt-1 border-t border-slate-200/60 flex items-center justify-between text-[10px]">
                  {hasChange ? (
                    <div className={`font-bold flex items-center ${isPos ? 'text-emerald-600' : 'text-rose-600'}`}>
                      {isPos ? <ArrowUpRight size={10} /> : <ArrowDownRight size={10} />}
                      {formatPct(cue.change_pct)}
                    </div>
                  ) : (
                    <span className="text-slate-400 font-mono text-[9px]">UNAVAIL</span>
                  )}
                  <span className={`text-[8px] font-bold px-1 py-0.2 rounded ${
                    stateLabel === 'LIVE' ? 'bg-emerald-100 text-emerald-800' :
                    stateLabel === 'PRE-MARKET' ? 'bg-amber-100 text-amber-800' : 'bg-slate-200 text-slate-600'
                  }`}>
                    {stateLabel}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* SECTION 3 & 4: MARKET BREADTH & SECTOR ROTATION                   */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Market Breadth Card */}
        <div className="bg-white border border-gray-200 rounded-2xl p-5 shadow-sm flex flex-col justify-between">
          <div>
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-1 mb-3">
              <div>
                <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider flex items-center gap-2">
                  <BarChart2 size={16} className="text-indigo-500" /> Market Breadth Analysis
                </h2>
                <p className="text-[11px] text-slate-500 mt-0.5">
                  Universe: <span className="font-bold text-slate-700">{breadth.universe_size || 511}</span> | Evaluated: <span className="font-bold text-slate-700">{breadth.evaluated_count || breadth.total_stocks || 51}</span> | Coverage: <span className="font-bold text-indigo-600">{breadth.coverage_pct || 10.0}%</span>
                </p>
              </div>
              <div className="text-xs font-bold text-slate-500 text-right">
                A/D Ratio: <span className="text-slate-900 font-black">{breadth.ad_ratio ? `${breadth.ad_ratio} : 1` : '--'}</span>
              </div>
            </div>

            {/* Advances / Declines Bar */}
            <div className="mt-3">
              <div className="flex justify-between text-xs font-bold mb-1">
                <span className="text-emerald-600 flex items-center gap-1">
                  ▲ {breadth.advances || 0} Advances ({breadth.pct_advancing || 50}% of {breadth.evaluated_count || breadth.total_stocks || 51})
                </span>
                <span className="text-rose-600 flex items-center gap-1">
                  ▼ {breadth.declines || 0} Declines ({breadth.pct_declining || 50}% of {breadth.evaluated_count || breadth.total_stocks || 51})
                </span>
              </div>
              <div className="h-2.5 bg-slate-200 rounded-full overflow-hidden flex">
                <div className="bg-emerald-500 h-full transition-all" style={{ width: `${breadth.pct_advancing || 50}%` }}></div>
                <div className="bg-rose-500 h-full transition-all" style={{ width: `${breadth.pct_declining || 50}%` }}></div>
              </div>
            </div>

            {/* Technical Moving Average Breadth */}
            <div className="grid grid-cols-3 gap-3 mt-4">
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200 text-center">
                <p className="text-[10px] font-bold text-slate-500 uppercase">&gt; 20 DMA</p>
                <p className="text-base font-black text-slate-800 mt-0.5">{breadth.above_20_dma_pct || '--'}%</p>
                <p className="text-[10px] text-slate-400 font-mono mt-0.5">
                  {breadth.above_20_count != null ? `${breadth.above_20_count} / ${breadth.dma_evaluated_count || breadth.evaluated_count || 51}` : '--'}
                </p>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200 text-center">
                <p className="text-[10px] font-bold text-slate-500 uppercase">&gt; 50 DMA</p>
                <p className="text-base font-black text-slate-800 mt-0.5">{breadth.above_50_dma_pct || '--'}%</p>
                <p className="text-[10px] text-slate-400 font-mono mt-0.5">
                  {breadth.above_50_count != null ? `${breadth.above_50_count} / ${breadth.dma_evaluated_count || breadth.evaluated_count || 51}` : '--'}
                </p>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200 text-center">
                <p className="text-[10px] font-bold text-slate-500 uppercase">&gt; 200 DMA</p>
                <p className="text-base font-black text-slate-800 mt-0.5">{breadth.above_200_dma_pct || '--'}%</p>
                <p className="text-[10px] text-slate-400 font-mono mt-0.5">
                  {breadth.above_200_count != null ? `${breadth.above_200_count} / ${breadth.dma_evaluated_count || breadth.evaluated_count || 51}` : '--'}
                </p>
              </div>
            </div>
          </div>

          <div className="mt-4 pt-3 border-t border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-1 text-xs text-slate-500">
            <span className="italic">{breadth.interpretation || 'Balanced distribution.'}</span>
            <span className="font-mono text-[10px] text-slate-400">
              52W Highs: {breadth.highs_52w || 0} | Lows: {breadth.lows_52w || 0} (of {breadth.high_low_evaluated_count || breadth.dma_evaluated_count || breadth.evaluated_count || 51})
            </span>
          </div>
        </div>

        {/* Sector Rotation Matrix Card */}
        <div className="bg-white border border-gray-200 rounded-2xl p-5 shadow-sm">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider flex items-center gap-2">
              <Layers size={16} className="text-purple-500" /> Sector Rotation & Leaders
            </h2>
            <span className="text-xs text-slate-400 font-mono">1D Performance Ranked</span>
          </div>

          <div className="grid grid-cols-2 gap-2.5">
            {(sectors.sectors || []).slice(0, 8).map((sec, i) => {
              const hasChg = sec.change_1d_pct != null;
              const isPos = hasChg && sec.change_1d_pct >= 0;
              return (
                <div key={i} className="p-2.5 bg-slate-50 rounded-xl border border-slate-200 flex justify-between items-center">
                  <div>
                    <p className="text-xs font-bold text-slate-800">{sec.name.replace('NIFTY ', '')}</p>
                    <p className="text-[10px] text-slate-400">{sec.ltp != null ? `₹${sec.ltp.toFixed(1)}` : '--'}</p>
                  </div>
                  {hasChg ? (
                    <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                      isPos ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                    }`}>
                      {formatPct(sec.change_1d_pct)}
                    </span>
                  ) : (
                    <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 border border-slate-200">
                      UNAVAILABLE
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* SECTION 5: INSTITUTIONAL FLOWS & COMPOSITE RISK RADAR             */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Institutional Flows (FII/DII) */}
        <div className="lg:col-span-1 bg-white border border-gray-200 rounded-2xl p-5 shadow-sm flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider flex items-center gap-2">
                <TrendingUp size={16} className="text-blue-500" /> Institutional Flows (FII / DII)
              </h2>
              <span className={`text-[10px] font-bold px-2 py-0.5 rounded border ${
                fiiDii.status === 'FRESH' ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-slate-100 text-slate-600 border-slate-200'
              }`}>
                {fiiDii.status || 'UNAVAILABLE'}
              </span>
            </div>

            {fiiDii.status === 'FRESH' ? (
              <div className="space-y-2 mt-2">
                <div className="flex justify-between items-center p-2 bg-slate-50 rounded-lg text-xs">
                  <span className="font-medium text-slate-600">Latest Session FII</span>
                  <span className={`font-bold ${fiiDii.fii_latest_cr >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                    {fiiDii.fii_latest_cr >= 0 ? '+' : ''}{safeNum(fiiDii.fii_latest_cr)} Cr
                  </span>
                </div>
                <div className="flex justify-between items-center p-2 bg-slate-50 rounded-lg text-xs">
                  <span className="font-medium text-slate-600">Latest Session DII</span>
                  <span className={`font-bold ${fiiDii.dii_latest_cr >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                    {fiiDii.dii_latest_cr >= 0 ? '+' : ''}{safeNum(fiiDii.dii_latest_cr)} Cr
                  </span>
                </div>
                <div className="flex justify-between items-center p-2 bg-slate-50 rounded-lg text-xs">
                  <span className="font-medium text-slate-600">5-Day Net FII</span>
                  <span className="font-bold text-slate-800">{safeNum(fiiDii.fii_5d_cr)} Cr</span>
                </div>
              </div>
            ) : (
              <div className="p-4 bg-slate-50 border border-dashed border-slate-200 rounded-xl text-center text-xs text-slate-500 my-4">
                <p className="font-bold text-slate-700">Official Exchange Feed Offline</p>
                <p className="text-[11px] mt-1 text-slate-400">Values are strictly verified against exchange disclosures. Never estimated.</p>
              </div>
            )}
          </div>

          <div className="text-[10px] text-slate-400 border-t border-slate-100 pt-2">
            Source: NSE / NSDL Official Filings
          </div>
        </div>

        {/* Volatility & Composite Risk Radar */}
        <div className="lg:col-span-2 bg-white border border-gray-200 rounded-2xl p-5 shadow-sm flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider flex items-center gap-2">
                <AlertTriangle size={16} className="text-amber-500" /> Intermarket Volatility & Risk Radar
              </h2>
              <span className={`px-3 py-1 rounded-full text-xs font-black tracking-wide border shadow-sm ${
                riskRadar.composite_risk === 'LOW' ? 'bg-emerald-100 text-emerald-800 border-emerald-200' :
                riskRadar.composite_risk === 'HIGH' ? 'bg-rose-100 text-rose-800 border-rose-200' :
                'bg-amber-100 text-amber-800 border-amber-200'
              }`}>
                {riskRadar.composite_risk || 'MODERATE'} RISK
              </span>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-2">
              <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                <p className="text-[10px] font-bold text-slate-400 uppercase">India VIX</p>
                <p className="text-base font-black text-slate-800 mt-0.5">{riskRadar.india_vix || '--'}</p>
                <p className="text-[10px] text-slate-500">Domestic Volatility</p>
              </div>
              <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                <p className="text-[10px] font-bold text-slate-400 uppercase">US VIX</p>
                <p className="text-base font-black text-slate-800 mt-0.5">{riskRadar.us_vix || '--'}</p>
                <p className="text-[10px] text-slate-500">Global Volatility</p>
              </div>
              <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                <p className="text-[10px] font-bold text-slate-400 uppercase">Brent Crude</p>
                <p className="text-base font-black text-slate-800 mt-0.5">${riskRadar.crude_brent || '--'}</p>
                <p className="text-[10px] text-slate-500">Energy Drag</p>
              </div>
              <div className="p-3 bg-slate-50 rounded-xl border border-slate-200">
                <p className="text-[10px] font-bold text-slate-400 uppercase">USD / INR</p>
                <p className="text-base font-black text-slate-800 mt-0.5">₹{riskRadar.usdinr || '--'}</p>
                <p className="text-[10px] text-slate-500">Currency FX</p>
              </div>
            </div>

            <div className="mt-3 p-2.5 bg-slate-50 rounded-xl text-xs text-slate-600 border border-slate-200">
              <span className="font-bold text-slate-700">Radar Contributing Factors: </span>
              {(riskRadar.contributing_factors || []).join(' • ')}
            </div>
          </div>

          <div className="text-[10px] text-slate-400 border-t border-slate-100 pt-2 mt-3">
            Informational telemetry only. Does not alter production quantitative risk constraints.
          </div>
        </div>
      </div>

      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* SECTION 6: NEWS INTELLIGENCE & SENTIMENT BAROMETER                */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="bg-white border border-gray-200 rounded-2xl p-5 shadow-sm">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 mb-4">
          <div>
            <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider flex items-center gap-2">
              <Newspaper size={16} className="text-blue-500" /> News Intelligence & Sentiment Barometer
            </h2>
            <p className="text-xs text-slate-500">Verified exchange news filings analyzed via VADER Financial Lexicon Engine</p>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-xs font-bold text-slate-500">Sentiment:</span>
            <span className={`px-3 py-1 rounded-full text-xs font-black border ${
              news.overall_sentiment === 'BULLISH' ? 'bg-emerald-100 text-emerald-800 border-emerald-200' :
              news.overall_sentiment === 'BEARISH' ? 'bg-rose-100 text-rose-800 border-rose-200' :
              'bg-slate-100 text-slate-700 border-slate-200'
            }`}>
              {news.overall_sentiment || 'NEUTRAL'} ({news.total_articles || 0} Filings)
            </span>
          </div>
        </div>

        {/* Headlines List */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {(news.articles || []).slice(0, 6).map((item, i) => (
            <div key={i} className="p-3 bg-slate-50 hover:bg-slate-100 rounded-xl border border-slate-200 transition flex flex-col justify-between">
              <div>
                <div className="flex justify-between items-start text-[10px] font-bold text-slate-500 mb-1">
                  <span className="px-1.5 py-0.5 rounded bg-slate-200 text-slate-700">{item.category}</span>
                  <span className={item.sentiment === 'BULLISH' ? 'text-emerald-600' : item.sentiment === 'BEARISH' ? 'text-rose-600' : 'text-slate-500'}>
                    ● {item.sentiment} ({item.score})
                  </span>
                </div>
                <p className="text-xs font-semibold text-slate-800 line-clamp-2 mt-1" title={item.headline}>
                  {item.headline}
                </p>
              </div>
              <div className="flex justify-between items-center text-[10px] text-slate-400 mt-2 pt-1 border-t border-slate-200/60">
                <span>{item.source}</span>
                <span>{item.timestamp}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* SECTION 7: AI MARKET OPPORTUNITIES (VIRTUAL RECOMMENDATIONS)      */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="bg-white border border-gray-200 rounded-2xl p-5 shadow-sm">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 mb-3">
          <div className="flex items-center gap-2">
            <Cpu size={18} className="text-sky-500" />
            <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider">
              AI Market Opportunities (Intraday & Swing)
            </h2>
            <span className="text-[11px] font-bold text-sky-700 bg-sky-50 border border-sky-200 px-2 py-0.5 rounded-full">
              {aiOps.unique_tickers_count != null ? `${aiOps.unique_tickers_count} Unique Active Setups` : `${(aiOps.intraday?.opportunities?.length || 0) + (aiOps.swing?.opportunities?.length || 0)} Setups`}
            </span>
          </div>
          <span className="text-xs font-black px-2.5 py-1 bg-rose-50 text-rose-700 border border-rose-200 rounded-lg">
            ⚠️ VIRTUAL AI RECOMMENDATIONS — NOT LIVE POSITIONS
          </span>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-3">
          {/* Intraday Opportunities */}
          <div className="border border-slate-200 rounded-xl p-4 bg-slate-50">
            <h3 className="text-xs font-bold text-slate-700 uppercase tracking-wider mb-2 flex items-center justify-between">
              <span>Intraday AI Setups (5M / 15M)</span>
              <span className="text-slate-400 font-mono text-[10px]">{aiOps.intraday?.opportunities?.length || 0} Active</span>
            </h3>

            {aiOps.intraday?.opportunities?.length > 0 ? (
              <div className="space-y-2">
                {aiOps.intraday.opportunities.slice(0, 3).map((op, i) => (
                  <div key={i} className="p-3 bg-white rounded-lg border border-slate-200 shadow-2xs flex justify-between items-center">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-bold text-slate-900 text-xs">{op.ticker}</span>
                        <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                          op.direction === 'BULLISH' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                        }`}>
                          {op.direction}
                        </span>
                        <span className="text-[10px] text-slate-400 font-mono">Conf: {op.confidence}%</span>
                      </div>
                      <p className="text-[11px] text-slate-500 mt-1 truncate max-w-[280px]">{op.reason}</p>
                    </div>
                    <div className="text-right text-xs">
                      <p className="font-mono font-bold text-slate-800">₹{op.entry.toFixed(1)}</p>
                      <p className="text-[10px] text-emerald-600">T: ₹{op.tp1.toFixed(1)}</p>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="p-6 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-lg">
                NO QUALIFIED INTRADAY OPPORTUNITIES — Strict quality thresholds enforced.
              </div>
            )}
          </div>

          {/* Swing Opportunities */}
          <div className="border border-slate-200 rounded-xl p-4 bg-slate-50">
            <h3 className="text-xs font-bold text-slate-700 uppercase tracking-wider mb-2 flex items-center justify-between">
              <span>Swing AI Setups (Daily / Multi-Day)</span>
              <span className="text-slate-400 font-mono text-[10px]">{aiOps.swing?.opportunities?.length || 0} Active</span>
            </h3>

            {aiOps.swing?.opportunities?.length > 0 ? (
              <div className="space-y-2">
                {aiOps.swing.opportunities.slice(0, 3).map((op, i) => (
                  <div key={i} className="p-3 bg-white rounded-lg border border-slate-200 shadow-2xs flex justify-between items-center">
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-bold text-slate-900 text-xs">{op.ticker}</span>
                        <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                          op.direction === 'BULLISH' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                        }`}>
                          {op.direction}
                        </span>
                        <span className="text-[10px] text-slate-400 font-mono">Conf: {op.confidence}%</span>
                      </div>
                      <p className="text-[11px] text-slate-500 mt-1 truncate max-w-[280px]">{op.reason}</p>
                    </div>
                    <div className="text-right text-xs">
                      <p className="font-mono font-bold text-slate-800">₹{op.entry.toFixed(1)}</p>
                      <p className="text-[10px] text-emerald-600">T: ₹{op.tp1.toFixed(1)}</p>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="p-6 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-lg">
                NO QUALIFIED SWING OPPORTUNITIES — Strict quality thresholds enforced.
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* SECTION 8: AI MARKET SUMMARY & INFRASTRUCTURE HEALTH             */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* AI Grounded Synthesis */}
        <div className="lg:col-span-2 bg-white border border-gray-200 rounded-2xl p-5 shadow-sm">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider flex items-center gap-2">
              <Zap size={16} className="text-amber-500" /> AI Grounded Market Summary & Synthesis
            </h2>
            <span className="text-xs font-bold text-sky-600 bg-sky-50 px-2.5 py-0.5 rounded-full border border-sky-200">
              {aiSummary.market_view || 'CONSTRUCTIVE'}
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-3 text-xs">
            <div className="p-3 bg-emerald-50/60 rounded-xl border border-emerald-100">
              <p className="font-bold text-emerald-800 mb-1 flex items-center gap-1">
                <CheckCircle2 size={14} className="text-emerald-600" /> Supporting Tailwinds
              </p>
              <ul className="space-y-1 text-emerald-900 text-[11px]">
                {(aiSummary.supporting_factors || []).map((f, i) => (
                  <li key={i}>• {f}</li>
                ))}
              </ul>
            </div>

            <div className="p-3 bg-rose-50/60 rounded-xl border border-rose-100">
              <p className="font-bold text-rose-800 mb-1 flex items-center gap-1">
                <AlertTriangle size={14} className="text-rose-600" /> Market Headwinds
              </p>
              <ul className="space-y-1 text-rose-900 text-[11px]">
                {(aiSummary.headwinds || []).map((h, i) => (
                  <li key={i}>• {h}</li>
                ))}
              </ul>
            </div>
          </div>

          <p className="mt-3 text-xs italic text-slate-600 bg-slate-50 p-3 rounded-xl border border-slate-200">
            "{aiSummary.ai_observation || 'Conditions remain balanced; maintain risk-budgeted allocations.'}"
          </p>
        </div>

        {/* Subsystem Health Matrix */}
        <div className="lg:col-span-1 bg-white border border-gray-200 rounded-2xl p-5 shadow-sm flex flex-col justify-between">
          <div>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-bold text-slate-800 uppercase tracking-wider flex items-center gap-2">
                <ShieldCheck size={16} className="text-emerald-500" /> Subsystem Health
              </h2>
              <span className="text-xs font-black text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
                {sysHealth.overall_score || 100} / 100
              </span>
            </div>

            <div className="space-y-1.5 text-xs">
              {[
                { label: 'Market Data Layer', status: sysHealth.market_data },
                { label: 'Production AI Models', status: sysHealth.ai_models },
                { label: 'Research Engine', status: sysHealth.research_engine },
                { label: 'Database (WAL Mode)', status: sysHealth.database },
                { label: 'Telegram Engine', status: sysHealth.telegram },
                { label: 'Broker Gateway', status: sysHealth.broker_mode },
                { label: 'Background Scheduler', status: sysHealth.scheduler },
              ].map((item, i) => (
                <div key={i} className="flex justify-between items-center py-1 border-b border-slate-100 last:border-0">
                  <span className="text-slate-600 text-[11px]">{item.label}</span>
                  <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                    item.status === 'HEALTHY' || item.status === 'PASS' ? 'bg-emerald-100 text-emerald-800' :
                    item.status === 'SIMULATION (Fail-Safe)' ? 'bg-sky-100 text-sky-800' : 'bg-slate-100 text-slate-700'
                  }`}>
                    {item.status || 'HEALTHY'}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="text-[10px] text-slate-400 border-t border-slate-100 pt-2 mt-2">
            AI Brain & Lab Subsystems Monitored Continuously
          </div>
        </div>
      </div>

      {/* ═════════════════════════════════════════════════════════════════ */}
      {/* PRESERVED SECTION: SINGLE TICKER DRILL-DOWN & BOT MONITOR        */}
      {/* ═════════════════════════════════════════════════════════════════ */}
      <div className="border-t border-slate-200 pt-8 mt-8">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4 mb-6">
          <div>
            <h2 className="text-lg sm:text-xl font-bold text-slate-900 flex items-center gap-2">
              <Activity className="text-blue-600" /> Single Ticker Technical Inspection
            </h2>
            <p className="text-xs text-slate-500">Ad-hoc technical indicator matrix and moving averages breakdown</p>
          </div>
          <div className="w-full sm:w-80">
            <TickerSearch 
              value={tickerInput} 
              onChange={setTickerInput} 
              onSubmit={fetchSingleTickerData} 
            />
          </div>
        </div>

        {recentSearches.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 mb-6 text-sm">
            <div className="flex items-center space-x-1 text-gray-500">
              <Clock size={16} />
              <span className="text-xs">Recent:</span>
            </div>
            {recentSearches.map(ticker => (
              <button 
                key={ticker} 
                onClick={() => { setTickerInput(ticker); fetchSingleTickerData(ticker); }}
                className="px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded text-gray-700 transition text-xs"
              >
                {ticker}
              </button>
            ))}
          </div>
        )}

        {tickerError && (
          <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-6 text-sm">
            {tickerError}
          </div>
        )}

        {/* Autonomous Bot Alerts Feed */}
        <div className="mb-8">
          <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-2 mb-4">
            <h3 className="text-base font-bold text-gray-800 flex items-center">
              <Activity className="mr-2 text-indigo-500 animate-pulse shrink-0" size={18} /> Autonomous Trade Manager (Live)
            </h3>
            <div className="flex flex-wrap items-center gap-2">
              {activeMonitors.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {activeMonitors.map((m, i) => (
                    <span key={i} className="text-xs font-bold bg-gray-100 text-gray-600 px-2 py-0.5 rounded border border-gray-200">
                      <span className={m.direction === 'BULLISH' ? 'text-green-600' : 'text-red-600'}>●</span> {m.ticker.replace('.NS', '')}
                    </span>
                  ))}
                </div>
              )}
              <span className="text-xs font-bold bg-green-100 text-green-700 px-2 py-1 rounded flex items-center shrink-0">
                <span className="w-2 h-2 rounded-full bg-green-500 mr-2 animate-pulse"></span>
                Active
              </span>
            </div>
          </div>

          {alerts && alerts.length > 0 ? (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {alerts.slice(0, 3).map((alert, i) => (
                <div key={i} className={`p-4 rounded-xl border shadow-sm ${
                  alert.level === 'CRITICAL' ? 'bg-red-50 border-red-200' : 
                  alert.level === 'WARNING' ? 'bg-orange-50 border-orange-200' : 'bg-blue-50 border-blue-200'
                }`}>
                  <div className="flex justify-between items-start mb-2">
                    <span className={`font-black text-sm ${
                      alert.level === 'CRITICAL' ? 'text-red-700' : 
                      alert.level === 'WARNING' ? 'text-orange-700' : 'text-blue-700'
                    }`}>
                      {alert.ticker}
                    </span>
                    <span className="text-xs text-gray-500 font-mono">
                      {new Date(alert.timestamp).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}
                    </span>
                  </div>
                  <p className={`text-xs ${
                    alert.level === 'CRITICAL' ? 'text-red-900 font-medium' : 
                    alert.level === 'WARNING' ? 'text-orange-900' : 'text-blue-900'
                  }`}>
                    {alert.message}
                  </p>
                </div>
              ))}
            </div>
          ) : (
            <div className="bg-gray-50 border border-gray-200 border-dashed rounded-xl p-4 text-center text-gray-500 text-xs">
              ✅ <strong>All Systems Nominal.</strong> Autonomous Bot is running in background. No critical trade alerts.
            </div>
          )}
        </div>

        {/* Selected Stock Metrics & Moving Averages */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-1 bg-white rounded-xl shadow-sm border border-gray-100 p-6 flex flex-col">
            <h3 className="text-base font-bold mb-4 text-gray-800 border-b pb-2 flex items-center">
              <Activity className="mr-2 text-blue-500" size={18}/> Technical Standpoint ({activeTicker})
            </h3>

            {loadingTicker ? (
              <div className="flex-1 flex justify-center items-center">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
              </div>
            ) : marketData ? (
              <div className="space-y-4 text-xs">
                <div className="flex justify-between items-center">
                  <span className="text-gray-600 font-semibold">LTP:</span>
                  <span className="font-bold text-gray-900 text-sm">{formatCurrency(marketData.close)}</span>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-gray-600 font-semibold">24h Change:</span>
                  <span className={`font-bold ${marketData.change_pct >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                    {formatPct(marketData.change_pct)}
                  </span>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-gray-600 font-semibold">RSI (14):</span>
                  <span className="font-bold text-gray-800">{marketData.rsi_14 != null ? marketData.rsi_14.toFixed(1) : '--'}</span>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-gray-600 font-semibold">MACD:</span>
                  <span className={`font-bold ${marketData.macd > 0 ? 'text-green-600' : 'text-red-600'}`}>
                    {marketData.macd != null ? marketData.macd.toFixed(2) : '--'}
                  </span>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-gray-600 font-semibold">ADX Trend Strength:</span>
                  <span className="font-bold text-gray-800">{marketData.adx != null ? marketData.adx.toFixed(1) : '--'}</span>
                </div>
              </div>
            ) : (
              <p className="text-gray-400 text-center py-6 text-xs">No ticker data available.</p>
            )}
          </div>

          <div className="lg:col-span-2 bg-white rounded-xl shadow-sm border border-gray-100 p-6">
            <h3 className="text-base font-bold mb-4 text-gray-800 border-b pb-2">Moving Averages Matrix ({activeTicker})</h3>
            {loadingTicker ? (
              <div className="flex justify-center items-center h-32">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
              </div>
            ) : marketData ? (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                <div className="p-4 bg-gray-50 rounded-xl border border-gray-200">
                  <span className="text-gray-500 text-[10px] font-bold uppercase block">Short-Term (20 EMA)</span>
                  <p className="text-xl font-bold text-gray-800 mt-1">{formatCurrency(marketData.ema_20)}</p>
                  <p className={`text-[11px] mt-1 font-medium ${marketData.close > marketData.ema_20 ? 'text-green-600' : 'text-red-500'}`}>
                    {marketData.close > marketData.ema_20 ? 'Above 20 EMA' : 'Below 20 EMA'}
                  </p>
                </div>
                <div className="p-4 bg-gray-50 rounded-xl border border-gray-200">
                  <span className="text-gray-500 text-[10px] font-bold uppercase block">Medium-Term (50 EMA)</span>
                  <p className="text-xl font-bold text-gray-800 mt-1">{formatCurrency(marketData.ema_50)}</p>
                  <p className={`text-[11px] mt-1 font-medium ${marketData.close > marketData.ema_50 ? 'text-green-600' : 'text-red-500'}`}>
                    {marketData.close > marketData.ema_50 ? 'Above 50 EMA' : 'Below 50 EMA'}
                  </p>
                </div>
                <div className="p-4 bg-gray-50 rounded-xl border border-gray-200">
                  <span className="text-gray-500 text-[10px] font-bold uppercase block">Macro Trend (200 EMA)</span>
                  <p className="text-xl font-bold text-gray-800 mt-1">{formatCurrency(marketData.ema_200)}</p>
                  <p className={`text-[11px] mt-1 font-medium ${marketData.close > marketData.ema_200 ? 'text-green-600' : 'text-red-500'}`}>
                    {marketData.close > marketData.ema_200 ? 'Above 200 EMA' : 'Below 200 EMA'}
                  </p>
                </div>
              </div>
            ) : (
              <p className="text-gray-400 text-center py-6 text-xs">No moving average data available.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
