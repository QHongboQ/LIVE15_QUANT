# LIVE15_QUANT

LIVE15_QUANT 是一个**只针对 Robinhood Live 15-minute prediction-market contracts** 的数据研究项目。范围不包括小时、日、周、体育、政治合约，也暂不包括模型、特征工程、回测、数据库、Paper Trading 或真钱交易。

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

`Robinhood15MinuteProvider` 读取公开的 [Robinhood 15-minute category page](https://robinhood.com/us/en/prediction-markets/15-min/)，解析页面 HTML 和公开嵌入的 `__NEXT_DATA__`。`robots.txt` 没有禁止该路径。provider 不调用浏览器后台接口，也不依赖认证。

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

## 配置

| Variable | Default |
| --- | --- |
| `LIVE15_PRODUCTS` | `BTC-USD,ETH-USD,SOL-USD,XRP-USD,DOGE-USD` |
| `LIVE15_COINBASE_REST_URL` | `https://api.exchange.coinbase.com` |
| `LIVE15_COINBASE_WS_URL` | `wss://ws-feed.exchange.coinbase.com` |
| `LIVE15_ROBINHOOD_MAX_SOURCE_AGE_SECONDS` | `360` |
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
python -c "import live15_quant.providers.coinbase; import live15_quant.providers.robinhood_15min"
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
- 当前不持久化任何数据，这是后续 milestone 的范围。
