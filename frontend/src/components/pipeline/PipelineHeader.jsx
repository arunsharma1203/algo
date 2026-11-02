import React from 'react';
import { Play, RefreshCw, CheckCircle2, AlertTriangle, XCircle, ShieldCheck, Activity, Clock, Terminal, ChevronRight } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

export default function PipelineHeader({
  selectedTf = 'intraday',
  onSelectTf,
  onRunDiagnostic,
  diagRunning = false,
  diagResult = null
}) {
  const navigate = useNavigate();

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-5">
      {/* Top row: Title and Trigger Controls */}
      <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="p-2 rounded-xl bg-indigo-600/20 text-indigo-400 border border-indigo-500/30">
              <Activity size={22} />
            </span>
            <div>
              <h2 className="text-xl font-black text-white tracking-tight flex items-center gap-2">
                Pipeline Health &amp; TestStock Diagnostic
                <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-cyan-950 text-cyan-300 border border-cyan-800 font-bold">
                  DIAGNOSTIC_VERIFICATION
                </span>
              </h2>
              <p className="text-xs text-slate-400">
                Verifies complete end-to-end stage health on synthetic invariant <code>TESTSTOCK.NS</code>. Strictly isolated from live heat, orders, and Telegram.
              </p>
            </div>
          </div>
        </div>

        {/* Action Controls */}
        <div className="flex flex-wrap items-center gap-2.5">
          {/* Timeframe Scope Selector */}
          <div className="flex items-center bg-slate-950 p-1 rounded-xl border border-slate-800 text-xs font-bold">
            <button
              onClick={() => onSelectTf('intraday')}
              className={`px-3 py-1.5 rounded-lg transition cursor-pointer font-mono ${
                selectedTf === 'intraday'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              Intraday (15m)
            </button>
            <button
              onClick={() => onSelectTf('swing')}
              className={`px-3 py-1.5 rounded-lg transition cursor-pointer font-mono ${
                selectedTf === 'swing'
                  ? 'bg-indigo-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              Swing (1D)
            </button>
          </div>

          {/* Master Logger navigation button */}
          <button
            onClick={() => navigate('/audit')}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold border border-slate-700 transition cursor-pointer"
            title="Inspect events in Master System Logger"
          >
            <Terminal size={14} className="text-slate-400" />
            <span>Master Logger</span>
          </button>

          {/* Run Diagnostic Trigger */}
          <button
            onClick={onRunDiagnostic}
            disabled={diagRunning}
            className="flex items-center gap-2 px-4 py-2 bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 disabled:opacity-50 text-white text-xs font-bold rounded-xl shadow-lg transition cursor-pointer"
          >
            {diagRunning ? (
              <>
                <RefreshCw size={14} className="animate-spin" />
                <span>Sweeping Stages ({selectedTf.toUpperCase()})...</span>
              </>
            ) : (
              <>
                <Play size={14} />
                <span>Run Pipeline Verification</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Telemetry Summary Cards (when diagResult available) */}
      {diagResult && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 pt-3 border-t border-slate-800/80">
          {/* 1. System Health */}
          <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800/80 space-y-1">
            <span className="text-[10px] font-mono font-bold text-slate-500 uppercase block">System Health</span>
            <div className="flex items-center gap-1.5">
              {diagResult.overall_pass ? (
                <CheckCircle2 size={16} className="text-emerald-400 shrink-0" />
              ) : (
                <AlertTriangle size={16} className="text-rose-400 shrink-0" />
              )}
              <span className={`text-xs font-mono font-black ${diagResult.overall_pass ? 'text-emerald-400' : 'text-rose-400'}`}>
                {diagResult.overall_pass ? 'PIPELINE HEALTHY' : 'PIPELINE ISSUE'}
              </span>
            </div>
            <span className="text-[10px] text-slate-500 block">
              {diagResult.passed_stages}/{diagResult.total_stages} Stages Passed
            </span>
          </div>

          {/* 2. Trade Eligibility (Distinguished from Pipeline Health) */}
          <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800/80 space-y-1">
            <span className="text-[10px] font-mono font-bold text-slate-500 uppercase block">Trade Eligibility</span>
            <div className="flex items-center gap-1.5">
              {diagResult.eligibility === 'ELIGIBLE' ? (
                <CheckCircle2 size={16} className="text-emerald-400 shrink-0" />
              ) : (
                <XCircle size={16} className="text-amber-400 shrink-0" />
              )}
              <span className={`text-xs font-mono font-black ${diagResult.eligibility === 'ELIGIBLE' ? 'text-emerald-400' : 'text-amber-400'}`}>
                {diagResult.eligibility === 'ELIGIBLE' ? 'TRADE ELIGIBLE' : 'NOT ELIGIBLE'}
              </span>
            </div>
            <span className="text-[10px] text-slate-400 block truncate" title={diagResult.rejection_reason || 'Gate criteria met'}>
              {diagResult.rejection_reason || 'Criteria Satisfied'}
            </span>
          </div>

          {/* 3. Decision & Conviction */}
          <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800/80 space-y-1">
            <span className="text-[10px] font-mono font-bold text-slate-500 uppercase block">Decision &amp; Conviction</span>
            <div className="text-xs font-mono font-black text-indigo-300">
              {diagResult.decision || 'N/A'}
            </div>
            <span className="text-[10px] text-slate-400 block">
              {diagResult.confidence !== undefined ? `${(diagResult.confidence * 100).toFixed(1)}% Calibrated` : '—'}
            </span>
          </div>

          {/* 4. Portfolio Heat */}
          <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800/80 space-y-1">
            <span className="text-[10px] font-mono font-bold text-slate-500 uppercase block">Portfolio Heat</span>
            <div className="text-xs font-mono font-black text-emerald-400">
              {diagResult.risk_heat_pct !== undefined ? `${diagResult.risk_heat_pct.toFixed(2)}%` : '0.00%'}
            </div>
            <span className="text-[10px] text-slate-500 block">
              TestStock Drag: 0.00%
            </span>
          </div>

          {/* 5. Data Freshness */}
          <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800/80 space-y-1">
            <span className="text-[10px] font-mono font-bold text-slate-500 uppercase block">Data Freshness</span>
            <div className="text-xs font-mono font-black text-slate-200">
              {diagResult.data_freshness || 'OK'}
            </div>
            <span className="text-[10px] text-slate-500 block">
              Market Data Verified
            </span>
          </div>

          {/* 6. Execution Latency */}
          <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800/80 space-y-1">
            <span className="text-[10px] font-mono font-bold text-slate-500 uppercase block">Execution Latency</span>
            <div className="text-xs font-mono font-black text-slate-200">
              {diagResult.execution_time_sec !== undefined ? `${diagResult.execution_time_sec.toFixed(3)}s` : `${diagResult.duration_ms || 0}ms`}
            </div>
            <span className="text-[10px] text-slate-500 block">
              Synthetic Sweep
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
