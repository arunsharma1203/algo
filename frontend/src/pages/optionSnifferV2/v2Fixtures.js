// V2 Option Sniffer — Safe Value Formatters & Explicit Development Fixtures (?fixture=...)

export function fmtInr(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return 'Unavailable';
  }
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 2,
  }).format(Number(val));
}

export function fmtSignedInr(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return 'Unavailable';
  }
  const n = Number(val);
  const absStr = fmtInr(Math.abs(n));
  if (n > 0) return `+${absStr}`;
  if (n < 0) return `-${absStr}`;
  return absStr;
}

export function fmtAgeMs(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return 'Unavailable';
  }
  const ms = Number(val);
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

export function fmtPct(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return 'Unavailable';
  }
  return `${Number(val).toFixed(2)}%`;
}

export function fmtIstStamp(val) {
  if (!val) return 'Unavailable';
  return String(val).replace('T', ' ').replace('+05:30', ' IST');
}

function _mkEndpointEntry(data, error = null) {
  if (!data) {
    return {
      data: null,
      status: 'unavailable',
      stale: false,
      unavailable: true,
      httpStatus: 404,
      asOfIst: null,
      error: error || 'HTTP 404 from /api/option-sniffer-advance-v2',
    };
  }
  return {
    data,
    status: 'fresh',
    stale: false,
    unavailable: false,
    httpStatus: 200,
    asOfIst: '2026-11-24 14:22:08 IST',
    error: null,
  };
}

/**
 * Returns fixture state strictly when `?fixture=<mode>` is explicitly provided:
 *   - healthy (alias: active)
 *   - no_trades (alias: pending_restart, idle)
 *   - stale_unresolved (alias: unresolved_exposure)
 *   - api_unavailable
 */
