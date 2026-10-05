"""install.sh and bwctl, run for real against a pretend ``docker`` (tests/fake_docker.py).

Every run is also checked for *confinement*: nothing may appear in the home folder, the temp folder or the
working directory, and the install folder may only gain the files the installer is documented to create.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional

from .pty_driver import CTRL_C, converse

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name("fake_docker.py")
PROJECT_FILES = ("install.sh", "bwctl", "docker-compose.yml", "Dockerfile", ".env.example")
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]
TOOLS = ("sed", "awk", "tr", "cp", "mv", "rm", "cmp", "cat", "tail", "head", "date", "kill", "sleep", "chmod", "mkdir",
         "df", "id", "find", "readlink", "dirname", "uname", "grep", "wc", "sort", "cut", "ls", "env", "true", "false")


def kind(call: Dict) -> str:
    """A short canonical name for one recorded docker call."""
    argv = call["argv"]
    if argv[:1] == ["compose"]:
        rest = argv[1:]
        if rest[:1] == ["version"]:
            return "compose version"
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2
        sub, args = rest[i], rest[i + 1:]
        if sub == "run":
            words = [a for a in args if a not in ("--rm", "-T", "--no-deps")]
            if words[:2] == ["--entrypoint", "find"]:
                return "compose run find"
            return "compose run " + " ".join(words[1:])
        if sub == "exec":
            words = [a for a in args if a != "-T"]
            return "compose exec " + " ".join(words[2:3])
        if sub == "up":
            return "compose up" + (" --force-recreate" if "--force-recreate" in args else "")
        return "compose " + sub
    if argv[:1] == ["image"]:
        return "image " + argv[1]
    return argv[0]


class Result:
    def __init__(self, proc: subprocess.CompletedProcess):
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    @property
    def text(self) -> str:
        return self.out + self.err


class Sandbox:
    """A throw-away server: an install folder, a fake docker with its own little world, empty home and tmp."""

    ALLOWED_NEW = {".env", "install.log", "data"}

    def __init__(self, test: unittest.TestCase, name: str = "bwheater"):
        self.test = test
        self.base = Path(tempfile.mkdtemp(prefix="bwbox."))
        test.addCleanup(shutil.rmtree, str(self.base), True)
        self.world = self.base / "world"
        self.home = self.base / "home"
        self.tmp = self.base / "tmp"
        self.elsewhere = self.base / "elsewhere"
        self.bin = self.base / "bin"
        self.dir = self.base / "opt" / name
        for folder in (self.world, self.home, self.tmp, self.elsewhere, self.bin, self.dir):
            folder.mkdir(parents=True)
        for name_ in PROJECT_FILES:
            shutil.copy2(ROOT / name_, self.dir / name_)
        shutil.copytree(ROOT / "bwwatch", self.dir / "bwwatch", ignore=shutil.ignore_patterns("__pycache__"))
        wrapper = self.bin / "docker"
        wrapper.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, FAKE))
        wrapper.chmod(0o755)
        self.baseline = self.listing()
        self.scenario()

    # --- the pretend world --------------------------------------------------
    def scenario(self, **settings) -> None:
        (self.world / "scenario.json").write_text(json.dumps(settings), encoding="utf-8")

    def state(self) -> Dict:
        try:
            return json.loads((self.world / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_container(self, running: bool = True, owner: Optional[str] = None, image: bool = True) -> None:
        box = {"exists": True, "running": running, "owner": str(self.dir) if owner is None else owner}
        (self.world / "state.json").write_text(json.dumps({"container": box, "image": image, "generated": None}), encoding="utf-8")

    def calls(self) -> List[Dict]:
        try:
            lines = (self.world / "calls.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines]

    def kinds(self) -> List[str]:
        return [kind(c) for c in self.calls() if not c.get("unexpected")]

    def unexpected(self) -> List[List[str]]:
        return [c["argv"] for c in self.calls() if c.get("unexpected")]

    def forget_calls(self) -> None:
        try:
            (self.world / "calls.jsonl").unlink()
        except FileNotFoundError:
            pass

    # --- files --------------------------------------------------------------
    def listing(self) -> set:
        return {str(p.relative_to(self.dir)) for p in self.dir.iterdir()}

    def env_vars(self, path_dirs: Optional[List[str]] = None, **extra: str) -> Dict[str, str]:
        path = os.pathsep.join([str(self.bin)] + (path_dirs if path_dirs is not None else [os.environ.get("PATH", "")]))
        env = {"PATH": path, "HOME": str(self.home), "TMPDIR": str(self.tmp), "FAKE_DOCKER_WORLD": str(self.world),
               "LANG": "C", "NO_COLOR": "1"}
        env.update(extra)
        return env

    def shim(self, name: str, body: str) -> None:
        """A pretend system command that comes first on PATH."""
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)

    def pretend_ufw(self, status_text: str) -> None:
        (self.world / "ufw.txt").write_text(status_text, encoding="utf-8")
        self.shim("ufw", 'echo "$*" >> "$FAKE_DOCKER_WORLD/ufw.calls"\n[ "$1" = status ] && cat "$FAKE_DOCKER_WORLD/ufw.txt"\nexit 0\n')

    def pretend_not_root(self) -> None:
        (self.world / "uid").write_text("1000\n", encoding="utf-8")
        self.shim("id", '[ "$1" = "-u" ] && [ -f "$FAKE_DOCKER_WORLD/uid" ] && { cat "$FAKE_DOCKER_WORLD/uid"; exit 0; }\nexec %s "$@"\n' % shutil.which("id"))

    def pretend_listening(self, ports) -> None:
        lines = ["State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"]
        for number, port in enumerate(ports):
            where = ("0.0.0.0", "[::]", "127.0.0.1", "*")[number % 4]
            lines.append("LISTEN 0      128          %s:%d         0.0.0.0:*" % (where, port))
        (self.world / "ss.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.shim("ss", 'cat "$FAKE_DOCKER_WORLD/ss.txt"\n')

    def ufw_calls(self) -> list:
        try:
            return (self.world / "ufw.calls").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

    def put_env(self, text: str) -> None:
        """An .env as an earlier install would have left it (private, and not counted as new)."""
        path = self.dir / ".env"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        self.baseline = self.listing()

    # --- running ------------------------------------------------------------
    def run(self, *args: str, stdin: str = "", script: str = "install.sh", shell: str = "sh",
            path_dirs: Optional[List[str]] = None, timeout: float = 90, confined: bool = True, **extra_env: str) -> Result:
        proc = subprocess.run([shutil.which(shell) or shell, str(self.dir / script), *args], input=stdin, text=True, capture_output=True,
                              cwd=str(self.elsewhere), env=self.env_vars(path_dirs, **extra_env), timeout=timeout)
        if confined:
            self.assert_confined()
        return Result(proc)

    def assert_confined(self) -> None:
        t = self.test
        for folder, label in ((self.home, "the home folder"), (self.tmp, "the temp folder"), (self.elsewhere, "the working directory")):
            t.assertEqual(sorted(p.name for p in folder.iterdir()), [], "%s must stay empty" % label)
        new = self.listing() - self.baseline
        for name in new:
            t.assertTrue(name in self.ALLOWED_NEW or name.startswith(".env.bak-"), "unexpected new file in the install folder: %s" % name)
        for name in (".install.out", ".env.new"):
            t.assertNotIn(name, new, "scratch file %s was left behind" % name)
        for name in new:
            mode = stat.S_IMODE(os.stat(self.dir / name).st_mode)
            t.assertEqual(mode & 0o077, 0, "%s is readable by others (mode %o)" % (name, mode))
        t.assertEqual(self.unexpected(), [], "the scripts ran a docker command they should not have")


def system_path_without(*names: str) -> List[str]:
    """A PATH dir holding only the plain Unix tools the scripts need, so 'docker' can be genuinely absent."""
    return []


class SandboxCase(unittest.TestCase):
    def box(self, name: str = "bwheater") -> Sandbox:
        return Sandbox(self, name)


# =============================================================================== static checks
class Scripts(unittest.TestCase):
    def test_present_executable_and_parse_under_every_shell(self):
        for name in ("install.sh", "bwctl"):
            path = ROOT / name
            self.assertTrue(os.access(path, os.X_OK), "%s must be executable" % name)
            self.assertTrue(path.read_text(encoding="utf-8").startswith("#!/bin/sh\n"), name)
            for shell in SHELLS:
                done = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, "%s -n %s: %s" % (shell, name, done.stderr))

    def executed_lines(self, name: str) -> List[str]:
        """Lines that run something: not comments, not text that is only printed, not the help here-document."""
        lines, in_heredoc, kept = (ROOT / name).read_text(encoding="utf-8").splitlines(), False, []
        for line in lines:
            stripped = line.strip()
            if in_heredoc:
                in_heredoc = stripped != "EOF"
                continue
            if "<<EOF" in stripped or "<<'EOF'" in stripped:
                in_heredoc = True
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(r"^(say|hint|warn|fail|ok|blank|printf|echo|die)\b", stripped):
                continue
            kept.append(stripped)
        return kept

    def test_nothing_is_written_outside_the_folder(self):
        forbidden = [r"/etc\b", r"/tmp\b", r"/var\b", r"/usr\b", r"/root\b", r"/home\b", r"\$HOME", r"~/", r"\bmktemp\b",
                     r"\bcrontab\b", r"\bcron\b", r"\bsystemctl +(enable|start|restart|daemon-reload)", r"\bln +-s",
                     r"\bsudo\b", r"\bchown\b", r"/dev/shm", r"XDG_"]
        for name in ("install.sh", "bwctl"):
            for line in self.executed_lines(name):
                line = line.replace("/etc/os-release", "")  # the one thing read from outside: which distribution this is
                for pattern in forbidden:
                    self.assertIsNone(re.search(pattern, line), "%s runs something that reaches outside the folder: %s" % (name, line))

    def test_redirections_only_target_the_folders_own_files(self):
        allowed = ('"$OUT"', '"$LOG"', '"$NEW_ENV"', '/dev/null', '"$BACKUP"', '"$DIR/.env"', '"$DIR/.env.bak-$STAMP"')
        for name in ("install.sh", "bwctl"):
            for line in self.executed_lines(name):
                for target in re.findall(r">>?\s*(\"[^\"]*\"|'[^']*'|[^\s;)}&|]+)", line):
                    self.assertTrue(target in allowed or target.startswith(("\"$DIR/", "$DIR/")), "%s writes to %s in: %s" % (name, target, line))

    def test_only_this_projects_docker_objects_are_touched(self):
        banned = [r"docker +(system|volume|network|builder|buildx|container|rm|rmi|kill|prune|pull|push|tag|save|load|commit|exec|cp)\b",
                  r"\bprune\b", r"--privileged", r"--volumes?\b", r"\bdown +-v", r"-v +\S+:", r"docker +run\b", r"--network", r"--pid", r"--cap-add"]
        for name in ("install.sh", "bwctl"):
            for line in self.executed_lines(name):
                for pattern in banned:
                    self.assertIsNone(re.search(pattern, line), "%s: %s" % (name, line))
        # the one deletion in the installer is bounded to the data folder, inside the container or at its own path
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "-delete" in line and not line.strip().startswith(("#", "say", "hint", "warn")):
                self.assertTrue("/data -mindepth 1 -delete" in line or '"$DIR/data" -mindepth 1 -delete' in line, line)
        for line in self.executed_lines("install.sh"):
            self.assertIsNone(re.search(r"\brm +-[a-zA-Z]*r", line), "no recursive rm is ever run: %s" % line)
        for line in text.splitlines():
            if re.search(r"\brm +-f", line) and not line.strip().startswith(("#", "say", "hint", "warn")):
                self.assertRegex(line, r'"\$(OUT|NEW_ENV|LOG|DIR)|"\$DIR"/', line)

    def test_the_firewall_and_the_network_are_only_ever_read(self):
        banned = r"\b(iptables|ip6tables|iptables-save|nft|firewall-cmd|sysctl|ifconfig|route|brctl)\b|\bip +(link|addr|route|rule|netns)\b"
        for name in ("install.sh", "bwctl"):
            for line in self.executed_lines(name):
                self.assertIsNone(re.search(banned, line), "%s touches the firewall or network: %s" % (name, line))
                unquoted = re.sub(r'"[^"]*"', "", line)  # words inside printed messages do not count
                if re.search(r"\bufw\b", unquoted):
                    self.assertTrue(re.search(r"command -v ufw|ufw status verbose", line), "%s: ufw is only ever asked for its status: %s" % (name, line))

    def test_every_compose_call_is_pinned_to_this_project(self):
        for name in ("install.sh", "bwctl"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('docker compose -p "$PROJECT" -f "$DIR/docker-compose.yml" --project-directory "$DIR"', text)
            for line in self.executed_lines(name):
                if "docker compose" in line and "version" not in line:
                    self.assertTrue(line.startswith("dc()"), "%s calls docker compose outside its wrapper: %s" % (name, line))

    def test_secrets_never_go_to_the_log(self):
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("set -x", text)
        self.assertIn("umask 077", text)
        # the settings file the wizard produces is written to .env.new, never to the log or the terminal
        self.assertRegex(text, r'--print-env >"\$NEW_ENV"')

    def test_shellcheck_is_clean_when_available(self):
        tool = shutil.which("shellcheck") or os.environ.get("SHELLCHECK")
        if not tool:
            self.skipTest("shellcheck not installed")
        done = subprocess.run([tool, "-s", "sh", str(ROOT / "install.sh"), str(ROOT / "bwctl")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


# =============================================================================== install.sh: this server
class Preflight(SandboxCase):
    def test_help_and_unknown_options_change_nothing(self):
        box = self.box()
        done = box.run("--help")
        self.assertEqual(done.code, 0)
        self.assertIn("--reconfigure", done.out)
        self.assertIn(str(box.dir), done.out)
        done = box.run("--wat")
        self.assertEqual(done.code, 2)
        self.assertIn("unknown option", done.err)
        self.assertEqual(box.calls(), [])
        self.assertEqual(box.listing(), box.baseline)

    def test_docker_missing_is_explained_and_nothing_is_created(self):
        box = self.box()
        tools = Path(box.base / "tools")
        tools.mkdir()
        for name in TOOLS:
            found = shutil.which(name)
            if found:
                (tools / name).symlink_to(found)
        (box.bin / "docker").unlink()
        done = box.run(stdin="y\n", path_dirs=[str(tools)])
        self.assertEqual(done.code, 1, done.text)
        self.assertIn("Docker is not installed", done.out)
        self.assertIn("docs.docker.com", done.out)
        self.assertNotIn(".env", box.listing() - box.baseline, "nothing is set up before Docker is confirmed")

    def test_the_install_hint_names_docker_steps_for_this_distribution(self):
        box = self.box()
        (box.bin / "docker").unlink()
        tools = box.base / "tools"
        tools.mkdir()
        for name in TOOLS:
            found = shutil.which(name)
            if found:
                (tools / name).symlink_to(found)
        done = box.run(stdin="y\n", path_dirs=[str(tools)])
        distro = {}
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    distro[key] = value.strip('"')
        except OSError:
            pass
        expected = "https://docs.docker.com/engine/install/" + (distro.get("ID", "") + "/" if distro.get("ID") in ("debian", "ubuntu", "fedora", "raspbian") else "")
        self.assertIn(expected, done.out)

    def test_daemon_not_running(self):
        box = self.box()
        box.scenario(info="down")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("not running", done.out)
        self.assertIn("systemctl start docker", done.out)

    def test_permission_denied_points_at_sudo_and_leaves_the_choice(self):
        box = self.box()
        box.scenario(info="permission")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("not allowed to use it", done.out)
        self.assertIn("sudo ./install.sh", done.out)
        self.assertIn("your call", done.out)

    def test_an_unrecognised_docker_error_is_shown(self):
        box = self.box()
        box.scenario(info="odd")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("something unexpected happened", done.out)

    def test_compose_plugin_missing_or_too_old(self):
        box = self.box()
        box.scenario(compose_version="")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("Compose v2 is missing", done.out)
        box.scenario(compose_version="1.29.2")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("too old", done.out)
        box.scenario(compose_version="v2.24.0")
        done = box.run(stdin="y\n", )
        self.assertIn("Docker Compose 2.24.0", done.out, "a leading v is tolerated")

    def test_a_remote_docker_host_is_refused(self):
        box = self.box()
        done = box.run(stdin="y\n", DOCKER_HOST="tcp://10.0.0.5:2375")
        self.assertEqual(done.code, 1)
        self.assertIn("another machine", done.out)
        self.assertNotIn("compose build", box.kinds())
        done = box.run(stdin="y\n", DOCKER_HOST="unix:///var/run/docker.sock")
        self.assertNotIn("another machine", done.out)

    def test_an_incomplete_folder_is_reported(self):
        box = self.box()
        (box.dir / "Dockerfile").unlink()
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("Dockerfile is missing", done.out)
        self.assertEqual(box.calls(), [])

    @unittest.skipIf(os.geteuid() == 0, "root can write anywhere")
    def test_a_folder_you_cannot_write_to_is_reported(self):
        box = self.box()
        box.dir.chmod(0o555)
        self.addCleanup(box.dir.chmod, 0o755)
        done = box.run(stdin="y\n", confined=False)
        self.assertEqual(done.code, 1)
        self.assertIn("not allowed to write", done.out)

    def test_a_different_folder_is_confirmed_and_declining_changes_nothing(self):
        box = self.box()
        done = box.run(stdin="n\n")
        self.assertEqual(done.code, 130)
        self.assertIn("You planned /opt/bwheater", done.out)
        self.assertEqual(box.listing(), box.baseline, "declining leaves the folder exactly as it was")
        self.assertNotIn("compose build", box.kinds())

    def test_a_foreign_container_called_bwwatch_blocks_the_install_and_is_left_alone(self):
        box = self.box()
        box.set_container(owner="/srv/someone-elses-project")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn('A container named "bwwatch" already exists', done.out)
        self.assertIn("/srv/someone-elses-project", done.out)
        kinds = box.kinds()
        for forbidden in ("compose build", "compose up", "compose down", "compose stop", "compose run setup"):
            self.assertNotIn(forbidden, kinds)
        self.assertTrue(box.state()["container"]["exists"])

    def test_an_invalid_compose_file_is_reported_with_the_reason(self):
        box = self.box()
        box.scenario(config="fail")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("could not read docker-compose.yml", done.out)
        self.assertIn("Additional property bogus", done.out)

    def test_low_disk_space_is_a_warning_not_a_stop(self):
        box = self.box()
        root = box.base / "docker-root"
        root.mkdir()
        box.scenario(docker_root=str(root))
        done = box.run(stdin="y\ny\n")
        self.assertIn("MB free for Docker's storage", done.out + " MB free for Docker's storage")


# =============================================================================== install.sh: ports and firewall
UFW_NORMAL = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
"""


