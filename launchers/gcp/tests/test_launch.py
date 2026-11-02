import dataclasses
import hashlib
import inspect
import json
import time
import types

import pytest
from google.api_core import exceptions as google_errors
from google.cloud import compute_v1

from fake_compute import FakeCompute, clock
from launcher import Compute, GoogleCompute, NoRoom, launch, render_shutdown, render_startup

ZONES = ["us-west4-a", "us-west4-b", "us-west4-c"]
TYPES = ["e2-standard-2", "c4d-standard-2", "n2d-standard-2"]  # what 2 vCPUs and 8 GB choose, in the order tried
NOTICE = "https://plane.example/interrupted?runner=superci-1&t=tok"

REQ = {"name": "superci-1", "job": 4711, "repo": "Acme/Widgets.git", "max_minutes": 70, "cpu": 2, "ram_gb": 8,
       "disk_gb": 60, "location": "wnam", "jit": "QUJD", "notice": NOTICE}  # (no "spot": that means spot)


def jit_hash(jit: str) -> str:
    return hashlib.sha256(jit.encode()).hexdigest()[:8]


ID = "superci-1-" + jit_hash("QUJD")  # the instance name of REQ's VM: its runner name, then its runner configuration's hash


def without(*keys) -> dict:
    return {k: v for k, v in REQ.items() if k not in keys}


def startup_script(body: dict) -> str:
    return body["metadata"]["items"][0]["value"]


def metadata_keys(body: dict) -> list[str]:
    return [item["key"] for item in body["metadata"]["items"]]


def test_the_first_pair_with_room_wins_and_the_answer_prices_it(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "exhausted", ("us-west4-b", "e2-standard-2"): "ok"})
    a = launch(REQ, fc, cfg, clock())
    assert a == {"id": ID, "kind": "e2-standard-2", "usd_per_hour": 0.0104, "spot": True}
    assert fc.inserted[-1]["zone"] == "us-west4-b" and fc.inserted[-1]["body"]["scheduling"]["provisioningModel"] == "SPOT"
    assert len(fc.inserted) == 2


def test_every_pair_is_tried_types_before_zones_and_then_it_is_no_room(cfg):
    fc = FakeCompute()  # room nowhere
    with pytest.raises(NoRoom) as e:
        launch(REQ, fc, cfg, clock())
    assert fc.pairs == [(z, t) for t in TYPES for z in ZONES]
    assert len(e.value.reasons) == 9 and e.value.spot is True
    # (Grouped by reason and short: the plane shows no more than 300 characters of what a launcher says.)
    assert str(e.value) == "ZONE_RESOURCE_POOL_EXHAUSTED: " + ", ".join(f"{t}/{z}" for t in TYPES for z in "abc")
    assert len(str(e.value)) < 250
    assert fc.vms == {}


@pytest.mark.parametrize("error", [
    "QUOTA_EXCEEDED: Quota 'PREEMPTIBLE_CPUS' exceeded. Limit: 24.0 in region us-west4.",
    "RESOURCE_POOL_EXHAUSTED: The region does not have enough resources.",
])
def test_a_quota_error_and_the_regional_form_of_no_capacity_move_on_like_the_zonal_one(cfg, error):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): f"error:{error}", ("us-west4-b", "e2-standard-2"): "ok"})
    assert launch(REQ, fc, cfg, clock())["kind"] == "e2-standard-2"
    assert fc.pairs == [("us-west4-a", "e2-standard-2"), ("us-west4-b", "e2-standard-2")]


def test_the_fallback_starts_an_on_demand_vm_of_the_first_type_in_the_first_zone(cfg):
    cfg = dataclasses.replace(cfg, on_demand_fallback=True)
    fc = FakeCompute({("us-west4-a", "e2-standard-2", "on-demand"): "ok"})
    a = launch(REQ, fc, cfg, clock())
    assert a == {"id": ID, "kind": "e2-standard-2", "usd_per_hour": 0.0755, "spot": False}
    assert len(fc.inserted) == 10  # the nine spot pairs, then this
    last = fc.inserted[-1]
    assert last["zone"] == "us-west4-a" and last["body"]["machineType"].endswith("/e2-standard-2")
    assert last["body"]["scheduling"] == {"onHostMaintenance": "MIGRATE", "automaticRestart": False}
    assert "instance/preempted" not in startup_script(last["body"])  # an on-demand VM has no notice to give
    assert metadata_keys(last["body"]) == ["startup-script"] and metadata_keys(fc.inserted[0]["body"]) == ["startup-script", "shutdown-script"]
    assert launch(REQ, fc, cfg, clock()) == a and len(fc.inserted) == 10  # asked again, it is the same VM, still on demand


