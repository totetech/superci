# GCP Spot Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run GitHub Actions jobs on GCP spot VMs in us-west4 through SuperCI, one machine per job, with a small launcher service in the GCP project and a handful of generic changes in a fork of SuperCI.

**Architecture:** SuperCI's control plane (on Cloudflare) treats GCP as a launcher: an HTTP agent with `/launch` and `/stop` that trusts the plane's ES256 token. The fork teaches the plane a fourth cloud name, GCP's capacity, a price and a spot flag in the launcher protocol, a "wait" answer, and an eight-minute boot window. The launcher is a Python service that picks a machine type, walks the three us-west4 zones for spot capacity, and starts a VM from a prebuilt image whose startup script runs GitHub's runner and reports preemption.

**Tech Stack:** Rust (the fork, edition 2021, no new dependencies), Python 3.12 with FastAPI and `google-cloud-compute` (the launcher), bash (startup script, image build), `gcloud`.

**Spec:** `docs/superpowers/specs/2026-11-01-gcp-spot-launcher-design.md`

## Global Constraints

- Core changes are generic, not GCP-shaped: "a launcher may say its price", "a launcher may be spot", "a launcher may say wait". No new crate dependencies in `crates/core` or `crates/cli`.
- The fourth cloud's id is `gcp` everywhere; its display name is `GCP`.
- Launcher protocol additions, exactly: request fields `spot` (bool) and `notice` (string, absent when `spot` is false); answer fields `usd_per_hour` (number) and `spot` (bool); status `429` with `{"detail": "<why>"}` means wait, `503` means retry, `400` means refuse.
- `Capacity::gcp()`: x64 only, Linux only, images `["ubuntu24"]`, `max_cpu: 64`, `max_ram_gb: 256`, `max_disk_gb: 500`, `std_cpu: 2`, `std_ram_gb: 8`, `std_disk_gb: 60`, `ram_per_cpu: 4`, `min_ram_per_cpu: 0`, no GPUs, `scratch: false`.
- Machine families in this order: `e2`, `n2d`, `c4d`; zones `us-west4-a`, `us-west4-b`, `us-west4-c`; sizes 2, 4, 8, 16 vCPU.
- The launch call is bounded to 8 seconds in all; each insert waits at most 5 seconds.
- Job VMs: no service account, labels `superci-plane`, `superci-job`, `superci-repo`, `superci-max-minutes`; spot VMs use `provisioningModel: SPOT`, `instanceTerminationAction: DELETE`, `automaticRestart: false`.
- Network path A (the plan) gives job VMs an ephemeral external address and runs the launcher on a VM; path B (only if the policy exception is refused) uses Cloud NAT and API Gateway. Tasks marked **[path B only]** are skipped unless the exception is refused.
- No tool attribution or branding in commits, branch names or files. Commit messages are plain and specific.
- Python tests run with `pytest` in a virtualenv under `launchers/gcp/.venv`; Rust tests with `cargo test`.

## Review Focus

- A runner name that is not a valid GCE instance name (uppercase, over 63 characters, a leading digit): the launcher must sanitise it to one GCE accepts and still find the VM again by the id it returns. Test in Task 9.
- A launch whose first insert succeeded but whose answer the plane never received (a timeout). The plane does not ask again with the same JIT configuration: each start attempt withdraws the runner at GitHub and registers a new configuration under the same runner name (a late machine is stopped and started again in the same way), and its call to `/launch` is a single send, with no retry. So the VM's name is keyed on the configuration, `instance_name(name)[:54] + "-" + sha256(jit)[:8]` (hex), and is the `id`: every attempt's VM has a name of its own, a launch with a new configuration under the same runner name makes a new VM and cannot fail with "already exists", and a VM made by a launch whose answer was lost is never found again or reused. It holds a withdrawn configuration, so its runner cannot register, it powers off, and the sweep deletes it (the `max-minutes` bound ends it otherwise). A request repeated exactly answers the existing VM's id, and a VM found by that name in `STOPPING` or `TERMINATED` status is answered `429`. Test in Task 10.
- A `/stop` for a VM already gone (preempted and deleted, or swept): must answer `{"stopped": true}`, not 404, or the plane's sweep retries forever. Test in Task 11.
- A token whose `aud` is the VM's address rather than the registered URL, or vice versa: must be refused, and the registered URL must be what the plane signs for. Test in Task 7.
- A preemption notice with a token that is not the job's, or for a job that has no notice (an on-demand machine): the plane must ignore it. Test in Task 3.

---

## Part 1: the fork of SuperCI

Tasks 1 to 6 change the Rust code. Task 0 prepares the checkout and build. Tasks 1 to 5 need only `cargo test -p superci-core`, which builds without the embedded planes; Task 6 and the final build need Task 0's artifacts.

### Task 0: Fork, clone and a building tree

**Files:**
- Create: the fork `<org>/superci` on GitHub from `supercorp-ai/superci`, branch `gcp-launcher` from tag `v0.19.21`
- Create: `crates/plane-aws/build/bootstrap.zip`, `crates/plane-modal/build/superci-plane`, `crates/runners-cloudflare/build/index.js`, `crates/runners-cloudflare/build/index_bg.wasm` (copied prebuilt, not committed)

**Interfaces:**
- Produces: a checkout at `~/code/superci-fork` (the tarball extract at `~/code/superci` has no `.git` and is kept only as a reference) where `cargo test -p superci-core` passes and `cargo build -p superci` succeeds.

- [ ] **Step 1: Fork and clone**

Run (outside the sandbox if `git clone` is refused in it):
```bash
gh repo fork supercorp-ai/superci --org <org> --clone=false
git clone git@github.com:<org>/superci.git ~/code/superci-fork
cd ~/code/superci-fork && git checkout -b gcp-launcher v0.19.21
```
Expected: `git log -1` shows the v0.19.21 commit.

- [ ] **Step 2: Copy the design and this plan into the fork**

```bash
mkdir -p docs/superpowers/specs docs/superpowers/plans
cp ~/code/superci/docs/superpowers/specs/2026-11-01-gcp-spot-launcher-design.md docs/superpowers/specs/
cp ~/code/superci/docs/superpowers/plans/2026-11-02-gcp-spot-launcher.md docs/superpowers/plans/
git add docs && git commit -m "Add the GCP spot launcher design and plan"
```

- [ ] **Step 3: Install the build toolchain**

Rust stable, `cargo install worker-build cargo-zigbuild`, zig (`brew install zig`), `zip`. `cargo-nextest` is optional.

- [ ] **Step 4: Obtain the prebuilt programs the CLI carries**

Three crates carry what `build.sh` builds (`superci-programs-planes`, `superci-programs-cache`, `superci-programs-layers`); each `build.rs` looks first at the paths `build.sh` writes and then at its own `embedded/` folder, which the published crates ship filled. Take those folders from the published 0.19.21 crates:
```bash
for c in planes cache layers; do
  curl -sL "https://crates.io/api/v1/crates/superci-programs-$c/0.19.21/download" -o "/tmp/pp-$c.crate"
  tar xzf "/tmp/pp-$c.crate" -C /tmp
  cp -R "/tmp/superci-programs-$c-0.19.21/embedded" "crates/programs-$c/embedded"
done
```
Expected: `crates/programs-planes/embedded/plane-aws.zip` and the other packed names from each `build.rs` table exist. The Cloudflare plane is then built from source in Step 5, and its `build/` path wins over the embedded copy. Do not commit the `embedded/` folders (add them to `.gitignore` in the fork).

- [ ] **Step 5: Build the Cloudflare plane from source and the CLI**

```bash
(cd crates/plane-cloudflare && worker-build --release)
cargo build -p superci
cargo test -p superci-core
```
Expected: `target/debug/superci` exists; core tests pass. Do not commit `build/` directories (they are ignored already; confirm with `git status`).

