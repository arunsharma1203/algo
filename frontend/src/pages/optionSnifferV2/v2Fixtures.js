// V2 Option Sniffer — Formatters & Explicit Opt-In Test Fixtures (?fixture=active|pending_restart|unresolved_exposure)

export function fmtInr(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return 'UNAVAILABLE';
  }
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 2,
  }).format(Number(val));
}

export function fmtSignedInr(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return 'UNAVAILABLE';
  }
  const n = Number(val);
  const absStr = fmtInr(Math.abs(n));
  if (n > 0) return `+${absStr}`;
  if (n < 0) return `-${absStr}`;
  return absStr;
}

export function fmtAgeMs(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return 'UNAVAILABLE';
  }
  const ms = Number(val);
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function getV2FixtureBundle(fixtureMode) {
  if (!fixtureMode) return null;

  const isPending = fixtureMode === 'pending_restart';
  const isUnresolved = fixtureMode === 'unresolved_exposure';

  const baseOverview = {
    service_name: 'V2 Option Sniffer',
    package: 'option_sniffer_v2',
    protocol_id: 'OPTION_SNIFFER_V2_ISOLATED_PAPER_V1',
    config_hash: '0f972ca95e7ccdc3f11dfe4f7d84834063c1b9f5d4aa569759055fda844d7f9d',
    simulation_mode: true,
    live_capable_by_config: false,
    execution_adapter: 'LOCAL_PAPER_SIMULATOR_ONLY',
    result_boundary_notice:
      'FIXTURE_MODE_ACTIVE: Deterministic UI test fixture. FORWARD_PAPER and ARCHIVE_REPLAY kept strictly separate.',
    collection_status: {
      collector_state: isPending ? 'PENDING_SHARED_SERVICE_RESTART' : 'PARALLEL_LIVE_COLLECTION_ACTIVE',
      collector_detail: isPending
        ? 'Live :8000 daemon has not been restarted yet to mount /api/option-sniffer-advance-v2.'
        : 'Fixture: Passive fan-out listener attached to shared OptionStreamManager.',
      listener_attached: !isPending,
      db_path: 'fixture://isolated_test.db',
      archive_replay_persisted: !isPending,
    },
    books: {
      V2_BOOK_A_PROPOSED_O3: {
        book_id: 'V2_BOOK_A_PROPOSED_O3',
        autonomous_entries_enabled: false,
        status_banner: 'DISABLED FOR AUTONOMOUS FORWARD ENTRIES: UNVERIFIED_SPEC_CONFLICT',
        credit_capture_target_pct: 50.0,
        min_net_credit_pts: 30.0,
        max_structural_loss_inr: 4800.0,
      },
      V2_BOOK_B_PAID_FLOW: {
        book_id: 'V2_BOOK_B_PAID_FLOW',
        autonomous_entries_enabled: true,
        status_banner: 'ACTIVE PAPER HYPOTHESIS (D-BASIS + 10:15-13:00 + 20%/Rs1800 + 1-OTM)',
        entry_window_start_hhmm: 1015,
        entry_window_end_hhmm: 1300,
        min_intraday_basis_change_abs_pts: 3.0,
        max_entry_outlay_inr: 11500.0,
        planned_loss_max_pct_of_outlay: 20.0,
        planned_loss_max_inr: 1800.0,
      },
    },
    forward_paper_summary: {
      trade_count: isPending ? 0 : 1,
      wins: isPending ? 0 : 1,
      win_rate_pct: isPending ? 0 : 100,
      profit_factor: isPending ? 0 : 99.0,
      gross_pnl_inr: isPending ? 0 : 950.0,
      total_charges_inr: isPending ? 0 : 74.97,
      net_pnl_inr: isPending ? 0 : 875.03,
      closed_dd_inr: 0.0,
      closed_dd_pct: 0.0,
      intraday_mtm_dd_inr: isUnresolved ? 4800.0 : 0.0,
      intraday_mtm_dd_pct: isUnresolved ? 2.4 : 0.0,
      peak_capital_used_inr: isPending ? 0 : isUnresolved ? 42000.0 : 9400.0,
      open_count: isUnresolved ? 1 : 0,
      unresolved_exposure_count: isUnresolved ? 1 : 0,
      unresolved_structural_risk_charged_inr: isUnresolved ? 4800.0 : 0.0,
      realized_equity_inr: isPending ? 200000.0 : 200875.03,
      reserved_capital_inr: isUnresolved ? 42000.0 : 0.0,
      canonical_ledger_sha256: 'fixture_sha256_forward',
    },
    archive_replay_summary: {
      trade_count: 2,
      wins: 2,
      win_rate_pct: 100.0,
      profit_factor: 99.0,
      gross_pnl_inr: 9886.5,
      total_charges_inr: 161.48,
      net_pnl_inr: 9725.02,
      closed_dd_inr: 0.0,
      intraday_mtm_dd_inr: 10927.93,
      intraday_mtm_dd_pct: 5.46,
      peak_capital_used_inr: 10855.0,
      persisted_sessions: 6,
      canonical_ledger_sha256: '52c0cfb5c1e46e87abee21c40d39a0e431017411879e1f895309a7f9729c551a',
    },
    promotion_gates: {
      status: 'FAIL_COLLECTING_FORWARD_EVIDENCE',
      side_effect: 'NONE_HUMAN_REVIEW_ONLY',
      sessions_progress: isPending ? '0/60' : '1/60',
      unresolved_exposure_count: isUnresolved ? 1 : 0,
      unresolved_structural_risk_charged_inr: isUnresolved ? 4800.0 : 0.0,
      checks: [
        { gate: 'Clean Forward Sessions >= 60', required: 60, observed: isPending ? 0 : 1, passed: false },
        { gate: 'Forward Closed Trades >= 30', required: 30, observed: isPending ? 0 : 1, passed: false },
        {
          gate: 'Zero Unresolved / Unpriced Open Exposure',
          required: 0,
          observed: isUnresolved ? 1 : 0,
          passed: !isUnresolved,
        },
      ],
    },
    feed_hygiene: {
      feed_consumer: {
        queue_capacity: 4096,
        current_queue_depth: 0,
        max_queue_depth_seen: isPending ? 0 : 12,
        enqueued_total: isPending ? 0 : 2500,
        processed_total: isPending ? 0 : 2500,
        duplicate_events_suppressed: 0,
        late_out_of_order_events: 0,
        sequence_bar_gaps: 0,
        dropped_overflow_events: 0,
        queue_lag_p50_ms: 0.04,
        queue_lag_p99_ms: 0.19,
        uncertainty_blocks: [],
      },
      subscription_registry: {
        tracked_v2_keys: isUnresolved ? ['NSE_FO|48001'] : [],
        v2_owned_only_keys: isUnresolved ? ['NSE_FO|48001'] : [],
        evicted_v2_keys: isUnresolved ? ['NSE_FO|48001'] : [],
        pressure_yield_count: isUnresolved ? 1 : 0,
        ws_total_soft_cap: 1400,
        v1_reserved_headroom: 100,
        max_v2_additive_pins: 8,
      },
      spot_age_ms: isPending ? null : 180,
      futures_age_ms: isPending ? null : 210,
      spot_fut_sync: {
        ok: !isPending,
        skew_ms: isPending ? null : 30,
        basis_pts: isPending ? null : 48.2,
      },
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

  return {
    overview: baseOverview,
    positions: {
      forward_open_positions: isUnresolved
        ? [
            {
              package_id: 'osv2_pkg_fixture_unres',
              book_id: 'V2_BOOK_A_PROPOSED_O3',
              session_date: '2026-11-23',
              expiry: '2026-11-23',
              state: 'UNRESOLVED_EXPOSURE_PAST_1445',
              direction_label: 'PUT_CREDIT_SPREAD_100W',
              entry_bar_ts: '2026-11-20 14:15:00',
              reserved_capital_inr: 42000.0,
              structural_max_loss_inr: 4800.0,
              is_unpriced_exposure: 1,
              unpriced_reason: 'LEG_SUBSCRIPTION_EVICTED',
              unpriced_age_ms: 48000,
              conservative_worst_loss_inr: -4800.0,
              legs_count: 2,
            },
          ]
        : [],
      forward_closed_trades: [],
      replay_closed_trades: [
        {
          package_id: 'osv2_pkg_7e17c055d8d89c2f',
          book_id: 'V2_BOOK_B_PAID_FLOW',
          session_date: '2026-11-17',
          direction_label: 'NIFTY 25950 PE (1-OTM)',
          entry_bar_ts: '2026-11-17 11:10:00',
          exit_bar_ts: '2026-11-17 12:43:00',
          exit_reason: 'CLOSE_ONLY_RATCHET_STOP',
          planned_loss_inr: 1797.25,
          actual_gross_fill_loss_inr: 0.0,
          slippage_and_gap_overshoot_inr: 0.0,
          gross_pnl_inr: 1056.25,
          total_friction_inr: 74.97,
          net_pnl_inr: 981.28,
        },
        {
          package_id: 'osv2_pkg_63dd1db191ace403',
          book_id: 'V2_BOOK_B_PAID_FLOW',
          session_date: '2026-11-19',
          direction_label: 'NIFTY 25300 CE (ATM)',
          entry_bar_ts: '2026-11-19 10:49:00',
          exit_bar_ts: '2026-11-19 15:15:00',
          exit_reason: 'EOD_SQUAREOFF_1515',
          planned_loss_inr: 1797.25,
          actual_gross_fill_loss_inr: 0.0,
          slippage_and_gap_overshoot_inr: 0.0,
          gross_pnl_inr: 8830.25,
          total_friction_inr: 86.51,
          net_pnl_inr: 8743.74,
        },
      ],
    },
    dailyComparison: {
      v1_reported_summary: { trade_count: 8, net_pnl_inr: 16281.11, profit_factor: 11.714, closed_dd_inr: 806.84 },
      v2_replay_summary: { trade_count: 2, net_pnl_inr: 9725.02, profit_factor: 99.0, intraday_mtm_dd_inr: 10927.93 },
      v2_forward_paper_summary: baseOverview.forward_paper_summary,
      archive_daily_comparison: [],
    },
    counterfactuals: {
      included_in_v2_totals: false,
      cf1_summary: { total_v1_packages: 8, clean_or_resolved_count: 4, unresolved_stale_wing_count: 4, mark_at_last_fresh_sum_inr: 8262.13 },
      cf2_summary: { total_v1_packages: 8, unresolved_stale_wing_count: 4, mark_at_last_fresh_sum_inr: 10478.29 },
      rows: [],
    },
    replayAudit: {
      phase4_o3_audit: { reproduction_status: 'ARTIFACT_RECONCILED_RAW_OPTION_SQLITE_PURGED_FROM_TMP', spec_conflicts: [] },
      ablation_study: [],
      sensitivity: {},
    },
    diagnostics: {
      simulation_mode: true,
      live_capable_by_config: false,
      protected_artifacts_integrity: { all_unchanged: true, hashes: {} },
      upstox_entitlement_field_inventory: [],
      existing_v2_systems_inventory: [],
    },
  };
}