def test_a_fallback_that_finds_no_room_either_is_no_room_and_any_other_failure_of_it_is_raised(cfg):
    cfg = dataclasses.replace(cfg, on_demand_fallback=True)
    fc = FakeCompute()
    with pytest.raises(NoRoom, match=r"ZONE_RESOURCE_POOL_EXHAUSTED: .*n2d-standard-2/c, on demand e2-standard-2/a$"):
        launch(REQ, fc, cfg, clock())
    assert len(fc.inserted) == 10
    fc = FakeCompute({("us-west4-a", "e2-standard-2", "on-demand"): "error:PERMISSION_DENIED: no"})
    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        launch(REQ, fc, cfg, clock())


def test_an_insert_still_running_at_the_budget_is_answered_as_started(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "slow"})
    a = launch(REQ, fc, cfg, clock())
    assert a == {"id": ID, "kind": "e2-standard-2", "usd_per_hour": 0.0104, "spot": True}
    assert len(fc.inserted) == 1


def test_each_wait_is_no_longer_than_what_is_left_of_the_budget(cfg):
    now = clock()
    fc = FakeCompute(clock=now, answer_s=3.0)  # each pair says no, after three seconds
    a = launch(REQ, fc, cfg, now)  # a budget of 8 seconds and a wait of at most 5
    assert [seconds for _, seconds in fc.waits] == [5.0, 5.0, 2.0]
    assert fc.pairs == [(z, "e2-standard-2") for z in ZONES]
    assert a["kind"] == "e2-standard-2"  # the third was still running when its two seconds were up: started


def test_no_insert_is_issued_once_the_budget_is_spent(cfg):
    now = clock()
    fc = FakeCompute(clock=now, answer_s=4.0)
    with pytest.raises(NoRoom) as e:
        launch(REQ, fc, dataclasses.replace(cfg, on_demand_fallback=True), now)
    assert [seconds for _, seconds in fc.waits] == [5.0, 4.0]
    assert len(fc.inserted) == 2
    assert str(e.value) == "ZONE_RESOURCE_POOL_EXHAUSTED: e2-standard-2/a, e2-standard-2/b; no time left: e2-standard-2/c"


def test_the_fallback_is_not_tried_when_the_budget_is_spent_by_then(cfg):
    now = clock()
    cfg = dataclasses.replace(cfg, on_demand_fallback=True, budget_s=9.0)
    fc = FakeCompute(clock=now, answer_s=1.0)  # nine pairs of one second each use the nine seconds
    with pytest.raises(NoRoom, match="no time left: on demand e2-standard-2/a$"):
        launch(REQ, fc, cfg, now)
    assert len(fc.inserted) == 9


def test_a_second_launch_with_the_same_jit_answers_the_existing_vm(cfg):
    # (An identical request, the same JIT configuration, is answered with the VM the first call made. The plane registers a new
    # JIT configuration for each attempt, so a retry after a timeout is a different request and gets its own VM.)
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "exhausted", ("us-west4-b", "e2-standard-2"): "ok"})
    first = launch(REQ, fc, cfg, clock())
    assert len(fc.inserted) == 2
    # (The fake refuses an insert of a name it has, as GCE does, so a second insert would raise.)
    assert launch(REQ, fc, cfg, clock()) == first
    assert len(fc.inserted) == 2


def test_a_launch_for_the_same_name_with_a_new_jit_makes_a_new_vm(cfg):
    # After a failed start the plane withdraws the runner at GitHub and registers a new JIT configuration under the same runner
    # name; a late machine is stopped and launched again the same way. The VM of the withdrawn configuration is not the answer.
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
    first = launch(REQ, fc, cfg, clock())
    again = launch({**REQ, "jit": "QUJE"}, fc, cfg, clock())
    assert first["id"] == ID and again["id"] == "superci-1-" + jit_hash("QUJE")
    assert len(fc.inserted) == 2 and fc.inserted[1]["body"]["name"] == again["id"]
    assert "--jitconfig QUJE" in startup_script(fc.inserted[1]["body"])
    assert fc.inserted[1]["body"]["labels"] == fc.inserted[0]["body"]["labels"]  # (the labels are the request's, as before)
    assert set(fc.vms) == {("us-west4-a", first["id"]), ("us-west4-a", again["id"])}


