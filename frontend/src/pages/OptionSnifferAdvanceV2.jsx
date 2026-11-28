import React, { useState, useEffect, useCallback, useRef } from 'react';
import { fetchAllV2EndpointsReadOnly } from './optionSnifferV2/v2ApiClient';
import {
  fetchV2LiveStatus,
  fetchV2LivePositions,
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
  const [loading, setLoading] = useState(true);
  const [armModalOpen, setArmModalOpen] = useState(false);
  const refreshBusyRef = useRef(false);

  const refreshAll = useCallback(async () => {
    if (refreshBusyRef.current) return;
    refreshBusyRef.current = true;
    try {
      const [paperRes, lStatus, lPos] = await Promise.all([
        fetchAllV2EndpointsReadOnly(),
        fetchV2LiveStatus(true).catch(() => null),
        fetchV2LivePositions().catch(() => null),
      ]);
      setEndpoints(paperRes);
      if (lStatus) setLiveStatus(lStatus);
      if (lPos) setLivePositions(lPos);
    } finally {
      refreshBusyRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshAll();
    const timer = setInterval(refreshAll, 10000);
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
          liveStatus={liveStatus}
          isLoading={loading}
          onRefreshAll={refreshAll}
          onOpenArmModal={() => setArmModalOpen(true)}
        />

        <main>
          {activeTab === 'overview' && (
            <V2OverviewTab overview={overview} liveStatus={liveStatus} />
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
          liveStatus={liveStatus}
          onCompleted={refreshAll}
          onRefreshStatus={refreshAll}
        />
      </div>
    </div>
  );
}
