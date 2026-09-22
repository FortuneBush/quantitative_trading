# 扩展真实数据源与策略

## 1. 开发原则

扩展代码应继续支持 Python 3.9，使用 `async` provider 契约和现有领域模型。领域层是供应商、存储、策略和 API 之间的边界：供应商原始 payload 不应泄漏到策略；策略不应直接访问厂商 API、数据库或 WebSocket。

当前平台只生成研究指标、告警和信号，不下单。新增策略不得在 `evaluate()` 中调用券商交易接口。若未来确有交易需求，应建立独立、默认关闭、具备人工确认和完整风控审计的执行子系统。

## 2. 接入 MarketDataProvider

### 2.1 实现契约

在 `quant_monitor/data_sources/` 新建适配器并继承 `MarketDataProvider`：

```python
from datetime import datetime
from typing import List, Optional, Sequence

from quant_monitor.data_sources import MarketDataProvider
from quant_monitor.domain import Bar, Instrument, Quote


class VendorProvider(MarketDataProvider):
    @property
    def name(self) -> str:
        return "vendor"  # 稳定、短小；会随数据持久化

    async def fetch_quotes(self, symbols: Sequence[str]) -> List[Quote]:
        ...

    async def fetch_bars(
        self,
        symbol: str,
        interval: str = "1m",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[Bar]:
        ...  # 必须按 timestamp 从旧到新

    async def fetch_instruments(self) -> List[Instrument]:
        return []  # 供应商不提供目录时可保持默认

    async def close(self) -> None:
        ...  # 关闭 HTTP/WebSocket 会话，允许重复调用
```

必要行为：

- 接受并返回规范代码 `NNNNNN.SH/SZ/BJ`。供应商代码转换只发生在适配器中；未知交易所或不明确证券不能猜测。
- `fetch_quotes()` 返回每请求证券零或一条最新报价，不能混入未请求证券或其他来源。共享采集链路拒绝畸形/重复/混源/未来快照；缺失证券可由适配器按严格模式拒绝，手动刷新另通过 `refresh_result` 披露部分返回。
- 若适配器支持全市场批量端点，可额外实现 `fetch_quotes_relaxed()`：它允许停牌/无价证券被跳过并返回可用子集，供 `/api/v1/market/quotes` 使用；不要因此削弱 `fetch_quotes()` 对 watchlist 的严格校验。
- `fetch_bars()` 尊重 `interval`、含时区 `start/end` 和正整数 `limit`，返回从旧到新、同一证券同一周期的 K 线。需定义时间戳代表区间开始还是结束，并统一到全平台约定。
- 构造领域对象前完成单位转换：价格/金额用 `Decimal`，成交量和盘口量为非负整数；明确供应商的股/手、元/万元和累计/增量口径。
- 交易所事件时间必须带时区。不要用本机收到响应的时间冒充行情时间；本机接收时间应另作质量 metadata 或后续模型字段。
- 采集服务会用注入的 `TradingCalendar` 校验新收到的日线会话日期：周末等已知非会话日
  和有限日历覆盖外的日期保留在 lineage 中但标记 `quarantined`，不会计算指标、发布
  BAR 事件或驱动实时策略；批次 metadata 会记录隔离原因。不要在 provider 中把这类
  行静默删除，以便质量审计追溯供应商异常。
- 所有网络认证、分页、批大小、限流、重试、重连和供应商错误映射封装在适配器中。不要让策略知道 token、HTTP 状态或厂商字段名。
- `name` 一旦用于持久化就保持稳定。若同一供应商协议发生不兼容变化，用 metadata/schema version 标识，而非悄悄改变字段语义。
- 若供应商能提供交易所日历，可实现可选的 `get_trading_calendar(symbols=None)`。该方法必须
  只读取已加载缓存并返回 `TradingCalendar`（不能在质量/回测调用中发起网络请求）；无法证明
  覆盖完整性时返回 `None`，运行时会明确回退 `weekday_fallback`。AShareHub 的实现会把
  SSE/SZSE 完整缓存转换为 `VersionedSessionCalendar`，并由采集、质量 API 和回测共享。

