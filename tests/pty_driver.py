"""Drive a program through a real pseudo-terminal, like a person at the keyboard (standard library only).

``converse(argv, script)`` starts the program with a controlling terminal, waits for each pattern in the
script to appear in its output and then types the reply. It exists so the installer's *interactive* path
(colours, Docker's terminal mode, Ctrl-C) can be tested, not just the piped one.
"""
from __future__ import annotations

import os
import pty
import re
import select
import signal
import time
from typing import Dict, List, Optional, Sequence, Tuple

CTRL_C = "\x03"


def converse(
    argv: Sequence[str],
    script: Sequence[Tuple[str, str]],
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    timeout: float = 120.0,
    patience: float = 60.0,
) -> Tuple[int, str]:
    """Run ``argv`` on a terminal; return ``(exit status, everything it printed)``.

    ``script`` is a list of ``(regex, reply)``: once the regex shows up in the output *after the previous
    match*, ``reply`` is typed (a trailing newline is added unless the reply is exactly CTRL_C). When the
    script is used up the program runs to the end. Fails with AssertionError if a pattern never appears.
    """
    pid, fd = pty.fork()
    if pid == 0:  # child
        if cwd:
            os.chdir(cwd)
        os.execvpe(argv[0], list(argv), env if env is not None else dict(os.environ))
    transcript = ""
    consumed = 0
    pending: List[Tuple[str, str]] = list(script)
    started = time.monotonic()
    waiting_since = started
    status: Optional[int] = None
    try:
        while True:
            now = time.monotonic()
            if now - started > timeout:
                raise AssertionError("timed out after %ds; output so far:\n%s" % (timeout, transcript[-1500:]))
            if pending and now - waiting_since > patience:
                raise AssertionError("never saw %r; output so far:\n%s" % (pending[0][0], transcript[-1500:]))
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    transcript += chunk.decode("utf-8", "replace")
                else:
                    break
            if pending:
                match = re.search(pending[0][0], transcript[consumed:])
                if match:
                    consumed += match.end()
                    reply = pending.pop(0)[1]
                    os.write(fd, reply.encode() if reply == CTRL_C else (reply + "\n").encode())
                    waiting_since = time.monotonic()
        _, raw = os.waitpid(pid, 0)
        status = os.waitstatus_to_exitcode(raw)
    finally:
        if status is None:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
        os.close(fd)
    if pending:
        raise AssertionError("the program ended before showing %r; output:\n%s" % (pending[0][0], transcript[-1500:]))
    return status, transcript.replace("\r\n", "\n")
