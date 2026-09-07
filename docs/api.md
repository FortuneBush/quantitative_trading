# 本地 API

## 1. 约定

服务默认监听 `http://127.0.0.1:8000`。交互式 OpenAPI 页面位于 `/api/docs`；浏览器看板位于 `/`，静态资源位于 `/static/`。当前 API 没有鉴权，设计用途仅限受信任的本机环境，不应直接暴露到局域网或公网。

所有证券代码采用六位代码加交易所后缀：`600519.SH`、`000001.SZ`、`430047.BJ`。输入不区分大小写，输出为大写。时间参数必须是带时区的 ISO-8601，例如 `2026-08-12T09:30:00+08:00` 或 `2026-08-12T01:30:00Z`；服务归一化为 `Asia/Shanghai`。

金额与价格等 `Decimal` 字段在 JSON 中以字符串返回，避免二进制浮点损失，例如 `"last": "12.34"`；指标值、涨跌百分比和信号强度为 JSON number。普通列表使用 `limit` 截取；市场范围批量行情端点支持 `limit`/`offset` 分页。历史行情按时间升序，告警和信号按时间倒序。

每个 HTTP 响应包含：

- `X-Request-ID`：请求追踪 ID；错误体中也会返回。
- `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`。
- `/api/` 路径使用 `Cache-Control: no-store`；其他路径使用 `no-cache`。

