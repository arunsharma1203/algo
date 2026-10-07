import React, { useState, useEffect, useRef } from 'react';
import { 
  ShieldAlert, 
  CheckCircle2, 
  AlertTriangle, 
  TrendingUp, 
  TrendingDown, 
  Cpu, 
  Zap, 
  RefreshCw, 
  Layers, 
  Crosshair, 
  ArrowRight,
  Clock,
  Sparkles,
  Info,
  BarChart2,
  LineChart as LineChartIcon,
  Award,
  Calendar,
  ChevronDown,
  ChevronUp,
  Sliders,
  Activity,
  ShieldCheck,
  Filter,
  Square,
  Trophy,
  Gamepad2,
  Newspaper,
  Compass,
  HelpCircle,
  Check
} from 'lucide-react';
import { 
  ResponsiveContainer, 
  LineChart, 
  Line, 
  XAxis, 
  YAxis, 
  CartesianGrid, 
  Tooltip, 
  Legend 
} from 'recharts';
import { API_BASE } from '../services/api';
import ConsensusGameArena from '../components/ConsensusGameArena';

export default function ConsensusScanner() {
  // Navigation
  const [mainTab, setMainTab] = useState(() => {
    if (typeof window !== 'undefined') {
      if (window.location.hash.includes('arena') || window.location.pathname.includes('arena') || window.location.pathname.includes('game')) {
        return 'arena';
      }
    }
    return 'scanner';
  }); // 'scanner' | 'comparison' | 'arena'

  // Scanner State with LocalStorage Persistence
  const [strategy, setStrategy] = useState(() => localStorage.getItem('consensus_strategy') || 'INTRADAY');
  const [universe, setUniverse] = useState(() => localStorage.getItem('consensus_universe') || 'ALL_DATABASE_STOCKS');
  const [topK, setTopK] = useState(() => localStorage.getItem('consensus_topK') || '5');
  const [loading, setLoading] = useState(false);
  const [activeScan, setActiveScan] = useState(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [scanResult, setScanResult] = useState(() => {
    try {
      const saved = localStorage.getItem('consensus_scanResult');
      return saved ? JSON.parse(saved) : null;
    } catch {
      return null;
    }
  });
  const [engineStatus, setEngineStatus] = useState(null);
  const [error, setError] = useState(null);
  const [activeTab, setActiveTab] = useState('unanimous');
  const [expandedCards, setExpandedCards] = useState({});

  // Comparison State
  const [comparisonData, setComparisonData] = useState(null);
  const [historyData, setHistoryData] = useState(null);
  const [loadingComparison, setLoadingComparison] = useState(false);
  const [comparisonDays, setComparisonDays] = useState(30);
  const [comparisonFilterMode, setComparisonFilterMode] = useState(() => {
    return localStorage.getItem('consensus_comparison_filter_mode') || 'ALL_QUALIFIED';
  });

  // Tracked Consensus Trades State (SSOT from consensus_trade_history)
  const [trackedTrades, setTrackedTrades] = useState([]);
  const [loadingTrackedTrades, setLoadingTrackedTrades] = useState(false);
  const [trackedFilter, setTrackedFilter] = useState('ALL'); // 'ALL' | 'OPEN' | 'CLOSED'

  // Daily Online Learning State
  const [consensusWeights, setConsensusWeights] = useState(null);

  // Keep strategy ref current to avoid stale interval closures
  const strategyRef = useRef(strategy);
  useEffect(() => {
    strategyRef.current = strategy;
  }, [strategy]);

  // Toggle card expansion
  const toggleCardExpansion = (cardId) => {
    setExpandedCards(prev => ({
      ...prev,
      [cardId]: !prev[cardId]
    }));
  };

  // Fetch status, active scan, tracked trades, weights, and latest result on mount
  useEffect(() => {
    fetchEngineStatus();
    fetchConsensusWeights(strategy);
    fetchComparisonData(comparisonDays, comparisonFilterMode);
    checkActiveAndLatest(strategy);
    fetchTrackedTrades();

    // Auto-refresh tracked trades, latest scan results & weights every 15s to keep live LTPs & background scans fresh
    const trackedInterval = setInterval(() => {
      fetchTrackedTrades();
      fetchConsensusWeights(strategyRef.current);
      fetchLatestScan(strategyRef.current);
    }, 15000);

    return () => clearInterval(trackedInterval);
  }, []);

  // Polling for active background scan
  useEffect(() => {
    let interval = null;
    if (loading) {
      interval = setInterval(async () => {
        setElapsedSeconds(prev => prev + 1);
        try {
          const res = await fetch(`${API_BASE}/consensus/active`);
          if (res.ok) {
            const data = await res.json();
            if (data.active) {
              setActiveScan(data);
              if (data.elapsed_seconds) {
                setElapsedSeconds(Math.round(data.elapsed_seconds));
              }
            } else {
              // Active scan finished! Fetch latest authoritative result & tracked trades
              clearInterval(interval);
              await fetchLatestScan(strategy);
              await fetchTrackedTrades();
              setLoading(false);
              setActiveScan(null);
            }
          }
        } catch (e) {
          console.warn('Active scan polling error', e);
        }
      }, 1500);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [loading, strategy]);

  const checkActiveAndLatest = async (strat) => {
    try {
      // 1. Check if a scan is currently active on backend
      const actRes = await fetch(`${API_BASE}/consensus/active`);
      if (actRes.ok) {
        const actData = await actRes.json();
        if (actData.active) {
          setActiveScan(actData);
          setLoading(true);
          if (actData.elapsed_seconds) {
            setElapsedSeconds(Math.round(actData.elapsed_seconds));
          }
          return;
        }
      }

      // 2. If idle, fetch latest completed scan
      await fetchLatestScan(strat);
    } catch (err) {
      console.warn('Could not initialize consensus state', err);
    }
  };

  const fetchLatestScan = async (strat) => {
    try {
      const latRes = await fetch(`${API_BASE}/consensus/latest?strategy=${strat}`);
      if (latRes.ok) {
        const latData = await latRes.json();
        if (latData.has_latest && latData.scan) {
          setScanResult(latData.scan);
          localStorage.setItem('consensus_scanResult', JSON.stringify(latData.scan));
        }
      }
    } catch (err) {
      console.warn('Could not fetch latest consensus scan', err);
    }
  };

  const fetchEngineStatus = async () => {
    try {
      const res = await fetch(`${API_BASE}/consensus/status`);
      if (res.ok) {
        const data = await res.json();
        setEngineStatus(data);
      }
    } catch (err) {
      console.warn('Could not fetch consensus status', err);
    }
  };

  const fetchConsensusWeights = async (strat = strategy) => {
    try {
      const res = await fetch(`${API_BASE}/consensus/weights?strategy=${strat}`);
      if (res.ok) {
        const data = await res.json();
        setConsensusWeights(data);
      }
    } catch (err) {
      console.warn('Could not fetch consensus weights', err);
    }
  };

  const fetchTrackedTrades = async () => {
    setLoadingTrackedTrades(true);
    try {
      const res = await fetch(`${API_BASE}/consensus/tracked-trades?limit=100`);
      if (res.ok) {
        const data = await res.json();
        setTrackedTrades(Array.isArray(data) ? data : (data.trades || []));
      }
    } catch (err) {
      console.warn('Could not fetch tracked consensus trades', err);
    } finally {
      setLoadingTrackedTrades(false);
    }
  };

  const fetchComparisonData = async (days = comparisonDays, filterMode = comparisonFilterMode) => {
    setLoadingComparison(true);
    try {
      const [compRes, histRes] = await Promise.all([
        fetch(`${API_BASE}/consensus/comparison?days=${days}&filter_mode=${filterMode}`),
        fetch(`${API_BASE}/consensus/history?days=${days}&filter_mode=${filterMode}`)
      ]);

      if (compRes.ok) {
        const cData = await compRes.json();
        setComparisonData(cData);
      }
      if (histRes.ok) {
        const hData = await histRes.json();
        setHistoryData(hData);
      }
    } catch (err) {
      console.warn('Could not fetch multi-engine comparison', err);
    } finally {
      setLoadingComparison(false);
    }
  };

  const handleSetComparisonFilterMode = (newMode) => {
    setComparisonFilterMode(newMode);
    localStorage.setItem('consensus_comparison_filter_mode', newMode);
    fetchComparisonData(comparisonDays, newMode);
  };

  const handleRunScan = async () => {
    setLoading(true);
    setElapsedSeconds(0);
    setError(null);
    localStorage.setItem('consensus_strategy', strategy);
    localStorage.setItem('consensus_universe', universe);
    localStorage.setItem('consensus_topK', topK.toString());

    try {
      const res = await fetch(`${API_BASE}/consensus/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy,
          universe,
          top_k: parseInt(topK, 10)
        })
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail || 'Consensus scan failed');
      }

      const data = await res.json();
      if (data.status === 'ALREADY_RUNNING') {
        setActiveScan(data.active_scan);
        return; // Polling loop will capture completion
      }

      setScanResult(data);
      localStorage.setItem('consensus_scanResult', JSON.stringify(data));
      setLoading(false);
      setActiveScan(null);

      // Immediately integrate newly discovered unanimous trades into Persisted Trades UI with 0ms delay
      if (Array.isArray(data.unanimous_trades) && data.unanimous_trades.length > 0) {
        setTrackedTrades(prev => {
          const prevMap = new Map((prev || []).map(t => [t.ticker || t.symbol, t]));
          const nowIso = new Date().toISOString().replace('T', ' ').substring(0, 19);
          const newEntries = data.unanimous_trades.map((u, idx) => ({
            id: u.id || `live_${Date.now()}_${idx}`,
            timestamp: u.timestamp || nowIso,
            ticker: u.ticker || `${u.symbol}.NS`,
            symbol: u.symbol || (u.ticker ? u.ticker.replace('.NS', '') : ''),
            strategy: u.strategy || strategy,
            direction: u.direction || 'BUY',
            entry_price: u.blended_entry || u.entry || 0,
            stop_loss: u.blended_sl || u.stop_loss || 0,
            target_price: u.blended_tp || u.target_price || 0,
            composite_score: u.composite_score || 50,
            risk_reward_ratio: u.risk_reward_ratio || 1.5,
            status: 'OPEN',
            outcome: 'OPEN',
            profit_pct: 0.0,
            source: 'MANUAL',
            position_type: 'NOT_A_POSITION',
            details_json: JSON.stringify(u),
            current_price: u.blended_entry || u.entry || 0,
            timeframe: (u.strategy || strategy) === 'INTRADAY' ? '15m' : '1d'
          }));
          const brandNew = newEntries.filter(n => !prevMap.has(n.ticker));
          return [...brandNew, ...(prev || [])];
        });
      }

      await fetchTrackedTrades();

      if (data.unanimous_trades?.length > 0) {
        setActiveTab('unanimous');
      } else if (data.qlib_solo?.length > 0) {
        setActiveTab('qlib');
      } else if (data.legacy_solo?.length > 0) {
        setActiveTab('legacy');
      } else if (data.conflicting_trades?.length > 0) {
        setActiveTab('conflicts');
      } else {
        setActiveTab('gated');
      }
    } catch (err) {
      setError(err.message || 'An unexpected error occurred during the dual scan');
      setLoading(false);
      setActiveScan(null);
    }
  };

  const handleStopScan = async () => {
    try {
      await fetch(`${API_BASE}/consensus/stop`, { method: 'POST' });
      setLoading(false);
      setActiveScan(null);
    } catch (err) {
      console.warn('Could not stop consensus scan', err);
    }
  };

  const handleStrategyChange = (newStrat) => {
    setStrategy(newStrat);
    localStorage.setItem('consensus_strategy', newStrat);
    fetchLatestScan(newStrat);
    fetchConsensusWeights(newStrat);
  };

  const formatPnL = (val) => {
    if (val === undefined || val === null) return '0.00%';
    const num = Number(val);
    const sign = num > 0 ? '+' : '';
    return `${sign}${num.toFixed(2)}%`;
  };

  return (
    <div className="flex-1 overflow-y-auto p-4 sm:p-6 lg:p-8 space-y-6 max-w-7xl mx-auto custom-scrollbar">
      
      {/* ── HEADER ────────────────────────────────────────────────────────── */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-slate-200 pb-5">
        <div>
          <div className="flex items-center space-x-2.5">
            <div className="p-2.5 bg-gradient-to-tr from-emerald-600 to-teal-500 text-white rounded-xl shadow-md">
              <Crosshair size={22} className="animate-pulse" />
            </div>
            <div>
              <h1 className="text-2xl sm:text-3xl font-black text-slate-900 tracking-tight">
                Dual-Engine Consensus Center
              </h1>
              <p className="text-xs sm:text-sm text-slate-500 font-medium mt-0.5">
                Multi-Model Confluence: Microsoft Qlib (Alpha-158 GBDT) + Legacy AI (Ensemble + FinBERT)
              </p>
            </div>
          </div>
        </div>

        {/* Engine Readiness Pills */}
        <div className="flex flex-wrap items-center gap-2">
          <div className="px-3 py-1.5 bg-purple-50 border border-purple-200 rounded-lg flex items-center space-x-2">
            <Cpu size={14} className="text-purple-600" />
            <span className="text-xs font-bold text-purple-900">Qlib Alpha-158</span>
            <span className="w-2 h-2 rounded-full bg-emerald-500"></span>
          </div>
          <div className="px-3 py-1.5 bg-indigo-50 border border-indigo-200 rounded-lg flex items-center space-x-2">
            <Zap size={14} className="text-indigo-600" />
            <span className="text-xs font-bold text-indigo-900">Legacy AI + FinBERT</span>
            <span className="w-2 h-2 rounded-full bg-emerald-500"></span>
          </div>
          <div className="px-3 py-1.5 bg-amber-50 border border-amber-200 rounded-lg flex items-center space-x-2">
            <Award size={14} className="text-amber-600" />
            <span className="text-xs font-bold text-amber-900">Confluence Daemon</span>
            <span className="w-2 h-2 rounded-full bg-emerald-500"></span>
          </div>
        </div>
      </div>

      {/* ── DAILY LEARNING & ADAPTIVE WEIGHTS BANNER ────────────────────── */}
      <div className="bg-gradient-to-r from-slate-900 via-slate-800 to-indigo-950 text-white rounded-2xl p-4 sm:p-5 shadow-lg border border-slate-700/50 flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
        <div className="flex items-center space-x-3.5">
          <div className="p-2.5 bg-indigo-500/20 rounded-xl border border-indigo-500/30 text-indigo-400 shrink-0">
            <Sliders size={22} />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <span className="text-sm font-black tracking-wide text-white">Adaptive Consensus Allocation</span>
              <span className="px-2 py-0.5 rounded-full text-[10px] font-extrabold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">
                HEDGE O(√T) ONLINE LEARNER
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Consensus weights adapt daily from post-market Brier scores. Base Qlib and Legacy models remain 100% frozen.
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2.5 w-full md:w-auto justify-start md:justify-end">
          <div className="px-3 py-1.5 bg-slate-800/80 rounded-xl border border-slate-700 flex items-center space-x-2">
            <span className="text-[11px] font-bold text-slate-400">Qlib Alpha:</span>
            <span className="text-xs font-black text-purple-400">
              {consensusWeights?.weights ? `${(consensusWeights.weights.w_qlib * 100).toFixed(1)}%` : '50.0%'}
            </span>
          </div>

          <div className="px-3 py-1.5 bg-slate-800/80 rounded-xl border border-slate-700 flex items-center space-x-2">
            <span className="text-[11px] font-bold text-slate-400">Legacy AI:</span>
            <span className="text-xs font-black text-indigo-400">
              {consensusWeights?.weights ? `${(consensusWeights.weights.w_legacy * 100).toFixed(1)}%` : '50.0%'}
            </span>
          </div>

          <div className="px-3 py-1.5 bg-slate-800/80 rounded-xl border border-slate-700 flex items-center space-x-2">
            <span className="text-[11px] font-bold text-slate-400">CUSUM Sequential Gate:</span>
            <span className={`text-xs font-black flex items-center space-x-1.5 ${
              consensusWeights?.cusum_risk_gate?.alarm_active ? 'text-amber-400' : 'text-emerald-400'
            }`}>
              <span className={`w-1.5 h-1.5 rounded-full ${
                consensusWeights?.cusum_risk_gate?.alarm_active ? 'bg-amber-400 animate-ping' : 'bg-emerald-400'
              }`}></span>
              <span>{consensusWeights?.cusum_risk_gate?.alarm_active ? 'Drift Flagged (+10% Req)' : 'Normal (No Drift)'}</span>
            </span>
          </div>
        </div>
      </div>

      {/* ── TOP VIEW NAVIGATION TABS ───────────────────────────────────────── */}
      <div className="flex items-center space-x-2 bg-slate-100 p-1.5 rounded-2xl w-fit">
        <button
          type="button"
          onClick={() => setMainTab('scanner')}
          className={`flex items-center space-x-2 px-4 py-2 rounded-xl text-xs font-black uppercase tracking-wider transition-all ${
            mainTab === 'scanner'
              ? 'bg-white text-emerald-800 shadow-sm'
              : 'text-slate-600 hover:text-slate-900'
          }`}
        >
          <Crosshair size={15} />
          <span>Live Confluence Scanner</span>
        </button>
        <button
          type="button"
          onClick={() => setMainTab('arena')}
          className={`flex items-center space-x-2 px-4 py-2 rounded-xl text-xs font-black uppercase tracking-wider transition-all ${
            mainTab === 'arena'
              ? 'bg-gradient-to-r from-amber-500 to-amber-600 text-slate-950 shadow-md font-black'
              : 'text-slate-600 hover:text-slate-900'
          }`}
        >
          <Trophy size={15} className={mainTab === 'arena' ? 'text-slate-950' : 'text-amber-600'} />
          <span>Consensus Arena 🎮</span>
          <span className="px-1.5 py-0.5 bg-amber-200 text-amber-950 rounded-full text-[10px] font-black">
            LIVE
          </span>
        </button>
        <button
          type="button"
          onClick={() => {
            setMainTab('comparison');
            fetchComparisonData(comparisonDays, comparisonFilterMode);
          }}
          className={`flex items-center space-x-2 px-4 py-2 rounded-xl text-xs font-black uppercase tracking-wider transition-all ${
            mainTab === 'comparison'
              ? 'bg-white text-indigo-900 shadow-sm'
              : 'text-slate-600 hover:text-slate-900'
          }`}
        >
          <BarChart2 size={15} />
          <span>Qlib vs Consensus vs Legacy Comparison</span>
          {comparisonData?.summary?.consensus?.total_trades > 0 && (
            <span className="px-1.5 py-0.5 bg-amber-100 text-amber-800 rounded-full text-[10px] font-bold">
              {comparisonData.summary.consensus.total_trades}
            </span>
          )}
        </button>
      </div>

      {/* ==================================================================== */}
      {/* ── VIEW 1: LIVE SCANNER ─────────────────────────────────────────── */}
      {/* ==================================================================== */}
      {mainTab === 'scanner' && (
        <div className="space-y-6">
          {/* Scan Control Panel - Sticky Persistence */}
          <div className="sticky top-2 z-20 bg-white/95 backdrop-blur-md border border-slate-200/90 rounded-2xl p-5 shadow-md">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 items-end">
              
              {/* Strategy Selector */}
              <div>
                <label className="block text-xs font-bold uppercase tracking-wider text-slate-600 mb-1.5">
                  Trading Timeframe
                </label>
                <div className="grid grid-cols-2 gap-2 bg-slate-100 p-1 rounded-xl">
                  <button
                    type="button"
                    onClick={() => handleStrategyChange('INTRADAY')}
                    className={`py-2 text-xs font-bold rounded-lg transition-all ${
                      strategy === 'INTRADAY' 
                        ? 'bg-white text-slate-900 shadow-xs' 
                        : 'text-slate-500 hover:text-slate-900'
                    }`}
                  >
                    15m Intraday
                  </button>
                  <button
                    type="button"
                    onClick={() => handleStrategyChange('SWING')}
                    className={`py-2 text-xs font-bold rounded-lg transition-all ${
                      strategy === 'SWING' 
                        ? 'bg-white text-slate-900 shadow-xs' 
                        : 'text-slate-500 hover:text-slate-900'
                    }`}
                  >
                    Daily Swing
                  </button>
                </div>
              </div>

              {/* Universe Selector */}
              <div>
                <label className="block text-xs font-bold uppercase tracking-wider text-slate-600 mb-1.5">
                  Target Universe (Synchronized 509 Equities)
                </label>
                <select
                  value={universe}
                  onChange={(e) => {
                    setUniverse(e.target.value);
                    localStorage.setItem('consensus_universe', e.target.value);
                  }}
                  className="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2 text-xs font-semibold text-slate-800 focus:outline-hidden focus:ring-2 focus:ring-emerald-500"
                >
                  <option value="ALL_DATABASE_STOCKS">All Database Equities (509 Stocks SSOT)</option>
                  <option value="NIFTY_500">NIFTY 500 Broad Universe</option>
                  <option value="NIFTY_50">NIFTY 50 Bluechip Benchmark</option>
                  <option value="LIVE_52">Top 52 Liquid F&O Benchmark</option>
                </select>
              </div>

              {/* Top K */}
              <div>
                <label className="block text-xs font-bold uppercase tracking-wider text-slate-600 mb-1.5">
                  Max Results Per Bucket
                </label>
                <select
                  value={topK}
                  onChange={(e) => {
                    setTopK(e.target.value);
                    localStorage.setItem('consensus_topK', e.target.value);
                  }}
                  className="w-full bg-slate-50 border border-slate-300 rounded-xl px-3 py-2 text-xs font-semibold text-slate-800 focus:outline-hidden focus:ring-2 focus:ring-emerald-500"
                >
                  <option value="5">Top 5 Opportunities</option>
                  <option value="10">Top 10 Opportunities</option>
                  <option value="20">Top 20 Opportunities</option>
                </select>
              </div>

              {/* Action Button & Reload State */}
              <div className="flex items-center space-x-2">
                <button
                  onClick={handleRunScan}
                  disabled={loading}
                  className={`flex-1 py-2.5 px-4 rounded-xl text-xs font-black uppercase tracking-wider text-white shadow-md flex items-center justify-center space-x-2 transition-all ${
                    loading
                      ? 'bg-slate-400 cursor-not-allowed'
                      : 'bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 active:scale-[0.99]'
                  }`}
                >
                  {loading ? (
                    <>
                      <RefreshCw size={15} className="animate-spin" />
                      <span>Scanning Both Engines...</span>
                    </>
                  ) : (
                    <>
                      <Crosshair size={15} />
                      <span>Run Dual Consensus Scan</span>
                    </>
                  )}
                </button>
                <button
                  onClick={() => fetchLatestScan(strategy)}
                  disabled={loading}
                  title="Reload Latest Scan Results"
                  className="p-2.5 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-xl transition-all"
                >
                  <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
                </button>
              </div>

            </div>
          </div>

          {/* ── REAL-TIME SCAN INTELLIGENCE TERMINAL (WHEN SCANNING) ───────────── */}
          {loading && (
            <div className="bg-slate-900 border border-slate-800 text-white rounded-2xl p-6 shadow-xl space-y-5 animate-in fade-in duration-300">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-800 pb-4">
                <div className="flex items-center space-x-3">
                  <div className="p-2.5 bg-emerald-500/20 text-emerald-400 rounded-xl border border-emerald-500/30">
                    <RefreshCw size={20} className="animate-spin text-emerald-400" />
                  </div>
                  <div>
                    <div className="flex items-center space-x-2">
                      <h2 className="text-base font-black tracking-tight text-slate-100">
                        DUAL-ENGINE CONSENSUS SWEEP IN PROGRESS
                      </h2>
                      <span className="px-2 py-0.5 bg-emerald-500/20 border border-emerald-500/40 text-emerald-300 text-[10px] font-mono font-bold rounded-full animate-pulse">
                        LIVE
                      </span>
                    </div>
                    <p className="text-xs text-slate-400 mt-0.5 font-mono">
                      {strategy} • {universe} • Evaluating up to 509 database equities concurrently
                    </p>
                  </div>
                </div>
                <div className="flex items-center space-x-3 text-xs font-mono">
                  <div className="px-3 py-1.5 bg-slate-800 rounded-xl border border-slate-700 text-emerald-400 font-bold flex items-center space-x-2">
                    <Clock size={14} />
                    <span>{elapsedSeconds}s Elapsed</span>
                  </div>
                  <div className="px-3 py-1.5 bg-slate-800 rounded-xl border border-slate-700 text-slate-300">
                    Stage: <span className="text-amber-400 font-bold">{activeScan?.stage_name || 'Executing Sweep'}</span>
                  </div>
                  <button
                    onClick={handleStopScan}
                    className="px-3 py-1.5 bg-rose-500/20 hover:bg-rose-500/30 border border-rose-500/40 rounded-xl text-rose-300 font-bold flex items-center space-x-1.5 transition-all shadow-sm active:scale-95"
                    title="Stop and cancel active consensus scan"
                  >
                    <Square size={12} className="fill-current" />
                    <span>Stop Scan</span>
                  </button>
                </div>
              </div>

              {/* Dynamic Progress Bar */}
              <div className="space-y-2">
                <div className="flex justify-between text-xs font-mono text-slate-400">
                  <span>{activeScan?.message || "Running quantitative alpha and ensemble algorithms..."}</span>
                  <span className="text-emerald-400 font-bold">{activeScan?.progress || Math.min(92, Math.max(15, elapsedSeconds * 3))}%</span>
                </div>
                <div className="w-full bg-slate-800 h-2.5 rounded-full overflow-hidden border border-slate-700">
                  <div 
                    className="bg-gradient-to-r from-emerald-500 via-teal-400 to-indigo-500 h-full rounded-full transition-all duration-500 shadow-sm"
                    style={{ width: `${activeScan?.progress || Math.min(92, Math.max(15, elapsedSeconds * 3))}%` }}
                  />
                </div>
              </div>

              {/* 4-Stage Visual Execution Pipeline */}
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 pt-1 font-mono">
                <div className={`p-3 rounded-xl border transition-all ${
                  (activeScan?.progress || 20) >= 10 ? 'bg-slate-800/80 border-emerald-500/50 text-slate-200' : 'bg-slate-800/30 border-slate-800 text-slate-500'
                }`}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] font-black uppercase text-emerald-400">Stage 1</span>
                    {(activeScan?.progress || 0) >= 35 ? <CheckCircle2 size={13} className="text-emerald-400" /> : <RefreshCw size={12} className="animate-spin text-amber-400" />}
                  </div>
                  <div className="text-xs font-bold text-slate-200">Universe Ingestion</div>
                  <p className="text-[10px] text-slate-400 mt-0.5">Resolved {activeScan?.universe_count || 509} database symbols</p>
                </div>

                <div className={`p-3 rounded-xl border transition-all ${
                  (activeScan?.progress || 20) >= 35 ? 'bg-slate-800/80 border-purple-500/50 text-slate-200' : 'bg-slate-800/30 border-slate-800 text-slate-500'
                }`}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] font-black uppercase text-purple-400">Stage 2</span>
                    {(activeScan?.progress || 0) >= 85 ? <CheckCircle2 size={13} className="text-emerald-400" /> : <RefreshCw size={12} className="animate-spin text-purple-400" />}
                  </div>
                  <div className="text-xs font-bold text-slate-200">Qlib Alpha-158 GBDT</div>
                  <p className="text-[10px] text-slate-400 mt-0.5">Alpha factors & cross-sectional ranking</p>
                </div>

                <div className={`p-3 rounded-xl border transition-all ${
                  (activeScan?.progress || 20) >= 35 ? 'bg-slate-800/80 border-indigo-500/50 text-slate-200' : 'bg-slate-800/30 border-slate-800 text-slate-500'
                }`}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] font-black uppercase text-indigo-400">Stage 3</span>
                    {(activeScan?.progress || 0) >= 85 ? <CheckCircle2 size={13} className="text-emerald-400" /> : <RefreshCw size={12} className="animate-spin text-indigo-400" />}
                  </div>
                  <div className="text-xs font-bold text-slate-200">Legacy AI Multi-Model</div>
                  <p className="text-[10px] text-slate-400 mt-0.5">RF + GBDT + SVC + FinBERT sentiment</p>
                </div>

                <div className={`p-3 rounded-xl border transition-all ${
                  (activeScan?.progress || 20) >= 85 ? 'bg-slate-800/80 border-amber-500/50 text-slate-200' : 'bg-slate-800/30 border-slate-800 text-slate-500'
                }`}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] font-black uppercase text-amber-400">Stage 4</span>
                    {(activeScan?.progress || 0) >= 100 ? <CheckCircle2 size={13} className="text-emerald-400" /> : <RefreshCw size={12} className="animate-spin text-amber-400" />}
                  </div>
                  <div className="text-xs font-bold text-slate-200">Confluence Synthesis</div>
                  <p className="text-[10px] text-slate-400 mt-0.5">Grade-A match & risk parameter blend</p>
                </div>
              </div>
            </div>
          )}

          {/* Error Banner */}
          {error && (
            <div className="bg-rose-50 border border-rose-200 rounded-2xl p-4 text-rose-800 flex items-start space-x-3 shadow-xs">
              <AlertTriangle size={18} className="text-rose-600 shrink-0 mt-0.5" />
              <div className="text-xs">
                <strong className="font-bold">Scan Execution Blocked:</strong> {error}
              </div>
            </div>
          )}

          {/* Telemetry Summary Cards */}
          {scanResult && !loading && (
            <div className="space-y-3">
              <div className="flex items-center justify-between text-xs text-slate-500 px-1 font-mono">
                <span>Completed: {scanResult.completed_at ? new Date(scanResult.completed_at).toLocaleTimeString() : 'Recent'} ({scanResult.elapsed_seconds}s)</span>
                <span>Scanned Universe: <strong>{scanResult.universe}</strong> ({scanResult.summary?.total_unique_symbols || 509} stocks evaluated)</span>
              </div>

              <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
                <div 
                  onClick={() => setActiveTab('unanimous')}
                  className={`p-4 rounded-2xl border cursor-pointer transition-all ${
                    activeTab === 'unanimous' 
                      ? 'bg-emerald-50/70 border-emerald-500 shadow-md ring-1 ring-emerald-400' 
                      : 'bg-white border-slate-200 hover:border-emerald-300'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] font-black uppercase tracking-wider text-emerald-800">
                      Grade-A Confluence
                    </span>
                    <span className="p-1.5 bg-emerald-100 text-emerald-700 rounded-lg">
                      <CheckCircle2 size={16} />
                    </span>
                  </div>
                  <div className="mt-2 text-2xl sm:text-3xl font-black text-emerald-950">
                    {scanResult.summary?.unanimous_count || 0}
                  </div>
                  <p className="text-[11px] text-emerald-700/90 font-medium mt-1">
                    Both Qlib & Legacy Agree
                  </p>
                </div>

                <div 
                  onClick={() => setActiveTab('conflicts')}
                  className={`p-4 rounded-2xl border cursor-pointer transition-all ${
                    activeTab === 'conflicts' 
                      ? 'bg-amber-50/70 border-amber-500 shadow-md ring-1 ring-amber-400' 
                      : 'bg-white border-slate-200 hover:border-amber-300'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] font-black uppercase tracking-wider text-amber-800">
                      Conflicts
                    </span>
                    <span className="p-1.5 bg-amber-100 text-amber-700 rounded-lg">
                      <ShieldAlert size={16} />
                    </span>
                  </div>
                  <div className="mt-2 text-2xl sm:text-3xl font-black text-amber-950">
                    {scanResult.summary?.conflict_count || 0}
                  </div>
                  <p className="text-[11px] text-amber-700/90 font-medium mt-1">
                    Models Disagree
                  </p>
                </div>

                <div 
                  onClick={() => setActiveTab('qlib')}
                  className={`p-4 rounded-2xl border cursor-pointer transition-all ${
                    activeTab === 'qlib' 
                      ? 'bg-purple-50/70 border-purple-500 shadow-md ring-1 ring-purple-400' 
                      : 'bg-white border-slate-200 hover:border-purple-300'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] font-black uppercase tracking-wider text-purple-800">
                      Qlib Alpha Solo
                    </span>
                    <span className="p-1.5 bg-purple-100 text-purple-700 rounded-lg">
                      <Cpu size={16} />
                    </span>
                  </div>
                  <div className="mt-2 text-2xl sm:text-3xl font-black text-purple-950 flex items-baseline justify-between">
                    <span>{scanResult.summary?.qlib_solo_count || 0}</span>
                    {(scanResult.summary?.unanimous_count || 0) > 0 && (
                      <span className="text-[10px] font-bold text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded-full border border-emerald-200">
                        +{scanResult.summary.unanimous_count} Confluent
                      </span>
                    )}
                  </div>
                  <p className="text-[11px] text-purple-700/90 font-medium mt-1">
                    Quant Factor Picks Only
                  </p>
                </div>

                <div 
                  onClick={() => setActiveTab('legacy')}
                  className={`p-4 rounded-2xl border cursor-pointer transition-all ${
                    activeTab === 'legacy' 
                      ? 'bg-blue-50/70 border-blue-500 shadow-md ring-1 ring-blue-400' 
                      : 'bg-white border-slate-200 hover:border-blue-300'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] font-black uppercase tracking-wider text-blue-800">
                      Legacy AI Solo
                    </span>
                    <span className="p-1.5 bg-blue-100 text-blue-700 rounded-lg">
                      <Zap size={16} />
                    </span>
                  </div>
                  <div className="mt-2 text-2xl sm:text-3xl font-black text-blue-950 flex items-baseline justify-between">
                    <span>{scanResult.summary?.legacy_solo_count || 0}</span>
                    {(scanResult.summary?.unanimous_count || 0) > 0 && (
                      <span className="text-[10px] font-bold text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded-full border border-emerald-200">
                        +{scanResult.summary.unanimous_count} Confluent
                      </span>
                    )}
                  </div>
                  <p className="text-[11px] text-blue-700/90 font-medium mt-1">
                    Multi-Model Ensemble Picks
                  </p>
                </div>

                <div 
                  onClick={() => setActiveTab('gated')}
                  className={`p-4 rounded-2xl border cursor-pointer transition-all ${
                    activeTab === 'gated' 
                      ? 'bg-slate-100 border-slate-500 shadow-md ring-1 ring-slate-400' 
                      : 'bg-white border-slate-200 hover:border-slate-300'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-[11px] font-black uppercase tracking-wider text-slate-800">
                      Gated Setups
                    </span>
                    <span className="p-1.5 bg-slate-100 text-slate-700 rounded-lg">
                      <ShieldCheck size={16} />
                    </span>
                  </div>
                  <div className="mt-2 text-2xl sm:text-3xl font-black text-slate-900">
                    {scanResult.screened_out_trades?.qlib_rejected?.length || scanResult.engines?.qlib?.rejected_count || 0}
                  </div>
                  <p className="text-[11px] text-slate-500 font-medium mt-1">
                    Regime & Runway Gated
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* Results Sections */}
          {scanResult && !loading && (
            <div className="space-y-4">
              <div className="flex border-b border-slate-200 space-x-6 overflow-x-auto pb-1">
                <button
                  onClick={() => setActiveTab('unanimous')}
                  className={`pb-3 text-xs font-black uppercase tracking-wider transition-all border-b-2 whitespace-nowrap ${
                    activeTab === 'unanimous'
                      ? 'border-emerald-600 text-emerald-700'
                      : 'border-transparent text-slate-500 hover:text-slate-800'
                  }`}
                >
                  ★ Unanimous Confluence ({(scanResult.unanimous_trades?.length || 0) > 0 ? scanResult.unanimous_trades.length : (scanResult.previous_active_trades?.length ? `${scanResult.previous_active_trades.length} active` : 0)})
                </button>
                <button
                  onClick={() => setActiveTab('conflicts')}
                  className={`pb-3 text-xs font-black uppercase tracking-wider transition-all border-b-2 whitespace-nowrap ${
                    activeTab === 'conflicts'
                      ? 'border-amber-600 text-amber-700'
                      : 'border-transparent text-slate-500 hover:text-slate-800'
                  }`}
                >
                  ⚠ Directional Conflicts ({scanResult.conflicting_trades?.length || 0})
                </button>
                <button
                  onClick={() => setActiveTab('qlib')}
                  className={`pb-3 text-xs font-black uppercase tracking-wider transition-all border-b-2 whitespace-nowrap ${
                    activeTab === 'qlib'
                      ? 'border-purple-600 text-purple-700'
                      : 'border-transparent text-slate-500 hover:text-slate-800'
                  }`}
                >
                  Qlib Solo ({scanResult.qlib_solo?.length || 0})
                </button>
                <button
                  onClick={() => setActiveTab('legacy')}
                  className={`pb-3 text-xs font-black uppercase tracking-wider transition-all border-b-2 whitespace-nowrap ${
                    activeTab === 'legacy'
                      ? 'border-blue-600 text-blue-700'
                      : 'border-transparent text-slate-500 hover:text-slate-800'
                  }`}
                >
                  Legacy Solo ({scanResult.legacy_solo?.length || 0})
                </button>
                <button
                  onClick={() => setActiveTab('gated')}
                  className={`pb-3 text-xs font-black uppercase tracking-wider transition-all border-b-2 whitespace-nowrap ${
                    activeTab === 'gated'
                      ? 'border-slate-800 text-slate-900'
                      : 'border-transparent text-slate-500 hover:text-slate-800'
                  }`}
                >
                  🛡️ Gated / Screened Out ({scanResult.screened_out_trades?.qlib_rejected?.length || 0})
                </button>
              </div>

              {/* TAB 1: UNANIMOUS CONFLUENCE SETUPS */}
              {activeTab === 'unanimous' && (() => {
                const currentTrades = scanResult.unanimous_trades || [];
                const previousTrades = scanResult.previous_active_trades || [];
                const isShowingPrevious = currentTrades.length === 0 && previousTrades.length > 0;
                const tradesToDisplay = isShowingPrevious ? previousTrades : currentTrades;

                return (
                  <div className="space-y-4">
                    {isShowingPrevious && (
                      <div className="bg-amber-50 border border-amber-200 rounded-xl p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-2 text-xs text-amber-900">
                        <div className="flex items-center space-x-2">
                          <Clock size={16} className="text-amber-600 flex-shrink-0" />
                          <span>
                            <strong>Current sweep returned 0 new setups.</strong> Showing active setups retained from earlier sweep ({scanResult.previous_active_scan_at ? new Date(scanResult.previous_active_scan_at).toLocaleTimeString() : 'Earlier'}).
                          </span>
                        </div>
                        <span className="text-[10px] font-mono bg-amber-200/80 text-amber-950 px-2.5 py-1 rounded font-bold uppercase tracking-wider self-start sm:self-auto">
                          RETAINED ACTIVE SETUPS
                        </span>
                      </div>
                    )}

                    {tradesToDisplay.length === 0 ? (
                      <div className="bg-white border border-slate-200 rounded-2xl p-8 text-center">
                        <div className="w-12 h-12 bg-slate-100 text-slate-400 rounded-full flex items-center justify-center mx-auto mb-3">
                          <Info size={24} />
                        </div>
                        <h3 className="text-sm font-bold text-slate-800">No Unanimous Confluence in Current Sweep</h3>
                        <p className="text-xs text-slate-500 max-w-md mx-auto mt-1">
                          Both engines generated distinct non-overlapping picks. Inspect the Solo tabs or check back during market hours when the 15-minute daemon operates.
                        </p>
                      </div>
                    ) : (
                      tradesToDisplay.map((trade, idx) => {
                        const isExpanded = !!expandedCards[`u_${idx}`];
                        const stratBadge = trade.strategy || scanResult.strategy || 'INTRADAY';
                        const tfBadge = trade.timeframe || (stratBadge === 'INTRADAY' ? '15m' : '1d');

                        return (
                          <div 
                            key={idx} 
                            className="bg-white border-2 border-emerald-500/70 rounded-2xl p-5 shadow-sm hover:shadow-md transition-all relative overflow-hidden"
                          >
                            <div className="absolute top-0 right-0 bg-emerald-600 text-white text-[10px] font-black uppercase px-3 py-1 rounded-bl-xl tracking-wider">
                              GRADE-A CONFLUENCE
                            </div>

                            <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
                              <div className="flex items-center space-x-3">
                                <div className={`p-3 rounded-xl ${
                                  trade.direction === 'BUY' 
                                    ? 'bg-emerald-100 text-emerald-700' 
                                    : 'bg-rose-100 text-rose-700'
                                }`}>
                                  {trade.direction === 'BUY' ? <TrendingUp size={24} /> : <TrendingDown size={24} />}
                                </div>
                                <div>
                                  <div className="flex items-center space-x-2">
                                    <h2 className="text-lg font-black text-slate-900">{trade.ticker}</h2>
                                    <span className={`px-2 py-0.5 rounded text-[10px] font-black ${
                                      trade.direction === 'BUY'
                                        ? 'bg-emerald-100 text-emerald-800'
                                        : 'bg-rose-100 text-rose-800'
                                    }`}>
                                      {trade.direction}
                                    </span>
                                    <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                                      stratBadge === 'INTRADAY' 
                                        ? 'bg-cyan-100 text-cyan-800 border border-cyan-300' 
                                        : 'bg-purple-100 text-purple-800 border border-purple-300'
                                    }`}>
                                      {stratBadge} • {tfBadge}
                                    </span>
                                  </div>
                                  <p className="text-xs text-slate-500 font-mono mt-0.5">
                                    Blended Entry: ₹{trade.blended_entry} • R:R 1:{trade.risk_reward_ratio}
                                  </p>
                                  <div className="flex flex-wrap items-center gap-1.5 pt-1.5 text-[10px] font-mono">
                                    <span className="px-1.5 py-0.5 rounded bg-purple-50 text-purple-800 border border-purple-200 font-semibold">
                                      ATR(14): ₹{trade.qlib_metrics?.atr_14 ?? trade.atr_14 ?? (trade.blended_entry ? (trade.blended_entry * 0.008).toFixed(2) : 'N/A')}
                                    </span>
                                    <span className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-800 border border-indigo-200 font-semibold">
                                      RSI: {trade.legacy_metrics?.explanation?.rsi ?? trade.explanation?.rsi ?? 'N/A'} • ADX: {trade.legacy_metrics?.explanation?.adx ?? trade.explanation?.adx ?? 'N/A'}
                                    </span>
                                    <span className={`px-1.5 py-0.5 rounded border ${
                                      trade.fno_metrics?.is_fno || trade.fno_confluence?.is_fno || trade.fno_metrics?.status === 'F&O ACTIVE'
                                        ? 'bg-sky-50 text-sky-800 border-sky-200 font-bold'
                                        : 'bg-slate-100 text-slate-600 border-slate-200'
                                    }`}>
                                      {trade.fno_metrics?.is_fno || trade.fno_confluence?.is_fno || trade.fno_metrics?.status === 'F&O ACTIVE' ? 'F&O ACTIVE' : 'CASH ONLY'}
                                    </span>
                                    <span className={`px-1.5 py-0.5 rounded border ${
                                      (trade.sentiment_metrics?.polarity || 'NEUTRAL') === 'BULLISH'
                                        ? 'bg-emerald-50 text-emerald-800 border-emerald-200 font-bold'
                                        : (trade.sentiment_metrics?.polarity === 'BEARISH')
                                          ? 'bg-rose-50 text-rose-800 border-rose-200 font-bold'
                                          : 'bg-slate-100 text-slate-600 border-slate-200'
                                    }`}>
                                      NLP: {trade.sentiment_metrics?.polarity || 'NEUTRAL'}
                                    </span>
                                    {trade.chart_pattern?.detected && (
                                      <span className="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200 font-bold">
                                        PATTERN: {trade.chart_pattern.pattern_name.replace(/_/g, ' ')}
                                      </span>
                                    )}
                                  </div>
                                </div>
                              </div>

                              <div className="bg-slate-50 border border-slate-200 rounded-xl px-4 py-2.5 flex items-center space-x-6">
                                <div>
                                  <span className="text-[10px] font-bold text-slate-500 uppercase tracking-wider block">
                                    Final Calibrated Conviction
                                  </span>
                                  <div className="flex items-baseline space-x-2">
                                    <span className="text-xl font-black text-emerald-600 font-mono">
                                      {trade.meta_learner_v2?.final_conviction ?? trade.composite_score}%
                                    </span>
                                    {trade.meta_learner_v2?.meta_adjustment !== undefined && trade.meta_learner_v2?.meta_adjustment !== 0 && (
                                      <span className={`text-[10px] font-bold font-mono px-1.5 py-0.5 rounded ${
                                        trade.meta_learner_v2.meta_adjustment > 0 
                                          ? 'bg-emerald-100 text-emerald-800' 
                                          : 'bg-rose-100 text-rose-800'
                                      }`}>
                                        {trade.meta_learner_v2.meta_adjustment > 0 ? `+${trade.meta_learner_v2.meta_adjustment}%` : `${trade.meta_learner_v2.meta_adjustment}%`}
                                      </span>
                                    )}
                                  </div>
                                </div>
                                <div className="border-l border-slate-200 pl-4 text-xs font-mono space-y-0.5">
                                  <div className="text-slate-600">Base Conviction: <strong className="text-slate-800">{trade.meta_learner_v2?.base_conviction ?? trade.composite_score}%</strong></div>
                                  <div className="text-slate-600">Qlib: <strong className="text-purple-700">{trade.qlib_metrics?.score}%</strong> | Legacy: <strong className="text-indigo-700">{trade.legacy_metrics?.confidence}%</strong></div>
                                </div>
                              </div>
                            </div>

                            <div className="mt-4 pt-4 border-t border-slate-100 grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                              <div className="bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                                <span className="text-[10px] text-slate-500 block uppercase font-semibold">Blended Entry</span>
                                <strong className="font-mono text-slate-800 text-sm">₹{trade.blended_entry}</strong>
                              </div>
                              <div className="bg-rose-50/60 p-2.5 rounded-xl border border-rose-200">
                                <span className="text-[10px] text-rose-700 block uppercase font-semibold">Conservative Stop Loss</span>
                                <strong className="font-mono text-rose-800 text-sm">₹{trade.blended_sl}</strong>
                              </div>
                              <div className="bg-emerald-50/60 p-2.5 rounded-xl border border-emerald-200">
                                <span className="text-[10px] text-emerald-700 block uppercase font-semibold">Primary Target</span>
                                <strong className="font-mono text-emerald-800 text-sm">₹{trade.blended_tp}</strong>
                              </div>
                              <div className="bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                                <span className="text-[10px] text-slate-500 block uppercase font-semibold">Risk : Reward</span>
                                <strong className="font-mono text-slate-800 text-sm">1 : {trade.risk_reward_ratio}</strong>
                              </div>
                            </div>

                            {/* Expandable Technicalities Toggle */}
                            <div className="mt-3 pt-2 flex items-center justify-between border-t border-slate-100">
                              <button
                                onClick={() => toggleCardExpansion(`u_${idx}`)}
                                className="text-xs font-bold text-slate-600 hover:text-emerald-700 flex items-center space-x-1 transition-all"
                              >
                                <Sliders size={13} />
                                <span>{isExpanded ? 'Hide Deep Technicalities' : 'View Deep Technicalities & Model Telemetry'}</span>
                                {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                              </button>
                              <span className="text-[11px] text-slate-400 font-mono">Qlib + Legacy Confluence</span>
                            </div>

                            {/* Expanded Technical Details Drawer */}
                            {isExpanded && (
                              <div className="mt-3 bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-4 font-mono text-xs animate-in fade-in duration-200">
                                {/* Row 1: Core Engine Quant & Ensemble Indicators */}
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                  {/* Qlib Breakdown */}
                                  <div className="bg-purple-50/60 border border-purple-200 rounded-xl p-3 space-y-1.5">
                                    <div className="flex items-center justify-between text-purple-900 font-bold border-b border-purple-200 pb-1">
                                      <span className="flex items-center space-x-1.5">
                                        <Cpu size={14} />
                                        <span>Microsoft Qlib (Alpha-158)</span>
                                      </span>
                                      <span className="px-2 py-0.5 bg-purple-100 text-purple-800 rounded font-black">
                                        Score: {trade.qlib_metrics?.score}%
                                      </span>
                                    </div>
                                    <div className="text-[11px] text-purple-800 space-y-1 pt-1">
                                      <div>Alpha Raw Prediction: <strong>{trade.qlib_metrics?.raw_prediction ?? 'N/A'}</strong></div>
                                      <div>Rank Percentile: <strong>{trade.qlib_metrics?.rank_percentile ?? 'Top Decile'}%</strong></div>
                                      <div>ATR Volatility Envelope: <strong className="text-purple-950 font-black">₹{trade.qlib_metrics?.atr_14 ?? trade.atr_14 ?? (trade.blended_entry ? (trade.blended_entry * 0.008).toFixed(2) : 'N/A')}</strong></div>
                                      <div>Qlib Anchor SL: ₹{trade.qlib_metrics?.stop_loss ?? '—'} | TP: ₹{trade.qlib_metrics?.target_price ?? '—'}</div>
                                    </div>
                                  </div>

                                  {/* Legacy Breakdown */}
                                  <div className="bg-indigo-50/60 border border-indigo-200 rounded-xl p-3 space-y-1.5">
                                    <div className="flex items-center justify-between text-indigo-900 font-bold border-b border-indigo-200 pb-1">
                                      <span className="flex items-center space-x-1.5">
                                        <Zap size={14} />
                                        <span>Legacy AI Multi-Model Ensemble</span>
                                      </span>
                                      <span className="px-2 py-0.5 bg-indigo-100 text-indigo-800 rounded font-black">
                                        Win Rate: {trade.legacy_metrics?.confidence}%
                                      </span>
                                    </div>
                                    <div className="text-[11px] text-indigo-800 space-y-1 pt-1">
                                      <div className="flex items-center space-x-3">
                                        <span>RF: <strong>{trade.legacy_metrics?.base_probs?.rf ?? 50}%</strong></span>
                                        <span>GB: <strong>{trade.legacy_metrics?.base_probs?.gb ?? 50}%</strong></span>
                                        <span>SVC: <strong>{trade.legacy_metrics?.base_probs?.svc ?? 50}%</strong></span>
                                      </div>
                                      <div>RSI(14): <strong className="text-indigo-950 font-black">{trade.legacy_metrics?.explanation?.rsi ?? trade.explanation?.rsi ?? 'N/A'}</strong> • ADX: <strong className="text-indigo-950 font-black">{trade.legacy_metrics?.explanation?.adx ?? trade.explanation?.adx ?? 'N/A'}</strong></div>
                                      <div>Vol Multiple: <strong>{trade.legacy_metrics?.explanation?.volume_ratio ?? 1.0}x</strong></div>
                                      <div>Legacy Anchor SL: ₹{trade.legacy_metrics?.sl ?? '—'} | TP1: ₹{trade.legacy_metrics?.tp1 ?? '—'}</div>
                                    </div>
                                  </div>
                                </div>

                                {/* Row 1.5: Consensus Meta-Learner V2 Empirical Attribution Box */}
                                {trade.meta_learner_v2 && (
                                  <div className="bg-slate-900 text-slate-100 rounded-xl p-3.5 border border-slate-800 space-y-2">
                                    <div className="flex items-center justify-between border-b border-slate-800 pb-2">
                                      <div className="flex items-center space-x-2">
                                        <Sparkles size={14} className="text-amber-400" />
                                        <span className="font-bold text-slate-200">Consensus Meta-Learner V2 (De Prado Meta-Labeling)</span>
                                      </div>
                                      <div className="flex items-center space-x-2 font-mono">
                                        <span className="text-[10px] text-slate-400">Base: {trade.meta_learner_v2.base_conviction}%</span>
                                        <span className="text-slate-600">→</span>
                                        <span className="text-[11px] font-black text-amber-300">
                                          Calibrated Conviction: {trade.meta_learner_v2.final_conviction}%
                                        </span>
                                      </div>
                                    </div>
                                    <div className="grid grid-cols-2 sm:grid-cols-6 gap-2 pt-1 font-mono text-[10px]">
                                      <div className="bg-slate-800/80 p-2 rounded-lg border border-slate-700/60">
                                        <span className="text-slate-400 block">Market Regime</span>
                                        <strong className={Number(trade.meta_learner_v2.group_attributions?.MARKET_REGIME || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}>
                                          {Number(trade.meta_learner_v2.group_attributions?.MARKET_REGIME || 0) >= 0 ? `+${trade.meta_learner_v2.group_attributions?.MARKET_REGIME || 0}%` : `${trade.meta_learner_v2.group_attributions?.MARKET_REGIME || 0}%`}
                                        </strong>
                                      </div>
                                      <div className="bg-slate-800/80 p-2 rounded-lg border border-slate-700/60">
                                        <span className="text-slate-400 block">Sector Context</span>
                                        <strong className={Number(trade.meta_learner_v2.group_attributions?.SECTOR_CONTEXT || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}>
                                          {Number(trade.meta_learner_v2.group_attributions?.SECTOR_CONTEXT || 0) >= 0 ? `+${trade.meta_learner_v2.group_attributions?.SECTOR_CONTEXT || 0}%` : `${trade.meta_learner_v2.group_attributions?.SECTOR_CONTEXT || 0}%`}
                                        </strong>
                                      </div>
                                      <div className="bg-slate-800/80 p-2 rounded-lg border border-slate-700/60">
                                        <span className="text-slate-400 block">Rel Strength</span>
                                        <strong className={Number(trade.meta_learner_v2.group_attributions?.RELATIVE_STRENGTH || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}>
                                          {Number(trade.meta_learner_v2.group_attributions?.RELATIVE_STRENGTH || 0) >= 0 ? `+${trade.meta_learner_v2.group_attributions?.RELATIVE_STRENGTH || 0}%` : `${trade.meta_learner_v2.group_attributions?.RELATIVE_STRENGTH || 0}%`}
                                        </strong>
                                      </div>
                                      <div className="bg-slate-800/80 p-2 rounded-lg border border-slate-700/60">
                                        <span className="text-slate-400 block">F&O Liquidity</span>
                                        <strong className={Number(trade.meta_learner_v2.group_attributions?.FNO_CONTEXT || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}>
                                          {Number(trade.meta_learner_v2.group_attributions?.FNO_CONTEXT || 0) >= 0 ? `+${trade.meta_learner_v2.group_attributions?.FNO_CONTEXT || 0}%` : `${trade.meta_learner_v2.group_attributions?.FNO_CONTEXT || 0}%`}
                                        </strong>
                                      </div>
                                      <div className="bg-slate-800/80 p-2 rounded-lg border border-slate-700/60">
                                        <span className="text-slate-400 block">FinBERT News</span>
                                        <strong className={Number(trade.meta_learner_v2.group_attributions?.FINBERT_NEWS || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}>
                                          {Number(trade.meta_learner_v2.group_attributions?.FINBERT_NEWS || 0) >= 0 ? `+${trade.meta_learner_v2.group_attributions?.FINBERT_NEWS || 0}%` : `${trade.meta_learner_v2.group_attributions?.FINBERT_NEWS || 0}%`}
                                        </strong>
                                      </div>
                                      <div className="bg-slate-800/80 p-2 rounded-lg border border-slate-700/60">
                                        <span className="text-slate-400 block">Price Structure</span>
                                        <strong className={Number(trade.meta_learner_v2.group_attributions?.CHART_STRUCTURE || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}>
                                          {Number(trade.meta_learner_v2.group_attributions?.CHART_STRUCTURE || 0) >= 0 ? `+${trade.meta_learner_v2.group_attributions?.CHART_STRUCTURE || 0}%` : `${trade.meta_learner_v2.group_attributions?.CHART_STRUCTURE || 0}%`}
                                        </strong>
                                      </div>
                                    </div>
                                  </div>
                                )}

                                {/* Row 2: FinBERT Financial NLP Sentiment & Option Chain F&O */}
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                                  {/* FinBERT NLP Card */}
                                  <div className="bg-emerald-50/70 border border-emerald-200 rounded-xl p-3 space-y-2">
                                    <div className="flex items-center justify-between text-emerald-950 font-bold border-b border-emerald-200 pb-1">
                                      <span className="flex items-center space-x-1.5">
                                        <Newspaper size={14} className="text-emerald-700" />
                                        <span>FinBERT NLP Financial Sentiment</span>
                                      </span>
                                      <span className={`px-2 py-0.5 rounded text-[10px] font-black uppercase ${
                                        (trade.sentiment_metrics?.polarity || (trade.finbert_sentiment?.score > 0.05 ? 'BULLISH' : (trade.finbert_sentiment?.score < -0.05 ? 'BEARISH' : (trade.legacy_metrics?.explanation?.nlp_sentiment > 0.05 ? 'BULLISH' : (trade.legacy_metrics?.explanation?.nlp_sentiment < -0.05 ? 'BEARISH' : 'NEUTRAL'))))) === 'BULLISH'
                                          ? 'bg-emerald-200 text-emerald-900 border border-emerald-300'
                                          : (trade.sentiment_metrics?.polarity === 'BEARISH' || trade.finbert_sentiment?.score < -0.05 || trade.legacy_metrics?.explanation?.nlp_sentiment < -0.05)
                                            ? 'bg-rose-200 text-rose-900 border border-rose-300'
                                            : 'bg-slate-200 text-slate-800'
                                      }`}>
                                        {trade.sentiment_metrics?.polarity || (trade.finbert_sentiment?.score > 0.05 ? 'BULLISH' : (trade.finbert_sentiment?.score < -0.05 ? 'BEARISH' : (trade.legacy_metrics?.explanation?.nlp_sentiment > 0.05 ? 'BULLISH' : (trade.legacy_metrics?.explanation?.nlp_sentiment < -0.05 ? 'BEARISH' : 'NEUTRAL'))))} (Score: {(trade.sentiment_metrics?.score ?? trade.finbert_sentiment?.score ?? trade.legacy_metrics?.explanation?.nlp_sentiment ?? 0.0) > 0 ? `+${(trade.sentiment_metrics?.score ?? trade.finbert_sentiment?.score ?? trade.legacy_metrics?.explanation?.nlp_sentiment ?? 0.0).toFixed(2)}` : (trade.sentiment_metrics?.score ?? trade.finbert_sentiment?.score ?? trade.legacy_metrics?.explanation?.nlp_sentiment ?? 0.0).toFixed(2)})
                                      </span>
                                    </div>
                                    <div className="text-[11px] text-emerald-900 space-y-1.5">
                                      <div className="bg-white/80 p-2 rounded-lg border border-emerald-100 text-slate-700 italic">
                                        "{trade.sentiment_metrics?.headline || trade.finbert_sentiment?.headline || trade.legacy_metrics?.explanation?.nlp_headline || 'Market sentiment neutral — no high-impact catalyst detected.'}"
                                      </div>
                                      <div className="flex items-center justify-between text-[10px] text-emerald-700 pt-0.5">
                                        <span>Model: ProsusAI/finbert (Transformer)</span>
                                        <span>Articles Analyzed: {trade.sentiment_metrics?.articles_analyzed ?? 1}</span>
                                      </div>
                                    </div>
                                  </div>

                                  {/* Options Chain & F&O Card */}
                                  <div className="bg-sky-50/70 border border-sky-200 rounded-xl p-3 space-y-2">
                                    <div className="flex items-center justify-between text-sky-950 font-bold border-b border-sky-200 pb-1">
                                      <span className="flex items-center space-x-1.5">
                                        <Activity size={14} className="text-sky-700" />
                                        <span>F&O Derivatives & Liquidity</span>
                                      </span>
                                      <span className={`px-2 py-0.5 rounded text-[10px] font-black uppercase ${
                                        trade.fno_metrics?.is_fno || trade.fno_confluence?.is_fno || trade.fno_metrics?.status === 'F&O ACTIVE'
                                          ? 'bg-sky-200 text-sky-900 border border-sky-300'
                                          : 'bg-slate-200 text-slate-700'
                                      }`}>
                                        {trade.fno_metrics?.is_fno || trade.fno_confluence?.is_fno || trade.fno_metrics?.status === 'F&O ACTIVE' ? 'F&O ELIGIBLE' : 'EQUITY CASH ONLY'}
                                      </span>
                                    </div>
                                    <div className="text-[11px] text-sky-900 space-y-1 pt-0.5">
                                      <div className="flex items-center justify-between">
                                        <span>Put-Call Ratio (PCR):</span>
                                        <strong>{(trade.fno_metrics?.pcr ?? trade.fno_confluence?.pcr) ? `${trade.fno_metrics?.pcr ?? trade.fno_confluence?.pcr} (${(trade.fno_metrics?.pcr ?? trade.fno_confluence?.pcr) >= 1.0 ? 'Bullish Put Base' : 'Bearish Call Base'})` : 'N/A (Cash Segment)'}</strong>
                                      </div>
                                      <div className="flex items-center justify-between">
                                        <span>Options Max Pain Strike:</span>
                                        <strong>{(trade.fno_metrics?.max_pain ?? trade.fno_confluence?.max_pain) ? `₹${trade.fno_metrics?.max_pain ?? trade.fno_confluence?.max_pain}` : 'N/A'}</strong>
                                      </div>
                                      <div className="text-[10px] text-sky-700 pt-1 border-t border-sky-100 flex items-center justify-between">
                                        <span>Institutional Data Feed:</span>
                                        <span className="font-semibold text-sky-900">Upstox V3 Stream & SQLite Cache (0 REST Quota Consumed)</span>
                                      </div>
                                    </div>
                                  </div>
                                </div>

                                {/* Row 3: How The Chart & Price Envelope Is Made */}
                                <div className="bg-white border border-slate-200 rounded-xl p-3.5 space-y-3">
                                  <div className="flex items-center justify-between border-b border-slate-100 pb-2">
                                    <div className="flex items-center space-x-2 text-slate-800 font-bold">
                                      <Compass size={15} className="text-amber-600" />
                                      <span>Chart Pattern Engine & Target Projection Architecture</span>
                                    </div>
                                    <div className="flex items-center space-x-2">
                                      <span className={`text-[10px] px-2 py-0.5 rounded font-black uppercase ${
                                        trade.chart_pattern?.detected
                                          ? 'bg-amber-200 text-amber-900 border border-amber-300'
                                          : 'bg-slate-200 text-slate-700'
                                      }`}>
                                        {trade.chart_pattern?.detected ? trade.chart_pattern.pattern_name.replace(/_/g, ' ') : 'TREND & VWAP CLEARANCE'}
                                      </span>
                                      <span className="text-[10px] px-2 py-0.5 bg-amber-100 text-amber-800 rounded font-bold">
                                        {trade.chart_pattern?.detected ? `${trade.chart_pattern.confidence}% Pattern Confidence` : 'Mathematical Envelope'}
                                      </span>
                                    </div>
                                  </div>

                                  {/* Pattern Details Callout */}
                                  <div className={`p-2.5 rounded-lg border text-[11px] space-y-1 ${
                                    trade.chart_pattern?.detected
                                      ? 'bg-amber-50/70 border-amber-200 text-amber-900'
                                      : 'bg-slate-50 border-slate-200 text-slate-600'
                                  }`}>
                                    <div className="flex items-center justify-between font-bold">
                                      <span className="flex items-center space-x-1.5">
                                        <TrendingUp size={13} className={trade.chart_pattern?.detected ? 'text-amber-700' : 'text-slate-500'} />
                                        <span>Geometric Structure: <strong>{trade.chart_pattern?.detected ? trade.chart_pattern.pattern_name.replace(/_/g, ' ') : 'Consolidation Envelope / No Breakout'}</strong></span>
                                      </span>
                                      <span className="font-mono text-[10px]">
                                        Status: <strong>{trade.chart_pattern?.status || (trade.chart_pattern?.detected ? 'CONFIRMED_CONTINUATION' : 'NO_GEOMETRIC_PATTERN')}</strong>
                                      </span>
                                    </div>
                                    <p className="text-[10px] leading-relaxed">
                                      {trade.chart_pattern?.details || 'Standard algorithmic trend alignment: Price cleared session VWAP and day open with EMA 9 > 21 > 50 directional momentum.'}
                                    </p>
                                    {trade.chart_pattern?.structural_tp && (
                                      <div className="text-[10px] text-amber-800 font-mono pt-0.5 flex items-center space-x-3">
                                        <span>Geometric TP: <strong>₹{trade.chart_pattern.structural_tp}</strong></span>
                                        <span>Structural Invalidation SL: <strong>₹{trade.chart_pattern.structural_sl}</strong></span>
                                        <span>Pattern R:R: <strong>1:{trade.chart_pattern.risk_reward_ratio || trade.risk_reward_ratio}</strong></span>
                                      </div>
                                    )}
                                  </div>

                                  {/* Visual Risk-Reward Price Channel */}
                                  <div className="space-y-1.5">
                                    <div className="flex items-center justify-between text-[11px] font-bold">
                                      <span className="text-rose-600">SL: ₹{trade.blended_sl}</span>
                                      <span className="text-blue-600">Entry: ₹{trade.blended_entry}</span>
                                      <span className="text-emerald-600">TP1: ₹{trade.blended_tp}</span>
                                      <span className="text-purple-600">
                                        TP2: ₹{trade.legacy_metrics?.tp2 || (trade.direction === 'BUY' ? (Number(trade.blended_entry) + Math.abs(Number(trade.blended_tp) - Number(trade.blended_entry)) * 2).toFixed(2) : (Number(trade.blended_entry) - Math.abs(Number(trade.blended_entry) - Number(trade.blended_tp)) * 2).toFixed(2))}
                                      </span>
                                    </div>

                                    {/* Visual Track */}
                                    <div className="h-3 w-full bg-slate-100 rounded-full overflow-hidden flex items-center border border-slate-200">
                                      <div className="h-full bg-rose-400 w-1/4" title="Protective Stop Zone (-1.5× ATR)" />
                                      <div className="h-full bg-blue-500 w-1/4" title="Blended Entry Anchor (VWAP / Open)" />
                                      <div className="h-full bg-emerald-500 w-1/4" title="Primary Target 1 (+1.5× ATR)" />
                                      <div className="h-full bg-purple-500 w-1/4" title="Runner Target 2 (+3.0× ATR)" />
                                    </div>

                                    <div className="flex items-center justify-between text-[10px] text-slate-400">
                                      <span>Risk: -₹{Math.abs(Number(trade.blended_entry) - Number(trade.blended_sl)).toFixed(2)} (1.5× ATR)</span>
                                      <span>Reward TP1: +₹{Math.abs(Number(trade.blended_tp) - Number(trade.blended_entry)).toFixed(2)} (1.5× ATR)</span>
                                      <span>Reward TP2: +₹{(Math.abs(Number(trade.blended_tp) - Number(trade.blended_entry)) * 2).toFixed(2)} (3.0× ATR)</span>
                                    </div>
                                  </div>

                                  {/* 4-Step Technical Construction Anatomy */}
                                  <div className="grid grid-cols-1 sm:grid-cols-4 gap-2 pt-2 border-t border-slate-100 text-[10px] text-slate-600">
                                    <div className="bg-slate-50 p-2 rounded-lg border border-slate-200">
                                      <strong className="text-slate-800 block font-bold mb-0.5">1. Candle Sourcing</strong>
                                      15m / 1D OHLCV bars ingested via Upstox V3 WebSocket & SQLite DataGateway cache. Zero REST rate limit impact.
                                    </div>
                                    <div className="bg-slate-50 p-2 rounded-lg border border-slate-200">
                                      <strong className="text-slate-800 block font-bold mb-0.5">2. Volatility Math</strong>
                                      14-period True Range (ATR ₹{trade.qlib_metrics?.atr_14 ?? 'N/A'}) establishes real session standard deviation.
                                    </div>
                                    <div className="bg-slate-50 p-2 rounded-lg border border-slate-200">
                                      <strong className="text-slate-800 block font-bold mb-0.5">3. Institutional Anchor</strong>
                                      Entry requires confirmation above Intraday VWAP (₹{trade.vwap || trade.blended_entry}) & Day's Open (₹{trade.day_open || trade.blended_entry}).
                                    </div>
                                    <div className="bg-slate-50 p-2 rounded-lg border border-slate-200">
                                      <strong className="text-slate-800 block font-bold mb-0.5">4. Asymmetric Targets</strong>
                                      Targets and stop bounds dynamically expand with market volatility, locking in R:R ≥ 1:{trade.risk_reward_ratio}.
                                    </div>
                                  </div>
                                </div>

                              </div>
                            )}

                          </div>
                        );
                      })
                    )}
                  </div>
                );
              })()}

              {/* TAB 2: CONFLICTS */}
              {activeTab === 'conflicts' && (
                <div className="space-y-4">
                  {(!scanResult.conflicting_trades || scanResult.conflicting_trades.length === 0) ? (
                    <div className="bg-white border border-slate-200 rounded-2xl p-8 text-center">
                      <div className="w-12 h-12 bg-emerald-50 text-emerald-600 rounded-full flex items-center justify-center mx-auto mb-3">
                        <CheckCircle2 size={24} />
                      </div>
                      <h3 className="text-sm font-bold text-slate-800">Zero Directional Conflicts Detected</h3>
                      <p className="text-xs text-slate-500 max-w-md mx-auto mt-1">
                        Neither engine is taking opposite positions on the same instruments.
                      </p>
                    </div>
                  ) : (
                    scanResult.conflicting_trades.map((item, idx) => {
                      const isExpanded = !!expandedCards[`c_${idx}`];
                      return (
                        <div 
                          key={idx}
                          className="bg-amber-50/50 border border-amber-300 rounded-2xl p-5 shadow-xs"
                        >
                          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <div className="flex items-center space-x-3">
                              <div className="p-2.5 bg-amber-100 text-amber-800 rounded-xl">
                                <AlertTriangle size={20} />
                              </div>
                              <div>
                                <div className="flex items-center space-x-2">
                                  <h3 className="text-base font-black text-amber-950">{item.ticker}</h3>
                                  <span className="px-2 py-0.5 bg-amber-200 text-amber-900 rounded text-[10px] font-black uppercase">
                                    OPPOSING BIAS
                                  </span>
                                  <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                                    (item.strategy || scanResult.strategy || 'INTRADAY') === 'INTRADAY'
                                      ? 'bg-cyan-100 text-cyan-800 border border-cyan-300'
                                      : 'bg-purple-100 text-purple-800 border border-purple-300'
                                  }`}>
                                    {(item.strategy || scanResult.strategy || 'INTRADAY')} • {(item.timeframe || ((item.strategy || scanResult.strategy || 'INTRADAY') === 'INTRADAY' ? '15m' : '1d'))}
                                  </span>
                                </div>
                                <p className="text-xs text-amber-800/90 font-medium mt-0.5">
                                  {item.warning}
                                </p>
                              </div>
                            </div>

                            <div className="flex items-center space-x-4 bg-white/80 border border-amber-200 rounded-xl p-2.5 text-xs font-mono">
                              <div className="text-center">
                                <span className="text-[10px] text-purple-700 block font-bold">QLIB</span>
                                <span className={`font-black ${item.qlib_direction === 'BUY' ? 'text-emerald-600' : 'text-rose-600'}`}>
                                  {item.qlib_direction} ({item.qlib_score}%)
                                </span>
                              </div>
                              <div className="text-slate-300 font-light">vs</div>
                              <div className="text-center">
                                <span className="text-[10px] text-indigo-700 block font-bold">LEGACY</span>
                                <span className={`font-black ${item.legacy_direction === 'BUY' ? 'text-emerald-600' : 'text-rose-600'}`}>
                                  {item.legacy_direction} ({item.legacy_confidence}%)
                                </span>
                              </div>
                            </div>
                          </div>

                          <div className="mt-3 pt-2 flex items-center justify-between border-t border-amber-200">
                            <button
                              onClick={() => toggleCardExpansion(`c_${idx}`)}
                              className="text-xs font-bold text-amber-800 hover:text-amber-950 flex items-center space-x-1 transition-all"
                            >
                              <Sliders size={13} />
                              <span>{isExpanded ? 'Hide Technical Disagreement' : 'View Detailed Technical Disagreement'}</span>
                              {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                            </button>
                            <span className="text-[11px] text-amber-700 font-mono">Opposing Quant vs Technical</span>
                          </div>

                          {isExpanded && (
                            <div className="mt-3 bg-white/90 border border-amber-200 rounded-xl p-3 grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs font-mono">
                              <div className="bg-purple-50 p-2.5 rounded-lg border border-purple-100">
                                <span className="text-[10px] font-bold text-purple-900 block uppercase">Qlib Alpha Rationale</span>
                                <div className="text-[11px] text-purple-800 mt-1">
                                  <div>Alpha Score: <strong>{item.qlib_metrics?.score ?? item.qlib_score}%</strong></div>
                                  <div>Rank: <strong>{item.qlib_metrics?.rank_percentile ?? 'Top'}%</strong></div>
                                  <div>Qlib SL: ₹{item.qlib_metrics?.stop_loss ?? '—'} | TP: ₹{item.qlib_metrics?.target_price ?? '—'}</div>
                                </div>
                              </div>
                              <div className="bg-indigo-50 p-2.5 rounded-lg border border-indigo-100">
                                <span className="text-[10px] font-bold text-indigo-900 block uppercase">Legacy Ensemble Rationale</span>
                                <div className="text-[11px] text-indigo-800 mt-1">
                                  <div>Confidence: <strong>{item.legacy_metrics?.confidence ?? item.legacy_confidence}%</strong></div>
                                  <div>Votes: RF {item.legacy_metrics?.base_probs?.rf ?? 50}% • GB {item.legacy_metrics?.base_probs?.gb ?? 50}%</div>
                                  <div>Legacy SL: ₹{item.legacy_metrics?.sl ?? '—'} | TP1: ₹{item.legacy_metrics?.tp1 ?? '—'}</div>
                                </div>
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              )}

              {/* TAB 3: QLIB SOLO */}
              {activeTab === 'qlib' && (
                <div className="space-y-4">
                  {(!scanResult.qlib_solo || scanResult.qlib_solo.length === 0) ? (
                    <div className="bg-white border border-slate-200 rounded-2xl p-8 text-center text-xs text-slate-500 space-y-2">
                      <p className="font-bold text-slate-800 text-sm">No Qlib Solo Picks in this Sweep</p>
                      {(scanResult.unanimous_trades?.length || 0) > 0 ? (
                        <p className="text-purple-700 font-medium max-w-lg mx-auto">
                          All {scanResult.unanimous_trades.length} qualified Qlib trade(s) achieved unanimous agreement with the Legacy AI engine and are featured in the <strong className="text-emerald-700 cursor-pointer underline hover:text-emerald-800" onClick={() => setActiveTab('unanimous')}>Grade-A Confluence</strong> tab.
                        </p>
                      ) : (
                        <p className="text-slate-500 max-w-md mx-auto">All candidates evaluated by Qlib were either screened out by alpha conviction hurdles or gated by the macro regime.</p>
                      )}
                    </div>
                  ) : (
                    scanResult.qlib_solo.map((opp, idx) => {
                      const isExpanded = !!expandedCards[`q_${idx}`];
                      return (
                        <div key={idx} className="bg-white border border-purple-200 rounded-2xl p-4 shadow-xs hover:border-purple-300 transition-all">
                          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <div className="flex items-center space-x-3">
                              <div className={`p-2.5 rounded-xl ${
                                opp.direction === 'BUY' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                              }`}>
                                {opp.direction === 'BUY' ? <TrendingUp size={20} /> : <TrendingDown size={20} />}
                              </div>
                              <div>
                                <div className="flex items-center space-x-2">
                                  <span className="font-black text-slate-900 text-base">{opp.ticker}</span>
                                  <span className={`px-2 py-0.5 rounded text-[10px] font-black ${
                                    opp.direction === 'BUY' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                                  }`}>
                                    {opp.direction}
                                  </span>
                                  <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                                    (opp.strategy || scanResult.strategy || 'INTRADAY') === 'INTRADAY'
                                      ? 'bg-cyan-100 text-cyan-800 border border-cyan-300'
                                      : 'bg-purple-100 text-purple-800 border border-purple-300'
                                  }`}>
                                    {(opp.strategy || scanResult.strategy || 'INTRADAY')} • {(opp.timeframe || ((opp.strategy || scanResult.strategy || 'INTRADAY') === 'INTRADAY' ? '15m' : '1d'))}
                                  </span>
                                </div>
                                <div className="flex items-center space-x-2 text-xs font-mono mt-0.5">
                                  <span className="text-slate-500">
                                    Alpha-158 Score: <strong className="text-purple-700">{opp.score}%</strong> • Percentile: <strong>{opp.rank_percentile ?? 'Top Tier'}%</strong>
                                  </span>
                                  {opp.meta_learner_v2 && (
                                    <span className={`text-[10px] px-1.5 py-0.2 rounded font-bold ${
                                      opp.meta_learner_v2.meta_adjustment > 0 ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                                    }`}>
                                      Meta V2: {opp.meta_learner_v2.final_conviction}% ({opp.meta_learner_v2.meta_adjustment > 0 ? `+${opp.meta_learner_v2.meta_adjustment}` : opp.meta_learner_v2.meta_adjustment}%)
                                    </span>
                                  )}
                                </div>
                              </div>
                            </div>

                            <div className="flex items-center space-x-3 text-xs font-mono">
                              <div className="bg-slate-50 px-3 py-1.5 rounded-xl border border-slate-200">
                                <span className="text-[10px] text-slate-500 block uppercase">Entry</span>
                                <strong className="text-slate-800">₹{opp.entry}</strong>
                              </div>
                              <div className="bg-rose-50 px-3 py-1.5 rounded-xl border border-rose-200">
                                <span className="text-[10px] text-rose-600 block uppercase">SL</span>
                                <strong className="text-rose-800">₹{opp.sl}</strong>
                              </div>
                              <div className="bg-emerald-50 px-3 py-1.5 rounded-xl border border-emerald-200">
                                <span className="text-[10px] text-emerald-600 block uppercase">TP</span>
                                <strong className="text-emerald-800">₹{opp.tp}</strong>
                              </div>
                            </div>
                          </div>

                          <div className="mt-3 pt-2 flex items-center justify-between border-t border-slate-100">
                            <button
                              onClick={() => toggleCardExpansion(`q_${idx}`)}
                              className="text-xs font-bold text-purple-700 hover:text-purple-900 flex items-center space-x-1 transition-all"
                            >
                              <Sliders size={13} />
                              <span>{isExpanded ? 'Hide Quant Factors' : 'View Alpha-158 Factor Technicalities'}</span>
                              {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                            </button>
                            <span className="text-[11px] text-slate-400 font-mono">Model: {opp.model_id || 'Qlib Champion'}</span>
                          </div>

                          {isExpanded && (
                            <div className="mt-3 bg-purple-50/50 border border-purple-100 rounded-xl p-3 text-xs font-mono space-y-1.5 text-purple-950">
                              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                                <div>Raw Prediction: <strong>{opp.raw_prediction ?? opp.score}</strong></div>
                                <div>Rank Percentile: <strong>{opp.rank_percentile ?? 'Top Decile'}%</strong></div>
                                <div>ATR Envelope: <strong>₹{opp.atr_14 ?? 'N/A'}</strong></div>
                                <div>Data Status: <strong>{opp.pnl_status || 'FRESH'}</strong></div>
                              </div>
                              {opp.model_hash && (
                                <div className="text-[10px] text-purple-700 truncate pt-1 border-t border-purple-200">
                                  Artifact SHA256: {opp.model_hash}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              )}

              {/* TAB 4: LEGACY SOLO */}
              {activeTab === 'legacy' && (
                <div className="space-y-4">
                  {(!scanResult.legacy_solo || scanResult.legacy_solo.length === 0) ? (
                    <div className="bg-white border border-slate-200 rounded-2xl p-8 text-center text-xs text-slate-500">
                      No Legacy AI solo picks found for this sweep.
                    </div>
                  ) : (
                    scanResult.legacy_solo.map((cand, idx) => {
                      const isExpanded = !!expandedCards[`l_${idx}`];
                      return (
                        <div key={idx} className="bg-white border border-indigo-200 rounded-2xl p-4 shadow-xs hover:border-indigo-300 transition-all">
                          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                            <div className="flex items-center space-x-3">
                              <div className={`p-2.5 rounded-xl ${
                                cand.direction === 'BUY' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                              }`}>
                                {cand.direction === 'BUY' ? <TrendingUp size={20} /> : <TrendingDown size={20} />}
                              </div>
                              <div>
                                <div className="flex items-center space-x-2">
                                  <span className="font-black text-slate-900 text-base">{cand.ticker}</span>
                                  <span className={`px-2 py-0.5 rounded text-[10px] font-black ${
                                    cand.direction === 'BUY' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                                  }`}>
                                    {cand.direction}
                                  </span>
                                  <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                                    (cand.strategy || scanResult.strategy || 'INTRADAY') === 'INTRADAY'
                                      ? 'bg-cyan-100 text-cyan-800 border border-cyan-300'
                                      : 'bg-purple-100 text-purple-800 border border-purple-300'
                                  }`}>
                                    {(cand.strategy || scanResult.strategy || 'INTRADAY')} • {(cand.timeframe || ((cand.strategy || scanResult.strategy || 'INTRADAY') === 'INTRADAY' ? '15m' : '1d'))}
                                  </span>
                                </div>
                                <div className="flex items-center space-x-2 text-xs font-mono mt-0.5">
                                  <span className="text-slate-500">
                                    Ensemble Confidence: <strong className="text-indigo-700">{cand.confidence}%</strong> (Raw: {cand.raw_confidence || cand.confidence}%)
                                  </span>
                                  {cand.meta_learner_v2 && (
                                    <span className={`text-[10px] px-1.5 py-0.2 rounded font-bold ${
                                      cand.meta_learner_v2.meta_adjustment > 0 ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                                    }`}>
                                      Meta V2: {cand.meta_learner_v2.final_conviction}% ({cand.meta_learner_v2.meta_adjustment > 0 ? `+${cand.meta_learner_v2.meta_adjustment}` : cand.meta_learner_v2.meta_adjustment}%)
                                    </span>
                                  )}
                                </div>
                              </div>
                            </div>

                            <div className="flex items-center space-x-3 text-xs font-mono">
                              <div className="bg-slate-50 px-3 py-1.5 rounded-xl border border-slate-200">
                                <span className="text-[10px] text-slate-500 block uppercase">Entry</span>
                                <strong className="text-slate-800">₹{cand.entry}</strong>
                              </div>
                              <div className="bg-rose-50 px-3 py-1.5 rounded-xl border border-rose-200">
                                <span className="text-[10px] text-rose-600 block uppercase">SL</span>
                                <strong className="text-rose-800">₹{cand.sl}</strong>
                              </div>
                              <div className="bg-emerald-50 px-3 py-1.5 rounded-xl border border-emerald-200">
                                <span className="text-[10px] text-emerald-600 block uppercase">TP1</span>
                                <strong className="text-emerald-800">₹{cand.tp1}</strong>
                              </div>
                            </div>
                          </div>

                          <div className="mt-3 pt-2 flex items-center justify-between border-t border-slate-100">
                            <button
                              onClick={() => toggleCardExpansion(`l_${idx}`)}
                              className="text-xs font-bold text-indigo-700 hover:text-indigo-900 flex items-center space-x-1 transition-all"
                            >
                              <Sliders size={13} />
                              <span>{isExpanded ? 'Hide Technical Indicators' : 'View Model Votes & Indicator Technicalities'}</span>
                              {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                            </button>
                            <span className="text-[11px] text-slate-400 font-mono">
                              Votes: RF {cand.base_probs?.rf || 50}% • GB {cand.base_probs?.gb || 50}% • SVC {cand.base_probs?.svc || 50}%
                            </span>
                          </div>

                          {isExpanded && (
                            <div className="mt-3 bg-indigo-50/50 border border-indigo-100 rounded-xl p-3 text-xs font-mono space-y-2 text-indigo-950">
                              <div className="flex items-center space-x-4 border-b border-indigo-100 pb-1.5">
                                <span className="font-bold text-indigo-900">Ensemble Votes:</span>
                                <span>Random Forest: <strong>{cand.base_probs?.rf ?? 50}%</strong></span>
                                <span>Gradient Boosting: <strong>{cand.base_probs?.gb ?? 50}%</strong></span>
                                <span>SVC: <strong>{cand.base_probs?.svc ?? 50}%</strong></span>
                              </div>
                              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 pt-0.5">
                                <div>RSI(14): <strong>{cand.explanation?.rsi ?? 'N/A'}</strong></div>
                                <div>ADX Strength: <strong>{cand.explanation?.adx ?? 'N/A'}</strong></div>
                                <div>ATR Volatility: <strong>₹{cand.explanation?.atr ?? 'N/A'}</strong></div>
                                <div>Volume Surge: <strong>{cand.explanation?.volume_ratio ?? 1.0}x</strong></div>
                              </div>
                              <div className="flex items-center justify-between pt-1 border-t border-indigo-100 text-[11px] text-indigo-800">
                                <span>FinBERT Sentiment: <strong>{cand.explanation?.nlp_sentiment || 'NEUTRAL'}</strong></span>
                                <span>Target 2: <strong>₹{cand.tp2 || '—'}</strong></span>
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              )}

              {/* TAB 5: GATED SETUPS (TRANSPARENCY ON REJECTED STOCKS) */}
              {activeTab === 'gated' && (
                <div className="space-y-4">
                  <div className="bg-slate-50 border border-slate-200 rounded-2xl p-5 space-y-3">
                    <div className="flex items-center space-x-2 text-slate-800 font-bold text-sm">
                      <ShieldCheck size={18} className="text-emerald-600" />
                      <span>Capital Protection Guardrails Active</span>
                    </div>
                    <p className="text-xs text-slate-600 leading-relaxed">
                      {(scanResult.strategy || strategy) === 'INTRADAY' ? (
                        `Intraday Regime: ${scanResult.macro_regime?.intraday_verdict || 'INTRADAY_NEUTRAL_CHOP'} ` +
                        `(Session Change: ${scanResult.macro_regime?.intraday_change_pct > 0 ? '+' : ''}${scanResult.macro_regime?.intraday_change_pct ?? 0}%, ` +
                        `vs Open: ${scanResult.macro_regime?.intraday_open_change_pct > 0 ? '+' : ''}${scanResult.macro_regime?.intraday_open_change_pct ?? 0}%). ` +
                        (scanResult.macro_regime?.intraday_verdict === 'INTRADAY_BULL_MOMENTUM' ? 'Counter-trend short setups are held back to avoid catching falling knives against morning momentum.' :
                         scanResult.macro_regime?.intraday_verdict === 'INTRADAY_BEAR_PRESSURE' ? 'Counter-trend long setups are held back to avoid fighting intraday sell pressure.' :
                         'Both long and short setups permitted under neutral session conditions.')
                      ) : (
                        scanResult.screened_out_trades?.macro_regime_reason || (
                          `Swing Macro Regime: ${scanResult.macro_regime?.verdict || 'BEAR_DISTRIBUTION'} (NIFTY 50 at ₹${scanResult.macro_regime?.nifty_close || '23,217'}, ` +
                          `${scanResult.macro_regime?.distance_ema20_pct}% vs 20 EMA). Counter-trend Long swing breakouts are strictly held back to protect capital.`
                        )
                      )}
                    </p>
                  </div>

                  {(!scanResult.screened_out_trades?.qlib_rejected || scanResult.screened_out_trades.qlib_rejected.length === 0) ? (
                    <div className="bg-white border border-slate-200 rounded-2xl p-8 text-center text-xs text-slate-500">
                      No rejected candidates recorded in telemetry for this session.
                    </div>
                  ) : (
                    <div className="bg-white border border-slate-200 rounded-2xl overflow-hidden shadow-xs">
                      <div className="p-4 border-b border-slate-200 flex items-center justify-between">
                        <h3 className="text-xs font-black uppercase text-slate-800 tracking-wider">
                          Sample Gated Equities & Quantitative Filter Reasons
                        </h3>
                        <span className="text-xs font-mono text-slate-500">
                          {scanResult.screened_out_trades.qlib_rejected.length} Filtered Setups
                        </span>
                      </div>
                      <div className="divide-y divide-slate-100 font-mono text-xs">
                        {scanResult.screened_out_trades.qlib_rejected.map((item, idx) => (
                          <div key={idx} className="p-3.5 flex flex-col sm:flex-row sm:items-center justify-between gap-2 hover:bg-slate-50">
                            <div className="flex items-center space-x-2">
                              <span className="font-bold text-slate-900">{item.ticker}</span>
                              <span className="px-2 py-0.5 bg-slate-100 text-slate-700 text-[10px] font-bold rounded">
                                SCREENED OUT
                              </span>
                            </div>
                            <div className="text-slate-600 text-xs sm:text-right">
                              {item.reason}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

            </div>
          )}

          {/* Initial Empty State (When no scanResult and not loading) */}
          {!scanResult && !loading && (
            <div className="bg-white border border-slate-200 rounded-2xl p-12 text-center shadow-xs">
              <div className="w-16 h-16 bg-gradient-to-tr from-emerald-100 to-teal-100 text-emerald-700 rounded-2xl flex items-center justify-center mx-auto mb-4 shadow-xs">
                <Crosshair size={32} />
              </div>
              <h2 className="text-lg font-black text-slate-900">Run Your Dual Consensus Sweep Across 509 Stocks</h2>
              <p className="text-xs sm:text-sm text-slate-500 max-w-lg mx-auto mt-1.5 leading-relaxed">
                Select your timeframe (15m Intraday or Daily Swing) and target universe above, then trigger a dual scan. Both Microsoft Qlib and Legacy AI evaluate instruments concurrently to discover high-conviction confluence.
              </p>
            </div>
          )}

          {/* ── PERSISTED TRADES & REAL-TIME LIFECYCLE TRACKING ──────────────── */}
          <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 border-b border-slate-100 pb-4">
              <div>
                <div className="flex items-center space-x-2">
                  <div className="p-1.5 bg-amber-500/10 text-amber-600 rounded-lg">
                    <Activity size={18} />
                  </div>
                  <h3 className="text-base font-black text-slate-900 tracking-tight">
                    Persisted Consensus Trades & Live Tracking
                  </h3>
                  <span className="px-2 py-0.5 bg-slate-100 text-slate-700 rounded-full text-[11px] font-bold font-mono">
                    {trackedTrades.length} Total
                  </span>
                </div>
                <p className="text-xs text-slate-500 mt-1">
                  Authoritative trade lifecycle tracking across All Manual & 15m Background Scans (SSOT: <code className="text-indigo-600 font-mono">consensus_trade_history</code>)
                </p>
              </div>

              <div className="flex items-center space-x-2">
                {/* Filter buttons */}
                {(() => {
                  const isTradeOpen = (t) => t.status === 'OPEN';
                  const openCount = trackedTrades.filter(isTradeOpen).length;
                  const closedCount = trackedTrades.filter(t => !isTradeOpen(t)).length;

                  return (
                    <div className="flex items-center space-x-1 bg-slate-100 p-1 rounded-xl text-xs font-bold">
                      <button
                        type="button"
                        onClick={() => setTrackedFilter('ALL')}
                        className={`px-3 py-1 rounded-lg transition-all ${
                          trackedFilter === 'ALL'
                            ? 'bg-white text-slate-900 shadow-xs'
                            : 'text-slate-500 hover:text-slate-900'
                        }`}
                      >
                        All ({trackedTrades.length})
                      </button>
                      <button
                        type="button"
                        onClick={() => setTrackedFilter('OPEN')}
                        className={`px-3 py-1 rounded-lg transition-all flex items-center space-x-1.5 ${
                          trackedFilter === 'OPEN'
                            ? 'bg-white text-emerald-700 shadow-xs'
                            : 'text-slate-500 hover:text-slate-900'
                        }`}
                      >
                        <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
                        <span>Open ({openCount})</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => setTrackedFilter('CLOSED')}
                        className={`px-3 py-1 rounded-lg transition-all ${
                          trackedFilter === 'CLOSED'
                            ? 'bg-white text-slate-900 shadow-xs'
                            : 'text-slate-500 hover:text-slate-900'
                        }`}
                      >
                        Closed ({closedCount})
                      </button>
                    </div>
                  );
                })()}

                {/* Refresh button */}
                <button
                  onClick={fetchTrackedTrades}
                  disabled={loadingTrackedTrades}
                  title="Refresh Tracked Trades & Live LTPs"
                  className="p-2 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-xl transition-all"
                >
                  <RefreshCw size={15} className={loadingTrackedTrades ? 'animate-spin' : ''} />
                </button>
              </div>
            </div>

            {/* Quick Metrics Bar */}
            {trackedTrades.length > 0 && (() => {
              const isTradeOpen = (t) => t.status === 'OPEN';
              const openCount = trackedTrades.filter(isTradeOpen).length;
              const closedList = trackedTrades.filter(t => !isTradeOpen(t));
              const wins = closedList.filter(t => Number(t.profit_pct ?? 0) > 0).length;
              const winRate = closedList.length > 0 ? `${((wins / closedList.length) * 100).toFixed(1)}%` : '—';
              const manualCount = trackedTrades.filter(t => t.source === 'MANUAL').length;
              const daemonCount = trackedTrades.length - manualCount;

              return (
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                  <div className="bg-slate-50 p-3 rounded-xl border border-slate-200/80">
                    <span className="text-[10px] font-bold text-slate-500 uppercase block">Active Positions</span>
                    <div className="flex items-center space-x-2 mt-0.5">
                      <span className="text-xl font-black text-slate-900 font-mono">
                        {openCount}
                      </span>
                      {openCount > 0 && (
                        <span className="px-1.5 py-0.5 bg-emerald-100 text-emerald-800 text-[10px] font-bold rounded">Live Tracking</span>
                      )}
                    </div>
                  </div>

                  <div className="bg-slate-50 p-3 rounded-xl border border-slate-200/80">
                    <span className="text-[10px] font-bold text-slate-500 uppercase block">Closed Trades</span>
                    <span className="text-xl font-black text-slate-900 font-mono mt-0.5 block">
                      {closedList.length}
                    </span>
                  </div>

                  <div className="bg-slate-50 p-3 rounded-xl border border-slate-200/80">
                    <span className="text-[10px] font-bold text-slate-500 uppercase block">Confluence Win Rate</span>
                    <span className="text-xl font-black text-emerald-700 font-mono mt-0.5 block">
                      {winRate}
                    </span>
                  </div>

                  <div className="bg-slate-50 p-3 rounded-xl border border-slate-200/80">
                    <span className="text-[10px] font-bold text-slate-500 uppercase block">Sources</span>
                    <div className="flex items-center space-x-1 mt-1 text-[11px] font-semibold text-slate-700">
                      <span className="px-1.5 py-0.5 bg-indigo-50 text-indigo-700 border border-indigo-200 rounded text-[10px]">
                        {manualCount} Manual
                      </span>
                      <span className="px-1.5 py-0.5 bg-amber-50 text-amber-700 border border-amber-200 rounded text-[10px]">
                        {daemonCount} Daemon
                      </span>
                    </div>
                  </div>
                </div>
              );
            })()}

            {/* Table of Tracked Trades */}
            {trackedTrades.length === 0 ? (
              <div className="p-8 text-center bg-slate-50 rounded-xl border border-slate-200 text-slate-500 space-y-2">
                <Info size={24} className="mx-auto text-slate-400" />
                <p className="text-xs font-semibold text-slate-700">No Persisted Consensus Trades Recorded Yet</p>
                <p className="text-xs text-slate-500 max-w-md mx-auto">
                  When a manual consensus scan identifies unanimous confluence, or when the 15-minute background daemon runs during market hours, trades are automatically persisted to <code className="text-indigo-600 font-mono">consensus_trade_history</code> and tracked here with real-time LTP and PnL.
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="bg-slate-50 text-slate-500 uppercase tracking-wider font-semibold border-b border-slate-200">
                    <tr>
                      <th className="py-2.5 px-3">Date / Time</th>
                      <th className="py-2.5 px-3">Ticker</th>
                      <th className="py-2.5 px-3">Strategy</th>
                      <th className="py-2.5 px-3">Direction</th>
                      <th className="py-2.5 px-3">Entry</th>
                      <th className="py-2.5 px-3">Current LTP / Exit</th>
                      <th className="py-2.5 px-3">SL / Target</th>
                      <th className="py-2.5 px-3">P&L (%)</th>
                      <th className="py-2.5 px-3">Status</th>
                      <th className="py-2.5 px-3">Score / Engines</th>
                      <th className="py-2.5 px-3">Source</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 font-mono">
                    {trackedTrades
                      .filter(t => {
                        const isTradeOpen = t.status === 'OPEN';
                        if (trackedFilter === 'OPEN') return isTradeOpen;
                        if (trackedFilter === 'CLOSED') return !isTradeOpen;
                        return true;
                      })
                      .map((t) => {
                        const isOpen = t.status === 'OPEN';
                        const displayPnl = t.profit_pct ?? t.unrealized_pnl_pct ?? 0;
                        const isWin = Number(displayPnl) > 0;
                        const isLoss = Number(displayPnl) < 0;
                        const isBull = t.direction === 'BUY' || t.direction === 'BULLISH';
                        const currentLtp = t.current_price ?? t.current_ltp ?? t.entry_price;
                        const statusVerdict = t.status === 'OPEN'
                          ? (t.outcome === 'SELECTED' ? 'OPEN (ARENA PICK)' : (t.outcome === 'REJECTED_LOWER_RANK' ? 'OPEN (ARENA BENCH)' : 'OPEN'))
                          : (t.outcome || 'CLOSED');
                        const cleanTs = (t.timestamp || '').replace('T', ' ').split('.')[0];
                        const [datePart, timePart] = cleanTs.includes(' ') ? cleanTs.split(' ') : ['', cleanTs];
                        const scoreVal = t.composite_score ?? t.score;

                        return (
                          <tr key={t.id} className="hover:bg-slate-50/80 transition-colors">
                            {/* Timestamp */}
                            <td className="py-2.5 px-3 text-slate-600 whitespace-nowrap">
                              <div className="font-semibold text-slate-800">
                                {timePart || cleanTs}
                              </div>
                              <div className="text-[10px] text-slate-400">
                                {datePart}
                              </div>
                            </td>

                            {/* Ticker */}
                            <td className="py-2.5 px-3">
                              <span className="font-black text-slate-900 text-xs">
                                {t.ticker || (t.symbol ? `${t.symbol}.NS` : '—')}
                              </span>
                            </td>

                            {/* Strategy */}
                            <td className="py-2.5 px-3">
                              <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                                t.strategy === 'INTRADAY' 
                                  ? 'bg-blue-50 text-blue-700 border border-blue-200' 
                                  : 'bg-purple-50 text-purple-700 border border-purple-200'
                              }`}>
                                {t.strategy === 'INTRADAY' ? '15m Intra' : 'Daily Swing'}
                              </span>
                            </td>

                            {/* Direction */}
                            <td className="py-2.5 px-3">
                              <span className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold ${
                                isBull 
                                  ? 'bg-emerald-100 text-emerald-800' 
                                  : 'bg-rose-100 text-rose-800'
                              }`}>
                                {isBull ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
                                <span>{isBull ? 'BUY' : 'SELL'}</span>
                              </span>
                            </td>

                            {/* Entry Price */}
                            <td className="py-2.5 px-3 text-slate-800 font-bold whitespace-nowrap">
                              ₹{t.entry_price ? Number(t.entry_price).toFixed(2) : '—'}
                            </td>

                            {/* Current LTP / Exit */}
                            <td className="py-2.5 px-3 whitespace-nowrap">
                              {isOpen ? (
                                <div className="flex items-center space-x-1.5">
                                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
                                  <span className="font-bold text-slate-900">
                                    ₹{currentLtp ? Number(currentLtp).toFixed(2) : '—'}
                                  </span>
                                </div>
                              ) : (
                                <span className="text-slate-700">
                                  ₹{t.exit_price ? Number(t.exit_price).toFixed(2) : (currentLtp ? Number(currentLtp).toFixed(2) : '—')}
                                </span>
                              )}
                            </td>

                            {/* SL / Target */}
                            <td className="py-2.5 px-3 text-[11px] whitespace-nowrap">
                              <div className="text-rose-600 font-medium">SL: ₹{t.stop_loss ? Number(t.stop_loss).toFixed(2) : '—'}</div>
                              <div className="text-emerald-600 font-medium">TP: ₹{t.target_price ? Number(t.target_price).toFixed(2) : '—'}</div>
                            </td>

                            {/* P&L */}
                            <td className={`py-2.5 px-3 font-bold whitespace-nowrap ${
                              isWin ? 'text-emerald-600' : isLoss ? 'text-rose-600' : 'text-slate-600'
                            }`}>
                              <div>{formatPnL(displayPnl)}</div>
                              {isOpen && (
                                <span className="text-[9px] font-semibold uppercase tracking-wider text-slate-400">
                                  Live MTM
                                </span>
                              )}
                            </td>

                            {/* Status */}
                            <td className="py-2.5 px-3 whitespace-nowrap">
                              <span className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold ${
                                statusVerdict === 'TARGET MET' ? 'bg-emerald-100 text-emerald-800 border border-emerald-300' :
                                statusVerdict === 'SL HIT' ? 'bg-rose-100 text-rose-800 border border-rose-300' :
                                statusVerdict === 'SQUARED OFF' ? 'bg-amber-100 text-amber-800 border border-amber-300' :
                                statusVerdict === 'OPEN (ARENA BENCH)' ? 'bg-slate-100 text-slate-700 border border-slate-300' :
                                statusVerdict === 'OPEN (ARENA PICK)' ? 'bg-purple-100 text-purple-800 border border-purple-300 font-extrabold' :
                                'bg-emerald-50 text-emerald-700 border border-emerald-200 animate-pulse'
                              }`}>
                                {statusVerdict === 'TARGET MET' && <CheckCircle2 size={10} />}
                                {statusVerdict === 'SL HIT' && <ShieldAlert size={10} />}
                                {statusVerdict === 'SQUARED OFF' && <Clock size={10} />}
                                {statusVerdict.startsWith('OPEN') && <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span>}
                                <span>{statusVerdict}</span>
                              </span>
                            </td>

                            {/* Score & Engines */}
                            <td className="py-2.5 px-3 whitespace-nowrap text-[11px]">
                              <div className="font-bold text-amber-700 flex items-center space-x-1.5">
                                <span>{scoreVal ? `${Number(scoreVal).toFixed(0)}% Confluence` : '—'}</span>
                                {t.meta_learner_v2 && (
                                  <span className={`text-[9px] px-1 py-0.2 rounded font-mono font-bold ${
                                    t.meta_learner_v2.meta_adjustment > 0 ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                                  }`}>
                                    Meta: {t.meta_learner_v2.final_conviction}%
                                  </span>
                                )}
                              </div>
                              <div className="text-[10px] text-slate-400 font-sans">
                                {t.qlib_score != null && `Qlib: ${Number(t.qlib_score).toFixed(3)} `}
                                {t.legacy_score != null && `Legacy: ${(Number(t.legacy_score) > 1 ? Number(t.legacy_score) : Number(t.legacy_score) * 100).toFixed(0)}%`}
                              </div>
                            </td>

                            {/* Source */}
                            <td className="py-2.5 px-3 whitespace-nowrap">
                              <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                                t.source === 'MANUAL'
                                  ? 'bg-slate-100 text-slate-700 border border-slate-300'
                                  : 'bg-indigo-50 text-indigo-700 border border-indigo-200'
                              }`}>
                                {t.source === 'MANUAL' ? 'MANUAL' : '15M DAEMON'}
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ==================================================================== */}
      {/* ── VIEW 2: MULTI-ENGINE PERFORMANCE COMPARISON & TRACKED TRADES ─── */}
      {/* ==================================================================== */}
      {mainTab === 'comparison' && (
        <div className="space-y-6">
          
          {/* Controls Bar */}
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 bg-white p-4 rounded-2xl border border-slate-200">
            <div className="flex items-center space-x-2">
              <LineChartIcon size={18} className="text-indigo-600" />
              <h2 className="text-sm font-black text-slate-900">
                Authoritative Multi-Engine Scorecard
              </h2>
              <span className="hidden md:inline-flex items-center space-x-1 px-2.5 py-0.5 bg-slate-100 text-slate-700 text-[10px] font-bold rounded-full border border-slate-200">
                {comparisonFilterMode === 'HIGHEST_CONFLUENCE_ONLY' ? (
                  <>
                    <Zap size={11} className="text-amber-500 fill-amber-500" />
                    <span>Top Score per Scan</span>
                  </>
                ) : comparisonFilterMode === 'TIMED_ANCHOR_CONFLUENCE' ? (
                  <>
                    <Clock size={11} className="text-amber-600" />
                    <span>Timed Institutional Anchors (09:30, 11:30, 13:30)</span>
                  </>
                ) : (
                  <span>All Qualified Setups</span>
                )}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              {/* Filter Mode Switch: All Trades vs Highest Confluence Only vs Timed Scans */}
              <div className="flex items-center bg-slate-100 p-1 rounded-xl border border-slate-200/80">
                <button
                  type="button"
                  onClick={() => handleSetComparisonFilterMode('ALL_QUALIFIED')}
                  className={`px-3 py-1 text-xs font-bold rounded-lg transition-all cursor-pointer ${
                    comparisonFilterMode === 'ALL_QUALIFIED'
                      ? 'bg-white text-slate-900 shadow-xs font-black'
                      : 'text-slate-500 hover:text-slate-900'
                  }`}
                  title="Include all qualified trades across each engine"
                >
                  All Trades
                </button>
                <button
                  type="button"
                  onClick={() => handleSetComparisonFilterMode('HIGHEST_CONFLUENCE_ONLY')}
                  className={`px-3 py-1 text-xs font-bold rounded-lg transition-all flex items-center space-x-1 cursor-pointer ${
                    comparisonFilterMode === 'HIGHEST_CONFLUENCE_ONLY'
                      ? 'bg-amber-400 text-slate-950 shadow-xs font-black'
                      : 'text-slate-500 hover:text-slate-900'
                  }`}
                  title="Filter to only the single highest confluence / score setup per scan sweep"
                >
                  <Zap size={12} className={comparisonFilterMode === 'HIGHEST_CONFLUENCE_ONLY' ? 'fill-slate-950 text-slate-950' : 'text-amber-500'} />
                  <span>Highest Confluence Only</span>
                </button>
                <button
                  type="button"
                  onClick={() => handleSetComparisonFilterMode('TIMED_ANCHOR_CONFLUENCE')}
                  className={`px-3 py-1 text-xs font-bold rounded-lg transition-all flex items-center space-x-1 cursor-pointer ${
                    comparisonFilterMode === 'TIMED_ANCHOR_CONFLUENCE'
                      ? 'bg-amber-400 text-slate-950 shadow-xs font-black'
                      : 'text-slate-500 hover:text-slate-900'
                  }`}
                  title="Filter to top setups from 09:30, 11:30 & 13:30 institutional anchor scans with mature market context"
                >
                  <Clock size={12} className={comparisonFilterMode === 'TIMED_ANCHOR_CONFLUENCE' ? 'text-slate-950' : 'text-amber-600'} />
                  <span>Timed Scans (High Confluence)</span>
                </button>
              </div>

              {/* Time Horizon Filter */}
              <div className="flex items-center space-x-1 bg-slate-100 p-1 rounded-xl">
                {[7, 30, 90].map((days) => (
                  <button
                    key={days}
                    onClick={() => {
                      setComparisonDays(days);
                      fetchComparisonData(days, comparisonFilterMode);
                    }}
                    className={`px-3 py-1 text-xs font-bold rounded-lg transition-all ${
                      comparisonDays === days
                        ? 'bg-white text-slate-900 shadow-xs'
                        : 'text-slate-500 hover:text-slate-900'
                    }`}
                  >
                    {days}D
                  </button>
                ))}
              </div>
              <button
                onClick={() => fetchComparisonData(comparisonDays, comparisonFilterMode)}
                disabled={loadingComparison}
                className="p-2 bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-xl transition-all"
                title="Refresh Comparison Data"
              >
                <RefreshCw size={15} className={loadingComparison ? 'animate-spin' : ''} />
              </button>
            </div>
          </div>

          {/* 3-Engine KPI Cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
            
            {/* 1. Consensus Confluence */}
            <div className="bg-gradient-to-br from-amber-500/10 via-amber-50/60 to-white border-2 border-amber-400/80 rounded-2xl p-5 shadow-xs relative overflow-hidden">
              <div className="flex items-center justify-between pb-3 border-b border-amber-200">
                <div className="flex items-center space-x-2">
                  <div className="p-2 bg-amber-500 text-white rounded-xl shadow-xs">
                    <Award size={18} />
                  </div>
                  <div>
                    <h3 className="text-sm font-black text-amber-950">Dual Consensus</h3>
                    <span className="text-[10px] font-black uppercase text-amber-700 bg-amber-100 px-2 py-0.5 rounded">
                      Confluence (Grade-A)
                    </span>
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 mt-4">
                <div className="bg-white/80 p-3 rounded-xl border border-amber-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Win Rate</span>
                  <span className="text-2xl font-black text-amber-900 font-mono">
                    {comparisonData?.summary?.consensus?.win_rate ?? 0.0}%
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-amber-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Total P&L</span>
                  <span className={`text-2xl font-black font-mono ${
                    (comparisonData?.summary?.consensus?.total_pnl || 0) >= 0 ? 'text-emerald-700' : 'text-rose-700'
                  }`}>
                    {formatPnL(comparisonData?.summary?.consensus?.total_pnl)}
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-amber-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Total Trades</span>
                  <span className="text-lg font-black text-slate-800 font-mono">
                    {comparisonData?.summary?.consensus?.total_trades ?? 0}
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-amber-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Profit Factor</span>
                  <span className="text-lg font-black text-slate-800 font-mono">
                    {comparisonData?.summary?.consensus?.profit_factor ?? 1.0}x
                  </span>
                </div>
              </div>

              <p className="text-[11px] text-amber-800/90 font-medium mt-3 bg-amber-100/50 p-2.5 rounded-xl border border-amber-200/60">
                ⭐ Zero Heat (NOT_A_POSITION). Triggered only when both Qlib & Legacy models reach unanimous directional agreement.
              </p>
            </div>

            {/* 2. Microsoft Qlib */}
            <div className="bg-gradient-to-br from-purple-500/10 via-purple-50/60 to-white border border-purple-300 rounded-2xl p-5 shadow-xs">
              <div className="flex items-center justify-between pb-3 border-b border-purple-200">
                <div className="flex items-center space-x-2">
                  <div className="p-2 bg-purple-600 text-white rounded-xl shadow-xs">
                    <Cpu size={18} />
                  </div>
                  <div>
                    <h3 className="text-sm font-black text-purple-950">Microsoft Qlib</h3>
                    <span className="text-[10px] font-black uppercase text-purple-700 bg-purple-100 px-2 py-0.5 rounded">
                      Alpha-158 GBDT
                    </span>
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 mt-4">
                <div className="bg-white/80 p-3 rounded-xl border border-purple-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Win Rate</span>
                  <span className="text-2xl font-black text-purple-900 font-mono">
                    {comparisonData?.summary?.qlib?.win_rate ?? 0.0}%
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-purple-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Total P&L</span>
                  <span className={`text-2xl font-black font-mono ${
                    (comparisonData?.summary?.qlib?.total_pnl || 0) >= 0 ? 'text-emerald-700' : 'text-rose-700'
                  }`}>
                    {formatPnL(comparisonData?.summary?.qlib?.total_pnl)}
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-purple-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Total Trades</span>
                  <span className="text-lg font-black text-slate-800 font-mono">
                    {comparisonData?.summary?.qlib?.total_trades ?? 0}
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-purple-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Profit Factor</span>
                  <span className="text-lg font-black text-slate-800 font-mono">
                    {comparisonData?.summary?.qlib?.profit_factor ?? 1.0}x
                  </span>
                </div>
              </div>

              <p className="text-[11px] text-purple-800/90 font-medium mt-3 bg-purple-100/50 p-2.5 rounded-xl border border-purple-200/60">
                Pure quantitative factors evaluated with LightGBM and custom Alpha-158 intraday cross-sectional ranking.
              </p>
            </div>

            {/* 3. Legacy Multi-Model AI */}
            <div className="bg-gradient-to-br from-indigo-500/10 via-indigo-50/60 to-white border border-indigo-300 rounded-2xl p-5 shadow-xs">
              <div className="flex items-center justify-between pb-3 border-b border-indigo-200">
                <div className="flex items-center space-x-2">
                  <div className="p-2 bg-indigo-600 text-white rounded-xl shadow-xs">
                    <Zap size={18} />
                  </div>
                  <div>
                    <h3 className="text-sm font-black text-indigo-950">Legacy Multi-Model</h3>
                    <span className="text-[10px] font-black uppercase text-indigo-700 bg-indigo-100 px-2 py-0.5 rounded">
                      Ensemble + FinBERT
                    </span>
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 mt-4">
                <div className="bg-white/80 p-3 rounded-xl border border-indigo-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Win Rate</span>
                  <span className="text-2xl font-black text-indigo-900 font-mono">
                    {comparisonData?.summary?.legacy?.win_rate ?? 0.0}%
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-indigo-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Total P&L</span>
                  <span className={`text-2xl font-black font-mono ${
                    (comparisonData?.summary?.legacy?.total_pnl || 0) >= 0 ? 'text-emerald-700' : 'text-rose-700'
                  }`}>
                    {formatPnL(comparisonData?.summary?.legacy?.total_pnl)}
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-indigo-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Total Trades</span>
                  <span className="text-lg font-black text-slate-800 font-mono">
                    {comparisonData?.summary?.legacy?.total_trades ?? 0}
                  </span>
                </div>
                <div className="bg-white/80 p-3 rounded-xl border border-indigo-200">
                  <span className="text-[10px] font-bold text-slate-500 uppercase block">Profit Factor</span>
                  <span className="text-lg font-black text-slate-800 font-mono">
                    {comparisonData?.summary?.legacy?.profit_factor ?? 1.0}x
                  </span>
                </div>
              </div>

              <p className="text-[11px] text-indigo-800/90 font-medium mt-3 bg-indigo-100/50 p-2.5 rounded-xl border border-indigo-200/60">
                Technical patterns combined with calibrated confidence curves and real FinBERT market sentiment sweeps.
              </p>
            </div>

          </div>

          {/* Cumulative Multi-Engine Timeline Chart */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
              <div>
                <div className="flex items-center space-x-2">
                  <h3 className="text-base font-black text-slate-900">
                    Multi-Engine Cumulative Return Curve (% P&L)
                  </h3>
                  {comparisonData?.timeline?.length > 0 && (
                    <span className="px-2 py-0.5 bg-emerald-50 text-emerald-800 border border-emerald-200 rounded-md text-[10px] font-black uppercase tracking-wider flex items-center space-x-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span>
                      <span>Updated: {comparisonData.timeline[comparisonData.timeline.length - 1].date}</span>
                    </span>
                  )}
                </div>
                <p className="text-xs text-slate-500 mt-0.5">
                  Authoritative cumulative equity curves plotted across identical trading sessions from real database records.
                </p>
                {comparisonData?.timeline?.length > 1 && (() => {
                  const t = comparisonData.timeline;
                  const cur = t[t.length - 1];
                  const prev = t[t.length - 2];
                  const cDelta = cur.consensus - prev.consensus;
                  const qDelta = cur.qlib - prev.qlib;
                  const lDelta = cur.legacy - prev.legacy;
                  return (
                    <div className="text-[11px] font-semibold text-slate-600 mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="text-slate-400 font-bold uppercase text-[10px]">Session Delta ({cur.date}):</span>
                      <span className={cDelta >= 0 ? 'text-amber-700 font-bold' : 'text-rose-700 font-bold'}>
                        Consensus {cDelta >= 0 ? `+${cDelta.toFixed(2)}%` : `${cDelta.toFixed(2)}%`}
                      </span>
                      <span className="text-slate-300">•</span>
                      <span className={qDelta >= 0 ? 'text-purple-700 font-bold' : 'text-rose-700 font-bold'}>
                        Qlib {qDelta >= 0 ? `+${qDelta.toFixed(2)}%` : `${qDelta.toFixed(2)}%`}
                      </span>
                      <span className="text-slate-300">•</span>
                      <span className={lDelta >= 0 ? 'text-indigo-700 font-bold' : 'text-rose-700 font-bold'}>
                        Legacy {lDelta >= 0 ? `+${lDelta.toFixed(2)}%` : `${lDelta.toFixed(2)}%`}
                      </span>
                    </div>
                  );
                })()}
              </div>

              {/* Chart Legend */}
              <div className="flex items-center space-x-4 text-xs font-bold">
                <div className="flex items-center space-x-1.5">
                  <span className="w-3 h-3 rounded-full bg-amber-500"></span>
                  <span className="text-amber-900">Consensus Confluence</span>
                </div>
                <div className="flex items-center space-x-1.5">
                  <span className="w-3 h-3 rounded-full bg-purple-600"></span>
                  <span className="text-purple-900">Microsoft Qlib</span>
                </div>
                <div className="flex items-center space-x-1.5">
                  <span className="w-3 h-3 rounded-full bg-indigo-600"></span>
                  <span className="text-indigo-900">Legacy Multi-Model</span>
                </div>
              </div>
            </div>

            {comparisonData?.timeline?.length > 0 ? (
              <div className="h-80 w-full pt-2">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={comparisonData.timeline} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                    <XAxis 
                      dataKey="date" 
                      tick={{ fontSize: 11, fill: '#64748b' }} 
                      tickLine={false} 
                    />
                    <YAxis 
                      tick={{ fontSize: 11, fill: '#64748b' }} 
                      tickLine={false}
                      unit="%" 
                    />
                    <Tooltip 
                      formatter={(val, name) => [`${Number(val).toFixed(2)}%`, name]}
                      contentStyle={{ backgroundColor: '#ffffff', borderColor: '#cbd5e1', borderRadius: '0.75rem', fontSize: '12px' }}
                    />
                    <Line 
                      type="monotone" 
                      dataKey="consensus" 
                      name="Dual Consensus" 
                      stroke="#d97706" 
                      strokeWidth={3} 
                      dot={{ r: 3, fill: '#d97706' }} 
                    />
                    <Line 
                      type="monotone" 
                      dataKey="qlib" 
                      name="Microsoft Qlib" 
                      stroke="#9333ea" 
                      strokeWidth={2} 
                      dot={false} 
                    />
                    <Line 
                      type="monotone" 
                      dataKey="legacy" 
                      name="Legacy AI" 
                      stroke="#4f46e5" 
                      strokeWidth={2} 
                      dot={false} 
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            ) : (
              <div className="h-64 flex items-center justify-center border border-dashed border-slate-200 rounded-xl text-xs text-slate-400">
                No closed trades found across selected timeline.
              </div>
            )}
          </div>

          {/* Tracked Consensus Trades Table */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <div className="flex items-center space-x-2">
                  <h3 className="text-base font-black text-slate-900">
                    Tracked Consensus Confluence Trades
                  </h3>
                  <span className="px-2 py-0.5 bg-amber-50 text-amber-800 text-[10px] font-black rounded-full border border-amber-200">
                    {comparisonFilterMode === 'HIGHEST_CONFLUENCE_ONLY'
                      ? 'Top Confluence / Scan'
                      : comparisonFilterMode === 'TIMED_ANCHOR_CONFLUENCE'
                      ? 'Timed Anchor Scans'
                      : 'All Scanned Setups'}
                  </span>
                </div>
                <p className="text-xs text-slate-500">
                  {comparisonFilterMode === 'HIGHEST_CONFLUENCE_ONLY'
                    ? "Displaying strictly the single highest confluence setup per scan sweep with real-time lifecycle tracking."
                    : comparisonFilterMode === 'TIMED_ANCHOR_CONFLUENCE'
                    ? "Displaying institutional milestone setups (09:30 ORB, 11:30 & 13:30 VWAP momentum) with real-time lifecycle tracking."
                    : "Real-time lifecycle reconciliation (Target Met, Stop Loss Hit, Square Off at 15:15 IST)"}
                </p>
              </div>
              <span className="px-3 py-1 bg-amber-100 text-amber-900 rounded-xl text-xs font-bold font-mono">
                {historyData?.total_trades || historyData?.trades?.length || 0} Recorded
              </span>
            </div>

            {(!historyData?.trades || historyData.trades.length === 0) ? (
              <div className="p-8 text-center bg-slate-50 rounded-xl border border-slate-200 text-slate-500">
                <Info size={24} className="mx-auto mb-2 text-slate-400" />
                <p className="text-xs font-medium">
                  No consensus trades recorded yet. When the 15-minute background daemon identifies unanimous confluence during active market hours (09:15–15:30 IST), trades are automatically stored in <code className="text-indigo-600">consensus_trade_history</code>, alerted via Telegram, and tracked against 15m candle bars.
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="bg-slate-50 text-slate-500 uppercase tracking-wider font-semibold border-b border-slate-200">
                    <tr>
                      <th className="py-2.5 px-3">Date / Time</th>
                      <th className="py-2.5 px-3">Ticker</th>
                      <th className="py-2.5 px-3">Direction</th>
                      <th className="py-2.5 px-3">Entry</th>
                      <th className="py-2.5 px-3">Exit</th>
                      <th className="py-2.5 px-3">P&L (%)</th>
                      <th className="py-2.5 px-3">Outcome</th>
                      <th className="py-2.5 px-3">Score</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 font-mono">
                    {historyData.trades.map((t) => (
                      <tr key={t.id} className="hover:bg-slate-50/80">
                        <td className="py-2.5 px-3 text-slate-600">{t.timestamp}</td>
                        <td className="py-2.5 px-3 font-bold text-slate-900">{t.ticker}</td>
                        <td className="py-2.5 px-3">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                            t.direction === 'BUY' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                          }`}>
                            {t.direction}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 text-slate-800">₹{t.entry_price}</td>
                        <td className="py-2.5 px-3 text-slate-800">₹{t.exit_price || '—'}</td>
                        <td className={`py-2.5 px-3 font-bold ${
                          (t.profit_pct || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'
                        }`}>
                          {formatPnL(t.profit_pct)}
                        </td>
                        <td className="py-2.5 px-3">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                            t.outcome === 'TARGET MET' ? 'bg-emerald-100 text-emerald-800' :
                            t.outcome === 'SL HIT' ? 'bg-rose-100 text-rose-800' :
                            'bg-slate-100 text-slate-700'
                          }`}>
                            {t.outcome || 'OPEN'}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 text-slate-600">{t.score}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

        </div>
      )}

      {/* ==================================================================== */}
      {/* ── VIEW 3: CONSENSUS CAPITAL ARENA (GAMIFIED PAPER TRADING) ─────── */}
      {/* ==================================================================== */}
      {mainTab === 'arena' && (
        <ConsensusGameArena />
      )}

    </div>
  );
}
