import React, { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { fmtAgeMs, fmtInr, fmtIstStamp, fmtSignedInr } from './v2Fixtures';

function TradeDetailDrawer({ row }) {
  const fills = row.fills || [];
  const gates = row.signal_gates || [];
  const timeline = row.events_timeline || [];
  const legs = row.legs_state || {};

  return (
    <div className="bg-slate-950/80 border-t border-slate-800 px-3 py-3 space-y-3 text-xs font-mono">
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-2.5 space-y-1">
          <div className="text-[10px] uppercase text-slate-400">Accounting &amp; Risk Breakdown</div>
          <div className="flex justify-between">
            <span className="text-slate-400">Gross P&amp;L</span>
            <span className="text-slate-200">{fmtSignedInr(row.gross_pnl_inr)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-slate-400">Statutory + Brokerage</span>
            <span className="text-slate-300">{fmtInr(row.total_friction_inr)}</span>
          </div>
          <div className="flex justify-between border-t border-slate-800 pt-1 font-semibold">
            <span className="text-slate-300">Net P&amp;L</span>
            <span className={Number(row.net_pnl_inr) >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
              {fmtSignedInr(row.net_pnl_inr)}
            </span>
          </div>
          <div className="flex justify-between text-[11px] pt-1">
            <span className="text-slate-400">Planned Stop / Overshoot</span>
            <span className="text-amber-300">
              {fmtInr(row.planned_loss_inr)} / {fmtInr(row.slippage_and_gap_overshoot_inr)}
            </span>
          </div>
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-2.5 space-y-1">
          <div className="text-[10px] uppercase text-slate-400">Leg Fills &amp; Consumed Depth</div>
          {fills.length === 0 ? (
            <div className="text-[11px] text-slate-400">
              {legs.long_leg
                ? `${legs.long_leg.trading_symbol} entry ${legs.long_leg.entry_price}`
                : 'No separate leg fill trace recorded.'}
            </div>
          ) : (
            <div className="space-y-1.5">
              {fills.map((f) => (
                <div key={f.fill_id || `${f.leg_role}_${f.ts_ms}`} className="border-b border-slate-800/70 pb-1 last:border-0 last:pb-0">
                  <div className="flex justify-between text-[11px]">
                    <span className="text-sky-300 font-semibold">
                      {f.leg_role} ({f.side} {f.filled_qty_units}u)
                    </span>
                    <span className="text-white">@ {f.fill_price}</span>
                  </div>
                  <div className="text-[10px] text-slate-400 break-words">
                    Fidelity: {f.fill_fidelity || 'UNKNOWN'} • Rungs: {JSON.stringify(f.consumed_rungs || [])}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-900/70 p-2.5 space-y-1">
          <div className="text-[10px] uppercase text-slate-400">Entry Gate Inputs</div>
          {gates.length === 0 ? (
            <div className="text-[11px] text-slate-400">Gate breakdown unavailable on legacy row.</div>
          ) : (
            <div className="space-y-1">
              {gates.map((g, i) => (
                <div key={i} className="flex justify-between gap-2 text-[11px]">
                  <span className="text-slate-300 truncate">{g.name}</span>
                  <span className={g.passed ? 'text-emerald-300' : 'text-rose-300'}>
                    {String(g.current)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {timeline.length > 0 && (
        <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-2.5">
          <div className="text-[10px] uppercase text-slate-400 mb-1">Chronological Event Timeline</div>
          <div className="space-y-1">
            {timeline.map((ev, i) => (
              <div key={i} className="flex flex-wrap gap-2 text-[11px] text-slate-300">
                <span className="text-slate-400 whitespace-nowrap">{fmtIstStamp(ev.bar_ts)}</span>
                <span className="text-sky-300 font-semibold">{ev.event}</span>
                <span className="text-slate-300 break-words">{ev.detail}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function V2ExpandableTradeTable({ title, badge, rows, emptyMessage }) {
  const [expandedId, setExpandedId] = useState(null);

  return (
    <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <span className="text-xs font-bold uppercase tracking-wider text-slate-200 font-mono">{title}</span>
        <span className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-600 font-mono">
          {badge} ({rows.length})
        </span>
      </div>

      {rows.length === 0 ? (
        <div className="text-xs font-mono text-slate-400 py-2">{emptyMessage || 'No trades recorded.'}</div>
      ) : (
        <div className="divide-y divide-slate-800 border border-slate-800 rounded-lg overflow-hidden">
          {rows.map((r) => {
            const isOpen = expandedId === r.package_id;
            return (
              <div key={r.package_id} className="bg-slate-950/40">
                <button
                  type="button"
                  aria-expanded={isOpen}
                  onClick={() => setExpandedId(isOpen ? null : r.package_id)}
                  className="w-full text-left px-3 py-2.5 hover:bg-slate-800/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 transition-colors font-mono text-xs"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      {isOpen ? (
                        <ChevronDown className="w-4 h-4 text-sky-400 shrink-0" />
                      ) : (
                        <ChevronRight className="w-4 h-4 text-slate-400 shrink-0" />
                      )}
                      <div className="min-w-0">
                        <div className="font-semibold text-white truncate">{r.direction_label}</div>
                        <div className="text-[11px] text-slate-400 break-words">
                          {r.session_date} • {r.entry_bar_ts?.slice(11, 16) || '—'} → {r.exit_bar_ts?.slice(11, 16) || '—'} IST •{' '}
                          <span className="text-slate-300">{r.exit_reason}</span>
                        </div>
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className={`font-bold ${Number(r.net_pnl_inr) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {fmtSignedInr(r.net_pnl_inr)}
                      </div>
                      <div className="text-[10px] text-slate-400">
                        Charges {fmtInr(r.total_friction_inr)}
                      </div>
                    </div>
                  </div>
                </button>
                {isOpen && <TradeDetailDrawer row={r} />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function V2OpenExposurePanel({ openPositions }) {
  const [expandedId, setExpandedId] = useState(null);
  const rows = openPositions || [];

  return (
    <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <span className="text-xs font-bold uppercase tracking-wider text-amber-300 font-mono">
          Open / Pending / Unresolved Forward Exposure ({rows.length})
        </span>
        <span className="text-[11px] font-mono text-slate-400">
          Unpriced legs retain last reliable mark age and full conservative structural risk
        </span>
      </div>

      {rows.length === 0 ? (
        <div className="text-xs font-mono text-slate-400 py-1">Zero open forward paper positions.</div>
      ) : (
        <div className="divide-y divide-slate-800 border border-slate-800 rounded-lg overflow-hidden">
          {rows.map((p) => {
            const isOpen = expandedId === p.package_id;
            return (
              <div key={p.package_id} className="bg-slate-950/40">
                <button
                  type="button"
                  aria-expanded={isOpen}
                  onClick={() => setExpandedId(isOpen ? null : p.package_id)}
                  className="w-full text-left px-3 py-2.5 hover:bg-slate-800/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 transition-colors font-mono text-xs"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      {isOpen ? (
                        <ChevronDown className="w-4 h-4 text-amber-300 shrink-0" />
                      ) : (
                        <ChevronRight className="w-4 h-4 text-slate-400 shrink-0" />
                      )}
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-semibold text-white">{p.direction_label}</span>
                          <span
                            className={`px-1.5 py-0.5 rounded text-[10px] ${
                              p.is_unpriced_exposure
                                ? 'bg-rose-900/70 text-rose-200 border border-rose-500/40'
                                : 'bg-sky-900/60 text-sky-200 border border-sky-500/40'
                            }`}
                          >
                            {p.state}
                          </span>
                        </div>
                        <div className="text-[11px] text-amber-300 break-words mt-0.5 space-y-0.5">
                          <div>
                            {p.is_unpriced_exposure || p.is_stale_mark ? (
                              <span className="inline-flex items-center gap-1.5">
                                <span className="px-1.5 py-0.2 rounded bg-rose-500/20 text-rose-300 border border-rose-500/40 font-semibold text-[10px]">
                                  {p.is_unpriced_exposure ? 'UNPRICED / STALE' : 'STALE MARK'}
                                </span>
                                <span>
                                  {p.is_unpriced_exposure
                                    ? `${p.unpriced_reason} (${fmtAgeMs(p.unpriced_age_ms)} old)`
                                    : `${fmtSignedInr(p.last_reliable_mark_net_inr)} (${fmtAgeMs(p.mark_age_ms)} old)`}
                                </span>
                              </span>
                            ) : (
                              <span className="text-emerald-300">
                                Fresh synchronized mark: {fmtSignedInr(p.fresh_executable_mark_inr ?? p.last_reliable_mark_net_inr)}
                              </span>
                            )}
                          </div>
                          {p.last_reliable_mark_net_inr != null && (
                            <div className="text-slate-400">
                              Last reliable mark: {fmtSignedInr(p.last_reliable_mark_net_inr)} ({fmtAgeMs(p.mark_age_ms)} ago
                              {p.is_unpriced_exposure ? ' — STALE' : ''})
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="font-bold text-rose-400">{fmtSignedInr(p.conservative_worst_loss_inr)}</div>
                      <div className="text-[10px] text-amber-300/80">Conservative Stress Est</div>
                      <div className="text-[10px] text-slate-400">Reserved {fmtInr(p.reserved_capital_inr)}</div>
                    </div>
                  </div>
                </button>
                {isOpen && <TradeDetailDrawer row={p} />}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function V2PositionsTab({ positions, positionsMeta }) {
  if (!positions) {
    return (
      <div className="rounded-xl border border-slate-700 bg-slate-900/70 p-4 text-xs font-mono text-amber-300">
        Positions data Unavailable {positionsMeta?.error ? `(${positionsMeta.error})` : ''}
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <V2OpenExposurePanel openPositions={positions.forward_open_positions} />
      <V2ExpandableTradeTable
        title="Forward Paper Closed Trades"
        badge="FORWARD_PAPER"
        rows={positions.forward_closed_trades || []}
        emptyMessage="No closed Forward Paper trades recorded yet."
      />
    </div>
  );
}
