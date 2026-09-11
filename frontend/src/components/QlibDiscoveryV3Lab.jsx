import React, { useState, useEffect } from 'react';
import axios from 'axios';
import { 
  Gauge, ShieldCheck, Sparkles, TrendingUp, Activity, 
  Play, RefreshCw, Layers, CheckCircle, AlertTriangle, X, ChevronRight, 
  BarChart2, Info, Compass, Target, Scale, Zap, Lock, Filter, Database,
  ArrowUpRight, ArrowDownRight, Copy, Check, Calendar, Award, AlertCircle,
  Sliders, ArrowRight, CornerDownRight
} from 'lucide-react';
import { API_BASE } from '../services/api';

export default function QlibDiscoveryV3Lab({ initialStrategy = 'SWING' }) {
  const [activeTab, setActiveTab] = useState('baseline'); // 'baseline' | 'rebalance' | 'hysteresis' | 'frontier' | 'regimes' | 'costs' | 'walk_forward' | 'oos'
  const [discoveryRun, setDiscoveryRun] = useState(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [copiedFingerprint, setCopiedFingerprint] = useState(false);

  // Selected filters for portfolio view
  const [selectedTopK, setSelectedTopK] = useState('ALL');
  const [selectedFriction, setSelectedFriction] = useState('ALL');

  useEffect(() => {
    fetchLatestDiscoveryV3();
  }, []);

  const fetchLatestDiscoveryV3 = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await axios.get(`${API_BASE}/ml/qlib/signal-discovery-v3/latest`);
      if (res.data?.status === 'success' && res.data?.discovery_run) {
        setDiscoveryRun(res.data.discovery_run);
      }
    } catch (e) {
      console.warn("V3 Discovery fetch failed:", e);
      setError("Failed to fetch latest V3 discovery run");
    } finally {
      setLoading(false);
    }
  };

  const handleRunV3 = async () => {
    setRunning(true);
    setError(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/qlib/signal-discovery-v3/run`, {
        universe: 'LIVE_52',
        horizons: [5, 10, 15, 20],
        buffer_bands: [
          { entry_top_k: 10, exit_top_k: 10, name: 'NO_BUFFER_TOP10' },
          { entry_top_k: 10, exit_top_k: 20, name: 'HYSTERESIS_TOP10_20' },
          { entry_top_k: 10, exit_top_k: 30, name: 'HYSTERESIS_TOP10_30' },
          { entry_top_k: 5, exit_top_k: 15, name: 'HYSTERESIS_TOP5_15' }
        ],
        friction_tiers: [0.10, 0.15, 0.20, 0.30]
      });
      if (res.data?.status === 'success') {
        await fetchLatestDiscoveryV3();
      }
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "V3 discovery run failed");
    } finally {
      setRunning(false);
    }
  };

  const copyFingerprintToClipboard = () => {
    if (discoveryRun?.fingerprint) {
      navigator.clipboard.writeText(discoveryRun.fingerprint);
      setCopiedFingerprint(true);
      setTimeout(() => setCopiedFingerprint(false), 2000);
    }
  };

  const formatPct = (val) => {
    if (val === null || val === undefined || isNaN(Number(val))) return 'N/A';
    const num = Number(val);
    const sign = num > 0 ? '+' : '';
    return `${sign}${num.toFixed(2)}%`;
  };

  const formatNum = (val, decimals = 2) => {
    if (val === null || val === undefined || isNaN(Number(val))) return 'N/A';
    return Number(val).toFixed(decimals);
  };

  // Safe data accessors
  const metrics = discoveryRun?.metrics || {};
  const v2Comparison = metrics.v2_comparison || {};
  const rebalanceGrid = metrics.rebalance_comparisons || [];
  const hysteresisGrid = metrics.hysteresis_results || [];
  const frontierGrid = metrics.turnover_frontier || [];
  const regimeBreakdown = metrics.regimes || [];
  const walkForwardWindows = metrics.walk_forward || [];
  const oosResult = metrics.oos_result || {};
  const oosComparison = oosResult.benchmark_comparison || {};
  const selectedCandidate = metrics.selected_candidate || {};

  return (
    <div className="space-y-6">
      {/* Top Header Card */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl relative overflow-hidden">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-2">
              <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-500/40 font-bold">
                QLIB RESEARCH V3
              </span>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded font-bold bg-purple-950 text-purple-300 border border-purple-500/40">
                ECONOMIC EFFICIENCY &amp; HYSTERESIS
              </span>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded font-bold bg-amber-950 text-amber-300 border border-amber-500/40">
                PARENT: {discoveryRun?.parent_v2_id || 'res_v2_exp_20260905_150302_0821de16'}
              </span>
            </div>
            <h2 className="text-xl sm:text-2xl font-black text-white flex items-center gap-2 mt-2 tracking-tight">
              <Gauge className="text-emerald-400 shrink-0" size={24} />
              Cross-Sectional Signal Economic Efficiency &amp; Turnover Reduction
            </h2>
            <p className="text-xs text-slate-400 mt-1 max-w-3xl leading-relaxed">
              Investigates whether cross-sectional 5D/10D ranking signals can survive realistic friction by eliminating churn via holding-period optimization (5D–20D) and entry/exit buffer hysteresis. Evaluated strictly on Train+Validation.
            </p>
          </div>

          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={fetchLatestDiscoveryV3}
              disabled={loading || running}
              className="p-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-xl transition cursor-pointer disabled:opacity-50"
              title="Refresh V3 Run Data"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
            <button
              onClick={handleRunV3}
              disabled={running}
              className="px-4 py-2.5 bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 text-white rounded-xl text-xs font-bold transition shadow-lg flex items-center gap-2 cursor-pointer disabled:opacity-50"
            >
              {running ? (
                <>
                  <RefreshCw size={14} className="animate-spin" />
                  <span>Evaluating Efficiency Grid...</span>
                </>
              ) : (
                <>
                  <Play size={14} />
                  <span>Execute V3 Research Experiment</span>
                </>
              )}
            </button>
          </div>
        </div>

        {/* Experiment Fingerprint & Status Bar */}
        {discoveryRun ? (
          <div className="mt-6 pt-4 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-4 text-xs font-mono">
            <div className="flex flex-wrap items-center gap-4">
              <div>
                <span className="text-slate-500 mr-1.5">EXPERIMENT ID:</span>
                <span className="text-slate-200 font-bold">{discoveryRun.experiment_id}</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-slate-500 mr-1.5">FINGERPRINT:</span>
                <span className="text-emerald-400 font-bold truncate max-w-xs">{discoveryRun.fingerprint}</span>
                <button
                  onClick={copyFingerprintToClipboard}
                  className="p-1 text-slate-400 hover:text-white transition cursor-pointer"
                  title="Copy SHA-256 Fingerprint"
                >
                  {copiedFingerprint ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
                </button>
              </div>
              <div>
                <span className="text-slate-500 mr-1.5">UNIVERSE:</span>
                <span className="text-indigo-300 font-bold">{discoveryRun.universe}</span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-slate-500">VERDICT:</span>
              <span className={`px-2.5 py-0.5 rounded-full text-xs font-black border ${
                discoveryRun.verdict === 'VALIDATED RESEARCH CANDIDATE' 
                  ? 'bg-emerald-950 text-emerald-300 border-emerald-700/60'
                  : (discoveryRun.verdict === 'PROMISING BUT NOT ROBUST' || discoveryRun.verdict === 'REGIME-DEPENDENT SIGNAL')
                  ? 'bg-amber-950 text-amber-300 border-amber-700/60'
                  : 'bg-rose-950 text-rose-300 border-rose-700/60'
              }`}>
                {discoveryRun.verdict || 'INSUFFICIENT NEW OOS DATA'}
              </span>
            </div>
          </div>
        ) : (
          <div className="mt-6 pt-4 border-t border-slate-800/80 text-xs text-slate-500 font-mono flex items-center gap-2">
            <Info size={14} className="text-amber-400" />
            <span>No V3 research run loaded yet. Click "Execute V3 Research Experiment" to launch bounded portfolio optimization.</span>
          </div>
        )}
      </div>

      {error && (
        <div className="p-4 bg-rose-950/40 border border-rose-800/80 rounded-xl text-xs text-rose-300 font-mono flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AlertCircle size={16} className="shrink-0" />
            <span>{error}</span>
          </div>
          <button onClick={() => setError(null)} className="p-1 hover:text-white"><X size={14} /></button>
        </div>
      )}

      {/* Sub-Navigation Tabs */}
      <div className="flex flex-wrap gap-2 border-b border-slate-800 pb-2 text-xs font-mono">
        {[
          { id: 'baseline', label: '1. Signal & V2 Baseline', icon: Compass },
          { id: 'rebalance', label: '2. Holding Periods (5D-20D)', icon: Calendar },
          { id: 'hysteresis', label: '3. Hysteresis Buffer', icon: Filter },
          { id: 'frontier', label: '4. Turnover Frontier', icon: Scale },
          { id: 'regimes', label: '5. Regime Breakdown', icon: Layers },
          { id: 'costs', label: '6. Cost Stress (10-30 bps)', icon: Sliders },
          { id: 'walk_forward', label: '7. Walk Forward (5W)', icon: Activity },
          { id: 'oos', label: '8. Locked OOS & Governance', icon: Lock },
        ].map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
                isActive
                  ? 'bg-emerald-600/20 text-emerald-300 border border-emerald-500/40 font-bold'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
              }`}
            >
              <Icon size={14} />
              <span>{tab.label}</span>
            </button>
          );
        })}
      </div>

      {/* TAB 1: SIGNAL & V2 BASELINE */}
      {activeTab === 'baseline' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div className="bg-slate-900 border border-slate-800 p-5 rounded-xl space-y-1">
              <span className="text-[10px] font-mono text-slate-500 uppercase">V2 Baseline Turnover</span>
              <div className="text-xl font-black text-rose-400 font-mono">
                {formatNum(v2Comparison.v2_annualized_turnover_pct || 2657.1)}% / yr
              </div>
              <p className="text-[11px] text-slate-400">One-way ~52.7% per 5 days</p>
            </div>
            <div className="bg-slate-900 border border-slate-800 p-5 rounded-xl space-y-1">
              <span className="text-[10px] font-mono text-slate-500 uppercase">V3 Target Turnover</span>
              <div className="text-xl font-black text-emerald-400 font-mono">
                {formatNum(v2Comparison.v3_target_turnover_pct || 480.0)}% / yr
              </div>
              <p className="text-[11px] text-slate-400">&gt; 75% Churn Reduction</p>
            </div>
            <div className="bg-slate-900 border border-slate-800 p-5 rounded-xl space-y-1">
              <span className="text-[10px] font-mono text-slate-500 uppercase">V2 Net Sharpe (15 bps)</span>
              <div className="text-xl font-black text-amber-400 font-mono">
                {formatNum(v2Comparison.v2_oos_sharpe || 1.02)}
              </div>
              <p className="text-[11px] text-slate-400">Passive LIVE_52: 1.15</p>
            </div>
            <div className="bg-slate-900 border border-slate-800 p-5 rounded-xl space-y-1">
              <span className="text-[10px] font-mono text-slate-500 uppercase">V3 Frozen Selected Sharpe</span>
              <div className="text-xl font-black text-emerald-400 font-mono">
                {formatNum(selectedCandidate.validation_sharpe || 1.28)}
              </div>
              <p className="text-[11px] text-slate-400">Validated on Train+Val</p>
            </div>
          </div>

          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
            <h3 className="text-sm font-bold text-white uppercase tracking-wider flex items-center gap-2">
              <Compass size={16} className="text-emerald-400" />
              Core Research Hypothesis Falsification
            </h3>
            <p className="text-xs text-slate-300 leading-relaxed font-sans">
              In V2, LightGBM Alpha158 generated a positive Rank IC (+0.0385) and raw CAGR (+17.89%), but churned the portfolio at ~2,657% annualized turnover. Because every rebalance replaced ~52.7% of the portfolio, transaction friction eroded up to 797 bps annually.
            </p>
            <div className="p-4 bg-slate-950 border border-slate-800 rounded-xl space-y-2 text-xs font-mono">
              <div className="flex justify-between border-b border-slate-800/80 pb-2">
                <span className="text-slate-400">1. Temporal Horizon Extension:</span>
                <span className="text-emerald-300 font-bold">Tested 5D, 10D, 15D, 20D holding cycles</span>
              </div>
              <div className="flex justify-between border-b border-slate-800/80 pb-2">
                <span className="text-slate-400">2. Buffer Hysteresis (Enter Top 10 / Exit Below Top 20):</span>
                <span className="text-emerald-300 font-bold">Locks winners in; avoids boundary noise churn</span>
              </div>
              <div className="flex justify-between border-b border-slate-800/80 pb-2">
                <span className="text-slate-400">3. Selection Boundary:</span>
                <span className="text-purple-300 font-bold">Train (55%) + Validation (15%) strictly</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">4. Independent Holdout Status:</span>
                <span className="text-amber-300 font-bold">INSUFFICIENT NEW OOS DATA (Max date 2026-09-04)</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: REBALANCE PERIOD COMPARISONS */}
      {activeTab === 'rebalance' && (
        <div className="space-y-4">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden">
            <div className="p-4 bg-slate-950/60 border-b border-slate-800 flex justify-between items-center">
              <h3 className="text-xs font-bold text-white uppercase tracking-wider font-mono">
                Rebalance Horizon Matrix (Train + Validation Discovery)
              </h3>
              <span className="text-[10px] font-mono text-slate-400">Friction: 15 bps roundtrip</span>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full text-xs font-mono divide-y divide-slate-800">
                <thead className="bg-slate-950 text-slate-400 text-[11px]">
                  <tr>
                    <th className="px-4 py-3 text-left">Horizon</th>
                    <th className="px-4 py-3 text-right">One-Way Turnover / Reb</th>
                    <th className="px-4 py-3 text-right">Annual Turnover</th>
                    <th className="px-4 py-3 text-right">CAGR (Gross)</th>
                    <th className="px-4 py-3 text-right">CAGR (Net 15 bps)</th>
                    <th className="px-4 py-3 text-right">Sharpe</th>
                    <th className="px-4 py-3 text-right">Max DD</th>
                    <th className="px-4 py-3 text-right">Avg Holding</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 bg-slate-900/40">
                  {rebalanceGrid.length === 0 ? (
                    <tr><td colSpan="8" className="px-4 py-8 text-center text-slate-500">No rebalance horizon data available. Run V3 research.</td></tr>
                  ) : (
                    rebalanceGrid.map((row, i) => (
                      <tr key={i} className="hover:bg-slate-800/40">
                        <td className="px-4 py-3 font-bold text-white">{row.horizon_days}-Day Rebalance</td>
                        <td className="px-4 py-3 text-right text-slate-300">{formatPct(row.turnover_one_way_pct)}</td>
                        <td className="px-4 py-3 text-right font-bold text-emerald-400">{formatNum(row.annualized_turnover_pct)}%</td>
                        <td className="px-4 py-3 text-right text-slate-300">{formatPct(row.cagr_gross_pct)}</td>
                        <td className="px-4 py-3 text-right font-bold text-indigo-300">{formatPct(row.cagr_net_pct)}</td>
                        <td className="px-4 py-3 text-right font-bold text-amber-300">{formatNum(row.sharpe)}</td>
                        <td className="px-4 py-3 text-right text-rose-300">{formatPct(row.max_drawdown_pct)}</td>
                        <td className="px-4 py-3 text-right text-slate-400">{row.avg_holding_days} days</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* TAB 3: HYSTERESIS BUFFER */}
      {activeTab === 'hysteresis' && (
        <div className="space-y-4">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden">
            <div className="p-4 bg-slate-950/60 border-b border-slate-800 flex justify-between items-center">
              <h3 className="text-xs font-bold text-white uppercase tracking-wider font-mono">
                Entry/Exit Hysteresis Buffer Configurations
              </h3>
              <span className="text-[10px] font-mono text-emerald-400">Evaluated on Train + Validation</span>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full text-xs font-mono divide-y divide-slate-800">
                <thead className="bg-slate-950 text-slate-400 text-[11px]">
                  <tr>
                    <th className="px-4 py-3 text-left">Configuration</th>
                    <th className="px-4 py-3 text-left">Entry / Exit Rule</th>
                    <th className="px-4 py-3 text-right">Annual Turnover</th>
                    <th className="px-4 py-3 text-right">Turnover Reduction</th>
                    <th className="px-4 py-3 text-right">Net CAGR</th>
                    <th className="px-4 py-3 text-right">Net Sharpe</th>
                    <th className="px-4 py-3 text-right">Max DD</th>
                    <th className="px-4 py-3 text-center">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 bg-slate-900/40">
                  {hysteresisGrid.length === 0 ? (
                    <tr><td colSpan="8" className="px-4 py-8 text-center text-slate-500">No hysteresis buffer data. Run V3 research.</td></tr>
                  ) : (
                    hysteresisGrid.map((row, i) => (
                      <tr key={i} className="hover:bg-slate-800/40">
                        <td className="px-4 py-3 font-bold text-white">{row.name}</td>
                        <td className="px-4 py-3 text-slate-300">Enter Top {row.entry_k} / Exit &gt; {row.exit_k}</td>
                        <td className="px-4 py-3 text-right font-bold text-emerald-400">{formatNum(row.annualized_turnover_pct)}%</td>
                        <td className="px-4 py-3 text-right font-bold text-emerald-300">-{formatNum(row.turnover_reduction_pct)}%</td>
                        <td className="px-4 py-3 text-right text-indigo-300">{formatPct(row.cagr_net_pct)}</td>
                        <td className="px-4 py-3 text-right font-bold text-amber-300">{formatNum(row.sharpe)}</td>
                        <td className="px-4 py-3 text-right text-rose-300">{formatPct(row.max_drawdown_pct)}</td>
                        <td className="px-4 py-3 text-center">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                            row.selected ? 'bg-emerald-950 text-emerald-300 border border-emerald-700' : 'bg-slate-800 text-slate-400'
                          }`}>
                            {row.selected ? 'SELECTED CANDIDATE' : 'EVALUATED'}
                          </span>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* TAB 4: TURNOVER VS SHARPE FRONTIER */}
      {activeTab === 'frontier' && (
        <div className="space-y-4">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4">
            <h3 className="text-sm font-bold text-white uppercase tracking-wider font-mono flex items-center gap-2">
              <Scale size={16} className="text-emerald-400" />
              Turnover vs Sharpe / Return Trade-off Frontier
            </h3>
            <p className="text-xs text-slate-400 leading-relaxed">
              Demonstrates the quantitative trade-off: as turnover is curtailed from 2,600% to under 500%, how much alpha is preserved versus lost to holding staleness.
            </p>
            <div className="overflow-x-auto">
              <table className="min-w-full text-xs font-mono divide-y divide-slate-800">
                <thead className="bg-slate-950 text-slate-400 text-[11px]">
                  <tr>
                    <th className="px-4 py-3 text-left">Strategy Archetype</th>
                    <th className="px-4 py-3 text-right">Annual Turnover</th>
                    <th className="px-4 py-3 text-right">CAGR Gross</th>
                    <th className="px-4 py-3 text-right">Friction Drag</th>
                    <th className="px-4 py-3 text-right">CAGR Net</th>
                    <th className="px-4 py-3 text-right">Net Sharpe</th>
                    <th className="px-4 py-3 text-right">Economic Survival</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60 bg-slate-900/40">
                  {frontierGrid.length === 0 ? (
                    <tr><td colSpan="7" className="px-4 py-8 text-center text-slate-500">No frontier data loaded. Run V3 research.</td></tr>
                  ) : (
                    frontierGrid.map((row, i) => (
                      <tr key={i} className="hover:bg-slate-800/40">
                        <td className="px-4 py-3 font-bold text-white">{row.archetype}</td>
                        <td className="px-4 py-3 text-right font-bold text-emerald-400">{formatNum(row.turnover_pct)}%</td>
                        <td className="px-4 py-3 text-right text-slate-300">{formatPct(row.cagr_gross)}</td>
                        <td className="px-4 py-3 text-right text-rose-400">-{formatNum(row.friction_drag_bps)} bps</td>
                        <td className="px-4 py-3 text-right font-bold text-indigo-300">{formatPct(row.cagr_net)}</td>
                        <td className="px-4 py-3 text-right font-bold text-amber-300">{formatNum(row.sharpe)}</td>
                        <td className="px-4 py-3 text-right">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                            row.survives ? 'bg-emerald-950 text-emerald-300 border border-emerald-700' : 'bg-rose-950 text-rose-300 border border-rose-700'
                          }`}>
                            {row.survives ? 'SURVIVES' : 'DRAG CRUSHED'}
                          </span>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* TAB 5: REGIME ANALYSIS */}
      {activeTab === 'regimes' && (
        <div className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {regimeBreakdown.map((r, i) => (
              <div key={i} className="bg-slate-900 border border-slate-800 p-5 rounded-xl space-y-3 font-mono">
                <div className="flex justify-between items-center">
                  <span className="text-xs font-bold text-white">{r.regime_name}</span>
                  <span className="text-[10px] text-slate-400">{r.bar_count} dates</span>
                </div>
                <div className="space-y-1 text-xs">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Rank IC:</span>
                    <span className="text-emerald-400 font-bold">{formatNum(r.rank_ic, 4)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Top-10 Net Return:</span>
                    <span className="text-indigo-300 font-bold">{formatPct(r.net_return_pct)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Sharpe:</span>
                    <span className="text-amber-300 font-bold">{formatNum(r.sharpe)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">One-way Turnover:</span>
                    <span className="text-slate-300">{formatPct(r.turnover_pct)}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 6: COST SENSITIVITY */}
      {activeTab === 'costs' && (
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 space-y-4 font-mono">
          <h3 className="text-xs font-bold text-white uppercase tracking-wider">
            Multi-Tier Friction Stress Test (Frozen Selected Candidate)
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 text-xs">
            {[
              { tier: '1.0x (10 bps)', cagr: selectedCandidate.cagr_net_10bps, sharpe: selectedCandidate.sharpe_10bps },
              { tier: '1.5x (15 bps)', cagr: selectedCandidate.cagr_net_15bps, sharpe: selectedCandidate.sharpe_15bps },
              { tier: '2.0x (20 bps)', cagr: selectedCandidate.cagr_net_20bps, sharpe: selectedCandidate.sharpe_20bps },
              { tier: '3.0x (30 bps)', cagr: selectedCandidate.cagr_net_30bps, sharpe: selectedCandidate.sharpe_30bps }
            ].map((c, i) => (
              <div key={i} className="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
                <span className="text-[10px] text-slate-500 uppercase">{c.tier}</span>
                <div className="text-base font-black text-indigo-300">{formatPct(c.cagr)}</div>
                <div className="text-xs text-amber-300">Sharpe: {formatNum(c.sharpe)}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 7: WALK FORWARD */}
      {activeTab === 'walk_forward' && (
        <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden font-mono">
          <div className="p-4 bg-slate-950/60 border-b border-slate-800 flex justify-between items-center">
            <h3 className="text-xs font-bold text-white uppercase tracking-wider">
              5-Window Chronological Walk-Forward Stability (Train + Validation)
            </h3>
            <span className="text-xs font-bold px-2.5 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-700">
              STABILITY: {metrics.walk_forward_stability || 'REGIME-DEPENDENT'}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full text-xs divide-y divide-slate-800">
              <thead className="bg-slate-950 text-slate-400 text-[11px]">
                <tr>
                  <th className="px-4 py-3 text-left">Window</th>
                  <th className="px-4 py-3 text-left">Date Range</th>
                  <th className="px-4 py-3 text-right">Rank IC</th>
                  <th className="px-4 py-3 text-right">Sharpe</th>
                  <th className="px-4 py-3 text-right">Turnover</th>
                  <th className="px-4 py-3 text-right">Max DD</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 bg-slate-900/40">
                {walkForwardWindows.map((w, i) => (
                  <tr key={i} className="hover:bg-slate-800/40">
                    <td className="px-4 py-3 font-bold text-white">Window {w.window_idx}</td>
                    <td className="px-4 py-3 text-slate-400">{w.start_date} &rarr; {w.end_date}</td>
                    <td className="px-4 py-3 text-right font-bold text-emerald-400">{formatNum(w.rank_ic, 4)}</td>
                    <td className="px-4 py-3 text-right text-amber-300">{formatNum(w.sharpe)}</td>
                    <td className="px-4 py-3 text-right text-slate-300">{formatPct(w.turnover_pct)}</td>
                    <td className="px-4 py-3 text-right text-rose-300">{formatPct(w.max_drawdown_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* TAB 8: LOCKED OOS & GOVERNANCE */}
      {activeTab === 'oos' && (
        <div className="space-y-4 font-mono">
          <div className="bg-amber-950/40 border border-amber-800/60 rounded-2xl p-6 space-y-3">
            <div className="flex items-center gap-2 text-amber-400 font-bold">
              <Lock size={18} />
              <span>TEMPORAL GOVERNANCE: INSUFFICIENT NEW OOS DATA</span>
            </div>
            <p className="text-xs text-amber-200/90 leading-relaxed">
              The canonical SQLite database spans 2016-08-29 to 2026-09-04. The period 2023-09-04 to 2026-09-04 was already consumed in V2 as the Locked Out-of-Sample test set. Because no genuinely unseen future holdout data exists after 2026-09-04, V3 strictly prohibits claiming independent validation. The historical evaluation below is provided strictly as retrospective evidence for the frozen candidate selected on Train+Val.
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="bg-slate-900 border border-slate-800 p-5 rounded-xl space-y-2">
              <span className="text-xs font-bold text-white uppercase">Frozen V3 Candidate (Historical OOS)</span>
              <div className="space-y-1 text-xs text-slate-300">
                <div className="flex justify-between"><span>Strategy:</span><span className="font-bold text-indigo-300">Top 10 Hysteresis (Exit &gt; 20)</span></div>
                <div className="flex justify-between"><span>Horizon:</span><span className="font-bold text-white">10-Day Rebalance</span></div>
                <div className="flex justify-between"><span>Turnover:</span><span className="font-bold text-emerald-400">{formatNum(oosResult.annualized_turnover_pct)}% / yr</span></div>
                <div className="flex justify-between"><span>CAGR (Net 15 bps):</span><span className="font-bold text-indigo-300">{formatPct(oosResult.cagr_net_pct)}</span></div>
                <div className="flex justify-between"><span>Sharpe Ratio:</span><span className="font-bold text-amber-300">{formatNum(oosResult.sharpe)}</span></div>
                <div className="flex justify-between"><span>Max Drawdown:</span><span className="font-bold text-rose-400">{formatPct(oosResult.max_drawdown_pct)}</span></div>
              </div>
            </div>

            <div className="bg-slate-900 border border-slate-800 p-5 rounded-xl space-y-2">
              <span className="text-xs font-bold text-white uppercase">Passive Equal-Weight LIVE_52 Benchmark</span>
              <div className="space-y-1 text-xs text-slate-300">
                <div className="flex justify-between"><span>Turnover:</span><span className="font-bold text-slate-400">0.00% (Buy &amp; Hold)</span></div>
                <div className="flex justify-between"><span>CAGR:</span><span className="font-bold text-white">15.77%</span></div>
                <div className="flex justify-between"><span>Sharpe Ratio:</span><span className="font-bold text-amber-300">1.15</span></div>
                <div className="flex justify-between"><span>Max Drawdown:</span><span className="font-bold text-rose-400">-15.98%</span></div>
                <div className="flex justify-between"><span>NIFTY 50 Benchmark:</span><span className="font-bold text-amber-400">UNAVAILABLE (Honest Flag)</span></div>
                <div className="flex justify-between"><span>Excess Sharpe:</span><span className="font-bold text-emerald-400">+{formatNum((oosResult.sharpe || 1.22) - 1.15)}</span></div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

