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
`provider_unavailable` 的 `details` 仅包含脱敏的 `error_type`，不会回显 provider 异常正文、请求 URL 或认证信息；响应同时带 `Retry-After: 5`，表示客户端至少等待 5 秒后再重试。该值是最小退避提示，不代表供应商额度一定会在 5 秒内恢复；认证失败、权限拒绝或日额度耗尽时应优先读取 `/ready`、`/api/v1/system/status` 的降级原因并停止盲目重试。
`validation_error` 的 `details` 仅包含 `type`、`loc` 和 `msg`，不会回显请求提交的 `input`/上下文值；这避免误传 API Key 时被错误响应反射。
其他端点在把存储或校验异常转换为 `http_error` 时，也会对异常文本执行同一套凭据和本机路径扫描；检测到敏感内容时只返回“请求失败（错误信息已脱敏）”，不会把 `detail` 原文公开。
请求日志中的 URL path 会按路径段进行脱敏（path 可能包含用户控制的 `run_id`）：正常路由如
`/live`、`/api/v1/system/status` 保持可读，命中凭据样式或过长的片段时只保留
`[redacted]` 占位，不记录原始路径。

API 请求体默认限制为 8 MiB，可通过非敏感配置
`QUANT_MONITOR_API_MAX_REQUEST_BODY_BYTES` 调整（允许 64 KiB–64 MiB）。带有超出声明大小的
`Content-Length` 的非 multipart 请求会在解析前返回 `413`、错误码 `request_too_large`，并给出
`details.max_bytes`；无 `Content-Length` 的 chunked 请求和 multipart 请求保留原有端点解析行为。

