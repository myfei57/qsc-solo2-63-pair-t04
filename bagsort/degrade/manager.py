"""Chute degradation and recovery.

One operator action (:meth:`DegradationManager.degrade`) computes the impact
plan, commits it and stamps the audit evidence, all in a single store session:
either every flight moves a generation and every affected bag has a ledger line,
or the transaction rolls back and the plant is left exactly as it was.

The chosen policy is **reroute queued bags in place**. A bag still in the
in-transit queue has not touched the dead chute, so it simply inherits the new
assignment generation and lands in the spare when the sorter reaches it; the
passenger never waits for a batch boundary and the bag is never sorted twice.
What that policy gives up is any rescue for bags that already reached the dead
chute: they cannot be moved in place, so they are pulled into recheck and make
one extra trip through the sorter. Bags no spare can hold are returned as an
explicit overflow (or, only on ``force``, diverted with an audit line); they
are never silently discarded.
"""

from __future__ import annotations

from bagsort.clock import Clock
from bagsort.errors import ConflictError, QuotaError, StaleError, ValidationError
from bagsort.ids import IdFactory
from bagsort.model.entities import ChuteAssignment, DegradationIncident, RerouteRecord
from bagsort.model.enums import BagState
from bagsort.chute.history import AssignmentHistory
from bagsort.ns.chutes import ChuteRegistry
from bagsort.store.snapshot import digest_for
from bagsort.store.store import Store
from .board import DegradationBoard
from .planner import (
    ImpactPlan,
    live_flights,
    plan_impact,
    queued_bag_ids,
    stranded_bag_ids,
)
from .reconcile import incident_reroutes, reconcile


