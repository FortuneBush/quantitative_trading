(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.ResearchReport = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Governance is deliberately fetched alongside the immutable artifacts.  It
  // is a cache-only evidence report (not an approval signal), but hiding it
  // made the report UI look more complete than the server-side gate really was.
  const PARTS = ["result", "risk", "integrity", "governance"];
  const LABELS = {
    result: "研究结果", risk: "风险统计", integrity: "指标一致性审计", governance: "研究治理审计",
  };
  const WARNINGS = {
    missing_or_invalid_return_observations: "收益观测缺失或非法，尾部风险不可评估",
    insufficient_return_observations_for_historical_tail_metrics: "有效收益样本不足",
    missing_or_invalid_ending_nav: "结束净值缺失或非法",
    missing_or_invalid_ending_cash: "结束现金缺失或非法",
    missing_or_invalid_position_market_values: "持仓估值缺失或非法，暴露不可评估",
    missing_position_data: "持仓明细缺失，暴露与集中度不可评估",
    invalid_total_position_market_value: "持仓总估值无法校验，暴露不可评估",
    top_position_exceeds_25_percent_of_ending_nav: "最大单票权重超过净值的 25%",
  };
  const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const list = (value) => Array.isArray(value) ? value.map(String) : [];
  function number(value) {
    if (typeof value !== "number" && (typeof value !== "string" || !value.trim())) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  function format(value, kind = "percent") {
    const parsed = number(value);
    if (parsed === null) return "—";
    if (kind === "count") return parsed.toLocaleString("zh-CN");
    if (kind === "ratio") return parsed.toFixed(2);
    if (kind === "money" || kind === "signed_money") {
      const amount = (kind === "signed_money" ? Math.abs(parsed) : parsed).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      return kind === "signed_money" ? `${parsed >= 0 ? "+" : "-"}${amount}` : amount;
    }
    return `${(parsed * 100).toFixed(2)}%`;
  }

  // Older immutable result artifacts do not have a realized_pnl aggregate in
  // metrics, while their trade ledger still records it on completed sells.
  // Derive the aggregate only when every observed value is numeric; partial or
  // malformed ledgers remain unavailable instead of being silently treated as
  // zero.
  function realizedPnlFromLedger(result, metrics) {
    // An explicitly unknown/malformed aggregate is not an invitation to
    // replace the supplied value with a plausible ledger-derived number.
    if (Object.prototype.hasOwnProperty.call(metrics, "realized_pnl")) return number(metrics.realized_pnl);
    if (!Array.isArray(result.trades) || !result.trades.length) return null;
    const trades = result.trades;
    const declaredTrades = number(metrics.trade_count);
    if (metrics.trade_count !== undefined && (!Number.isSafeInteger(declaredTrades) || declaredTrades !== trades.length)) return null;
    let total = 0;
    let closed = 0;
    for (const trade of trades) {
      if (!trade || typeof trade !== "object" || Array.isArray(trade)) return null;
      if (trade.action === "buy") {
        if (trade.realized_pnl !== null && trade.realized_pnl !== undefined) return null;
      } else if (trade.action === "sell") {
        const value = number(trade.realized_pnl);
        if (value === null) return null;
        total += value;
        ++closed;
      } else return null;
    }
    const declaredClosed = number(metrics.closed_trade_count);
    if (metrics.closed_trade_count !== undefined && (!Number.isSafeInteger(declaredClosed) || declaredClosed !== closed)) return null;
    return closed > 0 && Number.isFinite(total) ? total : null;
  }

  function tradeStatistics(metrics) {
    const closed = number(metrics.closed_trade_count);
    const losses = number(metrics.losing_trade_count);
    const grossLoss = number(metrics.gross_loss);
    const status = metrics.trade_statistics_status;
    const factorStatus = metrics.profit_factor_status;
    const noClosed = status === "no_closed_trades" || closed === 0;
    const available = !noClosed && Number.isSafeInteger(closed) && closed > 0
      && (status === undefined || status === "available");
    const noLosses = available && (factorStatus === "no_losses" || losses === 0 || grossLoss === 0);
    const factorAvailable = available && !noLosses && Number.isSafeInteger(losses) && losses > 0
      && grossLoss !== null && grossLoss > 0 && (factorStatus === undefined || factorStatus === "available");
    return {
      available, factorAvailable,
      notice: noClosed ? "没有已平仓交易，胜率与单笔期望收益不可评估。"
        : !available ? "缺少完整平仓样本信息，胜率与单笔期望收益不可评估。" : null,
      factorNotice: noClosed || factorStatus === "no_closed_trades" ? "没有已平仓交易，不能计算有限盈亏比。"
        : noLosses ? "无亏损样本，不能计算有限盈亏比。"
          : !factorAvailable ? "缺少完整亏损样本信息，不能计算有限盈亏比。" : null,
    };
  }

  function buildModel(runId, responses) {
    const values = object(responses);
    const errors = {};
    const accepted = {};
    const resultResponse = object(values.result);
    const resultEnvelope = object(resultResponse.payload);
    const hash = resultResponse.ok && resultEnvelope.run_id === runId && typeof resultEnvelope.result_hash === "string"
      ? resultEnvelope.result_hash : null;
    PARTS.forEach((part) => {
      const response = object(values[part]);
      const payload = object(response.payload);
      if (response.ok !== true) errors[part] = response.error || "unavailable";
      else if (payload.run_id !== runId) errors[part] = "run_id_mismatch";
      // A run without a result has no result hash yet; governance can still
      // show manifest/lineage evidence in that case. Once a result exists,
      // governance must attest the exact same immutable result hash.
      else if (part !== "governance" && (!hash || payload.result_hash !== hash)) errors[part] = "result_hash_mismatch";
      else if (part === "governance" && hash && payload.result_hash !== hash) errors[part] = "result_hash_mismatch";
      else if (part !== "result" && (payload.served_from !== "sqlite_cache" || payload.upstream_requests !== 0)) errors[part] = "provenance_unverified";
      else if (part !== "governance" && (!payload[part] || typeof payload[part] !== "object" || Array.isArray(payload[part]))) errors[part] = "invalid_payload";
      else accepted[part] = part === "governance" ? payload : payload[part];
    });
    const result = object(accepted.result);
    const risk = object(accepted.risk);
    const integrity = object(accepted.integrity);
    const governance = object(accepted.governance);
    const metrics = { ...object(result.metrics) };
    const realizedPnl = realizedPnlFromLedger(result, metrics);
    metrics.realized_pnl = realizedPnl;
    const tradeStats = tradeStatistics(metrics);
    if (!tradeStats.available) {
      metrics.win_rate = null;
      metrics.expectancy_per_closed_trade = null;
    }
    if (!tradeStats.factorAvailable) metrics.profit_factor = null;
    const checks = object(integrity.checks);
    const mismatches = list(integrity.mismatches);
    const unavailableChecksRaw = integrity.unavailable_checks;
    const unavailableChecks = list(unavailableChecksRaw);
    const unavailableChecksValid = Array.isArray(unavailableChecksRaw)
      && unavailableChecksRaw.every((item) => typeof item === "string" && item.trim().length > 0);
    const integrityWarnings = list(integrity.warnings);
    const tradeStatisticsStatus = typeof integrity.trade_statistics_status === "string"
      ? integrity.trade_statistics_status : null;
    const missingReturns = number(risk.missing_return_observations);
    const observations = number(risk.return_observations);
    const minimum = number(risk.minimum_observations);
    const sampleReady = !!accepted.risk && risk.sample_sufficient === true && missingReturns === 0
      && observations !== null && minimum !== null && observations >= minimum && minimum > 0;
    const verified = !!accepted.integrity && integrity.verified === true && mismatches.length === 0
      && Object.keys(checks).length > 0 && Object.values(checks).every((value) => value === true);
    // `verified` retains the historical checks-only meaning for compatibility.
    // Coverage is a stricter, explicit claim: the newer integrity report must
    // attest trade statistics and report no uncovered checks. Legacy reports
    // therefore remain auditable but are never labelled fully covered.
    const coverageComplete = verified && tradeStatisticsStatus === "verified"
      && unavailableChecksValid && unavailableChecks.length === 0;
    const coveragePartial = verified && !coverageComplete && tradeStatisticsStatus !== "failed";
    const universeBinding = object(governance.universe_binding);
    const governanceWarnings = list(governance.warnings);
    const governanceChecks = object(governance.checks);
    const replayAudit = object(governance.replay_audit);
    // Do not trust the aggregate `reproducible` label alone: the report must
    // expose an explicit replay check before the UI describes a replay as
    // verified.  This keeps a malformed/forged aggregate fail-closed.
    const governanceReplayVerified = governanceChecks.replay_verified === true
      || replayAudit.verified === true;
    const governanceArtifactsConsistent = governance.artifacts_consistent === true;
    const governanceDecisionReady = governance.decision_ready === true;
    const governanceStatus = !accepted.governance ? "unavailable"
      : governanceDecisionReady ? "decision_ready"
        : governanceReplayVerified ? "replay_verified"
          : governanceArtifactsConsistent ? "artifacts_consistent" : "evidence_incomplete";
    return {
      runId, hash, errors, result, metrics, risk, integrity, sampleReady, verified, tradeStats,
      hasResult: !!accepted.result, hasRisk: !!accepted.risk, hasIntegrity: !!accepted.integrity,
      hasGovernance: !!accepted.governance, governance, universeBinding, governanceWarnings,
      governanceChecks, governanceReplayVerified, governanceArtifactsConsistent,
      governanceDecisionReady, governanceStatus,
      checks, mismatches, unavailableChecks, unavailableChecksValid, integrityWarnings, tradeStatisticsStatus,
      coverageComplete, coveragePartial,
      warnings: [...new Set([...list(object(result.diagnostics).warnings), ...list(risk.warnings)])],
      // Never let a serialized flag turn a calculation audit into trading approval.
      notice: "只读本地研究制品；指标一致不代表数据权威、策略有效或可交易。未复权、交易日历、股票池及成交假设仍须独立核验。",
    };
  }

  function render(container, state) {
    if (!container) return;
    const doc = container.ownerDocument;
    container.replaceChildren();
    container.hidden = state.phase === "idle";
    container.setAttribute("aria-busy", String(state.phase === "loading"));
    if (container.hidden) return;
    const add = (parent, tag, text, cls) => {
      const node = doc.createElement(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = text;
      parent.appendChild(node);
      return node;
    };
    const header = add(container, "header", undefined, "research-report-header");
    const title = add(header, "h3", `研究报告 · ${state.runId}`);
    title.tabIndex = -1;
    if (state.phase === "loading" && typeof title.focus === "function") title.focus({ preventScroll: true });
    const actions = add(header, "div", undefined, "research-run-actions");
    if (state.phase !== "loading") {
      const retry = add(actions, "button", "重新读取", "research-run-action");
      retry.type = "button"; retry.addEventListener("click", state.retry);
    }
    const close = add(actions, "button", "关闭报告", "research-run-action");
    close.type = "button"; close.addEventListener("click", state.close);
    if (state.phase === "loading") {
      add(container, "p", "正在读取本地结果、风险、审计与治理报告…不请求行情源。", "panel-note");
      return;
    }
    const model = state.model;
    add(container, "p", model.notice, "research-report-notice");
    Object.entries(model.errors).forEach(([part, error]) => {
      add(container, "p", `${LABELS[part]}暂不可用（${error}）；未使用旧报告替代。`, "research-report-warning");
    });
    if (model.hash) add(container, "p", `结果标识 ${model.hash}`, "research-report-hash");
    const grid = add(container, "div", undefined, "research-report-grid");
    const section = (heading) => {
      const node = add(grid, "section", undefined, "research-report-section");
      add(node, "h4", heading);
      return node;
    };
    const fields = (parent, rows) => {
      const dl = add(parent, "dl", undefined, "research-report-fields");
      rows.forEach(([label, value]) => {
        const pair = add(dl, "div"); add(pair, "dt", label); add(pair, "dd", value);
      });
    };
    if (model.hasResult) {
      const summary = section("收益摘要 · 保存值");
      fields(summary, [["策略", String(model.result.strategy_name || "未提供")], ["累计收益", format(model.metrics.total_return)],
        ["年化收益", format(model.metrics.annualized_return)], ["最大回撤", format(model.metrics.maximum_drawdown)],
        ["Sharpe", format(model.metrics.sharpe_ratio, "ratio")], ["成交次数", format(model.metrics.trade_count, "count")],
        ["已平仓交易", format(model.metrics.closed_trade_count, "count")], ["胜率", format(model.metrics.win_rate)],
        ["盈利 / 亏损 / 持平", `${format(model.metrics.winning_trade_count, "count")} / ${format(model.metrics.losing_trade_count, "count")} / ${format(model.metrics.break_even_trade_count, "count")}`],
        ["Profit Factor", format(model.metrics.profit_factor, "ratio")],
        ["单笔期望收益", format(model.metrics.expectancy_per_closed_trade, "signed_money")],
        ["已实现盈亏", format(model.metrics.realized_pnl, "signed_money")],
        ["毛盈利", format(model.metrics.gross_profit, "money")], ["毛亏损", format(model.metrics.gross_loss, "money")]]);
      add(summary, "p", "收益为历史研究结果，不代表未来收益。", "panel-note");
      if (model.tradeStats.notice) add(summary, "p", model.tradeStats.notice, "panel-note");
      if (model.tradeStats.factorNotice) add(summary, "p", model.tradeStats.factorNotice, "panel-note");
    }
    if (model.hasRisk) {
      const risk = section(model.sampleReady ? "尾部风险 · 样本门禁通过" : "尾部风险 · 数据不足或不完整");
      fields(risk, [["有效 / 最低收益样本", `${format(model.risk.return_observations, "count")} / ${format(model.risk.minimum_observations, "count")}`],
        ["缺失收益观测", format(model.risk.missing_return_observations, "count")],
        ["历史 VaR 95%（单会话）", format(model.sampleReady ? object(model.risk.historical_var)["0.95"] : null)],
        ["历史 CVaR 95%（单会话）", format(model.sampleReady ? object(model.risk.historical_cvar)["0.95"] : null)],
        ["结束净值", format(model.risk.ending_nav, "money")], ["现金权重", format(model.risk.cash_weight)],
        ["总持仓暴露", format(model.risk.gross_exposure)], ["最大单票权重", format(model.risk.top_position_weight)]]);
      add(risk, "p", "VaR/CVaR 为历史损失分位数，不是亏损上限；— 表示不可评估。", "panel-note");
    }
    const audit = section(model.coverageComplete ? "指标计算一致" : model.coveragePartial
      ? "已覆盖指标一致 · 部分项目未核验" : "指标审计未通过或不可用");
    add(audit, "p", "计算一致性与研究数据门禁是两回事；此处不提供交易准入。", "panel-note");
    if (model.hasIntegrity) {
      const details = add(audit, "details");
      add(details, "summary", `检查明细 · ${Object.keys(model.checks).length} 项`);
      const checks = add(details, "ul");
      Object.entries(model.checks).forEach(([name, passed]) => add(checks, "li", `${passed === true ? "通过" : "未通过"} · ${name}`));
      model.mismatches.forEach((name) => add(audit, "p", `不一致：${name}`, "research-report-warning"));
      model.unavailableChecks.forEach((name) => add(audit, "p", `未核验：${name}`, "research-report-warning"));
      model.integrityWarnings.forEach((warning) => add(audit, "p", `审计警告：${warning}`, "research-report-warning"));
    }
    const governanceTitle = model.governanceDecisionReady
      ? "研究治理 · 门禁通过（仅审计）"
      : model.governanceReplayVerified
        ? "研究治理 · 独立重放已通过"
        : model.governanceArtifactsConsistent
          ? "研究治理 · 制品一致但证据未完整"
          : "研究治理 · 证据不足或不可用";
    const governanceSection = section(governanceTitle);
    if (model.hasGovernance) {
      const binding = model.universeBinding;
      const bindingStatus = binding.required
        ? `${binding.status || "unknown"} · 已匹配 ${list(binding.matched_snapshot_hashes).length} · 缺失 ${list(binding.missing_snapshot_hashes).length}`
        : "未声明 PIT 股票池";
      fields(governanceSection, [
        ["制品一致性", model.governance.artifacts_consistent === true ? "通过" : "未通过"],
        ["独立重放", model.governanceReplayVerified ? "通过" : "未通过 / 未完成"],
        ["研究可复现", model.governance.reproducible === true ? "通过" : "未通过"],
        ["决策就绪", model.governanceDecisionReady ? "是（仅审计）" : "否"],
        ["PIT 股票池证据", bindingStatus],
        ["研究时点", typeof model.governance.decision_at === "string" ? model.governance.decision_at : "—"],
      ]);
      if (typeof model.governance.notice === "string" && model.governance.notice.trim()) {
        add(governanceSection, "p", model.governance.notice, "panel-note");
      }
      model.governanceWarnings.forEach((warning) => add(governanceSection, "p", `治理警告：${warning}`, "research-report-warning"));
      const failedChecks = Object.entries(model.governanceChecks).filter(([, passed]) => passed !== true).map(([name]) => name);
      if (failedChecks.length) add(governanceSection, "p", `未通过治理检查：${failedChecks.join("、")}`, "research-report-warning");
    } else {
      add(governanceSection, "p", "治理接口未返回；不能据此判断研究可复现或决策就绪。", "research-report-warning");
    }
    if (model.warnings.length) {
      const details = add(container, "details", undefined, "research-report-warnings");
      details.open = true;
      add(details, "summary", `研究限制与风险提示 · ${model.warnings.length} 项`);
      const warnings = add(details, "ul");
      model.warnings.forEach((warning) => add(warnings, "li", WARNINGS[warning] ? `${WARNINGS[warning]}（${warning}）` : warning));
    }
  }

  function createController({ fetcher, onUpdate, timeoutMs = 15000 }) {
    let generation = 0;
    let active = null;
    let runId = null;
    let pending = null;
    const publish = (state) => onUpdate({ ...state, close, retry: () => open(runId) });
    async function readPart(id, part, signal) {
      let timer;
      try {
        const work = (async () => {
          const response = await fetcher(`/api/v1/research/runs/${encodeURIComponent(id)}/${part}`, {
            method: "GET", headers: { Accept: "application/json" }, cache: "no-store", signal,
          });
          if (!response.ok) return { ok: false, error: `http_${response.status}` };
          return { ok: true, payload: await response.json() };
        })();
        return await Promise.race([work, new Promise((resolve) => {
          timer = setTimeout(() => resolve({ ok: false, error: "timeout" }), timeoutMs);
        })]);
      } catch (_) {
        // Never reflect arbitrary response bodies, URLs or exception text.
        return { ok: false, error: "network_or_payload_error" };
      } finally {
        clearTimeout(timer);
      }
    }
    function open(id) {
      if (typeof id !== "string" || !id.trim() || id.length > 128) throw new Error("invalid_run_id");
      if (pending && id === runId) return pending;
      if (active) active.abort();
      const token = ++generation;
      runId = id;
      active = new AbortController();
      const controller = active;
      publish({ phase: "loading", runId: id });
      pending = Promise.all(PARTS.map(async (part) => [part, await readPart(id, part, controller.signal)]))
        .then((results) => {
          if (token === generation) publish({ phase: "ready", runId: id, model: buildModel(id, Object.fromEntries(results)) });
        }).finally(() => {
          controller.abort();
          if (token === generation) { pending = null; active = null; }
        });
      return pending;
    }
    function close() {
      ++generation;
      if (active) active.abort();
      active = null; pending = null; runId = null;
      publish({ phase: "idle" });
    }
    return { open, close };
  }
  return { buildModel, createController, render, format };
});
