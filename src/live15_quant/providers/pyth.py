"""Authenticated, read-only, multi-feed Pyth Hermes ingestion."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType

import requests

from live15_quant.config import Settings
from live15_quant.models import Asset, FreshnessState, UnderlyingObservation, UnderlyingProvider
from live15_quant.secrets import is_project_secret_path

PYTH_FEEDS: Mapping[Asset, tuple[str, str]] = MappingProxyType(
    {
        Asset.GOLD: (
            "Metal.XAU/USD",
            "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2",
        ),
        Asset.SILVER: (
            "Metal.XAG/USD",
            "f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e",
        ),
        Asset.WTI_OIL: (
            "Commodities.USOILSPOT",
            "925ca92ff005ae943c158e3563f59698ce7e75c5a8c8dd43303a0a154887b3e6",
        ),
        Asset.HYPE: (
            "Crypto.HYPE/USD",
            "4279e31cc369bbcc2faf022b382b080e32a8e689ff20fbc530d2a603eb6cd98b",
        ),
        Asset.BNB: (
            "Crypto.BNB/USD",
            "2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f",
        ),
    }
)
PYTH_ASSETS = tuple(PYTH_FEEDS)
PYTH_FEED_REGISTRY_URL = "https://hermes.pyth.network/v2/price_feeds"


class PythPayloadError(ValueError):
    """Hermes returned an envelope that cannot be interpreted safely."""


class PythCredentialError(ValueError):
    """The external key-file boundary is absent or unsafe."""


class PythNetworkError(ConnectionError):
    """A sanitized transient Hermes transport failure with safe diagnostics."""

    def __init__(self, message: str, *, category: str = "NETWORK", cause_type: str | None = None):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type


class PythRateLimitError(PythNetworkError):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__("Pyth request rate limited")
        self.retry_after_seconds = max(0.0, retry_after_seconds)


@dataclass(frozen=True, slots=True)
class PythFeedIssue:
    """One feed-local problem; raw payloads and credentials are deliberately absent."""

    code: str
    asset: Asset | None = None
    feed_id: str | None = None


@dataclass(frozen=True, slots=True)
class PythUpdateBatch:
    observations: tuple[UnderlyingObservation, ...]
    issues: tuple[PythFeedIssue, ...] = ()
    socket_received_monotonic_ns: int | None = None
    parse_completed_monotonic_ns: int | None = None


class PythFeedDemultiplexer:
    """Keep feed timelines independent and reject duplicate/out-of-order updates."""

    def __init__(self) -> None:
        self._last: dict[Asset, UnderlyingObservation] = {}

    def accept(self, batch: PythUpdateBatch) -> PythUpdateBatch:
        accepted: list[UnderlyingObservation] = []
        issues = list(batch.issues)
        for observation in batch.observations:
            prior = self._last.get(observation.asset)
            if prior is not None and observation.source_timestamp < prior.source_timestamp:
                issues.append(PythFeedIssue("out_of_order", observation.asset, observation.feed_id))
                continue
            if prior is not None and observation.source_timestamp == prior.source_timestamp:
                if observation.price == prior.price and observation.confidence == prior.confidence:
                    issues.append(
                        PythFeedIssue("duplicate", observation.asset, observation.feed_id)
                    )
                    continue
                # publish_time has second precision; changed states within one second
                # are legitimate observations and must not be discarded.
                self._last[observation.asset] = observation
                accepted.append(observation)
                continue
            self._last[observation.asset] = observation
            accepted.append(observation)
        return PythUpdateBatch(
            tuple(accepted),
            tuple(issues),
            batch.socket_received_monotonic_ns,
            batch.parse_completed_monotonic_ns,
        )


class _RequestBudget:
    """Shared sliding-window budget with headroom below the official 10/10 seconds."""

    def __init__(self, maximum: int, monotonic: Callable[[], float]) -> None:
        if maximum <= 0 or maximum > 10:
            raise ValueError("Pyth request budget must be within 1..10 per 10 seconds")
        self._maximum = maximum
        self._monotonic = monotonic
        self._requests: deque[float] = deque()
        self._lock = threading.Lock()

    def consume(self) -> None:
        with self._lock:
            now = self._monotonic()
            while self._requests and now - self._requests[0] >= 10:
                self._requests.popleft()
            if len(self._requests) >= self._maximum:
                raise PythRateLimitError(10 - (now - self._requests[0]))
            self._requests.append(now)


def read_pyth_api_key(path: Path | None) -> str:
    """Read a server-side Pyth key without permitting repository-local credentials."""

    if path is None:
        raise PythCredentialError("Pyth API key path is not configured")
    resolved = path.expanduser().resolve()
    repository_roots = {
        candidate
        for start in (Path.cwd().resolve(), Path(__file__).resolve())
        for candidate in (start, *start.parents)
        if (candidate / ".git").exists()
    }
    if any(resolved.is_relative_to(root) for root in repository_roots) and not any(
        is_project_secret_path(resolved, project_root=root) for root in repository_roots
    ):
        raise PythCredentialError("Pyth API key must remain outside the repository")
    try:
        key = resolved.read_text(encoding="utf-8").strip()
    except OSError:
        raise PythCredentialError("Pyth API key file is unavailable") from None
    if not key or "\n" in key or "\r" in key:
        raise PythCredentialError("Pyth API key file must contain one non-empty value")
    return key


class PythHermesClient:
    """One authenticated SSE connection with one batch-REST fallback path."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = settings.pyth_hermes_base_url.rstrip("/")
        self._timeout = settings.request_timeout_seconds
        self._stream_timeout = settings.pyth_stream_read_timeout_seconds
        self._max_source_age = settings.recorder_pyth_stale_seconds
        self._key = read_pyth_api_key(settings.pyth_api_key_path)
        self._session = session or requests.Session()
        self._budget = _RequestBudget(settings.pyth_request_budget_per_10_seconds, monotonic)
        self._active_response: requests.Response | None = None
        self._response_lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _params(feed_ids: tuple[str, ...] | None = None) -> list[tuple[str, str]]:
        ids = (
            feed_ids
            if feed_ids is not None
            else tuple(feed_id for _, feed_id in PYTH_FEEDS.values())
        )
        return [("ids[]", feed_id) for feed_id in ids]

    def _get(
        self, endpoint: str, *, stream: bool, feed_ids: tuple[str, ...] | None = None
    ) -> requests.Response:
        if self._closed:
            raise PythNetworkError("Pyth client is closed")
        self._budget.consume()
        try:
            response = self._session.get(
                f"{self._base_url}{endpoint}",
                params=self._params(feed_ids),
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Accept": "text/event-stream" if stream else "application/json",
                },
                timeout=(self._timeout, self._stream_timeout if stream else self._timeout),
                stream=stream,
            )
        except requests.exceptions.SSLError as error:
            raise PythNetworkError(
                "Pyth request failed", category="TLS", cause_type=type(error).__name__
            ) from None
        except requests.exceptions.ProxyError as error:
            raise PythNetworkError(
                "Pyth request failed", category="PROXY", cause_type=type(error).__name__
            ) from None
        except requests.exceptions.ConnectTimeout as error:
            raise PythNetworkError(
                "Pyth request failed", category="CONNECT_TIMEOUT", cause_type=type(error).__name__
            ) from None
        except requests.exceptions.ReadTimeout as error:
            raise PythNetworkError(
                "Pyth request failed", category="READ_TIMEOUT", cause_type=type(error).__name__
            ) from None
        except requests.exceptions.ConnectionError as error:
            raise PythNetworkError(
                "Pyth request failed", category="CONNECTION", cause_type=type(error).__name__
            ) from None
        except requests.RequestException as error:
            raise PythNetworkError(
                "Pyth request failed", category="REQUEST", cause_type=type(error).__name__
            ) from None
        if response.status_code == 429:
            retry_after = _retry_after(response.headers.get("Retry-After"))
            response.close()
            raise PythRateLimitError(retry_after)
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            category = (
                "AUTH" if status in (401, 403) else "HTTP_RATE_LIMIT" if status == 429 else "HTTP"
            )
            raise PythNetworkError(
                f"Pyth request failed with HTTP {status}",
                category=category,
                cause_type=f"HTTP_{status}",
            )
        return response

    def latest_batch(self, *, feed_ids: tuple[str, ...] | None = None) -> PythUpdateBatch:
        """Fetch the configured feeds with one REST request.

        Hermes may legitimately omit a feed whose provider listing has changed
        (for example, a retired commodity symbol).  Keep valid sibling feeds
        usable and surface the omission as a feed-local issue instead of
        converting the whole batch into a transport outage.
        """

        try:
            response = self._get("/v2/updates/price/latest", stream=False, feed_ids=feed_ids)
        except PythNetworkError as error:
            # Hermes can return 404 for a batch containing a retired feed.
            # Retry each configured feed independently so valid siblings remain
            # available and the retired feed is represented as a local issue.
            if error.category != "HTTP" or error.cause_type != "HTTP_404":
                raise
            batches: list[PythUpdateBatch] = []
            requested_ids = (
                feed_ids
                if feed_ids is not None
                else tuple(feed_id for _, feed_id in PYTH_FEEDS.values())
            )
            for feed_id in requested_ids:
                try:
                    single = self._get(
                        "/v2/updates/price/latest", stream=False, feed_ids=(feed_id,)
                    )
                except PythNetworkError as single_error:
                    if single_error.category != "HTTP" or single_error.cause_type != "HTTP_404":
                        raise
                    asset = next(asset for asset, (_, fid) in PYTH_FEEDS.items() if fid == feed_id)
                    batches.append(
                        PythUpdateBatch((), (PythFeedIssue("feed_unavailable", asset, feed_id),))
                    )
                    continue
                try:
                    batches.append(
                        parse_update_payload(
                            single.json(),
                            received=datetime.now(UTC),
                            source=f"{self._base_url}/v2/updates/price/latest",
                            max_source_age_seconds=self._max_source_age,
                            require_all=False,
                        )
                    )
                finally:
                    single.close()
            return PythUpdateBatch(
                tuple(obs for batch in batches for obs in batch.observations),
                tuple(issue for batch in batches for issue in batch.issues),
            )
        try:
            try:
                payload = response.json()
            except (requests.JSONDecodeError, ValueError):
                raise PythPayloadError("Hermes response is not JSON") from None
            return parse_update_payload(
                payload,
                received=datetime.now(UTC),
                source=f"{self._base_url}/v2/updates/price/latest",
                max_source_age_seconds=self._max_source_age,
                require_all=True,
            )
        finally:
            response.close()

    def stream_batches(
        self, *, feed_ids: tuple[str, ...] | None = None
    ) -> Iterator[PythUpdateBatch]:
        """Yield demultiplexable events from one selected-feed SSE connection."""

        response = self._get("/v2/updates/price/stream", stream=True, feed_ids=feed_ids)
        with self._response_lock:
            self._active_response = response
        data_lines: list[str] = []
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if self._closed:
                    return
                line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
                if line == "":
                    if data_lines:
                        yield _parse_sse_data(
                            "\n".join(data_lines),
                            source=f"{self._base_url}/v2/updates/price/stream",
                            max_source_age_seconds=self._max_source_age,
                        )
                        data_lines.clear()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                yield _parse_sse_data(
                    "\n".join(data_lines),
                    source=f"{self._base_url}/v2/updates/price/stream",
                    max_source_age_seconds=self._max_source_age,
                )
        except requests.exceptions.SSLError as error:
            if not self._closed:
                raise PythNetworkError(
                    "Pyth stream disconnected", category="TLS", cause_type=type(error).__name__
                ) from None
        except requests.exceptions.ProxyError as error:
            if not self._closed:
                raise PythNetworkError(
                    "Pyth stream disconnected", category="PROXY", cause_type=type(error).__name__
                ) from None
        except requests.exceptions.ConnectTimeout as error:
            if not self._closed:
                raise PythNetworkError(
                    "Pyth stream disconnected",
                    category="CONNECT_TIMEOUT",
                    cause_type=type(error).__name__,
                ) from None
        except requests.exceptions.ReadTimeout as error:
            if not self._closed:
                raise PythNetworkError(
                    "Pyth stream disconnected",
                    category="READ_TIMEOUT",
                    cause_type=type(error).__name__,
                ) from None
        except (requests.ConnectionError, OSError) as error:
            if not self._closed:
                raise PythNetworkError(
                    "Pyth stream disconnected",
                    category="CONNECTION",
                    cause_type=type(error).__name__,
                ) from None
        except Exception:
            # Closing a requests response from another thread can make its iterator
            # fail in implementation-specific ways. Only suppress that during an
            # intentional shutdown; programming/correctness errors must escape.
            if not self._closed:
                raise
        finally:
            with self._response_lock:
                if self._active_response is response:
                    self._active_response = None
            response.close()

    def close(self) -> None:
        self._closed = True
        self._key = ""
        with self._response_lock:
            active = self._active_response
            self._active_response = None
        if active is not None:
            active.close()
        self._session.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self._base_url!r}, authenticated=True)"


