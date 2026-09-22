# ResearchRun manifest 与稳定数据哈希

`quant_monitor.research` 是纯标准库、无网络的研究血缘组件。它把一次研究声明的输入固定为不可变 manifest，并生成稳定 SHA-256 内容哈希；SQLite v9 以不可变记录保存清单、回测结果制品、基本面事实、公司行动与复权因子、PIT 股票池快照，持久化采集作业幂等摘要，REST API 提供只读查询。公司行动/复权/PIT 股票池查询按 `as_of` 时间截断，不会因迟到修订污染历史研究。

## v2 规范化契约

`CANONICALIZATION_VERSION` 当前为 `quant-monitor-canonical-json-v2`，manifest schema 为 `2`。v2 是与 v1 不兼容的哈希域升级；旧 v1 摘要不能与 v2 摘要直接比较。

- JSON 原生 `null`、布尔值、整数、浮点数、字符串、映射和列表保持各自语义。
- 非 JSON 原生语义通过保留字段 `$quant_monitor_type` 编码：`Decimal`、`date`、带时区 `datetime`、无时区 `time`、`Enum`、tuple、set、frozenset 和 dataclass 均有不同类型标签。用户映射若包含同名保留字段，会整体转义为 tagged mapping，不能伪造类型值。
- 因此 `Decimal("1.23")` 与字符串 `"1.23"`、`date` 与 ISO 字符串、tuple 与 list、set 与 frozenset、`True` 与整数 `1` 均产生不同 JSON 和 hash。
- 映射按字符串键排序；实现只快照一次 `Mapping.items()`，不会先读 keys 再二次取值。集合按每个成员的规范 JSON 排序。输入顺序和 `PYTHONHASHSEED` 不会改变结果。
- `Decimal` 用不依赖进程 decimal context 的 coefficient/exponent 表示。等值 Decimal（例如 `1.2300` 与 `1.23`）得到同一编码。
- 带时区 `datetime` 统一到 `Asia/Shanghai`；任何携带 `tzinfo` 的 `time` 都被拒绝，避免只有时间却无日期时 DST/偏移语义不确定。非有限数、无时区 datetime、不支持对象、循环引用、NUL 和 Unicode lone surrogate 也会失败。
- manifest 构造时会把 `datetime` 冻结为已规范化的时间点字符串，不保留调用方的 `tzinfo` 对象；即使自定义 `tzinfo` 之后被修改，`content_dict()` / `to_json()` 也不会与已经生成的 `content_hash` 脱节。`date`、无时区 `time` 和 `Decimal` 本身是不可变值，也会在构造时完成校验。
- `hash_input_records(records)` 将记录视作多重集：逐条规范化后排序，因此供应商批次顺序无关，但增删、修改或重复记录都会改变摘要。

dataclass 和 Enum 的类型标签使用 `module + qualname`，并同时哈希字段/成员语义。这能区分正常代码中的不同公开类型，但不是 Python 运行时类型真实性证明：动态代码可以刻意创建相同 `module + qualname` 的不同类。跨代码版本或插件边界需要调用方在描述符中提供并治理显式 schema/code version，不能只依赖运行时类名。

`DatasetDescriptor.record_count` 必须是精确的 `int`，布尔值、浮点数、字符串和 Decimal 都不会被隐式转换。所有 identifier、`input_hash` 和 `hash_canonical(..., namespace=...)` 的 namespace 必须是字符串；不会对任意对象调用 `str()`。

## 不可变性与输出

`DatasetDescriptor` 和 `ResearchRunManifest` 会防御性复制并冻结所有嵌套值，同时保留 list、tuple、set、frozenset、Enum 与 dataclass 的原始语义。`to_dict()` 返回的是独立、规范化后的 JSON 树，允许调用方修改而不影响 manifest；非 JSON 原生值以 v2 类型标签呈现。

`run_id` 是外部运行身份，不参与 `content_hash`；相同内容可分配不同 UUID 或持久化编号而不破坏内容寻址。完整 JSON 同时包含 `run_id` 和 `content_hash`。

## 安全门禁

manifest 不是秘密存储。扫描同时应用于每个映射键和字符串值，并最多处理双层 URL 编码。以下内容会在哈希或序列化前失败：

