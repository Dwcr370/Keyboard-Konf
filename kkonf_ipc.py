"""Socket protocol shared by the daemon and its clients.

One JSON object per line, UTF-8, newline-terminated, in both directions.

Client -> daemon, one of:

    {"cmd": "ping"}
    {"cmd": "get"}                      current state
    {"cmd": "effects"}                  what effects exist and their parameters
    {"cmd": "set", "state": {...}}      any subset of the state; "params" merge
    {"cmd": "toggle"}                   on <-> off
    {"cmd": "apply"}                    write the current state again (after Fn+Space)
    {"cmd": "next"}                     the next effect in the cycle

Daemon -> client: one reply per command, {"ok": true, ...} or
{"ok": false, "error": "..."}, plus {"event": "state", "state": {...}} to
every connected client after each change, so a UI stays in step with the
command line and vice versa.
"""

from __future__ import annotations

import json
import os
import socket

SOCKET_ENV = "KKONF_SOCKET"
SOCKET_NAME = "keyboard-konf.sock"


class DaemonUnavailable(Exception):
    """No daemon answered on the socket."""


def socket_path() -> str:
    """The daemon's socket: $KKONF_SOCKET, else $XDG_RUNTIME_DIR/keyboard-konf.sock."""
    override = os.environ.get(SOCKET_ENV)
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return os.path.join(runtime, SOCKET_NAME)


def encode(message: dict) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


class LineBuffer:
    """Collect bytes, hand out complete JSON objects, ignore anything else."""

    def __init__(self) -> None:
        self._data = b""

    def feed(self, chunk: bytes) -> list[dict]:
        self._data += chunk
        messages: list[dict] = []
        while b"\n" in self._data:
            line, self._data = self._data.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(parsed, dict):
                messages.append(parsed)
        return messages


def request(message: dict, timeout: float = 2.0) -> dict:
    """Send one command and return its reply; raise DaemonUnavailable otherwise.

    State events that arrive before the reply are skipped.
    """
    path = socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except (FileNotFoundError, ConnectionRefusedError, PermissionError) as error:
        sock.close()
        raise DaemonUnavailable("no daemon at %s (%s)" % (path, error.strerror or error))
    with sock:
        try:
            sock.sendall(encode(message))
            buffer = LineBuffer()
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    raise DaemonUnavailable("the daemon closed the connection")
                for reply in buffer.feed(chunk):
                    if "event" not in reply:
                        return reply
        except socket.timeout:
            raise DaemonUnavailable("the daemon did not answer within %.0f s" % timeout) from None
        except OSError as error:
            raise DaemonUnavailable(str(error)) from None
