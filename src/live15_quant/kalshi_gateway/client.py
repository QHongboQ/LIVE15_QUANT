"""SDK construction with explicit LIVE15 environment and credential boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from live15_quant.config import (
    KALSHI_DEMO_API_BASE_URL,
    KALSHI_DEMO_WEBSOCKET_URL,
    KALSHI_PRODUCTION_WEBSOCKET_URL,
    KALSHI_PUBLIC_API_BASE_URL,
)


class KalshiGatewayError(RuntimeError):
    """Raised when the SDK boundary cannot be constructed safely."""


class KalshiEnvironment(StrEnum):
    DEMO = "DEMO"
    PRODUCTION = "PRODUCTION"


@dataclass(frozen=True, slots=True)
class KalshiGatewayConfig:
    environment: KalshiEnvironment
    rest_base_url: str
    websocket_url: str
    timeout_seconds: float = 10.0
    read_retries: int = 3

    @classmethod
    def for_environment(
        cls,
        environment: KalshiEnvironment,
        *,
        timeout_seconds: float = 10.0,
        read_retries: int = 3,
    ) -> KalshiGatewayConfig:
        if timeout_seconds <= 0 or read_retries < 0:
            raise ValueError("Kalshi gateway timeout/retries are invalid")
        if environment is KalshiEnvironment.DEMO:
            return cls(
                environment,
                KALSHI_DEMO_API_BASE_URL,
                KALSHI_DEMO_WEBSOCKET_URL,
                timeout_seconds,
                read_retries,
            )
        if environment is KalshiEnvironment.PRODUCTION:
            return cls(
                environment,
                KALSHI_PUBLIC_API_BASE_URL,
                KALSHI_PRODUCTION_WEBSOCKET_URL,
                timeout_seconds,
                read_retries,
            )
        raise ValueError("unsupported Kalshi environment")

    def validate(self) -> None:
        expected = self.for_environment(
            self.environment,
            timeout_seconds=self.timeout_seconds,
            read_retries=self.read_retries,
        )
        if (
            self.rest_base_url != expected.rest_base_url
            or self.websocket_url != expected.websocket_url
        ):
            raise KalshiGatewayError("Kalshi gateway endpoint mismatch")


@dataclass(frozen=True, slots=True, repr=False)
class GatewayCredentials:
    api_key_id: str
    private_key_path: Path

    @classmethod
    def from_files(cls, api_key_id_path: Path, private_key_path: Path) -> GatewayCredentials:
        if not api_key_id_path.is_file() or not private_key_path.is_file():
            raise KalshiGatewayError("Kalshi credential file is unavailable")
        api_key_id = api_key_id_path.read_text(encoding="utf-8").strip()
        credentials = cls(api_key_id=api_key_id, private_key_path=private_key_path.resolve())
        credentials.validate()
        return credentials

    def validate(self) -> None:
        if not self.api_key_id or any(char.isspace() for char in self.api_key_id):
            raise KalshiGatewayError("Kalshi API key ID is malformed")
        if not self.private_key_path.is_absolute() or not self.private_key_path.is_file():
            raise KalshiGatewayError("Kalshi private-key path is unavailable")


_DEMO_RUNTIME_VARIABLES = (
    "KALSHI_DEMO",
    "LIVE15_KALSHI_DEMO_API_KEY_ID",
    "LIVE15_KALSHI_DEMO_API_KEY_ID_FILE",
    "LIVE15_KALSHI_DEMO_PRIVATE_KEY_PATH",
)
_SDK_ENDPOINT_OVERRIDE_VARIABLES = (
    "KALSHI_BASE_URL",
    "KALSHI_WS_BASE_URL",
)


def production_credentials(settings: Any) -> GatewayCredentials:
    """Load only the explicitly configured Production credential pair.

    Production runtime deliberately has no filesystem fallback: silently picking
    up an old read-only or Demo key makes REST/WS identity impossible to audit.
    """

    key_id_path = getattr(settings, "kalshi_production_api_key_id_path", None)
    private_key_path = getattr(settings, "kalshi_production_private_key_path", None)
    if key_id_path is None or private_key_path is None:
        raise KalshiGatewayError("Production Kalshi credential paths are not configured")
    return GatewayCredentials.from_files(Path(key_id_path), Path(private_key_path))


def production_runtime_environment(
    settings: Any,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a sanitized, explicit Production-only environment for a child.

    Demo credentials may exist for isolated tests, but never reach a formal
    Production runtime child.  The function rejects an explicit Demo mode or
    endpoint override rather than attempting an implicit environment switch.
    """

    environment = dict(base or {})
    demo_flag = environment.get("KALSHI_DEMO", "").strip().lower()
    if demo_flag in {"1", "true", "yes", "on"}:
        raise KalshiGatewayError("Production runtime refuses KALSHI_DEMO=true")
    for name in _SDK_ENDPOINT_OVERRIDE_VARIABLES:
        value = environment.get(name, "")
        if "demo" in value.lower():
            raise KalshiGatewayError(f"Production runtime refuses Demo endpoint override: {name}")
    credentials = production_credentials(settings)
    for name in _DEMO_RUNTIME_VARIABLES:
        environment.pop(name, None)
    for name in _SDK_ENDPOINT_OVERRIDE_VARIABLES:
        environment.pop(name, None)
    environment["LIVE15_KALSHI_RUNTIME_ENVIRONMENT"] = KalshiEnvironment.PRODUCTION.value
    # Preserve the exact caller-supplied ID-file location; it is intentionally
    # not derived from the private-key directory.
    key_id_path = Path(settings.kalshi_production_api_key_id_path).resolve()
    environment["LIVE15_KALSHI_PRODUCTION_API_KEY_ID_PATH"] = str(key_id_path)
    environment["LIVE15_KALSHI_PRODUCTION_PRIVATE_KEY_PATH"] = str(credentials.private_key_path)
    environment["LIVE15_ENABLE_KALSHI_PRODUCTION_WEBSOCKET"] = "true"
    return environment


def _sdk_types() -> tuple[type[Any], type[Any]]:
    try:
        from kalshi import KalshiClient, KalshiConfig
    except ImportError as error:  # pragma: no cover - deployment failure path
        raise KalshiGatewayError("kalshi-sdk==12.0.0 is unavailable") from error
    return KalshiClient, KalshiConfig


def build_sdk_client(
    config: KalshiGatewayConfig,
    *,
    credentials: GatewayCredentials | None = None,
) -> Any:
    """Build the pinned SDK without accepting SDK defaults or environment fallback."""

    config.validate()
    if credentials is not None:
        credentials.validate()
    client_type, config_type = _sdk_types()
    # SDK v12's host allowlist predates Kalshi's recommended external-api hosts.
    # LIVE15 validates exact constants above before enabling this SDK compatibility flag.
    sdk_config = config_type(
        base_url=config.rest_base_url,
        ws_base_url=config.websocket_url,
        timeout=config.timeout_seconds,
        max_retries=config.read_retries,
        total_timeout=config.timeout_seconds * (config.read_retries + 1),
        allow_unknown_host=True,
    )
    kwargs: dict[str, object] = {"config": sdk_config}
    if credentials is not None:
        kwargs.update(
            key_id=credentials.api_key_id,
            private_key_path=credentials.private_key_path,
        )
    return client_type(**kwargs)
