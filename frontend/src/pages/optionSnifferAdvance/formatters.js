/**
 * Pure formatting, state derivation, era classification, and polling-sequence helpers
 * for Option Sniffer Advance.
 *
 * Truthful-display invariants:
 *  - Missing / null / undefined / NaN values NEVER fall back to invented constants or 0.
 *  - Genuine numeric 0 ALWAYS remains 0 (never replaced via `||`).
 *  - Unknown or stale execution status NEVER appears as confirmed safe/disarmed.
 *  - Real broker, active forward shadow/paper (`S205_SHADOW_FORWARD_V1`), legacy historical
 *    paper (`V6B_*` / `V2.0`), and backtests are NEVER aggregated into a single headline.
 */

export const UNAVAILABLE_LABEL = 'Unavailable';
export const DEFAULT_STALE_MS = 15000;

export function hasValidNumber(value) {
  if (value === null || value === undefined || value === '') return false;
  if (typeof value === 'boolean') return false;
  const num = Number(value);
  return Number.isFinite(num);
}

export function hasPositiveNumber(value) {
  return hasValidNumber(value) && Number(value) > 0;
}

export function formatINRValue(value, { decimals = 2 } = {}) {
  if (!hasValidNumber(value)) return UNAVAILABLE_LABEL;
  const num = Number(value);
  const absFormatted = Math.abs(num).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  if (num < 0) return `-₹${absFormatted}`;
  return `₹${absFormatted}`;
}

export function formatSignedINR(value, { decimals = 2 } = {}) {
  if (!hasValidNumber(value)) return UNAVAILABLE_LABEL;
  const num = Number(value);
  const absFormatted = Math.abs(num).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  if (Object.is(num, -0) || num === 0) {
    return `₹${(0).toLocaleString('en-IN', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })}`;
  }
  return `${num > 0 ? '+' : '-'}₹${absFormatted}`;
}

export function formatNumber(value, { decimals = 2, signed = false, suffix = '' } = {}) {
  if (!hasValidNumber(value)) return UNAVAILABLE_LABEL;
  const num = Number(value);
  const formatted = Math.abs(num).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  if (num === 0) return `${formatted}${suffix}`;
  const prefix = num < 0 ? '-' : signed ? '+' : '';
  return `${prefix}${formatted}${suffix}`;
}

export function formatPercent(value, { decimals = 1, signed = false } = {}) {
  return formatNumber(value, { decimals, signed, suffix: '%' });
}

export function formatClockIST(epochMs) {
  if (!hasPositiveNumber(epochMs)) return UNAVAILABLE_LABEL;
  try {
    return (
      new Date(Number(epochMs)).toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      }) + ' IST'
    );
  } catch {
    return UNAVAILABLE_LABEL;
  }
}

export function formatTimestampLabel(rawTs) {
  if (!rawTs || typeof rawTs !== 'string') return UNAVAILABLE_LABEL;
  const trimmed = rawTs.trim();
  if (!trimmed || trimmed === '—' || trimmed === '-') return UNAVAILABLE_LABEL;
  if (trimmed.endsWith('IST')) return trimmed;
  const cleaned = trimmed.replace('T', ' ').slice(0, 19);
  return cleaned ? `${cleaned} IST` : UNAVAILABLE_LABEL;
}