### Task 1: The fourth cloud's name and capacity

**Files:**
- Modify: `crates/core/src/spec.rs:83` (`CLOUDS`), `crates/core/src/spec.rs:316-320` (add `Capacity::gcp` after `modal`), `crates/core/src/spec.rs:606` (the error string test)
- Modify: `crates/core/src/plane.rs:717` (`CLOUD_NAMES`), `crates/core/src/plane.rs:1016` (`capacity_of`), `crates/core/src/plane.rs:3647` (`fail_fast`'s launcher list)
- Test: `crates/core/src/spec.rs` (unit tests in the file), `crates/core/tests/plane.rs`

**Interfaces:**
- Produces: `Capacity::gcp() -> Capacity` with the values in Global Constraints; `CLOUDS` containing `"gcp"`; `cloud_name("gcp") == "GCP"`; `capacity_of("gcp")` returning `Capacity::gcp()`.

- [ ] **Step 1: Write the failing spec tests**

In `spec.rs`'s `mod tests`, add to `every_key_says_what_it_takes_when_given_what_it_does_not` the new expected string `"provider is aws or cloudflare or modal or gcp, not “bsd”"` for the label `superci/provider=bsd` (and remove the `provider=gcp` case, which now parses), and a new test:
```rust
#[test]
fn gcp_sizes_the_machine_and_refuses_what_it_has_not() {
    let s = |l: &str| Spec::parse(l, "superci").unwrap().unwrap();
    assert_eq!(Capacity::gcp().fit(&s("superci")), Ok(Size { cpu: 2, ram_gb: 8, disk_gb: 60, gpu: None }));
    assert_eq!(Capacity::gcp().fit(&s("superci/cpu=4")), Ok(Size { cpu: 4, ram_gb: 16, disk_gb: 60, gpu: None }));
    assert!(Capacity::gcp().refuses(&s("superci/cpu=65")).unwrap().contains("64 CPU"));
    assert_eq!(Capacity::gcp().refuses(&s("superci/arch=arm64")), Some("x64 only".into()));
    assert_eq!(Capacity::gcp().refuses(&s("superci/gpu=any")), Some("no GPUs".into()));
    assert!(Capacity::gcp().refuses(&s("superci/os=windows")).unwrap().contains("Linux only"));
    assert_eq!(s("superci/provider=gcp").cloud.as_deref(), Some("gcp"));
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cargo test -p superci-core --lib spec::tests`
Expected: FAIL, `Capacity::gcp` not found and the error-string mismatch.

- [ ] **Step 3: Implement**

`CLOUDS: [&str; 4] = ["aws", "cloudflare", "modal", "gcp"]`; `pub fn gcp() -> Self` beside `modal()` with the constrained values; in `plane.rs`, `CLOUD_NAMES: [(&str, &str); 5]` adding `("gcp", "GCP")`; `capacity_of` becomes a `match` on `"cloudflare"`, `"gcp"`, else Modal; `fail_fast`'s list becomes `["cloudflare", "modal", "gcp"]`.

- [ ] **Step 4: Run the tests**

Run: `cargo test -p superci-core`
Expected: PASS. If a test in `tests/plane.rs` asserts the old error string, update it the same way.

- [ ] **Step 5: Commit**

```bash
git add crates/core && git commit -m "Add gcp as a fourth cloud name with its capacity"
```

### Task 2: A launcher may say its price and whether the machine is spot

**Files:**
- Modify: `crates/core/src/plane.rs:3443-3452` (the `Runner::Launcher` branch in `start()`)
- Test: `crates/core/tests/plane.rs`

**Interfaces:**
- Consumes: the launcher answer JSON `v` from `launcher_call`.
- Produces: `job.usd_per_hour` taken from `v["usd_per_hour"]` when it is a number; `job.on_demand` set to `v["spot"] == false` when `spot` is present.

- [ ] **Step 1: Write the failing test**

Mirror `launchers_and_failing_runners_start_where_they_can_and_say_what_they_could_not` (`tests/plane.rs:6748`): a `With(FakeClouds::default(), …)` answering `https://gcp.acme.example/launch` with `{"id":"superci-1","kind":"e2-standard-2","usd_per_hour":0.0104,"spot":true}`, `config.launchers = vec![Launcher { cloud: "gcp".into(), url: "https://gcp.acme.example".into() }]`, a queued job with label `superci`:
```rust
#[test]
fn a_launcher_that_says_its_price_is_believed_and_one_that_does_not_is_priced_by_formula() {
    // … setup as above …
    block_on(plane.handle(labelled("queued", 1, "acme/app", None, "superci")));
    let j = job(&store, 1);
    assert_eq!((j["usd_per_hour"].as_f64(), j["machine_id"].as_str(), j["machine_type"].as_str(), j["on_demand"].as_bool().unwrap_or(false)), (Some(0.0104), Some("superci-1"), Some("e2-standard-2"), false), "{j}");
    // The same launcher answering spot:false: an on-demand machine.
    // … second plane with the answer {"id":"superci-2","kind":"e2-standard-2","usd_per_hour":0.0755,"spot":false} …
    assert_eq!((job(&store, 2)["usd_per_hour"].as_f64(), job(&store, 2)["on_demand"]), (Some(0.0755), true));
}
```
Register the test name in the list near `tests/plane.rs:6970` as the others are.

- [ ] **Step 2: Run it to verify it fails**

Run: `cargo test -p superci-core --test plane a_launcher_that_says_its_price`
Expected: FAIL, price is Modal's formula.

- [ ] **Step 3: Implement**

After `let v = self.launcher_call(...)`, before `Ok(Tried::Started(...))`: `if let Some(p) = v["usd_per_hour"].as_f64() { job.usd_per_hour = Some(p) }` and `if let Some(s) = v["spot"].as_bool() { job.on_demand = !s }`. Leave the formula line before the call as it is (it is the fallback).

- [ ] **Step 4: Run the test**

Run: `cargo test -p superci-core --test plane a_launcher_that_says_its_price`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crates/core && git commit -m "Take a launcher's own price and spot flag from its answer"
```

### Task 3: Spot through a launcher

**Files:**
- Modify: `crates/core/src/plane.rs:3443-3452` (the `Runner::Launcher` branch, before `launcher_call`)
- Test: `crates/core/tests/plane.rs`

**Interfaces:**
- Consumes: `job.no_spot`, `spec.on_demand()`, `crate::crypto::random_token(18)`, the `/interrupted` route (unchanged).
- Produces: for a launcher whose cloud is `gcp`, the `/launch` body carries `"spot": bool` and, when spot, `"notice": "<plane_url>/interrupted?runner=<name>&t=<token>"`, and `job.notice` holds the token. Other clouds' bodies are unchanged.

- [ ] **Step 1: Write the failing test**

```rust
#[test]
fn a_gcp_launcher_is_told_spot_and_a_notice_link_and_its_machine_posts_a_notice_when_it_is_taken_back() {
    // Setup as in Task 2, capturing the /launch body (clouds.calls records (method, url, body)).
    block_on(plane.handle(labelled("queued", 1, "acme/app", None, "superci")));
    let body: serde_json::Value = serde_json::from_str(&launch_body_of("https://gcp.acme.example/launch")).unwrap();
    let token = job(&store, 1)["notice"].as_str().expect("a notice token").to_string();
    assert!(token.len() >= 16 && body["spot"] == true && body["notice"] == format!("{PLANE_URL}/interrupted?runner={}&t={token}", body["name"].as_str().unwrap()), "{body}");
    // The machine says it was taken back: the job is marked interrupted once; a wrong token is ignored.
    block_on(plane.handle(Request::new("POST", &format!("{PLANE_URL}/interrupted?runner={}&t=wrong", body["name"].as_str().unwrap()))));
    assert_eq!(job(&store, 1)["interrupted"], serde_json::Value::Null);
    block_on(plane.handle(Request::new("POST", &format!("{PLANE_URL}/interrupted?runner={}&t={token}", body["name"].as_str().unwrap()))));
    assert_eq!(job(&store, 1)["interrupted"], true);
    // A label with spot=false, and a rerun after a reclaim (no_spot): spot:false and no notice.
    block_on(plane.handle(labelled("queued", 2, "acme/app", None, "superci/spot=false")));
    let body2: serde_json::Value = serde_json::from_str(&launch_body_of_job(2)).unwrap();
    assert!(body2["spot"] == false && body2.get("notice").is_none() && job(&store, 2)["notice"].is_null(), "{body2}");
    // A notice for a job that has none is ignored, whatever its token.
    block_on(plane.handle(Request::new("POST", &format!("{PLANE_URL}/interrupted?runner={}&t=anything", body2["name"].as_str().unwrap()))));
    assert_eq!(job(&store, 2)["interrupted"], serde_json::Value::Null);
    // A Modal launcher's body says nothing of spot.
    // … a plane with a modal launcher, a queued job 3; assert body3.get("spot").is_none() …
}
```
`PLANE_URL` is the test file's constant (`tests/plane.rs:254`). Recorded calls are `(method, url, body)` triples in `clouds.0.calls.borrow()` when the fake is wrapped in `With`; add a small `launch_body_of(url) -> String` helper in the test file that returns the last body sent to that URL, and `launch_body_of_job(n)` the last `/launch` body whose `job` field is `n`. For the rerun case, set `store` key `rerun:acme/app:<run>:<name>` as `launch()` reads it (`rerun_key`), or set `job.no_spot` through a second delivery; the existing reclaim tests show the shape (search `after_reclaim` in `tests/plane.rs`).

- [ ] **Step 2: Run it to verify it fails**

Run: `cargo test -p superci-core --test plane a_gcp_launcher_is_told_spot`
Expected: FAIL, no `spot` in the body.

- [ ] **Step 3: Implement**

In the `Runner::Launcher` branch, before building `body`: `let spot_capable = a.cloud == "gcp";` and `let spot = spot_capable && !spec.on_demand() && !job.no_spot;`. If `spot_capable`: `body["spot"] = spot.into()`; `job.notice = spot.then(|| crate::crypto::random_token(18))`; if `let Some(t) = &job.notice`, `body["notice"] = format!("{plane_url}/interrupted?runner={name}&t={t}").into()`. (`job.notice` is already reset to `None` at `plane.rs:3237` for each place tried.)

- [ ] **Step 4: Run the tests**

Run: `cargo test -p superci-core --test plane`
Expected: PASS, including the existing `/interrupted` tests.

- [ ] **Step 5: Commit**

```bash
git add crates/core && git commit -m "Tell a spot-capable launcher whether to use spot and where to report a reclaim"
```

### Task 4: A launcher may say wait

**Files:**
- Modify: `crates/core/src/plane.rs:3605-3620` (the error tail of `start()`, before the retries block)
- Test: `crates/core/tests/plane.rs`

**Interfaces:**
- Consumes: `launcher_call`'s error string, which for status 429 reads `"<cloud> launcher: 429 <detail>"`.
- Produces: a job whose launcher answered 429 is in state `waiting` with `error` = `"waiting: <detail>"`, a wake-up asked in 20 seconds, `retries` unchanged; the next sweep (`plane.alarm()`) tries it again.

- [ ] **Step 1: Write the failing test**

```rust
#[test]
fn a_launcher_saying_no_room_makes_the_job_wait_and_the_sweep_tries_again() {
    let full = std::cell::Cell::new(true);
    let clouds = With(FakeClouds::default(), |r: &Request, _: &str| (r.url == "https://gcp.acme.example/launch").then(|| if full.get() {
        Response::new(429, "application/json", r#"{"detail":"no spot room in us-west4 (ZONE_RESOURCE_POOL_EXHAUSTED in every zone)"}"#)
    } else { Response::new(200, "application/json", r#"{"id":"superci-1","kind":"e2-standard-2","usd_per_hour":0.0104,"spot":true}"#) }));
    // … plane with a gcp launcher …
    block_on(plane.handle(labelled("queued", 1, "acme/app", None, "superci")));
    let j = job(&store, 1);
    assert_eq!((j["state"].as_str(), j["retries"].as_u64().unwrap_or(0)), (Some("waiting"), 0), "{j}");
    assert!(j["error"].as_str().unwrap().starts_with("waiting: no spot room in us-west4"), "{j}");
    assert_eq!(wakes.0.borrow().last().copied(), Some(20_000));
    // Still full at the next sweep: still waiting, still no retry counted.
    block_on(plane.alarm()).unwrap();
    assert_eq!((job(&store, 1)["state"].as_str(), job(&store, 1)["retries"].as_u64().unwrap_or(0)), (Some("waiting"), 0));
    full.set(false);
    block_on(plane.alarm()).unwrap();
    assert_eq!(job(&store, 1)["state"], "launched");
}
```
`Wakes` is the test file's `Timer` fake (`tests/plane.rs:247`); its first field records every `wake_in` in milliseconds.

- [ ] **Step 2: Run it to verify it fails**

Run: `cargo test -p superci-core --test plane a_launcher_saying_no_room`
Expected: FAIL, state is `waiting` with "trying again" and `retries` is 1 (the retry path).

- [ ] **Step 3: Implement**

In the error tail, before the `if !job.cloud.is_empty() && job.retries < 2` block: `if let Some((_, said)) = e.split_once(" launcher: 429 ") { job.state = "waiting".into(); job.error = Some(format!("waiting: {}", said.chars().take(280).collect::<String>())); self.keep(&key, &*job).await?; return self.timer.wake_in(20_000).await; }`. The sweep already re-enters `start()` for `waiting` jobs and fails them after a day.

- [ ] **Step 4: Run the tests**

Run: `cargo test -p superci-core --test plane`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crates/core && git commit -m "Let a launcher answer 429 to make a job wait for room"
```

### Task 5: Eight minutes for a GCP machine to come online

**Files:**
- Modify: `crates/core/src/plane.rs:5041`
- Test: `crates/core/tests/plane.rs`

**Interfaces:**
- Produces: `(wait, up_by)` is `(8 * 60_000, 90_000)` when `j.cloud` is `"aws"` or `"gcp"`.

- [ ] **Step 1: Write the failing test**

Find the existing test for a launcher machine that never began its job (search `did not start a machine for it` in `tests/plane.rs`) and add a sibling with cloud `gcp`: a launched job whose runner is not online, swept at 4 minutes, is still `launched`; swept at 9 minutes, it is stopped and placed again (`retries == 1`). Use that test's way of advancing the clock.

- [ ] **Step 2: Run it to verify it fails**

Run: `cargo test -p superci-core --test plane <its name>`
Expected: FAIL at the 4-minute assertion.

- [ ] **Step 3: Implement**

`let (wait, up_by) = if j.cloud == "aws" || j.cloud == "gcp" { (8 * 60_000, 90_000) } else { (3 * 60_000, 60_000) };`

- [ ] **Step 4: Run the tests, then commit**

Run: `cargo test -p superci-core`
```bash
git add crates/core && git commit -m "Give a GCP machine eight minutes to come online, as an AWS one has"
```

### Task 6: `superci runners create gcp --url=…`

**Files:**
- Modify: `crates/cli/src/commands.rs:35` (the `runners` help text), `crates/cli/src/commands.rs:1216-1233` (the `("runners", "create")` arm)
- Modify: `crates/cli/src/dashboard.rs:3453` (add a `("POST", "/runners/gcp")` arm beside `/runners/modal`), `crates/cli/src/dashboard.rs:9797` (the list of routes that accept a form POST, if `/runners/modal` is listed there)
- Test: `crates/cli/tests/commands.rs:374` and `:400`

**Interfaces:**
- Consumes: `launchers_in`, `settings_doc`, `put_as_said`, `status_soon`, `wait_for` from `dashboard.rs` (as `/runners/modal` uses them); `field("url", …)` from `commands.rs`.
- Produces: `POST /runners/gcp` with form field `url`: checks `GET <url>/health` answers 200 with `{"agent":"gcp"}` (else a 400 message saying what it answered), adds `Launcher { cloud: "gcp", url }` (replacing one already there) and writes it through `put_as_said`, waits for the status to list it, redirects to `/?p=runners`. The command prints `gcp runs jobs now.`

- [ ] **Step 1: Write the failing tests**

In `crates/cli/tests/commands.rs`, change the row at `:374` to `(&["runners", "create", "gcp"], "Say its address: `superci runners create gcp --url=https://…`")` and add beside `:400` the row `(&["runners", "create", "gcp", "--url=https://launcher.example", "--dry-run"], "let gcp run jobs for the control plane in use")`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cargo test -p superci --test commands`
Expected: FAIL on both rows.

- [ ] **Step 3: Implement**

In the match: `("gcp", _) => match args.get("url") { Some(u) if u.starts_with("https://") => ("/runners/gcp", vec![field("url", u)]), _ => return usage("Say its address: `superci runners create gcp --url=https://…`.") }`, placed before the `_ =>` Cloudflare arm. Update the usage string at `:1218` to list `gcp`, and the help at `:35`. In the dashboard, the `/runners/gcp` arm: read `url` from the form, one `ureq::get(&crate::aws::stand_in(&format!("{url}/health")))` expecting status 200 and a body whose `agent` is `gcp`, then the same launchers write as `/runners/modal` does with `cloud: "gcp"`.

- [ ] **Step 4: Run the tests, then build**

Run: `cargo test -p superci --test commands` then `cargo build --release -p superci`
Expected: PASS; `target/release/superci` is the binary the setup uses.

- [ ] **Step 5: Commit**

```bash
git add crates/cli && git commit -m "Register a GCP launcher by its address with runners create gcp"
```

---

## Part 2: the launcher

All files under `launchers/gcp/` in the fork. Python 3.12. Tasks 7 to 11 are unit-tested with a fake Compute client; Task 12 builds the image; Task 13 deploys on path A; Task 14 is path B only.

### Task 7: Scaffold, token verification and `/health`

**Files:**
- Create: `launchers/gcp/launcher.py`, `launchers/gcp/app.py`, `launchers/gcp/requirements.txt`, `launchers/gcp/requirements-dev.txt`, `launchers/gcp/README.md`
- Test: `launchers/gcp/tests/test_verify.py`, `launchers/gcp/tests/conftest.py`

**Interfaces:**
- Produces, in `launcher.py`:
  - `@dataclass class Config: project: str; region: str; zones: tuple[str, ...]; network: str; subnet: str; image_family: str; runner_version: str; on_demand_fallback: bool; public_url: str; trust: dict; plane_id: str; sweep_secret: str; behind_gateway: bool = False; external_address: bool = True; budget_s: float = 8.0; insert_wait_s: float = 5.0` and `Config.from_env() -> Config` reading `PROJECT`, `REGION`, `NETWORK`, `SUBNET`, `IMAGE_FAMILY`, `RUNNER_VERSION`, `ON_DEMAND_FALLBACK` (`"1"` means on), `PUBLIC_URL`, `TRUSTED_ISSUER`, `TRUSTED_SUBJECT`, `TRUSTED_KEYS` (JWKS JSON), `SWEEP_SECRET`, `BEHIND_GATEWAY` (`"1"`, path B), `EXTERNAL_ADDRESS` (`"0"` on path B), zones defaulting to `us-west4-a,b,c`; `plane_id` is `TRUSTED_SUBJECT` after `plane:`; `trust` is `{"issuer": TRUSTED_ISSUER, "subject": TRUSTED_SUBJECT, "keys": <TRUSTED_KEYS parsed>}`.
  - `verify(authorization: str, audience: str, trust: dict, now: float) -> dict`: returns the claims or raises `PermissionError` with one of `"no token"`, `"bad token"`, `"expired"`, `"token for another agent or control plane"`. ES256 over the JWKS in `trust["keys"]`, `iss == trust["issuer"]`, `sub == trust["subject"]`, `aud == audience`. Copy the algorithm from `crates/cli/src/modal_launcher.py`.
  - In `app.py`: FastAPI `api` with `GET /health -> {"agent": "gcp"}` (no token), and a dependency `checked(request) -> dict` that calls `verify` with `audience = cfg.public_url` and turns `PermissionError` into HTTP 401 `{"detail": "not allowed: <why>"}`.
- `requirements.txt`: `fastapi[standard]==0.118.0`, `cryptography==46.0.1`, `google-cloud-compute==1.*` (pin the exact current release); `requirements-dev.txt`: `pytest`, `httpx`.

- [ ] **Step 1: Write the failing tests**

`conftest.py` makes an ES256 key with `cryptography`, a JWKS from it, a `trust` dict and a `mint(claims) -> str` helper. `test_verify.py`:
```python
def test_a_good_token_yields_its_claims(trust, mint, now):
    claims = verify("Bearer " + mint({"iss": trust["issuer"], "sub": trust["subject"], "aud": "https://launcher.example", "exp": now + 60}), "https://launcher.example", trust, now)
    assert claims["sub"] == trust["subject"]

@pytest.mark.parametrize("claims,why", [
    ({"aud": "https://other.example"}, "token for another agent or control plane"),
    ({"sub": "plane:other"}, "token for another agent or control plane"),
    ({"exp": -60}, "expired"),
])
def test_a_token_for_another_agent_or_expired_is_refused(trust, mint, now, claims, why): ...

def test_no_token_and_a_token_signed_by_another_key_are_refused(trust, mint, now): ...  # "no token", "bad token"

def test_health_needs_no_token(client): assert client.get("/health").json() == {"agent": "gcp"}
def test_launch_without_a_token_is_401(client): assert client.post("/launch", json={}).status_code == 401
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd launchers/gcp && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt && .venv/bin/pytest -q`
Expected: FAIL, `launcher` not importable.

- [ ] **Step 3: Implement `Config`, `verify`, the app and `/health`**

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add launchers/gcp && git commit -m "Add the GCP launcher's scaffold with token verification"
```

### Task 8: Machine choice and the price table

**Files:**
- Create: `launchers/gcp/prices.py`
- Modify: `launchers/gcp/launcher.py`
- Test: `launchers/gcp/tests/test_choose.py`

**Interfaces:**
- Produces: `FAMILIES = ("e2", "n2d", "c4d")`, `SIZES = {"e2": (2, 4, 8, 16, 32), "n2d": (2, 4, 8, 16, 32, 64), "c4d": (2, 4, 8, 16, 32, 64)}` (e2 stops at 32 vCPU; `Capacity::gcp` allows 64); `choose(cpu: int, ram_gb: int, families: tuple[str, ...] = FAMILIES) -> list[str]`: for each family in order, the smallest `<family>-standard-<n>` in its sizes with `n >= cpu` and `n * 4 >= ram_gb`, skipping a family with no such size; empty when nothing fits. The same 4 GB-a-vCPU rule applies to c4d, whose standard sizes carry 3.75 GB a vCPU; a job gets up to 6 percent less memory there than it asked for, accepted. `prices.SPOT: dict[str, float]`, `prices.ON_DEMAND: dict[str, float]` keyed by machine type, for us-west4, covering every size in `SIZES`, and `prices.TAKEN = "2026-11-01"`. `price(machine_type: str, spot: bool) -> float` raising `KeyError` for an unknown type.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_smallest_size_that_fits_in_each_family_in_order():
    assert choose(2, 8) == ["e2-standard-2", "n2d-standard-2", "c4d-standard-2"]
    assert choose(4, 16) == ["e2-standard-4", "n2d-standard-4", "c4d-standard-4"]
    assert choose(3, 8) == ["e2-standard-4", "n2d-standard-4", "c4d-standard-4"]
    assert choose(2, 12) == ["e2-standard-4", "n2d-standard-4", "c4d-standard-4"]
    assert choose(64, 256) == ["n2d-standard-64", "c4d-standard-64"]
    assert choose(65, 8) == []

def test_every_size_of_every_family_has_both_prices():
    for family, sizes in SIZES.items():
        for n in sizes:
            t = f"{family}-standard-{n}"
            assert t in SPOT and t in ON_DEMAND and 0 < SPOT[t] < ON_DEMAND[t], t

def test_known_prices():
    assert (SPOT["e2-standard-2"], ON_DEMAND["e2-standard-2"]) == (0.0104, 0.0755)
    assert (SPOT["c4d-standard-2"], ON_DEMAND["c4d-standard-2"]) == (0.0165, 0.1033)
    assert price("e2-standard-2", spot=False) == 0.0755
    with pytest.raises(KeyError): price("n1-standard-1", spot=True)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest -q tests/test_choose.py`
Expected: FAIL.

- [ ] **Step 3: Fill the price table and implement `choose` and `price`**

Prices come from the dataset the comparison used: download `https://gcloud-compute.com/machine-types-regions.csv` (about 7 MB; columns `name`, `region`, `hour`, `hourSpot`) and take, for region `us-west4`, every size in `SIZES` of `e2-standard`, `n2d-standard` and `c4d-standard`. Write the file's `TAKEN` as the dataset's date. Values known from the comparison: e2-standard-2 0.0104 / 0.0755, e2-standard-4 0.0209 / 0.1509, n2d-standard-2 0.0268 / 0.0952, n2d-standard-4 0.0535 / 0.1903, c4d-standard-2 0.0165 / 0.1033, c4d-standard-4 0.0337 / 0.2107.

- [ ] **Step 4: Run the tests, then commit**

Run: `.venv/bin/pytest -q`
```bash
git add launchers/gcp && git commit -m "Choose a GCE machine type by size and price it from a us-west4 table"
```

### Task 9: The startup script and the instance name

**Files:**
- Create: `launchers/gcp/startup.sh.tmpl`
- Modify: `launchers/gcp/launcher.py`
- Test: `launchers/gcp/tests/test_startup.py`

**Interfaces:**
- Produces: `instance_name(runner: str) -> str`: lowercase, every character outside `[a-z0-9-]` replaced by `-`, a leading non-letter prefixed with `r-`, cut to 63 characters, no trailing `-`; the same input always gives the same output. `render_startup(jit: str, fail: str | None, max_minutes: int, notice: str | None, runner_version: str) -> str` from the template with placeholders `__JIT__`, `__FAIL_HOOK__`, `__MAX_MINUTES__`, `__NOTICE_WATCHER__`, `__RUNNER_VERSION__`; raises `ValueError` when `jit` or `fail` is not base64 (`[A-Za-z0-9+/=]`).
- The template, in order: `#!/bin/bash`, log to the serial console (`exec > >(tee /var/log/superci.log > /dev/console) 2>&1`), `shutdown -h +__MAX_MINUTES__`, the notice watcher when spot (a background loop: `curl -s -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/preempted?wait_for_change=true'` until it prints `TRUE`, then `curl -s -f -m 5 --retry 4 --retry-all-errors -X POST '__NOTICE__' 2>/dev/null | bash &>/dev/null`: each try bounded to 5 seconds, four more tries on any error, and the plane's answer run with bash, as `SPOT_WATCH` in `crates/core/src/aws.rs` posts AWS's notice), the fail hook when given (copied from `runner_command` in `modal_launcher.py`), `cd /home/runner && sudo -u runner -H ./run.sh --jitconfig __JIT__` (with `ACTIONS_RUNNER_HOOK_JOB_STARTED` set when failing), then `poweroff`. The runner is preinstalled by the image (Task 12) at `/home/runner`; `__RUNNER_VERSION__` is only asserted against the installed version and logged.
- The shutdown script: `render_shutdown(notice: str | None) -> str | None`, `None` without a notice. A spot VM with a notice gets it as its `shutdown-script` metadata item, GCE's documented mechanism for preemption, which GCE runs during the 30 seconds a preempted VM is given: it reads `instance/preempted` and, only when it is `TRUE`, posts the notice with the same bounded, retried `curl` and runs the answer with bash. The watcher in the startup script is kept beside it.

- [ ] **Step 1: Write the failing tests**

```python
def test_instance_names_are_valid_for_gce_and_stable():
    assert instance_name("SuperCI-abc123-456789") == "superci-abc123-456789"
    assert instance_name("1job") == "r-1job"
    assert len(instance_name("a" * 80)) == 63
    assert not instance_name("x" * 62 + "--").endswith("-")
    assert instance_name("Acme_CI-1") == instance_name("Acme_CI-1") == "acme-ci-1"

def test_a_spot_script_watches_for_preemption_and_a_plain_one_does_not():
    s = render_startup("QUJD", None, 70, "https://plane.example/interrupted?runner=x&t=tok", "2.330.0")
    assert "shutdown -h +70" in s and "instance/preempted" in s and "https://plane.example/interrupted?runner=x&t=tok" in s and "--jitconfig QUJD" in s and s.rstrip().endswith("poweroff")
    assert "instance/preempted" not in render_startup("QUJD", None, 70, None, "2.330.0")

def test_a_failing_runner_gets_its_hook_and_bad_base64_is_refused():
    s = render_startup("QUJD", "d2h5", 10, None, "2.330.0")
    assert "ACTIONS_RUNNER_HOOK_JOB_STARTED" in s and "d2h5" in s
    with pytest.raises(ValueError): render_startup("abc; rm -rf /", None, 10, None, "2.330.0")
```

- [ ] **Step 2: Run them to verify they fail**, **Step 3: Implement**, **Step 4: Run them**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add launchers/gcp && git commit -m "Render a job VM's startup script and a GCE-safe instance name"
```

### Task 10: `/launch`: the zone and family walk within a budget

**Files:**
- Modify: `launchers/gcp/launcher.py`, `launchers/gcp/app.py`
- Test: `launchers/gcp/tests/test_launch.py`, `launchers/gcp/tests/fake_compute.py`

**Interfaces:**
- Produces: `class Compute(Protocol)` with `insert(zone: str, body: dict) -> str` (operation name), `wait(zone: str, operation: str, timeout_s: float) -> dict | None` (`None` while still running; `{}` when done; `{"error": "<CODE>: <message>"}` when failed), `get(zone: str, name: str) -> dict | None`, `delete(zone: str, name: str) -> None` (no error when absent), `list(zone: str, label_filter: str) -> list[dict]`; `class GoogleCompute(Compute)` over `google.cloud.compute_v1` (not unit-tested); `class NoRoom(Exception)` carrying the per-pair reasons; `launch(req: dict, compute: Compute, cfg: Config, now: Callable[[], float]) -> dict` returning `{"id", "kind", "usd_per_hour", "spot"}`.
- Behaviour: `name = vm_name(req["name"], req["jit"])`, which is `instance_name(req["name"])[:54] + "-" + sha256(jit)[:8]` (hex); if `compute.get(zone, name)` finds it in any zone, answer that VM without an insert (a request repeated exactly; the plane sends none, see Review Focus), except one in `STOPPING` or `TERMINATED` status (`ENDING_STATES`), which raises `NoRoom`; `spot = req.get("spot", True)`; for each type in `choose(req["cpu"], req["ram_gb"])` and each zone, in that nesting order, `insert` with the VM body from Global Constraints (`_vm_body`; `scheduling` is `onHostMaintenance: TERMINATE`, `provisioningModel: SPOT`, `instanceTerminationAction: DELETE` and `automaticRestart: false` for a spot VM, and `onHostMaintenance: MIGRATE` with `automaticRestart: false` for an on-demand one; `disks[0]` of `max(req.get("disk_gb", 60), MIN_DISK_GB)` GB (`MIN_DISK_GB = 10`, the size of the runner image: GCE refuses a boot disk smaller than its image), `pd-balanced`, or `hyperdisk-balanced` for `c4d`; `networkInterfaces[0]` on `cfg.subnet` with `nicType: GVNIC` and `accessConfigs: [{"type": "ONE_TO_ONE_NAT"}]` on path A and none on path B; `metadata.items` with `startup-script` from `render_startup` and, for a spot VM with a notice, a `shutdown-script` item from `render_shutdown`, which posts the notice only when `instance/preempted` reads `TRUE`; `serviceAccounts: []`; the four labels), then `wait` with `min(cfg.insert_wait_s, budget_left)`: `None` means answer started; `{}` means started; an error containing `ZONE_RESOURCE_POOL_EXHAUSTED`, `QUOTA_EXCEEDED` or `RESOURCE_POOL_EXHAUSTED` moves on, and so does an insert that GCE refuses at the request itself for want of quota or capacity (`Compute.insert` raises `Refused`, which `_insert` takes as the same error, so that pair counts as no room); any other error, from the operation or from the insert request, raises `RuntimeError(message)` (the app answers 503). When the budget is spent before an insert is issued, raise `NoRoom`. When every pair moved on: with `cfg.on_demand_fallback`, one more insert of the first type in the first zone as an on-demand VM (no SPOT fields, no notice watcher, no `shutdown-script`) and answer `spot: False` at the on-demand price; else raise `NoRoom`. The app maps `NoRoom` to 429 `{"detail": "no spot room in us-west4 (<reasons>)"}` (`no room in …` when the request was not for a spot VM), `ValueError` to 400, `RuntimeError` to 503.

- [ ] **Step 1: Write the failing tests**

`fake_compute.py`: an in-memory `FakeCompute` whose `plan` maps `(zone, machine_type)` to `"ok"`, `"exhausted"`, `"slow"` (wait returns `None`), or `"error:<message>"`, records inserts, and whose `get`/`delete`/`list` work on what was inserted. Tests:
```python
def test_the_first_pair_with_room_wins_and_the_answer_prices_it(cfg):
    fc = FakeCompute({("us-west4-a", "e2-standard-2"): "exhausted", ("us-west4-b", "e2-standard-2"): "ok"})
    a = launch(REQ, fc, cfg, clock())
    assert a == {"id": "superci-1-d9cae0db", "kind": "e2-standard-2", "usd_per_hour": 0.0104, "spot": True}  # (d9cae0db: sha256("QUJD"))
    assert fc.inserted[-1]["zone"] == "us-west4-b" and fc.inserted[-1]["body"]["scheduling"]["provisioningModel"] == "SPOT"

def test_every_pair_exhausted_is_no_room_and_the_fallback_starts_on_demand(cfg): ...
    # NoRoom with all 9 pairs exhausted; with cfg.on_demand_fallback the answer has spot False and price 0.0755

def test_an_insert_still_running_at_the_budget_is_answered_as_started(cfg): ...  # plan "slow": answer has the id; one insert only

def test_a_second_launch_with_the_same_jit_answers_the_existing_vm(cfg): ...  # Review Focus
def test_a_launch_for_the_same_name_with_a_new_jit_makes_a_new_vm(cfg): ...  # Review Focus
def test_a_vm_of_that_name_that_is_stopping_or_terminated_is_no_room_and_nothing_is_inserted(cfg, status): ...  # Review Focus: 429

def test_an_unexpected_error_is_raised_and_a_bad_request_is_a_value_error(cfg): ...  # "error:PERMISSION_DENIED …" -> RuntimeError; missing jit -> ValueError

def test_spot_false_makes_an_on_demand_vm_with_no_notice(cfg): ...  # no provisioningModel SPOT, startup script without the watcher

def test_the_http_layer_maps_errors(client_with_fake): ...  # 429 detail, 503, 400, and 200 with the body
```

- [ ] **Step 2: Run them to verify they fail**, **Step 3: Implement**, **Step 4: Run them**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add launchers/gcp && git commit -m "Launch a job VM by walking zones and families within a time budget"
```

### Task 11: `/stop` and `/sweep`

**Files:**
- Modify: `launchers/gcp/launcher.py`, `launchers/gcp/app.py`
- Test: `launchers/gcp/tests/test_stop_sweep.py`

**Interfaces:**
- Produces: `stop(name: str, compute: Compute, cfg: Config) -> None` deleting the VM in whichever zone has it, no error when none does; `sweep(compute: Compute, cfg: Config, now: float) -> int` deleting, in every zone, VMs labelled `superci-plane=<cfg.plane_id>` whose `creationTimestamp` is older than their `superci-max-minutes` label plus 10 minutes, or whose `status` is `TERMINATED`; returns how many. `POST /stop` answers `{"stopped": true}`; `POST /sweep` needs header `x-superci-sweep` equal to `SWEEP_SECRET` (404 otherwise) and answers `{"swept": n}`. The app also runs `sweep` inline before answering any `/launch` or `/stop` when more than 600 seconds passed since the last sweep.

- [ ] **Step 1: Write the failing tests**

```python
def test_stop_deletes_wherever_the_vm_is_and_is_quiet_when_it_is_gone(cfg): ...
def test_sweep_deletes_only_what_is_old_or_terminated(cfg): ...   # three VMs: young running (kept), old running (deleted), young TERMINATED (deleted)
def test_sweep_route_needs_its_secret(client_with_fake): ...       # 404 without, {"swept": 0} with
```

- [ ] **Step 2: Run them to verify they fail**, **Step 3: Implement**, **Step 4: Run them**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add launchers/gcp && git commit -m "Stop a job VM on request and sweep leftovers"
```

### Task 12: The runner image

**Files:**
- Create: `launchers/gcp/image/install.sh`, `launchers/gcp/image/build-image.sh`

**Interfaces:**
- Produces: a GCE image in family `superci-runner` in the project, built from `ubuntu-2404-lts-amd64`, with Docker (`docker.io` from Ubuntu, `max-concurrent-downloads` 16 as `runner_user_data` in `crates/core/src/aws.rs` sets it), `git jq zip unzip xz-utils zstd build-essential pkg-config curl`, a `runner` user in the `docker` group, GitHub's runner at `/home/runner` at the version in `RUNNER_VERSION` (default `2.330.0`; take the current release from `https://github.com/actions/runner/releases`), and `RUNNER_ALLOW_RUNASROOT` unset.
- `build-image.sh PROJECT ZONE [RUNNER_VERSION]`: creates a temporary VM `superci-image-build-<timestamp>` with `install.sh` as its startup script, polls the serial console for the line `superci: image ready`, stops the VM, runs `gcloud compute images create superci-runner-<date> --source-disk … --family superci-runner`, deletes the VM. Fails with the serial log when `image ready` has not appeared in 15 minutes.

- [ ] **Step 1: Write `install.sh`** ending in `echo 'superci: image ready'`.

- [ ] **Step 2: Write `build-image.sh`** as specified.

- [ ] **Step 3: Build the image and verify it**

Run: `launchers/gcp/image/build-image.sh <project> us-west4-a`
Then start a throwaway VM from the family and check: `docker run --rm hello-world` succeeds as the `runner` user; `/home/runner/run.sh --version` prints the pinned version; time from `gcloud compute instances create` to the VM accepting an SSH command is under 60 seconds. Delete it.
Expected: all three hold.

- [ ] **Step 4: Commit**

```bash
git add launchers/gcp/image && git commit -m "Build the superci-runner image for GCE job VMs"
```

### Task 13: Deploy the launcher on path A (a VM behind Caddy)

**Files:**
- Create: `launchers/gcp/deploy/install.sh`, `launchers/gcp/deploy/superci-launcher.service`, `launchers/gcp/deploy/Caddyfile`, `launchers/gcp/deploy/env.example`

**Interfaces:**
- Consumes: Task 15's project setup (VPC, subnet, service account, firewall rule), a DNS name pointing at the launcher VM's static address.
- Produces: `https://<name>/health` answering `{"agent": "gcp"}`; the launcher running as `superci-launcher.service` with `EnvironmentFile=/etc/superci-launcher.env`; Caddy with `<name> { reverse_proxy 127.0.0.1:8080 }`.

- [ ] **Step 1: Write the four files.** `install.sh` installs Python 3.12 and Caddy, copies `launchers/gcp` to `/opt/superci-launcher`, makes the venv, installs the unit and the Caddyfile, enables both. `env.example` lists every variable from `Config.from_env` plus `SWEEP_SECRET`, with the plane's JWKS to be pasted from `curl <plane>/.well-known/jwks.json`.

- [ ] **Step 2: Create the VM and install**

```bash
gcloud compute instances create superci-launcher --project <project> --zone us-east1-b --machine-type e2-micro --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud --subnet superci-launcher --address <static address> --service-account superci-launcher@<project>.iam.gserviceaccount.com --scopes cloud-platform --tags superci-launcher
gcloud compute scp --recurse launchers/gcp superci-launcher:~/ --zone us-east1-b
gcloud compute ssh superci-launcher --zone us-east1-b -- sudo bash ~/gcp/deploy/install.sh
```

- [ ] **Step 3: Verify**

Run: `curl -s https://<name>/health` → `{"agent":"gcp"}`; `curl -s -o /dev/null -w '%{http_code}' -X POST https://<name>/launch` → `401`.

- [ ] **Step 4: Commit**

```bash
git add launchers/gcp/deploy && git commit -m "Install the GCP launcher on a VM behind Caddy"
```

### Task 14: **[path B only]** Deploy the launcher on Cloud Run behind API Gateway

**Files:**
- Create: `launchers/gcp/deploy/Procfile` (`web: uvicorn app:api --host 0.0.0.0 --port $PORT`), `launchers/gcp/deploy/openapi.yaml`

**Interfaces:**
- Produces: a gateway whose `securityDefinitions.plane` has `x-google-issuer: <plane url>`, `x-google-jwks_uri: <plane url>/.well-known/jwks.json`, `x-google-audiences: <gateway url>`, and whose paths `/launch`, `/stop`, `/sweep`, `/health` route to the Cloud Run service with `x-google-backend.address` and `jwt_audience`. The launcher reads claims from `X-Apigateway-Api-Userinfo` (base64url JSON) when present and checks `iss`, `sub`, `aud`, `exp` from it; `verify` is bypassed only when that header is present and `cfg.behind_gateway` is on.

- [ ] **Step 1: Probe ES256 first.** Deploy a throwaway gateway with the config above pointing at a trivial backend; mint a token with the plane's key (the CLI has none for this; use a small script with the plane's private key from the dashboard's secrets, or sign with a test key and set the JWKS URI to a static file to test the algorithm alone). If the gateway refuses ES256 tokens, stop and switch to the Cloudflare Tunnel fallback in the design.

- [ ] **Step 2: Deploy.** `gcloud run deploy superci-launcher --source launchers/gcp --region us-west4 --no-allow-unauthenticated --min-instances 0 --set-env-vars …`; grant the gateway's service account `roles/run.invoker`; `gcloud api-gateway api-configs create` and `gateways create`; a Cloud Scheduler job POSTing `/sweep` with the secret every 10 minutes.

- [ ] **Step 3: Verify** as in Task 13 Step 3 against the gateway URL. **Step 4: Commit.**

---

## Part 3: setup and first runs

### Task 15: The GCP project

- [ ] **Step 1: Enable Compute** and create: VPC `superci`; subnet `superci-jobs` in us-west4 (Private Google Access on); on path A a subnet `superci-launcher` in us-east1 and a firewall rule `superci-launcher-443` allowing TCP 443 to tag `superci-launcher`; no other ingress rules; a static external address for the launcher. On path B instead: a Cloud Router and NAT gateway in us-west4, and an Artifact Registry remote repository for Docker Hub with the daemon in the image (Task 12) configured to use it.
- [ ] **Step 2: Service accounts.** `superci-launcher@` with `roles/compute.instanceAdmin.v1` on the project. Nothing else. Confirm `gcloud projects get-iam-policy` shows no broader grant.
- [ ] **Step 3: Quota.** Request `PREEMPTIBLE_CPUS` (spot CPUs) in us-west4 of at least 200 and `CPUS` of at least 50 for on-demand fallback. Expected: the request is filed; approval may take a day.
- [ ] **Step 4: Verify** `gcloud compute networks subnets describe superci-jobs --region us-west4` shows `privateIpGoogleAccess: true`.

### Task 16: The control plane on Cloudflare

- [ ] **Step 1:** Create a Cloudflare account (free plan). Run `target/release/superci dashboard` from the fork, sign in with Cloudflare, choose Cloudflare for the control plane, deploy. Expected: `superci status --json` shows the plane's URL and version `0.19.21`.
- [ ] **Step 2:** From the dashboard, create the GitHub App and install it on the organisation; connect one repository first. Expected: `superci connections list --json` lists it.
- [ ] **Step 3:** Record the plane URL and plane id in `launchers/gcp/deploy/env.example`'s instructions (not the secrets).

### Task 17: Register the launcher and set limits

- [ ] **Step 1:** `target/release/superci runners create gcp --url=https://<launcher>` Expected: `gcp runs jobs now.` and `superci runners list` shows `gcp`.
- [ ] **Step 2:** `superci runners update gcp --max-jobs=100 --monthly-usd=<agreed limit>`; `superci limits update --monthly-usd=<agreed limit>`.
- [ ] **Step 3:** `superci machine update --cpu=2 --ram=8` so `runs-on: superci` means the 2-vCPU machine the comparison priced.

### Task 18: First run and the success checks

- [ ] **Step 1:** In a repository with the App installed, `superci runs create --workflow=.github/workflows/<small one>.yml --quiet`. Expected: the job runs; `superci jobs retrieve <id> --json` shows `cloud: gcp`, `machine_type: e2-standard-2`, `usd_per_hour: 0.0104`, `on_demand` absent or false.
- [ ] **Step 2: Preemption drill.** Start a job that sleeps 5 minutes; `gcloud compute instances simulate-maintenance-event <its name> --zone <its zone>` while it runs. This preempts the spot VM and sets `instance/preempted`; a manual delete does not set it, so it would not exercise the notice. Expected: the job's page says its machine was taken back, and SuperCI runs the job again with `on_demand: true` (spot false) once the job's end is known: for a GitHub job, from GitHub, when its run has finished; for a run from a computer (`superci runs create`), from the sweep's `/status` check (Task 20), at the first sweep after GCE has terminated or deleted the VM. The rerun passes.
- [ ] **Step 3: Nothing left behind.** After a day of traffic: `gcloud compute instances list --filter='labels.superci-plane:*'` shows only VMs younger than their jobs; no `TERMINATED` instances.
- [ ] **Step 4: Cost tracks the estimate.** `superci limits retrieve` and the GCP billing report for the month agree within the overheads the comparison page names. On path B, read the NAT data processed from Cloud Monitoring and put it against the comparison's three rows.
- [ ] **Step 5:** Change the remaining workflows to `runs-on: superci` one pull request at a time, as the SuperCI skill document describes.

### Task 19: A GCP launcher on the dashboard

Added 2026-11-02 after the final review. The design listed dashboard pages for GCP as a non-goal; the account owner asked for the dashboard to cover GCP, so the non-goal is narrowed to Google sign-in in the CLI and the test stand-ins for Google's APIs. This task adds the smallest page support that lets a person add, see and remove a GCP launcher without the command line, and makes a GCP job's pages read as GCP rather than AWS.

**Files:**
- Modify: `crates/cli/src/dashboard.rs` (the Add a provider page's rows, the Runners page's Clouds row and GCP Remove dialog, the job list's and job page's reclaim and machine lines, `RUNNERS_SOON`, the shared input CSS)
- Modify: `crates/cli/src/logos.rs` (the `gcp` provider renders the existing `googlecloud` mark)
- Test: the in-crate tests of both files

**Interfaces:**
- Consumes: `POST /runners/gcp` (Task 6: form field `url`, one `GET <url>/health` expecting `{"agent":"gcp"}`, then the launchers write and a redirect to `/?p=runners`); `cloud_name` from `superci_core::plane`; `off_spot(j)` and `aws_machine(j)` in `dashboard.rs`; the `Launcher { cloud, url }` list in the status.
- Produces: on Add a provider, a GCP row with one `<input type="url" name="url" placeholder="https://launcher.example" required>` and an Add button posting to `/runners/gcp`, shown as "Added" once a `gcp` launcher is registered; on Runners, the Clouds row for `gcp` reads "Its launcher starts a VM for each job · <url>" with the existing Remove; the GCP Remove dialog says adding it back takes its address under Add provider, that jobs already running there finish and their machines end with them, and its button reads "Remove"; `after_reclaim` jobs are labelled with `cloud_name(j["cloud"])` ("after GCP took back its spot machine"); `gcp_machine(j) -> Option<(String, String)>` gives the job page a Machine row (`<machine_type> · <cpu> CPU`, with the `gcp` logo) and a Spot-or-on-demand row ("Spot · $<usd_per_hour> an hour", or "On-demand · <why>" through `off_spot`), and no Region row; `RUNNERS_SOON` no longer lists Google Cloud.

- [ ] **Step 1: Write the failing tests** — the Add row present without a `gcp` launcher and the "Added" pill with one; the Clouds row text and URL; the Remove dialog's subtitle, running-jobs line and button text; the reclaim label for a `gcp` job; `gcp_machine` for a reclaimed and for a spot job; `logo("gcp", 20)` carries the Google Cloud mark.
- [ ] **Step 2: Run them to verify they fail.** `cargo test -p superci --bin superci dashboard:: logos::` — the new tests fail on the missing row, the old copy and the missing function. (Tests that bind a local port fail in a sandbox with "Operation not permitted"; name them, do not count them.)
- [ ] **Step 3: Implement** with the page's existing row, dialog and fact helpers; keep every other cloud's rows unchanged.
- [ ] **Step 4: Run the tests and build.** The same test command, then `cargo build -p superci`. Expected: the new and extended tests pass; the build is clean.
- [ ] **Step 5: Commit.** `git add crates/cli/src/dashboard.rs crates/cli/src/logos.rs && git commit -m "Add and describe a GCP launcher on the dashboard"`

### Task 20: The plane notices a launcher machine that is gone

Added 2026-11-02 after the preemption drill of Task 18. The drill's job was a run from a computer. Its VM posted the reclaim notice, the job's log said why, and GCE deleted the VM; but no GitHub event reports the end of such a job, and the plane had no way to ask a launcher whether a machine still exists. The job stayed `running` for 13 minutes, billed and with no rerun, until it was cancelled by hand. This task gives the launcher a `/status` call and has the sweep ask it about a run's job.

**Files:**
- Modify: `launchers/gcp/launcher.py` (`status` beside `stop`), `launchers/gcp/app.py` (the `/status` route)
- Modify: `crates/core/src/plane.rs` (the check in the sweep in `alarm`; the run-from-a-computer wording of the `/interrupted` answer, which no longer says the job starts again "at once")
- Test: `launchers/gcp/tests/test_stop_sweep.py`, `crates/core/tests/plane.rs`

**Interfaces:**
- Produces: `status(name: str, compute: Compute, cfg: Config) -> str`, looking for the VM across `cfg.zones` as `stop` does: `"running"` while one of this plane's VMs of that name is in a state outside `ENDING_STATES`, `"ending"` while one is `STOPPING` (a VM taken back is, while its notice may still be on its way), `"gone"` when every one is `TERMINATED` or no zone has one; `ValueError` when every VM of that name is another plane's. `POST /status` takes `{"id"}` with the `checked` token and answers `200 {"state": "running" | "ending" | "gone"}`, `400` for a missing `id` or another plane's VM, `503` when Compute fails.
- Consumes (core): `launcher_call(plane_url, launcher, "/status", {"id": machine_id})`, once per sweep, for a job with `provider == "local"`, state `running`, a `machine_id`, a launcher for its cloud in `config.launchers`, and `launched_ms` more than two minutes ago.
- Produces (core): on `"gone"` only, the job's record is read again (a notice may have come during the call; a record that has ended meanwhile is left as it is), and the job ends `failed` with `ended_ms` now and the error `"<cloud> took back its spot machine"` when that record is interrupted, else `"its machine ended before the job did"`, then `settle` and `keep`. The same sweep moves the run on (`advance_run`), which sees the run's job over and, for an interrupted job, starts it again with `off_spot`: a second `/launch` with `spot: false`. An error, a timeout or any other answer, `"ending"` included, changes nothing, and the next sweep asks again. GitHub and GitLab jobs are not asked.

- [ ] **Step 1: Write the failing tests.** Launcher: `status` is running wherever this plane's VM is, gone when no zone has it, gone when it is `TERMINATED`, ending while it is `STOPPING`, refused for another plane's VM, and the route over HTTP (the three answers, 400, 401, 503). Core: a run's job on GCP whose VM posts its notice and whose launcher then answers `gone` is failed with the reclaim error and started again with a second `/launch` carrying `spot: false`; a launcher answering `running`, `ending`, `503`, another state or something that is not JSON changes nothing, and `gone` without a notice fails the job with no rerun; a notice that arrives while the sweep waits for `/status` is what the job's error says; a GitHub job's machine is never asked. The core tests are registered in `every_test_here_holds_with_each_call_and_store_operation_failing`.
- [ ] **Step 2: Run them to verify they fail.** `cd launchers/gcp && .venv/bin/python -m pytest tests/test_stop_sweep.py -q` fails importing `status`; `cargo test -p superci-core --test plane -- a_run_from_a_computers_job` fails with the job still `running` and no `/status` sent.
- [ ] **Step 3: Implement** `status` and its route beside `stop`; in the sweep, the check after the states it skips and before the block for machines that never began their job, ending the job as its machine's end would and leaving the rerun to `advance_run`.
- [ ] **Step 4: Run the tests.** `.venv/bin/python -m pytest tests -q`; `cargo test -p superci-core --test plane` and `cargo test -p superci-core --lib` (two `runner_service` tests bind a local port and fail in a sandbox with "Operation not permitted"; name them, do not count them). Expected: all pass.
- [ ] **Step 5: Commit**, one commit per part: `git add launchers/gcp/launcher.py launchers/gcp/app.py launchers/gcp/tests/test_stop_sweep.py && git commit -m "Answer whether a job VM is still there, through POST /status on the GCP launcher"`, then `git add crates/core/src/plane.rs crates/core/tests/plane.rs && git commit -m "End a run's job whose launcher says its machine is gone, so its run starts it again off spot"`.
