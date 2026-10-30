import React from 'react';
import { CheckCircle2, AlertTriangle, XCircle, Clock, ArrowRight, ShieldAlert, Cpu, Eye } from 'lucide-react';

export default function PipelineFlow({ diagResult, onSelectStage, selectedStageId }) {
  // If diagnostic has not been run yet, render the conceptual fallback pipeline flow
  if (!diagResult || !diagResult.stages || diagResult.stages.length === 0) {
    const conceptualStages = [
      { id: '1_data_feed', name: 'Market Data Ingestion', purpose: 'Synthesizes/validates OHLCV bars' },
      { id: '2_validation_gate', name: 'Data Validation Gate', purpose: 'Zero NaN, volume and spread bounds' },
      { id: '3_feature_engine', name: 'Production Feature Engine', purpose: 'Calculates RSI, MACD, ATR, Bollinger' },
      { id: '4_champion_inference', name: 'Champion Ensemble Inference', purpose: 'RF, GradientBoosting, SVM ensemble' },
      { id: '5_sentiment_engine', name: 'Financial Sentiment Engine', purpose: 'VADER news polarity scoring' },
      { id: '6_metalearner_arbitration', name: 'Layer-2 Meta-Learner', purpose: 'Stacked arbitration with macro regime' },
      { id: '7_probability_calibration', name: 'Probability Calibration', purpose: 'Isotonic calibration for reliability' },
      { id: '8_decision_engine', name: 'Decision Engine Gate', purpose: 'Minimum conviction & cash-equity gates' },
      { id: '9_risk_heat_gate', name: 'Kelly Sizer & Risk Gate', purpose: 'Portfolio heat budget (<=6% max ceiling)' },
      { id: '10_persistence_quarantine', name: 'Database Persistence Check', purpose: 'Quarantine diagnostic from ml_trade_history' },
      { id: '11_telegram_suppression', name: 'Notification Gate Check', purpose: 'Ensures diagnostic alerts remain suppressed' }
    ];

    return (
      <div className="bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
        <div className="flex items-center justify-between border-b border-slate-800 pb-3">
          <div>
            <h3 className="text-sm font-bold text-white flex items-center gap-2">
              <Cpu size={16} className="text-indigo-400" />
              Production Pipeline Architecture (Standby)
            </h3>
            <p className="text-xs text-slate-400 mt-0.5">
              Click &quot;Run Full Pipeline Verification&quot; to execute real-time synthetic telemetry across all production stages.
            </p>
          </div>
          <span className="text-[10px] font-mono px-2.5 py-1 rounded-md bg-slate-800 text-slate-400 border border-slate-700">
            AWAITING RUN
          </span>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {conceptualStages.map((st, idx) => (
            <div
              key={st.id}
              className="p-3.5 rounded-xl border border-slate-800/80 bg-slate-950/40 text-xs flex flex-col justify-between space-y-2 opacity-75 hover:opacity-100 transition"
            >
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-mono font-bold text-slate-500">STAGE {idx + 1}</span>
                <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">READY</span>
              </div>
              <div>
                <h4 className="font-bold text-slate-200 text-xs">{st.name}</h4>
                <p className="text-[11px] text-slate-400 mt-1 leading-snug">{st.purpose}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  // Dynamic stages from backend telemetry (Source of truth)
  const stages = diagResult.stages;

  const getStageStyles = (status) => {
    switch (status) {
      case 'PASS':
        return {
          card: 'bg-emerald-950/20 border-emerald-800/60 hover:border-emerald-500/80 text-emerald-200',
          badge: 'bg-emerald-900/60 text-emerald-300 border-emerald-700/50',
          icon: <CheckCircle2 size={14} className="text-emerald-400 shrink-0" />
        };
      case 'WARNING':
        return {
          card: 'bg-amber-950/20 border-amber-800/60 hover:border-amber-500/80 text-amber-200',
          badge: 'bg-amber-900/60 text-amber-300 border-amber-700/50',
          icon: <AlertTriangle size={14} className="text-amber-400 shrink-0" />
        };
      case 'REJECTED':
        return {
          card: 'bg-rose-950/20 border-rose-800/60 hover:border-rose-500/80 text-rose-200',
          badge: 'bg-rose-900/60 text-rose-300 border-rose-700/50',
          icon: <XCircle size={14} className="text-rose-400 shrink-0" />
        };
      case 'FAIL':
        return {
          card: 'bg-red-950/30 border-red-800 hover:border-red-600 text-red-200',
          badge: 'bg-red-900/80 text-red-200 border-red-700',
          icon: <ShieldAlert size={14} className="text-red-400 shrink-0" />
        };
      case 'NOT_RUN':
        return {
          card: 'bg-slate-950/30 border-slate-800 hover:border-slate-700 text-slate-400 opacity-65',
          badge: 'bg-slate-800 text-slate-400 border-slate-700',
          icon: <Clock size={14} className="text-slate-500 shrink-0" />
        };
      default:
        return {
          card: 'bg-slate-900/50 border-slate-800 hover:border-slate-700 text-slate-300',
          badge: 'bg-slate-800 text-slate-300 border-slate-700',
          icon: <Clock size={14} className="text-slate-400 shrink-0" />
        };
    }
  };

  return (
    <div className="bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <div>
          <h3 className="text-sm font-bold text-white flex items-center gap-2">
            <Cpu size={16} className="text-emerald-400" />
            Live Diagnostic Pipeline Execution Flow
            <span className="text-xs font-mono font-normal text-slate-400">
              ({stages.length} Dynamic Stages &bull; {diagResult.duration_ms || 0}ms)
            </span>
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            Rendered dynamically from backend stage telemetry. Click any stage to inspect inputs, outputs, and forensic rationale.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-[11px] text-slate-400">Target:</span>
          <span className="font-mono text-xs text-cyan-300 bg-cyan-950/50 border border-cyan-800/60 px-2 py-0.5 rounded font-bold">
            {diagResult.symbol || 'TESTSTOCK.NS'}
          </span>
          <span className="font-mono text-xs text-indigo-300 bg-indigo-950/50 border border-indigo-800/60 px-2 py-0.5 rounded uppercase font-bold">
            {diagResult.timeframe || 'intraday'}
          </span>
        </div>
      </div>

      {/* Dynamic Stage Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
        {stages.map((st, idx) => {
          const style = getStageStyles(st.status);
          const isSelected = selectedStageId === (st.stage_id || String(idx));

          return (
            <div
              key={st.stage_id || idx}
              onClick={() => onSelectStage && onSelectStage(st)}
              className={`p-3.5 rounded-xl border transition-all cursor-pointer flex flex-col justify-between space-y-3 relative group ${style.card} ${
                isSelected ? 'ring-2 ring-indigo-500 shadow-lg' : ''
              }`}
            >
              {/* Header: Stage Number & Status Badge */}
              <div className="flex items-center justify-between gap-1">
                <span className="text-[10px] font-mono font-bold text-slate-400">
                  {idx + 1}. {st.stage_id ? st.stage_id.toUpperCase() : `STAGE_${idx + 1}`}
                </span>
                <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold border flex items-center gap-1 ${style.badge}`}>
                  {style.icon}
                  {st.status}
                </span>
              </div>

              {/* Title & Purpose */}
              <div className="space-y-1">
                <h4 className="font-bold text-slate-100 text-xs group-hover:text-white flex items-center justify-between">
                  <span className="truncate">{st.stage}</span>
                  <Eye size={12} className="opacity-0 group-hover:opacity-100 text-slate-400 transition" />
                </h4>
                <p className="text-[11px] text-slate-300 line-clamp-2 leading-relaxed">
                  {st.purpose || st.detail}
                </p>
              </div>

              {/* Footer: Duration & Inputs/Outputs hints */}
              <div className="pt-2 border-t border-slate-800/60 flex items-center justify-between text-[10px] font-mono text-slate-400">
                <span>{st.duration_ms !== undefined ? `${st.duration_ms} ms` : '—'}</span>
                {st.rejection_reason && (
                  <span className="text-rose-400 font-bold truncate max-w-[150px]" title={st.rejection_reason}>
                    Rejection Invariant
                  </span>
                )}
                {st.warning && !st.rejection_reason && (
                  <span className="text-amber-400 font-bold truncate max-w-[150px]" title={st.warning}>
                    Advisory Warning
                  </span>
                )}
                {!st.rejection_reason && !st.warning && (
                  <span className="text-slate-500 group-hover:text-slate-300 transition">Inspect &rarr;</span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
