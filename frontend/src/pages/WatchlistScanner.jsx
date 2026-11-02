import React, { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { 
  RefreshCw, Plus, Trash2, Search, Zap, TrendingUp, TrendingDown, 
  Layers, ArrowUpRight, ArrowDownRight, ShieldCheck, ExternalLink, 
  Sliders, ChevronRight, X, AlertCircle, Clock, CheckCircle2, BarChart2
} from 'lucide-react';
import { 
  getWatchlist, addToWatchlist, removeFromWatchlist, 
  migrateLegacyWatchlist, getWatchlistPresets, scanWatchlistBatch 
} from '../services/api';
import TickerAutocomplete from '../components/TickerAutocomplete';
import { useLiveIndicator } from '../context/LiveIndicatorContext';

export default function WatchlistScanner() {
  const navigate = useNavigate();
  const { triggerFetchIndicator } = useLiveIndicator();

  // Presets & Active Selection
  const [presets, setPresets] = useState([
    { id: 'WATCHLIST', name: 'My Watchlist (Backend)', description: 'User personal operational watchlist in SQLite' },
    { id: 'NIFTY_50', name: 'NIFTY 50', description: 'Top 50 large-cap benchmark equities' },
    { id: 'BANK_NIFTY', name: 'Bank Nifty', description: '12 liquid banking leaders' },
    { id: 'NIFTY_IT', name: 'NIFTY IT', description: '10 technology leaders' },
    { id: 'LIVE_52', name: 'Live Scanner (52)', description: 'Core 52 multi-factor scanner equities' }
  ]);
  const [selectedPreset, setSelectedPreset] = useState('WATCHLIST');

  // Watchlist State (Sole Authoritative Backend SQLite)
  const [watchlistTickers, setWatchlistTickers] = useState([]);
  const [newTickerInput, setNewTickerInput] = useState('');
  
  // Scan State & Telemetry
  const [scanData, setScanData] = useState([]);
  const [kpis, setKpis] = useState(null);
  const [loading, setLoading] = useState(false);
  const [lastScanTime, setLastScanTime] = useState(null);

  // Filters & Sorting
  const [filterMode, setFilterMode] = useState('ALL'); // ALL, BULLISH_AI, BEARISH_AI, VOLUME_SURGE, OVERSOLD, BREAKOUT
  const [searchQuery, setSearchQuery] = useState('');
  const [sortBy, setSortBy] = useState('CONVICTION'); // CONVICTION, VOLUME_SURGE, CHANGE, TECH_SCORE

  // Deep Inspection Drawer
  const [inspectedStock, setInspectedStock] = useState(null);

  // 1. Initial Load & Safe Migration (Enforces Invariants 1 & 8)
  useEffect(() => {
    async function initializeWatchlist() {
      try {
        // Migration Safety: Check if legacy browser localStorage exists
        const legacyLocal = localStorage.getItem('watchlist');
        if (legacyLocal) {
          try {
            const parsed = JSON.parse(legacyLocal);
            if (Array.isArray(parsed) && parsed.length > 0) {
              // Ingest to backend first
              const migRes = await migrateLegacyWatchlist(parsed);
              // Invariant 1: Only delete localStorage after backend confirms persistence!
              if (migRes && migRes.verified_in_backend) {
                localStorage.removeItem('watchlist');
                console.info("Watchlist successfully migrated to backend SQLite and localStorage purged.");
              }
            } else {
              localStorage.removeItem('watchlist');
            }
          } catch (migErr) {
            console.warn("Legacy migration deferred to prevent data loss:", migErr);
          }
        }

        // Fetch authoritative presets & watchlist from backend SQLite
        const [presetsRes, wlRes] = await Promise.all([
          getWatchlistPresets().catch(() => null),
          getWatchlist()
        ]);

        if (presetsRes?.presets) {
          setPresets(presetsRes.presets);
        }

        if (wlRes?.tickers) {
          setWatchlistTickers(wlRes.tickers);
        }
      } catch (err) {
        console.error("Failed to initialize backend watchlist:", err);
      }
    }

    initializeWatchlist();
  }, []);

  // 2. Trigger Scan whenever preset changes
  useEffect(() => {
    executeScan(selectedPreset);
  }, [selectedPreset]);

  // Execute High-Speed Batch Scan
  const executeScan = async (presetId) => {
    setLoading(true);
    try {
      const resp = await scanWatchlistBatch(presetId);
      if (resp?.status === 'success') {
        setScanData(resp.results || []);
        setKpis(resp.kpi_summary || null);
        setLastScanTime(new Date());

        // Update live status bar if first valid item has timestamp
        const validItem = resp.results?.find(r => !r.error);
        if (validItem) {
          triggerFetchIndicator({
            source: validItem.data_source,
            timestamp: validItem.data_timestamp
          });
        }
      }
    } catch (e) {
      console.error("Batch scan failed:", e);
    } finally {
      setLoading(false);
    }
  };

  // Add Ticker to Authoritative Backend
  const handleAddTicker = async (e) => {
    e?.preventDefault();
    const clean = newTickerInput.trim().toUpperCase();
    if (!clean) return;

    try {
      const resp = await addToWatchlist(clean);
      if (resp?.tickers) {
        setWatchlistTickers(resp.tickers);
        setNewTickerInput('');
        if (selectedPreset === 'WATCHLIST') {
          executeScan('WATCHLIST');
        }
      }
    } catch (err) {
      alert(`Could not add ticker: ${err.response?.data?.detail || err.message}`);
    }
  };

  // Remove Ticker from Authoritative Backend
  const handleRemoveTicker = async (ticker, e) => {
    e?.stopPropagation();
    try {
      const resp = await removeFromWatchlist(ticker);
      if (resp?.tickers) {
        setWatchlistTickers(resp.tickers);
        setScanData(prev => prev.filter(item => item.ticker !== ticker));
      }
    } catch (err) {
      alert(`Could not delete ticker: ${err.response?.data?.detail || err.message}`);
    }
  };

  // Filtered & Sorted Rows
  const displayedRows = useMemo(() => {
    let rows = [...scanData];

    // Search query
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toUpperCase();
      rows = rows.filter(r => r.ticker.toUpperCase().includes(q));
    }

    // Filter modes
    if (filterMode === 'BULLISH_AI') {
      rows = rows.filter(r => r.ai_signal === 'BULLISH' && r.ai_conviction >= 60);
    } else if (filterMode === 'BEARISH_AI') {
      rows = rows.filter(r => r.ai_signal === 'BEARISH' && r.ai_conviction >= 60);
    } else if (filterMode === 'VOLUME_SURGE') {
      rows = rows.filter(r => (r.volume_surge || 0) >= 1.4);
    } else if (filterMode === 'OVERSOLD') {
      rows = rows.filter(r => (r.rsi || 50) <= 35);
    } else if (filterMode === 'BREAKOUT') {
      rows = rows.filter(r => (r.setup_tag || '').includes('Breakout'));
    }

    // Sorting
    rows.sort((a, b) => {
      if (sortBy === 'CONVICTION') {
        return (b.ai_conviction || 0) - (a.ai_conviction || 0);
      }
      if (sortBy === 'VOLUME_SURGE') {
        return (b.volume_surge || 0) - (a.volume_surge || 0);
      }
      if (sortBy === 'CHANGE') {
        return (b.change_pct || 0) - (a.change_pct || 0);
      }
      if (sortBy === 'TECH_SCORE') {
        return (b.technical_score || 0) - (a.technical_score || 0);
      }
      return 0;
    });

    return rows;
  }, [scanData, searchQuery, filterMode, sortBy]);

  return (
    <div className="min-h-screen bg-slate-50/60 pb-16 text-slate-800">
      {/* ── TOP HEADER & TERMINAL CONTROL BAR ────────────────────────── */}
      <div className="bg-white border-b border-slate-200 px-6 py-5 shadow-xs sticky top-0 z-20">
        <div className="max-w-7xl mx-auto flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="bg-blue-600 text-white p-1.5 rounded-lg shadow-sm">
                <BarChart2 size={18} />
              </span>
              <h1 className="text-xl font-black tracking-tight text-slate-900">
                Institutional Watchlist Terminal
              </h1>
              <span className="text-[11px] font-bold uppercase tracking-wider bg-emerald-50 text-emerald-700 border border-emerald-200 px-2 py-0.5 rounded-md flex items-center gap-1">
                <ShieldCheck size={12} /> Backend SQLite
              </span>
            </div>
            <p className="text-xs text-slate-500 mt-1">
              Parallel multi-factor screening powered by Authoritative Champion Decision Engine & Kelly sizing.
            </p>
          </div>

          {/* Quick Refresh & Telemetry */}
          <div className="flex items-center gap-3">
            {lastScanTime && (
              <span className="text-xs text-slate-400 flex items-center gap-1">
                <Clock size={13} /> Scanned {lastScanTime.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
              </span>
            )}
            <button
              onClick={() => executeScan(selectedPreset)}
              disabled={loading}
              className={`flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-bold text-white shadow-sm transition-all ${
                loading ? 'bg-blue-400 cursor-not-allowed' : 'bg-blue-600 hover:bg-blue-700 active:scale-98'
              }`}
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
              {loading ? 'Scanning...' : 'Rescan Pool'}
            </button>
          </div>
        </div>

        {/* Preset Universe Tabs */}
        <div className="max-w-7xl mx-auto mt-4 flex items-center gap-2 overflow-x-auto pb-1 scrollbar-none">
          {presets.map((preset) => (
            <button
              key={preset.id}
              onClick={() => setSelectedPreset(preset.id)}
              className={`px-3.5 py-1.5 rounded-xl text-xs font-bold whitespace-nowrap transition-all flex items-center gap-1.5 border ${
                selectedPreset === preset.id
                  ? 'bg-slate-900 text-white border-slate-900 shadow-sm'
                  : 'bg-white text-slate-600 hover:bg-slate-100/80 border-slate-200'
              }`}
            >
              <Layers size={13} className={selectedPreset === preset.id ? 'text-blue-400' : 'text-slate-400'} />
              {preset.name}
              {preset.id === 'WATCHLIST' && (
                <span className="text-[10px] ml-1 px-1.5 py-0.2 rounded-full bg-blue-500/20 text-blue-300 font-mono">
                  {watchlistTickers.length}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-6 mt-6 space-y-6">
        {/* ── KPI TELEMETRY STRIP ───────────────────────────────────── */}
        {kpis && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
              <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider block">Market Breath</span>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-xl font-black text-emerald-600">▲ {kpis.advancing}</span>
                <span className="text-slate-300">/</span>
                <span className="text-xl font-black text-rose-600">▼ {kpis.declining}</span>
              </div>
              <span className="text-[11px] text-slate-400 mt-1 block">Advancing vs Declining</span>
            </div>

            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
              <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider block">High Conviction AI</span>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-xl font-black text-emerald-600">{kpis.bullish_ai_count} Long</span>
                <span className="text-slate-300">|</span>
                <span className="text-xl font-black text-rose-600">{kpis.bearish_ai_count} Short</span>
              </div>
              <span className="text-[11px] text-slate-400 mt-1 block">≥60% Calibrated Conviction</span>
            </div>

            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
              <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider block">Avg Volume Surge</span>
              <div className="text-xl font-black text-slate-900 mt-1 flex items-center gap-1.5">
                <Zap size={18} className="text-amber-500 fill-amber-500" />
                {kpis.avg_volume_surge}x
              </div>
              <span className="text-[11px] text-slate-400 mt-1 block">vs 20-Day SMA Volume</span>
            </div>

            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
              <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider block">Risk Framework</span>
              <div className="text-sm font-black text-slate-900 mt-1 flex items-center gap-1.5 text-emerald-600">
                <ShieldCheck size={18} /> Zero Order Risk
              </div>
              <span className="text-[11px] text-slate-400 mt-1 block">Informational Review Mode</span>
            </div>
          </div>
        )}

        {/* ── SEARCH, ADD TICKER, & FILTER STRIP ────────────────────── */}
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs space-y-4">
          <div className="flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-4">
            {/* Live Search */}
            <div className="relative flex-1">
              <Search size={16} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                placeholder="Filter symbols in current scan..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full pl-10 pr-4 py-2 text-xs bg-slate-50 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-blue-500 focus:bg-white transition"
              />
            </div>

            {/* Add Symbol Input (Direct to Backend SQLite) */}
            <form onSubmit={handleAddTicker} className="flex items-center gap-2">
              <div className="w-56">
                <TickerAutocomplete
                  value={newTickerInput}
                  onChange={setNewTickerInput}
                  onSubmit={() => handleAddTicker()}
                  placeholder="Add symbol (e.g. TATAMOTORS)"
                  multiSelect={false}
                  showPresets={false}
                  variant="light"
                />
              </div>
              <button
                type="submit"
                className="px-3.5 py-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-white text-xs font-bold flex items-center gap-1 shadow-xs transition shrink-0"
              >
                <Plus size={14} /> Add to Watchlist
              </button>
            </form>

            {/* Sort Selector */}
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-400 font-medium">Sort:</span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                className="text-xs bg-slate-50 border border-slate-200 rounded-xl px-3 py-2 font-medium text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                <option value="CONVICTION">AI Conviction</option>
                <option value="VOLUME_SURGE">Volume Surge</option>
                <option value="CHANGE">1D Return (%)</option>
                <option value="TECH_SCORE">Technical Score</option>
              </select>
            </div>
          </div>

          {/* Filter Chips */}
          <div className="flex items-center gap-2 overflow-x-auto pb-1 scrollbar-none text-xs">
            {[
              { id: 'ALL', label: `All Symbols (${displayedRows.length})` },
              { id: 'BULLISH_AI', label: '🔥 AI Bullish (≥60%)' },
              { id: 'BEARISH_AI', label: '❄️ AI Bearish (≥60%)' },
              { id: 'VOLUME_SURGE', label: '⚡ Volume Surge (≥1.4x)' },
              { id: 'BREAKOUT', label: '🎯 Breakout Setups' },
              { id: 'OVERSOLD', label: '📉 Oversold (RSI ≤35)' }
            ].map(tab => (
              <button
                key={tab.id}
                onClick={() => setFilterMode(tab.id)}
                className={`px-3 py-1 rounded-lg font-semibold transition-all whitespace-nowrap ${
                  filterMode === tab.id
                    ? 'bg-blue-50 text-blue-700 border border-blue-200'
                    : 'bg-slate-100/70 text-slate-500 hover:bg-slate-100 hover:text-slate-700'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        {/* ── TERMINAL DATA GRID ────────────────────────────────────── */}
        <div className="bg-white border border-slate-200 rounded-2xl shadow-xs overflow-hidden">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-slate-100 text-left">
              <thead>
                <tr className="bg-slate-50/80 text-[11px] font-bold text-slate-500 uppercase tracking-wider">
                  <th className="py-3.5 px-4">Symbol & Freshness</th>
                  <th className="py-3.5 px-4 text-right">LTP (₹)</th>
                  <th className="py-3.5 px-4 text-right">1D Change</th>
                  <th className="py-3.5 px-4 text-center">Volume Surge</th>
                  <th className="py-3.5 px-4 text-center">Setup Tag</th>
                  <th className="py-3.5 px-4 text-center">Tech Score</th>
                  <th className="py-3.5 px-4 text-center">Authoritative AI Signal</th>
                  <th className="py-3.5 px-4 text-right">Dynamic SL / TP</th>
                  <th className="py-3.5 px-4 text-center">Inspection & Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-xs">
                {displayedRows.length === 0 ? (
                  <tr>
                    <td colSpan="9" className="py-12 text-center text-slate-400">
                      {loading ? (
                        <div className="flex flex-col items-center gap-2">
                          <RefreshCw size={24} className="animate-spin text-blue-500" />
                          <span className="font-semibold text-slate-600">Screening pool with Authoritative Decision Engine...</span>
                        </div>
                      ) : (
                        <div>
                          <p className="font-bold text-slate-600 text-sm">No symbols match current filters.</p>
                          <p className="text-[11px] mt-1">Try selecting a different filter chip or adding symbols above.</p>
                        </div>
                      )}
                    </td>
                  </tr>
                ) : (
                  displayedRows.map((stock) => {
                    const isLong = stock.ai_signal === 'BULLISH';
                    const isShort = stock.ai_signal === 'BEARISH';
                    const isFresh = stock.data_freshness === 'LIVE' || stock.data_freshness === 'DAILY_EOD';

                    return (
                      <tr 
                        key={stock.ticker}
                        onClick={() => setInspectedStock(stock)}
                        className="hover:bg-blue-50/40 transition-colors cursor-pointer group"
                      >
                        {/* Symbol & Freshness */}
                        <td className="py-3.5 px-4">
                          <div className="flex items-center gap-2">
                            <span className="font-bold text-slate-900 font-mono text-sm">{stock.ticker}</span>
                            <span className={`text-[9px] font-bold px-1.5 py-0.2 rounded border ${
                              isFresh ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-amber-50 text-amber-700 border-amber-200'
                            }`}>
                              {stock.data_freshness}
                            </span>
                          </div>
                          <span className="text-[10px] text-slate-400 block mt-0.5">
                            {stock.data_source || 'Cache'} • {stock.data_timestamp?.slice(0, 10)}
                          </span>
                        </td>

                        {/* LTP */}
                        <td className="py-3.5 px-4 text-right font-bold font-mono text-slate-900">
                          ₹{stock.ltp != null ? stock.ltp.toFixed(2) : '-'}
                        </td>

                        {/* 1D Change */}
                        <td className="py-3.5 px-4 text-right">
                          <span className={`inline-flex items-center font-bold px-2 py-0.5 rounded-md ${
                            stock.change_pct >= 0 ? 'text-emerald-700 bg-emerald-50' : 'text-rose-700 bg-rose-50'
                          }`}>
                            {stock.change_pct >= 0 ? <ArrowUpRight size={12} className="mr-0.5" /> : <ArrowDownRight size={12} className="mr-0.5" />}
                            {stock.change_pct >= 0 ? '+' : ''}{stock.change_pct?.toFixed(2)}%
                          </span>
                        </td>

                        {/* Volume Surge */}
                        <td className="py-3.5 px-4 text-center">
                          <span className={`inline-flex items-center gap-1 font-bold text-xs px-2 py-0.5 rounded-lg border ${
                            (stock.volume_surge || 0) >= 1.5
                              ? 'bg-amber-50 text-amber-800 border-amber-200'
                              : 'bg-slate-50 text-slate-600 border-slate-200'
                          }`}>
                            {(stock.volume_surge || 0) >= 1.5 && <Zap size={11} className="text-amber-500 fill-amber-500" />}
                            {stock.volume_surge?.toFixed(1)}x
                          </span>
                        </td>

                        {/* Setup Classification */}
                        <td className="py-3.5 px-4 text-center">
                          <span className="text-[11px] font-semibold px-2 py-0.5 rounded-md bg-slate-100 text-slate-700 border border-slate-200">
                            {stock.setup_tag || 'Consolidation'}
                          </span>
                        </td>

                        {/* Technical Score (Informational) */}
                        <td className="py-3.5 px-4 text-center">
                          <div className="inline-flex flex-col items-center">
                            <span className="font-bold text-slate-700">{stock.technical_score || 50}/100</span>
                            <div className="w-12 bg-slate-100 h-1 rounded-full overflow-hidden mt-1">
                              <div 
                                className={`h-full ${
                                  (stock.technical_score || 50) >= 70 ? 'bg-emerald-500' :
                                  (stock.technical_score || 50) <= 35 ? 'bg-rose-500' : 'bg-blue-500'
                                }`}
                                style={{ width: `${stock.technical_score || 50}%` }}
                              />
                            </div>
                          </div>
                        </td>

                        {/* Authoritative AI Signal */}
                        <td className="py-3.5 px-4 text-center">
                          <div className="inline-flex flex-col items-center">
                            <span className={`text-xs font-black px-2.5 py-1 rounded-lg border shadow-2xs ${
                              isLong ? 'bg-emerald-50 text-emerald-700 border-emerald-300' :
                              isShort ? 'bg-rose-50 text-rose-700 border-rose-300' :
                              'bg-slate-100 text-slate-600 border-slate-200'
                            }`}>
                              {stock.ai_signal} {stock.ai_conviction}%
                            </span>
                            {stock.ai_qualified ? (
                              <span className="text-[10px] text-emerald-600 font-semibold mt-0.5">● Qualified</span>
                            ) : (
                              <span className="text-[10px] text-slate-400 mt-0.5">Watch</span>
                            )}
                          </div>
                        </td>

                        {/* Dynamic ATR SL / TP */}
                        <td className="py-3.5 px-4 text-right font-mono text-[11px]">
                          <div><span className="text-slate-400">SL:</span> <span className="font-semibold text-rose-600">₹{stock.sl?.toFixed(1) || '-'}</span></div>
                          <div><span className="text-slate-400">TP:</span> <span className="font-semibold text-emerald-600">₹{stock.tp1?.toFixed(1) || '-'}</span></div>
                        </td>

                        {/* Actions */}
                        <td className="py-3.5 px-4 text-center" onClick={(e) => e.stopPropagation()}>
                          <div className="flex items-center justify-center gap-2">
                            <button
                              onClick={() => setInspectedStock(stock)}
                              className="px-2.5 py-1 text-xs font-bold text-blue-600 bg-blue-50 hover:bg-blue-100 border border-blue-200 rounded-lg transition"
                            >
                              Inspect
                            </button>
                            {selectedPreset === 'WATCHLIST' && (
                              <button
                                onClick={(e) => handleRemoveTicker(stock.ticker, e)}
                                title="Remove from backend watchlist"
                                className="p-1 text-slate-400 hover:text-rose-600 transition"
                              >
                                <Trash2 size={14} />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* ── SLIDE-OUT DEEP INSPECTION DRAWER ─────────────────────────── */}
      {inspectedStock && (
        <div className="fixed inset-0 z-50 overflow-hidden bg-slate-900/40 backdrop-blur-xs flex justify-end">
          <div className="w-full max-w-lg bg-white h-full shadow-2xl p-6 overflow-y-auto flex flex-col justify-between border-l border-slate-200 animate-in slide-in-from-right duration-200">
            <div className="space-y-6">
              {/* Header */}
              <div className="flex items-start justify-between border-b border-slate-100 pb-4">
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-2xl font-black text-slate-900 font-mono">{inspectedStock.ticker}</h2>
                    <span className="text-[10px] font-bold px-2 py-0.5 rounded border bg-blue-50 text-blue-700 border-blue-200">
                      {inspectedStock.setup_tag}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 mt-1">
                    <span className="text-xl font-black text-slate-900 font-mono">₹{inspectedStock.ltp?.toFixed(2)}</span>
                    <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${
                      inspectedStock.change_pct >= 0 ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'
                    }`}>
                      {inspectedStock.change_pct >= 0 ? '+' : ''}{inspectedStock.change_pct?.toFixed(2)}%
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => setInspectedStock(null)}
                  className="p-1.5 text-slate-400 hover:text-slate-700 hover:bg-slate-100 rounded-lg transition"
                >
                  <X size={20} />
                </button>
              </div>

              {/* Authoritative AI Decision Engine Verdict */}
              <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold uppercase tracking-wider text-slate-500">
                    Authoritative AI Decision Engine
                  </span>
                  <span className={`text-xs font-black px-2 py-0.5 rounded ${
                    inspectedStock.ai_signal === 'BULLISH' ? 'bg-emerald-100 text-emerald-800' :
                    inspectedStock.ai_signal === 'BEARISH' ? 'bg-rose-100 text-rose-800' : 'bg-slate-200 text-slate-700'
                  }`}>
                    {inspectedStock.ai_signal} {inspectedStock.ai_conviction}%
                  </span>
                </div>

                <div className="grid grid-cols-2 gap-3 text-xs">
                  <div>
                    <span className="text-slate-400 block text-[11px]">Gate Eligibility:</span>
                    <span className={`font-bold ${inspectedStock.ai_qualified ? 'text-emerald-600' : 'text-slate-600'}`}>
                      {inspectedStock.ai_qualified ? 'QUALIFIED FOR EXECUTION' : 'FILTERED / WATCH ONLY'}
                    </span>
                  </div>
                  <div>
                    <span className="text-slate-400 block text-[11px]">Decision Brain:</span>
                    <span className="font-bold text-slate-800">Production Champion Ensemble</span>
                  </div>
                </div>

                {inspectedStock.rejection_reason && (
                  <div className="text-[11px] text-amber-700 bg-amber-50 border border-amber-200 p-2 rounded-lg flex items-center gap-1.5">
                    <AlertCircle size={14} className="shrink-0" />
                    <span>Filter Reason: {inspectedStock.rejection_reason}</span>
                  </div>
                )}
              </div>

              {/* Informational Fractional Kelly Sizing (Invariant 4) */}
              <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold uppercase tracking-wider text-slate-500">
                    Informational Kelly Position Sizing
                  </span>
                  <span className="text-[10px] text-blue-600 bg-blue-50 px-2 py-0.5 rounded border border-blue-100 font-semibold">
                    Half-Kelly Mode
                  </span>
                </div>

                <div className="grid grid-cols-3 gap-3 text-center">
                  <div className="bg-slate-50 p-2 rounded-lg border border-slate-100">
                    <span className="text-[10px] text-slate-400 uppercase font-semibold block">Suggested Shares</span>
                    <span className="text-base font-black text-slate-900 font-mono">
                      {inspectedStock.kelly_sizing?.recommended_qty || 0}
                    </span>
                  </div>
                  <div className="bg-slate-50 p-2 rounded-lg border border-slate-100">
                    <span className="text-[10px] text-slate-400 uppercase font-semibold block">Allocated Risk</span>
                    <span className="text-base font-black text-slate-900 font-mono">
                      {inspectedStock.kelly_sizing?.allocated_risk_pct?.toFixed(1)}%
                    </span>
                  </div>
                  <div className="bg-slate-50 p-2 rounded-lg border border-slate-100">
                    <span className="text-[10px] text-slate-400 uppercase font-semibold block">Reward : Risk</span>
                    <span className="text-base font-black text-emerald-600 font-mono">
                      1 : {inspectedStock.kelly_sizing?.reward_risk_ratio || 2.0}
                    </span>
                  </div>
                </div>

                <p className="text-[10px] text-slate-400">
                  Computed against nominal ₹1,00,000 equity using dynamic ATR stop levels. Zero live broker orders created.
                </p>
              </div>

              {/* Dynamic ATR Volatility Framework */}
              <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
                <span className="text-xs font-bold uppercase tracking-wider text-slate-500 block">
                  Dynamic Volatility Targets (14-ATR)
                </span>

                <div className="space-y-2 text-xs font-mono">
                  <div className="flex justify-between items-center p-2 bg-rose-50 rounded-lg border border-rose-100">
                    <span className="font-bold text-rose-700">Dynamic Stop Loss (1.5x ATR)</span>
                    <span className="font-bold text-rose-800">₹{inspectedStock.sl?.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between items-center p-2 bg-emerald-50 rounded-lg border border-emerald-100">
                    <span className="font-bold text-emerald-700">Target 1 (2.0x ATR)</span>
                    <span className="font-bold text-emerald-800">₹{inspectedStock.tp1?.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between items-center p-2 bg-emerald-50 rounded-lg border border-emerald-100">
                    <span className="font-bold text-emerald-700">Target 2 (3.0x ATR)</span>
                    <span className="font-bold text-emerald-800">₹{inspectedStock.tp2?.toFixed(2)}</span>
                  </div>
                </div>
              </div>

              {/* Technical Indicator Breakdown */}
              <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-2 text-xs">
                <span className="text-xs font-bold uppercase tracking-wider text-slate-500 block mb-2">
                  Technical Indicator Anatomy
                </span>
                <div className="grid grid-cols-2 gap-2 text-[11px]">
                  <div className="flex justify-between p-2 bg-slate-50 rounded border border-slate-100">
                    <span className="text-slate-500">20 EMA</span>
                    <span className="font-bold font-mono">₹{inspectedStock.ema20?.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between p-2 bg-slate-50 rounded border border-slate-100">
                    <span className="text-slate-500">50 EMA</span>
                    <span className="font-bold font-mono">₹{inspectedStock.ema50?.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between p-2 bg-slate-50 rounded border border-slate-100">
                    <span className="text-slate-500">200 EMA</span>
                    <span className="font-bold font-mono">₹{inspectedStock.ema200?.toFixed(2)}</span>
                  </div>
                  <div className="flex justify-between p-2 bg-slate-50 rounded border border-slate-100">
                    <span className="text-slate-500">RSI (14)</span>
                    <span className="font-bold font-mono">{inspectedStock.rsi?.toFixed(1)}</span>
                  </div>
                </div>
              </div>
            </div>

            {/* Non-Direct Review Workflows (Invariant 4) */}
            <div className="pt-6 border-t border-slate-100 space-y-2 mt-6">
              <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider block mb-1">
                Deep Analysis & Review Workflows
              </span>
              <button
                onClick={() => navigate('/intraday-ml')}
                className="w-full py-2.5 px-4 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-bold flex items-center justify-center gap-2 shadow-xs transition"
              >
                Send to Intraday ML Scanner <ExternalLink size={14} />
              </button>
              <button
                onClick={() => navigate('/swing-ml')}
                className="w-full py-2.5 px-4 bg-slate-900 hover:bg-slate-800 text-white rounded-xl text-xs font-bold flex items-center justify-center gap-2 shadow-xs transition"
              >
                Send to Swing Trade Scanner <ExternalLink size={14} />
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
