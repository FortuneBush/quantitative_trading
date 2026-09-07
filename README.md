# A 股量化数据监控台

一个本地优先、可扩展的中国 A 股行情与量化信号监控框架。当前默认通过 AShareHub 获取沪深 A 股 HTTP 批量实时快照、未复权日线与证券目录，数据经统一领域模型后写入 SQLite、指标、示例策略、REST API、本地 WebSocket 和可视化仪表盘；仍可切换到 AllTick、AKShare 或确定性的离线模拟源。

> AShareHub 的实时性、覆盖范围、留存和展示权限取决于 API 套餐及供应商条款；免费档只有 100 次请求/日。示例策略只产生研究信号，不构成投资建议，也不会发送订单。

## 立即启动（Windows）

Windows CMD 直接运行一个脚本即可：

```cmd
scripts\start.cmd
```

该一键入口固定默认使用 AShareHub；其他数据源需显式传入 `--provider`。

首次运行会以隐藏输入方式询问一次 AShareHub API Key，并通过 Windows DPAPI 加密保存到当前用户的
`%LOCALAPPDATA%\a-share-quant-monitor\asharehub-api-key.dpapi`。以后双击或执行同一个脚本会自动
解密到启动进程环境并直接启动；明文密钥不写入仓库、JSON、日志或浏览器。

需要更换密钥时执行 `scripts\start.cmd --reset-key`，其余参数（例如
`--no-browser --port 8001`）会原样传给应用。

需要验证密钥时可执行 `scripts\start.cmd --smoke`：它复用 DPAPI 凭据，只检查
`600519.SH`，固定发 1 次实时请求并计入同一本地预算，不会启动服务或拉日线。

PowerShell 中也直接调用同一个入口：

```powershell
.\scripts\start.cmd
```

使用 Git Bash：

```bash
cd "/d/Program Files/code/quantitative_trading"
bash ./scripts/start.sh
```

Windows 的 Git Bash 和已启用 Windows 互操作的 WSL 会自动复用 Windows DPAPI 凭据与
`.venv`；WSL 互操作不可用时会回退到 WSL 本地 Python，此时与原生 Linux/macOS 一样需通过
环境变量提供 API Key。传入 `--no-browser`、`--port 8001` 等参数时会原样交给应用。
启动器还会为每个 provider 自动使用独立数据库，例如 AShareHub 对应
`data/quant_monitor_asharehub.db`、mock 对应 `data/quant_monitor_mock.db`，避免不同来源因旧版
主键未包含 `source` 而互相覆盖；显式设置 `QUANT_MONITOR_DATABASE_PATH` 时以你的路径为准。

脚本会在首次启动时创建对应虚拟环境并安装必要依赖，随后打开：

```text
http://127.0.0.1:8000
```

AShareHub 首次补齐 42 只银行日线可能需要数分钟，但不会再阻塞服务启动。
浏览器和健康接口会先可用；历史数据按股票逐只写入 SQLite，中断后可从已完成处继续。
若本地目录或历史曾被清理，下一次 AShareHub 启动会在预算保护下重新获取一次证券目录并逐只补数；
补数期间页面应显示“数据未就绪”，不会用 mock 或其他 provider 的旧数据填充结果。
可用以下两个不消耗上游额度的接口区分“进程活着”和“研究数据已就绪”：

```text
GET http://127.0.0.1:8000/live
GET http://127.0.0.1:8000/ready
```

`/ready` 中 `serving_ready=true` 表示 HTTP 已可服务，`research_ready=true` 才表示达到
策略所需的最低历史覆盖；`history_bootstrap` 给出逐股进度、失败代码和额度延期状态。

如果希望手动执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m ensurepip --upgrade
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m quant_monitor
```

常用选项：

```powershell
.\.venv\Scripts\python.exe -m quant_monitor --no-browser --port 8001
```

API 文档位于 `http://127.0.0.1:8000/api/docs`。

常用接口（以下接口严格只读 SQLite，不消耗上游额度）：

```text
GET http://127.0.0.1:8000/api/v1/sectors/banks
GET http://127.0.0.1:8000/api/v1/analysis/600036.SH?interval=1d&limit=240
GET http://127.0.0.1:8000/api/v1/data-quality/daily
```

