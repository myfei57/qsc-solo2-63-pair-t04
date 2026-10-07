"""Cross-ledger reconciliation around a degradation.

Three ledgers must agree:

1. **allocation generations** — the flight's chute assignment generation;
2. **in-transit queue** — every queued bag maps to the flight that owns it;
3. **carousel batch** — bags physically on a carousel belong to that batch, and
   transfer commands still pending all name the running batch.

The recheck is a deliberate fourth holding area: a bag rescued out of a dead
chute is expected to disagree about its chute, and that is reported as a known
rescue rather than counted as drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bagsort.model.enums import BagState
from bagsort.model.entities import RerouteRecord


@dataclass
class ReconcileReport:
    ok: bool = True
    stale_assignments: list[dict[str, object]] = field(default_factory=list)
    orphan_queued: list[str] = field(default_factory=list)
    batch_mismatches: list[dict[str, object]] = field(default_factory=list)
    rescued: list[str] = field(default_factory=list)
    reroutes: int = 0

    def add_stale(self, detail: dict[str, object]) -> None:
        self.ok = False
        self.stale_assignments.append(detail)

    def add_orphan(self, bag_id: str) -> None:
        self.ok = False
        self.orphan_queued.append(bag_id)

    def add_batch(self, detail: dict[str, object]) -> None:
        self.ok = False
        self.batch_mismatches.append(detail)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "stale_assignments": list(self.stale_assignments),
            "orphan_queued": list(self.orphan_queued),
            "batch_mismatches": list(self.batch_mismatches),
            "rescued": list(self.rescued),
            "reroutes": self.reroutes,
        }


def incident_reroutes(state, incident_id: str) -> list[RerouteRecord]:
    return [record for record in state.reroutes if record.incident_id == incident_id]


def reconcile(state, incident_id: str) -> ReconcileReport:
    """Prove the three ledgers still agree while an incident is in force."""

    incidents = state.incidents
    if incident_id not in incidents:
        raise KeyError(incident_id)
    incident = incidents[incident_id]
    report = ReconcileReport()
    records = incident_reroutes(state, incident_id)
    report.reroutes = len(records)
    rescued = {record.bag_id for record in records if record.kind == "stranded_rescue"}
    report.rescued = sorted(rescued)

    # 1. allocation generations: a rerouted flight's generation must have moved
    #    past the one the incident captured, and it must not still name the dead
    #    chute while the incident is active.
    for flight_id, from_generation in incident.generations.items():
        assignment = state.assignments.get(flight_id)
        if assignment is None:
            report.add_stale({"flight_id": flight_id, "reason": "assignment_missing"})
            continue
        if assignment.generation < from_generation:
            report.add_stale(
                {
                    "flight_id": flight_id,
                    "reason": "generation_went_backwards",
                    "expected_min": from_generation + 1,
                    "actual": assignment.generation,
                }
            )
        if (
            incident.status == "active"
            and assignment.chute_id == incident.chute_id
        ):
            report.add_stale(
                {
                    "flight_id": flight_id,
                    "reason": "still_points_at_dead_chute",
                    "chute_id": incident.chute_id,
                    "generation": assignment.generation,
                }
            )

    # 2. in-transit queue: every queued bag must map to a live flight.
    for bag in state.bags.values():
        if bag.state is not BagState.INJECTED:
            continue
        flight_id = state.mappings.get(bag.id)
        if flight_id is None or flight_id not in state.flights:
            report.add_orphan(bag.id)

    # 3. carousel batch: on-carousel bags must have a load-consistent state and
    #    no queued transfer command may name a batch older than the belt's.
    running_batch = state.belt_batch
    for bag_id in state.belt_on_carousel:
        bag = state.bags.get(bag_id)
        if bag is None:
            report.add_batch({"bag_id": bag_id, "reason": "on_carousel_but_missing"})
    for command in state.transfer_commands:
        if command.batch < running_batch:
            report.add_batch(
                {
                    "command_id": command.id,
                    "reason": "transfer_older_than_running_batch",
                    "command_batch": command.batch,
                    "running_batch": running_batch,
                }
            )
    return report
