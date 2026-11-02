# A stand-in for Google Compute Engine in the launcher's tests: it models only what the launcher asks of it.
import copy
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from launcher import Refused

EXHAUSTED = "ZONE_RESOURCE_POOL_EXHAUSTED: The zone does not have enough resources available to fulfill the request."


def rfc3339(epoch: float) -> str:
    """An epoch time as GCE writes a `creationTimestamp`: RFC 3339 with milliseconds and the project's UTC offset."""
    return datetime.fromtimestamp(epoch, timezone(timedelta(hours=-8))).isoformat(timespec="milliseconds")


class Clock:
    """The time a test lets pass: calling it gives the time, `step` moves it on."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def step(self, seconds: float) -> None:
        self.t += seconds


def clock() -> Clock:
    return Clock()


class FakeCompute:
    """An in-memory `Compute`.

    `plan` says what each (zone, machine type) answers to an insert; a pair it does not name has no room:
      "ok"               the VM is made
      "exhausted"        the operation fails with ZONE_RESOURCE_POOL_EXHAUSTED
      "slow"             the VM is made, but its operation is still running when `wait` gives up
      "error:<message>"  the operation fails with that message
      "refused:<message>"   GCE refuses the insert request itself for want of quota or capacity: `insert` raises Refused(message)
      "rejected:<message>"  GCE refuses the insert request itself for another reason: `insert` raises RuntimeError(message)
    A key `(zone, machine type, "on-demand")` is what an insert without the SPOT fields gets; the plain key serves both.

    With a `clock` given, an answer takes `answer_s` seconds to come and a wait that is shorter than that gives up
    after its own timeout, still running. Only a VM that was made is seen by `get`, `list` and `delete`.

    A VM made is `RUNNING` and has the `creationTimestamp` of `wall()`, the time of the wall clock (epoch seconds) when it was
    made, as GCE stamps it; a test that needs an old VM sets `wall` before the insert.
    """

    def __init__(self, plan: dict | None = None, clock: Clock | None = None, answer_s: float = 0.0):
        self.plan = dict(plan or {})
        self.clock = clock
        self.answer_s = answer_s
        self.wall: Callable[[], float] = time.time
        self.inserted: list[dict] = []  # {"zone", "body"} of every insert, in order
        self.waits: list[tuple[str, float]] = []  # (zone, timeout_s) of every wait
        self.deleted: list[tuple[str, str]] = []
        self.vms: dict[tuple[str, str], dict] = {}
        self._outcomes: dict[str, str] = {}

    @property
    def pairs(self) -> list[tuple[str, str]]:
        """The (zone, machine type) of every insert, in order."""
        return [(i["zone"], i["body"]["machineType"].rsplit("/", 1)[-1]) for i in self.inserted]

    def _step(self, seconds: float) -> None:
        if self.clock is not None:
            self.clock.step(seconds)

    def insert(self, zone: str, body: dict) -> str:
        kind = body["machineType"].rsplit("/", 1)[-1]
        on_demand = body.get("scheduling", {}).get("provisioningModel") != "SPOT"
        outcome = self.plan.get((zone, kind, "on-demand")) if on_demand else None
        outcome = outcome or self.plan.get((zone, kind), "exhausted")
        self.inserted.append({"zone": zone, "body": copy.deepcopy(body)})
        if (zone, body["name"]) in self.vms:
            raise RuntimeError("ALREADY_EXISTS: the resource already exists")
        if outcome.startswith("refused:"):
            raise Refused(outcome.removeprefix("refused:"))
        if outcome.startswith("rejected:"):
            raise RuntimeError(outcome.removeprefix("rejected:"))
        if outcome in ("ok", "slow"):
            self.vms[(zone, body["name"])] = {**copy.deepcopy(body), "status": "RUNNING", "creationTimestamp": rfc3339(self.wall())}
        operation = f"operation-{len(self.inserted)}"
        self._outcomes[operation] = outcome
        return operation

    def wait(self, zone: str, operation: str, timeout_s: float) -> dict | None:
        self.waits.append((zone, timeout_s))
        outcome = self._outcomes[operation]
        if outcome == "slow" or self.answer_s > timeout_s:
            self._step(timeout_s)
            return None
        self._step(self.answer_s)
        if outcome == "ok":
            return {}
        if outcome == "exhausted":
            return {"error": EXHAUSTED}
        return {"error": outcome.removeprefix("error:")}

    def get(self, zone: str, name: str) -> dict | None:
        return copy.deepcopy(self.vms.get((zone, name)))

    def delete(self, zone: str, name: str) -> None:
        self.deleted.append((zone, name))
        self.vms.pop((zone, name), None)

    def list(self, zone: str, label_filter: str) -> list[dict]:
        # (Only the form `labels.<key>=<value>`, or no filter at all.)
        want = re.fullmatch(r'labels\.([\w-]+)\s*=\s*"?([\w-]*)"?', label_filter)
        assert want or not label_filter, label_filter
        return [copy.deepcopy(vm) for (z, _), vm in self.vms.items()
                if z == zone and (not want or vm.get("labels", {}).get(want[1]) == want[2])]
