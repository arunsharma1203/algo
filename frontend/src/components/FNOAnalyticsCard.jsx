import React, { useState, useEffect } from 'react';
import { Layers, ShieldAlert, ArrowUpRight, ArrowDownRight, Compass, RefreshCw, BarChart2 } from 'lucide-react';
import axios from 'axios';

export default function FNOAnalyticsCard({ symbol = "NIFTY" }) {
  const [fnoData, setFnoData] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchFNO = () => {
    setLoading(true);
    axios.get(`http://localhost:8000/api/fno/option-chain/${symbol}`)
      .then(res => {
        setFnoData(res.data);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchFNO();
  }, [symbol]);

  if (!fnoData && !loading) return null;

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm space-y-3">
      {/* Header */}
      <div className="flex justify-between items-center border-b border-slate-800 pb-2.5">
        <div className="flex items-center gap-2">
          <div className="p-1.5 bg-blue-950/70 border border-blue-500/40 rounded-lg text-blue-400">
            <Layers className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-xs font-bold uppercase tracking-wider text-slate-200 flex items-center gap-2">
              NSE Option Chain & OI Confluence ({fnoData?.symbol || symbol})
            </h3>
            <span className="text-[10px] text-slate-400">
              Underlying Spot: <strong className="text-slate-200 font-mono">₹{fnoData?.underlying_price?.toLocaleString('en-IN') || '...'}</strong>
            </span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <span className={`px-2.5 py-0.5 rounded-full text-[10px] font-black uppercase tracking-wider border ${
            fnoData?.bias === 'BULLISH' 
              ? 'bg-emerald-950/80 text-emerald-300 border-emerald-500/40' 
              : fnoData?.bias === 'BEARISH' 
              ? 'bg-rose-950/80 text-rose-300 border-rose-500/40' 
              : 'bg-slate-800 text-slate-300 border-slate-700'
          }`}>
            {fnoData?.buildup || 'ANALYZING...'}
          </span>
          <button 
            onClick={fetchFNO} 
            disabled={loading}
            className="p-1 hover:bg-slate-800 text-slate-400 hover:text-slate-200 rounded transition-colors"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin text-purple-400' : ''}`} />
          </button>
        </div>
      </div>

      {/* PCR & Max Pain Summary Grid */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 text-center">
        <div className="bg-slate-950/60 p-2 rounded-lg border border-slate-800">
          <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block">Put-Call Ratio (PCR)</span>
          <span className={`text-sm font-black font-mono mt-0.5 block ${
            (fnoData?.pcr || 1) >= 1.15 ? 'text-emerald-400' : (fnoData?.pcr || 1) <= 0.85 ? 'text-rose-400' : 'text-amber-400'
          }`}>
            {fnoData?.pcr || '--'}
          </span>
        </div>

        <div className="bg-slate-950/60 p-2 rounded-lg border border-slate-800">
          <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block">Max Pain Magnet</span>
          <span className="text-sm font-black font-mono text-purple-300 mt-0.5 block">
            ₹{fnoData?.max_pain?.toLocaleString('en-IN') || '--'}
          </span>
        </div>

        <div className="bg-slate-950/60 p-2 rounded-lg border border-slate-800">
          <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block">Major Resistance (Call OI)</span>
          <span className="text-sm font-black font-mono text-rose-400 mt-0.5 block">
            {fnoData?.call_walls?.[0] ? `₹${fnoData.call_walls[0].strike}` : '--'}
          </span>
        </div>

        <div className="bg-slate-950/60 p-2 rounded-lg border border-slate-800">
          <span className="text-[9px] font-bold text-slate-400 uppercase tracking-wider block">Major Support (Put OI)</span>
          <span className="text-sm font-black font-mono text-emerald-400 mt-0.5 block">
            {fnoData?.put_walls?.[0] ? `₹${fnoData.put_walls[0].strike}` : '--'}
          </span>
        </div>
      </div>

      {/* OI Walls Breakdown */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs pt-1">
        {/* Call Walls (Resistance) */}
        <div className="bg-rose-950/20 border border-rose-900/40 rounded-lg p-2.5 space-y-1.5">
          <div className="flex justify-between items-center text-[10px] font-bold text-rose-300 uppercase">
            <span className="flex items-center gap-1"><ArrowUpRight className="w-3 h-3 text-rose-400" /> Call OI Walls (Ceiling)</span>
            <span>Open Interest</span>
          </div>
          <div className="space-y-1 font-mono text-[11px]">
            {fnoData?.call_walls?.map((w, idx) => (
              <div key={idx} className="flex justify-between items-center text-slate-300 bg-slate-900/60 px-2 py-0.5 rounded border border-rose-900/20">
                <span className="font-bold text-rose-300">₹{w.strike} CE</span>
                <span className="text-slate-400">{(w.oi / 100000).toFixed(2)}L OI</span>
              </div>
            ))}
          </div>
        </div>

        {/* Put Walls (Support) */}
        <div className="bg-emerald-950/20 border border-emerald-900/40 rounded-lg p-2.5 space-y-1.5">
          <div className="flex justify-between items-center text-[10px] font-bold text-emerald-300 uppercase">
            <span className="flex items-center gap-1"><ArrowDownRight className="w-3 h-3 text-emerald-400" /> Put OI Walls (Floor)</span>
            <span>Open Interest</span>
          </div>
          <div className="space-y-1 font-mono text-[11px]">
            {fnoData?.put_walls?.map((w, idx) => (
              <div key={idx} className="flex justify-between items-center text-slate-300 bg-slate-900/60 px-2 py-0.5 rounded border border-emerald-900/20">
                <span className="font-bold text-emerald-300">₹{w.strike} PE</span>
                <span className="text-slate-400">{(w.oi / 100000).toFixed(2)}L OI</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
