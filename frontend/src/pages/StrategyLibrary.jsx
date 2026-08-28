import React, { useState } from 'react';
import { PlayCircle, Settings, Copy } from 'lucide-react';
import BacktestViewer from '../components/BacktestViewer';

const PRESET_STRATEGIES = [
  {
    id: 1,
    name: "EMA Crossover",
    category: "Trend Following",
    description: "Popular trend-following strategy using fast and slow Exponential Moving Averages.",
    indicators: "EMA (20), EMA (50)",
    complexity: "Low",
    strategyJson: {
      name: "EMA Crossover",
      entry: {
        logic: "ALL",
        conditions: [
          { left: { name: "ema", params: { period: 20 } }, operator: "crosses_above", right: { name: "ema", params: { period: 50 } } }
        ]
      },
      exit: {
        logic: "ANY",
        conditions: [
          { left: { name: "ema", params: { period: 20 } }, operator: "crosses_below", right: { name: "ema", params: { period: 50 } } }
        ]
      },
      risk: { stop_loss_pct: 5.0, take_profit_pct: null }
    }
  },
  {
    id: 2,
    name: "RSI Mean Reversion",
    category: "Mean Reversion",
    description: "Historically studied strategy buying oversold conditions and selling when normalized.",
    indicators: "RSI (14)",
    complexity: "Low",
    strategyJson: {
      name: "RSI Mean Reversion",
      entry: {
        logic: "ALL",
        conditions: [
          { left: { name: "rsi", params: { period: 14 } }, operator: "<", right: 30 }
        ]
      },
      exit: {
        logic: "ANY",
        conditions: [
          { left: { name: "rsi", params: { period: 14 } }, operator: ">", right: 50 }
        ]
      },
      risk: { stop_loss_pct: 3.0, take_profit_pct: null }
    }
  },
  {
    id: 3,
    name: "MACD + RSI + ADX Master",
    category: "Multi-Indicator",
    description: "High probability trend continuation setup requiring momentum, trend strength, and direction alignment.",
    indicators: "MACD, RSI (14), ADX (14)",
    complexity: "High",
    strategyJson: {
      name: "MACD + RSI + ADX Master",
      entry: {
        logic: "ALL",
        conditions: [
          { left: { name: "macd", params: { fast: 12, slow: 26, signal: 9 } }, operator: ">", right: 0 },
          { left: { name: "rsi", params: { period: 14 } }, operator: ">", right: 50 },
          { left: { name: "adx", params: { period: 14 } }, operator: ">", right: 25 }
        ]
      },
      exit: {
        logic: "ANY",
        conditions: [
          { left: { name: "macd", params: { fast: 12, slow: 26, signal: 9 } }, operator: "<", right: 0 },
          { left: { name: "rsi", params: { period: 14 } }, operator: "<", right: 40 }
        ]
      },
      risk: { stop_loss_pct: 4.0, take_profit_pct: 12.0 }
    }
  },
  {
    id: 4,
    name: "Triple Moving Average",
    category: "Trend Following",
    description: "Classic long-term trend filter. Requires short, medium, and long-term averages to align perfectly.",
    indicators: "EMA (20), EMA (50), EMA (200)",
    complexity: "Medium",
    strategyJson: {
      name: "Triple Moving Average",
      entry: {
        logic: "ALL",
        conditions: [
          { left: { name: "ema", params: { period: 20 } }, operator: ">", right: { name: "ema", params: { period: 50 } } },
          { left: { name: "ema", params: { period: 50 } }, operator: ">", right: { name: "ema", params: { period: 200 } } }
        ]
      },
      exit: {
        logic: "ANY",
        conditions: [
          { left: { name: "ema", params: { period: 20 } }, operator: "<", right: { name: "ema", params: { period: 50 } } }
        ]
      },
      risk: { stop_loss_pct: 6.0, trailing_stop_pct: 5.0 }
    }
  },
  {
    id: 5,
    name: "Volume Breakout + Trend",
    category: "Breakout",
    description: "Identifies explosive moves backed by high volume during an existing uptrend.",
    indicators: "Volume SMA (20), EMA (200)",
    complexity: "High",
    strategyJson: {
      name: "Volume Breakout + Trend",
      entry: {
        logic: "ALL",
        conditions: [
          { left: { name: "close", params: {} }, operator: ">", right: { name: "ema", params: { period: 200 } } },
          { left: { name: "volume", params: {} }, operator: ">", right: { name: "volume_sma", params: { period: 20 } } }
        ]
      },
      exit: {
        logic: "ANY",
        conditions: [
          { left: { name: "close", params: {} }, operator: "<", right: { name: "ema", params: { period: 200 } } }
        ]
      },
      risk: { stop_loss_pct: 5.0, take_profit_pct: 15.0 }
    }
  }
];

export default function StrategyLibrary() {
  const [selectedStrategy, setSelectedStrategy] = useState(null);

  if (selectedStrategy) {
    return (
      <div>
        <button onClick={() => setSelectedStrategy(null)} className="mb-4 text-blue-500 hover:underline">
          &larr; Back to Library
        </button>
        <BacktestViewer strategy={selectedStrategy} />
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-3xl font-bold mb-2 text-gray-800">Strategy Library</h1>
      <p className="text-gray-600 mb-8">Select from a large library of popular and historically well-known trading strategies. Past performance does not guarantee future results.</p>
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {PRESET_STRATEGIES.map(st => (
          <div key={st.id} className="bg-white rounded-lg shadow-sm border border-gray-200 p-6 flex flex-col hover:shadow-md transition">
            <div className="flex justify-between items-start mb-2">
              <h3 className="text-xl font-bold text-gray-800">{st.name}</h3>
              <span className="bg-blue-100 text-blue-800 text-xs px-2 py-1 rounded font-semibold">{st.category}</span>
            </div>
            <p className="text-gray-600 text-sm flex-1 mb-4">{st.description}</p>
            
            <div className="text-sm text-gray-500 mb-4 bg-gray-50 p-3 rounded">
              <p className="mb-1"><span className="font-semibold text-gray-700">Indicators:</span> {st.indicators}</p>
              <p><span className="font-semibold text-gray-700">Complexity:</span> <span className={st.complexity === 'High' ? 'text-red-600 font-bold' : st.complexity === 'Medium' ? 'text-orange-500 font-bold' : 'text-green-600 font-bold'}>{st.complexity}</span></p>
            </div>
            
            <div className="flex space-x-2 mt-auto">
              <button 
                onClick={() => setSelectedStrategy(st.strategyJson)}
                className="flex-1 bg-blue-600 hover:bg-blue-700 text-white py-2 rounded flex items-center justify-center font-medium transition shadow-sm"
              >
                <PlayCircle size={18} className="mr-2"/> Backtest
              </button>
              <button className="bg-gray-100 hover:bg-gray-200 text-gray-700 px-3 py-2 rounded transition shadow-sm" title="Customize">
                <Settings size={18} />
              </button>
              <button className="bg-gray-100 hover:bg-gray-200 text-gray-700 px-3 py-2 rounded transition shadow-sm" title="Duplicate">
                <Copy size={18} />
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