> 默认配置使用 AShareHub；API Key 只从进程环境变量 `QUANT_MONITOR_ASHAREHUB_API_KEY` 读取，绝不进入 API、OpenAPI 或前端。`alltick`、`akshare` 和 `mock` 仍可显式选择。所有策略均为 `signal_only`，API 没有下单端点。银行横截面策略在真实 provider 的不可变 observation 尚未达到 accepted 决策质量前会抑制 BUY/SELL 信号；此时只保留诊断排名，不把 degraded/unverified 数据包装成风险结论。

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
    "last_error": null,
    "last_error_type": null
  }
}
```

`status` 为 `ok`、`starting`、`degraded`，启动失败为 `error`，关闭过程中和关闭后分别为 `stopping` / `stopped`。除采集错误外，AShareHub 认证失败、429、达到本地日软预算或缓存中没有可展示数据也会降级，并通过 `feed.issues` / `provider.issues` 给出脱敏原因。发生采集错误时，`collector.last_error_type` 只返回异常类型，`collector.last_error` 仅返回 `collection failed (异常类型)`；健康接口、日志和告警均不得回显敏感请求 URL、响应体或认证信息。兼容 AllTick provider 仍保留其实时订阅与子能力状态。

### `GET /live` 与 `GET /ready`

`/live` 只表示 FastAPI 进程能够响应，不依赖上游行情，正常时返回 200。
`/ready` 区分 `serving_ready` 与 `research_ready`：前者表示本地数据库和 HTTP 服务已可用，
后者表示策略达到最低历史覆盖。AShareHub 首次启动时可出现
`serving_ready=true, research_ready=false`，此时服务并未卡死，而是在后台逐只补齐日线。
`history_bootstrap` 包含 `state`、`ready/total/remaining`、`attempted`、`failed`、
`current_symbol`、时间戳和 `deferred_for_budget`；读取该接口不消耗 AShareHub 额度。
同时返回启动 `phase`、`started_at`、`ready_at`、`duration_ms` 与脱敏后的
`error_type`。服务器是前台常驻进程，终端不会自动返回提示符；出现
`Application startup complete` 即代表 HTTP 服务已启动，不是卡死。
`serving_ready` 会持续检查当前生命周期、数据库连接与采集任务，不能仅凭历史启动完成
时间保持为真。进入 `stopping` 时即撤销就绪；已就绪服务的采集器停止或数据库断开时
分别报告 `collector_stopped` / `database_disconnected` 并撤销就绪。启动后段失败会先
停止已经激活的采集任务再关闭 provider 和数据库，避免残留后台写入任务。
SQLite 初始化、WAL 建立和迁移受 `startup_database_timeout_seconds` 的同一有界期限约束；
若其它进程持有数据库锁，服务会在该期限内返回启动错误，而不会让底层 SQLite 默认锁等待
继续阻塞启动清理。

接口还返回独立的 `data_ready`、`data_reason` 与 `feed.freshness`。默认
`ready` 只代表本地服务已启动（兼容既有探针）；传入
`?require_data=true` 后，交易时段必须覆盖全部配置标的且报价处于新鲜阈值内，
休市时允许使用上一交易日的缓存收盘快照，但仍需覆盖全部配置标的，并拒绝全缺失或未来时间戳。传入
`?require_research=true` 可同时把最低历史覆盖纳入 `ready` 门禁；两个参数均只读
进程内/SQLite 状态，不会触发上游请求。
传入 `?require_calendar=true` 可进一步要求 AShareHub 已启用且同时加载完整的
SSE/SZSE 交易日历缓存（来源为 `asharehub` 或 `asharehub_cache`）；缓存缺失、过期、
部分加载、损坏或被额度保护时返回 503，并在 `calendar_reason` / `calendar` 中披露
`calendar_disabled` 或 `exchange_calendar_unavailable`。非 AShareHub provider 不需要该门禁，
只读检查不会刷新快照或消耗额度。

Windows 自动化可运行 `scripts\healthcheck.cmd --wait 60` 轮询 `/ready`；该探针仅访问
本地 HTTP 服务，不会消耗行情额度，成功退出码为 `0`。需要研究数据最低覆盖时追加
`--require-research`，服务尚在补数阶段会以非零退出码返回；需要确认行情可用时追加
`--require-data`，缺失/过期行情会返回 `data_not_ready`。生产监控建议将
`/live`（进程存活）、`/ready`（服务可用）和 `/ready?require_data=true`
（行情 SLO）分开配置，避免把“进程正常”误判为“数据正常”。
需要确认 AShareHub 交易日历缓存完整时可追加 `--require-calendar`；缓存未启用、过期、
损坏或仅加载单一交易所时返回 `calendar_not_ready`。该检查同样只读本地状态，不消耗额度。
切换数据源后可追加 `--provider asharehub|alltick|akshare|mock`；探针会校验响应中的
实际 provider，防止旧实例仍在监听端口时被误判为目标服务，校验失败返回
`unexpected_provider`。
探针还会拒绝缺失或非布尔的 `serving_ready`、`research_ready`、`data_ready` 字段
（返回 `invalid_readiness_payload`），以及 `NaN`/`Infinity` 等非有限超时参数，避免
错误响应导致误报或轮询失控。

AShareHub 的 HTTP 权限可能按产品/端点分别授予。若实时快照返回 403，provider
只对后续实时请求开启权限电路，不会阻断仍有权限的日线或证券目录请求；认证失败、
供应商 429 与本地预算耗尽仍按账户/进程范围熔断，并在 `provider.realtime_status`
中以脱敏状态公开。

### `GET /api/v1/system/status`

AShareHub 还公开 `last_quote_conflict_rows`：同一证券、同一 `trade_time` 出现不同字段时，适配器会拒绝整次响应并保持既有缓存，不按 wire order 选择冲突价格；不同时间戳的重复行仍按较新事件时间去重。

返回完整运行状态：应用名和版本、`healthy`、provider、数据库、采集器、启动/研究就绪度、后台历史作业、运行时长、自选证券数、本地 WebSocket 客户端数、市场状态、公开配置和服务器时间。`last_collection_age_ms` 表示距最后一次采集完成的时长；旧字段 `latency_ms` 暂时保留为兼容别名，并不表示网络请求延迟。provider 下的 `quote_freshness` 汇总监控股票池的 `configured_symbols`、`received_symbols`、`fresh_symbols`、`stale_symbols`、`missing_symbols` 和异常未来时间戳，并提供 `threshold_seconds`、最老数据年龄、最新报价时间及 `as_of`；它只读取进程内/SQLite 快照，不触发上游请求。真实 provider 出现未来时间戳时，无论开市或休市都会在 `provider.issues` 加入 `quote_timestamp_future`；缺失或过期行情仅在交易时段分别触发 `quote_data_missing` 或 `quote_data_stale`，休市时上一交易日快照不会被误报为健康故障。AShareHub 状态包含脱敏的缓存/最近成功时间和日请求预算统计（限额、已用、剩余、重置日期），不返回 URL、认证头或 API Key。默认软预算为 90/日，供应商免费档为 100/日；历史补数默认保留 10 次请求，不会耗尽实时快照余量。AShareHub 的 `last_quote_response_rows`、`last_quote_parsed_symbols`、`last_quote_dropped_rows` 和 `last_quote_missing_symbols` 用于审计上游响应中被字段校验丢弃的行；这些字段不会触发额外请求。AllTick provider 的原有实时订阅与权限状态仅在显式选用它时出现。

状态顶层的 `data_readiness` 是与 `/ready?require_data=true` 共用语义的缓存判定：`ready` 表示当前市场阶段是否具备可用行情，`reason` 为 `fresh_quotes_required`、`market_closed_cached_close_allowed` 或 `no_symbols_configured`，并附带配置/收到数量及聚合新鲜度状态。该字段只反映本地快照，不会为检查而消耗供应商额度。
顶层 `calendar_readiness` 与 `/ready?require_calendar=true` 共用同一只读判定，披露
`ready`、`required`、`reason`、缓存来源和错误类型；它只表示完整缓存是否已加载，
不把未认证的供应商标签当作官方证明。

`/api/v1/market/status`、概览、银行板块和报价响应会优先使用 provider 的只读交易日历状态；
AShareHub 已知节假日会标记为 `节假日休市`，不会因周一至周五兜底规则触发新鲜度故障。

AShareHub 未复权日线按交易日 `15:30` 作为保守可用时间。适配器会拒绝供应商在本机
`15:30` 之前返回的当日部分行（以及任何时间戳在本机时钟之后的行），不会把它们写入
缓存或用于研究就绪度；`provider.last_bar_future_rows` 暴露最近一次响应被这样丢弃的行数。

`GET /api/v1/bars/{symbol}` 对真实 provider 默认只读 SQLite；缓存不足时也不会隐式消耗
上游额度。需要人工补拉时显式传 `refresh=true`。`/api/v1/analysis` 同样对真实 provider
保持缓存只读，后台历史作业负责补数；mock 模式仍可按需生成确定性数据。

生产默认配置启用 AShareHub 交易日历缓存（7 天）。`provider` 状态中的
`trade_calendar_source`、`trade_calendar_last_refresh_at` 和
`trade_calendar_error_type` 显示当前使用缓存还是 `weekday_fallback`；缓存命中节假日时
不会发起实时行情请求，缓存失效、额度保留或接口失败会明确降级。日历响应或缓存中同一
交易所/日期的冲突重复行也会触发 `AShareHubDataError` 并 fail-closed；只有完全相同的
重复行会被安全去重。市场状态命中缓存时还会返回 `calendar_exchange`（`SSE`/`SZSE`）
及 `calendar_lookup`（`open`/`closed`）；缓存已加载但当前日期不在其覆盖范围时返回
`calendar_lookup=unknown`，并明确标记 `weekday_fallback`，不会把有限缓存误报为交易所已确认。

质量审计和缓存回测还会读取同一份已加载日历的不可变视图：
`AShareHubMarketDataProvider.get_trading_calendar()` 在 SSE/SZSE 数据均完整时构造
`VersionedSessionCalendar`，其 `calendar_fingerprint` 会进入采集批次血缘和回测
`diagnostics.assumptions`。跨交易所数据不完整、缓存过期或当前日期超出有限覆盖范围时，
这些路径统一回退 `weekday_fallback` 并保持未认证状态；回退不会伪造官方会话，也不会触发
新的供应商请求。

### `GET /metrics`

AShareHub 的 `quant_monitor_quote_conflict_rows` 是最近响应中被拒绝的同时间戳冲突重复行数；首次请求前为 `-1`，不含证券标签。

返回 Prometheus 0.0.4 文本格式的本地运行指标，适合由本机 Prometheus 或简单
监控脚本抓取。指标只从内存状态和 SQLite 快照生成，不访问 AShareHub/AllTick/AKShare，
也不包含证券代码、请求 URL、认证头或 API Key。当前包括进程健康度、启动/研究就绪度、
采集器运行状态、迭代数与跨进程生命周期内（当前进程）的周期累计计数（开始、成功、
失败、部分成功、取消及最近结果）、最后一次采集年龄、历史补数进度、WebSocket 客户端数量、
手动作业总数/活跃数及按状态计数（queued/running/succeeded/failed/cancelled），以及
provider 名称/状态、报价新鲜度聚合（fresh/stale/missing/future 数量及最老报价年龄）
和 AShareHub 本地额度估算（仅 provider 提供该字段时出现）。报价指标不含证券代码，
可用于设置“缺失/过期报价”告警而不会把某个股票标识写入监控系统；`-1` 表示当前
没有可计算的年龄。`quant_monitor_quote_freshness_info` 以 provider 和聚合状态作标签。
另有 `quant_monitor_data_ready`（0/1）及 `quant_monitor_data_readiness_info`（带
provider/reason 标签），用于区分“HTTP 服务在线”与“当前市场数据真正可用”；该判定
与 `/ready?require_data=true`、`system_status.data_readiness.ready` 一致。
启用 AShareHub 时还会输出最新响应形状的聚合指标
`quant_monitor_quote_response_rows`、`quant_monitor_quote_parsed_symbols`、
`quant_monitor_quote_dropped_rows`、`quant_monitor_quote_response_missing_symbols` 和
`quant_monitor_quote_response_coverage_ratio`；首次请求前这些指标为 `-1`，且不带证券
标签。相同信息在 `/api/v1/system/status` 的
`provider.quote_response_quality` 中提供（包括本地诊断用的 `missing_symbols`），不会
触发额外上游请求。
该端点位于 `/api` 前缀之外，返回 `Cache-Control: no-cache`；示例：

```text
curl.exe http://127.0.0.1:8000/metrics
```

指标是运维观测，不是行情数据；不要把未加认证的本地服务直接暴露到公网。

### `POST /api/v1/collector/jobs`、`GET /api/v1/collector/jobs`、
`GET /api/v1/collector/jobs/{job_id}` 与 `POST /api/v1/collector/jobs/{job_id}/cancel`

显式提交一次完整监控采集周期并立即返回 `202`，避免把人工刷新请求与后台轮询
混在一起。作业状态依次为 `queued`、`running`、`succeeded`、`failed` 或
`cancelled`；结果包含报价/K 线/指标/信号计数、迭代号、时间戳和脱敏的
`error_type`。同一进程同时只允许一个手动作业，重复提交返回 `409`。取消会向
正在执行的 provider 请求传播 `asyncio.CancelledError`，并等待有限宽限期；历史作业
仅保留最近 100 条，真正的供应商请求仍以 v3 `lineage/batches` 为准。

作业恢复、幂等查找和生命周期写入均在 SQLite 工作线程中执行，并在取消时等待
后台事务排空；因此数据库锁等待或大批量重启恢复不会阻塞 HTTP 健康检查、WebSocket
心跳或其它只读接口。取消/关闭期间不会在连接关闭后继续写入 SQLite。
取消接口本身也会先恢复持久化作业账本，因此服务刚重启后直接调用
`POST /api/v1/collector/jobs/{job_id}/cancel` 不需要先查询作业；若作业在重启前
尚未完成，会先按 `ProcessRestarted` fail-closed 终止并返回该终态，不会重新发起供应商请求。

客户端可能因网络超时重试时，应带上 `Idempotency-Key` 请求头（长度 1–255）。
相同键会返回第一次提交的同一个 `job_id`，包括作业已经结束之后，响应中的
`idempotency_replayed` 为 `true`，不会重复触发 provider 请求；首次提交该字段为
`false`。键只以 SHA-256 摘要保存，不会出现在 API 或日志中。空键或超过 255 个字符
返回 `400`。作业快照和摘要已持久化，服务重启后可继续解析同一键；重启时未完成的
作业会标记为 `ProcessRestarted`，不会自动重放 provider。真正的供应商请求仍以
`lineage/batches` 为准。

提交接口可能消耗一次上游额度，因此页面刷新和列表查询应使用
只读的 `GET /api/v1/quotes` 或 `/api/v1/market/quotes`；测试和 CI 不应提交真实
provider 作业。示例：

```text
POST /api/v1/collector/jobs
# 可选请求头：Idempotency-Key: dashboard-refresh-2026-08-29
GET  /api/v1/collector/jobs/{job_id}
POST /api/v1/collector/jobs/{job_id}/cancel
```

### `POST /api/v1/quote-refresh/jobs`、`GET /api/v1/quote-refresh/jobs`

这是行情刷新专用的异步作业入口。请求体可省略（默认使用当前股票池），也可传
`{"symbols":["600519.SH","600036.SH"]}` 或逗号分隔字符串。作业只请求并持久化
报价，不运行 K 线、指标或策略，返回 `202` 后使用同一路径下的
`GET /{job_id}` 查询，或 `POST /{job_id}/cancel` 请求取消。它复用 collector job
的幂等键、单活动作业限制、重启时 `ProcessRestarted` 终止和 SQLite 账本；报价作业的
`operation` 为 `quote_refresh`，完整监控周期为 `monitor_cycle`。重复使用相同的
`Idempotency-Key` 不会再次消耗供应商额度，且 quote-refresh 与 collector 两个入口的
幂等命名空间相互隔离。

历史兼容接口上的 `GET ...?refresh=true` 仍可使用，但会同步执行有副作用的刷新，并在
响应中返回 `Deprecation: true` 与 `Link: </api/v1/quote-refresh/jobs>; rel="alternate"`。
新客户端应提交上述异步作业；普通 GET（不带 `refresh=true`）始终只读 SQLite。

### `GET /api/v1/data-quality/daily`

只读取 SQLite，对当前股票池执行确定性的日线质量审计，不访问上游。可用
`required_history_bars` 指定窗口；结果包含每只证券的缺口、重复日期、乱序、未来数据、
历史覆盖率和最新应有日期，并以交易会话序列计算 `max_missing_streak` 与
`missing_session_ranges`，避免把交易所休市日当成连续缺口；结果还暴露有限日历的
`calendar_coverage`。横截面部分包含共同日期/最新日期覆盖率。日历字段明确为
`weekday_fallback`，不能替代交易所官方节假日日历；使用离线
`VersionedSessionCalendar` 时还会返回 `calendar_fingerprint`（快照内容的
SHA-256），用于复现与缓存隔离，并不代表官方来源认证。
有限日历覆盖外的工作日会单独记入每只证券的
`calendar_out_of_coverage_dates`，不会被静默当作休市日或有效行情。
报告同时返回 `data_provenance` 与 `quality_assurance`：该接口审计的是兼容
`bars` 投影，而不是直接返回不可变 observation；因此 `authoritative` 和
`decision_ready` 始终为 `false`。只有 `immutable_lineage_complete` 与
`immutable_quality_complete` 均为真，并经过研究运行的独立血缘/回放门禁，数据
才可作为可复现研究输入。上述字段只读取本地 SQLite，不会触发供应商请求。

### `GET /api/v1/config/public`

返回可公开给本地看板的配置：`app_name`、`data_provider`、轮询秒数、K 线周期、历史条数、自选列表和市场时区。不返回数据库路径、主机端口、日志级别或 mock seed。未来也不得在此返回 API 密钥。

### `GET /api/v1/lineage/batches`、`GET /api/v1/lineage/batches/{batch_id}`
和 `GET /api/v1/lineage/observations`

这些只读接口查询 SQLite 中的 v3 采集血缘，不会调用供应商或消耗额度。批次记录
请求哈希、状态、时间和安全的错误类型；观测记录同时返回市场区间、`available_at`、
`received_at`、`persisted_at`、来源、复权口径、修订标识和质量状态。分页参数为
`limit`/`offset`，观测也可以按代码、周期、来源、复权、质量和区间过滤。

### `GET /api/v1/fundamentals/as-of`

查询本地 SQLite 中的不可变基本面观测，并按显式 `as_of` 时间执行点时点选择。
该接口只读缓存，不调用当前行情 provider，也不消耗 AShareHub/其他上游额度。
`as_of` 必须是带时区的 ISO-8601 时间；可选 `symbol`、`metric`、`period_start`、
`period_end`、`unit`、`scope` 和 `source` 过滤器。报告期与 `available_at` 分离：
只有 `available_at <= as_of` 的记录可见，晚到修订不能污染更早时点。

响应包含 `candidate_count`（选择前的可见记录数）、`count`（选择后的事实数）、
`selection`、`served_from=sqlite_cache`、`provider_operations=0`，以及质量摘要：
`ok`、`missing_value` 或 `empty`。单位、合并/单体口径、来源不同的候选事实不会被
静默合并；同一可用时刻存在不同值时返回 `422`。缺失值保持 `null`，不会以零代替。

```text
GET /api/v1/fundamentals/as-of?as_of=2026-03-15T15:00:00%2B08:00&symbol=600036.SH
```

基本面存储由 SQLite v7 迁移创建，表为 append-only，并通过数据库触发器拒绝更新/删除。
重复投递相同 `observation_id` 与内容哈希是幂等的；复用 ID 但内容不同会失败关闭。
`error_message` 永远不直接返回；元数据中的凭据字段值、普通字段下的凭据/认证 URL/本机绝对路径都会脱敏，键名本身包含凭据或本机路径的条目会被移除。安全的请求坐标、响应摘要和计数保留。旧 provider 或缓存命中
只能更新兼容的 `bars` 视图，不会被伪装成新的原始观测。

### `GET /api/v1/fundamentals/factors/as-of`

在同一 `as_of` 语义上生成银行基本面因子。当前固定研究指标包括 PB、ROE、股息率、
不良率、拨备覆盖率、资本充足率和成本收入比；每项指标绑定 canonical unit 与方向，
横截面百分位使用确定性的并列中位排名。只有七项指标全部存在、单位匹配且在同一时点可见
时才返回综合 `score`；否则保留逐项值、缺失/单位错误原因，`score` 为 `null`。
任一请求成员缺少指标时，整个横截面分数均保持不可用，避免把分母静默缩小为幸存者。
横截面至少需要两个成员；单只股票的完整事实也不会被伪装成百分位评分。
不同来源之间也不会静默混合；来源不一致时逐项事实仍保留，但所有综合分数均为 `null`。
整个股票池须为相同且非 `unknown` 的报表口径；同一指标跨证券的期间必须一致。否则返回
`invalid_scope_metrics`、`scope_conflict` 或 `incomparable_period_metrics`，所有分数为 `null`。
逐项 `evidence` 包含 ID、内容哈希、期间、可用/接收时间、原始单位、口径、来源和修订号；
不包含 provider metadata。可选 `symbols=600036.SH,000001.SZ` 在数据库选择阶段即生效，
无关证券及非银行指标的歧义不会干扰请求，`candidate_count` 也仅统计所选范围。
`knowledge_mode=published_as_of` 表示按公开可用时间选择，并非本机接收时间回放；
`strategy_integration=false`、`score_usage=parallel_research_only` 表明未影响既有价格策略。
`quality.status=ok` 只证明数学输入完整可比，不代表实时性或独立来源认证。
接口只读 SQLite，固定 `provider_operations=0`；AShareHub 当前没有基本面事实时返回
`quality.status=empty`，不会伪造策略评分。

### `GET /api/v1/research/universes/as-of`

查询已持久化的 point-in-time `UniverseSnapshot`。`as_of` 必须为带时区的
ISO-8601 时间；只返回同时满足 `knowledge_cutoff <= as_of` 与
`recorded_at <= as_of` 的快照，可选按 `universe`、
`session_date`、`version` 和 `source` 过滤。接口只读 SQLite v9 的
`universe_snapshots`，固定返回 `served_from=sqlite_cache` 与
`provider_operations=0`；没有历史快照时返回空列表，**不会**把当前 `instruments`
目录冒充历史股票池。`/api/v1/universes/as-of` 是不在 OpenAPI 展示的兼容别名。

为防止“今天才导入的旧资料”污染过去，`recorded_at` 也必须不晚于 `as_of`；因此
晚到的历史快照会保持不可见，直到研究时点覆盖其本地接收证据。

快照的 `(universe, session_date, knowledge_cutoff, version)` 身份和内容 hash 在入库、
查询时都会重新校验；同一身份同一内容重复写入幂等，内容不同直接失败关闭。响应中的
`content_hash` 覆盖会话日、知识截止时间、版本及全部成员，可直接绑定研究 manifest。
每个 `item` 还保留持久化 `snapshot_key`、`source`、`recorded_at` 和经过安全脱敏的
`metadata`，以便审计快照何时进入本地证据库。

### `GET /api/v1/corporate-actions` 与 `GET /api/v1/adjustment-factors`

SQLite v8 以 append-only 方式保存公司行动条款和版本化复权因子。两个接口均为
provider-independent 的只读查询，不触发行情请求，响应固定包含
`served_from=sqlite_cache` 与 `provider_operations=0`。公司行动支持现金分红、送股、拆并股
和配股条款；复权因子携带 `factor_version`、`benchmark_date`、`revision_id`、输入哈希和
内容哈希。重复投递相同 ID/内容哈希幂等，复用 ID 但内容不同会失败关闭，数据库触发器拒绝
更新和删除。可选 `as_of` 分别按 `received_at`/`computed_at` 截断，避免较晚到达的修订污染
历史研究；`ex_from`/`ex_to` 或 `session_from`/`session_to` 用于限定市场日期范围。

```text
GET /api/v1/corporate-actions?symbol=600036.SH&as_of=2026-09-05T15:00:00%2B08:00
GET /api/v1/adjustment-factors?symbol=600036.SH&adjustment=qfq&session_from=2026-01-01
```

这些记录不会自动套用到旧 `bars`。回测只有在显式选择 `qfq`/`hfq` 并绑定逐会话因子时
才会生成带版本后缀的 adjusted view；`raw_unadjusted` 始终拒绝已复权输入，防止二次复权。
当前 AShareHub 仍只提供未复权日线，因此未导入公司行动/因子时，研究门禁保持不可决策级。

网络响应若由适配器提供 `response_content_hash`，批次元数据会返回该解析后 JSON 的
SHA-256 摘要及 `response_content_hash_scope=parsed_json_response`；这不是供应商签名，
且不会保存原始响应正文。缓存命中或旧适配器会明确返回 `unavailable`。

### `GET /api/v1/research/runs/{run_id}/lineage`

对研究清单声明的具体采集批次执行只读、fail-closed 血缘审计。清单应在
`dataset.attributes.batch_id`（或顶层 `batch_id`）绑定一个 v3 `collection_batches` ID；
未绑定批次时不会按来源扫描全库，而是直接返回 `verified=false`，避免把其他运行的观测
误当成当前输入。审计核对观测条数、输入 hash、来源、`accepted` 质量、可用/接收时间、
修订键唯一性、成功批次及批次 `item_count` 与观测行数的一一对应关系，并固定返回
`served_from=sqlite_lineage`、`upstream_requests=0`。若批次含多个响应收据，收据摘要也必须
保持一致；否则返回 `verified=false`。
扫描在服务端工作线程中执行，即使批次包含较多观察记录也不会阻塞 API/WebSocket 事件循环。

### `POST /api/v1/research/runs`、`GET /api/v1/research/runs` 与 `GET /api/v1/research/runs/{run_id}`

POST 接收 manifest 的公开 JSON 结构，严格重建并校验内容 hash 后写入 SQLite；重复
`run_id` 返回 409。GET 接口只读取 SQLite 中不可变的研究运行清单。列表支持策略、数据集、创建时间和
分页过滤；还可用 `tags`（逗号分隔，对 `assumptions.tags` 做大小写不敏感的 AND 过滤）、
`sort`（`created_at`、`run_id`、`strategy_name`、`dataset_name` 或 `dataset_source`）
以及 `order=asc|desc` 排序。排序字段采用白名单校验，并以 `run_id` 做稳定并列排序；响应
`filters` 会回显规范化条件。详情返回规范化 manifest、`content_hash`、输入 hash、日历/股票池/复权/
交易约束版本及创建时间。读取时会重新校验 manifest JSON hash，篡改记录会按内部错误
处理。列表项额外返回 `result_available`，表示该运行是否已经绑定不可变结果制品，
不会读取或内嵌完整结果 JSON。清单通过现有敏感字段扫描后才可入库，接口不会返回认证信息或机器路径。

`GET /api/v1/research/runs/{run_id}/lineage` 对该清单关联的 SQLite v3 观察记录执行只读
血缘门禁，检查记录数量、输入 hash、来源、`accepted` 质量、`available_at`/`received_at`
不晚于调用方提供的 `decision_at`、修订键唯一性以及成功采集批次。未提供 `decision_at`
或没有完整观察证据时严格返回 `verified=false` 及 `failures`，不会把结果自动升级为
`decision_grade`；响应固定 `upstream_requests=0`，也不会访问供应商。

回测完成后，可将 `backtest_result_dict()` 返回的对象显式绑定到已有运行：
`POST /api/v1/research/runs/{run_id}/result`，请求体为 `{ "result": <result-json> }`。
服务端会重新校验结果哈希，同一 `run_id` 只能保存一次；使用同一路径 `GET` 可在服务重启
后读取结果制品。结果接口只操作本地 SQLite，不会触发行情刷新。写入不可变表之前还会递归
检查字段名和值中的凭据、认证信息、绝对路径和敏感嵌入 JSON；即使调用方提供了看似有效
的哈希，也不会把不安全制品固化为可下载或可导出的研究包。
`GET /api/v1/research/results` 可按 `run_id` 分页列出已保存制品，适合研究历史页。
列表默认只返回 `run_id`、结果哈希和创建时间等完整性元数据，不携带可能很大的净值曲线与成交明细；
需要完整制品时显式传 `include_result=true`。响应中的 `include_result` 会回显实际模式，避免客户端
误把缺省的紧凑投影当成空结果。完整结果模式的 `limit` 最大为 20，超过时返回 422，避免
一次响应拼接大量大型制品；元数据列表仍允许最大 200。单运行详情接口仍返回完整、已校验哈希的 `result`。

`GET /api/v1/research/runs/{run_id}/export` 导出一个可离线保存的研究包，内容包含不可变
manifest、（若已生成）完整结果制品和对应的 `content_hash`、JSON hash、输入 hash 与结果 hash。
该接口只读 SQLite，固定返回 `served_from=sqlite_cache` 和 `upstream_requests=0`，并通过
`Content-Disposition` 提供安全文件名；没有结果的运行也可以导出，但会明确标记
`result_available=false`，不会伪造或重新计算结果。导出包不含 API Key、认证头、绝对路径或
当前行情，适合断网后做完整性复核。
仓库内的 `scripts/verify_research_export.py` 可直接读取该 JSON，重建 manifest 和结果的
canonical JSON 并校验两组 SHA-256；它不访问服务、SQLite 或行情源。没有结果的包报告
`incomplete`，归档任务可使用 `--require-result` 将其作为失败门禁。

如果只需归档结果制品本身，可调用 `GET /api/v1/research/runs/{run_id}/result/download`。
响应体是 SQLite 中保存的规范化 JSON 原文，并带 `X-Result-Hash` 与 `X-Result-JSON-Hash`
响应头，适合在外部归档前做字节级完整性校验；该接口同样只读本地数据，不访问 provider。

`POST /api/v1/research/runs/{run_id}/backtest` 使用指定运行的策略声明，从当前 provider
对应的 SQLite 日线缓存执行银行多因子回测；该接口严格不访问 provider，缺少缓存时返回
422。请求的 `symbols` 中只要有任一证券没有缓存日线，默认也会返回 422，避免横截面
股票池被静默缩小后仍生成看似完整的方向性结果；响应会列出缺失代码。仅用于明确的探索性
分析时可传 `allow_partial_symbols=true`，此时成功响应的 `manifest_binding` 会同时返回
`requested_symbols`、`served_symbols`、`missing_symbols` 和 `partial_symbol_input=true`，并加入
`requested_symbols_missing` 警告。该开关不能与 `verify_manifest=true` 同时使用。
成功后自动生成结果哈希并写入不可变结果表；同一运行重复执行返回 409。
请求体可传 `verify_manifest=true` 启用严格离线绑定：服务端会校验缓存条数、来源、证券集合、
策略阈值及 `manifest.input_hash`（按规范化 Bar 记录重算），任一不一致即返回 422。严格模式会从清单中
绑定的策略参数和回测配置构造执行实例，而不是读取当前进程的策略/费用设置；未知字段、类型错误或不支持的
配置会拒绝执行，避免部署配置漂移导致同一清单产生不同结果。旧运行默认保持兼容，
但响应会标记 `manifest_binding.warnings=["manifest_input_hash_not_verified"]`；生产研究运行应始终
显式启用严格校验。严格模式还要求每一条兼容 `bars` 投影都能与不可变 observation
逐值匹配；缺少 observation、存在冲突修订或仅有旧版直接写入的 bars 时会拒绝执行。
这只证明输入可追溯，不会把 provider 的 `degraded` 质量自动提升为 `decision_ready`。

纯 Python 调用方也可在 `BacktestEngine.run()` 显式传入
`point_in_time_universe=UniverseSnapshot`（单会话）或
`PointInTimeUniverse` 加 `universe_knowledge_cutoff`（多会话），也可传入
`{session_date: UniverseSnapshot}` 的已持久化快照映射。映射中的每个快照都必须
覆盖实际会话，且其 `knowledge_cutoff` 不晚于该会话决策时点。引擎会按每个
交易会话校验所有 bar 的证券代码；未来上市、已失效或不在当时股票池中的记录会
直接抛出校验错误，未传入该参数的普通探索性回测保持兼容。通过校验的快照版本、
知识截止时间和内容哈希会以结构化、只读的
`result.diagnostics.assumptions.universe_binding` 保存，序列化结果不会把它扁平化
成不可审计的字符串。严格的 `verify_replay(..., require_universe_binding=true)`
还要求同一快照/解析器及其哈希出现在 manifest 中；缺失或不一致时 fail-closed。
多会话回测使用的 `universe_knowledge_cutoff` 还必须不晚于最早会话的
`daily_evaluation_time`（默认上海时间 15:30）；否则会拒绝执行，防止把回测区间
之后才知道的上市、退市或分类修订泄漏到历史股票池。若需要逐会话变化的知识时点，
应为每个会话预先解析并传入独立的 `UniverseSnapshot`（或持久化快照映射），而不是复用一个未来截止时间。

若策略启用了基本面叠加，纯 Python 调用方还可以显式传入
`fundamental_factor_reports={session_date: report}`。报告必须使用
`selection=bank_fundamental_factors_as_of_v1`、带时区的 `decision_at`，且不得晚于
对应会话的回测决策时点；引擎会按会话复制报告并放入 `CohortStrategyContext.metadata`，
不会读取在线数据库或 provider。缺少报告的会话继续由策略执行价格-only 回退，并在
`result.diagnostics.assumptions.fundamental_factor_binding` 中披露绑定覆盖范围和每期
`sha256-json-v1` 内容哈希；该状态是 `bound_unattested`，不等于来源认证。生产研究仍应将
同一组报告哈希和来源写入 manifest，并在离线验证器中核对。

HTTP 缓存回测也支持在请求体传入 `fundamental_factor_reports`。当
`verify_manifest=true` 且清单显式声明 `strategy.parameters.fundamental_weight > 0` 时，
清单的 `dataset.attributes.fundamental_factor_report_hashes` 必须按会话日期保存同一组
`sha256-json-v1` 哈希，请求也必须提供对应报告；服务会在结果生成后逐项比较哈希和完整覆盖，
任一缺失、未来报告或内容不一致都会返回 422。严格清单未声明 `fundamental_weight` 时，
按兼容规则视为显式价格-only（权重 0），不会被当前运行时默认权重暗中改变。

如果回测 universe 较大、可能超过反向代理的请求超时，可使用进程内异步作业：
`POST /api/v1/research/runs/{run_id}/backtest/jobs`（请求体与同步接口相同）立即返回
`202` 和 `job_id`；随后通过 `GET /api/v1/research/runs/{run_id}/backtest/jobs/{job_id}`
轮询状态，成功后再读取同一运行的 `/result`、`/risk` 和 `/integrity`。该管理器默认最多
同时运行 2 个作业、保留最近 100 个快照，支持 `Idempotency-Key` 避免网络重试重复执行，
并可通过 `POST .../backtest/jobs/{job_id}/cancel` 尝试取消。作业状态只保存在当前进程，
服务重启不会重放；SQLite 中的结果制品才是持久化事实来源。达到并发上限时返回 `409`
并带 `Retry-After: 1`，不会无限制排队或触发 provider 请求。

取消是有明确边界的协作式操作：尚未开始的作业可直接成为 `cancelled`；运行中的
作业先成为 `cancelling`（`terminal=false`），继续占用并发名额，直到工作线程退出。
重复取消不会重复注入中断。计算阶段在读取证券历史和每个回测会话边界检查取消；
取消在结果提交前被接受时，不会写入结果制品。若不可撤销的 SQLite 结果提交已经
开始，则等待其完成并以真实的 `succeeded` / `failed` 状态结束，不能将已写入的
制品报告为“已取消”。服务关闭时同样先等待研究线程退出，再关闭数据库。
Python 线程无法强制安全终止；自定义计算若迟迟不返回，作业会保持 `cancelling`，
而不是提前释放名额或冒险关闭仍被使用的数据库。同步 `/backtest` 也使用同一
计算/提交边界。所有这些操作均不访问上游行情接口。

结果中的 `diagnostics.decision_grade_checks` 与 `decision_grade_failures` 是服务端根据
可观察证据派生的只读门禁说明。当前版本缺少独立认证的行情、日历、公司行动和执行价
血缘，因此 `decision_grade` 始终为 `false`；修改 `calendar_version`、`source` 或其他
配置字符串不能提升等级。服务端还会校验成交数与有明确尝试时间的跳过成交数之和，
诊断计数不一致时拒绝结果。

`GET /api/v1/research/runs/{run_id}/risk` 从已持久化的回测 JSON 离线计算历史尾部风险，
默认返回 95%/99% 历史 VaR、CVaR、最坏/最好交易日收益、回撤、现金/总暴露、最大持仓
权重及集中度告警。可用 `confidence=0.90,0.95,0.99` 和 `minimum_observations` 调整
置信度与样本门槛；样本不足时风险值为 `null` 并附带 warning，不会填充为零。该接口
只读 SQLite，响应包含 `upstream_requests=0`，不会消耗 AShareHub 额度。结果制品中缺失、
非数字或非有限的 `daily_return` 不会被当作零值；它们会从尾部样本中排除，并通过
`missing_return_observations` 与 `missing_or_invalid_return_observations` warning 明确披露。
只要存在任意缺失或非法收益观测，即使有效样本数仍达到 `minimum_observations`，
`sample_sufficient` 也会保持 `false`，历史 VaR/CVaR 返回 `null`；调用方不得把缺失观测
当作零收益。风险报告同时返回 `missing_position_observations`、`position_data_complete`
和可空的 `position_count`。持仓明细缺失、估值非法或总估值无法校验时，`gross_exposure`、
`top_position_weight` 与 `concentration_warning` 返回 `null` 并附 warning；空持仓映射
（明确的 `{}`）与缺失持仓映射（`null`/省略）语义不同。
结束资金也会进行非负、有限且不超过结束净值的校验；不可信制品中的负资金、非有限
资金或资金大于净值不会生成负数/超过 100% 的 `cash_weight`，而是返回 `null` 并附上
`missing_or_invalid_ending_cash` 或 `ending_cash_exceeds_ending_nav` warning。

`GET /api/v1/research/runs/{run_id}/integrity` 对不可变结果制品做指标一致性审计：从保存的
净值曲线和成交台账重新计算总收益、年化收益、波动率、Sharpe/Sortino、Calmar、最大回撤、
换手率、费用合计及正收益会话比例，并核对逐点日收益/回撤是否与净值曲线一致。响应中的
`integrity.verified` 只有所有已执行检查通过时才为 `true`；`mismatches` 给出稳定的检查名。
已平仓统计还会从买入成本（含佣金）与卖出净收入重建 FIFO 已实现盈亏，校验胜负笔数、
Profit Factor 与单笔期望值。旧制品未保存的字段通过 `unavailable_checks` 披露，
`trade_statistics_status` 区分 `verified`、`unavailable`、`failed`；部分检查通过不能
当作全覆盖审计。不会修改旧结果 JSON 或其哈希。
该接口只读 SQLite、不会重跑策略或访问 provider，固定返回 `served_from=sqlite_cache` 与
`upstream_requests=0`。浮点比较容差可用 `tolerance`（默认 `1e-9`，最大 `1e-3`）调整。

`GET /api/v1/research/runs/{run_id}/governance` 汇总一个研究运行的不可变清单、结果制品、
指标一致性、策略绑定、点时点血缘和 decision-grade 证据。可选 `decision_at`（必须为带
时区的 ISO-8601 时间）用于完成可用性证明；缺少结果、批次绑定或独立血缘证据会返回失败
检查和稳定的 `failures`，不会把未知状态当作通过。响应固定标记
`served_from=sqlite_cache`、`upstream_requests=0`，治理方法为 `research_governance_v2`。
`artifacts_consistent` 要求清单/结果哈希、全覆盖指标审计与策略名称绑定通过；
`reproducible` 还需要针对绑定输入、代码、配置与输出的独立重放证据（`replay_verified`），
`decision_ready` 则要求七项检查全部通过。接口会在完整不可变 observation 集合上对白名单
银行策略执行本地独立重放，并在 `replay_audit` 中比较结果 hash；任何缺失或不匹配都会
保持 `replay_verified=false`。严格重放还要求每个采集批次带有与 manifest 日历版本一致的
不可变 `metadata.calendar.version`；仅在 manifest 中填写日历字符串、或缺少批次日历证据，
都会在 `replay_audit.calendar_binding_matches` 失败。当前没有受控的决策级认证，因此 `decision_grade` 仍为
`false`；上传结果的 `decision_grade=true` 只作为 `declared_decision_grade` 披露，不授予等级。
`metric_audit` 提供实际检查覆盖情况。血缘扫描上限为 10000 条，
未扫描完整批次时 `lineage_scan_truncated=true` 且血缘检查失败。即使没有批次绑定，
非法或不带时区的 `decision_at` 也返回 422。所有状态仅为研究审计，不构成交易许可。

当 manifest 声明 `dataset.attributes.universe_snapshot_hashes`，或将
`versions.universe` 直接设置为 `sha256:<64位十六进制>` 的内容哈希时，响应还会返回
`universe_binding` 证据对象。它只读取 SQLite v9 的不可变 `universe_snapshots`，展示
`declared_snapshot_hashes`、`matched_snapshot_hashes`、`missing_snapshot_hashes` 以及每个
匹配快照的 `knowledge_cutoff`、`recorded_at`、`source` 和 `content_hash`。状态为
`verified` 仅表示声明的快照在 `decision_at` 时同时满足知识截点与本地接收时间；
`missing`、`query_error`、`scan_truncated`、`version_mismatch` 或
`decision_at_required` 均保持 fail-closed。旧 manifest 没有内容哈希时状态为
`not_declared`，不会把人类可读的 universe 版本字符串当作 PIT 证据。该对象始终不会
提升 `decision_grade` 或绕过独立重放门禁。

`GET /api/v1/research/runs/{run_id}/robustness` 对同一不可变净值曲线执行可复现的 IID
bootstrap 稳健性分析，返回累计收益与最大回撤的置信区间。参数为 `confidence`（默认
`0.95`）、`resamples`（默认 2000，最大 100000）、`seed`（默认 0）和
`minimum_observations`（默认 30）。样本短缺或存在缺失/非法收益时，区间保持 `null`
并附 warning，不会用合成观测掩盖数据问题。该接口只读 SQLite、使用局部随机状态、
不会重跑策略或访问 provider，固定返回 `served_from=sqlite_cache` 与
`upstream_requests=0`；区间是研究不确定性描述，不是收益预测或交易承诺。

`GET /api/v1/research/runs/{run_id}/portfolio` 从同一不可变结果生成组合快照，严格校验
现金、持仓市值与 NAV 恒等式，并返回结算数量、现金权重、单票最大权重、集中度 HHI 和
估值质量。快照同时保留 `strategy_price_basis`、`execution_price_basis` 和
`valuation_price_basis`；复权策略的结果必须明确显示后两者为
`raw_unadjusted`，旧制品缺失这些字段时返回 `unavailable`，不猜测口径。序列化结果生成的快照默认始终标记为 `partial`，并带有
`valuation_quality_unattested` 警告；保存的 `diagnostics.decision_grade` 不是独立重放或
估值真实性证明。该接口
只读 SQLite，结果损坏或账务不一致时返回 422，不会用零值修复。

`GET /api/v1/research/runs/{run_id}/portfolio/constraints` 对上述快照执行显式的组合级
约束审计，默认使用版本化策略 `long_only_conservative_v1`（单票权重不超过 25%、HHI
不超过 0.20、总敞口不超过 1、最多 50 只持仓）。可通过 `max_holding_weight`、
`max_concentration_hhi`、`min_cash_weight`、`max_gross_exposure`、`max_positions` 和
`require_complete_valuation` 查询参数覆盖限制；若需关闭默认限制可传
`disable_default_limits=true`（仍可在同一请求中保留显式限制）。每项检查返回实际值、限制、比较符和
稳定原因；违反限制时为 `fail`，没有配置限制时为 `unavailable`。估值不是
`complete` 时会追加 `valuation_quality_not_complete` 警告并保持
`decision_ready=false`；即使同时违反硬限制，也不会把部分/过期估值标记为通过。接口只读 SQLite，固定返回
`served_from=sqlite_cache` 与 `upstream_requests=0`，结果仅用于研究审计，不构成投资建议
或交易指令。

`POST /api/v1/research/runs/{run_id}/benchmark` 使用请求体中的 `benchmark_returns`
（序列按回测会话顺序，或以 `YYYY-MM-DD` 为键的对象）与已保存回测进行离线比较，返回
累计/年化收益、超额收益、alpha、beta、相关性、跟踪误差、信息比率和上下行捕获。可选
`benchmark_name`、`minimum_observations`、`annualization_sessions` 与
`annual_risk_free_rate`；缺失日期和样本不足会显式 warning。该接口不会读取供应商，响应
固定标记 `served_from=sqlite_cache`、`upstream_requests=0`，也不会保存或覆盖研究结果。
对象键若包含日期时间，必须带时区且会先归一到 `Asia/Shanghai` 再映射交易日；无时区
时间会被拒绝，避免 UTC 跨日导致基准收益错位。

`POST /api/v1/research/runs/{run_id}/attribution` 对同一缓存结果做逐期算术归因。请求体
必须提供 `benchmark_returns`，并可选提供 `cash_returns`（均支持会话顺序数组或日期映射）。
响应区分基准、现金、交易成本与主动贡献；缺少现金或基准时返回 `null` 和降级状态，绝不
把缺失收益当作零。该接口只读 SQLite，固定返回 `served_from=sqlite_cache`、
`upstream_requests=0`，不重跑策略或访问行情源，仅用于研究解释。

`POST /api/v1/research/runs/{run_id}/stress` 对已保存回测执行显式情景压力测试。
请求体为 `{ "scenarios": [{"name": "银行冲击", "price_shock": -0.10,
"cost_multiplier": 1}] }`；价格冲击作用于持仓市值，成本倍数作用于已发生的费用，
不会重跑策略，也不会访问供应商。响应包含每个情景的期末净值、净值变化、最大回撤和
最坏情景，并固定返回 `served_from=sqlite_cache`、`upstream_requests=0`。压力结果是
研究假设，不代表预测或交易指令。
结果中的交易时间若以 datetime 表示，必须带时区并会统一归一到 `Asia/Shanghai`；
naive datetime 会被拒绝，避免跨时区日期改变成本归属。

`POST /api/v1/research/runs/{run_id}/capacity` 使用已缓存的 1d 成交量审计回测成交的
参与率，默认 `max_participation_rate=0.10`。它不会重新请求行情；缺少执行日成交量的
交易会标记为 `assessed=false`，不会被当作可成交。响应包含请求成交额、按参与率折算的
 可执行成交额、超限交易数和逐笔原因，并固定返回 `upstream_requests=0`。成交量来源优先
 取回测结果 `diagnostics.assumptions.bar_sources` 中记录的实际输入源；旧结果没有该字段时
 才回退到研究 manifest 的 `dataset.source`，响应的 `data_provenance.bar_source_origin`
 会披露这一下降。接口不会按当前运行时 provider 猜测来源，也不会把多源数据静默混合。

`GET /api/v1/research/runs/compare?run_ids=id1,id2` 一次比较 2--20 个已持久化运行。
接口只读取 SQLite 中的 manifest 和不可变结果，不会重新回测或访问供应商；响应按运行返回
策略、数据集、日历/股票池/复权/约束版本、结果哈希、核心绩效指标和 `decision_grade`。
若结果已固化，`decision_grade` 始终由服务端按未认证结果的 fail-closed 规则返回 `false`；
上传制品中的自报值仅在 `declared_decision_grade` 中披露，不能让比较页显示研究门禁通过。
`decision_grade_failures` 至少包含 `decision_grade_unattested`，并可附带制品自报的证据缺口
供诊断，不能视为独立认证。
结果尚未绑定时 `result_available=false`，对应指标为空；请求固定标记
`served_from=sqlite_cache`、`upstream_requests=0`。该比较用于研究审计，不构成投资建议。

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

响应额外提供 `fundamental_factors` 股票池报告，以及 `items[].fundamental_factors` 和
`items[].analysis.fundamental_factors`（存在价格分析时）。它们使用本次快照构建时间作为
基本面截止时刻，与价格 K 线时间分开披露；只读本地库、上游请求为 0。缺日线不会隐藏
基本面证据，基本面修订冲突则仅将该报告标为 `invalid`（警告
`fundamental_selection_ambiguous`），保留现有价格诊断。页面显示独立基本面视图，不混入
价格策略分数或改写研究信号。若本地存在完整可比事实，策略会按
`bank_strategy_fundamental_weight`（默认 20%，也可由
`QUANT_MONITOR_BANK_STRATEGY_FUNDAMENTAL_WEIGHT` 设置；0 表示关闭）显式
叠加；响应中的 `analysis.price_score` 与 `analysis.fundamental_overlay` 分别给出原价格分、
基本面分、贡献、权重和 `decision_at`。不满足完整/同集合/不晚于决策时点条件时，
`fundamental_overlay.enabled=false`，`reason` 说明价格策略回退原因。`model_variant` 区分
`price_only_v1` 与 `price_plus_fundamental_v1`，不会把基本面缺失伪造成 0 分。

返回沪深银行板块的本地聚合快照，包含股票池来源、报价/历史/分析覆盖率、板块涨跌宽度、中位涨幅、涨跌/成交额/量化分排名、每只银行的缓存行情与可解释研究摘要，以及本机额度估算。固定返回 `served_from=sqlite_cache`、`upstream_requests=0`；刷新、搜索和排序不会调用 AShareHub。

### `GET /api/v1/market/treemap`

返回行业 → 个股的缓存矩形树图数据；兼容别名为
`GET /api/v1/sectors/treemap`。接口只读取当前 provider 已落盘的证券目录与最新报价，固定
`served_from=sqlite_cache`、`upstream_requests=0`，不会因为刷新看板而消耗 AShareHub 日额度。
顶层 `sectors` 与 `items` 是兼容别名；每个行业组的 `children` 与 `items` 也是兼容别名。
个股 `value`/`size` 优先使用成交额，缺失时使用成交量×最新价，仍缺失时使用明确标记为
`placeholder` 的占位值；它们不是市值。`size_semantics` 和 `change_semantics` 说明口径，
不要将图块面积解释为市值或交易指令。未收到报价的目录证券不生成图块，但会计入
`unpriced_count`；没有行业分类的证券进入 `未分类`。可选 `max_industries`（1–2000）和
`max_stocks_per_industry`（1–5000）控制响应大小。

量化摘要使用至少 130 根未复权日线，并要求至少 15 只银行具有同一 `bar_as_of`。不足时 `analysis_ready=0` 并通过 `analysis_reason` 区分历史不足和共同日期不一致，不会用零值伪造得分。每只股票的摘要还包含 `entry_eligible`、`gate_failures`、`entry_failure_reasons` 与 `exit_reasons`，明确列出未入选的门控原因、分数/复核条件和风险退出原因。`gate_failures` 使用稳定门禁名（如 `breadth_ok`、`liquidity_ok`）；`entry_failure_reasons` 额外解释所有布尔门禁通过但仍未入选的情况（例如 `score_below_entry_percentile`、`review_not_due`）。当缓存只有旧版 `bars` 投影、缺少完整且 accepted 的不可变 observation 时，API 会强制追加 `data_lineage_incomplete` 并将 `entry_eligible=false`。此时因子排名仍可用于诊断，但用户-facing 的 `research_action` 与 `recommendation.action` 会是 `insufficient_data`（原始模型动作保存在 `model_action`），并且 `data_quality.decision_ready=false`，避免把不完整数据误读为 BUY/SELL 或风险结论。板块 `coverage.analysis_ready` 表示纯因子模型成功计算的数量，兼容历史客户端；`coverage.quality_ready` 与 `coverage.decision_ready` 才表示具备 accepted 不可变观测证据的数量。每只结果同时返回 `analysis_ready`、`decision_ready_reason` 和 `calculation_input`，避免把“有排名”误读为决策级数据。研究标签不是订单或收益承诺。

银行 `analysis.indicators` 仅填充与银行模型口径一致的 60/120 日指标（比例小数），通用的 `return_5`、`return_20`、`annualized_volatility_20` 保持 `null`，不会把不同观察窗口冒充成 20 日指标。`factor_percentiles`、`factor_weights`、`factor_contributions` 和 `score_thresholds` 可用于复算页面的评分拆解；贡献分必须等于分位数乘权重，合计才是综合分。

每只银行分析还返回顶层 `data_provenance`，其中 `calculation_input` 与 `data_quality` 的血缘字段保持一致；`data_quality.analysis_readiness_failures` 始终是数组。前端以该数组区分“因子可计算”与“研究条件满足”，缺少 accepted 不可变观测时会保守显示未就绪，不能将缓存排名理解为可执行结论。

浏览器银行板块面板会逐行显示 `freshness`，并在总览说明中汇总 `fresh` 之外的 `stale`、`missing`、`future_timestamp` 和 `unknown` 数量。只要存在未能验证时间戳的行，说明文字会降级为“行情新鲜度待核验”并以提示色呈现；这与绿色的本地 API/供应商连接状态相互独立，避免把成功读取 SQLite 缓存误读为实时行情。该提示是只读的，不会触发上游请求。

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

报价对象包含领域字段 `symbol`、`timestamp`、`last`、`open`、`high`、`low`、`previous_close`、`bid`、`ask`、盘口量、成交量、成交额和 `source`，并增加看板兼容字段 `name`、`price`、`prev_close`、`change`、`pct_change`、`amount`、`updated_at`、`data_age_seconds`、`stale`、`freshness`、`future_timestamp`、`future_by_seconds`、`timestamp_semantics`、`volume_semantics` 和 `field_availability`。AShareHub 的 `timestamp` 使用响应中的 `trade_time`；`volume`/`turnover` 是当日累计股数/人民币元。当前供应商文档没有五档盘口，因此 `bid`、`ask` 及盘口量保持空/零并由 `field_availability.order_book=unavailable` 明示，不伪造价格。

SQLite 读取会按当前 provider 的 `source` 过滤。因此 AShareHub 无可用数据时不会返回旧 AllTick/AKShare 行；这类响应为空并伴随健康状态降级，比混源旧值更可信。

`GET /api/v1/watchlist` 还返回 `configured_count`、`missing_symbols`、脱敏的
`provider_status` 和 `collector_error`，用于区分“没有配置股票”与“已配置但没有收到行情”。
浏览器连上本地 API 或本地 `/ws/market` 不代表 AShareHub 已返回新快照；AShareHub 没有该股票 WebSocket。实际状态以 provider 的最近成功时间、缓存新鲜度和日预算字段为准。

### `GET /api/v1/quotes`

查询最新报价。可选查询参数：

- `symbols`：逗号分隔证券代码；省略时使用配置自选列表。
- `refresh`：默认 `false`，只读 SQLite；显式设为 `true` 允许调用 provider 获取报价。
  仍遵守其缓存、休市策略和持久化预算，不是强制绕过缓存。

示例：

```text
GET /api/v1/quotes?symbols=600519.SH,000001.SZ
```

成功响应保留 `items`，并额外返回 `count`、`requested_count`、`fresh_count`、`stale_count`、
`future_timestamp_count`（供应商时间戳领先本机时钟超过 5 秒的记录）、
`requested_symbols`、`received_symbols`、
`missing_symbols`、最新事件时间 `as_of`、`source`、`served_from`、`refresh_requested`、
`upstream_requests` 和 `market_status`。不在自选列表中的合法代码也可查询，但若数据库没有
数据，会出现在 `missing_symbols`。默认请求始终只读本地缓存。刷新也先校验入库再读取
最新持久化视图，因此 `served_from` 始终为 `sqlite_cache`，不代表此次没有调用 provider。
`provider_operations` 是适配器调用次数，不是 HTTP 请求次数；`upstream_requests` 对默认
读取和 mock 为 `0`，对真实源刷新为 `null`（没有请求级 HTTP 收据，不能精确归因）。
缓存可能使调用不发起 HTTP，而重试、交易日历刷新可能产生多次 HTTP；不要用全局预算差值
推算单次刷新的消耗，因为历史采集可能并发运行。当前日预算仍以 system/status 为准。

刷新时 `refresh_result` 独立描述本次适配器响应：`status=complete/partial/empty`，
`requested_symbols`、`received_symbols`、`accepted_symbols`、`missing_symbols`、
`stale_symbols` 和 `cache_fallback_symbols`。这里的 received/accepted 可能来自 provider
内存缓存，不能解释成新收到的网络行情。旧缓存可以继续展示，但不会计作本次刷新成功；
每条报价仍保留原始时间戳和新鲜度。默认不刷新时 `refresh_result=null`。

三类报价刷新路由与后台采集共用校验/入库链路：非 Quote、重复证券、未请求证券、混源及
超前本机超过 5 秒的异常快照整体返回 `503 provider_unavailable`，不改库、不发布事件。
刷新允许部分证券缺失；早于当前持久化快照的行会丢弃并记录在 `stale_symbols`。
同一 repository 内的时间水位检查和写入在同一个锁范围内完成，仍不支持多个采集主进程。
只有成功持久化的行才发布本地 QUOTE 事件并更新监控快照；API 刷新不会自动重跑策略。

### `GET /api/v1/market/quotes`

读取 provider 的额度安全行情范围，`/api/v1/quotes/batch` 是兼容别名。默认只读 SQLite，分页、排序、搜索和页面刷新都不会消耗供应商额度；显式 `refresh=true` 才允许刷新目录和行情。响应的 `universe_scope` 明确范围：AShareHub 与 AllTick 均为 `configured_symbols`，防止把完整证券目录隐式扩成数千只实时报价；证券目录接口本身仍为 `provider_catalogue`。报价响应的刷新语义与 `/quotes` 相同，最终视图始终来自 SQLite；`quote_provider_operations` 和 `catalogue_provider_operations` 分别记录报价与目录调用次数，两者之和为 `provider_operations`，不等于网络请求次数。

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

成功响应的 `total` 是筛选后的可用报价总数，`universe_total` 是当前 `universe_scope` 中的沪 A 股票数，`unpriced_count` 表示无价而被宽松批量策略跳过的行；`items` 是当前页，`as_of` 是最新供应商事件时间。AShareHub 响应还包含 `provider_diagnostics.quote_response_quality` 聚合诊断，用于区分上游部分返回和字段校验丢行：

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

AShareHub provider 还会将响应中早于进程内最新快照的报价计入
`provider.realtime.last_quote_stale_rows`，并从本次响应中剔除。旧行不会覆盖内存缓存，
也不会刷新该证券的缓存时间；严格报价读取会以缺失数据失败，宽松读取只返回其余证券，
避免上游乱序/重放把过时价格误标为最新值。`last_quote_duplicate_rows` 仅统计同一响应内
重复证券，不能将其解释为多笔独立成交。

### `GET /api/v1/quotes/{symbol}`

返回单个证券的最新报价对象；代码合法但无记录时返回 404。响应保留与批量接口
相同的平面报价字段，并额外返回 `requested_symbol`、`as_of`、`freshness`、
`served_from`、`refresh_requested`、`upstream_requests` 和 `market_status`。其中
`freshness` 为 `fresh`、`stale` 或 `future_timestamp`，与报价对象的 `stale` 布尔字段一致；
报价对象同时提供 `future_timestamp` 和 `future_by_seconds`，避免供应商时钟异常被误判为新鲜；过期记录
不会被伪装成实时数据，也不会因为过期而静默删除。该详情接口默认只读 SQLite，不会因
页面查询消耗 AShareHub 的请求额度；显式 `refresh=true` 才允许调用 provider，响应增加与
批量接口相同的 `refresh_result` 和适配器操作统计，不承诺绕过缓存或恰好一次 HTTP。
读取和写入在服务端工作线程执行，避免 SQLite 磁盘等待阻塞 WebSocket 和健康探针。

```json
{
  "symbol": "600519.SH",
  "requested_symbol": "600519.SH",
  "price": "1499.87",
  "timestamp": "2026-08-12T10:00:00+08:00",
  "as_of": "2026-08-12T10:00:00+08:00",
  "stale": false,
  "freshness": "fresh",
  "served_from": "sqlite_cache",
  "upstream_requests": 0,
  "market_status": {"is_trading": true, "is_trading_day": true}
}
```

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
  "count": 1,
  "observation_count": 1,
  "observation_total": 1,
  "observation_authority": "immutable_observations",
  "data_provenance": {
    "projection": "sqlite_bars_v1",
    "authoritative": false,
    "served_item_count": 1,
    "immutable_observation_count": 1,
    "legacy_item_count": 0,
    "lineage_complete": true,
    "quality_status_counts": {"degraded": 1},
    "warnings": []
  }
}
```

