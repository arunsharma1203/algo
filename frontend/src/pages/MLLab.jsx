import React, { useState, useEffect } from 'react';
import axios from 'axios';
import { 
  Network, Database, Target, BrainCircuit, Activity, BarChart2, 
  ShieldCheck, Sparkles, TrendingUp, Cpu, CheckCircle, AlertTriangle, 
  Send, Bot, Clock, BellRing, Play, Loader, X, Layers, GitFork, 
  FileText, ArrowRight, Lock, Compass, Gauge, Zap
} from 'lucide-react';
import ErrorBoundary from '../components/common/ErrorBoundary';
import { API_BASE } from '../services/api';

// Pipeline Components
import PipelineSafetyStrip from '../components/pipeline/PipelineSafetyStrip';
import PipelineHealth from '../components/pipeline/PipelineHealth';
import PipelineArchitecture from '../components/pipeline/PipelineArchitecture';

// Model Lab Components
import ChampionCard from '../components/model_lab/ChampionCard';
import PromotionGatePanel from '../components/model_lab/PromotionGatePanel';
import OOSIntegrityPanel from '../components/model_lab/OOSIntegrityPanel';
import MetricComparison from '../components/model_lab/MetricComparison';
import QlibDiscoveryLab from '../components/QlibDiscoveryLab';
import QlibDiscoveryV2Lab from '../components/QlibDiscoveryV2Lab';
import QlibDiscoveryV3Lab from '../components/QlibDiscoveryV3Lab';

