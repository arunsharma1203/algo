import React from 'react';
import { fmtInr, fmtSignedInr } from './v2Fixtures';

export default function V2ComparisonTab({ dailyComparison, counterfactuals, replayAudit }) {
  const dc = dailyComparison || {};
  const v1Sum = dc.v1_reported_summary || {};
  const v2RepSum = dc.v2_replay_summary || {};
  const v2FwdSum = dc.v2_forward_paper_summary || {};
  const dailyRows = dc.archive_daily_comparison || [];

  const cf = counterfactuals || {};
  const cf1 = cf.cf1_summary || {};
  const cf2 = cf.cf2_summary || {};
  const cfRows = (cf.rows || []).filter((r) => r.experiment_code === 'CF1_TRUE_FRESH_70PCT_CONTINUATION');

  const ablation = (replayAudit && replayAudit.ablation_study) || [];
  const sens = (replayAudit && replayAudit.sensitivity) || {};

  return (
    <div className="space-y-4">
      {/* Three-column headline comparison */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="rounded-xl border border-slate-700 bg-slate-900/70 p-4 font-mono">
          <div className="text-[10px] text-slate-400 uppercase">V1 Advance S205 (Reported)</div>
          <div className="text-lg font-bold text-white mt-1">{fmtSignedInr(v1Sum.net_pnl_inr)}</div>
          <div className="text-[11px] text-amber-300 mt-1">
            {v1Sum.trade_count ?? 0} trades (4 stale-wing exits) • Peak Cap {fmtInr(v1Sum.peak_capital_used_inr)}
          </div>
        </div>
        <div className="rounded-xl border border-purple-500/40 bg-slate-900/70 p-4 font-mono">
          <div className="text-[10px] text-purple-300 uppercase">V2 Option Sniffer (Archive L5 Replay)</div>
          <div className="text-lg font-bold text-emerald-400 mt-1">{fmtSignedInr(v2RepSum.net_pnl_inr)}</div>
          <div className="text-[11px] text-slate-300 mt-1">
            {v2RepSum.trade_count ?? 0} trades • Charges {fmtInr(v2RepSum.total_charges_inr)} • Peak Cap {fmtInr(v2RepSum.peak_capital_used_inr)}
          </div>
        </div>
        <div className="rounded-xl border border-sky-500/40 bg-slate-900/70 p-4 font-mono">
          <div className="text-[10px] text-sky-300 uppercase">V2 Option Sniffer (Forward Paper)</div>
          <div className="text-lg font-bold text-white mt-1">{fmtSignedInr(v2FwdSum.net_pnl_inr)}</div>
          <div className="text-[11px] text-slate-300 mt-1">
            {v2FwdSum.trade_count ?? 0} closed trades • Con. MTM DD {fmtInr(v2FwdSum.intraday_mtm_dd_inr)}
          </div>
        </div>
      </div>

      {/* Daily V1 vs V2 Replay Table */}
      <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <span className="text-xs font-bold uppercase tracking-wider text-slate-200 font-mono">
            Daily V1 Reported vs V2 Replay (2026-11-16..23)
          </span>
          <span className="text-[10px] font-mono text-slate-400">Separate from Forward Paper</span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                <th className="py-2 pr-3">Session Date</th>
                <th className="py-2 pr-3 text-right">V1 Net</th>
                <th className="py-2 pr-3 text-right">V1 Trades (Stale Wing)</th>
                <th className="py-2 pr-3 text-right">V1 Reserved</th>
                <th className="py-2 pr-3 text-right">V2 Replay Net</th>
                <th className="py-2 pr-3 text-right">V2 Trades</th>
                <th className="py-2 text-right">V2 Peak Capital</th>
              </tr>
            </thead>
            <tbody>
              {dailyRows.map((d) => (
                <tr key={d.session_date} className="border-b border-slate-800/80 text-slate-200">
                  <td className="py-2 pr-3">{d.session_date}</td>
                  <td className={`py-2 pr-3 text-right ${Number(d.v1_reported_net_pnl_inr) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                    {fmtSignedInr(d.v1_reported_net_pnl_inr)}
                  </td>
                  <td className="py-2 pr-3 text-right">
                    {d.v1_reported_trades} ({d.v1_stale_wing_contaminated_count})
                  </td>
                  <td className="py-2 pr-3 text-right text-slate-400">{fmtInr(d.v1_capital_reserved_inr)}</td>
                  <td className={`py-2 pr-3 text-right font-bold ${Number(d.v2_replay_net_pnl_inr) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {fmtSignedInr(d.v2_replay_net_pnl_inr)}
                  </td>
                  <td className="py-2 pr-3 text-right">{d.v2_replay_trades ?? 'UNAVAILABLE'}</td>
                  <td className="py-2 text-right text-slate-300">{fmtInr(d.v2_replay_peak_capital_inr)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Book B Single-Factor Attribution & Sensitivity */}
      {ablation.length > 0 && (
        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <span className="text-xs font-bold uppercase tracking-wider text-slate-200 font-mono">
              Book B Single-Factor Attribution &amp; Stress Sensitivity
            </span>
            <span className="text-[10px] font-mono text-amber-300">
              2x Charges Net: {fmtSignedInr(sens.stress_2x_charges?.net_pnl_inr)} | Ex-Best Day ({sens.ex_best_session?.excluded_session}):{' '}
              {fmtSignedInr(sens.ex_best_session?.net_pnl_inr)}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs font-mono">
              <thead>
                <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                  <th className="py-2 pr-3">Variant</th>
                  <th className="py-2 pr-3 text-right">Trades</th>
                  <th className="py-2 pr-3 text-right">Net PnL</th>
                  <th className="py-2 pr-3 text-right">Delta vs B0</th>
                  <th className="py-2 pr-3 text-right">Profit Factor</th>
                  <th className="py-2 text-right">Conservative MTM DD</th>
                </tr>
              </thead>
              <tbody>
                {ablation.map((a) => (
                  <tr key={a.variant_code} className="border-b border-slate-800/80 text-slate-200">
                    <td className="py-2 pr-3">
                      <div className="font-semibold text-white">{a.variant_code}</div>
                      <div className="text-[10px] text-slate-400">{a.label}</div>
                    </td>
                    <td className="py-2 pr-3 text-right">{a.stats?.trade_count ?? 0}</td>
                    <td className="py-2 pr-3 text-right text-emerald-400 font-bold">{fmtSignedInr(a.stats?.net_pnl_inr)}</td>
                    <td className={`py-2 pr-3 text-right ${Number(a.incremental_net_vs_b0_inr) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                      {fmtSignedInr(a.incremental_net_vs_b0_inr)}
                    </td>
                    <td className="py-2 pr-3 text-right">{a.stats?.profit_factor ?? 0}</td>
                    <td className="py-2 text-right text-amber-300">{fmtInr(a.stats?.intraday_mtm_dd_inr)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* V1 Counterfactuals Excluded from V2 Totals */}
      <div className="rounded-xl border border-amber-500/40 bg-slate-900/70 p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <span className="text-xs font-bold uppercase tracking-wider text-amber-300 font-mono">
            V1 Counterfactual Study (STRICTLY EXCLUDED FROM V2 TOTALS)
          </span>
          <span className="text-[10px] px-2 py-0.5 rounded bg-amber-950/60 text-amber-200 border border-amber-500/40 font-mono">
            included_in_v2_totals = 0
          </span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs font-mono">
          <div className="p-3 rounded bg-slate-950/60 border border-slate-800">
            <div className="text-slate-400">CF1: True Fresh 70% Continuation</div>
            <div className="text-white font-bold mt-1">
              Last Fresh Sum: {fmtSignedInr(cf1.mark_at_last_fresh_sum_inr)} vs V1 Reported {fmtSignedInr(v1Sum.net_pnl_inr)}
            </div>
            <div className="text-[11px] text-rose-300 mt-1">
              Clean Resolved: {cf1.clean_or_resolved_count ?? 0} | UNRESOLVED_AT_1515_STALE_WING: {cf1.unresolved_stale_wing_count ?? 0}
            </div>
          </div>
          <div className="p-3 rounded bg-slate-950/60 border border-slate-800">
            <div className="text-slate-400">CF2: Book B Ratchet Bypass Experiment</div>
            <div className="text-white font-bold mt-1">Last Fresh Sum: {fmtSignedInr(cf2.mark_at_last_fresh_sum_inr)}</div>
            <div className="text-[11px] text-slate-400 mt-1">
              Archived wing dead-quotes marked honestly rather than fabricating 15:15 fills.
            </div>
          </div>
        </div>
        {cfRows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-[11px] font-mono">
              <thead>
                <tr className="border-b border-slate-700 text-slate-400 uppercase">
                  <th className="py-1.5 pr-3">Session</th>
                  <th className="py-1.5 pr-3">V1 Package / Book</th>
                  <th className="py-1.5 pr-3">CF1 Status</th>
                  <th className="py-1.5 pr-3 text-right">V1 Reported</th>
                  <th className="py-1.5 text-right">CF1 Net</th>
                </tr>
              </thead>
              <tbody>
                {cfRows.map((r) => (
                  <tr key={r.cf_id} className="border-b border-slate-800/70 text-slate-300">
                    <td className="py-1.5 pr-3">{r.session_date}</td>
                    <td className="py-1.5 pr-3">
                      {r.v1_package_id} ({r.book_id})
                    </td>
                    <td className={`py-1.5 pr-3 ${r.cf_status === 'UNRESOLVED_AT_1515_STALE_WING' ? 'text-rose-300' : 'text-emerald-300'}`}>
                      {r.cf_status}
                    </td>
                    <td className="py-1.5 pr-3 text-right">{fmtSignedInr(r.v1_reported_net_inr)}</td>
                    <td className="py-1.5 text-right font-semibold">{fmtSignedInr(r.cf_net_pnl_inr)}</td>
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