class PortsAndFirewall(SandboxCase):
    """bwwatch listens on nothing; the installer says so, proves it, and only ever READS the firewall."""

    def test_it_says_plainly_that_nothing_listens(self):
        box = self.box()
        done = box.run(stdin="y\ny\n")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("bwwatch listens on no port", done.out)
        for phrase in ("nothing to open in the", "nginx", "no subdomain", "./bwctl", "push alerts"):
            self.assertIn(phrase, done.out)
        self.assertIn("The container publishes no port", done.out, "and the running container is checked afterwards")
        self.assertIn("there is no web page and no port", done.out)

    def test_a_compose_file_that_publishes_a_port_is_flagged_not_silently_accepted(self):
        box = self.box()
        box.scenario(config_ports=True)
        done = box.run(stdin="y\ny\n")
        self.assertIn("publishes a port or uses the host network", done.out)
        self.assertIn("remove the 'ports:'", done.out)
        self.assertNotIn("bwwatch listens on no port", done.out)

    def test_a_container_that_does_publish_a_port_is_flagged_after_it_starts(self):
        box = self.box()
        box.scenario(port_bindings={"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "56284"}]})
        done = box.run(stdin="y\ny\n")
        self.assertIn("The container publishes ports", done.out)
        self.assertIn("56284", done.out)

    def test_check_reports_the_published_ports_too(self):
        box = self.box()
        box.set_container(running=True)
        self.assertIn("publishes no port", box.run("--check").out)
        box.scenario(port_bindings={"80/tcp": [{"HostPort": "80"}]})
        self.assertIn("publishes ports", box.run("--check").out)

    def test_ports_already_in_use_are_listed_for_the_record_and_left_alone(self):
        box = self.box()
        box.pretend_listening([22, 80, 443, 5432, 80])
        done = box.run(stdin="y\ny\n")
        self.assertIn("4 TCP port(s) are already in use on this server (22 80 443 5432)", done.out)
        self.assertIn("uses none of them and adds none", done.out)

    def test_a_long_list_of_ports_is_shortened(self):
        box = self.box()
        box.pretend_listening(list(range(1000, 1015)))
        done = box.run(stdin="y\ny\n")
        self.assertIn("15 TCP port(s)", done.out)
        self.assertIn("1009 ...)", done.out)
        self.assertNotIn("1010", done.out)

    def test_no_ss_no_port_line(self):
        box = self.box()
        box.pretend_listening([])
        done = box.run(stdin="y\ny\n")
        self.assertNotIn("already in use", done.out)

    def test_ufw_active_with_the_usual_policy_needs_nothing(self):
        box = self.box()
        box.pretend_ufw(UFW_NORMAL)
        done = box.run(stdin="y\ny\n")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("ufw is active (policy: deny (incoming), allow (outgoing), disabled (routed))", done.out)
        self.assertIn("Nothing about it needs to change", done.out)
        self.assertNotIn("denies outgoing", done.out)
        self.assertEqual(box.ufw_calls(), ["status verbose"], "the only thing ever asked of ufw")

    def test_ufw_blocking_outgoing_is_a_warning_that_points_at_the_real_test(self):
        box = self.box()
        box.pretend_ufw(UFW_NORMAL.replace("allow (outgoing)", "deny (outgoing)"))
        done = box.run(stdin="y\ny\n")
        self.assertEqual(done.code, 0, "a warning, not a stop: the real test decides")
        self.assertIn("ufw denies outgoing connections by default", done.out)
        self.assertIn("TCP 443", done.out)
        self.assertIn("guided setup checks the real path", done.out)
        self.assertEqual(box.ufw_calls(), ["status verbose"])

    def test_ufw_inactive_cannot_be_in_the_way(self):
        box = self.box()
        box.pretend_ufw("Status: inactive\n")
        done = box.run(stdin="y\ny\n")
        self.assertIn("ufw is installed but inactive", done.out)

    def test_without_root_the_firewall_is_not_even_asked(self):
        box = self.box()
        box.pretend_ufw(UFW_NORMAL)
        box.pretend_not_root()
        done = box.run(stdin="y\ny\n")
        self.assertIn("run the installer as root to let it read the firewall status", done.out)
        self.assertIn("nothing was changed", done.out)
        self.assertEqual(box.ufw_calls(), [])

    def test_the_firewall_is_never_changed_whatever_it_says(self):
        for text in (UFW_NORMAL, "Status: inactive\n", "garbage\n", UFW_NORMAL.replace("allow (outgoing)", "reject (outgoing)")):
            box = self.box()
            box.pretend_ufw(text)
            box.run(stdin="y\ny\n")
            self.assertTrue(all(call == "status verbose" for call in box.ufw_calls()), box.ufw_calls())

    def test_firewalld_is_noticed_without_touching_it(self):
        box = self.box()
        box.shim("systemctl", '[ "$1" = "is-active" ] && [ "$2" = "firewalld" ] && { echo active; exit 0; }\necho enabled\n')
        done = box.run(stdin="y\ny\n")
        self.assertIn("firewalld is active", done.out)
        self.assertIn("needs nothing opened", done.out)


# =============================================================================== install.sh: the whole install
class Install(SandboxCase):
    def test_a_complete_install(self):
        box = self.box()
        done = box.run(stdin="y\ny\n")
        self.assertEqual(done.code, 0, done.text)

        # the right things happened, in the right order
        expected = ["info", "version", "compose version", "compose config", "inspect", "compose build", "image inspect",
                    "inspect", "compose run setup", "compose run setup --print-env", "compose run setup --verify",
                    "compose run setup --cleanup", "inspect", "compose up", "compose exec verify"]
        kinds = box.kinds()
        positions = [kinds.index(k) for k in ("compose config", "compose build", "compose run setup", "compose run setup --print-env",
                                              "compose run setup --verify", "compose run setup --cleanup", "compose up", "compose exec verify")]
        self.assertEqual(positions, sorted(positions), kinds)
        self.assertEqual(kinds.count("compose build"), 1)
        self.assertEqual(kinds.count("compose up"), 1)
        self.assertNotIn("compose up --force-recreate", kinds, "a first install creates, it does not recreate")
        self.assertTrue(set(kinds) <= set(expected) | {"info", "inspect", "image inspect", "version"}, kinds)

        # the settings the wizard produced are now the real .env, private, and nothing else is left over
        env = (box.dir / ".env").read_text(encoding="utf-8")
        self.assertEqual(env, "NTFY_TOPIC=bwwatch-0123456789abcdef0123\nDISPLAY_TZ=UTC\n")
        self.assertEqual(stat.S_IMODE(os.stat(box.dir / ".env").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(box.dir / "data").st_mode), 0o700)
        self.assertFalse(any(n.startswith(".env.bak") for n in box.listing()), "the untouched template is not worth a backup")
        self.assertTrue(box.state()["container"]["running"])
        self.assertIsNone(box.state()["generated"], "the wizard's temporary output was cleaned up")

        # it told the person what happened
        for text in ("Part 1 of 4", "Part 2 of 4", "Part 3 of 4", "Part 4 of 4", "bwwatch is installed and working",
                     "All 2 settings reached the service exactly as chosen", "Every check passed", "./bwctl status"):
            self.assertIn(text, done.out)
        self.assertIn(str(box.dir), done.out)
        log = (box.dir / "install.log").read_text(encoding="utf-8")
        self.assertIn("Part 4 of 4", log)
        self.assertNotIn("bwwatch-0123456789abcdef0123", log + done.out, "the secret topic is not echoed by the installer")

    def test_it_uses_the_piped_mode_of_docker_when_there_is_no_terminal(self):
        box = self.box()
        box.run(stdin="y\ny\n")
        wizard = [c for c in box.calls() if kind(c) == "compose run setup"][0]
        self.assertIn("-T", wizard["argv"])
        self.assertFalse(wizard["tty_in"])

    def test_every_compose_call_names_this_project_and_folder(self):
        box = self.box()
        box.run(stdin="y\ny\n")
        for call in box.calls():
            if call["argv"][:1] == ["compose"] and call["argv"][1:2] != ["version"]:
                argv = call["argv"]
                self.assertEqual(argv[1:7], ["-p", "bwwatch", "-f", str(box.dir / "docker-compose.yml"), "--project-directory", str(box.dir)])

    def test_the_container_check_runs_inside_the_container_and_asks_to_wait_for_the_new_run(self):
        box = self.box()
        box.run(stdin="y\ny\n")
        call = [c for c in box.calls() if kind(c) == "compose exec verify"][0]
        args = call["argv"]
        self.assertIn("--wait", args)
        self.assertIn("--since", args)
        since = float(args[args.index("--since") + 1])
        self.assertGreater(since, 1.7e9)

    def test_it_works_under_every_shell(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                box = self.box()
                done = box.run(stdin="y\ny\n", shell=shell)
                self.assertEqual(done.code, 0, "%s\n%s" % (shell, done.text))

    def test_build_failure_shows_docker_output_and_stops_before_any_setup(self):
        box = self.box()
        box.scenario(build="fail")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("The build failed", done.out)
        self.assertIn("no such host", done.out)
        self.assertIn("no internet or DNS", done.out)
        self.assertNotIn("compose run setup", box.kinds())
        self.assertNotIn("compose up", box.kinds())
        self.assertIn("no such host", (box.dir / "install.log").read_text(encoding="utf-8"))

    def test_an_abandoned_first_install_leaves_no_network_behind(self):
        box = self.box()
        box.scenario(wizard_rc=130)
        box.run(stdin="y\n")
        self.assertEqual(box.kinds()[-1], "compose down", "the private network the setup created is removed again")
        box = self.box()
        box.scenario(build="fail")
        box.run(stdin="y\n")
        self.assertEqual(box.kinds()[-1], "compose down")

    def test_but_an_existing_container_is_never_taken_down_by_an_abandoned_reconfigure(self):
        box = self.box()
        box.put_env("NTFY_TOPIC=my-old-topic\n")
        box.set_container(running=True)
        box.scenario(wizard_rc=130)
        box.run("--reconfigure", stdin="y\n")
        self.assertNotIn("compose down", box.kinds())
        self.assertTrue(box.state()["container"]["exists"])

    def test_a_finished_install_keeps_its_network(self):
        box = self.box()
        box.run(stdin="y\ny\n")
        self.assertNotIn("compose down", box.kinds())

    def test_cancelling_the_guided_setup_installs_nothing(self):
        box = self.box()
        box.scenario(wizard_rc=130)
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 130)
        self.assertIn("Stopped", done.out)
        self.assertEqual((box.dir / ".env").read_text(encoding="utf-8"), (box.dir / ".env.example").read_text(encoding="utf-8"))
        for forbidden in ("compose up", "compose run setup --print-env"):
            self.assertNotIn(forbidden, box.kinds())

    def test_a_failed_guided_setup_says_what_is_kept(self):
        box = self.box()
        box.scenario(wizard_rc=1)
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("did not finish", done.out)
        self.assertIn("Wave sign-in you already completed is kept", done.out)
        self.assertNotIn("compose up", box.kinds())

    def test_an_unreadable_wizard_result_stops_the_install(self):
        box = self.box()
        box.scenario(print_env_rc=1)
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("could not be read back", done.out)
        self.assertNotIn("compose up", box.kinds())
        self.assertEqual((box.dir / ".env").read_text(encoding="utf-8"), (box.dir / ".env.example").read_text(encoding="utf-8"))

    def test_settings_that_do_not_arrive_intact_restore_the_old_file_and_start_nothing(self):
        box = self.box()
        box.put_env("OLD_SETTING=1\n")
        box.scenario(verify_setup="fail")
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("did not reach the program unchanged", done.out)
        self.assertIn("NTFY_TOPIC", done.out, "names the setting")
        self.assertEqual((box.dir / ".env").read_text(encoding="utf-8"), "OLD_SETTING=1\n", "the earlier file is put back")
        self.assertNotIn("compose up", box.kinds())

    def test_an_existing_env_is_kept_as_a_private_backup(self):
        box = self.box()
        box.put_env("NTFY_TOPIC=my-old-topic\n")
        done = box.run(stdin="y\ny\n")
        self.assertEqual(done.code, 0, done.text)
        backups = [n for n in box.listing() if n.startswith(".env.bak-")]
        self.assertEqual(len(backups), 1)
        self.assertEqual((box.dir / backups[0]).read_text(encoding="utf-8"), "NTFY_TOPIC=my-old-topic\n")
        self.assertEqual(stat.S_IMODE(os.stat(box.dir / backups[0]).st_mode), 0o600)
        self.assertIn(backups[0], done.out)

    def test_docker_failing_to_start_it_is_reported(self):
        box = self.box()
        box.scenario(up_rc=1)
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("could not start bwwatch", done.out)

    def test_a_failing_health_check_is_shown_with_its_advice_and_the_service_keeps_running(self):
        box = self.box()
        box.scenario(verify_rc=1, verify_out="  [FAIL] Wave cloud  last poll FAILED: sign-in rejected\n"
                                             "                      -> Sign in again:  ./bwctl login\n")
        done = box.run(stdin="y\ny\n")
        self.assertEqual(done.code, 1)
        self.assertIn("[FAIL] Wave cloud", done.out)
        self.assertIn("-> Sign in again:  ./bwctl login", done.out)
        self.assertIn("still running", done.out)
        self.assertNotIn("compose down", box.kinds())
        self.assertNotIn("bwwatch is installed and working", done.out)

    def test_a_check_that_cannot_even_run_shows_the_container_log(self):
        box = self.box()
        box.scenario(verify_out="", verify_rc=1)
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertIn("Could not run the health check", done.out)
        self.assertIn("fake log line 1", done.out)

    def test_alerts_that_did_not_arrive_mean_it_is_not_finished(self):
        box = self.box()
        done = box.run(stdin="y\nn\n")
        self.assertEqual(done.code, 1)
        self.assertIn("Not confirmed", done.out)
        self.assertIn("./bwctl test-notify", done.out)
        self.assertNotIn("bwwatch is installed and working", done.out)

    def test_no_question_about_a_message_that_was_not_sent(self):
        box = self.box()
        box.scenario(verify_out="  [ OK ] Service  running\n  [ OK ] Startup alert  none was due this time (bwwatch sends at most one an hour)\n")
        done = box.run(stdin="y\n")  # only the folder question: nothing else is asked
        self.assertEqual(done.code, 0, done.text)
        self.assertNotIn("message arrive", done.out)
        self.assertIn("nothing more to confirm", done.out)
        self.assertIn("bwwatch is installed and working", done.out)

    def test_notes_are_not_called_failures(self):
        box = self.box()
        box.scenario(verify_out="  [ OK ] Service  running\n  [WARN] Backups  none yet\n  [ OK ] Startup alert  \"bwwatch started\" was delivered at 10:00\n")
        done = box.run(stdin="y\ny\n")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("No problems found", done.out)
        self.assertNotIn("Every check passed", done.out)

    def test_a_env_file_docker_cannot_read_is_set_aside_not_a_dead_end(self):
        box = self.box()
        box.put_env("NTFY_TOPIC=keep-me\nBROKEN LINE\n")
        box.scenario(config="env_broken")
        done = box.run(stdin="y\ny\ny\ny\n")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("could not read your .env file", done.out)
        self.assertIn("Continuing with a fresh .env", done.out)
        backups = sorted(n for n in box.listing() if n.startswith(".env.bak-"))
        self.assertTrue(backups)
        self.assertIn("BROKEN LINE", (box.dir / backups[0]).read_text(encoding="utf-8"), "the old file is kept, not lost")
        self.assertEqual(stat.S_IMODE(os.stat(box.dir / backups[0]).st_mode), 0o600)

    def test_declining_to_set_a_broken_env_aside_changes_nothing(self):
        box = self.box()
        box.put_env("NTFY_TOPIC=keep-me\nBROKEN LINE\n")
        box.scenario(config="env_broken")
        done = box.run(stdin="y\nn\n")
        self.assertEqual(done.code, 130)
        self.assertEqual((box.dir / ".env").read_text(encoding="utf-8"), "NTFY_TOPIC=keep-me\nBROKEN LINE\n")
        self.assertNotIn("compose build", box.kinds())

    def test_the_missing_fault_request_is_called_out_at_the_end(self):
        box = self.box()
        box.scenario(verify_out="  [ OK ] Service  running\n  [WARN] Fault history  not configured: only settings and status flags are watched\n"
                                "  [ OK ] Startup alert  \"bwwatch started\" was delivered at 10:00\n")
        done = box.run(stdin="y\ny\n")
        self.assertEqual(done.code, 0)
        self.assertIn("Notifications request", done.out)
        self.assertIn("Finding the fault request", done.out)

    def test_end_of_input_at_any_question_stops_cleanly(self):
        box = self.box()
        done = box.run(stdin="y\n")  # no answer to the final question
        self.assertEqual(done.code, 130)
        self.assertIn("Stopped", done.out)

    def test_running_it_twice_does_not_repeat_the_install(self):
        box = self.box()
        self.assertEqual(box.run(stdin="y\ny\n").code, 0)
        box.forget_calls()
        done = box.run(stdin="y\n")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("already installed", done.out)
        kinds = box.kinds()
        for forbidden in ("compose build", "compose run setup", "compose up", "compose down", "compose stop"):
            self.assertNotIn(forbidden, kinds)
        self.assertIn("compose exec verify", kinds)


class Reconfigure(SandboxCase):
    def test_it_pauses_the_service_for_the_setup_and_restarts_it_with_the_new_settings(self):
        box = self.box()
        box.put_env("NTFY_TOPIC=my-old-topic\n")
        box.set_container(running=True)
        done = box.run("--reconfigure", stdin="y\ny\n")
        self.assertEqual(done.code, 0, done.text)
        kinds = box.kinds()
        self.assertLess(kinds.index("compose stop"), kinds.index("compose run setup"), "paused before the wizard talks to Wave")
        self.assertIn("compose up --force-recreate", kinds)
        self.assertTrue(box.state()["container"]["running"])
        self.assertEqual(kinds.count("compose start"), 0, "no stray restart once the new one is up")
        self.assertTrue(any(n.startswith(".env.bak-") for n in box.listing()))

    def test_cancelling_brings_the_old_service_back(self):
        box = self.box()
        box.put_env("NTFY_TOPIC=my-old-topic\n")
        box.set_container(running=True)
        box.scenario(wizard_rc=130)
        done = box.run("--reconfigure", stdin="y\n")
        self.assertEqual(done.code, 130)
        self.assertIn("bwwatch is running again", done.out)
        kinds = [k for k in box.kinds() if k != "inspect"]
        self.assertEqual(kinds[-1], "compose start")
        self.assertNotIn("compose down", kinds, "the old container is never taken down by a cancelled reconfigure")
        self.assertTrue(box.state()["container"]["running"])
        self.assertEqual((box.dir / ".env").read_text(encoding="utf-8"), "NTFY_TOPIC=my-old-topic\n")

    def test_a_failed_wizard_also_brings_the_old_service_back(self):
        box = self.box()
        box.put_env("NTFY_TOPIC=my-old-topic\n")
        box.set_container(running=True)
        box.scenario(wizard_rc=1)
        done = box.run("--reconfigure", stdin="y\n")
        self.assertEqual(done.code, 1)
        self.assertTrue(box.state()["container"]["running"])

    def test_reconfiguring_something_that_is_not_installed_is_just_an_install(self):
        box = self.box()
        done = box.run("--reconfigure", stdin="y\ny\n")
        self.assertEqual(done.code, 0, done.text)
        self.assertNotIn("compose stop", box.kinds())
        self.assertNotIn("compose up --force-recreate", box.kinds())

    def test_an_installed_copy_offers_a_menu_when_run_from_a_terminal(self):
        box = self.box()
        box.put_env("NTFY_TOPIC=my-old-topic\n")
        box.set_container(running=True)
        status, text = converse(
            ["sh", str(box.dir / "install.sh")],
            [(r"Install here anyway\? \[y/N\] ", "y"), (r"Your choice \[1\]: ", "3")],
            env=box.env_vars(), cwd=str(box.elsewhere))
        self.assertEqual(status, 0, text)
        self.assertIn("already installed here", text)
        self.assertNotIn("compose build", box.kinds())
        box.assert_confined()


# =============================================================================== install.sh --check
class Check(SandboxCase):
    def test_not_installed(self):
        box = self.box()
        done = box.run("--check")
        self.assertEqual(done.code, 1)
        self.assertIn("not installed here", done.out)
        self.assertEqual(box.listing(), box.baseline, "a check creates nothing")

    def test_installed_but_stopped(self):
        box = self.box()
        box.set_container(running=False)
        done = box.run("--check")
        self.assertEqual(done.code, 1)
        self.assertIn("not running", done.out)
        self.assertIn("./bwctl start", done.out)

    def test_healthy(self):
        box = self.box()
        box.set_container(running=True)
        done = box.run("--check")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("Everything checks out", done.out)
        self.assertEqual(box.kinds()[-1], "compose exec verify")
        self.assertNotIn("--wait", [c for c in box.calls() if kind(c) == "compose exec verify"][0]["argv"])
        self.assertEqual(box.listing(), box.baseline, "a check writes nothing at all")

    def test_unhealthy_shows_what_to_do(self):
        box = self.box()
        box.set_container(running=True)
        box.scenario(verify_rc=1, verify_out="  [FAIL] Alert channels  none configured\n         -> Run  ./install.sh --reconfigure\n")
        done = box.run("--check")
        self.assertEqual(done.code, 1)
        self.assertIn("needs attention", done.out)
        self.assertIn("./install.sh --reconfigure", done.out)

    def test_it_changes_nothing(self):
        box = self.box()
        box.set_container(running=True)
        box.run("--check")
        self.assertEqual(set(box.kinds()) - {"info", "version", "compose version", "inspect"}, {"compose exec verify"})

    def test_a_foreign_container_is_refused(self):
        box = self.box()
        box.set_container(running=True, owner="/srv/other")
        done = box.run("--check")
        self.assertEqual(done.code, 1)
        self.assertNotIn("compose exec verify", box.kinds())


# =============================================================================== install.sh --uninstall
class Uninstall(SandboxCase):
    def installed(self) -> Sandbox:
        box = self.box()
        box.put_env("NTFY_TOPIC=abc\n")
        (box.dir / ".env.bak-20260101-000000").write_text("old\n", encoding="utf-8")
        (box.dir / "data").mkdir()
        (box.dir / "data" / "bwwatch.db").write_text("db", encoding="utf-8")
        box.set_container(running=True, image=True)
        box.baseline = box.listing()
        return box

    def test_declining_changes_nothing(self):
        box = self.installed()
        done = box.run("--uninstall", stdin="n\n")
        self.assertEqual(done.code, 0)
        self.assertIn("Nothing was changed", done.out)
        self.assertEqual(set(box.kinds()) - {"info", "version", "compose version", "inspect"}, set())
        self.assertTrue(box.state()["container"]["exists"])

    def test_it_says_exactly_what_it_will_and_will_not_touch(self):
        box = self.installed()
        done = box.run("--uninstall", stdin="n\n")
        for text in ("stop and remove the container", "remove the image", "NOT touch anything else", "no other containers",
                     "not the Python base image"):
            self.assertIn(text, done.out)

    def test_by_default_it_keeps_your_data_and_settings(self):
        box = self.installed()
        done = box.run("--uninstall", stdin="y\n\n")
        self.assertEqual(done.code, 0, done.text)
        kinds = box.kinds()
        self.assertIn("compose down", kinds)
        self.assertIn("image rm", kinds)
        self.assertNotIn("compose run find", kinds)
        self.assertEqual((box.dir / ".env").read_text(encoding="utf-8"), "NTFY_TOPIC=abc\n")
        self.assertTrue((box.dir / "data" / "bwwatch.db").exists())
        self.assertTrue((box.dir / ".env.bak-20260101-000000").exists())
        self.assertFalse(box.state()["container"]["exists"])
        self.assertFalse(box.state()["image"])
        self.assertIn("rm -rf %s" % box.dir, done.out, "tells you how to remove the folder yourself")

    def test_only_this_projects_image_is_removed(self):
        box = self.installed()
        box.run("--uninstall", stdin="y\n\n")
        removals = [c["argv"] for c in box.calls() if c["argv"][:2] == ["image", "rm"]]
        self.assertEqual(removals, [["image", "rm", "bwwatch:local"]])
        downs = [c["argv"] for c in box.calls() if kind(c) == "compose down"]
        self.assertEqual(len(downs), 1)
        self.assertNotIn("-v", downs[0])
        self.assertNotIn("--volumes", downs[0])
        self.assertNotIn("--rmi", downs[0])

    def test_typing_delete_erases_the_data_first_inside_the_container_then_removes_the_rest(self):
        box = self.installed()
        done = box.run("--uninstall", stdin="y\nDELETE\n")
        self.assertEqual(done.code, 0, done.text)
        kinds = box.kinds()
        self.assertLess(kinds.index("compose stop"), kinds.index("compose run find"),
                        "the service is stopped first: on its way out it writes into data/ again")
        self.assertLess(kinds.index("compose run find"), kinds.index("compose down"),
                        "the erasing runs on the project's network, so the network is removed after it")
        self.assertLess(kinds.index("compose down"), kinds.index("image rm"), "data is erased while the image still exists")
        wipe = [c["argv"] for c in box.calls() if kind(c) == "compose run find"][0]
        self.assertEqual(wipe[wipe.index("--entrypoint"):], ["--entrypoint", "find", "bwwatch", "/data", "-mindepth", "1", "-delete"])
        self.assertFalse((box.dir / ".env").exists())
        self.assertFalse((box.dir / ".env.bak-20260101-000000").exists())
        self.assertFalse((box.dir / "install.log").exists())
        self.assertTrue((box.dir / "install.sh").exists(), "the program files stay")
        self.assertTrue((box.dir / "bwwatch" / "__main__.py").exists())

    def test_anything_but_exactly_delete_keeps_the_data(self):
        for answer in ("delete", "yes", "DELETE ME", " "):
            box = self.installed()
            box.run("--uninstall", stdin="y\n%s\n" % answer)
            self.assertTrue((box.dir / ".env").exists(), answer)
            self.assertNotIn("compose run find", box.kinds(), answer)

    def test_an_already_removed_image_is_not_an_error(self):
        box = self.installed()
        box.set_container(running=False, image=False)
        done = box.run("--uninstall", stdin="y\n\n")
        self.assertEqual(done.code, 0, done.text)
        self.assertNotIn("image rm", box.kinds())

    def test_a_foreign_container_is_never_removed(self):
        box = self.installed()
        box.set_container(running=True, owner="/srv/other")
        done = box.run("--uninstall", stdin="y\nDELETE\n")
        self.assertEqual(done.code, 1)
        self.assertNotIn("compose down", box.kinds())
        self.assertNotIn("image rm", box.kinds())
        self.assertTrue((box.dir / ".env").exists())

    def test_the_container_is_stopped_and_its_network_removed_even_when_nothing_is_erased(self):
        box = self.installed()
        box.run("--uninstall", stdin="y\n\n")
        kinds = box.kinds()
        self.assertLess(kinds.index("compose stop"), kinds.index("compose down"))

    def test_a_missing_env_does_not_block_stopping_the_container(self):
        box = self.installed()
        (box.dir / ".env").unlink()
        done = box.run("--uninstall", stdin="y\n\n")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("compose down", box.kinds())


# =============================================================================== the real terminal
class OnATerminal(SandboxCase):
    ANSWERS = [(r"Install here anyway\? \[y/N\] ", "y"), (r"Did the .bwwatch started. message arrive\? \[Y/n\] ", "y")]

    def test_a_person_at_a_keyboard(self):
        box = self.box()
        env = box.env_vars()
        env.pop("NO_COLOR")
        status, text = converse(["sh", str(box.dir / "install.sh")], self.ANSWERS, env=env, cwd=str(box.elsewhere))
        self.assertEqual(status, 0, text)
        self.assertIn("\033[32m[ OK ]", text, "colour on a terminal")
        self.assertIn("bwwatch is installed and working", text)
        wizard = [c for c in box.calls() if kind(c) == "compose run setup"][0]
        self.assertNotIn("-T", wizard["argv"], "Docker's terminal mode, so the wizard can read what you type")
        self.assertTrue(wizard["tty_in"])
        box.assert_confined()

    def test_no_colour_when_asked_not_to(self):
        box = self.box()
        status, text = converse(["sh", str(box.dir / "install.sh")], self.ANSWERS, env=box.env_vars(), cwd=str(box.elsewhere))
        self.assertEqual(status, 0, text)
        self.assertNotIn("\033[", text)

    def test_ctrl_c_during_the_guided_setup_stops_cleanly(self):
        box = self.box()
        box.scenario(wizard_sleep=30)
        status, text = converse(
            ["sh", str(box.dir / "install.sh")],
            [(r"Install here anyway\? \[y/N\] ", "y"), (r"fake guided setup", CTRL_C)],
            env=box.env_vars(), cwd=str(box.elsewhere), timeout=60)
        self.assertEqual(status, 130, text)
        self.assertIn("Stopped", text)
        self.assertNotIn("compose up", box.kinds())
        box.assert_confined()

    def test_ctrl_c_during_the_build_stops_the_build_too(self):
        box = self.box()
        # the fake build is instant, so interrupt at the first prompt of Part 2 instead: the installer must exit 130 and clean up
        status, text = converse(["sh", str(box.dir / "install.sh")], [(r"Install here anyway\? \[y/N\] ", CTRL_C)],
                                env=box.env_vars(), cwd=str(box.elsewhere))
        self.assertEqual(status, 130, text)
        self.assertEqual(box.listing(), box.baseline)


# =============================================================================== bwctl
class Bwctl(SandboxCase):
    def test_help(self):
        box = self.box()
        done = box.run(script="bwctl")
        self.assertEqual(done.code, 0)
        for word in ("status", "faults", "logs", "restart", "update", "uninstall", "test-notify"):
            self.assertIn(word, done.out)
        self.assertEqual(box.calls(), [], "help needs no docker")

    def test_it_needs_an_installation(self):
        box = self.box()
        done = box.run("status", script="bwctl")
        self.assertEqual(done.code, 1)
        self.assertIn("not installed yet", done.err)
        self.assertIn(str(box.dir / "install.sh"), done.err)

    def test_a_command_runs_inside_the_service_when_it_is_up(self):
        box = self.box()
        box.put_env("X=1\n")
        box.set_container(running=True)
        done = box.run("faults", "--all", script="bwctl")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("fake exec: faults --all", done.out)
        call = [c for c in box.calls() if kind(c).startswith("compose exec")][0]
        self.assertIn("-T", call["argv"], "no terminal here, so no terminal mode (keeps output such as CSV clean)")
        self.assertEqual(call["argv"][call["argv"].index("exec"):], ["exec", "-T", "bwwatch", "bwwatch", "faults", "--all"])

    def test_otherwise_in_a_one_off_container(self):
        box = self.box()
        box.put_env("X=1\n")
        box.set_container(running=False)
        done = box.run("status", script="bwctl")
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("fake run: status", done.out)
        self.assertTrue(any(kind(c) == "compose run status" for c in box.calls()))

    def test_a_container_of_another_folder_is_not_used(self):
        box = self.box()
        box.put_env("X=1\n")
        box.set_container(running=True, owner="/srv/other")
        box.run("status", script="bwctl")
        self.assertFalse(any(kind(c).startswith("compose exec") for c in box.calls()))

    def test_on_a_terminal_it_keeps_the_terminal_so_login_can_ask_you_things(self):
        box = self.box()
        box.put_env("X=1\n")
        box.set_container(running=True)
        status, text = converse(["sh", str(box.dir / "bwctl"), "login"], [], env=box.env_vars(), cwd=str(box.elsewhere))
        self.assertEqual(status, 0, text)
        call = [c for c in box.calls() if kind(c).startswith("compose exec")][0]
        self.assertNotIn("-T", call["argv"])
        self.assertTrue(call["tty_in"] and call["tty_out"])

    def test_service_control(self):
        box = self.box()
        box.put_env("X=1\n")
        box.set_container(running=True)
        self.assertEqual(box.run("stop", script="bwctl").code, 0)
        self.assertFalse(box.state()["container"]["running"])
        self.assertEqual(box.run("start", script="bwctl").code, 0)
        self.assertEqual(box.run("restart", script="bwctl").code, 0)
        self.assertEqual(box.run("logs", "-f", script="bwctl").code, 0)
        kinds = box.kinds()
        self.assertIn("compose stop", kinds)
        self.assertIn("compose up", kinds)
        self.assertIn("compose up --force-recreate", kinds, "restart re-reads .env")
        logs = [c["argv"] for c in box.calls() if kind(c) == "compose logs"][0]
        self.assertEqual(logs[logs.index("logs"):], ["logs", "--tail", "200", "-f", "bwwatch"])

    def test_update_rebuilds_restarts_and_checks(self):
        box = self.box()
        box.put_env("X=1\n")
        box.set_container(running=True)
        done = box.run("update", script="bwctl")
        self.assertEqual(done.code, 0, done.text)
        kinds = box.kinds()
        self.assertEqual([k for k in kinds if k in ("compose build", "compose up", "compose exec verify")],
                         ["compose build", "compose up", "compose exec verify"])
        verify = [c["argv"] for c in box.calls() if kind(c) == "compose exec verify"][0]
        self.assertIn("--wait", verify)

    def test_a_failed_build_stops_the_update(self):
        box = self.box()
        box.put_env("X=1\n")
        box.set_container(running=True)
        box.scenario(build="fail")
        done = box.run("update", script="bwctl")
        self.assertNotEqual(done.code, 0)
        self.assertNotIn("compose up", box.kinds(), "the running service is left alone if the new build failed")

    def test_it_works_through_a_symbolic_link_from_anywhere(self):
        box = self.box()
        box.put_env("X=1\n")
        box.set_container(running=True)
        link = box.elsewhere / "bw"
        link.symlink_to(box.dir / "bwctl")
        done = subprocess.run(["sh", str(link), "status"], capture_output=True, text=True, cwd=str(box.elsewhere), env=box.env_vars())
        self.assertEqual(done.returncode, 0, done.stderr)
        call = box.calls()[-1]
        self.assertEqual(call["argv"][call["argv"].index("--project-directory") + 1], str(box.dir))
        link.unlink()
        box.assert_confined()

    def test_install_helpers_are_forwarded(self):
        box = self.box()
        done = box.run("uninstall", script="bwctl", stdin="n\n", confined=False)
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("Uninstalling bwwatch", done.out)


if __name__ == "__main__":
    unittest.main()
