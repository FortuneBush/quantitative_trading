const test = require('node:test');
const assert = require('node:assert/strict');
const Report = require('../../quant_monitor/web/research-report.js');

function responses(id = 'run-a', hash = 'sha256:fixture') {
  const envelope = { run_id: id, result_hash: hash, served_from: 'sqlite_cache', upstream_requests: 0 };
  return {
    result: { ok: true, payload: { ...envelope, result: { strategy_name: 'fixture', metrics: { total_return: .05 }, diagnostics: { warnings: ['raw_unadjusted'] } } } },
    risk: { ok: true, payload: { ...envelope, risk: { sample_sufficient: true, minimum_observations: 20, return_observations: 25, missing_return_observations: 0, historical_var: { '0.95': .04 }, warnings: [] } } },
    integrity: { ok: true, payload: { ...envelope, integrity: { verified: true, checks: { returns_match: true }, mismatches: [] } } },
    governance: { ok: true, payload: { ...envelope, artifacts_consistent: true, reproducible: false, decision_ready: false,
      checks: { replay_verified: false }, universe_binding: { required: false, status: 'not_declared', matched_snapshot_hashes: [], missing_snapshot_hashes: [] },
      warnings: ['decision_grade_unattested'], notice: '治理仅用于研究审计。' } },
  };
}

test('report separates sample gate, calculation verification and provenance', () => {
  const model = Report.buildModel('run-a', responses());
  assert.equal(model.sampleReady, true);
  assert.equal(model.verified, true);
  assert.equal(model.coverageComplete, false);
  assert.equal(model.coveragePartial, true);
  assert.deepEqual(model.errors, {});
  assert.match(model.notice, /不代表.*可交易/);
});

test('report exposes governance and PIT evidence without turning it into approval', () => {
  const data = responses();
  data.governance.payload = {
    ...data.governance.payload,
    artifacts_consistent: true,
    reproducible: true,
    decision_ready: false,
    checks: { replay_verified: true, decision_grade: false },
    universe_binding: {
      required: true, status: 'missing', matched_snapshot_hashes: ['sha256:matched'],
      missing_snapshot_hashes: ['sha256:missing'], missing_snapshot_sessions: ['2024-01-02'],
    },
    warnings: ['universe_snapshot_evidence_missing'],
  };
  const model = Report.buildModel('run-a', data);
  assert.equal(model.hasGovernance, true);
  assert.equal(model.governanceReplayVerified, true);
  assert.equal(model.governanceDecisionReady, false);
  assert.equal(model.universeBinding.status, 'missing');
  function node(tag) {
    return { tag, children: [], textContent: '', appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {} };
  }
  const container = { ...node('section'), ownerDocument: { createElement: node } };
  Report.render(container, { phase: 'ready', runId: 'run-a', model, close() {}, retry() {} });
  const texts = [];
  (function scan(n) { texts.push(n.textContent); n.children.forEach(scan); })(container);
  assert.ok(texts.some((text) => text.includes('研究治理 · 独立重放已通过')));
  assert.ok(texts.some((text) => text.includes('PIT 股票池证据')));
  assert.ok(texts.some((text) => text.includes('missing · 已匹配 1 · 缺失 1')));
  assert.ok(texts.some((text) => text.includes('治理警告：universe_snapshot_evidence_missing')));
  assert.ok(texts.some((text) => text.includes('决策就绪')));
});

test('governance cannot be paired with a different immutable result hash', () => {
  const data = responses();
  data.governance.payload.result_hash = 'sha256:other';
  const model = Report.buildModel('run-a', data);
  assert.equal(model.hasGovernance, false);
  assert.equal(model.errors.governance, 'result_hash_mismatch');
});

test('governance aggregate labels cannot bypass an explicit replay check', () => {
  const data = responses();
  data.governance.payload.reproducible = true;
  data.governance.payload.checks = {};
  data.governance.payload.replay_audit = { verified: false };
  const model = Report.buildModel('run-a', data);
  assert.equal(model.governanceReplayVerified, false);
});

