"""Audited Robinhood Live 15-minute settlement specifications."""

from __future__ import annotations

from live15_quant.models import Asset, AssetSupport, SettlementSpec, SupportLevel

_CF_METHOD = (
    "Yes when the simple average of 60 one-second RTI observations immediately before the "
    "end is at least the corresponding 60-observation average immediately before the start."
)
_PYTH_METHOD = (
    "Yes when the Pyth 1-minute candlestick close at the window end is at least the target; "
    "the timestamped candle covers the immediately preceding minute, and the most recent "
    "published value is used if the specified value is unavailable."
)
_CF_DATA = "https://docs.cfbenchmarks.com/api/"
_PYTH_DATA = "https://docs.pyth.network/price-feeds/core/getting-started"
_CF_ACCESS = (
    "Benchmark identity and method are public. Exact real-time and historical API values require "
    "a CF Benchmarks licensed API key, so automated settlement-truth acquisition is not enabled."
)
_PYTH_ACCESS = (
    "Pyth feeds are permissionless on-chain, but the hosted Hermes API requires an API key as of "
    "2026-08-18. Exact Robinhood benchmark-series acquisition is not enabled without verified "
    "access."
)


def _cf(asset: Asset, benchmark: str, decimals: int | None, source_url: str) -> SettlementSpec:
    return SettlementSpec(
        asset=asset,
        benchmark=benchmark,
        method=_CF_METHOD,
        decimal_places=decimals,
        source_url=source_url,
        benchmark_data_url=_CF_DATA,
        data_access=SupportLevel.PARTIAL,
        access_notes=_CF_ACCESS,
    )


def _pyth(asset: Asset, benchmark: str, decimals: int, source_url: str) -> SettlementSpec:
    return SettlementSpec(
        asset=asset,
        benchmark=benchmark,
        method=_PYTH_METHOD,
        decimal_places=decimals,
        source_url=source_url,
        benchmark_data_url=_PYTH_DATA,
        data_access=SupportLevel.PARTIAL,
        access_notes=_PYTH_ACCESS,
    )


SETTLEMENT_SPECS: dict[Asset, SettlementSpec] = {
    Asset.BTC: _cf(
        Asset.BTC,
        "CF Benchmarks BRTI",
        2,
        "https://robinhood.com/us/en/prediction-markets/crypto/events/"
        "btc-15-min-62-26563-target-jul-13-2026/",
    ),
    Asset.ETH: _cf(
        Asset.ETH,
        "CF Benchmarks ETHUSDRTI",
        None,
        "https://robinhood.com/us/en/prediction-markets/crypto/events/"
        "eth-15-min-1-92379-target-jul-15-2026/",
    ),
    Asset.XRP: _cf(
        Asset.XRP,
        "CF Benchmarks XRPUSDRTI",
        4,
        "https://robinhood.com/us/en/prediction-markets/crypto/events/"
        "xrp-15-min-10450-target-jun-27-2026/",
    ),
    Asset.SOL: _cf(
        Asset.SOL,
        "CF Benchmarks SOLUSDRTI",
        4,
        "https://robinhood.com/us/en/prediction-markets/crypto/events/"
        "sol-15-min-734302-target-jun-15-2026/",
    ),
    Asset.HYPE: _cf(
        Asset.HYPE,
        "CF Benchmarks HYPEUSDRTI",
        4,
        "https://robinhood.com/us/en/prediction-markets/crypto/events/"
        "hype-15-min-590979-target-jul-18-2026/",
    ),
    Asset.DOGE: _cf(
        Asset.DOGE,
        "CF Benchmarks DOGEUSDRTI",
        7,
        "https://robinhood.com/us/en/prediction-markets/crypto/events/"
        "doge-15-min-01010484-target-may-29-2026/",
    ),
    Asset.BNB: _cf(
        Asset.BNB,
        "CF Benchmarks BNBUSDRTI",
        2,
        "https://robinhood.com/us/en/prediction-markets/crypto/events/"
        "bnb-15-min-57983-target-jul-15-2026/",
    ),
    Asset.GOLD: _pyth(
        Asset.GOLD,
        "Pyth - Gold",
        2,
        "https://robinhood.com/us/en/prediction-markets/metals/events/"
        "gold-15-min-4-10014-target-aug-03-2026/",
    ),
    Asset.SILVER: _pyth(
        Asset.SILVER,
        "Pyth - Silver",
        3,
        "https://robinhood.com/us/en/prediction-markets/metals/events/"
        "silver-15-min-58140-target-aug-03-2026/",
    ),
    Asset.WTI_OIL: _pyth(
        Asset.WTI_OIL,
        "Pyth - WTI",
        2,
        "https://robinhood.com/us/en/prediction-markets/commodities/events/"
        "wti-oil-15-min-7964-target-aug-03-2026/",
    ),
}


PREDICTIVE_SOURCE_SUPPORT: dict[Asset, SupportLevel] = {
    asset: (
        SupportLevel.FULL
        if asset in {Asset.BTC, Asset.ETH, Asset.XRP, Asset.SOL, Asset.DOGE}
        else SupportLevel.UNSUPPORTED
    )
    for asset in Asset
}

ASSET_SUPPORT: dict[Asset, AssetSupport] = {
    asset: AssetSupport(
        asset=asset,
        discovery=SupportLevel.FULL,
        predictive_input=PREDICTIVE_SOURCE_SUPPORT[asset],
        displayed_quote=SupportLevel.PARTIAL,
        settlement_metadata=(SupportLevel.PARTIAL if asset is Asset.ETH else SupportLevel.FULL),
        settlement_truth=SETTLEMENT_SPECS[asset].data_access,
        overall=SupportLevel.PARTIAL,
    )
    for asset in Asset
}
