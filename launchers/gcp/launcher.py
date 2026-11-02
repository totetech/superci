# The GCP launcher: starts a job's machine in a Google Cloud project when the SuperCI control plane asks.
# It trusts only tokens that control plane signed (ES256, its public keys given in TRUSTED_KEYS), made for this
# launcher's own URL and for the plane's subject, the way the Modal and Cloudflare launchers do.
import base64
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Protocol

import requests
from google.api_core import exceptions as google_errors

from prices import ON_DEMAND, SPOT

log = logging.getLogger(__name__)


@dataclass
class Config:
    project: str
    region: str
    zones: tuple[str, ...]
    network: str
    subnet: str
    image_family: str
    runner_version: str
    on_demand_fallback: bool
    public_url: str  # the URL registered with the plane; a token's audience must be exactly this
    trust: dict  # {"issuer", "subject", "keys": the plane's JWKS}
    plane_id: str
    sweep_secret: str
    behind_gateway: bool = False  # path B: API Gateway checks the token and the launcher reads its claims
    external_address: bool = True  # path A gives job VMs an external address; path B has Cloud NAT instead
    budget_s: float = 8.0  # a launch call is bounded to this many seconds in all
    insert_wait_s: float = 5.0  # and each insert waits at most this long

    @classmethod
    def from_env(cls) -> "Config":
        """The launcher's configuration from its environment; a missing required variable raises KeyError, and a ZONES that
        names no zone (such as ",") ValueError: a launcher with no zone to try would answer every launch with no room."""
        env = os.environ
        region = env["REGION"]
        zones = tuple(z.strip() for z in (env.get("ZONES") or ",".join(f"{region}-{z}" for z in "abc")).split(",") if z.strip())
        if not zones:
            raise ValueError(f"ZONES names no zone: {env.get('ZONES')!r}")
        subject = env["TRUSTED_SUBJECT"]
        return cls(
            project=env["PROJECT"],
            region=region,
            zones=zones,
            network=env["NETWORK"],
            subnet=env["SUBNET"],
            image_family=env["IMAGE_FAMILY"],
            runner_version=env["RUNNER_VERSION"],
            on_demand_fallback=env.get("ON_DEMAND_FALLBACK") == "1",
            public_url=env["PUBLIC_URL"],
            trust={"issuer": env["TRUSTED_ISSUER"], "subject": subject, "keys": json.loads(env["TRUSTED_KEYS"])},
            plane_id=subject.removeprefix("plane:"),
            sweep_secret=env["SWEEP_SECRET"],
            behind_gateway=env.get("BEHIND_GATEWAY") == "1",
            external_address=env.get("EXTERNAL_ADDRESS") != "0",
        )


