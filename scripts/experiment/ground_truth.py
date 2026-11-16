"""Independent, control-plane-only Ground Truth state and serialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import tempfile
from typing import Any


FAULT_TYPES = {
    "cpu_saturation",
    "network_latency",
    "service_unavailable",
    "application_exception",
    "datastore_unavailable",
}
LOCATION_KINDS = {"service", "service_edge", "operation", "datastore"}
RECORD_FIELDS = {
    "schema_version",
    "fault_id",
    "fault_type",
    "root_location",
    "requested_start",
    "confirmed_start",
    "confirmed_end",
    "injection_confirmation",
    "recovery_confirmation",
    "execution_eligible",
    "status",
    "ineligibility_reason",
}


class GroundTruthError(ValueError):
    """Raised when a Ground Truth record violates its schema or state machine."""


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise GroundTruthError("timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GroundTruthError(f"invalid ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GroundTruthError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GroundTruthError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Confirmation:
    observed_at: str
    control_plane_check: str
    evidence: str

    @classmethod
    def create(
        cls, observed_at: str, control_plane_check: str, evidence: str
    ) -> "Confirmation":
        timestamp = format_timestamp(parse_timestamp(observed_at))
        if not control_plane_check.strip() or not evidence.strip():
            raise GroundTruthError("confirmation requires check and evidence")
        return cls(timestamp, control_plane_check.strip(), evidence.strip())

    def to_dict(self) -> dict[str, str]:
        return {
            "observed_at": self.observed_at,
            "control_plane_check": self.control_plane_check,
            "evidence": self.evidence,
        }


@dataclass
class GroundTruth:
    fault_id: str
    fault_type: str
    location_kind: str
    location: str
    requested_start: str
    confirmed_start: str | None = None
    confirmed_end: str | None = None
    injection_confirmation: Confirmation | None = None
    recovery_confirmation: Confirmation | None = None
    execution_eligible: bool = False
    status: str = "PENDING"
    ineligibility_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fault_id, str) or not self.fault_id.strip():
            raise GroundTruthError("fault_id must not be empty")
        if self.fault_type not in FAULT_TYPES:
            raise GroundTruthError(f"unsupported fault_type: {self.fault_type}")
        if self.location_kind not in LOCATION_KINDS:
            raise GroundTruthError(f"unsupported location kind: {self.location_kind}")
        if not isinstance(self.location, str) or not self.location.strip():
            raise GroundTruthError("root location must not be empty")
        if not isinstance(self.execution_eligible, bool):
            raise GroundTruthError("execution_eligible must be boolean")
        if self.ineligibility_reason is not None and not isinstance(
            self.ineligibility_reason, str
        ):
            raise GroundTruthError("ineligibility_reason must be a string or null")
        self.requested_start = format_timestamp(parse_timestamp(self.requested_start))
        self.validate()

    @classmethod
    def request(
        cls,
        fault_id: str,
        fault_type: str,
        location_kind: str,
        location: str,
        requested_start: str,
    ) -> "GroundTruth":
        """Create a pending record from control-plane intent only."""
        return cls(fault_id, fault_type, location_kind, location, requested_start)

    def confirm_injection(
        self,
        observed_at: str,
        control_plane_check: str,
        evidence: str,
    ) -> None:
        if self.status != "PENDING":
            raise GroundTruthError("injection can only be confirmed from PENDING")
        confirmation = Confirmation.create(observed_at, control_plane_check, evidence)
        if parse_timestamp(confirmation.observed_at) < parse_timestamp(self.requested_start):
            raise GroundTruthError("confirmed_start precedes requested_start")
        self.injection_confirmation = confirmation
        self.confirmed_start = confirmation.observed_at
        self.status = "ACTIVE"
        self.validate()

    def confirm_recovery(
        self,
        observed_at: str,
        control_plane_check: str,
        evidence: str,
    ) -> None:
        if self.status != "ACTIVE" or self.confirmed_start is None:
            raise GroundTruthError("recovery can only be confirmed for an ACTIVE fault")
        confirmation = Confirmation.create(observed_at, control_plane_check, evidence)
        if parse_timestamp(confirmation.observed_at) < parse_timestamp(self.confirmed_start):
            raise GroundTruthError("confirmed_end precedes confirmed_start")
        self.recovery_confirmation = confirmation
        self.confirmed_end = confirmation.observed_at
        self.execution_eligible = True
        self.status = "RECOVERED"
        self.validate()

    def mark_ineligible(self, reason: str) -> None:
        if self.status == "RECOVERED":
            raise GroundTruthError("a recovered eligible execution cannot be invalidated")
        if not reason.strip():
            raise GroundTruthError("ineligibility requires a reason")
        self.execution_eligible = False
        self.status = "INELIGIBLE"
        self.ineligibility_reason = reason.strip()
        self.validate()

    def validate(self) -> None:
        requested = parse_timestamp(self.requested_start)
        start = parse_timestamp(self.confirmed_start) if self.confirmed_start else None
        end = parse_timestamp(self.confirmed_end) if self.confirmed_end else None

        if self.status == "PENDING":
            valid = not any(
                (self.confirmed_start, self.confirmed_end, self.injection_confirmation,
                 self.recovery_confirmation, self.execution_eligible,
                 self.ineligibility_reason)
            )
        elif self.status == "ACTIVE":
            valid = (
                start is not None
                and start >= requested
                and self.injection_confirmation is not None
                and self.injection_confirmation.observed_at == self.confirmed_start
                and end is None
                and self.recovery_confirmation is None
                and not self.execution_eligible
                and self.ineligibility_reason is None
            )
        elif self.status == "RECOVERED":
            valid = (
                start is not None
                and end is not None
                and start >= requested
                and end >= start
                and self.injection_confirmation is not None
                and self.recovery_confirmation is not None
                and self.injection_confirmation.observed_at == self.confirmed_start
                and self.recovery_confirmation.observed_at == self.confirmed_end
                and self.execution_eligible
                and self.ineligibility_reason is None
            )
        elif self.status == "INELIGIBLE":
            valid = (
                not self.execution_eligible
                and bool(self.ineligibility_reason)
                and (start is None) == (self.injection_confirmation is None)
                and end is None
                and self.recovery_confirmation is None
                and (start is None or start >= requested)
            )
        else:
            raise GroundTruthError(f"unsupported status: {self.status}")
        if not valid:
            raise GroundTruthError(f"inconsistent Ground Truth state: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": "1.0",
            "fault_id": self.fault_id,
            "fault_type": self.fault_type,
            "root_location": {"kind": self.location_kind, "value": self.location},
            "requested_start": self.requested_start,
            "confirmed_start": self.confirmed_start,
            "confirmed_end": self.confirmed_end,
            "injection_confirmation": (
                self.injection_confirmation.to_dict()
                if self.injection_confirmation
                else None
            ),
            "recovery_confirmation": (
                self.recovery_confirmation.to_dict()
                if self.recovery_confirmation
                else None
            ),
            "execution_eligible": self.execution_eligible,
            "status": self.status,
            "ineligibility_reason": self.ineligibility_reason,
        }

    def write_json(self, path: Path) -> None:
        """Atomically persist the control-plane Ground Truth record."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GroundTruth":
        if set(data) != RECORD_FIELDS:
            missing = sorted(RECORD_FIELDS - set(data))
            extra = sorted(set(data) - RECORD_FIELDS)
            raise GroundTruthError(
                f"Ground Truth fields mismatch; missing={missing}, extra={extra}"
            )
        if data.get("schema_version") != "1.0":
            raise GroundTruthError("unsupported Ground Truth schema_version")
        location = data.get("root_location")
        if not isinstance(location, dict) or set(location) != {"kind", "value"}:
            raise GroundTruthError("root_location must be an object")
        if not isinstance(data.get("execution_eligible"), bool):
            raise GroundTruthError("execution_eligible must be boolean")

        def confirmation(name: str) -> Confirmation | None:
            value = data.get(name)
            if value is None:
                return None
            if not isinstance(value, dict):
                raise GroundTruthError(f"{name} must be an object or null")
            return Confirmation.create(
                value.get("observed_at", ""),
                value.get("control_plane_check", ""),
                value.get("evidence", ""),
            )

        record = cls(
            fault_id=data.get("fault_id", ""),
            fault_type=data.get("fault_type", ""),
            location_kind=location.get("kind", ""),
            location=location.get("value", ""),
            requested_start=data.get("requested_start", ""),
            confirmed_start=data.get("confirmed_start"),
            confirmed_end=data.get("confirmed_end"),
            injection_confirmation=confirmation("injection_confirmation"),
            recovery_confirmation=confirmation("recovery_confirmation"),
            execution_eligible=data.get("execution_eligible", False),
            status=data.get("status", ""),
            ineligibility_reason=data.get("ineligibility_reason"),
        )
        record.validate()
        return record

    @classmethod
    def read_json(cls, path: Path) -> "GroundTruth":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GroundTruthError(f"cannot read Ground Truth JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise GroundTruthError("Ground Truth JSON root must be an object")
        return cls.from_dict(data)
