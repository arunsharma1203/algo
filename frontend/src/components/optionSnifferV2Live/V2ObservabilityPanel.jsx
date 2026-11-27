import React from 'react';
import { Activity, Filter, Radio, Layers } from 'lucide-react';

const fmtMs = (v) => (v === null || v === undefined ? '—' : `${Number(v).toFixed(1)} ms`);
const fmtInr = (v) =>
  v === null || v === undefined ? '—' : `₹${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export default function V2ObservabilityPanel({ overview, diagnostics, opportunities }) {
  const fwd = overview?.forward_paper_summary || {};
  const ddSep = fwd?.drawdown_audit_correction || {};
  const feed = overview?.feed_hygiene?.feed_consumer || diagnostics?.feed_hygiene?.feed_consumer || {};
  const arch = diagnostics?.feed_architecture_comparison || {};
  const opps = opportunities?.rows || [];
  const statusSummary = opportunities?.summary_by_status || {};
  const openBasisFails = overview?.open_basis_fail_summary || {};
  const arb = overview?.regime_arbitration || null;

  return (
    <div className="space-y-4 mt-4">
      {arb && (
        <div className="rounded-2xl border border-indigo-500/40 bg-indigo-950/15 p-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-indigo-300">
              <Layers className="w-4 h-4" />
              Causal Multi-Book Regime Arbitrator (`v2_regime_arbitrator`)
            </div>
            <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-indigo-900/50 text-indigo-200 border border-indigo-500/30">
              {arb.regime_code} • Preferred: {arb.preferred_book || 'NONE'}
            </span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Nearest Trading DTE</div>
              <div className="font-semibold text-slate-100">{arb.nearest_trading_dte ?? '—'} ({arb.nearest_expiry || '—'})</div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Anti-Self-Hedge Gate</div>
              <div className="font-semibold text-emerald-300">
                Bull: {arb.allowed_bull ? 'OPEN' : 'LOCKED'} / Bear: {arb.allowed_bear ? 'OPEN' : 'LOCKED'}
              </div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Breakout / ADX+ER</div>
              <div className="font-semibold text-slate-100">
                {arb.is_orb_breakout ? 'ORB_BREAK' : 'RANGE'} / {arb.trend_strength_ok ? 'PASS' : 'LOW'}
              </div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Book A / Book B Active</div>
              <div className="font-semibold text-slate-100">
                {arb.book_a_enabled ? 'ON' : 'OFF (DTE<=4 Research)'} / {arb.book_b_enabled ? 'ON' : 'OFF'}
              </div>
            </div>
          </div>
          <div className="text-[11px] text-slate-400">
            {arb.reason} <span className="text-amber-300/90">• {arb.governance_note}</span>
          </div>
        </div>
      )}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="rounded-2xl border border-slate-700/80 bg-slate-900/75 p-4 space-y-2.5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-cyan-300">
              <Radio className="w-4 h-4" />
              4-Stage Feed Telemetry &amp; Queue Isolation
            </div>
            <span className="text-[11px] px-2 py-0.5 rounded bg-slate-800 text-slate-300">
              {arch?.active_mode || 'SHARED_PASSIVE_PRIORITY'}
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Source Age (p95)</div>
              <div className="font-semibold text-slate-100">{fmtMs(feed?.source_age_p95_ms)}</div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Queue Lag (p95)</div>
              <div className="font-semibold text-slate-100">{fmtMs(feed?.queue_lag_p95_ms)}</div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Proc Delay (p95)</div>
              <div className="font-semibold text-slate-100">{fmtMs(feed?.processing_lag_p95_ms)}</div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Spot-Fut Skew (p99)</div>
              <div className="font-semibold text-amber-300">{fmtMs(feed?.spot_fut_skew_p99_ms)}</div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Cross-Leg Skew (p99)</div>
              <div className="font-semibold text-amber-300">{fmtMs(feed?.cross_leg_skew_p99_ms)}</div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2">
              <div className="text-[10px] text-slate-400">Priority vs Normal Drops</div>
              <div className="font-semibold text-slate-100">
                {feed?.dropped_priority_overflow_events ?? 0} / {feed?.dropped_normal_overflow_events ?? 0}
              </div>
            </div>
          </div>

          {Object.keys(openBasisFails).length > 0 && (
            <div className="text-[11px] text-amber-300">
              Open-Basis Warmup Failure Counts: {JSON.stringify(openBasisFails)}
            </div>
          )}

          {arch?.upstox_limits && (
            <div className="text-[11px] text-slate-400">
              Upstox V3 Plan ({arch.account_tier_assumed || 'NORMAL'}): max{' '}
              {arch.upstox_limits.normal_max_concurrent_ws_connections} WS connections (Plus:{' '}
              {arch.upstox_limits.plus_max_concurrent_ws_connections}),{' '}
              {arch.upstox_limits.normal_full_combined} combined full instruments. Dedicated V2 WS remains{' '}
              <span className="text-slate-200 font-medium">opt-in disabled</span> to protect V1.
            </div>
          )}
        </div>

        <div className="rounded-2xl border border-slate-700/80 bg-slate-900/75 p-4 space-y-2.5">
          <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-amber-300">
            <Layers className="w-4 h-4" />
            Versioned Observed vs Stress Drawdown Audit
          </div>
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2.5">
              <div className="text-[10px] text-slate-400 uppercase">Observed-Mark Max DD</div>
              <div className="text-base font-bold text-emerald-300 mt-0.5">
                {fmtInr(ddSep?.observed_mark_max_dd_inr ?? fwd?.observed_mark_dd_inr)}
              </div>
              <div className="text-[10px] text-slate-400">Synchronized L5 executable marks</div>
            </div>
            <div className="rounded-lg bg-slate-950/70 border border-slate-800 p-2.5">
              <div className="text-[10px] text-slate-400 uppercase">Conservative Stress DD</div>
              <div className="text-base font-bold text-amber-300 mt-0.5">
                {fmtInr(ddSep?.stress_max_dd_inr ?? fwd?.stress_dd_inr)}
              </div>
              <div className="text-[10px] text-slate-400">
                Includes unpriced gap charges ({ddSep?.unpriced_stress_spikes_count ?? 0} spike)
              </div>
            </div>
          </div>
          {Array.isArray(ddSep?.unpriced_stress_spikes) && ddSep.unpriced_stress_spikes.length > 0 && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-950/20 px-2.5 py-2 text-[11px] text-amber-200">
              {ddSep.unpriced_stress_spikes.slice(0, 2).map((sp) => (
                <div key={`${sp.seq_no}-${sp.session_date}`}>
                  {sp.session_date} seq {sp.seq_no}: Stress mark {fmtInr(sp.conservative_mtm_inr)} between observed{' '}
                  {fmtInr(sp.prior_bar_mtm_inr)} and {fmtInr(sp.next_bar_mtm_inr)} ({sp.classification})
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="rounded-2xl border border-slate-700/80 bg-slate-900/75 p-4 space-y-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-slate-200">
            <Filter className="w-4 h-4 text-cyan-400" />
            Accepted &amp; Rejected Opportunity Gate Log (`osv2_opportunities`)
          </div>
          <div className="text-[11px] text-slate-400 flex items-center gap-3">
            <Activity className="w-3.5 h-3.5 text-slate-500" />
            <span>
              Total logged: {opportunities?.count ?? opps.length}
              {statusSummary?.ACCEPTED !== undefined ? ` • Accepted: ${statusSummary.ACCEPTED}` : ''}
              {statusSummary?.REJECTED !== undefined ? ` • Rejected: ${statusSummary.REJECTED}` : ''}
              {statusSummary?.BLOCKED !== undefined ? ` • Blocked: ${statusSummary.BLOCKED}` : ''}
            </span>
          </div>
        </div>

        {opps.length === 0 ? (
          <div className="text-xs text-slate-400 py-2">
            No deduplicated forward opportunities persisted yet in this DB session.
          </div>
        ) : (
          <div className="max-h-56 overflow-y-auto divide-y divide-slate-800/70 text-xs">
            {opps.slice(0, 25).map((op) => (
              <div key={op.opportunity_id} className="py-2 flex flex-wrap items-center justify-between gap-2">
                <div>
                  <span
                    className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-bold mr-2 ${
                      op.decision_status === 'ACCEPTED'
                        ? 'bg-emerald-500/20 text-emerald-300'
                        : 'bg-slate-800 text-amber-300'
                    }`}
                  >
                    {op.decision_status}
                  </span>
                  <span className="font-mono text-slate-200">{op.bar_ts}</span>{' '}
                  <span className="text-slate-400">({op.book_id})</span>
                </div>
                <div className="text-[11px] text-slate-300 font-mono">
                  {op.primary_reject_reason || (op.failed_gates || []).join(', ') || 'ACCEPTED'} •{' '}
                  {op.execution_availability}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
