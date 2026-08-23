"""Frozen-candidate, local-only forward paper/shadow validation.

This module deliberately has no network client and no execution provider other
than the local :class:`KalshiPaperExecutionProvider`.  It reads bounded,
as-of snapshots from the recorder SQLite database, writes a separate forward
ledger, and never touches the recorder's raw truth tables.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from live15_quant.config import Settings
from live15_quant.execution import ContractOutcome
from live15_quant.features import (
    COINBASE_PRODUCT_BY_ASSET,
    FeatureEngine,
    FeatureInputs,
    SamplingPolicy,
)
from live15_quant.model_zoo import (
    CertifiedDataset,
    DatasetExample,
    Preprocessor,
    load_certified_dataset,
    xgb,
)
from live15_quant.model_zoo_v2 import ModelZooV2Config, _AssetAwareXgb, _fit_spec, _predict
from live15_quant.models import (
    Asset,
    DataRole,
    ExecutabilityClassification,
    FreshnessState,
    MappingConfidence,
    OrderBookLevel,
    PredictionMarketQuote,
    SourceTimestampKind,
    Venue,
)
from live15_quant.paper import PaperDecisionType, StrategyDecision
from live15_quant.paper_execution import KalshiPaperExecutionProvider, PaperExecutionError
from live15_quant.paper_storage import PaperStore
from live15_quant.records import KalshiNativeQuoteRecord
from live15_quant.risk import HardRiskLimits, ImmutableHardRiskLayer
from live15_quant.storage import RecorderStore

FORWARD_VERSION = "1.0.0"
FORWARD_CANDIDATES = (
    ("logistic_l2_identity", Decimal("0.10")),
    ("xgboost_pooled_identity", Decimal("0.075")),
    ("xgboost_asset_identity", Decimal("0.075")),
)
PAPER_ELIGIBLE_ASSETS = frozenset(
    {Asset.BTC, Asset.ETH, Asset.XRP, Asset.SOL, Asset.DOGE, Asset.BNB, Asset.HYPE}
)
FORWARD_REQUIRED_LOOKBACK = timedelta(minutes=5)


class ForwardShadowError(RuntimeError):
    """A lineage, ledger, or bounded live-read invariant failed."""


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ForwardShadowError("forward timestamp must be UTC-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ForwardShadowError("forward timestamp is malformed")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ForwardShadowError("forward timestamp is malformed")
    return parsed.astimezone(UTC)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class ForwardCandidate:
    model_id: str
    threshold: Decimal


DEFAULT_FORWARD_CANDIDATES = tuple(
    ForwardCandidate(model_id=model_id, threshold=threshold)
    for model_id, threshold in FORWARD_CANDIDATES
)


class ForwardPredictionProvider(Protocol):
    """Stable prediction boundary for frozen v2 and future model artifacts."""

    artifact_hash: str

    def predict(self, model_id: str, row: DatasetExample) -> Decimal:
        """Return a calibrated probability for one immutable feature row."""


@dataclass(frozen=True, slots=True)
class LiveFeatureSnapshot:
    asset: Asset
    ticker: str
    event_ticker: str
    window_start: datetime
    window_end: datetime
    decision_timestamp: datetime
    values: tuple[Decimal | None, ...]
    missing_reasons: tuple[str | None, ...]
    quote: PredictionMarketQuote | None
    data_status: str
    data_reason: str | None

    @property
    def opportunity_id(self) -> str:
        return f"{self.ticker}:{_timestamp(self.decision_timestamp)}"

    @property
    def feature_hash(self) -> str:
        return _hash(
            {
                "ticker": self.ticker,
                "decision_timestamp": _timestamp(self.decision_timestamp),
                "values": [_decimal(value) for value in self.values],
                "missing_reasons": list(self.missing_reasons),
            }
        )


class ForwardShadowStore:
    """Append-only forward decision ledger, separate from raw and paper stores."""

    def __init__(
        self,
        path: Path,
        *,
        lineage: dict[str, str],
        candidate_ids: Iterable[str] | None = None,
    ) -> None:
        self.path = path
        self.candidate_ids = tuple(
            candidate_ids if candidate_ids is not None else (name for name, _ in FORWARD_CANDIDATES)
        )
        if not self.candidate_ids or len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ForwardShadowError("forward candidate ids must be unique and non-empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._initialize(lineage)
        except Exception:
            self._connection.close()
            raise

    def _initialize(self, lineage: dict[str, str]) -> None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forward_metadata'"
        ).fetchone()
        if exists is None:
            for marker in ("recorder_metadata", "paper_metadata", "feature_store_metadata"):
                collision = self._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (marker,)
                ).fetchone()
                if collision is not None:
                    raise ForwardShadowError(
                        "forward ledger cannot share raw, feature, or paper execution storage"
                    )
            self._connection.executescript(
                """
                CREATE TABLE forward_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT;
                CREATE TABLE forward_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    opportunity_id TEXT NOT NULL,
                    fact_hash TEXT NOT NULL,
                    decision_timestamp TEXT NOT NULL,
                    bucket_seconds INTEGER NOT NULL,
                    asset TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    feature_hash TEXT NOT NULL,
                    prediction TEXT,
                    yes_ask TEXT,
                    no_ask TEXT,
                    yes_edge TEXT,
                    no_edge TEXT,
                    action TEXT NOT NULL,
                    data_status TEXT NOT NULL,
                    data_reason TEXT,
                    risk_allowed INTEGER,
                    risk_reasons TEXT,
                    paper_order_id TEXT,
                    fill_state TEXT,
                    fill_reason TEXT,
                    filled_quantity TEXT,
                    average_fill_price TEXT,
                    fees TEXT,
                    model_artifact_hash TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(model_id,opportunity_id)
                ) STRICT;
                CREATE INDEX idx_forward_decisions_ticker
                ON forward_decisions(ticker,decision_timestamp,id);
                CREATE TABLE forward_settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL,
                    opportunity_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    outcome_yes INTEGER NOT NULL CHECK(outcome_yes IN (0,1)),
                    settlement_timestamp TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    UNIQUE(model_id,opportunity_id),
                    FOREIGN KEY(model_id,opportunity_id)
                    REFERENCES forward_decisions(model_id,opportunity_id)
                ) STRICT;
                """
            )
            metadata = {"schema_version": FORWARD_VERSION, **lineage}
            metadata["forward_validation_started_at"] = _timestamp(datetime.now(UTC))
            self._connection.executemany(
                "INSERT INTO forward_metadata(key,value) VALUES (?,?)", metadata.items()
            )
            self._connection.commit()
            return
        metadata = {
            str(row["key"]): str(row["value"])
            for row in self._connection.execute("SELECT key,value FROM forward_metadata")
        }
        if metadata.get("schema_version") != FORWARD_VERSION:
            raise ForwardShadowError("incompatible forward ledger schema")
        for key, value in lineage.items():
            if key == "mode" and key not in metadata:
                # The first local-only ledger format predated an explicit mode
                # marker.  Adding this immutable descriptive metadata is safe;
                # it never changes a decision fact or start boundary.
                try:
                    self._connection.execute("BEGIN IMMEDIATE")
                    self._connection.execute(
                        "INSERT INTO forward_metadata(key,value) VALUES (?,?)", (key, value)
                    )
                    self._connection.commit()
                except Exception:
                    self._connection.rollback()
                    raise
                metadata[key] = value
            if metadata.get(key) != value:
                raise ForwardShadowError("forward ledger lineage conflicts with frozen candidate")

    @property
    def started_at(self) -> datetime:
        row = self._connection.execute(
            "SELECT value FROM forward_metadata WHERE key='forward_validation_started_at'"
        ).fetchone()
        if row is None:
            raise ForwardShadowError("forward ledger start timestamp is missing")
        return _parse_timestamp(row["value"])

    def contains(self, model_id: str, opportunity_id: str) -> bool:
        """Return whether an immutable opportunity was already committed."""

        return (
            self._connection.execute(
                "SELECT 1 FROM forward_decisions WHERE model_id=? AND opportunity_id=? LIMIT 1",
                (model_id, opportunity_id),
            ).fetchone()
            is not None
        )

    def append(self, payload: dict[str, object]) -> bool:
        model_id = str(payload["model_id"])
        opportunity_id = str(payload["opportunity_id"])
        if _parse_timestamp(payload["decision_timestamp"]) < self.started_at:
            raise ForwardShadowError("forward decision predates validation start")
        immutable = {key: value for key, value in payload.items() if key != "created_at"}
        fact_hash = _hash(immutable)
        existing = self._connection.execute(
            "SELECT fact_hash,data_reason FROM forward_decisions "
            "WHERE model_id=? AND opportunity_id=?",
            (model_id, opportunity_id),
        ).fetchone()
        if existing is not None:
            if str(existing["fact_hash"]) != fact_hash:
                if (
                    existing["data_reason"] == "paper_decision_conflict"
                    and payload.get("data_reason") == "paper_decision_conflict"
                ):
                    return False
                raise ForwardShadowError(
                    "forward idempotency key conflicts with immutable decision fact"
                )
            return False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            concurrent = self._connection.execute(
                "SELECT fact_hash,data_reason FROM forward_decisions "
                "WHERE model_id=? AND opportunity_id=?",
                (model_id, opportunity_id),
            ).fetchone()
            if concurrent is not None:
                if str(concurrent["fact_hash"]) != fact_hash:
                    if (
                        concurrent["data_reason"] == "paper_decision_conflict"
                        and payload.get("data_reason") == "paper_decision_conflict"
                    ):
                        self._connection.rollback()
                        return False
                    raise ForwardShadowError(
                        "forward idempotency key conflicts with immutable decision fact"
                    )
                self._connection.rollback()
                return False
            self._connection.execute(
                """INSERT INTO forward_decisions(
                model_id,opportunity_id,fact_hash,decision_timestamp,bucket_seconds,asset,ticker,feature_hash,
                prediction,yes_ask,no_ask,yes_edge,no_edge,action,data_status,data_reason,
                risk_allowed,risk_reasons,paper_order_id,fill_state,fill_reason,filled_quantity,
                average_fill_price,fees,model_artifact_hash,dataset_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    model_id,
                    opportunity_id,
                    fact_hash,
                    payload["decision_timestamp"],
                    payload["bucket_seconds"],
                    payload["asset"],
                    payload["ticker"],
                    payload["feature_hash"],
                    payload.get("prediction"),
                    payload.get("yes_ask"),
                    payload.get("no_ask"),
                    payload.get("yes_edge"),
                    payload.get("no_edge"),
                    payload["action"],
                    payload["data_status"],
                    payload.get("data_reason"),
                    payload.get("risk_allowed"),
                    payload.get("risk_reasons"),
                    payload.get("paper_order_id"),
                    payload.get("fill_state"),
                    payload.get("fill_reason"),
                    payload.get("filled_quantity"),
                    payload.get("average_fill_price"),
                    payload.get("fees"),
                    payload["model_artifact_hash"],
                    payload["dataset_id"],
                    payload["created_at"],
                ),
            )
            self._connection.commit()
            return True
        except Exception:
            self._connection.rollback()
            raise

    def pending_tickers(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            """SELECT DISTINCT ticker FROM forward_decisions AS d
            WHERE d.paper_order_id IS NOT NULL AND NOT EXISTS(
              SELECT 1 FROM forward_settlements AS s
              WHERE s.model_id=d.model_id AND s.opportunity_id=d.opportunity_id
            ) ORDER BY ticker"""
        )
        return tuple(str(row["ticker"]) for row in rows)

    def decisions_for_ticker(self, ticker: str) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self._connection.execute(
                """SELECT * FROM forward_decisions AS d WHERE ticker=?
                AND paper_order_id IS NOT NULL AND NOT EXISTS(
                  SELECT 1 FROM forward_settlements AS s
                  WHERE s.model_id=d.model_id AND s.opportunity_id=d.opportunity_id
                ) ORDER BY id""",
                (ticker,),
            )
        )

    def append_settlement(
        self,
        *,
        model_id: str,
        opportunity_id: str,
        ticker: str,
        outcome_yes: bool,
        settlement_timestamp: datetime,
        realized_pnl: Decimal,
    ) -> bool:
        existing = self._connection.execute(
            "SELECT outcome_yes,settlement_timestamp,realized_pnl FROM forward_settlements "
            "WHERE model_id=? AND opportunity_id=?",
            (model_id, opportunity_id),
        ).fetchone()
        expected = (int(outcome_yes), _timestamp(settlement_timestamp), str(realized_pnl))
        if existing is not None:
            if tuple(existing) != expected:
                raise ForwardShadowError("forward settlement conflicts with official truth")
            return False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            concurrent = self._connection.execute(
                "SELECT outcome_yes,settlement_timestamp,realized_pnl FROM forward_settlements "
                "WHERE model_id=? AND opportunity_id=?",
                (model_id, opportunity_id),
            ).fetchone()
            if concurrent is not None:
                if tuple(concurrent) != expected:
                    raise ForwardShadowError("forward settlement conflicts with official truth")
                self._connection.rollback()
                return False
            self._connection.execute(
                """INSERT INTO forward_settlements(
                model_id,opportunity_id,ticker,outcome_yes,settlement_timestamp,realized_pnl
                ) VALUES (?,?,?,?,?,?)""",
                (model_id, opportunity_id, ticker, *expected),
            )
            self._connection.commit()
            return True
        except Exception:
            self._connection.rollback()
            raise

    def summary(self) -> dict[str, int]:
        return {
            "predictions": int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM forward_decisions WHERE prediction IS NOT NULL"
                ).fetchone()[0]
            ),
            "holds": int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM forward_decisions WHERE action='hold'"
                ).fetchone()[0]
            ),
            "signals": int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM forward_decisions WHERE action IN ('buy_yes','buy_no')"
                ).fetchone()[0]
            ),
            "settled": int(
                self._connection.execute("SELECT COUNT(*) FROM forward_settlements").fetchone()[0]
            ),
        }

    def metrics(self) -> dict[str, dict[str, object]]:
        """Return forward-only, per-model metrics from the immutable decision ledger.

        Pending decisions intentionally do not contribute to outcome metrics.  This
        avoids treating an unresolved Kalshi contract as either a loss or a zero.
        """

        rows = tuple(
            self._connection.execute(
                """SELECT d.model_id,d.asset,d.bucket_seconds,d.prediction,d.action,d.yes_edge,
                d.no_edge,d.fees,d.paper_order_id,d.fill_state,d.filled_quantity,s.outcome_yes,
                s.settlement_timestamp,s.realized_pnl FROM forward_decisions AS d
                LEFT JOIN forward_settlements AS s
                ON s.model_id=d.model_id AND s.opportunity_id=d.opportunity_id
                ORDER BY s.settlement_timestamp,d.id"""
            )
        )
        result: dict[str, dict[str, object]] = {}
        for model_id in self.candidate_ids:
            model_rows = [row for row in rows if row["model_id"] == model_id]
            result[model_id] = _forward_metric_summary(model_rows)
        return result

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> ForwardShadowStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _forward_metric_summary(
    rows: list[sqlite3.Row], *, include_breakdowns: bool = True
) -> dict[str, object]:
    """Compute outcome metrics only from immutable forward ledger facts."""

    predictions = [row for row in rows if row["prediction"] is not None]
    scored = [row for row in predictions if row["outcome_yes"] is not None]
    actionable = [row for row in rows if row["action"] in {"buy_yes", "buy_no"}]
    settled_trades = [row for row in actionable if row["outcome_yes"] is not None]
    probabilities = [float(row["prediction"]) for row in scored]
    outcomes = [int(row["outcome_yes"]) for row in scored]
    brier = (
        None
        if not scored
        else sum(
            (probability - outcome) ** 2
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        )
        / len(scored)
    )
    clipped = [min(max(value, 1e-15), 1 - 1e-15) for value in probabilities]
    logloss = (
        None
        if not scored
        else -sum(
            outcome * np.log(probability) + (1 - outcome) * np.log(1 - probability)
            for probability, outcome in zip(clipped, outcomes, strict=True)
        )
        / len(scored)
    )
    calibration = _expected_calibration_error(clipped, outcomes)
    accuracy = (
        None
        if not scored
        else sum(
            (probability >= 0.5) == bool(outcome)
            for probability, outcome in zip(probabilities, outcomes, strict=True)
        )
        / len(scored)
    )
    pnl = [Decimal(str(row["realized_pnl"])) for row in settled_trades]
    fees = [Decimal(str(row["fees"] or "0")) for row in settled_trades]
    gross = sum((value + fee for value, fee in zip(pnl, fees, strict=True)), Decimal(0))
    net = sum(pnl, Decimal(0))
    fee_total = sum(fees, Decimal(0))
    gains = sum((value for value in pnl if value > 0), Decimal(0))
    losses = -sum((value for value in pnl if value < 0), Decimal(0))
    peak = Decimal(0)
    running = Decimal(0)
    max_drawdown = Decimal(0)
    for value in pnl:
        running += value
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
    expected_edges: list[Decimal] = []
    for row in actionable:
        edge = row["yes_edge"] if row["action"] == "buy_yes" else row["no_edge"]
        if edge is None:
            raise ForwardShadowError("actionable forward decision lacks its immutable edge")
        expected_edges.append(Decimal(str(edge)))
    filled = [row for row in actionable if Decimal(str(row["filled_quantity"] or "0")) > 0]
    result: dict[str, object] = {
        "predictions": len(predictions),
        "holds": len([row for row in rows if row["action"] == "hold"]),
        "actionable_signals": len(actionable),
        "submitted_paper_orders": len(
            [row for row in actionable if row["paper_order_id"] is not None]
        ),
        "full_fills": len([row for row in actionable if row["fill_state"] == "filled"]),
        "partial_fills": len(
            [row for row in actionable if row["fill_state"] == "partially_filled"]
        ),
        "no_fills": len(
            [
                row
                for row in actionable
                if row["fill_state"] in {"not_submitted", "cancelled", "rejected"}
                or Decimal(str(row["filled_quantity"] or "0")) == 0
            ]
        ),
        "open_positions": len(
            [
                row
                for row in actionable
                if row["outcome_yes"] is None and Decimal(str(row["filled_quantity"] or "0")) > 0
            ]
        ),
        "settled_trades": len(settled_trades),
        "wins": len([value for value in pnl if value > 0]),
        "losses": len([value for value in pnl if value < 0]),
        "brier": brier,
        "logloss": logloss,
        "calibration_error": calibration,
        "accuracy_diagnostic": accuracy,
        "gross_pnl": str(gross),
        "fees": str(fee_total),
        "net_pnl": str(net),
        "max_drawdown": str(max_drawdown),
        "profit_factor": None if losses == 0 else float(gains / losses),
        "average_predicted_edge": (
            None
            if not expected_edges
            else str(sum(expected_edges, Decimal(0)) / len(expected_edges))
        ),
        "realized_edge": (
            None
            if not filled
            else str(
                net / sum((Decimal(str(row["filled_quantity"])) for row in filled), Decimal(0))
            )
        ),
        "assets": sorted({str(row["asset"]) for row in rows}),
        "decision_buckets": sorted({int(row["bucket_seconds"]) for row in rows}),
    }
    if include_breakdowns:
        result["by_decision_bucket"] = {
            str(bucket): _forward_metric_summary(
                [row for row in rows if int(row["bucket_seconds"]) == bucket],
                include_breakdowns=False,
            )
            for bucket in sorted({int(row["bucket_seconds"]) for row in rows})
        }
        result["by_asset"] = {
            asset: _forward_metric_summary(
                [row for row in rows if row["asset"] == asset], include_breakdowns=False
            )
            for asset in sorted({str(row["asset"]) for row in rows})
        }
    return result