领域模型还会执行以下校验：时间必须有时区；价格/金额有限且非负；OHLC 关系正确；`high >= low`；代码后缀与 `Instrument.exchange` 一致。因此适配器应把空值、停牌标记和缺失字段映射为 `None` 或独立状态，绝不能用 `0` 掩盖错误。

### 2.2 实时质量、乱序和去重

没有供应商序列号或版本号时，同一证券、同一事件时间的冲突重复报价必须 fail-closed，不能按响应顺序选择；适配器应公开冲突计数，确保质量审计能区分普通乱序重复与不可裁决修订。

当前 schema 只存一个行情时间戳，upsert 键为报价 `(symbol, timestamp)`、K 线 `(symbol, interval, timestamp)`。正式接入建议先扩展模型/数据库，增加：

- `exchange_time`、`vendor_time`、`received_at`、`persisted_at`；
- 供应商序列号或事件 ID、会话 ID、快照/增量类型；
- 数据质量状态、迟到/乱序标记、原始 payload 的受控追踪 ID；
- 复权类型、复权因子版本、证券交易状态和当日上下限价。

适配器应优先用供应商序列号去重；没有序列号时使用供应商文档允许的复合键和内容哈希。相同时间戳的修订可 upsert，但必须可观测。实时采集使用 `upsert_quote_snapshot()` 在同一 repository 锁范围内校验水位并入库：严格采集遇到旧行整批拒绝，显式宽松刷新丢弃旧行并披露。只有已入库行才发布事件；MonitoringService 的快照也禁止事件时间倒退。通用 `upsert_quotes()` 仍允许历史导入，不可直接用于 API 实时刷新。相同时间戳的先后修订仍无法由当前模型辨别，跨进程多主节点仍不支持。

建议对每批执行以下校验并输出计数/延迟分位数：请求缺失、同证券多条快照、序列跳号、时间倒退、未来时间、过期、`bid > ask`、OHLC 非法、当日累计量回退、负值、超出当日上下限价和跨源价差异常。数据质量失败应区分“丢弃”“隔离但落原始区”“降级展示”和“中止策略”，不能一概重试。

### 2.3 限流、重试和生命周期

当前 collector 会对自选证券的 K 线请求使用 `asyncio.gather` 并发。适配器必须保护厂商额度：

- 按供应商上限分批 symbols，使用连接池、并发信号量和令牌桶；历史与实时请求最好使用不同预算。
- 对超时、连接重置、429 和明确的 5xx 做有限重试；尊重 `Retry-After`，使用指数退避加随机抖动。
- 认证失败、无权限、非法参数、许可证额度耗尽等永久错误立即失败并告警，不无限重试。
- 重试必须幂等且可取消；`close()` 应停止后台心跳/重连并释放会话，避免阻碍应用关停。
- 记录请求数、状态码、限流等待、重试次数、断线次数、最近成功时间和数据新鲜度，但不得记录 token 或完整敏感响应。

若供应商主要提供推送流，可在适配器内部维护受界队列和最新快照缓存，使 `fetch_quotes()` 仍满足拉取契约；队列满时明确选择丢弃旧增量、触发快照重建或背压，不能无限增长。更大规模时可扩展采集服务支持原生 async iterator，但应保留领域模型和持久化边界。

当前默认 AShareHub 是低额度 HTTP provider，应额外遵守：免费档 100 次/日，本地默认只允许
90 次并把计数按上海自然日持久化，进程重启不能清零；预算递增必须有跨进程锁，所有显式冒烟也必须共用该预算；自选报价合并为最多 200 个代码的一次请求，
报价至少缓存 300 秒，日线至少缓存到下一收盘更新窗口，证券目录使用多日缓存。默认真实请求重试
为 0，任何增加重试、缩短 TTL、全市场扫描或 REST 懒加载的改动都必须先计算最坏日请求量。AShareHub
当前只提供 HTTP 实时快照和未复权日线，不得把本地 `/ws/market` 描述为供应商 WebSocket，也不得
合成逐笔、五档盘口或分钟 K 线。日线 `vol` 的手数和 `amount` 的千元口径必须在适配器内转换。

### 2.4 交易日历、证券状态与复权

