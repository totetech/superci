# The launcher's HTTP side: GET /health is open; everything else needs a token the control plane signed.
# Run with `uvicorn app:api`; the configuration is read from the environment when this module is imported.
import hmac
import logging
import threading
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from launcher import Compute, Config, GoogleCompute, NoRoom, launch as start_vm, status as vm_status, stop as stop_vm, sweep as sweep_vms, verify

cfg = Config.from_env()
api = FastAPI()
gce = GoogleCompute(cfg)  # made once, here; it makes its Google clients (which look for credentials) when first used
log = logging.getLogger(__name__)

SWEEP_EVERY_S = 600
last_sweep = time.monotonic()  # when the VMs were last swept, on the monotonic clock; starting at the process start, so its first request does not sweep
_sweep_lock = threading.Lock()


def get_compute() -> Compute:
    """The Compute that routes use. A test replaces it: `api.dependency_overrides[get_compute] = lambda: fake`."""
    return gce


async def checked(request: Request) -> dict:
    """The JSON body of a request that carries a token the plane signed for this launcher's registered URL.

    The audience is the registered URL, not the address the request came to: a token for the machine's own
    address is a token for something else.
    """
    try:
        verify(request.headers.get("authorization", ""), cfg.public_url, cfg.trust, time.time())
    except PermissionError as e:
        raise HTTPException(status_code=401, detail=f"not allowed: {e}")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="the body is not JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="the body is not a JSON object")
    return body


def sweep_first(compute: Compute) -> None:
    """Sweeps the VMs before a /stop is answered when more than `SWEEP_EVERY_S` seconds have passed since the last sweep. A failure
    is logged and the request goes on. The time is taken when the sweep begins, so of requests that arrive together one sweeps, and
    a sweep that failed is not tried again by the next request but after the same time."""
    global last_sweep
    with _sweep_lock:
        if time.monotonic() - last_sweep <= SWEEP_EVERY_S:
            return
        last_sweep = time.monotonic()
    try:
        sweep_vms(compute, cfg, time.time())  # (the wall clock: a VM's creation time is one)
    except Exception:  # any failure: a request is not to fail because it came when a sweep was due
        log.exception("the sweep before a request failed")


@api.get("/health")
async def health():
    return {"agent": "gcp"}


@api.post("/launch")
def launch(body: dict = Depends(checked), compute: Compute = Depends(get_compute)):
    """Starts a job's VM (see `launcher.launch`). 429 means no room now and the plane is to wait, 503 that it failed and may be
    tried again, 400 that the request is refused. A plain `def`: Google's client blocks, so the route runs in a thread, not in the
    event loop that answers /health. It does not sweep first, as /stop does: a launch has a time budget to keep."""
    try:
        return start_vm(body, compute, cfg, time.monotonic)
    except NoRoom as e:
        raise HTTPException(status_code=429, detail=f"no {'spot ' if e.spot else ''}room in {cfg.region} ({e})")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@api.post("/stop")
def stop(body: dict = Depends(checked), compute: Compute = Depends(get_compute)):
    """Deletes this plane's VM whose instance name is `id` (see `launcher.stop`), after the sweep if one is due, and answers
    `{"stopped": true}` whether or not it was there (a VM of that name that is not this plane's is left, and answered the same).
    400 for no `id`, 503 when GCE failed and the plane may try again."""
    sweep_first(compute)
    name = body.get("id")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="id is missing or not text")
    try:
        stop_vm(name, compute, cfg)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"stopped": True}



@api.post("/status")
def status(body: dict = Depends(checked), compute: Compute = Depends(get_compute)):
    """Whether this plane's VM whose instance name is `id` is still there (see `launcher.status`): `{"state": "running"}`;
    `{"state": "ending"}` while it is stopping; `{"state": "gone"}` when it is terminated or no zone has it. 400 for no `id` and for a
    VM of that name that is not this plane's, 503 when GCE failed and the plane may ask again."""
    name = body.get("id")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="id is missing or not text")
    try:
        return {"state": vm_status(name, compute, cfg)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

@api.post("/sweep")
def sweep(x_superci_sweep: str = Header(""), compute: Compute = Depends(get_compute)):
    """Deletes this plane's leftover VMs (see `launcher.sweep`) and answers `{"swept": n}`. It is for whatever calls it on a timer, not
    for the plane, so it takes no plane token but the secret in the header `x-superci-sweep`; without it, or with another, it
    is 404, as if there were no such route (and no secret at all in the configuration shuts it)."""
    global last_sweep
    if not cfg.sweep_secret or not hmac.compare_digest(x_superci_sweep.encode(), cfg.sweep_secret.encode()):
        raise HTTPException(status_code=404)
    last_sweep = time.monotonic()  # (this is the last sweep, so a request in the next ten minutes need not)
    try:
        return {"swept": sweep_vms(compute, cfg, time.time())}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
