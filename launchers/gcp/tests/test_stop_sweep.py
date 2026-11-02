import dataclasses
import logging
import time

import pytest

from fake_compute import FakeCompute, clock
from launcher import launch, status, stop, sweep, vm_name

ZONE_A, ZONE_B, ZONE_C = "us-west4-a", "us-west4-b", "us-west4-c"
REQ = {"name": "superci-1", "max_minutes": 60, "cpu": 2, "ram_gb": 8, "jit": "QUJD"}
SECRET = {"x-superci-sweep": "sweep-secret"}


def vm(name: str) -> str:
    """The instance name `launch` gives the VM of runner `name` with REQ's runner configuration (the id it answers)."""
    return vm_name(name, REQ["jit"])


def made(fc, cfg, name, zone=ZONE_A, *, aged_min=0.0, max_minutes=60, now):
    """A VM that `launch` made for runner `name` in `zone` `aged_min` minutes before `now` (so its labels are the ones launch
    writes): its zone and the id launch answered, which is what the plane holds and stops it by."""
    plan, wall = fc.plan, fc.wall
    fc.plan, fc.wall = {(zone, "e2-standard-2"): "ok"}, lambda: now - aged_min * 60
    answer = launch({**REQ, "name": name, "max_minutes": max_minutes}, fc, cfg, clock())
    fc.plan, fc.wall = plan, wall
    return zone, answer["id"]


def fails(error: Exception):
    """A stand-in for a `Compute` method that raises `error`."""
    def call(*args):
        raise error
    return call


def bearer(mint) -> dict:
    return {"authorization": "Bearer " + mint({"exp": time.time() + 60})}


def test_stop_deletes_wherever_the_vm_is_and_is_quiet_when_it_is_gone(cfg, now):
    fc = FakeCompute()
    in_a = made(fc, cfg, "in-a", ZONE_A, now=now)
    in_c = made(fc, cfg, "in-c", ZONE_C, now=now)
    stop(in_c[1], fc, cfg)
    assert fc.deleted == [in_c]
    assert list(fc.vms) == [in_a]
    stop(in_c[1], fc, cfg)  # it is gone now
    stop("never-was", fc, cfg)
    assert fc.deleted == [in_c] and list(fc.vms) == [in_a]


def test_stop_deletes_a_name_in_every_zone_that_has_it(cfg):
    fc = FakeCompute({(z, "e2-standard-2"): "ok" for z in (ZONE_A, ZONE_B)})
    for zone in (ZONE_A, ZONE_B):  # (overlapping launches of one runner can make two)
        fc.insert(zone, {"name": "twice", "machineType": f"zones/{zone}/machineTypes/e2-standard-2", "labels": {"superci-plane": cfg.plane_id}})
    stop("twice", fc, cfg)
    assert fc.deleted == [(ZONE_A, "twice"), (ZONE_B, "twice")] and fc.vms == {}


def test_stop_leaves_a_vm_of_that_name_that_is_not_this_planes_and_is_quiet_about_it(cfg, now):
    fc = FakeCompute({(ZONE_B, "e2-standard-2"): "ok", (ZONE_C, "e2-standard-2"): "ok"})
    _, theirs = made(fc, dataclasses.replace(cfg, plane_id="p2"), "theirs", ZONE_A, now=now)  # another plane's
    fc.insert(ZONE_B, {"name": theirs, "machineType": f"zones/{ZONE_B}/machineTypes/e2-standard-2"})  # no labels at all
    fc.insert(ZONE_C, {"name": theirs, "machineType": f"zones/{ZONE_C}/machineTypes/e2-standard-2", "labels": {"owner": "someone"}})  # none of ours
    stop(theirs, fc, cfg)
    assert fc.deleted == [] and sorted(fc.vms) == [(ZONE_A, theirs), (ZONE_B, theirs), (ZONE_C, theirs)]


