import React from 'react';
import { Network, ArrowRight, ShieldCheck, Cpu, Database, Sparkles, Layers, Lock, AlertCircle } from 'lucide-react';

export default function PipelineArchitecture() {
  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-6">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3 border-b border-slate-800 pb-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="p-2 rounded-xl bg-purple-600/20 text-purple-400 border border-purple-500/30">
              <Network size={20} />
            </span>
            <h3 className="text-lg font-black text-white tracking-tight">
              Architectural Separation: Production vs. Research vs. Foundation Challengers
            </h3>
          </div>
          <p className="text-xs text-slate-400 mt-1">
            Enforces strict structural boundaries so that exploratory research and experimental foundation models cannot silently mutate production Champion execution.
          </p>
        </div>

        <div className="flex items-center gap-2 text-xs font-mono">
          <span className="px-2.5 py-1 rounded-md bg-emerald-950/60 border border-emerald-700/50 text-emerald-300 font-bold">
            PROD: ACTIVE
          </span>
          <span className="px-2.5 py-1 rounded-md bg-purple-950/60 border border-purple-700/50 text-purple-300 font-bold">
            RESEARCH: SANDBOXED
          </span>
        </div>
      </div>

      {/* Path 1: PRODUCTION CHAMPION PATH */}
      <div className="p-4 rounded-xl bg-slate-950/70 border border-emerald-900/50 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse"></span>
            <span className="text-xs font-mono font-black text-emerald-300 uppercase tracking-wider">
              1. Production Champion Inference Path (Authoritative)
            </span>
          </div>
          <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-800">
            INTRADAY &amp; SWING CHAMPIONS
          </span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2 text-xs font-mono">
          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STAGE A</span>
            <span className="font-bold text-slate-200 block text-[11px]">Market Data Feed</span>
            <p className="text-[10px] text-slate-400 font-sans">NSE / Yahoo live stream with point-in-time enforcement</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STAGE B</span>
            <span className="font-bold text-slate-200 block text-[11px]">Production Features</span>
            <p className="text-[10px] text-slate-400 font-sans">RSI, MACD, Bollinger, ATR, Vol SMA (Zero Alpha158)</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-emerald-800/60 space-y-1">
            <span className="text-[9px] text-emerald-400 block">STAGE C</span>
            <span className="font-bold text-emerald-300 block text-[11px]">Champion Ensemble</span>
            <p className="text-[10px] text-slate-400 font-sans">RF + GB + SVM Voting Classifier + VADER Polarity</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STAGE D</span>
            <span className="font-bold text-slate-200 block text-[11px]">Layer-2 Meta-Learner</span>
            <p className="text-[10px] text-slate-400 font-sans">Stacking arbitration &amp; regime gating</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STAGE E</span>
            <span className="font-bold text-slate-200 block text-[11px]">Decision &amp; Risk</span>
            <p className="text-[10px] text-slate-400 font-sans">60% hurdle &bull; Cash short lock &bull; 6% Heat cap</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STAGE F</span>
            <span className="font-bold text-slate-200 block text-[11px]">Output Telemetry</span>
            <p className="text-[10px] text-slate-400 font-sans">Telegram Bot push &bull; Frontend Scanners &bull; DB Audit</p>
          </div>
        </div>
      </div>

      {/* Path 2: RESEARCH / QLIB ALPHA158 PATH */}
      <div className="p-4 rounded-xl bg-slate-950/70 border border-cyan-900/50 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full bg-cyan-400"></span>
            <span className="text-xs font-mono font-black text-cyan-300 uppercase tracking-wider">
              2. QLib Alpha158 Research Ecosystem (Sandboxed)
            </span>
          </div>
          <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-cyan-950 text-cyan-300 border border-cyan-800 font-bold">
            RESEARCH ONLY &bull; ZERO PROD CONTAMINATION
          </span>
        </div>

        <div className="bg-cyan-950/20 border border-cyan-800/40 rounded-lg p-2.5 text-xs text-cyan-300 flex items-center gap-2 font-mono">
          <AlertCircle size={14} className="shrink-0 text-cyan-400" />
          <span>
            <strong>Architectural Invariant:</strong> Alpha158 features, LightGBM, and DoubleEnsemble belong strictly to the Model Lab Research Sandbox. They are never called during production live scanning or TestStock inference.
          </span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2 text-xs font-mono">
          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STEP 1</span>
            <span className="font-bold text-slate-200 block text-[11px]">Alpha158 / Alpha360</span>
            <p className="text-[10px] text-slate-400 font-sans">Multi-frequency alpha factor extraction</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STEP 2</span>
            <span className="font-bold text-slate-200 block text-[11px]">Model Zoo Candidates</span>
            <p className="text-[10px] text-slate-400 font-sans">LGBM, CatBoost, XGBoost, DoubleEnsemble</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STEP 3</span>
            <span className="font-bold text-slate-200 block text-[11px]">Walk-Forward Validation</span>
            <p className="text-[10px] text-slate-400 font-sans">4-Fold Purged &amp; Embargoed TimeSeriesSplit</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">STEP 4</span>
            <span className="font-bold text-slate-200 block text-[11px]">Locked OOS Evaluation</span>
            <p className="text-[10px] text-slate-400 font-sans">30% Chronologically locked holdout evaluation</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-cyan-800/60 space-y-1">
            <span className="text-[9px] text-cyan-400 block">STEP 5</span>
            <span className="font-bold text-cyan-300 block text-[11px]">Promotion Governance</span>
            <p className="text-[10px] text-slate-400 font-sans">F1 &ge; +0.0100 &bull; N &ge; 30 trades &bull; Human Sign-off</p>
          </div>
        </div>
      </div>

      {/* Path 3: FOUNDATION CHALLENGERS PATH */}
      <div className="p-4 rounded-xl bg-slate-950/70 border border-purple-900/50 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full bg-purple-400"></span>
            <span className="text-xs font-mono font-black text-purple-300 uppercase tracking-wider">
              3. Foundation Model Challenger Path (TimesFM 2.5 &amp; Chronos-2)
            </span>
          </div>
          <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-800 font-bold">
            ADVISORY / CHALLENGER BENCHMARK
          </span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-2 text-xs font-mono">
          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">FOUNDATION A</span>
            <span className="font-bold text-slate-200 block text-[11px]">Google TimesFM 2.5</span>
            <p className="text-[10px] text-slate-400 font-sans">200M Transformer Decoder &bull; Continuous forecast</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">FOUNDATION B</span>
            <span className="font-bold text-slate-200 block text-[11px]">Amazon Chronos-2</span>
            <p className="text-[10px] text-slate-400 font-sans">Probabilistic quantiles (10%, 50%, 90%) &bull; Tail risk</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-slate-800 space-y-1">
            <span className="text-[9px] text-slate-500 block">FOUNDATION C</span>
            <span className="font-bold text-slate-200 block text-[11px]">Ablation Benchmark</span>
            <p className="text-[10px] text-slate-400 font-sans">Tests incremental value vs Champion Baseline</p>
          </div>

          <div className="p-2.5 rounded-lg bg-slate-900 border border-purple-800/60 space-y-1">
            <span className="text-[9px] text-purple-400 block">FOUNDATION D</span>
            <span className="font-bold text-purple-300 block text-[11px]">Promotion Governance</span>
            <p className="text-[10px] text-slate-400 font-sans">Strictly requires Sharpe &amp; F1 gain over baseline</p>
          </div>
        </div>
      </div>
    </div>
  );
}
