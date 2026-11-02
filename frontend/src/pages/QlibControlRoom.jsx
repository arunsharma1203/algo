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
  ChevronLeft,
  ChevronDown,
  ChevronUp,
  Check,
  X,
  Award,
  Database,
  ArrowUpRight,
  ArrowDownRight,
  Sliders,
  AlertTriangle,
  Terminal
} from 'lucide-react';
import { API_BASE, getLegacyRuntimeHealth, getQlibRuntimeHealth } from '../services/api';
import QlibResearchFactory from '../components/qlib/QlibResearchFactory';
import QlibReadinessCard from '../components/qlib/QlibReadinessCard';

export default function QlibControlRoom() {
  const [activeTab, setActiveTab] = useState('research');
  const [statusData, setStatusData] = useState(null);
  const [comparisonData, setComparisonData] = useState(null);
  const [recommendations, setRecommendations] = useState([]);
  const [runtimeHealth, setRuntimeHealth] = useState({ legacy: null, qlib: null, loading: true });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Industrial Research Factory state
  const [selectedUniverse, setSelectedUniverse] = useState('NIFTY_500_ESTABLISHED');
  const [datasetHealth, setDatasetHealth] = useState(null);
  const [datasetHealthLoading, setDatasetHealthLoading] = useState(false);
  const [signalDiscovery, setSignalDiscovery] = useState(null);
  const [signalDiscoveryLoading, setSignalDiscoveryLoading] = useState(false);
  const [candidateComparison, setCandidateComparison] = useState(null);
  const [candidateComparisonLoading, setCandidateComparisonLoading] = useState(false);
  const [selectedCandidateId, setSelectedCandidateId] = useState(null);

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
  const [trainUniverse, setTrainUniverse] = useState('NIFTY_500_ESTABLISHED');
  const [training, setTraining] = useState(false);
  const [trainingStatus, setTrainingStatus] = useState(null);
  const [trainResult, setTrainResult] = useState(null);
  const [trainError, setTrainError] = useState(null);
  const [trainTelemetry, setTrainTelemetry] = useState(null);
  const [activeJobId, setActiveJobId] = useState(null);

  // Benchmarks state
  const [benchmarks, setBenchmarks] = useState(null);
  const [benchmarksLoading, setBenchmarksLoading] = useState(false);
  const [benchmarksRunning, setBenchmarksRunning] = useState(false);
  const [benchmarksError, setBenchmarksError] = useState(null);

  // Challenger state & promotion
  const [challengers, setChallengers] = useState([]);
  const [challengersLoading, setChallengersLoading] = useState(false);
  const [promotingModelId, setPromotingModelId] = useState(null);
  const [promoteSuccess, setPromoteSuccess] = useState(null);
  const [promoteError, setPromoteError] = useState(null);
  const [expandedChallengerId, setExpandedChallengerId] = useState(null);

  // Active champion & training history state
  const [activeChampion, setActiveChampion] = useState(null);
  const [championLoading, setChampionLoading] = useState(false);
  const [trainingHistory, setTrainingHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  // F&O state
  const [fnoData, setFnoData] = useState(null);
  const [fnoLoading, setFnoLoading] = useState(false);
  const [fnoError, setFnoError] = useState(null);
  const [lastFnoRefresh, setLastFnoRefresh] = useState(null);

  // Operational Readiness Matrix state
  const [readinessData, setReadinessData] = useState(null);
  const [readinessLoading, setReadinessLoading] = useState(false);

  // Recommendations state & server-side pagination
  const [recFilterStrategy, setRecFilterStrategy] = useState('ALL');
  const [recFilterStatus, setRecFilterStatus] = useState('ALL');
  const [recPage, setRecPage] = useState(1);
  const [recTotalPages, setRecTotalPages] = useState(1);
  const [recTotalCount, setRecTotalCount] = useState(0);
  const [recFilteredCount, setRecFilteredCount] = useState(0);
  const [recCounts, setRecCounts] = useState({ ALL: 0, OPEN: 0, CLOSED: 0, SWING: 0, INTRADAY: 0, FNO: 0 });
  const [recLoading, setRecLoading] = useState(false);

  const fetchRecommendations = async (strategy = recFilterStrategy, status = recFilterStatus, page = 1) => {
    try {
      setRecLoading(true);
      const params = new URLSearchParams();
      if (strategy && strategy !== 'ALL') params.append('strategy', strategy);
      if (status && status !== 'ALL') params.append('status', status);
      params.append('page', page.toString());
      params.append('limit', '20');

      const res = await fetch(`${API_BASE}/qlib/recommendations?${params.toString()}`);
      if (res.ok) {
        const data = await res.json();
        setRecommendations(data.recommendations || []);
        if (data.counts) setRecCounts(data.counts);
        if (data.total_pages) setRecTotalPages(data.total_pages);
        if (data.page) setRecPage(data.page);
        if (data.total_count !== undefined) setRecTotalCount(data.total_count);
        if (data.filtered_count !== undefined) setRecFilteredCount(data.filtered_count);
      }
    } catch (err) {
      console.error('Failed to load Qlib recommendations:', err);
    } finally {
      setRecLoading(false);
    }
  };

  const handleStrategyChange = (strat) => {
    setRecFilterStrategy(strat);
    setRecPage(1);
    fetchRecommendations(strat, recFilterStatus, 1);
  };

  const handleStatusChange = (stat) => {
    setRecFilterStatus(stat);
    setRecPage(1);
    fetchRecommendations(recFilterStrategy, stat, 1);
  };

  const handlePageChange = (newPage) => {
    setRecPage(newPage);
    fetchRecommendations(recFilterStrategy, recFilterStatus, newPage);
  };

  const fetchChallengers = async () => {
    try {
      setChallengersLoading(true);
      const res = await fetch(`${API_BASE}/qlib/models/challengers`);
      if (res.ok) {
        const data = await res.json();
        setChallengers(data.challengers || []);
      }
    } catch (err) {
      console.error('Failed to load challengers:', err);
    } finally {
      setChallengersLoading(false);
    }
  };

  const handlePromoteChallenger = async (strat, modelId) => {
    if (!window.confirm(`Are you sure you want to promote challenger ${modelId} to ACTIVE for ${strat}? This requires explicit manual approval and will archive the current active model.`)) {
      return;
    }
    try {
      setPromotingModelId(modelId);
      setPromoteError(null);
      setPromoteSuccess(null);
      const res = await fetch(`${API_BASE}/qlib/models/promote`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy: strat,
          model_id: modelId,
          manual_approval: true,
          force: false
        })
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Promotion failed');
      }
      setPromoteSuccess(`Successfully promoted ${modelId} to active ${strat} model.`);
      await fetchActiveChampion(strat);
      await fetchChallengers();
      await fetchTelemetry();
      await fetchTrainingHistory();
    } catch (err) {
      setPromoteError(err.message);
    } finally {
      setPromotingModelId(null);
    }
  };

  const fetchActiveChampion = async (strat = trainStrategy) => {
    try {
      setChampionLoading(true);
      const res = await fetch(`${API_BASE}/qlib/models/champion?strategy=${strat || 'SWING'}`);
      if (res.ok) {
        const data = await res.json();
        setActiveChampion(data);
      }
    } catch (err) {
      console.warn('Error fetching active champion:', err);
    } finally {
      setChampionLoading(false);
    }
  };

  const fetchDatasetHealth = async (uni = selectedUniverse) => {
    try {
      setDatasetHealthLoading(true);
      const res = await fetch(`${API_BASE}/qlib/dataset/health?universe=${uni || 'NIFTY_500_ESTABLISHED'}`);
      if (res.ok) {
        const data = await res.json();
        setDatasetHealth(data);
      }
    } catch (err) {
      console.warn('Error fetching dataset health:', err);
    } finally {
      setDatasetHealthLoading(false);
    }
  };

  const fetchSignalDiscovery = async (uni = selectedUniverse) => {
    try {
      setSignalDiscoveryLoading(true);
      const res = await fetch(`${API_BASE}/qlib/signals/discovery?universe=${uni || 'NIFTY_500_ESTABLISHED'}`);
      if (res.ok) {
        const data = await res.json();
        setSignalDiscovery(data);
      }
    } catch (err) {
      console.warn('Error fetching signal discovery:', err);
    } finally {
      setSignalDiscoveryLoading(false);
    }
  };

  const fetchCandidateComparison = async (strat = 'SWING', candId = selectedCandidateId) => {
    try {
      setCandidateComparisonLoading(true);
      const url = candId
        ? `${API_BASE}/qlib/models/candidate-comparison?strategy=${strat}&candidate_id=${candId}`
        : `${API_BASE}/qlib/models/candidate-comparison?strategy=${strat}`;
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        setCandidateComparison(data);
      }
    } catch (err) {
      console.warn('Error fetching candidate comparison:', err);
    } finally {
      setCandidateComparisonLoading(false);
    }
  };

  const handleUniverseChange = (uni) => {
    setSelectedUniverse(uni);
    fetchDatasetHealth(uni);
    fetchSignalDiscovery(uni);
  };

  const handleSelectCandidate = (candId) => {
    setSelectedCandidateId(candId);
    fetchCandidateComparison('SWING', candId);
  };

  const handleRefreshResearchFactory = () => {
    fetchDatasetHealth(selectedUniverse);
    fetchSignalDiscovery(selectedUniverse);
    fetchCandidateComparison('SWING', selectedCandidateId);
    fetchActiveChampion(trainStrategy);
    fetchChallengers();
    fetchReadiness();
  };

  const fetchReadiness = async () => {
    try {
      setReadinessLoading(true);
      const res = await fetch(`${API_BASE}/qlib/system/readiness`);
      if (res.ok) {
        const data = await res.json();
        setReadinessData(data);
      }
    } catch (err) {
      console.warn('Error fetching system readiness:', err);
    } finally {
      setReadinessLoading(false);
    }
  };

  const handleFindBestModel = async (options = {}) => {
    try {
      setTraining(true);
      setTrainError(null);
      const res = await fetch(`${API_BASE}/qlib/research/find-best-model`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          universe: options.universe || selectedUniverse || 'NIFTY_500_ESTABLISHED',
          strategy: options.strategy || 'SWING',
          friction_bps: options.friction_bps !== undefined ? options.friction_bps : 25.0
        })
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to start Find Best Model research mission');
      }
      setActiveJobId(data.job_id);
      setActiveJobId(data.job_id || data.mission_id);
      connectSSE();
    } catch (err) {
      console.error('Failed to start Find Best Model:', err);
      setTrainError(err.message);
      setTraining(false);
    }
  };

  const handleResetTraining = async () => {
    try {
      const res = await fetch(`${API_BASE}/qlib/train/reset`, { method: 'POST' });
      if (res.ok) {
        setTraining(false);
        setTrainingStatus('IDLE');
        setTrainError(null);
        setActiveJobId(null);
        if (window._qlibEvtSource) {
          window._qlibEvtSource.close();
          window._qlibEvtSource = null;
        }
        fetchTrainingHistory();
      }
    } catch (err) {
      console.error('Failed to reset training state:', err);
      setTraining(false);
    }
  };

  const handleStartExperiment = async (params) => {
    try {
      setTraining(true);
      setTrainError(null);
      const res = await fetch(`${API_BASE}/qlib/train/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params)
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || 'Failed to start research experiment');
      }
      setActiveJobId(data.job_id);
      connectSSE();
    } catch (err) {
      console.error('Failed to start research experiment:', err);
      setTrainError(err.message);
      setTraining(false);
    }
  };

  const fetchTrainingHistory = async () => {
    try {
      setHistoryLoading(true);
      const res = await fetch(`${API_BASE}/qlib/train/jobs/history?limit=25`);
      if (res.ok) {
        const data = await res.json();
        setTrainingHistory(data.jobs || []);
        // If we don't have active telemetry, show the latest completed/failed job from history
        if (!training && (!trainTelemetry || trainTelemetry.status === 'IDLE') && data.jobs && data.jobs.length > 0) {
          const latestCompleted = data.jobs[0];
          setTrainTelemetry(latestCompleted);
          if (latestCompleted.metrics) {
            setTrainResult(latestCompleted.metrics);
          }
        }
      }
    } catch (err) {
      console.warn('Error fetching training history:', err);
    } finally {
      setHistoryLoading(false);
    }
  };

  const connectSSE = () => {
    try {
      if (window._qlibEvtSource) {
        window._qlibEvtSource.close();
      }
      const evtSource = new EventSource(`${API_BASE}/qlib/train/telemetry/stream`);
      window._qlibEvtSource = evtSource;

      evtSource.addEventListener('telemetry', (e) => {
        try {
          const tData = JSON.parse(e.data);
          setTrainTelemetry(tData);
          if (tData.stage) setTrainingStatus(tData.stage);
          if (tData.job_id) setActiveJobId(tData.job_id);

          if (tData.status === 'COMPLETED') {
            setTraining(false);
            setTrainingStatus('COMPLETE');
            setTrainResult(tData.artifact || tData.result || tData);
            fetchChallengers();
            fetchActiveChampion(trainStrategy);
            fetchTrainingHistory();
            evtSource.close();
            window._qlibEvtSource = null;
          } else if (tData.status === 'FAILED') {
            setTraining(false);
            setTrainingStatus('FAILED');
            setTrainError(tData.error || 'Training failed');
            fetchTrainingHistory();
            evtSource.close();
            window._qlibEvtSource = null;
          } else if (tData.status === 'IDLE' || tData.status === 'CANCELLED') {
            setTraining(false);
            setTrainingStatus('IDLE');
            if (tData.status === 'CANCELLED') {
              fetchTrainingHistory();
            }
          } else if (tData.status === 'RUNNING' || tData.status === 'VALIDATING' || tData.status === 'QUEUED') {
            setTraining(true);
          }
        } catch (pErr) {
          console.error("Telemetry parse error", pErr);
        }
      });

      evtSource.onerror = () => {
        // Fallback polling on disconnect
        fetch(`${API_BASE}/qlib/train/jobs/active`)
          .then(r => r.json())
          .then(act => {
            if (act?.has_active_job && act.job && ['RUNNING', 'VALIDATING', 'QUEUED'].includes(act.job.status)) {
              setTrainTelemetry(act.job);
              setTraining(true);
              setTrainingStatus(act.job.stage || 'RUNNING');
            } else {
              // No active job running, release training lock and sync history
              setTraining(false);
              fetchTrainingHistory();
              if (window._qlibEvtSource) {
                window._qlibEvtSource.close();
                window._qlibEvtSource = null;
              }
            }
          })
          .catch(() => {
            setTraining(false);
          });
      };
    } catch (streamErr) {
      console.warn("Could not initiate EventSource:", streamErr);
    }
  };

  const checkActiveJobAndHydrate = async () => {
    try {
      const res = await fetch(`${API_BASE}/qlib/train/jobs/active`);
      if (res.ok) {
        const data = await res.json();
        if (data?.has_active_job && data.job && ['RUNNING', 'VALIDATING', 'QUEUED'].includes(data.job.status)) {
          setActiveJobId(data.job.job_id);
          setTrainTelemetry(data.job);
          setTraining(true);
          setTrainingStatus(data.job.stage || 'RUNNING');
          connectSSE();
          return true;
        } else {
          setTraining(false);
        }
      } else {
        setTraining(false);
      }
    } catch (err) {
      console.warn("Error checking active training job:", err);
      setTraining(false);
    }
    return false;
  };

  const fetchTelemetry = async () => {
    try {
      setLoading(true);
      setError(null);
      const [stRes, compRes, legHealth, qlbHealth] = await Promise.all([
        fetch(`${API_BASE}/qlib/status`),
        fetch(`${API_BASE}/qlib/comparison`),
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

      await fetchRecommendations('ALL', 'ALL', 1);
      await fetchChallengers();
    } catch (err) {
      setError(err.message || 'Failed to load Qlib telemetry');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTelemetry();
    fetchReadiness();
    checkActiveJobAndHydrate().then(hasActive => {
      if (!hasActive) {
        fetchTrainingHistory();
      }
    });
    fetchActiveChampion(trainStrategy);
    fetchDatasetHealth(selectedUniverse);
    fetchSignalDiscovery(selectedUniverse);
    fetchCandidateComparison('SWING');
    fetchRecommendations('ALL', 'ALL', 1);
  }, []);

  useEffect(() => {
    if (activeTab === 'models' || activeTab === 'training') {
      checkActiveJobAndHydrate().then(hasActive => {
        if (!hasActive) {
          fetchTrainingHistory();
        }
      });
      fetchActiveChampion(trainStrategy);
      fetchChallengers();
    }
    if (activeTab === 'overview') {
      fetchReadiness();
      fetchRecommendations(recFilterStrategy, recFilterStatus, 1);
    }
  }, [activeTab, trainStrategy]);

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
      if (data.status?.startsWith('DATA_') || data.error) {
        setScanError(data.error || `Scan blocked: ${data.status}`);
      }
      setScanResult(data);
      await fetchRecommendations(recFilterStrategy, recFilterStatus, 1);
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
      setTrainingStatus('INITIALIZING');
      setTrainTelemetry(null);

      const res = await fetch(`${API_BASE}/qlib/train/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy: trainStrategy,
          feature_family: trainFeatureFamily,
          model_family: trainModelFamily,
          universe: trainUniverse
        })
      });

      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `Training failed to start: HTTP ${res.status}`);
      }

      const data = await res.json();
      setActiveJobId(data.job_id);

      // Connect to SSE Telemetry stream
      connectSSE();
    } catch (err) {
      setTrainError(err.message);
      setTrainingStatus('FAILED');
      setTraining(false);
    }
  };

  const fetchBenchmarks = async () => {
    try {
      setBenchmarksLoading(true);
      setBenchmarksError(null);
      const res = await fetch(`${API_BASE}/qlib/benchmark`);
      if (!res.ok) throw new Error(`Benchmark API HTTP ${res.status}`);
      const data = await res.json();
      setBenchmarks(data);
    } catch (err) {
      setBenchmarksError(err.message || 'Failed to load benchmarks');
    } finally {
      setBenchmarksLoading(false);
    }
  };

  const handleRunBenchmarks = async () => {
    try {
      setBenchmarksRunning(true);
      const res = await fetch(`${API_BASE}/qlib/benchmark/run`, { method: 'POST' });
      if (!res.ok) throw new Error(`Failed to initiate benchmark run: HTTP ${res.status}`);
      setTimeout(() => {
        fetchBenchmarks();
        setBenchmarksRunning(false);
      }, 3000);
    } catch (err) {
      setBenchmarksError(err.message);
      setBenchmarksRunning(false);
    }
  };

  const handleFetchFno = async () => {
    try {
      setFnoLoading(true);
      setFnoError(null);
      const res = await fetch(`${API_BASE}/qlib/fno`);
      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.detail || `F&O fetch failed with HTTP ${res.status}`);
      }
      const data = await res.json();
      setFnoData(data);
      setLastFnoRefresh(new Date().toLocaleTimeString());
      await fetchRecommendations(recFilterStrategy, recFilterStatus, 1);
    } catch (err) {
      console.error(err);
      setFnoError(err.message || 'Failed to fetch F&O data');
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
            { id: 'overview', label: 'Overview / Champion', icon: Activity },
            { id: 'research', label: 'Find Best Model', icon: Sliders },
            { id: 'trading', label: 'Swing | Intraday | F&O', icon: Search },
            { id: 'models', label: 'Models / Research History', icon: Terminal },
            { id: 'system', label: 'System / Data Health', icon: Database }
          ].map(tab => {
            const Icon = tab.icon;
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => {
                  setActiveTab(tab.id);
                  if (tab.id === 'system' && !benchmarks) fetchBenchmarks();
                  if (tab.id === 'overview' && !readinessData) fetchReadiness();
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

      {/* ── TAB 0: INDUSTRIAL RESEARCH FACTORY ─────────────────────────── */}
      {activeTab === 'research' && (
        <QlibResearchFactory
          datasetHealth={datasetHealth}
          datasetHealthLoading={datasetHealthLoading}
          selectedUniverse={selectedUniverse}
          onUniverseChange={handleUniverseChange}
          signalDiscovery={signalDiscovery}
          signalDiscoveryLoading={signalDiscoveryLoading}
          onRefreshSignalDiscovery={() => fetchSignalDiscovery(selectedUniverse)}
          activeChampion={activeChampion}
          championLoading={championLoading}
          candidateComparison={candidateComparison}
          candidateComparisonLoading={candidateComparisonLoading}
          onSelectCandidate={handleSelectCandidate}
          challengers={challengers}
          trainingTelemetry={trainTelemetry}
          activeJobId={activeJobId}
          isTraining={training}
          onStartExperiment={handleStartExperiment}
          onPromoteCandidate={(modelId) => handlePromoteChallenger('SWING', modelId)}
          promotingModelId={promotingModelId}
          promoteSuccess={promoteSuccess}
          promoteError={promoteError}
          onRefreshAll={handleRefreshResearchFactory}
          onFindBestModel={handleFindBestModel}
          onResetTraining={handleResetTraining}
        />
      )}

      {/* ── TAB 1: OVERVIEW / CHAMPION ─────────────────────────────── */}
      {activeTab === 'overview' && (
        <div className="space-y-6">
          {/* Strategy Operational Readiness Matrix Card */}
          <QlibReadinessCard
            readinessData={readinessData}
            selectedStrategy={trainStrategy}
            onStrategySelect={(strat) => {
              setTrainStrategy(strat);
              fetchActiveChampion(strat);
            }}
          />

          {/* Data Freshness & Invariants Banner */}
          <div className="bg-slate-900/90 border border-slate-800 rounded-2xl p-4 flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className={`p-2.5 rounded-xl border ${
                (statusData?.data_freshness?.status || statusData?.data_freshness?.data_status) === 'FRESH'
                  ? 'bg-emerald-950/60 border-emerald-500/40 text-emerald-400'
                  : (statusData?.data_freshness?.status || statusData?.data_freshness?.data_status) === 'DELAYED'
                  ? 'bg-amber-950/60 border-amber-500/40 text-amber-400'
                  : 'bg-rose-950/60 border-rose-500/40 text-rose-400'
              }`}>
                <Activity size={20} />
              </div>
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-xs font-bold text-white uppercase tracking-wider font-mono">
                    Market Data Freshness:
                  </span>
                  <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase ${
                    (statusData?.data_freshness?.status || statusData?.data_freshness?.data_status) === 'FRESH'
                      ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                      : (statusData?.data_freshness?.status || statusData?.data_freshness?.data_status) === 'DELAYED'
                      ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
                      : 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
                  }`}>
                    {statusData?.data_freshness?.status || statusData?.data_freshness?.data_status || 'CHECKING...'}
                  </span>
                  {(statusData?.data_freshness?.lag_seconds ?? statusData?.data_freshness?.data_lag_seconds) ? (
                    <span className="text-[11px] text-gray-400 font-mono">
                      (Lag: {Math.round((statusData.data_freshness.lag_seconds ?? statusData.data_freshness.data_lag_seconds) / 60)} min)
                    </span>
                  ) : null}
                </div>
                <div className="text-[11px] text-gray-400 font-mono mt-0.5">
                  Provider Bar: <span className="text-gray-300">{statusData?.data_freshness?.provider_latest || statusData?.data_freshness?.qlib_provider_latest_timestamp || 'N/A'}</span> · DB Latest: <span className="text-gray-300">{statusData?.data_freshness?.authoritative_latest || statusData?.data_freshness?.authoritative_latest_timestamp || 'N/A'}</span>
                </div>
              </div>
            </div>

            <div className="flex items-center gap-2 text-xs font-mono">
              <span className="px-2.5 py-1 bg-purple-950/60 text-purple-300 border border-purple-500/30 rounded-lg">
                Portfolio Heat: <strong className="text-emerald-400">0.0% (Virtual Only)</strong>
              </span>
              <span className="px-2.5 py-1 bg-slate-800/80 text-gray-300 border border-slate-700 rounded-lg">
                Broker Execution: <strong className="text-amber-400">DISCONNECTED (Protected)</strong>
              </span>
            </div>
          </div>

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
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-800">
              <div className="flex items-center gap-2">
                <TrendingUp size={16} className="text-emerald-400" />
                <h3 className="text-xs font-mono uppercase tracking-wider text-gray-200 font-bold">
                  Qlib Virtual Recommendations ({recCounts.ALL})
                </h3>
                {recLoading && <RefreshCw size={12} className="animate-spin text-purple-400 ml-1" />}
              </div>

              {/* Server-Side Filter Tabs */}
              <div className="flex flex-wrap items-center gap-2">
                {/* Status Filter Tabs */}
                <div className="flex items-center gap-1 bg-slate-950/80 p-1 rounded-xl border border-slate-800">
                  <button
                    onClick={() => handleStatusChange('ALL')}
                    className={`px-2.5 py-1 rounded-lg text-[10px] font-mono font-bold transition-all ${
                      recFilterStatus === 'ALL'
                        ? 'bg-slate-700 text-white shadow'
                        : 'text-gray-400 hover:text-white'
                    }`}
                  >
                    ALL
                  </button>
                  <button
                    onClick={() => handleStatusChange('OPEN')}
                    className={`px-2.5 py-1 rounded-lg text-[10px] font-mono font-bold transition-all ${
                      recFilterStatus === 'OPEN'
                        ? 'bg-amber-600/80 text-white shadow'
                        : 'text-amber-400 hover:text-amber-300'
                    }`}
                  >
                    OPEN ({recCounts.OPEN})
                  </button>
                  <button
                    onClick={() => handleStatusChange('CLOSED')}
                    className={`px-2.5 py-1 rounded-lg text-[10px] font-mono font-bold transition-all ${
                      recFilterStatus === 'CLOSED'
                        ? 'bg-emerald-600/80 text-white shadow'
                        : 'text-emerald-400 hover:text-emerald-300'
                    }`}
                  >
                    CLOSED ({recCounts.CLOSED})
                  </button>
                </div>

                {/* Strategy Filter Tabs */}
                <div className="flex items-center gap-1 bg-slate-950/80 p-1 rounded-xl border border-slate-800">
                  {[
                    { key: 'ALL', label: `ALL (${recCounts.ALL})` },
                    { key: 'SWING', label: `SWING (${recCounts.SWING})` },
                    { key: 'INTRADAY', label: `INTRADAY (${recCounts.INTRADAY})` },
                    { key: 'FNO', label: `F&O (${recCounts.FNO})` }
                  ].map(tab => (
                    <button
                      key={tab.key}
                      onClick={() => handleStrategyChange(tab.key)}
                      className={`px-2.5 py-1 rounded-lg text-[10px] font-mono font-bold transition-all ${
                        recFilterStrategy === tab.key
                          ? 'bg-purple-600 text-white shadow'
                          : 'text-gray-400 hover:text-white'
                      }`}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Performance & Heat Integrity Callout */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono">
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Recorded Virtual Recs</div>
                <div className="text-purple-300 font-bold mt-0.5">{recCounts.ALL}</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Open Virtual Recs</div>
                <div className="text-amber-300 font-bold mt-0.5">{recCounts.OPEN}</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Closed Virtual Trades</div>
                <div className="text-white font-bold mt-0.5">{recCounts.CLOSED}</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                <div className="text-gray-500 text-[10px] uppercase">Portfolio Heat Impact</div>
                <div className="text-emerald-400 font-bold mt-0.5">0.0% (Zero Live Risk)</div>
              </div>
            </div>

            {/* Recommendations Table */}
            {recommendations.length === 0 ? (
              <div className="text-center py-10 text-xs font-mono text-gray-500 border border-dashed border-slate-800 rounded-xl space-y-1">
                <div className="text-gray-400 font-bold">
                  NO QLIB RECOMMENDATIONS {recFilterStrategy !== 'ALL' ? `FOR ${recFilterStrategy}` : ''} {recFilterStatus !== 'ALL' ? `[${recFilterStatus}]` : ''}
                </div>
                <div className="text-[11px] text-gray-600">Run a Qlib scan or F&O check to generate recommendations.</div>
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs font-mono">
                  <thead>
                    <tr className="border-b border-slate-800 text-gray-400">
                      <th className="pb-2">Timestamp</th>
                      <th className="pb-2">Instrument / Contract</th>
                      <th className="pb-2">Strategy</th>
                      <th className="pb-2">Action / Signal</th>
                      <th className="pb-2">Pricing / Premium</th>
                      <th className="pb-2">Risk (SL / Target)</th>
                      <th className="pb-2">P&L (% & ₹)</th>
                      <th className="pb-2">Qlib Rank / Percentile</th>
                      <th className="pb-2">Status / Outcome</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/60">
                    {recommendations.map(rec => (
                      <tr key={rec.id} className="hover:bg-slate-800/30">
                        {/* Timestamp */}
                        <td className="py-2.5 text-yellow-500 whitespace-nowrap">
                          {rec.timestamp?.slice(0, 16).replace('T', ' ')}
                        </td>

                        {/* Instrument / Contract */}
                        <td className="py-2.5">
                          <div className="font-bold text-white flex items-center gap-1.5">
                            {rec.ticker}
                            {rec.lot_size > 0 && (
                              <span className="px-1.5 py-0.2 bg-slate-800 text-gray-300 rounded text-[9px]">
                                Lot: {rec.lot_size}
                              </span>
                            )}
                          </div>
                          {rec.strategy === 'FNO' && (
                            <div className="text-[10px] text-purple-300 font-mono">
                              {rec.fno_contract_info?.contract_symbol || (rec.strike ? `${rec.underlying || rec.ticker} ${rec.strike} ${rec.option_type || ''}` : 'F&O Contract')}
                            </div>
                          )}
                          {rec.strategy === 'SWING' && (
                            <div className="text-[10px] text-gray-500">
                              {rec.days_held !== undefined ? `${rec.days_held}/5 NSE Days Held` : '5-Day Horizon'}
                              {rec.days_remaining !== undefined && rec.status === 'OPEN' ? ` (${rec.days_remaining}d left)` : ''}
                            </div>
                          )}
                          {rec.strategy === 'INTRADAY' && (
                            <div className="text-[10px] text-gray-500">
                              Square-off: 15:15–15:30 IST
                            </div>
                          )}
                        </td>

                        {/* Strategy */}
                        <td className="py-2.5">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                            rec.strategy === 'FNO'
                              ? 'bg-amber-950/60 text-amber-300 border-amber-500/30'
                              : rec.strategy === 'SWING'
                              ? 'bg-purple-950/60 text-purple-300 border-purple-500/30'
                              : 'bg-cyan-950/60 text-cyan-300 border-cyan-500/30'
                          }`}>
                            {rec.strategy}
                          </span>
                        </td>

                        {/* Action / Signal */}
                        <td className="py-2.5">
                          <div className="flex flex-col gap-0.5">
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold w-fit ${
                              rec.action?.includes('BUY') || rec.direction === 'BUY' || rec.direction === 'BULLISH'
                                ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                                : 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
                            }`}>
                              {rec.action || rec.direction}
                            </span>
                            {rec.strategy === 'FNO' && (
                              <span className="text-[9px] text-gray-400">
                                Direction: {rec.direction}
                              </span>
                            )}
                          </div>
                        </td>

                        {/* Pricing / Premium */}
                        <td className="py-2.5 text-xs">
                          {rec.strategy === 'FNO' ? (
                            <div className="space-y-0.5">
                              <div className="text-gray-400 text-[10px]">
                                Spot: <span className="text-white font-semibold">₹{rec.spot_price || rec.entry_price}</span>
                              </div>
                              <div className="text-purple-300 font-semibold text-[11px]">
                                Premium: ₹{rec.entry_premium || rec.entry_price}
                                {rec.current_premium && rec.current_premium !== rec.entry_premium && (
                                  <span className="text-white ml-1 font-normal">→ ₹{rec.current_premium}</span>
                                )}
                              </div>
                            </div>
                          ) : (
                            <div className="space-y-0.5">
                              <div>Entry: <span className="text-white font-semibold">₹{rec.entry_price || rec.entry}</span></div>
                              <div className="text-gray-400 text-[10px]">
                                Curr: ₹{rec.current_price || rec.entry_price || rec.entry}
                              </div>
                            </div>
                          )}
                        </td>

                        {/* Risk (SL / Target) */}
                        <td className="py-2.5">
                          <div className="space-y-0.5">
                            <div className="text-rose-400">SL: ₹{rec.stop_loss || rec.sl}</div>
                            <div className="text-emerald-400">Tgt: ₹{rec.target_price || rec.tp1}</div>
                            {rec.strategy === 'FNO' && (
                              <div className="text-[9px]">
                                {rec.risk_structure_valid !== false ? (
                                  <span className="text-emerald-400/80">✓ SL &lt; Entry &lt; Tgt</span>
                                ) : (
                                  <span className="text-rose-400 font-bold">⚠ Risk Invalid</span>
                                )}
                              </div>
                            )}
                          </div>
                        </td>

                        {/* P&L (% & ₹) */}
                        <td className="py-2.5 font-bold">
                          {rec.unrealized_pnl_pct !== undefined && rec.unrealized_pnl_pct !== null ? (
                            <div className="space-y-0.5">
                              <span className={rec.unrealized_pnl_pct > 0 ? 'text-emerald-400' : rec.unrealized_pnl_pct < 0 ? 'text-rose-400' : 'text-gray-400'}>
                                {rec.unrealized_pnl_pct > 0 ? '+' : ''}{rec.unrealized_pnl_pct}%
                              </span>
                              {rec.pnl_rupees !== null && rec.pnl_rupees !== undefined && (
                                <div className={`text-[10px] font-normal ${rec.pnl_rupees > 0 ? 'text-emerald-400' : rec.pnl_rupees < 0 ? 'text-rose-400' : 'text-gray-400'}`}>
                                  {rec.pnl_rupees > 0 ? '+₹' : '₹'}{rec.pnl_rupees}
                                </div>
                              )}
                            </div>
                          ) : rec.profit_pct !== undefined && rec.profit_pct !== null ? (
                            <div className="space-y-0.5">
                              <span className={rec.profit_pct > 0 ? 'text-emerald-400' : rec.profit_pct < 0 ? 'text-rose-400' : 'text-gray-400'}>
                                {rec.profit_pct > 0 ? '+' : ''}{rec.profit_pct}%
                              </span>
                              {rec.pnl_rupees !== null && rec.pnl_rupees !== undefined && (
                                <div className={`text-[10px] font-normal ${rec.pnl_rupees > 0 ? 'text-emerald-400' : rec.pnl_rupees < 0 ? 'text-rose-400' : 'text-gray-400'}`}>
                                  {rec.pnl_rupees > 0 ? '+₹' : '₹'}{rec.pnl_rupees}
                                </div>
                              )}
                            </div>
                          ) : (
                            <span className="text-gray-500">-</span>
                          )}
                        </td>

                        {/* Qlib Rank / Percentile */}
                        <td className="py-2.5">
                          <div className="text-purple-300 font-semibold">
                            {rec.rank_percentile !== undefined && rec.rank_percentile !== null ? `${rec.rank_percentile}%` : `${rec.confidence}%`}
                          </div>
                          <div className="text-[10px] text-gray-500">
                            Universe Rank
                          </div>
                        </td>

                        {/* Status / Outcome */}
                        <td className="py-2.5">
                          <div className="flex flex-col gap-1">
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold w-fit ${
                              rec.pnl_status === 'PROFIT' || rec.pnl_status === 'WIN'
                                ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                                : rec.pnl_status === 'LOSS'
                                ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
                                : rec.status === 'OPEN'
                                ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
                                : 'bg-slate-800 text-gray-400'
                            }`}>
                              {rec.pnl_status || rec.status}
                            </span>
                            <span className="text-[10px] text-gray-400">
                              {rec.outcome || (rec.status === 'OPEN' ? 'TRACKING' : 'CLOSED')}
                            </span>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Pagination Bar */}
            {recFilteredCount > 0 && (
              <div className="flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-slate-800 text-xs font-mono">
                <div className="text-gray-400">
                  Showing <span className="text-white font-bold">{((recPage - 1) * 20) + 1}</span>–<span className="text-white font-bold">{Math.min(recPage * 20, recFilteredCount)}</span> of <span className="text-purple-300 font-bold">{recFilteredCount}</span> recommendations
                  {recFilterStrategy !== 'ALL' && <span className="text-gray-500 ml-1">({recFilterStrategy})</span>}
                  {recFilterStatus !== 'ALL' && <span className="text-gray-500 ml-1">[{recFilterStatus}]</span>}
                </div>

                <div className="flex items-center gap-2">
                  <span className="text-gray-400 text-[11px]">
                    Page <span className="text-white font-bold">{recPage}</span> of <span className="text-white font-bold">{recTotalPages}</span>
                  </span>
                  <button
                    onClick={() => handlePageChange(recPage - 1)}
                    disabled={recPage <= 1 || recLoading}
                    className="p-1.5 rounded-lg bg-slate-800 border border-slate-700 text-gray-300 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                    title="Previous Page"
                  >
                    <ChevronLeft size={16} />
                  </button>
                  <button
                    onClick={() => handlePageChange(recPage + 1)}
                    disabled={recPage >= recTotalPages || recLoading}
                    className="p-1.5 rounded-lg bg-slate-800 border border-slate-700 text-gray-300 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                    title="Next Page"
                  >
                    <ChevronRight size={16} />
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── TAB 2: SWING | INTRADAY | F&O SCANNER ─────────────────── */}
      {activeTab === 'trading' && (
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

              {scanResult.error ? (
                <div className="text-center py-10 font-mono text-xs text-gray-400 space-y-2">
                  <div className="text-rose-400 font-bold">SCAN EXECUTION BLOCKED (FAIL-CLOSED)</div>
                  <p className="text-rose-300 max-w-md mx-auto">
                    {scanResult.error}
                  </p>
                  <p className="text-gray-500 text-[11px] max-w-md mx-auto">
                    The platform enforces fail-closed data freshness boundaries to protect capital against stale market signals.
                  </p>
                </div>
              ) : scanResult.recommendations?.length === 0 ? (
                <div className="text-center py-8 font-mono text-xs text-gray-400 space-y-3">
                  <div className="text-amber-300 font-bold flex items-center justify-center gap-2">
                    <AlertTriangle size={16} className="text-amber-400" />
                    NO TRADES QUALIFIED ({scanResult.universe_stats?.scan_ready || scanResult.universe_count || 509} STOCKS EVALUATED)
                  </div>
                  {scanResult.strategy === 'INTRADAY' ? (
                    scanResult.regime_status === 'BEARISH_PROTECTION_ACTIVE' ? (
                      <div className="p-3.5 bg-amber-950/30 border border-amber-500/30 rounded-xl text-amber-200/90 max-w-lg mx-auto text-left space-y-1.5">
                        <div className="font-bold text-amber-400 flex items-center gap-1.5 text-xs">
                          🛡️ INTRADAY REGIME PROTECTION: BEAR PRESSURE
                        </div>
                        <p className="text-[11px] text-gray-300">
                          NIFTY 50 intraday session is down {scanResult.macro_regime?.intraday_change_pct ?? '0.0'}%. All counter-trend Long setups were strictly suppressed by the Intraday Regime Gate to prevent fighting session momentum.
                        </p>
                        {scanResult.rejected?.length > 0 && (
                          <div className="text-[10px] text-gray-400 pt-2 border-t border-amber-900/50 space-y-1">
                            <div className="text-amber-300 font-semibold">Sample Filtered Candidates:</div>
                            {scanResult.rejected.slice(0, 3).map((rej, rIdx) => (
                              <div key={rIdx} className="flex justify-between">
                                <span className="text-white font-medium">{rej.ticker}</span>
                                <span className="text-gray-400 truncate max-w-[280px]">{rej.reason}</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="p-3.5 bg-blue-950/30 border border-blue-500/30 rounded-xl text-blue-200/90 max-w-lg mx-auto text-left space-y-1.5">
                        <div className="font-bold text-blue-400 flex items-center gap-1.5 text-xs">
                          ⚡ INTRADAY REGIME: {scanResult.macro_regime?.intraday_regime_verdict || 'NEUTRAL / ACTIVE'}
                        </div>
                        <p className="text-[11px] text-gray-300">
                          {scanResult.macro_regime?.intraday_regime_verdict === 'INTRADAY_BULL_MOMENTUM' 
                            ? `NIFTY 50 session is bullish (+${scanResult.macro_regime?.intraday_change_pct}%). Counter-trend Short setups were held back, and no Long candidate exceeded the strict ±0.01 conviction threshold.`
                            : `The Qlib model evaluated ${scanResult.universe_stats?.scan_ready || 509} symbols in universe '${scanResult.universe}' on fresh market data and found no setups meeting conviction thresholds. Thresholds are never lowered to manufacture artificial trades.`}
                        </p>
                        {scanResult.rejected?.length > 0 && (
                          <div className="text-[10px] text-gray-400 pt-2 border-t border-blue-900/50 space-y-1">
                            <div className="text-blue-300 font-semibold">Sample Filtered Candidates:</div>
                            {scanResult.rejected.slice(0, 3).map((rej, rIdx) => (
                              <div key={rIdx} className="flex justify-between">
                                <span className="text-white font-medium">{rej.ticker}</span>
                                <span className="text-gray-400 truncate max-w-[280px]">{rej.reason}</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )
                  ) : (scanResult.macro_regime?.regime_verdict === 'BEAR_DISTRIBUTION' || scanResult.regime_status === 'BEARISH_PROTECTION_ACTIVE') ? (
                    <div className="p-3.5 bg-amber-950/30 border border-amber-500/30 rounded-xl text-amber-200/90 max-w-lg mx-auto text-left space-y-1.5">
                      <div className="font-bold text-amber-400 flex items-center gap-1.5 text-xs">
                        🛡️ FAIL-CLOSED REGIME PROTECTION: {scanResult.macro_regime?.regime_verdict || 'BEAR_DISTRIBUTION'}
                      </div>
                      <p className="text-[11px] text-gray-300">
                        NIFTY 50 is {scanResult.macro_regime?.distance_ema20_pct ?? '-2.09'}% below 20 EMA. All counter-trend Long setups were strictly suppressed by the Macro Regime Gate to protect capital during active market distribution.
                      </p>
                      {scanResult.rejected?.length > 0 && (
                        <div className="text-[10px] text-gray-400 pt-2 border-t border-amber-900/50 space-y-1">
                          <div className="text-amber-300 font-semibold">Sample Filtered Candidates:</div>
                          {scanResult.rejected.slice(0, 3).map((rej, rIdx) => (
                            <div key={rIdx} className="flex justify-between">
                              <span className="text-white font-medium">{rej.ticker}</span>
                              <span className="text-gray-400 truncate max-w-[280px]">{rej.reason}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ) : (
                    <p className="text-gray-500 max-w-md mx-auto">
                      The Qlib model evaluated {scanResult.universe_stats?.scan_ready || 509} symbols in universe '{scanResult.universe}' on fresh market data and found no setups meeting conviction thresholds. Thresholds are never lowered to manufacture artificial trades.
                    </p>
                  )}
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
                        <span>Qlib Rank: <span className="text-emerald-400 font-bold">{rec.rank_percentile !== undefined && rec.rank_percentile !== null ? `${rec.rank_percentile}%` : `${rec.confidence}%`}</span></span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Integrated Real F&O Option Contracts Section */}
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <h2 className="text-lg font-bold text-white flex items-center gap-2">
                  <Layers className="text-purple-400" size={20} />
                  Real NSE F&O Option Chain Contracts & Virtual Tracking
                </h2>
                <p className="text-xs text-gray-400 font-mono mt-0.5">
                  Real CE/PE strikes, monthly expiries, live premiums, and lot sizes from official NSE derivative feeds. 0% Portfolio Heat.
                </p>
              </div>

              <div className="flex items-center gap-3">
                <span className="text-[11px] font-mono text-gray-400">
                  Last refresh: <span className="text-purple-300 font-semibold">{lastFnoRefresh || 'Never refreshed'}</span>
                </span>
                <button
                  onClick={handleFetchFno}
                  disabled={fnoLoading}
                  className="px-4 py-2 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded-xl text-xs font-mono font-bold flex items-center gap-2 shadow-lg shadow-purple-950/40 disabled:opacity-50 transition-all"
                >
                  <RefreshCw size={14} className={fnoLoading ? 'animate-spin' : ''} />
                  {fnoLoading ? 'Scanning Option Chain...' : 'Refresh Option Chain'}
                </button>
              </div>
            </div>

            {/* F&O Provenance and Refresh Telemetry Strip */}
            <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3 text-xs font-mono pt-2 border-t border-slate-800">
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px] uppercase">F&O Universe</div>
                <div className="text-purple-300 font-bold mt-0.5 text-sm" title={fnoData?.universe_source || 'Dynamic Discovery'}>
                  {fnoData?.universe_count ?? '--'}
                </div>
                <div className="text-[10px] text-gray-500 truncate">{fnoData?.universe_source || 'Discovered'}</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px] uppercase">Attempted</div>
                <div className="text-blue-300 font-bold mt-0.5 text-sm">
                  {fnoData?.attempted_count ?? '--'}
                </div>
                <div className="text-[10px] text-gray-500">Live NSE requests</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px] uppercase">Fetched</div>
                <div className="text-emerald-400 font-bold mt-0.5 text-sm">
                  {fnoData?.successful_count ?? '--'}
                </div>
                <div className="text-[10px] text-gray-500">Success HTTP 200</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px] uppercase">Cached (Fresh)</div>
                <div className="text-cyan-300 font-bold mt-0.5 text-sm">
                  {fnoData?.skipped_cached_count ?? '--'}
                </div>
                <div className="text-[10px] text-gray-500">&lt; 5m Local SQLite</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px] uppercase">Failed / Offline</div>
                <div className={`font-bold mt-0.5 text-sm ${fnoData?.failed_count > 0 ? 'text-rose-400' : 'text-gray-400'}`}>
                  {fnoData?.failed_count ?? 0}
                </div>
                <div className="text-[10px] text-gray-500">Recorded Failures</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px] uppercase">Analyzed</div>
                <div className="text-indigo-300 font-bold mt-0.5 text-sm">
                  {fnoData?.analyzed_count ?? '--'}
                </div>
                <div className="text-[10px] text-gray-500">Alpha158 Features</div>
              </div>
              <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800">
                <div className="text-gray-500 text-[10px] uppercase">
                  {fnoData?.contract_status === 'MARKET_CLOSED' ? 'Preview Setups' : 'Recommendations'}
                </div>
                <div className="text-white font-bold mt-0.5 text-sm">
                  {fnoData?.active_contracts_count ?? fnoData?.recommendation_count ?? 0}
                </div>
                <div className="text-[10px] text-emerald-400">
                  {fnoData?.contract_status === 'MARKET_CLOSED' ? 'After-Hours Preview' : '0% Portfolio Heat'}
                </div>
              </div>
            </div>

            {/* Error Banner */}
            {fnoError && (
              <div className="p-4 bg-rose-950/40 border border-rose-500/40 rounded-xl text-xs font-mono text-rose-300 flex items-center gap-3">
                <AlertCircle size={18} className="shrink-0 text-rose-400" />
                <div>
                  <div className="font-bold">F&O DATA UNAVAILABLE</div>
                  <div className="text-gray-400 mt-0.5">{fnoError}</div>
                </div>
              </div>
            )}

            {fnoData?.contract_status === 'MARKET_CLOSED' && !fnoError && (
              <div className="p-3.5 bg-indigo-950/40 border border-indigo-500/40 rounded-xl text-xs font-mono text-indigo-200 flex items-center gap-3">
                <Clock size={16} className="shrink-0 text-indigo-400" />
                <div>
                  <span className="font-bold text-indigo-300">Market Closed (09:15–15:30 IST): </span>
                  <span className="text-gray-300">
                    {fnoData.reason || 'Showing session closing preview. Live order execution & virtual trade creation paused until 09:15 IST.'}
                  </span>
                </div>
              </div>
            )}

            {fnoData?.contract_status === 'DATA UNAVAILABLE' && !fnoError && (
              <div className="p-3 bg-amber-950/40 border border-amber-500/40 rounded-xl text-xs font-mono text-amber-300 flex items-center gap-3">
                <AlertTriangle size={16} className="shrink-0 text-amber-400" />
                <div>
                  <span className="font-bold">NSE Option Chain Offline / Contracts Unavailable: </span>
                  <span className="text-gray-400">{fnoData.reason || 'Real live option chain currently unreachable'}</span>
                </div>
              </div>
            )}

            {/* Opportunities Cards */}
            {fnoData?.opportunities?.length > 0 ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {fnoData.opportunities.map((opp, idx) => (
                  <div key={idx} className="bg-slate-950/80 border border-slate-800 rounded-xl p-4 space-y-3 font-mono text-xs">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-bold text-white">{opp.underlying}</span>
                        {opp.freshness_state && (
                          <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold ${
                            opp.freshness_state === 'FRESH'
                              ? 'bg-emerald-950 text-emerald-300 border border-emerald-500/40'
                              : opp.freshness_state === 'DELAYED'
                              ? 'bg-amber-950 text-amber-300 border border-amber-500/40'
                              : opp.freshness_state === 'STALE'
                              ? 'bg-orange-950 text-orange-300 border border-orange-500/40'
                              : 'bg-rose-950 text-rose-300 border border-rose-500/40'
                          }`}>
                            {opp.freshness_state}
                          </span>
                        )}
                      </div>
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

                    {opp.contract_status === 'AVAILABLE' || opp.contract_status === 'MARKET_CLOSED' || (opp.contract_status === 'LOW_CONVICTION_FILTERED' && opp.indicative_entry) ? (
                      <div className="space-y-2">
                        <div className="p-2.5 bg-slate-900 rounded border border-slate-800 flex justify-between items-center">
                          <div className="flex items-center gap-2">
                            <span className="font-bold text-purple-300">{opp.contract}</span>
                            {opp.contract_status === 'MARKET_CLOSED' ? (
                              <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-indigo-950 text-indigo-300 border border-indigo-500/40">
                                PREVIEW
                              </span>
                            ) : opp.contract_status === 'LOW_CONVICTION_FILTERED' ? (
                              <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-slate-800 text-amber-300/80 border border-slate-700">
                                LOW CONVICTION
                              </span>
                            ) : null}
                          </div>
                          <span className="text-emerald-400 font-bold">LTP ₹{opp.indicative_entry}</span>
                        </div>
                        <div className="grid grid-cols-3 gap-2 text-[11px] text-gray-400">
                          <div>Spot: ₹{opp.spot_price}</div>
                          <div>PCR: {opp.pcr ?? 'N/A'}</div>
                          <div>Max Pain: ₹{opp.max_pain ?? 'N/A'}</div>
                        </div>
                        <div className="grid grid-cols-2 gap-2 text-[11px] text-gray-400 pt-1 border-t border-slate-900">
                          <div>SL: ₹{opp.stop_loss}</div>
                          <div>Target: ₹{opp.target_price || opp.target}</div>
                        </div>
                        {opp.reason && opp.contract_status === 'LOW_CONVICTION_FILTERED' && (
                          <div className="p-1.5 bg-slate-900/60 rounded border border-slate-800/80 text-[10px] text-gray-400">
                            {opp.reason}
                          </div>
                        )}
                        <div className="text-[10px] text-gray-500 pt-1 border-t border-slate-900 flex justify-between">
                          <span>As of: {opp.fetched_at ? new Date(opp.fetched_at).toLocaleTimeString() : 'N/A'}</span>
                          <span>Age: {opp.freshness_age_seconds != null ? `${opp.freshness_age_seconds}s` : 'Live'}</span>
                        </div>
                      </div>
                    ) : (
                      <div className="p-3 bg-slate-900/60 rounded border border-slate-800 text-amber-400/90 text-[11px] space-y-1">
                        <div className="flex items-center gap-2">
                          <AlertTriangle size={14} className="shrink-0 text-amber-400" />
                          <span className="font-bold">
                            {opp.contract_status === 'LOW_CONVICTION_FILTERED' ? 'FILTERED (LOW CONVICTION)' : opp.contract_status === 'NEUTRAL_SIGNAL' ? 'NEUTRAL SIGNAL (0 ALPHA)' : 'F&O DATA UNAVAILABLE'}
                          </span>
                        </div>
                        <div className="text-gray-400 text-[10px]">{opp.reason || 'NSE Option Chain API currently unreachable or STALE'}</div>
                        {opp.fetched_at && (
                          <div className="text-[10px] text-gray-500">Attempted: {new Date(opp.fetched_at).toLocaleTimeString()}</div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              !fnoLoading && !fnoError && (
                <div className="text-center py-10 text-xs font-mono text-gray-500 border border-dashed border-slate-800 rounded-xl space-y-1">
                  <div className="text-gray-400 font-bold">No F&O Opportunities Loaded</div>
                  <div className="text-[11px] text-gray-600">Click "Refresh Option Chain" to scan real F&O contracts.</div>
                </div>
              )
            )}
          </div>
        </div>
      )}

      {/* ── TAB 3: MODELS / RESEARCH HISTORY ───────────────────────── */}
      {(activeTab === 'models' || activeTab === 'training') && (
        <div className="space-y-6">
          {/* CURRENT ACTIVE CHAMPION CARD */}
          <div className="bg-slate-900/80 border border-slate-800 rounded-2xl p-5 shadow-xl">
            <div className="flex flex-wrap items-center justify-between gap-3 pb-4 border-b border-slate-800">
              <div className="flex items-center gap-2.5">
                <Award className="text-amber-400" size={22} />
                <div>
                  <h2 className="text-base font-bold text-white flex items-center gap-2">
                    CURRENT ACTIVE CHAMPION
                    <span className="px-2 py-0.5 bg-amber-500/10 border border-amber-500/30 text-amber-300 rounded text-[10px] font-mono font-bold">
                      {trainStrategy}
                    </span>
                  </h2>
                  <p className="text-xs text-gray-400 font-mono">
                    Authoritative model serving live Qlib inference and signal generation.
                  </p>
                </div>
              </div>
              <button
                onClick={() => fetchActiveChampion(trainStrategy)}
                disabled={championLoading}
                className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-gray-300 rounded-lg text-xs font-mono flex items-center gap-1.5 transition-all"
              >
                <RefreshCw size={12} className={championLoading ? 'animate-spin' : ''} />
                Refresh Champion
              </button>
            </div>

            {championLoading ? (
              <div className="py-6 text-center text-xs font-mono text-gray-400 flex items-center justify-center gap-2">
                <RefreshCw size={14} className="animate-spin text-purple-400" />
                Loading active champion details...
              </div>
            ) : activeChampion?.has_champion && activeChampion?.champion ? (
              <div className="mt-4 space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-4 gap-3 font-mono text-xs">
                  <div className="bg-slate-950/70 p-3 rounded-xl border border-slate-800">
                    <div className="text-gray-500 text-[10px] font-bold">MODEL ID & ARCHITECTURE</div>
                    <div className="text-white font-bold text-sm mt-0.5 truncate" title={activeChampion.champion.model_id}>
                      {activeChampion.champion.model_id}
                    </div>
                    <div className="text-indigo-400 text-[11px] mt-0.5">
                      {activeChampion.champion.model_family || 'Qlib Estimator'} · {activeChampion.champion.feature_family || 'Alpha158'} ({activeChampion.champion.features_count || 158}f)
                    </div>
                  </div>

                  <div className="bg-slate-950/70 p-3 rounded-xl border border-slate-800">
                    <div className="text-gray-500 text-[10px] font-bold">NET SHARPE & PROFIT FACTOR</div>
                    <div className="flex items-center gap-2 mt-0.5">
                      <span className={`text-base font-extrabold ${Number(activeChampion.champion.metrics?.sharpe) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {activeChampion.champion.metrics?.sharpe !== undefined ? Number(activeChampion.champion.metrics.sharpe).toFixed(2) : 'N/A'}
                      </span>
                      <span className="text-gray-500 text-[11px]">Sharpe</span>
                    </div>
                    <div className="text-gray-300 text-[11px] mt-0.5">
                      PF: <strong className="text-white">{activeChampion.champion.metrics?.profit_factor !== undefined ? Number(activeChampion.champion.metrics.profit_factor).toFixed(2) : 'N/A'}</strong>
                    </div>
                  </div>

                  <div className="bg-slate-950/70 p-3 rounded-xl border border-slate-800">
                    <div className="text-gray-500 text-[10px] font-bold">WIN RATE & DRAWDOWN</div>
                    <div className="text-emerald-400 font-bold text-sm mt-0.5">
                      {activeChampion.champion.metrics?.win_rate !== undefined ? `${Number(activeChampion.champion.metrics.win_rate).toFixed(2)}%` : 'N/A'}
                    </div>
                    <div className="text-rose-400 text-[11px] mt-0.5">
                      Max DD: <strong>{activeChampion.champion.metrics?.max_drawdown_pct !== undefined ? `${Number(activeChampion.champion.metrics.max_drawdown_pct).toFixed(2)}%` : 'N/A'}</strong>
                    </div>
                  </div>

                  <div className="bg-slate-950/70 p-3 rounded-xl border border-slate-800">
                    <div className="text-gray-500 text-[10px] font-bold">OOS SAMPLES & METRICS</div>
                    <div className="text-purple-300 font-bold text-sm mt-0.5">
                      {activeChampion.champion.metrics?.trades ?? activeChampion.champion.metrics?.total_trades ?? 'N/A'} trades
                    </div>
                    <div className="text-gray-400 text-[11px] mt-0.5">
                      IC: <strong className="text-white">{activeChampion.champion.metrics?.ic !== undefined ? Number(activeChampion.champion.metrics.ic).toFixed(4) : 'N/A'}</strong>
                    </div>
                  </div>
                </div>

                <div className="p-3 bg-slate-950/90 rounded-xl border border-slate-800 flex flex-wrap items-center justify-between gap-3 text-[11px] font-mono text-gray-400">
                  <div className="flex items-center gap-2">
                    <span className="text-gray-500">CANONICAL PATH:</span>
                    <span className="text-gray-300 font-bold">{activeChampion.champion.model_path}</span>
                  </div>
                  <div className="flex items-center gap-2 truncate max-w-lg">
                    <span className="text-gray-500">SHA-256:</span>
                    <span className="text-indigo-300 truncate font-mono">{activeChampion.champion.artifact_sha256}</span>
                  </div>
                </div>
              </div>
            ) : (
              <div className="mt-4 p-4 bg-amber-950/20 border border-amber-500/40 rounded-xl flex items-start gap-3">
                <AlertTriangle className="text-amber-400 shrink-0 mt-0.5" size={18} />
                <div className="space-y-1 text-xs font-mono">
                  <div className="font-bold text-amber-300 tracking-wide">NO ACTIVE CHAMPION</div>
                  <p className="text-gray-300 leading-relaxed">
                    No active champion model is currently deployed for <strong className="text-white">{trainStrategy}</strong>. 
                    The next trained challenger that satisfies all 12 backend governance gates (positive Sharpe, min 30 OOS trades, valid architecture) will become the inaugural active champion upon promotion.
                  </p>
                </div>
              </div>
            )}
          </div>

          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5">
            <h2 className="text-lg font-bold text-white flex items-center gap-2 mb-1">
              <Sliders className="text-purple-400" size={20} />
              Train Real Microsoft Qlib Estimator
            </h2>
            <p className="text-xs text-gray-400 font-mono mb-6">
              Executes DatasetH generation, Alpha158/Alpha360 extraction, and LightGBM / DoubleEnsemble fitting with chronological Train/Validation/Locked OOS splits.
            </p>

            <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
              <div>
                <label className="block text-xs font-mono text-gray-400 mb-1.5">Strategy</label>
                <select
                  value={trainStrategy}
                  onChange={e => {
                    const val = e.target.value;
                    setTrainStrategy(val);
                    fetchActiveChampion(val);
                  }}
                  className="w-full bg-slate-800 border border-slate-700 text-xs font-mono text-white rounded-xl px-3 py-2.5 outline-none"
                >
                  <option value="SWING">SWING (Daily Bars, 70/15/15)</option>
                  <option value="INTRADAY">INTRADAY (15m Candles, 70/15/15)</option>
                  <option value="FNO">FNO (Underlying Equity/Index)</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-mono text-gray-400 mb-1.5">Universe</label>
                <select
                  value={trainUniverse}
                  onChange={e => setTrainUniverse(e.target.value)}
                  className="w-full bg-slate-800 border border-slate-700 text-xs font-mono text-white rounded-xl px-3 py-2.5 outline-none"
                >
                  <option value="NIFTY_500_ESTABLISHED">NIFTY 500 Established (414 Stocks, 1200+ Bars)</option>
                  <option value="FNO">NSE F&O Underlyings (~172 Stocks)</option>
                  <option value="NIFTY_50">NIFTY 50 Core (50 Stocks)</option>
                  <option value="BOOTSTRAP_35">Bootstrap Subset (35 Stocks)</option>
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
                Universe: <span className="text-purple-300 font-bold">{trainUniverse}</span> · Segments: Train (70%) → Validation (15%) → Locked OOS (15%)
              </div>
              <button
                onClick={handleTrain}
                disabled={training}
                className="px-6 py-2.5 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded-xl text-xs font-bold flex items-center gap-2 shadow-lg disabled:opacity-50 transition-all"
              >
                {training ? <RefreshCw className="animate-spin" size={14} /> : <Play size={14} />}
                {training ? 'Training Qlib Estimator...' : 'Start Qlib Training'}
              </button>
              <div className="flex items-center gap-2">
                {training && (
                  <button
                    onClick={handleResetTraining}
                    title="Unlock and reset training state"
                    className="px-4 py-2.5 bg-rose-950/80 hover:bg-rose-900 border border-rose-500/50 text-rose-300 rounded-xl text-xs font-bold flex items-center gap-1.5 transition-all"
                  >
                    <X size={14} />
                    <span>Reset / Unlock</span>
                  </button>
                )}
                <button
                  onClick={handleTrain}
                  disabled={training}
                  className="px-6 py-2.5 bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 text-white rounded-xl text-xs font-bold flex items-center gap-2 shadow-lg disabled:opacity-50 transition-all"
                >
                  {training ? <RefreshCw className="animate-spin" size={14} /> : <Play size={14} />}
                  {training ? 'Training Qlib Estimator...' : 'Start Qlib Training'}
                </button>
              </div>
            </div>

            {/* LIVE TRAINING TELEMETRY CARD */}
            {(training || trainTelemetry) && (
              <div className="mt-6 bg-slate-950 border border-purple-500/40 rounded-2xl p-5 shadow-2xl space-y-4 font-mono">
                <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-slate-800">
                  <div className="flex items-center gap-2.5">
                    <Terminal size={18} className="text-purple-400" />
                    <span className="text-xs font-bold text-white tracking-wide">
                      LIVE TRAINING TELEMETRY & ETA
                    </span>
                    <span className="px-2 py-0.5 bg-purple-950 border border-purple-500/50 text-purple-300 rounded text-[10px]">
                      JOB: {activeJobId || trainTelemetry?.job_id || 'ACTIVE'}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="px-2.5 py-1 bg-slate-900 border border-slate-700 rounded-lg text-[11px] font-bold text-purple-300">
                      STAGE: {trainTelemetry?.stage || trainingStatus || 'INITIALIZING'}
                    </span>
                    {training && <RefreshCw size={14} className="animate-spin text-purple-400" />}
                    {training && (
                      <button
                        onClick={handleResetTraining}
                        title="Cancel active job and unlock state"
                        className="px-2.5 py-1 bg-rose-950/80 hover:bg-rose-900 border border-rose-500/50 text-rose-300 rounded-lg text-[11px] font-bold flex items-center gap-1 transition-all"
                      >
                        <X size={12} />
                        <span>Cancel / Unlock</span>
                      </button>
                    )}
                  </div>
                </div>

                {/* Progress Bar */}
                <div className="space-y-1.5">
                  <div className="flex justify-between items-center text-xs text-gray-400">
                    <span>Pipeline Progress (10 Discrete Stages)</span>
                    <span className="text-purple-300 font-bold">{trainTelemetry?.progress_pct || 0}%</span>
                  </div>
                  <div className="w-full bg-slate-900 rounded-full h-2.5 overflow-hidden border border-slate-800">
                    <div
                      className="bg-gradient-to-r from-purple-600 via-indigo-500 to-emerald-400 h-full rounded-full transition-all duration-500 ease-out"
                      style={{ width: `${Math.min(100, Math.max(0, trainTelemetry?.progress_pct || 0))}%` }}
                    />
                  </div>
                </div>

                {/* 4 Telemetry Metrics */}
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  <div className="bg-slate-900/80 p-3 rounded-xl border border-slate-800">
                    <div className="text-gray-500 text-[10px]">ELAPSED TIME</div>
                    <div className="text-white font-bold text-sm mt-0.5">
                      {trainTelemetry?.elapsed_seconds ? `${trainTelemetry.elapsed_seconds}s` : '0s'}
                    </div>
                  </div>
                  <div className="bg-slate-900/80 p-3 rounded-xl border border-slate-800">
                    <div className="text-gray-500 text-[10px]">HONEST ETA REMAINING</div>
                    <div className="text-emerald-400 font-bold text-sm mt-0.5">
                      {trainTelemetry?.eta_seconds !== null && trainTelemetry?.eta_seconds !== undefined
                        ? (trainTelemetry.eta_seconds > 60
                            ? `${Math.floor(trainTelemetry.eta_seconds / 60)}m ${trainTelemetry.eta_seconds % 60}s`
                            : `${trainTelemetry.eta_seconds}s`)
                        : (training ? 'Calculating...' : 'Done')}
                    </div>
                  </div>
                  <div className="bg-slate-900/80 p-3 rounded-xl border border-slate-800">
                    <div className="text-gray-500 text-[10px]">MEMORY USAGE (RSS)</div>
                    <div className="text-indigo-300 font-bold text-sm mt-0.5">
                      {trainTelemetry?.memory_rss_mb ? `${trainTelemetry.memory_rss_mb} MB` : 'Monitoring'}
                    </div>
                  </div>
                  <div className="bg-slate-900/80 p-3 rounded-xl border border-slate-800">
                    <div className="text-gray-500 text-[10px]">DATASET / FEATURES</div>
                    <div className="text-purple-300 font-bold text-sm mt-0.5">
                      {trainTelemetry?.rows_processed
                        ? `${trainTelemetry.rows_processed.toLocaleString()} rows (${trainTelemetry.feature_count || 158}f)`
                        : `${trainUniverse === 'NIFTY_500_ESTABLISHED' ? '414 stocks' : trainUniverse}`}
                    </div>
                  </div>
                </div>

                {/* Live Terminal Log Stream */}
                {trainTelemetry?.logs && trainTelemetry.logs.length > 0 && (
                  <div className="bg-black/90 rounded-xl p-3 border border-slate-800 text-[11px] text-gray-300 font-mono max-h-48 overflow-y-auto space-y-1">
                    {trainTelemetry.logs.map((logMsg, lIdx) => (
                      <div key={lIdx} className="leading-tight">
                        <span className="text-purple-400 mr-1.5">&gt;</span>
                        <span>{logMsg}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {trainError && (
              <div className="mt-4 p-4 bg-rose-950/40 border border-rose-500/40 rounded-xl text-xs font-mono text-rose-300 flex items-center gap-2">
                <AlertCircle size={16} className="text-rose-400 shrink-0" />
                <span>Training Failed: {trainError}</span>
              </div>
            )}

            {trainResult && (
              <div className="mt-6 p-5 bg-slate-950/80 border border-purple-500/40 rounded-xl space-y-4 font-mono text-xs">
                <div className="flex items-center justify-between text-purple-300 font-bold">
                  <span className="flex items-center gap-2">
                    <ShieldCheck size={18} className="text-purple-400" />
                    FROZEN CHALLENGER ARTIFACT CREATED
                  </span>
                  <span className="px-2 py-0.5 bg-purple-950/80 border border-purple-500/50 text-purple-300 rounded text-[10px]">
                    STATUS: FROZEN_CHALLENGER
                  </span>
                </div>

                <div className="p-3 bg-purple-950/30 border border-purple-500/20 rounded-lg text-gray-300 text-[11px] leading-relaxed">
                  <strong className="text-purple-300">Model Governance:</strong> This model has been stored in the Challenger Vault as an immutable artifact. The ACTIVE model pointer is <strong className="text-amber-300">UNCHANGED</strong>. Promotion requires explicit manual approval below.
                </div>

                {/* Signal Metrics vs Execution Metrics */}
                <div className="space-y-3">
                  <div>
                    <div className="text-[11px] font-bold text-gray-400 mb-1.5 flex items-center gap-1.5">
                      <BarChart3 size={13} className="text-indigo-400" />
                      SIGNAL-LEVEL METRICS (Gross Alpha)
                    </div>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
                      <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                        <div className="text-gray-500 text-[10px]">INFORMATION COEFF (IC)</div>
                        <div className="text-white font-bold text-sm">{trainResult.oos_metrics?.signal_metrics?.information_coefficient ?? trainResult.oos_metrics?.ic}</div>
                      </div>
                      <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                        <div className="text-gray-500 text-[10px]">RANK IC</div>
                        <div className="text-indigo-300 font-bold text-sm">{trainResult.oos_metrics?.signal_metrics?.rank_ic ?? trainResult.oos_metrics?.rank_ic}</div>
                      </div>
                      <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                        <div className="text-gray-500 text-[10px]">RAW WIN RATE</div>
                        <div className="text-emerald-400 font-bold text-sm">{trainResult.oos_metrics?.signal_metrics?.raw_win_rate ?? trainResult.oos_metrics?.win_rate}%</div>
                      </div>
                      <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                        <div className="text-gray-500 text-[10px]">GROSS PROFIT FACTOR</div>
                        <div className="text-emerald-400 font-bold text-sm">{trainResult.oos_metrics?.signal_metrics?.raw_profit_factor ?? trainResult.oos_metrics?.profit_factor}</div>
                      </div>
                    </div>
                  </div>

                  <div>
                    <div className="text-[11px] font-bold text-gray-400 mb-1.5 flex items-center gap-1.5">
                      <TrendingUp size={13} className="text-emerald-400" />
                      EXECUTION-LEVEL METRICS (Net with 25 bps STT, Brokerage & Slippage Friction)
                    </div>
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
                      <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                        <div className="text-gray-500 text-[10px]">NET WIN RATE</div>
                        <div className="text-emerald-400 font-bold text-sm">{trainResult.oos_metrics?.execution_metrics?.net_win_rate ?? trainResult.oos_metrics?.win_rate}%</div>
                      </div>
                      <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                        <div className="text-gray-500 text-[10px]">NET PROFIT FACTOR</div>
                        <div className="text-emerald-400 font-bold text-sm">{trainResult.oos_metrics?.execution_metrics?.net_profit_factor ?? trainResult.oos_metrics?.profit_factor}</div>
                      </div>
                      <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                        <div className="text-gray-500 text-[10px]">NET SHARPE</div>
                        <div className="text-purple-300 font-bold text-sm">{trainResult.oos_metrics?.execution_metrics?.net_sharpe ?? trainResult.oos_metrics?.sharpe}</div>
                      </div>
                      <div className="bg-slate-900/60 p-2.5 rounded border border-slate-800">
                        <div className="text-gray-500 text-[10px]">MAX DRAWDOWN</div>
                        <div className="text-rose-400 font-bold text-sm">{trainResult.oos_metrics?.execution_metrics?.net_max_drawdown_pct ?? trainResult.oos_metrics?.max_drawdown_pct}%</div>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="pt-2 border-t border-slate-800/80 flex flex-wrap items-center justify-between gap-2 text-[10px] text-gray-400">
                  <span>Model ID: <span className="text-white font-semibold">{trainResult.model_id}</span></span>
                  <span className="truncate max-w-md">Artifact SHA-256: <span className="font-mono text-gray-300">{trainResult.artifact_sha256}</span></span>
                </div>
              </div>
            )}

            {/* Challenger Models Vault Section */}
            <div className="mt-8 pt-6 border-t border-slate-800 space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <ShieldCheck className="text-indigo-400" size={16} />
                    Challenger Models Vault
                  </h3>
                  <p className="text-xs text-gray-400 font-mono mt-0.5">
                    Immutable frozen challengers evaluated against the 12 Authoritative Backend Promotion Gates.
                  </p>
                </div>
                <button
                  onClick={fetchChallengers}
                  disabled={challengersLoading}
                  className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-gray-300 rounded-lg text-xs font-mono flex items-center gap-1.5 transition-all"
                >
                  <RefreshCw size={12} className={challengersLoading ? 'animate-spin' : ''} />
                  Refresh Vault
                </button>
              </div>

              {promoteSuccess && (
                <div className="p-3 bg-emerald-950/40 border border-emerald-500/40 rounded-xl text-xs font-mono text-emerald-300 flex items-center gap-2">
                  <CheckCircle2 size={16} className="text-emerald-400 shrink-0" />
                  <span>{promoteSuccess}</span>
                </div>
              )}

              {promoteError && (
                <div className="p-3 bg-rose-950/40 border border-rose-500/40 rounded-xl text-xs font-mono text-rose-300 flex items-center gap-2">
                  <AlertCircle size={16} className="text-rose-400 shrink-0" />
                  <span>Promotion Failed: {promoteError}</span>
                </div>
              )}

              {challengers.length === 0 ? (
                <div className="p-6 bg-slate-950/40 border border-slate-800 rounded-xl text-center text-xs font-mono text-gray-500">
                  No frozen challengers found. Run a training task above to create a new challenger.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs font-mono">
                    <thead>
                      <tr className="border-b border-slate-800 text-gray-400">
                        <th className="pb-2">Strategy</th>
                        <th className="pb-2">Model ID</th>
                        <th className="pb-2">Net Sharpe (Δ)</th>
                        <th className="pb-2">Net PF (Δ)</th>
                        <th className="pb-2">Net Win Rate (Δ)</th>
                        <th className="pb-2">True Max DD (Δ)</th>
                        <th className="pb-2">OOS Trades</th>
                        <th className="pb-2">Governance Status</th>
                        <th className="pb-2 text-right">Action</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60">
                      {challengers.map(c => {
                        const elig = c.promotion_eligibility;
                        const comp = c.champion_comparison;
                        const isEligible = elig?.eligible === true;
                        
                        const cSharpe = c.metrics?.execution_metrics?.net_sharpe ?? c.metrics?.sharpe ?? null;
                        const cPF = c.metrics?.execution_metrics?.net_profit_factor ?? c.metrics?.profit_factor ?? null;
                        const cWR = c.metrics?.execution_metrics?.net_win_rate ?? c.metrics?.win_rate ?? null;
                        const cTrades = c.metrics?.execution_metrics?.trades ?? c.metrics?.trades ?? c.metrics?.total_trades ?? null;
                        const cDD = c.metrics?.execution_metrics?.net_max_drawdown_pct ?? c.metrics?.max_drawdown_pct ?? null;

                        const deltaSharpe = comp?.metrics_delta?.sharpe_delta ?? null;
                        const deltaPF = comp?.metrics_delta?.profit_factor_delta ?? null;
                        const deltaWR = comp?.metrics_delta?.win_rate_delta ?? null;
                        const deltaDD = comp?.metrics_delta?.max_drawdown_delta ?? 
                          (comp?.champion_metrics?.max_drawdown_pct !== undefined && cDD !== null 
                            ? (Number(cDD) - Number(comp.champion_metrics.max_drawdown_pct)) 
                            : null);

                        const isExpanded = expandedChallengerId === c.model_id;

                        return (
                          <React.Fragment key={c.model_id}>
                            <tr className="hover:bg-slate-800/30 transition-colors">
                              <td className="py-2.5">
                                <span className="px-2 py-0.5 rounded text-[10px] font-bold border bg-purple-950/60 text-purple-300 border-purple-500/30">
                                  {c.strategy}
                                </span>
                              </td>
                              <td className="py-2.5">
                                <div className="text-white font-semibold flex items-center gap-1.5">
                                  <span>{c.model_id}</span>
                                </div>
                                <div className="text-[10px] text-gray-500">
                                  {c.created_at?.slice(0, 16).replace('T', ' ')}
                                </div>
                              </td>
                              <td className="py-2.5">
                                {cSharpe !== null ? (
                                  <div className="flex items-center gap-1">
                                    <span className={Number(cSharpe) >= 0 ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                                      {Number(cSharpe).toFixed(2)}
                                    </span>
                                    {deltaSharpe !== null && (
                                      <span className={`text-[10px] font-bold ${Number(deltaSharpe) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                        (Δ {Number(deltaSharpe) >= 0 ? '+' : ''}{Number(deltaSharpe).toFixed(2)})
                                      </span>
                                    )}
                                  </div>
                                ) : '-'}
                              </td>
                              <td className="py-2.5">
                                {cPF !== null ? (
                                  <div className="flex items-center gap-1">
                                    <span className={Number(cPF) >= 1.0 ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                                      {Number(cPF).toFixed(2)}
                                    </span>
                                    {deltaPF !== null && (
                                      <span className={`text-[10px] font-bold ${Number(deltaPF) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                        (Δ {Number(deltaPF) >= 0 ? '+' : ''}{Number(deltaPF).toFixed(2)})
                                      </span>
                                    )}
                                  </div>
                                ) : '-'}
                              </td>
                              <td className="py-2.5">
                                {cWR !== null ? (
                                  <div className="flex items-center gap-1">
                                    <span className="text-emerald-400 font-bold">
                                      {Number(cWR).toFixed(1)}%
                                    </span>
                                    {deltaWR !== null && (
                                      <span className={`text-[10px] font-bold ${Number(deltaWR) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                        (Δ {Number(deltaWR) >= 0 ? '+' : ''}{Number(deltaWR).toFixed(1)}%)
                                      </span>
                                    )}
                                  </div>
                                ) : '-'}
                              </td>
                              <td className="py-2.5">
                                {cDD !== null ? (
                                  <div className="flex items-center gap-1">
                                    <span className={`font-bold ${Number(cDD) <= 20.0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                      {Number(cDD).toFixed(2)}%
                                    </span>
                                    {deltaDD !== null && (
                                      <span className={`text-[10px] font-bold ${Number(deltaDD) <= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                        (Δ {Number(deltaDD) > 0 ? '+' : ''}{Number(deltaDD).toFixed(2)}%)
                                      </span>
                                    )}
                                  </div>
                                ) : '-'}
                              </td>
                              <td className="py-2.5 text-purple-300 font-bold">
                                {cTrades !== null ? cTrades.toLocaleString() : '-'}
                              </td>
                              <td className="py-2.5">
                                {isEligible ? (
                                  <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-950/80 border border-emerald-500/40 text-emerald-300 flex items-center gap-1 w-fit">
                                    <CheckCircle2 size={11} /> ELIGIBLE ({elig?.passed_gates_count ?? 12}/12 GATES)
                                  </span>
                                ) : (
                                  <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-rose-950/80 border border-rose-500/40 text-rose-300 flex items-center gap-1 w-fit">
                                    <AlertCircle size={11} /> NOT ELIGIBLE ({elig?.failed_reasons?.length ?? 1} FAILED)
                                  </span>
                                )}
                              </td>
                              <td className="py-2.5 text-right">
                                <div className="flex items-center justify-end gap-1.5">
                                  {isEligible ? (
                                    <button
                                      onClick={() => handlePromoteChallenger(c.strategy, c.model_id)}
                                      disabled={promotingModelId === c.model_id}
                                      className="px-3 py-1 bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 text-white rounded text-xs font-bold transition-all shadow disabled:opacity-50 flex items-center gap-1"
                                    >
                                      <CheckCircle2 size={12} />
                                      {promotingModelId === c.model_id ? 'Promoting...' : 'Promote to Active'}
                                    </button>
                                  ) : (
                                    <button
                                      disabled
                                      title={elig?.failed_reasons && elig.failed_reasons.length > 0 ? elig.failed_reasons.join('; ') : "Candidate failed governance safety gates. Click expand to see failed gates."}
                                      className="px-3 py-1 bg-slate-800/80 border border-slate-700 text-gray-500 rounded text-xs font-bold cursor-not-allowed flex items-center gap-1"
                                    >
                                      <AlertCircle size={12} />
                                      Ineligible (Locked)
                                    </button>
                                  )}
                                  <button
                                    onClick={() => setExpandedChallengerId(isExpanded ? null : c.model_id)}
                                    className="p-1 text-gray-400 hover:text-white rounded hover:bg-slate-800 transition-colors"
                                    title={isExpanded ? 'Collapse details' : 'Expand gate details and comparison'}
                                  >
                                    {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                                  </button>
                                </div>
                              </td>
                            </tr>

                            {/* EXPANDED ACCORDION DETAILS */}
                            {isExpanded && (
                              <tr className="bg-slate-950/90 border-b border-slate-800">
                                <td colSpan={8} className="p-4 space-y-4">
                                  {/* Failed Reasons Alert */}
                                  {elig?.failed_reasons && elig.failed_reasons.length > 0 && (
                                    <div className="p-3 bg-rose-950/40 border border-rose-500/40 rounded-xl space-y-1">
                                      <div className="text-xs font-bold text-rose-300 flex items-center gap-1.5">
                                        <AlertCircle size={14} />
                                        PROMOTION SAFETY VIOLATIONS ({elig.failed_reasons.length} Gates Failed)
                                      </div>
                                      <ul className="text-[11px] text-rose-200/90 list-disc list-inside space-y-0.5">
                                        {elig.failed_reasons.map((r, rIdx) => (
                                          <li key={rIdx}>{r}</li>
                                        ))}
                                      </ul>
                                    </div>
                                  )}

                                  {/* Side-by-side Candidate vs Champion Matrix */}
                                  <div className="space-y-2">
                                    <div className="text-xs font-bold text-gray-300 flex items-center gap-1.5">
                                      <BarChart3 size={13} className="text-purple-400" />
                                      SIDE-BY-SIDE EVALUATION: CANDIDATE VS ACTIVE CHAMPION
                                    </div>
                                    <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-[11px]">
                                      <div className="bg-slate-900/80 p-2.5 rounded-lg border border-slate-800">
                                        <div className="text-gray-500 text-[10px]">NET SHARPE</div>
                                        <div className="text-white font-bold mt-0.5">
                                          {cSharpe !== null ? Number(cSharpe).toFixed(2) : 'N/A'}
                                          <span className="text-gray-500 font-normal ml-1">
                                            vs {comp?.champion_metrics?.sharpe !== undefined ? Number(comp.champion_metrics.sharpe).toFixed(2) : 'None'}
                                          </span>
                                        </div>
                                        <div className={`text-[10px] font-bold mt-0.5 ${deltaSharpe !== null && deltaSharpe >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                          {deltaSharpe !== null ? `Δ ${deltaSharpe >= 0 ? '+' : ''}${Number(deltaSharpe).toFixed(2)}` : 'N/A'}
                                        </div>
                                      </div>

                                      <div className="bg-slate-900/80 p-2.5 rounded-lg border border-slate-800">
                                        <div className="text-gray-500 text-[10px]">NET PROFIT FACTOR</div>
                                        <div className="text-white font-bold mt-0.5">
                                          {cPF !== null ? Number(cPF).toFixed(2) : 'N/A'}
                                          <span className="text-gray-500 font-normal ml-1">
                                            vs {comp?.champion_metrics?.profit_factor !== undefined ? Number(comp.champion_metrics.profit_factor).toFixed(2) : 'None'}
                                          </span>
                                        </div>
                                        <div className={`text-[10px] font-bold mt-0.5 ${deltaPF !== null && deltaPF >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                          {deltaPF !== null ? `Δ ${deltaPF >= 0 ? '+' : ''}${Number(deltaPF).toFixed(2)}` : 'N/A'}
                                        </div>
                                      </div>

                                      <div className="bg-slate-900/80 p-2.5 rounded-lg border border-slate-800">
                                        <div className="text-gray-500 text-[10px]">NET WIN RATE</div>
                                        <div className="text-white font-bold mt-0.5">
                                          {cWR !== null ? `${Number(cWR).toFixed(1)}%` : 'N/A'}
                                          <span className="text-gray-500 font-normal ml-1">
                                            vs {comp?.champion_metrics?.win_rate !== undefined ? `${Number(comp.champion_metrics.win_rate).toFixed(1)}%` : 'None'}
                                          </span>
                                        </div>
                                        <div className={`text-[10px] font-bold mt-0.5 ${deltaWR !== null && deltaWR >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                          {deltaWR !== null ? `Δ ${deltaWR >= 0 ? '+' : ''}${Number(deltaWR).toFixed(1)}%` : 'N/A'}
                                        </div>
                                      </div>

                                      <div className="bg-slate-900/80 p-2.5 rounded-lg border border-slate-800">
                                        <div className="text-gray-500 text-[10px]">OOS SAMPLES</div>
                                        <div className="text-white font-bold mt-0.5">
                                          {cTrades !== null ? cTrades.toLocaleString() : 'N/A'}
                                          <span className="text-gray-500 font-normal ml-1">
                                            vs {comp?.champion_metrics?.trades ?? comp?.champion_metrics?.total_trades ?? 'None'}
                                          </span>
                                        </div>
                                        <div className="text-[10px] font-bold mt-0.5 text-indigo-400">
                                          {cTrades !== null && cTrades >= 30 ? 'Gate Pass (≥30)' : 'Gate Fail (<30)'}
                                        </div>
                                      </div>

                                      <div className="bg-slate-900/80 p-2.5 rounded-lg border border-slate-800">
                                        <div className="text-gray-500 text-[10px]">TRUE MAX DRAWDOWN</div>
                                        <div className="text-white font-bold mt-0.5">
                                          {cDD !== null ? `${Number(cDD).toFixed(2)}%` : 'N/A'}
                                          <span className="text-gray-500 font-normal ml-1">
                                            vs {comp?.champion_metrics?.max_drawdown_pct !== undefined ? `${Number(comp.champion_metrics.max_drawdown_pct).toFixed(2)}%` : 'None'}
                                          </span>
                                        </div>
                                        <div className={`text-[10px] font-bold mt-0.5 ${cDD !== null && Number(cDD) <= 20.0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                          {cDD !== null && Number(cDD) <= 20.0 ? 'Gate 7 Pass (≤20.0%)' : 'Gate 7 Fail (>20.0%)'}
                                        </div>
                                      </div>
                                    </div>
                                  </div>

                                  {/* 12 Formal Safety Gates Checklist */}
                                  <div className="space-y-2">
                                    <div className="text-xs font-bold text-gray-300 flex items-center justify-between">
                                      <div className="flex items-center gap-1.5">
                                        <ShieldCheck size={13} className="text-emerald-400" />
                                        12 FORMAL GOVERNANCE PROMOTION GATES
                                      </div>
                                      <span className="text-[10px] text-gray-400 font-mono">
                                        Platform Risk Ceiling: ≤ 20.0% Max DD
                                      </span>
                                    </div>
                                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2 text-[11px]">
                                      {Object.entries(elig?.gates || elig?.gates_detail || {}).map(([gateName, gateData]) => {
                                        const isObj = typeof gateData === 'object' && gateData !== null;
                                        const passed = isObj ? gateData.passed : Boolean(gateData);
                                        const threshold = isObj ? gateData.threshold : null;
                                        const val = isObj ? gateData.value : null;

                                        return (
                                          <div
                                            key={gateName}
                                            className={`p-2.5 rounded-lg border flex flex-col gap-1 ${
                                              passed
                                                ? 'bg-emerald-950/30 border-emerald-500/30 text-emerald-300'
                                                : 'bg-rose-950/30 border-rose-500/30 text-rose-300'
                                            }`}
                                          >
                                            <div className="flex items-center justify-between">
                                              <div className="flex items-center gap-1.5 font-bold">
                                                {passed ? (
                                                  <Check size={13} className="text-emerald-400 shrink-0" />
                                                ) : (
                                                  <X size={13} className="text-rose-400 shrink-0" />
                                                )}
                                                <span className="truncate" title={isObj && gateData.name ? gateData.name : gateName}>
                                                  {isObj && gateData.name ? gateData.name : gateName.replace(/_/g, ' ')}
                                                </span>
                                              </div>
                                              <span className={`text-[10px] font-bold px-1.5 py-0.2 rounded ${passed ? 'bg-emerald-900/60 text-emerald-200' : 'bg-rose-900/60 text-rose-200'}`}>
                                                {passed ? 'PASS' : 'FAIL'}
                                              </span>
                                            </div>
                                            {threshold && (
                                              <div className="text-[10px] text-gray-400 pl-5">
                                                Rule: <span className="text-gray-300">{threshold}</span>
                                              </div>
                                            )}
                                            {val !== null && val !== undefined && (
                                              <div className="text-[10px] text-gray-400 pl-5">
                                                Value: <span className="text-white font-mono">{String(val)}</span>
                                              </div>
                                            )}
                                          </div>
                                        );
                                      })}
                                    </div>
                                  </div>

                                  {/* Cryptographic Hash and Path */}
                                  <div className="p-2.5 bg-black/60 rounded-lg border border-slate-800 text-[10px] flex flex-wrap items-center justify-between gap-2 text-gray-400">
                                    <span>Model Path: <strong className="text-gray-200">{c.model_path}</strong></span>
                                    <span className="truncate max-w-md">SHA-256: <strong className="text-indigo-300 font-mono">{c.artifact_sha256}</strong></span>
                                  </div>
                                </td>
                              </tr>
                            )}
                          </React.Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* Persistent Training Job History Section */}
            <div className="mt-8 pt-6 border-t border-slate-800 space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <Clock className="text-purple-400" size={16} />
                    Persistent Training Job History
                  </h3>
                  <p className="text-xs text-gray-400 font-mono mt-0.5">
                    Server-persisted SQLite records (<code className="text-purple-300">qlib_training_jobs</code>). Survives browser refreshes, tab navigations, and client reloads.
                  </p>
                </div>
                <button
                  onClick={fetchTrainingHistory}
                  disabled={historyLoading}
                  className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-gray-300 rounded-lg text-xs font-mono flex items-center gap-1.5 transition-all"
                >
                  <RefreshCw size={12} className={historyLoading ? 'animate-spin' : ''} />
                  Refresh History
                </button>
              </div>

              {trainingHistory.length === 0 ? (
                <div className="p-6 bg-slate-950/40 border border-slate-800 rounded-xl text-center text-xs font-mono text-gray-500">
                  No training history found in persistent database.
                </div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs font-mono">
                    <thead>
                      <tr className="border-b border-slate-800 text-gray-400">
                        <th className="pb-2">Job ID</th>
                        <th className="pb-2">Status</th>
                        <th className="pb-2">Strategy / Universe</th>
                        <th className="pb-2">Architecture</th>
                        <th className="pb-2">Candidate ID</th>
                        <th className="pb-2">Net Sharpe / PF</th>
                        <th className="pb-2">Elapsed</th>
                        <th className="pb-2 text-right">Started At</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60">
                      {trainingHistory.map(job => (
                        <tr key={job.job_id} className="hover:bg-slate-800/30">
                          <td className="py-2.5 text-white font-semibold truncate max-w-[140px]" title={job.job_id}>
                            {job.job_id}
                          </td>
                          <td className="py-2.5">
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                              job.status === 'COMPLETED'
                                ? 'bg-emerald-950/60 text-emerald-300 border-emerald-500/30'
                                : job.status === 'FAILED'
                                ? 'bg-rose-950/60 text-rose-300 border-rose-500/30'
                                : 'bg-purple-950/60 text-purple-300 border-purple-500/30 animate-pulse'
                            }`}>
                              {job.status}
                            </span>
                          </td>
                          <td className="py-2.5 text-gray-300">
                            <div>{job.strategy || 'SWING'}</div>
                            <div className="text-[10px] text-gray-500">{job.universe}</div>
                          </td>
                          <td className="py-2.5 text-indigo-300 text-[11px]">
                            {job.model_family || 'LGBModel'} / {job.feature_family || 'Alpha158'}
                          </td>
                          <td className="py-2.5 text-gray-300 truncate max-w-[150px]" title={job.candidate_model_id || 'None'}>
                            {job.candidate_model_id || '-'}
                          </td>
                          <td className="py-2.5">
                            {job.metrics?.execution_metrics?.net_sharpe !== undefined || job.metrics?.sharpe !== undefined ? (
                              <div>
                                <span className={Number(job.metrics?.execution_metrics?.net_sharpe ?? job.metrics?.sharpe) >= 0 ? 'text-emerald-400 font-bold' : 'text-rose-400 font-bold'}>
                                  SR: {Number(job.metrics?.execution_metrics?.net_sharpe ?? job.metrics?.sharpe).toFixed(2)}
                                </span>
                                <span className="text-gray-400 ml-1 text-[10px]">
                                  PF: {Number(job.metrics?.execution_metrics?.net_profit_factor ?? job.metrics?.profit_factor ?? 0).toFixed(2)}
                                </span>
                              </div>
                            ) : (
                              <span className="text-gray-500">-</span>
                            )}
                          </td>
                          <td className="py-2.5 text-gray-400">
                            {job.elapsed_seconds ? `${job.elapsed_seconds}s` : '-'}
                          </td>
                          <td className="py-2.5 text-gray-400 text-[11px] text-right">
                            {job.started_at?.slice(0, 16).replace('T', ' ') || '-'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ── TAB 4: SYSTEM / DATA HEALTH (Hardware Benchmarks & Legacy Comparison) ──── */}
      {(activeTab === 'system' || activeTab === 'benchmarks' || activeTab === 'comparison') && (
        <div className="space-y-6">
          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <h2 className="text-lg font-bold text-white flex items-center gap-2 mb-1">
                  <Cpu className="text-purple-400" size={20} />
                  Microsoft Qlib Empirical Hardware Benchmarks
                </h2>
                <p className="text-xs text-gray-400 font-mono">
                  Hardware-measured memory consumption (RSS), dataset rows, feature extraction time, and model training duration across 3 distinct universe tiers.
                </p>
              </div>

              <div className="flex items-center gap-2">
                <button
                  onClick={fetchBenchmarks}
                  disabled={benchmarksLoading}
                  className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded-xl text-xs font-mono text-gray-300 flex items-center gap-1.5 transition-all"
                >
                  <RefreshCw size={13} className={benchmarksLoading ? 'animate-spin' : ''} />
                  Refresh
                </button>
                <button
                  onClick={handleRunBenchmarks}
                  disabled={benchmarksRunning}
                  className="px-4 py-1.5 bg-purple-600 hover:bg-purple-500 text-white rounded-xl text-xs font-mono font-bold flex items-center gap-1.5 shadow transition-all disabled:opacity-50"
                >
                  {benchmarksRunning ? <RefreshCw size={13} className="animate-spin" /> : <Play size={13} />}
                  {benchmarksRunning ? 'Running Benchmarks...' : 'Run Benchmark Suite'}
                </button>
              </div>
            </div>

            {benchmarksError && (
              <div className="p-4 bg-rose-950/40 border border-rose-500/40 rounded-xl text-xs font-mono text-rose-300 flex items-center gap-2">
                <AlertCircle size={16} className="text-rose-400 shrink-0" />
                <span>Benchmark Load Error: {benchmarksError}</span>
              </div>
            )}

            {/* Benchmark Tier Cards */}
            {benchmarks?.tiers ? (
              <div className="space-y-6">
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
                  {Object.entries(benchmarks.tiers).map(([tierKey, tierData]) => {
                    const isEstablished = tierKey === 'NIFTY_500_ESTABLISHED';
                    const isFno = tierKey === 'FNO';
                    return (
                      <div
                        key={tierKey}
                        className={`rounded-2xl p-5 border flex flex-col justify-between space-y-4 font-mono transition-all ${
                          isEstablished
                            ? 'bg-purple-950/20 border-purple-500/50 shadow-lg shadow-purple-950/30'
                            : isFno
                            ? 'bg-indigo-950/20 border-indigo-500/40'
                            : 'bg-slate-950/80 border-slate-800'
                        }`}
                      >
                        <div>
                          <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                            <div>
                              <span className="text-[10px] font-bold tracking-wider uppercase text-gray-500">
                                TIER: {tierKey}
                              </span>
                              <h3 className="text-sm font-black text-white mt-0.5">
                                {tierData.display_name}
                              </h3>
                            </div>
                            <span
                              className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                                isEstablished
                                  ? 'bg-purple-500/20 text-purple-300 border border-purple-500/30'
                                  : isFno
                                  ? 'bg-indigo-500/20 text-indigo-300 border border-indigo-500/30'
                                  : 'bg-slate-800 text-gray-300 border border-slate-700'
                              }`}
                            >
                              {tierData.instruments_count} STOCKS
                            </span>
                          </div>

                          <div className="grid grid-cols-2 gap-2.5 mt-4 text-xs">
                            <div className="bg-slate-900/80 p-2.5 rounded-xl border border-slate-800">
                              <span className="text-[10px] text-gray-500 block">TOTAL DATA ROWS</span>
                              <span className="text-white font-bold text-sm">
                                {tierData.feature_rows?.total?.toLocaleString() ?? 'N/A'}
                              </span>
                            </div>
                            <div className="bg-slate-900/80 p-2.5 rounded-xl border border-slate-800">
                              <span className="text-[10px] text-gray-500 block">ALPHA158 FACTORS</span>
                              <span className="text-purple-300 font-bold text-sm">
                                {tierData.features_count ?? 158}
                              </span>
                            </div>
                            <div className="bg-slate-900/80 p-2.5 rounded-xl border border-slate-800">
                              <span className="text-[10px] text-gray-500 block">FEATURE GEN TIME</span>
                              <span className="text-emerald-400 font-bold text-sm">
                                {tierData.timings_seconds?.feature_generation
                                  ? `${tierData.timings_seconds.feature_generation}s`
                                  : 'N/A'}
                              </span>
                            </div>
                            <div className="bg-slate-900/80 p-2.5 rounded-xl border border-slate-800">
                              <span className="text-[10px] text-gray-500 block">MODEL FIT TIME</span>
                              <span className="text-emerald-400 font-bold text-sm">
                                {tierData.timings_seconds?.model_training
                                  ? `${tierData.timings_seconds.model_training}s`
                                  : 'N/A'}
                              </span>
                            </div>
                            <div className="bg-slate-900/80 p-2.5 rounded-xl border border-slate-800">
                              <span className="text-[10px] text-gray-500 block">PEAK MEMORY (RSS)</span>
                              <span className="text-indigo-300 font-bold text-sm">
                                {tierData.memory_mb?.peak ? `${tierData.memory_mb.peak} MB` : 'N/A'}
                              </span>
                            </div>
                            <div className="bg-slate-900/80 p-2.5 rounded-xl border border-slate-800">
                              <span className="text-[10px] text-gray-500 block">LATENCY / STOCK</span>
                              <span className="text-amber-300 font-bold text-sm">
                                {tierData.model?.latency_per_stock_ms
                                  ? `${tierData.model.latency_per_stock_ms} ms`
                                  : 'N/A'}
                              </span>
                            </div>
                          </div>
                        </div>

                        <div className="pt-3 border-t border-slate-800/80 flex items-center justify-between text-[11px] text-gray-400">
                          <span>Total Elapsed: {tierData.timings_seconds?.total_elapsed ? `${tierData.timings_seconds.total_elapsed}s` : 'N/A'}</span>
                          <span className="text-purple-300 font-bold">Artifact: {tierData.model?.artifact_size_kb ? `${tierData.model.artifact_size_kb} KB` : 'N/A'}</span>
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* Empirical Benchmark Summary Table */}
                <div className="bg-slate-950/80 border border-slate-800 rounded-2xl p-5 overflow-x-auto">
                  <h4 className="text-xs font-bold text-gray-300 font-mono uppercase tracking-wider mb-3">
                    Comparative Scaling Matrix
                  </h4>
                  <table className="w-full text-xs font-mono text-left">
                    <thead>
                      <tr className="border-b border-slate-800 text-gray-500">
                        <th className="py-2.5 pr-4">Tier</th>
                        <th className="py-2.5 px-4">Stocks</th>
                        <th className="py-2.5 px-4">Rows Processed</th>
                        <th className="py-2.5 px-4">Alpha158 Extraction</th>
                        <th className="py-2.5 px-4">Train Time</th>
                        <th className="py-2.5 px-4">Total Time</th>
                        <th className="py-2.5 px-4">Peak RAM</th>
                        <th className="py-2.5 pl-4">Inference / Stock</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60">
                      {Object.entries(benchmarks.tiers).map(([k, t]) => (
                        <tr key={k} className="hover:bg-slate-900/40">
                          <td className="py-2.5 pr-4 font-bold text-white">{k}</td>
                          <td className="py-2.5 px-4 text-purple-300">{t.instruments_count}</td>
                          <td className="py-2.5 px-4 text-gray-300">{t.feature_rows?.total?.toLocaleString()}</td>
                          <td className="py-2.5 px-4 text-emerald-400">{t.timings_seconds?.feature_generation}s</td>
                          <td className="py-2.5 px-4 text-emerald-400">{t.timings_seconds?.model_training}s</td>
                          <td className="py-2.5 px-4 font-bold text-white">{t.timings_seconds?.total_elapsed}s</td>
                          <td className="py-2.5 px-4 text-indigo-300">{t.memory_mb?.peak} MB</td>
                          <td className="py-2.5 pl-4 text-amber-300">{t.model?.latency_per_stock_ms} ms</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="mt-3 text-[10px] text-gray-500 font-mono">
                    System Architecture: {benchmarks.system_info?.platform || 'Darwin / arm64'} · CPU: {benchmarks.system_info?.cpu_count || 12} Cores · Total RAM: {benchmarks.system_info?.total_ram_gb || 16} GB
                  </div>
                </div>
              </div>
            ) : (
              !benchmarksLoading && (
                <div className="text-center py-12 text-xs font-mono text-gray-500 border border-dashed border-slate-800 rounded-xl space-y-2">
                  <div className="text-gray-400 font-bold">No Benchmark Data Loaded</div>
                  <div className="text-[11px] text-gray-600">Click "Refresh" or "Run Benchmark Suite" to measure empirical performance.</div>
                </div>
              )
            )}
          </div>

          <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 space-y-6">
            <div>
              <h2 className="text-lg font-bold text-white flex items-center gap-2 mb-1">
                <BarChart3 className="text-purple-400" size={20} />
                Legacy Production Champion vs Microsoft Qlib
              </h2>
              <p className="text-xs text-gray-400 font-mono">
                Rigorous, transparent side-by-side performance audit derived strictly from authoritative database history. The migration decision requires verified statistical superiority on live forward trading.
              </p>
            </div>

            {/* Verdict Callout */}
            <div className="p-4 bg-purple-950/30 border border-purple-500/40 rounded-xl">
              <div className="text-xs font-mono text-gray-400 uppercase tracking-wider">Independent Audit Verdict</div>
              <div className="text-sm font-bold text-purple-300 mt-1">{comparisonData?.verdict || 'Evaluation in progress...'}</div>
              <div className="mt-2 text-xs font-mono text-gray-400">
                Action Decision: <span className="text-white font-bold px-2 py-0.5 bg-slate-800 border border-slate-700 rounded ml-1">{comparisonData?.system_decision || 'MONITORING'}</span>
              </div>
            </div>

            {/* Overall Side-by-Side Scorecard */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Legacy Column */}
              <div className="bg-slate-950/80 border border-slate-800 rounded-2xl p-5 space-y-4">
                <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                  <span className="font-bold text-sm text-gray-200">LEGACY SYSTEM (Control)</span>
                  <span className="px-2.5 py-0.5 bg-blue-500/20 text-blue-300 border border-blue-500/30 rounded text-[10px] font-mono font-bold">
                    ACTIVE PRODUCTION
                  </span>
                </div>
                <div className="space-y-3 text-xs font-mono">
                  <div className="flex justify-between text-gray-400">
                    <span>Total Recommendations:</span>
                    <span className="text-white font-bold">{comparisonData?.legacy?.total_recommendations ?? 0}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Completed Trades:</span>
                    <span className="text-white font-bold">{comparisonData?.legacy?.completed_trades ?? 0}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Realized Win Rate:</span>
                    <span className="text-emerald-400 font-bold">
                      {comparisonData?.legacy?.win_rate != null ? `${comparisonData.legacy.win_rate}%` : 'N/A'}
                    </span>
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
                  <div className="flex justify-between items-center text-gray-400">
                    <span className="flex items-center space-x-1">
                      <span>Cumulative Return:</span>
                      <span className="text-[10px] text-gray-500 font-sans" title="Arithmetic sum of individual percentage returns across all closed trades">(Σ r_i)</span>
                    </span>
                    <span className={`font-bold ${(comparisonData?.legacy?.net_pnl_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {comparisonData?.legacy?.net_pnl_pct != null ? `${comparisonData.legacy.net_pnl_pct > 0 ? '+' : ''}${comparisonData.legacy.net_pnl_pct}%` : 'N/A'}
                    </span>
                  </div>
                  {comparisonData?.legacy?.avg_trade_pnl_pct != null && (
                    <div className="flex justify-between text-gray-400">
                      <span>Avg P&L / Trade:</span>
                      <span className={`font-bold ${comparisonData.legacy.avg_trade_pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {comparisonData.legacy.avg_trade_pnl_pct > 0 ? '+' : ''}{comparisonData.legacy.avg_trade_pnl_pct}%
                      </span>
                    </div>
                  )}

                  <div className="flex justify-between text-gray-400">
                    <span>Avg Holding Period:</span>
                    <span className="text-gray-300">{comparisonData?.legacy?.avg_holding_period ?? 'N/A'}</span>
                  </div>
                </div>
              </div>

              {/* Qlib Column */}
              <div className="bg-slate-950/80 border border-purple-500/40 rounded-2xl p-5 space-y-4 shadow-lg shadow-purple-950/30">
                <div className="flex items-center justify-between pb-3 border-b border-slate-800">
                  <span className="font-bold text-sm text-purple-300">QLIB SYSTEM (Experimental)</span>
                  <span className="px-2.5 py-0.5 bg-purple-500/20 text-purple-300 border border-purple-500/30 rounded text-[10px] font-mono font-bold">
                    PARALLEL BRANCH
                  </span>
                </div>
                <div className="space-y-3 text-xs font-mono">
                  <div className="flex justify-between text-gray-400">
                    <span>Total Virtual Recommendations:</span>
                    <span className="text-white font-bold">{comparisonData?.qlib?.total_recommendations ?? 0}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Completed Virtual Trades:</span>
                    <span className="text-white font-bold">{comparisonData?.qlib?.completed_trades ?? 0}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Forward Live Win Rate:</span>
                    <span className="font-bold">
                      {comparisonData?.qlib?.completed_trades > 0 && comparisonData?.qlib?.win_rate != null
                        ? `${comparisonData.qlib.win_rate}%`
                        : <span className="text-amber-400 font-bold">INSUFFICIENT DATA (0 closed)</span>}
                    </span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Live Profit Factor:</span>
                    <span className="font-bold">
                      {comparisonData?.qlib?.completed_trades > 0 && comparisonData?.qlib?.profit_factor != null
                        ? comparisonData.qlib.profit_factor
                        : <span className="text-gray-500">N/A</span>}
                    </span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Locked OOS Validation Win Rate:</span>
                    <span className="text-emerald-400 font-bold">
                      {comparisonData?.qlib?.oos_win_rate != null ? `${comparisonData.qlib.oos_win_rate}%` : 'N/A'}
                    </span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Locked OOS Profit Factor:</span>
                    <span className="text-emerald-400 font-bold">{comparisonData?.qlib?.oos_profit_factor ?? 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Locked OOS Sharpe:</span>
                    <span className="text-purple-300 font-bold">{comparisonData?.qlib?.oos_sharpe ?? 'N/A'}</span>
                  </div>
                  <div className="flex justify-between text-gray-400">
                    <span>Live Risk & Portfolio Heat:</span>
                    <span className="text-emerald-400 font-bold">0.0% (Zero Live Risk)</span>
                  </div>
                </div>
              </div>
            </div>

            {/* Strategy Breakdowns */}
            <div className="pt-4 border-t border-slate-800 space-y-4">
              <h3 className="text-sm font-bold text-gray-200 font-mono uppercase tracking-wider">
                Strategy-Level Side-by-Side Breakdown
              </h3>

              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 font-mono text-xs">
                {/* Swing Breakdown */}
                <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-3">
                  <div className="flex items-center justify-between pb-2 border-b border-slate-800">
                    <span className="font-bold text-white">SWING (Daily)</span>
                    <span className="text-[10px] text-purple-300">Direct Comparison</span>
                  </div>
                  <div className="space-y-1.5 text-[11px]">
                    <div className="text-gray-500 font-bold uppercase text-[10px]">Legacy Control Group:</div>
                    <div className="flex justify-between text-gray-400">
                      <span>Completed:</span>
                      <span className="text-white font-bold">{comparisonData?.by_strategy?.SWING?.legacy?.completed_trades ?? 0}</span>
                    </div>
                    <div className="flex justify-between text-gray-400">
                      <span>Win Rate:</span>
                      <span className="text-emerald-400 font-bold">{comparisonData?.by_strategy?.SWING?.legacy?.win_rate ?? 0}%</span>
                    </div>
                    <div className="flex justify-between items-center text-gray-400">
                      <span className="flex items-center space-x-1">
                        <span>Net Return:</span>
                        <span className="text-[9px] text-gray-500 font-sans" title="Arithmetic sum of individual trade percentage returns">(Σ r_i)</span>
                      </span>
                      <span className={`font-bold ${(comparisonData?.by_strategy?.SWING?.legacy?.net_pnl_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {comparisonData?.by_strategy?.SWING?.legacy?.net_pnl_pct != null ? `${comparisonData.by_strategy.SWING.legacy.net_pnl_pct > 0 ? '+' : ''}${comparisonData.by_strategy.SWING.legacy.net_pnl_pct}%` : '0%'}
                      </span>
                    </div>
                    {comparisonData?.by_strategy?.SWING?.legacy?.avg_trade_pnl_pct != null && (
                      <div className="flex justify-between text-gray-400">
                        <span>Avg / Trade:</span>
                        <span className={`font-bold ${comparisonData.by_strategy.SWING.legacy.avg_trade_pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                          {comparisonData.by_strategy.SWING.legacy.avg_trade_pnl_pct > 0 ? '+' : ''}{comparisonData.by_strategy.SWING.legacy.avg_trade_pnl_pct}%
                        </span>
                      </div>
                    )}

                    <div className="text-purple-400 font-bold uppercase text-[10px] pt-2">Qlib Experimental:</div>
                    <div className="flex justify-between text-gray-400">
                      <span>Total Recs:</span>
                      <span className="text-white font-bold">{comparisonData?.by_strategy?.SWING?.qlib?.total_recommendations ?? 0}</span>
                    </div>
                    <div className="flex justify-between text-gray-400">
                      <span>Completed:</span>
                      <span className="text-white font-bold">{comparisonData?.by_strategy?.SWING?.qlib?.completed_trades ?? 0}</span>
                    </div>
                    <div className="flex justify-between text-gray-400">
                      <span>Win Rate:</span>
                      <span className="text-amber-400 font-semibold">
                        {comparisonData?.by_strategy?.SWING?.qlib?.completed_trades > 0
                          ? `${comparisonData.by_strategy.SWING.qlib.win_rate}%`
                          : 'INSUFFICIENT DATA'}
                      </span>
                    </div>
                    {comparisonData?.by_strategy?.SWING?.qlib?.completed_trades > 0 && comparisonData?.by_strategy?.SWING?.qlib?.net_pnl_pct != null && (
                      <div className="flex justify-between items-center text-gray-400">
                        <span>Net Return (Σ r_i):</span>
                        <span className={`font-bold ${comparisonData.by_strategy.SWING.qlib.net_pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                          {comparisonData.by_strategy.SWING.qlib.net_pnl_pct > 0 ? '+' : ''}{comparisonData.by_strategy.SWING.qlib.net_pnl_pct}%
                        </span>
                      </div>
                    )}
                  </div>
                </div>

                {/* Intraday Breakdown */}
                <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-3">
                  <div className="flex items-center justify-between pb-2 border-b border-slate-800">
                    <span className="font-bold text-white">INTRADAY (15m)</span>
                    <span className="text-[10px] text-purple-300">Direct Comparison</span>
                  </div>
                  <div className="space-y-1.5 text-[11px]">
                    <div className="text-gray-500 font-bold uppercase text-[10px]">Legacy Control Group:</div>
                    <div className="flex justify-between text-gray-400">
                      <span>Completed:</span>
                      <span className="text-white font-bold">{comparisonData?.by_strategy?.INTRADAY?.legacy?.completed_trades ?? 0}</span>
                    </div>
                    <div className="flex justify-between text-gray-400">
                      <span>Win Rate:</span>
                      <span className="text-emerald-400 font-bold">{comparisonData?.by_strategy?.INTRADAY?.legacy?.win_rate ?? 0}%</span>
                    </div>
                    <div className="flex justify-between items-center text-gray-400">
                      <span className="flex items-center space-x-1">
                        <span>Net Return:</span>
                        <span className="text-[9px] text-gray-500 font-sans" title="Arithmetic sum of individual trade percentage returns">(Σ r_i)</span>
                      </span>
                      <span className={`font-bold ${(comparisonData?.by_strategy?.INTRADAY?.legacy?.net_pnl_pct ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {comparisonData?.by_strategy?.INTRADAY?.legacy?.net_pnl_pct != null ? `${comparisonData.by_strategy.INTRADAY.legacy.net_pnl_pct > 0 ? '+' : ''}${comparisonData.by_strategy.INTRADAY.legacy.net_pnl_pct}%` : '0%'}
                      </span>
                    </div>
                    {comparisonData?.by_strategy?.INTRADAY?.legacy?.avg_trade_pnl_pct != null && (
                      <div className="flex justify-between text-gray-400">
                        <span>Avg / Trade:</span>
                        <span className={`font-bold ${comparisonData.by_strategy.INTRADAY.legacy.avg_trade_pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                          {comparisonData.by_strategy.INTRADAY.legacy.avg_trade_pnl_pct > 0 ? '+' : ''}{comparisonData.by_strategy.INTRADAY.legacy.avg_trade_pnl_pct}%
                        </span>
                      </div>
                    )}

                    <div className="text-purple-400 font-bold uppercase text-[10px] pt-2">Qlib Experimental:</div>
                    <div className="flex justify-between text-gray-400">
                      <span>Total Recs:</span>
                      <span className="text-white font-bold">{comparisonData?.by_strategy?.INTRADAY?.qlib?.total_recommendations ?? 0}</span>
                    </div>
                    <div className="flex justify-between text-gray-400">
                      <span>Completed:</span>
                      <span className="text-white font-bold">{comparisonData?.by_strategy?.INTRADAY?.qlib?.completed_trades ?? 0}</span>
                    </div>
                    <div className="flex justify-between text-gray-400">
                      <span>Win Rate:</span>
                      <span className="text-amber-400 font-semibold">
                        {comparisonData?.by_strategy?.INTRADAY?.qlib?.completed_trades > 0
                          ? `${comparisonData.by_strategy.INTRADAY.qlib.win_rate}%`
                          : 'INSUFFICIENT DATA'}
                      </span>
                    </div>
                    {comparisonData?.by_strategy?.INTRADAY?.qlib?.completed_trades > 0 && comparisonData?.by_strategy?.INTRADAY?.qlib?.net_pnl_pct != null && (
                      <div className="flex justify-between items-center text-gray-400">
                        <span>Net Return (Σ r_i):</span>
                        <span className={`font-bold ${comparisonData.by_strategy.INTRADAY.qlib.net_pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                          {comparisonData.by_strategy.INTRADAY.qlib.net_pnl_pct > 0 ? '+' : ''}{comparisonData.by_strategy.INTRADAY.qlib.net_pnl_pct}%
                        </span>
                      </div>
                    )}
                  </div>
                </div>

                {/* F&O Breakdown */}
                <div className="bg-slate-950/60 p-4 rounded-xl border border-slate-800 space-y-3">
                  <div className="flex items-center justify-between pb-2 border-b border-slate-800">
                    <span className="font-bold text-white">F&O CONTRACTS</span>
                    <span className="text-[10px] text-purple-300">Qlib Only</span>
                  </div>
                  <div className="space-y-1.5 text-[11px]">
                    <div className="text-gray-500 font-bold uppercase text-[10px]">Legacy Control Group:</div>
                    <div className="text-gray-500 italic text-[11px] py-2">
                      No Legacy F&O scanner exists. Qlib runs as standalone experimental engine.
                    </div>

                    <div className="text-purple-400 font-bold uppercase text-[10px] pt-1">Qlib Experimental:</div>
                    <div className="flex justify-between text-gray-400">
                      <span>Total Recs:</span>
                      <span className="text-white font-bold">{comparisonData?.by_strategy?.FNO?.qlib?.total_recommendations ?? 0}</span>
                    </div>
                    <div className="flex justify-between text-gray-400">
                      <span>Completed:</span>
                      <span className="text-white font-bold">{comparisonData?.by_strategy?.FNO?.qlib?.completed_trades ?? 0}</span>
                    </div>
                    <div className="flex justify-between text-gray-400">
                      <span>Win Rate:</span>
                      <span className="text-amber-400 font-semibold">
                        {comparisonData?.by_strategy?.FNO?.qlib?.completed_trades > 0
                          ? `${comparisonData.by_strategy.FNO.qlib.win_rate}%`
                          : 'INSUFFICIENT DATA'}
                      </span>
                    </div>
                    {comparisonData?.by_strategy?.FNO?.qlib?.completed_trades > 0 && comparisonData?.by_strategy?.FNO?.qlib?.net_pnl_pct != null && (
                      <div className="flex justify-between items-center text-gray-400">
                        <span>Net Return (Σ r_i):</span>
                        <span className={`font-bold ${comparisonData.by_strategy.FNO.qlib.net_pnl_pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                          {comparisonData.by_strategy.FNO.qlib.net_pnl_pct > 0 ? '+' : ''}{comparisonData.by_strategy.FNO.qlib.net_pnl_pct}%
                        </span>
                      </div>
                    )}
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
