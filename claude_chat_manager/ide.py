"""Talks to a running editor over the bridge Claude Code leaves in ~/.claude/ide, which is how a
conversation's tab is closed from outside the editor."""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import vscode

AUTH_HEADER = "x-claude-code-ide-authorization"
SUBPROTOCOL = "mcp"
PROTOCOL_VERSION = "2025-06-18"

TEXT, BINARY, CLOSE, PING, PONG = 0x1, 0x2, 0x8, 0x9, 0xA


class BridgeError(Exception):
    """Raised when the editor cannot be reached or refuses the request."""


@dataclass
class Bridge:
    """One running editor window, as its lock file describes it."""

    port: int
    pid: int
    token: str
    ide_name: str
    folders: list[str]

    @property
    def alive(self) -> bool:
        """Whether the editor process that wrote this lock is still running."""
        return Path(f"/proc/{self.pid}").is_dir() if Path("/proc").is_dir() else True

    def covers(self, project_path: str) -> bool:
        """Whether this window has the project open, which is where its tabs are."""
        target = os.path.realpath(project_path)
        return any(
            target == os.path.realpath(f) or target.startswith(os.path.realpath(f) + os.sep)
            for f in self.folders
        )


def lock_dir(cfg: config_mod.Config | None = None) -> Path:
    """Where the editor extensions announce themselves."""
    cfg = cfg or config_mod.load()
    return Path(cfg.claude_dir).expanduser() / "ide"


def bridges(cfg: config_mod.Config | None = None) -> list[Bridge]:
    """Every editor window currently offering the bridge."""
    directory = lock_dir(cfg)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.lock")):
        # The file is named after the port it listens on; the pid inside is the editor's.
        try:
            port = int(path.stem)
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        bridge = Bridge(
            port=port,
            pid=int(record.get("pid") or 0),
            token=str(record.get("authToken") or ""),
            ide_name=str(record.get("ideName") or "editor"),
            folders=[str(f) for f in record.get("workspaceFolders") or []],
        )
        if bridge.token and bridge.alive:
            out.append(bridge)
    return out


def bridge_for(project_path: str, cfg: config_mod.Config | None = None) -> Bridge | None:
    """The running window that has this project open."""
    return next((b for b in bridges(cfg) if b.covers(project_path)), None)


# The websocket client ------------------------------------------------------------------------------


class Socket:
    """Just enough of a websocket client to exchange a few small JSON messages."""

    def __init__(self, port: int, token: str, timeout: float = 5.0) -> None:
        self.raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.raw.settimeout(timeout)
        self.buffer = b""
        self._handshake(port, token)

    def _handshake(self, port: int, token: str) -> None:
        """Upgrade the connection, and refuse to go on unless the editor agrees to."""
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: {SUBPROTOCOL}\r\n"
            f"{AUTH_HEADER}: {token}\r\n\r\n"
        )
        self.raw.sendall(request.encode())
        while b"\r\n\r\n" not in self.buffer:
            chunk = self.raw.recv(4096)
            if not chunk:
                raise BridgeError("the editor closed the connection during the handshake")
            self.buffer += chunk
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise BridgeError(f"the editor refused the connection: {status}")

    def send(self, payload: dict) -> None:
        """Write one JSON message as a masked text frame, which is what a client must send."""
        body = json.dumps(payload).encode()
        header = bytearray([0x80 | TEXT])
        length = len(body)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = secrets.token_bytes(4)
        header += mask
        self.raw.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(body)))

    def _read(self, count: int) -> bytes:
        """Read exactly this many bytes, topping the buffer up from the socket."""
        while len(self.buffer) < count:
            chunk = self.raw.recv(65536)
            if not chunk:
                raise BridgeError("the editor closed the connection")
            self.buffer += chunk
        out, self.buffer = self.buffer[:count], self.buffer[count:]
        return out

    def receive(self) -> dict:
        """Read frames until a whole JSON message arrives, answering pings on the way."""
        parts: list[bytes] = []
        while True:
            first, second = self._read(2)
            final, opcode = first & 0x80, first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else b""
            body = self._read(length)
            if mask:
                body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
            if opcode == PING:
                self.send_frame(PONG, body)
                continue
            if opcode == CLOSE:
                raise BridgeError("the editor closed the connection")
            if opcode in (TEXT, BINARY, 0x0):
                parts.append(body)
                if final:
                    return json.loads(b"".join(parts).decode(errors="replace"))

    def send_frame(self, opcode: int, body: bytes = b"") -> None:
        """Write one short control frame."""
        mask = secrets.token_bytes(4)
        header = bytes([0x80 | opcode, 0x80 | len(body)]) + mask
        self.raw.sendall(header + bytes(b ^ mask[i % 4] for i, b in enumerate(body)))

    def close(self) -> None:
        """Say goodbye and drop the socket, ignoring an editor that has already gone."""
        try:
            self.send_frame(CLOSE)
        except OSError:
            pass
        finally:
            self.raw.close()


