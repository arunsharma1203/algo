import React, { useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  buildPackageLegRows,
  formatClockIST,
  formatINRValue,
  formatNumber,
  formatQuoteFreshness,
  formatSignedINR,
  formatTimestampLabel,
  getRowCohort,
  hasPositiveNumber,
  hasValidNumber,
  parseRegimeContext,
} from './formatters';

function intentStateBadge(state, exitState) {
  const st = String(state || 'UNKNOWN').toUpperCase();
  const ex = String(exitState || '').toUpperCase();
  if (st === 'REJECTED' || ex.includes('UNRESOLVED') || ex === 'EXIT_RETRY_PENDING') {
    return 'bg-rose-50 text-rose-800 border-rose-300';
  }
  if (st === 'PENDING' || st === 'SUBMITTED' || st === 'OPEN') {
    return 'bg-amber-50 text-amber-800 border-amber-300';
  }
  if (st === 'CANCELLED') {
    return 'bg-slate-100 text-slate-600 border-slate-300';
  }
  if (ex === 'CONFIRMED_FLAT') {
    return 'bg-slate-100 text-slate-800 border-slate-300';
  }
  return 'bg-indigo-50 text-indigo-800 border-indigo-200';
}

function formatLegAgeCompact(ageMs) {
  if (ageMs === null || ageMs === undefined || Number.isNaN(Number(ageMs))) {
    return 'unk';
  }
  const n = Math.max(0, Number(ageMs));
  if (n >= 60000) {
    return `${Math.round(n / 60000)}m`;
  }
  if (n >= 1000) {
    return `${(n / 1000).toFixed(1)}s`;
  }
  return `${Math.round(n)}ms`;
}

