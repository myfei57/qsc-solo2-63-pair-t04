"""Immutable-by-convention domain records and their JSON codecs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .enums import BagState, FlightState


def _clean(raw: Any) -> Any:
    """Normalise nested containers so codecs round-trip deterministically."""

    if isinstance(raw, dict):
        return {str(key): _clean(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        return [_clean(item) for item in raw]
    return raw


@dataclass(frozen=True)
class Flight:
    id: str
    code: str
    terminal: str = "T1"
    carousel: str = ""
    chute: str = ""
    state: FlightState = FlightState.OPEN
    batch: int = 0
    schedule_generation: int = 1
    cutoff_at: str = ""
    closed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "terminal": self.terminal,
            "carousel": self.carousel,
            "chute": self.chute,
            "state": self.state.value,
            "batch": self.batch,
            "schedule_generation": self.schedule_generation,
            "cutoff_at": self.cutoff_at,
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Flight":
        return cls(
            id=str(raw["id"]),
            code=str(raw.get("code", "")),
            terminal=str(raw.get("terminal", "T1")),
            carousel=str(raw.get("carousel", "")),
            chute=str(raw.get("chute", "")),
            state=FlightState(raw.get("state", FlightState.OPEN.value)),
            batch=int(raw.get("batch", 0)),
            schedule_generation=int(raw.get("schedule_generation", 1)),
            cutoff_at=str(raw.get("cutoff_at", "")),
            closed_at=str(raw.get("closed_at", "")),
        )

    def evolved(self, **changes: Any) -> "Flight":
        return replace(self, **changes)


@dataclass(frozen=True)
class Bag:
    id: str
    barcode: str
    sequence: int
    epoch: int = 0
    flight_id: str = ""
    chute_id: str = ""
    state: BagState = BagState.CHECKED_IN

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "barcode": self.barcode,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Bag":
        return cls(
            id=str(raw["id"]),
            barcode=str(raw.get("barcode", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            state=BagState(raw.get("state", BagState.CHECKED_IN.value)),
        )

    def evolved(self, **changes: Any) -> "Bag":
        return replace(self, **changes)


@dataclass(frozen=True)
class Reading:
    barcode: str
    bag_id: str
    sequence: int
    epoch: int
    committed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "bag_id": self.bag_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "committed": self.committed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Reading":
        return cls(
            barcode=str(raw.get("barcode", "")),
            bag_id=str(raw.get("bag_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            committed=bool(raw.get("committed", False)),
        )


@dataclass(frozen=True)
class ChuteAssignment:
    flight_id: str
    chute_id: str
    generation: int = 1
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "generation": self.generation,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChuteAssignment":
        return cls(
            flight_id=str(raw["flight_id"]),
            chute_id=str(raw.get("chute_id", "")),
            generation=int(raw.get("generation", 1)),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass(frozen=True)
class SortRecord:
    bag_id: str
    flight_id: str
    chute_id: str
    sequence: int
    epoch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "chute_id": self.chute_id,
            "sequence": self.sequence,
            "epoch": self.epoch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SortRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            chute_id=str(raw.get("chute_id", "")),
            sequence=int(raw.get("sequence", 0)),
            epoch=int(raw.get("epoch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class TransferCommand:
    id: str
    flight_id: str
    carousel: str
    batch: int
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flight_id": self.flight_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TransferCommand":
        return cls(
            id=str(raw["id"]),
            flight_id=str(raw.get("flight_id", "")),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            created_at=str(raw.get("created_at", "")),
        )


@dataclass(frozen=True)
class LoadRecord:
    bag_id: str
    carousel: str
    batch: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "bag_id": self.bag_id,
            "carousel": self.carousel,
            "batch": self.batch,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LoadRecord":
        return cls(
            bag_id=str(raw["bag_id"]),
            carousel=str(raw.get("carousel", "")),
            batch=int(raw.get("batch", 0)),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class LaneRecord:
    """A physical chute as it exists in the committed document.

    Unlike the in-memory registry, this survives a restart. ``capacity`` is how
    many bags the chute may hold; a reroute plan treats
    ``capacity - bags currently sitting in the chute`` as the headroom a
    diverted flight may move into.
    """

    chute_id: str
    carousel_id: str
    enabled: bool = True
    capacity: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "chute_id": self.chute_id,
            "carousel_id": self.carousel_id,
            "enabled": self.enabled,
            "capacity": self.capacity,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LaneRecord":
        return cls(
            chute_id=str(raw["chute_id"]),
            carousel_id=str(raw.get("carousel_id", "")),
            enabled=bool(raw.get("enabled", True)),
            capacity=int(raw.get("capacity", 0)),
        )

    def evolved(self, **changes: Any) -> "LaneRecord":
        return replace(self, **changes)


@dataclass(frozen=True)
class DegradationIncident:
    """One chute outage and the reroute generation it forced.

    The before/after snapshots are a journal watermark plus the digest of every
    committed entry up to it, so an auditor can prove the folded state around
    the outage matches what the audit chain says happened.
    """

    id: str
    chute_id: str
    status: str = "active"
    affected_flights: tuple[str, ...] = ()
    generations: dict[str, int] = field(default_factory=dict)
    before_watermark: int = 0
    before_digest: str = ""
    after_watermark: int = 0
    after_digest: str = ""
    opened_at: str = ""
    recovered_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chute_id": self.chute_id,
            "status": self.status,
            "affected_flights": list(self.affected_flights),
            "generations": dict(sorted(self.generations.items())),
            "before_watermark": self.before_watermark,
            "before_digest": self.before_digest,
            "after_watermark": self.after_watermark,
            "after_digest": self.after_digest,
            "opened_at": self.opened_at,
            "recovered_at": self.recovered_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DegradationIncident":
        return cls(
            id=str(raw["id"]),
            chute_id=str(raw["chute_id"]),
            status=str(raw.get("status", "active")),
            affected_flights=tuple(str(item) for item in raw.get("affected_flights", ())),
            generations={str(key): int(value) for key, value in raw.get("generations", {}).items()},
            before_watermark=int(raw.get("before_watermark", 0)),
            before_digest=str(raw.get("before_digest", "")),
            after_watermark=int(raw.get("after_watermark", 0)),
            after_digest=str(raw.get("after_digest", "")),
            opened_at=str(raw.get("opened_at", "")),
            recovered_at=str(raw.get("recovered_at", "")),
        )

    def evolved(self, **changes: Any) -> "DegradationIncident":
        return replace(self, **changes)


@dataclass(frozen=True)
class RerouteRecord:
    """The ledger line saying one piece of baggage followed a degradation move.

    ``kind`` is ``queued_in_place`` for a bag still in the in-transit queue that
    inherited the new assignment generation without moving twice,
    ``stranded_rescue`` for a bag pulled out of the dead chute into recheck, and
    ``overflow_manual`` for a bag no spare chute could take, left to explicit
    manual handling rather than being dropped silently.
    """

    incident_id: str
    bag_id: str
    flight_id: str
    from_chute: str
    to_chute: str
    generation: int
    kind: str
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "bag_id": self.bag_id,
            "flight_id": self.flight_id,
            "from_chute": self.from_chute,
            "to_chute": self.to_chute,
            "generation": self.generation,
            "kind": self.kind,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RerouteRecord":
        return cls(
            incident_id=str(raw["incident_id"]),
            bag_id=str(raw["bag_id"]),
            flight_id=str(raw.get("flight_id", "")),
            from_chute=str(raw.get("from_chute", "")),
            to_chute=str(raw.get("to_chute", "")),
            generation=int(raw.get("generation", 0)),
            kind=str(raw.get("kind", "")),
            at=str(raw.get("at", "")),
        )


@dataclass(frozen=True)
class Sequence:
    epoch: int = 0
    next_value: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "next": self.next_value}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Sequence":
        return cls(epoch=int(raw.get("epoch", 0)), next_value=int(raw.get("next", 0)))


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    kind: str
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)
    prev_digest: str = ""
    digest: str = ""
    at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "subject": self.subject,
            "detail": _clean(self.detail),
            "prev_digest": self.prev_digest,
            "digest": self.digest,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AuditEntry":
        return cls(
            sequence=int(raw.get("sequence", 0)),
            kind=str(raw.get("kind", "")),
            subject=str(raw.get("subject", "")),
            detail=_clean(raw.get("detail", {})),
            prev_digest=str(raw.get("prev_digest", "")),
            digest=str(raw.get("digest", "")),
            at=str(raw.get("at", "")),
        )
