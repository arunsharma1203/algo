import React, { useState } from 'react';
import { runBacktest } from '../services/api';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import TickerSearch from './TickerSearch';
import { useLiveIndicator } from '../context/LiveIndicatorContext';

export default function BacktestViewer({ strategy }) {
  const [ticker, setTicker] = useState('');
  
  // Set default end date to today, start date to 1 year ago
  const today = new Date();
  const lastYear = new Date();
  lastYear.setFullYear(today.getFullYear() - 1);
  
  const [startDate, setStartDate] = useState(lastYear.toISOString().split('T')[0]);
  const [endDate, setEndDate] = useState(today.toISOString().split('T')[0]);
  const [interval, setInterval] = useState('1d');
  
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);
  const { triggerFetchIndicator } = useLiveIndicator();

  const handleRun = async () => {
    if (!ticker.trim()) {
      setError("Please enter a ticker symbol to backtest.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const data = await runBacktest({
        ticker,
        start_date: startDate,
        end_date: endDate,
        initial_capital: 100000,
        interval: interval,
        strategy: strategy
      });
      setResults(data);
      if (data.metadata) triggerFetchIndicator(data.metadata);
      
      // Save last backtest stats for Dashboard with the ticker
      localStorage.setItem('last_backtest_stats', JSON.stringify({
        ticker: ticker.toUpperCase(),
        cagr: data.metrics.cagr,
        max_drawdown: data.metrics.max_drawdown
      }));
    } catch (err) {
      setError(err.response?.data?.detail || "An error occurred during backtesting");
    } finally {
      setLoading(false);
    }
  };

  const renderRuleCondition = (c) => {
    const leftStr = c.left.name + (c.left.params?.period ? `(${c.left.params.period})` : '');
    const rightStr = c.right.name ? (c.right.name + (c.right.params?.period ? `(${c.right.params.period})` : '')) : c.right;
    return `${leftStr} ${c.operator.replace('_', ' ')} ${rightStr}`;
  };

  return (
    <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200">
      <h2 className="text-2xl font-bold mb-4">{strategy.name} - Backtest Configuration</h2>
      
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Ticker Symbol</label>
          <TickerSearch 
            value={ticker} 
            onChange={setTicker} 
            placeholder="e.g. RELIANCE.NS" 
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Start Date</label>
          <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} className="w-full border border-gray-300 rounded-md p-2 focus:ring-blue-500 focus:border-blue-500 text-gray-700 shadow-sm" />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">End Date</label>
          <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} className="w-full border border-gray-300 rounded-md p-2 focus:ring-blue-500 focus:border-blue-500 text-gray-700 shadow-sm" />
        </div>
        <div className="flex items-end">
          <button onClick={handleRun} disabled={loading} className="w-full bg-green-600 hover:bg-green-700 text-white font-bold py-2 px-4 rounded transition">
            {loading ? 'Running...' : 'Run Backtest'}
          </button>
        </div>
      </div>
      
      {error && (
        <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-4">
          {error}
        </div>
      )}

      {results && results.metrics && (
        <div className="mt-8 border-t pt-8">
          
          <div className="bg-blue-50 p-4 rounded-lg border border-blue-100 mb-8 text-sm">
            <h3 className="font-bold text-blue-800 mb-2">Backtest Summary: {ticker.toUpperCase()} ({startDate} to {endDate})</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <span className="font-semibold text-blue-700">Entry Logic ({strategy.entry.logic}):</span>
                <ul className="list-disc pl-4 text-blue-900 mt-1">
                  {strategy.entry.conditions.map((c, i) => <li key={i}>{renderRuleCondition(c)}</li>)}
                </ul>
              </div>
              <div>
                <span className="font-semibold text-blue-700">Exit Logic ({strategy.exit.logic}):</span>
                <ul className="list-disc pl-4 text-blue-900 mt-1">
                  {strategy.exit.conditions.map((c, i) => <li key={i}>{renderRuleCondition(c)}</li>)}
                </ul>
                <div className="mt-2 text-blue-800 font-medium">
                  {strategy.risk?.stop_loss_pct && <span>Stop Loss: {strategy.risk.stop_loss_pct}% </span>}
                  {strategy.risk?.take_profit_pct && <span>Take Profit: {strategy.risk.take_profit_pct}%</span>}
                </div>
              </div>
            </div>
          </div>

          <h3 className="text-xl font-bold mb-4">Performance Metrics</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
            <div className="bg-gray-50 p-4 rounded border hover:shadow-sm transition">
              <p className="text-sm text-gray-500 font-medium">Total Return</p>
              <p className={`text-2xl font-bold mt-1 ${results.metrics.total_return >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                {results.metrics.total_return.toFixed(2)}%
              </p>
            </div>
            <div className="bg-gray-50 p-4 rounded border hover:shadow-sm transition">
              <p className="text-sm text-gray-500 font-medium">CAGR</p>
              <p className="text-2xl font-bold mt-1 text-gray-800">{results.metrics.cagr.toFixed(2)}%</p>
            </div>
            <div className="bg-gray-50 p-4 rounded border hover:shadow-sm transition">
              <p className="text-sm text-gray-500 font-medium">Max Drawdown</p>
              <p className="text-2xl font-bold mt-1 text-red-500">{results.metrics.max_drawdown.toFixed(2)}%</p>
            </div>
            <div className="bg-gray-50 p-4 rounded border hover:shadow-sm transition">
              <p className="text-sm text-gray-500 font-medium">Win Rate</p>
              <p className="text-2xl font-bold mt-1 text-gray-800">{results.metrics.win_rate.toFixed(1)}%</p>
            </div>
            <div className="bg-gray-50 p-4 rounded border hover:shadow-sm transition">
              <p className="text-sm text-gray-500 font-medium">Total Trades</p>
              <p className="text-2xl font-bold mt-1 text-gray-800">{results.metrics.total_trades}</p>
            </div>
            <div className="bg-gray-50 p-4 rounded border hover:shadow-sm transition">
              <p className="text-sm text-gray-500 font-medium">Profit Factor</p>
              <p className="text-2xl font-bold mt-1 text-gray-800">{results.metrics.profit_factor.toFixed(2)}</p>
            </div>
          </div>

          <h3 className="text-xl font-bold mb-4">Equity Curve</h3>
          <div className="h-80 w-full mb-8">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={results.equity_curve}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="date" tick={{fontSize: 12}} tickFormatter={(tick) => tick.substring(0, 10)} minTickGap={30} />
                <YAxis domain={['auto', 'auto']} tickFormatter={(tick) => `₹${(tick/1000).toFixed(0)}k`} width={80} />
                <Tooltip formatter={(value) => [`₹${value.toFixed(2)}`, 'Equity']} labelFormatter={(label) => label.substring(0,10)} />
                <Legend />
                <Line type="monotone" dataKey="equity" stroke="#2563eb" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          
          <h3 className="text-xl font-bold mb-4">Trade History</h3>
          <div className="overflow-x-auto rounded-lg border border-gray-200">
            <table className="min-w-full bg-white">
              <thead>
                <tr className="bg-gray-50 text-gray-600 uppercase text-xs leading-normal border-b">
                  <th className="py-3 px-6 text-left font-semibold">Entry Date</th>
                  <th className="py-3 px-6 text-left font-semibold">Exit Date</th>
                  <th className="py-3 px-6 text-center font-semibold">Days</th>
                  <th className="py-3 px-6 text-right font-semibold">Entry Price</th>
                  <th className="py-3 px-6 text-right font-semibold">Exit Price</th>
                  <th className="py-3 px-6 text-right font-semibold">Shares</th>
                  <th className="py-3 px-6 text-right font-semibold">Net P&L</th>
                  <th className="py-3 px-6 text-right font-semibold">Return %</th>
                  <th className="py-3 px-6 text-left font-semibold">Exit Reason</th>
                </tr>
              </thead>
              <tbody className="text-gray-600 text-sm font-light">
                {results.trades.map((trade, i) => (
                  <tr key={i} className="border-b border-gray-100 hover:bg-gray-50">
                    <td className="py-3 px-6 text-left whitespace-nowrap">{trade.entry_date.substring(0, 10)}</td>
                    <td className="py-3 px-6 text-left whitespace-nowrap">{trade.exit_date.substring(0, 10)}</td>
                    <td className="py-3 px-6 text-center">{trade.holding_days}</td>
                    <td className="py-3 px-6 text-right font-medium">₹{trade.entry_price.toFixed(2)}</td>
                    <td className="py-3 px-6 text-right font-medium">₹{trade.exit_price.toFixed(2)}</td>
                    <td className="py-3 px-6 text-right">{trade.shares}</td>
                    <td className={`py-3 px-6 text-right font-bold ${trade.net_pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                      {trade.net_pnl > 0 ? '+' : ''}₹{trade.net_pnl.toFixed(2)}
                    </td>
                    <td className={`py-3 px-6 text-right font-bold ${trade.return_pct >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                      {trade.return_pct > 0 ? '+' : ''}{trade.return_pct.toFixed(2)}%
                    </td>
                    <td className="py-3 px-6 text-left whitespace-nowrap">
                      <span className={`px-2 py-1 rounded text-xs font-semibold ${
                        trade.exit_reason === 'Take Profit Hit' ? 'bg-green-100 text-green-800' : 
                        trade.exit_reason === 'Stop Loss Hit' ? 'bg-red-100 text-red-800' : 
                        'bg-blue-100 text-blue-800'
                      }`}>
                        {trade.exit_reason || "Exit Rules Met"}
                      </span>
                    </td>
                  </tr>
                ))}
                {results.trades.length === 0 && (
                  <tr>
                    <td colSpan="9" className="py-8 text-center text-gray-500 font-medium">No trades were executed for this strategy during the selected timeframe.</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
