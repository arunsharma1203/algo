import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  UNAVAILABLE_LABEL,
  buildPackageLegRows,
  classifyStrategyEra,
  computeScopedPerformance,
  createPollingCoordinator,
  deriveCandidateBlocker,
  deriveExecutionStatus,
  formatINRValue,
  formatNumber,
  formatPercent,
  formatSignedINR,
  hasValidNumber,
} from '../src/pages/optionSnifferAdvance/formatters.js';
import {
  FIXTURE_SCENARIOS,
  getFixtureBundle,
} from '../src/pages/optionSnifferAdvance/fixtures.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SRC_DIR = path.resolve(__dirname, '../src/pages');

test('Truthful formatting preserves genuine zero and negatives while rendering missing as Unavailable', () => {
  assert.equal(hasValidNumber(0), true);
  assert.equal(hasValidNumber(-0), true);
  assert.equal(hasValidNumber(-587.4), true);
  assert.equal(hasValidNumber(null), false);
  assert.equal(hasValidNumber(undefined), false);
  assert.equal(hasValidNumber(''), false);
  assert.equal(hasValidNumber(Number.NaN), false);
  assert.equal(hasValidNumber(false), false);

  // Genuine zero never turns into Unavailable or fallback numbers
  assert.equal(formatINRValue(0), '₹0.00');
  assert.equal(formatSignedINR(0), '₹0.00');
  assert.equal(formatSignedINR(-0), '₹0.00');
  assert.equal(formatPercent(0), '0.0%');
  assert.equal(formatNumber(0, { decimals: 2 }), '0.00');

  // Signed positive and negative values
  assert.equal(formatSignedINR(284.15), '+₹284.15');
  assert.equal(formatSignedINR(-587.4), '-₹587.40');
  assert.equal(formatINRValue(-1948.61), '-₹1,948.61');

  // Missing values render Unavailable
  assert.equal(formatINRValue(null), UNAVAILABLE_LABEL);
  assert.equal(formatSignedINR(undefined), UNAVAILABLE_LABEL);
  assert.equal(formatPercent(null), UNAVAILABLE_LABEL);
});

test('deriveExecutionStatus never treats missing, failed, or stale responses as confirmed safe/disarmed', () => {
  const now = 1_700_000_050_000;

  // 1. Unknown when /live/status failed (Fixture 2)
  const unknownBundle = getFixtureBundle('unknown_status', now);
  const unknownExec = deriveExecutionStatus({
    liveStatus: unknownBundle.live.data,
    liveFetchState: {
      status: 'error',
      lastSuccessMs: unknownBundle.live.retrievedAtMs,
      errorMessage: unknownBundle.live.error,
    },
    nowMs: now,
  });
  assert.equal(unknownExec.state, 'UNKNOWN');
  assert.equal(unknownExec.canConfirmSafe, false);
  assert.equal(unknownExec.tone, 'warning');

  // 2. Stale when last successful disarmed poll is >15s old (Fixture 4)
  const degradedBundle = getFixtureBundle('degraded_partial', now);
  const staleExec = deriveExecutionStatus({
    liveStatus: degradedBundle.live.data,
    liveFetchState: {
      status: 'ok',
      lastSuccessMs: degradedBundle.live.retrievedAtMs,
      errorMessage: null,
    },
    nowMs: now,
  });
  assert.equal(staleExec.state, 'STALE');
  assert.equal(staleExec.canConfirmSafe, false);
  assert.equal(staleExec.tone, 'warning');

  // 3. Stale while armed stays danger / armed
  const armedStale = deriveExecutionStatus({
    liveStatus: { is_live_armed: true, execution_mode: 'DUAL_BOOK_HEDGED' },
    liveFetchState: { status: 'error', lastSuccessMs: now - 30_000, errorMessage: 'Timeout' },
    nowMs: now,
  });
  assert.equal(armedStale.state, 'ARMED_STALE');
  assert.equal(armedStale.isArmed, true);
  assert.equal(armedStale.canConfirmSafe, false);
  assert.equal(armedStale.tone, 'danger');

  // 4. Fresh confirmed disarmed
  const freshSafe = deriveExecutionStatus({
    liveStatus: { is_live_armed: false, execution_mode: 'LONG_BOOK_ONLY' },
    liveFetchState: { status: 'ok', lastSuccessMs: now - 500, errorMessage: null },
    nowMs: now,
  });
  assert.equal(freshSafe.state, 'DISARMED_PAPER');
  assert.equal(freshSafe.canConfirmSafe, true);
  assert.equal(freshSafe.tone, 'safe');
});

