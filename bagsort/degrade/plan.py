"""Impact planning for a chute degradation, as a pure function of state.

The planner reads the committed state and the lane registry and answers three
questions without writing anything: which flights sit on the degraded chute,
how many of their bags are still waiting to drop, and which backup lanes can
absorb them. Execution lives in :mod:`bagsort.degrade.manager`; what is
computed here is exactly what gets committed there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bagsort.model.enums import BagState
from bagsort.model.state import State
from bagsort.ns.chutes import Lane

#: Bags in these states have not dropped anywhere yet, so they still consume
#: capacity on whichever chute their flight routes to.
PENDING_STATES = (BagState.CHECKED_IN, BagState.INJECTED, BagState.RECHECKED)


@dataclass(frozen=True)
class AffectedFlight:
    """A flight currently assigned to the degraded chute."""

    flight_id: str
    code: str
    state: str
    generation: int
    queued_bags: int
    pending_bags: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "code": self.code,
            "state": self.state,
            "generation": self.generation,
            "queued_bags": self.queued_bags,
            "pending_bags": self.pending_bags,
        }


@dataclass(frozen=True)
class CandidateLane:
    """A backup lane and the room it has left for not-yet-sorted bags."""

    chute_id: str
    carousel_id: str
    capacity: int
    load: int
    free: int | None  # None means the lane has no configured bound

    def as_dict(self) -> dict[str, Any]:
        return {
            "chute_id": self.chute_id,
            "carousel_id": self.carousel_id,
            "capacity": self.capacity,
            "load": self.load,
            "free": self.free,
        }


@dataclass(frozen=True)
class Move:
    """The reroute the planner proposes for one flight."""

    flight_id: str
    from_chute: str
    from_generation: int
    to_chute: str
    to_generation: int
    pending_bags: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "from_chute": self.from_chute,
            "from_generation": self.from_generation,
            "to_chute": self.to_chute,
            "to_generation": self.to_generation,
            "pending_bags": self.pending_bags,
        }


@dataclass(frozen=True)
class ImpactReport:
    """Everything a degradation would touch, computed before anything moves."""

    chute_id: str
    generated_at: str
    watermark: int
    flights: tuple[AffectedFlight, ...]
    queued_total: int
    stranded_sorted: tuple[str, ...]
    moves: tuple[Move, ...]
    candidates: tuple[CandidateLane, ...]
    overflow: tuple[str, ...]

    @property
    def absorbable(self) -> bool:
        """Whether every affected flight fits on some backup lane."""

        return not self.overflow

    def as_dict(self) -> dict[str, Any]:
        return {
            "chute_id": self.chute_id,
            "generated_at": self.generated_at,
            "watermark": self.watermark,
            "flights": [flight.as_dict() for flight in self.flights],
            "queued_total": self.queued_total,
            "stranded_sorted": list(self.stranded_sorted),
            "moves": [move.as_dict() for move in self.moves],
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "overflow": list(self.overflow),
            "absorbable": self.absorbable,
        }


def _pending_by_chute(state: State) -> dict[str, int]:
    """Not-yet-sorted bags grouped by the chute their flight routes to."""

    load: dict[str, int] = {}
    for bag in state.bags.values():
        if bag.state not in PENDING_STATES:
            continue
        assignment = state.assignments.get(bag.flight_id)
        if assignment is None:
            continue
        load[assignment.chute_id] = load.get(assignment.chute_id, 0) + 1
    return load


def _place(
    flights: list[AffectedFlight], candidates: list[CandidateLane], chute_id: str
) -> tuple[list[Move], list[str]]:
    """Worst-fit decreasing placement so no backup lane becomes the next hotspot.

    Flights are placed largest first into whichever lane currently has the
    most room; ties break towards the lighter lane, then the lower chute id,
    so the same state always yields the same plan. Flights no lane can absorb
    come back as the overflow list.
    """

    room: dict[str, float] = {
        lane.chute_id: float("inf") if lane.free is None else float(lane.free)
        for lane in candidates
    }
    load: dict[str, int] = {lane.chute_id: lane.load for lane in candidates}
    moves: list[Move] = []
    overflow: list[str] = []
    ordered = sorted(flights, key=lambda flight: (-flight.pending_bags, flight.flight_id))
    for flight in ordered:
        fitting = [
            lane for lane in candidates if room[lane.chute_id] >= flight.pending_bags
        ]
        if not fitting:
            overflow.append(flight.flight_id)
            continue
        chosen = min(
            fitting,
            key=lambda lane: (-room[lane.chute_id], load[lane.chute_id], lane.chute_id),
        )
        moves.append(
            Move(
                flight_id=flight.flight_id,
                from_chute=chute_id,
                from_generation=flight.generation,
                to_chute=chosen.chute_id,
                to_generation=flight.generation + 1,
                pending_bags=flight.pending_bags,
            )
        )
        if room[chosen.chute_id] != float("inf"):
            room[chosen.chute_id] -= flight.pending_bags
        load[chosen.chute_id] += flight.pending_bags
    return moves, overflow


def plan_impact(
    state: State, lanes: list[Lane], chute_id: str, at: str, watermark: int
) -> ImpactReport:
    """Compute the impact of degrading ``chute_id`` from one pass over state."""

    queued: dict[str, int] = {}
    pending: dict[str, int] = {}
    stranded: list[str] = []
    for bag in state.bags.values():
        if bag.state is BagState.INJECTED:
            queued[bag.flight_id] = queued.get(bag.flight_id, 0) + 1
        if bag.state in PENDING_STATES:
            pending[bag.flight_id] = pending.get(bag.flight_id, 0) + 1
        if bag.state is BagState.SORTED and bag.chute_id == chute_id:
            stranded.append(bag.id)

    flights: list[AffectedFlight] = []
    for flight_id, assignment in sorted(state.assignments.items()):
        if assignment.chute_id != chute_id:
            continue
        flight = state.flights.get(flight_id)
        flights.append(
            AffectedFlight(
                flight_id=flight_id,
                code=flight.code if flight else "",
                state=flight.state.value if flight else "",
                generation=assignment.generation,
                queued_bags=queued.get(flight_id, 0),
                pending_bags=pending.get(flight_id, 0),
            )
        )

    load_by_chute = _pending_by_chute(state)
    candidates: list[CandidateLane] = []
    for lane in sorted(lanes, key=lambda item: item.chute_id):
        if lane.chute_id == chute_id or not lane.enabled:
            continue
        load = load_by_chute.get(lane.chute_id, 0)
        free = None if lane.capacity == 0 else max(0, lane.capacity - load)
        candidates.append(
            CandidateLane(
                chute_id=lane.chute_id,
                carousel_id=lane.carousel_id,
                capacity=lane.capacity,
                load=load,
                free=free,
            )
        )

    moves, overflow = _place(flights, candidates, chute_id)
    return ImpactReport(
        chute_id=chute_id,
        generated_at=at,
        watermark=watermark,
        flights=tuple(flights),
        queued_total=sum(queued.get(flight.flight_id, 0) for flight in flights),
        stranded_sorted=tuple(sorted(stranded)),
        moves=tuple(moves),
        candidates=tuple(candidates),
        overflow=tuple(overflow),
    )