不要继续依赖 `market.py` 的周一至周五兜底。新增正式日历服务，读取交易所公告或有授权供应商数据，至少按交易所保存交易日、分段交易时段、临时休市和数据版本。调度、K 线聚合和回测必须共用它。

当前已提供 `quant_monitor.trading_calendar.TradingCalendar` 注入契约、明确降级的 `WeekdayFallbackCalendar`，以及用于离线研究的 `VersionedSessionCalendar`。后者虽保存来源和版本标签，但不验证调用方声明，`verified` 固定为 `false`；不得把它描述为官方日历。其覆盖范围是闭区间，前后会话、区间和历史窗口查询在范围外必须抛出 `CalendarRangeError`；`is_session` 对范围外周末可安全返回 `false`，但对范围外工作日仍抛出该错误，调用方不得把未知工作日静默裁剪为非交易日。当前所有公开状态、质量报告和回测诊断还会强制输出 `calendar_verified=false`，结构化 Protocol 的 duck object 无权自行认证。数据质量服务可通过构造参数注入该契约，回测可通过 `trading_calendar=` 注入；现有裸日期 `session_calendar=` 仅为未版本化兼容入口。质量审计会把非会话日期单独列入 `off_calendar_dates`，这些记录不计入有效窗口并使结果降级；还会逐证券和跨证券核对 `Bar.source`，任一证券混源、跨证券来源不一致或来源为空都会令报告降级，且原始空来源不会被伪装成默认供应商。

`QuantitativeAnalysisService(calendar=...)` 也接受同一日历契约，避免离线研究的日线新鲜度检查仍暗用工作日规则。日线分析的 `data_quality` 返回预期日线日期和日历来源、版本、指纹及固定为 `false` 的认证状态；未传入时明确标记 `weekday_fallback`。有限快照不能回答当前预期会话时，分析返回 `calendar_out_of_coverage`、`stale=true` 和 `insufficient_data` 研究动作，不静默切回工作日规则。当前应用默认仍使用降级日历；这不代表已接入或认证交易所日历。

跨自然日推进时应使用 `quant_monitor.trading_calendar.session_offset(calendar, value, offset)`：它先把日期锚定到“当日或之前的最近会话”，再按会话步数前后移动，`offset=0` 不会把周末/节假日误当成交易日。正负偏移以及有限快照的首尾边界都会委托日历并在无法回答时抛出 `CalendarRangeError`；不要用 `timedelta(days=1)` 自行实现 T+1、信号有效期或下一开盘日。

离线日期夹具建议使用 `VersionedSessionCalendar.to_snapshot()` 保存，并在回测/质量审计前通过 `from_snapshot()` 恢复。导入器必须保留 `fingerprint`，不得通过手工改写日期或元数据后继续复用旧指纹；指纹只证明本地快照内容未被静默修改，不构成官方来源认证。

文件级导入可使用 `load_calendar_snapshot(path)`：它强制 UTF-8、拒绝重复 JSON
字段并重新计算指纹。研究运行或部署清单应同时传入
`expected_fingerprint`，并可传入 `expected_identifier`、`expected_source`、
`expected_version` 钉住预期来源；文件内容完整但身份不匹配时仍会失败关闭，避免把
另一份合法日历误当成目标修订。命令行校验器也支持同名的
`--expected-*` 参数，例如：
`python scripts/validate_calendar_snapshot.py calendar.json --expected-fingerprint <sha256>`。
发布快照时使用 `write_calendar_snapshot(calendar, path)`，同一路径
只允许幂等写入同一修订；不同内容会被拒绝。`CalendarSnapshotStore(root)` 以 SHA-256
指纹作为文件名并追加保存修订，`list()` 遇到篡改或未知文件名会失败关闭。该存储只解决
本地可复现性和防止旧运行被覆盖的问题，不能把 `verified` 变为 `true`；正式接入仍需
受控的交易所公告核验和独立认证证据。

