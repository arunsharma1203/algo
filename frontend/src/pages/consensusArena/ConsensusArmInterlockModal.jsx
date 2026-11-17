import React, { useEffect, useRef } from 'react';
import { AlertTriangle, Lock, ShieldAlert, ShieldCheck, Unlock, X } from 'lucide-react';
import { formatINRValue } from '../optionSnifferAdvance/formatters';

const ARENA_EXECUTION_MODES = [
  {
    id: 'BOOK1_SWING_CNC_ONLY',
    title: 'Book 1 Only • 20d Positional Cash Delivery (CNC • Recommended Monday Start)',
    description:
      'Executes scheduled 20-day F&O-181 cash delivery rebalances + 4-Gate top-up deployment + SMA50/SMA200/FinBERT breakdown protection. Hard-locked to 1.0x Cash (0x leverage). Book 2 continues in Paper Shadow.',
  },
  {
    id: 'DUAL_FUNDED_CASH_ARENA',
    title: 'Dual Funded-Cash Arena (Book 1 CNC + Book 2 Intraday MIS)',
    description:
      'Unites Book 1 (1.0x Cash CNC Core) with Book 2 (15m Post-Only Limit MIS Sniffer) using a fenced 20% intraday cash reserve so swing holdings never starve intraday margin.',
  },
  {
    id: 'BOOK2_INTRADAY_MIS_SNIFFER',
    title: 'Book 2 Only • 15m F&O-181 Post-Only Limit Sniffer (MIS)',
    description:
      'Executes only 09:30–12:30 IST post-only passive limit orders on F&O-181 PDH/PDL failed sweeps with mandatory 15:15 IST square-off. Recommended after 60 shadow sessions.',
  },
];