- API key、token、Authorization、cookie、password、client secret、private key 等凭据字段，包括 camelCase、分隔符变体及常见中文“密钥/秘钥/密码/口令/令牌/授权/凭据”名称；
- 敏感字段匹配会先做 Unicode NFKC 安全规范化并移除零宽格式字符，因此全角拼写或 `pass\u200bword` / `to\u200bken` 之类不可见分隔不能绕过；规范化仅用于门禁，不改变输出中的普通文本。
- Bearer/Basic 认证值、API key 赋值或说明形式、私钥头、常见供应商 key 形态，以及 JSON 文本内嵌的敏感键值；
- URL userinfo 与敏感 query 参数，包括 URL 编码形式；
- Windows drive/root/UNC、POSIX、`~/` 和 `file:` 绝对本机路径，包括编码形式；
- NUL 和 Unicode surrogate。

异常仅报告结构路径或错误类别，绝不回显被拒绝值。扫描有意允许明显无害的研究描述，例如 `Basic research baseline`、`Bearer bond model`，以及 `token_count`、`prompt_token_count`、`tokenization` 等统计字段。

调用方仍须只传规范化且获准保存的数据。哈希摘要不会让受许可限制的行情变成可再分发数据，也不能用于恢复 API Key。

## 当前可信度边界

`content_hash` 只能证明“这些规范化值和声明版本完全相同”，不能单独证明数据来自供应商、未被篡改、完整覆盖历史或在当时已经可用。当前 AShareHub 数据仍缺少平台验证的供应商签名/原始响应证据、独立 `available_at` / `received_at` 血缘、官方版本化交易日历、由供应商填充的 point-in-time 银行股票池、公司行动/复权因子和完整历史交易状态。

## Point-in-time 股票池

`quant_monitor.research.PointInTimeUniverse` 是无网络、无数据库副作用的股票池解析器。
调用方必须提供版本化的 `UniverseMembership` 行：`effective_from/to` 表示成员关系
在市场日期上的有效区间，`known_at` 表示该事实最早可被研究使用的时间，`active` 可
用于保留退市/分类修订而不删除历史。`universe_at(session_date, knowledge_cutoff)`（与
`members_at()` 等价）先
过滤两个时间条件，再对同一代码选择截止时刻已知的最新修订；因此新增的未来成员或
未来才知道的分类修订不会泄漏到历史回测。省略 `knowledge_cutoff` 会直接失败，避免
误把今天的证券目录当成历史股票池。

同一代码在最新 `known_at` 上若有相互冲突的有效断言，解析会失败关闭；不会按
`revision_id` 或版本字符串的字典序猜测哪个才正确。只有事实相同、仅修订/版本标签
不同的重复断言才允许稳定裁决。冲突校验发生在会话日和知识截止时间过滤之后，因此
未来才可见的冲突不会污染过去查询。交易所必须与代码后缀一致，来源、版本和分类等
文本字段不能把 `null`、布尔或数值悄悄转换为看似有效的字符串。

`snapshot_at()` 返回按代码排序且内容寻址的 `UniverseSnapshot`。其 `content_hash`
覆盖会话日、知识截止时间、股票池版本和全部成员，可直接作为
`ResearchRunManifest.universe_version`（或写入 manifest 的 assumptions）的一部分。
若持久化层先拿到成员列表再生成快照，应使用同一解析器的
`snapshot_from_members()`；它会拒绝不属于该截止时点解析结果的目录行或伪造修订，
避免调用方把当前目录误绑定到历史运行。
直接构造或导入快照也会核对成员的 `active`、有效区间和 `known_at <= knowledge_cutoff`；
即使调用方重新计算了正确的 hash，也不能把未来、已失效或非活跃成员伪装成有效快照。
回测引擎对多会话 `PointInTimeUniverse` 采用更严格的决策时点门禁：共享的
`universe_knowledge_cutoff` 必须不晚于最早回测会话的 `daily_evaluation_time`
（默认上海时间 15:30），否则直接失败关闭。解析器本身仍允许较晚截止时间用于离线
历史审计；只有在回测边界才禁止这种可能造成未来信息泄漏的用法。需要滚动知识时点时，
请为每个会话传入已经解析好的 `UniverseSnapshot`，或传入已从 v9 SQLite
快照读取的 `{session_date: UniverseSnapshot}` 映射；不要用一个未来截止时间覆盖整段回测。
该模块只定义并校验契约；AShareHub 当前目录没有历史成员关系，因此启动兜底的静态
42 只银行名单仍标记为监控用途，不会自动获得可信回测等级。

