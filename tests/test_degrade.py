"""Chute degradation: impact planning, reroute, recovery and reconciliation."""

from __future__ import annotations

import pytest

from bagsort.errors import ConflictError, NotFoundError, QuotaError, StaleError
from bagsort.model.enums import BagState, DegradationStatus
from bagsort.store.store import Store
from bagsort.app import Plant
from tests.support import check_in, make_plant, register_flight


def _lanes(plant, *capacities: tuple[str, int]) -> None:
    for code, capacity in capacities:
        plant.registry.register(f"T1-CH{code}", "T1-CRCR1", capacity=capacity)


def _flight_on(plant, code: str, chute: str, bags: int = 0):
    flight = register_flight(plant, code=code)
    plant.chutes.assign(flight.id, chute)
    if bags:
        check_in(plant, flight.id, bags, prefix=f"B{code}")
    return flight


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def test_plan_previews_impact_without_writing(plant):
    _lanes(plant, ("A", 0), ("B", 10), ("C", 10))
    first = _flight_on(plant, "CA1", "T1-CHA", bags=3)
    _flight_on(plant, "CA2", "T1-CHA", bags=2)
    watermark = plant.store.watermark

    report = plant.degradation.plan("T1-CHA")

    assert plant.store.watermark == watermark
    assert [flight.flight_id for flight in report.flights] == [first.id, "T1-CA2"]
    assert report.queued_total == 5
    assert report.absorbable is True
    assert report.overflow == ()
    assert len(report.moves) == 2
    assert {move.to_chute for move in report.moves} <= {"T1-CHB", "T1-CHC"}
    candidates = {lane.chute_id: lane for lane in report.candidates}
    assert "T1-CHA" not in candidates
    assert candidates["T1-CHB"].free == 10


