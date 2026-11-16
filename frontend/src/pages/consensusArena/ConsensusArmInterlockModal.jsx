import React, { useEffect, useRef } from 'react';
import { AlertTriangle, Lock, ShieldAlert, Unlock, X } from 'lucide-react';
import { formatINRValue } from '../optionSnifferAdvance/formatters';

const ARENA_EXECUTION_MODES = [
  {
    id: 'DUAL_FUNDED_CASH_ARENA',
    title: 'Dual Funded-Cash Arena (Recommended)',
    description:
      'Unites Book 1 (20d Scheduled F&O-181 Cash CNC Core + Macro Exposure Shield) with Book 2 (15m Post-Only Limit Sniffer using strictly unreserved idle cash). Zero leverage.',
  },
  {
    id: 'BOOK1_SWING_CNC_ONLY',
    title: 'Book 1 Only • 20d Positional Cash Delivery (CNC)',
    description:
      'Executes only scheduled 20-trading-day F&O-181 cash delivery rebalances with Top-8 / Rank-78% hysteresis and Macro Breadth Shield.',
  },
  {
    id: 'BOOK2_INTRADAY_MIS_SNIFFER',
    title: 'Book 2 Only • 15m F&O-181 Post-Only Limit Sniffer (MIS)',
    description:
      'Executes only 09:30–12:30 IST post-only passive limit orders on F&O-181 PDH bull-traps with mandatory 15:15 IST square-off.',
  },
];

