"""The real thing: install.sh against a real Docker daemon, a real container, and a mock Wave cloud.

Opt-in, because it builds the image and starts containers (about two minutes):

    BWWATCH_DOCKER_E2E=1 python3 -m unittest tests.test_docker_e2e -v

What it does: copies the project into a throw-away folder, changes only two lines of the copy's compose file
(the build has no network, and the container uses the host network so it can reach the mock cloud listening on
the host's loopback), then drives the real ``install.sh`` through a pseudo-terminal like a person would -
answering the real wizard's questions - and afterwards exercises ``bwctl``, a restart, a crash, a
reconfigure, a cancelled reconfigure and the uninstall. It only ever touches the container ``bwwatch``, the
image ``bwwatch:local`` and two decoy objects it creates itself to prove the uninstall leaves other things alone.
Do not run it on a machine where you run a real bwwatch: the installer would (rightly) refuse to touch it.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional

from .mock_wave import MockWave, REDIRECT
from .pty_driver import CTRL_C, converse

ROOT = Path(__file__).resolve().parents[1]
ENABLED = os.environ.get("BWWATCH_DOCKER_E2E") == "1"
# "bridge": Docker's normal networking, as on a VPS (the compose file's own private network; the mock cloud listens on
# the docker bridge's address). "host": the container shares the host's network - for a daemon that has no bridge.
NETWORK = os.environ.get("BWWATCH_E2E_NETWORK", "bridge")
FAULT_REQUEST = "GET /wave/getNotifications?username={account_id}&macAddress={mac}"
AWKWARD_JSON = '{"X-Awkward": "it\'s $HOME # not a comment"}'
DECOY_CONTAINER, DECOY_VOLUME, BASE_IMAGE = "bwdecoy", "bwdecoy-vol", "python:3.12-slim-bookworm"

FOLDER = (r"Install here anyway\? \[y/N\] ", "y")
LAST_QUESTION = (r"Did the .bwwatch started. message arrive\? \[Y/n\] ", "y")


def docker(*args: str, check: bool = False, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def listening_ports() -> set:
    """TCP ports something is listening on right now, read from the kernel's own table."""
    found = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 3 and fields[3] == "0A":  # state LISTEN
                found.add(int(fields[1].rsplit(":", 1)[1], 16))
    return found


def docker_objects() -> Dict[str, set]:
    """Names of every container, network and volume, so before and after can be compared."""
    def names(*args: str) -> set:
        return {n for n in docker(*args, "--format", "{{.Names}}" if args[0] == "ps" else "{{.Name}}").stdout.split() if n}
    return {"containers": names("ps", "-a"), "networks": names("network", "ls"), "volumes": names("volume", "ls")}