def test_plan_counts_bags_already_sorted_to_the_dead_chute(plant, flight):
    _lanes(plant, ("A", 0), ("B", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    bag = check_in(plant, flight.id, 1)[0]
    plant.retry_bag(bag.id)  # sorted onto T1-CHA before it died

    report = plant.degradation.plan("T1-CHA")

    assert report.stranded_sorted == (bag.id,)
    assert report.queued_total == 0


def test_plan_refuses_an_unknown_or_already_degraded_chute(plant, flight):
    _lanes(plant, ("A", 0), ("B", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    with pytest.raises(NotFoundError):
        plant.degradation.plan("T1-CHZ")
    plant.degrade_chute("T1-CHA")
    with pytest.raises(ConflictError):
        plant.degradation.plan("T1-CHA")


# ---------------------------------------------------------------------------
# execution: the in-place re-drop
# ---------------------------------------------------------------------------


def test_degrade_reroutes_flights_and_redrops_the_queue(plant):
    _lanes(plant, ("A", 0), ("B", 0))
    first = _flight_on(plant, "CA1", "T1-CHA", bags=3)
    second = _flight_on(plant, "CA2", "T1-CHA", bags=2)

    record = plant.degrade_chute("T1-CHA")

    assert record.status is DegradationStatus.ACTIVE
    assert len(record.placements) == 2
    assert record.queued_total == 5
    assert plant.chutes.require(first.id).generation == 2
    assert plant.chutes.require(second.id).generation == 2
    # The queued bags follow the new generation without a single per-bag write.
    for bag in plant.book.list_bags_for(first.id):
        assert plant.sorter.route(bag.id).chute_id == "T1-CHB"
    # And the next dispatch physically lands on the backup chute.
    queued = plant.injector.queue(first.id)
    plant.retry_bag(queued[0].id)
    assert plant.sorter.log.last_chute(queued[0].id) == "T1-CHB"
    assert plant.registry.validate("T1-CHA") is False


def test_degrade_commit_size_is_independent_of_queue_depth(plant):
    _lanes(plant, ("A", 0), ("B", 0))
    flight = _flight_on(plant, "CA1", "T1-CHA", bags=200)
    watermark = plant.store.watermark

    plant.degrade_chute("T1-CHA")

    # One chute.assign per flight plus degrade.open, the counter and the audit
    # entry: the reroute does not grow with the 200 queued bags.
    assert plant.store.watermark - watermark == 4
    assert plant.degradation.active()[0].queued_total == 200
    assert plant.sorter.route(plant.injector.queue(flight.id)[0].id).chute_id == "T1-CHB"


def test_degrade_without_backup_lanes_overflows_everything(plant, flight):
    _lanes(plant, ("A", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    check_in(plant, flight.id, 2)

    record = plant.degrade_chute("T1-CHA")

    assert record.placements == ()
    assert record.overflow == (flight.id,)
    # Nothing is dropped or diverted: the flight keeps its assignment and the
    # bags stay queued, visible to reconciliation until the operator acts.
    assert plant.chutes.require(flight.id).chute_id == "T1-CHA"
    assert plant.injector.in_transit(flight.id) == 2
    codes = {finding.code for finding in plant.degradation.reconcile()}
    assert "overflow_unresolved" in codes


def test_overflow_is_split_between_what_fits_and_what_does_not(plant):
    _lanes(plant, ("A", 0), ("B", 3))
    small = _flight_on(plant, "CA1", "T1-CHA", bags=2)
    big = _flight_on(plant, "CA2", "T1-CHA", bags=5)

    record = plant.degrade_chute("T1-CHA")

    assert record.overflow == (big.id,)
    assert [placement.flight_id for placement in record.placements] == [small.id]
    assert plant.chutes.require(small.id).chute_id == "T1-CHB"
    assert plant.chutes.require(big.id).chute_id == "T1-CHA"


def test_strict_degrade_refuses_atomically_when_anyone_overflows(plant):
    _lanes(plant, ("A", 0), ("B", 1))
    _flight_on(plant, "CA1", "T1-CHA", bags=2)
    watermark = plant.store.watermark

    with pytest.raises(QuotaError):
        plant.degrade_chute("T1-CHA", strict=True)

    assert plant.store.watermark == watermark
    assert plant.degradation.history() == []
    assert plant.chutes.require("T1-CA1").chute_id == "T1-CHA"
    assert plant.registry.validate("T1-CHA") is True


# ---------------------------------------------------------------------------
# references across the degradation and the recovery
# ---------------------------------------------------------------------------


def test_stale_reference_is_refused_and_revalidatable(plant, flight):
    _lanes(plant, ("A", 0), ("B", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    record = plant.degrade_chute("T1-CHA")

    with pytest.raises(StaleError):
        plant.chutes.ensure_current(flight.id, 1)

    verdict = plant.degradation.validate_reference(flight.id, 1)
    assert verdict["verdict"] == "stale"
    assert verdict["current_generation"] == 2
    assert verdict["current_chute"] == "T1-CHB"
    assert verdict["invalidated_by"] == [record.id]
    assert plant.degradation.validate_reference(flight.id, 2)["verdict"] == "current"
    assert plant.degradation.validate_reference(flight.id, 9)["verdict"] == "unknown"


def test_recovery_keeps_generations_and_reenables_the_lane(plant, flight):
    _lanes(plant, ("A", 0), ("B", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    plant.degrade_chute("T1-CHA")

    record = plant.recover_chute("T1-CHA")

    assert record.status is DegradationStatus.RECOVERED
    assert record.recovered_at
    assert plant.registry.validate("T1-CHA") is True
    # Recovery is not a rollback: the flight stays on the backup chute and the
    # pre-degradation reference stays stale afterwards.
    assert plant.chutes.require(flight.id).chute_id == "T1-CHB"
    assert plant.degradation.validate_reference(flight.id, 1)["verdict"] == "stale"
    assert plant.degradation.active() == []
    assert plant.degradation.reconcile() == []


def test_overflow_flight_heals_when_the_chute_recovers(plant, flight):
    _lanes(plant, ("A", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    check_in(plant, flight.id, 2)
    plant.degrade_chute("T1-CHA")
    assert plant.degradation.reconcile()  # overflow is visible while degraded

    plant.recover_chute("T1-CHA")

    # The flight never moved, so recovery makes it whole with zero writes.
    assert plant.chutes.require(flight.id).generation == 1
    assert plant.degradation.reconcile() == []


def test_recover_without_an_active_degradation_is_refused(plant):
    _lanes(plant, ("A", 0), ("B", 0))
    with pytest.raises(ConflictError):
        plant.recover_chute("T1-CHA")
    with pytest.raises(NotFoundError):
        plant.recover_chute("T1-CHZ")


# ---------------------------------------------------------------------------
# reconciliation across the three states
# ---------------------------------------------------------------------------


def test_reconcile_flags_a_flight_rerouted_onto_the_dead_chute(plant):
    _lanes(plant, ("A", 0), ("B", 0))
    _flight_on(plant, "CA1", "T1-CHA")
    plant.degrade_chute("T1-CHA")
    late = _flight_on(plant, "CA9", "T1-CHA")  # operator error after the fact

    findings = {
        finding.code: finding for finding in plant.degradation.reconcile()
    }
    assert findings["routed_to_degraded_chute"].subject == late.id


def test_reconcile_flags_a_superseded_placement(plant, flight):
    _lanes(plant, ("A", 0), ("B", 0), ("C", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    plant.degrade_chute("T1-CHA")
    plant.chutes.assign(flight.id, "T1-CHC")  # manual move during the outage

    codes = {finding.code for finding in plant.degradation.reconcile()}
    assert "degradation_superseded" in codes


def test_reconcile_flags_stranded_sorted_bags(plant, flight):
    _lanes(plant, ("A", 0), ("B", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    bag = check_in(plant, flight.id, 1)[0]
    plant.retry_bag(bag.id)
    plant.degrade_chute("T1-CHA")

    findings = {
        finding.code: finding for finding in plant.degradation.reconcile()
    }
    assert findings["sorted_to_degraded_chute"].subject == bag.id


def test_reconcile_cross_checks_queue_and_belt(plant, flight):
    check_in(plant, flight.id, 1)  # injected but never assigned a chute
    plant.belt.set("T1-CRCR1", 2)
    plant.belt.append_transfer(flight.id, "T1-CRCR1", 1)

    codes = {finding.code for finding in plant.degradation.reconcile()}
    assert "in_transit_without_assignment" in codes
    assert "transfer_behind_batch" in codes


# ---------------------------------------------------------------------------
# persistence and audit
# ---------------------------------------------------------------------------


def test_degradation_survives_a_restart(plant, flight):
    _lanes(plant, ("A", 0), ("B", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    check_in(plant, flight.id, 3)
    plant.degrade_chute("T1-CHA")
    plant.store.persist()

    reopened = Plant.build(Store.open(plant.store.path), clock=plant.clock)

    active = reopened.degradation.active()
    assert len(active) == 1
    assert active[0].placements[0].to_chute == "T1-CHB"
    assert reopened.degradation.reconcile() == []
    assert reopened.audit.verify() is True
    assert reopened.store.state().as_dict() == plant.store.state().as_dict()
    # Lanes are runtime config; registering them again re-applies the
    # journaled degradation to the lane flag.
    reopened.register_lane("T1-CHA", "T1-CRCR1")
    reopened.register_lane("T1-CHB", "T1-CRCR1")
    assert reopened.registry.validate("T1-CHA") is False
    assert reopened.registry.validate("T1-CHB") is True
    # A second degradation gets a fresh id, not a reused one.
    reopened.degrade_chute("T1-CHB")
    assert [record.id for record in reopened.degradation.history()] == [
        "deg-000001",
        "deg-000002",
    ]


def test_audit_trail_records_the_degradation(plant, flight):
    _lanes(plant, ("A", 0), ("B", 0))
    plant.chutes.assign(flight.id, "T1-CHA")
    plant.degrade_chute("T1-CHA")
    plant.recover_chute("T1-CHA")

    kinds = [entry.kind for entry in plant.audit.for_subject("T1-CHA")]
    assert kinds == ["chute_degraded", "chute_recovered"]
    assert plant.audit.verify() is True
    stages = {entry.detail["stage"] for entry in plant.audit.for_subject("T1-CHA")}
    assert stages == {"chute"}


# ---------------------------------------------------------------------------
# console
# ---------------------------------------------------------------------------


def test_console_degradation_flow(server):
    def call(method, path, payload=None):
        import json

        response = server.call(method, path, payload)
        return response.status, json.loads(response.body)

    for code, capacity in (("A", 0), ("B", 10)):
        status, _ = call(
            "POST", "/api/chutes", {"chute_id": f"T1-CH{code}", "carousel_id": "T1-CRCR1", "capacity": capacity}
        )
        assert status == 201
    status, flight = call("POST", "/api/flights", {"code": "CA1", "chute": "A"})
    assert status == 201
    flight_id = flight["id"]
    call("POST", "/api/bags/checkin", {"barcode": "B1", "flight_id": flight_id})

    status, plan = call("POST", "/api/chutes/degrade/plan", {"chute_id": "T1-CHA"})
    assert status == 200
    assert plan["queued_total"] == 1 and plan["absorbable"] is True
    assert call("GET", "/api/degradations")[1] == []

    status, record = call("POST", "/api/chutes/degrade", {"chute_id": "T1-CHA"})
    assert status == 200
    assert record["status"] == "active"
    assert record["placements"][0]["to_chute"] == "T1-CHB"

    status, active = call("GET", "/api/degradations/active")
    assert [item["id"] for item in active] == [record["id"]]
    status, fetched = call("GET", f"/api/degradations/{record['id']}")
    assert fetched["chute_id"] == "T1-CHA"
    assert call("GET", "/api/degradations/deg-999999")[0] == 404

    status, verdict = call(
        "POST", f"/api/assignments/{flight_id}/validate", {"generation": 1}
    )
    assert verdict["verdict"] == "stale"
    assert verdict["invalidated_by"] == [record["id"]]

    status, reconciliation = call("GET", "/api/degradations/reconcile")
    assert reconciliation == {"ok": True, "findings": []}

    status, recovered = call("POST", "/api/chutes/recover", {"chute_id": "T1-CHA"})
    assert recovered["status"] == "recovered"
    assert call("GET", "/api/degradations/active")[1] == []
    assert call("POST", "/api/chutes/recover", {"chute_id": "T1-CHA"})[0] == 409
    assert call("POST", "/api/chutes/degrade", {"chute_id": "T1-GHOST"})[0] == 404