test('integrity coverage is explicit and audit warnings are rendered as text', () => {
  const data = responses();
  data.integrity.payload.integrity = {
    verified: true,
    checks: { returns_match: true },
    mismatches: [],
    trade_statistics_status: 'unavailable',
    unavailable_checks: ['closed_trade_count'],
    warnings: ['<integrity-warning>'],
  };
  const model = Report.buildModel('run-a', data);
  assert.equal(model.verified, true);
  assert.equal(model.coverageComplete, false);
  assert.equal(model.coveragePartial, true);
  assert.deepEqual(model.unavailableChecks, ['closed_trade_count']);
  assert.deepEqual(model.integrityWarnings, ['<integrity-warning>']);
  function node(tag) {
    return { tag, children: [], textContent: '', appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {} };
  }
  const container = { ...node('section'), ownerDocument: { createElement: node } };
  Report.render(container, { phase: 'ready', runId: 'run-a', model, close() {}, retry() {} });
  const texts = [];
  (function scan(n) { texts.push(n.textContent); n.children.forEach(scan); })(container);
  assert.ok(texts.includes('已覆盖指标一致 · 部分项目未核验'));
  assert.ok(texts.includes('未核验：closed_trade_count'));
  assert.ok(texts.includes('审计警告：<integrity-warning>'));
});

test('only an explicitly verified, fully covered integrity report gets the full label', () => {
  const data = responses();
  data.integrity.payload.integrity.trade_statistics_status = 'verified';
  data.integrity.payload.integrity.unavailable_checks = [];
  const model = Report.buildModel('run-a', data);
  assert.equal(model.verified, true);
  assert.equal(model.coverageComplete, true);
  assert.equal(model.coveragePartial, false);
  data.integrity.payload.integrity.trade_statistics_status = 'failed';
  const failed = Report.buildModel('run-a', data);
  assert.equal(failed.verified, true);
  assert.equal(failed.coverageComplete, false);
  assert.equal(failed.coveragePartial, false);
});

test('invalid unavailable-check metadata never promotes coverage to complete', () => {
  for (const value of [undefined, null, 'closed_trade_count', [''], [1], [null]]) {
    const data = responses();
    data.integrity.payload.integrity.trade_statistics_status = 'verified';
    data.integrity.payload.integrity.unavailable_checks = value;
    const model = Report.buildModel('run-a', data);
    assert.equal(model.coverageComplete, false);
    assert.equal(model.coveragePartial, true);
    assert.equal(model.unavailableChecksValid, false);
  }
});

test('missing returns or unsupported old risk metadata never appear ready', () => {
  const data = responses();
  data.risk.payload.risk.missing_return_observations = 1;
  assert.equal(Report.buildModel('run-a', data).sampleReady, false);
  delete data.risk.payload.risk.missing_return_observations;
  assert.equal(Report.buildModel('run-a', data).sampleReady, false);
  data.risk.payload.risk.missing_return_observations = 0;
  data.risk.payload.risk.return_observations = 1;
  assert.equal(Report.buildModel('run-a', data).sampleReady, false);
});

test('different runs, hashes and unverified origins are rejected independently', () => {
  const data = responses();
  data.risk.payload.result_hash = 'different';
  data.integrity.payload.run_id = 'different';
  let model = Report.buildModel('run-a', data);
  assert.equal(model.hasResult, true);
  assert.equal(model.hasRisk, false);
  assert.equal(model.verified, false);
  assert.equal(model.errors.risk, 'result_hash_mismatch');
  assert.equal(model.errors.integrity, 'run_id_mismatch');
  const other = responses();
  other.risk.payload.upstream_requests = 1;
  model = Report.buildModel('run-a', other);
  assert.equal(model.errors.risk, 'provenance_unverified');
});

test('verified flag alone or unknown checks cannot promote an audit', () => {
  const data = responses();
  data.integrity.payload.integrity.checks = {};
  assert.equal(Report.buildModel('run-a', data).verified, false);
  data.integrity.payload.integrity.checks = { a: 'true' };
  assert.equal(Report.buildModel('run-a', data).verified, false);
});

