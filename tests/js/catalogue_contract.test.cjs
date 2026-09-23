const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../quant_monitor/web/catalogue.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../../quant_monitor/web/index.html'), 'utf8');

function api(fetchImpl) {
  const context = {
    URLSearchParams,
    window: {
      setTimeout,
      clearTimeout,
      fetch: fetchImpl || (async () => ({ ok: true, json: async () => ({ items: [] }) })),
    },
  };
  vm.runInNewContext(source, context);
  return context.window.QuantMonitorCatalogue;
}

test('catalogue panel exposes cache-first controls and explicit refresh', () => {
  assert.match(html, /id="catalogue-panel"/);
  assert.match(html, /id="catalogue-search"/);
  assert.match(html, /id="catalogue-exchange"/);
  assert.match(html, /id="catalogue-type"/);
  assert.match(html, /id="catalogue-status"/);
  assert.match(html, /id="catalogue-refresh"/);
  assert.match(html, /id="catalogue-prev"/);
  assert.match(html, /id="catalogue-next"/);
  assert.match(source, /const API_PATH = "\/api\/v1\/instruments"/);
  assert.match(source, /load\(false\)/);
  assert.match(source, /refresh=true/);
});

test('catalogue normalizes provider security-master records', () => {
  const catalogue = api();
  assert.deepEqual(JSON.parse(JSON.stringify(catalogue.normalizeItem({
    symbol: '600519.SH', name: '贵州茅台', exchange: 'SSE', security_type: 'equity', list_status: 'L',
    listed_at: '2001-08-27', metadata: { pinyin_abbr: 'GZMT' },
  }))), {
    symbol: '600519.SH', name: '贵州茅台', exchange: 'SH', type: 'stock', listedAt: '2001-08-27', active: true,
    searchText: '600519.sh 贵州茅台 gzmt',
  });
});

test('catalogue filters by search, exchange, type and listing status', () => {
  const catalogue = api();
  const rows = [
    catalogue.normalizeItem({ symbol: '600519.SH', name: '贵州茅台', exchange: 'SH', security_type: 'stock', active: true }),
    catalogue.normalizeItem({ symbol: '000001.SZ', name: '平安银行', exchange: 'SZ', security_type: 'stock', active: true }),
    catalogue.normalizeItem({ symbol: '510300.SH', name: '沪深300ETF', exchange: 'SH', security_type: 'etf', active: false }),
  ];
  assert.deepEqual(catalogue.filterItems(rows, { query: '银行', exchange: 'all', type: 'all', status: 'all' }).map((row) => row.symbol), ['000001.SZ']);
  assert.deepEqual(catalogue.filterItems(rows, { query: '', exchange: 'SH', type: 'stock', status: 'active' }).map((row) => row.symbol), ['600519.SH']);
  assert.deepEqual(catalogue.filterItems(rows, { query: '', exchange: 'SH', type: 'all', status: 'inactive' }).map((row) => row.symbol), ['510300.SH']);
});

test('catalogue pagination is bounded and reports total pages', () => {
  const catalogue = api();
  const rows = Array.from({ length: 51 }, (_, index) => ({ symbol: String(index).padStart(6, '0') }));
  assert.deepEqual(JSON.parse(JSON.stringify(catalogue.pageSlice(rows, 1, 50))), { rows: rows.slice(0, 50), page: 1, pageSize: 50, totalPages: 2 });
  assert.deepEqual(JSON.parse(JSON.stringify(catalogue.pageSlice(rows, 99, 50))), { rows: [rows[50]], page: 2, pageSize: 50, totalPages: 2 });
});

test('catalogue payload keeps source, cache mode and as-of metadata', () => {
  const catalogue = api();
  const result = catalogue.normalizePayload({ provider: 'asharehub', served_from: 'sqlite_cache', as_of: '2026-09-23T09:30:00+08:00', total: 2, items: [{ symbol: '600519.SH', name: '贵州茅台' }, { symbol: '000001.SZ', name: '平安银行' }] });
  assert.equal(result.source, 'asharehub');
  assert.equal(result.servedFrom, 'sqlite_cache');
  assert.equal(result.asOf, '2026-09-23T09:30:00+08:00');
  assert.equal(result.total, 2);
  assert.equal(result.items.length, 2);
  assert.equal(catalogue.scopeLabel(result.scope), '供应商目录');
});

test('catalogue fetch fills API pages from the local cache without refreshing follow-ups', async () => {
  const calls = [];
  const catalogue = api(async (url) => {
    calls.push(url);
    const offset = Number((url.match(/offset=(\d+)/) || [])[1] || 0);
    if (offset === 0) {
      return { ok: true, json: async () => ({ provider: 'mock', total: 3, items: [{ symbol: '000001.SZ' }, { symbol: '000002.SZ' }] }) };
    }
    return { ok: true, json: async () => ({ provider: 'mock', total: 3, items: [{ symbol: '000003.SZ' }] }) };
  });
  const result = await catalogue.fetchCatalogue(false);
  assert.equal(result.items.length, 3);
  assert.equal(calls.length, 2);
  assert.match(calls[0], /limit=5000/);
  assert.match(calls[1], /offset=2/);
  assert.doesNotMatch(calls[1], /refresh=true/);
});
