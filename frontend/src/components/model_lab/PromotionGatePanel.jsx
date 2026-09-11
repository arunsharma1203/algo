import React from 'react';
import { ShieldCheck, AlertTriangle, CheckCircle2, XCircle, UserCheck, Lock } from 'lucide-react';

export default function PromotionGatePanel({
  backendRecommendation = 'RETAIN_CHAMPION',
  gatesPassed = false,
  rejectionReasons = [],
  tradeCount = 0,
  f1Gain = 0.0,
  sharpeGain = 0.0,
  maxDrawdown = null,
  isLowSample = true,
  onAuthorizePromotion,
  authorizing = false
}) {
  const isRetained = backendRecommendation === 'RETAIN_CHAMPION' || !gatesPassed;

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-4">
      {/* Panel Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <div className="space-y-0.5">
          <div className="flex items-center gap-2">
            <ShieldCheck size={18} className="text-purple-400" />
            <h4 className="text-sm font-bold text-white tracking-tight">
              Multi-Dimensional Promotion Governance Gates
            </h4>
          </div>
          <p className="text-xs text-slate-400">
            Authoritative backend governance verdict. React presents gate criteria but never overrides backend promotion logic.
          </p>
        </div>

        {/* Backend Verdict Badge */}
        <div className="flex items-center gap-2 font-mono">
          <span className="text-[10px] text-slate-400">BACKEND VERDICT:</span>
          <span
            className={`px-2.5 py-1 rounded-md text-xs font-bold border ${
              isRetained
                ? 'bg-rose-950/70 text-rose-300 border-rose-800/70'
                : 'bg-emerald-950/70 text-emerald-300 border-emerald-800/70'
            }`}
          >
            {backendRecommendation || (gatesPassed ? 'PROMOTE_CHALLENGER' : 'RETAIN_CHAMPION')}
          </span>
        </div>
      </div>

      {/* Gates Checklist */}
      <div className="space-y-2.5 text-xs">
        {/* Gate 1: Statistical Hurdle */}
        <div className="p-3 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-start justify-between gap-3">
          <div className="space-y-0.5">
            <div className="flex items-center gap-1.5 font-bold text-slate-200">
              <span>1. Statistical Hurdle (Alpha Superiority)</span>
            </div>
            <p className="text-[11px] text-slate-400">
              Requires out-of-sample F1 gain &ge; +0.0100 and Sharpe ratio &ge; baseline champion.
            </p>
          </div>
          <div className="text-right font-mono text-[11px] shrink-0">
            <span className={f1Gain >= 0.01 && sharpeGain >= 0 ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
              {f1Gain >= 0.01 && sharpeGain >= 0 ? '✓ MET' : '✕ NOT MET'}
            </span>
            <span className="text-slate-500 block text-[10px]">
              &Delta;F1: {f1Gain >= 0 ? `+${f1Gain.toFixed(4)}` : f1Gain.toFixed(4)}
            </span>
          </div>
        </div>

        {/* Gate 2: Sample Size Gate */}
        <div className="p-3 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-start justify-between gap-3">
          <div className="space-y-0.5">
            <div className="flex items-center gap-1.5 font-bold text-slate-200">
              <span>2. Sample Size Gate (Statistical Power)</span>
            </div>
            <p className="text-[11px] text-slate-400">
              Requires at least 30 completed out-of-sample trades. Single-trade spikes are rejected.
            </p>
          </div>
          <div className="text-right font-mono text-[11px] shrink-0">
            <span className={tradeCount >= 30 ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
              {tradeCount >= 30 ? '✓ MET' : '✕ BLOCKED'}
            </span>
            <span className="text-slate-500 block text-[10px]">
              {tradeCount}/30 Trades {isLowSample ? '(LOW SAMPLE)' : ''}
            </span>
          </div>
        </div>

        {/* Gate 3: Risk Boundary */}
        <div className="p-3 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-start justify-between gap-3">
          <div className="space-y-0.5">
            <div className="flex items-center gap-1.5 font-bold text-slate-200">
              <span>3. Tail-Risk Boundary (Drawdown Ceiling)</span>
            </div>
            <p className="text-[11px] text-slate-400">
              Maximum peak-to-trough drawdown must not exceed 20.0% under 0.1% friction drag.
            </p>
          </div>
          <div className="text-right font-mono text-[11px] shrink-0">
            <span className={maxDrawdown !== null && maxDrawdown <= 20.0 ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
              {maxDrawdown !== null && maxDrawdown <= 20.0 ? '✓ MET' : '✕ BLOCKED'}
            </span>
            <span className="text-slate-500 block text-[10px]">
              Max DD: {maxDrawdown !== null ? `${maxDrawdown.toFixed(1)}%` : 'N/A'}
            </span>
          </div>
        </div>

        {/* Gate 4: Human Sign-off */}
        <div className="p-3 rounded-xl bg-slate-950/60 border border-slate-800/80 flex items-start justify-between gap-3">
          <div className="space-y-0.5">
            <div className="flex items-center gap-1.5 font-bold text-slate-200">
              <UserCheck size={13} className="text-indigo-400" />
              <span>4. Two-Man Rule (Human Confirmation)</span>
            </div>
            <p className="text-[11px] text-slate-400">
              Autonomous self-promotion is architecturally forbidden. Requires explicit user authorization.
            </p>
          </div>
          <div className="text-right font-mono text-[11px] shrink-0">
            <span className="text-indigo-400 font-bold flex items-center gap-1">
              <Lock size={12} /> REQUIRED
            </span>
            <span className="text-slate-500 block text-[10px]">Fail-Closed</span>
          </div>
        </div>
      </div>

      {/* Rejection Reasons if blocked */}
      {rejectionReasons && rejectionReasons.length > 0 && (
        <div className="p-3 rounded-xl bg-rose-950/30 border border-rose-800/50 text-xs text-rose-300 space-y-1">
          <span className="font-bold flex items-center gap-1 text-[11px] text-rose-200">
            <XCircle size={14} className="text-rose-400" /> Promotion Gate Rejection Invariants:
          </span>
          <ul className="list-disc list-inside space-y-0.5 font-mono text-[11px]">
            {rejectionReasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
