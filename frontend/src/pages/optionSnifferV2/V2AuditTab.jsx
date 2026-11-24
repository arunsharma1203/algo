import React from 'react';
import { fmtInr } from './v2Fixtures';

export default function V2AuditTab({
  overview,
  replayAudit,
  diagnostics,
  diagnosticsMeta,
  includeCollateral,
  setIncludeCollateral,
  collateralYieldPa,
  setCollateralYieldPa,
  haircutPct,
  setHaircutPct,
}) {
  const o3 = replayAudit?.phase4_o3_audit || {};
  const conflicts = o3.spec_conflicts || [];
  const inv = diagnostics?.upstox_entitlement_field_inventory || [];
  const prot = diagnostics?.protected_artifacts_integrity;
  const existingV2 = diagnostics?.existing_v2_systems_inventory || [];
  const cmds = diagnostics?.operator_activation_commands || {};
  const coll = overview?.optional_collateral_reporting?.forward_paper;

  return (
    <div className="space-y-4 font-mono text-xs">
      {/* Protected Artifact Integrity — NEVER fabricated when diagnostics is unavailable */}
      <details open className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-2.5">
        <summary className="cursor-pointer font-bold uppercase text-slate-200 flex items-center justify-between flex-wrap gap-2">
          <span>Protected Artifact SHA-256 Integrity</span>
          {!diagnostics ? (
            <span className="text-amber-300">
              Unavailable {diagnosticsMeta?.error ? `(${diagnosticsMeta.error})` : ''}
            </span>
          ) : (
            <span className={prot?.all_unchanged ? 'text-emerald-400' : 'text-rose-400'}>
              {prot?.all_unchanged ? 'ALL 6 UNCHANGED' : 'HASH MISMATCH'}
            </span>
          )}
        </summary>
        {!diagnostics ? (
          <div className="text-slate-400 pt-2">
            Diagnostics endpoint unavailable; protected artifact verification is unknown/unavailable rather than assumed unchanged.
          </div>
        ) : (
          <div className="space-y-1 text-[11px] text-slate-400 break-all pt-2 border-t border-slate-800">
            {Object.entries(prot?.hashes || {}).map(([k, v]) => (
              <div key={k}>
                <span className="text-slate-300">{k}:</span> {v || 'MISSING'}
              </div>
            ))}
          </div>
        )}
      </details>

      {/* Phase-4 O3 6-Conflict Disclosure */}
      <details open className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
        <summary className="cursor-pointer font-bold uppercase text-rose-300 flex items-center justify-between flex-wrap gap-2">
          <span>Phase-4 O3 Spec Conflicts Blocking Autonomous Book A</span>
          <span className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300">
            {replayAudit ? o3.reproduction_status || 'UNAVAILABLE' : 'Unavailable'}
          </span>
        </summary>
        {!replayAudit ? (
          <div className="text-slate-400 pt-2">Replay audit unavailable.</div>
        ) : (
          <div className="overflow-x-auto pt-2 border-t border-slate-800">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-slate-700 text-[10px] text-slate-400 uppercase">
                  <th className="py-2 pr-3">Rule / Parameter</th>
                  <th className="py-2 pr-3">Phase-4 Evidence</th>
                  <th className="py-2 pr-3">Proposed Book A</th>
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
        )}
      </details>

      {/* Upstox Entitlement Field Inventory */}
      <details className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
        <summary className="cursor-pointer font-bold uppercase text-slate-200">
          Upstox Account Entitlement Field Inventory ({inv.length} field groups)
        </summary>
        {!diagnostics ? (
          <div className="text-slate-400 pt-2">Diagnostics unavailable.</div>
        ) : (
          <div className="overflow-x-auto pt-2 border-t border-slate-800">
            <table className="w-full text-left border-collapse text-[11px]">
              <thead>
                <tr className="border-b border-slate-700 text-slate-400 uppercase">
                  <th className="py-2 pr-3">Field Group</th>
                  <th className="py-2 pr-3">Role</th>
                  <th className="py-2 pr-3">Source</th>
                  <th className="py-2">Fail-Closed Policy</th>
                </tr>
              </thead>
              <tbody>
                {inv.map((f, idx) => (
                  <tr key={idx} className="border-b border-slate-800/80 text-slate-200 align-top">
                    <td className="py-2 pr-3 font-semibold text-white">{f.field}</td>
                    <td className="py-2 pr-3 text-sky-300">{f.decision_role}</td>
                    <td className="py-2 pr-3 text-slate-300">{f.source}</td>
                    <td className="py-2 text-amber-300">{f.missing_policy}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </details>

      {/* Optional Reporting-Only Collateral Scenario & Operator Commands */}
      <details className="rounded-xl border border-slate-700/80 bg-slate-900/70 p-4 space-y-3">
        <summary className="cursor-pointer font-bold uppercase text-slate-200">
          Optional Reporting-Only Collateral Yield &amp; CLI Commands
        </summary>
        <div className="pt-2 border-t border-slate-800 space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-amber-300">
              {coll?.classification_label || 'ASSUMED_YIELD_NOT_TRADING_ALPHA'}
            </span>
            <label className="flex items-center gap-2 text-slate-200 cursor-pointer">
              <input
                type="checkbox"
                checked={includeCollateral}
                onChange={(e) => setIncludeCollateral(e.target.checked)}
                className="rounded border-slate-600 bg-slate-950 text-sky-500"
              />
              Enable Reporting Scenario
            </label>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
            <label className="flex flex-col gap-1 text-slate-400">
              <span>Yield (% p.a.)</span>
              <input
                type="number"
                step="0.25"
                value={collateralYieldPa}
                onChange={(e) => setCollateralYieldPa(Number(e.target.value))}
                className="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-white"
              />
            </label>
            <label className="flex flex-col gap-1 text-slate-400">
              <span>Haircut (%)</span>
              <input
                type="number"
                step="1"
                value={haircutPct}
                onChange={(e) => setHaircutPct(Number(e.target.value))}
                className="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-white"
              />
            </label>
            <div className="p-2 rounded bg-slate-950/60 border border-slate-800">
              <div className="text-[10px] text-slate-400">Hypothetical Collateral (Not Alpha)</div>
              <div className="text-slate-100 font-semibold mt-0.5">
                {coll ? fmtInr(coll.hypothetical_collateral_income_inr) : 'Unavailable'}
              </div>
            </div>
          </div>
          <div className="space-y-1 text-[11px] text-slate-300 bg-slate-950/60 p-2.5 rounded border border-slate-800 break-words">
            {Object.entries(cmds).map(([k, v]) => (
              <div key={k}>
                <span className="text-sky-300">{k}:</span> {v}
              </div>
            ))}
            <div className="text-slate-400 pt-1">
              Pre-existing &quot;V2&quot; symbols untouched: {existingV2.map((x) => x.identifier).join(', ') || 'None'}
            </div>
          </div>
        </div>
      </details>
    </div>
  );
}
