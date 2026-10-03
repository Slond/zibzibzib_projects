"""AmneziaWG 3.1 configs, UAPI documents, and AmneziaVPN vpn:// keys."""

from __future__ import annotations

import base64
import ipaddress
import json
import random
import re
import struct
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.services.awg.crypto import b64_to_hex, genkey, pubkey

# AmneziaVPN treats the profile as full-tunnel only with this exact line.
ALLOWED_IPS_LINE = "AllowedIPs = 0.0.0.0/0, ::/0"
_ALLOWED_IPS = ["0.0.0.0/0", "::/0"]
_AWG_INI_KEYS = (
    "Jc",
    "Jmin",
    "Jmax",
    "S1",
    "S2",
    "S3",
    "S4",
    "H1",
    "H2",
    "H3",
    "H4",
    "HeaderProtectionKey",
)


@dataclass
class AwgObfuscation:
    jc: int
    jmin: int
    jmax: int
    s1: int
    s2: int
    s3: int
    s4: int
    h1: int
    h2: int
    h3: int
    h4: int
    header_protection_key: str

    def as_conf_lines(self) -> list[str]:
        return [
            f"Jc = {self.jc}",
            f"Jmin = {self.jmin}",
            f"Jmax = {self.jmax}",
            f"S1 = {self.s1}",
            f"S2 = {self.s2}",
            f"S3 = {self.s3}",
            f"S4 = {self.s4}",
            f"H1 = {self.h1}",
            f"H2 = {self.h2}",
            f"H3 = {self.h3}",
            f"H4 = {self.h4}",
            f"HeaderProtectionKey = {self.header_protection_key}",
        ]


def generate_obfuscation(rng: random.Random | None = None) -> AwgObfuscation:
    """Random AWG 3.1 obfuscation. S1–S4 stay >= 12 so header protection fits."""
    r = rng or random.SystemRandom()
    headers: set[int] = set()
    while len(headers) < 4:
        headers.add(r.randint(5, 2_147_483_647))
    h1, h2, h3, h4 = tuple(headers)
    pads = [r.randint(12, 80) for _ in range(4)]
    return AwgObfuscation(
        jc=r.randint(4, 8),
        jmin=40,
        jmax=70,
        s1=pads[0],
        s2=pads[1],
        s3=pads[2],
        s4=pads[3],
        h1=h1,
        h2=h2,
        h3=h3,
        h4=h4,
        header_protection_key=genkey(),
    )


@dataclass
class AwgServerConfig:
    private_key: str
    public_key: str
    listen_port: int
    address: str
    mtu: int
    endpoint: str
    obfuscation: AwgObfuscation

    def to_yaml_dict(self) -> dict[str, Any]:
        data = {
            "private_key": self.private_key,
            "public_key": self.public_key,
            "listen_port": self.listen_port,
            "address": self.address,
            "mtu": self.mtu,
            "endpoint": self.endpoint,
        }
        data.update(asdict(self.obfuscation))
        return data

    @classmethod
    def from_yaml_dict(cls, data: dict[str, Any]) -> AwgServerConfig:
        obf = AwgObfuscation(
            jc=int(data["jc"]),
            jmin=int(data["jmin"]),
            jmax=int(data["jmax"]),
            s1=int(data["s1"]),
            s2=int(data["s2"]),
            s3=int(data["s3"]),
            s4=int(data["s4"]),
            h1=int(data["h1"]),
            h2=int(data["h2"]),
            h3=int(data["h3"]),
            h4=int(data["h4"]),
            header_protection_key=str(data["header_protection_key"]),
        )
        return cls(
            private_key=str(data["private_key"]),
            public_key=str(data["public_key"]),
            listen_port=int(data["listen_port"]),
            address=str(data["address"]),
            mtu=int(data["mtu"]),
            endpoint=str(data.get("endpoint") or ""),
            obfuscation=obf,
        )


def new_server_config(
    *,
    listen_port: int,
    address: str,
    mtu: int,
    endpoint: str,
) -> AwgServerConfig:
    private = genkey()
    return AwgServerConfig(
        private_key=private,
        public_key=pubkey(private),
        listen_port=listen_port,
        address=address,
        mtu=mtu,
        endpoint=endpoint,
        obfuscation=generate_obfuscation(),
    )


def load_server_config(path: Path) -> AwgServerConfig | None:
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    if not data.get("private_key"):
        return None
    return AwgServerConfig.from_yaml_dict(data)


def save_server_config(path: Path, cfg: AwgServerConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg.to_yaml_dict(), sort_keys=False))
    path.chmod(0o600)


@dataclass
class AwgPeer:
    public_key: str
    address: str
    name: str = ""


@dataclass
class AwgPeerFile:
    peers: list[AwgPeer] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"peers": [asdict(p) for p in self.peers]}, indent=2) + "\n"

    @classmethod
    def from_json(cls, raw: str) -> AwgPeerFile:
        data = json.loads(raw or "{}")
        peers = [
            AwgPeer(
                public_key=p["public_key"],
                address=p["address"],
                name=p.get("name", ""),
            )
            for p in data.get("peers", [])
        ]
        return cls(peers=peers)


def load_peers(path: Path) -> AwgPeerFile:
    if not path.exists():
        return AwgPeerFile()
    return AwgPeerFile.from_json(path.read_text())