test('Broker, active forward paper, and legacy retired paper ledgers stay strictly isolated', () => {
  const now = 1_700_000_050_000;
  const activeBundle = getFixtureBundle('overview_active', now);

  // Forward V1 only includes S205_SHADOW_FORWARD_V1 (-587.40), never V6B (-1948.61) or Broker (-565.90)
  const forwardPerf = computeScopedPerformance({
    closedTrades: activeBundle.trades.data.closed_trades,
    shadowPackages: activeBundle.readiness.data.shadow_telemetry.recent_packages,
    liveIntents: activeBundle.live.data.recent_order_intents,
    sourceFilter: 'SHADOW_FORWARD_V1',
    defaultMonth: '2026-11',
  });
  assert.equal(forwardPerf.monthSummary.total_trades, 1);
  assert.equal(forwardPerf.monthSummary.total_net_pnl, -587.4);

  // Legacy Paper only includes historical V6B (-1948.61)
  const legacyPerf = computeScopedPerformance({
    closedTrades: activeBundle.trades.data.closed_trades,
    shadowPackages: activeBundle.readiness.data.shadow_telemetry.recent_packages,
    liveIntents: activeBundle.live.data.recent_order_intents,
    sourceFilter: 'LEGACY_PAPER',
    defaultMonth: '2026-11',
  });
  assert.equal(legacyPerf.monthSummary.total_trades, 1);
  assert.equal(legacyPerf.monthSummary.total_net_pnl, -1948.61);

  // Broker recent intents only includes CONFIRMED_FLAT broker intents (-565.90) + limitation disclosure
  const brokerPerf = computeScopedPerformance({
    closedTrades: activeBundle.trades.data.closed_trades,
    shadowPackages: activeBundle.readiness.data.shadow_telemetry.recent_packages,
    liveIntents: activeBundle.live.data.recent_order_intents,
    sourceFilter: 'LIVE_BROKER_RECENT',
    defaultMonth: '2026-11',
  });
  assert.equal(brokerPerf.monthSummary.total_trades, 1);
  assert.equal(brokerPerf.monthSummary.total_net_pnl, -565.9);
  assert.match(brokerPerf.scopeLimitations, /capped at 25 recent rows/i);

  // Date filter excludes out-of-range dates
  const filteredEmpty = computeScopedPerformance({
    closedTrades: activeBundle.trades.data.closed_trades,
    shadowPackages: activeBundle.readiness.data.shadow_telemetry.recent_packages,
    liveIntents: activeBundle.live.data.recent_order_intents,
    sourceFilter: 'SHADOW_FORWARD_V1',
    fromDate: '2026-11-15',
    toDate: '2026-11-15',
    defaultMonth: '2026-11',
  });
  assert.equal(filteredEmpty.monthSummary.total_trades, 0);
  assert.equal(filteredEmpty.monthSummary.total_net_pnl, 0);
  assert.equal(filteredEmpty.monthSummary.win_rate, null);
  assert.equal(filteredEmpty.monthSummary.profit_factor, null);
});

test('Market closed empty ledger and incomplete-leg spread unwinds render honestly', () => {
  const now = 1_700_000_050_000;
  const closedBundle = getFixtureBundle('market_closed_zero', now);
  const zeroPerf = computeScopedPerformance({
    closedTrades: closedBundle.trades.data.closed_trades,
    shadowPackages: closedBundle.readiness.data.shadow_telemetry.recent_packages,
    liveIntents: closedBundle.live.data.recent_order_intents,
    sourceFilter: 'SHADOW_FORWARD_V1',
    defaultMonth: '2026-11',
  });
  assert.equal(zeroPerf.monthSummary.total_net_pnl, 0);
  assert.equal(zeroPerf.monthSummary.win_rate, null);
  assert.equal(zeroPerf.monthSummary.profit_factor, null);

  // Spread 2-leg package expansion + unwind friction accounting
  const activeBundle = getFixtureBundle('overview_active', now);
  const openSpread = activeBundle.trades.data.open_trades[0];
  assert.equal(classifyStrategyEra(openSpread.strategy_mode, openSpread.regime_context), 'SHADOW_FORWARD_V1');
  const legs = buildPackageLegRows(openSpread);
  assert.equal(legs.length, 2);
  assert.equal(legs[0].side, 'BUY');
  assert.equal(legs[1].side, 'SELL');

  const unwindBundle = getFixtureBundle('spread_unwind', now);
  const unwindPerf = computeScopedPerformance({
    closedTrades: unwindBundle.trades.data.closed_trades,
    shadowPackages: unwindBundle.readiness.data.shadow_telemetry.recent_packages,
    liveIntents: unwindBundle.live.data.recent_order_intents,
    sourceFilter: 'SHADOW_FORWARD_V1',
    defaultMonth: '2026-11',
  });
  // Includes both the closed Nov 14 Book B row (-587.40) and the Nov 15 INCOMPLETE_LEG_UNWOUND wing friction row (-142.30)
  assert.equal(unwindPerf.monthSummary.total_net_pnl, -729.7);
  assert.equal(unwindPerf.dayMap['2026-11-15'].net_pnl_rupees, -142.3);

  // Rejected broker intent is excluded from CONFIRMED_FLAT realized broker P&L
  const rejBrokerPerf = computeScopedPerformance({
    closedTrades: [],
    shadowPackages: [],
    liveIntents: unwindBundle.live.data.recent_order_intents,
    sourceFilter: 'LIVE_BROKER_RECENT',
    defaultMonth: '2026-11',
  });
  assert.equal(rejBrokerPerf.monthSummary.total_trades, 0);
});