# Calling it ----------------------------------------------------------------------------------------


def call(bridge: Bridge, tool: str, arguments: dict | None = None) -> dict:
    """Run one of the editor's tools and return its reply."""
    try:
        connection = Socket(bridge.port, bridge.token)
    except (OSError, BridgeError) as exc:
        raise BridgeError(f"could not reach {bridge.ide_name} on port {bridge.port}: {exc}") from exc
    try:
        connection.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "claude-chat-manager", "version": "1"},
                },
            }
        )
        _wait_for(connection, 1)
        connection.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        connection.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            }
        )
        return _wait_for(connection, 2)
    except (OSError, ValueError) as exc:
        raise BridgeError(f"the editor did not answer: {exc}") from exc
    finally:
        connection.close()


def _wait_for(connection: Socket, message_id: int) -> dict:
    """Read replies until the one with this id turns up, since notifications arrive in between."""
    for _ in range(50):
        message = connection.receive()
        if message.get("id") == message_id:
            if "error" in message:
                raise BridgeError(str(message["error"].get("message") or message["error"]))
            return message.get("result") or {}
    raise BridgeError("the editor never answered")


def close_tab(bridge: Bridge, label: str) -> None:
    """Ask the editor to close the tab with this exact label."""
    # The editor reports TAB_CLOSED whether or not a tab matched, so the caller has to verify.
    call(bridge, "close_tab", {"tab_name": label})


def tab_is_open(session_id: str, project_path: str, cfg: config_mod.Config | None = None) -> str:
    """The label of this conversation's tab if an editor still has it open."""
    return vscode.tab_label(session_id, project_path, cfg)


def close_conversation_tab(
    session_id: str, project_path: str, cfg: config_mod.Config | None = None
) -> str:
    """Close one conversation's tab in the editor that has its project open.

    Returns a sentence saying what happened, and raises BridgeError when the editor could not be
    asked at all.
    """
    label = tab_is_open(session_id, project_path, cfg)
    if not label:
        return "no tab was open for it"
    bridge = bridge_for(project_path, cfg)
    if bridge is None:
        raise BridgeError(f"no running editor has {project_path} open")
    close_tab(bridge, label)
    # The extension rewrites its tab list a moment after closing, so give it time before checking.
    for _ in range(20):
        time.sleep(0.25)
        if not tab_is_open(session_id, project_path, cfg):
            return f"closed the tab named {label!r}"
    return f"asked {bridge.ide_name} to close {label!r}, but it is still listed as open"


def close_tab_quietly(
    session_id: str, project_path: str, cfg: config_mod.Config | None = None
) -> str:
    """Close the tab if one is open, reporting an unreachable editor rather than raising.

    Deleting a conversation should not be stopped by an editor that has already gone, so this is
    what the delete paths use.
    """
    try:
        return close_conversation_tab(session_id, project_path, cfg)
    except BridgeError as exc:
        return f"could not close its tab: {exc}"
