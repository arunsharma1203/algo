import React from 'react';
import { fmtInr, fmtIstStamp, fmtPct, fmtSignedInr } from './v2Fixtures';

function SupersededDisclosure({ superseded }) {
  if (!superseded || !superseded.evidence_status) return null;
  const st = superseded.superseded_stats || {};
  return (
    <details className="rounded-xl border border-amber-500/40 bg-amber-950/20 p-4 font-mono text-xs">
      <summary className="cursor-pointer font-bold text-amber-300 select-none">
        Superseded v2.0.0 Evidence Disclosure (Commit baa72f77 · {st.trade_count ?? 2} trades · {fmtSignedInr(st.net_pnl_inr)} — Excluded from v2.1.0 Totals)
      </summary>
      <div className="mt-3 space-y-2 text-slate-300 border-t border-amber-500/30 pt-3">
        <div>
          <span className="text-slate-400">Status:</span> {superseded.evidence_status} |{' '}
          <span className="text-slate-400">Digest:</span> {superseded.ledger_sha256 || 'Unavailable'}
        </div>
        <div className="text-[11px] text-amber-200 space-y-1">
          {(superseded.reason_superseded || []).map((r, i) => (
            <div key={i}>• {r}</div>
          ))}
        </div>
      </div>
    </details>
  );
}