def b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify(authorization: str, audience: str, trust: dict, now: float) -> dict:
    """The claims of the plane's token in an Authorization header; PermissionError says why it is not accepted."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if not authorization.startswith("Bearer "):
        raise PermissionError("no token")
    try:
        header, payload, signature = authorization[7:].split(".")
        raw = b64url(signature)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    except ValueError:  # not three parts, or not base64
        raise PermissionError("bad token") from None
    for jwk in trust["keys"]["keys"]:
        key = ec.EllipticCurvePublicNumbers(int.from_bytes(b64url(jwk["x"]), "big"), int.from_bytes(b64url(jwk["y"]), "big"), ec.SECP256R1()).public_key()
        try:
            key.verify(der, f"{header}.{payload}".encode(), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            continue
        try:
            claims = json.loads(b64url(payload))
        except ValueError:  # signed, but not JSON
            raise PermissionError("bad token") from None
        if not isinstance(claims, dict):
            raise PermissionError("bad token")
        if claims.get("exp", 0) < now:
            raise PermissionError("expired")
        if claims.get("iss") != trust["issuer"] or claims.get("sub") != trust["subject"] or claims.get("aud") != audience:
            raise PermissionError("token for another agent or control plane")
        return claims
    raise PermissionError("bad token")


# The machine families a job can run on, in the order they are tried: by spot price in us-west4, e2 first and c4d
# before n2d (c4d's spot price is below n2d's at every size they share; on demand it is the other way round).
FAMILIES = ("e2", "c4d", "n2d")
SIZES = {"e2": (2, 4, 8, 16, 32), "n2d": (2, 4, 8, 16, 32, 64), "c4d": (2, 4, 8, 16, 32, 64)}  # vCPUs; e2 stops at 32


def choose(cpu: int, ram_gb: int, families: tuple[str, ...] = FAMILIES) -> list[str]:
    """For each family in order, the smallest standard machine type with `cpu` vCPUs and `ram_gb` GB of memory.

    A standard type has 4 GB a vCPU, which is what the fit is tested against. A c4d standard type has 3.75 GB a
    vCPU, so a job there can get up to 6 percent less memory than it asked for; that is accepted. A family with
    no size that fits is left out, and the result is empty when none fits.
    """
    types = []
    for family in families:
        fits = [n for n in SIZES[family] if n >= cpu and n * 4 >= ram_gb]
        if fits:
            types.append(f"{family}-standard-{min(fits)}")
    return types


def price(machine_type: str, spot: bool) -> float:
    """The hourly price in US dollars of a machine type in the price table; KeyError when it is not in it."""
    return (SPOT if spot else ON_DEMAND)[machine_type]


def instance_name(runner: str) -> str:
    """A GCE instance name for a runner name: lowercase letters, digits and `-`, starting with a letter, at most 63
    characters, not ending in `-`. The same runner name always gives the same instance name."""
    name = re.sub(r"[^a-z0-9-]", "-", runner.lower())
    if not name[:1].isalpha():
        name = "r-" + name
    return name[:63].rstrip("-")


def vm_name(runner: str, jit: str) -> str:
    """The instance name of the VM for a runner and its JIT configuration: `instance_name(runner)` cut to 54 characters, `-`, and
    the first 8 hex digits of the configuration's SHA-256 (63 characters at most). It is the `id` /launch answers.

    The plane never sends one configuration twice: each start attempt withdraws the runner at GitHub and registers a new
    configuration under the same runner name (a machine that is late to come online is stopped and started again in the same way),
    and its call to /launch is a single send, with no retry. Keyed on the configuration, every attempt's VM has a name of its own,
    so a VM made by a launch whose answer was lost is never found again and never reused: it holds a withdrawn configuration, its
    runner cannot register, it powers off, and the sweep deletes it (the time bound in its startup script ends it otherwise).
    A VM found by this name was made for a request with this very runner name and configuration."""
    return instance_name(runner)[:54] + "-" + hashlib.sha256(jit.encode()).hexdigest()[:8]


# What the job VM runs at boot: GitHub's runner for one job, then power off. `startup.sh.tmpl` is the script; these
# are the two optional parts that `render_startup` puts in it.

# How the control plane's notice link is posted to, as AWS's spot machines post to theirs (`SPOT_WATCH` in crates/core/src/aws.rs):
# each try cut at 5 seconds, four more tries on any error, and the plane's answer (a script that writes the reason into the log of
# the job's running step) run with bash.
_POST_NOTICE = "curl -s -f -m 5 --retry 4 --retry-all-errors -X POST __NOTICE__ 2>/dev/null | bash &>/dev/null"

# GCE's metadata server answers `preempted?wait_for_change=true` when the value changes, to TRUE as the VM is taken
# back (30 seconds before it is stopped). The plane is told once, in the background, from before the runner starts.
_NOTICE_WATCHER = """# Told to the control plane when GCE takes this VM back.
( until [ "$(curl -s -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/preempted?wait_for_change=true')" = TRUE ]; do sleep 1; done
  """ + _POST_NOTICE + """ ) &"""

# GCE's documented way to act on a preemption: the `shutdown-script` metadata item, which GCE runs while the VM stops, within the
# 30 seconds a preempted spot VM is given. It runs at every other stop too (a power-off at the end of the job, a delete), when
# `instance/preempted` is FALSE and nothing is posted. It is kept beside the watcher in the startup script.
_SHUTDOWN_SCRIPT = """#!/bin/bash
# Told to the control plane when this VM stops because GCE takes it back, and only then.
[ "$(curl -s -m 2 -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/preempted')" = TRUE ] || exit 0
""" + _POST_NOTICE + "\n"

# A failing runner's job-started hook says why (base64, as the control plane wrote it) and fails the job before any
# step, as `runner_command` in the Modal launcher does. `RUNNER_ENV` is the setting the runner's command line gets (`sudo` would drop an exported one).
_FAIL_HOOK = """# This runner only says why its job fails.
printf '#!/bin/bash\\necho __WHY__ | base64 -d\\nexit 1\\n' > /tmp/superci-fail.sh && chmod 755 /tmp/superci-fail.sh
RUNNER_ENV=ACTIONS_RUNNER_HOOK_JOB_STARTED=/tmp/superci-fail.sh"""

_BASE64 = re.compile(r"[A-Za-z0-9+/=]+")


def _quoted(text: str) -> str:
    """`text` as one word for a shell, in single quotes."""
    return "'" + text.replace("'", "'\\''") + "'"


def render_startup(jit: str, fail: str | None, max_minutes: int, notice: str | None, runner_version: str) -> str:
    """The startup script of a job VM.

    `jit` is the runner's JIT configuration and `fail` (when the runner is only to fail its job) the reason, both
    base64 and ValueError otherwise, as they are put on a command line. The VM powers off after `max_minutes`
    whatever happens. `notice`, a link, is posted to once when GCE preempts the VM; none for an on-demand VM.
    `runner_version` is what the image's runner is expected to be: logged with the installed one.
    """
    if not _BASE64.fullmatch(jit):
        raise ValueError("the runner configuration is not base64")
    if fail is not None and not _BASE64.fullmatch(fail):
        raise ValueError("the failing runner's reason is not base64")
    if max_minutes < 1:
        raise ValueError("the time bound is under a minute")
    watcher = _NOTICE_WATCHER.replace("__NOTICE__", _quoted(notice)) if notice is not None else ""
    hook = _FAIL_HOOK.replace("__WHY__", fail) if fail is not None else ""
    template = Path(__file__).with_name("startup.sh.tmpl").read_text()
    # (A part left out leaves no blank line.) One pass over the template, so no inserted text is searched for placeholders.
    parts = {
        "__MAX_MINUTES__": str(max_minutes),
        "__RUNNER_VERSION__": _quoted(runner_version),
        "__JIT__": jit,
        "__NOTICE_WATCHER__\n": watcher + "\n" if watcher else "",
        "__FAIL_HOOK__\n": hook + "\n" if hook else "",
    }
    return re.sub("|".join(map(re.escape, parts)), lambda found: parts[found.group()], template)


def render_shutdown(notice: str | None) -> str | None:
    """The shutdown script of a spot job VM, which posts to `notice` when the VM stops because GCE preempts it; None when there is
    no notice to give (an on-demand VM, or a request without one)."""
    return _SHUTDOWN_SCRIPT.replace("__NOTICE__", _quoted(notice)) if notice is not None else None


# /launch: ask Google Compute Engine for the job's VM in each (machine type, zone) pair in turn until one has room.

class Compute(Protocol):
    """What the launcher asks of Google Compute Engine, in the project and region it is set up for. `GoogleCompute` is the real one;
    the tests use a stand-in. A VM is a dict in the form of GCE's REST API (an `Instance` resource as JSON, so camelCase keys),
    whichever way it goes."""

    def insert(self, zone: str, body: dict) -> str:
        """Starts making the VM `body` describes in `zone` and gives the name of the operation that makes it. When GCE refuses
        the request itself: Refused(message) for want of quota or capacity, RuntimeError(message) for any other reason."""
        ...

    def wait(self, zone: str, operation: str, timeout_s: float) -> dict | None:
        """The outcome of an operation, waiting up to `timeout_s` seconds for it: `None` while it is still running, `{}` when it
        is done, `{"error": "<CODE>: <message>"}` when it failed."""
        ...

    def get(self, zone: str, name: str) -> dict | None:
        """The VM of that name in `zone`, or None."""
        ...

    def delete(self, zone: str, name: str) -> None:
        """Starts deleting the VM of that name in `zone`; no error when there is none."""
        ...

    def list(self, zone: str, label_filter: str) -> list[dict]:
        """The VMs in `zone` that match a GCE filter on labels, such as `labels.superci-plane=p1`."""
        ...


# An operation error that says there is no room (or no quota) for the VM in that pair, here and now: the walk goes on to the next.
# Any other error is not the pair's lack of room and ends the launch. (The longer name is first, so a reason names the zonal code.)
NO_ROOM_CODES = ("ZONE_RESOURCE_POOL_EXHAUSTED", "QUOTA_EXCEEDED", "RESOURCE_POOL_EXHAUSTED")


class Refused(Exception):
    """GCE refused an insert request itself, before any operation began, for want of quota or capacity in that pair (a 403 with
    the reason `quotaExceeded`, or ZONE_RESOURCE_POOL_EXHAUSTED on the request). Its message is in the form of an operation's error,
    `<CODE>: <message>` with the code one of NO_ROOM_CODES, and `_insert` takes it as one, so the walk goes on to the next pair.
    Not a RuntimeError: that is any other refusal, which ends the launch."""


class NoRoom(Exception):
    """No (machine type, zone) pair took the VM within the time there was: the caller is to wait and ask again.

    `reasons` is, for each pair that did not, `(pair, why)`, such as `("e2-standard-2/a", "ZONE_RESOURCE_POOL_EXHAUSTED")`; `str`
    gives them grouped by why, which keeps it short (the plane shows only the first 300 characters of what a launcher says).
    `spot` is whether the VM asked for was a spot one.
    """

    def __init__(self, reasons: list[tuple[str, str]], spot: bool = True):
        by_why: dict[str, list[str]] = {}
        for pair, why in reasons:
            by_why.setdefault(why, []).append(pair)
        super().__init__("; ".join(f"{why}: {', '.join(pairs)}" for why, pairs in by_why.items()))
        self.reasons, self.spot = reasons, spot


@dataclass(frozen=True)
class _Ask:
    """What a /launch request says that the VM is made from."""
    name: str
    jit: str
    fail: str | None
    notice: str | None
    cpu: int
    ram_gb: int
    disk_gb: int
    max_minutes: int
    spot: bool
    job: str
    repo: str


def _text(req: dict, key: str, required: bool = True) -> str | None:
    value = req.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise ValueError(f"{key} is missing or not text")
    return value


def _whole(req: dict, key: str, default: int | None = None) -> int:
    value = req.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{key} is missing or not a whole number of at least 1")
    return value


# A notice link the launcher puts in a VM's scripts: https, and only the characters the plane's links have (as `runner_user_data_for`
# in crates/core/src/aws.rs requires of AWS's, with `%` for an escaped character).
_NOTICE_LINK = re.compile(r"https://[A-Za-z0-9:/?=&._%-]+")


def _ask(req: dict) -> _Ask:
    """The request's fields, checked: ValueError says what is wrong (the app answers 400 and the plane fails the job)."""
    if "jit" not in req:
        raise ValueError("a GitLab job cannot run here: the request has no jit" if "gitlab" in req else "the request has no jit")
    spot = req.get("spot", True)  # (the plane says nothing of it for its small failing runner)
    if not isinstance(spot, bool):
        raise ValueError("spot is not true or false")
    notice = _text(req, "notice", required=False)
    if notice is not None and not _NOTICE_LINK.fullmatch(notice):
        raise ValueError("notice is not an https link of letters, digits and :/?=&._%-")
    return _Ask(name=_text(req, "name"), jit=_text(req, "jit"), fail=_text(req, "fail", required=False),
                notice=notice, cpu=_whole(req, "cpu"), ram_gb=_whole(req, "ram_gb"),
                disk_gb=_whole(req, "disk_gb", 60), max_minutes=_whole(req, "max_minutes"), spot=spot,
                job=str(req.get("job", "")), repo=str(req.get("repo", "")))