因此，可以把 `source="asharehub"` 和版本字符串写入描述符，但它们仍是调用方声明，不能据此把回测提升为可信研究等级。后续只有采集层建立受控批次证据，并由数据血缘校验器自动核验版本后，manifest 才能成为平台级可信门禁的一部分。

### 持久化 PIT 快照

已经解析并校验的 `UniverseSnapshot` 可以通过
`SQLiteRepository.insert_universe_snapshots()` 追加到 v9 的
`universe_snapshots` 表。存储身份由 `(universe, session_date,
knowledge_cutoff, version)` 内容寻址；相同内容重复写入是幂等的，复用身份但
内容 hash 不同会失败关闭。该方法不会从当前 `instruments` 目录自动生成历史快照。

只读接口 `GET /api/v1/research/universes/as-of?as_of=...`（兼容别名
`/api/v1/universes/as-of`）只返回同时满足 `knowledge_cutoff <= as_of` 与本地
`recorded_at <= as_of` 的快照，且标记 `served_from=sqlite_cache`、
`provider_operations=0`。不存在已持久化的历史证据时返回空结果，而不是回退到当前
供应商目录；晚到导入的旧资料不会被倒灌进更早的研究时点。

## 回测研究门禁（fail-closed）

`BacktestDiagnostics` 现在会在构造时生成只读的 `decision_grade_checks` 和
`decision_grade_failures`。检查项包括日历、行情可用性、执行价可用性、公司行动
血缘、约束覆盖率、成交可交易性和诊断告警。当前回测内核收到的版本号、来源和时间
描述都属于调用方声明，缺少独立采集证据，因此前四项永远不会因字符串看起来“官方”
而通过；`decision_grade` 也继续被强制为 `false`。这组字段用于解释降级原因，不是
让调用方自我认证的开关。

结果一致性还会严格校验 `execution_attempt_count` 是否等于真实成交数加上有明确
尝试时间的跳过成交数。篡改或截断诊断计数会在构造 `BacktestResult` 时失败，而不是
在页面上悄悄显示为可信结果。未来接入受控、可验证的供应商血缘时，应由独立校验器
产生证据后再替换相应检查，不能直接放宽模型的 fail-closed 默认值。

对于已经落入 v3 `bar_observations` 的数据，可使用纯函数
`quant_monitor.research.audit_observation_lineage()` 做更严格的输入审计。它同时核对
观测存在性、精确条数、输入 hash、来源、`accepted` 质量、`available_at`/`received_at`
不晚于决策时点、修订键唯一性以及成功采集批次；缺少任一上下文时报告会保持
`verified=false`。该审计不访问网络、数据库或时钟，也不会自行把回测
`decision_grade` 提升为 `true`，调用方必须把报告和 run manifest 一并保存。

## SQLite 持久化与 API

`SQLiteRepository.create_research_run()` 只允许插入新的 `run_id`，不会更新或覆盖既有
清单；数据库触发器也会拒绝 UPDATE/DELETE。落库时同时保存规范化 manifest、内容 hash
和 manifest JSON hash，读取时同时重新校验序列化摘要、manifest schema/purpose 及内容哈希，
因此即使攻击者重算文本摘要，也不能把修改后的策略参数伪装成原运行。`GET /api/v1/research/runs` 支持策略、
数据集、时间和分页过滤，`GET /api/v1/research/runs/{run_id}` 返回完整清单。接口只读
本地数据，不触发 AShareHub 请求；列表项还会返回 `result_available`，用于区分已绑定结果
与仅有清单的运行。清单敏感字段和本机绝对路径会在构造阶段拒绝。

持久化记录还要求 `manifest_json` 与 `result_json` 均为平台 canonical JSON（UTF-8、无空白、
键排序且使用稳定分隔符）。即使格式化 JSON 在语义上等价，也会被拒绝；这样下载、归档和
离线签名只对应一种字节表示，`manifest_json_hash`/`result_json_hash` 不会出现多个可互换
的编码。结果内容 hash 仍按去除 `result_hash` 后的规范化对象计算，字节摘要则用于检测
存储或传输层改写。

