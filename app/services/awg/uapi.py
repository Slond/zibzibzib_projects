"""Talk to amneziawg-go over its userspace UAPI socket."""

from __future__ import annotations

import socket
from dataclasses import dataclass

from app.services.awg.crypto import hex_to_b64


class AwgUapiError(RuntimeError):
    pass


def uapi_send(payload: str, socket_path: str) -> str:
    """Send one UAPI request. Raises AwgUapiError when errno is not 0."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5)
        sock.connect(socket_path)
        sock.sendall(payload.encode())
        chunks: list[bytes] = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"\n\n" in b"".join(chunks):
                break
        raw = b"".join(chunks).decode(errors="replace")
    except OSError as exc:
        raise AwgUapiError(f"Нет связи с amneziawg-go ({socket_path}): {exc}") from exc
    finally:
        sock.close()

    errno = None
    for line in raw.splitlines():
        if line.startswith("errno="):
            try:
                errno = int(line.split("=", 1)[1])
            except ValueError:
                errno = -1
    if errno not in (0, None):
        raise AwgUapiError(f"amneziawg-go UAPI errno={errno}: {raw.strip()[:200]}")
    return raw


@dataclass
class PeerDump:
    public_key: str
    endpoint: str
    handshake: int
    rx_bytes: int
    tx_bytes: int


def parse_uapi_dump(raw: str) -> list[PeerDump]:
    """Parse a UAPI ``get=1`` dump into one record per peer.

    ``rx_bytes`` is what the server received from the client (client upload).
    ``tx_bytes`` is what the server sent to the client (client download).
    """
    peers: list[PeerDump] = []
    current: dict[str, str] | None = None

    def finish() -> None:
        nonlocal current
        if not current or "public_key" not in current:
            current = None
            return
        peers.append(
            PeerDump(
                public_key=hex_to_b64(current["public_key"]),
                endpoint=current.get("endpoint", ""),
                handshake=int(current.get("last_handshake_time_sec") or 0),
                rx_bytes=int(current.get("rx_bytes") or 0),
                tx_bytes=int(current.get("tx_bytes") or 0),
            )
        )
        current = None

    for line in raw.splitlines():
        if not line or line.startswith("errno="):
            continue
        if line.startswith("public_key="):
            finish()
            current = {"public_key": line.split("=", 1)[1].strip()}
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key] = value.strip()
    finish()
    return peers


def uapi_dump(socket_path: str) -> list[PeerDump]:
    raw = uapi_send("get=1\n\n", socket_path)
    return parse_uapi_dump(raw)