def _label(text: str) -> str:
    """`text` as a GCE label value: lowercase letters, digits, `_` and `-`, at most 63 characters (anything else becomes `-`)."""
    return re.sub(r"[^a-z0-9_-]", "-", text.lower())[:63]


def subnet_path(cfg: Config) -> str:
    """`cfg.subnet` as GCE's API takes a subnetwork: a path or URL (`projects/<p>/regions/<r>/subnetworks/<s>`,
    `regions/<r>/subnetworks/<s>`, or the full https URL; one in another project is a shared VPC's) is used as it is, and a bare name
    is the subnet of that name in this project and region. The VM's network is the subnet's, so `cfg.network` is not sent."""
    return cfg.subnet if "/" in cfg.subnet else f"projects/{cfg.project}/regions/{cfg.region}/subnetworks/{cfg.subnet}"


# The size in GB of the superci-runner image (image/build-image.sh makes it from a 10 GB boot disk): GCE refuses a boot disk smaller
# than its image. A request for less, such as the plane's failing runner with 8 GB, gets this.
MIN_DISK_GB = 10


def _vm_body(cfg: Config, ask: _Ask, zone: str, kind: str, spot: bool, script: str, shutdown: str | None = None) -> dict:
    """The Instance resource of a job VM of machine type `kind` in `zone`: a spot VM (deleted when GCE takes it back, never restarted)
    or an on-demand one. `script` is its startup script and `shutdown` its shutdown script, when it has one (a spot VM with a
    notice to give). No service account: a job runs as root on its VM and must not reach the project."""
    interface = {"subnetwork": subnet_path(cfg), "nicType": "GVNIC"}  # (c4d takes gVNIC only; e2 and n2d support it, and the image has the driver)
    if cfg.external_address:  # path A; path B's VMs have Cloud NAT instead
        interface["accessConfigs"] = [{"type": "ONE_TO_ONE_NAT"}]
    # A spot VM cannot be moved live when its host is maintained, so it is stopped (and deleted); an on-demand VM is moved. Neither
    # is restarted by GCE: a job's runner runs once, and a VM that stopped is the sweep's.
    if spot:
        scheduling = {"onHostMaintenance": "TERMINATE", "provisioningModel": "SPOT", "instanceTerminationAction": "DELETE", "automaticRestart": False}
    else:
        scheduling = {"onHostMaintenance": "MIGRATE", "automaticRestart": False}
    return {
        "name": vm_name(ask.name, ask.jit),
        "machineType": f"zones/{zone}/machineTypes/{kind}",
        "disks": [{
            "boot": True, "autoDelete": True, "type": "PERSISTENT",  # (autoDelete: a deleted VM leaves no disk behind)
            "initializeParams": {
                "sourceImage": f"projects/{cfg.project}/global/images/family/{cfg.image_family}",
                "diskSizeGb": max(ask.disk_gb, MIN_DISK_GB),
                # c4d takes Hyperdisk only
                "diskType": f"zones/{zone}/diskTypes/{'hyperdisk-balanced' if kind.startswith('c4d-') else 'pd-balanced'}",
            },
        }],
        "networkInterfaces": [interface],
        "metadata": {"items": [{"key": "startup-script", "value": script}]
                     + ([{"key": "shutdown-script", "value": shutdown}] if shutdown is not None else [])},
        "serviceAccounts": [],
        "labels": {"superci-plane": cfg.plane_id, "superci-job": _label(ask.job), "superci-repo": _label(ask.repo),
                   "superci-max-minutes": str(ask.max_minutes)},
        "scheduling": scheduling,
    }


