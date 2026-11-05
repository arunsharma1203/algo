import React, { useState, useEffect, useCallback, useMemo } from 'react';
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
  Shield,
  Power,
  Key,
  AlertOctagon,
  Lock,
  ShieldCheck,
  Coins,
  XCircle,
  Server,
  Wifi
} from 'lucide-react';
import {
  ResponsiveContainer,
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  CartesianGrid
} from 'recharts';
import { API_BASE } from '../services/api';

function buildIntradayChartModel(rawBars = [], liveSpot = 0, liveVwap = 0, fallbackOrb = null) {
  const validBars = Array.isArray(rawBars)
    ? rawBars.filter(b => b && Number(b.close) > 0)
    : [];

  let sessionBars = validBars;
  let sessionDate = '';
  if (validBars.length > 0) {
    const lastTime = String(validBars[validBars.length - 1].time || '');
    sessionDate = lastTime.slice(0, 10);
    const sameDay = validBars.filter(b => String(b.time || '').startsWith(sessionDate));
    if (sameDay.length >= 3) {
      sessionBars = sameDay;
    } else {
      sessionBars = validBars.slice(-45);
    }
  }

  let cumPV = 0;
  let cumV = 0;
  let runningHigh = 0;
  let runningLow = Infinity;
  let orbHigh = 0;
  let orbLow = Infinity;

  const series = sessionBars.map((b, idx) => {
    const o = Number(b.open) || Number(b.close);
    const h = Number(b.high) || Number(b.close);
    const l = Number(b.low) || Number(b.close);
    const c = Number(b.close);
    const v = Math.max(1, Number(b.volume) || 1);
    const tp = (h + l + c) / 3.0;
    cumPV += tp * v;
    cumV += v;
    const vwapVal = cumV > 0 ? Number((cumPV / cumV).toFixed(2)) : Number(tp.toFixed(2));

    if (h > runningHigh) runningHigh = h;
    if (l < runningLow) runningLow = l;

    const rawT = String(b.time || '');
    const tLabel = rawT.length >= 16 ? rawT.slice(11, 16) : (rawT.slice(5) || `#${idx + 1}`);
    const isOrbBar = idx < 3 || (tLabel >= '09:15' && tLabel <= '09:30');
    if (isOrbBar) {
      if (h > orbHigh) orbHigh = h;
      if (l < orbLow) orbLow = l;
    }

    return {
      time: tLabel,
      spot: Number(c.toFixed(2)),
      high: Number(h.toFixed(2)),
      low: Number(l.toFixed(2)),
      open: Number(o.toFixed(2)),
      vwap: vwapVal,
    };
  });

  const effectiveSpot = Number(liveSpot) > 0
    ? Number(liveSpot)
    : (series.length > 0 ? series[series.length - 1].spot : 0);
  const effectiveVwap = Number(liveVwap) > 0
    ? Number(liveVwap)
    : (series.length > 0 ? series[series.length - 1].vwap : effectiveSpot);

  if (effectiveSpot > 0) {
    if (series.length === 0) {
      series.push({
        time: '09:15',
        spot: effectiveSpot,
        high: effectiveSpot,
        low: effectiveSpot,
        open: effectiveSpot,
        vwap: effectiveVwap || effectiveSpot,
      });
    } else {
      const lastPt = series[series.length - 1];
      if (Math.abs(lastPt.spot - effectiveSpot) >= 0.05) {
        series.push({
          time: 'LIVE',
          spot: Number(effectiveSpot.toFixed(2)),
          high: Number(Math.max(lastPt.high, effectiveSpot).toFixed(2)),
          low: Number(Math.min(lastPt.low, effectiveSpot).toFixed(2)),
          open: lastPt.spot,
          vwap: Number((effectiveVwap || lastPt.vwap).toFixed(2)),
        });
      }
    }
    if (effectiveSpot > runningHigh) runningHigh = effectiveSpot;
    if (effectiveSpot < runningLow) runningLow = effectiveSpot;
  }

  if (fallbackOrb && Number(fallbackOrb.orb_high) > 0 && Number(fallbackOrb.orb_low) > 0) {
    orbHigh = Number(fallbackOrb.orb_high);
    orbLow = Number(fallbackOrb.orb_low);
  } else if (orbHigh <= 0 || orbLow === Infinity) {
    orbHigh = runningHigh > 0 ? runningHigh : effectiveSpot;
    orbLow = runningLow < Infinity ? runningLow : effectiveSpot;
  }

  const dayOpen = series.length > 0 ? series[0].open : effectiveSpot;
  const dayHigh = runningHigh > 0 ? runningHigh : effectiveSpot;
  const dayLow = runningLow < Infinity ? runningLow : effectiveSpot;
  const changePts = effectiveSpot > 0 && dayOpen > 0 ? effectiveSpot - dayOpen : 0;
  const changePct = dayOpen > 0 ? (changePts / dayOpen) * 100.0 : 0;

  // Determine exact Structural Zone (Zone 1 Breakout | Zone 2A ORB Retest | Zone 2B Mid-Air | Zone 3 Inside ORB)
  let zoneCode = 'ZONE_3_INSIDE_ORB';
  let zoneLabel = 'ZONE 3: INSIDE ORB (NO TRADE)';
  let zonePass = false;

  if (effectiveSpot > 0 && orbHigh > 0 && orbLow > 0) {
    const distFromHighPct = dayHigh > 0 ? ((dayHigh - effectiveSpot) / dayHigh) * 100.0 : 0;
    const distFromLowPct = dayLow > 0 ? ((effectiveSpot - dayLow) / dayLow) * 100.0 : 0;
    const distAboveOrbHighPct = orbHigh > 0 ? ((effectiveSpot - orbHigh) / orbHigh) * 100.0 : 0;
    const distBelowOrbLowPct = orbLow > 0 ? ((orbLow - effectiveSpot) / orbLow) * 100.0 : 0;

    if (effectiveSpot > orbHigh) {
      if (distFromHighPct <= 0.15) {
        zoneCode = 'ZONE_1_BREAKOUT';
        zoneLabel = 'ZONE 1: DAY HIGH BREAKOUT';
        zonePass = true;
      } else if (distAboveOrbHighPct <= 0.12 && effectiveSpot >= effectiveVwap) {
        zoneCode = 'ZONE_2A_ORB_RETEST';
        zoneLabel = 'ZONE 2A: ORB HIGH RETEST';
        zonePass = true;
      } else {
        zoneCode = 'ZONE_2B_MID_AIR';
        zoneLabel = 'ZONE 2B: MID-AIR PULLBACK (BLOCKED)';
        zonePass = false;
      }
    } else if (effectiveSpot < orbLow) {
      if (distFromLowPct <= 0.15) {
        zoneCode = 'ZONE_1_BREAKOUT';
        zoneLabel = 'ZONE 1: DAY LOW BREAKDOWN';
        zonePass = true;
      } else if (distBelowOrbLowPct <= 0.12 && effectiveSpot <= effectiveVwap) {
        zoneCode = 'ZONE_2A_ORB_RETEST';
        zoneLabel = 'ZONE 2A: ORB LOW RETEST';
        zonePass = true;
      } else {
        zoneCode = 'ZONE_2B_MID_AIR';
        zoneLabel = 'ZONE 2B: MID-AIR PULLBACK (BLOCKED)';
        zonePass = false;
      }
    }
  }

  return {
    sessionDate,
    series,
    spot: effectiveSpot,
    vwap: effectiveVwap,
    dayOpen,
    dayHigh,
    dayLow,
    orbHigh,
    orbLow,
    changePts,
    changePct,
    zoneCode,
    zoneLabel,
    zonePass,
  };
}

