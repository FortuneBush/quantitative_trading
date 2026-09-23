/*
 * Local security-master browser.
 *
 * The first request is deliberately cache-only.  ``refresh=true`` is used
 * only when the operator clicks the refresh button because an AShareHub
 * catalogue refresh can consume one of the daily upstream requests.
 */
(function catalogueModule(global) {
  "use strict";

  const API_PATH = "/api/v1/instruments";
  const FETCH_TIMEOUT_MS = 8000;
  const TYPE_LABELS = {
    stock: "股票", etf: "ETF", fund: "基金", index: "指数", bond: "债券", other: "其他",
  };
  const EXCHANGE_LABELS = { SH: "沪市", SZ: "深市", BJ: "北交所" };

  function firstDefined(object, keys, fallback = null) {
    if (!object || typeof object !== "object") return fallback;
    for (const key of keys) {
      if (object[key] !== undefined && object[key] !== null) return object[key];
    }
    return fallback;
  }

  function text(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    return String(value).trim() || fallback;
  }

  function listFrom(payload) {
    if (Array.isArray(payload)) return payload;
    if (!payload || typeof payload !== "object") return [];
    for (const key of ["items", "instruments", "records", "results", "list"]) {
      if (Array.isArray(payload[key])) return payload[key];
    }
    if (payload.data !== undefined && payload.data !== payload) return listFrom(payload.data);
    return [];
  }

  function normalizeExchange(value, symbol = "") {
    const raw = text(value || symbol.split(".").pop()).toUpperCase();
    return { SSE: "SH", SH: "SH", SZSE: "SZ", SZ: "SZ", BSE: "BJ", BJ: "BJ", BE: "BJ" }[raw] || raw || "—";
  }

  function normalizeType(value) {
    const raw = text(value, "stock").toLowerCase();
    if (["equity", "share", "shares"].includes(raw)) return "stock";
    if (["etf", "exchange_traded_fund"].includes(raw)) return "etf";
    if (["fund", "lof", "reits"].includes(raw)) return "fund";
    if (["index", "index_value"].includes(raw)) return "index";
    if (["bond", "convertible_bond"].includes(raw)) return "bond";
    return TYPE_LABELS[raw] ? raw : "other";
  }

  function normalizeActive(source) {
    const explicit = firstDefined(source, ["active", "is_active"]);
    if (explicit !== null) return explicit === true || explicit === 1 || ["1", "true", "active", "listed", "L"].includes(String(explicit).toLowerCase());
    const status = text(firstDefined(source, ["list_status", "status", "listing_status"]), "").toLowerCase();
    if (!status) return true;
    return ["active", "listed", "normal", "正常", "l", "1", "true"].includes(status);
  }

  function normalizeItem(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const nested = firstDefined(source, ["instrument", "security"], {});
    const merged = nested && typeof nested === "object" ? { ...nested, ...source } : source;
    const symbol = text(firstDefined(merged, ["symbol", "code", "ticker"])).toUpperCase();
    const name = text(firstDefined(merged, ["name", "display_name", "security_name"]), "未命名");
    const exchange = normalizeExchange(firstDefined(merged, ["exchange", "market"]), symbol);
    const type = normalizeType(firstDefined(merged, ["security_type", "type", "instrument_type"]));
    const metadata = firstDefined(merged, ["metadata", "meta"], {});
    const listedAt = text(firstDefined(merged, ["listed_at", "listing_date", "list_date"]), "—");
    const pinyin = text(firstDefined(merged, ["pinyin", "pinyin_abbr", "name_pinyin"]), text(metadata && typeof metadata === "object" ? firstDefined(metadata, ["pinyin", "pinyin_abbr"]) : ""));
    return {
      symbol, name, exchange, type, listedAt, active: normalizeActive(merged),
      searchText: `${symbol} ${name} ${pinyin}`.toLocaleLowerCase("zh-CN"),
    };
  }

  function normalizePayload(payload) {
    const object = payload && typeof payload === "object" ? payload : {};
    const items = listFrom(payload).map(normalizeItem).filter((item) => item.symbol);
    const source = firstDefined(object, ["provider", "source", "data_source"], "未知来源");
    const sourceName = source && typeof source === "object" ? firstDefined(source, ["name", "source"], "未知来源") : source;
    const coverage = firstDefined(object, ["coverage", "catalogue_coverage"], {});
    const total = Number(firstDefined(object, ["total", "count", "universe_total"], firstDefined(coverage, ["total", "configured"], items.length)));
    return {
      items,
      source: text(sourceName, "未知来源"),
      servedFrom: text(firstDefined(object, ["served_from", "cache", "source_mode"]), "sqlite_cache"),
      scope: text(firstDefined(object, ["universe_scope", "scope"], "provider_catalogue"), "provider_catalogue"),
      asOf: firstDefined(object, ["as_of", "updated_at", "catalogue_updated_at", "data_time"]),
      total: Number.isFinite(total) ? total : items.length,
      coverage,
    };
  }

  function filterItems(items, state) {
    const query = text(state.query).toLocaleLowerCase("zh-CN");
    return items.filter((item) => (!query || item.searchText.includes(query))
      && (state.exchange === "all" || item.exchange === state.exchange)
      && (state.type === "all" || item.type === state.type)
      && (state.status === "all" || (state.status === "active" ? item.active : !item.active)));
  }

  function pageSlice(items, page, pageSize) {
    const size = Math.max(1, Number(pageSize) || 50);
    const totalPages = Math.max(1, Math.ceil(items.length / size));
    const current = Math.min(Math.max(1, Number(page) || 1), totalPages);
    return { rows: items.slice((current - 1) * size, current * size), page: current, pageSize: size, totalPages };
  }

  function safeDate(value) {
    if (!value) return "目录时间未提供";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return text(value, "目录时间未提供");
    return date.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function scopeLabel(value) {
    return ({ provider_catalogue: "供应商目录", configured_symbols: "配置标的", cached_catalogue: "本地目录" }[text(value)] || text(value, "目录范围未说明"));
  }

  function apiError(response) {
    const error = new Error(`目录请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    return error;
  }

  async function fetchCataloguePage(refresh = false, offset = 0) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timer = controller ? global.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS) : null;
    try {
      const params = new URLSearchParams({ limit: "5000", offset: String(Math.max(0, offset)) });
      if (refresh) params.set("refresh", "true");
      const url = `${API_PATH}?${params.toString()}`;
      const response = await global.fetch(url, controller ? { signal: controller.signal } : undefined);
      if (!response.ok) throw apiError(response);
      return await response.json();
    } finally {
      if (timer) global.clearTimeout(timer);
    }
  }

  async function fetchCatalogue(refresh = false) {
    const firstPage = await fetchCataloguePage(refresh, 0);
    const firstItems = listFrom(firstPage);
    const expectedTotal = Number(firstPage && firstPage.total);
    const items = firstItems.slice();
    // AShareHub's reference catalogue can exceed the API page cap. Follow-up
    // pages are cache-only, even after the operator explicitly refreshes the
    // first page, so one button click still consumes at most one upstream call.
    let offset = items.length;
    let guard = 0;
    while (Number.isFinite(expectedTotal) && expectedTotal > offset && firstItems.length && guard < 100) {
      const nextPage = await fetchCataloguePage(false, offset);
      const nextItems = listFrom(nextPage);
      if (!nextItems.length) break;
      items.push(...nextItems);
      const nextOffset = items.length;
      if (nextOffset <= offset) break;
      offset = nextOffset;
      guard += 1;
    }
    return normalizePayload({ ...firstPage, items, total: expectedTotal || items.length });
  }

  function init(documentRef = global.document) {
    if (!documentRef) return null;
    const byId = (id) => documentRef.getElementById(id);
    const dom = {
      panel: byId("catalogue-panel"), body: byId("catalogue-body"), source: byId("catalogue-source"), count: byId("catalogue-count"),
      note: byId("catalogue-note"), coverage: byId("catalogue-coverage"), search: byId("catalogue-search"), exchange: byId("catalogue-exchange"),
      type: byId("catalogue-type"), status: byId("catalogue-status"), refresh: byId("catalogue-refresh"), prev: byId("catalogue-prev"),
      next: byId("catalogue-next"), page: byId("catalogue-page"), pageSize: byId("catalogue-page-size"),
    };
    if (!dom.body || !dom.search) return null;
    const state = { allItems: [], filtered: [], payload: null, query: "", exchange: "all", type: "all", status: "all", page: 1, pageSize: Number(dom.pageSize?.value) || 50, loading: false };

    const setSource = (label, status = "neutral") => {
      if (!dom.source) return;
      const span = dom.source.querySelector("span");
      if (span) span.textContent = label;
      dom.source.className = `status-pill ${status}`;
    };
    const draw = () => {
      state.filtered = filterItems(state.allItems, state);
      const pageData = pageSlice(state.filtered, state.page, state.pageSize);
      state.page = pageData.page;
      dom.body.replaceChildren();
      if (!pageData.rows.length) {
        const row = documentRef.createElement("tr");
        const cell = documentRef.createElement("td");
        cell.colSpan = 5; cell.className = "catalogue-empty"; cell.textContent = state.allItems.length ? "没有符合条件的证券" : "本地目录为空，请显式刷新目录";
        row.append(cell); dom.body.append(row);
      } else {
        pageData.rows.forEach((item) => {
          const row = documentRef.createElement("tr"); row.dataset.symbol = item.symbol; row.tabIndex = 0; row.setAttribute("role", "button"); row.setAttribute("aria-label", `查看 ${item.name} ${item.symbol}`);
          const identity = documentRef.createElement("td"); const identityWrap = documentRef.createElement("div"); identityWrap.className = "catalogue-symbol";
          const name = documentRef.createElement("strong"); name.textContent = item.name; const symbol = documentRef.createElement("small"); symbol.textContent = item.symbol; identityWrap.append(name, symbol); identity.append(identityWrap);
          const exchange = documentRef.createElement("td"); exchange.textContent = `${EXCHANGE_LABELS[item.exchange] || item.exchange} · ${item.exchange}`;
          const type = documentRef.createElement("td"); const typeTag = documentRef.createElement("span"); typeTag.className = "catalogue-type"; typeTag.textContent = TYPE_LABELS[item.type] || item.type; type.append(typeTag);
          const listed = documentRef.createElement("td"); listed.textContent = item.listedAt;
          const status = documentRef.createElement("td"); const statusTag = documentRef.createElement("span"); statusTag.className = `catalogue-status ${item.active ? "active" : "inactive"}`; statusTag.textContent = item.active ? "正常上市" : "非活跃 / 已退市"; status.append(statusTag);
          row.append(identity, exchange, type, listed, status);
          const select = () => global.dispatchEvent(new global.CustomEvent("quant-monitor:select-symbol", { detail: { symbol: item.symbol, name: item.name } }));
          row.addEventListener("click", select); row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
          dom.body.append(row);
        });
      }
      if (dom.count) dom.count.textContent = `${state.filtered.length.toLocaleString("zh-CN")} / ${state.payload ? state.payload.total.toLocaleString("zh-CN") : state.allItems.length.toLocaleString("zh-CN")} 只`;
      if (dom.page) dom.page.textContent = `第 ${pageData.page} / ${pageData.totalPages} 页`;
      if (dom.prev) dom.prev.disabled = pageData.page <= 1;
      if (dom.next) dom.next.disabled = pageData.page >= pageData.totalPages;
      if (dom.coverage) {
        const active = state.allItems.filter((item) => item.active).length;
        dom.coverage.textContent = state.allItems.length ? `本地目录 ${state.allItems.length.toLocaleString("zh-CN")} 只 · 正常上市 ${active.toLocaleString("zh-CN")} 只` : "覆盖率 —";
      }
    };
    const load = async (refresh = false) => {
      if (state.loading) return;
      state.loading = true; if (dom.refresh) dom.refresh.disabled = true; setSource(refresh ? "正在刷新" : "读取缓存", "neutral");
      try {
        const payload = await fetchCatalogue(refresh); state.payload = payload; state.allItems = payload.items; state.page = 1;
        const served = payload.servedFrom === "provider_refresh" ? "上游目录" : "本地缓存";
        setSource(`${payload.source} · ${served}`, payload.items.length ? "ok" : "warning");
        if (dom.note) dom.note.textContent = `${served} · 范围：${scopeLabel(payload.scope)}；数据时间：${safeDate(payload.asOf)}；本地检索不会消耗 API 额度。`;
        draw();
      } catch (error) {
        setSource("目录读取失败", "bad");
        if (dom.note) dom.note.textContent = `${error.name === "AbortError" ? "目录请求超时" : error.message || "目录请求失败"}；请确认本地服务已启动。`;
        if (!state.allItems.length) draw();
      } finally { state.loading = false; if (dom.refresh) dom.refresh.disabled = false; }
    };
    const resetPage = () => { state.page = 1; draw(); };
    dom.search.addEventListener("input", () => { state.query = dom.search.value; resetPage(); });
    dom.exchange?.addEventListener("change", () => { state.exchange = dom.exchange.value; resetPage(); });
    dom.type?.addEventListener("change", () => { state.type = dom.type.value; resetPage(); });
    dom.status?.addEventListener("change", () => { state.status = dom.status.value; resetPage(); });
    dom.pageSize?.addEventListener("change", () => { state.pageSize = Number(dom.pageSize.value) || 50; resetPage(); });
    dom.prev?.addEventListener("click", () => { state.page -= 1; draw(); });
    dom.next?.addEventListener("click", () => { state.page += 1; draw(); });
    dom.refresh?.addEventListener("click", () => load(true));
    load(false);
    return { state, dom, load, draw };
  }

  global.QuantMonitorCatalogue = { normalizeItem, normalizePayload, filterItems, pageSlice, safeDate, scopeLabel, fetchCatalogue, init };
  if (global.document && global.document.readyState === "loading") global.document.addEventListener("DOMContentLoaded", () => init());
  else init();
}(window));