除成功响应外，HTTP 错误统一包装为：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数无效",
    "details": [],
    "request_id": "..."
  }
}
```

`code` 可能为 `validation_error`、`http_error`、`provider_unavailable` 或 `internal_error`。无效代码/时间/周期通常返回 422，资源不存在返回 404；AShareHub/AllTick/AKShare 上游暂时不可用、额度耗尽或本地软预算阻止新请求时返回 503，未处理异常返回 500。

> 默认配置使用 AShareHub；API Key 只从进程环境变量 `QUANT_MONITOR_ASHAREHUB_API_KEY` 读取，绝不进入 API、OpenAPI 或前端。`alltick`、`akshare` 和 `mock` 仍可显式选择。所有策略均为 `signal_only`，API 没有下单端点。

## 2. 健康与状态

### `GET /health`

轻量健康信息，不在 `/api/v1` 前缀下。

```json
{
  "status": "ok",
  "version": "0.1.0",
  "server_time": "2026-08-12T10:00:00+08:00",
  "database": {"type": "sqlite", "state": "connected"},
  "feed": {"name": "asharehub", "state": "running"},
  "collector": {
    "running": true,
    "iteration": 12,
    "last_run": "2026-08-12T09:59:59+08:00",
    "last_error": null
  }
}
```

`status` 为 `ok`、`starting` 或 `degraded`。除采集错误外，AShareHub 认证失败、429、达到本地日软预算或缓存中没有可展示数据也会降级，并通过 `feed.issues` / `provider.issues` 给出脱敏原因。兼容 AllTick provider 仍保留其实时订阅与子能力状态。

### `GET /live` 与 `GET /ready`

`/live` 只表示 FastAPI 进程能够响应，不依赖上游行情，正常时返回 200。
`/ready` 区分 `serving_ready` 与 `research_ready`：前者表示本地数据库和 HTTP 服务已可用，
后者表示策略达到最低历史覆盖。AShareHub 首次启动时可出现
`serving_ready=true, research_ready=false`，此时服务并未卡死，而是在后台逐只补齐日线。
`history_bootstrap` 包含 `state`、`ready/total/remaining`、`attempted`、`failed`、
`current_symbol`、时间戳和 `deferred_for_budget`；读取该接口不消耗 AShareHub 额度。

### `GET /api/v1/system/status`

返回完整运行状态：应用名和版本、`healthy`、provider、数据库、采集器、启动/研究就绪度、后台历史作业、运行时长、自选证券数、本地 WebSocket 客户端数、市场状态、公开配置和服务器时间。`last_collection_age_ms` 表示距最后一次采集完成的时长；旧字段 `latency_ms` 暂时保留为兼容别名，并不表示网络请求延迟。AShareHub 状态包含脱敏的缓存/最近成功时间和日请求预算统计（限额、已用、剩余、重置日期），不返回 URL、认证头或 API Key。默认软预算为 90/日，供应商免费档为 100/日；历史补数默认保留 10 次请求，不会耗尽实时快照余量。AllTick provider 的原有实时订阅与权限状态仅在显式选用它时出现。

`GET /api/v1/bars/{symbol}` 对真实 provider 默认只读 SQLite；缓存不足时也不会隐式消耗
上游额度。需要人工补拉时显式传 `refresh=true`。`/api/v1/analysis` 同样对真实 provider
保持缓存只读，后台历史作业负责补数；mock 模式仍可按需生成确定性数据。

### `GET /api/v1/data-quality/daily`

只读取 SQLite，对当前股票池执行确定性的日线质量审计，不访问上游。可用
`required_history_bars` 指定窗口；结果包含每只证券的缺口、重复日期、乱序、未来数据、
历史覆盖率和最新应有日期，以及横截面的共同日期/最新日期覆盖率。日历字段明确为
`weekday_fallback`，不能替代交易所官方节假日日历。

### `GET /api/v1/config/public`

返回可公开给本地看板的配置：`app_name`、`data_provider`、轮询秒数、K 线周期、历史条数、自选列表和市场时区。不返回数据库路径、主机端口、日志级别或 mock seed。未来也不得在此返回 API 密钥。

## 3. 市场与概览

### `GET /api/v1/market/status`

返回当前上海时区的阶段：

```json
{
  "phase": "morning",
  "label": "上午连续竞价",
  "is_trading": true,
  "is_trading_day": true,
  "trading_date": "2026-08-12",
  "timestamp": "2026-08-12T10:00:00+08:00",
  "calendar": "weekday_fallback",
  "calendar_notice": "接入真实数据源时应替换为交易所官方交易日历"
}
```

当前 `is_trading_day` 仅按周一至周五判断，不能识别法定节假日、临时休市或个股停牌，不可作为真实交易判断依据。

### `GET /api/v1/instruments`

返回当前 provider 可识别的证券基础信息；`symbols` 可传逗号分隔代码进行过滤。默认只读 SQLite，返回 `served_from=sqlite_cache` 且不会调用供应商。只有显式传入 `refresh=true` 才允许 AShareHub 调用 `/v2/reference/stocks`（可能消耗一次额度），响应同时给出 `provider`、`universe_scope` 和 `refresh_requested`。AllTick 模式仍只返回配置股票池。

### `GET /api/v1/overview`

返回市场状态、provider 状态、数量统计、最多三条最新行情、更新时间和数据源提示。

```json
{
  "market_status": {},
  "provider": {"name": "asharehub", "state": "running"},
  "counts": {"watchlist": 6, "alerts": 0, "signals": 0},
  "indices": [],
  "updated_at": "2026-08-12T10:00:00+08:00",
  "notice": "当前使用 AShareHub：自选行情来自受本地日预算和缓存保护的 HTTP 批量快照，历史仅提供未复权日线。"
}
```

`indices` 的名称保留是为了当前看板兼容；实际内容是自选列表中的股票，不是真实指数或市场宽度。`alerts` / `signals` 计数目前最多统计最近 100 条，因此不是数据库总数。

## 4. 行情与 K 线

### `GET /api/v1/sectors/banks`

返回沪深银行板块的本地聚合快照，包含股票池来源、报价/历史/分析覆盖率、板块涨跌宽度、中位涨幅、涨跌/成交额/量化分排名、每只银行的缓存行情与可解释研究摘要，以及本机额度估算。固定返回 `served_from=sqlite_cache`、`upstream_requests=0`；刷新、搜索和排序不会调用 AShareHub。

量化摘要使用至少 130 根未复权日线，并要求至少 15 只银行具有同一 `bar_as_of`。不足时 `analysis_ready=0` 并通过 `analysis_reason` 区分历史不足和共同日期不一致，不会用零值伪造得分。研究标签不是订单或收益承诺。

### `GET /api/v1/watchlist`

读取配置自选列表的最新报价。

```json
{
  "items": [],
  "count": 6,
  "provider": "asharehub",
  "updated_at": "2026-08-12T10:00:00+08:00"
}
```

报价对象包含领域字段 `symbol`、`timestamp`、`last`、`open`、`high`、`low`、`previous_close`、`bid`、`ask`、盘口量、成交量、成交额和 `source`，并增加看板兼容字段 `name`、`price`、`prev_close`、`change`、`pct_change`、`amount`、`updated_at`、`data_age_seconds`、`stale`、`timestamp_semantics`、`volume_semantics` 和 `field_availability`。AShareHub 的 `timestamp` 使用响应中的 `trade_time`；`volume`/`turnover` 是当日累计股数/人民币元。当前供应商文档没有五档盘口，因此 `bid`、`ask` 及盘口量保持空/零并由 `field_availability.order_book=unavailable` 明示，不伪造价格。

SQLite 读取会按当前 provider 的 `source` 过滤。因此 AShareHub 无可用数据时不会返回旧 AllTick/AKShare 行；这类响应为空并伴随健康状态降级，比混源旧值更可信。

`GET /api/v1/watchlist` 还返回 `configured_count`、`missing_symbols`、脱敏的
`provider_status` 和 `collector_error`，用于区分“没有配置股票”与“已配置但没有收到行情”。
浏览器连上本地 API 或本地 `/ws/market` 不代表 AShareHub 已返回新快照；AShareHub 没有该股票 WebSocket。实际状态以 provider 的最近成功时间、缓存新鲜度和日预算字段为准。

### `GET /api/v1/quotes`

查询最新报价。可选查询参数：

- `symbols`：逗号分隔证券代码；省略时使用配置自选列表。

示例：

```text
GET /api/v1/quotes?symbols=600519.SH,000001.SZ
```

成功响应为 `{"items": [...]}`。不在自选列表中的合法代码也可查询，但若数据库没有数据，只是不出现在列表中。

### `GET /api/v1/market/quotes`

读取 provider 的额度安全行情范围，`/api/v1/quotes/batch` 是兼容别名。默认只读 SQLite，分页、排序、搜索和页面刷新都不会消耗供应商额度；显式 `refresh=true` 才允许刷新目录和行情。响应的 `universe_scope` 明确范围：AShareHub 与 AllTick 均为 `configured_symbols`，防止把完整证券目录隐式扩成数千只实时报价；证券目录接口本身仍为 `provider_catalogue`。响应以 `served_from` 和 `refresh_requested` 标明实际语义。

查询参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `limit` | 100 | 返回条数，1–5000 |
| `offset` | 0 | 偏移量，非负 |
| `sort` | `symbol` | `symbol`、`name`、`last`/`price`、`pct_change`/`change_pct`/`change`、`volume`、`turnover`/`amount`、`timestamp`/`updated_at` |
| `order` | `asc` | `asc` 或 `desc` |
| `search` | 无 | 按证券代码或名称不区分大小写过滤；支持中文名称 |
| `include_status` | `true` | 是否返回 `market_status`（周一至周五兜底日历） |
| `include_source` | `true` | 是否返回顶层 `source`/`sources` |

成功响应的 `total` 是筛选后的可用报价总数，`universe_total` 是当前 `universe_scope` 中的沪 A 股票数，`unpriced_count` 表示无价而被宽松批量策略跳过的行；`items` 是当前页，`as_of` 是最新供应商事件时间：

```json
{
  "items": [
    {
      "symbol": "600519.SH",
      "name": "贵州茅台",
      "price": "1499.87",
      "pct_change": 0.42,
      "source": "asharehub",
      "updated_at": "2026-08-12T10:00:00+08:00"
    }
  ],
  "total": 3,
  "universe_total": 3,
  "unpriced_count": 0,
  "limit": 100,
  "offset": 0,
  "has_more": true,
  "as_of": "2026-08-12T10:00:00+08:00",
  "source": "asharehub",
  "universe_scope": "configured_symbols",
  "market_status": {"is_trading": true, "is_trading_day": true},
  "summary": {
    "count": 3,
    "catalogue_total": 3,
    "matched": 3,
    "priced": 3,
    "unpriced": 0,
    "advancers": 2,
    "decliners": 1,
    "unchanged": 0,
    "total_volume": 900,
    "total_turnover": "135000.00",
    "average_pct_change": 0.18,
    "top_gainers": [],
    "top_losers": []
  }
}
```

`source` 为单一源名称；多源混合时为 `"mixed"` 并附带 `sources` 数组。空目录是正常的 `200`（`items=[]`、`total=0`、`as_of=null`）；搜索无匹配也返回 `200`，但仍保留快照 `as_of`。provider 上游连接失败、认证失败、限流或返回不可用数据时为 `503 provider_unavailable`；本地 API 不把供应商 401 误作浏览器鉴权错误。

AShareHub 模式的 `summary.total_volume` / `total_turnover` 是响应中可用股票的当日累计量额之和；如果只请求自选列表，它不是全市场统计。缺少 `previous_close` 的股票不会进入涨跌家数、平均涨跌幅或排行榜计算。

### `GET /api/v1/quotes/{symbol}`

返回单个证券的最新报价对象；代码合法但无记录时返回 404。

### `GET /api/v1/bars/{symbol}`

读取指定证券的 K 线。查询参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `interval` | 配置的 `bar_interval` | AShareHub 模式仅支持 `1d`；其他 provider 可能支持额外周期 |
| `period` | 无 | 看板兼容别名；同时提供时 `interval` 优先 |
| `from` | 无 | 带时区 ISO-8601 起点，含边界 |
| `to` | 无 | 带时区 ISO-8601 终点，含边界 |
| `limit` | 240 | 1–2000 |

示例：

```text
GET /api/v1/bars/600519.SH?interval=1d&from=2026-01-01T00:00:00%2B08:00&limit=120
```

响应：

```json
{
  "symbol": "600519.SH",
  "interval": "1d",
  "items": [
    {
      "symbol": "600519.SH",
      "interval": "1d",
      "timestamp": "2026-08-12T00:00:00+08:00",
      "open": "100.00",
      "high": "101.00",
      "low": "99.80",
      "close": "100.50",
      "volume": 10000,
      "turnover": "1002500.00",
      "source": "asharehub",
      "amount": "1002500.00",
      "time": "2026-08-12T00:00:00+08:00"
    }
  ],
  "count": 1
}
```

`from > to` 返回 422。真实 provider 默认严格读取 SQLite，不会因为页面访问而隐式消耗额度；只有显式传入 `refresh=true` 才会尝试调用 provider 并写库。AShareHub 不提供分钟 K 线，`1m`/`5m` 等请求会被拒绝，而不是用日线或合成数据冒充。显式刷新时若认证失败、429、软预算耗尽或上游不可用则返回 503。

### `GET /api/v1/analysis/{symbol}`

基于指定股票的历史 K 线和最新报价生成可解释的研究快照。默认使用日线，支持
`interval`、`period` 和 `limit`（1–2000）。指标包括 SMA5/SMA20、EMA12/EMA26、
RSI14、20 期波动率、5/20 期动量、最大回撤和成交量比；趋势、动量、风险与建议均为
规则化的研究标签，不是交易指令。

```json
{
  "symbol": "600519.SH",
  "name": "贵州茅台",
  "price": "1499.87",
  "change_pct": 0.42,
  "indicators": {
    "sma_5": 1492.1,
    "sma_20": 1478.4,
    "rsi_14": 58.2,
    "annualized_volatility_20": 0.24,
    "return_5": 1.8,
    "return_20": 4.6
  },
  "trend": {"label": "bullish", "score": 0.67, "description": "..."},
  "momentum": {"label": "positive", "score": 0.31, "description": "..."},
  "risk": {"label": "medium", "score": 0.5, "description": "..."},
  "recommendation": {
    "action": "positive_bias",
    "label": "偏强观察",
    "signal_only": true,
    "reason": ["...", "..."]
  },
  "timestamp": "2026-08-12T10:00:00+08:00",
  "interval": "1d",
  "bars_used": 240,
  "source": "asharehub",
  "stale": false,
  "data_quality": {
    "status": "ok",
    "bars_used": 240,
    "bars_required": 60,
    "missing_indicators": []
  }
}
```

历史数据不足时，未准备好的指标返回 `null`，`data_quality.status` 为
`insufficient`；数据过期时为 `stale`。不会用零值填充缺失行情。

### `GET /api/v1/analysis`

批量分析配置自选列表，或通过 `symbols=600519.SH,600036.SH` 指定股票；响应为
`{"items": [...], "count": N, "interval": "1d", "provider": "asharehub"}`。

## 5. 告警与信号

### `GET /api/v1/alerts`

查询告警，参数均可选：

- `symbol`：证券代码。
- `level`：`info`、`warning` 或 `critical`。
- `acknowledged`：`true` / `false`。
- `limit`：1–500，默认 50。

响应为 `{"items": [...], "count": N}`。对象包含 `id`、`symbol`、`timestamp`、`level`、`title`、`message`、`category`、确认状态和 metadata，并增加 `severity`、`created_at` 兼容字段。

### `PATCH /api/v1/alerts/{alert_id}`

设置确认状态。JSON 请求体：

```json
{"acknowledged": true}
```

字段省略时默认为 `true`；当前实现会用 Python 真值转换，因此客户端应发送真正的 JSON boolean，避免字符串 `"false"` 被视为真。成功返回 `{"id": 1, "acknowledged": true}`；ID 不存在返回 404，JSON 无效返回 422。

### `GET /api/v1/signals`

查询研究信号：

- `symbol`：可选证券代码。
- `strategy`：可选策略稳定名称，精确匹配。
- `limit`：1–500，默认 50。

响应对象含 `id`、`strategy`、`symbol`、`timestamp`、`action`（`buy` / `sell` / `hold`）、`strength`（-1 到 1）、可选 `price`、`reason` 和 metadata，并增加 `strategy_name`、`created_at`。

### `GET /api/v1/strategies`

列出当前运行时加载的策略：

```json
{
  "items": [
    {
      "id": "moving_average_cross",
      "enabled": true,
      "type": "MovingAverageCrossStrategy",
      "mode": "signal_only"
    }
  ],
  "notice": "策略仅生成研究信号，不会提交订单。"
}
```

当前没有通过 API 启停、修改或执行策略的端点。

## 6. WebSocket

此处 WebSocket 是浏览器与本机 FastAPI 之间的事件通道。AShareHub 当前文档只提供 HTTP REST，
没有官方股票 WebSocket；因此“本地 WebSocket 已连接”只能证明页面推送链路正常，不能证明上游
正在逐笔推送，也不提供五档盘口。新行情仍由受缓存和日预算保护的 HTTP 轮询产生。

### `WS /ws/market`

连接后服务首先发送一份完整快照：

```json
{
  "type": "snapshot",
  "timestamp": "2026-08-12T10:00:00+08:00",
  "data": {
    "overview": {"market_status": {}},
    "watchlist": {"items": []},
    "alerts": {"items": []},
    "signals": {"items": []},
    "system": {}
  }
}
```

随后广播进程内事件：

```json
{
  "type": "quote",
  "timestamp": "2026-08-12T10:00:01+08:00",
  "data": {}
}
```

当前 WebSocket 的 `type` 可能为 `snapshot`、`subscribed`、`quote`、`alert`、`signal`、`error` 或 `heartbeat`。报价、告警和信号采用与 REST 相同的兼容序列化。进程内仍会产生 `bar` 与 `metric` 事件，但历史序列由 REST 提供，避免每轮采集向浏览器重发整批历史数据。

客户端发送 `{"type":"ping"}` 或 `{"action":"ping"}` 会收到 `heartbeat`；20 秒无客户端消息时服务也主动发送心跳。发送 `subscribe` / `unsubscribe` 只会收到当前配置自选列表的 `subscribed` 回应，当前版本没有每连接动态订阅过滤，所有客户端都接收同一事件流。

WebSocket 不保证交付、顺序游标或重放。慢/断线客户端会被移除，应重连并用首帧快照或 REST 恢复状态；不要只依赖增量消息维护权威历史。

## 7. 兼容性说明

当前 API 版本为 `/api/v1`，但项目尚未承诺长期稳定的公开协议。扩展时建议：

- 只做向后兼容的字段增加；改变字段类型、时间/成交量口径或错误结构时升级 API 版本。
- 对真实数据明确原始/复权口径、交易所时间和接收时间、数据质量与证券交易状态。
- 在局域网或公网使用前增加认证、TLS、速率限制和授权审计；确认行情许可证允许展示或传输。
- 保持“信号”和“订单”边界。若未来加入交易执行，应使用独立权限、独立 API 命名空间和人工风控，不复用当前信号查询端点。