def _parse_sse_data(data: str, *, source: str, max_source_age_seconds: float) -> PythUpdateBatch:
    received_monotonic_ns = time.perf_counter_ns()
    received = datetime.now(UTC)
    try:
        payload = json.loads(data)
    except (TypeError, ValueError):
        return PythUpdateBatch(
            (),
            (PythFeedIssue("malformed_sse_json"),),
            received_monotonic_ns,
            time.perf_counter_ns(),
        )
    try:
        parsed = parse_update_payload(
            payload,
            received=received,
            source=source,
            max_source_age_seconds=max_source_age_seconds,
            require_all=False,
        )
        return PythUpdateBatch(
            parsed.observations,
            parsed.issues,
            received_monotonic_ns,
            time.perf_counter_ns(),
        )
    except PythPayloadError:
        return PythUpdateBatch(
            (),
            (PythFeedIssue("malformed_sse_envelope"),),
            received_monotonic_ns,
            time.perf_counter_ns(),
        )


def parse_update_payload(
    payload: object,
    *,
    received: datetime,
    source: str,
    max_source_age_seconds: float,
    require_all: bool,
) -> PythUpdateBatch:
    """Parse independent feed items; malformed items never reject valid siblings."""

    if not isinstance(payload, dict) or not isinstance(payload.get("parsed"), list):
        raise PythPayloadError("Hermes payload is missing parsed updates")
    expected = {feed_id: asset for asset, (_, feed_id) in PYTH_FEEDS.items()}
    observations: list[UnderlyingObservation] = []
    issues: list[PythFeedIssue] = []
    seen: set[str] = set()
    for item in payload["parsed"]:
        if not isinstance(item, dict):
            issues.append(PythFeedIssue("malformed_feed_update"))
            continue
        feed_id = str(item.get("id", "")).lower().removeprefix("0x")
        asset = expected.get(feed_id)
        if asset is None:
            issues.append(PythFeedIssue("unexpected_feed_id", feed_id=feed_id or None))
            continue
        if feed_id in seen:
            issues.append(PythFeedIssue("duplicate_feed_in_batch", asset, feed_id))
            continue
        seen.add(feed_id)
        price = item.get("price")
        if not isinstance(price, dict):
            issues.append(PythFeedIssue("malformed_price", asset, feed_id))
            continue
        try:
            exponent = int(price["expo"])
            scaled_price = Decimal(str(price["price"])) * (Decimal(10) ** exponent)
            confidence = Decimal(str(price["conf"])) * (Decimal(10) ** exponent)
            source_timestamp = datetime.fromtimestamp(int(price["publish_time"]), UTC)
            age = (received - source_timestamp).total_seconds()
            freshness = (
                FreshnessState.UNKNOWN
                if age < -1
                else FreshnessState.FRESH
                if age <= max_source_age_seconds
                else FreshnessState.STALE
            )
            symbol, _ = PYTH_FEEDS[asset]
            observation = UnderlyingObservation(
                asset=asset,
                provider=UnderlyingProvider.PYTH_HERMES,
                symbol=symbol,
                feed_id=feed_id,
                price=scaled_price,
                source_timestamp=source_timestamp,
                received_timestamp=received,
                confidence=confidence,
                provenance=source,
                freshness=freshness,
            )
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, OSError):
            issues.append(PythFeedIssue("malformed_price", asset, feed_id))
            continue
        observations.append(observation)
    if require_all:
        for feed_id, asset in expected.items():
            if feed_id not in seen:
                issues.append(PythFeedIssue("missing_feed", asset, feed_id))
    return PythUpdateBatch(tuple(observations), tuple(issues))


def _retry_after(value: str | None) -> float:
    try:
        return min(60.0, max(0.0, float(value))) if value is not None else 60.0
    except ValueError:
        return 60.0


def discovery_metadata(assets: tuple[Asset, ...] = PYTH_ASSETS) -> tuple[dict[str, str], ...]:
    """Query the public registry only to verify exact configured IDs; never returns prices."""

    session = requests.Session()
    try:
        result = []
        for asset in assets:
            symbol, feed_id = PYTH_FEEDS[asset]
            response = session.get(PYTH_FEED_REGISTRY_URL, params={"query": symbol}, timeout=10)
            response.raise_for_status()
            entries = response.json()
            exact = [item for item in entries if str(item.get("id", "")).lower() == feed_id]
            if len(exact) != 1:
                raise PythPayloadError(f"official registry did not verify {asset.value}")
            result.append({"asset": asset.value, "symbol": symbol, "feed_id": feed_id})
        return tuple(result)
    finally:
        session.close()
