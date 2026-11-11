import base64
import hashlib
import json
import socket
import threading
import unittest

from client_mod_bench.cdp import CdpError, CdpSession, decode_frame, encode_frame, find_page

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FrameTest(unittest.TestCase):
    def test_roundtrip_all_length_encodings(self):
        for size in (0, 10, 125, 126, 200, 65535, 70000):
            payload = bytes(i % 251 for i in range(size))
            for mask in (None, b"\x01\x02\x03\x04"):
                frame = encode_frame(0x1, payload, mask=mask)
                fin, opcode, decoded, used = decode_frame(frame)
                self.assertTrue(fin)
                self.assertEqual(opcode, 0x1)
                self.assertEqual(decoded, payload)
                self.assertEqual(used, len(frame))

    def test_incomplete_frame(self):
        frame = encode_frame(0x1, b"x" * 300, mask=b"abcd")
        for cut in (0, 1, 3, 7, len(frame) - 1):
            self.assertIsNone(decode_frame(frame[:cut]))


class FakeDevTools(threading.Thread):
    """Tiny WebSocket server that answers like a DevTools page target."""

    def __init__(self):
        super().__init__(daemon=True)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.pongs = 0

    def respond(self, request):
        method = request["method"]
        if method == "Runtime.evaluate":
            return {"result": {"result": {"type": "boolean", "value": True}}}
        if method == "Big":
            return {"result": {"data": "x" * 70000}}
        if method == "Fail":
            return {"error": {"code": -32000, "message": "nope"}}
        return {"result": {"echo": method}}

    def run(self):
        conn, _ = self.server.accept()
        with conn, self.server:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            key = next(line.split(":", 1)[1].strip() for line in data.decode().split("\r\n")
                       if line.lower().startswith("sec-websocket-key"))
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            handshake = (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
            )
            conn.sendall(handshake.encode())
            buf = bytearray()
            while True:
                frame = decode_frame(buf)
                if frame is None:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                    continue
                _, opcode, payload, used = frame
                del buf[:used]
                if opcode == 0x8:
                    return
                if opcode == 0xA:
                    self.pongs += 1
                    continue
                request = json.loads(payload)
                event = json.dumps({"method": "Page.frameNavigated", "params": {}}).encode()
                conn.sendall(encode_frame(0x1, event) + encode_frame(0x9, b"ping"))
                body = json.dumps({"id": request["id"], **self.respond(request)}).encode()
                half = len(body) // 2  # fragmented message: text frame + continuation frame
                conn.sendall(encode_frame(0x1, body[:half], fin=False)
                             + encode_frame(0x0, body[half:]))


class SessionTest(unittest.TestCase):
    def test_calls_skip_events_and_answer_pings(self):
        server = FakeDevTools()
        server.start()
        session = CdpSession.connect(f"ws://127.0.0.1:{server.port}/devtools/page/A", timeout=5)
        try:
            self.assertEqual(session.call("Echo.test")["echo"], "Echo.test")
            self.assertIs(session.evaluate("1 + 1 === 2"), True)
            self.assertEqual(len(session.call("Big")["data"]), 70000)
            with self.assertRaises(CdpError):
                session.call("Fail")
        finally:
            session.close()
        server.join(timeout=5)
        self.assertGreaterEqual(server.pongs, 4)


class FindPageTest(unittest.TestCase):
    def test_skips_splash_and_workers(self):
        targets = [
            {"type": "page", "url": "file:///splash/index.html", "webSocketDebuggerUrl": "ws://a"},
            {"type": "service_worker", "url": "https://discord.com/sw.js",
             "webSocketDebuggerUrl": "ws://b"},
            {"type": "page", "url": "https://discord.com/channels/@me",
             "webSocketDebuggerUrl": "ws://c"},
        ]
        self.assertEqual(find_page(targets, "discord.com")["webSocketDebuggerUrl"], "ws://c")
        self.assertIsNone(find_page(targets, "example.org"))

    def test_prefers_main_window_over_popout(self):
        # Real Discord Canary lists its /popout window first once it has opened.
        popout = {"type": "page", "url": "https://canary.discord.com/popout",
                  "webSocketDebuggerUrl": "ws://popout"}
        main = {"type": "page", "url": "https://canary.discord.com/channels/@me",
                "webSocketDebuggerUrl": "ws://main"}
        self.assertEqual(find_page([popout, main], "discord.com"), main)
        self.assertEqual(find_page([popout], "discord.com"), popout)


if __name__ == "__main__":
    unittest.main()
