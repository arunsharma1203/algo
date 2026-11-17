import React, { useState } from 'react';
import { Calendar as CalendarIcon, ShieldCheck, TrendingUp } from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  formatINRValue,
  formatPercent,
  formatSignedINR,
} from '../optionSnifferAdvance/formatters';

export default function ConsensusV3EquityPanel({ equityData, accounting }) {
  const [selectedSessionDate, setSelectedSessionDate] = useState(null);

  const acct = accounting || equityData?.accounting || {};
  const funded = Number(acct.funded_capital_inr || 400000);
  const currentEq = Number(acct.current_equity_inr ?? funded);
  const freeCash = Number(acct.free_unreserved_cash_inr ?? funded);
  const totalNet = Number(acct.realized_pnl_inr || 0) + Number(acct.unrealized_pnl_inr || 0);
  const closedCount = Number(equityData?.closed_trades_count || 0);
  const curve = Array.isArray(equityData?.equity_curve) && equityData.equity_curve.length > 0
    ? equityData.equity_curve
    : [
        { idx: 0, label: 'Funded Baseline', equity_inr: funded, cum_net_pnl_inr: 0 },
        { idx: 1, label: 'Live MTM', equity_inr: currentEq, cum_net_pnl_inr: totalNet },
      ];
  const sessions = Array.isArray(equityData?.daily_sessions) ? equityData.daily_sessions : [];
  const activeSession = sessions.find((s) => s.date === selectedSessionDate) || sessions[0] || null;

  const isPositive = totalNet >= 0;
  const strokeColor = isPositive ? '#059669' : '#e11d48';
  const fillId = isPositive ? 'v3EqGreen' : 'v3EqRed';

  const minEq = Math.min(funded, ...curve.map((p) => Number(p.equity_inr || funded)));
  const maxEq = Math.max(funded, ...curve.map((p) => Number(p.equity_inr || funded)));
  const pad = Math.max(funded * 0.015, (maxEq - minEq) * 0.2, 500);

  return (
    <div className="bg-white rounded-2xl border border-slate-200 shadow-2xs overflow-hidden space-y-5 p-4 sm:p-6">
      {/* Top KPI Summary Strip */}
      <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3 border-b border-slate-100 pb-4">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <TrendingUp size={16} className="text-emerald-600" />
            <h2 className="text-sm sm:text-base font-bold text-slate-900">
              Prospective V3 Funded Equity Curve &amp; Session Ledger
            </h2>
            <span className="px-2 py-0.5 rounded bg-emerald-50 text-emerald-800 border border-emerald-200 text-[10px] font-mono font-bold">
              ERA-ISOLATED • 0 HISTORICAL REPLAY CONTAMINATION
            </span>
          </div>
          <p className="text-xs text-slate-500 mt-0.5">
            Tracks strictly forward Consensus V3 positions (`consensus_v3_positions`). Updating or resetting your wallet
            capital immediately updates this baseline.
          </p>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2.5 text-xs font-mono">
          <div className="px-3 py-2 rounded-xl bg-slate-50 border border-slate-200">
            <span className="block text-[10px] text-slate-500 uppercase">Configured Capital</span>
            <span className="font-bold text-slate-900">{formatINRValue(funded)}</span>
          </div>
          <div className="px-3 py-2 rounded-xl bg-indigo-50/60 border border-indigo-200">
            <span className="block text-[10px] text-indigo-800 uppercase">Unit NAV (TWR)</span>
            <span className="font-bold text-indigo-900">
              ₹{Number(acct.unit_nav ?? 100).toFixed(2)}{' '}
              <span className="text-[10px]">
                ({formatPercent(acct.time_weighted_return_pct ?? 0, { signed: true })})
              </span>
            </span>
          </div>
          <div className="px-3 py-2 rounded-xl bg-emerald-50/60 border border-emerald-200">
            <span className="block text-[10px] text-emerald-800 uppercase">Available Free Cash</span>
            <span className="font-bold text-emerald-700">{formatINRValue(freeCash)}</span>
          </div>
          <div className="px-3 py-2 rounded-xl bg-slate-50 border border-slate-200">
            <span className="block text-[10px] text-slate-500 uppercase">Forward Net P&amp;L</span>
            <span className={`font-bold ${isPositive ? 'text-emerald-700' : 'text-rose-700'}`}>
              {formatSignedINR(totalNet)}
            </span>
          </div>
          <div className="px-3 py-2 rounded-xl bg-slate-50 border border-slate-200">
            <span className="block text-[10px] text-slate-500 uppercase">Max Drawdown</span>
            <span className="font-bold text-slate-900">{formatPercent(acct.max_drawdown_pct || 0)}</span>
          </div>
        </div>
      </div>

      {closedCount === 0 && (
        <div className="p-3 rounded-xl bg-emerald-50/70 border border-emerald-200 flex items-center gap-2 text-xs text-emerald-950">
          <ShieldCheck size={15} className="text-emerald-600 shrink-0" />
          <span>
            <strong>Clean Prospective Baseline Ready:</strong> 0 closed V3 trades so far — 100% of your{' '}
            <strong className="font-mono">{formatINRValue(freeCash)}</strong> cash is ready for automated market-hours
            execution (`09:15–15:30 IST`).
          </span>
        </div>
      )}

      {/* Equity Curve Chart */}
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={curve} margin={{ top: 10, right: 16, left: 8, bottom: 4 }}>
            <defs>
              <linearGradient id={fillId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={strokeColor} stopOpacity={0.22} />
                <stop offset="95%" stopColor={strokeColor} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis dataKey="label" tick={{ fontSize: 11, fill: '#64748b' }} />
            <YAxis
              domain={[Math.floor(minEq - pad), Math.ceil(maxEq + pad)]}
              tickFormatter={(v) => `₹${Math.round(v).toLocaleString('en-IN')}`}
              tick={{ fontSize: 11, fill: '#64748b' }}
              width={86}
            />
            <Tooltip
              formatter={(value, name) => [
                formatINRValue(value),
                name === 'equity_inr' ? 'Total Equity' : 'Cum Net P&L',
              ]}
            />
            <ReferenceLine
              y={funded}
              stroke="#475569"
              strokeDasharray="4 4"
              label={{
                value: `Baseline ${formatINRValue(funded, { decimals: 0 })}`,
                position: 'insideTopRight',
                fill: '#475569',
                fontSize: 11,
              }}
            />
            <Area
              type="monotone"
              dataKey="equity_inr"
              stroke={strokeColor}
              strokeWidth={2.5}
              fill={`url(#${fillId})`}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Forward Daily Session Calendar */}
      <div className="pt-3 border-t border-slate-100 space-y-3">
        <div className="flex items-center gap-2">
          <CalendarIcon size={15} className="text-slate-600" />
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-700">
            Prospective V3 Daily Session P&amp;L Calendar ({sessions.length} Active Session{sessions.length === 1 ? '' : 's'})
          </h3>
        </div>

        {sessions.length === 0 ? (
          <p className="text-xs text-slate-500">
            Daily P&amp;L tiles will populate automatically here as V3 positions close during live market sessions.
          </p>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-2.5 font-mono text-xs">
              {sessions.map((s) => {
                const pos = Number(s.net_pnl_inr || 0) >= 0;
                const selected = activeSession?.date === s.date;
                return (
                  <button
                    key={s.date}
                    type="button"
                    onClick={() => setSelectedSessionDate(s.date)}
                    className={`text-left p-2.5 rounded-xl border transition-all cursor-pointer ${
                      pos
                        ? 'bg-emerald-50/70 border-emerald-200 hover:border-emerald-400'
                        : 'bg-rose-50/70 border-rose-200 hover:border-rose-400'
                    } ${selected ? 'ring-2 ring-slate-900' : ''}`}
                  >
                    <div className="text-[10px] text-slate-500 font-bold">{s.date}</div>
                    <div className={`text-sm font-bold mt-0.5 ${pos ? 'text-emerald-700' : 'text-rose-700'}`}>
                      {formatSignedINR(s.net_pnl_inr)}
                    </div>
                    <div className="text-[10px] text-slate-600 mt-0.5">
                      {s.trades} tr • {s.wins}W/{s.losses}L • Fees {formatINRValue(s.fees_inr, { decimals: 0 })}
                    </div>
                  </button>
                );
              })}
            </div>

            {activeSession && (
              <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-mono">
                  <span className="font-bold text-slate-900">Session Breakdown: {activeSession.date}</span>
                  <span className="text-slate-600">
                    B1 Swing: {formatSignedINR(activeSession.book1_net_inr)} • B2 Intraday:{' '}
                    {formatSignedINR(activeSession.book2_net_inr)} • Statutory Fees:{' '}
                    {formatINRValue(activeSession.fees_inr)}
                  </span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs text-left border-collapse font-mono">
                    <thead>
                      <tr className="border-b border-slate-200 text-[10px] uppercase text-slate-500 font-sans font-bold">
                        <th className="py-1.5 px-2">Symbol</th>
                        <th className="py-1.5 px-2">Product</th>
                        <th className="py-1.5 px-2">Shares</th>
                        <th className="py-1.5 px-2">Entry → Exit</th>
                        <th className="py-1.5 px-2">Fees</th>
                        <th className="py-1.5 px-2">Net P&amp;L</th>
                        <th className="py-1.5 px-2">Reason</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-200/60">
                      {activeSession.items.map((it) => (
                        <tr key={it.position_id}>
                          <td className="py-1.5 px-2 font-bold text-slate-900">{it.symbol}</td>
                          <td className="py-1.5 px-2">{it.product_type}</td>
                          <td className="py-1.5 px-2">{it.shares}</td>
                          <td className="py-1.5 px-2">
                            {formatINRValue(it.entry_price)} → {formatINRValue(it.exit_price)}
                          </td>
                          <td className="py-1.5 px-2 text-amber-700">{formatINRValue(it.fees_inr)}</td>
                          <td
                            className={`py-1.5 px-2 font-bold ${
                              (it.net_pnl_inr || 0) >= 0 ? 'text-emerald-700' : 'text-rose-700'
                            }`}
                          >
                            {formatSignedINR(it.net_pnl_inr)}
                          </td>
                          <td className="py-1.5 px-2 font-sans text-[11px]">{it.exit_reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
