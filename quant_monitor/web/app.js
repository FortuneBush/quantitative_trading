// Pure, side-effect-free readiness normalization keeps the dashboard's
// fail-closed research semantics testable without booting the DOM runtime.
function quantMonitorAnalysisReadiness(quality, provenance) {
  const q = quality && typeof quality === "object" ? quality : {};
  const p = provenance && typeof provenance === "object" ? provenance : {};
  const analysisReady = q.analysis_ready === true;
  const decisionReady = q.decision_ready === true;
  const failuresValid = Array.isArray(q.analysis_readiness_failures);
  const failures = failuresValid ? q.analysis_readiness_failures : [];
  const analysisUsable = analysisReady && failuresValid && failures.length === 0;
  const calculationInput = String(p.calculation_input || "unknown");
  const inputLabel = calculationInput === "immutable_observations" ? "计算输入：不可变记录"
    : calculationInput === "legacy_bars_projection" ? "计算输入：兼容缓存投影" : "计算输入：来源未说明";
  return { analysisReady, decisionReady, analysisUsable, researchReady: analysisUsable && decisionReady, failures, calculationInput, inputLabel };
}

// A green HTTP/provider status is not enough to establish that a market
// snapshot is usable.  Keep a compact aggregate that the dashboard can show
// next to the breadth/ranking panels, so an operator does not have to inspect
// every row to discover that the entire view is stale or untimestamped.
function quantMonitorFreshnessSummary(states) {
  const rows = Array.isArray(states) ? states : [];
  const problematic = rows.filter((state) => ["stale", "future_timestamp", "missing", "unknown"].includes(String(state)));
  const counts = problematic.reduce((result, state) => {
    const key = String(state);
    result[key] = (result[key] || 0) + 1;
    return result;
  }, {});
  return {
    total: rows.length,
    problemCount: problematic.length,
    status: rows.length === 0 ? "empty" : problematic.length ? "warning" : "ok",
    counts,
  };
}