## 当前已具备

- 可插拔 `MarketDataProvider`，内置 AShareHub、AllTick、AKShare 和离线 mock provider
- `Instrument / Quote / Bar / Metric / Alert / Signal` 领域模型
- 价格使用 `Decimal`，时间统一为带 `+08:00` 的 `Asia/Shanghai`
- SQLite WAL 持久化、幂等 upsert 与带 checksum/回滚保护的版本迁移
- SMA、EMA、RSI、收益率、波动率、涨跌幅等指标
- 42 只沪深上市银行静态兜底池，并优先使用 AShareHub 证券目录的 `industry=银行` 动态分类
- AShareHub 证券目录需通过总量、银行数量与单一来源完整性门禁后才会原子替换本地目录；部分响应继续使用明确标注的静态兜底
- 银行股横截面多因子研究模型：趋势、动量、低波动、回撤、流动性和板块宽度
- 后台轮询、事件总线、错误告警及优雅启停
- 真实数据源启动非阻塞、逐股日线断点式落库、额度保留和可视化补数进度
- 纯离线日线质量审计：缺口、重复、乱序、未来数据、覆盖率和横截面共同日期
- 研究用途日频回测内核：复用策略协议，次日开盘、100 股整数手、费用/滑点/印花税、T+1 与不可成交约束
- REST API、本地事件 WebSocket 和响应式中文仪表盘
- 默认仅监听 `127.0.0.1`，API 密钥无需进入浏览器

## 配置

默认配置在 `config/default.json`。环境变量以 `QUANT_MONITOR_` 开头，并覆盖 JSON，例如：

```powershell
$env:QUANT_MONITOR_PORT = "8001"
$env:QUANT_MONITOR_POLL_INTERVAL = "3"  # 也支持完整名 POLL_INTERVAL_SECONDS
$env:QUANT_MONITOR_DB_PATH = "data/my_monitor.db"  # 也支持完整名 DATABASE_PATH
```

也可复制 `.env.example` 作为字段参考，但程序不会自动读取 `.env`。AShareHub 密钥只通过
`QUANT_MONITOR_ASHAREHUB_API_KEY`（兼容短别名 `QUANT_MONITOR_ASHAREHUB_KEY`）接收；
`scripts\start.cmd` 可从当前 Windows 用户的 DPAPI 加密凭据中读取后注入启动进程。
密钥不允许写入默认 JSON、日志、公开配置、WebSocket 或浏览器代码。

## 项目结构

```text
quant_monitor/
  app.py             FastAPI、REST、WebSocket、静态站点
  runtime.py         依赖装配与进程生命周期
  domain/            纯领域模型和事件
  data_sources/      AShareHub HTTP、AllTick HTTP/WS、AKShare 与离线模拟适配器
  storage/           SQLite Repository
  services/          采集、指标、量化分析、事件总线、监控编排
  strategies/        策略协议、注册表和示例策略
  web/               无 CDN 的原生仪表盘
config/              本地配置
docs/                架构、API 与扩展说明
tests/               自动化测试
scripts/             Windows 启动脚本
```

## 测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

## 下一步如何接入你的 API 和策略

你提供实时/历史行情 API 文档与样例响应后，只需新增一个 `MarketDataProvider` 适配器，完成字段标准化、鉴权、限流与重试，再在 `runtime.py` 按配置选择它；监控界面和策略不需要理解供应商原始字段。

单标的策略实现 `Strategy.evaluate(context)`；需要同业排名的策略实现 `CohortStrategy.evaluate(context)`。策略层只产生 `Signal`，不会直接连接券商或下单。

更详细的设计与约束见：

- `docs/architecture.md`
- `docs/api.md`
- `docs/extending.md`
- `docs/maturity-roadmap.md`
- `docs/research-runs.md`

## AShareHub 数据（当前默认源）

默认 provider 使用 AShareHub v2 REST API：