test('deriveCandidateBlocker surfaces deterministic blocker without implying expectancy', () => {
  const bundle = getFixtureBundle('overview_active', Date.now());
  const [bookB, bookA, retiredV6B] = bundle.readiness.data.engine_readiness;

  const blockerB = deriveCandidateBlocker(bookB);
  assert.match(blockerB.principalBlocker, /ObsL5Depth/);
  assert.equal(blockerB.direction, 'BUY_CE');

  const blockerA = deriveCandidateBlocker(bookA);
  assert.match(blockerA.principalBlocker, /expectancy unverified/i);
  assert.equal(blockerA.direction, 'CREDIT_SPREAD_PE');

  const blockerV6 = deriveCandidateBlocker(retiredV6B);
  assert.match(blockerV6.principalBlocker, /Retired from execution/i);
});

test('createPollingCoordinator prevents overlapping tier polls and rejects out-of-order responses', () => {
  const coord = createPollingCoordinator();
  assert.equal(coord.beginTier('fast'), true);
  assert.equal(coord.beginTier('fast'), false); // Overlapping fast poll blocked
  assert.equal(coord.beginTier('slow'), true);  // Independent slow tier allowed
  coord.endTier('fast');
  assert.equal(coord.beginTier('fast'), true);
  coord.endTier('fast');
  coord.endTier('slow');

  // Out-of-order response sequence guard
  const slowReq1 = coord.issueSliceToken('live');
  const fastReq2 = coord.issueSliceToken('live');
  assert.equal(coord.shouldApplySliceResponse('live', fastReq2), true);
  // Older response arriving late must be rejected
  assert.equal(coord.shouldApplySliceResponse('live', slowReq1), false);
  assert.equal(coord.getAppliedSeq('live'), fastReq2);
});

test('Static source audit: default Overview workspace, zero misleading fallbacks, accessible dialogs, and zero automatic POSTs', () => {
  assert.equal(FIXTURE_SCENARIOS.length, 5);

  const mainFile = fs.readFileSync(path.join(SRC_DIR, 'OptionSnifferAdvance.jsx'), 'utf8');
  const subDir = path.join(SRC_DIR, 'optionSnifferAdvance');
  const subFiles = fs
    .readdirSync(subDir)
    .filter((f) => f.endsWith('.js') || f.endsWith('.jsx'))
    .map((f) => ({ name: f, content: fs.readFileSync(path.join(subDir, f), 'utf8') }));

  const allSources = [{ name: 'OptionSnifferAdvance.jsx', content: mainFile }, ...subFiles];

  // 1. Default workspace is 'overview', never 'calendar'
  assert.match(mainFile, /validTab\s*=\s*WORKSPACE_TABS\.some.*:\s*'overview'/);

  // 2. Banned hardcoded fallbacks and unsupported marketing claims are absent
  const bannedPatterns = [
    '14.37',
    '17,910.94',
    '17910.94',
    '85.7%',
    '10666.61',
    '7244.33',
    '+₹5,779',
    '2600:1900:40a0:83a::63',
    '-₹2,800.00',
    'Catastrophe Shield',
    'Net Alpha',
    'Safe Capital',
    '0 Intrabar Bias',
    'Sub-Second Flatten',
  ];

  for (const src of allSources) {
    for (const banned of bannedPatterns) {
      assert.equal(
        src.content.includes(banned),
        false,
        `Found banned fallback/claim "${banned}" in ${src.name}`
      );
    }
  }

  // 3. No POST calls exist in child presentation tabs or formatters
  for (const src of subFiles) {
    assert.equal(
      /method:\s*['"]POST['"]/i.test(src.content),
      false,
      `Unexpected POST request found in child module ${src.name}`
    );
  }

  // 4. All 5 POST calls in OptionSnifferAdvance.jsx are strictly inside explicit human handlers (after polling lifecycle)
  const postMatches = mainFile.match(/method:\s*['"]POST['"]/g) || [];
  assert.equal(postMatches.length, 5);
  const humanActionsStart = mainFile.indexOf('// Explicit human-initiated actions ONLY');
  assert.equal(humanActionsStart > 0, true);
  assert.equal(/method:\s*['"]POST['"]/i.test(mainFile.slice(0, humanActionsStart)), false);

  // 5. Accessible keyboard-operable dialogs in ArmInterlockModal and PerformanceTab
  const armModalSrc = subFiles.find((f) => f.name === 'ArmInterlockModal.jsx')?.content || '';
  const perfTabSrc = subFiles.find((f) => f.name === 'PerformanceTab.jsx')?.content || '';
  assert.match(armModalSrc, /role="dialog"/);
  assert.match(armModalSrc, /aria-modal="true"/);
  assert.match(armModalSrc, /Escape/);
  assert.match(perfTabSrc, /role="dialog"/);
  assert.match(perfTabSrc, /aria-modal="true"/);
  assert.match(perfTabSrc, /Escape/);
});
