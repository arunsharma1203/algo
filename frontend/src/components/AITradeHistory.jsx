import React, { useState, useEffect } from 'react';
import { History, CheckCircle, XCircle, Clock } from 'lucide-react';
import axios from 'axios';

export default function AITradeHistory({ tradeType, refreshTrigger = 0 }) {
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchHistory = async () => {
      try {
        const res = await axios.get('http://localhost:8000/api/ml/history');
        setHistory(res.data || []);
      } catch (e) {
        console.error(e);
      } finally {
        setLoading(false);
      }
    };
    fetchHistory();
    // Refresh every 60 seconds
    const interval = setInterval(fetchHistory, 60000);
    return () => clearInterval(interval);
  }, [tradeType, refreshTrigger]);

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden mt-10">
      <div className="bg-gray-50 px-6 py-4 border-b border-gray-200 flex justify-between items-center">
        <h2 className="font-bold text-gray-800 flex items-center">
          <History className="mr-2 text-indigo-500" size={20} />
          Live AI Trade Evaluator
        </h2>
        {loading && <span className="text-xs text-indigo-600 font-bold animate-pulse">Syncing...</span>}
      </div>
      
      <div className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-white border-b border-gray-100">
            <tr>
              <th className="px-6 py-3 text-left font-semibold text-gray-500 uppercase text-xs">Timestamp</th>
              <th className="px-6 py-3 text-left font-semibold text-gray-500 uppercase text-xs">Symbol</th>
              <th className="px-6 py-3 text-center font-semibold text-gray-500 uppercase text-xs">Conviction</th>
              <th className="px-6 py-3 text-right font-semibold text-gray-500 uppercase text-xs">Entry</th>
              <th className="px-6 py-3 text-right font-semibold text-gray-500 uppercase text-xs">Stop Loss</th>
              <th className="px-6 py-3 text-right font-semibold text-gray-500 uppercase text-xs">Target 1</th>
              <th className="px-6 py-3 text-right font-semibold text-gray-500 uppercase text-xs">Highest/Lowest</th>
              <th className="px-6 py-3 text-center font-semibold text-gray-500 uppercase text-xs">Live Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {history.filter(t => !tradeType || t.trade_type === tradeType || (tradeType === 'INTRADAY' && !t.trade_type)).length === 0 && !loading && (
              <tr>
                <td colSpan="8" className="px-6 py-8 text-center text-gray-400 font-medium">
                  No trades found in the AI memory vault.
                </td>
              </tr>
            )}
            {history.filter(t => !tradeType || t.trade_type === tradeType || (tradeType === 'INTRADAY' && !t.trade_type)).map((trade, i) => (
              <tr key={i} className="hover:bg-gray-50 font-mono">
                <td className="px-6 py-4 text-gray-500">{new Date(trade.timestamp).toLocaleString()}</td>
                <td className="px-6 py-4 font-bold text-gray-800">
                  <span className={trade.direction === 'BULLISH' ? 'text-green-600' : 'text-red-600'}>
                    {trade.ticker}
                  </span>
                </td>
                <td className="px-6 py-4 text-center">
                  <span className="bg-purple-100 text-purple-800 px-2 py-1 rounded text-xs font-bold">
                    {trade.confidence ? trade.confidence.toFixed(1) : '-'}
                  </span>
                </td>
                <td className="px-6 py-4 text-right text-gray-700 font-medium">{trade.entry.toFixed(2)}</td>
                <td className="px-6 py-4 text-right text-red-500 font-medium">{trade.sl.toFixed(2)}</td>
                <td className="px-6 py-4 text-right text-green-500 font-medium">{trade.tp1.toFixed(2)}</td>
                <td className="px-6 py-4 text-right text-gray-600">
                  H: {trade.max_price ? trade.max_price.toFixed(2) : '-'} <br/>
                  L: {trade.min_price ? trade.min_price.toFixed(2) : '-'}
                </td>
                <td className="px-6 py-4 text-center">
                  {trade.outcome === 'TARGET MET' ? (
                    <span className="bg-green-100 text-green-800 px-3 py-1 rounded-full text-xs font-bold flex items-center justify-center w-max mx-auto">
                      <CheckCircle size={14} className="mr-1" /> TARGET HIT
                    </span>
                  ) : trade.outcome === 'SL HIT' ? (
                    <span className="bg-red-100 text-red-800 px-3 py-1 rounded-full text-xs font-bold flex items-center justify-center w-max mx-auto">
                      <XCircle size={14} className="mr-1" /> STOPPED OUT
                    </span>
                  ) : trade.outcome === 'SQUARED OFF (3:15 PM)' ? (
                    <span className="bg-blue-100 text-blue-800 px-3 py-1 rounded-full text-xs font-bold flex items-center justify-center w-max mx-auto">
                      <CheckCircle size={14} className="mr-1" /> SQ OFF
                    </span>
                  ) : (
                    <span className="bg-yellow-100 text-yellow-800 px-3 py-1 rounded-full text-xs font-bold flex items-center justify-center w-max mx-auto">
                      <Clock size={14} className="mr-1" /> ACTIVE
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
