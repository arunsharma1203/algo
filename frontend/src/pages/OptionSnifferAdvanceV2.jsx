import React, { useCallback, useEffect, useRef, useState } from 'react';
import { RefreshCw, ShieldCheck, FlaskConical } from 'lucide-react';
import V2OverviewTab from './optionSnifferV2/V2OverviewTab';
import V2LiveControlPanel from '../components/optionSnifferV2Live/V2LiveControlPanel';
import V2ObservabilityPanel from '../components/optionSnifferV2Live/V2ObservabilityPanel';
import V2ComparisonTab from './optionSnifferV2/V2ComparisonTab';
import V2AuditTab from './optionSnifferV2/V2AuditTab';
import { fetchAllV2EndpointsReadOnly } from './optionSnifferV2/v2ApiClient';
import { fmtAgeMs, fmtIstStamp, getV2FixtureBundle } from './optionSnifferV2/v2Fixtures';

const TABS = [
  { id: 'forward', label: 'Forward Paper' },
  { id: 'replay', label: 'Causal Replay' },
  { id: 'comparison', label: 'V1 Comparison' },
  { id: 'audit', label: 'Audit & Disclosures' },
];

export default function OptionSnifferAdvanceV2() {
  const [activeTab, setActiveTab] = useState('forward');
  const [loading, setLoading] = useState(true);
  const [fixtureMode, setFixtureMode] = useState(null);

  const [includeCollateral, setIncludeCollateral] = useState(false);
  const [collateralYieldPa, setCollateralYieldPa] = useState(6.0);
  const [haircutPct, setHaircutPct] = useState(10.0);

  const [endpoints, setEndpoints] = useState({
    overview: { data: null, status: 'loading', stale: false, unavailable: false, error: null },
    positions: { data: null, status: 'loading', stale: false, unavailable: false, error: null },
    dailyComparison: { data: null, status: 'loading', stale: false, unavailable: false, error: null },
    replayAudit: { data: null, status: 'loading', stale: false, unavailable: false, error: null },
    counterfactuals: { data: null, status: 'loading', stale: false, unavailable: false, error: null },
    diagnostics: { data: null, status: 'loading', stale: false, unavailable: false, error: null },
    opportunities: { data: null, status: 'loading', stale: false, unavailable: false, error: null },
  });

  const abortRef = useRef(null);
  const cacheRef = useRef({});

  // Strictly read-only GET fetch (zero writes/subscriptions/collector side effects on load/refresh)
  const fetchReadOnlyData = useCallback(async () => {
    const params = new URLSearchParams(window.location.search);
    const fxParam = params.get('fixture');
    if (fxParam) {
      const fx = getV2FixtureBundle(fxParam);
      if (fx) {
        setFixtureMode(fx.fixtureMode);
        setEndpoints(fx.endpoints);
        setLoading(false);
        return;
      }
    }
    setFixtureMode(null);

    if (abortRef.current) {
      abortRef.current.abort();
    }
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      const nextState = await fetchAllV2EndpointsReadOnly({
        signal: ctrl.signal,
        includeCollateral,
        collateralYieldPa,
        haircutPct,
        previousState: cacheRef.current,
      });
      cacheRef.current = nextState;
      setEndpoints(nextState);
    } catch (err) {
      if (err?.name !== 'AbortError') {
        // Unreachable unexpected error handled per-endpoint
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

  const ov = endpoints.overview?.data;
  const colStatus = ov?.collection_status;
  const collectorLabel = colStatus?.collector_state || 'Unavailable';
  const isHealthyCollector = collectorLabel === 'ACTIVE_RECEIVING_DATA';
  const lastEvtIst = colStatus?.last_usable_event_ist ? fmtIstStamp(colStatus.last_usable_event_ist) : 'Unavailable';
  const freshnessLabel = colStatus ? fmtAgeMs(colStatus.data_freshness_ms) : 'Unavailable';

  return (
    <div className="max-w-7xl mx-auto px-3 sm:px-4 py-4 space-y-4 text-slate-100">
      {/* Development Fixture Banner (Explicit `?fixture=` Only) */}
      {fixtureMode && (
        <div className="rounded-xl border border-fuchsia-500/60 bg-fuchsia-950/50 px-3.5 py-2 text-xs font-mono text-fuchsia-100 flex items-center gap-2">
          <FlaskConical className="w-4 h-4 text-fuchsia-300 shrink-0" />
          <span>
            DEVELOPMENT TEST FIXTURE ACTIVE (`?fixture={fixtureMode}`) — Sample display state only; not live broker or SQLite state.
          </span>
        </div>
      )}

      {/* Compact Operator Header */}
      <header className="rounded-xl border border-slate-800 bg-slate-900/70 px-4 py-3 flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
        <div className="flex items-center gap-2.5 flex-wrap">
          <ShieldCheck className="w-5 h-5 text-emerald-400 shrink-0" />
          <h1 className="text-lg font-bold tracking-tight text-white">V2 Option Sniffer</h1>
          <span className="text-[11px] font-mono font-bold px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-500/40">
            PAPER ONLY
          </span>
          <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">
            {ov?.engine_version || 'v2.2.0-causal'}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-2 text-[11px] font-mono">
          <span
            className={`px-2 py-1 rounded border ${
              isHealthyCollector
                ? 'bg-emerald-950/70 text-emerald-200 border-emerald-500/40'
                : collectorLabel === 'Unavailable' || collectorLabel.includes('DEGRADED')
                ? 'bg-rose-950/70 text-rose-200 border-rose-500/40'
                : 'bg-slate-800 text-amber-200 border-slate-700'
            }`}
          >
            Collector: {collectorLabel}
          </span>
          <span className="px-2 py-1 rounded bg-slate-950/80 text-slate-300 border border-slate-800">
            Last Event: {lastEvtIst}
          </span>
          <span className="px-2 py-1 rounded bg-slate-950/80 text-slate-300 border border-slate-800">
            Freshness: {freshnessLabel}
          </span>
          <button
            type="button"
            onClick={fetchReadOnlyData}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 border border-slate-600 text-slate-100 transition-colors"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        </div>
      </header>

      {/* Focused 4-View Navigation */}
      <nav aria-label="V2 Option Sniffer Views" className="flex flex-wrap gap-2 border-b border-slate-800 pb-2">
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
                  : 'bg-slate-900/60 text-slate-300 hover:text-white border border-slate-800'
              }`}
            >
              {t.label}
            </button>
          );
        })}
      </nav>

      {/* Active View */}
      {loading && endpoints.overview.status === 'loading' ? (
        <div className="py-10 text-center font-mono text-xs text-slate-400">
          Loading read-only V2 Option Sniffer telemetry...
        </div>
      ) : (
        <>
          <V2LiveControlPanel paperOpenCount={endpoints.positions.data?.forward_open_positions?.length || 0} />
          {activeTab === 'forward' && (
            <>
              <V2OverviewTab
                overview={endpoints.overview.data}
                overviewMeta={endpoints.overview}
                positions={endpoints.positions.data}
                positionsMeta={endpoints.positions}
              />
              <V2ObservabilityPanel
                overview={endpoints.overview.data}
                diagnostics={endpoints.diagnostics.data}
                opportunities={endpoints.opportunities.data}
              />
            </>
          )}
          {activeTab === 'replay' && (
            <V2ComparisonTab
              mode="replay"
              overview={endpoints.overview.data}
              dailyComparison={endpoints.dailyComparison.data}
              dailyComparisonMeta={endpoints.dailyComparison}
              replayAudit={endpoints.replayAudit.data}
              replayAuditMeta={endpoints.replayAudit}
              counterfactuals={endpoints.counterfactuals.data}
              counterfactualsMeta={endpoints.counterfactuals}
            />
          )}
          {activeTab === 'comparison' && (
            <V2ComparisonTab
              mode="comparison"
              overview={endpoints.overview.data}
              dailyComparison={endpoints.dailyComparison.data}
              dailyComparisonMeta={endpoints.dailyComparison}
              replayAudit={endpoints.replayAudit.data}
              replayAuditMeta={endpoints.replayAudit}
              counterfactuals={endpoints.counterfactuals.data}
              counterfactualsMeta={endpoints.counterfactuals}
            />
          )}
          {activeTab === 'audit' && (
            <V2AuditTab
              overview={endpoints.overview.data}
              replayAudit={endpoints.replayAudit.data}
              diagnostics={endpoints.diagnostics.data}
              diagnosticsMeta={endpoints.diagnostics}
              includeCollateral={includeCollateral}
              setIncludeCollateral={setIncludeCollateral}
              collateralYieldPa={collateralYieldPa}
              setCollateralYieldPa={setCollateralYieldPa}
              haircutPct={haircutPct}
              setHaircutPct={setHaircutPct}
            />
          )}
        </>
      )}
    </div>
  );
}