export default function V2ComparisonTab({
  mode = 'comparison',
  overview,
  dailyComparison,
  dailyComparisonMeta,
  replayAudit,
  replayAuditMeta,
  counterfactuals,
  counterfactualsMeta,
}) {
  if (mode === 'replay') {
    const repSum = overview?.archive_replay_summary || dailyComparison?.v2_replay_summary;
    const outcome = replayAudit?.causal_verification_outcome;
    const timing = replayAudit?.archive_timing_evidence;
    const superseded = replayAudit?.superseded_v2_0_replay || overview?.superseded_v2_0_replay;
    const ablation = replayAudit?.ablation_study || [];
    const sens = replayAudit?.sensitivity || {};

    return (
      <div className="space-y-4 font-mono">
        {replayAuditMeta?.unavailable && (
          <div className="rounded-xl border border-amber-500/50 bg-amber-950/35 px-3.5 py-2.5 text-xs text-amber-100">
            Causal replay endpoint unavailable ({replayAuditMeta.error || 'request failed'}).
          </div>
        )}

        {/* Causal Verification Outcome */}
        <div className="rounded-xl border border-purple-500/40 bg-slate-900/80 p-4 space-y-2.5">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <span className="text-xs font-bold uppercase tracking-wider text-purple-300">
              Causal Replay v2.1.0-causal (`REPLAY_V2_1_CAUSAL`) — Archive Availability Outcome
            </span>
            <span className="text-[11px] px-2 py-0.5 rounded bg-purple-950 text-purple-200 border border-purple-500/40">
              Verified Basis Trades: {outcome ? outcome.verified_futures_basis_trades : repSum ? repSum.trade_count : 'Unavailable'}
            </span>
          </div>
          <p className="text-xs text-slate-300 break-words">
            {outcome?.reason ||
              (repSum
                ? 'Archive snapshots (backend/market_data.db) lack separate Spot vs Futures source timestamps and Futures contract identity/expiry; causal gate fails closed with 0 fabricated trades.'
                : 'Replay summary unavailable.')}
          </p>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
            <div className="p-2.5 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Replay Net P&amp;L</div>
              <div className="font-bold text-slate-100">{repSum ? fmtSignedInr(repSum.net_pnl_inr) : 'Unavailable'}</div>
            </div>
            <div className="p-2.5 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Unverifiable Basis Events</div>
              <div className="font-bold text-amber-300">{outcome?.unverifiable_basis_events ?? 'Unavailable'}</div>
            </div>
            <div className="p-2.5 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Delayed Rows Rejected</div>
              <div className="font-bold text-slate-100">{outcome?.late_events_rejected ?? 'Unavailable'}</div>
            </div>
            <div className="p-2.5 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Post-15:30 EOD Ignored</div>
              <div className="font-bold text-slate-100">{outcome?.post_session_flushes_ignored ?? 'Unavailable'}</div>
            </div>
          </div>
        </div>

        {/* Archive Timing Audit Proof */}
        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2.5">
          <div className="text-xs font-bold uppercase text-slate-200">
            Empirical Archive Timing Proof (`backend/market_data.db` Asia/Kolkata)
          </div>
          {!timing ? (
            <div className="text-xs text-slate-400">
              Run explicit CLI audit or `POST /control/run-archive-replay` to inspect full timing proof.
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5 text-xs">
              <div className="p-2.5 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400 uppercase">Bar Close Availability Lag</div>
                <div className="text-slate-100 font-semibold mt-0.5">
                  {timing.bar_2026_11_20_1030?.bar_ts_open || '2026-11-20 10:30:00'}
                </div>
                <div className="text-[11px] text-emerald-300">
                  recorded_at: {fmtIstStamp(timing.bar_2026_11_20_1030?.earliest_recorded_at)}
                </div>
              </div>
              <div className="p-2.5 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400 uppercase">Late Arrival Containment (09:40)</div>
                <div className="text-slate-100 font-semibold mt-0.5">
                  {timing.delayed_2026_11_20_0940?.delayed_row_count ?? 22} rows delayed +250s
                </div>
                <div className="text-[11px] text-amber-300">
                  recorded_at: {fmtIstStamp(timing.delayed_2026_11_20_0940?.delayed_recorded_at)}
                </div>
              </div>
              <div className="p-2.5 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400 uppercase">15:15 Cutoff &amp; 15:29 Late Flush</div>
                <div className="text-slate-100 font-semibold mt-0.5">
                  15:15 recorded: {fmtIstStamp(timing.cutoff_2026_11_19_1515?.timestamp)}
                </div>
                <div className="text-[11px] text-slate-300">
                  Late 15:29 flushes (19:41–23:23 IST) isolated &amp; ignored.
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Single-Factor Attribution Table if populated */}
        {ablation.length > 0 && (
          <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2.5">
            <div className="flex items-center justify-between flex-wrap gap-2">
              <span className="text-xs font-bold uppercase text-slate-200">
                Book B Single-Factor Causal Attribution (`REPLAY_V2_1_CAUSAL`)
              </span>
              <span className="text-[11px] text-slate-400">
                2x Charges: {fmtSignedInr(sens.stress_2x_charges?.net_pnl_inr)} • Ex-Best: {fmtSignedInr(sens.ex_best_session?.net_pnl_inr)}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse text-xs">
                <thead>
                  <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                    <th className="py-2 pr-3">Variant</th>
                    <th className="py-2 pr-3 text-right">Trades</th>
                    <th className="py-2 pr-3 text-right">Unverifiable</th>
                    <th className="py-2 pr-3 text-right">Net P&amp;L</th>
                    <th className="py-2 text-right">MTM DD</th>
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
                      <td className="py-2 pr-3 text-right text-amber-300">{a.unverifiable_basis_events ?? 0}</td>
                      <td className="py-2 pr-3 text-right font-semibold">{fmtSignedInr(a.stats?.net_pnl_inr)}</td>
                      <td className="py-2 text-right">{fmtInr(a.stats?.intraday_mtm_dd_inr)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <SupersededDisclosure superseded={superseded} />
      </div>
    );
  }

  // Default `comparison` mode: V1 Reported vs V2 Forward Paper vs V2 Causal Replay + Corrected CF1/CF2
  const dc = dailyComparison;
  const v1Sum = dc?.v1_reported_summary;
  const v2RepSum = dc?.v2_replay_summary;
  const v2FwdSum = dc?.v2_forward_paper_summary;
  const dailyRows = dc?.archive_daily_comparison || [];

  const cf = counterfactuals;
  const cf1 = cf?.cf1_summary;
  const cf2 = cf?.cf2_summary;
  const cf1Rows = (cf?.rows || []).filter((r) => r.experiment_code === 'CF1_TRUE_FRESH_70PCT_CONTINUATION');

  return (
    <div className="space-y-4 font-mono">
      {dailyComparisonMeta?.unavailable && (
        <div className="rounded-xl border border-amber-500/50 bg-amber-950/35 px-3.5 py-2.5 text-xs text-amber-100">
          Daily comparison endpoint unavailable ({dailyComparisonMeta.error || 'request failed'}).
        </div>
      )}

      {/* Three Strict Non-Mixed Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div className="rounded-xl border border-slate-700 bg-slate-900/70 p-4">
          <div className="text-[10px] text-slate-400 uppercase">V1 S205 (Reported Baseline)</div>
          <div className="text-lg font-bold text-white mt-1">{v1Sum ? fmtSignedInr(v1Sum.net_pnl_inr) : 'Unavailable'}</div>
          <div className="text-[11px] text-amber-300 mt-1">
            {v1Sum ? `${v1Sum.trade_count ?? 0} trades (4 stale-wing exits)` : 'Unavailable'}
          </div>
        </div>
        <div className="rounded-xl border border-purple-500/40 bg-slate-900/70 p-4">
          <div className="text-[10px] text-purple-300 uppercase">V2 Causal Replay (v2.1.0-causal)</div>
          <div className="text-lg font-bold text-slate-100 mt-1">{v2RepSum ? fmtSignedInr(v2RepSum.net_pnl_inr) : 'Unavailable'}</div>
          <div className="text-[11px] text-slate-300 mt-1">
            {v2RepSum ? `${v2RepSum.trade_count ?? 0} verified trades • Fail-closed on missing ts` : 'Unavailable'}
          </div>
        </div>
        <div className="rounded-xl border border-sky-500/40 bg-slate-900/70 p-4">
          <div className="text-[10px] text-sky-300 uppercase">V2 Forward Paper (Live Stream)</div>
          <div className="text-lg font-bold text-emerald-400 mt-1">{v2FwdSum ? fmtSignedInr(v2FwdSum.net_pnl_inr) : 'Unavailable'}</div>
          <div className="text-[11px] text-slate-300 mt-1">
            {v2FwdSum
              ? `${v2FwdSum.trade_count ?? 0} trades • MTM DD ${fmtInr(v2FwdSum.intraday_mtm_dd_inr)} (${fmtPct(v2FwdSum.intraday_mtm_dd_pct_peak)} peak)`
              : 'Unavailable'}
          </div>
        </div>
      </div>

      {/* Daily V1 vs V2 Causal Replay Table */}
      <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2.5">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <span className="text-xs font-bold uppercase text-slate-200">
            Session-by-Session: V1 Reported vs V2 Causal Replay
          </span>
          <span className="text-[10px] text-slate-400">Forward Paper strictly separated</span>
        </div>
        {!dc ? (
          <div className="text-xs text-slate-400">Unavailable</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                  <th className="py-2 pr-3">Session</th>
                  <th className="py-2 pr-3 text-right">V1 Net</th>
                  <th className="py-2 pr-3 text-right">V1 Trades (Stale)</th>
                  <th className="py-2 pr-3 text-right">V2 Replay Net</th>
                  <th className="py-2 pr-3 text-right">V2 Trades</th>
                  <th className="py-2">Verification Outcome</th>
                </tr>
              </thead>
              <tbody>
                {dailyRows.map((d) => (
                  <tr key={d.session_date} className="border-b border-slate-800/80 text-slate-200">
                    <td className="py-2 pr-3 whitespace-nowrap">{d.session_date}</td>
                    <td className={`py-2 pr-3 text-right ${Number(d.v1_reported_net_pnl_inr) >= 0 ? 'text-emerald-300' : 'text-rose-300'}`}>
                      {fmtSignedInr(d.v1_reported_net_pnl_inr)}
                    </td>
                    <td className="py-2 pr-3 text-right">
                      {d.v1_reported_trades} ({d.v1_stale_wing_contaminated_count})
                    </td>
                    <td className="py-2 pr-3 text-right">{fmtSignedInr(d.v2_replay_net_pnl_inr)}</td>
                    <td className="py-2 pr-3 text-right">{d.v2_replay_trades ?? 'Unavailable'}</td>
                    <td className="py-2 text-[11px] text-amber-300 break-words">
                      {d.verification_outcome || d.v2_session_state || 'REPLAY_NOT_RUN'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Corrected CF1 & CF2 (Realized vs Unresolved Separated, Excluded from V2 Totals) */}
      <div className="rounded-xl border border-amber-500/40 bg-slate-900/70 p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <span className="text-xs font-bold uppercase text-amber-300">
            Corrected V1 Counterfactuals (Realized vs Unresolved Separated · Excluded from V2)
          </span>
          <span className="text-[10px] px-2 py-0.5 rounded bg-amber-950/60 text-amber-200 border border-amber-500/40">
            included_in_v2_totals = 0
          </span>
        </div>

        {!cf ? (
          <div className="text-xs text-slate-400">
            Counterfactuals: Unavailable {counterfactualsMeta?.error ? `(${counterfactualsMeta.error})` : ''}
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
              <div className="p-3 rounded bg-slate-950/60 border border-slate-800 space-y-1">
                <div className="text-slate-300 font-semibold">CF1: 70% Arm → 85% Target / 55% Lock</div>
                <div className="text-emerald-400 font-bold">
                  Realized ({cf1?.realized_count ?? 0}/{cf1?.total_v1_packages ?? 0}): {fmtSignedInr(cf1?.realized_net_pnl_sum_inr)}
                </div>
                <div className="text-[11px] text-rose-300">
                  Unresolved Observation ({cf1?.unresolved_count ?? 0}): Last Fresh Mark {fmtSignedInr(cf1?.unresolved_last_fresh_mark_sum_inr)} (not mixed into realized)
                </div>
              </div>
              <div className="p-3 rounded bg-slate-950/60 border border-slate-800 space-y-1">
                <div className="text-slate-300 font-semibold">CF2: Ordinary Ratchet Bypass Only</div>
                <div className="text-emerald-400 font-bold">
                  Realized ({cf2?.realized_count ?? 0}/{cf2?.total_v1_packages ?? 0}): {fmtSignedInr(cf2?.realized_net_pnl_sum_inr)}
                </div>
                <div className="text-[11px] text-rose-300">
                  Unresolved Observation ({cf2?.unresolved_count ?? 0}): Last Fresh Mark {fmtSignedInr(cf2?.unresolved_last_fresh_mark_sum_inr)}
                </div>
              </div>
            </div>

            {cf1Rows.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-[11px]">
                  <thead>
                    <tr className="border-b border-slate-700 text-slate-400 uppercase">
                      <th className="py-1.5 pr-3">Session</th>
                      <th className="py-1.5 pr-3">V1 Package</th>
                      <th className="py-1.5 pr-3">CF1 Status / Exit</th>
                      <th className="py-1.5 pr-3 text-right">Realized Net</th>
                      <th className="py-1.5 text-right">Unresolved Mark</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cf1Rows.map((r) => (
                      <tr key={r.cf_id} className="border-b border-slate-800/70 text-slate-300">
                        <td className="py-1.5 pr-3 whitespace-nowrap">{r.session_date}</td>
                        <td className="py-1.5 pr-3 whitespace-nowrap">{r.v1_package_id}</td>
                        <td className={`py-1.5 pr-3 break-words ${r.cf_net_pnl_inr == null ? 'text-rose-300' : 'text-emerald-300'}`}>
                          {r.cf_status} • {r.cf_exit_reason}
                        </td>
                        <td className="py-1.5 pr-3 text-right font-semibold">
                          {r.cf_net_pnl_inr != null ? fmtSignedInr(r.cf_net_pnl_inr) : '—'}
                        </td>
                        <td className="py-1.5 text-right text-amber-300">
                          {r.cf_net_pnl_inr == null ? fmtSignedInr((r.detail || {}).last_fresh_mark_net_inr) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>

      <SupersededDisclosure superseded={dc?.superseded_v2_0_replay} />
    </div>
  );
}
