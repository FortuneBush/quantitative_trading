/*
 * A small, dependency-free sector treemap used by the local dashboard.
 *
 * The component deliberately accepts a loose API payload.  Provider fields
 * stay at the HTTP adapter boundary; the browser only receives normalized
 * display values and never calls an upstream service.  Keeping normalization
 * and layout pure also makes the visualization useful to the offline JS
 * tests and to custom dashboards that embed this file.
 */
(function installTreemap(root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.quantMonitorTreemap = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function createTreemapApi() {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  function finiteNumber(value) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
    const number = typeof value === "string" ? Number(value.replace(/,/g, "")) : Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function objectFrom(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    if (value.data && typeof value.data === "object" && !Array.isArray(value.data)) return value.data;
    return value;
  }

  function listFrom(value, keys) {
    if (Array.isArray(value)) return value;
    const object = objectFrom(value);
    for (const key of (keys || []).concat(["items", "records", "results", "list"])) {
      if (Array.isArray(object[key])) return object[key];
    }
    return [];
  }

  function first(object, keys, fallback) {
    if (!object || typeof object !== "object") return fallback;
    for (const key of keys) {
      if (object[key] !== undefined && object[key] !== null && object[key] !== "") return object[key];
    }
    return fallback;
  }

  function text(value, fallback) {
    const result = String(value === undefined || value === null ? "" : value).trim();
    return result || fallback;
  }

  function normalizeItem(raw, fallbackSector) {
    const source = objectFrom(raw);
    const symbol = text(first(source, ["symbol", "code", "ticker"], ""), "");
    if (!symbol) return null;
    const changePct = finiteNumber(first(source, ["change_pct", "pct_change", "changePercent", "change", "涨跌幅"], null));
    const price = finiteNumber(first(source, ["price", "last", "latest", "close", "最新价"], null));
    const amount = finiteNumber(first(source, ["amount", "turnover", "成交额"], null));
    const marketCap = finiteNumber(first(source, ["market_cap", "marketCap", "capitalization", "市值"], null));
    const sector = text(first(source, ["sector", "industry", "industry_name", "l1_name", "板块"], fallbackSector), fallbackSector);
    // Prefer market cap because it matches the market-map convention.  The
    // provider currently may only expose turnover, so fall back gracefully.
    const size = Math.max(0, finiteNumber(first(source, ["size", "weight"], null)) ?? marketCap ?? amount ?? 1);
    return {
      symbol,
      name: text(first(source, ["name", "display_name", "short_name", "名称"], symbol), symbol),
      sector,
      changePct,
      price,
      amount,
      marketCap,
      size: size > 0 ? size : 1,
    };
  }

  function normalizePayload(payload) {
    const object = objectFrom(payload);
    const rawSectors = listFrom(object, ["sectors", "industries", "groups", "板块"]);
    const rawItems = listFrom(object, ["items", "stocks", "quotes", "records", "个股"]);
    const grouped = new Map();
    const add = (name, raw, itemFallback) => {
      const sectorName = text(name, itemFallback || "未分类");
      const item = normalizeItem(raw, sectorName);
      if (!item) return;
      const key = sectorName;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(item);
    };
    rawSectors.forEach((rawSector) => {
      const source = objectFrom(rawSector);
      const sectorName = text(first(source, ["name", "label", "industry", "sector", "行业", "板块"], "未分类"), "未分类");
      const members = listFrom(source, ["items", "stocks", "quotes", "members", "个股"]);
      if (members.length) members.forEach((item) => add(sectorName, item, sectorName));
      else if (first(source, ["symbol", "code", "ticker"], null)) add(sectorName, source, sectorName);
    });
    // A flat response is also accepted.  If both forms exist, members already
    // present in a sector are not duplicated.
    rawItems.forEach((raw) => {
      const source = objectFrom(raw);
      const sector = text(first(source, ["sector", "industry", "industry_name", "l1_name", "行业", "板块"], "未分类"), "未分类");
      add(sector, raw, sector);
    });
    const sectors = [...grouped.entries()].map(([name, rows]) => {
      const unique = new Map(rows.map((row) => [row.symbol, row]));
      const items = [...unique.values()].sort((a, b) => b.size - a.size || b.changePct - a.changePct || a.symbol.localeCompare(b.symbol));
      const weight = items.reduce((sum, item) => sum + item.size, 0) || items.length || 1;
      const knownChanges = items.map((item) => item.changePct).filter((item) => item !== null);
      const totalAmount = items.reduce((sum, item) => sum + (item.amount || 0), 0);
      const weightedChange = knownChanges.length
        ? items.reduce((sum, item) => sum + (item.changePct === null ? 0 : item.changePct * item.size), 0) / Math.max(1, items.filter((item) => item.changePct !== null).reduce((sum, item) => sum + item.size, 0))
        : null;
      return { id: name, name, size: weight, changePct: weightedChange, amount: totalAmount || null, items };
    }).filter((sector) => sector.items.length).sort((a, b) => b.size - a.size || a.name.localeCompare(b.name));
    return {
      asOf: first(object, ["as_of", "updated_at", "timestamp"], null),
      source: text(first(object, ["source", "provider", "served_from"], "本地缓存"), "本地缓存"),
      marketStatus: first(object, ["market_status", "market", "session"], null),
      summary: objectFrom(first(object, ["summary", "breadth"], {})),
      sectors,
      total: sectors.reduce((sum, sector) => sum + sector.items.length, 0),
    };
  }

  function sumSize(nodes) {
    return nodes.reduce((sum, node) => sum + Math.max(0, finiteNumber(node.size) ?? 1), 0) || nodes.length || 1;
  }

  function worst(row, side) {
    if (!row.length || side <= 0) return Infinity;
    const total = sumSize(row);
    const max = Math.max(...row.map((node) => Math.max(0, finiteNumber(node.size) ?? 1)));
    const min = Math.min(...row.map((node) => Math.max(0, finiteNumber(node.size) ?? 1)));
    return Math.max((side * side * max) / (total * total), (total * total) / Math.max(1e-12, side * side * min));
  }

  // Squarified rectangles preserve the visual hierarchy while avoiding the
  // long unreadable strips produced by a simple slice-and-dice layout.
  function layoutTreemap(nodes, width, height) {
    const source = Array.isArray(nodes) ? nodes.filter(Boolean).map((node) => ({ ...node })) : [];
    const w = Math.max(0, Number(width) || 0);
    const h = Math.max(0, Number(height) || 0);
    if (!source.length || !w || !h) return [];
    const total = sumSize(source);
    const scaled = source.sort((a, b) => (b.size || 1) - (a.size || 1)).map((node) => ({ ...node, _area: (Math.max(0, finiteNumber(node.size) ?? 1) / total) * w * h }));
    const output = [];
    function recurse(remaining, x, y, rw, rh) {
      if (!remaining.length || rw <= 0 || rh <= 0) return;
      const row = [];
      let remainingArea = remaining.reduce((sum, node) => sum + node._area, 0);
      while (remaining.length) {
        const next = remaining[0];
        const side = Math.min(rw, rh);
        if (row.length && worst(row.concat(next), side) > worst(row, side)) break;
        row.push(next);
        remaining.shift();
      }
      const rowArea = row.reduce((sum, node) => sum + node._area, 0);
      if (rw >= rh) {
        const rowHeight = rowArea / rw;
        let cursor = x;
        row.forEach((node) => {
          const itemWidth = rowHeight ? node._area / rowHeight : 0;
          output.push({ node, x: cursor, y, width: itemWidth, height: rowHeight });
          cursor += itemWidth;
        });
        recurse(remaining, x, y + rowHeight, rw, rh - rowHeight);
      } else {
        const rowWidth = rowArea / rh;
        let cursor = y;
        row.forEach((node) => {
          const itemHeight = rowWidth ? node._area / rowWidth : 0;
          output.push({ node, x, y: cursor, width: rowWidth, height: itemHeight });
          cursor += itemHeight;
        });
        recurse(remaining, x + rowWidth, y, rw - rowWidth, rh);
      }
      remainingArea = remainingArea; // documented for debuggers; no mutation of payload
    }
    recurse(scaled, 0, 0, w, h);
    return output.map(({ node, ...rect }) => ({ ...rect, node: (() => { const copy = { ...node }; delete copy._area; return copy; })() }));
  }

  function escapeTitle(value) {
    return String(value === undefined || value === null ? "" : value);
  }

  function colorForChange(change, alpha) {
    const value = finiteNumber(change);
    if (value === null || value === 0) return `rgba(76, 91, 108, ${alpha || 0.88})`;
    const magnitude = Math.min(1, Math.abs(value) / 6);
    if (value > 0) return `rgba(222, ${Math.round(62 + magnitude * 36)}, ${Math.round(72 + magnitude * 42)}, ${alpha || 0.9})`;
    return `rgba(${Math.round(35 - magnitude * 6)}, ${Math.round(120 + magnitude * 72)}, ${Math.round(99 + magnitude * 40)}, ${alpha || 0.9})`;
  }

  function changeLabel(value) {
    const number = finiteNumber(value);
    return number === null ? "—" : `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
  }

  function createSvgElement(name, attrs) {
    const node = document.createElementNS(SVG_NS, name);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function render(container, payload, options) {
    if (!container || typeof document === "undefined") return null;
    const normalized = normalizePayload(payload);
    container.replaceChildren();
    if (!normalized.sectors.length) {
      const empty = document.createElement("div");
      empty.className = "sector-treemap-empty";
      empty.textContent = "暂无可用的行业与个股行情";
      container.append(empty);
      return normalized;
    }
    const width = Math.max(280, Math.floor(container.clientWidth || 900));
    const height = Math.max(260, Math.floor(container.clientHeight || 520));
    const svg = createSvgElement("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "行业与个股涨跌幅矩形树图" });
    const background = createSvgElement("rect", { x: 0, y: 0, width, height, fill: "#071019", rx: 8 });
    svg.append(background);
    const sectorRects = layoutTreemap(normalized.sectors, width, height);
    const onSelect = options && typeof options.onSelect === "function" ? options.onSelect : null;
    sectorRects.forEach(({ node: sector, x, y, width: sectorWidth, height: sectorHeight }) => {
      const group = createSvgElement("g", { class: "sector-treemap-sector" });
      const outer = createSvgElement("rect", { x: x + 1, y: y + 1, width: Math.max(0, sectorWidth - 2), height: Math.max(0, sectorHeight - 2), rx: 3, fill: colorForChange(sector.changePct, 0.2), stroke: "rgba(9, 17, 26, .95)", "stroke-width": 2 });
      group.append(outer);
      const headerHeight = sectorHeight >= 48 ? 22 : 0;
      if (headerHeight) {
        const header = createSvgElement("rect", { x: x + 1, y: y + 1, width: Math.max(0, sectorWidth - 2), height: headerHeight, fill: "rgba(3, 10, 16, .72)" });
        group.append(header);
        if (sectorWidth >= 68) {
          const label = createSvgElement("text", { x: x + 7, y: y + 15, fill: "#d7e5ee", "font-size": sectorWidth >= 130 ? 12 : 10, "font-weight": 650 });
          label.textContent = `${sector.name}  ${changeLabel(sector.changePct)}`;
          group.append(label);
        }
      }
      const inner = layoutTreemap(sector.items, Math.max(0, sectorWidth - 4), Math.max(0, sectorHeight - headerHeight - 4));
      inner.forEach(({ node: item, x: itemX, y: itemY, width: itemWidth, height: itemHeight }) => {
        const px = x + 2 + itemX;
        const py = y + headerHeight + 2 + itemY;
        const cell = createSvgElement("g", { class: "sector-treemap-item", tabindex: "0", role: "button", "aria-label": `${item.name} ${item.symbol} ${changeLabel(item.changePct)}` });
        const rect = createSvgElement("rect", { x: px, y: py, width: Math.max(0, itemWidth - 1), height: Math.max(0, itemHeight - 1), rx: 2, fill: colorForChange(item.changePct, 0.88), stroke: "rgba(8, 17, 25, .75)", "stroke-width": 1 });
        cell.append(rect);
        const title = createSvgElement("title", {});
        title.textContent = `${item.name} (${item.symbol}) · ${changeLabel(item.changePct)}${item.price === null ? "" : ` · ${item.price}`}`;
        cell.append(title);
        if (itemWidth >= 52 && itemHeight >= 25) {
          const name = createSvgElement("text", { x: px + itemWidth / 2, y: py + Math.min(itemHeight / 2 - 1, 17), fill: "#f1f6f8", "font-size": itemWidth >= 110 ? 12 : 9, "font-weight": 600, "text-anchor": "middle", "pointer-events": "none" });
          name.textContent = item.name.length > 10 && itemWidth < 130 ? `${item.name.slice(0, 9)}…` : item.name;
          const change = createSvgElement("text", { x: px + itemWidth / 2, y: py + Math.min(itemHeight - 8, itemHeight / 2 + 15), fill: "#f4fafb", "font-size": itemWidth >= 110 ? 11 : 9, "text-anchor": "middle", "pointer-events": "none" });
          change.textContent = changeLabel(item.changePct);
          cell.append(name, change);
        }
        if (onSelect) {
          cell.addEventListener("click", () => onSelect(item.symbol));
          cell.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(item.symbol); } });
        }
        group.append(cell);
      });
      svg.append(group);
    });
    container.append(svg);
    return normalized;
  }

  return { normalizePayload, layoutTreemap, render };
});