@pytest.mark.parametrize("status", ["STOPPING", "TERMINATED"])
def test_a_vm_of_that_name_that_is_stopping_or_terminated_is_no_room_and_nothing_is_inserted(cfg, status):
    fc = FakeCompute({(z, t): "ok" for z in ZONES for t in TYPES})
    launch(REQ, fc, cfg, clock())
    fc.vms[("us-west4-a", ID)]["status"] = status
    with pytest.raises(NoRoom) as e:
        launch(REQ, fc, cfg, clock())
    assert str(e.value) == f"{status}: VM {ID} in us-west4-a" and e.value.spot is True
    assert len(fc.inserted) == 1


def test_the_existing_vm_is_answered_as_it_is_whichever_zone_it_is_in_and_not_as_the_request_asks(cfg):
    fc = FakeCompute({("us-west4-c", "n2d-standard-2", "on-demand"): "ok"})
    fc.vms[("us-west4-c", ID)] = {"name": ID, "status": "RUNNING",
        "machineType": "https://www.googleapis.com/compute/v1/projects/superci-test/zones/us-west4-c/machineTypes/n2d-standard-2",
        "scheduling": {"provisioningModel": "STANDARD"}}
    a = launch(REQ, fc, cfg, clock())
    assert a == {"id": ID, "kind": "n2d-standard-2", "usd_per_hour": 0.0952, "spot": False}
    assert fc.inserted == []


def test_the_id_is_an_instance_name_gce_accepts_and_the_vm_is_found_again_by_it(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
    req = {**REQ, "name": "SuperCI.Acme_1"}
    a = launch(req, fc, cfg, clock())
    assert a["id"] == "superci-acme-1-" + jit_hash("QUJD") == fc.inserted[0]["body"]["name"]
    assert launch(req, fc, cfg, clock()) == a and len(fc.inserted) == 1
    long = launch({**REQ, "name": "A" * 80}, fc, cfg, clock())  # (cut to 54 characters, then the hash: 63, GCE's limit)
    assert long["id"] == "a" * 54 + "-" + jit_hash("QUJD") and len(long["id"]) == 63


QUOTA_MESSAGE = "QUOTA_EXCEEDED: Quota 'CPUS' exceeded.  Limit: 24.0 in region us-west4."


def test_a_request_refused_for_quota_or_capacity_moves_on_like_an_operation_error(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): f"refused:{QUOTA_MESSAGE}",
                      ("us-west4-b", "e2-standard-2"): "refused:ZONE_RESOURCE_POOL_EXHAUSTED: no room", ("us-west4-c", "e2-standard-2"): "ok"})
    assert launch(REQ, fc, cfg, clock())["kind"] == "e2-standard-2"
    assert fc.pairs == [(z, "e2-standard-2") for z in ZONES] and list(fc.vms) == [("us-west4-c", ID)]


def test_a_request_refused_for_another_reason_ends_the_walk(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "rejected:PERMISSION_DENIED: Required 'compute.instances.create' permission"})
    with pytest.raises(RuntimeError, match="^PERMISSION_DENIED"):
        launch(REQ, fc, cfg, clock())
    assert len(fc.inserted) == 1


class Instances:
    """A stand-in for Google's InstancesClient under `GoogleCompute`: `insert` raises `refusals[zone]` in that zone, and elsewhere
    starts an operation; `get` finds no VM."""

    def __init__(self, refusals: dict):
        self.refusals, self.asked = refusals, []

    def insert(self, *, project, zone, instance_resource, retry, timeout):
        self.asked.append(zone)
        if zone in self.refusals:
            raise self.refusals[zone]
        return types.SimpleNamespace(name=f"operation-{len(self.asked)}")

    def get(self, *, project, zone, instance, retry, timeout):
        raise google_errors.NotFound("The resource was not found")


class Operations:
    def wait(self, *, project, zone, operation, timeout):
        return compute_v1.Operation(name=operation, status=compute_v1.Operation.Status.DONE)