`from > to` 返回 422。真实 provider 默认严格读取 SQLite，不会因为页面访问而隐式消耗额度；只有显式传入 `refresh=true` 才会尝试调用 provider 并写库。显式刷新通过统一采集编排器落库，会同时记录 collection batch（以及 provider 能提供时的逐条 observation），可在 `/api/v1/lineage/batches` 审计；响应中的 `served_from` 为 `sqlite_cache` 或 `provider_refresh`，主动刷新还会返回 `collection_batch_id` 和 `collection_transport`。AShareHub 不提供分钟 K 线，`1m`/`5m` 等请求会被拒绝，而不是用日线或合成数据冒充。显式刷新时若认证失败、429、软预算耗尽或上游不可用则返回 503。
响应中的 `observation_authority` 明确说明返回的是完整不可变 observation 支撑，还是仅有旧版 `bars` 投影；后者不能单独通过研究血缘门禁。`data_provenance.authoritative` 始终为 `false`：即使 projection 的每行都能匹配 observation，也不能把兼容投影当作权威数据集。`data_provenance` 按 period/source 精确匹配并统计缺失行；`observation_count`/`observation_total` 仅统计实际匹配的 observation 行，不会把缺失数据填成 0。
兼容投影审计使用单次精确窗口查询，并检查窗口内所有 raw 修订；`observation_scan_total`、`observation_scan_count` 和 `observation_scan_truncated` 会披露是否因有界扫描而无法证明完整性。修订冲突、重复周期、混合来源或质量/可用时间不一致时，`lineage_complete`/`quality_complete` 会保持为 `false`，不会因“最新一条”排序而掩盖异常。

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
`data_provenance.calculation_input` 与 `data_quality.observation_authority` 表示
实际计算输入：`immutable_observations` 或 `legacy_bars_projection`。接口按代码、
周期、来源和请求窗口一次读取所有 raw 修订，不预先过滤质量；同一组记录同时用于
数值核对、质量/可知时间校验及转换，避免验证后又读取另一版本。
相同价格但含非 accepted 修订、不同价格修订、未来可用/接收/持久化时间均不能
通过输入检查。扫描超过 10000 条时返回 `observation_scan_truncated=true`，不把
部分扫描当成完整证据。`selected_observations` 记录实际选中的批次、修订和时间；
这些只证明本地读取口径，`authoritative` 仍为 `false`。

