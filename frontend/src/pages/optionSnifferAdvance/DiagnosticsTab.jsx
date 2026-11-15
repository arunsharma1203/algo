import React, { useState } from 'react';
import {
  Activity,
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Cpu,
  Database,
  Server,
  ShieldCheck,
} from 'lucide-react';
import {
  UNAVAILABLE_LABEL,
  formatClockIST,
  formatINRValue,
  formatNumber,
  formatPercent,
  formatTimestampLabel,
  hasPositiveNumber,
  hasValidNumber,
} from './formatters';

function MetricRow({ label, value, mono = true }) {
  return (
    <div className="flex items-center justify-between gap-2 py-1 border-b border-slate-100 last:border-b-0 text-xs">
      <span className="text-slate-500">{label}</span>
      <span className={`${mono ? 'font-mono' : ''} font-semibold text-slate-900 tabular-nums text-right`}>
        {value ?? UNAVAILABLE_LABEL}
      </span>
    </div>
  );
}

function ChainCard({ symbol, chainData, retrievedAtMs }) {
  const d = chainData && typeof chainData === 'object' ? chainData : {};
  const hasSnapshot = Boolean(d.timestamp) || hasPositiveNumber(d.spot_ltp);

  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/50 p-4 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-slate-200">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-bold text-slate-900">{symbol} Chain Intelligence</h3>
            <span className="px-2 py-0.5 rounded text-[10px] font-mono bg-white border border-slate-200 text-slate-700">
              {d.gex_regime || UNAVAILABLE_LABEL}
            </span>
          </div>
          <div className="text-[11px] font-mono text-slate-500 mt-0.5">
            Snapshot: {formatTimestampLabel(d.timestamp)} • Retrieved:{' '}
            {retrievedAtMs ? formatClockIST(retrievedAtMs) : UNAVAILABLE_LABEL}
          </div>
        </div>
        <div className="text-right font-mono tabular-nums">
          <div className="text-xs text-slate-500">Spot / Session TPB</div>
          <div className="text-sm font-bold text-slate-900">
            {hasPositiveNumber(d.spot_ltp) ? formatNumber(d.spot_ltp) : UNAVAILABLE_LABEL}
            {' / '}
            {hasPositiveNumber(d.session_tpb) ? formatNumber(d.session_tpb) : UNAVAILABLE_LABEL}
          </div>
        </div>
      </div>

      {!hasSnapshot ? (
        <div className="p-3 rounded-lg bg-white border border-slate-200 text-xs font-mono text-slate-500">
          Chain snapshot unavailable for {symbol}. Zero fallback constants applied.
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-0.5">
          <div>
            <MetricRow
              label="Synthetic Future / Basis"
              value={
                hasPositiveNumber(d.synthetic_future)
                  ? `${formatNumber(d.synthetic_future)} (${
                      hasValidNumber(d.synthetic_basis_bps)
                        ? formatNumber(d.synthetic_basis_bps, { signed: true, suffix: ' bps' })
                        : UNAVAILABLE_LABEL
                    })`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Net Dealer GEX (Cr)"
              value={
                hasValidNumber(d.net_gex_cr)
                  ? formatNumber(d.net_gex_cr, { signed: true, suffix: ' Cr' })
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="ATM IV / 15m RV"
              value={
                hasValidNumber(d.atm_iv) || hasValidNumber(d.rv_15m)
                  ? `${hasValidNumber(d.atm_iv) ? formatPercent(d.atm_iv, { decimals: 2 }) : UNAVAILABLE_LABEL} / ${
                      hasValidNumber(d.rv_15m) ? formatPercent(d.rv_15m, { decimals: 2 }) : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Vol Risk Premium (IV - RV)"
              value={
                hasValidNumber(d.vrp)
                  ? formatNumber(d.vrp, { decimals: 2, signed: true, suffix: ' vol pts' })
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="25D IV Skew"
              value={
                hasValidNumber(d.iv_skew_25d)
                  ? formatNumber(d.iv_skew_25d, { decimals: 2, signed: true })
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="PCR (OI / Volume)"
              value={
                hasValidNumber(d.pcr_oi) || hasValidNumber(d.pcr_vol)
                  ? `${hasValidNumber(d.pcr_oi) ? formatNumber(d.pcr_oi) : UNAVAILABLE_LABEL} / ${
                      hasValidNumber(d.pcr_vol) ? formatNumber(d.pcr_vol) : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
          </div>

          <div>
            <MetricRow
              label="Put Wall / Call Wall"
              value={
                hasPositiveNumber(d.put_wall_strike) || hasPositiveNumber(d.call_wall_strike)
                  ? `${hasPositiveNumber(d.put_wall_strike) ? d.put_wall_strike : UNAVAILABLE_LABEL} / ${
                      hasPositiveNumber(d.call_wall_strike) ? d.call_wall_strike : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Max Pain Strike"
              value={hasPositiveNumber(d.max_pain_strike) ? String(d.max_pain_strike) : UNAVAILABLE_LABEL}
            />
            <MetricRow
              label="Wilder ADX(14) / Kaufman ER"
              value={
                hasValidNumber(d.adx_14) || hasValidNumber(d.kaufman_er)
                  ? `${hasValidNumber(d.adx_14) ? formatNumber(d.adx_14, { decimals: 1 }) : UNAVAILABLE_LABEL} / ${
                      hasValidNumber(d.kaufman_er) ? formatNumber(d.kaufman_er, { decimals: 2 }) : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="ATR Ratio (5m / 20m)"
              value={
                hasValidNumber(d.atr_ratio_5_20)
                  ? `${formatNumber(d.atr_ratio_5_20, { decimals: 2 })}x`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="VWAP Drift / Chain OBI"
              value={
                hasValidNumber(d.vwap_drift_pct) || hasValidNumber(d.chain_obi)
                  ? `${
                      hasValidNumber(d.vwap_drift_pct)
                        ? formatPercent(d.vwap_drift_pct, { decimals: 2, signed: true })
                        : UNAVAILABLE_LABEL
                    } / ${
                      hasValidNumber(d.chain_obi)
                        ? formatNumber(d.chain_obi, { decimals: 2, signed: true })
                        : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Regime Tag"
              value={d.recommended_strategy || UNAVAILABLE_LABEL}
            />
          </div>
        </div>
      )}

      {d.regime_reason && (
        <div className="text-[11px] text-slate-600 bg-white border border-slate-200 rounded-lg px-3 py-1.5">
          {d.regime_reason}
        </div>
      )}
    </div>
  );
}

export default function DiagnosticsTab({
  statusData,
  activeReadiness,
  shadowTelemetry,
  liveStatus,
  sliceMeta,
}) {
  const [showReferenceDocs, setShowReferenceDocs] = useState(false);

  const v3 = activeReadiness?.upstox_v3_feed || {};
  const meta = activeReadiness?.meta_model || {};
  const timing = activeReadiness?.timing || {};
  const streamHealth = statusData?.stream_health || {};
  const chainMap = statusData?.chain_intelligence || {};

  const cert = shadowTelemetry?.certification_progress || {};
  const rowCounts = shadowTelemetry?.telemetry_row_counts || {};
  const marginProbe = shadowTelemetry?.latest_margin_probe || null;
  const fspWindow = shadowTelemetry?.latest_fsp_window || null;

  const readiness = liveStatus?.readiness || {};
  const parity = statusData?.runtime_parity || {};
  const bootHashes = parity.boot_hashes && typeof parity.boot_hashes === 'object' ? parity.boot_hashes : {};
  const diskHashes = parity.disk_hashes && typeof parity.disk_hashes === 'object' ? parity.disk_hashes : {};
  const hashFiles = Array.from(new Set([...Object.keys(bootHashes), ...Object.keys(diskHashes)])).sort();

  const gatekeepers = [
    {
      id: 'token_valid',
      label: 'Upstox Access Token Valid',
      status:
        typeof readiness.token_valid === 'boolean'
          ? readiness.token_valid
            ? 'PASS'
            : 'BLOCKED'
          : UNAVAILABLE_LABEL,
    },
    {
      id: 'nfo_enabled',
      label: 'NFO Derivatives Segment Enabled',
      status:
        typeof readiness.nfo_enabled === 'boolean'
          ? readiness.nfo_enabled
            ? 'PASS'
            : 'BLOCKED'
          : UNAVAILABLE_LABEL,
    },
    {
      id: 'static_ip_matched',
      label: 'Static IPv6 Egress Matched',
      status:
        typeof readiness.static_ip_matched === 'boolean'
          ? readiness.static_ip_matched
            ? 'PASS'
            : 'BLOCKED'
          : UNAVAILABLE_LABEL,
    },
    {
      id: 'mode_margin_ok',
      label: 'Configured Mode Margin Requirement Met',
      status:
        typeof liveStatus?.mode_margin_ok === 'boolean'
          ? liveStatus.mode_margin_ok
            ? 'PASS'
            : 'BLOCKED'
          : UNAVAILABLE_LABEL,
    },
  ];

  return (
    <div className="space-y-5">
      {/* PANEL 1: Live Upstox V3 Feed & Cross-Sectional Chain Intelligence */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <Activity size={16} className="text-slate-700" />
              <span>1. Live Upstox V3 Feed &amp; Cross-Sectional Chain Intelligence</span>
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Active runtime microstructure feed, causal ORB-30 bounds, meta-model fold identity,
              and NIFTY / BANKNIFTY chain snapshots.
            </p>
          </div>
          <div className="text-xs font-mono text-slate-500">
            Retrieved:{' '}
            {sliceMeta?.readiness?.lastSuccessMs
              ? formatClockIST(sliceMeta.readiness.lastSuccessMs)
              : UNAVAILABLE_LABEL}
          </div>
        </div>

        {/* V3 Feed & Engine Timing Strip */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/40">
            <div className="text-xs font-bold text-slate-800 mb-2">Index &amp; Futures Basis Feed</div>
            <MetricRow
              label="WebSocket Stream"
              value={
                typeof v3.websocket_connected === 'boolean'
                  ? v3.websocket_connected
                    ? 'Connected'
                    : 'Disconnected'
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="NIFTY Spot / TPB VWAP"
              value={
                hasPositiveNumber(v3.spot_ltp)
                  ? `${formatNumber(v3.spot_ltp)} / ${
                      hasPositiveNumber(v3.spot_tpb_vwap)
                        ? formatNumber(v3.spot_tpb_vwap)
                        : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label={`Front Future (${v3.fut_symbol || 'NFO'})`}
              value={
                hasPositiveNumber(v3.fut_ltp)
                  ? `${formatNumber(v3.fut_ltp)} (ATP ${
                      hasPositiveNumber(v3.fut_atp_vwap)
                        ? formatNumber(v3.fut_atp_vwap)
                        : UNAVAILABLE_LABEL
                    })`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Live Basis & Source"
              value={
                hasValidNumber(v3.real_basis_pts) && hasPositiveNumber(v3.fut_ltp)
                  ? `${formatNumber(v3.real_basis_pts, { signed: true, suffix: ' pts' })} (${
                      v3.basis_source || UNAVAILABLE_LABEL
                    })`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Futures OI / L5 OBI"
              value={
                hasPositiveNumber(v3.fut_oi) || hasValidNumber(v3.fut_obi)
                  ? `${
                      hasPositiveNumber(v3.fut_oi)
                        ? formatNumber(v3.fut_oi, { decimals: 0 })
                        : UNAVAILABLE_LABEL
                    } / ${
                      hasValidNumber(v3.fut_obi)
                        ? formatNumber(v3.fut_obi, { decimals: 2, signed: true })
                        : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Spot / Fut Tick Age"
              value={
                hasValidNumber(v3.spot_quote_age_ms) || hasValidNumber(v3.fut_quote_age_ms)
                  ? `${
                      hasValidNumber(v3.spot_quote_age_ms)
                        ? `${Math.max(0, Math.round(Number(v3.spot_quote_age_ms)))}ms`
                        : UNAVAILABLE_LABEL
                    } / ${
                      hasValidNumber(v3.fut_quote_age_ms)
                        ? `${Math.max(0, Math.round(Number(v3.fut_quote_age_ms)))}ms`
                        : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
          </div>

          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/40">
            <div className="text-xs font-bold text-slate-800 mb-2">Option Depth &amp; Causal ORB-30</div>
            <MetricRow
              label="India VIX"
              value={hasPositiveNumber(v3.india_vix) ? formatNumber(v3.india_vix) : UNAVAILABLE_LABEL}
            />
            <MetricRow
              label="Avg Option L5 Spread / OBI"
              value={
                hasValidNumber(v3.avg_opt_spread_pct) || hasValidNumber(v3.avg_l5_obi)
                  ? `${
                      hasValidNumber(v3.avg_opt_spread_pct)
                        ? formatPercent(v3.avg_opt_spread_pct, { decimals: 2 })
                        : UNAVAILABLE_LABEL
                    } / ${
                      hasValidNumber(v3.avg_l5_obi)
                        ? formatNumber(v3.avg_l5_obi, { decimals: 2, signed: true })
                        : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Subscribed / Valid Quotes"
              value={
                hasValidNumber(streamHealth.subscribed_keys_count) ||
                hasValidNumber(v3.active_nifty_contracts)
                  ? `${streamHealth.subscribed_keys_count ?? v3.active_nifty_contracts ?? UNAVAILABLE_LABEL} / ${
                      streamHealth.valid_quotes_count ?? UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="ORB-30 Range (09:15–09:45)"
              value={
                hasPositiveNumber(v3.indicators?.orb30_low) &&
                hasPositiveNumber(v3.indicators?.orb30_high)
                  ? `${formatNumber(v3.indicators.orb30_low, { decimals: 0 })} – ${formatNumber(
                      v3.indicators.orb30_high,
                      { decimals: 0 }
                    )}`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Feed Regime Tag"
              value={v3.indicators?.regime_tag || UNAVAILABLE_LABEL}
            />
          </div>

          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/40">
            <div className="text-xs font-bold text-slate-800 mb-2">Causal Loop &amp; Frozen Meta-Model</div>
            <MetricRow
              label="Last Bar Evaluated"
              value={formatTimestampLabel(timing.last_bar_evaluated_ts)}
            />
            <MetricRow
              label="1m Bars Collected / Latency"
              value={
                hasValidNumber(timing.bars_collected_today)
                  ? `${timing.bars_collected_today} bars (${
                      hasValidNumber(timing.last_eval_latency_ms)
                        ? `${formatNumber(timing.last_eval_latency_ms, { decimals: 1 })} ms`
                        : UNAVAILABLE_LABEL
                    })`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Candidate / Fold"
              value={
                meta.candidate_id
                  ? `${meta.candidate_id} • Fold ${meta.fold_id ?? UNAVAILABLE_LABEL} (${
                      meta.test_month || UNAVAILABLE_LABEL
                    })`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Embargoed Train End / p_min"
              value={
                meta.embargoed_train_end || hasValidNumber(meta.p_min)
                  ? `${meta.embargoed_train_end || UNAVAILABLE_LABEL} / ${
                      hasValidNumber(meta.p_min) ? formatNumber(meta.p_min) : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Weights SHA-256"
              value={meta.weights_sha256 ? String(meta.weights_sha256).slice(0, 16) : UNAVAILABLE_LABEL}
            />
          </div>
        </div>

        {/* NIFTY & BANKNIFTY Chain Cards */}
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <ChainCard
            symbol="NIFTY"
            chainData={chainMap.NIFTY}
            retrievedAtMs={sliceMeta?.status?.lastSuccessMs}
          />
          <ChainCard
            symbol="BANKNIFTY"
            chainData={chainMap.BANKNIFTY}
            retrievedAtMs={sliceMeta?.status?.lastSuccessMs}
          />
        </div>
      </section>

      {/* PANEL 2: Forward Collector Progress & Microstructure Tables */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <Database size={16} className="text-slate-700" />
              <span>
                2. Forward Collector Sample Progress &amp; Microstructure Telemetry (
                <code>S205_SHADOW_FORWARD_V1</code>)
              </span>
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Sample collection progress tracks accumulated trading sessions and closed forward
              packages—never strategy certification or positive expectancy.
            </p>
          </div>
          <span className="px-2.5 py-1 rounded text-[11px] font-mono bg-slate-100 text-slate-700 border border-slate-200">
            Sample Collection Progress (Not Strategy Certification)
          </span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Sample Counts & Protocol Hash */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/40">
            <div className="text-xs font-bold text-slate-800 mb-2">
              Pre-Registered Sample Counters
            </div>
            <MetricRow
              label="Sessions Collected"
              value={
                hasValidNumber(cert.sessions_collected) && hasValidNumber(cert.min_sessions_required)
                  ? `${cert.sessions_collected} / ${cert.min_sessions_required} min (target ${
                      cert.target_sessions ?? 90
                    })`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Closed Forward Packages"
              value={
                hasValidNumber(cert.closed_shadow_packages) && hasValidNumber(cert.min_trades_required)
                  ? `${cert.closed_shadow_packages} / ${cert.min_trades_required} min`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Go/No-Go Sample Threshold Met"
              value={
                typeof cert.ready_for_go_no_go_review === 'boolean'
                  ? cert.ready_for_go_no_go_review
                    ? 'Threshold Reached (Awaiting Review)'
                    : 'Collecting Samples'
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="L5 1m Rows / Quote-Fade Rows"
              value={
                hasValidNumber(rowCounts.l5_chain_1m_rows) ||
                hasValidNumber(rowCounts.quote_fade_event_rows)
                  ? `${
                      hasValidNumber(rowCounts.l5_chain_1m_rows)
                        ? formatNumber(rowCounts.l5_chain_1m_rows, { decimals: 0 })
                        : UNAVAILABLE_LABEL
                    } / ${
                      hasValidNumber(rowCounts.quote_fade_event_rows)
                        ? formatNumber(rowCounts.quote_fade_event_rows, { decimals: 0 })
                        : UNAVAILABLE_LABEL
                    }`
                  : UNAVAILABLE_LABEL
              }
            />
            <MetricRow
              label="Pre-Reg SHA-256"
              value={
                shadowTelemetry?.prereg_sha256
                  ? `${String(shadowTelemetry.prereg_sha256).slice(0, 16)}...`
                  : UNAVAILABLE_LABEL
              }
            />
          </div>

          {/* Latest Upstox SPAN Margin Probe */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/40">
            <div className="text-xs font-bold text-slate-800 mb-2">
              Latest Upstox SPAN Margin Probe ({rowCounts.upstox_margin_probe_rows ?? 0} recorded)
            </div>
            {!marginProbe ? (
              <div className="text-xs font-mono text-slate-500 py-4">
                No SPAN margin probe recorded yet. Local fallback margin estimates remain tagged
                UNVERIFIED_HEURISTIC_NOT_BROKER_SPAN.
              </div>
            ) : (
              <>
                <MetricRow
                  label="Probe Timestamp"
                  value={formatTimestampLabel(marginProbe.timestamp)}
                />
                <MetricRow
                  label="Verification Status"
                  value={marginProbe.margin_status || UNAVAILABLE_LABEL}
                />
                <MetricRow
                  label="SPAN / Exposure Margin"
                  value={`${formatINRValue(marginProbe.span_margin_inr, {
                    decimals: 0,
                  })} / ${formatINRValue(marginProbe.exposure_margin_inr, { decimals: 0 })}`}
                />
                <MetricRow
                  label="Final Required Margin"
                  value={formatINRValue(marginProbe.final_margin_inr, { decimals: 0 })}
                />
                <MetricRow
                  label="Wing Hedge Benefit"
                  value={formatINRValue(marginProbe.hedge_benefit_inr, { decimals: 0 })}
                />
              </>
            )}
          </div>

          {/* Latest Expiry 15:00-15:30 NCL FSP Settlement Window */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/40">
            <div className="text-xs font-bold text-slate-800 mb-2">
              Expiry 15:00–15:30 NCL FSP Window ({rowCounts.expiry_fsp_window_rows ?? 0} recorded)
            </div>
            {!fspWindow ? (
              <div className="text-xs font-mono text-slate-500 py-4">
                No expiry settlement TWAP window recorded yet.
              </div>
            ) : (
              <>
                <MetricRow
                  label="Expiry Date / Samples"
                  value={`${fspWindow.expiry_date || UNAVAILABLE_LABEL} (${
                    fspWindow.samples_count ?? UNAVAILABLE_LABEL
                  } ticks)`}
                />
                <MetricRow
                  label="15:00–15:30 Spot TWAP"
                  value={formatNumber(fspWindow.spot_1500_1530_twap)}
                />
                <MetricRow
                  label="15:29 Close Spot"
                  value={formatNumber(fspWindow.spot_1529_close)}
                />
                <MetricRow
                  label="FSP Minus 15:29 Divergence"
                  value={
                    hasValidNumber(fspWindow.fsp_minus_1529_pts)
                      ? formatNumber(fspWindow.fsp_minus_1529_pts, {
                          signed: true,
                          suffix: ' pts',
                        })
                      : UNAVAILABLE_LABEL
                  }
                />
              </>
            )}
          </div>
        </div>
      </section>

      {/* PANEL 3: Broker Pre-Flight Readiness, Proxy & Runtime RAM/Disk Hash Parity */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs p-4 sm:p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <Server size={16} className="text-slate-700" />
              <span>3. Broker Pre-Flight Readiness, Static IPv6 Proxy &amp; Runtime Hash Parity</span>
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Zero hardcoded IP addresses, exchange lists, or margin constants; missing fields
              display Unavailable.
            </p>
          </div>
          <span className="text-xs font-mono text-slate-500">
            Pre-Flight Snap: {formatTimestampLabel(readiness.timestamp)}
          </span>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Left: Account, Proxy & 4 Safety Gatekeepers */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/40 space-y-3">
            <div className="text-xs font-bold text-slate-800 flex items-center gap-1.5">
              <ShieldCheck size={14} className="text-slate-600" />
              <span>Broker Account, Proxy &amp; Pre-Flight Gatekeepers</span>
            </div>

            <div>
              <MetricRow
                label="Account Identity"
                value={
                  readiness.user_id || readiness.user_name
                    ? `${readiness.user_id || UNAVAILABLE_LABEL} (${
                        readiness.user_name || UNAVAILABLE_LABEL
                      })`
                    : UNAVAILABLE_LABEL
                }
              />
              <MetricRow
                label="Enabled Exchanges"
                value={
                  Array.isArray(readiness.exchanges) && readiness.exchanges.length > 0
                    ? readiness.exchanges.join(', ')
                    : UNAVAILABLE_LABEL
                }
              />
              <MetricRow
                label="Proxy Endpoint / Latency"
                value={
                  readiness.proxy_url_masked || hasValidNumber(readiness.proxy_latency_ms)
                    ? `${readiness.proxy_url_masked || UNAVAILABLE_LABEL} (${
                        hasValidNumber(readiness.proxy_latency_ms)
                          ? `${formatNumber(readiness.proxy_latency_ms, { decimals: 1 })} ms`
                          : UNAVAILABLE_LABEL
                      })`
                    : UNAVAILABLE_LABEL
                }
              />
              <MetricRow
                label="Observed Egress IP"
                value={readiness.primary_ip || UNAVAILABLE_LABEL}
              />
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 pt-1">
              {gatekeepers.map((gk) => (
                <div
                  key={gk.id}
                  className="rounded border border-slate-200 bg-white px-2.5 py-2 flex items-center justify-between gap-2 text-[11px]"
                >
                  <span className="text-slate-700 font-medium">{gk.label}</span>
                  <span
                    className={`font-mono font-bold px-1.5 py-0.5 rounded ${
                      gk.status === 'PASS'
                        ? 'bg-emerald-50 text-emerald-800'
                        : gk.status === 'BLOCKED'
                        ? 'bg-rose-50 text-rose-800'
                        : 'bg-slate-100 text-slate-500'
                    }`}
                  >
                    {gk.status}
                  </span>
                </div>
              ))}
            </div>

            {Array.isArray(readiness.blockers) && readiness.blockers.length > 0 && (
              <div className="p-2.5 rounded bg-rose-50 border border-rose-200 text-xs text-rose-900 font-mono">
                Blockers: {readiness.blockers.join(' • ')}
              </div>
            )}
          </div>

          {/* Right: Runtime RAM vs Disk SHA-256 Parity */}
          <div className="rounded-lg border border-slate-200 p-3.5 bg-slate-50/40 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div className="text-xs font-bold text-slate-800 flex items-center gap-1.5">
                <Cpu size={14} className="text-slate-600" />
                <span>Runtime RAM vs. Disk SHA-256 Parity</span>
              </div>
              <span
                className={`px-2 py-0.5 rounded text-[11px] font-mono font-semibold border ${
                  parity.in_sync === true
                    ? 'bg-emerald-50 text-emerald-800 border-emerald-300'
                    : parity.in_sync === false
                    ? 'bg-amber-50 text-amber-800 border-amber-300'
                    : 'bg-slate-100 text-slate-600 border-slate-300'
                }`}
              >
                {parity.status || UNAVAILABLE_LABEL}
              </span>
            </div>

            {parity.in_sync === false && (
              <div className="p-2.5 rounded bg-amber-50 border border-amber-200 text-xs text-amber-900 flex items-center gap-2 font-mono">
                <AlertTriangle size={14} className="text-amber-600 shrink-0" />
                <span>
                  Stale RAM module(s): {(parity.stale_files || []).join(', ') || 'Mismatch detected'}
                </span>
              </div>
            )}

            {hashFiles.length === 0 ? (
              <div className="text-xs font-mono text-slate-500 py-4">
                Module hash table unavailable from /status.
              </div>
            ) : (
              <div className="overflow-x-auto border border-slate-200 rounded-lg bg-white">
                <table className="w-full text-left border-collapse text-[11px] font-mono">
                  <thead>
                    <tr className="bg-slate-50 border-b border-slate-200 text-slate-600 font-sans font-semibold">
                      <th className="py-1.5 px-2.5">Module</th>
                      <th className="py-1.5 px-2.5">Boot RAM SHA</th>
                      <th className="py-1.5 px-2.5">Disk SHA</th>
                      <th className="py-1.5 px-2.5 text-right">Match</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {hashFiles.map((file) => {
                      const bHash = bootHashes[file] || UNAVAILABLE_LABEL;
                      const dHash = diskHashes[file] || UNAVAILABLE_LABEL;
                      const matched = bHash !== UNAVAILABLE_LABEL && bHash === dHash;
                      return (
                        <tr key={file}>
                          <td className="py-1.5 px-2.5 text-slate-800 font-semibold">{file}</td>
                          <td className="py-1.5 px-2.5 text-slate-600">{String(bHash).slice(0, 12)}</td>
                          <td className="py-1.5 px-2.5 text-slate-600">{String(dHash).slice(0, 12)}</td>
                          <td className="py-1.5 px-2.5 text-right">
                            {matched ? (
                              <span className="inline-flex items-center gap-1 text-emerald-700 font-bold">
                                <CheckCircle2 size={12} /> OK
                              </span>
                            ) : (
                              <span className="text-amber-700 font-bold">STALE</span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* PANEL 4: Collapsible Reference Documentation (Clearly Badged Not Active Runtime) */}
      <section className="bg-white rounded-xl border border-slate-200 shadow-2xs overflow-hidden">
        <button
          type="button"
          onClick={() => setShowReferenceDocs((v) => !v)}
          aria-expanded={showReferenceDocs}
          className="w-full p-4 sm:p-5 flex flex-wrap items-center justify-between gap-2 text-left hover:bg-slate-50 transition cursor-pointer"
        >
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <BookOpen size={15} className="text-slate-600" />
              <h2 className="text-sm font-bold text-slate-800">
                4. Reference Protocol &amp; Historical Architecture Notes
              </h2>
              <span className="px-2 py-0.5 rounded text-[10px] font-mono font-semibold bg-amber-50 text-amber-900 border border-amber-300">
                REFERENCE SPECIFICATION / EXPOSED HISTORICAL BACKTEST — NOT ACTIVE RUNTIME
              </span>
            </div>
            <p className="text-xs text-slate-500">
              Collapsed by default so static rule documentation is never mistaken for live
              execution telemetry.
            </p>
          </div>
          <span className="text-slate-500">
            {showReferenceDocs ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          </span>
        </button>

        {showReferenceDocs && (
          <div className="border-t border-slate-100 p-4 sm:p-5 bg-slate-50/60 grid grid-cols-1 md:grid-cols-3 gap-4 text-xs text-slate-700 leading-relaxed">
            <div className="bg-white rounded-lg border border-slate-200 p-3.5 space-y-1.5">
              <div className="font-bold text-slate-900">
                Forward Paper Book B (<code>SHADOW_BOOK_B_PAID_FLOW</code>)
              </div>
              <p>
                Evaluates 1-lot cash-paid directional NIFTY options during 09:45–12:45 IST using
                causal ORB-30 + VWAP alignment, live futures basis, Wilder ADX(14), Kaufman ER, and
                observed L5 ask depth. Trailing stops update strictly on completed 1-minute closes.
              </p>
            </div>

            <div className="bg-white rounded-lg border border-slate-200 p-3.5 space-y-1.5">
              <div className="font-bold text-slate-900">
                Forward Paper Book A (<code>SHADOW_BOOK_A_SPREAD_150</code>)
              </div>
              <p>
                Evaluates 150-point defined-width NIFTY credit spreads during 09:45–13:15 IST.
                Simulates buying the protective wing first on observed L5 ask before selling the
                short leg on observed L5 bid, recording incomplete-leg wing unwinds when Leg-2
                liquidity or credit degrades.
              </p>
            </div>

            <div className="bg-white rounded-lg border border-slate-200 p-3.5 space-y-1.5">
              <div className="font-bold text-slate-900">
                Retired Historical Engines (<code>V6B_*</code> / <code>V2.0</code>)
              </div>
              <p>
                Earlier unhedged writer surveillance (`V6B_S1_OI`, `V6B_S2_PIN`) and V2.0 research
                baselines remain retired from active paper execution (`SURVEILLANCE_ONLY`) and are
                segregated into the Legacy Paper filter in Performance and Positions.
              </p>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