若公告已整理为“工作日 + 法定休市 + 周末调休开市”，可用
`VersionedSessionCalendar.from_weekday_rules(start, end, holidays=..., makeup_sessions=...)`
生成有限覆盖快照。例外日期必须在范围内、不可重复且不能同时出现在两组中；这能覆盖
中国市场常见的周末调休语义，同时仍保持未认证和有限覆盖的失败关闭行为。为避免导入
错误被静默忽略，`holidays` 只能包含工作日，`makeup_sessions` 只能包含周末；把周末
写入休市列表或把普通工作日写入调休列表会直接抛错。

证券主数据需要版本化保存板块、证券类型、上市/退市日、风险警示状态、停复牌状态、最小价位和交易单位。涨跌停不能只按代码前缀判断；计算或校验时使用当日状态与交易所给出的参考价/上下限价，涵盖主板、科创板、创业板、北交所、ST/风险警示、上市初期及特殊交易日差异。无涨跌幅限制日仍可能有申报价格范围和盘中临停规则。

始终保留原始未复权行情，另建公司行为和复权因子表。对外查询显式要求 `raw`、`qfq` 或 `hfq`，并返回因子来源、版本和基准日。前复权和后复权的计算不得覆盖原始 K 线；供应商修订公司行为时，按血缘重算指标/信号。除权除息日涨跌幅基准应使用交易所参考价，不能直接使用上一条收盘。

当前领域层提供 `quant_monitor.domain.CorporateAction`、`AdjustmentFactor` 和
`AdjustedBar` 三个不可变契约，供后续持久化/供应商适配器使用。公司行为必须带
`action_id`、除权日、来源、修订标识和进程接收时间；现金分红、送转、拆并股和配股的
条款按动作类型校验，不能把不完整条款当成有效事件。复权因子必须带 64 位输入哈希、
因子版本、基准日和来源，`raw` 因子只能为 1。`AdjustmentFactor.apply_bar()` 只产生
带原始 `Bar` 引用的显式 `AdjustedBar` 视图，原始价格永不覆盖；只有调用方明确执行
`to_bar()` 才物化调整价，且会在来源中留下复权口径/版本标记。当前这是离线领域契约，
不代表 AShareHub 已提供公司行为数据，也不会从价格缺口自动推断分红或拆股。成交量与
成交额暂保持原始口径，若未来需要拆股后数量调整，必须新增独立的数量因子契约。
`AdjustedBar` 的公开构造函数也会重复校验证券和会话日期；不要直接包装不同证券或日期的
原始 K 线。已经由 `to_bar()` 物化、来源带有 `|raw/qfq/hfq|因子版本` 标记的价格不得再次
作为原始 K 线套用因子，否则会产生静默的二次复权；如需换因子，始终从最初的
`AdjustedBar.raw_bar` 重新计算。

回测引擎同样不会把 `price_basis` 当作自我声明的标签：`raw_unadjusted` 会拒绝已经带
复权来源标记的 K 线；`qfq`/`hfq`（包括带版本后缀的名称）必须通过
`adjustment_factors={(symbol, session_date): AdjustmentFactor(...)}` 为每一根输入 K 线
提供匹配的、版本化因子，随后才会从原始 K 线生成调整视图。缺少因子、证券/日期不匹配或
口径不匹配会直接 fail-closed。回测诊断会记录因子版本、修订号和因子 ID，但仍标记为
`bound_unattested`，因为当前没有供应商签名或独立持久化认证；因子绑定不能单独提升
`decision_grade`。

回测中的口径必须分层：`price_basis` 只控制策略看到的研究序列；即使策略使用
`qfq`/`hfq`，模拟成交价格、现金扣减、持仓市值、净值曲线和最终持仓仍只从原始
未复权 K 线读取。诊断会同时写出 `strategy_price_basis`、
`execution_price_basis=raw_unadjusted` 和 `valuation_price_basis=raw_unadjusted`。
这避免把分红/拆股造成的历史价格变换误当成可成交价格；公司行为对数量、现金分配
和总回报的完整处理仍需独立的公司行为账本，不能由复权因子单独推断。