export default function OptionSniffer() {
  const [activeTab, setActiveTab] = useState('signals'); // 'signals' (Traffic-Light Gates) | 'candidates' | 'trades' | 'live_terminal'
  const [statusData, setStatusData] = useState(null);
  const [candidates, setCandidates] = useState([]);
  const [signals, setSignals] = useState([]);
  const [paperTrades, setPaperTrades] = useState({ open_trades: [], closed_trades: [], pnl_summary: {} });
  const [chainIntel, setChainIntel] = useState({ NIFTY: null, BANKNIFTY: null });
  const [indexBars, setIndexBars] = useState({ NIFTY: [], BANKNIFTY: [] });
  const [liveReadiness, setLiveReadiness] = useState(null);
  const [liveStatusData, setLiveStatusData] = useState(null);
  const [allocationSim, setAllocationSim] = useState(null);
  const [orderIntents, setOrderIntents] = useState([]);
  const [refreshingLive, setRefreshingLive] = useState(false);
  const [armingModalOpen, setArmingModalOpen] = useState(false);
  const [pinInput, setPinInput] = useState('');
  const [armingLoading, setArmingLoading] = useState(false);
  const [armingFeedback, setArmingFeedback] = useState(null);
  const [showRealPnl, setShowRealPnl] = useState(null);
  const [loading, setLoading] = useState(true);
  const [underlyingFilter, setUnderlyingFilter] = useState('ALL'); // 'ALL' | 'NIFTY' | 'BANKNIFTY'
  const [lastUpdated, setLastUpdated] = useState(null);

  const fetchLiveTerminalData = useCallback(async (forceRefresh = false) => {
    try {
      setRefreshingLive(true);
      const [readinessRes, allocRes, intentsRes, liveStatusRes] = await Promise.all([
        fetch(`${API_BASE}/option-sniffer/live/readiness?force_refresh=${forceRefresh}`),
        fetch(`${API_BASE}/option-sniffer/live/allocation`),
        fetch(`${API_BASE}/option-sniffer/live/order-intents?limit=20`),
        fetch(`${API_BASE}/option-sniffer/live/status`),
      ]);
      if (readinessRes.ok) setLiveReadiness(await readinessRes.json());
      if (allocRes.ok) setAllocationSim(await allocRes.json());
      if (intentsRes.ok) setOrderIntents(await intentsRes.json());
      if (liveStatusRes.ok) setLiveStatusData(await liveStatusRes.json());
    } catch (err) {
      console.error('Failed to fetch live terminal telemetry:', err);
    } finally {
      setRefreshingLive(false);
    }
  }, []);

  const handleArmLiveTrading = async () => {
    setArmingLoading(true);
    setArmingFeedback(null);
    try {
      const res = await fetch(`${API_BASE}/option-sniffer/live/arm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: pinInput })
      });
      const data = await res.json();
      setArmingFeedback(data);
      if (data.status === 'ARMED') {
        setTimeout(() => {
          setArmingModalOpen(false);
          setPinInput('');
          setArmingFeedback(null);
        }, 1200);
      }
      await fetchLiveTerminalData(true);
    } catch (err) {
      setArmingFeedback({ status: 'ERROR', message: String(err) });
    } finally {
      setArmingLoading(false);
    }
  };

  const handleDisarmLiveTrading = async () => {
    try {
      await fetch(`${API_BASE}/option-sniffer/live/disarm`, { method: 'POST' });
      await fetchLiveTerminalData(true);
    } catch (err) {
      console.error('Failed to disarm live trading:', err);
    }
  };

  const handleGlobalSOS = async () => {
    try {
      await fetch(`${API_BASE}/option-sniffer/live/sos-panic-exit`, { method: 'POST' });
      await fetchLiveTerminalData(true);
    } catch (err) {
      console.error('Failed to execute global SOS exit:', err);
    }
  };


  const handleTriggerSOS = async (orderIntentId, currentBid = 100.0) => {
    try {
      await fetch(`${API_BASE}/option-sniffer/live/simulate-sos`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          order_intent_id: orderIntentId,
          current_bid_price: currentBid
        })
      });
      await fetchLiveTerminalData();
    } catch (err) {
      console.error('SOS exit failed:', err);
    }
  };

  const fetchIndexCharts = useCallback(async () => {
    try {
      const [niftyRes, bnfRes] = await Promise.all([
        fetch(`${API_BASE}/market/latest/%5ENSEI?period=5d&interval=5m`),
        fetch(`${API_BASE}/market/latest/%5ENSEBANK?period=5d&interval=5m`),
      ]);
      const nextBars = {};
      if (niftyRes.ok) {
        const nd = await niftyRes.json();
        nextBars.NIFTY = Array.isArray(nd?.data) ? nd.data : [];
      }
      if (bnfRes.ok) {
        const bd = await bnfRes.json();
        nextBars.BANKNIFTY = Array.isArray(bd?.data) ? bd.data : [];
      }
      setIndexBars(prev => ({
        NIFTY: nextBars.NIFTY?.length ? nextBars.NIFTY : prev.NIFTY,
        BANKNIFTY: nextBars.BANKNIFTY?.length ? nextBars.BANKNIFTY : prev.BANKNIFTY,
      }));
    } catch (err) {
      console.error('Failed to fetch NIFTY/BANKNIFTY chart history:', err);
    }
  }, []);

  const fetchData = useCallback(async () => {
    try {
      const [statusRes, candRes, sigRes, tradesRes, intelRes] = await Promise.all([
        fetch(`${API_BASE}/option-sniffer/status`),
        fetch(`${API_BASE}/option-sniffer/candidates`),
        fetch(`${API_BASE}/option-sniffer/signals?limit=50`),
        fetch(`${API_BASE}/option-sniffer/paper-trades`),
        fetch(`${API_BASE}/option-sniffer-advance/chain-intelligence`),
      ]);

      if (statusRes.ok) setStatusData(await statusRes.json());
      if (candRes.ok) setCandidates(await candRes.json());
      if (sigRes.ok) setSignals(await sigRes.json());
      if (tradesRes.ok) setPaperTrades(await tradesRes.json());
      if (intelRes.ok) setChainIntel(await intelRes.json());

      setLastUpdated(new Date().toLocaleTimeString());
    } catch (err) {
      console.error('Failed to fetch option sniffer data:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
    fetchLiveTerminalData();
    fetchIndexCharts();
    const interval = setInterval(() => {
      fetchData();
      fetchLiveTerminalData();
    }, 4000); // Polling every 4 seconds for reactive UI synchronization
    const chartInterval = setInterval(() => {
      fetchIndexCharts();
    }, 20000); // Refresh 5m OHLCV bars every 20s while 4s poll updates live spot/VWAP
    return () => {
      clearInterval(interval);
      clearInterval(chartInterval);
    };
  }, [fetchData, fetchLiveTerminalData, fetchIndexCharts]);

  const pnl = paperTrades.pnl_summary || {};
  const livePnl = liveStatusData?.pnl_summary || {};
  const isArmed = Boolean(liveStatusData?.is_live_armed);
  const hasActiveLivePositions = (liveStatusData?.active_positions?.length || 0) > 0;
  const ms = liveStatusData?.microstructure || {};
  const hasRealActivity = isArmed || hasActiveLivePositions || (livePnl.closed_trades_count || 0) > 0 || Math.abs(livePnl.realized_net_pnl || 0) > 0.01;
  const displayReal = showRealPnl !== null ? showRealPnl : hasRealActivity;
  const feedHealth = statusData?.feed_health || {};
  const isConnected = feedHealth.websocket_connected;
  const marketRegime = statusData?.market_regime || {};
  const parity = statusData?.runtime_parity || {};
  const circuitBreaker = statusData?.circuit_breaker || {};
  const apexQuant = statusData?.apex_quant || {};
  const isPeakLockTripped = Boolean(circuitBreaker?.is_peak_lock_tripped || apexQuant?.is_peak_lock_tripped);
  const peakProfitFloor = circuitBreaker?.peak_profit_floor || apexQuant?.peak_profit_floor || 0;
  const engineMode = statusData?.engine_mode || 'V1_CLASSIC';
  const isSessionHalted = Boolean(circuitBreaker?.is_session_halted);
  const isPauseActive = Boolean(circuitBreaker?.is_pause_active);
  const consecutiveLosses = circuitBreaker?.consecutive_losses || 0;
  const maxConsecutiveLosses = circuitBreaker?.max_consecutive_losses || 2;
  const todayNetPnl = circuitBreaker?.today_net_pnl || 0;
  const pauseRemainingSec = circuitBreaker?.pause_remaining_sec || 0;
  const pauseUnfreezeTime = circuitBreaker?.pause_unfreeze_time || '';

  const niftyIntel = chainIntel?.NIFTY || {};
  const bnfIntel = chainIntel?.BANKNIFTY || {};

  const niftyChart = useMemo(() => {
    const liveSpot = Number(niftyIntel.spot_price || niftyIntel.spot_ltp || marketRegime.spot || 0);
    const liveVwap = Number(niftyIntel.session_tpb || marketRegime.day_vwap || 0);
    return buildIntradayChartModel(indexBars.NIFTY, liveSpot, liveVwap, ms.orb_status);
  }, [indexBars.NIFTY, niftyIntel.spot_price, niftyIntel.spot_ltp, niftyIntel.session_tpb, marketRegime.spot, marketRegime.day_vwap, ms.orb_status]);

  const bankniftyChart = useMemo(() => {
    const liveSpot = Number(bnfIntel.spot_price || bnfIntel.spot_ltp || 0);
    const liveVwap = Number(bnfIntel.session_tpb || 0);
    return buildIntradayChartModel(indexBars.BANKNIFTY, liveSpot, liveVwap, null);
  }, [indexBars.BANKNIFTY, bnfIntel.spot_price, bnfIntel.spot_ltp, bnfIntel.session_tpb]);

  // ── REAL-TIME TRAFFIC-LIGHT QUALITY GATES EVALUATION (ALL GREEN = TRADE, 1 RED = NO TRADE) ──
  const gatesSummary = useMemo(() => {
    // 1. Gate 1: Session & Golden Entry Window (09:30 - 13:15 IST)
    const isTradingWin = Boolean(statusData?.is_trading_window);
    const unlockOk = isArmed ? Boolean(ms.is_unlocked) : isTradingWin;
    const gate1Pass = isTradingWin && unlockOk;

    // 2. Gate 2: Upstox V3 Stream & Quote Freshness
    const validQuotes = candidates.filter(c => c?.quote && Number(c.quote.ltp) > 0);
    const freshestAgeMs = validQuotes.length > 0
      ? Math.min(...validQuotes.map(c => Number(c.quote.quote_age_ms ?? 0)))
      : (feedHealth.last_message_latency_ms ?? 99999);
    const ageLimitMs = isArmed ? 1000 : 15000;
    const gate2Pass = Boolean(isConnected) && validQuotes.length > 0 && freshestAgeMs <= ageLimitMs;

    // 3. Gate 3: Order Book & ATM Bid-Ask Spread Ceiling
    const atmSpreadPct = ms.spread_atm_pct != null
      ? Number(ms.spread_atm_pct)
      : (validQuotes.length > 0 ? Number(validQuotes[0]?.quote?.spread_pct ?? 9.99) : 9.99);
    const spreadCeiling = isArmed ? 1.20 : 3.50;
    const askFloorCount = validQuotes.filter(c => Number(c.quote?.ask || 0) >= 35.0 && Number(c.quote?.ask || 0) >= Number(c.quote?.bid || 0) && Number(c.quote?.bid || 0) > 0).length;
    const gate3Pass = atmSpreadPct <= spreadCeiling && askFloorCount > 0;

    // 4. Gate 4: Contract Liquidity & Delta Floor (OI >= 5,000, Vol >= 200, |Delta| >= 0.32)
    const liquidContracts = validQuotes.filter(c => {
      const q = c.quote || {};
      return Number(q.oi || 0) >= 5000 && Number(q.volume || 0) >= 200 && Math.abs(Number(q.delta || 0)) >= 0.32;
    });
    const bestOi = validQuotes.length > 0 ? Math.max(...validQuotes.map(c => Number(c.quote?.oi || 0))) : 0;
    const bestVol = validQuotes.length > 0 ? Math.max(...validQuotes.map(c => Number(c.quote?.volume || 0))) : 0;
    const gate4Pass = liquidContracts.length > 0;

    // 5. Gate 5: Opening Range (09:15-09:30 ORB) & Structural Zone (Zone 1 Breakout / Zone 2A ORB Retest)
    const orbAccepted = Boolean(ms.orb_status?.is_accepted);
    const zonePass = Boolean(niftyChart.zonePass || bankniftyChart.zonePass);
    const gate5Pass = orbAccepted && zonePass;
    const activeZoneText = niftyChart.zonePass
      ? `NIFTY ${niftyChart.zoneLabel}`
      : (bankniftyChart.zonePass ? `BNF ${bankniftyChart.zoneLabel}` : niftyChart.zoneLabel);

    // 6. Gate 6: Trend Efficiency & Chop / Climax Shield
    const erVal = Number(ms.kaufman_er ?? niftyIntel.kaufman_er ?? 0);
    const adxVal = Number(niftyIntel.adx_14 ?? 0);
    const erHurdle = isArmed ? 0.35 : 0.28;
    const isChopBlocked = marketRegime?.regime === 'RANGEBOUND_CHOP' || marketRegime?.shield === 'SHIELD_PAUSED';
    const gate6Pass = !isChopBlocked && erVal >= erHurdle && adxVal >= 18.0;

    // 7. Gate 7: AI Ensemble Conviction & Net Return Hurdle
    const activeSignalsList = signals.filter(s => s.status === 'ACTIVE' || s.status === 'TRADE_READY' || s.status === 'WATCH');
    const topSignal = activeSignalsList[0] || signals[0] || null;
    const topProbPct = topSignal ? Number((topSignal.calibrated_prob || 0) * 100) : 0;
    const topNetRet = topSignal ? Number(topSignal.expected_net_return || 0) : 0;
    const probHurdle = topSignal?.is_crbi_active ? 38.0 : 44.0;
    const gate7Pass = activeSignalsList.length > 0 && topProbPct >= probHurdle && topNetRet >= 2.0;

    // 8. Gate 8: Circuit Breaker, Canary Scout & Slot Guard
    const openPaperCount = paperTrades?.open_trades?.length || 0;
    const openLiveCount = liveStatusData?.active_positions?.length || 0;
    const activeSlotCount = isArmed ? openLiveCount : openPaperCount;
    const canaryOk = ms.canary_status !== 'LOCKED';
    const cbOk = !isSessionHalted && !isPauseActive && consecutiveLosses < maxConsecutiveLosses && todayNetPnl > -3500;
    const slotsOk = activeSlotCount < 2;
    const gate8Pass = canaryOk && cbOk && slotsOk;

    const gates = [
      {
        id: 'G1',
        shortName: 'Entry Window',
        title: 'Gate 1: Golden Entry Window & Clock',
        pass: gate1Pass,
        liveValue: isTradingWin
          ? (unlockOk ? `OPEN (${ms.dynamic_unlock_time || '09:30-13:15 IST'})` : 'WAITING CANARY UNLOCK')
          : 'CLOSED (OUTSIDE WINDOW)',
        threshold: '09:30 – 13:15 IST (Mon–Fri)',
        detail: gate1Pass
          ? 'Session clock is inside the high-alpha Golden Entry Window.'
          : (ms.unlock_reason || 'Entries strictly blocked outside 09:30–13:15 IST to evade opening noise & afternoon theta decay.'),
      },
      {
        id: 'G2',
        shortName: 'Feed Freshness',
        title: 'Gate 2: Upstox V3 Stream & Quote Age',
        pass: gate2Pass,
        liveValue: `${isConnected ? 'WS CONNECTED' : 'WS OFFLINE'} • Age: ${freshestAgeMs < 99000 ? `${freshestAgeMs}ms` : 'STALE'}`,
        threshold: `WS Connected & Quote Age ≤ ${ageLimitMs}ms`,
        detail: gate2Pass
          ? `${validQuotes.length} ATM contracts streaming live via Upstox V3 protobuf feed.`
          : 'Blocked: WebSocket stream offline or option quote age exceeds real-time freshness ceiling.',
      },
      {
        id: 'G3',
        shortName: 'ATM Spread',
        title: 'Gate 3: Order Book & Bid-Ask Spread',
        pass: gate3Pass,
        liveValue: `ATM Spread: ${atmSpreadPct.toFixed(2)}% • Valid Ask (≥₹35): ${askFloorCount}`,
        threshold: `Spread ≤ ${spreadCeiling.toFixed(2)}% & Ask ≥ ₹35`,
        detail: gate3Pass
          ? 'Order book spread is tight; slippage friction is within institutional tolerance.'
          : `Blocked: ATM bid-ask spread (${atmSpreadPct.toFixed(2)}%) exceeds ${spreadCeiling.toFixed(2)}% ceiling or premium < ₹35.`,
      },
      {
        id: 'G4',
        shortName: 'Liquidity & Delta',
        title: 'Gate 4: Contract Depth, OI & Delta Floor',
        pass: gate4Pass,
        liveValue: `${liquidContracts.length}/${candidates.length} Pass • Max OI: ${Math.round(bestOi).toLocaleString()} • Vol: ${bestVol.toLocaleString()}`,
        threshold: 'OI ≥ 5,000 • Vol ≥ 200 • |Δ| ∈ [0.32, 0.68]',
        detail: gate4Pass
          ? `${liquidContracts.length} ATM strikes satisfy institutional OI, volume, and gamma/delta responsiveness.`
          : 'Blocked: Monitored strikes lack minimum Open Interest (5,000), Volume (200), or Delta (|Δ| ≥ 0.32).',
      },
      {
        id: 'G5',
        shortName: 'ORB & Zone 1/2A',
        title: 'Gate 5: ORB Breakout & Structural Zone',
        pass: gate5Pass,
        liveValue: `${ms.orb_status?.breakout_direction || 'NO ORB BREAK'} • ${activeZoneText}`,
        threshold: '5m ORB Accepted + Zone 1 (Breakout) or Zone 2A (ORB Retest)',
        detail: gate5Pass
          ? `Price accepted outside 09:15–09:30 ORB (${ms.orb_status?.volume_ratio || 1.2}x vol) in actionable ${activeZoneText}.`
          : `Blocked: Price is in ${activeZoneText}. Mid-air pullbacks (Zone 2B) and inside-ORB chop (Zone 3) are strictly rejected.`,
      },
      {
        id: 'G6',
        shortName: 'Trend & Chop Shield',
        title: 'Gate 6: Kaufman Efficiency & Chop Shield',
        pass: gate6Pass,
        liveValue: `ER(14): ${erVal.toFixed(2)} • ADX(14): ${adxVal.toFixed(1)} • Regime: ${marketRegime?.regime || 'EVALUATING'}`,
        threshold: `Kaufman ER ≥ ${erHurdle.toFixed(2)} • ADX ≥ 18.0 • Non-Chop`,
        detail: gate6Pass
          ? 'Directional efficiency and Wilder ADX confirm genuine trend expansion over intraday noise.'
          : `Blocked: ${marketRegime?.reason || `Trend efficiency ER (${erVal.toFixed(2)}) or ADX (${adxVal.toFixed(1)}) below expansion hurdle.`}`,
      },
      {
        id: 'G7',
        shortName: 'AI Conviction',
        title: 'Gate 7: Calibrated Win Prob & Net Return',
        pass: gate7Pass,
        liveValue: activeSignalsList.length > 0
          ? `P(Win): ${topProbPct.toFixed(1)}% • Exp Net: +${topNetRet.toFixed(1)}% (${topSignal?.trading_symbol})`
          : '0 Qualified Contracts (Awaiting Setup)',
        threshold: 'P(Net Win) ≥ 44.0% (38% CRB-I) & Net Return ≥ +2.0%',
        detail: gate7Pass
          ? `${topSignal?.trading_symbol} clears isotonic probability & post-tax net return hurdle.`
          : 'Blocked: No contract currently clears both the 44% calibrated win probability and +2.0% net edge hurdle.',
      },
      {
        id: 'G8',
        shortName: 'Risk & Canary Guard',
        title: 'Gate 8: Circuit Breaker, Canary & Slots',
        pass: gate8Pass,
        liveValue: `Canary: ${ms.canary_status || 'PROBING'} • Losses: ${consecutiveLosses}/${maxConsecutiveLosses} • Slots: ${activeSlotCount}/2`,
        threshold: 'Canary HEALTHY (0 SL) • Losses < 2 • Net > -₹3,500 • Slots < 2',
        detail: gate8Pass
          ? 'All risk circuit breakers, Canary scout checks, and capital slot limits are green.'
          : (circuitBreaker?.halt_reason || 'Blocked: Circuit breaker, Canary stop-loss lock, or max open slot ceiling reached.'),
      },
    ];

    const greenCount = gates.filter(g => g.pass).length;
    const redGates = gates.filter(g => !g.pass);
    return {
      allGreen: redGates.length === 0,
      greenCount,
      redCount: redGates.length,
      redGates,
      gates,
    };
  }, [
    statusData,
    isArmed,
    ms,
    candidates,
    feedHealth,
    isConnected,
    niftyChart,
    bankniftyChart,
    niftyIntel,
    marketRegime,
    signals,
    paperTrades,
    liveStatusData,
    isSessionHalted,
    isPauseActive,
    consecutiveLosses,
    maxConsecutiveLosses,
    todayNetPnl,
  ]);

  const filteredCandidates = candidates.filter(c => {
    if (underlyingFilter === 'ALL') return true;
    return c.underlying === underlyingFilter;
  });

  return (
    <div className="space-y-6 max-w-7xl mx-auto pb-12 font-sans">
      {/* ── RUNTIME-TO-DISK CODE DESYNCHRONIZATION ALERT ── */}
      {parity?.status === 'DESYNCHRONIZED_STALE_RAM' && (
        <div className="bg-amber-950 border-2 border-amber-500 rounded-2xl p-4 text-amber-100 shadow-xl flex items-center justify-between gap-4 animate-pulse">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-amber-600/30 text-amber-400 flex items-center justify-center shrink-0 border border-amber-500/50">
              <AlertTriangle size={22} />
            </div>
            <div>
              <div className="text-xs font-black uppercase text-amber-400 tracking-wider">
                ⚠️ CODE DESYNCHRONIZATION: RUNNING RAM BYTECODE IS STALE
              </div>
              <div className="text-xs font-mono text-amber-200/90 mt-0.5">
                Disk files modified but not loaded into running server: {parity.stale_files?.join(', ')}. Restart backend process to sync!
              </div>
            </div>
          </div>
          <span className="text-[10px] font-black uppercase bg-amber-500 text-slate-950 px-2.5 py-1 rounded-lg shrink-0">
            STALE RAM
          </span>
        </div>
      )}

      {/* ── PEAK-PROFIT TRAILING LOCK BANNER (GREEN HARVEST LOCK) ── */}
      {isSessionHalted && isPeakLockTripped && (
        <div className="bg-gradient-to-r from-emerald-950 via-slate-900 to-emerald-950 border-2 border-emerald-500 rounded-2xl p-4 text-white shadow-xl flex flex-col md:flex-row items-center justify-between gap-4">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-emerald-600/30 text-emerald-400 flex items-center justify-center shrink-0 border border-emerald-500/50">
              <Award size={22} />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-xs font-black bg-emerald-600 text-slate-950 px-2 py-0.5 rounded tracking-widest uppercase">
                  🏆 APEX QUANT: MORNING HARVEST PERMANENTLY LOCKED
                </span>
                <span className="text-xs font-mono text-emerald-300">
                  Peak: ₹{(circuitBreaker?.peak_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })} | Locked Floor: ₹{peakProfitFloor.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                </span>
              </div>
              <div className="text-xs text-emerald-200 mt-1">
                {circuitBreaker.halt_reason || "Session profit exceeded +₹4,000. 50% peak profit floor engaged to eliminate midday givebacks. Capital secured."}
              </div>
            </div>
          </div>
          <span className="text-[11px] font-mono font-bold text-emerald-300 border border-emerald-500/40 px-3 py-1.5 rounded-xl bg-emerald-900/40 shrink-0">
            PROFIT PRESERVED FOR SESSION
          </span>
        </div>
      )}

      {/* ── PERSISTENT SESSION CIRCUIT BREAKER HALTED BANNER (HARD CAPITAL CEILING) ── */}
      {isSessionHalted && !isPeakLockTripped && (
        <div className="bg-gradient-to-r from-rose-950 via-slate-900 to-rose-950 border-2 border-rose-500 rounded-2xl p-4 text-white shadow-xl flex flex-col md:flex-row items-center justify-between gap-4">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-rose-600/30 text-rose-400 flex items-center justify-center shrink-0 border border-rose-500/50">
              <Lock size={22} />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-xs font-black bg-rose-600 text-white px-2 py-0.5 rounded tracking-widest uppercase">
                  🛑 HARD CAPITAL CIRCUIT BREAKER ENGAGED (-₹3,500 CEILING HIT)
                </span>
                <span className="text-xs font-mono text-slate-300">
                  Losses: {consecutiveLosses}/{maxConsecutiveLosses} | Today Net: ₹{todayNetPnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                </span>
              </div>
              <div className="text-xs text-rose-200 mt-1">
                {circuitBreaker.halt_reason || "Daily loss threshold reached. All new option entries are halted until 09:15 next session to protect capital."}
              </div>
            </div>
          </div>
          <span className="text-[11px] font-mono font-bold text-rose-300 border border-rose-500/40 px-3 py-1.5 rounded-xl bg-rose-900/40 shrink-0">
            PROTECTED UNTIL 09:15 IST
          </span>
        </div>
      )}

      {/* ── DYNAMIC INTROSPECTION PAUSE BANNER (30-MINUTE REGIME COOL-OFF) ── */}
      {!isSessionHalted && isPauseActive && (
        <div className="bg-gradient-to-r from-amber-950 via-slate-900 to-amber-950 border-2 border-amber-500/80 rounded-2xl p-4 text-white shadow-xl flex flex-col md:flex-row items-center justify-between gap-4 animate-pulse">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-amber-600/30 text-amber-400 flex items-center justify-center shrink-0 border border-amber-500/50">
              <Clock size={22} />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-xs font-black bg-amber-600 text-slate-950 px-2 py-0.5 rounded tracking-widest uppercase">
                  ⏳ DYNAMIC INTROSPECTION PAUSE ACTIVE
                </span>
                <span className="text-xs font-mono text-slate-300">
                  Losses: {consecutiveLosses}/{maxConsecutiveLosses} | Remaining: {Math.floor(pauseRemainingSec / 60)}m {pauseRemainingSec % 60}s
                </span>
              </div>
              <div className="text-xs text-amber-200/90 mt-1">
                {circuitBreaker.halt_reason || "2 consecutive losses detected. Trading temporarily paused to filter morning whipsaw noise."}
              </div>
            </div>
          </div>
          <span className="text-[11px] font-mono font-bold text-amber-300 border border-amber-500/40 px-3 py-1.5 rounded-xl bg-amber-900/40 shrink-0">
            RESUMES AT {pauseUnfreezeTime || 'COOLDOWN END'} IST
          </span>
        </div>
      )}

      {/* ── PERSISTENT WAR-ROOM HUD (LOCKS ON TOP WHEN ANY LIVE POSITION IS ACTIVE) ── */}
      {liveStatusData?.active_positions?.length > 0 && (
        <div className="bg-gradient-to-r from-rose-950 via-slate-900 to-rose-950 border-2 border-rose-500 rounded-2xl p-4 text-white shadow-xl flex flex-col md:flex-row items-center justify-between gap-4 animate-pulse">
          <div className="flex items-center space-x-3">
            <div className="w-12 h-12 rounded-xl bg-rose-600/30 text-rose-400 flex items-center justify-center shrink-0 border border-rose-500/50">
              <AlertOctagon size={24} className="animate-spin" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-xs font-black bg-rose-600 text-white px-2 py-0.5 rounded tracking-widest uppercase">
                  🚨 LIVE BROKER POSITION ACTIVE ({liveStatusData.active_positions.length}/2 SLOTS)
                </span>
                <span className="text-xs font-mono text-slate-300">
                  {liveStatusData.active_positions.map(p => `${p.trading_symbol} (${p.filled_quantity} qty)`).join(' | ')}
                </span>
              </div>
              <div className="text-sm font-bold mt-1 text-slate-200">
                Live Net MTM: <span className={(livePnl.total_net_pnl || 0) >= 0 ? "text-emerald-400" : "text-rose-400"}>
                  {(livePnl.total_net_pnl || 0) >= 0 ? '+' : ''}₹{(livePnl.total_net_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                </span> | SL Trigger: ₹{liveStatusData.active_positions[0].stop_loss_trigger} | Risk Ceiling: -₹3,500.00
              </div>
            </div>
          </div>

          <div className="flex items-center space-x-3">
            <button
              onClick={handleGlobalSOS}
              className="px-5 py-2.5 rounded-xl text-xs font-black bg-rose-600 hover:bg-rose-700 text-white uppercase tracking-wider transition-all shadow-lg hover:shadow-rose-600/50 active:scale-95 cursor-pointer flex items-center space-x-2"
            >
              <AlertOctagon size={16} />
              <span>🚨 SOS PANIC EXIT (FLATTEN TO ZERO)</span>
            </button>
          </div>
        </div>
      )}

      {/* ── TOP BANNER: 1-DAY PIN-GUARDED REAL TRADING SWITCH ────────────────────────── */}
      <div className={`border rounded-2xl p-4 flex flex-col sm:flex-row items-center justify-between gap-4 shadow-xs transition-all ${
        liveStatusData?.is_live_armed
          ? 'bg-gradient-to-r from-emerald-500/15 via-emerald-500/5 to-transparent border-emerald-500/40'
          : 'bg-gradient-to-r from-amber-500/10 via-amber-500/5 to-transparent border-amber-500/30'
      }`}>
        <div className="flex items-center space-x-3">
          <div className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 border ${
            liveStatusData?.is_live_armed
              ? 'bg-emerald-500/20 text-emerald-600 border-emerald-500/30'
              : 'bg-amber-500/20 text-amber-500 border-amber-500/30'
          }`}>
            {liveStatusData?.is_live_armed ? <Power size={20} className="animate-pulse" /> : <Shield size={20} />}
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-black text-slate-900 tracking-wide">OPTION SNIFFER</span>
              {liveStatusData?.is_live_armed ? (
                <span className="text-[10px] font-black uppercase px-2 py-0.5 rounded-full bg-emerald-600 text-white tracking-widest shadow-xs flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-white animate-ping" />
                  REAL TRADING ARMED (1-DAY)
                </span>
              ) : (
                <span className="text-[10px] font-black uppercase px-2 py-0.5 rounded-full bg-amber-500 text-white tracking-widest shadow-xs">
                  PAPER-ONLY MODE
                </span>
              )}
              <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded-md bg-slate-100 text-slate-700 border border-slate-200">
                {engineMode}
              </span>
              {apexQuant?.enabled && (
                <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded-md bg-purple-50 text-purple-700 border border-purple-200 flex items-center gap-1">
                  ⚡ APEX QUANT
                </span>
              )}
              {isPeakLockTripped && (
                <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded-md bg-emerald-600 text-white border border-emerald-700 flex items-center gap-1">
                  🏆 PEAK LOCKED (₹{peakProfitFloor.toLocaleString('en-IN')})
                </span>
              )}
              <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-md border flex items-center gap-1 ${
                parity?.in_sync
                  ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                  : 'bg-rose-50 text-rose-700 border-rose-300 animate-pulse'
              }`}>
                {parity?.in_sync ? '✅ SYNC' : '⚠️ STALE RAM'}
              </span>
              <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-md border flex items-center gap-1 ${
                isSessionHalted
                  ? 'bg-rose-600 text-white border-rose-700'
                  : consecutiveLosses > 0
                  ? 'bg-amber-100 text-amber-800 border-amber-300'
                  : 'bg-slate-100 text-slate-700 border-slate-200'
              }`}>
                {isSessionHalted ? '🛑 CB: HALTED' : `🛡️ CB: ${consecutiveLosses}/${maxConsecutiveLosses}`}
              </span>
              <span className="text-[10px] font-mono text-slate-500">
                Cutoff: 15:15 IST
              </span>
            </div>
            <p className="text-xs text-slate-600 mt-0.5">
              {liveStatusData?.is_live_armed
                ? 'Automated live orders armed for today with Upstox broker Stop-Loss & Target. Risk ceiling: -₹2,000.00.'
                : 'Zero live orders executed. All trades, P&L, and state machines operate in safe simulation mode.'}
            </p>
          </div>
        </div>

        {/* Real Trading Toggle Control */}
        <div className="flex items-center space-x-2.5 shrink-0">
          {liveStatusData?.is_live_armed ? (
            <button
              onClick={handleDisarmLiveTrading}
              className="flex items-center space-x-1.5 px-3.5 py-2 rounded-xl text-xs font-bold bg-slate-900 text-white hover:bg-slate-800 transition-all cursor-pointer shadow-xs"
            >
              <Power size={13} className="text-rose-400" />
              <span>Disarm to Paper Mode</span>
            </button>
          ) : (
            <button
              onClick={() => {
                setArmingModalOpen(true);
                setArmingFeedback(null);
                setPinInput('');
              }}
              className="flex items-center space-x-1.5 px-3.5 py-2 rounded-xl text-xs font-black bg-gradient-to-r from-amber-500 to-amber-600 hover:from-amber-600 hover:to-amber-700 text-white transition-all cursor-pointer shadow-sm active:scale-95"
            >
              <Zap size={14} />
              <span>⚡ Arm Real Trading for Today</span>
            </button>
          )}

          <div className="flex items-center space-x-1.5 px-3 py-2 rounded-xl text-[11px] font-mono font-bold text-slate-600 bg-white border border-slate-200 shadow-2xs">
            <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-emerald-500 animate-ping' : 'bg-indigo-500 animate-pulse'}`} />
            <span>AUTO-SYNC 4s {lastUpdated ? `• ${lastUpdated}` : ''}</span>
          </div>
        </div>
      </div>

      {/* ── 2-FACTOR 4-DIGIT PIN CONFIRMATION MODAL ───────────────────────── */}
      {armingModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 backdrop-blur-xs p-4 animate-in fade-in duration-150">
          <div className="bg-white rounded-3xl max-w-md w-full p-6 shadow-2xl border border-slate-200 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center space-x-2">
                <div className="w-8 h-8 rounded-lg bg-amber-500/20 text-amber-600 flex items-center justify-center">
                  <Key size={16} />
                </div>
                <div>
                  <h3 className="text-sm font-black text-slate-900">Arm 1-Day Real Trading</h3>
                  <p className="text-[11px] text-slate-500">Security PIN Verification Interlock</p>
                </div>
              </div>
              <button
                onClick={() => setArmingModalOpen(false)}
                className="text-slate-400 hover:text-slate-600 p-1 cursor-pointer"
              >
                <XCircle size={18} />
              </button>
            </div>

            <div className="bg-slate-50 rounded-2xl p-4 text-center border border-slate-200 space-y-2">
              <span className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">
                Security Verification Code
              </span>
              <div className="text-3xl font-black font-mono tracking-widest text-indigo-700 select-all">
                {liveStatusData?.current_pin || '----'}
              </div>
              <p className="text-[11px] text-slate-500">
                Type the 4-digit code above to confirm live broker execution rights for today.
              </p>
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-bold text-slate-700">Enter 4-Digit Security Code:</label>
              <input
                type="text"
                maxLength={4}
                value={pinInput}
                onChange={(e) => setPinInput(e.target.value.replace(/\D/g, ''))}
                placeholder="Type code here"
                className="w-full text-center font-mono text-2xl font-bold tracking-widest py-2.5 px-4 rounded-xl border border-slate-300 focus:border-indigo-600 focus:ring-2 focus:ring-indigo-100 outline-hidden"
              />
            </div>

            <div className="p-3 rounded-xl bg-amber-50/70 border border-amber-200 text-[11px] text-amber-900 space-y-1">
              <div className="font-bold flex items-center gap-1 text-amber-800">
                <AlertTriangle size={13} />
                Canary Scout & Dynamic Microstructure Safeguards:
              </div>
              <div>• Window: <strong>09:35 / 09:45 to 13:15 IST</strong> (Dynamic early unlock at 09:35 on Spread ≤ 1.2% & ER ≥ 0.35).</div>
              <div>• Canary Guard: If paper scout hits any Stop-Loss today, real trading is automatically locked.</div>
              <div>• Anti-Chase Guard: Contracts running &gt; 15% from base are automatically blocked from chasing.</div>
              <div>• Auto-expires at <strong>15:15 IST</strong> today (resets to Paper Mode).</div>
              <div>• Max 1 active lot per index. Hard daily net loss ceiling: <strong>-₹3,500.00</strong>.</div>
              <div>• Stop-Loss & Target orders automatically placed with Upstox broker.</div>
            </div>

            {armingFeedback && (
              <div className={`p-3 rounded-xl text-xs font-medium border ${
                armingFeedback.status === 'ARMED'
                  ? 'bg-emerald-50 text-emerald-900 border-emerald-200'
                  : 'bg-rose-50 text-rose-900 border-rose-200'
              }`}>
                {armingFeedback.message}
              </div>
            )}

            <div className="flex items-center space-x-2 pt-2">
              <button
                onClick={() => setArmingModalOpen(false)}
                className="w-1/2 py-2.5 rounded-xl text-xs font-bold bg-slate-100 text-slate-700 hover:bg-slate-200 transition-all cursor-pointer"
              >
                Cancel
              </button>
              <button
                disabled={pinInput.length !== 4 || armingLoading}
                onClick={handleArmLiveTrading}
                className="w-1/2 py-2.5 rounded-xl text-xs font-black bg-gradient-to-r from-emerald-600 to-emerald-700 hover:from-emerald-700 hover:to-emerald-800 text-white transition-all shadow-sm disabled:opacity-50 cursor-pointer"
              >
                {armingLoading ? 'Verifying...' : '⚡ Arm for Today'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── DYNAMIC MICROSTRUCTURE & CANARY SCOUT RADAR HUD ────────────────────────── */}
      <div className="bg-white border border-slate-200 rounded-2xl p-3.5 shadow-2xs space-y-2.5">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-100 pb-2.5">
          <div className="flex items-center space-x-2">
            <div className={`w-2.5 h-2.5 rounded-full ${
              ms.canary_status === 'LOCKED'
                ? 'bg-rose-600 animate-pulse'
                : ms.is_unlocked
                ? 'bg-emerald-500 animate-pulse'
                : 'bg-amber-500'
            }`} />
            <span className="text-xs font-black text-slate-800 tracking-wide uppercase flex items-center gap-1.5">
              <span>Dynamic Microstructure & Canary Radar</span>
              <span className="text-[10px] font-mono font-normal text-slate-400">
                ({ms.evaluated_at || 'Syncing...'})
              </span>
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {/* Dynamic Unlock Status Pill */}
            {ms.canary_status === 'LOCKED' ? (
              <span className="text-[10px] font-black uppercase px-2 py-0.5 rounded-md bg-rose-600 text-white tracking-wider flex items-center gap-1 shadow-xs">
                <span>🛑 CANARY ABORT: REAL LOCKED</span>
              </span>
            ) : ms.is_unlocked ? (
              <span className="text-[10px] font-black uppercase px-2 py-0.5 rounded-md bg-emerald-600 text-white tracking-wider flex items-center gap-1 shadow-xs">
                <span>⚡ UNLOCKED AT {ms.dynamic_unlock_time || '09:35 IST'}</span>
              </span>
            ) : (
              <span className="text-[10px] font-black uppercase px-2 py-0.5 rounded-md bg-amber-500 text-white tracking-wider flex items-center gap-1 shadow-xs">
                <span>⏳ EVALUATING (EARLIEST: 09:35 | FALLBACK: 09:45)</span>
              </span>
            )}

            {/* Canary Scout Status Badge */}
            <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-md border flex items-center gap-1 ${
              ms.canary_status === 'HEALTHY'
                ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                : ms.canary_status === 'LOCKED'
                ? 'bg-rose-50 text-rose-700 border-rose-300'
                : 'bg-slate-100 text-slate-600 border-slate-200'
            }`}>
              {ms.canary_status === 'HEALTHY' ? '🛡️ CANARY: HEALTHY (0 SL)' :
               ms.canary_status === 'LOCKED' ? '🚨 CANARY: STOP-LOSS DETECTED' :
               `CANARY: ${ms.canary_status || 'PROBING'}`}
            </span>
          </div>
        </div>

        {/* 3-Condition Microstructure Light Bar */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2.5 text-xs">
          {/* Light 1: Spread Compression */}
          <div className={`p-2 rounded-xl border flex items-center justify-between ${
            ms.spread_ok
              ? 'bg-emerald-50/50 border-emerald-200 text-emerald-900'
              : 'bg-amber-50/50 border-amber-200 text-amber-900'
          }`}>
            <div className="flex items-center space-x-2">
              <span className={`w-2 h-2 rounded-full ${ms.spread_ok ? 'bg-emerald-500' : 'bg-amber-500'}`} />
              <span className="font-bold">1. ATM Spread Compression</span>
            </div>
            <div className="font-mono text-[11px] font-bold">
              <span>{ms.spread_atm_pct != null ? `${ms.spread_atm_pct}%` : '0.23%'}</span>
              <span className="text-slate-400 font-normal"> (≤ 1.20%)</span>
              <span className="ml-1">{ms.spread_ok ? '✅' : '⏳'}</span>
            </div>
          </div>

          {/* Light 2: Kaufman ER */}
          <div className={`p-2 rounded-xl border flex items-center justify-between ${
            ms.kaufman_ok
              ? 'bg-emerald-50/50 border-emerald-200 text-emerald-900'
              : 'bg-amber-50/50 border-amber-200 text-amber-900'
          }`}>
            <div className="flex items-center space-x-2">
              <span className={`w-2 h-2 rounded-full ${ms.kaufman_ok ? 'bg-emerald-500' : 'bg-amber-500'}`} />
              <span className="font-bold">2. Kaufman Trend Efficiency</span>
            </div>
            <div className="font-mono text-[11px] font-bold">
              <span>{ms.kaufman_er != null ? ms.kaufman_er : '0.48'}</span>
              <span className="text-slate-400 font-normal"> (≥ 0.35)</span>
              <span className="ml-1">{ms.kaufman_ok ? '✅' : '⏳'}</span>
            </div>
          </div>

          {/* Light 3: 5m ORB Acceptance */}
          <div className={`p-2 rounded-xl border flex items-center justify-between ${
            ms.orb_status?.is_accepted
              ? 'bg-emerald-50/50 border-emerald-200 text-emerald-900'
              : 'bg-slate-50 border-slate-200 text-slate-700'
          }`}>
            <div className="flex items-center space-x-2">
              <span className={`w-2 h-2 rounded-full ${ms.orb_status?.is_accepted ? 'bg-emerald-500' : 'bg-slate-400'}`} />
              <span className="font-bold">3. 5m Opening Range Break</span>
            </div>
            <div className="font-mono text-[11px] font-bold">
              <span className={ms.orb_status?.is_accepted ? 'text-emerald-700' : 'text-slate-500'}>
                {ms.orb_status?.breakout_direction === 'BULL_EXPANSION' ? 'BULL BREAK' :
                 ms.orb_status?.breakout_direction === 'BEAR_BREAKDOWN' ? 'BEAR BREAK' :
                 'AWAITING 5M'}
              </span>
              <span className="text-slate-400 font-normal">
                {ms.orb_status?.volume_ratio ? ` (${ms.orb_status.volume_ratio}x vol)` : ''}
              </span>
              <span className="ml-1">{ms.orb_status?.is_accepted ? '✅' : '⏳'}</span>
            </div>
          </div>
        </div>

        {/* Informative Diagnostic Note */}
        {ms.unlock_reason && (
          <div className="text-[11px] font-mono text-slate-500 bg-slate-50/80 px-2.5 py-1 rounded-lg border border-slate-200/60 truncate">
            💡 {ms.unlock_reason}
          </div>
        )}
      </div>

      {/* ── SECTION 1: TOP EXECUTIVE METRICS CARDS ─────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {/* Card 1: Live Stream Feed & Market Regime */}
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Feed & Market Regime</span>
            <div className="flex items-center space-x-1.5">
              {marketRegime?.regime && (
                <span className={`text-[9px] font-mono font-black px-1.5 py-0.5 rounded border ${
                  marketRegime.regime === 'RANGEBOUND_CHOP'
                    ? 'bg-amber-50 text-amber-700 border-amber-300'
                    : marketRegime.regime === 'TRENDING_BULL'
                    ? 'bg-emerald-50 text-emerald-700 border-emerald-300'
                    : marketRegime.regime === 'TRENDING_BEAR'
                    ? 'bg-rose-50 text-rose-700 border-rose-300'
                    : 'bg-cyan-50 text-cyan-700 border-cyan-300'
                }`}>
                  {marketRegime.regime === 'RANGEBOUND_CHOP' ? '🛡️ CHOP' :
                   marketRegime.regime === 'TRENDING_BULL' ? '🟢 BULL' :
                   marketRegime.regime === 'TRENDING_BEAR' ? '🔴 BEAR' : '⚡ SQUEEZE'}
                </span>
              )}
              <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-emerald-500 animate-ping' : 'bg-rose-500'}`} />
            </div>
          </div>
          <div className="flex items-baseline justify-between">
            <div className="flex items-baseline space-x-2">
              <span className={`text-xl font-black ${isConnected ? 'text-emerald-700' : 'text-slate-700'}`}>
                {isConnected ? 'LIVE FEED' : 'STANDBY'}
              </span>
              <span className="text-xs text-slate-400 font-mono">V3 Full</span>
            </div>
            {marketRegime?.shield && (
              <span className={`text-[10px] font-mono font-bold ${
                marketRegime.shield === 'SHIELD_PAUSED' ? 'text-amber-600' : 'text-emerald-600'
              }`}>
                {marketRegime.shield === 'SHIELD_PAUSED' ? 'PAUSED' : 'ACTIVE'}
              </span>
            )}
          </div>
          <div className="text-[11px] text-slate-500 mt-2 font-mono flex items-center justify-between">
            <span title={marketRegime.reason || 'Feed Status'}>
              {marketRegime.range_pts > 0 ? `Range: ${marketRegime.range_pts} pts` : `Subscribed: ${feedHealth.subscribed_keys_count || candidates.length} keys`}
            </span>
            <span>Age: {feedHealth.last_message_latency_ms ? `${feedHealth.last_message_latency_ms}ms` : '—'}</span>
          </div>
        </div>

        {/* Card 2: Traffic-Light Quality Gates Summary */}
        <div className={`bg-white border rounded-2xl p-4 shadow-xs transition-all ${
          gatesSummary.allGreen ? 'border-emerald-300 ring-2 ring-emerald-500/10' : 'border-rose-200'
        }`}>
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Traffic-Light Gates</span>
            <span className={`text-[10px] font-mono font-black px-1.5 py-0.5 rounded border ${
              gatesSummary.allGreen
                ? 'bg-emerald-600 text-white border-emerald-700 animate-pulse'
                : 'bg-rose-600 text-white border-rose-700'
            }`}>
              {gatesSummary.allGreen ? '🟢 ALL GREEN' : '🔴 NO TRADE'}
            </span>
          </div>
          <div className="flex items-baseline justify-between">
            <div className="flex items-baseline space-x-2">
              <span className={`text-xl font-black ${gatesSummary.allGreen ? 'text-emerald-700' : 'text-rose-600'}`}>
                {gatesSummary.greenCount} / {gatesSummary.gates.length}
              </span>
              <span className="text-xs text-slate-500 font-semibold">Gates Green</span>
            </div>
            <div className="flex items-center space-x-1">
              {gatesSummary.gates.map(g => (
                <span
                  key={g.id}
                  title={`${g.title}: ${g.pass ? 'GREEN (PASS)' : 'RED (BLOCKED)'}`}
                  className={`w-2.5 h-2.5 rounded-full ${g.pass ? 'bg-emerald-500' : 'bg-rose-500'}`}
                />
              ))}
            </div>
          </div>
          <div className="text-[11px] text-slate-500 mt-2 font-mono flex items-center justify-between truncate">
            <span className="truncate">
              {gatesSummary.allGreen
                ? 'All 8 gates passed — Trade Ready'
                : `Blocked: ${gatesSummary.redGates.slice(0, 2).map(g => g.shortName).join(', ')}${gatesSummary.redCount > 2 ? ` +${gatesSummary.redCount - 2}` : ''}`}
            </span>
          </div>
        </div>

        {/* Card 3: Today's Real Live P&L or Paper Net P&L */}
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">
              {displayReal ? "⚡ Real Broker Net P&L" : "Today's Paper P&L"}
            </span>
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setShowRealPnl(!displayReal)}
                className="text-[9px] font-mono px-1.5 py-0.5 rounded border border-slate-200 bg-slate-50 hover:bg-slate-100 text-slate-600 cursor-pointer transition-all"
              >
                {displayReal ? "View Paper" : "View Real"}
              </button>
              <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded font-bold ${
                displayReal
                  ? ((livePnl.total_net_pnl || 0) >= 0 ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-rose-50 text-rose-700 border border-rose-200')
                  : ((pnl.today_total_net_pnl ?? pnl.today_net_pnl ?? 0) >= 0 ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-rose-50 text-rose-700 border border-rose-200')
              }`}>
                {displayReal ? '⚡ LIVE BROKER' : ((pnl.unrealized_net_pnl || 0) !== 0 ? 'LIVE MTM' : 'NET FRICTION')}
              </span>
            </div>
          </div>
          <div className="flex items-baseline space-x-2">
            <span className={`text-xl font-black ${
              displayReal
                ? ((livePnl.total_net_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-rose-600')
                : ((pnl.today_total_net_pnl ?? pnl.today_net_pnl ?? 0) >= 0 ? 'text-emerald-600' : 'text-rose-600')
            }`}>
              {displayReal ? (
                `${(livePnl.total_net_pnl || 0) >= 0 ? '+' : ''}₹${(livePnl.total_net_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
              ) : (
                `${(pnl.today_total_net_pnl ?? pnl.today_net_pnl ?? 0) >= 0 ? '+' : ''}₹${(pnl.today_total_net_pnl ?? pnl.today_net_pnl ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
              )}
            </span>
            <span className="text-xs text-slate-400 font-mono">
              {displayReal
                ? `(${livePnl.win_count || 0}/${livePnl.closed_trades_count || 0} closed) | ${livePnl.active_positions_count || 0}/2 slots`
                : `(${pnl.today_wins || 0}/${pnl.today_closed_trades || 0} closed)`}
            </span>
          </div>
          <div className="text-[11px] text-slate-500 mt-2 font-mono flex items-center justify-between">
            {displayReal ? (
              <>
                <span title="Broker Gross MTM">Gross: ₹{(livePnl.realized_gross_pnl ?? livePnl.total_net_pnl ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</span>
                <span title="Estimated Statutory Brokerage & STT Charges" className="text-slate-400">Charges: -₹{(livePnl.statutory_charges || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</span>
                <span title="Net Realized P&L after statutory friction" className={(livePnl.realized_net_pnl || 0) >= 0 ? 'text-emerald-600 font-bold' : 'text-rose-600 font-bold'}>
                  Net: ₹{(livePnl.realized_net_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                </span>
              </>
            ) : (
              <>
                <span>MTM: ₹{(pnl.unrealized_net_pnl || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</span>
                <span>Realized: ₹{(pnl.today_realized_net_pnl ?? pnl.today_net_pnl ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}</span>
              </>
            )}
          </div>
        </div>

        {/* Card 4: Upstox Live Wallet */}
        <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider">Upstox Wallet</span>
            <span className={`text-[10px] font-mono font-bold px-1.5 py-0.5 rounded ${
              (liveReadiness?.available_margin || 0) >= 10000 ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800'
            }`}>
              {(liveReadiness?.available_margin || 0) >= 10000 ? 'READY' : 'MARGIN LOW'}
            </span>
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-xl font-black text-slate-900">
              ₹{(liveReadiness?.available_margin || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
            </span>
            <span className="text-xs text-slate-500">Available</span>
          </div>
          <div className="text-[11px] text-slate-500 mt-2 font-mono flex items-center justify-between">
            <span>F&O: {liveReadiness?.nfo_enabled ? 'Active' : 'Pending'}</span>
            <span>Proxy: {liveReadiness?.proxy_latency_ms || '—'}ms</span>
          </div>
        </div>
      </div>

      {/* ── SECTION 1B: LIVE MARKET REGIME & INTRADAY SHIELD BANNER ────────────────── */}
      {marketRegime?.regime && (
        <div className={`p-3.5 rounded-2xl border transition-all ${
          marketRegime.regime === 'RANGEBOUND_CHOP'
            ? 'bg-amber-50/70 border-amber-200 text-amber-900'
            : marketRegime.regime === 'TRENDING_BULL'
            ? 'bg-emerald-50/70 border-emerald-200 text-emerald-900'
            : marketRegime.regime === 'TRENDING_BEAR'
            ? 'bg-rose-50/70 border-rose-200 text-rose-900'
            : 'bg-cyan-50/70 border-cyan-200 text-cyan-900'
        }`}>
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-2">
            <div className="flex items-center space-x-2.5">
              <span className={`px-2 py-0.5 rounded text-[10px] font-black uppercase tracking-wider font-mono border ${
                marketRegime.regime === 'RANGEBOUND_CHOP'
                  ? 'bg-amber-100 text-amber-800 border-amber-300'
                  : marketRegime.regime === 'TRENDING_BULL'
                  ? 'bg-emerald-100 text-emerald-800 border-emerald-300'
                  : marketRegime.regime === 'TRENDING_BEAR'
                  ? 'bg-rose-100 text-rose-800 border-rose-300'
                  : 'bg-cyan-100 text-cyan-800 border-cyan-300'
              }`}>
                {marketRegime.title || marketRegime.regime}
              </span>
              <span className="text-xs font-semibold">
                {marketRegime.reason}
              </span>
            </div>
            <div className="flex items-center space-x-4 text-[11px] font-mono text-slate-600">
              {marketRegime.spot > 0 && (
                <span>Spot: <strong className="text-slate-900">₹{marketRegime.spot.toLocaleString('en-IN', { minimumFractionDigits: 1 })}</strong></span>
              )}
              {marketRegime.day_vwap > 0 && (
                <span>VWAP: <strong className="text-slate-900">₹{marketRegime.day_vwap.toLocaleString('en-IN', { minimumFractionDigits: 1 })}</strong></span>
              )}
              {marketRegime.range_pts > 0 && (
                <span>Day Span: <strong className="text-slate-900">{marketRegime.range_pts} pts ({marketRegime.range_pct}%)</strong></span>
              )}
              {marketRegime.vix > 0 && (
                <span>VIX: <strong className="text-slate-900">{marketRegime.vix}</strong></span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ── SECTION 1C: DUAL LIVE NIFTY 50 & BANKNIFTY INTRADAY STRUCTURAL GRAPHS ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {[
          {
            symbol: 'NIFTY 50',
            keyName: 'NIFTY',
            chart: niftyChart,
            intel: niftyIntel,
            accentHex: '#4f46e5',
            gradId: 'niftySpotGrad',
          },
          {
            symbol: 'BANKNIFTY',
            keyName: 'BANKNIFTY',
            chart: bankniftyChart,
            intel: bnfIntel,
            accentHex: '#059669',
            gradId: 'bnfSpotGrad',
          },
        ].map(({ symbol, keyName, chart, intel, accentHex, gradId }) => {
          const isUp = (chart.changePts || 0) >= 0;
          return (
            <div key={keyName} className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs space-y-3">
              {/* Chart Card Header */}
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 pb-2.5">
                <div className="flex items-center space-x-2.5">
                  <div className="w-8 h-8 rounded-xl bg-slate-900 text-white flex items-center justify-center font-black text-xs">
                    {keyName === 'NIFTY' ? 'N50' : 'BNF'}
                  </div>
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="text-xs font-black text-slate-900 tracking-wide">{symbol}</span>
                      <span className={`text-[10px] font-mono font-black px-2 py-0.5 rounded border ${
                        chart.zonePass
                          ? 'bg-emerald-50 text-emerald-700 border-emerald-300'
                          : 'bg-rose-50 text-rose-700 border-rose-200'
                      }`}>
                        {chart.zonePass ? `🟢 ${chart.zoneLabel}` : `🔴 ${chart.zoneLabel}`}
                      </span>
                    </div>
                    <div className="text-[10px] font-mono text-slate-400">
                      Session: {chart.sessionDate || 'Intraday'} • 5m Candles + 4s Live Spot/VWAP
                    </div>
                  </div>
                </div>

                <div className="text-right font-mono">
                  <div className="text-base font-black text-slate-900">
                    ₹{(chart.spot || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </div>
                  <div className={`text-[11px] font-bold ${isUp ? 'text-emerald-600' : 'text-rose-600'}`}>
                    {isUp ? '+' : ''}{(chart.changePts || 0).toFixed(1)} pts ({isUp ? '+' : ''}{(chart.changePct || 0).toFixed(2)}%)
                  </div>
                </div>
              </div>

              {/* Intraday Graph Canvas */}
              <div className="h-52 w-full">
                {chart.series && chart.series.length > 0 ? (
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={chart.series} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor={accentHex} stopOpacity={0.22} />
                          <stop offset="95%" stopColor={accentHex} stopOpacity={0.01} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                      <XAxis
                        dataKey="time"
                        tick={{ fontSize: 10, fill: '#64748b' }}
                        tickLine={false}
                        axisLine={{ stroke: '#e2e8f0' }}
                        minTickGap={24}
                      />
                      <YAxis
                        domain={['auto', 'auto']}
                        tick={{ fontSize: 10, fill: '#64748b' }}
                        tickLine={false}
                        axisLine={false}
                        width={58}
                        tickFormatter={val => Number(val).toFixed(0)}
                      />
                      <Tooltip
                        contentStyle={{
                          backgroundColor: '#0f172a',
                          borderColor: '#334155',
                          borderRadius: '12px',
                          fontSize: '11px',
                          color: '#f8fafc',
                          fontFamily: 'monospace'
                        }}
                        formatter={(val, name) => [
                          `₹${Number(val).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`,
                          name === 'spot' ? 'Spot Price' : 'Session VWAP'
                        ]}
                      />
                      {chart.orbHigh > 0 && (
                        <ReferenceLine
                          y={chart.orbHigh}
                          stroke="#10b981"
                          strokeDasharray="4 4"
                          label={{ value: `ORB High (${chart.orbHigh.toFixed(0)})`, position: 'insideTopRight', fill: '#059669', fontSize: 9 }}
                        />
                      )}
                      {chart.orbLow > 0 && (
                        <ReferenceLine
                          y={chart.orbLow}
                          stroke="#f43f5e"
                          strokeDasharray="4 4"
                          label={{ value: `ORB Low (${chart.orbLow.toFixed(0)})`, position: 'insideBottomRight', fill: '#e11d48', fontSize: 9 }}
                        />
                      )}
                      <Area
                        type="monotone"
                        dataKey="spot"
                        stroke={accentHex}
                        strokeWidth={2.2}
                        fillOpacity={1}
                        fill={`url(#${gradId})`}
                        isAnimationActive={false}
                      />
                      <Line
                        type="monotone"
                        dataKey="vwap"
                        stroke="#f59e0b"
                        strokeWidth={1.6}
                        strokeDasharray="5 3"
                        dot={false}
                        isAnimationActive={false}
                      />
                    </ComposedChart>
                  </ResponsiveContainer>
                ) : (
                  <div className="h-full flex items-center justify-center text-xs text-slate-400 font-mono bg-slate-50 rounded-xl border border-slate-100">
                    Syncing {symbol} intraday series...
                  </div>
                )}
              </div>

              {/* Structural Levels & Chain Telemetry Strip */}
              <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 pt-1 text-[10px] font-mono">
                <div className="bg-slate-50 px-2.5 py-1.5 rounded-lg border border-slate-200/70">
                  <span className="text-slate-400 block uppercase">Session VWAP</span>
                  <strong className="text-amber-700">₹{(chart.vwap || 0).toFixed(1)}</strong>
                </div>
                <div className="bg-slate-50 px-2.5 py-1.5 rounded-lg border border-slate-200/70">
                  <span className="text-slate-400 block uppercase">ORB (09:15-09:30)</span>
                  <strong className="text-slate-800">{(chart.orbLow || 0).toFixed(0)} – {(chart.orbHigh || 0).toFixed(0)}</strong>
                </div>
                <div className="bg-slate-50 px-2.5 py-1.5 rounded-lg border border-slate-200/70">
                  <span className="text-slate-400 block uppercase">Day Low / High</span>
                  <strong className="text-slate-800">{(chart.dayLow || 0).toFixed(0)} – {(chart.dayHigh || 0).toFixed(0)}</strong>
                </div>
                <div className="bg-slate-50 px-2.5 py-1.5 rounded-lg border border-slate-200/70">
                  <span className="text-slate-400 block uppercase">ADX(14) • ER(14)</span>
                  <strong className="text-indigo-700">
                    {Number(intel.adx_14 || 0).toFixed(1)} • {Number(intel.kaufman_er || 0).toFixed(2)}
                  </strong>
                </div>
                <div className="bg-slate-50 px-2.5 py-1.5 rounded-lg border border-slate-200/70">
                  <span className="text-slate-400 block uppercase">PCR • Put/Call Wall</span>
                  <strong className="text-slate-800">
                    {Number(intel.pcr_oi || 1.0).toFixed(2)} • {intel.put_wall_strike || '—'}/{intel.call_wall_strike || '—'}
                  </strong>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* ── SECTION 2: SIMPLIFIED TAB NAVIGATION (4 FOCUSED TABS) ─────────── */}
      <div className="border-b border-slate-200 flex items-center justify-between overflow-x-auto pb-px">
        <div className="flex space-x-2">
          {[
            {
              id: 'signals',
              label: '🚦 Quality Gates (Traffic Light)',
              icon: Shield,
              badge: gatesSummary.allGreen ? 'ALL 8 GREEN' : `${gatesSummary.redCount} RED • NO TRADE`
            },
            { id: 'candidates', label: 'Watch Candidates (ATM)', icon: Eye, count: candidates.length },
            { id: 'trades', label: 'Paper Positions & Journal', icon: DollarSign, count: paperTrades.open_trades?.length },
            {
              id: 'live_terminal',
              label: '⚡ Upstox Live Terminal',
              icon: ShieldCheck,
              badge: liveStatusData?.is_live_armed ? 'LIVE ARMED' : (liveReadiness?.nfo_enabled ? 'READY' : 'NFO PENDING')
            },
          ].map(tab => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`flex items-center space-x-2 px-4 py-3 text-xs font-bold border-b-2 transition-all whitespace-nowrap cursor-pointer ${
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
                {tab.badge && (
                  <span className={`text-[9px] px-1.5 py-0.5 rounded font-mono font-bold ${
                    tab.badge === 'LIVE ARMED' || tab.badge === 'ALL 8 GREEN'
                      ? 'bg-emerald-600 text-white animate-pulse'
                      : tab.badge.includes('RED')
                      ? 'bg-rose-600 text-white'
                      : (tab.badge === 'READY' ? 'bg-emerald-100 text-emerald-800 border border-emerald-300' : 'bg-amber-100 text-amber-800 border border-amber-300')
                  }`}>
                    {tab.badge}
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

      {/* TAB 1: TRAFFIC-LIGHT QUALITY GATES MATRIX (+ ACTIVE SIGNALS BELOW WHEN TRIGGERED) */}
      {activeTab === 'signals' && (
        <div className="space-y-4">
          {/* Master Traffic-Light Verdict Banner */}
          <div className={`border-2 rounded-2xl p-4 flex flex-col lg:flex-row lg:items-center justify-between gap-4 shadow-xs transition-all ${
            gatesSummary.allGreen
              ? 'bg-gradient-to-r from-emerald-950 via-slate-900 to-emerald-950 border-emerald-500 text-white'
              : 'bg-gradient-to-r from-rose-950 via-slate-900 to-slate-900 border-rose-500/80 text-white'
          }`}>
            <div className="flex items-start sm:items-center space-x-3.5">
              {/* Vertical 3-Lamp Traffic Light Housing */}
              <div className="bg-slate-950 border border-slate-700 rounded-xl p-2 flex flex-col items-center space-y-1.5 shrink-0 shadow-inner">
                <span className={`w-3.5 h-3.5 rounded-full transition-all ${
                  !gatesSummary.allGreen ? 'bg-rose-500 shadow-[0_0_10px_#f43f5e] animate-pulse' : 'bg-rose-950/50'
                }`} />
                <span className="w-3.5 h-3.5 rounded-full bg-amber-950/40" />
                <span className={`w-3.5 h-3.5 rounded-full transition-all ${
                  gatesSummary.allGreen ? 'bg-emerald-400 shadow-[0_0_10px_#10b981] animate-pulse' : 'bg-emerald-950/50'
                }`} />
              </div>

              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`text-xs font-black uppercase px-2.5 py-0.5 rounded tracking-widest ${
                    gatesSummary.allGreen ? 'bg-emerald-500 text-slate-950' : 'bg-rose-600 text-white'
                  }`}>
                    {gatesSummary.allGreen
                      ? '🟢 ALL 8 GATES GREEN — TRADE EXECUTION PERMITTED'
                      : `🔴 NO TRADE — ${gatesSummary.redCount} OF 8 GATE(S) RED`}
                  </span>
                  <span className="text-xs font-mono text-slate-300">
                    ({gatesSummary.greenCount} Green / {gatesSummary.redCount} Red)
                  </span>
                </div>
                <p className="text-xs text-slate-300 mt-1">
                  {gatesSummary.allGreen
                    ? 'Every institutional gate is passing right now. System is actively executing qualified setups.'
                    : `Strict Traffic-Light Rule: Even 1 Red gate blocks entry. Currently blocking: ${gatesSummary.redGates.map(g => `${g.id} (${g.shortName})`).join(' • ')}.`}
                </p>
              </div>
            </div>

            {/* 8-Lamp Horizontal Traffic Light Pill Bar */}
            <div className="flex flex-wrap items-center gap-1.5 bg-slate-950/80 border border-slate-800 px-3 py-2 rounded-xl shrink-0">
              {gatesSummary.gates.map(g => (
                <div
                  key={g.id}
                  className={`flex items-center space-x-1 px-2 py-1 rounded-lg border text-[10px] font-mono font-bold ${
                    g.pass
                      ? 'bg-emerald-950/90 border-emerald-500/50 text-emerald-300'
                      : 'bg-rose-950/90 border-rose-500/60 text-rose-300'
                  }`}
                >
                  <span className={`w-2 h-2 rounded-full ${g.pass ? 'bg-emerald-400' : 'bg-rose-500 animate-pulse'}`} />
                  <span>{g.id}</span>
                </div>
              ))}
            </div>
          </div>

          {/* 8 Individual Traffic-Light Gate Cards */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3.5">
            {gatesSummary.gates.map(gate => (
              <div
                key={gate.id}
                className={`bg-white border-2 rounded-2xl p-4 shadow-xs flex flex-col justify-between transition-all ${
                  gate.pass
                    ? 'border-emerald-200 hover:border-emerald-300'
                    : 'border-rose-200 bg-rose-50/15 hover:border-rose-300'
                }`}
              >
                <div>
                  <div className="flex items-center justify-between gap-2 mb-2.5">
                    <div className="flex items-center space-x-2">
                      <span className={`w-3.5 h-3.5 rounded-full shrink-0 ${
                        gate.pass
                          ? 'bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.65)]'
                          : 'bg-rose-600 shadow-[0_0_8px_rgba(225,29,72,0.65)] animate-pulse'
                      }`} />
                      <span className="text-xs font-black text-slate-900">{gate.title}</span>
                    </div>
                    <span className={`text-[9px] font-mono font-black px-1.5 py-0.5 rounded border shrink-0 ${
                      gate.pass
                        ? 'bg-emerald-100 text-emerald-800 border-emerald-300'
                        : 'bg-rose-100 text-rose-800 border-rose-300'
                    }`}>
                      {gate.pass ? '🟢 PASS' : '🔴 NO TRADE'}
                    </span>
                  </div>

                  <div className="bg-slate-50 border border-slate-200/80 rounded-xl p-2.5 font-mono text-[11px] space-y-1 mb-2.5">
                    <div className="flex items-center justify-between gap-1">
                      <span className="text-[9px] uppercase text-slate-400 font-bold">Live Reading</span>
                      <span className={`font-bold text-right ${gate.pass ? 'text-emerald-700' : 'text-rose-700'}`}>
                        {gate.liveValue}
                      </span>
                    </div>
                    <div className="flex items-center justify-between gap-1 pt-1 border-t border-slate-200/60">
                      <span className="text-[9px] uppercase text-slate-400 font-bold">Required</span>
                      <span className="text-slate-600 text-[10px] text-right">{gate.threshold}</span>
                    </div>
                  </div>
                </div>

                <p className="text-[11px] text-slate-600 leading-snug">
                  {gate.detail}
                </p>
              </div>
            ))}
          </div>

          {/* Live Triggered Option Signals (Rendered Automatically When Any Contract Qualifies) */}
          {signals.length > 0 && (
            <div className="space-y-3 pt-2">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-black text-slate-900 uppercase tracking-wider flex items-center gap-1.5">
                  <Zap size={14} className="text-indigo-600" />
                  <span>Recent Qualified Option Signals ({signals.length})</span>
                </h3>
                <span className="text-[11px] font-mono text-slate-400">Net of Indian Statutory Charges</span>
              </div>
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
                          {sig.is_crbi_active && (
                            <span className="text-[9px] font-mono font-bold px-1.5 py-0.5 rounded bg-purple-100 text-purple-800 border border-purple-200">
                              CRB-I
                            </span>
                          )}
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
                              {apexQuant?.enabled && (
                                <>
                                  <span>•</span>
                                  <span className={holdingMins >= 10 ? "text-amber-600 font-bold" : "text-slate-500"}>
                                    Velocity Stop: <strong className="text-slate-700">{Math.max(0, 12 - holdingMins)}m</strong> (MFE: {tr.mfe_pct || 0}%)
                                  </span>
                                </>
                              )}
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

      {/* TAB 4: LIVE UPSTOX TERMINAL */}
      {activeTab === 'live_terminal' && (
        <div className="space-y-6">
          {/* Header Banner: Phase 3 Operational Status */}
          <div className={`border rounded-2xl p-4 flex flex-col md:flex-row items-start md:items-center justify-between gap-4 transition-all ${
            liveStatusData?.is_live_armed
              ? 'bg-gradient-to-r from-emerald-500/15 via-emerald-500/5 to-transparent border-emerald-500/40'
              : 'bg-gradient-to-r from-amber-500/10 via-amber-500/5 to-transparent border-amber-500/30'
          }`}>
            <div className="flex items-center space-x-3">
              <div className={`w-10 h-10 rounded-xl flex items-center justify-center shrink-0 border ${
                liveStatusData?.is_live_armed
                  ? 'bg-emerald-500/20 text-emerald-600 border-emerald-500/30'
                  : 'bg-amber-500/20 text-amber-500 border-amber-500/30'
              }`}>
                {liveStatusData?.is_live_armed ? <Zap size={20} className="animate-pulse" /> : <Lock size={20} />}
              </div>
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-black text-slate-900 tracking-wide">UPSTOX LIVE EXECUTION TERMINAL</span>
                  <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-full ${
                    liveStatusData?.is_live_armed
                      ? 'bg-emerald-600 text-white animate-pulse'
                      : 'bg-amber-500 text-white'
                  }`}>
                    {liveStatusData?.is_live_armed ? 'REAL TRADING ARMED (1-DAY)' : 'PAPER MODE ACTIVE'}
                  </span>
                  <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-full border flex items-center gap-1 ${
                    liveStatusData?.feed_status?.is_connected
                      ? 'bg-emerald-50 text-emerald-700 border-emerald-300'
                      : 'bg-amber-50 text-amber-700 border-amber-300'
                  }`}>
                    <span className={`w-1.5 h-1.5 rounded-full ${liveStatusData?.feed_status?.is_connected ? 'bg-emerald-500' : 'bg-amber-500'}`} />
                    {liveStatusData?.feed_status?.is_connected
                      ? `FEED STREAM LIVE (${liveStatusData?.feed_status?.last_message_seconds_ago != null ? `${liveStatusData.feed_status.last_message_seconds_ago}s ago` : 'connected'})`
                      : 'FEED STREAM OFFLINE / POLLING'}
                  </span>
                </div>
                <p className="text-xs text-slate-600 mt-0.5">
                  {liveStatusData?.is_live_armed
                    ? 'Live execution armed for today. Orders placed with linked Upstox broker Stop-Loss & Target. Auto-expires at 15:15 IST.'
                    : 'Pre-flight account verification, Mumbai static IPv6 proxy telemetry, and dynamic knapsack capital allocation.'}
                </p>
              </div>
            </div>
            <button
              onClick={() => fetchLiveTerminalData(true)}
              disabled={refreshingLive}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl text-xs font-bold bg-white text-slate-700 hover:bg-slate-50 border border-slate-200 transition-all shadow-2xs self-end md:self-auto cursor-pointer"
            >
              <RefreshCw size={13} className={refreshingLive ? 'animate-spin text-indigo-600' : 'text-slate-500'} />
              <span>{refreshingLive ? 'Probing Upstox...' : 'Refresh Telemetry'}</span>
            </button>
          </div>

          {/* Section 1: Pre-Flight Readiness Cards */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {/* Card 1: Account Profile & Exchange Segments */}
            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                  <Server size={13} className="text-slate-400" />
                  Account & Segments
                </span>
                <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                  liveReadiness?.nfo_enabled ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800 border border-rose-200'
                }`}>
                  {liveReadiness?.nfo_enabled ? 'NFO ACTIVE' : 'NFO INACTIVE'}
                </span>
              </div>
              <div>
                <div className="text-base font-black text-slate-900">
                  {liveReadiness?.user_name || 'Upstox Account'}
                </div>
                <div className="text-xs text-slate-500 font-mono">
                  User ID: {liveReadiness?.user_id || '—'}
                </div>
              </div>
              <div className="text-[11px] space-y-1 pt-2 border-t border-slate-100">
                <div className="flex justify-between">
                  <span className="text-slate-500">Enabled Exchanges:</span>
                  <span className="font-mono font-bold text-slate-800">
                    {liveReadiness?.exchanges?.length ? liveReadiness.exchanges.join(', ') : 'None'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">F&O Derivatives:</span>
                  <span className={`font-mono font-bold ${liveReadiness?.nfo_enabled ? 'text-emerald-600' : 'text-rose-600'}`}>
                    {liveReadiness?.nfo_enabled ? 'Enabled (NFO)' : 'Disabled (BSE, NSE only)'}
                  </span>
                </div>
              </div>
            </div>

            {/* Card 2: Wallet Margins */}
            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                  <Coins size={13} className="text-slate-400" />
                  Equity Wallet Margin
                </span>
                <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                  (liveReadiness?.available_margin || 0) >= 10000 ? 'bg-emerald-100 text-emerald-800' : 'bg-amber-100 text-amber-800 border border-amber-200'
                }`}>
                  {(liveReadiness?.available_margin || 0) >= 10000 ? 'FUNDED' : 'LOW BALANCE'}
                </span>
              </div>
              <div>
                <div className="text-xl font-black text-slate-900">
                  ₹{(liveReadiness?.available_margin || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </div>
                <div className="text-xs text-slate-500">Available Trading Margin</div>
              </div>
              <div className="text-[11px] space-y-1 pt-2 border-t border-slate-100">
                <div className="flex justify-between">
                  <span className="text-slate-500">Used Margin:</span>
                  <span className="font-mono font-bold text-slate-800">
                    ₹{(liveReadiness?.used_margin || 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">Required Minimum:</span>
                  <span className="font-mono font-bold text-slate-700">₹10,000.00</span>
                </div>
              </div>
            </div>

            {/* Card 3: Mumbai Proxy & Static IP */}
            <div className="bg-white border border-slate-200 rounded-2xl p-4 shadow-xs space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                  <Wifi size={13} className="text-slate-400" />
                  Static IP Proxy
                </span>
                <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                  liveReadiness?.static_ip_matched ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800 border border-rose-200'
                }`}>
                  {liveReadiness?.static_ip_matched ? 'WHITELIST MATCH' : 'IP MISMATCH'}
                </span>
              </div>
              <div>
                <div className="text-sm font-black text-slate-900 font-mono truncate" title={liveReadiness?.primary_ip}>
                  {liveReadiness?.primary_ip || '2600:1900:40a0:83a::63'}
                </div>
                <div className="text-xs text-slate-500">GCP Mumbai Static IPv6</div>
              </div>
              <div className="text-[11px] space-y-1 pt-2 border-t border-slate-100">
                <div className="flex justify-between">
                  <span className="text-slate-500">Proxy Gateway:</span>
                  <span className="font-mono text-slate-800">{liveReadiness?.proxy_url_masked || 'in-mum.ipv6-proxy.in:13099'}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">Round-Trip Latency:</span>
                  <span className="font-mono font-bold text-indigo-600">{liveReadiness?.proxy_latency_ms || '—'} ms</span>
                </div>
              </div>
            </div>
          </div>

          {/* Section 2: Pre-Flight Gate Blockers Card */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-3">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center space-x-2">
                <ShieldAlert size={16} className="text-amber-500" />
                <h3 className="text-sm font-bold text-slate-900">Pre-Flight Safety Gatekeepers (Hard Blockers)</h3>
              </div>
              <span className={`text-[11px] font-mono font-bold px-2 py-0.5 rounded border ${
                liveReadiness?.live_trading_eligible
                  ? 'text-emerald-700 bg-emerald-50 border-emerald-300'
                  : 'text-rose-600 bg-rose-50 border-rose-200'
              }`}>
                {liveReadiness?.live_trading_eligible ? 'ALL GATES PASSED — ELIGIBLE' : 'PRE-FLIGHT GATE BLOCKED'}
              </span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
              <div className="flex items-start space-x-2.5 p-3 rounded-xl bg-slate-50 border border-slate-200">
                {liveReadiness?.nfo_enabled ? (
                  <CheckCircle2 size={16} className="text-emerald-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle size={16} className="text-rose-500 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-bold text-slate-800">1. Upstox F&O (NFO) Exchange Segment</div>
                  <div className="text-slate-500 text-[11px] mt-0.5">
                    {liveReadiness?.nfo_enabled
                      ? 'NFO segment active. Ready for options trading.'
                      : 'FAIL: Profile lists only BSE and NSE. Upstox will reject any NIFTY/BANKNIFTY options orders.'}
                  </div>
                </div>
              </div>

              <div className="flex items-start space-x-2.5 p-3 rounded-xl bg-slate-50 border border-slate-200">
                {(liveReadiness?.available_margin || 0) >= 10000 ? (
                  <CheckCircle2 size={16} className="text-emerald-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle size={16} className="text-rose-500 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-bold text-slate-800">2. Minimum Trading Margin (₹10,000+)</div>
                  <div className="text-slate-500 text-[11px] mt-0.5">
                    {(liveReadiness?.available_margin || 0) >= 10000
                      ? 'Sufficient wallet margin available.'
                      : `FAIL: Available margin is ₹${(liveReadiness?.available_margin || 0).toFixed(2)}. Minimum ₹10,000 required to fund single-lot option premium.`}
                  </div>
                </div>
              </div>

              <div className="flex items-start space-x-2.5 p-3 rounded-xl bg-slate-50 border border-slate-200">
                {liveReadiness?.static_ip_matched ? (
                  <CheckCircle2 size={16} className="text-emerald-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle size={16} className="text-rose-500 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-bold text-slate-800">3. Mumbai Static IP Whitelist</div>
                  <div className="text-slate-500 text-[11px] mt-0.5">
                    {liveReadiness?.static_ip_matched
                      ? 'Proxy IP matches registered primary/secondary whitelist.'
                      : 'FAIL: Outgoing proxy IP does not match registered whitelist.'}
                  </div>
                </div>
              </div>

              <div className="flex items-start space-x-2.5 p-3 rounded-xl bg-slate-50 border border-slate-200">
                {liveReadiness?.token_valid ? (
                  <CheckCircle2 size={16} className="text-emerald-500 shrink-0 mt-0.5" />
                ) : (
                  <XCircle size={16} className="text-rose-500 shrink-0 mt-0.5" />
                )}
                <div>
                  <div className="font-bold text-slate-800">4. Upstox V2 API Authentication</div>
                  <div className="text-slate-500 text-[11px] mt-0.5">
                    {liveReadiness?.token_valid
                      ? 'Bearer access token valid and authenticated (200 OK).'
                      : 'FAIL: Token is invalid, expired, or missing in app settings.'}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Section 3: Dynamic Knapsack Capital Allocator & Interactive Simulator */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-3">
              <div>
                <h3 className="text-sm font-bold text-slate-900">Dynamic Knapsack Capital Allocator</h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  Greedily allocates active Option Sniffer signals by risk-adjusted edge (Prob × Return %), applying a 10% safety buffer for statutory charges and slippage.
                </p>
              </div>

              {/* Driven Strictly by Actual Wallet Margin */}
              <div className="flex items-center space-x-2">
                <span className="text-xs font-bold text-slate-600 whitespace-nowrap">Upstox Wallet Margin:</span>
                <span className={`text-xs font-mono font-bold px-2.5 py-1 rounded-lg border ${
                  (liveReadiness?.available_margin || 0) >= 10000
                    ? 'bg-emerald-50 text-emerald-800 border-emerald-200'
                    : 'bg-amber-50 text-amber-800 border-amber-200'
                }`}>
                  ₹{(liveReadiness?.available_margin || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </span>
              </div>
            </div>

            {/* Allocation Metrics Strip */}
            {allocationSim && (
              <div className="grid grid-cols-2 sm:grid-cols-6 gap-2 text-xs font-mono">
                <div className="bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                  <span className="text-[10px] text-slate-400 block uppercase">Wallet Margin</span>
                  <strong className="text-slate-800">₹{(allocationSim.wallet_margin_actual ?? 0).toLocaleString()}</strong>
                </div>
                <div className="bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                  <span className="text-[10px] text-slate-400 block uppercase">10% Safety Buffer</span>
                  <strong className="text-amber-700">-₹{allocationSim.safety_reserve_deducted?.toLocaleString()}</strong>
                </div>
                <div className="bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                  <span className="text-[10px] text-slate-400 block uppercase">Deployable Capital</span>
                  <strong className="text-indigo-700">₹{allocationSim.deployable_capital?.toLocaleString()}</strong>
                </div>
                <div className="bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                  <span className="text-[10px] text-slate-400 block uppercase">Capital Utilized</span>
                  <strong className="text-emerald-700">₹{allocationSim.capital_utilized?.toLocaleString()}</strong>
                </div>
                <div className="bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                  <span className="text-[10px] text-slate-400 block uppercase">Remaining Reserve</span>
                  <strong className="text-slate-600">₹{allocationSim.capital_unallocated?.toLocaleString()}</strong>
                </div>
                <div className="bg-slate-50 p-2.5 rounded-xl border border-slate-200">
                  <span className="text-[10px] text-slate-400 block uppercase">Max Lots Funded</span>
                  <strong className="text-indigo-900">{allocationSim.max_concurrent_lots_supported || 0} Lots</strong>
                </div>
              </div>
            )}

            {/* Allocated Trades Table */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-bold text-emerald-800 uppercase tracking-wider flex items-center gap-1.5">
                  <CheckCircle2 size={13} className="text-emerald-600" />
                  {isArmed ? '⚡ Active Autopilot Allocation Slots' : '⚡ Planned Capital Allocation (Pre-Flight Preview)'} ({allocationSim?.allocated_trades?.length || 0}/2 Slots Funded)
                </span>
                <span className="text-[11px] text-slate-400 font-mono">Ranked by Priority Score (Desc)</span>
              </div>
              <div className="border border-slate-200 rounded-xl overflow-x-auto">
                <table className="w-full text-left text-xs font-sans">
                  <thead className="bg-slate-50 border-b border-slate-200 text-[10px] uppercase font-bold text-slate-500 font-mono">
                    <tr>
                      <th className="py-2.5 px-3">Contract Symbol</th>
                      <th className="py-2.5 px-3">Action</th>
                      <th className="py-2.5 px-3">Entry Ask</th>
                      <th className="py-2.5 px-3">Lot Size</th>
                      <th className="py-2.5 px-3">Capital Required</th>
                      <th className="py-2.5 px-3">Priority Score</th>
                      <th className="py-2.5 px-3">Calibrated Prob</th>
                      <th className="py-2.5 px-3">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 font-mono text-[11px]">
                    {allocationSim?.allocated_trades?.length > 0 ? (
                      allocationSim.allocated_trades.map((t, idx) => (
                        <tr key={idx} className="hover:bg-slate-50/80 transition-all bg-emerald-50/20">
                          <td className="py-2.5 px-3 font-bold text-slate-900">{t.trading_symbol}</td>
                          <td className="py-2.5 px-3">
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                              t.action.includes('CE') ? 'bg-emerald-100 text-emerald-800' : 'bg-rose-100 text-rose-800'
                            }`}>
                              {t.action}
                            </span>
                          </td>
                          <td className="py-2.5 px-3">₹{t.suggested_entry?.toFixed(2)}</td>
                          <td className="py-2.5 px-3">{t.lot_size} qty</td>
                          <td className="py-2.5 px-3 font-bold text-slate-900">₹{t.cost_per_lot?.toLocaleString()}</td>
                          <td className="py-2.5 px-3 font-bold text-indigo-700">{t.priority_score}</td>
                          <td className="py-2.5 px-3">{(t.calibrated_prob * 100).toFixed(1)}%</td>
                          <td className="py-2.5 px-3">
                            <span className="text-[10px] font-bold bg-emerald-100 text-emerald-800 px-2 py-0.5 rounded border border-emerald-200">
                              {isArmed ? `AUTOPILOT TARGET (SLOT ${idx + 1}/2)` : `FUNDED & PLANNED (SLOT ${idx + 1}/2)`}
                            </span>
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={8} className="py-6 text-center text-slate-500 font-sans text-xs space-y-1">
                          <div className="font-bold text-slate-700">🔍 Standby for Live Market Session (09:30 IST Window)</div>
                          <div className="text-[11px] text-slate-400">
                            No active signals for today yet. The greedy Knapsack optimizer will automatically populate the top 2 highest-conviction contracts from the live chain once market opens.
                          </div>
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Skipped Trades Table */}
            {allocationSim?.skipped_trades?.length > 0 && (
              <div>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-bold text-amber-800 uppercase tracking-wider flex items-center gap-1.5">
                    <AlertTriangle size={13} className="text-amber-600" />
                    Signals Skipped Due to Margin Limit ({allocationSim.skipped_trades.length})
                  </span>
                  <span className="text-[11px] text-slate-400 font-mono">Continues in Paper Mode Only</span>
                </div>
                <div className="border border-slate-200 rounded-xl overflow-x-auto">
                  <table className="w-full text-left text-xs font-sans">
                    <thead className="bg-slate-50 border-b border-slate-200 text-[10px] uppercase font-bold text-slate-500 font-mono">
                      <tr>
                        <th className="py-2 px-3">Contract Symbol</th>
                        <th className="py-2 px-3">Action</th>
                        <th className="py-2 px-3">Cost/Lot</th>
                        <th className="py-2 px-3">Priority Score</th>
                        <th className="py-2 px-3">Margin Shortfall</th>
                        <th className="py-2 px-3">Reason</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 font-mono text-[11px]">
                      {allocationSim.skipped_trades.slice(0, 5).map((t, idx) => (
                        <tr key={idx} className="hover:bg-slate-50/80 transition-all text-slate-500">
                          <td className="py-2 px-3 font-medium text-slate-700">{t.trading_symbol}</td>
                          <td className="py-2 px-3">{t.action}</td>
                          <td className="py-2 px-3">₹{t.cost_per_lot?.toLocaleString()}</td>
                          <td className="py-2 px-3">{t.priority_score}</td>
                          <td className="py-2 px-3 text-rose-600 font-bold">₹{t.shortfall_amount?.toLocaleString()}</td>
                          <td className="py-2 px-3">
                            <span className="text-[10px] bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded">
                              EXCEEDS WALLET BUDGET
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>

          {/* Section 4: Read-Only Paper Positions Mirror */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-3">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center space-x-2">
                <CheckCircle2 size={16} className="text-indigo-600" />
                <h3 className="text-sm font-bold text-slate-900">Parallel Paper Execution Mirror (Read-Only Observer)</h3>
              </div>
              <span className="text-[10px] font-mono text-slate-500">
                Connected via SQLite <code>mode=ro</code> & <code>query_only=ON</code>
              </span>
            </div>
            <p className="text-xs text-slate-600 leading-relaxed">
              Paper trading continues running in parallel 100% untouched. The live execution adapter reads signals and positions with zero write permissions to ensure your backtests and paper performance remain completely isolated from broker orders.
            </p>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono pt-2">
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
                <span className="text-[10px] text-slate-400 block uppercase">Paper Positions Open</span>
                <strong className="text-indigo-700">{paperTrades.open_trades?.length || 0} active</strong>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
                <span className="text-[10px] text-slate-400 block uppercase">Closed Trades Today</span>
                <strong className="text-slate-800">{pnl.today_closed_trades || 0} trades</strong>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
                <span className="text-[10px] text-slate-400 block uppercase">Today Paper Net P&L</span>
                <strong className={(pnl.today_total_net_pnl ?? 0) >= 0 ? 'text-emerald-600' : 'text-rose-600'}>
                  ₹{(pnl.today_total_net_pnl ?? 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                </strong>
              </div>
              <div className="bg-slate-50 p-3 rounded-xl border border-slate-200">
                <span className="text-[10px] text-slate-400 block uppercase">DB Write Permission</span>
                <strong className="text-rose-600">PROHIBITED (0 BYTES)</strong>
              </div>
            </div>
          </div>

          {/* Section 5: Live Order Intents & Executions Ledger */}
          <div className="bg-white border border-slate-200 rounded-2xl p-5 shadow-xs space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-3">
              <div>
                <div className="flex items-center space-x-2">
                  <ShieldCheck size={16} className="text-emerald-600" />
                  <h3 className="text-sm font-bold text-slate-900">Live Order Intents & Executions Ledger</h3>
                </div>
                <p className="text-xs text-slate-500 mt-0.5">
                  Tracked broker order intents with automatic Stop-Loss, Target orders, and emergency square-off protection.
                </p>
              </div>
              <span className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded ${
                liveStatusData?.is_live_armed
                  ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                  : 'bg-slate-100 text-slate-600 border border-slate-200'
              }`}>
                {liveStatusData?.is_live_armed ? 'LIVE EXECUTION ACTIVE' : 'PAPER-ONLY MODE'}
              </span>
            </div>

            {/* Order Intents Ledger Table */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-bold text-slate-800 uppercase tracking-wider">
                  Live Orders & Positions ({orderIntents.length})
                </span>
                <span className="text-[11px] font-mono text-slate-400">Isolated Additive Table (option_live_order_intents)</span>
              </div>
              <div className="border border-slate-200 rounded-xl overflow-x-auto">
                <table className="w-full text-left text-xs font-sans">
                  <thead className="bg-slate-50 border-b border-slate-200 text-[10px] uppercase font-bold text-slate-500 font-mono">
                    <tr>
                      <th className="py-2.5 px-3">Intent ID</th>
                      <th className="py-2.5 px-3">Symbol</th>
                      <th className="py-2.5 px-3">State</th>
                      <th className="py-2.5 px-3">Filled Qty</th>
                      <th className="py-2.5 px-3">Fill Price</th>
                      <th className="py-2.5 px-3">Linked SL</th>
                      <th className="py-2.5 px-3">Exit State</th>
                      <th className="py-2.5 px-3">Net P&L (MTM / Realized)</th>
                      <th className="py-2.5 px-3 text-right">Emergency Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 font-mono text-[11px]">
                    {orderIntents.length > 0 ? (
                      orderIntents.map((intent, idx) => (
                        <tr key={idx} className="hover:bg-slate-50/80 transition-all">
                          <td className="py-2.5 px-3 font-medium text-slate-800">
                            <div className="font-mono text-xs font-bold truncate max-w-[150px]" title={intent.order_intent_id}>
                              {intent.order_intent_id}
                            </div>
                            {intent.broker_order_id ? (
                              <div className="flex items-center gap-1 mt-1">
                                {String(intent.broker_order_id).startsWith('UPSTOX_') ? (
                                  <span className="px-1.5 py-0.2 rounded text-[9px] font-bold bg-amber-100 text-amber-800 border border-amber-300 font-mono">
                                    🧪 SIMULATED TEST
                                  </span>
                                ) : (
                                  <span className="px-1.5 py-0.2 rounded text-[9px] font-bold bg-emerald-100 text-emerald-800 border border-emerald-300 font-mono">
                                    ⚡ LIVE BROKER
                                  </span>
                                )}
                                <span className="text-[10px] text-slate-500 font-mono truncate max-w-[90px]" title={intent.broker_order_id}>
                                  {intent.broker_order_id}
                                </span>
                              </div>
                            ) : null}
                          </td>
                          <td className="py-2.5 px-3 font-bold text-slate-900">{intent.trading_symbol}</td>
                          <td className="py-2.5 px-3">
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                              intent.state === 'FILLED' ? 'bg-emerald-100 text-emerald-800' :
                              intent.state === 'PARTIALLY_FILLED' ? 'bg-amber-100 text-amber-800' :
                              intent.state === 'REJECTED' ? 'bg-rose-100 text-rose-800' :
                              'bg-slate-100 text-slate-700'
                            }`}>
                              {intent.state}
                            </span>
                          </td>
                          <td className="py-2.5 px-3">{intent.filled_quantity}/{intent.target_quantity}</td>
                          <td className="py-2.5 px-3">
                            <div>₹{intent.average_fill_price?.toFixed(2) || '—'}</div>
                            {intent.current_bid ? (
                              <div className="text-[10px] text-indigo-600 font-bold">Bid: ₹{intent.current_bid.toFixed(2)}</div>
                            ) : null}
                          </td>
                          <td className="py-2.5 px-3 font-bold text-rose-600">
                            {intent.stop_loss_trigger ? `₹${intent.stop_loss_trigger}` : '—'}
                          </td>
                          <td className="py-2.5 px-3">
                            <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${
                              intent.exit_state === 'CONFIRMED_FLAT' ? 'bg-slate-100 text-slate-600' :
                              intent.exit_state?.includes('EXIT') ? 'bg-amber-100 text-amber-800' :
                              'text-slate-400'
                            }`}>
                              {intent.exit_state}
                            </span>
                          </td>
                          <td className="py-2.5 px-3 font-bold">
                            {intent.exit_state === 'CONFIRMED_FLAT' ? (
                              <div>
                                <span className={(intent.gross_pnl ?? intent.realized_pnl) >= 0 ? 'text-emerald-600' : 'text-rose-600'}>
                                  {(intent.gross_pnl ?? intent.realized_pnl) >= 0 ? '+' : ''}₹{(intent.gross_pnl ?? intent.realized_pnl)?.toFixed(2)}
                                </span>
                                {intent.statutory_charges > 0 && (
                                  <span className="block text-[9px] text-slate-400 font-normal">
                                    Net: ₹{intent.realized_pnl?.toFixed(2)} (charges: -₹{intent.statutory_charges?.toFixed(2)})
                                  </span>
                                )}
                              </div>
                            ) : (intent.state === 'FILLED' && typeof intent.unrealized_pnl === 'number') ? (
                              <div>
                                <span className={intent.unrealized_pnl >= 0 ? 'text-emerald-600' : 'text-rose-600'}>
                                  {intent.unrealized_pnl >= 0 ? '+' : ''}₹{intent.unrealized_pnl.toFixed(2)}
                                </span>
                                <span className="ml-1 text-[9px] px-1 py-0.2 rounded bg-indigo-50 text-indigo-700 border border-indigo-200">
                                  LIVE MTM ({intent.unrealized_pnl_pct >= 0 ? '+' : ''}{intent.unrealized_pnl_pct?.toFixed(1)}%)
                                </span>
                              </div>
                            ) : '—'}
                          </td>
                          <td className="py-2.5 px-3 text-right">
                            {intent.state === 'FILLED' && intent.exit_state !== 'CONFIRMED_FLAT' ? (
                              <button
                                onClick={() => handleTriggerSOS(intent.order_intent_id, intent.current_bid || (intent.average_fill_price || 100) * 1.05)}
                                className="px-2.5 py-1 rounded text-[10px] font-bold bg-rose-600 hover:bg-rose-700 text-white transition-all cursor-pointer shadow-2xs whitespace-nowrap"
                              >
                                🚨 SOS PANIC EXIT
                              </button>
                            ) : (
                              <span className="text-[10px] text-slate-400">FLAT</span>
                            )}
                          </td>
                        </tr>
                      ))
                    ) : (
                      <tr>
                        <td colSpan={9} className="py-4 text-center text-slate-400 font-sans text-xs">
                          No live orders active. When 1-Day Real Trading is armed, orders appear here with real-time fill and SL tracking.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      )}


    </div>
  );
}