def test_stop_deletes_only_the_vm_of_its_own_plane_when_another_plane_has_the_name_in_another_zone(cfg, now):
    fc = FakeCompute({(ZONE_A, "e2-standard-2"): "ok"})
    ours = made(fc, cfg, "shared", ZONE_B, now=now)
    fc.insert(ZONE_A, {"name": ours[1], "machineType": f"zones/{ZONE_A}/machineTypes/e2-standard-2", "labels": {"superci-plane": "p2"}})
    stop(ours[1], fc, cfg)
    assert fc.deleted == [ours] and list(fc.vms) == [(ZONE_A, ours[1])]


def test_stop_finds_a_vm_by_the_id_launch_answered_also_in_uppercase(cfg, now):
    fc = FakeCompute()
    key = made(fc, cfg, "Acme/Widgets 7_Job", ZONE_B, now=now)  # the instance is `acme-widgets-7-job-<8 hex digits>`
    assert key == (ZONE_B, vm("Acme/Widgets 7_Job")) and key[1].startswith("acme-widgets-7-job-")
    stop(key[1], fc, cfg)
    assert fc.deleted == [key] and fc.vms == {}
    _, nine = made(fc, cfg, "superci-9", ZONE_A, now=now)
    stop(nine.upper(), fc, cfg)  # uppercase is the same name
    assert fc.vms == {}


def test_status_is_running_while_this_planes_vm_is_there_wherever_it_is(cfg, now):
    fc = FakeCompute()
    _, in_c = made(fc, cfg, "in-c", ZONE_C, now=now)
    assert status(in_c, fc, cfg) == "running"
    assert status(in_c.upper(), fc, cfg) == "running"  # (an instance name, as stop makes it)
    assert fc.deleted == []


def test_status_is_gone_when_no_zone_has_the_vm(cfg, now):
    fc = FakeCompute()
    key = made(fc, cfg, "taken", ZONE_B, now=now)
    del fc.vms[key]  # (GCE deleted it: a spot VM taken back is deleted, as its termination action says)
    assert status(key[1], fc, cfg) == "gone"
    assert status("never-was", fc, cfg) == "gone"


def test_status_is_gone_when_the_vm_is_terminated(cfg, now):
    fc = FakeCompute()
    key = made(fc, cfg, "taken", ZONE_A, now=now)
    fc.vms[key]["status"] = "TERMINATED"
    assert status(key[1], fc, cfg) == "gone"


def test_status_is_ending_while_the_vm_is_stopping_and_running_while_another_of_its_name_runs(cfg, now):
    # (A VM taken back is STOPPING for up to about thirty seconds, while its notice may still be on its way to the plane.)
    fc = FakeCompute({(ZONE_B, "e2-standard-2"): "ok"})
    key = made(fc, cfg, "taken", ZONE_A, now=now)
    fc.vms[key]["status"] = "STOPPING"
    assert status(key[1], fc, cfg) == "ending"
    fc.insert(ZONE_B, {"name": key[1], "machineType": f"zones/{ZONE_B}/machineTypes/e2-standard-2", "labels": {"superci-plane": cfg.plane_id}})
    assert status(key[1], fc, cfg) == "running"
    fc.vms[(ZONE_B, key[1])]["status"] = "TERMINATED"
    assert status(key[1], fc, cfg) == "ending"


def test_status_refuses_a_vm_of_that_name_that_is_not_this_planes_and_answers_its_own_in_another_zone(cfg, now):
    fc = FakeCompute({(ZONE_B, "e2-standard-2"): "ok"})
    _, theirs = made(fc, dataclasses.replace(cfg, plane_id="p2"), "theirs", ZONE_A, now=now)
    with pytest.raises(ValueError, match="not this plane's"):
        status(theirs, fc, cfg)
    fc.insert(ZONE_B, {"name": theirs, "machineType": f"zones/{ZONE_B}/machineTypes/e2-standard-2", "labels": {"superci-plane": cfg.plane_id}})
    assert status(theirs, fc, cfg) == "running"  # (its own VM of that name, in another zone, is what it answers for)