def _expected_calibration_error(probabilities: list[float], outcomes: list[int]) -> float | None:
    if not probabilities:
        return None
    weighted_error = 0.0
    total = len(probabilities)
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        values = [
            (probability, outcome)
            for probability, outcome in zip(probabilities, outcomes, strict=True)
            if lower <= probability < upper or (index == 9 and probability == 1)
        ]
        if values:
            weighted_error += (
                len(values)
                / total
                * abs(
                    sum(probability for probability, _outcome in values) / len(values)
                    - sum(outcome for _probability, outcome in values) / len(values)
                )
            )
    return weighted_error


class FrozenForwardModels:
    """Immutable, Train-only materialization of a configured candidate set."""

    def __init__(
        self,
        root: Path,
        dataset: CertifiedDataset,
        v2_path: Path,
        candidates: tuple[ForwardCandidate, ...] = DEFAULT_FORWARD_CANDIDATES,
    ) -> None:
        self.root = root
        self.dataset = dataset
        self.v2_path = v2_path
        self.candidates = candidates
        self.candidate_ids = tuple(candidate.model_id for candidate in candidates)
        if not self.candidate_ids or len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ForwardShadowError("forward candidate ids must be unique and non-empty")
        self._models: dict[str, object] = {}
        self.artifact_hash = ""

    def load_or_materialize(self, *, allow_materialize: bool) -> None:
        manifest = _read_json(self.v2_path / "manifest.json")
        _verify_v2(manifest, self.dataset)
        identity = _hash(
            {
                "v2_id": manifest["zoo_id"],
                "v2_hash": manifest["deterministic_build_hash"],
                "dataset_id": self.dataset.dataset_id,
                "dataset_hash": self.dataset.deterministic_build_hash,
                "candidate_ids": list(self.candidate_ids),
            }
        )
        path = self.root / f"forward-models-{identity[:20]}"
        if path.is_dir():
            self._load(path, identity)
            return
        if not allow_materialize:
            raise ForwardShadowError("frozen forward-model artifact is not materialized")
        self._materialize(path, identity, manifest)
        self._load(path, identity)

    def _materialize(self, path: Path, identity: str, v2_manifest: dict[str, object]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".forward-model-stage-", dir=self.root))
        try:
            config_payload = v2_manifest.get("config")
            if not isinstance(config_payload, dict):
                raise ForwardShadowError("Model Zoo v2 config is malformed")
            config = ModelZooV2Config()
            if config.payload() != config_payload:
                raise ForwardShadowError("Model Zoo v2 frozen configuration is not recognized")
            rows = tuple(
                row for row in self.dataset.splits["train"] if row.asset in self.dataset.oos_assets
            )
            assets = tuple(sorted(self.dataset.oos_assets))
            payload: dict[str, object] = {
                "format": "live15-forward-models-v1",
                "identity": identity,
                "dataset_id": self.dataset.dataset_id,
                "dataset_hash": self.dataset.deterministic_build_hash,
                "v2_id": v2_manifest["zoo_id"],
                "v2_hash": v2_manifest["deterministic_build_hash"],
                "train_only": True,
                "final_test_used": False,
                "models": {},
            }
            models = payload["models"]
            assert isinstance(models, dict)
            for candidate in self.candidates:
                name = candidate.model_id
                family = {
                    "logistic_l2_identity": "logistic_l2",
                    "xgboost_pooled_identity": "xgboost",
                    "xgboost_asset_identity": "xgboost_asset_identity",
                }.get(name)
                if family is None:
                    raise ForwardShadowError(
                        "candidate requires an injected prediction provider for materialization"
                    )
                model = _fit_spec(family, rows, self.dataset.feature_names, assets, config)
                metadata, files = _serialize_model(name, model)
                models[name] = metadata
                for filename, data in files.items():
                    (staging / filename).write_bytes(data)
            encoded = _canonical(payload).encode("utf-8")
            (staging / "manifest.json").write_bytes(encoded)
            if path.exists():
                raise ForwardShadowError("forward model immutable target already exists")
            os.replace(staging, path)
        except Exception:
            for child in staging.glob("*"):
                child.unlink(missing_ok=True)
            staging.rmdir()
            raise

    def _load(self, path: Path, identity: str) -> None:
        manifest = _read_json(path / "manifest.json")
        if (
            manifest.get("format") != "live15-forward-models-v1"
            or manifest.get("identity") != identity
        ):
            raise ForwardShadowError(
                "frozen forward-model manifest conflicts with required lineage"
            )
        if manifest.get("final_test_used") is not False or manifest.get("train_only") is not True:
            raise ForwardShadowError("forward model artifact has invalid final-test lineage")
        models = manifest.get("models")
        if not isinstance(models, dict):
            raise ForwardShadowError("frozen forward-model manifest is malformed")
        self._models = {
            name: _deserialize_model(path, name, metadata)
            for name, metadata in models.items()
            if name in set(self.candidate_ids)
        }
        if set(self._models) != set(self.candidate_ids):
            raise ForwardShadowError("frozen forward-model artifact lacks a candidate")
        self.artifact_hash = _hash(manifest)

    def predict(self, model_id: str, row: DatasetExample) -> Decimal:
        model = self._models.get(model_id)
        if model is None:
            raise ForwardShadowError("unknown frozen forward candidate")
        value = float(_predict(model, (row,))[0])
        if not 0 < value < 1:
            raise ForwardShadowError("frozen candidate returned invalid probability")
        return Decimal(str(value))


