import React from 'react';

const fmtInr = (val) => {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return '—';
  const n = Number(val);
  return `${n >= 0 ? '+' : '-'}₹${Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

const fmtNum = (val, digits = 2, suffix = '') => {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return '—';
  return `${Number(val).toFixed(digits)}${suffix}`;
};

export default function V2OverviewTab({ overview, liveStatus, fundsPreview }) {
  if (!overview) {
    return (
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 text-sm text-slate-400">
        Loading Option Sniffer Advance V2 overview telemetry...
      </div>
    );
  }

  const fwd = overview.forward_paper_summary || {};
  const vrp = overview.vrp_telemetry || {};
  const aiml = overview.aiml_passive_telemetry || {};
  const arb = overview.regime_arbitration || {};
  const split = overview.book_performance_split || {};
  const bookARow =
    (overview.book_status_rows || []).find((r) => r.book_id === 'V2_BOOK_A_INTRADAY_CREDIT') || {};
  const bookBRow =
    (overview.book_status_rows || []).find((r) => r.book_id === 'V2_BOOK_B_PAID_FLOW') || {};

  const armed = Boolean(liveStatus?.armed);
  const lots = armed
    ? Number(liveStatus?.max_lots_per_book || liveStatus?.arm_state?.max_lots_per_book || 1)
    : Number(overview?.lots_per_book || 1);
  const availMargin =
    fundsPreview?.available_margin_inr ?? liveStatus?.available_margin_inr ?? null;
  const effReserveCap = Number(
    overview?.effective_max_capital_reserved_inr ?? fwd.effective_max_capital_reserved_inr ?? 110000 * lots
  );

  const vrpVal = vrp.vrp_ratio;
  const vrpBadgeClass =
    vrpVal === null || vrpVal === undefined
      ? 'text-slate-300'
      : Number(vrpVal) <= 1.15
      ? 'text-emerald-400'
      : 'text-amber-400';

  const realizedNet = Number(fwd.net_pnl_inr || 0);
  const liveNet = Number(liveStatus?.daily_combined_net_inr || 0);
  const displayNet = armed ? liveNet : realizedNet;
  const eqNow = Number(fwd.marked_equity_inr ?? fwd.realized_equity_inr ?? 200000);

  return (
    <div className="space-y-5">
      {/* KPI Row */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">
            {armed ? 'Live Daily Net P&L' : 'Simulated Net P&L'}
          </div>
          <div className={`text-xl font-bold mt-1 ${displayNet >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {fmtInr(displayNet)}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">
            {armed
              ? `${liveStatus?.open_package_count || 0} open • ${liveStatus?.closed_package_count || 0} closed live (Sim ${fmtInr(realizedNet)})`
              : `${fwd.closed_trades || 0} closed • ${fwd.open_count || 0} open`}
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">
            {armed ? 'Broker Margin Available' : `Capital & Sim MTM (${lots}L)`}
          </div>
          <div className="text-xl font-bold text-white mt-1">
            {armed && availMargin !== null && availMargin !== undefined
              ? `₹${Number(availMargin).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
              : `₹${eqNow.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">
            {armed
              ? `Req Floor (${lots}L): ₹${Number(liveStatus?.min_required_margin_inr || 0).toLocaleString('en-IN')}`
              : availMargin !== null && availMargin !== undefined
              ? `Upstox: ₹${Number(availMargin).toLocaleString('en-IN', { maximumFractionDigits: 0 })} • Cap: ₹${effReserveCap.toLocaleString('en-IN')}`
              : `Reserved: ₹${Number(fwd.reserved_capital_inr || 0).toLocaleString('en-IN')} / ₹${effReserveCap.toLocaleString('en-IN')}`}
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">VRP Ratio (IV / HAR-RV)</div>
          <div className={`text-xl font-bold mt-1 ${vrpBadgeClass}`}>
            {vrpVal !== null && vrpVal !== undefined ? fmtNum(vrpVal, 2) : vrp.status || 'Awaiting Live Bar'}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">
            IV {fmtNum(vrp.atm_iv_pct, 2, '%')} vs HAR {fmtNum(vrp.har_rv_pct, 2, '%')} (Gate &le; 1.15)
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Regime &amp; Strategy Scope</div>
          <div className="text-sm font-bold text-sky-300 mt-1.5 truncate" title={arb.regime_code || ''}>
            {arb.regime_code || 'STANDBY_OBSERVING'}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">
            Route:{' '}
            <span className="text-slate-300 font-semibold">
              {armed ? liveStatus?.live_book_scope : overview.paper_execution_mode || 'AUTO'} ({lots}L)
            </span>
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">20D RV Warmup</div>
          <div
            className={`text-xl font-bold mt-1 ${
              overview.warmup_20d_ready ? 'text-emerald-400' : 'text-amber-400'
            }`}
          >
            {overview.warmup_20d_ready ? 'READY' : 'WARMING'}
          </div>
          <div className="text-[11px] text-slate-500 mt-1">
            {overview.warmup_prior_days_count || 0} completed sessions (&ge; 20 req)
          </div>
        </div>
      </div>

      {/* Live Broker Margin Strip when Armed */}
      {armed && (
        <div className="bg-amber-950/30 border border-amber-500/40 rounded-xl px-4 py-3 flex flex-wrap items-center justify-between gap-3">
          <div className="text-xs text-amber-200">
            <span className="font-bold">LIVE ARMED ({liveStatus.live_book_scope} &bull; {lots}L):</span>{' '}
            Open Live: {liveStatus.open_package_count || 0} &bull; Closed Live: {liveStatus.closed_package_count || 0} &bull; Live Daily Net:{' '}
            <span className="font-semibold">{fmtInr(liveStatus.daily_combined_net_inr)}</span>
          </div>
          <div className="text-[11px] text-amber-300">
            Min Margin Floor ({lots}L): &#8377;{Number(liveStatus.min_required_margin_inr || 0).toLocaleString('en-IN')}
            {availMargin !== null && availMargin !== undefined
              ? ` • Upstox Avail: ₹${Number(availMargin).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
              : ''}{' '}
            &bull; PIN Interlock Active
          </div>
        </div>
      )}

      {/* Dual Book Status Cards */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <h3 className="text-sm font-bold text-white">Book A &bull; Intraday Credit Spread</h3>
              <p className="text-xs text-slate-400 mt-0.5">{bookARow.config_summary}</p>
            </div>
            <span
              className={`px-2 py-0.5 text-[11px] font-semibold rounded-full border ${
                bookARow.enabled
                  ? 'bg-sky-500/15 text-sky-300 border-sky-500/30'
                  : 'bg-slate-800 text-slate-400 border-slate-700'
              }`}
            >
              {bookARow.eligibility || 'STANDBY'}
            </span>
          </div>

          <div className="grid grid-cols-3 gap-2 text-xs bg-slate-950/70 border border-slate-800/80 rounded-lg p-3">
            <div>
              <div className="text-slate-400 text-[11px]">Sim Baseline</div>
              <div className={`font-bold mt-0.5 ${(split.book_a_intraday?.net_pnl_inr || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {fmtInr(split.book_a_intraday?.net_pnl_inr || 0)}
              </div>
            </div>
            <div>
              <div className="text-slate-400 text-[11px]">Trades / Win%</div>
              <div className="font-semibold text-white mt-0.5">
                {split.book_a_intraday?.trades || 0} ({fmtNum(split.book_a_intraday?.win_rate_pct || 0, 1, '%')})
              </div>
            </div>
            <div>
              <div className="text-slate-400 text-[11px]">Position</div>
              <div className="font-semibold text-slate-200 mt-0.5">{bookARow.current_position || 'NONE'}</div>
            </div>
          </div>

          <div className="text-[11px] text-slate-400 flex flex-wrap items-center justify-between gap-2">
            <span>
              Status: <span className="text-slate-300">{bookARow.eligibility_reason || 'Waiting Friday 11:30-13:15 IST'}</span>
            </span>
            <span className="text-sky-300/90 font-mono">
              Alloc ({lots}L): &#8377;{(75000 * lots).toLocaleString('en-IN')} min
            </span>
          </div>
          <div className="text-[11px] text-slate-500">
            Stop trigger budget &#8377;{(1950 * lots).toLocaleString('en-IN')} (1.8x target) &bull; 100pt Wing Structural Cap &#8377;{(4875 * lots).toLocaleString('en-IN')} (actual gap/fill loss can exceed trigger budget).
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-3">
          <div className="flex items-start justify-between gap-2">
            <div>
              <h3 className="text-sm font-bold text-white">Book B &bull; Paid Convexity Flow</h3>
              <p className="text-xs text-slate-400 mt-0.5">{bookBRow.config_summary}</p>
            </div>
            <span
              className={`px-2 py-0.5 text-[11px] font-semibold rounded-full border ${
                bookBRow.enabled
                  ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30'
                  : 'bg-slate-800 text-slate-400 border-slate-700'
              }`}
            >
              {bookBRow.eligibility || 'STANDBY'}
            </span>
          </div>

          <div className="grid grid-cols-3 gap-2 text-xs bg-slate-950/70 border border-slate-800/80 rounded-lg p-3">
            <div>
              <div className="text-slate-400 text-[11px]">Sim Baseline</div>
              <div className={`font-bold mt-0.5 ${(split.book_b_paid_flow?.net_pnl_inr || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {fmtInr(split.book_b_paid_flow?.net_pnl_inr || 0)}
              </div>
            </div>
            <div>
              <div className="text-slate-400 text-[11px]">Trades / Win%</div>
              <div className="font-semibold text-white mt-0.5">
                {split.book_b_paid_flow?.trades || 0} ({fmtNum(split.book_b_paid_flow?.win_rate_pct || 0, 1, '%')})
              </div>
            </div>
            <div>
              <div className="text-slate-400 text-[11px]">Position</div>
              <div className="font-semibold text-slate-200 mt-0.5">{bookBRow.current_position || 'NONE'}</div>
            </div>
          </div>

          <div className="text-[11px] text-slate-400 flex flex-wrap items-center justify-between gap-2">
            <span>
              Status: <span className="text-slate-300">{bookBRow.eligibility_reason || 'Active 10:15-13:00 IST'}</span>
            </span>
            <span className="text-emerald-300/90 font-mono">
              Alloc ({lots}L): &#8377;{(18500 * lots).toLocaleString('en-IN')} cap / &#8377;{(25000 * lots).toLocaleString('en-IN')} min
            </span>
          </div>
          <div className="text-[11px] text-slate-500">
            Outlay ceiling &#8377;{(18500 * lots).toLocaleString('en-IN')} at live limit &bull; Actual-fill GTT stop trigger budget &#8377;{(1800 * lots).toLocaleString('en-IN')} (gap loss bounded by premium paid).
          </div>
        </div>
      </div>

      {/* Passive AIML Telemetry Banner */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl px-4 py-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs">
          <span className="px-2 py-0.5 rounded bg-slate-800 text-sky-300 border border-slate-700 font-semibold">
            Passive AIML Telemetry
          </span>
          <span className="text-slate-300">
            50pt Breach Prob:{' '}
            <strong>
              {aiml.p_breach !== null && aiml.p_breach !== undefined
                ? `${(Number(aiml.p_breach) * 100).toFixed(1)}%`
                : aiml.status || 'Awaiting Live Bar'}
            </strong>
          </span>
        </div>
        <div className="text-[11px] text-slate-400">
          Observational only (`advisory_gate_applied = false`) &bull; Anti-self-hedge arbitration active
        </div>
      </div>
    </div>
  );
}
