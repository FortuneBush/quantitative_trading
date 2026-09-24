const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const treemap = require(path.join(__dirname, '../../quant_monitor/web/treemap.js'));
const appSource = fs.readFileSync(path.join(__dirname, '../../quant_monitor/web/app.js'), 'utf8');
const treemapSource = fs.readFileSync(path.join(__dirname, '../../quant_monitor/web/treemap.js'), 'utf8');

function overlap(a, b) {
  const x = Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x));
  const y = Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));
  return x * y > 1e-8;
}

test('normalizes industry/stock aliases, deduplicates symbols and exposes stable display fields', () => {
  const normalized = treemap.normalizePayload({
    as_of: '2026-09-18T15:00:00+08:00',
    source: 'asharehub',
    sectors: [{
      name: '银行',
      items: [
        { symbol: '600000.SH', name: '甲银行', change_pct: '1.2', turnover: '100' },
        { symbol: '600000.SH', name: '甲银行旧值', change_pct: '99', turnover: '1' },
      ],
    }],
  });

  assert.equal(normalized.asOf, '2026-09-18T15:00:00+08:00');
  assert.equal(normalized.source, 'asharehub');
  assert.equal(normalized.sectors.length, 1);
  assert.equal(normalized.sectors[0].name, '银行');
  assert.equal(normalized.sectors[0].items.length, 1);
  assert.equal(normalized.sectors[0].items[0].symbol, '600000.SH');
  assert.equal(normalized.sectors[0].items[0].changePct, 99);
  assert.equal(normalized.sectors[0].items[0].amount, 1);
  assert.equal(normalized.total, 1);
});

test('invalid/missing numeric values remain null and never become NaN', () => {
  const normalized = treemap.normalizePayload({
    items: [
      { symbol: '600000.SH', name: '甲', industry: '银行', change_pct: 'bad', price: 'NaN', amount: null, size: '-1' },
      { symbol: '', name: 'dropped' },
    ],
  });
  const item = normalized.sectors[0].items[0];
  assert.equal(item.changePct, null);
  assert.equal(item.price, null);
  assert.equal(item.amount, null);
  assert.equal(item.size, 1);
  assert.equal(normalized.total, 1);
});

test('squarified layout is bounded, positive and non-overlapping', () => {
  const nodes = [
    { id: 'a', size: 10 },
    { id: 'b', size: 7 },
    { id: 'c', size: 3 },
    { id: 'd', size: 1 },
  ];
  const width = 640;
  const height = 360;
  const rects = treemap.layoutTreemap(nodes, width, height);
  assert.equal(rects.length, nodes.length);
  rects.forEach((rect) => {
    assert.ok(rect.width > 0);
    assert.ok(rect.height > 0);
    assert.ok(rect.x >= -1e-8 && rect.y >= -1e-8);
    assert.ok(rect.x + rect.width <= width + 1e-8);
    assert.ok(rect.y + rect.height <= height + 1e-8);
  });
  for (let i = 0; i < rects.length; i += 1) {
    for (let j = i + 1; j < rects.length; j += 1) {
      assert.equal(overlap(rects[i], rects[j]), false);
    }
  }
});

test('empty payload and zero viewport are safe no-op contracts', () => {
  assert.deepEqual(treemap.normalizePayload(null).sectors, []);
  assert.deepEqual(treemap.layoutTreemap([], 800, 400), []);
  assert.deepEqual(treemap.layoutTreemap([{ id: 'x', size: 1 }], 0, 400), []);
  assert.deepEqual(treemap.layoutTreemap([{ id: 'x', size: 1 }], 800, 0), []);
});

test('dashboard fetches and renders the cache-only treemap endpoint', () => {
  assert.match(appSource, /market\/treemap/);
  assert.match(appSource, /quantMonitorTreemap/);
  assert.match(appSource, /renderSectorTreemap/);
  assert.match(appSource, /页面不会为绘图单独请求上游接口/);
});

test('electronic-cloud contract exposes local live-update and sector-selection hooks', () => {
  assert.match(appSource, /updateTreemapQuote/);
  assert.match(appSource, /sectorTreemapLive/);
  assert.match(appSource, /sqlite_cache\+local_websocket/);
  assert.match(treemapSource, /onSelectSector/);
  assert.match(treemapSource, /data-freshness/);
  const normalized = treemap.normalizePayload({
    visualization: 'electronic_cloud',
    served_from: 'sqlite_cache',
    universe_count: 2,
    priced_count: 1,
    sectors: [{
      industry: '银行',
      children: [{ symbol: '600000.SH', name: '甲银行', timestamp: '2026-09-18T15:00:00+08:00', freshness: 'fresh', change_pct: 1, amount: 10 }],
    }],
  });
  assert.equal(normalized.servedFrom, 'sqlite_cache');
  assert.equal(normalized.universeCount, 2);
  assert.equal(normalized.pricedCount, 1);
  assert.equal(normalized.sectors[0].items[0].freshness, 'fresh');
});