目录查询、结果查询和不可变 manifest/result 写入均在 API 工作线程中执行；运行列表会在同一
工作单元内读取结果存在性，避免 SQLite 锁等待阻塞 WebSocket/健康探针，也避免列表页看到
清单与结果状态不一致的撕裂快照。

`quant_monitor.serialization.backtest_result_dict()` 可将内存回测结果导出为完整 JSON
研究制品，并生成 `result_hash`。哈希覆盖策略、配置、交易、跳过记录、持仓、净值、风险
指标和诊断字段；相同输入的离线回测应产生相同哈希。该制品不自动写库或提交订单，写入
研究运行表时仍应由调用方显式创建并绑定 manifest。绑定后的制品可通过
`POST /api/v1/research/runs/{run_id}/result` 写入、通过同路径 `GET` 读取；同一
`run_id` 只能绑定一个结果，重复提交会返回冲突。
服务端在固化结果前还会递归扫描 JSON 字段名和值，拒绝凭据/认证信息、绝对路径和
敏感嵌入 JSON；结果哈希不能绕过这道导出安全门禁。拒绝信息不会回显敏感值。
新结果的 `metrics` 还会报告已平仓交易分布：`closed_trade_count`、胜/负/保本笔数、
`win_rate`、`gross_profit`、`gross_loss`、`realized_pnl`、`profit_factor` 和单笔期望值；每笔卖出交易的
`realized_pnl` 已扣除买入成本、佣金及印花税，`metrics.realized_pnl` 是所有已平仓交易的净合计。
金额（包括 `expectancy_per_closed_trade`）使用 Decimal，JSON 序列化为字符串。
`trade_statistics_status` 区分 `available`、`no_closed_trades` 与 `unavailable`。
没有平仓交易时计数与金额合计为 0，但胜率、单笔期望和 Profit Factor 为 `null`；
有平仓且没有亏损样本时 Profit Factor 仍为 `null`，`profit_factor_status=no_losses`，
不能用 0 或无穷大冒充有限比率。存在亏损时 Profit Factor 为毛盈利 / 毛亏损，
并非平均盈利 / 平均亏损。前端用 `—` 和原因提示展示不可评估值。
也可以调用同一运行的 `/backtest` 子路径，让服务仅从 SQLite 日线缓存执行当前银行策略并
自动保存结果；该路径不会刷新行情，缓存不足或请求股票池不完整时默认明确失败，不会静默
用剩余证券生成横截面结果。明确的探索性调用可传 `allow_partial_symbols=true`，但返回的
`manifest_binding` 会披露请求/实际证券集合和 `requested_symbols_missing` 警告；该模式不能
与严格 `verify_manifest=true` 绑定同时使用。
响应中的 `manifest_binding.data_provenance` 会标明所用 SQLite `bars` 投影有多少行能按
周期结束时间和来源匹配到不可变 observation；`authoritative` 始终为 `false`，旧版或直接
写入的 projection 会在 `warnings` 中标记，不能仅凭回测成功宣称数据可独立重放。
对于需要可复核输入的运行，调用时加入 `verify_manifest=true`。严格模式仅使用 SQLite 缓存，
按规范化 Bar 记录重算并核对 `input_hash`、数据集 `record_count`/`source`、策略阈值、
回测配置以及清单声明的证券集合；严格执行时直接使用清单绑定的策略参数和回测配置，
不会因当前进程配置变化而改变执行口径；
校验失败会拒绝回测，不会写入结果。未启用时响应仍会明确给出
`manifest_binding.warnings=["manifest_input_hash_not_verified"]`，表示结果只能视为未绑定的本地快照。
如需单独审计更强的 v3 观察证据，可调用
`GET /api/v1/research/runs/{run_id}/lineage?decision_at=...`。该只读门禁会核对观察数量、
输入 hash、来源、质量状态、点时可用时间、修订键和成功采集批次；缺少 `decision_at` 或
任何证据时 fail-closed 返回 `verified=false`，不会修改既有回测结果或提升
`decision_grade`。
其中成功批次还必须与 observation 集合一一对应，且每个批次的 `item_count` 与实际行数
精确一致；多批次响应收据存在冲突时同样失败。这防止把数量被篡改的批次或其他运行的
观测拼接进来后仍通过研究血缘门禁。
保存后可调用 `/api/v1/research/runs/{run_id}/risk`，在不重跑策略、不访问供应商的情况下
计算历史 VaR/CVaR、尾部损失、回撤和持仓暴露。风险报告携带样本充足标志与数据诊断
warning；短历史不会被误报为低风险。

