import React, { useState, useEffect, useRef } from 'react';
import { Shield, ShieldAlert, Lock, X, AlertTriangle, CheckCircle2, RefreshCw } from 'lucide-react';
import { postV2LiveArm, postV2LiveDisarm } from './v2LiveApiClient';

const MODE_CARDS = [
  {
    id: 'PAPER_ONLY',
    title: 'Paper Only',
    minMarginInr: 0,
    minLabel: '₹0 (0 Live Orders)',
    desc: 'Runs paper simulation & live L5 telemetry without placing any broker orders.',
    books: [],
    scope: 'PAPER_ONLY',
  },
  {
    id: 'BOOK_B_ONLY',
    title: 'Live • Book B Only',
    minMarginInr: 25000,
    minLabel: '₹25k min margin',
    desc: 'Single-leg Paid Convexity (VRP <= 1.15, 45m exit, mandatory V3 GTT stop).',
    books: ['V2_BOOK_B_PAID_FLOW'],
    scope: 'BOOK_B_ONLY',
  },
  {
    id: 'BOOK_A_ONLY',
    title: 'Live • Book A Only',
    minMarginInr: 75000,
    minLabel: '₹75k min margin',
    desc: 'Friday Intraday 50/100w Spread (Buy-Wing-First + SPAN check + 24pt min credit).',
    books: ['V2_BOOK_A_INTRADAY_CREDIT'],
    scope: 'BOOK_A_ONLY',
  },
  {
    id: 'AUTO_DUAL',
    title: 'Live • Auto Book A + B',
    minMarginInr: 85000,
    minLabel: '₹85k min margin',
    desc: 'Dual-book live routing under anti-self-hedge guard and SPAN margin buffer.',
    books: ['V2_BOOK_B_PAID_FLOW', 'V2_BOOK_A_INTRADAY_CREDIT'],
    scope: 'AUTO_DUAL',
  },
];

