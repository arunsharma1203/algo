import React from 'react';

const fmtInr = (val) => {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return '—';
  const n = Number(val);
  return `${n >= 0 ? '+' : '-'}₹${Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export default function V2PositionsTab({ positions, liveSubPanel = null }) {
  const openPkgs = positions?.forward_open_positions || [];
  const closedPkgs = positions?.forward_closed_trades || positions?.forward_closed_positions || [];

  return (
    <div className="space-y-5">
      {liveSubPanel}

      {/* Open Paper Positions */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold text-white">Open Forward Paper Packages</h3>
          <span className="text-xs text-slate-400">{openPkgs.length} active</span>
        </div>

        {openPkgs.length === 0 ? (
          <div className="text-xs text-slate-400 py-4 text-center">No open paper positions right now.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left">
              <thead className="text-[11px] text-slate-400 border-b border-slate-800">
                <tr>
                  <th className="py-2 pr-3">Entry Bar</th>
                  <th className="py-2 pr-3">Book</th>
                  <th className="py-2 pr-3">Direction</th>
                  <th className="py-2 pr-3">State</th>
                  <th className="py-2 pr-3">Expiry</th>
                  <th className="py-2 pr-3 text-right">Reserved &#8377;</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70">
                {openPkgs.map((p) => (
                  <tr key={p.package_id} className="text-slate-200">
                    <td className="py-2 pr-3 font-mono">{p.entry_bar_ts}</td>
                    <td className="py-2 pr-3 font-semibold">{p.book_id}</td>
                    <td className="py-2 pr-3">{p.direction_label}</td>
                    <td className="py-2 pr-3 text-sky-300 font-mono">{p.state}</td>
                    <td className="py-2 pr-3 font-mono">{p.expiry}</td>
                    <td className="py-2 pr-3 text-right font-mono">
                      &#8377;{Number(p.reserved_capital_inr || 0).toLocaleString('en-IN')}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Closed Paper Ledger */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold text-white">Closed Paper Trade Ledger (Reconciled Canonical Accounting)</h3>
          <span className="text-xs text-slate-400">{closedPkgs.length} trades</span>
        </div>

        {closedPkgs.length === 0 ? (
          <div className="text-xs text-slate-400 py-4 text-center">No closed paper trades recorded yet.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left">
              <thead className="text-[11px] text-slate-400 border-b border-slate-800">
                <tr>
                  <th className="py-2 pr-3">Session</th>
                  <th className="py-2 pr-3">Book</th>
                  <th className="py-2 pr-3">Direction</th>
                  <th className="py-2 pr-3">Entry &rarr; Exit</th>
                  <th className="py-2 pr-3">Exit Reason</th>
                  <th className="py-2 pr-3 text-right">Charges</th>
                  <th className="py-2 text-right">Net P&amp;L</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70">
                {closedPkgs.map((p) => {
                  const net = Number(p.net_pnl_inr || 0);
                  return (
                    <tr key={p.package_id} className="text-slate-300">
                      <td className="py-2 pr-3 font-mono">{p.session_date}</td>
                      <td className="py-2 pr-3 font-semibold">{p.book_id}</td>
                      <td className="py-2 pr-3">{p.direction_label}</td>
                      <td className="py-2 pr-3 font-mono text-[11px]">
                        {(p.entry_bar_ts || '').slice(11, 16)} &rarr; {(p.exit_bar_ts || '').slice(11, 16)}
                      </td>
                      <td className="py-2 pr-3 font-mono text-[11px]">{p.exit_reason}</td>
                      <td className="py-2 pr-3 text-right font-mono text-slate-400">
                        &#8377;{Number(p.total_friction_inr || 0).toFixed(2)}
                      </td>
                      <td className={`py-2 text-right font-bold font-mono ${net >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {fmtInr(net)}
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
  );
}