另可调用 `/api/v1/research/runs/{run_id}/integrity` 做指标一致性审计。审计会从同一结果制品
重算净值曲线的日收益与回撤、聚合收益/风险指标、成交费用和换手率，并按买入含费成本与
卖出净收入重建 FIFO 已实现盈亏；`verified=false` 时，
`mismatches` 会列出具体失败检查。结果 JSON 的哈希只能证明字节未被修改，不能证明指标由曲线
正确计算，因此正式研究归档建议同时保存该审计响应。该接口纯离线、不会消耗行情额度。
旧制品缺失的检查项列入 `unavailable_checks`，仍有检查通过不代表完整核验；审计不会回写
旧制品或改变其哈希。治理接口 `/governance` 使用 `research_governance_v2`，区分
`artifacts_consistent`（制品一致）、`reproducible`（独立重放验证）和 `decision_ready`
（全部决策证据通过）。治理接口会在完整不可变 observation 集合上对白名单银行策略执行
本地重放并比较完整结果 hash；严格重放要求每个采集批次保存与 manifest 日历版本一致的
`metadata.calendar.version`，仅有日历版本字符串而没有批次证据时
`replay_audit.calendar_binding_matches=false`。缺失指标检查覆盖、血缘或重放证据均明确披露。当前没有
受控的决策级认证，上传 JSON 的自报通过标志也不能提升等级。

## 逐期组合归因

已保存的回测结果可以通过 `POST /api/v1/research/runs/{run_id}/attribution`
做离线、现金感知的算术归因。请求体必须提供显式的 `benchmark_returns`，并可选提供
`cash_returns`；两者都可以是按回测会话顺序排列的收益数组，或以 `YYYY-MM-DD` 为键的
日期映射。服务只读取 SQLite 中不可变结果，不下载指数、不假设现金利率，也不重跑策略。

每期结果将 NAV 变化拆成基准贡献、现金贡献、已记录交易成本贡献和主动贡献；成本来自
结果制品中的佣金、印花税与滑点账本。缺失基准/现金会保留为 `null`，状态降级为
`benchmark_only` 或 `unavailable`，不会把缺失数据填成零。只有所有会话同时具备两类
显式收益时才会输出 `status=complete` 和主动贡献。该报告是研究解释工具，不代表收益预测、
交易建议或数据权威认证。

## 组合压力测试

`quant_monitor.backtesting.run_stress_test()` 接受一个已经完成的
`BacktestResult` 和显式的 `StressScenario` 列表，在内存中对每个净值观测的持仓市值
施加比例冲击，并可用 `cost_multiplier` 模拟佣金、印花税和滑点上升。例如：

```python
from quant_monitor.backtesting import StressScenario, run_stress_test

report = run_stress_test(
    result,
    [
        StressScenario("market_minus_10pct", price_shock="-0.10"),
        StressScenario("friction_2x", cost_multiplier="2"),
    ],
)
print(report.to_dict())
```

结果提供压力后期末净值、相对基准的净值变化、最大回撤和最低净值，并指出最差
期末净值场景。也可对 `backtest_result_dict()` 的 JSON 投影调用
`run_stress_test_from_payload()`；该路径不重跑策略、不访问 AShareHub，也不会消耗
请求额度。压力场景是研究假设，不是收益预测或交易建议；实际组合还应结合流动性、
涨跌停和交易状态约束进行解释。

## 成交容量审计

`quant_monitor.backtesting.audit_capacity()` 用执行日线的成交量和显式的
`CapacityAssumptions(max_participation_rate=...)` 对每笔历史成交做容量审计。它返回
实际参与率、按上限估算的可执行金额、超限笔数，并把缺少成交量或成交量为零的记录标为
`assessed=false`，而不是假设可以成交。容量审计是规模可实现性的警示层，不会修改原始
回测成交或净值，也不构成盘口级成交模拟；没有可靠的成交量时应保守解释结果。
容量 API 不依赖请求时的当前 provider：回放会把实际 K 线的 `bar_sources` 写入结果诊断，
报告优先按该来源读取本地成交量；旧制品才回退 manifest 的数据源，并在
`data_provenance.bar_source_origin` 中明确标记，避免切换数据源后把不同口径拼入同一审计。

