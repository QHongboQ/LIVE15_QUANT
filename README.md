# LIVE15_QUANT

LIVE15_QUANT 是一个**只针对 Robinhood Live 15-minute prediction-market contracts** 的数据研究项目。当前实现持续采集并持久化原始事件快照和对应的 Coinbase predictive ticks；范围不包括小时、日、周、体育、政治合约，也暂不包括模型、特征工程、回测、Paper Trading 或真钱交易。

项目仅使用无需登录且允许公开访问的数据：Coinbase Exchange 公共市场数据和 Robinhood 服务端渲染的公开网页。代码不调用 Robinhood 私有/未公开 API，不访问账户，不包含订单或交易能力。

## 三类数据严格分离

| 数据角色 | 当前来源 | 用途与限制 |
| --- | --- | --- |
| Predictive market-data source | Coinbase Exchange REST/WebSocket | BTC、ETH、XRP、SOL、DOGE 的预测输入；**不是结算真值** |
| Robinhood contract market quote | Robinhood 公开 15-minute 网页 | 页面显示的 Yes probability；信息性、可能延迟或错误、不可执行；页面未公开 No 时保存为 `null` |
| Actual settlement benchmark | CF Benchmarks RTI 或 Pyth | 合约条款指定的结算源；当前记录官方映射和规则，但不冒用 Coinbase 代替 |

## 当前支持矩阵

“Full” 表示该维度已通过公开来源发现并标准化；“Partial” 表示元数据已验证，但公开访问或字段完整性有限；“Unsupported” 表示当前没有合适的数据源。

| Asset | 15-min discovery + normalization | Coinbase predictive input | Robinhood displayed quote | Settlement metadata | Automated settlement truth |
| --- | --- | --- | --- | --- | --- |
| BTC | Full | Full (`BTC-USD`) | Partial | Full: CF Benchmarks BRTI | Partial: licensed API required |
| ETH | Full | Full (`ETH-USD`) | Partial | Partial: CF Benchmarks ETHUSDRTI（rounding precision 未公开） | Partial: licensed API required |
| Gold | Full | Unsupported | Partial | Full: Pyth - Gold | Partial: hosted API access/exact series validation required |
| Silver | Full | Unsupported | Partial | Full: Pyth - Silver | Partial: hosted API access/exact series validation required |
| XRP | Full | Full (`XRP-USD`) | Partial | Full: CF Benchmarks XRPUSDRTI | Partial: licensed API required |
| WTI Oil | Full | Unsupported | Partial | Full: Pyth - WTI | Partial: hosted API access/exact series validation required |
| SOL | Full | Full (`SOL-USD`) | Partial | Full: CF Benchmarks SOLUSDRTI | Partial: licensed API required |
| HYPE | Full | Unsupported | Partial | Full: CF Benchmarks HYPEUSDRTI | Partial: licensed API required |
| DOGE | Full | Full (`DOGE-USD`) | Partial | Full: CF Benchmarks DOGEUSDRTI | Partial: licensed API required |
| BNB | Full | Unsupported | Partial | Full: CF Benchmarks BNBUSDRTI | Partial: licensed API required |

因此，Milestone 2 的**事件发现和标准化覆盖 10/10 资产**，但若按“公开 contract quote + 可自动取得的实际 settlement truth”端到端口径衡量，10 个资产目前都只能标记为 Partial，没有资产可诚实标为 Full。未支持项是 Gold、Silver、WTI、HYPE、BNB 的 Coinbase predictive input；它们不影响 Robinhood 合约发现。

## Discovery 与标准化

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