test('format preserves missing values instead of coercing booleans or arrays to zero', () => {
  for (const missing of [null, undefined, '', ' ', true, false, [], {}, NaN, Infinity]) {
    assert.equal(Report.format(missing), '—');
  }
  assert.equal(Report.format(0), '0.00%');
  assert.equal(Report.format('0.025'), '2.50%');
});

test('money formatting preserves the sign of ordinary and explicit signed amounts', () => {
  assert.equal(Report.format(-12.5, 'money'), '-12.50');
  assert.equal(Report.format(-12.5, 'signed_money'), '-12.50');
  assert.equal(Report.format(12.5, 'money'), '12.50');
  assert.equal(Report.format(12.5, 'signed_money'), '+12.50');
  assert.equal(Report.format(null, 'money'), '—');
});

test('explicit open makes exactly four cache-only GETs and reports partial failures', async () => {
  const calls = [], updates = [];
  const data = responses('run/a');
  const controller = Report.createController({
    fetcher: async (url, options) => {
      calls.push({ url, options });
      const part = url.split('/').pop();
      if (part === 'integrity') return { ok: false, status: 503 };
      return { ok: true, json: async () => data[part].payload };
    },
    onUpdate: (value) => updates.push(value),
  });
  assert.equal(calls.length, 0);
  await controller.open('run/a');
  assert.equal(calls.length, 4);
  assert.ok(calls.every(({ url, options }) => url.startsWith('/api/v1/research/runs/run%2Fa/') && !url.includes('refresh') && options.method === 'GET'));
  assert.equal(updates.at(-1).model.hasRisk, true);
  assert.equal(updates.at(-1).model.verified, false);
  assert.equal(updates.at(-1).model.errors.integrity, 'http_503');
});

test('same-run clicks coalesce while old late responses never replace a new run', async () => {
  const waiting = [], updates = [];
  const controller = Report.createController({
    fetcher: (url, options) => new Promise((resolve) => waiting.push({ url, options, resolve })),
    onUpdate: (value) => updates.push(value),
  });
  const first = controller.open('run-a');
  assert.equal(controller.open('run-a'), first);
  assert.equal(waiting.length, 4);
  const second = controller.open('run-b');
  assert.ok(waiting[0].options.signal.aborted);
  function finish(items, id) {
    for (const item of items) item.resolve({ ok: true, json: async () => responses(id)[item.url.split('/').pop()].payload });
  }
  finish(waiting.slice(4), 'run-b');
  await second;
  finish(waiting.slice(0, 4), 'run-a');
  await first;
  assert.deepEqual(updates.filter((v) => v.phase === 'ready').map((v) => v.runId), ['run-b']);
});

test('close cancels active selection and cannot be reopened by its late response', async () => {
  const waiting = [], updates = [];
  const controller = Report.createController({
    fetcher: (url, options) => new Promise((resolve) => waiting.push({ resolve, options })),
    onUpdate: (value) => updates.push(value),
  });
  const load = controller.open('run-a');
  controller.close();
  assert.equal(waiting[0].options.signal.aborted, true);
  waiting.forEach((v) => v.resolve({ ok: false, status: 404 }));
  await load;
  assert.equal(updates.at(-1).phase, 'idle');
});

test('timeout is bounded even for an uncooperative transport and retry is available', async () => {
  const updates = [];
  const controller = Report.createController({
    fetcher: () => new Promise(() => {}), onUpdate: (value) => updates.push(value), timeoutMs: 5,
  });
  await controller.open('run-a');
  assert.equal(updates.at(-1).phase, 'ready');
  assert.equal(updates.at(-1).model.errors.result, 'timeout');
  assert.equal(typeof updates.at(-1).retry, 'function');
});

