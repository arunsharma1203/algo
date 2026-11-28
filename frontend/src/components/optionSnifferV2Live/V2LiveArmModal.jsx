import React, { useState, useEffect, useRef } from 'react';
import { Shield, ShieldAlert, Lock, X, AlertTriangle, CheckCircle2, RefreshCw } from 'lucide-react';
import {
  postV2LiveArm,
  postV2LiveArmChallenge,
  postV2LiveDisarm,
  postV2PaperMode,
  fetchV2LiveFundsPreview,
} from './v2LiveApiClient';

const MODE_CARDS = [
  {
    id: 'PAPER_ONLY',
    title: 'Paper Only',
    minMarginInr: 0,
    desc: 'Runs paper simulation & live L5 telemetry without placing any broker orders.',
    books: [],
    scope: 'PAPER_ONLY',
  },
  {
    id: 'BOOK_B_ONLY',
    title: 'Live • Book B Only',
    minMarginInr: 25000,
    desc: 'Single-leg Paid Convexity (VRP <= 1.15, 45m exit, mandatory Upstox V3 GTT stop).',
    books: ['V2_BOOK_B_PAID_FLOW'],
    scope: 'BOOK_B_ONLY',
  },
  {
    id: 'BOOK_A_ONLY',
    title: 'Live • Book A Only',
    minMarginInr: 75000,
    desc: 'Friday Intraday 50/100w Spread (Buy-Wing-First + SPAN check + 24pt min credit).',
    books: ['V2_BOOK_A_INTRADAY_CREDIT'],
    scope: 'BOOK_A_ONLY',
  },
  {
    id: 'AUTO_DUAL',
    title: 'Live • Auto Book A + B',
    minMarginInr: 85000,
    desc: 'Dual-book live routing under anti-self-hedge guard and SPAN margin buffer.',
    books: ['V2_BOOK_B_PAID_FLOW', 'V2_BOOK_A_INTRADAY_CREDIT'],
    scope: 'AUTO_DUAL',
  },
];

const paperToScope = (pm) => {
  const s = String(pm || 'AUTO').toUpperCase();
  if (s === 'BOOK_A_ONLY') return 'BOOK_A_ONLY';
  if (s === 'BOOK_B_ONLY') return 'BOOK_B_ONLY';
  return 'AUTO_DUAL';
};

const scopeToPaper = (sc) => {
  if (sc === 'BOOK_A_ONLY') return 'BOOK_A_ONLY';
  if (sc === 'BOOK_B_ONLY') return 'BOOK_B_ONLY';
  return 'AUTO';
};