def test_sweep_deletes_only_what_is_old_or_terminated(cfg, now):
    fc = FakeCompute()
    young = made(fc, cfg, "young", ZONE_A, aged_min=5, now=now)  # running, and inside its hour
    old = made(fc, cfg, "old", ZONE_B, aged_min=71, now=now)  # running, and past its hour and the ten minutes after
    off = made(fc, cfg, "off", ZONE_C, aged_min=5, now=now)
    fc.vms[off]["status"] = "TERMINATED"
    assert sweep(fc, cfg, now) == 2
    assert sorted(fc.deleted) == [old, off]
    assert list(fc.vms) == [young]
    assert sweep(fc, cfg, now) == 0 and len(fc.deleted) == 2


@pytest.mark.parametrize("max_minutes, aged_min, swept", [
    (60, 59, 0), (60, 69.9, 0), (60, 70, 0), (60, 70.1, 1),  # the limit is the VM's own label plus ten minutes, and "older than" is strict
    (30, 39.9, 0), (30, 40.1, 1), (240, 100, 0), (240, 251, 1),
])
def test_a_vm_is_old_when_it_is_past_its_own_limit_and_ten_minutes(cfg, now, max_minutes, aged_min, swept):
    fc = FakeCompute()
    made(fc, cfg, "vm", aged_min=aged_min, max_minutes=max_minutes, now=now)
    assert sweep(fc, cfg, now) == swept
    assert len(fc.deleted) == swept


def test_sweep_goes_by_the_labels_of_its_own_plane(cfg, now):
    fc = FakeCompute()
    theirs = made(fc, dataclasses.replace(cfg, plane_id="p2"), "theirs", aged_min=500, now=now)
    ours = made(fc, cfg, "ours", aged_min=500, now=now)
    assert sweep(fc, cfg, now) == 1
    assert fc.deleted == [ours] and list(fc.vms) == [theirs]


def test_sweep_skips_a_listed_vm_that_is_not_this_planes_even_when_the_listing_returns_it(cfg, now, monkeypatch):
    fc = FakeCompute({(ZONE_C, "e2-standard-2"): "ok"})
    theirs = made(fc, dataclasses.replace(cfg, plane_id="p2"), "theirs", ZONE_A, aged_min=500, now=now)
    ours = made(fc, cfg, "ours", ZONE_B, aged_min=500, now=now)
    fc.insert(ZONE_C, {"name": "unlabelled", "machineType": f"zones/{ZONE_C}/machineTypes/e2-standard-2"})
    fc.vms[(ZONE_C, "unlabelled")]["status"] = "TERMINATED"
    monkeypatch.setattr(fc, "list", lambda zone, label_filter: FakeCompute.list(fc, zone, ""))  # (a listing that ignores the filter)
    assert sweep(fc, cfg, now) == 1
    assert fc.deleted == [ours] and sorted(fc.vms) == [theirs, (ZONE_C, "unlabelled")]


def test_sweep_looks_in_the_zones_of_the_configuration_and_no_others(cfg, now):
    fc = FakeCompute()
    elsewhere = made(fc, cfg, "elsewhere", ZONE_A, aged_min=500, now=now)
    here = made(fc, cfg, "here", ZONE_B, aged_min=500, now=now)
    assert sweep(fc, dataclasses.replace(cfg, zones=(ZONE_B,)), now) == 1
    assert fc.deleted == [here] and list(fc.vms) == [elsewhere]


DAMAGED = pytest.mark.parametrize("damage", [
    lambda vm: vm["labels"].pop("superci-max-minutes"),
    lambda vm: vm.update(labels={"superci-plane": vm["labels"]["superci-plane"]}),  # (the plane label is what makes it ours)
    lambda vm: vm["labels"].update({"superci-max-minutes": "soon"}),
    lambda vm: vm["labels"].update({"superci-max-minutes": ""}),
    lambda vm: vm["labels"].update({"superci-max-minutes": "-5"}),
    lambda vm: vm["labels"].update({"superci-max-minutes": "1_0"}),
    lambda vm: vm.pop("creationTimestamp"),
    lambda vm: vm.update(creationTimestamp="yesterday"),
    lambda vm: vm.update(creationTimestamp="2020-01-01T00:00:00.000"),  # no offset, so no telling when
    lambda vm: vm.update(creationTimestamp=None),
], ids=["no label", "only the plane label", "label not a number", "empty label", "negative label", "underscore in label",
        "no timestamp", "timestamp not a time", "timestamp without offset", "timestamp none"])