def _answer(name: str, kind: str, spot: bool) -> dict:
    return {"id": name, "kind": kind, "usd_per_hour": price(kind, spot), "spot": spot}


def _insert(compute: Compute, zone: str, body: dict, cfg: Config, left: Callable[[], float]) -> str:
    """Inserts the VM and waits for its operation as long as `cfg.insert_wait_s` and what is `left` of the budget allow.
    The error the operation failed with, or that of a request refused for quota or capacity (`Refused`), or "" when the VM is made
    or its operation is still running (then it is taken as started: a VM that never comes up is found by the plane's sweep)."""
    try:
        operation = compute.insert(zone, body)
    except Refused as e:
        return str(e)
    wait_s = min(cfg.insert_wait_s, left())
    outcome = compute.wait(zone, operation, wait_s) if wait_s > 0 else None
    return (outcome or {}).get("error", "")


def _no_room(error: str) -> str:
    """The code in an operation error that says there is no room or no quota; RuntimeError, with the error, for any other."""
    for code in NO_ROOM_CODES:
        if code in error:
            return code
    raise RuntimeError(error)


def _pair(kind: str, zone: str, on_demand: bool = False) -> str:
    return f"{'on demand ' if on_demand else ''}{kind}/{zone.rsplit('-', 1)[-1]}"  # "e2-standard-2/a"


