import React from 'react';
import { fmtAgeMs, fmtInr, fmtSignedInr } from './v2Fixtures';

function LedgerTable({ title, badge, rows }) {
  return (
    <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <span className="text-xs font-bold uppercase tracking-wider text-slate-200 font-mono">{title}</span>
        <span className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-600 font-mono">
          {badge} ({rows.length})
        </span>
      </div>
      {rows.length === 0 ? (
        <div className="text-xs font-mono text-slate-400 py-3">No closed trades recorded in this stream yet.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                <th className="py-2 pr-3">Session</th>
                <th className="py-2 pr-3">Contract / Book</th>
                <th className="py-2 pr-3">Entry → Exit</th>
                <th className="py-2 pr-3">Exit Reason</th>
                <th className="py-2 pr-3 text-right">Planned SL</th>
                <th className="py-2 pr-3 text-right">Overshoot</th>
                <th className="py-2 pr-3 text-right">Gross PnL</th>
                <th className="py-2 pr-3 text-right">Charges</th>
                <th className="py-2 text-right">Net PnL</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.package_id} className="border-b border-slate-800/80 text-slate-200">
                  <td className="py-2 pr-3 whitespace-nowrap">{r.session_date}</td>
                  <td className="py-2 pr-3">
                    <div className="font-semibold text-white">{r.direction_label}</div>
                    <div className="text-[10px] text-slate-400">{r.book_id}</div>
                  </td>
                  <td className="py-2 pr-3 text-[11px] text-slate-300 whitespace-nowrap">
                    {r.entry_bar_ts} → {r.exit_bar_ts}
                  </td>
                  <td className="py-2 pr-3 text-[11px]">{r.exit_reason}</td>
                  <td className="py-2 pr-3 text-right text-slate-300">{fmtInr(r.planned_loss_inr)}</td>
                  <td className="py-2 pr-3 text-right text-amber-300">{fmtInr(r.slippage_and_gap_overshoot_inr)}</td>
                  <td className="py-2 pr-3 text-right">{fmtSignedInr(r.gross_pnl_inr)}</td>
                  <td className="py-2 pr-3 text-right text-slate-400">{fmtInr(r.total_friction_inr)}</td>
                  <td className={`py-2 text-right font-bold ${Number(r.net_pnl_inr) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {fmtSignedInr(r.net_pnl_inr)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function V2PositionsTab({ positions }) {
  if (!positions) return null;
  const openList = positions.forward_open_positions || [];
  const fwdClosed = positions.forward_closed_trades || [];
  const repClosed = positions.replay_closed_trades || [];

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-bold uppercase tracking-wider text-amber-300 font-mono">
            Open / Pending-Leg2 / Unresolved Forward Exposure
          </span>
          <span className="text-[10px] font-mono text-slate-400">
            Missing/stale quotes retain open risk cleanly (never zeroed or auto-flattened)
          </span>
        </div>
        {openList.length === 0 ? (
          <div className="text-xs font-mono text-slate-400 py-2">Zero open forward paper packages.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs font-mono">
              <thead>
                <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                  <th className="py-2 pr-3">Package</th>
                  <th className="py-2 pr-3">State</th>
                  <th className="py-2 pr-3">Reserved</th>
                  <th className="py-2 pr-3">Structural Max Risk</th>
                  <th className="py-2 pr-3">Unpriced Reason / Age</th>
                  <th className="py-2 text-right">Conservative MTM</th>
                </tr>
              </thead>
              <tbody>
                {openList.map((p) => (
                  <tr key={p.package_id} className="border-b border-slate-800/80 text-slate-200">
                    <td className="py-2 pr-3">
                      <div className="text-white font-semibold">{p.direction_label}</div>
                      <div className="text-[10px] text-slate-400">{p.package_id}</div>
                    </td>
                    <td className="py-2 pr-3">
                      <span
                        className={`px-1.5 py-0.5 rounded text-[10px] ${
                          p.is_unpriced_exposure
                            ? 'bg-rose-900/60 text-rose-200 border border-rose-500/40'
                            : 'bg-sky-900/50 text-sky-200 border border-sky-500/40'
                        }`}
                      >
                        {p.state}
                      </span>
                    </td>
                    <td className="py-2 pr-3">{fmtInr(p.reserved_capital_inr)}</td>
                    <td className="py-2 pr-3 text-rose-300">{fmtInr(p.structural_max_loss_inr)}</td>
                    <td className="py-2 pr-3 text-[11px] text-amber-300">
                      {p.is_unpriced_exposure ? `${p.unpriced_reason} (${fmtAgeMs(p.unpriced_age_ms)})` : 'FRESH_PRICED'}
                    </td>
                    <td className="py-2 text-right font-bold text-rose-400">
                      {fmtSignedInr(p.conservative_worst_loss_inr)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <LedgerTable title="Forward Paper Closed Trades" badge="FORWARD_PAPER" rows={fwdClosed} />
      <LedgerTable title="Archived L5 Replay Closed Trades (Canonical Rs 0.00 Reconciled)" badge="ARCHIVE_REPLAY" rows={repClosed} />
    </div>
  );
}
