"""Pure impact and capacity planning for a chute outage.

The planner never writes. It reads a committed state snapshot and says, for every
flight that currently points at the dead chute:

* how many of its bags are still in the in-transit queue (those can inherit the
  new chute in place, with zero physical re-handling),
* how many are already sitting sorted in the dead chute (those are stranded and
  must be rescued through recheck),
* which enabled spare chute can take the queue, preferring a spare feeding the
  same carousel (so the carousel batch does not split),
* whether the chosen spare actually has the headroom.

A bag no spare can hold is returned as an explicit overflow; it is never dropped
silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bagsort.model.enums import BagState, FlightState


@dataclass(frozen=True)
class FlightImpact:
    flight_id: str
    code: str
    carousel: str
    current_chute: str
    generation: int
    queued: int
    stranded: int
    cutoff_at: str

    @property
    def demand(self) -> int:
        """Bags that still need a live chute to land in."""

        return self.queued

    def as_dict(self) -> dict[str, object]:
        return {
            "flight_id": self.flight_id,
            "code": self.code,
            "carousel": self.carousel,
            "current_chute": self.current_chute,
            "generation": self.generation,
            "queued": self.queued,
            "stranded": self.stranded,
            "demand": self.demand,
            "cutoff_at": self.cutoff_at,
        }


@dataclass(frozen=True)
class SpareOffer:
    chute_id: str
    carousel_id: str
    headroom: int
    same_carousel: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "chute_id": self.chute_id,
            "carousel_id": self.carousel_id,
            "headroom": self.headroom,
            "same_carousel": self.same_carousel,
        }


@dataclass
class PlannedReroute:
    impact: FlightImpact
    spare: SpareOffer | None
    fits: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "flight": self.impact.as_dict(),
            "spare": None if self.spare is None else self.spare.as_dict(),
            "fits": self.fits,
        }


@dataclass
class ImpactPlan:
    dead_chute: str
    impacts: list[FlightImpact] = field(default_factory=list)
    reroutes: list[PlannedReroute] = field(default_factory=list)
    overflow: list[str] = field(default_factory=list)
    stranded: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.overflow

    def affected_flights(self) -> list[str]:
        return [impact.flight_id for impact in self.impacts]

    def as_dict(self) -> dict[str, object]:
        return {
            "dead_chute": self.dead_chute,
            "feasible": self.feasible,
            "affected_flights": self.affected_flights(),
            "impacts": [impact.as_dict() for impact in self.impacts],
            "reroutes": [reroute.as_dict() for reroute in self.reroutes],
            "overflow": list(self.overflow),
            "stranded": list(self.stranded),
        }


def _queued_bags(state, flight_id: str) -> list:
    return [
        bag
        for bag in state.bags.values()
        if bag.flight_id == flight_id and bag.state is BagState.INJECTED
    ]


def _stranded_bags(state, flight_id: str, chute_id: str) -> list:
    return [
        bag
        for bag in state.bags.values()
        if bag.flight_id == flight_id
        and bag.chute_id == chute_id
        and bag.state is BagState.SORTED
    ]


def impact_for_flight(state, flight_id: str, dead_chute: str) -> FlightImpact | None:
    flight = state.flights.get(flight_id)
    assignment = state.assignments.get(flight_id)
    if flight is None or assignment is None or assignment.chute_id != dead_chute:
        return None
    queued = _queued_bags(state, flight_id)
    stranded = _stranded_bags(state, flight_id, dead_chute)
    return FlightImpact(
        flight_id=flight_id,
        code=flight.code,
        carousel=flight.carousel,
        current_chute=dead_chute,
        generation=assignment.generation,
        queued=len(queued),
        stranded=len(stranded),
        cutoff_at=flight.cutoff_at,
    )


def queued_bag_ids(state, flight_id: str) -> list[str]:
    queued = _queued_bags(state, flight_id)
    queued.sort(key=lambda bag: (bag.epoch, bag.sequence, bag.id))
    return [bag.id for bag in queued]


def stranded_bag_ids(state, flight_id: str, dead_chute: str) -> list[str]:
    stranded = _stranded_bags(state, flight_id, dead_chute)
    stranded.sort(key=lambda bag: (bag.epoch, bag.sequence, bag.id))
    return [bag.id for bag in stranded]


def spare_offers(state, dead_chute: str, carousel: str) -> list[SpareOffer]:
    offers = []
    for lane in state.lanes.values():
        if not lane.enabled or lane.chute_id == dead_chute:
            continue
        # A chute already claimed by another active incident is not a spare.
        if any(
            incident.chute_id == lane.chute_id and incident.status == "active"
            for incident in state.incidents.values()
        ):
            continue
        occupied = sum(
            1
            for bag in state.bags.values()
            if bag.chute_id == lane.chute_id
            and bag.chute_id
            and bag.state.active
        )
        offers.append(
            SpareOffer(
                chute_id=lane.chute_id,
                carousel_id=lane.carousel_id,
                headroom=max(0, lane.capacity - occupied),
                same_carousel=bool(carousel and lane.carousel_id == carousel),
            )
        )
    offers.sort(key=lambda offer: (not offer.same_carousel, -offer.headroom, offer.chute_id))
    return offers


def _urgency(impact: FlightImpact) -> tuple:
    # Earliest cutoff first (empty cutoff sorts last), then the longer queue so
    # the hardest flight claims capacity before an easy one can fragment it.
    return (impact.cutoff_at == "", impact.cutoff_at, -impact.queued, impact.flight_id)


def plan_impact(state, dead_chute: str) -> ImpactPlan:
    """Compute who must move and whether the live chutes can absorb them."""

    plan = ImpactPlan(dead_chute=dead_chute)
    impacts = [
        impact
        for flight_id in state.assignments
        if (impact := impact_for_flight(state, flight_id, dead_chute)) is not None
    ]
    impacts.sort(key=_urgency)
    plan.impacts = impacts

    # Committed headroom shrinks as flights are placed on a shared spare.
    reserved: dict[str, int] = {}
    for impact in impacts:
        if impact.stranded:
            plan.stranded.extend(stranded_bag_ids(state, impact.flight_id, dead_chute))
        offers = spare_offers(state, dead_chute, impact.carousel)
        placed: PlannedReroute | None = None
        for offer in offers:
            used = reserved.get(offer.chute_id, 0)
            if offer.headroom - used >= impact.demand:
                spare = SpareOffer(
                    chute_id=offer.chute_id,
                    carousel_id=offer.carousel_id,
                    headroom=offer.headroom - used,
                    same_carousel=offer.same_carousel,
                )
                placed = PlannedReroute(impact=impact, spare=spare, fits=True)
                reserved[offer.chute_id] = used + impact.demand
                break
        if placed is None:
            best = offers[0] if offers else None
            spare = None
            if best is not None:
                spare = SpareOffer(
                    chute_id=best.chute_id,
                    carousel_id=best.carousel_id,
                    headroom=max(0, best.headroom - reserved.get(best.chute_id, 0)),
                    same_carousel=best.same_carousel,
                )
            placed = PlannedReroute(impact=impact, spare=spare, fits=False)
            if impact.demand:
                plan.overflow.append(impact.flight_id)
        plan.reroutes.append(placed)
    return plan


def live_flights(state) -> list[str]:
    """Flights still accepting baggage, the only ones a recovery re-points."""

    open_states = {FlightState.OPEN, FlightState.BOARDING}
    return [
        flight_id
        for flight_id, flight in state.flights.items()
        if flight.state in open_states
    ]
