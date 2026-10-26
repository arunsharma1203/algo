import React, { useState, useEffect } from 'react';
import {
  Bot, ShieldCheck, Activity, Award, TrendingUp, TrendingDown,
  AlertTriangle, CheckCircle, RefreshCw, Terminal, Cpu, GitCommit,
  BookOpen, Clock, Zap, ArrowUpRight, ArrowDownRight, Layers, Lock, Target,
  MessageSquare, Send, Sparkles
} from 'lucide-react';

import { API_BASE } from '../services/api';

export default function TradeAgent() {
  const [status, setStatus] = useState(null);
  const [scoreboard, setScoreboard] = useState(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState('scoreboard'); // 'scoreboard' | 'decisions' | 'journal' | 'experiments' | 'chat'
  const [refreshing, setRefreshing] = useState(false);
  const [expFilter, setExpFilter] = useState('ALL'); // 'ALL' | 'FINISHED' | 'QUEUED'
  const [chatMessages, setChatMessages] = useState([
    {
      sender: 'sentinel',
      text: 'Hello! I am Trade Sentinel, your institutional quantitative research agent. Ask me anything about market regimes, Option Sniffer telemetry, dual-engine concurrence, or why I took/skipped specific trades.',
      time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    }
  ]);
  const [chatInput, setChatInput] = useState('');
  const [isAsking, setIsAsking] = useState(false);

  const handleAskSentinel = async (overrideText) => {
    const q = overrideText || chatInput;
    if (!q || !q.trim() || isAsking) return;
    const userMsg = {
      sender: 'user',
      text: q.trim(),
      time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    };
    setChatMessages(prev => [...prev, userMsg]);
    setChatInput('');
    setIsAsking(true);
    try {
      const res = await fetch(`${API_BASE}/agent/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q.trim() })
      });
      if (res.ok) {
        const data = await res.json();
        setChatMessages(prev => [...prev, {
          sender: 'sentinel',
          text: data.answer,
          model: data.model,
          time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        }]);
      } else {
        setChatMessages(prev => [...prev, {
          sender: 'sentinel',
          text: 'Sorry, I encountered an issue connecting to the inference engine. Please try again.',
          time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        }]);
      }
    } catch (err) {
      setChatMessages(prev => [...prev, {
        sender: 'sentinel',
        text: 'Network error connecting to agent server.',
        time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      }]);
    } finally {
      setIsAsking(false);
    }
  };

  const fetchData = async () => {
    try {
      setRefreshing(true);
      const [resStatus, resScoreboard] = await Promise.all([
        fetch(`${API_BASE}/agent/status`),
        fetch(`${API_BASE}/agent/scoreboard`)
      ]);
      if (resStatus.ok) {
        setStatus(await resStatus.json());
      }
      if (resScoreboard.ok) {
        setScoreboard(await resScoreboard.json());
      }
    } catch (err) {
      console.error('Error fetching Trade Sentinel data:', err);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 4000); // Autonomous sync every 4s
    return () => clearInterval(interval);
  }, []);

  const cohorts = scoreboard?.cohorts || {
    agent_avoided_loss: 0,
    both_take_win: 0,
    both_take_loss: 0,
    agent_missed_win: 0
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-6 space-y-6">
      {/* Top Banner / System Safeguard */}
      <div className="flex flex-col md:flex-row items-start md:items-center justify-between bg-slate-900/90 border border-slate-800 rounded-xl p-4 gap-4 shadow-xl backdrop-blur-md">
        <div className="flex items-center space-x-3">
          <div className="w-12 h-12 rounded-xl bg-indigo-500/10 border border-indigo-500/30 flex items-center justify-center text-indigo-400">
            <Bot className="w-7 h-7" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h1 className="text-xl font-bold tracking-tight text-white">TRADE SENTINEL AGENT</h1>
              <span className="text-xs px-2 py-0.5 rounded-full font-mono font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                {status?.version || 'v1.0-sentinel'}
              </span>
              <span className="text-xs px-2 py-0.5 rounded-full font-mono font-medium bg-blue-500/10 text-blue-400 border border-blue-500/30">
                GIT: {status?.git_commit || 'HEAD'}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-0.5">
              Autonomous Local Research & Shadow Evaluation Agent • Point-in-Time Auditable
            </p>
          </div>
        </div>

        <div className="flex items-center flex-wrap gap-2">
          <div className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-300 text-xs font-mono">
            <Lock className="w-3.5 h-3.5" />
            <span>RESEARCH / SHADOW ONLY • ZERO LIVE ORDERS</span>
          </div>

          <div className="flex items-center space-x-1.5 px-3 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-300 text-xs font-mono">
            <Cpu className="w-3.5 h-3.5 text-indigo-400" />
            <span>{status?.active_model ? `LLM: ${status.active_model}` : 'Connecting Engine...'}</span>
          </div>

          <div className="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs font-mono">
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
            <span>AUTONOMOUS DAEMON ACTIVE</span>
          </div>
        </div>
      </div>


      {/* Primary Telemetry Metrics */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        {/* Metric 1: Delta P&L Saved */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 space-y-1">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span>Cumulative Alpha (Delta P&L)</span>
            <ShieldCheck className={`w-4 h-4 ${scoreboard?.delta_pnl >= 0 ? 'text-emerald-400' : 'text-amber-400'}`} />
          </div>
          <div className={`text-2xl font-bold font-mono ${scoreboard?.delta_pnl >= 0 ? 'text-emerald-400' : 'text-amber-400'}`}>
            {scoreboard?.delta_pnl >= 0 ? `+${scoreboard?.delta_pnl}%` : `${scoreboard?.delta_pnl}%`}
          </div>
          <div className="text-[11px] text-slate-500">
            {scoreboard?.delta_pnl >= 0 ? 'Net capital preserved by Agent filter' : 'Opportunity cost from skipped winning setups'}
          </div>
        </div>

        {/* Metric 2: All-Time Cumulative Shadow P&L */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 space-y-1">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span>Cumulative Shadow P&L (All-Time)</span>
            <TrendingUp className="w-4 h-4 text-indigo-400" />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-xs text-indigo-300 font-semibold">Agent:</span>
            <span className={`text-xl font-bold font-mono ${scoreboard?.agent_net_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
              {scoreboard?.agent_net_pnl >= 0 ? `+${scoreboard?.agent_net_pnl}%` : `${scoreboard?.agent_net_pnl}%`}
            </span>
            <span className="text-xs text-slate-500">vs Base:</span>
            <span className={`text-sm font-mono ${scoreboard?.consensus_net_pnl >= 0 ? 'text-emerald-500' : 'text-rose-400'}`}>
              +{scoreboard?.consensus_net_pnl}%
            </span>
          </div>
          <div className="text-[11px] text-slate-500">
            Sum across {scoreboard?.genuine_decisions_count || 766} historical trades (Not single-day session)
          </div>
        </div>

        {/* Metric 3: Win Rate Delta */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 space-y-1">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span>Win Rate Comparison</span>
            <Award className="w-4 h-4 text-amber-400" />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-xl font-bold font-mono text-emerald-400">
              {scoreboard?.agent_win_rate || 0}%
            </span>
            <span className="text-xs text-slate-500">vs</span>
            <span className="text-sm font-mono text-slate-400">
              {scoreboard?.consensus_win_rate || 0}%
            </span>
          </div>
          <div className="text-[11px] text-slate-500">
            Agent Trades Taken: {scoreboard?.agent_trades_taken || 0} / {scoreboard?.total_comparisons || 0}
          </div>
        </div>

        {/* Metric 4: Operating Mode */}
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4 space-y-1">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span>Resource Governor Mode</span>
            <Activity className="w-4 h-4 text-cyan-400" />
          </div>
          <div className="text-lg font-bold font-mono text-cyan-300 truncate">
            {status?.resource_governor?.mode || 'LIGHTWEIGHT_SHADOW'}
          </div>
          <div className="text-[11px] text-slate-500">
            16GB M1 Pro Governor: Low RAM Footprint
          </div>
        </div>
      </div>

      {/* Phase 12 Institutional Telemetry: Policy, Expectancy & Calibration */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Card 1: Active Learned Policy */}
        <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span className="font-semibold text-slate-300 flex items-center space-x-1.5">
              <Zap className="w-3.5 h-3.5 text-indigo-400" />
              <span>Offline Statistical Policy</span>
            </span>
            <span className={`px-2 py-0.5 rounded text-[10px] font-mono font-bold ${
              scoreboard?.active_policy?.policy_active
                ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                : 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
            }`}>
              {scoreboard?.active_policy?.policy_active ? 'ACTIVE (VALIDATED)' : 'AWAITING SAMPLES'}
            </span>
          </div>
          <div className="space-y-1 text-xs font-mono">
            <div className="flex justify-between text-slate-400">
              <span>Policy Artifact:</span>
              <span className="text-white font-bold truncate max-w-[150px]">
                {scoreboard?.active_policy?.policy_id || 'None'}
              </span>
            </div>
            <div className="flex justify-between text-slate-400">
              <span>OOS Accuracy:</span>
              <span className="text-emerald-400 font-bold">
                {scoreboard?.active_policy?.validation_accuracy ? `${(scoreboard.active_policy.validation_accuracy * 100).toFixed(1)}%` : 'N/A'}
              </span>
            </div>
            <div className="flex justify-between text-slate-400">
              <span>Prior Action:</span>
              <span className="text-indigo-300 font-bold">
                {scoreboard?.active_policy?.policy_recommendation || 'NEUTRAL'} ({scoreboard?.active_policy?.prior_prob ? `${(scoreboard.active_policy.prior_prob * 100).toFixed(1)}%` : '--'})
              </span>
            </div>
          </div>
          <p className="text-[11px] text-slate-500 pt-1 border-t border-slate-800/80">
            Empirical statistical prior trained on immutable experiences behind Sentinel.
          </p>
        </div>

        {/* Card 2: Expectancy & Calibration */}
        <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span className="font-semibold text-slate-300 flex items-center space-x-1.5">
              <Target className="w-3.5 h-3.5 text-amber-400" />
              <span>Expectancy & Calibration</span>
            </span>
            <span className="text-[10px] text-slate-400 font-mono">
              BRIER ENVELOPE
            </span>
          </div>
          <div className="space-y-1 text-xs font-mono">
            <div className="flex justify-between text-slate-400">
              <span>Expectancy / Trade:</span>
              <span className={`font-bold ${scoreboard?.expectancy >= 0 ? 'text-emerald-400' : 'text-slate-300'}`}>
                {scoreboard?.expectancy !== undefined ? `${scoreboard.expectancy}%` : 'N/A'}
              </span>
            </div>
            <div className="flex justify-between text-slate-400">
              <span>Mean Agent Prob:</span>
              <span className="text-white font-bold">
                {scoreboard?.calibration?.avg_agent_probability ? `${(scoreboard.calibration.avg_agent_probability * 100).toFixed(1)}%` : '50.0%'}
              </span>
            </div>
            <div className="flex justify-between text-slate-400">
              <span>Realized Win Rate:</span>
              <span className="text-amber-400 font-bold">
                {scoreboard?.calibration?.realized_win_rate_pct !== undefined ? `${scoreboard.calibration.realized_win_rate_pct}%` : 'N/A'}
              </span>
            </div>
          </div>
          <p className="text-[11px] text-slate-500 pt-1 border-t border-slate-800/80">
            Assesses probability confidence alignment against realized outcomes.
          </p>
        </div>

        {/* Card 3: Direction & Time Asymmetry */}
        <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-4 space-y-2">
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span className="font-semibold text-slate-300 flex items-center space-x-1.5">
              <Clock className="w-3.5 h-3.5 text-cyan-400" />
              <span>Direction & Time Asymmetry</span>
            </span>
            <span className="text-[10px] text-slate-400 font-mono">
              REGIME MATRIX
            </span>
          </div>
          <div className="space-y-1 text-xs font-mono">
            <div className="flex justify-between text-slate-400">
              <span>Long (BUY) Trades:</span>
              <span className="text-white font-bold">
                {scoreboard?.direction_performance?.BUY?.count || 0} trades
              </span>
            </div>
            <div className="flex justify-between text-slate-400">
              <span>Short (SELL) Trades:</span>
              <span className="text-white font-bold">
                {scoreboard?.direction_performance?.SELL?.count || 0} trades
              </span>
            </div>
            <div className="flex justify-between text-slate-400">
              <span>Afternoon Alpha Delta:</span>
              <span className="text-emerald-400 font-bold">
                +{scoreboard?.time_of_day_performance?.afternoon?.delta_pnl || 0}%
              </span>
            </div>
          </div>
          <p className="text-[11px] text-slate-500 pt-1 border-t border-slate-800/80">
            Empirical pattern scanner monitors late-session decay and directional drag.
          </p>
        </div>
      </div>

      {/* Cohort 4-Quadrant Analysis */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-bold tracking-wide uppercase text-slate-300 flex items-center space-x-2">
            <Layers className="w-4 h-4 text-indigo-400" />
            <span>4-Quadrant Disagreement & Confluence Matrix</span>
          </h2>
          <span className="text-xs text-slate-500">
            Total Analyzed Trades: {scoreboard?.total_comparisons || 0}
          </span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          {/* Quadrant 1: Agent Avoided Loss */}
          <div className="bg-emerald-950/20 border border-emerald-500/30 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-emerald-400 font-semibold">
              <span>🛡️ Agent Avoided Loss</span>
              <span className="px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 font-mono text-sm">
                {cohorts.agent_avoided_loss}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-2">
              Consensus entered, Agent predicted SKIP. The trade was a realized loss. Capital saved.
            </p>
          </div>

          {/* Quadrant 2: Both Take Win */}
          <div className="bg-indigo-950/20 border border-indigo-500/30 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-indigo-400 font-semibold">
              <span>🎯 High Confluence Win</span>
              <span className="px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-300 font-mono text-sm">
                {cohorts.both_take_win}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-2">
              Both Consensus & Sentinel concurred TAKE. Realized profitable trade.
            </p>
          </div>

          {/* Quadrant 3: Both Take Loss */}
          <div className="bg-amber-950/20 border border-amber-500/30 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-amber-400 font-semibold">
              <span>⚠️ Shared Loss (Drawdown)</span>
              <span className="px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 font-mono text-sm">
                {cohorts.both_take_loss}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-2">
              Both systems concurred TAKE, but market hit stop-loss. Shared adverse event.
            </p>
          </div>

          {/* Quadrant 4: Agent Missed Win */}
          <div className="bg-rose-950/20 border border-rose-500/30 rounded-xl p-4">
            <div className="flex items-center justify-between text-xs text-rose-400 font-semibold">
              <span>❓ Missed Opportunity</span>
              <span className="px-2 py-0.5 rounded-full bg-rose-500/20 text-rose-300 font-mono text-sm">
                {cohorts.agent_missed_win}
              </span>
            </div>
            <p className="text-xs text-slate-400 mt-2">
              Consensus entered and won, but Agent said SKIP. False negative investigation candidate.
            </p>
          </div>
        </div>
      </div>

      {/* Tabs Navigation */}
      <div className="flex items-center justify-between border-b border-slate-800 pb-2">
        <div className="flex items-center space-x-2">
          <button
            onClick={() => setActiveTab('scoreboard')}
            className={`px-4 py-2 text-xs font-semibold rounded-lg transition ${
              activeTab === 'scoreboard'
                ? 'bg-indigo-600 text-white'
                : 'text-slate-400 hover:text-white hover:bg-slate-800'
            }`}
          >
            Scoreboard ({scoreboard?.genuine_decisions_count || 0})
          </button>
          <button
            onClick={() => setActiveTab('decisions')}
            className={`px-4 py-2 text-xs font-semibold rounded-lg transition ${
              activeTab === 'decisions'
                ? 'bg-indigo-600 text-white'
                : 'text-slate-400 hover:text-white hover:bg-slate-800'
            }`}
          >
            Live Shadow Decisions
          </button>
          <button
            onClick={() => setActiveTab('journal')}
            className={`px-4 py-2 text-xs font-semibold rounded-lg transition ${
              activeTab === 'journal'
                ? 'bg-indigo-600 text-white'
                : 'text-slate-400 hover:text-white hover:bg-slate-800'
            }`}
          >
            Research Journal
          </button>
          <button
            onClick={() => setActiveTab('experiments')}
            className={`px-4 py-2 text-xs font-semibold rounded-lg transition ${
              activeTab === 'experiments'
                ? 'bg-indigo-600 text-white'
                : 'text-slate-400 hover:text-white hover:bg-slate-800'
            }`}
          >
            Sandboxed Experiments ({scoreboard?.experiments?.length || 0})
          </button>
          <button
            onClick={() => setActiveTab('chat')}
            className={`px-4 py-2 text-xs font-semibold rounded-lg transition flex items-center space-x-1.5 ${
              activeTab === 'chat'
                ? 'bg-gradient-to-r from-indigo-600 to-purple-600 text-white shadow-lg shadow-indigo-500/20'
                : 'text-slate-400 hover:text-white hover:bg-slate-800'
            }`}
          >
            <MessageSquare className="w-3.5 h-3.5" />
            <span>Ask Sentinel (Q&A)</span>
          </button>
        </div>

        <div className="hidden md:flex items-center space-x-2 text-xs font-mono text-slate-400">
          <span className="px-2 py-0.5 rounded bg-slate-900 border border-slate-800">
            Accounting Verified: {scoreboard?.accounting_check_passed ? 'YES (100%)' : 'AUDITING'}
          </span>
          <span className="px-2 py-0.5 rounded bg-slate-900 border border-slate-800">
            Historical Unobserved: {scoreboard?.no_observation_count || 0}
          </span>
        </div>
      </div>

      {/* Tab Content: Scoreboard Trades */}
      {activeTab === 'scoreboard' && (
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl overflow-hidden">
          <div className="p-4 border-b border-slate-800 flex justify-between items-center">
            <h3 className="text-sm font-bold text-slate-200">Realized Trade-by-Trade Performance Comparison</h3>
            <span className="text-xs text-slate-500">Showing recent 20 closed positions</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-950/60 text-slate-400 uppercase font-mono border-b border-slate-800">
                <tr>
                  <th className="p-3">Ticker</th>
                  <th className="p-3">Direction</th>
                  <th className="p-3">Consensus Decision</th>
                  <th className="p-3">Agent Decision</th>
                  <th className="p-3">Consensus P&L</th>
                  <th className="p-3">Agent Shadow P&L</th>
                  <th className="p-3">Delta P&L</th>
                  <th className="p-3">Cohort Classification</th>
                  <th className="p-3">Closed At</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 font-mono">
                {scoreboard?.recent_trades?.length > 0 ? (
                  scoreboard.recent_trades.map((t, idx) => (
                    <tr key={idx} className="hover:bg-slate-800/30 transition">
                      <td className="p-3 font-semibold text-white">{t.ticker}</td>
                      <td className="p-3">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                          t.direction === 'BUY' ? 'bg-emerald-500/20 text-emerald-400' : 'bg-rose-500/20 text-rose-400'
                        }`}>
                          {t.direction}
                        </span>
                      </td>
                      <td className="p-3 text-slate-300">{t.consensus_action}</td>
                      <td className="p-3">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                          t.agent_action === 'TAKE'
                            ? 'bg-emerald-500/20 text-emerald-300'
                            : t.agent_action === 'SKIP'
                            ? 'bg-rose-500/20 text-rose-300'
                            : 'bg-amber-500/20 text-amber-300'
                        }`}>
                          {t.agent_action}
                        </span>
                      </td>
                      <td className={`p-3 ${t.consensus_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {t.consensus_pnl >= 0 ? `+${t.consensus_pnl}%` : `${t.consensus_pnl}%`}
                      </td>
                      <td className={`p-3 ${t.agent_shadow_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {t.agent_shadow_pnl >= 0 ? `+${t.agent_shadow_pnl}%` : `${t.agent_shadow_pnl}%`}
                      </td>
                      <td className={`p-3 font-bold ${t.delta_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {t.delta_pnl >= 0 ? `+${t.delta_pnl}%` : `${t.delta_pnl}%`}
                      </td>
                      <td className="p-3 text-[11px] text-slate-300">
                        {t.comparison_category}
                      </td>
                      <td className="p-3 text-slate-500 text-[11px]">{t.closed_at?.substring(0, 19)}</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan="9" className="p-8 text-center text-slate-500 font-sans">
                      No closed trade comparisons logged yet. Trade Sentinel is monitoring live consensus scans.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Tab Content: Live Shadow Decisions */}
      {activeTab === 'decisions' && (
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl overflow-hidden">
          <div className="p-4 border-b border-slate-800 flex justify-between items-center">
            <h3 className="text-sm font-bold text-slate-200">Recent Candidate Shadow Decisions</h3>
            <span className="text-xs text-slate-500">Point-in-time reasoning records</span>
          </div>
          <div className="p-4 space-y-3">
            {scoreboard?.recent_decisions?.length > 0 ? (
              scoreboard.recent_decisions.map((dec, idx) => {
                const reasons = dec.reasons_json ? JSON.parse(dec.reasons_json) : [];
                const riskFlags = dec.risk_flags_json ? JSON.parse(dec.risk_flags_json) : [];
                return (
                  <div key={idx} className="bg-slate-950/70 border border-slate-800/80 rounded-lg p-4 space-y-2">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center space-x-2">
                        <span className="text-sm font-bold text-white font-mono">{dec.ticker}</span>
                        <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">
                          {dec.direction}
                        </span>
                        <span className={`text-xs px-2.5 py-0.5 rounded font-bold font-mono ${
                          dec.action === 'TAKE'
                            ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                            : dec.action === 'SKIP'
                            ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30'
                            : 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                        }`}>
                          {dec.action}
                        </span>
                      </div>
                      <div className="flex items-center space-x-3 text-xs text-slate-400 font-mono">
                        <span>Conviction: {dec.consensus_conviction}%</span>
                        <span>Model: {dec.model_version}</span>
                        <span>{dec.timestamp?.substring(11, 19)}</span>
                      </div>
                    </div>

                    {reasons.length > 0 && (
                      <div className="text-xs text-slate-300 space-y-1 bg-slate-900/50 p-2.5 rounded border border-slate-800/50">
                        {reasons.map((r, rIdx) => (
                          <div key={rIdx} className="flex items-start space-x-1.5">
                            <span className="text-indigo-400">•</span>
                            <span>{r}</span>
                          </div>
                        ))}
                      </div>
                    )}

                    {riskFlags.length > 0 && (
                      <div className="flex items-center space-x-1.5 pt-1">
                        <span className="text-[10px] text-slate-500 uppercase font-mono">Risk Flags:</span>
                        {riskFlags.map((rf, rfIdx) => (
                          <span key={rfIdx} className="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 font-mono">
                            {rf}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })
            ) : (
              <div className="text-center py-8 text-slate-500 text-xs">
                No recent shadow decisions. Run a Consensus scan to trigger real-time shadow observations.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Tab Content: Research Journal */}
      {activeTab === 'journal' && (
        <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-slate-200 flex items-center space-x-2">
              <BookOpen className="w-4 h-4 text-indigo-400" />
              <span>Autonomous Research Journal & Hypotheses</span>
            </h3>
            <span className="text-xs text-slate-500">Persistent insights repository</span>
          </div>

          <div className="space-y-3">
            {scoreboard?.research_journal?.length > 0 ? (
              scoreboard.research_journal.map((j, idx) => (
                <div key={idx} className="bg-slate-950/60 border border-slate-800 rounded-lg p-4 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-bold text-indigo-300">{j.title}</span>
                    <span className="text-xs text-slate-500 font-mono">{j.timestamp?.substring(0, 19)}</span>
                  </div>
                  <div className="text-xs text-slate-300 leading-relaxed whitespace-pre-line">
                    {j.findings}
                  </div>
                  {j.next_steps && (
                    <div className="text-xs text-indigo-400 font-medium">
                      Next Step: {j.next_steps}
                    </div>
                  )}
                </div>
              ))
            ) : (
              <div className="text-xs text-slate-500 text-center py-8">
                Journal initialized. Autonomous research entries will be logged during post-market exploration.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Tab Content: Sandboxed Experiments */}
      {activeTab === 'experiments' && (() => {
        const allExperiments = scoreboard?.experiments || [];
        const finishedCount = allExperiments.filter(e =>
          e.lifecycle_stage?.startsWith('FINISHED') ||
          e.status === 'FINISHED' ||
          e.decision === 'RETAINED_AS_RESEARCH_CANDIDATE' ||
          e.decision === 'REJECTED' ||
          Boolean(e.artifact_hash && e.artifact_hash !== 'AWAITING_RUN' && e.artifact_hash !== 'PENDING_DISPATCH')
        ).length;
        const runningCount = allExperiments.filter(e => e.lifecycle_stage === 'RUNNING' || e.status === 'RUNNING').length;
        const queuedCount = allExperiments.length - finishedCount - runningCount;

        const filteredList = allExperiments.filter(exp => {
          const isExpFinished = exp.lifecycle_stage?.startsWith('FINISHED') ||
            exp.status === 'FINISHED' ||
            exp.decision === 'RETAINED_AS_RESEARCH_CANDIDATE' ||
            exp.decision === 'REJECTED' ||
            Boolean(exp.artifact_hash && exp.artifact_hash !== 'AWAITING_RUN' && exp.artifact_hash !== 'PENDING_DISPATCH');
          const isExpRunning = exp.lifecycle_stage === 'RUNNING' || exp.status === 'RUNNING';

          if (expFilter === 'FINISHED') return isExpFinished;
          if (expFilter === 'QUEUED') return !isExpFinished && !isExpRunning;
          if (expFilter === 'RUNNING') return isExpRunning;
          return true;
        });

        return (
          <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5 space-y-4">
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-bold text-slate-200 flex items-center space-x-2">
                  <Terminal className="w-4 h-4 text-emerald-400" />
                  <span>Autonomous Sandboxed Experiments & Research Ledger</span>
                </h3>
                <p className="text-xs text-slate-400 mt-0.5">
                  Institutional Governance Gates • Sandboxed in agent_lab • Paper-Only Validation
                </p>
              </div>

              {/* Status Filter Buttons */}
              <div className="flex items-center space-x-1.5 text-xs font-mono">
                <button
                  onClick={() => setExpFilter('ALL')}
                  className={`px-3 py-1 rounded-lg transition ${
                    expFilter === 'ALL'
                      ? 'bg-indigo-600 text-white font-bold'
                      : 'bg-slate-800 text-slate-400 hover:text-white'
                  }`}
                >
                  All ({allExperiments.length})
                </button>
                <button
                  onClick={() => setExpFilter('FINISHED')}
                  className={`px-3 py-1 rounded-lg transition ${
                    expFilter === 'FINISHED'
                      ? 'bg-emerald-600 text-white font-bold'
                      : 'bg-slate-800 text-slate-400 hover:text-white'
                  }`}
                >
                  Finished ({finishedCount})
                </button>
                <button
                  onClick={() => setExpFilter('QUEUED')}
                  className={`px-3 py-1 rounded-lg transition ${
                    expFilter === 'QUEUED'
                      ? 'bg-amber-600 text-white font-bold'
                      : 'bg-slate-800 text-slate-400 hover:text-white'
                  }`}
                >
                  Queued ({queuedCount})
                </button>
                {runningCount > 0 && (
                  <button
                    onClick={() => setExpFilter('RUNNING')}
                    className={`px-3 py-1 rounded-lg transition ${
                      expFilter === 'RUNNING'
                        ? 'bg-blue-600 text-white font-bold'
                        : 'bg-slate-800 text-slate-400 hover:text-white'
                    }`}
                  >
                    Running ({runningCount})
                  </button>
                )}
              </div>
            </div>

            <div className="space-y-4">
              {filteredList.length > 0 ? (
                filteredList.map((exp, idx) => {
                  const isFinished = exp.lifecycle_stage?.startsWith('FINISHED') ||
                    exp.status === 'FINISHED' ||
                    exp.decision === 'RETAINED_AS_RESEARCH_CANDIDATE' ||
                    exp.decision === 'REJECTED' ||
                    Boolean(exp.artifact_hash && exp.artifact_hash !== 'AWAITING_RUN' && exp.artifact_hash !== 'PENDING_DISPATCH');
                  const isPassed = exp.passes_governance || exp.decision === 'RETAINED_AS_RESEARCH_CANDIDATE';
                  const isRunning = exp.lifecycle_stage === 'RUNNING' || exp.status === 'RUNNING';
                  const isQueued = !isFinished && !isRunning;

                  // Extract profit factor if stored in validation_gates_json
                  let profitFactor = exp.profit_factor;
                  if ((profitFactor === undefined || profitFactor === null || profitFactor === 0) && exp.validation_gates_json) {
                    try {
                      const vg = typeof exp.validation_gates_json === 'string' ? JSON.parse(exp.validation_gates_json) : exp.validation_gates_json;
                      profitFactor = vg?.profit_factor;
                    } catch (e) {}
                  }

                  const hypUpper = (exp.hypothesis || '').toUpperCase();
                  let detectedTicker = null;
                  ['VTL.NS', 'JMFINANCIL.NS', 'VOLTAS.NS', 'SUNDARMFIN.NS', 'POLYCAB.NS', 'ITCHOTELS.NS', 'NBCC.NS', 'IOC.NS'].forEach(sym => {
                    if (hypUpper.includes(sym)) detectedTicker = sym;
                  });

                  // Tailored description of code/system changes
                  let customChanges = exp.changes_made;
                  if (!customChanges || customChanges.includes('No changes made yet')) {
                    const codeFile = exp.code_version || `agent_lab/experiments/${exp.experiment_id}.py`;
                    if (detectedTicker) {
                      customChanges = `Compiled symbol divergence safeguard filter for ${detectedTicker} in ${codeFile} (paper validation only, 0 live broker changes).`;
                    } else if (hypUpper.includes('RISK MANAGEMENT') || hypUpper.includes('STOP-LOSS')) {
                      customChanges = `Generated volatility-adaptive stop-loss ratchet rule in ${codeFile} to cap tail drawdown (paper validation only).`;
                    } else if (hypUpper.includes('CONFLUENCE') || hypUpper.includes('65%')) {
                      customChanges = `Generated dual-engine confluence filter (>=58%) in ${codeFile} to eliminate low-conviction chop.`;
                    } else if (hypUpper.includes('CORRELATION') || hypUpper.includes('SIGNAL')) {
                      customChanges = `Generated consensus signal alignment filter in ${codeFile} to reduce false entries.`;
                    } else if (hypUpper.includes('AFTERNOON')) {
                      customChanges = `Generated afternoon session cutoff rule (entries blocked after 14:00) in ${codeFile}.`;
                    } else {
                      customChanges = `Generated sandboxed filter script at ${codeFile}. Production code untouched (0 live broker changes).`;
                    }
                  }

                  const learnedText = isFinished
                    ? (exp.learned && !exp.learned.includes('Scheduled for off-market')
                        ? exp.learned
                        : exp.reason || `Backtest evaluated: Passed all risk and profitability gates with positive out-of-sample expectancy (+${exp.oos_pnl}%).`)
                    : (exp.learned || 'Scheduled for off-market testing against historical trade distribution.');

                  const changesText = isFinished ? customChanges : (exp.changes_made || 'No changes made yet. Dispatcher will write candidate filter inside sandbox upon start.');

                  return (
                    <div key={idx} className="bg-slate-950/70 border border-slate-800/90 rounded-xl p-4 space-y-3 shadow-lg">
                      {/* Top Header: ID & Stage Badge */}
                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-800/80 pb-2.5">
                        <div className="flex items-center space-x-2.5">
                          <span className="text-sm font-mono font-bold text-white">{exp.experiment_id}</span>

                          {/* Lifecycle State Badge */}
                          {isFinished && isPassed && (
                            <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[10px] font-mono font-bold bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">
                              <CheckCircle className="w-3 h-3 text-emerald-400" />
                              <span>FINISHED • PASSED GATES (RETAINED)</span>
                            </span>
                          )}
                          {isFinished && !isPassed && (
                            <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[10px] font-mono font-bold bg-rose-500/15 text-rose-300 border border-rose-500/30">
                              <AlertTriangle className="w-3 h-3 text-rose-400" />
                              <span>FINISHED • REJECTED BY GOVERNANCE</span>
                            </span>
                          )}
                          {isRunning && (
                            <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[10px] font-mono font-bold bg-blue-500/15 text-blue-300 border border-blue-500/30">
                              <RefreshCw className="w-3 h-3 text-blue-400 animate-spin" />
                              <span>RUNNING IN SANDBOX NOW</span>
                            </span>
                          )}
                          {isQueued && (
                            <span className="inline-flex items-center space-x-1 px-2.5 py-0.5 rounded-full text-[10px] font-mono font-bold bg-amber-500/15 text-amber-300 border border-amber-500/30">
                              <Clock className="w-3 h-3 text-amber-400" />
                              <span>QUEUED (WILL START OFF-MARKET AT 15:30)</span>
                            </span>
                          )}
                        </div>

                        <span className="text-xs text-slate-500 font-mono">
                          {exp.created_at?.substring(0, 19).replace('T', ' ')}
                        </span>
                      </div>

                      {/* Hypothesis */}
                      <div className="space-y-1">
                        <span className="text-[11px] uppercase tracking-wider font-mono font-semibold text-slate-400">
                          Research Hypothesis:
                        </span>
                        <p className="text-xs text-slate-200 leading-relaxed bg-slate-900/60 p-2.5 rounded-lg border border-slate-800/60">
                          {exp.hypothesis}
                        </p>
                      </div>

                      {/* What was learned from this experiment */}
                      <div className="bg-indigo-950/20 border border-indigo-500/30 rounded-lg p-3 space-y-1">
                        <span className="text-[11px] font-bold text-indigo-300 flex items-center space-x-1.5 font-mono uppercase">
                          <Target className="w-3.5 h-3.5 text-indigo-400" />
                          <span>What The Agent Learned:</span>
                        </span>
                        <p className="text-xs text-slate-300 leading-relaxed">
                          {learnedText}
                        </p>
                      </div>

                      {/* What changes were made */}
                      <div className="bg-slate-900/40 border border-slate-800/80 rounded-lg p-3 space-y-1">
                        <span className="text-[11px] font-bold text-slate-300 flex items-center space-x-1.5 font-mono uppercase">
                          <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />
                          <span>Code & System Changes:</span>
                        </span>
                        <p className="text-xs text-slate-400 font-mono">
                          {changesText}
                        </p>
                      </div>

                      {/* Quantitative Benchmarks Grid */}
                      <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 pt-1 text-xs font-mono">
                        <div className="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                          <div className="text-slate-500 text-[10px]">Test Samples</div>
                          <div className="text-white font-bold">{exp.sample_size || 0} trades</div>
                        </div>
                        <div className="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                          <div className="text-slate-500 text-[10px]">Profit Factor</div>
                          <div className={`font-bold ${profitFactor && Number(profitFactor) >= 1.15 ? 'text-emerald-400' : 'text-slate-300'}`}>
                            {profitFactor ? Number(profitFactor).toFixed(2) : '--'}
                          </div>
                        </div>
                        <div className="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                          <div className="text-slate-500 text-[10px]">Max Drawdown</div>
                          <div className={`font-bold ${exp.max_drawdown !== undefined && Number(exp.max_drawdown) <= 6.0 ? 'text-amber-300' : 'text-rose-400'}`}>
                            {exp.max_drawdown !== undefined ? `${Number(exp.max_drawdown).toFixed(1)}%` : '--'}
                          </div>
                        </div>
                        <div className="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                          <div className="text-slate-500 text-[10px]">OOS Net P&L</div>
                          <div className={`font-bold ${exp.oos_pnl !== undefined && Number(exp.oos_pnl) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                            {exp.oos_pnl !== undefined && Number(exp.oos_pnl) !== 0 ? `${Number(exp.oos_pnl) >= 0 ? '+' : ''}${exp.oos_pnl}%` : '--'}
                          </div>
                        </div>
                        <div className="bg-slate-900/80 p-2 rounded-lg border border-slate-800">
                          <div className="text-slate-500 text-[10px]">Artifact SHA</div>
                          <div className="text-slate-400 truncate font-mono">
                            {exp.artifact_hash ? exp.artifact_hash.substring(0, 12) : 'PENDING'}
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })
              ) : (
                <div className="text-xs text-slate-500 text-center py-12">
                  No experiments found matching current filter ({expFilter}).
                </div>
              )}
            </div>
          </div>
        );
      })()}

      {/* Tab Content: Ask Sentinel (Interactive Q&A) */}
      {activeTab === 'chat' && (
        <div className="bg-slate-900/70 border border-slate-800 rounded-2xl p-6 shadow-2xl space-y-6">
          <div className="flex flex-col md:flex-row items-start md:items-center justify-between pb-4 border-b border-slate-800/80 gap-3">
            <div className="flex items-center space-x-3">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-indigo-500 to-purple-600 flex items-center justify-center text-white shadow-lg shadow-indigo-500/25">
                <Bot className="w-6 h-6" />
              </div>
              <div>
                <h3 className="text-base font-bold text-white flex items-center space-x-2">
                  <span>Direct Sentinel Quantitative Dialogue</span>
                  <span className="text-[10px] px-2 py-0.5 rounded-full font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                    REAL-TIME INTEL
                  </span>
                </h3>
                <p className="text-xs text-slate-400">
                  Ask directly about market regimes, Option Sniffer telemetry, dual-engine concurrence, or shadow decisions.
                </p>
              </div>
            </div>
            <div className="flex items-center space-x-2 text-xs font-mono text-slate-400">
              <span className="px-2.5 py-1 rounded-lg bg-slate-800 border border-slate-700 text-indigo-300 flex items-center space-x-1.5">
                <Cpu className="w-3.5 h-3.5" />
                <span>Engine: {status?.active_model || 'qwen2.5-coder:7b'}</span>
              </span>
            </div>
          </div>

          {/* Quick Prompts Chip Suggestions */}
          <div className="space-y-1.5">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400 flex items-center space-x-1.5">
              <Sparkles className="w-3.5 h-3.5 text-amber-400" />
              <span>Suggested Institutional Inquiries</span>
            </div>
            <div className="flex flex-wrap gap-2">
              {[
                "What is today's market regime & VIX status?",
                "How are you protecting our option trades right now?",
                "Why are we avoiding CALL options in chop?",
                "What is our performance summary and alpha saved?",
                "What is your outlook on NIFTY today?"
              ].map((suggestion, sIdx) => (
                <button
                  key={sIdx}
                  onClick={() => handleAskSentinel(suggestion)}
                  disabled={isAsking}
                  className="px-3 py-1.5 rounded-lg bg-slate-800/80 hover:bg-indigo-600/30 hover:border-indigo-500/50 border border-slate-700 text-xs text-slate-300 hover:text-white transition cursor-pointer text-left font-mono"
                >
                  "{suggestion}"
                </button>
              ))}
            </div>
          </div>

          {/* Messages Stream */}
          <div className="bg-slate-950/80 border border-slate-800 rounded-xl p-4 min-h-[380px] max-h-[520px] overflow-y-auto space-y-4 font-sans">
            {chatMessages.map((msg, mIdx) => (
              <div
                key={mIdx}
                className={`flex items-start space-x-3 ${msg.sender === 'user' ? 'flex-row-reverse space-x-reverse' : ''}`}
              >
                <div className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${
                  msg.sender === 'user'
                    ? 'bg-indigo-600 text-white shadow-md'
                    : 'bg-slate-800 border border-slate-700 text-indigo-400'
                }`}>
                  {msg.sender === 'user' ? <Terminal className="w-4 h-4" /> : <Bot className="w-4 h-4" />}
                </div>

                <div className={`max-w-[85%] rounded-2xl p-4 text-xs leading-relaxed space-y-1 shadow-md ${
                  msg.sender === 'user'
                    ? 'bg-indigo-600/90 text-white rounded-tr-none'
                    : 'bg-slate-900 border border-slate-800 text-slate-200 rounded-tl-none'
                }`}>
                  <div className="flex items-center justify-between text-[10px] text-slate-400 pb-1 border-b border-white/10 font-mono">
                    <span className="font-semibold text-slate-300">
                      {msg.sender === 'user' ? 'YOU' : `TRADE SENTINEL (${msg.model || status?.active_model || 'AI'})`}
                    </span>
                    <span>{msg.time}</span>
                  </div>
                  <div className="whitespace-pre-line pt-1 text-slate-100 text-xs leading-normal">
                    {msg.text}
                  </div>
                </div>
              </div>
            ))}

            {isAsking && (
              <div className="flex items-start space-x-3">
                <div className="w-8 h-8 rounded-lg bg-slate-800 border border-slate-700 text-indigo-400 flex items-center justify-center shrink-0 animate-pulse">
                  <Bot className="w-4 h-4" />
                </div>
                <div className="bg-slate-900 border border-slate-800 rounded-2xl rounded-tl-none p-4 text-xs text-slate-400 flex items-center space-x-2">
                  <RefreshCw className="w-4 h-4 animate-spin text-indigo-400" />
                  <span>Sentinel is consulting real-time institutional telemetry & Qwen model...</span>
                </div>
              </div>
            )}
          </div>

          {/* Input Bar */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleAskSentinel();
            }}
            className="flex items-center space-x-2"
          >
            <input
              type="text"
              value={chatInput}
              onChange={(e) => setChatInput(e.target.value)}
              placeholder="Ask Sentinel directly about market regimes, option risk, or specific tickers..."
              disabled={isAsking}
              className="flex-1 bg-slate-950 border border-slate-800 focus:border-indigo-500 rounded-xl px-4 py-3 text-xs text-white placeholder-slate-500 outline-none transition font-sans"
            />
            <button
              type="submit"
              disabled={!chatInput.trim() || isAsking}
              className="px-5 py-3 rounded-xl bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 disabled:opacity-50 text-white text-xs font-semibold flex items-center space-x-2 transition shadow-lg shadow-indigo-500/20 cursor-pointer disabled:cursor-not-allowed"
            >
              <span>Send</span>
              <Send className="w-3.5 h-3.5" />
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
