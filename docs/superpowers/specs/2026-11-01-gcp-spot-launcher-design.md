# GCP spot runners for SuperCI, for one account

Date: 2026-11-01. Status: draft for review.

## Intent

Run Tote's GitHub Actions jobs on GCP spot VMs in us-west4, one fresh machine per job, through SuperCI, at a cost near the estimate in the comparison page (about $450 a month for one million 2-vCPU minutes, against $6,000 on GitHub's runners). This is for one account's own use, not an upstream contribution: Google sign-in in the CLI and the test stand-ins for Google's APIs are out of scope. The dashboard adds, lists and removes a GCP launcher and shows a GCP job's machine and reclaim; that was added on 2026-11-02 at the account owner's request (see Task 19 of the plan).

The user decided: GCP us-west4, spot only, the first provider to build. Everything marked "my call" below is the author's default and can be changed without changing the shape of the work.

## How SuperCI is put together, in the parts this touches

- The control plane (`crates/core`) is runtime-neutral. It is deployed by the `superci` CLI to AWS Lambda, a Cloudflare Worker or Modal. It hears GitHub's job webhooks, claims each job, picks a provider in `place()` and asks for a machine in `start()` (both in `crates/core/src/plane.rs`).
- A provider is one of three kinds: `Runner::Aws` (the plane calls EC2 itself), `Runner::Own` (the plane's own runtime starts containers) or `Runner::Launcher` (an HTTP agent in another cloud with `/launch` and `/stop`, trusting an ES256 JWT the plane signs). GCP is added as a launcher.
- The launcher contract, as the Modal launcher implements it in `crates/cli/src/modal_launcher.py`: `GET /health` answers `{"agent": "<cloud>"}`; `POST /launch` takes `{name, job, repo, max_minutes, cpu, ram_gb, disk_gb, location, gpu?, jit | gitlab: {url, token}, fail?}` and answers `{"id", "kind"}`; `POST /stop` takes `{"id"}`. Both POSTs carry `Authorization: Bearer <JWT>` with `iss` = the plane's URL, `sub` = `plane:<plane id>`, `aud` = the launcher's origin, `exp` two minutes out. The launcher pins the plane's public keys, read once from `<plane>/.well-known/jwks.json`.
- The plane calls `/stop` when GitHub reports the job complete, when a job is cancelled, and from its once-a-minute sweep for machines that passed the job's time limit or whose runner has not come online at GitHub in time: eight minutes for an AWS machine, three for a launcher's (`plane.rs:5041`). A late machine is stopped and the job placed again, three times, then failed.
- On a Cloudflare plane the launch runs inside GitHub's webhook request, because the Cloudflare runtime does not implement `Timer::later` (the AWS and Modal runtimes answer GitHub first and launch after). GitHub counts a delivery as failed after ten seconds, though the plane still processes it. A launcher called from a Cloudflare plane therefore has to answer within a few seconds.
- Launchers are kept in the plane's settings as `LAUNCHERS`, a JSON list of `{cloud, url}`, and a `Pool` for the cloud in the routing order carries `max_jobs` and `monthly_usd`.

## Goals

- A job labelled `runs-on: superci` (or `superci/cpu=4` and so on) gets a spot VM in us-west4 within about a minute and the machine is deleted when the job ends.
- A VM taken back by Google reports it to the plane before it goes, so SuperCI reruns the job as it does after an AWS spot reclaim.
- The plane's job pages and limits show the right hourly price for GCP machines.
- Nothing runs between jobs: no warm pool, no standing job machine. The one standing item is the launcher's free-tier micro VM on path A, or the NAT gateway on path B.

## Non-goals

- A control plane on GCP. The plane runs on Cloudflare (my call: it needs no server, and Cloudflare's Durable Objects with alarms have been on the free plan since April 2025). Whether the free plan's per-request limits fit the plane has not been tried by anyone as far as the repository shows; the setup starts on the free plan and moves to Workers Paid, $5 a month, if a deploy or a limit refuses. AWS Lambda or Modal would also work; the launcher does not care.
- Windows, arm64 or GPU machines on GCP. x64 Linux only in this version.
- SuperCI's cache and Docker layer store, which are S3-only. Jobs use GitHub's own cache service through `actions/cache`, as Cloudflare and Modal runners do today.
- Google sign-in in the CLI, and testkit stand-ins for Google's APIs. (Dashboard pages for GCP were listed here until 2026-11-02, when the account owner asked for them: the dashboard now adds, lists and removes a GCP launcher and shows a GCP job's machine and reclaim; see Task 19 of the plan.)
- GitLab jobs. Docker is available on the VMs so nothing prevents it later, but it is not tested here.

## Anti-goals

- A warm pool or a shared long-lived runner. The whole saving comes from paying only while a job runs.
- Giving the job VMs any GCP credentials. A job runs as root on its machine; the VM must not be able to touch the project. Only the launcher VM holds a service account.
- Forking so far from upstream that updates cannot be merged. Core changes are kept small and generic ("a launcher may say its price", "a launcher may be spot") rather than GCP-shaped.

## Constraints

- The CLI carries the built control planes inside it (`crates/programs-planes`), and its build script needs Rust, worker-build, zig with cargo-zigbuild and zip, and builds mkfs.erofs. A core change means rebuilding the Cloudflare plane (WebAssembly) and the CLI. The AWS and Modal planes need not be rebuilt: their prebuilt artifacts can be taken from the published `superci-programs-planes` crate's `embedded/` folder and placed where `build.rs` looks. The builder decides whether to shortcut this way or run `build.sh` whole.
- GCP creation limits: new projects start with a low spot CPU quota per region. At one million minutes a month the fleet averages about 25 machines and 50 vCPUs with business-hour peaks several times that, so a quota request for a few hundred spot vCPUs in us-west4 is part of setup.
- Cloudflare's free plan allows 100,000 Worker requests a day. At 100,000 jobs a month the plane sees roughly three webhooks per job plus a sweep a minute plus dashboard traffic, around 12,000 a day. Fine, but worth knowing.
- The 20-jobs-at-once default per provider (`JOBS_AT_ONCE`) is below the average fleet. The pool's `max_jobs` must be raised at registration.
- GCP spot prices change up to once a day. The launcher's price table is a snapshot and is refreshed by hand.
- **On path B, Cloud NAT is a cost the comparison page does not have.** It charges $0.045 a GiB processed, inbound and outbound alike, plus about $0.044 an hour for the gateway once more than 32 VMs use it and $0.005 an hour for its address. The fixed part is about $40 a month. The per-GiB part depends on what the jobs pull from outside Google: at 100,000 jobs a month, 100 MB a job is $450, 300 MB is $1,350, and 1 GB is $4,500. The custom image and the registry mirrors above exist to push that number down; GitHub traffic itself (checkout, actions, `actions/cache`) cannot be moved. Measure a week of real jobs before trusting any of these three figures. A per-project exception to the external-address policy for the CI project would remove this cost entirely; the job VMs have no ingress rules either way.

## The design

### 1. The GCP launcher

A small HTTP service in the Tote GCP project. Python with FastAPI, mirroring the Modal launcher, using the `google-cloud-compute` client. The plane calls it with a token the plane signed and has no Google identity, so the launcher must be reachable without one.

Two organisation policies, confirmed with the user on 2026-11-02, rule out the plain ways of doing that: unauthenticated Cloud Run services are refused (no `allUsers` binding), and VMs may not have external IP addresses. The user is asking for a per-project exception to the external-address policy for the CI project (requested 2026-11-02, pending). The design therefore has two network paths, identical everywhere else. Path A is the plan; path B is built only if the exception is refused.

**Path A, exception granted.** The launcher runs on one small always-on VM: an e2-micro in us-east1, which GCP's free tier covers, running the service under systemd behind Caddy for automatic TLS on a DNS name of the team's choosing (a `tote.ai` subdomain, or the VM's address under `sslip.io`). It creates the job VMs in us-west4 through the API; it does not need to be there itself. A firewall rule admits port 443 to the launcher only. The launcher verifies the plane's token itself from the `Authorization` header, exactly as the Cloudflare and Modal launchers do. Job VMs get an ephemeral external address for egress to GitHub and no ingress rules; there is no NAT and no per-gigabyte charge. Its configuration is environment variables in the systemd unit: `TRUSTED_ISSUER`, `TRUSTED_SUBJECT`, `TRUSTED_KEYS` (the plane's JWKS as JSON), `PUBLIC_URL`, `PROJECT`, `REGION` (us-west4), `NETWORK`, `IMAGE_FAMILY`, and `ON_DEMAND_FALLBACK` (default off, see below). Its sweep runs from a background thread every ten minutes.

**Path B, exception refused.**

- The launcher is a Cloud Run service in us-west4 with IAM authentication on, behind **API Gateway**. The gateway's OpenAPI config names the plane as the token issuer (`x-google-issuer` = the plane's URL, `x-google-jwks_uri` = `<plane>/.well-known/jwks.json`, `x-google-audiences` = the gateway's own URL) and routes to the Cloud Run service with `x-google-backend`, calling it with the gateway's service account, which holds `roles/run.invoker` on that one service. The gateway validates the signature; it replaces the `Authorization` header and hands the validated claims to the launcher base64url-encoded in `X-Apigateway-Api-Userinfo`. The launcher checks `iss`, `sub` (`plane:<plane id>`), `aud` and `exp` from that header and refuses anything else. The plane registers the gateway's URL as the launcher's.
- **To confirm first on this path, before anything else is built:** that API Gateway accepts an ES256-signed token. Google's page lists the key formats it reads (a JWK set, X.509) and not the algorithms, and the plane signs only ES256. A ten-minute test with a throwaway gateway config and a token minted with the plane's key settles it. If it is refused, the remaining way in without an `allUsers` binding or an external address is a Cloudflare Tunnel from a VM with no external address (outbound only, through Cloud NAT) to a hostname on a domain held in the team's Cloudflare account, and the builder switches to that.
- The launcher's configuration is the same environment variables, on the Cloud Run service, with `PUBLIC_URL` being the gateway's URL. It runs its sweep on each request that arrives after ten minutes have passed since the last one, since a scale-to-zero service has no background thread; a Cloud Scheduler job calls `/sweep` with a shared secret every ten minutes so quiet periods are covered too.
- Job VMs get no external address. Egress to the internet goes through one Cloud NAT gateway in us-west4; traffic to Google services goes through Private Google Access and is not charged by NAT. The constraint on NAT's cost below applies.

On either path the launcher's own service account carries `compute.instanceAdmin.v1` on the project and nothing else; job VMs get no service account.

Endpoints:

- `GET /health` → `{"agent": "gcp"}`.
- `POST /launch` → creates one VM and answers `{"id": "<instance name>", "kind": "<machine type>", "usd_per_hour": <number>, "spot": true|false}`. The id is the instance name, which is the runner name from the request (unique per job, lowercase, within GCE's 63-character limit after the launcher sanitises it). The plane keeps the id and passes it to `/stop`.
  The call is bounded to about eight seconds in all, because of the webhook constraint above. The launcher issues an insert and waits on its operation for up to a few seconds; a capacity or quota error that completes within the budget moves it on to the next (zone, family) pair; an insert still running when the budget is spent is answered as started, and a VM that then never comes up is caught by the plane's sweep. Status codes carry the meaning the plane acts on: `200` started; `429` with `{"detail": "<why>"}` when every pair answered no room or quota (the plane makes the job wait, see core change 7; `detail` because that is the field the plane already reads from a launcher's error answers); `503` with the reason for any other failure (the plane retries twice, then fails the job); `400` for a request the launcher refuses outright (the plane fails the job at once, without retries).
- `POST /stop` → deletes the instance by name, in every zone of the region it may be in, and answers `{"stopped": true}` whether or not it was still there.
- `POST /status` → takes `{"id"}` with the same token as `/stop` and the same ownership rule (only a VM labelled `superci-plane=<plane id>`), looks for the instance in every zone `/stop` looks in, and answers `200 {"state": "running"}` while it exists and is neither `STOPPING` nor `TERMINATED`, `200 {"state": "ending"}` while it is `STOPPING`, and `200 {"state": "gone"}` when it is `TERMINATED` or no zone has it; `400` for a missing `id` or a VM of that name that is another plane's, `503` when Compute fails. The plane's once-a-minute sweep asks it about the job of a run from a computer (`superci runs create`) whose runner has begun the job and whose machine started more than two minutes ago, and only `"gone"` ends the job: GCE deletes a spot VM it takes back, the runner may not report the job's end first, and no GitHub event reports it for such a run. `"ending"` is asked again at the next sweep, because a VM taken back is `STOPPING` for up to about thirty seconds while its notice may still be on its way to the plane, and a job ended before the notice arrives is not started again. The job then fails, and its run starts it again on an on-demand VM when the VM had posted its reclaim notice. A GitHub or GitLab job's machine is not asked: the code host reports a lost runner, and the job is run again from there. Added 2026-11-02 after the preemption drill (Task 20 of the plan).
- `POST /sweep` → deletes any instance labelled by this launcher that is older than its own `max_minutes` label plus ten minutes, or that is in `TERMINATED` state (a VM that powered itself off but whose `/stop` was missed). Called by Cloud Scheduler every ten minutes with a shared secret header, and run inline by the launcher when a request finds the last sweep older than ten minutes. This is belt and braces; the plane's own sweep does the same through `/stop`.

Machine choice, my call: the request's `cpu` and `ram_gb` are matched to the smallest standard size that fits, in this family order: `e2` (cheapest, shared cores), then `n2d`, then `c4d` (dedicated, newest, needs Hyperdisk Balanced rather than pd-balanced for its boot disk). The launcher tries each (zone, family) pair across us-west4-a, b and c in that order and moves on when Compute answers `ZONE_RESOURCE_POOL_EXHAUSTED` or a quota error. The family order lives in code with a comment, not in configuration; `e2` first is the user's cost priority, and the comparison page notes it may run jobs slower than `c4d`. A timing test on a real workflow before and after switching the order is the way to settle it.

When every pair is exhausted and `ON_DEMAND_FALLBACK` is off (the default), `/launch` answers `429` and the job waits: the plane tries it again at each sweep, as an AWS job with no spot machine anywhere waits, and fails it only after a day, which is the plane's existing rule for jobs waiting for room. A quota error is treated the same as no capacity, since both clear with time and neither is the job's fault. My call: during a business-hour spot shortage the alternative, which is what the code does today for a `503`, fails every queued job on GitHub within about a minute. With the fallback on, the launcher starts an on-demand VM of the first family instead and reports `"spot": false` and the on-demand price. Default off, my call: the user said spot is the point, and the plane's monthly limit should not be surprised by on-demand prices.

The VM:

- 60 GB boot disk (pd-balanced; Hyperdisk Balanced for c4d), no service account at all, in a dedicated VPC `superci` with a subnet in us-west4 that has Private Google Access on and no ingress firewall rules. Egress as the network path says: an ephemeral external address on path A, Cloud NAT on path B. Labels: `superci-plane`, `superci-job`, `superci-repo`, `superci-max-minutes`.
- A **custom image** built from Ubuntu 24.04 with Docker, the tools GitHub's image has, and GitHub's runner preinstalled, so a boot downloads nothing and comes up in well under a minute. On path B this is what keeps a few hundred megabytes a boot off NAT's meter; on path A it is still the cheapest way to make boots fast and predictable, so it is built on both. The image is made once by a script that creates a VM, runs the install, and saves it to the `superci-runner` image family; the launcher takes the family from `IMAGE_FAMILY`.
- On path B, Docker's daemon in the image is configured to pull through an Artifact Registry remote repository that mirrors Docker Hub (and another for ghcr.io if jobs use it), so image layers come over Private Google Access and are cached in the project rather than crossing NAT on every job. Whether to do the same for npm or PyPI is left to the builder, by what the workflows pull. On path A the mirror is optional.
- Provisioning model SPOT with `instanceTerminationAction: DELETE` and no automatic restart, so a reclaimed VM leaves no disk behind. With `"spot": false` from the request (a label with `spot=false`, or a rerun after a reclaim, see 2 below), an on-demand VM.
- A startup script, generated by the launcher from a template, that does in order: schedule a hard stop at `max_minutes` (`shutdown -h +N`); install Docker and the few tools GitHub's image has that a stock Ubuntu lacks (git, jq, zip, unzip, xz, zstd, build-essential), as the Modal launcher's image does; create the `runner` user and fetch GitHub's runner at a pinned version; start a background watcher that long-polls the metadata server's `instance/preempted` endpoint and, when it flips to TRUE, POSTs to the notice link from the request; run `./run.sh --jitconfig <jit>` as the runner user (or the failing-runner hook when `fail` is given, copied from the Modal launcher); then `poweroff`. The script logs to the serial console so a failed boot can be read with `gcloud compute instances get-serial-port-output`.
- Boot to runner-ready on the custom image is expected under a minute, and the plane still gives a GCP machine eight minutes (core change 6) so a slow boot does not get a job swept and re-placed.

Price reporting: a table in the launcher of spot and on-demand hourly prices for the sizes it can pick, in us-west4, with the date it was taken, read from the same dataset as the comparison page. `usd_per_hour` in the `/launch` answer is the picked type's price. Left to the builder: whether to add the external IP and disk per-hour extras into that number (the comparison page counts them separately; the plane has no field for them for launcher jobs).

### 2. Core changes in the fork

Each is small and generic. File references are to the tree at v0.19.21.

1. **The fourth cloud's name.** `CLOUDS` in `crates/core/src/spec.rs` gains `"gcp"` (and the test string that lists the clouds). `CLOUD_NAMES` in `plane.rs` gains `("gcp", "GCP")`. `fail_fast` in `plane.rs` adds `"gcp"` to the launcher clouds it may use for the failing runner.
2. **Capacity.** `Capacity::gcp()` in `spec.rs`: x64, Linux, `ubuntu24` only, up to 64 CPU, 256 GB, 500 GB disk, standard machine 2 CPU and 8 GB, 4 GB per CPU, no GPUs, no scratch. `capacity_of` in `plane.rs` returns it for `"gcp"` instead of falling through to Modal's.
3. **Price from the launcher.** In the `Runner::Launcher` branch of `start()`, when the `/launch` answer carries `usd_per_hour`, use it for `job.usd_per_hour` instead of the Cloudflare-or-Modal formula. Clouds whose launchers do not say it keep the formula.
4. **Spot through a launcher.** In the same branch, when the launcher's cloud is spot-capable (`"gcp"`): set `job.notice` to a random token as the AWS branch does, and add to the body `"notice": "<plane>/interrupted?runner=<name>&t=<token>"` and `"spot": <bool>`. `spot` is false when the label says `spot=false` or the job is a rerun after a reclaim (`job.no_spot`), true otherwise. The existing `/interrupted` handler then works unchanged: it finds the job by runner name, checks the token, marks it interrupted, and the rerun logic that exists for AWS takes over. In `place()`, the refusal of `spot=false` labels applies to `"aws"` only and stays so; for GCP the flag is passed through instead.
5. **Registering the launcher.** `superci runners create gcp --url=https://…` in `crates/cli/src/commands.rs`: adds `{cloud: "gcp", url}` to `launchers` and a `Pool` for `gcp` to the order through the existing settings-change path, after a `GET <url>/health` check that it answers `{"agent": "gcp"}`. `superci runners update gcp --max-jobs=N --monthly-usd=N` and `superci runners delete gcp` already work on any cloud name in the order once `CLOUDS` includes it. The dashboard adds, lists and removes a GCP launcher and shows a GCP job's machine and reclaim; that was added on 2026-11-02 at the account owner's request (see Task 19 of the plan).
6. **Time to come up.** The sweep's window for a machine whose runner has not come online (`plane.rs:5041`) gives `"gcp"` AWS's eight minutes instead of a launcher's three. A one-line match.
7. **A launcher may say wait.** In the `Runner::Launcher` branch of `start()`, a `429` answer with `{"detail": "<why>"}` puts the job in the `waiting` state with that reason and asks for a wake-up in twenty seconds, the way `WAIT_FOR_SPOT` does for AWS, instead of counting as a failed start. The existing day-long cap on waiting jobs applies. Launchers that never answer `429` are unaffected.
8. **Tests.** The core's own tests that enumerate clouds are updated. No new stand-in for Google's API: the launcher is tested on its own with the Compute client mocked, and the core change is covered by the existing launcher tests with a fake launcher that answers `usd_per_hour` and receives `notice` and `spot`.

### 3. Setup, in order

1. Fork `supercorp-ai/superci` into the team's GitHub organisation; branch `gcp-launcher`. Make the core changes; build the Cloudflare plane and the CLI.
2. GCP project: enable Compute; create the `superci` VPC with a subnet in us-west4 (Private Google Access on, no ingress rules); a service account for the launcher with `compute.instanceAdmin.v1` on the project; request spot CPU quota in us-west4; build the custom runner image. On path A also a subnet in us-east1 for the launcher (ingress on 443 only) and a DNS name for it. On path B also Cloud Run, Cloud Build, API Gateway (with its Service Management and Service Control APIs), Cloud Scheduler and Artifact Registry; a Cloud Router and Cloud NAT gateway in us-west4; an Artifact Registry remote repository mirroring Docker Hub; a service account for the gateway; and the ES256 check from section 1 before anything else.
3. Cloudflare: a free account; `superci dashboard` signs in and deploys the control plane; the dashboard creates the GitHub App and connects the organisation.
4. Path A: create the launcher VM (e2-micro, us-east1, Ubuntu 24.04, the launcher's service account, a static external address) and install the launcher with one script: Python and the service's dependencies, the systemd unit with the plane's URL, plane id and JWKS in its environment, and Caddy with the DNS name. Path B: deploy the launcher with `gcloud run deploy superci-launcher --source . --region us-west4 --no-allow-unauthenticated --min-instances 0`; grant the gateway's service account `roles/run.invoker` on it; create the API config from the OpenAPI file with the plane's URL and JWKS as the issuer and deploy the gateway; a Cloud Scheduler job for `/sweep`. Either way, check that `/health` answers `{"agent": "gcp"}` with a token the plane signed and refuses without one.
5. `superci runners create gcp --url=<Cloud Run URL>`, then `superci runners update gcp --max-jobs=100 --monthly-usd=<limit>`.
6. One workflow changed to `runs-on: superci`; `superci runs create --workflow=…` runs it without a push; then the rest, as the SuperCI skill document describes.

### 4. Success looks like

- A pushed workflow's job starts on a us-west4 spot VM, and the job page in `superci jobs retrieve` shows the machine type, `spot`, and an hourly price matching the launcher's table.
- Deleting the VM by hand mid-job makes the plane rerun the job on an on-demand VM once the run has finished, and the rerun's page says why.
- After a day of real traffic, `gcloud compute instances list` shows no VM older than its job, and the month's spend in `superci limits retrieve` tracks the comparison page's estimate within the overheads it names.

## Left to the builder

- Instance naming and label sanitising within GCE's rules.
- Exact machine size tables per family, and whether c4d's Hyperdisk requirement makes it the third choice or drops it from this version.
- Whether the launcher's `usd_per_hour` includes the disk and IP extras.
- The runner version pin and how the startup script fetches it (GitHub release URL, or a copy in a GCS bucket the VM can read without credentials).
- Shortcutting the build of the AWS and Modal planes from the published crate, or running `build.sh` whole.
- How the `/sweep` secret is held (an environment variable is enough for one account).
- The image build script's shape (a VM that installs and is saved as an image is enough; Packer is not needed), and the runner version pin inside it.

## Open questions for the user

- Which GitHub organisation holds the fork, and does the launcher's source live in the fork (beside `modal_launcher.py`) or in Tote's infra repository?
- Pending: the per-project exception to the external-address policy, requested 2026-11-02. Granted, path A; refused, path B, and the first week of real traffic then decides whether GCP under NAT still beats DigitalOcean, whose Droplet price includes transfer.
- Decided 2026-11-02: unauthenticated Cloud Run services are refused in the organisation on any path, so the launcher never relies on an `allUsers` binding.
