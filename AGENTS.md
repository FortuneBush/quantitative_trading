# AGENTS.md

本文件供 AI 编码代理阅读，假设读者对本项目一无所知。

## 项目概览

**a-share-quant-monitor**（A 股量化数据监控台）：一个本地优先、可扩展的中国 A 股行情与量化信号监控框架。最低支持 Python 3.9。

- 默认通过 **AShareHub** 获取沪深 A 股 HTTP 批量实时快照、未复权日线和证券目录；API Key 仅从进程环境或 Windows DPAPI 本机存储读取。保留 AllTick、AKShare 和确定性的离线 `mock` 数据源。
- 数据经统一领域模型后写入 **SQLite（WAL）**，再经指标计算、示例策略产生信号，通过 **FastAPI** 提供 REST API、WebSocket 推送和原生 HTML/JS 中文仪表盘。
- **平台只产生研究信号（`Signal`），不连接券商、不下单。** `buy`/`sell` 仅为研究信号，不是交易指令。任何自动化下单路径都不允许出现。
- 默认仅绑定 `127.0.0.1`，无鉴权；改为局域网/公网监听前必须先加认证、TLS 和访问控制。

更详细的设计约束见 `docs/architecture.md`（架构与市场语义边界）、`docs/api.md`（API 约定）、`docs/extending.md`（provider/策略扩展契约）。**扩展 provider 或策略前必读 `docs/extending.md`。**

## 构建、运行与测试命令

项目在 Windows 上开发，使用项目内 `.venv`：

```powershell
# 安装依赖
.\.venv\Scripts\python.exe -m pip install -r requirements.txt        # 运行时
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt    # 测试

# 启动服务（默认 http://127.0.0.1:8000，API 文档在 /api/docs）
.\.venv\Scripts\python.exe -m quant_monitor                    # 自动打开浏览器
.\.venv\Scripts\python.exe -m quant_monitor --no-browser --port 8001
# 推荐直接运行 scripts/start.cmd；首次隐藏录入 AShareHub API Key 并以 Windows DPAPI 加密保存，之后一键启动
# scripts/start.ps1 是内部实现，也可单独使用；两个入口均会按需创建 venv

# 运行测试（离线测试请显式用 mock 源，避免访问网络）
$env:QUANT_MONITOR_DATA_PROVIDER = "mock"
.\.venv\Scripts\python.exe -m pytest -q

# AShareHub 真实链路冒烟检查（显式执行；固定单代码、一次实时请求）
set "QUANT_MONITOR_ASHAREHUB_API_KEY=替换为本机key"
.\.venv\Scripts\python.exe scripts\smoke_asharehub.py --symbols 600519.SH
```

- 打包：`pyproject.toml` 使用 setuptools；`pip install .` 后提供 `quant-monitor` 命令入口。
- 没有 linter/格式化工具配置；保持与现有代码风格一致即可。

## 代码组织

```text
quant_monitor/
  config.py          Settings：config/default.json + QUANT_MONITOR_* 环境变量覆盖
  main.py            CLI 入口（python -m quant_monitor）
  runtime.py         组合根：provider / repository / 服务 / WebSocketHub 装配与生命周期
  app.py             FastAPI：REST、WebSocket（/ws/market）、静态仪表盘、统一错误格式
  market.py          交易时段判断（仅周一至周五兜底，非真实交易日历）
  serialization.py   统一 JSON 序列化（Decimal→字符串、datetime、Enum）
  domain/            纯领域模型与进程内事件，仅用标准库
  data_sources/      MarketDataProvider 协议；asharehub.py（默认 HTTP）、alltick.py、akshare.py 与 mock.py
  storage/           sqlite.py：WAL、幂等 upsert；migrations/：版本迁移、checksum 和回滚保护
  services/          collection（采集编排）、indicators（SMA/EMA/RSI 等纯计算）、
                     event_bus（单进程发布订阅）、monitoring（轮询、策略求值、告警）
  strategies/        Strategy 协议、注册表、均线交叉示例（MovingAverageCrossStrategy）
  web/               无 CDN 的原生仪表盘（index.html / app.js / styles.css）
config/default.json  默认配置（当前 data_provider 为 asharehub；API Key 不在 JSON 中）
tests/               pytest 测试
docs/                架构、API、扩展文档（中文）
scripts/             Windows/POSIX 启动脚本及显式真实链路冒烟脚本
```

