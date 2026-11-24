import React from 'react';

export default function V2AuditTab({ replayAudit, diagnostics }) {
  const o3 = (replayAudit && replayAudit.phase4_o3_audit) || {};
  const conflicts = o3.spec_conflicts || [];
  const inv = (diagnostics && diagnostics.upstox_entitlement_field_inventory) || [];
  const prot = (diagnostics && diagnostics.protected_artifacts_integrity) || {};
  const existingV2 = (diagnostics && diagnostics.existing_v2_systems_inventory) || [];
  const cmds = (diagnostics && diagnostics.operator_activation_commands) || {};

  return (
    <div className="space-y-4">
      {/* Phase-4 O3 6-Conflict Matrix */}
      <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <span className="text-xs font-bold uppercase tracking-wider text-rose-300 font-mono">
            Phase-4 O3 Reproduction &amp; Spec Conflicts Blocking Autonomous Book A
          </span>
          <span className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">
            {o3.reproduction_status || 'UNAVAILABLE'}
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-xs font-mono">
            <thead>
              <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                <th className="py-2 pr-3">Rule / Parameter</th>
                <th className="py-2 pr-3">Phase-4 Frozen O3 Evidence</th>
                <th className="py-2 pr-3">Proposed V2 Book A</th>
                <th className="py-2">Impact</th>
              </tr>
            </thead>
            <tbody>
              {conflicts.map((c, idx) => (
                <tr key={idx} className="border-b border-slate-800/80 text-slate-200 align-top">
                  <td className="py-2 pr-3 font-semibold text-white">{c.field}</td>
                  <td className="py-2 pr-3 text-slate-300">{c.phase4_o3_evidence}</td>
                  <td className="py-2 pr-3 text-amber-300">{c.proposed_v2_book_a}</td>
                  <td className="py-2 text-slate-400">{c.impact}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Upstox Field Inventory */}
      <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
        <span className="text-xs font-bold uppercase tracking-wider text-slate-200 font-mono">
          Upstox Account Entitlement Field Inventory
        </span>
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse text-[11px] font-mono">
            <thead>
              <tr className="border-b border-slate-700 text-slate-400 uppercase">
                <th className="py-2 pr-3">Field Group</th>
                <th className="py-2 pr-3">Role</th>
                <th className="py-2 pr-3">Source</th>
                <th className="py-2">Missing / Fail Policy</th>
              </tr>
            </thead>
            <tbody>
              {inv.map((f, idx) => (
                <tr key={idx} className="border-b border-slate-800/80 text-slate-200 align-top">
                  <td className="py-2 pr-3 font-semibold text-white">{f.field}</td>
                  <td className="py-2 pr-3">
                    <span className="px-1.5 py-0.5 rounded bg-slate-800 text-sky-300">{f.decision_role}</span>
                  </td>
                  <td className="py-2 pr-3 text-slate-300">{f.source}</td>
                  <td className="py-2 text-amber-300">{f.missing_policy}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Protected Hashes & Pre-Existing V2 Systems */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2 font-mono text-xs">
          <div className="flex items-center justify-between">
            <span className="font-bold text-slate-200 uppercase">Protected Artifact Integrity</span>
            <span className={prot.all_unchanged ? 'text-emerald-400' : 'text-rose-400'}>
              {prot.all_unchanged ? 'ALL 6 UNCHANGED' : 'HASH MISMATCH'}
            </span>
          </div>
          <div className="space-y-1 text-[10px] text-slate-400 break-all">
            {Object.entries(prot.hashes || {}).map(([k, v]) => (
              <div key={k}>
                <span className="text-slate-300">{k}:</span> {v || 'MISSING'}
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2 font-mono text-xs">
          <span className="font-bold text-slate-200 uppercase">Operator Commands &amp; Existing V2 Inventory</span>
          <div className="space-y-1 text-[10px] text-slate-300 bg-slate-950/60 p-2 rounded border border-slate-800">
            {Object.entries(cmds).map(([k, v]) => (
              <div key={k}>
                <span className="text-sky-300">{k}:</span> {v}
              </div>
            ))}
          </div>
          <div className="text-[10px] text-slate-400 pt-1">
            Pre-existing &quot;V2&quot; symbols preserved untouched: {existingV2.map((x) => x.identifier).join(', ') || 'None'}
          </div>
        </div>
      </div>
    </div>
  );
}
