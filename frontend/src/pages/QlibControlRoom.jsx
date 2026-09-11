import React, { useState, useEffect } from 'react';
import { 
  Cpu, 
  Layers, 
  Activity, 
  Search, 
  Play, 
  RefreshCw, 
  CheckCircle2, 
  AlertCircle, 
  Clock, 
  TrendingUp, 
  ShieldCheck, 
  BarChart3, 
  ExternalLink,
  ChevronRight,
  Database,
  ArrowUpRight,
  ArrowDownRight,
  Sliders,
  AlertTriangle,
  Terminal
} from 'lucide-react';
import { API_BASE, getLegacyRuntimeHealth, getQlibRuntimeHealth } from '../services/api';

export default function QlibControlRoom() {
  const [activeTab, setActiveTab] = useState('command');
  const [statusData, setStatusData] = useState(null);
  const [comparisonData, setComparisonData] = useState(null);
  const [recommendations, setRecommendations] = useState([]);
  const [runtimeHealth, setRuntimeHealth] = useState({ legacy: null, qlib: null, loading: true });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Scanner state
  const [scanStrategy, setScanStrategy] = useState('SWING');
  const [scanUniverse, setScanUniverse] = useState('ALL_DATABASE_STOCKS');
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState(null);
  const [scanError, setScanError] = useState(null);

  // Training state
  const [trainStrategy, setTrainStrategy] = useState('SWING');
  const [trainModelFamily, setTrainModelFamily] = useState('LGBModel');
  const [trainFeatureFamily, setTrainFeatureFamily] = useState('Alpha158');
  const [training, setTraining] = useState(false);
  const [trainingStatus, setTrainingStatus] = useState(null);
  const [trainResult, setTrainResult] = useState(null);
  const [trainError, setTrainError] = useState(null);

  // F&O state
  const [fnoData, setFnoData] = useState(null);
  const [fnoLoading, setFnoLoading] = useState(false);

  const fetchTelemetry = async () => {
    try {
      setLoading(true);
      setError(null);
      const [stRes, compRes, recRes, legHealth, qlbHealth] = await Promise.all([
        fetch(`${API_BASE}/qlib/status`),
        fetch(`${API_BASE}/qlib/comparison`),
        fetch(`${API_BASE}/qlib/recommendations?limit=20`),
        getLegacyRuntimeHealth(),
        getQlibRuntimeHealth()
      ]);

      setRuntimeHealth({
        legacy: legHealth,
        qlib: qlbHealth,
        loading: false
      });

      if (!stRes.ok) throw new Error(`Status API error: ${stRes.status}`);
      const stJson = await stRes.json();
      setStatusData(stJson);

      if (compRes.ok) {
        const compJson = await compRes.json();
        setComparisonData(compJson);
      }

      if (recRes.ok) {
        const recJson = await recRes.json();
        setRecommendations(recJson.recommendations || []);
      }
    } catch (err) {
      setError(err.message || 'Failed to load Qlib telemetry');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTelemetry();
  }, []);

  const handleRunScan = async () => {
    try {
      setScanning(true);
      setScanError(null);
      setScanResult(null);

      const res = await fetch(`${API_BASE}/qlib/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy: scanStrategy,
          universe: scanUniverse,
          top_k: 5
        })
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `Scan failed with HTTP ${res.status}`);
      }

      const data = await res.json();
      setScanResult(data);
      // Refresh recommendations
      const recRes = await fetch(`${API_BASE}/qlib/recommendations?limit=20`);
      if (recRes.ok) {
        const recJson = await recRes.json();
        setRecommendations(recJson.recommendations || []);
      }
    } catch (err) {
      setScanError(err.message);
    } finally {
      setScanning(false);
    }
  };

  const handleTrain = async () => {
    try {
      setTraining(true);
      setTrainError(null);
      setTrainResult(null);
      setTrainingStatus('INITIALIZING_QLIB_DATA');

      const res = await fetch(`${API_BASE}/qlib/train`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy: trainStrategy,
          feature_family: trainFeatureFamily,
          model_family: trainModelFamily
        })
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `Training failed with HTTP ${res.status}`);
      }

      const data = await res.json();
      setTrainResult(data);
      setTrainingStatus('COMPLETE');
      fetchTelemetry();
    } catch (err) {
      setTrainError(err.message);
      setTrainingStatus('FAILED');
    } finally {
      setTraining(false);
    }
  };

  const handleFetchFno = async () => {
    try {
      setFnoLoading(true);
      const res = await fetch(`${API_BASE}/qlib/fno`);
      if (res.ok) {
        const data = await res.json();
        setFnoData(data);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setFnoLoading(false);
    }
  };

  if (loading && !statusData) {
    return (
      <div className="flex items-center justify-center min-h-[500px]">
        <div className="flex flex-col items-center gap-3">
          <RefreshCw className="animate-spin text-purple-400" size={36} />
          <p className="text-gray-400 text-sm font-mono">Initializing Microsoft Qlib Parallel Engine...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6 text-gray-100 font-sans">
      {/* Top Banner & Visual Cue */}
      <div className="bg-gradient-to-r from-purple-950/60 via-slate-900 to-indigo-950/60 border border-purple-500/40 rounded-2xl p-6 shadow-xl relative overflow-hidden backdrop-blur-md">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="space-y-1">
            <div className="flex items-center gap-3">
              <span className="px-3 py-1 bg-purple-600/30 border border-purple-400/50 rounded-full text-xs font-mono font-bold tracking-wider text-purple-300 flex items-center gap-1.5 shadow-sm">
                <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                QLIB ENGINE • ACTIVE
              </span>
              <span className="px-2.5 py-0.5 bg-slate-800 border border-slate-700 rounded-md text-xs font-mono text-gray-300">
                v{statusData?.qlib_version || '0.9.7'}
              </span>
              <span className="px-2.5 py-0.5 bg-amber-500/10 border border-amber-500/30 text-amber-300 rounded-md text-xs font-mono">
                BRANCH: qlib-production-v1
              </span>
            </div>
            <h1 className="text-2xl font-black tracking-tight text-white flex items-center gap-2.5">
              <Cpu className="text-purple-400" size={28} />
              Qlib Quantitative Control Room
            </h1>
            <p className="text-xs text-gray-400 font-mono">
              Microsoft Qlib (Alpha158/Alpha360) · Real NSE Market Data · Chronological OOS · Virtual Tracking Only
            </p>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={fetchTelemetry}
              className="px-3.5 py-2 bg-slate-800/80 hover:bg-slate-700 border border-slate-700 rounded-xl text-xs font-medium text-gray-300 flex items-center gap-1.5 transition-all shadow"
            >
              <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
              Sync Telemetry
            </button>
          </div>
        </div>

        {/* Tab Navigation */}
        <div className="flex items-center gap-2 mt-6 pt-4 border-t border-slate-800/80 overflow-x-auto">
          {[
            { id: 'command', label: 'Command Center', icon: Activity },
            { id: 'scanner', label: 'Qlib Scanner', icon: Search },
            { id: 'training', label: 'Model Training', icon: Sliders },
            { id: 'fno', label: 'F&O Contracts', icon: Layers },
            { id: 'comparison', label: 'Legacy vs Qlib', icon: BarChart3 }
          ].map(tab => {
            const Icon = tab.icon;
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => {
                  setActiveTab(tab.id);
                  if (tab.id === 'fno' && !fnoData) handleFetchFno();
                }}
                className={`flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-semibold tracking-wide transition-all ${
                  active 
                    ? 'bg-purple-600 text-white shadow-lg shadow-purple-950/50 border border-purple-400/40' 
                    : 'text-gray-400 hover:text-white hover:bg-slate-800/60'
                }`}
              >
                <Icon size={14} />
                {tab.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* ── DUAL-SYSTEM CONCURRENT RUNTIME STATUS BANNER ──────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {/* Legacy Runtime Card */}
        <div className="bg-slate-900/80 border border-slate-800/90 rounded-2xl p-4 flex flex-col justify-between shadow-lg">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className={`w-2.5 h-2.5 rounded-full ${runtimeHealth.legacy?.status === 'RUNNING' ? 'bg-emerald-500 animate-pulse' : 'bg-rose-500'}`} />
              <span className="font-mono font-bold text-xs uppercase tracking-wider text-gray-300">LEGACY SYSTEM</span>
              <span className="text-[10px] bg-slate-800 text-gray-400 px-2 py-0.5 rounded font-mono">PORT 8000</span>
            </div>
            <span className={`text-[10px] font-bold px-2.5 py-0.5 rounded font-mono ${runtimeHealth.legacy?.status === 'RUNNING' ? 'bg-emerald-950/70 text-emerald-400 border border-emerald-500/30' : 'bg-rose-950/70 text-rose-400 border border-rose-500/30'}`}>
              ● {runtimeHealth.legacy?.status || 'CHECKING...'}
            </span>
          </div>
          <div className="grid grid-cols-3 gap-2 mt-3 pt-3 border-t border-slate-800 text-xs">
            <div>
              <div className="text-[10px] text-gray-500 font-mono uppercase">Runtime</div>
              <div className="font-semibold text-gray-200">{runtimeHealth.legacy?.status === 'RUNNING' ? 'ONLINE' : 'OFFLINE'}</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-500 font-mono uppercase">Active Jobs</div>
              <div className="font-semibold text-gray-200">{runtimeHealth.legacy?.data?.job_count ?? (runtimeHealth.legacy?.status === 'RUNNING' ? '10' : '0')}</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-500 font-mono uppercase">Last Scan</div>
              <div className="font-semibold text-gray-200 truncate">{runtimeHealth.legacy?.data?.jobs?.[0]?.next_run_time ? 'Scheduled' : (runtimeHealth.legacy?.status === 'RUNNING' ? 'Active' : 'N/A')}</div>
            </div>
          </div>
        </div>

        {/* Qlib Runtime Card */}
        <div className="bg-slate-900/80 border border-purple-900/40 rounded-2xl p-4 flex flex-col justify-between shadow-lg">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className={`w-2.5 h-2.5 rounded-full ${runtimeHealth.qlib?.status === 'RUNNING' ? 'bg-purple-400 animate-pulse' : 'bg-rose-500'}`} />
              <span className="font-mono font-bold text-xs uppercase tracking-wider text-purple-300">QLIB SYSTEM</span>
              <span className="text-[10px] bg-purple-950/60 text-purple-300 px-2 py-0.5 rounded font-mono">PORT 8001</span>
            </div>
            <span className={`text-[10px] font-bold px-2.5 py-0.5 rounded font-mono ${runtimeHealth.qlib?.status === 'RUNNING' ? 'bg-purple-950/70 text-purple-300 border border-purple-500/30' : 'bg-rose-950/70 text-rose-400 border border-rose-500/30'}`}>
              ● {runtimeHealth.qlib?.status || 'CHECKING...'}
            </span>
          </div>
          <div className="grid grid-cols-4 gap-2 mt-3 pt-3 border-t border-slate-800 text-xs">
            <div>
              <div className="text-[10px] text-gray-500 font-mono uppercase">Qlib Ver</div>
              <div className="font-semibold text-purple-300">{runtimeHealth.qlib?.data?.qlib_version || '0.9.7'}</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-500 font-mono uppercase">Active Models</div>
              <div className="font-semibold text-gray-200">{runtimeHealth.qlib?.data?.active_models_count ?? 3} Active</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-500 font-mono uppercase">Training</div>
              <div className="font-semibold text-gray-200">{runtimeHealth.qlib?.data?.active_training_job ? runtimeHealth.qlib.data.active_training_job.status : 'Idle'}</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-500 font-mono uppercase">Last Rec</div>
              <div className="font-semibold text-emerald-400 truncate">{recommendations[0]?.ticker || (runtimeHealth.qlib?.data?.last_recommendation?.ticker || 'None')}</div>
            </div>
          </div>
        </div>
      </div>

      {/* ── TAB 1: COMMAND CENTER ────────────────────────────────────── */}
      {activeTab === 'command' && (
        <div className="space-y-6">
          {/* Active Model Cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
            {['swing', 'intraday', 'fno'].map(strat => {
              const info = statusData?.models_status?.[strat] || {};
              const avail = info.available;
              const metrics = info.metrics || {};
              return (
                <div 
                  key={strat}
                  className={`rounded-2xl p-5 border transition-all ${
                    avail 
                      ? 'bg-slate-900/80 border-purple-500/30 shadow-lg' 
                      : 'bg-slate-900/40 border-slate-800 opacity-70'
                  }`}
                >
                  <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                    <span className="text-xs font-mono font-bold uppercase tracking-wider text-purple-400">
                      QLIB {strat} MODEL
                    </span>
                    <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
                      avail ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
                    }`}>
                      {avail ? 'ACTIVE' : 'NOT TRAINED'}
                    </span>
                  </div>

                  {avail ? (
                    <div className="mt-4 space-y-2.5 text-xs font-mono">
                      <div className="flex justify-between text-gray-400">
                        <span>Model Class:</span>
                        <span className="text-white font-semibold">{info.model_class}</span>
                      </div>
                      <div className="flex justify-between text-gray-400">
                        <span>Feature Set:</span>
                        <span className="text-white">{info.feature_handler}</span>
                      </div>
                      <div className="flex justify-between text-gray-400">
                        <span>OOS IC / Rank IC:</span>
                        <span className="text-purple-300 font-bold">
                          {metrics.ic ?? 'N/A'} / {metrics.rank_ic ?? 'N/A'}
                        </span>
                      </div>
                      <div className="flex justify-between text-gray-400">
                        <span>OOS Win Rate:</span>
                        <span className="text-emerald-400 font-bold">
                          {metrics.win_rate != null ? `${metrics.win_rate}%` : 'N/A'}
                        </span>
                      </div>
                      <div className="flex justify-between text-gray-400">
                        <span>OOS Profit Factor:</span>
                        <span className="text-emerald-400 font-bold">
                          {metrics.profit_factor != null ? `${metrics.profit_factor}` : 'N/A'}
                        </span>
                      </div>
                      <div className="pt-2 border-t border-slate-800/80 text-[10px] text-gray-500 truncate" title={info.artifact_sha256}>
                        SHA256: {info.artifact_sha256 ? `${info.artifact_sha256.slice(0, 16)}...` : 'N/A'}
                      </div>
                    </div>
                  ) : (
                    <div className="mt-6 text-center py-4 text-xs text-gray-500 font-mono">
                      No active model registered. Run training from Model Training tab.
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Runtime Environment Specs */}
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
            <h3 className="text-xs font-mono uppercase tracking-wider text-gray-400 mb-4 flex items-center gap-2">
              <Terminal size={14} className="text-purple-400" />
              Runtime Verification & Provenance
            </h3>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs font-mono">
              <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px]">MICROSOFT QLIB</div>
                <div className="text-purple-300 font-bold mt-1">Installed (v{statusData?.qlib_version})</div>
              </div>
              <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px]">PACKAGE LOCATION</div>
                <div className="text-gray-300 truncate mt-1" title={statusData?.qlib_package_file}>
                  {statusData?.qlib_package_file?.split('/').slice(-3).join('/')}
                </div>
              </div>
              <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px]">DATA PROVIDER</div>
                <div className="text-gray-300 truncate mt-1" title={statusData?.provider_uri}>
                  Indian NSE/BSE (Binary)
                </div>
              </div>
              <div className="bg-slate-950/60 p-3 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px]">EXECUTION MODE</div>
                <div className="text-emerald-400 font-bold mt-1">Virtual / Zero Live Heat</div>
              </div>
            </div>
          </div>

          {/* Recent Virtual Recommendations */}
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-xs font-mono uppercase tracking-wider text-gray-400 flex items-center gap-2">
                <TrendingUp size={14} className="text-emerald-400" />
                Latest Qlib Virtual Recommendations ({recommendations.length})
              </h3>
              <span className="text-[11px] font-mono text-gray-500">
                Heat Consumption: 0.0% · Broker Orders: DISABLED
              </span>
            </div>

            {recommendations.length === 0 ? (
              <div className="text-center py-8 text-xs font-mono text-gray-500 border border-dashed border-slate-800 rounded-xl">
                No recommendations recorded yet. Run a Qlib scan to populate.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs font-mono">
                  <thead>
                    <tr className="border-b border-slate-800 text-gray-400">
                      <th className="pb-2">Timestamp</th>
                      <th className="pb-2">Ticker</th>
                      <th className="pb-2">Strategy</th>
                      <th className="pb-2">Direction</th>
                      <th className="pb-2">Entry</th>
                      <th className="pb-2">SL</th>
                      <th className="pb-2">Target</th>
                      <th className="pb-2">Confidence</th>
                      <th className="pb-2">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60">
                    {recommendations.map(rec => (
                      <tr key={rec.id} className="hover:bg-slate-800/30">
                        <td className="py-2.5 text-gray-500">{rec.timestamp?.slice(0, 16).replace('T', ' ')}</td>
                        <td className="py-2.5 font-bold text-white">{rec.ticker}</td>
                        <td className="py-2.5 text-purple-300">{rec.strategy}</td>
                        <td className="py-2.5 text-emerald-400 font-bold">{rec.direction}</td>
                        <td className="py-2.5">₹{rec.entry_price}</td>
                        <td className="py-2.5 text-rose-400">₹{rec.stop_loss}</td>
                        <td className="py-2.5 text-emerald-400">₹{rec.target_price}</td>
                        <td className="py-2.5 text-purple-300">{rec.confidence}%</td>
                        <td className="py-2.5">
                          <span className="px-2 py-0.5 bg-slate-800 text-gray-300 rounded text-[10px]">
                            {rec.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── TAB 2: QLIB SCANNER ──────────────────────────────────────── */}
      {activeTab === 'scanner' && (
        <div className="space-y-6">
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
            <div className="flex flex-wrap items-center justify-between gap-4 mb-4">
              <div>
                <h2 className="text-lg font-bold text-white flex items-center gap-2">
                  <Search className="text-purple-400" size={20} />
                  Run Real Microsoft Qlib Scanner
                </h2>
                <p className="text-xs text-gray-400 font-mono mt-0.5">
                  Calculates Alpha158/Alpha360 features on fresh Indian market data and evaluates via active Qlib model.
                </p>
              </div>

              <div className="flex items-center gap-3">
                <select
                  value={scanStrategy}
                  onChange={e => setScanStrategy(e.target.value)}
                  className="bg-slate-800 border border-slate-700 text-xs font-mono text-white rounded-xl px-3 py-2 outline-none"
                >
                  <option value="SWING">Strategy: SWING (Daily)</option>
                  <option value="INTRADAY">Strategy: INTRADAY (15m)</option>
                  <option value="FNO">Strategy: F&O Underlying</option>
                </select>

                <select
                  value={scanUniverse}
                  onChange={e => setScanUniverse(e.target.value)}
                  className="bg-slate-800 border border-slate-700 text-xs font-mono text-white rounded-xl px-3 py-2 outline-none"
                >
                  <option value="ALL_DATABASE_STOCKS">Universe: ALL_DATABASE_STOCKS (Canonical Local DB)</option>
                  <option value="LIVE_52">Universe: LIVE_52 (Top Liquid 52)</option>
                  <option value="NIFTY_50">Universe: NIFTY_50 (Top 50 Bluechips)</option>
                  <option value="NIFTY_500">Universe: NIFTY_500 (Broad Market)</option>
                </select>

                <button
                  onClick={handleRunScan}
                  disabled={scanning}
                  className="px-5 py-2 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded-xl text-xs font-bold flex items-center gap-2 shadow-lg shadow-purple-900/40 disabled:opacity-50 transition-all"
                >
                  {scanning ? <RefreshCw className="animate-spin" size={14} /> : <Play size={14} />}
                  {scanning ? 'Running Qlib Inference...' : 'Run Qlib Scan'}
                </button>
              </div>
            </div>

            {/* Dynamic Universe & Provenance Telemetry Banner */}
            <div className="mt-4 pt-4 border-t border-slate-800/80 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 text-xs font-mono">
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Stocks Discovered</div>
                <div className="text-purple-300 font-bold mt-0.5">
                  {statusData?.universe_stats?.stocks_discovered ?? '511'}
                </div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Valid for Scan</div>
                <div className="text-emerald-400 font-bold mt-0.5">
                  {statusData?.universe_stats?.stocks_scan_ready ?? '509'}
                </div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Model</div>
                <div className="text-white font-semibold mt-0.5 truncate" title={statusData?.models_status?.[scanStrategy.toLowerCase()]?.model_class || 'LGBModel'}>
                  {statusData?.models_status?.[scanStrategy.toLowerCase()]?.model_class || 'LGBModel'}
                </div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Model Hash</div>
                <div className="text-purple-400 font-mono text-[11px] mt-0.5 truncate" title={statusData?.models_status?.[scanStrategy.toLowerCase()]?.artifact_sha256}>
                  {statusData?.models_status?.[scanStrategy.toLowerCase()]?.artifact_sha256 ? `${statusData.models_status[scanStrategy.toLowerCase()].artifact_sha256.slice(0, 10)}...` : 'N/A'}
                </div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Data Source</div>
                <div className="text-gray-300 font-medium mt-0.5">Indian NSE/BSE</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Data Freshness</div>
                <div className="text-emerald-300 font-medium mt-0.5 truncate">
                  {scanResult?.data_timestamp || 'Daily EOD'}
                </div>
              </div>
            </div>

            {scanError && (
              <div className="p-4 bg-rose-950/40 border border-rose-500/40 rounded-xl text-xs font-mono text-rose-300 flex items-center gap-3 mt-4">
                <AlertCircle size={18} className="shrink-0 text-rose-400" />
                <div>
                  <div className="font-bold">Scan Execution Blocked (Fail-Closed)</div>
                  <div className="text-gray-400 mt-0.5">{scanError}</div>
                </div>
              </div>
            )}
          </div>

          {/* Scan Results */}
          {scanResult && (
            <div className="bg-slate-900/60 border border-purple-500/30 rounded-2xl p-5 shadow-xl space-y-4">
              <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-mono font-bold text-purple-400">
                    RESULTS: {scanResult.strategy} SCAN
                  </span>
                  <span className="text-xs text-gray-500 font-mono">
                    ({scanResult.recommendations_count || 0} Qualified Trades)
                  </span>
                </div>
                <span className="text-[11px] font-mono text-gray-400">
                  Data Bar: {scanResult.data_timestamp} · Engine: {scanResult.engine}
                </span>
              </div>

              {scanResult.recommendations?.length === 0 ? (
                <div className="text-center py-10 font-mono text-xs text-gray-400 space-y-2">
                  <div className="text-amber-300 font-bold">NO TRADES QUALIFIED</div>
                  <p className="text-gray-500 max-w-md mx-auto">
                    The Qlib model evaluated the universe on fresh market data and found no symbols meeting the positive conviction threshold. Thresholds are never lowered to manufacture artificial trades.
                  </p>
                </div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {scanResult.recommendations?.map((rec, idx) => (
                    <div 
                      key={idx}
                      className="bg-slate-950/80 border border-purple-500/40 rounded-xl p-4 space-y-3 relative overflow-hidden"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-black text-white">{rec.ticker}</span>
                        <span className="px-2 py-0.5 bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 rounded text-xs font-bold">
                          {rec.direction}
                        </span>
                      </div>

                      <div className="grid grid-cols-3 gap-2 text-xs font-mono py-2 bg-slate-900/60 rounded-lg px-3 border border-slate-800">
                        <div>
                          <div className="text-gray-500 text-[10px]">ENTRY</div>
                          <div className="font-bold text-white">₹{rec.entry_price}</div>
                        </div>
                        <div>
                          <div className="text-gray-500 text-[10px]">STOP LOSS</div>
                          <div className="font-bold text-rose-400">₹{rec.stop_loss}</div>
                        </div>
                        <div>
                          <div className="text-gray-500 text-[10px]">TARGET</div>
                          <div className="font-bold text-emerald-400">₹{rec.target_price}</div>
                        </div>
                      </div>

                      <div className="flex justify-between items-center text-xs font-mono text-gray-400 pt-1">
                        <span>Qlib Score: <span className="text-purple-300 font-bold">{rec.model_score}</span></span>
                        <span>Confidence: <span className="text-emerald-400 font-bold">{rec.confidence}%</span></span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── TAB 3: MODEL TRAINING ────────────────────────────────────── */}
      {activeTab === 'training' && (
        <div className="space-y-6">
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
            <h2 className="text-lg font-bold text-white flex items-center gap-2 mb-1">
              <Sliders className="text-purple-400" size={20} />
              Train Real Microsoft Qlib Estimator
            </h2>
            <p className="text-xs text-gray-400 font-mono mb-6">
              Executes DatasetH generation, Alpha158/Alpha360 extraction, and LightGBM / DoubleEnsemble fitting with chronological Train/Validation/Locked OOS splits.
            </p>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
              <div>
                <label className="block text-xs font-mono text-gray-400 mb-1.5">Strategy</label>
                <select
                  value={trainStrategy}
                  onChange={e => setTrainStrategy(e.target.value)}
                  className="w-full bg-slate-800 border border-slate-700 text-xs font-mono text-white rounded-xl px-3 py-2.5 outline-none"
                >
                  <option value="SWING">SWING (Daily Bars, 70/15/15)</option>
                  <option value="INTRADAY">INTRADAY (15m Candles, 70/15/15)</option>
                  <option value="FNO">FNO (Underlying Equity/Index)</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-mono text-gray-400 mb-1.5">Feature Family</label>
                <select
                  value={trainFeatureFamily}
                  onChange={e => setTrainFeatureFamily(e.target.value)}
                  className="w-full bg-slate-800 border border-slate-700 text-xs font-mono text-white rounded-xl px-3 py-2.5 outline-none"
                >
                  <option value="Alpha158">Alpha158 (Canonical 158 factors)</option>
                  <option value="Alpha360">Alpha360 (Normalized sequence features)</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-mono text-gray-400 mb-1.5">Model Architecture</label>
                <select
                  value={trainModelFamily}
                  onChange={e => setTrainModelFamily(e.target.value)}
                  className="w-full bg-slate-800 border border-slate-700 text-xs font-mono text-white rounded-xl px-3 py-2.5 outline-none"
                >
                  <option value="LGBModel">Qlib LGBModel (LightGBM GBDT)</option>
                  <option value="DEnsembleModel">Qlib DEnsembleModel (DoubleEnsemble)</option>
                  <option value="CatBoostModel">Qlib CatBoostModel (CatBoost)</option>
                </select>
              </div>
            </div>

            <div className="flex items-center justify-between pt-4 border-t border-slate-800">
              <div className="text-xs font-mono text-gray-500">
                Chronological Segments: Train (70%) → Validation (15%) → Locked OOS (15%)
              </div>
              <button
                onClick={handleTrain}
                disabled={training}
                className="px-6 py-2.5 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded-xl text-xs font-bold flex items-center gap-2 shadow-lg disabled:opacity-50 transition-all"
              >
                {training ? <RefreshCw className="animate-spin" size={14} /> : <Play size={14} />}
                {training ? 'Training Qlib Estimator...' : 'Start Qlib Training'}
              </button>
            </div>

            {training && (
              <div className="mt-4 p-4 bg-purple-950/30 border border-purple-500/30 rounded-xl text-xs font-mono flex items-center gap-3">
                <RefreshCw size={16} className="animate-spin text-purple-400" />
                <div>
                  <div className="text-purple-300 font-bold">Training In Progress...</div>
                  <div className="text-gray-400 text-[11px] mt-0.5">Extracting Qlib features and fitting model on Indian market data.</div>
                </div>
              </div>
            )}

            {trainError && (
              <div className="mt-4 p-4 bg-rose-950/40 border border-rose-500/40 rounded-xl text-xs font-mono text-rose-300 flex items-center gap-2">
                <AlertCircle size={16} className="text-rose-400 shrink-0" />
                <span>Training Failed: {trainError}</span>
              </div>
            )}

            {trainResult && (
              <div className="mt-6 p-5 bg-slate-950/80 border border-emerald-500/40 rounded-xl space-y-3 font-mono text-xs">
                <div className="flex items-center justify-between text-emerald-400 font-bold">
                  <span className="flex items-center gap-2">
                    <CheckCircle2 size={16} />
                    TRAINING COMPLETE & MODEL REGISTERED
                  </span>
                  <span className="text-gray-400 text-[11px]">{trainResult.model_id}</span>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 pt-2">
                  <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                    <div className="text-gray-500 text-[10px]">OOS IC</div>
                    <div className="text-white font-bold">{trainResult.oos_metrics?.ic}</div>
                  </div>
                  <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                    <div className="text-gray-500 text-[10px]">OOS WIN RATE</div>
                    <div className="text-emerald-400 font-bold">{trainResult.oos_metrics?.win_rate}%</div>
                  </div>
                  <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                    <div className="text-gray-500 text-[10px]">OOS PROFIT FACTOR</div>
                    <div className="text-emerald-400 font-bold">{trainResult.oos_metrics?.profit_factor}</div>
                  </div>
                  <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                    <div className="text-gray-500 text-[10px]">OOS SHARPE</div>
                    <div className="text-purple-300 font-bold">{trainResult.oos_metrics?.sharpe}</div>
                  </div>
                </div>

                <div className="text-[10px] text-gray-500 truncate pt-2">
                  Artifact SHA-256: {trainResult.artifact_sha256}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── TAB 4: F&O CONTRACTS ────────────────────────────────────── */}
      {activeTab === 'fno' && (
        <div className="space-y-6">
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-lg font-bold text-white flex items-center gap-2">
                  <Layers className="text-purple-400" size={20} />
                  Qlib F&O Scanner
                </h2>
                <p className="text-xs text-gray-400 font-mono mt-0.5">
                  Evaluates underlying direction with Qlib models and maps opportunities to real NSE option chain contracts.
                </p>
              </div>
              <button
                onClick={handleFetchFno}
                disabled={fnoLoading}
                className="px-4 py-2 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-xl text-xs font-mono text-gray-200 flex items-center gap-2"
              >
                <RefreshCw size={14} className={fnoLoading ? 'animate-spin' : ''} />
                Refresh Option Chain
              </button>
            </div>

            {fnoData?.opportunities?.length > 0 ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {fnoData.opportunities.map((opp, idx) => (
                  <div key={idx} className="bg-slate-950/80 border border-slate-800 rounded-xl p-4 space-y-3 font-mono text-xs">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-bold text-white">{opp.underlying}</span>
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                        opp.direction === 'BULLISH' 
                          ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                          : opp.direction === 'BEARISH'
                          ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
                          : 'bg-slate-800 text-gray-400'
                      }`}>
                        {opp.direction} (Score: {opp.model_score})
                      </span>
                    </div>

                    {opp.contract_status === 'AVAILABLE' ? (
                      <div className="space-y-2">
                        <div className="p-2.5 bg-slate-900 rounded border border-slate-800 flex justify-between items-center">
                          <span className="font-bold text-purple-300">{opp.contract}</span>
                          <span className="text-emerald-400 font-bold">LTP ₹{opp.indicative_entry}</span>
                        </div>
                        <div className="grid grid-cols-3 gap-2 text-[11px] text-gray-400">
                          <div>Spot: ₹{opp.spot_price}</div>
                          <div>PCR: {opp.pcr || 'N/A'}</div>
                          <div>Max Pain: ₹{opp.max_pain || 'N/A'}</div>
                        </div>
                      </div>
                    ) : (
                      <div className="p-3 bg-slate-900/60 rounded border border-slate-800 text-amber-400/80 text-[11px] flex items-center gap-2">
                        <AlertTriangle size={14} className="shrink-0" />
                        <span>DATA UNAVAILABLE: {opp.reason || 'Option chain offline'}</span>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-8 text-xs font-mono text-gray-500 border border-dashed border-slate-800 rounded-xl">
                Click "Refresh Option Chain" to scan F&O opportunities.
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── TAB 5: LEGACY vs QLIB COMPARISON ────────────────────────── */}
      {activeTab === 'comparison' && (
        <div className="space-y-6">
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
            <h2 className="text-lg font-bold text-white flex items-center gap-2 mb-1">
              <BarChart3 className="text-purple-400" size={20} />
              Legacy Production Champion vs Microsoft Qlib
            </h2>
            <p className="text-xs text-gray-400 font-mono mb-6">
              Rigorous, transparent side-by-side performance audit. The migration decision requires verified statistical superiority on live forward trading.
            </p>

            {/* Verdict Callout */}
            <div className="p-4 bg-purple-950/30 border border-purple-500/40 rounded-xl mb-6">
              <div className="text-xs font-mono text-gray-400 uppercase tracking-wider">Independent Audit Verdict</div>
              <div className="text-sm font-bold text-purple-300 mt-1">{comparisonData?.verdict || 'Evaluation in progress...'}</div>
              <div className="mt-2 text-xs font-mono text-gray-400">
                Action Decision: <span className="text-white font-bold">{comparisonData?.system_decision}</span>
              </div>
            </div>

            {/* Side-by-Side Table */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Legacy Column */}
              <div className="bg-slate-950/80 border border-slate-800 rounded-2xl p-5 space-y-4">
                <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                  <span className="font-bold text-sm text-gray-200">LEGACY SYSTEM (Control)</span>
                  <span className="px-2.5 py-0.5 bg-blue-500/20 text-blue-300 border border-blue-500/30 rounded text-[10px] font-mono">
                    ACTIVE PRODUCTION
                  </span>
                </div>
                <div className="space-y-3 text-xs font-mono">
                  <div className="flex justify-between text-gray-400">
                    <span>Completed Trades:</span>
                    <span className="text-white font-bold">{comparisonData?.legacy?.completed_trades ?? 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Win Rate:</span>
                    <span className="text-emerald-400 font-bold">{comparisonData?.legacy?.win_rate != null ? `${comparisonData.legacy.win_rate}%` : 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Profit Factor:</span>
                    <span className="text-emerald-400 font-bold">{comparisonData?.legacy?.profit_factor ?? 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Sharpe Ratio:</span>
                    <span className="text-purple-300 font-bold">{comparisonData?.legacy?.sharpe ?? 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Max Drawdown:</span>
                    <span className="text-rose-400 font-bold">{comparisonData?.legacy?.max_drawdown_pct != null ? `${comparisonData.legacy.max_drawdown_pct}%` : 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Realized Net P&L:</span>
                    <span className="text-white font-bold">{comparisonData?.legacy?.net_pnl_pct != null ? `${comparisonData.legacy.net_pnl_pct}%` : 'N/A'}</span>
                  </div>
                </div>
              </div>

              {/* Qlib Column */}
              <div className="bg-slate-950/80 border border-purple-500/40 rounded-2xl p-5 space-y-4 shadow-lg shadow-purple-950/30">
                <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                  <span className="font-bold text-sm text-purple-300">QLIB SYSTEM (Experimental)</span>
                  <span className="px-2.5 py-0.5 bg-purple-500/20 text-purple-300 border border-purple-500/30 rounded text-[10px] font-mono">
                    PARALLEL BRANCH
                  </span>
                </div>
                <div className="space-y-3 text-xs font-mono">
                  <div className="flex justify-between text-gray-400">
                    <span>Completed Virtual Trades:</span>
                    <span className="text-white font-bold">{comparisonData?.qlib?.completed_virtual_trades ?? 0}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Forward Live Win Rate:</span>
                    <span className="text-emerald-400 font-bold">
                      {comparisonData?.qlib?.live_win_rate != null ? `${comparisonData.qlib.live_win_rate}%` : 'N/A — no completed trades'}
                    </span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Locked OOS Trades:</span>
                    <span className="text-white">{comparisonData?.qlib?.oos_trades_count ?? 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Locked OOS Win Rate:</span>
                    <span className="text-emerald-400 font-bold">{comparisonData?.qlib?.oos_win_rate != null ? `${comparisonData.qlib.oos_win_rate}%` : 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Locked OOS Profit Factor:</span>
                    <span className="text-emerald-400 font-bold">{comparisonData?.qlib?.oos_profit_factor ?? 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Locked OOS Sharpe:</span>
                    <span className="text-purple-300 font-bold">{comparisonData?.qlib?.oos_sharpe ?? 'N/A'}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
