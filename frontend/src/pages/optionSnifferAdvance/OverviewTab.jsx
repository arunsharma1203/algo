import React from 'react';
import {
  AlertTriangle,
  ArrowRight,
  Clock,
  GitCompare,
  ShieldAlert,
  Wallet,
} from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  classifyStrategyEra,
  deriveCandidateBlocker,
  formatClockIST,
  formatINRValue,
  formatNumber,
  formatPercent,
  formatSignedINR,
  formatTimestampLabel,
  getRowCohort,
  hasValidNumber,
} from './formatters';

export default function OverviewTab({
  executionStatus,
  liveStatus,
  tradesData,
  activeReadiness,
  shadowTelemetry,
  cohortComparison = null,
  sliceMeta,
  onSelectTab,
}) {
  const brokerPositions = Array.isArray(liveStatus?.active_live_positions)
    ? liveStatus.active_live_positions
    : [];
  const brokerIntents = Array.isArray(liveStatus?.recent_order_intents)
    ? liveStatus.recent_order_intents
    : [];
  const problemIntents = brokerIntents.filter(
    (i) =>
      i &&
      (i.state === 'REJECTED' ||
        i.exit_state === 'EXIT_RETRY_PENDING' ||
        String(i.exit_state || '').includes('UNRESOLVED'))
  );

  const allOpenPaper = Array.isArray(tradesData?.open_trades) ? tradesData.open_trades : [];
  const openForwardPackages = allOpenPaper.filter(
    (t) => getRowCohort(t) === 'S205_SHADOW_FORWARD_V1'
  );
  const openV6bComparator = allOpenPaper.filter(
    (t) => getRowCohort(t) === 'V6B_ML_LEGACY_COMPARATOR'
  );

  const openForwardUnrealized =
    sliceMeta?.trades?.status === 'ok'
      ? openForwardPackages.reduce(
          (acc, t) => acc + (hasValidNumber(t.unrealized_net_pnl) ? Number(t.unrealized_net_pnl) : 0),
          0
        )
      : null;

  const openV6bUnrealized =
    sliceMeta?.trades?.status === 'ok'
      ? openV6bComparator.reduce(
          (acc, t) => acc + (hasValidNumber(t.unrealized_net_pnl) ? Number(t.unrealized_net_pnl) : 0),
          0
        )
      : null;

  const shadowLedger = shadowTelemetry?.shadow_account_ledger || null;
  const pendingLeg2 = shadowLedger?.pending_leg2_count ?? null;
  const unwoundCount = shadowLedger?.incomplete_leg_unwinds_count ?? null;

  const shCohort = cohortComparison?.S205_SHADOW_FORWARD_V1 || null;
  const v6bCohort = cohortComparison?.V6B_ML_LEGACY_COMPARATOR || null;
  const histCohort = cohortComparison?.HISTORICAL_LEGACY_V2 || null;

  const engineRows = Array.isArray(activeReadiness?.engine_readiness)
    ? activeReadiness.engine_readiness
    : [];
  const shadowCandidateRows = engineRows.filter(
    (r) => classifyStrategyEra(r.engine_id, r) === 'SHADOW_FORWARD_V1'
  );
  const v6bCandidateRows = engineRows.filter(
    (r) => classifyStrategyEra(r.engine_id, r) === 'V6B_ML_LEGACY_COMPARATOR'
  );

  const availMargin = liveStatus?.available_margin ?? liveStatus?.readiness?.available_margin;
  const reqMargin = liveStatus?.required_margin_for_mode;
  const providerMarginTs = liveStatus?.readiness?.timestamp;
  const marginProbe = shadowTelemetry?.latest_margin_probe || null;

  const shadowRealizedNet = hasValidNumber(shCohort?.realized_net_pnl_inr)
    ? shCohort.realized_net_pnl_inr
    : shadowLedger?.realized_net_pnl_inr;
  const shadowCharges = hasValidNumber(shCohort?.total_charges_inr)
    ? shCohort.total_charges_inr
    : shadowLedger?.total_canonical_charges_inr;

  const shadowMirror = liveStatus?.shadow_mirror || null;
  const regimeBanners = shadowMirror?.regime_banners || null;
  const vixBannerVal = regimeBanners?.india_vix;
  const vixBannerHigh = regimeBanners?.vix_high_warning === true;
  const dteBannerVal = regimeBanners?.front_dte;
  const expiryDayWarn = regimeBanners?.is_expiry_day === true;
  const mirrorRecent = Array.isArray(shadowMirror?.recent_packages)
    ? shadowMirror.recent_packages.slice(0, 6)
    : [];

  return (
    <div className="space-y-5">
      {/* 0. Regime Watch Banners (VIX > 18 & Expiry Day) + Isolated Shadow Mirror Status */}
      <section className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div
          className={`rounded-xl border p-3.5 flex items-center justify-between shadow-2xs ${
            !hasValidNumber(vixBannerVal)
              ? 'bg-white border-slate-200 text-slate-800'
              : vixBannerHigh
              ? 'bg-rose-50 border-rose-300 text-rose-950'
              : 'bg-emerald-50/70 border-emerald-300 text-emerald-950'
          }`}
        >
          <div>
            <div className="text-[11px] font-bold uppercase tracking-wide">
              India VIX Watch (&gt; 18 Gate)
            </div>
            <div className="text-lg font-bold font-mono mt-0.5 tabular-nums">
              {hasValidNumber(vixBannerVal) ? formatNumber(vixBannerVal) : UNAVAILABLE_LABEL}
            </div>
          </div>
          <span
            className={`px-2.5 py-1 rounded-lg text-[11px] font-mono font-bold ${
              !hasValidNumber(vixBannerVal)
                ? 'bg-slate-200 text-slate-700'
                : vixBannerHigh
                ? 'bg-rose-600 text-white'
                : 'bg-emerald-600 text-white'
            }`}
          >
            {!hasValidNumber(vixBannerVal) ? 'UNAVAILABLE' : vixBannerHigh ? 'VIX > 18' : 'VIX <= 18'}
          </span>
        </div>

        <div
          className={`rounded-xl border p-3.5 flex items-center justify-between shadow-2xs ${
            !hasValidNumber(dteBannerVal)
              ? 'bg-white border-slate-200 text-slate-800'
              : expiryDayWarn
              ? 'bg-rose-50 border-rose-300 text-rose-950'
              : 'bg-emerald-50/70 border-emerald-300 text-emerald-950'
          }`}
        >
          <div>
            <div className="text-[11px] font-bold uppercase tracking-wide">
              Expiry Watch ({regimeBanners?.front_expiry || UNAVAILABLE_LABEL})
            </div>
            <div className="text-lg font-bold font-mono mt-0.5 tabular-nums">
              {hasValidNumber(dteBannerVal) ? `DTE ${dteBannerVal}` : UNAVAILABLE_LABEL}
            </div>
          </div>
          <span
            className={`px-2.5 py-1 rounded-lg text-[11px] font-mono font-bold ${
              !hasValidNumber(dteBannerVal)
                ? 'bg-slate-200 text-slate-700'
                : expiryDayWarn
                ? 'bg-rose-600 text-white'
                : 'bg-emerald-600 text-white'
            }`}
          >
            {!hasValidNumber(dteBannerVal) ? 'UNAVAILABLE' : expiryDayWarn ? 'EXPIRY DAY' : 'NON-EXPIRY'}
          </span>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-3.5 flex items-center justify-between shadow-2xs">
          <div>
            <div className="text-[11px] font-bold uppercase tracking-wide text-slate-700">
              Isolated Shadow V1 Mirror
            </div>
            <div className="text-xs font-mono text-slate-800 mt-1">
              Book A: <strong>{shadowMirror?.book_a_tier || 'DRY_RUN'}</strong> • Book B:{' '}
              <strong>{shadowMirror?.book_b_tier || 'DRY_RUN'}</strong>
            </div>
            <div className="text-[11px] font-mono text-slate-500 mt-0.5">
              Dry-Run Closed: {shadowMirror?.dry_run_completed_count ?? 0} • Live Closed:{' '}
              {shadowMirror?.live_completed_count ?? 0}
            </div>
          </div>
          <span
            className={`px-2.5 py-1 rounded-lg text-[11px] font-mono font-bold ${
              shadowMirror?.book_a_tier === 'LIVE_ARMED' || shadowMirror?.book_b_tier === 'LIVE_ARMED'
                ? 'bg-rose-600 text-white'
                : 'bg-indigo-100 text-indigo-900 border border-indigo-200'
            }`}
          >
            {shadowMirror?.book_a_tier === 'LIVE_ARMED' || shadowMirror?.book_b_tier === 'LIVE_ARMED'
              ? 'LIVE MIRROR'
              : 'DRY-RUN ACTIVE'}
          </span>
        </div>
      </section>

      {mirrorRecent.length > 0 && (
        <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5 space-y-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h2 className="text-sm font-bold text-slate-900">
                Shadow V1 Mirror Execution Log (Dry-Run &amp; Live Audit)
              </h2>
              <p className="text-xs text-slate-500">
                Isolated atomic 2-leg Buy-First / Short-First-Exit (Book A) and Cancel-First SL-L
                (Book B) mirror ledger.
              </p>
            </div>
            <span className="text-xs font-mono text-slate-500">
              Broker Mutations Sent: {shadowMirror?.mutation_call_count ?? 0}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-200 text-slate-500 font-mono text-[11px]">
                  <th className="py-1.5 pr-3">Tier</th>
                  <th className="py-1.5 pr-3">Book</th>
                  <th className="py-1.5 pr-3">State</th>
                  <th className="py-1.5 pr-3">Leg 1 Entry→Exit</th>
                  <th className="py-1.5 pr-3">Leg 2 / SL</th>
                  <th className="py-1.5 pr-3 text-right">Mirror Net</th>
                  <th className="py-1.5 text-right">vs Shadow</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 font-mono">
                {mirrorRecent.map((m) => (
                  <tr key={m.mirror_id}>
                    <td className="py-1.5 pr-3">
                      <span
                        className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                          m.execution_tier === 'LIVE_ARMED'
                            ? 'bg-rose-100 text-rose-800'
                            : 'bg-slate-100 text-slate-700'
                        }`}
                      >
                        {m.execution_tier}
                      </span>
                    </td>
                    <td className="py-1.5 pr-3">{m.book_id}</td>
                    <td className="py-1.5 pr-3">{m.state}</td>
                    <td className="py-1.5 pr-3">
                      {hasValidNumber(m.leg1_entry_fill) ? formatINRValue(m.leg1_entry_fill) : UNAVAILABLE_LABEL}
                      {' → '}
                      {hasValidNumber(m.leg1_exit_fill) ? formatINRValue(m.leg1_exit_fill) : '—'}
                    </td>
                    <td className="py-1.5 pr-3">
                      {m.book_id === 'SHADOW_BOOK_A_SPREAD_150'
                        ? `${hasValidNumber(m.leg2_entry_fill) ? formatINRValue(m.leg2_entry_fill) : UNAVAILABLE_LABEL} → ${hasValidNumber(m.leg2_exit_fill) ? formatINRValue(m.leg2_exit_fill) : '—'}`
                        : `SL ${hasValidNumber(m.broker_sl_trigger) ? formatINRValue(m.broker_sl_trigger) : '—'}`}
                    </td>
                    <td className="py-1.5 pr-3 text-right">
                      {hasValidNumber(m.realized_net_pnl_inr)
                        ? formatSignedINR(m.realized_net_pnl_inr)
                        : UNAVAILABLE_LABEL}
                    </td>
                    <td className="py-1.5 text-right">
                      {hasValidNumber(m.live_minus_shadow_drift_inr)
                        ? formatSignedINR(m.live_minus_shadow_drift_inr)
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* 1. Open Exposure & Outstanding Execution Problems */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              Open Exposure &amp; Execution Exceptions
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Broker positions, active forward packages (<code>S205_SHADOW_FORWARD_V1</code>), OOS
              comparator positions (<code>V6B_ML_LEGACY_COMPARATOR</code>), and 2-leg spread states.
            </p>
          </div>
          <div className="flex items-center gap-3 text-xs font-mono text-slate-500">
            <span>
              Retrieved:{' '}
              {sliceMeta?.trades?.lastSuccessMs
                ? formatClockIST(sliceMeta.trades.lastSuccessMs)
                : UNAVAILABLE_LABEL}
            </span>
            <button
              type="button"
              onClick={() => onSelectTab('positions')}
              className="inline-flex items-center gap-1 text-indigo-700 hover:text-indigo-900 font-sans font-semibold cursor-pointer"
            >
              <span>Inspect Positions</span>
              <ArrowRight size={13} />
            </button>
          </div>
        </div>

        {sliceMeta?.trades?.status === 'error' || sliceMeta?.live?.status === 'error' ? (
          <div className="mt-3 p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900 flex items-center gap-2">
            <AlertTriangle size={15} className="text-amber-600 shrink-0" />
            <span>
              Partial exposure retrieval error — Broker status:{' '}
              <strong>{sliceMeta?.live?.errorMessage || sliceMeta?.live?.status}</strong> • Paper
              trades: <strong>{sliceMeta?.trades?.errorMessage || sliceMeta?.trades?.status}</strong>.
            </span>
          </div>
        ) : null}

        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3 mt-3">
          {/* Broker Exposure Box */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/60">
            <div className="flex items-center justify-between text-xs">
              <span className="font-semibold text-slate-700">Broker-Reported Positions</span>
              <span
                className={`px-2 py-0.5 rounded text-[11px] font-mono font-semibold ${
                  executionStatus.tone === 'danger'
                    ? 'bg-rose-100 text-rose-800'
                    : executionStatus.tone === 'warning'
                    ? 'bg-amber-100 text-amber-800'
                    : 'bg-slate-200 text-slate-700'
                }`}
              >
                {executionStatus.shortLabel}
              </span>
            </div>
            <div className="text-lg font-bold font-mono text-slate-900 mt-1.5 tabular-nums">
              {liveStatus ? `${brokerPositions.length} Open` : UNAVAILABLE_LABEL}
            </div>
            <div className="text-xs text-slate-600 mt-1">
              {problemIntents.length > 0 ? (
                <span className="text-rose-700 font-semibold">
                  {problemIntents.length} rejected / retry / unresolved intent(s) require review
                </span>
              ) : liveStatus ? (
                '0 unresolved broker order exceptions in recent sample'
              ) : (
                'Cannot verify broker positions until /live/status responds'
              )}
            </div>
          </div>

          {/* Forward Paper Exposure Box (S205_SHADOW_FORWARD_V1) */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/60 min-w-0">
            <div className="flex items-start justify-between gap-2 text-xs">
              <span className="font-semibold text-slate-700 min-w-0">
                Forward Paper Packages
                <span className="block text-[10px] font-mono font-normal text-slate-500 break-all">
                  S205_SHADOW_FORWARD_V1
                </span>
              </span>
              <span className="px-2 py-0.5 rounded text-[10px] font-mono bg-indigo-50 text-indigo-800 border border-indigo-200 shrink-0 whitespace-nowrap">
                SHADOW_V1
              </span>
            </div>
            <div className="text-lg font-bold font-mono text-slate-900 mt-1.5 tabular-nums">
              {sliceMeta?.trades?.status === 'ok'
                ? `${openForwardPackages.length} Open`
                : UNAVAILABLE_LABEL}
            </div>
            <div className="text-xs text-slate-600 mt-1 tabular-nums">
              {openForwardPackages.length > 0
                ? openForwardPackages
                    .map((p) => p.spread_legs_label || p.trading_symbol)
                    .join(' • ')
                : 'No open S205_SHADOW_FORWARD_V1 packages'}
            </div>
          </div>

          {/* V6B ML Comparator Exposure Box */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/60 min-w-0">
            <div className="flex items-start justify-between gap-2 text-xs">
              <span className="font-semibold text-slate-700 min-w-0">
                OOS Paper Comparator
                <span className="block text-[10px] font-mono font-normal text-slate-500 break-all">
                  V6B_ML_LEGACY_COMPARATOR
                </span>
              </span>
              <span className="px-2 py-0.5 rounded text-[10px] font-mono bg-amber-50 text-amber-900 border border-amber-200 shrink-0 whitespace-nowrap">
                NOT LIVE ELIGIBLE
              </span>
            </div>
            <div className="text-lg font-bold font-mono text-slate-900 mt-1.5 tabular-nums">
              {sliceMeta?.trades?.status === 'ok'
                ? `${openV6bComparator.length} Open`
                : UNAVAILABLE_LABEL}
            </div>
            <div className="text-xs text-slate-600 mt-1 tabular-nums">
              {openV6bComparator.length > 0
                ? openV6bComparator.map((p) => p.trading_symbol).join(' • ')
                : 'No open V6B_ML_LEGACY_COMPARATOR positions'}
            </div>
          </div>

          {/* Intermediate Leg & Unwind Exceptions Box */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/60">
            <div className="flex items-center justify-between text-xs">
              <span className="font-semibold text-slate-700">2-Leg Spread Execution Health</span>
              <span className="text-[11px] font-mono text-slate-500">Buy-First Wing</span>
            </div>
            <div className="text-lg font-bold font-mono text-slate-900 mt-1.5 tabular-nums">
              {hasValidNumber(pendingLeg2)
                ? `${pendingLeg2} Pending Leg-2`
                : UNAVAILABLE_LABEL}
            </div>
            <div className="text-xs text-slate-600 mt-1 tabular-nums">
              Incomplete-leg wing unwinds:{' '}
              <strong className={(unwoundCount || 0) > 0 ? 'text-amber-700' : 'text-slate-800'}>
                {hasValidNumber(unwoundCount) ? unwoundCount : UNAVAILABLE_LABEL}
              </strong>
            </div>
          </div>
        </div>
      </section>

      {/* 2. Side-by-Side Head-to-Head OOS Paper Cohort Comparison (Never Aggregated Into One Headline) */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <GitCompare size={16} className="text-indigo-700" />
              <span>
                Head-to-Head OOS Paper Comparison (<code>S205_SHADOW_FORWARD_V1</code> vs.{' '}
                <code>V6B_ML_LEGACY_COMPARATOR</code>)
              </span>
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Independent slots, cooldowns, and daily MTM kills on identical Upstox V3 sessions (
              &ge; {cohortComparison?.comparison_start_date || '2026-11-15'}). Cohort P&amp;L is
              strictly separated and never summed.
            </p>
          </div>
          <button
            type="button"
            onClick={() => onSelectTab('performance')}
            className="inline-flex items-center gap-1 text-xs text-indigo-700 hover:text-indigo-900 font-semibold cursor-pointer"
          >
            <span>Compare Calendars</span>
            <ArrowRight size={13} />
          </button>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Cohort A: S205_SHADOW_FORWARD_V1 */}
          <div className="rounded-xl border border-indigo-200 bg-indigo-50/20 p-4 space-y-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="flex items-center gap-2">
                  <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-indigo-100 text-indigo-900 border border-indigo-200">
                    SHADOW_V1
                  </span>
                  <h3 className="text-xs font-bold text-slate-900">
                    Active 2-Book L5 Architecture (<code>S205_SHADOW_FORWARD_V1</code>)
                  </h3>
                </div>
                <div className="text-[11px] font-mono text-slate-500 mt-1">
                  SHADOW_BOOK_A_SPREAD_150 (Buy-First 150pt Spread) + SHADOW_BOOK_B_PAID_FLOW (1L Paid Buy)
                </div>
              </div>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-white border border-slate-200 text-slate-700">
                Defined-Risk Paper
              </span>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 pt-1 font-mono tabular-nums">
              <div className="bg-white rounded-lg border border-slate-200 p-2.5">
                <div className="text-[10px] font-sans text-slate-500">Realized Net P&amp;L</div>
                <div
                  className={`text-sm font-bold mt-0.5 ${
                    !hasValidNumber(shadowRealizedNet)
                      ? 'text-slate-400'
                      : Number(shadowRealizedNet) > 0
                      ? 'text-emerald-700'
                      : Number(shadowRealizedNet) < 0
                      ? 'text-rose-700'
                      : 'text-slate-900'
                  }`}
                >
                  {formatSignedINR(shadowRealizedNet)}
                </div>
              </div>
              <div className="bg-white rounded-lg border border-slate-200 p-2.5">
                <div className="text-[10px] font-sans text-slate-500">Open MTM</div>
                <div className="text-sm font-bold text-slate-900 mt-0.5">
                  {formatSignedINR(
                    hasValidNumber(shCohort?.unrealized_net_pnl_inr)
                      ? shCohort.unrealized_net_pnl_inr
                      : openForwardUnrealized
                  )}
                </div>
              </div>
              <div className="bg-white rounded-lg border border-slate-200 p-2.5">
                <div className="text-[10px] font-sans text-slate-500">Closed / Win%</div>
                <div className="text-sm font-bold text-slate-900 mt-0.5">
                  {hasValidNumber(shCohort?.closed_trades_count)
                    ? `${shCohort.closed_trades_count} (${
                        hasValidNumber(shCohort.win_rate_pct)
                          ? formatPercent(shCohort.win_rate_pct)
                          : '—'
                      })`
                    : UNAVAILABLE_LABEL}
                </div>
              </div>
              <div className="bg-white rounded-lg border border-slate-200 p-2.5">
                <div className="text-[10px] font-sans text-slate-500">Est. Fees / Today</div>
                <div className="text-sm font-bold text-slate-800 mt-0.5">
                  {formatINRValue(shadowCharges, { decimals: 0 })} •{' '}
                  {shCohort ? `${shCohort.today_trades_count}/${shCohort.max_trades_day}` : '—'}
                </div>
              </div>
            </div>
          </div>

          {/* Cohort B: V6B_ML_LEGACY_COMPARATOR */}
          <div className="rounded-xl border border-amber-200 bg-amber-50/20 p-4 space-y-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="flex items-center gap-2">
                  <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-100 text-amber-950 border border-amber-300">
                    V6B_ML_COMPARATOR
                  </span>
                  <h3 className="text-xs font-bold text-slate-900">
                    Retired 4-Engine + 30-Feature ML Benchmark
                  </h3>
                </div>
                <div className="text-[11px] font-mono text-slate-500 mt-1">
                  V6B_S1_OI • V6B_S3_SPIKE • V6B_S4_Z2 • V6B_LBASIS_ATM (p_min &ge; 0.56)
                </div>
              </div>
              <span className="text-[10px] font-mono font-semibold px-2 py-0.5 rounded bg-amber-100 text-amber-950 border border-amber-300">
                UNHEDGED_SINGLE_LEG_PAPER_COMPARATOR (NOT LIVE ELIGIBLE)
              </span>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 pt-1 font-mono tabular-nums">
              <div className="bg-white rounded-lg border border-slate-200 p-2.5">
                <div className="text-[10px] font-sans text-slate-500">Realized Net P&amp;L</div>
                <div
                  className={`text-sm font-bold mt-0.5 ${
                    !hasValidNumber(v6bCohort?.realized_net_pnl_inr)
                      ? 'text-slate-400'
                      : Number(v6bCohort.realized_net_pnl_inr) > 0
                      ? 'text-emerald-700'
                      : Number(v6bCohort.realized_net_pnl_inr) < 0
                      ? 'text-rose-700'
                      : 'text-slate-900'
                  }`}
                >
                  {formatSignedINR(v6bCohort?.realized_net_pnl_inr)}
                </div>
              </div>
              <div className="bg-white rounded-lg border border-slate-200 p-2.5">
                <div className="text-[10px] font-sans text-slate-500">Open MTM</div>
                <div className="text-sm font-bold text-slate-900 mt-0.5">
                  {formatSignedINR(
                    hasValidNumber(v6bCohort?.unrealized_net_pnl_inr)
                      ? v6bCohort.unrealized_net_pnl_inr
                      : openV6bUnrealized
                  )}
                </div>
              </div>
              <div className="bg-white rounded-lg border border-slate-200 p-2.5">
                <div className="text-[10px] font-sans text-slate-500">Closed / Win%</div>
                <div className="text-sm font-bold text-slate-900 mt-0.5">
                  {hasValidNumber(v6bCohort?.closed_trades_count)
                    ? `${v6bCohort.closed_trades_count} (${
                        hasValidNumber(v6bCohort.win_rate_pct)
                          ? formatPercent(v6bCohort.win_rate_pct)
                          : '—'
                      })`
                    : UNAVAILABLE_LABEL}
                </div>
              </div>
              <div className="bg-white rounded-lg border border-slate-200 p-2.5">
                <div className="text-[10px] font-sans text-slate-500">Est. Fees / Today</div>
                <div className="text-sm font-bold text-slate-800 mt-0.5">
                  {formatINRValue(v6bCohort?.total_charges_inr, { decimals: 0 })} •{' '}
                  {v6bCohort ? `${v6bCohort.today_trades_count}/${v6bCohort.max_trades_day}` : '—'}
                </div>
              </div>
            </div>
          </div>
        </div>

        {histCohort && hasValidNumber(histCohort.closed_trades_count) && histCohort.closed_trades_count > 0 && (
          <div className="text-[11px] font-mono text-slate-500 pt-1">
            Historical Pre-Registration Archive (<code>HISTORICAL_LEGACY_V2</code>, &le; 2026-11-14):{' '}
            {histCohort.closed_trades_count} closed trades ({formatSignedINR(histCohort.realized_net_pnl_inr)})
            kept strictly excluded from both active OOS cohorts above.
          </div>
        )}
      </section>

      {/* 3. Scoped Broker, Funds & Risk Controls Cards */}
      <section className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {/* Card 1: Forward Paper Envelope (S205_SHADOW_FORWARD_V1 Only) */}
        <div className="bg-white rounded-xl border border-slate-200 p-4 shadow-2xs flex flex-col justify-between min-w-0">
          <div>
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-semibold text-slate-600 leading-snug min-w-0">
                Forward Paper Ledger
                <span className="block text-[10px] font-mono font-normal text-slate-500 break-all">
                  S205_SHADOW_FORWARD_V1
                </span>
              </span>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 shrink-0 whitespace-nowrap">
                Paper Scope
              </span>
            </div>
            <div
              className={`text-2xl font-bold font-mono mt-2 tabular-nums ${
                !hasValidNumber(shadowLedger?.realized_net_pnl_inr)
                  ? 'text-slate-400 text-lg'
                  : Number(shadowLedger.realized_net_pnl_inr) > 0
                  ? 'text-emerald-700'
                  : Number(shadowLedger.realized_net_pnl_inr) < 0
                  ? 'text-rose-700'
                  : 'text-slate-900'
              }`}
            >
              {formatSignedINR(shadowLedger?.realized_net_pnl_inr)}
            </div>
            <div className="mt-2 space-y-1 text-xs font-mono text-slate-600 tabular-nums">
              <div className="flex justify-between">
                <span>Open Unrealized MTM:</span>
                <span className="font-semibold text-slate-800">
                  {formatSignedINR(openForwardUnrealized)}
                </span>
              </div>
              <div className="flex justify-between">
                <span>Est. Canonical Fees:</span>
                <span>{formatINRValue(shadowLedger?.total_canonical_charges_inr)}</span>
              </div>
              <div className="flex justify-between">
                <span>Protocol Paper Envelope:</span>
                <span>{formatINRValue(shadowLedger?.initial_capital_inr, { decimals: 0 })}</span>
              </div>
            </div>
          </div>
          <div className="mt-3 pt-2 border-t border-slate-100 text-[11px] text-slate-500 flex justify-between font-mono">
            <span>Pre-registered paper envelope</span>
            <span>
              {sliceMeta?.readiness?.lastSuccessMs
                ? formatClockIST(sliceMeta.readiness.lastSuccessMs)
                : UNAVAILABLE_LABEL}
            </span>
          </div>
        </div>

        {/* Card 2: Broker Today Session P&L */}
        <div className="bg-white rounded-xl border border-slate-200 p-4 shadow-2xs flex flex-col justify-between min-w-0">
          <div>
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-semibold text-slate-600 leading-snug">
                Broker Session P&amp;L (Today)
              </span>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 shrink-0 whitespace-nowrap">
                Broker Scope
              </span>
            </div>
            <div
              className={`text-2xl font-bold font-mono mt-2 tabular-nums ${
                !hasValidNumber(liveStatus?.pnl_summary?.realized_net_pnl)
                  ? 'text-slate-400 text-lg'
                  : Number(liveStatus.pnl_summary.realized_net_pnl) > 0
                  ? 'text-emerald-700'
                  : Number(liveStatus.pnl_summary.realized_net_pnl) < 0
                  ? 'text-rose-700'
                  : 'text-slate-900'
              }`}
            >
              {formatSignedINR(liveStatus?.pnl_summary?.realized_net_pnl)}
            </div>
            <div className="mt-2 space-y-1 text-xs font-mono text-slate-600 tabular-nums">
              <div className="flex justify-between">
                <span>Broker Unrealized:</span>
                <span className="font-semibold text-slate-800">
                  {formatSignedINR(liveStatus?.pnl_summary?.unrealized_net_pnl)}
                </span>
              </div>
              <div className="flex justify-between">
                <span>Est. Statutory Fees:</span>
                <span>{formatINRValue(liveStatus?.pnl_summary?.statutory_charges)}</span>
              </div>
              <div className="flex justify-between">
                <span>Today Closed Count:</span>
                <span>
                  {hasValidNumber(liveStatus?.pnl_summary?.closed_trades_count)
                    ? liveStatus.pnl_summary.closed_trades_count
                    : UNAVAILABLE_LABEL}
                </span>
              </div>
            </div>
          </div>
          <div className="mt-3 pt-2 border-t border-slate-100 text-[11px] text-slate-500 flex justify-between font-mono">
            <span>Session: {liveStatus?.pnl_summary?.session_date || UNAVAILABLE_LABEL}</span>
            <span>
              {sliceMeta?.live?.lastSuccessMs
                ? formatClockIST(sliceMeta.live.lastSuccessMs)
                : UNAVAILABLE_LABEL}
            </span>
          </div>
        </div>

        {/* Card 3: Available Funds & Margin Provenance */}
        <div className="bg-white rounded-xl border border-slate-200 p-4 shadow-2xs flex flex-col justify-between min-w-0">
          <div>
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-semibold text-slate-600 flex items-center gap-1 leading-snug">
                <Wallet size={13} className="text-slate-500 shrink-0" />
                <span>Broker Funds &amp; Margin</span>
              </span>
              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 shrink-0 whitespace-nowrap">
                Upstox Snapshot
              </span>
            </div>
            <div
              className={`text-2xl font-bold font-mono mt-2 tabular-nums ${
                hasValidNumber(availMargin) ? 'text-slate-900' : 'text-slate-400 text-lg'
              }`}
            >
              {formatINRValue(availMargin)}
            </div>
            <div className="mt-2 space-y-1 text-xs font-mono text-slate-600 tabular-nums">
              <div className="flex justify-between">
                <span>Mode Required Min:</span>
                <span>{formatINRValue(reqMargin, { decimals: 0 })}</span>
              </div>
              <div className="flex justify-between">
                <span>Shadow V1 Reserved:</span>
                <span>
                  {formatINRValue(shadowLedger?.active_cash_or_margin_reserved_inr, {
                    decimals: 0,
                  })}
                </span>
              </div>
              <div className="flex justify-between">
                <span>Latest SPAN Probe:</span>
                <span>
                  {marginProbe?.final_margin_inr
                    ? formatINRValue(marginProbe.final_margin_inr, { decimals: 0 })
                    : 'No probe yet'}
                </span>
              </div>
            </div>
          </div>
          <div className="mt-3 pt-2 border-t border-slate-100 text-[11px] text-slate-500 flex justify-between font-mono">
            <span className="truncate" title={providerMarginTs || UNAVAILABLE_LABEL}>
              Snap: {formatTimestampLabel(providerMarginTs)}
            </span>
          </div>
        </div>

        {/* Card 4: Independent Cohort Risk Envelopes */}
        <div className="bg-white rounded-xl border border-slate-200 p-4 shadow-2xs flex flex-col justify-between min-w-0">
          <div>
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-semibold text-slate-600 flex items-center gap-1 leading-snug">
                <ShieldAlert size={13} className="text-slate-500 shrink-0" />
                <span>Independent Risk Controls</span>
              </span>
              <span
                className={`text-[10px] font-mono px-1.5 py-0.5 rounded shrink-0 whitespace-nowrap ${
                  shCohort?.daily_kill_triggered ||
                  v6bCohort?.daily_kill_triggered ||
                  liveStatus?.circuit_breaker?.is_session_halted
                    ? 'bg-rose-100 text-rose-800 font-bold'
                    : 'bg-slate-100 text-slate-700'
                }`}
              >
                {shCohort?.daily_kill_triggered || liveStatus?.circuit_breaker?.is_session_halted
                  ? 'HALTED'
                  : 'ISOLATED'}
              </span>
            </div>
            <div className="mt-2.5 space-y-1.5 text-xs font-mono text-slate-700 tabular-nums">
              <div className="flex justify-between">
                <span className="text-slate-500">Per-Cohort Daily Kill:</span>
                <span className="font-semibold">
                  {formatSignedINR(activeReadiness?.session_risk?.daily_kill_limit_inr ?? -3500, {
                    decimals: 0,
                  })}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Shadow V1 Slots:</span>
                <span>
                  {shCohort
                    ? `${shCohort.today_trades_count}/${shCohort.max_trades_day} day (${shCohort.open_trades_count}/${shCohort.max_open_positions} open)`
                    : UNAVAILABLE_LABEL}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">V6B ML Slots:</span>
                <span>
                  {v6bCohort
                    ? `${v6bCohort.today_trades_count}/${v6bCohort.max_trades_day} day (${v6bCohort.open_trades_count}/${v6bCohort.max_open_positions} open)`
                    : UNAVAILABLE_LABEL}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Post-Exit Cooldown:</span>
                <span>900s Independent</span>
              </div>
            </div>
          </div>
          <div className="mt-3 pt-2 border-t border-slate-100 text-[11px] text-slate-500">
            Stop/kill thresholds are software triggers; cohorts never block or flatten each other.
          </div>
        </div>
      </section>

      {/* 4. Current Candidates & Principal Blockers (Both OOS Cohorts Separated) */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              Current OOS Candidates &amp; Principal Blockers (<code>SHADOW_V1</code> vs.{' '}
              <code>V6B_ML_COMPARATOR</code>)
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Operational rule status for both side-by-side paper cohorts. Rule eligibility does not
              imply positive statistical expectancy.
            </p>
          </div>
          <div className="flex items-center gap-3 text-xs font-mono text-slate-500">
            <span className="inline-flex items-center gap-1">
              <Clock size={12} />
              <span>
                Eval Bar: {formatTimestampLabel(activeReadiness?.timing?.last_bar_evaluated_ts)}
              </span>
            </span>
            <button
              type="button"
              onClick={() => onSelectTab('signals')}
              className="inline-flex items-center gap-1 text-indigo-700 hover:text-indigo-900 font-sans font-semibold cursor-pointer"
            >
              <span>All Gate Checks</span>
              <ArrowRight size={13} />
            </button>
          </div>
        </div>

        {shadowCandidateRows.length === 0 && v6bCandidateRows.length === 0 ? (
          <div className="p-4 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-600 font-mono">
            {sliceMeta?.readiness?.status === 'loading'
              ? 'Loading candidate readiness...'
              : 'Candidate readiness unavailable.'}
          </div>
        ) : (
          <div className="space-y-4">
            {/* Group A: SHADOW_V1 */}
            <div className="space-y-2">
              <div className="text-xs font-bold uppercase tracking-wide text-indigo-900 flex items-center gap-2">
                <span className="px-2 py-0.5 rounded bg-indigo-100 border border-indigo-200 font-mono text-[10px]">
                  SHADOW_V1
                </span>
                <span>Active 2-Book L5 Forward Candidates</span>
              </div>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {shadowCandidateRows.map((row) => {
                  const info = deriveCandidateBlocker(row);
                  const isEligible = row.state === 'READY_FIRE';
                  return (
                    <div
                      key={row.engine_id}
                      className="rounded-lg border border-slate-200 p-3.5 flex flex-col justify-between gap-2 bg-slate-50/40"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <div className="text-xs font-bold text-slate-900">{row.label}</div>
                          <div className="text-[11px] font-mono text-slate-500">
                            {row.engine_id} • Window {row.window_ist || UNAVAILABLE_LABEL} IST
                          </div>
                        </div>
                        <span
                          className={`px-2 py-0.5 rounded text-[11px] font-mono font-semibold border ${
                            isEligible
                              ? 'bg-emerald-50 text-emerald-800 border-emerald-300'
                              : 'bg-slate-100 text-slate-700 border-slate-300'
                          }`}
                        >
                          {row.state} ({row.gates_passed ?? 0}/{row.gates_total ?? 0} checks)
                        </span>
                      </div>

                      <div className="text-xs font-mono text-slate-800 flex flex-wrap items-center justify-between gap-2 pt-1">
                        <span>
                          Candidate:{' '}
                          <strong>
                            {row.candidate_contract && row.candidate_contract !== '—'
                              ? row.candidate_contract
                              : UNAVAILABLE_LABEL}
                          </strong>
                        </span>
                        <span className="text-slate-500">Direction: {info.direction}</span>
                      </div>

                      <div className="text-xs px-2.5 py-1.5 rounded bg-white border border-slate-200 text-slate-700">
                        <span className="font-semibold text-slate-900">
                          {isEligible ? 'Operational Status: ' : 'Principal Blocker: '}
                        </span>
                        <span className="font-mono text-[11px]">{info.principalBlocker}</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Group B: V6B_ML_COMPARATOR */}
            {v6bCandidateRows.length > 0 && (
              <div className="space-y-2 pt-2 border-t border-slate-100">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-bold uppercase tracking-wide text-amber-950 flex items-center gap-2">
                    <span className="px-2 py-0.5 rounded bg-amber-100 border border-amber-300 font-mono text-[10px]">
                      V6B_ML_COMPARATOR
                    </span>
                    <span>Retired 4-Engine + 30-Feature ML OOS Paper Candidates</span>
                  </div>
                  <span className="text-[10px] font-mono font-semibold text-amber-900 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded">
                    UNHEDGED_SINGLE_LEG_PAPER_COMPARATOR (NOT LIVE ELIGIBLE)
                  </span>
                </div>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                  {v6bCandidateRows.map((row) => {
                    const info = deriveCandidateBlocker(row);
                    const isEligible = row.state === 'READY_FIRE';
                    const isShort = row.book_type === 'SHORT_BOOK_SELL';
                    const mlProbStr = hasValidNumber(row.ml_prob)
                      ? formatNumber(row.ml_prob)
                      : UNAVAILABLE_LABEL;
                    const mlMinStr = hasValidNumber(row.ml_p_min)
                      ? formatNumber(row.ml_p_min)
                      : '0.56';
                    return (
                      <div
                        key={row.engine_id}
                        className="rounded-lg border border-slate-200 p-3.5 flex flex-col justify-between gap-2 bg-amber-50/15"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <div className="text-xs font-bold text-slate-900">{row.label}</div>
                            <div className="text-[11px] font-mono text-slate-500">
                              {row.engine_id} • Window {row.window_ist || UNAVAILABLE_LABEL} IST
                            </div>
                          </div>
                          <span
                            className={`px-2 py-0.5 rounded text-[11px] font-mono font-semibold border ${
                              isEligible
                                ? 'bg-emerald-50 text-emerald-800 border-emerald-300'
                                : 'bg-slate-100 text-slate-700 border-slate-300'
                            }`}
                          >
                            {row.state} ({row.gates_passed ?? 0}/{row.gates_total ?? 0} checks)
                          </span>
                        </div>

                        <div className="text-xs font-mono text-slate-800 flex flex-wrap items-center justify-between gap-2 pt-1">
                          <span>
                            Candidate:{' '}
                            <strong>
                              {row.candidate_contract && row.candidate_contract !== '—'
                                ? row.candidate_contract
                                : UNAVAILABLE_LABEL}
                            </strong>
                          </span>
                          <span className="text-slate-600">
                            ML: <strong>{mlProbStr}</strong> / &ge; {mlMinStr}
                          </span>
                        </div>

                        {isShort && (
                          <div className="text-[10px] font-mono text-amber-900">
                            UNHEDGED_SINGLE_LEG_PAPER_COMPARATOR (NOT LIVE ELIGIBLE)
                          </div>
                        )}

                        <div className="text-xs px-2.5 py-1.5 rounded bg-white border border-slate-200 text-slate-700">
                          <span className="font-semibold text-slate-900">
                            {isEligible ? 'Operational Status: ' : 'Principal Blocker: '}
                          </span>
                          <span className="font-mono text-[11px]">{info.principalBlocker}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
