import base64
import os
import stat
import subprocess

import pytest

from launcher import instance_name, render_shutdown, render_startup

NOTICE = "https://plane.example/interrupted?runner=x&t=tok"
# How the notice is posted, as AWS's spot machines post theirs: each try bounded, tried again, and the plane's answer run with bash.
POST = "curl -s -f -m 5 --retry 4 --retry-all-errors -X POST"


def test_instance_names_are_valid_for_gce_and_stable():
    assert instance_name("SuperCI-abc123-456789") == "superci-abc123-456789"
    assert instance_name("1job") == "r-1job"
    assert len(instance_name("a" * 80)) == 63
    assert not instance_name("x" * 62 + "--").endswith("-")
    assert instance_name("Acme_CI-1") == instance_name("Acme_CI-1") == "acme-ci-1"


def test_instance_name_edge_cases_are_still_valid():
    assert instance_name("a.b/c d") == "a-b-c-d"
    assert instance_name("-x") == "r--x"
    assert instance_name("é1") == "r--1"
    assert instance_name("") == "r"  # r- with its trailing dash cut
    assert instance_name("---") == "r"
    for runner in ("", "9", "A" * 100, "x" * 62 + "--", "ünï_cödé", "1" * 70):
        name = instance_name(runner)
        assert 1 <= len(name) <= 63 and name[0].isalpha() and name[0].islower() and not name.endswith("-"), runner
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in name), runner


def test_a_spot_script_watches_for_preemption_and_a_plain_one_does_not():
    s = render_startup("QUJD", None, 70, NOTICE, "2.330.0")
    assert "shutdown -h +70" in s and "instance/preempted" in s and NOTICE in s and "--jitconfig QUJD" in s and s.rstrip().endswith("poweroff")
    assert f"{POST} '{NOTICE}' 2>/dev/null | bash &>/dev/null" in s
    assert "instance/preempted" not in render_startup("QUJD", None, 70, None, "2.330.0")


def test_the_shutdown_script_is_made_only_with_a_notice_and_checks_for_preemption_before_posting():
    assert render_shutdown(None) is None
    s = render_shutdown(NOTICE)
    assert s.startswith("#!/bin/bash\n")
    check = """[ "$(curl -s -m 2 -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/preempted')" = TRUE ] || exit 0"""
    post = f"{POST} '{NOTICE}' 2>/dev/null | bash &>/dev/null"
    assert check in s and post in s and s.index(check) < s.index(post)
    assert _bash_syntax_error(s) == ""
    quoted = render_shutdown("https://x.example/a'; reboot; echo '")
    assert "'https://x.example/a'\\''; reboot; echo '\\'''" in quoted and _bash_syntax_error(quoted) == ""


SHUTDOWN_CURL = """#!/bin/bash
case "$*" in
  *instance/preempted*) printf %s 'PREEMPTED';;
  *) echo "$*" >> DIR/posted; echo "echo answered >> DIR/answered";;
esac
"""


