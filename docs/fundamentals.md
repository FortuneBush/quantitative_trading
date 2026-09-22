# 点时点基本面契约

`quant_monitor.domain.FundamentalObservation` 是一个只读领域对象，供研究
回放使用。它不访问供应商、不写数据库，也不代表当前 AShareHub 已经提供了
基本面数据。每条记录必须同时带有：

- `period_start` / `period_end`：财务报告覆盖的期间；
- `available_at`：策略在数据供应商处最早可以看到该事实的时间；
- `received_at`：本地收到记录的时间；
- `unit`、`scope`、`source`：决定事实是否可比较；
- `revision_id`：供应商修订链中的稳定标识。

## as-of 选择规则

`fundamentals_as_of(observations, decision_at=...)` 只接受
`available_at <= decision_at` 的观察值，并按以下顺序选择：

在筛选前会先验证 `observation_id` 的稳定性：同一个 ID 如果对应不同的
经济内容（指标、期间、数值、发布时间或修订号），查询会 fail-closed 抛出
`ValueError`。这样期间/指标过滤不会把供应商错误复用的修订 ID 静默隐藏；
仅接收时间或元数据不同、但内容哈希一致的重复投递可以安全去重处理。

1. 在相同的 `(symbol, metric)` 下，先检查 `unit + scope + source`。如果候选
   记录出现多个组合，函数会抛出 `ValueError`，要求调用方使用显式过滤器；
   不会把百分比和倍数、合并口径和单体口径、不同供应商的值混在一起。
2. 未指定期间时，先选择 `period_end` 最新、再以 `period_start` 排序的报告期。
   因此晚到的旧报告重述不会因为 `available_at` 更晚而替换较新的报告。
3. 对选定报告期，再选择最晚的 `available_at`。同一可用时刻若出现不同值，
   视为无法判定的冲突并抛出 `ValueError`；相同值的重复修订才允许使用
   `revision_id`（再以 `observation_id`）作确定性排序。

如果研究确实需要旧报告或某一供应商，应显式指定 `period_start`、
`period_end`、`unit`、`scope` 和/或 `source`。同时指定两个期间边界是精确
匹配；只指定一侧时，会在匹配该边界的记录中继续选择最新期间。没有匹配记录
时返回空元组，而不是用零填充缺失值。

## 不可变性与安全边界

构造时会递归冻结 `metadata`：嵌套映射变为只读代理，列表变为 tuple，集合
变为 frozenset。`to_dict()` 返回独立副本，可以安全修改，不会改变已经选定的
观察对象。对象的内容哈希不包含接收时间和 `metadata`，用于标识经济事实与
发布时间；研究运行仍应在 manifest 中记录完整的数据集描述与来源版本。

本模块不会把报告期当作可见时间，也不会根据当前最新财报回填历史。供应商若
没有可审计的 `available_at`、修订号或公司行动/复权口径，研究门禁应保持
`partial`/不可决策状态。

## SQLite 与 HTTP 读取

SQLite v7 的 `fundamental_observations` 表保存上述记录的不可变副本，并以
`observation_id` 为主键、`content_hash` 为经济内容校验。仓储层的
`insert_fundamental_observations()` 对相同事实幂等；同 ID 不同内容直接拒绝。
更新和删除由 SQLite trigger 拒绝。`query_fundamentals_as_of()` 先按
`available_at <= decision_at` 读取候选，再调用同一 `fundamentals_as_of()` 规则，
因此 API 与离线回放不会出现两套选择语义。

只读接口 `GET /api/v1/fundamentals/as-of` 要求显式带时区 `as_of`，响应披露候选数、
选择后数量、来源、质量状态及 `provider_operations=0`。基本面 provider 尚未接入时，
空结果是正常且可观察的；接口不会把当前最新目录或财报自动补进历史研究。

### 离线导入

可使用 `scripts/import_fundamentals.py` 将已经取得授权、且带有
`available_at`/`received_at` 的 JSON 或 JSONL 文件追加到本地数据库。导入器不访问
provider、不读取 API Key；先执行 dry-run：

```powershell
.\.venv\Scripts\python.exe scripts\import_fundamentals.py `
  data\quant_monitor_asharehub.db fundamentals.json --dry-run
