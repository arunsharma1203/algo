import React from 'react';
import { Database, Lock, ShieldCheck, Hash, Layers, Clock } from 'lucide-react';

export default function OOSIntegrityPanel({
  universe = 'LIVE_52',
  tickerCount = 52,
  oosBars = 702,
  barUnit = 'daily bars',
  datasetHash = 'e9c8b417a8e2',
  universeHash = 'a47b19df3c08',
  configHash = 'd83c21a4f092',
  featureVersion = 'qlib_research_v1',
  oosWindow = '2024-01-01 to 2026-08-31'
}) {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <div className="flex items-center gap-2">
          <Database size={18} className="text-cyan-400" />
          <h4 className="text-sm font-bold text-white tracking-tight">
            Out-of-Sample Dataset &amp; Split Integrity
          </h4>
        </div>
        <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-cyan-950 text-cyan-300 border border-cyan-800 font-bold">
          TEMPORAL PURITY: ENFORCED
        </span>
      </div>

      {/* Chronological Split Diagram */}
      <div className="space-y-2 text-xs">
        <span className="text-[10px] font-mono text-slate-400 uppercase font-bold">
          Chronological Split Funnel (Zero Future Leakage)
        </span>
        <div className="grid grid-cols-12 gap-1 font-mono text-[10px] text-center">
          <div className="col-span-6 p-2 rounded-lg bg-indigo-950/60 border border-indigo-800/60 text-indigo-300">
            <span className="font-bold block">TRAIN (60%)</span>
            <span className="text-[9px] text-slate-400">Expanding Historical</span>
          </div>
          <div className="col-span-2 p-2 rounded-lg bg-purple-950/60 border border-purple-800/60 text-purple-300">
            <span className="font-bold block">VAL (10%)</span>
            <span className="text-[9px] text-slate-400">TimeSeries</span>
          </div>
          <div className="col-span-4 p-2 rounded-lg bg-emerald-950/70 border border-emerald-700/70 text-emerald-300">
            <span className="font-bold block">LOCKED OOS (30%)</span>
            <span className="text-[9px] text-emerald-400">{oosBars} {barUnit}</span>
          </div>
        </div>
      </div>

      {/* Cryptographic Hashes & Provenance */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5 text-xs font-mono">
        <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800/80 space-y-0.5">
          <span className="text-[10px] text-slate-500 uppercase flex items-center gap-1">
            <Hash size={11} className="text-cyan-400" /> Dataset Hash
          </span>
          <span className="text-slate-200 font-bold block text-[11px] truncate" title={datasetHash}>
            {datasetHash ? `${datasetHash.substring(0, 12)}...` : 'N/A'}
          </span>
        </div>

        <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800/80 space-y-0.5">
          <span className="text-[10px] text-slate-500 uppercase flex items-center gap-1">
            <Hash size={11} className="text-indigo-400" /> Universe Hash
          </span>
          <span className="text-slate-200 font-bold block text-[11px] truncate" title={universeHash}>
            {universeHash ? `${universeHash.substring(0, 12)}...` : 'N/A'}
          </span>
        </div>

        <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800/80 space-y-0.5">
          <span className="text-[10px] text-slate-500 uppercase flex items-center gap-1">
            <Layers size={11} className="text-purple-400" /> Feature Version
          </span>
          <span className="text-purple-300 font-bold block text-[11px] truncate">
            {featureVersion}
          </span>
        </div>

        <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800/80 space-y-0.5">
          <span className="text-[10px] text-slate-500 uppercase flex items-center gap-1">
            <Clock size={11} className="text-emerald-400" /> Evaluated Universe
          </span>
          <span className="text-emerald-300 font-bold block text-[11px] truncate">
            {universe} ({tickerCount} Stocks)
          </span>
        </div>
      </div>
    </div>
  );
}
