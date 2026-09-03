import React, { useState, useEffect } from 'react';
import { Database, HardDrive, FileText, Clock, Table } from 'lucide-react';
import axios from 'axios';
import { API_BASE } from '../services/api';

export default function DataDump() {
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  
  const [reportTicker, setReportTicker] = useState('RELIANCE.NS');
  const [reportData, setReportData] = useState([]);
  const [reportLoading, setReportLoading] = useState(false);
  
  const fetchReport = async () => {
    if (!reportTicker) return;
    setReportLoading(true);
    try {
      const res = await axios.get(`${API_BASE}/ml/report/${reportTicker}`);
      if (res.data.status === 'success') {
        setReportData(res.data.data);
      } else {
        alert(res.data.message);
      }
    } catch(err) {
      alert("Failed to fetch report");
    } finally {
      setReportLoading(false);
    }
  };

  useEffect(() => {
    const fetchStats = async () => {
      try {
        const response = await axios.get(`${API_BASE}/market/dump-stats`);
        setStats(response.data);
      } catch (err) {
        setError("Failed to fetch database dump statistics.");
      } finally {
        setLoading(false);
      }
    };
    fetchStats();
  }, []);

  return (
    <div className="max-w-6xl mx-auto max-w-full">
      <div className="mb-6 sm:mb-8">
        <h1 className="text-2xl sm:text-3xl font-bold text-gray-800 flex items-center">
          <Database className="text-blue-600 mr-2 sm:mr-3 shrink-0" size={28} />
          Local Database Dump
        </h1>
        <p className="text-gray-500 mt-2 text-xs sm:text-sm max-w-3xl">
          View raw statistics and data blocks securely cached in your local SQLite engine. This fool-proof cache powers all your backtests and AI scans without duplicate network calls.
        </p>
      </div>

      {loading && (
        <div className="flex justify-center items-center h-64">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
        </div>
      )}

      {error && (
        <div className="bg-red-50 text-red-600 p-4 rounded-lg border border-red-200 text-sm">
          {error}
        </div>
      )}

      {stats && stats.status === "success" && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4 sm:gap-6">
            <div className="bg-white p-5 sm:p-6 rounded-xl shadow-sm border border-gray-100 flex flex-col justify-center">
              <p className="text-xs sm:text-sm text-gray-500 font-bold uppercase tracking-wider mb-2 flex items-center">
                <HardDrive size={16} className="mr-2 shrink-0" /> Cache Size
              </p>
              <p className="text-2xl sm:text-3xl font-black text-gray-800">{stats.db_size_mb} <span className="text-base text-gray-500 font-normal">MB</span></p>
            </div>
            
            <div className="bg-white p-5 sm:p-6 rounded-xl shadow-sm border border-gray-100 flex flex-col justify-center">
              <p className="text-xs sm:text-sm text-gray-500 font-bold uppercase tracking-wider mb-2 flex items-center">
                <Table size={16} className="mr-2 text-indigo-500 shrink-0" /> OHLCV Rows
              </p>
              <p className="text-2xl sm:text-3xl font-black text-gray-800">{stats.total_rows.toLocaleString()}</p>
            </div>

            <div className="bg-white p-5 sm:p-6 rounded-xl shadow-sm border border-purple-100 flex flex-col justify-center relative overflow-hidden">
              <div className="absolute top-0 right-0 p-3 opacity-10">
                <Database size={64} className="text-purple-600" />
              </div>
              <p className="text-xs sm:text-sm text-purple-600 font-bold uppercase tracking-wider mb-2 z-10 flex items-center">
                <Database size={16} className="mr-2 shrink-0" /> AI Memory Rows
              </p>
              <p className="text-2xl sm:text-3xl font-black text-purple-900 z-10">{stats.ml_training_rows?.toLocaleString()}</p>
            </div>

            <div className="bg-white p-5 sm:p-6 rounded-xl shadow-sm border border-pink-100 flex flex-col justify-center relative overflow-hidden">
              <p className="text-xs sm:text-sm text-pink-600 font-bold uppercase tracking-wider mb-2 z-10">
                AI Trades Logged
              </p>
              <p className="text-2xl sm:text-3xl font-black text-pink-900 z-10">{stats.ml_trade_count}</p>
            </div>
          </div>

          <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
            <div className="px-4 sm:px-6 py-4 border-b border-gray-100 bg-gray-50 flex items-center justify-between">
              <h3 className="font-bold text-gray-800 flex items-center text-sm sm:text-base">
                <FileText size={18} className="mr-2 text-blue-500 shrink-0" />
                Cached Tickers Overview
              </h3>
              <span className="text-xs font-bold bg-blue-100 text-blue-800 px-3 py-1 rounded-full">
                {stats.tickers.length} Symbols
              </span>
            </div>
            <div className="overflow-x-auto max-h-96">
              <table className="min-w-full text-sm">
                <thead className="bg-white sticky top-0 border-b border-gray-200">
                  <tr>
                    <th className="px-6 py-3 text-left font-semibold text-gray-600">Ticker Symbol</th>
                    <th className="px-6 py-3 text-right font-semibold text-gray-600">Cached Rows</th>
                    <th className="px-6 py-3 text-center font-semibold text-gray-600">Data Starts</th>
                    <th className="px-6 py-3 text-center font-semibold text-gray-600">Data Ends</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {stats.tickers.map((t, i) => (
                    <tr key={i} className="hover:bg-gray-50">
                      <td className="px-6 py-4 font-bold text-gray-800">{t.ticker}</td>
                      <td className="px-6 py-4 text-right font-medium text-blue-600">{t.rows.toLocaleString()}</td>
                      <td className="px-6 py-4 text-center text-gray-500 font-mono text-xs">{t.min_date}</td>
                      <td className="px-6 py-4 text-center text-gray-500 font-mono text-xs">{t.max_date}</td>
                    </tr>
                  ))}
                  {stats.tickers.length === 0 && (
                    <tr>
                      <td colSpan="4" className="px-6 py-8 text-center text-gray-500">No data cached yet. Run a backtest or AI scan!</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden mt-6">
            <div className="px-4 sm:px-6 py-4 border-b border-gray-100 bg-gray-50 flex items-center">
              <h3 className="font-bold text-gray-800 flex items-center text-sm sm:text-base">
                <Clock size={18} className="mr-2 text-indigo-500 shrink-0" />
                Foolproof Block Fetch Log (Recent 100)
              </h3>
            </div>
            <div className="overflow-x-auto max-h-80">
              <table className="min-w-full text-sm">
                <thead className="bg-white sticky top-0 border-b border-gray-200">
                  <tr>
                    <th className="px-6 py-3 text-left font-semibold text-gray-600">Ticker</th>
                    <th className="px-6 py-3 text-center font-semibold text-gray-600">Block Start Date</th>
                    <th className="px-6 py-3 text-center font-semibold text-gray-600">Block End Date</th>
                    <th className="px-6 py-3 text-center font-semibold text-gray-600">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {stats.fetch_logs.map((log, i) => (
                    <tr key={i} className="hover:bg-gray-50">
                      <td className="px-6 py-3 font-bold text-gray-700">{log.ticker}</td>
                      <td className="px-6 py-3 text-center text-gray-500 font-mono text-xs">{log.start_date}</td>
                      <td className="px-6 py-3 text-center text-gray-500 font-mono text-xs">{log.end_date}</td>
                      <td className="px-6 py-3 text-center">
                        <span className="bg-green-100 text-green-700 px-2 py-1 rounded text-xs font-bold">CACHED</span>
                      </td>
                    </tr>
                  ))}
                  {stats.fetch_logs.length === 0 && (
                    <tr>
                      <td colSpan="4" className="px-6 py-8 text-center text-gray-500">No fetch logs available.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* ML Training Data Report */}
      <div className="bg-white p-4 sm:p-6 rounded-xl shadow-sm border border-gray-200 mt-8">
        <div className="flex flex-col sm:flex-row justify-between items-start sm:items-end gap-4 mb-6">
          <div>
            <h2 className="text-lg sm:text-xl font-bold text-gray-800 flex items-center">
              <Table className="mr-2 text-indigo-500 shrink-0" size={22} />
              AI Training Data Inspector
            </h2>
            <p className="text-xs sm:text-sm text-gray-500 mt-1">Audit the exact 15-minute candles, sources, and technicals hoarded by the background worker.</p>
          </div>
          <div className="flex w-full sm:w-auto space-x-2">
            <input 
              type="text" 
              value={reportTicker}
              onChange={e => setReportTicker(e.target.value.toUpperCase())}
              placeholder="e.g. TCS.NS"
              className="border border-gray-300 rounded p-2 text-sm font-bold flex-1 sm:w-36"
            />
            <button 
              onClick={fetchReport}
              disabled={reportLoading}
              className="bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2 rounded text-sm font-bold"
            >
              {reportLoading ? 'Loading...' : 'Inspect Ticker'}
            </button>
          </div>
        </div>
        
        {reportData.length > 0 ? (
          <div className="overflow-x-auto rounded-lg border border-gray-200">
            <table className="min-w-full text-sm text-left">
              <thead className="text-xs text-gray-500 uppercase bg-gray-50 border-b">
                <tr>
                  <th className="px-4 py-3">15m Timestamp</th>
                  <th className="px-4 py-3">Source Provider</th>
                  <th className="px-4 py-3">Hoard Time</th>
                  <th className="px-4 py-3">Close</th>
                  <th className="px-4 py-3">RSI (14)</th>
                  <th className="px-4 py-3">MACD</th>
                  <th className="px-4 py-3">ADX</th>
                </tr>
              </thead>
              <tbody>
                {reportData.map((row, i) => (
                  <tr key={i} className="border-b hover:bg-gray-50 font-mono">
                    <td className="px-4 py-3 font-bold text-gray-700">{row.datetime}</td>
                    <td className="px-4 py-3">
                      <span className="bg-blue-100 text-blue-800 px-2 py-1 rounded text-xs font-bold uppercase">
                        {row.source || 'Unknown'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">{row.hoard_timestamp ? new Date(row.hoard_timestamp).toLocaleString() : 'N/A'}</td>
                    <td className="px-4 py-3 text-green-700 font-bold">{typeof row.close === 'number' ? row.close.toFixed(2) : row.close}</td>
                    <td className="px-4 py-3">{typeof row.rsi === 'number' ? row.rsi.toFixed(1) : row.rsi}</td>
                    <td className="px-4 py-3">{typeof row.macd === 'number' ? row.macd.toFixed(2) : row.macd}</td>
                    <td className="px-4 py-3">{typeof row.adx === 'number' ? row.adx.toFixed(1) : row.adx}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="bg-gray-50 border border-dashed border-gray-300 rounded-lg p-10 text-center">
            <FileText className="mx-auto text-gray-400 mb-2" size={32} />
            <p className="text-gray-500 font-medium">Enter a ticker to inspect its hoarded ML data.</p>
          </div>
        )}
      </div>
    </div>
  );
}
