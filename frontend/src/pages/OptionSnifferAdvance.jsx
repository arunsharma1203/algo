import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  BarChart3,
  Briefcase,
  FlaskConical,
  LayoutDashboard,
  Radio,
} from 'lucide-react';
import { API_BASE } from '../services/api';
import ArmInterlockModal from './optionSnifferAdvance/ArmInterlockModal';
import DiagnosticsTab from './optionSnifferAdvance/DiagnosticsTab';
import OverviewTab from './optionSnifferAdvance/OverviewTab';
import PerformanceTab from './optionSnifferAdvance/PerformanceTab';
import PositionsTab from './optionSnifferAdvance/PositionsTab';
import SignalsTab from './optionSnifferAdvance/SignalsTab';
import WorkspaceHeader from './optionSnifferAdvance/WorkspaceHeader';
import { FIXTURE_SCENARIOS, getFixtureBundle } from './optionSnifferAdvance/fixtures';
import {
  createPollingCoordinator,
  deriveExecutionStatus,
} from './optionSnifferAdvance/formatters';

const ADV_BASE = `${API_BASE}/option-sniffer-advance`;

const WORKSPACE_TABS = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'signals', label: 'Signals', icon: Radio },
  { id: 'positions', label: 'Positions & Executions', icon: Briefcase },
  { id: 'performance', label: 'Performance', icon: BarChart3 },
  { id: 'diagnostics', label: 'Diagnostics', icon: Activity },
];

const INITIAL_SLICE_META = {
  live: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  trades: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  readiness: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  status: { status: 'loading', lastSuccessMs: null, errorMessage: null },
  signals: { status: 'loading', lastSuccessMs: null, errorMessage: null },
};

function readInitialQuery() {
  if (typeof window === 'undefined') {
    return { fixture: null, tab: 'overview' };
  }
  const params = new URLSearchParams(window.location.search);
  const rawFixture = params.get('fixture');
  const rawTab = params.get('tab');
  const validTab = WORKSPACE_TABS.some((t) => t.id === rawTab) ? rawTab : 'overview';
  return {
    fixture: rawFixture ? rawFixture.trim() : null,
    tab: validTab,
  };
}

function getCurrentMonthIST() {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Kolkata',
      year: 'numeric',
      month: '2-digit',
    }).formatToParts(new Date());
    const yr = parts.find((p) => p.type === 'year')?.value;
    const mo = parts.find((p) => p.type === 'month')?.value;
    if (yr && mo) return `${yr}-${mo}`;
  } catch {
    // Fallback below
  }
  return new Date().toISOString().slice(0, 7);
}

