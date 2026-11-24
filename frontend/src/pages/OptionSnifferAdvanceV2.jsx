import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ShieldCheck, RefreshCw, AlertTriangle } from 'lucide-react';
import { API_BASE } from '../services/api';
import V2OverviewTab from './optionSnifferV2/V2OverviewTab';
import V2PositionsTab from './optionSnifferV2/V2PositionsTab';
import V2ComparisonTab from './optionSnifferV2/V2ComparisonTab';
import V2AuditTab from './optionSnifferV2/V2AuditTab';
import { getV2FixtureBundle } from './optionSnifferV2/v2Fixtures';

const TABS = [
  { id: 'overview', label: 'Overview & Hygiene' },
  { id: 'positions', label: 'Positions & Ledger' },
  { id: 'comparison', label: 'V1 vs V2 & Attribution' },
  { id: 'audit', label: 'O3 Audit & Isolation' },
];

export default function OptionSnifferAdvanceV2() {
  const [activeTab, setActiveTab] = useState('overview');
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState(null);

  const [includeCollateral, setIncludeCollateral] = useState(false);
  const [collateralYieldPa, setCollateralYieldPa] = useState(6.0);
  const [haircutPct, setHaircutPct] = useState(10.0);

  const [overview, setOverview] = useState(null);
  const [positions, setPositions] = useState(null);
  const [dailyComparison, setDailyComparison] = useState(null);
  const [replayAudit, setReplayAudit] = useState(null);
  const [counterfactuals, setCounterfactuals] = useState(null);
  const [diagnostics, setDiagnostics] = useState(null);

  const abortRef = useRef(null);

  // Strictly read-only GET fetch (never calls POST or starts collectors on load/refresh)
  const fetchReadOnlyData = useCallback(async () => {
    const params = new URLSearchParams(window.location.search);
    const fixtureMode = params.get('fixture');
    if (fixtureMode) {
      const fx = getV2FixtureBundle(fixtureMode);
      if (fx) {
        setOverview(fx.overview);
        setPositions(fx.positions);
        setDailyComparison(fx.dailyComparison);
        setReplayAudit(fx.replayAudit);
        setCounterfactuals(fx.counterfactuals);
        setDiagnostics(fx.diagnostics);
        setErrorMsg(null);
        setLoading(false);
        return;
      }
    }

    if (abortRef.current) {
      abortRef.current.abort();
    }
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    const base = `${API_BASE}/api/option-sniffer-advance-v2`;
    const qs = `include_collateral=${includeCollateral ? 'true' : 'false'}&collateral_yield_pa=${encodeURIComponent(
      collateralYieldPa
    )}&haircut_pct=${encodeURIComponent(haircutPct)}`;

    try {
      const ovRes = await fetch(`${base}/overview?${qs}`, { method: 'GET', signal: ctrl.signal });
      if (ovRes.status === 404) {
        const pendingFx = getV2FixtureBundle('pending_restart');
        setOverview({
          ...pendingFx.overview,
          result_boundary_notice:
            'Shared backend (:8000) returned 404 because it was intentionally left running without restart to protect V1.',
          forward_paper_summary: {
            ...pendingFx.overview.forward_paper_summary,
            trade_count: 0,
            net_pnl_inr: 0,
            realized_equity_inr: null,
          },
          archive_replay_summary: {
            trade_count: null,
            net_pnl_inr: null,
            total_charges_inr: null,
            peak_capital_used_inr: null,
            intraday_mtm_dd_inr: null,
            canonical_ledger_sha256: null,
          },
        });
        setPositions({ forward_open_positions: [], forward_closed_trades: [], replay_closed_trades: [] });
        setDailyComparison({ archive_daily_comparison: [] });
        setCounterfactuals({ included_in_v2_totals: false, cf1_summary: {}, cf2_summary: {}, rows: [] });
        setReplayAudit({ phase4_o3_audit: { reproduction_status: 'PENDING_SHARED_SERVICE_RESTART', spec_conflicts: [] }, ablation_study: [], sensitivity: {} });
        setDiagnostics({ simulation_mode: true, live_capable_by_config: false, protected_artifacts_integrity: { all_unchanged: true, hashes: {} }, upstox_entitlement_field_inventory: [], existing_v2_systems_inventory: [] });
        setErrorMsg('Shared backend (:8000) has not been restarted to mount /api/option-sniffer-advance-v2 yet.');
        setLoading(false);
        return;
      }
      if (!ovRes.ok) {
        throw new Error(`HTTP ${ovRes.status} from /overview`);
      }
      const ovJson = await ovRes.json();

      const [posJson, cmpJson, repJson, cfJson, diagJson] = await Promise.all([
        fetch(`${base}/positions`, { method: 'GET', signal: ctrl.signal }).then((r) => (r.ok ? r.json() : null)),
        fetch(`${base}/daily-comparison`, { method: 'GET', signal: ctrl.signal }).then((r) => (r.ok ? r.json() : null)),
        fetch(`${base}/replay-audit`, { method: 'GET', signal: ctrl.signal }).then((r) => (r.ok ? r.json() : null)),
        fetch(`${base}/counterfactuals`, { method: 'GET', signal: ctrl.signal }).then((r) => (r.ok ? r.json() : null)),
        fetch(`${base}/diagnostics`, { method: 'GET', signal: ctrl.signal }).then((r) => (r.ok ? r.json() : null)),
      ]);

      setOverview(ovJson);
      if (posJson) setPositions(posJson);
      if (cmpJson) setDailyComparison(cmpJson);
      if (repJson) setReplayAudit(repJson);
      if (cfJson) setCounterfactuals(cfJson);
      if (diagJson) setDiagnostics(diagJson);
      setErrorMsg(null);
    } catch (err) {
      if (err?.name !== 'AbortError') {
        setErrorMsg(err?.message || 'Unable to load read-only V2 views.');
      }
    } finally {
      setLoading(false);
    }
  }, [includeCollateral, collateralYieldPa, haircutPct]);

  useEffect(() => {
    fetchReadOnlyData();
    const id = setInterval(() => {
      fetchReadOnlyData();
    }, 15000);
    return () => {
      clearInterval(id);
      if (abortRef.current) abortRef.current.abort();
    };
  }, [fetchReadOnlyData]);

  return (
    <div className="max-w-7xl mx-auto px-4 py-5 space-y-5 text-slate-100">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3 border-b border-slate-800 pb-4">
        <div>
          <div className="flex items-center gap-2.5 flex-wrap">
            <ShieldCheck className="w-6 h-6 text-emerald-400" />
            <h1 className="text-xl font-bold tracking-tight text-white">V2 Option Sniffer</h1>
            <span className="text-[11px] font-mono font-bold px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-500/40">
              PAPER ONLY (LOCAL SIMULATOR)
            </span>
            <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
              Isolated osv2_* DB
            </span>
          </div>
          <p className="text-xs text-slate-400 mt-1 font-mono">
            Parallel paper experiment (`option_sniffer_v2`). Zero broker orders, zero live arm/disarm paths, zero interference with V1/V6B.
          </p>
        </div>

        <button
          type="button"
          onClick={fetchReadOnlyData}
          className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-600 text-xs font-mono text-slate-200 transition-colors self-start md:self-auto"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh Read-Only
        </button>
      </div>

      {errorMsg && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-950/30 px-3 py-2 text-xs font-mono text-amber-200 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
          <span>{errorMsg}</span>
        </div>
      )}

      {/* Navigation Tabs */}
      <div className="flex flex-wrap gap-2 border-b border-slate-800 pb-2">
        {TABS.map((t) => {
          const active = activeTab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => setActiveTab(t.id)}
              className={`px-3 py-1.5 rounded-lg text-xs font-mono font-semibold transition-colors ${
                active
                  ? 'bg-emerald-600/20 text-emerald-300 border border-emerald-500/50'
                  : 'bg-slate-900/60 text-slate-400 hover:text-slate-200 border border-slate-800'
              }`}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {/* Tab Body */}
      {loading && !overview ? (
        <div className="py-12 text-center font-mono text-sm text-slate-400">Loading V2 Option Sniffer read-only telemetry...</div>
      ) : (
        <>
          {activeTab === 'overview' && (
            <V2OverviewTab
              overview={overview}
              includeCollateral={includeCollateral}
              setIncludeCollateral={setIncludeCollateral}
              collateralYieldPa={collateralYieldPa}
              setCollateralYieldPa={setCollateralYieldPa}
              haircutPct={haircutPct}
              setHaircutPct={setHaircutPct}
            />
          )}
          {activeTab === 'positions' && <V2PositionsTab positions={positions} />}
          {activeTab === 'comparison' && (
            <V2ComparisonTab
              dailyComparison={dailyComparison}
              counterfactuals={counterfactuals}
              replayAudit={replayAudit}
            />
          )}
          {activeTab === 'audit' && <V2AuditTab replayAudit={replayAudit} diagnostics={diagnostics} />}
        </>
      )}
    </div>
  );
}
