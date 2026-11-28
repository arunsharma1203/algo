import React, { useState } from 'react';
import {
  Activity,
  GitBranch,
  Layers,
  HeartPulse,
  Shield,
  ShieldAlert,
  Play,
  RefreshCw,
  AlertOctagon,
  Sliders,
} from 'lucide-react';
import {
  postV2LiveDisarm,
  postV2LiveSosFlatten,
  postV2PaperMode,
  postV2StartCollector,
  postV2ReconcileBookingFault,
} from './v2LiveApiClient';

const PAPER_MODES = [
  { id: 'AUTO', label: 'Auto' },
  { id: 'BOOK_A_ONLY', label: 'Book A' },
  { id: 'BOOK_B_ONLY', label: 'Book B' },
];

const NAV_TABS = [
  { id: 'overview', label: 'Overview', icon: Activity },
  { id: 'signals', label: 'Signals & Gates', icon: GitBranch },
  { id: 'positions', label: 'Positions & Trades', icon: Layers },
  { id: 'health', label: 'System Health', icon: HeartPulse },
];

export default function V2WorkspaceHeader({
  activeTab,
  onSelectTab,
  overview,
  liveStatus,
  isLoading,
  onRefreshAll,
  onOpenArmModal,
}) {
  const [busyAction, setBusyAction] = useState('');
  const [actionError, setActionError] = useState('');

  const armed = Boolean(liveStatus?.armed);
  const liveScope = liveStatus?.live_book_scope || 'PAPER_ONLY';
  const paperMode = String(overview?.paper_execution_mode || 'AUTO').toUpperCase();
  const collectorState = overview?.collection_status?.collector_state || 'STANDBY_NOT_STARTED';
  const isCollectorRunning =
    Boolean(overview?.collection_status?.listener_attached) ||
    collectorState === 'PARALLEL_LIVE_COLLECTION_ACTIVE' ||
    collectorState === 'IDLE_NO_RECENT_EVENTS' ||
    collectorState === 'STANDBY_STREAM_INACTIVE' ||
    collectorState === 'DEGRADED_BOOKING_FAULT';
  const hasOpenLivePositions = Number(liveStatus?.open_package_count || 0) > 0;
  const isBookingHalted = Boolean(overview?.collection_status?.booking_halted);

  const runAction = async (key, fn) => {
    setBusyAction(key);
    setActionError('');
    try {
      await fn();
      if (onRefreshAll) await onRefreshAll();
    } catch (err) {
      setActionError(err?.message || 'Action failed');
    } finally {
      setBusyAction('');
    }
  };

  return (
    <header className="bg-slate-900 border border-slate-800 rounded-xl shadow-lg mb-5 overflow-hidden">
      <div className="px-4 py-3.5 flex flex-wrap items-center justify-between gap-3 border-b border-slate-800/90">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2.5">
            <div className="h-9 w-9 rounded-lg bg-sky-500/15 border border-sky-500/30 flex items-center justify-center text-sky-400">
              <Activity className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <h1 className="text-base sm:text-lg font-bold tracking-tight text-white">
                  Option Sniffer Advance V2
                </h1>
                <span className="px-2 py-0.5 text-[11px] font-medium rounded-full bg-slate-800 text-slate-300 border border-slate-700">
                  NIFTY Dual-Book VRP
                </span>
              </div>
              <p className="text-xs text-slate-400">
                Book A Friday 50/100w Intraday Credit &bull; Book B Paid Convexity (VRP &le; 1.15)
              </p>
            </div>
          </div>

          <span
            className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold border ${
              armed
                ? 'bg-amber-500/15 text-amber-300 border-amber-500/40'
                : 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30'
            }`}
          >
            {armed ? <ShieldAlert className="h-3.5 w-3.5" /> : <Shield className="h-3.5 w-3.5" />}
            {armed ? `LIVE ARMED • ${liveScope}` : 'DISARMED • PAPER BASELINE ACTIVE'}
          </span>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          {/* Paper Mode Selector */}
          <div className="inline-flex items-center bg-slate-950 border border-slate-800 rounded-lg p-0.5">
            <span className="px-2 text-[11px] font-medium text-slate-400 hidden sm:inline">Paper:</span>
            {PAPER_MODES.map((m) => {
              const active = paperMode === m.id;
              return (
                <button
                  key={m.id}
                  type="button"
                  disabled={Boolean(busyAction)}
                  onClick={() => runAction(`mode_${m.id}`, () => postV2PaperMode(m.id))}
                  className={`px-2.5 py-1 text-xs font-semibold rounded-md transition ${
                    active
                      ? 'bg-sky-600 text-white shadow-sm'
                      : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
                  }`}
                >
                  {m.label}
                </button>
              );
            })}
          </div>

          {/* Start Collector / Reconcile Booking Fault */}
          {!isCollectorRunning && (
            <button
              type="button"
              disabled={Boolean(busyAction)}
              onClick={() => runAction('start_collector', () => postV2StartCollector())}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white transition disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />
              {busyAction === 'start_collector' ? 'Starting...' : 'Start Collector'}
            </button>
          )}

          {isBookingHalted && (
            <button
              type="button"
              disabled={Boolean(busyAction)}
              onClick={() => runAction('reconcile_fault', () => postV2ReconcileBookingFault())}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-amber-600 hover:bg-amber-500 text-white transition disabled:opacity-50"
            >
              Clear Booking Halt
            </button>
          )}

          {/* Arm / Live Mode Button */}
          <button
            type="button"
            disabled={Boolean(busyAction)}
            onClick={onOpenArmModal}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-100 border border-slate-700 transition"
          >
            <Sliders className="h-3.5 w-3.5 text-sky-400" />
            {armed ? 'Live Settings' : 'Arm / Mode'}
          </button>

          {armed && (
            <button
              type="button"
              disabled={Boolean(busyAction)}
              onClick={() => runAction('disarm', () => postV2LiveDisarm('HUMAN_HEADER_DISARM'))}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-slate-700 hover:bg-slate-600 text-white transition disabled:opacity-50"
            >
              Disarm
            </button>
          )}

          {(armed || hasOpenLivePositions) && (
            <button
              type="button"
              disabled={Boolean(busyAction)}
              onClick={() => {
                if (window.confirm('SOS FLATTEN: Immediately disarm live execution, cancel V2 working orders, and flatten ALL open V2 live positions?')) {
                  runAction('sos', () => postV2LiveSosFlatten('OPERATOR_HEADER_SOS_FLATTEN'));
                }
              }}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold rounded-lg bg-rose-600 hover:bg-rose-500 text-white shadow-sm transition disabled:opacity-50"
            >
              <AlertOctagon className="h-3.5 w-3.5" />
              SOS Flatten
            </button>
          )}

          <button
            type="button"
            disabled={isLoading || Boolean(busyAction)}
            onClick={() => onRefreshAll && onRefreshAll()}
            title="Refresh telemetry"
            className="inline-flex items-center justify-center p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition disabled:opacity-50"
          >
            <RefreshCw className={`h-4 w-4 ${isLoading ? 'animate-spin text-sky-400' : ''}`} />
          </button>
        </div>
      </div>

      {actionError && (
        <div className="px-4 py-2 bg-rose-950/70 border-b border-rose-800 text-xs text-rose-200 flex items-center justify-between">
          <span>{actionError}</span>
          <button
            type="button"
            onClick={() => setActionError('')}
            className="underline text-rose-300 hover:text-white"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Navigation Tabs */}
      <nav className="px-4 py-2 bg-slate-950/60 flex items-center gap-1.5 overflow-x-auto">
        {NAV_TABS.map((t) => {
          const Icon = t.icon;
          const active = activeTab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => onSelectTab(t.id)}
              className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold whitespace-nowrap transition ${
                active
                  ? 'bg-sky-500/20 text-sky-300 border border-sky-500/40'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50 border border-transparent'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {t.label}
            </button>
          );
        })}
      </nav>
    </header>
  );
}