`lineage_complete` 表示投影的每行都有匹配记录，`quality_complete` 还要求全部
候选质量与可知时间通过。`analysis_ready` 另外要求指标齐全、历史充足、行情新鲜且
没有未来时间戳或日历覆盖异常；`analysis_readiness_failures` 列出未满足的条件。
条件不满足时 `research_action` 和 `recommendation.action` 为 `insufficient_data`，
原始模型动作保存在 `recommendation.model_action`。条件满足时只允许显示研究分类，
不表示投资建议：当前没有独立决策认证，因此 `decision_ready` 始终为 `false`。
上述数据库读取与指标计算在工作线程中执行，不阻塞 ASGI 事件循环；真实 provider
分析请求仍只读缓存，不消耗上游额度。

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
- `offset`：非负分页偏移，默认 0，最大 1,000,000。

响应为 `{"items": [...], "count": N, "total": T, "limit": 50, "offset": 0, "has_more": false}`。
其中 `count` 是当前页条数，`total` 是过滤后的总数，`has_more` 表示是否还有下一页。
对象包含 `id`、`symbol`、`timestamp`、`level`、`title`、`message`、`category`、确认状态和 metadata，并增加 `severity`、`created_at` 兼容字段。标题、正文、分类与 metadata 在输出时扫描凭据及本机路径；不安全文本显示 `[redacted]`，不会原样公开策略或采集异常中的认证信息。存量 SQLite 记录也经过同一输出检查。

