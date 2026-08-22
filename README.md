# LIVE15_QUANT

LIVE15_QUANT 是一个 **Kalshi-native、只针对十个 15-minute Prediction Market series** 的本地量化研究项目。核心运行链为 Kalshi 官方 market discovery → quote/orderbook → lifecycle → official settlement truth；Coinbase 只提供 predictive underlying input。Robinhood SSR 仅是默认关闭的可选参考，不再决定 market availability、ticker、target、rollover 或训练标签。范围不包括小时、日、周、体育、政治合约，也不包括真钱交易。

长期 recorder 默认只使用 Coinbase Exchange 公共市场数据与 Kalshi 官方免认证 REST market-data API。另有一个隔离的 Kalshi Demo authenticated GET-only connectivity audit，只读取 balance/markets/positions/orders/fills；paper adapter 不访问账户、不持有凭据、不调用任何下单 endpoint，所有 paper order/fill 都只存在本地 SQLite。

## 六类数据严格分离

| 数据角色 | 当前来源 | 用途与限制 |
| --- | --- | --- |
| Predictive market-data source | Coinbase Exchange WebSocket / Pyth Hermes | 各 provider/feed 独立保存；**不是结算真值** |
| Optional reference | Robinhood 公开 15-minute 网页 | 默认关闭；只作兼容/交叉验证，故障或 rollover gap 不影响核心 runtime |
| Official venue contract quote | Kalshi public REST market/orderbook | 经 exact series + UTC window + target 唯一映射后的 Yes/No bid/ask、last、volume、depth；与 SSR 独立保存 |
| Official settlement truth | Kalshi public market `finalized` + `result` + `settlement_ts` | 唯一训练 ground-truth label；保存 settlement value/expiration value（若官方返回） |
| Settlement benchmark metadata | Kalshi series/rules | 保存官方 settlement sources 与 determination rules；Coinbase 永远不是 settlement truth |
| Paper execution ledger | 本地 Kalshi paper adapter | 只记录模拟 decision/order/fill/position/PnL；独立 SQLite，绝不表示真实成交 |

## 当前 Kalshi-native 支持矩阵

十个资产均由固定 series、UTC quarter-hour window 与 contract 自身 target 确定；不做标题模糊匹配，也不需要 Robinhood event 才能发现或记录。

| Asset | Kalshi series | Native lifecycle | Bid/ask + orderbook | Final YES/NO label | Primary underlying |
| --- | --- | --- | --- | --- | --- |
| BTC | `KXBTC15M` | Full | Full | Full | `BTC-USD` |
| ETH | `KXETH15M` | Full | Full | Full | `ETH-USD` |
| Gold | `KXGOLD15M` | Full | Full | Full | Pyth `Metal.XAU/USD`（已验证并启用） |
| Silver | `KXSILVER15M` | Full | Full | Full | Pyth `Metal.XAG/USD`（已验证并启用） |
| XRP | `KXXRP15M` | Full | Full | Full | `XRP-USD` |
| WTI Oil | `KXWTI15M` | Full | Full | Full | Pyth `Commodities.USOILSPOT`（已验证并启用） |
| SOL | `KXSOL15M` | Full | Full | Full | `SOL-USD` |
| HYPE | `KXHYPE15M` | Full | Full | Full | Pyth `Crypto.HYPE/USD`（已验证并启用） |
| DOGE | `KXDOGE15M` | Full | Full | Full | `DOGE-USD` |
| BNB | `KXBNB15M` | Full | Full | Full | Pyth `Crypto.BNB/USD`（已验证并启用） |

这里的 final label 来自 Kalshi 官方 finalized market result，不表示底层 benchmark feed 已获许可，也不表示任何真实下单能力。

## Kalshi-native discovery 与标准化

`KalshiNativeMarketProvider` 对十个固定 series 调用 Kalshi 官方公共 `GET /markets`，按 UTC 时间范围取得 previous/current/next/future candidates。每个 candidate 必须具有正确的 series/event/ticker prefix、整刻 15 分钟 UTC window、contract 自身的正数有限 Decimal target，且 ticker/window 唯一；target 为 `TBD`、缺失、malformed、duplicate 或 conflicting 时 fail closed。未来 contract 的 target 不会被拿来填当前窗口，当前 target 也不会复制给 future contract。

`initialized` 映射为 `UPCOMING`，`active` 为 `OPEN`，`inactive` 明确映射为可恢复的 `PAUSED`，`closed` 为 `CLOSED`，`determined/disputed/amended` 为 `SETTLEMENT_PENDING`；只有官方 `finalized` 且 `result=yes|no`、`settlement_ts` 有效时才进入 `SETTLED_YES/SETTLED_NO`。本地时间、Coinbase 与 Robinhood 都不能推断最终结果。尚未发布的 `Target Price: TBD` 是可重试的 upstream-unavailable；malformed timestamp、未知 status、错误 ticker hierarchy 或矛盾 result/status 会作为 correctness error 失败，不会被降级成 target unavailable。

### Optional Robinhood reference

