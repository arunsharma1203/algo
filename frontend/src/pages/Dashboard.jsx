import React, { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Activity, TrendingUp, TrendingDown, Clock } from 'lucide-react';
import { getLatestData } from '../services/api';
import TickerSearch from '../components/TickerSearch';
import { useLiveIndicator } from '../context/LiveIndicatorContext';

export default function Dashboard() {
  const [tickerInput, setTickerInput] = useState('RELIANCE.NS');
  const [activeTicker, setActiveTicker] = useState('RELIANCE.NS');
  const [marketData, setMarketData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [recentSearches, setRecentSearches] = useState([]);
  const [lastStats, setLastStats] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [activeMonitors, setActiveMonitors] = useState([]);
  
  const { triggerFetchIndicator } = useLiveIndicator();

  useEffect(() => {
    const saved = localStorage.getItem('recent_searches');
    let defaultTicker = 'RELIANCE.NS';
    if (saved) {
      const parsed = JSON.parse(saved);
      setRecentSearches(parsed);
      if (parsed.length > 0) defaultTicker = parsed[0];
    }
    setTickerInput(defaultTicker);
    setActiveTicker(defaultTicker);
    fetchMarketData(defaultTicker);
    
    // Fetch Autonomous Bot Alerts
    const fetchAlerts = async () => {
      try {
        const response = await fetch('http://localhost:8000/api/ml/alerts');
        if (response.ok) {
          const data = await response.json();
          setAlerts(data);
        }
        
        const monitorRes = await fetch('http://localhost:8000/api/ml/active-monitors');
        if (monitorRes.ok) {
          setActiveMonitors(await monitorRes.json());
        }
      } catch (err) {}
    };
    fetchAlerts();
    const interval = setInterval(fetchAlerts, 10000); // Check every 10s
    return () => clearInterval(interval);
    
    const stats = localStorage.getItem('last_backtest_stats');
    if (stats) setLastStats(JSON.parse(stats));
  }, []);

  const addRecentSearch = (ticker) => {
    const updated = [ticker, ...recentSearches.filter(t => t !== ticker)].slice(0, 5);
    setRecentSearches(updated);
    localStorage.setItem('recent_searches', JSON.stringify(updated));
  };

  const fetchMarketData = async (ticker) => {
    if (!ticker) return;
    const upperTicker = ticker.toUpperCase();
    
    setLoading(true);
    setError(null);
    try {
      const data = await getLatestData(upperTicker);
      setMarketData(data);
      setActiveTicker(upperTicker);
      addRecentSearch(upperTicker);
      triggerFetchIndicator(data.metadata); // Trigger global green live indicator
    } catch (err) {
      setError(`Failed to fetch data for ${upperTicker}.`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold text-gray-800">Dashboard</h1>
        <div className="w-80">
          <TickerSearch 
            value={tickerInput} 
            onChange={setTickerInput} 
            onSubmit={fetchMarketData} 
          />
        </div>
      </div>
      
      {recentSearches.length > 0 && (
        <div className="flex items-center space-x-2 mb-6 text-sm">
          <Clock size={16} className="text-gray-500" />
          <span className="text-gray-500">Recent:</span>
          {recentSearches.map(ticker => (
            <button 
              key={ticker} 
              onClick={() => { setTickerInput(ticker); fetchMarketData(ticker); }}
              className="px-2 py-1 bg-gray-200 hover:bg-gray-300 rounded text-gray-700 transition"
            >
              {ticker}
            </button>
          ))}
        </div>
      )}
      
      {error && (
        <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-6">
          {error}
        </div>
      )}
      
      {/* Autonomous Bot Alert Feed */}
      <div className="mb-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-bold text-gray-800 flex items-center">
            <Activity className="mr-2 text-indigo-500 animate-pulse" /> Autonomous Trade Manager (Live)
          </h2>
          {activeMonitors.length > 0 && (
            <div className="flex space-x-2">
              {activeMonitors.map((m, i) => (
                <span key={i} className="text-xs font-bold bg-gray-100 text-gray-600 px-2 py-1 rounded border border-gray-200">
                  <span className={m.direction === 'BULLISH' ? 'text-green-600' : 'text-red-600'}>●</span> {m.ticker.replace('.NS', '')}
                </span>
              ))}
            </div>
          )}
          <span className="text-xs font-bold bg-green-100 text-green-700 px-2 py-1 rounded animate-pulse flex items-center">
            <span className="w-2 h-2 rounded-full bg-green-500 mr-2"></span>
            Monitoring Active Calls
          </span>
        </div>
        
        {alerts && alerts.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {alerts.slice(0, 3).map((alert, i) => (
              <div key={i} className={`p-4 rounded-xl border shadow-sm ${alert.level === 'CRITICAL' ? 'bg-red-50 border-red-200' : alert.level === 'WARNING' ? 'bg-orange-50 border-orange-200' : 'bg-blue-50 border-blue-200'}`}>
                <div className="flex justify-between items-start mb-2">
                  <span className={`font-black ${alert.level === 'CRITICAL' ? 'text-red-700' : alert.level === 'WARNING' ? 'text-orange-700' : 'text-blue-700'}`}>
                    {alert.ticker}
                  </span>
                  <span className="text-xs text-gray-500 font-mono">
                    {new Date(alert.timestamp).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}
                  </span>
                </div>
                <p className={`text-sm ${alert.level === 'CRITICAL' ? 'text-red-900 font-medium' : alert.level === 'WARNING' ? 'text-orange-900' : 'text-blue-900'}`}>
                  {alert.message}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <div className="bg-gray-50 border border-gray-200 border-dashed rounded-xl p-6 text-center text-gray-500 text-sm">
            ✅ <strong>All Systems Nominal.</strong> The Autonomous Bot is running in the background and scanning open trades. No critical market shifts detected.
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-100 hover:shadow-md transition">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-gray-500 text-sm">Selected Stock</p>
              <h3 className="text-2xl font-bold mt-1 truncate max-w-[150px]" title={activeTicker}>{activeTicker}</h3>
              {loading ? (
                <p className="text-gray-400 text-sm mt-2">Loading...</p>
              ) : marketData ? (
                <p className={`text-sm flex items-center mt-2 font-medium ${marketData.change_pct >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {marketData.change_pct >= 0 ? <TrendingUp size={16} className="mr-1"/> : <TrendingDown size={16} className="mr-1"/>}
                  {marketData.change_pct > 0 ? '+' : ''}{(marketData.change_pct != null ? marketData.change_pct.toFixed(2) : '-')}% (₹{(marketData.close != null ? marketData.close.toFixed(2) : '-')})
                </p>
              ) : null}
            </div>
            <div className="p-3 bg-blue-50 rounded-full text-blue-600">
              <Activity size={24} />
            </div>
          </div>
        </div>
        
        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-100 hover:shadow-md transition">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-gray-500 text-sm">Active Strategy</p>
              <h3 className="text-xl font-bold mt-1 truncate">EMA Crossover</h3>
              <p className="text-gray-600 text-sm mt-2">Status: <span className="text-blue-600 font-bold">Monitoring</span></p>
            </div>
            <div className="p-3 bg-purple-50 rounded-full text-purple-600">
              <Activity size={24} />
            </div>
          </div>
        </div>
        
        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-100 hover:shadow-md transition">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-gray-500 text-sm">Historical CAGR ({activeTicker})</p>
              {lastStats?.ticker === activeTicker ? (
                <>
                  <h3 className="text-2xl font-bold mt-1 text-gray-800">{lastStats.cagr.toFixed(2)}%</h3>
                  <p className="text-gray-500 text-sm mt-2 flex items-center">From last backtest run</p>
                </>
              ) : (
                <>
                  <h3 className="text-2xl font-bold mt-1 text-gray-400">--</h3>
                  <Link to="/custom" className="text-blue-600 text-sm mt-2 flex items-center font-bold hover:underline">
                    Run Backtest <Activity size={14} className="ml-1" />
                  </Link>
                </>
              )}
            </div>
            <div className="p-3 bg-green-50 rounded-full text-green-600">
              <TrendingUp size={24} />
            </div>
          </div>
        </div>

        <div className="bg-white p-6 rounded-lg shadow-sm border border-gray-100 hover:shadow-md transition">
          <div className="flex justify-between items-start">
            <div>
              <p className="text-gray-500 text-sm">Max Drawdown ({activeTicker})</p>
              {lastStats?.ticker === activeTicker ? (
                <>
                  <h3 className="text-2xl font-bold mt-1 text-red-500">{lastStats.max_drawdown.toFixed(2)}%</h3>
                  <p className="text-gray-500 text-sm mt-2 flex items-center">From last backtest run</p>
                </>
              ) : (
                <>
                  <h3 className="text-2xl font-bold mt-1 text-gray-400">--</h3>
                  <Link to="/custom" className="text-blue-600 text-sm mt-2 flex items-center font-bold hover:underline">
                    Test Strategy <Activity size={14} className="ml-1" />
                  </Link>
                </>
              )}
            </div>
            <div className="p-3 bg-red-50 rounded-full text-red-600">
              <TrendingDown size={24} />
            </div>
          </div>
        </div>
      </div>
      
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Trend Widget */}
        <div className="lg:col-span-1 bg-white rounded-xl shadow-sm border border-gray-100 p-6 flex flex-col">
          <h2 className="text-xl font-bold mb-4 text-gray-800 border-b pb-2 flex items-center">
            <Activity className="mr-2 text-blue-500" size={20}/> Technical Standpoint
          </h2>
          
          {loading ? (
            <div className="flex-1 flex justify-center items-center">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
            </div>
          ) : marketData ? (
            <div className="flex-1 flex flex-col">
              {(() => {
                const md = marketData;
                let score = 0;
                
                // EMA 20 vs 50
                if (md.ema_20 > md.ema_50) score += 1; else score -= 1;
                // EMA 50 vs 200
                if (md.ema_50 > md.ema_200) score += 1; else score -= 1;
                // Price vs 20 EMA
                if (md.close > md.ema_20) score += 1; else score -= 1;
                // MACD
                if (md.macd > 0) score += 1; else score -= 1;
                // Stochastic
                if (md.stoch_k > md.stoch_d) score += 1; else score -= 1;
                
                let trendText = "NEUTRAL";
                let trendColor = "bg-gray-100 text-gray-800 border-gray-200";
                
                if (score >= 4) { trendText = "STRONG BULLISH"; trendColor = "bg-green-100 text-green-800 border-green-200"; }
                else if (score >= 1) { trendText = "BULLISH"; trendColor = "bg-green-50 text-green-700 border-green-100"; }
                else if (score <= -4) { trendText = "STRONG BEARISH"; trendColor = "bg-red-100 text-red-800 border-red-200"; }
                else if (score <= -1) { trendText = "BEARISH"; trendColor = "bg-red-50 text-red-700 border-red-100"; }
                
                return (
                  <>
                    <div className="flex items-center justify-between mb-6">
                      <span className="text-gray-600 font-semibold text-sm uppercase tracking-wide">Overall Rating:</span>
                      <span className={`px-4 py-1.5 rounded-full text-xs font-extrabold tracking-wider border shadow-sm ${trendColor}`}>
                        {trendText}
                      </span>
                    </div>
                    
                    <div className="space-y-4 flex-1">
                      {/* Price Action & Bands */}
                      <div>
                        <div className="flex justify-between text-xs mb-1">
                          <span className="text-gray-500">Price vs Bollinger Bands (20,2)</span>
                        </div>
                        <div className="relative h-2 bg-gray-200 rounded-full overflow-hidden">
                          {/* Map lower to 0%, upper to 100% */}
                          {(() => {
                            const range = md.bb_upper - md.bb_lower;
                            const pos = range > 0 ? ((md.close - md.bb_lower) / range) * 100 : 50;
                            const clampedPos = Math.max(0, Math.min(100, pos));
                            return (
                              <div 
                                className={`absolute top-0 h-full w-2 rounded-full ${clampedPos > 80 ? 'bg-red-500' : clampedPos < 20 ? 'bg-green-500' : 'bg-blue-500'}`}
                                style={{ left: `calc(${clampedPos}% - 4px)` }}
                              />
                            );
                          })()}
                        </div>
                        <div className="flex justify-between text-[10px] text-gray-400 mt-1">
                          <span>Lower: {(md.bb_lower != null ? md.bb_lower.toFixed(1) : '-')}</span>
                          <span className="font-bold text-gray-700 text-xs">₹{(md.close != null ? md.close.toFixed(2) : '-')}</span>
                          <span>Upper: {(md.bb_upper != null ? md.bb_upper.toFixed(1) : '-')}</span>
                        </div>
                      </div>
                      
                      {/* Stochastic */}
                      <div className="grid grid-cols-2 gap-3 mt-4">
                        <div className="bg-gray-50 p-2 rounded border border-gray-100">
                          <p className="text-[10px] text-gray-400 font-bold uppercase">Stochastic %K</p>
                          <p className="font-semibold text-gray-800">{(md.stoch_k != null ? md.stoch_k.toFixed(1) : '-')}</p>
                        </div>
                        <div className="bg-gray-50 p-2 rounded border border-gray-100">
                          <p className="text-[10px] text-gray-400 font-bold uppercase">Stochastic %D</p>
                          <p className="font-semibold text-gray-800">{(md.stoch_d != null ? md.stoch_d.toFixed(1) : '-')}</p>
                        </div>
                      </div>
                      
                      {/* RSI & MACD */}
                      <div className="grid grid-cols-2 gap-3 mt-2">
                        <div className={`p-2 rounded border ${md.rsi_14 > 70 ? 'bg-red-50 border-red-100' : md.rsi_14 < 30 ? 'bg-green-50 border-green-100' : 'bg-gray-50 border-gray-100'}`}>
                          <p className="text-[10px] text-gray-400 font-bold uppercase">RSI (14)</p>
                          <p className={`font-semibold ${md.rsi_14 > 70 ? 'text-red-600' : md.rsi_14 < 30 ? 'text-green-600' : 'text-gray-800'}`}>
                            {(md.rsi_14 != null ? md.rsi_14.toFixed(1) : '-')}
                          </p>
                        </div>
                        <div className={`p-2 rounded border ${md.macd > 0 ? 'bg-green-50 border-green-100' : 'bg-red-50 border-red-100'}`}>
                          <p className="text-[10px] text-gray-400 font-bold uppercase">MACD</p>
                          <p className={`font-semibold ${md.macd > 0 ? 'text-green-600' : 'text-red-600'}`}>
                            {md.macd > 0 ? '+' : ''}{(md.macd != null ? md.macd.toFixed(2) : '-')}
                          </p>
                        </div>
                      </div>
                      
                      {/* ADX */}
                      <div className="flex items-center justify-between bg-gray-50 p-2 rounded border border-gray-100 mt-2">
                        <span className="text-[10px] text-gray-400 font-bold uppercase">Trend Strength (ADX)</span>
                        <span className={`text-sm font-bold ${md.adx > 25 ? 'text-green-600' : 'text-gray-500'}`}>{(md.adx != null ? md.adx.toFixed(1) : '-')}</span>
                      </div>
                    </div>
                  </>
                );
              })()}
            </div>
          ) : (
            <p className="text-gray-500 text-center py-8">No data available.</p>
          )}
        </div>

        {/* Existing Market Indicators Panel (Grid) */}
        <div className="lg:col-span-2 bg-white rounded-xl shadow-sm border border-gray-100 p-6">
          <h2 className="text-xl font-bold mb-6 text-gray-800 border-b pb-2">Moving Averages Matrix</h2>
          {loading ? (
            <div className="flex justify-center items-center h-32">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
            </div>
          ) : marketData ? (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
              <div className="p-5 bg-gray-50 rounded-xl border border-gray-200 shadow-sm relative overflow-hidden group">
                <div className="absolute top-0 left-0 w-1 h-full bg-blue-400 group-hover:w-2 transition-all"></div>
                <span className="text-gray-500 text-xs font-bold uppercase tracking-wider block mb-1">Short-Term (20 EMA)</span>
                <p className="text-3xl font-bold text-gray-800">₹{(marketData.ema_20 != null ? marketData.ema_20.toFixed(2) : '-')}</p>
                <p className={`text-xs mt-2 font-medium ${marketData.close > marketData.ema_20 ? 'text-green-600' : 'text-red-500'}`}>
                  Price is {Math.abs(((marketData.close - marketData.ema_20)/marketData.ema_20)*100).toFixed(1)}% {marketData.close > marketData.ema_20 ? 'above' : 'below'}
                </p>
              </div>
              <div className="p-5 bg-gray-50 rounded-xl border border-gray-200 shadow-sm relative overflow-hidden group">
                <div className="absolute top-0 left-0 w-1 h-full bg-indigo-400 group-hover:w-2 transition-all"></div>
                <span className="text-gray-500 text-xs font-bold uppercase tracking-wider block mb-1">Medium-Term (50 EMA)</span>
                <p className="text-3xl font-bold text-gray-800">₹{(marketData.ema_50 != null ? marketData.ema_50.toFixed(2) : '-')}</p>
                <p className={`text-xs mt-2 font-medium ${marketData.close > marketData.ema_50 ? 'text-green-600' : 'text-red-500'}`}>
                  Price is {Math.abs(((marketData.close - marketData.ema_50)/marketData.ema_50)*100).toFixed(1)}% {marketData.close > marketData.ema_50 ? 'above' : 'below'}
                </p>
              </div>
              <div className="p-5 bg-gray-50 rounded-xl border border-gray-200 shadow-sm relative overflow-hidden group">
                <div className="absolute top-0 left-0 w-1 h-full bg-purple-500 group-hover:w-2 transition-all"></div>
                <span className="text-gray-500 text-xs font-bold uppercase tracking-wider block mb-1">Macro Trend (200 EMA)</span>
                <p className="text-3xl font-bold text-gray-800">₹{(marketData.ema_200 != null ? marketData.ema_200.toFixed(2) : '-')}</p>
                <p className={`text-xs mt-2 font-medium ${marketData.close > marketData.ema_200 ? 'text-green-600' : 'text-red-500'}`}>
                  Price is {Math.abs(((marketData.close - marketData.ema_200)/marketData.ema_200)*100).toFixed(1)}% {marketData.close > marketData.ema_200 ? 'above' : 'below'}
                </p>
              </div>
            </div>
          ) : (
            <p className="text-gray-500 text-center py-8">No data available.</p>
          )}
        </div>
      </div>
    </div>
  );
}