# The states of a VM that is being deleted or has powered off. Such a VM found by the name of a launch is not answered: no runner
# will start on it, and the name cannot be inserted again until it is gone.
ENDING_STATES = ("STOPPING", "TERMINATED")


def launch(req: dict, compute: Compute, cfg: Config, now: Callable[[], float]) -> dict:
    """Starts the VM for a job and answers `{"id", "kind", "usd_per_hour", "spot"}`; the id is its instance name.

    The walk: for each machine type that fits (`choose`, cheapest family first) and in it each zone of `cfg.zones`, insert the VM
    and wait for the operation; no room or no quota there goes on to the next pair, anything else is RuntimeError. All within
    `cfg.budget_s` seconds as `now` counts them (a wait is cut to what is left); the budget spent before an insert is NoRoom. An
    insert whose operation is still running when its wait ends is answered as started. When every pair is out of room, a request
    for a spot VM with `cfg.on_demand_fallback` on gets one on-demand VM of the first type in the first zone (answered
    `"spot": False`, at the on-demand price, and without the preemption notice, which an on-demand VM has none to give; its
    own no-room or error is as any other's). Otherwise NoRoom.

    The VM's name is `vm_name(name, jit)`. A VM of that name in any zone is answered as it is, without an insert: so a request
    repeated exactly finds the VM its first call made. The plane sends none: each of its start attempts has a configuration of its
    own, so a VM whose launch answer was lost is not found again (see `vm_name`). One found `STOPPING` or `TERMINATED`
    (`ENDING_STATES`) is NoRoom: no runner will start on it, and its name cannot be inserted again until it is gone (a `TERMINATED`
    one is deleted by the sweep). ValueError for a request that cannot be served.
    """
    began = now()

    def left() -> float:
        return cfg.budget_s - (now() - began)

    ask = _ask(req)
    types = choose(ask.cpu, ask.ram_gb)
    if not types:
        raise ValueError(f"no machine type has {ask.cpu} vCPUs and {ask.ram_gb} GB of memory")
    script = render_startup(ask.jit, ask.fail, ask.max_minutes, ask.notice if ask.spot else None, cfg.runner_version)
    shutdown = render_shutdown(ask.notice if ask.spot else None)
    name = vm_name(ask.name, ask.jit)
    for zone in cfg.zones:
        vm = compute.get(zone, name)
        if vm is not None:
            if vm.get("status") in ENDING_STATES:
                raise NoRoom([(f"VM {name} in {zone}", vm["status"])], ask.spot)
            kind = vm["machineType"].rsplit("/", 1)[-1]
            return _answer(name, kind, vm.get("scheduling", {}).get("provisioningModel") == "SPOT")
    reasons: list[tuple[str, str]] = []
    for kind in types:
        for zone in cfg.zones:
            if left() <= 0:
                raise NoRoom(reasons + [(_pair(kind, zone), "no time left")], ask.spot)
            error = _insert(compute, zone, _vm_body(cfg, ask, zone, kind, ask.spot, script, shutdown), cfg, left)
            if not error:
                return _answer(name, kind, ask.spot)
            reasons.append((_pair(kind, zone), _no_room(error)))
    if cfg.on_demand_fallback and ask.spot:
        kind, zone = types[0], cfg.zones[0]
        if left() <= 0:
            raise NoRoom(reasons + [(_pair(kind, zone, True), "no time left")], ask.spot)
        plain = render_startup(ask.jit, ask.fail, ask.max_minutes, None, cfg.runner_version)
        error = _insert(compute, zone, _vm_body(cfg, ask, zone, kind, False, plain), cfg, left)
        if not error:
            return _answer(name, kind, False)
        reasons.append((_pair(kind, zone, True), _no_room(error)))
    raise NoRoom(reasons, ask.spot)