@pytest.mark.parametrize("preempted", ["TRUE", "FALSE", ""])
def test_running_the_shutdown_script_posts_the_notice_and_runs_the_answer_only_when_preempted(tmp_path, preempted):
    # (The metadata server answers TRUE once GCE preempts the VM; FALSE, or nothing, at any other shutdown.)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(SHUTDOWN_CURL.replace("PREEMPTED", preempted).replace("DIR", str(tmp_path)))
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    done = subprocess.run(["bash", "-c", render_shutdown(NOTICE)], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                          capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stderr
    if preempted == "TRUE":
        assert (tmp_path / "posted").read_text().splitlines() == [f"-s -f -m 5 --retry 4 --retry-all-errors -X POST {NOTICE}"]
        assert (tmp_path / "answered").read_text() == "answered\n"
    else:
        assert not (tmp_path / "posted").exists() and not (tmp_path / "answered").exists()


def test_a_failing_runner_gets_its_hook_and_bad_base64_is_refused():
    s = render_startup("QUJD", "d2h5", 10, None, "2.330.0")
    assert "ACTIONS_RUNNER_HOOK_JOB_STARTED" in s and "d2h5" in s
    with pytest.raises(ValueError): render_startup("abc; rm -rf /", None, 10, None, "2.330.0")


def test_a_plain_runner_has_no_hook():
    assert "ACTIONS_RUNNER_HOOK_JOB_STARTED" not in render_startup("QUJD", None, 10, None, "2.330.0")


def test_values_that_would_reach_a_shell_are_refused():
    for jit in ("", "QUJD$(id)", "QU JD", "QUJD\n", "QUJD'"):
        with pytest.raises(ValueError):
            render_startup(jit, None, 10, None, "2.330.0")
    for fail in ("", "d2h5; reboot", "d2h5'", "d2 h5", "d2h5\n"):
        with pytest.raises(ValueError):
            render_startup("QUJD", fail, 10, None, "2.330.0")
    for minutes in (0, -5):
        with pytest.raises(ValueError):
            render_startup("QUJD", None, minutes, None, "2.330.0")
    render_startup("a+b/c==", "a+b/c==", 1, None, "2.330.0")  # everything base64 may contain is accepted


def test_a_quote_in_the_notice_or_version_stays_inside_its_quotes():
    s = render_startup("QUJD", None, 10, "https://x.example/a'; reboot; echo '", "1.0'; reboot; echo '")
    assert "'https://x.example/a'\\''; reboot; echo '\\'''" in s
    assert "'1.0'\\''; reboot; echo '\\'''" in s
    assert _bash_syntax_error(s) == ""


def test_a_notice_that_names_a_placeholder_is_left_as_it_is():
    s = render_startup("QUJD", None, 10, "https://x.example/__JIT__/__RUNNER_VERSION__/__MAX_MINUTES__", "2.330.0")
    assert "POST 'https://x.example/__JIT__/__RUNNER_VERSION__/__MAX_MINUTES__'" in s and "--jitconfig QUJD" in s


def test_the_steps_come_in_order():
    s = render_startup("QUJD", "d2h5", 70, NOTICE, "2.330.0")
    steps = ["#!/bin/bash", "/dev/console", "shutdown -h +70", "instance/preempted", "base64 -d", "--jitconfig QUJD"]
    positions = [s.index(step) for step in steps]
    assert positions == sorted(positions) and s.startswith("#!/bin/bash\n")
    assert s.rstrip().splitlines()[-1] == "poweroff" and s.count("poweroff") == 1


@pytest.mark.parametrize("fail,notice", [(None, None), (None, NOTICE), ("d2h5", None), ("d2h5", NOTICE)])
def test_every_variant_is_valid_bash_with_no_placeholder_left(fail, notice):
    s = render_startup("QUJD", fail, 70, notice, "2.330.0")
    assert _bash_syntax_error(s) == ""
    assert "__" not in s


def _bash_syntax_error(script: str) -> str:
    return subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True).stderr


