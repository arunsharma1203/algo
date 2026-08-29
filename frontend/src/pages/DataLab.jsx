import React, { useState, useEffect } from 'react';
import axios from 'axios';
import { Database, ShieldCheck, Activity, RefreshCw, BarChart2, Layers, AlertTriangle, CheckCircle, TrendingUp, Cpu, Compass, Sliders, Play } from 'lucide-react';

export default function DataLab() {
  const [universe, setUniverse] = useState('BENCHMARK_5');
  const [coverageData, setCoverageData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState(null);
  const [searchTerm, setSearchTerm] = useState('');

  // Portfolio Backtest state
  const [portfolioRunning, setPortfolioRunning] = useState(false);
  const [portfolioResults, setPortfolioResults] = useState(null);
  const [portfolioError, setPortfolioError] = useState(null);
  const [capital, setCapital] = useState(500000);
  const [heatCap, setHeatCap] = useState(6.0);
  const [kellyFraction, setKellyFraction] = useState('HALF');

  const fetchCoverage = async (u = universe) => {
    setLoading(true);
    try {
      const res = await axios.get(`http://localhost:8000/api/data-lab/coverage?universe=${u}`);
      setCoverageData(res.data);
    } catch (e) {
      console.error("Coverage fetch error:", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchCoverage(universe);
  }, [universe]);

  const handleSync10Y = async () => {
    setSyncing(true);
    setSyncMessage(null);
    try {
      const res = await axios.post('http://localhost:8000/api/data-lab/sync-10y', {
        universe: universe,
        force_refresh: false
      });
      if (res.data?.status === 'success') {
        setSyncMessage({ type: 'success', text: `✅ Synced ${res.data.synced_count}/${res.data.total_tickers} tickers successfully.` });
        fetchCoverage(universe);
      } else {
        setSyncMessage({ type: 'error', text: '❌ Sync failed.' });
      }
    } catch (e) {
      setSyncMessage({ type: 'error', text: `❌ Network error: ${e.message}` });
    } finally {
      setSyncing(false);
    }
  };

  const handleRunPortfolioBacktest = async () => {
    setPortfolioRunning(true);
    setPortfolioError(null);
    setPortfolioResults(null);
    try {
      const res = await axios.post('http://localhost:8000/api/data-lab/portfolio-backtest', {
        universe: universe,
        initial_capital: parseFloat(capital),
        max_portfolio_heat: parseFloat(heatCap),
        kelly_mode: kellyFraction,
        brokerage_per_order: 20.0,
        slippage_pct: 0.08
      });
      setPortfolioResults(res.data);
    } catch (e) {
      setPortfolioError(e.response?.data?.detail || e.message);
    } finally {
      setPortfolioRunning(false);
    }
  };

  const filteredTickers = (coverageData?.daily_coverage?.tickers_detail || []).filter(t => 
    t.ticker.toLowerCase().includes(searchTerm.toLowerCase())
  );

  return (
    <div className="space-y-6">
      {/* Header Banner */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 relative overflow-hidden shadow-2xl">
        <div className="absolute top-0 right-0 w-96 h-96 bg-cyan-500/10 rounded-full blur-3xl pointer-events-none -mr-20 -mt-20"></div>
        <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 relative z-10">
          <div>
            <div className="flex items-center gap-3">
              <span className="px-3 py-1 bg-cyan-950/80 border border-cyan-500/30 text-cyan-400 text-xs font-semibold rounded-full flex items-center gap-1.5 shadow-sm">
                <Database className="w-3.5 h-3.5" /> 10-Year Historical Data Foundation
              </span>
              <span className="px-3 py-1 bg-emerald-950/80 border border-emerald-500/30 text-emerald-400 text-xs font-semibold rounded-full flex items-center gap-1.5 shadow-sm">
                <ShieldCheck className="w-3.5 h-3.5" /> Scientific Zero-Synthetic Integrity
              </span>
            </div>
            <h1 className="text-2xl font-black tracking-tight text-white mt-2">
              Research Data Lab & Portfolio Walk-Forward
            </h1>
            <p className="text-slate-400 text-sm mt-1 max-w-2xl">
              Centralized historical market-data layer managing 10-year daily and accumulated 15m intraday feeds with multi-stock portfolio walk-forward simulations.
            </p>
          </div>

          <div className="flex items-center gap-3">
            <select
              value={universe}
              onChange={(e) => setUniverse(e.target.value)}
              className="bg-slate-950 border border-slate-700 text-slate-200 text-sm rounded-lg px-3 py-2 focus:outline-none focus:border-cyan-500"
            >
              <option value="BENCHMARK_5">Benchmark 5 (Heavyweights)</option>
              <option value="LIVE_52">Live Universe (52 Stocks)</option>
              <option value="RESEARCH_100">Expanded Research (100 Stocks)</option>
            </select>

            <button
              onClick={handleSync10Y}
              disabled={syncing}
              className="px-4 py-2 bg-gradient-to-r from-cyan-600 to-blue-600 hover:from-cyan-500 hover:to-blue-500 text-white font-semibold text-sm rounded-lg flex items-center gap-2 shadow-lg shadow-cyan-900/30 transition disabled:opacity-50"
            >
              <RefreshCw className={`w-4 h-4 ${syncing ? 'animate-spin' : ''}`} />
              {syncing ? 'Syncing...' : 'Sync 10Y Data'}
            </button>
          </div>
        </div>

        {syncMessage && (
          <div className={`mt-4 p-3 rounded-lg text-xs font-medium ${syncMessage.type === 'success' ? 'bg-emerald-950/80 border border-emerald-500/30 text-emerald-300' : 'bg-rose-950/80 border border-rose-500/30 text-rose-300'}`}>
            {syncMessage.text}
          </div>
        )}
      </div>

      {/* Survivorship Bias & Data Provenance Disclosure */}
      <div className="bg-amber-950/30 border border-amber-500/30 rounded-xl p-4 flex items-start gap-3">
        <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" />
        <div className="text-xs text-amber-200/90 leading-relaxed">
          <span className="font-bold text-amber-300">Survivorship Bias Disclosure: </span>
          {coverageData?.survivorship_bias || "MODERATE — Uses current constituent lists retrospectively."}
          <span className="block text-amber-400/80 mt-1 font-mono">
            Scientific Guarantee: Historical VADER news and NSE Option Chains for past years are marked as 'HISTORICAL DATA UNAVAILABLE' with 0 mock fabrication.
          </span>
        </div>
      </div>

      {/* Coverage Statistics Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="flex items-center justify-between text-slate-400 text-xs font-medium">
            <span>Universe Size</span>
            <Layers className="w-4 h-4 text-cyan-400" />
          </div>
          <div className="text-2xl font-bold text-white mt-2">
            {coverageData?.daily_coverage?.universe_size || 0} Stocks
          </div>
          <div className="text-xs text-slate-500 mt-1">
            {coverageData?.daily_coverage?.stocks_with_10y || 0} stocks with full 10-year depth
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="flex items-center justify-between text-slate-400 text-xs font-medium">
            <span>10-Year Coverage</span>
            <ShieldCheck className="w-4 h-4 text-emerald-400" />
          </div>
          <div className="text-2xl font-bold text-emerald-400 mt-2">
            {coverageData?.daily_coverage?.coverage_pct_10y || 0}%
          </div>
          <div className="text-xs text-slate-500 mt-1">
            Cutoff: {coverageData?.daily_coverage?.ten_year_reference_date || "2016-08"}
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="flex items-center justify-between text-slate-400 text-xs font-medium">
            <span>Total Daily Bars</span>
            <BarChart2 className="w-4 h-4 text-indigo-400" />
          </div>
          <div className="text-2xl font-bold text-white mt-2">
            {(coverageData?.daily_coverage?.total_daily_bars_stored || 0).toLocaleString()}
          </div>
          <div className="text-xs text-slate-500 mt-1">
            Indexed SQLite WAL storage
          </div>
        </div>

        <div className="bg-slate-900 border border-slate-800 rounded-xl p-4">
          <div className="flex items-center justify-between text-slate-400 text-xs font-medium">
            <span>Accumulated 15m Feeds</span>
            <Activity className="w-4 h-4 text-purple-400" />
          </div>
          <div className="text-2xl font-bold text-purple-400 mt-2">
            {(coverageData?.intraday_accumulated?.total_15m_candles || 0).toLocaleString()}
          </div>
          <div className="text-xs text-slate-500 mt-1">
            Across {coverageData?.intraday_accumulated?.distinct_tickers || 0} tickers ({coverageData?.intraday_accumulated?.calendar_days_span || 0} days span)
          </div>
        </div>
      </div>

      {/* Multi-Stock Portfolio Walk-Forward Simulator Card */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-xl">
        <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 pb-4 border-b border-slate-800">
          <div>
            <div className="flex items-center gap-2">
              <Compass className="w-5 h-5 text-cyan-400" />
              <h2 className="text-lg font-bold text-white">Multi-Stock Portfolio Walk-Forward Backtester</h2>
            </div>
            <p className="text-xs text-slate-400 mt-1">
              Synchronous cross-sectional walk-forward simulation across {universe} with shared capital and Portfolio Heat capping.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <div>
              <label className="text-[10px] uppercase font-bold text-slate-400 block mb-1">Capital (₹)</label>
              <input
                type="number"
                value={capital}
                onChange={(e) => setCapital(e.target.value)}
                className="w-28 bg-slate-950 border border-slate-700 text-white text-xs rounded px-2 py-1.5 focus:outline-none focus:border-cyan-500"
              />
            </div>
            <div>
              <label className="text-[10px] uppercase font-bold text-slate-400 block mb-1">Heat Cap (%)</label>
              <input
                type="number"
                step="0.5"
                value={heatCap}
                onChange={(e) => setHeatCap(e.target.value)}
                className="w-20 bg-slate-950 border border-slate-700 text-white text-xs rounded px-2 py-1.5 focus:outline-none focus:border-cyan-500"
              />
            </div>
            <div>
              <label className="text-[10px] uppercase font-bold text-slate-400 block mb-1">Kelly Mode</label>
              <select
                value={kellyFraction}
                onChange={(e) => setKellyFraction(e.target.value)}
                className="bg-slate-950 border border-slate-700 text-white text-xs rounded px-2 py-1.5 focus:outline-none focus:border-cyan-500"
              >
                <option value="HALF">Half Kelly (Optimal)</option>
                <option value="QUARTER">Quarter Kelly (Safe)</option>
                <option value="FULL">Full Kelly (Aggressive)</option>
              </select>
            </div>
            <button
              onClick={handleRunPortfolioBacktest}
              disabled={portfolioRunning}
              className="mt-4 md:mt-0 px-4 py-2 bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 text-white font-bold text-xs rounded-lg flex items-center gap-2 shadow-lg shadow-emerald-900/30 transition disabled:opacity-50"
            >
              <Play className={`w-3.5 h-3.5 ${portfolioRunning ? 'animate-spin' : ''}`} />
              {portfolioRunning ? 'Simulating...' : 'Run Portfolio Walk-Forward'}
            </button>
          </div>
        </div>

        {portfolioError && (
          <div className="mt-4 p-3 bg-rose-950/80 border border-rose-500/30 rounded-lg text-xs text-rose-300">
            ❌ {portfolioError}
          </div>
        )}

        {portfolioResults && (
          <div className="mt-6 space-y-6">
            <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
              <div className="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <span className="text-[10px] uppercase font-bold text-slate-400">Total Net P&L</span>
                <div className={`text-lg font-black mt-1 ${portfolioResults.metrics?.total_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                  ₹{portfolioResults.metrics?.total_pnl?.toLocaleString()}
                </div>
              </div>
              <div className="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <span className="text-[10px] uppercase font-bold text-slate-400">Win Rate</span>
                <div className="text-lg font-black text-white mt-1">
                  {portfolioResults.metrics?.win_rate}%
                </div>
              </div>
              <div className="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <span className="text-[10px] uppercase font-bold text-slate-400">Profit Factor</span>
                <div className="text-lg font-black text-white mt-1">
                  {portfolioResults.metrics?.profit_factor}
                </div>
              </div>
              <div className="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <span className="text-[10px] uppercase font-bold text-slate-400">Max Drawdown</span>
                <div className="text-lg font-black text-rose-400 mt-1">
                  {portfolioResults.metrics?.max_drawdown}%
                </div>
              </div>
              <div className="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <span className="text-[10px] uppercase font-bold text-slate-400">Sharpe Ratio</span>
                <div className="text-lg font-black text-white mt-1">
                  {portfolioResults.metrics?.sharpe_ratio}
                </div>
              </div>
              <div className="bg-slate-950 border border-slate-800 rounded-lg p-3">
                <span className="text-[10px] uppercase font-bold text-slate-400">Total Trades</span>
                <div className="text-lg font-black text-white mt-1">
                  {portfolioResults.metrics?.total_trades}
                </div>
              </div>
            </div>

            {/* Model Lifecycle & Locked Holdout */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="bg-slate-950 border border-slate-800 rounded-lg p-4">
                <span className="text-xs font-bold text-cyan-400 uppercase tracking-wider block mb-2">
                  Champion / Challenger Retraining Lifecycle
                </span>
                <div className="space-y-1.5 text-xs text-slate-300">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Total Weekly Retrain Cycles:</span>
                    <span className="font-mono font-bold text-white">{portfolioResults.champion_challenger_lifecycle?.total_weekly_cycles}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Challenger Promotions:</span>
                    <span className="font-mono font-bold text-emerald-400">{portfolioResults.champion_challenger_lifecycle?.promotions}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Champion Retentions:</span>
                    <span className="font-mono font-bold text-slate-400">{portfolioResults.champion_challenger_lifecycle?.retentions}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Final Active Version:</span>
                    <span className="font-mono font-bold text-cyan-400">{portfolioResults.champion_challenger_lifecycle?.active_champion_version}</span>
                  </div>
                </div>
              </div>

              <div className="bg-slate-950 border border-slate-800 rounded-lg p-4">
                <span className="text-xs font-bold text-purple-400 uppercase tracking-wider block mb-2">
                  Locked Final OOS Holdout Benchmark (Final 15%)
                </span>
                <div className="space-y-1.5 text-xs text-slate-300">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Holdout Bars:</span>
                    <span className="font-mono font-bold text-white">{portfolioResults.locked_final_holdout?.holdout_samples} bars</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Holdout Trades:</span>
                    <span className="font-mono font-bold text-white">{portfolioResults.locked_final_holdout?.total_trades}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Holdout Win Rate:</span>
                    <span className="font-mono font-bold text-white">{portfolioResults.locked_final_holdout?.win_rate_pct}%</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Holdout Net P&L:</span>
                    <span className={`font-mono font-bold ${portfolioResults.locked_final_holdout?.net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                      ₹{portfolioResults.locked_final_holdout?.net_pnl?.toLocaleString()}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Ticker Quality Audit Table */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden shadow-xl">
        <div className="p-4 border-b border-slate-800 flex flex-col md:flex-row justify-between items-start md:items-center gap-3">
          <div>
            <h3 className="text-sm font-bold text-white flex items-center gap-2">
              <Database className="w-4 h-4 text-cyan-400" />
              Historical Data Coverage Audit ({filteredTickers.length} Tickers)
            </h3>
          </div>
          <input
            type="text"
            placeholder="Search ticker..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full md:w-64 bg-slate-950 border border-slate-700 text-white text-xs rounded-lg px-3 py-1.5 focus:outline-none focus:border-cyan-500"
          />
        </div>

        <div className="overflow-x-auto max-h-96">
          <table className="w-full text-left text-xs text-slate-300">
            <thead className="bg-slate-950 text-slate-400 font-mono uppercase text-[10px] sticky top-0 border-b border-slate-800">
              <tr>
                <th className="py-2.5 px-4">Ticker</th>
                <th className="py-2.5 px-4">First Available</th>
                <th className="py-2.5 px-4">Latest Date</th>
                <th className="py-2.5 px-4">Total Daily Bars</th>
                <th className="py-2.5 px-4">10Y Depth</th>
                <th className="py-2.5 px-4">Quality Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-mono">
              {filteredTickers.map((t, idx) => (
                <tr key={idx} className="hover:bg-slate-800/40 transition">
                  <td className="py-2.5 px-4 font-bold text-white">{t.ticker}</td>
                  <td className="py-2.5 px-4 text-slate-400">{t.first_available_date}</td>
                  <td className="py-2.5 px-4 text-slate-400">{t.last_available_date}</td>
                  <td className="py-2.5 px-4 text-white font-bold">{t.total_daily_bars.toLocaleString()}</td>
                  <td className="py-2.5 px-4">
                    {t.has_10y_history ? (
                      <span className="px-2 py-0.5 bg-emerald-950 border border-emerald-500/30 text-emerald-400 rounded text-[10px] font-bold">
                        10Y READY
                      </span>
                    ) : (
                      <span className="px-2 py-0.5 bg-slate-800 text-slate-400 rounded text-[10px]">
                        &lt;10Y
                      </span>
                    )}
                  </td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${t.quality_status === 'VALID' ? 'bg-cyan-950 text-cyan-400 border border-cyan-500/30' : 'bg-rose-950 text-rose-400 border border-rose-500/30'}`}>
                      {t.quality_status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

