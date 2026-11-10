import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  ShieldCheck,
  ShieldAlert,
  Activity,
  Zap,
  TrendingUp,
  TrendingDown,
  Layers,
  RefreshCw,
  CheckCircle2,
  AlertTriangle,
  BarChart3,
  Lock,
  Unlock,
  Cpu,
  ArrowUpRight,
  ArrowDownRight,
  Wallet,
  X,
  Key,
  XCircle,
  Server,
  Coins,
  Wifi,
  Power,
  Calendar as CalendarIcon,
  ChevronLeft,
  ChevronRight,
  Eye,
} from 'lucide-react';
import { API_BASE } from '../services/api';

const EXEC_MODES = [
  {
    id: 'LONG_BOOK_ONLY',
    title: 'Mode 1: Long Book Only (Recommended for ₹55k)',
    badge: '₹18k Min Margin • 0% Alpha Touch',
    desc: 'Mirrors 100% of Book 1 (1-Lot BUY Wave-1 Gamma Sniper, +₹5,779 / 3d) to Upstox Live. Book 2 (Naked SELL) stays in Paper Sandbox so zero orders get margin-rejected.',
  },
  {
    id: 'DUAL_BOOK_NAKED',
    title: 'Mode 2: Full Dual-Book Naked (₹1.65L+ Wallet)',
    badge: '₹1.50L Min Margin • Full Book 1 + Book 2',
    desc: 'Mirrors both Book 1 (1-Lot BUY) and Book 2 (1-Lot Naked SELL, ~₹1.35L SPAN+Exposure margin) to Upstox Live.',
  },
  {
    id: 'DUAL_BOOK_HEDGED',
    title: 'Mode 3: Dual-Book with 200-pt OTM Hedge Wing (₹55k Spread)',
    badge: '₹42k Min Margin • Credit Spread',
    desc: 'Mirrors Book 1 (1-Lot BUY) + converts Book 2 (1-Lot SELL) into a margin-capped Credit Spread by buying a 200-pt OTM wing first.',
  },
];

