"""Bare-bones DevTools client for --cdp (time to usable, JS heap after GC).

Stdlib only on purpose, so psutil stays the one dependency. Electron apps take
--remote-debugging-port the same way Chrome does.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

# Discord's server list. It only exists once the real UI is up, and it doesn't depend
# on the language.
DEFAULT_READY_SELECTOR = '[data-list-id="guildsnav"]'

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# ignore HTTP_PROXY and friends, this only ever talks to 127.0.0.1
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PROBE_ERRORS = (OSError, ValueError, http.client.HTTPException)


class CdpError(Exception):
    pass


def _apply_mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return data
    repeated = (key * (len(data) // 4 + 1))[:len(data)]
    masked = int.from_bytes(data, "big") ^ int.from_bytes(repeated, "big")
    return masked.to_bytes(len(data), "big")


def encode_frame(opcode: int, payload: bytes, mask: bytes | None = None, fin: bool = True) -> bytes:
    # clients have to mask their frames, servers must not (RFC 6455, 5.3)
    header = bytearray([(0x80 if fin else 0) | opcode])
    mask_bit = 0x80 if mask else 0
    length = len(payload)
    if length < 126:
        header.append(mask_bit | length)
    elif length < 1 << 16:
        header.append(mask_bit | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(mask_bit | 127)
        header += length.to_bytes(8, "big")
    if mask:
        header += mask
        payload = _apply_mask(payload, mask)
    return bytes(header) + payload


def decode_frame(buf: bytes | bytearray) -> tuple[bool, int, bytes, int] | None:
    """-> (fin, opcode, payload, bytes_used), or None if buf doesn't hold a whole frame yet."""
    if len(buf) < 2:
        return None
    fin = bool(buf[0] & 0x80)
    opcode = buf[0] & 0x0F
    masked = bool(buf[1] & 0x80)
    length = buf[1] & 0x7F
    pos = 2
    if length == 126:
        if len(buf) < 4:
            return None
        length = int.from_bytes(buf[2:4], "big")
        pos = 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length = int.from_bytes(buf[2:10], "big")
        pos = 10
    key = b""
    if masked:
        if len(buf) < pos + 4:
            return None
        key = bytes(buf[pos:pos + 4])
        pos += 4
    if len(buf) < pos + length:
        return None
    payload = bytes(buf[pos:pos + length])
    if masked:
        payload = _apply_mask(payload, key)
    return fin, opcode, payload, pos + length


class WebSocket:
    """Just enough of RFC 6455 for DevTools: text frames, fragmentation, ping/pong, close."""

    def __init__(self, sock: socket.socket, buffered: bytes = b"") -> None:
        self.sock = sock
        self._buf = bytearray(buffered)
        self._fragments: list[bytes] = []

    @classmethod
    def connect(cls, url: str, timeout: float = 5.0) -> WebSocket:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "ws":
            raise CdpError(f"unsupported WebSocket URL: {url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise CdpError("connection closed during the WebSocket handshake")
                response += chunk
                if len(response) > 65536:
                    raise CdpError("WebSocket handshake response is too large")
            head, rest = response.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            status = lines[0].split()
            if len(status) < 2 or status[1] != "101":
                raise CdpError(f"WebSocket handshake rejected: {lines[0]}")
            headers = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
            if headers.get("sec-websocket-accept") != base64.b64encode(digest).decode("ascii"):
                raise CdpError("WebSocket handshake returned a wrong Sec-WebSocket-Accept")
        except BaseException:
            sock.close()
            raise
        return cls(sock, rest)

    def _send(self, opcode: int, payload: bytes) -> None:
        self.sock.sendall(encode_frame(opcode, payload, mask=os.urandom(4)))

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode("utf-8"))

    def recv_text(self, timeout: float | None = None) -> str:
        # a half-read frame stays in self._buf, so a timeout doesn't lose anything
        self.sock.settimeout(timeout)
        while True:
            frame = decode_frame(self._buf)
            if frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise CdpError("WebSocket closed by the client")
                self._buf += chunk
                continue
            fin, opcode, payload, used = frame
            del self._buf[:used]
            if opcode == 0x8:
                raise CdpError("WebSocket closed by the client")
            if opcode == 0x9:
                self._send(0xA, payload)
            elif opcode in (0x0, 0x1, 0x2):
                self._fragments.append(payload)
                if fin:
                    message = b"".join(self._fragments)
                    self._fragments = []
                    return message.decode("utf-8")

    def close(self) -> None:
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        self.sock.close()


