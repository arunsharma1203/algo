import React, { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { 
  Zap, 
  Target, 
  Terminal, 
  Play, 
  Loader, 
  TrendingUp, 
  TrendingDown, 
  AlertTriangle, 
  CheckCircle, 
  Shield, 
  Clock, 
  Sliders, 
  BarChart2, 
  RotateCcw,
  Sparkles,
  RefreshCw
} from 'lucide-react';
import AITradeHistory from '../components/AITradeHistory';
import MLBacktestModal from '../components/MLBacktestModal';
import ExecutionModal from '../components/ExecutionModal';
import FNOAnalyticsCard from '../components/FNOAnalyticsCard';
import { API_BASE } from '../services/api';

const UNIVERSE_OPTIONS = [
  { id: 'NIFTY_500', label: 'NIFTY 500 (Broad Market)' },
  { id: 'NIFTY_50', label: 'NIFTY 50 (Blue-Chips)' },
  { id: 'BANK_NIFTY', label: 'Bank NIFTY (Banking)' },
  { id: 'NIFTY_IT', label: 'NIFTY IT (Technology)' },
  { id: 'LIVE_52', label: 'Live Priority 52' },
  { id: 'WATCHLIST', label: 'My Watchlist' },
  { id: 'BENCHMARK_5', label: 'Benchmark 5 (Fast Smoke Test)' },
  { id: 'CUSTOM', label: 'Custom Symbol Selection' }
];

export default function SmartScanner({ defaultTimeframe }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlTf = searchParams.get('tf');
  
  // ── 1. ACTIVE TIMEFRAME STATE (WITH LOCALSTORAGE RESTORATION) ──
  const [timeframe, setTimeframe] = useState(() => {
    if (defaultTimeframe) return defaultTimeframe;
    if (urlTf === 'intraday' || urlTf === 'swing') return urlTf;
    const savedTf = localStorage.getItem('smart_scanner_last_tf');
    return savedTf === 'swing' ? 'swing' : 'intraday';
  });

  // ── 2. CONTROLS STATE (PERSISTED PER TIMEFRAME) ─────────────────
  const [universe, setUniverse] = useState(() => {
    return localStorage.getItem(`smart_scanner_${timeframe}_universe`) || 'NIFTY_500';
  });
  const [customTickers, setCustomTickers] = useState(() => {
    return localStorage.getItem(`smart_scanner_${timeframe}_custom_tickers`) || '';
  });
  const [minConfidence, setMinConfidence] = useState(() => {
    const saved = localStorage.getItem(`smart_scanner_${timeframe}_min_conf`);
    return saved ? Number(saved) : 60;
  });

  // ── 3. SCAN PROGRESS & TELEMETRY (PERSISTED PER TIMEFRAME) ──────
  const [scanning, setScanning] = useState(false);
  const [progress, setProgress] = useState(() => {
    const saved = localStorage.getItem(`smart_scanner_${timeframe}_progress`);
    return saved ? Number(saved) : 0;
  });
  const [logs, setLogs] = useState(() => {
    const saved = localStorage.getItem(`smart_scanner_${timeframe}_logs`);
    try {
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [terminalOpen, setTerminalOpen] = useState(() => {
    const saved = localStorage.getItem(`smart_scanner_${timeframe}_terminal_open`);
    return saved === 'true';
  });

  // ── 4. CANDIDATES & SUMMARY (PERSISTED PER TIMEFRAME) ───────────
  const [result, setResult] = useState(() => {
    const saved = localStorage.getItem(`smart_scanner_${timeframe}_result`);
    try {
      return saved ? JSON.parse(saved) : null;
    } catch {
      return null;
    }
  });
  const [allCandidates, setAllCandidates] = useState(() => {
    const saved = localStorage.getItem(`smart_scanner_${timeframe}_candidates`);
    try {
      return saved ? JSON.parse(saved) : [];
    } catch {
      return [];
    }
  });
  const [scanSummary, setScanSummary] = useState(() => {
    const saved = localStorage.getItem(`smart_scanner_${timeframe}_summary`);
    try {
      return saved ? JSON.parse(saved) : null;
    } catch {
      return null;
    }
  });

  // ── 5. MODAL & INTERACTION STATES ──────────────────────────────
  const [execModalOpen, setExecModalOpen] = useState(false);
  const [backtestModalOpen, setBacktestModalOpen] = useState(false);
  const [execTradeData, setExecTradeData] = useState(null);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  const terminalContainerRef = useRef(null);

  // Sync timeframe changes with URL query parameter
  useEffect(() => {
    if (urlTf && (urlTf === 'intraday' || urlTf === 'swing') && urlTf !== timeframe) {
      handleTimeframeChange(urlTf);
    }
  }, [urlTf]);

  // Persist states into localStorage whenever they update
  useEffect(() => {
    try {
      localStorage.setItem('smart_scanner_last_tf', timeframe);
      localStorage.setItem(`smart_scanner_${timeframe}_universe`, universe);
      localStorage.setItem(`smart_scanner_${timeframe}_custom_tickers`, customTickers);
      localStorage.setItem(`smart_scanner_${timeframe}_min_conf`, minConfidence.toString());
      localStorage.setItem(`smart_scanner_${timeframe}_progress`, progress.toString());
      localStorage.setItem(`smart_scanner_${timeframe}_terminal_open`, terminalOpen.toString());
      localStorage.setItem(`smart_scanner_${timeframe}_logs`, JSON.stringify(logs.slice(-100)));
      if (result) {
        localStorage.setItem(`smart_scanner_${timeframe}_result`, JSON.stringify(result));
      } else {
        localStorage.removeItem(`smart_scanner_${timeframe}_result`);
      }
      localStorage.setItem(`smart_scanner_${timeframe}_candidates`, JSON.stringify(allCandidates));
      if (scanSummary) {
        localStorage.setItem(`smart_scanner_${timeframe}_summary`, JSON.stringify(scanSummary));
      } else {
        localStorage.removeItem(`smart_scanner_${timeframe}_summary`);
      }
    } catch (e) {
      console.warn("Error persisting scanner state to localStorage:", e);
    }
  }, [timeframe, universe, customTickers, minConfidence, progress, terminalOpen, logs, result, allCandidates, scanSummary]);

  // Switch timeframe and restore that timeframe's stored results
  const handleTimeframeChange = (newTf) => {
    setTimeframe(newTf);
    setSearchParams({ tf: newTf });
    localStorage.setItem('smart_scanner_last_tf', newTf);

    try {
      const savedResult = localStorage.getItem(`smart_scanner_${newTf}_result`);
      setResult(savedResult ? JSON.parse(savedResult) : null);

      const savedCandidates = localStorage.getItem(`smart_scanner_${newTf}_candidates`);
      setAllCandidates(savedCandidates ? JSON.parse(savedCandidates) : []);

      const savedLogs = localStorage.getItem(`smart_scanner_${newTf}_logs`);
      setLogs(savedLogs ? JSON.parse(savedLogs) : []);

      const savedProgress = localStorage.getItem(`smart_scanner_${newTf}_progress`);
      setProgress(savedProgress ? Number(savedProgress) : 0);

      const savedUniverse = localStorage.getItem(`smart_scanner_${newTf}_universe`);
      if (savedUniverse) setUniverse(savedUniverse);

      const savedConf = localStorage.getItem(`smart_scanner_${newTf}_min_conf`);
      if (savedConf) setMinConfidence(Number(savedConf));

      const savedCustom = localStorage.getItem(`smart_scanner_${newTf}_custom_tickers`);
      if (savedCustom) setCustomTickers(savedCustom);

      const savedSummary = localStorage.getItem(`smart_scanner_${newTf}_summary`);
      setScanSummary(savedSummary ? JSON.parse(savedSummary) : null);
    } catch (e) {
      console.warn("Failed loading persisted timeframe state:", e);
    }
  };

  // Clear current timeframe's persisted state
  const handleClearHistory = () => {
    localStorage.removeItem(`smart_scanner_${timeframe}_result`);
    localStorage.removeItem(`smart_scanner_${timeframe}_candidates`);
    localStorage.removeItem(`smart_scanner_${timeframe}_logs`);
    localStorage.removeItem(`smart_scanner_${timeframe}_progress`);
    localStorage.removeItem(`smart_scanner_${timeframe}_summary`);
    setResult(null);
    setAllCandidates([]);
    setLogs([]);
    setProgress(0);
    setScanSummary(null);
  };

  useEffect(() => {
    if (terminalContainerRef.current) {
      terminalContainerRef.current.scrollTop = terminalContainerRef.current.scrollHeight;
    }
  }, [logs]);

  // ── SCAN SWEEP EXECUTION ───────────────────────────────────────
  const startSweep = async () => {
    setScanning(true);
    setProgress(0);
    setResult(null);
    setAllCandidates([]);
    setScanSummary(null);
    setTerminalOpen(true);
    setLogs([{ 
      type: 'system', 
      message: `Initiating ${timeframe.toUpperCase()} Sweep (${timeframe === 'intraday' ? '15m Candles' : '1D Daily'}, Universe: ${universe}, Min Conviction: ${minConfidence}%)...` 
    }]);

    try {
      let queryUrl = `${API_BASE}/smart-scanner/sweep?timeframe=${timeframe}&universe=${universe}&min_confidence=${minConfidence}`;
      if (universe === 'CUSTOM' && customTickers.trim()) {
        queryUrl += `&custom_tickers=${encodeURIComponent(customTickers.trim())}`;
      }

      const response = await fetch(queryUrl);
      if (!response.ok) {
        throw new Error(`Server returned status ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const data = JSON.parse(line);

            if (data.progress !== undefined) {
              setProgress(data.progress);
            }

            if (data.type === 'result') {
              if (data.data) {
                setResult(data.data);
              }
              if (data.summary) {
                setScanSummary(data.summary);
                if (Array.isArray(data.summary.all_candidates)) {
                  setAllCandidates(data.summary.all_candidates);
                }
              }
              setRefreshTrigger(prev => prev + 1);
            } else if (data.type === 'candidate' && data.data) {
              setAllCandidates(prev => {
                const exists = prev.some(c => c.ticker === data.data.ticker);
                return exists ? prev : [data.data, ...prev];
              });
              setResult(prev => prev || data.data);
            }

            if (data.message) {
              setLogs(prev => [...prev, { type: data.type || 'info', message: data.message }]);
            }
          } catch (err) {
            console.error('Error parsing SSE event:', err, line);
          }
        }
      }
    } catch (err) {
      setLogs(prev => [...prev, { type: 'error', message: `Sweep failed: ${err.message}` }]);
    } finally {
      setScanning(false);
      setProgress(100);
    }
  };

  return (
    <div className="space-y-6 pb-12 max-w-7xl mx-auto px-4 sm:px-6">
      
      {/* ── HEADER & TIMEFRAME SWITCHER ────────────────────────────── */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl">
        <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
          <div>
            <div className="flex items-center space-x-3">
              <div className="p-2.5 bg-indigo-600/20 border border-indigo-500/30 rounded-xl text-indigo-400">
                <Sparkles size={24} />
              </div>
              <div>
                <h1 className="text-2xl font-black tracking-tight text-white flex items-center gap-2">
                  Smart AI Scanner
                  <span className="text-xs px-2.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-semibold">
                    v1.0 Champion Verified
                  </span>
                </h1>
                <p className="text-sm text-slate-400">
                  Dual-timeframe quantitative screening engine powered by verified machine learning models.
                </p>
              </div>
            </div>
          </div>

          {/* Timeframe Toggle Switch */}
          <div className="flex items-center bg-slate-950 p-1.5 rounded-xl border border-slate-800 shadow-inner">
            <button
              onClick={() => handleTimeframeChange('intraday')}
              className={`flex items-center space-x-2 px-4 py-2 rounded-lg text-xs font-bold transition-all ${
                timeframe === 'intraday'
                  ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/30'
                  : 'text-slate-400 hover:text-white'
              }`}
            >
              <Zap size={14} />
              <span>15M INTRADAY</span>
            </button>
            <button
              onClick={() => handleTimeframeChange('swing')}
              className={`flex items-center space-x-2 px-4 py-2 rounded-lg text-xs font-bold transition-all ${
                timeframe === 'swing'
                  ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/30'
                  : 'text-slate-400 hover:text-white'
              }`}
            >
              <Clock size={14} />
              <span>1D SWING</span>
            </button>
          </div>
        </div>

        {/* ── CONTROLS TOOLBAR ─────────────────────────────────────── */}
        <div className="mt-5 pt-5 border-t border-slate-800/80 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 items-end">
          
          {/* Universe Selector */}
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1.5">
              Target Universe
            </label>
            <select
              value={universe}
              onChange={(e) => setUniverse(e.target.value)}
              disabled={scanning}
              className="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-3 py-2 text-sm text-slate-200 font-medium focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 transition"
            >
              {UNIVERSE_OPTIONS.map(opt => (
                <option key={opt.id} value={opt.id}>{opt.label}</option>
              ))}
            </select>
          </div>

          {/* Min Conviction Filter */}
          <div>
            <label className="block text-xs font-semibold text-slate-400 mb-1.5 flex justify-between">
              <span>Min Conviction</span>
              <span className="text-indigo-400 font-bold">{minConfidence}%</span>
            </label>
            <input
              type="range"
              min="40"
              max="80"
              step="5"
              value={minConfidence}
              onChange={(e) => setMinConfidence(Number(e.target.value))}
              disabled={scanning}
              className="w-full accent-indigo-500 bg-slate-950 h-2 rounded-lg cursor-pointer"
            />
          </div>

          {/* Custom Ticker Input (if CUSTOM selected) */}
          {universe === 'CUSTOM' ? (
            <div>
              <label className="block text-xs font-semibold text-slate-400 mb-1.5">
                Custom Symbols
              </label>
              <input
                type="text"
                placeholder="RELIANCE, TCS, INFY"
                value={customTickers}
                onChange={(e) => setCustomTickers(e.target.value)}
                disabled={scanning}
                className="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-3 py-2 text-sm text-slate-200 font-medium focus:ring-2 focus:ring-indigo-500"
              />
            </div>
          ) : (
            <div className="hidden lg:block text-xs text-slate-500 leading-relaxed">
              <span>Time Horizon: </span>
              <strong className="text-slate-300">
                {timeframe === 'intraday' ? 'Same-day cash exit' : '3-10 days positional hold'}
              </strong>
              <br />
              <span>Capital Risk: </span>
              <strong className="text-emerald-400 font-semibold">0.0% Portfolio Heat</strong>
            </div>
          )}

          {/* Single Primary Action Button */}
          <div>
            <button
              onClick={startSweep}
              disabled={scanning}
              className={`w-full py-2.5 px-4 rounded-xl font-bold text-sm flex items-center justify-center space-x-2 transition-all shadow-lg ${
                scanning
                  ? 'bg-indigo-700/50 text-indigo-300 cursor-not-allowed border border-indigo-500/30'
                  : 'bg-gradient-to-r from-indigo-600 to-indigo-500 hover:from-indigo-500 hover:to-indigo-600 text-white shadow-indigo-600/20 active:scale-[0.98]'
              }`}
            >
              {scanning ? (
                <>
                  <Loader size={16} className="animate-spin text-white" />
                  <span>Screening Market ({progress}%)...</span>
                </>
              ) : (
                <>
                  <Zap size={16} className="text-yellow-300 fill-yellow-300" />
                  <span>RUN AI SWEEP</span>
                </>
              )}
            </button>
          </div>

        </div>

        {/* Progress Bar (Visible while scanning) */}
        {scanning && (
          <div className="mt-4 pt-4 border-t border-slate-800/60">
            <div className="flex justify-between items-center text-xs text-slate-400 mb-1">
              <span className="font-medium">AI Screening Progress</span>
              <span className="font-mono text-indigo-400 font-bold">{progress}%</span>
            </div>
            <div className="w-full bg-slate-950 rounded-full h-2 overflow-hidden border border-slate-800">
              <div
                className="bg-gradient-to-r from-indigo-500 to-emerald-400 h-2 rounded-full transition-all duration-300"
                style={{ width: `${progress}%` }}
              ></div>
            </div>
          </div>
        )}
      </div>

      {/* ── LAST SCAN SUMMARY STATUS BAR (PERSISTED) ───────────────── */}
      {scanSummary && !scanning && (
        <div className="bg-slate-900/80 border border-slate-800 rounded-xl px-4 py-3 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 text-xs">
          <div className="flex items-center space-x-2 text-slate-300">
            <CheckCircle size={16} className="text-emerald-400 shrink-0" />
            <span>
              Last Scan Completed: <strong className="text-white">{scanSummary.total_scanned || allCandidates.length} symbols</strong> evaluated across <strong className="text-indigo-300">{scanSummary.universe || universe}</strong> ({timeframe.toUpperCase()}).
            </span>
            <span className="text-slate-500 font-mono">
              ({scanSummary.completed_at ? new Date(scanSummary.completed_at).toLocaleTimeString() : 'Persisted'})
            </span>
          </div>

          <div className="flex items-center space-x-3 w-full sm:w-auto justify-end">
            <span className="text-emerald-400 font-bold">
              {allCandidates.length} Qualified Setup{allCandidates.length === 1 ? '' : 's'}
            </span>
            <button
              onClick={handleClearHistory}
              title="Clear stored results for this timeframe"
              className="text-slate-400 hover:text-rose-400 transition flex items-center space-x-1 cursor-pointer"
            >
              <RotateCcw size={12} />
              <span>Reset</span>
            </button>
          </div>
        </div>
      )}

      {/* ── LIVE TELEMETRY TERMINAL (COLLAPSIBLE) ───────────────────── */}
      {logs.length > 0 && (
        <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden shadow-md">
          <div 
            onClick={() => setTerminalOpen(!terminalOpen)}
            className="px-4 py-2.5 bg-slate-900/80 border-b border-slate-800 flex items-center justify-between cursor-pointer hover:bg-slate-900 transition"
          >
            <div className="flex items-center space-x-2 text-xs font-semibold text-slate-300">
              <Terminal size={14} className="text-indigo-400" />
              <span>Live Sweep Telemetry</span>
              <span className="text-[10px] text-slate-500 font-mono">({logs.length} events)</span>
            </div>
            <span className="text-xs text-indigo-400 font-medium">
              {terminalOpen ? 'Hide Terminal ▲' : 'Show Terminal ▼'}
            </span>
          </div>

          {terminalOpen && (
            <div 
              ref={terminalContainerRef}
              className="p-3 max-h-52 overflow-y-auto font-mono text-xs space-y-1 bg-black/40 text-slate-300"
            >
              {logs.map((log, index) => (
                <div 
                  key={index}
                  className={`leading-relaxed ${
                    log.type === 'error' ? 'text-rose-400' :
                    log.type === 'candidate' ? 'text-emerald-400 font-bold' :
                    log.type === 'system' ? 'text-indigo-300' : 'text-slate-400'
                  }`}
                >
                  <span className="opacity-40 select-none mr-2">&gt;</span>
                  {log.message}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── EMPTY QUALIFICATION STATE (IF SWEEP FINISHED WITH 0 SETUPS) ── */}
      {!scanning && progress === 100 && allCandidates.length === 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 text-center space-y-3 shadow-xl">
          <div className="w-12 h-12 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-indigo-400 flex items-center justify-center mx-auto">
            <CheckCircle size={24} />
          </div>
          <h3 className="text-lg font-bold text-white">
            AI Market Sweep Complete ({scanSummary?.total_scanned || 'All'} Symbols Evaluated)
          </h3>
          <p className="text-sm text-slate-400 max-w-xl mx-auto">
            No instruments met the strict <strong className="text-indigo-400 font-bold">{minConfidence}%</strong> conviction threshold under the current macro regime. All candidate signals were safely filtered out by the 8-stage qualification engine.
          </p>
          <div className="flex flex-wrap items-center justify-center gap-3 pt-2">
            <button
              onClick={() => {
                setMinConfidence(50);
                startSweep();
              }}
              className="px-3 py-1.5 bg-indigo-600/30 hover:bg-indigo-600/50 text-indigo-300 border border-indigo-500/40 rounded-xl text-xs font-semibold transition cursor-pointer"
            >
              Lower Threshold to 50% &amp; Rescan
            </button>
            <button
              onClick={() => {
                setUniverse('LIVE_52');
                startSweep();
              }}
              className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 rounded-xl text-xs font-semibold transition cursor-pointer"
            >
              Try Live Priority 52 Universe
            </button>
          </div>
        </div>
      )}

      {/* ── TOP QUALIFIED CANDIDATE CARD ──────────────────────────── */}
      {result && (
        <div className="bg-gradient-to-br from-slate-900 to-slate-950 border-2 border-indigo-500/40 rounded-2xl p-6 shadow-2xl relative overflow-hidden">
          <div className="absolute -top-10 -right-10 w-40 h-40 bg-indigo-500/10 rounded-full blur-3xl pointer-events-none"></div>

          <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 mb-6">
            <div className="flex items-center space-x-4">
              <div className={`p-3 rounded-xl border ${
                result.is_bullish 
                  ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400' 
                  : 'bg-rose-500/10 border-rose-500/30 text-rose-400'
              }`}>
                {result.is_bullish ? <TrendingUp size={28} /> : <TrendingDown size={28} />}
              </div>
              <div>
                <div className="flex items-center space-x-2">
                  <span className="text-2xl font-black text-white tracking-tight">{result.ticker}</span>
                  <span className={`px-2.5 py-0.5 rounded-full text-xs font-black tracking-wider uppercase border ${
                    result.is_bullish 
                      ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' 
                      : 'bg-rose-500/20 text-rose-300 border-rose-500/30'
                  }`}>
                    {result.direction}
                  </span>
                  <span className={`text-xs px-2.5 py-0.5 rounded-md font-black border ${
                    result.trade_type === 'SWING' 
                      ? 'bg-indigo-900/60 text-indigo-300 border-indigo-700' 
                      : 'bg-sky-900/60 text-sky-300 border-sky-700'
                  }`}>
                    {result.trade_type === 'SWING' ? '🎯 SWING (1D)' : '⚡ INTRADAY (15M)'}
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  Calibrated AI Confidence: <strong className="text-indigo-400 font-bold">{result.confidence}%</strong> (Raw: {result.raw_confidence}%)
                </p>
              </div>
            </div>

            {/* Quick Action Buttons */}
            <div className="flex items-center space-x-3 w-full md:w-auto">
              <button
                onClick={() => {
                  setExecTradeData(result);
                  setExecModalOpen(true);
                }}
                className="flex-1 md:flex-none px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded-xl text-xs font-bold shadow-lg shadow-emerald-600/20 transition flex items-center justify-center space-x-1.5 cursor-pointer"
              >
                <Shield size={14} />
                <span>EXECUTE / PAPER TRADE</span>
              </button>

              <button
                onClick={() => {
                  setExecTradeData(result);
                  setBacktestModalOpen(true);
                }}
                className="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-xl text-xs font-semibold border border-slate-700 transition flex items-center space-x-1 cursor-pointer"
              >
                <BarChart2 size={14} />
                <span>Backtest</span>
              </button>
            </div>
          </div>

          {/* Metric Grid */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
            <div className="bg-slate-950/80 border border-slate-800 rounded-xl p-3">
              <span className="text-[11px] font-semibold text-slate-400 block mb-1">Recommended Entry</span>
              <span className="text-lg font-bold text-white">₹{result.entry}</span>
            </div>
            <div className="bg-slate-950/80 border border-slate-800 rounded-xl p-3">
              <span className="text-[11px] font-semibold text-rose-400 block mb-1">Stop Loss</span>
              <span className="text-lg font-bold text-rose-300">₹{result.sl}</span>
            </div>
            <div className="bg-slate-950/80 border border-slate-800 rounded-xl p-3">
              <span className="text-[11px] font-semibold text-emerald-400 block mb-1">Target 1 (Primary)</span>
              <span className="text-lg font-bold text-emerald-300">₹{result.tp1}</span>
            </div>
            <div className="bg-slate-950/80 border border-slate-800 rounded-xl p-3">
              <span className="text-[11px] font-semibold text-indigo-400 block mb-1">Target 2 (Extended)</span>
              <span className="text-lg font-bold text-indigo-300">₹{result.tp2}</span>
            </div>
          </div>

          {/* Model Consensus Bar */}
          {result.base_probs && (
            <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-3 flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 text-xs">
              <span className="font-semibold text-slate-400">Hunter Ensemble Consensus:</span>
              <div className="flex items-center space-x-4">
                <span className="text-slate-300">
                  Random Forest: <strong className="text-indigo-400">{result.base_probs.rf}%</strong>
                </span>
                <span className="text-slate-300">
                  Gradient Boosting: <strong className="text-indigo-400">{result.base_probs.gb}%</strong>
                </span>
                <span className="text-slate-300">
                  SVM: <strong className="text-indigo-400">{result.base_probs.svc}%</strong>
                </span>
              </div>
            </div>
          )}

          {/* FNO Context if applicable */}
          <div className="mt-4">
            <FNOAnalyticsCard ticker={result.ticker} />
          </div>
        </div>
      )}

      {/* ── ALL QUALIFIED SETUPS CAROUSEL/LIST ─────────────────────── */}
      {allCandidates.length > 1 && (
        <div className="space-y-3">
          <h2 className="text-base font-bold text-white flex items-center gap-2">
            <span>Additional Qualified Setups</span>
            <span className="text-xs px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 font-mono">
              {allCandidates.length} Found
            </span>
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {allCandidates.slice(1).map((item, idx) => (
              <div 
                key={idx}
                onClick={() => setResult(item)}
                className="bg-slate-900/90 border border-slate-800 hover:border-indigo-500/40 rounded-xl p-4 cursor-pointer transition shadow-md hover:shadow-lg"
              >
                <div className="flex justify-between items-center mb-2">
                  <div className="flex items-center space-x-2">
                    <span className="font-bold text-white text-base">{item.ticker}</span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-black ${
                      item.trade_type === 'SWING' ? 'bg-indigo-900/60 text-indigo-300' : 'bg-sky-900/60 text-sky-300'
                    }`}>
                      {item.trade_type === 'SWING' ? '🎯 1D' : '⚡ 15M'}
                    </span>
                  </div>
                  <span className={`text-xs px-2 py-0.5 rounded font-black ${
                    item.is_bullish ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'
                  }`}>
                    {item.direction}
                  </span>
                </div>
                <div className="flex justify-between text-xs text-slate-400 mt-2">
                  <span>Entry: <strong className="text-slate-200">₹{item.entry}</strong></span>
                  <span>Target: <strong className="text-emerald-300">₹{item.tp1}</strong></span>
                  <span>SL: <strong className="text-rose-300">₹{item.sl}</strong></span>
                </div>
                <div className="mt-3 pt-2 border-t border-slate-800 flex justify-between items-center text-[11px]">
                  <span className="text-indigo-400 font-semibold">Conviction: {item.confidence}%</span>
                  <span className="text-slate-500 font-medium">Inspect Card &rarr;</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── INTEGRATED TRADE HISTORY DRAWER ────────────────────────── */}
      <div className="mt-8">
        <AITradeHistory 
          refreshTrigger={refreshTrigger}
          defaultFilter="ALL"
        />
      </div>

      {/* ── MODALS ─────────────────────────────────────────────────── */}
      {execModalOpen && (
        <ExecutionModal
          trade={execTradeData}
          isOpen={execModalOpen}
          onClose={() => setExecModalOpen(false)}
          onSuccess={() => setRefreshTrigger(prev => prev + 1)}
        />
      )}

      {backtestModalOpen && (
        <MLBacktestModal
          ticker={execTradeData?.ticker}
          isOpen={backtestModalOpen}
          onClose={() => setBacktestModalOpen(false)}
        />
      )}

    </div>
  );
}
