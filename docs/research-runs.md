# ResearchRun manifest 与稳定数据哈希

`quant_monitor.research` 是纯标准库、无网络和数据库副作用的研究血缘组件。它把一次研究声明的输入固定为不可变 manifest，并生成稳定 SHA-256 内容哈希；当前尚未接入 REST API、页面或 SQLite。

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

`content_hash` 只能证明“这些规范化值和声明版本完全相同”，不能单独证明数据来自供应商、未被篡改、完整覆盖历史或在当时已经可用。当前 AShareHub 数据仍缺少平台验证的供应商签名/原始响应证据、独立 `available_at` / `received_at` 血缘、官方版本化交易日历、point-in-time 银行股票池、公司行动/复权因子和完整历史交易状态。

因此，可以把 `source="asharehub"` 和版本字符串写入描述符，但它们仍是调用方声明，不能据此把回测提升为可信研究等级。后续只有采集层建立受控批次证据，并由数据血缘校验器自动核验版本后，manifest 才能成为平台级可信门禁的一部分。

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
