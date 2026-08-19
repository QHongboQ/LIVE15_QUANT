from __future__ import annotations

from live15_quant.models import Asset, DataRole, SupportLevel
from live15_quant.settlement import ASSET_SUPPORT, PREDICTIVE_SOURCE_SUPPORT, SETTLEMENT_SPECS


def test_all_target_assets_have_audited_settlement_specs() -> None:
    assert set(SETTLEMENT_SPECS) == set(Asset)
    assert all(spec.role is DataRole.SETTLEMENT_BENCHMARK for spec in SETTLEMENT_SPECS.values())


def test_crypto_and_non_crypto_benchmarks_remain_distinct() -> None:
    assert SETTLEMENT_SPECS[Asset.BTC].benchmark == "CF Benchmarks BRTI"
    assert "60 one-second RTI" in SETTLEMENT_SPECS[Asset.SOL].method
    assert SETTLEMENT_SPECS[Asset.GOLD].benchmark == "Pyth - Gold"
    assert "1-minute candlestick close" in SETTLEMENT_SPECS[Asset.WTI_OIL].method


def test_every_settlement_mapping_matches_audited_name_and_precision() -> None:
    expected = {
        Asset.BTC: ("CF Benchmarks BRTI", 2),
        Asset.ETH: ("CF Benchmarks ETHUSDRTI", None),
        Asset.XRP: ("CF Benchmarks XRPUSDRTI", 4),
        Asset.SOL: ("CF Benchmarks SOLUSDRTI", 4),
        Asset.HYPE: ("CF Benchmarks HYPEUSDRTI", 4),
        Asset.DOGE: ("CF Benchmarks DOGEUSDRTI", 7),
        Asset.BNB: ("CF Benchmarks BNBUSDRTI", 2),
        Asset.GOLD: ("Pyth - Gold", 2),
        Asset.SILVER: ("Pyth - Silver", 3),
        Asset.WTI_OIL: ("Pyth - WTI", 2),
    }

    assert {
        asset: (spec.benchmark, spec.decimal_places) for asset, spec in SETTLEMENT_SPECS.items()
    } == expected


def test_coinbase_predictive_support_is_not_settlement_support() -> None:
    assert PREDICTIVE_SOURCE_SUPPORT[Asset.BTC] is SupportLevel.FULL
    assert PREDICTIVE_SOURCE_SUPPORT[Asset.BNB] is SupportLevel.UNSUPPORTED
    assert SETTLEMENT_SPECS[Asset.BTC].data_access is SupportLevel.PARTIAL


def test_support_matrix_never_conflates_discovery_with_overall_support() -> None:
    assert set(ASSET_SUPPORT) == set(Asset)
    assert all(profile.discovery is SupportLevel.FULL for profile in ASSET_SUPPORT.values())
    assert all(
        profile.displayed_quote is SupportLevel.PARTIAL for profile in ASSET_SUPPORT.values()
    )
    assert ASSET_SUPPORT[Asset.ETH].settlement_metadata is SupportLevel.PARTIAL
    assert all(
        profile.settlement_metadata is SupportLevel.FULL
        for asset, profile in ASSET_SUPPORT.items()
        if asset is not Asset.ETH
    )
    assert all(profile.overall is SupportLevel.PARTIAL for profile in ASSET_SUPPORT.values())