```

确认输出的 `record_count` 后再去掉 `--dry-run` 正式写入。导入支持
`fundamental_observations_v1` 包装格式或直接数组；每行会重新构造领域对象并校验时区、
报告期、十进制字符串数值、重复 JSON 字段、敏感字段和可选 `content_hash`。导入文件默认
限制为 16 MiB / 50,000 条，输出还包含不含密钥的 `input_hash`，可写入研究 manifest。
仓储仍按 observation ID 幂等写入，同 ID 不同内容会失败关闭；脚本错误输出不会回显原始行
或疑似密钥。导入后接口和银行策略只会
按决策时点选择可见事实，不会把晚到事实回填到过去。

## 银行因子层

`quant_monitor.research.build_bank_fundamental_factors()` 在上述 as-of 事实之上提供
固定的银行指标集合：PB、ROE、股息率、不良率、拨备覆盖率、资本充足率和成本收入比。
每项指标有明确 canonical unit 与方向，横截面采用确定性的并列中位百分位排名；任何
请求成员缺少指标或单位不匹配时，整个横截面综合分数保持 `null`，避免把幸存者当成分母。
横截面还必须至少包含两个成员；单只股票的完整事实不会被伪装成百分位评分。
不同来源的事实也不会被静默混成一个综合分数；来源冲突会保留为证据状态并使评分不可用。
逐项事实、缺失原因、修订号和来源仍会返回，便于研究者补充数据后复核。

可比性还要求整个横截面的 `scope` 一致且不能为 `unknown`，以及**同一指标**在不同证券上的
`period_start/period_end` 一致；不同指标之间可以有不同期间。单体/合并口径混用或年报/中报
混用时，保留事实但不计算分数，并返回 `invalid_scope_metrics`、`scope_conflict`、
`incomparable_period_metrics` 与稳定的警告代码。单位不匹配的值也保留其原始单位，不能按
canonical unit 格式化它。

每项 `evidence` 包含观测 ID、内容哈希、期间、公开可用/本机接收时间、单位、口径、来源和
修订号，不透出任意 provider metadata。构造时验证事实哈希与数值、覆盖状态一致，报告再次
校验共同决策时点和实际百分位计算。`symbols=[]` 表示空股票池，不能退化为查询全部；数据库
与因子构建器均在修订解析前限定证券及银行指标，未请求数据的歧义不会干扰当前股票池。

只读接口 `GET /api/v1/fundamentals/factors/as-of` 不访问 provider；当前 AShareHub
没有点时点基本面 feed，因此默认会返回 `quality.status=empty`，不会自动把因子混入
银行价格策略，也不会生成买卖指令。只有在外部合法导入并持久化完整事实后，因子报告才
会出现综合分数。

银行页的 `/api/v1/sectors/banks` 已包含这一独立视图：顶层为股票池覆盖报告，每个成员及其
价格 `analysis`（若存在）附带该股票的 `fundamental_factors`。没有日线也可以查看基本面；
冲突事实使基本面报告降为 `quality.status=invalid`，不会中断价格诊断。页面默认折叠详细
指标，可展开查看事实证据；没有事实时保持“评分不可用”。这不改变现有价格策略、信号或
回测权重，显式返回 `strategy_integration=false`、`score_usage=parallel_research_only`。

银行页面的截止时间是本次缓存快照构建时间，并非价格 K 线时间。`knowledge_mode=published_as_of`
仅按 `available_at` 截断，不意味着本机当时已收到这些数据；`received_at` 供单独核验。
`quality.status=ok` 只表示完整可比的数学输入，不认证数据来源、及时性或投资价值；旧报告即使
完整也不会因此被标为实时数据。将基本面纳入策略仍需要版本化策略、历史逐期输入绑定及
样本外验证，不能直接把页面分数混入旧价格策略。

策略叠加层现已提供显式的 `fundamental_weight`（默认 `0.20`）路径，可通过配置项
`bank_strategy_fundamental_weight` 或环境变量
`QUANT_MONITOR_BANK_STRATEGY_FUNDAMENTAL_WEIGHT` 调整；设置为 `0` 可明确关闭叠加。
编排器把同一决策时点
的已物化报告放入纯策略上下文；策略只接受 `selection`、`quality.status=ok`、无警告、完整
横截面且股票集合完全匹配的报告，并要求 `decision_at <= context.timestamp`。通过后综合分为
`price_score * (1 - weight) + fundamental_score * weight`，结果同时返回价格分、基本面分、
叠加贡献、权重、报告时点和 `model_variant=price_plus_fundamental_v1`。任何未来报告、缺失
成员、冲突或伪造分数都会保持 `price_only_v1`，并返回稳定的 `fundamental_*` 回退原因；
不会用 0 替代或抑制原有价格策略。默认 AShareHub 没有基本面事实时，运行自然处于价格回退
状态。回测路径不访问在线仓储；纯 Python 调用方可以把已经物化、按会话日期索引的报告通过
`BacktestEngine.run(fundamental_factor_reports={session_date: report})` 显式绑定。引擎会
校验报告选择器、时区、决策时点不得晚于对应会话，并将复制后的报告放入策略上下文；
缺少某个会话时策略保持价格-only 回退。绑定状态会写入
`result.diagnostics.assumptions.fundamental_factor_binding`，其中包含每个会话的
`sha256-json-v1` 内容哈希；绑定仍标记为 `bound_unattested`，因为引擎不会替调用方认证
来源。生产回放仍应把同一组逐会话报告哈希和输入来源写入 manifest，并由外部验证器核对，
不能靠当前数据库补读。