## 样本外 Walk-forward 验证

`quant_monitor.backtesting.run_walk_forward()` 将显式会话按训练、验证、测试顺序切分。
每个阶段会保留该阶段结束前的历史 K 线作为指标 warm-up 上下文，但只把阶段会话
写入该阶段的净值曲线和执行账本；因此滚动指标不会在切分边界被重置，同时测试阶段
不会看到未来 K 线。若持仓在某一会话没有对应估值 bar，回测会 fail-closed 并拒绝
沿用旧收盘价，避免把停牌/缺数伪造成平稳净值。
每个候选策略工厂在各阶段创建全新实例；候选只按训练/验证得分选择，测试窗口只在选择
完成后运行，且所有失败候选都会保留在报告中。`WalkForwardConfig` 支持滚动或扩展训练窗、
步长、候选评分指标和训练最低分数；测试汇总包含复合收益、分位数、正收益折数比例和
平均 Sharpe。该模块只接受调用方提供的已规范化 K 线和交易日历，不访问数据库、网络或
券商，测试结果仍需绑定研究 manifest 才可复核。
已保存结果可通过 `/api/v1/research/runs/{run_id}/stress` 和
`/api/v1/research/runs/{run_id}/capacity` 调用同样的离线报告；两个端点均只读取 SQLite，
响应明确标记 `upstream_requests=0`。

多个运行可用 `GET /api/v1/research/runs/compare?run_ids=id1,id2` 并排查看。该接口返回
版本指纹、标签、结果哈希、核心绩效指标和决策等级；有结果的比较行会对客户端自报的
`diagnostics.decision_grade` 保持 fail-closed，服务端 `decision_grade` 固定为 `false`，
原始自报值只在 `declared_decision_grade` 披露，并以 `decision_grade_unattested` 标记证据缺口。
未绑定结果的运行会明确标记为不可用，不会把缺失值填成零，也不会触发新的行情请求。

网页仪表盘的“研究运行与结果对比”面板复用了运行列表与 compare 两个只读接口：启动和刷新时仅加载
最近 20 条本地清单，用户勾选运行后才请求比较结果。面板中的“尚无结果”状态表示
清单已保存但尚未绑定不可变结果，不能据此推断策略收益为零。每条运行还可直接下载离线
研究包；已绑定结果时可单独下载规范化结果 JSON，两个下载动作均只读取 SQLite，不消耗行情源额度。
已绑定结果的“查看报告”按钮会在用户明确点击后并行读取同一 `run_id` 的
`/result`、`/risk`、`/integrity` 和 `/governance` 四个只读端点（请求带
`cache-control: no-store`，不会刷新行情）。报告只接受响应共同携带的 `run_id`/`result_hash`；
风险、审计和治理响应还必须满足 `served_from=sqlite_cache`、`upstream_requests=0`。
治理响应会在报告中展示制品一致性、独立重放、决策就绪及 PIT 股票池证据状态，但不会把
治理证据渲染成交易许可。任一端点失败、超时、哈希不一致或
风险样本缺失都会单独显示，不会用旧报告或零值拼接。风险门禁通过且样本完整时才显示
VaR/CVaR 数值；这仍然不等于数据权威、策略有效或可交易，报告明确保留日历、公司行动、
复权、成交可行性等研究限制。关闭/切换运行会取消旧请求，页面不会自动轮询该报告。

下载的研究包可以在脱离服务的环境中做完整性复核：

```powershell
.venv\Scripts\python.exe scripts\verify_research_export.py .\research-run.json
# 归档门禁：清单没有绑定不可变结果时返回非零退出码
.venv\Scripts\python.exe scripts\verify_research_export.py .\research-run.json --require-result --json
```

校验器只重建 manifest、结果 canonical JSON 及其 SHA-256 内容/字节摘要，不访问 SQLite、
行情 provider 或网络；它不会把 hash 通过解释为数据权威或策略有效。没有结果的运行会报告
`incomplete`，而不是伪造空结果；`--require-result` 可用于备份归档任务的失败门禁。

组合层提供 `quant_monitor.backtesting.build_portfolio_snapshot()` 和
`portfolio_snapshot_from_result()`。它们只接受本地回测持仓，强制校验
`现金 + 持仓市值 = NAV`，并输出现金权重、单票最大权重、集中度 HHI、结算数量和估值质量。
快照是不可变研究对象，不包含任何券商或下单接口。