# /stop, /status and /sweep: delete a job's VM when the plane says its job is over, say whether it is still there, and delete
# the leftovers the plane never stopped.

def _ours(vm: dict, cfg: Config) -> bool:
    """Whether a VM carries the label `superci-plane=<cfg.plane_id>`, which is what makes it a job VM of this plane's."""
    return (vm.get("labels") or {}).get("superci-plane") == cfg.plane_id


def stop(name: str, compute: Compute, cfg: Config) -> None:
    """Deletes this plane's VM of that name in every zone of `cfg.zones` that has one; overlapping launches of one request can
    have made two. The name is made an instance name (`instance_name`, which the `id` that /launch answered already is) before it is
    looked for, and a VM of that name without this plane's label (another plane's, or none of ours) is left alone. No VM is no error:
    it is gone already, never was, or is not this plane's. RuntimeError when GCE fails."""
    name = instance_name(name)
    for zone in cfg.zones:
        vm = compute.get(zone, name)
        if vm is not None and _ours(vm, cfg):
            compute.delete(zone, name)



def status(name: str, compute: Compute, cfg: Config) -> str:
    """Whether this plane's VM of that name is still there, looked for in every zone of `cfg.zones` as `stop` does: "running"
    while one of this plane's VMs of that name is in a state other than `STOPPING` or `TERMINATED` (`ENDING_STATES`); "ending"
    while one is `STOPPING` (a VM taken back is, for up to about thirty seconds, while its notice may still be on its way to the
    plane); "gone" when every one is `TERMINATED`, or no zone has one. ValueError when every VM of that name is not this plane's:
    it is not this plane's to ask after. RuntimeError when GCE fails."""
    name = instance_name(name)
    found = [vm for vm in (compute.get(zone, name) for zone in cfg.zones) if vm is not None]
    ours = [vm for vm in found if _ours(vm, cfg)]
    if found and not ours:
        raise ValueError(f"the VM {name} is not this plane's")
    states = {vm.get("status") for vm in ours}
    if states - set(ENDING_STATES):
        return "running"
    return "ending" if "STOPPING" in states else "gone"

