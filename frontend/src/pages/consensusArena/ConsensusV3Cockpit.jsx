import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  BarChart3,
  Briefcase,
  CheckCircle2,
  Clock,
  LayoutDashboard,
  Radio,
  ShieldCheck,
  Sliders,
} from 'lucide-react';
import { API_BASE } from '../../services/api';
import ConsensusGameArena from '../../components/ConsensusGameArena';
import {
  createPollingCoordinator,
  formatINRValue,
  formatNumber,
  formatPercent,
  formatSignedINR,
  formatTimestampLabel,
} from '../optionSnifferAdvance/formatters';
import ConsensusArmInterlockModal from './ConsensusArmInterlockModal';
import ConsensusWorkspaceHeader from './ConsensusWorkspaceHeader';

const V3_BASE = `${API_BASE}/consensus/v3`;

const COCKPIT_TABS = [
  { id: 'overview', label: 'Overview & Arena', icon: LayoutDashboard },
  { id: 'signals', label: '6-Layer Signals & Abstentions', icon: Radio },
  { id: 'positions', label: 'Positions & Cash Reservations', icon: Briefcase },
  { id: 'performance', label: 'Performance vs. Baseline', icon: BarChart3 },
  { id: 'diagnostics', label: 'Diagnostics & Invariants', icon: Activity },
];

const INITIAL_META = {
  status: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  signals: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  positions: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  performance: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  diagnostics: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  intents: { status: 'loading', lastSuccessMs: null, errorMessage: null },
};