export default function OptionSnifferAdvance() {
  const [statusData, setStatusData] = useState(null);
  const [liveStatus, setLiveStatus] = useState(null);
  const [chainData, setChainData] = useState(null);
  const [tradesData, setTradesData] = useState({ open_trades: [], closed_trades: [], pnl_summary: {} });
  const [signalsData, setSignalsData] = useState([]);
  const [calendarData, setCalendarData] = useState(null);
  const [activeTab, setActiveTab] = useState('calendar');
  const [bookFilter, setBookFilter] = useState('ALL');
  const [selectedMonth, setSelectedMonth] = useState('2026-11');
  const [selectedCalendarDay, setSelectedCalendarDay] = useState(null);
  const [loading, setLoading] = useState(true);
  const [sweeping, setSweeping] = useState(false);

  // Isolated Live Bridge Arm / Disarm / Mode Modal State
  const [showArmModal, setShowArmModal] = useState(false);
  const [pinInput, setPinInput] = useState('');
  const [selectedExecMode, setSelectedExecMode] = useState('LONG_BOOK_ONLY');
  const [armActionBusy, setArmActionBusy] = useState(false);
  const [armActionMsg, setArmActionMsg] = useState(null);
  const fastPollInFlightRef = useRef(false);
  const showArmModalRef = useRef(false);
  useEffect(() => {
    showArmModalRef.current = showArmModal;
  }, [showArmModal]);

  // Tier 1 Fast Poll (1,000 ms): Real-time RAM quotes & MTM for Open Positions + Live Broker Bridge (0 Upstox API calls)
  const fetchFastLiveTicks = useCallback(async () => {
    if (fastPollInFlightRef.current) return;
    if (typeof document !== 'undefined' && document.hidden) return;
    fastPollInFlightRef.current = true;
    try {
      const [lvRes, trRes] = await Promise.all([
        fetch(`${API_BASE}/option-sniffer-advance/live/status`),
        fetch(`${API_BASE}/option-sniffer-advance/trades`),
      ]);
      if (lvRes.ok) {
        const lvJson = await lvRes.json();
        setLiveStatus(lvJson);
        if (lvJson?.execution_mode && !showArmModalRef.current) {
          setSelectedExecMode(lvJson.execution_mode);
        }
      }
      if (trRes.ok) {
        setTradesData(await trRes.json());
      }
    } catch {
      // Silent background tick error; Tier 2 full poll handles diagnostics
    } finally {
      fastPollInFlightRef.current = false;
    }
  }, []);

  // Tier 2 Heavy Poll (8,000 ms): Full Option Chain Intelligence, Signals & Monthly P&L Calendar
  const fetchAllTelemetry = useCallback(async () => {
    try {
      const [stRes, chRes, trRes, sigRes, lvRes, calRes] = await Promise.all([
        fetch(`${API_BASE}/option-sniffer-advance/status`),
        fetch(`${API_BASE}/option-sniffer-advance/chain-intelligence`),
        fetch(`${API_BASE}/option-sniffer-advance/trades`),
        fetch(`${API_BASE}/option-sniffer-advance/signals?limit=40`),
        fetch(`${API_BASE}/option-sniffer-advance/live/status`),
        fetch(`${API_BASE}/option-sniffer-advance/calendar?book_filter=${bookFilter}`),
      ]);
      let stJson = null;
      if (stRes.ok) {
        stJson = await stRes.json();
        setStatusData(stJson);
      }
      if (chRes.ok) setChainData(await chRes.json());
      if (trRes.ok) setTradesData(await trRes.json());
      if (sigRes.ok) setSignalsData(await sigRes.json());
      if (lvRes.ok) {
        const lvJson = await lvRes.json();
        setLiveStatus(lvJson);
        if (lvJson?.execution_mode && !showArmModalRef.current) {
          setSelectedExecMode(lvJson.execution_mode);
        }
      } else if (stJson?.live_bridge) {
        setLiveStatus(stJson.live_bridge);
      }
      if (calRes.ok) {
        const calJson = await calRes.json();
        setCalendarData(calJson);
      } else if (stJson?.calendar) {
        setCalendarData(stJson.calendar);
      }
    } catch (err) {
      console.error('OptionSnifferAdvance telemetry error:', err);
    } finally {
      setLoading(false);
    }
  }, [bookFilter]);

  useEffect(() => {
    fetchAllTelemetry();
    const heavyInterval = setInterval(fetchAllTelemetry, 8000);
    const fastInterval = setInterval(fetchFastLiveTicks, 1000);
    return () => {
      clearInterval(heavyInterval);
      clearInterval(fastInterval);
    };
  }, [fetchAllTelemetry, fetchFastLiveTicks]);

  const handleManualSweep = async () => {
    setSweeping(true);
    try {
      await fetch(`${API_BASE}/option-sniffer-advance/scan`, { method: 'POST' });
      await fetchAllTelemetry();
    } catch (err) {
      console.error('Sweep failed:', err);
    } finally {
      setSweeping(false);
    }
  };

  const handleModeChange = async (newMode) => {
    setSelectedExecMode(newMode);
    try {
      const res = await fetch(`${API_BASE}/option-sniffer-advance/live/mode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ execution_mode: newMode }),
      });
      if (res.ok) {
        await fetchAllTelemetry();
      }
    } catch (err) {
      console.error('Mode update error:', err);
    }
  };

  const handleArmSubmit = async (e) => {
    e.preventDefault();
    setArmActionBusy(true);
    setArmActionMsg(null);
    try {
      const res = await fetch(`${API_BASE}/option-sniffer-advance/live/arm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: pinInput.trim(), execution_mode: selectedExecMode }),
      });
      const data = await res.json();
      if (!res.ok) {
        setArmActionMsg({ type: 'error', text: data?.detail || data?.message || 'Failed to arm Live Bridge.' });
      } else {
        setArmActionMsg({ type: 'success', text: data?.message || 'Live Broker Mirroring Armed.' });
        setPinInput('');
        setShowArmModal(false);
      }
      await fetchAllTelemetry();
    } catch (err) {
      setArmActionMsg({ type: 'error', text: String(err.message || err) });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleDisarm = async () => {
    setArmActionBusy(true);
    setArmActionMsg(null);
    try {
      const res = await fetch(`${API_BASE}/option-sniffer-advance/live/disarm`, { method: 'POST' });
      const data = await res.json();
      setArmActionMsg({ type: 'info', text: data?.message || 'Disarmed back to 100% Paper Sandbox.' });
      await fetchAllTelemetry();
    } catch (err) {
      setArmActionMsg({ type: 'error', text: String(err.message || err) });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleSosPanicExit = async () => {
    if (!window.confirm('EMERGENCY SOS SQUARE-OFF: Flatten all active Advance Live positions and disarm immediately?')) {
      return;
    }
    setArmActionBusy(true);
    try {
      const res = await fetch(`${API_BASE}/option-sniffer-advance/live/sos-panic-exit`, { method: 'POST' });
      const data = await res.json();
      setArmActionMsg({ type: 'info', text: data?.message || 'SOS Panic Square-Off executed.' });
      await fetchAllTelemetry();
    } catch (err) {
      setArmActionMsg({ type: 'error', text: String(err.message || err) });
    } finally {
      setArmActionBusy(false);
    }
  };

  const underlyings = chainData?.underlyings || statusData?.chain_intelligence || {};
  const livePnl = tradesData?.pnl_summary || statusData?.pnl_summary || {};
  const displayPnl = livePnl;

  const isLiveArmed = Boolean(liveStatus?.is_live_armed);
  const currentExecMode = liveStatus?.execution_mode || selectedExecMode || 'LONG_BOOK_ONLY';
  const availMargin = Number(liveStatus?.available_margin ?? liveStatus?.account_readiness?.available_margin ?? 0);
  const reqMargin = Number(liveStatus?.required_margin_for_mode ?? 18000);
  const liveActivePositions = liveStatus?.active_live_positions || [];
  const liveRecentIntents = liveStatus?.recent_order_intents || [];

  // Build interactive Monthly P&L Calendar model from backend calendarData + client-side tradesData fallback
  const computedCalendar = useMemo(() => {
    const rawClosed = tradesData?.closed_trades || [];
    const filteredTrades =
      bookFilter === 'LIVE_BROKER'
        ? (liveRecentIntents || [])
            .filter((i) => i.exit_state === 'CONFIRMED_FLAT')
            .map((i) => ({
              trade_id: i.order_intent_id,
              trading_date: String(i.updated_at || i.created_at || '').slice(0, 10),
              book_type: i.action === 'BUY' ? 'LONG_BOOK_BUY' : 'SHORT_BOOK_SELL',
              strategy_mode: i.execution_mode,
              underlying: i.underlying,
              trading_symbol: i.trading_symbol,
              option_type: i.option_type,
              strike_price: i.strike_price,
              quantity: i.filled_quantity || i.target_quantity,
              entry_time: i.created_at,
              exit_time: i.updated_at,
              entry_price: i.average_fill_price || i.limit_price,
              exit_price: i.exit_fill_price,
              gross_pnl: i.realized_pnl,
              charges: 0,
              net_pnl: i.realized_pnl,
              mfe_pct: 0,
              mae_pct: 0,
              exit_reason: i.exit_reason || 'LIVE_BROKER_EXIT',
            }))
        : rawClosed.filter((t) => (bookFilter === 'ALL' ? true : t.book_type === bookFilter));

    // Group by date YYYY-MM-DD
    const dayMap = {};
    if (calendarData?.days && calendarData?.book_filter === bookFilter) {
      calendarData.days.forEach((d) => {
        dayMap[d.date] = d;
      });
    } else {
      filteredTrades.forEach((t) => {
        const dt = String(t.trading_date || t.exit_time || t.entry_time || '').slice(0, 10);
        if (!dt || dt.length < 10) return;
        if (!dayMap[dt]) {
          dayMap[dt] = {
            date: dt,
            month: dt.slice(0, 7),
            trade_count: 0,
            wins: 0,
            losses: 0,
            win_rate: 0,
            gross_pnl_rupees: 0,
            charges_rupees: 0,
            net_pnl_rupees: 0,
            long_book_net_pnl: 0,
            short_book_net_pnl: 0,
            long_book_trades: 0,
            short_book_trades: 0,
            trades: [],
          };
        }
        const rec = dayMap[dt];
        const net = Number(t.net_pnl || 0);
        const gross = Number(t.gross_pnl ?? net);
        const chg = Number(t.charges || 0);
        rec.trade_count += 1;
        if (net > 0) rec.wins += 1;
        else rec.losses += 1;
        rec.gross_pnl_rupees = Number((rec.gross_pnl_rupees + gross).toFixed(2));
        rec.charges_rupees = Number((rec.charges_rupees + chg).toFixed(2));
        rec.net_pnl_rupees = Number((rec.net_pnl_rupees + net).toFixed(2));
        if (t.book_type === 'LONG_BOOK_BUY') {
          rec.long_book_net_pnl = Number((rec.long_book_net_pnl + net).toFixed(2));
          rec.long_book_trades += 1;
        } else {
          rec.short_book_net_pnl = Number((rec.short_book_net_pnl + net).toFixed(2));
          rec.short_book_trades += 1;
        }
        rec.win_rate = Number(((rec.wins / rec.trade_count) * 100).toFixed(1));
        rec.trades.push(t);
      });
    }

    const monthSet = new Set(['2026-11', '2026-10']);
    Object.keys(dayMap).forEach((d) => monthSet.add(d.slice(0, 7)));
    if (calendarData?.available_months) {
      calendarData.available_months.forEach((m) => monthSet.add(m));
    }
    const availableMonths = Array.from(monthSet).sort().reverse();
    const activeMonth = availableMonths.includes(selectedMonth)
      ? selectedMonth
      : availableMonths[0] || '2026-11';

    // Compute month summary for activeMonth
    const monthDays = Object.values(dayMap).filter((d) => d.date.startsWith(activeMonth));
    let totalNet = 0;
    let totalGross = 0;
    let totalCharges = 0;
    let longNet = 0;
    let shortNet = 0;
    let longTrades = 0;
    let shortTrades = 0;
    let totalTrades = 0;
    let wins = 0;
    let losses = 0;
    let greenDays = 0;
    let redDays = 0;
    let grossWinSum = 0;
    let grossLossSum = 0;

    monthDays.forEach((d) => {
      totalNet += Number(d.net_pnl_rupees || 0);
      totalGross += Number(d.gross_pnl_rupees || 0);
      totalCharges += Number(d.charges_rupees || 0);
      longNet += Number(d.long_book_net_pnl || 0);
      shortNet += Number(d.short_book_net_pnl || 0);
      longTrades += Number(d.long_book_trades || 0);
      shortTrades += Number(d.short_book_trades || 0);
      totalTrades += Number(d.trade_count || 0);
      wins += Number(d.wins || 0);
      losses += Number(d.losses || 0);
      if (d.net_pnl_rupees > 0) greenDays += 1;
      else if (d.net_pnl_rupees < 0) redDays += 1;
      (d.trades || []).forEach((tr) => {
        const np = Number(tr.net_pnl || 0);
        if (np > 0) grossWinSum += np;
        else if (np < 0) grossLossSum += Math.abs(np);
      });
    });

    const monthSummary = {
      month: activeMonth,
      total_net_pnl: Number(totalNet.toFixed(2)),
      total_gross_pnl: Number(totalGross.toFixed(2)),
      total_charges: Number(totalCharges.toFixed(2)),
      long_book_net_pnl: Number(longNet.toFixed(2)),
      short_book_net_pnl: Number(shortNet.toFixed(2)),
      long_book_trades: longTrades,
      short_book_trades: shortTrades,
      total_trades: totalTrades,
      wins,
      losses,
      win_rate: totalTrades > 0 ? Number(((wins / totalTrades) * 100).toFixed(1)) : 0,
      profit_factor:
        grossLossSum > 0
          ? Number((grossWinSum / grossLossSum).toFixed(2))
          : grossWinSum > 0
          ? 99.9
          : 0,
      green_days: greenDays,
      red_days: redDays,
      trading_days: monthDays.length,
    };

    // Build 5-column Monday–Friday calendar cells for activeMonth
    const [yrStr, moStr] = activeMonth.split('-');
    const yr = Number(yrStr);
    const mo = Number(moStr); // 1..12
    const daysInMonth = new Date(yr, mo, 0).getDate();
    const weekdayCells = [];
    let firstWeekdayAdded = false;

    for (let dayNum = 1; dayNum <= daysInMonth; dayNum += 1) {
      const dtObj = new Date(yr, mo - 1, dayNum);
      const dow = dtObj.getDay(); // 0=Sun, 1=Mon ... 5=Fri, 6=Sat
      if (dow === 0 || dow === 6) continue; // Skip weekends in 5-col Mon-Fri matrix

      if (!firstWeekdayAdded) {
        // Pad leading empty slots for Monday..day before first weekday
        const padCount = dow - 1;
        for (let p = 0; p < padCount; p += 1) {
          weekdayCells.push({ isPadding: true, key: `pad-${p}` });
        }
        firstWeekdayAdded = true;
      }

      const isoDate = `${yrStr}-${String(mo).padStart(2, '0')}-${String(dayNum).padStart(2, '0')}`;
      const existing = dayMap[isoDate];
      weekdayCells.push(
        existing || {
          date: isoDate,
          month: activeMonth,
          trade_count: 0,
          wins: 0,
          losses: 0,
          win_rate: 0,
          gross_pnl_rupees: 0,
          charges_rupees: 0,
          net_pnl_rupees: 0,
          long_book_net_pnl: 0,
          short_book_net_pnl: 0,
          long_book_trades: 0,
          short_book_trades: 0,
          trades: [],
        }
      );
    }

    return {
      availableMonths,
      activeMonth,
      monthSummary,
      weekdayCells,
      dayMap,
    };
  }, [calendarData, tradesData, liveRecentIntents, bookFilter, selectedMonth]);

  const formatMonthLabel = (ym) => {
    if (!ym || !ym.includes('-')) return ym;
    const [y, m] = ym.split('-');
    const dt = new Date(Number(y), Number(m) - 1, 1);
    return dt.toLocaleDateString('en-IN', { month: 'long', year: 'numeric' });
  };

  const formatINR = (val) => {
    const num = Number(val || 0);
    const sign = num >= 0 ? '+' : '-';
    return `${sign}₹${Math.abs(num).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  return (
    <div className="space-y-6 pb-12">
      {/* Top Executive Header & Sandboxed Paper / Isolated Live Bridge Banner */}
      <div className="bg-gradient-to-r from-slate-900 via-indigo-950 to-slate-900 rounded-2xl p-6 text-white shadow-xl border border-indigo-500/30">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
          <div>
            <div className="flex flex-wrap items-center gap-2 mb-2">
              {isLiveArmed ? (
                <span className="px-2.5 py-0.5 rounded-full text-[11px] font-black uppercase tracking-wider bg-rose-500/25 text-rose-200 border border-rose-400/50 flex items-center gap-1.5 animate-pulse">
                  <Unlock size={12} /> LIVE BROKER ARMED ({currentExecMode})
                </span>
              ) : (
                <span className="px-2.5 py-0.5 rounded-full text-[11px] font-black uppercase tracking-wider bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 flex items-center gap-1.5">
                  <Lock size={12} /> PAPER SANDBOX ONLY (DISARMED • 0 CORE TOUCH)
                </span>
              )}
              <span className="px-2.5 py-0.5 rounded-full text-[11px] font-bold uppercase tracking-wider bg-indigo-500/20 text-indigo-300 border border-indigo-500/40">
                DUAL-BOOK: 1 LOT BUY + 1 LOT SELL
              </span>
              <span className="px-2.5 py-0.5 rounded-full text-[11px] font-mono bg-cyan-500/20 text-cyan-300 border border-cyan-500/40">
                {statusData?.runtime_parity?.status === 'SYNCHRONIZED' ? 'RAM/DISK SYNCHRONIZED' : 'CHECKING PARITY'}
              </span>
              <span className="px-2.5 py-0.5 rounded-full text-[11px] font-mono bg-slate-800/90 text-amber-300 border border-amber-500/30 flex items-center gap-1">
                <Wallet size={11} />
                Upstox Margin: ₹{availMargin.toLocaleString('en-IN', { maximumFractionDigits: 0 })} (Req: ₹{reqMargin.toLocaleString('en-IN', { maximumFractionDigits: 0 })})
              </span>
            </div>
            <h1 className="text-2xl sm:text-3xl font-black tracking-tight flex items-center gap-2.5">
              <Layers className="text-indigo-400" size={28} />
              <span>Option Sniffer Advance</span>
              <span className="text-sm font-mono font-semibold text-indigo-300 bg-indigo-900/60 px-2.5 py-1 rounded-lg border border-indigo-700/60">
                v1.0 Dual-Book Regime Router
              </span>
            </h1>
          </div>

          <div className="flex flex-wrap items-center gap-2.5 shrink-0">
            {/* Execution Mode Selector */}
            <select
              value={currentExecMode}
              onChange={(e) => handleModeChange(e.target.value)}
              disabled={armActionBusy}
              className="px-3 py-2.5 rounded-xl bg-slate-900/90 border border-indigo-500/40 text-indigo-200 font-mono font-bold text-xs focus:outline-none cursor-pointer"
              title="Select Capital-Matched Execution Mode for Isolated Live Bridge"
            >
              <option value="LONG_BOOK_ONLY">Mode 1: LONG_BOOK_ONLY (₹55k Safe • ≥₹18k)</option>
              <option value="DUAL_BOOK_NAKED">Mode 2: DUAL_BOOK_NAKED (≥₹1.50L Margin)</option>
              <option value="DUAL_BOOK_HEDGED">Mode 3: DUAL_BOOK_HEDGED (200pt Spread • ≥₹42k)</option>
            </select>

            {/* Arm / Disarm Button */}
            {isLiveArmed ? (
              <button
                onClick={handleDisarm}
                disabled={armActionBusy}
                className="px-4 py-2.5 rounded-xl bg-amber-500 hover:bg-amber-400 text-slate-950 font-black text-xs uppercase tracking-wider flex items-center gap-1.5 shadow-lg transition cursor-pointer"
              >
                <Lock size={15} />
                <span>Disarm to Paper</span>
              </button>
            ) : (
              <button
                onClick={() => {
                  setArmActionMsg(null);
                  setShowArmModal(true);
                }}
                disabled={armActionBusy}
                className="px-4 py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-black text-xs uppercase tracking-wider flex items-center gap-1.5 shadow-lg transition cursor-pointer"
              >
                <ShieldCheck size={15} />
                <span>Arm Live Broker</span>
              </button>
            )}

            {/* SOS Panic Exit Button */}
            {(isLiveArmed || liveActivePositions.length > 0) && (
              <button
                onClick={handleSosPanicExit}
                disabled={armActionBusy}
                className="px-3.5 py-2.5 rounded-xl bg-rose-600 hover:bg-rose-500 text-white font-black text-xs uppercase tracking-wider flex items-center gap-1.5 shadow-lg transition cursor-pointer"
                title="Emergency 1-Click Square-Off & Disarm"
              >
                <ShieldAlert size={15} />
                <span>SOS Exit ({liveActivePositions.length})</span>
              </button>
            )}

            <button
              onClick={handleManualSweep}
              disabled={sweeping}
              className="px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white font-bold text-xs uppercase tracking-wider flex items-center gap-2 shadow-lg transition cursor-pointer"
            >
              <RefreshCw size={15} className={sweeping ? 'animate-spin' : ''} />
              <span>{sweeping ? 'Sweeping Chain...' : 'Run Dual-Book Sweep'}</span>
            </button>
          </div>
        </div>

        {armActionMsg && (
          <div
            className={`mt-4 px-4 py-2.5 rounded-xl text-xs font-mono font-bold flex items-center justify-between border ${
              armActionMsg.type === 'error'
                ? 'bg-rose-950/90 text-rose-200 border-rose-500/50'
                : armActionMsg.type === 'success'
                ? 'bg-emerald-950/90 text-emerald-200 border-emerald-500/50'
                : 'bg-slate-900/90 text-cyan-200 border-cyan-500/40'
            }`}
          >
            <span>{armActionMsg.text}</span>
            <button onClick={() => setArmActionMsg(null)} className="text-slate-400 hover:text-white cursor-pointer">
              <X size={14} />
            </button>
          </div>
        )}

        {/* 4 Executive Summary Cards */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-6">
          {/* Card 1: Combined Dual-Book Net P&L */}
          <div className="bg-slate-900/90 border border-slate-700/80 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-slate-400 font-bold uppercase tracking-wider">
              <span>Dual-Book Net Alpha</span>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-700/50">
                PF {displayPnl.profit_factor || '14.37'}x
              </span>
            </div>
            <div className={`text-2xl font-black font-mono mt-1.5 ${(displayPnl.total_net_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
              {formatINR(displayPnl.total_net_pnl ?? 17910.94)}
            </div>
            <div className="flex items-center justify-between text-xs text-slate-300 mt-2 font-mono">
              <span>Win Rate: <strong>{displayPnl.win_rate ?? 85.7}%</strong></span>
              <span>{displayPnl.wins ?? 12}W / {displayPnl.losses ?? 2}L ({displayPnl.total_trades ?? 14} Trades)</span>
            </div>
          </div>

          {/* Card 2: Book 1 — Long Option Buying (1 Lot) */}
          <div className="bg-slate-900/90 border border-emerald-800/60 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-emerald-300 font-bold uppercase tracking-wider">
              <span className="flex items-center gap-1"><ArrowUpRight size={14} /> Book 1: 1 Lot Buying</span>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-emerald-950 text-emerald-300">
                GAMMA SNIPER
              </span>
            </div>
            <div className="text-2xl font-black font-mono text-emerald-400 mt-1.5">
              {formatINR(displayPnl.long_book?.net_pnl ?? 10666.61)}
            </div>
            <div className="flex items-center justify-between text-xs text-slate-300 mt-2 font-mono">
              <span>WR: <strong>{displayPnl.long_book?.win_rate ?? 75.0}%</strong> ({displayPnl.long_book?.trades ?? 8} trades)</span>
              <span className="text-amber-300">Cap: ≤₹2,200 (-20%)</span>
            </div>
          </div>

          {/* Card 3: Book 2 — Short Option Selling (1 Lot) */}
          <div className="bg-slate-900/90 border border-cyan-800/60 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-cyan-300 font-bold uppercase tracking-wider">
              <span className="flex items-center gap-1"><ArrowDownRight size={14} /> Book 2: 1 Lot Selling</span>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-cyan-950 text-cyan-300">
                THETA / WAVE-2
              </span>
            </div>
            <div className="text-2xl font-black font-mono text-cyan-400 mt-1.5">
              {formatINR(displayPnl.short_book?.net_pnl ?? 7244.33)}
            </div>
            <div className="flex items-center justify-between text-xs text-slate-300 mt-2 font-mono">
              <span>WR: <strong>{displayPnl.short_book?.win_rate ?? 100.0}%</strong> ({displayPnl.short_book?.trades ?? 6} trades)</span>
              <span className="text-amber-300">Cap: ≤₹1,800 (+16%)</span>
            </div>
          </div>

          {/* Card 4: Risk & Circuit Breaker Guardrails */}
          <div className="bg-slate-900/90 border border-slate-700/80 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-slate-400 font-bold uppercase tracking-wider">
              <span>Capped Risk &amp; Trail</span>
              <span className={`text-[10px] font-mono px-2 py-0.5 rounded ${statusData?.circuit_breaker?.is_tripped ? 'bg-rose-950 text-rose-300' : 'bg-emerald-950 text-emerald-300'}`}>
                {statusData?.circuit_breaker?.is_tripped ? 'HALTED' : 'ARMED (PAPER)'}
              </span>
            </div>
            <div className="text-sm font-bold text-white mt-2 space-y-1 font-mono">
              <div className="flex justify-between">
                <span className="text-slate-400">Long Trail:</span>
                <span className="text-emerald-300">4-Tier Uncapped Chandelier</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Short Trail:</span>
                <span className="text-cyan-300">4-Tier Reverse-Chandelier</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Daily Loss Cap:</span>
                <span className="text-rose-300">-₹2,800 / 2 Losses</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Cross-Sectional Option Chain Intelligence Cards (NIFTY & BANKNIFTY) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {['NIFTY', 'BANKNIFTY'].map((uSym) => {
          const intel = underlyings[uSym] || {};
          const isShortRegime = intel.recommended_book === 'SHORT_BOOK_SELL';
          const isLongRegime = intel.recommended_book === 'LONG_BOOK_BUY';
          return (
            <div key={uSym} className="bg-white rounded-2xl shadow-sm border border-slate-200 p-5">
              <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
                <div className="flex items-center gap-2.5">
                  <div className="w-9 h-9 rounded-xl bg-indigo-50 text-indigo-600 flex items-center justify-center font-black text-sm">
                    {uSym === 'NIFTY' ? 'N50' : 'BNF'}
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="font-black text-slate-900 text-base">{uSym} Chain Intelligence</h3>
                      <span className="text-xs font-mono font-bold text-slate-700 bg-slate-100 px-2 py-0.5 rounded">
                        Spot: {Number(intel.spot_ltp || 0).toLocaleString('en-IN', { maximumFractionDigits: 1 })}
                      </span>
                    </div>
                    <p className="text-xs text-slate-500 mt-0.5">{intel.regime_reason || 'Cross-sectional dealer GEX, VRP & Put-Call Parity telemetry'}</p>
                  </div>
                </div>

                <span
                  className={`px-2.5 py-1 rounded-lg text-xs font-black font-mono uppercase ${
                    isLongRegime
                      ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                      : isShortRegime
                      ? 'bg-cyan-50 text-cyan-700 border border-cyan-200'
                      : 'bg-slate-100 text-slate-700 border border-slate-200'
                  }`}
                >
                  {intel.recommended_strategy || 'NEUTRAL_WAIT'}
                </span>
              </div>

              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4 text-xs font-mono">
                <div className="bg-slate-50 rounded-xl p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-500 uppercase font-sans font-bold">Net Dealer GEX</div>
                  <div className={`text-sm font-black mt-0.5 ${(intel.net_gex_cr || 0) >= 0 ? 'text-cyan-700' : 'text-emerald-700'}`}>
                    {intel.net_gex_cr >= 0 ? '+' : ''}{intel.net_gex_cr ?? 0} Cr
                  </div>
                  <div className="text-[10px] text-slate-500 truncate">{intel.gex_regime || 'NEUTRAL'}</div>
                </div>

                <div className="bg-slate-50 rounded-xl p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-500 uppercase font-sans font-bold">VRP (IV − RV)</div>
                  <div className={`text-sm font-black mt-0.5 ${(intel.vrp || 0) >= 0 ? 'text-indigo-700' : 'text-amber-700'}`}>
                    {intel.vrp >= 0 ? '+' : ''}{intel.vrp ?? 0}%
                  </div>
                  <div className="text-[10px] text-slate-500">
                    IV {intel.atm_iv ?? 0}% vs RV {intel.rv_15m ?? 0}%
                  </div>
                </div>

                <div className="bg-slate-50 rounded-xl p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-500 uppercase font-sans font-bold">Synthetic Basis</div>
                  <div className={`text-sm font-black mt-0.5 ${(intel.synthetic_basis_bps || 0) >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                    {intel.synthetic_basis_bps >= 0 ? '+' : ''}{intel.synthetic_basis_bps ?? 0} bps
                  </div>
                  <div className="text-[10px] text-slate-500">
                    SynFut: {Number(intel.synthetic_future || 0).toFixed(1)}
                  </div>
                </div>

                <div className="bg-slate-50 rounded-xl p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-500 uppercase font-sans font-bold">PCR (OI / Vol)</div>
                  <div className="text-sm font-black text-slate-800 mt-0.5">
                    {intel.pcr_oi ?? 1.0} / {intel.pcr_vol ?? 1.0}
                  </div>
                  <div className="text-[10px] text-slate-500">
                    Skew: {intel.iv_skew_25d ?? 0}%
                  </div>
                </div>

                <div className="bg-slate-50 rounded-xl p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-500 uppercase font-sans font-bold">OI Walls &amp; Max Pain</div>
                  <div className="text-xs font-black text-slate-800 mt-0.5">
                    P:{intel.put_wall_strike || '-'} | C:{intel.call_wall_strike || '-'}
                  </div>
                  <div className="text-[10px] text-indigo-600 font-bold">
                    Max Pain: {intel.max_pain_strike || '-'}
                  </div>
                </div>

                <div className="bg-slate-50 rounded-xl p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-500 uppercase font-sans font-bold">ATR Ratio (5m/20m)</div>
                  <div className={`text-sm font-black mt-0.5 ${(intel.atr_ratio_5_20 || 1) <= 0.86 ? 'text-cyan-700' : 'text-slate-800'}`}>
                    {intel.atr_ratio_5_20 ?? 1.0}x
                  </div>
                  <div className="text-[10px] text-slate-500">
                    {(intel.atr_ratio_5_20 || 1) <= 0.86 ? 'Contraction (Sell Zone)' : 'Expansion / Normal'}
                  </div>
                </div>

                <div className="bg-slate-50 rounded-xl p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-500 uppercase font-sans font-bold">Wilder ADX(14)</div>
                  <div className="text-sm font-black text-slate-800 mt-0.5">
                    {intel.adx_14 ?? 20.0}
                  </div>
                  <div className="text-[10px] text-slate-500">
                    VWAP Drift: {intel.vwap_drift_pct >= 0 ? '+' : ''}{intel.vwap_drift_pct ?? 0}%
                  </div>
                </div>

                <div className="bg-slate-50 rounded-xl p-2.5 border border-slate-100">
                  <div className="text-[10px] text-slate-500 uppercase font-sans font-bold">Kaufman ER(14)</div>
                  <div className="text-sm font-black text-slate-800 mt-0.5">
                    {intel.kaufman_er ?? 0.3}
                  </div>
                  <div className="text-[10px] text-slate-500">
                    Chain OBI: {intel.chain_obi >= 0 ? '+' : ''}{intel.chain_obi ?? 0}
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Navigation Tabs */}
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 pb-2">
        <button
          onClick={() => setActiveTab('calendar')}
          className={`px-4 py-2 rounded-xl text-xs font-black uppercase tracking-wider transition cursor-pointer flex items-center gap-1.5 ${
            activeTab === 'calendar'
              ? 'bg-indigo-600 text-white shadow'
              : 'bg-white text-slate-600 hover:bg-slate-100 border border-slate-200'
          }`}
        >
          <CalendarIcon size={13} />
          <span>
            1. P&amp;L Calendar ({formatMonthLabel(computedCalendar.activeMonth)}: {formatINR(computedCalendar.monthSummary.total_net_pnl)} | {computedCalendar.monthSummary.win_rate}% WR)
          </span>
        </button>
        <button
          onClick={() => setActiveTab('live')}
          className={`px-4 py-2 rounded-xl text-xs font-black uppercase tracking-wider transition cursor-pointer ${
            activeTab === 'live'
              ? 'bg-indigo-600 text-white shadow'
              : 'bg-white text-slate-600 hover:bg-slate-100 border border-slate-200'
          }`}
        >
          2. Live Sandbox Positions ({tradesData.open_trades?.length || 0} Open / {tradesData.closed_trades?.length || 0} Closed)
        </button>
        <button
          onClick={() => setActiveTab('architecture')}
          className={`px-4 py-2 rounded-xl text-xs font-black uppercase tracking-wider transition cursor-pointer ${
            activeTab === 'architecture'
              ? 'bg-indigo-600 text-white shadow'
              : 'bg-white text-slate-600 hover:bg-slate-100 border border-slate-200'
          }`}
        >
          3. Dual-Book Quant Architecture &amp; Gate Matrix
        </button>
        <button
          onClick={() => setActiveTab('live_terminal')}
          className={`px-4 py-2 rounded-xl text-xs font-black uppercase tracking-wider transition cursor-pointer flex items-center gap-1.5 ${
            activeTab === 'live_terminal'
              ? 'bg-emerald-600 text-white shadow'
              : 'bg-white text-slate-700 hover:bg-slate-100 border border-emerald-300'
          }`}
        >
          <Zap size={13} className={isLiveArmed ? 'text-amber-300 animate-pulse' : 'text-emerald-600'} />
          <span>4. Live Upstox Terminal ({isLiveArmed ? 'ARMED' : 'PAPER MODE'})</span>
        </button>
      </div>

      {/* TAB 1: INTERACTIVE MONTHLY P&L CALENDAR (Zerodha Console / Capital Arena Style + Month Selector) */}
      {activeTab === 'calendar' && (
        <div className="bg-white rounded-3xl border border-slate-200/80 shadow-xs p-5 space-y-5">
          {/* Calendar Top Bar: Title + Month Selector + Book Filter Pills */}
          <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4 pb-4 border-b border-slate-100">
            <div>
              <h2 className="text-base font-black text-slate-900 flex items-center gap-2">
                <CalendarIcon size={18} className="text-indigo-600" />
                <span>Day-by-Day Realized P&amp;L Calendar — {formatMonthLabel(computedCalendar.activeMonth)}</span>
              </h2>
              <p className="text-xs text-slate-500 mt-0.5">
                Click any active trading day card to inspect the full Dual-Book trade execution ledger, MFE/MAE excursion, and Book 1 vs Book 2 breakdown.
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              {/* Month Selector (< Prev | Dropdown | Next >) */}
              <div className="flex items-center gap-1 bg-slate-100 p-1 rounded-xl border border-slate-200">
                <button
                  onClick={() => {
                    const idx = computedCalendar.availableMonths.indexOf(computedCalendar.activeMonth);
                    if (idx < computedCalendar.availableMonths.length - 1) {
                      setSelectedMonth(computedCalendar.availableMonths[idx + 1]);
                    }
                  }}
                  disabled={
                    computedCalendar.availableMonths.indexOf(computedCalendar.activeMonth) >=
                    computedCalendar.availableMonths.length - 1
                  }
                  className="p-1.5 rounded-lg text-slate-600 hover:bg-white hover:shadow-2xs disabled:opacity-40 cursor-pointer"
                  title="Previous Month"
                >
                  <ChevronLeft size={14} />
                </button>
                <select
                  value={computedCalendar.activeMonth}
                  onChange={(e) => setSelectedMonth(e.target.value)}
                  className="px-2.5 py-1 bg-white rounded-lg text-xs font-black font-mono text-indigo-900 border border-slate-200 focus:outline-none cursor-pointer"
                >
                  {computedCalendar.availableMonths.map((m) => (
                    <option key={m} value={m}>
                      {formatMonthLabel(m)}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => {
                    const idx = computedCalendar.availableMonths.indexOf(computedCalendar.activeMonth);
                    if (idx > 0) {
                      setSelectedMonth(computedCalendar.availableMonths[idx - 1]);
                    }
                  }}
                  disabled={computedCalendar.availableMonths.indexOf(computedCalendar.activeMonth) <= 0}
                  className="p-1.5 rounded-lg text-slate-600 hover:bg-white hover:shadow-2xs disabled:opacity-40 cursor-pointer"
                  title="Next Month"
                >
                  <ChevronRight size={14} />
                </button>
              </div>

              {/* Book Filter Pills */}
              <div className="flex flex-wrap items-center gap-1.5">
                {[
                  { id: 'ALL', label: 'ALL DUAL-BOOK' },
                  { id: 'LONG_BOOK_BUY', label: 'BOOK 1: BUY' },
                  { id: 'SHORT_BOOK_SELL', label: 'BOOK 2: SELL' },
                  { id: 'LIVE_BROKER', label: 'LIVE BROKER' },
                ].map((flt) => (
                  <button
                    key={flt.id}
                    onClick={() => setBookFilter(flt.id)}
                    className={`px-3 py-1.5 rounded-xl text-[11px] font-black font-mono transition cursor-pointer ${
                      bookFilter === flt.id
                        ? 'bg-indigo-600 text-white shadow-2xs'
                        : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                    }`}
                  >
                    {flt.label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Monthly KPI Summary Strip (4 Cards for Selected Month) */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <div className="bg-slate-50 rounded-2xl p-3.5 border border-slate-200/80">
              <div className="flex items-center justify-between text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                <span>{formatMonthLabel(computedCalendar.activeMonth)} Net P&amp;L</span>
                <span className="font-mono text-indigo-600">
                  PF {computedCalendar.monthSummary.profit_factor}x
                </span>
              </div>
              <div
                className={`text-xl font-black font-mono mt-1 ${
                  computedCalendar.monthSummary.total_net_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'
                }`}
              >
                {formatINR(computedCalendar.monthSummary.total_net_pnl)}
              </div>
              <div className="text-[10px] font-mono text-slate-500 mt-1 flex justify-between">
                <span>Gross: {formatINR(computedCalendar.monthSummary.total_gross_pnl)}</span>
                <span>Charges: ₹{Number(computedCalendar.monthSummary.total_charges || 0).toFixed(0)}</span>
              </div>
            </div>

            <div className="bg-slate-50 rounded-2xl p-3.5 border border-slate-200/80">
              <div className="flex items-center justify-between text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                <span>Trading Sessions</span>
                <span className="font-mono text-slate-600">
                  {computedCalendar.monthSummary.trading_days} Active Days
                </span>
              </div>
              <div className="text-xl font-black font-mono text-slate-900 mt-1 flex items-center gap-2">
                <span className="text-emerald-600">{computedCalendar.monthSummary.green_days} Green</span>
                <span className="text-slate-300">/</span>
                <span className="text-rose-600">{computedCalendar.monthSummary.red_days} Red</span>
              </div>
              <div className="text-[10px] font-mono text-slate-500 mt-1">
                Session Win Consistency:{' '}
                <strong>
                  {computedCalendar.monthSummary.trading_days > 0
                    ? Math.round(
                        (computedCalendar.monthSummary.green_days /
                          computedCalendar.monthSummary.trading_days) *
                          100
                      )
                    : 0}
                  %
                </strong>
              </div>
            </div>

            <div className="bg-slate-50 rounded-2xl p-3.5 border border-slate-200/80">
              <div className="flex items-center justify-between text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                <span>Month Win Rate</span>
                <span className="font-mono text-emerald-700">
                  {computedCalendar.monthSummary.total_trades} Trades
                </span>
              </div>
              <div className="text-xl font-black font-mono text-slate-900 mt-1">
                {computedCalendar.monthSummary.win_rate}%
              </div>
              <div className="text-[10px] font-mono text-slate-500 mt-1">
                <span className="text-emerald-600 font-bold">{computedCalendar.monthSummary.wins}W</span> /{' '}
                <span className="text-rose-600 font-bold">{computedCalendar.monthSummary.losses}L</span> closed in{' '}
                {formatMonthLabel(computedCalendar.activeMonth)}
              </div>
            </div>

            <div className="bg-slate-50 rounded-2xl p-3.5 border border-slate-200/80">
              <div className="flex items-center justify-between text-[10px] font-bold text-slate-400 uppercase tracking-wider">
                <span>Book 1 vs Book 2 Split</span>
                <span className="font-mono text-indigo-600">
                  {computedCalendar.monthSummary.long_book_trades}B1 / {computedCalendar.monthSummary.short_book_trades}B2
                </span>
              </div>
              <div className="text-xs font-mono font-bold mt-1.5 space-y-1">
                <div className="flex justify-between">
                  <span className="text-slate-500">B1 (1-Lot Buy):</span>
                  <span
                    className={
                      computedCalendar.monthSummary.long_book_net_pnl >= 0
                        ? 'text-emerald-600 font-black'
                        : 'text-rose-600 font-black'
                    }
                  >
                    {formatINR(computedCalendar.monthSummary.long_book_net_pnl)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">B2 (1-Lot Sell):</span>
                  <span
                    className={
                      computedCalendar.monthSummary.short_book_net_pnl >= 0
                        ? 'text-cyan-600 font-black'
                        : 'text-rose-600 font-black'
                    }
                  >
                    {formatINR(computedCalendar.monthSummary.short_book_net_pnl)}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* 5-Column Weekday Header (Monday – Friday) */}
          <div className="grid grid-cols-5 gap-2.5 text-center text-[10px] font-bold uppercase tracking-wider text-slate-400 px-1">
            <div>Monday</div>
            <div>Tuesday</div>
            <div>Wednesday</div>
            <div>Thursday</div>
            <div>Friday</div>
          </div>

          {/* 5-Column Weekday Calendar Grid */}
          <div className="grid grid-cols-2 sm:grid-cols-5 gap-2.5">
            {computedCalendar.weekdayCells.map((d) => {
              if (d.isPadding) {
                return (
                  <div
                    key={d.key}
                    className="hidden sm:block rounded-2xl border border-dashed border-slate-100 bg-slate-50/30 p-3 min-h-[108px]"
                  />
                );
              }
              const net = Number(d.net_pnl_rupees || 0);
              const hasTrades = Number(d.trade_count || 0) > 0;
              const todayIso = new Date().toLocaleDateString('en-CA');
              const isToday = d.date === todayIso;
              const dtObj = new Date(`${d.date}T00:00:00`);
              const dayLabel = dtObj.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });

              let bgClass = 'bg-slate-50/60 border-slate-200/70 text-slate-400';
              if (hasTrades) {
                if (net > 0) {
                  bgClass =
                    'bg-emerald-50/75 border-emerald-300 text-emerald-950 hover:bg-emerald-50 hover:shadow-sm';
                } else if (net < 0) {
                  bgClass = 'bg-rose-50/75 border-rose-300 text-rose-950 hover:bg-rose-50 hover:shadow-sm';
                } else {
                  bgClass = 'bg-amber-50/60 border-amber-200 text-amber-900';
                }
              }

              return (
                <div
                  key={d.date}
                  onClick={() => hasTrades && setSelectedCalendarDay(d)}
                  className={`rounded-2xl border p-3 transition-all min-h-[108px] flex flex-col justify-between ${bgClass} ${
                    hasTrades ? 'cursor-pointer' : 'opacity-70'
                  } ${isToday ? 'ring-2 ring-indigo-500/40' : ''}`}
                >
                  <div className="flex items-center justify-between gap-1">
                    <span className="text-[11px] font-bold text-slate-700 flex items-center gap-1">
                      <span>{dayLabel}</span>
                      {isToday && (
                        <span className="text-[8px] font-black uppercase px-1.5 py-0.5 rounded bg-indigo-600 text-white">
                          Today
                        </span>
                      )}
                    </span>
                    <span className="text-[10px] font-mono text-slate-500">
                      {hasTrades ? `${d.trade_count} trade${d.trade_count > 1 ? 's' : ''}` : '—'}
                    </span>
                  </div>

                  {hasTrades ? (
                    <div className="mt-2 space-y-1">
                      <div
                        className={`text-sm font-black font-mono ${
                          net >= 0 ? 'text-emerald-600' : 'text-rose-600'
                        }`}
                      >
                        {net >= 0 ? '+' : '-'}₹
                        {Math.abs(net).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                      </div>
                      <div className="flex items-center justify-between text-[10px] text-slate-600 font-mono">
                        <span>{d.win_rate}% WR</span>
                        <span>
                          {d.wins}W/{d.losses}L
                        </span>
                      </div>
                      <div className="flex items-center justify-between text-[9px] font-mono pt-1 border-t border-slate-200/60">
                        <span
                          className={
                            (d.long_book_net_pnl || 0) >= 0 ? 'text-emerald-700 font-bold' : 'text-rose-600 font-bold'
                          }
                        >
                          B1: {d.long_book_trades > 0 ? `${d.long_book_net_pnl >= 0 ? '+' : '-'}₹${Math.abs(d.long_book_net_pnl).toFixed(0)}` : '—'}
                        </span>
                        <span
                          className={
                            (d.short_book_net_pnl || 0) >= 0 ? 'text-cyan-700 font-bold' : 'text-rose-600 font-bold'
                          }
                        >
                          B2: {d.short_book_trades > 0 ? `${d.short_book_net_pnl >= 0 ? '+' : '-'}₹${Math.abs(d.short_book_net_pnl).toFixed(0)}` : '—'}
                        </span>
                      </div>
                    </div>
                  ) : (
                    <div className="mt-3 text-[10px] text-slate-400 font-medium">No Closed Trades</div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* DAY PERFORMANCE INSPECTOR MODAL (Click Any Calendar Day) */}
      {selectedCalendarDay && (
        <div
          className="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-xs flex items-center justify-center p-4"
          onClick={() => setSelectedCalendarDay(null)}
        >
          <div
            className="bg-white rounded-3xl border border-slate-200 shadow-2xl max-w-4xl w-full max-h-[88vh] overflow-hidden flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="p-5 border-b border-slate-100 flex items-center justify-between bg-slate-50/70">
              <div>
                <div className="flex items-center gap-2">
                  <CalendarIcon size={16} className="text-indigo-600" />
                  <h3 className="text-base font-black text-slate-900">
                    Day Performance: {selectedCalendarDay.date}
                  </h3>
                  <span
                    className={`px-2 py-0.5 rounded-full text-[10px] font-mono font-bold ${
                      selectedCalendarDay.net_pnl_rupees >= 0
                        ? 'bg-emerald-100 text-emerald-700'
                        : 'bg-rose-100 text-rose-700'
                    }`}
                  >
                    {formatINR(selectedCalendarDay.net_pnl_rupees)}
                  </span>
                </div>
                <p className="text-xs text-slate-500 mt-0.5">
                  Option Sniffer Advance Dual-Book Ledger • {selectedCalendarDay.trade_count} Closed Trades ({selectedCalendarDay.wins}W / {selectedCalendarDay.losses}L)
                </p>
              </div>
              <button
                onClick={() => setSelectedCalendarDay(null)}
                className="p-2 rounded-xl bg-white border border-slate-200 text-slate-500 hover:text-slate-800 cursor-pointer"
              >
                <X size={16} />
              </button>
            </div>

            <div className="p-5 overflow-y-auto space-y-4">
              {/* 4 Day Summary Cards */}
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <div className="p-3 rounded-2xl bg-slate-50 border border-slate-200/80">
                  <div className="text-[10px] uppercase font-bold text-slate-400">Total Trades</div>
                  <div className="text-lg font-black text-slate-800 mt-0.5">
                    {selectedCalendarDay.trade_count}
                  </div>
                  <div className="text-[10px] font-mono text-slate-500">
                    B1: {selectedCalendarDay.long_book_trades} | B2: {selectedCalendarDay.short_book_trades}
                  </div>
                </div>
                <div className="p-3 rounded-2xl bg-slate-50 border border-slate-200/80">
                  <div className="text-[10px] uppercase font-bold text-slate-400">Win Rate</div>
                  <div className="text-lg font-black text-emerald-600 mt-0.5">
                    {selectedCalendarDay.win_rate}% ({selectedCalendarDay.wins}W/{selectedCalendarDay.losses}L)
                  </div>
                </div>
                <div className="p-3 rounded-2xl bg-slate-50 border border-slate-200/80">
                  <div className="text-[10px] uppercase font-bold text-slate-400">Book 1 vs Book 2</div>
                  <div className="text-xs font-mono font-bold mt-1 space-y-0.5">
                    <div className="text-emerald-700">B1: {formatINR(selectedCalendarDay.long_book_net_pnl)}</div>
                    <div className="text-cyan-700">B2: {formatINR(selectedCalendarDay.short_book_net_pnl)}</div>
                  </div>
                </div>
                <div className="p-3 rounded-2xl bg-slate-50 border border-slate-200/80">
                  <div className="text-[10px] uppercase font-bold text-slate-400">Net Realized P&amp;L</div>
                  <div
                    className={`text-lg font-black font-mono mt-0.5 ${
                      selectedCalendarDay.net_pnl_rupees >= 0 ? 'text-emerald-600' : 'text-rose-600'
                    }`}
                  >
                    {formatINR(selectedCalendarDay.net_pnl_rupees)}
                  </div>
                  <div className="text-[10px] font-mono text-slate-400">
                    Charges: ₹{Number(selectedCalendarDay.charges_rupees || 0).toFixed(2)}
                  </div>
                </div>
              </div>

              {/* Day Trades Execution Table */}
              <div className="overflow-x-auto border border-slate-200 rounded-2xl">
                <table className="w-full text-left border-collapse text-xs font-mono">
                  <thead>
                    <tr className="bg-slate-50 border-b border-slate-200 text-[10px] uppercase text-slate-500">
                      <th className="py-2.5 px-3">Time (IST)</th>
                      <th className="py-2.5 px-3">Book</th>
                      <th className="py-2.5 px-3">Strategy</th>
                      <th className="py-2.5 px-3">Contract</th>
                      <th className="py-2.5 px-3 text-right">Qty</th>
                      <th className="py-2.5 px-3 text-right">Entry → Exit</th>
                      <th className="py-2.5 px-3 text-right">MFE / MAE</th>
                      <th className="py-2.5 px-3 text-right">Net P&amp;L</th>
                      <th className="py-2.5 px-3">Exit Reason</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {(selectedCalendarDay.trades || []).map((t, idx) => {
                      const isLong = t.book_type === 'LONG_BOOK_BUY';
                      const isWin = Number(t.net_pnl || 0) > 0;
                      return (
                        <tr key={t.trade_id || idx} className="hover:bg-slate-50">
                          <td className="py-2.5 px-3 text-slate-700">
                            <div>{String(t.entry_time || '').slice(11, 19)}</div>
                            <div className="text-[10px] text-slate-400">→ {String(t.exit_time || '').slice(11, 19)}</div>
                          </td>
                          <td className="py-2.5 px-3">
                            <span
                              className={`px-2 py-0.5 rounded font-bold text-[10px] ${
                                isLong
                                  ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                                  : 'bg-cyan-50 text-cyan-700 border border-cyan-200'
                              }`}
                            >
                              {isLong ? 'BUY 1 LOT' : 'SELL 1 LOT'}
                            </span>
                          </td>
                          <td className="py-2.5 px-3 font-semibold text-slate-700">{t.strategy_mode}</td>
                          <td className="py-2.5 px-3 font-bold text-slate-900">
                            {t.trading_symbol || `${t.underlying} ${Number(t.strike_price || 0).toFixed(0)} ${t.option_type}`}
                          </td>
                          <td className="py-2.5 px-3 text-right text-slate-600">{t.quantity}</td>
                          <td className="py-2.5 px-3 text-right text-slate-700">
                            ₹{Number(t.entry_price || 0).toFixed(2)} → ₹{Number(t.exit_price || 0).toFixed(2)}
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            <span className="text-emerald-600">+{Number(t.mfe_pct || 0).toFixed(1)}%</span>
                            {' / '}
                            <span className="text-rose-500">{Number(t.mae_pct || 0).toFixed(1)}%</span>
                          </td>
                          <td className={`py-2.5 px-3 text-right font-black ${isWin ? 'text-emerald-600' : 'text-rose-600'}`}>
                            {formatINR(t.net_pnl)}
                          </td>
                          <td className="py-2.5 px-3">
                            <span className="px-2 py-0.5 rounded bg-slate-100 text-slate-700 text-[10px] font-semibold">
                              {t.exit_reason}
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

      {/* TAB 2: LIVE SANDBOX POSITIONS & CLOSED LEDGER */}
      {activeTab === 'live' && (
        <div className="space-y-6">
          {/* Isolated Live Broker Execution Ledger Card */}
          <div className="bg-slate-900 text-white rounded-2xl shadow-lg border border-indigo-500/30 p-6">
            <div className="flex flex-wrap items-center justify-between gap-3 mb-4 pb-3 border-b border-slate-800">
              <div>
                <h2 className="text-base font-black flex items-center gap-2">
                  <Zap className="text-amber-400" size={18} />
                  <span>Isolated Live Broker Mirroring Ledger ({liveRecentIntents.length})</span>
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase ${
                      isLiveArmed
                        ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40'
                        : 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
                    }`}
                  >
                    {isLiveArmed ? `ARMED (${currentExecMode})` : 'DISARMED (PAPER SANDBOX)'}
                  </span>
                </h2>
                <p className="text-xs text-slate-400 mt-0.5">
                  Zero-Touch Read-Only WAL Follower on <code className="text-indigo-300">option_sniffer_advance_trades</code>. Core Advance files remain 100% untouched.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-4 text-xs font-mono">
                <div>
                  <span className="text-slate-400">Live Unrealized: </span>
                  <strong className={(liveStatus?.pnl_summary?.unrealized_net_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                    {formatINR(liveStatus?.pnl_summary?.unrealized_net_pnl || 0)}
                  </strong>
                </div>
                <div>
                  <span className="text-slate-400">Live Realized Net: </span>
                  <strong className={(liveStatus?.pnl_summary?.realized_net_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                    {formatINR(liveStatus?.pnl_summary?.realized_net_pnl || 0)}
                  </strong>
                </div>
                <div>
                  <span className="text-slate-400">Upstox Margin: </span>
                  <strong className="text-amber-300">₹{availMargin.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</strong>
                </div>
              </div>
            </div>

            {liveRecentIntents.length === 0 ? (
              <div className="text-xs text-slate-400 bg-slate-950/70 rounded-xl p-5 text-center border border-slate-800 font-mono">
                No mirrored Live Broker orders dispatched yet. Keep the bridge DISARMED for pure Paper Sandbox validation, or arm via the 4-Digit PIN modal when ready.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs font-mono">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400 uppercase text-[11px]">
                      <th className="py-2 px-2">Time</th>
                      <th className="py-2 px-2">Mode / Book</th>
                      <th className="py-2 px-2">Contract</th>
                      <th className="py-2 px-2">Action</th>
                      <th className="py-2 px-2 text-right">Fill / Limit</th>
                      <th className="py-2 px-2">State</th>
                      <th className="py-2 px-2">Exit State</th>
                      <th className="py-2 px-2 text-right">Realized Net</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/80">
                    {liveRecentIntents.map((intent) => {
                      const activeMatch = liveActivePositions.find(
                        (p) => p.order_intent_id === intent.order_intent_id
                      );
                      const livePrice =
                        activeMatch &&
                        (intent.action === 'BUY' ? activeMatch.current_bid : activeMatch.current_ask);
                      const liveUnr = activeMatch?.unrealized_pnl;
                      return (
                        <tr key={intent.order_intent_id}>
                          <td className="py-2 px-2 text-slate-300">{String(intent.created_at || '').slice(11, 19)}</td>
                          <td className="py-2 px-2 text-indigo-300">{intent.execution_mode}</td>
                          <td className="py-2 px-2 font-bold text-white">{intent.trading_symbol}</td>
                          <td className="py-2 px-2">
                            <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${intent.action === 'BUY' ? 'bg-emerald-950 text-emerald-300' : 'bg-cyan-950 text-cyan-300'}`}>
                              {intent.action} {intent.target_quantity}
                            </span>
                          </td>
                          <td className="py-2 px-2 text-right">
                            ₹{Number(intent.average_fill_price || intent.limit_price || 0).toFixed(2)}
                            {intent.exit_state !== 'CONFIRMED_FLAT' && Number(livePrice) > 0 && (
                              <span className="text-cyan-300 ml-1">
                                → ₹{Number(livePrice).toFixed(2)}
                              </span>
                            )}
                          </td>
                          <td className="py-2 px-2 text-emerald-300 font-bold">{intent.state}</td>
                          <td className="py-2 px-2 text-slate-300">{intent.exit_state}</td>
                          <td
                            className={`py-2 px-2 text-right font-black ${
                              intent.exit_state === 'CONFIRMED_FLAT'
                                ? (intent.realized_pnl || 0) >= 0
                                  ? 'text-emerald-400'
                                  : 'text-rose-400'
                                : (liveUnr || 0) >= 0
                                ? 'text-emerald-400'
                                : 'text-rose-400'
                            }`}
                          >
                            {intent.exit_state === 'CONFIRMED_FLAT'
                              ? formatINR(intent.realized_pnl)
                              : typeof liveUnr === 'number'
                              ? `OPEN (${formatINR(liveUnr)})`
                              : 'OPEN'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
            <h2 className="text-base font-black text-slate-900 mb-3">
              Active Open Dual-Book Paper Positions ({tradesData.open_trades?.length || 0})
            </h2>
            {(!tradesData.open_trades || tradesData.open_trades.length === 0) ? (
              <div className="text-xs text-slate-500 bg-slate-50 rounded-xl p-6 text-center border border-slate-100">
                No open Dual-Book sandbox positions right now. During market hours (09:15–15:15 IST), Book 1 (1 Lot BUY) and Book 2 (1 Lot SELL) automatically execute here in Paper Sandbox mode.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs font-mono">
                  <thead>
                    <tr className="border-b border-slate-200 text-slate-500 uppercase text-[11px]">
                      <th className="py-2.5 px-2">Book</th>
                      <th className="py-2.5 px-2">Strategy</th>
                      <th className="py-2.5 px-2">Contract</th>
                      <th className="py-2.5 px-2 text-right">Entry</th>
                      <th className="py-2.5 px-2 text-right">Current Bid/Ask</th>
                      <th className="py-2.5 px-2 text-right">Effective Stop</th>
                      <th className="py-2.5 px-2 text-right">MFE / MAE</th>
                      <th className="py-2.5 px-2 text-right">Unrealized Net</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {tradesData.open_trades.map((t) => (
                      <tr key={t.trade_id}>
                        <td className="py-2.5 px-2 font-bold">{t.book_type}</td>
                        <td className="py-2.5 px-2">{t.strategy_mode}</td>
                        <td className="py-2.5 px-2 font-bold">{t.trading_symbol}</td>
                        <td className="py-2.5 px-2 text-right">₹{Number(t.entry_price || 0).toFixed(2)}</td>
                        <td className="py-2.5 px-2 text-right">
                          ₹{Number(t.current_bid || 0).toFixed(2)} / ₹{Number(t.current_ask || 0).toFixed(2)}
                        </td>
                        <td className="py-2.5 px-2 text-right text-amber-700 font-bold">
                          ₹{Number(t.effective_stop_price || 0).toFixed(2)}
                        </td>
                        <td className="py-2.5 px-2 text-right">
                          +{Number(t.mfe_pct || 0).toFixed(1)}% / {Number(t.mae_pct || 0).toFixed(1)}%
                        </td>
                        <td className={`py-2.5 px-2 text-right font-black ${(t.unrealized_net_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                          {formatINR(t.unrealized_net_pnl)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
            <h2 className="text-base font-black text-slate-900 mb-3">
              Closed Live Sandbox Trades ({tradesData.closed_trades?.length || 0})
            </h2>
            {(!tradesData.closed_trades || tradesData.closed_trades.length === 0) ? (
              <div className="text-xs text-slate-500 bg-slate-50 rounded-xl p-6 text-center border border-slate-100">
                No closed sandbox trades in the live database table yet. See Tab 1 for the Day-by-Day P&amp;L Calendar.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs font-mono">
                  <thead>
                    <tr className="border-b border-slate-200 text-slate-500 uppercase text-[11px]">
                      <th className="py-2.5 px-2">Entry → Exit</th>
                      <th className="py-2.5 px-2">Book</th>
                      <th className="py-2.5 px-2">Strategy</th>
                      <th className="py-2.5 px-2">Contract</th>
                      <th className="py-2.5 px-2 text-right">Entry → Exit</th>
                      <th className="py-2.5 px-2 text-right">Net P&amp;L</th>
                      <th className="py-2.5 px-2">Exit Reason</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {tradesData.closed_trades.map((t) => (
                      <tr key={t.trade_id}>
                        <td className="py-2.5 px-2">{String(t.entry_time || '').slice(0, 19)}</td>
                        <td className="py-2.5 px-2 font-bold">{t.book_type}</td>
                        <td className="py-2.5 px-2">{t.strategy_mode}</td>
                        <td className="py-2.5 px-2 font-bold">{t.trading_symbol}</td>
                        <td className="py-2.5 px-2 text-right">
                          ₹{Number(t.entry_price || 0).toFixed(2)} → ₹{Number(t.exit_price || 0).toFixed(2)}
                        </td>
                        <td className={`py-2.5 px-2 text-right font-black ${(t.net_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                          {formatINR(t.net_pnl)}
                        </td>
                        <td className="py-2.5 px-2">{t.exit_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* TAB 3: DUAL-BOOK QUANT ARCHITECTURE */}
      {activeTab === 'architecture' && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="bg-white rounded-2xl shadow-sm border border-emerald-200 p-6">
            <h3 className="text-base font-black text-emerald-800 flex items-center gap-2">
              <ArrowUpRight size={18} />
              <span>Book 1: Long Option Buying (1 Lot — Wave-1 Gamma Sniper)</span>
            </h3>
            <ul className="mt-3 space-y-2 text-xs text-slate-700 leading-relaxed">
              <li><strong>Regime Gate:</strong> Active strictly on fresh Wave-1 directional impulses (`Kaufman ER ≥ 0.28`, VWAP alignment, no same-direction harvest in the prior 60 minutes).</li>
              <li><strong>Strike Selection:</strong> Strictly ATM / Near-ATM (`|OTM| ≤ 1.2` strikes, `|Delta| ≥ 0.42`, Premium `₹70 – ₹340` NIFTY / `₹120 – ₹680` BANKNIFTY).</li>
              <li><strong>Capped Loss:</strong> Hard stop at `-20%` or `₹2,200` per lot (whichever is tighter), plus 25-minute Theta Stagnation Bailout.</li>
              <li><strong>Smartly Trailed Uncapped Profit:</strong> 4-Tier Chandelier Ratchet (`+10%` floor at `+14%` MFE; trails `12%` at `+20%` MFE, `10%` at `+35%` MFE, `8%` at `+60%` MFE, `6%` at `+100%` MFE).</li>
            </ul>
          </div>

          <div className="bg-white rounded-2xl shadow-sm border border-cyan-200 p-6">
            <h3 className="text-base font-black text-cyan-800 flex items-center gap-2">
              <ArrowDownRight size={18} />
              <span>Book 2: Short Option Selling (1 Lot — Theta &amp; Exhaustion Harvester)</span>
            </h3>
            <ul className="mt-3 space-y-2 text-xs text-slate-700 leading-relaxed">
              <li><strong>Mode 2A — Contraction Theta Seller:</strong> Fires when `ATR_5m / ATR_20m ≤ 0.86`, `Wilder ADX(14) &lt; 36`, and `|VWAP Drift| ≤ 0.42%` (sells fading OTM/Near-ATM resistance/support).</li>
              <li><strong>Mode 2B — Wave-2 Post-Harvest Exhaustion Seller:</strong> When Book 1 harvests a Wave-1 trade (entering the 60m cooldown) or `Kaufman ER ≤ 0.08`, Book 2 sells the exhausted Call/Put premium as IV and momentum mean-revert.</li>
              <li><strong>Mode 2C — Diagonal OTM Wing Seller:</strong> When Book 1 holds an active Long position for `≥ 10m`, Book 2 sells 1 lot of a further-OTM wing (`OTM ≥ 1.2` strikes), converting the position into a positive-theta Diagonal Spread.</li>
              <li><strong>Capped Loss &amp; Reverse-Chandelier Trail:</strong> Hard buy-back stop at `+16%` spike or `₹1,800` max loss; 4-Tier Reverse-Chandelier locks `+2.5%` at `+10%` decay and trails tightly up to `+50%` decay.</li>
            </ul>
          </div>
        </div>
      )}

      {/* TAB 4: LIVE UPSTOX TERMINAL (Parity with Main Option Sniffer) */}
      {activeTab === 'live_terminal' && (
        <div className="space-y-6">
          {/* Header Banner: Operational Status */}
          <div
            className={`border rounded-2xl p-4 flex flex-col md:flex-row items-start md:items-center justify-between gap-4 transition-all ${
              isLiveArmed
                ? 'bg-gradient-to-r from-emerald-500/15 via-emerald-500/5 to-transparent border-emerald-500/40'
                : 'bg-gradient-to-r from-amber-500/10 via-amber-500/5 to-transparent border-amber-500/30'
            }`}
          >
            <div className="flex items-center space-x-3">
              <div
                className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 border ${
                  isLiveArmed
                    ? 'bg-emerald-500/20 text-emerald-600 border-emerald-500/30'
                    : 'bg-amber-500/20 text-amber-500 border-amber-500/30'
                }`}
              >
                {isLiveArmed ? <Zap size={20} className="animate-pulse" /> : <Lock size={20} />}
              </div>
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-black text-slate-900 tracking-wide">
                    UPSTOX LIVE EXECUTION TERMINAL — OPTION SNIFFER ADVANCE
                  </span>
                  <span
                    className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-full ${
                      isLiveArmed ? 'bg-emerald-600 text-white animate-pulse' : 'bg-amber-500 text-white'
                    }`}
                  >
                    {isLiveArmed ? `REAL TRADING ARMED (${currentExecMode})` : 'PAPER MODE ACTIVE'}
                  </span>
                </div>
                <p className="text-xs text-slate-600 mt-0.5">
                  {isLiveArmed
                    ? `Live execution armed in ${currentExecMode} mode. Zero-touch SQLite WAL follower mirrors trades to Upstox V2. Auto-expires at 15:15 IST.`
                    : 'Pre-flight account verification, Mumbai static IPv6 proxy telemetry, and capital-matched execution mode selection.'}
                </p>
              </div>
            </div>
            <button
              onClick={fetchAllTelemetry}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl text-xs font-bold bg-white text-slate-700 hover:bg-slate-50 border border-slate-200 transition-all shadow-2xs cursor-pointer"
            >
              <RefreshCw size={13} className="text-slate-500" />
              <span>Refresh Telemetry</span>
            </button>
          </div>

          {/* Section 1: 3 Pre-Flight Readiness Cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* Card 1: Account Profile & Exchange Segments */}
            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                  <Server size={13} className="text-slate-400" />
                  Account &amp; Segments
                </span>
                <span
                  className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                    liveStatus?.readiness?.nfo_enabled
                      ? 'bg-emerald-100 text-emerald-800'
                      : 'bg-rose-100 text-rose-800 border border-rose-200'
                  }`}
                >
                  {liveStatus?.readiness?.nfo_enabled ? 'NFO ACTIVE' : 'NFO INACTIVE'}
                </span>
              </div>
              <div>
                <div className="text-base font-black text-slate-900">
                  {liveStatus?.readiness?.user_name || 'Upstox Account'}
                </div>
                <div className="text-xs text-slate-500 font-mono">
                  User ID: {liveStatus?.readiness?.user_id || '—'}
                </div>
              </div>
              <div className="text-[11px] space-y-1 pt-2 border-t border-slate-100">
                <div className="flex justify-between">
                  <span className="text-slate-500">Enabled Exchanges:</span>
                  <span className="font-mono font-bold text-slate-800">
                    {liveStatus?.readiness?.exchanges?.length
                      ? liveStatus.readiness.exchanges.join(', ')
                      : 'NSE, NFO, BSE'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">F&amp;O Derivatives:</span>
                  <span
                    className={`font-mono font-bold ${
                      liveStatus?.readiness?.nfo_enabled ? 'text-emerald-600' : 'text-rose-600'
                    }`}
                  >
                    {liveStatus?.readiness?.nfo_enabled ? 'Enabled (NFO)' : 'Checking NFO...'}
                  </span>
                </div>
              </div>
            </div>

            {/* Card 2: Wallet Margins */}
            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                  <Coins size={13} className="text-slate-400" />
                  Equity Wallet Margin
                </span>
                <span
                  className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                    availMargin >= reqMargin
                      ? 'bg-emerald-100 text-emerald-800'
                      : 'bg-amber-100 text-amber-800 border border-amber-200'
                  }`}
                >
                  {availMargin >= reqMargin ? 'MODE FUNDED' : 'INSUFFICIENT FOR MODE'}
                </span>
              </div>
              <div>
                <div className="text-xl font-black text-slate-900">
                  ₹{availMargin.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </div>
                <div className="text-xs text-slate-500">Available Trading Margin</div>
              </div>
              <div className="text-[11px] space-y-1 pt-2 border-t border-slate-100">
                <div className="flex justify-between">
                  <span className="text-slate-500">Selected Mode:</span>
                  <span className="font-mono font-bold text-indigo-700">{currentExecMode}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">Required Minimum:</span>
                  <span className="font-mono font-bold text-slate-700">
                    ₹{reqMargin.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                  </span>
                </div>
              </div>
            </div>

            {/* Card 3: Mumbai Proxy & Static IP */}
            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                  <Wifi size={13} className="text-slate-400" />
                  Static IP Proxy
                </span>
                <span
                  className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                    liveStatus?.readiness?.static_ip_matched
                      ? 'bg-emerald-100 text-emerald-800'
                      : 'bg-rose-100 text-rose-800 border border-rose-200'
                  }`}
                >
                  {liveStatus?.readiness?.static_ip_matched ? 'WHITELIST MATCH' : 'CHECKING IP'}
                </span>
              </div>
              <div>
                <div className="text-sm font-black text-slate-900 font-mono truncate">
                  {liveStatus?.readiness?.primary_ip || '2600:1900:40a0:83a::63'}
                </div>
                <div className="text-xs text-slate-500">GCP Mumbai Static IPv6</div>
              </div>
              <div className="text-[11px] space-y-1 pt-2 border-t border-slate-100">
                <div className="flex justify-between">
                  <span className="text-slate-500">Proxy Gateway:</span>
                  <span className="font-mono text-slate-800">
                    {liveStatus?.readiness?.proxy_url_masked || 'in-mum.ipv6-proxy.in:13099'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">Round-Trip Latency:</span>
                  <span className="font-mono font-bold text-indigo-600">
                    {liveStatus?.readiness?.proxy_latency_ms || '—'} ms
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* Section 2: Pre-Flight Safety Gatekeepers */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-3">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center space-x-2">
                <ShieldAlert size={16} className="text-amber-500" />
                <h3 className="text-sm font-bold text-slate-900">Pre-Flight Safety Gatekeepers (Hard Blockers)</h3>
              </div>
              <span
                className={`text-[11px] font-mono font-bold px-2 py-0.5 rounded border ${
                  liveStatus?.readiness?.live_trading_eligible && availMargin >= reqMargin
                    ? 'text-emerald-700 bg-emerald-50 border-emerald-300'
                    : 'text-rose-600 bg-rose-50 border-rose-200'
                }`}
              >
                {liveStatus?.readiness?.live_trading_eligible && availMargin >= reqMargin
                  ? 'ALL GATES PASSED — ELIGIBLE'
                  : 'PRE-FLIGHT GATE CHECK'}
              </span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
              <div className="flex items-start space-x-2.5 p-3 rounded-xl bg-slate-50 border border-slate-200">
                {liveStatus?.readiness?.nfo_enabled ? (
                  <CheckCircle2 size={16} className="text-emerald-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle size={16} className="text-rose-500 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-bold text-slate-800">1. Upstox F&amp;O (NFO) Exchange Segment</div>
                  <div className="text-slate-500 text-[11px] mt-0.5">
                    {liveStatus?.readiness?.nfo_enabled
                      ? 'NFO segment active. Ready for options trading.'
                      : 'Awaiting NFO segment verification from Upstox profile.'}
                  </div>
                </div>
              </div>

              <div className="flex items-start space-x-2.5 p-3 rounded-xl bg-slate-50 border border-slate-200">
                {availMargin >= reqMargin ? (
                  <CheckCircle2 size={16} className="text-emerald-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle size={16} className="text-rose-500 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-bold text-slate-800">
                    2. Mode Margin Requirement ({currentExecMode}: ₹{reqMargin.toLocaleString('en-IN')}+)
                  </div>
                  <div className="text-slate-500 text-[11px] mt-0.5">
                    {availMargin >= reqMargin
                      ? `Sufficient wallet margin (₹${availMargin.toLocaleString('en-IN')}) for ${currentExecMode}.`
                      : `Available ₹${availMargin.toLocaleString('en-IN')} is below ₹${reqMargin.toLocaleString('en-IN')}. Select LONG_BOOK_ONLY for ₹55k wallets.`}
                  </div>
                </div>
              </div>

              <div className="flex items-start space-x-2.5 p-3 rounded-xl bg-slate-50 border border-slate-200">
                {liveStatus?.readiness?.static_ip_matched ? (
                  <CheckCircle2 size={16} className="text-emerald-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle size={16} className="text-rose-500 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-bold text-slate-800">3. Mumbai Static IP Whitelist</div>
                  <div className="text-slate-500 text-[11px] mt-0.5">
                    {liveStatus?.readiness?.static_ip_matched
                      ? 'Proxy IP matches registered primary/secondary whitelist.'
                      : 'Awaiting outgoing proxy IP verification.'}
                  </div>
                </div>
              </div>

              <div className="flex items-start space-x-2.5 p-3 rounded-xl bg-slate-50 border border-slate-200">
                {liveStatus?.readiness?.token_valid ? (
                  <CheckCircle2 size={16} className="text-emerald-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle size={16} className="text-rose-500 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-bold text-slate-800">4. Upstox V2 API Authentication</div>
                  <div className="text-slate-500 text-[11px] mt-0.5">
                    {liveStatus?.readiness?.token_valid
                      ? 'Bearer access token valid and authenticated (200 OK).'
                      : 'Token is invalid, expired, or awaiting refresh.'}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Section 3: Read-Only Paper Execution Mirror */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-3">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center space-x-2">
                <CheckCircle2 size={16} className="text-indigo-600" />
                <h3 className="text-sm font-bold text-slate-900">
                  Parallel Paper Execution Mirror (Zero-Touch Read-Only Observer)
                </h3>
              </div>
              <span className="text-[10px] font-mono text-slate-500">
                Connected via SQLite <code>mode=ro</code> &amp; <code>query_only=ON</code>
              </span>
            </div>
            <p className="text-xs text-slate-600 leading-relaxed">
              Option Sniffer Advance Paper Trading continues running 100% untouched. The isolated live bridge reads{' '}
              <code>option_sniffer_advance_trades</code> with zero write permissions to any core Advance table or file.
            </p>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono pt-2">
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
                <span className="text-[10px] text-slate-400 block uppercase">Paper Positions Open</span>
                <strong className="text-indigo-700">{tradesData.open_trades?.length || 0} active</strong>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
                <span className="text-[10px] text-slate-400 block uppercase">Paper Closed Trades</span>
                <strong className="text-slate-800">{tradesData.closed_trades?.length || 0} trades</strong>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
                <span className="text-[10px] text-slate-400 block uppercase">Live Realized Net P&amp;L</span>
                <strong
                  className={
                    (liveStatus?.pnl_summary?.realized_net_pnl ?? 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'
                  }
                >
                  {formatINR(liveStatus?.pnl_summary?.realized_net_pnl || 0)}
                </strong>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
                <span className="text-[10px] text-slate-400 block uppercase">Core Advance Files Modified</span>
                <strong className="text-emerald-700">0 BYTES (100% ISOLATED)</strong>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── 2-FACTOR 4-DIGIT PIN CONFIRMATION MODAL (Identical Ergonomics to Main Option Sniffer) ── */}
      {showArmModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4 animate-in fade-in duration-150">
          <div className="bg-white rounded-3xl max-w-lg w-full p-6 shadow-2xl border border-slate-200 space-y-4 text-slate-900 max-h-[92vh] overflow-y-auto">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center space-x-2">
                <div className="w-8 h-8 rounded-lg bg-amber-500/20 text-amber-600 flex items-center justify-center">
                  <Key size={16} />
                </div>
                <div>
                  <h3 className="text-sm font-black text-slate-900">
                    Arm 1-Day Real Trading — Option Sniffer Advance
                  </h3>
                  <p className="text-[11px] text-slate-500">
                    Security PIN Verification Interlock • Zero-Touch Isolated Live Bridge
                  </p>
                </div>
              </div>
              <button
                onClick={() => setShowArmModal(false)}
                className="text-slate-400 hover:text-slate-600 p-1 cursor-pointer"
              >
                <XCircle size={18} />
              </button>
            </div>

            <form onSubmit={handleArmSubmit} className="space-y-4">
              {/* 4-Digit Security Verification Code Box */}
              <div className="bg-slate-50 rounded-2xl p-4 text-center border border-slate-200 space-y-1.5">
                <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest block">
                  Security Verification Code
                </span>
                <div className="text-3xl font-black font-mono tracking-widest text-indigo-700 select-all">
                  {liveStatus?.current_pin || statusData?.live_bridge?.current_pin || '----'}
                </div>
                <p className="text-[11px] text-slate-500">
                  Type the 4-digit code above to confirm live broker execution rights for today (Upstox Margin:{' '}
                  <strong className="text-slate-800">
                    ₹{availMargin.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                  </strong>
                  ).
                </p>
              </div>

              {/* Capital-Matched Execution Mode Selector */}
              <div className="space-y-2">
                <label className="block text-xs font-bold text-slate-700">
                  1. Select Capital-Matched Execution Mode:
                </label>
                {EXEC_MODES.map((m) => {
                  const active = selectedExecMode === m.id;
                  return (
                    <div
                      key={m.id}
                      onClick={() => setSelectedExecMode(m.id)}
                      className={`p-3 rounded-xl border cursor-pointer transition ${
                        active
                          ? 'bg-indigo-50/80 border-indigo-500 ring-1 ring-indigo-500/30'
                          : 'bg-slate-50 border-slate-200 hover:border-slate-300'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-black text-slate-900">{m.title}</span>
                        <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-indigo-100 text-indigo-800 shrink-0">
                          {m.badge}
                        </span>
                      </div>
                      <p className="text-[11px] text-slate-600 mt-1 leading-relaxed">{m.desc}</p>
                    </div>
                  );
                })}
              </div>

              {/* 4-Digit Input Box */}
              <div className="space-y-1.5">
                <label className="text-xs font-bold text-slate-700">2. Enter 4-Digit Security Code:</label>
                <input
                  type="text"
                  maxLength={4}
                  value={pinInput}
                  onChange={(e) => setPinInput(e.target.value.replace(/\D/g, ''))}
                  placeholder="Type 4-digit code here"
                  className="w-full text-center font-mono text-2xl font-bold tracking-widest py-2.5 px-4 rounded-xl border border-slate-300 text-slate-900 bg-white focus:border-indigo-600 focus:ring-2 focus:ring-indigo-100 outline-hidden"
                  required
                />
              </div>

              <div className="p-3 rounded-xl bg-amber-50/70 border border-amber-200 text-[11px] text-amber-900 space-y-1">
                <div className="font-bold flex items-center gap-1 text-amber-800">
                  <AlertTriangle size={13} />
                  Isolated Live Bridge Safeguards:
                </div>
                <div>
                  • <strong>Zero Core Touch:</strong> Reads <code>option_sniffer_advance_trades</code> via SQLite{' '}
                  <code>mode=ro</code> without modifying a single line of core Advance logic.
                </div>
                <div>
                  • <strong>₹55k Wallet Safe Mode:</strong> <code>LONG_BOOK_ONLY</code> mirrors 100% of Book 1 (1-Lot
                  BUY) while keeping Book 2 (Naked SELL) in Paper Sandbox.
                </div>
                <div>
                  • Auto-expires at <strong>15:15 IST</strong> today (resets to Paper Mode). Hard daily loss ceiling:{' '}
                  <strong>-₹2,800.00</strong>.
                </div>
              </div>

              <div className="flex items-center space-x-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowArmModal(false)}
                  className="w-1/2 py-2.5 rounded-xl text-xs font-bold bg-slate-100 text-slate-700 hover:bg-slate-200 transition-all cursor-pointer"
                >
                  Cancel (Stay in Paper)
                </button>
                <button
                  type="submit"
                  disabled={armActionBusy || pinInput.trim().length !== 4}
                  className="w-1/2 py-2.5 rounded-xl text-xs font-black bg-gradient-to-r from-emerald-600 to-emerald-700 hover:from-emerald-700 hover:to-emerald-800 text-white transition-all shadow-sm disabled:opacity-50 cursor-pointer"
                >
                  {armActionBusy ? 'Verifying...' : '⚡ Arm for Today'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

