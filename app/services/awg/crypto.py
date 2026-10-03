"""WireGuard X25519 keys. No third-party crypto library."""

from __future__ import annotations

import base64
import os

_P = 2**255 - 19
_A24 = 121665


def _clamp_private(raw: bytes) -> bytes:
    key = bytearray(raw)
    if len(key) != 32:
        raise ValueError("X25519 key must be 32 bytes")
    key[0] &= 248
    key[31] &= 127
    key[31] |= 64
    return bytes(key)


def _x25519(scalar: bytes, u_coord: bytes) -> bytes:
    """RFC 7748 X25519. ``scalar`` is 32 bytes."""
    k = bytearray(_clamp_private(scalar))
    u = list(u_coord)
    u[-1] &= 127
    x1 = int.from_bytes(bytes(u), "little") % _P
    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0
    bits = [(k[i >> 3] >> (i & 7)) & 1 for i in range(255)]
    for bit in reversed(bits):
        swap ^= bit
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = bit
        a = (x2 + z2) % _P
        aa = (a * a) % _P
        b = (x2 - z2) % _P
        bb = (b * b) % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = (d * a) % _P
        cb = (c * b) % _P
        x3 = pow(da + cb, 2, _P)
        z3 = (x1 * pow(da - cb, 2, _P)) % _P
        x2 = (aa * bb) % _P
        z2 = (e * (aa + _A24 * e)) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    out = (x2 * pow(z2, _P - 2, _P)) % _P
    return out.to_bytes(32, "little")


def genkey() -> str:
    """WireGuard/Amnezia private key (standard base64)."""
    return base64.b64encode(_clamp_private(os.urandom(32))).decode()


def pubkey(private_b64: str) -> str:
    """Public key for a base64 private key."""
    private = base64.b64decode(private_b64)
    nine = (9).to_bytes(32, "little")
    return base64.b64encode(_x25519(private, nine)).decode()


def b64_to_hex(value: str) -> str:
    return base64.b64decode(value).hex()


def hex_to_b64(value: str) -> str:
    return base64.b64encode(bytes.fromhex(value)).decode()
