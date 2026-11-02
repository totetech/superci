# SuperCI launcher for GCP

An HTTP agent that starts a job's machine in a Google Cloud project when the SuperCI control plane asks.
It answers `GET /health` without a token. Every other route needs a token the control plane signed: ES256,
`iss` the plane's URL, `sub` `plane:<plane id>`, `aud` this launcher's own registered URL (`PUBLIC_URL`, not the
address a request happened to arrive at), and an `exp` in the future. A token that fails any of these is answered
with 401 and `not allowed: <why>`.

`POST /launch` starts a job's VM. It picks the smallest standard machine type that fits (`e2`, then `c4d`, then `n2d`) and
walks them, and in each the zones, inserting the VM until one has room, all within about eight seconds (each insert
waits at most five). It answers `{"id", "kind", "usd_per_hour", "spot"}`; the id is the instance name, the runner name made
an instance name and cut to 54 characters, then `-` and the first 8 hex digits of the SHA-256 of the request's `jit`. Asking
again with the same `jit` answers the VM already made; a new `jit` under the same runner name gets a new VM, and a VM of that
name found `STOPPING` or `TERMINATED` is answered 429. `429 {"detail"}` means no room (the plane waits and asks again),
`503` that GCE failed another way (the plane retries), `400` that the request is refused (for instance a GitLab job, which
has no `jit`). With `ON_DEMAND_FALLBACK=1`, when no spot room is found it starts one on-demand VM of the first machine type in
the first zone instead.

`POST /stop` with `{"id": "<instance name>"}` deletes that VM (the name is made an instance name first) in every zone that has
it and answers `{"stopped": true}`, also when it is gone already. Only a VM labelled `superci-plane=<plane id>` is deleted; one
of that name without the label is left alone and the answer is the same.

`POST /status` with `{"id": "<instance name>"}` answers `{"state": "running"}` while that VM exists and is not ending,
`{"state": "ending"}` while it is `STOPPING`, and `{"state": "gone"}` when it is `TERMINATED` or no zone has it. Only `gone`
ends a job at the control plane: a VM taken back is `STOPPING` while its notice may still be on its way. The control plane asks
it, on its sweep, for a job started from a computer whose machine may have gone, since no code host reports that job's end. A
VM of that name that is not this plane's answers 400.

`POST /sweep` deletes this plane's job VMs (label `superci-plane=<plane id>`) in every zone that are `TERMINATED`, or older than
their `superci-max-minutes` label plus 10 minutes, and answers `{"swept": n}`, the number deleted. A VM that is not `TERMINATED`
and whose label or creation time cannot be read is left alone. A delete that fails is logged (zone and VM) and the sweep goes on
with the next VM. `/sweep` is for something that calls it on a timer, so it takes no plane token: the header `x-superci-sweep`
must equal `SWEEP_SECRET`, or the answer is 404. `/stop` also runs that sweep first when more than 600 seconds have passed
since the last one (the first request after the start does not); a failure in it is logged and the stop is answered. `/launch`
does not sweep, so that a launch keeps its time budget.

## Configuration

Read from the environment when the service starts; a missing required variable stops the start.

| Variable | Meaning |
|---|---|
| `PROJECT` | GCP project that holds the job VMs (required) |
| `REGION` | region of the job VMs, for example `us-west4` (required) |
| `ZONES` | comma-separated zones to try, in order; default `<REGION>-a,<REGION>-b,<REGION>-c`. A value that names no zone, such as `,`, stops the start |
| `NETWORK`, `SUBNET` | VPC network and subnet of the job VMs (required). `SUBNET` is a bare name (the subnet of that name in `PROJECT` and `REGION`) or a path GCE takes, such as `projects/<project>/regions/<region>/subnetworks/<name>`; the VM's network is the subnet's |
| `IMAGE_FAMILY` | image family of the runner image (required) |
| `RUNNER_VERSION` | GitHub runner version the startup script expects (required) |
| `ON_DEMAND_FALLBACK` | `1` starts an on-demand VM when no spot room is found; anything else is off |
| `PUBLIC_URL` | the URL registered with the control plane; the audience of its tokens (required) |
| `TRUSTED_ISSUER` | the plane's URL (required) |
| `TRUSTED_SUBJECT` | `plane:<plane id>` (required) |
| `TRUSTED_KEYS` | the plane's JWKS as JSON, from `<plane>/.well-known/jwks.json` (required) |
| `SWEEP_SECRET` | shared secret for `/sweep` (required) |
| `BEHIND_GATEWAY` | `1` on path B, where API Gateway sits in front |
| `EXTERNAL_ADDRESS` | `0` on path B, where job VMs have no external address |

## Run

Python 3.12 (Ubuntu 24.04's).

```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:api --host 127.0.0.1 --port 8080
```

## Test

```
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -q
```

The tests bind no port; they call the app through FastAPI's `TestClient`.
