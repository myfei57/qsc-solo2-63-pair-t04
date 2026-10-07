"""Registry of the physical chutes and carousels the terminal operates.

Lanes are mirrored in the committed document (``lane.put``) so a restart knows
which chutes exist, whether they are enabled and how much capacity each has;
the in-memory registry stays a fast lookup in front of that record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bagsort.clock import Clock
from bagsort.errors import NotFoundError, ValidationError
from bagsort.model.entities import LaneRecord
from bagsort.store.store import Store

DEFAULT_CAPACITY = 40


@dataclass(frozen=True)
class Lane:
    """One chute with the carousel it feeds."""

    chute_id: str
    carousel_id: str
    enabled: bool = True
    capacity: int = DEFAULT_CAPACITY

    def as_dict(self) -> dict[str, object]:
        return {
            "chute_id": self.chute_id,
            "carousel_id": self.carousel_id,
            "enabled": self.enabled,
            "capacity": self.capacity,
        }


@dataclass
class ChuteRegistry:
    """Which chutes exist, which are enabled and which carousel they feed."""

    lanes: dict[str, Lane] = field(default_factory=dict)
    _store: Store | None = None
    _clock: Clock | None = None

    def bind(self, store: Store, clock: Clock) -> None:
        """Attach the journal and hydrate every committed lane into memory."""

        self._store = store
        self._clock = clock
        self.sync_from_store()

    def register(
        self,
        chute_id: str,
        carousel_id: str,
        enabled: bool = True,
        capacity: int = DEFAULT_CAPACITY,
    ) -> Lane:
        identifier = (chute_id or "").strip()
        if not identifier:
            raise ValidationError("chute id must not be empty")
        if capacity < 0:
            raise ValidationError("chute capacity must not be negative")
        lane = Lane(
            chute_id=identifier,
            carousel_id=carousel_id.strip(),
            enabled=enabled,
            capacity=capacity,
        )
        self.lanes[identifier] = lane
        self._persist(lane)
        return lane

    def set_capacity(self, chute_id: str, capacity: int) -> Lane:
        if capacity < 0:
            raise ValidationError("chute capacity must not be negative")
        lane = self.get(chute_id)
        return self._replace(lane, capacity=capacity)

    def get(self, chute_id: str) -> Lane:
        lane = self.lanes.get(chute_id)
        if lane is None:
            raise NotFoundError(f"chute {chute_id!r} is not registered")
        return lane

    def known(self, chute_id: str) -> bool:
        return chute_id in self.lanes

    def validate(self, chute_id: str) -> bool:
        lane = self.lanes.get((chute_id or "").strip())
        return bool(lane and lane.enabled)

    def disable(self, chute_id: str) -> Lane:
        lane = self.get(chute_id)
        return self._replace(lane, enabled=False)

    def enable(self, chute_id: str) -> Lane:
        lane = self.get(chute_id)
        return self._replace(lane, enabled=True)

    def enabled_chutes(self) -> list[str]:
        return sorted(lane.chute_id for lane in self.lanes.values() if lane.enabled)

    def disabled_chutes(self) -> list[str]:
        return sorted(lane.chute_id for lane in self.lanes.values() if not lane.enabled)

    def capacity(self, chute_id: str) -> int:
        return self.get(chute_id).capacity

    def occupancy(self, chute_id: str) -> int:
        """Bags already sitting in a chute, counted from the committed view."""

        self.get(chute_id)
        if self._store is None:
            return 0
        return sum(
            1
            for bag in self._store.state().bags.values()
            if bag.chute_id == chute_id and bag.state.active
        )

    def headroom(self, chute_id: str) -> int:
        lane = self.get(chute_id)
        return max(0, lane.capacity - self.occupancy(chute_id))

    def carousels(self) -> list[str]:
        return sorted({lane.carousel_id for lane in self.lanes.values() if lane.carousel_id})

    def as_list(self) -> list[dict[str, Any]]:
        return [self._row(self.lanes[key]) for key in sorted(self.lanes)]

    def summary(self) -> dict[str, object]:
        return {
            "lanes": self.as_list(),
            "enabled": self.enabled_chutes(),
            "disabled": self.disabled_chutes(),
            "carousels": self.carousels(),
            "count": len(self.lanes),
        }

    def sync_from_store(self) -> int:
        """Rebuild the in-memory lanes from committed ``lane.put`` commands."""

        if self._store is None:
            return 0
        self.lanes = {
            record.chute_id: Lane(
                chute_id=record.chute_id,
                carousel_id=record.carousel_id,
                enabled=record.enabled,
                capacity=record.capacity,
            )
            for record in self._store.state().lanes.values()
        }
        return len(self.lanes)

    def _row(self, lane: Lane) -> dict[str, Any]:
        row = dict(lane.as_dict())
        row["occupied"] = self.occupancy(lane.chute_id)
        row["headroom"] = self.headroom(lane.chute_id)
        return row

    def _replace(self, lane: Lane, **changes: Any) -> Lane:
        updated = Lane(
            chute_id=lane.chute_id,
            carousel_id=changes.get("carousel_id", lane.carousel_id),
            enabled=changes.get("enabled", lane.enabled),
            capacity=changes.get("capacity", lane.capacity),
        )
        self.lanes[lane.chute_id] = updated
        self._persist(updated)
        return updated

    def _persist(self, lane: Lane) -> None:
        if self._store is None or self._clock is None:
            return
        record = LaneRecord(
            chute_id=lane.chute_id,
            carousel_id=lane.carousel_id,
            enabled=lane.enabled,
            capacity=lane.capacity,
        )
        with self._store.session() as session:
            session.state.lanes[lane.chute_id] = record
            session.record(
                "lane.put", {"lane": record.as_dict()}, self._clock.now_iso()
            )
