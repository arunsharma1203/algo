import React, { useState, useEffect } from 'react';
import axios from 'axios';
import { Network, Database, Target, BrainCircuit, Activity, BarChart2 } from 'lucide-react';

export default function MLLab() {
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await axios.get('http://localhost:8000/api/ml/lab-stats');
        setStats(res.data);
      } catch (e) {
        console.error(e);
        setError(e.message || "Failed to fetch stats");
      } finally {
        setLoading(false);
      }
    };
    fetchStats();
  }, []);

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="animate-spin h-8 w-8 rounded-full border-4 border-t-transparent border-indigo-600"></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-64 items-center justify-center text-red-500 font-bold">
        Error: {error}
      </div>
    );
  }

  if (!stats) return null;

  return (
    <div className="max-w-6xl mx-auto space-y-8 pb-12 animate-fade-in">
      <div>
        <h1 className="text-3xl font-bold text-gray-800 flex items-center">
          <Network className="text-indigo-600 mr-3" size={32} />
          AI Brain & ML Lab
        </h1>
        <p className="text-gray-500 mt-2">
          Monitor the internal health, feature weights, and historical training memory of the Ensemble Machine Learning architecture.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="bg-white rounded-xl p-6 shadow-sm border border-gray-200">
          <div className="flex items-center text-gray-500 mb-4 font-bold text-sm uppercase tracking-wider">
            <Target className="mr-2" size={18} /> Model Accuracy
          </div>
          <p className="text-5xl font-black text-gray-800">{stats.win_rate}%</p>
          <p className="text-sm text-gray-500 mt-2">Target Hit Rate (Historical)</p>
          <div className="w-full bg-gray-100 rounded-full h-2 mt-4">
            <div className="bg-green-500 h-2 rounded-full" style={{ width: `${stats.win_rate}%` }}></div>
          </div>
        </div>

        <div className="bg-white rounded-xl p-6 shadow-sm border border-gray-200">
          <div className="flex items-center text-gray-500 mb-4 font-bold text-sm uppercase tracking-wider">
            <BrainCircuit className="mr-2" size={18} /> Active Models
          </div>
          <p className="text-5xl font-black text-indigo-600">3</p>
          <div className="mt-4 flex flex-wrap gap-2">
            <span className="px-2 py-1 bg-indigo-50 text-indigo-700 text-xs font-bold rounded">RandomForest</span>
            <span className="px-2 py-1 bg-indigo-50 text-indigo-700 text-xs font-bold rounded">GradientBoost</span>
            <span className="px-2 py-1 bg-indigo-50 text-indigo-700 text-xs font-bold rounded">SVM Classifier</span>
          </div>
        </div>

        <div className="bg-white rounded-xl p-6 shadow-sm border border-gray-200">
          <div className="flex items-center text-gray-500 mb-4 font-bold text-sm uppercase tracking-wider">
            <Activity className="mr-2" size={18} /> Total Decisions
          </div>
          <p className="text-5xl font-black text-gray-800">{stats.total_closed_trades}</p>
          <p className="text-sm text-gray-500 mt-2">Historical Calls Logged</p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
          <div className="bg-gray-50 p-5 border-b border-gray-200">
            <h3 className="font-bold text-gray-800 flex items-center">
              <BarChart2 className="mr-2 text-indigo-500" size={20} /> AI Feature Importance
            </h3>
            <p className="text-xs text-gray-500 mt-1">What technical patterns the AI values most right now.</p>
          </div>
          <div className="p-6 space-y-6">
            {Object.entries(stats.feature_importance).sort((a,b) => b[1] - a[1]).map(([feature, weight]) => (
              <div key={feature}>
                <div className="flex justify-between mb-1">
                  <span className="text-sm font-bold text-gray-700">{feature.replace('_', ' ')}</span>
                  <span className="text-sm font-bold text-indigo-600">{weight}%</span>
                </div>
                <div className="w-full bg-gray-100 rounded-full h-3">
                  <div className="bg-indigo-500 h-3 rounded-full transition-all duration-1000" style={{ width: `${weight}%` }}></div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden flex flex-col">
          <div className="bg-gray-50 p-5 border-b border-gray-200">
            <h3 className="font-bold text-gray-800 flex items-center">
              <Database className="mr-2 text-indigo-500" size={20} /> Memory DB Accumulation
            </h3>
            <p className="text-xs text-gray-500 mt-1">Total 15-minute historical rows cached for ML Training.</p>
          </div>
          <div className="flex-1 overflow-y-auto max-h-[400px]">
            <table className="min-w-full text-sm">
              <thead className="bg-white border-b border-gray-100 sticky top-0">
                <tr>
                  <th className="px-6 py-3 text-left font-semibold text-gray-500">Ticker</th>
                  <th className="px-6 py-3 text-right font-semibold text-gray-500">Training Rows</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {stats.memory_stats.map((item, i) => (
                  <tr key={i} className="hover:bg-gray-50 transition">
                    <td className="px-6 py-3 font-bold text-gray-700">{item.ticker}</td>
                    <td className="px-6 py-3 text-right font-medium text-indigo-600">{item.rows.toLocaleString()}</td>
                  </tr>
                ))}
                {stats.memory_stats.length === 0 && (
                  <tr>
                    <td colSpan="2" className="px-6 py-8 text-center text-gray-400">
                      Run an Intraday ML Scan to start accumulating memory!
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