export function parseRegimeContext(row) {
  if (!row || typeof row !== 'object') return {};
  if (row.regime_context && typeof row.regime_context === 'object') {
    return row.regime_context;
  }
  const raw = row.regime_context_json || row.regime_telemetry_json;
  if (!raw) return {};
  if (typeof raw === 'object') return raw;
  try {
    const parsed = JSON.parse(String(raw));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * Classifies a trade, signal, or readiness row into its truthful operational scope.
 */
export function classifyStrategyEra(strategyMode, regimeContext = {}) {
  const mode = String(strategyMode || '').trim().toUpperCase();
  const rctx = regimeContext && typeof regimeContext === 'object' ? regimeContext : {};
  if (
    mode.startsWith('SHADOW_BOOK_') ||
    rctx.shadow_only === true ||
    rctx.protocol_id === 'S205_SHADOW_FORWARD_V1'
  ) {
    return 'SHADOW_FORWARD_V1';
  }
  if (mode.startsWith('V6B_')) {
    return 'SURVEILLANCE_V6B';
  }
  if (
    mode === 'LONG_BOOK_ONLY' ||
    mode === 'DUAL_BOOK_NAKED' ||
    mode === 'DUAL_BOOK_HEDGED'
  ) {
    return 'LIVE_BROKER';
  }
  return 'LEGACY_PAPER';
}

/**
 * Derives confirmed execution state without ever equating missing/failed/stale responses
 * to a confirmed safe/disarmed state. Pure function — zero network side effects.
 */
export function deriveExecutionStatus({
  liveStatus,
  liveFetchState,
  nowMs = Date.now(),
  staleAfterMs = DEFAULT_STALE_MS,
} = {}) {
  const fetchSt = liveFetchState || { status: 'loading', lastSuccessMs: null, errorMessage: null };

  if (
    !liveStatus ||
    typeof liveStatus !== 'object' ||
    typeof liveStatus.is_live_armed !== 'boolean'
  ) {
    const reason =
      fetchSt.status === 'error'
        ? `Live status request failed (${fetchSt.errorMessage || 'network/HTTP error'}).`
        : 'Awaiting initial /live/status confirmation from backend.';
    return {
      state: 'UNKNOWN',
      label: 'EXECUTION STATE UNKNOWN',
      shortLabel: 'UNKNOWN',
      tone: 'warning',
      canConfirmSafe: false,
      isArmed: false,
      executionMode: UNAVAILABLE_LABEL,
      detail: `${reason} Unconfirmed state is never treated as disarmed.`,
    };
  }

  const lastOk = hasPositiveNumber(fetchSt.lastSuccessMs) ? Number(fetchSt.lastSuccessMs) : null;
  const ageMs = lastOk !== null ? Math.max(0, Number(nowMs) - lastOk) : Infinity;
  const isStale = fetchSt.status === 'error' || lastOk === null || ageMs > staleAfterMs;
  const mode = liveStatus.execution_mode || UNAVAILABLE_LABEL;

  if (liveStatus.is_live_armed === true) {
    return {
      state: isStale ? 'ARMED_STALE' : 'ARMED_LIVE',
      label: isStale
        ? `LIVE BROKER ARMED • TELEMETRY STALE (${mode})`
        : `LIVE BROKER ARMED (${mode})`,
      shortLabel: isStale ? 'ARMED (STALE)' : 'ARMED LIVE',
      tone: 'danger',
      canConfirmSafe: false,
      isArmed: true,
      executionMode: mode,
      detail: isStale
        ? `Last confirmed ARMED ${Math.round(ageMs / 1000)}s ago; latest status poll failed or timed out.`
        : `Broker mirroring active for ${liveStatus.armed_for_date || 'today'} until ${liveStatus.session_cutoff || '15:15 IST'}.`,
    };
  }

  if (isStale) {
    return {
      state: 'STALE',
      label: 'EXECUTION STATUS STALE',
      shortLabel: 'STALE',
      tone: 'warning',
      canConfirmSafe: false,
      isArmed: false,
      executionMode: mode,
      detail:
        lastOk !== null
          ? `Last confirmed disarmed ${Math.round(ageMs / 1000)}s ago (${formatClockIST(lastOk)}), but current status is stale/unverified.`
          : 'Status retrieval timestamp unavailable; cannot confirm current disarmed state.',
    };
  }

  return {
    state: 'DISARMED_PAPER',
    label: 'CONFIRMED DISARMED • PAPER ONLY',
    shortLabel: 'DISARMED (PAPER)',
    tone: 'safe',
    canConfirmSafe: true,
    isArmed: false,
    executionMode: mode,
    detail: 'Live broker bridge confirmed disarmed; zero live broker orders permitted.',
  };
}

/**
 * Extracts principal blocker and missing inputs from an engine_readiness row.
 */
export function deriveCandidateBlocker(row) {
  if (!row || typeof row !== 'object') {
    return {
      principalBlocker: UNAVAILABLE_LABEL,
      missingInputs: [],
      failedGates: [],
      direction: UNAVAILABLE_LABEL,
      era: 'LEGACY_PAPER',
    };
  }
  const gates = Array.isArray(row.gates) ? row.gates : [];
  const failedGates = gates.filter((g) => !g?.passed);
  const missingInputs = gates
    .filter((g) => {
      const cur = String(g?.current ?? '').trim();
      return !cur || cur === '—' || cur === '-' || cur === '0q/OBI +0.00' || cur === 'W:0q S:0q';
    })
    .map((g) => g.name);

  let direction = UNAVAILABLE_LABEL;
  const cand = String(row.candidate_contract || '');
  if (row.engine_id === 'SHADOW_BOOK_A_SPREAD_150') {
    direction = cand.includes('PE') ? 'CREDIT_SPREAD_PE' : cand.includes('CE') ? 'CREDIT_SPREAD_CE' : 'CREDIT_SPREAD';
  } else if (row.book_type === 'LONG_BOOK_BUY') {
    direction = cand.includes('PE') ? 'BUY_PE' : cand.includes('CE') ? 'BUY_CE' : 'BUY_OPTION';
  } else if (row.book_type === 'SHORT_BOOK_SELL') {
    direction = cand.includes('PE') ? 'SELL_PE' : cand.includes('CE') ? 'SELL_CE' : 'SELL_OPTION';
  }

  const state = String(row.state || 'UNKNOWN');
  let principalBlocker = 'None (Operational rules met — expectancy unverified)';
  if (state === 'SURVEILLANCE_ONLY') {
    principalBlocker = 'Retired from execution (passive surveillance only)';
  } else if (state === 'HALTED_KILL' || state === 'RISK_PAUSED') {
    principalBlocker = 'Session risk halt / daily MTM kill active';
  } else if (state === 'WARMUP_0945' || state === 'WARMING_UP') {
    principalBlocker = 'Pre-09:45 IST ORB-30 causal warmup incomplete';
  } else if (state === 'WINDOW_CLOSED') {
    principalBlocker = `Outside entry window (${row.window_ist || 'closed'} IST)`;
  } else if (state === 'ML_VETOED') {
    principalBlocker = 'Meta-learner score below configured threshold';
  } else if (failedGates.length > 0) {
    const first = failedGates[0];
    principalBlocker = `${first.name}: ${first.current ?? UNAVAILABLE_LABEL} (target ${first.target ?? UNAVAILABLE_LABEL})`;
    if (failedGates.length > 1) {
      principalBlocker += ` (+${failedGates.length - 1} more)`;
    }
  } else if (state !== 'READY_FIRE') {
    principalBlocker = `State: ${state}`;
  }

  return {
    principalBlocker,
    missingInputs,
    failedGates,
    direction,
    era: classifyStrategyEra(row.engine_id),
  };
}

/**
 * Builds expandable per-leg breakdown for open or closed forward shadow/paper trades.
 */
export function buildPackageLegRows(trade) {
  if (!trade || typeof trade !== 'object') return [];
  const rctx = parseRegimeContext(trade);
  const mode = String(trade.strategy_mode || trade.book_id || '').toUpperCase();
  const qty = hasValidNumber(trade.quantity) ? Number(trade.quantity) : hasValidNumber(trade.lot_size) ? Number(trade.lot_size) : null;
  const expiry = trade.expiry || UNAVAILABLE_LABEL;

  if (mode === 'SHADOW_BOOK_A_SPREAD_150' && (rctx.hedge_wing_strike || trade.legs_summary?.includes('/'))) {
    const wingStrike = hasValidNumber(rctx.hedge_wing_strike) ? Number(rctx.hedge_wing_strike) : null;
    const wingOpt = rctx.hedge_wing_option_type || trade.option_type || '';
    const shortStrike = hasValidNumber(trade.strike ?? trade.strike_price) ? Number(trade.strike ?? trade.strike_price) : null;
    const shortOpt = trade.option_type || wingOpt;
    return [
      {
        legId: `${trade.trade_id || trade.package_id}-wing`,
        role: 'Leg 1 • Protective Wing (Bought First)',
        side: 'BUY',
        contract: wingStrike ? `NIFTY ${wingStrike} ${wingOpt}` : 'Protective Wing',
        expiry,
        quantity: qty !== null ? `+${Math.abs(qty)}` : UNAVAILABLE_LABEL,
        entryPrice: hasValidNumber(rctx.hedge_wing_entry_price) ? formatINRValue(rctx.hedge_wing_entry_price) : UNAVAILABLE_LABEL,
        liveOrExitQuote: 'Bundled in Net Spread Quote (Per-leg quote unavailable)',
        hedgeState: 'HEDGE_WING_ACTIVE',
      },
      {
        legId: `${trade.trade_id || trade.package_id}-short`,
        role: 'Leg 2 • Short Credit Leg',
        side: 'SELL',
        contract: shortStrike ? `NIFTY ${shortStrike} ${shortOpt}` : trade.trading_symbol || UNAVAILABLE_LABEL,
        expiry,
        quantity: qty !== null ? `-${Math.abs(qty)}` : UNAVAILABLE_LABEL,
        entryPrice: hasValidNumber(rctx.short_leg_entry_price) ? formatINRValue(rctx.short_leg_entry_price) : UNAVAILABLE_LABEL,
        liveOrExitQuote: 'Bundled in Net Spread Quote (Per-leg quote unavailable)',
        hedgeState: 'COVERED_BY_150PT_WING',
      },
    ];
  }

  return [
    {
      legId: `${trade.trade_id || trade.package_id}-single`,
      role: trade.book_type === 'LONG_BOOK_BUY' ? 'Single Leg • Cash-Paid Long Option' : 'Single Leg • Option Position',
      side: trade.book_type === 'LONG_BOOK_BUY' ? 'BUY' : 'SELL',
      contract: trade.trading_symbol || `${trade.underlying || 'NIFTY'} ${trade.strike ?? ''} ${trade.option_type || ''}`.trim(),
      expiry,
      quantity: qty !== null ? `${trade.book_type === 'LONG_BOOK_BUY' ? '+' : '-'}${Math.abs(qty)}` : UNAVAILABLE_LABEL,
      entryPrice: hasValidNumber(trade.entry_price ?? trade.entry_price_used)
        ? formatINRValue(trade.entry_price ?? trade.entry_price_used)
        : UNAVAILABLE_LABEL,
      liveOrExitQuote:
        trade.status === 'CLOSED'
          ? formatINRValue(trade.exit_price ?? trade.exit_price_used)
          : hasValidNumber(trade.current_bid) && hasValidNumber(trade.current_ask)
          ? `${formatINRValue(trade.current_bid)} / ${formatINRValue(trade.current_ask)}`
          : UNAVAILABLE_LABEL,
      hedgeState:
        trade.book_type === 'LONG_BOOK_BUY'
          ? 'LONG_PREMIUM_PAID (NO SHORT LIABILITY)'
          : rctx.hedge_wing_strike
          ? `WING_REFERENCED (${rctx.hedge_wing_strike} ${rctx.hedge_wing_option_type || ''})`
          : 'UNHEDGED_SHORT',
    },
  ];
}

/**
 * Computes strictly source-isolated performance calendar and summary metrics.
 * NEVER mixes live broker, active forward shadow/paper (`S205_SHADOW_FORWARD_V1`),
 * legacy retired paper (`V6B_*` / `V2.0`), and backtests.
 */
export function computeScopedPerformance({
  closedTrades = [],
  shadowPackages = [],
  liveIntents = [],
  sourceFilter = 'SHADOW_FORWARD_V1',
  fromDate = '',
  toDate = '',
  selectedMonth = '',
  defaultMonth = '2026-11',
} = {}) {
  let normalizedRows = [];
  let feeLabel = 'Unavailable';
  let scopeLimitations = null;

  if (sourceFilter === 'LIVE_BROKER_RECENT') {
    feeLabel = 'Estimated Broker Statutory Charges';
    scopeLimitations =
      'Derived from recent_order_intents (capped at 25 recent rows by /live/status). Not an authoritative full-history broker ledger.';
    normalizedRows = (Array.isArray(liveIntents) ? liveIntents : [])
      .filter(
        (i) =>
          i &&
          i.exit_state === 'CONFIRMED_FLAT' &&
          i.state !== 'CANCELLED' &&
          Number(i.filled_quantity || 0) > 0
      )
      .map((i) => ({
        trade_id: i.order_intent_id,
        trading_date: String(i.trading_date || i.updated_at || i.created_at || '').slice(0, 10),
        book_type: i.action === 'BUY' ? 'LONG_BOOK_BUY' : 'SHORT_BOOK_SELL',
        strategy_mode: i.execution_mode || 'LIVE_BROKER',
        trading_symbol: i.trading_symbol || UNAVAILABLE_LABEL,
        spread_legs_label: i.hedge_wing_strike ? `Wing ${i.hedge_wing_strike}` : null,
        quantity: i.filled_quantity ?? i.target_quantity ?? null,
        entry_time: i.created_at,
        exit_time: i.updated_at,
        entry_price: hasValidNumber(i.average_fill_price) ? Number(i.average_fill_price) : i.limit_price,
        exit_price: i.exit_fill_price,
        gross_pnl: hasValidNumber(i.gross_pnl) ? Number(i.gross_pnl) : null,
        charges: hasValidNumber(i.statutory_charges) ? Number(i.statutory_charges) : null,
        net_pnl: hasValidNumber(i.realized_pnl) ? Number(i.realized_pnl) : null,
        mfe_pct: null,
        mae_pct: null,
        exit_reason: i.exit_reason || 'CONFIRMED_FLAT',
        source_scope: 'LIVE_BROKER_RECENT',
      }));
  } else if (
    sourceFilter === 'SHADOW_FORWARD_V1' ||
    sourceFilter === 'PAPER_BOOK_B' ||
    sourceFilter === 'PAPER_BOOK_A'
  ) {
    feeLabel = 'Estimated Statutory (Canonical NSE/Upstox)';
    // Combine closed mirrored forward rows in closedTrades + unwound/closed packages in shadowPackages without double counting
    const byKey = new Map();
    (Array.isArray(closedTrades) ? closedTrades : []).forEach((t) => {
      const rctx = parseRegimeContext(t);
      if (classifyStrategyEra(t.strategy_mode, rctx) !== 'SHADOW_FORWARD_V1') return;
      if (sourceFilter === 'PAPER_BOOK_B' && t.strategy_mode !== 'SHADOW_BOOK_B_PAID_FLOW') return;
      if (sourceFilter === 'PAPER_BOOK_A' && t.strategy_mode !== 'SHADOW_BOOK_A_SPREAD_150') return;
      byKey.set(String(t.trade_id), {
        trade_id: t.trade_id,
        trading_date: String(t.trading_date || t.exit_time || t.entry_time || '').slice(0, 10),
        book_type: t.book_type,
        strategy_mode: t.strategy_mode,
        trading_symbol: t.trading_symbol || UNAVAILABLE_LABEL,
        spread_legs_label: t.spread_legs_label || rctx.spread_legs_label || null,
        quantity: t.quantity ?? null,
        entry_time: t.entry_time,
        exit_time: t.exit_time,
        entry_price: t.entry_price ?? t.entry_price_used,
        exit_price: t.exit_price ?? t.exit_price_used,
        gross_pnl: hasValidNumber(t.gross_pnl) ? Number(t.gross_pnl) : null,
        charges: hasValidNumber(t.charges) ? Number(t.charges) : null,
        net_pnl: hasValidNumber(t.net_pnl) ? Number(t.net_pnl) : null,
        mfe_pct: hasValidNumber(t.mfe_pct) ? Number(t.mfe_pct) : null,
        mae_pct: hasValidNumber(t.mae_pct) ? Number(t.mae_pct) : null,
        exit_reason: t.exit_reason || 'CLOSED',
        source_scope: 'SHADOW_FORWARD_V1',
      });
    });

    (Array.isArray(shadowPackages) ? shadowPackages : []).forEach((pkg) => {
      if (!pkg || pkg.state === 'OPEN') return;
      if (sourceFilter === 'PAPER_BOOK_B' && pkg.book_id !== 'SHADOW_BOOK_B_PAID_FLOW') return;
      if (sourceFilter === 'PAPER_BOOK_A' && pkg.book_id !== 'SHADOW_BOOK_A_SPREAD_150') return;
      // Match backend s205_shadow_collector.py (`f"adv_shdw_{pkg_id[-10:]}"`) and explicit advance_trade_id
      const pkgId = String(pkg.package_id || '');
      const mirrored10 = `adv_shdw_${pkgId.slice(-10)}`;
      const mirroredStem = `adv_shdw_${pkgId.replace(/^shdw_(pkg_)?/, '')}`;
      if (
        byKey.has(pkgId) ||
        (pkg.advance_trade_id && byKey.has(String(pkg.advance_trade_id))) ||
        byKey.has(mirrored10) ||
        byKey.has(mirroredStem)
      ) {
        return;
      }
      byKey.set(pkgId, {
        trade_id: pkg.package_id,
        trading_date: String(pkg.exit_bar_ts || pkg.entry_bar_ts || '').slice(0, 10),
        book_type: pkg.ui_book_type || (pkg.book_id === 'SHADOW_BOOK_B_PAID_FLOW' ? 'LONG_BOOK_BUY' : 'SHORT_BOOK_SELL'),
        strategy_mode: pkg.book_id,
        trading_symbol: pkg.legs_summary || pkg.direction_label || UNAVAILABLE_LABEL,
        spread_legs_label: pkg.book_id === 'SHADOW_BOOK_A_SPREAD_150' ? pkg.legs_summary : null,
        quantity: 65,
        entry_time: pkg.entry_bar_ts,
        exit_time: pkg.exit_bar_ts,
        entry_price: null,
        exit_price: null,
        gross_pnl: hasValidNumber(pkg.gross_pnl_inr) ? Number(pkg.gross_pnl_inr) : null,
        charges: hasValidNumber(pkg.total_charges_inr) ? Number(pkg.total_charges_inr) : null,
        net_pnl: hasValidNumber(pkg.net_pnl_inr) ? Number(pkg.net_pnl_inr) : null,
        mfe_pct: null,
        mae_pct: null,
        exit_reason: pkg.exit_reason || pkg.state,
        source_scope: 'SHADOW_FORWARD_V1',
      });
    });

    normalizedRows = Array.from(byKey.values());
    scopeLimitations =
      'Scoped strictly to S205_SHADOW_FORWARD_V1 forward paper packages (including any INCOMPLETE_LEG_UNWOUND friction). Pre-registered historical V6B/V2.0 rows are excluded.';
  } else if (sourceFilter === 'LEGACY_PAPER') {
    feeLabel = 'Estimated Legacy Paper Charges';
    scopeLimitations =
      'Historical retired V6B_* / V2.0 paper sandbox trades (up to 200 recent closed rows from /trades). Excluded from S205_SHADOW_FORWARD_V1 forward evaluation.';
    normalizedRows = (Array.isArray(closedTrades) ? closedTrades : [])
      .filter((t) => classifyStrategyEra(t.strategy_mode, parseRegimeContext(t)) !== 'SHADOW_FORWARD_V1')
      .map((t) => ({
        trade_id: t.trade_id,
        trading_date: String(t.trading_date || t.exit_time || t.entry_time || '').slice(0, 10),
        book_type: t.book_type,
        strategy_mode: t.strategy_mode,
        trading_symbol: t.trading_symbol || UNAVAILABLE_LABEL,
        spread_legs_label: t.spread_legs_label || null,
        quantity: t.quantity ?? null,
        entry_time: t.entry_time,
        exit_time: t.exit_time,
        entry_price: t.entry_price ?? t.entry_price_used,
        exit_price: t.exit_price ?? t.exit_price_used,
        gross_pnl: hasValidNumber(t.gross_pnl) ? Number(t.gross_pnl) : null,
        charges: hasValidNumber(t.charges) ? Number(t.charges) : null,
        net_pnl: hasValidNumber(t.net_pnl) ? Number(t.net_pnl) : null,
        mfe_pct: hasValidNumber(t.mfe_pct) ? Number(t.mfe_pct) : null,
        mae_pct: hasValidNumber(t.mae_pct) ? Number(t.mae_pct) : null,
        exit_reason: t.exit_reason || 'CLOSED',
        source_scope: 'LEGACY_PAPER',
      }));
  }

  // Apply explicit date-range filter (`fromDate` .. `toDate`)
  const dateFiltered = normalizedRows.filter((row) => {
    const dt = row.trading_date;
    if (!dt || dt.length < 10) return false;
    if (fromDate && dt < fromDate) return false;
    if (toDate && dt > toDate) return false;
    return true;
  });

  const dayMap = {};
  const monthSet = new Set([defaultMonth]);
  dateFiltered.forEach((t) => {
    const dt = t.trading_date;
    monthSet.add(dt.slice(0, 7));
    if (!dayMap[dt]) {
      dayMap[dt] = {
        date: dt,
        month: dt.slice(0, 7),
        trade_count: 0,
        wins: 0,
        losses: 0,
        flat: 0,
        win_rate: 0,
        gross_pnl_rupees: 0,
        charges_rupees: 0,
        has_charges: false,
        net_pnl_rupees: 0,
        long_book_net_pnl: 0,
        short_book_net_pnl: 0,
        long_book_trades: 0,
        short_book_trades: 0,
        trades: [],
      };
    }
    const rec = dayMap[dt];
    const net = hasValidNumber(t.net_pnl) ? Number(t.net_pnl) : 0;
    const gross = hasValidNumber(t.gross_pnl) ? Number(t.gross_pnl) : net;
    const chg = hasValidNumber(t.charges) ? Number(t.charges) : 0;
    if (hasValidNumber(t.charges)) rec.has_charges = true;

    rec.trade_count += 1;
    if (net > 0) rec.wins += 1;
    else if (net < 0) rec.losses += 1;
    else rec.flat += 1;

    rec.gross_pnl_rupees = Number((rec.gross_pnl_rupees + gross).toFixed(2));
    rec.charges_rupees = Number((rec.charges_rupees + chg).toFixed(2));
    rec.net_pnl_rupees = Number((rec.net_pnl_rupees + net).toFixed(2));

    if (t.book_type === 'LONG_BOOK_BUY') {
      rec.long_book_net_pnl = Number((rec.long_book_net_pnl + net).toFixed(2));
      rec.long_book_trades += 1;
    } else {
      rec.short_book_net_pnl = Number((rec.short_book_net_pnl + net).toFixed(2));
      rec.short_book_trades += 1;
    }
    rec.win_rate = Number(((rec.wins / rec.trade_count) * 100).toFixed(1));
    rec.trades.push(t);
  });

  const availableMonths = Array.from(monthSet).filter(Boolean).sort().reverse();
  const activeMonth =
    selectedMonth && availableMonths.includes(selectedMonth)
      ? selectedMonth
      : availableMonths[0] || defaultMonth;

  const monthDays = Object.values(dayMap).filter((d) => d.date.startsWith(activeMonth));
  let totalNet = 0;
  let totalGross = 0;
  let totalCharges = 0;
  let anyCharges = false;
  let longNet = 0;
  let shortNet = 0;
  let longTrades = 0;
  let shortTrades = 0;
  let totalTrades = 0;
  let wins = 0;
  let losses = 0;
  let greenDays = 0;
  let redDays = 0;
  let grossWinSum = 0;
  let grossLossSum = 0;

  monthDays.forEach((d) => {
    totalNet += d.net_pnl_rupees;
    totalGross += d.gross_pnl_rupees;
    totalCharges += d.charges_rupees;
    if (d.has_charges) anyCharges = true;
    longNet += d.long_book_net_pnl;
    shortNet += d.short_book_net_pnl;
    longTrades += d.long_book_trades;
    shortTrades += d.short_book_trades;
    totalTrades += d.trade_count;
    wins += d.wins;
    losses += d.losses;
    if (d.net_pnl_rupees > 0) greenDays += 1;
    else if (d.net_pnl_rupees < 0) redDays += 1;
    d.trades.forEach((tr) => {
      const np = hasValidNumber(tr.net_pnl) ? Number(tr.net_pnl) : 0;
      if (np > 0) grossWinSum += np;
      else if (np < 0) grossLossSum += Math.abs(np);
    });
  });

  const profitFactor =
    totalTrades === 0
      ? null
      : grossLossSum > 0
      ? Number((grossWinSum / grossLossSum).toFixed(2))
      : grossWinSum > 0
      ? null // Infinite/undefined ratio when 0 losses; do not invent 99.9x
      : 0;

  const [yrStr, moStr] = activeMonth.split('-');
  const yr = Number(yrStr);
  const mo = Number(moStr);
  const daysInMonth = Number.isFinite(yr) && Number.isFinite(mo) ? new Date(yr, mo, 0).getDate() : 30;
  const weekdayCells = [];
  let firstWeekdayAdded = false;

  for (let dayNum = 1; dayNum <= daysInMonth; dayNum += 1) {
    const dtObj = new Date(yr, mo - 1, dayNum);
    const dow = dtObj.getDay();
    if (dow === 0 || dow === 6) continue;
    if (!firstWeekdayAdded) {
      for (let p = 0; p < dow - 1; p += 1) {
        weekdayCells.push({ isPadding: true, key: `pad-${p}` });
      }
      firstWeekdayAdded = true;
    }
    const isoDate = `${yrStr}-${String(mo).padStart(2, '0')}-${String(dayNum).padStart(2, '0')}`;
    weekdayCells.push(
      dayMap[isoDate] || {
        date: isoDate,
        month: activeMonth,
        trade_count: 0,
        wins: 0,
        losses: 0,
        win_rate: 0,
        gross_pnl_rupees: 0,
        charges_rupees: 0,
        net_pnl_rupees: 0,
        long_book_net_pnl: 0,
        short_book_net_pnl: 0,
        long_book_trades: 0,
        short_book_trades: 0,
        trades: [],
      }
    );
  }

  return {
    sourceFilter,
    feeLabel: totalTrades > 0 && !anyCharges ? UNAVAILABLE_LABEL : feeLabel,
    scopeLimitations,
    availableMonths,
    activeMonth,
    weekdayCells,
    dayMap,
    filteredTotalCount: dateFiltered.length,
    monthSummary: {
      month: activeMonth,
      total_net_pnl: Number(totalNet.toFixed(2)),
      total_gross_pnl: Number(totalGross.toFixed(2)),
      total_charges: totalTrades === 0 ? 0 : anyCharges ? Number(totalCharges.toFixed(2)) : null,
      long_book_net_pnl: Number(longNet.toFixed(2)),
      short_book_net_pnl: Number(shortNet.toFixed(2)),
      long_book_trades: longTrades,
      short_book_trades: shortTrades,
      total_trades: totalTrades,
      wins,
      losses,
      win_rate: totalTrades > 0 ? Number(((wins / totalTrades) * 100).toFixed(1)) : null,
      profit_factor: profitFactor,
      no_loss_wins_only: totalTrades > 0 && grossLossSum === 0 && grossWinSum > 0,
      green_days: greenDays,
      red_days: redDays,
      trading_days: monthDays.length,
    },
  };
}

/**
 * Single-owner monotonic polling coordinator.
 * Prevents overlapping polls on the same tier and rejects out-of-order responses
 * so an older/slower HTTP response can NEVER overwrite newer state.
 */
export function createPollingCoordinator() {
  const inFlight = { fast: false, slow: false };
  const nextSeqBySlice = {};
  const appliedSeqBySlice = {};

  return {
    canStartTier(tier) {
      return !inFlight[tier];
    },
    beginTier(tier) {
      if (inFlight[tier]) return false;
      inFlight[tier] = true;
      return true;
    },
    endTier(tier) {
      inFlight[tier] = false;
    },
    issueSliceToken(slice) {
      const next = (nextSeqBySlice[slice] || 0) + 1;
      nextSeqBySlice[slice] = next;
      return next;
    },
    shouldApplySliceResponse(slice, token) {
      const latestApplied = appliedSeqBySlice[slice] || 0;
      if (token < latestApplied) {
        return false;
      }
      appliedSeqBySlice[slice] = token;
      return true;
    },
    getAppliedSeq(slice) {
      return appliedSeqBySlice[slice] || 0;
    },
  };
}
