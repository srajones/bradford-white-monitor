"""Running as an unprivileged user, and making sure only one watcher runs at a time."""
from __future__ import annotations

import fcntl
import logging
import os
import tempfile
from pathlib import Path
from typing import IO

from .errors import ConfigError

log = logging.getLogger("bwwatch.privs")

APP_UID = 10001
APP_GID = 10001
# Only files bwwatch itself creates are ever re-owned - never arbitrary files in a mounted directory.
_KNOWN_FILES = (
    "bwwatch.db", "bwwatch.db-wal", "bwwatch.db-shm", "bwwatch.db-journal",
    "token.json", "status.json", "bwwatch.lock",
)


class AlreadyRunning(Exception):
    """Another bwwatch already holds the lock on this data directory."""


def _fix_owner(path: Path, uid: int, gid: int) -> None:
    try:
        info = os.lstat(str(path))
        if (info.st_uid, info.st_gid) != (uid, gid):
            os.lchown(str(path), uid, gid)
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        log.warning("could not change the owner of %s: %s", path, exc)


def _hand_over(data_dir: Path, uid: int, gid: int) -> None:
    _fix_owner(data_dir, uid, gid)
    for name in _KNOWN_FILES:
        _fix_owner(data_dir / name, uid, gid)
    for sub in ("backups", "corrupt", "logs"):
        root = data_dir / sub
        if root.is_dir() and not root.is_symlink():
            _fix_owner(root, uid, gid)
            for dirpath, dirnames, filenames in os.walk(str(root)):  # does not follow symlinks
                for entry in dirnames + filenames:
                    _fix_owner(Path(dirpath) / entry, uid, gid)


def prepare_runtime(data_dir: Path, uid: int = APP_UID, gid: int = APP_GID) -> None:
    """Make ``data_dir`` usable, then make sure we are not root.

    Started as root (the default in the container) this creates the directory, gives the
    app user ownership of bwwatch's own files, and drops to that user - so a freshly
    created bind mount "just works" while the service itself never runs with privileges.
    Started as any other user it only checks the directory is writable.
    """
    if os.geteuid() == 0:
        data_dir.mkdir(parents=True, exist_ok=True)
        _hand_over(data_dir, uid, gid)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
        if os.geteuid() == 0:  # pragma: no cover - paranoia
            raise RuntimeError("failed to drop root privileges")
    os.umask(0o077)  # database, tokens and backups are private to the app user
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(data_dir)):
            pass
    except OSError as exc:
        raise ConfigError(
            "cannot write to the data directory %s as user %d (%s). "
            "On the host run:  sudo chown -R %d:%d ./data" % (data_dir, os.geteuid(), exc, APP_UID, APP_GID)
        )


def acquire_lock(path: Path) -> IO[bytes]:
    """Hold an exclusive lock for the life of the process (the kernel drops it if we die)."""
    handle = open(str(path), "a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning("another bwwatch is already running against %s" % path.parent)
    handle.seek(0)
    handle.truncate()
    handle.write(("%d\n" % os.getpid()).encode("ascii"))
    handle.flush()
    return handle