export default function ConsensusArmInterlockModal({
  isOpen,
  liveStatus,
  armPinInput,
  onChangePinInput,
  armActionBusy,
  capitalInput,
  marginModeInput,
  snifferReserveInput,
  ddpiConfirmedInput,
  onChangeCapital,
  onChangeMarginMode,
  onChangeSnifferReserve,
  onChangeDdpiConfirmed,
  onSaveCapitalConfig,
  configBusy,
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
  const isCncOnlyMode = activeMode === 'BOOK1_SWING_CNC_ONLY';
  const effectiveMarginMode = isCncOnlyMode ? 'CASH_1X' : marginModeInput || 'CASH_1X';

  const expectedPin = liveStatus?.current_pin ? String(liveStatus.current_pin) : null;
  const availMargin = liveStatus?.available_margin;
  const reqMargin = liveStatus?.required_margin_for_mode;
  const configuredSleeveCap = liveStatus?.configured_sleeve_capital_inr ?? liveStatus?.accounting?.funded_capital_inr;
  const liveInvestedEq = liveStatus?.live_invested_equity_inr || 0;
  const snifferFw = liveStatus?.option_sniffer_firewall || {};
  const shadowGate = liveStatus?.shadow_gate_progress || {};
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

  const fenceNum = Math.max(0, Number(snifferReserveInput) || 0);
  const maxAllocatableAfterFence =
    typeof availMargin === 'number' ? Math.max(10000, Math.floor(availMargin - fenceNum)) : null;

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
              (`simulation_mode = true`). Live `LIVE_UPSTOX` ledger is isolated from Paper Shadow positions.
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
              1. Select Funded-Cash Execution Book Mode (Whole Shares Only)
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

          {/* Inline Wallet Capital, CNC 0x-Leverage Lock & Option Sniffer Fence Configurator */}
          <div className="p-3.5 rounded-xl bg-indigo-50/60 border border-indigo-200 space-y-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-bold uppercase tracking-wider text-indigo-950">
                2. Equity Sub-Wallet, Leverage Lock &amp; Option Sniffer Margin Fence
              </span>
              {typeof availMargin === 'number' && availMargin >= 10000 && onChangeCapital && (
                <button
                  type="button"
                  onClick={() => onChangeCapital(String(maxAllocatableAfterFence))}
                  className="px-2 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-white text-[10px] font-bold cursor-pointer"
                >
                  Use Free Upstox After Fence ({formatINRValue(maxAllocatableAfterFence, { decimals: 0 })})
                </button>
              )}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              <div>
                <label className="block text-[10px] font-semibold text-slate-600 mb-1">
                  Equity Sub-Wallet Capital (₹ • e.g. 50,000)
                </label>
                <input
                  type="number"
                  min="10000"
                  step="1000"
                  value={capitalInput ?? ''}
                  onChange={(e) => onChangeCapital && onChangeCapital(e.target.value)}
                  className="w-full px-2.5 py-1.5 rounded-lg border border-slate-300 bg-white text-slate-900 font-mono text-xs"
                />
              </div>
              <div>
                <label className="block text-[10px] font-semibold text-slate-600 mb-1">
                  Option Sniffer Reserved Margin Fence (₹ Protected)
                </label>
                <input
                  type="number"
                  min="0"
                  step="5000"
                  value={snifferReserveInput ?? '0'}
                  onChange={(e) => onChangeSnifferReserve && onChangeSnifferReserve(e.target.value)}
                  placeholder="e.g. 90000 to protect Option Sniffer"
                  className="w-full px-2.5 py-1.5 rounded-lg border border-slate-300 bg-white text-slate-900 font-mono text-xs"
                />
              </div>
              <div>
                <label className="block text-[10px] font-semibold text-slate-600 mb-1">
                  {isCncOnlyMode
                    ? 'CNC Delivery Leverage (Exchange Locked 1.0x)'
                    : 'Book-2 MIS Leverage Only (Book-1 CNC Always 1.0x)'}
                </label>
                <select
                  value={effectiveMarginMode}
                  disabled={isCncOnlyMode}
                  onChange={(e) => !isCncOnlyMode && onChangeMarginMode && onChangeMarginMode(e.target.value)}
                  className={`w-full px-2 py-1.5 rounded-lg border font-semibold text-xs ${
                    isCncOnlyMode
                      ? 'border-slate-300 bg-slate-100 text-slate-500 cursor-not-allowed'
                      : 'border-slate-300 bg-white text-slate-900'
                  }`}
                >
                  {isCncOnlyMode ? (
                    <option value="CASH_1X">1.0x Cash Locked (0x Lev — Mandatory for CNC)</option>
                  ) : (
                    <>
                      <option value="CASH_1X">1.0x Cash (0x Lev • Default)</option>
                      <option value="MIS_2X">2.0x MIS Intraday Only (0.50% SL Cap)</option>
                      <option value="MIS_3X">3.0x MIS Intraday Only (0.50% SL Cap)</option>
                      <option value="UPSTOX_VAR_ELM_MAX_5X">Upstox VaR+ELM (&lt;=5.0x MIS Only • SL Capped)</option>
                    </>
                  )}
                </select>
              </div>
              <div className="flex items-end">
                <button
                  type="button"
                  disabled={configBusy}
                  onClick={() => onSaveCapitalConfig && onSaveCapitalConfig()}
                  className="w-full py-1.5 px-3 rounded-lg bg-slate-900 hover:bg-slate-800 text-white font-bold text-xs cursor-pointer disabled:opacity-50"
                >
                  {configBusy ? 'Saving...' : 'Save Sub-Wallet & Fence'}
                </button>
              </div>
            </div>
          </div>

          {/* Pre-Flight Readiness, Option Sniffer Firewall & Day-2 Invested Summary */}
          <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 space-y-1.5 font-mono">
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Configured Equity Sub-Wallet:</span>
              <span className="font-bold text-slate-900">{formatINRValue(configuredSleeveCap)}</span>
            </div>
            {liveInvestedEq > 0 && (
              <div className="flex items-center justify-between text-sky-800">
                <span>Already Invested in Live CNC Shares:</span>
                <span className="font-bold">{formatINRValue(liveInvestedEq)}</span>
              </div>
            )}
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Option Sniffer Protected Fence:</span>
              <span className="font-bold text-indigo-700">
                {formatINRValue(snifferFw.reserved_margin_inr || 0)}{' '}
                {snifferFw.option_sniffer_armed ? '(SNIFFER ARMED)' : '(FENCED)'}
              </span>
            </div>
            <div className="flex items-center justify-between border-t border-slate-200 pt-1.5">
              <span className="text-slate-700 font-semibold">Required Free Upstox Cash Today:</span>
              <span className="font-bold text-slate-900">{formatINRValue(reqMargin)}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Upstox Available Wallet Cash:</span>
              <span className={`font-bold ${marginOk ? 'text-emerald-700' : 'text-rose-700'}`}>
                {formatINRValue(availMargin)}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Auto-Scaled Slots (B1 CNC / B2 MIS):</span>
              <span className="font-bold text-indigo-800">
                {liveStatus?.accounting?.book1_slots?.max ?? '—'} CNC (1.0x) /{' '}
                {isCncOnlyMode
                  ? 'Shadow Only'
                  : `${liveStatus?.accounting?.book2_slots?.max ?? '—'} MIS (${liveStatus?.intraday_mis_leverage ?? 1}x)`}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Book-2 60-Session Shadow Gate:</span>
              <span className="font-bold text-slate-700">
                {shadowGate.completed_sessions ?? 0} / {shadowGate.target_sessions ?? 60} sessions
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-600">Intraday Realized Loss Halt (-1.25%):</span>
              <span className="font-bold text-rose-700">{formatINRValue(maxDailyLoss)}</span>
            </div>
          </div>

          {/* DDPI / CDSL TPIN Advisory for CNC Delivery Exits */}
          <label className="flex items-start gap-2.5 p-3 rounded-xl bg-emerald-50/70 border border-emerald-200 text-emerald-950 cursor-pointer">
            <input
              type="checkbox"
              checked={Boolean(ddpiConfirmedInput)}
              onChange={(e) => onChangeDdpiConfirmed && onChangeDdpiConfirmed(e.target.checked)}
              className="mt-0.5 rounded border-emerald-400"
            />
            <span className="text-[11px] leading-relaxed">
              <strong className="inline-flex items-center gap-1">
                <ShieldCheck size={13} className="text-emerald-700" />
                Upstox DDPI / CDSL TPIN Ready:
              </strong>{' '}
              I confirm DDPI is enabled on my Upstox demat (or I will pre-authorize CDSL EDIS TPIN before 09:15 IST) so
              automated protective &amp; 20-day rebalance `CNC SELL` exits execute without demat authorization rejection.
            </span>
          </label>

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
                3. Enter Local Confirmation Challenge PIN:
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
