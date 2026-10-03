"""In-process AmneziaWG UAPI stand-in for tests and the local demo."""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from app.services.awg.crypto import b64_to_hex


class LabDaemon:
    def __init__(self, path: Path):
        self.path = path
        self.peers: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()
        self._stop = False
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.path))
        sock.listen(16)
        sock.settimeout(0.2)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, name="awg-lab", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    def set_stats(
        self,
        public_b64: str,
        *,
        rx: int,
        tx: int,
        handshake: int = 0,
        endpoint: str = "203.0.113.8:50000",
    ) -> None:
        key = b64_to_hex(public_b64)
        with self._lock:
            peer = self.peers.setdefault(key, _empty_peer())
            peer["rx"] = rx
            peer["tx"] = tx
            peer["handshake"] = handshake
            peer["endpoint"] = endpoint

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                self._read(conn)
            finally:
                conn.close()

    def _read(self, conn: socket.socket) -> None:
        data = b""
        conn.settimeout(2)
        while b"\n\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        if not data:
            return
        conn.sendall(self._handle(data.decode(errors="replace")).encode())

    def _handle(self, text: str) -> str:
        first = text.lstrip().split("\n", 1)[0]
        if first.startswith("get=1"):
            return self._dump()
        lines = text.splitlines()
        with self._lock:
            if any(line == "replace_peers=true" for line in lines):
                self.peers.clear()
            current: str | None = None
            for line in lines:
                if line.startswith("public_key="):
                    current = line.split("=", 1)[1].strip()
                    self.peers.setdefault(current, _empty_peer())
                elif line == "remove=true" and current:
                    self.peers.pop(current, None)
                    current = None
                elif line.startswith("allowed_ip=") and current and current in self.peers:
                    self.peers[current]["allowed_ip"] = line.split("=", 1)[1].strip()
        return "errno=0\n\n"

    def _dump(self) -> str:
        with self._lock:
            items = list(self.peers.items())
        lines: list[str] = []
        for key, peer in items:
            lines.append(f"public_key={key}")
            endpoint = str(peer.get("endpoint") or "")
            if endpoint:
                lines.append(f"endpoint={endpoint}")
            lines.append(f"last_handshake_time_sec={int(peer.get('handshake') or 0)}")
            lines.append(f"rx_bytes={int(peer.get('rx') or 0)}")
            lines.append(f"tx_bytes={int(peer.get('tx') or 0)}")
        lines.append("errno=0")
        lines.append("")
        return "\n".join(lines) + "\n"


def _empty_peer() -> dict[str, object]:
    return {"rx": 0, "tx": 0, "handshake": 0, "endpoint": "", "allowed_ip": ""}
