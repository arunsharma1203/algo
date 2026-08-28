import React, { useState, useEffect } from 'react';
import { runBacktest } from '../services/api';
import TickerSearch from '../components/TickerSearch';
import { useLiveIndicator } from '../context/LiveIndicatorContext';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import { PlayCircle, Settings, Check } from 'lucide-react';

export default function Optimizer() {
  const [strategies, setStrategies] = useState([]);
  const [selectedStrategy, setSelectedStrategy] = useState(null);
  
  const [ticker, setTicker] = useState('RELIANCE.NS');
  const today = new Date();
  const lastYear = new Date();
  lastYear.setFullYear(today.getFullYear() - 1);
  const [startDate, setStartDate] = useState(lastYear.toISOString().split('T')[0]);
  const [endDate, setEndDate] = useState(today.toISOString().split('T')[0]);
  
  const [paramToOptimize, setParamToOptimize] = useState(''); // e.g. "entry.0.left.period"
  const [paramMin, setParamMin] = useState(10);
  const [paramMax, setParamMax] = useState(50);
  const [paramStep, setParamStep] = useState(10);
  
  const [isOptimizing, setIsOptimizing] = useState(false);
  const [progress, setProgress] = useState(0);
  const [results, setResults] = useState([]);
  const [bestResult, setBestResult] = useState(null);
  const [error, setError] = useState(null);
  
  const { triggerFetchIndicator } = useLiveIndicator();

  useEffect(() => {
    const saved = localStorage.getItem('saved_strategies');
    if (saved) {
      setStrategies(JSON.parse(saved));
    }
  }, []);

  const getParamOptions = (strategy) => {
    if (!strategy) return [];
    let options = [];
    
    // Scan entry conditions
    strategy.entry.conditions.forEach((c, idx) => {
      if (c.left?.params?.period) options.push({ label: `Entry Cond ${idx+1}: ${c.left.name} Period`, path: `entry.conditions.${idx}.left.params.period`, val: c.left.params.period });
      if (c.right?.params?.period) options.push({ label: `Entry Cond ${idx+1}: ${c.right.name} Period`, path: `entry.conditions.${idx}.right.params.period`, val: c.right.params.period });
      if (typeof c.right === 'number') options.push({ label: `Entry Cond ${idx+1}: Value (${c.right})`, path: `entry.conditions.${idx}.right`, val: c.right });
    });
    
    // Scan exit conditions
    strategy.exit.conditions.forEach((c, idx) => {
      if (c.left?.params?.period) options.push({ label: `Exit Cond ${idx+1}: ${c.left.name} Period`, path: `exit.conditions.${idx}.left.params.period`, val: c.left.params.period });
      if (c.right?.params?.period) options.push({ label: `Exit Cond ${idx+1}: ${c.right.name} Period`, path: `exit.conditions.${idx}.right.params.period`, val: c.right.params.period });
      if (typeof c.right === 'number') options.push({ label: `Exit Cond ${idx+1}: Value (${c.right})`, path: `exit.conditions.${idx}.right`, val: c.right });
    });
    
    // Risk params
    if (strategy.risk?.stop_loss_pct) options.push({ label: `Risk: Stop Loss %`, path: `risk.stop_loss_pct`, val: strategy.risk.stop_loss_pct });
    if (strategy.risk?.take_profit_pct) options.push({ label: `Risk: Take Profit %`, path: `risk.take_profit_pct`, val: strategy.risk.take_profit_pct });
    
    return options;
  };

  const handleStrategySelect = (e) => {
    const st = strategies.find(s => s.name === e.target.value);
    setSelectedStrategy(st);
    setParamToOptimize('');
  };
  
  const cloneStrategyWithNewParam = (strategy, path, newValue) => {
    let cloned = JSON.parse(JSON.stringify(strategy));
    let parts = path.split('.');
    let current = cloned;
    for (let i = 0; i < parts.length - 1; i++) {
      current = current[parts[i]];
    }
    current[parts[parts.length - 1]] = newValue;
    return cloned;
  };

  const runOptimization = async () => {
    if (!selectedStrategy || !paramToOptimize || !ticker) {
      setError("Please fill all required fields.");
      return;
    }
    
    setIsOptimizing(true);
    setError(null);
    setResults([]);
    setBestResult(null);
    setProgress(0);
    
    let optResults = [];
    let best = { cagr: -999 };
    
    const min = parseFloat(paramMin);
    const max = parseFloat(paramMax);
    const step = parseFloat(paramStep);
    const steps = Math.floor((max - min) / step) + 1;
    
    let currentStep = 0;

    for (let val = min; val <= max; val += step) {
      currentStep++;
      setProgress(Math.round((currentStep / steps) * 100));
      
      const st = cloneStrategyWithNewParam(selectedStrategy, paramToOptimize, val);
      
      try {
        const res = await runBacktest({
          ticker,
          start_date: startDate,
          end_date: endDate,
          initial_capital: 100000,
          strategy: st
        });
        
        const resObj = {
          paramValue: val,
          cagr: res.metrics.cagr,
          winRate: res.metrics.win_rate,
          drawdown: res.metrics.max_drawdown,
          trades: res.metrics.total_trades
        };
        
        optResults.push(resObj);
        
        if (res.metrics.cagr > best.cagr) {
          best = { ...resObj, fullMetrics: res.metrics, strategy: st };
        }
        
      } catch (err) {
        console.error("Optimization iteration failed", err);
      }
    }
    
    setResults(optResults);
    setBestResult(best);
    setIsOptimizing(false);
    triggerFetchIndicator();
  };

  return (
    <div>
      <h1 className="text-3xl font-bold mb-2 text-gray-800">Strategy Optimizer</h1>
      <p className="text-gray-600 mb-8">Run iterative backtests to find the best performing parameters for your custom strategies.</p>
      
      <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200 mb-8">
        <h2 className="text-xl font-bold mb-4 border-b pb-2">1. Select Strategy & Asset</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Custom Strategy</label>
            <select className="w-full border border-gray-300 rounded p-2" onChange={handleStrategySelect} value={selectedStrategy?.name || ''}>
              <option value="">-- Select a Saved Strategy --</option>
              {strategies.map(s => <option key={s.name} value={s.name}>{s.name}</option>)}
            </select>
            {strategies.length === 0 && <p className="text-sm text-red-500 mt-1">You need to create and save a strategy first.</p>}
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Ticker</label>
            <TickerSearch value={ticker} onChange={setTicker} />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Start Date</label>
            <input type="date" className="w-full border border-gray-300 rounded p-2" value={startDate} onChange={e => setStartDate(e.target.value)} />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">End Date</label>
            <input type="date" className="w-full border border-gray-300 rounded p-2" value={endDate} onChange={e => setEndDate(e.target.value)} />
          </div>
        </div>
        
        {selectedStrategy && (
          <>
            <h2 className="text-xl font-bold mb-4 border-b pb-2">2. Select Parameter to Optimize</h2>
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              <div className="md:col-span-1">
                <label className="block text-sm font-medium text-gray-700 mb-1">Parameter</label>
                <select className="w-full border border-gray-300 rounded p-2" value={paramToOptimize} onChange={e => setParamToOptimize(e.target.value)}>
                  <option value="">-- Select Parameter --</option>
                  {getParamOptions(selectedStrategy).map(opt => (
                    <option key={opt.path} value={opt.path}>{opt.label} (Current: {opt.val})</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Min Value</label>
                <input type="number" className="w-full border border-gray-300 rounded p-2" value={paramMin} onChange={e => setParamMin(e.target.value)} />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Max Value</label>
                <input type="number" className="w-full border border-gray-300 rounded p-2" value={paramMax} onChange={e => setParamMax(e.target.value)} />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Step Size</label>
                <input type="number" className="w-full border border-gray-300 rounded p-2" value={paramStep} onChange={e => setParamStep(e.target.value)} />
              </div>
            </div>
            
            <div className="mt-6 flex justify-end">
              <button 
                onClick={runOptimization} 
                disabled={isOptimizing || !paramToOptimize}
                className={`flex items-center px-6 py-2 rounded font-bold text-white transition ${isOptimizing || !paramToOptimize ? 'bg-blue-400 cursor-not-allowed' : 'bg-blue-600 hover:bg-blue-700 shadow-md'}`}
              >
                {isOptimizing ? (
                  <><span className="animate-spin h-5 w-5 mr-2 border-2 border-t-transparent border-white rounded-full"></span> Optimizing... {progress}%</>
                ) : (
                  <><Settings className="mr-2" size={20} /> Start Optimization Grid</>
                )}
              </button>
            </div>
            
            {error && <p className="text-red-500 mt-2">{error}</p>}
          </>
        )}
      </div>
      
      {results.length > 0 && (
        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200">
          <h2 className="text-xl font-bold mb-6 border-b pb-2">Optimization Results</h2>
          
          {bestResult && (
            <div className="bg-green-50 border border-green-200 p-4 rounded-lg mb-8 flex items-center justify-between">
              <div>
                <h3 className="text-green-800 font-bold text-lg flex items-center"><Check size={20} className="mr-2"/> Best Parameter Value: {bestResult.paramValue}</h3>
                <p className="text-green-700 text-sm mt-1">Achieved {bestResult.cagr.toFixed(2)}% CAGR with {bestResult.drawdown.toFixed(2)}% Max Drawdown.</p>
              </div>
              <button 
                onClick={() => {
                  let existing = JSON.parse(localStorage.getItem('saved_strategies') || '[]');
                  const newName = `${selectedStrategy.name} (Opt ${bestResult.paramValue})`;
                  const st = { ...bestResult.strategy, name: newName };
                  existing.push(st);
                  localStorage.setItem('saved_strategies', JSON.stringify(existing));
                  alert(`Saved as "${newName}"!`);
                }}
                className="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded text-sm font-bold shadow transition"
              >
                Save as New Strategy
              </button>
            </div>
          )}
          
          <h3 className="text-lg font-bold mb-4 text-gray-700">CAGR vs Parameter Value</h3>
          <div className="h-80 w-full mb-8">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={results} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="paramValue" />
                <YAxis />
                <Tooltip formatter={(value, name) => [value.toFixed(2), name]} />
                <Legend />
                <Line type="monotone" dataKey="cagr" name="CAGR (%)" stroke="#2563eb" strokeWidth={3} activeDot={{ r: 8 }} />
                <Line type="monotone" dataKey="winRate" name="Win Rate (%)" stroke="#10b981" strokeWidth={2} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          
          <div className="overflow-x-auto rounded-lg border border-gray-200">
            <table className="min-w-full bg-white">
              <thead>
                <tr className="bg-gray-50 text-gray-600 uppercase text-xs leading-normal border-b">
                  <th className="py-3 px-6 text-left font-semibold">Parameter Value</th>
                  <th className="py-3 px-6 text-right font-semibold">CAGR (%)</th>
                  <th className="py-3 px-6 text-right font-semibold">Win Rate (%)</th>
                  <th className="py-3 px-6 text-right font-semibold">Max Drawdown (%)</th>
                  <th className="py-3 px-6 text-right font-semibold">Total Trades</th>
                </tr>
              </thead>
              <tbody className="text-gray-600 text-sm">
                {results.map((res, i) => (
                  <tr key={i} className={`border-b border-gray-100 hover:bg-gray-50 ${bestResult?.paramValue === res.paramValue ? 'bg-blue-50/50' : ''}`}>
                    <td className="py-3 px-6 text-left font-bold">{res.paramValue} {bestResult?.paramValue === res.paramValue && <span className="ml-2 text-xs bg-blue-100 text-blue-800 px-2 py-1 rounded">BEST</span>}</td>
                    <td className={`py-3 px-6 text-right font-bold ${res.cagr >= 0 ? 'text-green-600' : 'text-red-600'}`}>{res.cagr.toFixed(2)}</td>
                    <td className="py-3 px-6 text-right">{res.winRate.toFixed(2)}</td>
                    <td className="py-3 px-6 text-right text-red-500">{res.drawdown.toFixed(2)}</td>
                    <td className="py-3 px-6 text-right">{res.trades}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
