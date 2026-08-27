"""Transport-independent runtime ownership and health-resolution contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class OwnerType(StrEnum):
    SERVICE_MANAGED = "SERVICE_MANAGED"
    SUPERVISOR_MANAGED = "SUPERVISOR_MANAGED"
    IN_PROCESS_WORKER = "IN_PROCESS_WORKER"
    ON_DEMAND = "ON_DEMAND"
    RETIRED = "RETIRED"


class ComponentHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"
    STALE_TELEMETRY = "STALE_TELEMETRY"


@dataclass(frozen=True, slots=True)
class ServiceHealthObservation:
    """The Windows service is authoritative; receipts are corroborating telemetry."""

    service_running: bool
    heartbeat_at: datetime | None
    checked_at: datetime
    stale_after_seconds: float

    def resolve(self) -> ComponentHealth:
        if not self.service_running:
            return ComponentHealth.STOPPED
        if self.heartbeat_at is None:
            return ComponentHealth.STALE_TELEMETRY
        heartbeat = self.heartbeat_at.astimezone(UTC)
        checked = self.checked_at.astimezone(UTC)
        if (checked - heartbeat).total_seconds() > self.stale_after_seconds:
            return ComponentHealth.STALE_TELEMETRY
        return ComponentHealth.HEALTHY
