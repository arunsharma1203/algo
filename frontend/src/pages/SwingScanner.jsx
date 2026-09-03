import React, { useState, useEffect, useRef } from 'react';
import { Terminal, Play, Loader, TrendingUp, AlertTriangle, Crosshair, Target, CheckCircle, Shield } from 'lucide-react';
import AITradeHistory from '../components/AITradeHistory';
import MLBacktestModal from '../components/MLBacktestModal';
import ExecutionModal from '../components/ExecutionModal';
import FNOAnalyticsCard from '../components/FNOAnalyticsCard';
import { API_BASE } from '../services/api';

export default function SwingScanner() {
  const [logs, setLogs] = useState(() => {
    const saved = localStorage.getItem('last_swing_logs');
    return saved ? JSON.parse(saved) : [];
  });
  
  const [scanning, setScanning] = useState(false);
  const [progress, setProgress] = useState(() => {
    const saved = localStorage.getItem('last_swing_progress');
    return saved ? JSON.parse(saved) : 0;
  });
  
  const [result, setResult] = useState(() => {
    const saved = localStorage.getItem('last_swing_result');
    return saved ? JSON.parse(saved) : null;
  });

  const [execModalOpen, setExecModalOpen] = useState(false);
  const [backtestModalOpen, setBacktestModalOpen] = useState(false);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [execTradeData, setExecTradeData] = useState(null);
  const [selectedUniverse, setSelectedUniverse] = useState('NIFTY_500');

  const terminalContainerRef = useRef(null);

  useEffect(() => {
    if (terminalContainerRef.current) {
      terminalContainerRef.current.scrollTop = terminalContainerRef.current.scrollHeight;
    }
    
    // Save to local storage on change
    localStorage.setItem('last_swing_logs', JSON.stringify(logs));
    localStorage.setItem('last_swing_progress', JSON.stringify(progress));
    if (result) {
      localStorage.setItem('last_swing_result', JSON.stringify(result));
    }
  }, [logs, progress, result]);

  const startScan = async () => {
    setScanning(true);
    setLogs([{ type: 'system', message: 'Initiating Swing Trade ML Sweep (1D Candles, 5-Year History)...' }]);
    setProgress(0);
    setResult(null);

    try {
      let url = `${API_BASE}/ml/swing-scan?universe=${selectedUniverse}`;
      if (selectedUniverse === 'WATCHLIST') {
        const savedWatchlist = localStorage.getItem('watchlist');
        const customTickers = savedWatchlist ? JSON.parse(savedWatchlist).join(',') : '';
        if (customTickers) {
          url += `&custom_tickers=${customTickers}`;
        }
      }
      const response = await fetch(url);
      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      
      let buffer = '';
      
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); 
        
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const data = JSON.parse(line);
            
            if (data.type === 'result') {
              setResult(data.data);
              setRefreshTrigger(prev => prev + 1);
            }
            if (data.progress !== undefined) {
              setProgress(data.progress);
            }
            
            if (data.message) {
              setLogs(prev => [...prev, data]);
            }
          } catch (e) {
            console.error("Failed parsing SSE:", line);
          }
        }
      }
    } catch (err) {
      setLogs(prev => [...prev, { type: 'error', message: 'Connection to ML Swing Engine failed.' }]);
    } finally {
      setScanning(false);
    }
  };

  const handleExecuteClick = (tradeParams) => {
    setExecTradeData(tradeParams);
    setExecModalOpen(true);
  };

  return (
    <div className="max-w-6xl mx-auto pb-10 max-w-full">
      {execModalOpen && execTradeData && (
        <ExecutionModal 
          trade={execTradeData}
          onClose={() => setExecModalOpen(false)}
          onSuccess={(data) => {
             setExecModalOpen(false);
             alert(data.message || "Order Executed Successfully");
          }}
        />
      )}

      <div className="mb-6 sm:mb-8 flex flex-col sm:flex-row justify-between items-start sm:items-end gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-black text-gray-900 tracking-tight flex items-center">
            <TrendingUp className="text-purple-600 mr-2 sm:mr-3 shrink-0" size={28} />
            Swing Trade ML Scanner
          </h1>
          <p className="text-gray-500 mt-2 font-medium max-w-2xl text-xs sm:text-sm">
            Multi-day algorithmic sweeping. The AI trains on 5 years of daily candles to predict 3%+ momentum breakouts over the next 5-10 trading days.
          </p>
        </div>
        
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 w-full sm:w-auto">
          <select
            value={selectedUniverse}
            onChange={(e) => setSelectedUniverse(e.target.value)}
            disabled={scanning}
            className="bg-gray-900 border border-gray-700 text-gray-200 text-xs font-bold rounded-lg px-3 py-3 focus:outline-none focus:border-purple-500 shadow-sm"
          >
            <option value="NIFTY_500">NIFTY 500 (500 Stocks - Broad Market)</option>
            <option value="NIFTY_50">NIFTY 50 (50 Benchmark Stocks)</option>
            <option value="WATCHLIST">My Watchlist Only</option>
          </select>
          <button 
            onClick={startScan}
            disabled={scanning}
            className={`flex items-center justify-center font-bold py-3 px-6 sm:px-8 rounded-lg shadow-md transition transform hover:scale-105 shrink-0 w-full sm:w-auto text-sm ${scanning ? 'bg-gray-400 cursor-not-allowed' : 'bg-purple-600 hover:bg-purple-700 text-white'}`}
          >
            {scanning ? (
              <><Loader className="animate-spin mr-2" size={18} /> Sweeping Market...</>
            ) : (
              <><Play className="mr-2" size={18} /> Initiate AI Scan</>
            )}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 sm:gap-8">
        <div className="lg:col-span-2 space-y-6">
          <div className="bg-gray-900 rounded-xl overflow-hidden shadow-2xl border border-gray-800">
            <div className="bg-gray-950 px-4 py-2 border-b border-gray-800 flex justify-between items-center">
              <div className="flex items-center">
                <Terminal size={14} className="text-gray-500 mr-2 shrink-0" />
                <span className="text-xs text-gray-500 font-mono">ensemble_swing_node_01</span>
              </div>
              <div className="flex space-x-1.5">
                <div className="w-2.5 h-2.5 rounded-full bg-red-500 opacity-50"></div>
                <div className="w-2.5 h-2.5 rounded-full bg-yellow-500 opacity-50"></div>
                <div className="w-2.5 h-2.5 rounded-full bg-green-500 opacity-50"></div>
              </div>
            </div>
            
            <div 
              ref={terminalContainerRef}
              className="p-4 sm:p-5 h-80 sm:h-96 overflow-y-auto font-mono text-xs sm:text-sm flex flex-col space-y-1 break-all"
            >
              {logs.length === 0 && (
                <div className="text-gray-600 text-center mt-20">
                  System Ready. Waiting for execution command...
                </div>
              )}
              {logs.map((log, i) => (
                <div key={i} className={
                  log.type === 'error' ? 'text-red-400' : 
                  log.type === 'system' ? 'text-blue-400 font-bold' : 'text-green-400'
                }>
                  <span className="text-gray-600 mr-2">[{new Date().toLocaleTimeString()}]</span>
                  {log.message}
                </div>
              ))}
            </div>
            
            <div className="bg-gray-950 px-4 py-3 border-t border-gray-800">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs text-gray-500 font-mono uppercase tracking-wider">Sweep Progress</span>
                <span className="text-xs text-purple-400 font-mono font-bold">{progress}%</span>
              </div>
              <div className="w-full bg-gray-800 rounded-full h-1.5">
                <div className="bg-purple-500 h-1.5 rounded-full transition-all duration-300 ease-out" style={{ width: `${progress}%` }}></div>
              </div>
            </div>
          </div>
        </div>

        <div>
          {result ? (
            <div className="bg-white rounded-xl shadow-md border border-gray-200 overflow-hidden sticky top-8">
              {/* Compact Header */}
              <div className="px-5 py-4 text-white bg-gradient-to-r from-purple-700 via-indigo-700 to-indigo-800">
                <div className="flex justify-between items-center">
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="bg-white/20 px-2 py-0.5 rounded text-[10px] font-black tracking-wider uppercase">
                        LONG / CASH EQUITY (5-DAY HORIZON)
                      </span>
                      <h2 className="text-2xl font-black tracking-tight">{result.ticker}</h2>
                    </div>
                    <div className="mt-1 flex items-baseline space-x-2">
                      <span className="text-2xl font-black">{result.entry != null ? `₹${Number(result.entry).toFixed(2)}` : '-'}</span>
                      <span className="text-xs text-purple-200 font-medium">
                        {result.price_is_fresh ? '● Live LTP' : 'Reference Entry'}
                      </span>
                      {result.model_candle_close != null && (
                        <span className="text-[10px] text-purple-300 font-mono">
                          (Candle: ₹{Number(result.model_candle_close).toFixed(2)})
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="bg-white/10 backdrop-blur-sm border border-white/20 px-3 py-1.5 rounded-xl text-right shadow-xs">
                    <div className="font-black text-xl leading-none">{result.score != null ? Number(result.score).toFixed(1) : '-'}%</div>
                    <span className="block text-[9px] font-bold text-purple-200 uppercase tracking-wider mt-0.5">
                      {result.calibration ? 'CALIBRATED WIN RATE' : 'CONVICTION'}
                    </span>
                    {result.raw_score !== undefined && result.raw_score !== result.score && (
                      <span className="block text-[8px] text-purple-300 font-mono">
                        Raw: {Number(result.raw_score).toFixed(1)}%
                      </span>
                    )}
                  </div>
                </div>
              </div>

              {/* Compact 3-Column Targets & SL */}
              <div className="grid grid-cols-3 gap-2 p-3 bg-slate-50 border-b border-slate-100">
                <div className="bg-white p-2 rounded-lg border border-red-100 text-center shadow-xs">
                  <span className="text-[10px] font-bold text-red-600 uppercase block">Stop Loss</span>
                  <span className="font-mono font-bold text-xs text-red-700">{result.sl != null ? `₹${Number(result.sl).toFixed(2)}` : '-'}</span>
                </div>
                <div className="bg-white p-2 rounded-lg border border-green-100 text-center shadow-xs">
                  <span className="text-[10px] font-bold text-green-600 uppercase block">Target 1 (1:1.5)</span>
                  <span className="font-mono font-bold text-xs text-green-700">{result.tp1 != null ? `₹${Number(result.tp1).toFixed(2)}` : '-'}</span>
                </div>
                <div className="bg-white p-2 rounded-lg border border-emerald-100 text-center shadow-xs">
                  <span className="text-[10px] font-bold text-emerald-600 uppercase block">Target 2 (1:3.0)</span>
                  <span className="font-mono font-bold text-xs text-emerald-700">{result.tp2 != null ? `₹${Number(result.tp2).toFixed(2)}` : '-'}</span>
                </div>
              </div>

              {/* Compact Multi-Factor Telemetry & Sizing Bar */}
              <div className="p-3 bg-slate-900 text-white space-y-2">
                <div className="flex items-center justify-between text-[11px] pb-1.5 border-b border-slate-800">
                  <div className="flex items-center space-x-1.5">
                    <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
                    <span className="font-bold text-emerald-400 text-[10px] uppercase">Layer-2 Signals</span>
                  </div>
                  {result.nlp_sentiment !== undefined && (
                    <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${result.nlp_sentiment > 0 ? 'bg-emerald-950 text-emerald-300' : result.nlp_sentiment < 0 ? 'bg-rose-950 text-rose-300' : 'bg-slate-800 text-slate-300'}`}>
                      News: {result.nlp_sentiment > 0 ? '+' : ''}{result.nlp_sentiment}
                    </span>
                  )}
                </div>

                <div className="grid grid-cols-3 gap-2 text-center text-[10px]">
                  <div className="bg-slate-800/80 px-2 py-1 rounded border border-slate-700/60">
                    <span className="text-slate-400 block text-[9px]">Volume Surge</span>
                    <span className="font-bold text-emerald-300">{(result.telemetry?.volume_ratio || result.volume_ratio || 1.0).toFixed(1)}x SMA</span>
                  </div>
                  <div className="bg-slate-800/80 px-2 py-1 rounded border border-slate-700/60">
                    <span className="text-slate-400 block text-[9px]">ATR Range</span>
                    <span className="font-bold text-indigo-300">{(result.telemetry?.atr_pct || result.atr_pct || 2.0).toFixed(1)}%</span>
                  </div>
                  <div className="bg-slate-800/80 px-2 py-1 rounded border border-slate-700/60">
                    <span className="text-slate-400 block text-[9px]">Macro Bias</span>
                    <span className={`font-bold ${result.telemetry?.macro_aligned !== false ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {result.telemetry?.macro_aligned !== false ? 'Aligned' : 'Headwind'}
                    </span>
                  </div>
                </div>

                {/* Platt Calibration Bar */}
                {result.calibration && (
                  <div className="pt-1.5 border-t border-slate-800">
                    <div className="flex justify-between text-[10px] text-slate-400 mb-1">
                      <span>Platt Calibration:</span>
                      <span className="font-mono text-purple-300 font-bold">{result.raw_score?.toFixed(1)}% &rarr; {result.score.toFixed(1)}%</span>
                    </div>
                    <div className="w-full bg-slate-800 rounded-full h-1 overflow-hidden">
                      <div className="bg-gradient-to-r from-indigo-400 to-purple-400 h-1 rounded-full" style={{ width: `${Math.min(100, Math.max(0, result.score))}%` }} />
                    </div>
                  </div>
                )}
              </div>

              {/* Dynamic Position Sizing Strip */}
              {(() => {
                const savedProfileStr = localStorage.getItem('swing_profile');
                const defaultProfile = savedProfileStr ? JSON.parse(savedProfileStr) : { defaultCapital: 100000, maxRiskPerTrade: 2.0 };
                const capital = Number(defaultProfile.defaultCapital) || 100000;
                const maxRisk = Number(defaultProfile.maxRiskPerTrade) || 2.0;
                
                const riskAmount = capital * (maxRisk / 100);
                const entryVal = Number(result.entry) || 0;
                const slVal = Number(result.sl) || 0;
                const riskPerShare = Math.abs(entryVal - slVal);
                let qty = 0;
                if (riskPerShare > 0 && entryVal > 0) {
                  qty = Math.floor(riskAmount / riskPerShare);
                  const maxQtyByCapital = Math.floor(capital / entryVal);
                  qty = Math.min(qty, maxQtyByCapital);
                }
                const capitalRequired = qty * entryVal;

                return (
                  <div className="px-4 py-2.5 bg-indigo-50/70 border-b border-indigo-100 flex items-center justify-between text-xs">
                    <div className="text-indigo-900">
                      <span className="font-bold">Position Sizing: </span>
                      <span className="font-black text-indigo-700">{qty} Shares</span>
                      <span className="text-[11px] text-indigo-600 ml-1.5">(₹{capitalRequired.toLocaleString('en-IN', { maximumFractionDigits: 0 })})</span>
                    </div>
                    <span className="text-[10px] font-bold text-indigo-700 bg-indigo-100 px-2 py-0.5 rounded">
                      {maxRisk}% Risk (₹{riskAmount.toFixed(0)})
                    </span>
                  </div>
                );
              })()}

              {/* Actions Footer */}
              <div className="p-3 bg-white">
                <div className="flex gap-2 w-full">
                  <button 
                    onClick={() => handleExecuteClick({
                      ticker: result.ticker,
                      type: result.is_bullish !== false ? 'BUY' : 'SELL',
                      entry: result.entry,
                      sl: result.sl,
                      tp1: result.tp1,
                      tp2: result.tp2
                    })}
                    className={`flex-1 text-white font-bold py-2.5 px-3 rounded-lg shadow-sm flex items-center justify-center text-xs transition cursor-pointer ${result.is_bullish !== false ? 'bg-gray-900 hover:bg-black' : 'bg-red-700 hover:bg-red-800'}`}
                  >
                    <Shield size={14} className="mr-1.5 text-purple-400" />
                    1-Click Execute
                  </button>
                  
                  <button 
                    onClick={() => setBacktestModalOpen(true)}
                    className="px-3 py-2.5 bg-purple-50 text-purple-700 hover:bg-purple-100 font-bold rounded-lg text-xs transition border border-purple-200 cursor-pointer"
                  >
                    Verify Backtest
                  </button>
                </div>
              </div>
            </div>
          ) : (
             <div className="bg-gray-50 border-2 border-dashed border-gray-200 rounded-xl h-full flex flex-col items-center justify-center p-10 text-center min-h-[400px]">
               <div className="bg-purple-100 p-4 rounded-full mb-4">
                 <TrendingUp className="text-purple-600" size={32} />
               </div>
               <h3 className="text-gray-800 font-bold mb-2">Awaiting Swing Setup</h3>
               <p className="text-gray-500 text-sm">
                 Run the scanner to discover high-probability swing trades for the upcoming week.
               </p>
             </div>
          )}
        </div>
      </div>
      <FNOAnalyticsCard symbol={result?.ticker || "NIFTY"} />
      <AITradeHistory tradeType="SWING" refreshTrigger={refreshTrigger} />
      <MLBacktestModal 
        isOpen={backtestModalOpen} 
        onClose={() => setBacktestModalOpen(false)} 
        ticker={result?.ticker}
        defaultType="SWING"
      />

    </div>
  );
}
