import React, { useState } from 'react';
import axios from 'axios';
import { API_BASE } from '../../services/api';
import PipelineHeader from './PipelineHeader';
import PipelineFlow from './PipelineFlow';
import PipelineStageDrawer from './PipelineStageDrawer';

export default function PipelineHealth({ initialTimeframe = 'intraday', onDiagnosticComplete }) {
  const [selectedTf, setSelectedTf] = useState(initialTimeframe);
  const [diagRunning, setDiagRunning] = useState(false);
  const [diagResult, setDiagResult] = useState(null);
  const [diagError, setDiagError] = useState(null);
  const [selectedStage, setSelectedStage] = useState(null);

  const handleRunDiagnostic = async () => {
    setDiagRunning(true);
    setDiagError(null);
    try {
      const res = await axios.post(`${API_BASE}/system/pipeline-test?timeframe=${selectedTf}`);
      if (res.data) {
        setDiagResult(res.data);
        if (onDiagnosticComplete) {
          onDiagnosticComplete(res.data);
        }
      }
    } catch (err) {
      console.error('Diagnostic error:', err);
      setDiagError(err.response?.data?.detail || err.message || 'Pipeline diagnostic failed');
    } finally {
      setDiagRunning(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header with controls and summary telemetry */}
      <PipelineHeader
        selectedTf={selectedTf}
        onSelectTf={setSelectedTf}
        onRunDiagnostic={handleRunDiagnostic}
        diagRunning={diagRunning}
        diagResult={diagResult}
      />

      {/* Error notification if diagnostic call fails */}
      {diagError && (
        <div className="p-4 rounded-xl bg-rose-950/60 border border-rose-800 text-rose-300 text-xs font-mono">
          <strong>Diagnostic Sweeper Failure:</strong> {diagError}
        </div>
      )}

      {/* Dynamic Pipeline Flow Diagram */}
      <PipelineFlow
        diagResult={diagResult}
        onSelectStage={(stage) => setSelectedStage(stage)}
        selectedStageId={selectedStage?.stage_id}
      />

      {/* Deep Inspection Detail Drawer */}
      {selectedStage && (
        <PipelineStageDrawer
          stage={selectedStage}
          onClose={() => setSelectedStage(null)}
        />
      )}
    </div>
  );
}
