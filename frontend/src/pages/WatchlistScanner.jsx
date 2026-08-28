import React, { useState, useEffect } from 'react';
import { RefreshCw } from 'lucide-react';
import { getLatestData } from '../services/api';
import TickerSearch from '../components/TickerSearch';
import { useLiveIndicator } from '../context/LiveIndicatorContext';

const DEFAULT_WATCHLIST = ['RELIANCE.NS', 'TCS.NS', 'INFY.NS', 'HDFCBANK.NS', 'ICICIBANK.NS'];

export default function WatchlistScanner() {
  const [watchlist, setWatchlist] = useState([]);
  const [data, setData] = useState({});
  const [loading, setLoading] = useState(false);
  const [newTicker, setNewTicker] = useState('');
  const [selectedTrend, setSelectedTrend] = useState(null);
  
  const { triggerFetchIndicator } = useLiveIndicator();

  useEffect(() => {
    const saved = localStorage.getItem('watchlist');
    if (saved) {
      setWatchlist(JSON.parse(saved));
    } else {
      setWatchlist(DEFAULT_WATCHLIST);
      localStorage.setItem('watchlist', JSON.stringify(DEFAULT_WATCHLIST));
    }
  }, []);

  useEffect(() => {
    if (watchlist.length > 0) {
      scanWatchlist();
    }
  }, [watchlist]);

  const scanWatchlist = async () => {
    setLoading(true);
    const newData = { ...data };
    let success = false;
    
    for (const ticker of watchlist) {
      try {
        const result = await getLatestData(ticker);
        newData[ticker] = result;
        success = true;
      } catch (e) {
        console.error("Failed to fetch", ticker);
        newData[ticker] = { error: true };
      }
    }
    
    setData(newData);
    setLoading(false);
    if (success) {
      // Find the first successful response with metadata
      const firstMeta = Object.values(newData).find(d => !d.error && d.metadata)?.metadata;
      triggerFetchIndicator(firstMeta);
    }
  };

  const handleAdd = (ticker) => {
    if (!ticker) return;
    const cleanTicker = ticker.trim().toUpperCase();
    if (cleanTicker && !watchlist.includes(cleanTicker)) {
      const updated = [...watchlist, cleanTicker];
      setWatchlist(updated);
      localStorage.setItem('watchlist', JSON.stringify(updated));
      setNewTicker('');
    }
  };

  const handleRemove = (ticker) => {
    const updated = watchlist.filter(t => t !== ticker);
    setWatchlist(updated);
    localStorage.setItem('watchlist', JSON.stringify(updated));
    
    const newData = { ...data };
    delete newData[ticker];
    setData(newData);
  };

  return (
    <div>
      {selectedTrend && (
        <div className="fixed inset-0 bg-black bg-opacity-30 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-xl shadow-2xl p-6 max-w-md w-full max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center mb-2">
              <h3 className="text-xl font-bold">Trend Report: {selectedTrend.ticker}</h3>
              <button onClick={() => setSelectedTrend(null)} className="text-gray-400 hover:text-gray-600">✕</button>
            </div>
            
            <div className={`text-sm font-bold px-3 py-1 rounded inline-block mb-4 ${selectedTrend.isBullish ? 'bg-green-100 text-green-800' : selectedTrend.isBearish ? 'bg-red-100 text-red-800' : 'bg-gray-100 text-gray-800'}`}>
              OVERALL: {selectedTrend.isBullish ? 'BULLISH' : selectedTrend.isBearish ? 'BEARISH' : 'NEUTRAL'}
            </div>
            <p className="text-gray-700 text-sm mb-4">
              Comprehensive technical breakdown of all indicators for {selectedTrend.ticker}:
            </p>
            <ul className="space-y-4 mb-6">
              
              {/* Short Term Trend */}
              <li className="flex items-start bg-gray-50 p-3 rounded-lg border border-gray-100">
                <span className={`h-6 w-6 rounded-full flex items-center justify-center mr-3 mt-0.5 shadow-sm text-sm ${selectedTrend.st_ema ? 'bg-green-500 text-white' : 'bg-red-500 text-white'}`}>
                  {selectedTrend.st_ema ? '↑' : '↓'}
                </span>
                <div>
                  <p className="font-bold text-gray-800">Short-Term Trend (20 vs 50)</p>
                  <p className="text-xs text-gray-600 mt-1">
                    20 EMA (<span className="font-semibold">{(selectedTrend.data.ema_20 != null ? selectedTrend.data.ema_20.toFixed(2) : '-')}</span>) is {selectedTrend.st_ema ? 'above' : 'below'} 50 EMA (<span className="font-semibold">{(selectedTrend.data.ema_50 != null ? selectedTrend.data.ema_50.toFixed(2) : '-')}</span>).
                  </p>
                </div>
              </li>

              {/* Long Term Trend */}
              <li className="flex items-start bg-gray-50 p-3 rounded-lg border border-gray-100">
                <span className={`h-6 w-6 rounded-full flex items-center justify-center mr-3 mt-0.5 shadow-sm text-sm ${selectedTrend.lt_ema ? 'bg-green-500 text-white' : 'bg-red-500 text-white'}`}>
                  {selectedTrend.lt_ema ? '↑' : '↓'}
                </span>
                <div>
                  <p className="font-bold text-gray-800">Macro Trend (50 vs 200)</p>
                  <p className="text-xs text-gray-600 mt-1">
                    50 EMA (<span className="font-semibold">{(selectedTrend.data.ema_50 != null ? selectedTrend.data.ema_50.toFixed(2) : '-')}</span>) is {selectedTrend.lt_ema ? 'above' : 'below'} 200 EMA (<span className="font-semibold">{(selectedTrend.data.ema_200 != null ? selectedTrend.data.ema_200.toFixed(2) : '-')}</span>).
                  </p>
                </div>
              </li>

              {/* Price Action */}
              <li className="flex items-start bg-gray-50 p-3 rounded-lg border border-gray-100">
                <span className={`h-6 w-6 rounded-full flex items-center justify-center mr-3 mt-0.5 shadow-sm text-sm ${selectedTrend.price_action ? 'bg-green-500 text-white' : 'bg-red-500 text-white'}`}>
                  {selectedTrend.price_action ? '↑' : '↓'}
                </span>
                <div>
                  <p className="font-bold text-gray-800">Current Price Action</p>
                  <p className="text-xs text-gray-600 mt-1">
                    Closing price (<span className="font-semibold">{(selectedTrend.data.close != null ? selectedTrend.data.close.toFixed(2) : '-')}</span>) is {selectedTrend.price_action ? 'trading above' : 'trading below'} its 20-day average.
                  </p>
                </div>
              </li>

              {/* MACD */}
              <li className="flex items-start bg-gray-50 p-3 rounded-lg border border-gray-100">
                <span className={`h-6 w-6 rounded-full flex items-center justify-center mr-3 mt-0.5 shadow-sm text-sm ${selectedTrend.macd_pos ? 'bg-green-500 text-white' : 'bg-red-500 text-white'}`}>
                  {selectedTrend.macd_pos ? '+' : '-'}
                </span>
                <div>
                  <p className="font-bold text-gray-800">Momentum (MACD)</p>
                  <p className="text-xs text-gray-600 mt-1">
                    MACD histogram is {selectedTrend.macd_pos ? 'positive' : 'negative'} (<span className="font-semibold">{(selectedTrend.data.macd != null ? selectedTrend.data.macd.toFixed(2) : '-')}</span>), indicating {selectedTrend.macd_pos ? 'upward' : 'downward'} momentum.
                  </p>
                </div>
              </li>
              
              {/* RSI & ADX */}
              <div className="grid grid-cols-2 gap-3 mt-2">
                <div className="bg-gray-50 p-3 rounded-lg border border-gray-100 text-center">
                  <p className="text-xs text-gray-500 uppercase font-bold tracking-wider mb-1">RSI (14)</p>
                  <p className={`text-xl font-black ${selectedTrend.rsi > 70 ? 'text-red-500' : selectedTrend.rsi < 30 ? 'text-green-500' : 'text-blue-500'}`}>
                    {(selectedTrend.rsi != null ? selectedTrend.rsi.toFixed(1) : '-')}
                  </p>
                  <p className="text-[10px] text-gray-500 mt-1">
                    {selectedTrend.rsi > 70 ? 'Overbought' : selectedTrend.rsi < 30 ? 'Oversold' : 'Neutral'}
                  </p>
                </div>
                
                <div className="bg-gray-50 p-3 rounded-lg border border-gray-100 text-center">
                  <p className="text-xs text-gray-500 uppercase font-bold tracking-wider mb-1">ADX Strength</p>
                  <p className={`text-xl font-black ${selectedTrend.adx > 25 ? 'text-green-600' : 'text-gray-400'}`}>
                    {(selectedTrend.adx != null ? selectedTrend.adx.toFixed(1) : '-')}
                  </p>
                  <p className="text-[10px] text-gray-500 mt-1">
                    {selectedTrend.adx > 25 ? 'Strong Trend' : 'Weak/Sideways Trend'}
                  </p>
                </div>
              </div>

            </ul>
            <button onClick={() => setSelectedTrend(null)} className="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 rounded-lg shadow-md transition">
              Close Report
            </button>
          </div>
        </div>
      )}

      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold text-gray-800">Watchlist Scanner</h1>
        <button onClick={scanWatchlist} disabled={loading} className="flex items-center space-x-2 bg-gray-200 hover:bg-gray-300 px-4 py-2 rounded transition">
          <RefreshCw size={18} className={loading ? "animate-spin" : ""} /> <span>Refresh Data</span>
        </button>
      </div>

      <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-200 mb-8">
        <div className="max-w-md">
          <TickerSearch 
            value={newTicker}
            onChange={setNewTicker}
            onSubmit={handleAdd}
            placeholder="Add ticker (e.g. ITC.NS)"
          />
        </div>
      </div>

      <div className="overflow-x-auto bg-white rounded-lg shadow-sm border border-gray-200">
        <table className="min-w-full">
          <thead>
            <tr className="bg-gray-50 border-b border-gray-200 text-gray-500 uppercase text-xs font-semibold text-left">
              <th className="py-3 px-6">Ticker</th>
              <th className="py-3 px-6 text-right">Price</th>
              <th className="py-3 px-6 text-right">Change</th>
              <th className="py-3 px-6 text-right">RSI (14)</th>
              <th className="py-3 px-6 text-right">MACD</th>
              <th className="py-3 px-6 text-right">20 EMA</th>
              <th className="py-3 px-6 text-right">200 EMA</th>
              <th className="py-3 px-6 text-center">Trend</th>
              <th className="py-3 px-6 text-center">Action</th>
            </tr>
          </thead>
          <tbody className="text-sm">
            {watchlist.map((ticker) => {
              const d = data[ticker];
              
              if (!d) return (
                <tr key={ticker} className="border-b border-gray-100">
                  <td className="py-4 px-6 font-bold">{ticker}</td>
                  <td colSpan="8" className="py-4 px-6 text-center text-gray-400">Loading...</td>
                </tr>
              );
              
              if (d.error) return (
                <tr key={ticker} className="border-b border-gray-100 bg-red-50">
                  <td className="py-4 px-6 font-bold">{ticker}</td>
                  <td colSpan="7" className="py-4 px-6 text-center text-red-500">Failed to load data</td>
                  <td className="py-4 px-6 text-center">
                    <button onClick={() => handleRemove(ticker)} className="text-red-500 hover:underline text-xs">Remove</button>
                  </td>
                </tr>
              );

              const isBullish = d.ema_20 > d.ema_200 && d.macd > 0;
              const isBearish = d.ema_20 < d.ema_200 && d.macd < 0;

              const handleTrendClick = () => {
                const st_ema = d.ema_20 > d.ema_50;
                const lt_ema = d.ema_50 > d.ema_200;
                const price_action = d.close > d.ema_20;
                const macd_pos = d.macd > 0;
                
                // Let's determine overall trend dynamically based on majority
                const score = (st_ema ? 1 : 0) + (lt_ema ? 1 : 0) + (price_action ? 1 : 0) + (macd_pos ? 1 : 0);
                const isOverallBullish = score >= 3;
                const isOverallBearish = score <= 1;

                setSelectedTrend({
                  ticker,
                  isBullish: isOverallBullish,
                  isBearish: isOverallBearish,
                  data: d,
                  st_ema,
                  lt_ema,
                  price_action,
                  macd_pos,
                  rsi: d.rsi_14,
                  adx: d.adx
                });
              };

              return (
                <tr key={ticker} className="border-b border-gray-100 hover:bg-gray-50">
                  <td className="py-4 px-6 font-bold">{ticker}</td>
                  <td className="py-4 px-6 text-right font-medium">₹{(d.close != null ? d.close.toFixed(2) : '-')}</td>
                  <td className={`py-4 px-6 text-right font-semibold ${d.change_pct >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                    {d.change_pct > 0 ? '+' : ''}{(d.change_pct != null ? d.change_pct.toFixed(2) : '-')}%
                  </td>
                  <td className={`py-4 px-6 text-right font-medium ${d.rsi_14 > 70 ? 'text-red-500' : d.rsi_14 < 30 ? 'text-green-500' : 'text-gray-600'}`}>
                    {(d.rsi_14 != null ? d.rsi_14.toFixed(1) : '-')}
                  </td>
                  <td className={`py-4 px-6 text-right ${d.macd >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                    {(d.macd != null ? d.macd.toFixed(2) : '-')}
                  </td>
                  <td className="py-4 px-6 text-right text-gray-600">₹{(d.ema_20 != null ? d.ema_20.toFixed(2) : '-')}</td>
                  <td className="py-4 px-6 text-right text-gray-600">₹{(d.ema_200 != null ? d.ema_200.toFixed(2) : '-')}</td>
                  <td className="py-4 px-6 text-center cursor-pointer" onClick={handleTrendClick}>
                    {isBullish ? (
                      <span className="bg-green-100 text-green-800 px-2 py-1 rounded text-xs font-bold shadow-sm hover:ring-2 hover:ring-green-400 transition">BULLISH</span>
                    ) : isBearish ? (
                      <span className="bg-red-100 text-red-800 px-2 py-1 rounded text-xs font-bold shadow-sm hover:ring-2 hover:ring-red-400 transition">BEARISH</span>
                    ) : (
                      <span className="bg-gray-100 text-gray-800 px-2 py-1 rounded text-xs font-bold shadow-sm hover:ring-2 hover:ring-gray-400 transition">NEUTRAL</span>
                    )}
                  </td>
                  <td className="py-4 px-6 text-center">
                    <button onClick={() => handleRemove(ticker)} className="text-red-500 hover:underline text-xs">Remove</button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
