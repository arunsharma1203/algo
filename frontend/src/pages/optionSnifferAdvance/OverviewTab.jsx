import React from 'react';
import {
  AlertTriangle,
  ArrowRight,
  Clock,
  ShieldAlert,
  Wallet,
} from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  classifyStrategyEra,
  deriveCandidateBlocker,
  formatClockIST,
  formatINRValue,
  formatSignedINR,
  formatTimestampLabel,
  hasValidNumber,
  parseRegimeContext,
} from './formatters';

export default function OverviewTab({
  executionStatus,
  liveStatus,
  tradesData,
  activeReadiness,
  shadowTelemetry,
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
    (t) => classifyStrategyEra(t.strategy_mode, parseRegimeContext(t)) === 'SHADOW_FORWARD_V1'
  );
  const openLegacyPaper = allOpenPaper.filter(
    (t) => classifyStrategyEra(t.strategy_mode, parseRegimeContext(t)) !== 'SHADOW_FORWARD_V1'
  );

  const openForwardUnrealized =
    sliceMeta?.trades?.status === 'ok'
      ? openForwardPackages.reduce(
          (acc, t) => acc + (hasValidNumber(t.unrealized_net_pnl) ? Number(t.unrealized_net_pnl) : 0),
          0
        )
      : null;

  const shadowLedger = shadowTelemetry?.shadow_account_ledger || null;
  const pendingLeg2 = shadowLedger?.pending_leg2_count ?? null;
  const unwoundCount = shadowLedger?.incomplete_leg_unwinds_count ?? null;

  const engineRows = Array.isArray(activeReadiness?.engine_readiness)
    ? activeReadiness.engine_readiness
    : [];
  const activeCandidateRows = engineRows.filter(
    (r) => classifyStrategyEra(r.engine_id) === 'SHADOW_FORWARD_V1'
  );

  const availMargin = liveStatus?.available_margin ?? liveStatus?.readiness?.available_margin;
  const reqMargin = liveStatus?.required_margin_for_mode;
  const providerMarginTs = liveStatus?.readiness?.timestamp;
  const marginProbe = shadowTelemetry?.latest_margin_probe || null;

  return (
    <div className="space-y-5">
      {/* 1. Open Exposure & Outstanding Execution Problems */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              Open Exposure &amp; Execution Exceptions
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Broker positions, forward paper packages, and unresolved multi-leg or order states.
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

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 mt-3">
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

          {/* Forward Paper Exposure Box */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/60 min-w-0">
            <div className="flex items-start justify-between gap-2 text-xs">
              <span className="font-semibold text-slate-700 min-w-0">
                Forward Paper Packages
                <span className="block text-[10px] font-mono font-normal text-slate-500 break-all">
                  S205_SHADOW_FORWARD_V1
                </span>
              </span>
              <span className="px-2 py-0.5 rounded text-[11px] font-mono bg-indigo-50 text-indigo-800 border border-indigo-200 shrink-0 whitespace-nowrap">
                Airgapped Paper
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
                : 'No open forward paper packages'}
              {openLegacyPaper.length > 0
                ? ` (${openLegacyPaper.length} legacy paper open)`
                : ''}
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

      {/* 2. Scoped P&L, Charges, Margin Provenance & Risk Controls (Never labeled "Alpha") */}
      <section className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
        {/* Card 1: Forward Paper P&L (S205_SHADOW_FORWARD_V1 Only) */}
        <div className="bg-white rounded-xl border border-slate-200 p-4 shadow-2xs flex flex-col justify-between min-w-0">
          <div>
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-semibold text-slate-600 leading-snug min-w-0">
                Forward Paper P&amp;L
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
                <span>Paper Reserved:</span>
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

        {/* Card 4: Configured Risk Triggers (Honest Stop vs Structural Bound Language) */}
        <div className="bg-white rounded-xl border border-slate-200 p-4 shadow-2xs flex flex-col justify-between min-w-0">
          <div>
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-semibold text-slate-600 flex items-center gap-1 leading-snug">
                <ShieldAlert size={13} className="text-slate-500 shrink-0" />
                <span>Configured Risk Controls</span>
              </span>
              <span
                className={`text-[10px] font-mono px-1.5 py-0.5 rounded shrink-0 whitespace-nowrap ${
                  activeReadiness?.session_risk?.daily_kill_triggered ||
                  liveStatus?.circuit_breaker?.is_session_halted
                    ? 'bg-rose-100 text-rose-800 font-bold'
                    : 'bg-slate-100 text-slate-700'
                }`}
              >
                {activeReadiness?.session_risk?.daily_kill_triggered ||
                liveStatus?.circuit_breaker?.is_session_halted
                  ? 'HALTED'
                  : 'ACTIVE'}
              </span>
            </div>
            <div className="mt-2.5 space-y-1.5 text-xs font-mono text-slate-700 tabular-nums">
              <div className="flex justify-between">
                <span className="text-slate-500">Daily MTM Trigger:</span>
                <span className="font-semibold">
                  {formatSignedINR(activeReadiness?.session_risk?.daily_kill_limit_inr, {
                    decimals: 0,
                  })}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Broker Loss Trigger:</span>
                <span className="font-semibold">
                  {hasValidNumber(liveStatus?.circuit_breaker?.max_daily_loss_inr)
                    ? formatSignedINR(-Math.abs(liveStatus.circuit_breaker.max_daily_loss_inr), {
                        decimals: 0,
                      })
                    : UNAVAILABLE_LABEL}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Trail Update Rule:</span>
                <span>:00 Completed 1m Close</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Paper Slots Used:</span>
                <span>
                  {hasValidNumber(activeReadiness?.session_risk?.trades_today) &&
                  hasValidNumber(activeReadiness?.session_risk?.max_trades_day)
                    ? `${activeReadiness.session_risk.trades_today} / ${activeReadiness.session_risk.max_trades_day}`
                    : UNAVAILABLE_LABEL}
                </span>
              </div>
            </div>
          </div>
          <div className="mt-3 pt-2 border-t border-slate-100 text-[11px] text-slate-500">
            Stop/kill thresholds are software triggers, not guaranteed maximum losses under gaps.
          </div>
        </div>
      </section>

      {/* 3. Current Forward Candidates & Principal Blockers */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              Current Forward Candidates &amp; Principal Blockers
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Operational rule status for active forward paper books. Rule eligibility does not
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

        {activeCandidateRows.length === 0 ? (
          <div className="mt-3 p-4 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-600 font-mono">
            {sliceMeta?.readiness?.status === 'loading'
              ? 'Loading candidate readiness...'
              : 'Candidate readiness unavailable.'}
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-3">
            {activeCandidateRows.map((row) => {
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
        )}
      </section>
    </div>
  );
}
