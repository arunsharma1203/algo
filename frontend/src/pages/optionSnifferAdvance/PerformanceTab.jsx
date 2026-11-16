import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Calendar as CalendarIcon, ChevronLeft, ChevronRight, GitCompare, Info, X } from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  computeScopedPerformance,
  formatClockIST,
  formatINRValue,
  formatNumber,
  formatPercent,
  formatSignedINR,
  formatTimestampLabel,
  hasValidNumber,
} from './formatters';

const SOURCE_OPTIONS = [
  {
    id: 'SHADOW_FORWARD_V1',
    label: 'Forward Paper • All (S205_SHADOW_FORWARD_V1)',
  },
  {
    id: 'PAPER_BOOK_B',
    label: 'Forward Paper • Book B Only (Paid Flow)',
  },
  {
    id: 'PAPER_BOOK_A',
    label: 'Forward Paper • Book A Only (150pt Spread)',
  },
  {
    id: 'V6B_ML_LEGACY_COMPARATOR',
    label: 'OOS Paper Comparator • Retired V6B + 30-Feature ML',
  },
  {
    id: 'HISTORICAL_LEGACY_V2',
    label: 'Historical Archive • Pre-S205 Paper (<= 2026-11-14)',
  },
  {
    id: 'LIVE_BROKER_RECENT',
    label: 'Broker Recent Intents (Limited ≤25 Sample)',
  },
];

function formatMonthLabel(ym) {
  if (!ym || !ym.includes('-')) return ym || UNAVAILABLE_LABEL;
  const [y, m] = ym.split('-');
  const dt = new Date(Number(y), Number(m) - 1, 1);
  return dt.toLocaleDateString('en-IN', { month: 'long', year: 'numeric' });
}

