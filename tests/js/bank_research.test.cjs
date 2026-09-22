const test = require('node:test');
const assert = require('node:assert/strict');
const BankResearch = require('../../quant_monitor/web/bank-research.js');

function analysis() {
  const weights = { price_to_sma_120: .15, sma_20_to_60: .15, momentum_60_5: .20, momentum_120_20: .15, low_volatility_60: .20, shallow_drawdown_60: .15 };
  const percentiles = { price_to_sma_120: .8, sma_20_to_60: .6, momentum_60_5: .7, momentum_120_20: .5, low_volatility_60: .9, shallow_drawdown_60: .4 };
  const contributions = Object.fromEntries(Object.keys(weights).map(k => [k, weights[k] * percentiles[k]]));
  return { model_version: 'bank-multifactor-v2', interval: '1d', factor_weights: weights, factor_percentiles: percentiles, factor_contributions: contributions, trend: { score: Object.values(contributions).reduce((a,b) => a+b, 0) }, score_thresholds: { entry: .8, exit: .5 }, gates: { price_above_sma_120: true, volatility_ok: false }, gate_failures: ['volatility_ok'], exit_reasons: [] };
}

test('buildModel validates complete factor math and thresholds', () => {
  const model = BankResearch.buildModel(analysis());
  assert.equal(model.available, true);
  assert.equal(model.rows.length, 6);
  assert.equal(model.scorePercent, 66.5);
  assert.equal(model.entryPercent, 80);
  assert.equal(model.exitPercent, 50);
});

test('buildModel validates an applied fundamental overlay separately from price factors', () => {
  const value = analysis();
  const price = Object.values(value.factor_contributions).reduce((a, b) => a + b, 0);
  value.price_score = price;
  value.fundamental_overlay = { enabled: true, status: 'applied', weight: .2, score: .9, contribution: .18, reason: 'fundamental_overlay_applied' };
  value.trend.score = price * .8 + .18;
  const model = BankResearch.buildModel(value);
  assert.equal(model.available, true);
  assert.equal(model.overlayEnabled, true);
  assert.equal(model.overlayScore, .9);
  assert.equal(model.overlayContribution, .18);
});

test('tampered fundamental overlay cannot make a price score look valid', () => {
  const value = analysis();
  value.price_score = .4;
  value.fundamental_overlay = { enabled: true, status: 'applied', weight: .2, score: .9, contribution: .01 };
  value.trend.score = .41;
  assert.equal(BankResearch.buildModel(value).available, false);
});

test('tampered or missing contribution is unavailable instead of fabricated', () => {
  const value = analysis();
  value.factor_contributions.momentum_60_5 += .01;
  assert.equal(BankResearch.buildModel(value).available, false);
  assert.equal(BankResearch.buildModel({}).available, false);
});

test('render uses text nodes and does not interpret untrusted labels', () => {
  function node() { return { firstChild: null, children: [], appendChild(child) { this.children.push(child); }, removeChild() {}, className: '', textContent: '' }; }
  const doc = { createElement() { return node(); } };
  const container = node();
  assert.doesNotThrow(() => BankResearch.render(container, analysis(), doc));
  assert.equal(container.children.length, 1);
});

function fundamentals() {
  const keys = ['pb', 'roe', 'dividend_yield', 'npl_ratio', 'provision_coverage', 'capital_adequacy', 'cost_income_ratio'];
  return {
    symbol: '600036.SH', decision_at: '2026-09-19T10:00:00+08:00',
    values: Object.fromEntries(keys.map(key => [key, '1.25'])),
    evidence: Object.fromEntries(keys.map(key => [key, {
      observation_id: key, content_hash: 'a'.repeat(64), unit: key === 'pb' ? 'ratio' : 'percent',
      scope: 'consolidated', period_start: '2025-01-01', period_end: '2025-12-31',
      available_at: '2026-04-01T10:00:00+08:00', received_at: '2026-04-02T10:00:00+08:00',
      source: 'fixture', revision_id: 'r1',
    }])),
    score: .5, score_status: 'complete', source_conflict: false, scope_conflict: false,
    missing_metrics: [], invalid_unit_metrics: [], invalid_scope_metrics: [], incomparable_period_metrics: [],
    strategy_integration: false, score_usage: 'parallel_research_only', warnings: [],
  };
}

test('independent fundamental model preserves percent units and score meaning', () => {
  const model = BankResearch.buildFundamentalModel(fundamentals(), '600036.SH');
  assert.equal(model.available, true);
  assert.equal(model.score, 50);
  assert.equal(model.rows[0].value, '1.25 倍');
  assert.equal(model.rows[1].value, '1.25 %');
  assert.match(model.notice, /未纳入价格策略/);
});

test('fundamental model fails closed on missing, conflicting or mismatched evidence', () => {
  assert.equal(BankResearch.buildFundamentalModel(null, '600036.SH').score, null);
  assert.equal(BankResearch.buildFundamentalModel(fundamentals(), '601398.SH').score, null);
  for (const mutate of [
    value => { delete value.evidence.roe; },
    value => { value.values.pb = null; },
    value => { value.values.pb = ''; },
    value => { value.evidence.pb.unit = 'percent'; },
    value => { value.scope_conflict = true; },
    value => { value.source_conflict = true; },
    value => { value.incomparable_period_metrics = ['roe']; },
    value => { value.warnings = ['fundamental_selection_ambiguous']; },
    value => { value.score = '0.5'; },
  ]) {
    const data = fundamentals(); mutate(data);
    assert.equal(BankResearch.buildFundamentalModel(data, '600036.SH').score, null);
  }
  const partial = fundamentals();
  partial.score = null; partial.score_status = 'unavailable'; partial.values.roe = null;
  const model = BankResearch.buildFundamentalModel(partial, partial.symbol);
  assert.equal(model.rows[1].value, '—');
  assert.equal(model.rows[0].value, '1.25 倍');
});

test('fundamental panel uses text, displays evidence and clears prior selection', () => {
  function node(tag) {
    return { tag, children: [], textContent: '',
      appendChild(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
      set innerHTML(_) { throw new Error('untrusted HTML'); },
    };
  }
  const doc = { createElement: node };
  const container = node('section');
  const value = fundamentals();
  value.evidence.roe.source = '<img src=x onerror=alert(1)>';
  BankResearch.renderFundamentals(container, value, value.symbol, doc);
  const texts = [];
  (function scan(n) { texts.push(n.textContent); n.children.forEach(scan); })(container);
  assert.ok(texts.some(text => text.includes('<img src=x onerror=alert(1)>')));
  assert.ok(texts.some(text => text.includes('2025-12-31')));
  assert.ok(texts.some(text => text.includes('本机接收')));
  assert.ok(texts.some(text => text.includes('未纳入价格策略')));
  BankResearch.renderFundamentals(container, null, '601398.SH', doc);
  assert.equal(container.children[0].children[0].textContent, '银行基本面 · 601398.SH · 评分不可用');
  assert.match(container.children[0].children[1].textContent, /评分不可用/);
});
