"""Install amneziawg-go 3.1 on one Linux server. No hop, no Docker."""

from __future__ import annotations

import base64
import ipaddress
import shlex
from dataclasses import dataclass, field

import yaml

from app.services.awg.protocol import (
    AwgPeer,
    AwgPeerFile,
    AwgServerConfig,
    new_server_config,
    render_uapi_sync,
)

AMNEZIAWG_GO_TAG = "v3.1.20260828"
AMNEZIAWG_GO_COMMIT = "b5928efb6ca19f0153958460c3d141f04abc5c2e"
AMNEZIAWG_GO_REPO = "https://github.com/amnezia-vpn/amneziawg-go.git"
GO_VERSION = "1.23.6"
GO_TARBALL_SHA256 = {
    "amd64": "9379441ea310de000f33a4dc767bd966e72ab2826270e038e78b2c53c2e7802d",
    "arm64": "561c780e8f4a8955d32bf72e46af0b5ee5e0debe1e4633df9a03781878219202",
}
AWG_DIR = "/etc/amneziawg"
AWG_INTERFACE = "awg0"
AWG_SERVICE = "amneziawg"
DEFAULT_PORT = 1234
DEFAULT_ADDRESS = "10.66.66.1/24"
DEFAULT_MTU = 1280
PANEL_ROOT = "/opt/awg-panel"
PANEL_VENV = "/opt/awg-panel-venv"
PANEL_ENV = "/etc/awg-panel.env"
PANEL_DATA = "/var/lib/awg-panel"

_CONFIGURE = r"""#!/bin/bash
set -euo pipefail
IFACE="__IFACE__"
SOCK="/var/run/amneziawg/${IFACE}.sock"
CONF="__AWG_DIR__/uapi.set"
ADDR="__ADDRESS__"
MTU="__MTU__"
SUBNET="__SUBNET__"

for _ in $(seq 1 40); do
  if [ -S "$SOCK" ]; then
    break
  fi
  sleep 0.25
done
if [ ! -S "$SOCK" ]; then
  echo "amneziawg-go socket missing: $SOCK" >&2
  exit 1
fi

ip addr replace "$ADDR" dev "$IFACE"
ip link set mtu "$MTU" up dev "$IFACE"
# amneziawg-go writes packets into the tun and does not fill checksums.
# With tx offload left on, the client drops every TCP and UDP reply:
# the handshake stays up and the internet does not.
if command -v ethtool >/dev/null 2>&1; then
  ethtool -K "$IFACE" tx off sg off tso off gso off gro off || true
fi

python3 - "$SOCK" "$CONF" <<'PY'
import socket, sys
sock_path, conf_path = sys.argv[1], sys.argv[2]
with open(conf_path) as handle:
    raw = handle.read()
if not raw.strip():
    raise SystemExit("no UAPI config")
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(8)
sock.connect(sock_path)
sock.sendall(raw.encode())
chunks = []
while True:
    data = sock.recv(4096)
    if not data:
        break
    chunks.append(data)
    if b"\n\n" in b"".join(chunks):
        break
sock.close()
out = b"".join(chunks).decode(errors="replace")
errno = None
for line in out.splitlines():
    if line.startswith("errno="):
        errno = int(line.split("=", 1)[1])
if errno not in (0, None):
    raise SystemExit(out.strip() or "UAPI error")
PY

sysctl -w net.ipv4.ip_forward=1 >/dev/null
sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null
mkdir -p /etc/sysctl.d
printf '%s\n' 'net.ipv4.ip_forward=1' 'net.ipv6.conf.all.forwarding=1' \
  > /etc/sysctl.d/99-amneziawg.conf

iptables -t nat -C POSTROUTING -s "$SUBNET" ! -d "$SUBNET" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s "$SUBNET" ! -d "$SUBNET" -j MASQUERADE
iptables -C FORWARD -i "$IFACE" -j ACCEPT 2>/dev/null \
  || iptables -I FORWARD 1 -i "$IFACE" -j ACCEPT
iptables -C FORWARD -o "$IFACE" -j ACCEPT 2>/dev/null \
  || iptables -I FORWARD 1 -o "$IFACE" -j ACCEPT
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw route allow in on "$IFACE" >/dev/null 2>&1 || true
  ufw route allow out on "$IFACE" >/dev/null 2>&1 || true
fi
iptables -t mangle -C FORWARD -o "$IFACE" -p tcp \
  --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \
  || iptables -t mangle -A FORWARD -o "$IFACE" -p tcp \
  --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
iptables -t mangle -C FORWARD -i "$IFACE" -p tcp \
  --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null \
  || iptables -t mangle -A FORWARD -i "$IFACE" -p tcp \
  --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -C FORWARD -i "$IFACE" -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null \
    || ip6tables -I FORWARD 1 -i "$IFACE" -j REJECT --reject-with icmp6-adm-prohibited
fi
"""