@DAMAGED
def test_a_running_vm_whose_label_or_timestamp_cannot_be_read_is_left_alone_and_not_counted(cfg, now, damage):
    fc = FakeCompute()
    key = made(fc, cfg, "odd", aged_min=5000, now=now)
    damage(fc.vms[key])
    assert sweep(fc, cfg, now) == 0
    assert fc.deleted == [] and list(fc.vms) == [key]


@DAMAGED
def test_a_terminated_vm_of_this_plane_is_swept_regardless_of_its_label_and_timestamp(cfg, now, damage):
    fc = FakeCompute()
    key = made(fc, cfg, "odd", aged_min=1, now=now)
    fc.vms[key]["status"] = "TERMINATED"
    damage(fc.vms[key])
    assert sweep(fc, cfg, now) == 1
    assert fc.deleted == [key] and fc.vms == {}


def test_a_vm_that_cannot_be_read_does_not_hold_back_the_others(cfg, now):
    fc = FakeCompute()
    odd = made(fc, cfg, "odd", ZONE_A, aged_min=5000, now=now)
    del fc.vms[odd]["labels"]["superci-max-minutes"]
    made(fc, cfg, "old", ZONE_A, aged_min=5000, now=now)
    assert sweep(fc, cfg, now) == 1 and list(fc.vms) == [odd]


def test_a_failure_to_list_is_raised(cfg, now, monkeypatch):
    fc = FakeCompute()
    monkeypatch.setattr(fc, "list", fails(RuntimeError("UNAVAILABLE: later")))
    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        sweep(fc, cfg, now)


def refusing_to_delete(fc, monkeypatch, *names):
    """`fc.delete` as it is, but raising for the VMs of these names."""
    real = fc.delete

    def delete(zone, name):
        if name in names:
            raise RuntimeError(f"PERMISSION_DENIED: no delete of {name}")
        real(zone, name)

    monkeypatch.setattr(fc, "delete", delete)


def test_a_delete_that_fails_is_logged_and_the_sweep_goes_on_and_counts_only_what_it_deleted(cfg, now, monkeypatch, caplog):
    fc = FakeCompute()
    for name, zone in (("first", ZONE_A), ("stuck", ZONE_B), ("last", ZONE_C), ("after-stuck", ZONE_B)):
        made(fc, cfg, name, zone, aged_min=500, now=now)
    refusing_to_delete(fc, monkeypatch, vm("stuck"))
    with caplog.at_level(logging.WARNING):
        assert sweep(fc, cfg, now) == 3
    assert sorted(fc.vms) == [(ZONE_B, vm("stuck"))]  # the others were deleted, the one that refused is still there
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and ZONE_B in lines[0] and "stuck" in lines[0] and "PERMISSION_DENIED" in lines[0]
    monkeypatch.undo()
    assert sweep(fc, cfg, now) == 1 and fc.vms == {}  # the next sweep tries it again


# The routes.

def test_sweep_route_needs_its_secret(client_with_fake, fake, cfg, mint):
    old = made(fake, cfg, "old", aged_min=500, now=time.time())
    wrong = [{}, {"x-superci-sweep": "wrong"}, {"x-superci-sweep": "sweep-secre"}, {"x-superci-sweep": "sweep-secrets"},
             {"x-superci-sweep": ""}, {"x-superci-sweep": "swéep".encode("latin-1")}, bearer(mint)]  # (a plane's token opens nothing here)
    for headers in wrong:
        r = client_with_fake.post("/sweep", headers=headers)
        assert (r.status_code, r.json()) == (404, {"detail": "Not Found"}), headers
    assert fake.deleted == []
    r = client_with_fake.post("/sweep", headers=SECRET)
    assert (r.status_code, r.json()) == (200, {"swept": 1})
    assert fake.deleted == [old]
    assert client_with_fake.post("/sweep", headers=SECRET).json() == {"swept": 0}