test('renderer treats untrusted warnings as text and hides incomplete tail metrics', () => {
  const data = responses();
  data.risk.payload.risk.missing_return_observations = 1;
  data.risk.payload.risk.warnings = ['<script>bad()</script>'];
  function node(tag) {
    return { tag, children: [], textContent: '', appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {},
      set innerHTML(_) { throw new Error('HTML interpolation is forbidden'); } };
  }
  const doc = { createElement: node };
  const container = { ...node('section'), ownerDocument: doc };
  Report.render(container, { phase: 'ready', runId: 'run-a', model: Report.buildModel('run-a', data), close() {}, retry() {} });
  const texts = [];
  (function scan(n) { texts.push(n.textContent); n.children.forEach(scan); })(container);
  assert.ok(texts.includes('<script>bad()</script>'));
  assert.ok(texts.includes('尾部风险 · 数据不足或不完整'));
  assert.ok(!texts.includes('4.00%'));
});

test('renderer exposes persisted trade-quality metrics without coercing missing values', () => {
  const data = responses();
  Object.assign(data.result.payload.result.metrics, {
    trade_count: 8, closed_trade_count: 4, winning_trade_count: 3, losing_trade_count: 1,
    break_even_trade_count: 0, win_rate: 0.75, profit_factor: 2.5,
    expectancy_per_closed_trade: 12.5, realized_pnl: 50, gross_profit: 75, gross_loss: 25,
  });
  function node(tag) {
    return { tag, children: [], textContent: '', appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {},
      set innerHTML(_) { throw new Error('HTML interpolation is forbidden'); } };
  }
  const doc = { createElement: node };
  const container = { ...node('section'), ownerDocument: doc };
  Report.render(container, { phase: 'ready', runId: 'run-a', model: Report.buildModel('run-a', data), close() {}, retry() {} });
  const texts = [];
  (function scan(n) { texts.push(n.textContent); n.children.forEach(scan); })(container);
  assert.ok(texts.includes('已平仓交易'));
  assert.ok(texts.includes('75.00%'));
  assert.ok(texts.includes('+12.50'));
  assert.ok(texts.includes('+50.00'));
  assert.ok(texts.includes('2.50'));
});

test('report derives realized pnl only from numeric ledger observations', () => {
  const data = responses();
  data.result.payload.result.trades = [
    { action: 'buy', realized_pnl: null },
    { action: 'sell', realized_pnl: '18.25' },
    { action: 'sell', realized_pnl: -3.25 },
  ];
  const model = Report.buildModel('run-a', data);
  assert.equal(model.metrics.realized_pnl, 15);
  data.result.payload.result.trades[2].realized_pnl = 'unknown';
  const incomplete = Report.buildModel('run-a', data);
  assert.equal(incomplete.metrics.realized_pnl, null);
});

test('legacy and new no-sell results do not present placeholder zero statistics', () => {
  for (const status of [undefined, 'no_closed_trades']) {
    const data = responses();
    Object.assign(data.result.payload.result.metrics, {
      closed_trade_count: 0, losing_trade_count: 0, gross_loss: '0',
      win_rate: 0, expectancy_per_closed_trade: 0, profit_factor: 0,
      trade_statistics_status: status, profit_factor_status: status,
    });
    const model = Report.buildModel('run-a', data);
    assert.equal(model.metrics.win_rate, null);
    assert.equal(model.metrics.expectancy_per_closed_trade, null);
    assert.equal(model.metrics.profit_factor, null);
    assert.match(model.tradeStats.notice, /没有已平仓交易/);
  }
});

test('legacy zero profit factor with no losses is unavailable with an explanation', () => {
  for (const status of [undefined, 'no_losses']) {
    const data = responses();
    Object.assign(data.result.payload.result.metrics, {
      closed_trade_count: 4, losing_trade_count: 0, gross_loss: '0',
      win_rate: 1, expectancy_per_closed_trade: 50, profit_factor: 0,
      profit_factor_status: status,
    });
    const model = Report.buildModel('run-a', data);
    assert.equal(model.metrics.win_rate, 1);
    assert.equal(model.metrics.expectancy_per_closed_trade, 50);
    assert.equal(model.metrics.profit_factor, null);
    assert.equal(model.tradeStats.factorNotice, '无亏损样本，不能计算有限盈亏比。');
  }
});