- `GET /v2/market/realtime` 一次最多批量请求 200 个代码，返回最新价、日内开高低、昨收、当日累计成交量/成交额和供应商快照时间；
- `GET /v2/market/daily` 提供收盘后更新的未复权日线。供应商日线 `vol` 单位为手、`amount` 单位为千元，适配器统一转换成股和元；
- `GET /v2/reference/stocks` 提供证券主数据。目录长缓存，不在每个采集周期重复请求；
- `/api/v1/market/quotes` 在 AShareHub 模式只覆盖配置自选范围，不会用完整证券目录隐式扫描全市场；
- AShareHub 当前文档没有股票 WebSocket、逐笔成交、五档盘口或分钟 K 线接口。本项目 `/ws/market` 是本地 FastAPI 对已采集快照的推送，不代表连接到 AShareHub 推送流；盘口字段保持为空，策略默认使用 `1d`；
- 供应商非交易时段返回上一交易日收盘快照，页面以 `trade_time` 和本地新鲜度标识展示，不用本机接收时间冒充交易时间。

免费档为 100 次请求/日。默认银行配置以一次请求批量获取约 42 只银行快照，快照缓存 300 秒、采集循环 60 秒；正常交易时段理论上最多约 48 次快照请求，实际还受软预算和非交易时段复用保护。首次补齐 42 只银行日线约需 42 次。历史补数在后台逐只持久化，默认始终保留软预算中的 10 次给实时快照和人工操作；达到保留线后暂停，并在下一上海自然日预算重置后继续。预算状态持久化到
`data/asharehub_budget.json`，并使用跨进程锁原子递增；计数按不可逆密钥哈希身份隔离，文件不保存明文密钥，重启不会清零且手工冒烟也计入同一预算。默认报价缓存 300 秒、日线缓存 24 小时、证券目录缓存
7 天且自动批量合并银行代码。页面的银行筛选、排序、排名只读取 `/api/v1/sectors/banks` 的 SQLite 聚合结果，不会新增上游调用。
目录刷新只有在至少包含 1,000 只证券、40 只银行且全部标记为 AShareHub 来源时才会原子替换缓存，防止分页截断或字段漂移把不完整横截面冒充成银行全集。
收到 429 或达到本地软预算后会停止新增上游调用并将服务标为降级，而不是无限重试。

真实链路冒烟必须显式执行，固定只接受一个代码并消耗 **1 次**实时报价请求，不拉日线和证券目录：

```cmd
set "QUANT_MONITOR_ASHAREHUB_API_KEY=替换为本机密钥"
.\.venv\Scripts\python.exe scripts\smoke_asharehub.py --symbols 600519.SH
```

不要把冒烟脚本加入 pytest 或启动流程；如需增加任何额外真实请求，应先检查当日剩余额度并显式确认。
AShareHub 文档：<https://asharehub.com/zh/docs>。

## 银行板块研究模型

默认 `bank_daily_multifactor_v1` 在同一已完成交易日上对至少 15 只、每只至少 130 根日线的银行股做横截面排名。因子包括价格相对 120 日均线、20/60 日均线、60-5 与 120-20 日动量、60 日低波动和较浅回撤，并用流动性与板块宽度门控。候选每周复核，风险退出每日检查；页面展示证据、风险和失效条件。

当前 AShareHub 日线是未复权价格。银行股分红较频繁，代码会对疑似除权缺口暂停新信号，但该启发式不能替代前复权数据。因此输出仅是“偏强研究 / 中性观察 / 偏弱研究”，不能直接作为买卖指令。正式使用前还需用点时点银行名单、复权行情、交易成本、T+1、涨跌停和停牌约束做无未来函数回测。

仓库现已提供纯内存 `quant_monitor.backtesting.BacktestEngine` 作为研究验证基础：策略只能看到
当前及过去日线，信号最早在下一数据会话开盘尝试执行，并显式记录无法成交的研究信号。
它不会访问网络、数据库或券商，也不会发送订单。当前结果仍受未复权行情、周一至周五兜底日历、
当前股票池的幸存者偏差和缺少官方涨跌停/停牌历史约束影响，不能作为实盘收益承诺。

## 兼容 AllTick 数据源

AllTick provider 使用以下组合：

- WebSocket 生命周期独立于 HTTP 快照，启动后在同一连接订阅逐笔成交 `22004/22998`
  和五档盘口 `22002/22999`；两次订阅至少间隔 1 秒，每 10 秒心跳，断线后重新订阅；
- HTTP `/trade-tick` 获取最新成交快照，`/depth-tick` 补启动盘口，`/static_info`
  缓存名称、交易所、每手股数和可用基本面字段；