def test_sweep_route_is_closed_when_no_secret_is_configured(app_module, client_with_fake, fake, cfg, monkeypatch):
    monkeypatch.setattr(app_module.cfg, "sweep_secret", "")
    made(fake, cfg, "old", aged_min=500, now=time.time())
    for headers in ({}, {"x-superci-sweep": ""}):
        assert client_with_fake.post("/sweep", headers=headers).status_code == 404
    assert fake.deleted == []


def test_a_sweep_that_cannot_list_is_503_and_one_that_cannot_delete_a_vm_counts_the_others(client_with_fake, fake, cfg, monkeypatch):
    made(fake, cfg, "stuck", aged_min=500, now=time.time())
    made(fake, cfg, "gone", aged_min=500, now=time.time())
    refusing_to_delete(fake, monkeypatch, vm("stuck"))
    r = client_with_fake.post("/sweep", headers=SECRET)
    assert (r.status_code, r.json()) == (200, {"swept": 1})
    monkeypatch.setattr(fake, "list", fails(RuntimeError("UNAVAILABLE: later")))
    r = client_with_fake.post("/sweep", headers=SECRET)
    assert (r.status_code, r.json()) == (503, {"detail": "UNAVAILABLE: later"})


def test_stop_route(client_with_fake, fake, cfg, mint, monkeypatch):
    _, ours = made(fake, cfg, "superci-1", now=time.time())
    post = lambda body: client_with_fake.post("/stop", json=body, headers=bearer(mint))
    r = post({"id": ours})
    assert (r.status_code, r.json()) == (200, {"stopped": True})
    assert fake.vms == {}
    r = post({"id": ours})  # gone already
    assert (r.status_code, r.json()) == (200, {"stopped": True})
    for body in ({}, {"id": ""}, {"id": 5}, {"id": None}, {"name": ours}):
        r = post(body)
        assert r.status_code == 400 and "id" in r.json()["detail"], body
    theirs = made(fake, dataclasses.replace(cfg, plane_id="p2"), "theirs", now=time.time())
    r = post({"id": theirs[1]})  # another plane's: not stopped, and the plane is answered all the same
    assert (r.status_code, r.json()) == (200, {"stopped": True})
    assert list(fake.vms) == [theirs]
    assert client_with_fake.post("/stop", json={"id": ours}).status_code == 401
    monkeypatch.setattr(fake, "get", fails(RuntimeError("UNAVAILABLE: later")))
    r = post({"id": ours})
    assert (r.status_code, r.json()) == (503, {"detail": "UNAVAILABLE: later"})


def test_status_route(client_with_fake, fake, cfg, mint, monkeypatch):
    key = made(fake, cfg, "superci-1", now=time.time())
    post = lambda body: client_with_fake.post("/status", json=body, headers=bearer(mint))
    r = post({"id": key[1]})
    assert (r.status_code, r.json()) == (200, {"state": "running"})
    fake.vms[key]["status"] = "STOPPING"
    assert post({"id": key[1]}).json() == {"state": "ending"}
    fake.vms[key]["status"] = "TERMINATED"
    assert post({"id": key[1]}).json() == {"state": "gone"}
    del fake.vms[key]
    r = post({"id": key[1]})
    assert (r.status_code, r.json()) == (200, {"state": "gone"})
    for body in ({}, {"id": ""}, {"id": 5}, {"id": None}, {"name": key[1]}):
        r = post(body)
        assert r.status_code == 400 and "id" in r.json()["detail"], body
    _, theirs = made(fake, dataclasses.replace(cfg, plane_id="p2"), "theirs", now=time.time())
    r = post({"id": theirs})  # another plane's: refused, and not said to be gone
    assert r.status_code == 400 and "not this plane's" in r.json()["detail"]
    assert client_with_fake.post("/status", json={"id": key[1]}).status_code == 401
    monkeypatch.setattr(fake, "get", fails(RuntimeError("UNAVAILABLE: later")))
    r = post({"id": key[1]})
    assert (r.status_code, r.json()) == (503, {"detail": "UNAVAILABLE: later"})
    assert fake.deleted == []