SWEEP_GRACE_MINUTES = 10  # what a VM is given beyond its `superci-max-minutes` before it is a leftover (a late start, a slow power-off)


def _leftover(vm: dict, now: float) -> bool:
    """Whether a job VM is for the sweep. A `TERMINATED` one always is: it powered itself off, and it costs its disk until it is
    deleted. Any other is when it was made more than its `superci-max-minutes` label plus `SWEEP_GRACE_MINUTES` before `now`; for that
    the label must be a whole number of minutes and `creationTimestamp` a time with an offset (GCE gives RFC 3339), and a VM that has
    no such label or timestamp, or an unreadable one, is not known to be overdue and is left alone."""
    if vm.get("status") == "TERMINATED":
        return True
    try:
        minutes = str(vm["labels"]["superci-max-minutes"])
        created = datetime.fromisoformat(vm["creationTimestamp"])
    except (KeyError, TypeError, ValueError):
        return False
    if not (minutes.isascii() and minutes.isdigit()) or created.tzinfo is None:
        return False
    return now - created.timestamp() > (int(minutes) + SWEEP_GRACE_MINUTES) * 60


def sweep(compute: Compute, cfg: Config, now: float) -> int:
    """Deletes this plane's leftover job VMs (`_leftover`) in every zone of `cfg.zones` and answers how many it deleted. A VM is this
    plane's when it carries the label `superci-plane=<cfg.plane_id>`: the listing is filtered by it, and a VM listed without it is
    skipped. `now` is epoch seconds, the wall clock (a `creationTimestamp` is one), not the monotonic clock `launch` is given.
    A delete that fails is logged, naming the zone and the VM, and the sweep goes on with the next one (that VM is tried again by
    the next sweep, and is not in the count). RuntimeError when GCE fails to list."""
    swept = 0
    for zone in cfg.zones:
        for vm in compute.list(zone, f"labels.superci-plane={cfg.plane_id}"):
            if _ours(vm, cfg) and _leftover(vm, now):  # (the filter selects this plane's VMs; this holds if it ever returns more)
                try:
                    compute.delete(zone, vm["name"])
                except Exception as e:
                    log.error("sweep: could not delete %s in %s: %s", vm["name"], zone, e)
                    continue
                swept += 1
    return swept


_FAILURES = (google_errors.GoogleAPICallError, requests.exceptions.RequestException)


def _refused(error: Exception) -> RuntimeError:
    """What GCE's answer, or the lack of one, is to the launcher: a failure of this call, said in the error's own words."""
    return RuntimeError(f"{type(error).__name__}: {getattr(error, 'message', None) or error}")


_QUOTA_MESSAGE = re.compile(r"\bQuota '[^']+' exceeded")