export default function PositionsTab({
  executionStatus,
  liveStatus,
  tradesData,
  shadowTelemetry,
  cohortComparison = null,
  sliceMeta,
  extraBrokerIntents = [],
  loadingMoreIntents = false,
  onLoadMoreBrokerIntents = null,
}) {
  const [expandedPackages, setExpandedPackages] = useState({});
  const [showHistoricalArchive, setShowHistoricalArchive] = useState(false);

  const togglePackage = (id) => {
    setExpandedPackages((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const brokerPositions = Array.isArray(liveStatus?.active_live_positions)
    ? liveStatus.active_live_positions
    : [];
  const mergedBrokerIntents = (() => {
    const recent = Array.isArray(liveStatus?.recent_order_intents)
      ? liveStatus.recent_order_intents
      : [];
    const extra = Array.isArray(extraBrokerIntents) ? extraBrokerIntents : [];
    const seen = new Set();
    const out = [];
    [...brokerPositions, ...recent, ...extra].forEach((row) => {
      const id = row?.order_intent_id;
      if (!id || seen.has(id)) return;
      seen.add(id);
      out.push(row);
    });
    return out;
  })();
  const totalBrokerIntentsCount = hasValidNumber(liveStatus?.total_order_intents_count)
    ? Math.max(mergedBrokerIntents.length, Number(liveStatus.total_order_intents_count))
    : mergedBrokerIntents.length;

  const allOpenTrades = Array.isArray(tradesData?.open_trades) ? tradesData.open_trades : [];
  const allClosedTrades = Array.isArray(tradesData?.closed_trades) ? tradesData.closed_trades : [];

  const openForwardTrades = allOpenTrades.filter(
    (t) => getRowCohort(t) === 'S205_SHADOW_FORWARD_V1'
  );
  const openV6bTrades = allOpenTrades.filter(
    (t) => getRowCohort(t) === 'V6B_ML_LEGACY_COMPARATOR'
  );
  const closedV6bTrades = allClosedTrades.filter(
    (t) => getRowCohort(t) === 'V6B_ML_LEGACY_COMPARATOR'
  );
  const openHistoricalTrades = allOpenTrades.filter(
    (t) => getRowCohort(t) === 'HISTORICAL_LEGACY_V2'
  );
  const closedHistoricalTrades = allClosedTrades.filter(
    (t) => getRowCohort(t) === 'HISTORICAL_LEGACY_V2'
  );

  const shadowPackages = Array.isArray(shadowTelemetry?.recent_packages)
    ? shadowTelemetry.recent_packages
    : [];
  const pendingLeg2Count = shadowTelemetry?.shadow_account_ledger?.pending_leg2_count ?? 0;
  const v6bCohortSummary = cohortComparison?.V6B_ML_LEGACY_COMPARATOR || null;

  return (
    <div className="space-y-5">
      {/* Risk & Payoff Semantics Notice */}
      <div className="bg-slate-100 border border-slate-200 rounded-xl px-4 py-3 text-xs text-slate-700 leading-relaxed">
        <strong className="text-slate-900">Risk &amp; Cohort Separation Disclosure:</strong>{' '}
        Broker positions, active forward packages (<code>S205_SHADOW_FORWARD_V1</code>), OOS
        comparator trades (<code>V6B_ML_LEGACY_COMPARATOR</code>), and pre-registration archive rows
        (<code>HISTORICAL_LEGACY_V2</code>) are strictly separated. Software stop-loss levels are
        order triggers—not guaranteed maximum losses under gaps.
      </div>

      {/* SECTION 1: Broker-Reported Live Positions & Order Intents */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs overflow-hidden">
        <div className="p-4 sm:p-5 border-b border-slate-100 flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-bold text-slate-900">
                1. Broker-Reported Positions &amp; Order Intents
              </h2>
              <span
                className={`px-2 py-0.5 rounded text-[11px] font-mono font-semibold border ${
                  executionStatus.tone === 'danger'
                    ? 'bg-rose-50 text-rose-800 border-rose-300'
                    : executionStatus.tone === 'warning'
                    ? 'bg-amber-50 text-amber-800 border-amber-300'
                    : 'bg-slate-100 text-slate-700 border-slate-300'
                }`}
              >
                {executionStatus.shortLabel}
              </span>
            </div>
            <p className="text-xs text-slate-500 mt-0.5">
              Isolated Upstox broker intents (showing {mergedBrokerIntents.length} of{' '}
              {totalBrokerIntentsCount} total SQLite records).
            </p>
          </div>
          <span className="text-xs font-mono text-slate-500">
            Retrieved:{' '}
            {sliceMeta?.live?.lastSuccessMs
              ? formatClockIST(sliceMeta.live.lastSuccessMs)
              : UNAVAILABLE_LABEL}
          </span>
        </div>

        {!liveStatus ? (
          <div className="p-4 m-4 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900 flex items-center gap-2">
            <AlertTriangle size={15} className="text-amber-600 shrink-0" />
            <span>
              Broker status unavailable ({sliceMeta?.live?.errorMessage || 'awaiting response'}).
              Active broker exposure cannot be confirmed.
            </span>
          </div>
        ) : brokerPositions.length === 0 && mergedBrokerIntents.length === 0 ? (
          <div className="p-5 text-xs font-mono text-slate-500">
            0 active broker positions and 0 recent broker order intents.
          </div>
        ) : (
          <div className="overflow-x-auto">
            {Boolean(liveStatus?.pnl_summary?.has_stale_positions) && (
              <div className="mx-4 mt-3 mb-1 p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900 flex items-center gap-2 font-mono">
                <AlertTriangle size={14} className="text-amber-600 shrink-0" />
                <span>
                  STALE LIVE QUOTE ALERT: {liveStatus?.pnl_summary?.stale_positions_count || 1}{' '}
                  open broker position(s) have stale or unknown leg quote ages (&gt;10s) — displayed
                  MTM may lag Upstox; new live entries are paused until fresh quotes resume.
                </span>
              </div>
            )}
            <table className="w-full text-left border-collapse text-xs font-mono">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-200 text-[11px] font-sans font-semibold uppercase tracking-wide text-slate-600">
                  <th className="py-2.5 px-3">Created / Updated (IST)</th>
                  <th className="py-2.5 px-3">Mode</th>
                  <th className="py-2.5 px-3">Contract</th>
                  <th className="py-2.5 px-3 text-right">Side &amp; Qty</th>
                  <th className="py-2.5 px-3 text-right">Fill / Limit</th>
                  <th className="py-2.5 px-3">Hedge State</th>
                  <th className="py-2.5 px-3">Order / Exit State</th>
                  <th className="py-2.5 px-3 text-right">Est. Fees</th>
                  <th className="py-2.5 px-3 text-right">Net P&amp;L</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 tabular-nums">
                {mergedBrokerIntents.map((intent) => {
                  const activeMatch = brokerPositions.find(
                    (p) => p.order_intent_id === intent.order_intent_id
                  );
                  const hedgeLabel = intent.hedge_wing_strike
                    ? intent.hedge_wing_broker_order_id
                      ? `Wing ${intent.hedge_wing_strike} (Order ${intent.hedge_wing_broker_order_id})`
                      : `Wing ${intent.hedge_wing_strike} (Unconfirmed Order)`
                    : intent.action === 'BUY'
                    ? 'Long Premium (N/A)'
                    : 'Unhedged Short';

                  const netPnl =
                    intent.state === 'CANCELLED' || intent.state === 'REJECTED'
                      ? null
                      : intent.exit_state === 'CONFIRMED_FLAT'
                      ? intent.realized_pnl
                      : activeMatch?.unrealized_pnl ?? intent.unrealized_pnl;
                  const isOpenRow =
                    intent.state !== 'CANCELLED' &&
                    intent.state !== 'REJECTED' &&
                    intent.exit_state !== 'CONFIRMED_FLAT';
                  const isStaleLiveQuote =
                    isOpenRow && Boolean(activeMatch?.quote_stale ?? intent?.quote_stale);
                  const wingAgeMs = activeMatch?.wing_quote_age_ms ?? intent?.wing_quote_age_ms;
                  const shortAgeMs = activeMatch?.short_quote_age_ms ?? intent?.short_quote_age_ms;

                  return (
                    <tr key={intent.order_intent_id} className="hover:bg-slate-50">
                      <td className="py-2.5 px-3 whitespace-nowrap text-slate-600">
                        <div>{formatTimestampLabel(intent.created_at)}</div>
                        <div className="text-[10px] text-slate-400">
                          Upd: {formatTimestampLabel(intent.updated_at)}
                        </div>
                      </td>
                      <td className="py-2.5 px-3 whitespace-nowrap text-slate-700">
                        {intent.execution_mode || UNAVAILABLE_LABEL}
                      </td>
                      <td className="py-2.5 px-3 font-semibold text-slate-900 whitespace-nowrap">
                        <div>{intent.trading_symbol || UNAVAILABLE_LABEL}</div>
                        {intent.rejection_reason && (
                          <div className="text-[10px] font-sans text-rose-700 mt-0.5">
                            {intent.rejection_reason}
                          </div>
                        )}
                      </td>
                      <td className="py-2.5 px-3 text-right whitespace-nowrap">
                        {intent.action || UNAVAILABLE_LABEL}{' '}
                        {hasValidNumber(intent.filled_quantity)
                          ? `${intent.filled_quantity}/${intent.target_quantity ?? '?'}`
                          : intent.target_quantity ?? UNAVAILABLE_LABEL}
                      </td>
                      <td className="py-2.5 px-3 text-right whitespace-nowrap">
                        {hasPositiveNumber(intent.average_fill_price)
                          ? `${formatINRValue(intent.average_fill_price)} (fill)`
                          : hasPositiveNumber(intent.limit_price)
                          ? `${formatINRValue(intent.limit_price)} (lim)`
                          : UNAVAILABLE_LABEL}
                        {hasPositiveNumber(intent.exit_fill_price) && (
                          <div className="text-[10px] text-slate-500">
                            Exit: {formatINRValue(intent.exit_fill_price)}
                          </div>
                        )}
                      </td>
                      <td className="py-2.5 px-3 font-sans text-[11px] text-slate-700">
                        {hedgeLabel}
                      </td>
                      <td className="py-2.5 px-3 whitespace-nowrap">
                        <span
                          className={`inline-block px-2 py-0.5 rounded text-[10px] font-semibold border ${intentStateBadge(
                            intent.state,
                            intent.exit_state
                          )}`}
                        >
                          {intent.state || 'UNKNOWN'} / {intent.exit_state || 'PENDING'}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-right whitespace-nowrap text-slate-600">
                        {formatINRValue(intent.statutory_charges)}
                      </td>
                      <td
                        className={`py-2.5 px-3 text-right font-semibold whitespace-nowrap ${
                          !hasValidNumber(netPnl)
                            ? 'text-slate-400'
                            : isStaleLiveQuote
                            ? 'text-amber-700'
                            : Number(netPnl) > 0
                            ? 'text-emerald-700'
                            : Number(netPnl) < 0
                            ? 'text-rose-700'
                            : 'text-slate-800'
                        }`}
                      >
                        <div>
                          {intent.state === 'CANCELLED'
                            ? 'Cancelled (0 fill)'
                            : intent.state === 'REJECTED'
                            ? 'Rejected (0 fill)'
                            : formatSignedINR(netPnl)}
                        </div>
                        {isStaleLiveQuote && (
                          <div className="mt-0.5 flex flex-col items-end gap-0.5">
                            <span className="inline-block px-1.5 py-0.2 rounded text-[9px] font-bold bg-amber-100 text-amber-900 border border-amber-300">
                              STALE QUOTE
                            </span>
                            <span className="text-[9px] font-normal text-amber-800">
                              {hasPositiveNumber(intent.hedge_wing_strike)
                                ? `W:${formatLegAgeCompact(wingAgeMs)} / S:${formatLegAgeCompact(shortAgeMs)} — check Upstox`
                                : `Age:${formatLegAgeCompact(wingAgeMs ?? shortAgeMs)} — check Upstox`}
                            </span>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {onLoadMoreBrokerIntents && totalBrokerIntentsCount > mergedBrokerIntents.length && (
              <div className="px-4 py-3 border-t border-slate-100 bg-slate-50/60 flex items-center justify-between gap-2 text-xs">
                <span className="font-mono text-slate-500">
                  Showing {mergedBrokerIntents.length} of {totalBrokerIntentsCount} broker intents
                </span>
                <button
                  type="button"
                  disabled={loadingMoreIntents}
                  onClick={() => onLoadMoreBrokerIntents(mergedBrokerIntents.length)}
                  className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 hover:bg-slate-100 text-slate-800 font-semibold disabled:opacity-50 cursor-pointer"
                >
                  {loadingMoreIntents ? 'Loading older intents…' : 'Load Older Broker Intents'}
                </button>
              </div>
            )}
          </div>
        )}
      </section>

      {/* SECTION 2: Forward Paper / Shadow Packages (S205_SHADOW_FORWARD_V1) with Expandable Legs */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs overflow-hidden">
        <div className="p-4 sm:p-5 border-b border-slate-100 flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="flex items-center gap-2">
              <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-indigo-100 text-indigo-900 border border-indigo-200">
                SHADOW_V1
              </span>
              <h2 className="text-sm font-bold text-slate-900">
                2. Forward Paper Packages (<code>S205_SHADOW_FORWARD_V1</code> — Open &amp; Recent)
              </h2>
            </div>
            <p className="text-xs text-slate-500 mt-0.5">
              Expand any open package to inspect individual legs, live L1 quotes &amp; tick age,
              paid outlay vs. payoff bound vs. stop trigger vs. reserved margin.
            </p>
          </div>
          <span className="text-xs font-mono text-slate-500">
            Retrieved:{' '}
            {sliceMeta?.trades?.lastSuccessMs
              ? formatClockIST(sliceMeta.trades.lastSuccessMs)
              : UNAVAILABLE_LABEL}
          </span>
        </div>

        {pendingLeg2Count > 0 && (
          <div className="mx-4 mt-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900 font-mono">
            INTERMEDIATE LEG ALERT: {pendingLeg2Count} buy-first protective wing(s) currently
            awaiting Leg-2 short fill (&le; 3,000ms window before automatic wing unwind).
          </div>
        )}

        {(shadowTelemetry?.shadow_account_ledger?.unpriced_open_packages_count || 0) > 0 && (
          <div className="mx-4 mt-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900 flex items-center gap-2 font-mono">
            <AlertTriangle size={14} className="text-amber-600 shrink-0" />
            <span>
              UNPRICED OPEN RISK ALERT:{' '}
              {shadowTelemetry.shadow_account_ledger.unpriced_open_packages_count} shadow package(s)
              currently have stale or unverified leg quotes — new shadow entries halted until fresh
              quotes resume.
            </span>
          </div>
        )}

        <div className="p-4 sm:p-5 space-y-3">
          <h3 className="text-xs font-bold uppercase tracking-wide text-slate-600">
            Open Forward Packages ({openForwardTrades.length})
          </h3>

          {openForwardTrades.length === 0 ? (
            <div className="p-4 rounded-lg bg-slate-50 border border-slate-200 text-xs font-mono text-slate-500">
              No open S205_SHADOW_FORWARD_V1 packages.
            </div>
          ) : (
            <div className="overflow-x-auto border border-slate-200 rounded-lg">
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-200 text-[11px] font-sans font-semibold uppercase tracking-wide text-slate-600">
                    <th className="py-2.5 px-2 w-8" aria-label="Expand legs" />
                    <th className="py-2.5 px-3">Book / Package</th>
                    <th className="py-2.5 px-3 text-right">Qty</th>
                    <th className="py-2.5 px-3 text-right">Entry Fill (Est.)</th>
                    <th className="py-2.5 px-3 text-right">Live Net Bid / Ask</th>
                    <th className="py-2.5 px-3 text-right">Stop Trigger</th>
                    <th className="py-2.5 px-3 text-right">Payoff Bound</th>
                    <th className="py-2.5 px-3 text-right">Margin / Outlay</th>
                    <th className="py-2.5 px-3 text-right">Est. Fees</th>
                    <th className="py-2.5 px-3 text-right">Unrealized Net</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 tabular-nums">
                  {openForwardTrades.map((t) => {
                    const isExpanded = Boolean(expandedPackages[t.trade_id]);
                    const rctx = parseRegimeContext(t);
                    const legs = buildPackageLegRows(t);
                    const isSpread = t.strategy_mode === 'SHADOW_BOOK_A_SPREAD_150';
                    const payoffBound = isSpread
                      ? rctx.structural_max_loss_inr
                      : hasValidNumber(t.entry_price) && hasValidNumber(t.quantity)
                      ? Number(t.entry_price) * Number(t.quantity)
                      : null;

                    return (
                      <React.Fragment key={t.trade_id}>
                        <tr className="hover:bg-slate-50">
                          <td className="py-2.5 px-2">
                            <button
                              type="button"
                              onClick={() => togglePackage(t.trade_id)}
                              aria-expanded={isExpanded}
                              aria-label={`Toggle leg details for ${t.trade_id}`}
                              className="p-1 rounded hover:bg-slate-200/70 text-slate-600 cursor-pointer"
                            >
                              {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                            </button>
                          </td>
                          <td className="py-2.5 px-3">
                            <div className="font-semibold text-slate-900">
                              {t.trading_symbol || UNAVAILABLE_LABEL}
                            </div>
                            <div className="text-[10px] text-slate-500">
                              {t.strategy_mode} • Exp {t.expiry || UNAVAILABLE_LABEL} •{' '}
                              {formatTimestampLabel(t.entry_time)}
                            </div>
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            {hasValidNumber(t.quantity) ? t.quantity : UNAVAILABLE_LABEL}
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            {formatINRValue(t.entry_price)}{' '}
                            <span className="text-[10px] text-slate-400">
                              {isSpread ? '(cr)' : '(paid)'}
                            </span>
                          </td>
                          <td className="py-2.5 px-3 text-right whitespace-nowrap">
                            <div>
                              {hasValidNumber(t.current_bid) && hasValidNumber(t.current_ask)
                                ? `${formatINRValue(t.current_bid)} / ${formatINRValue(t.current_ask)}`
                                : UNAVAILABLE_LABEL}
                            </div>
                            <div className="text-[10px] text-slate-500">
                              Quote: {formatQuoteFreshness(t)}
                            </div>
                          </td>
                          <td className="py-2.5 px-3 text-right text-amber-800 font-semibold">
                            {formatINRValue(t.effective_stop_price)}
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            {formatINRValue(payoffBound, { decimals: 0 })}
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            <div>{formatINRValue(t.estimated_margin, { decimals: 0 })}</div>
                            <div className="text-[10px] text-slate-400">
                              {isSpread ? rctx.margin_status || 'Margin reserve' : 'Cash outlay'}
                            </div>
                          </td>
                          <td className="py-2.5 px-3 text-right text-slate-600">
                            {formatINRValue(t.unrealized_charges)}
                          </td>
                          <td
                            className={`py-2.5 px-3 text-right font-bold ${
                              !hasValidNumber(t.unrealized_net_pnl)
                                ? 'text-slate-400'
                                : Number(t.unrealized_net_pnl) >= 0
                                ? 'text-emerald-700'
                                : 'text-rose-700'
                            }`}
                          >
                            {formatSignedINR(t.unrealized_net_pnl)}
                          </td>
                        </tr>

                        {isExpanded && (
                          <tr className="bg-slate-50/90">
                            <td colSpan={10} className="p-3.5 border-t border-slate-100">
                              <div className="text-[11px] font-sans font-semibold text-slate-700 mb-2">
                                Individual Package Legs ({legs.length})
                              </div>
                              <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                                {legs.map((leg) => (
                                  <div
                                    key={leg.legId}
                                    className="bg-white rounded-lg border border-slate-200 p-2.5 text-[11px] font-mono space-y-1"
                                  >
                                    <div className="flex items-center justify-between font-bold text-slate-900">
                                      <span>{leg.role}</span>
                                      <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700">
                                        {leg.side} {leg.quantity}
                                      </span>
                                    </div>
                                    <div>
                                      Contract: <strong>{leg.contract}</strong> (Exp {leg.expiry})
                                    </div>
                                    <div>Simulated Entry Fill: {leg.entryPrice}</div>
                                    <div>Current Quote: {leg.liveOrExitQuote}</div>
                                    <div className="text-slate-500">Hedge: {leg.hedgeState}</div>
                                  </div>
                                ))}
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* 2B: Recent Forward Package Ledger */}
          <h3 className="text-xs font-bold uppercase tracking-wide text-slate-600 pt-2">
            Recent Collector Package Ledger ({shadowPackages.length})
          </h3>
          {shadowPackages.length === 0 ? (
            <div className="p-4 rounded-lg bg-slate-50 border border-slate-200 text-xs font-mono text-slate-500">
              No forward shadow/paper packages recorded yet in option_sniffer_advance_s205_shadow_packages.
            </div>
          ) : (
            <div className="overflow-x-auto border border-slate-200 rounded-lg">
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-200 text-[11px] font-sans font-semibold uppercase tracking-wide text-slate-600">
                    <th className="py-2 px-3">Entry / Exit Bar (IST)</th>
                    <th className="py-2 px-3">Book</th>
                    <th className="py-2 px-3">Package Legs</th>
                    <th className="py-2 px-3">State</th>
                    <th className="py-2 px-3 text-right">Payoff Bound</th>
                    <th className="py-2 px-3 text-right">Margin / Provenance</th>
                    <th className="py-2 px-3 text-right">Est. Fees</th>
                    <th className="py-2 px-3 text-right">Realized Net</th>
                    <th className="py-2 px-3">Exit / Unwind Reason</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 tabular-nums">
                  {shadowPackages.map((pkg) => (
                    <tr key={pkg.package_id} className="hover:bg-slate-50">
                      <td className="py-2 px-3 whitespace-nowrap text-slate-600">
                        <div>{formatTimestampLabel(pkg.entry_bar_ts)}</div>
                        {pkg.exit_bar_ts && (
                          <div className="text-[10px] text-slate-400">
                            Exit: {formatTimestampLabel(pkg.exit_bar_ts)}
                          </div>
                        )}
                      </td>
                      <td className="py-2 px-3 font-semibold text-slate-800">{pkg.book_id}</td>
                      <td className="py-2 px-3 text-slate-900">
                        {pkg.legs_summary || UNAVAILABLE_LABEL}
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap">
                        <span
                          className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${
                            pkg.state === 'INCOMPLETE_LEG_UNWOUND'
                              ? 'bg-amber-50 text-amber-800 border-amber-300'
                              : pkg.state === 'OPEN'
                              ? 'bg-indigo-50 text-indigo-800 border-indigo-200'
                              : 'bg-slate-100 text-slate-700 border-slate-300'
                          }`}
                        >
                          {pkg.state}
                        </span>
                      </td>
                      <td className="py-2 px-3 text-right">
                        {formatINRValue(pkg.structural_max_loss_inr, { decimals: 0 })}
                      </td>
                      <td className="py-2 px-3 text-right">
                        <div>
                          {hasPositiveNumber(pkg.upstox_final_margin_inr)
                            ? formatINRValue(pkg.upstox_final_margin_inr, { decimals: 0 })
                            : formatINRValue(pkg.cash_reserved_inr, { decimals: 0 })}
                        </div>
                        <div className="text-[10px] text-slate-400">
                          {pkg.margin_status || UNAVAILABLE_LABEL}
                        </div>
                      </td>
                      <td className="py-2 px-3 text-right text-slate-600">
                        {pkg.state === 'OPEN' ? 'Open' : formatINRValue(pkg.total_charges_inr)}
                      </td>
                      <td
                        className={`py-2 px-3 text-right font-semibold ${
                          pkg.state === 'OPEN'
                            ? 'text-slate-500'
                            : Number(pkg.net_pnl_inr) > 0
                            ? 'text-emerald-700'
                            : Number(pkg.net_pnl_inr) < 0
                            ? 'text-rose-700'
                            : 'text-slate-800'
                        }`}
                      >
                        {pkg.state === 'OPEN' ? 'Open' : formatSignedINR(pkg.net_pnl_inr)}
                      </td>
                      <td className="py-2 px-3 font-sans text-slate-600">
                        {pkg.exit_reason ||
                          (pkg.state === 'OPEN' ? 'Active monitoring' : UNAVAILABLE_LABEL)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {/* SECTION 3: Isolated OOS Paper Comparator (V6B_ML_LEGACY_COMPARATOR — Retired 4-Engine + ML) */}
      <section className="bg-white rounded-xl border border-amber-200 shadow-2xs overflow-hidden">
        <div className="p-4 sm:p-5 border-b border-amber-100 bg-amber-50/20 flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-100 text-amber-950 border border-amber-300">
                V6B_ML_COMPARATOR
              </span>
              <h2 className="text-sm font-bold text-slate-900">
                3. OOS Paper Comparator Positions (<code>V6B_ML_LEGACY_COMPARATOR</code> —{' '}
                {openV6bTrades.length} Open / {closedV6bTrades.length} Closed)
              </h2>
            </div>
            <p className="text-xs text-slate-600 mt-0.5">
              Head-to-head paper benchmark for retired V6B 4-engine + 30-feature ML rules. Hard
              airgapped (<code>SHADOW_TELEMETRY_AIRGAP_SKIP</code>); never mirrored to live broker
              or summed with <code>S205_SHADOW_FORWARD_V1</code>.
            </p>
          </div>
          <div className="flex items-center gap-3 text-xs font-mono">
            {v6bCohortSummary && (
              <span className="px-2.5 py-1 rounded bg-white border border-amber-200 text-slate-800 font-semibold">
                Realized: {formatSignedINR(v6bCohortSummary.realized_net_pnl_inr)}
              </span>
            )}
            <span className="px-2 py-0.5 rounded bg-amber-100 text-amber-950 border border-amber-300 text-[10px] font-bold">
              UNHEDGED_SINGLE_LEG_PAPER_COMPARATOR (NOT LIVE ELIGIBLE)
            </span>
          </div>
        </div>

        <div className="p-4 sm:p-5 space-y-4">
          <h3 className="text-xs font-bold uppercase tracking-wide text-slate-600">
            Open V6B + ML Comparator Positions ({openV6bTrades.length})
          </h3>

          {openV6bTrades.length === 0 ? (
            <div className="p-4 rounded-lg bg-slate-50 border border-slate-200 text-xs font-mono text-slate-500">
              No open V6B_ML_LEGACY_COMPARATOR paper positions.
            </div>
          ) : (
            <div className="overflow-x-auto border border-slate-200 rounded-lg">
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-200 text-[11px] font-sans font-semibold uppercase tracking-wide text-slate-600">
                    <th className="py-2.5 px-2 w-8" aria-label="Expand leg" />
                    <th className="py-2.5 px-3">Engine / Contract</th>
                    <th className="py-2.5 px-3 text-right">Qty</th>
                    <th className="py-2.5 px-3 text-right">ML Prob</th>
                    <th className="py-2.5 px-3 text-right">Entry Fill</th>
                    <th className="py-2.5 px-3 text-right">Live Bid / Ask</th>
                    <th className="py-2.5 px-3 text-right">Stop Trigger</th>
                    <th className="py-2.5 px-3 text-right">Payoff Bound</th>
                    <th className="py-2.5 px-3 text-right">Est. Fees</th>
                    <th className="py-2.5 px-3 text-right">Unrealized Net</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 tabular-nums">
                  {openV6bTrades.map((t) => {
                    const isExpanded = Boolean(expandedPackages[t.trade_id]);
                    const rctx = parseRegimeContext(t);
                    const legs = buildPackageLegRows(t);
                    const isShort = t.book_type === 'SHORT_BOOK_SELL';
                    const paidBound =
                      !isShort && hasValidNumber(t.entry_price) && hasValidNumber(t.quantity)
                        ? Number(t.entry_price) * Number(t.quantity)
                        : null;

                    return (
                      <React.Fragment key={t.trade_id}>
                        <tr className="hover:bg-slate-50">
                          <td className="py-2.5 px-2">
                            <button
                              type="button"
                              onClick={() => togglePackage(t.trade_id)}
                              aria-expanded={isExpanded}
                              aria-label={`Toggle leg details for ${t.trade_id}`}
                              className="p-1 rounded hover:bg-slate-200/70 text-slate-600 cursor-pointer"
                            >
                              {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                            </button>
                          </td>
                          <td className="py-2.5 px-3">
                            <div className="font-semibold text-slate-900">
                              {t.trading_symbol || UNAVAILABLE_LABEL}
                            </div>
                            <div className="text-[10px] text-slate-500">
                              {t.strategy_mode} • {formatTimestampLabel(t.entry_time)}
                            </div>
                            {isShort && (
                              <div className="text-[10px] font-semibold text-amber-900 mt-0.5">
                                UNHEDGED_SINGLE_LEG_PAPER_COMPARATOR (NOT LIVE ELIGIBLE)
                              </div>
                            )}
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            {isShort ? `-${t.quantity ?? 65}` : `+${t.quantity ?? 65}`}
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            {hasValidNumber(rctx.ml_prob)
                              ? `${formatNumber(rctx.ml_prob)} / 0.56`
                              : UNAVAILABLE_LABEL}
                          </td>
                          <td className="py-2.5 px-3 text-right">{formatINRValue(t.entry_price)}</td>
                          <td className="py-2.5 px-3 text-right whitespace-nowrap">
                            <div>
                              {hasValidNumber(t.current_bid) && hasValidNumber(t.current_ask)
                                ? `${formatINRValue(t.current_bid)} / ${formatINRValue(t.current_ask)}`
                                : UNAVAILABLE_LABEL}
                            </div>
                            <div className="text-[10px] text-slate-500">
                              Quote: {formatQuoteFreshness(t)}
                            </div>
                          </td>
                          <td className="py-2.5 px-3 text-right text-amber-800 font-semibold">
                            {formatINRValue(t.effective_stop_price)}
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            {isShort ? (
                              <span className="text-rose-800 font-semibold text-[11px]">
                                Unbounded (No Wing)
                              </span>
                            ) : (
                              formatINRValue(paidBound, { decimals: 0 })
                            )}
                          </td>
                          <td className="py-2.5 px-3 text-right text-slate-600">
                            {formatINRValue(t.unrealized_charges)}
                          </td>
                          <td
                            className={`py-2.5 px-3 text-right font-bold ${
                              !hasValidNumber(t.unrealized_net_pnl)
                                ? 'text-slate-400'
                                : Number(t.unrealized_net_pnl) >= 0
                                ? 'text-emerald-700'
                                : 'text-rose-700'
                            }`}
                          >
                            {formatSignedINR(t.unrealized_net_pnl)}
                          </td>
                        </tr>

                        {isExpanded && (
                          <tr className="bg-slate-50/90">
                            <td colSpan={10} className="p-3.5 border-t border-slate-100">
                              <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                                {legs.map((leg) => (
                                  <div
                                    key={leg.legId}
                                    className="bg-white rounded-lg border border-slate-200 p-2.5 text-[11px] font-mono space-y-1"
                                  >
                                    <div className="flex items-center justify-between font-bold text-slate-900">
                                      <span>{leg.role}</span>
                                      <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700">
                                        {leg.side} {leg.quantity}
                                      </span>
                                    </div>
                                    <div>
                                      Contract: <strong>{leg.contract}</strong> (Exp {leg.expiry})
                                    </div>
                                    <div>Simulated Entry Fill: {leg.entryPrice}</div>
                                    <div>Current Quote: {leg.liveOrExitQuote}</div>
                                    <div className="text-amber-900 font-semibold">
                                      Hedge: {leg.hedgeState}
                                    </div>
                                  </div>
                                ))}
                              </div>
                            </td>
                          </tr>
                        )}
                      </React.Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <h3 className="text-xs font-bold uppercase tracking-wide text-slate-600 pt-1">
            Closed V6B + ML Comparator OOS Trades ({closedV6bTrades.length})
          </h3>

          {closedV6bTrades.length === 0 ? (
            <div className="p-4 rounded-lg bg-slate-50 border border-slate-200 text-xs font-mono text-slate-500">
              No closed V6B_ML_LEGACY_COMPARATOR OOS trades recorded yet since 2026-11-15.
            </div>
          ) : (
            <div className="overflow-x-auto border border-slate-200 rounded-lg">
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-200 text-[11px] font-sans font-semibold uppercase tracking-wide text-slate-600">
                    <th className="py-2 px-3">Entry / Exit (IST)</th>
                    <th className="py-2 px-3">Engine</th>
                    <th className="py-2 px-3">Contract &amp; Risk Structure</th>
                    <th className="py-2 px-3 text-right">ML Prob</th>
                    <th className="py-2 px-3 text-right">Entry &rarr; Exit</th>
                    <th className="py-2 px-3 text-right">Est. Fees</th>
                    <th className="py-2 px-3 text-right">Realized Net</th>
                    <th className="py-2 px-3">Exit Reason</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 tabular-nums">
                  {closedV6bTrades.slice(0, 60).map((t) => {
                    const rctx = parseRegimeContext(t);
                    const isShort = t.book_type === 'SHORT_BOOK_SELL';
                    return (
                      <tr key={t.trade_id} className="hover:bg-slate-50">
                        <td className="py-2 px-3 whitespace-nowrap text-slate-600">
                          <div>{formatTimestampLabel(t.entry_time)}</div>
                          {t.exit_time && (
                            <div className="text-[10px] text-slate-400">
                              Exit: {formatTimestampLabel(t.exit_time)}
                            </div>
                          )}
                        </td>
                        <td className="py-2 px-3 whitespace-nowrap font-semibold text-slate-800">
                          {t.strategy_mode}
                        </td>
                        <td className="py-2 px-3 text-slate-900">
                          <div className="font-semibold">{t.trading_symbol}</div>
                          {isShort && (
                            <div className="text-[10px] text-amber-900 font-semibold">
                              UNHEDGED_SINGLE_LEG_PAPER_COMPARATOR (NOT LIVE ELIGIBLE)
                            </div>
                          )}
                        </td>
                        <td className="py-2 px-3 text-right whitespace-nowrap">
                          {hasValidNumber(rctx.ml_prob)
                            ? `${formatNumber(rctx.ml_prob)} / 0.56`
                            : '—'}
                        </td>
                        <td className="py-2 px-3 text-right whitespace-nowrap">
                          {formatINRValue(t.entry_price)} &rarr; {formatINRValue(t.exit_price)}
                        </td>
                        <td className="py-2 px-3 text-right text-slate-600">
                          {formatINRValue(t.charges)}
                        </td>
                        <td
                          className={`py-2 px-3 text-right font-semibold ${
                            Number(t.net_pnl) > 0
                              ? 'text-emerald-700'
                              : Number(t.net_pnl) < 0
                              ? 'text-rose-700'
                              : 'text-slate-800'
                          }`}
                        >
                          {formatSignedINR(t.net_pnl)}
                        </td>
                        <td className="py-2 px-3 font-sans text-slate-600">
                          {t.exit_reason || t.status}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {/* SECTION 4: Separated Pre-Registration Historical Paper Archive (HISTORICAL_LEGACY_V2) */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs overflow-hidden">
        <button
          type="button"
          onClick={() => setShowHistoricalArchive((v) => !v)}
          aria-expanded={showHistoricalArchive}
          className="w-full p-4 sm:p-5 flex items-center justify-between text-left hover:bg-slate-50 transition cursor-pointer"
        >
          <div>
            <h2 className="text-sm font-bold text-slate-800">
              4. Pre-S205 Historical Paper Archive (<code>HISTORICAL_LEGACY_V2</code> —{' '}
              {openHistoricalTrades.length} Open / {closedHistoricalTrades.length} Closed)
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Archived paper rows recorded prior to 2026-11-15 kept strictly excluded from both
              active forward OOS cohorts.
            </p>
          </div>
          <span className="text-slate-500">
            {showHistoricalArchive ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          </span>
        </button>

        {showHistoricalArchive && (
          <div className="border-t border-slate-100 p-4 overflow-x-auto">
            {closedHistoricalTrades.length === 0 && openHistoricalTrades.length === 0 ? (
              <div className="text-xs font-mono text-slate-500">
                No archived pre-2026-11-15 paper rows present.
              </div>
            ) : (
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-200 text-[11px] font-sans font-semibold uppercase tracking-wide text-slate-600">
                    <th className="py-2 px-3">Entry / Exit (IST)</th>
                    <th className="py-2 px-3">Strategy</th>
                    <th className="py-2 px-3">Contract</th>
                    <th className="py-2 px-3 text-right">Qty</th>
                    <th className="py-2 px-3 text-right">Entry &rarr; Exit</th>
                    <th className="py-2 px-3 text-right">Est. Fees</th>
                    <th className="py-2 px-3 text-right">Net P&amp;L</th>
                    <th className="py-2 px-3">Exit Reason</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 tabular-nums">
                  {[...openHistoricalTrades, ...closedHistoricalTrades].slice(0, 60).map((t) => (
                    <tr key={t.trade_id} className="hover:bg-slate-50">
                      <td className="py-2 px-3 whitespace-nowrap text-slate-600">
                        {formatTimestampLabel(t.entry_time)}
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap">{t.strategy_mode}</td>
                      <td className="py-2 px-3 font-semibold text-slate-800">{t.trading_symbol}</td>
                      <td className="py-2 px-3 text-right">{t.quantity ?? UNAVAILABLE_LABEL}</td>
                      <td className="py-2 px-3 text-right whitespace-nowrap">
                        {formatINRValue(t.entry_price)} &rarr; {formatINRValue(t.exit_price)}
                      </td>
                      <td className="py-2 px-3 text-right text-slate-600">
                        {formatINRValue(t.charges)}
                      </td>
                      <td
                        className={`py-2 px-3 text-right font-semibold ${
                          Number(t.net_pnl) >= 0 ? 'text-emerald-700' : 'text-rose-700'
                        }`}
                      >
                        {formatSignedINR(t.net_pnl)}
                      </td>
                      <td className="py-2 px-3 font-sans text-slate-600">
                        {t.exit_reason || t.status}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