### `PATCH /api/v1/alerts/{alert_id}`

设置确认状态。JSON 请求体：

```json
{"acknowledged": true}
```

字段省略时默认为 `true`；`acknowledged` 必须是 JSON boolean，字符串
`"false"` 等类型会返回 422，不会按 Python 真值规则隐式转换。成功返回
`{"id": 1, "acknowledged": true}`；ID 不存在返回 404，JSON 无效返回 422。

### `GET /api/v1/signals`

查询研究信号：

- `symbol`：可选证券代码。
- `strategy`：可选策略稳定名称，精确匹配。
- `action`：可选 `buy`、`sell` 或 `hold`。
- `limit`：1–500，默认 50。
- `offset`：非负分页偏移，默认 0，最大 1,000,000。

响应对象含 `items`、`count`（当前页）、`total`（过滤后总数）、`limit`、`offset` 和 `has_more`；每条记录含 `id`、`strategy`、`symbol`、`timestamp`、`action`（`buy` / `sell` / `hold`）、`strength`（-1 到 1）、可选 `price`、`reason` 和 metadata，并增加 `strategy_name`、`created_at`。

REST 列表、WebSocket 初始快照与 `signal` 事件共用 `research_assurance`：
`research_only=true`、`decision_ready=false`、`orders_supported=false`，以及
`input_lineage_status=unavailable`。当前实时 Signal 未绑定完整的不可变输入数据集，
因此策略自行声明的质量标签、策略代码摘要或 BUY/SELL 动作都不代表决策就绪。
`strategy_binding_status` 为 `missing`（旧记录无绑定）、`invalid`（描述符或摘要不符）
或 `digest_consistent`（代码/参数描述符摘要一致，**不代表来源认证或数据质量合格**）。
`action` 保留历史模型输出的语义；前端同时展示输入血缘和绑定状态，不能将其视为当前
交易建议。信号和告警的公开 metadata 还会递归隐藏 token、API Key 等敏感键。