## 核心约定（修改代码时必须遵守）

- **领域模型是不可变 dataclass**，位于 `quant_monitor/domain/models.py`：
  - 价格/金额一律用 `Decimal`；JSON 输出中序列化为字符串；指标值、信号强度用 float。
  - 时间戳必须带时区，入库前统一归一化为 `Asia/Shanghai`（`ensure_market_time`）；DB 中以带 `+08:00` 的 ISO-8601 文本存储。
  - 证券代码规范形式为 `600519.SH` / `000001.SZ` / `430047.BJ`（`normalize_symbol`），输入不区分大小写，输出大写。
  - 模型自带校验（非负价格、OHLC 关系、strength ∈ [-1, 1] 等）；**不要用 0 掩盖缺失/停牌数据，用 `None` 或独立状态**。
- **分层边界**：供应商原始字段只许出现在 `data_sources` 适配器内；策略不得访问网络、数据库或厂商 API；domain 层保持零第三方依赖。
- **先持久化、后发布事件**：采集流程先写库再经 EventBus 广播；EventBus 无重放，客户端漏消息后用 REST 补拉。
- **配置**：新增配置项加入 `Settings`（含 `__post_init__` 校验），环境变量前缀 `QUANT_MONITOR_`（短别名见 `_ENV_ALIASES`）。程序不自动读取 `.env`。
- 新 provider 必须在 `ApplicationRuntime` 的 provider 工厂中显式注册；新策略需传给 `MonitoringService(strategies=...)`。配置不允许任意 Python import path 或 `eval`。

## 测试策略

- pytest + pytest-asyncio（`asyncio_mode = "auto"`），测试在 `tests/`，`conftest.py` 仅做 sys.path 注入。
- 普通单元测试**不得访问真实网络/收费 API**；AShareHub/AllTick/AKShare 适配器测试均使用离线注入的 transport/函数。测试数量随接入变更，以最近一次完整 pytest 结果为准。
- 测试与离线开发使用 `QUANT_MONITOR_DATA_PROVIDER=mock`。
- 新增 provider/策略时遵循 `docs/extending.md` 中的测试清单（边界、时区、停牌、乱序、幂等重放等）。

## 安全注意事项

- API 密钥只允许来自环境变量或本机密钥存储，**绝不**写入代码、配置 JSON、日志、`public_config`、WebSocket 消息或前端。
- 服务默认绑定 `127.0.0.1` 且无鉴权；WebSocket 校验 Origin 必须与 Host 同源。对外暴露前必须加认证、TLS、限流并确认数据许可。
- 行情数据有授权与再分发限制；本地使用不自动取得抓取、留存或再分发许可。
- 严禁任何自动下单路径；未来若加执行功能，必须是独立、默认关闭、需人工确认的子系统。
- 勿让多个应用实例同时作为采集主节点写同一 SQLite 库。

## 已知边界（不要"顺手修复"）

- `market.py` 的交易日历是周一至周五兜底，明确标注 `weekday_fallback`，不识别的节假日/停牌属于待接入生产数据的既定工作。
- SQLite 当前 `SCHEMA_VERSION = 3`，已有顺序迁移执行器；改 schema 必须增加新迁移并保持历史 checksum 不变，不能只改初始化 DDL。
- upsert 唯一键不含 `source`；多源行情会相互覆盖，接入多源前需先定权威源。
- 信号去重仅在进程内，重启/回放可能重复。
- AShareHub 免费档为 100 次请求/日；本地默认 90 次软预算、实时报价 300 秒缓存、日线 24 小时缓存、证券目录 7 天缓存。预算使用跨进程锁原子持久化，主服务和显式冒烟共用。不得把 pytest、CI 或启动流程改成自动访问真实接口，也不要绕过持久化日预算。
- AShareHub 当前文档没有官方股票 WebSocket、逐笔成交、五档盘口或分钟 K 线；`/ws/market` 只是本地事件推送，不能标成供应商实时流。
