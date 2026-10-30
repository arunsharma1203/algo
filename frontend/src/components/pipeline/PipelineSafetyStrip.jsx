import React from 'react';
import { ShieldCheck, Lock, Cpu, Database } from 'lucide-react';

export default function PipelineSafetyStrip({ currentHeat = 0.0, maxHeat = 6.0, brokerMode = 'SIMULATION (FAIL-CLOSED)' }) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-3 shadow-md">
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
        {/* Left: Key Invariant Badges */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Champion Integrity */}
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-emerald-950/60 border border-emerald-700/50 text-emerald-300 font-mono text-[11px]">
            <ShieldCheck size={13} className="text-emerald-400 shrink-0" />
            <span className="font-semibold">CHAMPION INTEGRITY:</span>
            <span className="text-emerald-200">SHA-256 VERIFIED</span>
          </div>

          {/* Broker Fail-Closed */}
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-slate-800/80 border border-slate-700 text-slate-300 font-mono text-[11px]">
            <Lock size={12} className="text-amber-400 shrink-0" />
            <span className="text-slate-400">BROKER:</span>
            <span className="font-semibold text-amber-300">{brokerMode}</span>
          </div>

          {/* Research Boundary */}
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-indigo-950/50 border border-indigo-700/40 text-indigo-300 font-mono text-[11px]">
            <Cpu size={12} className="text-indigo-400 shrink-0" />
            <span className="text-indigo-400">RESEARCH BOUNDARY:</span>
            <span className="font-semibold text-indigo-200">QLIB &amp; TFM ISOLATED</span>
          </div>

          {/* Diagnostic Invariant */}
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-cyan-950/40 border border-cyan-800/40 text-cyan-300 font-mono text-[11px]">
            <Database size={12} className="text-cyan-400 shrink-0" />
            <span className="text-cyan-400">TESTSTOCK:</span>
            <span className="font-semibold text-cyan-200">DIAGNOSTIC ONLY (0% HEAT)</span>
          </div>
        </div>

        {/* Right: Portfolio Heat */}
        <div className="flex items-center gap-3 font-mono text-[11px] shrink-0">
          <div className="flex items-center gap-1.5 text-slate-300">
            <span className="text-slate-500 uppercase">Portfolio Heat:</span>
            <span className={`font-bold ${currentHeat > 0 ? 'text-amber-400' : 'text-emerald-400'}`}>
              {currentHeat.toFixed(2)}% / {maxHeat.toFixed(2)}%
            </span>
          </div>
          <div className="w-16 bg-slate-800 rounded-full h-1.5 overflow-hidden border border-slate-700">
            <div 
              className={`h-full rounded-full transition-all duration-500 ${
                currentHeat >= maxHeat ? 'bg-rose-500' : currentHeat > 3.0 ? 'bg-amber-400' : 'bg-emerald-400'
              }`}
              style={{ width: `${Math.min(100, (currentHeat / maxHeat) * 100)}%` }}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