`Robinhood15MinuteProvider` 读取公开的 [Robinhood 15-minute category page](https://robinhood.com/us/en/prediction-markets/15-min/)，解析页面 HTML 和公开嵌入的 `__NEXT_DATA__`。provider 不调用浏览器后台接口，也不依赖认证。

每个事件输出 typed `FifteenMinuteContract`：

- asset、event ID、contract ID
- UTC start/end time、target/reference price
- 页面显示的 Yes probability；仅当页面公开 No 时才会记录 No（当前为 `null`），并用 `availability` 区分 Partial/Unsupported
- `venue=None` 表示单事件底层 exchange 未披露；另存页面列出的 KalshiEX LLC、ForecastEX, LLC、Rothera Exchange and Clearing LLC 候选集合，但不猜测映射
- settlement benchmark、method、rounding precision
- lifecycle、source URL、fetched timestamp
- 基于 HTTP cache `Age`、HTTP `Date` 与事件窗口共同计算的 fresh/stale/unknown 状态

默认 source-age 阈值为 360 秒，可通过 `LIVE15_ROBINHOOD_MAX_SOURCE_AGE_SECONDS` 调整。即使 CDN `Age` 很小，只要 HTTP `Date` 或事件窗口表明快照已旧，也会标为 stale。页面结构缺失关键 ID、出现冲突重复事件，或时间不满足严格 15 分钟窗口时，provider 会显式报错；HTTP 429/5xx 和短暂网络错误使用有界退避重试。运行时 discovery URL 固定为经验证的公开 category page，不能通过环境变量改到隐藏接口。

## Settlement 映射

Crypto 合约均比较开始和结束窗口前一分钟内的 60 个逐秒 RTI 值的简单平均数，结束平均值大于等于开始平均值则 Yes。Robinhood 官方事件页明确写出的精度为 BTC 2、XRP 4、SOL 4、HYPE 4、DOGE 7、BNB 2 位；抽查的 ETH 官方页面未声明 rounding precision，因此代码保存为 `None`，不根据 target 展示位数猜测。官方事件页示例：

- [BTC / BRTI](https://robinhood.com/us/en/prediction-markets/crypto/events/btc-15-min-62-26563-target-jul-13-2026/)
- [ETH / ETHUSDRTI](https://robinhood.com/us/en/prediction-markets/crypto/events/eth-15-min-1-92379-target-jul-15-2026/)
- [XRP / XRPUSDRTI](https://robinhood.com/us/en/prediction-markets/crypto/events/xrp-15-min-10450-target-jun-27-2026/)
- [SOL / SOLUSDRTI](https://robinhood.com/us/en/prediction-markets/crypto/events/sol-15-min-734302-target-jun-15-2026/)
- [HYPE / HYPEUSDRTI](https://robinhood.com/us/en/prediction-markets/crypto/events/hype-15-min-590979-target-jul-18-2026/)
- [DOGE / DOGEUSDRTI](https://robinhood.com/us/en/prediction-markets/crypto/events/doge-15-min-01010484-target-may-29-2026/)
- [BNB / BNBUSDRTI](https://robinhood.com/us/en/prediction-markets/crypto/events/bnb-15-min-57983-target-jul-15-2026/)

Gold、Silver、WTI 使用对应 Pyth 1-minute candlestick 在窗口结束点的 close，分别按 2、3、2 位小数取整；指定值缺失时条款使用最近已发布值：

- [Gold / Pyth - Gold](https://robinhood.com/us/en/prediction-markets/metals/events/gold-15-min-4-10014-target-aug-03-2026/)
- [Silver / Pyth - Silver](https://robinhood.com/us/en/prediction-markets/metals/events/silver-15-min-58140-target-aug-03-2026/)
- [WTI / Pyth - WTI](https://robinhood.com/us/en/prediction-markets/commodities/events/wti-oil-15-min-7964-target-aug-03-2026/)

[CF Benchmarks API](https://docs.cfbenchmarks.com/api/) 明确要求 licensed API key；因此本项目没有伪造或替代 RTI observations。[Pyth](https://docs.pyth.network/price-feeds/core/getting-started) 的价格流在链上 permissionless，但其 hosted access 与具体 benchmark 仍需独立许可核查。这些 feed 只用于理解 determination rule；自动训练 label 直接采用 Kalshi 官方 finalized result，不自行重算 benchmark。

## Official venue mapping 与实时 quote

当前十个资产均存在上述 Kalshi 官方 15-minute series。核心路径直接接收 `KalshiNativeMarketProvider` 已验证的 native market；不再需要 Robinhood → Kalshi join。`KalshiOfficialQuoteProvider` 每次只接受同时满足以下条件的 instrument：

- asset 对应固定官方 series；
- Kalshi `open_time` / `close_time` 与已验证的 native UTC window 完全相等；
- detail response 的 contract target 与 discovery target 的 Decimal 原值完全相等；
- ticker、event ticker 与固定 series 必须完全一致。

任一 metadata 改变或 instrument mismatch 都抑制本轮 quote。native quote 只保存 Kalshi series/ticker/event ticker 和官方 evidence，不以 Robinhood 字段作别名。旧 `PredictionMarketQuote` 路径保留用于 Milestone 4/paper compatibility，不属于 native recorder 核心。

[Kalshi public market-data API](https://docs.kalshi.com/getting_started/quick_start_market_data) 明确允许免认证读取实时 markets 和 orderbook。REST 提供显式 `yes_bid_dollars`、`yes_ask_dollars`、`no_bid_dollars`、`no_ask_dollars`、`last_price_dollars`、`volume_fp`，orderbook 提供 Yes/No bid depth；缺失字段保持 SQL `NULL`，不通过 `1 - price` 推导。REST payload 的 market `updated_time` 在实测中不会随每次 quote 变化，不能冒充 quote event timestamp；因此保存低精度 HTTP `Date` 并明确标注 `http_response_date` 语义，另存本地 receive timestamp。官方 [WebSocket](https://docs.kalshi.com/getting_started/quick_start_websockets) 即使只读 market data 也要求 API key 握手；仓库只保存 repository 外 credential 的路径引用。synchronized WS orderbook 是 live primary，免认证 REST 继续独立承担 discovery、metadata、fallback 与 cross-check。

正式 quote 必须具有 verified mapping，market 与 orderbook 两个响应都必须通过 typed validation；orderbook 请求重试耗尽或 payload malformed 时，本轮 quote 整体失败并由 recorder 记录错误，不能降级成伪装为有效的空 depth quote。合法的真实空 orderbook 仍保存为空 tuple。

[ForecastEx Data](https://www.forecastex.com/data) 官方提供 pairs CSV（每 10 分钟刷新）、日终 prices 和 summary；未发布适合本项目的免认证秒级 REST/WebSocket quote API，因此不作为实时 source。[CFTC 官方登记](https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizations) 说明 Rothera 是原 LedgerX/MIAXdx 于 2026-01-20 更名后的 DCM；当前未发现 Rothera 官方公开实时 quote API。两者都不会通过页面标题猜测接入。

`official_venue_order_book` 表示数据来自官方 venue book，**不表示可通过 Robinhood 执行**，也不表示本项目具备账户资格、路由、费用或下单能力。

### Detail-page live quote audit

2026-08-20 对未登录公开详情页的浏览器网络行为进行了审计。页面通过重复 XHR 读取 `api.robinhood.com` 下的 event-state、contract quote、fundamentals 和 15-second historical 数据；未观察到 SSE 或 WebSocket。全新无 Cookie、无 Authorization header 的 HTTP session 能读取 quote JSON，字段包括 Yes/No bid/ask、last trade、instrument ID 和源更新时间。

这些 Prediction Market 路由没有出现在 [Robinhood 的公开 API 文档](https://docs.robinhood.com/) 中；[Robinhood 官方 third-party connections 说明](https://robinhood.com/us/en/support/articles/third-party-connections/) 也没有授权此类未发布接口供第三方 collector 使用。因此“无需认证即可响应”不被当作“公开且允许的 API”。本项目**不调用、不封装、不持久化这些路由**；仅用浏览器进行只读时间接近对比。正式实时 source 是独立的 Kalshi 官方 REST provider。

### Robinhood Trading MCP capability audit

2026-08-20 完成授权后的实际 schema 审计。Robinhood Trading MCP 连接正常，共暴露 54 个工具；但当前 `search` 只接受 `instrument`、`currency_pair`、`market_index`，以 `event_contracts` 只读搜索 BTC、ETH、SOL、DOGE 15-minute 均被服务端明确拒绝。工具清单中也没有 event discovery、event/contract quote、Yes/No Bid/Ask、event position、event order/status 或 event cancel 接口。因此当前结论是：**Robinhood MCP Event Contract capability = currently unsupported**。

MCP 保留为未来官方 execution adapter，但项目目前不实现 `RobinhoodMCPProvider`，也不以未发布 XHR 代替它。完整证据与当前非 Event Contract 能力清单见 [Robinhood MCP capability audit](docs/robinhood_mcp_capability_audit.md)。此次审计没有读取账户、购买力、持仓或订单，也没有调用任何 review/place/cancel/exercise 或其他写工具。

`ExecutionProvider` 现在有一个 `ExecutionMode.PAPER` 的 concrete adapter。它实现本地账户状态、持仓、submit/cancel、reduce/close 与 fill status；没有 authenticated network write 或 credentials。不可由 strategy 修改的 `ImmutableHardRiskLayer` 与 provider 独立，在成交模拟前硬性检查单 event/总 exposure、每日亏损、连续亏损、stale/missing quote、mapping、source/fill uncertainty 与 kill switch。默认数值只适用于 deterministic dummy paper runtime，不会成为未来真钱限额。

## Kalshi paper execution

`KalshiOrderBookFillSimulator` 只消耗官方 orderbook 的真实可执行 depth，不使用 mid：Buy Yes 使用由 No bid depth 对应的 Yes ask，Close/Reduce Yes 使用 Yes bid depth；Buy No 使用由 Yes bid depth 对应的 No ask，Close/Reduce No 使用 No bid depth。转换后的最佳 depth 必须与 API 显式 top-of-book 完全一致，否则结果是 `price_moved`，不会成交。IOC、FOK、resting GTC、full/partial/no fill 均有显式状态；每个 fill 保存 signal/submit/quote/fill timestamp、原始 Decimal price/quantity、spread、slippage 和 fee 分解。

IOC 若只吃到部分 depth，会保存实际 fills 并以 `cancelled + partial_fill` 表示剩余数量已立即取消；只有 GTC partial 才保持 `partially_filled` 并允许本地 CANCEL。连续亏损熔断按每个已实现退出订单的净 realized increment（价差减该退出 fees）计数，多层 depth 仍只算一次退出。

费用模型按 [Kalshi 官方 fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) 的通用 taker 公式 `0.07 × C × P × (1-P)`，并实现官方 fixed-point 文档的 `$0.0001` trade-fee ceiling、cent-alignment rounding fee 和同一 order 的 accumulator rebate。2026-08-20 官方 `fee_changes` 对十个目标 series 均无 override，因此当前明确标记为 assumption；未来接入 demo/production 前必须重新核验，详见 [Kalshi execution API audit](docs/kalshi_execution_api_audit.md)。

paper 数据默认写入独立 `data/paper.sqlite3`，与 `data/live15.sqlite3` 的 raw market data 严格分离。SQLite WAL ledger 使用唯一 decision/order/fill ID 防重复，restart 时按 `fill_timestamp,id` 重建 portfolio；`PaperReplayReader` 以固定 tie-break 顺序读取 order/fill。positions 支持 open、反复 add、partial reduce 和 early close；事件到期但没有官方 settlement truth 时状态变为 `pending_settlement`，不会使用 Coinbase 代结算。

paper schema version 1 使用独立的 `paper_metadata`、`paper_decisions`、`paper_orders`、`paper_order_events`、`paper_fills`、`paper_risk_decisions`、`paper_position_snapshots` 和 `paper_portfolio_snapshots` 表。所有货币、价格和数量均以 Decimal 原始字符串保存；decision/order/fill 唯一约束及 foreign keys 防止重复或孤立记录。paper/raw store 会检查对方的 metadata marker，并拒绝打开同一个数据库文件。

## Underlying coverage 与 Data Readiness Report

BTC、ETH、XRP、SOL、DOGE 继续使用 Coinbase Exchange 公共 WebSocket。Gold、Silver、WTI、HYPE、BNB 的 primary predictive source 已确定为 Pyth Core upgraded Hermes，精确 symbol/feed ID 由官方 `/v2/price_feeds` registry 核验：

| Asset | Pyth symbol | Stable feed ID |
|---|---|---|
| Gold | `Metal.XAU/USD` | `765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2` |
| Silver | `Metal.XAG/USD` | `f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e` |
| WTI | `Commodities.USOILSPOT` | `925ca92ff005ae943c158e3563f59698ce7e75c5a8c8dd43303a0a154887b3e6` |
| HYPE | `Crypto.HYPE/USD` | `4279e31cc369bbcc2faf022b382b080e32a8e689ff20fbc530d2a603eb6cd98b` |
| BNB | `Crypto.BNB/USD` | `2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f` |

[Pyth Hermes API](https://hermes.pyth.network/docs/) 的 latest-price REST 与 SSE 都提供 publish time、raw integer price、exponent 和 confidence。正式 client 使用本次 authenticated acceptance 返回 200 的官方 `https://hermes.pyth.network` endpoint，只从仓库外 key 文件读取 Bearer credential，不提供 write/trading 方法；文档列出的 upgraded `https://pyth.dourolabs.app/hermes` 在本次两个 v2 route 验证中均返回 404，因此不作为静默 fallback。[Pyth market hours](https://docs.pyth.network/price-feeds/core/market-hours) 说明金属和 USOILSPOT 的交易/maintenance windows，闭市期间 stale 是明确 source state，不能用旧值伪装 fresh。Pyth 仍是 HYPE/BNB 的 primary predictive source；venue-native 数据由同一个 Continuous Recorder 追加为独立 secondary observations，不覆盖、平均或自动替换 Pyth，也不代表 Kalshi settlement benchmark。

运行时只建立一条 authenticated `/v2/updates/price/stream` SSE connection，并在同一网络层按 exact feed ID demux 五个 feed。服务端按官方说明会在 24 小时关闭 SSE，recorder 会 bounded reconnect；单个 feed malformed/stale/out-of-order 只产生该 feed 的诊断。SSE 断开时最多以一个 `/v2/updates/price/latest` batch request 同时获取全部五个 feed，不会退化成五次独立 polling。Pyth 官方限额为所有 endpoints 合计 10 requests / 10 seconds / IP，超限后可持续 60 秒返回 429；client 使用共享 sliding-window budget，默认只允许 8 requests / 10 seconds 并尊重 bounded `Retry-After`，为其他调用保留余量。

`live15-readiness` 使用 SQLite online backup 从正在增长的 raw DB 创建 transaction-consistent 临时副本，仅在临时副本上迁移 schema、重建 Dataset，并原子写入 Git-ignored 的 `data/readiness.json`。报告包含 10 资产 source 状态、source→receive latency、gap、duplicate、out-of-order/clock-skew、Kalshi quote quality、42 个 feature 定义与 live/leakage readiness、dataset balance/buckets/missing/stale/rejection reasons，以及不含本机路径的 source row boundaries/content digests，便于区分可复现的 point-in-time 结果与持续增长的当前值。它不会打开正式 raw DB writer，也不会修改 raw truth。跨机器时钟的负 source→receive 值只标记 `clock skew`，不作为负网络延迟，也不会把按本机 receive age 仍然新鲜的 source 降级为 stale。

schema v9 新增 append-only `data_gaps`：按 provider、asset、instrument 和真实 receive-time interval 保存 threshold、reason、recovery/session/incident provenance。来源超过 threshold 时 recorder 先追加不可变 OPEN fact，恢复后的第一条 observation 再追加 RECOVERED fact，不会更新或覆盖旧事实；重启会从 indexed source cursor 与尚未闭合的 OPEN fact 恢复检测状态。历史 gap 只在 transaction-consistent 临时 snapshot 上重建，绝不对 active raw DB 做无界 cadence scan。重复检测同一状态幂等，冲突 end/reason/threshold 会 fail loudly。DatasetBuilder 将 OPEN/RECOVERED facts deterministic 投影成有效 interval，并在每个 decision row 上分别检查 Kalshi quote freshness window 与 primary-underlying 300 秒 lookback；只 quarantine overlap 的 row，恢复后的后续 row 仍可用。它从不 forward/backward/synthetic fill，也不使用事后数据修补 decision-time 输入。未来 inference 可复用 typed `PASS / DATA_UNAVAILABLE` boundary；此边界不包含交易行为。

### Low-latency underlying benchmark

`live15-latency-benchmark --seconds 30` 是独立、只读、绝对有界的网络基准，不启动/停止 recorder，也不写正式 raw DB。它在系统临时目录创建并删除专用 SQLite，逐条保存 provider source timestamp、socket receive、JSON parse completion、bounded queue admission 和 SQLite commit completion 的 UTC 时间；本地阶段耗时与 receive gap 使用同一进程的 monotonic 纳秒钟计算，避免 Windows/NTP wall-clock 调整污染亚毫秒结果。`--venue-only` 可在没有 Pyth credential 时只测公开 venue streams。所有 source 保持独立 provenance；benchmark 结果不会自动切换 primary source。若 bounded queue 过载或任一 source 失败，报告会显式给出 `queue_drops`/`source_errors` 并把 `measurement_complete` 标为 false，不会静默宣称完整样本。

- BNB 使用 [Binance 官方 market-data-only WebSocket](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md) `wss://data-stream.binance.vision` 的 `BNBUSDT@aggTrade`。它无需账户/API key，提供毫秒 trade timestamp，但语义是 Binance Spot BNB/USDT last trade，不是 Pyth BNB/USD aggregate，也不是 Kalshi settlement truth。官方说明连接约 24 小时会断开并要求 ping/pong；adapter 使用 bounded reconnect。历史 REST/下载可用，但生产留存/再分发仍须遵守 Binance Terms 和所在地限制。
- HYPE 使用 [Hyperliquid 官方 mainnet public WebSocket](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) 的 HYPE perpetual `bbo`。保存原始 bid/ask，并明确以两者 midpoint 作为 secondary predictive price；source time 是官方 BBO block timestamp。无需钱包/账户，30 秒 application ping 和 bounded reconnect 遵循官方协议。[官方 S3 历史数据](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data)约月度上传、requester-pays、可能缺失，因此不能视为完整训练历史。
- Pyth Pro exact IDs 由 authenticated `/v1/symbols` 严格解析：Gold `346`、Silver `345`、HYPE `110`、BNB `15`。现有 key 已能订阅这四项；Gold/Silver 最快支持 `fixed_rate@200ms`，HYPE/BNB 支持 `real_time`。`Commodities.USOILSPOT` 的 Pro ID `657` 当前官方 metadata 状态为 `inactive`，因此没有把 WTI 强行加入 Pro stream。正式 Pyth Pro 高可用要求同时连接三个官方 endpoint；当前单连接/轮换实现只用于 benchmark，不是 recorder production adapter。
- [Pyth Pro rate limits](https://docs.pyth.network/price-feeds/pro/rate-limits) 的连接数、feed entitlement 和 channel 由具体 service agreement 决定；[Pyth 官方 upgrade 公告](https://www.pyth.network/blog/the-pyth-core-upgrade)称数据计划起价约 USD 500/月。现有 key 的当前 entitlement 已真实验证，但不能据此假设 trial/plan 永久有效。本项目没有购买、升级或绕过任何 plan；正式切换前必须由用户确认长期许可与费用。
- Gold/Silver/WTI 的 venue-native source 是 CME/COMEX/NYMEX。[CME 官方 Real-Time Futures & Options WebSocket](https://www.cmegroup.com/market-data/real-time-futures-and-options-data-api.html) top-of-book 约 500ms conflation，但需要 Data Services Portal onboarding、API ID、产品订阅和市场数据许可；本阶段未购买或绕过。因此 Gold/Silver 可把 Pyth Pro 列为低延迟 secondary 候选，WTI 仍以 Pyth Core 为可用 primary，CME 仅列为待授权候选。

2026-08-21 的同进程 20 秒短基准中：Binance BNB 81 observations、Hyperliquid HYPE 158、Pyth Core 每资产 46、Pyth Pro 每资产 99。source→socket median 分别约 `-60ms`（本机相对 Binance source clock 的偏差，不能解释为负延迟）、`160ms`、`3.93s`、`-44ms`（同样受 clock offset 影响）；本地 socket→parse→queue→commit median 分别约 `0.23ms`、`0.24ms`、`0.66–0.93ms`、`0.29–0.50ms`。所以当前 Pyth Core 3–6 秒主要来自 feed aggregation/proof availability 与 publish-time 语义，本地 Python/SQLite 热路径占比远低于 0.1%；绝不通过改写 timestamp 消除该差异。

报告把 `live_underlying_source_ready` 与 `historical_underlying_feature_coverage` 分开：前者表示当前实时推理路径可计算，后者表示已生成训练行中实际存在 underlying feature 的比例。新 source 刚启用时会标为 `PARTIAL`，不能仅因收到实时数据便宣称历史训练集完整。2026-08-21 短验收中 Pyth 五 feed 的 source→receive median 约 3.6 秒、p95 约 5.8 秒；官方 payload 的 `publish_time` 是整秒，proof availability 比 publish time 晚约 4 秒，本地与 HTTP `Date` 的中位偏差约 0.12 秒。因此该值主要反映 feed aggregation/proof availability、整秒量化和 transport，而不是本机 clock skew。它低于当前 15 秒 as-of/stale budget，仍须在 readiness report 中持续监控，绝不回写或修正历史 timestamp。

### Low-latency secondary recorder

schema v7 新增独立的 `secondary_underlying_observations` append-only table。BNB 保存 Binance `BNBUSDT@aggTrade` aggregate trade；HYPE 保存 Hyperliquid HYPE perpetual `bbo` 原始 bid/ask 与明确标注的 midpoint。两者均为无需账户/credential 的官方 public market-data WebSocket，并使用 bounded reconnect/backoff。任一 secondary outage 只降低对应 source health；storage conflict/corruption 仍 fail loudly。

Pyth primary 继续只存在 `underlying_observations`，DatasetBuilder 和 `training_source_snapshot()` 不读取 secondary table，因此当前 42-feature registry 不会隐式切源。`SecondaryFeatureBoundary` 仅定义未来可选 feature 的 typed receive-time as-of boundary。UI 管理的 Start/Resume 默认在同一个 recorder 中启用两路 secondary；手动前台运行需设置 `LIVE15_ENABLE_SECONDARY_UNDERLYING=true`。`live15-secondary-diagnostics --minutes 5` 执行 bounded/query-only Pyth-vs-venue cadence、age、gap 与 divergence 比较，并保留 BNB/USDT trade、HYPE perpetual BBO midpoint 与 Pyth aggregate USD feed 的语义差异。

## Training Dataset + Feature Store

Milestone 7 以 `data/live15.sqlite3` 的 raw recorder 为只读 source of truth，并把可重建训练数据写入独立的 `data/features.sqlite3`。`live15-dataset` 会按 exact ticker/window 将 decision-time metadata、Kalshi quote/orderbook、Coinbase predictive ticks 与 Kalshi 官方 finalized YES/NO label 组合；它不会改写 raw 数据，也不会读取 paper ledger。

Milestone 7.5 将该路径升级为无需人工盯守的连续采集服务：十个资产逐一隔离发现，rollover 后继续有界追踪 predecessor 到官方 finalized，重启时完全从 SQLite 恢复，并原子输出机器可读 health。长期运行、恢复、容量规划与 Windows restart helper 见 [Continuous training-data recorder](docs/continuous_recorder.md)。

默认 sampling grid 是距结束 14m、12m、10m、8m、5m、3m、2m、1m、30s，可通过 `LIVE15_DATASET_DECISION_OFFSETS_SECONDS` 配置。核心 `SamplingPolicy` 不含固定现实日期、固定 UTC 时刻或固定 grid。每个 event 可以产生多行，但 chronological/expanding/rolling split 都以完整 ticker event 为 group，同一 event 不会跨 train/validation/test。

Feature schema `1.0.0` 当前有 42 个具名、Decimal-safe feature：contract geometry/target distance、15s/30s/1m/2m/5m returns、return acceleration/momentum、1m/2m/5m realized volatility、range/regime、Kalshi Yes/No bid/ask/spread/midpoint/last、quote age、top/cumulative depth、imbalance/depth ratio/book change，以及只作描述的 spread-aware market-implied quantities。midpoint 不是模拟成交价，也不被宣称为真实 probability。

所有 as-of join 强制 local receive time 不晚于 `decision_timestamp`，若 source/exchange timestamp 存在也必须不晚于 decision；post-window quote、future tick、terminal lifecycle 和 settlement 字段不能进入 feature engine。label 只能来自 Kalshi `finalized + result + settlement_ts`，并与 feature 在类型层隔离。缺失值保持 SQL/JSON null，并记录 `truly_missing`、`stale`、`not_enough_lookback`、`source_unavailable` 或 `market_side_unavailable`，不填 0。

Feature store 保存 dataset/feature schema version、path-free raw snapshot（count/max row ID/content digest）、可复现 build manifest、source provenance、per-feature timestamp/missing reason 和机器可读 diagnostics。构建查询受启动时 max row ID 限制，即使 recorder 同时追加也不会改变本轮输入；中断后相同 manifest 可幂等 resume，内容冲突会 fail loudly。完整契约见 [Training dataset architecture](docs/training_dataset.md)。

## 安装

要求 Windows PowerShell 和 Python 3.13：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install pip==26.2.1
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
```

## 运行

```powershell
# 持续记录全部 Kalshi-native 目标 series + 5 个 Coinbase predictive products
live15-record

# 从 raw recorder 构建或幂等恢复版本化训练数据集
live15-dataset

# 构建一致快照并输出当前训练覆盖
live15-coverage

# 对 raw DB 做只读一致快照并生成完整 readiness.json
live15-readiness

# bounded/query-only Pyth primary vs venue secondary diagnostics
live15-secondary-diagnostics --minutes 5

# 读取 recorder 的原子 health heartbeat
live15-status

# 启动 localhost-only Control Center（market/training data 只读）
live15-ui

# 真实公开 Kalshi 行情驱动、只写本地 SQLite 的 deterministic paper runtime
live15-paper

# 显式 30 分钟隔离 acceptance；数据库写入系统临时目录
python -m live15_quant.paper_acceptance --duration-seconds 1800

# Kalshi-native event-driven live acceptance；动态选择最近真实 rollover，默认最长 30 分钟
python -m live15_quant.native_acceptance --max-seconds 1800

# 一次性发现并输出当前官方 Kalshi 15-minute markets（JSON logs）
live15-discover

# Coinbase predictive sources
live15-rest
live15-stream
live15-btc-stream
```

三个原有兼容入口仍可用：

```powershell
python btc_price_test.py
python btc_stream.py
python market_stream.py
```

### LIVE15 Control Center

`live15-ui` 默认且强制绑定 `http://127.0.0.1:8765`，提供 Dashboard 和五个只读
API：`/api/health`、`/api/markets`、`/api/markets/{asset}`、`/api/coverage`、
`/api/system`，以及有界、可筛选的 `/api/events` operational diagnostics。端口可用
`LIVE15_UI_PORT` 或 `--port` 调整，但 host 不可配置为
`0.0.0.0`。服务使用只读 SQLite connection、typed response models、现有
`FeatureEngine` 和 recorder heartbeat，不复制 settlement 或 feature 公式。

Windows 可直接双击 `scripts\start_control_center.cmd`。launcher 只使用项目固定 `.venv`、固定
module 和 localhost URL；若 LIVE15 UI 已运行则只打开浏览器，若端口被其他程序占用则明确报错，
不会重复启动 Uvicorn。Dashboard 只允许三个固定 recorder lifecycle action：Start、Pause、
Resume。Start 必须观察到**本次启动后**的新 heartbeat 才返回 running；Pause 通过 cooperative
shutdown 等待在途只读 HTTP/SQLite 操作收尾，不使用 taskkill；重复 Start/Pause 幂等，PID lease
阻止多 recorder。Windows 上由 UI 启动的 recorder 使用无可见 console 的独立后台进程；手动运行
`live15-record` 仍保留前台 console 日志。UI/Uvicorn 与 recorder 是独立进程，关闭浏览器或 UI
不会停止采集，fatal diagnostics 仍写入 heartbeat 与有界 `recorder_events`。

没有 dataset build、shell、任意文件浏览、credential、Demo 或 Production trading route；也没有
kill/restart/任意命令参数。API 只返回白名单 health 字段，不返回 Kalshi key ID、private
key path、signature 或账户信息。heartbeat 缺失表示 recorder `stopped`；超龄显示
`stale`；市场字段缺失保持 JSON `null` 并带明确 availability/status，不填成零。
若任一 recorder worker 因 correctness/storage 错误退出，最终 heartbeat 会保存
`fatal_task` 与 `fatal_error_type`（不保存 exception message、路径或 payload）；预期的 bounded
upstream unavailable 仍只记录在对应 asset/source failure 中并继续其他任务。

Milestone 7.6B 在同一个 localhost backend 上提供无构建步骤的原生 HTML/CSS/JavaScript
Dashboard。左侧导航包含 Dashboard、Markets、Training Data、Warnings / Errors 与 System /
Health；资产卡和
detail 页面展示 Kalshi quote/orderbook、Coinbase predictive underlying、现有
`FeatureEngine` 投影及官方 finalized history。前端只格式化 typed API 字段，不计算交易或
settlement 业务事实；missing、stale、unsupported 均显示文字状态和 `—`，不会填零。

三个时间尺度相互独立：recorder 的 source collection cadence 由 recorder 配置控制，不受 UI
影响；API 轮询按成本分级并禁止重叠请求（health、markets/detail 2.5 秒，events 15 秒，system
30 秒，coverage 60 秒）；`Remaining` 仅使用服务端返回的 `window_end` 在浏览器 DOM 中每秒倒计时，
不发请求且不改变任何 lifecycle/settlement/training fact。页面隐藏时暂停全部刷新和倒计时。
coverage backend 另有 30 秒线程安全缓存与只读 SQLite snapshot，绝不会因每秒展示更新触发
全库 COUNT、integrity check 或 dataset rebuild。
浏览器关闭不会影响独立 recorder。Dashboard 不包含交易按钮、credential 页面或任何其他写
API。Warnings / Errors 页面按 severity、asset、exact source 和时间范围过滤最近的 bounded
diagnostics；普通成功 tick 不写该表，默认最多保留 5,000 条，避免日志/数据库无限增长。

Training Data 页面严格区分当前 raw store 的 finalized 总数与最新 immutable DatasetBuilder
snapshot 已评估的 finalized 数。snapshot 之后到达的事件显示为 `unevaluated`，不会被误称为
不可训练；完成的新 build 会持久化 stable rejection reason/count（例如
`missing_decision_time_metadata`）。missing/stale feature 仍由 feature diagnostics 表达，不会
为了提高 trainable 数量而放宽 as-of/leakage 约束。

### Recorder 生命周期

`live15-record` 启动 Kalshi-native lifecycle discovery、独立 REST fallback/cross-check、Coinbase predictive stream 与 health 独立任务。UI-managed recorder 在标准仓库外 read-only Production credential 文件存在时，同时启动一条 Kalshi WebSocket，动态订阅十个 current ticker；只有 `SYNCHRONIZED` atomic book 才是 live orderbook primary。配置 Pyth key并显式启用后，同一个 recorder 追加一个五-feed Hermes SSE ingestion task；启用 secondary 后再追加 Binance BNB 与 Hyperliquid HYPE 两个 public WebSocket tasks，仍然只有一个 recorder/process/SQLite writer。source identity/错误状态独立。lifecycle 默认每 15 秒按十个固定 series 和 UTC close-time range 发现 previous/current/next；REST quote loop 每个 batch 后默认等待 2 秒。只有 `LIVE15_ENABLE_ROBINHOOD_REFERENCE=true` 才额外启动 Robinhood 参考任务。

明确的 `end_time` 是训练 snapshot 的硬边界：`fetched_timestamp >= end_time` 的旧事件永不写入 `robinhood_snapshots`。如果旧事件结束而新事件尚未公开，recorder 进入可持久恢复的 `rollover_gap`，在 health/log 中报告 gap 开始时间和持续时间；它不会延长旧窗口、猜测下一事件或伪造 quote。上游页面继续返回旧事件的事实只写入隔离的 diagnostics 表。真正的新 event ID/contract ID 出现后才关闭 gap 并恢复正常记录。

按 `Ctrl+C` 即可安全停止。`asyncio` 会取消等待中的网络任务，已完成的 SQLite 事务不会丢失；下次运行同一命令会打开原数据库继续追加，不覆盖已有历史。

## 持久化设计

默认数据文件为 `data/live15.sqlite3`，整个 `data/` 继续由 Git ignore。可用 `LIVE15_RECORDER_DATA_PATH` 指向其他位置；在线 smoke test 始终使用 pytest 临时目录，不会接触正式历史。

当前选择 **SQLite + WAL** 作为热存储：

- WAL 和原子事务适合长时间持续追加及进程崩溃后的自动恢复；`synchronous=NORMAL` 在可靠性和行情写入吞吐间取平衡。WAL 每 1,000 pages 自动 checkpoint，并设置 64 MiB journal size limit，避免 checkpoint 后的 WAL 文件无界保留。
- `INSERT OR IGNORE` 与 observation fingerprint 保留 Robinhood/Coinbase 的精确 observation；official quote stream 仅压缩**连续相同**状态，价格变化后回到旧值仍会再次保存。
- `(event_id, fetched_timestamp, id)`、`(product, received_timestamp, id)` 和 official quote event/ticker 索引支持确定性读取，不在热路径重写整个文件。
- 所有 price、bid、ask、spread、size、volume 和 probability 均以 Decimal 原始字符串保存；绝不按 settlement rounding precision 截断。
- SQLite 可直接被 pandas、Polars 或 DuckDB 查询，后续可批量导出 Parquet。JSONL 缺少可靠事务和索引；Parquet 更适合冷数据批量文件，不适合当前逐 tick append 和 crash recovery，因此两者均未用作热存储。

当前数据库 metadata 为 `schema_version=10`；历史 immutable rows 保留其原 schema version。v5→v6 新增 primary provider-neutral underlying，v6→v7 新增独立 secondary，v7→v8 新增 WS raw/checkpoint，v8→v9 新增 append-only data gaps，v9→v10 只为 WS raw event 增加 nullable enqueue timing。迁移均在单一事务中执行；旧 WS rows 不伪造 enqueue timestamp，任何失败都会 rollback，未知或未来版本会在修改 recorder tables 前明确拒绝。

Kalshi settlement follow-up 偶尔会在已观察到 `settlement_pending` 或 finalized truth 后返回较旧的
`closed`/`determined` representation。recorder 只对严格白名单的同 ticker、同 series、同 event、
同 UTC window、同 target 退化状态记一条 `lifecycle_regression` warning，并保留更强状态和官方
settlement truth；不会写退化 lifecycle。身份/target 冲突、官方 YES/NO 冲突、malformed payload
和 SQLite correctness failure 仍 fail loudly，并写 typed fatal event（若 storage 仍可用）。

### Kalshi-native lifecycle / settlement schema

`kalshi_market_lifecycle` 是 append-only 官方状态 observation，保存 asset、series、ticker、event ticker、UTC window、原始 Decimal target、normalized lifecycle、官方 status、rules、settlement timer、determination result（若已发布）、fetch timestamp 与 source。`kalshi_settlements` 每 ticker 只能有一条 finalized truth；相同 truth 幂等，冲突写入 `kalshi_settlement_conflicts` 并抛错，绝不覆盖。`kalshi_backfill_state` 在每页提交 cursor，使 `/markets` 与 `/historical/markets` 回填可恢复。完整设计见 [Kalshi-native architecture](docs/kalshi_native_architecture.md)。

`KalshiBackfillService.run(asset, start=..., end=..., historical=True)` 可按 UTC range 回填 archive。每页逐条幂等保存 typed markets/final labels，完成该页后再提交 cursor；中断后同参数自动续跑并安全重放未完成页。API 顺序不参与 replay，重复 ticker/result 幂等。Production historical endpoint 对 `series_ticker + mve_filter=exclude` 的实际响应与文档组合存在 400 差异，因此实现只使用足以精确限定这十个非-MVE series 的 `series_ticker`，并对 `Target price: TBD` 等无效 archive placeholder fail closed。

WS raw storage 的离线归因、四方案 lossless benchmark、HOT/COLD 推荐与 quota 设计见
[`docs/storage_scaling.md`](docs/storage_scaling.md)。`live15-storage-audit --snapshot <fixed-copy>`
只接受 WAL-free 固定副本，并明确拒绝当前配置的 active recorder DB；它不会执行 VACUUM、
删除或修改 raw truth。

`live15-ws-retention` 提供 verified COLD archive 与有界 HOT retention。默认保留最近 6 小时
WS rows；immutable chunk 必须通过 reopen、checksum、逐事件相等与 deterministic book replay，
再由独立 manifest 提交后才可删除对应精确 ID range。配置的前三个 verified chunks 是启用 purge
前的最低 shadow gate；首次 production acceptance 已对四个 chunks 完成 verified purge/page reuse。
离线 `compact-copy` 只在 recorder 已停止且 WAL checkpoint 后运行，并验证 integrity、foreign keys、
完整 table inventory 与逐表 row counts；archive/manifest 始终位于 raw DB 外。
`live15-archive-maintenance --once` 是 scheduler-compatible、non-blocking 的单次入口：它立即
返回 `WAITING_FOR_RETENTION_ELIGIBILITY` 与 `next_eligible_at`，或验证 bounded chunks 后，
仅对已重新打开并复核 checksum/replay/manifest/连续 ID 的 `PURGE_ELIGIBLE` range 执行 20k-row
短事务。`--max-chunks` 和 `--max-purge-batches` 为每次执行设置绝对工作上限；命令不 sleep，
永不 compact，也不修改 Task Scheduler 或 Windows service。purge 只增加 SQLite freelist，物理
文件由后续写入优先复用；不会为视觉缩小文件而频繁 VACUUM。
recorder loop 与 CLI 共享可过期的 manifest maintenance lease，禁止两个进程同时处理同一 chunk；
crash 后可自动取得过期 lease。FAILED verification 会阻断后续 range，绝不跳过坏区间继续 purge。
`compact-copy` 另有 benefit gate：默认只有 SQLite freelist 可回收空间同时达到 8 GiB 和 DB
的 25% 才允许进入 managed offline compaction；阈值可由
`LIVE15_WS_COMPACTION_MIN_RECLAIM_BYTES` / `LIVE15_WS_COMPACTION_MIN_RECLAIM_PERCENT`
调整。archive/purge 可以持续积累，不会因为出现一个 eligible chunk 就频繁重写整个 DB。

`live15_quant.native_acceptance` 不依赖固定日期或固定 UTC 开盘时刻。它每次启动先按十个精确 series 动态发现 previous/current/next，选择仍有可观察时间且最接近结束的真实 OPEN market，然后只跟踪该 asset。只有新 market 的 `window_start` 严格等于旧 market 的 `window_end` 才算 rollover；排期或维护 gap 不会被伪造成相邻窗口。验收要求旧 ticker 的 OPEN→CLOSED→SETTLEMENT_PENDING→官方 SETTLED_YES/NO、successor quote、SQLite restart/integrity 全部成立。默认且绝对 wall-clock 上限为 1,800 秒；acceptance 禁用 transport 内部的多轮 retry，每个 GET timeout 都被剩余总预算截断，并由外层执行 bounded capped backoff，避免一次晚到请求越过总 deadline。期限内上游未提供有效窗口、相邻 successor 或 settlement 时返回结构化 `expected_upstream_unavailable`，而 instrument、timestamp、Decimal、storage 或 lifecycle correctness 错误仍直接失败。可选 `--database-path` 使中断后在同一隔离数据库幂等继续；未指定时使用并自动清理系统临时数据库。

2026-08-20 的 event-driven acceptance 实测 `rollover_latency_seconds=22.14`。该值严格定义为**新窗口官方 metadata 首次被本轮 discovery 收到的本地时间减去新窗口 `window_start`**，包含 polling phase、REST 请求耗时以及 target/market 发布延迟；它不是 Kalshi settlement latency、quote latency、订单延迟或交易执行延迟。该次官方 settlement timestamp 是独立字段，二者不得混用。

### Robinhood snapshot schema

表 `robinhood_snapshots` 每行是一条未聚合 observation：`asset`、`event_id`、`contract_id`、UTC `start_time`/`end_time`、HTTP 响应到达本地时记录的独立 UTC `fetched_timestamp`、`seconds_remaining`、完整精度 `target_price`、`displayed_yes`、`displayed_no`、`quote_availability`、`lifecycle`、`freshness`、`venue`、settlement benchmark/method/precision/source/data-access metadata、`source_url`、data role、schema version 和 content hash。缺失的 displayed No 或 venue 保持 SQL `NULL`；displayed probability 仍不是 executable quote。

表 `robinhood_diagnostics` 与训练 snapshots 隔离，保存 `post_end_event_returned`、`rollover_gap_started` 和 `rollover_gap_ended`。每种 diagnostic 按 asset + event ID 只保留一次，持续 gap 的时长由 health 和 ended record 表达，不会每 15 秒重复扩张 diagnostics。重启时 recorder 从此表恢复仍未结束的 gap。`ReplayReader.event()` 只读取严格早于 event end 的正常 snapshot；诊断信息只能通过显式 `event_diagnostics()` 读取。

### Coinbase tick schema

表 `coinbase_ticks` 保存 Coinbase payload 提供的 exchange timestamp，以及 WebSocket message/REST response 到达后、解析前立即记录的本地 receive timestamp；另存 product、完整精度 price/bid/ask/spread、公开 ticker 提供时的 bid size、ask size、last size 与 24-hour volume、predictive data role、schema version 和 content hash。Coinbase 仅是 BTC/ETH/XRP/SOL/DOGE 的 predictive source，绝不被标记或使用为 Kalshi settlement truth。

### Provider-neutral underlying schema

表 `underlying_observations` 保存 primary asset/provider/symbol/exact feed ID/未量化 Decimal price/source timestamp/receive timestamp/confidence/freshness/provenance。schema v7 的 `secondary_underlying_observations` 另存 venue instrument、price semantics、可选 bid/ask、source/receive/persist timestamp、两段 latency、provenance 与 source event ID。不同 role/provider 永不混合；v5→v6 与 v6→v7 都只新增表，不重写旧 raw rows，reader 兼容 v5/v6/v7。

### Official prediction quote schema

核心表 `kalshi_prediction_quotes` 只使用 native series/ticker/event ticker，另存 HTTP source timestamp 及其语义、本地 receive timestamp、Yes/No bid/ask、last trade、volume、Yes/No bid depth、freshness、executability classification、evidence、data role、schema version 和 content hash。所有 Decimal 按 source 字符串精度写入；source 缺失字段为 SQL `NULL`，不会互补或插值。旧 `prediction_market_quotes` 表保留 Robinhood-to-venue compatibility records，但 native recorder 不写它。

### Deterministic replay

`ReplayReader(path).kalshi_market(ticker)`、`.kalshi_quotes(ticker)` 和 `.kalshi_settlements(series=...)` 分别按官方 fetch time、quote receive time 与 window/ticker 的固定 tie-break 稳定重放。`training_label(ticker, decision_timestamp)` 只返回 decision time 当时已经 fetched/received 的 metadata 与 quote；settlement 被隔离在 `label` 字段，decision 在 settlement 时刻或之后会被拒绝。旧 `.event()`/`.quotes()` 继续提供 Robinhood compatibility replay。损坏的时间、Decimal 或 enum 会显式抛出 `RecorderStorageError`。

### Health

结构化 `kalshi_native_health` 日志包含当前十个 native market、最后 discovery、各 Kalshi asset 与 Coinbase product 的最后 receive time、已观察 settlement 数及本进程写入数；可选 Robinhood reference 只有独立 health flag。数据库行数也可用只读 SQL 核查：

```powershell
@'
from pathlib import Path
from live15_quant.storage import RecorderStore
with RecorderStore(Path("data/live15.sqlite3")) as store:
    print("Robinhood:", store.count("robinhood_snapshots"))
    print("Diagnostics:", store.count("robinhood_diagnostics"))
    print("Coinbase:", store.count("coinbase_ticks"))
    print("Kalshi native quotes:", store.count("kalshi_prediction_quotes"))
    print("Kalshi lifecycle:", store.count("kalshi_market_lifecycle"))
    print("Kalshi settlements:", store.count("kalshi_settlements"))
'@ | python
```

## 配置

| Variable | Default |
| --- | --- |
| `LIVE15_PRODUCTS` | `BTC-USD,ETH-USD,SOL-USD,XRP-USD,DOGE-USD` |
| `LIVE15_COINBASE_REST_URL` | `https://api.exchange.coinbase.com` |
| `LIVE15_COINBASE_WS_URL` | `wss://ws-feed.exchange.coinbase.com` |
| `LIVE15_ROBINHOOD_MAX_SOURCE_AGE_SECONDS` | `360` |
| `LIVE15_ROBINHOOD_POLL_INTERVAL_SECONDS` | `15` |
| `LIVE15_ENABLE_ROBINHOOD_REFERENCE` | `false`（可选参考；不属于核心 runtime） |
| `LIVE15_OFFICIAL_QUOTE_POLL_INTERVAL_SECONDS` | `2` |
| `LIVE15_OFFICIAL_QUOTE_MAX_SOURCE_AGE_SECONDS` | `15` |
| `LIVE15_OFFICIAL_QUOTE_ORDERBOOK_DEPTH` | `10` |
| `LIVE15_RECORDER_DATA_PATH` | `data/live15.sqlite3` |
| `LIVE15_RECORDER_CONTROL_PATH` | `data/recorder-control.json`（runtime；Git ignored） |
| `LIVE15_RECORDER_PID_PATH` | `data/recorder.pid`（runtime；Git ignored） |
| `LIVE15_RECORDER_HEALTH_INTERVAL_SECONDS` | `30` |
| `LIVE15_RECORDER_COINBASE_STALE_SECONDS` | `30` |
| `LIVE15_ENABLE_PYTH_UNDERLYING` | `false`；必须先配置仓库外 key 文件 |
| `LIVE15_PYTH_API_KEY_PATH` | 未设置；Pyth Terminal 创建后保存于仓库外，不要贴入聊天/日志 |
| `LIVE15_PYTH_REST_FALLBACK_INTERVAL_SECONDS` | `2`；仅在 SSE outage/reconnect cycle 使用 |
| `LIVE15_PYTH_STREAM_READ_TIMEOUT_SECONDS` | `20`；idle connection bounded recovery |
| `LIVE15_PYTH_REQUEST_BUDGET_PER_10_SECONDS` | `8`；必须在 `1..10` |
| `LIVE15_RECORDER_PYTH_STALE_SECONDS` | `15` |
| `LIVE15_ENABLE_SECONDARY_UNDERLYING` | `false` for manual CLI；Control Center managed recorder defaults `true` |
| `LIVE15_RECORDER_SECONDARY_STALE_SECONDS` | `10` |
| `LIVE15_FEATURE_STORE_PATH` | `data/features.sqlite3` |
| `LIVE15_DATASET_DECISION_OFFSETS_SECONDS` | `840,720,600,480,300,180,120,60,30` |
| `LIVE15_DATASET_QUOTE_MAX_AGE_SECONDS` | `15` |
| `LIVE15_DATASET_UNDERLYING_MAX_AGE_SECONDS` | `15` |
| `LIVE15_PAPER_DATA_PATH` | `data/paper.sqlite3` |
| `LIVE15_PAPER_ACCOUNT_ID` | `local-paper` |
| `LIVE15_PAPER_STARTING_CASH` | `1000` |
| `LIVE15_PAPER_SIGNAL_INTERVAL_SECONDS` | `90` |
| `LIVE15_PAPER_MAX_ORDER_NOTIONAL` | `10` |
| `LIVE15_PAPER_MAX_EVENT_EXPOSURE` | `25` |
| `LIVE15_PAPER_MAX_DAILY_LOSS` | `20` |
| `LIVE15_PAPER_MAX_TOTAL_EXPOSURE` | `100` |
| `LIVE15_PAPER_MAX_CONSECUTIVE_LOSSES` | `3` |
| `LIVE15_PAPER_KILL_SWITCH` | `false`（设为 `true` 会在 hard-risk gate 阻断所有新 paper orders） |
| `LIVE15_KALSHI_DEMO_API_KEY_ID` | 未设置；仅接受 Kalshi Demo key ID，且不会写入日志 |
| `LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH` | 未设置；必须是仓库外 `.key`/`.pem` 的绝对路径 |
| `LIVE15_REQUEST_TIMEOUT_SECONDS` | `10` |
| `LIVE15_RECONNECT_DELAY_SECONDS` | `3` |
| `LIVE15_WS_PING_INTERVAL_SECONDS` | `20` |
| `LIVE15_WS_PING_TIMEOUT_SECONDS` | `20` |
| `LIVE15_REST_POLL_INTERVAL_SECONDS` | `5` |
| `LIVE15_LOG_LEVEL` | `INFO` |

## 验证

```powershell
ruff check .
ruff format --check .
python -c "import live15_quant.dataset; import live15_quant.feature_registry; import live15_quant.features; import live15_quant.splits"
pytest
python -m pip check
git diff --check
```

在线 smoke tests 默认跳过，必须显式开启：

```powershell
$env:LIVE15_RUN_SMOKE = "1"
pytest -m smoke
```

### Kalshi Demo 只读连接审计

项目提供 `live15-kalshi-demo-audit`，但它不是 execution adapter。其主机固定为官方 Demo
REST endpoint，只能依次读取 balance、open markets、positions、orders 和 fills；client 没有
POST、DELETE、create-order 或 cancel-order 方法，也不会连接 Production。Demo WebSocket endpoint
已记录，但当前审计不订阅它。

本人在 Kalshi Demo 的 **Account & security → API Keys** 创建环境专属 key 后，把下载的
`.key` 放到仓库外受当前 Windows 用户保护的目录，再仅在当前 shell 设置两个环境变量：

```powershell
$env:LIVE15_KALSHI_DEMO_API_KEY_ID = "<Demo API Key ID>"
$env:LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH = "C:\absolute\private\path\kalshi-demo.key"
live15-kalshi-demo-audit
```

不要把 key 内容粘贴进命令、`.env`、代码、测试或 issue。`.key`、`.pem`、`.env*` 和常见
secrets 目录也已 Git ignored 作为第二道保护，但正式要求仍是私钥必须在仓库外。需要显式
运行 authenticated smoke 时，额外设置 `LIVE15_RUN_KALSHI_DEMO_AUDIT=1`；普通 `pytest`
不会访问账户。完整官方能力与安全边界见
[`docs/kalshi_execution_api_audit.md`](docs/kalshi_execution_api_audit.md)。

## 已知限制

- Robinhood 网页明确声明 live data 仅供参考，可能延迟或错误；公开页面的缓存和结构可能变化。
- 页面当前通常显示 Yes probability，不提供可独立验证的 displayed No probability。
- 部分公开页面快照只在 `__NEXT_DATA__` 中提供 event/contract metadata、不渲染 quote card；这类 quote 的 `availability=unsupported`，Yes/No 均为 `null`。
- Kalshi-native discovery 不依赖 Robinhood；contract 自身 target 未发布、malformed 或 candidate 冲突时会暂时无 current/next，而不会借用其他窗口 target。
- Kalshi `finalized/result/settlement_ts` 已作为唯一自动化 settlement truth；CF Benchmarks/Pyth 是 determination benchmark metadata，Coinbase spot 永远不得替代最终标签。
- Coinbase 不覆盖 Gold、Silver、WTI Oil、HYPE、BNB；这些资产已有 Kalshi native lifecycle/quotes/settlement labels，但没有同步 Coinbase predictive ticks。
- 上述五资产已通过 authenticated Pyth Hermes multi-feed SSE 正式启用；历史 Pyth 覆盖从启用时刻开始积累，启用前窗口仍会如实保持 missing。SSE 不可用时只使用单个 five-feed batch REST fallback，不会回退旧匿名 endpoint 或五路独立 polling。
- Robinhood public category page 只暴露当前 snapshot，若页面缓存、暂时缺少 card 或事件在两次轮询之间出现并消失，recorder 无法补回从未公开观察到的数据。
- 页面偶尔先发布 upcoming event state、稍后才发布 contract ID/target；这类 placeholder 会产生结构化 warning 并暂不写入，待公开 metadata 完整后才开始记录，绝不猜测 ID 或 target。
- Kalshi official venue orderbook 与 finalized settlement truth 均已采集；venue book 仍不等同 Robinhood executable quote。
- Kalshi 免认证 REST 的 market `updated_time` 不是逐次 order-book event timestamp；当前保存 HTTP `Date`（秒级 response-time 语义）与本地 receive timestamp。Production WebSocket 所有握手均需 Production API key；Demo credential 不会用于 Production。continuous recorder 已接入固定官方 host、RSA-PSS handshake、bounded receive pump、snapshot/delta parser、subscription-level seq guard、`get_snapshot` resync、event-driven add-successor/remove-predecessor、schema v8/v10 raw timing/checkpoint 与 deterministic replay。只有 synchronized WS book 是 live primary；gap/reconnect 期间 fail-closed，REST 仍按 `kalshi_rest` provenance 独立保存。完整审计见 [`docs/kalshi_websocket_readonly_audit.md`](docs/kalshi_websocket_readonly_audit.md)。
- REST market top-of-book 与 orderbook depth 来自相邻的两次请求，不是交易所原子快照；因此它只作为 fallback/cross-check，绝不冒充 atomic WS book。
- 30 分钟 acceptance 中大量 `price_moved` 主要来自 REST market 与 orderbook 两次非原子读取之间的变化。这是正确性保护而非放宽条件的理由；后续应使用官方 authenticated WebSocket orderbook snapshot/delta 构造单一原子 market state，在完成前不得取消 top-of-book/depth 一致性检查。
- Paper fills 是基于轮询时观察到的 venue depth 的保守本地模拟，不代表真实 queue position、网络延迟、成交保证或 Robinhood executable quote；fill uncertainty 会被 hard-risk layer 阻断。
- Settlement truth 已独立落库，但 Milestone 6 不改变 paper settlement accounting；到期未平仓 paper positions 仍保持 `pending_settlement`，后续里程碑再以显式 settlement adapter 接入，当前不伪造 payout。
- 当前已有 Kalshi Demo-only RSA-PSS signer 与 authenticated GET-only connectivity audit，但没有 Demo/Production execution client，也没有任何仓库内 credential。用户仍需本人创建 Demo account/key 并安全保管 RSA private key；Demo 与 Production credentials 不通用。
- SQLite recorder 已实现 verified WS retention/压缩，但尚未实现 Parquet export 或多进程同时写入；单 recorder 进程是当前支持的运行模式。
- Feature store 当前使用 SQLite，而不是 Parquet；它支持确定性 replay 和后续 pandas/Polars/DuckDB 读取，但尚未提供批量 Parquet export。历史 backfill 若只有事后 finalized metadata、没有 decision-time quote/tick observation，会被诚实跳过，不能凭最终结果重建当时 feature。