[CF Benchmarks API](https://docs.cfbenchmarks.com/api/) 明确要求 licensed API key；因此本项目没有伪造或替代 RTI observations。[Pyth](https://docs.pyth.network/price-feeds/core/getting-started) 的价格流在链上 permissionless，但其 hosted Hermes API 自 2026-08-18 起要求 API key，而且仍需确认 Robinhood 所指的精确 benchmark series。因此目前只记录结算规格，不声称已经取得最终结算真值。

## Partner venue / executable quote 调查

Robinhood 公共事件页只说明合约可能由 KalshiEX、ForecastEx 或 Rothera 提供，不公开每个事件对应的 venue ticker。

- [Kalshi public market-data API](https://docs.kalshi.com/getting_started/quick_start_market_data) 允许免认证读取 markets 和 orderbook；WebSocket 需要认证，交易端点需要账户权限。
- ForecastEx 官方公开 markets 页面可查看部分合约，但未发现能可靠映射这些 Robinhood 15-minute IDs 的公开开发者 feed。
- Rothera 官方网站公开产品和监管资料，但未发现等价的公开行情 API。

由于缺少 Robinhood event ID 到 partner ticker 的官方映射，本项目不会通过标题或价格模糊匹配生成“可执行报价”。当前合法获得的是 Robinhood 网页的**信息性 displayed quote**；不是 executable quote。

### Detail-page live quote audit

2026-08-20 对未登录公开详情页的浏览器网络行为进行了审计。页面通过重复 XHR 读取 `api.robinhood.com` 下的 event-state、contract quote、fundamentals 和 15-second historical 数据；未观察到 SSE 或 WebSocket。全新无 Cookie、无 Authorization header 的 HTTP session 能读取 quote JSON，字段包括 Yes/No bid/ask、last trade、instrument ID 和源更新时间。

这些 Prediction Market 路由没有出现在 [Robinhood 的公开 API 文档](https://docs.robinhood.com/) 中；[Robinhood 官方 third-party connections 说明](https://robinhood.com/us/en/support/articles/third-party-connections/) 也没有授权此类未发布接口供第三方 collector 使用。因此“无需认证即可响应”不被当作“公开且允许的 API”。本项目**不调用、不封装、不持久化这些路由**，SSR discovery/quote 路径继续保留，quote capability 明确保持 Partial。若 Robinhood 或 partner venue 后续发布允许自动采集的官方 market-data API，再以独立 typed provider 接入。

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
# 持续记录全部 Robinhood 目标资产 + 5 个 Coinbase predictive products
live15-record

# 一次性发现并输出当前公开 Robinhood 15-minute snapshot（JSON logs）
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

### Recorder 生命周期

`live15-record` 启动三个协作任务：Robinhood category page 默认每 15 秒轮询一次；Coinbase 对 `BTC-USD`、`ETH-USD`、`SOL-USD`、`XRP-USD`、`DOGE-USD` 使用一个公共 WebSocket ticker subscription；health 默认每 30 秒写一条结构化日志。每次 discovery 会记录当前 10 个目标资产中实际公开的事件，从首次出现持续观察到 category page 将其标为 closed/settled、窗口结束或事件被新窗口替换。

明确的 `end_time` 是训练 snapshot 的硬边界：`fetched_timestamp >= end_time` 的旧事件永不写入 `robinhood_snapshots`。如果旧事件结束而新事件尚未公开，recorder 进入可持久恢复的 `rollover_gap`，在 health/log 中报告 gap 开始时间和持续时间；它不会延长旧窗口、猜测下一事件或伪造 quote。上游页面继续返回旧事件的事实只写入隔离的 diagnostics 表。真正的新 event ID/contract ID 出现后才关闭 gap 并恢复正常记录。

按 `Ctrl+C` 即可安全停止。`asyncio` 会取消等待中的网络任务，已完成的 SQLite 事务不会丢失；下次运行同一命令会打开原数据库继续追加，不覆盖已有历史。

## 持久化设计

默认数据文件为 `data/live15.sqlite3`，整个 `data/` 继续由 Git ignore。可用 `LIVE15_RECORDER_DATA_PATH` 指向其他位置；在线 smoke test 始终使用 pytest 临时目录，不会接触正式历史。

当前选择 **SQLite + WAL** 作为热存储：

- WAL 和原子事务适合长时间持续追加及进程崩溃后的自动恢复；`synchronous=NORMAL` 在可靠性和行情写入吞吐间取平衡。WAL 每 1,000 pages 自动 checkpoint，并设置 64 MiB journal size limit，避免 checkpoint 后的 WAL 文件无界保留。
- `INSERT OR IGNORE` 与 observation fingerprint 只拒绝 event/product、receive timestamp 和完整内容都相同的精确重复；同一时刻的真实价格变化会保留。
- `(event_id, fetched_timestamp, id)` 和 `(product, received_timestamp, id)` 索引支持单事件/产品确定性读取，不在热路径重写整个文件。
- 所有 price、bid、ask、spread、size、volume 和 probability 均以 Decimal 原始字符串保存；绝不按 settlement rounding precision 截断。
- SQLite 可直接被 pandas、Polars 或 DuckDB 查询，后续可批量导出 Parquet。JSONL 缺少可靠事务和索引；Parquet 更适合冷数据批量文件，不适合当前逐 tick append 和 crash recovery，因此两者均未用作热存储。

数据库 metadata 和每一行都包含 `schema_version=2`。已有 v1 recorder 数据库会在一个 `BEGIN IMMEDIATE` transaction 内创建 diagnostics schema、保留原数据精度并把兼容行升级到 v2；任何检查失败都会 rollback。未知或未来版本会在修改 recorder tables 前明确拒绝，避免静默误读。

### Robinhood snapshot schema

表 `robinhood_snapshots` 每行是一条未聚合 observation：`asset`、`event_id`、`contract_id`、UTC `start_time`/`end_time`、HTTP 响应到达本地时记录的独立 UTC `fetched_timestamp`、`seconds_remaining`、完整精度 `target_price`、`displayed_yes`、`displayed_no`、`quote_availability`、`lifecycle`、`freshness`、`venue`、settlement benchmark/method/precision/source/data-access metadata、`source_url`、data role、schema version 和 content hash。缺失的 displayed No 或 venue 保持 SQL `NULL`；displayed probability 仍不是 executable quote。

表 `robinhood_diagnostics` 与训练 snapshots 隔离，保存 `post_end_event_returned`、`rollover_gap_started` 和 `rollover_gap_ended`。每种 diagnostic 按 asset + event ID 只保留一次，持续 gap 的时长由 health 和 ended record 表达，不会每 15 秒重复扩张 diagnostics。重启时 recorder 从此表恢复仍未结束的 gap。`ReplayReader.event()` 只读取严格早于 event end 的正常 snapshot；诊断信息只能通过显式 `event_diagnostics()` 读取。

### Coinbase tick schema

表 `coinbase_ticks` 保存 Coinbase payload 提供的 exchange timestamp，以及 WebSocket message/REST response 到达后、解析前立即记录的本地 receive timestamp；另存 product、完整精度 price/bid/ask/spread、公开 ticker 提供时的 bid size、ask size、last size 与 24-hour volume、predictive data role、schema version 和 content hash。Coinbase 仅是 BTC/ETH/XRP/SOL/DOGE 的 predictive source，绝不被标记或使用为 Robinhood settlement truth。

### Deterministic replay

`ReplayReader(path).event(event_id)` 按 `fetched_timestamp, insertion id` 稳定重放单个 Robinhood event，并防御性排除 `fetched_timestamp >= end_time` 的遗留 active observations；`ReplayReader(path).coinbase(product)` 按本地 `received_timestamp, insertion id` 稳定重放一个 Coinbase product。reader 只恢复 typed records，不包含策略、回测或 time-alignment 假设。损坏的时间、Decimal 或 enum 会显式抛出 `RecorderStorageError`，不会静默跳过。

### Health

结构化 `recorder_health` 日志包含：当前 tracking event 数、最后 Robinhood snapshot 时间、各 Coinbase product 最后 receive 时间、stale/missing source 数、本进程成功写入记录数，以及每个 active rollover gap 的资产、前一 event ID、开始时间和持续秒数。Robinhood 使用 provider 的 freshness 判断；Coinbase 默认 30 秒未更新即 stale。数据库行数也可用只读 SQL 核查：

```powershell
@'
from pathlib import Path
from live15_quant.storage import RecorderStore
with RecorderStore(Path("data/live15.sqlite3")) as store:
    print("Robinhood:", store.count("robinhood_snapshots"))
    print("Diagnostics:", store.count("robinhood_diagnostics"))
    print("Coinbase:", store.count("coinbase_ticks"))
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
| `LIVE15_RECORDER_DATA_PATH` | `data/live15.sqlite3` |
| `LIVE15_RECORDER_HEALTH_INTERVAL_SECONDS` | `30` |
| `LIVE15_RECORDER_COINBASE_STALE_SECONDS` | `30` |
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
python -c "import live15_quant.providers.coinbase; import live15_quant.providers.robinhood_15min; import live15_quant.recorder; import live15_quant.records; import live15_quant.replay; import live15_quant.storage"
pytest
python -m pip check
git diff --check
```

在线 smoke tests 默认跳过，必须显式开启：

```powershell
$env:LIVE15_RUN_SMOKE = "1"
pytest -m smoke
```

## 已知限制

- Robinhood 网页明确声明 live data 仅供参考，可能延迟或错误；公开页面的缓存和结构可能变化。
- 页面当前通常显示 Yes probability，不提供可独立验证的 displayed No probability。
- 部分公开页面快照只在 `__NEXT_DATA__` 中提供 event/contract metadata、不渲染 quote card；这类 quote 的 `availability=unsupported`，Yes/No 均为 `null`。
- 页面不披露单事件的 partner venue ticker，无法合法、可靠地映射 executable orderbook。
- CF Benchmarks/Pyth 实际 settlement truth 尚未自动采集；不得用 Coinbase spot 替代。
- Coinbase 不覆盖 Gold、Silver、WTI Oil、HYPE、BNB，因此这些资产当前只有 Robinhood event snapshots，没有同步 predictive ticks。
- Robinhood public category page 只暴露当前 snapshot，若页面缓存、暂时缺少 card 或事件在两次轮询之间出现并消失，recorder 无法补回从未公开观察到的数据。
- 页面偶尔先发布 upcoming event state、稍后才发布 contract ID/target；这类 placeholder 会产生结构化 warning 并暂不写入，待公开 metadata 完整后才开始记录，绝不猜测 ID 或 target。
- 当前没有实际 CF Benchmarks/Pyth settlement series、partner venue executable orderbook 或最终 payout；数据库只保存已验证的 settlement metadata。
- SQLite recorder 尚未实现 retention、压缩、Parquet export 或多进程同时写入；单 recorder 进程是当前支持的运行模式。
