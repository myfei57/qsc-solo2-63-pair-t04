"""Chute degradation: impact planning, in-place reroute, recovery and audit."""

from __future__ import annotations

import json

import pytest

from bagsort.errors import ConflictError, QuotaError, StaleError, ValidationError
from bagsort.model.entities import Reading
from bagsort.ns.chutes import DEFAULT_CAPACITY
from tests.support import register_flight


def _lane(plant, code: str, carousel: str = "CR1", capacity: int = DEFAULT_CAPACITY):
    return plant.registry.register(
        plant.namespace.chute_id(code),
        plant.namespace.carousel_id(carousel),
        True,
        capacity,
    )


def _setup(plant, flight_code="CA1234", dead="A", spare=("B",), queued=3, stranded=1):
    _lane(plant, dead, "CR1")
    for code in spare:
        _lane(plant, code, "CR1")
    flight = register_flight(plant, code=flight_code, carousel="CR1")
    dead_id = plant.namespace.chute_id(dead)
    plant.assign_chute(flight.id, dead_id)
    bags = [plant.check_in(f"B{index:03d}", flight.id) for index in range(queued + stranded)]
    for bag in bags[:stranded]:
        plant.push_bag(bag.id, dead_id)
    return flight, dead_id, [plant.namespace.chute_id(code) for code in spare], bags


def test_plan_reports_queued_stranded_and_a_fitting_spare(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=3, stranded=2)
    plan = plant.plan_degradation(dead_id)
    assert plan.feasible is True
    impact = plan.impacts[0]
    assert impact.flight_id == flight.id
    assert impact.queued == 3
    assert impact.stranded == 2
    assert plan.reroutes[0].spare.chute_id == spare_ids[0]
    assert plan.stranded == [bags[0].id, bags[1].id]


def test_degrade_reroutes_queued_bags_in_place(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=3, stranded=1)
    before_generation = plant.chutes.current(flight.id).generation
    incident = plant.degrade_chute(dead_id)
    state = plant.store.state()
    assignment = state.assignments[flight.id]
    assert assignment.chute_id == spare_ids[0]
    assert assignment.generation == before_generation + 1
    queued_lines = [r for r in state.reroutes if r.kind == "queued_in_place"]
    assert {r.bag_id for r in queued_lines} == {bag.id for bag in bags[1:]}
    assert all(r.to_chute == spare_ids[0] for r in queued_lines)
    assert all(r.generation == assignment.generation for r in queued_lines)
    # queued bags never moved twice: still injected, no sort record to the dead
    # chute beyond the one stranded bag.
    assert state.bags[bags[1].id].state.value == "injected"


def test_degrade_rescues_bags_already_in_the_dead_chute_via_recheck(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=2, stranded=2)
    incident = plant.degrade_chute(dead_id)
    state = plant.store.state()
    rescued = [r for r in state.reroutes if r.kind == "stranded_rescue"]
    assert {r.bag_id for r in rescued} == {bags[0].id, bags[1].id}
    assert set(state.recheck_queue) == {bags[0].id, bags[1].id}
    assert all(state.bags[bag_id].state.value == "rechecked" for bag_id in state.recheck_queue)


def test_dead_chute_refuses_dispatch_but_spare_accepts(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=2, stranded=0)
    plant.degrade_chute(dead_id)
    target = bags[0]
    with pytest.raises(ConflictError):
        plant.push_bag(target.id, dead_id)
    route = plant.sorter.route(target.id)
    assert route.chute_id == spare_ids[0]
    reading = Reading(
        barcode=target.barcode,
        bag_id=target.id,
        sequence=target.sequence,
        epoch=target.epoch,
        committed=True,
    )
    record = plant.sorter.dispatch(plant.bag(target.id), reading)
    assert record.chute_id == spare_ids[0]


def test_overflow_is_rejected_then_force_diverts_explicitly(plant):
    _lane(plant, "A", "CR1")
    spare = _lane(plant, "B", "CR1", capacity=0)
    flight = register_flight(plant, code="CA1234", carousel="CR1")
    dead_id = plant.namespace.chute_id("A")
    plant.assign_chute(flight.id, dead_id)
    bags = [plant.check_in(f"B{index:03d}", flight.id) for index in range(2)]
    plan = plant.plan_degradation(dead_id)
    assert plan.feasible is False
    assert plan.overflow == [flight.id]
    with pytest.raises(QuotaError):
        plant.degrade_chute(dead_id)
    # failed attempt left nothing behind
    assert plant.store.state().incidents == {}
    assert plant.registry.validate(dead_id) is True
    incident = plant.degrade_chute(dead_id, force=True)
    state = plant.store.state()
    overflow = [r for r in state.reroutes if r.kind == "overflow_manual"]
    assert {r.bag_id for r in overflow} == {bag.id for bag in bags}
    assert all(state.bags[bag.id].state.value == "diverted" for bag in bags)
    assert state.counters.get("degrade.overflow") == 2
    assert spare.chute_id  # the spare lane record still exists


def test_prefer_same_carousel_spare(plant):
    _lane(plant, "A", "CR1")
    _lane(plant, "B", "CR1")  # same carousel
    _lane(plant, "C", "CR2")  # other carousel, more room
    plant.registry.set_capacity(plant.namespace.chute_id("C"), 100)
    flight = register_flight(plant, code="CA1234", carousel="CR1")
    dead_id = plant.namespace.chute_id("A")
    plant.assign_chute(flight.id, dead_id)
    plant.check_in("B001", flight.id)
    incident = plant.degrade_chute(dead_id)
    assert plant.store.state().assignments[flight.id].chute_id == plant.namespace.chute_id("B")