class DegradationManager:
    """Opens incidents, commits reroutes and restores the original chutes."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        registry: ChuteRegistry,
        board: DegradationBoard,
        ids: IdFactory | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._registry = registry
        self._board = board
        self._ids = ids or IdFactory("inc")

    # -- read side ---------------------------------------------------------

    def plan(self, chute_id: str) -> ImpactPlan:
        self._require_known(chute_id)
        return plan_impact(self._store.state(), chute_id)

    def reconcile(self, incident_id: str):
        self._board.require(incident_id)
        return reconcile(self._store.state(), incident_id)

    def history(self) -> list:
        return self._board.all()

    def reroutes(self, incident_id: str) -> list[RerouteRecord]:
        self._board.require(incident_id)
        return incident_reroutes(self._store.state(), incident_id)

    # -- degradation -------------------------------------------------------

    def degrade(self, chute_id: str, force: bool = False) -> DegradationIncident:
        self._require_known(chute_id)
        if self._board.incident_for(chute_id) is not None:
            raise ConflictError(f"chute {chute_id!r} already has an active incident")
        state = self._store.state()
        plan = plan_impact(state, chute_id)
        if not plan.feasible and not force:
            short = [
                {
                    "flight_id": reroute.impact.flight_id,
                    "demand": reroute.impact.demand,
                    "headroom": None if reroute.spare is None else reroute.spare.headroom,
                }
                for reroute in plan.reroutes
                if not reroute.fits
            ]
            raise QuotaError(
                f"no spare chute can absorb {len(short)} flight(s) from {chute_id!r}; "
                f"re-run with force to divert explicitly: {short}"
            )

        incident_id = self._ids.next()
        before_watermark = self._store.watermark
        before_digest = digest_for(self._store.entries())
        at = self._clock.now_iso()
        generations = {
            reroute.impact.flight_id: reroute.impact.generation for reroute in plan.reroutes
        }
        incident = DegradationIncident(
            id=incident_id,
            chute_id=chute_id,
            status="active",
            affected_flights=tuple(plan.affected_flights()),
            generations=generations,
            before_watermark=before_watermark,
            before_digest=before_digest,
            opened_at=at,
        )

        queued_rerouted = 0
        overflowed = 0
        rescued = 0
        with self._store.session() as session:
            # Disable the lane first: the dead chute is unroutable the instant
            # this transaction becomes visible.
            lane = session.state.lanes.get(chute_id)
            if lane is not None:
                disabled_lane = lane.evolved(enabled=False)
                session.state.lanes[chute_id] = disabled_lane
                session.record("lane.put", {"lane": disabled_lane.as_dict()}, at)
            session.state.incidents[incident_id] = incident
            session.record("incident.put", {"incident": incident.as_dict()}, at)

            for reroute in plan.reroutes:
                flight_id = reroute.impact.flight_id
                if reroute.fits and reroute.spare is not None:
                    moved = self._reroute_queued(
                        session,
                        incident_id,
                        flight_id,
                        chute_id,
                        reroute.spare.chute_id,
                        reroute.impact.generation,
                        at,
                    )
                    queued_rerouted += moved
                else:
                    overflowed += self._divert_overflow(
                        session, incident_id, flight_id, chute_id, at
                    )

            stranded = [
                bag_id
                for reroute in plan.reroutes
                for bag_id in stranded_bag_ids(session.state, reroute.impact.flight_id, chute_id)
            ]
            for bag_id in stranded:
                bag = session.state.bags.get(bag_id)
                if bag is None:
                    continue
                updated = bag.evolved(state=BagState.RECHECKED)
                session.state.bags[bag_id] = updated
                if bag_id not in session.state.recheck_queue:
                    session.state.recheck_queue.append(bag_id)
                session.record("bag.put", {"bag": updated.as_dict()}, at)
                session.record("recheck.enqueue", {"bag_id": bag_id}, at)
                self._append_reroute(
                    session, incident_id, updated, chute_id, "",
                    bag.flight_id, 0, "stranded_rescue", at,
                )
                rescued += 1

            if queued_rerouted:
                session.record(
                    "counter.add",
                    {"name": "degrade.rerouted", "delta": queued_rerouted},
                    at,
                )
            if overflowed:
                session.record(
                    "counter.add",
                    {"name": "degrade.overflow", "delta": overflowed},
                    at,
                )
            if rescued:
                session.record(
                    "counter.add",
                    {"name": "degrade.rescued", "delta": rescued},
                    at,
                )
            session.record(
                "counter.add", {"name": "degrade.incidents", "delta": 1}, at
            )

        self._registry.sync_from_store()
        return self._stamp_after(incident_id)

    # -- recovery ----------------------------------------------------------

    def recover(self, incident_id: str) -> DegradationIncident:
        incident = self._board.require(incident_id)
        if incident.status != "active":
            raise ConflictError(f"incident {incident_id!r} is not active")
        chute_id = incident.chute_id
        at = self._clock.now_iso()
        open_flights = set(live_flights(self._store.state()))
        with self._store.session() as session:
            lane = session.state.lanes.get(chute_id)
            if lane is not None:
                enabled_lane = lane.evolved(enabled=True)
                session.state.lanes[chute_id] = enabled_lane
                session.record("lane.put", {"lane": enabled_lane.as_dict()}, at)
            # Re-point the still-live flights the incident moved. The generation
            # keeps climbing, so a stale degraded-era cache is refused.
            for flight_id in incident.affected_flights:
                if flight_id not in open_flights:
                    continue
                assignment = session.state.assignments.get(flight_id)
                if assignment is None or assignment.chute_id == chute_id:
                    continue
                restored = ChuteAssignment(
                    flight_id=flight_id,
                    chute_id=chute_id,
                    generation=assignment.generation + 1,
                    updated_at=at,
                )
                session.state.assignments[flight_id] = restored
                session.record("chute.assign", restored.as_dict(), at)
            recovered = incident.evolved(status="recovered", recovered_at=at)
            session.state.incidents[incident_id] = recovered
            session.record("incident.put", {"incident": recovered.as_dict()}, at)
            session.record(
                "counter.add", {"name": "degrade.recovered", "delta": 1}, at
            )
        self._registry.sync_from_store()
        return self._board.require(incident_id)

    def revalidate_reference(self, flight_id: str, generation: int) -> dict[str, object]:
        """Re-check a routing reference a client held across an outage.

        A generation that ever existed is historically valid; the answer says
        whether it is still current, and if not which recovered incident
        superseded it.
        """

        history = AssignmentHistory(self._store).for_flight(flight_id)
        known = {assignment.generation for assignment in history}
        if not known:
            raise ValidationError(f"flight {flight_id!r} has no chute assignment")
        if generation not in known:
            raise StaleError(
                f"generation {generation} was never assigned to {flight_id!r}"
            )
        current = self._store.state().assignments.get(flight_id)
        current_generation = current.generation if current is not None else 0
        superseding = [
            {
                "incident_id": incident.id,
                "chute_id": incident.chute_id,
                "status": incident.status,
            }
            for incident in self._store.state().incidents.values()
            if flight_id in incident.affected_flights and incident.status == "recovered"
        ]
        return {
            "flight_id": flight_id,
            "checked_generation": generation,
            "current_generation": current_generation,
            "current": generation == current_generation,
            "recovered_incidents": superseding,
        }

    # -- helpers -----------------------------------------------------------

    def _reroute_queued(
        self,
        session,
        incident_id: str,
        flight_id: str,
        dead_chute: str,
        spare_chute: str,
        from_generation: int,
        at: str,
    ) -> int:
        assignment = ChuteAssignment(
            flight_id=flight_id,
            chute_id=spare_chute,
            generation=from_generation + 1,
            updated_at=at,
        )
        session.state.assignments[flight_id] = assignment
        session.record("chute.assign", assignment.as_dict(), at)
        count = 0
        for bag_id in queued_bag_ids(session.state, flight_id):
            bag = session.state.bags[bag_id]
            self._append_reroute(
                session, incident_id, bag, dead_chute, spare_chute,
                flight_id, assignment.generation, "queued_in_place", at,
            )
            count += 1
        return count

    def _divert_overflow(
        self, session, incident_id: str, flight_id: str, dead_chute: str, at: str
    ) -> int:
        # No spare could hold this flight: divert the queued bags one by one
        # with an explicit ledger line instead of dropping them.
        count = 0
        for bag_id in queued_bag_ids(session.state, flight_id):
            bag = session.state.bags[bag_id]
            diverted = bag.evolved(state=BagState.DIVERTED)
            session.state.bags[bag_id] = diverted
            session.record("bag.put", {"bag": diverted.as_dict()}, at)
            self._append_reroute(
                session, incident_id, diverted, dead_chute, "",
                flight_id, 0, "overflow_manual", at,
            )
            count += 1
        return count

    def _append_reroute(
        self,
        session,
        incident_id: str,
        bag,
        from_chute: str,
        to_chute: str,
        flight_id: str,
        generation: int,
        kind: str,
        at: str,
    ) -> None:
        record = RerouteRecord(
            incident_id=incident_id,
            bag_id=bag.id,
            flight_id=flight_id or bag.flight_id,
            from_chute=from_chute,
            to_chute=to_chute,
            generation=generation,
            kind=kind,
            at=at,
        )
        session.state.reroutes.append(record)
        session.record("reroute.append", {"reroute": record.as_dict()}, at)

    def _stamp_after(self, incident_id: str) -> DegradationIncident:
        state = self._store.state()
        incident = state.incidents[incident_id]
        stamped = incident.evolved(
            after_watermark=self._store.watermark,
            after_digest=digest_for(self._store.entries()),
        )
        with self._store.session() as session:
            session.state.incidents[incident_id] = stamped
            session.record("incident.put", {"incident": stamped.as_dict()})
        return stamped

    def _require_known(self, chute_id: str) -> None:
        if chute_id not in self._store.state().lanes:
            raise ValidationError(f"chute {chute_id!r} is not a registered lane")
