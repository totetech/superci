# Runner image

The launcher starts every job VM from the newest image of the family `superci-runner` (its `IMAGE_FAMILY`). The image is
Ubuntu 24.04 LTS (`ubuntu-2404-lts-amd64`) with:

- Docker (`docker.io` from Ubuntu), with `max-concurrent-downloads` 16 in `/etc/docker/daemon.json`
- `git jq zip unzip xz-utils zstd build-essential pkg-config curl`
- `/etc/sysctl.d/90-superci.conf`: `kernel.apparmor_restrict_unprivileged_userns = 0` and
  `net.ipv4.ip_local_reserved_ports = 50000-59999`, as the AWS machines' start-up script sets them at boot
- a `runner` user in the `docker` group, with passwordless `sudo` as on GitHub's runners
- GitHub's runner (`actions-runner-linux-x64`) unpacked in `/home/runner`, owned by `runner`, with its dependencies
  installed (`bin/installdependencies.sh`); `RUNNER_ALLOW_RUNASROOT` is not set, so `run.sh` is started as `runner`, as
  `startup.sh.tmpl` does

The image is made with the guest OS features `GVNIC`, `UEFI_COMPATIBLE` and `VIRTIO_SCSI_MULTIQUEUE`, because an image made
from a disk does not inherit them and every job VM has a gVNIC interface (c4d accepts no other).

Status: both scripts pass `bash -n` and `shellcheck`, and `build-image.sh` has been run against a stand-in for `gcloud`;
`install.sh` has not run on a VM and no image has been built yet. The first build is done once the GCP project exists, in
Part 3 of `docs/superpowers/plans/2026-11-02-gcp-spot-launcher.md`.

## Build

```
launchers/gcp/image/build-image.sh PROJECT ZONE [RUNNER_VERSION]
```

`RUNNER_VERSION` is a release of `github.com/actions/runner` without the `v`; the default is in the script. Set the
launcher's `RUNNER_VERSION` to the same number, since the startup script logs a difference between it and the installed
runner. Set `SUBNET` (a name, or a path GCE takes) when the project has no `default` network; the build VM needs a route
to the internet (an external address, or Cloud NAT).

The script creates `superci-image-build-<timestamp>` (e2-standard-4, no service account) with `install.sh` as its startup
script and the version as the metadata `runner-version`, and reads its serial console for `superci: image ready`. It
fails with the serial log when that line has not appeared in 15 minutes, or at once when `install.sh` printed
`superci: image build failed`. It then stops the VM, creates `superci-runner-<timestamp>` in the family from its boot disk,
and deletes the VM and the disk. The VM is deleted on failure and on interruption too. A run that is killed outright
(SIGKILL) leaves its VM; find it with
`gcloud compute instances list --filter='name~^superci-image-build-'`. Running the script again is safe: every run has its
own names.

Older images stay in the project; the family points to the newest. Delete the ones that are no longer wanted with
`gcloud compute images delete`.

## Verify

Start a throwaway VM from the family, as the launcher does (no service account, a gVNIC interface), and time it from the
create command to the first SSH command that is accepted (add `--tunnel-through-iap` to the `ssh` commands when the VM has
no external address):

```
start=$(date +%s)
gcloud compute instances create superci-image-check --project PROJECT --zone ZONE --machine-type e2-standard-2 \
  --image-family superci-runner --network-interface=nic-type=GVNIC,subnet=SUBNET --no-service-account --no-scopes
until gcloud compute ssh superci-image-check --project PROJECT --zone ZONE --quiet --command true; do sleep 2; done
echo "$(( $(date +%s) - start )) seconds from create to SSH"
```

The interface is given as `--network-interface=nic-type=GVNIC,subnet=SUBNET`, which replaces `--subnet`; the two cannot be
combined. In a project that uses its `default` network, `--network-interface=nic-type=GVNIC` alone does the same.

Three things must hold:

1. The time printed is under 60 seconds.
2. Docker works for the runner user:
   `gcloud compute ssh superci-image-check --project PROJECT --zone ZONE --command 'sudo -u runner -H docker run --rm hello-world'`
   prints Docker's greeting.
3. The runner reports the pinned version, on the first line of its output (`run.sh` then prints two lines of its own):
   `gcloud compute ssh superci-image-check --project PROJECT --zone ZONE --command 'cd /home/runner && sudo -u runner -H ./run.sh --version'`.
   Run it as `runner`: as root, `run.sh` refuses to start.

Also check that the sysctl file took effect at boot:
`gcloud compute ssh superci-image-check --project PROJECT --zone ZONE --command 'sysctl kernel.apparmor_restrict_unprivileged_userns net.ipv4.ip_local_reserved_ports'`
prints `0` and `50000-59999`.

Then delete the VM: `gcloud compute instances delete superci-image-check --project PROJECT --zone ZONE --quiet`.

The launcher also starts c4d machines, which boot from Hyperdisk only; start one VM of that kind from the family too
(`--machine-type c4d-standard-2 --boot-disk-type hyperdisk-balanced`, in a zone that has it) and check it reaches SSH.

## Keeping it current

A runner older than GitHub's current release may update itself before it takes its job, which delays the job. Rebuild the
image when GitHub publishes a new runner release, and set the launcher's `RUNNER_VERSION` to the new number.