_UNIT = """\
[Unit]
Description=AmneziaWG 3.1 (amneziawg-go)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
Environment=LOG_LEVEL=info
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
TimeoutStartSec=90
ExecStartPre=/bin/mkdir -p /var/run/amneziawg
ExecStartPre=-/sbin/modprobe tun
ExecStartPre=-/sbin/ip link delete __IFACE__
ExecStartPre=-/bin/rm -f /var/run/amneziawg/__IFACE__.sock
ExecStart=/usr/local/bin/amneziawg-go -f __IFACE__
ExecStartPost=/usr/local/sbin/awg-configure
Restart=on-failure
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
"""

_PANEL_UNIT = f"""\
[Unit]
Description=AmneziaWG panel
After=network-online.target {AWG_SERVICE}.service
Wants={AWG_SERVICE}.service

[Service]
Type=simple
EnvironmentFile={PANEL_ENV}
WorkingDirectory={PANEL_ROOT}
ExecStart={PANEL_VENV}/bin/awg-panel serve
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


class RemoteError(Exception):
    pass


class Remote:
    def run(self, command: str, timeout: int = 60) -> str:
        raise NotImplementedError

    def run_script(self, script: str, timeout: int = 900) -> str:
        raise NotImplementedError


@dataclass
class InstallPlan:
    endpoint: str
    script: str
    files: dict[str, str] = field(default_factory=dict)


def configure_script(*, address: str, mtu: int) -> str:
    subnet = str(ipaddress.ip_network(address, strict=False))
    return (
        _CONFIGURE.replace("__IFACE__", AWG_INTERFACE)
        .replace("__AWG_DIR__", AWG_DIR)
        .replace("__ADDRESS__", address)
        .replace("__MTU__", str(mtu))
        .replace("__SUBNET__", subnet)
    )


def server_unit() -> str:
    return _UNIT.replace("__IFACE__", AWG_INTERFACE)


def panel_unit() -> str:
    return _PANEL_UNIT


def panel_env(*, password: str) -> str:
    return "\n".join(
        [
            "PANEL_HOST=127.0.0.1",
            "PANEL_PORT=8787",
            f"AWG_DIR={AWG_DIR}",
            "AWG_SOCKET=/var/run/amneziawg/awg0.sock",
            f"PANEL_DATA={PANEL_DATA}",
            f"PANEL_PASSWORD={password}",
            "",
        ]
    )


def merge_server_config(
    existing_yaml: str,
    *,
    port: int,
    endpoint: str,
    address: str = DEFAULT_ADDRESS,
    mtu: int = DEFAULT_MTU,
) -> AwgServerConfig:
    data = yaml.safe_load(existing_yaml) if existing_yaml.strip() else None
    if isinstance(data, dict) and data.get("private_key"):
        cfg = AwgServerConfig.from_yaml_dict(data)
        cfg.listen_port = port
        cfg.endpoint = endpoint
        cfg.address = address
        cfg.mtu = mtu
        return cfg
    return new_server_config(listen_port=port, address=address, mtu=mtu, endpoint=endpoint)


def build_plan(
    *,
    existing_yaml: str,
    peers_json: str,
    port: int,
    endpoint: str,
    address: str = DEFAULT_ADDRESS,
    mtu: int = DEFAULT_MTU,
) -> InstallPlan:
    if not endpoint:
        raise RemoteError("Нет публичного адреса. Передайте --endpoint")
    cfg = merge_server_config(
        existing_yaml,
        port=port,
        endpoint=endpoint,
        address=address,
        mtu=mtu,
    )
    peers = AwgPeerFile.from_json(peers_json or "{}")
    peer_list: list[AwgPeer] = list(peers.peers)
    files = {
        f"{AWG_DIR}/server.yml": yaml.safe_dump(cfg.to_yaml_dict(), sort_keys=False),
        f"{AWG_DIR}/peers.json": AwgPeerFile(peer_list).to_json(),
        f"{AWG_DIR}/uapi.set": render_uapi_sync(cfg, peer_list),
        "/usr/local/sbin/awg-configure": configure_script(address=cfg.address, mtu=cfg.mtu),
        f"/etc/systemd/system/{AWG_SERVICE}.service": server_unit(),
    }
    script = _install_script(cfg.listen_port, files)
    return InstallPlan(endpoint=f"{cfg.endpoint}:{cfg.listen_port}", script=script, files=files)


def install(
    remote: Remote,
    *,
    port: int = DEFAULT_PORT,
    endpoint: str = "",
    address: str = DEFAULT_ADDRESS,
    mtu: int = DEFAULT_MTU,
) -> str:
    existing = remote.run(f"cat {AWG_DIR}/server.yml 2>/dev/null || true")
    peers_json = remote.run(f"cat {AWG_DIR}/peers.json 2>/dev/null || true")
    if not endpoint:
        detected = remote.run("curl -4 -fsSL --max-time 10 https://ifconfig.me || true")
        endpoint = detected.strip().splitlines()[-1].strip() if detected.strip() else ""
    plan = build_plan(
        existing_yaml=existing,
        peers_json=peers_json,
        port=port,
        endpoint=endpoint,
        address=address,
        mtu=mtu,
    )
    remote.run_script(plan.script, timeout=900)
    return plan.endpoint


def deploy_script(*, env_text: str) -> str:
    env_b64 = base64.b64encode(env_text.encode()).decode()
    unit_b64 = base64.b64encode(panel_unit().encode()).decode()
    return f"""#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip
python3 -m venv {PANEL_VENV}
{PANEL_VENV}/bin/pip install -q -e {PANEL_ROOT}
mkdir -p {PANEL_DATA}
chmod 700 {PANEL_DATA}
printf '%s' {shlex.quote(env_b64)} | base64 -d > {PANEL_ENV}.tmp
mv {PANEL_ENV}.tmp {PANEL_ENV}
chmod 600 {PANEL_ENV}
printf '%s' {shlex.quote(unit_b64)} | base64 -d > /etc/systemd/system/awg-panel.service
systemctl daemon-reload
systemctl enable awg-panel
systemctl restart awg-panel
systemctl is-active --quiet awg-panel
"""


def check(remote: Remote) -> tuple[str, str]:
    active = remote.run(f"systemctl is-active {AWG_SERVICE} || true").strip()
    listen = remote.run("ss -ulnp | grep -E 'amneziawg|awg0' || true").strip()
    return active, listen


def _embed(path: str, content: str) -> str:
    payload = base64.b64encode(content.encode()).decode()
    quoted = shlex.quote(path)
    mode = "755" if path.endswith("awg-configure") else "600"
    return (
        f"mkdir -p \"$(dirname {quoted})\"\n"
        f"printf '%s' {shlex.quote(payload)} | base64 -d > {quoted}.tmp\n"
        f"mv {quoted}.tmp {quoted}\n"
        f"chmod {mode} {quoted}\n"
    )


def _install_script(port: int, files: dict[str, str]) -> str:
    sha_amd64 = GO_TARBALL_SHA256["amd64"]
    sha_arm64 = GO_TARBALL_SHA256["arm64"]
    header = f"""#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates git make gcc iptables python3 iproute2 ethtool
machine=$(uname -m)
if [ "$machine" = "x86_64" ]; then
  go_arch=amd64
  sha="{sha_amd64}"
elif [ "$machine" = "aarch64" ] || [ "$machine" = "arm64" ]; then
  go_arch=arm64
  sha="{sha_arm64}"
else
  echo "unsupported arch: $machine" >&2
  exit 1
fi
tarball="go{GO_VERSION}.linux-${{go_arch}}.tar.gz"
if ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go{GO_VERSION}"; then
  curl -fsSL -o "/tmp/${{tarball}}" "https://go.dev/dl/${{tarball}}"
  echo "$sha  /tmp/${{tarball}}" | sha256sum -c -
  rm -rf /usr/local/go
  tar -C /usr/local -xzf "/tmp/${{tarball}}"
  rm -f "/tmp/${{tarball}}"
fi
tag="{AMNEZIAWG_GO_TAG}"
commit="{AMNEZIAWG_GO_COMMIT}"
src=/usr/local/src/amneziawg-go
mkdir -p {AWG_DIR}
if [ -x /usr/local/bin/amneziawg-go ] && [ "$(cat {AWG_DIR}/amneziawg-go.version 2>/dev/null || true)" = "$tag" ]; then
  echo "amneziawg-go $tag already installed"
else
  rm -rf "$src"
  git clone --branch "$tag" --depth 1 {AMNEZIAWG_GO_REPO} "$src"
  git -C "$src" rev-parse HEAD | grep -qx "$commit"
  (cd "$src" && PATH=/usr/local/go/bin:$PATH make)
  install -m 755 "$src/amneziawg-go" /usr/local/bin/amneziawg-go
  printf '%s\\n' "$tag" > {AWG_DIR}/amneziawg-go.version
fi
"""
    body = "".join(_embed(path, content) for path, content in files.items())
    footer = f"""
systemctl daemon-reload
systemctl reset-failed {AWG_SERVICE} 2>/dev/null || true
systemctl enable {AWG_SERVICE}
systemctl restart {AWG_SERVICE}
if ! systemctl is-active --quiet {AWG_SERVICE}; then
  journalctl -u {AWG_SERVICE} --no-pager -n 40 >&2 || true
  exit 1
fi
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow {int(port)}/udp comment 'amneziawg' || true
fi
echo "amneziawg listening on udp/{int(port)}"
"""
    return header + body + footer
