import React, { useState, useEffect, useCallback } from 'react';
import {
  Radio,
  TrendingUp,
  TrendingDown,
  ShieldAlert,
  Zap,
  Activity,
  Cpu,
  BarChart2,
  RefreshCw,
  Clock,
  CheckCircle2,
  AlertTriangle,
  Send,
  Eye,
  Sliders,
  Award,
  Layers,
  DollarSign,
  Info,
  Target,
  Shield
} from 'lucide-react';
import { API_BASE } from '../services/api';

export default function OptionSniffer() {
  const [activeTab, setActiveTab] = useState('signals'); // 'signals' | 'candidates' | 'trades' | 'pnl' | 'calibration' | 'health'
  const [statusData, setStatusData] = useState(null);
  const [candidates, setCandidates] = useState([]);
  const [signals, setSignals] = useState([]);
  const [paperTrades, setPaperTrades] = useState({ open_trades: [], closed_trades: [], pnl_summary: {} });
  const [calibrationData, setCalibrationData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [underlyingFilter, setUnderlyingFilter] = useState('ALL'); // 'ALL' | 'NIFTY' | 'BANKNIFTY'
  const [lastUpdated, setLastUpdated] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      const [statusRes, candRes, sigRes, tradesRes, calibRes] = await Promise.all([
        fetch(`${API_BASE}/option-sniffer/status`),
        fetch(`${API_BASE}/option-sniffer/candidates`),
        fetch(`${API_BASE}/option-sniffer/signals?limit=50`),
        fetch(`${API_BASE}/option-sniffer/paper-trades`),
        fetch(`${API_BASE}/option-sniffer/calibration`),
      ]);

      if (statusRes.ok) setStatusData(await statusRes.json());
      if (candRes.ok) setCandidates(await candRes.json());
      if (sigRes.ok) setSignals(await sigRes.json());
      if (tradesRes.ok) setPaperTrades(await tradesRes.json());
      if (calibRes.ok) setCalibrationData(await calibRes.json());

      setLastUpdated(new Date().toLocaleTimeString());
    } catch (err) {
      console.error('Failed to fetch option sniffer data:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 4000); // Polling every 4 seconds for reactive UI
    return () => clearInterval(interval);
  }, [fetchData]);

  const handleManualScan = async () => {
    setScanning(true);
    try {
      await fetch(`${API_BASE}/option-sniffer/scan`, { method: 'POST' });
      await fetchData();
    } catch (err) {
      console.error('Manual scan failed:', err);
    } finally {
      setScanning(false);
    }
  };

  const pnl = paperTrades.pnl_summary || {};
  const feedHealth = statusData?.feed_health || {};
  const isConnected = feedHealth.websocket_connected;

  const filteredCandidates = candidates.filter(c => {
    if (underlyingFilter === 'ALL') return true;
    return c.underlying === underlyingFilter;
  });

  return (
    <div className="space-y-6 max-w-7xl mx-auto pb-12 font-sans">
      {/* ── TOP BANNER: STRICT PAPER-ONLY NOTICE ────────────────────────── */}
      <div className="bg-gradient-to-r from-amber-500/10 via-amber-500/5 to-transparent border border-amber-500/30 rounded-2xl p-4 flex flex-col sm:flex-row items-center justify-between gap-3 shadow-xs">
        <div className="flex items-center space-x-3">
          <div className="w-10 h-10 rounded-xl bg-amber-500/20 text-amber-500 flex items-center justify-center shrink-0 border border-amber-500/30">
            <Radio size={20} className="animate-pulse" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <span className="text-sm font-black text-slate-900 tracking-wide">OPTION SNIFFER SUBSYSTEM</span>
              <span className="text-[10px] font-black uppercase px-2 py-0.5 rounded-full bg-amber-500 text-white tracking-widest shadow-xs">
                PAPER ONLY
              </span>
              <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-800 border border-emerald-300">
                ZERO LIVE ORDERS
              </span>
            </div>
            <p className="text-xs text-slate-500 mt-0.5">
              Dedicated Option Buying Research & Surveillance Engine. Powered by Upstox WebSocket V3 and 4-Stage Option Alpha Model.
            </p>
          </div>
        </div>

        <div className="flex items-center space-x-2 shrink-0">
          <button
            onClick={handleManualScan}
            disabled={scanning}
            className={`flex items-center space-x-1.5 px-3.5 py-2 rounded-xl text-xs font-bold text-white transition-all shadow-sm ${
              scanning
                ? 'bg-slate-400 cursor-not-allowed'
                : 'bg-indigo-600 hover:bg-indigo-700 active:scale-95'
            }`}
          >
            <RefreshCw size={13} className={scanning ? 'animate-spin' : ''} />
            <span>{scanning ? 'Sweeping Chain...' : 'Trigger Chain Sweep'}</span>
          </button>
        </div>
      </div>

      {/* ── SECTION 1: TOP EXECUTIVE METRICS CARDS ─────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {/* Card 1: Live Stream Feed */}
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Feed Stream</span>
            <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-emerald-500 animate-ping' : 'bg-rose-500'}`} />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className={`text-xl font-black ${isConnected ? 'text-emerald-700' : 'text-slate-700'}`}>
              {isConnected ? 'LIVE FEED' : 'STANDBY'}
            </span>
            <span className="text-xs text-slate-400 font-mono">V3 Full</span>
          </div>
          <div className="text-[11px] text-slate-500 mt-2 font-mono flex items-center justify-between">
            <span>Subscribed: {feedHealth.subscribed_keys_count || candidates.length} keys</span>
            <span>Age: {feedHealth.last_message_latency_ms ? `${feedHealth.last_message_latency_ms}ms` : '—'}</span>
          </div>
        </div>

        {/* Card 2: Model & Quality Hurdle */}
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Option Alpha Model</span>
            <span className="text-[10px] font-mono font-bold bg-indigo-50 text-indigo-700 px-1.5 py-0.5 rounded border border-indigo-200">
              STAGE 4
            </span>
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-xl font-black text-slate-900">58.0%</span>
            <span className="text-xs text-slate-500">Conviction Hurdle</span>
          </div>
          <div className="text-[11px] text-slate-500 mt-2 font-mono flex items-center justify-between">
            <span>Net Hurdle: +3.0%</span>
            <span>Brier: {calibrationData?.calibration_stats?.brier_score ?? '0.23'}</span>
          </div>
        </div>

        {/* Card 3: Today's Paper Net P&L */}
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Today's Paper P&L</span>
            <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded font-bold ${
              (pnl.today_total_net_pnl ?? pnl.today_net_pnl ?? 0) >= 0 ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-rose-50 text-rose-700 border border-rose-200'
            }`}>
              {(pnl.unrealized_net_pnl || 0) !== 0 ? 'LIVE MTM' : 'NET FRICTION'}
            </span>
          </div>
          <div className="flex items-baseline space-x-2">
            <span className={`text-xl font-black ${
              (pnl.today_total_net_pnl ?? pnl.today_net_pnl ?? 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'
            }`}>
              {(pnl.today_total_net_pnl ?? pnl.today_net_pnl ?? 0) >= 0 ? '+' : ''}₹{(pnl.today_total_net_pnl ?? pnl.today_net_pnl ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </span>
            <span className="text-xs text-slate-400 font-mono">
              ({pnl.today_wins || 0}/{pnl.today_closed_trades || 0} closed)
            </span>
          </div>
          <div className="text-[11px] text-slate-500 mt-2 font-mono flex items-center justify-between">
            <span>MTM: ₹{(pnl.unrealized_net_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })} ({paperTrades.open_trades?.length || 0} open)</span>
            <span>Realized: ₹{(pnl.today_realized_net_pnl ?? pnl.today_net_pnl ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</span>
          </div>
        </div>

        {/* Card 4: Active Positions & Signals */}
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Surveillance State</span>
            <span className="text-[10px] font-mono text-slate-400">10 Gates</span>
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-xl font-black text-indigo-700">
              {paperTrades.open_trades?.length || 0} Open
            </span>
            <span className="text-xs text-slate-500">/ 3 Max Trades</span>
          </div>
          <div className="text-[11px] text-slate-500 mt-2 font-mono flex items-center justify-between">
            <span>Signals: {signals.length} total</span>
            <span>Last Sync: {lastUpdated || '—'}</span>
          </div>
        </div>
      </div>

      {/* ── SECTION 2: TAB NAVIGATION ──────────────────────────────────── */}
      <div className="border-b border-slate-200 flex items-center justify-between overflow-x-auto pb-px">
        <div className="flex space-x-2">
          {[
            { id: 'signals', label: 'Active Signals & Alerts', icon: Zap, count: signals.filter(s => s.status === 'ACTIVE').length },
            { id: 'candidates', label: 'Watch Candidates (ATM)', icon: Eye, count: candidates.length },
            { id: 'trades', label: 'Paper Positions & P&L', icon: DollarSign, count: paperTrades.open_trades?.length },
            { id: 'calibration', label: 'Model Calibration & Ablation', icon: Layers },
            { id: 'health', label: 'Feed Health & Diagnostics', icon: Activity },
          ].map(tab => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center space-x-2 px-4 py-3 text-xs font-bold border-b-2 transition-all whitespace-nowrap ${
                  isActive
                    ? 'border-indigo-600 text-indigo-600 bg-indigo-50/40 rounded-t-xl'
                    : 'border-transparent text-slate-500 hover:text-slate-800 hover:border-slate-300'
                }`}
              >
                <Icon size={14} className={isActive ? 'text-indigo-600' : 'text-slate-400'} />
                <span>{tab.label}</span>
                {typeof tab.count === 'number' && tab.count > 0 && (
                  <span className={`text-[10px] px-1.5 py-0.2 rounded-full font-mono ${
                    isActive ? 'bg-indigo-600 text-white' : 'bg-slate-200 text-slate-700'
                  }`}>
                    {tab.count}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {activeTab === 'candidates' && (
          <div className="flex items-center space-x-1.5 pb-2">
            {['ALL', 'NIFTY', 'BANKNIFTY'].map(u => (
              <button
                key={u}
                onClick={() => setUnderlyingFilter(u)}
                className={`text-[10px] font-bold px-2.5 py-1 rounded-lg transition-all ${
                  underlyingFilter === u
                    ? 'bg-slate-900 text-white'
                    : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                }`}
              >
                {u}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* ── SECTION 3: TAB PANELS ──────────────────────────────────────── */}

      {/* TAB 1: ACTIVE SIGNALS & ALERTS */}
      {activeTab === 'signals' && (
        <div className="space-y-4">
          {signals.length === 0 ? (
            <div className="bg-white border border-slate-200 rounded-2xl p-12 text-center text-xs text-slate-500 space-y-2 shadow-xs">
              <div className="w-12 h-12 rounded-full bg-slate-100 flex items-center justify-center mx-auto text-slate-400">
                <ShieldAlert size={24} />
              </div>
              <h3 className="text-sm font-bold text-slate-800">Zero Active Option Signals</h3>
              <p className="max-w-md mx-auto text-slate-500">
                The 10 quality gates are currently filtering out non-actionable setups. Signals trigger strictly when calibrated net probability &ge; 58% and expected net return &ge; +3.0% after statutory costs.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {signals.map((sig, idx) => {
                const isBuyCall = sig.direction === 'BUY_CE';
                const isActive = sig.status === 'ACTIVE';
                return (
                  <div
                    key={idx}
                    className={`bg-white border rounded-2xl p-4.5 shadow-xs transition-all ${
                      isActive ? 'border-indigo-300 ring-2 ring-indigo-500/10' : 'border-slate-200 opacity-80'
                    }`}
                  >
                    <div className="flex items-center justify-between mb-3">
                      <div className="flex items-center space-x-2">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-black tracking-wide ${
                          isBuyCall ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                        }`}>
                          {isBuyCall ? 'CALL BUY' : 'PUT BUY'}
                        </span>
                        <span className="font-black text-slate-900 text-sm">{sig.trading_symbol}</span>
                      </div>
                      <span className={`text-[10px] font-mono px-2 py-0.5 rounded-full font-bold ${
                        isActive
                          ? 'bg-indigo-100 text-indigo-800 border border-indigo-200'
                          : 'bg-slate-100 text-slate-600'
                      }`}>
                        {sig.status}
                      </span>
                    </div>

                    <div className="grid grid-cols-3 gap-2 bg-slate-50 p-2.5 rounded-xl border border-slate-200/60 font-mono text-xs mb-3">
                      <div>
                        <span className="text-[10px] text-slate-400 block uppercase">Paper Entry</span>
                        <strong className="text-slate-800">₹{sig.option_ask?.toFixed(2)}</strong> (ASK)
                      </div>
                      <div>
                        <span className="text-[10px] text-slate-400 block uppercase">Spot Reference</span>
                        <strong className="text-slate-800">₹{sig.spot_price?.toFixed(2)}</strong>
                      </div>
                      <div>
                        <span className="text-[10px] text-slate-400 block uppercase">Spread %</span>
                        <strong className="text-slate-800">{sig.spread_pct?.toFixed(2)}%</strong>
                      </div>
                    </div>

                    <div className="grid grid-cols-4 gap-2 text-[11px] font-mono text-slate-600 mb-3">
                      <div>Delta: <strong>{sig.delta?.toFixed(2)}</strong></div>
                      <div>IV: <strong>{sig.iv?.toFixed(1)}%</strong></div>
                      <div>Vol: <strong>{sig.volume?.toLocaleString()}</strong></div>
                      <div>OI: <strong>{sig.oi ? Math.round(sig.oi).toLocaleString() : '—'}</strong></div>
                    </div>

                    <div className="flex items-center justify-between pt-2 border-t border-slate-100 text-[11px]">
                      <div className="flex items-center space-x-2">
                        <span className="text-indigo-600 font-bold font-mono">
                          P(Net Win): {(sig.calibrated_prob * 100).toFixed(1)}%
                        </span>
                        <span className="text-slate-300">•</span>
                        <span className="text-emerald-700 font-bold font-mono">
                          Exp Net: +{sig.expected_net_return?.toFixed(1)}%
                        </span>
                      </div>
                      {sig.telegram_sent === 1 && (
                        <span className="flex items-center text-[10px] text-emerald-600 font-mono font-bold">
                          <Send size={11} className="mr-1" /> Telegram Alerted
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* TAB 2: WATCH CANDIDATES (ATM GRID) */}
      {activeTab === 'candidates' && (
        <div className="bg-white border border-slate-200 rounded-2xl shadow-xs overflow-hidden">
          <div className="px-5 py-3.5 bg-slate-50 border-b border-slate-200 flex items-center justify-between">
            <div>
              <h3 className="text-xs font-black text-slate-900 uppercase tracking-wide">
                Controlled ATM Strike Universe ({filteredCandidates.length} Contracts Monitored)
              </h3>
              <p className="text-[11px] text-slate-500 mt-0.5">
                ATM &plusmn; 5 strikes around spot price. Continuous live Upstox WebSocket V3 streaming.
              </p>
            </div>
            <span className="text-[11px] font-mono text-slate-400">Order Book Top-of-Book</span>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs font-mono">
              <thead>
                <tr className="bg-slate-100/60 text-[10px] text-slate-500 uppercase tracking-wider border-b border-slate-200">
                  <th className="py-2.5 px-4 font-bold">Contract</th>
                  <th className="py-2.5 px-3 font-bold">Type</th>
                  <th className="py-2.5 px-3 font-bold text-right">Strike</th>
                  <th className="py-2.5 px-3 font-bold text-right">LTP</th>
                  <th className="py-2.5 px-3 font-bold text-right">Bid (Qty)</th>
                  <th className="py-2.5 px-3 font-bold text-right">Ask (Qty)</th>
                  <th className="py-2.5 px-3 font-bold text-right">Spread %</th>
                  <th className="py-2.5 px-3 font-bold text-right">Delta</th>
                  <th className="py-2.5 px-3 font-bold text-right">IV %</th>
                  <th className="py-2.5 px-3 font-bold text-right">OI</th>
                  <th className="py-2.5 px-4 font-bold text-center">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-slate-700">
                {filteredCandidates.map((c, idx) => {
                  const q = c.quote || {};
                  const isCall = c.option_type === 'CE';
                  return (
                    <tr key={idx} className="hover:bg-slate-50/80 transition-colors">
                      <td className="py-2.5 px-4 font-bold text-slate-900">{c.trading_symbol}</td>
                      <td className="py-2.5 px-3">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                          isCall ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                        }`}>
                          {c.option_type}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-right font-bold">{c.strike}</td>
                      <td className="py-2.5 px-3 text-right font-bold text-slate-900">
                        ₹{q.ltp ? q.ltp.toFixed(2) : '—'}
                      </td>
                      <td className="py-2.5 px-3 text-right text-emerald-700">
                        ₹{q.bid ? q.bid.toFixed(2) : '—'} <span className="text-[10px] text-slate-400">({q.bid_qty || 0})</span>
                      </td>
                      <td className="py-2.5 px-3 text-right text-rose-700">
                        ₹{q.ask ? q.ask.toFixed(2) : '—'} <span className="text-[10px] text-slate-400">({q.ask_qty || 0})</span>
                      </td>
                      <td className="py-2.5 px-3 text-right">
                        <span className={`${(q.spread_pct || 0) > 3.5 ? 'text-rose-600 font-bold' : 'text-slate-600'}`}>
                          {q.spread_pct ? `${q.spread_pct.toFixed(2)}%` : '—'}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 text-right">{q.delta ? q.delta.toFixed(2) : '—'}</td>
                      <td className="py-2.5 px-3 text-right">{q.iv ? `${q.iv.toFixed(1)}%` : '—'}</td>
                      <td className="py-2.5 px-3 text-right">{q.oi ? Math.round(q.oi).toLocaleString() : '—'}</td>
                      <td className="py-2.5 px-4 text-center">
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${
                          q.is_valid
                            ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                            : 'bg-slate-100 text-slate-500'
                        }`}>
                          {q.is_valid ? 'QUALIFIED' : 'WATCH'}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* TAB 3: PAPER POSITIONS & P&L */}
      {activeTab === 'trades' && (
        <div className="space-y-6">
          {/* Active Open Paper Positions */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs">
            <div className="flex items-center justify-between mb-4 border-b border-slate-100 pb-3">
              <div className="flex items-center space-x-2.5">
                <div className="w-7 h-7 rounded-lg bg-indigo-50 border border-indigo-200 flex items-center justify-center text-indigo-600">
                  <Activity size={15} />
                </div>
                <div>
                  <h3 className="text-sm font-bold text-slate-900">
                    Active Open Paper Positions ({paperTrades.open_trades?.length || 0})
                  </h3>
                  <p className="text-[11px] text-slate-400 font-mono">
                    Real-Time Mark-to-Market Valuation · Exit Liquidity Strictly Executed at BID
                  </p>
                </div>
              </div>
              <div className="flex items-center space-x-2">
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-mono font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-ping mr-1.5" />
                  Live Order Book Active
                </span>
              </div>
            </div>

            {(!paperTrades.open_trades || paperTrades.open_trades.length === 0) ? (
              <div className="text-center py-8 text-xs text-slate-400 font-mono">
                No open paper positions. Engine enters strictly when model P &ge; 44% and Net Return &ge; +2.0%.
              </div>
            ) : (
              <div className="space-y-4">
                {paperTrades.open_trades.map((tr, idx) => {
                  const entryPrice = tr.entry_ask || tr.entry_price_used || 0;
                  const currentBid = tr.current_bid || entryPrice;
                  const currentLtp = tr.current_ltp || currentBid;
                  const netPnl = tr.unrealized_net_pnl ?? 0;
                  const isProfit = netPnl >= 0;
                  const pnlPct = tr.unrealized_pnl_pct ?? 0;
                  const netPnlPct = tr.unrealized_net_pnl_pct ?? 0;
                  const profitDistPct = Math.min(Math.max(pnlPct, 0), 35.0);
                  const lossDistPct = Math.min(Math.max(-pnlPct, 0), 20.0);
                  const holdingMins = Math.floor((tr.holding_duration_seconds || 0) / 60);

                  return (
                    <div key={idx} className="bg-slate-50/80 border border-slate-200 rounded-xl p-4 space-y-3.5 font-mono shadow-2xs">
                      {/* Row 1: Contract Header & Live MTM Value */}
                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 pb-2 border-b border-slate-200/60">
                        <div className="flex items-center space-x-2.5">
                          <span className={`px-2 py-0.5 rounded text-[11px] font-black ${
                            tr.option_type === 'CE'
                              ? 'bg-blue-100 text-blue-800 border border-blue-200'
                              : 'bg-amber-100 text-amber-800 border border-amber-200'
                          }`}>
                            {tr.option_type}
                          </span>
                          <div>
                            <span className="font-bold text-slate-900 text-sm">{tr.trading_symbol}</span>
                            <div className="text-[11px] text-slate-500 mt-0.5 flex items-center space-x-2">
                              <span>Qty: <strong className="text-slate-700">{tr.quantity}</strong></span>
                              <span>•</span>
                              <span>Entry ASK: <strong className="text-slate-700">₹{entryPrice.toFixed(2)}</strong></span>
                              <span>•</span>
                              <span>Holding: <strong className="text-slate-700">{holdingMins}m / 45m</strong></span>
                            </div>
                          </div>
                        </div>

                        {/* Live Quotes & Unrealized PnL Pill */}
                        <div className="flex items-center space-x-3 text-right">
                          <div>
                            <div className="text-[10px] text-slate-400 font-semibold flex items-center justify-end gap-1">
                              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                              <span>LIVE BID</span>
                            </div>
                            <div className="text-sm font-black text-slate-900">
                              ₹{currentBid.toFixed(2)}
                            </div>
                            <div className="text-[10px] text-slate-400">
                              LTP: ₹{currentLtp.toFixed(2)}
                            </div>
                          </div>

                          <div className={`px-3 py-1.5 rounded-xl border flex flex-col items-end ${
                            isProfit
                              ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-700'
                              : 'bg-rose-500/10 border-rose-500/30 text-rose-700'
                          }`}>
                            <span className="text-[10px] uppercase font-bold tracking-wider">Unrealized Net</span>
                            <span className="text-sm font-black">
                              {isProfit ? '+' : ''}₹{netPnl.toFixed(2)}
                            </span>
                            <span className="text-[10px] font-semibold">
                              {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}% ({netPnlPct >= 0 ? '+' : ''}{netPnlPct.toFixed(2)}% net)
                            </span>
                          </div>
                        </div>
                      </div>

                      {/* Row 2: Target & Stop Loss Levels with Visual Payoff Progress Bar */}
                      <div className="space-y-1.5 pt-0.5">
                        <div className="flex items-center justify-between text-[11px] font-bold">
                          <span className="text-rose-600 flex items-center space-x-1">
                            <Shield size={12} />
                            <span>SL: ₹{(tr.stop_loss_price || (entryPrice * 0.8)).toFixed(2)} (-{tr.stop_loss_pct || 20}%)</span>
                          </span>
                          <span className="text-slate-400 font-semibold flex items-center gap-1 text-[10px]">
                            <span>Payoff Track</span>
                            <span className="font-mono text-slate-500">
                              ({isProfit ? `+${profitDistPct.toFixed(1)}% to TP` : `-${lossDistPct.toFixed(1)}% to SL`})
                            </span>
                          </span>
                          <span className="text-emerald-600 flex items-center space-x-1">
                            <Target size={12} />
                            <span>TGT: ₹{(tr.target_price || (entryPrice * 1.35)).toFixed(2)} (+{tr.target_pct || 35}%)</span>
                          </span>
                        </div>

                        {/* Centered Bidirectional Gauge: Middle is Entry (50%) */}
                        <div className="w-full bg-slate-200/90 h-3 rounded-full overflow-hidden relative shadow-inner">
                          {/* Center zero line marker (Entry) */}
                          <div className="absolute left-1/2 top-0 bottom-0 w-[2px] bg-slate-500 z-10 -translate-x-1/2" title="Entry Price" />

                          {/* Profit bar extending from center (50%) to the right */}
                          {isProfit && (
                            <div
                              className="absolute top-0 bottom-0 left-1/2 h-full transition-all duration-500 rounded-r-full"
                              style={{
                                width: `${(profitDistPct / 35.0) * 50}%`,
                                background: 'linear-gradient(to right, #10b981, #047857)'
                              }}
                            />
                          )}

                          {/* Loss bar extending from center (50%) to the left */}
                          {!isProfit && (
                            <div
                              className="absolute top-0 bottom-0 right-1/2 h-full transition-all duration-500 rounded-l-full"
                              style={{
                                width: `${(lossDistPct / 20.0) * 50}%`,
                                background: 'linear-gradient(to left, #f43f5e, #9f1239)'
                              }}
                            />
                          )}
                        </div>

                        <div className="flex items-center justify-between text-[10px] text-slate-400 font-medium">
                          <span className="text-rose-600/80">
                            {(tr.sl_distance_pct || 0) > 0 ? `+${tr.sl_distance_pct}% buffer` : 'Breached SL'}
                          </span>
                          <span className="text-slate-500 font-mono text-[9px]">ENTRY @ ₹{entryPrice.toFixed(2)}</span>
                          <span className="text-emerald-600/80">
                            {(tr.tp_distance_pct || 0) > 0 ? `${tr.tp_distance_pct}% to Target` : 'Target Reached'}
                          </span>
                        </div>
                      </div>

                      {/* Row 3: Friction, Trailing Ratchet & Extrema Badges */}
                      <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] bg-white border border-slate-200/80 rounded-lg px-3 py-2 text-slate-600">
                        {/* Trailing Stop Status */}
                        <div className="flex items-center space-x-1.5">
                          <span className="font-bold uppercase text-slate-400 text-[9px]">Trailing Ratchet:</span>
                          {tr.trailing_stop_active ? (
                            <span className="font-bold text-amber-700 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded">
                              ARMED @ ₹{tr.trailing_stop_price?.toFixed(2)} (-10% from peak)
                            </span>
                          ) : (
                            <span className="text-slate-500">
                              Arms at +15% MFE (Peak: +{tr.mfe_pct || 0}%)
                            </span>
                          )}
                        </div>

                        {/* Friction */}
                        <div className="flex items-center space-x-1.5">
                          <span className="text-slate-400">Gross: ₹{tr.unrealized_gross_pnl?.toFixed(2) || '0.00'}</span>
                          <span>•</span>
                          <span className="text-slate-400">Friction: ₹{tr.unrealized_charges?.toFixed(2) || '0.00'}</span>
                        </div>

                        {/* Peak MFE / Trough MAE */}
                        <div className="flex items-center space-x-2 font-mono">
                          <span className="text-emerald-700 font-bold">MFE: +{tr.mfe_pct || 0}%</span>
                          <span className="text-slate-300">|</span>
                          <span className="text-rose-700 font-bold">MAE: {tr.mae_pct || 0}%</span>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Closed Paper Trade History */}
          <div className="bg-white border border-slate-200 rounded-2xl shadow-xs overflow-hidden">
            <div className="px-5 py-3.5 bg-slate-50 border-b border-slate-200 flex items-center justify-between">
              <h3 className="text-xs font-black text-slate-900 uppercase tracking-wide">
                Closed Paper Trade Ledger ({paperTrades.closed_trades?.length || 0} Trades)
              </h3>
              <span className="text-[11px] font-mono text-slate-400">Full Indian Statutory Cost Deductions Applied</span>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="bg-slate-100/60 text-[10px] text-slate-500 uppercase tracking-wider border-b border-slate-200">
                    <th className="py-2.5 px-4 font-bold">Contract</th>
                    <th className="py-2.5 px-3 font-bold text-right">Entry ASK</th>
                    <th className="py-2.5 px-3 font-bold text-right">Exit BID</th>
                    <th className="py-2.5 px-3 font-bold text-right">Gross P&L</th>
                    <th className="py-2.5 px-3 font-bold text-right">Charges</th>
                    <th className="py-2.5 px-3 font-bold text-right">Net P&L</th>
                    <th className="py-2.5 px-3 font-bold text-right">Net %</th>
                    <th className="py-2.5 px-3 font-bold">Exit Reason</th>
                    <th className="py-2.5 px-4 font-bold">Exit Time</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {(!paperTrades.closed_trades || paperTrades.closed_trades.length === 0) ? (
                    <tr>
                      <td colSpan="9" className="py-8 text-center text-slate-400">
                        No closed paper trades recorded yet.
                      </td>
                    </tr>
                  ) : (
                    paperTrades.closed_trades.map((tr, idx) => {
                      const isWin = (tr.net_pnl || 0) > 0;
                      return (
                        <tr key={idx} className="hover:bg-slate-50/80 transition-colors">
                          <td className="py-2.5 px-4 font-bold text-slate-900">{tr.trading_symbol}</td>
                          <td className="py-2.5 px-3 text-right">₹{tr.entry_ask?.toFixed(2)}</td>
                          <td className="py-2.5 px-3 text-right">₹{tr.exit_bid?.toFixed(2)}</td>
                          <td className="py-2.5 px-3 text-right">₹{tr.gross_pnl?.toFixed(2)}</td>
                          <td className="py-2.5 px-3 text-right text-slate-500">₹{tr.charges?.toFixed(2)}</td>
                          <td className={`py-2.5 px-3 text-right font-bold ${isWin ? 'text-emerald-700' : 'text-rose-700'}`}>
                            ₹{tr.net_pnl?.toFixed(2)}
                          </td>
                          <td className={`py-2.5 px-3 text-right font-bold ${isWin ? 'text-emerald-700' : 'text-rose-700'}`}>
                            {tr.net_pnl_pct ? `${tr.net_pnl_pct.toFixed(1)}%` : '—'}
                          </td>
                          <td className="py-2.5 px-3 text-[11px] text-slate-500">{tr.exit_reason}</td>
                          <td className="py-2.5 px-4 text-[11px] text-slate-400">{tr.exit_time?.slice(11, 19)}</td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* TAB 4: MODEL CALIBRATION & FEATURE ABLATION */}
      {activeTab === 'calibration' && (
        <div className="space-y-6">
          {/* Feature Ablation Matrix */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs">
            <div className="flex items-center justify-between mb-3 border-b border-slate-100 pb-2">
              <div>
                <h3 className="text-sm font-bold text-slate-900">Feature Ablation Study (OOS Out-of-Sample)</h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  Evaluates which feature subsets genuinely provide predictive value vs. pure noise.
                </p>
              </div>
              <span className="text-xs font-mono bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded border border-indigo-200">
                HistGradientBoosting
              </span>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse text-xs font-mono">
                <thead>
                  <tr className="bg-slate-100 text-[10px] text-slate-500 uppercase tracking-wider">
                    <th className="py-2.5 px-3 font-bold">Architecture / Subset</th>
                    <th className="py-2.5 px-3 font-bold text-center">Features</th>
                    <th className="py-2.5 px-3 font-bold text-right">ROC-AUC</th>
                    <th className="py-2.5 px-3 font-bold text-right">Accuracy %</th>
                    <th className="py-2.5 px-3 font-bold text-right">Brier Score</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {calibrationData?.ablation_results &&
                    Object.entries(calibrationData.ablation_results).map(([name, res], i) => (
                      <tr key={i} className="hover:bg-slate-50">
                        <td className="py-2.5 px-3 font-bold text-slate-800">{name}</td>
                        <td className="py-2.5 px-3 text-center">{res.features_count}</td>
                        <td className="py-2.5 px-3 text-right font-bold text-indigo-600">{res.roc_auc?.toFixed(3)}</td>
                        <td className="py-2.5 px-3 text-right">{res.accuracy_pct?.toFixed(1)}%</td>
                        <td className="py-2.5 px-3 text-right text-slate-600">{res.brier_score?.toFixed(4)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Probability Calibration Reliability Table */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs">
            <h3 className="text-sm font-bold text-slate-900 mb-1">Empirical Probability Reliability Curve</h3>
            <p className="text-xs text-slate-500 mb-3">
              Compares model predicted win probabilities against empirical realized out-of-sample win rates.
            </p>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="border border-slate-200 rounded-xl p-3 bg-slate-50 font-mono text-xs space-y-2">
                <span className="text-[11px] font-bold text-slate-500 block uppercase">Reliability Binning</span>
                {calibrationData?.calibration_stats?.mean_predicted_prob?.map((p, idx) => (
                  <div key={idx} className="flex justify-between items-center py-1 border-b border-slate-200/60 last:border-0">
                    <span>Predicted: {(p * 100).toFixed(1)}%</span>
                    <span className="font-bold text-indigo-700">
                      Realized: {((calibrationData.calibration_stats.realized_win_rate?.[idx] || 0) * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>

              <div className="border border-slate-200 rounded-xl p-4 bg-indigo-50/50 space-y-2 text-xs">
                <div className="flex items-center space-x-2 text-indigo-950 font-bold">
                  <Info size={16} className="text-indigo-600" />
                  <span>Isotonic Calibration Verdict</span>
                </div>
                <p className="text-slate-600 leading-relaxed text-[11px]">
                  The Option Alpha model uses non-parametric Isotonic Regression on held-out validation partitions to eliminate overconfidence and probability skew before risk thresholds are evaluated.
                </p>
                <div className="pt-2 text-[11px] font-mono text-indigo-900">
                  Artifact SHA256: <code>{statusData?.model?.sha256?.slice(0, 16)}...</code>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 5: FEED HEALTH & DIAGNOSTICS */}
      {activeTab === 'health' && (
        <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-4">
          <div className="flex items-center justify-between border-b border-slate-100 pb-3">
            <div>
              <h3 className="text-sm font-bold text-slate-900">Upstox Market Data Feed V3 Telemetry</h3>
              <p className="text-xs text-slate-500 mt-0.5">Continuous rate-limit-free option data streaming.</p>
            </div>
            <span className={`px-2 py-0.5 rounded text-[10px] font-bold font-mono ${
              isConnected ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
            }`}>
              {feedHealth.status || 'UNKNOWN'}
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 font-mono text-xs">
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
              <span className="text-[10px] text-slate-400 block uppercase">WebSocket Connection</span>
              <strong className="text-slate-800">{isConnected ? 'CONNECTED' : 'DISCONNECTED'}</strong>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
              <span className="text-[10px] text-slate-400 block uppercase">Monitored Keys</span>
              <strong className="text-slate-800">{feedHealth.subscribed_keys_count || 0} keys</strong>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
              <span className="text-[10px] text-slate-400 block uppercase">Live Quotes Cached</span>
              <strong className="text-slate-800">{feedHealth.live_quotes_cached || 0}</strong>
            </div>
            <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
              <span className="text-[10px] text-slate-400 block uppercase">Total Ticks Ingested</span>
              <strong className="text-slate-800">{(feedHealth.total_ticks_received || 0).toLocaleString()}</strong>
            </div>
          </div>

          <div className="p-4 rounded-xl bg-slate-50 border border-slate-200 text-xs font-mono space-y-1 text-slate-600">
            <div className="font-bold text-slate-800 uppercase text-[10px] tracking-wider mb-1">Application Rate Governor Budget</div>
            <div>• Per Second Hard Ceiling: <strong>8 req/sec</strong> (200ms pacing)</div>
            <div>• Per Minute Hard Ceiling: <strong>64 req/min</strong></div>
            <div>• 30-Minute Quota Ceiling: <strong>1600 req/30min</strong></div>
            <div>• WebSocket Stream Consumption: <strong>0 REST calls</strong> (Continuous Protobuf Feed)</div>
          </div>
        </div>
      )}
    </div>
  );
}