def test_recovery_repoints_live_flights_and_keeps_climbing_generations(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=1, stranded=0)
    incident = plant.degrade_chute(dead_id)
    degraded_generation = plant.store.state().assignments[flight.id].generation
    recovered = plant.recover_chute(incident.id)
    assert recovered.status == "recovered"
    assignment = plant.store.state().assignments[flight.id]
    assert assignment.chute_id == dead_id
    assert assignment.generation == degraded_generation + 1
    assert plant.registry.validate(dead_id) is True
    assert plant.board.active() == {}
    with pytest.raises(ConflictError):
        plant.recover_chute(incident.id)


def test_stale_generation_is_rejected_and_old_reference_revalidates(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=1, stranded=0)
    original = plant.chutes.current(flight.id).generation
    incident = plant.degrade_chute(dead_id)
    with pytest.raises(StaleError):
        plant.chutes.ensure_current(flight.id, original)
    plant.recover_chute(incident.id)
    result = plant.revalidate_assignment(flight.id, original)
    assert result["current"] is False
    assert result["checked_generation"] == original
    assert result["current_generation"] == original + 2
    assert result["recovered_incidents"][0]["incident_id"] == incident.id
    current = result["current_generation"]
    assert plant.revalidate_assignment(flight.id, current)["current"] is True
    with pytest.raises(StaleError):
        plant.revalidate_assignment(flight.id, 999)


def test_three_ledgers_reconcile_clean(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=2, stranded=1)
    incident = plant.degrade_chute(dead_id)
    report = plant.degradation.reconcile(incident.id)
    assert report.ok is True
    assert report.rescued == [bags[0].id]
    assert report.reroutes == 3  # 2 queued + 1 rescued


def test_degrade_is_atomic_when_plan_is_infeasible(plant):
    _lane(plant, "A", "CR1")
    _lane(plant, "B", "CR1", capacity=0)
    flight = register_flight(plant, code="CA1234", carousel="CR1")
    dead_id = plant.namespace.chute_id("A")
    plant.assign_chute(flight.id, dead_id)
    plant.check_in("B001", flight.id)
    watermark_before = plant.store.watermark
    with pytest.raises(QuotaError):
        plant.degrade_chute(dead_id)
    # the rejected command stream appended nothing
    assert plant.store.watermark == watermark_before
    assert plant.store.state().incidents == {}
    assert plant.registry.validate(dead_id) is True


def test_before_after_snapshots_and_audit_trail_tie_out(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=1, stranded=1)
    before_watermark = plant.store.watermark
    incident = plant.degrade_chute(dead_id)
    assert incident.before_watermark == before_watermark
    assert incident.after_watermark > incident.before_watermark
    assert incident.before_digest and incident.after_digest
    assert incident.before_digest != incident.after_digest
    assert plant.audit.verify() is True
    kinds = plant.audit.by_kind()
    assert kinds.get("chute_degrade") == 1
    degrade_entries = [e for e in plant.audit.entries() if e.kind == "chute_degrade"]
    assert degrade_entries[0].subject == dead_id
    assert degrade_entries[0].detail.get("incident_id") == incident.id
    # the digest stored actually matches the journal prefix it claims
    from bagsort.store.snapshot import digest_for

    prefix = plant.store.entries()[: incident.before_watermark]
    assert digest_for(prefix) == incident.before_digest


def test_snapshot_round_trip_rebuilds_incidents_and_reroutes(tmp_path):
    from bagsort.app import Plant
    from bagsort.clock import Clock
    from bagsort.store.store import Store
    from tests.support import make_plant

    first = make_plant(tmp_path)
    _flight, dead_id, _spare, _bags = _setup(first)
    incident = first.degrade_chute(dead_id)
    path = first.store.path
    first.store.persist()
    reopened = Store.open(path)
    rebuilt_plant = Plant.build(reopened, clock=Clock())
    state = reopened.state()
    assert state.incidents[incident.id].chute_id == dead_id
    assert len(state.reroutes) == len(first.store.state().reroutes)
    assert rebuilt_plant.board.is_degraded(dead_id) is True
    assert rebuilt_plant.registry.validate(dead_id) is False
    # folded state is byte identical
    assert reopened.state().as_dict() == first.store.state().as_dict()


def test_unknown_chute_and_double_degrade_are_rejected(plant):
    with pytest.raises(ValidationError):
        plant.plan_degradation("T1-CHZ")
    _lane(plant, "A", "CR1")
    dead_id = plant.namespace.chute_id("A")
    plant.degrade_chute(dead_id)
    with pytest.raises(ConflictError):
        plant.degrade_chute(dead_id)


def test_degrade_commits_in_one_transaction_even_at_peak(plant):
    flight, dead_id, spare_ids, bags = _setup(plant, queued=300, stranded=0)
    plant.registry.set_capacity(spare_ids[0], 1000)
    before = plant.store.watermark
    incident = plant.degrade_chute(dead_id)
    # the after-stamp is its own command; the reroute body is one commit batch.
    assert incident.before_watermark == before
    body = incident.after_watermark - before
    assert body == 305
    assert sum(1 for r in plant.store.state().reroutes if r.kind == "queued_in_place") == 300
    assert plant.degradation.reconcile(incident.id).ok is True
