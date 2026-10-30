import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  Trophy,
  Award,
  TrendingUp,
  TrendingDown,
  DollarSign,
  Wallet,
  Activity,
  Calendar as CalendarIcon,
  Play,
  Pause,
  RotateCcw,
  FastForward,
  Shield,
  CheckCircle,
  XCircle,
  Clock,
  ChevronLeft,
  ChevronRight,
  Eye,
  RefreshCw,
  AlertTriangle,
  Layers,
  Sparkles,
  ArrowUpRight,
  ArrowDownRight,
  Sliders,
  Check,
  Zap,
  Info,
  HelpCircle,
  LogOut,
  ShieldCheck,
  Bot
} from 'lucide-react';
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  CartesianGrid
} from 'recharts';
import { API_BASE } from '../services/api';

export default function ConsensusGameArena() {
  // State Management
  const [portfolio, setPortfolio] = useState(null);
  const [equityCurve, setEquityCurve] = useState([]);
  const [calendarData, setCalendarData] = useState(null);
  const [shieldData, setShieldData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  // Modals
  const [deployModalOpen, setDeployModalOpen] = useState(false);
  const [resetModalOpen, setResetModalOpen] = useState(false);
  const [dayInspectorOpen, setDayInspectorOpen] = useState(false);
  const [howItWorksOpen, setHowItWorksOpen] = useState(false);
  const [selectedDay, setSelectedDay] = useState(null);

  // Deploy Capital Form State (persisted in localStorage)
  const [customCapital, setCustomCapital] = useState(() => {
    return Number(localStorage.getItem('consensus_arena_capital')) || 100000;
  });
  const [allocationMode, setAllocationMode] = useState(() => {
    return localStorage.getItem('consensus_arena_alloc_mode') || 'EQUAL_WEIGHT';
  });
  const [maxPositions, setMaxPositions] = useState(() => {
    return Number(localStorage.getItem('consensus_arena_max_pos')) || 5;
  });
  const [feesEnabled, setFeesEnabled] = useState(() => {
    const saved = localStorage.getItem('consensus_arena_fees');
    return saved !== null ? saved === 'true' : true;
  });
  const [tradeSelectionMode, setTradeSelectionMode] = useState(() => {
    return localStorage.getItem('consensus_arena_selection_mode') || 'ALL_QUALIFIED';
  });
  const [deploying, setDeploying] = useState(false);
  const [exitingId, setExitingId] = useState(null);
  const [exitingAll, setExitingAll] = useState(false);

  // Replay Mode State
  const [replayModalOpen, setReplayModalOpen] = useState(false);
  const [replayData, setReplayData] = useState(null);
  const [replayIndex, setReplayIndex] = useState(0);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [replaySpeed, setReplaySpeed] = useState(1); // 1x, 5x, 10x
  // Ledger Filter
  const [ledgerFilter, setLedgerFilter] = useState('ALL'); // 'ALL' | 'WIN' | 'LOSS'

  // Live Price Tick Animation & Active Section State
  const prevPricesRef = useRef({});
  const replayTimerRef = useRef(null);
  const [priceFlashes, setPriceFlashes] = useState({}); // { [ticker]: 'up' | 'down' }
  const activeHoldingsRef = useRef(null);

  // Persist form preferences in localStorage
  useEffect(() => {
    localStorage.setItem('consensus_arena_capital', customCapital.toString());
    localStorage.setItem('consensus_arena_alloc_mode', allocationMode);
    localStorage.setItem('consensus_arena_max_pos', maxPositions.toString());
    localStorage.setItem('consensus_arena_fees', feesEnabled.toString());
    localStorage.setItem('consensus_arena_selection_mode', tradeSelectionMode);
  }, [customCapital, allocationMode, maxPositions, feesEnabled, tradeSelectionMode]);

  // Synchronize trade selection mode from portfolio state if present
  useEffect(() => {
    if (portfolio?.trade_selection_mode) {
      setTradeSelectionMode(portfolio.trade_selection_mode);
      localStorage.setItem('consensus_arena_selection_mode', portfolio.trade_selection_mode);
    }
  }, [portfolio?.trade_selection_mode]);

  // Fetch all initial game data from backend
  const fetchAllGameData = async (showRefresh = false) => {
    if (showRefresh) setRefreshing(true);
    try {
      const [portRes, eqRes, calRes, shieldRes] = await Promise.all([
        fetch(`${API_BASE}/consensus/game/portfolio`),
        fetch(`${API_BASE}/consensus/game/equity-curve`),
        fetch(`${API_BASE}/consensus/game/calendar`),
        fetch(`${API_BASE}/consensus/game/upstox-shield`)
      ]);

      if (portRes.ok) {
        const pData = await portRes.json();
        setPortfolio(pData.data);
      }
      if (eqRes.ok) {
        const eData = await eqRes.json();
        setEquityCurve(eData.data || []);
      }
      if (calRes.ok) {
        const cData = await calRes.json();
        setCalendarData(cData.data);
      }
      if (shieldRes.ok) {
        const sData = await shieldRes.json();
        setShieldData(sData.shield);
      }
    } catch (err) {
      console.error('Failed to load consensus arena data:', err);
    } finally {
      setLoading(false);
      if (showRefresh) setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchAllGameData();
    // Auto-refresh every 15 seconds to keep MTM and Upstox shield updated
    const interval = setInterval(() => {
      fetchAllGameData();
    }, 15000);
    return () => clearInterval(interval);
  }, []);

  // Live Price Tick Flash Animation Tracker
  useEffect(() => {
    if (!portfolio?.open_positions) return;
    const newFlashes = {};
    portfolio.open_positions.forEach(pos => {
      const prev = prevPricesRef.current[pos.ticker];
      const cur = pos.current_price;
      if (prev !== undefined && cur !== undefined && cur !== prev) {
        newFlashes[pos.ticker] = cur > prev ? 'up' : 'down';
      }
      prevPricesRef.current[pos.ticker] = cur;
    });

    if (Object.keys(newFlashes).length > 0) {
      setPriceFlashes(prev => ({ ...prev, ...newFlashes }));
      const timer = setTimeout(() => {
        setPriceFlashes({});
      }, 1800);
      return () => clearTimeout(timer);
    }
  }, [portfolio?.open_positions]);

  // Toggle Trade Selection Mode: ALL_QUALIFIED vs HIGHEST_CONFLUENCE_ONLY
  const handleSetTradeSelectionMode = async (newMode) => {
    setTradeSelectionMode(newMode);
    localStorage.setItem('consensus_arena_selection_mode', newMode);
    setRefreshing(true);
    try {
      const res = await fetch(`${API_BASE}/consensus/game/selection-mode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: 'default_arena_session',
          trade_selection_mode: newMode
        })
      });
      if (res.ok) {
        await fetchAllGameData(true);
      }
    } catch (err) {
      console.error('Failed to update trade selection mode:', err);
    } finally {
      setRefreshing(false);
    }
  };

  // Exit / Square Off Single Position Action
  const handleExitPosition = async (positionId, ticker) => {
    if (!window.confirm(`Are you sure you want to square off ${ticker.replace('.NS', '')} at current live market price?`)) return;
    setExitingId(positionId);
    try {
      const res = await fetch(`${API_BASE}/consensus/game/exit-position`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          position_id: positionId,
          session_id: 'default_arena_session',
          reason: 'MANUAL EXIT'
        })
      });
      if (res.ok) {
        await fetchAllGameData(true);
      }
    } catch (err) {
      console.error('Failed to exit position:', err);
    } finally {
      setExitingId(null);
    }
  };

  // Exit / Square Off All Open Positions Action
  const handleExitAll = async () => {
    if (!window.confirm('Are you sure you want to square off ALL active positions in the arena at current live market prices?')) return;
    setExitingAll(true);
    try {
      const res = await fetch(`${API_BASE}/consensus/game/exit-all`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: 'default_arena_session',
          reason: 'MANUAL EXIT ALL'
        })
      });
      if (res.ok) {
        await fetchAllGameData(true);
      }
    } catch (err) {
      console.error('Failed to exit all positions:', err);
    } finally {
      setExitingAll(false);
    }
  };

  // Deploy / Start Anew Capital Action
  const handleDeployCapital = async (resetToDefault = false) => {
    setDeploying(true);
    const targetCapital = resetToDefault ? 100000 : parseFloat(customCapital);
    try {
      const res = await fetch(`${API_BASE}/consensus/game/deploy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          initial_capital: targetCapital,
          allocation_mode: allocationMode,
          max_positions: parseInt(maxPositions),
          fees_enabled: feesEnabled,
          session_id: 'default_arena_session',
          account_name: 'Consensus Arena Trader',
          trade_selection_mode: tradeSelectionMode
        })
      });
      if (res.ok) {
        setDeployModalOpen(false);
        setResetModalOpen(false);
        if (resetToDefault) setCustomCapital(100000);
        await fetchAllGameData(true);
      }
    } catch (err) {
      console.error('Deploy capital error:', err);
    } finally {
      setDeploying(false);
    }
  };

  // Launch Historical Day Replayer
  const handleOpenReplay = async (dateStr) => {
    try {
      setReplayPlaying(false);
      setReplayIndex(0);
      const res = await fetch(`${API_BASE}/consensus/game/day-replay?date=${dateStr}`);
      if (res.ok) {
        const data = await res.json();
        setReplayData(data.data);
        setReplayModalOpen(true);
      }
    } catch (err) {
      console.error('Failed to load day replay:', err);
    }
  };

  // Replay playback timer loop
  useEffect(() => {
    if (replayPlaying && replayData?.timeline?.length) {
      const intervalMs = Math.max(50, Math.floor(1000 / replaySpeed));
      replayTimerRef.current = setInterval(() => {
        setReplayIndex((prev) => {
          if (prev >= replayData.timeline.length - 1) {
            setReplayPlaying(false);
            return prev;
          }
          return prev + 1;
        });
      }, intervalMs);
    } else {
      clearInterval(replayTimerRef.current);
    }
    return () => clearInterval(replayTimerRef.current);
  }, [replayPlaying, replaySpeed, replayData]);

  // Dynamic Weekday Matrix (Mon-Fri) that automatically grows as days pass
  const septWeekdays = useMemo(() => {
    const dateMap = new Map();
    const now = new Date();
    const curYear = now.getFullYear();
    const curMonth = now.getMonth(); // 0-indexed

    // 1. Generate all Monday-Friday weekdays for current month
    const totalDaysInMonth = new Date(curYear, curMonth + 1, 0).getDate();
    for (let dayNum = 1; dayNum <= totalDaysInMonth; dayNum++) {
      const d = new Date(curYear, curMonth, dayNum);
      const dow = d.getDay(); // 0 is Sun, 6 is Sat
      if (dow >= 1 && dow <= 5) {
        const dateStr = `${curYear}-${String(curMonth + 1).padStart(2, '0')}-${String(dayNum).padStart(2, '0')}`;
        const monthShort = d.toLocaleString('en-US', { month: 'short' });
        dateMap.set(dateStr, {
          day: dayNum,
          date: dateStr,
          monthName: monthShort
        });
      }
    }

    // 2. Also ensure every date present in calendarData is included (e.g. historical trades or cross-month)
    if (calendarData?.days) {
      calendarData.days.forEach((cd) => {
        if (cd.date && !dateMap.has(cd.date)) {
          const parts = cd.date.split('-');
          if (parts.length === 3) {
            const y = parseInt(parts[0], 10);
            const m = parseInt(parts[1], 10);
            const dNum = parseInt(parts[2], 10);
            const dObj = new Date(y, m - 1, dNum);
            const monthShort = dObj.toLocaleString('en-US', { month: 'short' });
            dateMap.set(cd.date, {
              day: dNum,
              date: cd.date,
              monthName: monthShort
            });
          }
        }
      });
    }

    // 3. Ensure today's date is always included
    const todayStr = `${curYear}-${String(curMonth + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    if (!dateMap.has(todayStr)) {
      dateMap.set(todayStr, {
        day: now.getDate(),
        date: todayStr,
        monthName: now.toLocaleString('en-US', { month: 'short' })
      });
    }

    // Return chronologically sorted weekday items
    return Array.from(dateMap.values()).sort((a, b) => a.date.localeCompare(b.date));
  }, [calendarData]);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center p-24 space-y-4">
        <div className="w-12 h-12 border-4 border-amber-500 border-t-transparent rounded-full animate-spin" />
        <p className="text-sm font-bold text-slate-600 tracking-wide uppercase">
          Initializing Consensus Capital Arena...
        </p>
      </div>
    );
  }

  const p = portfolio || {};
  const isPositive = (p.net_pnl || 0) >= 0;
  const initialCap = p.initial_capital || 100000;
  const currentEquity = p.total_equity || initialCap;
  const currentReplayPoint = replayData?.timeline?.[replayIndex] || null;

  // Quick lookup for active day
  const sep18Record = (calendarData?.days || []).find((d) => d.date === '2026-09-18');

  return (
    <div className="space-y-6">
      {/* ── TOP HERO CONTROL BAR ────────────────────────────────────────────── */}
      <div className="bg-gradient-to-r from-slate-900 via-indigo-950 to-slate-900 border border-slate-800 rounded-3xl p-6 text-white shadow-xl relative overflow-hidden">
        {/* Ambient glow */}
        <div className="absolute top-0 right-0 w-96 h-96 bg-amber-500/10 rounded-full blur-3xl pointer-events-none" />
        <div className="absolute bottom-0 left-1/3 w-96 h-96 bg-indigo-500/10 rounded-full blur-3xl pointer-events-none" />

        <div className="relative z-10 flex flex-col lg:flex-row items-start lg:items-center justify-between gap-6">
          {/* Title & Status */}
          <div className="space-y-2">
            <div className="flex items-center space-x-3">
              <span className="p-2.5 bg-amber-500/20 border border-amber-500/40 rounded-2xl text-amber-400">
                <Trophy size={26} />
              </span>
              <div>
                <div className="flex items-center space-x-2.5">
                  <h1 className="text-2xl font-black tracking-tight">Consensus Capital Arena</h1>
                  <span className="px-2.5 py-0.5 bg-amber-400 text-amber-950 font-black text-xs rounded-full uppercase tracking-wider">
                    Paper Trading Game
                  </span>
                  <span className="px-2.5 py-0.5 bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-bold text-xs rounded-full">
                    Read-Only Sandbox
                  </span>
                </div>
                <p className="text-xs font-semibold text-slate-300 mt-0.5">
                  Deploy simulated capital into live dual-engine consensus signals with authentic whole-share money management and zero fake data.
                </p>
              </div>
            </div>
          </div>

          {/* Action Buttons & Upstox Shield */}
          <div className="flex flex-wrap items-center gap-3">
            {/* Trade Selection Mode Switch: All Trades vs Highest Confluence Only vs Timed Scans */}
            <div className="flex items-center bg-slate-800/90 border border-slate-700/80 p-1 rounded-2xl shadow-inner backdrop-blur-md">
              <button
                type="button"
                onClick={() => handleSetTradeSelectionMode('ALL_QUALIFIED')}
                className={`px-3 py-1.5 rounded-xl text-xs font-bold transition-all cursor-pointer ${
                  tradeSelectionMode === 'ALL_QUALIFIED'
                    ? 'bg-amber-400 text-slate-950 font-black shadow-md'
                    : 'text-slate-400 hover:text-white'
                }`}
                title="Queue all unanimous consensus setups from each scan up to slot capacity"
              >
                All Trades
              </button>
              <button
                type="button"
                onClick={() => handleSetTradeSelectionMode('HIGHEST_CONFLUENCE_ONLY')}
                className={`px-3 py-1.5 rounded-xl text-xs font-bold transition-all flex items-center space-x-1.5 cursor-pointer ${
                  tradeSelectionMode === 'HIGHEST_CONFLUENCE_ONLY'
                    ? 'bg-amber-400 text-slate-950 font-black shadow-md'
                    : 'text-slate-400 hover:text-white'
                }`}
                title="Exclusively trade the single highest confluence score setup per scan sweep, rejecting all others"
              >
                <Zap size={13} className={tradeSelectionMode === 'HIGHEST_CONFLUENCE_ONLY' ? 'fill-slate-950 text-slate-950' : 'text-amber-400'} />
                <span>Highest Confluence Only</span>
              </button>
              <button
                type="button"
                onClick={() => handleSetTradeSelectionMode('TIMED_ANCHOR_CONFLUENCE')}
                className={`px-3 py-1.5 rounded-xl text-xs font-bold transition-all flex items-center space-x-1.5 cursor-pointer ${
                  tradeSelectionMode === 'TIMED_ANCHOR_CONFLUENCE'
                    ? 'bg-amber-400 text-slate-950 font-black shadow-md'
                    : 'text-slate-400 hover:text-white'
                }`}
                title="Only trade the single highest confluence setup from institutional anchor scans (09:30, 11:30, 1:30) with mature volume and broader market context"
              >
                <Clock size={13} className={tradeSelectionMode === 'TIMED_ANCHOR_CONFLUENCE' ? 'text-slate-950' : 'text-amber-400'} />
                <span>Timed Scans (High Confluence)</span>
              </button>
            </div>

            {/* How Capital is Deployed Explainer Button */}
            <button
              type="button"
              onClick={() => setHowItWorksOpen(true)}
              className="flex items-center space-x-1.5 bg-slate-800/90 hover:bg-slate-700 border border-slate-700/80 px-3.5 py-2 rounded-2xl text-xs font-bold text-amber-300 transition-all"
            >
              <HelpCircle size={15} />
              <span>How Capital Works</span>
            </button>

            {/* Upstox Rate Limit Shield Status */}
            <div className="flex items-center space-x-2 bg-slate-800/90 border border-slate-700/80 px-3.5 py-2 rounded-2xl text-xs backdrop-blur-md">
              <Shield size={16} className={shieldData?.safe ? 'text-emerald-400' : 'text-amber-400'} />
              <div>
                <div className="flex items-center space-x-1.5 font-bold">
                  <span className="text-slate-300">Upstox Shield:</span>
                  <span className={shieldData?.safe ? 'text-emerald-400' : 'text-amber-400'}>
                    {shieldData?.safe ? 'SAFE (100% Budget)' : 'CIRCUIT ACTIVE'}
                  </span>
                </div>
                <div className="text-[10px] text-slate-400">
                  {shieldData?.requests_last_sec || 0} req/s (Cap: 8/s) • Zero API Calls for Replay
                </div>
              </div>
            </div>

            {/* Refresh Button */}
            <button
              type="button"
              onClick={() => fetchAllGameData(true)}
              disabled={refreshing}
              className="p-2.5 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-2xl text-slate-300 hover:text-white transition-all"
              title="Refresh Arena Data"
            >
              <RefreshCw size={17} className={refreshing ? 'animate-spin text-amber-400' : ''} />
            </button>

            {/* Reset / Start Anew Button */}
            <button
              type="button"
              onClick={() => setResetModalOpen(true)}
              className="flex items-center space-x-1.5 bg-slate-800 hover:bg-rose-950/40 border border-slate-700 hover:border-rose-500/50 text-slate-300 hover:text-rose-300 px-3.5 py-2.5 rounded-2xl text-xs font-black uppercase tracking-wider transition-all"
              title="Reset Sandbox and Start Anew"
            >
              <RotateCcw size={15} />
              <span>Start Anew</span>
            </button>

            {/* Deploy / Configure Capital Button */}
            <button
              type="button"
              onClick={() => setDeployModalOpen(true)}
              className="flex items-center space-x-2 bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-slate-950 font-black text-xs uppercase tracking-wider px-5 py-2.5 rounded-2xl shadow-lg shadow-amber-500/20 transition-all active:scale-95"
            >
              <Wallet size={16} />
              <span>Deploy Capital (₹{initialCap.toLocaleString('en-IN')})</span>
            </button>
          </div>
        </div>

        {/* Gamer Profile Level & XP Bar */}
        <div className="mt-6 pt-5 border-t border-slate-800/80 grid grid-cols-1 md:grid-cols-3 gap-4 items-center">
          {/* Level Info */}
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-2xl bg-amber-500/20 border border-amber-500/40 flex items-center justify-center text-amber-300 font-black text-base">
              L{p.gamer_profile?.level || 1}
            </div>
            <div>
              <div className="text-xs font-black text-amber-400 uppercase tracking-wider">
                {p.gamer_profile?.level_title || 'Consensus Tactician'}
              </div>
              <div className="text-[11px] text-slate-400">
                {p.gamer_profile?.xp || 0} XP Earned • {p.gamer_profile?.next_level_xp || 500} XP Next
              </div>
            </div>
          </div>

          {/* XP Progress Bar */}
          <div className="space-y-1.5">
            <div className="flex justify-between text-[11px] font-bold text-slate-400">
              <span>Arena Progression</span>
              <span>{Math.round(((p.gamer_profile?.xp || 0) % 500) / 5)}%</span>
            </div>
            <div className="w-full bg-slate-800 rounded-full h-2 overflow-hidden">
              <div
                className="bg-gradient-to-r from-amber-500 to-emerald-400 h-2 rounded-full transition-all duration-500"
                style={{
                  width: `${Math.min(100, Math.max(5, ((p.gamer_profile?.xp || 0) % 500) / 5))}%`
                }}
              />
            </div>
          </div>

          {/* Badges Earned */}
          <div className="flex items-center justify-end space-x-2">
            {(p.gamer_profile?.badges || []).map((b) => (
              <div
                key={b.id}
                title={`${b.name}: ${b.desc}`}
                className="flex items-center space-x-1.5 px-2.5 py-1 bg-slate-800/90 border border-slate-700/80 rounded-xl text-xs cursor-help"
              >
                <span>{b.icon}</span>
                <span className="font-bold text-slate-300 text-[11px]">{b.name}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── HERO KPI GRID ─────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7 gap-4">
        {/* 1. Net Portfolio Value */}
        <div className="bg-white border border-slate-200/90 rounded-2xl p-4 shadow-sm">
          <div className="text-[11px] font-bold uppercase tracking-wider text-slate-500 mb-1">
            Total Portfolio
          </div>
          <div className="text-xl font-black text-slate-900 tracking-tight">
            ₹{currentEquity.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <div className={`flex items-center space-x-1 text-xs font-black mt-1 ${isPositive ? 'text-emerald-600' : 'text-rose-600'}`}>
            {isPositive ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}
            <span>{p.return_pct > 0 ? `+${p.return_pct}%` : `${p.return_pct}%`}</span>
            <span className="text-slate-400 font-normal ml-0.5">overall</span>
          </div>
        </div>

        {/* 2. Today's Profit / Loss Widget */}
        <div className="bg-gradient-to-br from-amber-500/10 via-white to-white border-2 border-amber-400/90 rounded-2xl p-4 shadow-sm relative overflow-hidden">
          <div className="flex items-center justify-between text-[11px] font-black uppercase tracking-wider text-amber-950 mb-1">
            <span className="flex items-center space-x-1">
              <Clock size={12} className="text-amber-600" />
              <span>Today's P&L</span>
            </span>
            <span className="text-[9px] font-black uppercase px-1.5 py-0.5 bg-amber-100 text-amber-800 rounded">
              Day
            </span>
          </div>
          <div className={`text-xl font-black tracking-tight ${(p.today_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
            {(p.today_pnl || 0) >= 0
              ? `+₹${(p.today_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
              : `-₹${Math.abs(p.today_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          </div>
          <div className="text-[11px] mt-1 font-semibold flex items-center justify-between">
            <span className={`font-black ${(p.today_return_pct || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
              {(p.today_return_pct || 0) >= 0 ? `+${p.today_return_pct || 0}%` : `${p.today_return_pct || 0}%`}
            </span>
            <span className="text-[10px] text-slate-500 truncate" title={`Booked: ₹${(p.today_realized_pnl || 0).toFixed(2)} | Floating MTM: ₹${(p.unrealized_pnl || 0).toFixed(2)}`}>
              Booked: {(p.today_realized_pnl || 0) >= 0 ? `+₹${(p.today_realized_pnl || 0).toFixed(0)}` : `-₹${Math.abs(p.today_realized_pnl || 0).toFixed(0)}`} • MTM: {(p.unrealized_pnl || 0) >= 0 ? `+₹${(p.unrealized_pnl || 0).toFixed(0)}` : `-₹${Math.abs(p.unrealized_pnl || 0).toFixed(0)}`}
            </span>
          </div>
        </div>

        {/* 3. Available Cash */}
        <div className="bg-white border border-slate-200/90 rounded-2xl p-4 shadow-sm">
          <div className="text-[11px] font-bold uppercase tracking-wider text-slate-500 mb-1">
            Available Cash
          </div>
          <div className="text-xl font-black text-slate-900 tracking-tight">
            ₹{(p.cash || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <div className="text-[11px] text-slate-500 mt-1 font-semibold">
            {p.open_count || 0} / {p.max_positions || 5} active slots used
          </div>
        </div>

        {/* 4. Unrealized MTM */}
        <div className="bg-white border border-slate-200/90 rounded-2xl p-4 shadow-sm">
          <div className="text-[11px] font-bold uppercase tracking-wider text-slate-500 mb-1">
            Unrealized MTM
          </div>
          <div className={`text-xl font-black tracking-tight ${(p.unrealized_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
            {(p.unrealized_pnl || 0) >= 0 ? `+₹${(p.unrealized_pnl || 0).toFixed(2)}` : `-₹${Math.abs(p.unrealized_pnl || 0).toFixed(2)}`}
          </div>
          <div className="text-[11px] text-slate-500 mt-1 font-semibold">
            Live Tick Valuations ({p.open_count || 0} open)
          </div>
        </div>

        {/* 5. Realized P&L */}
        <div className="bg-white border border-slate-200/90 rounded-2xl p-4 shadow-sm">
          <div className="text-[11px] font-bold uppercase tracking-wider text-slate-500 mb-1">
            All-Time Realized P&L
          </div>
          <div className={`text-xl font-black tracking-tight ${(p.realized_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
            {(p.realized_pnl || 0) >= 0 ? `+₹${(p.realized_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : `-₹${Math.abs(p.realized_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          </div>
          <div className="text-[11px] text-slate-500 mt-1 font-semibold">
            Fees: ₹{(p.total_friction || calendarData?.total_fees || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} deducted
          </div>
        </div>

        {/* 6. Win Rate */}
        <div className="bg-white border border-slate-200/90 rounded-2xl p-4 shadow-sm">
          <div className="flex items-center justify-between text-[11px] font-bold uppercase tracking-wider text-slate-500 mb-1">
            <span>Arena Win Rate</span>
            <span className="text-[10px] font-semibold text-slate-400">Signal: {p.gross_win_rate || p.win_rate || 0}%</span>
          </div>
          <div className="text-xl font-black text-slate-900 tracking-tight flex items-baseline space-x-1.5">
            <span>{p.gross_win_rate || p.win_rate || 0}%</span>
            {p.net_win_rate !== undefined && (
              <span className="text-xs font-bold text-slate-500">
                ({p.net_win_rate}% net)
              </span>
            )}
          </div>
          <div className="text-[11px] text-slate-500 mt-1 font-semibold">
            {p.wins || 0} Wins • {p.losses || 0} Losses {p.net_wins !== undefined ? `(${p.net_wins}W net)` : ''}
          </div>
        </div>

        {/* 7. Drawdown & Streak */}
        <div className="bg-white border border-slate-200/90 rounded-2xl p-4 shadow-sm">
          <div className="text-[11px] font-bold uppercase tracking-wider text-slate-500 mb-1">
            Max Drawdown
          </div>
          <div className="text-xl font-black text-rose-600 tracking-tight">
            {p.max_drawdown_pct || 0}%
          </div>
          <div className="text-[11px] text-slate-500 mt-1 font-semibold flex items-center space-x-1">
            <span>Streak:</span>
            <span className="font-bold text-amber-600">
              {p.streak > 0 ? `+${p.streak} Wins 🔥` : `${p.streak} Losses`}
            </span>
          </div>
        </div>
      </div>

      {/* ── HIGH-RESOLUTION EQUITY GRAPH ──────────────────────────────────── */}
      <div className="bg-white border border-slate-200/90 rounded-3xl p-6 shadow-sm">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 mb-4">
          <div>
            <div className="flex items-center space-x-2">
              <TrendingUp className="text-amber-600" size={20} />
              <h2 className="text-lg font-black text-slate-900">Portfolio Equity Curve</h2>
              {/* Mode label */}
              <span className={`text-[10px] font-black px-2 py-0.5 rounded-full uppercase tracking-wider ${
                p.trade_selection_mode === 'TIMED_ANCHOR_CONFLUENCE'
                  ? 'bg-amber-100 text-amber-900 border border-amber-300'
                  : p.trade_selection_mode === 'HIGHEST_CONFLUENCE_ONLY'
                  ? 'bg-amber-100 text-amber-800'
                  : 'bg-slate-100 text-slate-600'
              }`}>
                {p.trade_selection_mode === 'TIMED_ANCHOR_CONFLUENCE'
                  ? '🕒 Timed Scans (09:30, 11:30, 1:30)'
                  : p.trade_selection_mode === 'HIGHEST_CONFLUENCE_ONLY'
                  ? '⚡ Highest Confluence'
                  : 'All Trades'}
              </span>
            </div>
            <p className="text-xs font-semibold text-slate-500 mt-0.5">
              Trade-by-trade portfolio valuation with high-water mark and drawdown from peak.
            </p>
          </div>

          {/* Legend */}
          <div className="flex items-center flex-wrap gap-x-4 gap-y-1 text-xs font-bold">
            <div className="flex items-center space-x-1.5">
              <span className="w-3 h-3 rounded-full bg-emerald-500 inline-block" />
              <span className="text-slate-600">Equity (above start)</span>
            </div>
            <div className="flex items-center space-x-1.5">
              <span className="w-3 h-3 rounded-full bg-rose-400 inline-block" />
              <span className="text-slate-600">Equity (below start)</span>
            </div>
            <div className="flex items-center space-x-1.5">
              <span className="w-5 h-0.5 border-t-2 border-dashed border-slate-400 inline-block" />
              <span className="text-slate-500">Start ₹{initialCap.toLocaleString('en-IN')}</span>
            </div>
            <div className="flex items-center space-x-1.5">
              <span className="w-5 h-0.5 border-t-2 border-dashed border-emerald-500 inline-block" />
              <span className="text-slate-500">Peak HWM</span>
            </div>
          </div>
        </div>

        {/* Recharts Curve */}
        <div className="h-80 w-full">
          {equityCurve.length > 0 ? (() => {
            // Deduplicate same-timestamp points — keep last (most recent) per timestamp
            const seen = new Map();
            equityCurve.forEach((pt) => { seen.set(pt.timestamp, pt); });
            const chartData = Array.from(seen.values());

            // Determine adaptive color: green if current equity >= start, red otherwise
            const lastEq = chartData[chartData.length - 1]?.total_equity ?? initialCap;
            const isAboveStart = lastEq >= initialCap;
            const lineColor = isAboveStart ? '#10b981' : '#f43f5e';
            const gradId = isAboveStart ? 'equityGradGreen' : 'equityGradRed';
            const gradStart = isAboveStart ? '#10b981' : '#f43f5e';

            // Format x-axis label: show date if multi-day, else just time
            const firstDate = chartData[0]?.timestamp?.slice(0, 10) ?? '';
            const lastDate = chartData[chartData.length - 1]?.timestamp?.slice(0, 10) ?? '';
            const isMultiDay = firstDate !== lastDate;

            const xFormatter = (t) => {
              if (!t) return '';
              // t could be "2026-09-22 09:21:52" or "2026-09-22T09:21:52.123"
              const clean = t.replace('T', ' ');
              if (isMultiDay) {
                const date = clean.slice(5, 10); // MM-DD
                const time = clean.slice(11, 16); // HH:MM
                return `${date} ${time}`;
              }
              return clean.slice(11, 16);
            };

            return (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 10, right: 24, left: 16, bottom: 0 }}>
                  <defs>
                    <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={gradStart} stopOpacity={0.25} />
                      <stop offset="95%" stopColor={gradStart} stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#f1f5f9" />
                  <XAxis
                    dataKey="timestamp"
                    tick={{ fontSize: 10, fill: '#64748b' }}
                    tickFormatter={xFormatter}
                    interval="preserveStartEnd"
                    minTickGap={isMultiDay ? 80 : 40}
                  />
                  <YAxis
                    domain={['auto', 'auto']}
                    tick={{ fontSize: 10, fill: '#64748b' }}
                    tickFormatter={(v) => `₹${Math.round(v / 1000)}k`}
                    width={52}
                  />
                  <Tooltip
                    content={({ active, payload }) => {
                      if (active && payload && payload.length) {
                        const d = payload[0].payload;
                        const eq = d.total_equity;
                        const diff = eq - initialCap;
                        const diffFromPeak = eq - (d.high_water_mark || eq);
                        const drawdownPct = d.high_water_mark > 0
                          ? ((d.high_water_mark - eq) / d.high_water_mark * 100)
                          : 0;
                        return (
                          <div className="bg-slate-900 border border-slate-700 rounded-xl p-3 text-white text-xs shadow-xl min-w-[180px] space-y-1.5">
                            <div className="text-slate-400 text-[10px] font-bold border-b border-slate-700 pb-1.5">
                              {d.timestamp?.replace('T', ' ')?.slice(0, 19)}
                            </div>
                            <div className="text-base font-black text-white">
                              ₹{eq.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                            </div>
                            <div className={`font-bold text-xs ${diff >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                              {diff >= 0 ? `+₹${diff.toFixed(2)}` : `-₹${Math.abs(diff).toFixed(2)}`}
                              {' '}({diff >= 0 ? '+' : ''}{((diff / initialCap) * 100).toFixed(2)}%) vs start
                            </div>
                            {drawdownPct > 0.01 && (
                              <div className="text-rose-300 text-[10px]">
                                Drawdown from HWM: -{drawdownPct.toFixed(2)}%
                              </div>
                            )}
                            <div className="text-slate-400 text-[10px] pt-1 border-t border-slate-700 flex justify-between">
                              <span>Cash: ₹{(d.cash || 0).toLocaleString('en-IN')}</span>
                              <span>{d.open_positions} open</span>
                            </div>
                            {d.event_type && (
                              <div className={`text-[9px] font-black uppercase tracking-wider px-1.5 py-0.5 rounded w-fit ${
                                d.event_type === 'TRADE_CLOSED' ? 'bg-emerald-900 text-emerald-300' :
                                d.event_type === 'TRADE_OPENED' ? 'bg-amber-900 text-amber-300' :
                                'bg-slate-700 text-slate-300'
                              }`}>
                                {d.event_type?.replace('_', ' ')}
                              </div>
                            )}
                            {d.note && <div className="text-slate-400 text-[10px] italic truncate max-w-[200px]">{d.note}</div>}
                          </div>
                        );
                      }
                      return null;
                    }}
                  />
                  <ReferenceLine
                    y={initialCap}
                    stroke="#94a3b8"
                    strokeDasharray="5 3"
                    strokeWidth={1.5}
                    label={{ value: `Start ₹${(initialCap/1000).toFixed(0)}k`, position: 'insideBottomRight', fill: '#94a3b8', fontSize: 9, fontWeight: 700 }}
                  />
                  {(p.peak_equity || 0) > initialCap && (
                    <ReferenceLine
                      y={p.peak_equity}
                      stroke="#10b981"
                      strokeDasharray="3 3"
                      strokeWidth={1.5}
                      label={{ value: `Peak ₹${(p.peak_equity/1000).toFixed(1)}k`, position: 'insideTopRight', fill: '#10b981', fontSize: 9, fontWeight: 700 }}
                    />
                  )}
                  <Area
                    type="monotone"
                    dataKey="total_equity"
                    stroke={lineColor}
                    strokeWidth={2}
                    fillOpacity={1}
                    fill={`url(#${gradId})`}
                    dot={false}
                    activeDot={{ r: 5, fill: lineColor, stroke: '#fff', strokeWidth: 2 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            );
          })() : (
            <div className="h-full flex items-center justify-center text-slate-400 text-xs font-semibold">
              No historical equity snapshots generated yet.
            </div>
          )}
        </div>
      </div>

      {/* ── LIVE ACTIVE HOLDINGS & MARKET-HOURS POSITIONS (TODAY) ──────────── */}
      <div id="active-holdings-section" ref={activeHoldingsRef} className="bg-white border border-slate-200/90 rounded-3xl p-6 shadow-sm space-y-5">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 border-b border-slate-100 pb-4">
          <div>
            <div className="flex items-center space-x-2.5">
              <div className="p-2 bg-amber-500/10 text-amber-600 rounded-xl">
                <Zap size={20} className="fill-amber-500/30" />
              </div>
              <h2 className="text-lg font-black text-slate-900 tracking-tight">
                Live Arena Holdings & Today's Positions
              </h2>
              <span className="flex items-center space-x-1.5 px-3 py-1 bg-emerald-50 text-emerald-700 text-xs font-black rounded-full border border-emerald-200/80">
                <span className="w-2 h-2 rounded-full bg-emerald-500 animate-ping" />
                <span>{(p.open_positions || []).length} / {p.max_positions || 5} Slots Active</span>
              </span>
              <span className="hidden sm:flex items-center space-x-1 px-2.5 py-1 bg-amber-50 text-amber-900 text-xs font-bold rounded-full border border-amber-200/80">
                {tradeSelectionMode === 'TIMED_ANCHOR_CONFLUENCE' ? (
                  <>
                    <Clock size={11} className="text-amber-600" />
                    <span>Timed Scans (09:30, 11:30, 1:30)</span>
                  </>
                ) : tradeSelectionMode === 'HIGHEST_CONFLUENCE_ONLY' ? (
                  <>
                    <Zap size={11} className="text-amber-600 fill-amber-600" />
                    <span>Highest Confluence Filter</span>
                  </>
                ) : (
                  <span>All Consensus Setups</span>
                )}
              </span>
            </div>
            <p className="text-xs font-semibold text-slate-500 mt-1">
              Real-time mark-to-market positions derived strictly from today's live consensus signals with sub-second price tick animations.
            </p>
          </div>

          <div className="flex items-center space-x-3 bg-slate-50 border border-slate-200/70 p-2 rounded-2xl">
            <div className="text-right px-2">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Total Invested</div>
              <div className="text-sm font-black text-slate-900">
                ₹{((p.open_positions || []).reduce((acc, pos) => acc + (pos.allocated_capital || 0), 0)).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
              </div>
            </div>
            <div className="h-7 w-px bg-slate-200" />
            <div className="text-right px-2">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Live Floating MTM</div>
              <div className={`text-sm font-black flex items-center justify-end space-x-1 ${(p.unrealized_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                {(p.unrealized_pnl || 0) >= 0 ? <ArrowUpRight size={15} /> : <ArrowDownRight size={15} />}
                <span>
                  {(p.unrealized_pnl || 0) >= 0 ? `+₹${(p.unrealized_pnl || 0).toFixed(2)}` : `-₹${Math.abs(p.unrealized_pnl || 0).toFixed(2)}`}
                </span>
              </div>
            </div>
            <div className="h-7 w-px bg-slate-200" />
            <div className="text-right px-2">
              <div className="text-[10px] font-bold uppercase tracking-wider text-slate-400">Today's Booked P&L</div>
              <div className={`text-sm font-black flex items-center justify-end space-x-1 ${(p.today_realized_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                {(p.today_realized_pnl || 0) >= 0 ? <ArrowUpRight size={15} /> : <ArrowDownRight size={15} />}
                <span>
                  {(p.today_realized_pnl || 0) >= 0 ? `+₹${(p.today_realized_pnl || 0).toFixed(2)}` : `-₹${Math.abs(p.today_realized_pnl || 0).toFixed(2)}`}
                </span>
              </div>
            </div>
            {(p.open_positions || []).length > 0 && (
              <>
                <div className="h-7 w-px bg-slate-200" />
                <button
                  onClick={handleExitAll}
                  disabled={exitingAll}
                  className="px-3 py-1.5 bg-rose-600 hover:bg-rose-700 disabled:opacity-50 text-white text-xs font-black rounded-xl transition shadow-xs flex items-center space-x-1.5 cursor-pointer shrink-0"
                  title="Square off all open arena positions at live market prices"
                >
                  <LogOut size={13} />
                  <span>{exitingAll ? 'Exiting All...' : 'Exit All'}</span>
                </button>
              </>
            )}
          </div>
        </div>

        {/* Positions Cards or Empty State */}
        {(p.open_positions || []).length === 0 ? (
          <div className="p-8 text-center bg-slate-50/70 rounded-2xl border border-dashed border-slate-200 space-y-2">
            <Layers className="mx-auto text-slate-400" size={32} />
            <div className="text-sm font-bold text-slate-700">No Active Positions Currently Open</div>
            <p className="text-xs text-slate-400 max-w-md mx-auto">
              All slots are liquid and ready. When the next 15-minute or Autopilot sweep generates a Unanimous consensus signal, it will be automatically deployed into the arena.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {(p.open_positions || []).map((pos) => {
              const flash = priceFlashes[pos.ticker];
              const pnlRupees = pos.unrealized_pnl_rupees || 0;
              const pnlPct = pos.unrealized_pnl_pct || 0;
              const isProfit = pnlRupees >= 0;

              // Progress towards target vs stop loss (Direction-Aware for BUY & SELL)
              const isSell = (pos.direction || 'BUY').toUpperCase() === 'SELL';
              const sl = pos.stop_loss || (isSell ? pos.entry_price * 1.01 : pos.entry_price * 0.99);
              const tp = pos.target_price || (isSell ? pos.entry_price * 0.98 : pos.entry_price * 1.02);
              const curPrice = pos.current_price || pos.entry_price;

              let profitDistPct = 0;
              let lossDistPct = 0;
              if (isSell) {
                // For SELL: profit if price < entry, loss if price > entry
                if (curPrice <= pos.entry_price) {
                  const profitRange = Math.max(0.001, pos.entry_price - tp);
                  profitDistPct = Math.min(100, Math.max(0, ((pos.entry_price - curPrice) / profitRange) * 100));
                } else {
                  const lossRange = Math.max(0.001, sl - pos.entry_price);
                  lossDistPct = Math.min(100, Math.max(0, ((curPrice - pos.entry_price) / lossRange) * 100));
                }
              } else {
                // For BUY: profit if price >= entry, loss if price < entry
                if (curPrice >= pos.entry_price) {
                  const profitRange = Math.max(0.001, tp - pos.entry_price);
                  profitDistPct = Math.min(100, Math.max(0, ((curPrice - pos.entry_price) / profitRange) * 100));
                } else {
                  const lossRange = Math.max(0.001, pos.entry_price - sl);
                  lossDistPct = Math.min(100, Math.max(0, ((pos.entry_price - curPrice) / lossRange) * 100));
                }
              }

              return (
                <div
                  key={pos.id}
                  className="bg-slate-50/50 hover:bg-white border border-slate-200 hover:border-slate-300 rounded-2xl p-4 transition-all duration-300 shadow-xs hover:shadow-md space-y-3 relative group"
                >
                  {/* Top: Ticker, Direction, Entry Time */}
                  <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-2">
                      <span className="text-base font-black text-slate-900 tracking-tight">
                        {(pos.ticker || '').replace('.NS', '').replace('.BO', '')}
                      </span>
                      <span className="text-[10px] font-bold text-slate-400 font-mono">.NS</span>
                      <span className={`text-[10px] font-black px-2 py-0.5 rounded-full uppercase tracking-wider ${
                        (pos.direction || 'BUY').toUpperCase() === 'BUY' ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                      }`}>
                        {pos.direction || 'BUY'}
                      </span>
                      {(() => {
                        const sentinel = pos.trade_sentinel || pos.details?.trade_sentinel;
                        if (!sentinel) return null;
                        if (sentinel.action === 'TAKE') {
                          return (
                            <span 
                              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-black bg-indigo-50 text-indigo-700 border border-indigo-200 shadow-2xs"
                              title={`Agent Approved: Trade Sentinel Concurrence (Win Prob: ${((sentinel.prob ?? 0) * 100).toFixed(0)}%, Expected Net: ${((sentinel.expected_net_return ?? 0) * 100).toFixed(2)}%)`}
                            >
                              <ShieldCheck size={11} className="text-indigo-600" />
                              <span>AGENT APPROVED</span>
                            </span>
                          );
                        } else if (sentinel.action === 'SKIP') {
                          return (
                            <span 
                              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-black bg-amber-50 text-amber-700 border border-amber-200 shadow-2xs"
                              title={`Agent Caution: Advised Skip (${sentinel.reasoning || 'Adversarial risk guardrail flagged'})`}
                            >
                              <AlertTriangle size={11} className="text-amber-600" />
                              <span>AGENT SKIP</span>
                            </span>
                          );
                        }
                        return null;
                      })()}
                    </div>

                    {/* Live Status Badge */}
                    {pos.is_stale || p.is_data_stale ? (
                      <span className="flex items-center space-x-1 text-[10px] font-black text-rose-700 bg-rose-50 border border-rose-200/80 px-2 py-0.5 rounded-full" title="Live price updates temporarily paused">
                        <span className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse" />
                        <span>STALE DATA</span>
                      </span>
                    ) : (
                      <span className="flex items-center space-x-1 text-[10px] font-black text-emerald-700 bg-emerald-50 border border-emerald-200/80 px-2 py-0.5 rounded-full">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                        <span>IN MARKET</span>
                      </span>
                    )}
                  </div>

                  {/* Pricing Row: Entry vs Live LTP with Flash Animations */}
                  <div className="grid grid-cols-2 gap-2 bg-white border border-slate-200/80 rounded-xl p-2.5">
                    <div>
                      <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Entry Price</div>
                      <div className="text-sm font-bold text-slate-700 font-mono">
                        ₹{(pos.entry_price ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                      </div>
                      <div className="text-[10px] text-slate-400 mt-0.5">
                        {pos.shares ?? 0} Shares (₹{Math.round(pos.allocated_capital ?? 0).toLocaleString('en-IN')})
                      </div>
                    </div>

                    <div className="text-right">
                      <div className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">Live Current LTP</div>
                      <div className="inline-flex items-center justify-end mt-0.5">
                        <span
                          className={`text-sm font-black font-mono px-2 py-0.5 rounded-lg transition-all duration-300 ${
                            flash === 'up'
                              ? 'bg-emerald-500 text-white ring-2 ring-emerald-300 shadow-md shadow-emerald-500/30 scale-105'
                              : flash === 'down'
                              ? 'bg-rose-500 text-white ring-2 ring-rose-300 shadow-md shadow-rose-500/30 scale-105'
                              : 'text-slate-900'
                          }`}
                        >
                          {flash === 'up' && <TrendingUp size={13} className="inline mr-1 animate-bounce" />}
                          {flash === 'down' && <TrendingDown size={13} className="inline mr-1 animate-bounce" />}
                          ₹{(pos.current_price || pos.entry_price).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                        </span>
                      </div>
                      <div className={`text-[11px] font-black mt-0.5 ${isProfit ? 'text-emerald-600' : 'text-rose-600'}`}>
                        {isProfit ? `+₹${pnlRupees.toFixed(2)}` : `-₹${Math.abs(pnlRupees).toFixed(2)}`} ({isProfit ? '+' : ''}{pnlPct.toFixed(2)}%)
                      </div>
                    </div>
                  </div>

                  {/* Target & Stop Loss Levels with Visual Payoff Progress Bar */}
                  <div className="space-y-1.5 pt-1">
                    <div className="flex items-center justify-between text-[10px] font-bold">
                      <span className="text-rose-600 font-mono">SL: ₹{pos.stop_loss}</span>
                      <span className="text-slate-400 font-semibold flex items-center gap-1">
                        <span>Payoff Gauge</span>
                        <span className="text-[9px] text-slate-400 font-normal">
                          ({isProfit ? `+${profitDistPct.toFixed(1)}% to TP` : `-${lossDistPct.toFixed(1)}% to SL`})
                        </span>
                      </span>
                      <span className="text-emerald-600 font-mono">TP: ₹{pos.target_price}</span>
                    </div>

                    {/* Centered Bidirectional Gauge: Middle is Entry (50%) */}
                    <div className="w-full bg-slate-200 h-2.5 rounded-full overflow-hidden relative shadow-inner">
                      {/* Center zero line marker */}
                      <div className="absolute left-1/2 top-0 bottom-0 w-[1.5px] bg-slate-400 z-10 -translate-x-1/2" title="Entry Price" />

                      {/* Profit bar extending from center (50%) to the right */}
                      {isProfit && (
                        <div
                          className="absolute top-0 bottom-0 left-1/2 h-full transition-all duration-500 rounded-r-full"
                          style={{
                            width: `${(profitDistPct / 100) * 50}%`,
                            background: `linear-gradient(to right, #10b981, #047857)`
                          }}
                        />
                      )}

                      {/* Loss bar extending from center (50%) to the left */}
                      {!isProfit && (
                        <div
                          className="absolute top-0 bottom-0 right-1/2 h-full transition-all duration-500 rounded-l-full"
                          style={{
                            width: `${(lossDistPct / 100) * 50}%`,
                            background: `linear-gradient(to left, #f43f5e, #9f1239)`
                          }}
                        />
                      )}
                    </div>

                    <div className="flex items-center justify-between text-[9px] text-slate-400 font-semibold">
                      <span>{(pos.sl_dist_pct || 0) > 0 ? `${pos.sl_dist_pct}% to SL` : 'At SL'}</span>
                      <span className="text-[8px] font-mono text-slate-400">ENTRY: ₹{pos.entry_price}</span>
                      <span>Target: {(pos.target_dist_pct || 0) > 0 ? `${pos.target_dist_pct}% away` : 'Reached'}</span>
                    </div>
                  </div>

                  {/* Trade Sentinel Audit Pill */}
                  {(() => {
                    const sentinel = pos.trade_sentinel || pos.details?.trade_sentinel;
                    if (!sentinel) return null;
                    return (
                      <div className="flex items-center justify-between text-[10px] bg-slate-100/80 border border-slate-200/80 rounded-lg px-2.5 py-1 text-slate-600 font-mono">
                        <span className="flex items-center gap-1 font-bold text-indigo-700">
                          <Bot size={12} />
                          <span>Sentinel: {sentinel.action}</span>
                        </span>
                        <span>Win Prob: {sentinel.prob != null ? `${(Number(sentinel.prob) * 100).toFixed(0)}%` : '—'}</span>
                        <span>Exp Net: {sentinel.expected_net_return != null ? `${(Number(sentinel.expected_net_return) * 100).toFixed(2)}%` : '—'}</span>
                      </div>
                    );
                  })()}

                  {/* Manual Exit / Square Off Button */}
                  <div className="pt-2 border-t border-slate-200/60">
                    <button
                      onClick={() => handleExitPosition(pos.id, pos.ticker)}
                      disabled={exitingId === pos.id}
                      className="w-full py-1.5 px-3 bg-rose-50 hover:bg-rose-100 disabled:opacity-50 text-rose-700 border border-rose-200 hover:border-rose-300 text-xs font-black rounded-xl transition flex items-center justify-center space-x-1.5 cursor-pointer shadow-2xs"
                      title={`Manually square off ${pos.ticker} at live market price`}
                    >
                      <LogOut size={12} />
                      <span>{exitingId === pos.id ? 'Exiting...' : 'Exit Trade'}</span>
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* ── ZERODHA CONSOLE-STYLE INTERACTIVE P&L CALENDAR ────────────────── */}
      <div className="bg-white border border-slate-200/90 rounded-3xl p-6 shadow-sm space-y-5">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div>
            <div className="flex items-center space-x-2">
              <CalendarIcon className="text-indigo-600" size={20} />
              <h2 className="text-lg font-black text-slate-900">Interactive P&L Calendar</h2>
              <span className="px-2.5 py-0.5 bg-indigo-50 text-indigo-700 text-xs font-black rounded-full uppercase">
                Console View
              </span>
            </div>
            <p className="text-xs font-semibold text-slate-500 mt-0.5">
              Click any calendar day (or the quick inspection badge) to view the complete profit/loss execution report.
            </p>
          </div>

          {/* Quick Inspection Pills for Active Days */}
          <div className="flex flex-wrap items-center gap-2">
            {(p.open_positions || []).length > 0 && (
              <button
                type="button"
                onClick={() => {
                  activeHoldingsRef.current?.scrollIntoView({ behavior: 'smooth' });
                }}
                className="flex items-center space-x-1.5 px-3 py-2 bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-600 hover:to-amber-700 text-white rounded-xl text-xs font-black shadow-md shadow-amber-500/20 transition-all active:scale-95"
              >
                <Zap size={14} className="fill-white" />
                <span>Today: {(p.open_positions || []).length} Active (MTM: {(p.unrealized_pnl || 0) >= 0 ? `+₹${(p.unrealized_pnl || 0).toFixed(2)}` : `-₹${Math.abs(p.unrealized_pnl || 0).toFixed(2)}`})</span>
              </button>
            )}

            {sep18Record && (
              <button
                type="button"
                onClick={() => {
                  setSelectedDay(sep18Record);
                  setDayInspectorOpen(true);
                }}
                className="flex items-center space-x-2 px-3.5 py-2 bg-gradient-to-r from-indigo-500 to-indigo-600 hover:from-indigo-600 hover:to-indigo-700 text-white rounded-xl text-xs font-black shadow-md shadow-indigo-500/20 transition-all active:scale-95"
              >
                <Eye size={14} />
                <span>Inspect Sep 18 P&L Report (20 Trades)</span>
              </button>
            )}
          </div>
        </div>

        {/* Days of Week Header */}
        <div className="grid grid-cols-5 gap-3">
          {['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'].map((day) => (
            <div key={day} className="text-center text-[11px] font-black uppercase tracking-wider text-slate-400 py-1 bg-slate-50 rounded-lg">
              {day}
            </div>
          ))}
        </div>

        {/* Calendar Matrix Grid */}
        <div className="grid grid-cols-5 gap-3">
          {septWeekdays.map((item) => {
            const dayRecord = (calendarData?.days || []).find((d) => d.date === item.date);
            const now = new Date();
            const localToday = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
            const isToday = item.date === localToday;
            const openPosCount = isToday ? (p.open_positions || []).length : 0;
            const hasTrades = !!dayRecord || (isToday && openPosCount > 0);
            const isProfit = dayRecord ? dayRecord.net_pnl_rupees > 0 : (p.unrealized_pnl || 0) >= 0;
            const isLoss = dayRecord ? dayRecord.net_pnl_rupees < 0 : (p.unrealized_pnl || 0) < 0;

            return (
              <div
                key={item.date}
                onClick={() => {
                  if (isToday && dayRecord) {
                    setSelectedDay(dayRecord);
                    setDayInspectorOpen(true);
                  } else if (isToday && openPosCount > 0) {
                    activeHoldingsRef.current?.scrollIntoView({ behavior: 'smooth' });
                  } else if (hasTrades) {
                    setSelectedDay(dayRecord);
                    setDayInspectorOpen(true);
                  }
                }}
                className={`min-h-[110px] p-3 rounded-2xl border transition-all duration-200 relative group flex flex-col justify-between cursor-pointer ${
                  isToday
                    ? 'bg-gradient-to-br from-amber-50/90 via-emerald-50/80 to-white border-amber-400 ring-2 ring-amber-400/30 hover:border-amber-500 hover:shadow-lg'
                    : hasTrades
                    ? isProfit
                      ? 'bg-emerald-50/90 border-emerald-400 ring-2 ring-emerald-400/20 hover:border-emerald-600 hover:shadow-lg'
                      : 'bg-rose-50/90 border-rose-400 ring-2 ring-rose-400/20 hover:border-rose-600 hover:shadow-lg'
                    : 'bg-slate-50/60 border-slate-200/70 text-slate-400 cursor-default'
                }`}
              >
                {/* Day Header */}
                <div className="flex items-center justify-between">
                  <span className={`text-xs font-black ${isToday ? 'text-amber-950 font-black flex items-center space-x-1' : hasTrades ? (isProfit ? 'text-emerald-950 font-black' : 'text-rose-950 font-black') : 'text-slate-400'}`}>
                    <span>{item.monthName || 'Sep'} {item.day}</span>
                    {isToday && <span className="ml-1 px-1.5 py-0.2 bg-amber-200 text-amber-900 rounded text-[9px]">TODAY</span>}
                  </span>
                  {isToday ? (
                    <span className="text-[10px] font-black px-2 py-0.5 rounded-full uppercase tracking-wider bg-emerald-200 text-emerald-900 flex items-center space-x-1">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-600 animate-ping" />
                      <span>{openPosCount} Active</span>
                    </span>
                  ) : hasTrades && (
                    <span
                      className={`text-[10px] font-black px-2 py-0.5 rounded-full uppercase tracking-wider ${
                        isProfit ? 'bg-emerald-200 text-emerald-900' : 'bg-rose-200 text-rose-900'
                      }`}
                    >
                      {dayRecord.trade_count} Trades
                    </span>
                  )}
                </div>

                {/* Day Content */}
                {isToday ? (
                  <div className="space-y-0.5 my-1">
                    {dayRecord ? (
                      <div className={`text-base font-black tracking-tight ${dayRecord.net_pnl_rupees >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                        {dayRecord.net_pnl_rupees >= 0 ? `+₹${dayRecord.net_pnl_rupees.toLocaleString('en-IN')}` : `-₹${Math.abs(dayRecord.net_pnl_rupees).toLocaleString('en-IN')}`}
                      </div>
                    ) : (
                      <div className="text-base font-black tracking-tight text-slate-400">₹0.00</div>
                    )}
                    <div className="text-[10px] font-black text-amber-700 uppercase tracking-wider flex items-center space-x-1">
                      <span>Booked P&amp;L</span>
                      {(p.unrealized_pnl || 0) !== 0 && (
                        <span className={`${(p.unrealized_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                          {'\u00b7'} MTM {(p.unrealized_pnl || 0) >= 0 ? `+₹${(p.unrealized_pnl || 0).toFixed(0)}` : `-₹${Math.abs(p.unrealized_pnl || 0).toFixed(0)}`}
                        </span>
                      )}
                    </div>
                    <div className="text-[10px] font-bold text-slate-600">
                      {openPosCount} / {p.max_positions || 5} slots active
                    </div>
                  </div>
                ) : hasTrades ? (
                  <div className="space-y-0.5 my-1">
                    <div className={`text-base font-black tracking-tight ${isProfit ? 'text-emerald-700' : 'text-rose-700'}`}>
                      {isProfit
                        ? `+₹${(dayRecord?.net_pnl_rupees ?? 0).toLocaleString('en-IN')}`
                        : `-₹${Math.abs(dayRecord?.net_pnl_rupees ?? 0).toLocaleString('en-IN')}`}
                    </div>
                    <div className={`text-xs font-bold ${isProfit ? 'text-emerald-600' : 'text-rose-600'}`}>
                      {(dayRecord?.return_pct ?? 0) > 0 ? `+${dayRecord?.return_pct}%` : `${dayRecord?.return_pct ?? 0}%`}
                    </div>
                    <div className="text-[10px] font-bold text-slate-600">
                      {dayRecord?.win_rate ?? 0}% Win Rate ({dayRecord?.wins ?? 0}W / {dayRecord?.losses ?? 0}L)
                    </div>
                  </div>
                ) : (
                  <div className="text-[11px] text-slate-400 italic">No consensus trades</div>
                )}

                {/* Bottom prompt */}
                {isToday && dayRecord ? (
                  <div className="text-[11px] font-black text-indigo-700 group-hover:underline flex items-center space-x-1 mt-1 pt-1 border-t border-amber-200/80">
                    <Eye size={13} />
                    <span>View Today's P&amp;L Report</span>
                  </div>
                ) : isToday && openPosCount > 0 ? (
                  <div className="text-[11px] font-black text-amber-800 group-hover:underline flex items-center space-x-1 mt-1 pt-1 border-t border-amber-200/80">
                    <Zap size={13} />
                    <span>View Active Holdings</span>
                  </div>
                ) : hasTrades && (
                  <div className="text-[11px] font-black text-indigo-700 group-hover:underline flex items-center space-x-1 mt-1 pt-1 border-t border-rose-200/60">
                    <Eye size={13} />
                    <span>View P&amp;L Report</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* ── ACTIVE POSITIONS & AUDITED LEDGER ─────────────────────────────── */}
      <div className="bg-white border border-slate-200/90 rounded-3xl p-6 shadow-sm">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 mb-6">
          <div>
            <div className="flex items-center space-x-2">
              <Layers className="text-amber-600" size={20} />
              <h2 className="text-lg font-black text-slate-900">Arena Executed Trade Ledger</h2>
            </div>
            <p className="text-xs font-semibold text-slate-500 mt-0.5">
              Audited positions sized with real whole shares, authentic exit prices, and Zerodha/Upstox friction deductions.
            </p>
          </div>

          {/* Filter Chips */}
          <div className="flex items-center space-x-2 bg-slate-100 p-1 rounded-xl">
            {['ALL', 'WIN', 'LOSS'].map((flt) => (
              <button
                key={flt}
                type="button"
                onClick={() => setLedgerFilter(flt)}
                className={`px-3 py-1.5 rounded-lg text-xs font-black transition-all ${
                  ledgerFilter === flt ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                {flt === 'ALL' ? 'All Executions' : flt === 'WIN' ? 'Winning Trades' : 'Stop Loss / Losses'}
              </button>
            ))}
          </div>
        </div>

        {/* Ledger Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-slate-100 text-[10px] uppercase font-black tracking-wider text-slate-400">
                <th className="pb-3">Stock / Symbol</th>
                <th className="pb-3">Direction</th>
                <th className="pb-3">Shares</th>
                <th className="pb-3">Entry Price</th>
                <th className="pb-3">Exit Price</th>
                <th className="pb-3">Capital Deployed</th>
                <th className="pb-3">Fees & Slippage</th>
                <th className="pb-3">Net Realized P&L</th>
                <th className="pb-3">Return %</th>
                <th className="pb-3">Outcome</th>
                <th className="pb-3">Exit Time</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 font-medium">
              {(() => {
                const openTrades = (p.open_positions || []).map((pos) => ({
                  id: pos.id,
                  ticker: pos.ticker,
                  direction: pos.direction,
                  shares: pos.shares,
                  entry_price: pos.entry_price,
                  exit_price: null,
                  allocated_capital: pos.allocated_capital,
                  net_pnl: pos.unrealized_pnl_rupees || 0,
                  pnl_pct: pos.unrealized_pnl_pct || 0,
                  outcome: 'OPEN',
                  status: 'OPEN',
                  entry_time: pos.entry_time,
                  exit_time: null,
                  isOpen: true,
                  trade_sentinel: pos.trade_sentinel || pos.details?.trade_sentinel
                }));

                const closedTrades = (calendarData?.days || [])
                  .slice()
                  .reverse()
                  .flatMap((d) => (d.trades || []).slice().reverse()) || [];

                const combinedTrades = [...openTrades, ...closedTrades];

                return combinedTrades
                  .filter((t) => {
                    if (ledgerFilter === 'WIN') return (t.net_pnl || 0) > 0;
                    if (ledgerFilter === 'LOSS') return (t.net_pnl || 0) < 0;
                    return true;
                  })
                  .map((trade) => {
                    const isTradeWin = (trade.net_pnl || 0) > 0;
                    const isOpen = trade.isOpen || trade.outcome === 'OPEN' || trade.status === 'OPEN';
                    return (
                      <tr
                        key={`${trade.id}-${trade.exit_time || trade.entry_time || 'open'}`}
                        className={`transition-all ${isOpen ? 'bg-sky-50/50 hover:bg-sky-50' : 'hover:bg-slate-50/80'}`}
                      >
                        <td className="py-3 font-bold text-slate-900 flex items-center space-x-1.5">
                          <span>{trade.ticker.replace('.NS', '').replace('.BO', '')}</span>
                          {isOpen && (
                            <span className="w-1.5 h-1.5 rounded-full bg-sky-500 animate-pulse" title="Active in slot" />
                          )}
                          {(() => {
                            const sentinel = trade.trade_sentinel || trade.details?.trade_sentinel;
                            if (sentinel?.action === 'TAKE') {
                              return (
                                <span className="inline-flex items-center gap-0.5 px-1.5 py-0.2 rounded text-[9px] font-black bg-indigo-50 text-indigo-700 border border-indigo-200" title="Agent Approved">
                                  <ShieldCheck size={9} className="text-indigo-600" />
                                  <span>APP</span>
                                </span>
                              );
                            } else if (sentinel?.action === 'SKIP') {
                              return (
                                <span className="inline-flex items-center gap-0.5 px-1.5 py-0.2 rounded text-[9px] font-black bg-amber-50 text-amber-700 border border-amber-200" title="Agent Skip Flagged">
                                  <AlertTriangle size={9} className="text-amber-600" />
                                  <span>SKIP</span>
                                </span>
                              );
                            }
                            return null;
                          })()}
                        </td>
                        <td className="py-3">
                          <span
                            className={`px-2 py-0.5 rounded-md text-[10px] font-black ${
                              trade.direction.includes('BUY') || trade.direction.includes('BULLISH')
                                ? 'bg-emerald-100 text-emerald-800'
                                : 'bg-rose-100 text-rose-800'
                            }`}
                          >
                            {trade.direction}
                          </span>
                        </td>
                        <td className="py-3 font-bold text-slate-800">{trade.shares} qty</td>
                        <td className="py-3">₹{trade.entry_price.toFixed(2)}</td>
                        <td className="py-3 font-bold">
                          {trade.exit_price ? `₹${trade.exit_price.toFixed(2)}` : (
                            <span className="text-slate-400 font-normal italic">In Slot</span>
                          )}
                        </td>
                        <td className="py-3 font-semibold text-slate-700">
                          ₹{(trade.allocated_capital || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                        </td>
                        <td className="py-3 text-slate-500">
                          ₹{(trade.allocated_capital * 0.0005 + 40).toFixed(2)}
                        </td>
                        <td className={`py-3 font-black ${isTradeWin ? 'text-emerald-600' : (isOpen && trade.net_pnl === 0 ? 'text-slate-500' : 'text-rose-600')}`}>
                          {isOpen ? (
                            <span>
                              {isTradeWin ? `+₹${trade.net_pnl.toFixed(2)}` : trade.net_pnl < 0 ? `-₹${Math.abs(trade.net_pnl).toFixed(2)}` : '₹0.00'}
                              <span className="text-[9px] font-normal text-slate-400 font-sans ml-1">(MTM)</span>
                            </span>
                          ) : (
                            isTradeWin ? `+₹${trade.net_pnl.toFixed(2)}` : `-₹${Math.abs(trade.net_pnl).toFixed(2)}`
                          )}
                        </td>
                        <td className={`py-3 font-bold ${isTradeWin ? 'text-emerald-600' : (isOpen && trade.pnl_pct === 0 ? 'text-slate-500' : 'text-rose-600')}`}>
                          {trade.pnl_pct > 0 ? `+${trade.pnl_pct}%` : `${trade.pnl_pct}%`}
                        </td>
                        <td className="py-3">
                          <span
                            className={`px-2 py-0.5 rounded-full text-[10px] font-black uppercase tracking-wider ${
                              isOpen
                                ? 'bg-sky-100 text-sky-800 border border-sky-300 animate-pulse'
                                : trade.outcome === 'TARGET MET'
                                ? 'bg-emerald-100 text-emerald-800'
                                : trade.outcome === 'BREAKEVEN HIT'
                                ? 'bg-amber-100 text-amber-800'
                                : trade.outcome === 'SL HIT'
                                ? 'bg-rose-100 text-rose-800'
                                : 'bg-slate-100 text-slate-700'
                            }`}
                          >
                            {isOpen ? 'ACTIVE (IN SLOT)' : (trade.outcome || 'CLOSED')}
                          </span>
                        </td>
                        <td className="py-3 text-slate-500 text-[11px] font-semibold whitespace-nowrap">
                          {trade.exit_time
                            ? trade.exit_time.slice(0, 16)
                            : (trade.entry_time ? `${trade.entry_time.slice(11, 16)} (Entered)` : '-')}
                        </td>
                      </tr>
                    );
                  });
              })()}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── MODAL 1: CAPITAL DEPLOYMENT & SETTINGS ────────────────────────── */}
      {deployModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-sm p-4">
          <div className="bg-white border border-slate-200 rounded-3xl p-6 max-w-lg w-full shadow-2xl space-y-5 animate-in fade-in zoom-in-95 duration-200">
            <div className="flex items-center justify-between border-b border-slate-100 pb-4">
              <div className="flex items-center space-x-2.5">
                <span className="p-2 bg-amber-100 text-amber-800 rounded-xl">
                  <Wallet size={20} />
                </span>
                <div>
                  <h3 className="text-base font-black text-slate-900">Deploy Capital into Arena</h3>
                  <p className="text-xs text-slate-500">Configure your starting bankroll & allocation parameters</p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setDeployModalOpen(false)}
                className="text-slate-400 hover:text-slate-600 p-1"
              >
                ✕
              </button>
            </div>

            {/* Quick Capital Preset Chips */}
            <div className="space-y-2">
              <label className="block text-xs font-bold uppercase tracking-wider text-slate-600">
                Initial Bankroll Amount (₹)
              </label>
              <div className="grid grid-cols-4 gap-2">
                {[50000, 100000, 200000, 500000].map((amt) => (
                  <button
                    key={amt}
                    type="button"
                    onClick={() => setCustomCapital(amt)}
                    className={`py-2 text-xs font-black rounded-xl border transition-all ${
                      customCapital === amt
                        ? 'bg-amber-500 border-amber-600 text-slate-950 shadow-sm'
                        : 'bg-slate-50 border-slate-200 text-slate-700 hover:bg-slate-100'
                    }`}
                  >
                    ₹{(amt / 1000).toFixed(0)}K
                  </button>
                ))}
              </div>
              <input
                type="number"
                value={customCapital}
                onChange={(e) => setCustomCapital(Math.max(1000, Number(e.target.value)))}
                className="w-full mt-2 px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm font-bold text-slate-900 focus:outline-none focus:ring-2 focus:ring-amber-500/20"
                placeholder="Enter custom capital in ₹"
              />
            </div>

            {/* Max Concurrent Positions */}
            <div className="space-y-2">
              <label className="block text-xs font-bold uppercase tracking-wider text-slate-600">
                Max Concurrent Positions (Slots)
              </label>
              <div className="grid grid-cols-4 gap-2">
                {[3, 5, 8, 10].map((slots) => (
                  <button
                    key={slots}
                    type="button"
                    onClick={() => setMaxPositions(slots)}
                    className={`py-2 text-xs font-black rounded-xl border transition-all ${
                      maxPositions === slots
                        ? 'bg-slate-900 border-slate-900 text-white shadow-sm'
                        : 'bg-slate-50 border-slate-200 text-slate-700 hover:bg-slate-100'
                    }`}
                  >
                    {slots} Slots ({((1 / slots) * 100).toFixed(0)}% ea)
                  </button>
                ))}
              </div>
            </div>

            {/* Scan Execution Filter Mode */}
            <div className="space-y-2">
              <label className="block text-xs font-bold uppercase tracking-wider text-slate-600">
                Scan Execution Filter
              </label>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
                <button
                  type="button"
                  onClick={() => setTradeSelectionMode('ALL_QUALIFIED')}
                  className={`p-3 text-xs font-bold rounded-xl border text-left transition-all ${
                    tradeSelectionMode === 'ALL_QUALIFIED'
                      ? 'bg-amber-500/10 border-amber-500 text-slate-900 shadow-sm'
                      : 'bg-slate-50 border-slate-200 text-slate-600 hover:bg-slate-100'
                  }`}
                >
                  <div className="font-black text-slate-900">All Trades</div>
                  <div className="text-[11px] text-slate-500 mt-0.5">Take every unanimous consensus setup up to slot limit</div>
                </button>
                <button
                  type="button"
                  onClick={() => setTradeSelectionMode('HIGHEST_CONFLUENCE_ONLY')}
                  className={`p-3 text-xs font-bold rounded-xl border text-left transition-all ${
                    tradeSelectionMode === 'HIGHEST_CONFLUENCE_ONLY'
                      ? 'bg-amber-500/10 border-amber-500 text-slate-900 shadow-sm'
                      : 'bg-slate-50 border-slate-200 text-slate-600 hover:bg-slate-100'
                  }`}
                >
                  <div className="flex items-center space-x-1.5 font-black text-slate-900">
                    <Zap size={13} className="text-amber-500 fill-amber-500" />
                    <span>Highest Confluence</span>
                  </div>
                  <div className="text-[11px] text-slate-500 mt-0.5">Pick single highest confluence setup per scan, reject others</div>
                </button>
                <button
                  type="button"
                  onClick={() => setTradeSelectionMode('TIMED_ANCHOR_CONFLUENCE')}
                  className={`p-3 text-xs font-bold rounded-xl border text-left transition-all ${
                    tradeSelectionMode === 'TIMED_ANCHOR_CONFLUENCE'
                      ? 'bg-amber-500/10 border-amber-500 text-slate-900 shadow-sm'
                      : 'bg-slate-50 border-slate-200 text-slate-600 hover:bg-slate-100'
                  }`}
                >
                  <div className="flex items-center space-x-1.5 font-black text-slate-900">
                    <Clock size={13} className="text-amber-600" />
                    <span>Timed Scans</span>
                  </div>
                  <div className="text-[11px] text-slate-500 mt-0.5">Top setup from 09:30, 11:30 & 13:30 anchors (avoids 15m friction)</div>
                </button>
              </div>
            </div>

            {/* Brokerage & Friction Toggle */}
            <div className="flex items-center justify-between p-3.5 bg-slate-50 border border-slate-200 rounded-2xl">
              <div>
                <div className="text-xs font-bold text-slate-900">Include Realistic Fees & Slippage</div>
                <div className="text-[11px] text-slate-500">₹20 flat brokerage per leg + 0.05% realistic market slippage</div>
              </div>
              <input
                type="checkbox"
                checked={feesEnabled}
                onChange={(e) => setFeesEnabled(e.target.checked)}
                className="w-4 h-4 accent-amber-500 rounded cursor-pointer"
              />
            </div>

            {/* Deploy Action */}
            <div className="pt-2 flex justify-end space-x-3">
              <button
                type="button"
                onClick={() => setDeployModalOpen(false)}
                className="px-4 py-2.5 text-xs font-bold text-slate-600 hover:text-slate-900"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => handleDeployCapital(false)}
                disabled={deploying}
                className="flex items-center space-x-2 px-6 py-2.5 bg-amber-500 hover:bg-amber-400 text-slate-950 font-black text-xs uppercase tracking-wider rounded-xl shadow-md shadow-amber-500/20 transition-all active:scale-95"
              >
                {deploying ? (
                  <RefreshCw size={14} className="animate-spin" />
                ) : (
                  <Check size={14} />
                )}
                <span>Deploy ₹{customCapital.toLocaleString('en-IN')} Bankroll</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── MODAL 2: RESET / START ANEW CONFIRMATION ──────────────────────── */}
      {resetModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-sm p-4">
          <div className="bg-white border border-slate-200 rounded-3xl p-6 max-w-md w-full shadow-2xl space-y-4 animate-in fade-in zoom-in-95 duration-200">
            <div className="flex items-center space-x-3 text-rose-600">
              <div className="p-3 bg-rose-100 rounded-2xl">
                <RotateCcw size={24} />
              </div>
              <div>
                <h3 className="text-base font-black text-slate-900">Reset & Start Anew?</h3>
                <p className="text-xs text-slate-500">Restart your paper trading arena</p>
              </div>
            </div>

            <p className="text-xs text-slate-600 leading-relaxed">
              This will reset your paper trading balance back to <b>₹1,00,000</b> and cleanly re-synchronize all consensus signals from scratch. 
              <br /><br />
              <span className="font-bold text-emerald-700">✓ Production Safe:</span> All real models, historical candles, and live data remain completely untouched.
            </p>

            <div className="pt-2 flex justify-end space-x-3">
              <button
                type="button"
                onClick={() => setResetModalOpen(false)}
                className="px-4 py-2 text-xs font-bold text-slate-600 hover:text-slate-900"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => handleDeployCapital(true)}
                disabled={deploying}
                className="flex items-center space-x-1.5 px-5 py-2.5 bg-rose-600 hover:bg-rose-500 text-white font-black text-xs uppercase tracking-wider rounded-xl transition-all shadow-md shadow-rose-600/20 active:scale-95"
              >
                {deploying ? <RefreshCw size={14} className="animate-spin" /> : <RotateCcw size={14} />}
                <span>Reset to ₹1,00,000</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── MODAL 3: HOW CAPITAL IS DEPLOYED EXPLAINER ─────────────────────── */}
      {howItWorksOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-sm p-4">
          <div className="bg-white border border-slate-200 rounded-3xl p-6 max-w-lg w-full shadow-2xl space-y-5 max-h-[88vh] overflow-y-auto animate-in fade-in zoom-in-95 duration-200">
            <div className="flex items-center justify-between border-b border-slate-100 pb-4">
              <div className="flex items-center space-x-2.5">
                <span className="p-2 bg-indigo-100 text-indigo-700 rounded-xl">
                  <HelpCircle size={20} />
                </span>
                <div>
                  <h3 className="text-base font-black text-slate-900">How Capital is Deployed</h3>
                  <p className="text-xs text-slate-500">Mathematical sizing & trade lifecycle rules</p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setHowItWorksOpen(false)}
                className="text-slate-400 hover:text-slate-600 p-1"
              >
                ✕
              </button>
            </div>

            <div className="space-y-4 text-xs text-slate-700 leading-relaxed">
              {/* Step 1 */}
              <div className="p-3.5 bg-slate-50 rounded-2xl border border-slate-200/80 space-y-1">
                <div className="font-black text-slate-900 flex items-center space-x-1.5">
                  <span className="w-5 h-5 bg-amber-500 text-slate-950 rounded-full flex items-center justify-center text-[10px] font-black">1</span>
                  <span>Dual Consensus Signal Trigger</span>
                </div>
                <p className="text-[11px] text-slate-600">
                  A trade enters the arena only when <b>Qlib Alpha-158 GBDT</b> and the <b>Legacy Ensemble</b> reach unanimous confluence (Direction match + Combined Conviction &gt; 50%).
                </p>
              </div>

              {/* Step 2 */}
              <div className="p-3.5 bg-slate-50 rounded-2xl border border-slate-200/80 space-y-1">
                <div className="font-black text-slate-900 flex items-center space-x-1.5">
                  <span className="w-5 h-5 bg-amber-500 text-slate-950 rounded-full flex items-center justify-center text-[10px] font-black">2</span>
                  <span>Bankroll Slot Partitioning</span>
                </div>
                <p className="text-[11px] text-slate-600">
                  Your bankroll (e.g. ₹1,00,000) is split across your chosen concurrent slots (default 5 slots = <b>₹20,000 maximum per position</b>).
                </p>
              </div>

              {/* Step 3 */}
              <div className="p-3.5 bg-slate-50 rounded-2xl border border-slate-200/80 space-y-1">
                <div className="font-black text-slate-900 flex items-center space-x-1.5">
                  <span className="w-5 h-5 bg-amber-500 text-slate-950 rounded-full flex items-center justify-center text-[10px] font-black">3</span>
                  <span>Strict Whole-Share Execution</span>
                </div>
                <p className="text-[11px] text-slate-600">
                  Exact shares are calculated via: <code>shares = floor(slot_capital / entry_price)</code>. 
                  Zero fractional shares! If HAL costs ₹4,878, exactly <b>4 shares</b> (₹19,512) are bought. The remaining ₹488 stays in cash.
                </p>
              </div>

              {/* Step 4 */}
              <div className="p-3.5 bg-slate-50 rounded-2xl border border-slate-200/80 space-y-1">
                <div className="font-black text-slate-900 flex items-center space-x-1.5">
                  <span className="w-5 h-5 bg-amber-500 text-slate-950 rounded-full flex items-center justify-center text-[10px] font-black">4</span>
                  <span>Automated Risk & Exit Management</span>
                </div>
                <p className="text-[11px] text-slate-600">
                  Positions are strictly governed by their Stop Loss and Target Price (TP1). Profit is locked in with a dynamic breakeven ratchet at +0.70% gain. Remaining intraday trades are squared off at 15:15 IST.
                </p>
              </div>

              {/* Step 5 */}
              <div className="p-3.5 bg-slate-50 rounded-2xl border border-slate-200/80 space-y-1">
                <div className="font-black text-slate-900 flex items-center space-x-1.5">
                  <span className="w-5 h-5 bg-amber-500 text-slate-950 rounded-full flex items-center justify-center text-[10px] font-black">5</span>
                  <span>Capital Recycling & Friction</span>
                </div>
                <p className="text-[11px] text-slate-600">
                  On trade exit, your original allocated capital plus/minus net P&L (after ₹20/order flat fee and 0.05% slippage) immediately returns to your available cash, ready for the next trade!
                </p>
              </div>
            </div>

            <div className="pt-2 flex justify-end">
              <button
                type="button"
                onClick={() => setHowItWorksOpen(false)}
                className="px-5 py-2 bg-slate-900 text-white rounded-xl text-xs font-black"
              >
                Got It
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── MODAL 4: DAY INSPECTOR DRAWER (PROFIT/LOSS REPORT) ────────────── */}
      {dayInspectorOpen && selectedDay && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-sm p-4">
          <div className="bg-white border border-slate-200 rounded-3xl p-6 max-w-2xl w-full shadow-2xl space-y-5 max-h-[90vh] overflow-y-auto animate-in fade-in zoom-in-95 duration-200">
            {/* Header */}
            <div className="flex items-center justify-between border-b border-slate-100 pb-4">
              <div>
                <div className="flex items-center space-x-2">
                  <CalendarIcon className="text-indigo-600" size={20} />
                  <h3 className="text-base font-black text-slate-900">
                    Day Performance: {selectedDay.date}
                  </h3>
                </div>
                <p className="text-xs text-slate-500 mt-0.5">
                  Audited execution log for all consensus confluence trades on this date.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setDayInspectorOpen(false)}
                className="text-slate-400 hover:text-slate-600 p-1"
              >
                ✕
              </button>
            </div>

            {/* Day Summary Cards */}
            <div className="grid grid-cols-4 gap-3">
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-100">
                <div className="text-[10px] uppercase font-bold text-slate-400">Net Day P&L</div>
                <div className={`text-base font-black ${selectedDay.net_pnl_rupees >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                  {selectedDay.net_pnl_rupees >= 0 ? `+₹${selectedDay.net_pnl_rupees.toLocaleString('en-IN')}` : `-₹${Math.abs(selectedDay.net_pnl_rupees).toLocaleString('en-IN')}`}
                </div>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-100">
                <div className="text-[10px] uppercase font-bold text-slate-400">Return %</div>
                <div className={`text-base font-black ${selectedDay.return_pct >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                  {selectedDay.return_pct}%
                </div>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-100">
                <div className="text-[10px] uppercase font-bold text-slate-400">Win Rate</div>
                <div className="text-base font-black text-slate-800">{selectedDay.win_rate}%</div>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-100">
                <div className="text-[10px] uppercase font-bold text-slate-400">Friction Fees</div>
                <div className="text-base font-black text-slate-700">₹{selectedDay.fees_rupees.toFixed(2)}</div>
              </div>
            </div>

            {/* Launch Replay Action Banner */}
            <div className="bg-gradient-to-r from-amber-500/10 via-indigo-500/10 to-amber-500/10 border border-amber-500/30 rounded-2xl p-4 flex items-center justify-between">
              <div>
                <div className="text-xs font-black text-slate-900 flex items-center space-x-1.5">
                  <FastForward size={14} className="text-amber-600" />
                  <span>Timeline Replay Mode</span>
                </div>
                <div className="text-[11px] text-slate-600">
                  Replay how your equity curve evolved trade-by-trade on {selectedDay.date}.
                </div>
              </div>
              <button
                type="button"
                onClick={() => {
                  setDayInspectorOpen(false);
                  handleOpenReplay(selectedDay.date);
                }}
                className="flex items-center space-x-1.5 px-4 py-2 bg-slate-900 hover:bg-slate-800 text-white text-xs font-black rounded-xl transition-all shadow-sm"
              >
                <Play size={13} />
                <span>Replay Day</span>
              </button>
            </div>

            {/* Trades List */}
            <div className="space-y-2">
              <div className="text-xs font-black uppercase tracking-wider text-slate-500">
                Trades Executed ({selectedDay.trades?.length || 0})
              </div>
              <div className="divide-y divide-slate-100 border border-slate-100 rounded-2xl overflow-hidden max-h-60 overflow-y-auto">
                {(selectedDay.trades || []).map((t) => {
                  const isWin = (t.net_pnl || 0) > 0;
                  return (
                    <div key={t.id} className="p-3 flex items-center justify-between hover:bg-slate-50 text-xs">
                      <div>
                        <div className="flex items-center space-x-2">
                          <span className="font-bold text-slate-900">{t.ticker.replace('.NS', '')}</span>
                          <span
                            className={`px-1.5 py-0.5 text-[9px] font-black rounded ${
                              t.direction.includes('BUY') ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                            }`}
                          >
                            {t.direction}
                          </span>
                          <span className="text-[10px] text-slate-400">({t.shares} shares)</span>
                        </div>
                        <div className="text-[11px] text-slate-500 mt-0.5">
                          Entry: ₹{t.entry_price.toFixed(2)} → Exit: ₹{t.exit_price ? t.exit_price.toFixed(2) : '-'} (
                          {t.outcome})
                        </div>
                      </div>
                      <div className="text-right">
                        <div className={`font-black ${isWin ? 'text-emerald-600' : 'text-rose-600'}`}>
                          {isWin ? `+₹${t.net_pnl.toFixed(2)}` : `-₹${Math.abs(t.net_pnl).toFixed(2)}`}
                        </div>
                        <div className="text-[10px] text-slate-400">{t.exit_time?.slice(11, 19)}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── MODAL 5: HISTORICAL DAY REPLAY PLAYER ─────────────────────────── */}
      {replayModalOpen && replayData && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-sm p-4">
          <div className="bg-slate-900 border border-slate-800 rounded-3xl p-6 max-w-2xl w-full text-white shadow-2xl space-y-5 animate-in fade-in zoom-in-95 duration-200">
            {/* Header */}
            <div className="flex items-center justify-between border-b border-slate-800 pb-4">
              <div>
                <div className="flex items-center space-x-2">
                  <Play className="text-amber-400" size={20} />
                  <h3 className="text-base font-black">Historical Day Replayer: {replayData.date}</h3>
                </div>
                <p className="text-xs text-slate-400 mt-0.5">
                  100% Local SQLite OHLCV candles (0 Upstox API calls consumed)
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setReplayPlaying(false);
                  setReplayModalOpen(false);
                }}
                className="text-slate-400 hover:text-white p-1"
              >
                ✕
              </button>
            </div>

            {/* Current Tick Metrics Display */}
            {currentReplayPoint && (
              <div className="grid grid-cols-3 gap-4 bg-slate-800/80 border border-slate-700/80 rounded-2xl p-4">
                <div>
                  <div className="text-[10px] font-bold uppercase text-slate-400">Timestamp</div>
                  <div className="text-sm font-black text-amber-400 mt-0.5">
                    {currentReplayPoint.timestamp}
                  </div>
                  <div className="text-[10px] text-slate-400 italic truncate mt-0.5">
                    {currentReplayPoint.event}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] font-bold uppercase text-slate-400">Simulated Equity</div>
                  <div className="text-base font-black text-white mt-0.5">
                    ₹{currentReplayPoint.equity.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                  </div>
                  <div className={`text-[11px] font-bold ${currentReplayPoint.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {currentReplayPoint.pnl >= 0 ? `+₹${currentReplayPoint.pnl.toFixed(2)}` : `-₹${Math.abs(currentReplayPoint.pnl).toFixed(2)}`}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] font-bold uppercase text-slate-400">Cash & Open</div>
                  <div className="text-sm font-black text-slate-300 mt-0.5">
                    Cash: ₹{currentReplayPoint.cash.toLocaleString('en-IN')}
                  </div>
                  <div className="text-[11px] font-semibold text-slate-400">
                    {currentReplayPoint.open_positions} Positions Open
                  </div>
                </div>
              </div>
            )}

            {/* Mini Replay Chart */}
            <div className="h-44 w-full bg-slate-950/60 rounded-2xl p-2 border border-slate-800">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={replayData.timeline.slice(0, replayIndex + 1)}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#1e293b" />
                  <XAxis dataKey="timestamp" hide />
                  <YAxis domain={['auto', 'auto']} hide />
                  <Line
                    type="monotone"
                    dataKey="equity"
                    stroke="#f59e0b"
                    strokeWidth={2.5}
                    dot={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>

            {/* Timeline Progress Slider */}
            <div className="space-y-1.5">
              <div className="flex justify-between text-[11px] text-slate-400 font-bold">
                <span>Progress: Step {replayIndex + 1} of {replayData.timeline?.length || 0}</span>
                <span>{Math.round(((replayIndex + 1) / (replayData.timeline?.length || 1)) * 100)}%</span>
              </div>
              <input
                type="range"
                min={0}
                max={Math.max(0, (replayData.timeline?.length || 1) - 1)}
                value={replayIndex}
                onChange={(e) => {
                  setReplayPlaying(false);
                  setReplayIndex(Number(e.target.value));
                }}
                className="w-full accent-amber-500 cursor-pointer"
              />
            </div>

            {/* Replay Controls & Speed Multiplex */}
            <div className="flex items-center justify-between pt-2">
              <div className="flex items-center space-x-2">
                <button
                  type="button"
                  onClick={() => {
                    setReplayPlaying(false);
                    setReplayIndex(0);
                  }}
                  className="p-2.5 bg-slate-800 hover:bg-slate-700 rounded-xl text-slate-300"
                  title="Reset Timeline"
                >
                  <RotateCcw size={15} />
                </button>
                <button
                  type="button"
                  onClick={() => setReplayPlaying(!replayPlaying)}
                  className="flex items-center space-x-1.5 px-5 py-2.5 bg-amber-500 hover:bg-amber-400 text-slate-950 font-black text-xs uppercase tracking-wider rounded-xl transition-all shadow-md"
                >
                  {replayPlaying ? <Pause size={15} /> : <Play size={15} />}
                  <span>{replayPlaying ? 'Pause Replay' : 'Play Replay'}</span>
                </button>
              </div>

              {/* Speed Toggles */}
              <div className="flex items-center space-x-1 bg-slate-800 p-1 rounded-xl">
                {[1, 5, 10].map((spd) => (
                  <button
                    key={spd}
                    type="button"
                    onClick={() => setReplaySpeed(spd)}
                    className={`px-2.5 py-1 text-xs font-black rounded-lg transition-all ${
                      replaySpeed === spd ? 'bg-amber-500 text-slate-950' : 'text-slate-400 hover:text-white'
                    }`}
                  >
                    {spd}x
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