export default function OptionSnifferAdvance() {
  const initialQuery = useMemo(() => readInitialQuery(), []);
  const [fixtureScenario, setFixtureScenario] = useState(initialQuery.fixture);
  const [fixtureAnchorMs, setFixtureAnchorMs] = useState(() => Date.now());
  const [activeTab, setActiveTab] = useState(initialQuery.tab);

  const [polledLiveStatus, setPolledLiveStatus] = useState(null);
  const [polledTradesData, setPolledTradesData] = useState(null);
  const [polledReadinessData, setPolledReadinessData] = useState(null);
  const [polledStatusData, setPolledStatusData] = useState(null);
  const [polledSignalsData, setPolledSignalsData] = useState([]);
  const [polledSliceMeta, setPolledSliceMeta] = useState(INITIAL_SLICE_META);

  const [liveNowMs, setLiveNowMs] = useState(() => Date.now());
  const [sweeping, setSweeping] = useState(false);
  const [extraBrokerIntents, setExtraBrokerIntents] = useState([]);
  const [loadingMoreIntents, setLoadingMoreIntents] = useState(false);
  const [armModalOpen, setArmModalOpen] = useState(false);
  const [armPinInput, setArmPinInput] = useState('');
  const [armActionBusy, setArmActionBusy] = useState(false);
  const [armActionMsg, setArmActionMsg] = useState(null);

  const [coordinator] = useState(() => createPollingCoordinator());
  const activeControllersRef = useRef(new Set());

  const isFixtureMode = Boolean(fixtureScenario);
  const defaultCurrentMonth = useMemo(() => getCurrentMonthIST(), []);

  // Deterministic synthetic fixture bundle computed without cascading effect renders or network I/O
  const fixtureBundle = useMemo(
    () => (isFixtureMode ? getFixtureBundle(fixtureScenario, fixtureAnchorMs) : null),
    [isFixtureMode, fixtureScenario, fixtureAnchorMs]
  );

  const nowMs = isFixtureMode ? fixtureAnchorMs : liveNowMs;
  const liveStatus = isFixtureMode ? fixtureBundle.live.data : polledLiveStatus;
  const tradesData = isFixtureMode ? fixtureBundle.trades.data : polledTradesData;
  const readinessData = isFixtureMode ? fixtureBundle.readiness.data : polledReadinessData;
  const statusData = isFixtureMode ? fixtureBundle.status.data : polledStatusData;
  const signalsData = isFixtureMode ? fixtureBundle.signals.data || [] : polledSignalsData;
  const sliceMeta = useMemo(() => {
    if (!isFixtureMode || !fixtureBundle) return polledSliceMeta;
    return {
      live: {
        status: fixtureBundle.live.ok ? 'ok' : 'error',
        lastSuccessMs: fixtureBundle.live.retrievedAtMs,
        errorMessage: fixtureBundle.live.error || null,
      },
      trades: {
        status: fixtureBundle.trades.ok ? 'ok' : 'error',
        lastSuccessMs: fixtureBundle.trades.retrievedAtMs,
        errorMessage: fixtureBundle.trades.error || null,
      },
      readiness: {
        status: fixtureBundle.readiness.ok ? 'ok' : 'error',
        lastSuccessMs: fixtureBundle.readiness.retrievedAtMs,
        errorMessage: fixtureBundle.readiness.error || null,
      },
      status: {
        status: fixtureBundle.status.ok ? 'ok' : 'error',
        lastSuccessMs: fixtureBundle.status.retrievedAtMs,
        errorMessage: fixtureBundle.status.error || null,
      },
      signals: {
        status: fixtureBundle.signals.ok ? 'ok' : 'error',
        lastSuccessMs: fixtureBundle.signals.retrievedAtMs,
        errorMessage: fixtureBundle.signals.error || null,
      },
    };
  }, [isFixtureMode, fixtureBundle, polledSliceMeta]);

  const fetchSliceJson = useCallback(
    async (sliceName, url, setter) => {
      const token = coordinator.issueSliceToken(sliceName);
      const controller = new AbortController();
      activeControllersRef.current.add(controller);

      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) {
          let detail = `HTTP ${response.status}`;
          try {
            const errJson = await response.json();
            if (errJson?.detail) detail = String(errJson.detail);
          } catch {
            // Ignore non-JSON error body
          }
          throw new Error(detail);
        }
        const payload = await response.json();
        if (controller.signal.aborted) return;
        if (!coordinator.shouldApplySliceResponse(sliceName, token)) return;

        const okTs = Date.now();
        setter(payload);
        setPolledSliceMeta((prev) => ({
          ...prev,
          [sliceName]: {
            status: 'ok',
            lastSuccessMs: okTs,
            errorMessage: null,
          },
        }));
      } catch (err) {
        if (controller.signal.aborted || err?.name === 'AbortError') return;
        if (!coordinator.shouldApplySliceResponse(sliceName, token)) return;
        setPolledSliceMeta((prev) => ({
          ...prev,
          [sliceName]: {
            status: 'error',
            lastSuccessMs: prev[sliceName]?.lastSuccessMs ?? null,
            errorMessage: err?.message || 'Network request failed',
          },
        }));
      } finally {
        activeControllersRef.current.delete(controller);
      }
    },
    [coordinator]
  );

  const runFastPoll = useCallback(async () => {
    if (isFixtureMode) return;
    if (!coordinator.beginTier('fast')) return;
    try {
      await Promise.allSettled([
        fetchSliceJson('live', `${ADV_BASE}/live/status`, setPolledLiveStatus),
        fetchSliceJson('trades', `${ADV_BASE}/trades`, setPolledTradesData),
        fetchSliceJson('readiness', `${ADV_BASE}/backend-readiness`, setPolledReadinessData),
      ]);
    } finally {
      coordinator.endTier('fast');
    }
  }, [isFixtureMode, coordinator, fetchSliceJson]);

  const runSlowPoll = useCallback(async () => {
    if (isFixtureMode) return;
    if (!coordinator.beginTier('slow')) return;
    try {
      await Promise.allSettled([
        fetchSliceJson('status', `${ADV_BASE}/status`, setPolledStatusData),
        fetchSliceJson('signals', `${ADV_BASE}/signals?limit=40`, setPolledSignalsData),
      ]);
    } finally {
      coordinator.endTier('slow');
    }
  }, [isFixtureMode, coordinator, fetchSliceJson]);

  // Single-owner consolidated polling lifecycle (Zero POST requests; aborted cleanly on unmount)
  useEffect(() => {
    if (isFixtureMode) return undefined;
    const controllers = activeControllersRef.current;

    runFastPoll();
    runSlowPoll();

    const fastTimer = setInterval(runFastPoll, 1000);
    const slowTimer = setInterval(runSlowPoll, 8000);
    const clockTimer = setInterval(() => setLiveNowMs(Date.now()), 1000);

    return () => {
      clearInterval(fastTimer);
      clearInterval(slowTimer);
      clearInterval(clockTimer);
      controllers.forEach((ctrl) => ctrl.abort());
      controllers.clear();
    };
  }, [isFixtureMode, runFastPoll, runSlowPoll]);

  const activeReadiness = readinessData || statusData?.backend_readiness || null;
  const shadowTelemetry =
    activeReadiness?.shadow_telemetry || statusData?.shadow_telemetry || null;

  const executionStatus = useMemo(
    () =>
      deriveExecutionStatus({
        liveStatus,
        liveFetchState: sliceMeta.live,
        nowMs,
      }),
    [liveStatus, sliceMeta.live, nowMs]
  );

  const openPaperCount = Array.isArray(tradesData?.open_trades)
    ? tradesData.open_trades.length
    : null;
  const pendingLeg2Count = shadowTelemetry?.shadow_account_ledger?.pending_leg2_count ?? 0;
  const unwindCount =
    shadowTelemetry?.shadow_account_ledger?.incomplete_leg_unwinds_count ?? 0;

  const blockIfFixture = useCallback(
    (actionName) => {
      if (!isFixtureMode) return false;
      setArmActionMsg({
        type: 'info',
        text: `${actionName} intercepted locally: synthetic fixture preview mode never sends live backend or broker requests.`,
      });
      return true;
    },
    [isFixtureMode]
  );

  // Explicit human-initiated actions ONLY
  const handleLoadMoreBrokerIntents = async (offset = 25) => {
    if (blockIfFixture('Broker order-intent pagination')) return;
    setLoadingMoreIntents(true);
    try {
      const res = await fetch(`${ADV_BASE}/live/order-intents?limit=50&offset=${Math.max(0, Number(offset) || 0)}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.detail || `HTTP ${res.status}`);
      }
      const rows = await res.json();
      if (Array.isArray(rows) && rows.length > 0) {
        setExtraBrokerIntents((prev) => {
          const seen = new Set(prev.map((r) => r?.order_intent_id));
          const additions = rows.filter((r) => r?.order_intent_id && !seen.has(r.order_intent_id));
          return [...prev, ...additions];
        });
      }
    } catch (err) {
      setArmActionMsg({
        type: 'error',
        text: `Failed to load older broker intents: ${err?.message || 'Error'}`,
      });
    } finally {
      setLoadingMoreIntents(false);
    }
  };

  const handleManualSweep = async () => {
    if (blockIfFixture('Surveillance sweep')) return;
    setSweeping(true);
    try {
      const res = await fetch(`${ADV_BASE}/scan`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.detail || `HTTP ${res.status}`);
      }
      await Promise.allSettled([runFastPoll(), runSlowPoll()]);
    } catch (err) {
      setArmActionMsg({
        type: 'error',
        text: `Manual surveillance sweep failed: ${err?.message || 'Error'}`,
      });
    } finally {
      setSweeping(false);
    }
  };

  const handleSelectMode = async (modeId) => {
    if (blockIfFixture(`Mode switch (${modeId})`)) return;
    setArmActionBusy(true);
    try {
      const res = await fetch(`${ADV_BASE}/live/mode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ execution_mode: modeId }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data?.detail || data?.message || `HTTP ${res.status}`);
      }
      await runFastPoll();
      setArmActionMsg({
        type: 'info',
        text: data?.message || `Configured execution mode updated to ${modeId}.`,
      });
    } catch (err) {
      setArmActionMsg({
        type: 'error',
        text: `Failed to update execution mode: ${err?.message || 'Error'}`,
      });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleConfirmArm = async () => {
    if (blockIfFixture('Broker arm request')) return;
    setArmActionBusy(true);
    try {
      const res = await fetch(`${ADV_BASE}/live/arm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pin: armPinInput.trim(),
          execution_mode: liveStatus?.execution_mode || 'LONG_BOOK_ONLY',
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data?.detail || data?.message || `HTTP ${res.status}`);
      }
      setArmPinInput('');
      setArmModalOpen(false);
      await runFastPoll();
      setArmActionMsg({
        type: 'info',
        text: data?.message || 'Live broker mirroring armed by operator.',
      });
    } catch (err) {
      setArmActionMsg({
        type: 'error',
        text: `Arm request rejected: ${err?.message || 'Error'}`,
      });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleDisarm = async () => {
    if (blockIfFixture('Broker disarm request')) return;
    setArmActionBusy(true);
    try {
      const res = await fetch(`${ADV_BASE}/live/disarm`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data?.detail || data?.message || `HTTP ${res.status}`);
      }
      await runFastPoll();
      setArmActionMsg({
        type: 'info',
        text: data?.message || 'Broker bridge disarmed; reverted to Paper Only.',
      });
    } catch (err) {
      setArmActionMsg({
        type: 'error',
        text: `Disarm request failed: ${err?.message || 'Error'}`,
      });
    } finally {
      setArmActionBusy(false);
    }
  };

  const handleSosPanicExit = async () => {
    if (blockIfFixture('Emergency SOS flatten')) return;
    const confirmed = window.confirm(
      'Execute Emergency SOS Broker Flatten & Disarm? This submits immediate market exit orders for any open broker positions and disarms the live bridge.'
    );
    if (!confirmed) return;

    setArmActionBusy(true);
    try {
      const res = await fetch(`${ADV_BASE}/live/sos-panic-exit`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data?.detail || data?.message || `HTTP ${res.status}`);
      }
      await runFastPoll();
      setArmActionMsg({
        type: 'info',
        text: data?.message || 'SOS emergency flatten executed and broker bridge disarmed.',
      });
    } catch (err) {
      setArmActionMsg({
        type: 'error',
        text: `SOS flatten request failed: ${err?.message || 'Error'}`,
      });
    } finally {
      setArmActionBusy(false);
    }
  };

  return (
    <div className="w-full max-w-7xl min-w-0 overflow-x-hidden mx-auto px-3 sm:px-6 pt-4 pb-16 space-y-4 text-slate-900">
      {/* Visibly Labeled Synthetic Fixture Banner (Rendered ONLY when `?fixture=` is active) */}
      {isFixtureMode && (
        <div
          role="region"
          aria-label="Synthetic fixture preview banner"
          className="rounded-xl border-2 border-amber-400 bg-amber-50 px-3.5 sm:px-4 py-3 flex flex-col md:flex-row md:items-center md:justify-between gap-3 text-xs text-amber-950 shadow-xs min-w-0"
        >
          <div className="flex items-start gap-2.5 min-w-0">
            <FlaskConical size={16} className="text-amber-700 shrink-0 mt-0.5" />
            <div className="min-w-0 break-words">
              <div className="font-bold tracking-wide uppercase">
                Synthetic Fixture Preview Mode — Not Live Market or Broker Data
              </div>
              <div className="text-amber-900 mt-0.5">
                All backend polling and broker POST endpoints are disabled. Displaying deterministic
                offline test fixture <code>{fixtureScenario}</code>.
              </div>
            </div>
          </div>

          <div className="flex flex-col sm:flex-row sm:items-center gap-1.5 sm:gap-2 min-w-0 md:max-w-sm w-full md:w-auto">
            <label htmlFor="fixture-scenario-select" className="font-semibold shrink-0">
              Scenario:
            </label>
            <select
              id="fixture-scenario-select"
              value={fixtureScenario}
              onChange={(e) => {
                setFixtureScenario(e.target.value);
                setFixtureAnchorMs(Date.now());
              }}
              className="w-full min-w-0 max-w-full px-2.5 py-1.5 rounded-lg bg-white border border-amber-400 font-medium text-xs text-slate-900 truncate"
            >
              {FIXTURE_SCENARIOS.map((sc) => (
                <option key={sc.id} value={sc.id}>
                  {sc.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}

      {/* Persistent Status & Separated Operator Controls Header */}
      <WorkspaceHeader
        executionStatus={executionStatus}
        statusData={statusData}
        activeReadiness={activeReadiness}
        liveStatus={liveStatus}
        sliceMeta={sliceMeta}
        openPaperCount={openPaperCount}
        pendingLeg2Count={pendingLeg2Count}
        unwindCount={unwindCount}
        sweeping={sweeping}
        armActionBusy={armActionBusy}
        armActionMsg={armActionMsg}
        onClearArmMsg={() => setArmActionMsg(null)}
        onOpenArmModal={() => {
          setArmPinInput('');
          setArmModalOpen(true);
        }}
        onDisarm={handleDisarm}
        onSosExit={handleSosPanicExit}
        onManualSweep={handleManualSweep}
      />

      {/* Primary 5-Workspace Navigation (Defaults to Overview) */}
      <nav
        aria-label="Option Sniffer Advance workspaces"
        className="bg-white rounded-xl border border-slate-200 p-1.5 shadow-2xs overflow-x-auto"
      >
        <div role="tablist" className="flex items-center gap-1 min-w-max">
          {WORKSPACE_TABS.map((tab) => {
            const Icon = tab.icon;
            const isSelected = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                role="tab"
                type="button"
                aria-selected={isSelected}
                onClick={() => setActiveTab(tab.id)}
                className={`px-3.5 py-2 rounded-lg text-xs font-semibold inline-flex items-center gap-1.5 transition cursor-pointer ${
                  isSelected
                    ? 'bg-slate-900 text-white shadow-2xs'
                    : 'text-slate-600 hover:text-slate-900 hover:bg-slate-100'
                }`}
              >
                <Icon size={14} />
                <span>{tab.label}</span>
              </button>
            );
          })}
        </div>
      </nav>

      {/* Active Workspace Panel */}
      <main>
        {activeTab === 'overview' && (
          <OverviewTab
            executionStatus={executionStatus}
            liveStatus={liveStatus}
            tradesData={tradesData}
            activeReadiness={activeReadiness}
            shadowTelemetry={shadowTelemetry}
            sliceMeta={sliceMeta}
            onSelectTab={setActiveTab}
          />
        )}

        {activeTab === 'signals' && (
          <SignalsTab
            activeReadiness={activeReadiness}
            signalsData={signalsData}
            sliceMeta={sliceMeta}
          />
        )}

        {activeTab === 'positions' && (
          <PositionsTab
            executionStatus={executionStatus}
            liveStatus={liveStatus}
            tradesData={tradesData}
            shadowTelemetry={shadowTelemetry}
            sliceMeta={sliceMeta}
            extraBrokerIntents={extraBrokerIntents}
            loadingMoreIntents={loadingMoreIntents}
            onLoadMoreBrokerIntents={handleLoadMoreBrokerIntents}
          />
        )}

        {activeTab === 'performance' && (
          <PerformanceTab
            tradesData={tradesData}
            shadowTelemetry={shadowTelemetry}
            liveStatus={liveStatus}
            calendarsByEra={statusData?.calendars_by_era || null}
            sliceMeta={sliceMeta}
            defaultCurrentMonth={defaultCurrentMonth}
          />
        )}

        {activeTab === 'diagnostics' && (
          <DiagnosticsTab
            statusData={statusData}
            activeReadiness={activeReadiness}
            shadowTelemetry={shadowTelemetry}
            liveStatus={liveStatus}
            sliceMeta={sliceMeta}
          />
        )}
      </main>

      {/* Accessible Human Confirmation Interlock Dialog */}
      <ArmInterlockModal
        isOpen={armModalOpen}
        liveStatus={liveStatus}
        armPinInput={armPinInput}
        onChangePinInput={setArmPinInput}
        armActionBusy={armActionBusy}
        onSelectMode={handleSelectMode}
        onConfirmArm={handleConfirmArm}
        onClose={() => {
          if (!armActionBusy) {
            setArmModalOpen(false);
            setArmPinInput('');
          }
        }}
      />
    </div>
  );
}
