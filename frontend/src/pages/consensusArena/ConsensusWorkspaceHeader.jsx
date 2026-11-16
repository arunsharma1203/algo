import React from 'react';
import {
  AlertTriangle,
  Lock,
  RefreshCw,
  ShieldAlert,
  Unlock,
  Wifi,
  X,
  Zap,
} from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  formatClockIST,
  formatINRValue,
  formatSignedINR,
  hasValidNumber,
} from '../optionSnifferAdvance/formatters';

export default function ConsensusWorkspaceHeader({
  statusData,
  liveStatus,
  sliceMeta,
  sweeping,
  armActionBusy,
  armActionMsg,
  onClearArmMsg,
  onOpenArmModal,
  onDisarm,
  onSosExit,
  onManualSweep,
}) {
  const statusOk = sliceMeta?.status?.status === 'ok' && Boolean(liveStatus);
  const isArmed = statusOk && liveStatus?.is_armed === true && liveStatus?.simulation_mode === false;
  const canConfirmPaper = statusOk && liveStatus?.simulation_mode === true && !liveStatus?.is_armed;

  const badgeTone = isArmed ? 'danger' : canConfirmPaper ? 'safe' : 'warning';
  const badgeClasses =
    badgeTone === 'danger'
      ? 'bg-rose-950/80 text-rose-200 border-rose-500/60'
      : badgeTone === 'warning'
      ? 'bg-amber-950/80 text-amber-200 border-amber-500/60'
      : 'bg-slate-800 text-emerald-300 border-emerald-600/50';

  const badgeLabel = isArmed
    ? `ARMED • LIVE UPSTOX (${liveStatus?.execution_mode || 'DUAL'})`
    : canConfirmPaper
    ? 'DISARMED • PAPER SHADOW (simulation_mode=true)'
    : 'EXECUTION STATUS SYNCING';

  const acct = liveStatus?.accounting || {};
  const liveActiveCount = Array.isArray(liveStatus?.active_live_positions)
    ? liveStatus.active_live_positions.length
    : 0;
  const openPosCount =
    (acct?.book1_slots?.open || 0) + (acct?.book2_slots?.open || 0);
  const showSos = isArmed || liveActiveCount > 0 || openPosCount > 0 || !statusOk;

  const wsConnected = liveStatus?.stream_health?.websocket_connected;
  const latestMs = Math.max(
    sliceMeta?.status?.lastSuccessMs || 0,
    sliceMeta?.positions?.lastSuccessMs || 0,
    sliceMeta?.signals?.lastSuccessMs || 0
  );

  return (
    <header className="bg-slate-900 text-slate-100 rounded-2xl border border-slate-800 shadow-sm overflow-hidden">
      <div className="p-4 sm:p-5 flex flex-col xl:flex-row xl:items-center xl:justify-between gap-4">
        {/* LEFT: Confirmed Operational Telemetry */}
        <div className="space-y-2.5 min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-semibold tracking-wide border ${badgeClasses}`}
            >
              {isArmed ? (
                <Unlock size={13} className="shrink-0" />
              ) : canConfirmPaper ? (
                <Lock size={13} className="shrink-0" />
              ) : (
                <AlertTriangle size={13} className="shrink-0" />
              )}
              <span>{badgeLabel}</span>
            </span>

            <span className="px-2.5 py-1 rounded-md text-xs font-mono bg-slate-800/90 text-slate-200 border border-slate-700">
              {statusData?.engine_version || 'CONSENSUS_V3_6LAYER'} • {statusData?.protocol_id || 'SHADOW_V3'}
            </span>

            <span className="px-2.5 py-1 rounded-md text-xs font-mono bg-slate-800/90 text-slate-300 border border-slate-700">
              {statusData?.session_phase_label || UNAVAILABLE_LABEL}
            </span>

            <span
              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-mono border ${
                wsConnected === true
                  ? 'bg-slate-800/90 text-emerald-300 border-emerald-700/50'
                  : wsConnected === false
                  ? 'bg-amber-950/70 text-amber-200 border-amber-600/50'
                  : 'bg-slate-800/90 text-slate-400 border-slate-700'
              }`}
            >
              <Wifi size={12} />
              <span>
                Feed:{' '}
                {wsConnected === true
                  ? 'Connected'
                  : wsConnected === false
                  ? 'Disconnected'
                  : 'Local SQLite'}
              </span>
            </span>

            {hasValidNumber(statusData?.last_sweep_elapsed_ms) && (
              <span className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono bg-emerald-950/60 text-emerald-300 border border-emerald-700/50">
                <Zap size={12} />
                <span>Sniffer: {Number(statusData.last_sweep_elapsed_ms).toFixed(0)}ms</span>
              </span>
            )}
          </div>

          <div>
            <h1 className="text-lg sm:text-xl font-bold tracking-tight text-white">
              Equity Consensus Sniffer &amp; Funded-Cash Arena
            </h1>
            <p className="text-xs text-slate-400 mt-0.5">
              6-Layer Audited Architecture • Book 1 (20d F&amp;O-181 Cash CNC Core + Macro Shield) • Book 2 (15m Post-Only Limit Sniffer on Idle Cash) •{' '}
              {latestMs > 0 ? `Updated ${formatClockIST(latestMs)}` : 'Syncing telemetry...'}
            </p>
          </div>
        </div>

        {/* RIGHT: Execution Controls */}
        <div className="flex flex-wrap items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={onManualSweep}
            disabled={sweeping}
            className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-100 border border-slate-700 disabled:opacity-50 cursor-pointer"
          >
            <RefreshCw size={13} className={sweeping ? 'animate-spin text-emerald-400' : 'text-emerald-400'} />
            <span>{sweeping ? 'Sweeping...' : 'Run Sniffer Sweep'}</span>
          </button>

          {isArmed ? (
            <button
              type="button"
              onClick={onDisarm}
              disabled={armActionBusy}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-xs font-bold bg-amber-500 hover:bg-amber-400 text-slate-950 disabled:opacity-50 cursor-pointer"
            >
              <Lock size={13} />
              <span>Disarm to Paper</span>
            </button>
          ) : (
            <button
              type="button"
              onClick={onOpenArmModal}
              disabled={armActionBusy}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-xs font-bold bg-slate-800 hover:bg-slate-700 text-amber-300 border border-amber-500/40 disabled:opacity-50 cursor-pointer"
            >
              <Unlock size={13} />
              <span>Arm Live Bridge</span>
            </button>
          )}

          {showSos && (
            <button
              type="button"
              onClick={onSosExit}
              disabled={armActionBusy}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-xs font-bold bg-rose-600 hover:bg-rose-500 text-white shadow-sm disabled:opacity-50 cursor-pointer"
              title="Cancels all pending limit reservations, flattens open positions, and disarms to Paper Shadow"
            >
              <ShieldAlert size={14} />
              <span>SOS Flatten</span>
            </button>
          )}
        </div>
      </div>

      {/* FUNDED CASH CONSERVATION BAR (0x Leverage Invariant) */}
      <div className="px-4 sm:px-5 py-3 bg-slate-950/70 border-t border-slate-800 grid grid-cols-2 lg:grid-cols-5 gap-3 text-xs font-mono tabular-nums">
        <div>
          <span className="text-slate-400 block text-[10px] uppercase">Total Funded Equity</span>
          <span className="text-white font-bold text-sm">{formatINRValue(acct.current_equity_inr)}</span>
        </div>
        <div>
          <span className="text-slate-400 block text-[10px] uppercase">Free Unreserved Cash</span>
          <span className="text-emerald-300 font-bold text-sm">{formatINRValue(acct.free_unreserved_cash_inr)}</span>
        </div>
        <div>
          <span className="text-slate-400 block text-[10px] uppercase">Pending Limit Reserved</span>
          <span className="text-amber-300 font-bold text-sm">
            {formatINRValue(acct.pending_reserved_cash_inr)}{' '}
            <span className="text-[10px] text-slate-400">({acct?.book2_slots?.pending || 0} resting)</span>
          </span>
        </div>
        <div>
          <span className="text-slate-400 block text-[10px] uppercase">Active Invested Notional</span>
          <span className="text-sky-300 font-bold text-sm">
            {formatINRValue(acct.invested_notional_inr)}{' '}
            <span className="text-[10px] text-slate-400">
              (B1:{acct?.book1_slots?.open || 0} • B2:{acct?.book2_slots?.open || 0})
            </span>
          </span>
        </div>
        <div>
          <span className="text-slate-400 block text-[10px] uppercase">Realized + Floating P&amp;L</span>
          <span
            className={`font-bold text-sm ${
              (acct.realized_pnl_inr || 0) + (acct.unrealized_pnl_inr || 0) >= 0
                ? 'text-emerald-400'
                : 'text-rose-400'
            }`}
          >
            {formatSignedINR((acct.realized_pnl_inr || 0) + (acct.unrealized_pnl_inr || 0))}
          </span>
        </div>
      </div>

      {armActionMsg && (
        <div
          className={`px-4 sm:px-5 py-2.5 border-t flex items-center justify-between text-xs font-mono ${
            armActionMsg.tone === 'error'
              ? 'bg-rose-950/90 border-rose-700 text-rose-200'
              : 'bg-emerald-950/90 border-emerald-700 text-emerald-200'
          }`}
        >
          <span>{armActionMsg.text}</span>
          <button
            type="button"
            onClick={onClearArmMsg}
            className="p-1 text-slate-300 hover:text-white cursor-pointer"
          >
            <X size={14} />
          </button>
        </div>
      )}
    </header>
  );
}
