import React, { useState, useEffect } from 'react';
import { History, CheckCircle, XCircle, Clock, AlertTriangle, ShieldAlert, ShieldCheck, ArrowRight, X, Info, Activity } from 'lucide-react';
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

function AITradeRiskAuditModal({ trade, onClose }) {
  if (!trade || !trade.risk_audit) return null;
  const audit = trade.risk_audit;
  const models = audit.model_breakdown || {};

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 backdrop-blur-xs p-4 animate-fade-in">
      <div className="bg-slate-900 border border-slate-700 text-white rounded-2xl shadow-2xl max-w-2xl w-full overflow-hidden flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="px-6 py-4 border-b border-slate-800 flex justify-between items-center bg-gradient-to-r from-slate-900 via-indigo-950/40 to-slate-900">
          <div className="flex items-center space-x-3">
            <div className={`w-9 h-9 rounded-xl flex items-center justify-center shadow-inner ${audit.risk_level === 'CRITICAL' ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30' : 'bg-amber-500/20 text-amber-400 border border-amber-500/30'}`}>
              {audit.risk_level === 'CRITICAL' ? <ShieldAlert size={20} /> : <AlertTriangle size={20} />}
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h3 className="font-bold text-lg text-white tracking-tight">{trade.ticker}</h3>
                <span className={`px-2 py-0.5 rounded text-[10px] font-black uppercase tracking-wider ${trade.direction === 'BULLISH' ? 'bg-emerald-950 text-emerald-300 border border-emerald-700' : 'bg-rose-950 text-rose-300 border border-rose-700'}`}>
                  {trade.direction}
                </span>
                <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-800 text-slate-300 border border-slate-700">
                  {trade.trade_type || 'SWING'}
                </span>
              </div>
              <p className="text-xs text-slate-400 mt-0.5">Live AI Risk &amp; Model Responsibility Audit</p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition cursor-pointer">
            <X size={20} />
          </button>
        </div>

        {/* Scrollable Body */}
        <div className="p-6 overflow-y-auto space-y-5">
          {/* Threat Meter */}
          <div className="bg-slate-850 p-4 rounded-xl border border-slate-800">
            <div className="flex justify-between items-center mb-2">
              <span className="text-xs font-bold uppercase tracking-wider text-slate-400">AI Panic Threat Meter</span>
              <span className={`text-xs font-mono font-bold px-2 py-0.5 rounded ${audit.panic_level >= 70 ? 'bg-rose-900/60 text-rose-300' : audit.panic_level >= 40 ? 'bg-amber-900/60 text-amber-300' : 'bg-emerald-900/60 text-emerald-300'}`}>
                {audit.panic_level} / 100 Threat Score
              </span>
            </div>
            <div className="w-full bg-slate-800 rounded-full h-2 overflow-hidden">
              <div 
                className={`h-2 rounded-full transition-all duration-500 ${audit.panic_level >= 70 ? 'bg-gradient-to-r from-amber-500 to-rose-500' : audit.panic_level >= 40 ? 'bg-gradient-to-r from-indigo-500 to-amber-400' : 'bg-emerald-500'}`}
                style={{ width: `${Math.min(100, Math.max(5, audit.panic_level))}%` }}
              />
            </div>
            <p className="text-[11px] text-slate-400 mt-2">
              {audit.risk_level === 'CRITICAL' 
                ? '🚨 Multiple models have triggered an Emergency Exit consensus. Capital is at high risk.' 
                : '⚠️ Market conditions have deteriorated against this trade. Tightening stop-loss protects capital from severe drawdown.'}
            </p>
          </div>

          {/* Actionable Stop Loss Plan */}
          <div className="bg-gradient-to-r from-indigo-950/60 via-slate-850 to-slate-900 p-4 rounded-xl border border-indigo-900/40">
            <div className="flex justify-between items-center mb-3">
              <span className="text-xs font-bold text-indigo-300 uppercase tracking-wider flex items-center">
                <ShieldCheck size={14} className="mr-1.5 text-indigo-400" /> Actionable Stop Loss Adjustment
              </span>
              <span className="text-[10px] font-bold bg-indigo-900/80 text-indigo-200 px-2 py-0.5 rounded border border-indigo-700/50">
                {audit.sl_mode}
              </span>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="bg-slate-900/80 p-3 rounded-lg border border-slate-800 text-center">
                <span className="text-[10px] text-slate-400 uppercase font-semibold block">Original Stop Loss</span>
                <span className="text-lg font-mono font-bold text-slate-300 line-through">₹{audit.original_sl?.toFixed(2)}</span>
              </div>
              <div className="bg-indigo-900/30 p-3 rounded-lg border border-indigo-500/40 text-center shadow-inner">
                <span className="text-[10px] text-indigo-300 uppercase font-bold block">Recommended Tightened SL</span>
                <span className="text-xl font-mono font-black text-emerald-400">₹{audit.tightened_sl?.toFixed(2)}</span>
              </div>
            </div>

            <div className="mt-3 flex items-center justify-between text-[11px] text-slate-300 pt-2 border-t border-slate-800/80">
              <span>Entry Fill: <strong className="text-white font-mono">₹{audit.entry?.toFixed(2)}</strong></span>
              <span className="text-emerald-400 font-bold">🛡️ {audit.risk_reduction_pct}% Risk Cut Active</span>
            </div>
          </div>

          {/* Model Responsibility Matrix */}
          <div>
            <h4 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">
              Multi-Model Responsibility Matrix
            </h4>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {Object.entries(models).map(([key, model]) => (
                <div 
                  key={key} 
                  className={`p-3 rounded-xl border transition ${model.triggered ? 'bg-rose-950/20 border-rose-800/40' : 'bg-slate-850/60 border-slate-800'}`}
                >
                  <div className="flex justify-between items-start mb-1.5">
                    <span className="font-bold text-xs text-white">{model.name}</span>
                    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded font-mono ${model.triggered ? 'bg-rose-900/70 text-rose-300 border border-rose-700' : 'bg-slate-800 text-slate-400'}`}>
                      {model.triggered ? `+${model.points} pts` : '0 pts'}
                    </span>
                  </div>
                  <p className="text-[11px] text-slate-300 leading-snug">{model.detail}</p>
                </div>
              ))}
            </div>
          </div>

          {/* Consensus Reasons List */}
          {audit.reasons && audit.reasons.length > 0 && (
            <div className="bg-slate-850 p-4 rounded-xl border border-slate-800">
              <span className="text-xs font-bold text-slate-400 uppercase tracking-wider block mb-2">
                Trigger Reasons Identified by AI Consensus
              </span>
              <ul className="space-y-1.5 text-xs text-slate-200">
                {audit.reasons.map((reason, idx) => (
                  <li key={idx} className="flex items-center text-amber-300">
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-400 mr-2 shrink-0"></span>
                    {reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 bg-slate-950 border-t border-slate-800 flex justify-between items-center">
          <span className="text-[11px] text-slate-500">
            Audit Evaluated: {new Date(audit.evaluated_at).toLocaleTimeString()}
          </span>
          <button 
            onClick={onClose}
            className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-xs rounded-lg transition shadow-md cursor-pointer"
          >
            Acknowledge &amp; Close
          </button>
        </div>
      </div>
    </div>
  );
}

export default function AITradeHistory({ tradeType, refreshTrigger = 0 }) {
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedAuditTrade, setSelectedAuditTrade] = useState(null);

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
              <th className="px-6 py-3 text-center font-semibold text-gray-500 uppercase text-xs">Conviction &amp; SHAP</th>
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
                <td className="px-6 py-4 text-right font-medium">
                  <div className="text-red-500">₹{trade.sl.toFixed(2)}</div>
                  {trade.risk_audit && trade.risk_audit.risk_level !== 'NORMAL' && (
                    <button 
                      onClick={() => setSelectedAuditTrade(trade)}
                      className="mt-1 inline-flex items-center text-[10px] bg-amber-50 hover:bg-amber-100 text-amber-800 border border-amber-300 font-bold px-1.5 py-0.5 rounded cursor-pointer transition shadow-2xs"
                      title="AI detected weakness: Click to view model audit"
                    >
                      <AlertTriangle size={10} className="mr-1 text-amber-600" />
                      Tighten: ₹{trade.risk_audit.tightened_sl.toFixed(2)} 🔍
                    </button>
                  )}
                </td>
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
                  ) : trade.risk_audit && trade.risk_audit.risk_level === 'CRITICAL' ? (
                    <button 
                      onClick={() => setSelectedAuditTrade(trade)}
                      className="bg-rose-100 hover:bg-rose-200 text-rose-800 border border-rose-300 px-2.5 py-1 rounded-full text-xs font-bold flex items-center justify-center w-max mx-auto cursor-pointer transition shadow-xs animate-pulse"
                      title="Click to view detailed model breakdown and exit advisory"
                    >
                      <ShieldAlert size={14} className="mr-1 text-rose-600" /> EARLY EXIT 🔍
                    </button>
                  ) : trade.risk_audit && trade.risk_audit.risk_level === 'WARNING' ? (
                    <button 
                      onClick={() => setSelectedAuditTrade(trade)}
                      className="bg-amber-100 hover:bg-amber-200 text-amber-800 border border-amber-300 px-2.5 py-1 rounded-full text-xs font-bold flex items-center justify-center w-max mx-auto cursor-pointer transition shadow-xs"
                      title="Click to view detailed model breakdown and stop loss advisory"
                    >
                      <AlertTriangle size={14} className="mr-1 text-amber-600" /> AI WARNING 🔍
                    </button>
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

      {/* Model Responsibility Audit Modal */}
      {selectedAuditTrade && (
        <AITradeRiskAuditModal 
          trade={selectedAuditTrade} 
          onClose={() => setSelectedAuditTrade(null)} 
        />
      )}
    </div>
  );
}
