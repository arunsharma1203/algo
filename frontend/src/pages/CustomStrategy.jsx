import React, { useState } from 'react';
import { Plus, Trash2, Save, PlayCircle } from 'lucide-react';
import BacktestViewer from '../components/BacktestViewer';

export default function CustomStrategy() {
  const [showBacktest, setShowBacktest] = useState(false);
  const [strategyName, setStrategyName] = useState("My Custom Strategy");
  
  const [entryConditions, setEntryConditions] = useState([
    { leftInd: 'ema', leftPeriod: 20, operator: 'crosses_above', rightInd: 'ema', rightPeriod: 50 }
  ]);
  const [entryLogic, setEntryLogic] = useState("ALL");
  
  const [exitConditions, setExitConditions] = useState([
    { leftInd: 'ema', leftPeriod: 20, operator: 'crosses_below', rightInd: 'ema', rightPeriod: 50 }
  ]);
  const [exitLogic, setExitLogic] = useState("ANY");
  
  const [stopLoss, setStopLoss] = useState(5.0);
  const [takeProfit, setTakeProfit] = useState(10.0);

  const addCondition = (type) => {
    const newCond = { leftInd: 'rsi', leftPeriod: 14, operator: '>', rightInd: 'value', rightValue: 50 };
    if (type === 'entry') setEntryConditions([...entryConditions, newCond]);
    else setExitConditions([...exitConditions, newCond]);
  };

  const removeCondition = (type, index) => {
    if (type === 'entry') {
      const newC = [...entryConditions];
      newC.splice(index, 1);
      setEntryConditions(newC);
    } else {
      const newC = [...exitConditions];
      newC.splice(index, 1);
      setExitConditions(newC);
    }
  };

  const updateCondition = (type, index, field, value) => {
    const arr = type === 'entry' ? [...entryConditions] : [...exitConditions];
    arr[index][field] = value;
    if (type === 'entry') setEntryConditions(arr);
    else setExitConditions(arr);
  };

  const generateStrategyJson = () => {
    const mapCondition = (c) => {
      let left = { name: c.leftInd, params: {} };
      if (['ema', 'sma', 'rsi', 'adx', 'volume_sma'].includes(c.leftInd)) left.params.period = parseInt(c.leftPeriod);
      if (c.leftInd === 'macd') left.params = { fast: 12, slow: 26, signal: 9 };
      
      let right;
      if (c.rightInd === 'value') {
        right = parseFloat(c.rightValue || 0);
      } else {
        right = { name: c.rightInd, params: {} };
        if (['ema', 'sma', 'rsi', 'adx', 'volume_sma'].includes(c.rightInd)) right.params.period = parseInt(c.rightPeriod);
        if (c.rightInd === 'macd') right.params = { fast: 12, slow: 26, signal: 9 };
      }
      
      return { left, operator: c.operator, right };
    };

    return {
      name: strategyName,
      entry: { logic: entryLogic, conditions: entryConditions.map(mapCondition) },
      exit: { logic: exitLogic, conditions: exitConditions.map(mapCondition) },
      risk: {
        stop_loss_pct: stopLoss ? parseFloat(stopLoss) : null,
        take_profit_pct: takeProfit ? parseFloat(takeProfit) : null
      }
    };
  };

  if (showBacktest) {
    return (
      <div>
        <button onClick={() => setShowBacktest(false)} className="mb-4 text-blue-500 hover:underline">
          &larr; Back to Editor
        </button>
        <BacktestViewer strategy={generateStrategyJson()} />
      </div>
    );
  }

  const renderConditionRow = (c, index, type) => (
    <div key={index} className="flex items-center space-x-3 mb-3 bg-gray-50 p-3 rounded border">
      <select value={c.leftInd} onChange={e => updateCondition(type, index, 'leftInd', e.target.value)} className="border p-2 rounded bg-white">
        <option value="ema">EMA</option>
        <option value="sma">SMA</option>
        <option value="rsi">RSI</option>
        <option value="macd">MACD</option>
        <option value="adx">ADX</option>
        <option value="close">Close Price</option>
        <option value="volume">Volume</option>
      </select>
      
      {['ema', 'sma', 'rsi', 'adx'].includes(c.leftInd) && (
        <input type="number" value={c.leftPeriod || 14} onChange={e => updateCondition(type, index, 'leftPeriod', e.target.value)} className="border p-2 rounded w-20" placeholder="Period" />
      )}
      
      <select value={c.operator} onChange={e => updateCondition(type, index, 'operator', e.target.value)} className="border p-2 rounded bg-white font-bold text-blue-600">
        <option value=">">&gt;</option>
        <option value="<">&lt;</option>
        <option value=">=">&gt;=</option>
        <option value="<=">&lt;=</option>
        <option value="==">==</option>
        <option value="crosses_above">Crosses Above</option>
        <option value="crosses_below">Crosses Below</option>
      </select>
      
      <select value={c.rightInd || 'value'} onChange={e => updateCondition(type, index, 'rightInd', e.target.value)} className="border p-2 rounded bg-white">
        <option value="value">Number Value</option>
        <option value="ema">EMA</option>
        <option value="sma">SMA</option>
        <option value="rsi">RSI</option>
        <option value="macd">MACD</option>
        <option value="adx">ADX</option>
        <option value="close">Close Price</option>
      </select>
      
      {c.rightInd === 'value' ? (
        <input type="number" value={c.rightValue || 50} onChange={e => updateCondition(type, index, 'rightValue', e.target.value)} className="border p-2 rounded w-24" placeholder="Value" />
      ) : (
        ['ema', 'sma', 'rsi', 'adx'].includes(c.rightInd) && (
          <input type="number" value={c.rightPeriod || 50} onChange={e => updateCondition(type, index, 'rightPeriod', e.target.value)} className="border p-2 rounded w-20" placeholder="Period" />
        )
      )}
      
      <button onClick={() => removeCondition(type, index)} className="text-red-500 hover:text-red-700 ml-auto p-2">
        <Trash2 size={18} />
      </button>
    </div>
  );

  const handleSaveStrategy = () => {
    const json = generateStrategyJson();
    const existingStr = localStorage.getItem('saved_strategies');
    let saved = [];
    if (existingStr) {
      try {
        saved = JSON.parse(existingStr);
      } catch(e) {}
    }
    
    // Check if updating or creating new
    const idx = saved.findIndex(s => s.name === json.name);
    if (idx >= 0) {
      saved[idx] = json;
    } else {
      json.id = Date.now();
      saved.push(json);
    }
    
    localStorage.setItem('saved_strategies', JSON.stringify(saved));
    alert(`Strategy "${json.name}" saved successfully!`);
  };

  return (
    <div className="max-w-5xl mx-auto">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold text-gray-800">Strategy Builder</h1>
        <div className="flex space-x-3">
          <button onClick={handleSaveStrategy} className="flex items-center space-x-2 bg-white border border-gray-300 px-4 py-2 rounded text-gray-700 hover:bg-gray-50 transition">
            <Save size={18} /> <span>Save Strategy</span>
          </button>
          <button onClick={() => setShowBacktest(true)} className="flex items-center space-x-2 bg-blue-600 px-4 py-2 rounded text-white hover:bg-blue-700 transition font-medium">
            <PlayCircle size={18} /> <span>Backtest Now</span>
          </button>
        </div>
      </div>
      
      <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200 mb-6">
        <label className="block text-sm font-medium text-gray-700 mb-2">Strategy Name</label>
        <input type="text" value={strategyName} onChange={e => setStrategyName(e.target.value)} className="w-full border border-gray-300 rounded-md p-3 text-lg font-bold" />
      </div>
      
      <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200 mb-6">
        <div className="flex justify-between items-center mb-4 border-b pb-2">
          <h2 className="text-xl font-bold text-green-700 flex items-center">ENTRY RULES</h2>
          <select value={entryLogic} onChange={e => setEntryLogic(e.target.value)} className="border-gray-300 rounded border p-1 text-sm bg-gray-50">
            <option value="ALL">Match ALL conditions (AND)</option>
            <option value="ANY">Match ANY condition (OR)</option>
          </select>
        </div>
        
        {entryConditions.map((c, i) => renderConditionRow(c, i, 'entry'))}
        
        <button onClick={() => addCondition('entry')} className="mt-2 text-blue-600 flex items-center text-sm font-semibold hover:underline">
          <Plus size={16} className="mr-1" /> Add Entry Condition
        </button>
      </div>

      <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200 mb-6">
        <div className="flex justify-between items-center mb-4 border-b pb-2">
          <h2 className="text-xl font-bold text-red-700 flex items-center">EXIT RULES</h2>
          <select value={exitLogic} onChange={e => setExitLogic(e.target.value)} className="border-gray-300 rounded border p-1 text-sm bg-gray-50">
            <option value="ANY">Match ANY condition (OR)</option>
            <option value="ALL">Match ALL conditions (AND)</option>
          </select>
        </div>
        
        {exitConditions.map((c, i) => renderConditionRow(c, i, 'exit'))}
        
        <button onClick={() => addCondition('exit')} className="mt-2 text-blue-600 flex items-center text-sm font-semibold hover:underline">
          <Plus size={16} className="mr-1" /> Add Exit Condition
        </button>
      </div>

      <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200 mb-6">
        <h2 className="text-xl font-bold text-gray-800 mb-4 border-b pb-2">RISK MANAGEMENT</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Stop Loss (%)</label>
            <input type="number" step="0.1" value={stopLoss} onChange={e => setStopLoss(e.target.value)} className="w-full border border-gray-300 rounded p-2" placeholder="e.g. 5.0" />
            <p className="text-xs text-gray-500 mt-1">Leave empty to disable</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Take Profit (%)</label>
            <input type="number" step="0.1" value={takeProfit} onChange={e => setTakeProfit(e.target.value)} className="w-full border border-gray-300 rounded p-2" placeholder="e.g. 15.0" />
            <p className="text-xs text-gray-500 mt-1">Leave empty to disable</p>
          </div>
        </div>
      </div>
    </div>
  );
}
