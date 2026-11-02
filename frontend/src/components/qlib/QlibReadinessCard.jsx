import React, { useState } from 'react';
import {
  ShieldCheck,
  AlertTriangle,
  AlertCircle,
  CheckCircle2,
  Clock,
  ChevronDown,
  ChevronUp,
  Activity,
  Layers,
  Cpu
} from 'lucide-react';

export default function QlibReadinessCard({ readinessData, onStrategySelect, selectedStrategy = 'SWING' }) {
  const [expandedStrategy, setExpandedStrategy] = useState(null);

  if (!readinessData || !readinessData.strategies) {
    return (
      <div className="bg-slate-900/80 border border-slate-800 rounded-2xl p-4 text-center text-xs text-gray-500 font-mono">
        Loading strategy operational readiness...
      </div>
    );
  }

  const strategies = ['SWING', 'INTRADAY', 'FNO'];

  const getStatusBadge = (stratInfo) => {
    if (stratInfo.trading_ready) {
      return (
        <span className="px-2.5 py-1 rounded-full text-[10px] font-mono font-bold uppercase bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 flex items-center gap-1.5 shadow-sm">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
          TRADING READY
        </span>
      );
    }
    if (stratInfo.scanner_ready) {
      return (
        <span className="px-2.5 py-1 rounded-full text-[10px] font-mono font-bold uppercase bg-amber-500/20 text-amber-300 border border-amber-500/40 flex items-center gap-1.5 shadow-sm">
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400" />
          {stratInfo.champion_qualified ? 'SCANNER READY' : 'SCANNER READY (UNQUALIFIED)'}
        </span>
      );
    }
    if (stratInfo.live_data_ready) {
      return (
        <span className="px-2.5 py-1 rounded-full text-[10px] font-mono font-bold uppercase bg-blue-500/20 text-blue-300 border border-blue-500/40 flex items-center gap-1.5 shadow-sm">
          <span className="w-1.5 h-1.5 rounded-full bg-blue-400" />
          LIVE DATA READY
        </span>
      );
    }
    return (
      <span className="px-2.5 py-1 rounded-full text-[10px] font-mono font-bold uppercase bg-rose-500/20 text-rose-300 border border-rose-500/40 flex items-center gap-1.5 shadow-sm">
        <span className="w-1.5 h-1.5 rounded-full bg-rose-400" />
        NOT READY — NO QUALIFIED CHAMPION
      </span>
    );
  };

  return (
    <div className="bg-slate-900/90 border border-slate-800/90 rounded-2xl p-5 shadow-xl space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-3">
        <div className="flex items-center gap-2.5">
          <div className="p-2 bg-purple-500/10 border border-purple-500/30 rounded-xl text-purple-400">
            <ShieldCheck size={18} />
          </div>
          <div>
            <h3 className="text-sm font-bold text-white tracking-tight">Strategy Operational Readiness Matrix</h3>
            <p className="text-[11px] text-gray-400 font-mono">
              Strictly distinguishes RESEARCH READY, SCANNER READY, LIVE DATA READY, and TRADING READY
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 text-[10px] font-mono text-gray-400">
          <span className="flex items-center gap-1">🟢 READY</span>
          <span className="flex items-center gap-1">🟡 WARNING</span>
          <span className="flex items-center gap-1">🔴 BLOCKED</span>
          <span className="flex items-center gap-1">⚪ DATA UNAVAILABLE</span>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {strategies.map((strat) => {
          const info = readinessData.strategies[strat] || {};
          const isSelected = selectedStrategy === strat;
          const isExpanded = expandedStrategy === strat;
          const metrics = info.metrics || {};
          const hasFailedGates = (info.failed_gates || []).length > 0;

          return (
            <div
              key={strat}
              className={`rounded-xl border transition-all p-4 flex flex-col justify-between ${
                isSelected
                  ? 'bg-slate-800/90 border-purple-500/50 shadow-lg shadow-purple-950/20'
                  : 'bg-slate-950/60 border-slate-800/80 hover:border-slate-700'
              }`}
            >
              <div className="space-y-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-bold text-sm text-white">{strat}</span>
                    {strat === 'SWING' && <span className="text-[10px] font-mono text-gray-400">(5D Hold)</span>}
                    {strat === 'INTRADAY' && <span className="text-[10px] font-mono text-gray-400">(15m Intraday)</span>}
                    {strat === 'FNO' && <span className="text-[10px] font-mono text-gray-400">(NSE Contracts)</span>}
                  </div>
                  {onStrategySelect && (
                    <button
                      onClick={() => onStrategySelect(strat)}
                      className={`text-[10px] font-mono px-2 py-0.5 rounded border transition-all ${
                        isSelected
                          ? 'bg-purple-600 text-white border-purple-400'
                          : 'bg-slate-800 text-gray-400 border-slate-700 hover:text-white'
                      }`}
                    >
                      {isSelected ? 'ACTIVE VIEW' : 'SELECT'}
                    </button>
                  )}
                </div>

                <div>{getStatusBadge(info)}</div>

                {/* 4 Pillars Readiness Checklist */}
                <div className="space-y-1.5 pt-2 border-t border-slate-800/80 font-mono text-[11px]">
                  <div className="flex items-center justify-between">
                    <span className="text-gray-400">Research Pipeline:</span>
                    <span className={info.research_ready ? 'text-emerald-400 font-bold' : 'text-gray-500'}>
                      {info.research_ready ? '🟢 READY' : '⚪ UNAVAILABLE'}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-gray-400">Market Data Freshness:</span>
                    <span
                      className={
                        info.data_freshness === 'FRESH'
                          ? 'text-emerald-400 font-bold'
                          : info.data_freshness === 'DELAYED'
                          ? 'text-amber-400 font-bold'
                          : 'text-rose-400 font-bold'
                      }
                    >
                      {info.data_freshness === 'FRESH' ? '🟢 FRESH' : info.data_freshness === 'DELAYED' ? '🟡 DELAYED' : '🔴 STALE'}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-gray-400">Scanner Engine:</span>
                    <span className={info.scanner_ready ? 'text-emerald-400 font-bold' : 'text-amber-400 font-bold'}>
                      {info.scanner_ready ? '🟢 READY' : '🟡 MODEL UNREADY'}
                    </span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-gray-400">Champion Qualified:</span>
                    <span className={info.champion_qualified ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                      {info.champion_qualified ? '🟢 QUALIFIED' : '🔴 UNQUALIFIED'}
                    </span>
                  </div>
                </div>

                {/* Metrics Summary Strip */}
                {info.has_champion && (
                  <div className="bg-slate-900/90 rounded-lg p-2.5 border border-slate-800/60 grid grid-cols-4 gap-1.5 text-center font-mono">
                    <div>
                      <div className="text-[9px] text-gray-500 uppercase">Sharpe</div>
                      <div className={`text-xs font-bold ${metrics.sharpe > 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {metrics.sharpe !== undefined ? metrics.sharpe : 'N/A'}
                      </div>
                    </div>
                    <div>
                      <div className="text-[9px] text-gray-500 uppercase">PF</div>
                      <div className={`text-xs font-bold ${metrics.profit_factor >= 1.0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {metrics.profit_factor !== undefined ? metrics.profit_factor : 'N/A'}
                      </div>
                    </div>
                    <div>
                      <div className="text-[9px] text-gray-500 uppercase">Max DD</div>
                      <div className={`text-xs font-bold ${(metrics.max_drawdown_pct || 0) <= 20.0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {metrics.max_drawdown_pct !== undefined ? `${metrics.max_drawdown_pct}%` : 'N/A'}
                      </div>
                    </div>
                    <div>
                      <div className="text-[9px] text-gray-500 uppercase">Trades</div>
                      <div className="text-xs font-bold text-gray-300">
                        {metrics.trades_count || metrics.trades || 0}
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Collapsible Details */}
              <div className="mt-3 pt-2 border-t border-slate-800/60">
                <button
                  onClick={() => setExpandedStrategy(isExpanded ? null : strat)}
                  className="w-full flex items-center justify-between text-[10px] font-mono text-gray-400 hover:text-gray-200 transition-colors"
                >
                  <span>{info.data_note || 'Details'}</span>
                  {isExpanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
                </button>

                {isExpanded && (
                  <div className="mt-2 text-[10px] font-mono bg-slate-900/90 rounded p-2 border border-slate-800 space-y-1 text-gray-300">
                    <div>
                      <span className="text-gray-500">Active Model: </span>
                      <span className="text-purple-300 font-bold truncate block">{info.champion_id || 'None'}</span>
                    </div>
                    {hasFailedGates && (
                      <div className="pt-1 text-rose-400 space-y-0.5">
                        <div className="font-bold text-rose-300">Failed Gates:</div>
                        {info.failed_gates.slice(0, 3).map((fg, i) => (
                          <div key={i} className="truncate">• {fg}</div>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
