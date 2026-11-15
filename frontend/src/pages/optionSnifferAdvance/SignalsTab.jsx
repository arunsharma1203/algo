import React, { useState } from 'react';
import { ChevronDown, ChevronRight, AlertTriangle } from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  classifyStrategyEra,
  deriveCandidateBlocker,
  formatClockIST,
  formatINRValue,
  formatNumber,
  formatTimestampLabel,
  hasPositiveNumber,
  parseRegimeContext,
} from './formatters';

function parseRejectionList(raw) {
  if (!raw) return [];
  if (Array.isArray(raw)) return raw.filter(Boolean);
  try {
    const parsed = JSON.parse(String(raw));
    return Array.isArray(parsed) ? parsed.filter(Boolean) : [String(raw)];
  } catch {
    return String(raw).trim() ? [String(raw)] : [];
  }
}

export default function SignalsTab({ activeReadiness, signalsData, sliceMeta }) {
  const [expandedEngines, setExpandedEngines] = useState({});
  const [showSurveillance, setShowSurveillance] = useState(true);

  const toggleEngine = (engineId) => {
    setExpandedEngines((prev) => ({ ...prev, [engineId]: !prev[engineId] }));
  };

  const allRows = Array.isArray(activeReadiness?.engine_readiness)
    ? activeReadiness.engine_readiness
    : [];
  const filteredRows = showSurveillance
    ? allRows
    : allRows.filter((r) => classifyStrategyEra(r.engine_id) === 'SHADOW_FORWARD_V1');

  const barEvalTs = formatTimestampLabel(activeReadiness?.timing?.last_bar_evaluated_ts);
  const persistedSignals = Array.isArray(signalsData) ? signalsData : [];

  return (
    <div className="space-y-5">
      {/* 1. Live Engine Gate Readiness Table */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs overflow-hidden">
        <div className="p-4 sm:p-5 border-b border-slate-100 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              Candidate Gate Evaluation (Current vs. Target)
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Gate counts indicate deterministic rule checks met (<code>passed/total</code>), not
              probabilities or expected profitability. Expand any row for full gate inputs.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <label className="inline-flex items-center gap-1.5 text-slate-700 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={showSurveillance}
                onChange={(e) => setShowSurveillance(e.target.checked)}
                className="rounded border-slate-300"
              />
              <span>Show Passive Surveillance (Retired V6B)</span>
            </label>
            <span className="font-mono text-slate-500">
              Retrieved:{' '}
              {sliceMeta?.readiness?.lastSuccessMs
                ? formatClockIST(sliceMeta.readiness.lastSuccessMs)
                : UNAVAILABLE_LABEL}
            </span>
          </div>
        </div>

        {filteredRows.length === 0 ? (
          <div className="p-6 text-xs font-mono text-slate-500">
            {sliceMeta?.readiness?.status === 'loading'
              ? 'Loading engine gate checks...'
              : sliceMeta?.readiness?.status === 'error'
              ? `Failed to retrieve engine readiness: ${sliceMeta.readiness.errorMessage || 'Error'}`
              : 'No engine readiness rows available.'}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-200 text-[11px] font-semibold uppercase tracking-wide text-slate-600">
                  <th className="py-2.5 px-3 w-8" aria-label="Expand row" />
                  <th className="py-2.5 px-3">Engine / Book</th>
                  <th className="py-2.5 px-3">Candidate Contract</th>
                  <th className="py-2.5 px-3">Direction</th>
                  <th className="py-2.5 px-3">Eval Bar (IST)</th>
                  <th className="py-2.5 px-3">State</th>
                  <th className="py-2.5 px-3">Principal Blocker</th>
                  <th className="py-2.5 px-3 text-right">Checks</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {filteredRows.map((row) => {
                  const isExpanded = Boolean(expandedEngines[row.engine_id]);
                  const info = deriveCandidateBlocker(row);
                  const isSurveillance = row.state === 'SURVEILLANCE_ONLY';
                  const isEligible = row.state === 'READY_FIRE';

                  return (
                    <React.Fragment key={row.engine_id}>
                      <tr
                        className={
                          isSurveillance
                            ? 'bg-slate-50/50 text-slate-600'
                            : 'hover:bg-slate-50/80 text-slate-800'
                        }
                      >
                        <td className="py-2.5 px-3">
                          <button
                            type="button"
                            onClick={() => toggleEngine(row.engine_id)}
                            aria-expanded={isExpanded}
                            aria-label={`Toggle gate details for ${row.engine_id}`}
                            className="p-1 rounded hover:bg-slate-200/70 text-slate-600 cursor-pointer"
                          >
                            {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                          </button>
                        </td>
                        <td className="py-2.5 px-3">
                          <div className="font-semibold text-slate-900">{row.label}</div>
                          <div className="text-[11px] font-mono text-slate-500 flex items-center gap-1.5 mt-0.5">
                            <span>{row.engine_id}</span>
                            <span>•</span>
                            <span>
                              {isSurveillance ? 'Passive Surveillance' : 'Forward Paper'}
                            </span>
                          </div>
                        </td>
                        <td className="py-2.5 px-3 font-mono font-semibold whitespace-nowrap">
                          {row.candidate_contract && row.candidate_contract !== '—'
                            ? row.candidate_contract
                            : UNAVAILABLE_LABEL}
                        </td>
                        <td className="py-2.5 px-3 font-mono whitespace-nowrap">
                          {info.direction}
                        </td>
                        <td className="py-2.5 px-3 font-mono whitespace-nowrap text-slate-600">
                          {barEvalTs}
                        </td>
                        <td className="py-2.5 px-3 whitespace-nowrap">
                          <span
                            className={`inline-block px-2 py-0.5 rounded font-mono text-[11px] font-semibold border ${
                              isEligible
                                ? 'bg-emerald-50 text-emerald-800 border-emerald-300'
                                : isSurveillance
                                ? 'bg-slate-100 text-slate-600 border-slate-300'
                                : 'bg-slate-100 text-slate-800 border-slate-300'
                            }`}
                          >
                            {row.state || UNAVAILABLE_LABEL}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 font-mono text-[11px] max-w-xs truncate" title={info.principalBlocker}>
                          {info.principalBlocker}
                        </td>
                        <td className="py-2.5 px-3 text-right font-mono font-semibold tabular-nums whitespace-nowrap">
                          {row.gates_passed ?? 0}/{row.gates_total ?? 0}
                        </td>
                      </tr>

                      {isExpanded && (
                        <tr className="bg-slate-50/90">
                          <td colSpan={8} className="p-4 border-t border-slate-100">
                            <div className="space-y-3">
                              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                                <span className="font-semibold text-slate-800">
                                  Window: {row.window_ist || UNAVAILABLE_LABEL} IST • Trigger Rule:{' '}
                                  <span className="font-normal text-slate-600">
                                    {row.when_order_fires || UNAVAILABLE_LABEL}
                                  </span>
                                </span>
                                {info.missingInputs.length > 0 && (
                                  <span className="text-amber-800 font-mono text-[11px]">
                                    Missing / Zero Inputs: {info.missingInputs.join(', ')}
                                  </span>
                                )}
                              </div>

                              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-2">
                                {(row.gates || []).map((g) => {
                                  const rawCur = String(g.current ?? '').trim();
                                  const displayCur =
                                    !rawCur || rawCur === '—' || rawCur === '-'
                                      ? UNAVAILABLE_LABEL
                                      : rawCur;
                                  return (
                                    <div
                                      key={g.name}
                                      className={`rounded-lg p-2.5 border font-mono text-[11px] ${
                                        g.passed
                                          ? 'bg-emerald-50/60 border-emerald-200 text-emerald-950'
                                          : 'bg-white border-slate-200 text-slate-700'
                                      }`}
                                    >
                                      <div className="flex items-center justify-between font-bold">
                                        <span>{g.name}</span>
                                        <span>{g.passed ? 'PASS' : 'BLOCKED'}</span>
                                      </div>
                                      <div className="mt-1">Current: {displayCur}</div>
                                      <div className="text-slate-500">
                                        Target: {g.target || UNAVAILABLE_LABEL}
                                      </div>
                                    </div>
                                  );
                                })}
                              </div>
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
      </section>

      {/* 2. Persisted Signal Audit Log (/signals) */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs overflow-hidden">
        <div className="p-4 sm:p-5 border-b border-slate-100 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              Recorded Signal Ledger (<code>GET /signals</code>)
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Persisted signal evaluations and backend-supplied rejection reasons.
            </p>
          </div>
          <span className="text-xs font-mono text-slate-500">
            Retrieved:{' '}
            {sliceMeta?.signals?.lastSuccessMs
              ? formatClockIST(sliceMeta.signals.lastSuccessMs)
              : UNAVAILABLE_LABEL}
          </span>
        </div>

        {sliceMeta?.signals?.status === 'error' ? (
          <div className="p-4 m-4 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-900 flex items-center gap-2">
            <AlertTriangle size={15} className="text-amber-600 shrink-0" />
            <span>
              Signal history request failed: {sliceMeta.signals.errorMessage || 'Unavailable'}.
            </span>
          </div>
        ) : persistedSignals.length === 0 ? (
          <div className="p-6 text-xs text-slate-500 font-mono">
            {sliceMeta?.signals?.status === 'loading'
              ? 'Loading recorded signals...'
              : 'No persisted signal rows returned by /signals.'}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs font-mono">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-200 text-[11px] font-sans font-semibold uppercase tracking-wide text-slate-600">
                  <th className="py-2.5 px-3">Created (IST)</th>
                  <th className="py-2.5 px-3">Scope</th>
                  <th className="py-2.5 px-3">Strategy</th>
                  <th className="py-2.5 px-3">Contract</th>
                  <th className="py-2.5 px-3">Direction</th>
                  <th className="py-2.5 px-3 text-right">Bid / Ask (Spread)</th>
                  <th className="py-2.5 px-3">Status</th>
                  <th className="py-2.5 px-3">Rejection Reasons</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 tabular-nums">
                {persistedSignals.slice(0, 40).map((sig) => {
                  const era = classifyStrategyEra(sig.strategy_mode, parseRegimeContext(sig));
                  const rejections = parseRejectionList(sig.rejection_reasons);
                  return (
                    <tr key={sig.signal_id} className="hover:bg-slate-50">
                      <td className="py-2 px-3 whitespace-nowrap text-slate-600">
                        {formatTimestampLabel(sig.created_at)}
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap">
                        <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 text-[10px]">
                          {era === 'SHADOW_FORWARD_V1' ? 'Forward V1' : 'Legacy Paper'}
                        </span>
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap font-semibold text-slate-800">
                        {sig.strategy_mode || UNAVAILABLE_LABEL}
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap text-slate-900">
                        {sig.trading_symbol || UNAVAILABLE_LABEL}
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap">{sig.direction || UNAVAILABLE_LABEL}</td>
                      <td className="py-2 px-3 text-right whitespace-nowrap">
                        {hasPositiveNumber(sig.option_bid) && hasPositiveNumber(sig.option_ask)
                          ? `${formatINRValue(sig.option_bid)} / ${formatINRValue(sig.option_ask)} (${formatNumber(sig.spread_pct, { decimals: 2, suffix: '%' })})`
                          : UNAVAILABLE_LABEL}
                      </td>
                      <td className="py-2 px-3 whitespace-nowrap font-semibold">
                        {sig.status || UNAVAILABLE_LABEL}
                      </td>
                      <td className="py-2 px-3 font-sans text-slate-600 max-w-sm">
                        {rejections.length > 0 ? rejections.join('; ') : 'None recorded'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
