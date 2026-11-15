import React, { useEffect, useRef } from 'react';
import { AlertTriangle, Lock, ShieldAlert, Unlock, X } from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  formatINRValue,
  formatSignedINR,
  hasValidNumber,
} from './formatters';

const CONFIGURED_MODES = [
  {
    id: 'LONG_BOOK_ONLY',
    title: 'Long Option Buying Only',
    minMarginInr: 18000,
    description:
      'Permits cash-paid long option intents only. Short option selling intents are blocked at the bridge.',
  },
  {
    id: 'DUAL_BOOK_HEDGED',
    title: 'Dual Book • Wing-Hedged Spread',
    minMarginInr: 42000,
    description:
      'Requires an OTM protective wing order before a short option leg can be submitted.',
  },
  {
    id: 'DUAL_BOOK_NAKED',
    title: 'Dual Book • Unhedged Short Enabled',
    minMarginInr: 150000,
    description:
      'Permits unhedged short option intents (elevated tail & margin exposure; requires full SPAN margin).',
  },
];

export default function ArmInterlockModal({
  isOpen,
  liveStatus,
  armPinInput,
  onChangePinInput,
  armActionBusy,
  onSelectMode,
  onConfirmArm,
  onClose,
}) {
  const pinInputRef = useRef(null);
  const previousFocusRef = useRef(null);

  useEffect(() => {
    if (!isOpen) return undefined;
    previousFocusRef.current = document.activeElement;
    const timer = setTimeout(() => {
      pinInputRef.current?.focus();
    }, 0);

    const handleKeyDown = (e) => {
      if (e.key === 'Escape' && !armActionBusy) {
        onClose();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      clearTimeout(timer);
      window.removeEventListener('keydown', handleKeyDown);
      if (previousFocusRef.current && typeof previousFocusRef.current.focus === 'function') {
        previousFocusRef.current.focus();
      }
    };
  }, [isOpen, armActionBusy, onClose]);

  if (!isOpen) return null;

  const activeMode = liveStatus?.execution_mode || 'LONG_BOOK_ONLY';
  const expectedPin = liveStatus?.current_pin ? String(liveStatus.current_pin) : null;
  const availMargin = liveStatus?.available_margin ?? liveStatus?.readiness?.available_margin;
  const reqMargin = liveStatus?.required_margin_for_mode;
  const modeMarginOk = liveStatus?.mode_margin_ok === true;
  const liveEligible = liveStatus?.readiness?.live_trading_eligible === true;
  const blockers = Array.isArray(liveStatus?.readiness?.blockers)
    ? liveStatus.readiness.blockers
    : [];

  const maxDailyLoss = liveStatus?.circuit_breaker?.max_daily_loss_inr;
  const pinMatches = Boolean(expectedPin && armPinInput.trim() === expectedPin);
  const canArm = Boolean(liveStatus && liveEligible && modeMarginOk && pinMatches && !armActionBusy);

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
        aria-labelledby="arm-interlock-title"
        className="bg-white rounded-2xl border border-slate-300 shadow-2xl max-w-2xl w-full max-h-[92vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="p-4 sm:p-5 border-b border-slate-200 bg-slate-900 text-white flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <ShieldAlert size={18} className="text-amber-400 shrink-0" />
              <h2 id="arm-interlock-title" className="text-base font-bold">
                Human Confirmation Interlock — Live Broker Bridge
              </h2>
            </div>
            <p className="text-xs text-slate-300 mt-1">
              Manual operator confirmation required to arm live Upstox order routing. This PIN is a
              local human confirmation interlock against accidental clicks, not independent 2FA.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={armActionBusy}
            aria-label="Close broker interlock dialog"
            className="p-1.5 rounded-lg bg-slate-800 text-slate-300 hover:text-white disabled:opacity-50 cursor-pointer"
          >
            <X size={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-4 sm:p-5 space-y-4 text-xs">
          {!liveStatus && (
            <div className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-900 flex items-center gap-2 font-mono">
              <AlertTriangle size={15} className="text-rose-600 shrink-0" />
              <span>
                /live/status is currently unavailable. Arming is blocked until broker readiness and
                margin state are confirmed.
              </span>
            </div>
          )}

          {/* Airgap & Risk Disclosure */}
          <div className="p-3 rounded-lg bg-amber-50 border border-amber-200 text-amber-950 space-y-1 leading-relaxed">
            <div className="font-bold">Operational &amp; Risk Boundaries:</div>
            <ul className="list-disc pl-4 space-y-0.5">
              <li>
                Active forward research packages (<code>S205_SHADOW_FORWARD_V1</code>) carry{' '}
                <code>shadow_only = true</code> and are hard-blocked at the broker bridge (
                <code>SHADOW_TELEMETRY_AIRGAP_SKIP</code>).
              </li>
              <li>
                Configured daily loss trigger:{' '}
                <strong className="font-mono">
                  {hasValidNumber(maxDailyLoss)
                    ? formatSignedINR(-Math.abs(maxDailyLoss), { decimals: 0 })
                    : UNAVAILABLE_LABEL}
                </strong>{' '}
                • Session auto-disarm cutoff:{' '}
                <strong className="font-mono">
                  {liveStatus?.session_cutoff || '15:15 IST'}
                </strong>
                . Stop/loss triggers do not guarantee maximum loss under market gaps.
              </li>
            </ul>
          </div>

          {/* Configured Execution Mode Selector */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="font-bold text-slate-900">1. Select Configured Execution Mode</span>
              <span className="font-mono text-slate-600">
                Available Margin: <strong>{formatINRValue(availMargin)}</strong> (Req:{' '}
                {formatINRValue(reqMargin, { decimals: 0 })})
              </span>
            </div>

            <div className="grid grid-cols-1 gap-2">
              {CONFIGURED_MODES.map((m) => {
                const isSelected = activeMode === m.id;
                const marginSuffices =
                  hasValidNumber(availMargin) && Number(availMargin) >= m.minMarginInr;
                return (
                  <button
                    key={m.id}
                    type="button"
                    disabled={armActionBusy || !liveStatus}
                    onClick={() => m.id !== activeMode && onSelectMode(m.id)}
                    className={`text-left p-3 rounded-xl border transition cursor-pointer ${
                      isSelected
                        ? 'bg-indigo-50/70 border-indigo-500 ring-1 ring-indigo-500'
                        : 'bg-white border-slate-200 hover:border-slate-300'
                    }`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-bold text-slate-900">
                        {m.title} <span className="font-mono text-slate-500">({m.id})</span>
                      </span>
                      <span
                        className={`font-mono text-[11px] px-2 py-0.5 rounded ${
                          marginSuffices
                            ? 'bg-slate-100 text-slate-800'
                            : 'bg-rose-50 text-rose-800 font-semibold'
                        }`}
                      >
                        Configured Min: {formatINRValue(m.minMarginInr, { decimals: 0 })}
                      </span>
                    </div>
                    <p className="text-slate-600 mt-1">{m.description}</p>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Pre-Flight Blockers */}
          {(blockers.length > 0 || (liveStatus && (!liveEligible || !modeMarginOk))) && (
            <div className="p-3 rounded-lg bg-rose-50 border border-rose-200 text-rose-900 font-mono space-y-1">
              <div className="font-sans font-bold">Pre-Flight Blockers Active:</div>
              {blockers.map((b, i) => (
                <div key={i}>• {b}</div>
              ))}
              {!modeMarginOk && (
                <div>
                  • Available margin ({formatINRValue(availMargin)}) is below mode requirement (
                  {formatINRValue(reqMargin, { decimals: 0 })}).
                </div>
              )}
            </div>
          )}

          {/* 4-Digit Human Confirmation Interlock PIN */}
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3.5 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label htmlFor="interlock-pin-input" className="font-bold text-slate-900">
                2. Enter 4-Digit Session Interlock Code
              </label>
              <span className="font-mono text-slate-700">
                Session Code:{' '}
                <strong className="px-2 py-0.5 rounded bg-white border border-slate-300 text-slate-900 tracking-widest">
                  {expectedPin || UNAVAILABLE_LABEL}
                </strong>
              </span>
            </div>
            <input
              ref={pinInputRef}
              id="interlock-pin-input"
              type="text"
              inputMode="numeric"
              autoComplete="off"
              maxLength={4}
              value={armPinInput}
              onChange={(e) => onChangePinInput(e.target.value.replace(/\D/g, '').slice(0, 4))}
              placeholder="Enter 4-digit code shown above"
              className="w-full px-3 py-2 rounded-lg bg-white border border-slate-300 font-mono text-sm tracking-widest text-slate-900"
            />
          </div>

          {/* Footer Buttons */}
          <div className="flex items-center justify-end gap-2.5 pt-2 border-t border-slate-200">
            <button
              type="button"
              onClick={onClose}
              disabled={armActionBusy}
              className="px-4 py-2 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-800 font-semibold cursor-pointer"
            >
              Keep Disarmed (Paper Only)
            </button>
            <button
              type="submit"
              disabled={!canArm}
              className="px-4 py-2 rounded-lg bg-rose-600 hover:bg-rose-500 disabled:opacity-40 text-white font-semibold inline-flex items-center gap-1.5 cursor-pointer"
            >
              {canArm ? <Unlock size={14} /> : <Lock size={14} />}
              <span>{armActionBusy ? 'Confirming...' : `Arm ${activeMode}`}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
