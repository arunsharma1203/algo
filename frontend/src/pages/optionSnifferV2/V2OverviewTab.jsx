import React from 'react';
import { ShieldAlert, CheckCircle2, XCircle, Radio, Layers, Activity, Lock } from 'lucide-react';
import { fmtAgeMs, fmtInr, fmtSignedInr } from './v2Fixtures';

export default function V2OverviewTab({
  overview,
  includeCollateral,
  setIncludeCollateral,
  collateralYieldPa,
  setCollateralYieldPa,
  haircutPct,
  setHaircutPct,
}) {
  if (!overview) return null;

  const colStatus = overview.collection_status || {};
  const isLive = colStatus.collector_state === 'PARALLEL_LIVE_COLLECTION_ACTIVE';
  const isRestartPending = colStatus.collector_state === 'PENDING_SHARED_SERVICE_RESTART';

  const fwd = overview.forward_paper_summary || {};
  const rep = overview.archive_replay_summary || {};
  const promo = overview.promotion_gates || {};
  const hyg = overview.feed_hygiene || {};
  const fc = hyg.feed_consumer || {};
  const sub = hyg.subscription_registry || {};
  const bookA = (overview.books || {}).V2_BOOK_A_PROPOSED_O3 || {};
  const bookB = (overview.books || {}).V2_BOOK_B_PAID_FLOW || {};
  const coll = (overview.optional_collateral_reporting || {}).archive_replay || {};
  const collFwd = (overview.optional_collateral_reporting || {}).forward_paper || {};

  return (
    <div className="space-y-4">
      {/* Honest Collection & Shared-Restart Status Banner */}
      <div
        className={`rounded-xl border p-4 flex flex-col md:flex-row md:items-center md:justify-between gap-3 ${
          isLive
            ? 'bg-emerald-950/30 border-emerald-500/40'
            : isRestartPending
            ? 'bg-amber-950/35 border-amber-500/50'
            : 'bg-slate-900/70 border-slate-700'
        }`}
      >
        <div className="flex items-start gap-3">
          <Radio
            className={`w-5 h-5 mt-0.5 shrink-0 ${
              isLive ? 'text-emerald-400 animate-pulse' : isRestartPending ? 'text-amber-400' : 'text-sky-400'
            }`}
          />
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-mono font-bold uppercase tracking-wider text-white">
                Collection State: {colStatus.collector_state || 'UNAVAILABLE'}
              </span>
              <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-900/50 text-emerald-300 border border-emerald-500/40 font-mono">
                PAPER ONLY • simulation_mode=true • live_capable=false
              </span>
            </div>
            <p className="text-xs text-slate-300 mt-1">{colStatus.collector_detail}</p>
            {isRestartPending && (
              <p className="text-[11px] font-mono text-amber-300 mt-1.5 bg-black/30 px-2 py-1 rounded border border-amber-500/30">
                Live daemon (:8000) was left running &amp; untouched for V1 protection. To activate live V2 fan-out:
                stop :8000 off-hours and run `cd backend &amp;&amp; venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000`
              </p>
            )}
          </div>
        </div>
        <div className="text-right font-mono text-[11px] text-slate-400 shrink-0">
          <div>Protocol Hash: {(overview.config_hash || '').slice(0, 14)}...</div>
          <div>Adapter: {overview.execution_adapter || 'LOCAL_PAPER_SIMULATOR_ONLY'}</div>
        </div>
      </div>

      {/* Unresolved Open Risk Alert if > 0 */}
      {Number(fwd.unresolved_exposure_count || 0) > 0 && (
        <div className="rounded-xl border border-rose-500/50 bg-rose-950/40 p-3 flex items-center justify-between">
          <div className="flex items-center gap-2 text-rose-200 text-xs font-mono">
            <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0" />
            <span>
              UNRESOLVED EXPOSURE ACTIVE ({fwd.unresolved_exposure_count} pkg) — Full structural max risk (
              {fmtInr(fwd.unresolved_structural_risk_charged_inr)}) charged to conservative MTM DD &amp; blocks promotion gates.
            </span>
          </div>
        </div>
      )}

      {/* Strict Separation: Forward Paper vs Archive Replay */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-bold uppercase tracking-wider text-sky-300 font-mono">
              1. V2 Forward Paper (Live Stream Fan-Out)
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded bg-sky-900/40 text-sky-300 border border-sky-500/30 font-mono">
              FORWARD_PAPER • Realized Eq: {fmtInr(fwd.realized_equity_inr)}
            </span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-mono">
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Net PnL (Closed)</div>
              <div className={`font-bold ${Number(fwd.net_pnl_inr) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {fmtSignedInr(fwd.net_pnl_inr)}
              </div>
            </div>
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Trades / Open</div>
              <div className="font-bold text-white">
                {fwd.trade_count ?? 0} / {fwd.open_count ?? 0}
              </div>
            </div>
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Reserved Cash</div>
              <div className="font-bold text-slate-200">{fmtInr(fwd.reserved_capital_inr)}</div>
            </div>
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Conservative MTM DD</div>
              <div className="font-bold text-amber-300">
                {fmtInr(fwd.intraday_mtm_dd_inr)} ({fwd.intraday_mtm_dd_pct ?? 0}%)
              </div>
            </div>
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-bold uppercase tracking-wider text-purple-300 font-mono">
              2. Archived L5 Deterministic Replay (2026-11-16..23)
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded bg-purple-900/40 text-purple-300 border border-purple-500/30 font-mono">
              ARCHIVE_REPLAY • Never mixed with Forward
            </span>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-mono">
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Replay Net PnL</div>
              <div className="font-bold text-emerald-400">{fmtSignedInr(rep.net_pnl_inr)}</div>
            </div>
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Trades / Charges</div>
              <div className="font-bold text-white">
                {rep.trade_count ?? 0} / {fmtInr(rep.total_charges_inr)}
              </div>
            </div>
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Peak Capital</div>
              <div className="font-bold text-slate-200">{fmtInr(rep.peak_capital_used_inr)}</div>
            </div>
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Conservative MTM DD</div>
              <div className="font-bold text-amber-300">{fmtInr(rep.intraday_mtm_dd_inr)}</div>
            </div>
          </div>
          <div className="text-[10px] font-mono text-slate-400 truncate">
            Replay Ledger SHA-256: {rep.canonical_ledger_sha256 || 'PENDING_ARCHIVE_REPLAY_RUN'}
          </div>
        </div>
      </div>

      {/* Book A vs Book B Cards */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="rounded-xl border border-rose-500/40 bg-slate-900/70 p-4 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Lock className="w-4 h-4 text-rose-400" />
              <span className="text-xs font-bold text-white font-mono">BOOK A: Proposed Phase-4 O3 T-1→T0 Spread</span>
            </div>
            <span className="text-[10px] px-2 py-0.5 rounded bg-rose-900/60 text-rose-200 border border-rose-500/40 font-mono">
              {bookA.autonomous_entries_enabled ? 'OPT-IN REPLAY' : 'DISABLED: UNVERIFIED_SPEC_CONFLICT'}
            </span>
          </div>
          <p className="text-xs text-slate-300">
            Hold-closed pending raw O3 evidence reconciliation: Phase-4 used 15m Open[t+1] candles, 70% decay target, weekday DTE &amp; 100pt grid vs V2&apos;s 50% target, holiday-adjusted DTE, 50pt grid &amp; strict sub-second Leg-2 credit floor.
          </p>
          <div className="text-[11px] font-mono text-slate-400 pt-1">
            Safety rules coded &amp; unit-tested: Buy-first partial lot, ₹30 strict floor at actual hedge fill, Entry-Day MTM + Overnight Gap + Expiry Intraday P&amp;L split, 14:45 fail-closed cutoff retention.
          </div>
        </div>

        <div className="rounded-xl border border-emerald-500/40 bg-slate-900/70 p-4 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Activity className="w-4 h-4 text-emerald-400" />
              <span className="text-xs font-bold text-white font-mono">BOOK B: Paid Convexity Flow (Active Paper Hypothesis)</span>
            </div>
            <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-900/60 text-emerald-200 border border-emerald-500/40 font-mono">
              ENABLED (PAPER)
            </span>
          </div>
          <div className="grid grid-cols-2 gap-2 text-[11px] font-mono text-slate-300 pt-1">
            <div>Window: 10:15–13:00 IST</div>
            <div>Signal: ΔBasis ≥ ±3.0 pt (same FUT contract)</div>
            <div>Cap: ₹11,500 (+1-OTM fallback on CashCap only)</div>
            <div>Planned Stop: min(20%, ₹1,800) adverse_buy tick</div>
          </div>
        </div>
      </div>

      {/* Feed Hygiene & Subscription Safety */}
      <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4">
        <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-slate-200 font-mono mb-3">
          <Layers className="w-4 h-4 text-sky-400" />
          <span>Feed Hygiene, Queue Lag &amp; V2-Owned Subscription Safety</span>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 text-xs font-mono">
          <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
            <div className="text-[10px] text-slate-400">Spot / Fut Age</div>
            <div className="text-white font-semibold">
              {fmtAgeMs(hyg.spot_age_ms)} / {fmtAgeMs(hyg.futures_age_ms)}
            </div>
          </div>
          <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
            <div className="text-[10px] text-slate-400">Spot-Fut Skew</div>
            <div className="text-white font-semibold">
              {hyg.spot_fut_sync?.skew_ms != null ? `${hyg.spot_fut_sync.skew_ms} ms` : 'UNAVAILABLE'}
            </div>
          </div>
          <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
            <div className="text-[10px] text-slate-400">Queue Depth / Cap</div>
            <div className="text-white font-semibold">
              {fc.current_queue_depth ?? 0} / {fc.queue_capacity ?? 4096}
            </div>
          </div>
          <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
            <div className="text-[10px] text-slate-400">Lag p50 / p99</div>
            <div className="text-white font-semibold">
              {fc.queue_lag_p50_ms ?? 0} / {fc.queue_lag_p99_ms ?? 0} ms
            </div>
          </div>
          <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
            <div className="text-[10px] text-slate-400">Dups / Late / Gaps / Drops</div>
            <div className="text-white font-semibold">
              {fc.duplicate_events_suppressed ?? 0}/{fc.late_out_of_order_events ?? 0}/{fc.sequence_bar_gaps ?? 0}/{fc.dropped_overflow_events ?? 0}
            </div>
          </div>
          <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
            <div className="text-[10px] text-slate-400">V2 Pins / Yields</div>
            <div className="text-white font-semibold">
              {(sub.v2_owned_only_keys || []).length} / {sub.pressure_yield_count ?? 0}
            </div>
          </div>
        </div>
      </div>

      {/* 60-Session Promotion Gate Monitor & Optional Collateral Carry */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2.5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-bold uppercase tracking-wider text-slate-200 font-mono">
              60-Session Forward Promotion Gates
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded bg-amber-950/70 text-amber-300 border border-amber-500/40 font-mono">
              {promo.sessions_progress || '0/60'} • {promo.status || 'COLLECTING'}
            </span>
          </div>
          <div className="space-y-1.5">
            {(promo.checks || []).map((c, idx) => (
              <div key={idx} className="flex items-center justify-between text-xs font-mono bg-slate-950/50 px-2.5 py-1.5 rounded border border-slate-800">
                <div className="flex items-center gap-2">
                  {c.passed ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                  ) : (
                    <XCircle className="w-3.5 h-3.5 text-slate-500" />
                  )}
                  <span className="text-slate-200">{c.gate}</span>
                </div>
                <span className={c.passed ? 'text-emerald-400' : 'text-amber-300'}>
                  {String(c.observed)} (req {String(c.required)})
                </span>
              </div>
            ))}
          </div>
          <div className="text-[10px] text-slate-400 font-mono">
            Gate clearance triggers ZERO automated orders or broker calls (human review only).
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div>
              <span className="text-xs font-bold uppercase tracking-wider text-slate-200 font-mono">
                Optional Reporting-Only Collateral Yield
              </span>
              <div className="text-[10px] font-mono text-amber-300">
                {coll.classification_label || 'ASSUMED_YIELD_NOT_TRADING_ALPHA'}
              </div>
            </div>
            <label className="flex items-center gap-2 text-xs font-mono text-slate-200 cursor-pointer">
              <input
                type="checkbox"
                checked={includeCollateral}
                onChange={(e) => setIncludeCollateral(e.target.checked)}
                className="rounded border-slate-600 bg-slate-950 text-sky-500"
              />
              Enable Scenario
            </label>
          </div>
          <div className="grid grid-cols-2 gap-2 text-xs font-mono">
            <label className="flex flex-col gap-1 text-slate-400">
              <span>Assumed Yield (% p.a.)</span>
              <input
                type="number"
                step="0.25"
                value={collateralYieldPa}
                onChange={(e) => setCollateralYieldPa(Number(e.target.value) || 0)}
                className="px-2 py-1 rounded bg-slate-950 border border-slate-700 text-white"
              />
            </label>
            <label className="flex flex-col gap-1 text-slate-400">
              <span>Haircut (%)</span>
              <input
                type="number"
                step="1"
                value={haircutPct}
                onChange={(e) => setHaircutPct(Number(e.target.value) || 0)}
                className="px-2 py-1 rounded bg-slate-950 border border-slate-700 text-white"
              />
            </label>
          </div>
          <div className="grid grid-cols-2 gap-2 text-[11px] font-mono bg-slate-950/60 p-2.5 rounded border border-slate-800">
            <div>Archive Excess vs 6.5% Rf (Trading): {fmtSignedInr(coll.excess_trading_alpha_vs_rf_inr)}</div>
            <div>Hypothetical Carry Income: {fmtSignedInr(coll.hypothetical_collateral_income_inr)}</div>
            <div>Forward Hypothetical Carry: {fmtSignedInr(collFwd.hypothetical_collateral_income_inr)}</div>
            <div>50:50 Cash Rule Buffer: {fmtInr(coll.cash_buffer_after_50pct_rule_inr)}</div>
          </div>
        </div>
      </div>
    </div>
  );
}
