import React from 'react';
import { CheckCircle2, AlertTriangle } from 'lucide-react';

export default function V2AuditTab({ overview, diagnostics, liveStatus }) {
  const hygiene = overview?.feed_hygiene || diagnostics?.feed_hygiene || {};
  const fc = hygiene.feed_consumer || {};
  const alerts = overview?.active_alerts || diagnostics?.active_alerts || [];
  const integrity = diagnostics?.protected_artifacts_integrity || {};
  const openBasis = hygiene.open_basis_reference || null;
  const sync = hygiene.spot_fut_sync || {};

  return (
    <div className="space-y-5">
      {/* Safety & Integrity Banner */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-1.5">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Collector &amp; L5 Stream</div>
          <div className="text-sm font-bold text-white">
            {overview?.collection_status?.collector_state || 'STANDBY_NOT_STARTED'}
          </div>
          <div className="text-xs text-slate-400">
            Bars Today: {hygiene.completed_bars_today ?? 0} &bull; ORB30:{' '}
            {hygiene.orb30_ready ? 'READY' : 'WARMING'}
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-1.5">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Spot / Futures Sync &amp; Open Basis</div>
          <div className={`text-sm font-bold ${sync.ok ? 'text-emerald-400' : 'text-amber-300'}`}>
            {sync.ok ? `SYNC OK (${sync.basis_pts ?? '—'}pt)` : sync.reason || 'Awaiting Sync'}
          </div>
          <div className="text-xs text-slate-400">
            Open Basis Ref:{' '}
            {openBasis?.ok ? `${openBasis.basis_pts}pt (${hygiene.open_basis_samples_count || 1} samples)` : 'Not anchored yet'}
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-1.5">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Protected Models &amp; Live Bridge</div>
          <div className="flex items-center gap-1.5 text-sm font-bold">
            {integrity.all_unchanged !== false ? (
              <>
                <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                <span className="text-emerald-400">V1 / Preregistration Locked</span>
              </>
            ) : (
              <>
                <AlertTriangle className="h-4 w-4 text-rose-400" />
                <span className="text-rose-400">Hash Drift Detected</span>
              </>
            )}
          </div>
          <div className="text-xs text-slate-400">
            Observer Attached: {liveStatus?.paper_observer_attached ? 'YES' : 'NO'} &bull; Faults:{' '}
            {liveStatus?.unresolved_faults?.total_blocking_faults ?? 0}
          </div>
        </div>
      </div>

      {/* Queue & Skew Hygiene Metrics */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
        <h3 className="text-sm font-bold text-white">Feed Latency, Queue Drop &amp; Isolation Counters</h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">Events Consumed</div>
            <div className="font-mono font-bold text-white mt-1">{fc.processed_total ?? 0}</div>
          </div>
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">Overflow Drops</div>
            <div className="font-mono font-bold text-white mt-1">{fc.dropped_overflow_events ?? 0}</div>
          </div>
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">Foreign Keys Rejected</div>
            <div className="font-mono font-bold text-white mt-1">{hygiene.foreign_instrument_rejected_count ?? 0}</div>
          </div>
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">p99 Processing / Skew</div>
            <div className="font-mono font-bold text-white mt-1">
              {fc.processing_lag_p99_ms ?? 0}ms / {fc.cross_leg_skew_p99_ms ?? 0}ms
            </div>
          </div>
        </div>
      </div>

      {/* Active System Alerts */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-2.5">
        <h3 className="text-sm font-bold text-white">Active System Alerts</h3>
        {alerts.length === 0 ? (
          <div className="text-xs text-emerald-400">Zero warnings or degraded safety latches active.</div>
        ) : (
          <div className="space-y-2">
            {alerts.map((a, idx) => (
              <div
                key={`${a.code || 'alert'}_${idx}`}
                className="p-2.5 rounded-lg bg-slate-950 border border-slate-800 text-xs text-slate-300 flex items-start gap-2"
              >
                <AlertTriangle className="h-4 w-4 text-amber-400 shrink-0 mt-0.5" />
                <div>
                  <span className="font-semibold text-white">{a.code}: </span>
                  <span>{a.message}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