class CdpSession:
    """call() waits for the reply with its id; events that arrive meanwhile are dropped."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._next_id = 0

    @classmethod
    def connect(cls, ws_url: str, timeout: float = 5.0) -> CdpSession:
        return cls(WebSocket.connect(ws_url, timeout))

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 10.0
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        self.ws.send_text(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpError(f"{method} timed out")
            message = json.loads(self.ws.recv_text(timeout=remaining))
            if message.get("id") != message_id:
                continue
            if "error" in message:
                error = message["error"]
                detail = error.get("message", error) if isinstance(error, dict) else error
                raise CdpError(f"{method} failed: {detail}")
            result = message.get("result", {})
            return result if isinstance(result, dict) else {}

    def evaluate(self, expression: str, timeout: float = 5.0) -> Any:
        result = self.call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout
        )
        if "exceptionDetails" in result:
            raise CdpError(f"evaluation threw: {result['exceptionDetails'].get('text', '?')}")
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self.ws.close()


def list_targets(port: int, timeout: float = 1.0, host: str = "127.0.0.1") -> list[dict[str, Any]]:
    with _OPENER.open(f"http://{host}:{port}/json/list", timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("unexpected /json/list response")
    return data


# Discord opens a /popout window ~15 s after launch and lists it first, so those only
# count when nothing else matches.
_AUX_PAGES = ("/popout", "/overlay")


def find_page(targets: list[dict[str, Any]], url_match: str) -> dict[str, Any] | None:
    # the splash screen is a file:// page, so url_match already filters it out
    pages = [
        target
        for target in targets
        if target.get("type") == "page"
        and url_match in str(target.get("url", ""))
        and target.get("webSocketDebuggerUrl")
    ]
    for page in pages:
        if not any(aux in str(page.get("url", "")) for aux in _AUX_PAGES):
            return page
    return pages[0] if pages else None


class CdpProbe:
    """Times the ready selector during launch and reads the JS heap at the end."""

    def __init__(
        self, port: int, url_match: str, selector: str, clock: Callable[[], float]
    ) -> None:
        self.port = port
        self.url_match = url_match
        self.selector = selector
        self.clock = clock
        self.ready_time: float | None = None
        self.notes: list[str] = []
        self._session: CdpSession | None = None
        self._port_open = False
        self._expression = f"!!document.querySelector({json.dumps(selector)})"

    def _note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def _connect(self) -> CdpSession | None:
        if self._session is not None:
            return self._session
        try:
            targets = list_targets(self.port, timeout=0.5)
        except _PROBE_ERRORS:
            return None
        self._port_open = True
        page = find_page(targets, self.url_match)
        if page is None:
            return None
        try:
            self._session = CdpSession.connect(page["webSocketDebuggerUrl"], timeout=2.0)
        except (*_PROBE_ERRORS, CdpError) as err:
            self._note(f"could not open a DevTools session: {err}")
            return None
        return self._session

    def _drop(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def poll_ready(self) -> bool:
        """Checks the selector once. Returns True while we are still waiting for it."""
        if self.ready_time is not None:
            return False
        session = self._connect()
        if session is None:
            return True
        try:
            matched = session.evaluate(self._expression, timeout=1.0)
        except (*_PROBE_ERRORS, CdpError):
            self._drop()  # the page navigated or reloaded; reconnect on the next poll
            return True
        if matched is True:
            self.ready_time = round(self.clock(), 3)
            return False
        return True

    def heap_after_gc(self) -> int | None:
        session = self._connect()
        if session is None:
            if not self._port_open:
                self._note(
                    f"DevTools port {self.port} never opened; "
                    "the client may ignore --remote-debugging-port"
                )
            else:
                self._note(f"no DevTools page with a URL containing {self.url_match!r}")
            return None
        try:
            session.call("HeapProfiler.collectGarbage", timeout=20.0)
            usage = session.call("Runtime.getHeapUsage", timeout=5.0)
            return int(usage["usedSize"])
        except (*_PROBE_ERRORS, CdpError, KeyError, TypeError) as err:
            self._note(f"could not read the JS heap: {err}")
            return None

    def close(self) -> None:
        if self._port_open and self.ready_time is None:
            self._note(f"ready selector {self.selector!r} never matched")
        self._drop()