# The sweep that /stop runs first (a /launch does not, so that it keeps its time budget).

def test_a_stop_sweeps_first_once_more_than_ten_minutes_have_passed_since_the_last_sweep(app_module, client_with_fake, fake, cfg, mint):
    old = made(fake, cfg, "old", aged_min=500, now=time.time())
    request = lambda: client_with_fake.post("/stop", json={"id": "other"}, headers=bearer(mint))
    assert request().status_code == 200
    assert fake.deleted == []  # the first request since the process started does not sweep
    app_module.last_sweep = time.monotonic() - 590
    assert request().status_code == 200
    assert fake.deleted == []  # nor does one inside the ten minutes
    app_module.last_sweep = time.monotonic() - 610
    assert request().status_code == 200
    assert fake.deleted == [old]
    made(fake, cfg, "older", aged_min=500, now=time.time())
    assert request().status_code == 200
    assert fake.deleted == [old]  # that was the last sweep: not again


def test_a_launch_does_not_sweep_however_long_it_has_been(app_module, client_with_fake, fake, cfg, mint):
    old = made(fake, cfg, "old", aged_min=500, now=time.time())
    fake.plan = {(ZONE_A, "e2-standard-2"): "ok"}
    long_ago = app_module.last_sweep = time.monotonic() - 100_000
    listed = []
    real_list = fake.list
    fake.list = lambda zone, label_filter: listed.append(zone) or real_list(zone, label_filter)
    r = client_with_fake.post("/launch", json={**REQ, "name": "fresh"}, headers=bearer(mint))
    assert r.status_code == 200 and r.json()["id"] == vm("fresh")
    assert listed == [] and fake.deleted == []  # no list call, so none of the time a sweep takes
    assert app_module.last_sweep == long_ago  # and it is not taken as a sweep: the next stop still sweeps
    client_with_fake.post("/stop", json={"id": "other"}, headers=bearer(mint))
    assert fake.deleted == [old]


def test_a_call_of_sweep_counts_as_the_last_sweep(app_module, client_with_fake, fake, cfg, mint):
    old = made(fake, cfg, "old", aged_min=500, now=time.time())
    app_module.last_sweep = time.monotonic() - 610
    assert client_with_fake.post("/sweep", headers=SECRET).json() == {"swept": 1}
    made(fake, cfg, "older", aged_min=500, now=time.time())
    assert client_with_fake.post("/stop", json={"id": "x"}, headers=bearer(mint)).status_code == 200
    assert fake.deleted == [old]


@pytest.mark.parametrize("error", [RuntimeError("UNAVAILABLE: later"), KeyError("name")], ids=["GCE failed", "something unexpected"])
def test_a_sweep_that_fails_does_not_fail_the_stop_and_is_not_tried_again_at_once(app_module, client_with_fake, fake, mint, caplog, monkeypatch, error):
    asked = []

    def broken(zone, label_filter):
        asked.append(zone)
        fails(error)()

    monkeypatch.setattr(fake, "list", broken)
    request = lambda: client_with_fake.post("/stop", json={"id": "other"}, headers=bearer(mint))
    app_module.last_sweep = time.monotonic() - 610
    with caplog.at_level(logging.WARNING):
        r = request()
    assert r.status_code == 200
    assert asked == [ZONE_A]  # it went as far as the first list
    assert str(error) in caplog.text  # and the failure was logged
    assert request().status_code == 200 and asked == [ZONE_A]  # the next request does not try again
