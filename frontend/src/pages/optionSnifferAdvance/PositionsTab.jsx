import React, { useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  buildPackageLegRows,
  classifyStrategyEra,
  formatClockIST,
  formatINRValue,
  formatSignedINR,
  formatTimestampLabel,
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

export default function PositionsTab({
  executionStatus,
  liveStatus,
  tradesData,
  shadowTelemetry,
  sliceMeta,
}) {
  const [expandedPackages, setExpandedPackages] = useState({});
  const [showLegacyClosed, setShowLegacyClosed] = useState(false);

  const togglePackage = (id) => {
    setExpandedPackages((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const brokerPositions = Array.isArray(liveStatus?.active_live_positions)
    ? liveStatus.active_live_positions
    : [];
  const brokerIntents = Array.isArray(liveStatus?.recent_order_intents)
    ? liveStatus.recent_order_intents
    : [];

  const allOpenTrades = Array.isArray(tradesData?.open_trades) ? tradesData.open_trades : [];
  const allClosedTrades = Array.isArray(tradesData?.closed_trades) ? tradesData.closed_trades : [];

  const openForwardTrades = allOpenTrades.filter(
    (t) => classifyStrategyEra(t.strategy_mode, parseRegimeContext(t)) === 'SHADOW_FORWARD_V1'
  );
  const openLegacyTrades = allOpenTrades.filter(
    (t) => classifyStrategyEra(t.strategy_mode, parseRegimeContext(t)) !== 'SHADOW_FORWARD_V1'
  );
  const closedLegacyTrades = allClosedTrades.filter(
    (t) => classifyStrategyEra(t.strategy_mode, parseRegimeContext(t)) !== 'SHADOW_FORWARD_V1'
  );

  const shadowPackages = Array.isArray(shadowTelemetry?.recent_packages)
    ? shadowTelemetry.recent_packages
    : [];
  const pendingLeg2Count = shadowTelemetry?.shadow_account_ledger?.pending_leg2_count ?? 0;

  return (
    <div className="space-y-5">
      {/* Risk & Payoff Semantics Notice */}
      <div className="bg-slate-100 border border-slate-200 rounded-xl px-4 py-3 text-xs text-slate-700 leading-relaxed">
        <strong className="text-slate-900">Risk &amp; Quote Disclosure:</strong> Software stop-loss
        levels are order triggers evaluated on observed quotes—not guaranteed maximum losses under
        intrabar gaps or illiquidity. Only long-option paid outlay or a completed long protective
        wing bounds structural expiry payoff. Quote freshness reflects client retrieval time where
        provider tick timestamps are not supplied by the endpoint.
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
              Isolated Upstox broker intents from <code>GET /live/status</code> (limited to 25 most
              recent rows).
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
        ) : brokerPositions.length === 0 && brokerIntents.length === 0 ? (
          <div className="p-5 text-xs font-mono text-slate-500">
            0 active broker positions and 0 recent broker order intents.
          </div>
        ) : (
          <div className="overflow-x-auto">
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
                {brokerIntents.map((intent) => {
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
                      : activeMatch?.unrealized_pnl;

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
                            : Number(netPnl) > 0
                            ? 'text-emerald-700'
                            : Number(netPnl) < 0
                            ? 'text-rose-700'
                            : 'text-slate-800'
                        }`}
                      >
                        {intent.state === 'CANCELLED'
                          ? 'Cancelled (0 fill)'
                          : intent.state === 'REJECTED'
                          ? 'Rejected (0 fill)'
                          : formatSignedINR(netPnl)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* SECTION 2: Forward Paper / Shadow Packages (S205_SHADOW_FORWARD_V1) with Expandable Legs */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs overflow-hidden">
        <div className="p-4 sm:p-5 border-b border-slate-100 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              2. Forward Paper Packages (<code>S205_SHADOW_FORWARD_V1</code> — Open &amp; Recent)
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Expand any open package to inspect individual legs, paid outlay vs. payoff bound vs.
              stop trigger vs. reserved margin.
            </p>
          </div>
          <span className="text-xs font-mono text-slate-500">
            Retrieved:{' '}
            {sliceMeta?.trades?.lastSuccessMs
              ? formatClockIST(sliceMeta.trades.lastSuccessMs)
              : UNAVAILABLE_LABEL}{' '}
            (Quote tick ts: Unavailable)
          </span>
        </div>

        {pendingLeg2Count > 0 && (
          <div className="mx-4 mt-4 p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900 font-mono">
            INTERMEDIATE LEG ALERT: {pendingLeg2Count} buy-first protective wing(s) currently
            awaiting Leg-2 short fill (&le; 3,000ms window before automatic wing unwind).
          </div>
        )}

        {/* 2A: Open Forward Paper Packages */}
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
                            {hasValidNumber(t.current_bid) && hasValidNumber(t.current_ask)
                              ? `${formatINRValue(t.current_bid)} / ${formatINRValue(t.current_ask)}`
                              : UNAVAILABLE_LABEL}
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
                              {isSpread
                                ? rctx.margin_status || 'Margin reserve'
                                : 'Cash outlay'}
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

          {/* 2B: Recent Forward Package Ledger (Includes INCOMPLETE_LEG_UNWOUND & CLOSED) */}
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
                        {pkg.state === 'OPEN'
                          ? 'Open'
                          : formatINRValue(pkg.total_charges_inr)}
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
                        {pkg.exit_reason || (pkg.state === 'OPEN' ? 'Active monitoring' : UNAVAILABLE_LABEL)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {/* SECTION 3: Separated Historical / Legacy Paper Sandbox Ledger */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs overflow-hidden">
        <button
          type="button"
          onClick={() => setShowLegacyClosed((v) => !v)}
          aria-expanded={showLegacyClosed}
          className="w-full p-4 sm:p-5 flex items-center justify-between text-left hover:bg-slate-50 transition cursor-pointer"
        >
          <div>
            <h2 className="text-sm font-bold text-slate-800">
              3. Legacy Retired Paper Sandbox History ({openLegacyTrades.length} Open /{' '}
              {closedLegacyTrades.length} Closed)
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Pre-S205_SHADOW_FORWARD_V1 historical V6B / V2.0 paper trades kept strictly isolated
              from active forward results.
            </p>
          </div>
          <span className="text-slate-500">
            {showLegacyClosed ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          </span>
        </button>

        {showLegacyClosed && (
          <div className="border-t border-slate-100 p-4 overflow-x-auto">
            {closedLegacyTrades.length === 0 && openLegacyTrades.length === 0 ? (
              <div className="text-xs font-mono text-slate-500">
                No legacy paper rows present.
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
                  {[...openLegacyTrades, ...closedLegacyTrades].slice(0, 60).map((t) => (
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