def docker_available() -> bool:
    try:
        return docker("info", timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(ENABLED, "set BWWATCH_DOCKER_E2E=1 to run the real-Docker end-to-end test")
@unittest.skipUnless(sys.platform.startswith("linux"), "the mock cloud is reached over the host network (Linux)")
class RealInstall(unittest.TestCase):
    """One scenario, in order: the numbered tests build on each other."""

    installed = False

    # --- the throw-away server ----------------------------------------------
    @classmethod
    def setUpClass(cls) -> None:
        if not docker_available():
            raise unittest.SkipTest("no Docker daemon is reachable")
        if docker("inspect", "-f", "{{.Id}}", "bwwatch").returncode == 0:
            raise unittest.SkipTest("a container called bwwatch already exists here; refusing to touch it")
        if docker("network", "inspect", "bwwatch_default").returncode == 0:
            raise unittest.SkipTest("a network called bwwatch_default already exists here (left by an aborted run?): "
                                    "remove it with  docker network rm bwwatch_default  and run again")
        cls.base = Path(tempfile.mkdtemp(prefix="bwe2e."))
        cls.home, cls.tmp, cls.elsewhere = (cls.base / n for n in ("home", "tmp", "elsewhere"))
        for folder in (cls.home, cls.tmp, cls.elsewhere):
            folder.mkdir()
        cls.dir = cls.base / "opt" / "bwheater"
        cls.dir.mkdir(parents=True)
        for name in ("install.sh", "bwctl", "Dockerfile", ".env.example"):
            shutil.copy2(ROOT / name, cls.dir / name)
        shutil.copytree(ROOT / "bwwatch", cls.dir / "bwwatch", ignore=shutil.ignore_patterns("__pycache__"))

        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        changes = [("    build: .\n", "    build:\n      context: .\n      network: none   # test-only: nothing to download\n")]
        if NETWORK == "host":
            changes.append(("    restart: unless-stopped\n", "    restart: unless-stopped\n    network_mode: host   # test-only: the mock cloud is on the host's loopback\n"))
        for old, new in changes:
            assert old in compose, old
            compose = compose.replace(old, new, 1)
        (cls.dir / "docker-compose.yml").write_text(compose, encoding="utf-8")

        if NETWORK == "bridge":
            gateway = docker("network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}").stdout.strip()
            cls.mock = MockWave(host="0.0.0.0", public_host=gateway).start()
        else:
            cls.mock = MockWave().start()
        cls.docker_before = docker_objects()
        cls.addClassCleanup(cls.mock.stop)
        cls.login_code = cls.mock.issue_login_code("e2e-login-code-" + "a" * 24)
        # the entry from the Wave app's Notifications tab, as the app words it: a fault that cleared by itself
        cls.mock.notifications = {"notifications": [{"title": "Fault 10", "message": "(Cleared) Superheat Fault", "timestamp": 1791134100}]}
        # What a person would have typed into .env if their servers lived elsewhere: the wizard keeps these.
        # (the last line is an awkward value - quotes, a dollar sign, a # - to prove it survives Compose's .env rules)
        (cls.dir / ".env").write_text(
            "BW_API_BASE=%s\nBW_AUTH_BASE=%s/auth\nBW_ALLOW_INSECURE_HTTP=1\nNTFY_URL=%s/ntfy\nBW_HTTP_TIMEOUT_SECONDS=10\n"
            "BW_EXTRA_HEADERS=\"{\\\"X-Awkward\\\": \\\"it's \\$HOME # not a comment\\\"}\"\n"
            % (cls.mock.url, cls.mock.url, cls.mock.url), encoding="utf-8")
        os.chmod(cls.dir / ".env", 0o600)
        cls.baseline = {p.name for p in cls.dir.iterdir()}
        cls.addClassCleanup(cls.clean_up)

    @classmethod
    def clean_up(cls) -> None:
        docker("rm", "-f", "bwwatch", DECOY_CONTAINER)
        docker("volume", "rm", "-f", DECOY_VOLUME)
        docker("image", "rm", "bwwatch:local")
        docker("network", "rm", "bwwatch_default")  # only exists if a step failed before the uninstall
        if (cls.dir / "data").exists():  # files owned by the container's user: let a container remove them
            docker("run", "--rm", "--network", "none", "-v", "%s:/data" % (cls.dir / "data"), "--entrypoint", "find",
                   BASE_IMAGE, "/data", "-mindepth", "1", "-delete")
        shutil.rmtree(cls.base, ignore_errors=True)

    @classmethod
    def env(cls, **extra: str) -> Dict[str, str]:
        env = dict(os.environ, HOME=str(cls.home), TMPDIR=str(cls.tmp), NO_COLOR="1", LANG="C")
        env.update(extra)
        return env

    # --- helpers --------------------------------------------------------------
    def installer(self, *args: str, script: Optional[List] = None, timeout: float = 900, patience: float = 400):
        return converse(["sh", str(self.dir / "install.sh"), *args], script or [], env=self.env(), cwd=str(self.elsewhere),
                        timeout=timeout, patience=patience)

    def ctl(self, *argv: str, stdin: str = "", script: str = "bwctl", timeout: float = 300) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(self.dir / script), *argv], input=stdin, capture_output=True, text=True,
                              cwd=str(self.elsewhere), env=self.env(), timeout=timeout)

    def container(self, field: str) -> str:
        done = docker("inspect", "-f", field, "bwwatch")
        return done.stdout.strip() if done.returncode == 0 else ""

    def wait_for(self, what: str, condition, seconds: float = 90) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(1)
        self.fail("timed out waiting for " + what)

    def need_install(self) -> None:
        if not type(self).installed:
            self.skipTest("the install step did not finish")
        if self.container("{{.State.Running}}") != "true":
            self.ctl("start")
            self.wait_for("the container to run", lambda: self.container("{{.State.Running}}") == "true")

    def wizard_answers(self) -> List:
        return [
            FOLDER,
            (r"Press Enter to begin ", ""),
            (r"Your choice \[1\]: ", "1"),                                   # ntfy
            (r"Press Enter once you have subscribed in the app ", ""),
            (r"Did it arrive on your device\? \[1\]: ", "1"),
            (r"Time zone \[UTC\]: ", "America/Chicago"),
            (r"Is that right\? \[Y/n\] ", "y"),
            (r"Paste the address here[^\n]*: ", "%s?state=x&code=%s" % (REDIRECT, self.login_code)),
            (r"Is that your water heater\? \[Y/n\] ", "y"),
            (r"Your choice \[3\]: ", "1"),                                   # "I have the request"
            (r"The request \(blank to skip\): ", FAULT_REQUEST),
            (r"Does this look like your notifications\? \[Y/n\] ", "y"),
            (r"Change advanced options\?[^\n]*\[y/N\] ", "n"),
            (r"Save them\? \[Y/n\] ", "y"),
            LAST_QUESTION,
        ]

    # --- 1. the install --------------------------------------------------------
    def test_1_install_walks_through_everything(self):
        listening_before = listening_ports()
        status, text = self.installer(script=self.wizard_answers())
        self.assertEqual(status, 0, text[-3000:])
        self.assertIn("bwwatch listens on no port", text)
        self.assertIn("The container publishes no port", text)
        self.assertIn("Superheat Fault", text, "the wizard showed what it found in the Notifications list")
        self.assertIn("cleared", text)
        for part in ("Part 1 of 4", "Part 2 of 4", "Part 3 of 4", "Part 4 of 4"):
            self.assertIn(part, text)
        self.assertIn("Built the image bwwatch:local", text)
        self.assertIn("The guided setup is complete", text)
        self.assertIn("All ", text)
        self.assertIn("reached the service exactly as chosen", text, "Docker Compose read the new .env back unchanged")
        self.assertIn("Every check passed", text)
        self.assertIn("bwwatch is installed and working", text)
        type(self).installed = True

        # the service really is up, restarts by itself, and is locked down
        self.assertEqual(self.container("{{.State.Running}}"), "true")
        self.assertEqual(self.container("{{.HostConfig.RestartPolicy.Name}}"), "unless-stopped")
        self.assertEqual(self.container("{{.HostConfig.ReadonlyRootfs}}"), "true")
        self.assertIn("ALL", self.container("{{.HostConfig.CapDrop}}"))
        self.assertIn("no-new-privileges", self.container("{{.HostConfig.SecurityOpt}}"))
        self.assertEqual(self.container('{{index .Config.Labels "com.docker.compose.project.working_dir"}}'), str(self.dir.resolve()))

        # networking: it listens on nothing and publishes nothing - no new listening port anywhere on the host
        self.assertEqual(self.container("{{json .HostConfig.PortBindings}}"), "{}")
        self.assertEqual(docker("port", "bwwatch").stdout.strip(), "")
        self.assertEqual(listening_ports() - listening_before, set(), "no new listening socket on the server")
        if NETWORK == "bridge":
            self.assertEqual(self.container("{{.HostConfig.NetworkMode}}"), "bwwatch_default")
            made = docker_objects()["networks"] - self.docker_before["networks"]
            self.assertEqual(made, {"bwwatch_default"}, "the one private network it needs, and nothing else")

        # what the wizard chose is what the installer put in place: private, complete, with the old file kept
        env_path = self.dir / ".env"
        self.assertEqual(stat.S_IMODE(os.stat(env_path).st_mode), 0o600)
        settings = dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines()
                        if re.match(r"^[A-Z_]+=", line))
        self.assertRegex(settings["NTFY_TOPIC"], r"^bwwatch-[0-9a-f]{20}$")
        self.assertEqual(settings["DISPLAY_TZ"], "America/Chicago")
        self.assertEqual(settings["BW_FAULT_REQUEST"], "'%s'" % FAULT_REQUEST, "quoted so Compose keeps { } & and spaces as typed")
        type(self).topic = settings["NTFY_TOPIC"]
        backups = [p for p in self.dir.iterdir() if p.name.startswith(".env.bak-")]
        self.assertEqual(len(backups), 1)
        self.assertIn("BW_HTTP_TIMEOUT_SECONDS=10", backups[0].read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(os.stat(backups[0]).st_mode), 0o600)

        # data/ belongs to the container's own user, private to it
        data = self.dir / "data"
        self.assertEqual(os.stat(data).st_uid, 10001)
        self.assertEqual(stat.S_IMODE(os.stat(data).st_mode), 0o700)

        # nothing but the documented files, and no scratch files left behind
        new = {p.name for p in self.dir.iterdir()} - self.baseline
        self.assertEqual({n for n in new if not n.startswith(".env.bak-")}, {"data", "install.log"})
        self.assertEqual(sorted(p.name for p in self.elsewhere.iterdir()), [], "the working directory stays empty")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [], "so does the temp folder")
        self.assertLessEqual({p.name for p in self.home.iterdir()}, {".docker"}, "only Docker's own bookkeeping may appear in home")
        log = (self.dir / "install.log").read_text(encoding="utf-8")
        self.assertNotIn(settings["NTFY_TOPIC"], log)
        self.assertIn("Part 4 of 4", log)

        # the awkward hand-typed value went through the wizard, the new .env and Compose's parser unchanged
        self.assertEqual(self.container("{{.Config.Env}}").count("BW_EXTRA_HEADERS="), 1)
        printed = docker("exec", "bwwatch", "printenv", "BW_EXTRA_HEADERS").stdout.rstrip("\n")
        self.assertEqual(printed, AWKWARD_JSON)
        sent = [r["headers"].get("X-Awkward") for r in self.mock.requests if r["path"].startswith("/wave/")]
        self.assertTrue(sent and all(h == "it's $HOME # not a comment" for h in sent), sent)

        # what the mock cloud saw: a real sign-in, the heater read, alerts delivered, never a change
        self.assertEqual(self.mock.writes, [], "the read-only guarantee, against the real container")
        names = {r["path"].rsplit("/", 1)[-1] for r in self.mock.requests if r["path"].startswith("/wave/")}
        self.assertEqual(names, {"getApplianceList", "getApplianceStatus", "getNotifications"})
        topics = {m["json"]["topic"] for m in self.mock.sinks["ntfy"]}
        self.assertEqual(topics, {self.topic})
        self.assertGreaterEqual(len(self.mock.sinks["ntfy"]), 2, "the wizard's test alert and the service's own")
        self.assertTrue(any("started" in m["json"]["title"] for m in self.mock.sinks["ntfy"]))

    # --- 2. checking it ------------------------------------------------------------
    def test_2_check_and_bwctl(self):
        self.need_install()
        done = self.ctl("--check", script="install.sh")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Everything checks out", done.stdout)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith(".install")), [], "--check leaves no scratch file")

        done = self.ctl("verify")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("[ OK ] Wave cloud", done.stdout)

        done = self.ctl("status")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Basement", done.stdout)

        done = self.ctl("faults")  # the "(Cleared)" fault from the screenshot: logged as history, not alarmed about
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"10\s+cleared\s+Superheat Fault")
        self.assertIn("pre-existing", done.stdout)
        self.assertIn("2026-10-04 12:15 CDT", done.stdout, "the time the app gave (17:15 UTC), in the time zone chosen in the setup")
        self.assertIn("already cleared when first seen", done.stdout)
        self.assertFalse(any("fault 10" in m["json"]["title"].lower() for m in self.mock.sinks["ntfy"]), "no alarm for old history")
        self.assertTrue(any("Superheat Fault" in m["json"]["message"] and "[cleared]" in m["json"]["message"]
                            for m in self.mock.sinks["ntfy"]), "but the first message lists it")

        done = self.ctl("export", "faults")  # piped: Docker's terminal mode would corrupt this with \r\n
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("\r", done.stdout)
        self.assertIn("code", done.stdout.splitlines()[0].lower())

        before = len(self.mock.sinks["ntfy"])
        done = self.ctl("test-notify")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(len(self.mock.sinks["ntfy"]), before + 1)

        done = self.ctl("call", "GET /wave/getApplianceStatus?macAddress={mac}")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Heat Pump", done.stdout)

        done = self.ctl("call", "GET /wave/changeSetpoint?macAddress={mac}&temperature=140")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.mock.writes, [], "a change request is refused before anything is sent")

    def test_2b_login_again_on_a_terminal(self):
        self.need_install()
        again = self.mock.issue_login_code("again-code-" + "b" * 24)
        status, text = converse(["sh", str(self.dir / "bwctl"), "login"], [(r"Paste the address here[^\n]*: ", "%s?code=%s" % (REDIRECT, again))],
                                env=self.env(), cwd=str(self.elsewhere), timeout=120, patience=60)
        self.assertEqual(status, 0, text[-2000:])
        self.assertIn("Signed in", text)
        self.assertIn("Basement", text)
        grants = [r["form"].get("grant_type") for r in self.mock.requests if r["path"] == "/auth/token"]
        self.assertGreaterEqual(grants.count("authorization_code"), 2, "the first install and this login")
        self.assertNotIn(again, text.split("Signed in")[-1], "after the terminal's own echo of what was typed, the one-time code is never printed")

    # --- 3. start, stop, restart, crash ------------------------------------------------
    def test_3_service_control_and_a_crash(self):
        self.need_install()
        self.assertEqual(self.ctl("stop").returncode, 0)
        self.assertEqual(self.container("{{.State.Running}}"), "false")
        done = self.ctl("--check", script="install.sh")
        self.assertEqual(done.returncode, 1)
        self.assertIn("not running", done.stdout)

        self.assertEqual(self.ctl("start").returncode, 0)
        self.wait_for("the container to run", lambda: self.container("{{.State.Running}}") == "true")

        before = self.container("{{.Id}}")
        self.assertEqual(self.ctl("restart").returncode, 0)
        self.assertNotEqual(self.container("{{.Id}}"), before, "restart recreates the container so .env is re-read")
        self.wait_for("the container to run", lambda: self.container("{{.State.Running}}") == "true")

        # A crash: the process is killed from outside, like an out-of-memory kill. Docker's restart policy brings it
        # back and the database is intact. (`docker kill` does not count: Docker treats that as a deliberate stop. And a
        # restart policy only applies to a container that has been up for 10 seconds.)
        if os.geteuid() == 0:
            time.sleep(12)
            started, pid = self.container("{{.State.StartedAt}}"), self.container("{{.State.Pid}}")
            os.kill(int(pid), signal.SIGKILL)
            self.wait_for("Docker to restart the crashed container", lambda: self.container("{{.State.Running}}") == "true"
                          and self.container("{{.State.StartedAt}}") != started)
            self.assertEqual(self.container("{{.RestartCount}}"), "1")
            time.sleep(3)
            done = self.ctl("dbcheck")
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        logs = self.ctl("logs")
        self.assertEqual(logs.returncode, 0)
        self.assertNotIn(self.topic, logs.stdout + logs.stderr, "secrets stay out of the logs")
        self.assertNotIn("e2e-login-code", logs.stdout + logs.stderr)

    # --- 4. a cancelled reconfigure leaves the service running ----------------------------
    def test_4_cancelling_a_reconfigure_brings_the_service_back(self):
        self.need_install()
        env_before = (self.dir / ".env").read_text(encoding="utf-8")
        status, text = self.installer("--reconfigure", script=[FOLDER, (r"Press Enter to begin ", CTRL_C)], patience=300)
        self.assertEqual(status, 130, text[-2000:])
        self.assertIn("pausing it while you change settings", text)
        self.assertIn("bwwatch is running again", text)
        self.assertEqual(self.container("{{.State.Running}}"), "true")
        self.assertEqual((self.dir / ".env").read_text(encoding="utf-8"), env_before)

    # --- 5. reconfigure, keeping everything ---------------------------------------------------
    def test_5_reconfigure_keeps_what_you_keep(self):
        self.need_install()
        before = self.container("{{.Id}}")
        answers = [
            FOLDER,
            (r"Press Enter to begin ", ""),
            (r"Keep them\? \[Y/n\] ", "y"),
            (r"Send a test alert now[^\n]*\[Y/n\] ", "y"),
            (r"Time zone \[America/Chicago\]: ", ""),
            (r"Is that right\? \[Y/n\] ", "y"),
            (r"Keep that sign-in\? \[Y/n\] ", "y"),
            (r"Is that your water heater\? \[Y/n\] ", "y"),
            (r"Keep it\? \[Y/n\] ", "y"),
            (r"Change advanced options\?[^\n]*\[y/N\] ", "n"),
            (r"Save them\? \[Y/n\] ", "y"),
        ]  # (no final question: the service sends no "started" message again within the hour)
        status, text = self.installer("--reconfigure", script=answers)
        self.assertEqual(status, 0, text[-3000:])
        self.assertIn("nothing more to confirm", text)
        self.assertNotIn("Paste the address", text, "the Wave sign-in was kept: no second sign-in")
        self.assertIn("Restarting bwwatch with the new settings", text)
        self.assertNotEqual(self.container("{{.Id}}"), before)
        self.assertEqual(self.container("{{.State.Running}}"), "true")
        settings = (self.dir / ".env").read_text(encoding="utf-8")
        self.assertIn("NTFY_TOPIC=" + self.topic, settings, "the topic your phone is subscribed to did not change")
        self.assertEqual(len([p for p in self.dir.iterdir() if p.name.startswith(".env.bak-")]), 2)
        self.assertEqual(self.mock.writes, [])

    # --- 6. uninstall leaves everything else alone ------------------------------------------------
    def test_6_uninstall_removes_only_bwwatch(self):
        self.need_install()
        self.assertEqual(docker("volume", "create", DECOY_VOLUME).returncode, 0)
        self.assertEqual(docker("create", "--name", DECOY_CONTAINER, BASE_IMAGE, "sleep", "1000").returncode, 0)

        done = self.ctl("--uninstall", script="install.sh", stdin="y\nDELETE\n")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Erased everything in data/", done.stdout)
        self.assertIn("Removed the container", done.stdout)
        self.assertIn("Removed the image bwwatch:local", done.stdout)

        self.assertNotEqual(docker("inspect", "-f", "{{.Id}}", "bwwatch").returncode, 0, "our container is gone")
        self.assertNotEqual(docker("image", "inspect", "bwwatch:local").returncode, 0, "our image is gone")
        self.assertEqual(docker("inspect", "-f", "{{.Name}}", DECOY_CONTAINER).returncode, 0, "someone else's container survives")
        self.assertEqual(docker("volume", "inspect", DECOY_VOLUME).returncode, 0, "someone else's volume survives")
        self.assertEqual(docker("image", "inspect", BASE_IMAGE).returncode, 0, "the shared base image survives")

        now = docker_objects()
        for kind in ("containers", "networks", "volumes"):
            added = now[kind] - self.docker_before[kind] - {DECOY_CONTAINER, DECOY_VOLUME}
            self.assertEqual(added, set(), "uninstall leaves no %s behind" % kind)
            self.assertEqual(self.docker_before[kind] - now[kind], set(), "and removes no one else's %s" % kind)

        self.assertEqual(list((self.dir / "data").iterdir()), [], "recorded data erased")
        remaining = {p.name for p in self.dir.iterdir()}
        self.assertFalse(any(n.startswith(".env") and n != ".env.example" for n in remaining), remaining)
        self.assertNotIn("install.log", remaining)
        for kept in ("install.sh", "bwctl", "Dockerfile", "docker-compose.yml", ".env.example", "bwwatch"):
            self.assertIn(kept, remaining, "the program files stay")
        self.assertIn("rm -rf", done.stdout, "it tells you how to remove the folder itself")


if __name__ == "__main__":
    unittest.main()
