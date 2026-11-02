#!/bin/bash
# Builds the GCE image the launcher starts job VMs from: a temporary VM runs install.sh as its startup script, and
# when it reports `superci: image ready` on its serial console the VM is stopped, its boot disk becomes an image in the
# family `superci-runner`, and the VM and its disk are deleted (also when anything fails on the way).
#
#   build-image.sh PROJECT ZONE [RUNNER_VERSION]
#
# RUNNER_VERSION is the GitHub runner release to install, without the `v`. Environment: SUBNET, a subnet of ZONE's region
# (a name, or a path GCE takes), for a project that has no `default` network; the VM needs a route to the internet.
# NO_ADDRESS=1 gives the VM no external address, for a project whose policy refuses them: the subnet's region then
# needs Cloud NAT for that route.
set -euo pipefail

# The current release of github.com/actions/runner on 2 November 2026 (v2.338.2, published 26 October).
default_runner_version=2.338.2
family=superci-runner
wait_seconds=900

usage() {
  echo "usage: $0 PROJECT ZONE [RUNNER_VERSION]" >&2
  exit 2
}

[ $# -ge 2 ] && [ $# -le 3 ] || usage
project=$1
zone=$2
runner_version=${3:-$default_runner_version}
if ! [[ $runner_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "superci: '$runner_version' is not a runner release number such as $default_runner_version" >&2
  exit 2
fi

install_script=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh
[ -f "$install_script" ] || { echo "superci: $install_script not found" >&2; exit 1; }
command -v gcloud > /dev/null || { echo "superci: gcloud not found" >&2; exit 1; }

stamp=$(date -u +%Y%m%d-%H%M%S)
vm=superci-image-build-$stamp
image=superci-runner-$stamp

subnet_args=()
if [ -n "${SUBNET:-}" ]; then
  subnet_args=(--subnet "$SUBNET")
fi
if [ "${NO_ADDRESS:-}" = 1 ]; then
  subnet_args+=(--no-address)
fi

# Deletes the build VM and, with it, its disk, whatever the outcome. A failed delete is said, and fails the script.
vm_requested=0
cleanup() {
  local status=$?
  trap - EXIT
  if [ "$vm_requested" = 1 ]; then
    echo "superci: deleting $vm" >&2
    if ! gcloud compute instances delete "$vm" --project "$project" --zone "$zone" --delete-disks all --quiet; then
      echo "superci: $vm in $zone was not deleted; if it exists, delete it and its disk" >&2
      [ "$status" -ne 0 ] || status=1
    fi
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Says why the build failed, then the VM's serial log, and stops.
fail_with_log() {
  echo "superci: $1; the serial log of $vm follows" >&2
  printf '%s\n' "$log" >&2
  exit 1
}

echo "superci: creating $vm in $zone of $project (runner $runner_version)"
vm_requested=1
gcloud compute instances create "$vm" \
  --project "$project" --zone "$zone" \
  --machine-type e2-standard-4 \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
  --boot-disk-size 10GB --boot-disk-type pd-balanced --boot-disk-auto-delete \
  --no-service-account --no-scopes \
  --metadata "runner-version=$runner_version" \
  --metadata-from-file "startup-script=$install_script" \
  ${subnet_args[@]+"${subnet_args[@]}"}

echo "superci: waiting up to $((wait_seconds / 60)) minutes for the VM to finish installing"
deadline=$((SECONDS + wait_seconds))
log=
while :; do
  # (Unreadable while the VM is starting.)
  log=$(gcloud compute instances get-serial-port-output "$vm" --project "$project" --zone "$zone" 2> /dev/null) || log=
  case $log in
    *'superci: image ready'*) break ;;
    *'superci: image build failed'*) fail_with_log "install.sh failed" ;;
  esac
  [ "$SECONDS" -lt "$deadline" ] || fail_with_log "'superci: image ready' did not appear in $((wait_seconds / 60)) minutes"
  sleep 10
done

echo "superci: stopping $vm"
gcloud compute instances stop "$vm" --project "$project" --zone "$zone"

# (The boot disk of an instance is named for it.) An image made from a disk does not inherit the guest OS features of the
# image the disk came from, and every job VM has a gVNIC interface (c4d accepts no other), so they are given here.
echo "superci: creating image $image in family $family"
gcloud compute images create "$image" \
  --project "$project" --family "$family" \
  --source-disk "$vm" --source-disk-zone "$zone" \
  --guest-os-features=GVNIC,UEFI_COMPATIBLE,VIRTIO_SCSI_MULTIQUEUE

echo "superci: image $image is the newest in family $family of $project (runner $runner_version)"