export default function V2LiveArmModal({
  isOpen,
  onClose,
  overview,
  liveStatus,
  fundsPreview,
  onCompleted,
  onRefreshStatus,
}) {
  const [selectedMode, setSelectedMode] = useState('BOOK_B_ONLY');
  const [maxLots, setMaxLots] = useState(1);
  const [maxDailyLoss, setMaxDailyLoss] = useState(2500);
  const [dailyLossEdited, setDailyLossEdited] = useState(false);
  const [maxOpenPkgs, setMaxOpenPkgs] = useState(1);
  const [pinInput, setPinInput] = useState('');
  const [humanConfirmed, setHumanConfirmed] = useState(false);
  const [localFunds, setLocalFunds] = useState(null);
  const [challengeState, setChallengeState] = useState({ pin: null, pinMode: null });
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
    setDailyLossEdited(false);
    if (liveStatus?.armed) {
      const lotsInit = Number(liveStatus?.arm_state?.max_lots_per_book || 1);
      setSelectedMode(liveStatus?.live_book_scope || 'BOOK_B_ONLY');
      setMaxLots(lotsInit);
      setMaxDailyLoss(Number(liveStatus?.arm_state?.max_daily_loss_inr || 2500 * lotsInit));
      setMaxOpenPkgs(Number(liveStatus?.arm_state?.max_open_packages || 1));
    } else {
      const initScope = paperToScope(overview?.paper_execution_mode);
      const lotsInit = Number(overview?.lots_per_book || 1);
      setSelectedMode(initScope);
      setMaxLots(lotsInit);
      setMaxDailyLoss(2500 * lotsInit);
      setMaxOpenPkgs(initScope === 'AUTO_DUAL' ? 2 : 1);
    }
    fetchV2LiveFundsPreview(false)
      .then((res) => setLocalFunds(res))
      .catch(() => {});
    postV2LiveArmChallenge()
      .then((ch) =>
        setChallengeState({
          pin: ch?.challenge_pin ? String(ch.challenge_pin) : ch?.current_pin ? String(ch.current_pin) : null,
          pinMode: ch?.pin_delivery || ch?.pin_mode || null,
        })
      )
      .catch(() => setChallengeState({ pin: null, pinMode: 'OUT_OF_BAND_REQUIRED' }));
  }, [isOpen, liveStatus, overview]);

  if (!isOpen) return null;

  const activeCard = MODE_CARDS.find((c) => c.id === selectedMode) || MODE_CARDS[0];
  const isPaperSelected = activeCard.id === 'PAPER_ONLY';
  const expectedPin = challengeState.pin || (liveStatus?.current_pin ? String(liveStatus.current_pin) : null);
  const pinMatchOk = expectedPin
    ? pinInput.trim() === expectedPin
    : /^\d{4}$/.test(pinInput.trim());
  const faults = liveStatus?.unresolved_faults || { total_blocking_faults: 0 };

  const effFunds = localFunds || fundsPreview || null;
  const availMargin =
    effFunds?.available_margin_inr ?? liveStatus?.available_margin_inr ?? null;
  const reqMarginInr = activeCard.minMarginInr * maxLots;
  const marginInsufficient =
    !isPaperSelected &&
    availMargin !== null &&
    availMargin !== undefined &&
    Number(availMargin) < reqMarginInr;

  const canSubmit = isPaperSelected
    ? !submitting
    : Boolean(liveStatus) &&
      faults.total_blocking_faults === 0 &&
      !marginInsufficient &&
      humanConfirmed &&
      pinMatchOk &&
      !submitting;

  const handleLotsChange = (rawVal) => {
    const nextLots = Math.max(1, Math.min(5, Number(rawVal) || 1));
    setMaxLots(nextLots);
    if (!dailyLossEdited) {
      setMaxDailyLoss(2500 * nextLots);
    }
  };

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
        await postV2PaperMode(scopeToPaper(activeCard.scope), Number(maxLots)).catch(() => {});
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
    if (refreshing) return;
    setRefreshing(true);
    try {
      const [f, ch] = await Promise.all([
        fetchV2LiveFundsPreview(true).catch(() => null),
        postV2LiveArmChallenge().catch(() => null),
      ]);
      if (f) setLocalFunds(f);
      if (ch) {
        setChallengeState({
          pin: ch?.challenge_pin ? String(ch.challenge_pin) : ch?.current_pin ? String(ch.current_pin) : null,
          pinMode: ch?.pin_delivery || ch?.pin_mode || null,
        });
      }
      if (onRefreshStatus) await onRefreshStatus();
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
            <button
              type="button"
              onClick={handleRefresh}
              disabled={refreshing}
              className="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300"
              title="Refresh PIN, broker margin, and fault state"
            >
              <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
            </button>
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
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Select Execution Mode ({maxLots} {maxLots === 1 ? 'Lot' : 'Lots'} = {maxLots * 65} Qty)
              </div>
              {availMargin !== null && availMargin !== undefined && (
                <div className="text-[11px] font-semibold text-sky-300">
                  Upstox Available: &#8377;{Number(availMargin).toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                </div>
              )}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              {MODE_CARDS.map((card) => {
                const selected = selectedMode === card.id;
                const cardReqInr = card.minMarginInr * maxLots;
                const cardAfford =
                  cardReqInr === 0 ||
                  availMargin === null ||
                  availMargin === undefined ||
                  Number(availMargin) >= cardReqInr;
                const dynamicLabel =
                  card.minMarginInr === 0
                    ? '₹0 (0 Live Orders)'
                    : `₹${cardReqInr.toLocaleString('en-IN')} min (${maxLots}L)`;
                return (
                  <button
                    key={card.id}
                    type="button"
                    onClick={() => {
                      setSelectedMode(card.id);
                      if (card.id === 'AUTO_DUAL' && maxOpenPkgs < 2) setMaxOpenPkgs(2);
                      if ((card.id === 'BOOK_A_ONLY' || card.id === 'BOOK_B_ONLY') && maxOpenPkgs > 1) setMaxOpenPkgs(1);
                    }}
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
                      <span
                        className={`px-2 py-0.5 text-[10px] font-semibold rounded border ${
                          cardReqInr === 0
                            ? 'bg-slate-800 text-slate-300 border-slate-700'
                            : cardAfford
                            ? 'bg-emerald-950/70 text-emerald-300 border-emerald-700/60'
                            : 'bg-rose-950/70 text-rose-300 border-rose-700/60'
                        }`}
                      >
                        {dynamicLabel}
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

              {/* Risk & Capital Controls */}
              <div className="grid grid-cols-3 gap-3 bg-slate-950/60 border border-slate-800 rounded-lg p-3">
                <label className="text-xs text-slate-300">
                  <span className="block text-[11px] text-slate-400 mb-1">Lots / Book (1-5)</span>
                  <input
                    type="number"
                    min={1}
                    max={5}
                    value={maxLots}
                    onChange={(e) => handleLotsChange(e.target.value)}
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
                    onChange={(e) => {
                      setDailyLossEdited(true);
                      setMaxDailyLoss(Math.max(500, Number(e.target.value) || 2500));
                    }}
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

              {/* Capital, Margin & Stop Microstructure Readiness Summary */}
              <div
                className={`p-3 rounded-lg border text-xs space-y-1.5 ${
                  marginInsufficient
                    ? 'bg-rose-950/50 border-rose-700 text-rose-200'
                    : 'bg-slate-950/70 border-slate-800 text-slate-300'
                }`}
              >
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <span className="font-semibold">
                    Required Margin ({maxLots}L): &#8377;{reqMarginInr.toLocaleString('en-IN')}
                  </span>
                  <span className="font-semibold">
                    {availMargin !== null && availMargin !== undefined
                      ? marginInsufficient
                        ? `INSUFFICIENT BY ₹${(reqMarginInr - Number(availMargin)).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
                        : `READY (+₹${(Number(availMargin) - reqMarginInr).toLocaleString('en-IN', { maximumFractionDigits: 0 })} buffer)`
                      : 'Margin verified at submit'}
                  </span>
                </div>
                <div className="text-[11px] text-slate-400 space-y-1">
                  {(activeCard.id === 'BOOK_B_ONLY' || activeCard.id === 'AUTO_DUAL') && (
                    <div>
                      Book B Outlay Cap: &#8377;{(18500 * maxLots).toLocaleString('en-IN')} (enforced at live limit collar) &bull; Exchange V3 GTT Stop derived from actual fill (&#8377;{(1800 * maxLots).toLocaleString('en-IN')} trigger budget; gap/slippage loss bounded by premium paid).
                    </div>
                  )}
                  {(activeCard.id === 'BOOK_A_ONLY' || activeCard.id === 'AUTO_DUAL') && (
                    <div>
                      Book A 100pt Wing Structural Cap: &#8377;{(4875 * maxLots).toLocaleString('en-IN')} &bull; Close-only 1m stop trigger budget &#8377;{(1950 * maxLots).toLocaleString('en-IN')} + 5s live guardian (actual gap/fill loss can exceed trigger budget up to wing width).
                    </div>
                  )}
                </div>
              </div>

              {/* 4-Digit Human Session PIN Interlock */}
              <div className="p-3.5 rounded-lg bg-slate-950/80 border border-slate-800 space-y-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 text-xs text-slate-300">
                    <Lock className="h-4 w-4 text-amber-400" />
                    <span>4-Digit Session Security PIN</span>
                  </div>
                  <div className="px-2.5 py-1 rounded bg-slate-900 border border-slate-700 font-mono text-sm font-bold tracking-widest text-amber-300">
                    {expectedPin || (challengeState.pinMode === 'OUT_OF_BAND_TERMINAL_ONLY' ? 'SERVER LOG' : '----')}
                  </div>
                </div>
                <input
                  type="text"
                  inputMode="numeric"
                  maxLength={4}
                  placeholder={expectedPin ? 'Type the 4-digit PIN shown above' : 'Enter 4-digit PIN from backend startup log'}
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
                    I explicitly authorize live Upstox orders for <strong>{activeCard.title}</strong> ({maxLots} {maxLots === 1 ? 'lot' : 'lots'} / {maxLots * 65} qty) with verified minimum margin &#8377;{reqMarginInr.toLocaleString('en-IN')}.
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