def google_compute(cfg, refusals: dict) -> GoogleCompute:
    """A `GoogleCompute` whose Google clients are the stand-ins above (they are made when first used, so none is made here)."""
    gc = GoogleCompute(cfg)
    gc.__dict__.update(_instances=Instances(refusals), _operations=Operations())
    return gc


# What Google's client raises for an insert GCE refuses: the REST answer's message, and its `errors` with their reasons.
QUOTA_403 = google_errors.Forbidden("Quota 'CPUS' exceeded.  Limit: 24.0 in region us-west4.",
                                    errors=[{"message": "Quota 'CPUS' exceeded.  Limit: 24.0 in region us-west4.", "domain": "usageLimits", "reason": "quotaExceeded"}])
PERMISSION_403 = google_errors.Forbidden("Required 'compute.instances.create' permission for 'projects/superci-test/zones/us-west4-a/instances/x'",
                                         errors=[{"message": "Required 'compute.instances.create' permission", "domain": "global", "reason": "forbidden"}])


@pytest.mark.parametrize("refusal,code", [
    (QUOTA_403, "QUOTA_EXCEEDED"),
    (google_errors.Forbidden("Quota 'PREEMPTIBLE_CPUS' exceeded.  Limit: 8.0 in region us-west4."), "QUOTA_EXCEEDED"),  # (no reason given)
    (google_errors.ServiceUnavailable("ZONE_RESOURCE_POOL_EXHAUSTED: The zone 'projects/superci-test/zones/us-west4-a' does not have enough resources available to fulfill the request."), "ZONE_RESOURCE_POOL_EXHAUSTED"),
], ids=["quota, by its reason", "quota, by its message", "capacity"])
def test_google_compute_takes_a_request_refused_for_quota_or_capacity_as_no_room(cfg, refusal, code):
    gc = google_compute(cfg, {"us-west4-a": refusal})
    assert launch(REQ, gc, cfg, clock())["kind"] == "e2-standard-2"
    assert gc._instances.asked == ["us-west4-a", "us-west4-b"]
    gc = google_compute(cfg, {z: refusal for z in ZONES})
    with pytest.raises(NoRoom) as e:
        launch(REQ, gc, cfg, clock())
    assert str(e.value) == f"{code}: " + ", ".join(f"{t}/{z}" for t in TYPES for z in "abc")


def test_google_compute_raises_a_request_refused_for_permission(cfg):
    gc = google_compute(cfg, {"us-west4-a": PERMISSION_403})
    with pytest.raises(RuntimeError, match="^Forbidden: .*Required 'compute.instances.create' permission"):
        launch(REQ, gc, cfg, clock())
    assert gc._instances.asked == ["us-west4-a"]


def test_a_request_refused_for_quota_everywhere_is_429_and_one_refused_for_permission_is_503(app_module, client, cfg, mint):
    def post(refusal):
        app_module.api.dependency_overrides[app_module.get_compute] = lambda: google_compute(cfg, {z: refusal for z in ZONES})
        return client.post("/launch", json=REQ, headers=bearer(mint))

    r = post(QUOTA_403)
    assert (r.status_code, r.json()) == (429, {"detail": "no spot room in us-west4 (QUOTA_EXCEEDED: " + ", ".join(f"{t}/{z}" for t in TYPES for z in "abc") + ")"})
    r = post(PERMISSION_403)
    assert r.status_code == 503 and r.json()["detail"].startswith("Forbidden: Required 'compute.instances.create' permission for "), r.json()


def test_an_unexpected_error_is_raised_and_ends_the_walk(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "error:PERMISSION_DENIED: Required 'compute.instances.create' permission"})
    with pytest.raises(RuntimeError, match="^PERMISSION_DENIED: Required 'compute.instances.create' permission$"):
        launch(REQ, fc, cfg, clock())
    assert len(fc.inserted) == 1


