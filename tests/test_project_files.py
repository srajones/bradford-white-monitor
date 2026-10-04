"""The files around the code - env template, compose file, Dockerfile, README - stay correct and in sync."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from bwwatch import cli
from bwwatch.config import Config

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

try:
    import yaml  # PyYAML: optional, only needed for the compose checks
except ImportError:  # pragma: no cover
    yaml = None


def env_assignments(text: str, commented: bool):
    """(NAME, value) for active lines, or for commented-out `# NAME=value` lines."""
    pattern = r"^#\s?([A-Z][A-Z0-9_]+)=(.*)$" if commented else r"^([A-Z][A-Z0-9_]+)=(.*)$"
    return re.findall(pattern, text, flags=re.M)


class EnvTemplate(unittest.TestCase):
    def code_variables(self):
        names = set()
        for module in ("config.py", "cli.py", "service.py"):
            source = (ROOT / "bwwatch" / module).read_text(encoding="utf-8")
            names |= set(re.findall(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"', source))
        # DATA_DIR is pinned to /data by docker-compose.yml on purpose, so the template does not offer it.
        names -= {"DEFAULT_API_BASE", "DATA_DIR"}
        return names

    def test_every_setting_the_code_reads_is_documented(self):
        documented = {n for n, _ in env_assignments(ENV_EXAMPLE, False)} | {n for n, _ in env_assignments(ENV_EXAMPLE, True)}
        missing = sorted(self.code_variables() - documented)
        self.assertEqual(missing, [], "add these to .env.example")

    def test_nothing_documented_that_the_code_ignores(self):
        documented = {n for n, _ in env_assignments(ENV_EXAMPLE, False)} | {n for n, _ in env_assignments(ENV_EXAMPLE, True)}
        stale = sorted(n for n in documented - self.code_variables() if not n.startswith("BW_FAULT_REQUEST"))
        self.assertEqual(stale, [], "documented in .env.example but never read")

    def test_no_secrets_and_no_trailing_comments(self):
        for name, value in env_assignments(ENV_EXAMPLE, False):
            self.assertIn(value, ("", "3600", "UTC"), "%s must ship blank (found %r)" % (name, value))
        for name, value in env_assignments(ENV_EXAMPLE, False) + env_assignments(ENV_EXAMPLE, True):
            self.assertNotRegex(value, r"\s#", "%s has a trailing comment; some env-file parsers would keep it in the value" % name)

    def test_template_as_shipped_gives_the_documented_defaults(self):
        env = dict(env_assignments(ENV_EXAMPLE, False))
        cfg = Config.from_env(env)
        self.assertEqual((cfg.interval, cfg.channel_names, cfg.fault_request), (3600, (), None))
        self.assertIsNotNone(cfg.status_request)
        self.assertEqual(cfg.display_tz, "UTC")

    def test_every_commented_example_is_valid(self):
        companions = {
            "NTFY_": {"NTFY_TOPIC": "a-long-enough-topic-1"},
            "TELEGRAM_": {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"},
            "SMTP_": {"SMTP_HOST": "mail.example", "SMTP_TO": "a@example.com"},
            "WEBHOOK_": {"WEBHOOK_URL": "https://hook.example/x"},
            "HA_": {"HA_WEBHOOK_URL": "https://ha.example/api/webhook/x"},
        }
        checked = 0
        for name, value in env_assignments(ENV_EXAMPLE, True):
            if not value or "<" in value or name in ("LOG_LEVEL",):
                continue
            env = {name: value}
            for prefix, extra in companions.items():
                if name.startswith(prefix):
                    env.update(extra)
            if name == "SMTP_TO":
                env["SMTP_HOST"] = "mail.example"
            Config.from_env(env)  # must not raise
            checked += 1
        self.assertGreater(checked, 30)

    def test_the_documented_fault_request_examples_parse(self):
        for example in re.findall(r"^#\s+BW_FAULT_REQUEST=(.*)$", ENV_EXAMPLE, flags=re.M):
            Config.from_env({"BW_FAULT_REQUEST": example})


class Readme(unittest.TestCase):
    def test_every_command_is_documented(self):
        for command in cli.COMMANDS:
            if command == "run":
                continue
            self.assertIn("`%s" % command, README, "README does not mention the %r command" % command)

    def test_no_secrets_or_session_details_in_the_docs(self):
        for text in (README, ENV_EXAMPLE):
            self.assertNotRegex(text, r"eyJ[\w-]{20,}")
            self.assertNotRegex(text, r"(?i)claude\.ai/code")

    def test_documented_numbers_match_the_code(self):
        from bwwatch.config import MIN_POLL_SECONDS
        from bwwatch.service import AUTH_RETRY_SECONDS, BACKOFF_AFTER_FAILURES

        self.assertEqual(MIN_POLL_SECONDS, 300)
        self.assertIn("every 5 minutes", README)
        self.assertEqual(AUTH_RETRY_SECONDS, 6 * 3600)
        self.assertIn("every 6 hours", README)
        self.assertEqual(BACKOFF_AFTER_FAILURES, 3)
        self.assertEqual(len(cli.PROBE_NAMES) + 2, 14)
        self.assertIn("about 14", README)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ComposeFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        cls.svc = cls.compose["services"]["bwwatch"]

    def test_locked_down(self):
        svc = self.svc
        self.assertTrue(svc["read_only"])
        self.assertEqual(svc["cap_drop"], ["ALL"])
        self.assertLessEqual(set(svc["cap_add"]), {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"})
        self.assertIn("no-new-privileges:true", svc["security_opt"])
        for forbidden in ("privileged", "ports", "network_mode", "pid", "ipc", "devices", "cap_add_all"):
            self.assertNotIn(forbidden, svc, forbidden)
        self.assertNotIn("user", svc, "starts as root only to hand over ./data, then drops privileges itself")

    def test_a_separate_init_would_break_graceful_stop(self):
        # tini (docker's `init: true`) runs as root and must signal the unprivileged service; with all
        # capabilities dropped it needs CAP_KILL or `docker stop` degrades into a hard kill. Found in testing.
        if self.svc.get("init"):
            self.assertIn("KILL", self.svc["cap_add"])

    def test_restart_health_and_data(self):
        svc = self.svc
        self.assertEqual(svc["restart"], "unless-stopped")
        self.assertEqual(svc["env_file"], ".env")
        self.assertIn("./data:/data", svc["volumes"], "a host folder, so `down -v` cannot remove the data")
        self.assertEqual(svc["environment"]["DATA_DIR"], "/data")
        self.assertEqual(svc["healthcheck"]["test"], ["CMD", "bwwatch", "healthcheck"])
        self.assertRegex(svc["stop_grace_period"], r"^(\d+)s$")
        self.assertGreaterEqual(int(svc["stop_grace_period"][:-1]), 30)
        self.assertIn("/tmp", " ".join(svc["tmpfs"]))
        self.assertEqual(self.compose["name"], "bwwatch")

    def test_no_named_volumes_that_down_v_would_delete(self):
        self.assertNotIn("volumes", self.compose)


class Dockerfile(unittest.TestCase):
    TEXT = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_nothing_is_downloaded_or_installed(self):
        for forbidden in ("pip install", "apt-get", "apk add", "curl ", "wget ", "npm "):
            self.assertNotIn(forbidden, self.TEXT)

    def test_pinned_base_and_minimal_copy(self):
        self.assertRegex(self.TEXT, r"(?m)^FROM python:3\.\d+-slim-\w+$")
        self.assertNotIn(":latest", self.TEXT)
        copies = re.findall(r"(?m)^COPY (.*)$", self.TEXT)
        self.assertEqual(copies, ["bwwatch/ /app/bwwatch/"], "only the package is copied: no .env, tests or data")

    def test_runs_as_an_unprivileged_user_after_start(self):
        self.assertIn("--uid 10001", self.TEXT)
        self.assertIn("nologin", self.TEXT)
        self.assertRegex(self.TEXT, r'ENTRYPOINT \["bwwatch"\]')

    def test_user_id_matches_the_code(self):
        from bwwatch.privs import APP_GID, APP_UID

        self.assertEqual((APP_UID, APP_GID), (10001, 10001))


class IgnoreFiles(unittest.TestCase):
    def test_secrets_and_data_never_reach_git_or_the_image(self):
        for name in (".gitignore", ".dockerignore"):
            lines = {l.strip() for l in (ROOT / name).read_text(encoding="utf-8").splitlines()}
            for needed in (".env", "data/", "token.json"):
                self.assertIn(needed, lines, "%s must ignore %s" % (name, needed))
            self.assertTrue({"*.db", "*.db-wal", "*.db-shm"} <= lines, name)
            self.assertIn("!.env.example", lines)

    def test_no_env_file_or_database_is_committed(self):
        import subprocess

        tracked = subprocess.run(["git", "ls-files"], cwd=str(ROOT), capture_output=True, text=True).stdout.split()
        bad = [f for f in tracked if f == ".env" or f.endswith((".db", ".db-wal", ".db-shm")) or f.startswith("data/") or f == "token.json"]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
