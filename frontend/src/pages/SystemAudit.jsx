import React, { useState, useEffect, useCallback } from 'react';
import axios from 'axios';
import { 
  ShieldCheck, ShieldAlert, Activity, RefreshCw, Filter, Search, 
  CheckCircle2, AlertTriangle, XCircle, ArrowRight, Play, Cpu, 
  Database, Bell, Terminal, Clock, ChevronDown, ChevronRight 
} from 'lucide-react';
import { API_BASE } from '../services/api';

const CATEGORIES = [
  { id: '', label: 'All Categories' },
  { id: 'SCAN_MANUAL', label: 'Manual Scans' },
  { id: 'SCAN_AUTONOMOUS', label: 'Autonomous Sweeps' },
  { id: 'DECISION_ENGINE', label: 'Decision Engine' },
  { id: 'TELEGRAM', label: 'Telegram Notifications' },
  { id: 'AI_GUARD', label: 'AI Guard & Risk' },
  { id: 'RESEARCH', label: 'Research Engine' },
  { id: 'SCHEDULER', label: 'Scheduler Jobs' },
  { id: 'PROMOTION', label: 'Challenger Promotion' },
  { id: 'DATA_GATE', label: 'Data Integrity Gate' },
  { id: 'SYSTEM_TEST', label: 'System Diagnostics' }
];

const SEVERITIES = [
  { id: '', label: 'All Severities' },
  { id: 'INFO', label: 'INFO' },
  { id: 'WARNING', label: 'WARNING' },
  { id: 'ERROR', label: 'ERROR' },
  { id: 'CRITICAL', label: 'CRITICAL' },
  { id: 'DEBUG', label: 'DEBUG' }
];

