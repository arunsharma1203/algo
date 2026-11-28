import React from 'react';
import { CheckCircle2, XCircle } from 'lucide-react';

export default function V2SignalsTab({ overview, opportunities }) {
  const vrp = overview?.vrp_telemetry || {};
  const aiml = overview?.aiml_passive_telemetry || {};
  const rows = (overview?.book_status_rows || []).filter(
    (r) => r.book_id === 'V2_BOOK_A_INTRADAY_CREDIT' || r.book_id === 'V2_BOOK_B_PAID_FLOW'
  );
  const oppRows = opportunities?.rows || [];

  return (
    <div className="space-y-5">
      {/* Live VRP & HAR-RV Telemetry Card */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <div className="flex items-center justify-between flex-wrap gap-2 mb-3">
          <div>
            <h3 className="text-sm font-bold text-white">HAR-RV &amp; Same-Week Black-76 VRP Telemetry</h3>
            <p className="text-xs text-slate-400">
              Book B only buys convexity breakouts when same-week ATM straddle IV &le; 1.15 &times; HAR-RV forecast.
            </p>
          </div>
          <span className="px-2.5 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-200 border border-slate-700">
            {vrp.status || 'STANDBY'}
          </span>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs">
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">ATM Strike &amp; Straddle</div>
            <div className="font-semibold text-white mt-1">
              {vrp.atm_strike ? `${vrp.atm_strike} (CE ${vrp.atm_ce_mid ?? '—'} + PE ${vrp.atm_pe_mid ?? '—'})` : '—'}
            </div>
          </div>
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">ATM Straddle IV</div>
            <div className="font-bold text-sky-300 mt-1">
              {vrp.atm_iv_pct !== null && vrp.atm_iv_pct !== undefined ? `${Number(vrp.atm_iv_pct).toFixed(2)}%` : '—'}
            </div>
          </div>
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">HAR-RV Forecast</div>
            <div className="font-bold text-white mt-1">
              {vrp.har_rv_pct !== null && vrp.har_rv_pct !== undefined ? `${Number(vrp.har_rv_pct).toFixed(2)}%` : '—'}
            </div>
          </div>
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">VRP Ratio (IV/HAR)</div>
            <div
              className={`font-bold mt-1 ${
                vrp.vrp_ratio == null
                  ? 'text-slate-400'
                  : Number(vrp.vrp_ratio) <= 1.15
                  ? 'text-emerald-400'
                  : 'text-amber-400'
              }`}
            >
              {vrp.vrp_ratio !== null && vrp.vrp_ratio !== undefined ? Number(vrp.vrp_ratio).toFixed(3) : '—'}
            </div>
          </div>
          <div className="bg-slate-950/70 border border-slate-800 rounded-lg p-3">
            <div className="text-slate-400 text-[11px]">1-Sigma Expected Move / AIML</div>
            <div className="font-semibold text-slate-200 mt-1">
              {vrp.har_1sigma_pts ? `±${vrp.har_1sigma_pts}pt` : '—'} &bull;{' '}
              {aiml.p_breach !== null && aiml.p_breach !== undefined ? `${(Number(aiml.p_breach) * 100).toFixed(1)}%` : '—'}
            </div>
          </div>
        </div>
      </div>

      {/* Live Book Checklists */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {rows.map((book) => (
          <div key={book.book_id} className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-sm font-bold text-white">{book.book_label}</h3>
              <span className="text-xs font-mono text-slate-300">{book.latest_decision || 'WAITING'}</span>
            </div>

            {Array.isArray(book.gates) && book.gates.length > 0 ? (
              <div className="divide-y divide-slate-800/80 text-xs">
                {book.gates.map((g, idx) => (
                  <div key={`${book.book_id}_${g.name}_${idx}`} className="py-2 flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      {g.passed ? (
                        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400 shrink-0" />
                      ) : (
                        <XCircle className="h-3.5 w-3.5 text-rose-400 shrink-0" />
                      )}
                      <span className="font-semibold text-slate-200">{g.name}</span>
                    </div>
                    <div className="text-right font-mono">
                      <span className={g.passed ? 'text-emerald-300' : 'text-rose-300'}>{g.current}</span>
                      <span className="text-slate-500 ml-2">({g.target})</span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-slate-400 py-3">
                Awaiting completed 1-minute bar evaluation ({book.eligibility_reason || 'Idle'}).
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Recent Opportunity Evaluations */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold text-white">Recent Signal Evaluations (Read-Only Audit Trail)</h3>
          <span className="text-xs text-slate-400">{oppRows.length} recent events</span>
        </div>

        {oppRows.length === 0 ? (
          <div className="text-xs text-slate-400 py-4 text-center">
            No completed bar evaluations recorded yet in forward paper ledger.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left">
              <thead className="text-[11px] text-slate-400 border-b border-slate-800">
                <tr>
                  <th className="py-2 pr-3">Bar Time</th>
                  <th className="py-2 pr-3">Book</th>
                  <th className="py-2 pr-3">Dir</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3">Reject / Note</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70">
                {oppRows.slice(0, 30).map((o) => (
                  <tr key={o.opportunity_id} className="text-slate-300">
                    <td className="py-2 pr-3 font-mono whitespace-nowrap">{o.bar_ts}</td>
                    <td className="py-2 pr-3 font-semibold">{o.book_id}</td>
                    <td className="py-2 pr-3">{o.direction || '—'}</td>
                    <td className="py-2 pr-3">
                      <span
                        className={`px-2 py-0.5 rounded text-[11px] font-semibold ${
                          o.decision_status === 'ACCEPTED'
                            ? 'bg-emerald-500/20 text-emerald-300'
                            : o.decision_status === 'BLOCKED'
                            ? 'bg-amber-500/20 text-amber-300'
                            : 'bg-slate-800 text-slate-400'
                        }`}
                      >
                        {o.decision_status}
                      </span>
                    </td>
                    <td className="py-2 pr-3 font-mono text-[11px] text-slate-400">
                      {o.primary_reject_reason || 'ALL_GATES_PASSED'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