基本面接入应使用 `quant_monitor.domain.FundamentalObservation`：报告期
(`period_start/period_end`) 与首次可用时间 (`available_at`) 必须分开记录，修订由
`revision_id` 标识。策略只可调用 `fundamentals_as_of()` 获取决策时点已经可见的版本；
当前 AShareHub 没有该契约的供应商实现，禁止用最新财报回填历史或用 0 补缺。
公司行动回放可使用 `apply_corporate_action()` 生成不可变账本效果，但零股、税费和
供应商特定结算规则仍必须由独立、版本化的执行约束实现。

停牌应作为状态而非零价格。默认不生成虚假的平盘 K 线；需要填充研究日历时，为合成数据增加明确标志，策略默认排除。区分全天停牌、盘中临停、无成交、非交易时段和数据中断。

### 2.5 注册到运行时

完成类后：

1. 从 `quant_monitor/data_sources/__init__.py` 导出适配器。
2. 在 `Settings` / `config/default.json` 增加非敏感选项；密钥使用 `QUANT_MONITOR_*` 环境变量或本机密钥存储，不进入 JSON、Git、日志或 API。
3. 修改 `ApplicationRuntime` 的 provider 工厂。当前内置 `asharehub`（默认低额度 HTTP 真实源）、`alltick`（HTTP/WS 兼容源）、`akshare`（临时公开源）和 `mock`（离线源）；新增供应商时必须在此显式注册，不能只导出适配器类。
4. 明确 provider 支持的周期集合，并与 `app.py` 的 `SUPPORTED_INTERVALS` 对齐。内置 mock 支持当前路由列出的周期，真实供应商不一定全部支持。
5. 决定 REST 的空库 K 线是否允许触发供应商请求。当前真实 provider 的 `GET /bars` 与分析接口默认只读 SQLite，只有调用方显式传 `refresh=true` 才会拉取并写库；新增 provider 不得把普通页面读取改回隐式消费额度。

### 2.6 Provider 测试清单

使用录制后脱敏的 fixture 或供应商 sandbox，至少覆盖：

- 代码映射、三个交易所、不同证券类型、中文名称和未知代码；
- 上海时区、UTC 输入、夏令时无关性、跨午休/跨交易日、节假日；
- 分页边界、`start == end`、空结果、`limit`、各周期排序；
- 停牌、无成交、涨跌停、上市初期、除权除息和复权因子修订；
- 重复、乱序、序列缺口、断线重连、429、超时、认证失败、部分证券缺失；
- `close()` 幂等、任务可取消、日志不泄密；
- 同批重放后的数据库行数不增长，以及迟到补数不触发实时策略。

不要让普通单元测试访问真实收费 API；把少量端到端测试放在显式启用、受配额保护的测试组中。
AShareHub 的 `scripts/smoke_asharehub.py` 固定只允许单代码的一次实时请求，不能由 pytest、应用启动、
CI 或文档生成自动执行；任何额外端点检查都应单独显式选择并在执行前提示预计消耗次数。

## 3. 新增策略

### 3.1 实现契约

在 `quant_monitor/strategies/` 新建类并继承 `Strategy`：

```python
from typing import Sequence

from quant_monitor.domain import Signal, SignalAction
from quant_monitor.strategies import Strategy, StrategyContext


class ExampleStrategy(Strategy):
    def __init__(self, window: int = 20, strategy_name: str = "example") -> None:
        if window <= 0:
            raise ValueError("window must be positive")
        self.window = int(window)
        self._name = strategy_name

    @property
    def name(self) -> str:
        return self._name  # 一个运行时中必须唯一且稳定

    def evaluate(self, context: StrategyContext) -> Sequence[Signal]:
        bars = [bar for bar in context.bars if bar.interval == "1m"]
        if len(bars) < self.window:
            return ()
        latest = bars[-1]
        # 示例条件省略；无信号时返回空序列。
        return (
            Signal(
                strategy=self.name,
                symbol=context.symbol,
                timestamp=latest.timestamp,
                action=SignalAction.HOLD,
                strength=0.0,
                price=latest.close,
                reason="example only",
                metadata={"window": self.window, "interval": latest.interval},
            ),
        )
```

`StrategyContext` 是一次求值的只读切面，含规范化 symbol、求值时间、可选最新 quote，以及按时间升序的 bars 和 metrics。当前监控器传入的是“本次 provider 返回的 K 线批次和本次计算指标”，不是从数据库自动拼接的任意长历史；策略所需窗口不得超过配置 `history_bar_count`，或应先扩展上下文构建逻辑。