export default function PerformanceTab({
  tradesData,
  shadowTelemetry,
  liveStatus,
  cohortComparison = null,
  calendarsByEra = null,
  sliceMeta,
  defaultCurrentMonth,
}) {
  const [sourceFilter, setSourceFilter] = useState('SHADOW_FORWARD_V1');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [selectedMonth, setSelectedMonth] = useState(defaultCurrentMonth);
  const [selectedDayDate, setSelectedDayDate] = useState(null);
  const closeBtnRef = useRef(null);

  const sourceOptions = useMemo(
    () =>
      SOURCE_OPTIONS.map((opt) =>
        opt.id === 'LIVE_BROKER_RECENT' && calendarsByEra?.LIVE_BROKER
          ? { ...opt, label: 'Broker Realized Ledger (Full SQLite)' }
          : opt
      ),
    [calendarsByEra]
  );

  const scoped = useMemo(
    () =>
      computeScopedPerformance({
        closedTrades: tradesData?.closed_trades || [],
        shadowPackages: shadowTelemetry?.recent_packages || [],
        liveIntents: liveStatus?.recent_order_intents || [],
        calendarsByEra,
        sourceFilter,
        fromDate,
        toDate,
        selectedMonth,
        defaultMonth: defaultCurrentMonth,
      }),
    [
      tradesData,
      shadowTelemetry,
      liveStatus,
      calendarsByEra,
      sourceFilter,
      fromDate,
      toDate,
      selectedMonth,
      defaultCurrentMonth,
    ]
  );

  const activeDay = selectedDayDate ? scoped.dayMap[selectedDayDate] || null : null;

  useEffect(() => {
    if (!activeDay) return undefined;
    closeBtnRef.current?.focus();
    const onKeyDown = (e) => {
      if (e.key === 'Escape') {
        setSelectedDayDate(null);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [activeDay]);

  const sum = scoped.monthSummary;
  const shCohort = cohortComparison?.S205_SHADOW_FORWARD_V1 || null;
  const v6bCohort = cohortComparison?.V6B_ML_LEGACY_COMPARATOR || null;

  return (
    <div className="space-y-5">
      {/* Side-by-Side Head-to-Head OOS Cohort Comparison Strip (Never Combined) */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <GitCompare size={16} className="text-indigo-700" />
              <span>
                Side-by-Side Matched-Window OOS Scoreboard (<code>S205_SHADOW_FORWARD_V1</code> vs.{' '}
                <code>V6B_ML_LEGACY_COMPARATOR</code>)
              </span>
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Cumulative post-registration (&ge; {cohortComparison?.comparison_start_date || '2026-11-15'})
              realized comparison. Select either cohort card to load its isolated day-by-day calendar below.
            </p>
          </div>
          <span className="text-[11px] font-mono text-slate-500">
            Zero Cross-Cohort Aggregation
          </span>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 font-mono tabular-nums">
          <button
            type="button"
            onClick={() => {
              setSourceFilter('SHADOW_FORWARD_V1');
              setSelectedDayDate(null);
            }}
            className={`text-left rounded-xl border p-4 transition cursor-pointer ${
              sourceFilter === 'SHADOW_FORWARD_V1'
                ? 'border-indigo-400 bg-indigo-50/40 ring-1 ring-indigo-300'
                : 'border-slate-200 bg-slate-50/40 hover:bg-slate-50'
            }`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2 font-sans">
              <div className="flex items-center gap-2">
                <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-indigo-100 text-indigo-900 border border-indigo-200">
                  SHADOW_V1
                </span>
                <span className="text-xs font-bold text-slate-900">
                  Current 2-Book L5 Architecture
                </span>
              </div>
              <span className="text-[11px] font-mono text-slate-500">
                {shCohort ? `${shCohort.closed_trades_count} closed` : UNAVAILABLE_LABEL}
              </span>
            </div>
            <div className="grid grid-cols-3 gap-2 mt-3 text-xs">
              <div>
                <div className="text-[10px] font-sans text-slate-500">Realized Net</div>
                <div
                  className={`text-base font-bold ${
                    !hasValidNumber(shCohort?.realized_net_pnl_inr)
                      ? 'text-slate-400'
                      : Number(shCohort.realized_net_pnl_inr) > 0
                      ? 'text-emerald-700'
                      : Number(shCohort.realized_net_pnl_inr) < 0
                      ? 'text-rose-700'
                      : 'text-slate-900'
                  }`}
                >
                  {formatSignedINR(shCohort?.realized_net_pnl_inr)}
                </div>
              </div>
              <div>
                <div className="text-[10px] font-sans text-slate-500">Win Rate / PF</div>
                <div className="text-sm font-bold text-slate-900">
                  {hasValidNumber(shCohort?.win_rate_pct)
                    ? formatPercent(shCohort.win_rate_pct)
                    : '—'}{' '}
                  /{' '}
                  {shCohort?.no_loss_wins_only
                    ? 'No loss'
                    : hasValidNumber(shCohort?.profit_factor)
                    ? `${formatNumber(shCohort.profit_factor)}x`
                    : '—'}
                </div>
              </div>
              <div>
                <div className="text-[10px] font-sans text-slate-500">Est. Fees</div>
                <div className="text-sm font-bold text-slate-800">
                  {formatINRValue(shCohort?.total_charges_inr)}
                </div>
              </div>
            </div>
          </button>

          <button
            type="button"
            onClick={() => {
              setSourceFilter('V6B_ML_LEGACY_COMPARATOR');
              setSelectedDayDate(null);
            }}
            className={`text-left rounded-xl border p-4 transition cursor-pointer ${
              sourceFilter === 'V6B_ML_LEGACY_COMPARATOR'
                ? 'border-amber-400 bg-amber-50/40 ring-1 ring-amber-300'
                : 'border-slate-200 bg-slate-50/40 hover:bg-slate-50'
            }`}
          >
            <div className="flex flex-wrap items-center justify-between gap-2 font-sans">
              <div className="flex items-center gap-2">
                <span className="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-100 text-amber-950 border border-amber-300">
                  V6B_ML_COMPARATOR
                </span>
                <span className="text-xs font-bold text-slate-900">
                  Retired 4-Engine + 30-Feature ML
                </span>
              </div>
              <span className="text-[10px] font-mono font-semibold text-amber-900">
                NOT LIVE ELIGIBLE •{' '}
                {v6bCohort ? `${v6bCohort.closed_trades_count} closed` : UNAVAILABLE_LABEL}
              </span>
            </div>
            <div className="grid grid-cols-3 gap-2 mt-3 text-xs">
              <div>
                <div className="text-[10px] font-sans text-slate-500">Realized Net</div>
                <div
                  className={`text-base font-bold ${
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
              <div>
                <div className="text-[10px] font-sans text-slate-500">Win Rate / PF</div>
                <div className="text-sm font-bold text-slate-900">
                  {hasValidNumber(v6bCohort?.win_rate_pct)
                    ? formatPercent(v6bCohort.win_rate_pct)
                    : '—'}{' '}
                  /{' '}
                  {v6bCohort?.no_loss_wins_only
                    ? 'No loss'
                    : hasValidNumber(v6bCohort?.profit_factor)
                    ? `${formatNumber(v6bCohort.profit_factor)}x`
                    : '—'}
                </div>
              </div>
              <div>
                <div className="text-[10px] font-sans text-slate-500">Est. Fees</div>
                <div className="text-sm font-bold text-slate-800">
                  {formatINRValue(v6bCohort?.total_charges_inr)}
                </div>
              </div>
            </div>
          </button>
        </div>
      </section>

      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5 space-y-4">
        {/* Top Controls: Explicit Source & Date Range Selectors */}
        <div className="flex flex-col xl:flex-row xl:items-end xl:justify-between gap-4 pb-4 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <CalendarIcon size={16} className="text-slate-700" />
              <span>Source-Isolated Realized Performance &amp; Calendar</span>
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Real broker, active forward paper, and legacy historical paper ledgers are strictly
              isolated and never combined into one headline.
            </p>
          </div>

          <div className="flex flex-wrap items-end gap-3 text-xs">
            <div>
              <label
                htmlFor="perf-source-select"
                className="block text-[11px] font-semibold text-slate-600 mb-1"
              >
                Ledger Source (Required)
              </label>
              <select
                id="perf-source-select"
                value={sourceFilter}
                onChange={(e) => {
                  setSourceFilter(e.target.value);
                  setSelectedDayDate(null);
                }}
                className="px-3 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-900 font-medium text-xs"
              >
                {sourceOptions.map((opt) => (
                  <option key={opt.id} value={opt.id}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label
                htmlFor="perf-from-date"
                className="block text-[11px] font-semibold text-slate-600 mb-1"
              >
                From Date
              </label>
              <input
                id="perf-from-date"
                type="date"
                value={fromDate}
                onChange={(e) => setFromDate(e.target.value)}
                className="px-2.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-800 font-mono text-xs"
              />
            </div>

            <div>
              <label
                htmlFor="perf-to-date"
                className="block text-[11px] font-semibold text-slate-600 mb-1"
              >
                To Date
              </label>
              <input
                id="perf-to-date"
                type="date"
                value={toDate}
                onChange={(e) => setToDate(e.target.value)}
                className="px-2.5 py-1.5 rounded-lg bg-white border border-slate-300 text-slate-800 font-mono text-xs"
              />
            </div>

            {(fromDate || toDate) && (
              <button
                type="button"
                onClick={() => {
                  setFromDate('');
                  setToDate('');
                }}
                className="px-2.5 py-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold cursor-pointer"
              >
                Clear Dates
              </button>
            )}

            {/* Month Switcher */}
            <div className="flex items-center gap-1 bg-slate-100 p-1 rounded-lg border border-slate-200">
              <button
                type="button"
                onClick={() => {
                  const idx = scoped.availableMonths.indexOf(scoped.activeMonth);
                  if (idx < scoped.availableMonths.length - 1) {
                    setSelectedMonth(scoped.availableMonths[idx + 1]);
                  }
                }}
                disabled={
                  scoped.availableMonths.indexOf(scoped.activeMonth) >=
                  scoped.availableMonths.length - 1
                }
                aria-label="Previous month"
                className="p-1 rounded text-slate-600 hover:bg-white disabled:opacity-40 cursor-pointer"
              >
                <ChevronLeft size={14} />
              </button>
              <select
                aria-label="Select month"
                value={scoped.activeMonth}
                onChange={(e) => setSelectedMonth(e.target.value)}
                className="px-2 py-1 bg-white rounded text-xs font-semibold font-mono text-slate-900 border border-slate-200"
              >
                {scoped.availableMonths.map((m) => (
                  <option key={m} value={m}>
                    {formatMonthLabel(m)}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => {
                  const idx = scoped.availableMonths.indexOf(scoped.activeMonth);
                  if (idx > 0) {
                    setSelectedMonth(scoped.availableMonths[idx - 1]);
                  }
                }}
                disabled={scoped.availableMonths.indexOf(scoped.activeMonth) <= 0}
                aria-label="Next month"
                className="p-1 rounded text-slate-600 hover:bg-white disabled:opacity-40 cursor-pointer"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        </div>

        {/* Scope & Fee Provenance Disclosure */}
        <div className="rounded-lg bg-slate-50 border border-slate-200 px-3.5 py-2.5 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-700">
          <div className="flex items-start gap-2">
            <Info size={14} className="text-slate-500 shrink-0 mt-0.5" />
            <span>{scoped.scopeLimitations}</span>
          </div>
          <div className="font-mono text-[11px] text-slate-500 shrink-0">
            Fees: <strong>{scoped.feeLabel}</strong> • Retrieved:{' '}
            {sliceMeta?.trades?.lastSuccessMs
              ? formatClockIST(sliceMeta.trades.lastSuccessMs)
              : UNAVAILABLE_LABEL}
          </div>
        </div>

        {/* 4 Scoped Month Summary Cards (Zero Hardcoded Fallbacks) */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <div className="bg-slate-50/70 rounded-xl p-3.5 border border-slate-200">
            <div className="flex items-center justify-between text-[11px] font-semibold text-slate-600">
              <span>{formatMonthLabel(scoped.activeMonth)} Net P&amp;L</span>
              <span className="font-mono text-slate-500">
                PF:{' '}
                {sum.no_loss_wins_only
                  ? 'No losses'
                  : hasValidNumber(sum.profit_factor)
                  ? `${formatNumber(sum.profit_factor)}x`
                  : UNAVAILABLE_LABEL}
              </span>
            </div>
            <div
              className={`text-xl font-bold font-mono mt-1 tabular-nums ${
                sum.total_net_pnl > 0
                  ? 'text-emerald-700'
                  : sum.total_net_pnl < 0
                  ? 'text-rose-700'
                  : 'text-slate-900'
              }`}
            >
              {formatSignedINR(sum.total_net_pnl)}
            </div>
            <div className="text-[11px] font-mono text-slate-600 mt-1 flex justify-between tabular-nums">
              <span>Gross: {formatSignedINR(sum.total_gross_pnl)}</span>
              <span>Fees: {formatINRValue(sum.total_charges)}</span>
            </div>
          </div>

          <div className="bg-slate-50/70 rounded-xl p-3.5 border border-slate-200">
            <div className="flex items-center justify-between text-[11px] font-semibold text-slate-600">
              <span>Active Trading Days</span>
              <span className="font-mono text-slate-500">{sum.trading_days} days</span>
            </div>
            <div className="text-xl font-bold font-mono text-slate-900 mt-1 tabular-nums">
              <span className="text-emerald-700">{sum.green_days} Pos</span>
              <span className="text-slate-400"> / </span>
              <span className="text-rose-700">{sum.red_days} Neg</span>
            </div>
            <div className="text-[11px] font-mono text-slate-600 mt-1">
              Filtered records in scope: {scoped.filteredTotalCount}
            </div>
          </div>

          <div className="bg-slate-50/70 rounded-xl p-3.5 border border-slate-200">
            <div className="flex items-center justify-between text-[11px] font-semibold text-slate-600">
              <span>Closed Trade Win Rate</span>
              <span className="font-mono text-slate-500">{sum.total_trades} closed</span>
            </div>
            <div className="text-xl font-bold font-mono text-slate-900 mt-1 tabular-nums">
              {hasValidNumber(sum.win_rate) ? formatPercent(sum.win_rate) : UNAVAILABLE_LABEL}
            </div>
            <div className="text-[11px] font-mono text-slate-600 mt-1 tabular-nums">
              {sum.wins}W / {sum.losses}L in {formatMonthLabel(scoped.activeMonth)}
            </div>
          </div>

          <div className="bg-slate-50/70 rounded-xl p-3.5 border border-slate-200">
            <div className="flex items-center justify-between text-[11px] font-semibold text-slate-600">
              <span>Long vs. Spread/Short Split</span>
              <span className="font-mono text-slate-500">
                {sum.long_book_trades}L / {sum.short_book_trades}S
              </span>
            </div>
            <div className="text-xs font-mono mt-1.5 space-y-1 tabular-nums">
              <div className="flex justify-between">
                <span className="text-slate-600">Long Buying:</span>
                <span className="font-semibold text-slate-900">
                  {formatSignedINR(sum.long_book_net_pnl)}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-600">Spread / Short:</span>
                <span className="font-semibold text-slate-900">
                  {formatSignedINR(sum.short_book_net_pnl)}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* 5-Column Weekday Calendar */}
        <div className="hidden sm:grid grid-cols-5 gap-2 text-center text-[11px] font-semibold uppercase tracking-wide text-slate-500 pt-1">
          <div>Monday</div>
          <div>Tuesday</div>
          <div>Wednesday</div>
          <div>Thursday</div>
          <div>Friday</div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
          {scoped.weekdayCells.map((d) => {
            if (d.isPadding) {
              return (
                <div
                  key={d.key}
                  className="hidden sm:block rounded-xl border border-dashed border-slate-200 bg-slate-50/30 p-3 min-h-[96px]"
                />
              );
            }
            const hasTrades = d.trade_count > 0;
            const net = d.net_pnl_rupees;
            const dtObj = new Date(`${d.date}T00:00:00`);
            const dayLabel = dtObj.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });

            const cellTone = !hasTrades
              ? 'bg-slate-50/50 border-slate-200 text-slate-400'
              : net > 0
              ? 'bg-emerald-50/60 border-emerald-300 text-slate-900 hover:bg-emerald-50'
              : net < 0
              ? 'bg-rose-50/60 border-rose-300 text-slate-900 hover:bg-rose-50'
              : 'bg-slate-100 border-slate-300 text-slate-900';

            return (
              <button
                key={d.date}
                type="button"
                disabled={!hasTrades}
                onClick={() => hasTrades && setSelectedDayDate(d.date)}
                className={`text-left rounded-xl border p-2.5 min-h-[96px] flex flex-col justify-between transition ${cellTone} ${
                  hasTrades ? 'cursor-pointer' : 'cursor-default'
                }`}
              >
                <div className="flex items-center justify-between gap-1 w-full text-xs">
                  <span className="font-semibold text-slate-700">{dayLabel}</span>
                  <span className="text-[10px] font-mono text-slate-500">
                    {hasTrades ? `${d.trade_count} trd` : '—'}
                  </span>
                </div>

                {hasTrades ? (
                  <div className="mt-2 space-y-0.5 font-mono tabular-nums w-full">
                    <div
                      className={`text-sm font-bold ${
                        net > 0
                          ? 'text-emerald-700'
                          : net < 0
                          ? 'text-rose-700'
                          : 'text-slate-800'
                      }`}
                    >
                      {formatSignedINR(net, { decimals: 0 })}
                    </div>
                    <div className="flex justify-between text-[10px] text-slate-600">
                      <span>{d.win_rate}% WR</span>
                      <span>
                        {d.wins}W/{d.losses}L
                      </span>
                    </div>
                  </div>
                ) : (
                  <div className="text-[10px] text-slate-400 mt-2">0 closed</div>
                )}
              </button>
            );
          })}
        </div>
      </section>

      {/* Accessible Day Drill-Down Dialog */}
      {activeDay && (
        <div
          className="fixed inset-0 z-50 bg-slate-900/60 flex items-center justify-center p-4"
          onClick={() => setSelectedDayDate(null)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="day-drilldown-title"
            className="bg-white rounded-2xl border border-slate-200 shadow-xl max-w-4xl w-full max-h-[88vh] overflow-hidden flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="p-4 sm:p-5 border-b border-slate-200 flex items-center justify-between bg-slate-50">
              <div>
                <h3 id="day-drilldown-title" className="text-sm font-bold text-slate-900">
                  Realized Day Ledger — {activeDay.date} ({formatSignedINR(activeDay.net_pnl_rupees)})
                </h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  Scope: {SOURCE_OPTIONS.find((s) => s.id === sourceFilter)?.label} • Fees:{' '}
                  {scoped.feeLabel}
                </p>
              </div>
              <button
                ref={closeBtnRef}
                type="button"
                onClick={() => setSelectedDayDate(null)}
                aria-label="Close day performance dialog"
                className="p-1.5 rounded-lg bg-white border border-slate-300 text-slate-600 hover:text-slate-900 cursor-pointer"
              >
                <X size={16} />
              </button>
            </div>

            <div className="p-4 sm:p-5 overflow-y-auto">
              <div className="overflow-x-auto border border-slate-200 rounded-lg">
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
                    {activeDay.trades.map((t, idx) => (
                      <tr key={t.trade_id || idx} className="hover:bg-slate-50">
                        <td className="py-2 px-3 whitespace-nowrap text-slate-600">
                          <div>{formatTimestampLabel(t.entry_time)}</div>
                          <div className="text-[10px] text-slate-400">
                            &rarr; {formatTimestampLabel(t.exit_time)}
                          </div>
                        </td>
                        <td className="py-2 px-3 whitespace-nowrap">{t.strategy_mode}</td>
                        <td className="py-2 px-3 font-semibold text-slate-900">
                          <div>{t.trading_symbol}</div>
                          {t.spread_legs_label && (
                            <div className="text-[10px] text-indigo-700">{t.spread_legs_label}</div>
                          )}
                        </td>
                        <td className="py-2 px-3 text-right">
                          {hasValidNumber(t.quantity) ? t.quantity : UNAVAILABLE_LABEL}
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
                          {t.exit_reason || UNAVAILABLE_LABEL}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