(() => {
  "use strict";

  const API_BASE = "/api/v1";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const FETCH_TIMEOUT = 8000;
  const POLL_INTERVAL = 30000;
  const MAX_EVENTS = 30;

  const state = {
    watchlist: [],
    bankMode: false,
    bankPayload: null,
    bankItems: [],
    bankSearch: "",
    bankExchange: "all",
    bankSort: "change_desc",
    marketQuotes: [],
    marketSummary: null,
    sectorTreemapPayload: null,
    treemapRenderTimer: null,
    analysis: null,
    selectedSymbol: null,
    period: "1d",
    bars: [],
    ws: null,
    wsRetry: 0,
    wsTimer: null,
    pollTimer: null,
    lastApiSuccess: null,
    marketLoading: false,
    marketStatus: null,
    configuredWatchlist: [],
    lastSystem: null,
    latestMarketTime: null,
    chartProvenance: null,
    requestSerial: 0,
    // A visibility/online event can fire while the initial refresh is still
    // in flight.  Reusing the same promise prevents duplicate requests and,
    // more importantly, avoids older responses overwriting a newer view.
    refreshInFlight: null,
    researchRuns: [],
    researchSelected: new Set(),
    collectionIdempotencyKey: null,
  };

  const el = (id) => document.getElementById(id);
  const dom = {
    overview: el("overview-cards"),
    marketTitle: el("market-insights-title"),
    marketSource: el("market-source"),
    marketTotal: el("market-total"),
    marketBreadth: el("market-breadth"),
    marketGainers: el("market-gainers"),
    marketLosers: el("market-losers"),
    marketTurnover: el("market-turnover"),
    marketResearch: el("market-research"),
    marketInsightsNote: el("market-insights-note"),
    sectorTreemap: el("sector-treemap"),
    sectorTreemapSource: el("sector-treemap-source"),
    sectorTreemapAsOf: el("sector-treemap-asof"),
    sectorTreemapLive: el("sector-treemap-live"),
    sectorTreemapCoverage: el("sector-treemap-coverage"),
    sectorTreemapNote: el("sector-treemap-note"),
    bankQuotaBadge: el("bank-quota-badge"),
    analysisName: el("analysis-name"),
    analysisSymbol: el("analysis-symbol"),
    analysisStatus: el("analysis-status"),
    analysisVerdict: el("analysis-verdict-value"),
    analysisVerdictNote: el("analysis-verdict-note"),
    analysisScoreLabel: el("analysis-score-label"),
    analysisScore: el("analysis-score-value"),
    analysisRisk: el("analysis-risk-value"),
    analysisRiskNote: el("analysis-risk-note"),
    analysisQualityStatus: el("analysis-quality-status"),
    analysisQualityCoverage: el("analysis-quality-coverage"),
    analysisQualityAsOf: el("analysis-quality-asof"),
    analysisEntryGate: el("analysis-entry-gate"),
    analysisMetrics: el("analysis-metrics"),
    analysisNote: el("analysis-note"),
    analysisEvidence: el("analysis-evidence"),
    analysisRisks: el("analysis-risks"),
    analysisInvalidations: el("analysis-invalidations"),
    analysisFactorBreakdown: el("analysis-factor-breakdown"),
    bankFundamentals: el("bank-fundamentals"),
    watchlistBody: el("watchlist-body"),
    watchlistCount: el("watchlist-count"),
    bankToolbar: el("bank-toolbar"),
    bankSearch: el("bank-search"),
    bankExchange: el("bank-exchange"),
    bankSort: el("bank-sort"),
    alertsList: el("alerts-list"),
    alertCount: el("alert-count"),
    signalsList: el("signals-list"),
    signalCount: el("signal-count"),
    priceChart: el("price-chart"),
    volumeChart: el("volume-chart"),
    chartWrap: el("chart-wrap"),
    chartEmpty: el("chart-empty"),
    chartTooltip: el("chart-tooltip"),
    selectedName: el("selected-name"),
    selectedSymbol: el("selected-symbol"),
    selectedPrice: el("selected-price"),
    selectedChange: el("selected-change"),
    chartProvenance: el("chart-provenance"),
    apiStatus: el("api-status"),
    wsStatus: el("ws-status"),
    providerStatus: el("provider-status"),
    healthBadge: el("health-badge"),
    readinessBanner: el("readiness-banner"),
    readinessIcon: el("readiness-icon"),
    readinessTitle: el("readiness-title"),
    readinessBadge: el("readiness-badge"),
    readinessSummary: el("readiness-summary"),
    readinessDetail: el("readiness-detail"),
    readinessProgress: el("readiness-progress"),
    readinessProgressBar: el("readiness-progress-bar"),
    readinessPercent: el("readiness-percent"),
    systemMetrics: el("system-metrics"),
    heartbeatDot: el("heartbeat-dot"),
    heartbeatText: el("heartbeat-text"),
    lastUpdated: el("last-updated"),
    marketBadge: el("market-badge"),
    clock: el("clock"),
    refresh: el("refresh-all"),
    collectNow: el("collect-now"),
    researchRunsCount: el("research-runs-count"),
    researchRunsNote: el("research-runs-note"),
    researchRunsList: el("research-runs-list"),
    researchCompare: el("research-compare"),
    researchCompareResult: el("research-compare-result"),
    researchReport: el("research-report"),
    settings: el("settings-dialog"),
    toastRegion: el("toast-region"),
  };

  const reportViewer = window.ResearchReport && dom.researchReport
    ? window.ResearchReport.createController({
      fetcher: (url, options) => window.fetch(url, options),
      onUpdate: (reportState) => window.ResearchReport.render(dom.researchReport, reportState),
    }) : null;

  function firstDefined(object, keys, fallback = null) {
    if (!object || typeof object !== "object") return fallback;
    for (const key of keys) {
      if (object[key] !== undefined && object[key] !== null) return object[key];
    }
    return fallback;
  }

  function finiteNumber(value) {
    if (value === "" || value === null || value === undefined) return null;
    // JSON booleans/objects are not numeric market data.  Number(true) and
    // Number([]) would otherwise silently become 1/0 and contaminate charts,
    // breadth and indicator labels.
    if (typeof value === "boolean" || (typeof value === "object" && !Array.isArray(value))) return null;
    const number = typeof value === "string" ? Number(value.replace(/,/g, "")) : Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function listFrom(payload, keys = []) {
    if (Array.isArray(payload)) return payload;
    if (!payload || typeof payload !== "object") return [];
    for (const key of [...keys, "items", "list", "results", "records"]) {
      if (Array.isArray(payload[key])) return payload[key];
    }
    if (payload.data !== undefined) {
      if (Array.isArray(payload.data)) return payload.data;
      if (payload.data && payload.data !== payload) return listFrom(payload.data, keys);
    }
    return [];
  }

  function objectFrom(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
    if (payload.data && typeof payload.data === "object" && !Array.isArray(payload.data)) return payload.data;
    return payload;
  }

  function formatPrice(value, digits = 2) {
    const number = finiteNumber(value);
    return number === null ? "—" : number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  function formatPercent(value, signed = true) {
    const number = finiteNumber(value);
    if (number === null) return "—";
    return `${signed && number > 0 ? "+" : ""}${number.toFixed(2)}%`;
  }

  function formatCompact(value) {
    const number = finiteNumber(value);
    if (number === null) return "—";
    const abs = Math.abs(number);
    if (abs >= 1e12) return `${(number / 1e12).toFixed(2)}万亿`;
    if (abs >= 1e8) return `${(number / 1e8).toFixed(2)}亿`;
    if (abs >= 1e4) return `${(number / 1e4).toFixed(2)}万`;
    return number.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
  }

  function formatDuration(seconds) {
    const value = finiteNumber(seconds);
    if (value === null) return "—";
    const days = Math.floor(value / 86400);
    const hours = Math.floor((value % 86400) / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    if (days) return `${days}天 ${hours}小时`;
    if (hours) return `${hours}小时 ${minutes}分`;
    return `${minutes}分钟`;
  }

  function directionClass(change) {
    const value = finiteNumber(change);
    return value === null || value === 0 ? "flat" : value > 0 ? "up" : "down";
  }

  function toDate(value) {
    if (value === undefined || value === null || value === "") return null;
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
    if (typeof value === "number" || /^\d{10,13}$/.test(String(value))) {
      const raw = Number(value);
      const date = new Date(raw < 1e12 ? raw * 1000 : raw);
      return Number.isNaN(date.getTime()) ? null : date;
    }
    const date = new Date(String(value).replace(" ", "T"));
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatTime(value, includeDate = false) {
    const date = toDate(value);
    if (!date) return value ? String(value).slice(0, 16) : "—";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      month: includeDate ? "2-digit" : undefined,
      day: includeDate ? "2-digit" : undefined,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function setPill(node, text, status = "neutral") {
    if (!node) return;
    node.className = `status-pill ${status}`;
    const label = node.querySelector("span");
    if (label) label.textContent = text;
  }

  function setQualityChip(node, text, status = "neutral") {
    if (!node) return;
    node.className = `quality-chip ${status}`;
    node.textContent = text;
  }

  function showToast(message, type = "info") {
    if (!dom.toastRegion) return;
    const toast = document.createElement("div");
    toast.className = `toast ${type === "error" ? "error" : ""}`;
    toast.textContent = message;
    dom.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  // Keep the server's stable error contract intact for the dashboard.  A bare
  // `HTTP 503` loses the provider/quota reason and makes an operator retry
  // blindly; this error remains text-only so vendor messages cannot become
  // executable markup.
  class ApiRequestError extends Error {
    constructor(status, payload = null, retryAfter = null) {
      const envelope = payload && typeof payload === "object" && payload.error && typeof payload.error === "object"
        ? payload.error : {};
      const code = typeof envelope.code === "string" ? envelope.code : "http_error";
      const message = typeof envelope.message === "string" && envelope.message.trim()
        ? envelope.message.trim().slice(0, 256) : `HTTP ${status}`;
      super(`HTTP ${status}: ${message}`);
      this.name = "ApiRequestError";
      this.status = Number(status);
      this.code = code;
      this.detail = message;
      this.requestId = typeof envelope.request_id === "string" ? envelope.request_id : null;
      this.retryAfter = retryAfter;
      this.details = envelope.details ?? null;
    }
  }

  function requestErrorMessage(error, fallback = "请求失败，请稍后重试") {
    if (!error) return fallback;
    if (error.name === "AbortError") return "请求超时，请稍后重试";
    const detail = error instanceof ApiRequestError && error.detail ? error.detail : "";
    const retry = error instanceof ApiRequestError && error.retryAfter
      ? `（建议 ${error.retryAfter} 秒后重试）` : "";
    return `${detail || fallback}${retry}`.slice(0, 280);
  }

  async function fetchJSON(path, options = {}) {
    const { timeout: requestTimeout = FETCH_TIMEOUT, ...requestOptions } = options;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), requestTimeout);
    try {
      const response = await fetch(path, {
        headers: { Accept: "application/json", ...(requestOptions.headers || {}) },
        cache: "no-store",
        ...requestOptions,
        signal: controller.signal,
      });
      let payload = null;
      if (!response.ok) {
        // Error envelopes are small and bounded by the server, but malformed
        // or non-JSON upstream responses must not hide the HTTP status.
        try { payload = await response.json(); } catch { payload = null; }
        const rawRetryAfter = response.headers && typeof response.headers.get === "function"
          ? response.headers.get("Retry-After") : null;
        const retryAfter = rawRetryAfter !== null && /^\d{1,6}$/.test(String(rawRetryAfter).trim())
          ? Number(rawRetryAfter) : null;
        setPill(dom.apiStatus, "本地 API 返回错误", "bad");
        throw new ApiRequestError(response.status, payload, retryAfter);
      }
      payload = await response.json();
      state.lastApiSuccess = new Date();
      setPill(dom.apiStatus, "本地 API 已连接", "ok");
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function normalizeQuote(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const price = finiteNumber(firstDefined(raw, ["price", "last", "last_price", "close", "current"]));
    const preClose = finiteNumber(firstDefined(raw, ["pre_close", "prev_close", "previous_close", "yclose"]));
    let change = finiteNumber(firstDefined(raw, ["change", "chg", "price_change"]));
    let changePct = finiteNumber(firstDefined(raw, ["change_pct", "change_percent", "pct_change", "pct", "percent"]));
    if (change === null && price !== null && preClose !== null) change = price - preClose;
    if (changePct === null && price !== null && preClose) changePct = ((price - preClose) / preClose) * 100;
    return {
      symbol: String(firstDefined(raw, ["symbol", "code", "ticker", "ts_code"], "")),
      name: String(firstDefined(raw, ["name", "display_name", "stock_name", "security_name"], "未命名")),
      price,
      preClose,
      change,
      changePct,
      amount: finiteNumber(firstDefined(raw, ["amount", "turnover", "turnover_amount", "value"])),
      volume: finiteNumber(firstDefined(raw, ["volume", "vol"])),
      updatedAt: firstDefined(raw, ["updated_at", "timestamp", "time", "datetime", "ts"]),
      freshness: firstDefined(source, ["freshness", "freshness_state", "quote_freshness"], null),
      stale: firstDefined(source, ["stale"], null),
      raw,
    };
  }

  // Keep freshness visible at the row level.  A successful HTTP response can
  // still contain an old SQLite snapshot, so a green provider status alone is
  // not sufficient evidence that an individual quote is current.
  function quoteFreshness(quote) {
    const explicit = String(firstDefined(quote, ["freshness"], "")).toLowerCase();
    if (["fresh", "stale", "missing", "future_timestamp", "future"].includes(explicit)) {
      return explicit === "future" ? "future_timestamp" : explicit;
    }
    if (firstDefined(quote, ["stale"], null) === true) return "stale";
    const timestamp = toDate(firstDefined(quote, ["updatedAt", "updated_at", "timestamp"], null));
    if (!timestamp) return "unknown";
    const age = (Date.now() - timestamp.getTime()) / 1000;
    if (age < -5) return "future_timestamp";
    const configured = state.lastSystem?.provider?.quote_freshness;
    const threshold = finiteNumber(firstDefined(configured, ["threshold_seconds"], null));
    return threshold !== null && age > threshold ? "stale" : "fresh";
  }

  function renderQuoteFreshness(cell, quote) {
    if (!cell) return;
    const container = cell.querySelector(".quote-price-stack") || cell;
    let node = container.querySelector(".quote-asof");
    if (!node) {
      node = document.createElement("small");
      node.className = "quote-asof";
      node.dataset.field = "freshness";
      container.append(node);
    }
    const status = quoteFreshness(quote);
    const labels = { fresh: "新鲜", stale: "过期", missing: "缺失", future_timestamp: "时间异常", unknown: "时间未知" };
    node.className = `quote-asof ${status}`;
    node.textContent = `${labels[status] || "时间未知"} · ${formatTime(quote.updatedAt, true)}`;
    const timestamp = toDate(quote.updatedAt);
    node.title = timestamp ? `行情时间 ${timestamp.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}` : "该行情没有可验证的时间戳";
    cell.classList.toggle("quote-stale-cell", status === "stale" || status === "future_timestamp");
  }

  function freshnessNotice(items) {
    const rows = Array.isArray(items) ? items : [];
    const summary = quantMonitorFreshnessSummary(rows.map(quoteFreshness));
    if (summary.status === "empty") return "行情新鲜度待核验：暂无可验证快照";
    if (summary.status === "ok") return `行情新鲜度：${summary.total} 只均通过时间戳校验`;
    const details = [
      ["stale", "过期"],
      ["missing", "时间缺失"],
      ["future_timestamp", "时间异常"],
      ["unknown", "时间未知"],
    ].filter(([key]) => summary.counts[key]).map(([key, label]) => `${label} ${summary.counts[key]}`).join("、");
    return `行情新鲜度待核验：${summary.problemCount}/${summary.total} 只${details ? `（${details}）` : ""}`;
  }

  function refreshMarketFreshnessSummary() {
    const rows = state.bankMode ? state.bankItems : state.marketQuotes;
    const states = rows.map(quoteFreshness);
    const summary = quantMonitorFreshnessSummary(states);
    const text = freshnessNotice(rows);
    if (dom.marketSource) {
      const current = String(dom.marketSource.textContent || "").trim();
      setPill(dom.marketSource, current || "行情源", summary.status === "ok" ? "ok" : "warning");
      dom.marketSource.title = summary.status === "ok"
        ? `收到 ${summary.total} 条可验证的新鲜行情`
        : summary.status === "empty" ? "尚未收到可用行情" : `收到 ${summary.total - summary.problemCount} 条可验证行情，其中 ${summary.problemCount} 条新鲜度需核验`;
    }
    if (dom.marketInsightsNote) {
      const previous = String(dom.marketInsightsNote.textContent || "");
      const marker = /行情新鲜度(?:待核验)?：[^·]+/u;
      dom.marketInsightsNote.textContent = marker.test(previous) ? previous.replace(marker, text) : previous ? `${previous} · ${text}` : text;
      dom.marketInsightsNote.dataset.freshness = summary.status;
      dom.marketInsightsNote.setAttribute("aria-live", "polite");
    }
  }

  function refreshQuoteFreshnessIndicators() {
    if (!dom.watchlistBody) return;
    state.watchlist.forEach((quote) => {
      const row = dom.watchlistBody.querySelector(`tr[data-symbol="${CSS.escape(quote.symbol)}"]`);
      if (row) renderQuoteFreshness(row.querySelector('[data-field="price-cell"]'), quote);
    });
  }

  function unwrapBankItem(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const instrument = firstDefined(source, ["instrument", "security"], {});
    const quote = firstDefined(source, ["quote", "snapshot"], {});
    const analysis = firstDefined(source, ["analysis", "analysis_summary", "research"], null);
    const merged = {
      ...source,
      ...(quote && typeof quote === "object" ? quote : {}),
      symbol: firstDefined(source, ["symbol", "code"], firstDefined(instrument, ["symbol", "code"], firstDefined(quote, ["symbol", "code"], ""))),
      name: firstDefined(source, ["name", "display_name"], firstDefined(instrument, ["name", "display_name"], firstDefined(quote, ["name"], "未命名"))),
    };
    const normalized = normalizeQuote(merged);
    const exchangeRaw = String(firstDefined(source, ["exchange"], firstDefined(instrument, ["exchange"], normalized.symbol.split(".").pop() || ""))).toUpperCase();
    const recommendation = analysis && typeof analysis === "object" ? firstDefined(analysis, ["recommendation", "signal", "bias"], analysis) : {};
    const trend = analysis && typeof analysis === "object" ? firstDefined(analysis, ["trend", "trend_analysis"], {}) : {};
    const researchScore = finiteNumber(firstDefined(source, ["research_score", "quant_score", "score"], firstDefined(recommendation, ["score", "strength"], firstDefined(trend, ["score"], null))));
    return {
      ...normalized,
      exchange: exchangeRaw === "SSE" ? "SH" : exchangeRaw === "SZSE" ? "SZ" : exchangeRaw,
      analysis: analysis && typeof analysis === "object" ? analysis : null,
      fundamental_factors: firstDefined(source, ["fundamental_factors"], null),
      researchScore,
      researchAction: String(firstDefined(recommendation, ["action", "signal", "bias"], firstDefined(source, ["action", "research_action"], ""))).toLowerCase(),
      researchLabel: firstDefined(recommendation, ["label"], firstDefined(source, ["research_label"], null)),
      dataQuality: analysis && typeof analysis === "object" ? firstDefined(analysis, ["data_quality", "quality"], {}) : firstDefined(source, ["data_quality", "quality"], {}),
      raw: source,
    };
  }

  function researchPresentation(action, label = null) {
    const value = String(action || "").toLowerCase();
    if (["buy", "long", "positive_bias", "bullish", "positive", "买入", "看多"].includes(value)) {
      return { label: "偏强研究", className: "up" };
    }
    if (["sell", "short", "negative_bias", "bearish", "negative", "卖出", "看空"].includes(value)) {
      return { label: "偏弱研究", className: "down" };
    }
    if (["risk_caution", "caution", "warning"].includes(value)) {
      return { label: "风险观察", className: "warning" };
    }
    if (["insufficient", "insufficient_data", "unavailable"].includes(value)) {
      return { label: "数据不足", className: "neutral" };
    }
    return { label: value === "hold" || value === "neutral" ? "中性观察" : label || "中性观察", className: "flat" };
  }

  function formatResearchScore(value) {
    const score = finiteNumber(value);
    if (score === null) return null;
    const display = Math.abs(score) <= 1 ? score * 100 : score;
    return `${display > 0 ? "+" : ""}${display.toFixed(0)}`;
  }

  function asTextList(value, fallback) {
    if (Array.isArray(value)) {
      const rows = value.map((item) => {
        if (item && typeof item === "object") return firstDefined(item, ["label", "description", "reason", "message", "text"], "");
        return item;
      }).map(String).map((item) => item.trim()).filter(Boolean);
      if (rows.length) return rows;
    }
    if (typeof value === "string" && value.trim()) return value.split(/[；;\n]/).map((item) => item.trim()).filter(Boolean);
    return [fallback];
  }

  function renderDetailList(node, values, fallback) {
    if (!node) return;
    node.replaceChildren();
    asTextList(values, fallback).slice(0, 4).forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      node.append(item);
    });
  }

  function normalizeMarketSummary(raw, rows = []) {
    const source = raw && typeof raw === "object" ? raw : {};
    const changes = rows.map((item) => item.changePct).filter((value) => value !== null);
    const countBy = (predicate) => rows.filter((item) => predicate(item.changePct)).length;
    return {
      total: finiteNumber(firstDefined(source, ["total", "count", "matched", "universe", "stocks"], rows.length)),
      universeTotal: finiteNumber(firstDefined(source, ["universe_total", "catalogue_total", "universe", "total", "count"], rows.length)),
      unpriced: finiteNumber(firstDefined(source, ["unpriced", "unpriced_count"], null)),
      priced: finiteNumber(firstDefined(source, ["priced"], changes.length)),
      advancing: finiteNumber(firstDefined(source, ["advancing", "advancers", "up", "gainers", "rise_count"], countBy((value) => value > 0))),
      declining: finiteNumber(firstDefined(source, ["declining", "decliners", "down", "losers", "fall_count"], countBy((value) => value < 0))),
      unchanged: finiteNumber(firstDefined(source, ["unchanged", "flat", "unchanged_count"], countBy((value) => value === 0))),
      averageChangePct: finiteNumber(firstDefined(source, ["average_pct_change", "average_change_pct", "avg_change_pct", "mean_change_pct", "average_pct"], changes.length ? changes.reduce((a, b) => a + b, 0) / changes.length : null)),
      medianChangePct: finiteNumber(firstDefined(source, ["median_change_pct", "median_pct"], null)),
      asOf: firstDefined(source, ["as_of", "timestamp", "updated_at", "received_at"], null),
      topGainers: listFrom(firstDefined(source, ["top_gainers"], []), ["items", "quotes"]),
      topLosers: listFrom(firstDefined(source, ["top_losers"], []), ["items", "quotes"]),
    };
  }

  function renderMarketQuotes(payload) {
    const object = objectFrom(payload);
    const rows = listFrom(payload, ["items", "quotes", "stocks", "records"]).map(normalizeQuote).filter((item) => item.symbol);
    state.marketQuotes = rows;
    const summary = normalizeMarketSummary(firstDefined(object, ["summary", "breadth", "market_summary"], object), rows);
    state.marketSummary = summary;
    const receivedCount = finiteNumber(firstDefined(object, ["total"], rows.length)) ?? rows.length;
    const configuredCount = summary.universeTotal ?? summary.total ?? rows.length;
    if (dom.marketTotal) dom.marketTotal.textContent = `已收到 ${receivedCount} / 配置 ${configuredCount}`;
    const source = firstDefined(object, ["source", "provider", "data_source"], "未配置");
    const sourceName = source && typeof source === "object" ? firstDefined(source, ["name", "source"], "未配置") : source;
    // A non-empty response is not sufficient evidence that the market view is
    // current. Preserve the provider name, but visibly downgrade the source
    // badge whenever any row is stale, future-dated, missing a timestamp or
    // otherwise unverifiable. This keeps a cached close from looking like a
    // live feed while retaining the row-level freshness markers below.
    const freshnessStates = rows.map(quoteFreshness);
    const freshnessWarnings = freshnessStates.filter((value) => ["stale", "future_timestamp", "missing", "unknown"].includes(value));
    const sourceStatus = receivedCount <= 0 || !rows.length ? "warning" : freshnessWarnings.length ? "warning" : "ok";
    setPill(dom.marketSource, sourceName ? String(sourceName) : "未配置", sourceStatus);
    if (dom.marketSource) {
      dom.marketSource.title = receivedCount <= 0 || !rows.length
        ? "尚未收到可用行情"
        : freshnessWarnings.length
          ? `收到 ${receivedCount} 条行情，其中 ${freshnessWarnings.length} 条新鲜度需核验`
          : `收到 ${receivedCount} 条可验证的新鲜行情`;
    }
    const universeScope = String(firstDefined(object, ["universe_scope"], "provider_catalogue"));
    const configuredScope = universeScope === "configured_symbols";
    if (dom.marketTitle) dom.marketTitle.textContent = state.bankMode ? "银行板块概览" : configuredScope ? "自选行情范围" : "沪 A 全市场";
    if (dom.marketInsightsNote) {
      const asOf = firstDefined(object, ["as_of", "timestamp", "updated_at"], summary.asOf);
      const unpricedNote = summary.unpriced ? ` · ${summary.unpriced} 只未收到行情` : "";
      const scopeNote = state.bankMode ? "银行分类与排名来自服务端聚合缓存，本地筛选排序不消耗额度" : configuredScope ? "当前看板仅展示已配置自选标的，以节省每日 API 额度" : "当前数据源提供完整证券目录";
      const breadthNote = summary.priced === 0 ? " · 日线参考尚未加载，涨跌统计暂不可用" : "";
      const sessionNote = " · 非交易时段为上一交易日收盘快照";
      const freshnessNote = freshnessNotice(rows);
      if (dom.marketInsightsNote) {
        dom.marketInsightsNote.dataset.freshness = quantMonitorFreshnessSummary(rows.map(quoteFreshness)).status;
        dom.marketInsightsNote.dataset.freshnessText = freshnessNote;
      }
      dom.marketInsightsNote.textContent = asOf
        ? `行情时间 ${formatTime(asOf, true)}${unpricedNote} · ${scopeNote}${breadthNote}${sessionNote} · ${freshnessNote}`
        : `${scopeNote}；尚未收到行情。${breadthNote}${sessionNote} · ${freshnessNote}`;
    }
    if (dom.marketBreadth) {
      const values = [
        ["上涨", summary.priced === 0 ? null : summary.advancing, "up"],
        ["下跌", summary.priced === 0 ? null : summary.declining, "down"],
        ["平盘", summary.priced === 0 ? null : summary.unchanged, "flat"],
        [state.bankMode ? "中位涨幅" : "平均涨跌", state.bankMode && summary.medianChangePct !== null ? summary.medianChangePct : summary.averageChangePct, directionClass(state.bankMode && summary.medianChangePct !== null ? summary.medianChangePct : summary.averageChangePct)],
      ];
      [...dom.marketBreadth.children].forEach((node, index) => {
        const value = values[index];
        if (!value) return;
        const strong = node.querySelector("strong");
        if (strong) {
          strong.textContent = index === 3 ? formatPercent(value[1]) : (value[1] === null ? "—" : Number(value[1]).toLocaleString("zh-CN"));
          strong.className = value[2];
        }
      });
    }
    const suppliedGainers = summary.topGainers || [];
    const suppliedLosers = summary.topLosers || [];
    const gainers = (suppliedGainers.length ? suppliedGainers : rows.filter((item) => item.changePct !== null && item.changePct > 0).slice().sort((a, b) => b.changePct - a.changePct)).map(normalizeQuote).slice(0, 5);
    const losers = (suppliedLosers.length ? suppliedLosers : rows.filter((item) => item.changePct !== null && item.changePct < 0).slice().sort((a, b) => a.changePct - b.changePct)).map(normalizeQuote).slice(0, 5);
    renderLeaderList(dom.marketGainers, gainers, "up");
    renderLeaderList(dom.marketLosers, losers, "down");
    if (!state.bankMode) {
      renderLeaderList(dom.marketTurnover, [], "flat");
      renderLeaderList(dom.marketResearch, [], "flat");
    }
  }

  function renderLeaderList(list, rows, direction) {
    if (!list) return;
    list.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("li");
      empty.className = "leader-empty";
      empty.textContent = "暂无可用快照";
      list.append(empty);
      return;
    }
    rows.forEach((quote, index) => {
      const item = document.createElement("li");
      item.className = "leader-row";
      item.dataset.symbol = quote.symbol;
      item.tabIndex = 0;
      item.setAttribute("role", "button");
      const rank = document.createElement("span");
      rank.className = "leader-rank";
      rank.textContent = String(index + 1).padStart(2, "0");
      const identity = document.createElement("span");
      identity.className = "leader-identity";
      const name = document.createElement("strong");
      name.textContent = quote.name || quote.symbol;
      const symbol = document.createElement("small");
      symbol.textContent = quote.symbol;
      identity.append(name, symbol);
      const change = document.createElement("strong");
      change.className = `leader-change ${directionClass(quote.changePct)}`;
      if (direction === "turnover") change.textContent = formatCompact(quote.amount);
      else if (direction === "research") {
        const research = researchPresentation(quote.researchAction, quote.researchLabel);
        change.textContent = formatResearchScore(quote.researchScore) || research.label;
        change.className = `leader-change ${research.className}`;
      } else change.textContent = formatPercent(quote.changePct);
      item.append(rank, identity, change);
      item.addEventListener("click", () => selectSymbol(quote.symbol));
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectSymbol(quote.symbol);
        }
      });
      list.append(item);
    });
  }

  function analysisLabel(value, labels, fallback = "—") {
    const key = String(value || "");
    return labels[key] || (key ? key : fallback);
  }

  function normalizeAnalysis(payload) {
    const data = objectFrom(payload);
    const indicators = firstDefined(data, ["indicators", "metrics"], {});
    const trend = firstDefined(data, ["trend", "trend_analysis"], {});
    const momentum = firstDefined(data, ["momentum", "momentum_analysis"], {});
    const risk = firstDefined(data, ["risk", "risk_analysis"], {});
    const recommendation = firstDefined(data, ["recommendation", "signal", "bias"], {});
    return {
      ...data,
      symbol: String(firstDefined(data, ["symbol", "code"], state.selectedSymbol || "")),
      name: String(firstDefined(data, ["name", "display_name"], state.selectedSymbol || "")),
      price: finiteNumber(firstDefined(data, ["price", "last", "close"])),
      changePct: finiteNumber(firstDefined(data, ["change_pct", "pct_change", "change_percent"])),
      indicators: indicators && typeof indicators === "object" ? indicators : {},
      trend: trend && typeof trend === "object" ? trend : {},
      momentum: momentum && typeof momentum === "object" ? momentum : {},
      risk: risk && typeof risk === "object" ? risk : {},
      recommendation: recommendation && typeof recommendation === "object" ? recommendation : {},
      dataQuality: firstDefined(data, ["data_quality", "quality"], {}),
      dataProvenance: firstDefined(data, ["data_provenance", "provenance"], {}),
      factors: firstDefined(data, ["factors"], {}),
      rank: finiteNumber(firstDefined(data, ["rank"], null)),
      universe_size: finiteNumber(firstDefined(data, ["universe_size"], null)),
    };
  }

  function formatIndicator(value, kind = "number") {
    const number = finiteNumber(value);
    if (number === null) return "—";
    if (kind === "percent") return formatPercent(number);
    if (kind === "ratio-percent") return formatPercent(number * 100);
    if (kind === "volatility") return formatPercent(number * 100);
    if (kind === "ratio") return `${number.toFixed(2)}x`;
    return number.toFixed(2);
  }

  function renderBankFundamentals(record, symbol) {
    if (!dom.bankFundamentals) return;
    dom.bankFundamentals.hidden = !state.bankMode;
    if (state.bankMode && typeof globalThis.BankResearch !== "undefined") {
      globalThis.BankResearch.renderFundamentals(dom.bankFundamentals, record, symbol);
    } else dom.bankFundamentals.textContent = "";
  }

  function renderAnalysis(payload) {
    const data = normalizeAnalysis(payload);
    state.analysis = data;
    renderBankFundamentals(data.fundamental_factors, data.symbol);
    const marketName = state.marketQuotes.find((item) => item.symbol === data.symbol)?.name;
    if (dom.analysisName) dom.analysisName.textContent = (data.name && data.name !== data.symbol ? data.name : marketName) || data.symbol || "选择股票";
    if (dom.analysisSymbol) dom.analysisSymbol.textContent = data.symbol || "—";
    const quality = data.dataQuality && typeof data.dataQuality === "object" ? data.dataQuality : {};
    const qualityStatus = String(firstDefined(quality, ["status"], "ok")).toLowerCase();
    const provenance = data.dataProvenance && typeof data.dataProvenance === "object" ? data.dataProvenance : {};
    const readiness = quantMonitorAnalysisReadiness(quality, provenance);
    const calculationInput = readiness.calculationInput;
    // Research readiness is deliberately fail-closed.  A complete indicator
    // calculation is not the same as a decision-ready, immutable evidence
    // set, and old payloads without these fields must never look healthy.
    const { analysisReady, decisionReady, analysisUsable, researchReady, failures: readinessFailures, inputLabel } = readiness;
    const hasReadinessFailures = readinessFailures.length > 0;
    const statusText = !analysisUsable ? (qualityStatus === "stale" ? "数据偏旧 · 研究未就绪" : "研究条件未满足") : !decisionReady ? "研究结果可用 · 未认证" : qualityStatus === "ok" ? "分析就绪" : qualityStatus === "stale" ? "数据偏旧" : qualityStatus === "partial" ? "缓存分析" : "数据不足";
    setPill(dom.analysisStatus, statusText, researchReady && qualityStatus === "ok" ? "ok" : qualityStatus === "stale" || qualityStatus === "partial" || !researchReady ? "warning" : "neutral");
    const qualityLabels = { ok: "数据质量 良好", stale: "数据质量 偏旧", partial: "数据质量 部分", insufficient: "数据质量 不足", insufficient_data: "数据质量 不足" };
    const qualityTone = researchReady && qualityStatus === "ok" ? "ok" : ["stale", "partial"].includes(qualityStatus) || !researchReady ? "warning" : "neutral";
    setQualityChip(dom.analysisQualityStatus, !analysisUsable ? "研究证据 未就绪" : !decisionReady ? "研究数据可用 · 未认证" : (qualityLabels[qualityStatus] || `数据质量 ${qualityStatus}`), qualityTone);
    if (dom.analysisQualityStatus) {
      const missingIndicators = firstDefined(quality, ["missing_indicators"], []);
      const qualityReasons = [];
      if (quality.stale || qualityStatus === "stale") qualityReasons.push("日线或快照已超过新鲜度阈值");
      if (Array.isArray(missingIndicators) && missingIndicators.length) qualityReasons.push(`缺少指标：${missingIndicators.slice(0, 4).join("、")}`);
      if (hasReadinessFailures) qualityReasons.push(`研究条件：${readinessFailures.slice(0, 3).map(String).join("、")}`);
      qualityReasons.push(inputLabel);
      dom.analysisQualityStatus.title = qualityReasons.join("；") || "研究数据质量由服务端根据样本量、新鲜度和指标完整性判定";
    }
    const recommendation = data.recommendation || {};
    const bankBreakdown = state.bankMode && typeof globalThis.BankResearch !== "undefined";
    if (dom.analysisScoreLabel) dom.analysisScoreLabel.textContent = state.bankMode ? "同业综合分" : "趋势分数";
    const action = analysisUsable ? String(firstDefined(recommendation, ["action"], "")) : "insufficient_data";
    const actionLabels = { buy: "偏强研究", sell: "偏弱研究", hold: "中性观察", positive_bias: "偏强研究", negative_bias: "偏弱研究", risk_caution: "风险观察", neutral: "中性观察", insufficient_data: "数据不足" };
    const research = researchPresentation(action, firstDefined(recommendation, ["label"], null));
    if (dom.analysisVerdict) {
      dom.analysisVerdict.textContent = actionLabels[action] || research.label;
      dom.analysisVerdict.className = research.className;
    }
    if (dom.analysisVerdictNote) {
      const reason = firstDefined(recommendation, ["reason", "description"], "基于趋势、动量与风险指标");
      const reasonText = Array.isArray(reason) ? reason.filter(Boolean).slice(0, 2).join(" · ") : String(reason);
      dom.analysisVerdictNote.textContent = !analysisUsable
        ? `${inputLabel} · 研究条件未满足${hasReadinessFailures ? `：${readinessFailures.slice(0, 2).map(String).join("、")}` : ""}`
        : `${inputLabel} · ${reasonText}${decisionReady ? "" : " · 来源认证未接入，仅供研究，不构成交易指令"}`;
    }
    const trendScore = finiteNumber(firstDefined(data.trend, ["score"], null));
    if (dom.analysisScore) dom.analysisScore.textContent = trendScore === null ? "—" : state.bankMode ? (trendScore * 100).toFixed(1) : `${trendScore > 0 ? "+" : ""}${(trendScore * 100).toFixed(0)}`;
    const riskScore = finiteNumber(firstDefined(data.risk, ["score"], null));
    const riskLabels = { low: "低", medium: "中", high: "高", insufficient_data: "数据不足" };
    const riskLabel = analysisLabel(firstDefined(data.risk, ["label"], ""), riskLabels);
    if (dom.analysisRisk) dom.analysisRisk.textContent = riskLabel;
    if (dom.analysisRisk) dom.analysisRisk.className = riskLabel === "高" ? "up" : riskLabel === "低" ? "down" : "warning";
    if (dom.analysisRiskNote) dom.analysisRiskNote.textContent = String(firstDefined(data.risk, ["description"], riskScore === null ? "等待波动率" : `风险分数 ${(riskScore * 100).toFixed(0)}`));
    const factors = data.factors && typeof data.factors === "object" ? data.factors : {};
    const metrics = state.bankMode ? [
      ["相对 120 日均线", ["price_to_sma_120"], "ratio-percent", factors],
      ["20 / 60 日均线", ["sma_20_to_60"], "ratio-percent", factors],
      ["同业排名", ["rank"], "rank", data],
      ["60 日波动率", ["annualized_volatility_60"], "volatility", factors],
      ["60-5 日动量", ["momentum_60_5"], "ratio-percent", factors],
      ["60 日回撤", ["max_drawdown_60"], "ratio-percent", factors],
    ] : [
      ["MA5", ["sma_5", "ma5"], "number", data.indicators],
      ["MA20", ["sma_20", "ma20"], "number", data.indicators],
      ["RSI14", ["rsi_14", "rsi14"], "number", data.indicators],
      ["20 日波动率", ["annualized_volatility_20", "volatility_20"], "volatility", data.indicators],
      ["5 日动量", ["return_5", "momentum_5"], "percent", data.indicators],
      ["20 日动量", ["return_20", "momentum_20"], "percent", data.indicators],
    ];
    if (dom.analysisMetrics) {
      [...dom.analysisMetrics.children].forEach((node, index) => {
        const spec = metrics[index];
        if (!spec) return;
        const label = node.querySelector("span");
        if (label) label.textContent = spec[0];
        const value = firstDefined(spec[3], spec[1], null);
        const strong = node.querySelector("strong");
        if (strong) strong.textContent = spec[2] === "rank" && finiteNumber(value) !== null
          ? `${finiteNumber(value)} / ${finiteNumber(data.universe_size) ?? "—"}`
          : formatIndicator(value, spec[2]);
      });
    }
    const barsUsed = firstDefined(quality, ["bars_used"], data.bars_used);
    const barsRequired = firstDefined(quality, ["bars_required"], null);
    const coverageText = barsUsed !== null && barsUsed !== undefined
      ? `样本 ${barsUsed}${barsRequired ? ` / ${barsRequired}` : ""} 根`
      : "样本 —";
    setQualityChip(dom.analysisQualityCoverage, coverageText, barsRequired && Number(barsUsed) < Number(barsRequired) ? "warning" : "neutral");
    const barAsOf = firstDefined(quality, ["bar_as_of", "latest_bar_at"], null);
    setQualityChip(dom.analysisQualityAsOf, barAsOf ? `日线截至 ${formatTime(barAsOf, true)}` : "日线截至 —", barAsOf ? "neutral" : "warning");
    const entryEligible = firstDefined(recommendation, ["entry_eligible"], null);
    const gateFailures = firstDefined(recommendation, ["gate_failures"], []);
    const gateFailed = Array.isArray(gateFailures) && gateFailures.length > 0;
    if (entryEligible === true && analysisUsable && decisionReady) setQualityChip(dom.analysisEntryGate, "入场门控 满足", "ok");
    else if (entryEligible === true && analysisUsable) setQualityChip(dom.analysisEntryGate, "入场门控 计算通过 · 未认证", "warning");
    else if (entryEligible === false || gateFailed) setQualityChip(dom.analysisEntryGate, gateFailed ? `入场门控 未通过 ${gateFailures.length}` : "入场门控 未满足", "warning");
    else setQualityChip(dom.analysisEntryGate, "入场门控 —", "neutral");
    if (dom.analysisEntryGate) dom.analysisEntryGate.title = gateFailed ? gateFailures.map(String).join("；") : "策略未提供可解释的入场门控结果";
    const evidence = firstDefined(recommendation, ["evidence", "reasons", "reason"], [
      firstDefined(data.trend, ["description"], "趋势证据暂不可用"),
      firstDefined(data.momentum, ["description"], "动量证据暂不可用"),
    ]);
    const risks = firstDefined(recommendation, ["risks", "risk_factors"], [firstDefined(data.risk, ["description"], "风险指标暂不可用")]);
    const invalidations = firstDefined(recommendation, ["invalidation_conditions", "invalidations", "exit_conditions"], firstDefined(data, ["invalidation_conditions", "invalidations"], null));
    renderDetailList(dom.analysisEvidence, evidence, "当前缓存未提供可复核的研究证据");
    renderDetailList(dom.analysisRisks, risks, "风险指标或历史样本不足");
    renderDetailList(dom.analysisInvalidations, invalidations, "缓存策略尚未提供明确失效条件，不应据此形成交易指令");
    if (dom.analysisFactorBreakdown) {
      if (bankBreakdown) globalThis.BankResearch.render(dom.analysisFactorBreakdown, data);
      else dom.analysisFactorBreakdown.textContent = "";
    }
    if (dom.analysisNote) {
      const qualityNote = barsUsed !== null && barsUsed !== undefined
        ? `使用 ${barsUsed}${barsRequired ? ` / 至少 ${barsRequired}` : ""} 根 K 线 · 数据状态 ${qualityStatus} · ${inputLabel} · ${!analysisUsable ? "研究条件未满足，暂不形成方向性结论" : decisionReady ? "研究条件已满足" : "研究结果可用但来源未认证，仅供研究"} · 仅供研究观察，不构成投资建议。`
        : `${inputLabel} · 当前仅展示服务端已有缓存分析；研究条件未满足，不构成投资建议。`;
      dom.analysisNote.textContent = qualityNote;
    }
  }

  function renderAnalysisUnavailable(bankItem = null) {
    const quote = bankItem || {};
    renderBankFundamentals(quote.fundamental_factors, quote.symbol);
    state.analysis = null;
    if (dom.analysisName) dom.analysisName.textContent = quote.name || "选择银行股";
    if (dom.analysisSymbol) dom.analysisSymbol.textContent = quote.symbol || "—";
    setPill(dom.analysisStatus, quote.symbol ? "缓存分析不足" : "等待选择", "neutral");
    if (dom.analysisVerdict) {
      dom.analysisVerdict.textContent = "数据不足";
      dom.analysisVerdict.className = "";
    }
    if (dom.analysisVerdictNote) dom.analysisVerdictNote.textContent = "板块页不会为每只银行自动请求日线";
    if (dom.analysisScore) dom.analysisScore.textContent = "—";
    if (dom.analysisScoreLabel) dom.analysisScoreLabel.textContent = state.bankMode ? "同业综合分" : "趋势分数";
    if (dom.analysisRisk) {
      dom.analysisRisk.textContent = "—";
      dom.analysisRisk.className = "";
    }
    if (dom.analysisRiskNote) dom.analysisRiskNote.textContent = "等待完整历史样本";
    setQualityChip(dom.analysisQualityStatus, "数据质量 不足", "neutral");
    setQualityChip(dom.analysisQualityCoverage, "样本 —", "neutral");
    setQualityChip(dom.analysisQualityAsOf, "日线截至 —", "warning");
    setQualityChip(dom.analysisEntryGate, "入场门控 —", "neutral");
    if (dom.analysisMetrics) [...dom.analysisMetrics.querySelectorAll("strong")].forEach((node) => { node.textContent = "—"; });
    if (dom.analysisFactorBreakdown) dom.analysisFactorBreakdown.textContent = "";
    renderDetailList(dom.analysisEvidence, null, "当前缓存没有足够的历史 K 线证据");
    renderDetailList(dom.analysisRisks, null, "数据不足本身就是风险，暂不形成方向性研究偏向");
    renderDetailList(dom.analysisInvalidations, null, "没有有效研究结论，因此没有可执行的失效条件");
    if (dom.analysisNote) dom.analysisNote.textContent = "如需查看该股历史走势，请主动点击选择；板块刷新不会批量调用每股分析接口。";
  }

  function normalizeOverview(raw, index = 0) {
    const quote = normalizeQuote(raw);
    return {
      ...quote,
      name: quote.name === "未命名" ? ["上证指数", "深证成指", "创业板指", "科创50"][index] || "市场指标" : quote.name,
      symbol: quote.symbol || ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH"][index] || "",
      metric: firstDefined(raw, ["metric", "label"]),
      secondary: firstDefined(raw, ["secondary", "description"]),
      sparkline: listFrom(firstDefined(raw, ["sparkline", "trend", "series"], [])),
    };
  }

  function makeSparkline(values) {
    const numbers = values.map((item) => finiteNumber(typeof item === "object" ? firstDefined(item, ["value", "price", "close"]) : item)).filter((item) => item !== null);
    if (numbers.length < 2) return null;
    const min = Math.min(...numbers);
    const max = Math.max(...numbers);
    const range = max - min || 1;
    const points = numbers.map((value, index) => `${(index / (numbers.length - 1)) * 72 + 1},${23 - ((value - min) / range) * 20}`).join(" ");
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.classList.add("metric-spark");
    svg.setAttribute("viewBox", "0 0 74 25");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", `M ${points.replaceAll(" ", " L ")}`);
    svg.append(path);
    return svg;
  }

  function renderOverview(items) {
    dom.overview.replaceChildren();
    const fallback = [
      { name: "上证指数", symbol: "000001.SH" },
      { name: "深证成指", symbol: "399001.SZ" },
      { name: "创业板指", symbol: "399006.SZ" },
      { name: "市场宽度", symbol: "UP / DOWN" },
    ];
    const rows = items.length ? items.slice(0, 4) : fallback;
    rows.forEach((raw, index) => {
      const item = normalizeOverview(raw, index);
      const card = document.createElement("article");
      card.className = "metric-card";
      const head = document.createElement("div");
      head.className = "metric-card-head";
      const name = document.createElement("h3");
      name.textContent = item.name;
      const symbol = document.createElement("span");
      symbol.className = "symbol";
      symbol.textContent = item.symbol;
      head.append(name, symbol);

      const main = document.createElement("div");
      main.className = "metric-main";
      const value = document.createElement("span");
      value.className = "metric-value";
      value.textContent = item.metric ?? formatPrice(item.price);
      const change = document.createElement("span");
      change.className = `metric-change ${directionClass(item.changePct)}`;
      change.textContent = formatPercent(item.changePct);
      main.append(value, change);

      const foot = document.createElement("div");
      foot.className = "metric-foot";
      const label = document.createElement("span");
      label.textContent = item.secondary || (item.amount !== null ? `成交额 ${formatCompact(item.amount)}` : "等待数据源");
      foot.append(label);
      card.append(head, main, foot);
      const sparkline = makeSparkline(item.sparkline);
      if (sparkline) card.append(sparkline);
      dom.overview.append(card);
    });
  }

  function renderBankOverview(payload) {
    const object = objectFrom(payload);
    const universe = firstDefined(object, ["universe"], {});
    const coverage = firstDefined(object, ["coverage"], {});
    const summary = firstDefined(object, ["summary", "breadth"], {});
    const quota = firstDefined(object, ["quota", "request_quota"], {});
    const total = finiteNumber(firstDefined(universe, ["total", "count"], firstDefined(coverage, ["configured", "total"], state.bankItems.length)));
    const received = finiteNumber(firstDefined(coverage, ["quotes_received", "received", "priced"], state.bankItems.filter((item) => item.price !== null).length));
    const analysisReady = finiteNumber(firstDefined(coverage, ["analysis_ready"], null));
    const qualityReady = finiteNumber(firstDefined(coverage, ["quality_ready"], null));
    const decisionReady = finiteNumber(firstDefined(coverage, ["decision_ready"], null));
    const advancers = finiteNumber(firstDefined(summary, ["advancers", "advancing"], state.bankItems.filter((item) => item.changePct !== null && item.changePct > 0).length));
    const decliners = finiteNumber(firstDefined(summary, ["decliners", "declining"], state.bankItems.filter((item) => item.changePct !== null && item.changePct < 0).length));
    const median = finiteNumber(firstDefined(summary, ["median_pct_change", "median_change_pct"], null));
    const limit = finiteNumber(firstDefined(quota, ["soft_limit", "limit", "daily_limit", "total"], null));
    const used = finiteNumber(firstDefined(quota, ["used", "requests_used", "consumed"], null));
    let remaining = finiteNumber(firstDefined(quota, ["remaining_soft", "remaining", "available"], null));
    if (remaining === null && limit !== null && used !== null) remaining = Math.max(0, limit - used);
    const coverageSecondary = `缺失 ${Math.max(0, (total ?? 0) - (received ?? 0))} 只`;
    const coverageEvidence = [
      analysisReady === null ? null : `研究 ${analysisReady}`,
      qualityReady === null ? null : `质量证据 ${qualityReady}`,
      decisionReady === null ? null : `决策证据 ${decisionReady}`,
    ].filter(Boolean).join(" / ");
    const cards = [
      { name: "银行股覆盖", symbol: "COVERAGE", metric: `${received ?? 0} / ${total ?? state.bankItems.length}`, secondary: coverageEvidence ? `${coverageSecondary}；${coverageEvidence}` : coverageSecondary },
      { name: "涨跌家数", symbol: "UP / DOWN", metric: `${advancers ?? 0} / ${decliners ?? 0}`, secondary: `上涨 / 下跌` },
      { name: "中位涨幅", symbol: "MEDIAN", metric: formatPercent(median), secondary: "较平均值更少受极端波动影响", change_pct: median },
      { name: "今日 API 额度", symbol: "LOCAL EST.", metric: remaining === null ? "—" : `${remaining} 次`, secondary: limit === null ? "本机估算" : `本机软预算 ${limit} 次` },
    ];
    renderOverview(cards);
    if (dom.bankQuotaBadge) {
      dom.bankQuotaBadge.hidden = false;
      dom.bankQuotaBadge.textContent = remaining === null ? "额度 —" : `额度 ${remaining}/${limit ?? "—"}`;
      dom.bankQuotaBadge.classList.toggle("warning", remaining !== null && remaining <= 10);
      dom.bankQuotaBadge.title = "本机持久化软预算估算，不代表供应商账户权威账单";
    }
  }

  function bankSummaryPayload(payload) {
    const object = objectFrom(payload);
    const universe = firstDefined(object, ["universe"], {});
    const coverage = firstDefined(object, ["coverage"], {});
    const summary = firstDefined(object, ["summary", "breadth"], {});
    return {
      items: state.bankItems.map((item) => ({
        symbol: item.symbol,
        name: item.name,
        price: item.price,
        previous_close: item.preClose,
        change: item.change,
        pct_change: item.changePct,
        amount: item.amount,
        volume: item.volume,
        updated_at: item.updatedAt,
        freshness: item.freshness,
        stale: item.stale,
      })),
      total: finiteNumber(firstDefined(coverage, ["quotes_received", "received"], state.bankItems.length)) ?? state.bankItems.length,
      universe_total: finiteNumber(firstDefined(universe, ["total", "count"], firstDefined(coverage, ["configured", "total"], state.bankItems.length))) ?? state.bankItems.length,
      unpriced_count: finiteNumber(firstDefined(coverage, ["unpriced", "missing_quotes", "missing"], null)),
      summary,
      source: firstDefined(object, ["provider", "source"], firstDefined(universe, ["classification", "source"], "银行板块缓存")),
      universe_scope: "bank_sector",
      as_of: firstDefined(object, ["as_of", "updated_at"], firstDefined(summary, ["as_of"], null)),
    };
  }

  function renderBankRankings(payload) {
    const object = objectFrom(payload);
    const rankings = firstDefined(object, ["rankings", "leaders"], {});
    const bySymbol = new Map(state.bankItems.map((item) => [item.symbol, item]));
    const normalizeRows = (value) => listFrom(value, ["items", "quotes", "stocks"]).map((raw) => {
      const normalized = unwrapBankItem(raw);
      return bySymbol.get(normalized.symbol) || normalized;
    }).filter((item) => item.symbol);
    const gainers = normalizeRows(firstDefined(rankings, ["gainers", "top_gainers", "change_desc"], []));
    const losers = normalizeRows(firstDefined(rankings, ["losers", "top_losers", "change_asc"], []));
    const turnover = normalizeRows(firstDefined(rankings, ["turnover", "top_turnover", "turnover_desc"], []));
    const research = normalizeRows(firstDefined(rankings, ["quant_score_cached", "research", "quant", "analysis"], []));
    const fallbackGainers = state.bankItems.filter((item) => item.changePct !== null).slice().sort((a, b) => b.changePct - a.changePct).slice(0, 5);
    const fallbackLosers = state.bankItems.filter((item) => item.changePct !== null).slice().sort((a, b) => a.changePct - b.changePct).slice(0, 5);
    const fallbackTurnover = state.bankItems.filter((item) => item.amount !== null).slice().sort((a, b) => b.amount - a.amount).slice(0, 5);
    const fallbackResearch = state.bankItems.filter((item) => item.researchScore !== null).slice().sort((a, b) => b.researchScore - a.researchScore).slice(0, 5);
    renderLeaderList(dom.marketGainers, (gainers.length ? gainers : fallbackGainers).slice(0, 5), "up");
    renderLeaderList(dom.marketLosers, (losers.length ? losers : fallbackLosers).slice(0, 5), "down");
    renderLeaderList(dom.marketTurnover, (turnover.length ? turnover : fallbackTurnover).slice(0, 5), "turnover");
    renderLeaderList(dom.marketResearch, (research.length ? research : fallbackResearch).slice(0, 5), "research");
  }

  function filteredBankItems() {
    if (!state.bankMode) return state.watchlist;
    const search = state.bankSearch.trim().toLocaleLowerCase("zh-CN");
    const rows = state.watchlist.filter((item) => {
      const matchesSearch = !search || item.symbol.toLowerCase().includes(search) || item.name.toLocaleLowerCase("zh-CN").includes(search);
      const matchesExchange = state.bankExchange === "all" || item.exchange === state.bankExchange;
      return matchesSearch && matchesExchange;
    });
    const knownLast = (value, ascending = false) => value === null ? (ascending ? Number.POSITIVE_INFINITY : Number.NEGATIVE_INFINITY) : value;
    rows.sort((a, b) => {
      if (state.bankSort === "change_asc") return knownLast(a.changePct, true) - knownLast(b.changePct, true) || a.symbol.localeCompare(b.symbol);
      if (state.bankSort === "turnover_desc") return knownLast(b.amount) - knownLast(a.amount) || a.symbol.localeCompare(b.symbol);
      if (state.bankSort === "research_desc") return knownLast(b.researchScore) - knownLast(a.researchScore) || a.symbol.localeCompare(b.symbol);
      if (state.bankSort === "symbol_asc") return a.symbol.localeCompare(b.symbol);
      return knownLast(b.changePct) - knownLast(a.changePct) || a.symbol.localeCompare(b.symbol);
    });
    return rows;
  }

  function applyMarketStatus(raw) {
    if (!raw || typeof raw !== "object") return;
    const trading = firstDefined(raw, ["is_trading", "trading", "is_open"]);
    const phase = String(firstDefined(raw, ["phase", "status", "state"], "")).toLowerCase();
    const label = String(firstDefined(raw, ["label", "name"], trading ? "交易中" : "休市"));
    const calendarLookup = String(firstDefined(raw, ["calendar_lookup"], "")).toLowerCase();
    const calendar = String(firstDefined(raw, ["calendar"], "")).toLowerCase();
    const calendarUnknown = calendarLookup === "unknown" || calendar === "weekday_fallback" && calendarLookup !== "open" && calendarLookup !== "closed";
    state.marketStatus = { trading: trading === true, phase, label, calendarLookup, calendar, receivedAt: Date.now() };
    const status = calendarUnknown ? "warning" : trading === true ? "ok" : ["auction", "preopen", "call_auction", "集合竞价"].includes(phase) ? "warning" : "closed";
    setPill(dom.marketBadge, label, status);
    if (dom.marketBadge) dom.marketBadge.title = calendarUnknown
      ? "交易日历未覆盖当前日期，市场状态仅按工作日规则估计"
      : "";
  }

  function renderWatchlist() {
    const configuredCount = state.configuredWatchlist.length;
    const visibleItems = filteredBankItems();
    dom.watchlistCount.textContent = state.bankMode
      ? `显示 ${visibleItems.length} / 板块 ${state.watchlist.length}`
      : configuredCount
        ? `已收到 ${state.watchlist.length} / 配置 ${configuredCount}`
        : `${state.watchlist.length} 只`;
    dom.watchlistBody.replaceChildren();
    if (!visibleItems.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 5;
      const empty = document.createElement("div");
      empty.className = "empty-state compact";
      const title = document.createElement("span");
      title.textContent = state.bankMode && state.watchlist.length ? "没有符合筛选条件的银行股" : state.bankMode ? "尚未收到银行板块缓存" : configuredCount ? "尚未收到自选行情" : "尚未配置自选股票";
      const note = document.createElement("small");
      const quota = state.lastSystem?.provider?.quota || {};
      const exhausted = quota && typeof quota === "object" && firstDefined(quota, ["exhausted"], false) === true;
      note.textContent = exhausted
        ? "本机预算已用尽；保留缓存并等待每日额度重置"
        : configuredCount
          ? state.bankMode ? "服务端将返回已有板块缓存；页面不会自动逐股请求" : "本地服务已启动，等待上游批量快照或本地缓存"
          : "请先在配置中添加自选标的";
      empty.append(title, note);
      cell.append(empty);
      row.append(cell);
      dom.watchlistBody.append(row);
      return;
    }

    visibleItems.forEach((quote) => {
      const row = document.createElement("tr");
      row.dataset.symbol = quote.symbol;
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", `查看 ${quote.name} ${quote.symbol} 走势`);
      if (quote.symbol === state.selectedSymbol) row.classList.add("selected");

      const identity = document.createElement("td");
      const stockId = document.createElement("div");
      stockId.className = "stock-id";
      const name = document.createElement("strong");
      name.textContent = quote.name;
      const code = document.createElement("small");
      code.textContent = state.bankMode && quote.exchange ? `${quote.symbol} · ${quote.exchange}` : quote.symbol;
      stockId.append(name, code);
      identity.append(stockId);

      const price = document.createElement("td");
      price.className = "quote-price";
      price.dataset.field = "price";
      price.textContent = formatPrice(quote.price);
      const priceCell = document.createElement("td");
      priceCell.dataset.field = "price-cell";
      const priceStack = document.createElement("div");
      priceStack.className = "quote-price-stack";
      priceStack.append(price);
      priceCell.append(priceStack);
      renderQuoteFreshness(priceCell, quote);

      const changeCell = document.createElement("td");
      const change = document.createElement("span");
      change.className = `quote-change ${directionClass(quote.changePct)}`;
      change.dataset.field = "change";
      change.textContent = formatPercent(quote.changePct);
      changeCell.append(change);

      const amount = document.createElement("td");
      amount.className = "optional-amount";
      amount.dataset.field = "amount";
      amount.textContent = formatCompact(quote.amount);
      const researchCell = document.createElement("td");
      const research = researchPresentation(quote.researchAction, quote.researchLabel);
      const researchTag = document.createElement("span");
      researchTag.className = `research-tag ${research.className}`;
      researchTag.textContent = quote.analysis ? research.label : "待分析";
      researchCell.append(researchTag);
      row.append(identity, priceCell, changeCell, amount, researchCell);
      row.addEventListener("click", () => selectSymbol(quote.symbol));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectSymbol(quote.symbol);
        }
      });
      dom.watchlistBody.append(row);
    });
  }

  // The treemap is initially built from the cache-only HTTP snapshot.  Local
  // WebSocket quote events can then update an existing tile in place, without
  // requesting the provider again.  This keeps the cloud responsive when the
  // collector is running while preserving the provider's daily request budget.
  function scheduleTreemapRender() {
    if (!state.sectorTreemapPayload || !dom.sectorTreemap) return;
    window.clearTimeout(state.treemapRenderTimer);
    state.treemapRenderTimer = window.setTimeout(() => {
      state.treemapRenderTimer = null;
      renderSectorTreemap(state.sectorTreemapPayload);
    }, 120);
  }

  function updateTreemapQuote(raw) {
    const incoming = normalizeQuote(raw);
    const payload = state.sectorTreemapPayload;
    if (!incoming.symbol || !payload) return false;
    const object = objectFrom(payload);
    const groups = listFrom(object, ["sectors", "industries", "groups", "items"]);
    const arrays = [];
    const seen = new Set();
    const addArray = (rows) => {
      if (!Array.isArray(rows) || seen.has(rows)) return;
      seen.add(rows);
      arrays.push(rows);
    };
    groups.forEach((group) => {
      if (!group || typeof group !== "object") return;
      ["children", "items", "stocks", "quotes", "members"].forEach((key) => addArray(group[key]));
    });
    ["stocks", "quotes", "records"].forEach((key) => addArray(object[key]));
    let touched = false;
    arrays.forEach((rows) => rows.forEach((item) => {
      if (!item || typeof item !== "object") return;
      const symbol = String(firstDefined(item, ["symbol", "code", "ticker"], ""));
      if (symbol !== incoming.symbol) return;
      touched = true;
      if (incoming.price !== null) {
        item.price = incoming.price;
        item.last = incoming.price;
      }
      if (incoming.preClose !== null) item.previous_close = incoming.preClose;
      if (incoming.changePct !== null) {
        item.change_pct = incoming.changePct;
        item.pct_change = incoming.changePct;
      }
      if (incoming.amount !== null) {
        item.amount = incoming.amount;
        item.turnover = incoming.amount;
        if (String(firstDefined(item, ["size_basis"], "")).includes("turnover")) {
          item.size = incoming.amount > 0 ? incoming.amount : 1;
          item.value = item.size;
        }
      }
      if (incoming.updatedAt) {
        item.timestamp = incoming.updatedAt;
        payload.as_of = incoming.updatedAt;
        payload.updated_at = incoming.updatedAt;
      }
      item.freshness = incoming.freshness || (incoming.updatedAt ? "fresh" : item.freshness || "unknown");
      item.quote_status = "available";
    }));
    if (touched) {
      payload.live_update_at = incoming.updatedAt || new Date().toISOString();
      payload.served_from = "sqlite_cache+local_websocket";
      scheduleTreemapRender();
    }
    return touched;
  }

  function updateQuote(raw) {
    const incoming = normalizeQuote(raw);
    if (!incoming.symbol) return;
    updateTreemapQuote(raw);
    const index = state.watchlist.findIndex((item) => item.symbol === incoming.symbol);
    if (index < 0) return;
    const previous = state.watchlist[index];
    state.watchlist[index] = {
      ...previous,
      ...Object.fromEntries(Object.entries(incoming).filter(([, value]) => value !== null && value !== "" && value !== "未命名")),
      name: incoming.name === "未命名" ? previous.name : incoming.name,
    };
    const row = dom.watchlistBody.querySelector(`tr[data-symbol="${CSS.escape(incoming.symbol)}"]`);
    if (row) {
      const priceNode = row.querySelector('[data-field="price"]');
      const changeNode = row.querySelector('[data-field="change"]');
      const amountNode = row.querySelector('[data-field="amount"]');
      if (priceNode && incoming.price !== null) {
        priceNode.textContent = formatPrice(incoming.price);
        const movement = previous.price !== null && incoming.price !== previous.price ? (incoming.price > previous.price ? "flash-up" : "flash-down") : "";
        if (movement) {
          priceNode.classList.remove("flash-up", "flash-down");
          void priceNode.offsetWidth;
          priceNode.classList.add(movement);
        }
      }
      const priceCell = row.querySelector('[data-field="price-cell"]');
      if (priceCell) renderQuoteFreshness(priceCell, state.watchlist[index]);
      if (changeNode && incoming.changePct !== null) {
        changeNode.className = `quote-change ${directionClass(incoming.changePct)}`;
        changeNode.textContent = formatPercent(incoming.changePct);
      }
      if (amountNode && incoming.amount !== null) amountNode.textContent = formatCompact(incoming.amount);
    }
    if (incoming.symbol === state.selectedSymbol) updateSelectedSummary(state.watchlist[index]);
    if (incoming.symbol === state.selectedSymbol && state.analysis) {
      state.analysis.price = incoming.price;
      state.analysis.changePct = incoming.changePct;
      renderAnalysis(state.analysis);
    }
  }

  function updateSelectedSummary(quote) {
    if (!quote) return;
    dom.selectedName.textContent = quote.name;
    dom.selectedSymbol.textContent = quote.symbol;
    dom.selectedPrice.textContent = formatPrice(quote.price);
    dom.selectedPrice.className = directionClass(quote.changePct);
    dom.selectedChange.textContent = `${formatPrice(quote.change)}  ${formatPercent(quote.changePct)}`;
    dom.selectedChange.className = directionClass(quote.changePct);
  }

  async function selectSymbol(symbol, force = false) {
    if (!symbol) return;
    if (!force && state.selectedSymbol === symbol && state.bars.length && state.analysis) return;
    // Never carry the previous symbol's research conclusion while a new
    // request is in flight.  A failed request must leave an explicit empty
    // state rather than silently showing stale evidence for another stock.
    if (state.selectedSymbol !== symbol) {
      state.analysis = null;
      state.bars = [];
      state.chartProvenance = null;
    }
    state.selectedSymbol = symbol;
    renderWatchlist();
    const quote = state.watchlist.find((item) => item.symbol === symbol)
      || state.marketQuotes.find((item) => item.symbol === symbol);
    updateSelectedSummary(quote || { name: symbol, symbol, price: null, change: null, changePct: null });
    if (state.bankMode) {
      // Bank-sector refreshes never fan out to one `/analysis` request per
      // symbol. A user click may explicitly load the selected chart once, but
      // the explainable panel first consumes the aggregate endpoint's cache.
      if (quote?.analysis) renderAnalysis({ ...quote.analysis, symbol: quote.symbol, name: quote.name });
      else renderAnalysisUnavailable(quote);
      await loadBars(symbol).catch(() => {});
      return;
    }
    // Generic mode preserves the existing on-demand sequence: bars populate
    // SQLite first, so analysis can normally reuse them.
    await loadBars(symbol).catch(() => {});
    await loadAnalysis(symbol).catch(() => {});
  }

  function normalizeBar(raw) {
    return {
      time: firstDefined(raw, ["time", "datetime", "timestamp", "date", "trade_date"]),
      open: finiteNumber(firstDefined(raw, ["open", "o"])),
      high: finiteNumber(firstDefined(raw, ["high", "h"])),
      low: finiteNumber(firstDefined(raw, ["low", "l"])),
      close: finiteNumber(firstDefined(raw, ["close", "price", "c", "last"])),
      volume: finiteNumber(firstDefined(raw, ["volume", "vol", "v"])),
      ma5: finiteNumber(firstDefined(raw, ["ma5", "ma_5"])),
      ma20: finiteNumber(firstDefined(raw, ["ma20", "ma_20"])),
    };
  }

  // A successful bars response is not, by itself, proof that every rendered
  // candle has immutable source/quality evidence. Keep that distinction
  // visible next to the chart so cached legacy projections cannot be mistaken
  // for a fully auditable history.
  function renderChartProvenance(provenance, rows = state.bars) {
    if (!dom.chartProvenance) return;
    const data = provenance && typeof provenance === "object" ? provenance : {};
    const served = finiteNumber(firstDefined(data, ["served_item_count", "count"], rows.length)) ?? rows.length;
    const matched = finiteNumber(firstDefined(data, ["immutable_observation_count", "matched_count"], null));
    const legacy = finiteNumber(firstDefined(data, ["legacy_item_count"], matched === null ? null : Math.max(0, served - matched)));
    const last = rows.length ? rows[rows.length - 1].time : null;
    const asOf = firstDefined(data, ["as_of", "latest_period_end", "updated_at"], last);
    const dateText = asOf ? ` · 截至 ${formatTime(asOf, true)}` : "";
    let tone = "neutral";
    let text = "K 线来源 —";
    if (!served) {
      text = "K 线暂无数据";
    } else if (data.authoritative === true && matched === served) {
      tone = "ok";
      text = `K 线可追溯 · ${served} 根${dateText}`;
    } else if (matched !== null && matched > 0 && legacy !== null && legacy > 0) {
      tone = "warning";
      text = `K 线部分可追溯 · ${matched}/${served} 根${dateText}`;
    } else {
      tone = "warning";
      text = `K 线缓存投影 · ${served} 根${dateText}`;
    }
    dom.chartProvenance.className = `chart-provenance ${tone}`;
    dom.chartProvenance.textContent = text;
    const warnings = Array.isArray(data.warnings) && data.warnings.length ? `；警告：${data.warnings.join("、")}` : "";
    dom.chartProvenance.title = `${data.authoritative === true ? "已声明权威" : "兼容缓存投影，未声明权威"}${matched !== null ? `；不可变观测 ${matched}/${served}` : ""}${warnings}`;
  }

  function movingAverage(rows, size) {
    let sum = 0;
    const queue = [];
    return rows.map((bar) => {
      queue.push(bar.close);
      sum += bar.close;
      if (queue.length > size) sum -= queue.shift();
      return queue.length === size ? sum / size : null;
    });
  }

  function svgNode(name, attributes = {}) {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function linePath(values, xFor, yFor) {
    let drawing = false;
    return values.map((value, index) => {
      if (value === null || !Number.isFinite(value)) {
        drawing = false;
        return "";
      }
      const command = drawing ? "L" : "M";
      drawing = true;
      return `${command}${xFor(index).toFixed(2)},${yFor(value).toFixed(2)}`;
    }).join(" ");
  }

  function renderCharts() {
    dom.priceChart.replaceChildren();
    dom.volumeChart.replaceChildren();
    dom.chartTooltip.hidden = true;
    const rows = state.bars.filter((bar) => bar.close !== null);
    renderChartProvenance(state.chartProvenance, rows);
    if (rows.length < 2) {
      dom.chartEmpty.hidden = false;
      return;
    }
    dom.chartEmpty.hidden = true;

    const width = 800;
    const height = 300;
    // Keep the price scale next to the left edge, where it remains visible
    // while the user scans the newest candles on the right.
    const padding = { top: 12, right: 10, bottom: 27, left: 54 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const ma5Computed = movingAverage(rows, 5);
    const ma20Computed = movingAverage(rows, 20);
    const ma5 = rows.map((bar, index) => bar.ma5 ?? ma5Computed[index]);
    const ma20 = rows.map((bar, index) => bar.ma20 ?? ma20Computed[index]);
    const values = [...rows.map((bar) => bar.close), ...ma5, ...ma20].filter((value) => value !== null && Number.isFinite(value));
    let min = Math.min(...values);
    let max = Math.max(...values);
    const span = max - min || Math.abs(max) * .02 || 1;
    min -= span * .08;
    max += span * .08;
    const xFor = (index) => padding.left + (index / (rows.length - 1)) * plotWidth;
    const yFor = (value) => padding.top + ((max - value) / (max - min)) * plotHeight;

    const defs = svgNode("defs");
    const gradient = svgNode("linearGradient", { id: "price-gradient", x1: "0", y1: "0", x2: "0", y2: "1" });
    gradient.append(svgNode("stop", { offset: "0%", "stop-color": "#20c7b7", "stop-opacity": ".21" }), svgNode("stop", { offset: "100%", "stop-color": "#20c7b7", "stop-opacity": "0" }));
    defs.append(gradient);
    dom.priceChart.append(defs);

    for (let line = 0; line <= 4; line += 1) {
      const y = padding.top + (line / 4) * plotHeight;
      dom.priceChart.append(svgNode("line", { x1: padding.left, y1: y, x2: width - padding.right, y2: y, class: "grid-line" }));
      const label = svgNode("text", {
        x: padding.left - 7,
        y: y + 3,
        class: "axis-label",
        "text-anchor": "end",
      });
      label.textContent = formatPrice(max - (line / 4) * (max - min));
      dom.priceChart.append(label);
    }

    const labelCount = Math.min(5, rows.length);
    for (let tick = 0; tick < labelCount; tick += 1) {
      const index = Math.round((tick / Math.max(1, labelCount - 1)) * (rows.length - 1));
      const x = xFor(index);
      dom.priceChart.append(svgNode("line", { x1: x, y1: padding.top, x2: x, y2: height - padding.bottom, class: "grid-line" }));
      const label = svgNode("text", { x, y: height - 8, class: "axis-label", "text-anchor": tick === 0 ? "start" : tick === labelCount - 1 ? "end" : "middle" });
      label.textContent = formatAxisTime(rows[index].time);
      dom.priceChart.append(label);
    }

    const closePath = linePath(rows.map((bar) => bar.close), xFor, yFor);
    const area = `${closePath} L${xFor(rows.length - 1)},${height - padding.bottom} L${xFor(0)},${height - padding.bottom} Z`;
    dom.priceChart.append(svgNode("path", { d: area, class: "price-area" }));
    dom.priceChart.append(svgNode("path", { d: closePath, class: "price-line" }));
    dom.priceChart.append(svgNode("path", { d: linePath(ma5, xFor, yFor), class: "ma-line ma5" }));
    dom.priceChart.append(svgNode("path", { d: linePath(ma20, xFor, yFor), class: "ma-line ma20" }));

    const crossX = svgNode("line", { class: "crosshair", y1: padding.top, y2: height - padding.bottom, visibility: "hidden" });
    const crossY = svgNode("line", { class: "crosshair", x1: padding.left, x2: width - padding.right, visibility: "hidden" });
    const point = svgNode("circle", { r: 3.5, fill: "#20c7b7", stroke: "#071019", "stroke-width": 2, visibility: "hidden" });
    const hitArea = svgNode("rect", { x: padding.left, y: padding.top, width: plotWidth, height: plotHeight, class: "chart-hit-area" });
    hitArea.addEventListener("pointermove", (event) => {
      const rect = dom.priceChart.getBoundingClientRect();
      const svgX = ((event.clientX - rect.left) / rect.width) * width;
      const index = Math.max(0, Math.min(rows.length - 1, Math.round(((svgX - padding.left) / plotWidth) * (rows.length - 1))));
      const x = xFor(index);
      const y = yFor(rows[index].close);
      crossX.setAttribute("x1", x); crossX.setAttribute("x2", x);
      crossY.setAttribute("y1", y); crossY.setAttribute("y2", y);
      point.setAttribute("cx", x); point.setAttribute("cy", y);
      crossX.setAttribute("visibility", "visible"); crossY.setAttribute("visibility", "visible"); point.setAttribute("visibility", "visible");
      const tooltipX = (x / width) * rect.width;
      const tooltipY = (y / height) * rect.height;
      dom.chartTooltip.textContent = `${formatTime(rows[index].time, true)}\n收 ${formatPrice(rows[index].close)}  量 ${formatCompact(rows[index].volume)}`;
      dom.chartTooltip.style.whiteSpace = "pre-line";
      dom.chartTooltip.style.left = `${Math.min(rect.width - 130, Math.max(4, tooltipX + 12))}px`;
      dom.chartTooltip.style.top = `${Math.max(4, tooltipY - 44)}px`;
      dom.chartTooltip.hidden = false;
    });
    hitArea.addEventListener("pointerleave", () => {
      crossX.setAttribute("visibility", "hidden"); crossY.setAttribute("visibility", "hidden"); point.setAttribute("visibility", "hidden");
      dom.chartTooltip.hidden = true;
    });
    dom.priceChart.append(crossX, crossY, point, hitArea);

    const volumeHeight = 88;
    const maxVolume = Math.max(...rows.map((bar) => bar.volume || 0), 1);
    const barSlot = plotWidth / rows.length;
    rows.forEach((bar, index) => {
      const value = bar.volume || 0;
      const h = (value / maxVolume) * 67;
      const previous = index ? rows[index - 1].close : bar.open;
      const direction = previous === null || previous === bar.close ? "flat" : bar.close > previous ? "up" : "down";
      dom.volumeChart.append(svgNode("rect", {
        x: padding.left + index * barSlot + Math.max(.5, barSlot * .12),
        y: volumeHeight - h,
        width: Math.max(.7, barSlot * .76),
        height: Math.max(.5, h),
        class: `volume-bar ${direction}`,
      }));
    });
  }

  function formatAxisTime(value) {
    const date = toDate(value);
    if (!date) return value ? String(value).slice(-5) : "";
    const options = state.period === "1m"
      ? { hour: "2-digit", minute: "2-digit", hour12: false }
      : { month: "2-digit", day: "2-digit" };
    return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", ...options }).format(date);
  }

  function renderEvents(type, items) {
    const isAlert = type === "alert";
    const list = isAlert ? dom.alertsList : dom.signalsList;
    const count = isAlert ? dom.alertCount : dom.signalCount;
    const rows = items.slice(0, MAX_EVENTS);
    count.textContent = `${rows.length} ${isAlert ? "条" : "个"}`;
    list.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state compact";
      const title = document.createElement("span");
      title.textContent = isAlert ? "暂无告警" : "暂无研究信号";
      const note = document.createElement("small");
      note.textContent = isAlert ? "触发监控规则后会显示" : "研究偏向不是交易指令";
      empty.append(title, note);
      list.append(empty);
      return;
    }
    rows.forEach((raw) => list.append(makeEventNode(raw, isAlert)));
  }

  function makeEventNode(raw, isAlert) {
    const item = document.createElement("article");
    item.className = "event-item";
    const actionRaw = String(firstDefined(raw, ["action", "signal", "side", "direction"], "hold")).toLowerCase();
    const action = ["buy", "long", "positive_bias", "bullish", "买入", "看多"].includes(actionRaw) ? "buy" : ["sell", "short", "negative_bias", "bearish", "卖出", "看空"].includes(actionRaw) ? "sell" : "hold";
    const severityRaw = String(firstDefined(raw, ["severity", "level", "priority"], "low")).toLowerCase();
    const severity = ["critical", "error", "high", "严重", "高"].includes(severityRaw) ? "high" : ["warning", "medium", "中"].includes(severityRaw) ? "medium" : "low";
    const dot = document.createElement("span");
    dot.className = `event-dot ${isAlert ? severity : action}`;

    const copy = document.createElement("div");
    copy.className = "event-copy";
    const title = document.createElement("strong");
    const symbol = firstDefined(raw, ["symbol", "code", "ticker"], "");
    const name = firstDefined(raw, ["title", "name", "strategy_name", "rule_name"], isAlert ? "行情告警" : "策略信号");
    if (!isAlert) {
      const tag = document.createElement("span");
      tag.className = `event-tag ${action}`;
      tag.textContent = action === "buy" ? "偏强研究" : action === "sell" ? "偏弱研究" : "中性观察";
      title.append(tag);
    }
    title.append(document.createTextNode(`${name}${symbol ? ` · ${symbol}` : ""}`));
    const description = document.createElement("p");
    description.textContent = String(firstDefined(raw, ["message", "description", "reason", "content"], "等待更多详情"));
    copy.append(title, description);
    if (!isAlert) {
      const assurance = raw && raw.research_assurance;
      const bindingState = assurance && assurance.strategy_binding_status;
      const bindingLabel = bindingState === "digest_consistent"
        ? "策略摘要一致（非来源认证）"
        : bindingState === "invalid" ? "策略绑定异常" : "策略绑定未记录";
      const evidence = document.createElement("small");
      evidence.className = "event-assurance";
      // Missing/old server fields must not make historical BUY/SELL events
      // appear certified.  The signal API has no bound input dataset yet.
      evidence.textContent = `历史研究输出 · 输入血缘未绑定 · ${bindingLabel} · 非交易指令`;
      copy.append(evidence);
    }

    const time = document.createElement("time");
    time.className = "event-time";
    const timestamp = firstDefined(raw, ["created_at", "timestamp", "time", "datetime"]);
    time.textContent = formatTime(timestamp);
    if (timestamp) time.dateTime = String(timestamp);
    item.append(dot, copy, time);
    return item;
  }

  function prependEvent(type, raw) {
    const list = type === "alert" ? dom.alertsList : dom.signalsList;
    const count = type === "alert" ? dom.alertCount : dom.signalCount;
    const empty = list.querySelector(".empty-state");
    if (empty) empty.remove();
    list.prepend(makeEventNode(raw, type === "alert"));
    while (list.children.length > MAX_EVENTS) list.lastElementChild.remove();
    count.textContent = `${list.children.length} ${type === "alert" ? "条" : "个"}`;
  }

  function renderReadiness(data, quota) {
    if (!dom.readinessBanner) return;
    const startup = firstDefined(data, ["startup", "readiness"], null);
    const bootstrap = startup && typeof startup === "object"
      ? firstDefined(startup, ["history_bootstrap", "history", "bootstrap"], null)
      : null;
    const total = finiteNumber(firstDefined(bootstrap, ["total", "configured", "symbols"], null));
    if (!bootstrap || total === null || total <= 0) {
      dom.readinessBanner.hidden = true;
      return;
    }

    const rawReady = finiteNumber(firstDefined(bootstrap, ["ready", "completed", "available"], 0)) || 0;
    const ready = Math.max(0, Math.min(total, rawReady));
    const remaining = Math.max(0, finiteNumber(firstDefined(bootstrap, ["remaining", "pending"], total - ready)) ?? total - ready);
    const attempted = Math.max(0, finiteNumber(firstDefined(bootstrap, ["attempted", "processed"], 0)) || 0);
    const currentSymbol = String(firstDefined(bootstrap, ["current_symbol", "symbol", "current"], "") || "").trim();
    const failedValue = firstDefined(bootstrap, ["failed", "failures", "errors"], {});
    const failedSymbols = Array.isArray(failedValue)
      ? failedValue.map(String)
      : failedValue && typeof failedValue === "object"
        ? Object.keys(failedValue)
        : [];
    const failedCount = failedSymbols.length || Math.max(0, finiteNumber(failedValue) || 0);
    const stateValue = String(firstDefined(bootstrap, ["state", "status"], ready >= total ? "complete" : "running")).toLowerCase();
    const servingReady = firstDefined(startup, ["serving_ready", "ready"], true) !== false;
    const researchReady = firstDefined(startup, ["research_ready"], ready >= total) === true;
    const deferred = firstDefined(bootstrap, ["deferred_for_budget", "deferred"], false) === true
      || ["deferred_budget", "quota_deferred", "budget_deferred"].includes(stateValue);
    const stopped = ["failed", "error", "stopped"].includes(stateValue);
    const complete = ready >= total && !deferred && !stopped && failedCount === 0;
    const checking = ["scheduled", "checking", "pending"].includes(stateValue);
    const percentage = total > 0 ? Math.round((ready / total) * 100) : 0;
    const quotaRemaining = finiteNumber(firstDefined(quota, ["remaining", "remaining_soft", "requests_remaining_soft", "available"], null));
    const quotaLimit = finiteNumber(firstDefined(quota, ["soft_limit", "limit", "daily_limit", "total"], null));

    let tone = "neutral";
    let title = servingReady ? "服务已启动，历史数据在后台准备" : "本地服务正在初始化";
    let badge = checking ? "等待后台任务" : "后台补数中";
    let detail = "页面可以继续访问；量化排名将在历史日线满足要求后自动出现。";
    if (complete) {
      tone = "ok";
      title = "历史日线准备完成";
      badge = researchReady ? "研究数据已就绪" : "历史数据已就绪";
      detail = "银行股历史日线已完成本地缓存，量化研究可直接读取缓存。";
    } else if (deferred) {
      tone = "warning";
      title = "服务运行正常，历史补数因额度保护暂停";
      badge = "等待额度恢复";
      detail = `已保留行情请求额度，后台将在额度可用后自动继续${quotaRemaining !== null ? `；当前剩余 ${quotaRemaining}${quotaLimit !== null ? ` / ${quotaLimit}` : ""} 次` : ""}。`;
    } else if (stopped) {
      tone = "bad";
      title = "服务已启动，但历史补数已停止";
      badge = "补数失败";
      detail = "请查看监控告警，检查 API Key、接口权限或当日额度后重启服务。";
    } else if (failedCount > 0 || stateValue === "partial") {
      tone = "warning";
      title = "服务已启动，部分历史数据仍在重试";
      badge = `${failedCount} 只暂未就绪`;
      const examples = failedSymbols.slice(0, 3).join("、");
      detail = `${examples ? `暂未就绪：${examples}${failedCount > 3 ? " 等" : ""}。` : "部分股票暂未就绪。"}后台会按刷新周期继续检查，页面无需一直停留。`;
    }

    const stateLabels = {
      scheduled: "任务已排队",
      checking: "检查更新中",
      pending: "等待后台任务",
      running: "后台补数中",
      partial: "部分待重试",
      complete: "准备完成",
      completed: "准备完成",
      ready: "准备完成",
    };
    if (!complete && !deferred && !stopped && failedCount === 0) badge = stateLabels[stateValue] || badge;
    const activity = currentSymbol
      ? ` · 当前 ${currentSymbol}`
      : attempted > ready && !complete ? ` · 已尝试 ${attempted} 只` : "";

    dom.readinessBanner.hidden = false;
    dom.readinessBanner.dataset.state = tone;
    dom.readinessIcon.className = `readiness-icon ${tone}${!complete && !deferred && !stopped ? " active" : ""}`;
    dom.readinessTitle.textContent = title;
    setPill(dom.readinessBadge, badge, tone);
    dom.readinessSummary.textContent = `历史日线已准备 ${ready} / ${total} 只 · 剩余 ${remaining} 只${activity}`;
    dom.readinessDetail.textContent = detail;
    dom.readinessProgress.setAttribute("aria-valuemax", String(total));
    dom.readinessProgress.setAttribute("aria-valuenow", String(ready));
    dom.readinessProgress.setAttribute("aria-valuetext", `${ready} / ${total} 只股票已准备`);
    dom.readinessProgressBar.style.width = `${percentage}%`;
    dom.readinessPercent.textContent = `${percentage}%`;
  }

  function renderSystem(payload) {
    const data = objectFrom(payload);
    state.lastSystem = data;
    // The system endpoint carries the provider's freshness threshold.  Apply
    // it to already-rendered rows so a later health poll can relabel cached
    // quotes without fetching each detail endpoint again.
    refreshQuoteFreshnessIndicators();
    // The same threshold also changes the aggregate warning in the market
    // panel; refresh it after health telemetry arrives so the note cannot
    // remain falsely green from the earlier cache render.
    refreshMarketFreshnessSummary();
    const healthyRaw = firstDefined(data, ["healthy", "ok", "status"], false);
    const statusRaw = firstDefined(data, ["status"], healthyRaw);
    const healthy = healthyRaw === true || ["ok", "healthy", "running", "up", "正常"].includes(String(healthyRaw).toLowerCase());
    const degraded = ["degraded", "warning", "partial", "降级"].includes(String(statusRaw).toLowerCase());
    setPill(dom.healthBadge, healthy ? "运行正常" : degraded ? "服务降级" : "服务离线", healthy ? "ok" : degraded ? "warning" : "bad");

    const delay = finiteNumber(firstDefined(data, ["last_collection_age_ms", "latency_ms", "delay_ms", "data_latency_ms", "latency"]));
    const provider = firstDefined(data, ["provider"], {});
    const providerName = provider && typeof provider === "object"
      ? firstDefined(provider, ["name", "provider", "source"], "未配置")
      : firstDefined(data, ["data_source", "source", "provider"], "未配置");
    const providerState = provider && typeof provider === "object" ? firstDefined(provider, ["state", "status"]) : null;
    const source = `${providerName}${providerState ? ` · ${providerState}` : ""}`;
    const uptime = firstDefined(data, ["uptime_seconds", "uptime", "running_seconds"]);
    const collector = firstDefined(data, ["collector"], {});
    const collectorRunning = collector && typeof collector === "object" ? firstDefined(collector, ["running", "active", "status"]) : null;
    const settings = firstDefined(data, ["settings"], {});
    const configuredWatchlist = settings && typeof settings === "object" ? firstDefined(settings, ["watchlist", "symbols"]) : null;
    const monitoredSymbols = firstDefined(data, ["subscriptions", "symbols_count", "watchlist_count"], Array.isArray(configuredWatchlist) ? configuredWatchlist.length : null);
    state.configuredWatchlist = Array.isArray(configuredWatchlist) ? configuredWatchlist.slice() : state.configuredWatchlist;
    const isAShareHub = String(providerName).toLowerCase() === "asharehub";
    const providerMode = String(firstDefined(provider, ["mode", "transport", "delivery_mode"], isAShareHub ? "http_polling" : "unspecified")).toLowerCase();
    const legacyProviderTelemetry = provider && typeof provider === "object" ? firstDefined(provider, ["realtime"], {}) : {};
    const quota = provider && typeof provider === "object" ? firstDefined(provider, ["quota", "request_quota", "usage"], legacyProviderTelemetry) : {};
    const cache = provider && typeof provider === "object" ? firstDefined(provider, ["cache", "cache_status"], legacyProviderTelemetry) : {};
    const capabilities = provider && typeof provider === "object" ? firstDefined(provider, ["capabilities"], {}) : {};
    const quotaLimit = finiteNumber(firstDefined(quota, ["limit", "soft_limit", "daily_limit", "daily_soft_limit", "total"], null));
    const quotaUsed = finiteNumber(firstDefined(quota, ["used", "requests_used", "requests_used_today", "consumed"], null));
    let quotaRemaining = finiteNumber(firstDefined(quota, ["remaining", "remaining_soft", "requests_remaining", "requests_remaining_soft", "available"], null));
    if (quotaRemaining === null && quotaLimit !== null && quotaUsed !== null) quotaRemaining = Math.max(0, quotaLimit - quotaUsed);
    const quotaEstimated = firstDefined(quota, ["estimated", "is_estimate", "local_estimate"], isAShareHub) !== false;
    const upstreamQuotaExhausted = firstDefined(quota, ["upstream_exhausted"], false) === true;
    const localQuotaExhausted = firstDefined(quota, ["local_exhausted"], false) === true || quotaRemaining === 0;
    const quotaExhausted = firstDefined(quota, ["exhausted"], false) === true || quotaRemaining === 0;
    const cacheState = String(firstDefined(cache, ["state", "status"], "")).toLowerCase();
    const cacheHit = firstDefined(cache, ["hit", "last_request_hit", "served_from_cache"], null);
    const cacheTtl = finiteNumber(firstDefined(cache, ["quote_ttl_seconds", "quote_cache_seconds", "ttl_seconds"], null));
    const nextRefreshAt = firstDefined(cache, ["next_quote_refresh_at", "next_refresh_at", "refresh_at"], firstDefined(legacyProviderTelemetry, ["next_refresh_at"], null));
    const lastProviderSuccess = firstDefined(provider, ["last_success_at", "last_request_at", "updated_at"], firstDefined(cache, ["last_success_at", "last_request_at", "last_quote_fetch_at", "updated_at"], null));
    const providerStateText = String(providerState || "starting").toLowerCase();
    const upstreamRunning = ["running", "ok", "healthy", "ready"].includes(providerStateText) && !quotaExhausted;
    const modeLabel = providerMode.includes("http") || providerMode.includes("poll") ? "HTTP 快照" : "行情";
    let upstreamLabel = `${providerName} ${modeLabel}`;
    if (quotaRemaining !== null && quotaLimit !== null) upstreamLabel += ` · ${quotaRemaining}/${quotaLimit}`;
    if (upstreamQuotaExhausted) upstreamLabel = `${providerName} 上游额度已拒绝`;
    else if (localQuotaExhausted) upstreamLabel = `${providerName} 本机预算已用尽`;
    else if (!upstreamRunning) upstreamLabel = `${providerName} ${String(providerState || "检查中")}`;
    setPill(dom.providerStatus, upstreamLabel, upstreamRunning ? "ok" : degraded || quotaExhausted ? "warning" : "neutral");
    dom.providerStatus.title = quotaRemaining !== null && quotaLimit !== null
      ? `${upstreamQuotaExhausted ? "供应商已返回额度拒绝；" : ""}${quotaEstimated ? "本机进程估算" : "供应商返回"}：今日剩余 ${quotaRemaining}/${quotaLimit} 次；${cacheHit === true || cacheState === "hit" ? "本次命中缓存" : "请求会优先使用本地缓存"}${cacheTtl !== null ? `（快照缓存 ${cacheTtl} 秒）` : ""}${nextRefreshAt ? `；计划更新 ${formatTime(nextRefreshAt, true)}` : ""}`
      : "本地服务到上游行情 API 的状态；请求会优先使用本地缓存";
    if (quotaExhausted && !state.watchlist.length && dom.marketInsightsNote) {
      dom.marketInsightsNote.textContent = "本机估算的每日 API 预算已用尽；不会自动请求全市场，当前仅显示已有缓存。";
      renderWatchlist();
    }
    renderReadiness(data, quota);
    const hasOrderBook = firstDefined(capabilities, ["order_book", "depth", "level2"], isAShareHub ? false : null) === true;
    const hasIntradayBars = firstDefined(capabilities, ["intraday_bars", "minute_bars", "kline_intraday"], isAShareHub ? false : null);
    const supportedIntervals = firstDefined(capabilities, ["bar_intervals", "supported_intervals", "intervals"], isAShareHub ? ["1d"] : null);
    document.querySelectorAll(".period-button").forEach((button) => {
      const period = String(button.dataset.period || "").toLowerCase();
      let supported = Array.isArray(supportedIntervals) ? supportedIntervals.map(String).map((item) => item.toLowerCase()).includes(period) : null;
      if (period === "1m" && hasIntradayBars !== null) supported = hasIntradayBars === true;
      button.disabled = supported !== null ? !supported : false;
      button.title = supported === true ? `${period} K 线` : supported === false ? `${providerName} 当前不提供 ${period} K 线` : "周期能力由当前数据源决定";
    });
    const collectorLabel = collectorRunning === true || ["running", "ok", "active"].includes(String(collectorRunning).toLowerCase()) ? "运行中" : collectorRunning === false ? "已停止" : "—";
    const quotaMetric = quotaRemaining !== null && quotaLimit !== null
      ? `${quotaEstimated ? "本机估算 " : ""}${quotaRemaining}/${quotaLimit}`
      : monitoredSymbols ?? "—";
    const definitions = [
      ["距上次采集", delay === null ? "—" : `${Math.round(delay)} ms`],
      ["数据源", source],
      [uptime === null || uptime === undefined ? "采集器" : "运行时长", uptime === null || uptime === undefined ? collectorLabel : (typeof uptime === "string" ? uptime : formatDuration(uptime))],
      [quotaRemaining !== null ? "今日额度" : "监控标的", quotaMetric],
    ];
    [...dom.systemMetrics.children].forEach((row, index) => {
      const [label, value] = definitions[index];
      row.querySelector("dt").textContent = label;
      row.querySelector("dd").textContent = String(value);
    });
    dom.heartbeatDot.className = `heartbeat ${healthy ? "ok" : ""}`;
    const heartbeat = firstDefined(data, ["last_heartbeat", "updated_at", "timestamp", "server_time"], collector && typeof collector === "object" ? firstDefined(collector, ["last_run", "last_cycle", "updated_at"]) : null);
    const collectorError = collector && typeof collector === "object" ? firstDefined(collector, ["last_error", "error"]) : null;
    const providerIssues = provider && typeof provider === "object" ? firstDefined(provider, ["issues"], []) : [];
    const quoteFreshness = provider && typeof provider === "object" ? firstDefined(provider, ["quote_freshness"], {}) : {};
    const freshnessState = String(firstDefined(quoteFreshness, ["state"], "")).toLowerCase();
    const freshnessReceived = finiteNumber(firstDefined(quoteFreshness, ["received_symbols"], null));
    const freshnessConfigured = finiteNumber(firstDefined(quoteFreshness, ["configured_symbols"], null));
    const freshnessSummary = freshnessReceived !== null && freshnessConfigured !== null
      ? `行情 ${freshnessReceived}/${freshnessConfigured}${freshnessState && !["fresh", "untracked"].includes(freshnessState) ? ` · ${({ stale: "过期", partial: "部分", missing: "缺失", future_timestamp: "时间异常" }[freshnessState] || freshnessState)}` : ""}`
      : "";
    if (healthy) {
      const cacheNote = cacheHit === true || cacheState === "hit" ? " · 本次命中本地缓存" : "";
      const capabilityNote = isAShareHub && !hasOrderBook ? " · 无五档盘口" : "";
      dom.heartbeatText.textContent = `最近心跳 ${formatTime(heartbeat || Date.now())}${lastProviderSuccess ? ` · 上游更新 ${formatTime(lastProviderSuccess, true)}` : ""}${freshnessSummary ? ` · ${freshnessSummary}` : ""}${cacheNote}${capabilityNote}`;
    } else if (collectorError) {
      dom.heartbeatText.textContent = `采集异常：${String(collectorError).slice(0, 140)}`;
    } else if (Array.isArray(providerIssues) && providerIssues.length) {
      const issueLabels = {
        upstream_connecting: "上游行情接口正在连接",
        upstream_unavailable: "上游行情接口暂不可用",
        quota_low: "本机估算的今日 API 额度较低",
        quota_exhausted: "本机估算的今日 API 预算已用尽",
        upstream_quota_exhausted: "供应商已拒绝当日额度请求",
        rate_limited: "供应商已限制请求频率",
        authentication_failed: "API Key 认证失败",
        no_quote_data: "尚未收到可用行情快照",
        quote_data_missing: "部分行情快照缺失",
        quote_data_stale: "行情快照已过期",
        quote_timestamp_future: "行情时间戳异常",
        order_book_unavailable: "五档盘口暂不可用",
        daily_reference_unavailable: "日 K 参考数据暂不可用",
        static_info_unavailable: "证券基础信息暂不可用",
      };
      const labels = providerIssues
        .filter((item) => item !== "collector_error")
        .map((item) => issueLabels[item] || String(item));
      dom.heartbeatText.textContent = `数据能力降级：${labels.join("；").slice(0, 140)}`;
    } else {
      dom.heartbeatText.textContent = degraded ? "部分行情暂不可用" : "未收到服务心跳";
    }
  }

  async function loadOverview() {
    const payload = await fetchJSON(`${API_BASE}/overview`);
    const object = objectFrom(payload);
    applyMarketStatus(firstDefined(object, ["market_status", "market", "session"]));
    let items = listFrom(payload, ["indices", "overview", "markets"]);
    if (!items.length && object.indices) items = listFrom(object.indices);
    if (!items.length) {
      const candidateKeys = ["shanghai", "shenzhen", "chinext", "star50", "market_breadth"];
      items = candidateKeys.map((key) => object[key]).filter((item) => item && typeof item === "object");
    }
    const counts = firstDefined(object, ["counts", "summary"], {});
    if (items.length > 0 && items.length < 4 && counts && typeof counts === "object") {
      const watched = firstDefined(counts, ["watchlist", "symbols", "stocks"], 0);
      const alerts = firstDefined(counts, ["alerts", "alert_count"], 0);
      const signals = firstDefined(counts, ["signals", "signal_count"], 0);
      items.push({ name: "监控任务", symbol: "LOCAL", metric: `${watched} 只自选`, secondary: `告警 ${alerts} · 信号 ${signals}` });
    }
    renderOverview(items);
  }

  async function loadBankSector() {
    const payload = await fetchJSON(`${API_BASE}/sectors/banks`);
    const object = objectFrom(payload);
    const rawItems = listFrom(payload, ["items", "banks", "stocks", "records"]);
    if (!rawItems.length) {
      const universe = firstDefined(object, ["universe"], {});
      const expected = finiteNumber(firstDefined(universe, ["total", "count"], 0)) || 0;
      const mode = String(firstDefined(universe, ["mode", "type", "name"], firstDefined(firstDefined(object, ["sector"], {}), ["id", "name"], ""))).toLowerCase();
      if (!expected && !mode.includes("bank") && !mode.includes("银行")) throw new Error("Bank sector endpoint returned an incompatible payload");
    }
    const rows = rawItems
      .map(unwrapBankItem)
      .filter((item) => item.symbol);
    state.bankMode = true;
    document.body.classList.add("bank-mode");
    state.bankPayload = object;
    state.bankItems = rows;
    state.watchlist = rows;
    state.marketQuotes = rows;
    state.configuredWatchlist = rows.map((item) => item.symbol);
    if (dom.bankToolbar) dom.bankToolbar.hidden = false;
    applyMarketStatus(firstDefined(object, ["market_status", "market", "session"], null));
    renderBankOverview(object);
    renderMarketQuotes(bankSummaryPayload(object));
    // `renderMarketQuotes` normalizes a compact view for breadth; retain the
    // enriched bank rows (exchange, cached analysis and research score) for
    // rankings, filters and row selection.
    state.marketQuotes = rows;
    renderBankRankings(object);
    renderWatchlist();
    const coverage = firstDefined(object, ["coverage"], {});
    const universe = firstDefined(object, ["universe"], {});
    const total = finiteNumber(firstDefined(universe, ["total", "count"], rows.length)) ?? rows.length;
    const received = finiteNumber(firstDefined(coverage, ["quotes_received", "received"], rows.filter((item) => item.price !== null).length)) ?? rows.length;
    if (dom.marketTotal) dom.marketTotal.textContent = `已收到 ${received} / 银行 ${total}`;
    const quota = firstDefined(object, ["quota"], {});
    const remaining = finiteNumber(firstDefined(quota, ["remaining_soft", "remaining"], null));
    const limit = finiteNumber(firstDefined(quota, ["soft_limit", "limit"], null));
    const asOf = firstDefined(object, ["as_of", "updated_at"], firstDefined(universe, ["as_of", "updated_at"], firstDefined(coverage, ["as_of", "updated_at"], null)));
    const notice = String(firstDefined(object, ["notice"], "银行分类、排名与量化摘要来自服务端缓存"));
    if (dom.marketInsightsNote) {
      const freshnessStates = state.bankItems.map(quoteFreshness);
      const freshnessStatus = quantMonitorFreshnessSummary(freshnessStates);
      const freshnessText = freshnessNotice(state.bankItems);
      dom.marketInsightsNote.dataset.freshness = freshnessStatus.status;
      dom.marketInsightsNote.dataset.freshnessText = freshnessText;
      dom.marketInsightsNote.textContent = `${asOf ? `数据时间 ${formatTime(asOf, true)} · ` : ""}${notice}${remaining !== null ? ` · 本机估算额度 ${remaining}/${limit ?? "—"}` : ""} · ${freshnessText} · 页面筛选与排序不调用上游 API。`;
      dom.marketInsightsNote.setAttribute("aria-live", "polite");
    }
    if (state.selectedSymbol && !rows.some((item) => item.symbol === state.selectedSymbol)) {
      state.selectedSymbol = null;
      state.analysis = null;
      state.bars = [];
    }
    if (state.selectedSymbol) {
      const selected = rows.find((item) => item.symbol === state.selectedSymbol);
      updateSelectedSummary(selected);
      if (selected?.analysis) renderAnalysis({ ...selected.analysis, symbol: selected.symbol, name: selected.name });
      else renderAnalysisUnavailable(selected);
    } else {
      renderAnalysisUnavailable();
    }
    return payload;
  }

  async function loadWatchlist() {
    const payload = await fetchJSON(`${API_BASE}/watchlist`);
    const items = listFrom(payload, ["watchlist", "quotes", "stocks"]);
    const configured = firstDefined(payload, ["configured_count"], null);
    const missing = firstDefined(payload, ["missing_symbols"], []);
    if (Array.isArray(missing)) {
      state.configuredWatchlist = [...new Set([...items.map((item) => firstDefined(item, ["symbol", "code"])), ...missing].filter(Boolean))];
    } else if (configured === 0) {
      state.configuredWatchlist = [];
    }
    state.watchlist = items.map(normalizeQuote).filter((item) => item.symbol);
    renderMarketQuotes({
      items,
      total: state.watchlist.length,
      universe_total: finiteNumber(configured) ?? state.configuredWatchlist.length,
      universe_scope: "configured_symbols",
      source: firstDefined(payload, ["provider", "source", "data_source"], "未配置"),
      as_of: firstDefined(payload, ["as_of", "updated_at", "timestamp"], null),
    });
    if (state.selectedSymbol && !state.watchlist.some((item) => item.symbol === state.selectedSymbol)) state.selectedSymbol = null;
    renderWatchlist();
    if (!state.selectedSymbol && state.watchlist[0]) {
      state.selectedSymbol = state.watchlist[0].symbol;
      renderWatchlist();
      updateSelectedSummary(state.watchlist[0]);
    }
    else if (state.selectedSymbol) updateSelectedSummary(state.watchlist.find((item) => item.symbol === state.selectedSymbol));
  }

  async function loadBars(symbol = state.selectedSymbol) {
    if (!symbol) {
      state.bars = [];
      state.chartProvenance = null;
      renderCharts();
      return;
    }
    const serial = ++state.requestSerial;
    dom.chartEmpty.hidden = false;
    dom.chartEmpty.querySelector("strong").textContent = "正在加载走势";
    dom.chartEmpty.querySelector("small").textContent = `${symbol} · ${state.period}`;
    try {
      const payload = await fetchJSON(
        `${API_BASE}/bars/${encodeURIComponent(symbol)}?interval=${encodeURIComponent(state.period)}&limit=240`,
        { timeout: 60000 },
      );
      if (serial !== state.requestSerial || symbol !== state.selectedSymbol) return;
      const items = listFrom(payload, ["bars", "candles", "klines", "series"]);
      state.bars = items.map(normalizeBar).filter((item) => item.close !== null).slice(-300);
      state.chartProvenance = firstDefined(objectFrom(payload), ["data_provenance"], null);
      renderCharts();
    } catch (error) {
      if (serial !== state.requestSerial) return;
      state.bars = [];
      state.chartProvenance = null;
      renderCharts();
      dom.chartEmpty.querySelector("strong").textContent = "走势数据暂不可用";
      dom.chartEmpty.querySelector("small").textContent = requestErrorMessage(error, "等待本地数据服务提供历史行情");
      throw error;
    }
  }

  async function loadAnalysis(symbol = state.selectedSymbol) {
    if (!symbol) {
      state.analysis = null;
      return;
    }
    try {
      const payload = await fetchJSON(
        `${API_BASE}/analysis/${encodeURIComponent(symbol)}?interval=1d&limit=240`,
        { timeout: 60000 },
      );
      if (symbol === state.selectedSymbol) renderAnalysis(payload);
    } catch (error) {
      if (symbol !== state.selectedSymbol) return;
      state.analysis = null;
      setPill(dom.analysisStatus, "分析不可用", "bad");
      renderBankFundamentals(null, symbol);
      if (dom.analysisScore) dom.analysisScore.textContent = "—";
      if (dom.analysisRisk) { dom.analysisRisk.textContent = "—"; dom.analysisRisk.className = ""; }
      if (dom.analysisFactorBreakdown) dom.analysisFactorBreakdown.textContent = "";
      renderDetailList(dom.analysisEvidence, null, "当前请求没有返回可复核的研究证据");
      renderDetailList(dom.analysisRisks, null, "历史行情不足或数据源暂不可用");
      renderDetailList(dom.analysisInvalidations, null, "没有有效研究结论，因此没有可执行的失效条件");
      if (dom.analysisNote) dom.analysisNote.textContent = requestErrorMessage(error, "历史行情不足或数据源暂不可用，暂不生成分析结论。");
      throw error;
    }
  }

  async function loadAlerts() {
    const payload = await fetchJSON(`${API_BASE}/alerts`);
    renderEvents("alert", listFrom(payload, ["alerts", "events"]));
  }

  async function loadSignals() {
    const payload = await fetchJSON(`${API_BASE}/signals`);
    renderEvents("signal", listFrom(payload, ["signals", "events"]));
  }

  async function loadSystem() {
    const payload = await fetchJSON(`${API_BASE}/system/status`);
    renderSystem(payload);
  }

  function formatResearchMetric(value, kind = "percent") {
    const number = finiteNumber(value);
    if (number === null) return "—";
    if (kind === "count") return Math.round(number).toLocaleString("zh-CN");
    if (kind === "ratio") return number.toFixed(2);
    if (kind === "unsigned_percent") return `${(number * 100).toFixed(2)}%`;
    return `${number >= 0 ? "+" : ""}${(number * 100).toFixed(2)}%`;
  }

  function renderResearchCompare(payload) {
    if (!dom.researchCompareResult) return;
    const rows = listFrom(payload, ["runs", "items"]);
    dom.researchCompareResult.replaceChildren();
    if (!rows.length) {
      dom.researchCompareResult.hidden = true;
      return;
    }
    const heading = document.createElement("div");
    heading.className = "research-compare-heading";
    heading.textContent = `对比结果 · ${rows.length} 个研究运行（仅本地缓存）`;
    dom.researchCompareResult.append(heading);
    const grid = document.createElement("div");
    grid.className = "research-compare-grid";
    rows.forEach((row) => {
      const card = document.createElement("article");
      card.className = "research-compare-card";
      const title = document.createElement("strong");
      title.textContent = String(row.strategy_name || "未命名策略");
      const id = document.createElement("small");
      id.textContent = String(row.run_id || "—");
      card.append(title, id);
      const metrics = row.metrics && typeof row.metrics === "object" ? row.metrics : {};
      const table = document.createElement("dl");
      [["累计收益", formatResearchMetric(metrics.total_return)], ["年化收益", formatResearchMetric(metrics.annualized_return)],
        ["夏普", formatResearchMetric(metrics.sharpe_ratio, "ratio")], ["最大回撤", formatResearchMetric(metrics.maximum_drawdown, "unsigned_percent")],
        ["换手率", formatResearchMetric(metrics.turnover, "unsigned_percent")], ["交易次数", formatResearchMetric(metrics.trade_count, "count")]].forEach(([label, value]) => {
        const dt = document.createElement("dt");
        dt.textContent = label;
        const dd = document.createElement("dd");
        dd.textContent = value;
        table.append(dt, dd);
      });
      card.append(table);
      const status = document.createElement("span");
      status.className = `research-result-status ${row.result_available ? "ok" : "neutral"}`;
      status.textContent = row.result_available ? "结果已固化" : "尚无结果";
      card.append(status);
      if (row.result_available) {
        const gate = document.createElement("small");
        const failures = Array.isArray(row.decision_grade_failures) ? row.decision_grade_failures : [];
        gate.className = `research-gate-status ${row.decision_grade === true ? "ok" : "warn"}`;
        gate.textContent = row.decision_grade === true ? "研究门禁通过" : `研究门禁未通过 · ${failures.length || 1} 项证据不足`;
        if (failures.length) gate.title = failures.join(", ");
        card.append(gate);
      }
      grid.append(card);
    });
    dom.researchCompareResult.append(grid);
    dom.researchCompareResult.hidden = false;
  }

  function renderResearchRuns(runs) {
    if (!dom.researchRunsList) return;
    const normalized = (Array.isArray(runs) ? runs : []).map((item) => ({
      runId: String(firstDefined(item, ["run_id", "id"], "")),
      strategy: String(firstDefined(item, ["strategy_name"], "未命名策略")),
      dataset: String(firstDefined(item, ["dataset_name"], "未命名数据集")),
      createdAt: firstDefined(item, ["created_at"], null),
      hash: String(firstDefined(item, ["content_hash", "manifest_hash"], "")),
      resultAvailable: firstDefined(item, ["result_available"], false) === true,
    })).filter((item) => item.runId);
    state.researchRuns = normalized;
    const known = new Set(normalized.map((item) => item.runId));
    state.researchSelected = new Set([...state.researchSelected].filter((id) => known.has(id)));
    dom.researchRunsCount.textContent = `${normalized.length} 条`;
    dom.researchRunsList.replaceChildren();
    if (!normalized.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state compact";
      empty.innerHTML = "<span>暂无已保存研究运行</span><small>通过研究 API 创建运行后，可在此勾选比较</small>";
      dom.researchRunsList.append(empty);
      dom.researchCompare.disabled = true;
      return;
    }
    const list = document.createElement("div");
    list.className = "research-run-rows";
    normalized.forEach((run) => {
      const label = document.createElement("div");
      label.className = "research-run-row";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = run.runId;
      checkbox.checked = state.researchSelected.has(run.runId);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.researchSelected.add(run.runId);
        else state.researchSelected.delete(run.runId);
        dom.researchCompare.disabled = state.researchSelected.size < 2;
      });
      const copy = document.createElement("label");
      copy.className = "research-run-copy";
      checkbox.id = `research-select-${normalized.indexOf(run)}`;
      copy.htmlFor = checkbox.id;
      checkbox.setAttribute("aria-label", `比较研究运行 ${run.runId}`);
      const title = document.createElement("strong");
      title.textContent = `${run.strategy} · ${run.runId}`;
      const meta = document.createElement("small");
      meta.textContent = `${run.dataset} · ${formatTime(run.createdAt, true)}${run.hash ? ` · ${run.hash.slice(0, 18)}…` : ""}`;
      copy.append(title, meta);
      label.append(checkbox, copy);

      // Keep immutable research artifacts one click away.  These links are
      // cache-only endpoints: downloading never spends provider quota and the
      // server returns the canonical bytes plus integrity headers.
      const actions = document.createElement("span");
      actions.className = "research-run-actions";
      const exportLink = document.createElement("a");
      exportLink.className = "research-run-action";
      exportLink.href = `${API_BASE}/research/runs/${encodeURIComponent(run.runId)}/export`;
      exportLink.download = "research-run.json";
      exportLink.textContent = "导出";
      exportLink.title = "下载研究运行、清单与结果（仅本地缓存）";
      exportLink.addEventListener("click", (event) => event.stopPropagation());
      actions.append(exportLink);
      if (run.resultAvailable) {
        if (reportViewer) {
          const reportButton = document.createElement("button");
          reportButton.type = "button";
          reportButton.className = "research-run-action";
          reportButton.textContent = "查看报告";
          reportButton.setAttribute("aria-controls", "research-report");
          reportButton.setAttribute("aria-label", `查看报告 ${run.runId}`);
          reportButton.addEventListener("click", () => {
            reportViewer.open(run.runId);
            dom.researchReport.scrollIntoView({ block: "nearest" });
          });
          actions.append(reportButton);
        }
        const resultLink = document.createElement("a");
        resultLink.className = "research-run-action primary";
        resultLink.href = `${API_BASE}/research/runs/${encodeURIComponent(run.runId)}/result/download`;
        resultLink.download = "research-result.json";
        resultLink.textContent = "结果";
        resultLink.title = "下载不可变研究结果 JSON（仅本地缓存）";
        resultLink.addEventListener("click", (event) => event.stopPropagation());
        actions.append(resultLink);
      }
      label.append(actions);
      list.append(label);
    });
    dom.researchRunsList.append(list);
    dom.researchCompare.disabled = state.researchSelected.size < 2;
  }

  function renderSectorTreemap(payload) {
    if (!dom.sectorTreemap) return;
    state.sectorTreemapPayload = payload || null;
    const api = globalThis.quantMonitorTreemap;
    if (!api || typeof api.render !== "function") {
      dom.sectorTreemap.textContent = "行业热力图组件尚未加载";
      return;
    }
    let normalized;
    const treemapQuote = (symbol) => {
      const object = objectFrom(payload);
      const groups = listFrom(object, ["sectors", "industries", "groups", "items"]);
      for (const group of groups) {
        const members = listFrom(group, ["children", "items", "stocks", "quotes", "members"]);
        const found = members.find((item) => String(firstDefined(item, ["symbol", "code", "ticker"], "")) === String(symbol));
        if (found) return normalizeQuote(found);
      }
      const flat = listFrom(object, ["stocks", "quotes", "records"]);
      const found = flat.find((item) => String(firstDefined(item, ["symbol", "code", "ticker"], "")) === String(symbol));
      return found ? normalizeQuote(found) : null;
    };
    try {
      normalized = api.render(dom.sectorTreemap, payload, {
        onSelect: (symbol) => {
          const quote = treemapQuote(symbol);
          if (quote && !state.marketQuotes.some((item) => item.symbol === quote.symbol)) {
            state.marketQuotes = [...state.marketQuotes, quote];
          }
          selectSymbol(symbol);
        },
      });
    } catch (error) {
      dom.sectorTreemap.textContent = "行业行情暂不可用";
      if (dom.sectorTreemapNote) dom.sectorTreemapNote.textContent = `热力图渲染失败：${String(error && error.message || error).slice(0, 140)}`;
      return;
    }
    const sectors = Array.isArray(normalized?.sectors) ? normalized.sectors : [];
    const source = firstDefined(payload, ["source", "provider", "served_from"], "本地缓存");
    const universeCount = finiteNumber(firstDefined(payload, ["universe_count", "universe_total"], null));
    const pricedCount = finiteNumber(firstDefined(payload, ["priced_count", "visible_stock_count"], normalized.total));
    const partial = universeCount !== null && pricedCount !== null && pricedCount < universeCount;
    const servedFrom = String(firstDefined(payload, ["served_from"], normalized.servedFrom || "sqlite_cache"));
    const liveUpdateAt = toDate(firstDefined(payload, ["live_update_at"], null));
    const liveAge = liveUpdateAt ? (Date.now() - liveUpdateAt.getTime()) / 1000 : Infinity;
    const live = liveAge >= 0 && liveAge <= Math.max(15, POLL_INTERVAL / 1000 * 2);
    const freshness = objectFrom(firstDefined(payload, ["freshness"], {}));
    const freshnessState = String(firstDefined(freshness, ["state"], "unknown")).toLowerCase();
    const staleSnapshot = ["stale", "future_timestamp"].includes(freshnessState);
    setPill(
      dom.sectorTreemapSource,
      sectors.length ? String(source) : "等待行业行情",
      sectors.length ? (partial ? "warning" : "ok") : "neutral",
    );
    if (dom.sectorTreemapLive) {
      const label = live
        ? "本地推送实时"
        : staleSnapshot
          ? "缓存已过期"
          : servedFrom.includes("sqlite") ? "SQLite 缓存" : "等待行情";
      setPill(dom.sectorTreemapLive, label, live ? "ok" : staleSnapshot ? "bad" : sectors.length ? "warning" : "neutral");
      dom.sectorTreemapLive.title = live
        ? `最近一条本地 WebSocket 行情：${formatTime(liveUpdateAt, true)}`
        : staleSnapshot
          ? `最新落盘行情已超过新鲜度阈值：${formatTime(firstDefined(freshness, ["as_of"], null), true)}`
          : "电子云图不会因页面刷新直接请求上游；数据来自本地行情缓存";
    }
    if (dom.sectorTreemapCoverage) {
      const coverageText = universeCount === null
        ? `${normalized.total || 0} 只已报价`
        : `行情 ${pricedCount ?? 0} / 目录 ${universeCount}`;
      dom.sectorTreemapCoverage.textContent = coverageText;
      dom.sectorTreemapCoverage.className = `count-badge ${partial ? "warning" : ""}`;
    }
    const asOf = firstDefined(payload, ["as_of", "updated_at", "timestamp"], normalized?.asOf);
    if (dom.sectorTreemapAsOf) dom.sectorTreemapAsOf.textContent = asOf ? `数据时间 ${formatTime(asOf, true)}` : "数据时间 —";
    if (dom.sectorTreemapNote) {
      const sizeSemantics = String(firstDefined(payload, ["size_semantics"], "turnover_preferred") || "turnover_preferred");
      const sizeText = sizeSemantics.includes("turnover") ? "成交额" : sizeSemantics.includes("market_cap") ? "市值" : sizeSemantics.includes("volume_times_price") ? "成交量×价格" : "可用规模";
      const scaleNote = sizeSemantics.includes("market_cap") ? "" : "，不等同于市值";
      const streamNote = live
        ? "本地 WebSocket 行情会更新已显示图块"
        : staleSnapshot
          ? "最新缓存已过期，等待新的本地行情"
          : "当前按 SQLite 缓存快照显示，等待本地行情推送";
      dom.sectorTreemapNote.textContent = sectors.length
        ? `已展示 ${sectors.length} 个行业、${normalized.total || 0} 只个股${universeCount !== null ? `（目录 ${universeCount}，已收到报价 ${pricedCount ?? 0}）` : ""}；面积优先按${sizeText}（缺失时按可用规模${scaleNote}），颜色按涨跌幅。${streamNote}；点击个股可查看下方分析。`
        : "当前没有行业分类或个股行情缓存；页面不会为绘图单独请求上游接口。";
    }
  }

  async function loadSectorTreemap() {
    try {
      const payload = await fetchJSON(`${API_BASE}/market/treemap`);
      renderSectorTreemap(payload);
      return payload;
    } catch (error) {
      renderSectorTreemap({ sectors: [], source: "本地缓存" });
      if (dom.sectorTreemapNote) dom.sectorTreemapNote.textContent = requestErrorMessage(error, "行业热力图接口暂不可用；请确认服务已启动");
      // The map is an optional visualization.  Do not make a healthy quote
      // refresh appear failed when an older/custom backend lacks this route.
      return null;
    }
  }

  async function loadResearchRuns() {
    try {
      const payload = await fetchJSON(`${API_BASE}/research/runs?limit=20&offset=0`);
      renderResearchRuns(listFrom(payload, ["runs", "items"]));
      if (dom.researchRunsNote) dom.researchRunsNote.textContent = "仅读取 SQLite 中已保存的研究运行，不访问行情源；勾选至少两条运行即可比较。";
    } catch (error) {
      if (dom.researchRunsCount) dom.researchRunsCount.textContent = "不可用";
      if (dom.researchRunsNote) dom.researchRunsNote.textContent = "研究运行接口暂不可用；行情与分析页面仍可继续使用。";
      if (dom.researchRunsList) {
        dom.researchRunsList.replaceChildren();
        const empty = document.createElement("div");
        empty.className = "empty-state compact";
        empty.innerHTML = "<span>研究运行读取失败</span><small>请确认本地服务已就绪后刷新</small>";
        dom.researchRunsList.append(empty);
      }
    }
  }

  async function compareSelectedResearchRuns() {
    const ids = [...state.researchSelected];
    if (ids.length < 2 || !dom.researchCompare) return;
    dom.researchCompare.disabled = true;
    dom.researchCompare.classList.add("loading");
    try {
      const payload = await fetchJSON(`${API_BASE}/research/runs/compare?run_ids=${encodeURIComponent(ids.join(","))}`, { timeout: 15000 });
      renderResearchCompare(payload);
    } catch (error) {
      showToast("研究运行比较失败，请检查所选运行是否仍存在", "error");
    } finally {
      dom.researchCompare.classList.remove("loading");
      dom.researchCompare.disabled = state.researchSelected.size < 2;
    }
  }

  async function refreshAll({ notify = false } = {}) {
    if (state.refreshInFlight) {
      // Do not start a second fan-out while one is already collecting the
      // cache-backed panels.  A manual click still gets the same completion
      // promise and therefore remains predictable for callers.
      return state.refreshInFlight;
    }
    const run = (async () => {
      dom.refresh.classList.add("loading");
      dom.refresh.disabled = true;
      let sectorLoaded = false;
      try {
        await loadBankSector();
        sectorLoaded = true;
      } catch (error) {
        // Older/custom backends remain usable. Only fall back when the aggregate
        // bank endpoint is unavailable; generic mode keeps its existing API flow.
        state.bankMode = false;
        document.body.classList.remove("bank-mode");
        state.bankPayload = null;
        state.bankItems = [];
        if (dom.bankToolbar) dom.bankToolbar.hidden = true;
        if (dom.bankQuotaBadge) dom.bankQuotaBadge.hidden = true;
      }
      const tasks = [
        ...(sectorLoaded ? [] : [loadOverview(), loadWatchlist()]),
        loadSectorTreemap(),
        loadAlerts(),
        loadSignals(),
        loadSystem(),
        loadResearchRuns(),
      ];
      const results = await Promise.allSettled(tasks);
      const failures = results.filter((result) => result.status === "rejected");
      if (!sectorLoaded && failures.length === tasks.length) {
        setPill(dom.apiStatus, "本地 API 未连接", "bad");
        dom.lastUpdated.textContent = "本地服务尚未就绪，页面将自动重试";
        if (notify) {
          const firstFailure = failures.find((result) => result.reason);
          showToast(firstFailure ? requestErrorMessage(firstFailure.reason, "无法连接本地数据服务，请确认服务已启动") : "无法连接本地数据服务，请确认服务已启动", "error");
        }
      } else {
        const now = new Date();
        const marketTimes = state.watchlist
          .map((item) => toDate(item.updatedAt))
          .filter(Boolean);
        state.latestMarketTime = marketTimes.length
          ? new Date(Math.max(...marketTimes.map((item) => item.getTime())))
          : null;
        dom.lastUpdated.textContent = state.latestMarketTime
          ? `页面刷新于 ${formatTime(now)} · 最新行情 ${formatTime(state.latestMarketTime, true)}`
          : `页面刷新于 ${formatTime(now)} · 尚未收到行情`;
        if (notify) showToast(failures.length ? `刷新完成，${failures.length} 项暂不可用` : "数据已刷新");
      }
    })();
    state.refreshInFlight = run;
    try {
      return await run;
    } finally {
      if (state.refreshInFlight === run) state.refreshInFlight = null;
      dom.refresh.classList.remove("loading");
      dom.refresh.disabled = false;
    }
  }

  async function collectLatest() {
    if (!dom.collectNow || dom.collectNow.disabled) return;
    dom.collectNow.disabled = true;
    dom.collectNow.classList.add("loading");
    // Keep one key for the whole user-triggered submission, so a network
    // timeout or browser retry cannot enqueue a second provider cycle.
    state.collectionIdempotencyKey = (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function")
      ? globalThis.crypto.randomUUID()
      : `dashboard-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    try {
      const submitted = await fetchJSON(`${API_BASE}/collector/jobs`, {
        method: "POST",
        headers: { "Idempotency-Key": state.collectionIdempotencyKey },
      });
      const jobId = firstDefined(submitted, ["job_id", "id"], null);
      if (!jobId) throw new Error("采集作业未返回 job_id");
      showToast("最新行情采集已提交，正在等待结果");
      let terminal = null;
      for (let attempt = 0; attempt < 60; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, attempt ? 500 : 150));
        terminal = await fetchJSON(`${API_BASE}/collector/jobs/${encodeURIComponent(jobId)}`);
        if (terminal && terminal.terminal) break;
      }
      if (!terminal || !terminal.terminal) {
        showToast("采集仍在后台进行，可稍后查看系统状态", "info");
      } else if (terminal.status === "succeeded") {
        showToast("最新行情采集完成，页面已刷新");
        await refreshAll();
      } else if (terminal.status === "cancelled") {
        showToast("采集已取消", "error");
      } else {
        showToast(`采集未完成：${terminal.error_type || terminal.status || "未知错误"}`, "error");
      }
    } catch (error) {
      if (error && error.status === 409) {
        showToast("已有采集任务运行中，请稍后查看系统状态", "error");
      } else {
        showToast(requestErrorMessage(error, "无法提交采集任务，请确认本地服务已就绪"), "error");
      }
    } finally {
      dom.collectNow.disabled = false;
      dom.collectNow.classList.remove("loading");
    }
  }

  function websocketUrl() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    return `${protocol}//${location.host}/ws/market`;
  }

  function connectWebSocket() {
    if (state.ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(state.ws.readyState)) return;
    window.clearTimeout(state.wsTimer);
    setPill(dom.wsStatus, state.wsRetry ? "本地推送重连中" : "本地推送连接中", "neutral");
    try {
      state.ws = new WebSocket(websocketUrl());
    } catch {
      scheduleReconnect();
      return;
    }
    state.ws.addEventListener("open", () => {
      state.wsRetry = 0;
      setPill(dom.wsStatus, "本地推送已连接", "ok");
      const symbols = state.watchlist.map((item) => item.symbol);
      if (symbols.length) {
        try { state.ws.send(JSON.stringify({ action: "subscribe", symbols })); } catch { /* connection may close concurrently */ }
      }
    });
    state.ws.addEventListener("message", (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      handleRealtimeMessage(message);
    });
    state.ws.addEventListener("close", () => {
      state.ws = null;
      setPill(dom.wsStatus, "本地推送已断开", "bad");
      scheduleReconnect();
    });
    state.ws.addEventListener("error", () => {
      if (state.ws) state.ws.close();
    });
  }

  function scheduleReconnect() {
    window.clearTimeout(state.wsTimer);
    const delay = Math.min(30000, 1000 * 2 ** Math.min(state.wsRetry, 5)) + Math.round(Math.random() * 500);
    state.wsRetry += 1;
    state.wsTimer = window.setTimeout(connectWebSocket, delay);
  }

  function handleRealtimeMessage(message) {
    const type = String(firstDefined(message, ["type", "event", "channel"], "quote")).toLowerCase();
    const payload = firstDefined(message, ["data", "payload"], message);
    if (["ping", "heartbeat"].includes(type)) {
      // This heartbeat proves only that the browser can reach the local
      // FastAPI WebSocket. This proves only the browser-to-local-server channel;
      // provider and collector health remain authoritative.
      if (state.ws?.readyState === WebSocket.OPEN && type === "ping") {
        try { state.ws.send(JSON.stringify({ type: "pong", timestamp: Date.now() })); } catch { /* no-op */ }
      }
      return;
    }
    if (type === "snapshot") {
      const snapshot = payload && typeof payload === "object" ? payload : {};
      const overview = firstDefined(snapshot, ["overview", "market"], snapshot);
      if (overview && typeof overview === "object") {
        applyMarketStatus(firstDefined(overview, ["market_status", "session"]));
        const indices = listFrom(overview, ["indices", "markets"]);
        if (indices.length) renderOverview(indices);
      }
      const watchlistPayload = firstDefined(snapshot, ["watchlist", "quotes"], []);
      const quotes = listFrom(watchlistPayload, ["items", "quotes", "watchlist"]);
      quotes.forEach((quote) => updateTreemapQuote(quote));
      if (!state.bankMode) {
        state.watchlist = quotes.map(normalizeQuote).filter((item) => item.symbol);
        renderWatchlist();
      }
      // Snapshot arrival updates prices only. Historical HTTP calls remain an
      // explicit user selection so a reconnect does not consume daily quota.
      if (state.selectedSymbol) updateSelectedSummary(state.watchlist.find((item) => item.symbol === state.selectedSymbol));
      const alerts = listFrom(firstDefined(snapshot, ["alerts"], []), ["items", "alerts"]);
      if (alerts.length) renderEvents("alert", alerts);
      const signals = listFrom(firstDefined(snapshot, ["signals"], []), ["items", "signals"]);
      if (signals.length) renderEvents("signal", signals);
      const system = firstDefined(snapshot, ["system", "status"]);
      if (system && typeof system === "object") renderSystem(system);
      return;
    }
    if (["alert", "alerts"].includes(type)) {
      listFrom(payload, ["alerts"]).forEach((item) => prependEvent("alert", item));
      if (!Array.isArray(payload) && !listFrom(payload, ["alerts"]).length) prependEvent("alert", payload);
      return;
    }
    if (["signal", "signals", "strategy_signal"].includes(type)) {
      listFrom(payload, ["signals"]).forEach((item) => prependEvent("signal", item));
      if (!Array.isArray(payload) && !listFrom(payload, ["signals"]).length) prependEvent("signal", payload);
      return;
    }
    if (["overview", "index", "market_overview"].includes(type)) {
      const rows = listFrom(payload, ["indices", "overview"]);
      if (rows.length) renderOverview(rows);
      return;
    }
    if (["system", "status", "health"].includes(type)) {
      renderSystem(payload);
      return;
    }
    const quotes = listFrom(payload, ["quotes", "ticks", "items"]);
    if (quotes.length) quotes.forEach(updateQuote);
    else if (payload && typeof payload === "object") updateQuote(payload);
  }

  function updateClock() {
    const now = new Date();
    const clockText = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(now);
    dom.clock.textContent = clockText;
    dom.clock.dateTime = now.toISOString();
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Shanghai", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(now).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
    const minute = Number(parts.hour) * 60 + Number(parts.minute);
    const weekday = parts.weekday;
    const workday = !["Sat", "Sun"].includes(weekday);
    if (state.marketStatus && Date.now() - state.marketStatus.receivedAt < POLL_INTERVAL * 2) {
      const calendarUnknown = state.marketStatus.calendarLookup === "unknown"
        || state.marketStatus.calendar === "weekday_fallback" && !["open", "closed"].includes(state.marketStatus.calendarLookup);
      const status = calendarUnknown ? "warning" : state.marketStatus.trading ? "ok" : ["auction", "preopen", "call_auction", "集合竞价"].includes(state.marketStatus.phase) ? "warning" : "closed";
      setPill(dom.marketBadge, state.marketStatus.label, status);
      if (dom.marketBadge) dom.marketBadge.title = calendarUnknown
        ? "交易日历未覆盖当前日期，市场状态仅按工作日规则估计"
        : "";
      return;
    }
    let text = "休市";
    let status = "closed";
    if (workday && ((minute >= 570 && minute < 690) || (minute >= 780 && minute < 900))) {
      text = "交易中";
      status = "ok";
    } else if (workday && minute >= 555 && minute < 570) {
      text = "集合竞价";
      status = "warning";
    } else if (workday && minute >= 690 && minute < 780) {
      text = "午间休市";
      status = "closed";
    }
    setPill(dom.marketBadge, text, status);
  }

  function bindEvents() {
    // The security-master panel is a separate cache-only module.  Reuse the
    // dashboard's existing selection path so a catalogue click updates the
    // quote, chart and explainable-research panels together.
    window.addEventListener("quant-monitor:select-symbol", (event) => {
      const symbol = event && event.detail ? event.detail.symbol : null;
      if (symbol) selectSymbol(symbol).catch(() => {});
    });
    dom.refresh.addEventListener("click", () => refreshAll({ notify: true }));
    dom.collectNow?.addEventListener("click", collectLatest);
    dom.researchCompare?.addEventListener("click", compareSelectedResearchRuns);
    el("open-settings").addEventListener("click", () => {
      if (typeof dom.settings.showModal === "function") dom.settings.showModal();
      else dom.settings.setAttribute("open", "");
    });
    document.querySelectorAll(".period-button").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.dataset.period === state.period) return;
        state.period = button.dataset.period;
        document.querySelectorAll(".period-button").forEach((item) => item.classList.toggle("active", item === button));
        loadBars().catch(() => {});
      });
    });
    if (dom.bankSearch) {
      dom.bankSearch.addEventListener("input", () => {
        state.bankSearch = dom.bankSearch.value || "";
        renderWatchlist();
      });
    }
    if (dom.bankExchange) {
      dom.bankExchange.addEventListener("change", () => {
        state.bankExchange = dom.bankExchange.value || "all";
        renderWatchlist();
      });
    }
    if (dom.bankSort) {
      dom.bankSort.addEventListener("change", () => {
        state.bankSort = dom.bankSort.value || "change_desc";
        renderWatchlist();
      });
    }
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        updateClock();
        connectWebSocket();
        if (!state.lastApiSuccess || Date.now() - state.lastApiSuccess.getTime() > POLL_INTERVAL) refreshAll();
      }
    });
    window.addEventListener("online", () => { connectWebSocket(); refreshAll(); });
    let treemapResizeTimer = null;
    window.addEventListener("resize", () => {
      window.clearTimeout(treemapResizeTimer);
      treemapResizeTimer = window.setTimeout(() => {
        if (state.sectorTreemapPayload) renderSectorTreemap(state.sectorTreemapPayload);
      }, 150);
    });
    window.addEventListener("offline", () => {
      setPill(dom.apiStatus, "本地 API 不可用", "bad");
      setPill(dom.wsStatus, "本地推送已断开", "bad");
    });
  }

  function init() {
    bindEvents();
    updateClock();
    window.setInterval(updateClock, 1000);
    renderOverview([]);
    refreshAll();
    connectWebSocket();
    state.pollTimer = window.setInterval(() => {
      if (!document.hidden) refreshAll();
    }, POLL_INTERVAL);
  }

  init();
})();