点时点基本面使用领域对象 `quant_monitor.domain.FundamentalObservation`。每条记录同时
保存报告期、`available_at`、接收时间、来源和修订号；`fundamentals_as_of()` 只选择在决策
时点已经可用的最新修订，同一时刻的修订按 revision ID 稳定排序，缺失指标保持缺失。
当前 AShareHub provider 尚未提供这些字段，因此它们不会被策略自动假设或填充。

公司行动账本函数 `quant_monitor.domain.apply_corporate_action()` 可在离线回放中显式处理
现金分红、送股、拆并股和配股；配股默认不认购，认购时会校验现金是否足够。函数返回不可变
`CorporateActionEffect`，保留行动 ID、旧/新数量、现金变化和权利认购状态；它不会修改原始
K 线或连接券商，也不负责交易所特定的零股/税费取整。

## 离线示例

```python
from quant_monitor.research import (
    DatasetDescriptor,
    ResearchRunManifest,
    hash_input_records,
)

records = normalized_bars  # 已规范化领域对象；本模块不会自行取数
input_hash = hash_input_records(records)

manifest = ResearchRunManifest(
    run_id="run-local-001",
    strategy_name="bank_multifactor",
    strategy_parameters={"minimum_universe": 15},
    strategy_code_version="bank-multifactor-v1+git.0123456",
    dataset=DatasetDescriptor(
        name="bank_daily_bars",
        schema_version="bar-v1",
        source="fixture",
        record_count=len(records),
        attributes={"interval": "1d"},
    ),
    input_hash=input_hash,
    calendar_version="weekday-fallback-untrusted",
    universe_version="current-bank-list-untrusted",
    adjustment_version="raw-unadjusted-v1",
    constraint_version="missing-untrusted",
    configuration={"initial_cash": "1000000"},
    assumptions={"purpose": "research_only"},
)

print(manifest.content_hash)
print(manifest.to_json())
```

示例中的 `untrusted` 是有意的：manifest 忠实记录限制，不替供应商或数据治理作保证。任何 `buy` / `sell` 输出仍只是研究信号，不能转换为自动订单。

## 独立回放的参数绑定

### 策略代码实体

`strategy.code_version` 不再只是可由调用方任意填写的显示标签。可独立回放的内置
`bank_daily_multifactor_v1` 必须使用 `strategy_code_version("bank_daily_multifactor_v1")`
生成的 `sha256:<64位十六进制>` 内容地址版本。该版本由固定白名单中的策略、回测、领域、
序列化和回放代码单元，以及 Python 主次版本组成；不会把本机绝对路径、密钥或供应商响应
写入 manifest。`GET /api/v1/strategies` 的 `code_identity` 返回同一只读描述，研究客户端
应在创建 manifest 前保存该描述和 `version`。

回放会重新计算当前安装的代码实体，并检查 `strategy_code_version_matches`。任一源文件
修改、缺失、Python 主次版本变化或 manifest 仍使用旧的人工标签，都会使回放失败关闭，
即使结果 JSON 的哈希和参数都相同，也不会授予 `replay_verified`。该哈希是本地内容一致性
证明，不是代码签名或供应商认证；需要发布级证明时仍应在外部保存并验证签名的构建制品。

`verify_replay()` 当前只对白名单 `bank_daily_multifactor_v1` 执行离线回放，
并校验完整结果哈希。回放必须执行清单 `strategy.parameters` 声明的参数；
可选 `strategy_config` 仅用于核对，不允许覆盖清单。不同参数即使在“无成交”等样本上
恰好产生相同结果，也不能通过 `strategy_configuration_bound` 检查。

策略参数只接受该策略实际支持的名称（如 `min_universe`，不是 `minimum_universe`）；
未知参数、混合直接参数与 `parameters` 包装的歧义输入会失败关闭。仅有
`{"parameters": {...}}` 的旧包装仍可兼容。清单 `configuration` 中的键必须是
`BacktestConfig` 的可初始化字段，未知字段令 `execution_configuration_supported=false`，
不会被静默忽略后改用默认值。任一绑定检查失败时不执行重放，`replayed_result_hash`
保持 `null`，治理报告也不会授予可复现状态。上述检查没有网络或数据库副作用。