export default function SystemAudit() {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(false);
  const [category, setCategory] = useState('');
  const [severity, setSeverity] = useState('');
  const [tickerSearch, setTickerSearch] = useState('');
  const [expandedId, setExpandedId] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);

  // Diagnostic state
  const [diagRunning, setDiagRunning] = useState(false);
  const [diagResult, setDiagResult] = useState(null);

  const fetchEvents = useCallback(async () => {
    setLoading(true);
    try {
      const params = { limit: 150 };
      if (category) params.category = category;
      if (severity) params.severity = severity;
      if (tickerSearch.trim()) params.ticker = tickerSearch.trim().toUpperCase();

      const res = await axios.get(`${API_BASE}/system/audit-log`, { params });
      if (res.data && res.data.events) {
        setEvents(res.data.events);
      }
    } catch (err) {
      console.error('Failed to fetch audit events:', err);
    } finally {
      setLoading(false);
    }
  }, [category, severity, tickerSearch]);

  useEffect(() => {
    fetchEvents();
  }, [fetchEvents]);

  // Auto-refresh every 10 seconds
  useEffect(() => {
    if (!autoRefresh) return;
    const interval = setInterval(() => {
      fetchEvents();
    }, 10000);
    return () => clearInterval(interval);
  }, [autoRefresh, fetchEvents]);

  const runDiagnostic = async () => {
    setDiagRunning(true);
    try {
      const res = await axios.post(`${API_BASE}/system/pipeline-test`);
      setDiagResult(res.data);
      fetchEvents(); // Refresh logs after diagnostic runs
    } catch (err) {
      console.error('Failed to run pipeline diagnostic:', err);
      setDiagResult({
        status: 'FAIL',
        overall_pass: false,
        error: err.message,
        stages: []
      });
    } finally {
      setDiagRunning(false);
    }
  };

  const getSeverityBadge = (sev) => {
    switch (sev) {
      case 'CRITICAL':
        return <span className="px-2 py-0.5 rounded text-[11px] font-black bg-rose-950 text-rose-300 border border-rose-800">CRITICAL</span>;
      case 'ERROR':
        return <span className="px-2 py-0.5 rounded text-[11px] font-bold bg-red-950 text-red-300 border border-red-800">ERROR</span>;
      case 'WARNING':
        return <span className="px-2 py-0.5 rounded text-[11px] font-semibold bg-amber-950 text-amber-300 border border-amber-800">WARNING</span>;
      case 'DEBUG':
        return <span className="px-2 py-0.5 rounded text-[11px] font-medium bg-slate-800 text-slate-400 border border-slate-700">DEBUG</span>;
      default:
        return <span className="px-2 py-0.5 rounded text-[11px] font-medium bg-emerald-950 text-emerald-300 border border-emerald-800">INFO</span>;
    }
  };

  const getCategoryBadge = (cat) => {
    const colors = {
      SCAN_MANUAL: 'bg-indigo-950 text-indigo-300 border-indigo-800',
      SCAN_AUTONOMOUS: 'bg-cyan-950 text-cyan-300 border-cyan-800',
      DECISION_ENGINE: 'bg-purple-950 text-purple-300 border-purple-800',
      TELEGRAM: 'bg-sky-950 text-sky-300 border-sky-800',
      AI_GUARD: 'bg-rose-950 text-rose-300 border-rose-800',
      RESEARCH: 'bg-amber-950 text-amber-300 border-amber-800',
      SCHEDULER: 'bg-teal-950 text-teal-300 border-teal-800',
      PROMOTION: 'bg-fuchsia-950 text-fuchsia-300 border-fuchsia-800',
      DATA_GATE: 'bg-orange-950 text-orange-300 border-orange-800',
      SYSTEM_TEST: 'bg-emerald-950 text-emerald-300 border-emerald-800'
    };
    const cls = colors[cat] || 'bg-slate-800 text-slate-300 border-slate-700';
    return (
      <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold uppercase tracking-wider border ${cls}`}>
        {cat}
      </span>
    );
  };

  return (
    <div className="space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl">
        <div className="flex items-center space-x-3">
          <div className="p-3 bg-indigo-600/20 text-indigo-400 rounded-xl border border-indigo-500/30">
            <ShieldCheck size={28} />
          </div>
          <div>
            <h1 className="text-xl sm:text-2xl font-black text-white tracking-tight flex items-center gap-2">
              Master System Audit & Logger
              <span className="text-xs font-mono font-normal px-2.5 py-0.5 rounded-full bg-emerald-950 text-emerald-400 border border-emerald-800">
                LIVE AUDIT
              </span>
            </h1>
            <p className="text-xs sm:text-sm text-slate-400 mt-1">
              Unified forensic audit trail across scans, decision engine, models, risk gates, and background jobs.
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-3 self-end md:self-auto">
          <label className="flex items-center space-x-2 text-xs text-slate-400 cursor-pointer select-none">
            <input 
              type="checkbox" 
              checked={autoRefresh} 
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="rounded bg-slate-800 border-slate-700 text-indigo-500 focus:ring-0" 
            />
            <span>Auto-poll (10s)</span>
          </label>

          <button
            onClick={fetchEvents}
            disabled={loading}
            className="flex items-center space-x-1.5 px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-xl border border-slate-700 transition"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            <span>Refresh</span>
          </button>

          <button
            onClick={runDiagnostic}
            disabled={diagRunning}
            className="flex items-center space-x-2 px-4 py-2 bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 text-white text-xs font-bold rounded-xl shadow-lg transition"
          >
            {diagRunning ? (
              <>
                <RefreshCw size={14} className="animate-spin" />
                <span>Sweeping 11 Stages...</span>
              </>
            ) : (
              <>
                <Play size={14} />
                <span>Run Pipeline Health Test</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Lifecycle Flow Banner */}
      <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-3.5 text-xs text-slate-300 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center space-x-2 font-mono text-[11px]">
          <span className="text-slate-400 font-bold uppercase tracking-wider">Audit Trace Flow:</span>
          <span className="text-indigo-400 font-bold">START</span>
          <ArrowRight size={12} className="text-slate-600" />
          <span className="text-cyan-400 font-bold">DATA</span>
          <ArrowRight size={12} className="text-slate-600" />
          <span className="text-purple-400 font-bold">MODELS</span>
          <ArrowRight size={12} className="text-slate-600" />
          <span className="text-emerald-400 font-bold">QUALIFICATION</span>
          <ArrowRight size={12} className="text-slate-600" />
          <span className="text-teal-400 font-bold">PERSISTENCE</span>
          <ArrowRight size={12} className="text-slate-600" />
          <span className="text-sky-400 font-bold">TELEGRAM</span>
          <ArrowRight size={12} className="text-slate-600" />
          <span className="text-slate-200 font-bold">FRONTEND</span>
        </div>
        <div className="text-[11px] text-slate-400">
          Showing <strong className="text-white">{events.length}</strong> recorded application events
        </div>
      </div>

      {/* Synthetic Diagnostic Results Card (Shows when run) */}
      {diagResult && (
        <div className="bg-slate-900 border border-emerald-900/60 rounded-2xl p-5 shadow-2xl space-y-4">
          <div className="flex items-center justify-between border-b border-slate-800 pb-3">
            <div className="flex items-center space-x-3">
              <div className={`p-2 rounded-lg ${diagResult.overall_pass ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : 'bg-rose-950 text-rose-400 border border-rose-800'}`}>
                {diagResult.overall_pass ? <CheckCircle2 size={20} /> : <AlertTriangle size={20} />}
              </div>
              <div>
                <h3 className="text-sm font-bold text-white flex items-center gap-2">
                  Synthetic Pipeline Diagnostic: <span className={diagResult.overall_pass ? 'text-emerald-400' : 'text-rose-400'}>{diagResult.status}</span>
                  <span className="text-xs font-mono font-normal text-slate-400">({diagResult.passed_stages}/{diagResult.total_stages} Stages Passed • {diagResult.duration_ms}ms)</span>
                </h3>
                <p className="text-xs text-slate-400">
                  Target: <code>{diagResult.symbol}</code> • Safe simulated execution: No model corruption, no heat drag, Telegram safely suppressed.
                </p>
              </div>
            </div>
            <button 
              onClick={() => setDiagResult(null)}
              className="text-xs text-slate-400 hover:text-white px-2 py-1 bg-slate-800 rounded-md"
            >
              Dismiss
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {diagResult.stages && diagResult.stages.map((st, idx) => (
              <div 
                key={idx} 
                className={`p-3 rounded-xl border text-xs flex flex-col justify-between ${
                  st.status === 'PASS' 
                    ? 'bg-emerald-950/20 border-emerald-800/40 text-emerald-300' 
                    : 'bg-rose-950/30 border-rose-800/50 text-rose-300'
                }`}
              >
                <div className="flex items-center justify-between font-bold mb-1">
                  <span className="truncate">{st.stage}</span>
                  <span className={`px-1.5 py-0.5 rounded text-[10px] font-mono ${st.status === 'PASS' ? 'bg-emerald-900/60 text-emerald-200' : 'bg-rose-900/60 text-rose-200'}`}>
                    {st.status}
                  </span>
                </div>
                <p className="text-[11px] text-slate-300 leading-relaxed">{st.detail}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Filter Toolbar */}
      <div className="bg-slate-900/90 border border-slate-800 rounded-2xl p-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center space-x-2 shrink-0">
          <Filter size={16} className="text-slate-400" />
          <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">Filters:</span>
        </div>

        {/* Category Dropdown */}
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="bg-slate-800 border border-slate-700 text-xs text-slate-200 rounded-xl px-3 py-2 focus:ring-1 focus:ring-indigo-500 focus:outline-none"
        >
          {CATEGORIES.map((c) => (
            <option key={c.id} value={c.id}>{c.label}</option>
          ))}
        </select>

        {/* Severity Dropdown */}
        <select
          value={severity}
          onChange={(e) => setSeverity(e.target.value)}
          className="bg-slate-800 border border-slate-700 text-xs text-slate-200 rounded-xl px-3 py-2 focus:ring-1 focus:ring-indigo-500 focus:outline-none"
        >
          {SEVERITIES.map((s) => (
            <option key={s.id} value={s.id}>{s.label}</option>
          ))}
        </select>

        {/* Ticker Search */}
        <div className="relative flex-1 min-w-[160px] max-w-xs">
          <Search size={14} className="absolute left-3 top-2.5 text-slate-400" />
          <input
            type="text"
            placeholder="Search ticker (e.g. RELIANCE)..."
            value={tickerSearch}
            onChange={(e) => setTickerSearch(e.target.value)}
            className="w-full bg-slate-800 border border-slate-700 text-xs text-slate-200 rounded-xl pl-9 pr-3 py-2 focus:ring-1 focus:ring-indigo-500 focus:outline-none font-mono"
          />
        </div>

        {(category || severity || tickerSearch) && (
          <button
            onClick={() => { setCategory(''); setSeverity(''); setTickerSearch(''); }}
            className="text-xs text-indigo-400 hover:text-indigo-300 underline ml-auto"
          >
            Clear Filters
          </button>
        )}
      </div>

      {/* Master Events Table */}
      <div className="bg-slate-900 border border-slate-800 rounded-2xl shadow-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="bg-slate-950/80 border-b border-slate-800 text-slate-400 font-mono uppercase text-[10px] tracking-wider">
                <th className="p-3.5 w-10 text-center">#</th>
                <th className="p-3.5 w-40">Timestamp</th>
                <th className="p-3.5 w-36">Category</th>
                <th className="p-3.5 w-36">Event Type</th>
                <th className="p-3.5 w-28">Ticker</th>
                <th className="p-3.5 w-24">Severity</th>
                <th className="p-3.5">Message</th>
                <th className="p-3.5 w-16 text-center">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 font-sans">
              {events.length === 0 && !loading && (
                <tr>
                  <td colSpan="8" className="p-12 text-center text-slate-400 font-medium">
                    No audit events match the active filter criteria.
                  </td>
                </tr>
              )}
              {events.map((ev) => {
                const isExpanded = expandedId === ev.id;
                return (
                  <React.Fragment key={ev.id}>
                    <tr className={`hover:bg-slate-800/50 transition cursor-pointer ${isExpanded ? 'bg-slate-800/60' : ''}`}
                        onClick={() => setExpandedId(isExpanded ? null : ev.id)}>
                      <td className="p-3 text-center text-slate-500 font-mono">{ev.id}</td>
                      <td className="p-3 font-mono text-[11px] text-slate-300 whitespace-nowrap">
                        {ev.timestamp ? ev.timestamp.replace('T', ' ').slice(0, 19) : ''}
                      </td>
                      <td className="p-3 whitespace-nowrap">{getCategoryBadge(ev.category)}</td>
                      <td className="p-3 font-mono font-bold text-slate-200 whitespace-nowrap">{ev.event_type}</td>
                      <td className="p-3 font-mono font-bold text-indigo-300 whitespace-nowrap">
                        {ev.ticker || (ev.universe ? <span className="text-slate-400 font-normal text-[10px]">[{ev.universe}]</span> : '—')}
                      </td>
                      <td className="p-3 whitespace-nowrap">{getSeverityBadge(ev.severity)}</td>
                      <td className="p-3 text-slate-200 font-medium max-w-md truncate">
                        {ev.message}
                      </td>
                      <td className="p-3 text-center text-slate-400">
                        {ev.details ? (
                          isExpanded ? <ChevronDown size={14} className="mx-auto" /> : <ChevronRight size={14} className="mx-auto" />
                        ) : '—'}
                      </td>
                    </tr>
                    {isExpanded && ev.details && (
                      <tr className="bg-slate-950/70">
                        <td colSpan="8" className="p-4 pl-12">
                          <div className="bg-slate-900 border border-slate-800 rounded-xl p-3 font-mono text-[11px] text-slate-300 overflow-x-auto max-h-60">
                            <div className="text-[10px] uppercase font-bold text-slate-500 mb-1">JSON Event Telemetry:</div>
                            <pre className="text-emerald-400 leading-relaxed whitespace-pre-wrap">
                              {JSON.stringify(ev.details, null, 2)}
                            </pre>
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
      </div>
    </div>
  );
}

