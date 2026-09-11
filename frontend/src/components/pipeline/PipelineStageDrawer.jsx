import React from 'react';
import { X, CheckCircle2, AlertTriangle, XCircle, Clock, Cpu, Layers, HelpCircle, ArrowRight } from 'lucide-react';

export default function PipelineStageDrawer({ stage, onClose }) {
  if (!stage) return null;

  const getStatusBadge = (status) => {
    switch (status) {
      case 'PASS':
        return (
          <span className="flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-mono font-bold bg-emerald-950 text-emerald-300 border border-emerald-700/60">
            <CheckCircle2 size={13} className="text-emerald-400" /> PASS
          </span>
        );
      case 'WARNING':
        return (
          <span className="flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-mono font-bold bg-amber-950 text-amber-300 border border-amber-700/60">
            <AlertTriangle size={13} className="text-amber-400" /> WARNING
          </span>
        );
      case 'REJECTED':
      case 'FAIL':
        return (
          <span className="flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-mono font-bold bg-rose-950 text-rose-300 border border-rose-700/60">
            <XCircle size={13} className="text-rose-400" /> {status}
          </span>
        );
      case 'NOT_RUN':
        return (
          <span className="flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-mono font-bold bg-slate-800 text-slate-400 border border-slate-700">
            <Clock size={13} /> NOT_RUN (BYPASSED)
          </span>
        );
      default:
        return (
          <span className="px-2.5 py-1 rounded-full text-xs font-mono font-bold bg-slate-800 text-slate-300 border border-slate-700">
            {status || 'UNKNOWN'}
          </span>
        );
    }
  };

  const renderJsonOrPrimitive = (val) => {
    if (val === null || val === undefined) {
      return <span className="text-slate-500 italic">None</span>;
    }
    if (typeof val === 'object') {
      return (
        <pre className="text-[11px] font-mono bg-slate-950/80 p-2.5 rounded-lg border border-slate-800/80 overflow-x-auto text-slate-300 max-h-48 overflow-y-auto">
          {JSON.stringify(val, null, 2)}
        </pre>
      );
    }
    return <span className="font-mono text-slate-200">{String(val)}</span>;
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/60 backdrop-blur-xs transition-opacity animate-fade-in" onClick={onClose}>
      <div 
        className="w-full max-w-lg bg-slate-900 border-l border-slate-800 h-full flex flex-col shadow-2xl overflow-hidden" 
        onClick={(e) => e.stopPropagation()}
      >
        {/* Drawer Header */}
        <div className="p-5 border-b border-slate-800 bg-slate-950/60 flex items-start justify-between">
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded bg-indigo-950 text-indigo-300 border border-indigo-700/50 font-bold">
                STAGE ID: {stage.stage_id || 'unassigned'}
              </span>
              {getStatusBadge(stage.status)}
            </div>
            <h3 className="text-lg font-bold text-white tracking-tight flex items-center gap-2 mt-1">
              <Layers size={18} className="text-indigo-400" />
              {stage.stage || 'Pipeline Stage'}
            </h3>
            {stage.duration_ms !== undefined && (
              <span className="text-xs font-mono text-slate-400 flex items-center gap-1">
                <Clock size={12} /> Execution Duration: <strong className="text-slate-200">{stage.duration_ms} ms</strong>
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 transition"
            title="Close Stage Details"
          >
            <X size={18} />
          </button>
        </div>

        {/* Drawer Body */}
        <div className="flex-1 overflow-y-auto p-5 space-y-5 text-xs text-slate-300">
          {/* Purpose */}
          <div className="space-y-1.5">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1">
              <HelpCircle size={12} className="text-indigo-400" /> Stage Purpose &amp; Role
            </span>
            <p className="bg-slate-950/50 p-3 rounded-lg border border-slate-800/80 text-slate-200 leading-relaxed">
              {stage.purpose || 'Executes atomic verification of pipeline criteria.'}
            </p>
          </div>

          {/* Subsystem Source */}
          <div className="space-y-1.5">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1">
              <Cpu size={12} className="text-emerald-400" /> Architecture Subsystem
            </span>
            <div className="bg-slate-950/50 px-3 py-2 rounded-lg border border-slate-800/80 font-mono text-slate-300">
              {stage.source || 'backend/app/analytics/synthetic_pipeline_tester.py'}
            </div>
          </div>

          {/* Rejection / Warning Notice */}
          {stage.rejection_reason && (
            <div className="p-3 rounded-lg bg-rose-950/40 border border-rose-800/60 text-rose-200 space-y-1">
              <span className="font-bold flex items-center gap-1 text-[11px] text-rose-300">
                <XCircle size={14} className="text-rose-400" /> Rejection Invariant Reason:
              </span>
              <p className="font-mono text-[11px]">{stage.rejection_reason}</p>
            </div>
          )}

          {stage.warning && (
            <div className="p-3 rounded-lg bg-amber-950/40 border border-amber-800/60 text-amber-200 space-y-1">
              <span className="font-bold flex items-center gap-1 text-[11px] text-amber-300">
                <AlertTriangle size={14} className="text-amber-400" /> Stage Advisory Warning:
              </span>
              <p className="font-mono text-[11px]">{stage.warning}</p>
            </div>
          )}

          {/* Execution Detail */}
          <div className="space-y-1.5">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
              Diagnostic Forensic Detail
            </span>
            <p className="bg-slate-950/50 p-3 rounded-lg border border-slate-800/80 font-mono text-[11px] text-slate-300 leading-relaxed whitespace-pre-wrap">
              {stage.detail || 'Executed without reported errors.'}
            </p>
          </div>

          {/* Key Inputs */}
          <div className="space-y-1.5">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
              Key Inputs Received
            </span>
            {stage.key_inputs ? renderJsonOrPrimitive(stage.key_inputs) : (
              <p className="text-slate-500 italic text-[11px]">No structured input telemetry captured</p>
            )}
          </div>

          {/* Key Outputs */}
          <div className="space-y-1.5">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider">
              Key Outputs Produced
            </span>
            {stage.key_outputs ? renderJsonOrPrimitive(stage.key_outputs) : (
              <p className="text-slate-500 italic text-[11px]">No structured output telemetry captured</p>
            )}
          </div>
        </div>

        {/* Drawer Footer */}
        <div className="p-4 border-t border-slate-800 bg-slate-950/80 flex justify-between items-center text-xs">
          <span className="text-slate-500 font-mono text-[10px]">
            Forensic Stage Inspector &bull; TestStock Safe
          </span>
          <button
            onClick={onClose}
            className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg font-semibold transition"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