def _insert_refused(error: Exception) -> Exception:
    """What GCE's refusal of an insert request is to the launcher. Refused, with its code in front, when it is for quota (the reason
    `quotaExceeded` or QUOTA_EXCEEDED among the error's reasons, or a message such as "Quota 'CPUS' exceeded") or for capacity (a
    message or a reason with one of NO_ROOM_CODES); `_refused`'s RuntimeError for any other, such as PERMISSION_DENIED."""
    message = str(getattr(error, "message", None) or error)
    reasons = {getattr(error, "reason", None)} | {e.get("reason") for e in getattr(error, "errors", None) or [] if isinstance(e, dict)}
    if "quotaExceeded" in reasons or _QUOTA_MESSAGE.search(message):
        reasons.add("QUOTA_EXCEEDED")
    for code in NO_ROOM_CODES:
        if code in message:
            return Refused(message)
        if code in reasons:
            return Refused(f"{code}: {message}")
    return _refused(error)


class GoogleCompute(Compute):
    """`Compute` over Google's client library, with the credentials its environment has (on the launcher's VM, that VM's service
    account). Not covered by the tests, which use a stand-in; the first real launch is its test.

    The clients are made when first used, since making one looks for credentials and an import has none to find. An answer of
    GCE's that is an error, or a request that timed out, is RuntimeError(message), which the app answers 503 for; each call is
    cut at `cfg.insert_wait_s` seconds and not retried, so that a stalled API cannot hold a launch past its budget.
    """

    def __init__(self, cfg: Config):
        self.project = cfg.project
        self.timeout_s = cfg.insert_wait_s

    @cached_property
    def _instances(self):
        from google.cloud import compute_v1  # (imported here: it takes most of a second)
        return compute_v1.InstancesClient()

    @cached_property
    def _operations(self):
        from google.cloud import compute_v1
        return compute_v1.ZoneOperationsClient()

    def insert(self, zone: str, body: dict) -> str:
        from google.cloud import compute_v1
        instance = compute_v1.Instance.from_json(json.dumps(body))  # (camelCase, as the REST API has it)
        try:
            operation = self._instances.insert(project=self.project, zone=zone, instance_resource=instance, retry=None, timeout=self.timeout_s)
        except _FAILURES as e:
            raise _insert_refused(e) from e
        return operation.name

    def wait(self, zone: str, operation: str, timeout_s: float) -> dict | None:
        from google.cloud import compute_v1
        try:
            done = self._operations.wait(project=self.project, zone=zone, operation=operation, timeout=timeout_s)
        # (What the REST transport raises when the time is up is not shown by its code: a requests timeout, or GCE's own 504.)
        except (requests.exceptions.Timeout, google_errors.DeadlineExceeded, google_errors.GatewayTimeout):
            return None
        except _FAILURES as e:
            raise _refused(e) from e
        if done.status != compute_v1.Operation.Status.DONE:
            return None  # (GCE's own wait ends after about two minutes, done or not)
        if done.error.errors:
            return {"error": "; ".join(f"{e.code}: {e.message}" for e in done.error.errors)}
        return {}

    def get(self, zone: str, name: str) -> dict | None:
        from google.cloud import compute_v1
        try:
            instance = self._instances.get(project=self.project, zone=zone, instance=name, retry=None, timeout=self.timeout_s)
        except google_errors.NotFound:
            return None
        except _FAILURES as e:
            raise _refused(e) from e
        return json.loads(compute_v1.Instance.to_json(instance))

    def delete(self, zone: str, name: str) -> None:
        try:
            self._instances.delete(project=self.project, zone=zone, instance=name, retry=None, timeout=self.timeout_s)
        except google_errors.NotFound:
            return
        except _FAILURES as e:
            raise _refused(e) from e

    def list(self, zone: str, label_filter: str) -> list[dict]:
        from google.cloud import compute_v1
        try:
            # (The filter travels in the request: the client's list() takes no filter keyword.)
            request = compute_v1.ListInstancesRequest(project=self.project, zone=zone, filter=label_filter)
            instances = self._instances.list(request=request, retry=None, timeout=self.timeout_s)
            return [json.loads(compute_v1.Instance.to_json(i)) for i in instances]
        except _FAILURES as e:
            raise _refused(e) from e