策略必须：

- `evaluate()` 保持同步、快速、确定性且无副作用；不做网络/数据库 I/O，不读系统当前时间，不修改 context。
- 数据不足、停牌、状态未知、质量不合格或复权口径不匹配时返回空序列，而非凭填充值产生信号。
- 返回的每个 `Signal.strategy` 等于 `self.name`，`Signal.symbol` 等于 `context.symbol`，否则监控器会记录策略失败告警。
- 使用行情/K 线的事件时间作为信号时间，`strength` 必须在 -1 到 1；在 `reason` 和 metadata 中记录可复现实验的参数与关键值。
- 明确预期周期、原始/前复权/后复权、价格字段和成交量单位；同一策略的实时和回测路径必须一致。
- 不把 `BUY` / `SELL` 当成订单，不调用交易接口，不基于未来 K 线或当根尚未收盘 K 线的最终值产生无说明信号。

当前均线交叉示例只在快慢 SMA 实际跨越时产生信号。监控服务按相同策略、证券、动作和时间戳
设计确定性业务键，并在进程内缓存之外查询 SQLite 的 append-only 信号历史，因此服务重启或
历史重放不会重复发布同一业务键（多个采集主节点并发写入仍被架构约束禁止）。若未来需要跨进程
部署，应把该业务键升级为数据库唯一约束或独立的原子 claim 表。

### 3.2 注册与启用

`StrategyRegistry` 支持配置驱动构造：

```python
from quant_monitor.strategies import StrategyRegistry

registry = StrategyRegistry()
registry.register("example", ExampleStrategy)
strategy = registry.create("example", window=30)
```

注册名会转换为小写；重复注册默认报错，只有明确传 `replace=True` 才替换。当前
`ApplicationRuntime` 对均线策略也通过白名单 registry 构造；银行横截面策略仍使用
独立的 `CohortStrategy` 配置路径。要正式启用新策略，还需：

1. 从 `quant_monitor/strategies/__init__.py` 导出类。
2. 在配置模型中定义启用列表和经校验的参数 schema，或在 runtime 中直接实例化。
3. 把实例传给 `MonitoringService(strategies=...)`；一个运行时内 `strategy.name` 不得重复。
4. 若增加指标类型，保持 `IndicatorCalculator` 为纯函数，并确认指标在 `StrategyContext.metrics` 中可用。
5. 更新 `/api/v1/strategies` 展示，但不要增加隐式执行或下单行为。

配置不要允许任意 Python import path 或 `eval`；使用白名单 registry 和显式参数校验，防止本地配置成为代码执行入口。

### 3.3 策略验证清单

- 单元测试：空数据、窗口少一条/刚好足够、多证券隔离、错误周期、时间排序、Decimal 边界。
- 规则测试：停牌、涨跌停不可成交、ST/板块差异、上市初期、除权除息、复牌跳空和午休边界。
- 防未来函数：只使用信号时间及之前已完成的数据；若使用盘中未完成 K 线，显式建模并在回测中复现。
- 数据质量：迟到、重复、缺口、异常价、源切换和 stale 数据不得静默生成信号。
- 回放一致性：固定输入重复运行得到相同业务信号；进程重启的重复写入行为有测试和预期。
- 统计验证：手续费、印花税、滑点、成交单位、T+1、涨跌停/停牌无法成交和幸存者偏差应在未来回测执行模型处理，而不是把原始信号胜率当收益。
- 回测可通过 `TradingConstraint.limit_up_price` / `limit_down_price` 提供证券在该会话的已知封板边界。引擎会先应用滑点和最小价位取整，再保守拦截实际买入价不低于涨停边界或卖出价不高于跌停边界，并记录稳定的 `limit_up_locked` / `limit_down_locked` 跳过原因；不得从代码前缀或上一收盘价自行猜测涨跌幅。
- 性能与隔离：一个策略异常只产生 warning 告警，不阻断其他策略；计算耗时不应超过轮询预算。重计算应放到线程/进程池或离线任务，而不是阻塞事件循环。