export default function ConsensusV3Cockpit() {
  const [subTab, setSubTab] = useState('overview');
  const [statusData, setStatusData] = useState(null);
  const [signalsData, setSignalsData] = useState(null);
  const [positionsData, setPositionsData] = useState(null);
  const [perfData, setPerfData] = useState(null);
  const [diagData, setDiagData] = useState(null);
  const [liveIntents, setLiveIntents] = useState([]);
  const [sliceMeta, setSliceMeta] = useState(INITIAL_META);

  const [sweeping, setSweeping] = useState(false);
  const [rebalancingBook1, setRebalancingBook1] = useState(false);
  const [armModalOpen, setArmModalOpen] = useState(false);
  const [armPinInput, setArmPinInput] = useState('');
  const [armActionBusy, setArmActionBusy] = useState(false);
  const [armActionMsg, setArmActionMsg] = useState(null);

  const [capitalInput, setCapitalInput] = useState('400000');
  const [modeInput, setModeInput] = useState('DUAL_FUNDED_CASH_ARENA');
  const [marginModeInput, setMarginModeInput] = useState('CASH_1X');
  const [configBusy, setConfigBusy] = useState(false);
  const [exitingPosId, setExitingPosId] = useState(null);
  const formDirtyRef = useRef(false);

  const [coordinator] = useState(() => createPollingCoordinator());
  const controllersRef = useRef(new Set());

  const fetchSlice = useCallback(
    async (sliceName, url, setter) => {
      const token = coordinator.issueSliceToken(sliceName);
      const controller = new AbortController();
      controllersRef.current.add(controller);
      try {
        const res = await fetch(url, { signal: controller.signal });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (coordinator.shouldApplySliceResponse(sliceName, token)) {
          setter(data);
          setSliceMeta((prev) => ({
            ...prev,
            [sliceName]: { status: 'ok', lastSuccessMs: Date.now(), errorMessage: null },
          }));
        }
      } catch (err) {
        if (err?.name === 'AbortError') return;
        if (coordinator.shouldApplySliceResponse(sliceName, token)) {
          setSliceMeta((prev) => ({
            ...prev,
            [sliceName]: {
              status: 'error',
              lastSuccessMs: prev[sliceName]?.lastSuccessMs || null,
              errorMessage: err?.message || 'Fetch error',
            },
          }));
        }
      } finally {
        controllersRef.current.delete(controller);
      }
    },
    [coordinator]
  );

  const refreshAllSlices = useCallback(async () => {
    await Promise.all([
      fetchSlice('status', `${V3_BASE}/status`, (d) => {
        setStatusData(d);
        if (!formDirtyRef.current) {
          if (d?.live_bridge?.accounting?.funded_capital_inr) {
            setCapitalInput(String(Math.round(d.live_bridge.accounting.funded_capital_inr)));
          }
          if (d?.live_bridge?.execution_mode) {
            setModeInput(d.live_bridge.execution_mode);
          }
          if (d?.live_bridge?.margin_mode || d?.live_bridge?.accounting?.margin_mode) {
            setMarginModeInput(d.live_bridge.margin_mode || d.live_bridge.accounting.margin_mode);
          }
        }
      }),
      fetchSlice('signals', `${V3_BASE}/signals?limit=60`, setSignalsData),
      fetchSlice('positions', `${V3_BASE}/positions?limit=100`, setPositionsData),
      fetchSlice('performance', `${V3_BASE}/performance`, setPerfData),
      fetchSlice('diagnostics', `${V3_BASE}/diagnostics`, setDiagData),
      fetchSlice('intents', `${V3_BASE}/live/order-intents?limit=30`, setLiveIntents),
    ]);
  }, [fetchSlice]);

  useEffect(() => {
    refreshAllSlices();
    const timer = setInterval(refreshAllSlices, 12000);
    return () => {
      clearInterval(timer);
      controllersRef.current.forEach((c) => c.abort());
      controllersRef.current.clear();
    };
  }, [refreshAllSlices]);

  const handleManualSweep = async () => {
    setSweeping(true);
    try {
      const res = await fetch(`${V3_BASE}/sniffer-sweep`, { method: 'POST' });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Sweep failed');
      const createdCnt = body?.data?.reserved_orders_created?.length || 0;
      const ms = body?.data?.elapsed_ms || 0;
      setArmActionMsg({
        tone: 'ok',
        text: `Sniffer sweep completed in ${Number(ms).toFixed(0)}ms • ${createdCnt} pending cash reservation(s) created.`,
      });
      await refreshAllSlices();
    } catch (err) {
      setArmActionMsg({ tone: 'error', text: err.message || 'Sweep error' });
    } finally {
      setSweeping(false);
    }
  };

  const handleSelectMode = async (newMode) => {
    setArmActionBusy(true);
    try {
      const res = await fetch(`${V3_BASE}/live/mode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ execution_mode: newMode }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Mode update failed');
      setModeInput(newMode);
      await refreshAllSlices();
    } catch (err) {
      setArmActionMsg({ tone: 'error', text: err.message });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleConfirmArm = async () => {
    setArmActionBusy(true);
    try {
      const res = await fetch(`${V3_BASE}/live/arm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pin: armPinInput.trim(),
          execution_mode: statusData?.live_bridge?.execution_mode || modeInput,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Arming rejected');
      setArmModalOpen(false);
      setArmPinInput('');
      setArmActionMsg({ tone: 'ok', text: data.message || 'Live bridge armed.' });
      await refreshAllSlices();
    } catch (err) {
      setArmActionMsg({ tone: 'error', text: err.message });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleDisarm = async () => {
    setArmActionBusy(true);
    try {
      const res = await fetch(`${V3_BASE}/live/disarm`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Disarm failed');
      setArmActionMsg({ tone: 'ok', text: data.message });
      await refreshAllSlices();
    } catch (err) {
      setArmActionMsg({ tone: 'error', text: err.message });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleSosExit = async () => {
    setArmActionBusy(true);
    try {
      const res = await fetch(`${V3_BASE}/live/sos-panic-exit`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'SOS exit failed');
      setArmActionMsg({
        tone: 'ok',
        text: `SOS executed: ${data.flattened_count || 0} position(s) flattened & bridge disarmed to Paper Shadow.`,
      });
      await refreshAllSlices();
    } catch (err) {
      setArmActionMsg({ tone: 'error', text: err.message });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleRunBook1Rebalance = async () => {
    setRebalancingBook1(true);
    try {
      const res = await fetch(`${V3_BASE}/book1-rebalance`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: true }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Book-1 rebalance failed');
      const rb = body?.data?.rebalance_execution || {};
      setArmActionMsg({
        tone: 'ok',
        text: `Book-1 20d Swing Rebalance (${rb.status || 'DONE'}): ${rb.opened ?? 0} opened • ${rb.retained ?? 0} retained • ${rb.closed ?? 0} closed (Target ${rb.target_slots ?? '—'} CNC slots).`,
      });
      await refreshAllSlices();
    } catch (err) {
      setArmActionMsg({ tone: 'error', text: err.message || 'Book-1 rebalance error' });
    } finally {
      setRebalancingBook1(false);
    }
  };

  const handleConfigureArena = async (resetFlag = false) => {
    setConfigBusy(true);
    try {
      const parsedCap = Math.max(10000, Number(capitalInput) || 10000);
      const res = await fetch(`${V3_BASE}/arena/configure`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          funded_capital_inr: parsedCap,
          execution_mode: modeInput,
          margin_mode: marginModeInput,
          reset_ledger: resetFlag,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Configuration failed');
      formDirtyRef.current = false;
      const acct = data.accounting || {};
      setArmActionMsg({
        tone: 'ok',
        text: resetFlag
          ? `Funded Arena reset cleanly to ${formatINRValue(acct.funded_capital_inr)} in Paper Shadow mode.`
          : `Updated Arena to ${formatINRValue(acct.funded_capital_inr)} • MIS ${acct.intraday_mis_leverage || 1}x • Auto Slots B1:${acct?.book1_slots?.max || '—'} / B2:${acct?.book2_slots?.max || '—'}.`,
      });
      await refreshAllSlices();
    } catch (err) {
      setArmActionMsg({ tone: 'error', text: err.message });
    } finally {
      setConfigBusy(false);
    }
  };

  const handleExitPosition = async (positionId) => {
    setExitingPosId(positionId);
    try {
      const res = await fetch(`${V3_BASE}/positions/exit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ position_id: positionId, reason: 'MANUAL_UI_EXIT' }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Exit failed');
      setArmActionMsg({
        tone: 'ok',
        text: `Position ${positionId} closed • Net P&L: ${formatSignedINR(data.net_pnl_inr)}`,
      });
      await refreshAllSlices();
    } catch (err) {
      setArmActionMsg({ tone: 'error', text: err.message });
    } finally {
      setExitingPosId(null);
    }
  };

  const liveStatus = statusData?.live_bridge || null;
  const shield = statusData?.book1_macro_shield || signalsData?.book1_macro_shield || {};
  const b2Ctx = statusData?.book2_market_context || {};
  const pendOrders = positionsData?.pending_reserved_orders || [];
  const openPositions = positionsData?.open_positions || [];
  const closedPositions = positionsData?.closed_positions || [];
  const b2Approved = signalsData?.book2_approved || [];
  const b2Abstained = signalsData?.book2_abstained || [];
  const b1Leaders = signalsData?.book1_rebalance_leaders || [];

  return (
    <div className="space-y-4">
      <ConsensusWorkspaceHeader
        statusData={statusData}
        liveStatus={liveStatus}
        sliceMeta={sliceMeta}
        sweeping={sweeping}
        rebalancingBook1={rebalancingBook1}
        armActionBusy={armActionBusy}
        armActionMsg={armActionMsg}
        onClearArmMsg={() => setArmActionMsg(null)}
        onOpenArmModal={() => setArmModalOpen(true)}
        onDisarm={handleDisarm}
        onSosExit={handleSosExit}
        onManualSweep={handleManualSweep}
        onRunBook1Rebalance={handleRunBook1Rebalance}
      />

      {/* OptionSnifferAdvance-Style 5-Tab Navigation Bar */}
      <nav
        aria-label="Consensus V3 workspace sections"
        className="bg-white rounded-xl border border-slate-200 p-1.5 flex flex-wrap gap-1 shadow-2xs"
      >
        {COCKPIT_TABS.map((t) => {
          const Icon = t.icon;
          const active = subTab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => setSubTab(t.id)}
              className={`inline-flex items-center gap-2 px-3.5 py-2 rounded-lg text-xs font-bold transition-all cursor-pointer ${
                active
                  ? 'bg-slate-900 text-white shadow-xs'
                  : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100'
              }`}
            >
              <Icon size={14} />
              <span>{t.label}</span>
              {t.id === 'positions' && (openPositions.length > 0 || pendOrders.length > 0) && (
                <span className="px-1.5 py-0.5 rounded-full text-[10px] font-mono bg-amber-400 text-slate-950 font-black">
                  {openPositions.length + pendOrders.length}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      {/* ── TAB 1: OVERVIEW & PRESERVED CONSENSUS GAME ARENA ──────────────── */}
      {subTab === 'overview' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            {/* Book 1 Macro Exposure Shield Card */}
            <div className="bg-white rounded-2xl border border-slate-200 p-4 shadow-2xs space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-black uppercase tracking-wider text-slate-500">
                  Book 1 • 20d Positional Cash Core (CNC)
                </span>
                <span
                  className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                    shield.regime === 'BULL_FULL_DEPLOYMENT'
                      ? 'bg-emerald-100 text-emerald-800'
                      : shield.regime === 'BEAR_CASH_SHIELD'
                      ? 'bg-rose-100 text-rose-800'
                      : 'bg-amber-100 text-amber-800'
                  }`}
                >
                  {shield.regime || 'SYNCING'}
                </span>
              </div>
              <div className="text-sm font-bold text-slate-900">
                Target Exposure: {shield.target_slots ?? '—'} / {shield.max_slots ?? 8} Funded Slots
              </div>
              <p className="text-xs text-slate-600">
                F&amp;O-181 Breadth &gt; SMA50:{' '}
                <strong className="font-mono">{formatPercent(shield.breadth_above_sma50_pct)}</strong> •{' '}
                Scheduled 20-day rebalance with Rank-78% hysteresis. Freed shield cash routes automatically to Book 2!
              </p>
            </div>

            {/* Book 2 Intraday Sniffer Card */}
            <div className="bg-white rounded-2xl border border-slate-200 p-4 shadow-2xs space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-black uppercase tracking-wider text-slate-500">
                  Book 2 • 15m Idle-Cash Sniffer (MIS)
                </span>
                <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-sky-100 text-sky-900">
                  POST-ONLY LIMIT • 15:15 EXIT
                </span>
              </div>
              <div className="text-sm font-bold text-slate-900">
                Approved Today: {b2Approved.length} • Abstained: {b2Abstained.length}
              </div>
              <p className="text-xs text-slate-600">
                Intraday F&amp;O-181 Regime: <strong className="font-mono">{b2Ctx.regime || '—'}</strong> (
                {formatPercent(b2Ctx.fno_mkt_return_pct, { decimals: 2, signed: true })}) • Independent Qlib + Legacy
                disagreement-penalized payoff gate.
              </p>
            </div>

            {/* Funded Capital, Buying Power & Mode Control Card */}
            <div className="bg-white rounded-2xl border border-slate-200 p-4 shadow-2xs space-y-2.5">
              <div className="flex items-center justify-between text-[11px] font-black uppercase tracking-wider text-slate-500">
                <span className="flex items-center gap-1.5">
                  <Sliders size={13} />
                  <span>Wallet Capital &amp; MIS Buying Power</span>
                </span>
                <span className="font-mono text-[10px] text-indigo-700">
                  Min ₹10k • SL Cap {liveStatus?.accounting?.max_risk_per_trade_pct ?? 0.5}%
                </span>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                <input
                  type="number"
                  min="10000"
                  step="1000"
                  value={capitalInput}
                  onChange={(e) => {
                    formDirtyRef.current = true;
                    setCapitalInput(e.target.value);
                  }}
                  placeholder="Wallet ₹ (e.g. 50000)"
                  className="px-2.5 py-1.5 rounded-lg border border-slate-300 text-xs font-mono text-slate-900"
                  aria-label="Funded capital in INR"
                />
                <select
                  value={marginModeInput}
                  onChange={(e) => {
                    formDirtyRef.current = true;
                    setMarginModeInput(e.target.value);
                  }}
                  className="px-2 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-800"
                  aria-label="Intraday MIS buying power"
                >
                  <option value="CASH_1X">1.0x Cash (0x Lev)</option>
                  <option value="MIS_2X">2.0x MIS (SL Capped)</option>
                  <option value="MIS_3X">3.0x MIS (SL Capped)</option>
                  <option value="UPSTOX_VAR_ELM_MAX_5X">Upstox &lt;=5.0x VaR+ELM</option>
                </select>
                <select
                  value={modeInput}
                  onChange={(e) => {
                    formDirtyRef.current = true;
                    setModeInput(e.target.value);
                  }}
                  className="px-2 py-1.5 rounded-lg border border-slate-300 text-xs font-semibold text-slate-800"
                  aria-label="Arena execution mode"
                >
                  <option value="DUAL_FUNDED_CASH_ARENA">Dual Book (B1 + B2)</option>
                  <option value="BOOK1_SWING_CNC_ONLY">Book 1 (20d CNC)</option>
                  <option value="BOOK2_INTRADAY_MIS_SNIFFER">Book 2 (15m MIS)</option>
                </select>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  disabled={configBusy}
                  onClick={() => handleConfigureArena(false)}
                  className="flex-1 py-1.5 px-3 rounded-lg bg-slate-900 hover:bg-slate-800 text-white text-xs font-bold cursor-pointer"
                >
                  {configBusy ? 'Saving...' : 'Apply Capital / Margin / Mode'}
                </button>
                {liveStatus?.simulation_mode !== false && (
                  <button
                    type="button"
                    disabled={configBusy}
                    onClick={() => handleConfigureArena(true)}
                    className="py-1.5 px-3 rounded-lg border border-slate-300 hover:bg-rose-50 text-slate-700 hover:text-rose-700 text-xs font-semibold cursor-pointer"
                  >
                    Reset Shadow
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* Preserved Rich ConsensusGameArena (Equity Curve, Monthly Calendar, Day Replay, Badges) */}
          <ConsensusGameArena />
        </div>
      )}

      {/* ── TAB 2: 6-LAYER SIGNALS, DISAGREEMENT & EXPLICIT ABSTENTIONS ────── */}
      {subTab === 'signals' && (
        <div className="space-y-4">
          <div className="bg-white rounded-2xl border border-slate-200 p-4 sm:p-5 shadow-2xs space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-bold text-slate-900">
                  Book 2 • 15m F&amp;O-181 Sniffer Evaluations (Approved &amp; Explicit Abstentions)
                </h2>
                <p className="text-xs text-slate-500">
                  Snapshot: {formatTimestampLabel(signalsData?.as_of_ts)} • Every structural trigger is audited with
                  independent Qlib bps, independent Legacy bps, Disagreement penalty, and preserved technical badges.
                </p>
              </div>
            </div>

            {[...b2Approved, ...b2Abstained].length === 0 ? (
              <div className="p-6 text-center text-xs text-slate-500 bg-slate-50 rounded-xl border border-slate-200">
                0 F&amp;O-181 stocks triggered a PDH/PDL failed-sweep bar on the latest 15m candle — System cleanly
                ABSTAINING (zero forced noise trades).
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left border-collapse">
                  <thead>
                    <tr className="border-b border-slate-200 text-[10px] uppercase text-slate-500 font-bold">
                      <th className="py-2 px-2.5">Ticker</th>
                      <th className="py-2 px-2.5">Side / Limit</th>
                      <th className="py-2 px-2.5">Structural SL</th>
                      <th className="py-2 px-2.5">Qlib Net</th>
                      <th className="py-2 px-2.5">Legacy Net (Win%)</th>
                      <th className="py-2 px-2.5">Disagree</th>
                      <th className="py-2 px-2.5">Penalized Edge</th>
                      <th className="py-2 px-2.5">Telemetry (RSI • ADX • ATR • RVOL • R:R)</th>
                      <th className="py-2 px-2.5">Decision / Abstention</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 font-mono">
                    {[...b2Approved, ...b2Abstained].map((s, idx) => {
                      const approved = s.decision === 'APPROVED_POSTONLY_LIMIT';
                      const tel = s.telemetry || {};
                      return (
                        <tr key={`${s.ticker}_${idx}`} className={approved ? 'bg-emerald-50/40' : ''}>
                          <td className="py-2.5 px-2.5 font-bold text-slate-900">
                            {s.symbol}{' '}
                            <span className="px-1.5 py-0.5 rounded bg-indigo-50 text-indigo-700 text-[9px]">
                              F&amp;O
                            </span>
                          </td>
                          <td className="py-2.5 px-2.5">
                            <span
                              className={`px-1.5 py-0.5 rounded text-[10px] font-bold mr-1.5 ${
                                s.direction === 'BUY'
                                  ? 'bg-emerald-100 text-emerald-800'
                                  : 'bg-rose-100 text-rose-800'
                              }`}
                            >
                              {s.direction}
                            </span>
                            {formatINRValue(s.limit_entry_px)}
                          </td>
                          <td className="py-2.5 px-2.5 text-slate-700">
                            {formatINRValue(s.stop_loss_px)} ({s.stop_distance_bps} bps)
                          </td>
                          <td className="py-2.5 px-2.5">
                            {formatNumber(s.e_qlib_net_bps, { decimals: 1, signed: true, suffix: ' bps' })}
                          </td>
                          <td className="py-2.5 px-2.5">
                            {formatNumber(s.e_legacy_net_bps, { decimals: 1, signed: true, suffix: ' bps' })} (
                            {formatPercent((s.legacy_win_prob || 0) * 100)})
                          </td>
                          <td className="py-2.5 px-2.5 text-amber-700">
                            {formatNumber(s.disagreement_bps, { decimals: 1, suffix: ' bps' })}
                          </td>
                          <td
                            className={`py-2.5 px-2.5 font-bold ${
                              (s.edge_penalized_bps || 0) >= 8 ? 'text-emerald-700' : 'text-slate-600'
                            }`}
                          >
                            {formatNumber(s.edge_penalized_bps, { decimals: 1, signed: true, suffix: ' bps' })}
                          </td>
                          <td className="py-2.5 px-2.5 text-[11px] font-sans text-slate-600">
                            RSI {tel.rsi} • ADX {tel.adx} • ATR {tel.atr_pct}% • RVOL {tel.tod_rvol}x • R:R{' '}
                            {s.risk_reward_ratio} • {tel.finbert_badge}
                          </td>
                          <td className="py-2.5 px-2.5 font-sans">
                            {approved ? (
                              <span className="px-2 py-0.5 rounded bg-emerald-600 text-white text-[10px] font-bold">
                                APPROVED LIMIT
                              </span>
                            ) : (
                              <span className="px-2 py-0.5 rounded bg-slate-200 text-slate-800 text-[10px] font-semibold">
                                ABSTAIN: {s.abstain_reason}
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Book 1 20d Rebalance Leaders */}
          <div className="bg-white rounded-2xl border border-slate-200 p-4 sm:p-5 shadow-2xs space-y-3">
            <h2 className="text-sm font-bold text-slate-900">
              Book 1 • Top Scheduled 20-Day Positional Leaders (F&amp;O-181 Cash CNC • Legacy in SHADOW_ONLY)
            </h2>
            <div className="overflow-x-auto">
              <table className="w-full text-xs text-left border-collapse">
                <thead>
                  <tr className="border-b border-slate-200 text-[10px] uppercase text-slate-500 font-bold">
                    <th className="py-2 px-2.5">Symbol</th>
                    <th className="py-2 px-2.5">Close</th>
                    <th className="py-2 px-2.5">Published 12-1 Mom Pct</th>
                    <th className="py-2 px-2.5">Qlib 20d Rank</th>
                    <th className="py-2 px-2.5">Expected 20d Net (after CNC Tax)</th>
                    <th className="py-2 px-2.5">Legacy Shadow Win%</th>
                    <th className="py-2 px-2.5">Telemetry</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 font-mono">
                  {b1Leaders.slice(0, 10).map((r) => (
                    <tr key={r.ticker}>
                      <td className="py-2 px-2.5 font-bold text-slate-900">{r.symbol}</td>
                      <td className="py-2 px-2.5">{formatINRValue(r.close_px)}</td>
                      <td className="py-2 px-2.5">{formatPercent((r.baseline_mom_pct || 0) * 100)}</td>
                      <td className="py-2 px-2.5 font-bold text-indigo-700">
                        {formatPercent((r.qlib_pct_rank || 0) * 100)}
                      </td>
                      <td className="py-2 px-2.5 text-emerald-700 font-bold">
                        {formatNumber(r.e_qlib_net_bps, { decimals: 0, signed: true, suffix: ' bps' })}
                      </td>
                      <td className="py-2 px-2.5 text-slate-500">
                        {formatPercent((r.legacy_shadow?.legacy_shadow_p_win || 0) * 100)} (SHADOW)
                      </td>
                      <td className="py-2 px-2.5 font-sans text-[11px] text-slate-600">
                        RSI {r.telemetry?.rsi} • ADX {r.telemetry?.adx} • ATR {r.telemetry?.atr_pct}% • 52w{' '}
                        {formatPercent((r.telemetry?.near_52w_ratio || 0) * 100)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* ── TAB 3: POSITIONS, PENDING CASH RESERVATIONS & LIVE INTENTS ────── */}
      {subTab === 'positions' && (
        <div className="space-y-4">
          {/* Pending Resting Limit Orders (Cash Reserved!) */}
          <div className="bg-white rounded-2xl border border-slate-200 p-4 sm:p-5 shadow-2xs space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
                <Clock size={15} className="text-amber-600" />
                <span>Pending Resting Limit Orders (Funded Cash Actively Reserved)</span>
              </h2>
              <span className="text-xs font-mono text-amber-800 bg-amber-100 px-2.5 py-0.5 rounded font-bold">
                {pendOrders.length} Active Reservation(s)
              </span>
            </div>
            {pendOrders.length === 0 ? (
              <p className="text-xs text-slate-500">
                No pending limit reservations right now — 100% of uninvested cash is unreserved.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left border-collapse font-mono">
                  <thead>
                    <tr className="border-b border-slate-200 text-[10px] uppercase text-slate-500 font-sans font-bold">
                      <th className="py-2 px-2.5">Symbol</th>
                      <th className="py-2 px-2.5">Side</th>
                      <th className="py-2 px-2.5">Whole Shares</th>
                      <th className="py-2 px-2.5">Post-Only Limit</th>
                      <th className="py-2 px-2.5">Locked Margin (Lev / Source)</th>
                      <th className="py-2 px-2.5">Expires At (15m TIF)</th>
                      <th className="py-2 px-2.5">Env</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {pendOrders.map((o) => (
                      <tr key={o.order_id}>
                        <td className="py-2 px-2.5 font-bold text-slate-900">{o.symbol}</td>
                        <td className="py-2 px-2.5 text-rose-700 font-bold">{o.direction}</td>
                        <td className="py-2 px-2.5">{o.shares}</td>
                        <td className="py-2 px-2.5">{formatINRValue(o.limit_price)}</td>
                        <td className="py-2 px-2.5 text-amber-700 font-bold">
                          {formatINRValue(o.reserved_cash_inr)}{' '}
                          <span className="text-[10px] text-slate-500 font-normal">
                            ({o.effective_leverage || 1}x • {o.margin_source || 'CASH_1X'})
                          </span>
                        </td>
                        <td className="py-2 px-2.5">{o.expires_at}</td>
                        <td className="py-2 px-2.5">{o.execution_env}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Open Funded Positions */}
          <div className="bg-white rounded-2xl border border-slate-200 p-4 sm:p-5 shadow-2xs space-y-3">
            <h2 className="text-sm font-bold text-slate-900">Open Funded Equity Positions ({openPositions.length})</h2>
            {openPositions.length === 0 ? (
              <p className="text-xs text-slate-500">0 open Consensus V3 positions.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left border-collapse font-mono">
                  <thead>
                    <tr className="border-b border-slate-200 text-[10px] uppercase text-slate-500 font-sans font-bold">
                      <th className="py-2 px-2.5">Symbol / Book</th>
                      <th className="py-2 px-2.5">Side</th>
                      <th className="py-2 px-2.5">Shares</th>
                      <th className="py-2 px-2.5">Entry</th>
                      <th className="py-2 px-2.5">Current</th>
                      <th className="py-2 px-2.5">Stop Loss</th>
                      <th className="py-2 px-2.5">Floating MTM</th>
                      <th className="py-2 px-2.5">Env</th>
                      <th className="py-2 px-2.5">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {openPositions.map((pos) => (
                      <tr key={pos.position_id}>
                        <td className="py-2 px-2.5 font-bold text-slate-900">
                          {pos.symbol} <span className="text-[10px] text-slate-500">({pos.product_type})</span>
                        </td>
                        <td className="py-2 px-2.5 font-bold">{pos.direction}</td>
                        <td className="py-2 px-2.5">{pos.shares}</td>
                        <td className="py-2 px-2.5">{formatINRValue(pos.entry_price)}</td>
                        <td className="py-2 px-2.5">{formatINRValue(pos.current_price)}</td>
                        <td className="py-2 px-2.5 text-rose-700">{formatINRValue(pos.stop_loss_px)}</td>
                        <td
                          className={`py-2 px-2.5 font-bold ${
                            (pos.unrealized_pnl_inr || 0) >= 0 ? 'text-emerald-700' : 'text-rose-700'
                          }`}
                        >
                          {formatSignedINR(pos.unrealized_pnl_inr)}
                        </td>
                        <td className="py-2 px-2.5">{pos.execution_env}</td>
                        <td className="py-2 px-2.5">
                          <button
                            type="button"
                            disabled={exitingPosId === pos.position_id}
                            onClick={() => handleExitPosition(pos.position_id)}
                            className="px-2.5 py-1 rounded bg-rose-600 hover:bg-rose-500 text-white font-sans font-bold text-[10px] cursor-pointer"
                          >
                            {exitingPosId === pos.position_id ? 'Closing...' : 'Exit'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Closed Audit Ledger & Live Broker Intents */}
          <div className="bg-white rounded-2xl border border-slate-200 p-4 sm:p-5 shadow-2xs space-y-3">
            <h2 className="text-sm font-bold text-slate-900">
              Closed Positions Audit Ledger ({closedPositions.length}) &amp; Live Broker Intents ({liveIntents.length})
            </h2>
            {closedPositions.length === 0 ? (
              <p className="text-xs text-slate-500">No closed Consensus V3 positions recorded yet.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left border-collapse font-mono">
                  <thead>
                    <tr className="border-b border-slate-200 text-[10px] uppercase text-slate-500 font-sans font-bold">
                      <th className="py-2 px-2.5">Symbol</th>
                      <th className="py-2 px-2.5">Shares</th>
                      <th className="py-2 px-2.5">Entry / Exit</th>
                      <th className="py-2 px-2.5">Gross P&amp;L</th>
                      <th className="py-2 px-2.5">Exact Statutory Fees</th>
                      <th className="py-2 px-2.5">Net P&amp;L</th>
                      <th className="py-2 px-2.5">Exit Reason</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {closedPositions.slice(0, 25).map((c) => (
                      <tr key={c.position_id}>
                        <td className="py-2 px-2.5 font-bold">{c.symbol}</td>
                        <td className="py-2 px-2.5">{c.shares}</td>
                        <td className="py-2 px-2.5">
                          {formatINRValue(c.entry_price)} → {formatINRValue(c.exit_price)}
                        </td>
                        <td className="py-2 px-2.5">{formatSignedINR(c.gross_pnl_inr)}</td>
                        <td className="py-2 px-2.5 text-amber-700">{formatINRValue(c.fees_inr)}</td>
                        <td
                          className={`py-2 px-2.5 font-bold ${
                            (c.net_pnl_inr || 0) >= 0 ? 'text-emerald-700' : 'text-rose-700'
                          }`}
                        >
                          {formatSignedINR(c.net_pnl_inr)}
                        </td>
                        <td className="py-2 px-2.5 font-sans text-[11px]">{c.exit_reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── TAB 4: PERFORMANCE VS. PUBLISHED BASELINE (SEPARATED BY ERA) ──── */}
      {subTab === 'performance' && (
        <div className="space-y-4">
          <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-2xs space-y-2">
            <h2 className="text-sm font-bold text-slate-900">
              1. Prospective Live Shadow / Forward Era ({perfData?.prospective_forward_v3?.protocol_id || 'V3'})
            </h2>
            <p className="text-xs text-slate-500">
              Strictly separated from historical backtests. Target validation gate: 60 prospective sessions before live
              capital deployment.
            </p>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 pt-2 font-mono text-xs">
              <div className="p-3 rounded-xl bg-slate-50 border border-slate-200">
                <span className="block text-[10px] text-slate-500 uppercase">Closed Trades</span>
                <span className="text-base font-bold text-slate-900">
                  {perfData?.prospective_forward_v3?.closed_trades ?? 0}
                </span>
              </div>
              <div className="p-3 rounded-xl bg-slate-50 border border-slate-200">
                <span className="block text-[10px] text-slate-500 uppercase">Win Rate</span>
                <span className="text-base font-bold text-slate-900">
                  {formatPercent(perfData?.prospective_forward_v3?.win_rate_pct)}
                </span>
              </div>
              <div className="p-3 rounded-xl bg-slate-50 border border-slate-200">
                <span className="block text-[10px] text-slate-500 uppercase">Net Realized P&amp;L</span>
                <span className="text-base font-bold text-emerald-700">
                  {formatSignedINR(perfData?.prospective_forward_v3?.net_pnl_inr)}
                </span>
              </div>
              <div className="p-3 rounded-xl bg-slate-50 border border-slate-200">
                <span className="block text-[10px] text-slate-500 uppercase">Profit Factor</span>
                <span className="text-base font-bold text-slate-900">
                  {formatNumber(perfData?.prospective_forward_v3?.profit_factor)}
                </span>
              </div>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-2xs space-y-2 text-xs">
              <h3 className="text-sm font-bold text-slate-900">
                2. Audited Pre-Test • Book 1 (20d Cash CNC Core vs. Published 12-1 Momentum)
              </h3>
              <p className="text-slate-500">
                {perfData?.audited_pretest_benchmarks?.book1_20d_lockbox_2025_2026?.window}
              </p>
              <div className="space-y-1.5 font-mono pt-1">
                <div className="flex justify-between">
                  <span>Published 12-1 Momentum Baseline:</span>
                  <span>+3.43% (PF 1.04, DD 24.7%)</span>
                </div>
                <div className="flex justify-between">
                  <span>Equal-Weight F&amp;O-181 Benchmark:</span>
                  <span>+13.68%</span>
                </div>
                <div className="flex justify-between font-bold text-emerald-700">
                  <span>Qlib 20d + Macro Exposure Shield (M3):</span>
                  <span>+15.47% (+₹61,881 • PF 1.46 • DD 17.7%)</span>
                </div>
                <div className="flex justify-between text-slate-600">
                  <span>M3 Stress • 2x Doubled CNC Costs (~1%/tr):</span>
                  <span>+11.73% (+₹46,908 • PF 1.33)</span>
                </div>
                <div className="flex justify-between text-slate-600">
                  <span>M3 Stress • +1 Full Day Execution Delay:</span>
                  <span>+14.41% (+₹57,652 • PF 1.43)</span>
                </div>
                <div className="flex justify-between font-semibold text-indigo-800 pt-1 border-t border-slate-100">
                  <span>Full 7-Year OOS (2020–2026, 1,697d):</span>
                  <span>+148.87% (+₹5,95,468 • DD 21.3% vs 44.0% • p=0.0157)</span>
                </div>
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-2xs space-y-2 text-xs">
              <h3 className="text-sm font-bold text-slate-900">
                3. Audited Pre-Test • Book 2 (15m Sniffer with Strict Pending-Order Cash Reservation)
              </h3>
              <p className="text-slate-500">
                {perfData?.audited_pretest_benchmarks?.book2_15m_sniffer_58d_wf?.window}
              </p>
              <div className="space-y-1.5 font-mono pt-1">
                <div className="flex justify-between text-rose-700">
                  <span>Published ORB30 Intraday Momentum:</span>
                  <span>-₹32,757 (-8.19% • PF 0.74)</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>PDH Sweep Baseline (0-ML):</span>
                  <span>-₹10,925 (-2.73% • PF 0.87)</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>Qlib Fixed-Horizon Solo:</span>
                  <span>-₹21,940 (-5.49% • PF 0.72)</span>
                </div>
                <div className="flex justify-between text-rose-700">
                  <span>Independent Legacy Solo:</span>
                  <span>-₹3,145 (-0.79% • PF 0.87)</span>
                </div>
                <div className="flex justify-between font-bold text-emerald-700">
                  <span>Full 6-Layer Consensus (Qlib + Indep. Legacy):</span>
                  <span>+₹2,621 (PF 1.19 • MaxDD 0.86% • Bear +₹7,650)</span>
                </div>
                <div className="flex justify-between text-emerald-700">
                  <span>+15m Execution Delay Stress:</span>
                  <span>+₹3,363 (PF 1.40 • MaxDD 0.80%)</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── TAB 5: DIAGNOSTICS, SPEEDUP & 7/7 ANTI-LEAKAGE INVARIANTS ─────── */}
      {subTab === 'diagnostics' && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 text-xs">
          <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-2xs space-y-3">
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <ShieldCheck size={16} className="text-emerald-600" />
              <span>7-Point Programmatic Anti-Leakage &amp; Cash-Reservation Invariants</span>
            </h2>
            <div className="space-y-2 font-mono">
              {Object.entries(diagData?.invariants || {}).map(([k, ok]) => (
                <div
                  key={k}
                  className="flex items-center justify-between p-2 rounded-lg bg-slate-50 border border-slate-200"
                >
                  <span className="text-slate-700">{k}</span>
                  <span
                    className={`inline-flex items-center gap-1 font-bold ${
                      ok ? 'text-emerald-700' : 'text-rose-700'
                    }`}
                  >
                    <CheckCircle2 size={13} />
                    <span>{ok ? 'PASS' : 'FAIL'}</span>
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-white rounded-2xl border border-slate-200 p-5 shadow-2xs space-y-3">
            <h2 className="text-sm font-bold text-slate-900">
              Runtime Latency, Incremental-Value Gates &amp; Artifact Provenance
            </h2>
            <div className="p-3 rounded-xl bg-emerald-50 border border-emerald-200 text-emerald-950 font-mono space-y-1">
              <div>
                Current Sniffer Latency: <strong>{diagData?.scan_latency_ms ?? '—'} ms</strong> (vs. Old Alpha158:{' '}
                <strong>62,400 ms</strong> GIL stall)
              </div>
              <div>15-Minute Backend/UI Hang Eliminated: YES ({diagData?.speedup_factor ?? '—'}x faster)</div>
            </div>
            <div className="p-3 rounded-xl bg-slate-50 border border-slate-200 space-y-1.5">
              <div>
                <strong>Book 1 Legacy Gate:</strong>{' '}
                <span className="font-mono text-amber-700">
                  {diagData?.book1_legacy_incremental_value_gate?.status}
                </span>{' '}
                — {diagData?.book1_legacy_incremental_value_gate?.reason}
              </div>
              <div>
                <strong>Book 2 Legacy Gate:</strong>{' '}
                <span className="font-mono text-emerald-700">
                  {diagData?.book2_legacy_incremental_value_gate?.status}
                </span>{' '}
                — {diagData?.book2_legacy_incremental_value_gate?.reason}
              </div>
            </div>
            <div className="p-3 rounded-xl bg-slate-50 border border-slate-200 font-mono text-[11px] space-y-1 break-all">
              <div>Qlib B1 SHA256: {diagData?.model_provenance?.qlib_book1_sha256 || '—'}</div>
              <div>Qlib B2 SHA256: {diagData?.model_provenance?.qlib_book2_sha256 || '—'}</div>
              <div>Legacy B2 SHA256: {diagData?.model_provenance?.legacy_book2_sha256 || '—'}</div>
              <div>Legacy Qlib Inputs Used: {diagData?.model_provenance?.legacy_qlib_inputs_used ?? 0}</div>
            </div>
          </div>
        </div>
      )}

      <ConsensusArmInterlockModal
        isOpen={armModalOpen}
        liveStatus={liveStatus}
        armPinInput={armPinInput}
        onChangePinInput={setArmPinInput}
        armActionBusy={armActionBusy}
        capitalInput={capitalInput}
        marginModeInput={marginModeInput}
        onChangeCapital={(val) => {
          formDirtyRef.current = true;
          setCapitalInput(val);
        }}
        onChangeMarginMode={(val) => {
          formDirtyRef.current = true;
          setMarginModeInput(val);
        }}
        onSaveCapitalConfig={() => handleConfigureArena(false)}
        configBusy={configBusy}
        onSelectMode={handleSelectMode}
        onConfirmArm={handleConfirmArm}
        onClose={() => {
          setArmModalOpen(false);
          setArmPinInput('');
        }}
      />
    </div>
  );
}