- 首次历史数据通过 `/kline` 回填；重启时先复用 SQLite 历史，后续通过
  `/batch-kline` 合并最新两根；最新两根日 K 用于补齐开高低、昨收和涨跌幅；
- 报价和 K 线分路容错：一条链路失败时，另一条成功数据仍会持久化，并把运行状态标为降级；
- 批量请求遇到 `604` 时按证券逐一隔离并熔断无权限代码，已授权股票继续采集；盘口权限失败不会关闭已成功的逐笔连接，隔离后会自动重建仅含授权代码的盘口订阅；
- 用供应商事件时间和 `seq` 拒绝重复、倒退行情，兼容重连后序列号重置；价格和金额使用 `Decimal`；
- HTTP 请求共享全局限流；免费档 Tick/盘口和 WebSocket 默认最多 5 个 code，
  批量 K 线使用当前文档的 10 组上限。

AllTick 官方没有证券枚举 API，也不提供全市场快照。因此 `/api/v1/market/quotes` 在
AllTick 模式下返回 `universe_scope=configured_symbols`，网页明确显示“沪 A 监控范围”，
不会把 watchlist 冒充全市场。切换回 AKShare 时，该端点仍可使用其沪 A provider 目录。

`/trade-tick` 的 `volume` / `turnover` 是最新一笔成交口径，不是当日累计量额；AllTick
也不直接返回涨跌幅。日线或盘口尚未载入、涨跌停单边盘口为空时，相应字段保持 `null`；
跨交易日或超过本地 TTL 的旧盘口会被清空。API 的 `field_availability` 会标明
`complete/partial/unavailable/stale`，不会用零值伪造。`/api/v1/instruments` 可读取已获取的证券静态信息。

在显式 AllTick 模式下，若 `/health` 或 `/api/v1/system/status` 出现 `ret=604`，官方含义是当前 token 没有请求
这些 code 的权限。这不是解析或限频问题；程序会拆分批次，让已授权代码继续工作，并对无权限
代码暂停请求 300 秒以避免重复消耗额度。仍需在 AllTick 侧开通相应 A 股代码/套餐或更换有权限的 token。旧数据库中其他 provider
的行情会按 `source` 隔离，不再冒充当前 AllTick 数据。

AllTick 兼容模式下页面顶部把三种状态分开显示：`本地 API` 是浏览器到 FastAPI，`本地推送` 是浏览器到
本机 `/ws/market`，`AllTick 行情` 才表示上游实际订阅。前两者成功但上游显示 604 时，
部署本身已经启动，真实行情仍会为空；首屏会显示已收到/配置数量和被拒绝代码。服务器
启动、访问和业务日志均使用带毫秒的本地时间戳。

显式切回 AllTick：

```cmd
scripts\start.cmd --provider alltick --no-browser
```

如需单独运行真实链路检查，请在该终端临时设置 token：

```cmd
set "QUANT_MONITOR_ALLTICK_TOKEN=替换为本机token"
.\.venv\Scripts\python.exe scripts\smoke_alltick.py --symbol 600519.SH --bars 2
```

AllTick 官方免费套餐文档规定 HTTP 每 10 秒一次、所有接口合计每天最多 1000 次，WebSocket
最多订阅 5 个 code。不要盲目降低 `ALLTICK_HTTP_MIN_INTERVAL_SECONDS`；更高套餐可按
实际授权调整批大小和间隔。AllTick 股票接口当前只明确支持 `.SH` / `.SZ`，不将 `.BJ`
视为已支持。

## 兼容 AKShare 临时源

需要临时切回 AKShare 时：

```cmd
set "QUANT_MONITOR_DB_PATH=data/quant_monitor_akshare.db"
scripts\start.cmd --provider akshare
```

AKShare 无需 API key，但属于公开网页数据源，不提供交易所级 SLA。可选 Sina 回退仍由
`QUANT_MONITOR_AKSHARE_USE_SPOT_FALLBACK=true` 显式开启。

需要离线开发或运行原有测试时显式切回模拟源：

```cmd
set "QUANT_MONITOR_DATA_PROVIDER=mock"
.\.venv\Scripts\python.exe -m pytest -q
```