export default function ConsensusArmInterlockModal({
  isOpen,
  liveStatus,
  armPinInput,
  onChangePinInput,
  armActionBusy,
  onSelectMode,
  onConfirmArm,
  onClose,
}) {
  const pinRef = useRef(null);

  useEffect(() => {
    if (!isOpen) return undefined;
    const t = setTimeout(() => pinRef.current?.focus(), 0);
    const onKey = (e) => {
      if (e.key === 'Escape' && !armActionBusy) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      clearTimeout(t);
      window.removeEventListener('keydown', onKey);
    };
  }, [isOpen, armActionBusy, onClose]);

  if (!isOpen) return null;

  const activeMode = liveStatus?.execution_mode || 'DUAL_FUNDED_CASH_ARENA';
  const expectedPin = liveStatus?.current_pin ? String(liveStatus.current_pin) : null;
  const availMargin = liveStatus?.available_margin;
  const reqMargin = liveStatus?.required_margin_for_mode;
  const marginOk = liveStatus?.mode_margin_ok === true;
  const eligible = liveStatus?.readiness?.live_trading_eligible === true;
  const blockers = Array.isArray(liveStatus?.readiness?.blockers)
    ? liveStatus.readiness.blockers
    : [];
  const maxDailyLoss = liveStatus?.circuit_breaker?.max_daily_loss_inr;
  const pinMatches = Boolean(expectedPin && armPinInput.trim() === expectedPin);
  const canArm = Boolean(liveStatus && eligible && marginOk && pinMatches && !armActionBusy);

  const handleSubmit = (e) => {
    e.preventDefault();
    if (!canArm) return;
    onConfirmArm();
  };

  return (
    <div
      className="fixed inset-0 z-50 bg-slate-950/70 flex items-center justify-center p-4"
      onClick={() => !armActionBusy && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="consensus-arm-title"
        className="bg-white rounded-2xl border border-slate-300 shadow-2xl max-w-2xl w-full max-h-[92vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-4 sm:p-5 border-b border-slate-200 bg-slate-900 text-white flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <ShieldAlert size={18} className="text-amber-400 shrink-0" />
              <h2 id="consensus-arm-title" className="text-base font-bold">
                Human Confirmation Interlock — Consensus Arena Live Equity Bridge
              </h2>
            </div>
            <p className="text-xs text-slate-300 mt-1">
              Exclusive human operator control (`AGENTS.md` Rule 6). Boots strictly in Paper Shadow mode
              (`simulation_mode = true`). Recommended: complete 60 prospective shadow sessions before arming real cash.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={armActionBusy}
            className="p-1.5 rounded-lg bg-slate-800 text-slate-300 hover:text-white disabled:opacity-50 cursor-pointer"
          >
            <X size={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-4 sm:p-5 space-y-4 text-xs">
          {/* Mode Selector */}
          <div className="space-y-2">
            <label className="block font-bold uppercase tracking-wider text-slate-700">
              1. Select Funded-Cash Execution Book Mode (0x Leverage • Whole Shares Only)
            </label>
            <div className="grid grid-cols-1 gap-2">
              {ARENA_EXECUTION_MODES.map((m) => {
                const selected = activeMode === m.id;
                return (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() => onSelectMode(m.id)}
                    disabled={armActionBusy}
                    className={`text-left p-3 rounded-xl border transition-all cursor-pointer ${
                      selected
                        ? 'border-slate-900 bg-slate-900 text-white'
                        : 'border-slate-200 bg-slate-50 hover:bg-slate-100 text-slate-800'
                    }`}
                  >
                    <div className="font-bold text-xs">{m.title}</div>
                    <div className={`text-[11px] mt-0.5 ${selected ? 'text-slate-300' : 'text-slate-600'}`}>
                      {m.description}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Pre-Flight Readiness & Cash Reservation Summary */}
          <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 space-y-2 font-mono">
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Arena Funded Cash Requirement:</span>
              <span className="font-bold text-slate-900">{formatINRValue(reqMargin)}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Upstox Available Wallet Cash:</span>
              <span className={`font-bold ${marginOk ? 'text-emerald-700' : 'text-rose-700'}`}>
                {formatINRValue(availMargin)}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Intraday Realized Loss Halt (-1.25%):</span>
              <span className="font-bold text-rose-700">{formatINRValue(maxDailyLoss)}</span>
            </div>
          </div>

          {blockers.length > 0 && (
            <div className="p-3 rounded-xl bg-rose-50 border border-rose-200 text-rose-900 space-y-1">
              <div className="font-bold flex items-center gap-1.5">
                <AlertTriangle size={14} className="text-rose-600 shrink-0" />
                <span>Active Readiness Blockers (Arming Locked):</span>
              </div>
              <ul className="list-disc list-inside font-mono text-[11px] space-y-0.5">
                {blockers.map((b, idx) => (
                  <li key={idx}>{b}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Human Confirmation PIN */}
          <div className="p-3.5 rounded-xl bg-amber-50/80 border border-amber-200 space-y-2">
            <div className="flex items-center justify-between">
              <span className="font-bold text-amber-950">
                2. Enter Local Confirmation Challenge PIN:
              </span>
              <span className="px-2.5 py-1 rounded bg-slate-900 text-amber-300 font-mono font-bold tracking-widest text-sm">
                {expectedPin || '----'}
              </span>
            </div>
            <input
              ref={pinRef}
              type="text"
              inputMode="numeric"
              maxLength={4}
              value={armPinInput}
              onChange={(e) => onChangePinInput(e.target.value.replace(/\D/g, '').slice(0, 4))}
              placeholder="Type 4-digit PIN above to confirm human intent"
              className="w-full px-3 py-2 rounded-lg border border-amber-300 bg-white text-slate-900 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-slate-900"
            />
          </div>

          <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-200">
            <button
              type="button"
              onClick={onClose}
              disabled={armActionBusy}
              className="px-4 py-2 rounded-lg border border-slate-300 bg-white text-slate-700 font-semibold hover:bg-slate-50 cursor-pointer"
            >
              Keep in Paper Shadow
            </button>
            <button
              type="submit"
              disabled={!canArm}
              className={`inline-flex items-center gap-1.5 px-4 py-2 rounded-lg font-bold text-white transition-all ${
                canArm
                  ? 'bg-rose-600 hover:bg-rose-500 cursor-pointer'
                  : 'bg-slate-300 text-slate-500 cursor-not-allowed'
              }`}
            >
              {canArm ? <Unlock size={14} /> : <Lock size={14} />}
              <span>{armActionBusy ? 'Arming...' : 'Confirm & Arm Live Bridge'}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
