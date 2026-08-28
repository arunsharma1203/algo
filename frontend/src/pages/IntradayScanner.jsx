import React, { useState, useEffect, useRef } from 'react';
import { Terminal, Play, Loader, TrendingUp, AlertTriangle, Crosshair, Target, CheckCircle, Shield } from 'lucide-react';
import AITradeHistory from '../components/AITradeHistory';
import MLBacktestModal from '../components/MLBacktestModal';
import ExecutionModal from '../components/ExecutionModal';

export default function IntradayScanner() {
  const [logs, setLogs] = useState(() => {
    const saved = localStorage.getItem('last_intraday_logs');
    return saved ? JSON.parse(saved) : [];
  });
  
  const [scanning, setScanning] = useState(false);
  const [progress, setProgress] = useState(() => {
    const saved = localStorage.getItem('last_intraday_progress');
    return saved ? JSON.parse(saved) : 0;
  });
  
  const [result, setResult] = useState(() => {
    const saved = localStorage.getItem('last_intraday_result');
    return saved ? JSON.parse(saved) : null;
  });

  const [execModalOpen, setExecModalOpen] = useState(false);
  const [backtestModalOpen, setBacktestModalOpen] = useState(false);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [execTradeData, setExecTradeData] = useState(null);

  const terminalContainerRef = useRef(null);

  useEffect(() => {
    if (terminalContainerRef.current) {
      terminalContainerRef.current.scrollTop = terminalContainerRef.current.scrollHeight;
    }
    
    // Save to local storage on change
    localStorage.setItem('last_intraday_logs', JSON.stringify(logs));
    localStorage.setItem('last_intraday_progress', JSON.stringify(progress));
    if (result) {
      localStorage.setItem('last_intraday_result', JSON.stringify(result));
    }
  }, [logs, progress, result]);

  const startScan = async () => {
    setScanning(true);
    setLogs([{ type: 'system', message: 'Initiating Intraday ML Sweep (15m Candles, 60-Day Deep History)...' }]);
    setProgress(0);
    setResult(null);

    try {
      const savedWatchlist = localStorage.getItem('watchlist');
      const customTickers = savedWatchlist ? JSON.parse(savedWatchlist).join(',') : '';
      const url = customTickers ? `http://localhost:8000/api/ml/intraday-scan?custom_tickers=${customTickers}` : 'http://localhost:8000/api/ml/intraday-scan';
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
    <div className="max-w-6xl mx-auto pb-10">
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

      <div className="mb-8 flex justify-between items-end">
        <div>
          <h1 className="text-3xl font-black text-gray-900 tracking-tight flex items-center">
            <TrendingUp className="text-purple-600 mr-3" size={32} />
            Intraday Trade ML Scanner
          </h1>
          <p className="text-gray-500 mt-2 font-medium max-w-2xl">
            Intraday algorithmic sweeping. The AI predicts momentum breakouts for the current trading session.
          </p>
        </div>
        
        <button 
          onClick={startScan}
          disabled={scanning}
          className={`flex items-center font-bold py-3 px-8 rounded-lg shadow-md transition transform hover:scale-105 ${scanning ? 'bg-gray-400 cursor-not-allowed' : 'bg-purple-600 hover:bg-purple-700 text-white'}`}
        >
          {scanning ? (
            <><Loader className="animate-spin mr-2" size={20} /> Sweeping Market...</>
          ) : (
            <><Play className="mr-2" size={20} /> Initiate AI Scan</>
          )}
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <div className="lg:col-span-2 space-y-6">
          <div className="bg-gray-900 rounded-xl overflow-hidden shadow-2xl border border-gray-800">
            <div className="bg-gray-950 px-4 py-2 border-b border-gray-800 flex justify-between items-center">
              <div className="flex items-center">
                <Terminal size={14} className="text-gray-500 mr-2" />
                <span className="text-xs text-gray-500 font-mono">ensemble_intraday_node_01</span>
              </div>
              <div className="flex space-x-1.5">
                <div className="w-2.5 h-2.5 rounded-full bg-red-500 opacity-50"></div>
                <div className="w-2.5 h-2.5 rounded-full bg-yellow-500 opacity-50"></div>
                <div className="w-2.5 h-2.5 rounded-full bg-green-500 opacity-50"></div>
              </div>
            </div>
            
            <div 
              ref={terminalContainerRef}
              className="p-5 h-96 overflow-y-auto font-mono text-sm flex flex-col space-y-1"
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
            <div className="bg-white rounded-xl shadow-lg border border-purple-100 overflow-hidden sticky top-8">
              <div className="bg-gradient-to-r from-purple-600 to-indigo-700 p-6 text-white">
                <div className="flex justify-between items-start">
                  <div>
                    <span className="bg-white/20 px-2 py-1 rounded text-xs font-bold tracking-wider mb-2 inline-block">INTRADAY BUY</span>
                    <h2 className="text-3xl font-black">{result.ticker}</h2>
                  </div>
                  <div className="bg-white text-purple-700 font-black text-xl px-3 py-2 rounded-lg shadow-inner">
                    {result.score.toFixed(1)}
                    <span className="block text-[10px] text-purple-400 text-center mt-0.5">SCORE</span>
                  </div>
                </div>
                <div className="mt-6 flex items-end">
                  <span className="text-4xl font-light">₹{result.entry.toFixed(2)}</span>
                  <span className="ml-2 text-purple-200 mb-1">Entry Price</span>
                </div>
              </div>
              
              <div className="p-6 space-y-4 bg-gray-50">
                <div className="flex items-center justify-between p-3 bg-white rounded-lg border border-red-100 shadow-sm">
                  <div className="flex items-center text-red-600">
                    <AlertTriangle size={18} className="mr-2" />
                    <span className="font-bold text-sm">Intraday Stop Loss</span>
                  </div>
                  <span className="font-mono font-bold text-red-700">₹{result.sl?.toFixed(2) || '-'}</span>
                </div>
                
                <div className="flex items-center justify-between p-3 bg-white rounded-lg border border-green-100 shadow-sm">
                  <div className="flex items-center text-green-600">
                    <Crosshair size={18} className="mr-2" />
                    <span className="font-bold text-sm">Target 1 (1:1.5)</span>
                  </div>
                  <span className="font-mono font-bold text-green-700">₹{result.tp1?.toFixed(2) || '-'}</span>
                </div>
                
                <div className="flex items-center justify-between p-3 bg-white rounded-lg border border-emerald-100 shadow-sm">
                  <div className="flex items-center text-emerald-600">
                    <Target size={18} className="mr-2" />
                    <span className="font-bold text-sm">Target 2 (1:3.0)</span>
                  </div>
                  <span className="font-mono font-bold text-emerald-700">₹{result.tp2?.toFixed(2) || '-'}</span>
                </div>
              </div>
              
              {result.nlp_sentiment !== undefined && (
                <div className={`p-4 mx-6 mb-4 rounded-lg border ${result.nlp_sentiment < -20 ? 'bg-red-50 border-red-200' : result.nlp_sentiment > 20 ? 'bg-green-50 border-green-200' : 'bg-gray-50 border-gray-200'}`}>
                  <div className="flex justify-between items-center mb-2">
                    <span className="text-xs font-bold uppercase tracking-wider text-gray-500">NLP News Sentiment</span>
                    <span className={`font-bold ${result.nlp_sentiment < -20 ? 'text-red-600' : result.nlp_sentiment > 20 ? 'text-green-600' : 'text-gray-600'}`}>
                      {result.nlp_sentiment > 0 ? '+' : ''}{result.nlp_sentiment} Score
                    </span>
                  </div>
                  <p className="text-sm text-gray-700 italic border-l-2 border-gray-300 pl-3">"{result.nlp_headline}"</p>
                </div>
              )}
              
              <div className="p-6 bg-white border-t border-gray-100">
               <div className="flex gap-4 w-full">
                <button 
                   onClick={() => handleExecuteClick({
                     ticker: result.ticker,
                     type: 'BUY',
                     entry: result.entry,
                     sl: result.sl,
                     tp1: result.tp1,
                     tp2: result.tp2
                   })}
                   className="w-full bg-gray-900 hover:bg-black text-white font-bold py-4 rounded-lg shadow-lg flex items-center justify-center transition transform hover:scale-105"
                >
                  <Shield size={18} className="mr-2 text-purple-400" />
                  1-Click Execution Mode
                </button>
                
                <button 
                  onClick={() => setBacktestModalOpen(true)}
                  className="px-6 bg-purple-100 text-purple-700 hover:bg-purple-200 font-bold rounded-lg shadow-sm flex items-center justify-center transition border border-purple-200"
                >
                  Verify History
                </button>
              </div>
                <p className="text-center text-xs text-gray-400 mt-3 font-medium">
                  Intraday setup evaluated on 15m interval.
                </p>
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
      <AITradeHistory tradeType="INTRADAY" refreshTrigger={refreshTrigger} />
      <MLBacktestModal 
        isOpen={backtestModalOpen} 
        onClose={() => setBacktestModalOpen(false)} 
        ticker={result?.ticker}
        defaultType="INTRADAY"
      />

    </div>
  );
}
