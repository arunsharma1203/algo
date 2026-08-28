import React, { useState, useEffect } from 'react';
import { History, CheckCircle, XCircle, Clock } from 'lucide-react';
import axios from 'axios';

function ConvictionTooltip({ trade }) {
  const exp = trade.explanation;
  const [show, setShow] = useState(false);

  return (
    <div className="relative inline-block" onMouseEnter={() => setShow(true)} onMouseLeave={() => setShow(false)}>
      <span className="bg-purple-100 text-purple-800 hover:bg-purple-200 cursor-pointer px-2.5 py-1 rounded text-xs font-bold transition flex items-center justify-center space-x-1">
        <span>{trade.confidence ? trade.confidence.toFixed(1) : '-'}</span>
        <span className="text-[10px] text-purple-400">ℹ️</span>
      </span>

      {show && (
        <div className="absolute z-50 bottom-full left-1/2 transform -translate-x-1/2 mb-2 w-72 bg-slate-950 text-white rounded-xl shadow-2xl p-4 border border-slate-700 text-left font-sans text-xs animate-fade-in pointer-events-none">
          <div className="flex justify-between items-center border-b border-slate-800 pb-2 mb-2">
            <span className="font-bold text-slate-200">Feature Attribution (SHAP)</span>
            <span className="text-[10px] bg-purple-900/60 text-purple-300 px-1.5 py-0.5 rounded font-mono">
              Net: {trade.confidence?.toFixed(1)}%
            </span>
          </div>

          {exp ? (
            <div className="space-y-1.5 font-mono text-[11px]">
              <div className="flex justify-between">
                <span className="text-slate-400">⚡ Base Hunters ML:</span>
                <span className="font-bold text-blue-400">+{exp.base_score || trade.confidence?.toFixed(1)}%</span>
              </div>
              {exp.nlp_sentiment !== undefined && (
                <div className="flex justify-between">
                  <span className="text-slate-400">📰 FinBERT News:</span>
                  <span className={`font-bold ${exp.nlp_sentiment > 0 ? 'text-emerald-400' : exp.nlp_sentiment < 0 ? 'text-rose-400' : 'text-slate-400'}`}>
                    {exp.nlp_sentiment > 0 ? '+' : ''}{exp.nlp_sentiment} pts
                  </span>
                </div>
              )}
              {exp.volume_ratio !== undefined && (
                <div className="flex justify-between">
                  <span className="text-slate-400">📊 Vol Multiplier ({exp.volume_ratio}x):</span>
                  <span className={`font-bold ${(exp.adjustments?.volume_surge || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {(exp.adjustments?.volume_surge || 0) >= 0 ? '+' : ''}{exp.adjustments?.volume_surge || 0} pts
                  </span>
                </div>
              )}
              {exp.atr_pct !== undefined && (
                <div className="flex justify-between">
                  <span className="text-slate-400">⚡ ATR Volatility ({exp.atr_pct}%):</span>
                  <span className={`font-bold ${(exp.adjustments?.volatility || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {(exp.adjustments?.volatility || 0) >= 0 ? '+' : ''}{exp.adjustments?.volatility || 0} pts
                  </span>
                </div>
              )}
              {exp.macro_regime !== undefined && (
                <div className="flex justify-between">
                  <span className="text-slate-400">🌐 Macro Regime:</span>
                  <span className={`font-bold ${exp.macro_aligned ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {exp.macro_aligned ? '+4.0 pts' : '-8.0 pts'}
                  </span>
                </div>
              )}
              {exp.meta_message && (
                <div className="mt-2 pt-2 border-t border-slate-800 text-[10px] text-slate-300 font-sans italic">
                  "{exp.meta_message}"
                </div>
              )}
            </div>
          ) : (
            <div className="text-slate-400 text-[11px] font-sans">
              <p>Legacy single-model trade record.</p>
              <p className="mt-1 text-[10px] text-slate-500">Multi-signal SHAP attribution is recorded on all new scans.</p>
            </div>
          )}

          <div className="absolute top-full left-1/2 transform -translate-x-1/2 border-4 border-transparent border-t-slate-950"></div>
        </div>
      )}
    </div>
  );
}

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
              <th className="px-6 py-3 text-center font-semibold text-gray-500 uppercase text-xs">Conviction & SHAP</th>
              <th className="px-6 py-3 text-center font-semibold text-gray-500 uppercase text-xs">Engines Active</th>
              <th className="px-6 py-3 text-right font-semibold text-gray-500 uppercase text-xs">Entry / Eff.</th>
              <th className="px-6 py-3 text-right font-semibold text-gray-500 uppercase text-xs">Stop Loss</th>
              <th className="px-6 py-3 text-right font-semibold text-gray-500 uppercase text-xs">Target 1</th>
              <th className="px-6 py-3 text-right font-semibold text-gray-500 uppercase text-xs">Real P&amp;L (Net)</th>
              <th className="px-6 py-3 text-center font-semibold text-gray-500 uppercase text-xs">Live Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {history.filter(t => !tradeType || t.trade_type === tradeType || (tradeType === 'INTRADAY' && !t.trade_type)).length === 0 && !loading && (
              <tr>
                <td colSpan="9" className="px-6 py-8 text-center text-gray-400 font-medium">
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
                  <ConvictionTooltip trade={trade} />
                </td>
                <td className="px-6 py-4 text-center">
                  <div className="flex justify-center space-x-1" title={new Date(trade.timestamp).getTime() > new Date('2024-08-28T00:00:00').getTime() ? "All 4 Deep Learning Models Active (Hunters, FinBERT, Meta-Learner, Macro)" : "Legacy Single-Model Trade"}>
                    {/* Hunter ML */}
                    <span className="w-4 h-4 rounded-full bg-blue-500 flex items-center justify-center text-[8px] text-white font-bold" title="Hunter ML">H</span>
                    {new Date(trade.timestamp).getTime() > new Date('2026-08-28T00:00:00').getTime() ? (
                      <>
                        {/* NLP FinBERT */}
                        <span className="w-4 h-4 rounded-full bg-purple-500 flex items-center justify-center text-[8px] text-white font-bold" title="FinBERT NLP">N</span>
                        {/* Meta-Learner */}
                        <span className="w-4 h-4 rounded-full bg-orange-500 flex items-center justify-center text-[8px] text-white font-bold" title="Layer-2 Meta-Learner">M</span>
                        {/* Macro Regime */}
                        <span className="w-4 h-4 rounded-full bg-red-500 flex items-center justify-center text-[8px] text-white font-bold" title="Macro Regime Engine">R</span>
                      </>
                    ) : (
                      <>
                        <span className="w-4 h-4 rounded-full bg-gray-200 flex items-center justify-center text-[8px] text-gray-400 font-bold" title="Offline">N</span>
                        <span className="w-4 h-4 rounded-full bg-gray-200 flex items-center justify-center text-[8px] text-gray-400 font-bold" title="Offline">M</span>
                        <span className="w-4 h-4 rounded-full bg-gray-200 flex items-center justify-center text-[8px] text-gray-400 font-bold" title="Offline">R</span>
                      </>
                    )}
                  </div>
                </td>
                <td className="px-6 py-4 text-right">
                  <div className="text-gray-800 font-medium">₹{trade.entry.toFixed(2)}</div>
                  {trade.effective_entry && (
                    <div className="text-[10px] text-slate-400" title={`Modeled with ${trade.slippage_pct}% execution slippage`}>
                      Eff: ₹{trade.effective_entry.toFixed(2)}
                    </div>
                  )}
                </td>
                <td className="px-6 py-4 text-right text-red-500 font-medium">₹{trade.sl.toFixed(2)}</td>
                <td className="px-6 py-4 text-right text-green-500 font-medium">₹{trade.tp1.toFixed(2)}</td>
                <td className="px-6 py-4 text-right font-mono">
                  <span className={`font-bold ${trade.profit_pct > 0 ? 'text-green-600' : trade.profit_pct < 0 ? 'text-red-600' : 'text-gray-500'}`}>
                    {trade.profit_pct > 0 ? '+' : ''}{trade.profit_pct?.toFixed(2)}%
                  </span>
                  {trade.slippage_drag !== undefined && trade.slippage_drag !== 0 && (
                    <span className="block text-[9px] text-slate-400" title="Slippage Friction Drag">
                      Drag: -{Math.abs(trade.slippage_drag).toFixed(2)}%
                    </span>
                  )}
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
