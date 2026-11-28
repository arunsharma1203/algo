import React, { useState } from 'react';
import { ShieldAlert, RefreshCw, LogOut } from 'lucide-react';
import { postV2LiveExitPosition, postV2LiveReconcile } from './v2LiveApiClient';

const fmtInr = (val) => {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return '—';
  const n = Number(val);
  return `${n >= 0 ? '+' : '-'}₹${Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export default function V2LivePositionsPanel({ liveStatus, livePositions, onRefreshAll }) {
  const [busyId, setBusyId] = useState('');
  const [err, setErr] = useState('');

  const openPkgs = livePositions?.open_packages || liveStatus?.open_packages || [];
  const closedPkgs = livePositions?.closed_packages || [];
  const closedTodayCount = Number(livePositions?.closed_today_count ?? 0);
  const armed = Boolean(liveStatus?.armed);

  const handleExit = async (pkgId) => {
    setBusyId(pkgId);
    setErr('');
    try {
      await postV2LiveExitPosition(pkgId, { reason: 'HUMAN_MANUAL_EXIT' });
      if (onRefreshAll) await onRefreshAll();
    } catch (e) {
      setErr(e?.message || 'Square-off failed');
    } finally {
      setBusyId('');
    }
  };

  const handleReconcile = async () => {
    setBusyId('__reconcile');
    setErr('');
    try {
      await postV2LiveReconcile();
      if (onRefreshAll) await onRefreshAll();
    } catch (e) {
      setErr(e?.message || 'Reconcile failed');
    } finally {
      setBusyId('');
    }
  };

  return (
    <section
      className={`bg-slate-900 border rounded-xl p-4 space-y-3 ${
        armed ? 'border-amber-500/40' : 'border-slate-800'
      }`}
    >
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <ShieldAlert className={`h-4 w-4 ${armed ? 'text-amber-400' : 'text-slate-400'}`} />
          <h3 className="text-sm font-bold text-white">Live Broker Execution Positions</h3>
          <span
            className={`px-2 py-0.5 text-[11px] font-semibold rounded border ${
              armed
                ? 'bg-amber-500/15 text-amber-300 border-amber-500/40'
                : 'bg-slate-800 text-slate-400 border-slate-700'
            }`}
          >
            {armed ? `ARMED • ${liveStatus?.live_book_scope}` : 'DISARMED'}
          </span>
          <span className="px-2 py-0.5 text-[11px] rounded bg-slate-800 text-slate-300 border border-slate-700">
            Open: {openPkgs.length} &bull; Closed Today: {closedTodayCount} (Total: {closedPkgs.length})
          </span>
        </div>
        <button
          type="button"
          disabled={Boolean(busyId)}
          onClick={handleReconcile}
          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold rounded bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 disabled:opacity-50"
        >
          <RefreshCw className={`h-3 w-3 ${busyId === '__reconcile' ? 'animate-spin' : ''}`} />
          Reconcile Broker
        </button>
      </div>

      {openPkgs.length === 0 && (
        <div className="text-xs text-slate-400">
          {armed
            ? 'Live engine armed — awaiting next validated Book A/B entry signal.'
            : 'Disarmed — click Arm / Mode in the header to enable live Upstox order execution.'}
        </div>
      )}

      {err && (
        <div className="p-2 rounded bg-rose-950/60 border border-rose-800 text-xs text-rose-200">
          {err}
        </div>
      )}

      {openPkgs.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs text-left">
            <thead className="text-[11px] text-slate-400 border-b border-slate-800">
              <tr>
                <th className="py-2 pr-3">Book / Structure</th>
                <th className="py-2 pr-3">State</th>
                <th className="py-2 pr-3">Buy / Wing</th>
                <th className="py-2 pr-3">Short Leg</th>
                <th className="py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/70">
              {openPkgs.map((p) => {
                const bl = p.legs?.buy_leg || {};
                const sl = p.legs?.short_leg || {};
                return (
                  <tr key={p.live_package_id} className="text-slate-200">
                    <td className="py-2 pr-3">
                      <div className="font-semibold">{p.book_id}</div>
                      <div className="text-[11px] text-slate-400">{p.structure}</div>
                    </td>
                    <td className="py-2 pr-3 font-mono text-amber-300">{p.state}</td>
                    <td className="py-2 pr-3">
                      {bl.trading_symbol || '—'} ({bl.qty || 0} @ ₹{bl.entry_price || 0})
                    </td>
                    <td className="py-2 pr-3">
                      {sl.trading_symbol ? `${sl.trading_symbol} (-${sl.qty || 0} @ ₹${sl.entry_price || 0})` : '—'}
                    </td>
                    <td className="py-2 text-right">
                      <button
                        type="button"
                        disabled={Boolean(busyId)}
                        onClick={() => handleExit(p.live_package_id)}
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded bg-rose-600 hover:bg-rose-500 text-white font-semibold"
                      >
                        <LogOut className="h-3 w-3" />
                        Exit Live
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {closedPkgs.length > 0 && (
        <div className="text-xs text-slate-400 pt-1">
          Latest live closed net: {fmtInr(closedPkgs[0].realized_net_inr)} ({closedPkgs[0].exit_reason})
        </div>
      )}
    </section>
  );
}
