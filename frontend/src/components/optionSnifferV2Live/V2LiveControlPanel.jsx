import React, { useCallback, useEffect, useState } from 'react';
import { Shield, ShieldOff, RefreshCw, AlertTriangle, LogOut, FileText } from 'lucide-react';
import V2LiveArmModal from './V2LiveArmModal';
import {
  fetchV2LiveAuditLog,
  fetchV2LiveStatus,
  postV2LiveArm,
  postV2LiveDisarm,
  postV2LiveExitPosition,
  postV2LiveReconcile,
} from './v2LiveApiClient';

const fmtInr = (val) => {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return '—';
  return `₹${Number(val).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

export default function V2LiveControlPanel({ paperOpenCount = 0 }) {
  const [liveStatus, setLiveStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState('');
  const [actionMsg, setActionMsg] = useState('');
  const [armModalOpen, setArmModalOpen] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const [auditEntries, setAuditEntries] = useState([]);
  const [busy, setBusy] = useState(false);

  const loadLiveStatus = useCallback(async () => {
    try {
      const data = await fetchV2LiveStatus(true);
      setLiveStatus(data);
      setErrorMsg('');
    } catch (err) {
      setErrorMsg(err?.message || 'V2 live control service unavailable (paper baseline unaffected).');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadLiveStatus();
    const timer = setInterval(loadLiveStatus, 10000);
    return () => clearInterval(timer);
  }, [loadLiveStatus]);

  const handleToggleAudit = async () => {
    const next = !showAudit;
    setShowAudit(next);
    if (next) {
      try {
        const res = await fetchV2LiveAuditLog(40);
        setAuditEntries(res.entries || []);
      } catch (err) {
        setErrorMsg(err?.message || 'Failed to load live audit log.');
      }
    }
  };

  const handleArm = async (payload) => {
    const updated = await postV2LiveArm(payload);
    setLiveStatus(updated);
    setActionMsg('V2 live execution ARMED by human PIN confirmation.');
  };

  const handleDisarm = async () => {
    setBusy(true);
    try {
      const updated = await postV2LiveDisarm('HUMAN_UI_DISARM');
      setLiveStatus(updated);
      setActionMsg('V2 live execution DISARMED. New entries blocked; open position exits remain enabled.');
    } catch (err) {
      setErrorMsg(err?.message || 'Disarm failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleReconcile = async () => {
    setBusy(true);
    try {
      const rep = await postV2LiveReconcile();
      await loadLiveStatus();
      setActionMsg(
        `Reconcile done: ${rep.resolved_intents?.length || 0} intent(s) checked, ${
          rep.unresolved_faults?.total_blocking_faults ?? 0
        } blocking fault(s) remaining.`
      );
    } catch (err) {
      setErrorMsg(err?.message || 'Reconciliation failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleExitPosition = async (livePackageId) => {
    setBusy(true);
    try {
      const res = await postV2LiveExitPosition(livePackageId, { reason: 'HUMAN_UI_SQUARE_OFF' });
      await loadLiveStatus();
      setActionMsg(`Square-off result for ${livePackageId}: ${res.status}`);
    } catch (err) {
      setErrorMsg(err?.message || `Square-off failed for ${livePackageId}`);
    } finally {
      setBusy(false);
    }
  };

  const armed = Boolean(liveStatus?.armed);
  const armState = liveStatus?.arm_state || {};
  const faults = liveStatus?.unresolved_faults || { total_blocking_faults: 0, items: [] };
  const openPkgs = liveStatus?.open_packages || [];

  return (
    <div
      className={`rounded-2xl border p-4 space-y-3 ${
        armed ? 'border-rose-500/60 bg-rose-950/15' : 'border-slate-700/80 bg-slate-900/75'
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          {armed ? (
            <Shield className="w-5 h-5 text-rose-400" />
          ) : (
            <ShieldOff className="w-5 h-5 text-emerald-400" />
          )}
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-bold uppercase tracking-wider text-slate-200">
                V2 Isolated Live Execution Layer
              </span>
              <span
                className={`px-2 py-0.5 rounded-full text-[11px] font-bold ${
                  armed
                    ? 'bg-rose-500/20 text-rose-300 border border-rose-500/40'
                    : 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
                }`}
              >
                {loading
                  ? 'CHECKING...'
                  : armed
                    ? 'ARMED • REAL BROKER ORDERS ENABLED'
                    : 'DISARMED • PAPER BASELINE ACTIVE'}
              </span>
              <span className="text-[11px] text-slate-400">
                Book: {(armState.selected_books || ['V2_BOOK_B_PAID_FLOW']).join(', ') || 'None'} • Limits:{' '}
                {armState.max_lots_per_book || 1} lot(s), DD cap {fmtInr(armState.max_daily_loss_inr || 2500)}
              </span>
            </div>
            <div className="text-[11px] text-slate-400 mt-0.5">
              Separate SQLite ledger (`option_sniffer_advance_v2_live.db`) • Boots disarmed • Realized:{' '}
              <span className="text-slate-200 font-semibold">{fmtInr(liveStatus?.daily_realized_net_inr)}</span> • Live Open MTM:{' '}
              <span className="text-slate-200 font-semibold">{fmtInr(liveStatus?.daily_unrealized_mtm_inr)}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <button
            type="button"
            disabled={busy}
            onClick={handleReconcile}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800/80 hover:bg-slate-700 text-xs font-medium text-slate-200"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${busy ? 'animate-spin' : ''}`} />
            Reconcile
          </button>
          <button
            type="button"
            onClick={handleToggleAudit}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800/80 hover:bg-slate-700 text-xs font-medium text-slate-200"
          >
            <FileText className="w-3.5 h-3.5" />
            {showAudit ? 'Hide Audit' : 'Audit Log'}
          </button>
          {armed ? (
            <button
              type="button"
              disabled={busy}
              onClick={handleDisarm}
              className="px-3.5 py-1.5 rounded-lg bg-amber-600 hover:bg-amber-500 text-xs font-bold text-white"
            >
              Disarm Live (Keep Exiting Allowed)
            </button>
          ) : (
            <button
              type="button"
              onClick={() => setArmModalOpen(true)}
              className="px-3.5 py-1.5 rounded-lg bg-rose-600 hover:bg-rose-500 text-xs font-bold text-white"
            >
              Arm Live Execution (Human PIN)
            </button>
          )}
        </div>
      </div>

      {faults.total_blocking_faults > 0 && (
        <div className="rounded-xl border border-rose-500/50 bg-rose-950/35 px-3 py-2 text-xs text-rose-200 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" />
          <span>
            <strong>{faults.total_blocking_faults} Unresolved Fault(s):</strong> New entries are halted until
            reconciled ({faults.items.map((f) => `${f.kind}:${f.state}`).join('; ')}).
          </span>
        </div>
      )}

      {errorMsg && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-950/30 px-3 py-1.5 text-xs text-amber-200">
          {errorMsg}
        </div>
      )}
      {actionMsg && (
        <div className="rounded-lg border border-cyan-500/40 bg-cyan-950/30 px-3 py-1.5 text-xs text-cyan-200">
          {actionMsg}
        </div>
      )}

      {openPkgs.length > 0 && (
        <div className="rounded-xl border border-slate-700/80 bg-slate-950/70 p-3 space-y-2">
          <div className="text-[11px] font-bold uppercase text-amber-300">
            Open / Active Live Packages ({openPkgs.length}) — Exits Allowed Even When Disarmed
          </div>
          <div className="space-y-2">
            {openPkgs.map((pkg) => {
              const buyLeg = pkg.legs?.buy_leg || {};
              const shortLeg = pkg.legs?.short_leg || {};
              return (
                <div
                  key={pkg.live_package_id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-900/70 px-3 py-2 text-xs"
                >
                  <div>
                    <div className="font-semibold text-slate-100">
                      {buyLeg.trading_symbol || pkg.live_package_id} ({pkg.structure}) •{' '}
                      <span className="text-cyan-300">{pkg.state}</span>
                    </div>
                    <div className="text-[11px] text-slate-400">
                      Buy {buyLeg.qty || 0} @ {buyLeg.entry_price || '—'}
                      {shortLeg.qty ? ` / Short ${shortLeg.qty} @ ${shortLeg.entry_price || '—'}` : ''}
                      {buyLeg.gtt_id ? ` • GTT ${buyLeg.gtt_id}` : ''}
                      {pkg.unresolved_fault ? ` • Fault: ${pkg.unresolved_fault}` : ''}
                    </div>
                  </div>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => handleExitPosition(pkg.live_package_id)}
                    className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-rose-700 hover:bg-rose-600 text-xs font-bold text-white"
                  >
                    <LogOut className="w-3.5 h-3.5" />
                    Square Off
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {showAudit && (
        <div className="rounded-xl border border-slate-800 bg-slate-950/80 p-3 max-h-56 overflow-y-auto text-[11px]">
          <div className="font-semibold text-slate-300 mb-1.5">Recent Live Audit Trail</div>
          {auditEntries.length === 0 ? (
            <div className="text-slate-500">No live audit events recorded yet (cold isolated ledger).</div>
          ) : (
            <div className="space-y-1">
              {auditEntries.map((ev) => (
                <div key={ev.id} className="flex items-start justify-between gap-2 border-b border-slate-800/60 pb-1">
                  <span className="text-cyan-300 font-mono">{ev.event_type}</span>
                  <span className="text-slate-400 truncate max-w-md">{JSON.stringify(ev.detail)}</span>
                  <span className="text-slate-500 shrink-0">{ev.ts_iso}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      <V2LiveArmModal
        isOpen={armModalOpen}
        onClose={() => setArmModalOpen(false)}
        liveStatus={liveStatus}
        paperOpenCount={paperOpenCount}
        onConfirmArm={handleArm}
      />
    </div>
  );
}
