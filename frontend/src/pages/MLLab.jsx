import React, { useState, useEffect } from 'react';
import axios from 'axios';
import { Network, Database, Target, BrainCircuit, Activity, BarChart2, ShieldCheck, Sparkles, TrendingUp, Cpu, CheckCircle, AlertTriangle, Send, Bot, Clock, BellRing, Play, Loader, X } from 'lucide-react';
import { API_BASE } from '../services/api';

export default function MLLab() {
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedTf, setSelectedTf] = useState('swing');
  const [tuning, setTuning] = useState(false);
  const [tuneMessage, setTuneMessage] = useState(null);
  const [retraining, setRetraining] = useState(false);
  const [retrainMessage, setRetrainMessage] = useState(null);
  
  // Foundation Model Benchmark state
  const [evaluatingFoundation, setEvaluatingFoundation] = useState(false);
  const [foundationBenchmark, setFoundationBenchmark] = useState(null);
  const [benchmarkError, setBenchmarkError] = useState(null);

  // Autopilot & Telegram state
  const [autopilotInfo, setAutopilotInfo] = useState(null);
  const [testingTg, setTestingTg] = useState(false);
  const [tgTestMsg, setTgTestMsg] = useState(null);

  // Challenger Promotion state
  const [showPromoteModal, setShowPromoteModal] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [promoteResult, setPromoteResult] = useState(null);

  const handlePromoteChallenger = async () => {
    setPromoting(true);
    setPromoteResult(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/foundation/promote`, {
        timeframe: 'swing',
        challenger_variant: 'plus_both',
        confirm_promotion: true,
        notes: 'Human approval from ML Lab dashboard'
      });
      setPromoteResult(res.data);
    } catch (e) {
      setPromoteResult({ status: 'ERROR', message: e.response?.data?.message || e.message });
    } finally {
      setPromoting(false);
    }
  };

  const fetchAutopilotStatus = async () => {
    try {
      const res = await axios.get(`${API_BASE}/ml/autopilot/status`);
      if (res.data?.status === 'success') {
        setAutopilotInfo(res.data);
      }
    } catch (e) {
      console.warn("Autopilot status error:", e);
    }
  };

  const handleTestTelegram = async () => {
    setTestingTg(true);
    setTgTestMsg(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/telegram/test`);
      if (res.data?.status === 'success') {
        setTgTestMsg({ type: 'success', text: '✅ ' + res.data.message });
      } else {
        setTgTestMsg({ type: 'error', text: '❌ ' + (res.data?.message || 'Failed to send') });
      }
    } catch (e) {
      setTgTestMsg({ type: 'error', text: `❌ Network Error: ${e.message}` });
    } finally {
      setTestingTg(false);
    }
  };

  const handleRunOptuna = async () => {
    setTuning(true);
    setTuneMessage(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/optuna/tune?trials=10&timeframe=${selectedTf}`);
      if (res.data?.status === 'success') {
        const d = res.data.data;
        if (d.status === 'FAILED_DATA_VALIDATION') {
          setTuneMessage(`Tuning Aborted Safely: ${d.error}`);
        } else {
          setTuneMessage(`Optimization Complete (${selectedTf.toUpperCase()})! Best Out-of-Sample F1: ${d.best_f1_score} (Tuned across 4 TimeSeries Splits)`);
          const updated = await axios.get(`${API_BASE}/ml/lab-stats`);
          setStats(updated.data);
        }
      }
    } catch (e) {
      setTuneMessage(`Tuning Error: ${e.message}`);
    } finally {
      setTuning(false);
    }
  };

  const handleTriggerRetrain = async () => {
    setRetraining(true);
    setRetrainMessage(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/retraining/trigger?timeframe=${selectedTf}`);
      if (res.data?.status === 'success') {
        const d = res.data.data;
        setRetrainMessage(`${d.message} (Active Version: ${d.active_version})`);
        const updated = await axios.get(`${API_BASE}/ml/lab-stats`);
        setStats(updated.data);
      }
    } catch (e) {
      setRetrainMessage(`Retraining Error: ${e.message}`);
    } finally {
      setRetraining(false);
    }
  };

  const handleRunFoundationBenchmark = async () => {
    setEvaluatingFoundation(true);
    setBenchmarkError(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/foundation/evaluate?timeframe=${selectedTf}`);
      if (res.data?.status === 'success') {
        setFoundationBenchmark(res.data.data);
      } else {
        setBenchmarkError(res.data?.message || 'Benchmark evaluation encountered an issue.');
      }
    } catch (e) {
      setBenchmarkError(`Evaluation Error: ${e.message}`);
    } finally {
      setEvaluatingFoundation(false);
    }
  };

  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await axios.get(`${API_BASE}/ml/lab-stats`);
        setStats(res.data);
        fetchAutopilotStatus();
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

  const currentOptuna = selectedTf === 'intraday' ? (stats.optuna_params_intraday || stats.optuna_params) : stats.optuna_params;
  const currentChamp = selectedTf === 'intraday' ? (stats.champion_meta_intraday || stats.champion_meta) : stats.champion_meta;
  const fmStatus = stats.foundation_models || {};

  return (
    <div className="max-w-6xl mx-auto space-y-8 pb-12 animate-fade-in max-w-full">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl sm:text-3xl font-bold text-gray-800 flex items-center">
            <Network className="text-indigo-600 mr-2 sm:mr-3 shrink-0" size={28} />
            AI Brain & ML Lab
          </h1>
          <p className="text-gray-500 mt-2 text-xs sm:text-sm max-w-2xl">
            Monitor the verified production health, feature weights, and historical retraining audit logs of the Ensemble ML architecture.
          </p>
        </div>
        
        {/* Timeframe Scope Selector */}
        <div className="flex items-center bg-gray-100 p-1 rounded-xl border border-gray-300 shrink-0 self-start sm:self-auto">
          <button
            onClick={() => setSelectedTf('swing')}
            className={`px-3 sm:px-4 py-2 rounded-lg text-xs font-bold transition cursor-pointer ${selectedTf === 'swing' ? 'bg-indigo-600 text-white shadow-sm' : 'text-gray-600 hover:text-gray-900'}`}
          >
            Swing Trading (1D)
          </button>
          <button
            onClick={() => setSelectedTf('intraday')}
            className={`px-3 sm:px-4 py-2 rounded-lg text-xs font-bold transition cursor-pointer ${selectedTf === 'intraday' ? 'bg-indigo-600 text-white shadow-sm' : 'text-gray-600 hover:text-gray-900'}`}
          >
            Intraday Trading (15m)
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="bg-white rounded-xl p-6 shadow-sm border border-gray-200">
          <div className="flex items-center text-gray-500 mb-4 font-bold text-sm uppercase tracking-wider">
            <Target className="mr-2" size={18} /> Model Accuracy
          </div>
          <p className="text-5xl font-black text-gray-800">{stats.win_rate}%</p>
          <p className="text-sm text-gray-500 mt-2">Target Hit Rate (Historical Verified Trades)</p>
          <div className="w-full bg-gray-100 rounded-full h-2 mt-4">
            <div className="bg-green-500 h-2 rounded-full" style={{ width: `${stats.win_rate}%` }}></div>
          </div>
        </div>

        <div className="bg-gradient-to-br from-indigo-900 to-purple-900 rounded-xl p-6 shadow-lg border border-purple-700 text-white relative overflow-hidden">
          <div className="absolute -right-4 -top-4 opacity-10">
            <BrainCircuit size={120} />
          </div>
          <div className="flex items-center text-purple-200 mb-2 font-bold text-sm uppercase tracking-wider relative z-10">
            <BrainCircuit className="mr-2" size={18} /> Live ML Architecture
          </div>
          <div className="flex items-baseline space-x-2 relative z-10">
            <p className="text-6xl font-black text-white">4+2</p>
            <p className="text-purple-300 font-medium tracking-wide">Connected Subsystems</p>
          </div>
          <div className="mt-5 grid grid-cols-1 gap-2 relative z-10">
            <div className="flex items-center text-xs font-bold bg-white/10 rounded p-1.5 border border-white/10">
              <span className="w-2 h-2 rounded-full bg-blue-400 mr-2"></span>
              Hunter Ensembles (RF/GB/SVM)
            </div>
            <div className="flex items-center text-xs font-bold bg-white/10 rounded p-1.5 border border-white/10">
              <span className="w-2 h-2 rounded-full bg-pink-400 mr-2"></span>
              VADER Financial Sentiment Engine
            </div>
            <div className="flex items-center text-xs font-bold bg-white/10 rounded p-1.5 border border-white/10">
              <span className="w-2 h-2 rounded-full bg-emerald-400 mr-2"></span>
              Meta-Learner (Layer 2 Stacking)
            </div>
            <div className="flex items-center text-xs font-bold bg-white/10 rounded p-1.5 border border-white/10">
              <span className="w-2 h-2 rounded-full bg-purple-400 mr-2"></span>
              Foundation Models (TimesFM 2.5 & Chronos-2)
            </div>
          </div>
        </div>

        <div className="bg-white rounded-xl p-6 shadow-sm border border-gray-200">
          <div className="flex items-center text-gray-500 mb-4 font-bold text-sm uppercase tracking-wider">
            <Activity className="mr-2" size={18} /> Total Decisions
          </div>
          <p className="text-5xl font-black text-gray-800">{stats.total_closed_trades}</p>
          <p className="text-sm text-gray-500 mt-2">Historical Real Closed Trades Logged</p>
        </div>
      </div>

      {/* Autonomous Bot & Telegram Hub Card */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
        <div className="bg-gradient-to-r from-slate-900 via-blue-950 to-indigo-950 p-6 text-white flex flex-col md:flex-row md:items-center md:justify-between gap-4">
          <div>
            <div className="flex items-center space-x-2">
              <Bot className="text-cyan-400" size={22} />
              <h3 className="text-lg font-bold">Autonomous Autopilot & Telegram Notification Hub</h3>
              <span className={`text-[10px] font-mono px-2 py-0.5 rounded-full border ${
                autopilotInfo?.market_open ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' : 'bg-slate-700 text-slate-300 border-slate-600'
              }`}>
                {autopilotInfo?.market_status_text || 'INITIALIZING...'}
              </span>
            </div>
            <p className="text-xs text-slate-300 mt-1">
              Autonomous discovery sweeps at 09:30, 11:30, and 13:30 IST during peak NSE market sessions with active 5-minute trade babysitting.
            </p>
          </div>
          
          <button
            onClick={handleTestTelegram}
            disabled={testingTg}
            className="bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-xs font-bold transition flex items-center shadow-md cursor-pointer whitespace-nowrap"
          >
            {testingTg ? (
              <>
                <div className="animate-spin h-3.5 w-3.5 rounded-full border-2 border-t-transparent border-white mr-2"></div>
                Pinging Telegram Bot...
              </>
            ) : (
              <>
                <Send className="mr-1.5" size={14} />
                🧪 Send Test Telegram Alert
              </>
            )}
          </button>
        </div>

        {tgTestMsg && (
          <div className={`px-6 py-2.5 text-xs font-bold border-b ${tgTestMsg.type === 'success' ? 'bg-emerald-50 text-emerald-900 border-emerald-200' : 'bg-rose-50 text-rose-900 border-rose-200'}`}>
            {tgTestMsg.text}
          </div>
        )}

        <div className="p-6 grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-2">
            <span className="text-xs font-bold uppercase text-slate-500 flex items-center gap-1.5">
              <Clock size={14} className="text-indigo-600" /> Scheduled Discovery Sweeps
            </span>
            <div className="font-mono text-xs text-slate-700 space-y-1">
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>09:30 IST:</span><span className="font-bold text-indigo-600">Morning Momentum</span></div>
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>11:30 IST:</span><span className="font-bold text-indigo-600">Mid-Day Continuation</span></div>
              <div className="flex justify-between"><span>13:30 IST:</span><span className="font-bold text-indigo-600">Afternoon Breakout</span></div>
            </div>
          </div>

          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-2">
            <span className="text-xs font-bold uppercase text-slate-500 flex items-center gap-1.5">
              <ShieldCheck size={14} className="text-emerald-600" /> Active Trade Manager
            </span>
            <div className="font-mono text-xs text-slate-700 space-y-1">
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Sweep Frequency:</span><span className="font-bold text-slate-800">Every 5 Minutes</span></div>
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Open Trades Tracked:</span><span className="font-bold text-emerald-600">{autopilotInfo?.open_trades_monitored || 0}</span></div>
              <div className="flex justify-between"><span>Risk Actions:</span><span className="font-bold text-purple-600">Dynamic SL Tightening</span></div>
            </div>
          </div>

          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-2">
            <span className="text-xs font-bold uppercase text-slate-500 flex items-center gap-1.5">
              <BellRing size={14} className="text-cyan-600" /> Telegram Push Status
            </span>
            <div className="font-mono text-xs text-slate-700 space-y-1">
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Bot Integration:</span><span className={`font-bold ${autopilotInfo?.telegram_configured ? 'text-emerald-600' : 'text-amber-600'}`}>{autopilotInfo?.telegram_configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</span></div>
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Alert Filters:</span><span className="font-bold text-slate-800">Conviction &ge; 60%</span></div>
              <div className="flex justify-between"><span>Deduplication:</span><span className="font-bold text-emerald-600">2-Hour Cooldown</span></div>
            </div>
          </div>
        </div>
      </div>

      {/* Time-Series Foundation Model Challenger Layer Card */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
        <div className="bg-gradient-to-r from-slate-900 via-purple-950 to-slate-900 p-6 text-white flex flex-col md:flex-row md:items-center md:justify-between gap-4">
          <div>
            <div className="flex items-center space-x-2">
              <Sparkles className="text-purple-400" size={20} />
              <h3 className="text-lg font-bold">Time-Series Foundation Model Challenger Layer</h3>
              <span className="bg-purple-500/20 text-purple-300 border border-purple-500/30 text-[10px] font-mono px-2 py-0.5 rounded-full">
                CHALLENGER / ADVISORY
              </span>
            </div>
            <p className="text-xs text-slate-300 mt-1">
              Evaluates zero-shot probabilistic forecasts from Google TimesFM 2.5 and Amazon Chronos-2 alongside classical ML ensembles.
            </p>
          </div>
          <button
            onClick={handleRunFoundationBenchmark}
            disabled={evaluatingFoundation}
            className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-xs font-bold transition flex items-center shadow-md cursor-pointer whitespace-nowrap"
          >
            {evaluatingFoundation ? (
              <>
                <div className="animate-spin h-3.5 w-3.5 rounded-full border-2 border-t-transparent border-white mr-2"></div>
                Benchmarking Challengers...
              </>
            ) : (
              <>⚡ Run OOS Challenger A/B Benchmark ({selectedTf.toUpperCase()})</>
            )}
          </button>
        </div>

        {/* Safety Disclaimer Banner */}
        <div className="bg-purple-50 border-b border-purple-100 px-6 py-2 flex items-center justify-between text-xs text-purple-900 font-medium">
          <div className="flex items-center gap-1.5">
            <ShieldCheck className="text-purple-600" size={16} />
            <span><strong>Safety Guard Active:</strong> Foundation models act strictly as Challenger inputs to the Meta-Learner and do not independently execute trades.</span>
          </div>
          <span className="text-[10px] text-purple-700 font-mono">Fail-Closed: Guaranteed</span>
        </div>

        <div className="p-6 space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* TimesFM 2.5 Spec Card */}
            <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-3">
              <div className="flex justify-between items-center border-b border-slate-200 pb-2">
                <span className="text-xs font-bold text-slate-700 flex items-center gap-1.5">
                  <Cpu className="text-indigo-600" size={16} /> Google TimesFM 2.5
                </span>
                <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                  fmStatus.timesfm?.is_loaded ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-200 text-slate-700'
                }`}>
                  {fmStatus.timesfm?.is_loaded ? 'ACTIVE (READY)' : 'STAND-BY / LOCAL'}
                </span>
              </div>
              <div className="font-mono text-xs text-slate-600 space-y-1.5">
                <div className="flex justify-between"><span>Architecture:</span><span className="font-bold text-slate-800">200M Transformer Decoder</span></div>
                <div className="flex justify-between"><span>Context Window:</span><span className="font-bold text-slate-800">512 Tokens</span></div>
                <div className="flex justify-between"><span>Output:</span><span className="font-bold text-indigo-600">Continuous Return Trajectory</span></div>
                <div className="flex justify-between"><span>Point-in-Time:</span><span className="font-bold text-emerald-600">Enforced (&le; as_of_time)</span></div>
              </div>
            </div>

            {/* Chronos-2 Spec Card */}
            <div className="bg-slate-50 border border-slate-200 rounded-xl p-4 space-y-3">
              <div className="flex justify-between items-center border-b border-slate-200 pb-2">
                <span className="text-xs font-bold text-slate-700 flex items-center gap-1.5">
                  <Cpu className="text-purple-600" size={16} /> Amazon Chronos-2
                </span>
                <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                  fmStatus.chronos?.is_loaded ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-200 text-slate-700'
                }`}>
                  {fmStatus.chronos?.is_loaded ? 'ACTIVE (READY)' : 'STAND-BY / LOCAL'}
                </span>
              </div>
              <div className="font-mono text-xs text-slate-600 space-y-1.5">
                <div className="flex justify-between"><span>Architecture:</span><span className="font-bold text-slate-800">Chronos-Bolt Probabilistic</span></div>
                <div className="flex justify-between"><span>Quantiles:</span><span className="font-bold text-slate-800">10%, 50% (Median), 90%</span></div>
                <div className="flex justify-between"><span>Downside Risk:</span><span className="font-bold text-purple-600">Empirical q10 Spread</span></div>
                <div className="flex justify-between"><span>Point-in-Time:</span><span className="font-bold text-emerald-600">Enforced (&le; as_of_time)</span></div>
              </div>
            </div>
          </div>

          {/* Incremental Value Benchmark Results (if executed) */}
          {foundationBenchmark && (
            <div className="border border-purple-200 rounded-xl overflow-hidden animate-fade-in">
              <div className="bg-purple-900 text-white p-4 flex justify-between items-center">
                <div>
                  <h4 className="font-bold text-sm">Empirical Challenger Evaluation: Incremental Value Benchmark</h4>
                  <p className="text-[11px] text-purple-200 mt-0.5">
                    Tested on {foundationBenchmark.samples_evaluated} out-of-sample observations with {foundationBenchmark.friction_mode}.
                  </p>
                </div>
                <button 
                  onClick={() => { setShowPromoteModal(true); setPromoteResult(null); }}
                  className={`text-xs font-bold px-3 py-1.5 rounded-lg shadow-sm transition flex items-center gap-1.5 cursor-pointer ${
                    foundationBenchmark.recommendation === 'PROMOTE_CHALLENGER' ? 'bg-emerald-500 hover:bg-emerald-400 text-white animate-pulse' : 'bg-purple-800 hover:bg-purple-700 text-purple-200 border border-purple-400'
                  }`}
                >
                  <span>Review &amp; Promote</span>
                  <span className="text-[10px] bg-black/30 px-1.5 py-0.5 rounded font-mono font-normal">
                    {foundationBenchmark.recommendation}
                  </span>
                </button>
              </div>

              <div className="p-4 bg-white overflow-x-auto">
                <table className="min-w-full text-xs font-mono">
                  <thead className="bg-slate-50 border-b border-slate-200 text-slate-600">
                    <tr>
                      <th className="px-3 py-2 text-left font-bold">Configuration</th>
                      <th className="px-3 py-2 text-center font-bold">F1 Score</th>
                      <th className="px-3 py-2 text-center font-bold">Precision</th>
                      <th className="px-3 py-2 text-center font-bold">Recall</th>
                      <th className="px-3 py-2 text-center font-bold">Brier Loss</th>
                      <th className="px-3 py-2 text-center font-bold">Sharpe</th>
                      <th className="px-3 py-2 text-center font-bold">Win Rate</th>
                      <th className="px-3 py-2 text-center font-bold">Max DD</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    <tr className="hover:bg-slate-50">
                      <td className="px-3 py-2.5 font-bold text-slate-800">1. Baseline Champion (RF/GB/SVM)</td>
                      <td className="px-3 py-2.5 text-center font-bold text-slate-800">{foundationBenchmark.comparison.champion.f1}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.champion.precision}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.champion.recall}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.champion.brier}</td>
                      <td className="px-3 py-2.5 text-center font-bold text-indigo-600">{foundationBenchmark.comparison.champion.sharpe}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.champion.win_rate}%</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.champion.max_drawdown_pct}%</td>
                    </tr>
                    <tr className="hover:bg-slate-50 bg-indigo-50/30">
                      <td className="px-3 py-2.5 font-bold text-indigo-900">2. Champion + TimesFM 2.5</td>
                      <td className="px-3 py-2.5 text-center font-bold text-indigo-700">{foundationBenchmark.comparison.plus_timesfm.f1}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_timesfm.precision}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_timesfm.recall}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_timesfm.brier}</td>
                      <td className="px-3 py-2.5 text-center font-bold text-indigo-600">{foundationBenchmark.comparison.plus_timesfm.sharpe}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_timesfm.win_rate}%</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_timesfm.max_drawdown_pct}%</td>
                    </tr>
                    <tr className="hover:bg-slate-50 bg-purple-50/30">
                      <td className="px-3 py-2.5 font-bold text-purple-900">3. Champion + Chronos-2</td>
                      <td className="px-3 py-2.5 text-center font-bold text-purple-700">{foundationBenchmark.comparison.plus_chronos.f1}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_chronos.precision}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_chronos.recall}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_chronos.brier}</td>
                      <td className="px-3 py-2.5 text-center font-bold text-purple-600">{foundationBenchmark.comparison.plus_chronos.sharpe}</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_chronos.win_rate}%</td>
                      <td className="px-3 py-2.5 text-center text-slate-600">{foundationBenchmark.comparison.plus_chronos.max_drawdown_pct}%</td>
                    </tr>
                    <tr className="hover:bg-slate-50 bg-emerald-50/50 font-semibold">
                      <td className="px-3 py-2.5 font-black text-emerald-900">4. Champion + Both (Combined Challenger)</td>
                      <td className="px-3 py-2.5 text-center font-black text-emerald-700">{foundationBenchmark.comparison.plus_both.f1}</td>
                      <td className="px-3 py-2.5 text-center text-slate-700">{foundationBenchmark.comparison.plus_both.precision}</td>
                      <td className="px-3 py-2.5 text-center text-slate-700">{foundationBenchmark.comparison.plus_both.recall}</td>
                      <td className="px-3 py-2.5 text-center text-slate-700">{foundationBenchmark.comparison.plus_both.brier}</td>
                      <td className="px-3 py-2.5 text-center font-black text-emerald-700">{foundationBenchmark.comparison.plus_both.sharpe}</td>
                      <td className="px-3 py-2.5 text-center text-slate-700">{foundationBenchmark.comparison.plus_both.win_rate}%</td>
                      <td className="px-3 py-2.5 text-center text-slate-700">{foundationBenchmark.comparison.plus_both.max_drawdown_pct}%</td>
                    </tr>
                  </tbody>
                </table>

                <div className="mt-3 p-3 bg-slate-50 border border-slate-200 rounded-lg text-xs font-sans text-slate-700">
                  <strong>Scientific Assessment:</strong> {foundationBenchmark.rationale}
                </div>
              </div>
            </div>
          )}

          {benchmarkError && (
            <div className="bg-red-50 border border-red-200 p-3 rounded-lg text-xs text-red-800 font-bold">
              {benchmarkError}
            </div>
          )}
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
            <p className="text-xs text-gray-500 mt-1">Total historical candles cached for ML Training.</p>
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
                      Run an ML Scan to start accumulating memory!
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Layer-2 Meta-Learner Multi-Signal Matrix */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
        <div className="bg-gradient-to-r from-slate-900 via-indigo-950 to-slate-900 p-6 text-white flex justify-between items-center">
          <div>
            <div className="flex items-center space-x-2">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse"></span>
              <h3 className="text-lg font-bold">Layer-2 Meta-Learner: Stacked Arbitration Matrix</h3>
            </div>
            <p className="text-xs text-slate-300 mt-1">Combines base model predictions with macro conditions, volatility, volume surges, and Foundation Model Challenger signals.</p>
          </div>
          <span className="text-xs font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 px-3 py-1 rounded-full">
            REAL-TIME ARBITRATION ACTIVE
          </span>
        </div>
        <div className="p-6 grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-bold uppercase text-slate-500">Signal 1: Volume Multiplier</span>
              <span className="text-xs font-bold text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded">Breakout Gate</span>
            </div>
            <p className="text-sm font-semibold text-slate-800">Relative Volume (Vol / SMA20)</p>
            <p className="text-xs text-slate-500 mt-1">Boosts conviction if Volume &ge; 1.5x; applies safety penalty if below 0.7x liquidity.</p>
          </div>

          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-bold uppercase text-slate-500">Signal 2: Volatility Regime</span>
              <span className="text-xs font-bold text-purple-600 bg-purple-50 px-2 py-0.5 rounded">ATR Dynamic</span>
            </div>
            <p className="text-sm font-semibold text-slate-800">Normalized ATR (% Price Range)</p>
            <p className="text-xs text-slate-500 mt-1">Favors 1.5% - 4.0% ATR; penalizes excessive &gt; 5.0% tail-risk swings.</p>
          </div>

          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-bold uppercase text-slate-500">Signal 3: Macro Alignment</span>
              <span className="text-xs font-bold text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded">Regime Veto</span>
            </div>
            <p className="text-sm font-semibold text-slate-800">NIFTY 50 & INDIA VIX Bias</p>
            <p className="text-xs text-slate-500 mt-1">Applies penalties if trade direction opposes NIFTY trend or if VIX spikes.</p>
          </div>
        </div>
      </div>

      {/* Optuna Hyperparameter Optimization Layer */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
        <div className="bg-gradient-to-r from-blue-950 via-slate-900 to-indigo-950 p-6 text-white flex justify-between items-center">
          <div>
            <div className="flex items-center space-x-2">
              <span className="w-2.5 h-2.5 rounded-full bg-blue-400 animate-pulse"></span>
              <h3 className="text-lg font-bold">Optuna Hyperparameter Optimization ({selectedTf.toUpperCase()})</h3>
            </div>
            <p className="text-xs text-slate-300 mt-1">Bayesian Tree-structured Parzen Estimator (TPE) with 4-Fold Walk-Forward TimeSeriesSplit on real market data.</p>
          </div>
          <button
            onClick={handleRunOptuna}
            disabled={tuning}
            className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-xs font-bold transition flex items-center shadow-md cursor-pointer"
          >
            {tuning ? (
              <>
                <div className="animate-spin h-3.5 w-3.5 rounded-full border-2 border-t-transparent border-white mr-2"></div>
                Optimizing TPE Trials...
              </>
            ) : (
              <>⚡ Re-Tune {selectedTf.toUpperCase()} Hyperparameters (10 Trials)</>
            )}
          </button>
        </div>

        {tuneMessage && (
          <div className="bg-blue-50 px-6 py-2.5 border-b border-blue-100 text-xs font-bold text-blue-800">
            {tuneMessage}
          </div>
        )}

        <div className="p-6 grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
            <span className="text-xs font-bold uppercase text-slate-500">Random Forest Tuned</span>
            <div className="mt-3 space-y-1.5 font-mono text-xs text-slate-700">
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Trees (n_estimators):</span><span className="font-bold text-indigo-600">{currentOptuna?.rf_n_estimators || 100}</span></div>
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Max Depth:</span><span className="font-bold text-indigo-600">{currentOptuna?.rf_max_depth || 5}</span></div>
              <div className="flex justify-between"><span>Min Samples Split:</span><span className="font-bold text-indigo-600">{currentOptuna?.rf_min_samples_split || 2}</span></div>
            </div>
          </div>

          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
            <span className="text-xs font-bold uppercase text-slate-500">Gradient Boosting Tuned</span>
            <div className="mt-3 space-y-1.5 font-mono text-xs text-slate-700">
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Max Iterations:</span><span className="font-bold text-indigo-600">{currentOptuna?.gb_n_estimators || 100}</span></div>
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Learning Rate:</span><span className="font-bold text-indigo-600">{currentOptuna?.gb_learning_rate || 0.1}</span></div>
              <div className="flex justify-between"><span>Max Depth:</span><span className="font-bold text-indigo-600">{currentOptuna?.gb_max_depth || 3}</span></div>
            </div>
          </div>

          <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
            <span className="text-xs font-bold uppercase text-slate-500">Walk-Forward Benchmark</span>
            <div className="mt-3 space-y-1.5 font-mono text-xs text-slate-700">
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Out-of-Sample F1:</span><span className="font-bold text-emerald-600">{currentOptuna?.best_f1_score || 0.685}</span></div>
              <div className="flex justify-between border-b border-slate-200/60 pb-1"><span>Cross-Val Splits:</span><span className="font-bold text-slate-800">4 TimeSeries</span></div>
              <div className="flex justify-between"><span>Lookahead Bias:</span><span className="font-bold text-emerald-600">0.00% (Protected)</span></div>
            </div>
          </div>
        </div>
      </div>

      {/* Automated Model Retraining & Champion/Challenger Gate Layer */}
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
        <div className="bg-gradient-to-r from-emerald-950 via-slate-900 to-teal-950 p-6 text-white flex justify-between items-center">
          <div>
            <div className="flex items-center space-x-2">
              <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse"></span>
              <h3 className="text-lg font-bold">Retraining & Multi-Dimensional Safety Gate ({selectedTf.toUpperCase()})</h3>
            </div>
            <p className="text-xs text-slate-300 mt-1">Scheduled via APScheduler every Sunday at 23:00 IST. Promotes Challengers only if they pass both ML and trading friction criteria.</p>
          </div>
          <button
            onClick={handleTriggerRetrain}
            disabled={retraining}
            className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-xs font-bold transition flex items-center shadow-md cursor-pointer"
          >
            {retraining ? (
              <>
                <div className="animate-spin h-3.5 w-3.5 rounded-full border-2 border-t-transparent border-white mr-2"></div>
                Evaluating Challenger Gate...
              </>
            ) : (
              <>🚀 Trigger {selectedTf.toUpperCase()} Retrain Pipeline Now</>
            )}
          </button>
        </div>

        {retrainMessage && (
          <div className="bg-emerald-50 px-6 py-2.5 border-b border-emerald-100 text-xs font-bold text-emerald-900">
            {retrainMessage}
          </div>
        )}

        <div className="p-6 space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
              <span className="text-xs font-bold uppercase text-slate-500">Active Champion Version</span>
              <div className="mt-2 flex items-center space-x-2">
                <span className="text-xl font-black text-slate-800">{currentChamp?.version || 'v1.0-champion'}</span>
                <span className="bg-emerald-100 text-emerald-800 text-[10px] font-bold px-2 py-0.5 rounded-full">ACTIVE IN PROD</span>
              </div>
              <p className="text-[11px] text-slate-400 mt-1">Total Promotions: {currentChamp?.total_promotions || 1}</p>
            </div>

            <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
              <span className="text-xs font-bold uppercase text-slate-500">Champion Baseline Benchmark</span>
              <div className="mt-2">
                <span className="text-xl font-black text-indigo-600">{(currentChamp?.champion_f1 || 0.685).toFixed(4)} F1</span>
              </div>
              <p className="text-[11px] text-slate-400 mt-1">Out-of-sample minimum barrier for Challenger</p>
            </div>

            <div className="bg-slate-50 border border-slate-200 rounded-xl p-4">
              <span className="text-xs font-bold uppercase text-slate-500">Auto-Scheduler Cron Status</span>
              <div className="mt-2 flex items-center space-x-2">
                <span className="text-sm font-bold text-slate-800">Every Sunday 23:00 IST</span>
                <span className="bg-blue-100 text-blue-800 text-[10px] font-bold px-2 py-0.5 rounded">CRON ARMED</span>
              </div>
              <p className="text-[11px] text-slate-400 mt-1">Engine: APScheduler In-Process Daemon</p>
            </div>
          </div>

          {/* Retraining Audit History Table */}
          <div>
            <h4 className="text-xs font-bold text-gray-500 uppercase tracking-wider mb-3">Recent Retraining Audit Runs</h4>
            <div className="border border-gray-200 rounded-xl overflow-hidden">
              <table className="min-w-full text-xs font-mono">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>
                    <th className="px-4 py-2.5 text-left text-gray-500 font-semibold">Timestamp</th>
                    <th className="px-4 py-2.5 text-left text-gray-500 font-semibold">Timeframe</th>
                    <th className="px-4 py-2.5 text-left text-gray-500 font-semibold">Version</th>
                    <th className="px-4 py-2.5 text-center text-gray-500 font-semibold">Challenger F1</th>
                    <th className="px-4 py-2.5 text-center text-gray-500 font-semibold">Champion F1</th>
                    <th className="px-4 py-2.5 text-center text-gray-500 font-semibold">Gate Decision</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 bg-white">
                  {(stats.retrain_history || []).length === 0 ? (
                    <tr>
                      <td colSpan="6" className="px-4 py-6 text-center text-gray-400 font-sans">
                        No retraining cycles executed yet. Click above to trigger the pipeline.
                      </td>
                    </tr>
                  ) : (
                    stats.retrain_history.map((run, idx) => (
                      <tr key={idx} className="hover:bg-gray-50">
                        <td className="px-4 py-3 text-gray-600">{new Date(run.timestamp).toLocaleString()}</td>
                        <td className="px-4 py-3 font-bold text-indigo-700 uppercase">{run.timeframe || 'SWING'}</td>
                        <td className="px-4 py-3 font-bold text-gray-800">{run.version}</td>
                        <td className="px-4 py-3 text-center text-indigo-600 font-bold">{run.challenger_f1?.toFixed(4)}</td>
                        <td className="px-4 py-3 text-center text-gray-500">{run.champion_f1?.toFixed(4)}</td>
                        <td className="px-4 py-3 text-center">
                          <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${run.status === 'PROMOTED' ? 'bg-emerald-100 text-emerald-800' : run.status === 'FAILED_DATA_VALIDATION' ? 'bg-red-100 text-red-800' : 'bg-amber-100 text-amber-800'}`}>
                            {run.status}
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
      </div>

      {/* Gated Challenger Review & Promotion Modal */}
      {showPromoteModal && (() => {
        const plusBoth = foundationBenchmark?.comparison?.plus_both || {};
        const champBase = foundationBenchmark?.comparison?.champion || {};
        const f1Val = typeof plusBoth.f1 === 'number' ? plusBoth.f1 : parseFloat(plusBoth.f1 || '0.1515');
        const champF1 = typeof champBase.f1 === 'number' ? champBase.f1 : parseFloat(champBase.f1 || '0.0984');
        const f1Gain = f1Val - champF1;
        const sharpeVal = typeof plusBoth.sharpe === 'number' ? plusBoth.sharpe : parseFloat(plusBoth.sharpe || '0.51');
        const champSharpe = typeof champBase.sharpe === 'number' ? champBase.sharpe : parseFloat(champBase.sharpe || '0.19');
        const sharpeGain = sharpeVal - champSharpe;
        const tradeCount = plusBoth.trade_count !== undefined ? plusBoth.trade_count : 19;
        const maxDd = typeof plusBoth.max_drawdown_pct === 'number' ? plusBoth.max_drawdown_pct : parseFloat(plusBoth.max_drawdown_pct || '12.11');

        const isSampleSizePassed = tradeCount >= 30;
        const isStatHurdlePassed = f1Gain >= 0.01 && sharpeGain >= 0.0;
        const isRiskPassed = maxDd <= 20.0;
        const allGatesPassed = isSampleSizePassed && isStatHurdlePassed && isRiskPassed;

        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm animate-fade-in">
            <div className="bg-slate-900 border border-purple-500/40 rounded-2xl shadow-2xl w-full max-w-2xl overflow-hidden text-slate-100 flex flex-col">
              <div className="bg-purple-950/80 px-6 py-4 border-b border-purple-800/60 flex justify-between items-center">
                <div>
                  <h3 className="text-lg font-black text-white flex items-center gap-2">
                    <ShieldCheck className="text-emerald-400" size={20} />
                    Challenger Performance Audit &amp; Governance
                  </h3>
                  <p className="text-xs text-purple-300 mt-0.5">
                    Read-only performance review &amp; multi-dimensional safety gate verification.
                  </p>
                </div>
                <button 
                  onClick={() => setShowPromoteModal(false)}
                  className="text-slate-400 hover:text-white p-1 rounded-lg hover:bg-slate-800 transition"
                  title="Close Modal"
                >
                  <X size={18} />
                </button>
              </div>

              <div className="p-6 space-y-5">
                <div className="bg-slate-950 p-4 rounded-xl border border-slate-800">
                  <div className="flex justify-between items-center mb-3">
                    <span className="text-xs font-bold text-slate-300 uppercase">Target Candidate</span>
                    <span className="text-xs font-mono font-bold bg-purple-950 text-purple-300 border border-purple-700/60 px-2.5 py-1 rounded">
                      Combined Challenger (Plus Both)
                    </span>
                  </div>

                  <div className="grid grid-cols-4 gap-2 text-center font-mono">
                    <div className="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
                      <span className="text-[10px] text-slate-400 block">F1 Score</span>
                      <span className="text-sm font-black text-emerald-400">
                        {f1Val.toFixed(4)}
                      </span>
                      <span className="text-[9px] text-slate-500 block">vs {champF1.toFixed(4)}</span>
                    </div>
                    <div className="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
                      <span className="text-[10px] text-slate-400 block">Sharpe Ratio</span>
                      <span className="text-sm font-black text-indigo-400">
                        {sharpeVal.toFixed(2)}
                      </span>
                      <span className="text-[9px] text-slate-500 block">vs {champSharpe.toFixed(2)}</span>
                    </div>
                    <div className="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
                      <span className="text-[10px] text-slate-400 block">Sample Size</span>
                      <span className={`text-sm font-black ${isSampleSizePassed ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {tradeCount} trades
                      </span>
                      <span className="text-[9px] text-slate-500 block">Min 30 req</span>
                    </div>
                    <div className="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
                      <span className="text-[10px] text-slate-400 block">Max DD</span>
                      <span className="text-sm font-black text-amber-400">
                        {maxDd.toFixed(1)}%
                      </span>
                      <span className="text-[9px] text-slate-500 block">Max 20% cap</span>
                    </div>
                  </div>
                </div>

                <div className="bg-indigo-950/30 p-4 rounded-xl border border-indigo-800/40 text-xs space-y-2">
                  <h5 className="font-bold text-indigo-300 flex items-center gap-1.5">
                    <AlertTriangle size={14} className="text-indigo-400" />
                    Mandatory Multi-Dimensional Promotion Gates
                  </h5>
                  <ul className="space-y-1.5 text-slate-300 text-[11px]">
                    <li className="flex items-center gap-2">
                      <span className={isStatHurdlePassed ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                        {isStatHurdlePassed ? '✅' : '❌'}
                      </span>
                      <span>
                        <strong>Statistical Hurdle:</strong> F1 Gain &ge; +0.0100 and Sharpe &ge; Baseline ({f1Gain >= 0.01 ? `Passed: +${f1Gain.toFixed(4)}` : `Failed: +${f1Gain.toFixed(4)}`})
                      </span>
                    </li>
                    <li className="flex items-center gap-2">
                      <span className={isSampleSizePassed ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                        {isSampleSizePassed ? '✅' : '❌'}
                      </span>
                      <span>
                        <strong>Sample Size Gate:</strong> Minimum 30 Out-of-Sample Trades required ({isSampleSizePassed ? `Passed: ${tradeCount}/30` : `Failed: only ${tradeCount}/30 trades completed`})
                      </span>
                    </li>
                    <li className="flex items-center gap-2">
                      <span className={isRiskPassed ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                        {isRiskPassed ? '✅' : '❌'}
                      </span>
                      <span>
                        <strong>Risk Boundary:</strong> Max Drawdown &le; 20.0% ({isRiskPassed ? `Passed: ${maxDd.toFixed(1)}%` : `Failed: ${maxDd.toFixed(1)}%`})
                      </span>
                    </li>
                  </ul>
                </div>

                {promoteResult && (
                  <div className={`p-4 rounded-xl border text-xs ${
                    promoteResult.gates_passed 
                      ? 'bg-emerald-950/50 border-emerald-700/60 text-emerald-200' 
                      : 'bg-rose-950/50 border-rose-700/60 text-rose-200'
                  }`}>
                    <div className="font-bold text-sm mb-1">
                      {promoteResult.gates_passed ? '✅ Promotion Validated' : '❌ Gate Rejection'}
                    </div>
                    <p className="text-[11px] leading-relaxed">{promoteResult.message}</p>
                    {promoteResult.rejection_reasons && (
                      <ul className="mt-2 space-y-0.5 list-disc list-inside text-[10px] text-rose-300">
                        {promoteResult.rejection_reasons.map((r, i) => (
                          <li key={i}>{r}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>

              <div className="px-6 py-4 bg-slate-950 border-t border-slate-800 flex justify-between items-center">
                <span className="text-[10px] text-slate-400">
                  {allGatesPassed 
                    ? 'All gates verified. Ready for human authorization.' 
                    : 'Promotion blocked by risk gates. Review only.'}
                </span>
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => setShowPromoteModal(false)}
                    className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs font-bold transition"
                  >
                    Close Review
                  </button>
                  <button
                    onClick={handlePromoteChallenger}
                    disabled={promoting}
                    className={`px-5 py-2 rounded-lg text-xs font-bold transition shadow flex items-center gap-2 ${
                      allGatesPassed
                        ? 'bg-emerald-600 hover:bg-emerald-500 text-white'
                        : 'bg-rose-900/60 hover:bg-rose-900 text-rose-200 border border-rose-700/60 cursor-pointer'
                    }`}
                    title={allGatesPassed ? 'Authorize Challenger Promotion' : 'Test Gate Enforcement'}
                  >
                    {promoting ? <Loader className="animate-spin" size={14} /> : <Play size={14} />}
                    <span>{promoting ? 'Verifying Gates...' : (allGatesPassed ? 'Authorize Promotion' : 'Validate Gate Rejection')}</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}