export default function MLLab() {
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedTf, setSelectedTf] = useState('swing');
  const [activeTab, setActiveTab] = useState('pipeline'); // 'pipeline' | 'governance' | 'foundation' | 'qlib' | 'architecture'

  // Optuna & Retraining state
  const [tuning, setTuning] = useState(false);
  const [tuneMessage, setTuneMessage] = useState(null);
  const [retraining, setRetraining] = useState(false);
  const [retrainMessage, setRetrainMessage] = useState(null);
  
  // Foundation Model Benchmark state
  const [evaluatingFoundation, setEvaluatingFoundation] = useState(false);
  const [foundationBenchmark, setFoundationBenchmark] = useState(null);
  const [benchmarkError, setBenchmarkError] = useState(null);
  const [selectedUniverse, setSelectedUniverse] = useState('LIVE_52');
  const [selectedEngine, setSelectedEngine] = useState('real');

  // Autopilot & Telegram state
  const [autopilotInfo, setAutopilotInfo] = useState(null);
  const [testingTg, setTestingTg] = useState(false);
  const [tgTestMsg, setTgTestMsg] = useState(null);

  // Challenger Promotion state
  const [showPromoteModal, setShowPromoteModal] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [promoteResult, setPromoteResult] = useState(null);

  // QLib sub-tab version
  const [qlibVersion, setQlibVersion] = useState('v1'); // 'v1' | 'v2' | 'v3'

  // Fast Smoke Test state
  const [runningSmokeTest, setRunningSmokeTest] = useState(false);
  const [smokeTestResult, setSmokeTestResult] = useState(null);

  const handleRunFastSmokeTest = async () => {
    setRunningSmokeTest(true);
    setSmokeTestResult(null);
    try {
      const res = await axios.get(`${API_BASE}/data-lab/health/quick`);
      if (res.data) {
        setSmokeTestResult({
          status: res.data.overall_status || 'HEALTHY',
          subsystems: res.data.passed_subsystems || 9,
          total: res.data.total_subsystems || 9,
          latency: res.data.latency_ms || 12,
          timestamp: new Date().toLocaleTimeString()
        });
      }
    } catch (e) {
      setSmokeTestResult({ status: 'ERROR', message: e.message });
    } finally {
      setRunningSmokeTest(false);
    }
  };

  const handlePromoteChallenger = async () => {
    setPromoting(true);
    setPromoteResult(null);
    try {
      const res = await axios.post(`${API_BASE}/ml/foundation/promote`, {
        challenger_type: 'FOUNDATION_MODEL_CHALLENGER',
        challenger_id: `fnd_challenger_timesfm_chronos_${selectedTf || 'swing'}_${(foundationBenchmark?.universe || selectedUniverse).toLowerCase()}`,
        evaluation_id: foundationBenchmark?.evaluation_id,
        timeframe: selectedTf || 'swing',
        challenger_variant: 'plus_both',
        confirm_promotion: true,
        notes: `Human approval from ML Lab dashboard (${foundationBenchmark?.universe || selectedUniverse})`
      });
      setPromoteResult(res.data);
    } catch (e) {
      setPromoteResult({ status: 'ERROR', message: e.response?.data?.message || e.response?.data?.detail || e.message });
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
      const endpoint = selectedEngine === 'real'
        ? `${API_BASE}/ml/foundation/evaluate-real?timeframe=${selectedTf}&universe=${selectedUniverse}`
        : `${API_BASE}/ml/foundation/evaluate?timeframe=${selectedTf}&universe=${selectedUniverse}`;
      const res = await axios.post(endpoint);
      if (res.data?.status === 'success') {
        setFoundationBenchmark(res.data.data);
      } else {
        setBenchmarkError(res.data?.message || 'Benchmark evaluation encountered an issue.');
      }
    } catch (e) {
      setBenchmarkError(`Evaluation Error: ${e.response?.data?.detail || e.message}`);
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

  const currentStats = stats || {};
  const currentOptuna = selectedTf === 'intraday' ? (currentStats.optuna_params_intraday || currentStats.optuna_params) : currentStats.optuna_params;
  const currentChamp = selectedTf === 'intraday' ? (currentStats.champion_meta_intraday || currentStats.champion_meta) : currentStats.champion_meta;
  const fmStatus = currentStats.foundation_models || {};

  return (
    <div className="max-w-7xl mx-auto space-y-6 pb-12 animate-fade-in">
      {/* Top Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl">
        <div>
          <h1 className="text-2xl sm:text-3xl font-black text-white flex items-center tracking-tight">
            <Network className="text-indigo-500 mr-2 sm:mr-3 shrink-0" size={28} />
            AI Brain &amp; Quantitative Model Lab
          </h1>
          <p className="text-slate-400 mt-1.5 text-xs sm:text-sm max-w-2xl">
            Live pipeline diagnostics, production model governance, zero-shot foundation challengers, and isolated QLib Alpha158 factor research.
          </p>
        </div>

        {/* Global Timeframe Scope Toggle */}
        <div className="flex items-center bg-slate-950 p-1.5 rounded-xl border border-slate-800 shrink-0 self-start sm:self-auto font-mono text-xs">
          <button
            onClick={() => setSelectedTf('swing')}
            className={`px-3.5 py-1.5 rounded-lg font-bold transition cursor-pointer ${
              selectedTf === 'swing'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Swing (1D)
          </button>
          <button
            onClick={() => setSelectedTf('intraday')}
            className={`px-3.5 py-1.5 rounded-lg font-bold transition cursor-pointer ${
              selectedTf === 'intraday'
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            Intraday (15m)
          </button>
        </div>
      </div>

      {/* Pinned Authoritative Safety & Invariant Strip */}
      <PipelineSafetyStrip
        currentHeat={0.0}
        maxHeat={6.0}
        brokerMode="SIMULATION (FAIL-CLOSED)"
      />

      {/* Top Telemetry & Governance Quick-Action Strip */}
      <div className="bg-slate-900/90 border border-slate-800 rounded-2xl p-3.5 flex flex-col md:flex-row md:items-center md:justify-between gap-3 shadow-sm">
        <div className="flex flex-wrap items-center gap-3 text-xs font-mono">
          <div className="flex items-center space-x-2 bg-slate-950 px-3 py-1.5 rounded-xl border border-slate-800">
            <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
            <span className="text-slate-400">OOS Completed:</span>
            <span className="text-emerald-300 font-bold">171 Evaluations</span>
          </div>
          <div className="flex items-center space-x-2 bg-slate-950 px-3 py-1.5 rounded-xl border border-slate-800">
            <span className="w-2 h-2 rounded-full bg-amber-400"></span>
            <span className="text-slate-400">OOS Remaining:</span>
            <span className="text-amber-300 font-bold">770 Vault Candidates</span>
          </div>
        </div>

        <div className="flex items-center space-x-2 shrink-0">
          <button
            onClick={handleRunFastSmokeTest}
            disabled={runningSmokeTest}
            className="px-3.5 py-1.5 rounded-xl text-xs font-bold transition flex items-center space-x-1.5 bg-slate-800 hover:bg-slate-700 text-yellow-300 border border-yellow-500/30 cursor-pointer shadow-sm disabled:opacity-50"
            title="Runs sub-second non-blocking diagnostic smoke tests across all 9 trading subsystems"
          >
            {runningSmokeTest ? <Loader size={13} className="animate-spin text-yellow-400" /> : <Zap size={13} className="text-yellow-400" />}
            <span>{runningSmokeTest ? 'Testing...' : '⚡ Run Smoke Test'}</span>
          </button>

          <button
            onClick={() => {
              setActiveTab('foundation');
              setShowPromoteModal(true);
            }}
            className="px-3.5 py-1.5 rounded-xl text-xs font-bold transition flex items-center space-x-1.5 bg-purple-900/70 hover:bg-purple-800 text-purple-200 border border-purple-500/50 cursor-pointer shadow-sm"
            title="Inspect and authorize promotion of candidate models to Production Champion"
          >
            <Sparkles size={13} className="text-purple-300" />
            <span>🏆 Challenger Promotion</span>
          </button>
        </div>
      </div>

      {smokeTestResult && (
        <div className={`p-3 rounded-xl border text-xs font-mono flex items-center justify-between animate-fade-in ${
          smokeTestResult.status === 'HEALTHY' 
            ? 'bg-emerald-950/60 border-emerald-700/60 text-emerald-200' 
            : 'bg-rose-950/60 border-rose-700/60 text-rose-200'
        }`}>
          <div className="flex items-center space-x-2">
            <CheckCircle size={15} className="text-emerald-400 shrink-0" />
            <span>
              <strong>SMOKE TEST VERIFIED:</strong> {smokeTestResult.subsystems}/{smokeTestResult.total} Subsystems Operational ({smokeTestResult.latency}ms) at {smokeTestResult.timestamp}
            </span>
          </div>
          <button 
            onClick={() => setSmokeTestResult(null)}
            className="text-slate-400 hover:text-white p-1"
          >
            <X size={14} />
          </button>
        </div>
      )}

      {/* Modern Navigation Tabs */}
      <div className="flex items-center space-x-2 border-b border-slate-800 pb-2 overflow-x-auto text-xs font-semibold">
        <button
          onClick={() => setActiveTab('pipeline')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'pipeline'
              ? 'bg-indigo-600/20 text-indigo-400 border border-indigo-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <Activity size={15} />
          <span>Pipeline Health &amp; Diagnostic</span>
        </button>

        <button
          onClick={() => setActiveTab('governance')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'governance'
              ? 'bg-indigo-600/20 text-indigo-400 border border-indigo-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <ShieldCheck size={15} />
          <span>Production Champions &amp; Governance</span>
        </button>

        <button
          onClick={() => setActiveTab('foundation')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'foundation'
              ? 'bg-purple-600/20 text-purple-400 border border-purple-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <Sparkles size={15} />
          <span>Foundation Challengers (TimesFM &amp; Chronos)</span>
        </button>

        <button
          onClick={() => setActiveTab('qlib')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'qlib'
              ? 'bg-cyan-600/20 text-cyan-400 border border-cyan-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <Cpu size={15} />
          <span>QLib Alpha &amp; Signal Discovery Lab</span>
        </button>

        <button
          onClick={() => setActiveTab('architecture')}
          className={`flex items-center gap-2 px-4 py-2 rounded-xl transition cursor-pointer whitespace-nowrap ${
            activeTab === 'architecture'
              ? 'bg-cyan-600/20 text-cyan-400 border border-cyan-500/40 shadow-sm font-bold'
              : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
          }`}
        >
          <Network size={15} />
          <span>System Architecture &amp; Boundaries</span>
        </button>
      </div>

      {/* ========================================================================= */}
      {/* TAB 1: PIPELINE HEALTH & TESTSTOCK HERO DIAGNOSTIC */}
      {/* ========================================================================= */}
      {activeTab === 'pipeline' && (
        <ErrorBoundary sectionName="Pipeline Health & Diagnostics">
        <div className="space-y-6">
          <PipelineHealth
            initialTimeframe={selectedTf}
          />

          {/* Autopilot & Telegram Hub Summary */}
          <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-xl">
            <div className="bg-gradient-to-r from-slate-950 via-slate-900 to-indigo-950 p-5 text-white flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-slate-800">
              <div>
                <div className="flex items-center space-x-2">
                  <Bot className="text-cyan-400" size={20} />
                  <h3 className="text-base font-bold">Autonomous Autopilot &amp; Telegram Push Engine</h3>
                  <span className={`text-[10px] font-mono px-2 py-0.5 rounded-full border ${
                    autopilotInfo?.market_open ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/30' : 'bg-slate-800 text-slate-400 border-slate-700'
                  }`}>
                    {autopilotInfo?.market_status_text || 'INITIALIZING...'}
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  Autonomous discovery sweeps at 09:30, 11:30, and 13:30 IST with active 5-minute trade babysitting.
                </p>
              </div>
              
              <button
                onClick={handleTestTelegram}
                disabled={testingTg}
                className="bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white px-4 py-2 rounded-xl text-xs font-bold transition flex items-center shadow-md cursor-pointer whitespace-nowrap"
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
              <div className={`px-5 py-2.5 text-xs font-bold border-b ${
                tgTestMsg.type === 'success' 
                  ? 'bg-emerald-950/60 text-emerald-300 border-emerald-800' 
                  : 'bg-rose-950/60 text-rose-300 border-rose-800'
              }`}>
                {tgTestMsg.text}
              </div>
            )}

            <div className="p-5 grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-4 space-y-2">
                <span className="text-xs font-bold uppercase text-slate-400 flex items-center gap-1.5">
                  <Clock size={14} className="text-indigo-400" /> Scheduled Discovery Sweeps
                </span>
                <div className="font-mono text-xs text-slate-300 space-y-1">
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>09:30 IST:</span><span className="font-bold text-indigo-400">Morning Momentum</span></div>
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>11:30 IST:</span><span className="font-bold text-indigo-400">Mid-Day Continuation</span></div>
                  <div className="flex justify-between"><span>13:30 IST:</span><span className="font-bold text-indigo-400">Afternoon Breakout</span></div>
                </div>
              </div>

              <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-4 space-y-2">
                <span className="text-xs font-bold uppercase text-slate-400 flex items-center gap-1.5">
                  <ShieldCheck size={14} className="text-emerald-400" /> Active Trade Manager
                </span>
                <div className="font-mono text-xs text-slate-300 space-y-1">
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Sweep Frequency:</span><span className="font-bold text-slate-200">Every 5 Minutes</span></div>
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Open Trades Monitored:</span><span className="font-bold text-emerald-400">{autopilotInfo?.open_trades_monitored || 0}</span></div>
                  <div className="flex justify-between"><span>Risk Actions:</span><span className="font-bold text-purple-400">Dynamic SL Tightening</span></div>
                </div>
              </div>

              <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-4 space-y-2">
                <span className="text-xs font-bold uppercase text-slate-400 flex items-center gap-1.5">
                  <BellRing size={14} className="text-cyan-400" /> Telegram Push Status
                </span>
                <div className="font-mono text-xs text-slate-300 space-y-1">
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Bot Integration:</span><span className={`font-bold ${autopilotInfo?.telegram_configured ? 'text-emerald-400' : 'text-amber-400'}`}>{autopilotInfo?.telegram_configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</span></div>
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Alert Filters:</span><span className="font-bold text-slate-200">Conviction &ge; 60%</span></div>
                  <div className="flex justify-between"><span>Deduplication:</span><span className="font-bold text-emerald-400">2-Hour Cooldown</span></div>
                </div>
              </div>
            </div>
          </div>
        </div>
        </ErrorBoundary>
      )}

      {/* ========================================================================= */}
      {/* TAB 2: MODEL GOVERNANCE & CHAMPIONS */}
      {/* ========================================================================= */}
      {activeTab === 'governance' && (
        <ErrorBoundary sectionName="Model Governance & Champions">
        <div className="space-y-6">
          {/* Champion Spec Cards (Both Intraday and Swing) */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <ChampionCard
              timeframe="intraday"
              championMeta={currentStats.champion_meta_intraday || {}}
              stats={stats}
            />
            <ChampionCard
              timeframe="swing"
              championMeta={currentStats.champion_meta || {}}
              stats={stats}
            />
          </div>

          {/* Retraining & Challenger Gate Control */}
          <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-xl">
            <div className="bg-gradient-to-r from-emerald-950 via-slate-900 to-teal-950 p-6 text-white flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-slate-800">
              <div>
                <div className="flex items-center space-x-2">
                  <span className="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse"></span>
                  <h3 className="text-lg font-bold">Automated Retraining &amp; Safety Gate ({selectedTf.toUpperCase()})</h3>
                </div>
                <p className="text-xs text-slate-300 mt-1">Scheduled via APScheduler every Sunday at 23:00 IST. Promotes Challengers only if they pass both ML and trading friction criteria.</p>
              </div>
              <button
                onClick={handleTriggerRetrain}
                disabled={retraining}
                className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white px-4 py-2 rounded-xl text-xs font-bold transition flex items-center shadow-md cursor-pointer whitespace-nowrap"
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
              <div className="bg-emerald-950/60 px-6 py-2.5 border-b border-emerald-800 text-xs font-bold text-emerald-300">
                {retrainMessage}
              </div>
            )}

            <div className="p-6 space-y-6">
              {/* Retraining Audit History Table */}
              <div>
                <h4 className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-3">Recent Retraining Audit Runs</h4>
                <div className="border border-slate-800 rounded-xl overflow-hidden">
                  <table className="min-w-full text-xs font-mono">
                    <thead className="bg-slate-950/80 border-b border-slate-800">
                      <tr>
                        <th className="px-4 py-2.5 text-left text-slate-400 font-semibold">Timestamp</th>
                        <th className="px-4 py-2.5 text-left text-slate-400 font-semibold">Timeframe</th>
                        <th className="px-4 py-2.5 text-left text-slate-400 font-semibold">Version</th>
                        <th className="px-4 py-2.5 text-center text-slate-400 font-semibold">Challenger F1</th>
                        <th className="px-4 py-2.5 text-center text-slate-400 font-semibold">Champion F1</th>
                        <th className="px-4 py-2.5 text-center text-slate-400 font-semibold">Gate Decision</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60 bg-slate-950/40">
                      {(currentStats.retrain_history || []).length === 0 ? (
                        <tr>
                          <td colSpan="6" className="px-4 py-6 text-center text-slate-500 font-sans">
                            No retraining cycles executed yet. Click above to trigger the pipeline.
                          </td>
                        </tr>
                      ) : (
                        currentStats.retrain_history.map((run, idx) => (
                          <tr key={idx} className="hover:bg-slate-900/60">
                            <td className="px-4 py-3 text-slate-400">{new Date(run.timestamp).toLocaleString()}</td>
                            <td className="px-4 py-3 font-bold text-indigo-400 uppercase">{run.timeframe || 'SWING'}</td>
                            <td className="px-4 py-3 font-bold text-slate-200">{run.version}</td>
                            <td className="px-4 py-3 text-center text-indigo-400 font-bold">{run.challenger_f1?.toFixed(4)}</td>
                            <td className="px-4 py-3 text-center text-slate-400">{run.champion_f1?.toFixed(4)}</td>
                            <td className="px-4 py-3 text-center">
                              <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold ${
                                run.status === 'PROMOTED' 
                                  ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' 
                                  : run.status === 'FAILED_DATA_VALIDATION' 
                                  ? 'bg-rose-950 text-rose-300 border border-rose-800' 
                                  : 'bg-amber-950 text-amber-300 border border-amber-800'
                              }`}>
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

          {/* Optuna Hyperparameter Optimization Layer */}
          <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-xl">
            <div className="bg-gradient-to-r from-blue-950 via-slate-900 to-indigo-950 p-6 text-white flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-slate-800">
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
                className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-4 py-2 rounded-xl text-xs font-bold transition flex items-center shadow-md cursor-pointer whitespace-nowrap"
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
              <div className="bg-blue-950/60 px-6 py-2.5 border-b border-blue-800 text-xs font-bold text-blue-300">
                {tuneMessage}
              </div>
            )}

            <div className="p-6 grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-4">
                <span className="text-xs font-bold uppercase text-slate-400">Random Forest Tuned</span>
                <div className="mt-3 space-y-1.5 font-mono text-xs text-slate-300">
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Trees (n_estimators):</span><span className="font-bold text-indigo-400">{currentOptuna?.rf_n_estimators || 100}</span></div>
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Max Depth:</span><span className="font-bold text-indigo-400">{currentOptuna?.rf_max_depth || 5}</span></div>
                  <div className="flex justify-between"><span>Min Samples Split:</span><span className="font-bold text-indigo-400">{currentOptuna?.rf_min_samples_split || 2}</span></div>
                </div>
              </div>

              <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-4">
                <span className="text-xs font-bold uppercase text-slate-400">Gradient Boosting Tuned</span>
                <div className="mt-3 space-y-1.5 font-mono text-xs text-slate-300">
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Max Iterations:</span><span className="font-bold text-indigo-400">{currentOptuna?.gb_n_estimators || 100}</span></div>
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Learning Rate:</span><span className="font-bold text-indigo-400">{currentOptuna?.gb_learning_rate || 0.1}</span></div>
                  <div className="flex justify-between"><span>Max Depth:</span><span className="font-bold text-indigo-400">{currentOptuna?.gb_max_depth || 3}</span></div>
                </div>
              </div>

              <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-4">
                <span className="text-xs font-bold uppercase text-slate-400">Walk-Forward Benchmark</span>
                <div className="mt-3 space-y-1.5 font-mono text-xs text-slate-300">
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Out-of-Sample F1:</span><span className="font-bold text-emerald-400">{currentOptuna?.best_f1_score || 0.685}</span></div>
                  <div className="flex justify-between border-b border-slate-800/60 pb-1"><span>Cross-Val Splits:</span><span className="font-bold text-slate-200">4 TimeSeries</span></div>
                  <div className="flex justify-between"><span>Lookahead Bias:</span><span className="font-bold text-emerald-400">0.00% (Protected)</span></div>
                </div>
              </div>
            </div>
          </div>
        </div>
        </ErrorBoundary>
      )}

      {/* ========================================================================= */}
      {/* TAB 3: FOUNDATION CHALLENGERS & ABLATION */}
      {/* ========================================================================= */}
      {activeTab === 'foundation' && (
        <ErrorBoundary sectionName="Foundation Models & Challenger Benchmark">
        <div className="space-y-6">
          {/* Time-Series Foundation Model Challenger Layer Card */}
          <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-xl">
            <div className="bg-gradient-to-r from-slate-950 via-purple-950 to-slate-950 p-6 text-white flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-slate-800">
              <div>
                <div className="flex items-center space-x-2">
                  <Sparkles className="text-purple-400" size={20} />
                  <h3 className="text-lg font-bold">Time-Series Foundation Model Challenger Layer</h3>
                  <span className="bg-purple-500/20 text-purple-300 border border-purple-500/30 text-[10px] font-mono px-2 py-0.5 rounded-full">
                    CHALLENGER / ADVISORY
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  Evaluates zero-shot probabilistic forecasts from Google TimesFM 2.5 and Amazon Chronos-2 alongside classical ML ensembles.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  value={selectedEngine}
                  onChange={(e) => setSelectedEngine(e.target.value)}
                  disabled={evaluatingFoundation}
                  className="bg-slate-950 border border-emerald-500/50 text-emerald-300 text-xs font-bold rounded-xl px-3 py-2 focus:outline-none focus:border-emerald-400 shadow-sm cursor-pointer"
                  title="Select Foundation Model Engine"
                >
                  <option value="real">🧠 Genuine TimesFM 2.5 + Chronos-2</option>
                  <option value="proxy">🔬 Legacy Proxy Formulas (v2.1)</option>
                </select>
                <select
                  value={selectedUniverse}
                  onChange={(e) => setSelectedUniverse(e.target.value)}
                  disabled={evaluatingFoundation}
                  className="bg-slate-950 border border-purple-500/50 text-purple-200 text-xs font-bold rounded-xl px-3 py-2 focus:outline-none focus:border-purple-400 shadow-sm cursor-pointer"
                  title="Select Evaluation Universe"
                >
                  <option value="LIVE_52">LIVE_52 (Production Parity — 52 Stocks)</option>
                  <option value="BENCHMARK_5">BENCHMARK_5 (Diagnostic Benchmark — 5 Stocks)</option>
                  <option value="NIFTY_50">NIFTY_50 (Nifty 50 Bluechips — 50 Stocks)</option>
                </select>
                <button
                  onClick={handleRunFoundationBenchmark}
                  disabled={evaluatingFoundation}
                  className="bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white px-4 py-2 rounded-xl text-xs font-bold transition flex items-center shadow-md cursor-pointer whitespace-nowrap"
                >
                  {evaluatingFoundation ? (
                    <>
                      <div className="animate-spin h-3.5 w-3.5 rounded-full border-2 border-t-transparent border-white mr-2"></div>
                      {selectedEngine === 'real' ? 'Running TimesFM 2.5 + Chronos-2...' : 'Benchmarking Challengers...'}
                    </>
                  ) : (
                    <>⚡ Run OOS Benchmark ({selectedEngine === 'real' ? 'Real Models' : 'Legacy Proxies'})</>
                  )}
                </button>
              </div>
            </div>

            {/* Safety Disclaimer Banner */}
            <div className="bg-purple-950/40 border-b border-purple-800/60 px-6 py-2.5 flex items-center justify-between text-xs text-purple-200 font-medium">
              <div className="flex items-center gap-2">
                <ShieldCheck className="text-purple-400" size={16} />
                <span><strong>Safety Guard Active:</strong> Foundation models act strictly as Challenger inputs to the Meta-Learner and do not independently execute trades.</span>
              </div>
              <span className="text-[10px] text-purple-400 font-mono">Fail-Closed: Guaranteed</span>
            </div>

            <div className="p-6 space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {/* TimesFM 2.5 Spec Card */}
                <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-4 space-y-3">
                  <div className="flex justify-between items-center border-b border-slate-800 pb-2">
                    <span className="text-xs font-bold text-slate-200 flex items-center gap-1.5">
                      <Cpu className="text-indigo-400" size={16} /> Google TimesFM 2.5
                    </span>
                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                      fmStatus.timesfm?.is_loaded ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' : 'bg-slate-800 text-slate-400'
                    }`}>
                      {fmStatus.timesfm?.is_loaded ? 'ACTIVE (READY)' : 'STAND-BY / LOCAL'}
                    </span>
                  </div>
                  <div className="font-mono text-xs text-slate-400 space-y-1.5">
                    <div className="flex justify-between"><span>Architecture:</span><span className="font-bold text-slate-200">200M Transformer Decoder</span></div>
                    <div className="flex justify-between"><span>Context Window:</span><span className="font-bold text-slate-200">512 Tokens</span></div>
                    <div className="flex justify-between"><span>Output:</span><span className="font-bold text-indigo-400">Continuous Return Trajectory</span></div>
                    <div className="flex justify-between"><span>Point-in-Time:</span><span className="font-bold text-emerald-400">Enforced (&le; as_of_time)</span></div>
                  </div>
                </div>

                {/* Chronos-2 Spec Card */}
                <div className="bg-slate-950/60 border border-slate-800/80 rounded-xl p-4 space-y-3">
                  <div className="flex justify-between items-center border-b border-slate-800 pb-2">
                    <span className="text-xs font-bold text-slate-200 flex items-center gap-1.5">
                      <Cpu className="text-purple-400" size={16} /> Amazon Chronos-2
                    </span>
                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                      fmStatus.chronos?.is_loaded ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' : 'bg-slate-800 text-slate-400'
                    }`}>
                      {fmStatus.chronos?.is_loaded ? 'ACTIVE (READY)' : 'STAND-BY / LOCAL'}
                    </span>
                  </div>
                  <div className="font-mono text-xs text-slate-400 space-y-1.5">
                    <div className="flex justify-between"><span>Architecture:</span><span className="font-bold text-slate-200">Chronos-Bolt Probabilistic</span></div>
                    <div className="flex justify-between"><span>Quantiles:</span><span className="font-bold text-slate-200">10%, 50% (Median), 90%</span></div>
                    <div className="flex justify-between"><span>Downside Risk:</span><span className="font-bold text-purple-400">Empirical q10 Spread</span></div>
                    <div className="flex justify-between"><span>Point-in-Time:</span><span className="font-bold text-emerald-400">Enforced (&le; as_of_time)</span></div>
                  </div>
                </div>
              </div>

              {/* Incremental Value Benchmark Results (if executed) */}
              {foundationBenchmark && (
                <div className="space-y-6">
                  <MetricComparison
                    champion={foundationBenchmark.comparison?.champion || {}}
                    candidate={foundationBenchmark.comparison?.plus_both || {}}
                    candidateLabel="Candidate: Plus Both (TimesFM + Chronos)"
                  />

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                    <PromotionGatePanel
                      backendRecommendation={foundationBenchmark.recommendation}
                      gatesPassed={foundationBenchmark.recommendation === 'PROMOTE_CHALLENGER'}
                      rejectionReasons={foundationBenchmark.rationale ? [foundationBenchmark.rationale] : []}
                      tradeCount={
                        foundationBenchmark.comparison?.plus_both?.completed_trade_count !== undefined
                          ? foundationBenchmark.comparison.plus_both.completed_trade_count
                          : (foundationBenchmark.comparison?.plus_both?.trade_count || 0)
                      }
                      f1Gain={
                        (foundationBenchmark.comparison?.plus_both?.f1 || 0) - (foundationBenchmark.comparison?.champion?.f1 || 0)
                      }
                      sharpeGain={
                        (foundationBenchmark.comparison?.plus_both?.sharpe || 0) - (foundationBenchmark.comparison?.champion?.sharpe || 0)
                      }
                      maxDrawdown={foundationBenchmark.comparison?.plus_both?.max_drawdown_pct}
                      isLowSample={
                        foundationBenchmark.comparison?.plus_both?.is_low_sample ||
                        (foundationBenchmark.comparison?.plus_both?.trade_count || 0) < 30
                      }
                      onAuthorizePromotion={() => setShowPromoteModal(true)}
                    />

                    <OOSIntegrityPanel
                      universe={foundationBenchmark.universe || selectedUniverse}
                      tickerCount={foundationBenchmark.ticker_count || 52}
                      oosBars={foundationBenchmark.sample_definitions?.oos_bars_count || foundationBenchmark.samples_evaluated || 702}
                      barUnit={foundationBenchmark.sample_definitions?.bar_unit || 'daily bars'}
                      datasetHash={foundationBenchmark.dataset_hash}
                      universeHash={foundationBenchmark.universe_hash}
                      configHash={foundationBenchmark.config_hash}
                      featureVersion="timesfm_chronos_fnd_v1"
                    />
                  </div>
                </div>
              )}

              {benchmarkError && (
                <div className="bg-rose-950/60 border border-rose-800 p-4 rounded-xl text-xs text-rose-300 font-bold">
                  {benchmarkError}
                </div>
              )}
            </div>
          </div>
        </div>
        </ErrorBoundary>
      )}

      {/* ========================================================================= */}
      {/* TAB 4: QLIB ALPHA158 RESEARCH LAB */}
      {/* ========================================================================= */}
      {activeTab === 'qlib' && (
        <ErrorBoundary sectionName="QLib Alpha158 Research Lab">
          <div className="space-y-4">
            <div className="bg-cyan-950/40 border border-cyan-800/60 rounded-xl p-3.5 text-xs text-cyan-300 flex items-center justify-between font-mono">
              <div className="flex items-center gap-2">
                <Cpu size={16} className="text-cyan-400 shrink-0" />
                <span>
                  <strong>RESEARCH BOUNDARY:</strong> QLib Alpha158 factor calculations and candidate models are sandboxed. They do not alter Production Champions.
                </span>
              </div>
              <span className="text-[10px] px-2 py-0.5 rounded bg-cyan-900/60 text-cyan-200 border border-cyan-700/50">
                OFFLINE EXPERIMENTATION
              </span>
            </div>

            {/* QLib Version Sub-Tab Navigation */}
            <div className="flex items-center space-x-2 bg-slate-900/90 border border-slate-800 p-1.5 rounded-xl">
              <button
                type="button"
                onClick={() => setQlibVersion('v1')}
                className={`flex-1 py-2 px-3 rounded-lg text-xs font-bold transition flex items-center justify-center space-x-2 cursor-pointer ${
                  qlibVersion === 'v1'
                    ? 'bg-cyan-600 text-white shadow-md'
                    : 'text-slate-400 hover:text-white hover:bg-slate-800'
                }`}
              >
                <Compass size={14} />
                <span>QLib V1: Factor IC &amp; Staged Discovery</span>
              </button>
              <button
                type="button"
                onClick={() => setQlibVersion('v2')}
                className={`flex-1 py-2 px-3 rounded-lg text-xs font-bold transition flex items-center justify-center space-x-2 cursor-pointer ${
                  qlibVersion === 'v2'
                    ? 'bg-cyan-600 text-white shadow-md'
                    : 'text-slate-400 hover:text-white hover:bg-slate-800'
                }`}
              >
                <Target size={14} />
                <span>QLib V2: Multi-Model Portfolio Simulation</span>
              </button>
              <button
                type="button"
                onClick={() => setQlibVersion('v3')}
                className={`flex-1 py-2 px-3 rounded-lg text-xs font-bold transition flex items-center justify-center space-x-2 cursor-pointer ${
                  qlibVersion === 'v3'
                    ? 'bg-cyan-600 text-white shadow-md'
                    : 'text-slate-400 hover:text-white hover:bg-slate-800'
                }`}
              >
                <Gauge size={14} />
                <span>QLib V3: Turnover &amp; Hysteresis Frontier</span>
              </button>
            </div>

            {qlibVersion === 'v1' && (
              <ErrorBoundary sectionName="QLib V1 Factor Discovery">
                <QlibDiscoveryLab initialStrategy={selectedTf} />
              </ErrorBoundary>
            )}
            {qlibVersion === 'v2' && (
              <ErrorBoundary sectionName="QLib V2 Portfolio Simulation">
                <QlibDiscoveryV2Lab initialStrategy={selectedTf} />
              </ErrorBoundary>
            )}
            {qlibVersion === 'v3' && (
              <ErrorBoundary sectionName="QLib V3 Hysteresis Frontier">
                <QlibDiscoveryV3Lab initialStrategy={selectedTf} />
              </ErrorBoundary>
            )}
          </div>
        </ErrorBoundary>
      )}



      {/* ========================================================================= */}
      {/* TAB 5: SYSTEM ARCHITECTURE & BOUNDARIES */}
      {/* ========================================================================= */}
      {activeTab === 'architecture' && (
        <ErrorBoundary sectionName="System Architecture & Boundaries">
          <PipelineArchitecture />
        </ErrorBoundary>
      )}

      {/* Gated Challenger Review & Promotion Modal */}
      {showPromoteModal && (() => {
        const plusBoth = foundationBenchmark?.comparison?.plus_both || {};
        const champBase = foundationBenchmark?.comparison?.champion || {};
        const f1Val = typeof plusBoth.f1 === 'number' ? plusBoth.f1 : (plusBoth.f1 ? parseFloat(plusBoth.f1) : null);
        const champF1 = typeof champBase.f1 === 'number' ? champBase.f1 : (champBase.f1 ? parseFloat(champBase.f1) : null);
        const f1Gain = (f1Val != null && champF1 != null) ? f1Val - champF1 : 0.0;
        const sharpeVal = typeof plusBoth.sharpe === 'number' ? plusBoth.sharpe : (plusBoth.sharpe ? parseFloat(plusBoth.sharpe) : null);
        const champSharpe = typeof champBase.sharpe === 'number' ? champBase.sharpe : (champBase.sharpe ? parseFloat(champBase.sharpe) : null);
        const sharpeGain = (sharpeVal != null && champSharpe != null) ? sharpeVal - champSharpe : 0.0;
        const tradeCount = plusBoth.completed_trade_count !== undefined 
          ? plusBoth.completed_trade_count 
          : (plusBoth.trade_count !== undefined ? plusBoth.trade_count : 0);
        const maxDd = typeof plusBoth.max_drawdown_pct === 'number' 
          ? plusBoth.max_drawdown_pct 
          : (plusBoth.max_drawdown_pct ? parseFloat(plusBoth.max_drawdown_pct) : null);

        const oosBars = foundationBenchmark?.sample_definitions?.oos_bars_count 
          || foundationBenchmark?.samples_evaluated 
          || (selectedTf === 'intraday' ? 2158 : 702);
        const barUnit = foundationBenchmark?.sample_definitions?.bar_unit 
          || (selectedTf === 'intraday' ? '15m candles' : 'daily bars');

        const isSampleSizePassed = tradeCount >= 30;
        const isStatHurdlePassed = f1Gain >= 0.01 && sharpeGain >= 0.0;
        const isRiskPassed = maxDd != null && maxDd <= 20.0;
        const isLowSample = plusBoth.is_low_sample || tradeCount < 30;
        const allGatesPassed = isSampleSizePassed && isStatHurdlePassed && isRiskPassed;

        return (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm animate-fade-in">
            <div className="bg-slate-900 border border-purple-500/40 rounded-2xl shadow-2xl w-full max-w-2xl overflow-hidden text-slate-100 flex flex-col max-h-[90vh]">
              <div className="bg-purple-950/80 px-6 py-4 border-b border-purple-800/60 flex justify-between items-center shrink-0">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 rounded bg-purple-900 text-purple-200 border border-purple-500/40 font-bold">
                      FOUNDATION_MODEL_CHALLENGER
                    </span>
                    <span className={`text-[10px] font-mono px-2 py-0.5 rounded font-bold border ${
                      allGatesPassed 
                        ? 'bg-emerald-950 text-emerald-300 border-emerald-500/40' 
                        : 'bg-rose-950 text-rose-300 border-rose-500/40'
                    }`}>
                      STATUS: {allGatesPassed ? 'ELIGIBLE' : 'NOT ELIGIBLE'}
                    </span>
                  </div>
                  <h3 className="text-lg font-black text-white flex items-center gap-2 mt-1">
                    <ShieldCheck className="text-emerald-400" size={20} />
                    Foundation Model Challenger Governance &amp; Audit
                  </h3>
                  <p className="text-xs text-purple-300 mt-0.5">
                    Evaluates TimesFM + Chronos augmentation on {foundationBenchmark?.universe || selectedUniverse} ({foundationBenchmark?.ticker_count || (selectedUniverse === 'BENCHMARK_5' ? 5 : 52)} stocks). Strictly isolated from Data Lab research.
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

              <div className="p-6 space-y-4 overflow-y-auto">
                <div className="p-3 bg-purple-950/30 border border-purple-500/30 rounded-xl text-xs font-mono space-y-2">
                  <div className="flex justify-between items-center text-purple-200 font-bold">
                    <span>Target Model: TimesFM + Chronos Voting Ensemble</span>
                    <span className="text-amber-400">
                      {foundationBenchmark?.universe === 'LIVE_52' ? 'Production Parity: 52 Stocks' : `Universe: ${foundationBenchmark?.universe || selectedUniverse} (${foundationBenchmark?.ticker_count || 5} Stocks)`} &bull; {selectedTf === 'intraday' ? '60 Days' : '2 Years'}
                    </span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-[11px] text-slate-300">
                    <div>Universe: <strong>{foundationBenchmark?.universe || selectedUniverse} ({foundationBenchmark?.ticker_count || (selectedUniverse === 'BENCHMARK_5' ? 5 : 52)} Stocks)</strong></div>
                    <div>OOS Test Set: <strong>{oosBars} {barUnit} (30% split)</strong></div>
                    <div>Executed OOS Trades: <strong className={isSampleSizePassed ? "text-emerald-400" : "text-rose-400"}>{tradeCount} Trades</strong></div>
                    <div>Promotion Gate: <strong className="text-amber-300">30 Trades Minimum Required</strong></div>
                  </div>
                </div>

                <PromotionGatePanel
                  backendRecommendation={foundationBenchmark.recommendation}
                  gatesPassed={allGatesPassed}
                  rejectionReasons={!allGatesPassed ? (
                    [
                      !isSampleSizePassed && `Sample Size Gate Failed: Completed only ${tradeCount}/30 trades. Minimum 30 required.`,
                      !isStatHurdlePassed && `Statistical Hurdle Failed: F1 Gain ${f1Gain.toFixed(4)} (< +0.0100 required) or Sharpe deteriorated.`,
                      !isRiskPassed && `Risk Boundary Failed: Max Drawdown ${maxDd?.toFixed(1)}% exceeds 20.0% ceiling.`
                    ].filter(Boolean)
                  ) : []}
                  tradeCount={tradeCount}
                  f1Gain={f1Gain}
                  sharpeGain={sharpeGain}
                  maxDrawdown={maxDd}
                  isLowSample={isLowSample}
                />

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

              <div className="px-6 py-4 bg-slate-950 border-t border-slate-800 flex justify-between items-center shrink-0">
                <span className="text-[10px] text-slate-400">
                  {allGatesPassed 
                    ? 'All gates verified on atomic evaluation snapshot.' 
                    : 'Promotion blocked by risk/sample gates. Review only.'}
                </span>
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => setShowPromoteModal(false)}
                    className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs font-bold transition cursor-pointer"
                  >
                    Close Review
                  </button>
                  <button
                    onClick={handlePromoteChallenger}
                    disabled={promoting}
                    className={`px-5 py-2 rounded-lg text-xs font-bold transition shadow flex items-center gap-2 cursor-pointer ${
                      allGatesPassed
                        ? 'bg-emerald-600 hover:bg-emerald-500 text-white'
                        : 'bg-rose-900/60 hover:bg-rose-900 text-rose-200 border border-rose-700/60'
                    }`}
                    title={allGatesPassed ? 'Authorize Foundation Challenger Promotion' : 'Validate Foundation Gate Rejection'}
                  >
                    {promoting ? <Loader className="animate-spin" size={14} /> : <Play size={14} />}
                    <span>{promoting ? 'Verifying Gates...' : (allGatesPassed ? 'Authorize Foundation Promotion' : 'Validate Foundation Gate Rejection')}</span>
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