import React from 'react';
import { BarChart2, AlertTriangle, CheckCircle2, XCircle } from 'lucide-react';

export default function MetricComparison({
  champion = {},
  candidate = {},
  candidateLabel = 'Candidate Model (Plus Both)'
}) {
  const cF1 = typeof champion.f1 === 'number' ? champion.f1 : parseFloat(champion.f1 || 0);
  const cdF1 = typeof candidate.f1 === 'number' ? candidate.f1 : parseFloat(candidate.f1 || 0);
  const deltaF1 = cdF1 - cF1;

  const cSharpe = typeof champion.sharpe === 'number' ? champion.sharpe : parseFloat(champion.sharpe || 0);
  const cdSharpe = typeof candidate.sharpe === 'number' ? candidate.sharpe : parseFloat(candidate.sharpe || 0);
  const deltaSharpe = cdSharpe - cSharpe;

  const cTrades = champion.completed_trade_count !== undefined ? champion.completed_trade_count : (champion.trade_count || 0);
  const cdTrades = candidate.completed_trade_count !== undefined ? candidate.completed_trade_count : (candidate.trade_count || 0);

  const cDd = champion.max_drawdown_pct !== undefined ? champion.max_drawdown_pct : 0;
  const cdDd = candidate.max_drawdown_pct !== undefined ? candidate.max_drawdown_pct : 0;

  const isLowSample = candidate.is_low_sample || cdTrades < 30;

  const metrics = [
    {
      name: 'F1 Score',
      champion: cF1.toFixed(4),
      candidate: cdF1.toFixed(4),
      delta: (deltaF1 >= 0 ? `+${deltaF1.toFixed(4)}` : deltaF1.toFixed(4)),
      gate: deltaF1 >= 0.01 ? 'PASS (+0.01 req)' : 'FAIL (< +0.01)',
      passed: deltaF1 >= 0.01
    },
    {
      name: 'Sharpe Ratio',
      champion: cSharpe.toFixed(2),
      candidate: cdSharpe.toFixed(2),
      delta: (deltaSharpe >= 0 ? `+${deltaSharpe.toFixed(2)}` : deltaSharpe.toFixed(2)),
      gate: deltaSharpe >= 0 ? 'PASS (>= Base)' : 'FAIL (< Base)',
      passed: deltaSharpe >= 0,
      badge: isLowSample ? 'LOW SAMPLE' : null
    },
    {
      name: 'Executed Trades',
      champion: cTrades,
      candidate: cdTrades,
      delta: cdTrades - cTrades,
      gate: cdTrades >= 30 ? 'PASS (>= 30)' : 'BLOCKED (< 30)',
      passed: cdTrades >= 30
    },
    {
      name: 'Max Drawdown',
      champion: `${cDd}%`,
      candidate: `${cdDd}%`,
      delta: `${(cdDd - cDd).toFixed(1)}%`,
      gate: cdDd <= 20.0 ? 'PASS (<= 20%)' : 'FAIL (> 20%)',
      passed: cdDd <= 20.0
    },
    {
      name: 'Win Rate',
      champion: `${champion.win_rate || 0}%`,
      candidate: `${candidate.win_rate || 0}%`,
      delta: `${((candidate.win_rate || 0) - (champion.win_rate || 0)).toFixed(1)}%`,
      gate: 'Informational',
      passed: true
    },
    {
      name: 'Brier Score',
      champion: champion.brier !== undefined ? champion.brier : '—',
      candidate: candidate.brier !== undefined ? candidate.brier : '—',
      delta: '—',
      gate: 'Calibration',
      passed: true
    }
  ];

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-800 pb-3">
        <div className="flex items-center gap-2">
          <BarChart2 size={18} className="text-indigo-400" />
          <h4 className="text-sm font-bold text-white tracking-tight">
            Candidate vs. Champion Out-of-Sample Metric Matrix
          </h4>
        </div>
        <span className="text-[10px] font-mono text-slate-400">
          Evaluated with 0.1% transaction friction drag
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="min-w-full text-xs font-mono">
          <thead className="bg-slate-950/80 border-b border-slate-800 text-slate-400">
            <tr>
              <th className="px-3 py-2 text-left font-bold">Metric Dimension</th>
              <th className="px-3 py-2 text-center font-bold">Champion Baseline</th>
              <th className="px-3 py-2 text-center font-bold text-indigo-300">{candidateLabel}</th>
              <th className="px-3 py-2 text-center font-bold">Absolute &Delta;</th>
              <th className="px-3 py-2 text-center font-bold">Gate Condition</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/60 bg-slate-950/40">
            {metrics.map((m, idx) => (
              <tr key={idx} className="hover:bg-slate-900/60 transition">
                <td className="px-3 py-2.5 font-bold text-slate-200">{m.name}</td>
                <td className="px-3 py-2.5 text-center text-slate-400">{m.champion}</td>
                <td className="px-3 py-2.5 text-center font-bold text-slate-100">
                  {m.candidate}
                  {m.badge && (
                    <span className="block text-[8px] text-rose-400 font-sans font-bold uppercase">
                      {m.badge}
                    </span>
                  )}
                </td>
                <td className="px-3 py-2.5 text-center text-slate-300">{m.delta}</td>
                <td className="px-3 py-2.5 text-center">
                  <span
                    className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                      m.gate === 'Informational' || m.gate === 'Calibration'
                        ? 'bg-slate-800 text-slate-400'
                        : m.passed
                        ? 'bg-emerald-950 text-emerald-300 border border-emerald-800'
                        : 'bg-rose-950 text-rose-300 border border-rose-800'
                    }`}
                  >
                    {m.gate}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