回测引擎的 `run(..., cancellation_check=...)` 支持纯控制面的协作取消检查，
默认不影响确定性结果；检查在预处理前、会话边界及策略返回后执行，异常会直接向外
传播，不会包装成成功结果。策略本身仍不能读取取消状态或执行 I/O。异步封装使用
`backtesting.worker.run_cancellable_worker(compute, commit)` 分离计算和持久化：
`compute` 不得写结果，取消时必须等实际工作线程退出，不能仅取消 `to_thread` 的
await 就释放容量或关闭数据库。`commit` 一旦开始则不可撤销，最终状态以其实际
结果为准。长时间不返回的自定义策略仍需要自行拆分或未来的进程级隔离，线程模式
不能假称已经强制终止。

银行横截面策略的 `fundamental_weight` 是纯上下文叠加参数，默认值为 `0.20`，可由
`bank_strategy_fundamental_weight` / `QUANT_MONITOR_BANK_STRATEGY_FUNDAMENTAL_WEIGHT`
统一配置（`0` 明确关闭）。它只读取
编排器传入的已物化 `fundamental_factor_report`，不会自己访问仓储、网络或时钟；报告必须
是完整、无警告、同股票集合、`decision_at` 不晚于策略上下文时间的
`bank_fundamental_factors_as_of_v1` 制品。验证失败时策略保留价格分并返回
`fundamental_report_*` 回退原因。回测不可从当前在线 SQLite 补读基本面；若要实现历史
叠加，调用方必须把逐会话报告及其 hash 纳入 manifest，并在策略求值前显式绑定。

## 4. 数据库与 API 变更

 SQLite 当前 `SCHEMA_VERSION = 9`，已引入顺序迁移执行器与
`schema_migrations(version, checksum, applied_at)` 账本，并支持现有 v1 数据库事务化升级。
新增字段或表时必须增加新的、不可修改历史 checksum 的迁移；升级前备份数据库，
并对旧库跑集成测试。不要只修改 repository 中的 `CREATE TABLE IF NOT EXISTS` 兼容 DDL 后
假设已有数据库会自动变化。

schema v3 新增 `collection_batches` 与 append-only `bar_observations`，schema v4 新增
不可变 `research_runs` 清单，schema v5 新增不可变 `research_results` 回测制品，schema v6
持久化采集作业快照与幂等键摘要，schema v7 新增 append-only
`fundamental_observations` 基本面事实及 provider-independent as-of 查询，schema v8 新增
append-only `corporate_actions` 与 `adjustment_factors` 并提供 as-of 只读查询；采集路径与旧
`bars` 兼容表双写。时间与事务契约见 `docs/data-lineage.md`。旧 `bars` 不会被无依据地
回填成可信观测，新 provider 应逐步提供显式 period、availability 和 revision 语义。

新增 API 字段时优先保持向后兼容，继续通过 `serialization.py` 统一处理 Decimal、datetime 和 Enum。改变字段类型、时间含义、成交量口径或错误协议时升级路径版本。真实行情相关端点应返回数据来源、时间戳语义、新鲜度、质量状态和复权口径。

当前 API 无鉴权且默认仅绑定 `127.0.0.1`。若修改 host，先加入认证、TLS、访问日志脱敏、请求限流和 CORS/来源策略，并确认许可证允许通过浏览器、局域网或远程方式展示行情。

## 5. 交付前检查

每次 provider 或策略扩展至少完成：

1. 运行完整测试，并新增上述边界 fixture。
2. 用临时 SQLite 跑“启动 → 首次采集 → 多周期轮询 → WebSocket → 停止”，确认无任务和连接泄漏。
3. 核对时区、交易日历、交易状态、价格/成交量单位、复权口径和当日上下限价。
4. 断网、限流、乱序、重复、部分缺失和 provider 关闭场景下验证降级与恢复。
5. 检查日志、异常、公开配置和前端资源中没有密钥或受限原始数据。
6. 在看板和文档标明数据源、模拟/真实状态、新鲜度和许可证限制。
7. 确认没有任何自动下单路径；所有 `Signal` 仍只是供人工研究与决策辅助。
