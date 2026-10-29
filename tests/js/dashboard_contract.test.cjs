const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../quant_monitor/web/app.js'), 'utf8');

test('dashboard renders an auditable freshness marker for each quote row', () => {
  assert.match(source, /function quoteFreshness\(quote\)/);
  assert.match(source, /threshold_seconds/);
  assert.match(source, /quote-asof/);
  assert.match(source, /refreshQuoteFreshnessIndicators/);
  assert.match(source, /served_from/); // cache provenance remains part of the UI contract
});

test('freshness states include stale and timestamp anomalies', () => {
  assert.match(source, /future_timestamp/);
  assert.match(source, /status === "stale"/);
  assert.match(source, /该行情没有可验证的时间戳/);
});

test('market source badge downgrades when received quotes are not fresh', () => {
  assert.match(source, /const freshnessStates = rows\.map\(quoteFreshness\)/);
  assert.match(source, /freshnessWarnings = freshnessStates\.filter/);
  assert.match(source, /const sourceStatus = receivedCount <= 0 \|\| !rows\.length \? "warning" : freshnessWarnings\.length \? "warning" : "ok"/);
  assert.match(source, /dom\.marketSource\.title/);
  assert.match(source, /freshness: item\.freshness/);
});

test('market badge discloses unknown calendar coverage', () => {
  assert.match(source, /calendar_lookup/);
  assert.match(source, /calendarUnknown/);
  assert.match(source, /交易日历未覆盖当前日期/);
});

test('bank overview distinguishes research coverage from decision evidence', () => {
  assert.match(source, /const qualityReady = finiteNumber/);
  assert.match(source, /const decisionReady = finiteNumber/);
  assert.match(source, /质量证据/);
  assert.match(source, /决策证据/);
});

