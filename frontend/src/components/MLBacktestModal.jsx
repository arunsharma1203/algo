import React, { useState, useEffect } from 'react';
import { X, Play, Loader, TrendingUp, AlertTriangle, Activity, Dices, ShieldAlert, BarChart3, ArrowUpRight, ArrowDownRight } from 'lucide-react';
import { AreaChart, Area, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts';
import { API_BASE } from '../services/api';

export default function MLBacktestModal({ isOpen, onClose, ticker, defaultType = 'SWING' }) {
  const [modelType, setModelType] = useState(defaultType);
  const [activeTab, setActiveTab] = useState('EQUITY'); // 'EQUITY' or 'MONTE_CARLO'
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (isOpen) {
      setResults(null);
      setError(null);
      setActiveTab('EQUITY');
    }
  }, [isOpen]);

  const runSimulation = async () => {
    if (!ticker) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/ml/backtest-simulate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker, model_type: modelType })
      });
      const data = await response.json();
      
      if (!response.ok) {
        throw new Error(data.detail || 'Simulation failed');
      }
      setResults(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-2.5 sm:p-6 bg-black/70 backdrop-blur-sm">
      <div className="bg-slate-900 text-slate-100 rounded-2xl shadow-2xl w-full max-w-6xl max-h-[92vh] overflow-hidden flex flex-col border border-slate-700">
        
        {/* Header */}
        <div className="flex justify-between items-center px-3.5 sm:px-6 py-3 sm:py-4 border-b border-slate-800 bg-slate-950/80">
          <div className="flex items-center gap-3">
            <div className="p-1.5 sm:p-2 bg-purple-950/70 border border-purple-500/40 rounded-xl text-purple-400 shrink-0">
              <Activity className="w-5 h-5 sm:w-6 sm:h-6" />
            </div>
            <div>
              <h2 className="text-sm sm:text-xl font-black tracking-tight text-white flex items-center gap-2">
                Quantitative ML Backtesting &amp; Monte Carlo
              </h2>
              <p className="text-slate-400 text-[10px] sm:text-xs mt-0.5">
                Stress-test ensemble models with multi-year data &amp; 1,000 bootstrap simulations
              </p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 sm:p-2 text-slate-400 hover:text-white hover:bg-slate-800 rounded-xl transition-colors shrink-0">
            <X className="w-5 h-5 sm:w-6 sm:h-6" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-3.5 sm:p-6 bg-slate-900 space-y-4 sm:space-y-6">
          
          {/* Controls Bar */}
          <div className="bg-slate-950/60 p-3 sm:p-4 rounded-xl border border-slate-800 shadow-sm flex flex-wrap items-end gap-3 sm:gap-4">
            <div className="flex-1 min-w-[140px] sm:min-w-[180px]">
              <label className="block text-xs font-bold text-slate-400 uppercase tracking-wider mb-1.5">Selected Asset</label>
              <input 
                type="text" 
                value={ticker} 
                disabled 
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 font-mono text-purple-300 font-black text-sm"
              />
            </div>
            
            <div className="flex-1 min-w-[180px] sm:min-w-[220px]">
              <label className="block text-xs font-bold text-slate-400 uppercase tracking-wider mb-1.5">Strategy / Horizon</label>
              <select 
                value={modelType} 
                onChange={(e) => setModelType(e.target.value)}
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 font-semibold text-slate-200 text-sm focus:ring-2 focus:ring-purple-500 focus:outline-none"
              >
                <option value="SWING">Swing ML (1D Candles, 5-Year Sweep)</option>
                <option value="INTRADAY">Intraday ML (15m Candles, 60-Day Sweep)</option>
              </select>
            </div>

            <button 
              onClick={runSimulation}
              disabled={loading}
              className={`w-full sm:w-auto px-6 py-2 rounded-lg font-bold text-sm flex items-center justify-center gap-2 text-white shadow-lg transition-all ${
                loading ? 'bg-purple-600/50 cursor-not-allowed' : 'bg-purple-600 hover:bg-purple-500 hover:shadow-purple-500/20 active:scale-95'
              }`}
            >
              {loading ? <Loader className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4 fill-current" />}
              {loading ? 'Simulating 1,000 Paths...' : 'Execute Quant Simulation'}
            </button>
          </div>

          {error && (
            <div className="bg-rose-950/50 border border-rose-800 text-rose-300 p-3 sm:p-4 rounded-xl flex items-start gap-3 text-xs sm:text-sm">
              <AlertTriangle className="w-5 h-5 flex-shrink-0 mt-0.5 text-rose-400" />
              <p className="font-medium">{error}</p>
            </div>
          )}

          {/* Results View */}
          {results && (
            <div className="space-y-6">
              
              {/* Tab Navigation */}
              <div className="flex overflow-x-auto whitespace-nowrap gap-2 border-b border-slate-800 pb-2 max-w-full">
                <button
                  onClick={() => setActiveTab('EQUITY')}
                  className={`flex items-center gap-2 px-3 sm:px-4 py-2 rounded-lg font-bold text-xs sm:text-sm transition-all shrink-0 ${
                    activeTab === 'EQUITY' 
                      ? 'bg-purple-950/80 text-purple-300 border border-purple-500/40' 
                      : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
                  }`}
                >
                  <BarChart3 className="w-4 h-4" />
                  Equity Curve &amp; KPIs
                </button>
                <button
                  onClick={() => setActiveTab('MONTE_CARLO')}
                  className={`flex items-center gap-2 px-3 sm:px-4 py-2 rounded-lg font-bold text-xs sm:text-sm transition-all shrink-0 ${
                    activeTab === 'MONTE_CARLO' 
                      ? 'bg-purple-950/80 text-purple-300 border border-purple-500/40' 
                      : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
                  }`}
                >
                  <Dices className="w-4 h-4 text-emerald-400" />
                  Monte Carlo (1,000 Paths)
                </button>
              </div>

              {/* Comprehensive KPI Metric Grid */}
              <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2 sm:gap-3">
                <MetricBox title="Win Rate" value={`${results.metrics.win_rate}%`} color={results.metrics.win_rate >= 50 ? 'text-emerald-400' : 'text-amber-400'} />
                <MetricBox title="Net PnL" value={`₹${results.metrics.total_pnl.toLocaleString('en-IN')}`} color={results.metrics.total_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'} />
                <MetricBox title="Profit Factor" value={results.metrics.profit_factor} color={results.metrics.profit_factor >= 1.5 ? 'text-emerald-400' : 'text-slate-200'} />
                <MetricBox title="Sortino Ratio" value={results.metrics.sortino_ratio} color="text-indigo-400" />
                <MetricBox title="Sharpe Ratio" value={results.metrics.sharpe_ratio} color="text-purple-400" />
                <MetricBox title="Expectancy" value={`₹${results.metrics.expectancy}`} color={results.metrics.expectancy > 0 ? 'text-emerald-400' : 'text-rose-400'} />
                <MetricBox title="Max Drawdown" value={`${results.metrics.max_drawdown}%`} color="text-rose-400" />
                <MetricBox title="Max Loss Streak" value={`${results.metrics.max_consecutive_losses} trades`} color="text-amber-400" />
              </div>

              {/* Tab 1: Equity Curve */}
              {activeTab === 'EQUITY' && (
                <div className="space-y-6">
                  <div className="bg-slate-950/60 p-5 rounded-xl border border-slate-800">
                    <div className="flex justify-between items-center mb-4">
                      <h3 className="text-base font-bold text-slate-200 flex items-center gap-2">
                        <TrendingUp className="w-4 h-4 text-purple-400" />
                        Historical Backtested Equity Growth (₹1,00,000 Base)
                      </h3>
                      <span className="text-xs font-mono text-slate-400">
                        Final Portfolio: <strong className="text-emerald-400">₹{results.metrics.final_equity.toLocaleString('en-IN')}</strong>
                      </span>
                    </div>
                    <div className="h-64 w-full">
                      <ResponsiveContainer>
                        <AreaChart data={results.equity_curve}>
                          <defs>
                            <linearGradient id="colorEquityDark" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor="#9333ea" stopOpacity={0.4}/>
                              <stop offset="95%" stopColor="#9333ea" stopOpacity={0}/>
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#334155" />
                          <XAxis dataKey="date" tick={{fontSize: 11, fill: '#94a3b8'}} tickMargin={8} minTickGap={30} />
                          <YAxis domain={['auto', 'auto']} tick={{fontSize: 11, fill: '#94a3b8'}} tickFormatter={(val) => `₹${(val/1000).toFixed(0)}k`} width={55} />
                          <Tooltip 
                            contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '8px', color: '#f8fafc' }}
                            formatter={(value) => [`₹${Number(value).toLocaleString('en-IN')}`, 'Portfolio Value']}
                          />
                          <Area type="monotone" dataKey="equity" stroke="#a855f7" strokeWidth={2.5} fillOpacity={1} fill="url(#colorEquityDark)" />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* Trade Ledger Table */}
                  <div className="bg-slate-950/60 rounded-xl border border-slate-800 overflow-hidden">
                    <div className="p-4 bg-slate-950 border-b border-slate-800 flex justify-between items-center">
                      <h3 className="font-bold text-sm text-slate-200">Simulated Trade Ledger ({results.trades.length} trades)</h3>
                      <span className="text-xs font-mono text-slate-400">Win/Loss Ratio: <strong>{results.metrics.win_loss_ratio}x</strong></span>
                    </div>
                    <div className="max-h-56 overflow-y-auto">
                      <table className="w-full text-xs text-left text-slate-300">
                        <thead className="text-[10px] text-slate-400 uppercase bg-slate-900/90 sticky top-0 border-b border-slate-800">
                          <tr>
                            <th className="px-4 py-2.5">Entry Date</th>
                            <th className="px-4 py-2.5">Exit Date</th>
                            <th className="px-4 py-2.5">Entry</th>
                            <th className="px-4 py-2.5">Exit</th>
                            <th className="px-4 py-2.5">Status</th>
                            <th className="px-4 py-2.5 text-right">PnL (₹)</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-800/60">
                          {results.trades.map((t, idx) => (
                            <tr key={idx} className="hover:bg-slate-900/40">
                              <td className="px-4 py-2 font-mono text-slate-400">{t.entry_date}</td>
                              <td className="px-4 py-2 font-mono text-slate-400">{t.exit_date}</td>
                              <td className="px-4 py-2 font-mono">₹{t.entry_price}</td>
                              <td className="px-4 py-2 font-mono">₹{t.exit_price}</td>
                              <td className="px-4 py-2">
                                <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                                  t.status === 'TARGET MET' ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-rose-950 text-rose-400 border border-rose-800'
                                }`}>
                                  {t.status}
                                </span>
                              </td>
                              <td className={`px-4 py-2 font-mono font-bold text-right ${t.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                {t.pnl >= 0 ? `+₹${t.pnl.toLocaleString('en-IN')}` : `-₹${Math.abs(t.pnl).toLocaleString('en-IN')}`}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>
              )}

              {/* Tab 2: Monte Carlo Simulation */}
              {activeTab === 'MONTE_CARLO' && results.monte_carlo && (
                <div className="space-y-6">
                  {/* Monte Carlo Summary Gauges */}
                  <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
                    <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800">
                      <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Median Outcome (50th %ile)</span>
                      <div className="text-xl font-black text-purple-400 mt-1 font-mono">
                        ₹{results.monte_carlo.p50_final_equity.toLocaleString('en-IN')}
                      </div>
                      <p className="text-[11px] text-slate-500 mt-1">Expected capital after 50 future trades</p>
                    </div>

                    <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800">
                      <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Worst-Case Risk (5th %ile)</span>
                      <div className="text-xl font-black text-rose-400 mt-1 font-mono">
                        ₹{results.monte_carlo.p05_final_equity.toLocaleString('en-IN')}
                      </div>
                      <p className="text-[11px] text-slate-500 mt-1">95% statistical confidence floor</p>
                    </div>

                    <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800">
                      <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Expected Max Drawdown</span>
                      <div className="text-xl font-black text-amber-400 mt-1 font-mono">
                        {results.monte_carlo.expected_max_drawdown}%
                      </div>
                      <p className="text-[11px] text-slate-500 mt-1">Median peak-to-trough drop</p>
                    </div>

                    <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800">
                      <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Risk of Ruin (&gt;25% DD)</span>
                      <div className={`text-xl font-black mt-1 font-mono ${results.monte_carlo.prob_drawdown_25pct > 15 ? 'text-rose-400' : 'text-emerald-400'}`}>
                        {results.monte_carlo.prob_drawdown_25pct}%
                      </div>
                      <p className="text-[11px] text-slate-500 mt-1">Probability of severe portfolio drawdown</p>
                    </div>
                  </div>

                  {/* Monte Carlo Confidence Cone Chart */}
                  <div className="bg-slate-950/60 p-5 rounded-xl border border-slate-800">
                    <div className="flex justify-between items-center mb-4">
                      <h3 className="text-base font-bold text-slate-200 flex items-center gap-2">
                        <Dices className="w-4 h-4 text-emerald-400" />
                        1,000-Path Monte Carlo Confidence Cone (50 Future Trade Horizon)
                      </h3>
                      <div className="flex items-center gap-4 text-xs font-mono">
                        <span className="flex items-center gap-1.5 text-emerald-400"><span className="w-2.5 h-2.5 rounded-full bg-emerald-500 inline-block"></span> 95th %ile</span>
                        <span className="flex items-center gap-1.5 text-purple-400"><span className="w-2.5 h-2.5 rounded-full bg-purple-500 inline-block"></span> Median</span>
                        <span className="flex items-center gap-1.5 text-rose-400"><span className="w-2.5 h-2.5 rounded-full bg-rose-500 inline-block"></span> 5th %ile</span>
                      </div>
                    </div>
                    <div className="h-72 w-full">
                      <ResponsiveContainer>
                        <LineChart data={results.monte_carlo.confidence_cone}>
                          <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#334155" />
                          <XAxis dataKey="trade_num" tick={{fontSize: 11, fill: '#94a3b8'}} tickMargin={8} label={{ value: 'Future Trades', position: 'insideBottom', offset: -5, fill: '#94a3b8', fontSize: 11 }} />
                          <YAxis domain={['auto', 'auto']} tick={{fontSize: 11, fill: '#94a3b8'}} tickFormatter={(val) => `₹${(val/1000).toFixed(0)}k`} width={60} />
                          <Tooltip 
                            contentStyle={{ backgroundColor: '#0f172a', borderColor: '#334155', borderRadius: '8px', color: '#f8fafc', fontSize: '12px' }}
                            formatter={(value, name) => {
                              const labels = { p95: '95th %ile (Bull Case)', p75: '75th %ile', p50: '50th %ile (Median)', p25: '25th %ile', p05: '5th %ile (Bear Floor)' };
                              return [`₹${Number(value).toLocaleString('en-IN')}`, labels[name] || name];
                            }}
                          />
                          <Line type="monotone" dataKey="p95" stroke="#10b981" strokeWidth={2} dot={false} strokeDasharray="4 4" />
                          <Line type="monotone" dataKey="p50" stroke="#a855f7" strokeWidth={3} dot={false} />
                          <Line type="monotone" dataKey="p05" stroke="#f43f5e" strokeWidth={2} dot={false} strokeDasharray="4 4" />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                </div>
              )}

            </div>
          )}

        </div>
      </div>
    </div>
  );
}

function MetricBox({ title, value, color }) {
  return (
    <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800 text-center">
      <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wider block">{title}</span>
      <span className={`text-base font-black font-mono mt-0.5 block ${color}`}>{value}</span>
    </div>
  );
}