def save_peers(path: Path, peers: AwgPeerFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(peers.to_json())
    path.chmod(0o600)


def _device_uapi_lines(cfg: AwgServerConfig) -> list[str]:
    o = cfg.obfuscation
    return [
        f"private_key={b64_to_hex(cfg.private_key)}",
        f"listen_port={cfg.listen_port}",
        f"jc={o.jc}",
        f"jmin={o.jmin}",
        f"jmax={o.jmax}",
        f"s1={o.s1}",
        f"s2={o.s2}",
        f"s3={o.s3}",
        f"s4={o.s4}",
        f"h1={o.h1}",
        f"h2={o.h2}",
        f"h3={o.h3}",
        f"h4={o.h4}",
        f"header_protection_key={b64_to_hex(o.header_protection_key)}",
    ]


def render_uapi_sync(cfg: AwgServerConfig, peers: list[AwgPeer] | None = None) -> str:
    """One UAPI set that replaces the peer list and restores the interface."""
    lines = ["set=1", *_device_uapi_lines(cfg), "replace_peers=true"]
    for peer in peers or []:
        lines.extend(
            [
                f"public_key={b64_to_hex(peer.public_key)}",
                "replace_allowed_ips=true",
                f"allowed_ip={peer.address}",
            ]
        )
    return "\n".join(lines) + "\n\n"


def render_uapi_add_peer(public_key_b64: str, allowed_ip: str) -> str:
    return (
        "set=1\n"
        f"public_key={b64_to_hex(public_key_b64)}\n"
        "replace_allowed_ips=true\n"
        f"allowed_ip={allowed_ip}\n"
        "\n"
    )


def render_uapi_remove_peer(public_key_b64: str) -> str:
    return f"set=1\npublic_key={b64_to_hex(public_key_b64)}\nremove=true\n\n"


def render_client_conf(
    *,
    cfg: AwgServerConfig,
    client_private: str,
    client_address: str,
    endpoint: str,
    dns: str = "1.1.1.1",
    keepalive: int = 25,
) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {client_private}",
        f"Address = {client_address}",
        f"DNS = {dns}",
        f"MTU = {cfg.mtu}",
        *cfg.obfuscation.as_conf_lines(),
        "",
        "[Peer]",
        f"PublicKey = {cfg.public_key}",
        f"Endpoint = {endpoint}",
        ALLOWED_IPS_LINE,
        f"PersistentKeepalive = {keepalive}",
        "",
    ]
    return "\n".join(lines)


def parse_client_conf(conf: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in conf.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def render_vpn_uri(conf: str, *, description: str = "") -> str:
    """Pack a client .conf into the vpn:// blob AmneziaVPN imports.

    The app expects Qt qCompress: a 4-byte big-endian length plus zlib.
    AWG 3.1 fields have to live inside last_config. A raw .conf import drops
    HeaderProtectionKey.
    """
    conf = re.sub(r"(?im)^AllowedIPs\s*=\s*.*$", ALLOWED_IPS_LINE, conf, count=1)
    if not conf.endswith("\n"):
        conf += "\n"
    kv = parse_client_conf(conf)
    endpoint = kv.get("Endpoint", "")
    host, sep, port_s = endpoint.rpartition(":")
    if not sep:
        raise ValueError("client config has no Endpoint host:port")
    port = int(port_s)
    last: dict[str, Any] = {
        "config": conf,
        "hostName": host,
        "port": port,
        "client_ip": kv.get("Address", ""),
        "client_priv_key": kv.get("PrivateKey", ""),
        "server_pub_key": kv.get("PublicKey", ""),
        "allowed_ips": list(_ALLOWED_IPS),
        "persistent_keep_alive": kv.get("PersistentKeepalive", "25"),
        "mtu": kv.get("MTU", "1280"),
        "isObfuscationEnabled": True,
        "isThirdPartyConfig": True,
    }
    private = kv.get("PrivateKey")
    if private:
        last["client_pub_key"] = pubkey(private)
    for key in _AWG_INI_KEYS:
        if kv.get(key):
            last[key] = kv[key]
    last_json = json.dumps(last, separators=(",", ":"), ensure_ascii=False)
    dns = kv.get("DNS", "1.1.1.1").split(",")[0].strip()
    envelope = {
        "hostName": host,
        "description": description or host,
        "dns1": dns,
        "dns2": "1.0.0.1",
        "defaultContainer": "amnezia-awg",
        "containers": [
            {
                "container": "amnezia-awg",
                "awg": {
                    "last_config": last_json,
                    "port": str(port),
                    "transport_proto": "udp",
                    "isThirdPartyConfig": True,
                },
            }
        ],
    }
    raw = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode()
    packed = struct.pack(">I", len(raw)) + zlib.compress(raw)
    return "vpn://" + base64.urlsafe_b64encode(packed).decode().rstrip("=")


def next_client_address(server_cidr: str, used: list[str]) -> str:
    """Next free host in the tunnel subnet, as ``ip/32``. Skips the server."""
    net = ipaddress.ip_network(server_cidr, strict=False)
    taken = {item.split("/", 1)[0] for item in used}
    server_ip = str(ipaddress.ip_interface(server_cidr).ip)
    for host in net.hosts():
        text = str(host)
        if text == server_ip or text in taken:
            continue
        return f"{text}/32"
    raise ValueError(f"No free addresses left in {server_cidr}")


def client_endpoint(cfg: AwgServerConfig) -> str:
    if not cfg.endpoint:
        raise ValueError("В server.yml нет endpoint — укажите публичный адрес сервера")
    return f"{cfg.endpoint}:{cfg.listen_port}"