def _run(tmp_path, fail, notice, expected="2.330.0"):
    """The rendered script under bash with its commands replaced by stubs; what the stubs saw, one file each."""
    bin_dir, home = tmp_path / "bin", tmp_path / "home"
    bin_dir.mkdir(), home.mkdir()

    def stub(path, body):
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    # The metadata server answers TRUE only once the runner has started, as in production, where the value changes at
    # preemption, while the job runs: a watcher that blocks the script before the runner starts never sees it. (Until
    # then it answers FALSE, after three seconds of holding the request.) The plane's link is recorded when posted to, and the
    # plane's answer is a script that records that it was run.
    stub(bin_dir / "curl", f'''case "$*" in
  *instance/preempted*) for i in $(seq 30); do [ -e {tmp_path}/runner-args ] && echo TRUE && exit; sleep 0.1; done; echo FALSE;;
  *) echo "$*" >> {tmp_path}/posted; echo posted >> {tmp_path}/events; echo "echo answered >> {tmp_path}/events; touch {tmp_path}/answered";;
esac
''')
    stub(bin_dir / "sudo", 'while [ "$1" != -H ]; do shift; done; shift; exec "$@"\n')  # sudo -u runner -H ...
    stub(bin_dir / "timeout", 'shift; exec "$@"\n')
    stub(bin_dir / "shutdown", f'echo "$*" >> {tmp_path}/shutdown\n')
    stub(bin_dir / "poweroff", f'echo poweroff >> {tmp_path}/events\n')
    # The runner: records how it was started, then stays up (as a job does) until the plane's answer to the post has been run.
    wait = "yes" if notice else ""
    # (`run.sh --version` as GitHub's runner prints it: the version, then two lines of its wrapper scripts.)
    stub(home / "run.sh", f'''if [ "$1" = --version ]; then echo 2.330.0; echo "Runner listener exit with 0 return code, stop the service, no retry needed."; echo "Exiting runner..."; exit; fi
echo "$*" > {tmp_path}/runner-args
echo runner-started >> {tmp_path}/events
echo "${{ACTIONS_RUNNER_HOOK_JOB_STARTED-unset}}" > {tmp_path}/runner-hook
[ -z "${{ACTIONS_RUNNER_HOOK_JOB_STARTED}}" ] || "${{ACTIONS_RUNNER_HOOK_JOB_STARTED}}" > {tmp_path}/hook-said
[ -z "{wait}" ] || for i in $(seq 50); do [ -e {tmp_path}/answered ] && break; sleep 0.1; done
echo runner-ended >> {tmp_path}/events
''')
    script = render_startup("QUJD", fail, 70, notice, expected)
    # (Not under test: process substitution, which not every sandbox allows; `bash -n` has checked this line.)
    script = script.replace("exec > >(tee /var/log/superci.log > /dev/console) 2>&1", f"exec > {tmp_path}/log 2>&1")
    for real, fake in (("/home/runner", str(home)), ("/tmp/superci-fail.sh", str(tmp_path / "fail.sh"))):
        script = script.replace(real, fake)
    try:
        done = subprocess.run(["bash", "-c", script], env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        pytest.fail("the script never got past its watcher to start the runner: the watcher must run in the background")
    assert done.returncode == 0, done.stderr
    return lambda name: (tmp_path / name).read_text() if (tmp_path / name).exists() else None


def test_running_a_spot_script_posts_the_notice_once_while_the_runner_runs_and_runs_the_answer(tmp_path):
    seen = _run(tmp_path, None, NOTICE)
    assert seen("shutdown").strip() == "-h +70"
    assert seen("runner-args").strip() == "--jitconfig QUJD" and seen("runner-hook").strip() == "unset"
    assert seen("posted").splitlines() == [f"-s -f -m 5 --retry 4 --retry-all-errors -X POST {NOTICE}"]
    # (Posted while the runner ran, and the plane's answer run with bash.)
    assert seen("events").splitlines() == ["runner-started", "posted", "answered", "runner-ended", "poweroff"]


def test_running_a_plain_script_posts_nothing(tmp_path):
    seen = _run(tmp_path, None, None)
    assert seen("posted") is None and seen("events").splitlines() == ["runner-started", "runner-ended", "poweroff"]


def test_a_failing_runner_is_started_with_a_hook_that_says_why_and_fails(tmp_path):
    reason = "no capacity for this job"
    seen = _run(tmp_path, base64.b64encode(reason.encode()).decode(), None)
    assert seen("runner-args").strip() == "--jitconfig QUJD"
    assert seen("runner-hook").strip() == str(tmp_path / "fail.sh")
    assert seen("hook-said") == reason
    assert (tmp_path / "fail.sh").stat().st_mode & 0o755 == 0o755
    assert subprocess.run([str(tmp_path / "fail.sh")], capture_output=True).returncode == 1


def test_the_version_is_logged_and_a_different_one_is_flagged_without_stopping_the_runner(tmp_path):
    (tmp_path / "same").mkdir(), (tmp_path / "other").mkdir()
    same = _run(tmp_path / "same", None, None)
    assert "superci: runner 2.330.0 (expected 2.330.0)" in same("log") and "differs" not in same("log")
    other = _run(tmp_path / "other", None, None, expected="2.331.0")  # the stub runner is 2.330.0
    assert "superci: runner 2.330.0 (expected 2.331.0)" in other("log") and "differs" in other("log")
    assert other("runner-args").strip() == "--jitconfig QUJD" and other("events").splitlines()[-1] == "poweroff"
