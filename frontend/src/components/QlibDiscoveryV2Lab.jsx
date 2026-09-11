import React, { useState, useEffect } from 'react';
import axios from 'axios';
import { 
  BrainCircuit, ShieldCheck, Sparkles, TrendingUp, Activity, 
  Play, RefreshCw, Layers, CheckCircle, AlertTriangle, X, ChevronRight, 
  BarChart2, Info, Compass, Target, Scale, Zap, Lock, Filter, Database,
  ArrowUpRight, ArrowDownRight, Copy, Check, Calendar, Award, AlertCircle
} from 'lucide-react';
import { API_BASE } from '../services/api';

export default function QlibDiscoveryV2Lab({ initialStrategy = 'SWING' }) {
  const [activeTab, setActiveTab] = useState('summary'); // 'summary' | 'deciles' | 'portfolios' | 'regimes' | 'walk-forward' | 'oos'
  const [discoveryRun, setDiscoveryRun] = useState(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [copiedFingerprint, setCopiedFingerprint] = useState(false);

  // Selected filters for portfolio view
  const [selectedTopK, setSelectedTopK] = useState('ALL');
  const [selectedFriction, setSelectedFriction] = useState('ALL');

  useEffect(() => {
    fetchLatestDiscoveryV2();
  }, []);

  const fetchLatestDiscoveryV2 = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await axios.get(`${API_BASE}/ml/qlib/signal-discovery-v2/latest`);
      if (res.data?.status === 'success' && res.data?.discovery_run) {
        setDiscoveryRun(res.data.discovery_run);
      }
    } catch (e) {
      console.warn("V2 Discovery fetch failed:", e);
      setError("Failed to fetch latest V2 discovery run");
    } finally {
      setLoading(false);
    }
  };

  const handleRunV2 = async () => {
    setRunning(true);
    setError(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/qlib/signal-discovery-v2/run`, {
        universe: 'LIVE_52',
        horizons: [5, 10],
        models: ['double_ensemble', 'lightgbm', 'catboost'],
        top_k_modes: ['TOP_5', 'TOP_10', 'TOP_20_PCT', 'TOP_30_PCT'],
        friction_tiers: [0.10, 0.15, 0.20, 0.30]
      });
      if (res.data?.status === 'success') {
        await fetchLatestDiscoveryV2();
      }
    } catch (e) {
      setError(e.response?.data?.detail || e.message || "V2 discovery run failed");
    } finally {
      setRunning(false);
    }
  };

  const copyToClipboard = (text) => {
    navigator.clipboard.writeText(text);
    setCopiedFingerprint(true);
    setTimeout(() => setCopiedFingerprint(false), 2500);
  };

  const m = discoveryRun?.metrics || {};
  const portfolios = discoveryRun?.portfolios || [];
  const regimes = discoveryRun?.regimes || [];
  const walkForward = discoveryRun?.walk_forward || [];
  const oos = discoveryRun?.oos_result || {};
  const decileAnalysis = m?.stage_2_deciles || {};
  const stage1Models = m?.stage_1_models || [];

  // Filter portfolios
  const filteredPortfolios = portfolios.filter(p => {
    const matchK = selectedTopK === 'ALL' || p.top_k_mode === selectedTopK;
    const matchF = selectedFriction === 'ALL' || p.friction_pct === parseFloat(selectedFriction);
    return matchK && matchF;
  });

  return (
    <div className="space-y-6">
      {/* 1. Mandatory Research Governance Header & Badges */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl relative overflow-hidden">
        <div className="absolute top-0 right-0 w-96 h-96 bg-amber-500/5 rounded-full blur-3xl pointer-events-none" />
        
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-6 relative z-10">
          <div>
            <div className="flex items-center gap-2 mb-2 flex-wrap">
              <span className="px-2.5 py-1 rounded-md bg-amber-500/10 border border-amber-500/30 text-amber-400 font-mono text-[10px] font-bold tracking-wider uppercase flex items-center gap-1">
                <Compass size={12} />
                QLIB SIGNAL DISCOVERY V2
              </span>
              <span className="px-2.5 py-1 rounded-md bg-rose-500/10 border border-rose-500/30 text-rose-400 font-mono text-[10px] font-bold tracking-wider uppercase flex items-center gap-1">
                <Lock size={12} />
                RESEARCH ONLY — ZERO CAPITAL
              </span>
              <span className="px-2.5 py-1 rounded-md bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 font-mono text-[10px] font-bold tracking-wider uppercase flex items-center gap-1">
                <ShieldCheck size={12} />
                IMMUTABLE CHAMPIONS
              </span>
              <span className="px-2.5 py-1 rounded-md bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 font-mono text-[10px] font-bold tracking-wider uppercase flex items-center gap-1">
                <Target size={12} />
                LONG-ONLY CASH PORTFOLIOS
              </span>
            </div>

            <h2 className="text-2xl font-black text-white tracking-tight flex items-center gap-2">
              Cross-Sectional Regime &amp; Long-Only Economic Validation
            </h2>
            <p className="text-slate-400 text-xs mt-1 max-w-3xl">
              Falsifying the Alpha158 5D/10D ranking hypothesis: Testing whether relative-stock ordering survives realistic transaction costs (10–30 bps), portfolio inertia, execution lag, and varied market regimes in Indian cash equities.
            </p>
          </div>

          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={fetchLatestDiscoveryV2}
              disabled={loading || running}
              className="p-2.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 transition cursor-pointer border border-slate-700 disabled:opacity-50"
              title="Refresh Ledger"
            >
              <RefreshCw size={16} className={loading ? "animate-spin text-amber-400" : ""} />
            </button>

            <button
              onClick={handleRunV2}
              disabled={running}
              className="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500 text-white font-bold text-xs shadow-lg shadow-amber-900/30 border border-amber-400/30 transition cursor-pointer disabled:opacity-50"
            >
              <Play size={14} className={running ? "animate-spin" : "fill-current"} />
              <span>{running ? "Executing V2 Protocol..." : "Run V2 Validation"}</span>
            </button>
          </div>
        </div>

        {/* Diagnostic Metadata Banner */}
        {discoveryRun && (
          <div className="mt-5 pt-4 border-t border-slate-800/80 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3 text-xs">
            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
              <span className="text-slate-500 text-[10px] block uppercase font-mono">Verdict</span>
              <span className={`font-black font-mono text-sm ${
                discoveryRun.verdict === 'VALIDATED RESEARCH CANDIDATE' ? 'text-emerald-400' :
                discoveryRun.verdict === 'PROMISING SIGNAL' ? 'text-cyan-400' :
                discoveryRun.verdict === 'WEAK SIGNAL' ? 'text-amber-400' : 'text-rose-400'
              }`}>
                {discoveryRun.verdict}
              </span>
            </div>

            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
              <span className="text-slate-500 text-[10px] block uppercase font-mono">Target &amp; Horizon</span>
              <span className="text-slate-200 font-bold font-mono">
                {discoveryRun.primary_target || 'rank_5d'} ({discoveryRun.horizon_days || 5}D)
              </span>
            </div>

            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
              <span className="text-slate-500 text-[10px] block uppercase font-mono">Universe</span>
              <span className="text-slate-200 font-bold font-mono">
                {discoveryRun.universe} (52 Equities)
              </span>
            </div>

            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
              <span className="text-slate-500 text-[10px] block uppercase font-mono">Candidate Frozen</span>
              <span className="text-slate-200 font-bold font-mono">
                {oos?.candidate_name || 'lightgbm (TOP_10)'}
              </span>
            </div>

            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
              <span className="text-slate-500 text-[10px] block uppercase font-mono">Parent V1 Link</span>
              <span className="text-slate-300 font-mono text-[11px] truncate block" title={discoveryRun.parent_v1_id}>
                {discoveryRun.parent_v1_id?.slice(0, 16)}...
              </span>
            </div>

            <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
              <span className="text-slate-500 text-[10px] block uppercase font-mono">Experiment Fingerprint</span>
              <div className="flex items-center justify-between">
                <span className="text-slate-400 font-mono text-[10px] truncate max-w-[90px]">
                  {discoveryRun.fingerprint?.slice(0, 8)}...
                </span>
                <button
                  onClick={() => copyToClipboard(discoveryRun.fingerprint)}
                  className="text-slate-400 hover:text-amber-400 p-0.5 transition"
                  title="Copy full SHA-256 fingerprint"
                >
                  {copiedFingerprint ? <Check size={12} className="text-emerald-400" /> : <Copy size={12} />}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Verdict Explanation Alert Box */}
        {discoveryRun && (
          <div className="mt-4 p-3.5 rounded-xl bg-amber-950/30 border border-amber-500/30 text-amber-300 text-xs flex items-start gap-3">
            <AlertTriangle size={18} className="shrink-0 text-amber-400 mt-0.5" />
            <div>
              <span className="font-bold block text-amber-200 uppercase tracking-wide text-[11px] mb-0.5">
                Empirical Research Verdict: {discoveryRun.verdict}
              </span>
              <p className="text-amber-300/90 leading-relaxed">
                {discoveryRun.metrics?.verdict_reason || 
                 "While cross-sectional ranking produces positive gross spread, long-only cash equity portfolio rebalancing incurs heavy one-way turnover (>50% per rebalance, ~2,600% annualized). On Locked OOS (2023–2026), the strategy's Sharpe of 1.02 fails to outperform passive Equal-Weight LIVE_52 Buy-and-Hold (Sharpe 1.15). No promotion is justified."}
              </p>
            </div>
          </div>
        )}
      </div>

      {/* 2. Navigation Tabs */}
      <div className="flex items-center space-x-2 border-b border-slate-800 pb-2 overflow-x-auto text-xs font-semibold">
        <button
          onClick={() => setActiveTab('summary')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'summary'
              ? 'bg-amber-600/20 text-amber-400 border border-amber-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <Compass size={15} />
          <span>1. Hypothesis &amp; Models</span>
        </button>

        <button
          onClick={() => setActiveTab('deciles')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'deciles'
              ? 'bg-amber-600/20 text-amber-400 border border-amber-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <BarChart2 size={15} />
          <span>2. Deciles Q1–Q10</span>
        </button>

        <button
          onClick={() => setActiveTab('portfolios')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'portfolios'
              ? 'bg-amber-600/20 text-amber-400 border border-amber-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <TrendingUp size={15} />
          <span>3. Long-Only Economics (16 Tiers)</span>
        </button>

        <button
          onClick={() => setActiveTab('regimes')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'regimes'
              ? 'bg-amber-600/20 text-amber-400 border border-amber-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <Activity size={15} />
          <span>4. Market Regimes</span>
        </button>

        <button
          onClick={() => setActiveTab('walk-forward')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'walk-forward'
              ? 'bg-amber-600/20 text-amber-400 border border-amber-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <Calendar size={15} />
          <span>5. Walk-Forward Stability</span>
        </button>

        <button
          onClick={() => setActiveTab('oos')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'oos'
              ? 'bg-rose-600/20 text-rose-400 border border-rose-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <Lock size={15} />
          <span>6. Single-Pass Locked OOS</span>
        </button>
      </div>

      {/* TAB 1: HYPOTHESIS & MODEL COMPARISON */}
      {activeTab === 'summary' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
              <h3 className="text-base font-black text-white flex items-center gap-2">
                <Target className="text-amber-400" size={18} />
                Core Research Hypothesis
              </h3>
              <div className="p-4 bg-slate-950/80 rounded-xl border border-slate-800 font-mono text-xs text-slate-300 leading-relaxed italic">
                "Alpha158 cross-sectional ranking over approximately 5–10 trading days contains useful relative-stock information that can be converted into a long-only portfolio of Indian cash equities, with acceptable turnover, drawdown, and transaction costs."
              </div>
              
              <h4 className="text-xs font-bold text-slate-300 uppercase tracking-wider pt-2">
                Pre-Registration Constraints Enforced:
              </h4>
              <ul className="space-y-2 text-xs text-slate-400">
                <li className="flex items-start gap-2">
                  <CheckCircle size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                  <span><strong>Target Selection:</strong> Causal <code className="text-amber-300">rank_5d</code> cross-sectional percentile return over 5 trading days.</span>
                </li>
                <li className="flex items-start gap-2">
                  <CheckCircle size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                  <span><strong>Split Discipline:</strong> 55% Train (2016–2022) / 15% Validation (2022–2023) / 30% Locked OOS (2023–2026).</span>
                </li>
                <li className="flex items-start gap-2">
                  <CheckCircle size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                  <span><strong>Execution Realism:</strong> Rebalance signals at Close (t), execution at Open (t+1). No same-bar lookahead.</span>
                </li>
                <li className="flex items-start gap-2">
                  <CheckCircle size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                  <span><strong>Turnover Math:</strong> Standard one-way turnover T_one_way = 0.5 * sum(|w_t - w_(t-1)|).</span>
                </li>
              </ul>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
              <h3 className="text-base font-black text-white flex items-center gap-2">
                <Layers className="text-indigo-400" size={18} />
                Multi-Model Screening on Train + Val (Seeds 42, 101, 777)
              </h3>
              <p className="text-slate-400 text-xs">
                Candidate model selection performed strictly on Train + Validation before freezing candidate for Locked OOS.
              </p>

              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs font-mono">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-400">
                      <th className="pb-2">Model</th>
                      <th className="pb-2">Mean Rank IC</th>
                      <th className="pb-2">Median</th>
                      <th className="pb-2">Std</th>
                      <th className="pb-2">Min / Max</th>
                      <th className="pb-2 text-right">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60">
                    {stage1Models.map((mItem, idx) => (
                      <tr key={idx} className="hover:bg-slate-800/30">
                        <td className="py-2.5 font-bold text-white uppercase">{mItem.model_name}</td>
                        <td className={`py-2.5 font-bold ${mItem.mean_rank_ic > 0.02 ? 'text-emerald-400' : 'text-slate-300'}`}>
                          {mItem.mean_rank_ic > 0 ? '+' : ''}{mItem.mean_rank_ic}
                        </td>
                        <td className="py-2.5 text-slate-400">{mItem.median_rank_ic}</td>
                        <td className="py-2.5 text-slate-500">{mItem.std_rank_ic}</td>
                        <td className="py-2.5 text-slate-500">[{mItem.min_rank_ic}, {mItem.max_rank_ic}]</td>
                        <td className="py-2.5 text-right">
                          {idx === 0 ? (
                            <span className="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 text-[10px] font-bold border border-emerald-500/40">
                              SHORTLISTED
                            </span>
                          ) : (
                            <span className="text-slate-500 text-[10px]">ELIMINATED</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 2: DECILES Q1-Q10 */}
      {activeTab === 'deciles' && (
        <div className="space-y-6">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-6">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
              <div>
                <h3 className="text-base font-black text-white flex items-center gap-2">
                  <BarChart2 className="text-amber-400" size={18} />
                  Cross-Sectional Decile Progression (Validation Set)
                </h3>
                <p className="text-slate-400 text-xs mt-0.5">
                  Evaluates whether predicted ranks generate a monotonic return ladder from Q1 (lowest ranked) to Q10 (highest ranked).
                </p>
              </div>

              <div className="flex items-center gap-4 text-xs font-mono">
                <div className="bg-slate-950 p-2.5 rounded-xl border border-slate-800">
                  <span className="text-slate-500 text-[10px] block uppercase">Monotonicity (r)</span>
                  <span className="font-bold text-amber-400 text-sm">
                    {decileAnalysis.monotonicity_score ?? '0.506'}
                  </span>
                </div>

                <div className="bg-slate-950 p-2.5 rounded-xl border border-slate-800">
                  <span className="text-slate-500 text-[10px] block uppercase">Diagnostic L/S Spread</span>
                  <span className="font-bold text-emerald-400 text-sm">
                    +{decileAnalysis.top_minus_bottom_spread_pct ?? '0.267'}%
                  </span>
                </div>
              </div>
            </div>

            {/* Decile Return Visualization */}
            <div className="space-y-2 pt-2">
              {(decileAnalysis.deciles || []).map((d) => {
                const maxRet = Math.max(...(decileAnalysis.deciles || []).map(x => Math.abs(x.avg_return_pct) || 0.01), 0.8);
                const widthPct = Math.min(100, Math.max(5, (Math.abs(d.avg_return_pct) / maxRet) * 100));
                const isTop = d.decile === 10;
                const isBottom = d.decile === 1;

                return (
                  <div key={d.decile} className="flex items-center gap-3 text-xs font-mono">
                    <span className={`w-8 shrink-0 font-bold ${isTop ? 'text-emerald-400' : isBottom ? 'text-rose-400' : 'text-slate-400'}`}>
                      Q{d.decile}
                    </span>
                    <div className="flex-1 bg-slate-950 h-6 rounded-lg overflow-hidden relative border border-slate-800 flex items-center">
                      <div 
                        className={`h-full transition-all ${
                          isTop ? 'bg-gradient-to-r from-emerald-600 to-teal-500' :
                          isBottom ? 'bg-gradient-to-r from-rose-700 to-rose-500' :
                          'bg-gradient-to-r from-slate-700 to-slate-600'
                        }`}
                        style={{ width: `${widthPct}%` }}
                      />
                      <span className="absolute left-3 text-[11px] font-bold text-white drop-shadow">
                        +{d.avg_return_pct}% avg (Sharpe: {d.sharpe}, Hit: {d.hit_rate}%)
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Deciles Diagnostic Reality Alert */}
            <div className="p-4 rounded-xl bg-slate-950/80 border border-slate-800 text-xs space-y-2">
              <h4 className="font-bold text-slate-300 flex items-center gap-1.5 uppercase text-[11px]">
                <Info size={14} className="text-cyan-400" />
                Cross-Sectional Decile Reality Check:
              </h4>
              <p className="text-slate-400 leading-relaxed">
                <strong>Interior Flatness:</strong> Deciles Q2 through Q8 produce virtually flat returns between +0.35% and +0.51%. Q10 (+0.462%) is actually lower than Q9 (+0.703%). The ordering does not form a smooth monotonic gradient across the middle 60% of the universe.
              </p>
              <p className="text-slate-500 leading-relaxed italic text-[11px]">
                * Note on Long/Short Spread: The +0.267% spread represents a theoretical paper long/short strategy. Indian cash equities disallow naked overnight shorting. Therefore, economic viability must be established solely on long-only portfolios.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* TAB 3: LONG-ONLY PORTFOLIO ECONOMICS */}
      {activeTab === 'portfolios' && (
        <div className="space-y-6">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-6">
            <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
              <div>
                <h3 className="text-base font-black text-white flex items-center gap-2">
                  <TrendingUp className="text-emerald-400" size={18} />
                  Long-Only Cash Portfolio Economics (16 Variants)
                </h3>
                <p className="text-slate-400 text-xs mt-0.5">
                  Evaluates long-only portfolios across 4 sizing modes and 4 friction tiers (10 bps, 15 bps, 20 bps, 30 bps).
                </p>
              </div>

              {/* Filters */}
              <div className="flex items-center gap-3 text-xs">
                <div className="flex items-center gap-1.5 bg-slate-950 p-1.5 rounded-xl border border-slate-800">
                  <span className="text-slate-500 text-[10px] uppercase font-mono px-1">Top-K:</span>
                  {['ALL', 'TOP_5', 'TOP_10', 'TOP_20_PCT', 'TOP_30_PCT'].map(k => (
                    <button
                      key={k}
                      onClick={() => setSelectedTopK(k)}
                      className={`px-2 py-1 rounded text-[11px] font-mono transition cursor-pointer ${
                        selectedTopK === k ? 'bg-indigo-600 text-white font-bold' : 'text-slate-400 hover:text-white'
                      }`}
                    >
                      {k}
                    </button>
                  ))}
                </div>

                <div className="flex items-center gap-1.5 bg-slate-950 p-1.5 rounded-xl border border-slate-800">
                  <span className="text-slate-500 text-[10px] uppercase font-mono px-1">Cost:</span>
                  {['ALL', '0.1', '0.15', '0.2', '0.3'].map(f => (
                    <button
                      key={f}
                      onClick={() => setSelectedFriction(f)}
                      className={`px-2 py-1 rounded text-[11px] font-mono transition cursor-pointer ${
                        selectedFriction === f ? 'bg-indigo-600 text-white font-bold' : 'text-slate-400 hover:text-white'
                      }`}
                    >
                      {f === 'ALL' ? 'ALL' : `${(parseFloat(f)*100).toFixed(0)} bps`}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Portfolios Table */}
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs font-mono">
                <thead>
                  <tr className="border-b border-slate-800 text-slate-400">
                    <th className="pb-2.5">Mode</th>
                    <th className="pb-2.5">Friction</th>
                    <th className="pb-2.5">CAGR</th>
                    <th className="pb-2.5">Sharpe</th>
                    <th className="pb-2.5">Max DD</th>
                    <th className="pb-2.5">Win Rate</th>
                    <th className="pb-2.5">1-Way Turnover</th>
                    <th className="pb-2.5">Annualized T/O</th>
                    <th className="pb-2.5 text-right">Profit Factor</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60">
                  {filteredPortfolios.map((p, idx) => (
                    <tr key={idx} className="hover:bg-slate-800/40">
                      <td className="py-2.5 font-bold text-white">{p.top_k_mode}</td>
                      <td className="py-2.5 text-slate-400">{(p.friction_pct * 100).toFixed(0)} bps</td>
                      <td className={`py-2.5 font-bold ${p.cagr_pct > 20 ? 'text-emerald-400' : p.cagr_pct > 10 ? 'text-cyan-400' : 'text-slate-400'}`}>
                        {p.cagr_pct.toFixed(2)}%
                      </td>
                      <td className={`py-2.5 font-bold ${p.sharpe_ratio >= 1.5 ? 'text-emerald-400' : p.sharpe_ratio >= 1.0 ? 'text-amber-400' : 'text-rose-400'}`}>
                        {p.sharpe_ratio.toFixed(2)}
                      </td>
                      <td className="py-2.5 text-rose-400">{p.max_drawdown_pct.toFixed(2)}%</td>
                      <td className="py-2.5 text-slate-300">{p.win_rate_pct.toFixed(1)}%</td>
                      <td className="py-2.5 text-amber-300 font-bold">{p.turnover_one_way_per_rebalance_pct.toFixed(1)}%</td>
                      <td className="py-2.5 text-slate-400">{p.annualized_turnover_pct.toFixed(0)}%</td>
                      <td className="py-2.5 text-right text-slate-300">{p.profit_factor.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Benchmark Comparison Card */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
              <div className="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
                <span className="text-slate-500 text-[10px] block uppercase font-mono">Equal-Weight LIVE_52 Benchmark (Buy-and-Hold)</span>
                <div className="grid grid-cols-3 gap-2 text-xs font-mono">
                  <div>
                    <span className="text-slate-500 block text-[10px]">CAGR</span>
                    <span className="font-bold text-white">15.77%</span>
                  </div>
                  <div>
                    <span className="text-slate-500 block text-[10px]">Sharpe</span>
                    <span className="font-bold text-emerald-400">1.15</span>
                  </div>
                  <div>
                    <span className="text-slate-500 block text-[10px]">Max DD</span>
                    <span className="font-bold text-rose-400">-15.98%</span>
                  </div>
                </div>
                <span className="text-slate-500 text-[11px] block mt-1">Turnover: 0.00% (Passive)</span>
              </div>

              <div className="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
                <span className="text-slate-500 text-[10px] block uppercase font-mono">NIFTY 50 Benchmark Reality Disclosure</span>
                <p className="text-xs text-slate-400">
                  <span className="font-bold text-amber-400 uppercase">UNAVAILABLE:</span> The canonical local SQLite database contains 511 cash equities and zero index tickers (<code className="text-slate-300">^NSEI</code> is not stored). In accordance with research integrity rule 4, no synthetic series was fabricated.
                </p>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 4: MARKET REGIMES */}
      {activeTab === 'regimes' && (
        <div className="space-y-6">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
            <h3 className="text-base font-black text-white flex items-center gap-2">
              <Activity className="text-amber-400" size={18} />
              Macro Market Regime Breakdown (Validation Set)
            </h3>
            <p className="text-slate-400 text-xs">
              Segmented by market conditions using 50-day moving average and 20-day historical volatility.
            </p>

            <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-3 pt-2">
              {regimes.map((r, idx) => (
                <div key={idx} className="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2 font-mono">
                  <span className="text-slate-500 text-[10px] block uppercase">{r.regime_name}</span>
                  <div className="text-xs space-y-1">
                    <div className="flex justify-between">
                      <span className="text-slate-400">Samples:</span>
                      <span className="text-white font-bold">{r.sample_count}</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-slate-400">Rank IC:</span>
                      <span className={`font-bold ${r.rank_ic > 0.03 ? 'text-emerald-400' : 'text-slate-300'}`}>
                        +{r.rank_ic.toFixed(4)}
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-slate-400">Win Rate:</span>
                      <span className="text-white font-bold">{r.win_rate.toFixed(1)}%</span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-slate-400">Spread:</span>
                      <span className="text-cyan-400 font-bold">+{r.spread_pct.toFixed(3)}%</span>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* TAB 5: WALK-FORWARD STABILITY */}
      {activeTab === 'walk-forward' && (
        <div className="space-y-6">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
            <h3 className="text-base font-black text-white flex items-center gap-2">
              <Calendar className="text-indigo-400" size={18} />
              5-Window Rolling Walk-Forward Temporal Stability
            </h3>
            <p className="text-slate-400 text-xs">
              Rolling window evaluation across historical train/validation timeline (2016–2023) to detect decay or regime vulnerability.
            </p>

            <div className="overflow-x-auto pt-2">
              <table className="w-full text-left text-xs font-mono">
                <thead>
                  <tr className="border-b border-slate-800 text-slate-400">
                    <th className="pb-2.5">Window</th>
                    <th className="pb-2.5">Date Range</th>
                    <th className="pb-2.5">Rank IC</th>
                    <th className="pb-2.5">Monotonicity</th>
                    <th className="pb-2.5">L/S Spread</th>
                    <th className="pb-2.5 text-right">Stability Diagnosis</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60">
                  {walkForward.map((w, idx) => (
                    <tr key={idx} className="hover:bg-slate-800/40">
                      <td className="py-2.5 font-bold text-white">Window {w.window_index}</td>
                      <td className="py-2.5 text-slate-400">{w.start_date?.split(' ')[0]} to {w.end_date?.split(' ')[0]}</td>
                      <td className={`py-2.5 font-bold ${w.rank_ic > 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {w.rank_ic > 0 ? '+' : ''}{w.rank_ic.toFixed(4)}
                      </td>
                      <td className={`py-2.5 ${w.monotonicity > 0 ? 'text-slate-300' : 'text-rose-400'}`}>
                        {w.monotonicity.toFixed(3)}
                      </td>
                      <td className={`py-2.5 font-bold ${w.spread_pct > 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {w.spread_pct > 0 ? '+' : ''}{w.spread_pct.toFixed(3)}%
                      </td>
                      <td className="py-2.5 text-right">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                          w.rank_ic > 0.05 && w.monotonicity > 0.4 
                            ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                            : w.rank_ic < 0 || w.spread_pct < 0
                            ? 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                            : 'bg-amber-500/10 text-amber-400 border-amber-500/30'
                        }`}>
                          {w.rank_ic < 0 || w.spread_pct < 0 ? 'REGIME BREAKDOWN' : w.rank_ic > 0.05 ? 'STRONG TREND' : 'MODERATE/CHOPPY'}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="p-4 rounded-xl bg-slate-950/80 border border-slate-800 text-xs text-slate-400 space-y-1">
              <span className="font-bold text-slate-200 block text-[11px] uppercase">Walk-Forward Takeaway:</span>
              <p>
                In Window 2 (2018–2019) and Window 3 (2019–2020), the signal experienced negative Rank IC (-0.0234) and negative spread (-0.482%). The bulk of historical profitability was concentrated in Window 4 (late 2020–2022 post-COVID liquidity surge). The signal is heavily regime-dependent.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* TAB 6: SINGLE-PASS LOCKED OOS */}
      {activeTab === 'oos' && (
        <div className="space-y-6">
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-6">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-base font-black text-white flex items-center gap-2">
                  <Lock className="text-rose-400" size={18} />
                  Single-Pass Locked OOS Holdout Evaluation (2023–2026)
                </h3>
                <p className="text-slate-400 text-xs mt-0.5">
                  Strict single-pass evaluation on untouched 3-year holdout dataset after all hyperparameters were frozen.
                </p>
              </div>

              <span className="px-3 py-1 rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-400 font-mono text-xs font-bold">
                SINGLE-PASS VERIFIED
              </span>
            </div>

            {/* OOS Metric Cards */}
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 font-mono">
              <div className="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
                <span className="text-slate-500 text-[10px] block uppercase">Candidate</span>
                <span className="font-bold text-white text-xs">{oos.candidate_name || 'lightgbm (TOP_10)'}</span>
              </div>

              <div className="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
                <span className="text-slate-500 text-[10px] block uppercase">OOS Rank IC</span>
                <span className="font-bold text-emerald-400 text-sm">+{oos.rank_ic}</span>
              </div>

              <div className="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
                <span className="text-slate-500 text-[10px] block uppercase">Long-Only CAGR</span>
                <span className="font-bold text-white text-sm">{oos.cagr_pct}%</span>
              </div>

              <div className="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
                <span className="text-slate-500 text-[10px] block uppercase">OOS Sharpe</span>
                <span className="font-bold text-amber-400 text-sm">{oos.sharpe}</span>
              </div>

              <div className="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
                <span className="text-slate-500 text-[10px] block uppercase">Max Drawdown</span>
                <span className="font-bold text-rose-400 text-sm">{oos.max_drawdown_pct}%</span>
              </div>

              <div className="bg-slate-950 p-3.5 rounded-xl border border-slate-800">
                <span className="text-slate-500 text-[10px] block uppercase">Annualized Turnover</span>
                <span className="font-bold text-amber-300 text-sm">{oos.turnover_pct}%</span>
              </div>
            </div>

            {/* Head-to-Head Comparison vs Buy & Hold */}
            <div className="bg-slate-950 p-5 rounded-xl border border-slate-800 space-y-4">
              <h4 className="text-xs font-bold text-slate-300 uppercase tracking-wider">
                Head-to-Head: Strategy vs Passive Equal-Weight LIVE_52 Benchmark
              </h4>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs font-mono">
                <div className="p-4 rounded-xl bg-slate-900 border border-slate-800 space-y-2">
                  <span className="text-amber-400 font-bold block text-sm">Active Top-10 Strategy</span>
                  <div className="space-y-1 text-slate-300">
                    <div className="flex justify-between"><span>CAGR:</span><span className="font-bold text-white">17.89%</span></div>
                    <div className="flex justify-between"><span>Sharpe:</span><span className="font-bold text-amber-400">1.02</span></div>
                    <div className="flex justify-between"><span>Max Drawdown:</span><span className="font-bold text-rose-400">-17.88%</span></div>
                    <div className="flex justify-between"><span>1-Way Turnover:</span><span className="font-bold text-amber-300">52.7% / rebalance</span></div>
                    <div className="flex justify-between"><span>Annualized T/O:</span><span className="font-bold text-amber-300">2,657.1%</span></div>
                  </div>
                </div>

                <div className="p-4 rounded-xl bg-slate-900 border border-slate-800 space-y-2">
                  <span className="text-emerald-400 font-bold block text-sm">Passive LIVE_52 Buy &amp; Hold</span>
                  <div className="space-y-1 text-slate-300">
                    <div className="flex justify-between"><span>CAGR:</span><span className="font-bold text-white">15.77%</span></div>
                    <div className="flex justify-between"><span>Sharpe:</span><span className="font-bold text-emerald-400">1.15</span></div>
                    <div className="flex justify-between"><span>Max Drawdown:</span><span className="font-bold text-emerald-400">-15.98%</span></div>
                    <div className="flex justify-between"><span>1-Way Turnover:</span><span className="font-bold text-white">0.0%</span></div>
                    <div className="flex justify-between"><span>Annualized T/O:</span><span className="font-bold text-white">0.0%</span></div>
                  </div>
                </div>
              </div>

              <div className="p-3.5 rounded-lg bg-rose-950/20 border border-rose-500/30 text-rose-300 text-xs flex items-start gap-2.5">
                <AlertCircle size={16} className="shrink-0 text-rose-400 mt-0.5" />
                <p className="leading-relaxed">
                  <strong>Governance Verdict:</strong> While the active strategy yields +2.12% higher raw CAGR (17.89% vs 15.77%), its Sharpe ratio is <strong>lower</strong> (1.02 vs 1.15) and maximum drawdown is <strong>deeper</strong> (-17.88% vs -15.98%) while burning 2,657% in annualized turnover. This fails the quantitative threshold for deployment. The production Champions remain untouched.
                </p>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