@pytest.mark.parametrize("req", [
    without("jit"),
    {**without("jit"), "gitlab": {"url": "https://gitlab.example", "token": "t"}},
    {**REQ, "jit": "abc; rm -rf /"},
    {**REQ, "jit": 5},
    without("name"), {**REQ, "name": ""}, {**REQ, "name": 5},
    without("cpu"), {**REQ, "cpu": "2"}, {**REQ, "cpu": 2.5}, {**REQ, "cpu": True}, {**REQ, "cpu": 0},
    without("ram_gb"), without("max_minutes"), {**REQ, "max_minutes": 0}, {**REQ, "disk_gb": -1},
    {**REQ, "spot": "false"}, {**REQ, "fail": 5}, {**REQ, "fail": "not base64!"},
    {**REQ, "notice": 5}, {**REQ, "notice": ""}, {**REQ, "notice": "http://plane.example/interrupted?runner=superci-1&t=tok"},
    {**REQ, "notice": "https://"}, {**REQ, "notice": "https://plane.example/a'; reboot; echo '"},
    {**REQ, "notice": "https://plane.example/a b"}, {**REQ, "notice": "https://plane.example/$(id)"},
    {**REQ, "notice": "https://plane.example/a\nb"}, {**REQ, "notice": "ftp://plane.example/x"},
    {**REQ, "cpu": 65},   # no machine type has so many vCPUs
    {**REQ, "ram_gb": 257},
], ids=lambda r: json.dumps({k: v for k, v in r.items() if REQ.get(k) != v} or {"without": [k for k in REQ if k not in r]}))
def test_a_bad_request_is_a_value_error_and_nothing_is_started(cfg, req):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
    with pytest.raises(ValueError):
        launch(req, fc, cfg, clock())
    assert fc.inserted == [] and fc.vms == {}


