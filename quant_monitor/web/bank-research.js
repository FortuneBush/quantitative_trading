(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BankResearch = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const FACTOR_LABELS = {
    price_to_sma_120: "价格相对 120 日均线",
    sma_20_to_60: "20 / 60 日均线差",
    momentum_60_5: "60–5 日动量",
    momentum_120_20: "120–20 日动量",
    low_volatility_60: "60 日低波动",
    shallow_drawdown_60: "60 日浅回撤",
  };
  const GATE_LABELS = {
    review_due: "达到定期复核日",
    breadth_ok: "板块宽度达标",
    price_above_sma_120: "价格高于 120 日均线",
    sma_20_above_sma_60: "20 日均线高于 60 日均线",
    momentum_60_5_positive: "60–5 日动量为正",
    volatility_ok: "60 日波动率未超限",
    drawdown_ok: "60 日回撤未超限",
    liquidity_ok: "流动性分位达标",
  };
  const ENTRY_LABELS = {
    review_not_due: "尚未达到定期复核日",
    score_below_entry_percentile: "综合分低于入选阈值",
    data_lineage_incomplete: "数据血缘证据不完整",
  };
  const EXIT_LABELS = {
    risk_limit: "风险限额触发",
    trend_below_sma_60: "趋势跌破 60 日均线",
    score_below_exit_percentile: "综合分低于退出阈值",
  };

  function number(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function text(value, fallback) {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function buildModel(analysis) {
    const data = analysis && typeof analysis === "object" ? analysis : {};
    const weights = data.factor_weights && typeof data.factor_weights === "object" ? data.factor_weights : {};
    const percentiles = data.factor_percentiles && typeof data.factor_percentiles === "object" ? data.factor_percentiles : {};
    const contributions = data.factor_contributions && typeof data.factor_contributions === "object" ? data.factor_contributions : {};
    const keys = Object.keys(FACTOR_LABELS);
    const rows = keys.map((key) => {
      const percentile = number(percentiles[key]);
      const weight = number(weights[key]);
      const contribution = number(contributions[key]);
      const valid = percentile !== null && weight !== null && contribution !== null && percentile >= 0 && percentile <= 1 && weight >= 0 && weight <= 1;
      return { key, label: FACTOR_LABELS[key], percentile, weight, contribution, valid };
    });
    const score = number(data.trend && data.trend.score) ?? number(data.recommendation && data.recommendation.score);
    const overlay = data.fundamental_overlay && typeof data.fundamental_overlay === "object" ? data.fundamental_overlay : {};
    const overlayEnabled = overlay.enabled === true && overlay.status === "applied";
    const overlayWeight = number(overlay.weight);
    const overlayScore = number(overlay.score);
    const overlayContribution = number(overlay.contribution);
    const priceScore = number(data.price_score);
    const weightsValid = Math.abs(rows.reduce((sum, row) => sum + (row.weight || 0), 0) - 1) < 1e-8;
    const contributionsValid = rows.every((row) => row.valid && Math.abs(row.contribution - row.percentile * row.weight) < 1e-8);
    const priceContributionSum = rows.reduce((sum, row) => sum + (row.contribution || 0), 0);
    const scoreValid = overlayEnabled
      ? score !== null && priceScore !== null && overlayWeight !== null && overlayScore !== null && overlayContribution !== null
        && overlayWeight >= 0 && overlayWeight < 1
        && Math.abs(priceContributionSum - priceScore) < 1e-8
        && Math.abs(overlayContribution - overlayScore * overlayWeight) < 1e-8
        && Math.abs(score - (priceScore * (1 - overlayWeight) + overlayContribution)) < 1e-8
      : score !== null && Math.abs(priceContributionSum - score) < 1e-8;
    const complete = rows.length > 0 && weightsValid && contributionsValid && scoreValid;
    const thresholds = data.score_thresholds && typeof data.score_thresholds === "object" ? data.score_thresholds : {};
    const entry = number(thresholds.entry);
    const exit = number(thresholds.exit);
    const gates = data.gates && typeof data.gates === "object" ? data.gates : {};
    const failures = Array.isArray(data.gate_failures) ? data.gate_failures.map(String) : [];
    const entryFailures = Array.isArray(data.entry_failure_reasons) ? data.entry_failure_reasons.map(String) : [];
    const exitReasons = Array.isArray(data.exit_reasons) ? data.exit_reasons.map(String) : [];
    return {
      available: complete,
      rows,
      score,
      scorePercent: score === null ? null : score * 100,
      entryPercent: entry === null ? null : entry * 100,
      exitPercent: exit === null ? null : exit * 100,
      gates,
      failures,
      entryFailures,
      exitReasons,
      overlayEnabled,
      overlayWeight,
      overlayScore,
      overlayContribution,
      overlayReason: text(overlay.reason, "未使用基本面叠加"),
      modelVersion: text(data.model_version, "未提供"),
      interval: text(data.interval, "1d"),
      note: "贡献分 = 同业分位 × 权重 × 100；综合分是研究排序指标，不是收益率或上涨概率。",
    };
  }

  function render(container, analysis, ownerDocument) {
    if (!container) return buildModel(analysis);
    const doc = ownerDocument || container.ownerDocument || document;
    const model = buildModel(analysis);
    while (container.firstChild) container.removeChild(container.firstChild);
    const details = doc.createElement("details");
    details.className = "factor-breakdown";
    details.open = false;
    const summary = doc.createElement("summary");
    summary.textContent = model.available ? "评分拆解与筛选条件" : "评分拆解（数据不足）";
    details.appendChild(summary);
    const body = doc.createElement("div");
    body.className = "factor-breakdown-body";
    if (!model.available) {
      const unavailable = doc.createElement("p");
      unavailable.className = "factor-empty";
      unavailable.textContent = "服务端尚未提供完整的同业分位、权重和贡献数据，不能臆造评分拆解。";
      body.appendChild(unavailable);
    } else {
      const table = doc.createElement("table");
      table.className = "factor-table";
      const head = doc.createElement("thead");
      const headRow = doc.createElement("tr");
      ["因子", "同业分位", "权重", "贡献分"].forEach((label) => { const cell = doc.createElement("th"); cell.scope = "col"; cell.textContent = label; headRow.appendChild(cell); });
      head.appendChild(headRow); table.appendChild(head);
      const tbody = doc.createElement("tbody");
      model.rows.forEach((row) => {
        const tr = doc.createElement("tr");
        [row.label, `${(row.percentile * 100).toFixed(1)}%`, `${(row.weight * 100).toFixed(1)}%`, `${(row.contribution * 100).toFixed(2)}`].forEach((value) => { const cell = doc.createElement("td"); cell.textContent = value; tr.appendChild(cell); });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody); body.appendChild(table);
      const total = doc.createElement("p"); total.className = "factor-total"; total.textContent = `综合分 ${model.scorePercent === null ? "—" : model.scorePercent.toFixed(2)} · 入选 ≥ ${model.entryPercent === null ? "—" : model.entryPercent.toFixed(1)} · 定期退出 < ${model.exitPercent === null ? "—" : model.exitPercent.toFixed(1)}`; body.appendChild(total);
      const overlayText = doc.createElement("p"); overlayText.className = "factor-meta";
      overlayText.textContent = model.overlayEnabled
        ? `基本面叠加已启用：基本面分 ${(model.overlayScore * 100).toFixed(1)} · 权重 ${(model.overlayWeight * 100).toFixed(1)}% · 贡献 ${(model.overlayContribution * 100).toFixed(2)}`
        : `基本面叠加未启用：${model.overlayReason}，当前综合分保持价格策略结果`;
      body.appendChild(overlayText);
    }
    const meta = doc.createElement("p"); meta.className = "factor-meta"; meta.textContent = `${model.note} 模型 ${model.modelVersion} · ${model.interval} 未复权日线，仅供研究观察。`; body.appendChild(meta);
    const gateTitle = doc.createElement("h4"); gateTitle.textContent = "入场门控"; body.appendChild(gateTitle);
    const gateList = doc.createElement("ul"); gateList.className = "factor-gates";
    const gateEntries = Object.keys(model.gates);
    if (!gateEntries.length && !model.failures.length) { const li = doc.createElement("li"); li.textContent = "服务端未提供门控状态"; gateList.appendChild(li); }
    gateEntries.forEach((key) => { const li = doc.createElement("li"); li.textContent = `${GATE_LABELS[key] || key}：${model.gates[key] === true ? "通过" : "未通过"}`; if (model.gates[key] !== true) li.className = "failed"; gateList.appendChild(li); });
    if (model.failures.length) { const li = doc.createElement("li"); li.className = "failed"; li.textContent = `失败原因：${model.failures.map((key) => GATE_LABELS[key] || key).join("、")}`; gateList.appendChild(li); }
    if (model.entryFailures.length) { const li = doc.createElement("li"); li.className = "failed"; li.textContent = `入选条件未满足：${model.entryFailures.map((key) => ENTRY_LABELS[key] || key).join("、")}`; gateList.appendChild(li); }
    body.appendChild(gateList);
    if (model.exitReasons.length) { const exits = doc.createElement("p"); exits.className = "factor-exits"; exits.textContent = `退出原因：${model.exitReasons.map((key) => EXIT_LABELS[key] || key).join("、")}`; body.appendChild(exits); }
    details.appendChild(body); container.appendChild(details);
    return model;
  }

  const FUNDAMENTAL_METRICS = {
    pb: ["市净率 PB", "ratio"],
    roe: ["净资产收益率 ROE", "percent"],
    dividend_yield: ["股息率", "percent"],
    npl_ratio: ["不良贷款率", "percent"],
    provision_coverage: ["拨备覆盖率", "percent"],
    capital_adequacy: ["资本充足率", "percent"],
    cost_income_ratio: ["成本收入比", "percent"],
  };
  const FUNDAMENTAL_WARNINGS = {
    fundamental_observations_unavailable: "本地尚无基本面事实",
    fundamental_cross_section_too_small: "股票池不足两只，不能计算同业分位",
    fundamental_sources_mixed: "股票池内来源不一致",
    fundamental_metric_coverage_incomplete: "股票池内仍有缺失或单位不兼容指标",
    fundamental_scopes_incomparable: "单体 / 合并口径不一致或未知",
    fundamental_periods_incomparable: "同一指标的报告期不一致",
    fundamental_selection_ambiguous: "基本面修订或口径冲突，无法选择事实",
  };

  function buildFundamentalModel(record, symbol) {
    const matches = record && typeof record === "object" && record.symbol === symbol;
    const data = matches ? record : {};
    const list = (key) => Array.isArray(data[key]) ? data[key].map(String) : [];
    const rows = Object.entries(FUNDAMENTAL_METRICS).map(([key, spec]) => {
      const fact = data.evidence && data.evidence[key];
      const evidence = fact && typeof fact === "object" ? fact : {};
      const raw = data.values && data.values[key];
      const numeric = (typeof raw === "string" && /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(raw)) || typeof raw === "number";
      const hasValue = numeric && Number.isFinite(Number(raw));
      const reasons = [];
      if (!hasValue || list("missing_metrics").includes(key)) reasons.push("缺失");
      if (list("invalid_unit_metrics").includes(key) || (hasValue && evidence.unit !== spec[1])) reasons.push("单位不兼容");
      if (list("invalid_scope_metrics").includes(key) || (hasValue && !["consolidated", "standalone"].includes(evidence.scope))) reasons.push("口径未知");
      if (list("incomparable_period_metrics").includes(key)) reasons.push("报告期不可比");
      const evidenceComplete = ["observation_id", "content_hash", "period_start", "period_end", "available_at", "received_at", "source", "revision_id"].every((name) => typeof evidence[name] === "string" && evidence[name].length > 0);
      if (hasValue && !evidenceComplete) reasons.push("证据不完整");
      const unit = evidence.unit === "percent" ? "%" : evidence.unit === "ratio" ? "倍" : text(evidence.unit, "单位未知");
      return { key, label: spec[0], value: hasValue ? `${raw} ${unit}` : "—", evidence, reasons, complete: hasValue && evidenceComplete && reasons.length === 0 };
    });
    const score = number(data.score);
    const warnings = list("warnings");
    const integrated = data.strategy_integration === true;
    const available = data.score_status === "complete" && score !== null && score >= 0 && score <= 1
      && data.source_conflict === false && data.scope_conflict === false
      && (data.score_usage === "parallel_research_only" || integrated)
      && warnings.length === 0 && rows.every((row) => row.complete);
    return {
      symbol: symbol || "未选择", rows, available, score: available ? score * 100 : null,
      decisionAt: text(data.decision_at, "未提供"),
      warnings: warnings.map((key) => FUNDAMENTAL_WARNINGS[key] || key),
      notice: integrated
        ? "基本面事实已按当前策略配置作为独立叠加层使用；仍是研究信号，不构成投资建议或交易指令。按公开可用时间截断，不代表本机当时已收到；新鲜度及来源真实性未认证。"
        : "基本面为独立研究视图，未纳入价格策略评分或买卖信号；不构成投资建议。按公开可用时间截断，不代表本机当时已收到；新鲜度及来源真实性未认证。",
    };
  }

  function renderFundamentals(container, record, symbol, ownerDocument) {
    const model = buildFundamentalModel(record, symbol);
    if (!container) return model;
    const doc = ownerDocument || container.ownerDocument || document;
    const expanded = container.querySelector?.("details")?.open === true;
    container.replaceChildren();
    function add(parent, tag, value, className) {
      const node = doc.createElement(tag);
      if (value !== null) node.textContent = value;
      if (className) node.className = className;
      parent.appendChild(node);
      return node;
    }
    const details = add(container, "details", null);
    details.open = expanded;
    add(details, "summary", `银行基本面 · ${model.symbol} · ${model.available ? `独立研究分 ${model.score.toFixed(1)}` : "评分不可用"}`);
    add(details, "p", model.available ? `独立基本面研究分 ${model.score.toFixed(1)} / 100（不是收益率）` : "基本面评分不可用 · 当前未形成完整可比横截面", "factor-meta");
    add(details, "p", `事实可用时间截止：${model.decisionAt}`, "factor-meta");
    const grid = add(details, "div", null, "fundamental-grid");
    model.rows.forEach((row) => {
      const cell = add(grid, "section", null, "fundamental-metric");
      add(cell, "h4", row.label);
      add(cell, "strong", row.value);
      if (row.reasons.length) add(cell, "p", row.reasons.join(" · "), "warning");
      if (row.evidence.observation_id) {
        const details = add(cell, "details", null);
        add(details, "summary", "查看事实证据");
        const fact = row.evidence;
        const scope = { consolidated: "合并口径", standalone: "单体口径", unknown: "口径未知" }[fact.scope] || "口径未知";
        add(details, "p", `${text(fact.source, "来源未知")} · ${scope}`);
        add(details, "p", `期间：${text(fact.period_start, "—")} 至 ${text(fact.period_end, "—")}`);
        add(details, "p", `可用时间：${text(fact.available_at, "—")}`);
        add(details, "p", `本机接收：${text(fact.received_at, "—")}`);
        add(details, "p", `观测 ${fact.observation_id} · 修订 ${text(fact.revision_id, "—")}`);
        add(details, "p", `内容哈希 ${text(fact.content_hash, "—")}`);
      }
    });
    if (model.warnings.length) add(details, "p", model.warnings.join("；"), "factor-meta warning");
    add(container, "p", model.notice, "factor-meta");
    return model;
  }

  return { buildModel, render, factorLabels: FACTOR_LABELS, buildFundamentalModel, renderFundamentals };
});
