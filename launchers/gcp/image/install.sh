#!/bin/bash
# Installs what a job VM needs onto the build VM that build-image.sh creates (Ubuntu 24.04), as root, as that VM's
# startup script: Docker, the tools GitHub's runner image has, a `runner` user, and GitHub's runner at /home/runner.
# The runner's version is the instance metadata `runner-version`. Its output goes to serial port 1, where
# build-image.sh looks for the last line, `superci: image ready`, or for `superci: image build failed`.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# (Ubuntu's needrestart, run after each apt-get, would otherwise ask which services to restart, or restart them, mid-build.)
export NEEDRESTART_SUSPEND=1

trap 'echo "superci: image build failed at line $LINENO" >&2' ERR

die() {
  echo "superci: image build failed: $*" >&2
  exit 1
}

# Runs a command up to five times: a first boot has package work of its own going on, and a mirror or GitHub can fail once.
retry() {
  local attempt
  for attempt in 1 2 3 4 5; do
    "$@" && return 0
    echo "superci: attempt $attempt of 5 failed: $*" >&2
    sleep 10
  done
  return 1
}

aptget() {
  apt-get -o DPkg::Lock::Timeout=600 -o Acquire::Retries=5 -y -q "$@"
}

version=$(curl -fsS --retry 5 -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/runner-version) \
  || die "the instance has no runner-version metadata"
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "runner-version '$version' is not a release number"

echo "superci: installing packages"
retry aptget update
retry aptget install docker.io git jq zip unzip xz-utils zstd build-essential pkg-config curl

# Docker pulls sixteen layers of an image at once (three unless told), as the AWS machines' start-up script sets it.
echo "superci: configuring Docker"
daemon_json=/etc/docker/daemon.json
mkdir -p /etc/docker
[ -e "$daemon_json" ] || echo '{}' > "$daemon_json"
jq '.["max-concurrent-downloads"] = 16' "$daemon_json" > "$daemon_json.new"
mv "$daemon_json.new" "$daemon_json"
systemctl enable docker
systemctl restart docker

# As GitHub's Ubuntu runners, and as the AWS machines' start-up script sets them at boot: unprivileged user namespaces
# (Ubuntu 24.04 restricts them, which breaks Chromium's sandbox), and the ports a job's own servers listen on in
# 50000-59999 never handed out to the connections the machine opens (Linux hands those out from 32768-60999).
echo "superci: configuring sysctl"
cat > /etc/sysctl.d/90-superci.conf <<'SYSCTL'
kernel.apparmor_restrict_unprivileged_userns = 0
net.ipv4.ip_local_reserved_ports = 50000-59999
SYSCTL
sysctl -q -p /etc/sysctl.d/90-superci.conf

# The runner's user: in the docker group (which docker.io created), and with passwordless sudo as on GitHub's runners,
# where workflows call `sudo apt-get install` and the like. The docker group is root-equivalent already, and a job VM
# lives for one job.
echo "superci: creating the runner user"
id runner &>/dev/null || useradd --create-home --home-dir /home/runner --shell /bin/bash runner
usermod -aG docker runner
echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
chmod 0440 /etc/sudoers.d/runner
visudo -cf /etc/sudoers.d/runner

# GitHub's runner, unpacked in the runner user's home. RUNNER_ALLOW_RUNASROOT is not set anywhere: run.sh is started
# as `runner`.
echo "superci: installing the runner $version"
tarball=$(mktemp)
retry curl -fsSL -o "$tarball" \
  "https://github.com/actions/runner/releases/download/v$version/actions-runner-linux-x64-$version.tar.gz"
tar -xzf "$tarball" -C /home/runner
rm -f "$tarball"
retry /home/runner/bin/installdependencies.sh
chown -R runner:runner /home/runner

# What a job VM relies on, tried as it uses it: Docker as `runner`, and the version run.sh reports on its first line
# (the lines after it are the wrapper script's own).
echo "superci: checking the image"
retry sudo -u runner -H docker info > /dev/null
output=$(cd /home/runner && timeout 60 sudo -u runner -H ./run.sh --version)
installed=${output%%$'\n'*}
[ "$installed" = "$version" ] || die "run.sh reports '$installed', expected '$version'"

aptget clean
sync
echo 'superci: image ready'