test('renderer shows an unavailable ratio and the no-loss explanation for legacy results', () => {
  const data = responses();
  Object.assign(data.result.payload.result.metrics, {
    closed_trade_count: 1, losing_trade_count: 0, gross_loss: 0,
    win_rate: 1, expectancy_per_closed_trade: 50, profit_factor: 0,
  });
  function node(tag) {
    return { tag, children: [], textContent: '', appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; }, setAttribute() {}, addEventListener() {} };
  }
  const container = { ...node('section'), ownerDocument: { createElement: node } };
  Report.render(container, { phase: 'ready', runId: 'run-a', model: Report.buildModel('run-a', data), close() {}, retry() {} });
  const nodes = [];
  (function scan(n) { nodes.push(n); n.children.forEach(scan); })(container);
  assert.ok(nodes.some((n) => n.textContent === '无亏损样本，不能计算有限盈亏比。'));
  const pair = nodes.find((n) => n.children.some((child) => child.tag === 'dt' && child.textContent === 'Profit Factor'));
  assert.equal(pair.children.find((n) => n.tag === 'dd').textContent, '—');
});

test('new available statistics are preserved but unknown or absent evidence fails closed', () => {
  const data = responses();
  Object.assign(data.result.payload.result.metrics, {
    closed_trade_count: 3, losing_trade_count: 1, gross_loss: '30',
    win_rate: 2 / 3, expectancy_per_closed_trade: 10, profit_factor: 2,
    trade_statistics_status: 'available', profit_factor_status: 'available',
  });
  const model = Report.buildModel('run-a', data);
  assert.equal(model.metrics.profit_factor, 2);
  assert.equal(model.tradeStats.notice, null);
  assert.equal(model.tradeStats.factorNotice, null);
  data.result.payload.result.metrics.trade_statistics_status = 'unavailable';
  const unknown = Report.buildModel('run-a', data);
  assert.equal(unknown.metrics.win_rate, null);
  assert.equal(unknown.metrics.expectancy_per_closed_trade, null);
  assert.equal(unknown.metrics.profit_factor, null);
  delete data.result.payload.result.metrics.closed_trade_count;
  delete data.result.payload.result.metrics.trade_statistics_status;
  assert.equal(Report.buildModel('run-a', data).metrics.win_rate, null);
});

test('explicit unavailable or malformed realized aggregate is never replaced from ledger', () => {
  for (const value of [null, undefined, '', 'unknown', true, [], {}]) {
    const data = responses();
    data.result.payload.result.metrics.realized_pnl = value;
    data.result.payload.result.trades = [{ action: 'sell', realized_pnl: 100 }];
    assert.equal(Report.buildModel('run-a', data).metrics.realized_pnl, null);
  }
});

test('realized fallback requires a complete sell ledger and matching declared counts', () => {
  const ledgers = [
    [{ action: 'sell', realized_pnl: 100 }, { action: 'sell' }],
    [{ action: 'sell', realized_pnl: 100 }, { action: 'sell', realized_pnl: null }],
    [{ action: 'sell', realized_pnl: 100 }, { action: 'unknown', realized_pnl: 10 }],
    [{ action: 'sell', realized_pnl: 100 }, null],
    [{ action: 'buy', realized_pnl: 100 }],
  ];
  for (const trades of ledgers) {
    const data = responses();
    data.result.payload.result.trades = trades;
    assert.equal(Report.buildModel('run-a', data).metrics.realized_pnl, null);
  }
  const data = responses();
  data.result.payload.result.trades = [{ action: 'sell', realized_pnl: 100 }];
  data.result.payload.result.metrics.closed_trade_count = 2;
  assert.equal(Report.buildModel('run-a', data).metrics.realized_pnl, null);
  data.result.payload.result.metrics.closed_trade_count = 1;
  data.result.payload.result.metrics.trade_count = 2;
  assert.equal(Report.buildModel('run-a', data).metrics.realized_pnl, null);
  data.result.payload.result.metrics.trade_count = 1;
  assert.equal(Report.buildModel('run-a', data).metrics.realized_pnl, 100);
});
