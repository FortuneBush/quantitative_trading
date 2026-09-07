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
`SQLiteRepository.abandon_running_collection_batches()` 标成 `abandoned`。

## 身份、复权与质量

观测主键包含 `batch_id`、证券、周期、会话日期、区间结束、来源、复权口径和
`revision_id`，因此不同来源、`raw/qfq/hfq` 及修订可并存。同一身份重复写入会硬失败，
不会用 `ON CONFLICT DO NOTHING` 掩盖内容冲突。由现有 `Bar` 转换的观测默认
`quality_status=degraded`，因为 provider 尚未返回可验证发布时间、修订序列或签名。

目前这是最小正式基础：尚未把 observations 切换成 API 的权威读取源，也未实现供应商
原生 revision/发布时间映射。`bars` 的旧唯一键仍会覆盖不同来源，因此启动脚本仍使用
provider 独立数据库；研究门禁不能因为 schema v3 存在就自动通过。
