"""Degradation manager: reroute flights off a dead chute in one commit.

When a chute stops serving, the queued bags are re-dropped *in place*: the
manager bumps every affected flight to a fresh assignment generation inside a
single commit, and because the sorter resolves the chute from the current
assignment at dispatch time, the whole in-transit queue follows the new
generation without one journal entry per bag. The alternative — holding the
queue until the running carousel batch drains and flipping at the boundary —
was rejected: against a dead chute the hold buys no sorting cleanliness (the
bags already sorted stay sorted) and costs unbounded passenger wait at peak.
The price this choice pays is visible in the record: bags that already
dropped to the dead chute (``stranded_sorted``) must be pulled back and
sorted twice, and one carousel batch may now hold bags dropped to two chutes,
so reconciliation runs by generation instead of by batch.

Recovery never moves flights back. Generations only move forward, so a
reference captured before the degradation stays stale after recovery and can
be re-validated against the journaled lineage at any time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bagsort.chute.assigner import ChuteAssigner
from bagsort.clock import Clock
from bagsort.errors import ConflictError, NotFoundError, QuotaError, ValidationError
from bagsort.flight.book import FlightBook
from bagsort.ids import IdFactory
from bagsort.model.entities import ChuteAssignment, DegradationRecord, Placement
from bagsort.model.enums import BagState, DegradationStatus
from bagsort.ns.chutes import ChuteRegistry
from bagsort.store.store import Store
from .plan import ImpactReport, plan_impact

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Finding:
    """One inconsistency spotted while cross-checking the three states."""

    code: str
    severity: str
    subject: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "detail": dict(self.detail),
        }


class DegradationManager:
    """Plans, commits and reconciles chute degradations."""

    def __init__(
        self,
        store: Store,
        chutes: ChuteAssigner,
        registry: ChuteRegistry,
        book: FlightBook,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._store = store
        self._chutes = chutes
        self._registry = registry
        self._book = book
        self._clock = clock
        self._ids = ids

    # -- planning ----------------------------------------------------------

    def plan(self, chute_id: str) -> ImpactReport:
        """Preview the impact of degrading ``chute_id``; writes nothing."""

        self._registry.get(chute_id)
        self._ensure_not_degraded(chute_id)
        return plan_impact(
            self._store.state(),
            list(self._registry.lanes.values()),
            chute_id,
            self._clock.now_iso(),
            self._store.watermark,
        )

    # -- execution ---------------------------------------------------------

    def degrade(self, chute_id: str, strict: bool = False) -> DegradationRecord:
        """Reroute every flight off ``chute_id`` in a single commit.

        Flights no backup lane can absorb stay assigned to the dead chute and
        are carried on the record's overflow list — visible to reconciliation
        until recovered, never silently dropped. With ``strict`` the whole
        degradation is refused instead when anyone would overflow.
        """

        report = self.plan(chute_id)
        if strict and not report.absorbable:
            raise QuotaError(
                f"no backup chute can absorb {len(report.overflow)} flight(s) "
                f"off {chute_id!r}: {', '.join(report.overflow)}"
            )
        at = self._clock.now_iso()
        watermark_before = self._store.watermark
        with self._store.session() as session:
            placements: list[Placement] = []
            for move in report.moves:
                previous = session.state.assignments.get(move.flight_id)
                from_generation = previous.generation if previous else 0
                assignment = ChuteAssignment(
                    flight_id=move.flight_id,
                    chute_id=move.to_chute,
                    generation=from_generation + 1,
                    updated_at=at,
                )
                session.state.assignments[move.flight_id] = assignment
                session.record("chute.assign", assignment.as_dict(), at)
                placements.append(
                    Placement(
                        flight_id=move.flight_id,
                        from_chute=chute_id,
                        from_generation=from_generation,
                        to_chute=move.to_chute,
                        to_generation=assignment.generation,
                        pending_bags=move.pending_bags,
                    )
                )
            record = DegradationRecord(
                id=self._ids.next(),
                chute_id=chute_id,
                status=DegradationStatus.ACTIVE,
                placements=tuple(placements),
                overflow=report.overflow,
                stranded_sorted=report.stranded_sorted,
                queued_total=report.queued_total,
                watermark_before=watermark_before,
                created_at=at,
            )
            session.state.degradations[record.id] = record
            session.record("degrade.open", {"record": record.as_dict()}, at)
            session.record("counter.add", {"name": "degrade.opened", "delta": 1}, at)
        self._registry.disable(chute_id)
        return record

    def recover(self, chute_id: str) -> DegradationRecord:
        """Close the active degradation on ``chute_id``.

        Assignments are left exactly where the degradation put them: bringing
        the chute back must not resurrect pre-degradation generations, so any
        move back is an explicit, generation-bumping reassignment.
        """

        self._registry.get(chute_id)
        record = self._active_for(chute_id)
        if record is None:
            raise ConflictError(f"chute {chute_id!r} has no active degradation")
        at = self._clock.now_iso()
        updated = record.evolved(
            status=DegradationStatus.RECOVERED, recovered_at=at
        )
        with self._store.session() as session:
            session.state.degradations[updated.id] = updated
            session.record("degrade.recover", {"record": updated.as_dict()}, at)
            session.record("counter.add", {"name": "degrade.recovered", "delta": 1}, at)
        self._registry.enable(chute_id)
        return updated

    # -- inspection ----------------------------------------------------------

    def get(self, degradation_id: str) -> DegradationRecord:
        record = self._store.state().degradations.get(degradation_id)
        if record is None:
            raise NotFoundError(f"degradation {degradation_id!r} is not recorded")
        return record

    def history(self) -> list[DegradationRecord]:
        records = self._store.state().degradations
        return [records[key] for key in sorted(records)]

    def active(self) -> list[DegradationRecord]:
        return [
            record
            for record in self.history()
            if record.status is DegradationStatus.ACTIVE
        ]

    def validate_reference(self, flight_id: str, generation: int) -> dict[str, Any]:
        """Re-check a captured generation against the journaled lineage.

        This is the non-throwing counterpart of ``ensure_current``: a caller
        holding a pre-degradation reference gets a definitive verdict and the
        degradations that invalidated it, before and after recovery.
        """

        if generation < 1:
            raise ValidationError("generation must be a positive integer")
        self._book.get(flight_id)
        assignment = self._chutes.current(flight_id)
        if assignment is None:
            return {
                "flight_id": flight_id,
                "generation": generation,
                "verdict": "unassigned",
                "current_generation": 0,
                "current_chute": "",
                "invalidated_by": [],
            }
        if generation == assignment.generation:
            verdict = "current"
        elif generation < assignment.generation:
            verdict = "stale"
        else:
            verdict = "unknown"
        invalidated_by = [
            record.id
            for record in self.history()
            if any(
                placement.flight_id == flight_id
                and placement.from_generation == generation
                for placement in record.placements
            )
        ]
        return {
            "flight_id": flight_id,
            "generation": generation,
            "verdict": verdict,
            "current_generation": assignment.generation,
            "current_chute": assignment.chute_id,
            "invalidated_by": invalidated_by,
        }

    # -- reconciliation ------------------------------------------------------

    def reconcile(self) -> list[Finding]:
        """Cross-check assignments, the in-transit queue and the belt batch.

        An empty result means the three states agree; every finding names the
        state that drifted so the operator can act on it directly.
        """

        state = self._store.state()
        findings: list[Finding] = []
        actives = self.active()
        degraded = {record.chute_id: record for record in actives}

        for record in actives:
            placed = {item.flight_id: item for item in record.placements}
            for flight_id, assignment in sorted(state.assignments.items()):
                if assignment.chute_id != record.chute_id:
                    continue
                if flight_id not in placed and flight_id not in record.overflow:
                    findings.append(
                        Finding(
                            code="routed_to_degraded_chute",
                            severity="error",
                            subject=flight_id,
                            detail={"chute_id": assignment.chute_id},
                        )
                    )
            for flight_id, placement in placed.items():
                current = state.assignments.get(flight_id)
                if current is not None and current.generation > placement.to_generation:
                    findings.append(
                        Finding(
                            code="degradation_superseded",
                            severity="info",
                            subject=flight_id,
                            detail={
                                "chute_id": current.chute_id,
                                "generation": current.generation,
                                "degradation_generation": placement.to_generation,
                            },
                        )
                    )
            if record.overflow:
                findings.append(
                    Finding(
                        code="overflow_unresolved",
                        severity="warning",
                        subject=record.chute_id,
                        detail={
                            "degradation_id": record.id,
                            "flights": list(record.overflow),
                        },
                    )
                )

        for bag in state.bags.values():
            if bag.state is BagState.INJECTED and bag.flight_id not in state.assignments:
                findings.append(
                    Finding(
                        code="in_transit_without_assignment",
                        severity="error",
                        subject=bag.id,
                        detail={"flight_id": bag.flight_id},
                    )
                )
            if bag.state is BagState.SORTED and bag.chute_id in degraded:
                findings.append(
                    Finding(
                        code="sorted_to_degraded_chute",
                        severity="warning",
                        subject=bag.id,
                        detail={"chute_id": bag.chute_id},
                    )
                )

        stale_transfers = [
            command.id
            for command in state.transfer_commands
            if command.batch < state.belt_batch
        ]
        if stale_transfers:
            findings.append(
                Finding(
                    code="transfer_behind_batch",
                    severity="warning",
                    subject=state.belt_carousel or "belt",
                    detail={"batch": state.belt_batch, "commands": stale_transfers},
                )
            )
        for bag_id in state.belt_on_carousel:
            if bag_id not in state.bags:
                findings.append(
                    Finding(
                        code="carousel_bag_missing",
                        severity="error",
                        subject=bag_id,
                        detail={"carousel": state.belt_carousel},
                    )
                )

        return sorted(
            findings,
            key=lambda finding: (
                _SEVERITY_ORDER.get(finding.severity, 3),
                finding.code,
                finding.subject,
            ),
        )

    # -- internals -----------------------------------------------------------

    def _active_for(self, chute_id: str) -> DegradationRecord | None:
        for record in self.active():
            if record.chute_id == chute_id:
                return record
        return None

    def _ensure_not_degraded(self, chute_id: str) -> None:
        if self._active_for(chute_id) is not None:
            raise ConflictError(
                f"chute {chute_id!r} already has an active degradation; "
                "recover it before degrading again"
            )
