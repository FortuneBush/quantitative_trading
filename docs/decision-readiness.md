# 决策级研究门禁

平台可以展示缓存中的行情、因子排序和电子云图，但 `decision_ready` 只在证据满足
以下条件时为 `true`：

1. 交易日历必须有明确身份、内容指纹和已认证的来源。内置
   `weekday_fallback`、AShareHub 当前未认证的缓存日历以及缺失/过期日历都只能用于
   展示和探索，不能作为决策级研究证据。
2. 证券主数据必须达到策略要求的最小数量，且不能来自静态 fallback 或临时
   `watchlist`。当前目录描述“现在知道什么”，不代表历史上“当时知道什么”。
3. 研究运行必须绑定不可变的 point-in-time universe snapshot。快照的
   `knowledge_cutoff` 不得晚于决策时间，成员、会话日期和内容哈希必须与请求一致。

这些条件由纯函数
`quant_monitor.research.build_decision_readiness` 组合。它只检查调用者传入的内存对象，
不会访问网络或 SQLite，也不会消耗 AShareHub 配额。检查结果包含独立的
`calendar_ready`、`universe_ready`、`point_in_time_ready` 和 `failures` 字段，便于界面
解释失败原因。

当前 AShareHub 交易日历快照的 `verified` 标记仍为 `false`，因此即使缓存加载成功，
也只满足运行状态/探索性查询，不会被标记为决策级数据。待接入能够验证官方交易所
发布物的导入流程后，再将认证身份和指纹传入门禁。

此门禁不会产生下单动作。平台仍然只输出研究信号；任何展示的 `buy`/`sell` 都必须
同时检查 `decision_ready` 和策略自身的风控门槛。