export default function V2LiveArmModal({
  isOpen,
  onClose,
  liveStatus,
  onCompleted,
  onRefreshStatus,
}) {
  const [selectedMode, setSelectedMode] = useState('BOOK_B_ONLY');
  const [maxLots, setMaxLots] = useState(1);
  const [maxDailyLoss, setMaxDailyLoss] = useState(2500);
  const [maxOpenPkgs, setMaxOpenPkgs] = useState(1);
  const [pinInput, setPinInput] = useState('');
  const [humanConfirmed, setHumanConfirmed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');

  const prevOpenRef = useRef(false);

  useEffect(() => {
    const wasOpen = prevOpenRef.current;
    prevOpenRef.current = Boolean(isOpen);
    if (!isOpen || wasOpen) return;
    setPinInput('');
    setHumanConfirmed(false);
    setErrorMsg('');
    if (liveStatus?.armed) {
      setSelectedMode(liveStatus?.live_book_scope || 'BOOK_B_ONLY');
      setMaxLots(liveStatus?.arm_state?.max_lots_per_book || 1);
      setMaxDailyLoss(liveStatus?.arm_state?.max_daily_loss_inr || 2500);
      setMaxOpenPkgs(liveStatus?.arm_state?.max_open_packages || 1);
    } else {
      setSelectedMode('BOOK_B_ONLY');
    }
  }, [isOpen, liveStatus]);

  if (!isOpen) return null;

  const activeCard = MODE_CARDS.find((c) => c.id === selectedMode) || MODE_CARDS[0];
  const isPaperSelected = activeCard.id === 'PAPER_ONLY';
  const expectedPin = liveStatus?.current_pin ? String(liveStatus.current_pin) : null;
  const faults = liveStatus?.unresolved_faults || { total_blocking_faults: 0 };

  const canSubmit = isPaperSelected
    ? !submitting
    : Boolean(liveStatus) &&
      Boolean(expectedPin) &&
      faults.total_blocking_faults === 0 &&
      humanConfirmed &&
      pinInput.trim() === expectedPin &&
      !submitting;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setErrorMsg('');
    try {
      if (isPaperSelected) {
        if (liveStatus?.armed) {
          await postV2LiveDisarm('HUMAN_SWITCHED_TO_PAPER_ONLY');
        }
      } else {
        await postV2LiveArm({
          pin: pinInput.trim(),
          human_confirmed: humanConfirmed,
          selected_books: activeCard.books,
          live_book_scope: activeCard.scope,
          protocol_profile_id: 'V2_4_PAPER_DUAL_BOOK',
          max_lots_per_book: Number(maxLots),
          max_daily_loss_inr: Number(maxDailyLoss),
          max_open_packages: Number(maxOpenPkgs),
        });
      }
      if (onCompleted) await onCompleted();
      onClose();
    } catch (err) {
      setErrorMsg(err?.message || 'Failed to update execution mode');
    } finally {
      setSubmitting(false);
    }
  };

  const handleRefresh = async () => {
    if (!onRefreshStatus || refreshing) return;
    setRefreshing(true);
    try {
      await onRefreshStatus();
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 backdrop-blur-sm p-4">
      <div className="w-full max-w-2xl bg-slate-900 border border-slate-700 rounded-xl shadow-2xl overflow-hidden text-slate-100">
        <div className="flex items-center justify-between px-5 py-3.5 bg-slate-950 border-b border-slate-800">
          <div className="flex items-center gap-2.5">
            {isPaperSelected ? (
              <Shield className="h-5 w-5 text-emerald-400" />
            ) : (
              <ShieldAlert className="h-5 w-5 text-amber-400" />
            )}
            <h2 className="text-sm sm:text-base font-bold text-white">
              V2 Execution &amp; Live Safety Interlock
            </h2>
          </div>
          <div className="flex items-center gap-2">
            {onRefreshStatus && (
              <button
                type="button"
                onClick={handleRefresh}
                disabled={refreshing}
                className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300"
                title="Refresh PIN and fault state"
              >
                <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-400 hover:text-white"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="p-5 space-y-4 max-h-[82vh] overflow-y-auto">
          {/* 4 Clickable Execution Mode Cards */}
          <div>
            <div className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">
              Select Execution Mode
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              {MODE_CARDS.map((card) => {
                const selected = selectedMode === card.id;
                return (
                  <button
                    key={card.id}
                    type="button"
                    onClick={() => setSelectedMode(card.id)}
                    className={`text-left p-3 rounded-lg border transition ${
                      selected
                        ? card.id === 'PAPER_ONLY'
                          ? 'bg-emerald-950/50 border-emerald-500 ring-1 ring-emerald-500/40'
                          : 'bg-amber-950/40 border-amber-500 ring-1 ring-amber-500/40'
                        : 'bg-slate-950/70 border-slate-800 hover:border-slate-700'
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <span className="text-xs font-bold text-white">{card.title}</span>
                      <span className="px-2 py-0.5 text-[10px] font-semibold rounded bg-slate-800 text-slate-300 border border-slate-700">
                        {card.minLabel}
                      </span>
                    </div>
                    <p className="text-[11px] text-slate-400 leading-relaxed">{card.desc}</p>
                  </button>
                );
              })}
            </div>
          </div>

          {!isPaperSelected && (
            <>
              {faults.total_blocking_faults > 0 && (
                <div className="p-3 rounded-lg bg-rose-950/60 border border-rose-700 text-xs text-rose-200 flex items-start gap-2">
                  <AlertTriangle className="h-4 w-4 text-rose-400 shrink-0 mt-0.5" />
                  <span>
                    {faults.total_blocking_faults} unresolved live fault(s) present. Run broker reconciliation before arming.
                  </span>
                </div>
              )}

              {/* Risk Controls */}
              <div className="grid grid-cols-3 gap-3 bg-slate-950/60 border border-slate-800 rounded-lg p-3">
                <label className="text-xs text-slate-300">
                  <span className="block text-[11px] text-slate-400 mb-1">Lots / Book (1-5)</span>
                  <input
                    type="number"
                    min={1}
                    max={5}
                    value={maxLots}
                    onChange={(e) => setMaxLots(Math.max(1, Math.min(5, Number(e.target.value) || 1)))}
                    className="w-full px-2.5 py-1.5 rounded bg-slate-900 border border-slate-700 text-white text-xs"
                  />
                </label>
                <label className="text-xs text-slate-300">
                  <span className="block text-[11px] text-slate-400 mb-1">Max Daily Loss (&#8377;)</span>
                  <input
                    type="number"
                    min={500}
                    step={250}
                    value={maxDailyLoss}
                    onChange={(e) => setMaxDailyLoss(Math.max(500, Number(e.target.value) || 2500))}
                    className="w-full px-2.5 py-1.5 rounded bg-slate-900 border border-slate-700 text-white text-xs"
                  />
                </label>
                <label className="text-xs text-slate-300">
                  <span className="block text-[11px] text-slate-400 mb-1">Max Open Slots</span>
                  <input
                    type="number"
                    min={1}
                    max={2}
                    value={maxOpenPkgs}
                    onChange={(e) => setMaxOpenPkgs(Math.max(1, Math.min(2, Number(e.target.value) || 1)))}
                    className="w-full px-2.5 py-1.5 rounded bg-slate-900 border border-slate-700 text-white text-xs"
                  />
                </label>
              </div>

              {/* 4-Digit Human Session PIN Interlock */}
              <div className="p-3.5 rounded-lg bg-slate-950/80 border border-slate-800 space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 text-xs text-slate-300">
                    <Lock className="h-4 w-4 text-amber-400" />
                    <span>4-Digit Session Security PIN</span>
                  </div>
                  <div className="px-2.5 py-1 rounded bg-slate-900 border border-slate-700 font-mono text-sm font-bold tracking-widest text-amber-300">
                    {expectedPin || '----'}
                  </div>
                </div>
                <input
                  type="text"
                  inputMode="numeric"
                  maxLength={4}
                  placeholder="Type the 4-digit PIN shown above"
                  value={pinInput}
                  onChange={(e) => setPinInput(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700 text-white text-sm font-mono tracking-widest focus:outline-none focus:border-amber-500"
                />
                <label className="flex items-start gap-2.5 text-xs text-slate-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={humanConfirmed}
                    onChange={(e) => setHumanConfirmed(e.target.checked)}
                    className="mt-0.5 rounded border-slate-600 bg-slate-900 text-amber-500"
                  />
                  <span>
                    I explicitly authorize live Upstox orders for <strong>{activeCard.title}</strong> with verified minimum margin &#8377;{(activeCard.minMarginInr * (activeCard.id === 'BOOK_B_ONLY' ? 1 : maxLots)).toLocaleString('en-IN')}.
                  </span>
                </label>
              </div>
            </>
          )}

          {errorMsg && (
            <div className="p-2.5 rounded-lg bg-rose-950/60 border border-rose-700 text-xs text-rose-200">
              {errorMsg}
            </div>
          )}

          <div className="flex items-center justify-end gap-2.5 pt-2 border-t border-slate-800">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-xs font-semibold rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              className={`inline-flex items-center gap-1.5 px-4 py-2 text-xs font-bold rounded-lg transition disabled:opacity-40 ${
                isPaperSelected
                  ? 'bg-emerald-600 hover:bg-emerald-500 text-white'
                  : 'bg-amber-600 hover:bg-amber-500 text-white'
              }`}
            >
              <CheckCircle2 className="h-3.5 w-3.5" />
              {submitting
                ? 'Applying...'
                : isPaperSelected
                ? liveStatus?.armed
                  ? 'Switch to Paper Only (Disarm Live)'
                  : 'Confirm Paper Baseline'
                : `Arm ${activeCard.title}`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
