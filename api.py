#!/usr/bin/env python3
"""Talk to the running Herdr server.

The overlay has one job on open: know every tab before the first frame. The
CLI can answer that, but each `herdr` call is a 24MB binary start that then
talks to the same socket we can reach ourselves -- and it answers one question
at a time, so a list of tabs plus a list of spaces is two of them.

`session.snapshot` over the Unix socket at HERDR_SOCKET_PATH is one round trip
(~15ms here) and carries tabs, panes, workspaces and agents together, which is
also where the cwd, agent kind and terminal title on each row come from. The
CLI stays as the fallback for a missing socket, a Windows named pipe, or a
server that restarted under us.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

PROBE_ID = "drover"


class ApiError(RuntimeError):
    """A Herdr request failed, or no server could be reached."""


def socket_path() -> str:
    return os.environ.get("HERDR_SOCKET_PATH") or ""


def herdr_bin() -> str:
    """The binary to shell out to.

    HERDR_BIN_PATH can outlive the binary it names -- upgrading Herdr under a
    running server leaves the old path dangling -- so an unusable value falls
    back to whatever `herdr` resolves to on PATH.
    """
    candidate = os.environ.get("HERDR_BIN_PATH")
    if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return "herdr"


class Client:
    def __init__(self, path: str | None = None, timeout: float = 4.0) -> None:
        self.path = socket_path() if path is None else path
        self.timeout = timeout
        self.socket_ok = bool(self.path) and hasattr(socket, "AF_UNIX") and os.path.exists(self.path)

    # -- transports ---------------------------------------------------------

    def _socket_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"id": PROBE_ID, "method": method, "params": params}) + "\n"
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(self.timeout)
        buffered = b""
        try:
            conn.connect(self.path)
            conn.sendall(payload.encode("utf-8"))
            while b"\n" not in buffered:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffered += chunk
        finally:
            try:
                conn.close()
            except OSError:
                pass
        head = buffered.split(b"\n", 1)[0]
        if not head:
            raise ApiError("herdr closed the connection without a response")
        return json.loads(head.decode("utf-8", "replace"))

    def _cli_call(self, args: list[str]) -> dict[str, Any]:
        import subprocess  # imported here: the socket path never pays for it

        try:
            result = subprocess.run(
                [herdr_bin(), *args],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout + 4.0,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ApiError("herdr binary not found (set HERDR_BIN_PATH)") from exc
        except subprocess.TimeoutExpired as exc:
            raise ApiError(f"herdr {' '.join(args)} timed out") from exc
        out = (result.stdout or b"").decode("utf-8", "replace").strip()
        if not out:
            err = (result.stderr or b"").decode("utf-8", "replace").strip()
            raise ApiError(err or f"herdr {' '.join(args)} produced no output")
        try:
            return json.loads(out.splitlines()[-1])
        except ValueError as exc:
            raise ApiError(f"herdr {' '.join(args)} returned unparseable output") from exc

    @staticmethod
    def _unwrap(response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise ApiError("herdr returned an unexpected response shape")
        error = response.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ApiError(str(message or "herdr request failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise ApiError("herdr returned an unexpected response shape")
        return result

    def call(self, method: str, params: dict[str, Any], cli: list[str]) -> dict[str, Any]:
        if self.socket_ok:
            try:
                return self._unwrap(self._socket_call(method, params))
            except ApiError:
                raise
            except (OSError, ValueError):
                # Transport failure only: the server may have restarted or
                # handed off. Stop trusting the socket for the rest of this run.
                self.socket_ok = False
        return self._unwrap(self._cli_call(cli))

    # -- operations ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        result = self.call("session.snapshot", {}, ["api", "snapshot"])
        snap = result.get("snapshot")
        if not isinstance(snap, dict):
            raise ApiError("session snapshot missing from response")
        return snap

    def read_pane(self, pane_id: str, lines: int, source: str = "visible") -> str:
        result = self.call(
            "pane.read",
            {"pane_id": pane_id, "source": source, "lines": lines},
            ["pane", "read", pane_id, "--source", source, "--lines", str(lines)],
        )
        read = result.get("read")
        if isinstance(read, dict) and isinstance(read.get("text"), str):
            return read["text"]
        return ""

    def focus_workspace(self, workspace_id: str) -> None:
        self.call("workspace.focus", {"workspace_id": workspace_id}, ["workspace", "focus", workspace_id])

    def focus_tab(self, tab_id: str) -> None:
        self.call("tab.focus", {"tab_id": tab_id}, ["tab", "focus", tab_id])

    def focus_pane(self, pane_id: str) -> None:
        # The CLI's `pane focus` only moves by direction, so there is no CLI
        # form. Without a socket the tab focus we do first is as close as we
        # can get, which beats failing the jump.
        if not self.socket_ok:
            return
        try:
            self._unwrap(self._socket_call("pane.focus", {"pane_id": pane_id}))
        except (OSError, ValueError, ApiError):
            self.socket_ok = False

    def close_tab(self, tab_id: str) -> None:
        self.call("tab.close", {"tab_id": tab_id}, ["tab", "close", tab_id])

    def create_tab(self, label: str = "") -> None:
        params: dict[str, Any] = {"focus": True}
        cli = ["tab", "create", "--focus"]
        if label:
            params["label"] = label
            cli.extend(["--label", label])
        self.call("tab.create", params, cli)


_CLIENT: Client | None = None


def client() -> Client:
    """One client per process: the socket check is a stat we do not repeat."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = Client()
    return _CLIENT


def reset_client() -> None:
    global _CLIENT
    _CLIENT = None
