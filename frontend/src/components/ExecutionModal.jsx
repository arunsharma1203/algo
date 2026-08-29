import React, { useState, useEffect } from 'react';
import { X, ShieldAlert, Zap, Calculator, Flame, ShieldCheck, Lock, Unlock, RefreshCw } from 'lucide-react';
import axios from 'axios';

export default function ExecutionModal({ trade, onClose, onSuccess }) {
  // Load global profile config
  const savedProfileStr = localStorage.getItem('swing_profile');
  const defaultProfile = savedProfileStr ? JSON.parse(savedProfileStr) : {
    apiKey: localStorage.getItem('indmoney_api_token') || '',
    simulationMode: true,
    defaultCapital: 100000
  };

  const [apiToken, setApiToken] = useState(defaultProfile.apiKey || localStorage.getItem('indmoney_api_token') || '');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [sizingMode, setSizingMode] = useState('KELLY'); // 'KELLY' or 'FIXED'
  const [kellyFraction, setKellyFraction] = useState('HALF'); // 'QUARTER', 'HALF', 'FULL'
  const [portfolioHeat, setPortfolioHeat] = useState(null);
  
  // Real-time market quote state
  const [liveQuote, setLiveQuote] = useState(null);
  const [fetchingQuote, setFetchingQuote] = useState(false);

  // Execution environment state
  const isGlobalSim = defaultProfile.simulationMode !== false;
  const [execMode, setExecMode] = useState(isGlobalSim ? 'SIMULATION' : 'LIVE');
  const [safeguardUnlocked, setSafeguardUnlocked] = useState(!isGlobalSim);

  const capital = Number(defaultProfile.defaultCapital) || 100000;
  const maxRisk = Number(defaultProfile.maxRiskPerTrade) || 2.0;

  // 1. Fetch live portfolio heat
  useEffect(() => {
    axios.get(`http://localhost:8000/api/broker/portfolio-heat?capital=${capital}`)
      .then(res => setPortfolioHeat(res.data))
      .catch(() => {});
  }, [capital]);

  // 2. Fetch live real-time quote from Upstox / market provider
  const fetchRealtimeQuote = () => {
    setFetchingQuote(true);
    axios.get(`http://localhost:8000/api/broker/live-quote?ticker=${trade.ticker}`)
      .then(res => setLiveQuote(res.data))
      .catch(e => console.warn("Live quote fetch error:", e))
      .finally(() => setFetchingQuote(false));
  };

  useEffect(() => {
    fetchRealtimeQuote();
  }, [trade.ticker]);

  // 3. Compute Position Sizing
  const currentPrice = (liveQuote && liveQuote.price) ? liveQuote.price : trade.entry;
  const riskPerShare = Math.abs(currentPrice - trade.sl);
  const rewardPerShare = Math.abs(trade.tp1 - currentPrice);
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
  const maxQtyByCapital = Math.floor(capital / currentPrice);
  qty = Math.max(1, Math.min(qty, maxQtyByCapital));

  const potentialLoss = (riskPerShare * qty).toFixed(2);
  const potentialGain = (rewardPerShare * qty).toFixed(2);
  const totalCommitment = (currentPrice * qty).toFixed(2);
  const slippage = liveQuote && liveQuote.price ? (liveQuote.price - trade.entry).toFixed(2) : 0;

  const handleExecute = async () => {
    const isLive = execMode === 'LIVE';

    if (isLive) {
      if (!safeguardUnlocked) {
        setError("Safeguard Lock Active: Please confirm authorization checkbox before routing real money.");
        return;
      }
      if (!apiToken || apiToken.length < 8) {
        setError("Please enter a valid Broker API Access Token for live execution.");
        return;
      }
    }
    
    if (apiToken) {
      localStorage.setItem('indmoney_api_token', apiToken);
    }
    
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
        simulation: !isLive,
        bypass_safeguard: safeguardUnlocked
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
            1-Click Order Execution: {trade.ticker}
          </h2>
          <button onClick={onClose} className="text-white/80 hover:text-white p-1 rounded-lg hover:bg-black/20">
            <X size={20} />
          </button>
        </div>
        
        {/* Environment & Live Feed Status Bar */}
        <div className={`px-4 py-2.5 flex items-center justify-between border-b ${
          execMode === 'SIMULATION' ? 'bg-indigo-950/60 border-indigo-800/50 text-indigo-300' : 'bg-amber-950/60 border-amber-800/50 text-amber-300'
        }`}>
          <div className="flex items-center gap-2 text-xs font-semibold">
            {execMode === 'SIMULATION' ? <ShieldCheck size={16} className="text-emerald-400" /> : <ShieldAlert size={16} className="text-amber-400" />}
            <span>{execMode === 'SIMULATION' ? '🛡️ Paper Trading (Simulation - 0 Risk)' : '🚨 Live Real-Money Order Routing'}</span>
          </div>

          <div className="flex items-center gap-2">
            {liveQuote && (
              <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full flex items-center gap-1 ${liveQuote.is_realtime ? 'bg-emerald-950 text-emerald-300 border border-emerald-700' : 'bg-slate-800 text-slate-300'}`}>
                <span className={`w-1.5 h-1.5 rounded-full ${liveQuote.is_realtime ? 'bg-emerald-400 animate-pulse' : 'bg-gray-400'}`}></span>
                {liveQuote.source === 'upstox' ? 'UPSTOX LIVE' : 'YAHOO'}
              </span>
            )}
            {portfolioHeat && (
              <div className="flex items-center gap-1 text-[11px] font-mono">
                <Flame size={13} className={portfolioHeat.status === 'MAX_REACHED' ? 'text-rose-400' : 'text-amber-400'} />
                <span>{portfolioHeat.current_heat_pct}% / {portfolioHeat.max_heat_cap_pct}%</span>
              </div>
            )}
          </div>
        </div>

        <div className="p-5 space-y-4 overflow-y-auto max-h-[75vh]">
          
          {/* Live Price Verification Banner */}
          <div className="bg-slate-950/80 p-3 rounded-xl border border-slate-800 flex items-center justify-between">
            <div>
              <div className="text-[10px] uppercase font-bold text-slate-400 flex items-center gap-1">
                <span>Live Price Verification</span>
                {fetchingQuote && <RefreshCw size={10} className="animate-spin text-purple-400" />}
              </div>
              <div className="flex items-baseline gap-2 mt-0.5">
                <span className="text-base font-black font-mono text-white">
                  ₹{liveQuote && liveQuote.price ? liveQuote.price : trade.entry}
                </span>
                {liveQuote && (
                  <span className="text-[10px] text-slate-400">
                    via {liveQuote.source_name}
                  </span>
                )}
              </div>
            </div>

            <div className="text-right">
              <span className="text-[10px] text-slate-400 block font-semibold">Signal Entry vs Live</span>
              <span className={`text-xs font-mono font-bold ${Number(slippage) > 0 ? 'text-amber-400' : Number(slippage) < 0 ? 'text-emerald-400' : 'text-slate-300'}`}>
                {Number(slippage) > 0 ? `+₹${slippage}` : Number(slippage) < 0 ? `-₹${Math.abs(slippage)}` : 'Exact (0.00)'}
              </span>
            </div>
          </div>

          {/* Trade Parameters Grid */}
          <div className="grid grid-cols-3 gap-2.5">
            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800 text-center">
              <span className="text-[10px] text-slate-400 block font-bold uppercase tracking-wider">Exec Price</span>
              <span className="text-sm font-black font-mono text-slate-200">₹{currentPrice}</span>
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

          {/* Execution Environment Switcher */}
          <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
            <label className="text-xs font-bold text-slate-400 block mb-2 uppercase tracking-wider">Execution Environment</label>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setExecMode('SIMULATION')}
                className={`py-2 px-3 rounded-lg text-xs font-bold transition flex items-center justify-center gap-1.5 ${
                  execMode === 'SIMULATION' 
                    ? 'bg-emerald-900/80 text-emerald-200 border border-emerald-600 shadow' 
                    : 'bg-slate-900 text-slate-400 hover:text-slate-200 border border-slate-800'
                }`}
              >
                <ShieldCheck size={14} /> Paper Trading (Safe)
              </button>
              <button
                type="button"
                onClick={() => setExecMode('LIVE')}
                className={`py-2 px-3 rounded-lg text-xs font-bold transition flex items-center justify-center gap-1.5 ${
                  execMode === 'LIVE' 
                    ? 'bg-amber-900/80 text-amber-200 border border-amber-600 shadow' 
                    : 'bg-slate-900 text-slate-400 hover:text-slate-200 border border-slate-800'
                }`}
              >
                <ShieldAlert size={14} /> Real Money (Live)
              </button>
            </div>
          </div>

          {/* Real Money Safeguard Lock Section (Only shown when Live Mode selected) */}
          {execMode === 'LIVE' && (
            <div className="bg-amber-950/40 border border-amber-700/60 rounded-xl p-3.5 space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  {safeguardUnlocked ? <Unlock size={16} className="text-amber-400" /> : <Lock size={16} className="text-rose-400" />}
                  <span className="text-xs font-bold text-amber-200">Execution Safeguard Lock</span>
                </div>
                <span className={`text-[10px] font-bold px-2 py-0.5 rounded ${safeguardUnlocked ? 'bg-amber-500/20 text-amber-300' : 'bg-rose-500/20 text-rose-300'}`}>
                  {safeguardUnlocked ? 'UNLOCKED' : 'LOCKED'}
                </span>
              </div>

              <label className="flex items-start gap-2 cursor-pointer bg-slate-900/80 p-2.5 rounded-lg border border-amber-800/40 hover:bg-slate-900 transition">
                <input 
                  type="checkbox"
                  checked={safeguardUnlocked}
                  onChange={(e) => setSafeguardUnlocked(e.target.checked)}
                  className="mt-0.5 text-amber-500 rounded focus:ring-amber-400"
                />
                <span className="text-[11px] text-amber-200/90 leading-tight">
                  I understand this order will commit <strong>₹{Number(totalCommitment).toLocaleString('en-IN')}</strong> of real capital directly to my broker.
                </span>
              </label>

              {/* Broker API Token input for live routing */}
              <div className="space-y-1">
                <label className="text-[10px] font-bold text-slate-400 block uppercase tracking-wider">Broker API Token (INDmoney / Upstox / Dhan)</label>
                <input 
                  type="password" 
                  value={apiToken}
                  onChange={(e) => setApiToken(e.target.value)}
                  placeholder="Paste broker token..."
                  className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono text-slate-200 focus:ring-2 focus:ring-amber-500 focus:outline-none"
                />
              </div>
            </div>
          )}

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
              disabled={loading || (execMode === 'LIVE' && !safeguardUnlocked)}
              className={`flex-1 py-2.5 rounded-xl font-bold text-xs text-white shadow-lg transition-all flex items-center justify-center gap-2 ${
                execMode === 'SIMULATION'
                  ? 'bg-emerald-600 hover:bg-emerald-500 active:scale-95'
                  : safeguardUnlocked
                    ? 'bg-amber-600 hover:bg-amber-500 active:scale-95'
                    : 'bg-slate-700 opacity-60 cursor-not-allowed'
              } ${loading ? 'opacity-50 cursor-not-allowed' : ''}`}
            >
              {loading ? <Zap className="animate-spin" size={16} /> : execMode === 'SIMULATION' ? <ShieldCheck size={16} /> : <ShieldAlert size={16} />}
              {loading 
                ? 'Routing Order...' 
                : execMode === 'SIMULATION'
                  ? `Simulate Paper Order (${qty} Shares)`
                  : safeguardUnlocked
                    ? `Execute LIVE REAL Order (₹${Number(totalCommitment).toLocaleString('en-IN')})`
                    : 'Safeguard Locked (Check Box Above)'
              }
            </button>
          </div>

        </div>
      </div>
    </div>
  );
}

