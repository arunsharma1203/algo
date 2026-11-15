import React from 'react';
import {
  AlertTriangle,
  Layers,
  Lock,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Unlock,
  Wifi,
  X,
} from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  formatClockIST,
  formatTimestampLabel,
  hasValidNumber,
} from './formatters';

export default function WorkspaceHeader({
  executionStatus,
  statusData,
  activeReadiness,
  liveStatus,
  sliceMeta,
  openPaperCount,
  pendingLeg2Count,
  unwindCount,
  sweeping,
  armActionBusy,
  armActionMsg,
  onClearArmMsg,
  onOpenArmModal,
  onDisarm,
  onSosExit,
  onManualSweep,
}) {
  const liveActiveCount = Array.isArray(liveStatus?.active_live_positions)
    ? liveStatus.active_live_positions.length
    : null;
  const hasBrokerExposure = (liveActiveCount ?? 0) > 0;
  const showEmergencySos =
    hasBrokerExposure ||
    executionStatus.isArmed ||
    executionStatus.state === 'UNKNOWN' ||
    executionStatus.state === 'STALE';

  const engineVersion =
    statusData?.engine_version || activeReadiness?.engine_version || UNAVAILABLE_LABEL;
  const protocolId =
    activeReadiness?.shadow_telemetry?.protocol_id ||
    statusData?.shadow_telemetry?.protocol_id ||
    UNAVAILABLE_LABEL;
  const sessionLabel =
    activeReadiness?.timing?.session_phase_label ||
    (typeof statusData?.is_trading_window === 'boolean'
      ? statusData.is_trading_window
        ? 'TRADING SESSION'
        : 'OUTSIDE TRADING WINDOW'
      : UNAVAILABLE_LABEL);

  const wsConnected =
    typeof statusData?.stream_health?.websocket_connected === 'boolean'
      ? statusData.stream_health.websocket_connected
      : typeof activeReadiness?.upstox_v3_feed?.websocket_connected === 'boolean'
      ? activeReadiness.upstox_v3_feed.websocket_connected
      : null;

  const parityInSync =
    typeof statusData?.runtime_parity?.in_sync === 'boolean'
      ? statusData.runtime_parity.in_sync
      : null;

  const isHalted =
    Boolean(liveStatus?.circuit_breaker?.is_session_halted) ||
    Boolean(activeReadiness?.session_risk?.daily_kill_triggered);

  const badgeClasses =
    executionStatus.tone === 'danger'
      ? 'bg-rose-950/80 text-rose-200 border-rose-500/60'
      : executionStatus.tone === 'warning'
      ? 'bg-amber-950/80 text-amber-200 border-amber-500/60'
      : 'bg-slate-800 text-emerald-300 border-emerald-600/50';

  const latestRetrievalMs = Math.max(
    sliceMeta?.live?.lastSuccessMs || 0,
    sliceMeta?.readiness?.lastSuccessMs || 0,
    sliceMeta?.status?.lastSuccessMs || 0
  );

  return (
    <header className="bg-slate-900 text-slate-100 rounded-2xl border border-slate-800 shadow-sm overflow-hidden">
      <div className="p-4 sm:p-5 flex flex-col xl:flex-row xl:items-center xl:justify-between gap-4">
        {/* LEFT: Observation & Confirmed Operational Status */}
        <div className="space-y-2.5 min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-semibold tracking-wide border ${badgeClasses}`}
              title={executionStatus.detail}
            >
              {executionStatus.isArmed ? (
                <Unlock size={13} className="shrink-0" />
              ) : executionStatus.canConfirmSafe ? (
                <Lock size={13} className="shrink-0" />
              ) : (
                <AlertTriangle size={13} className="shrink-0" />
              )}
              <span>{executionStatus.label}</span>
            </span>

            <span className="px-2.5 py-1 rounded-md text-xs font-mono bg-slate-800/90 text-slate-200 border border-slate-700 max-w-full break-words">
              Runtime: {engineVersion} • Forward: {protocolId}
            </span>

            <span className="px-2.5 py-1 rounded-md text-xs font-mono bg-slate-800/90 text-slate-300 border border-slate-700 max-w-full break-words">
              Session: {sessionLabel}
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
                  : UNAVAILABLE_LABEL}
              </span>
            </span>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1.5">
            <div className="flex items-center gap-2">
              <Layers className="text-slate-300 shrink-0" size={20} />
              <h1 className="text-lg sm:text-xl font-bold tracking-tight text-white">
                Option Sniffer Advance
              </h1>
              <span className="text-xs text-slate-400 hidden sm:inline">
                Operator Workstation
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs font-mono text-slate-300 tabular-nums">
              <span>
                Broker Exposure:{' '}
                <strong className={hasBrokerExposure ? 'text-amber-300' : 'text-slate-200'}>
                  {liveActiveCount !== null ? `${liveActiveCount} open` : UNAVAILABLE_LABEL}
                </strong>
              </span>
              <span>
                Paper Exposure:{' '}
                <strong className="text-slate-200">
                  {hasValidNumber(openPaperCount) ? `${openPaperCount} open` : UNAVAILABLE_LABEL}
                </strong>
              </span>
              <span>
                Retrieved:{' '}
                <strong className="text-slate-200">
                  {latestRetrievalMs > 0 ? formatClockIST(latestRetrievalMs) : UNAVAILABLE_LABEL}
                </strong>
              </span>
              <span>
                Last Sweep:{' '}
                <strong className="text-slate-300">
                  {formatTimestampLabel(statusData?.last_sweep_time)}
                </strong>
              </span>
            </div>
          </div>
        </div>

        {/* RIGHT: Separated Operator & Emergency Controls */}
        <div
          className="flex flex-wrap items-center gap-2 pt-3 xl:pt-0 border-t xl:border-t-0 xl:border-l border-slate-800 xl:pl-4 shrink-0"
          aria-label="Execution and surveillance controls"
        >
          {showEmergencySos && (
            <button
              type="button"
              onClick={onSosExit}
              disabled={armActionBusy}
              className="px-3.5 py-2 rounded-lg bg-rose-600 hover:bg-rose-500 disabled:opacity-50 text-white font-semibold text-xs tracking-wide flex items-center gap-1.5 shadow-xs transition cursor-pointer"
              title="Emergency Broker Square-Off & Disarm"
            >
              <ShieldAlert size={14} />
              <span>
                SOS Flatten Broker
                {liveActiveCount !== null ? ` (${liveActiveCount})` : ''}
              </span>
            </button>
          )}

          {executionStatus.isArmed ? (
            <button
              type="button"
              onClick={onDisarm}
              disabled={armActionBusy}
              className="px-3.5 py-2 rounded-lg bg-amber-500 hover:bg-amber-400 disabled:opacity-50 text-slate-950 font-semibold text-xs tracking-wide flex items-center gap-1.5 transition cursor-pointer"
            >
              <Lock size={14} />
              <span>Disarm Broker</span>
            </button>
          ) : (
            <button
              type="button"
              onClick={onOpenArmModal}
              disabled={armActionBusy}
              className="px-3.5 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-600 disabled:opacity-50 text-slate-100 font-semibold text-xs tracking-wide flex items-center gap-1.5 transition cursor-pointer"
            >
              <ShieldCheck size={14} />
              <span>Broker Interlock...</span>
            </button>
          )}

          <button
            type="button"
            onClick={onManualSweep}
            disabled={sweeping}
            className="px-3.5 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white font-semibold text-xs tracking-wide flex items-center gap-1.5 transition cursor-pointer"
          >
            <RefreshCw size={14} className={sweeping ? 'animate-spin' : ''} />
            <span>{sweeping ? 'Running Sweep...' : 'Run Surveillance Sweep'}</span>
          </button>
        </div>
      </div>

      {/* Operational Alerts Strip (Rendered whenever unknown, stale, desynchronized, halted, or incomplete leg exists) */}
      {(executionStatus.tone !== 'safe' ||
        parityInSync === false ||
        isHalted ||
        (pendingLeg2Count || 0) > 0 ||
        (unwindCount || 0) > 0 ||
        armActionMsg) && (
        <div className="bg-slate-950/80 border-t border-slate-800 px-4 sm:px-5 py-2.5 space-y-1.5 text-xs">
          {executionStatus.tone !== 'safe' && (
            <div className="flex items-center gap-2 text-amber-200 font-mono">
              <AlertTriangle size={13} className="text-amber-400 shrink-0" />
              <span>{executionStatus.detail}</span>
            </div>
          )}

          {parityInSync === false && (
            <div className="flex items-center gap-2 text-amber-200 font-mono">
              <AlertTriangle size={13} className="text-amber-400 shrink-0" />
              <span>
                Runtime RAM/Disk hash mismatch detected (
                {(statusData?.runtime_parity?.stale_files || []).join(', ') || 'check Diagnostics'}
                ).
              </span>
            </div>
          )}

          {isHalted && (
            <div className="flex items-center gap-2 text-rose-200 font-mono">
              <ShieldAlert size={13} className="text-rose-400 shrink-0" />
              <span>
                Daily session halt active:{' '}
                {liveStatus?.circuit_breaker?.halt_reason ||
                  'Daily MTM loss trigger reached — new entries blocked.'}
              </span>
            </div>
          )}

          {((pendingLeg2Count || 0) > 0 || (unwindCount || 0) > 0) && (
            <div className="flex items-center gap-2 text-amber-200 font-mono">
              <AlertTriangle size={13} className="text-amber-400 shrink-0" />
              <span>
                Spread leg alerts — Pending Leg-2: {pendingLeg2Count || 0} • Incomplete-Leg Wing
                Unwinds recorded: {unwindCount || 0}.
              </span>
            </div>
          )}

          {armActionMsg && (
            <div
              className={`flex items-center justify-between gap-2 px-3 py-1.5 rounded border font-mono ${
                armActionMsg.type === 'error'
                  ? 'bg-rose-950/80 text-rose-200 border-rose-700'
                  : 'bg-slate-900 text-slate-200 border-slate-700'
              }`}
            >
              <span>{armActionMsg.text}</span>
              <button
                type="button"
                onClick={onClearArmMsg}
                className="text-slate-400 hover:text-white cursor-pointer"
                aria-label="Dismiss message"
              >
                <X size={13} />
              </button>
            </div>
          )}
        </div>
      )}
    </header>
  );
}
