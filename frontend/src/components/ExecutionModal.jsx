import React, { useState, useEffect } from 'react';
import { X, ShieldAlert, Zap, Calculator, Flame, ShieldCheck } from 'lucide-react';
import axios from 'axios';

export default function ExecutionModal({ trade, onClose, onSuccess }) {
  // Load global profile config
  const savedProfileStr = localStorage.getItem('swing_profile');
  const defaultProfile = savedProfileStr ? JSON.parse(savedProfileStr) : {
    apiKey: localStorage.getItem('indmoney_api_token') || '',
    simulationMode: true,
    defaultCapital: 100000
  };

  const [apiToken, setApiToken] = useState(defaultProfile.apiKey);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [sizingMode, setSizingMode] = useState('KELLY'); // 'KELLY' or 'FIXED'
  const [kellyFraction, setKellyFraction] = useState('HALF'); // 'QUARTER', 'HALF', 'FULL'
  const [portfolioHeat, setPortfolioHeat] = useState(null);

  const capital = Number(defaultProfile.defaultCapital) || 100000;
  const maxRisk = Number(defaultProfile.maxRiskPerTrade) || 2.0;

  // 1. Fetch live portfolio heat
  useEffect(() => {
    axios.get(`http://localhost:8000/api/broker/portfolio-heat?capital=${capital}`)
      .then(res => setPortfolioHeat(res.data))
      .catch(() => {});
  }, [capital]);

  // 2. Compute Position Sizing
  const riskPerShare = Math.abs(trade.entry - trade.sl);
  const rewardPerShare = Math.abs(trade.tp1 - trade.entry);
  const rrRatio = riskPerShare > 0 ? (rewardPerShare / riskPerShare) : 1.5;
  const winProb = Number(trade.confidence) || 65.0;

  // Kelly Sizing Math
  const p = winProb / 100.0;
  const q = 1.0 - p;
  const fullKelly = rrRatio > 0 ? ((rrRatio * p - q) / rrRatio) : 0.02;
  const fractionMultipliers = { QUARTER: 0.25, HALF: 0.50, FULL: 1.0 };
  const adjKelly = Math.max(0.005, Math.min(0.05, Math.max(0, fullKelly * (fractionMultipliers[kellyFraction] || 0.5))));
  
  const effectiveRiskPct = sizingMode === 'KELLY' ? (adjKelly * 100.0) : maxRisk;
  const riskAmount = capital * (effectiveRiskPct / 100.0);
  
  let qty = 0;
  if (riskPerShare > 0) {
    qty = Math.floor(riskAmount / riskPerShare);
  }
  const maxQtyByCapital = Math.floor(capital / trade.entry);
  qty = Math.max(1, Math.min(qty, maxQtyByCapital));

  const potentialLoss = (riskPerShare * qty).toFixed(2);
  const potentialGain = (rewardPerShare * qty).toFixed(2);
  const totalCommitment = (trade.entry * qty).toFixed(2);

  const handleExecute = async () => {
    if (!apiToken || apiToken.length < 10) {
      setError("Please enter a valid Broker API Token");
      return;
    }
    
    localStorage.setItem('indmoney_api_token', apiToken);
    setLoading(true);
    setError(null);
    try {
      const response = await axios.post('http://localhost:8000/api/broker/execute', {
        api_token: apiToken,
        ticker: trade.ticker,
        action: trade.action || (trade.direction === 'BEARISH' ? 'SELL' : 'BUY'),
        quantity: qty,
        target: trade.tp1,
        stop_loss: trade.sl,
        order_type: "LIMIT",
        simulation: defaultProfile.simulationMode
      });
      
      onSuccess(response.data);
    } catch (err) {
      setError(err.response?.data?.detail || "Execution failed");
    } finally {
      setLoading(false);
    }
  };

  const action = trade.action || (trade.direction === 'BEARISH' ? 'SELL' : 'BUY');

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-slate-900 text-slate-100 rounded-2xl max-w-lg w-full shadow-2xl overflow-hidden border border-slate-700 flex flex-col">
        
        {/* Header */}
        <div className={`p-4 flex justify-between items-center ${action === 'BUY' ? 'bg-emerald-600' : 'bg-rose-600'}`}>
          <h2 className="text-lg font-black text-white flex items-center gap-2">
            <Zap size={20} className="fill-current" /> 
            1-Click Quant Order: {trade.ticker}
          </h2>
          <button onClick={onClose} className="text-white/80 hover:text-white p-1 rounded-lg hover:bg-black/20">
            <X size={20} />
          </button>
        </div>
        
        {/* Safeguard Warning */}
        <div className={`px-4 py-2.5 flex items-center justify-between border-b ${
          defaultProfile.simulationMode ? 'bg-indigo-950/60 border-indigo-800/50 text-indigo-300' : 'bg-amber-950/60 border-amber-800/50 text-amber-300'
        }`}>
          <div className="flex items-center gap-2 text-xs font-semibold">
            <ShieldAlert size={16} className="flex-shrink-0" />
            <span>{defaultProfile.simulationMode ? 'Paper Trading Simulation' : 'Live Order Routing'}</span>
          </div>
          {portfolioHeat && (
            <div className="flex items-center gap-1.5 text-xs font-mono">
              <Flame size={14} className={portfolioHeat.status === 'MAX_REACHED' ? 'text-rose-400' : 'text-amber-400'} />
              <span>Heat: <strong>{portfolioHeat.current_heat_pct}%</strong> / {portfolioHeat.max_heat_cap_pct}%</span>
            </div>
          )}
        </div>

        <div className="p-5 space-y-4 overflow-y-auto max-h-[75vh]">
          
          {/* Trade Parameters Grid */}
          <div className="grid grid-cols-3 gap-2.5">
            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800 text-center">
              <span className="text-[10px] text-slate-400 block font-bold uppercase tracking-wider">Entry</span>
              <span className="text-sm font-black font-mono text-slate-200">₹{trade.entry}</span>
            </div>
            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800 text-center">
              <span className="text-[10px] text-slate-400 block font-bold uppercase tracking-wider">Stop Loss</span>
              <span className="text-sm font-black font-mono text-rose-400">₹{trade.sl}</span>
            </div>
            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800 text-center">
              <span className="text-[10px] text-slate-400 block font-bold uppercase tracking-wider">Target 1</span>
              <span className="text-sm font-black font-mono text-emerald-400">₹{trade.tp1}</span>
            </div>
          </div>

          {/* Sizing Strategy Selector */}
          <div className="bg-slate-950/60 p-3.5 rounded-xl border border-slate-800 space-y-3">
            <div className="flex justify-between items-center">
              <span className="text-xs font-bold text-slate-300 flex items-center gap-1.5">
                <Calculator size={15} className="text-purple-400" />
                Position Sizing Model
              </span>
              <div className="flex bg-slate-900 rounded-lg p-0.5 border border-slate-700">
                <button
                  type="button"
                  onClick={() => setSizingMode('KELLY')}
                  className={`px-2.5 py-1 rounded-md text-[11px] font-bold transition-all ${
                    sizingMode === 'KELLY' ? 'bg-purple-600 text-white shadow' : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  Kelly Criterion
                </button>
                <button
                  type="button"
                  onClick={() => setSizingMode('FIXED')}
                  className={`px-2.5 py-1 rounded-md text-[11px] font-bold transition-all ${
                    sizingMode === 'FIXED' ? 'bg-purple-600 text-white shadow' : 'text-slate-400 hover:text-slate-200'
                  }`}
                >
                  Fixed {maxRisk}% Risk
                </button>
              </div>
            </div>

            {sizingMode === 'KELLY' && (
              <div className="bg-slate-900/80 p-2.5 rounded-lg border border-slate-800 flex items-center justify-between text-xs">
                <div>
                  <span className="text-slate-400 block text-[10px]">AI Calibrated Win Rate: <strong className="text-purple-300">{winProb}%</strong> (R:R {rrRatio.toFixed(1)}x)</span>
                  <span className="text-slate-300 font-semibold text-xs mt-0.5 block">
                    Kelly Multiplier: <strong className="text-emerald-400">{effectiveRiskPct.toFixed(2)}% of Capital</strong>
                  </span>
                </div>
                <div className="flex gap-1">
                  {['QUARTER', 'HALF', 'FULL'].map(f => (
                    <button
                      key={f}
                      type="button"
                      onClick={() => setKellyFraction(f)}
                      className={`px-2 py-0.5 rounded text-[10px] font-bold border transition-all ${
                        kellyFraction === f 
                          ? 'bg-purple-950 border-purple-500 text-purple-300' 
                          : 'bg-slate-950 border-slate-800 text-slate-500 hover:text-slate-300'
                      }`}
                    >
                      {f[0]}K
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Sizing Breakdown Details */}
            <div className="grid grid-cols-3 gap-2 text-center pt-1 border-t border-slate-800/80 text-xs">
              <div>
                <span className="text-[10px] text-slate-500 block">Allocated Qty</span>
                <span className="font-mono font-bold text-slate-200">{qty} shares</span>
              </div>
              <div>
                <span className="text-[10px] text-slate-500 block">Total Capital</span>
                <span className="font-mono font-bold text-slate-200">₹{Number(totalCommitment).toLocaleString('en-IN')}</span>
              </div>
              <div>
                <span className="text-[10px] text-slate-500 block">Risk at SL</span>
                <span className="font-mono font-bold text-rose-400">-₹{Number(potentialLoss).toLocaleString('en-IN')}</span>
              </div>
            </div>
          </div>

          {/* Broker API Token */}
          <div className="space-y-1.5">
            <label className="text-xs font-bold text-slate-400 block uppercase tracking-wider">Broker API Access Token</label>
            <input 
              type="password" 
              value={apiToken}
              onChange={(e) => setApiToken(e.target.value)}
              placeholder="Paste broker token (e.g. INDmoney / Dhan / Angel)"
              className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono text-slate-200 focus:ring-2 focus:ring-purple-500 focus:outline-none"
            />
          </div>

          {error && (
            <div className="bg-rose-950/60 border border-rose-800 text-rose-300 p-3 rounded-xl text-xs flex items-center gap-2">
              <ShieldAlert size={16} className="text-rose-400 flex-shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {/* Action Buttons */}
          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 py-2.5 rounded-xl border border-slate-700 font-bold text-xs text-slate-400 hover:bg-slate-800 hover:text-white transition-all"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleExecute}
              disabled={loading}
              className={`flex-1 py-2.5 rounded-xl font-bold text-xs text-white shadow-lg transition-all flex items-center justify-center gap-2 ${
                action === 'BUY' ? 'bg-emerald-600 hover:bg-emerald-500' : 'bg-rose-600 hover:bg-rose-500'
              } ${loading ? 'opacity-50 cursor-not-allowed' : 'active:scale-95'}`}
            >
              {loading ? <Zap className="animate-spin" size={16} /> : <ShieldCheck size={16} />}
              {loading ? 'Routing Order...' : `Execute ${qty} Shares (${action})`}
            </button>
          </div>

        </div>
      </div>
    </div>
  );
}