export function getV2FixtureBundle(fixtureMode) {
  if (!fixtureMode) return null;
  const raw = String(fixtureMode).toLowerCase().trim();
  const mode =
    raw === 'active'
      ? 'healthy'
      : raw === 'pending_restart' || raw === 'idle'
      ? 'no_trades'
      : raw === 'unresolved_exposure'
      ? 'stale_unresolved'
      : raw;

  if (mode === 'api_unavailable') {
    return {
      fixtureMode: 'api_unavailable',
      endpoints: {
        overview: _mkEndpointEntry(null, 'HTTP 404 Not Found (/api/option-sniffer-advance-v2/overview)'),
        positions: _mkEndpointEntry(null, 'HTTP 404 Not Found (/api/option-sniffer-advance-v2/positions)'),
        dailyComparison: _mkEndpointEntry(null, 'HTTP 404 Not Found (/api/option-sniffer-advance-v2/daily-comparison)'),
        replayAudit: _mkEndpointEntry(null, 'HTTP 404 Not Found (/api/option-sniffer-advance-v2/replay-audit)'),
        counterfactuals: _mkEndpointEntry(null, 'HTTP 404 Not Found (/api/option-sniffer-advance-v2/counterfactuals)'),
        diagnostics: _mkEndpointEntry(null, 'HTTP 404 Not Found (/api/option-sniffer-advance-v2/diagnostics)'),
      },
    };
  }

  const isHealthy = mode === 'healthy';
  const isNoTrades = mode === 'no_trades';
  const isUnresolved = mode === 'stale_unresolved';

  const fwdTrade = isHealthy
    ? [
        {
          package_id: 'osv2_pkg_fwd_demo_01',
          book_id: 'V2_BOOK_B_PAID_FLOW',
          session_date: '2026-11-24',
          direction_label: 'NIFTY 25600 CE (ATM)',
          entry_bar_ts: '2026-11-24 10:35:00',
          exit_bar_ts: '2026-11-24 11:48:00',
          entry_bar_ist: '2026-11-24T10:36:00.120+05:30',
          exit_bar_ist: '2026-11-24T11:49:00.090+05:30',
          exit_reason: 'CLOSE_ONLY_RATCHET_STOP',
          planned_loss_inr: 1797.25,
          actual_gross_fill_loss_inr: 0.0,
          slippage_and_gap_overshoot_inr: 0.0,
          gross_pnl_inr: 1625.0,
          total_friction_inr: 76.42,
          net_pnl_inr: 1548.58,
          legs_state: {
            mode: 'SINGLE_PAID_LONG',
            long_leg: {
              trading_symbol: 'NIFTY 25600 CE 28 NOV 26',
              strike: 25600,
              option_type: 'CE',
              qty: 1,
              entry_price: 138.0,
              exit_price: 163.0,
            },
          },
          fills: [
            {
              fill_id: 'osv2_fill_01_e',
              leg_role: 'LONG_ENTRY',
              side: 'BUY',
              fill_price: 138.0,
              filled_qty_units: 65,
              consumed_ts_ms: 1795503360120,
              fill_fidelity: 'OBSERVED_V3_L5',
              consumed_rungs: [{ rung: 1, price: 137.95, qty: 65 }, { rung: 'slip', price: 138.0, qty: 65 }],
            },
            {
              fill_id: 'osv2_fill_01_x',
              leg_role: 'LONG_EXIT',
              side: 'SELL',
              fill_price: 163.0,
              filled_qty_units: 65,
              consumed_ts_ms: 1795507740090,
              fill_fidelity: 'OBSERVED_L1_STRIPPED_CARRIED_DEPTH',
              consumed_rungs: [{ rung: 1, price: 163.05, qty: 65 }],
            },
          ],
          signal_gates: [
            { name: 'ADX14', current: 26.4, required: '>= 18.0', passed: true },
            { name: 'ER20', current: 0.37, required: '>= 0.32', passed: true },
            { name: 'dBasis', current: '+3.8 pt', required: '>= +3.0 pt (same FUT)', passed: true },
            { name: 'TPB VWAP', current: '25618.4 vs 25596.0', required: 'Spot > VWAP', passed: true },
          ],
          events_timeline: [
            { ts_ms: 1795503360120, bar_ts: '2026-11-24 10:35:00', event: 'ENTRY_FILLED', detail: '1 lot @ 138.00 (L5 verified)' },
            { ts_ms: 1795505760050, bar_ts: '2026-11-24 11:15:00', event: 'RATCHET_RAISED', detail: 'Close ratchet raised to 163.00' },
            { ts_ms: 1795507740090, bar_ts: '2026-11-24 11:48:00', event: 'EXIT_FILLED', detail: 'CLOSE_ONLY_RATCHET_STOP @ 163.00' },
          ],
        },
      ]
    : [];

  const openPositions = isUnresolved
    ? [
        {
          package_id: 'osv2_pkg_fixture_unres',
          book_id: 'V2_BOOK_A_PROPOSED_O3',
          session_date: '2026-11-24',
          expiry: '2026-11-28',
          state: 'UNRESOLVED_EXPOSURE_PAST_1445',
          direction_label: 'PUT_CREDIT_SPREAD_100W',
          entry_bar_ts: '2026-11-23 14:15:00',
          entry_bar_ist: '2026-11-23T14:16:00.050+05:30',
          reserved_capital_inr: 42000.0,
          structural_max_loss_inr: 4800.0,
          is_unpriced_exposure: 1,
          unpriced_reason: 'LEG_SUBSCRIPTION_EVICTED',
          unpriced_age_ms: 48200,
          last_reliable_mark_net_inr: -1420.5,
          mark_age_ms: 49500,
          conservative_worst_loss_inr: -4800.0,
          legs_count: 2,
          fills: [],
          events_timeline: [
            { ts_ms: 1795423560050, bar_ts: '2026-11-23 14:15:00', event: 'LEG2_SHORT_FILLED', detail: 'Net credit 41.20 pt' },
            { ts_ms: 1795499000000, bar_ts: '2026-11-24 14:46:00', event: 'MARK_UNPRICED', detail: 'Wing evicted; holding full structural risk' },
          ],
        },
      ]
    : [];

  const activeAlerts = isUnresolved
    ? [
        {
          severity: 'critical',
          code: 'UNRESOLVED_EXPOSURE',
          message: '1 unresolved/unpriced position active; full structural risk ₹4,800.00 charged to MTM DD.',
        },
        {
          severity: 'warning',
          code: 'FEED_IDLE_OR_STALE',
          message: 'Wing quote stale/evicted for 48.2 s; Book B new entries blocked while unpriced risk persists.',
        },
      ]
    : [];

  const collectionStatus = {
    collector_state: isHealthy
      ? 'ACTIVE_RECEIVING_DATA'
      : isNoTrades
      ? 'STANDBY_STREAM_INACTIVE'
      : 'DEGRADED_UNRESOLVED_EXPOSURE',
    collector_detail: isHealthy
      ? 'Receiving live Upstox passive fan-out ticks cleanly.'
      : isNoTrades
      ? 'Attached & listening cleanly; zero eligible signals triggered today.'
      : 'Open wing subscription evicted past 14:45; unpriced structural risk held conservatively.',
    listener_attached: !isNoTrades,
    receiving_usable_data: isHealthy,
    last_usable_event_ist: isUnresolved ? '2026-11-24T14:21:18+05:30' : isHealthy ? '2026-11-24T14:22:07+05:30' : null,
    data_freshness_ms: isUnresolved ? 49500 : isHealthy ? 240 : null,
    db_path: 'fixture://isolated_v2_test.db',
    db_exists: true,
    booking_halted: false,
  };

  const fwdStats = isHealthy
    ? {
        trade_count: 1,
        wins: 1,
        win_rate_pct: 100.0,
        profit_factor: 99.0,
        gross_pnl_inr: 1625.0,
        total_charges_inr: 76.42,
        net_pnl_inr: 1548.58,
        closed_dd_inr: 0.0,
        closed_dd_pct: 0.0,
        intraday_mtm_dd_inr: 410.0,
        intraday_mtm_dd_pct: 0.21,
        intraday_mtm_dd_pct_peak: 0.2,
        running_peak_equity_inr: 202010.0,
        peak_capital_used_inr: 8970.0,
        open_count: 0,
        unresolved_exposure_count: 0,
        unresolved_structural_risk_charged_inr: 0.0,
        realized_equity_inr: 201548.58,
        reserved_capital_inr: 0.0,
        canonical_ledger_sha256: 'fixture_healthy_fwd_sha256',
      }
    : {
        trade_count: 0,
        wins: 0,
        win_rate_pct: 0.0,
        profit_factor: 0.0,
        gross_pnl_inr: 0.0,
        total_charges_inr: 0.0,
        net_pnl_inr: 0.0,
        closed_dd_inr: 0.0,
        closed_dd_pct: 0.0,
        intraday_mtm_dd_inr: isUnresolved ? 4800.0 : 0.0,
        intraday_mtm_dd_pct: isUnresolved ? 2.4 : 0.0,
        intraday_mtm_dd_pct_peak: isUnresolved ? 2.4 : 0.0,
        running_peak_equity_inr: 200000.0,
        peak_capital_used_inr: isUnresolved ? 42000.0 : 0.0,
        open_count: isUnresolved ? 1 : 0,
        unresolved_exposure_count: isUnresolved ? 1 : 0,
        unresolved_structural_risk_charged_inr: isUnresolved ? 4800.0 : 0.0,
        realized_equity_inr: 200000.0,
        reserved_capital_inr: isUnresolved ? 42000.0 : 0.0,
        canonical_ledger_sha256: null,
      };

  const overviewData = {
    service_name: 'V2 Option Sniffer',
    package: 'option_sniffer_v2',
    protocol_id: 'OPTION_SNIFFER_V2_ISOLATED_PAPER_V1',
    engine_version: '2.1.0-causal',
    replay_run_mode: 'REPLAY_V2_1_CAUSAL',
    config_hash: '0f972ca95e7ccdc3f11dfe4f7d84834063c1b9f5d4aa569759055fda844d7f9d',
    simulation_mode: true,
    live_capable_by_config: false,
    execution_adapter: 'LOCAL_PAPER_SIMULATOR_ONLY',
    result_boundary_notice:
      'DEVELOPMENT TEST FIXTURE: FORWARD_PAPER and REPLAY_V2_1_CAUSAL strictly separated; V1 counterfactuals excluded.',
    collection_status: collectionStatus,
    active_alerts: activeAlerts,
    book_status_rows: [
      {
        book_id: 'V2_BOOK_A_PROPOSED_O3',
        book_label: 'Book A (Phase-4 O3 T-1->T0 Credit Spread)',
        enabled: false,
        eligibility: 'DISABLED_UNVERIFIED',
        current_position: isUnresolved ? 'PUT_CREDIT_SPREAD_100W' : 'NONE',
        latest_decision: 'DISABLED_UNVERIFIED',
        latest_rejection_reason:
          'BLOCKED_UNVERIFIED_O3_SPEC_CONFLICT: Phase-4 O3 uses 15m Open[t+1], 70% target, 100pt grid vs V2 50% / 50pt.',
        last_fill: null,
      },
      {
        book_id: 'V2_BOOK_B_PAID_FLOW',
        book_label: 'Book B (Paid Convexity Flow: dBasis + 10:15-13:00)',
        enabled: true,
        eligibility: isUnresolved ? 'BLOCKED_UNPRICED' : isHealthy ? 'ELIGIBLE' : 'NO_SIGNAL_TODAY',
        current_position: 'NONE',
        latest_decision: isHealthy ? 'TRIGGERED' : isUnresolved ? 'BLOCKED' : 'REJECTED',
        latest_rejection_reason: isHealthy
          ? null
          : isUnresolved
          ? 'UNPRICED_OPEN_EXPOSURE: New entries halted while open exposure lacks fresh executable quotes.'
          : 'BASIS_DELTA_TOO_SMALL (-1.2 pt < +-3.0 pt threshold during 10:15-13:00 IST)',
        last_fill: isHealthy ? fwdTrade[0].fills[1] : null,
      },
    ],
    forward_paper_summary: fwdStats,
    archive_replay_summary: {
      trade_count: 0,
      wins: 0,
      win_rate_pct: 0.0,
      profit_factor: 0.0,
      gross_pnl_inr: 0.0,
      total_charges_inr: 0.0,
      net_pnl_inr: 0.0,
      closed_dd_inr: 0.0,
      intraday_mtm_dd_inr: 0.0,
      intraday_mtm_dd_pct: 0.0,
      intraday_mtm_dd_pct_peak: 0.0,
      peak_capital_used_inr: 0.0,
      persisted_sessions: 6,
      replay_run_mode: 'REPLAY_V2_1_CAUSAL',
      canonical_ledger_sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    },
    superseded_v2_0_replay: {
      evidence_status: 'SUPERSEDED_V2_0_NONCAUSAL_SYNTHETIC_SYNC',
      audited_commit: 'baa72f775e016d2a25f3e5e1419e7667262756d9',
      ledger_sha256: '52c0cfb5c1e46e87abee21c40d39a0e431017411879e1f895309a7f9729c551a',
      reason_superseded: [
        'Used bar_ts open time instead of recorded_at/timestamp (~60s+ later).',
        'Fabricated identical spot/futures timestamps & synthetic NSE_FUT contract keys.',
      ],
      superseded_stats: { trade_count: 2, net_pnl_inr: 9725.02, intraday_mtm_dd_inr: 10927.93 },
    },
    promotion_gates: {
      status: 'FAIL_COLLECTING_FORWARD_EVIDENCE',
      side_effect: 'NONE_HUMAN_REVIEW_ONLY',
      sessions_progress: isHealthy ? '1/60' : '0/60',
      unresolved_exposure_count: isUnresolved ? 1 : 0,
      unresolved_structural_risk_charged_inr: isUnresolved ? 4800.0 : 0.0,
      checks: [
        { gate: 'Clean Forward Sessions >= 60', required: 60, observed: isHealthy ? 1 : 0, passed: false },
        { gate: 'Forward Closed Trades >= 30', required: 30, observed: isHealthy ? 1 : 0, passed: false },
        { gate: 'Zero Unresolved / Unpriced Open Exposure', required: 0, observed: isUnresolved ? 1 : 0, passed: !isUnresolved },
      ],
    },
    feed_hygiene: {
      feed_consumer: {
        queue_capacity: 4096,
        current_queue_depth: 0,
        queue_lag_p50_ms: 0.05,
        queue_lag_p99_ms: 0.21,
        duplicate_events_suppressed: 0,
        late_out_of_order_events: 0,
        sequence_bar_gaps: 0,
        dropped_overflow_events: 0,
      },
      subscription_registry: {
        v2_owned_only_keys: isUnresolved ? ['NSE_FO|48001'] : [],
        evicted_v2_keys: isUnresolved ? ['NSE_FO|48001'] : [],
        pressure_yield_count: isUnresolved ? 1 : 0,
      },
      spot_age_ms: isNoTrades ? null : isUnresolved ? 49500 : 180,
      futures_age_ms: isNoTrades ? null : isUnresolved ? 49800 : 210,
      spot_fut_sync: { ok: isHealthy, skew_ms: isHealthy ? 30 : null, reason: isHealthy ? 'SYNCED' : 'UNAVAILABLE_OR_STALE' },
    },
    optional_collateral_reporting: {
      forward_paper: {
        scenario_enabled: false,
        classification_label: 'ASSUMED_YIELD_NOT_TRADING_ALPHA',
        hypothetical_collateral_income_inr: 0,
        rf_benchmark_6p5_period_inr: 35.62,
      },
      archive_replay: {
        scenario_enabled: false,
        classification_label: 'ASSUMED_YIELD_NOT_TRADING_ALPHA',
        hypothetical_collateral_income_inr: 0,
        rf_benchmark_6p5_period_inr: 213.7,
      },
    },
  };

  const positionsData = {
    simulation_mode: true,
    engine_version: '2.1.0-causal',
    replay_run_mode: 'REPLAY_V2_1_CAUSAL',
    forward_open_positions: openPositions,
    forward_closed_trades: fwdTrade,
    forward_fills: fwdTrade[0]?.fills || [],
    replay_closed_trades: [],
    replay_fills: [],
  };

  const dailyComparisonData = {
    engine_version: '2.1.0-causal',
    replay_run_mode: 'REPLAY_V2_1_CAUSAL',
    v1_reported_summary: { trade_count: 8, net_pnl_inr: 16281.11, profit_factor: 11.714, peak_capital_used_inr: 51196.75, closed_dd_inr: 806.84 },
    v2_replay_summary: overviewData.archive_replay_summary,
    v2_forward_paper_summary: fwdStats,
    superseded_v2_0_replay: overviewData.superseded_v2_0_replay,
    archive_daily_comparison: [
      { session_date: '2026-11-16', v1_reported_net_pnl_inr: -640.71, v1_reported_trades: 1, v1_stale_wing_contaminated_count: 0, v2_replay_net_pnl_inr: 0, v2_replay_trades: 0, v2_session_state: 'DATA_GAP', verification_outcome: 'INSUFFICIENT_SOURCE_TS_OR_CONTRACT_EVIDENCE' },
      { session_date: '2026-11-17', v1_reported_net_pnl_inr: 2820.23, v1_reported_trades: 1, v1_stale_wing_contaminated_count: 1, v2_replay_net_pnl_inr: 0, v2_replay_trades: 0, v2_session_state: 'DATA_GAP', verification_outcome: 'INSUFFICIENT_SOURCE_TS_OR_CONTRACT_EVIDENCE' },
      { session_date: '2026-11-18', v1_reported_net_pnl_inr: -806.84, v1_reported_trades: 1, v1_stale_wing_contaminated_count: 0, v2_replay_net_pnl_inr: 0, v2_replay_trades: 0, v2_session_state: 'DATA_GAP', verification_outcome: 'INSUFFICIENT_SOURCE_TS_OR_CONTRACT_EVIDENCE' },
      { session_date: '2026-11-19', v1_reported_net_pnl_inr: 10944.03, v1_reported_trades: 2, v1_stale_wing_contaminated_count: 1, v2_replay_net_pnl_inr: 0, v2_replay_trades: 0, v2_session_state: 'DATA_GAP', verification_outcome: 'INSUFFICIENT_SOURCE_TS_OR_CONTRACT_EVIDENCE' },
      { session_date: '2026-11-20', v1_reported_net_pnl_inr: 2046.96, v1_reported_trades: 2, v1_stale_wing_contaminated_count: 1, v2_replay_net_pnl_inr: 0, v2_replay_trades: 0, v2_session_state: 'PARTIAL_SESSION', verification_outcome: 'INSUFFICIENT_SOURCE_TS_OR_CONTRACT_EVIDENCE' },
      { session_date: '2026-11-23', v1_reported_net_pnl_inr: 1917.44, v1_reported_trades: 1, v1_stale_wing_contaminated_count: 1, v2_replay_net_pnl_inr: 0, v2_replay_trades: 0, v2_session_state: 'PARTIAL_SESSION', verification_outcome: 'INSUFFICIENT_SOURCE_TS_OR_CONTRACT_EVIDENCE' },
    ],
  };

  const counterfactualsData = {
    included_in_v2_totals: false,
    cf1_summary: {
      total_v1_packages: 8,
      realized_count: 5,
      unresolved_count: 3,
      unresolved_stale_after_arming_count: 0,
      unresolved_stale_before_arming_count: 3,
      realized_net_pnl_sum_inr: 7722.02,
      unresolved_last_fresh_mark_sum_inr: 324.02,
    },
    cf2_summary: {
      total_v1_packages: 8,
      realized_count: 5,
      unresolved_count: 3,
      realized_net_pnl_sum_inr: 9772.05,
      unresolved_last_fresh_mark_sum_inr: 324.02,
    },
    rows: [
      { cf_id: 'cf1_1', experiment_code: 'CF1_TRUE_FRESH_70PCT_CONTINUATION', session_date: '2026-11-17', v1_package_id: 'shdw_a76ba1880a5b', book_id: 'SHADOW_BOOK_A_SPREAD_150', cf_status: 'REALIZED_OUTCOME', cf_exit_reason: 'CF1_SPOT_VWAP_FLIP_EXIT', v1_reported_net_inr: 2820.23, cf_net_pnl_inr: -209.0, detail: { last_fresh_mark_net_inr: -209.0 } },
      { cf_id: 'cf1_2', experiment_code: 'CF1_TRUE_FRESH_70PCT_CONTINUATION', session_date: '2026-11-19', v1_package_id: 'shdw_244107b61c06', book_id: 'SHADOW_BOOK_A_SPREAD_150', cf_status: 'UNRESOLVED_STALE_BEFORE_ARMING', cf_exit_reason: 'UNRESOLVED_STALE_BEFORE_ARMING_NO_SYNTHETIC_FILL', v1_reported_net_inr: 2375.44, cf_net_pnl_inr: null, detail: { last_fresh_mark_net_inr: 364.34 } },
    ],
  };

  const replayAuditData = {
    engine_version: '2.1.0-causal',
    replay_run_mode: 'REPLAY_V2_1_CAUSAL',
    causal_verification_outcome: {
      verified_futures_basis_trades: 0,
      unverifiable_basis_events: 2250,
      late_events_rejected: 25,
      post_session_flushes_ignored: 185,
      reason: 'FAIL_CLOSED_INSUFFICIENT_TIMESTAMP_EVIDENCE: l5_snapshots lacks separate spot/futures timestamps & futures contract identity.',
    },
    archive_timing_evidence: {
      bar_2026_11_20_1030: { bar_ts_open: '2026-11-20 10:30:00+05:30', earliest_recorded_at: '2026-11-20T10:31:00.109787', row_count: 22 },
      delayed_2026_11_20_0940: { bar_ts_open: '2026-11-20 09:40:00+05:30', delayed_recorded_at: '2026-11-20T09:44:10.039544', delayed_row_count: 22 },
      cutoff_2026_11_19_1515: { bar_ts: '2026-11-19 15:15:00', timestamp: '2026-11-19T15:16:00.020362' },
    },
    superseded_v2_0_replay: overviewData.superseded_v2_0_replay,
    phase4_o3_audit: { reproduction_status: 'ARTIFACT_RECONCILED_RAW_OPTION_SQLITE_PURGED_FROM_TMP', spec_conflicts: [] },
    ablation_study: [],
    sensitivity: {},
  };

  const diagnosticsData = {
    simulation_mode: true,
    live_capable_by_config: false,
    protected_artifacts_integrity: { all_unchanged: true, hashes: { 'SHADOW_PREREGISTRATION.json': 'fixture_sha' } },
    upstox_entitlement_field_inventory: [],
    existing_v2_systems_inventory: [],
    operator_activation_commands: {
      reproducible_cli_audit: 'PYTHONPATH=backend /usr/bin/python3 -B -m app.analytics.option_sniffer_v2.v2_reproducible_audit_cli',
    },
  };

  return {
    fixtureMode: mode,
    endpoints: {
      overview: _mkEndpointEntry(overviewData),
      positions: _mkEndpointEntry(positionsData),
      dailyComparison: _mkEndpointEntry(dailyComparisonData),
      replayAudit: _mkEndpointEntry(replayAuditData),
      counterfactuals: _mkEndpointEntry(counterfactualsData),
      diagnostics: _mkEndpointEntry(diagnosticsData),
    },
  };
}
