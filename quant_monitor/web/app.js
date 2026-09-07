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
    requestSerial: 0,
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
    bankQuotaBadge: el("bank-quota-badge"),
    analysisName: el("analysis-name"),
    analysisSymbol: el("analysis-symbol"),
    analysisStatus: el("analysis-status"),
    analysisVerdict: el("analysis-verdict-value"),
    analysisVerdictNote: el("analysis-verdict-note"),
    analysisScore: el("analysis-score-value"),
    analysisRisk: el("analysis-risk-value"),
    analysisRiskNote: el("analysis-risk-note"),
    analysisMetrics: el("analysis-metrics"),
    analysisNote: el("analysis-note"),
    analysisEvidence: el("analysis-evidence"),
    analysisRisks: el("analysis-risks"),
    analysisInvalidations: el("analysis-invalidations"),
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
    settings: el("settings-dialog"),
    toastRegion: el("toast-region"),
  };

  function firstDefined(object, keys, fallback = null) {
    if (!object || typeof object !== "object") return fallback;
    for (const key of keys) {
      if (object[key] !== undefined && object[key] !== null) return object[key];
    }
    return fallback;
  }

  function finiteNumber(value) {
    if (value === "" || value === null || value === undefined) return null;
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

  function showToast(message, type = "info") {
    if (!dom.toastRegion) return;
    const toast = document.createElement("div");
    toast.className = `toast ${type === "error" ? "error" : ""}`;
    toast.textContent = message;
    dom.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), 4200);
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
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      state.lastApiSuccess = new Date();
      setPill(dom.apiStatus, "本地 API 已连接", "ok");
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function normalizeQuote(raw) {
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
      raw,
    };
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
    setPill(dom.marketSource, sourceName ? String(sourceName) : "未配置", receivedCount > 0 ? "ok" : "warning");
    const universeScope = String(firstDefined(object, ["universe_scope"], "provider_catalogue"));
    const configuredScope = universeScope === "configured_symbols";
    if (dom.marketTitle) dom.marketTitle.textContent = state.bankMode ? "银行板块概览" : configuredScope ? "自选行情范围" : "沪 A 全市场";
    if (dom.marketInsightsNote) {
      const asOf = firstDefined(object, ["as_of", "timestamp", "updated_at"], summary.asOf);
      const unpricedNote = summary.unpriced ? ` · ${summary.unpriced} 只未收到行情` : "";
      const scopeNote = state.bankMode ? "银行分类与排名来自服务端聚合缓存，本地筛选排序不消耗额度" : configuredScope ? "当前看板仅展示已配置自选标的，以节省每日 API 额度" : "当前数据源提供完整证券目录";
      const breadthNote = summary.priced === 0 ? " · 日线参考尚未加载，涨跌统计暂不可用" : "";
      const sessionNote = " · 非交易时段为上一交易日收盘快照";
      dom.marketInsightsNote.textContent = asOf
        ? `行情时间 ${formatTime(asOf, true)}${unpricedNote} · ${scopeNote}${breadthNote}${sessionNote}`
        : `${scopeNote}；尚未收到行情。${breadthNote}${sessionNote}`;
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

  function renderAnalysis(payload) {
    const data = normalizeAnalysis(payload);
    state.analysis = data;
    const marketName = state.marketQuotes.find((item) => item.symbol === data.symbol)?.name;
    if (dom.analysisName) dom.analysisName.textContent = (data.name && data.name !== data.symbol ? data.name : marketName) || data.symbol || "选择股票";
    if (dom.analysisSymbol) dom.analysisSymbol.textContent = data.symbol || "—";
    const quality = data.dataQuality && typeof data.dataQuality === "object" ? data.dataQuality : {};
    const qualityStatus = String(firstDefined(quality, ["status"], "ok"));
    const statusText = qualityStatus === "ok" ? "分析就绪" : qualityStatus === "stale" ? "数据偏旧" : qualityStatus === "partial" ? "缓存分析" : "数据不足";
    setPill(dom.analysisStatus, statusText, qualityStatus === "ok" ? "ok" : qualityStatus === "stale" || qualityStatus === "partial" ? "warning" : "neutral");
    const recommendation = data.recommendation || {};
    const action = String(firstDefined(recommendation, ["action"], ""));
    const actionLabels = { buy: "偏强研究", sell: "偏弱研究", hold: "中性观察", positive_bias: "偏强研究", negative_bias: "偏弱研究", risk_caution: "风险观察", neutral: "中性观察", insufficient_data: "数据不足" };
    const research = researchPresentation(action, firstDefined(recommendation, ["label"], null));
    if (dom.analysisVerdict) {
      dom.analysisVerdict.textContent = actionLabels[action] || research.label;
      dom.analysisVerdict.className = research.className;
    }
    if (dom.analysisVerdictNote) {
      const reason = firstDefined(recommendation, ["reason", "description"], "基于趋势、动量与风险指标");
      dom.analysisVerdictNote.textContent = Array.isArray(reason) ? reason.filter(Boolean).slice(0, 2).join(" · ") : String(reason);
    }
    const trendScore = finiteNumber(firstDefined(data.trend, ["score"], null));
    if (dom.analysisScore) dom.analysisScore.textContent = trendScore === null ? "—" : `${trendScore > 0 ? "+" : ""}${(trendScore * 100).toFixed(0)}`;
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
    const evidence = firstDefined(recommendation, ["evidence", "reasons", "reason"], [
      firstDefined(data.trend, ["description"], "趋势证据暂不可用"),
      firstDefined(data.momentum, ["description"], "动量证据暂不可用"),
    ]);
    const risks = firstDefined(recommendation, ["risks", "risk_factors"], [firstDefined(data.risk, ["description"], "风险指标暂不可用")]);
    const invalidations = firstDefined(recommendation, ["invalidation_conditions", "invalidations", "exit_conditions"], firstDefined(data, ["invalidation_conditions", "invalidations"], null));
    renderDetailList(dom.analysisEvidence, evidence, "当前缓存未提供可复核的研究证据");
    renderDetailList(dom.analysisRisks, risks, "风险指标或历史样本不足");
    renderDetailList(dom.analysisInvalidations, invalidations, "缓存策略尚未提供明确失效条件，不应据此形成交易指令");
    if (dom.analysisNote) {
      const qualityNote = barsUsed !== null && barsUsed !== undefined
        ? `使用 ${barsUsed}${barsRequired ? ` / 至少 ${barsRequired}` : ""} 根 K 线 · 数据状态 ${qualityStatus} · 仅供研究观察，不构成投资建议。`
        : "当前仅展示服务端已有缓存分析；不会自动逐股请求历史数据，不构成投资建议。";
      dom.analysisNote.textContent = qualityNote;
    }
  }

  function renderAnalysisUnavailable(bankItem = null) {
    const quote = bankItem || {};
    if (dom.analysisName) dom.analysisName.textContent = quote.name || "选择银行股";
    if (dom.analysisSymbol) dom.analysisSymbol.textContent = quote.symbol || "—";
    setPill(dom.analysisStatus, quote.symbol ? "缓存分析不足" : "等待选择", "neutral");
    if (dom.analysisVerdict) {
      dom.analysisVerdict.textContent = "数据不足";
      dom.analysisVerdict.className = "";
    }
    if (dom.analysisVerdictNote) dom.analysisVerdictNote.textContent = "板块页不会为每只银行自动请求日线";
    if (dom.analysisScore) dom.analysisScore.textContent = "—";
    if (dom.analysisRisk) {
      dom.analysisRisk.textContent = "—";
      dom.analysisRisk.className = "";
    }
    if (dom.analysisRiskNote) dom.analysisRiskNote.textContent = "等待完整历史样本";
    if (dom.analysisMetrics) [...dom.analysisMetrics.querySelectorAll("strong")].forEach((node) => { node.textContent = "—"; });
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
    const advancers = finiteNumber(firstDefined(summary, ["advancers", "advancing"], state.bankItems.filter((item) => item.changePct !== null && item.changePct > 0).length));
    const decliners = finiteNumber(firstDefined(summary, ["decliners", "declining"], state.bankItems.filter((item) => item.changePct !== null && item.changePct < 0).length));
    const median = finiteNumber(firstDefined(summary, ["median_pct_change", "median_change_pct"], null));
    const limit = finiteNumber(firstDefined(quota, ["soft_limit", "limit", "daily_limit", "total"], null));
    const used = finiteNumber(firstDefined(quota, ["used", "requests_used", "consumed"], null));
    let remaining = finiteNumber(firstDefined(quota, ["remaining_soft", "remaining", "available"], null));
    if (remaining === null && limit !== null && used !== null) remaining = Math.max(0, limit - used);
    const cards = [
      { name: "银行股覆盖", symbol: "COVERAGE", metric: `${received ?? 0} / ${total ?? state.bankItems.length}`, secondary: `缺失 ${Math.max(0, (total ?? 0) - (received ?? 0))} 只` },
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
    state.marketStatus = { trading: trading === true, phase, label, receivedAt: Date.now() };
    const status = trading === true ? "ok" : ["auction", "preopen", "call_auction", "集合竞价"].includes(phase) ? "warning" : "closed";
    setPill(dom.marketBadge, label, status);
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
      row.append(identity, price, changeCell, amount, researchCell);
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

  function updateQuote(raw) {
    const incoming = normalizeQuote(raw);
    if (!incoming.symbol) return;
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
    if (healthy) {
      const cacheNote = cacheHit === true || cacheState === "hit" ? " · 本次命中本地缓存" : "";
      const capabilityNote = isAShareHub && !hasOrderBook ? " · 无五档盘口" : "";
      dom.heartbeatText.textContent = `最近心跳 ${formatTime(heartbeat || Date.now())}${lastProviderSuccess ? ` · 上游更新 ${formatTime(lastProviderSuccess, true)}` : ""}${cacheNote}${capabilityNote}`;
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
      dom.marketInsightsNote.textContent = `${asOf ? `数据时间 ${formatTime(asOf, true)} · ` : ""}${notice}${remaining !== null ? ` · 本机估算额度 ${remaining}/${limit ?? "—"}` : ""} · 页面筛选与排序不调用上游 API。`;
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
      renderCharts();
    } catch (error) {
      if (serial !== state.requestSerial) return;
      state.bars = [];
      renderCharts();
      dom.chartEmpty.querySelector("strong").textContent = "走势数据暂不可用";
      dom.chartEmpty.querySelector("small").textContent = error.name === "AbortError" ? "请求超时，请稍后重试" : "等待本地数据服务提供历史行情";
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
      setPill(dom.analysisStatus, "分析不可用", "bad");
      if (dom.analysisNote) dom.analysisNote.textContent = error.name === "AbortError" ? "分析请求超时，请稍后重试。" : "历史行情不足或数据源暂不可用，暂不生成分析结论。";
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

  async function refreshAll({ notify = false } = {}) {
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
      loadAlerts(),
      loadSignals(),
      loadSystem(),
    ];
    const results = await Promise.allSettled(tasks);
    const failures = results.filter((result) => result.status === "rejected");
    dom.refresh.classList.remove("loading");
    dom.refresh.disabled = false;
    if (!sectorLoaded && failures.length === tasks.length) {
      setPill(dom.apiStatus, "本地 API 未连接", "bad");
      dom.lastUpdated.textContent = "本地服务尚未就绪，页面将自动重试";
      if (notify) showToast("无法连接本地数据服务，请确认服务已启动", "error");
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
      const status = state.marketStatus.trading ? "ok" : ["auction", "preopen", "call_auction", "集合竞价"].includes(state.marketStatus.phase) ? "warning" : "closed";
      setPill(dom.marketBadge, state.marketStatus.label, status);
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
    dom.refresh.addEventListener("click", () => refreshAll({ notify: true }));
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
