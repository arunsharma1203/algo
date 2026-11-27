import React, { useState } from 'react';
import { ShieldAlert, Lock, X, CheckCircle2, AlertTriangle } from 'lucide-react';

const fmtInr = (val) => {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return 'Unavailable';
  return `₹${Number(val).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export default function V2LiveArmModal({ isOpen, onClose, liveStatus, paperOpenCount = 0, onConfirmArm }) {
  const [selectedBook, setSelectedBook] = useState('V2_BOOK_B_PAID_FLOW');
  const [maxLots, setMaxLots] = useState(liveStatus?.arm_state?.max_lots_per_book || 1);
  const [maxDailyLoss, setMaxDailyLoss] = useState(liveStatus?.arm_state?.max_daily_loss_inr || 2500);
  const [maxOpenPkgs, setMaxOpenPkgs] = useState(liveStatus?.arm_state?.max_open_packages || 1);
  const [pin, setPin] = useState('');
  const [humanConfirmed, setHumanConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');

  if (!isOpen) return null;

  const faults = liveStatus?.unresolved_faults || { total_blocking_faults: 0, items: [] };
  const paper = liveStatus?.paper_baseline_summary || {};
  const canArm =
    faults.total_blocking_faults === 0 &&
    humanConfirmed &&
    /^\d{4}$/.test(pin.trim()) &&
    !submitting;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!canArm) return;
    setErrorMsg('');
    setSubmitting(true);
    try {
      await onConfirmArm({
        pin: pin.trim(),
        human_confirmed: true,
        selected_books: [selectedBook],
        protocol_profile_id: 'V2_BOOK_B_FROZEN_PAPER',
        max_lots_per_book: Number(maxLots),
        max_daily_loss_inr: Number(maxDailyLoss),
        max_open_packages: Number(maxOpenPkgs),
      });
      setPin('');
      setHumanConfirmed(false);
      onClose();
    } catch (err) {
      setErrorMsg(err?.message || 'Failed to arm V2 live execution.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4">
      <div className="w-full max-w-2xl rounded-2xl border border-rose-500/40 bg-slate-950 text-slate-100 shadow-2xl overflow-hidden">
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-800 bg-rose-950/20">
          <div className="flex items-center gap-2.5">
            <ShieldAlert className="w-5 h-5 text-rose-400" />
            <div>
              <h3 className="text-sm font-bold uppercase tracking-wide text-rose-200">
                Arm Option Sniffer V2 Real-Capital Execution
              </h3>
              <p className="text-[11px] text-slate-400">
                Human-exclusive control • V2 Paper baseline continues untouched in parallel
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-5 space-y-4 max-h-[82vh] overflow-y-auto">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
            <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-3">
              <div className="text-[10px] text-slate-400 uppercase">Current Mode</div>
              <div className="font-bold text-amber-300 mt-1">{liveStatus?.execution_mode || 'DISARMED'}</div>
              <div className="text-[11px] text-slate-400 mt-1">
                Live Open: {liveStatus?.open_package_count ?? 0} • Paper Open: {paperOpenCount}
              </div>
            </div>
            <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-3">
              <div className="text-[10px] text-slate-400 uppercase">Paper Baseline Evidence</div>
              <div className="font-bold text-slate-100 mt-1">
                {paper?.available ? `${paper.closed_trades} closed (${fmtInr(paper.realized_net_pnl_inr)})` : 'Cold DB'}
              </div>
              <div className="text-[11px] text-slate-400 mt-1">
                {paper?.available
                  ? `Obs DD: ${fmtInr(paper.observed_mark_max_dd_inr)} • Stress: ${fmtInr(paper.stress_max_dd_inr)}`
                  : paper?.reason || 'No forward DB'}
              </div>
            </div>
            <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-3">
              <div className="text-[10px] text-slate-400 uppercase">Unresolved Faults</div>
              <div
                className={`font-bold mt-1 ${
                  faults.total_blocking_faults > 0 ? 'text-rose-400' : 'text-emerald-400'
                }`}
              >
                {faults.total_blocking_faults} Blocking
              </div>
              <div className="text-[11px] text-slate-400 mt-1">
                {faults.total_blocking_faults > 0 ? 'Reconcile before arming' : 'Clean intent ledger'}
              </div>
            </div>
          </div>

          {faults.total_blocking_faults > 0 && (
            <div className="rounded-xl border border-rose-500/50 bg-rose-950/30 p-3 text-xs text-rose-200 flex items-start gap-2">
              <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0 mt-0.5" />
              <div>
                <div className="font-bold">Arming Blocked by Unresolved Broker Faults</div>
                <div className="text-[11px] mt-1">
                  {faults.items.map((f) => `${f.kind}:${f.id} (${f.state})`).join(', ')}
                </div>
              </div>
            </div>
          )}

          <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-3.5 space-y-2.5 text-xs">
            <div className="font-semibold text-slate-200">Select Strategy Book</div>
            <label className="flex items-center justify-between p-2.5 rounded-lg border border-cyan-500/40 bg-cyan-950/20 cursor-pointer">
              <div className="flex items-center gap-2">
                <input
                  type="radio"
                  name="v2LiveBook"
                  value="V2_BOOK_B_PAID_FLOW"
                  checked={selectedBook === 'V2_BOOK_B_PAID_FLOW'}
                  onChange={() => setSelectedBook('V2_BOOK_B_PAID_FLOW')}
                />
                <div>
                  <div className="font-semibold text-cyan-200">V2_BOOK_B_PAID_FLOW (V2_BOOK_B_FROZEN_PAPER)</div>
                  <div className="text-[11px] text-slate-400">Paid intraday breakout flow with buy-wing-first protection</div>
                </div>
              </div>
              <CheckCircle2 className="w-4 h-4 text-cyan-400" />
            </label>
            <div className="flex items-center justify-between p-2.5 rounded-lg border border-slate-800 bg-slate-950/50 opacity-60">
              <div>
                <div className="font-semibold text-slate-400">V2_BOOK_A_PROPOSED_O3</div>
                <div className="text-[11px] text-amber-400">Blocked: UNVERIFIED_SPEC_CONFLICT vs historical Phase-4 O3</div>
              </div>
              <span className="px-2 py-0.5 rounded bg-slate-800 text-[10px] text-slate-400">DISABLED</span>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
            <label className="block">
              <span className="text-slate-400 text-[11px]">Max Lots / Book</span>
              <input
                type="number"
                min={1}
                max={10}
                value={maxLots}
                onChange={(e) => setMaxLots(e.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100"
              />
            </label>
            <label className="block">
              <span className="text-slate-400 text-[11px]">Max Daily Loss (INR)</span>
              <input
                type="number"
                min={500}
                step={250}
                value={maxDailyLoss}
                onChange={(e) => setMaxDailyLoss(e.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100"
              />
            </label>
            <label className="block">
              <span className="text-slate-400 text-[11px]">Max Open Packages</span>
              <input
                type="number"
                min={1}
                max={5}
                value={maxOpenPkgs}
                onChange={(e) => setMaxOpenPkgs(e.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900 px-3 py-2 text-slate-100"
              />
            </label>
          </div>

          <div className="rounded-xl border border-amber-500/30 bg-amber-950/20 p-3 text-[11px] text-amber-200/90 space-y-2">
            <div>
              <strong>Disclosure:</strong> An AI backtest or forward paper baseline does not prove future income.
              Option Sniffer V2 boots disarmed on every restart. Disarming stops new entries while keeping position
              management and manual square-off active.
            </div>
            <label className="flex items-start gap-2 cursor-pointer text-slate-100 font-medium">
              <input
                type="checkbox"
                checked={humanConfirmed}
                onChange={(e) => setHumanConfirmed(e.target.checked)}
                className="mt-0.5"
              />
              <span>I have reviewed open exposure, limits, and faults, and authorize live Upstox execution.</span>
            </label>
          </div>

          <div>
            <div className="rounded-xl border border-cyan-500/40 bg-cyan-950/25 p-3 mb-3 flex items-center justify-between gap-3">
              <div>
                <div className="text-[11px] font-bold uppercase tracking-wide text-cyan-200">
                  Human Confirmation Interlock Code
                </div>
                <div className="text-[11px] text-slate-300 mt-0.5">
                  Type the 4-digit session code shown here to confirm intentional human arming. Rotates after arm/disarm.
                </div>
              </div>
              <div
                data-testid="v2-live-session-pin"
                className="px-3.5 py-2 rounded-lg border border-cyan-400/50 bg-slate-950 font-mono text-lg font-extrabold tracking-[0.25em] text-cyan-200 select-all shrink-0"
              >
                {liveStatus?.current_pin || '----'}
              </div>
            </div>
            <label className="block text-xs font-semibold text-slate-300 mb-1">
              Type 4-Digit Human Confirmation Code
            </label>
            <div className="relative">
              <Lock className="w-4 h-4 text-slate-400 absolute left-3 top-2.5" />
              <input
                type="text"
                inputMode="numeric"
                maxLength={4}
                value={pin}
                onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 4))}
                placeholder="Enter 4-digit code shown above"
                className="w-full rounded-lg border border-slate-700 bg-slate-900 pl-9 pr-3 py-2 text-sm font-mono tracking-widest text-slate-100"
              />
            </div>
          </div>

          {errorMsg && (
            <div className="rounded-lg border border-rose-500/50 bg-rose-950/40 px-3 py-2 text-xs text-rose-200">
              {errorMsg}
            </div>
          )}

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-800">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-lg border border-slate-700 text-xs font-medium text-slate-300 hover:bg-slate-800"
            >
              Cancel (Stay Disarmed)
            </button>
            <button
              type="submit"
              disabled={!canArm}
              className="px-4 py-2 rounded-lg bg-rose-600 hover:bg-rose-500 disabled:opacity-40 text-xs font-bold text-white"
            >
              {submitting ? 'Verifying & Arming...' : 'Confirm Human Arm Live'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
