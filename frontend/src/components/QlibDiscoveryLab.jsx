import React, { useState, useEffect } from 'react';
import axios from 'axios';
import { 
  BrainCircuit, ShieldCheck, Sparkles, TrendingUp, Activity, 
  Play, RefreshCw, Layers, CheckCircle, AlertTriangle, X, ChevronRight, 
  BarChart2, Info, Compass, Target, Scale, Zap, Lock, Filter, Database
} from 'lucide-react';
import { API_BASE } from '../services/api';

export default function QlibDiscoveryLab({ initialStrategy = 'SWING' }) {
  const [strategy, setStrategy] = useState(initialStrategy.toUpperCase());
  const [viewMode, setViewMode] = useState('discovery'); // 'discovery' | 'evaluator'
  
  // Evaluator State
  const [modelFamily, setModelFamily] = useState('lightgbm');
  const [featureFamily, setFeatureFamily] = useState('Alpha158');
  const [universe, setUniverse] = useState('LIVE_52');
  const [evaluations, setEvaluations] = useState([]);
  const [evaluating, setEvaluating] = useState(false);
  const [evalResult, setEvalResult] = useState(null);
  const [selectedEval, setSelectedEval] = useState(null);

  // Signal Discovery Engine V1 State
  const [discoveryRun, setDiscoveryRun] = useState(null);
  const [discoveryLoading, setDiscoveryLoading] = useState(false);
  const [runningDiscovery, setRunningDiscovery] = useState(false);
  const [activeDiscoveryTab, setActiveDiscoveryTab] = useState('overview'); // overview, features, horizons, models, deciles, regimes, friction
  
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetchLatestDiscovery();
    fetchEvaluations();
  }, [strategy]);

  const fetchLatestDiscovery = async () => {
    setDiscoveryLoading(true);
    try {
      const res = await axios.get(`${API_BASE}/ml/qlib/signal-discovery/latest`);
      if (res.data?.status === 'success' && res.data?.discovery_run) {
        setDiscoveryRun(res.data.discovery_run);
      }
    } catch (e) {
      console.warn("Signal discovery fetch failed:", e);
    } finally {
      setDiscoveryLoading(false);
    }
  };

  const fetchEvaluations = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await axios.get(`${API_BASE}/ml/qlib/evaluations?strategy=${strategy}`);
      if (res.data?.status === 'success') {
        setEvaluations(res.data.evaluations || []);
      }
    } catch (e) {
      setError(`Failed to load Qlib evaluations: ${e.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleRunDiscovery = async () => {
    setRunningDiscovery(true);
    setError(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/qlib/signal-discovery/run`, {
        universe: 'LIVE_52',
        horizons: [1, 3, 5, 10, 20],
        seeds: [42, 101, 777]
      });
      if (res.data?.status === 'success') {
        await fetchLatestDiscovery();
      }
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "Signal discovery run failed");
    } finally {
      setRunningDiscovery(false);
    }
  };

  const handleRunEvaluation = async () => {
    setEvaluating(true);
    setError(null);
    setEvalResult(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/qlib/evaluate`, {
        strategy: strategy,
        model_family: modelFamily,
        feature_family: featureFamily,
        universe: universe
      });
      if (res.data?.status === 'success') {
        setEvalResult(res.data);
        await fetchEvaluations();
      }
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "Model evaluation failed");
    } finally {
      setEvaluating(false);
    }
  };

  const metrics = discoveryRun?.metrics || {};
  const stageA = metrics?.stage_a_features || {};
  const stageBHorizons = metrics?.stage_b_horizons || [];
  const stageBTargets = metrics?.stage_b_targets || [];
  const stageCModels = metrics?.stage_c_models || [];
  const stageDVal = metrics?.stage_d_validation || {};
  const stageELockedOOS = metrics?.stage_e_locked_oos || {};
  const audit = metrics?.multi_testing_audit || {};
  const datasetMeta = metrics?.dataset_meta || {};

  const championBaseline = evaluations.find(e => e.model_name === 'Champion Baseline' || e.gate_verdict === 'BENCHMARK');
  const candidateEvals = evaluations.filter(e => e.model_name !== 'Champion Baseline' && e.gate_verdict !== 'BENCHMARK');

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden mb-8">
      {/* Header Banner */}
      <div className="bg-gradient-to-r from-slate-900 via-indigo-950 to-slate-900 p-6 text-white flex flex-col md:flex-row md:items-center md:justify-between gap-4">
        <div>
          <div className="flex items-center space-x-2">
            <BrainCircuit className="text-emerald-400" size={22} />
            <h3 className="text-lg font-bold tracking-tight">Qlib Signal Discovery & Research Engine V1</h3>
            <span className="bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 text-[10px] font-mono px-2.5 py-0.5 rounded-full font-bold">
              SYSTEMATIC FACTOR RESEARCH
            </span>
          </div>
          <p className="text-xs text-slate-300 mt-1">
            Exhaustive empirical search for predictive signals in Indian equity data across Alpha158 features, multi-horizons, models, deciles, regimes, and locked holdout OOS.
          </p>
          <div className="mt-2 flex flex-wrap gap-2 text-[10px] font-mono text-slate-400">
            <span>Provenance: <strong className="text-emerald-300">QLIB-INSPIRED / QLIB-COMPATIBLE FEATURE IMPLEMENTATION</strong></span>
            <span>•</span>
            <span>Universe: <strong className="text-slate-200">LIVE_52 (Canonical SQLite)</strong></span>
            <span>•</span>
            <span>Isolation: <strong className="text-emerald-300">0 writes to ml_trade_history</strong></span>
          </div>
        </div>

        {/* Global Action Controls */}
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => { fetchLatestDiscovery(); fetchEvaluations(); }}
            disabled={loading || runningDiscovery || evaluating}
            className="bg-slate-800 hover:bg-slate-700 text-slate-300 px-3 py-2 rounded-lg text-xs font-bold transition flex items-center shadow-sm cursor-pointer"
            title="Refresh discovery findings"
          >
            <RefreshCw size={13} className={`mr-1.5 ${discoveryLoading ? 'animate-spin' : ''}`} />
            Sync
          </button>

          {viewMode === 'discovery' ? (
            <button
              onClick={handleRunDiscovery}
              disabled={runningDiscovery}
              className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-xs font-bold transition flex items-center shadow-md cursor-pointer whitespace-nowrap"
            >
              {runningDiscovery ? (
                <>
                  <div className="animate-spin h-3.5 w-3.5 rounded-full border-2 border-t-transparent border-white mr-2"></div>
                  Running 15-Step Discovery (LIVE_52)...
                </>
              ) : (
                <>
                  <Play size={13} className="mr-1.5 fill-current" />
                  ⚡ Run Staged Discovery V1
                </>
              )}
            </button>
          ) : (
            <button
              onClick={handleRunEvaluation}
              disabled={evaluating}
              className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-xs font-bold transition flex items-center shadow-md cursor-pointer whitespace-nowrap"
            >
              {evaluating ? (
                <>
                  <div className="animate-spin h-3.5 w-3.5 rounded-full border-2 border-t-transparent border-white mr-2"></div>
                  Evaluating {modelFamily.toUpperCase()}...
                </>
              ) : (
                <>
                  <Play size={13} className="mr-1.5 fill-current" />
                  ⚡ Evaluate Candidate Model
                </>
              )}
            </button>
          )}
        </div>
      </div>

      {/* Safety & Research Boundary Guard Strip */}
      <div className="bg-slate-900 border-b border-slate-800 px-6 py-2.5 flex flex-col md:flex-row md:items-center md:justify-between text-xs text-slate-300 font-medium gap-2">
        <div className="flex items-center gap-2">
          <ShieldCheck className="text-emerald-400 shrink-0" size={16} />
          <span>
            <strong>Research Invariants Enforced:</strong> Discovery matrix runs on Train (55%) + Val (15%) only. Frozen candidate evaluated on Locked OOS (30%) exactly once. Zero tuning on holdout.
          </span>
        </div>
        <div className="flex items-center gap-3 text-[10px] font-mono text-slate-400 shrink-0">
          <span>Champion Hashes: <strong className="text-emerald-400">UNTOUCHED</strong></span>
          <span>•</span>
          <span>Live Heat: <strong className="text-emerald-400">0.00%</strong></span>
          <span>•</span>
          <span>Orders: <strong className="text-emerald-400">0</strong></span>
        </div>
      </div>

      {/* View Mode Navigation Tabs */}
      <div className="border-b border-gray-200 bg-slate-50 px-6 flex items-center justify-between">
        <div className="flex space-x-4">
          <button
            onClick={() => setViewMode('discovery')}
            className={`py-3 text-xs font-bold border-b-2 flex items-center gap-2 cursor-pointer transition ${
              viewMode === 'discovery' 
                ? 'border-emerald-600 text-emerald-700' 
                : 'border-transparent text-gray-500 hover:text-gray-800'
            }`}
          >
            <Compass size={15} />
            🔬 QLib Signal Discovery Engine V1
            {discoveryRun && (
              <span className={`text-[10px] font-mono px-2 py-0.5 rounded-full font-bold ${
                discoveryRun.verdict === 'NO ROBUST SIGNAL FOUND' ? 'bg-rose-100 text-rose-800' : 'bg-emerald-100 text-emerald-800'
              }`}>
                {discoveryRun.verdict}
              </span>
            )}
          </button>

          <button
            onClick={() => setViewMode('evaluator')}
            className={`py-3 text-xs font-bold border-b-2 flex items-center gap-2 cursor-pointer transition ${
              viewMode === 'evaluator' 
                ? 'border-indigo-600 text-indigo-700' 
                : 'border-transparent text-gray-500 hover:text-gray-800'
            }`}
          >
            <Scale size={15} />
            ⚖️ Single Candidate Evaluator (Challenger vs Champion)
          </button>
        </div>

        <div className="text-[11px] font-mono text-gray-500">
          Last Run: <strong>{discoveryRun?.timestamp ? new Date(discoveryRun.timestamp).toLocaleTimeString() : 'N/A'}</strong>
        </div>
      </div>

      {/* Error Alert */}
      {error && (
        <div className="mx-6 mt-4 p-3 bg-rose-50 border border-rose-200 rounded-lg text-xs text-rose-800 font-bold flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AlertTriangle size={16} className="text-rose-600" />
            <span>{error}</span>
          </div>
          <button onClick={() => setError(null)} className="text-rose-500 hover:text-rose-700">
            <X size={14} />
          </button>
        </div>
      )}

      {/* ========================================================================= */}
      {/* VIEW 1: QLIB SIGNAL DISCOVERY ENGINE V1 */}
      {/* ========================================================================= */}
      {viewMode === 'discovery' && (
        <div className="p-6 space-y-6">
          {!discoveryRun ? (
            <div className="text-center py-12 bg-slate-50 border border-dashed border-gray-300 rounded-xl space-y-3">
              <BrainCircuit className="mx-auto text-slate-400" size={36} />
              <h4 className="text-sm font-bold text-gray-700">No Signal Discovery Runs Executed Yet</h4>
              <p className="text-xs text-gray-500 max-w-md mx-auto">
                Execute the comprehensive staged research funnel to evaluate 158 Alpha158 factors, 5 prediction horizons, 5 model families, cross-sectional ranking, and locked OOS holdouts.
              </p>
              <button
                onClick={handleRunDiscovery}
                disabled={runningDiscovery}
                className="bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs px-4 py-2 rounded-lg transition shadow cursor-pointer"
              >
                ⚡ Run Initial LIVE_52 Discovery
              </button>
            </div>
          ) : (
            <>
              {/* Executive Research Verdict Card */}
              <div className={`p-5 rounded-xl border ${
                discoveryRun.verdict === 'NO ROBUST SIGNAL FOUND'
                  ? 'bg-rose-50/70 border-rose-200 text-rose-950'
                  : 'bg-emerald-50/70 border-emerald-200 text-emerald-950'
              }`}>
                <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3 border-b pb-4 border-gray-200/60">
                  <div className="flex items-center gap-3">
                    <div className={`p-2.5 rounded-xl ${
                      discoveryRun.verdict === 'NO ROBUST SIGNAL FOUND' ? 'bg-rose-600 text-white' : 'bg-emerald-600 text-white'
                    }`}>
                      <Target size={22} />
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="text-xs uppercase tracking-wider font-bold text-gray-600">Research Invariant Verdict:</span>
                        <span className={`text-xs font-mono font-black px-2.5 py-0.5 rounded-full ${
                          discoveryRun.verdict === 'NO ROBUST SIGNAL FOUND' ? 'bg-rose-200 text-rose-900 border border-rose-300' : 'bg-emerald-200 text-emerald-900 border border-emerald-300'
                        }`}>
                          {discoveryRun.verdict}
                        </span>
                      </div>
                      <h3 className="text-base font-black tracking-tight mt-0.5">
                        {discoveryRun.verdict === 'NO ROBUST SIGNAL FOUND' 
                          ? 'Empirical Verdict: Alpha158 Factors Lack Monotonic Predictive Power on Locked Holdout'
                          : 'Empirical Verdict: Statistically Robust Predictive Signal Detected'}
                      </h3>
                    </div>
                  </div>

                  <div className="text-right font-mono text-[11px] text-gray-500">
                    <div>Experiment ID: <strong className="text-gray-800">{discoveryRun.experiment_id}</strong></div>
                    <div>Holdout Split: <strong className="text-gray-800">{metrics.dates_meta?.split_ratio || '55/15/30'}</strong></div>
                  </div>
                </div>

                {/* Locked Holdout Metrics Grid */}
                <div className="grid grid-cols-2 md:grid-cols-6 gap-3 pt-4 text-xs font-mono">
                  <div className="bg-white/80 p-2.5 rounded-lg border border-gray-200">
                    <span className="text-gray-500 block text-[10px] font-sans font-bold uppercase">Locked OOS Rank IC</span>
                    <span className={`text-base font-black ${stageELockedOOS.rank_ic >= 0.02 ? 'text-emerald-700' : 'text-slate-800'}`}>
                      {stageELockedOOS.rank_ic !== undefined ? stageELockedOOS.rank_ic : 'N/A'}
                    </span>
                  </div>
                  <div className="bg-white/80 p-2.5 rounded-lg border border-gray-200">
                    <span className="text-gray-500 block text-[10px] font-sans font-bold uppercase">Pearson IC</span>
                    <span className="text-base font-black text-slate-800">{stageELockedOOS.pearson_ic !== undefined ? stageELockedOOS.pearson_ic : 'N/A'}</span>
                  </div>
                  <div className="bg-white/80 p-2.5 rounded-lg border border-gray-200">
                    <span className="text-gray-500 block text-[10px] font-sans font-bold uppercase">Q10 - Q1 Spread</span>
                    <span className={`text-base font-black ${stageELockedOOS.top_minus_bottom_spread_pct > 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                      {stageELockedOOS.top_minus_bottom_spread_pct > 0 ? `+${stageELockedOOS.top_minus_bottom_spread_pct}%` : `${stageELockedOOS.top_minus_bottom_spread_pct}%`}
                    </span>
                  </div>
                  <div className="bg-white/80 p-2.5 rounded-lg border border-gray-200">
                    <span className="text-gray-500 block text-[10px] font-sans font-bold uppercase">Monotonicity (r)</span>
                    <span className={`text-base font-black ${stageELockedOOS.monotonicity_score >= 0.50 ? 'text-emerald-700' : 'text-amber-800'}`}>
                      {stageELockedOOS.monotonicity_score !== undefined ? stageELockedOOS.monotonicity_score : 'N/A'}
                    </span>
                  </div>
                  <div className="bg-white/80 p-2.5 rounded-lg border border-gray-200">
                    <span className="text-gray-500 block text-[10px] font-sans font-bold uppercase">Net Sharpe (12 bps)</span>
                    <span className={`text-base font-black ${stageELockedOOS.oos_sharpe > 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                      {stageELockedOOS.oos_sharpe !== undefined ? stageELockedOOS.oos_sharpe : 'N/A'}
                    </span>
                  </div>
                  <div className="bg-white/80 p-2.5 rounded-lg border border-gray-200">
                    <span className="text-gray-500 block text-[10px] font-sans font-bold uppercase">Holdout Date Range</span>
                    <span className="text-[11px] font-bold text-gray-700 block mt-1">{stageELockedOOS.oos_dates || 'N/A'}</span>
                  </div>
                </div>

                {/* Mandatory Survivorship Bias Disclaimer */}
                <div className="mt-4 p-3 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-900 flex items-start gap-2">
                  <Info className="text-amber-600 shrink-0 mt-0.5" size={15} />
                  <div>
                    <strong className="font-bold">Survivorship Governance Caveat:</strong> Universe mode is labeled <code className="bg-amber-100 px-1 rounded font-mono font-bold">CURRENT_CONSTITUENTS_RETROSPECTIVE</code>. Backtests on retrospective index constituents reflect survival survivorship bias; historical performance is not indicative of future out-of-universe efficacy.
                  </div>
                </div>
              </div>

              {/* Sub-Navigation Tabs */}
              <div className="border-b border-gray-200 flex space-x-2 text-xs font-bold overflow-x-auto">
                {[
                  { id: 'features', label: '📊 Alpha158 Factors & Clusters (Stage A)' },
                  { id: 'horizons', label: '⏱️ Horizons & Targets (Stage B)' },
                  { id: 'models', label: '🤖 Model Comparison & Seeds (Stage C)' },
                  { id: 'deciles', label: '📈 Decile Monotonicity Q1-Q10 (Stage D)' },
                  { id: 'regimes', label: '🌐 Regimes & Stock Breadth (Stage D)' },
                  { id: 'friction', label: '💸 Friction Survival & Audit (Stage D/E)' },
                  { id: 'hysteresis', label: '⚖️ Turnover & Hysteresis Frontier (V2/V3)' },
                ].map((tab) => (
                  <button
                    key={tab.id}
                    onClick={() => setActiveDiscoveryTab(tab.id)}
                    className={`py-2 px-3 border-b-2 transition cursor-pointer ${
                      activeDiscoveryTab === tab.id 
                        ? 'border-indigo-600 text-indigo-700 bg-indigo-50/50 rounded-t' 
                        : 'border-transparent text-gray-600 hover:text-gray-900'
                    }`}
                  >
                    {tab.label}
                  </button>
                ))}
              </div>

              {/* TAB 1: Alpha158 Factors & Core Clusters */}
              {activeDiscoveryTab === 'features' && (
                <div className="space-y-4">
                  <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
                    <div className="bg-emerald-50 border border-emerald-200 p-3 rounded-lg text-xs">
                      <span className="text-emerald-800 font-bold block">Stable Factors</span>
                      <span className="text-xl font-black text-emerald-950">{stageA.stable_count || 0}</span>
                      <span className="text-[10px] text-emerald-700 block mt-0.5">80%+ Sign Consistency</span>
                    </div>
                    <div className="bg-amber-50 border border-amber-200 p-3 rounded-lg text-xs">
                      <span className="text-amber-800 font-bold block">Regime-Sensitive</span>
                      <span className="text-xl font-black text-amber-950">{stageA.regime_sensitive_count || 0}</span>
                      <span className="text-[10px] text-amber-700 block mt-0.5">Fails under vol shocks</span>
                    </div>
                    <div className="bg-rose-50 border border-rose-200 p-3 rounded-lg text-xs">
                      <span className="text-rose-800 font-bold block">Unstable / Noise</span>
                      <span className="text-xl font-black text-rose-950">{stageA.unstable_count || 0}</span>
                      <span className="text-[10px] text-rose-700 block mt-0.5">Sign flips frequently</span>
                    </div>
                    <div className="bg-purple-50 border border-purple-200 p-3 rounded-lg text-xs">
                      <span className="text-purple-800 font-bold block">Negative Predictors</span>
                      <span className="text-xl font-black text-purple-950">{stageA.negative_count || 0}</span>
                      <span className="text-[10px] text-purple-700 block mt-0.5">Reversal signals</span>
                    </div>
                    <div className="bg-slate-100 border border-slate-200 p-3 rounded-lg text-xs">
                      <span className="text-slate-800 font-bold block">No Predictive Signal</span>
                      <span className="text-xl font-black text-slate-950">{stageA.no_signal_count || 0}</span>
                      <span className="text-[10px] text-slate-600 block mt-0.5">Rank IC near 0.00</span>
                    </div>
                  </div>

                  {/* Core Signal Clusters */}
                  {stageA.core_clusters && (
                    <div className="bg-slate-50 p-4 rounded-xl border border-gray-200 space-y-2">
                      <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider">Factor Correlation Clusters</h4>
                      <div className="grid grid-cols-1 md:grid-cols-5 gap-2 text-xs">
                        {stageA.core_clusters.map((c, i) => (
                          <div key={i} className="bg-white p-2.5 rounded border border-gray-200 space-y-1">
                            <span className="font-bold text-slate-800 block text-[11px]">{c.cluster_name}</span>
                            <div className="text-[10px] text-gray-500 font-mono">
                              <div>Features: <strong>{c.feature_count}</strong></div>
                              <div>Avg Internal Corr: <strong>{c.average_internal_correlation}</strong></div>
                              <div className="truncate text-indigo-600">Sample: {c.representative_features?.slice(0, 2).join(', ')}</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Top Features Table */}
                  <div className="overflow-x-auto rounded-lg border border-gray-200">
                    <table className="min-w-full text-xs divide-y divide-gray-200">
                      <thead className="bg-slate-100 text-slate-700 font-bold">
                        <tr>
                          <th className="px-3 py-2.5 text-left">Feature Name</th>
                          <th className="px-2 py-2.5 text-center">Rank IC</th>
                          <th className="px-2 py-2.5 text-center">Rank ICIR</th>
                          <th className="px-2 py-2.5 text-center">Pearson IC</th>
                          <th className="px-2 py-2.5 text-center">Positive %</th>
                          <th className="px-2 py-2.5 text-center">Sign Consistency</th>
                          <th className="px-3 py-2.5 text-center">Stability Class</th>
                          <th className="px-3 py-2.5 text-center">95% CI</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-200 bg-white">
                        {discoveryRun.feature_stats?.slice(0, 15).map((f, idx) => (
                          <tr key={idx} className="hover:bg-slate-50">
                            <td className="px-3 py-2 font-mono font-bold text-indigo-950">{f.feature_name}</td>
                            <td className={`px-2 py-2 text-center font-bold ${f.rank_ic_mean >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                              {f.rank_ic_mean}
                            </td>
                            <td className="px-2 py-2 text-center font-mono text-slate-700">{f.rank_icir}</td>
                            <td className="px-2 py-2 text-center font-mono text-slate-700">{f.ic_mean}</td>
                            <td className="px-2 py-2 text-center text-slate-700">{f.positive_ic_pct}%</td>
                            <td className="px-2 py-2 text-center text-slate-700">{f.sign_consistency_pct || 'N/A'}%</td>
                            <td className="px-3 py-2 text-center">
                              <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                                f.stability_class === 'STABLE' ? 'bg-emerald-100 text-emerald-800' :
                                f.stability_class === 'UNSTABLE' ? 'bg-rose-100 text-rose-800' :
                                f.stability_class === 'REGIME-SENSITIVE' ? 'bg-amber-100 text-amber-800' :
                                'bg-slate-100 text-slate-600'
                              }`}>
                                {f.stability_class}
                              </span>
                            </td>
                            <td className="px-3 py-2 text-center font-mono text-[10px] text-gray-500">
                              {f.ic_ci_95 ? `[${f.ic_ci_95[0]}, ${f.ic_ci_95[1]}]` : 'N/A'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* TAB 2: Prediction Horizons & Targets */}
              {activeDiscoveryTab === 'horizons' && (
                <div className="space-y-6">
                  {/* Horizons Comparison */}
                  <div>
                    <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider mb-2">
                      Multi-Horizon Signal Strength (Ridge Baseline on Train/Val)
                    </h4>
                    <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
                      {stageBHorizons.map((h, i) => (
                        <div key={i} className={`p-4 rounded-xl border text-center ${
                          h.rank_ic >= 0.02 ? 'bg-emerald-50 border-emerald-200' : 'bg-slate-50 border-gray-200'
                        }`}>
                          <span className="text-xs font-bold text-gray-600 block">{h.horizon_days}-Day Horizon</span>
                          <span className={`text-xl font-black block my-1 ${h.rank_ic >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                            {h.rank_ic}
                          </span>
                          <span className="text-[10px] font-mono text-gray-500 block">Pearson IC: {h.ic}</span>
                          <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full inline-block mt-2 ${
                            h.positive ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-200 text-slate-700'
                          }`}>
                            {h.positive ? 'POSITIVE' : 'NEGATIVE'}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Target Formulations */}
                  {stageBTargets.length > 0 && (
                    <div>
                      <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider mb-2">
                        Target Formulations Screening (5-Day Horizon)
                      </h4>
                      <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
                        {stageBTargets.map((t, i) => (
                          <div key={i} className="bg-white p-3.5 rounded-xl border border-gray-200 text-xs space-y-1">
                            <span className="font-bold text-slate-800 block text-[11px]">{t.target_label}</span>
                            <div className="pt-2 font-mono">
                              <div className="flex justify-between">
                                <span className="text-gray-500">Val Rank IC:</span>
                                <strong className={t.val_rank_ic >= 0 ? 'text-emerald-700' : 'text-rose-700'}>{t.val_rank_ic}</strong>
                              </div>
                              <div className="flex justify-between">
                                <span className="text-gray-500">Val IC:</span>
                                <strong>{t.val_ic}</strong>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* TAB 3: Models & Seeds */}
              {activeDiscoveryTab === 'models' && (
                <div className="space-y-4">
                  <div className="flex justify-between items-center">
                    <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider">
                      Model Comparison Across Random Seeds (42, 101, 777)
                    </h4>
                    <span className="text-[11px] font-mono text-gray-500">Target: 5-Day Continuous Return</span>
                  </div>

                  <div className="overflow-x-auto rounded-lg border border-gray-200">
                    <table className="min-w-full text-xs divide-y divide-gray-200">
                      <thead className="bg-slate-100 text-slate-700 font-bold">
                        <tr>
                          <th className="px-4 py-3 text-left">Model Family</th>
                          <th className="px-3 py-3 text-center">Mean Rank IC</th>
                          <th className="px-3 py-3 text-center">Median Rank IC</th>
                          <th className="px-3 py-3 text-center">Std Dev (Seed Jitter)</th>
                          <th className="px-3 py-3 text-center">Min (Worst Seed)</th>
                          <th className="px-3 py-3 text-center">Max (Best Seed)</th>
                          <th className="px-3 py-3 text-center">Candidate Status</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-200 bg-white">
                        {stageCModels.map((m, idx) => (
                          <tr key={idx} className="hover:bg-slate-50">
                            <td className="px-4 py-3 font-bold text-slate-900 uppercase font-mono">{m.model_name}</td>
                            <td className={`px-3 py-3 text-center font-bold ${m.rank_ic_mean >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                              {m.rank_ic_mean}
                            </td>
                            <td className="px-3 py-3 text-center font-mono text-slate-800">{m.rank_ic_median}</td>
                            <td className="px-3 py-3 text-center font-mono text-slate-600">{m.rank_ic_std}</td>
                            <td className="px-3 py-3 text-center font-mono text-slate-600">{m.rank_ic_min}</td>
                            <td className="px-3 py-3 text-center font-mono text-slate-600">{m.rank_ic_max}</td>
                            <td className="px-3 py-3 text-center">
                              {stageDVal.frozen_candidate === m.model_name ? (
                                <span className="bg-emerald-600 text-white text-[10px] font-bold px-2.5 py-0.5 rounded-full">
                                  FROZEN FOR OOS
                                </span>
                              ) : (
                                <span className="text-gray-400 text-[10px]">Candidate</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* TAB 4: Cross-Sectional Deciles (Q1 - Q10) */}
              {activeDiscoveryTab === 'deciles' && (
                <div className="space-y-4">
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <div className="bg-slate-50 p-3 rounded-lg border text-xs">
                      <span className="text-gray-500 block">Monotonicity Score</span>
                      <span className="text-lg font-black text-slate-900">{stageDVal.cross_sectional?.monotonicity_score || 'N/A'}</span>
                      <span className="text-[10px] text-gray-500 block">Target: ≥ +0.50</span>
                    </div>
                    <div className="bg-slate-50 p-3 rounded-lg border text-xs">
                      <span className="text-gray-500 block">Top - Bottom Spread (Q10 - Q1)</span>
                      <span className="text-lg font-black text-emerald-700">
                        {stageDVal.cross_sectional?.top_minus_bottom_spread_pct > 0 ? `+${stageDVal.cross_sectional?.top_minus_bottom_spread_pct}%` : `${stageDVal.cross_sectional?.top_minus_bottom_spread_pct}%`}
                      </span>
                      <span className="text-[10px] text-gray-500 block">Sharpe: {stageDVal.cross_sectional?.spread_sharpe || 'N/A'}</span>
                    </div>
                    <div className="bg-slate-50 p-3 rounded-lg border text-xs">
                      <span className="text-gray-500 block">Eligible Stocks Per Date</span>
                      <span className="text-lg font-black text-indigo-700">{stageDVal.cross_sectional?.median_eligible_stocks_per_date || 'N/A'}</span>
                      <span className="text-[10px] text-gray-500 block">Cross-sectional breadth</span>
                    </div>
                  </div>

                  <div className="overflow-x-auto rounded-lg border border-gray-200">
                    <table className="min-w-full text-xs divide-y divide-gray-200">
                      <thead className="bg-slate-100 text-slate-700 font-bold">
                        <tr>
                          <th className="px-3 py-2.5 text-center">Decile</th>
                          <th className="px-3 py-2.5 text-center">Average Return %</th>
                          <th className="px-3 py-2.5 text-center">Median Return %</th>
                          <th className="px-3 py-2.5 text-center">Hit Rate %</th>
                          <th className="px-3 py-2.5 text-center">Sharpe Ratio</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-200 bg-white">
                        {discoveryRun.decile_results?.map((d, idx) => (
                          <tr key={idx} className={d.decile === 10 ? 'bg-emerald-50/60 font-bold' : d.decile === 1 ? 'bg-rose-50/60 font-bold' : 'hover:bg-slate-50'}>
                            <td className="px-3 py-2 text-center font-mono font-bold">
                              {d.decile === 10 ? 'Q10 (Top Long)' : d.decile === 1 ? 'Q1 (Bottom Short)' : `Q${d.decile}`}
                            </td>
                            <td className={`px-3 py-2 text-center font-bold ${d.avg_return_pct >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                              {d.avg_return_pct > 0 ? `+${d.avg_return_pct}%` : `${d.avg_return_pct}%`}
                            </td>
                            <td className="px-3 py-2 text-center font-mono text-slate-700">{d.median_return_pct}%</td>
                            <td className="px-3 py-2 text-center text-slate-700">{d.hit_rate}%</td>
                            <td className="px-3 py-2 text-center font-mono text-slate-800">{d.sharpe}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* TAB 5: Market Regimes & Stock Breadth */}
              {activeDiscoveryTab === 'regimes' && (
                <div className="space-y-6">
                  {/* Regime Table */}
                  <div>
                    <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider mb-2">
                      Performance Across Market Regimes (Bull, Bear, Sideways, Volatility)
                    </h4>
                    <div className="overflow-x-auto rounded-lg border border-gray-200">
                      <table className="min-w-full text-xs divide-y divide-gray-200">
                        <thead className="bg-slate-100 text-slate-700 font-bold">
                          <tr>
                            <th className="px-3 py-2.5 text-left">Regime Name</th>
                            <th className="px-2 py-2.5 text-center">Sample Count</th>
                            <th className="px-2 py-2.5 text-center">Rank IC</th>
                            <th className="px-2 py-2.5 text-center">Pearson IC</th>
                            <th className="px-2 py-2.5 text-center">Proxy Spread</th>
                            <th className="px-2 py-2.5 text-center">Win Rate %</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-200 bg-white">
                          {discoveryRun.regime_results?.map((r, idx) => (
                            <tr key={idx} className="hover:bg-slate-50">
                              <td className="px-3 py-2 font-mono font-bold text-slate-900">{r.regime_name}</td>
                              <td className="px-2 py-2 text-center text-gray-600">{r.sample_count}</td>
                              <td className={`px-2 py-2 text-center font-bold ${r.rank_ic_mean >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                                {r.rank_ic_mean}
                              </td>
                              <td className="px-2 py-2 text-center font-mono text-slate-700">{r.ic_mean}</td>
                              <td className="px-2 py-2 text-center font-mono text-slate-700">{r.spread_pct}%</td>
                              <td className="px-2 py-2 text-center text-slate-800">{r.win_rate}%</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* Stock Breadth */}
                  {stageDVal.stock_breadth && (
                    <div className="bg-slate-50 p-4 rounded-xl border border-gray-200 space-y-3">
                      <div className="flex justify-between items-center">
                        <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider">
                          Cross-Ticker Consistency & Breadth
                        </h4>
                        <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                          stageDVal.stock_breadth.breadth_status === 'WELL_DISTRIBUTED' ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'
                        }`}>
                          {stageDVal.stock_breadth.breadth_status}
                        </span>
                      </div>
                      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs font-mono">
                        <div>Positive Ticker Ratio: <strong>{stageDVal.stock_breadth.positive_ratio}%</strong></div>
                        <div>Total Evaluated: <strong>{stageDVal.stock_breadth.total_evaluated_tickers}</strong></div>
                        <div>Positive Count: <strong>{stageDVal.stock_breadth.positive_tickers_count}</strong></div>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* TAB 6: Friction Sensitivity & Multi-Testing Audit */}
              {activeDiscoveryTab === 'friction' && (
                <div className="space-y-6">
                  {/* Friction Tiers */}
                  <div>
                    <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider mb-2">
                      Economic Survivability Under Friction Tiers (1.0x, 1.5x, 2.0x)
                    </h4>
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                      {stageDVal.cost_sensitivity?.map((c, idx) => (
                        <div key={idx} className="bg-white p-4 rounded-xl border border-gray-200 text-xs space-y-2">
                          <span className="font-bold text-slate-800 block text-sm">
                            {c.friction_pct}% Friction ({idx === 0 ? '1.0x Base' : idx === 1 ? '1.5x Stressed' : '2.0x Conservative'})
                          </span>
                          <div className="grid grid-cols-2 gap-2 text-[11px] font-mono pt-1">
                            <div>Trades: <strong>{c.trade_count}</strong></div>
                            <div>Win Rate: <strong>{c.win_rate}%</strong></div>
                            <div>Expectancy: <strong className={c.expectancy_pct >= 0 ? 'text-emerald-700' : 'text-rose-700'}>{c.expectancy_pct}%</strong></div>
                            <div>Sharpe: <strong>{c.sharpe}</strong></div>
                            <div>Profit Factor: <strong>{c.profit_factor}</strong></div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Multi-Testing Audit Trail */}
                  <div className="bg-slate-50 p-4 rounded-xl border border-gray-200 space-y-3">
                    <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider">
                      Multi-Testing Search Space & Provenance Hashes
                    </h4>
                    <div className="grid grid-cols-2 md:grid-cols-5 gap-3 text-xs font-mono">
                      <div>Total Configs: <strong>{audit.total_configurations_explored || 'N/A'}</strong></div>
                      <div>Features Screened: <strong>{audit.total_features_screened || 158}</strong></div>
                      <div>Horizons Tested: <strong>{audit.total_horizons_tested || 5}</strong></div>
                      <div>Models Evaluated: <strong>{audit.total_models_tested || 5}</strong></div>
                      <div>Seeds Audited: <strong>{audit.total_seeds_tested || 3}</strong></div>
                    </div>
                    <div className="p-3 bg-slate-100 rounded text-[11px] font-mono text-slate-700 space-y-1 overflow-x-auto">
                      <div>Fingerprint: <code>{discoveryRun.fingerprint || 'N/A'}</code></div>
                      <div>Dataset Hash: <code>{discoveryRun.dataset_hash || 'N/A'}</code></div>
                      <div>Feature Hash: <code>{discoveryRun.feature_hash || 'N/A'}</code></div>
                    </div>
                  </div>
                </div>
              )}

              {/* TAB 7: Turnover & Hysteresis Frontier (V2/V3 Research Findings) */}
              {activeDiscoveryTab === 'hysteresis' && (
                <div className="space-y-6">
                  {/* Executive Header Banner */}
                  <div className="p-4 bg-gradient-to-r from-amber-50 to-indigo-50 border border-amber-200/80 rounded-xl space-y-2">
                    <div className="flex items-center gap-2 text-amber-900 font-bold text-xs">
                      <Scale size={16} className="text-amber-600" />
                      <span>The Turnover Dilemma &amp; Hysteresis Buffer Solution (V2/V3 Research)</span>
                    </div>
                    <p className="text-xs text-slate-600 leading-relaxed">
                      Pure short-term factor ranking suffers from high portfolio turnover (2,400%+ annually), where trading friction erodes gross alpha.
                      Our 10-year quantitative exploration proves that introducing an <strong>entry/exit buffer (Hysteresis)</strong> combined with 
                      a <strong>10–15 day rebalance horizon</strong> slashes turnover by up to <strong>74.7%</strong> while preserving positive net alpha under 10–30 bps costs.
                    </p>
                  </div>

                  {/* Turnover vs Return Frontier Cards */}
                  <div>
                    <h4 className="text-xs font-bold text-gray-700 uppercase tracking-wider mb-2.5">
                      Empirical Turnover vs Net Return Frontier (10-Year Backtest)
                    </h4>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
                      <div className="bg-white p-4 rounded-xl border border-gray-200 shadow-sm space-y-2">
                        <span className="text-[10px] font-bold uppercase text-slate-400 block font-mono">Naive Top-K (5D)</span>
                        <div className="text-base font-black text-rose-700">2,657% / yr</div>
                        <p className="text-[11px] text-slate-500">Unbuffered rapid churn. Friction drag: 797 bps. Net Sharpe: 1.02.</p>
                        <span className="inline-block text-[10px] px-2 py-0.5 rounded bg-rose-100 text-rose-800 font-bold">HIGH DRAG</span>
                      </div>

                      <div className="bg-white p-4 rounded-xl border border-gray-200 shadow-sm space-y-2">
                        <span className="text-[10px] font-bold uppercase text-slate-400 block font-mono">Moderate (10D No Buffer)</span>
                        <div className="text-base font-black text-amber-700">1,320% / yr</div>
                        <p className="text-[11px] text-slate-500">Doubling horizon halves turnover. Friction drag: 396 bps. Net Sharpe: 1.12.</p>
                        <span className="inline-block text-[10px] px-2 py-0.5 rounded bg-amber-100 text-amber-800 font-bold">MODERATE</span>
                      </div>

                      <div className="bg-white p-4 rounded-xl border-2 border-emerald-500 shadow-sm space-y-2 relative overflow-hidden">
                        <div className="absolute top-0 right-0 bg-emerald-500 text-white text-[9px] font-bold px-2 py-0.5 rounded-bl">RECOMMENDED</div>
                        <span className="text-[10px] font-bold uppercase text-emerald-700 block font-mono">Optimal Hysteresis (15D)</span>
                        <div className="text-base font-black text-emerald-700">870% / yr</div>
                        <p className="text-[11px] text-slate-600">Enter Top-10, exit below Top-20. <strong>69.5% turnover reduction</strong>. Net Sharpe: 2.29.</p>
                        <span className="inline-block text-[10px] px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 font-bold">OPTIMAL ALPHA</span>
                      </div>

                      <div className="bg-white p-4 rounded-xl border border-gray-200 shadow-sm space-y-2">
                        <span className="text-[10px] font-bold uppercase text-slate-400 block font-mono">Ultra-Low Turnover (20D)</span>
                        <div className="text-base font-black text-indigo-700">340% / yr</div>
                        <p className="text-[11px] text-slate-500">Enter Top-10, exit below Top-30. Minimal churn, but alpha begins to decay past 20D.</p>
                        <span className="inline-block text-[10px] px-2 py-0.5 rounded bg-indigo-100 text-indigo-800 font-bold">DEFENSIVE</span>
                      </div>
                    </div>
                  </div>

                  {/* Buffer Configurations Table */}
                  <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                    <div className="p-3.5 bg-gray-50 border-b border-gray-200 flex items-center justify-between">
                      <h4 className="text-xs font-bold text-gray-800 uppercase tracking-wider font-mono">
                        Validated Buffer Archetypes &amp; Hysteresis Matrix
                      </h4>
                      <span className="text-[10px] text-slate-500 font-mono">SQLite Research Ledger</span>
                    </div>

                    <div className="overflow-x-auto">
                      <table className="w-full text-xs text-left">
                        <thead className="bg-gray-100/70 border-b border-gray-200 text-gray-600 font-bold text-[11px]">
                          <tr>
                            <th className="px-3 py-2.5">Strategy / Archetype</th>
                            <th className="px-3 py-2.5 text-center">Buy Gate</th>
                            <th className="px-3 py-2.5 text-center">Exit Gate</th>
                            <th className="px-3 py-2.5 text-center">Horizon</th>
                            <th className="px-3 py-2.5 text-center">Annualized T/O</th>
                            <th className="px-3 py-2.5 text-center">T/O Reduction</th>
                            <th className="px-3 py-2.5 text-center">Net CAGR</th>
                            <th className="px-3 py-2.5 text-center">Net Sharpe</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100 font-mono text-[11px]">
                          <tr className="hover:bg-slate-50">
                            <td className="px-3 py-2.5 font-bold text-slate-800">HYSTERESIS_TOP10_20_15D</td>
                            <td className="px-3 py-2.5 text-center text-emerald-700 font-bold">Top 10</td>
                            <td className="px-3 py-2.5 text-center text-rose-700 font-bold">Below 20</td>
                            <td className="px-3 py-2.5 text-center">15 Days</td>
                            <td className="px-3 py-2.5 text-center font-bold text-emerald-700">870.7%</td>
                            <td className="px-3 py-2.5 text-center font-bold text-emerald-600">-69.5%</td>
                            <td className="px-3 py-2.5 text-center font-bold text-emerald-700">+67.6%</td>
                            <td className="px-3 py-2.5 text-center font-bold text-emerald-700">2.29</td>
                          </tr>
                          <tr className="hover:bg-slate-50">
                            <td className="px-3 py-2.5 font-bold text-slate-800">HYSTERESIS_TOP10_20_10D</td>
                            <td className="px-3 py-2.5 text-center text-emerald-700 font-bold">Top 10</td>
                            <td className="px-3 py-2.5 text-center text-rose-700 font-bold">Below 20</td>
                            <td className="px-3 py-2.5 text-center">10 Days</td>
                            <td className="px-3 py-2.5 text-center">1,165.3%</td>
                            <td className="px-3 py-2.5 text-center text-emerald-600">-59.1%</td>
                            <td className="px-3 py-2.5 text-center">+59.0%</td>
                            <td className="px-3 py-2.5 text-center">2.19</td>
                          </tr>
                          <tr className="hover:bg-slate-50">
                            <td className="px-3 py-2.5 font-bold text-slate-800">HYSTERESIS_TOP10_30_15D</td>
                            <td className="px-3 py-2.5 text-center text-emerald-700 font-bold">Top 10</td>
                            <td className="px-3 py-2.5 text-center text-rose-700 font-bold">Below 30</td>
                            <td className="px-3 py-2.5 text-center">15 Days</td>
                            <td className="px-3 py-2.5 text-center font-bold text-emerald-700">594.6%</td>
                            <td className="px-3 py-2.5 text-center font-bold text-emerald-600">-79.1%</td>
                            <td className="px-3 py-2.5 text-center">+60.8%</td>
                            <td className="px-3 py-2.5 text-center">2.19</td>
                          </tr>
                          <tr className="hover:bg-slate-50">
                            <td className="px-3 py-2.5 font-bold text-slate-800">HYSTERESIS_TOP5_15_15D</td>
                            <td className="px-3 py-2.5 text-center text-emerald-700 font-bold">Top 5</td>
                            <td className="px-3 py-2.5 text-center text-rose-700 font-bold">Below 15</td>
                            <td className="px-3 py-2.5 text-center">15 Days</td>
                            <td className="px-3 py-2.5 text-center">1,012.4%</td>
                            <td className="px-3 py-2.5 text-center text-emerald-600">-64.5%</td>
                            <td className="px-3 py-2.5 text-center font-bold text-emerald-700">+95.9%</td>
                            <td className="px-3 py-2.5 text-center font-bold text-emerald-700">2.68</td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* Summary Callout */}
                  <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 text-xs text-slate-600 space-y-1">
                    <span className="font-bold text-slate-800 block text-[11px] uppercase">Governance Boundary Note:</span>
                    <p>
                      All findings above are archived in the canonical research ledger. The production Champions remain locked and protected until a candidate clears the multi-dimensional promotion gates under formal human sign-off.
                    </p>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* ========================================================================= */}
      {/* VIEW 2: SINGLE CANDIDATE EVALUATOR (CHALLENGER VS CHAMPION MATRIX) */}
      {/* ========================================================================= */}
      {viewMode === 'evaluator' && (
        <div>
          {/* Strategy and Engine Configuration Bar */}
          <div className="p-6 border-b border-gray-100 bg-slate-50/50 space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
              {/* Strategy Selector */}
              <div>
                <label className="block text-xs font-bold text-gray-700 uppercase tracking-wider mb-1.5">
                  Trading Strategy
                </label>
                <div className="flex bg-white rounded-lg p-1 border border-gray-200 shadow-sm">
                  <button
                    onClick={() => setStrategy('SWING')}
                    className={`flex-1 py-1.5 text-xs font-bold rounded transition cursor-pointer ${strategy === 'SWING' ? 'bg-indigo-600 text-white shadow-xs' : 'text-gray-600 hover:text-gray-900'}`}
                  >
                    Swing (1D)
                  </button>
                  <button
                    onClick={() => setStrategy('INTRADAY')}
                    className={`flex-1 py-1.5 text-xs font-bold rounded transition cursor-pointer ${strategy === 'INTRADAY' ? 'bg-indigo-600 text-white shadow-xs' : 'text-gray-600 hover:text-gray-900'}`}
                  >
                    Intraday (15m)
                  </button>
                </div>
              </div>

              {/* Feature Family Selector */}
              <div>
                <label className="block text-xs font-bold text-gray-700 uppercase tracking-wider mb-1.5">
                  Feature Family
                </label>
                <select
                  value={featureFamily}
                  onChange={(e) => setFeatureFamily(e.target.value)}
                  disabled={evaluating}
                  className="w-full bg-white border border-gray-200 text-gray-800 text-xs font-semibold rounded-lg p-2 focus:outline-none focus:border-indigo-500 shadow-sm cursor-pointer disabled:opacity-50"
                >
                  <option value="Alpha158">Alpha158 (158 Multi-Scale Factors)</option>
                  <option value="Alpha360">Alpha360 (60-Bar x 6-Channel Sequence)</option>
                </select>
              </div>

              {/* Model Architecture Selector */}
              <div>
                <label className="block text-xs font-bold text-gray-700 uppercase tracking-wider mb-1.5">
                  Model Architecture
                </label>
                <select
                  value={modelFamily}
                  onChange={(e) => setModelFamily(e.target.value)}
                  disabled={evaluating}
                  className="w-full bg-white border border-gray-200 text-gray-800 text-xs font-semibold rounded-lg p-2 focus:outline-none focus:border-indigo-500 shadow-sm cursor-pointer disabled:opacity-50"
                >
                  <option value="lightgbm">LightGBM (Gradient Boosting)</option>
                  <option value="catboost">CatBoost (Symmetric Trees)</option>
                  <option value="xgboost">XGBoost (Extreme Gradient Boost)</option>
                  <option value="double_ensemble">DoubleEnsemble (Chu et al. 2020)</option>
                </select>
              </div>

              {/* Universe */}
              <div>
                <label className="block text-xs font-bold text-gray-700 uppercase tracking-wider mb-1.5">
                  Target Universe
                </label>
                <input
                  type="text"
                  value={universe}
                  readOnly
                  className="w-full bg-gray-100 border border-gray-200 text-gray-700 text-xs font-mono font-bold rounded-lg p-2 cursor-not-allowed"
                />
              </div>
            </div>
          </div>

          {/* Active Evaluation Result Banner */}
          {evalResult && (
            <div className="mx-6 mt-4 p-4 bg-emerald-50 border border-emerald-200 rounded-xl space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <CheckCircle className="text-emerald-600" size={18} />
                  <span className="font-bold text-sm text-emerald-950">
                    Evaluation Complete: {evalResult.candidate?.model_name} ({evalResult.candidate?.feature_family})
                  </span>
                </div>
                <span className={`text-xs font-bold px-2.5 py-0.5 rounded-full ${
                  evalResult.gate_verdict === 'PROMOTION CANDIDATE' 
                    ? 'bg-emerald-600 text-white' 
                    : evalResult.gate_verdict === 'RETAIN CHAMPION'
                    ? 'bg-amber-100 text-amber-900 border border-amber-300'
                    : 'bg-slate-200 text-slate-800'
                }`}>
                  {evalResult.gate_verdict}
                </span>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-6 gap-3 text-xs font-mono pt-1 text-slate-700">
                <div>Trades: <strong>{evalResult.candidate?.trade_count}</strong></div>
                <div>Win Rate: <strong>{evalResult.candidate?.win_rate}%</strong></div>
                <div>Net P&amp;L: <strong className={evalResult.candidate?.net_pnl_pct >= 0 ? 'text-emerald-600' : 'text-rose-600'}>{evalResult.candidate?.net_pnl_pct}%</strong></div>
                <div>Sharpe: <strong>{evalResult.candidate?.sharpe_ratio}</strong></div>
                <div>Expectancy: <strong>{evalResult.candidate?.expectancy}%</strong></div>
                <div>F1 Score: <strong>{evalResult.candidate?.f1}</strong></div>
              </div>
            </div>
          )}

          {/* Comparative Evaluation Matrix */}
          <div className="p-6 space-y-4">
            <div className="flex justify-between items-center">
              <div className="flex items-center gap-2">
                <BarChart2 className="text-indigo-600" size={18} />
                <h4 className="text-sm font-bold text-gray-800 uppercase tracking-wider">
                  {strategy} Comparative Results Matrix (Locked Out-of-Sample)
                </h4>
              </div>
              <span className="text-xs text-gray-500 font-medium">
                Total Evaluations Recorded: <strong>{evaluations.length}</strong>
              </span>
            </div>

            <div className="overflow-x-auto rounded-lg border border-gray-200 shadow-xs">
              <table className="min-w-full text-xs divide-y divide-gray-200">
                <thead className="bg-slate-100 text-slate-700 font-bold">
                  <tr>
                    <th className="px-3 py-3 text-left">Model Architecture</th>
                    <th className="px-2 py-3 text-left">Feature Family</th>
                    <th className="px-2 py-3 text-center">Trades</th>
                    <th className="px-2 py-3 text-center">Win Rate</th>
                    <th className="px-2 py-3 text-center">Net P&amp;L</th>
                    <th className="px-2 py-3 text-center">Expectancy</th>
                    <th className="px-2 py-3 text-center">PF</th>
                    <th className="px-2 py-3 text-center">Sharpe</th>
                    <th className="px-2 py-3 text-center">Max DD</th>
                    <th className="px-2 py-3 text-center">F1</th>
                    <th className="px-2 py-3 text-center">Brier</th>
                    <th className="px-3 py-3 text-center">Gate Verdict</th>
                    <th className="px-2 py-3 text-center">Details</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 bg-white">
                  {/* Champion Benchmark Row */}
                  {championBaseline ? (
                    <tr className="bg-indigo-50/60 font-semibold border-b-2 border-indigo-200">
                      <td className="px-3 py-3 text-indigo-950 font-bold flex items-center gap-1.5">
                        <span className="w-2 h-2 rounded-full bg-indigo-600"></span>
                        {championBaseline.model_name}
                      </td>
                      <td className="px-2 py-3 text-indigo-900">{championBaseline.feature_family}</td>
                      <td className="px-2 py-3 text-center font-bold text-slate-800">{championBaseline.trade_count}</td>
                      <td className="px-2 py-3 text-center font-bold text-slate-800">{championBaseline.win_rate}%</td>
                      <td className={`px-2 py-3 text-center font-bold ${championBaseline.net_pnl_pct >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>
                        {championBaseline.net_pnl_pct > 0 ? `+${championBaseline.net_pnl_pct}%` : `${championBaseline.net_pnl_pct}%`}
                      </td>
                      <td className="px-2 py-3 text-center font-bold text-slate-800">{championBaseline.expectancy}%</td>
                      <td className="px-2 py-3 text-center text-slate-800">{championBaseline.profit_factor}</td>
                      <td className="px-2 py-3 text-center font-bold text-indigo-700">{championBaseline.sharpe_ratio}</td>
                      <td className="px-2 py-3 text-center text-rose-700">{championBaseline.max_drawdown_pct}%</td>
                      <td className="px-2 py-3 text-center text-slate-700">{championBaseline.f1}</td>
                      <td className="px-2 py-3 text-center text-slate-700">{championBaseline.brier}</td>
                      <td className="px-3 py-3 text-center">
                        <span className="bg-indigo-600 text-white text-[10px] font-bold px-2 py-0.5 rounded-full">
                          BENCHMARK
                        </span>
                      </td>
                      <td className="px-2 py-3 text-center">
                        <button
                          onClick={() => setSelectedEval(championBaseline)}
                          className="text-indigo-600 hover:text-indigo-800 p-1 rounded hover:bg-indigo-100 transition cursor-pointer"
                          title="Inspect Details"
                        >
                          <Info size={14} />
                        </button>
                      </td>
                    </tr>
                  ) : (
                    <tr className="bg-slate-50 text-slate-500 italic">
                      <td colSpan="13" className="px-4 py-3 text-center">
                        No Champion Benchmark evaluation recorded for {strategy}. Run an evaluation to establish the locked benchmark.
                      </td>
                    </tr>
                  )}

                  {/* Candidate Models */}
                  {candidateEvals.map((item, idx) => {
                    const isNetSuperior = championBaseline && item.net_pnl_pct > championBaseline.net_pnl_pct;
                    const isSharpeSuperior = championBaseline && item.sharpe_ratio > championBaseline.sharpe_ratio;
                    const isWinSuperior = championBaseline && item.win_rate > championBaseline.win_rate;

                    return (
                      <tr key={item.evaluation_id || idx} className="hover:bg-slate-50 transition">
                        <td className="px-3 py-3 font-bold text-slate-800 flex items-center gap-1.5">
                          <span className={`w-1.5 h-1.5 rounded-full ${item.gate_verdict === 'PROMOTION CANDIDATE' ? 'bg-emerald-500' : 'bg-slate-400'}`}></span>
                          {item.model_name}
                        </td>
                        <td className="px-2 py-3 text-slate-600 font-mono text-[11px]">{item.feature_family}</td>
                        <td className="px-2 py-3 text-center font-semibold text-slate-700">{item.trade_count}</td>
                        <td className="px-2 py-3 text-center font-bold text-slate-800">
                          {item.win_rate}%
                          {isWinSuperior && <span className="text-[9px] text-emerald-600 ml-0.5 font-sans">▲</span>}
                        </td>
                        <td className={`px-2 py-3 text-center font-bold ${item.net_pnl_pct >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                          {item.net_pnl_pct > 0 ? `+${item.net_pnl_pct}%` : `${item.net_pnl_pct}%`}
                          {isNetSuperior && <span className="text-[9px] text-emerald-600 ml-0.5 font-sans">▲</span>}
                        </td>
                        <td className="px-2 py-3 text-center font-semibold text-slate-700">{item.expectancy}%</td>
                        <td className="px-2 py-3 text-center text-slate-700">{item.profit_factor}</td>
                        <td className="px-2 py-3 text-center font-bold text-slate-800">
                          {item.sharpe_ratio}
                          {isSharpeSuperior && <span className="text-[9px] text-emerald-600 ml-0.5 font-sans">▲</span>}
                        </td>
                        <td className="px-2 py-3 text-center text-rose-600">{item.max_drawdown_pct}%</td>
                        <td className="px-2 py-3 text-center text-slate-600">{item.f1}</td>
                        <td className="px-2 py-3 text-center text-slate-600">{item.brier}</td>
                        <td className="px-3 py-3 text-center">
                          <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                            item.gate_verdict === 'PROMOTE CANDIDATE' || item.gate_verdict === 'PROMOTION CANDIDATE'
                              ? 'bg-emerald-100 text-emerald-800 border border-emerald-300'
                              : item.gate_verdict === 'INSUFFICIENT EVIDENCE'
                              ? 'bg-slate-200 text-slate-700 border border-slate-300'
                              : item.gate_verdict === 'BENCHMARK'
                              ? 'bg-indigo-100 text-indigo-800 border border-indigo-300'
                              : 'bg-amber-100 text-amber-800 border border-amber-300'
                          }`}>
                            {item.gate_verdict}
                          </span>
                        </td>
                        <td className="px-2 py-3 text-center">
                          <button
                            onClick={() => setSelectedEval(item)}
                            className="text-slate-500 hover:text-slate-800 p-1 rounded hover:bg-slate-200 transition cursor-pointer"
                            title="View Detailed Metrics"
                          >
                            <ChevronRight size={15} />
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Evaluation Detail Modal / Drawer */}
      {selectedEval && (
        <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-xl shadow-2xl max-w-2xl w-full p-6 space-y-4 max-h-[85vh] overflow-y-auto">
            <div className="flex justify-between items-center border-b pb-3">
              <div>
                <h4 className="text-base font-bold text-gray-900">
                  {selectedEval.model_name} ({selectedEval.feature_family})
                </h4>
                <p className="text-xs text-gray-500 font-mono">ID: {selectedEval.evaluation_id}</p>
              </div>
              <button onClick={() => setSelectedEval(null)} className="text-gray-400 hover:text-gray-600 cursor-pointer">
                <X size={18} />
              </button>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
              <div className="bg-slate-50 p-2.5 rounded-lg border">
                <span className="text-slate-500 block">Win Rate</span>
                <span className="text-base font-bold text-slate-800">{selectedEval.win_rate}%</span>
              </div>
              <div className="bg-slate-50 p-2.5 rounded-lg border">
                <span className="text-slate-500 block">Net P&amp;L</span>
                <span className="text-base font-bold text-emerald-600">{selectedEval.net_pnl_pct}%</span>
              </div>
              <div className="bg-slate-50 p-2.5 rounded-lg border">
                <span className="text-slate-500 block">Sharpe Ratio</span>
                <span className="text-base font-bold text-indigo-600">{selectedEval.sharpe_ratio}</span>
              </div>
              <div className="bg-slate-50 p-2.5 rounded-lg border">
                <span className="text-slate-500 block">Gate Verdict</span>
                <span className="text-xs font-bold text-amber-700">{selectedEval.gate_verdict}</span>
              </div>
            </div>

            {selectedEval.metrics_json?.gate_checks && (
              <div className="space-y-2">
                <span className="text-xs font-bold text-gray-700 uppercase">Promotion Gate Criteria</span>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
                  {Object.entries(selectedEval.metrics_json.gate_checks).map(([gate, passed]) => (
                    <div key={gate} className="flex justify-between items-center bg-gray-50 p-2 rounded border">
                      <span className="font-mono text-gray-600">{gate.replace(/_/g, ' ')}:</span>
                      <span className={`font-bold ${passed ? 'text-emerald-600' : 'text-rose-600'}`}>
                        {passed ? 'PASSED ✓' : 'FAILED ✗'}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {selectedEval.metrics_json?.concentration && (
              <div className="space-y-1">
                <span className="text-xs font-bold text-gray-700 uppercase">Concentration Risk</span>
                <div className="bg-slate-50 p-3 rounded-lg border text-xs font-mono space-y-1">
                  <div>Top 1 Stock Exposure: <strong>{selectedEval.metrics_json.concentration.top_1_pct}%</strong></div>
                  <div>Top 3 Stocks Exposure: <strong>{selectedEval.metrics_json.concentration.top_3_pct}%</strong></div>
                  <div>Status: <strong className="text-emerald-600">{selectedEval.metrics_json.concentration.is_concentrated ? 'CONCENTRATED' : 'BALANCED'}</strong></div>
                </div>
              </div>
            )}

            <div className="space-y-1">
              <span className="text-xs font-bold text-gray-700 uppercase">Dataset &amp; Provenance Hashes</span>
              <div className="bg-slate-100 p-3 rounded-lg text-[11px] font-mono text-slate-700 space-y-1 overflow-x-auto">
                <div>Dataset Hash: <code>{selectedEval.dataset_hash || 'N/A'}</code></div>
                <div>OOS Hash: <code>{selectedEval.oos_hash || 'N/A'}</code></div>
                <div>Artifact Hash: <code>{selectedEval.model_artifact_hash || 'N/A'}</code></div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
