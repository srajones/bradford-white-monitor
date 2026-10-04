#!/usr/bin/env python3
"""A pretend ``docker`` for testing install.sh and bwctl without a Docker daemon.

It understands only the handful of commands those scripts use, answers them from a scenario file, keeps a
little state (is there a container? an image?) and records every call. Anything else it refuses with status
99 and records as *unexpected*, so a test can prove the scripts never ran a command they should not.

Environment: FAKE_DOCKER_WORLD = a folder holding scenario.json (written by the test), state.json and
calls.jsonl (written here).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

WORLD = Path(os.environ.get("FAKE_DOCKER_WORLD", "."))
LABEL = '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'

DEFAULTS = {
    "info": "ok",                      # ok | permission | down | odd
    "compose_version": "2.29.1",       # "" = no compose plugin
    "config": "ok",                    # ok | fail
    "build": "ok",                     # ok | fail
    "wizard_rc": 0,
    "generated": "NTFY_TOPIC=bwwatch-0123456789abcdef0123\nDISPLAY_TZ=UTC\n",
    "print_env_rc": 0,
    "verify_setup": "auto",            # auto = compare .env with what the wizard made | fail
    "up_rc": 0,
    "verify_rc": 0,
    "verify_out": "  [ OK ] Service       running - ok (0 consecutive failed poll(s))\n"
                  "  [ OK ] Wave cloud    last poll succeeded at 2026-10-04 22:46 UTC\n"
                  "  [ OK ] Alert channels  ntfy\n",
    "wipe_rc": 0,
    "container": {"exists": False, "running": False, "owner": ""},
    "image": False,
}


def read_json(name: str, default):
    try:
        return json.loads((WORLD / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_state(state) -> None:
    (WORLD / "state.json").write_text(json.dumps(state), encoding="utf-8")


def record(argv, **extra) -> None:
    entry = {"argv": argv, "cwd": os.getcwd(), "tty_in": sys.stdin.isatty(), "tty_out": sys.stdout.isatty(), **extra}
    with open(WORLD / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def out(text: str = "") -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def main(argv) -> int:
    scenario = dict(DEFAULTS, **read_json("scenario.json", {}))
    state = read_json("state.json", None)
    if state is None:
        state = {"container": dict(scenario["container"]), "image": scenario["image"], "generated": None}
    record(argv)
    code = dispatch(argv, scenario, state)
    write_state(state)
    return code


def dispatch(argv, sc, state) -> int:
    if not argv:
        return 99
    head, rest = argv[0], argv[1:]

    if head == "info":
        if sc["info"] == "permission":
            err("permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock\n")
            return 1
        if sc["info"] == "down":
            err("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n")
            return 1
        if sc["info"] == "odd":
            err("something unexpected happened\nsecond line\n")
            return 1
        if "--format" in rest:
            out(sc.get("docker_root", str(WORLD / "docker-root")) + "\n")
        return 0

    if head == "version":
        out("26.1.0\n")
        return 0

    if head == "inspect":
        return inspect(rest, sc, state)

    if head == "image":
        return image(rest, state)

    if head == "compose":
        return compose(rest, sc, state)

    return unexpected(argv)


def unexpected(argv) -> int:
    record(argv, unexpected=True)
    err("fake docker: unexpected command: %s\n" % " ".join(argv))
    return 99


def inspect(rest, sc, state) -> int:
    if len(rest) != 3 or rest[0] != "-f" or rest[2] != "bwwatch":
        return unexpected(["inspect"] + rest)
    box = state["container"]
    if not box["exists"]:
        err("Error: No such object: bwwatch\n")
        return 1
    fmt = rest[1]
    if fmt == "{{.Id}}":
        out("0123456789abcdef\n")
    elif fmt == "{{.State.Running}}":
        out("true\n" if box["running"] else "false\n")
    elif fmt == LABEL:
        out(box.get("owner", "") + "\n")
    else:
        return unexpected(["inspect"] + rest)
    return 0


def image(rest, state) -> int:
    if rest[:1] == ["inspect"] and rest[-1:] == ["bwwatch:local"]:
        if not state["image"]:
            err("Error: No such image: bwwatch:local\n")
            return 1
        out("135000000\n" if "{{.Size}}" in rest else "[]\n")
        return 0
    if rest == ["rm", "bwwatch:local"]:
        if not state["image"]:
            err("Error: No such image: bwwatch:local\n")
            return 1
        state["image"] = False
        out("Untagged: bwwatch:local\n")
        return 0
    return unexpected(["image"] + rest)


def compose(rest, sc, state) -> int:
    if rest[:1] == ["version"]:
        if not sc["compose_version"]:
            err("docker: 'compose' is not a docker command.\n")
            return 1
        out(sc["compose_version"] + "\n")
        return 0

    project_dir = None
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        if rest[i] in ("-p", "-f", "--project-directory"):
            if rest[i] == "--project-directory":
                project_dir = rest[i + 1]
            i += 2
        else:
            return unexpected(["compose"] + rest)
    if i >= len(rest):
        return unexpected(["compose"] + rest)
    sub, args = rest[i], rest[i + 1:]
    box = state["container"]

    if sub == "config" and args == ["-q"]:
        if sc["config"] == "fail":
            err("validating docker-compose.yml: services.bwwatch Additional property bogus is not allowed\n")
            return 1
        return 0

    if sub == "build" and not args:
        out("#1 [internal] load build definition from Dockerfile\n#2 DONE 0.1s\n")
        if sc["build"] == "fail":
            err("ERROR: failed to solve: python:3.12-slim-bookworm: failed to resolve source metadata: "
                "dial tcp: lookup registry-1.docker.io: no such host\n")
            return 1
        state["image"] = True
        return 0

    if sub == "run":
        return run(args, sc, state, project_dir)

    if sub == "up":
        if args not in (["-d", "bwwatch"], ["-d", "--force-recreate", "bwwatch"]):
            return unexpected(["compose"] + rest)
        if sc["up_rc"]:
            err("Error response from daemon: driver failed programming external connectivity\n")
            return sc["up_rc"]
        box.update(exists=True, running=True, owner=project_dir or "")
        state["up_at"] = time.time()
        return 0

    if sub == "exec":
        if not box["running"]:
            err('service "bwwatch" is not running\n')
            return 1
        cmd = [a for a in args if a != "-T"]
        if cmd[:2] == ["bwwatch", "bwwatch"]:
            command = cmd[2:]
            if command[:1] == ["verify"]:
                out(sc["verify_out"])
                return sc["verify_rc"]
            out("fake exec: %s\n" % " ".join(command))
            return 0
        return unexpected(["compose"] + rest)

    if sub == "logs":
        out("bwwatch-1  | fake log line 1\nbwwatch-1  | fake log line 2\n")
        return 0
    if sub == "stop" and not args:
        box["running"] = False
        return 0
    if sub == "start" and not args:
        if not box["exists"]:
            err("no container to start\n")
            return 1
        box["running"] = True
        return 0
    if sub == "down" and not args:
        box.update(exists=False, running=False, owner="")
        return 0

    return unexpected(["compose"] + rest)


def run(args, sc, state, project_dir) -> int:
    entrypoint = None
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--entrypoint":
            entrypoint = args[i + 1]
            i += 2
        elif args[i] in ("--rm", "-T", "--no-deps"):
            i += 1
        else:
            return unexpected(["compose", "run"] + args)
    if i >= len(args) or args[i] != "bwwatch":
        return unexpected(["compose", "run"] + args)
    command = args[i + 1:]

    if entrypoint == "find":
        return sc["wipe_rc"]
    if entrypoint is not None:
        return unexpected(["compose", "run"] + args)

    if command == ["setup"]:
        out("(fake guided setup)\n")
        if sc.get("wizard_sleep"):
            try:
                time.sleep(sc["wizard_sleep"])
            except KeyboardInterrupt:
                out("\nStopped. Nothing has been installed yet.\n")
                return 130
        if sc["wizard_rc"] == 0:
            state["generated"] = sc["generated"]
        return sc["wizard_rc"]
    if command == ["setup", "--print-env"]:
        if state.get("generated") is None or sc["print_env_rc"]:
            return sc["print_env_rc"] or 1
        out(state["generated"])
        return 0
    if command == ["setup", "--verify"]:
        installed = Path(project_dir or ".", ".env")
        same = installed.exists() and installed.read_text(encoding="utf-8") == state.get("generated")
        if sc["verify_setup"] == "fail" or not same:
            out("These settings did NOT arrive exactly as chosen: NTFY_TOPIC\n")
            return 1
        out("All 2 settings reached the service exactly as chosen; alert channels: ntfy.\n")
        return 0
    if command == ["setup", "--cleanup"]:
        state["generated"] = None
        return 0
    out("fake run: %s\n" % " ".join(command))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
