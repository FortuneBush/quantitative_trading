# 行情采集血缘（schema v3）

schema v3 新增 `collection_batches` 与只追加的 `bar_observations`，同时保留
旧 `bars` 表作为现有 API、指标与策略的兼容投影。迁移不会把旧 `bars` 猜测性回填
为可信观测；旧记录缺少真实接收时间与发布证据，继续只存在于兼容表中。

## 时间语义

- `session_date`：行情所属的上海市场会话日期。
- `period_start` / `period_end`：K 线名义覆盖区间；日线当前按 09:30 到 15:00。
- `available_at`：本实例可以证明该版本最早可见的时刻。现有 provider 没有提供
  可验证的历史发布时间，因此适配后的保守值等于 `received_at`，绝不写成历史日期。
- `received_at`：采集服务实际收到并进入持久化流程的时刻。
- `persisted_at`：本地事务开始构造持久化记录的时刻。

当前约束为 `period_end <= available_at <= received_at <= persisted_at`。缺少可用时刻的
未来显式导入可使用 `available_at=NULL` 并标记 `untrusted_legacy`，但不会进入可信研究门禁。

## 批次与事务边界

采集服务在发起网络请求前先插入 `running` 批次，网络请求不占用数据库事务。成功后，
`bar_observations`、旧 `bars` 投影、指标以及批次 `succeeded` 终态在同一 SQLite 事务提交；
提交之后才发布进程内事件。网络异常记录 `failed` 与异常类型，但不保存可能包含 URL 或
凭证的异常正文。取消记录 `cancelled`；进程中断残留的 `running` 可由
`SQLiteRepository.abandon_running_collection_batches()` 标成 `abandoned`。取消路径会先
等待后台 SQLite 写入线程排空，再向调用方传播 `CancelledError`，因此服务关闭或 Windows
文件锁等待期间不会把批次留在永久 `running` 状态；重启恢复仍将未终态批次保守标成
`abandoned`，不会自动重放供应商请求。

网络响应的 `observations` 可以比 `bars` 兼容投影更宽（例如投影受 `limit` 截断），
投影之外的观测仍会完整留在 append-only 表中；但两者在同一
`(symbol, interval, period_end)` 重叠的行必须逐字段相同（包括 OHLCV 与 source）。
采集器在事务前执行该一致性检查，发现冲突即把批次标为 `failed`，不写入任一投影、
指标或观测，避免后续计算读取旧 `bars` 而血缘却显示另一份“已接受”值。

每个 K 线批次的 `input_hash` 同时覆盖规范化的代码、周期、条数、交易日历身份以及
公开请求窗口（例如 `start`/`end`）；`metadata.request` 保存同一组可复现坐标，便于
研究运行回放。采集编排器会在调用 provider 前递归移除 `token`、`api_key`、
`authorization`、`secret` 等敏感键，因此请求参数和血缘接口不会成为凭证泄漏路径。

## 身份、复权与质量

观测主键包含 `batch_id`、证券、周期、会话日期、区间结束、来源、复权口径和
`revision_id`，因此不同来源、`raw/qfq/hfq` 及修订可并存。同一身份重复写入会硬失败，
不会用 `ON CONFLICT DO NOTHING` 掩盖内容冲突。由现有 `Bar` 转换的观测默认
`quality_status=degraded`，因为 provider 尚未返回可验证发布时间、修订序列或签名。

网络型 `BarFetchResult` 还可以提供 `response_content_hash`。该值是供应商返回的
**解析后 JSON**（排序键、紧凑 JSON、SHA-256）的确定性摘要，和 `transport=network`
一起写入批次 metadata。它让重试、供应商修订和离线复核可以比较同一响应，而不把原始
响应正文、URL、请求头或 API Key 写入 SQLite。它不是供应商签名，也不能单独证明数据
权威性；`response_content_hash_scope` 会明确标为 `parsed_json_response`，没有适配器支持
时为 `unavailable`。缓存命中不会伪造新的摘要。

血缘门禁的检查值必须是实际布尔值；字符串或数字不会被转换为通过/失败。报告中的
`verified` 仅由这些检查派生，调用方不能通过提交自报标志提升证据等级。

血缘门禁还会把 supplied collection batch 与实际观测做一一覆盖校验：每个观测必须来自
一个成功批次，且该批次的持久化 `item_count` 必须恰好等于本次证据中的观测行数；额外、
重复或缺失批次都会使 `batch_item_count_matches=false`。这样仅篡改批次元数据或拼接其他
运行的观测不会被误判为同一输入快照。若存在多个响应收据，门禁也会拒绝互相冲突的
`response_content_hash`。

采集服务现在还会把注入的 `TradingCalendar` 用于新收到的日线观测：已知非交易日的
行（例如周末）标记为 `quarantined`，有限日历覆盖范围之外的日期也标记为
`quarantined`（原因分别为 `off_calendar_session` / `calendar_coverage_unknown`）。
这些行仍写入 `bar_observations` 和兼容的 `bars` 视图，便于审计供应商异常，但不会
计算指标、发布 BAR 事件或驱动实时策略。批次 metadata 会记录日历身份、指纹、隔离
数量及原因。缓存命中且没有本次新观测时保持兼容行为，后续质量审计负责识别历史
缓存中的异常行。

默认 AShareHub 运行时会把同一份已加载的 SSE/SZSE 日历快照转换为有限
`VersionedSessionCalendar`，采集器、日线质量 API 和缓存回测均从运行时共享该视图。
若任一交易所行缺失、缓存过期或查询日期超出快照覆盖，三者保持
`weekday_fallback` 并将 `calendar_fingerprint` 留空；这属于可观察的降级，而不是
把不完整的供应商响应拼成“官方日历”。

通用分析接口现使用有界的 observation 选择器：对请求窗口一次读取所有 raw 修订，
在同一组对象上核对价格、质量和可知时间后再转换为计算输入。完整且 accepted 的
无冲突记录可用于本地研究；冲突、未来接收时间或超过 10000 条扫描上限会返回
兼容投影并明确降级。该路径已由真实 SQLite 和 API 成功/失败测试覆盖，不会访问
供应商；`analysis_ready` 还要求数据新鲜和指标齐全，`decision_ready` 保持为 false。
这仍以兼容投影定位研究窗口，尚不等同于独立的 point-in-time 权威数据集查询。

目前这是最小正式基础：observations 已通过只读 lineage API 暴露，但尚未切换为行情 API
的唯一权威读取源，也未实现供应商
原生 revision/发布时间映射。`bars` 的旧唯一键仍会覆盖不同来源，因此启动脚本仍使用
provider 独立数据库；研究门禁不能因为 schema v3 存在就自动通过。