def test_the_vm_body_of_a_spot_request(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
    launch(REQ, fc, cfg, clock())
    assert fc.inserted[0]["body"] == {
        "name": ID,
        "machineType": "zones/us-west4-a/machineTypes/e2-standard-2",
        "disks": [{"boot": True, "autoDelete": True, "type": "PERSISTENT", "initializeParams": {
            "sourceImage": "projects/superci-test/global/images/family/superci-runner",
            "diskSizeGb": 60, "diskType": "zones/us-west4-a/diskTypes/pd-balanced"}}],
        "networkInterfaces": [{"subnetwork": "projects/superci-test/regions/us-west4/subnetworks/superci-us-west4",
                               "nicType": "GVNIC", "accessConfigs": [{"type": "ONE_TO_ONE_NAT"}]}],
        "metadata": {"items": [{"key": "startup-script", "value": render_startup("QUJD", None, 70, NOTICE, cfg.runner_version)},
                               {"key": "shutdown-script", "value": render_shutdown(NOTICE)}]},
        "serviceAccounts": [],
        "labels": {"superci-plane": "p1", "superci-job": "4711", "superci-repo": "acme-widgets-git", "superci-max-minutes": "70"},
        "scheduling": {"provisioningModel": "SPOT", "instanceTerminationAction": "DELETE", "automaticRestart": False,
                       "onHostMaintenance": "TERMINATE"},
    }


def test_spot_false_makes_an_on_demand_vm_with_no_notice(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
    a = launch({**REQ, "spot": False}, fc, cfg, clock())
    assert a == {"id": ID, "kind": "e2-standard-2", "usd_per_hour": 0.0755, "spot": False}
    body = fc.inserted[0]["body"]
    # (An on-demand VM is moved live when its host is maintained, and is not restarted: a spot VM cannot be moved and is stopped.)
    assert body["scheduling"] == {"onHostMaintenance": "MIGRATE", "automaticRestart": False}
    assert body["networkInterfaces"][0]["nicType"] == "GVNIC"
    script = startup_script(body)
    assert "instance/preempted" not in script and NOTICE not in script
    assert metadata_keys(body) == ["startup-script"]  # (no shutdown-script: there is no preemption to tell)
    assert "--jitconfig QUJD" in script and script == render_startup("QUJD", None, 70, None, cfg.runner_version)


def test_a_spot_vm_with_no_notice_to_give_has_no_watcher_and_a_failing_runner_has_its_reason(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
    launch({**without("notice"), "fail": "d2h5"}, fc, cfg, clock())
    body = fc.inserted[0]["body"]
    assert body["scheduling"]["provisioningModel"] == "SPOT"
    assert startup_script(body) == render_startup("QUJD", "d2h5", 70, None, cfg.runner_version)
    assert metadata_keys(body) == ["startup-script"]


def test_an_on_demand_request_gets_no_second_try_as_a_fallback(cfg):
    fc = FakeCompute()
    with pytest.raises(NoRoom) as e:
        launch({**REQ, "spot": False}, fc, dataclasses.replace(cfg, on_demand_fallback=True), clock())
    assert len(fc.inserted) == 9 and e.value.spot is False


def test_a_c4d_vm_boots_from_hyperdisk_and_the_others_from_pd_balanced(cfg):
    plan = {(z, "e2-standard-2"): "exhausted" for z in ZONES} | {("us-west4-b", "c4d-standard-2"): "ok"}
    fc = FakeCompute(plan)
    a = launch(REQ, fc, cfg, clock())
    assert (a["kind"], a["usd_per_hour"]) == ("c4d-standard-2", 0.0165)
    disk_types = [i["body"]["disks"][0]["initializeParams"]["diskType"] for i in fc.inserted]
    assert disk_types == [f"zones/{z}/diskTypes/pd-balanced" for z in ZONES] + ["zones/us-west4-a/diskTypes/hyperdisk-balanced",
                                                                              "zones/us-west4-b/diskTypes/hyperdisk-balanced"]


def test_the_disk_is_the_size_asked_for_and_sixty_gb_when_none_is(cfg):
    for req, size in (({**REQ, "disk_gb": 200}, 200), (without("disk_gb"), 60)):
        fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
        launch(req, fc, cfg, clock())
        assert fc.inserted[0]["body"]["disks"][0]["initializeParams"]["diskSizeGb"] == size


def test_a_disk_smaller_than_the_image_is_raised_to_the_images_size(cfg):
    # (The plane's failing runner asks for 8 GB; GCE refuses a boot disk smaller than its image, which is 10 GB.)
    for asked, size in ((8, 10), (1, 10), (10, 10), (11, 11)):
        fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
        launch({**REQ, "disk_gb": asked}, fc, cfg, clock())
        assert fc.inserted[0]["body"]["disks"][0]["initializeParams"]["diskSizeGb"] == size, asked


def test_on_path_b_the_vm_has_no_external_address(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
    launch(REQ, fc, dataclasses.replace(cfg, external_address=False), clock())
    nic = fc.inserted[0]["body"]["networkInterfaces"][0]
    assert "accessConfigs" not in nic and nic["subnetwork"].endswith("/subnetworks/superci-us-west4")
    assert nic["nicType"] == "GVNIC"


def test_the_subnet_is_used_as_a_path_when_it_is_one_and_made_into_one_when_it_is_a_name(cfg):
    shared = "projects/host-project/regions/us-west4/subnetworks/shared"
    url = "https://www.googleapis.com/compute/v1/" + shared
    for given, want in (("superci-us-west4", "projects/superci-test/regions/us-west4/subnetworks/superci-us-west4"),
                        (shared, shared), (url, url), ("regions/us-west4/subnetworks/s", "regions/us-west4/subnetworks/s")):
        fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
        launch(REQ, fc, dataclasses.replace(cfg, subnet=given), clock())
        assert fc.inserted[0]["body"]["networkInterfaces"][0]["subnetwork"] == want


def test_labels_are_made_safe_for_gce(cfg):
    for repo, label in (("Acme/Widgets Ünï.git", "acme-widgets--n--git"), ("my_repo-2", "my_repo-2"), ("x" * 80, "x" * 63), ("", "")):
        fc = FakeCompute({("us-west4-a", "e2-standard-2"): "ok"})
        launch({**REQ, "repo": repo}, fc, cfg, clock())
        assert fc.inserted[0]["body"]["labels"]["superci-repo"] == label


@pytest.mark.parametrize("req,config", [
    (REQ, {}),
    ({**REQ, "spot": False}, {}),
    ({**REQ, "cpu": 8}, {"external_address": False}),
])
def test_the_vm_body_is_a_compute_instance_as_googles_client_reads_it(cfg, req, config):
    """An unknown field name (a typo in a key) makes the client's parser raise."""
    fc = FakeCompute({(z, t): "ok" for z in ZONES for t in ("e2-standard-2", "e2-standard-8")})
    launch(req, fc, dataclasses.replace(cfg, **config), clock())
    instance = compute_v1.Instance.from_json(json.dumps(fc.inserted[0]["body"]))
    assert instance.name == ID and instance.disks[0].initialize_params.disk_size_gb == 60
    assert (instance.scheduling.provisioning_model == "SPOT") == req.get("spot", True)
    assert bool(instance.network_interfaces[0].access_configs) == (not config)
    assert instance.network_interfaces[0].nic_type == "GVNIC"


def test_google_compute_has_the_five_methods_of_compute_and_no_more():
    public = lambda cls: {name for name in vars(cls) if not name.startswith("_")}
    assert public(GoogleCompute) == public(Compute) == {"insert", "wait", "get", "delete", "list"}
    for name in public(Compute):
        assert list(inspect.signature(getattr(GoogleCompute, name)).parameters) == list(inspect.signature(getattr(Compute, name)).parameters)


def bearer(mint) -> dict:
    return {"authorization": "Bearer " + mint({"exp": time.time() + 60})}


def test_the_http_layer_maps_errors(client_with_fake, fake, mint):
    post = lambda body: client_with_fake.post("/launch", json=body, headers=bearer(mint))

    fake.plan[("us-west4-a", "e2-standard-2")] = "ok"
    r = post(REQ)
    assert (r.status_code, r.json()) == (200, {"id": ID, "kind": "e2-standard-2", "usd_per_hour": 0.0104, "spot": True})

    fake.plan.clear()  # room nowhere
    r = post({**REQ, "name": "superci-2"})
    assert r.status_code == 429
    assert r.json() == {"detail": "no spot room in us-west4 (ZONE_RESOURCE_POOL_EXHAUSTED: "
                                  + ", ".join(f"{t}/{z}" for t in TYPES for z in "abc") + ")"}
    r = post({**REQ, "name": "superci-3", "spot": False})
    assert r.status_code == 429 and r.json()["detail"].startswith("no room in us-west4 (")

    fake.plan[("us-west4-a", "e2-standard-2")] = "error:PERMISSION_DENIED: Required 'compute.instances.create' permission"
    r = post({**REQ, "name": "superci-4"})
    assert r.status_code == 503 and r.json() == {"detail": "PERMISSION_DENIED: Required 'compute.instances.create' permission"}

    r = post({**without("jit"), "name": "superci-5"})
    assert r.status_code == 400 and "jit" in r.json()["detail"]
    r = post({**without("jit"), "gitlab": {"url": "https://gitlab.example", "token": "t"}})
    assert r.status_code == 400 and "GitLab" in r.json()["detail"]
    assert len(fake.vms) == 1  # only the first launch made a VM


def test_a_launch_that_finds_its_vm_stopping_is_answered_429(client_with_fake, fake, mint):
    fake.plan[("us-west4-a", "e2-standard-2")] = "ok"
    assert client_with_fake.post("/launch", json=REQ, headers=bearer(mint)).status_code == 200
    fake.vms[("us-west4-a", ID)]["status"] = "STOPPING"
    r = client_with_fake.post("/launch", json=REQ, headers=bearer(mint))
    assert (r.status_code, r.json()) == (429, {"detail": f"no spot room in us-west4 (STOPPING: VM {ID} in us-west4-a)"})
    assert len(fake.inserted) == 1


def test_a_notice_link_that_is_https_with_plain_characters_is_taken_and_any_other_is_400(client_with_fake, fake, mint):
    fake.plan[("us-west4-a", "e2-standard-2")] = "ok"
    taken = "https://plane.example:8443/interrupted?runner=superci-1&t=AbC_d-9.%2F"
    assert client_with_fake.post("/launch", json={**REQ, "notice": taken}, headers=bearer(mint)).status_code == 200
    assert f"'{taken}'" in startup_script(fake.inserted[0]["body"])
    r = client_with_fake.post("/launch", json={**REQ, "name": "superci-2", "notice": "http://plane.example/interrupted"}, headers=bearer(mint))
    assert r.status_code == 400 and "notice" in r.json()["detail"]
    assert len(fake.inserted) == 1


def test_a_retried_launch_over_http_answers_the_same_vm(client_with_fake, fake, mint):
    fake.plan[("us-west4-b", "e2-standard-2")] = "ok"
    first = client_with_fake.post("/launch", json=REQ, headers=bearer(mint))
    again = client_with_fake.post("/launch", json=REQ, headers=bearer(mint))
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert len(fake.inserted) == 2  # a then b, once