test('price chart exposes cached projection provenance instead of implying authority', () => {
  assert.match(source, /chart-provenance/);
  assert.match(source, /renderChartProvenance\(provenance/);
  assert.match(source, /data\.authoritative === true && matched === served/);
  assert.match(source, /K 线缓存投影/);
  assert.match(source, /不可变观测/);
});

test('refresh fan-out is single-flight and always releases the loading state', () => {
  assert.match(source, /refreshInFlight: null/);
  assert.match(source, /if \(state\.refreshInFlight\) \{/);
  assert.match(source, /state\.refreshInFlight = run;/);
  assert.match(source, /if \(state\.refreshInFlight === run\) state\.refreshInFlight = null;/);
  assert.match(source, /dom\.refresh\.classList\.remove\("loading"\)/);
});

test('structured API errors preserve provider diagnostics and retry hints', () => {
  assert.match(source, /class ApiRequestError extends Error/);
  assert.match(source, /this\.code = code/);
  assert.match(source, /this\.retryAfter = retryAfter/);
  assert.match(source, /response\.headers\.get\("Retry-After"\)/);
  assert.match(source, /requestErrorMessage\(error/);
  assert.match(source, /error && error\.status === 409/);
});

test('historical research events show missing input lineage and binding status', () => {
  assert.match(source, /raw\.research_assurance/);
  assert.match(source, /assurance\.strategy_binding_status/);
  assert.match(source, /策略摘要一致（非来源认证）/);
  assert.match(source, /策略绑定异常/);
  assert.match(source, /输入血缘未绑定/);
  assert.match(source, /历史研究输出/);
});

test('analysis display fails closed and identifies the actual calculation input', () => {
  assert.match(source, /dataProvenance: firstDefined\(data, \["data_provenance", "provenance"\]/);
  assert.match(source, /calculation_input/);
  assert.match(source, /计算输入：不可变记录/);
  assert.match(source, /计算输入：兼容缓存投影/);
  assert.match(source, /const analysisReady = q\.analysis_ready === true/);
  assert.match(source, /const decisionReady = q\.decision_ready === true/);
  assert.match(source, /const analysisUsable = analysisReady && failuresValid && failures\.length === 0/);
  assert.match(source, /研究结果可用 · 未认证/);
  assert.match(source, /仅供研究，不构成交易指令/);
  assert.match(source, /entryEligible === true && analysisUsable && decisionReady/);
  assert.match(source, /入场门控 计算通过 · 未认证/);
  assert.match(source, /研究条件未满足/);
  assert.match(source, /const action = analysisUsable \? /);
});

test('analysis readiness preserves research-only bias while certification is unavailable', () => {
  const prefix = source.slice(0, source.indexOf('(() => {'));
  const context = {};
  vm.runInNewContext(`${prefix}; this.readiness = quantMonitorAnalysisReadiness;`, context);
  const usable = context.readiness(
    { analysis_ready: true, decision_ready: false, analysis_readiness_failures: [] },
    { calculation_input: 'immutable_observations' },
  );
  assert.equal(usable.analysisUsable, true);
  assert.equal(usable.researchReady, false);
  assert.equal(usable.inputLabel, '计算输入：不可变记录');

  const insufficient = context.readiness({}, { calculation_input: 'legacy_bars_projection' });
  assert.equal(insufficient.analysisUsable, false);
  assert.equal(insufficient.researchReady, false);
  assert.equal(insufficient.inputLabel, '计算输入：兼容缓存投影');
  assert.equal(context.readiness({ analysis_ready: true, decision_ready: true }, {}).analysisUsable, false);
  assert.equal(context.readiness({ analysis_ready: true, analysis_readiness_failures: {} }, {}).analysisUsable, false);
  assert.equal(context.readiness({ analysis_ready: true, analysis_readiness_failures: ['stale'] }, {}).analysisUsable, false);
});

test('freshness summary exposes aggregate stale coverage for market operators', () => {
  const prefix = source.slice(0, source.indexOf('(() => {'));
  const context = {};
  vm.runInNewContext(`${prefix}; this.freshness = quantMonitorFreshnessSummary;`, context);
  assert.deepEqual(JSON.parse(JSON.stringify(context.freshness([]))), {
    total: 0, problemCount: 0, status: 'empty', counts: {},
  });
  assert.deepEqual(JSON.parse(JSON.stringify(context.freshness(['fresh', 'stale', 'missing', 'future_timestamp', 'unknown']))), {
    total: 5,
    problemCount: 4,
    status: 'warning',
    counts: { stale: 1, missing: 1, future_timestamp: 1, unknown: 1 },
  });
  assert.deepEqual(JSON.parse(JSON.stringify(context.freshness(['fresh', 'fresh']))), {
    total: 2, problemCount: 0, status: 'ok', counts: {},
  });
});

test('bank market note discloses freshness coverage instead of only provider status', () => {
  assert.match(source, /function freshnessNotice\(items\)/);
  assert.match(source, /function refreshMarketFreshnessSummary\(\)/);
  assert.match(source, /行情新鲜度待核验/);
  assert.match(source, /marketInsightsNote\.dataset\.freshness/);
  assert.match(source, /setAttribute\("aria-live", "polite"\)/);
  assert.match(source, /refreshMarketFreshnessSummary\(\);/);
});

test('daily decision digest keeps the research-only boundary visible', () => {
  const index = fs.readFileSync(path.join(__dirname, '../../quant_monitor/web/index.html'), 'utf8');
  assert.match(source, /function renderDecisionDigest\(payload\)/);
  assert.match(source, /function loadDecisionDigest\(\)/);
  assert.match(source, /dashboard\/daily\?limit=50/);
  assert.match(source, /research_assurance/);
  assert.match(source, /orders_supported/);
  assert.match(index, /id="decision-digest-panel"/);
  assert.match(index, /id="decision-digest-quality"/);
  assert.match(index, /每日决策摘要/);
});

test('workspace navigation and local preferences are wired into the dashboard contract', () => {
  const index = fs.readFileSync(path.join(__dirname, '../../quant_monitor/web/index.html'), 'utf8');
  const styles = fs.readFileSync(path.join(__dirname, '../../quant_monitor/web/styles.css'), 'utf8');
  assert.match(index, /class="view-nav-link active"/);
  assert.match(index, /id="theme-toggle"/);
  assert.match(index, /id="density-toggle"/);
  assert.match(index, /id="task-strip"/);
  assert.match(index, /id="bank-filter"/);
  assert.match(source, /function applyPreferences()/);
  assert.match(source, /function renderTaskStrip\(payload\)/);
  assert.match(source, /localStorage\.setItem\("quant-monitor-theme"/);
  assert.match(source, /bindWorkspaceNavigation()/);
  assert.match(source, /state.bankFilter/);
  assert.match(styles, /.hero-dashboard-grid/);
  assert.match(styles, /body\[data-theme="light"\]/);
  assert.match(styles, /\.compact-mode/);
});