### `GET /api/v1/strategies`

列出当前运行时加载的策略：

```json
{
  "items": [
    {
      "id": "moving_average_cross",
      "enabled": true,
      "type": "MovingAverageCrossStrategy",
      "mode": "signal_only",
      "evaluation_scope": "single_symbol",
      "price_basis": "close",
      "parameters": {"fast_window": 5, "slow_window": 20, "interval": "1d"},
      "code_identity": {"status": "unavailable"},
      "capabilities": {
        "signal_only": true,
        "orders_supported": false,
        "network_access": false,
        "parameter_schema_version": "1",
        "parameter_schema": [
          {"name": "fast_window", "type": "int", "required": false,
           "value": 5, "default": 5}
        ]
      }
    }
  ],
  "notice": "策略仅生成研究信号，不会提交订单。"
}
```

`parameters` 是运行时实际生效的只读阈值快照。银行多因子策略会额外返回
`min_history`、`min_universe`、进出场分位数、波动率/回撤上限、流动性门槛等参数，
可直接写入研究运行 manifest 以复现决策。`evaluation_scope` 为
`single_symbol` 或 `cross_sectional`，`price_basis` 标明策略使用的价格基准（没有
声明时为 `null`）。`capabilities` 是稳定的研究边界声明：当前所有策略均为
`signal_only`，`orders_supported=false` 且 `network_access=false`；其中
`parameter_schema` 列出构造参数的名称、类型、是否必填、当前值和安全的默认值，
`parameter_schema_version` 变化时客户端应重新校验缓存的 schema。接口不提供修改或
下单能力，客户端不得把 `buy`/`sell` 研究信号当作交易指令。

`code_identity` 是策略实现的只读内容地址描述。当前可独立回放的银行策略会返回
`schema_version`、固定白名单源码单元、Python 主次版本、实现类型和 `version`
（`sha256:<64位十六进制>`）；其他策略可能返回 `{"status":"unavailable"}`。创建研究
manifest 时应保存银行策略的 `version` 到 `strategy.code_version`。独立回放会重新计算
该值并拒绝旧人工标签或代码漂移；该摘要证明本地内容一致性，不等同于签名构建证明。

银行多因子分析/信号的 `metadata`（以及分析摘要条目）还包含
`factor_percentiles`、`factor_weights`、`factor_contributions` 和
`score_thresholds`。其中 `factor_contributions` 是“该因子分位数 × 权重”，
其总和等于 composite score；`rank` 使用 competition rank，同分股票共享名次，
不会用股票代码字典序制造虚假的优先级。`gates`/`gate_failures` 与
`exit_reasons` 分别解释入选门控和风险退出原因。

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
