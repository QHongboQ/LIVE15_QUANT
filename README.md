# LIVE15_QUANT

LIVE15_QUANT 是一个面向多资产加密货币 15 分钟预测市场的量化研究项目。目前里程碑仅覆盖可复现的工程基础和公开 Coinbase 市场数据采集；项目不包含真钱下单，也不调用 Robinhood 私有或未公开接口。

## 当前能力

- Coinbase Exchange 公共 REST ticker：BTC-USD 价格、bid、ask。
- Coinbase Exchange 公共 WebSocket ticker：默认监控 BTC、ETH、SOL、XRP、DOGE。
- 环境变量统一配置、结构化 JSON logging、typed market model。
- pytest 单元测试，以及需要显式开启的 Coinbase 在线 smoke tests。
- Ruff lint 和 GitHub Actions CI。

Coinbase 数据只是研究输入。Robinhood 15 分钟合约按 CF Benchmarks RTI 的规则结算，因此不能把 Coinbase 价格当作最终结算价格。

## 环境安装

要求 Windows PowerShell 和 Python 3.13：

```powershell
# 确认当前 python 是 3.13.x
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install pip==26.2.1
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
```

`requirements.lock` 固定完整运行与开发依赖；`pyproject.toml` 定义项目元数据、直接依赖、命令入口及工具配置。

## 运行

```powershell
# 每 5 秒轮询 BTC REST ticker
live15-rest

# 默认五资产 WebSocket stream
live15-stream

# 仅 BTC WebSocket stream
live15-btc-stream
```

原有入口继续可用：

```powershell
python btc_price_test.py
python btc_stream.py
python market_stream.py
```

日志按一行一个 JSON object 输出，适合后续写入日志采集或持久化管线。

## 配置

所有配置均可通过环境变量覆盖：

| 变量 | 默认值 |
| --- | --- |
| `LIVE15_PRODUCTS` | `BTC-USD,ETH-USD,SOL-USD,XRP-USD,DOGE-USD` |
| `LIVE15_COINBASE_REST_URL` | `https://api.exchange.coinbase.com` |
| `LIVE15_COINBASE_WS_URL` | `wss://ws-feed.exchange.coinbase.com` |
| `LIVE15_REQUEST_TIMEOUT_SECONDS` | `10` |
| `LIVE15_RECONNECT_DELAY_SECONDS` | `3` |
| `LIVE15_WS_PING_INTERVAL_SECONDS` | `20` |
| `LIVE15_WS_PING_TIMEOUT_SECONDS` | `20` |
| `LIVE15_REST_POLL_INTERVAL_SECONDS` | `5` |
| `LIVE15_LOG_LEVEL` | `INFO` |

## 质量检查

```powershell
ruff check .
python -c "import live15_quant; import live15_quant.providers.coinbase"
pytest
```

在线 smoke tests 默认跳过，只有显式允许访问公共 Coinbase 服务时运行：

```powershell
$env:LIVE15_RUN_SMOKE = "1"
pytest -m smoke
```

## 当前边界

- 尚未实现数据持久化、15 分钟窗口构建、特征工程、模型、回测或 paper trading。
- 尚未实现 Robinhood event discovery/provider。
- 不包含认证信息、账户访问、订单执行或真钱交易能力。
