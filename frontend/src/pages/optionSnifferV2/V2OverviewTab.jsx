import React from 'react';
import { AlertTriangle, CheckCircle2, ShieldAlert, XCircle } from 'lucide-react';
import { V2ExpandableTradeTable, V2OpenExposurePanel } from './V2PositionsTab';
import { fmtAgeMs, fmtInr, fmtPct, fmtSignedInr } from './v2Fixtures';

export default function V2OverviewTab({ overview, overviewMeta, positions, positionsMeta }) {
  const alerts = [];
  if (overviewMeta?.unavailable) {
    alerts.push({
      severity: 'critical',
      code: 'OVERVIEW_ENDPOINT_UNAVAILABLE',
      message: `Live overview unavailable (${overviewMeta.error || 'request failed'}). No placeholder metrics are substituted.`,
    });
  } else if (overviewMeta?.stale) {
    alerts.push({
      severity: 'warning',
      code: 'OVERVIEW_CACHED_STALE',
      message: `Showing cached overview from ${overviewMeta.asOfIst || 'earlier poll'} (${overviewMeta.error || 'refresh failed'}).`,
    });
  }
  if (positionsMeta?.unavailable) {
    alerts.push({
      severity: 'warning',
      code: 'POSITIONS_UNAVAILABLE',
      message: `Positions ledger unavailable (${positionsMeta.error || 'request failed'}).`,
    });
  }
  if (overview?.active_alerts?.length) {
    overview.active_alerts.forEach((a) => alerts.push(a));
  }

  const fwd = overview?.forward_paper_summary;
  const promo = overview?.promotion_gates;
  const hyg = overview?.feed_hygiene;
  const fc = hyg?.feed_consumer || {};
  const sub = hyg?.subscription_registry || {};
  const bookRows = overview?.book_status_rows || [];

  return (
    <div className="space-y-4">
      {/* Alert Banner Above Metrics */}
      {alerts.length > 0 && (
        <div className="space-y-2" role="region" aria-label="Active Operational Alerts">
          {alerts.map((al, idx) => {
            const isCrit = al.severity === 'critical';
            return (
              <div
                key={`${al.code}_${idx}`}
                className={`rounded-xl border px-3.5 py-2.5 flex items-start gap-2.5 text-xs font-mono ${
                  isCrit
                    ? 'border-rose-500/60 bg-rose-950/50 text-rose-100'
                    : 'border-amber-500/50 bg-amber-950/40 text-amber-100'
                }`}
              >
                {isCrit ? (
                  <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0 mt-0.5" />
                ) : (
                  <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
                )}
                <div className="min-w-0 break-words">
                  <span className="font-bold mr-2">{al.code}:</span>
                  <span>{al.message}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 4 Single-Appearance Forward Paper KPI Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 font-mono">
        <div className="rounded-xl border border-slate-700/80 bg-slate-900/80 p-3.5">
          <div className="text-[11px] text-slate-400 uppercase">Forward Net P&amp;L</div>
          <div
            className={`text-lg font-bold mt-1 ${
              fwd ? (Number(fwd.net_pnl_inr) >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-slate-300'
            }`}
          >
            {fwd ? fmtSignedInr(fwd.net_pnl_inr) : 'Unavailable'}
          </div>
          <div className="text-[11px] text-slate-400 mt-1">
            {fwd ? `${fwd.trade_count ?? 0} closed • Equity ${fmtInr(fwd.realized_equity_inr)}` : 'Overview unavailable'}
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/80 bg-slate-900/80 p-3.5">
          <div className="text-[11px] text-slate-400 uppercase">Observed vs Stress Drawdown</div>
          <div className="text-lg font-bold text-amber-300 mt-1">
            {fwd
              ? fmtInr(
                  fwd.observed_mark_dd_inr !== undefined
                    ? fwd.observed_mark_dd_inr
                    : fwd.intraday_mtm_dd_inr
                )
              : 'Unavailable'}
          </div>
          <div className="text-[11px] text-slate-400 mt-1">
            {fwd ? (
              <>
                {fwd.drawdown_audit_correction?.observed_mark_max_dd_pct_start !== undefined
                  ? `Obs ${fmtPct(fwd.drawdown_audit_correction.observed_mark_max_dd_pct_start)} start • ${fmtPct(fwd.drawdown_audit_correction.observed_mark_max_dd_pct_peak)} peak`
                  : `${fmtPct(fwd.intraday_mtm_dd_pct)} start • ${fmtPct(fwd.intraday_mtm_dd_pct_peak)} peak`}
                {(fwd.stress_dd_inr !== undefined || fwd.drawdown_audit_correction?.stress_max_dd_inr !== undefined) && (
                  <div className="text-[10px] text-amber-400/90 mt-0.5">
                    Stress Est: {fmtInr(fwd.stress_dd_inr ?? fwd.drawdown_audit_correction?.stress_max_dd_inr)}
                    {fwd.has_unpriced_stress_inflation ? ' (incl. unpriced gap stress)' : ''}
                  </div>
                )}
              </>
            ) : (
              'Chronological DD unavailable'
            )}
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/80 bg-slate-900/80 p-3.5">
          <div className="text-[11px] text-slate-400 uppercase">Capital Reserved / Peak</div>
          <div className="text-lg font-bold text-slate-100 mt-1">
            {fwd ? fmtInr(fwd.reserved_capital_inr) : 'Unavailable'}
          </div>
          <div className="text-[11px] text-slate-400 mt-1">
            {fwd ? `Peak margin used: ${fmtInr(fwd.peak_capital_used_inr)}` : 'Overview unavailable'}
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/80 bg-slate-900/80 p-3.5">
          <div className="text-[11px] text-slate-400 uppercase">Open &amp; Unresolved Risk</div>
          <div
            className={`text-lg font-bold mt-1 ${
              fwd && Number(fwd.unresolved_exposure_count || 0) > 0 ? 'text-rose-400' : 'text-slate-100'
            }`}
          >
            {fwd ? `${fwd.open_count ?? 0} open / ${fwd.unresolved_exposure_count ?? 0} unresolved` : 'Unavailable'}
          </div>
          <div className="text-[11px] text-slate-400 mt-1">
            {fwd ? `Structural risk: ${fmtInr(fwd.unresolved_structural_risk_charged_inr)}` : 'Overview unavailable'}
          </div>
        </div>
      </div>

      {/* Compact Book A vs Book B Status Table */}
      <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2.5">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <span className="text-xs font-bold uppercase tracking-wider text-slate-200 font-mono">
            Book Status &amp; Latest Signal Decisions
          </span>
          <span className="text-[11px] font-mono text-slate-400">
            Paper-only simulator (`LOCAL_PAPER_SIMULATOR_ONLY`)
          </span>
        </div>

        {!overview ? (
          <div className="text-xs font-mono text-slate-400 py-2">Unavailable</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs font-mono">
              <thead>
                <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                  <th className="py-2 pr-3">Book</th>
                  <th className="py-2 pr-3">Eligibility</th>
                  <th className="py-2 pr-3">Position</th>
                  <th className="py-2 pr-3">Latest Decision / Rejection Reason</th>
                  <th className="py-2 text-right">Last Fill</th>
                </tr>
              </thead>
              <tbody>
                {bookRows.map((b) => (
                  <tr key={b.book_id} className="border-b border-slate-800/80 align-top text-slate-200">
                    <td className="py-2.5 pr-3 font-semibold text-white min-w-[200px]">
                      <div>{b.book_label}</div>
                      {b.config_summary && (
                        <div className="text-[11px] font-normal text-slate-400 mt-0.5">{b.config_summary}</div>
                      )}
                    </td>
                    <td className="py-2.5 pr-3 whitespace-nowrap">
                      <span
                        className={`px-2 py-0.5 rounded text-[10px] border ${
                          !b.enabled || String(b.eligibility).includes('HALTED') || String(b.eligibility).includes('BLOCKED')
                            ? 'bg-rose-950/70 text-rose-200 border-rose-500/40'
                            : 'bg-emerald-950/60 text-emerald-200 border-emerald-500/40'
                        }`}
                      >
                        {b.eligibility}
                      </span>
                    </td>
                    <td className="py-2.5 pr-3 text-slate-300 whitespace-nowrap">{b.current_position || 'NONE'}</td>
                    <td className="py-2.5 pr-3 text-slate-300 max-w-md whitespace-normal break-words">
                      <span className="font-semibold text-slate-100">{b.latest_decision}</span>
                      {b.latest_rejection_reason ? ` — ${b.latest_rejection_reason}` : ''}
                    </td>
                    <td className="py-2.5 text-right whitespace-nowrap text-slate-300">
                      {b.last_fill
                        ? `${b.last_fill.side} @ ${b.last_fill.fill_price}`
                        : 'None'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Open Exposure & Expandable Forward Paper Closed Trades */}
      {positions ? (
        <>
          <V2OpenExposurePanel openPositions={positions.forward_open_positions} />
          <V2ExpandableTradeTable
            title="Forward Paper Closed Trades (Expandable Fills & Gates)"
            badge="FORWARD_PAPER"
            rows={positions.forward_closed_trades || []}
            emptyMessage="No closed Forward Paper trades in current session/ledger."
          />
        </>
      ) : (
        <div className="rounded-xl border border-slate-700 bg-slate-900/70 p-4 text-xs font-mono text-slate-400">
          Forward trade ledger: Unavailable {positionsMeta?.error ? `(${positionsMeta.error})` : ''}
        </div>
      )}

      {/* Promotion Gates & Feed Hygiene */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2 font-mono text-xs">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <span className="font-bold uppercase text-slate-200">60-Session Promotion Gates</span>
            <span className="text-[11px] px-2 py-0.5 rounded bg-amber-950/60 text-amber-300 border border-amber-500/40">
              {promo ? `${promo.sessions_progress} • ${promo.status}` : 'Unavailable'}
            </span>
          </div>
          {!promo ? (
            <div className="text-slate-400">Unavailable</div>
          ) : (
            <div className="space-y-1.5">
              {(promo.checks || []).map((c, idx) => (
                <div key={idx} className="flex items-center justify-between gap-2 bg-slate-950/50 px-2.5 py-1.5 rounded border border-slate-800">
                  <div className="flex items-center gap-2 min-w-0">
                    {c.passed ? (
                      <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                    ) : (
                      <XCircle className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                    )}
                    <span className="text-slate-200 break-words">{c.gate}</span>
                  </div>
                  <span className={`shrink-0 ${c.passed ? 'text-emerald-400' : 'text-amber-300'}`}>
                    {String(c.observed)} / {String(c.required)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2 font-mono text-xs">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <span className="font-bold uppercase text-slate-200">Feed Hygiene &amp; Pin Ownership</span>
            <span className="text-[11px] text-slate-400">
              V2 yields first under WS pressure
            </span>
          </div>
          {!hyg ? (
            <div className="text-slate-400">Unavailable</div>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400">Spot / Fut Age</div>
                <div className="text-slate-100 font-semibold">
                  {fmtAgeMs(hyg.spot_age_ms)} / {fmtAgeMs(hyg.futures_age_ms)}
                </div>
              </div>
              <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400">Spot-Fut Skew</div>
                <div className="text-slate-100 font-semibold">
                  {hyg.spot_fut_sync?.skew_ms != null ? `${hyg.spot_fut_sync.skew_ms} ms` : 'Unavailable'}
                </div>
              </div>
              <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400">Open Basis Ref</div>
                <div
                  className={`font-semibold ${
                    hyg.open_basis_reference?.ok
                      ? 'text-emerald-300'
                      : hyg.open_basis_reference && hyg.open_basis_reference.ok === false
                        ? 'text-rose-300'
                        : 'text-amber-300'
                  }`}
                >
                  {hyg.open_basis_reference?.ok
                    ? `Median ${Number(hyg.open_basis_reference.basis_pts) >= 0 ? '+' : ''}${Number(hyg.open_basis_reference.basis_pts).toFixed(1)}pt (${hyg.open_basis_samples_count || 1}m)`
                    : hyg.open_basis_reference && hyg.open_basis_reference.ok === false
                      ? `BLOCKED: ${hyg.open_basis_reference.reason || 'INVALID_OPEN_BASIS'}`
                      : 'Awaiting 09:15–09:29'}
                </div>
              </div>
              <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400">ORB30 / 1m Bars</div>
                <div className={`font-semibold ${hyg.orb30_ready ? 'text-emerald-300' : 'text-amber-300'}`}>
                  {hyg.orb30_ready
                    ? `READY (${hyg.completed_bars_today ?? 0}m)`
                    : `Warmup (${hyg.completed_bars_today ?? 0}m)`}
                </div>
              </div>
              <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400">Lag p50 / p99</div>
                <div className="text-slate-100 font-semibold">
                  {fc.queue_lag_p50_ms ?? 0} / {fc.queue_lag_p99_ms ?? 0} ms
                </div>
              </div>
              <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400">Late / Gaps / Drops</div>
                <div className="text-slate-100 font-semibold">
                  {hyg.late_event_count ?? 0} / {fc.sequence_bar_gaps ?? 0} / {fc.dropped_overflow_events ?? 0}
                </div>
              </div>
              <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400">Foreign Rejected</div>
                <div className="text-slate-100 font-semibold">{hyg.foreign_instrument_rejected_count ?? 0}</div>
              </div>
              <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
                <div className="text-[10px] text-slate-400">V2 Pins / Yields</div>
                <div className="text-slate-100 font-semibold">
                  {(sub.v2_owned_only_keys || []).length} / {sub.pressure_yield_count ?? 0}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
