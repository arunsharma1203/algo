import React, { useState, useEffect, useCallback, useRef } from 'react';
import { fetchAllV2EndpointsReadOnly } from './optionSnifferV2/v2ApiClient';
import {
  fetchV2LiveStatus,
  fetchV2LivePositions,
  fetchV2LiveFundsPreview,
} from '../components/optionSnifferV2Live/v2LiveApiClient';
import V2WorkspaceHeader from '../components/optionSnifferV2Live/V2WorkspaceHeader';
import V2LiveArmModal from '../components/optionSnifferV2Live/V2LiveArmModal';
import V2LivePositionsPanel from '../components/optionSnifferV2Live/V2LivePositionsPanel';
import V2OverviewTab from './optionSnifferV2/V2OverviewTab';
import V2SignalsTab from './optionSnifferV2/V2SignalsTab';
import V2PositionsTab from './optionSnifferV2/V2PositionsTab';
import V2AuditTab from './optionSnifferV2/V2AuditTab';

export default function OptionSnifferAdvanceV2() {
  const [activeTab, setActiveTab] = useState('overview');
  const [endpoints, setEndpoints] = useState(null);
  const [liveStatus, setLiveStatus] = useState(null);
  const [livePositions, setLivePositions] = useState(null);
  const [fundsPreview, setFundsPreview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [armModalOpen, setArmModalOpen] = useState(false);
  const refreshBusyRef = useRef(false);
  const endpointsRef = useRef({});

  const refreshAll = useCallback(async ({ forceFunds = false } = {}) => {
    if (refreshBusyRef.current) return;
    refreshBusyRef.current = true;
    try {
      const [paperRes, lStatus, lPos, lFunds] = await Promise.all([
        fetchAllV2EndpointsReadOnly({
          previousState: endpointsRef.current || {},
          keys: ['overview', 'positions', 'diagnostics', 'opportunities'],
        }),
        fetchV2LiveStatus(false).catch(() => null),
        fetchV2LivePositions().catch(() => null),
        fetchV2LiveFundsPreview(forceFunds).catch(() => null),
      ]);
      endpointsRef.current = paperRes;
      setEndpoints(paperRes);
      if (lStatus) setLiveStatus(lStatus);
      if (lPos) setLivePositions(lPos);
      if (lFunds) setFundsPreview(lFunds);
    } finally {
      refreshBusyRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshAll();
    const timer = setInterval(() => refreshAll(), 10000);
    return () => clearInterval(timer);
  }, [refreshAll]);

  const overview = endpoints?.overview?.data || null;
  const positions = endpoints?.positions?.data || null;
  const diagnostics = endpoints?.diagnostics?.data || null;
  const opportunities = endpoints?.opportunities?.data || null;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 px-4 py-5 sm:px-6">
      <div className="max-w-7xl mx-auto">
        <V2WorkspaceHeader
          activeTab={activeTab}
          onSelectTab={setActiveTab}
          overview={overview}
          endpoints={endpoints}
          liveStatus={liveStatus}
          fundsPreview={fundsPreview}
          isLoading={loading}
          onRefreshAll={() => refreshAll({ forceFunds: true })}
          onOpenArmModal={() => setArmModalOpen(true)}
        />

        <main>
          {activeTab === 'overview' && (
            <V2OverviewTab
              overview={overview}
              liveStatus={liveStatus}
              fundsPreview={fundsPreview}
            />
          )}
          {activeTab === 'signals' && (
            <V2SignalsTab overview={overview} opportunities={opportunities} />
          )}
          {activeTab === 'positions' && (
            <V2PositionsTab
              positions={positions}
              liveSubPanel={
                <V2LivePositionsPanel
                  liveStatus={liveStatus}
                  livePositions={livePositions}
                  onRefreshAll={refreshAll}
                />
              }
            />
          )}
          {activeTab === 'health' && (
            <V2AuditTab overview={overview} diagnostics={diagnostics} liveStatus={liveStatus} />
          )}
        </main>

        <V2LiveArmModal
          isOpen={armModalOpen}
          onClose={() => setArmModalOpen(false)}
          overview={overview}
          liveStatus={liveStatus}
          fundsPreview={fundsPreview}
          onCompleted={() => refreshAll({ forceFunds: true })}
          onRefreshStatus={() => refreshAll({ forceFunds: true })}
        />
      </div>
    </div>
  );
}