def _serialize_model(name: str, model: object) -> tuple[dict[str, object], dict[str, bytes]]:
    if hasattr(model, "family") and model.family == "logistic_l2":
        preprocessor = model.preprocessor
        weights, intercept = model.model
        return (
            {
                "family": "logistic_l2",
                "preprocessor": preprocessor.payload(),
                "weights": [float(item) for item in weights],
                "intercept": float(intercept),
            },
            {},
        )
    if hasattr(model, "family") and model.family == "xgboost":
        booster = model.model
        preprocessor = model.preprocessor
        return _serialize_booster(name, "xgboost", booster, preprocessor.payload(), ())
    if isinstance(model, _AssetAwareXgb):
        return _serialize_booster(
            name,
            "xgboost_asset_identity",
            model.booster,
            model.preprocessor.payload(),
            model.assets,
        )
    raise ForwardShadowError("unsupported frozen forward model")


def _serialize_booster(
    name: str, family: str, booster: Any, preprocessor: dict[str, object], assets: tuple[str, ...]
) -> tuple[dict[str, object], dict[str, bytes]]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        booster.save_model(temporary)
        data = temporary.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
    filename = f"{name}.json"
    return (
        {
            "family": family,
            "preprocessor": preprocessor,
            "assets": list(assets),
            "file": filename,
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        {filename: data},
    )


def _preprocessor(payload: object) -> Preprocessor:
    if not isinstance(payload, dict):
        raise ForwardShadowError("forward model preprocessor is malformed")
    try:
        return Preprocessor(
            tuple(str(item) for item in payload["feature_names"]),
            tuple(float(item) for item in payload["medians"]),
            tuple(float(item) for item in payload["means"]),
            tuple(float(item) for item in payload["scales"]),
            tuple(bool(item) for item in payload["entirely_missing_in_train"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ForwardShadowError("forward model preprocessor is malformed") from error


def _deserialize_model(path: Path, name: str, payload: object) -> object:
    if not isinstance(payload, dict) or not isinstance(payload.get("family"), str):
        raise ForwardShadowError("forward model metadata is malformed")
    family = payload["family"]
    preprocessor = _preprocessor(payload.get("preprocessor"))
    if family == "logistic_l2":
        from live15_quant.model_zoo import FittedModel

        return FittedModel(
            name=name,
            family="logistic_l2",
            feature_names=preprocessor.feature_names,
            preprocessor=preprocessor,
            model=(np.asarray(payload["weights"], dtype=np.float64), float(payload["intercept"])),
            market_probability_index=None,
        )
    filename = payload.get("file")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise ForwardShadowError("forward model file path is invalid")
    data = (path / filename).read_bytes()
    if hashlib.sha256(data).hexdigest() != payload.get("sha256"):
        raise ForwardShadowError("forward model file hash mismatch")
    booster = xgb.Booster()
    booster.load_model(bytearray(data))
    if family == "xgboost":
        from live15_quant.model_zoo import FittedModel

        return FittedModel(
            name=name,
            family="xgboost",
            feature_names=preprocessor.feature_names,
            preprocessor=preprocessor,
            model=booster,
            market_probability_index=None,
        )
    if family == "xgboost_asset_identity":
        assets = payload.get("assets")
        if not isinstance(assets, list) or not all(isinstance(item, str) for item in assets):
            raise ForwardShadowError("asset-aware forward model metadata is malformed")
        return _AssetAwareXgb(preprocessor, booster, tuple(assets))
    raise ForwardShadowError("unknown frozen forward model family")


class LiveForwardReader:
    """Bounded as-of reader.  It never opens recorder SQLite for writes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def due_snapshots(self, now: datetime, started_at: datetime) -> tuple[LiveFeatureSnapshot, ...]:
        connection = sqlite3.connect(
            f"file:{self.settings.recorder_data_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=2,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            health = self._health()
            synchronized = health.get("kalshi_ws_synchronized_markets")
            if not isinstance(synchronized, dict):
                return ()
            snapshots: list[LiveFeatureSnapshot] = []
            for asset in Asset:
                ticker = synchronized.get(asset.value)
                if not isinstance(ticker, str):
                    continue
                market = connection.execute(
                    """SELECT * FROM kalshi_market_lifecycle WHERE ticker=?
                    AND window_start<=? AND window_end>? AND lifecycle='open'
                    AND fetched_timestamp<=?
                    ORDER BY fetched_timestamp DESC,id DESC LIMIT 1""",
                    (ticker, _timestamp(now), _timestamp(now), _timestamp(now)),
                ).fetchone()
                if market is None:
                    continue
                window_end = _parse_timestamp(market["window_end"])
                for offset in self.settings.dataset_decision_offsets_seconds:
                    decision = window_end - timedelta(seconds=offset)
                    if not (started_at <= decision <= now):
                        continue
                    if (
                        now - decision
                    ).total_seconds() > self.settings.forward_shadow_decision_grace_seconds:
                        continue
                    snapshots.append(self._snapshot(connection, health, market, decision))
            return tuple(snapshots)
        finally:
            connection.close()

    def probe_current(self, now: datetime) -> tuple[LiveFeatureSnapshot, ...]:
        """Read-only operational probe; outputs are not forward-performance decisions."""

        connection = sqlite3.connect(
            f"file:{self.settings.recorder_data_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=2,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            health = self._health()
            synchronized = health.get("kalshi_ws_synchronized_markets")
            if not isinstance(synchronized, dict):
                return ()
            snapshots = []
            for asset in Asset:
                ticker = synchronized.get(asset.value)
                if not isinstance(ticker, str):
                    continue
                market = connection.execute(
                    """SELECT * FROM kalshi_market_lifecycle WHERE ticker=?
                    AND window_start<=? AND window_end>? AND lifecycle='open'
                    AND fetched_timestamp<=?
                    ORDER BY fetched_timestamp DESC,id DESC LIMIT 1""",
                    (ticker, _timestamp(now), _timestamp(now), _timestamp(now)),
                ).fetchone()
                if market is not None:
                    snapshots.append(self._snapshot(connection, health, market, now))
            return tuple(snapshots)
        finally:
            connection.close()

    def _health(self) -> dict[str, object]:
        try:
            payload = json.loads(self.settings.recorder_health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ForwardShadowError("recorder health is unavailable") from error
        if not isinstance(payload, dict):
            raise ForwardShadowError("recorder health is malformed")
        return payload

    def _snapshot(
        self,
        connection: sqlite3.Connection,
        health: dict[str, object],
        market: sqlite3.Row,
        decision: datetime,
    ) -> LiveFeatureSnapshot:
        asset = Asset(str(market["asset"]))
        ticker = str(market["ticker"])
        event_ticker = str(market["event_ticker"])
        window_start = _parse_timestamp(market["window_start"])
        window_end = _parse_timestamp(market["window_end"])
        if str(health.get("kalshi_ws_connection_state")) != "synchronized":
            return self._unavailable(
                asset,
                ticker,
                event_ticker,
                window_start,
                window_end,
                decision,
                "ws_not_synchronized",
            )
        synchronized = health.get("kalshi_ws_synchronized_markets")
        if not isinstance(synchronized, dict) or synchronized.get(asset.value) != ticker:
            return self._unavailable(
                asset,
                ticker,
                event_ticker,
                window_start,
                window_end,
                decision,
                "market_ws_not_synchronized",
            )
        # The recorder's generic health heartbeat is intentionally lower-rate than
        # WebSocket data.  It proves process progress, not a book observation, so
        # it must never be used to invent or reject book freshness.  The exact
        # as-of checkpoint below is the authoritative application-data gate.
        if _has_required_gap(connection, asset, decision):
            return self._unavailable(
                asset, ticker, event_ticker, window_start, window_end, decision, "data_gap_overlap"
            )
        checkpoint = connection.execute(
            """SELECT * FROM kalshi_ws_book_checkpoints WHERE ticker=? AND received_timestamp<=?
            ORDER BY received_timestamp DESC,id DESC LIMIT 1""",
            (ticker, _timestamp(decision)),
        ).fetchone()
        if checkpoint is None:
            return self._unavailable(
                asset,
                ticker,
                event_ticker,
                window_start,
                window_end,
                decision,
                "ws_snapshot_missing",
            )
        checkpoint_received = _parse_timestamp(checkpoint["received_timestamp"])
        if (
            decision - checkpoint_received
        ).total_seconds() > self.settings.kalshi_websocket_stale_seconds:
            return self._unavailable(
                asset, ticker, event_ticker, window_start, window_end, decision, "ws_book_stale"
            )
        ws_quote = _checkpoint_quote(market, checkpoint)
        quotes = list(self._native_quotes(connection, ticker, decision))
        quotes.append(ws_quote)
        product = COINBASE_PRODUCT_BY_ASSET.get(asset)
        ticks = (
            ()
            if product is None
            else tuple(
                RecorderStore._tick_record(row)
                for row in connection.execute(
                    """SELECT * FROM coinbase_ticks WHERE product=? AND received_timestamp<=?
                AND received_timestamp>=? ORDER BY received_timestamp ASC,id ASC LIMIT 10000""",
                    (product, _timestamp(decision), _timestamp(decision - timedelta(minutes=6))),
                )
            )
        )
        underlying = (
            ()
            if product is not None
            else tuple(
                RecorderStore._underlying_record(row)
                for row in connection.execute(
                    """SELECT * FROM underlying_observations
                    WHERE asset=? AND provider='pyth_hermes' AND received_timestamp<=?
                    AND source_timestamp<=? AND received_timestamp>=?
                    ORDER BY received_timestamp ASC,id ASC LIMIT 10000""",
                    (
                        asset.value,
                        _timestamp(decision),
                        _timestamp(decision),
                        _timestamp(decision - timedelta(minutes=6)),
                    ),
                )
            )
        )
        try:
            vector = FeatureEngine(
                SamplingPolicy(
                    tuple(
                        timedelta(seconds=value)
                        for value in self.settings.dataset_decision_offsets_seconds
                    ),
                    quote_max_age=timedelta(seconds=self.settings.dataset_quote_max_age_seconds),
                    underlying_max_age=timedelta(
                        seconds=self.settings.dataset_underlying_max_age_seconds
                    ),
                )
            ).compute(
                FeatureInputs(
                    market=RecorderStore._kalshi_feature_market_record(market),
                    quotes=tuple(quotes),
                    ticks=ticks,
                    underlying=underlying,
                    decision_timestamp=decision,
                )
            )
        except (ValueError, sqlite3.Error) as error:
            return self._unavailable(
                asset,
                ticker,
                event_ticker,
                window_start,
                window_end,
                decision,
                f"feature_error:{type(error).__name__}",
            )
        values = tuple(item.value for item in vector.observations)
        reasons = tuple(
            None if item.missing_reason is None else item.missing_reason.value
            for item in vector.observations
        )
        required = {"underlying_price", "yes_ask", "no_ask", "market_probability_midpoint"}
        missing = [
            item.name
            for item in vector.observations
            if item.name in required and item.missing_reason is not None
        ]
        if missing:
            return LiveFeatureSnapshot(
                asset,
                ticker,
                event_ticker,
                window_start,
                window_end,
                decision,
                values,
                reasons,
                None,
                "data_unavailable",
                "required_feature_missing:" + ",".join(missing),
            )
        return LiveFeatureSnapshot(
            asset,
            ticker,
            event_ticker,
            window_start,
            window_end,
            decision,
            values,
            reasons,
            _prediction_quote(market, checkpoint),
            "ready",
            None,
        )

    @staticmethod
    def _native_quotes(
        connection: sqlite3.Connection, ticker: str, decision: datetime
    ) -> Iterable[KalshiNativeQuoteRecord]:
        rows = connection.execute(
            """SELECT * FROM kalshi_prediction_quotes WHERE ticker=? AND received_timestamp<=?
            AND received_timestamp>=? ORDER BY received_timestamp ASC,id ASC LIMIT 10000""",
            (ticker, _timestamp(decision), _timestamp(decision - timedelta(minutes=6))),
        )
        return tuple(RecorderStore._kalshi_native_quote_record(row) for row in rows)

    @staticmethod
    def _unavailable(
        asset: Asset,
        ticker: str,
        event_ticker: str,
        start: datetime,
        end: datetime,
        decision: datetime,
        reason: str,
    ) -> LiveFeatureSnapshot:
        return LiveFeatureSnapshot(
            asset,
            ticker,
            event_ticker,
            start,
            end,
            decision,
            (),
            (),
            None,
            "data_unavailable",
            reason,
        )


def _has_required_gap(connection: sqlite3.Connection, asset: Asset, decision: datetime) -> bool:
    """Return whether a required source gap overlaps the exact feature lookback."""

    required_since = decision - FORWARD_REQUIRED_LOOKBACK
    required_sources = (
        "kalshi_ws",
        "coinbase" if asset in COINBASE_PRODUCT_BY_ASSET else "pyth",
    )
    # `data_gaps` is append-only: an OPEN fact is never rewritten when its
    # RECOVERED counterpart arrives.  Project it as of the decision rather than
    # interpreting old OPEN facts as permanently active.  A recovered interval
    # still blocks if it overlaps the real feature lookback.
    return (
        connection.execute(
            """SELECT 1 FROM data_gaps AS open
            WHERE open.asset=? AND open.source IN (?,?) AND open.recovered=0
              AND open.gap_start<? AND NOT EXISTS(
                SELECT 1 FROM data_gaps AS closed
                WHERE closed.source=open.source AND closed.asset=open.asset
                  AND closed.instrument=open.instrument AND closed.gap_start=open.gap_start
                  AND closed.recovered=1 AND closed.gap_end<=?
              ) LIMIT 1""",
            (
                asset.value,
                *required_sources,
                _timestamp(decision),
                _timestamp(required_since),
            ),
        ).fetchone()
        is not None
    )


def _checkpoint_quote(market: sqlite3.Row, checkpoint: sqlite3.Row) -> KalshiNativeQuoteRecord:
    yes = tuple(
        OrderBookLevel(Decimal(price), Decimal(quantity))
        for price, quantity in json.loads(checkpoint["yes_bids"])
    )
    no = tuple(
        OrderBookLevel(Decimal(price), Decimal(quantity))
        for price, quantity in json.loads(checkpoint["no_bids"])
    )
    yes_bid = max((item.price for item in yes), default=None)
    no_bid = max((item.price for item in no), default=None)
    return KalshiNativeQuoteRecord(
        row_id=-int(checkpoint["id"]),
        schema_version=int(checkpoint["schema_version"]),
        asset=Asset(market["asset"]),
        series=str(market["series"]),
        ticker=str(market["ticker"]),
        event_ticker=str(market["event_ticker"]),
        source_timestamp=None
        if checkpoint["source_timestamp"] is None
        else _parse_timestamp(checkpoint["source_timestamp"]),
        source_timestamp_kind=SourceTimestampKind.EXCHANGE_EVENT_TIME
        if checkpoint["source_timestamp"] is not None
        else SourceTimestampKind.UNAVAILABLE,
        received_timestamp=_parse_timestamp(checkpoint["received_timestamp"]),
        yes_bid=yes_bid,
        yes_ask=None if no_bid is None else Decimal(1) - no_bid,
        no_bid=no_bid,
        no_ask=None if yes_bid is None else Decimal(1) - yes_bid,
        last_trade=None,
        volume=None,
        yes_bid_depth=yes,
        no_bid_depth=no,
        source="kalshi_ws",
        freshness=FreshnessState.FRESH,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=(),
        role=DataRole.CONTRACT_MARKET_QUOTE,
    )


def _prediction_quote(market: sqlite3.Row, checkpoint: sqlite3.Row) -> PredictionMarketQuote:
    native = _checkpoint_quote(market, checkpoint)
    return PredictionMarketQuote(
        asset=native.asset,
        robinhood_event_id=native.ticker,
        robinhood_contract_id=native.ticker,
        venue=Venue.KALSHI,
        venue_series=native.series,
        venue_ticker=native.ticker,
        mapping_confidence=MappingConfidence.VERIFIED,
        source_timestamp=native.source_timestamp,
        source_timestamp_kind=native.source_timestamp_kind,
        received_timestamp=native.received_timestamp,
        yes_bid=native.yes_bid,
        yes_ask=native.yes_ask,
        no_bid=native.no_bid,
        no_ask=native.no_ask,
        last_trade=None,
        volume=None,
        yes_bid_depth=native.yes_bid_depth,
        no_bid_depth=native.no_bid_depth,
        source="kalshi_ws",
        freshness=FreshnessState.FRESH,
        executability=ExecutabilityClassification.OFFICIAL_VENUE_ORDER_BOOK,
        evidence_urls=(),
    )


class ForwardShadowRuntime:
    """One prediction per due event/bucket/model, with isolated portfolios.

    The default provider is the immutable v2 loader.  A future model artifact can
    inject any implementation of :class:`ForwardPredictionProvider` without
    changing the ledger, paper execution, settlement, or reconciliation paths.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        allow_model_materialization: bool = False,
        candidates: tuple[ForwardCandidate, ...] = DEFAULT_FORWARD_CANDIDATES,
        prediction_provider: ForwardPredictionProvider | None = None,
    ) -> None:
        self.settings = settings
        self.candidates = candidates
        self.candidate_ids = tuple(candidate.model_id for candidate in candidates)
        if not self.candidate_ids or len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ForwardShadowError("forward candidate ids must be unique and non-empty")
        self.dataset = load_certified_dataset(settings.forward_shadow_dataset_path)
        if prediction_provider is None:
            self.models: ForwardPredictionProvider = FrozenForwardModels(
                settings.forward_shadow_model_root,
                self.dataset,
                settings.forward_shadow_model_zoo_v2_path,
                candidates,
            )
            self.models.load_or_materialize(allow_materialize=allow_model_materialization)
            model_lineage = settings.forward_shadow_model_zoo_v2_path.name
        else:
            self.models = prediction_provider
            model_lineage = "injected-prediction-provider"
        lineage = {
            "dataset_id": self.dataset.dataset_id,
            "model_artifact_hash": self.models.artifact_hash,
            # Keep the historical metadata key stable so a managed restart can
            # reopen the existing forward ledger without a lineage migration.
            "model_zoo_v2": model_lineage,
            "mode": "PAPER_SHADOW_LOCAL_ONLY",
        }
        self.store = ForwardShadowStore(
            settings.forward_shadow_data_path,
            lineage=lineage,
            candidate_ids=self.candidate_ids,
        )
        self.reader = LiveForwardReader(settings)
        self.executions: dict[str, KalshiPaperExecutionProvider] = {}
        self.paper_stores: list[PaperStore] = []
        for model_id in self.candidate_ids:
            paper = PaperStore(
                settings.forward_shadow_paper_root / f"{model_id}.sqlite3",
                account_id=f"paper_{model_id}",
                starting_cash=settings.forward_shadow_starting_cash,
            )
            self.paper_stores.append(paper)
            self.executions[model_id] = KalshiPaperExecutionProvider(
                store=paper,
                account_id=f"paper_{model_id}",
                starting_cash=settings.forward_shadow_starting_cash,
                risk=ImmutableHardRiskLayer(
                    HardRiskLimits(
                        settings.paper_max_order_notional,
                        settings.paper_max_event_exposure,
                        settings.paper_max_daily_loss,
                        settings.paper_max_total_exposure,
                        settings.paper_max_consecutive_losses,
                    )
                ),
                kill_switch=settings.paper_kill_switch,
            )

    def run_once(self, now: datetime | None = None) -> dict[str, int]:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        self._settle(observed)
        for snapshot in self.reader.due_snapshots(observed, self.store.started_at):
            self._process(snapshot)
        return self.store.summary()

    def probe(self, now: datetime | None = None) -> dict[str, int]:
        observed = (now or datetime.now(UTC)).astimezone(UTC)
        snapshots = self.reader.probe_current(observed)
        ready = sum(item.data_status == "ready" for item in snapshots)
        predictions = 0
        for snapshot in snapshots:
            if snapshot.data_status != "ready":
                continue
            row = self._example(snapshot)
            for model_id in self.candidate_ids:
                self.models.predict(model_id, row)
                predictions += 1
        return {
            "assets_seen": len(snapshots),
            "ready_assets": ready,
            "probe_predictions": predictions,
        }

    def _process(self, snapshot: LiveFeatureSnapshot) -> None:
        for candidate in self.candidates:
            model_id, threshold = candidate.model_id, candidate.threshold
            if self.store.contains(model_id, snapshot.opportunity_id):
                continue
            try:
                payload = self._payload(model_id, threshold, snapshot)
            except PaperExecutionError as error:
                # A crash can leave the isolated PaperStore committed while the
                # forward ledger transaction is still pending.  Recomputing the
                # same opportunity after restart may produce a different action
                # because the live quote moved; never submit a second intent or
                # kill the shadow worker.  Quarantine the opportunity instead.
                if "immutable intent" not in str(error):
                    raise
                payload = self._paper_conflict_payload(model_id, snapshot)
            if self.store.append(payload):
                continue

    def _paper_conflict_payload(
        self, model_id: str, snapshot: LiveFeatureSnapshot
    ) -> dict[str, object]:
        """Persist a restart conflict as DATA_UNAVAILABLE, never as a new order."""

        return {
            "model_id": model_id,
            "opportunity_id": snapshot.opportunity_id,
            "decision_timestamp": _timestamp(snapshot.decision_timestamp),
            "bucket_seconds": int(
                (snapshot.window_end - snapshot.decision_timestamp).total_seconds()
            ),
            "asset": snapshot.asset.value,
            "ticker": snapshot.ticker,
            "feature_hash": snapshot.feature_hash,
            "action": "hold",
            "data_status": "data_unavailable",
            "data_reason": "paper_decision_conflict",
            "risk_allowed": None,
            "risk_reasons": '["paper_decision_conflict"]',
            "model_artifact_hash": self.models.artifact_hash,
            "dataset_id": self.dataset.dataset_id,
            "created_at": _timestamp(datetime.now(UTC)),
        }

    def _payload(
        self, model_id: str, threshold: Decimal, snapshot: LiveFeatureSnapshot
    ) -> dict[str, object]:
        base: dict[str, object] = {
            "model_id": model_id,
            "opportunity_id": snapshot.opportunity_id,
            "decision_timestamp": _timestamp(snapshot.decision_timestamp),
            "bucket_seconds": int(
                (snapshot.window_end - snapshot.decision_timestamp).total_seconds()
            ),
            "asset": snapshot.asset.value,
            "ticker": snapshot.ticker,
            "feature_hash": snapshot.feature_hash,
            "data_status": snapshot.data_status,
            "data_reason": snapshot.data_reason,
            "model_artifact_hash": self.models.artifact_hash,
            "dataset_id": self.dataset.dataset_id,
            "created_at": _timestamp(datetime.now(UTC)),
        }
        if snapshot.data_status != "ready" or snapshot.quote is None:
            decision = StrategyDecision(
                decision_id=_decision_id(model_id, snapshot.opportunity_id),
                signal_timestamp=snapshot.decision_timestamp,
                asset=snapshot.asset,
                event_id=snapshot.ticker,
                contract_id=snapshot.ticker,
                decision=PaperDecisionType.HOLD,
                outcome=None,
                quantity=Decimal(0),
                limit_price=None,
            )
            result = self.executions[model_id].execute(decision, None)
            return {
                **base,
                "action": "hold",
                "risk_allowed": None,
                "risk_reasons": '["data_unavailable"]',
                "paper_order_id": result.order_id,
                "fill_state": result.state.value,
                "fill_reason": result.reason.value,
                "filled_quantity": str(result.filled_quantity),
                "fees": str(result.fees),
            }
        probability = self.models.predict(model_id, self._example(snapshot))
        quote = snapshot.quote
        assert quote.yes_ask is not None and quote.no_ask is not None
        yes_edge, no_edge = probability - quote.yes_ask, Decimal(1) - probability - quote.no_ask
        existing_position = self.executions[model_id].get_position(snapshot.ticker, snapshot.ticker)
        if snapshot.asset not in PAPER_ELIGIBLE_ASSETS:
            action, outcome, limit = PaperDecisionType.HOLD, None, None
        elif existing_position is not None:
            # A model gets at most one paper position per contract.  This keeps
            # settlement attribution unambiguous and prevents a rapid sequence of
            # decision buckets from silently pyramiding risk.
            action, outcome, limit = PaperDecisionType.HOLD, None, None
        elif max(yes_edge, no_edge) < threshold:
            action, outcome, limit = PaperDecisionType.HOLD, None, None
        elif yes_edge >= no_edge:
            action, outcome, limit = PaperDecisionType.BUY_YES, ContractOutcome.YES, quote.yes_ask
        else:
            action, outcome, limit = PaperDecisionType.BUY_NO, ContractOutcome.NO, quote.no_ask
        decision = StrategyDecision(
            decision_id=_decision_id(model_id, snapshot.opportunity_id),
            signal_timestamp=snapshot.decision_timestamp,
            asset=snapshot.asset,
            event_id=snapshot.ticker,
            contract_id=snapshot.ticker,
            decision=action,
            outcome=outcome,
            quantity=Decimal(0)
            if action is PaperDecisionType.HOLD
            else self.settings.forward_shadow_order_quantity,
            limit_price=limit,
        )
        result = self.executions[model_id].execute(
            decision, quote if action is not PaperDecisionType.HOLD else None
        )
        return {
            **base,
            "prediction": str(probability),
            "yes_ask": str(quote.yes_ask),
            "no_ask": str(quote.no_ask),
            "yes_edge": str(yes_edge),
            "no_edge": str(no_edge),
            "action": action.value,
            "risk_allowed": None if result.risk is None else int(result.risk.allowed),
            "risk_reasons": (
                _canonical(["asset_oos_not_eligible"])
                if snapshot.asset not in PAPER_ELIGIBLE_ASSETS
                else (
                    _canonical(["position_already_open"])
                    if existing_position is not None
                    else (
                        None
                        if result.risk is None
                        else _canonical([reason.value for reason in result.risk.reasons])
                    )
                )
            ),
            "paper_order_id": result.order_id,
            "fill_state": result.state.value,
            "fill_reason": result.reason.value,
            "filled_quantity": str(result.filled_quantity),
            "average_fill_price": _decimal(result.average_fill_price),
            "fees": str(result.fees),
        }

    def _example(self, snapshot: LiveFeatureSnapshot) -> DatasetExample:
        return DatasetExample(
            snapshot.asset.value,
            snapshot.ticker,
            snapshot.decision_timestamp,
            snapshot.window_start,
            snapshot.window_end,
            int((snapshot.window_end - snapshot.decision_timestamp).total_seconds()),
            0,
            snapshot.values,
            snapshot.missing_reasons,
        )

    def _settle(self, now: datetime) -> None:
        if not self.store.pending_tickers():
            return
        connection = sqlite3.connect(
            f"file:{self.settings.recorder_data_path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=2,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            for ticker in self.store.pending_tickers():
                truth = connection.execute(
                    "SELECT * FROM kalshi_settlements WHERE ticker=? ORDER BY id DESC LIMIT 1",
                    (ticker,),
                ).fetchone()
                if truth is None:
                    continue
                outcome_yes = str(truth["result"]) == "yes"
                timestamp = _parse_timestamp(truth["settlement_timestamp"])
                decisions_by_model: dict[str, list[sqlite3.Row]] = {}
                for decision in self.store.decisions_for_ticker(ticker):
                    decisions_by_model.setdefault(str(decision["model_id"]), []).append(decision)
                for model_id, decisions in decisions_by_model.items():
                    if len(decisions) != 1:
                        raise ForwardShadowError(
                            "multiple unsettled paper orders violate the one-model/event "
                            "forward policy"
                        )
                    decision = decisions[0]
                    inserted = self.executions[model_id].settle_event(
                        event_id=ticker, outcome_yes=outcome_yes, settlement_timestamp=timestamp
                    )
                    if not inserted and self.executions[model_id].settlement_record(ticker) is None:
                        raise ForwardShadowError("paper settlement recovery is inconsistent")
                    settlement = self.executions[model_id].settlement_record(ticker)
                    assert settlement is not None
                    self.store.append_settlement(
                        model_id=model_id,
                        opportunity_id=str(decision["opportunity_id"]),
                        ticker=ticker,
                        outcome_yes=outcome_yes,
                        settlement_timestamp=timestamp,
                        realized_pnl=settlement.realized_pnl,
                    )
        finally:
            connection.close()

    def close(self) -> None:
        for store in self.paper_stores:
            store.close()
        self.store.close()

    def __enter__(self) -> ForwardShadowRuntime:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _decision_id(model_id: str, opportunity_id: str) -> str:
    return "forward-" + hashlib.sha256(f"{model_id}:{opportunity_id}".encode()).hexdigest()[:30]


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ForwardShadowError("immutable model artifact is unavailable") from error
    if not isinstance(value, dict):
        raise ForwardShadowError("immutable model artifact is malformed")
    return value


def _verify_v2(manifest: dict[str, object], dataset: CertifiedDataset) -> None:
    if (
        manifest.get("format") != "live15-model-zoo-v2-development-v1"
        or manifest.get("status") != "FORWARD_CANDIDATE"
    ):
        raise ForwardShadowError("Model Zoo v2 is not a forward-candidate artifact")
    final = manifest.get("final_test")
    if (
        not isinstance(final, dict)
        or final.get("state") != "REVEALED_FINAL"
        or final.get("v2_test_rows_consumed_for_development") is not False
    ):
        raise ForwardShadowError("Model Zoo v2 final-test lineage is invalid")
    source = manifest.get("dataset")
    if (
        not isinstance(source, dict)
        or source.get("dataset_id") != dataset.dataset_id
        or source.get("deterministic_build_hash") != dataset.deterministic_build_hash
    ):
        raise ForwardShadowError("Model Zoo v2 dataset lineage conflicts")
    expected = {f"{name}@{threshold}" for name, threshold in FORWARD_CANDIDATES}
    actual = manifest.get("forward_candidates")
    if not isinstance(actual, list) or set(actual) != expected:
        raise ForwardShadowError(
            "Model Zoo v2 forward candidates conflict with approved frozen set"
        )
