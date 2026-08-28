import React, { useState, useEffect } from 'react';
import { X, Play, Loader, TrendingUp, AlertTriangle, Activity } from 'lucide-react';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

export default function MLBacktestModal({ isOpen, onClose, ticker, defaultType = 'SWING' }) {
  const [modelType, setModelType] = useState(defaultType);
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (isOpen) {
      setResults(null);
      setError(null);
    }
  }, [isOpen]);

  const runSimulation = async () => {
    if (!ticker) return;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch('http://localhost:8000/api/ml/backtest-simulate', {
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
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-6 bg-black/60 backdrop-blur-sm">
      <div className="bg-white rounded-xl shadow-2xl w-full max-w-5xl max-h-[90vh] overflow-hidden flex flex-col border border-gray-200">
        
        {/* Header */}
        <div className="flex justify-between items-center p-6 border-b border-gray-100 bg-gray-50">
          <div>
            <h2 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
              <Activity className="w-6 h-6 text-purple-600" />
              AI Paper Trading Simulator
            </h2>
            <p className="text-gray-500 text-sm mt-1">Train & Test Ensemble Models over 5 years of history</p>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-gray-200 rounded-full transition-colors">
            <X className="w-6 h-6 text-gray-500" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-6 bg-gray-50">
          
          {/* Controls */}
          <div className="bg-white p-4 rounded-xl border border-gray-200 shadow-sm flex flex-wrap items-end gap-4 mb-6">
            <div className="flex-1 min-w-[200px]">
              <label className="block text-sm font-bold text-gray-700 mb-1">Target Ticker</label>
              <input 
                type="text" 
                value={ticker} 
                disabled 
                className="w-full bg-gray-100 border border-gray-200 rounded-lg p-2.5 font-mono text-gray-600 font-bold"
              />
            </div>
            
            <div className="flex-1 min-w-[200px]">
              <label className="block text-sm font-bold text-gray-700 mb-1">ML Engine</label>
              <select 
                value={modelType} 
                onChange={(e) => setModelType(e.target.value)}
                className="w-full bg-white border border-gray-200 rounded-lg p-2.5 font-medium text-gray-800 focus:ring-2 focus:ring-purple-500"
              >
                <option value="SWING">Swing ML (1D Candles, 5 Years)</option>
                <option value="INTRADAY">Intraday ML (15m Candles, 60 Days)</option>
              </select>
            </div>

            <button 
              onClick={runSimulation}
              disabled={loading}
              className={`px-6 py-2.5 rounded-lg font-bold flex items-center gap-2 text-white shadow-md transition-all ${
                loading ? 'bg-purple-400 cursor-not-allowed' : 'bg-purple-600 hover:bg-purple-700 hover:shadow-lg'
              }`}
            >
              {loading ? <Loader className="w-5 h-5 animate-spin" /> : <Play className="w-5 h-5" />}
              {loading ? 'Training & Simulating...' : 'Run Simulation'}
            </button>
          </div>

          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 p-4 rounded-xl flex items-start gap-3 mb-6">
              <AlertTriangle className="w-5 h-5 flex-shrink-0 mt-0.5" />
              <p className="text-sm font-medium">{error}</p>
            </div>
          )}

          {/* Results Dashboard */}
          {results && (
            <div className="space-y-6">
              {/* Metrics */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <MetricCard title="Win Rate" value={`${results.metrics.win_rate}%`} color={results.metrics.win_rate > 50 ? 'text-green-600' : 'text-orange-600'} />
                <MetricCard title="Net PnL" value={`₹${results.metrics.total_pnl}`} color={results.metrics.total_pnl > 0 ? 'text-green-600' : 'text-red-600'} />
                <MetricCard title="Max Drawdown" value={`${results.metrics.max_drawdown}%`} color="text-red-600" />
                <MetricCard title="Sharpe Ratio" value={results.metrics.sharpe_ratio} color="text-indigo-600" />
              </div>

              {/* Chart */}
              <div className="bg-white p-6 rounded-xl border border-gray-200 shadow-sm">
                <h3 className="text-lg font-bold text-gray-800 mb-4 flex items-center gap-2">
                  <TrendingUp className="w-5 h-5 text-purple-600" />
                  Simulated Equity Curve (₹1,00,000 Starting)
                </h3>
                <div className="h-64 w-full">
                  <ResponsiveContainer>
                    <AreaChart data={results.equity_curve}>
                      <defs>
                        <linearGradient id="colorEquity" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#8b5cf6" stopOpacity={0.3}/>
                          <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0}/>
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#e5e7eb" />
                      <XAxis dataKey="date" tick={{fontSize: 12, fill: '#6b7280'}} tickMargin={10} minTickGap={30} />
                      <YAxis domain={['auto', 'auto']} tick={{fontSize: 12, fill: '#6b7280'}} tickFormatter={(val) => `₹${(val/1000)}k`} width={60} />
                      <Tooltip 
                        contentStyle={{ borderRadius: '8px', border: 'none', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)' }}
                        formatter={(value) => [`₹${value}`, 'Portfolio Value']}
                      />
                      <Area type="monotone" dataKey="equity" stroke="#8b5cf6" strokeWidth={2} fillOpacity={1} fill="url(#colorEquity)" />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>

              {/* Trade Log */}
              <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                <div className="p-4 bg-gray-50 border-b border-gray-200 flex justify-between items-center">
                  <h3 className="font-bold text-gray-800">Trade Ledger ({results.trades.length} trades)</h3>
                </div>
                <div className="max-h-60 overflow-y-auto">
                  <table className="w-full text-sm text-left">
                    <thead className="text-xs text-gray-500 uppercase bg-white sticky top-0 border-b border-gray-200">
                      <tr>
                        <th className="px-6 py-3">Entry Date</th>
                        <th className="px-6 py-3">Exit Date</th>
                        <th className="px-6 py-3">Entry</th>
                        <th className="px-6 py-3">Exit</th>
                        <th className="px-6 py-3">Status</th>
                        <th className="px-6 py-3 text-right">PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {results.trades.map((trade, idx) => (
                        <tr key={idx} className="border-b border-gray-50 hover:bg-gray-50 transition-colors">
                          <td className="px-6 py-3 font-medium text-gray-700">{trade.entry_date}</td>
                          <td className="px-6 py-3 text-gray-600">{trade.exit_date}</td>
                          <td className="px-6 py-3 font-mono">₹{trade.entry_price}</td>
                          <td className="px-6 py-3 font-mono">₹{trade.exit_price}</td>
                          <td className="px-6 py-3">
                            <span className={`px-2.5 py-1 text-xs font-bold rounded-full ${
                              trade.status === 'TARGET MET' ? 'bg-green-100 text-green-700' : 
                              trade.status === 'SL HIT' ? 'bg-red-100 text-red-700' : 'bg-gray-100 text-gray-700'
                            }`}>
                              {trade.status}
                            </span>
                          </td>
                          <td className={`px-6 py-3 text-right font-bold font-mono ${trade.pnl > 0 ? 'text-green-600' : 'text-red-600'}`}>
                            {trade.pnl > 0 ? '+' : ''}₹{trade.pnl}
                          </td>
                        </tr>
                      ))}
                      {results.trades.length === 0 && (
                        <tr>
                          <td colSpan="6" className="px-6 py-8 text-center text-gray-500 italic">No trades taken during test period.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function MetricCard({ title, value, color }) {
  return (
    <div className="bg-white p-4 rounded-xl border border-gray-200 shadow-sm flex flex-col justify-center items-center text-center">
      <span className="text-xs font-bold uppercase tracking-wider text-gray-500 mb-1">{title}</span>
      <span className={`text-2xl font-black ${color}`}>{value}</span>
    </div>
  );
}
